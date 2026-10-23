"""Production runtime ownership for persistent discovery and numeric caches."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np

from core.perception import Frame
from tibia_client.adaptive_runtime import AdaptiveResourceTracker
from tibia_client.models import Quality, Reading, Sensor, SensorKind
from tibia_client.visual import AdaptiveVisual


_KEY = ("paulistinha-skills-v1", False)


def _frame(value):
    return Frame(np.full((20, 30, 3), value, np.uint8))


def _requests():
    sensor = Sensor(SensorKind.VISUAL, (0, 0, 20, 15), "resource_health")
    return sensor, {sensor: AdaptiveVisual(_KEY[0], "resource_health")}


def _layout():
    return SimpleNamespace(bounds=(1, 1, 10, 10), fingerprint="current-layout",
                           scale=1.0, regions={"resource_health": (1, 1, 10, 10)})


class _Reader:
    def __init__(self):
        self.evidence = set()
        self.hits = self.discovers = self.reads = 0

    def discover(self, frame, *, selection=None, use_ocr=False, checkpoint=None):
        assert self.reads == 0, "discovery received the foreground reader"
        checkpoint()
        self.discovers += 1
        key = frame.pixels.tobytes()
        self.hits += key in self.evidence
        self.evidence.add(key)
        return SimpleNamespace(layout=_layout(), detail="", candidates=())

    def read(self, frame, layout, *, use_ocr=False):
        assert self.discovers == 0, "foreground received the discovery reader"
        self.reads += 1
        return {"resource_health": Reading({"count": int(frame.pixels[2, 2, 0])})}


def _join(group):
    assert group.worker is not None
    group.worker.join(5)
    assert not group.worker.is_alive()


def test_failed_attempts_keep_discovery_evidence_and_construct_hot_reader_off_thread():
    now = [0.0]
    created, factory_threads = [], []

    class RetryReader(_Reader):
        def discover(self, *args, **kwargs):
            result = super().discover(*args, **kwargs)
            if self.discovers < 3:
                result.layout, result.detail = None, "not found"
            return result

    def factory(_name):
        factory_threads.append(threading.get_ident())
        created.append(RetryReader())
        return created[-1]

    tracker = AdaptiveResourceTracker(factory, retry_interval=.5, clock=lambda: now[0])
    sensor, requests = _requests()
    observed = _frame(10)
    try:
        for attempt, instant in enumerate((0.0, .5, 1.5), start=1):
            now[0] = instant
            assert tracker.read(observed, requests)[sensor].quality is Quality.UNKNOWN
            group = tracker._groups[_KEY]
            _join(group)
            assert group.discovery_reader is created[0]
            assert created[0].discovers == attempt
            if attempt < 3:
                assert tracker.read(observed, requests)[sensor].quality is Quality.UNKNOWN
                assert group.reader is None

        # Worker completion never promotes its older numeric observations.
        current = tracker.read(_frame(99), requests)[sensor]
        assert current.quality is Quality.KNOWN
        assert current.value["count"] == 99
        assert len(created) == 2
        assert created[0].hits == 2
        assert group.reader is created[1]
        assert created[0].reads == created[1].discovers == 0
        assert all(thread != threading.get_ident() for thread in factory_threads)
    finally:
        tracker.close()


def test_reset_discards_old_discovery_lane_and_keeps_its_slot_until_cancelled():
    started, release = threading.Event(), threading.Event()
    created = []

    class BlockingReader(_Reader):
        def discover(self, *args, **kwargs):
            if self is created[0]:
                started.set()
                assert release.wait(5)
            return super().discover(*args, **kwargs)

    def factory(_name):
        created.append(BlockingReader())
        return created[-1]

    tracker = AdaptiveResourceTracker(factory)
    sensor, requests = _requests()
    old_group = new_group = None
    try:
        tracker.read(_frame(10), requests)
        old_group = tracker._groups[_KEY]
        assert started.wait(5)
        tracker.reset()
        assert tracker.read(_frame(20), requests)[sensor].quality is Quality.UNKNOWN
        new_group = tracker._groups[_KEY]
        assert new_group.worker is None
        assert new_group.discovery_reader is None
        assert len(created) == 1

        release.set()
        _join(old_group)
        assert old_group.completed is None
        assert old_group.reader is None
        assert old_group.discovery_reader.discovers == 0

        assert tracker.read(_frame(20), requests)[sensor].quality is Quality.UNKNOWN
        _join(new_group)
        current = tracker.read(_frame(30), requests)[sensor]
        assert current.value["count"] == 30
        assert len(created) == 3
        assert new_group.discovery_reader is created[1]
        assert new_group.reader is created[2]
    finally:
        tracker.close()
        release.set()
        for group in (old_group, new_group):
            if group is not None and group.worker is not None:
                _join(group)


def test_hot_reader_construction_failure_preserves_discovery_cache_and_retries():
    now = [0.0]
    discovery = _Reader()
    foreground = _Reader()
    calls = []

    def factory(_name):
        calls.append(threading.get_ident())
        if len(calls) == 1:
            return discovery
        if len(calls) == 2:
            raise RuntimeError("hot templates temporarily unavailable")
        return foreground

    tracker = AdaptiveResourceTracker(factory, retry_interval=.5, clock=lambda: now[0])
    sensor, requests = _requests()
    observed = _frame(10)
    try:
        tracker.read(observed, requests)
        group = tracker._groups[_KEY]
        _join(group)
        failed = tracker.read(observed, requests)[sensor]
        assert failed.quality is Quality.UNKNOWN
        assert "hot templates temporarily unavailable" in failed.detail
        assert group.reader is None

        now[0] = .5
        tracker.read(observed, requests)
        _join(group)
        recovered = tracker.read(_frame(40), requests)[sensor]
        assert recovered.value["count"] == 40
        assert group.discovery_reader is discovery
        assert group.reader is foreground
        assert discovery.discovers == 2 and discovery.hits == 1
        assert len(calls) == 3
    finally:
        tracker.close()


def test_cancelled_partial_refresh_keeps_lanes_private_for_next_attempt():
    now = [0.0]
    started, release = threading.Event(), threading.Event()
    created = []

    class PartialReader(_Reader):
        blocked_once = False

        def discover(self, *args, **kwargs):
            if self.discovers == 1 and not self.blocked_once:
                self.blocked_once = True
                started.set()
                assert release.wait(5)
            result = super().discover(*args, **kwargs)
            if self.discovers > 1:
                result.layout.regions["resource_mana"] = (10, 1, 19, 10)
                result.layout.fingerprint = "complete-layout"
            return result

        def read(self, frame, layout, *, use_ocr=False):
            result = super().read(frame, layout, use_ocr=use_ocr)
            if frame.pixels[0, 0, 0] == 0:
                return {name: Reading(None, Quality.UNKNOWN, "layout hidden")
                        for name in layout.regions}
            return {name: result["resource_health"] for name in layout.regions}

    def factory(_name):
        created.append(PartialReader())
        return created[-1]

    tracker = AdaptiveResourceTracker(factory, retry_interval=.5, clock=lambda: now[0])
    sensor, requests = _requests()
    mana = Sensor(SensorKind.VISUAL, (0, 0, 20, 15), "resource_mana")
    requests[mana] = AdaptiveVisual(_KEY[0], "resource_mana")
    group = None
    try:
        tracker.read(_frame(10), requests)
        group = tracker._groups[_KEY]
        _join(group)
        partial = tracker.read(_frame(20), requests)
        assert partial[sensor].value["count"] == 20
        assert partial[mana].quality is Quality.UNKNOWN
        assert started.wait(5)
        foreground = group.reader
        assert foreground is created[1]

        # Losing current geometry cancels the blocked partial-layout upgrade.
        hidden = tracker.read(_frame(0), requests)
        assert all(reading.quality is Quality.UNKNOWN for reading in hidden.values())
        assert group.layout is None
        release.set()
        _join(group)
        assert group.completed is None

        now[0] = .5
        assert tracker.read(_frame(30), requests)[sensor].quality is Quality.UNKNOWN
        _join(group)
        current = tracker.read(_frame(40), requests)
        assert all(reading.value["count"] == 40 for reading in current.values())
        assert group.reader is foreground
        assert group.discovery_reader is created[0]
        assert len(created) == 2
        assert created[0].reads == foreground.discovers == 0
    finally:
        tracker.close()
        release.set()
        if group is not None and group.worker is not None:
            _join(group)
