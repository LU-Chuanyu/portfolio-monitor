"""Fetch and validate a two-source Bybit snapshot for the local preview."""

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import gzip
from http.client import HTTPException
import json
import logging
from math import isfinite
import os
from pathlib import Path
import ssl
from threading import Lock
from time import monotonic, sleep, time_ns
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zlib import error as ZlibError

from bybit_public import TICKERS_URL, TLS_CONTEXT, now_utc
from bybit_stream import BybitTickerStream, TICKER_FIELDS
from portfolio_metrics import calculate_portfolio, number, timestamp_ms


CATEGORIES = ('spot', 'linear')
LOG = logging.getLogger(__name__)
REQUEST_TIMEOUT = 6
RETRY_DELAY = 0.25
RETRYABLE_HTTP = {408, 500, 502, 503, 504}
PRICE_FIELDS = {'spot': 'lastPrice', 'linear': 'markPrice'}
DEFAULT_PORTFOLIO_PATH = Path(__file__).resolve().parent / 'examples' / 'portfolio.json'
PORTFOLIO_PATH = Path(os.environ.get('CMCC_PORTFOLIO_PATH', DEFAULT_PORTFOLIO_PATH)).expanduser()
PORTFOLIO = json.loads(PORTFOLIO_PATH.read_text(encoding='utf-8'))
TRADED_SYMBOLS = {
    category: {
        row['symbol'] for row in PORTFOLIO['positions']
        if row['asset'] != 'USDT' and row['instrument'] == instrument
    }
    for category, instrument in (('spot', 'SPOT'), ('linear', 'PERP'))
}
# 75 seconds allows a polling interval plus slack. Small clock drift is normal,
# but prices returned over eight seconds apart should not be combined.
MAX_AGE_MS = 75_000
MAX_FUTURE_MS = 10_000
MAX_PAIR_SKEW_MS = 8_000
FUNDING_TIME_LABELS = {
    'missing_funding_time': 'next funding time missing',
    'invalid_funding_time': 'next funding time invalid',
    'expired_funding_time': 'next funding time already passed',
}

_snapshot_lock = Lock()
_in_flight = None
_rest_cache = None
REST_CACHE_SECONDS = 10
_stream = BybitTickerStream(TRADED_SYMBOLS, TLS_CONTEXT)


def source_result(exchange_time=None, received_at=None, reason=None, transport='rest'):
    return {
        'status': 'unavailable' if reason else 'available',
        'exchange_time_ms': exchange_time,
        'received_at': received_at,
        'reason': reason,
        'transport': transport,
    }


def fetch_category(category):
    request = Request(
        TICKERS_URL + category,
        headers={'User-Agent': 'CMCC-portfolio-case/1.0', 'Accept-Encoding': 'gzip'},
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT, context=TLS_CONTEXT) as response:
        raw = response.read()
        encoding = response.headers.get('Content-Encoding', '').strip().lower()
    receipt_ms = time_ns() // 1_000_000
    received_at = datetime.fromtimestamp(receipt_ms / 1000, timezone.utc).isoformat()
    try:
        if encoding == 'gzip':
            raw = gzip.decompress(raw)
        elif encoding not in ('', 'identity'):
            return {}, source_result(received_at=received_at, reason='invalid_response')
        body = json.loads(raw)
    except (EOFError, OSError, ValueError, ZlibError):
        return {}, source_result(received_at=received_at, reason='invalid_response')

    if not isinstance(body, dict) or type(body.get('retCode')) is not int:
        return {}, source_result(received_at=received_at, reason='invalid_response')
    if body['retCode'] != 0:
        return {}, source_result(received_at=received_at, reason='exchange_rejected')

    result = body.get('result')
    if (
        not isinstance(result, dict)
        or result.get('category') != category
        or not isinstance(result.get('list'), list)
    ):
        return {}, source_result(received_at=received_at, reason='invalid_response')
    tickers = {}
    for ticker in result['list']:
        symbol = ticker.get('symbol') if isinstance(ticker, dict) else None
        if not isinstance(symbol, str) or not symbol or symbol in tickers:
            return {}, source_result(received_at=received_at, reason='invalid_response')
        tickers[symbol] = ticker

    raw_time = body.get('time')
    exchange_time = timestamp_ms(raw_time)
    if raw_time in (None, ''):
        reason = 'missing_exchange_time'
    elif exchange_time is None:
        reason = 'invalid_exchange_time'
    elif receipt_ms - exchange_time > MAX_AGE_MS:
        reason = 'stale_exchange_time'
    elif exchange_time - receipt_ms > MAX_FUTURE_MS:
        reason = 'future_exchange_time'
    else:
        reason = None
    return tickers, source_result(exchange_time, received_at, reason)


def fetch_attempt(category):
    try:
        tickers, source = fetch_category(category)
    except HTTPError as error:
        source = source_result(received_at=now_utc(), reason='http_error')
        source['http_status'] = error.code
        return {}, source, error.code in RETRYABLE_HTTP
    except URLError as error:
        reason = 'timeout' if isinstance(error.reason, TimeoutError) else 'network_error'
        return {}, source_result(reason=reason), not isinstance(error.reason, ssl.SSLCertVerificationError)
    except TimeoutError:
        return {}, source_result(reason='timeout'), True
    except ssl.SSLCertVerificationError:
        return {}, source_result(reason='network_error'), False
    except (OSError, HTTPException):
        return {}, source_result(reason='network_error'), True
    return tickers, source, source['reason'] == 'stale_exchange_time'


def price_coverage(category, tickers):
    field = PRICE_FIELDS[category]
    return sum((number(tickers.get(symbol, {}).get(field)) or 0) > 0 for symbol in TRADED_SYMBOLS[category])


def fetch_with_retry(category):
    tickers, source, retryable = fetch_attempt(category)
    coverage = price_coverage(category, tickers)
    if not retryable and (source['reason'] or coverage == len(TRADED_SYMBOLS[category])):
        return tickers, source
    LOG.info('Bybit %s REST retry (reason=%s, priced=%s/%s)',
             category, source['reason'] or 'incomplete_prices', coverage, len(TRADED_SYMBOLS[category]))
    sleep(RETRY_DELAY)
    fresh, fresh_source, _ = fetch_attempt(category)
    # Retain the most complete coherent reply. Keep the newest when coverage ties.
    if source['reason'] or (
        not fresh_source['reason'] and price_coverage(category, fresh) >= coverage
    ):
        return fresh, fresh_source
    return tickers, source


def check_funding_times(linear, reference_ms):
    """Remove estimates that cannot be tied to a future settlement."""
    reasons = {}
    for position in PORTFOLIO['positions']:
        if position['instrument'] != 'PERP':
            continue
        symbol = position['symbol']
        ticker = linear.get(symbol)
        if ticker is None:
            continue
        raw_time = ticker.get('nextFundingTime')
        next_time = timestamp_ms(raw_time)
        if raw_time in (None, ''):
            reason = 'missing_funding_time'
        elif next_time is None:
            reason = 'invalid_funding_time'
        elif next_time <= reference_ms:
            reason = 'expired_funding_time'
        else:
            continue
        ticker.pop('fundingRate', None)
        ticker.pop('nextFundingTime', None)
        reasons[symbol] = reason
    return reasons


def market_snapshot():
    markets, sources = {}, {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {category: pool.submit(fetch_with_retry, category) for category in CATEGORIES}
        for category, future in futures.items():
            markets[category], sources[category] = future.result()
            source = sources[category]
            if source['reason']:
                LOG.warning('Bybit %s REST unavailable (reason=%s, http=%s, exchange_time_ms=%s)',
                            category, source['reason'], source.get('http_status'), source['exchange_time_ms'])
    return assemble_snapshot(markets, sources, 'rest')


def archive_value(value):
    if type(value) is float and not isfinite(value):
        return repr(value)
    if value is None or type(value) in (str, int, float, bool):
        return value
    return f'<{type(value).__name__}>'


def reject_misaligned_quotes(markets, sources, transport):
    """Invalidate each category that contains an older out-of-sync leg."""
    if not all(source['status'] == 'available' for source in sources.values()):
        return
    if transport == 'websocket':
        matched = TRADED_SYMBOLS['spot'] & TRADED_SYMBOLS['linear']
        comparisons = (
            (symbol, sources['spot']['symbol_exchange_time_ms'][symbol],
             sources['linear']['symbol_exchange_time_ms'][symbol])
            for symbol in sorted(matched)
        )
    else:
        # The REST batch envelope supplies one exchange time for each category.
        comparisons = [(None, sources['spot']['exchange_time_ms'], sources['linear']['exchange_time_ms'])]

    rejected = {'spot': {}, 'linear': {}}
    for symbol, spot_ms, linear_ms in comparisons:
        skew_ms = abs(spot_ms - linear_ms)
        if skew_ms > MAX_PAIR_SKEW_MS:
            older = 'spot' if spot_ms < linear_ms else 'linear'
            rejected[older][symbol] = skew_ms
    for category, skew_by_symbol in rejected.items():
        if skew_by_symbol:
            source = sources[category]
            source.update(status='unavailable', reason='exchange_time_skew')
            if transport == 'websocket':
                source['pair_skew_ms'] = skew_by_symbol
            markets.pop(category)


def next_settlement(positions, as_of, reference_ms):
    """Summarize the earliest common funding timestamp among verified schedules."""
    legs = [row for row in positions if row['instrument'] == 'PERP']
    result = {
        'timestamp_ms': None,
        'amount_usdt': None,
        'included_contracts': 0,
        'total_contracts': len(legs),
        'as_of': as_of,
    }
    if not legs or any((row['next_funding_time'] or 0) <= reference_ms for row in legs):
        return result
    earliest = min(row['next_funding_time'] for row in legs)
    amounts = [row['expected_funding'] for row in legs if row['next_funding_time'] == earliest]
    result['timestamp_ms'] = earliest
    result['included_contracts'] = len(amounts)
    if all(value is not None and isfinite(value) for value in amounts):
        amount = sum(amounts)
        if isfinite(amount):
            result['amount_usdt'] = amount
    return result


def assemble_snapshot(markets, sources, transport):
    # Record only the public fields this monitor consumes, before rejecting unsafe values.
    inputs = {}
    for category in CATEGORIES:
        tickers = markets.get(category, {})
        inputs[category] = {
            symbol: {
                field: archive_value(tickers[symbol][field])
                for field in TICKER_FIELDS[category] if field in tickers[symbol]
            }
            for symbol in sorted(TRADED_SYMBOLS[category] & tickers.keys())
        }
    markets = {category: tickers for category, tickers in markets.items()
               if sources[category]['status'] == 'available'}
    reject_misaligned_quotes(markets, sources, transport)

    observed_ms = time_ns() // 1_000_000
    reference_ms = max(
        [observed_ms]
        + [source['exchange_time_ms'] for source in sources.values() if source['status'] == 'available']
    )
    funding_reasons = check_funding_times(markets.get('linear', {}), reference_ms)
    errors = [
        f"{category.upper()} Bybit request failed ({source['reason'].replace('_', ' ')})"
        for category, source in sources.items() if source['reason']
    ]
    errors.extend(
        f'{symbol} PERP: {FUNDING_TIME_LABELS[reason]}'
        for symbol, reason in funding_reasons.items()
    )
    snapshot = calculate_portfolio(PORTFOLIO, markets, errors)
    snapshot['as_of'] = datetime.fromtimestamp(observed_ms / 1_000, timezone.utc).isoformat()
    snapshot['next_settlement'] = next_settlement(snapshot['positions'], snapshot['as_of'], reference_ms)
    for row in snapshot['positions']:
        if row['instrument'] == 'PERP' and row['symbol'] in funding_reasons:
            row['funding_time_reason'] = funding_reasons[row['symbol']]
    if not snapshot['issues']:
        snapshot['status'] = 'ready'
    elif any(row['asset'] != 'USDT' and row['current_price'] is not None for row in snapshot['positions']):
        snapshot['status'] = 'partial'
    else:
        snapshot['status'] = 'unavailable'
    snapshot['sources'] = sources
    snapshot['transport'] = transport
    snapshot['inputs'] = inputs
    return snapshot


def demo_market_snapshot():
    """Publish synthetic quotes through the same valuation boundary as live quotes."""
    stamp = time_ns() // 1_000_000
    period = 8 * 60 * 60 * 1_000
    settlement = (stamp // period + 1) * period
    movement = ((stamp // 5_000) % 21 - 10) / 20_000
    markets = {category: {} for category in CATEGORIES}
    for row in PORTFOLIO['positions']:
        if row['asset'] == 'USDT':
            continue
        is_spot = row['instrument'] == 'SPOT'
        price = row['entry_price_usdt'] * (1 + movement)
        if is_spot:
            markets['spot'][row['symbol']] = {'lastPrice': str(price)}
        else:
            funding_rate = '0.00008' if row['asset'] in ('BTC', 'SOL') else '-0.00004'
            markets['linear'][row['symbol']] = {
                'markPrice': str(price * (1 + movement / 5)),
                'fundingRate': funding_rate,
                'nextFundingTime': str(settlement),
            }
    sources = {category: source_result(stamp, now_utc(), transport='synthetic') for category in CATEGORIES}
    return assemble_snapshot(markets, sources, 'synthetic')


def shared_rest_snapshot(*, force=False):
    """Reuse a recent REST response to bound the collector's fallback traffic."""
    global _in_flight, _rest_cache
    with _snapshot_lock:
        pending = _in_flight
        if pending is None:
            if not force and _rest_cache is not None:
                deadline, snapshot = _rest_cache
                if monotonic() < deadline:
                    return snapshot
            pending = _in_flight = Future()
            owns_round = True
        else:
            owns_round = False
    if not owns_round:
        return pending.result()
    try:
        result = market_snapshot()
    except BaseException as error:
        with _snapshot_lock:
            pending.set_exception(error)
            _in_flight = None
        raise
    with _snapshot_lock:
        _rest_cache = (monotonic() + REST_CACHE_SECONDS, result)
        pending.set_result(result)
        _in_flight = None
    return result


def shared_market_snapshot(*, prefer_rest=False):
    """Read REST for minute samples or use the live streams between samples."""
    _stream.start()
    rest = shared_rest_snapshot(force=True) if prefer_rest else None
    if rest is not None and rest['status'] == 'ready':
        return rest
    markets, sources = _stream.snapshot_inputs(time_ns() // 1_000_000)
    if all(source['status'] == 'available' for source in sources.values()):
        candidate = assemble_snapshot(markets, sources, 'websocket')
        if candidate['status'] == 'ready':
            return {**candidate, 'stream':sources}
    return {**(rest if rest is not None else shared_rest_snapshot()), 'stream':sources}
