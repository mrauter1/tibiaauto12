from copy import deepcopy
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from core.perception import Frame
from tibia_client.hunt_workflow import HuntWorkflowController
from tibia_client.models import Quality
from tibia_client.resource_profiles import ResourceProfileError, apply_resource_preset
from tibia_client.visual import VisualProfile


FIXTURE = Path(__file__).parent / "fixtures/resources/paulistinha-skills.png"
EXPECTED = {
    "resource_health": 710,
    "resource_mana": 1085,
    "resource_soul": 4,
    "resource_capacity": 192,
}


def reference_frame(offset=(0, 0)):
    with Image.open(FIXTURE) as source:
        panel = source.convert("RGB")
    dx, dy = offset
    canvas = Image.new("RGB", (panel.width + dx + 20, panel.height + dy + 20), (42, 42, 42))
    canvas.paste(panel, offset)
    return Frame(canvas)


def apply(frame, tmp_path, document=None):
    return apply_resource_preset(
        {"bindings": {}, "visual": {}} if document is None else document,
        frame, calibration_size=frame.size, base_dir=tmp_path,
    )


def assert_values(result):
    assert set(result.readings) == set(EXPECTED)
    for key, expected in EXPECTED.items():
        reading = result.readings[key]
        assert reading.quality is Quality.KNOWN, (key, reading.detail)
        assert reading.value["count"] == expected


def test_reference_resources_are_read_through_the_sdk(tmp_path):
    frame = reference_frame()
    original = {"plan": {"id": "keep-my-hunt"}, "bindings": {}, "visual": {}}
    before = deepcopy(original)
    result = apply(frame, tmp_path, original)
    assert_values(result)
    assert original == before
    assert result.document["plan"] == before["plan"]
    profile = VisualProfile(result.document["visual"], base_path=tmp_path)
    for key, sensor in result.bindings.items():
        assert profile.read(sensor, frame) == result.readings[key]
        assert set(result.document["bindings"][key]) == {"name", "region"}
    controller = object.__new__(HuntWorkflowController)
    compiled_profile, compiled_bindings = controller._compile_bindings(result.document, tmp_path)
    assert dict(compiled_bindings) == dict(result.bindings)
    assert compiled_profile.fingerprint == profile.fingerprint


def test_reference_labels_relocate_without_desktop_coordinates(tmp_path):
    original = apply(reference_frame(), tmp_path)
    shifted = apply(reference_frame((173, 211)), tmp_path)
    assert_values(shifted)
    for key, sensor in shifted.bindings.items():
        old = original.bindings[key].region
        assert sensor.region == tuple(n + (173 if index % 2 == 0 else 211)
                                      for index, n in enumerate(old))


def test_preset_rejects_wrong_capture_geometry_without_changing_draft(tmp_path):
    frame = reference_frame()
    document = {"bindings": {}, "visual": {}}
    with pytest.raises(ResourceProfileError, match="differs from calibrated client"):
        apply_resource_preset(document, frame, calibration_size=(1920, 1080), base_dir=tmp_path)
    assert document == {"bindings": {}, "visual": {}}


@pytest.mark.parametrize("section", ["bindings", "visual"])
def test_preset_never_replaces_existing_resource_ids(tmp_path, section):
    document = {"bindings": {}, "visual": {}}
    document[section]["resource_soul"] = {"user": "configuration"}
    before = deepcopy(document)
    with pytest.raises(ResourceProfileError, match="already has configured IDs"):
        apply(reference_frame(), tmp_path, document)
    assert document == before


def test_preset_rejects_a_missing_label(tmp_path):
    pixels = reference_frame().pixels.copy()
    pixels[39:59, :119] = 42  # Cover Soul Points, keeping its number visible.
    with pytest.raises(ResourceProfileError, match="Soul label was not found"):
        apply(Frame(pixels), tmp_path)


def test_preset_rejects_duplicate_panels(tmp_path):
    frame = reference_frame()
    duplicated = Frame(np.concatenate((frame.pixels, frame.pixels), axis=1))
    with pytest.raises(ResourceProfileError, match="appears more than once"):
        apply(duplicated, tmp_path)


def test_preset_can_locate_a_field_without_inventing_an_unreadable_value(tmp_path):
    pixels = reference_frame().pixels.copy()
    pixels[39:58, 116:193] = 72  # Soul number only.
    result = apply(Frame(pixels), tmp_path)
    assert result.readings["resource_soul"].quality is Quality.UNKNOWN
    assert result.readings["resource_soul"].value is None
    assert result.readings["resource_health"].value["count"] == 710


def test_preset_context_stays_required_after_discovery(tmp_path):
    frame = reference_frame()
    result = apply(frame, tmp_path)
    profile = VisualProfile(result.document["visual"], base_path=tmp_path)
    pixels = frame.pixels.copy()
    pixels[39:59, :119] = 42
    reading = profile.read(result.bindings["resource_soul"], Frame(pixels))
    assert reading.quality is Quality.UNKNOWN
    assert reading.value is None


def test_new_soul_value_is_read_instead_of_reusing_reference_value(tmp_path):
    frame = reference_frame()
    result = apply(frame, tmp_path)
    profile = VisualProfile(result.document["visual"], base_path=tmp_path)
    sensor = result.bindings["resource_soul"]
    # Construct an alternate value from screenshot-derived glyphs. This is a
    # parser check, not live-client evidence or an additional real screenshot.
    pixels = frame.pixels.copy()
    left, top, right, bottom = sensor.region
    pixels[top:bottom, left + 112:right] = 72
    assets = Path(__file__).parents[1] / "profiles/paulistinha-skills-v1/digits"
    glyphs = [np.asarray(Image.open(assets / f"{digit}.png").convert("RGB"))
              for digit in "25"]
    x = right - 4 - sum(glyph.shape[1] + 2 for glyph in glyphs)
    for glyph in glyphs:
        height, width = glyph.shape[:2]
        pixels[top + 2:top + 2 + height, x:x + width] = glyph
        x += width + 2
    reading = profile.read(sensor, Frame(pixels))
    assert reading.quality is Quality.KNOWN, reading.detail
    assert reading.value["count"] == 25


def test_preset_assets_resolve_from_an_unrelated_workflow_directory(tmp_path, monkeypatch):
    workflow_dir = tmp_path / "my-hunts"
    workflow_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    frame = reference_frame()
    result = apply(frame, workflow_dir)
    profile = VisualProfile(result.document["visual"], base_path=workflow_dir)
    assert profile.read(result.bindings["resource_mana"], frame).value["count"] == 1085
