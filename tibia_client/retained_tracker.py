"""Retained numeric rows with one cancellable background discovery lane.

The owner serializes ``read`` calls. ``reset`` and ``close`` may interrupt that
owner: generation checks reject old discovery and read results. No capture or
visual reader is created here, and each discovery owns a separate reader.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import tempfile
import threading
import time

from tibia_client.adaptive import FIELDS
from tibia_client.models import Quality, Reading


_DEFAULT_CACHE = object()


def create_reader():
    from tibia_client.retained_resources import RetainedResourceReader

    return RetainedResourceReader()


class _Superseded(Exception):
    pass


class RetainedResourceTracker:
    """Read current pixels; reader factories must return independent instances."""

    def __init__(self, reader_factory=create_reader, *, clock=time.monotonic,
                 rediscovery_interval=2.0, cache_path=_DEFAULT_CACHE):
        if (isinstance(rediscovery_interval, bool)
                or not math.isfinite(rediscovery_interval) or rediscovery_interval <= 0):
            raise ValueError('rediscovery_interval must be finite and positive')
        from tibia_client.retained_resources import cache_path as default_path, load_layout

        self._cache_path = default_path() if cache_path is _DEFAULT_CACHE else cache_path
        self._layout = load_layout(self._cache_path) if self._cache_path is not None else None
        self._reader_factory, self._clock = reader_factory, clock
        self._retry_interval = max(10.0, rediscovery_interval)
        self._reader = None
        self._lock = threading.Lock()
        self._worker = None
        self._generation = 0
        self._closed = False
        self._candidate = None
        self._missing_since = None
        self._next_recovery = 0.0
        self._discovery_ms = 0.0
        self._detail = ''

    @property
    def busy(self):
        with self._lock:
            return self._busy()

    def _busy(self):
        return self._worker is not None and self._worker.is_alive()

    @property
    def discovery_ms(self):
        with self._lock:
            return self._discovery_ms

    @property
    def detail(self):
        with self._lock:
            return self._detail

    @property
    def has_layout(self):
        with self._lock:
            return self._layout is not None

    def _invalidate(self):
        self._generation += 1
        self._layout = self._candidate = self._missing_since = None
        self._next_recovery = self._discovery_ms = 0.0
        self._detail = ''

    def reset(self):
        """Discard calibration without waiting for an old discovery to finish."""
        with self._lock:
            self._invalidate()

    def close(self):
        with self._lock:
            self._closed = True
            self._invalidate()

    def wait(self, timeout=3.0):
        """Bounded worker shutdown wait; close/reset never join workers."""
        with self._lock:
            worker = self._worker
        if worker is not None:
            worker.join(timeout)
        return not self.busy

    @staticmethod
    def _unknown(detail):
        return {name: Reading(None, Quality.UNKNOWN, detail) for name in FIELDS}

    def _save(self, layout, generation):
        from tibia_client.retained_resources import save_layout

        try:
            # Serialize outside the state lock so reset/close can invalidate a
            # slow write. Only the final atomic publication needs that lock.
            path = Path(self._cache_path)
            with tempfile.TemporaryDirectory(prefix=f'.{path.stem}-', dir=path.parent) as directory:
                staged = Path(directory) / 'layout.json'
                save_layout(staged, layout)
                with self._lock:
                    if not self._closed and generation == self._generation:
                        os.replace(staged, path)
        except OSError as exc:
            with self._lock:
                if not self._closed and generation == self._generation:
                    self._detail = f'Layout cache: {exc}'

    def _recover(self, frame, generation, prepare, discover):
        started = self._clock()
        foreground = None

        def checkpoint():
            with self._lock:
                if self._closed or generation != self._generation:
                    raise _Superseded()
            if self._clock() - started > 30:
                raise TimeoutError('Panel search exceeded 30 seconds; capture continues')

        try:
            checkpoint()
            if prepare:
                foreground = self._reader_factory()
                checkpoint()
            result = None
            if discover:
                reader = self._reader_factory()
                checkpoint()
                result = reader.discover(frame, use_ocr=False, checkpoint=checkpoint)
            checkpoint()
            with self._lock:
                if not self._closed and generation == self._generation:
                    self._candidate = (result.layout if result else None,
                                       (self._clock() - started) * 1000, foreground)
                    self._detail = (result.detail or 'Panel located; validating current frame') if result else ''
                    if not discover:
                        self._next_recovery = 0.0
        except _Superseded:
            pass
        except Exception as exc:
            with self._lock:
                if not self._closed and generation == self._generation:
                    if foreground is not None:
                        self._candidate = (None, 0.0, foreground)
                    self._detail = f'Panel recovery: {exc}'

    def _launch(self, frame, generation, started, *, prepare=False, discover=True):
        """Called with the state lock held; the same lane also initializes readers."""
        self._next_recovery = started + self._retry_interval
        self._detail = ('Searching for panel in background; capture continues' if discover else
                        'Preparing resource reader in background')
        self._worker = threading.Thread(target=self._recover,
                                       args=(frame, generation, prepare, discover),
                                       name='resource-discovery', daemon=True)
        try:
            self._worker.start()
        except Exception as exc:
            self._worker = None
            self._detail = f'Panel recovery: {exc}'

    def read(self, frame):
        """Decode this frame; discovery results supply geometry, never values."""
        with self._lock:
            if self._closed:
                return self._unknown('Resource tracker is closed')
            candidate, self._candidate = self._candidate, None
            if candidate and candidate[2] is not None:
                self._reader = candidate[2]
            generation, reader, layout = self._generation, self._reader, self._layout
            missing_since, next_recovery = self._missing_since, self._next_recovery
            discovery_ms = self._discovery_ms
        if reader is None:
            with self._lock:
                if self._closed or generation != self._generation:
                    return self._unknown('Resource calibration changed during this read')
                started = self._clock()
                if started >= self._next_recovery and not self._busy():
                    self._launch(frame, generation, started, prepare=True, discover=layout is None)
            return self._unknown('Preparing resource reader in background')
        started = self._clock()
        save = False
        if candidate and candidate[0] is not None:
            proposed, discovery_ms, _ = candidate
            if len(reader.visible_fields(frame, proposed)) >= 2:
                layout, save = proposed, True
        visible = reader.visible_fields(frame, layout) if layout is not None else set()
        if layout is not None and len(visible) < 2 and started >= next_recovery:
            shifted = reader.relocate(frame, layout)
            if shifted is not None:
                shifted_visible = reader.visible_fields(frame, shifted)
                if len(shifted_visible) >= 2:
                    layout, visible, save = shifted, shifted_visible, True
        if len(visible) >= 2:
            missing_since = None
        elif missing_since is None:
            missing_since = started
        needs_search = layout is None or (missing_since is not None and started - missing_since >= 2)
        readings = (reader.read(frame, layout, use_ocr=False) if layout is not None else
                    self._unknown('Locating resource panel in background'))
        with self._lock:
            if self._closed or generation != self._generation:
                return self._unknown('Resource calibration changed during this read')
            self._layout, self._missing_since = layout, missing_since
            self._discovery_ms = discovery_ms
            if len(visible) >= 2 and not self._busy():
                self._detail = ''
            if needs_search and started >= self._next_recovery and not self._busy():
                self._launch(frame, generation, started)
        if save and self._cache_path is not None:
            self._save(layout, generation)
        with self._lock:
            if self._closed or generation != self._generation:
                return self._unknown('Resource calibration changed during this read')
            return readings
