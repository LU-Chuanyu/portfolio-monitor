#!/usr/bin/env python3
"""Evaluate a fixed model portfolio using Bybit's public historical observations."""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
import json
import os
from pathlib import Path
from statistics import stdev
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bybit_public import BASE_URL, TLS_CONTEXT as TLS
from portfolio_metrics import portfolio_fingerprint


ROOT = Path(__file__).resolve().parent
DAY = timedelta(days=1)
MINUTE_MS = 60_000
DAY_MS = 1_440 * MINUTE_MS
UTC = timezone.utc
METHODOLOGY = {
    "kind": "fixed_position_historical_simulation",
    "currency": "USDT",
    "valuation": "UTC daily spot close and perpetual mark close. Original quantities and entry prices are frozen. Cash is valued at 1 USDT.",
    "baseline": "Equity at the preceding UTC daily close, with cumulative simulated funding initialized to zero. The historical return starts from this equity.",
    "funding": "Signed cash flow = negative signed perpetual quantity × previous completed one-minute mark close × published funding rate. Positive cash flow is received. Events are grouped by their published UTC timestamp.",
    "funding_coverage": "Every currently reported cadence anchor is checked against observed historical events, and additional published events are included. The current cadence is a conservative screening rule, not independent proof of historical coverage.",
    "cost_scope": "Pre-cost simulation. Excludes trading fees, slippage, borrowing, interest, margin constraints, liquidations, and tax. Assumes unchanged holdings and no external flows.",
    "risk_metrics": "Return and drawdown use the baseline and daily closes. Max drawdown is a positive peak-to-trough magnitude. Daily volatility is the sample standard deviation of simple daily returns, unannualized.",
    "sources": {
        "spot_daily_close": "https://bybit-exchange.github.io/docs/v5/market/kline",
        "perpetual_marks": "https://bybit-exchange.github.io/docs/v5/market/mark-kline",
        "settled_funding_rates": "https://bybit-exchange.github.io/docs/v5/market/history-fund-rate",
        "current_funding_interval": "https://bybit-exchange.github.io/docs/v5/market/instrument",
        "funding_formula": "https://www.bybit.com/en/help-center/article/Funding-fee-calculation",
    },
}


def midnight(day: date) -> int:
    return int(datetime.combine(day, time(), UTC).timestamp()) * 1000


def utc_text(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp / 1000, UTC).isoformat().replace("+00:00", "Z")


def numeric(value, *, positive=False) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"Invalid numeric observation: {value!r}") from error
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError(f"Invalid numeric observation: {value!r}")
    return number


def positions(portfolio: dict) -> list[tuple[str, str, Decimal, Decimal]]:
    result = []
    for row in portfolio["positions"]:
        instrument, symbol, side = row["instrument"], row["symbol"], row["side"]
        if (
            instrument not in {"SPOT", "PERP"}
            or side not in {"LONG", "SHORT"}
            or (instrument == "SPOT" and side != "LONG")
            or (symbol == "USDT" and instrument != "SPOT")
            or (symbol != "USDT" and not symbol.endswith("USDT"))
        ):
            raise ValueError(f"Unsupported position: {symbol} {instrument} {side}")
        quantity = numeric(row["quantity"], positive=True)
        entry = numeric(row["entry_price_usdt"], positive=True)
        result.append((instrument, symbol, quantity if side == "LONG" else -quantity, entry))
    if not result:
        raise ValueError("Expected at least one position")
    return result


def evaluate_history(
    portfolio: dict,
    start: date,
    end: date,
    *,
    spot_daily: dict,
    mark_daily: dict,
    funding: dict,
    minute_marks: dict,
    interval_minutes: dict,
) -> dict:
    """Evaluate an inclusive range from timestamp-ms to price/rate mappings."""
    if end < start:
        raise ValueError("End date must be on or after start date")
    holdings = positions(portfolio)
    start_ms, stop_ms = midnight(start), midnight(end + DAY)
    dates = [start + offset * DAY for offset in range((end - start).days + 1)]
    capital = sum(quantity * entry for kind, _, quantity, entry in holdings if kind == "SPOT")
    initial_cash = sum(quantity for _, symbol, quantity, _ in holdings if symbol == "USDT")
    derivatives = defaultdict(Decimal)
    for kind, symbol, quantity, _ in holdings:
        if kind == "PERP":
            derivatives[symbol] += quantity

    issues = []
    incomplete = set()
    daily_funding = defaultdict(Decimal)
    funding_coverage = {}
    for symbol, quantity in sorted(derivatives.items()):
        interval = interval_minutes.get(symbol)
        if not isinstance(interval, int) or interval <= 0 or 1440 % interval != 0:
            issues.append(f"{symbol}: funding cadence unavailable")
            incomplete.update(midnight(day) for day in dates)
            continue
        events = {
            stamp: rate for stamp, rate in funding.get(symbol, {}).items()
            if start_ms <= stamp < stop_ms
        }
        cadence = interval * MINUTE_MS
        expected = set(range(start_ms, stop_ms, cadence))
        missing = expected - events.keys()
        incomplete.update(stamp - stamp % DAY_MS for stamp in missing)
        if missing:
            issues.append(
                f"{symbol}: {len(missing)} funding cadence anchor(s) unverified "
                f"from {utc_text(min(missing))}"
            )
        valued = 0
        for stamp, rate in sorted(events.items()):
            day = stamp - stamp % DAY_MS
            candle_start = stamp - stamp % MINUTE_MS - MINUTE_MS
            mark = minute_marks.get(symbol, {}).get(candle_start)
            if mark is None:
                incomplete.add(day)
                issues.append(f"{symbol}: completed minute mark unavailable at {utc_text(stamp)}")
                continue
            daily_funding[day] -= quantity * numeric(mark, positive=True) * numeric(rate)
            valued += 1
        funding_coverage[symbol] = {
            "reported_interval_minutes": interval,
            "expected_anchors": len(expected),
            "covered_anchors": len(expected) - len(missing),
            "observed_events": len(events),
            "valued_events": valued,
        }

    def valuation(day: date) -> Decimal | None:
        stamp = midnight(day)
        pnl = Decimal(0)
        missing = []
        for kind, symbol, quantity, entry in holdings:
            if symbol == "USDT":
                price = 1
            else:
                data = spot_daily if kind == "SPOT" else mark_daily
                price = data.get(symbol, {}).get(stamp)
            if price is None:
                missing.append(f"{symbol} {kind}")
            else:
                pnl += quantity * (numeric(price, positive=True) - entry)
        if missing:
            issues.append(f"{day.isoformat()}: daily close unavailable for {', '.join(missing)}")
            incomplete.add(stamp)
            return None
        return pnl

    baseline_pnl = valuation(start - DAY)
    pnl_by_day = {day: valuation(day) for day in dates}
    complete_days = sum(midnight(day) not in incomplete for day in dates)
    result = {
        "status": "unavailable",
        "account_name": portfolio["account_name"],
        "portfolio_sha256": portfolio_fingerprint(portfolio),
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "window": {"start_date": start.isoformat(), "end_date": end.isoformat(), "complete_days": complete_days},
        "initial_cash": float(initial_cash),
        "coverage": {"requested_days": len(dates), "baseline_complete": baseline_pnl is not None,
                     "funding": funding_coverage},
        "methodology": METHODOLOGY,
        "baseline": None,
        "series": [],
        "metrics": None,
        "issues": issues,
    }
    if baseline_pnl is None or complete_days != len(dates):
        return result

    baseline = capital + baseline_pnl
    result["baseline"] = {
        "as_of": utc_text(start_ms), "equity": float(baseline),
        "mark_to_market_pnl": float(baseline_pnl), "cumulative_funding": 0.0,
    }
    prior_equity = peak = baseline
    cumulative_funding = Decimal(0)
    returns = []
    drawdowns = []
    series = []
    equity_positive = baseline > 0
    for day, pnl in pnl_by_day.items():
        cash_flow = daily_funding[midnight(day)]
        cumulative_funding += cash_flow
        equity = capital + pnl + cumulative_funding
        peak = max(peak, equity)
        equity_positive = equity_positive and equity > 0
        daily_return = float(equity / prior_equity - 1) if equity_positive else None
        drawdown = float(equity / peak - 1) if peak > 0 else None
        returns.append(daily_return)
        drawdowns.append(drawdown)
        series.append({
            "date": day.isoformat(), "as_of": utc_text(midnight(day + DAY)),
            "equity": float(equity), "mark_to_market_pnl": float(pnl),
            "daily_funding": float(cash_flow), "cumulative_funding": float(cumulative_funding),
            "daily_return": daily_return, "drawdown": drawdown,
        })
        prior_equity = equity

    if not equity_positive:
        issues.append("Non-positive simulated equity invalidates return and volatility metrics.")
    result["status"] = "available"
    result["series"] = series
    result["metrics"] = {
        "starting_equity": float(baseline),
        "ending_equity": series[-1]["equity"],
        "total_return": float(prior_equity / baseline - 1) if equity_positive else None,
        "max_drawdown": -min(drawdowns) if baseline > 0 else None,
        "daily_volatility": stdev(returns) if equity_positive and len(returns) >= 2 else None,
        "total_funding": float(cumulative_funding),
    }
    return result


def bybit(path: str, **query) -> list:
    url = BASE_URL + "/v5/market/" + path + "?" + urlencode(query)
    request = Request(url, headers={"User-Agent": "CMCC-portfolio-history/1.0"})
    try:
        with urlopen(request, timeout=12, context=TLS) as response:
            payload = json.load(response)
    except HTTPException as error:
        raise OSError(f"Bybit {path} {query['symbol']} response could not be read") from error
    result = payload.get("result") if isinstance(payload, dict) else None
    if (not isinstance(result, dict) or payload.get("retCode") != 0
            or not isinstance(result.get("list"), list)):
        raise ValueError(f"Bybit {path} {query['symbol']} returned an error")
    return result["list"]


def unique_values(pairs) -> dict:
    result = {}
    for stamp, value in pairs:
        if stamp in result and result[stamp] != value:
            raise ValueError(f"Conflicting observations at {utc_text(stamp)}")
        result[stamp] = value
    return result


def closes(path: str, category: str, symbol: str, interval: str, start: int, stop: int) -> dict:
    data = bybit(path, category=category, symbol=symbol, interval=interval,
                 start=start, end=stop - 1, limit=1000)
    return unique_values(
        (int(row[0]), numeric(row[4], positive=True))
        for row in data if start <= int(row[0]) < stop
    )


def settlement_rates(symbol: str, start: int, stop: int) -> dict:
    pairs = []
    last = stop - 1
    while last >= start:
        batch = bybit("funding/history", category="linear", symbol=symbol,
                      startTime=start, endTime=last, limit=200)
        if not batch:
            break
        page = [
            (int(row["fundingRateTimestamp"]), numeric(row["fundingRate"]))
            for row in batch if row["symbol"] == symbol
        ]
        if not page or any(not start <= stamp <= last for stamp, _ in page):
            raise ValueError(f"Unexpected funding history page for {symbol}")
        pairs.extend(page)
        if len(batch) < 200:
            break
        last = min(stamp for stamp, _ in page) - 1
    return unique_values(pairs)


def minute_windows(candle_starts):
    """Batch required candle starts into ranges with at most 1000 one-minute bars."""
    pending = sorted(set(candle_starts))
    index = 0
    while index < len(pending):
        begin = pending[index]
        next_index = index + 1
        while next_index < len(pending) and pending[next_index] < begin + 1000 * MINUTE_MS:
            next_index += 1
        yield begin, pending[next_index - 1] + MINUTE_MS
        index = next_index


def collect_history(portfolio: dict, start: date, end: date) -> dict:
    holdings = positions(portfolio)
    first_close, begin, stop = midnight(start - DAY), midnight(start), midnight(end + DAY)
    spot_symbols = sorted({symbol for kind, symbol, _, _ in holdings if kind == "SPOT" and symbol != "USDT"})
    perp_symbols = sorted({symbol for kind, symbol, _, _ in holdings if kind == "PERP"})
    with ThreadPoolExecutor(max_workers=8) as pool:
        spot = {
            symbol: pool.submit(closes, "kline", "spot", symbol, "D", first_close, stop)
            for symbol in spot_symbols
        }
        marks = {
            symbol: pool.submit(closes, "mark-price-kline", "linear", symbol, "D", first_close, stop)
            for symbol in perp_symbols
        }
        rates = {
            symbol: pool.submit(settlement_rates, symbol, begin, stop)
            for symbol in perp_symbols
        }
        instruments = {
            symbol: pool.submit(bybit, "instruments-info", category="linear", symbol=symbol)
            for symbol in perp_symbols
        }
        funding = {symbol: future.result() for symbol, future in rates.items()}
        intervals = {}
        for symbol, future in instruments.items():
            for row in future.result():
                if row["symbol"] == symbol and row["contractType"] == "LinearPerpetual" and row["settleCoin"] == "USDT":
                    intervals[symbol] = int(row["fundingInterval"])

        requested_marks = {
            symbol: {stamp - stamp % MINUTE_MS - MINUTE_MS for stamp in rows}
            for symbol, rows in funding.items()
        }
        mark_downloads = {
            symbol: [pool.submit(closes, "mark-price-kline", "linear", symbol, "1", start_at, stop_at)
                     for start_at, stop_at in minute_windows(requested)]
            for symbol, requested in requested_marks.items()
        }
        minute_marks = {
            symbol: {stamp: price for future in downloads for stamp, price in future.result().items()
                     if stamp in requested_marks[symbol]}
            for symbol, downloads in mark_downloads.items()
        }
        return evaluate_history(
            portfolio, start, end,
            spot_daily={symbol: future.result() for symbol, future in spot.items()},
            mark_daily={symbol: future.result() for symbol, future in marks.items()},
            funding=funding,
            minute_marks=minute_marks,
            interval_minutes=intervals,
        )


def write_report(output: Path, report: dict) -> None:
    payload = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(payload)
            file.close()
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portfolio", type=Path,
                        default=Path(os.environ.get("CMCC_PORTFOLIO_PATH", ROOT / "examples" / "portfolio.json")))
    parser.add_argument("--days", type=int, default=30, help="Number of closed UTC days")
    parser.add_argument("--end", type=date.fromisoformat, help="Last closed day, YYYY-MM-DD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    today = datetime.now(UTC).date()
    end = args.end or today - DAY
    if end >= today:
        parser.error("--end must identify a completed UTC day")
    if not 1 <= args.days <= 90:
        parser.error("--days must be between 1 and 90")
    start = end - (args.days - 1) * DAY
    try:
        portfolio = json.loads(args.portfolio.read_text(encoding="utf-8"))
        report = collect_history(portfolio, start, end)
        write_report(args.output, report)
    except (OSError, KeyError, IndexError, TypeError, ValueError) as error:
        parser.error(str(error))
    print(f"{report['status']}: {report['window']['complete_days']}/{args.days} complete days → {args.output}")
    return 0 if report["status"] == "available" else 2


if __name__ == "__main__":
    raise SystemExit(main())
