"""Start the portfolio collectors and serve the dashboard and its read API."""

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import sqlite3
from time import gmtime
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent
RESEARCH = ROOT / 'data' / 'live'
from backtest import backtest_bounds, simulate
from diagnostics import Diagnostics
from funding_windows import FundingWindows, funding_archive
from history_backfill import ARCHIVE_ERRORS, HistoryBackfill
from live_monitor import LiveMonitor
from market import PORTFOLIO, PORTFOLIO_PATH, demo_market_snapshot, shared_market_snapshot


PAGE_ROUTES = {'/', '/index.html', '/positions', '/history', '/data'}
STATIC_ROUTES = {
    '/api.js': ('api.js', 'text/javascript; charset=utf-8'),
    '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
    '/chart.js': ('chart.js', 'text/javascript; charset=utf-8'),
    '/view-model.js': ('view-model.js', 'text/javascript; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/fonts/bricolage-grotesque.woff2': ('fonts/bricolage-grotesque.woff2', 'font/woff2'),
    '/fonts/dm-sans.woff2': ('fonts/dm-sans.woff2', 'font/woff2'),
}
LOGGER = logging.getLogger(__name__)
DIAGNOSTICS_UNAVAILABLE = 'Diagnostics storage unavailable.'
HISTORY_UNAVAILABLE = 'No verified historical data is available.'


def unavailable_history():
    """Keep the live dashboard available without publishing invalid history."""
    report = {'status': 'unavailable', 'issues': [HISTORY_UNAVAILABLE]}
    ingestion = {'state': 'unavailable', 'covered_through': None, 'target_utc_day': None,
                 'last_checked_at': None, 'error': HISTORY_UNAVAILABLE, 'warning': None}
    return {'report': report, 'observations': None, 'archive': None}, ingestion


@dataclass(frozen=True)
class DashboardReads:
    """Read operations available to HTTP handlers."""

    quotes: Callable[[], dict]
    history: Callable[[], tuple[dict, dict]]
    funding_windows: Callable[[], dict | None]
    health: Callable[[], dict]
    diagnostics: Callable[..., dict]


class PreviewHandler(BaseHTTPRequestHandler):
    def write_json(self, payload, status=200):
        self.write_response('application/json; charset=utf-8',
                            json.dumps(payload, allow_nan=False).encode(), status=status)

    def diagnostic_health(self):
        try:
            return self.server.reads.health()
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
            LOGGER.warning('Cannot read diagnostics health: %s', error)
            return {'state': 'unavailable', 'issues': [DIAGNOSTICS_UNAVAILABLE]}

    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path
        query = parse_qs(url.query, keep_blank_values=True)
        reads = self.server.reads
        if path == '/api/history':
            generation, ingestion = reads.history()
            report = generation['report']
            return self.write_json({**report, 'ingestion': ingestion},
                                   200 if report['status'] == 'available' else 503)
        if path == '/api/history/status':
            return self.write_json(reads.history()[1])
        if path == '/api/funding-windows':
            try:
                windows = reads.funding_windows()
            except ARCHIVE_ERRORS as error:
                LOGGER.warning('Cannot read funding windows: %s', error)
                return self.write_json({'issues': ['Eight-hour funding windows are temporarily unavailable.']}, 503)
            if windows is None:
                return self.write_json({'issues': [HISTORY_UNAVAILABLE]}, 503)
            return self.write_json(windows)
        if path == '/api/health':
            health = self.diagnostic_health()
            return self.write_json(health, 503 if health['state'] == 'unavailable' else 200)
        if path == '/api/diagnostics':
            try:
                filters = {key: query[key][0] for key in ('start', 'end', 'request_id', 'limit') if key in query}
                return self.write_json(reads.diagnostics(**filters))
            except (sqlite3.Error, OSError, json.JSONDecodeError) as error:
                LOGGER.warning('Cannot read diagnostics log: %s', error)
                return self.write_json({'issues': [DIAGNOSTICS_UNAVAILABLE]}, 503)
            except ValueError as error:
                return self.write_json({'issues': [str(error)]}, 400)

        if path in {'/api/funding-history', '/api/backtest', '/api/backtest/bounds'}:
            try:
                generation, _ = reads.history()
                if generation['observations'] is None:
                    return self.write_json({'issues': [HISTORY_UNAVAILABLE]}, 503)
                if path == '/api/funding-history':
                    payload = funding_archive(query.get('symbol', [''])[0], generation['archive'])
                elif path == '/api/backtest/bounds':
                    payload = backtest_bounds(generation['observations'])
                else:
                    payload = simulate(query, generation['observations'])
                return self.write_json(payload)
            except ValueError as error:
                return self.write_json({'issues': [str(error)]}, 400)
            except OSError:
                return self.write_json({'issues': ['Historical source unavailable.']}, 503)

        if path == '/api/snapshot':
            quotes = reads.quotes()
            snapshot = quotes['latest']
            if snapshot is None:
                return self.write_json({'issues': ['Waiting for the first quote snapshot.']}, 503)
            payload = {**snapshot, 'samples': quotes['samples'], 'health': self.diagnostic_health()}
            if snapshot['status'] != 'ready':
                payload['last_complete'] = quotes['last_complete']
            return self.write_json(payload)

        if path in PAGE_ROUTES:
            filename, content_type = 'index.html', 'text/html; charset=utf-8'
        elif path in STATIC_ROUTES:
            filename, content_type = STATIC_ROUTES[path]
        else:
            self.send_error(404)
            return

        try:
            data = (ROOT / filename).read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        if filename == 'index.html' and getattr(self.server, 'demo', False):
            data = data.replace(b'<body>', b'<body data-demo="true">', 1)

        self.write_response(content_type, data)

    def write_response(self, content_type, data, *, status=200):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8003)
    parser.add_argument('--demo', action='store_true', help='Run with synthetic prices and history without contacting Bybit')
    parser.add_argument('--research-dir', type=Path, default=RESEARCH)
    parser.add_argument('--diagnostics-db', type=Path)
    parser.add_argument('--alert-after', type=int, default=30, help='Seconds of continuous incomplete quotes before a local alert')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    if args.alert_after < 1:
        parser.error('--alert-after must be positive')
    logging.Formatter.converter = gmtime
    logging.basicConfig(level=logging.INFO, format='%(asctime)sZ %(levelname)s %(name)s %(message)s', datefmt='%Y-%m-%dT%H:%M:%S')
    mode = 'demo' if args.demo else 'live'
    diagnostics_db = args.diagnostics_db or ROOT / 'data' / mode / 'diagnostics.sqlite3'
    with ThreadingHTTPServer(('127.0.0.1', args.port), PreviewHandler) as server:
        server.demo = args.demo
        try:
            diagnostics = Diagnostics(diagnostics_db, alert_after_seconds=args.alert_after)
        except (sqlite3.Error, OSError) as error:
            LOGGER.warning('Diagnostics could not start: %s', error)
            diagnostics = None

        def record_observation(snapshot, observed_at):
            return diagnostics.observe(snapshot, observed_at_ms=observed_at)

        def unavailable_diagnostics(**filters):
            raise sqlite3.OperationalError(DIAGNOSTICS_UNAVAILABLE)

        live_monitor = LiveMonitor(
            demo_market_snapshot if args.demo else shared_market_snapshot,
            record_observation if diagnostics else None,
            minute_provider=None if args.demo else partial(shared_market_snapshot, prefer_rest=True),
        )
        try:
            demo_clock = None
            generation = None
            if args.demo:
                from scripts.generate_demo_data import build_demo_generation
                today = datetime.now(timezone.utc).date()
                demo_time = datetime.combine(today, time(), timezone.utc)
                demo_clock = lambda: demo_time
                generation = build_demo_generation(PORTFOLIO, end_date=today - timedelta(days=1))
            history_backfill = HistoryBackfill(args.research_dir, portfolio_path=PORTFOLIO_PATH,
                                               clock=demo_clock, generation=generation)
            if not args.demo and history_backfill.read()['observations'].get('data_origin') == 'synthetic':
                raise ValueError('Live mode requires a Bybit history directory.')
        except ARCHIVE_ERRORS as error:
            LOGGER.warning('Historical data could not start: %s', error)
            history_backfill = None
        funding_windows = (
            FundingWindows(PORTFOLIO, lambda: history_backfill.read()['observations'], clock=demo_clock)
            if history_backfill else None
        )
        server.reads = DashboardReads(
            quotes=live_monitor.view,
            history=history_backfill.view if history_backfill else unavailable_history,
            funding_windows=funding_windows.read if funding_windows else lambda: None,
            health=diagnostics.status if diagnostics else unavailable_diagnostics,
            diagnostics=diagnostics.query if diagnostics else unavailable_diagnostics,
        )
        live_monitor.start()
        workers = () if args.demo else (history_backfill, funding_windows)
        for worker in workers:
            if worker:
                worker.start()
        print(f'Open http://127.0.0.1:{server.server_port}/  (Ctrl+C to stop)', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            for worker in reversed(workers):
                if worker:
                    worker.stop()
            live_monitor.stop()


if __name__ == '__main__':
    main()
