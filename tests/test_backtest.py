"""Validate the replay against hand-calculated trades and synthetic history."""

from copy import deepcopy
from datetime import date
import unittest

from backtest import backtest_bounds, simulate
from demo_fixtures import demo_generation
from history import DAY_MS, MINUTE_MS, midnight


def query(**fields):
    return {key: [str(value)] for key, value in fields.items()}


START = midnight(date(2026, 1, 2))
NOON = DAY_MS // 2
BTC = 'BTCUSDT'
SIMULATED = {
    'window': {'start_date': '2026-01-02', 'end_date': '2026-01-03'},
    'interval_minutes': {BTC: 720},
    'spot_daily': {BTC: {str(START - DAY_MS): '100', str(START): '102', str(START + DAY_MS): '104'}},
    'mark_daily': {BTC: {str(START - DAY_MS): '100', str(START): '101', str(START + DAY_MS): '106'}},
    'funding': {BTC: {
        str(START): '0.9', str(START + NOON): '0.01', str(START + DAY_MS): '-0.01',
        str(START + DAY_MS + NOON): '0.02', str(START + 2 * DAY_MS): '0.9',
    }},
    'minute_marks': {BTC: {
        str(START + NOON - MINUTE_MS): '110',
        str(START + DAY_MS - MINUTE_MS): '101',
        str(START + DAY_MS + NOON - MINUTE_MS): '104',
    }},
}
INPUT = query(start='2026-01-02', end='2026-01-03', capital=1020, reserve_pct=50,
              entry_bps=100, exit_bps=100, btc=100, eth=0, sol=0, hype=0)


class BacktestTest(unittest.TestCase):
    def test_hand_calculated_hedge_with_both_fees_and_boundaries(self):
        report = simulate(INPUT, SIMULATED)
        self.assertEqual(report['window']['complete_days'], 2)
        self.assertEqual(report['baseline']['equity'], 1020)
        self.assertEqual(report['entries'][0]['quantity'], 5)
        day1, day2 = report['series']
        self.assertEqual(day1['daily_funding'], 5.5)      # 5 × 110 × +1%
        self.assertEqual(day2['daily_funding'], 5.35)     # 5 × (101 × −1% + 104 × 2%)
        self.assertEqual(day1['mark_to_market_pnl'], 5)   # 5 × (2 − 1)
        self.assertEqual(day2['mark_to_market_pnl'], -10) # 5 × (4 − 6)
        self.assertEqual(day1['equity'], 1020.5)
        self.assertEqual(day1['cash'], 515.5)
        self.assertEqual(day2['cash_before_exit'], 520.85)
        self.assertEqual(day2['cash'], day2['equity'])
        metrics = report['metrics']
        self.assertEqual(metrics['post_entry_cash'], 510)
        self.assertEqual(metrics['entry_fees'], 10)      # (500 + 500) × 1%
        self.assertEqual(metrics['exit_fees'], 10.5)     # (520 + 530) × 1%
        self.assertEqual(metrics['ending_equity'], 1000.35)
        self.assertAlmostEqual(metrics['total_return'], 1000.35 / 1020 - 1)
        self.assertAlmostEqual(metrics['max_drawdown'], 1 - 1000.35 / 1020.5)

    def test_one_day_and_all_cash(self):
        one_day = simulate({**INPUT, 'end': ['2026-01-02']}, SIMULATED)
        self.assertEqual(one_day['metrics']['exit_fees'], 10.15)
        self.assertEqual(one_day['metrics']['ending_equity'], 1010.35)
        self.assertIsNone(one_day['metrics']['daily_volatility'])
        cash = simulate({**INPUT, 'reserve_pct': ['100'], 'slippage_bps': ['5']}, SIMULATED)
        self.assertEqual(cash['entries'], [])
        self.assertEqual(cash['metrics']['total_return'], 0)
        self.assertEqual(cash['metrics']['ending_equity'], 1020)
        self.assertEqual(cash['metrics']['total_slippage'], 0)
        self.assertIsNone(cash['metrics']['break_even_slippage_bps'])

    def test_fixed_slippage_hits_cash_and_drawdown_without_resizing(self):
        original = simulate(INPUT, SIMULATED)
        replay = simulate({**INPUT, 'slippage_bps': ['25']}, SIMULATED)
        self.assertEqual(replay['entries'], original['entries'])
        day1, day2 = replay['series']
        metrics = replay['metrics']
        self.assertEqual(metrics['total_funding'], original['metrics']['total_funding'])
        self.assertEqual(metrics['total_fees'], original['metrics']['total_fees'])
        self.assertEqual(metrics['post_entry_cash'], 507.5)
        self.assertEqual(metrics['post_entry_equity'], 1007.5)
        self.assertEqual(day1['entry_slippage'], 2.5)  # 1,000 × 25 bps
        self.assertEqual(day1['exit_slippage'], 0)
        self.assertEqual(day1['cash'], 513)
        self.assertEqual(day1['equity'], 1018)
        self.assertEqual(day2['entry_slippage'], 0)
        self.assertEqual(day2['exit_slippage'], 2.625)  # 1,050 × 25 bps
        self.assertEqual(day2['cash_before_exit'], 518.35)
        self.assertEqual(day2['cash'], 995.225)
        self.assertEqual(metrics['ending_equity'], 995.225)
        self.assertEqual(metrics['total_slippage'], 5.125)
        self.assertEqual(metrics['pre_slippage_pnl'], original['metrics']['net_pnl'])
        self.assertEqual(metrics['net_pnl'], -24.775)
        self.assertIsNone(metrics['break_even_slippage_bps'])
        self.assertAlmostEqual(metrics['max_drawdown'], 1 - 995.225 / 1020)

    def test_reject_invalid_dates_weights_and_fees(self):
        for change in ({'start': ['2026-01-01']}, {'end': ['20260103']}, {'end': ['2025-12-01']},
                       {'capital': ['NaN']}, {'reserve_pct': ['101']}, {'entry_bps': ['-1']},
                       {'slippage_bps': ['-1']}, {'slippage_bps': ['1001']},
                       {'btc': ['99.999']}, {'eth': ['-10']}, {'btc': ['100', '50']}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                simulate({**INPUT, **change}, SIMULATED)

    def test_missing_observations_fail_for_active_asset_only(self):
        data = deepcopy(SIMULATED)
        del data['minute_marks'][BTC][str(START + NOON - MINUTE_MS)]
        with self.assertRaisesRegex(ValueError, 'Bybit data is missing'):
            simulate(INPUT, data)
        data = deepcopy(SIMULATED)
        del data['funding'][BTC][str(START + NOON)]
        with self.assertRaisesRegex(ValueError, 'Funding history is incomplete'):
            simulate(INPUT, data)

    def test_full_window_matches_fixture_funding_and_equity_identity(self):
        days = 30
        data = demo_generation(days=days)['observations']
        bounds = backtest_bounds(data)
        report = simulate(query(start=bounds['start'], end=bounds['end']), data)
        self.assertEqual(report['window']['complete_days'], days)
        start_ms = midnight(date.fromisoformat(bounds['start']))
        self.assertEqual(len(report['entries']), 4)
        expected_funding = 0
        expected_price_pnl = 0
        end_key = str(midnight(date.fromisoformat(bounds['end'])))
        spot_entry_key = str(start_ms - DAY_MS)
        for row in report['entries']:
            symbol = row['asset'] + 'USDT'
            self.assertEqual(row['spot_price'], float(data['spot_daily'][symbol][spot_entry_key]))
            self.assertEqual(row['perp_mark'], float(data['mark_daily'][symbol][spot_entry_key]))
            for timestamp, rate in data['funding'][symbol].items():
                time_ms = int(timestamp)
                if time_ms > start_ms:
                    mark_key = str((time_ms // MINUTE_MS - 1) * MINUTE_MS)
                    expected_funding += row['quantity'] * float(data['minute_marks'][symbol][mark_key]) * float(rate)
            expected_price_pnl += row['quantity'] * (
                float(data['spot_daily'][symbol][end_key]) - row['spot_price']
                + row['perp_mark'] - float(data['mark_daily'][symbol][end_key])
            )
        metrics = report['metrics']
        self.assertAlmostEqual(metrics['total_funding'], expected_funding, places=9)
        self.assertAlmostEqual(metrics['ending_equity'],
                               100000 + expected_funding + expected_price_pnl - metrics['total_fees'], places=8)

    def test_slippage_scenarios_cross_break_even(self):
        selection = {**INPUT, **query(capital=1000, entry_bps=0, exit_bps=0)}
        zero = simulate(selection, SIMULATED)
        zero_metrics = zero['metrics']
        self.assertEqual(zero, simulate({**selection, 'slippage_bps': ['0']}, SIMULATED))
        # Five units per leg earn 10.85 in funding and lose 10 to price movement.
        traded_notional = 5 * (100 + 100 + 104 + 106)
        for rate in (0, 1, 2, 5):
            with self.subTest(slippage_bps=rate):
                replay = simulate({**selection, 'slippage_bps': [str(rate)]}, SIMULATED)
                metrics = replay['metrics']
                self.assertEqual(replay['entries'], zero['entries'])
                self.assertEqual(metrics['total_funding'], zero_metrics['total_funding'])
                self.assertEqual(metrics['total_fees'], zero_metrics['total_fees'])
                self.assertAlmostEqual(metrics['net_pnl'], 0.85 - traded_notional * rate / 10_000)
                self.assertAlmostEqual(metrics['pre_slippage_pnl'] - metrics['total_slippage'], metrics['net_pnl'])
        break_even = zero_metrics['break_even_slippage_bps']
        result = simulate({**selection, 'slippage_bps': [str(break_even)]}, SIMULATED)
        self.assertAlmostEqual(break_even, 0.85 * 10_000 / traded_notional)
        self.assertAlmostEqual(result['metrics']['net_pnl'], 0)


if __name__ == '__main__':
    unittest.main()
