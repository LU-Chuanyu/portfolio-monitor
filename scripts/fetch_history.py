#!/usr/bin/env python3
"""Seed a local history directory from Bybit's public market data."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from history import DAY
from history_backfill import fetch_missing_days
from generate_demo_data import build_generation, write_seed_files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--portfolio', type=Path,
                        default=Path(os.environ.get('CMCC_PORTFOLIO_PATH', ROOT / 'examples' / 'portfolio.json')))
    parser.add_argument('--days', type=int, default=30, choices=range(1, 31), metavar='1–30')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'data' / 'live')
    args = parser.parse_args()
    end = datetime.now(timezone.utc).date() - DAY
    start = end - (args.days - 1) * DAY
    portfolio = json.loads(args.portfolio.expanduser().read_text(encoding='utf-8'))
    print(f'Fetching Bybit observations for {start} to {end} UTC', flush=True)
    observations = fetch_missing_days(portfolio, start - DAY, end)
    generation = build_generation(portfolio, start, end, observations)
    write_seed_files(args.output_dir, generation)
    print(f'Wrote {args.days} complete UTC days to {args.output_dir}')


if __name__ == '__main__':
    main()
