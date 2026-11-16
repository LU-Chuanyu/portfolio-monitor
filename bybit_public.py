"""Connection defaults for the public Bybit market data endpoints."""

from datetime import datetime, timezone
import ssl

import certifi


BASE_URL = 'https://api.bybit.com'
TICKERS_URL = f'{BASE_URL}/v5/market/tickers?category='
TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def now_utc():
    return datetime.now(timezone.utc).isoformat()
