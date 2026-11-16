"""Calculate the mock portfolio's valuations from one set of market inputs."""

import hashlib
import json
import math


def portfolio_fingerprint(portfolio):
    content = json.dumps(portfolio, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(content.encode()).hexdigest()


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def timestamp_ms(value):
    if type(value) is not int:
        if not isinstance(value, str) or not value.isascii() or not value.isdigit():
            return None
        try:
            value = int(value)
        except ValueError:
            return None
    return value if 0 < value <= 8_640_000_000_000_000 else None


def sum_if_complete(values):
    values = list(values)
    return sum(values) if None not in values else None


def calculate_portfolio(portfolio, markets, errors=()):
    positions = portfolio['positions']
    # The account owns the spot inventory and USDT cash without leverage.
    # The perpetual contracts have zero initial value.
    starting = sum(
        position['quantity'] * position['entry_price_usdt']
        for position in positions
        if position['instrument'] == 'SPOT'
    )
    rows, issues = [], list(errors)
    for position in positions:
        is_spot = position['instrument'] == 'SPOT'
        is_cash = is_spot and position['symbol'] == 'USDT'
        category = 'spot' if is_spot else 'linear'
        ticker = markets.get(category, {}).get(position['symbol'], {})
        price_field = 'lastPrice' if is_spot else 'markPrice'
        price = 1.0 if is_cash else number(ticker.get(price_field))
        if price is not None and price <= 0:
            price = None
        rate = None if is_spot else number(ticker.get('fundingRate'))
        next_time = None if is_spot else timestamp_ms(ticker.get('nextFundingTime'))
        name = f"{position['symbol']} {position['instrument']}"
        if price is None:
            issues.append(f'{name}: current price unavailable')
        if not is_spot and rate is None:
            issues.append(f'{name}: funding rate unavailable')
        if not is_spot and next_time is None:
            issues.append(f'{name}: next funding time unavailable')

        quantity = position['quantity']
        sign = 1 if position['side'] == 'LONG' else -1
        value = quantity * price if price is not None else None
        pnl = (price - position['entry_price_usdt']) * quantity * sign if value is not None else None
        # A positive rate pays shorts and charges longs.
        funding = -sign * value * rate if value is not None and rate is not None else None
        rows.append({
            **position,
            'current_price': price, 'value': value, 'pnl': pnl,
            'funding_rate': rate, 'next_funding_time': next_time, 'expected_funding': funding,
        })

    pnl = sum_if_complete(row['pnl'] for row in rows)
    equity = starting + pnl if pnl is not None else None
    total_funding = sum_if_complete(
        row['expected_funding'] for row in rows if row['instrument'] == 'PERP'
    )
    assets = sorted({row['asset'] for row in rows if row['asset'] != 'USDT'})
    deltas = []
    for asset in assets:
        legs = [row for row in rows if row['asset'] == asset]
        signed_values = [
            row['value'] if row['side'] == 'LONG' or row['value'] is None else -row['value']
            for row in legs
        ]
        deltas.append({'asset': asset, 'delta': sum_if_complete(signed_values)})
    net_delta = sum_if_complete(row['delta'] for row in deltas)
    delta_ratio = net_delta / equity if net_delta is not None and equity not in (None, 0) else None
    return {
        'starting_equity': starting, 'equity': equity, 'unrealised_pnl': pnl,
        'expected_funding': total_funding, 'net_delta': net_delta, 'delta_ratio': delta_ratio,
        'positions': rows, 'asset_deltas': deltas, 'issues': issues,
    }
