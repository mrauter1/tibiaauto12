"""Default readers decode real pixels and preserve explicit OCR settings."""
from pathlib import Path

import cv2
import pytest

from core.perception import Frame
from test_resource_row_geometry import _single_row
from tibia_client.adaptive import FIELDS
from tibia_client.adaptive_runtime import AdaptiveResourceTracker, _default_reader_factory
from tibia_client.adaptive_text import OCRBox
from tibia_client.models import Quality, Sensor, SensorKind
from tibia_client.resource_profiles import apply_adaptive_resource_preset
from tibia_client.resource_reader import NumberReader, ResourceReader
from tibia_client.visual import AdaptiveVisual, VisualProfile


def test_unknown_resource_profile_is_rejected():
    with pytest.raises(ValueError, match="unsupported adaptive reader"):
        _default_reader_factory("not-a-resource-profile")


@pytest.mark.parametrize("injected_ocr", [False, True], ids=["default-ocr", "injected-ocr"])
def test_resource_numeric_ocr_is_wired_opt_in_and_pixel_guarded(monkeypatch, injected_ocr):
    cv2.setNumThreads(1)

    class NumericOCR:
        text = "710"
        calls = 0

        def read_number_boxes(self, image, *, psm=7, timeout=3.0):
            self.calls += 1
            return (OCRBox(self.text, (1, 1, image.shape[1] - 1, image.shape[0] - 1), 0.98),)

    ocr = NumericOCR()
    created = []

    def make_ocr():
        created.append(ocr)
        return ocr

    monkeypatch.setattr("tibia_client.adaptive_text.TesseractOCR", make_ocr)
    reader = ResourceReader(**({"ocr": ocr} if injected_ocr else {}))
    assert reader.ocr is ocr
    assert reader.number_reader._ocr is ocr
    assert created == ([] if injected_ocr else [ocr])
    assert ocr.calls == 0

    # Require OCR to break a template tie on known fixture pixels. This keeps
    # the real resource guards and numeric fallback in the regression path.
    reader.number_reader._ambiguity_margin = 1.0
    frame = _single_row()
    discovery = reader.discover(frame, use_ocr=False)
    assert discovery.layout is not None, discovery.detail
    layout = discovery.layout

    without_ocr = reader.read(frame, layout)[FIELDS[0]]
    assert without_ocr.quality is Quality.UNKNOWN
    assert without_ocr.value is None
    assert ocr.calls == 0

    recovered = reader.read(frame, layout, use_ocr=True)[FIELDS[0]]
    assert recovered.quality is Quality.KNOWN, recovered.detail
    assert recovered.value["count"] == 710
    assert recovered.diagnostics["reader"] == "tesseract_with_template_evidence"
    assert ocr.calls == 1

    ocr.text = "718"
    disagreement = reader.read(frame, layout, use_ocr=True)[FIELDS[0]]
    assert disagreement.quality is Quality.UNKNOWN
    assert disagreement.value is None
    assert ocr.calls == 2

    ocr.text = "710"
    pixels = frame.pixels.copy()
    left, top, right, bottom = layout.regions[FIELDS[0]]
    pixels[top:bottom, left:right] = 42
    obscured = reader.read(Frame(pixels), layout, use_ocr=True)[FIELDS[0]]
    assert obscured.quality is Quality.UNKNOWN
    assert obscured.value is None
    assert ocr.calls == 2


def test_custom_number_reader_keeps_its_own_ocr_backend():
    numeric_ocr, label_ocr = object(), object()
    numbers = NumberReader(ocr=numeric_ocr)
    reader = ResourceReader(number_reader=numbers, ocr=label_ocr)
    assert reader.number_reader is numbers
    assert reader.ocr is label_ocr
    assert numbers._ocr is numeric_ocr


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
