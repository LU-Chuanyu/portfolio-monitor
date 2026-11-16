#!/usr/bin/env python3
"""Generate reproducible synthetic observations for an offline dashboard demo."""

import argparse
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from history import DAY, MINUTE_MS, evaluate_history, midnight, positions, utc_text, write_report


DEMO_END = date(2025, 12, 31)
DEMO_DAYS = 400
SETTLEMENT_MINUTES = 480
PRICE_STEP = Decimal('0.000001')
BASE_PRICES = {
    'BTCUSDT': Decimal('64000'),
    'ETHUSDT': Decimal('2300'),
    'SOLUSDT': Decimal('135'),
    'HYPEUSDT': Decimal('34'),
}
SEED_FILENAMES = {
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


def triangle(index, period):
    phase = index % period
    return min(phase, period - phase)


def synthetic_closes(day, index, base_price):
    sequence = (day - date(2024, 1, 1)).days
    trend = (triangle(sequence + 31 * index, 246) - 61) * 26
    variation = (triangle(sequence * 3 + 7 * index, 71) - 17) * 42
    spot = base_price * Decimal(10_000 + trend + variation) / 10_000
    basis = ((sequence * 19 + index * 23) % 121) - 60
    mark = spot * Decimal(100_000 + basis) / 100_000
    return spot.quantize(PRICE_STEP), mark.quantize(PRICE_STEP)


def synthetic_rate(day, index, settlement_index):
    sequence = (day - date(2024, 1, 1)).days
    regimes = (-105, 112, 24, 149, -18)
    regime = regimes[(sequence // 17) % len(regimes)]
    market_variation = (sequence * 13 + index * 37 + settlement_index * 19) % 99 - 49
    return Decimal(regime + market_variation + index * 9) / 1_000_000


def build_demo_generation(portfolio, *, end_date=DEMO_END, days=DEMO_DAYS):
    """Build one synthetic UTC ledger without calling a market-data service."""
    if type(days) is not int or days < 1:
        raise ValueError('Days must be a positive integer.')
    symbols = {symbol for _, symbol, _, _ in positions(portfolio)} - {'USDT'}
    unknown = symbols - BASE_PRICES.keys()
    if unknown:
        raise ValueError(f'Synthetic data is unavailable for {", ".join(sorted(unknown))}.')
    start_date = end_date - (days - 1) * DAY
    valuation_date = start_date - DAY
    intervals = {symbol: SETTLEMENT_MINUTES for symbol in BASE_PRICES}
    series = {name: {} for name in SERIES}

    for index, (symbol, base_price) in enumerate(BASE_PRICES.items()):
        daily_spot = {}
        daily_mark = {}
        rates = {}
        minute_marks = {}
        prior_mark = None
        for offset in range(days + 1):
            day = valuation_date + offset * DAY
            timestamp = midnight(day)
            spot, mark = synthetic_closes(day, index, base_price)
            daily_spot[timestamp] = spot
            daily_mark[timestamp] = mark
            if prior_mark is not None:
                for settlement_index in range(3):
                    at = timestamp + settlement_index * SETTLEMENT_MINUTES * MINUTE_MS
                    rates[at] = synthetic_rate(day, index, settlement_index)
                    minute = prior_mark + (mark - prior_mark) * settlement_index / 3
                    minute_marks[at - MINUTE_MS] = minute.quantize(PRICE_STEP)
            prior_mark = mark
        series['spot_daily'][symbol] = daily_spot
        series['mark_daily'][symbol] = daily_mark
        series['funding'][symbol] = rates
        series['minute_marks'][symbol] = minute_marks

    return build_generation(
        portfolio, start_date, end_date, {'interval_minutes': intervals, **series},
        data_origin='synthetic',
    )


def build_generation(portfolio, start_date, end_date, observations, *, data_origin='bybit'):
    """Package timestamp-keyed valuation observations into compatible history seeds."""
    intervals = observations['interval_minutes']
    series = {name: observations[name] for name in SERIES}
    report = evaluate_history(portfolio, start_date, end_date, interval_minutes=intervals, **series)
    if report['status'] != 'available':
        detail = ', '.join(report['issues'][:3]) or 'History observations are incomplete.'
        raise ValueError(detail)
    report['data_origin'] = data_origin
    if data_origin == 'synthetic':
        report['generated_at'] = utc_text(midnight(end_date + DAY))
        report['methodology'] = {
            **report['methodology'],
            'funding_coverage': 'Synthetic settlements occur at 00:00, 08:00, and 16:00 UTC.',
            'sources': {'synthetic': 'Deterministic demonstration values from scripts/generate_demo_data.py'},
        }
    sources = report['methodology']['sources']

    observations = {
        'window': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat(), 'timezone': 'UTC'},
        'portfolio_sha256': report['portfolio_sha256'],
        'timestamp_unit': 'Unix milliseconds',
        'data_origin': data_origin,
        'sources': sources,
        'interval_minutes': intervals,
        **{
            name: {symbol: {str(timestamp): str(value) for timestamp, value in sorted(values.items())}
                   for symbol, values in symbols.items()}
            for name, symbols in series.items()
        },
    }

    archive_symbols = {}
    for name, public_name in ARCHIVE_SERIES.items():
        for symbol, values in observations[name].items():
            archive_symbols.setdefault(symbol, {})[public_name] = {
                'first_timestamp': utc_text(int(next(iter(values)))) if values else None,
                'last_timestamp': utc_text(int(next(reversed(values)))) if values else None,
                'count': len(values),
                'values': dict(values),
            }
    archive = {
        'through_utc_day': end_date.isoformat(),
        'timestamp_unit': 'Unix milliseconds',
        'daily_timestamp': 'Opening time of the UTC candle. The stored price is its close.',
        'data_origin': data_origin,
        'sources': sources,
        'symbols': archive_symbols,
    }
    return {'report': report, 'observations': observations, 'archive': archive}


def write_seed_files(output_dir, generation):
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    for name, filename in SEED_FILENAMES.items():
        write_report(destination / filename, generation[name])


write_demo_seeds = write_seed_files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--portfolio', type=Path, default=ROOT / 'examples' / 'portfolio.json')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'data' / 'demo')
    parser.add_argument('--end', type=date.fromisoformat, default=DEMO_END, help='Final closed UTC date')
    parser.add_argument('--days', type=int, default=DEMO_DAYS)
    args = parser.parse_args(argv)
    try:
        portfolio = json.loads(args.portfolio.read_text(encoding='utf-8'))
        generation = build_demo_generation(portfolio, end_date=args.end, days=args.days)
        write_seed_files(args.output_dir, generation)
    except (OSError, ValueError, ArithmeticError, KeyError, TypeError) as error:
        parser.error(str(error))
    window = generation['report']['window']
    print(f"Wrote {window['complete_days']} synthetic UTC days through {window['end_date']} to {args.output_dir}")


if __name__ == '__main__':
    main()
