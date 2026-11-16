"""Check settled funding window boundaries, coverage and daily ledger parity."""

from datetime import date, datetime, timezone
from decimal import Decimal
from http.client import IncompleteRead
from threading import Event, Thread
import unittest
from unittest.mock import patch


from demo_fixtures import demo_generation, demo_portfolio
from funding_windows import FundingWindows, WINDOW_MS
from history import DAY, MINUTE_MS, bybit, midnight


class FundingWindowsTest(unittest.TestCase):
    def setUp(self):
        generation = demo_generation()
        self.observations = generation['observations']
        self.report = generation['report']
        self.published = date.fromisoformat(self.report['window']['end_date'])
        self.cutoff = midnight(self.published + DAY)
        portfolio = demo_portfolio()
        self.now = self.cutoff + 1_000
        self.calls = []
        self.hype = 'HYPEUSDT'
        self.btc = 'BTCUSDT'
        quantity = next(position['quantity'] for position in portfolio['positions']
                        if position['instrument'] == 'PERP' and position['symbol'] == self.hype)
        self.hype_funding = quantity * 100 * (0.001 - 0.002)
        self.no_mark = False
        self.no_settlement = False
        self.no_connection = False
        self.service = FundingWindows(
            portfolio, lambda: self.observations, clock=self.clock, fetch=self.download,
        )

    def clock(self):
        return datetime.fromtimestamp(self.now / 1000, timezone.utc)

    def download(self, symbol, start, stop):
        self.calls.append((symbol, start, stop))
        if self.no_connection and symbol == self.btc:
            raise TimeoutError('Bybit timed out')
        half = WINDOW_MS // 2
        interval = 240 if symbol == self.hype else 480
        rates = {start: Decimal('0'), stop: Decimal('999')}
        if symbol == self.hype:
            rates[start] = Decimal('0.001')
            if not self.no_settlement:
                rates[start + half] = Decimal('-0.002')
        marks = {stamp - MINUTE_MS: Decimal('100') for stamp in rates}
        if self.no_mark and symbol == self.hype:
            del marks[start - MINUTE_MS]
            marks[start] = Decimal('1')  # Must never price the settlement with this later candle.
        return interval, rates, marks

    def test_21_archived_windows_match_synthetic_daily_report(self):
        result = self.service.refresh_once()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['complete_windows'], 21)
        self.assertEqual(result['windows'][-1]['end_at'], result['as_of'])
        for day_index, day in enumerate(self.report['series'][-7:]):
            group = result['windows'][3 * day_index:3 * day_index + 3]
            self.assertEqual(group[0]['start_at'][:10], day['date'])
            self.assertAlmostEqual(sum(row['net_funding_usdt'] for row in group), day['daily_funding'], places=9)
        self.assertEqual(self.calls, [])

    def test_intraday_uses_actual_smaller_cadence_and_excludes_stop(self):
        self.now = self.cutoff + WINDOW_MS - 1
        self.assertEqual(self.service.refresh_once()['status'], 'complete')
        self.assertEqual(self.calls, [])
        self.now += 1
        self.assertEqual(self.service.read()['status'], 'partial')
        result = self.service.refresh_once()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(self.calls), 4)
        window = result['windows'][-1]
        self.assertEqual(window['assets']['HYPE']['settlements'], 2)
        self.assertAlmostEqual(window['assets']['HYPE']['funding_usdt'], self.hype_funding)
        self.assertAlmostEqual(window['net_funding_usdt'], self.hype_funding)
        self.assertEqual(window['assets']['BTC']['funding_usdt'], 0)
        self.service.refresh_once()
        self.assertEqual(len(self.calls), 4)

    def test_missing_cadence_mark_and_timeout_retry_only_affected_contracts(self):
        self.now = self.cutoff + WINDOW_MS
        self.no_mark = True
        self.no_settlement = True
        self.no_connection = True
        first = self.service.refresh_once()
        self.assertIsNone(first['windows'][-1]['net_funding_usdt'])
        self.assertIsNone(first['windows'][-1]['assets']['HYPE']['funding_usdt'])
        self.assertIsNone(first['windows'][-1]['assets']['BTC']['funding_usdt'])
        self.assertEqual(first['windows'][-1]['assets']['ETH']['funding_usdt'], 0)
        self.assertTrue(any('published settlement unavailable' in issue for issue in first['issues']))
        self.assertTrue(any('timed out' in issue for issue in first['issues']))
        self.no_settlement = False
        self.no_connection = False
        self.calls.clear()
        second = self.service.refresh_once()
        self.assertIsNone(second['windows'][-1]['net_funding_usdt'])
        self.assertTrue(any('preceding one-minute mark unavailable' in issue for issue in second['issues']))
        self.assertEqual({symbol for symbol, *_ in self.calls}, {self.hype, self.btc})
        self.no_mark = False
        self.calls.clear()
        self.assertEqual(self.service.refresh_once()['status'], 'complete')
        self.assertEqual({symbol for symbol, *_ in self.calls}, {self.hype})

    def test_truncated_exchange_response_keeps_other_contracts_and_can_retry(self):
        self.now = self.cutoff + WINDOW_MS
        interrupted = False

        def fetch(symbol, start, stop):
            nonlocal interrupted
            if symbol == self.btc and not interrupted:
                interrupted = True
                with patch('history.urlopen', side_effect=IncompleteRead(b'{"retCode":', 12)):
                    bybit('funding/history', symbol=symbol)
            return self.download(symbol, start, stop)

        self.service.fetch = fetch
        degraded = self.service.refresh_once()
        self.assertEqual(degraded['complete_windows'], 20)
        self.assertIn('response could not be read', ' '.join(degraded['issues']))
        self.assertIsNone(degraded['windows'][-1]['assets']['BTC']['funding_usdt'])
        self.assertEqual(degraded['windows'][-1]['assets']['ETH']['funding_usdt'], 0)
        self.calls.clear()
        self.assertEqual(self.service.refresh_once()['complete_windows'], 21)
        self.assertEqual({symbol for symbol, *_ in self.calls}, {self.btc})

    def test_new_archive_supersedes_intraday_and_keeps_closed_window_boundaries(self):
        self.now = self.cutoff + WINDOW_MS
        provisional = self.service.refresh_once()
        self.assertAlmostEqual(provisional['windows'][-1]['net_funding_usdt'], self.hype_funding)
        self.observations['window']['end_date'] = (self.published + DAY).isoformat()
        for symbol in self.observations['funding']:
            self.observations['funding'][symbol][str(self.cutoff)] = '0'
            self.observations['minute_marks'][symbol][str(self.cutoff - MINUTE_MS)] = '100'
        self.assertEqual(self.service.read()['windows'][-1]['net_funding_usdt'], 0)
        self.calls.clear()
        self.assertEqual(self.service.refresh_once()['status'], 'complete')
        self.assertEqual(self.calls, [])

    def test_read_remains_available_during_download(self):
        self.now = self.cutoff + WINDOW_MS
        entered, release, read_done = Event(), Event(), Event()
        results = []

        def slow_download(symbol, start, stop):
            entered.set()
            release.wait(4)
            return self.download(symbol, start, stop)

        def read():
            results.append(self.service.read())
            read_done.set()

        self.service.fetch = slow_download
        ingest = Thread(target=self.service.refresh_once)
        request = Thread(target=read)
        ingest.start()
        try:
            self.assertTrue(entered.wait(2))
            request.start()
            self.assertTrue(read_done.wait(2))
            self.assertEqual(results[0]['complete_windows'], 20)
            self.assertIsNone(results[0]['windows'][-1]['net_funding_usdt'])
        finally:
            release.set()
            ingest.join(timeout=5)
            if request.ident is not None:
                request.join(timeout=5)
        self.assertEqual(self.service.read()['complete_windows'], 21)

    def test_background_delay_aligns_to_utc_boundary_and_retries_gaps(self):
        self.now = self.cutoff + WINDOW_MS - 2_500
        closed = self.service.read()
        self.assertEqual(self.service._wait_seconds(closed), 2.5)
        self.now += 2_500
        self.assertEqual(self.service._wait_seconds(closed), 0)
        self.assertEqual(self.service._wait_seconds(self.service.read()), 30)
        filled = self.service.refresh_once()
        self.assertEqual(self.service._wait_seconds(filled), 300)
        self.now += WINDOW_MS - 5_000
        self.assertEqual(self.service._wait_seconds(filled), 5)


if __name__ == '__main__':
    unittest.main()
