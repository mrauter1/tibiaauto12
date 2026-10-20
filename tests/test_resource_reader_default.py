"""Application entry points use the exact benchmark-selected composition."""
from pathlib import Path

import cv2
import pytest

from core.perception import Frame
from test_resource_row_geometry import _single_row
from tibia_client.adaptive import FIELDS
from tibia_client.adaptive_runtime import AdaptiveResourceTracker, _default_reader_factory
from tibia_client.models import Quality, Sensor, SensorKind
from tibia_client.resource_monitor import create_reader
from tibia_client.resource_profiles import apply_adaptive_resource_preset
from tibia_client.resource_reader import NumberReader, ResourceReader
from tibia_client.visual import AdaptiveVisual, VisualProfile
from tools.resource_reader import CurrentNumberReader, CurrentResourceReader


def test_runtime_and_monitor_defaults_are_the_exact_benchmark_classes():
    assert ResourceReader is CurrentResourceReader
    assert NumberReader is CurrentNumberReader
    reader = _default_reader_factory("paulistinha-skills-v1")
    assert type(reader) is ResourceReader
    assert type(reader.number_reader) is NumberReader
    assert type(create_reader()) is ResourceReader
    with pytest.raises(ValueError, match="unsupported adaptive reader"):
        _default_reader_factory("not-a-resource-profile")


def test_profile_setup_and_direct_reads_use_independent_row_recognition():
    cv2.setNumThreads(1)
    frame = _single_row()
    # This guarded single visible row requires the selected composition;
    # the historical baseline requires a three-row constellation.
    root = Path(__file__).parents[1]
    result = apply_adaptive_resource_preset(
        {"bindings": {}, "visual": {}}, frame,
        calibration_size=frame.size, base_dir=root,
    )
    assert result.readings[FIELDS[0]].value["count"] == 710
    assert all(result.readings[name].quality is Quality.UNKNOWN for name in FIELDS[1:])
    profile = VisualProfile.from_data(result.document["visual"], base_path=root)
    assert profile.read(result.bindings[FIELDS[0]], frame).value["count"] == 710
    assert profile.read(result.bindings[FIELDS[1]], frame).quality is Quality.UNKNOWN
    assert all(spec["adaptive"]["reader"] == "paulistinha-skills-v1"
               for spec in result.document["visual"].values())


def test_default_live_tracker_recognizes_partial_panel_and_rejects_obscured_current_row():
    cv2.setNumThreads(1)
    frame = _single_row()
    health = Sensor(SensorKind.VISUAL, (0, 0, frame.width, frame.height), FIELDS[0])
    requested = {health: AdaptiveVisual("paulistinha-skills-v1", FIELDS[0])}
    tracker = AdaptiveResourceTracker()
    group = None
    try:
        initial = tracker.read(frame, requested)[health]
        assert initial.quality is Quality.UNKNOWN
        group = tracker._groups[("paulistinha-skills-v1", False)]
        # Discovery is deliberately outside the latency-sensitive read path.
        # This integration check is not a discovery-performance assertion.
        group.worker.join(60)
        assert not group.worker.is_alive()
        current = tracker.read(frame, requested)[health]
        assert current.quality is Quality.KNOWN, current.detail
        assert current.value["count"] == 710
        assert type(group.reader) is ResourceReader
        assert current.diagnostics["adaptive_reader"] == "paulistinha-skills-v1"

        pixels = frame.pixels.copy()
        left, top, right, bottom = group.layout.anchors[0].box
        pixels[top:bottom, left:right] = 42
        obscured = tracker.read(Frame(pixels), requested)[health]
        assert obscured.quality is Quality.UNKNOWN
        assert obscured.value is None
    finally:
        tracker.close()
        if group is not None and group.worker is not None:
            group.worker.join(10)
            assert not group.worker.is_alive()
