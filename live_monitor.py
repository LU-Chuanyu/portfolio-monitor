"""Keep the preview's quote state and UTC minute samples in the server process."""

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import logging
from math import isfinite
from threading import Event, Lock, Thread
from time import time_ns


LOG = logging.getLogger(__name__)
CHECK_MS = 5_000
MINUTE_MS = 60_000
SAMPLE_LIMIT = 120


def clock_ms():
    return time_ns() // 1_000_000


def unavailable(observed_at):
    as_of = datetime.fromtimestamp(observed_at / 1000, timezone.utc).isoformat()
    return {
        'status': 'unavailable',
        'as_of': as_of,
        'transport': 'monitor',
        'equity': None,
        'next_settlement': {
            'timestamp_ms': None, 'amount_usdt': None,
            'included_contracts': 0, 'total_contracts': 0, 'as_of': as_of,
        },
        'positions': [],
        'asset_deltas': [],
        'issues': ['Internal quote collection failed.'],
        'sources': {category: {
            'status': 'unavailable', 'reason': 'monitor_error', 'transport': 'monitor',
            'received_at': None, 'exchange_time_ms': None,
        } for category in ('spot', 'linear')},
    }


def valued_equity(snapshot):
    equity = snapshot.get('equity')
    sources = snapshot.get('sources', {})
    if type(equity) in (int, float) and isfinite(equity) and all(
        sources.get(category, {}).get('status') == 'available' for category in ('spot', 'linear')
    ):
        return equity
    return None


class LiveMonitor:
    def __init__(self, provider, on_observation=None, *, minute_provider=None, clock_ms=clock_ms):
        self._provider = provider
        self._minute_provider = minute_provider if minute_provider is not None else provider
        self._on_observation = on_observation
        self._clock_ms = clock_ms
        self._state_lock = Lock()
        self._observe_lock = Lock()
        self._stop = Event()
        self._thread = None
        self._samples = deque(maxlen=SAMPLE_LIMIT)
        self._latest = None
        self._last_complete = None

    def start(self):
        with self._state_lock:
            if self._thread is None:
                self._thread = Thread(target=self._run, name='live-equity-monitor', daemon=True)
                self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def _run(self):
        while not self._stop.is_set():
            self.observe()
            delay = (CHECK_MS - self._clock_ms() % CHECK_MS) / 1_000
            self._stop.wait(delay)

    def observe(self):
        """Collect one quote snapshot and record the first observation of its UTC minute."""
        with self._observe_lock:
            started_at = self._clock_ms()
            slot = started_at // MINUTE_MS * MINUTE_MS
            with self._state_lock:
                first_observation = not self._samples or slot > self._samples[-1][0]
            provider = self._minute_provider if first_observation else self._provider
            try:
                snapshot = deepcopy(provider())
            except Exception:
                LOG.exception('Internal quote collection failed')
                snapshot = unavailable(self._clock_ms())
            observed_at = self._clock_ms()
            if self._on_observation is not None:
                try:
                    request_id = self._on_observation(snapshot, observed_at)
                except Exception:
                    LOG.exception('Failed to record quote diagnostics')
                else:
                    if request_id:
                        snapshot = {**snapshot, 'request_id': request_id}

            equity = valued_equity(snapshot)
            with self._state_lock:
                self._latest = snapshot
                if snapshot['status'] == 'ready' and equity is not None:
                    self._last_complete = snapshot
                self._record_minute(slot, equity if observed_at < slot + MINUTE_MS else None)
            return deepcopy(snapshot)

    def _record_minute(self, slot, equity):
        if self._samples:
            previous = self._samples[-1][0]
            if slot <= previous:
                return
            first_missing = max(previous + MINUTE_MS, slot - (SAMPLE_LIMIT - 1) * MINUTE_MS)
            self._samples.extend((missing, None) for missing in range(first_missing, slot, MINUTE_MS))
        self._samples.append((slot, equity))

    def view(self):
        """Read a detached view of the state published by the collector."""
        with self._state_lock:
            return {
                'samples': [{'t': slot, 'value': equity} for slot, equity in self._samples],
                'latest': deepcopy(self._latest),
                'last_complete': deepcopy(self._last_complete),
            }
