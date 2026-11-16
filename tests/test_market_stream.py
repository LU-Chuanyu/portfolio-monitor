"""Exercise WebSocket snapshots and REST fallback without network access."""

import unittest
from unittest.mock import patch

from bybit_stream import BybitTickerStream, MAX_TICK_AGE_MS
import market


NOW = 1_940_000_040_000


def frame(symbol, data, *, stamp=NOW, kind='snapshot'):
    return {'topic':f'tickers.{symbol}', 'type':kind, 'ts':stamp, 'data':data}


class MarketStreamTest(unittest.TestCase):
    def setUp(self):
        self.stream = BybitTickerStream(market.TRADED_SYMBOLS, market.TLS_CONTEXT)
        for category in market.CATEGORIES:
            self.stream._reset(category, None)

    def populate(self, offsets=None):
        offsets = offsets or {}
        for symbol in market.TRADED_SYMBOLS['spot']:
            stamp = NOW + offsets.get(('spot', symbol), 0)
            self.stream.handle_ticker('spot', frame(symbol, {'lastPrice':'1000'}, stamp=stamp), NOW)
        for symbol in market.TRADED_SYMBOLS['linear']:
            data = {'markPrice':'1000','fundingRate':'0','nextFundingTime':str(NOW + 100_000)}
            stamp = NOW + offsets.get(('linear', symbol), 0)
            self.stream.handle_ticker('linear', frame(symbol, data, stamp=stamp), NOW)

    def test_delta_merges_without_erasing_settlement_and_ignores_older_updates(self):
        delta = frame('BTCUSDT', {'markPrice':'1001'}, stamp=NOW+50, kind='delta')
        self.stream.handle_ticker('linear', delta, NOW+50)
        _, sources = self.stream.snapshot_inputs(NOW+50)
        self.assertEqual(sources['linear']['reason'], 'stream_snapshot_incomplete')

        self.populate()
        self.stream.handle_ticker('linear', delta, NOW+50)
        self.stream.handle_ticker('linear', frame('BTCUSDT', {'markPrice':'1002'}, kind='delta'), NOW+50)
        markets, sources = self.stream.snapshot_inputs(NOW+50)
        self.assertEqual(sources['linear']['status'], 'available')
        self.assertEqual(markets['linear']['BTCUSDT'], {
            'markPrice':'1001','fundingRate':'0','nextFundingTime':str(NOW + 100_000),
        })

    def test_stale_or_disconnected_category_cannot_supply_partial_stream_snapshot(self):
        self.populate()
        markets, sources = self.stream.snapshot_inputs(NOW + MAX_TICK_AGE_MS + 1)
        self.assertFalse(markets)
        self.assertEqual(sources['spot']['reason'], 'stream_stale')
        self.stream._reset('linear', 'stream_disconnected')
        self.stream.handle_ticker('linear', frame('BTCUSDT', {'markPrice':'999'}, kind='delta'), NOW)
        self.assertEqual(self.stream.snapshot_inputs(NOW)[1]['linear']['reason'], 'stream_disconnected')
        self.stream._reset('linear', None)
        self.populate()
        self.assertEqual(self.stream.snapshot_inputs(NOW)[1]['linear']['status'], 'available')

    def test_disconnect_falls_back_to_rest_and_recovers_to_live_stream(self):
        self.populate()
        with (patch.object(market, '_stream', self.stream), patch.object(self.stream, 'start'),
              patch.object(market, 'time_ns', return_value=NOW*1_000_000),
              patch.object(market, 'shared_rest_snapshot', return_value={'transport':'rest'}) as rest):
            live = market.shared_market_snapshot()
            self.assertEqual(live['transport'], 'websocket')
            self.assertEqual(live['expected_funding'], 0)
            rest.assert_not_called()
            self.stream._reset('linear', 'stream_disconnected')
            fallback = market.shared_market_snapshot()
            self.assertEqual(fallback['transport'], 'rest')
            self.assertEqual(fallback['stream']['linear']['reason'], 'stream_disconnected')
            self.stream._reset('linear', None)
            self.populate()
            self.assertEqual(market.shared_market_snapshot()['transport'], 'websocket')
            rest.assert_called_once_with()

    def test_matching_pair_skew_triggers_rest_even_with_equal_category_times(self):
        self.populate({('spot', 'BTCUSDT'): -14_000, ('linear', 'ETHUSDT'): -14_000})
        _, sources = self.stream.snapshot_inputs(NOW)
        self.assertEqual(sources['spot']['exchange_time_ms'], sources['linear']['exchange_time_ms'])
        self.assertEqual(sources['spot']['symbol_exchange_time_ms']['BTCUSDT'], NOW - 14_000)
        with (patch.object(market, '_stream', self.stream), patch.object(self.stream, 'start'),
              patch.object(market, 'time_ns', return_value=NOW * 1_000_000),
              patch.object(market, 'shared_rest_snapshot', return_value={'transport': 'rest'}) as rest):
            fallback = market.shared_market_snapshot()
            self.assertEqual(fallback['transport'], 'rest')
            self.assertEqual(fallback['stream']['spot']['reason'], 'exchange_time_skew')
            self.assertEqual(fallback['stream']['spot']['pair_skew_ms'], {'BTCUSDT': 14_000})
            self.assertEqual(fallback['stream']['linear']['pair_skew_ms'], {'ETHUSDT': 14_000})
            self.populate()
            self.assertEqual(market.shared_market_snapshot()['transport'], 'websocket')
            rest.assert_called_once_with()

    def test_minute_read_prefers_fresh_rest_with_live_stream_fallback(self):
        self.populate()
        ready = {'transport': 'rest', 'status': 'ready'}
        partial = {'transport': 'rest', 'status': 'partial'}
        with (patch.object(market, '_stream', self.stream), patch.object(self.stream, 'start'),
              patch.object(market, 'time_ns', return_value=NOW * 1_000_000),
              patch.object(market, 'shared_rest_snapshot', side_effect=[ready, partial, partial]) as rest):
            self.assertEqual(market.shared_market_snapshot(prefer_rest=True), ready)
            self.assertEqual(market.shared_market_snapshot(prefer_rest=True)['transport'], 'websocket')
            self.stream._reset('linear', 'stream_disconnected')
            self.assertEqual(market.shared_market_snapshot(prefer_rest=True)['status'], 'partial')
            self.assertEqual(rest.call_count, 3)
            self.assertTrue(all(call.kwargs == {'force': True} for call in rest.call_args_list))

    def test_rest_skew_keeps_batch_envelope_guard_without_symbol_times(self):
        self.populate()
        markets, _ = self.stream.snapshot_inputs(NOW)
        sources = {
            'spot': market.source_result(NOW),
            'linear': market.source_result(NOW - market.MAX_PAIR_SKEW_MS - 1),
        }
        with patch.object(market, 'time_ns', return_value=NOW * 1_000_000):
            snapshot = market.assemble_snapshot(markets, sources, 'rest')
        self.assertEqual(snapshot['status'], 'partial')
        self.assertEqual(snapshot['sources']['linear']['reason'], 'exchange_time_skew')
        self.assertEqual(snapshot['sources']['spot']['status'], 'available')
        self.assertIsNone(snapshot['equity'])
        self.assertIsNone(snapshot['expected_funding'])
        self.assertIsNone(snapshot['next_settlement']['amount_usdt'])

    def test_earliest_funding_batch_differs_from_sum_of_each_legs_next_payment(self):
        self.populate()
        markets, sources = self.stream.snapshot_inputs(NOW)
        earliest = NOW + 50_000
        for symbol in ('BTCUSDT', 'ETHUSDT'):
            markets['linear'][symbol]['nextFundingTime'] = str(earliest)
        markets['linear']['SOLUSDT']['fundingRate'] = '0.001'
        portfolio = {
            'positions': [
                {**position, 'quantity': 3} for position in market.PORTFOLIO['positions']
            ],
        }
        with (patch.object(market, 'PORTFOLIO', portfolio),
              patch.object(market, 'time_ns', return_value=NOW * 1_000_000)):
            snapshot = market.assemble_snapshot(markets, sources, 'websocket')
        self.assertEqual(snapshot['status'], 'ready')
        self.assertEqual(snapshot['expected_funding'], 3)
        self.assertEqual(snapshot['next_settlement'], {
            'timestamp_ms': earliest, 'amount_usdt': 0, 'included_contracts': 2,
            'total_contracts': 4, 'as_of': snapshot['as_of'],
        })

    def test_next_batch_does_not_treat_unavailable_schedule_or_amount_as_zero(self):
        self.populate()
        markets, _ = self.stream.snapshot_inputs(NOW)
        sources = {category: market.source_result(NOW) for category in market.CATEGORIES}
        earliest = NOW + 50_000
        for symbol in ('BTCUSDT', 'ETHUSDT'):
            markets['linear'][symbol]['nextFundingTime'] = str(earliest)
        with patch.object(market, 'time_ns', return_value=NOW * 1_000_000):
            del markets['linear']['SOLUSDT']['fundingRate']
            result = market.assemble_snapshot(markets, sources, 'rest')
            self.assertEqual(result['next_settlement']['amount_usdt'], 0)
            del markets['linear']['BTCUSDT']['fundingRate']
            result = market.assemble_snapshot(markets, sources, 'rest')
            self.assertEqual(result['next_settlement']['timestamp_ms'], earliest)
            self.assertEqual(result['next_settlement']['included_contracts'], 2)
            self.assertIsNone(result['next_settlement']['amount_usdt'])
            markets['linear']['SOLUSDT']['nextFundingTime'] = str(NOW)
            result = market.assemble_snapshot(markets, sources, 'rest')
            self.assertIsNone(result['next_settlement']['timestamp_ms'])
            self.assertEqual(result['next_settlement']['included_contracts'], 0)
            self.assertEqual(result['next_settlement']['total_contracts'], 4)
            self.assertIsNone(result['next_settlement']['amount_usdt'])

    def test_rest_fallback_reuses_a_recent_snapshot_and_refreshes_after_expiry(self):
        with (patch.object(market, '_rest_cache', None),
              patch.object(market, 'monotonic', return_value=10) as clock,
              patch.object(market, 'market_snapshot', side_effect=[{'quote':1},{'quote':2}]) as fetch):
            self.assertEqual(market.shared_rest_snapshot(), {'quote':1})
            self.assertEqual(market.shared_rest_snapshot(), {'quote':1})
            clock.return_value = 10 + market.REST_CACHE_SECONDS
            self.assertEqual(market.shared_rest_snapshot(), {'quote':2})
            self.assertEqual(fetch.call_count, 2)
