"""Store local market observations and sustained outage alerts in SQLite."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3
from time import time_ns
from uuid import UUID, uuid4


LOG = logging.getLogger(__name__)
NETWORK_REASONS = {'timeout', 'network_error', 'stream_disconnected'}
RESPONSE_REASONS = {'http_error', 'exchange_rejected', 'invalid_response'}
# Two six-second REST attempts plus the five-second monitor cadence fit in this interval.
MAX_OBSERVATION_GAP_MS = 20_000
HEARTBEAT_TIMEOUT_MS = 30_000


def utc_iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def range_bound(value, end_of_day=False):
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if end_of_day and len(value) == 10:
            moment += timedelta(days=1, milliseconds=-1)
        return int(moment.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('Use an ISO 8601 date or timestamp for start and end.') from error


def classification(snapshot):
    if snapshot.get('transport') == 'monitor':
        return 'collector_error'
    if snapshot.get('status') == 'ready':
        if snapshot.get('transport') == 'rest' and any(
            source.get('reason') for source in snapshot.get('stream', {}).values()
        ):
            return 'fallback'
        return 'complete'
    reasons = {source.get('reason') for source in snapshot.get('sources', {}).values()}
    if reasons & RESPONSE_REASONS:
        return 'exchange_response'
    if reasons & NETWORK_REASONS:
        return 'network_error'
    return 'data_quality'


class Diagnostics:
    """Archive selected public ticker inputs, computed snapshots and local alerts."""

    def __init__(self, db_path, alert_after_seconds=30, *, max_rows=30_000, retention_seconds=7 * 86400,
                 clock_ms=None):
        if min(alert_after_seconds, max_rows, retention_seconds) <= 0:
            raise ValueError('Alert threshold and archive limits must be positive.')
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.threshold_seconds = alert_after_seconds
        self.max_rows = max_rows
        self.retention_ms = retention_seconds * 1000
        self.clock_ms = clock_ms or (lambda: time_ns() // 1_000_000)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS observations (
                    sequence INTEGER PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    observed_ms INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS observations_by_time
                ON observations(observed_ms, sequence);
                CREATE TABLE IF NOT EXISTS health_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    payload TEXT NOT NULL
                );
            """)

    @staticmethod
    def read_health(conn):
        row = conn.execute('SELECT payload FROM health_state WHERE singleton = 1').fetchone()
        if row:
            health = json.loads(row[0])
            if not isinstance(health, dict) or type(health.get('last_observation_ms')) is not int:
                raise ValueError('Invalid diagnostics health record.')
            return health
        return {
            'state': 'unknown', 'since': None, 'last_observation_at': None,
            'request_id': None, 'classification': None, 'issues': [],
            'last_alert': None, 'last_recovery': None,
            'failure_started_ms': None, 'last_observation_ms': None,
        }

    def observe(self, snapshot, trigger='automatic', *, observed_at_ms=None):
        """Record one completed monitor observation and return its request ID."""
        now_ms = self.clock_ms() if observed_at_ms is None else observed_at_ms
        recorded = snapshot.get('status', 'unavailable')
        row = {
            'request_id': str(uuid4()), 'observed_at': utc_iso(now_ms), 'trigger': trigger,
            'status': recorded, 'transport': snapshot.get('transport'),
            'classification': classification(snapshot), 'sources': snapshot.get('sources', {}),
            'issues': snapshot.get('issues', []), 'inputs': snapshot.get('inputs', {}),
            'snapshot': {key: value for key, value in snapshot.items() if key != 'inputs'},
            'alert_event': None,
        }
        event = None
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            health = self.read_health(conn)
            last_ms = health['last_observation_ms']
            if last_ms is None or now_ms >= last_ms:
                event = self.update_health(health, row, now_ms)
                row['alert_event'] = event
                conn.execute('INSERT OR REPLACE INTO health_state VALUES (1, ?)', (json.dumps(health),))
            conn.execute(
                'INSERT INTO observations (request_id, observed_ms, payload) VALUES (?, ?, ?)',
                (row['request_id'], now_ms, json.dumps(row, separators=(',', ':'), allow_nan=False)),
            )
            cutoff = (last_ms if last_ms is not None and last_ms > now_ms else now_ms) - self.retention_ms
            conn.execute('DELETE FROM observations WHERE observed_ms < ?', (cutoff,))
            conn.execute("""
                DELETE FROM observations WHERE (observed_ms, sequence) <= (
                    SELECT observed_ms, sequence FROM observations
                    ORDER BY observed_ms DESC, sequence DESC LIMIT 1 OFFSET ?
                )
            """, (self.max_rows,))
        if event:
            level = logging.WARNING if event['type'] == 'alert' else logging.INFO
            LOG.log(level, 'Market data %s at %s (request_id=%s, classification=%s, issues=%s)',
                    event['type'], event['at'], event['request_id'], event['classification'], event['issues'])
        return row['request_id']

    def update_health(self, health, row, now_ms):
        state = health['state']
        started = health['failure_started_ms']
        last_ms = health['last_observation_ms']
        threshold_ms = self.threshold_seconds * 1000
        event_type = None
        if row['status'] == 'ready':
            started = None
            if state == 'alerting':
                event_type = 'recovery'
            state = 'healthy'
        elif state != 'alerting':
            if started is None or now_ms - last_ms > MAX_OBSERVATION_GAP_MS:
                started = now_ms
            state = 'degraded'
            if now_ms - started >= threshold_ms:
                state = 'alerting'
                event_type = 'alert'
        if state in {'degraded', 'alerting'}:
            health['since'] = utc_iso(started)
        elif health['state'] != state:
            health['since'] = row['observed_at']
        health.update(
            state=state, last_observation_at=row['observed_at'], last_observation_ms=now_ms,
            request_id=row['request_id'], classification=row['classification'],
            issues=row['issues'], failure_started_ms=started,
        )
        if event_type is None:
            return None
        event = {
            'type': event_type, 'at': row['observed_at'], 'request_id': row['request_id'],
            'classification': row['classification'], 'issues': row['issues'],
        }
        health['last_' + event_type] = event
        return event

    def status(self):
        with closing(sqlite3.connect(self.path)) as conn:
            health = self.read_health(conn)
        health.pop('failure_started_ms')
        last = health.pop('last_observation_ms')
        if last is not None and self.clock_ms() - last > HEARTBEAT_TIMEOUT_MS:
            health['state'] = 'stale'
            health['issues'] = ['No recent collector observation.', *health['issues']]
        return {**health, 'threshold_seconds': self.threshold_seconds}

    def query(self, start=None, end=None, request_id=None, limit=50):
        lower, upper = range_bound(start), range_bound(end, end_of_day=True)
        if lower is not None and upper is not None and lower > upper:
            raise ValueError('Start must be on or before end.')
        try:
            limit = int(str(limit))
        except (ValueError, TypeError) as error:
            raise ValueError('Limit must be an integer from 1 to 100.') from error
        if not 1 <= limit <= 100:
            raise ValueError('Limit must be an integer from 1 to 100.')
        if request_id is not None:
            try:
                request_id = str(UUID(request_id))
            except (ValueError, AttributeError, TypeError) as error:
                raise ValueError('Request ID must be a UUID.') from error
        conditions = [
            ('observed_ms >= ?', lower), ('observed_ms <= ?', upper), ('request_id = ?', request_id),
        ]
        query = 'SELECT payload FROM observations'
        clauses = [clause for clause, value in conditions if value is not None]
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY observed_ms DESC, sequence DESC LIMIT ?'
        parameters = [value for _, value in conditions if value is not None]
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('BEGIN')
            rows = conn.execute(query, (*parameters, limit)).fetchall()
            first = conn.execute('SELECT MIN(observed_ms) FROM observations').fetchone()[0]
            last = conn.execute('SELECT MAX(observed_ms) FROM observations').fetchone()[0]
        coverage = None if first is None else {
            'start': utc_iso(first),
            'end': utc_iso(last),
            'outside': (lower is not None and lower > last) or (upper is not None and upper < first),
        }
        return {'observations': [json.loads(row[0]) for row in rows], 'limit': limit, 'coverage': coverage}
