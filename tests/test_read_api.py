"""Verify that refreshing the dashboard cannot collect or record quotes."""

from http.client import HTTPResponse
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from diagnostics import Diagnostics
from live_monitor import LiveMonitor
from serve import DashboardReads, PreviewHandler, unavailable_history


def request(reads, route):
    connection = Mock()
    connection.makefile.return_value = BytesIO(f'GET {route} HTTP/1.1\r\nHost: localhost\r\n\r\n'.encode())
    wire = BytesIO()
    connection.sendall.side_effect = wire.write
    PreviewHandler(connection, ('127.0.0.1', 0), SimpleNamespace(reads=reads))

    response_socket = Mock()
    response_socket.makefile.return_value = BytesIO(wire.getvalue())
    with HTTPResponse(response_socket) as response:
        response.begin()
        return response.status, json.load(response)


class SnapshotReadTest(unittest.TestCase):
    def test_current_and_legacy_refresh_urls_read_only(self):
        stamp = 1_790_000_000_000
        provider = Mock(return_value={
            'status': 'ready', 'equity': 100,
            'sources': {category: {'status': 'available'} for category in ('spot', 'linear')},
        })
        record = Mock(return_value='background-observation')
        monitor = LiveMonitor(provider, record, clock_ms=lambda: stamp)
        monitor.observe()

        reads = DashboardReads(
            quotes=monitor.view, history=Mock(), funding_windows=Mock(),
            health=lambda: {'state': 'healthy'}, diagnostics=Mock(),
        )
        for suffix in ('', '?fresh=1', ''):
            status, snapshot = request(reads, '/api/snapshot' + suffix)
            self.assertEqual(status, 200)
            self.assertEqual(snapshot['request_id'], 'background-observation')
            self.assertEqual(snapshot['samples'], [{'t': stamp - stamp % 60_000, 'value': 100}])
            self.assertNotIn('manual_refresh', snapshot)

        provider.assert_called_once_with()
        record.assert_called_once()

    def test_corrupt_diagnostic_storage_does_not_interrupt_ready_quotes(self):
        quote = {'status': 'ready', 'equity': 100}
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'diagnostics.sqlite3'
            diagnostics = Diagnostics(path)
            path.write_text('not a valid SQLite database', encoding='utf-8')
            reads = DashboardReads(
                quotes=lambda: {'latest': quote, 'samples': []},
                history=unavailable_history, funding_windows=lambda: None,
                health=diagnostics.status, diagnostics=diagnostics.query,
            )
            with self.assertLogs('serve', 'WARNING'):
                status, payload = request(reads, '/api/snapshot')
                self.assertEqual(status, 200)
                self.assertEqual(payload['equity'], 100)
                self.assertEqual(payload['status'], 'ready')
                self.assertEqual(payload['health']['state'], 'unavailable')
                for route in ('/api/health', '/api/diagnostics'):
                    with self.subTest(route=route):
                        status, payload = request(reads, route)
                        self.assertEqual(status, 503)
                        self.assertIn('Diagnostics storage unavailable.', payload['issues'])
            status, payload = request(reads, '/api/history/status')
            self.assertEqual(status, 200)
            self.assertEqual(payload['state'], 'unavailable')
            for route in ('/api/history', '/api/funding-windows', '/api/backtest/bounds',
                          '/api/funding-history?symbol=BTCUSDT'):
                with self.subTest(route=route):
                    status, payload = request(reads, route)
                    self.assertEqual(status, 503)
                    self.assertIn('No verified historical data is available.', payload['issues'])
