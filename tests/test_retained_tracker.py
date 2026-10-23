"""Retained numeric state stays current while discovery runs independently."""
from types import SimpleNamespace
import threading

import numpy as np
import pytest

from core.perception import Frame
from tibia_client.adaptive import Anchor, FIELDS, PanelLayout
from tibia_client.models import Quality, Reading
from tibia_client.retained_resources import load_layout, save_layout
from tibia_client.retained_tracker import RetainedResourceTracker


def frame(value=42):
    return Frame(np.full((12, 20, 3), value, np.uint8))


def layout(identity='saved'):
    anchors = tuple(Anchor(name, (1, 1 + i * 5, 4, 4 + i * 5), 1., 1., 'template')
                    for i, name in enumerate(FIELDS[:2]))
    return PanelLayout((0, 0, 20, 12), anchors,
                       {name: (8, 1, 18, 10) for name in FIELDS}, 1., identity,
                       (20, 12), (bytes([255]) * 9,) * 2)


class Readers:
    def __init__(self):
        self.instances = []
        self.searches = 0
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.visible = lambda current, proposed: set(FIELDS)
        self.proposed = layout('discovered')
        self.shifted = None
        self.relocations = 0
        self.failure = None
        self.read_hook = None

    def __call__(self):
        owner = self

        class Reader:
            def discover(self, current, *, use_ocr, checkpoint):
                assert not use_ocr
                owner.searches += 1
                owner.entered.set()
                assert owner.release.wait(3)
                checkpoint()
                if owner.failure:
                    raise RuntimeError(owner.failure)
                return SimpleNamespace(layout=owner.proposed, detail='search complete')

            def visible_fields(self, current, proposed):
                return owner.visible(current, proposed)

            def relocate(self, current, proposed):
                owner.relocations += 1
                return owner.shifted

            def read(self, current, proposed, *, use_ocr):
                assert not use_ocr
                if owner.read_hook:
                    owner.read_hook()
                visible = self.visible_fields(current, proposed)
                return {name: (Reading({'count': int(current.pixels[0, 0, 0])})
                               if name in visible else Reading(None, Quality.UNKNOWN, 'hidden'))
                        for name in FIELDS}

        reader = Reader()
        self.instances.append(reader)
        return reader


@pytest.fixture
def trackers():
    active = []

    def make(readers=None, **kwargs):
        readers = readers or Readers()
        kwargs.setdefault('cache_path', None)
        tracker = RetainedResourceTracker(readers, **kwargs)
        active.append((tracker, readers))
        return tracker, readers

    yield make
    for tracker, readers in active:
        tracker.close()
        readers.release.set()
        assert tracker.wait(3)


def test_constructor_is_lazy_and_background_candidate_decodes_current_frame(trackers):
    tracker, readers = trackers()
    readers.release.clear()
    assert not readers.instances and not tracker.busy
    assert all(r.quality is Quality.UNKNOWN for r in tracker.read(frame(20)).values())
    assert readers.entered.wait(1)
    assert tracker.busy and len(readers.instances) == 2
    assert readers.instances[0] is not readers.instances[1]
    assert all(r.quality is Quality.UNKNOWN for r in tracker.read(frame(30)).values())
    readers.release.set()
    assert tracker.wait(1)
    assert all(r.value['count'] == 40 for r in tracker.read(frame(40)).values())
    assert tracker.has_layout and tracker.detail == ''
    assert readers.searches == 1


def test_candidate_requires_two_labels_visible_in_current_frame(trackers):
    tracker, readers = trackers()
    tracker.read(frame(20))
    assert tracker.wait(1)
    readers.visible = lambda current, proposed: set(FIELDS[:1])
    readings = tracker.read(frame(30))
    assert not tracker.has_layout
    assert all(r.quality is Quality.UNKNOWN for r in readings.values())


def test_saved_layout_and_relocation_are_loaded_and_persisted(trackers, tmp_path):
    path = tmp_path / 'layout.json'
    saved, shifted = layout(), layout('shifted')
    save_layout(path, saved)
    tracker, readers = trackers(cache_path=path)
    assert tracker.has_layout and not readers.instances
    assert all(r.quality is Quality.UNKNOWN for r in tracker.read(frame()).values())
    assert tracker.wait(1)
    assert all(r.value['count'] == 42 for r in tracker.read(frame()).values())
    assert len(readers.instances) == 1
    assert readers.searches == 0
    readers.shifted = shifted
    readers.visible = lambda current, proposed: set(FIELDS) if proposed == shifted else set()
    tracker.read(frame())
    assert readers.relocations == 1
    assert load_layout(path) == shifted
    assert readers.searches == 0


def test_discovered_layout_is_saved_only_after_current_frame_validation(trackers, tmp_path):
    path = tmp_path / 'layout.json'
    tracker, _ = trackers(cache_path=path)
    tracker.read(frame())
    assert tracker.wait(1)
    assert not path.exists()
    tracker.read(frame(51))
    assert load_layout(path) == layout('discovered')


def test_hidden_rows_retain_layout_and_recovery_waits_two_seconds_then_ten(trackers, tmp_path):
    now = [100.]
    path = tmp_path / 'layout.json'
    save_layout(path, layout())
    tracker, readers = trackers(cache_path=path, clock=lambda: now[0], rediscovery_interval=.1)
    readers.visible = lambda current, proposed: set(FIELDS[:2])
    tracker.read(frame())
    assert tracker.wait(1)
    values = tracker.read(frame())
    assert values[FIELDS[0]].quality is Quality.KNOWN
    assert values[FIELDS[-1]].quality is Quality.UNKNOWN
    now[0] = 110.
    tracker.read(frame())
    assert readers.searches == 0
    readers.visible = lambda current, proposed: set(FIELDS[:1])
    tracker.read(frame())
    now[0] = 111.99
    tracker.read(frame())
    assert readers.searches == 0 and tracker.has_layout
    now[0] = 112.
    tracker.read(frame())
    assert tracker.wait(1) and readers.searches == 1
    now[0] = 121.99
    tracker.read(frame())
    assert readers.searches == 1
    now[0] = 122.
    tracker.read(frame())
    assert tracker.wait(1) and readers.searches == 2


def test_failed_search_reports_recovery_and_retry_respects_custom_interval(trackers):
    now = [10.]
    tracker, readers = trackers(clock=lambda: now[0], rediscovery_interval=15)
    readers.failure = 'OCR unavailable'
    tracker.read(frame())
    assert tracker.wait(1)
    assert tracker.detail == 'Panel recovery: OCR unavailable'
    now[0] = 24.99
    tracker.read(frame())
    assert readers.searches == 1
    now[0] = 25.
    tracker.read(frame())
    assert tracker.wait(1) and readers.searches == 2


def test_reset_cancels_old_candidate_without_overlapping_discovery(trackers):
    tracker, readers = trackers()
    readers.release.clear()
    tracker.read(frame())
    assert readers.entered.wait(1)
    for _ in range(3):
        tracker.reset()
        tracker.read(frame())
    assert readers.searches == 1 and tracker.busy
    readers.release.set()
    assert tracker.wait(1)
    assert not tracker.has_layout and tracker.detail == ''
    assert all(r.quality is Quality.UNKNOWN for r in tracker.read(frame()).values())
    assert tracker.wait(1) and readers.searches == 2
    assert all(r.quality is Quality.KNOWN for r in tracker.read(frame()).values())


def test_reset_during_foreground_read_rejects_late_values(trackers, tmp_path):
    path = tmp_path / 'layout.json'
    save_layout(path, layout())
    tracker, readers = trackers(cache_path=path)
    tracker.read(frame())
    assert tracker.wait(1)
    readers.read_hook = tracker.reset
    values = tracker.read(frame())
    assert all(r.quality is Quality.UNKNOWN for r in values.values())
    assert not tracker.has_layout and not tracker.busy


def test_close_cancels_late_discovery_and_never_restarts(trackers):
    tracker, readers = trackers()
    readers.release.clear()
    tracker.read(frame())
    assert readers.entered.wait(1)
    tracker.close()
    readers.release.set()
    assert tracker.wait(1)
    tracker.reset()
    assert all(r.quality is Quality.UNKNOWN for r in tracker.read(frame()).values())
    assert readers.searches == 1 and not tracker.has_layout


def test_slow_reader_construction_stays_off_foreground_lane(trackers, tmp_path):
    entered, release = threading.Event(), threading.Event()

    class SlowReaders(Readers):
        def __call__(self):
            entered.set()
            assert release.wait(3)
            return super().__call__()

    path = tmp_path / 'layout.json'
    save_layout(path, layout())
    readers = SlowReaders()
    tracker, _ = trackers(readers, cache_path=path)
    try:
        readings = tracker.read(frame())
        assert entered.wait(1)
        assert all(r.quality is Quality.UNKNOWN for r in readings.values())
        assert all(r.quality is Quality.UNKNOWN for r in tracker.read(frame()).values())
        assert tracker.busy and readers.searches == 0
    finally:
        release.set()
    assert tracker.wait(1)
    assert all(r.quality is Quality.KNOWN for r in tracker.read(frame()).values())
    assert len(readers.instances) == 1 and readers.searches == 0


def test_reset_during_cache_write_does_not_wait_or_publish_old_geometry(trackers, tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    path = tmp_path / 'layout.json'
    tracker, _ = trackers(cache_path=path)
    tracker.read(frame())
    assert tracker.wait(1)

    def delayed_save(destination, proposed):
        entered.set()
        assert release.wait(3)
        save_layout(destination, proposed)

    monkeypatch.setattr('tibia_client.retained_resources.save_layout', delayed_save)
    output = []
    worker = threading.Thread(target=lambda: output.append(tracker.read(frame())))
    worker.start()
    try:
        assert entered.wait(1)
        tracker.reset()
        assert not tracker.has_layout
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not path.exists()
    assert all(r.quality is Quality.UNKNOWN for r in output[0].values())


def test_failed_thread_start_does_not_leave_unjoinable_worker(trackers, monkeypatch):
    tracker, readers = trackers()

    class FailedThread(threading.Thread):
        def start(self):
            raise RuntimeError('worker unavailable')

    monkeypatch.setattr('tibia_client.retained_tracker.threading.Thread', FailedThread)
    readings = tracker.read(frame())
    assert all(r.quality is Quality.UNKNOWN for r in readings.values())
    assert tracker.detail == 'Panel recovery: worker unavailable'
    assert not tracker.busy and tracker.wait(0)
    assert readers.searches == 0


@pytest.mark.parametrize('value', [True, 0, -1, float('inf'), float('nan')])
def test_invalid_retry_intervals_rejected(value):
    with pytest.raises(ValueError, match='rediscovery_interval'):
        RetainedResourceTracker(rediscovery_interval=value, cache_path=None)
