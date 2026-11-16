"""Replay a newly opened hedge against the saved Bybit observations."""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from statistics import stdev

from history import DAY, DAY_MS, MINUTE_MS, midnight, numeric, utc_text


ASSETS = ('BTC', 'ETH', 'SOL', 'HYPE')
DEFAULTS = {
    'capital': '100000',
    'reserve_pct': '20',
    'entry_bps': '10',
    'exit_bps': '10',
    'slippage_bps': '0',
    **{asset.lower(): '25' for asset in ASSETS},
}


def backtest_bounds(observations):
    window = observations['window']
    return {'start': window['start_date'], 'end': window['end_date'], 'assets': list(ASSETS)}


def query_date(value, field):
    try:
        result = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f'{field} must be a UTC date in YYYY-MM-DD format.') from error
    if value != result.isoformat():
        raise ValueError(f'{field} must be a UTC date in YYYY-MM-DD format.')
    return result


def settings(query):
    allowed = DEFAULTS.keys() | {'start', 'end'}
    if query.keys() - allowed or any(len(values) != 1 for values in query.values()):
        raise ValueError('Supply each supported backtest input at most once.')
    values = {**DEFAULTS, **{key: items[0] for key, items in query.items()}}
    start = query_date(values.get('start', ''), 'Start')
    end = query_date(values.get('end', ''), 'End')
    if end < start:
        raise ValueError('End must be on or after start.')

    capital = numeric(values['capital'])
    reserve = numeric(values['reserve_pct'])
    entry_bps = numeric(values['entry_bps'])
    exit_bps = numeric(values['exit_bps'])
    slippage_bps = numeric(values['slippage_bps'])
    weights = {asset: numeric(values[asset.lower()]) for asset in ASSETS}
    if not 1 <= capital <= 1_000_000_000:
        raise ValueError('Capital must be between 1 and 1,000,000,000 USDT.')
    if not 0 <= reserve <= 100:
        raise ValueError('Cash reserve must be between 0% and 100%.')
    if not (0 <= entry_bps <= 1000 and 0 <= exit_bps <= 1000):
        raise ValueError('Entry and exit fees must each be between 0 and 1,000 basis points.')
    if not 0 <= slippage_bps <= 1000:
        raise ValueError('Slippage must be between 0 and 1,000 basis points per leg and trade.')
    if not all(0 <= weight <= 100 for weight in weights.values()) or sum(weights.values()) != 100:
        raise ValueError('Asset weights must be non-negative and sum to 100%.')
    return start, end, capital, reserve, entry_bps, exit_bps, slippage_bps, weights


def observation(data, series, symbol, timestamp):
    try:
        value = data[series][symbol][str(timestamp)]
    except KeyError as error:
        raise ValueError(f'Bybit data is missing for {symbol} at {utc_text(timestamp)} ({series}).') from error
    return numeric(value, positive=True)


def funding_for_window(data, symbol, quantity, start_ms, stop_ms):
    interval = data['interval_minutes'].get(symbol)
    if type(interval) is not int or interval <= 0 or 1440 % interval:
        raise ValueError(f'Funding cadence is unavailable for {symbol}.')
    events = {
        int(timestamp): rate for timestamp, rate in data['funding'].get(symbol, {}).items()
        if start_ms < int(timestamp) < stop_ms
    }
    cadence = interval * MINUTE_MS
    if any(timestamp not in events for timestamp in range(start_ms + cadence, stop_ms, cadence)):
        raise ValueError(f'Funding history is incomplete for {symbol} in the selected window.')

    daily = defaultdict(Decimal)
    for timestamp, rate in events.items():
        open_minute = timestamp - timestamp % MINUTE_MS - MINUTE_MS
        mark = observation(data, 'minute_marks', symbol, open_minute)
        day = timestamp - timestamp % DAY_MS
        daily[day] += quantity * mark * numeric(rate)
    return daily


def open_positions(data, start, capital, reserve, fee_rate, weights):
    if reserve == 100:
        return []
    timestamp = midnight(start - DAY)
    prices = {}
    for asset, weight in weights.items():
        if weight:
            symbol = asset + 'USDT'
            prices[asset] = (
                observation(data, 'spot_daily', symbol, timestamp),
                observation(data, 'mark_daily', symbol, timestamp),
            )

    # Make room for both entry fees while preserving the requested cash reserve.
    combined_notional_per_spot = sum(
        weights[asset] / 100 * (1 + mark / spot) for asset, (spot, mark) in prices.items()
    )
    spot_budget = capital * (1 - reserve / 100) / (1 + fee_rate * combined_notional_per_spot)
    entries = []
    for asset, (spot, mark) in prices.items():
        spot_notional = spot_budget * weights[asset] / 100
        quantity = spot_notional / spot
        perp_notional = quantity * mark
        entries.append({
            'asset': asset,
            'weight_pct': weights[asset],
            'quantity': quantity,
            'spot_price': spot,
            'perp_mark': mark,
            'spot_notional': spot_notional,
            'perp_notional': perp_notional,
            'entry_fees': (spot_notional + perp_notional) * fee_rate,
        })
    return entries


def simulate(query, data):
    """Run a replay without contacting Bybit or changing the saved positions."""
    start, end, capital, reserve, entry_bps, exit_bps, slippage_bps, weights = settings(query)
    first = date.fromisoformat(data['window']['start_date'])
    last = date.fromisoformat(data['window']['end_date'])
    if not first <= start <= end <= last:
        raise ValueError(f'Choose dates within {first} through {last} UTC.')

    start_ms, stop_ms = midnight(start), midnight(end + DAY)
    entries = open_positions(data, start, capital, reserve, entry_bps / 10_000, weights)
    entry_fees = sum((row['entry_fees'] for row in entries), Decimal(0))
    spot_cost = sum((row['spot_notional'] for row in entries), Decimal(0))
    entry_notional = spot_cost + sum(row['perp_notional'] for row in entries)
    slippage_rate = slippage_bps / 10_000
    entry_slippage = entry_notional * slippage_rate
    post_entry_cash = capital * reserve / 100 - entry_slippage
    daily_funding = defaultdict(Decimal)
    for row in entries:
        flows = funding_for_window(data, row['asset'] + 'USDT', row['quantity'], start_ms, stop_ms)
        for day, flow in flows.items():
            daily_funding[day] += flow

    cumulative_funding = Decimal(0)
    previous_equity = peak = capital
    max_drawdown = Decimal(0)
    exit_fees = exit_slippage = exit_notional = Decimal(0)
    daily_returns, series, issues = [], [], []
    complete_days = (end - start).days + 1
    equity_positive = True
    for offset in range(complete_days):
        day = start + offset * DAY
        day_ms = midnight(day)
        day_funding = daily_funding[day_ms]
        cumulative_funding += day_funding
        market_pnl = close_notional = Decimal(0)
        for row in entries:
            symbol = row['asset'] + 'USDT'
            spot_close = observation(data, 'spot_daily', symbol, day_ms)
            perp_close = observation(data, 'mark_daily', symbol, day_ms)
            market_pnl += row['quantity'] * (spot_close - row['spot_price'] + row['perp_mark'] - perp_close)
            close_notional += row['quantity'] * (spot_close + perp_close)
        cash_before_exit = post_entry_cash + cumulative_funding
        equity = cash_before_exit + spot_cost + market_pnl
        if day == end:
            exit_notional = close_notional
            exit_fees = exit_notional * exit_bps / 10_000
            exit_slippage = exit_notional * slippage_rate
            equity -= exit_fees + exit_slippage

        peak = max(peak, equity)
        drawdown = equity / peak - 1
        max_drawdown = max(max_drawdown, -drawdown)
        equity_positive = equity_positive and equity > 0
        daily_return = float(equity / previous_equity - 1) if equity_positive else None
        daily_returns.append(daily_return)
        series.append({
            'date': day.isoformat(),
            'as_of': utc_text(midnight(day + DAY)),
            'equity': float(equity),
            'cash': float(equity if day == end else cash_before_exit),
            'cash_before_exit': float(cash_before_exit),
            'daily_funding': float(day_funding),
            'cumulative_funding': float(cumulative_funding),
            'mark_to_market_pnl': float(market_pnl),
            'entry_slippage': float(entry_slippage) if offset == 0 else 0.0,
            'exit_fees': float(exit_fees),
            'exit_slippage': float(exit_slippage),
            'daily_return': daily_return,
            'drawdown': float(drawdown),
        })
        previous_equity = equity

    if not equity_positive:
        issues.append('Non-positive simulated equity invalidates return and volatility metrics.')
    if post_entry_cash < 0:
        issues.append('Modeled entry slippage exceeds the opening cash reserve.')
    turnover_notional = entry_notional + exit_notional
    total_slippage = entry_slippage + exit_slippage
    net_pnl = previous_equity - capital
    pre_slippage_pnl = net_pnl + total_slippage
    break_even_slippage = (
        pre_slippage_pnl * 10_000 / turnover_notional
        if turnover_notional and pre_slippage_pnl >= 0 else None
    )
    return {
        'status': 'available',
        'window': {'start_date': start.isoformat(), 'end_date': end.isoformat(), 'complete_days': complete_days},
        'inputs': {
            'capital': float(capital), 'reserve_pct': float(reserve),
            'entry_bps': float(entry_bps), 'exit_bps': float(exit_bps), 'slippage_bps': float(slippage_bps),
            'weights': {asset: float(value) for asset, value in weights.items()},
        },
        'baseline': {
            'date': start.isoformat(), 'as_of': utc_text(start_ms), 'equity': float(capital),
            'mark_to_market_pnl': 0.0, 'cumulative_funding': 0.0, 'cash': float(capital),
        },
        'initial_cash': float(post_entry_cash),
        'series': series,
        'metrics': {
            'starting_equity': float(capital), 'ending_equity': float(previous_equity),
            'post_entry_equity': float(capital - entry_fees - entry_slippage),
            'post_entry_cash': float(post_entry_cash),
            'total_return': float(previous_equity / capital - 1) if equity_positive else None,
            'max_drawdown': float(max_drawdown),
            'daily_volatility': stdev(daily_returns) if equity_positive and complete_days > 1 else None,
            'total_funding': float(cumulative_funding),
            'entry_fees': float(entry_fees), 'exit_fees': float(exit_fees),
            'total_fees': float(entry_fees + exit_fees),
            'entry_slippage': float(entry_slippage), 'exit_slippage': float(exit_slippage),
            'total_slippage': float(total_slippage), 'turnover_notional': float(turnover_notional),
            'pre_slippage_pnl': float(pre_slippage_pnl),
            'break_even_slippage_bps': float(break_even_slippage) if break_even_slippage is not None else None,
            'net_pnl': float(net_pnl),
        },
        'entries': [{key: float(value) if isinstance(value, Decimal) else value for key, value in row.items()}
                    for row in entries],
        'methodology': {
            'entry': 'Open after the selected start at 00:00 UTC using the preceding day’s completed spot and perpetual mark closes. Match quantities for the long and short legs.',
            'funding': 'Use actual settled funding rates and the preceding completed minute mark. Exclude the opening and closing boundary settlements.',
            'cash': 'Entry slippage reduces the modeled cash reserve. While positions are open, cash includes cumulative funding. The final point shows all cash after exit costs.',
            'costs': 'Fees and assumed slippage apply to both notionals at entry and exit. Slippage is a cash charge on fixed quantities valued at spot and mark closes. Margin constraints, borrowing, and liquidation are outside the model.',
            'risk': 'Drawdown uses starting capital and UTC daily closes. Returns include fees and assumed slippage. Volatility is the sample standard deviation of daily returns.',
        },
        'issues': issues,
    }
