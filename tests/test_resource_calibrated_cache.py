"""Diagnostic cache guards; generated edits are not real-corpus acceptance data."""
from __future__ import annotations

from pathlib import Path
import sys

import cv2
import numpy as np
from PIL import Image
import pytest

from core.perception import Frame
from tibia_client.adaptive import Anchor, FIELDS, PanelLayout, _crop, _ink
from tibia_client.models import Quality
from tools.resource_calibrated_cache import GeometryCacheMixin, _foreground_fast
from tools.resource_reader_experiments import ExperimentalResourceReader, VARIANTS


ROOT = Path(__file__).parents[1]


class CachedReader(GeometryCacheMixin, ExperimentalResourceReader):
    pass


def _reader(**kwargs):
    return CachedReader(VARIANTS["accelerated_fast"], **kwargs)


@pytest.fixture(scope="module")
def example():
    cv2.setNumThreads(1)
    with Image.open(ROOT / "tests/fixtures/resources/paulistinha-skills.png") as image:
        panel = np.array(image.convert("RGB"))
    pixels = np.full((max(300, panel.shape[0] + 60), 2 * panel.shape[1] + 180, 3),
                     42, dtype=np.uint8)
    pixels[20:20 + panel.shape[0], 20:20 + panel.shape[1]] = panel
    frame = Frame(pixels)
    reader = _reader(calibration_enabled=False)
    layout = reader.discover(frame, use_ocr=False).layout
    assert layout is not None
    assert all(item.quality is Quality.KNOWN for item in reader.read(frame, layout).values())
    return panel, frame, layout


def _replace_health_with_capacity(frame, layout):
    pixels = frame.pixels.copy()
    source = _crop(frame, layout.regions["resource_capacity"])
    yy, xx = np.where(_ink(source))
    glyph = source[yy.min():yy.max() + 1, xx.min():xx.max() + 1]
    left, top, right, bottom = layout.regions["resource_health"]
    background = np.median(pixels[top:bottom, left:right].reshape(-1, 3), axis=0).astype(np.uint8)
    pixels[top:bottom, left:right] = background
    native_top = top + (bottom - top - glyph.shape[0]) // 2
    # Native layout has three pixels of padding beyond the common right edge.
    native_right = right - max(3, round(3 * layout.scale))
    pixels[native_top:native_top + glyph.shape[0], native_right - glyph.shape[1]:native_right] = glyph
    return Frame(pixels)


def test_exact_foreground_fast_matches_existing_predicate():
    rng = np.random.default_rng(13)
    pixels = rng.integers(0, 256, (120, 240, 3), dtype=np.uint8)
    assert np.array_equal(_foreground_fast(pixels), _ink(pixels) > 0)


def test_cache_reuses_geometry_but_calls_decoder_for_every_current_row(example):
    _, frame, layout = example
    reader = _reader(calibration_enabled=False)
    calls = []
    delegate = reader.number_reader.read

    def counted(image, **options):
        calls.append(image.tobytes())
        return delegate(image, **options)

    reader.number_reader.read = counted
    first = reader.read(frame, layout)
    second = reader.read(frame, layout)
    third = reader.read(Frame(frame.pixels), layout)
    assert len(calls) == 12
    assert reader.cache_stats["layout_misses"] == 1
    assert reader.cache_stats["layout_hits"] == 2
    assert reader.cache_stats["duplicate_checks"] == 1
    assert reader.cache_stats["duplicate_pixel_hits"] == 1
    assert {k: v.value for k, v in first.items()} == {k: v.value for k, v in second.items()}
    assert {k: v.value for k, v in first.items()} == {k: v.value for k, v in third.items()}


def test_changed_number_rebuilds_geometry_and_reads_new_value(example):
    _, frame, layout = example
    reader = _reader(calibration_enabled=False)
    assert reader.read(frame, layout)["resource_health"].value["count"] == 710
    changed = _replace_health_with_capacity(frame, layout)
    current = reader.read(changed, layout)
    assert reader.cache_stats["layout_misses"] == 2
    assert reader.cache_stats["duplicate_checks"] == 2
    assert current["resource_health"].quality is Quality.KNOWN
    assert current["resource_health"].value["count"] == 192


@pytest.mark.parametrize("mutation", ["obscure_value", "extra_prefix", "obscure_label", "move", "resize"])
def test_current_pixels_cannot_reuse_old_known_health(example, mutation):
    _, frame, layout = example
    reader = _reader(calibration_enabled=False)
    assert reader.read(frame, layout)["resource_health"].value["count"] == 710
    pixels = frame.pixels.copy()
    left, top, right, bottom = layout.regions["resource_health"]
    if mutation == "obscure_value":
        pixels[top:bottom, left:right] = 42
    elif mutation == "extra_prefix":
        # An extra separated prefix lies inside the full observed row evidence.
        pixels[top + 3:bottom - 3, left + 1:left + 4] = 220
    elif mutation == "obscure_label":
        anchor = next(a for a in layout.anchors if a.field == "resource_health")
        x, y, r, b = anchor.box
        pixels[y:b, x:r] = 42
    elif mutation == "move":
        pixels = np.roll(pixels, 11, axis=0)
    else:
        pixels = pixels[:-1]
    result = reader.read(Frame(pixels), layout)
    assert result["resource_health"].quality is Quality.UNKNOWN
    assert result["resource_health"].value is None


def test_duplicate_panel_elsewhere_invalidates_previous_geometry(example):
    panel, frame, layout = example
    reader = _reader(calibration_enabled=False)
    assert reader.read(frame, layout)["resource_health"].quality is Quality.KNOWN
    pixels = frame.pixels.copy()
    x = panel.shape[1] + 100
    pixels[20:20 + panel.shape[0], x:x + panel.shape[1]] = panel
    changed = Frame(pixels)
    result = reader.read(changed, layout)
    assert all(reading.quality is Quality.UNKNOWN for reading in result.values())
    assert reader.cache_stats["duplicate_checks"] == 2


def test_cache_limit_is_bounded_and_new_foreground_outside_roi_invalidates(example):
    _, frame, layout = example
    reader = _reader(calibration_enabled=False, cache_size=2)
    reader.read(frame, layout)
    # These pixels are outside accepted ROIs but inside the evidence envelope.
    boxes = reader.geometry_evidence_boxes(frame, layout.anchors)
    left, top, _, _ = boxes[0]
    for offset in range(3):
        pixels = frame.pixels.copy()
        pixels[top + offset, left] = 200
        reader.read(Frame(pixels), layout)
    assert len(reader._geometry_cache) == 2
    assert reader.cache_stats["layout_misses"] == 4


@pytest.mark.skipif(sys.platform != "win32", reason="native local font rendering uses Windows GDI")
def test_calibration_is_label_based_and_does_not_follow_numeric_values(example):
    _, frame, layout = example
    reader = _reader()
    reader.read(frame, layout)
    first = reader._active_calibration
    assert first is not None
    reader.read(_replace_health_with_capacity(frame, layout), layout)
    assert reader._active_calibration is first
    assert reader.cache_stats["calibration_misses"] == 1
    assert reader.cache_stats["calibration_hits"] == 1


@pytest.mark.skipif(sys.platform != "win32", reason="native local font rendering uses Windows GDI")
@pytest.mark.parametrize("native_scale", [.8, 1.2])
def test_native_label_calibration_recovers_capture_scale_without_numbers(native_scale):
    from tools.resource_native_font import render_native_text
    pixels = np.full((150, 200, 3), 42, dtype=np.uint8)
    anchors = []
    for index, (field, text) in enumerate(zip(FIELDS, ("Hit Points", "Mana", "Soul Points", "Capacity"))):
        mask = render_native_text(text, native_scale, cache=True) >= .5
        height, width = mask.shape
        y = 10 + 25 * index
        pixels[y:y + height, 10:10 + width][mask] = 192
        # Deliberately seed a small scale error to exercise fitting rather than
        # simply repeating the existing anchor's scale estimate.
        anchors.append(Anchor(field, (10, y, 10 + width, y + height), 1,
                              native_scale + .02, "template"))
    frame = Frame(pixels)
    layout = PanelLayout((5, 5, 190, 120), tuple(anchors), {}, native_scale + .02,
                         "generated-calibration-only", frame.size,
                         tuple(_ink(_crop(frame, a.box)).tobytes() for a in anchors))
    reader = _reader()
    result = reader._label_calibration(frame, layout)
    assert result.matched_labels == 4
    assert result.scale == pytest.approx(native_scale, abs=.01)
    assert result.score == pytest.approx(1.0, abs=1e-6)


def test_failed_current_guard_clears_decoder_calibration(example):
    _, frame, layout = example
    reader = _reader(calibration_enabled=False)
    calls = []
    reader.number_reader.configure_calibration = lambda scale, family=None: calls.append((scale, family))
    reader.read(frame, layout)
    pixels = frame.pixels.copy()
    x, y, right, bottom = layout.anchors[0].box
    pixels[y:bottom, x:right] = 42
    reader.read(Frame(pixels), layout)
    assert calls[-1] == (None, None)
    assert reader._active_calibration is None

