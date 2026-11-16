"""Exercise local alerts and queryable evidence with an isolated archive."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from diagnostics import Diagnostics
from live_monitor import unavailable
import market


NOW = 1_940_000_040_000
READY = {'status': 'ready', 'issues': [], 'transport': 'websocket'}
PARTIAL = {
    'status': 'partial',
    'issues': ['LINEAR Bybit request failed (timeout)'],
    'transport': 'rest',
    'sources': {'linear': {'status': 'unavailable', 'reason': 'timeout'}},
}


class DiagnosticsTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'diagnostics.sqlite3'
        self.now_ms = NOW
        self.archive = Diagnostics(self.path, clock_ms=lambda: self.now_ms)

    def observe(self, snapshot, seconds=0, trigger='automatic'):
        self.now_ms = NOW + seconds * 1000
        return self.archive.observe(snapshot, trigger, observed_at_ms=self.now_ms)

    def test_brief_degradation_never_alerts(self):
        self.assertEqual(self.archive.status()['state'], 'unknown')
        self.observe(PARTIAL)
        self.observe(PARTIAL, 15)
        self.assertEqual(self.archive.status()['state'], 'degraded')
        self.observe(READY, 20)
        current = self.archive.status()
        self.assertEqual(current['state'], 'healthy')
        self.assertIsNone(current['last_alert'])
        self.assertIsNone(current['last_recovery'])

    def test_sustained_failure_alerts_once_and_recovers_after_restart(self):
        self.observe(PARTIAL)
        self.observe(PARTIAL, 15)
        with self.assertLogs('diagnostics', 'INFO') as messages:
            alert_id = self.observe(PARTIAL, 30)
            self.observe(PARTIAL, 35)
            self.archive = Diagnostics(self.path, clock_ms=lambda: self.now_ms)
            self.assertEqual(self.archive.status()['state'], 'alerting')
            recovery_id = self.observe(READY, 40)
            self.observe(READY, 45)
        self.assertEqual(len(messages.output), 2)
        self.assertIn(alert_id, messages.output[0])
        self.assertIn(recovery_id, messages.output[1])
        current = self.archive.status()
        self.assertEqual(current['state'], 'healthy')
        self.assertEqual(current['last_alert']['request_id'], alert_id)
        self.assertEqual(current['last_alert']['classification'], 'network_error')
        self.assertEqual(current['last_recovery']['request_id'], recovery_id)
        events = [
            row['alert_event']['type'] for row in self.archive.query()['observations']
            if row['alert_event']
        ]
        self.assertEqual(events, ['recovery', 'alert'])

    def test_gap_in_observation_cannot_create_sustained_alert(self):
        self.observe(PARTIAL)
        self.observe(PARTIAL, 120)
        self.assertEqual(self.archive.status()['state'], 'degraded')
        self.assertIsNone(self.archive.status()['last_alert'])

    def test_stopped_collector_becomes_stale_without_a_new_write(self):
        self.observe(READY)
        self.assertEqual(self.archive.status()['state'], 'healthy')
        self.now_ms += 31_000
        stalled = self.archive.status()
        self.assertEqual(stalled['state'], 'stale')
        self.assertIn('No recent collector observation.', stalled['issues'])
        self.assertEqual(len(self.archive.query()['observations']), 1)
        self.observe(READY, 32)
        self.assertEqual(self.archive.status()['state'], 'healthy')

    def test_usable_rest_fallback_and_internal_error_classification(self):
        fallback = {
            **READY, 'transport': 'rest',
            'stream': {'linear': {'status': 'unavailable', 'reason': 'stream_disconnected'}},
        }
        fallback_id = self.observe(fallback)
        failure_id = self.observe(unavailable(NOW + 1000), 1)
        self.assertEqual(self.archive.query(request_id=fallback_id)['observations'][0]['classification'], 'fallback')
        self.assertEqual(self.archive.query(request_id=failure_id)['observations'][0]['classification'], 'collector_error')
        self.assertIsNone(self.archive.status()['last_alert'])

    def test_query_by_full_day_timestamp_and_id_returns_inputs_and_calculation(self):
        snapshot = {
            **READY, 'equity': 1234.56,
            'sources': {'linear': {'exchange_time_ms': NOW, 'received_at': '2031-06-18T01:00:00Z'}},
            'inputs': {'linear': {'BTCUSDT': {'markPrice': '432.10'}}},
        }
        first = self.observe(snapshot, trigger='manual')
        second = self.observe(snapshot, 2)
        exact = self.archive.query(request_id=first)['observations'][0]
        self.assertEqual(exact['inputs'], snapshot['inputs'])
        self.assertEqual(exact['snapshot']['equity'], 1234.56)
        self.assertEqual(exact['sources'], snapshot['sources'])
        self.assertEqual(exact['trigger'], 'manual')
        self.assertEqual(exact['observed_at'][-1], 'Z')
        day = exact['observed_at'][:10]
        self.assertEqual(
            [row['request_id'] for row in self.archive.query(start=day, end=day)['observations']],
            [second, first],
        )
        self.assertEqual(self.archive.query(end=exact['observed_at'])['observations'][0]['request_id'], first)
        for args in ({'start': 'three hours ago'}, {'start': '2031-06-30', 'end': '2031-06-01'},
                     {'request_id': '%'}, {'limit': '-1'}, {'limit': '101'}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.archive.query(**args)

    def test_row_and_age_retention(self):
        self.archive = Diagnostics(self.path, max_rows=2, retention_seconds=20)
        first = self.observe(READY)
        second = self.observe(READY, 5)
        third = self.observe(READY, 10)
        self.assertEqual(self.archive.query(request_id=first)['observations'], [])
        self.assertEqual(len(self.archive.query()['observations']), 2)
        self.observe(READY, 35)
        self.assertEqual(self.archive.query(request_id=second)['observations'], [])
        self.assertEqual(self.archive.query(request_id=third)['observations'], [])

    def test_market_archive_keeps_selected_public_fields_from_before_validation(self):
        markets = {
            'spot': {'BTCUSDT': {'lastPrice': '1000', 'bid1Price': '990'}},
            'linear': {'BTCUSDT': {'markPrice': '1001', 'fundingRate': '-0.0001',
                                  'nextFundingTime': str(NOW - 1), 'ask1Price': '1003'}},
        }
        sources = {category: market.source_result(NOW, transport='rest') for category in markets}
        with patch.object(market, 'time_ns', return_value=NOW * 1_000_000):
            snapshot = market.assemble_snapshot(markets, sources, 'rest')
        self.assertEqual(snapshot['inputs'], {
            'spot': {'BTCUSDT': {'lastPrice': '1000'}},
            'linear': {'BTCUSDT': {'markPrice': '1001', 'fundingRate': '-0.0001',
                                  'nextFundingTime': str(NOW - 1)}},
        })
        self.assertEqual(snapshot['status'], 'partial')

    def test_malformed_numeric_input_does_not_prevent_archiving_other_sources(self):
        markets = {
            'spot': {'BTCUSDT': {'lastPrice': '1000'}},
            'linear': {'BTCUSDT': {'markPrice': float('nan'), 'fundingRate': {'invalid': float('nan')},
                                  'nextFundingTime': str(NOW + 60_000)}},
        }
        sources = {category: market.source_result(NOW, transport='rest') for category in markets}
        with patch.object(market, 'time_ns', return_value=NOW * 1_000_000):
            snapshot = market.assemble_snapshot(markets, sources, 'rest')
        request_id = self.observe(snapshot)
        row = self.archive.query(request_id=request_id)['observations'][0]
        self.assertEqual(row['classification'], 'data_quality')
        self.assertEqual(row['inputs']['spot']['BTCUSDT']['lastPrice'], '1000')
        self.assertEqual(row['inputs']['linear']['BTCUSDT']['markPrice'], 'nan')
        self.assertEqual(row['inputs']['linear']['BTCUSDT']['fundingRate'], '<dict>')
