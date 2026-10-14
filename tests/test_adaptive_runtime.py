from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import threading
import time
from types import MappingProxyType
from types import SimpleNamespace

import numpy as np

from core.perception import Frame
from tibia_client.adaptive_runtime import AdaptiveResourceTracker, CaptureInputMapping
from tibia_client.models import Quality, Reading, Sensor, SensorKind
from tibia_client.visual import AdaptiveVisual


@dataclass(frozen=True)
class Layout:
    bounds: tuple[int, int, int, int]
    fingerprint: str
    scale: float = 1.0
    regions: dict[str, tuple[int, int, int, int]] = field(
        default_factory=lambda: {"resource_health": (1, 1, 10, 10)}
    )


class Reader:
    def __init__(self, release=None):
        self.release = release
        self.discovers = 0
        self.reads = 0
        self.discovery_ocr = []
        self.read_ocr = []
        self.selections = []

    def discover(self, frame, *, selection=None, use_ocr=False, checkpoint=None):
        self.discovers += 1
        self.discovery_ocr.append(use_ocr)
        self.selections.append(selection)
        if self.release is not None:
            self.release.wait(1)
        if checkpoint is not None:
            checkpoint()
        return SimpleNamespace(layout=Layout((1, 1, 10, 10), "layout-a"), detail="", candidates=())

    def read(self, frame, layout, *, use_ocr=False):
        self.reads += 1
        self.read_ocr.append(use_ocr)
        if frame.pixels[0, 0, 0] == 9:
            return {
                "resource_health": Reading(
                    {"present": True, "position": None, "count": 710, "score": 0.99}
                )
            }
        return {
            "resource_health": Reading(
                None, Quality.UNKNOWN, "resource label moved; rediscovery required"
            )
        }

    def validate(self, frame, layout):
        return frame.pixels[0, 0, 0] == 9


def frame(marker):
    pixels = np.ones((20, 30, 3), dtype=np.uint8)
    pixels[0, 0, 0] = marker
    return Frame(pixels)


def request():
    sensor = Sensor(SensorKind.VISUAL, (0, 0, 20, 15), "resource_health")
    return sensor, {sensor: AdaptiveVisual("paulistinha-skills-v1", "resource_health")}


def test_coordinate_mapping_is_explicit_and_preserves_identity():
    identity = CaptureInputMapping((800, 600), (800, 600))
    scaled = CaptureInputMapping((800, 600), (1600, 1200))

    assert identity.identity
    assert identity.capture_to_input((17, 23)) == (17, 23)
    assert not scaled.identity
    assert scaled.capture_to_input((17, 23)) == (35, 47)


def test_discovery_is_nonblocking_and_only_current_frame_values_are_published():
    release = threading.Event()
    reader = Reader(release)
    tracker = AdaptiveResourceTracker(lambda _name: reader)
    sensor, requests = request()

    first = tracker.read(frame(1), requests)[sensor]
    assert first.quality is Quality.UNKNOWN
    assert "in progress" in first.detail

    release.set()
    tracker._groups[("paulistinha-skills-v1", False)].worker.join(1)
    current = tracker.read(frame(9), requests)[sensor]

    assert current.quality is Quality.KNOWN
    assert current.value["count"] == 710
    assert current.diagnostics["layout_revision"] == "layout-a"
    assert reader.discovers == 1


def test_completed_layout_is_rejected_when_current_frame_guard_fails():
    reader = Reader()
    tracker = AdaptiveResourceTracker(lambda _name: reader)
    sensor, requests = request()

    assert tracker.read(frame(9), requests)[sensor].quality is Quality.UNKNOWN
    tracker._groups[("paulistinha-skills-v1", False)].worker.join(1)
    stale = tracker.read(frame(1), requests)[sensor]

    assert stale.quality is Quality.UNKNOWN
    assert stale.value is None
    assert tracker._groups[("paulistinha-skills-v1", False)].layout is None


def test_relocation_reacquisition_stays_global_while_panels_are_ambiguous():
    now = [0.0]

    class RelocatedReader(Reader):
        def discover(
            self, observed, *, selection=None, use_ocr=False, checkpoint=None
        ):
            self.discovers += 1
            self.selections.append(selection)
            if checkpoint is not None:
                checkpoint()
            if self.discovers == 1:
                return SimpleNamespace(
                    layout=Layout((1, 1, 10, 10), "layout-a"),
                    detail="",
                    candidates=(),
                )
            assert selection is None
            return SimpleNamespace(
                layout=None,
                detail="resource panel is ambiguous",
                candidates=(
                    Layout((1, 1, 10, 10), "layout-b"),
                    Layout((12, 1, 21, 10), "layout-c"),
                ),
            )

    reader = RelocatedReader()
    tracker = AdaptiveResourceTracker(
        lambda _name: reader, retry_interval=0.5, clock=lambda: now[0]
    )
    sensor, requests = request()
    tracker.read(frame(9), requests)
    group = tracker._groups[("paulistinha-skills-v1", False)]
    group.worker.join(1)
    assert tracker.read(frame(9), requests)[sensor].quality is Quality.KNOWN

    # Losing an accepted layout starts a global reacquisition even though the
    # original startup hint still exists.
    assert tracker.read(frame(1), requests)[sensor].quality is Quality.UNKNOWN
    group.worker.join(1)
    assert reader.selections == [(0, 0, 30, 20), None]

    # An ambiguous global result must not fall back to the old local hint on
    # the next retry, where it could auto-select the wrong duplicate.
    tracker.read(frame(1), requests)
    now[0] = 0.5
    tracker.read(frame(1), requests)
    group.worker.join(1)
    assert reader.selections == [(0, 0, 30, 20), None, None]
    assert tracker.read(frame(1), requests)[sensor].quality is Quality.UNKNOWN


def test_requested_fields_share_one_panel_discovery():
    reader = Reader()
    tracker = AdaptiveResourceTracker(lambda _name: reader)
    first, requests = request()
    second = Sensor(SensorKind.VISUAL, (0, 15, 20, 20), "resource_mana")
    requests[second] = AdaptiveVisual("paulistinha-skills-v1", "resource_mana")

    result = tracker.read(frame(9), requests)
    tracker._groups[("paulistinha-skills-v1", False)].worker.join(1)

    assert result[first].quality is Quality.UNKNOWN
    assert result[second].quality is Quality.UNKNOWN
    assert reader.discovers == 1


def test_failed_discovery_is_rate_limited_off_the_scheduler_thread():
    now = [0.0]
    factory_thread = []

    class MissingReader(Reader):
        def discover(self, frame, *, selection=None, use_ocr=False, checkpoint=None):
            self.discovers += 1
            return SimpleNamespace(layout=None, detail="not found", candidates=())

    reader = MissingReader()

    def factory(_name):
        factory_thread.append(threading.get_ident())
        return reader

    owner_thread = threading.get_ident()
    tracker = AdaptiveResourceTracker(factory, retry_interval=0.5, clock=lambda: now[0])
    _sensor, requests = request()

    tracker.read(frame(9), requests)
    tracker._groups[("paulistinha-skills-v1", False)].worker.join(1)
    tracker.read(frame(9), requests)
    assert reader.discovers == 1
    assert tracker.read(frame(9), requests)[_sensor].detail == "not found"
    assert factory_thread and all(item != owner_thread for item in factory_thread)

    now[0] = 0.5
    tracker.read(frame(9), requests)
    tracker._groups[("paulistinha-skills-v1", False)].worker.join(1)
    assert reader.discovers == 2


def test_reader_factory_failure_is_preserved_backed_off_and_releases_worker_slot():
    now = [0.0]
    calls = []

    def failing_factory(_name):
        calls.append(threading.get_ident())
        raise RuntimeError("templates unavailable")

    tracker = AdaptiveResourceTracker(
        failing_factory, retry_interval=0.5, clock=lambda: now[0]
    )
    sensor, requests = request()
    first = tracker.read(frame(9), requests)[sensor]
    assert first.quality is Quality.UNKNOWN
    group = tracker._groups[("paulistinha-skills-v1", False)]
    group.worker.join(1)

    failed = tracker.read(frame(9), requests)[sensor]
    assert "adaptive discovery failed: RuntimeError: templates unavailable" in failed.detail
    assert failed.diagnostics["discovery_failures"] == 1
    assert len(calls) == 1

    now[0] = 0.5
    tracker.read(frame(9), requests)
    group.worker.join(1)
    assert len(calls) == 2


def test_startup_discovery_uses_the_saved_sensor_region_as_a_hint():
    reader = Reader()
    tracker = AdaptiveResourceTracker(lambda _name: reader)
    _sensor, requests = request()

    tracker.read(frame(9), requests)
    tracker._groups[("paulistinha-skills-v1", False)].worker.join(1)

    assert reader.selections == [(0, 0, 30, 20)]


def test_ocr_policy_runs_only_in_background_discovery():
    reader = Reader()
    sensor = Sensor(SensorKind.VISUAL, (0, 0, 20, 15), "resource_health")
    requests = {sensor: AdaptiveVisual("paulistinha-skills-v1", "resource_health", use_ocr=True)}
    owner_thread = threading.get_ident()
    factory_threads = []

    def factory(_name):
        factory_threads.append(threading.get_ident())
        return reader

    tracker = AdaptiveResourceTracker(factory)
    assert tracker.read(frame(9), requests)[sensor].quality is Quality.UNKNOWN
    tracker._groups[("paulistinha-skills-v1", True)].worker.join(1)
    known = tracker.read(frame(9), requests)[sensor]

    assert known.quality is Quality.KNOWN
    assert reader.discovery_ocr == [True]
    assert reader.read_ocr and all(value is False for value in reader.read_ocr)
    assert factory_threads and factory_threads[0] != owner_thread


def test_numeric_ocr_is_async_and_exact_roi_cache_never_reuses_changed_pixels():
    release = threading.Event()

    class OcrReader(Reader):
        numeric_eligible = True

        def read(self, observed, layout, *, use_ocr=False):
            self.reads += 1
            self.read_ocr.append(use_ocr)
            if not use_ocr:
                return {
                    "resource_health": Reading(
                        None,
                        Quality.UNKNOWN,
                        "glyph reader could not resolve count",
                        (
                            {
                                "numeric_eligible": True,
                                "effective_region": layout.regions["resource_health"],
                                "layout_fingerprint": layout.fingerprint,
                            }
                            if self.numeric_eligible
                            else {}
                        ),
                    )
                }
            release.wait(1)
            return {
                "resource_health": Reading(
                    {
                        "present": True,
                        "position": None,
                        "count": int(observed.pixels[2, 2, 0]),
                        "score": 0.8,
                    }
                )
            }

    reader = OcrReader()
    sensor = Sensor(SensorKind.VISUAL, (0, 0, 20, 15), "resource_health")
    requests = {sensor: AdaptiveVisual("paulistinha-skills-v1", "resource_health", use_ocr=True)}
    tracker = AdaptiveResourceTracker(lambda _name: reader)
    original = frame(9)
    tracker.read(original, requests)
    tracker._groups[("paulistinha-skills-v1", True)].worker.join(1)

    started = time.monotonic()
    waiting = tracker.read(original, requests)[sensor]
    assert time.monotonic() - started < 0.1
    assert waiting.quality is Quality.UNKNOWN
    worker = tracker._groups[("paulistinha-skills-v1", True)].ocr_worker
    assert worker is not None and worker.is_alive()

    release.set()
    worker.join(1)
    cached = tracker.read(original, requests)[sensor]
    assert cached.quality is Quality.KNOWN
    assert cached.value["count"] == 1
    assert cached.diagnostics["adaptive_ocr_cache"] == "exact_roi"

    # The byte-identical old ROI is insufficient when current column
    # geometry says the row is no longer eligible (for example, a suffix now
    # extends outside the old region).
    reader.numeric_eligible = False
    structurally_changed = tracker.read(original, requests)[sensor]
    assert structurally_changed.quality is Quality.UNKNOWN
    assert structurally_changed.value is None

    reader.numeric_eligible = True
    changed_pixels = original.pixels.copy()
    changed_pixels[2, 2, 0] = 7
    changed = Frame(changed_pixels)
    changed_reading = tracker.read(changed, requests)[sensor]
    assert changed_reading.quality is Quality.UNKNOWN
    assert changed_reading.value is None


def test_layout_reacquisition_does_not_wait_for_obsolete_ocr():
    ocr_release = threading.Event()

    class SlowOcrReader(Reader):
        def read(self, observed, layout, *, use_ocr=False):
            if not use_ocr:
                if observed.pixels[0, 0, 0] == 9:
                    return {
                        "resource_health": Reading(
                            None,
                            Quality.UNKNOWN,
                            "glyph reader could not resolve count",
                            {
                                "numeric_eligible": True,
                                "effective_region": layout.regions["resource_health"],
                                "layout_fingerprint": layout.fingerprint,
                            },
                        )
                    }
                return {
                    "resource_health": Reading(
                        None, Quality.UNKNOWN, "layout moved"
                    )
                }
            ocr_release.wait(1)
            return {}

    reader = SlowOcrReader()
    sensor = Sensor(SensorKind.VISUAL, (0, 0, 20, 15), "resource_health")
    requests = {
        sensor: AdaptiveVisual(
            "paulistinha-skills-v1", "resource_health", use_ocr=True
        )
    }
    tracker = AdaptiveResourceTracker(lambda _name: reader)
    tracker.read(frame(9), requests)
    group = tracker._groups[("paulistinha-skills-v1", True)]
    group.worker.join(1)
    tracker.read(frame(9), requests)
    assert group.ocr_worker is not None and group.ocr_worker.is_alive()

    moved = tracker.read(frame(1), requests)[sensor]
    assert moved.quality is Quality.UNKNOWN
    assert group.worker is not None and group.worker.is_alive() or reader.discovers >= 2

    ocr_release.set()
    group.ocr_worker.join(1)


def test_reset_cannot_accumulate_ocr_workers_from_old_generations():
    release = threading.Event()

    class ResetReader(Reader):
        def read(self, observed, layout, *, use_ocr=False):
            self.read_ocr.append(use_ocr)
            if use_ocr:
                release.wait(1)
                return {}
            return {
                "resource_health": Reading(
                    None,
                    Quality.UNKNOWN,
                    "glyph reader could not resolve count",
                    {
                        "numeric_eligible": True,
                        "effective_region": layout.regions["resource_health"],
                        "layout_fingerprint": layout.fingerprint,
                    },
                )
            }

    reader = ResetReader()
    sensor = Sensor(SensorKind.VISUAL, (0, 0, 20, 15), "resource_health")
    requests = {
        sensor: AdaptiveVisual(
            "paulistinha-skills-v1", "resource_health", use_ocr=True
        )
    }
    tracker = AdaptiveResourceTracker(lambda _name: reader)
    tracker.read(frame(9), requests)
    old_group = tracker._groups[("paulistinha-skills-v1", True)]
    old_group.worker.join(1)
    tracker.read(frame(9), requests)
    old_ocr = old_group.ocr_worker
    assert old_ocr is not None and old_ocr.is_alive()

    tracker.reset()
    tracker.read(frame(9), requests)
    new_group = tracker._groups[("paulistinha-skills-v1", True)]
    new_group.worker.join(1)
    tracker.read(frame(9), requests)

    assert new_group.ocr_worker is None
    assert reader.read_ocr.count(True) == 1
    release.set()
    old_ocr.join(1)


def test_transient_ocr_failure_is_backed_off_then_recovers_on_same_pixels():
    now = [0.0]

    class RecoveringReader(Reader):
        ocr_calls = 0

        def read(self, observed, layout, *, use_ocr=False):
            if not use_ocr:
                return {
                    "resource_health": Reading(
                        None,
                        Quality.UNKNOWN,
                        "glyph reader could not resolve count",
                        {
                            "numeric_eligible": True,
                            "effective_region": layout.regions["resource_health"],
                            "layout_fingerprint": layout.fingerprint,
                        },
                    )
                }
            self.ocr_calls += 1
            if self.ocr_calls == 1:
                raise RuntimeError("OCR provider restarting")
            return {
                "resource_health": Reading(
                    {
                        "present": True,
                        "position": None,
                        "count": 710,
                        "score": 0.8,
                    }
                )
            }

    reader = RecoveringReader()
    sensor = Sensor(SensorKind.VISUAL, (0, 0, 20, 15), "resource_health")
    requests = {
        sensor: AdaptiveVisual(
            "paulistinha-skills-v1", "resource_health", use_ocr=True
        )
    }
    tracker = AdaptiveResourceTracker(
        lambda _name: reader, retry_interval=0.5, clock=lambda: now[0]
    )
    tracker.read(frame(9), requests)
    group = tracker._groups[("paulistinha-skills-v1", True)]
    group.worker.join(1)
    tracker.read(frame(9), requests)
    group.ocr_worker.join(1)

    assert tracker.read(frame(9), requests)[sensor].quality is Quality.UNKNOWN
    assert reader.ocr_calls == 1
    now[0] = 0.5
    tracker.read(frame(9), requests)
    group.ocr_worker.join(1)
    recovered = tracker.read(frame(9), requests)[sensor]

    assert recovered.quality is Quality.KNOWN
    assert recovered.value["count"] == 710
    assert reader.ocr_calls == 2


def test_partial_layout_upgrades_when_missing_requested_row_reappears():
    now = [0.0]

    class UpgradingReader(Reader):
        complete = False

        def discover(self, observed, *, selection=None, use_ocr=False, checkpoint=None):
            self.discovers += 1
            if checkpoint is not None:
                checkpoint()
            regions = {"resource_health": (1, 1, 10, 10)}
            if self.complete:
                regions["resource_mana"] = (10, 1, 19, 10)
            return SimpleNamespace(
                layout=Layout(
                    (1, 1, 20, 10),
                    "layout-complete" if self.complete else "layout-partial",
                    regions=regions,
                ),
                detail="",
                candidates=(),
            )

        def read(self, observed, layout, *, use_ocr=False):
            return {
                field_name: Reading(
                    {
                        "present": True,
                        "position": None,
                        "count": 710 if field_name == "resource_health" else 1085,
                        "score": 1.0,
                    }
                )
                for field_name in layout.regions
            }

    reader = UpgradingReader()
    health, requests = request()
    mana = Sensor(SensorKind.VISUAL, (0, 0, 20, 15), "resource_mana")
    requests[mana] = AdaptiveVisual("paulistinha-skills-v1", "resource_mana")
    tracker = AdaptiveResourceTracker(
        lambda _name: reader, retry_interval=0.5, clock=lambda: now[0]
    )
    tracker.read(frame(9), requests)
    group = tracker._groups[("paulistinha-skills-v1", False)]
    group.worker.join(1)

    partial = tracker.read(frame(9), requests)
    assert partial[health].quality is Quality.KNOWN
    assert partial[mana].quality is Quality.UNKNOWN
    group.worker.join(1)
    tracker.read(frame(9), requests)  # consume failed partial upgrade

    reader.complete = True
    now[0] = 0.5
    still_partial = tracker.read(frame(9), requests)
    assert still_partial[health].quality is Quality.KNOWN
    group.worker.join(1)
    upgraded = tracker.read(frame(9), requests)

    assert upgraded[health].quality is Quality.KNOWN
    assert upgraded[mana].quality is Quality.KNOWN
    assert upgraded[mana].value["count"] == 1085
    assert upgraded[mana].diagnostics["layout_revision"] == "layout-complete"


def test_adaptive_digit_limit_rejects_only_the_oversized_field():
    reader = Reader()
    original_read = reader.read

    def read(observed, layout, *, use_ocr=False):
        result = dict(original_read(observed, layout, use_ocr=use_ocr))
        result["resource_soul"] = Reading(
            {"present": True, "position": None, "count": 1234, "score": 1.0}
        )
        return MappingProxyType(result)

    reader.read = read
    tracker = AdaptiveResourceTracker(lambda _name: reader)
    health, requests = request()
    soul = Sensor(SensorKind.VISUAL, (0, 0, 20, 15), "resource_soul")
    requests[soul] = AdaptiveVisual("paulistinha-skills-v1", "resource_soul", max_digits=3)

    tracker.read(frame(9), requests)
    tracker._groups[("paulistinha-skills-v1", False)].worker.join(1)
    result = tracker.read(frame(9), requests)

    assert result[health].quality is Quality.KNOWN
    assert result[soul].quality is Quality.UNKNOWN
    assert result[soul].diagnostics["configured_max_digits"] == 3
