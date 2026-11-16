"""Check quote selection, valuation and funding against hand-calculated inputs."""

import unittest

from portfolio_metrics import calculate_portfolio


def fixture(perp_side='SHORT', funding_rate='0.001'):
    portfolio = {'positions': [
        {'asset': 'USDT', 'symbol': 'USDT', 'instrument': 'SPOT', 'side': 'LONG',
         'quantity': 40, 'entry_price_usdt': 1},
        {'asset': 'BTC', 'symbol': 'BTCUSDT', 'instrument': 'SPOT', 'side': 'LONG',
         'quantity': 2, 'entry_price_usdt': 100},
        {'asset': 'BTC', 'symbol': 'BTCUSDT', 'instrument': 'PERP', 'side': perp_side,
         'quantity': 2, 'entry_price_usdt': 100},
    ]}
    markets = {
        'spot': {'BTCUSDT': {'lastPrice': '112', 'markPrice': '999'}},
        'linear': {'BTCUSDT': {'lastPrice': '999', 'markPrice': '115',
                              'fundingRate': funding_rate, 'nextFundingTime': '1800000000000'}},
    }
    return portfolio, markets


class PortfolioMetricsTest(unittest.TestCase):
    def test_last_and_mark_prices_drive_separate_legs(self):
        result = calculate_portfolio(*fixture())
        cash, spot, perp = result['positions']
        self.assertEqual(cash['value'], 40)
        self.assertIsNone(cash['expected_funding'])
        self.assertEqual(spot['current_price'], 112)
        self.assertEqual(spot['pnl'], 24)
        self.assertEqual(perp['current_price'], 115)
        self.assertEqual(perp['pnl'], -30)
        self.assertEqual(result['starting_equity'], 240)
        self.assertEqual(result['unrealised_pnl'], -6)
        self.assertEqual(result['equity'], 234)
        self.assertEqual(result['net_delta'], -6)
        self.assertEqual(result['delta_ratio'], -6 / 234)
        self.assertAlmostEqual(result['expected_funding'], 0.23)
        self.assertEqual(result['issues'], [])

    def test_missing_quote_rate_and_settlement_are_reported(self):
        portfolio, markets = fixture()
        markets['linear']['BTCUSDT'].update(markPrice='', fundingRate='', nextFundingTime='')
        result = calculate_portfolio(portfolio, markets)
        for metric in ('equity', 'unrealised_pnl', 'expected_funding', 'delta_ratio'):
            self.assertIsNone(result[metric])
        self.assertEqual(len(result['issues']), 3)
        self.assertEqual(result['positions'][1]['current_price'], 112)

    def test_positive_funding_pays_short_and_charges_long(self):
        for side, rate, expected in (('SHORT', '0.001', 0.23), ('SHORT', '-0.001', -0.23),
                                     ('LONG', '0.001', -0.23), ('LONG', '-0.001', 0.23)):
            with self.subTest(side=side, rate=rate):
                self.assertAlmostEqual(calculate_portfolio(*fixture(side, rate))['expected_funding'], expected)
