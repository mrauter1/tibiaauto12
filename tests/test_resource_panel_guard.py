"""Generated current-pixel guard diagnostics, not live-capture acceptance data."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
import pytest

from core.perception import Frame
from tibia_client.adaptive import _crop, _ink
from tibia_client.models import Quality
from tools import resource_panel_guard
from tools.resource_reader import CurrentResourceReader


ROOT = Path(__file__).parents[1]


def reader(**kwargs):
    return CurrentResourceReader(**kwargs)


@pytest.fixture(scope="module")
def example():
    cv2.setNumThreads(1)
    with Image.open(ROOT / "tests/fixtures/resources/paulistinha-skills.png") as image:
        panel = np.array(image.convert("RGB"))
    pixels = np.full((max(300, panel.shape[0] + 60), 2 * panel.shape[1] + 180, 3),
                     42, dtype=np.uint8)
    pixels[20:20 + panel.shape[0], 20:20 + panel.shape[1]] = panel
    frame = Frame(pixels)
    model = reader()
    layout = model.discover(frame, use_ocr=False).layout
    assert layout is not None
    assert all(value.quality is Quality.KNOWN for value in model.read(frame, layout).values())
    return panel, frame, layout


def replace_health_with_current_capacity(frame, layout):
    pixels = frame.pixels.copy()
    source = _crop(frame, layout.regions["resource_capacity"])
    yy, xx = np.where(_ink(source))
    glyph = source[yy.min():yy.max() + 1, xx.min():xx.max() + 1]
    left, top, right, bottom = layout.regions["resource_health"]
    background = np.median(pixels[top:bottom, left:right].reshape(-1, 3), axis=0).astype(np.uint8)
    pixels[top:bottom, left:right] = background
    native_top = top + (bottom - top - glyph.shape[0]) // 2
    native_right = right - max(3, round(3 * layout.scale))
    pixels[native_top:native_top + glyph.shape[0], native_right - glyph.shape[1]:native_right] = glyph
    return Frame(pixels)


def add_duplicate(panel, frame):
    pixels = frame.pixels.copy()
    x = panel.shape[1] + 100
    pixels[20:20 + panel.shape[0], x:x + panel.shape[1]] = panel
    return Frame(pixels)


def signature(readings):
    return {field: (reading.quality, reading.value) for field, reading in readings.items()}


def test_reusing_pixel_identical_geometry_and_duplicate_proof_still_decodes_all_rows(example, monkeypatch):
    _, frame, layout = example
    model = reader()
    number_calls, geometry_calls = [], []
    numeric, geometry = model.number_reader.read, model._layout

    def counted_number(image, **kwargs):
        number_calls.append(image.tobytes())
        return numeric(image, **kwargs)

    def counted_geometry(current, anchors):
        geometry_calls.append(current)
        return geometry(current, anchors)

    monkeypatch.setattr(model.number_reader, "read", counted_number)
    monkeypatch.setattr(model, "_layout", counted_geometry)
    first = model.read(frame, layout)
    first_count = len(geometry_calls)
    second = model.read(frame, layout)
    second_count = len(geometry_calls)
    third = model.read(Frame(frame.pixels), layout)
    assert len(number_calls) == 12
    assert first_count < second_count < len(geometry_calls)
    assert signature(first) == signature(second) == signature(third)
    assert model.panel_guard_stats["duplicate_checks"] == 1
    assert model.panel_guard_stats["frame_identity_hits"] == 1
    assert model.panel_guard_stats["duplicate_pixel_hits"] == 1
    assert model.panel_guard_stats["layout_misses"] == 1
    assert model.panel_guard_stats["layout_hits"] == 2


def test_changed_number_is_decoded_from_current_pixels(example):
    _, frame, layout = example
    model = reader()
    assert model.read(frame, layout)["resource_health"].value["count"] == 710
    current = model.read(replace_health_with_current_capacity(frame, layout), layout)
    assert current["resource_health"].quality is Quality.KNOWN
    assert current["resource_health"].value["count"] == 192
    assert model.panel_guard_stats["duplicate_checks"] == 2
    assert model.panel_guard_stats["layout_misses"] == 2


@pytest.mark.parametrize("mutation", ["obscure_value", "extra_prefix", "obscure_label", "move", "resize"])
def test_obscured_moved_or_resized_frame_never_returns_old_health(example, mutation):
    _, frame, layout = example
    model = reader()
    assert model.read(frame, layout)["resource_health"].value["count"] == 710
    pixels = frame.pixels.copy()
    left, top, right, bottom = layout.regions["resource_health"]
    if mutation == "obscure_value":
        pixels[top:bottom, left:right] = 42
    elif mutation == "extra_prefix":
        pixels[top + 3:bottom - 3, left + 1:left + 4] = 220
    elif mutation == "obscure_label":
        anchor = next(anchor for anchor in layout.anchors if anchor.field == "resource_health")
        x, y, right, bottom = anchor.box
        pixels[y:bottom, x:right] = 42
    elif mutation == "move":
        pixels = np.roll(pixels, 11, axis=0)
    else:
        pixels = pixels[:-1]
    current = model.read(Frame(pixels), layout)["resource_health"]
    assert current.quality is Quality.UNKNOWN and current.value is None


@pytest.mark.parametrize("different_numbers", [False, True])
def test_remote_duplicate_invalidates_even_when_its_numbers_differ(example, different_numbers):
    panel, frame, layout = example
    model = reader()
    assert model.read(frame, layout)["resource_health"].quality is Quality.KNOWN
    current = replace_health_with_current_capacity(frame, layout) if different_numbers else frame
    duplicated = add_duplicate(panel, current)
    rejected = model.read(duplicated, layout)
    assert all(reading.quality is Quality.UNKNOWN and reading.value is None
               for reading in rejected.values())
    assert model.panel_guard_stats["duplicate_checks"] == 2
    # Removing the second panel restores readings from this current frame.
    restored = model.read(current, layout)["resource_health"]
    assert restored.quality is Quality.KNOWN
    assert restored.value["count"] == (192 if different_numbers else 710)


def test_a_changed_pixel_anywhere_invalidates_only_the_duplicate_proof(example):
    _, frame, layout = example
    model = reader()
    first = model.read(frame, layout)
    pixels = frame.pixels.copy()
    pixels[-1, -1, 0] ^= 1
    second = model.read(Frame(pixels), layout)
    assert signature(first) == signature(second)
    assert model.panel_guard_stats["duplicate_checks"] == 2
    assert model.panel_guard_stats["duplicate_pixel_hits"] == 0
    assert model.panel_guard_stats["layout_misses"] == 1
    assert model.panel_guard_stats["layout_hits"] == 1


def test_changed_evidence_outside_accepted_number_regions_invalidates_geometry(example):
    _, frame, layout = example
    model = reader()
    first = model.read(frame, layout)
    left, top, _, _ = model.geometry_evidence_boxes(frame, layout.anchors)[0]
    assert not any(x <= left < right and y <= top < bottom
                   for x, y, right, bottom in layout.regions.values())
    pixels = frame.pixels.copy()
    pixels[top, left] = 200
    current = model.read(Frame(pixels), layout)
    assert signature(current) == signature(first)
    assert model.panel_guard_stats["layout_misses"] == 2


def test_geometry_cache_evicts_old_evidence_at_its_fixed_bound(example, monkeypatch):
    _, frame, layout = example
    monkeypatch.setattr(resource_panel_guard, "_LAYOUT_CACHE_LIMIT", 2)
    model = reader()
    left, top, _, _ = model.geometry_evidence_boxes(frame, layout.anchors)[0]
    for index in range(3):
        pixels = frame.pixels.copy()
        pixels[top, left] = 200 + index
        model._layout(Frame(pixels), layout.anchors)
    assert len(model._layout_cache) == 2
    assert model.panel_guard_stats["layout_misses"] == 3
    pixels = frame.pixels.copy()
    pixels[top, left] = 200
    model._layout(Frame(pixels), layout.anchors)
    assert len(model._layout_cache) == 2
    assert model.panel_guard_stats["layout_misses"] == 4


def test_guard_reuse_is_bound_to_complete_layout_identity_not_just_fingerprint(example):
    _, frame, layout = example
    model = reader()
    assert model._has_translated_duplicate(frame, layout) is False
    # Same fingerprint but different evidence identity must not reuse the check.
    changed = replace(layout, bounds=(layout.bounds[0] - 1, *layout.bounds[1:]))
    assert model._has_translated_duplicate(frame, changed) is False
    assert model.panel_guard_stats["duplicate_checks"] == 2


def test_unrestricted_discovery_can_seed_duplicate_proof(example):
    _, frame, _ = example
    model = reader()
    result = model.discover(frame, use_ocr=False)
    assert result.layout is not None
    model.read(frame, result.layout)
    assert model.panel_guard_stats["discovery_seed_hits"] == 1
    assert model.panel_guard_stats["duplicate_checks"] == 0


def test_scoped_discovery_cannot_claim_uniqueness_elsewhere_in_frame(example):
    panel, frame, layout = example
    duplicated = add_duplicate(panel, frame)
    model = reader()
    result = model.discover(duplicated, use_ocr=False, selection=layout.bounds)
    assert result.layout is not None
    assert model.panel_guard_stats["discovery_seed_hits"] == 0
    readings = model.read(duplicated, result.layout)
    assert all(reading.quality is Quality.UNKNOWN for reading in readings.values())
    assert model.panel_guard_stats["duplicate_checks"] == 1


def test_panel_guard_does_not_configure_or_enable_native_font_features(example):
    _, frame, layout = example
    backend = CurrentResourceReader().number_reader
    backend.cache_fonts = False
    backend.calibrate_scale = False

    def unexpected_calibration(*args, **kwargs):
        pytest.fail("panel guard must not configure numeric font calibration")

    backend.configure_calibration = unexpected_calibration
    model = reader(number_reader=backend)
    assert model.read(frame, layout)["resource_health"].quality is Quality.KNOWN
    assert backend.cache_fonts is False and backend.calibrate_scale is False
