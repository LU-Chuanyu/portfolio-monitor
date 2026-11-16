"""Exercise minute accounting and failures without a browser or Bybit access."""

from threading import Event, Thread
import unittest
from unittest.mock import Mock

from live_monitor import LiveMonitor, MINUTE_MS, SAMPLE_LIMIT


START = 30_000_000


def quote(value=None, *, status='ready', linear='available'):
    return {
        'status': status,
        'equity': value,
        'positions': [{'symbol': 'BTCUSDT', 'value': value}],
        'sources': {'spot': {'status':'available'}, 'linear': {'status':linear}},
        'issues': [],
    }


class LiveMonitorTest(unittest.TestCase):
    def setUp(self):
        self.now = START
        self.provider = Mock()
        self.hook = Mock(side_effect=lambda snapshot, stamp: f'quote-{stamp}')
        self.monitor = LiveMonitor(self.provider, self.hook, clock_ms=lambda: self.now)

    def test_single_source_outage_keeps_complete_snapshot_and_minute_gap(self):
        initial = quote(110)
        partial = quote(status='partial', linear='unavailable')
        self.provider.side_effect = [initial, partial, quote(111), quote(112)]
        self.monitor.observe()
        self.now += MINUTE_MS
        self.monitor.observe()
        state = self.monitor.view()
        self.assertEqual(state['latest']['status'], 'partial')
        self.assertIsNone(state['latest']['positions'][0]['value'])
        self.assertEqual(state['last_complete']['positions'][0]['value'], 110)
        self.assertNotIn('request_id', initial)
        self.now += 5_000
        self.monitor.observe()
        self.now += MINUTE_MS - 5_000
        self.monitor.observe()
        self.assertEqual(self.monitor.view()['samples'], [
            {'t': START, 'value': 110},
            {'t': START + MINUTE_MS, 'value': None},
            {'t': START + 2 * MINUTE_MS, 'value': 112},
        ])

    def test_funding_only_partial_can_still_supply_coherent_equity(self):
        self.provider.return_value = quote(110, status='partial')
        self.monitor.observe()
        self.assertIsNone(self.monitor.view()['last_complete'])
        self.assertEqual(self.monitor.view()['samples'], [{'t': START, 'value': 110}])

    def test_minute_provider_sets_chart_without_slowing_intermediate_quotes(self):
        minute_provider = Mock(side_effect=[quote(110), quote(112)])
        monitor = LiveMonitor(self.provider, minute_provider=minute_provider, clock_ms=lambda: self.now)
        self.provider.return_value = quote(111)
        monitor.observe()
        self.now += 5_000
        monitor.observe()
        self.assertEqual(monitor.view()['latest']['equity'], 111)
        self.now += MINUTE_MS - 5_000
        monitor.observe()
        self.assertEqual(monitor.view()['samples'], [
            {'t': START, 'value': 110},
            {'t': START + MINUTE_MS, 'value': 112},
        ])
        self.assertEqual(minute_provider.call_count, 2)
        self.provider.assert_called_once_with()

    def test_only_complete_price_coverage_and_finite_equity_can_be_sampled(self):
        for snapshot in (quote(110, status='partial', linear='unavailable'), quote(float('nan'))):
            with self.subTest(snapshot=snapshot):
                self.provider.return_value = snapshot
                self.monitor.observe()
                self.assertIsNone(self.monitor.view()['samples'][-1]['value'])
                self.now += MINUTE_MS
        self.assertIsNone(self.monitor.view()['last_complete'])

    def test_unobserved_minutes_are_null_and_series_memory_is_bounded(self):
        self.provider.return_value = quote(110)
        self.monitor.observe()
        self.now += (SAMPLE_LIMIT + 500) * MINUTE_MS
        self.monitor.observe()
        samples = self.monitor.view()['samples']
        self.assertEqual(len(samples), SAMPLE_LIMIT)
        self.assertEqual(samples[0], {'t': self.now - (SAMPLE_LIMIT - 1) * MINUTE_MS, 'value': None})
        self.assertEqual(samples[-1], {'t': self.now, 'value': 110})
        self.assertTrue(all(sample['value'] is None for sample in samples[:-1]))

    def test_readers_cannot_change_published_snapshots_or_record_observations(self):
        self.provider.return_value = quote(110)
        self.monitor.observe()
        view = self.monitor.view()
        view['latest']['positions'][0]['value'] = None
        view['last_complete']['equity'] = None
        view['samples'][0]['value'] = None

        published = self.monitor.view()
        self.assertEqual(published['latest']['positions'][0]['value'], 110)
        self.assertEqual(published['last_complete']['equity'], 110)
        self.assertEqual(published['samples'], [{'t': START, 'value': 110}])
        self.provider.assert_called_once_with()
        self.hook.assert_called_once_with(quote(110), START)

    def test_provider_exception_keeps_last_complete_and_reports_internal_failure(self):
        self.provider.side_effect = [quote(110), RuntimeError('synthetic provider failure')]
        self.monitor.observe()
        self.now += MINUTE_MS
        with self.assertLogs('live_monitor', 'ERROR'):
            self.monitor.observe()
        state = self.monitor.view()
        self.assertEqual(state['latest']['status'], 'unavailable')
        self.assertIsNone(state['latest']['next_settlement']['timestamp_ms'])
        self.assertIsNone(state['latest']['next_settlement']['amount_usdt'])
        self.assertEqual(state['latest']['sources']['linear']['reason'], 'monitor_error')
        self.assertNotIn('synthetic provider failure', repr(state['latest']))
        self.assertEqual(state['latest']['request_id'], f'quote-{self.now}')
        self.assertEqual(state['last_complete']['equity'], 110)
        self.assertIsNone(state['samples'][-1]['value'])

    def test_slow_observation_cannot_backdate_recovery(self):
        def recovery():
            self.now += MINUTE_MS + 10
            return quote(111)

        self.provider.side_effect = recovery
        self.monitor.observe()
        self.assertEqual(self.monitor.view()['samples'], [{'t': START, 'value': None}])
        self.provider.side_effect = None
        self.provider.return_value = quote(112)
        self.monitor.observe()
        self.assertEqual(self.monitor.view()['samples'][-1], {'t': START + MINUTE_MS, 'value': 112})

    def test_background_start_records_without_frontend_requests(self):
        observed = Event()
        self.provider.return_value = quote(110)

        def notify(*args):
            observed.set()

        monitor = LiveMonitor(self.provider, notify, clock_ms=lambda: self.now)
        monitor.start()
        monitor.start()
        try:
            self.assertTrue(observed.wait(timeout=2))
        finally:
            monitor.stop()
        self.provider.assert_called_once_with()
        self.assertEqual(monitor.view()['samples'], [{'t': START, 'value': 110}])

    def test_view_is_available_while_provider_waits(self):
        self.provider.return_value = quote(110)
        self.monitor.observe()
        provider_entered = Event()
        release_provider = Event()

        def delayed_quote():
            provider_entered.set()
            release_provider.wait(timeout=2)
            return quote(111)

        self.provider.side_effect = delayed_quote
        update = Thread(target=self.monitor.observe)
        update.start()
        try:
            self.assertTrue(provider_entered.wait(timeout=2))
            self.assertEqual(self.monitor.view()['latest']['equity'], 110)
            self.assertTrue(update.is_alive())
        finally:
            release_provider.set()
            update.join(timeout=2)
        self.assertEqual(self.monitor.view()['latest']['equity'], 111)


if __name__ == '__main__':
    unittest.main()
