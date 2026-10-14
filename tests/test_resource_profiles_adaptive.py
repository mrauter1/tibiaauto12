"""Import-boundary regressions for adaptive resource workflow presets."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from PIL import Image

from core.perception import Frame
from tibia_client.adaptive import FIELDS
from tibia_client.models import Quality, Reading
from tibia_client.resource_profiles import (
    ResourcePresetCandidates,
    ResourceProfileError,
    apply_adaptive_resource_candidate,
    apply_adaptive_resource_preset,
)
from tibia_client.visual import VisualProfile


ROOT = Path(__file__).parents[1]
CONFIRMED_CAPTURE = (
    ROOT / "tests/fixtures/screenshots/originals/user-paulistinha-20261013.png"
)


def _known(count: int) -> Reading:
    return Reading({"present": True, "position": None, "count": count, "score": 1.0})


def test_import_bounds_preview_counts_and_preserves_the_source_document(tmp_path, monkeypatch):
    calls = []
    layout = SimpleNamespace(bounds=(10, 10, 190, 90), fingerprint="fake-layout")

    class Reader:
        def discover(self, frame, *, selection=None, use_ocr=False):
            calls.append(("discover", frame, selection, use_ocr))
            return SimpleNamespace(layout=layout, detail="", candidates=(layout,))

        def read(self, frame, selected, *, use_ocr=False):
            assert selected is layout
            calls.append(("read", frame, use_ocr))
            return {
                "resource_health": _known(710),
                "resource_mana": Reading(None, Quality.UNKNOWN, "unreadable"),
                "resource_soul": _known(1234),
                "resource_capacity": Reading(None, Quality.UNKNOWN, "unreadable"),
            }

    monkeypatch.setattr("tibia_client.adaptive.ResourceReader", Reader)
    document = {
        "bindings": {"existing": {"name": "existing", "region": [0, 0, 2, 2]}},
        "visual": {"existing": {"kind": "template"}},
        "operator_notes": {"keep": ["pending", "edits"]},
    }
    before = deepcopy(document)
    frame = Frame(np.full((100, 200, 3), 42, dtype=np.uint8))

    result = apply_adaptive_resource_preset(
        document,
        frame,
        calibration_size=frame.size,
        base_dir=tmp_path,
        selection=(5, 6, 195, 95),
        use_ocr=True,
    )

    assert document == before
    assert result.document["operator_notes"] == before["operator_notes"]
    assert result.document["bindings"]["existing"] == before["bindings"]["existing"]
    assert result.readings["resource_health"].value["count"] == 710
    assert result.readings["resource_soul"].quality is Quality.UNKNOWN
    assert result.readings["resource_soul"].value is None
    assert result.readings["resource_soul"].diagnostics == {
        "configured_max_digits": 3,
        "observed_count": 1234,
    }
    assert all(
        result.document["visual"][field]["adaptive"] == {
            "reader": "paulistinha-skills-v1",
            "field": field,
            "use_ocr": True,
        }
        for field in FIELDS
    )
    assert calls == [
        ("discover", frame, (5, 6, 195, 95), True),
        ("read", frame, True),
    ]


def test_ambiguous_import_returns_bounded_clickable_choices_from_the_same_frame(
    tmp_path, monkeypatch
):
    layouts = (
        SimpleNamespace(
            bounds=(5, 5, 95, 45),
            regions={field: (50, 5 + index * 8, 90, 12 + index * 8)
                     for index, field in enumerate(FIELDS)},
            scale=1.0,
        ),
        SimpleNamespace(
            bounds=(105, 5, 195, 45),
            regions={field: (150, 5 + index * 8, 190, 12 + index * 8)
                     for index, field in enumerate(FIELDS)},
            scale=1.0,
        ),
    )

    class Reader:
        def discover(self, frame, *, selection=None, use_ocr=False):
            return SimpleNamespace(layout=None, detail="resource panel is ambiguous", candidates=layouts)

        def read(self, frame, layout, *, use_ocr=False):
            return {field: _known(index + 1) for index, field in enumerate(FIELDS)}

    monkeypatch.setattr("tibia_client.adaptive.ResourceReader", Reader)
    frame = Frame(np.zeros((50, 200, 3), dtype=np.uint8), source="offline:ambiguous.png")
    document = {"bindings": {}, "visual": {}, "notes": ["keep"]}
    before = deepcopy(document)

    choices = apply_adaptive_resource_preset(
        document, frame, calibration_size=frame.size, base_dir=tmp_path
    )

    assert isinstance(choices, ResourcePresetCandidates)
    assert choices.frame is frame
    assert [candidate.bounds for candidate in choices.candidates] == [
        (5, 5, 95, 45), (105, 5, 195, 45),
    ]
    assert document == before

    result = apply_adaptive_resource_candidate(document, choices, 1, base_dir=tmp_path)

    assert result.frame is frame
    assert result.document["notes"] == ["keep"]
    assert result.document["bindings"]["resource_health"]["region"] == [105, 5, 195, 45]
    assert document == before


def test_candidate_completion_rechecks_id_collisions(tmp_path, monkeypatch):
    layout = SimpleNamespace(
        bounds=(5, 5, 95, 45),
        regions={field: (50, 5, 90, 12) for field in FIELDS},
        scale=1.0,
    )

    class Reader:
        def discover(self, frame, *, selection=None, use_ocr=False):
            return SimpleNamespace(layout=None, detail="ambiguous", candidates=(layout, layout))

        def read(self, frame, selected, *, use_ocr=False):
            return {field: _known(1) for field in FIELDS}

    monkeypatch.setattr("tibia_client.adaptive.ResourceReader", Reader)
    frame = Frame(np.zeros((50, 100, 3), dtype=np.uint8))
    choices = apply_adaptive_resource_preset(
        {"bindings": {}, "visual": {}}, frame, calibration_size=frame.size, base_dir=tmp_path
    )
    changed = {"bindings": {"resource_mana": {"keep": True}}, "visual": {}}

    with pytest.raises(ResourceProfileError, match="resource_mana"):
        apply_adaptive_resource_candidate(changed, choices, 0, base_dir=tmp_path)

    assert changed["bindings"]["resource_mana"] == {"keep": True}


def test_scaled_confirmed_capture_imports_and_compiles_as_an_sdk_profile(tmp_path):
    with Image.open(CONFIRMED_CAPTURE) as source:
        # Keep the confirmed Skills resource rows and nearby panel structure.
        crop = np.asarray(source.convert("RGB").crop((1450, 20, 1740, 160)))
    scaled = cv2.resize(crop, None, fx=0.75, fy=0.75, interpolation=cv2.INTER_LINEAR)
    frame = Frame(scaled, source="offline:confirmed-scaled-0.75.png")
    document = {"bindings": {}, "visual": {}, "operator_notes": {"keep": True}}

    result = apply_adaptive_resource_preset(
        document,
        frame,
        calibration_size=frame.size,
        base_dir=tmp_path,
    )
    profile = VisualProfile.from_data(result.document["visual"], base_path=tmp_path)

    assert result.document["operator_notes"] == {"keep": True}
    assert result.readings["resource_health"].value["count"] == 710
    assert result.readings["resource_mana"].value["count"] == 1085
    assert result.readings["resource_soul"].value["count"] == 4
    assert not any(
        reading.quality is Quality.KNOWN
        and reading.value["count"] != expected
        for field, expected in {
            "resource_health": 710,
            "resource_mana": 1085,
            "resource_soul": 4,
            "resource_capacity": 192,
        }.items()
        if (reading := result.readings[field]).value is not None
    )
    assert profile["resource_soul"].adaptive is not None
    assert profile["resource_soul"].adaptive.max_digits == 3
    assert profile["resource_soul"].adaptive.use_ocr is False
    assert "use_ocr" not in result.document["visual"]["resource_soul"]["adaptive"]

    # Exercise the public SDK read path once using the compiled adaptive spec.
    reading = profile.read(result.bindings["resource_health"], frame)
    assert reading.quality is Quality.KNOWN
    assert reading.value["count"] == 710
