"""Extend the audited Bybit archive one contiguous set of closed UTC days at a time."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime
import fcntl
import json
import logging
from pathlib import Path
from threading import Event, Lock, Thread

from history import (
    DAY, MINUTE_MS, UTC, bybit, closes, evaluate_history, midnight, minute_windows,
    portfolio_fingerprint, positions, settlement_rates, utc_text, write_report,
)


LOGGER = logging.getLogger(__name__)
SEED_FILES = {
    'report': 'history-report-common.json',
    'observations': 'bybit-observations-common.json',
    'archive': 'bybit-observations-per-asset.json',
}
ARCHIVE_SERIES = {
    'spot_daily': 'spot_daily',
    'mark_daily': 'perpetual_mark_daily',
    'funding': 'settled_funding',
}
SERIES = (*ARCHIVE_SERIES, 'minute_marks')
MAX_BATCH_DAYS = 30
ARCHIVE_ERRORS = (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, ArithmeticError)
SEED_RECOVERY_WARNING = 'The saved history could not be verified. Verified seed history was used for recovery.'


def fetch_missing_days(portfolio, start, end):
    """Fetch just the missing dates and the marks needed to value their settlements."""
    holdings = positions(portfolio)
    spot_symbols = {symbol for kind, symbol, _, _ in holdings if kind == 'SPOT' and symbol != 'USDT'}
    perp_symbols = {symbol for kind, symbol, _, _ in holdings if kind == 'PERP'}
    begin, stop = midnight(start), midnight(end + DAY)
    with ThreadPoolExecutor(max_workers=8) as pool:
        spot = {symbol: pool.submit(closes, 'kline', 'spot', symbol, 'D', begin, stop)
                for symbol in sorted(spot_symbols)}
        marks = {symbol: pool.submit(closes, 'mark-price-kline', 'linear', symbol, 'D', begin, stop)
                 for symbol in sorted(perp_symbols)}
        rates = {symbol: pool.submit(settlement_rates, symbol, begin, stop) for symbol in sorted(perp_symbols)}
        instruments = {symbol: pool.submit(bybit, 'instruments-info', category='linear', symbol=symbol)
                       for symbol in sorted(perp_symbols)}
        funding = {symbol: request.result() for symbol, request in rates.items()}
        intervals = {
            symbol: int(row['fundingInterval'])
            for symbol, request in instruments.items()
            for row in request.result()
            if row['symbol'] == symbol and row['contractType'] == 'LinearPerpetual' and row['settleCoin'] == 'USDT'
        }
        required_marks = {
            symbol: {stamp - stamp % MINUTE_MS - MINUTE_MS for stamp in rates}
            for symbol, rates in funding.items()
        }
        minutes = {
            symbol: [pool.submit(closes, 'mark-price-kline', 'linear', symbol, '1', first, past_last)
                     for first, past_last in minute_windows(required)]
            for symbol, required in required_marks.items()
        }
        return {
            'interval_minutes': intervals,
            'spot_daily': {symbol: request.result() for symbol, request in spot.items()},
            'mark_daily': {symbol: request.result() for symbol, request in marks.items()},
            'funding': funding,
            'minute_marks': {symbol: {stamp: mark for request in requests for stamp, mark in request.result().items()
                                      if stamp in required_marks[symbol]}
                             for symbol, requests in minutes.items()},
        }


def rebuild_report(portfolio, observations):
    window = observations['window']
    return evaluate_history(
        portfolio, date.fromisoformat(window['start_date']), date.fromisoformat(window['end_date']),
        interval_minutes=observations['interval_minutes'],
        **{name: {symbol: {int(stamp): value for stamp, value in values.items()}
                  for symbol, values in observations[name].items()} for name in SERIES},
    )


def extend_generation(previous, portfolio, new, end):
    updated = deepcopy(previous)
    observations, archive = updated['observations'], updated['archive']
    if portfolio_fingerprint(portfolio) != observations['portfolio_sha256']:
        raise ValueError('Historical research portfolio differs from the current portfolio.')
    if new['interval_minutes'] != observations['interval_minutes']:
        raise ValueError('Bybit funding intervals differ from the audited archive. Verify cadence before extending history.')
    for name in SERIES:
        for symbol, values in new[name].items():
            added = {str(stamp): str(value) for stamp, value in values.items()}
            destination = observations[name][symbol]
            if destination.keys() & added.keys():
                raise ValueError(f'Overlapping {name} observations for {symbol}.')
            destination.update(added)
            if name in ARCHIVE_SERIES and added:
                entry = archive['symbols'][symbol][ARCHIVE_SERIES[name]]
                if entry['values'].keys() & added.keys():
                    raise ValueError(f'Overlapping archival {name} observations for {symbol}.')
                entry['values'].update(added)
                entry['count'] = len(entry['values'])
                entry['last_timestamp'] = utc_text(max(map(int, added)))
    observations['window']['end_date'] = end.isoformat()
    archive['through_utc_day'] = end.isoformat()
    report = rebuild_report(portfolio, observations)
    if report['status'] != 'available':
        raise ValueError('; '.join(report['issues'][:3]) or 'New historical observations are incomplete.')
    published = previous['report']
    if report['baseline'] != published['baseline'] or report['series'][:len(published['series'])] != published['series']:
        raise ValueError('New historical observations would change an existing account ledger entry.')
    updated['report'] = report
    return updated


class HistoryBackfill:
    def __init__(self, research_dir, *, portfolio_path=None, clock=None, fetch=None, interval_seconds=300,
                 generation=None):
        self.research = Path(research_dir)
        self.output = self.research / 'history-generation.json'
        self.portfolio = self._read_json(portfolio_path or self.research.parent / 'portfolio.json')
        self.clock = clock or (lambda: datetime.now(UTC))
        self.fetch = fetch or fetch_missing_days
        self.interval_seconds = interval_seconds
        if generation is None:
            self._generation, self._recovery_warning = self._load_generation()
        else:
            self._validate_generation(generation)
            self._generation, self._recovery_warning = generation, None
        self._lock = Lock()
        self._refresh = Lock()
        self._stop = Event()
        self._thread = None
        self._last_checked_at = None
        self._error = None
        self._updating = False

    @staticmethod
    def _read_json(path):
        return json.loads(Path(path).read_text(encoding='utf-8'))

    def _load_generation(self):
        if self.output.exists():
            try:
                generation = self._read_json(self.output)
                self._validate_generation(generation)
                return generation, None
            except ARCHIVE_ERRORS as error:
                LOGGER.warning('Saved historical archive rejected: %s', error)
                warning = SEED_RECOVERY_WARNING
        else:
            warning = None
        generation = {name: self._read_json(self.research / filename) for name, filename in SEED_FILES.items()}
        self._validate_generation(generation)
        return generation, warning

    def _validate_generation(self, generation):
        completed_date = generation['report']['window']['end_date']
        if (generation['report']['status'] != 'available'
                or generation['observations']['window']['end_date'] != completed_date
                or generation['archive']['through_utc_day'] != completed_date):
            raise ValueError('Historical generation does not contain a complete shared UTC window.')
        fingerprint = portfolio_fingerprint(self.portfolio)
        if any(generation[name].get('portfolio_sha256') != fingerprint for name in ('report', 'observations')):
            raise ValueError('The historical report and observations must match the active portfolio.')
        report = rebuild_report(self.portfolio, generation['observations'])
        if any(report[field] != generation['report'][field]
               for field in ('status', 'window', 'baseline', 'series', 'metrics')):
            raise ValueError('The historical report does not match the stored observations.')
        for name, archive_name in ARCHIVE_SERIES.items():
            for symbol, values in generation['observations'][name].items():
                published = generation['archive']['symbols'][symbol][archive_name]['values']
                if any(published.get(stamp) != value for stamp, value in values.items()):
                    raise ValueError(f'{archive_name} for {symbol} does not match the stored observations.')

    def read(self):
        """Read a consistent report, input observations and archive. Treat them as read-only."""
        with self._lock:
            return self._generation

    def status(self):
        return self.view()[1]

    def view(self):
        """Read the generation and its ingestion status at the same instant."""
        with self._lock:
            target = self.clock().astimezone(UTC).date() - DAY
            covered = self._generation['report']['window']['end_date']
            state = 'current' if date.fromisoformat(covered) >= target else 'pending'
            if self._updating:
                state = 'updating'
            elif self._error:
                state = 'error'
            return self._generation, {
                'state': state, 'covered_through': covered, 'target_utc_day': target.isoformat(),
                'last_checked_at': self._last_checked_at, 'error': self._error,
                'warning': self._recovery_warning,
            }

    def refresh_once(self):
        """Retry missing closed days and publish after every complete contiguous batch."""
        with self._refresh:
            now = self.clock().astimezone(UTC)
            target = now.date() - DAY
            with self._lock:
                self._last_checked_at = now.isoformat().replace('+00:00', 'Z')
                self._error = None
                self._updating = date.fromisoformat(self._generation['report']['window']['end_date']) < target
            if not self._updating:
                return self.status()
            try:
                # Serialize publication if two local servers share this research directory.
                with (self.research / '.history-generation.lock').open('a') as writer:
                    fcntl.flock(writer, fcntl.LOCK_EX)
                    current, warning = self._load_generation()
                    with self._lock:
                        held_until = self._generation['report']['window']['end_date']
                        if warning and current['report']['window']['end_date'] < held_until:
                            current = self._generation
                            warning = 'The saved history could not be verified. Kept the latest verified in-memory report.'
                        self._generation = current
                        self._recovery_warning = warning or self._recovery_warning
                    start = date.fromisoformat(current['report']['window']['end_date']) + DAY
                    while start <= target and not self._stop.is_set():
                        end = min(start + (MAX_BATCH_DAYS - 1) * DAY, target)
                        current = extend_generation(current, self.portfolio, self.fetch(self.portfolio, start, end), end)
                        write_report(self.output, current)
                        with self._lock:
                            self._generation = current
                        LOGGER.info('Historical data backfilled for %s to %s UTC', start, end)
                        start = end + DAY
            except ARCHIVE_ERRORS as error:
                LOGGER.warning('Historical backfill pending: %s', error)
                with self._lock:
                    self._error = str(error)
            finally:
                with self._lock:
                    self._updating = False
            return self.status()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception:
                LOGGER.exception('Historical update interrupted; will retry.')
                with self._lock:
                    self._updating = False
                    self._error = 'Historical update interrupted. The server will retry.'
            self._stop.wait(self.interval_seconds)

    def start(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = Thread(target=self._run, name='history-backfill', daemon=True)
                self._thread.start()

    def stop(self):
        self._stop.set()
