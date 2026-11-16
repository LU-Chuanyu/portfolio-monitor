"""Exercise historical publication with synthetic seeds in a temporary archive."""

from datetime import date, datetime, timezone
from decimal import Decimal
from http.client import IncompleteRead
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from demo_fixtures import PORTFOLIO_PATH, demo_generation, demo_portfolio
from history import DAY, MINUTE_MS, bybit, midnight
from history_backfill import HistoryBackfill, SEED_FILES
from scripts.generate_demo_data import write_demo_seeds


class BackfillTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.research = Path(directory.name)
        generation = demo_generation()
        write_demo_seeds(self.research, generation)
        self.seeds = {name: (self.research / name).read_bytes() for name in SEED_FILES.values()}
        self.last_day = date.fromisoformat(generation['report']['window']['end_date'])
        self.now = datetime.fromtimestamp(midnight(self.last_day + DAY) / 1000, timezone.utc)
        self.calls = []
        self.omit_mark = False
        self.error = None
        self.history = self.collector()

    def collector(self):
        return HistoryBackfill(self.research, portfolio_path=PORTFOLIO_PATH,
                               clock=lambda: self.now, fetch=self.download)

    def download(self, portfolio, start, end):
        self.calls.append((start, end))
        if self.error:
            raise self.error
        intervals = dict.fromkeys(('BTCUSDT', 'ETHUSDT', 'HYPEUSDT', 'SOLUSDT'), 480)
        first, stop = midnight(start), midnight(end + DAY)
        result = {name: {} for name in ('spot_daily', 'mark_daily', 'funding', 'minute_marks')}
        result['interval_minutes'] = intervals
        for symbol in intervals:
            result['spot_daily'][symbol] = {stamp: Decimal('101') for stamp in range(first, stop, 1440 * MINUTE_MS)}
            result['mark_daily'][symbol] = {stamp: Decimal('100') for stamp in range(first, stop, 1440 * MINUTE_MS)}
            result['funding'][symbol] = {stamp: Decimal('0') for stamp in range(first, stop, 480 * MINUTE_MS)}
            if symbol == 'BTCUSDT':
                result['funding'][symbol][first] = Decimal('0.01')
                result['funding'][symbol][first + 480 * MINUTE_MS] = Decimal('-0.02')
            result['minute_marks'][symbol] = {
                stamp - MINUTE_MS: Decimal('120') for stamp in result['funding'][symbol]
            }
        if self.omit_mark:
            del result['minute_marks']['BTCUSDT'][first - MINUTE_MS]
        return result

    def test_current_day_never_downloads_unclosed_day(self):
        self.assertEqual(self.history.refresh_once()['state'], 'current')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.history.read()['report']['window']['end_date'], self.last_day.isoformat())

    def test_archive_for_a_different_portfolio_is_rejected_at_startup(self):
        portfolio = demo_portfolio()
        portfolio['positions'][0]['quantity'] += 1
        portfolio_path = self.research / 'other-portfolio.json'
        portfolio_path.write_text(json.dumps(portfolio), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'match the active portfolio'):
            HistoryBackfill(self.research, portfolio_path=portfolio_path)

    def test_corrupt_generation_recovers_from_seeds_then_rebuilds(self):
        output = self.research / 'history-generation.json'
        output.write_text('truncated archive', encoding='utf-8')
        with self.assertLogs('history_backfill', 'WARNING'):
            history = self.collector()
            baseline, status = history.view()
            self.assertEqual(baseline, self.history.read())
            self.assertIn('Verified seed history', status['warning'])
            self.now += 2 * DAY
            status = history.refresh_once()
        self.assertEqual(status['state'], 'current')
        self.assertIn('Verified seed history', status['warning'])
        self.assertEqual(self.collector().read(), history.read())

    def test_atomic_generation_keeps_signed_funding_and_old_ledger(self):
        self.now += 3 * DAY
        first, last = self.last_day + DAY, self.last_day + 3 * DAY
        self.assertEqual(self.history.refresh_once()['state'], 'current')
        self.assertEqual(self.calls, [(first, last)])
        generation = self.history.read()
        original = json.loads(self.seeds['history-report-common.json'])
        self.assertEqual(generation['report']['series'][:-3], original['series'])
        quantity = next(position['quantity'] for position in demo_portfolio()['positions']
                        if position['instrument'] == 'PERP' and position['symbol'] == 'BTCUSDT')
        expected_funding = quantity * 120 * (0.01 - 0.02)
        self.assertAlmostEqual(generation['report']['series'][-3]['daily_funding'], expected_funding)
        self.assertAlmostEqual(generation['report']['series'][-1]['cumulative_funding'],
                               original['series'][-1]['cumulative_funding'] + expected_funding)
        self.assertEqual(generation['archive']['through_utc_day'], last.isoformat())
        self.assertEqual(generation['observations']['window']['end_date'], last.isoformat())
        archive = generation['archive']['symbols']['BTCUSDT']['settled_funding']
        self.assertEqual(archive['count'], len(archive['values']))
        self.assertEqual(archive['values'][str(midnight(first))], '0.01')
        self.assertEqual(generation['observations']['minute_marks']['BTCUSDT'][str(midnight(first) - MINUTE_MS)], '120')
        self.assertEqual(self.collector().read(), generation)
        self.assertEqual(self.collector().refresh_once()['state'], 'current')
        self.assertEqual(len(self.calls), 1)
        for name, contents in self.seeds.items():
            self.assertEqual((self.research / name).read_bytes(), contents)

    def test_failed_refresh_and_missing_minute_preserve_prior_generation(self):
        self.now += DAY
        self.assertEqual(self.history.refresh_once()['state'], 'current')
        before = self.history.read()
        saved = (self.research / 'history-generation.json').read_bytes()
        self.now += DAY
        self.error = TimeoutError('Bybit timed out')
        self.assertIn('timed out', self.history.refresh_once()['error'])
        self.assertEqual(self.history.read(), before)
        self.error = None
        self.omit_mark = True
        failed = self.history.refresh_once()
        self.assertEqual(failed['state'], 'error')
        self.assertIn('completed minute mark unavailable', failed['error'])
        self.assertEqual((self.research / 'history-generation.json').read_bytes(), saved)
        self.assertEqual(self.history.read(), before)
        self.omit_mark = False
        self.assertEqual(self.history.refresh_once()['state'], 'current')
        self.assertEqual(self.history.read()['report']['window']['end_date'], (self.last_day + 2 * DAY).isoformat())

    def test_start_checks_automatically_and_retries(self):
        self.now += DAY
        retried = Event()

        def fetch(portfolio, start, end):
            result = self.download(portfolio, start, end)
            if len(self.calls) == 1:
                with patch('history.urlopen', side_effect=IncompleteRead(b'{"retCode":', 12)):
                    bybit('funding/history', symbol='BTCUSDT')
            retried.set()
            return result

        history = HistoryBackfill(self.research, portfolio_path=PORTFOLIO_PATH,
                                  clock=lambda: self.now, fetch=fetch, interval_seconds=0.01)
        with self.assertLogs('history_backfill', level='INFO'):
            history.start()
            try:
                self.assertTrue(retried.wait(5))
                self.assertEqual(history.refresh_once()['state'], 'current')
            finally:
                history.stop()
        generation, status = history.view()
        self.assertEqual(generation['report']['window']['end_date'], status['covered_through'])
        self.assertEqual(len(self.calls), 2)


if __name__ == '__main__':
    unittest.main()
