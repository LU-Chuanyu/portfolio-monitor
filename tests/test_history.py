import math
import statistics
import unittest
from datetime import date, datetime, timezone

import history


def timestamp(day, hour=0, minute=0, second=0):
    return int(datetime(2026, 1, day, hour, minute, second, tzinfo=timezone.utc).timestamp() * 1000)


SYMBOL = "BTCUSDT"
PORTFOLIO = {
    "account_name": "Fixture",
    "positions": [
        {"symbol": "USDT", "instrument": "SPOT", "side": "LONG", "quantity": 100,
         "entry_price_usdt": 1},
        {"symbol": SYMBOL, "instrument": "SPOT", "side": "LONG", "quantity": 2,
         "entry_price_usdt": 100},
        {"symbol": SYMBOL, "instrument": "PERP", "side": "SHORT", "quantity": 2,
         "entry_price_usdt": 100},
    ],
}


def sources():
    return {
        "spot_daily": {SYMBOL: {
            timestamp(1): 105, timestamp(2): 115, timestamp(3): 113,
        }},
        "mark_daily": {SYMBOL: {
            timestamp(1): 106, timestamp(2): 116, timestamp(3): 120,
        }},
        "funding": {SYMBOL: {timestamp(2): 0.01, timestamp(3): -0.02}},
        "minute_marks": {SYMBOL: {
            timestamp(1, 23, 59): 110, timestamp(2): 999,
            timestamp(2, 23, 59): 125, timestamp(3): 999,
        }},
        "interval_minutes": {SYMBOL: 1440},
    }


class HistoryTest(unittest.TestCase):
    def calculate(self, data, portfolio=PORTFOLIO):
        return history.evaluate_history(portfolio, date(2026, 1, 2), date(2026, 1, 3), **data)

    def test_fixed_portfolio_pnl_funding_and_performance(self):
        result = self.calculate(sources())
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["window"]["complete_days"], 2)
        self.assertEqual(result["baseline"]["equity"], 298)
        first, second = result["series"]
        self.assertEqual(first["date"], "2026-01-02")
        self.assertEqual(first["daily_funding"], 2.2)
        self.assertEqual(first["mark_to_market_pnl"], -2)
        self.assertEqual(first["equity"], 300.2)
        self.assertEqual(second["daily_funding"], -5)
        self.assertEqual(second["cumulative_funding"], -2.8)
        self.assertEqual(second["equity"], 283.2)
        metrics = result["metrics"]
        self.assertAlmostEqual(metrics["total_return"], 283.2 / 298 - 1)
        self.assertAlmostEqual(metrics["max_drawdown"], 1 - 283.2 / 300.2)
        self.assertAlmostEqual(
            metrics["daily_volatility"], statistics.stdev([300.2 / 298 - 1, 283.2 / 300.2 - 1]),
        )

    def test_long_pays_positive_funding_and_receives_negative(self):
        positions = [{**position} for position in PORTFOLIO["positions"]]
        positions[-1]["side"] = "LONG"
        first, second = self.calculate(sources(), {**PORTFOLIO, "positions": positions})["series"]
        self.assertEqual(first["daily_funding"], -2.2)
        self.assertEqual(second["daily_funding"], 5)

    def test_missing_mark_does_not_use_future_or_stale_candle(self):
        data = sources()
        data["minute_marks"][SYMBOL].pop(timestamp(1, 23, 59))
        data["minute_marks"][SYMBOL][timestamp(1, 23, 58)] = 100
        result = self.calculate(data)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["series"], [])
        self.assertIsNone(result["metrics"])
        self.assertTrue(any("minute mark" in issue for issue in result["issues"]))

    def test_missing_funding_or_daily_close_blocks_full_window(self):
        for field, omitted in (("funding", timestamp(3)), ("spot_daily", timestamp(2))):
            with self.subTest(field=field):
                data = sources()
                data[field][SYMBOL].pop(omitted)
                result = self.calculate(data)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["window"]["complete_days"], 1)
                self.assertIsNone(result["metrics"])

    def test_unscheduled_published_event_is_included_without_boundary_lookahead(self):
        data = sources()
        intraday = timestamp(2, 8, second=10)
        data["funding"][SYMBOL][intraday] = 0.001
        data["minute_marks"][SYMBOL][timestamp(2, 7, 59)] = 115
        data["minute_marks"][SYMBOL][timestamp(2, 8)] = 999
        data["funding"][SYMBOL][timestamp(4)] = 9
        result = self.calculate(data)
        self.assertEqual(result["status"], "available")
        self.assertTrue(math.isclose(result["series"][0]["daily_funding"], 2.43))
        self.assertEqual(result["series"][1]["daily_funding"], -5)


if __name__ == "__main__":
    unittest.main()
