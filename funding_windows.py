"""Rolling eight-hour cash flows from settled Bybit rates and prior-minute marks."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
import logging
from threading import Event, Lock, Thread

from history import (
    DAY, MINUTE_MS, UTC, bybit, closes, midnight, minute_windows, numeric, positions,
    settlement_rates, utc_text,
)


WINDOW_HOURS = 8
WINDOW_MS = WINDOW_HOURS * 60 * MINUTE_MS
WINDOW_COUNT = 21
MAX_CLOCK_CHECK_SECONDS = 300
LOGGER = logging.getLogger(__name__)


def fetch_settled(symbol, start, stop):
    instruments = bybit('instruments-info', category='linear', symbol=symbol)
    interval = next((int(row['fundingInterval']) for row in instruments
                     if row['symbol'] == symbol and row['contractType'] == 'LinearPerpetual'
                     and row['settleCoin'] == 'USDT'), None)
    rates = settlement_rates(symbol, start, stop)
    required = {previous_minute(timestamp) for timestamp in rates}
    marks = {}
    for begin, end in minute_windows(required):
        marks.update({timestamp: value for timestamp, value in
                      closes('mark-price-kline', 'linear', symbol, '1', begin, end).items()
                      if timestamp in required})
    return interval, rates, marks


def previous_minute(timestamp):
    return timestamp - timestamp % MINUTE_MS - MINUTE_MS


def rates_by_window(rates, first, stop):
    groups = defaultdict(dict)
    for timestamp, rate in rates.items():
        timestamp = int(timestamp)
        if first <= timestamp < stop:
            groups[timestamp - timestamp % WINDOW_MS][timestamp] = rate
    return groups


def funding_for_window(quantity, start, interval, rates, marks):
    """Return an amount, count and optional coverage issue for a single contract."""
    if type(interval) is not int or interval <= 0 or 1440 % interval:
        return None, len(rates), 'funding cadence unavailable'
    cadence = interval * MINUTE_MS
    first = start + (-start % cadence)
    missing = next((timestamp for timestamp in range(first, start + WINDOW_MS, cadence)
                    if timestamp not in rates), None)
    if missing is not None:
        return None, len(rates), f'published settlement unavailable at {utc_text(missing)}'

    amount = Decimal(0)
    for timestamp, rate in rates.items():
        minute = previous_minute(timestamp)
        mark = marks.get(minute)
        if mark is None:
            return None, len(rates), f'preceding one-minute mark unavailable at {utc_text(minute)}'
        amount -= quantity * numeric(mark, positive=True) * numeric(rate)
    return amount, len(rates), None


def archival_cutoff(observations):
    return midnight(date.fromisoformat(observations['window']['end_date']) + DAY)


def funding_archive(symbol, archive):
    """Project published settlements into individual and UTC-daily series."""
    contracts = archive['symbols']
    if symbol not in contracts:
        raise ValueError('Choose a supported USDT perpetual contract.')
    source = contracts[symbol]['settled_funding']
    events = []
    daily = defaultdict(list)
    for stamp, rate in source['values'].items():
        time = datetime.fromtimestamp(int(stamp) / 1_000, UTC)
        day = time.date().isoformat()
        daily[day].append(Decimal(rate))
        events.append({'as_of': time.isoformat(), 'rate': float(rate)})
    return {
        'symbol': symbol,
        'first_observation': source['first_timestamp'],
        'last_observation': source['last_timestamp'],
        'events': sorted(events, key=lambda event: event['as_of']),
        'days': [{'date': day, 'rate': float(sum(rates)), 'settlements': len(rates)}
                 for day, rates in sorted(daily.items())],
    }


class FundingWindows:
    def __init__(self, portfolio, read_observations, *, clock=None, fetch=None, interval_seconds=30):
        self._read_observations = read_observations
        self.clock = clock or (lambda: datetime.now(UTC))
        self.fetch = fetch or fetch_settled
        self.interval_seconds = interval_seconds
        self.quantities = defaultdict(Decimal)
        for instrument, symbol, quantity, _ in positions(portfolio):
            if instrument == 'PERP':
                self.quantities[symbol] += quantity
        self._intraday = {}
        self._lock = Lock()
        self._refresh = Lock()
        self._stop = Event()
        self._thread = None

    def _bounds(self):
        now = int(self.clock().timestamp()) * 1_000
        stop = now - now % WINDOW_MS
        return stop - WINDOW_COUNT * WINDOW_MS, stop

    def read(self):
        """Build a current chart from the published generation and in-memory intraday data."""
        first, stop = self._bounds()
        observations = self._read_observations()
        cutoff = min(archival_cutoff(observations), stop)
        archived = range(first, cutoff, WINDOW_MS)
        with self._lock:
            intraday = self._intraday
        history = {start: {} for start in archived}
        for symbol, quantity in self.quantities.items():
            rates = rates_by_window(observations['funding'][symbol], first, cutoff)
            marks = observations['minute_marks'][symbol]
            # The saved JSON records Unix milliseconds as keys, already validated by daily ingestion.
            needed = {previous_minute(timestamp) for group in rates.values() for timestamp in group}
            selected_marks = {timestamp: marks[str(timestamp)] for timestamp in needed if str(timestamp) in marks}
            interval = observations['interval_minutes'].get(symbol)
            for start in archived:
                history[start][symbol] = funding_for_window(quantity, start, interval, rates[start], selected_marks)

        windows = []
        issues = []
        for start in range(first, stop, WINDOW_MS):
            details = history.get(start, intraday.get(start, {}))
            amounts = []
            assets = {}
            for symbol in self.quantities:
                amount, count, issue = details.get(symbol, (None, 0, 'awaiting settlement verification'))
                asset = symbol.removesuffix('USDT')
                assets[asset] = {'funding_usdt': None if amount is None else float(amount), 'settlements': count}
                if issue:
                    issues.append(f'{utc_text(start)} · {asset}: {issue}')
                if amount is not None:
                    amounts.append(amount)
            complete = len(amounts) == len(self.quantities)
            windows.append({
                'start_at': utc_text(start), 'end_at': utc_text(start + WINDOW_MS),
                'net_funding_usdt': float(sum(amounts)) if complete else None,
                'status': 'complete' if complete else 'missing', 'assets': assets,
            })
        complete = sum(row['status'] == 'complete' for row in windows)
        return {
            'window_hours': WINDOW_HOURS, 'timezone': 'UTC', 'as_of': utc_text(stop),
            'status': 'complete' if complete == WINDOW_COUNT else 'partial' if complete else 'unavailable',
            'windows': windows, 'complete_windows': complete, 'issues': issues,
        }

    def refresh_once(self):
        """Retry incomplete contract windows without blocking chart reads."""
        with self._refresh:
            first, stop = self._bounds()
            cutoff = archival_cutoff(self._read_observations())
            starts = range(max(first, cutoff), stop, WINDOW_MS)
            with self._lock:
                updated = {start: self._intraday.get(start, {}).copy() for start in starts}
            pending = {
                symbol: [start for start in starts if updated[start].get(symbol, (None,))[0] is None]
                for symbol in self.quantities
            }
            with ThreadPoolExecutor(max_workers=4) as pool:
                requests = {symbol: pool.submit(self.fetch, symbol, starts[0], starts[-1] + WINDOW_MS)
                            for symbol, starts in pending.items() if starts}
                for symbol, request in requests.items():
                    wanted = pending[symbol]
                    try:
                        interval, all_rates, marks = request.result()
                        rates = rates_by_window(all_rates, wanted[0], wanted[-1] + WINDOW_MS)
                        for start in wanted:
                            updated[start][symbol] = funding_for_window(
                                self.quantities[symbol], start, interval, rates[start], marks,
                            )
                    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
                        for start in wanted:
                            updated[start][symbol] = (None, 0, str(error) or type(error).__name__)
            with self._lock:
                self._intraday = updated
        return self.read()

    def _wait_seconds(self, result):
        now = self.clock().timestamp()
        window_seconds = WINDOW_MS // 1_000
        cutoff = int(now) - int(now) % window_seconds
        if result['as_of'] != utc_text(cutoff * 1_000):
            return 0  # The boundary passed while processing the previous window.
        next_boundary = cutoff + window_seconds
        recheck = MAX_CLOCK_CHECK_SECONDS if result['status'] == 'complete' else self.interval_seconds
        return min(next_boundary - now, recheck)

    def _run(self):
        while not self._stop.is_set():
            try:
                result = self.refresh_once()
                # Recheck for laptop sleep and clock corrections.
                delay = self._wait_seconds(result)
            except Exception:
                LOGGER.exception('Eight-hour funding update interrupted; will retry.')
                delay = self.interval_seconds
            self._stop.wait(delay)

    def start(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = Thread(target=self._run, name='funding-windows', daemon=True)
                self._thread.start()

    def stop(self):
        self._stop.set()
