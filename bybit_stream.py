"""Keep the portfolio's public Bybit ticker snapshots ready for the preview."""

from datetime import datetime, timezone
import json
import logging
from threading import Lock, Thread
from time import monotonic, sleep, time_ns

from portfolio_metrics import number, timestamp_ms

try:
    import websocket
except ImportError:
    websocket = None


STREAM_URL = 'wss://stream.bybit.com/v5/public/'
MAX_TICK_AGE_MS = 15_000
MAX_FUTURE_MS = 10_000
PING_INTERVAL = 20
PONG_TIMEOUT = 8
TICKER_FIELDS = {
    'spot': ('lastPrice',),
    'linear': ('markPrice', 'fundingRate', 'nextFundingTime'),
}
LOG = logging.getLogger(__name__)


class BybitTickerStream:
    def __init__(self, symbols, tls_context):
        self.symbols = symbols
        self.tls_context = tls_context
        self.lock = Lock()
        self.started = False
        initial = 'stream_connecting' if websocket else 'stream_dependency_missing'
        self.states = dict.fromkeys(symbols, initial)
        self.rows = {category: {} for category in symbols}

    def start(self):
        with self.lock:
            if self.started or websocket is None:
                return
            self.started = True
            for category in self.symbols:
                Thread(target=self._maintain, args=(category,), name=f'bybit-{category}', daemon=True).start()

    def _reset(self, category, state):
        with self.lock:
            self.rows[category].clear()
            self.states[category] = state

    def handle_ticker(self, category, message, receipt_ms):
        data = message.get('data')
        stamp = timestamp_ms(message.get('ts'))
        kind = message.get('type')
        if not isinstance(data, dict) or kind not in ('snapshot', 'delta') or stamp is None:
            return
        topic = message.get('topic')
        if not isinstance(topic, str) or not topic.startswith('tickers.'):
            return
        symbol = topic.removeprefix('tickers.')
        if symbol not in self.symbols[category] or data.get('symbol', symbol) != symbol:
            return
        if stamp > receipt_ms + MAX_FUTURE_MS or receipt_ms - stamp > MAX_TICK_AGE_MS:
            return
        fields = {field:data[field] for field in TICKER_FIELDS[category] if field in data}
        with self.lock:
            previous = self.rows[category].get(symbol)
            if previous and stamp < previous[1]:
                return
            if kind == 'delta':
                if previous is None or category == 'spot':
                    return
                fields = previous[0] | fields
            self.rows[category][symbol] = (fields, stamp, receipt_ms)

    def snapshot_inputs(self, now_ms):
        markets, sources = {}, {}
        with self.lock:
            for category, expected in self.symbols.items():
                rows = self.rows[category]
                reason = self.states[category]
                if reason is None:
                    reason = self._unavailable_reason(category, expected, rows, now_ms)
                exchange_times = {symbol: row[1] for symbol, row in rows.items()}
                oldest = min(exchange_times.values(), default=None)
                receipt = min((row[2] for row in rows.values()), default=None)
                sources[category] = {
                    'status': 'unavailable' if reason else 'available',
                    'exchange_time_ms': oldest,
                    'symbol_exchange_time_ms': exchange_times,
                    'received_at': datetime.fromtimestamp(receipt / 1000, timezone.utc).isoformat() if receipt is not None else None,
                    'reason': reason,
                    'transport': 'websocket',
                }
                if reason is None:
                    markets[category] = {symbol:row[0].copy() for symbol,row in rows.items()}
        return markets, sources

    def _unavailable_reason(self, category, expected, rows, now_ms):
        if rows.keys() != expected:
            return 'stream_snapshot_incomplete'
        for ticker, stamp, receipt in rows.values():
            if now_ms - min(stamp, receipt) > MAX_TICK_AGE_MS:
                return 'stream_stale'
            if (number(ticker.get(TICKER_FIELDS[category][0])) or 0) <= 0:
                return 'stream_price_invalid'
            if category == 'linear' and (number(ticker.get('fundingRate')) is None
                    or (timestamp_ms(ticker.get('nextFundingTime')) or 0) <= now_ms):
                return 'stream_funding_unavailable'
        return None

    def _maintain(self, category):
        delay = 1
        while True:
            self._reset(category, 'stream_connecting')
            connected_at = None
            connection = None
            try:
                connection = websocket.create_connection(
                    STREAM_URL + category, timeout=6, sslopt={'context':self.tls_context}
                )
                connected_at = monotonic()
                LOG.info('Bybit %s stream connected', category)
                with self.lock:
                    self.states[category] = None
                self._receive(category, connection)
            except (OSError, ValueError, websocket.WebSocketException) as error:
                if connected_at is not None and monotonic() - connected_at >= PING_INTERVAL:
                    delay = 1
                self._reset(category, 'stream_disconnected')
                LOG.warning('Bybit %s stream disconnected (%s); retry in %ss', category, str(error)[:160], delay)
            finally:
                if connection is not None:
                    connection.shutdown()
            sleep(delay)
            delay = min(30, delay * 2)

    def _receive(self, category, connection):
        connection.settimeout(1)
        connection.send(json.dumps({'op':'subscribe', 'args':[f'tickers.{s}' for s in sorted(self.symbols[category])] }))
        next_ping = monotonic() + PING_INTERVAL
        pong_deadline = None
        while True:
            now = monotonic()
            if pong_deadline is not None and now >= pong_deadline:
                raise TimeoutError('heartbeat reply not received')
            if now >= next_ping:
                connection.send('{"op":"ping"}')
                next_ping = now + PING_INTERVAL
                pong_deadline = now + PONG_TIMEOUT
            try:
                payload = connection.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not payload:
                raise ConnectionError('connection closed')
            message = json.loads(payload)
            if not isinstance(message, dict):
                continue
            if message.get('op') == 'pong' or message.get('ret_msg') == 'pong':
                pong_deadline = None
            elif message.get('op') == 'subscribe' and message.get('success') is False:
                raise ConnectionError('ticker subscription rejected')
            else:
                self.handle_ticker(category, message, time_ns() // 1_000_000)
