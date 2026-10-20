from __future__ import annotations

from types import MappingProxyType, SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from core.perception import Frame
from tibia_client.models import Quality, Reading, Sensor, SensorKind
import tibia_client.visual as visual
from tibia_client.visual import VisualProfile, VisualProfileError


GLYPHS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "001", "001"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
}


def _rgb(rows):
    mask = np.asarray([[cell == "1" for cell in row] for row in rows])
    image = np.zeros((*mask.shape, 3), dtype=np.uint8)
    image[mask] = 255
    return image


def _save(path, pixels):
    Image.fromarray(np.asarray(pixels, dtype=np.uint8)).save(path)


def _patterns(tmp_path):
    context = np.asarray(
        [
            [[14, 80, 190], [200, 20, 5], [9, 210, 70]],
            [[230, 120, 8], [40, 60, 220], [180, 11, 160]],
            [[12, 240, 210], [150, 90, 30], [250, 240, 20]],
        ],
        dtype=np.uint8,
    )
    target = np.asarray(
        [
            [[220, 20, 10], [10, 190, 40], [40, 50, 230]],
            [[5, 210, 200], [240, 120, 20], [180, 15, 160]],
        ],
        dtype=np.uint8,
    )
    _save(tmp_path / "context.png", context)
    _save(tmp_path / "target.png", target)
    for number, rows in GLYPHS.items():
        _save(tmp_path / f"{number}.png", _rgb(rows))
    return context, target


def _context_config():
    return {"template": "context.png", "region": [0, 0, 8, 8], "score": 0.999}


def _digit_paths():
    return {str(number): f"{number}.png" for number in range(10)}


def _put(canvas, pixels, x, y):
    height, width = pixels.shape[:2]
    canvas[y : y + height, x : x + width] = pixels


def _grouped_count_config(**changes):
    config = {
        "kind": "count",
        "digits": _digit_paths(),
        "thousands_separator": "comma.png",
        "context": _context_config(),
        "count_region": [8, 0, 58, 9],
        "foreground": [255, 255, 255],
        "tolerance": 0,
        "score": 0.96,
        "ambiguity_margin": 0.02,
        "max_digits": 6,
    }
    config.update(changes)
    return config


def test_adaptive_metadata_is_count_only_strict_and_fingerprinted(tmp_path):
    _patterns(tmp_path)
    _save(tmp_path / "comma.png", _rgb(("1", "1")))
    ordinary = VisualProfile({"mana": _grouped_count_config()}, base_path=tmp_path)
    adaptive_config = _grouped_count_config(
        adaptive={"reader": "paulistinha-skills-v1", "field": "resource_mana"}
    )
    adaptive = VisualProfile({"mana": adaptive_config}, base_path=tmp_path)

    assert adaptive["mana"].adaptive.reader == "paulistinha-skills-v1"
    assert adaptive["mana"].adaptive.field == "resource_mana"
    assert adaptive.fingerprint != ordinary.fingerprint

    ocr_config = _grouped_count_config(
        adaptive={
            "reader": "paulistinha-skills-v1",
            "field": "resource_mana",
            "use_ocr": True,
        }
    )
    ocr = VisualProfile({"mana": ocr_config}, base_path=tmp_path)
    assert ocr["mana"].adaptive.use_ocr is True
    assert ocr.fingerprint != adaptive.fingerprint

    malformed = _grouped_count_config(
        adaptive={"reader": "paulistinha-skills-v1", "field": "other"}
    )
    with pytest.raises(visual.VisualProfileError, match="bundled Skills resource"):
        VisualProfile({"mana": malformed}, base_path=tmp_path)
    invalid_ocr = _grouped_count_config(
        adaptive={
            "reader": "paulistinha-skills-v1",
            "field": "resource_mana",
            "use_ocr": 1,
        }
    )
    with pytest.raises(visual.VisualProfileError, match="use_ocr must be boolean"):
        VisualProfile({"mana": invalid_ocr}, base_path=tmp_path)
    mixed = _grouped_count_config(
        adaptive={
            "reader": "paulistinha-skills-v1",
            "field": "resource_health",
            "use_ocr": True,
        }
    )
    with pytest.raises(visual.VisualProfileError, match="same OCR policy"):
        VisualProfile(
            {"mana": adaptive_config, "health": mixed}, base_path=tmp_path
        )


def test_direct_adaptive_read_honors_selected_panel_hint(tmp_path, monkeypatch):
    _patterns(tmp_path)
    _save(tmp_path / "comma.png", _rgb(("1", "1")))
    profile = VisualProfile(
        {
            "mana": _grouped_count_config(
                adaptive={
                    "reader": "paulistinha-skills-v1",
                    "field": "resource_mana",
                    "use_ocr": True,
                }
            )
        },
        base_path=tmp_path,
    )
    calls = []
    layout = SimpleNamespace(fingerprint="adaptive-layout", regions={"resource_mana": (1, 1, 2, 2)})

    class Reader:
        def discover(self, frame, *, selection=None, use_ocr=False):
            calls.append((frame, selection, use_ocr))
            return SimpleNamespace(layout=layout, detail="", candidates=(layout,))

        def read(self, frame, selected, *, use_ocr=False):
            assert selected is layout
            calls.append((frame, "read", use_ocr))
            return {
                "resource_mana": Reading(
                    {"present": True, "position": None, "count": 1085, "score": 1.0}
                )
            }

    monkeypatch.setattr("tibia_client.resource_reader.ResourceReader", Reader)
    frame = Frame(np.ones((20, 60, 3), dtype=np.uint8))
    reading = profile.read(Sensor(SensorKind.VISUAL, (0, 0, 60, 11), "mana"), frame)

    assert reading.value["count"] == 1085
    assert reading.diagnostics["layout_revision"] == "adaptive-layout"
    assert calls == [(frame, (0, 0, 60, 11), True), (frame, "read", True)]

    narrow = VisualProfile(
        {
            "soul": _grouped_count_config(
                max_digits=3,
                adaptive={
                    "reader": "paulistinha-skills-v1",
                    "field": "resource_mana",
                    "use_ocr": True,
                },
            )
        },
        base_path=tmp_path,
    )
    rejected = narrow.read(
        Sensor(SensorKind.VISUAL, (0, 0, 60, 11), "soul"), frame
    )
    assert rejected.quality is Quality.UNKNOWN
    assert rejected.value is None
    assert rejected.diagnostics["configured_max_digits"] == 3


@pytest.mark.parametrize("key", ["reader", "field"])
@pytest.mark.parametrize("bad", [[], {}, 42, None])
def test_adaptive_identity_validation_reports_profile_errors(tmp_path, key, bad):
    _patterns(tmp_path)
    _save(tmp_path / "comma.png", _rgb(("1", "1")))
    metadata = {"reader": "paulistinha-skills-v1", "field": "resource_mana"}
    metadata[key] = bad
    with pytest.raises(VisualProfileError, match=f"adaptive {key}"):
        VisualProfile({"mana": _grouped_count_config(adaptive=metadata)}, base_path=tmp_path)


@pytest.mark.parametrize("ambiguous", [False, True])
def test_direct_adaptive_read_only_broadens_missing_hint(tmp_path, monkeypatch, ambiguous):
    _patterns(tmp_path)
    _save(tmp_path / "comma.png", _rgb(("1", "1")))
    profile = VisualProfile({"mana": _grouped_count_config(adaptive={
        "reader": "paulistinha-skills-v1", "field": "resource_mana",
    })}, base_path=tmp_path)
    selections = []
    layout = SimpleNamespace(fingerprint="moved", regions={"resource_mana": (1, 1, 2, 2)})

    class Reader:
        def discover(self, frame, *, selection=None, use_ocr=False):
            selections.append(selection)
            if selection is not None:
                return SimpleNamespace(layout=None, detail="ambiguous" if ambiguous else "missing",
                                       candidates=(layout, layout) if ambiguous else ())
            return SimpleNamespace(layout=layout, detail="", candidates=(layout,))

        def read(self, frame, selected, *, use_ocr=False):
            return {"resource_mana": Reading({"count": 1085})}

    monkeypatch.setattr("tibia_client.resource_reader.ResourceReader", Reader)
    frame = Frame(np.ones((20, 60, 3), dtype=np.uint8))
    region = (0, 0, 60, 11)
    reading = profile.read(Sensor(SensorKind.VISUAL, region, "mana"), frame)
    assert selections == ([region] if ambiguous else [region, None])
    assert reading.quality is (Quality.UNKNOWN if ambiguous else Quality.KNOWN)


def _put_count(canvas, text, *, comma_y=5):
    x = 10
    for character in text:
        if character == ",":
            pixels = _rgb(("1", "1"))
            y = comma_y
        elif character == ".":
            pixels = _rgb(("1",))
            y = 6
        else:
            pixels = _rgb(GLYPHS[character])
            y = 2
        _put(canvas, pixels, x, y)
        x += pixels.shape[1] + 1


def test_guarded_template_reports_presence_absence_and_absolute_position(tmp_path):
    context, target = _patterns(tmp_path)
    profile = VisualProfile.from_data(
        {
            "loot.gold": {
                "kind": "template",
                "template": "target.png",
                "context": _context_config(),
                "search_region": [8, 0, 32, 16],
                "score": 0.999,
                "max_matches": 2,
            }
        },
        base_path=tmp_path,
    )
    sensor = Sensor(SensorKind.VISUAL, (10, 5, 42, 21), "loot.gold")
    pixels = np.full((30, 50, 3), 33, dtype=np.uint8)
    _put(pixels, context, 11, 6)
    _put(pixels, target, 20, 8)
    _put(pixels, target, 30, 14)

    reading = profile.read(sensor, Frame(pixels))

    assert reading.quality is Quality.KNOWN
    assert dict(reading.value) == {
        "present": True,
        "position": (21, 9),
        "count": 2,
        "score": pytest.approx(1.0),
    }
    absent = pixels.copy()
    absent[5:21, 18:42] = 33
    absent_reading = profile.read(sensor, Frame(absent))
    assert absent_reading.quality is Quality.KNOWN
    assert absent_reading.value["present"] is False
    assert absent_reading.value["count"] == 0
    assert absent_reading.value["position"] is None


def test_template_absence_is_unknown_without_unique_context(tmp_path):
    context, _ = _patterns(tmp_path)
    profile = VisualProfile(
        {
            "vendor.open": {
                "kind": "template",
                "template": "target.png",
                "context": _context_config(),
                "score": 0.999,
            }
        },
        base_path=tmp_path,
    )
    sensor = Sensor(SensorKind.VISUAL, (0, 0, 32, 16), "vendor.open")
    missing = np.full((20, 40, 3), 31, dtype=np.uint8)
    assert profile.read(sensor, Frame(missing)).quality is Quality.UNKNOWN

    _put(missing, context, 1, 1)
    _put(missing, context, 4, 4)
    ambiguous = profile.read(sensor, Frame(missing))
    assert ambiguous.quality is Quality.UNKNOWN
    assert "ambiguous" in ambiguous.detail


def test_context_overlap_below_iou_limit_remains_ambiguous(tmp_path):
    # The first and last columns are equal, so these exact 3x3 placements can
    # overlap by one column. Their IoU is 0.2 and both must remain candidates.
    context = np.asarray(
        [
            [[10, 20, 30], [40, 50, 60], [10, 20, 30]],
            [[70, 80, 90], [100, 110, 120], [70, 80, 90]],
            [[130, 140, 150], [160, 170, 180], [130, 140, 150]],
        ],
        dtype=np.uint8,
    )
    target = np.asarray([[[240, 10, 20]]], dtype=np.uint8)
    _save(tmp_path / "overlap-context.png", context)
    _save(tmp_path / "single-target.png", target)
    profile = VisualProfile(
        {
            "overlap": {
                "kind": "template",
                "template": "single-target.png",
                "context": {
                    "template": "overlap-context.png",
                    "region": [0, 0, 8, 5],
                    "score": 0.999,
                },
                "search_region": [8, 0, 10, 5],
                "score": 0.999,
            }
        },
        base_path=tmp_path,
    )
    pixels = np.full((8, 14, 3), 25, dtype=np.uint8)
    _put(pixels, context, 1, 1)
    _put(pixels, context, 3, 1)

    reading = profile.read(
        Sensor(SensorKind.VISUAL, (0, 0, 10, 5), "overlap"), Frame(pixels)
    )

    assert reading.quality is Quality.UNKNOWN
    assert reading.value is None
    assert "competing boxes" in reading.detail
    assert "(1, 1, 4, 4)" in reading.detail
    assert "(3, 1, 6, 4)" in reading.detail
    assert dict(reading.diagnostics) == {
        "type": "visual_ambiguity",
        "scope": "context",
        "boxes": ((1, 1, 4, 4), (3, 1, 6, 4)),
        "scores": (pytest.approx(1.0), pytest.approx(1.0)),
    }


def test_target_overflow_reports_bounded_absolute_competing_boxes(tmp_path):
    context, target = _patterns(tmp_path)
    profile = VisualProfile(
        {
            "overflow": {
                "kind": "template",
                "template": "target.png",
                "context": _context_config(),
                "search_region": [8, 0, 32, 16],
                "score": 0.999,
                "max_matches": 1,
            }
        },
        base_path=tmp_path,
    )
    sensor = Sensor(SensorKind.VISUAL, (10, 5, 42, 21), "overflow")
    pixels = np.full((30, 50, 3), 33, dtype=np.uint8)
    _put(pixels, context, 11, 6)
    _put(pixels, target, 20, 8)
    _put(pixels, target, 30, 14)

    reading = profile.read(sensor, Frame(pixels))

    assert reading.quality is Quality.UNKNOWN
    assert reading.value is None
    assert reading.diagnostics["type"] == "visual_ambiguity"
    assert reading.diagnostics["scope"] == "target"
    assert reading.diagnostics["boxes"] == ((20, 8, 23, 10), (30, 14, 33, 16))
    assert len(reading.diagnostics["scores"]) == 2


def test_match_suppression_uses_iou_rule_for_ties_and_noisy_score_map(monkeypatch):
    template = visual._Template(bytes(3 * 3 * 3), 3, 3, "unused")
    scores = np.full((4, 9), 0.51, dtype=np.float32)
    scores[1, 1] = 1.0
    scores[1, 2] = 0.999  # IoU 0.5 with x=1: the same apparent object.
    scores[1, 3] = 1.0  # IoU 0.2 with x=1: a distinct valid placement.
    scores[1, 7] = 1.0
    monkeypatch.setattr(visual, "_score_map", lambda _haystack, _template: scores)

    matches, best = visual._find_matches(
        np.zeros((6, 11, 3), dtype=np.uint8), template, 0.99, limit=2
    )

    # Equal scores use OpenCV's deterministic top-left order. Processing is
    # capped at limit+1 distinct matches so callers can report overflow.
    assert [(match.x, match.y) for match in matches] == [(1, 1), (3, 1), (7, 1)]
    assert best == pytest.approx(1.0)


def test_rgb_template_does_not_accept_same_luminance_impostor(tmp_path):
    context, _ = _patterns(tmp_path)
    target = np.asarray(
        [
            [[255, 0, 0], [0, 255, 0]],
            [[0, 0, 255], [200, 70, 10]],
        ],
        dtype=np.uint8,
    )
    # Every pixel has the same OpenCV grayscale value as the corresponding
    # target pixel, while no RGB pixel is equal.
    impostor = np.asarray(
        [
            [[0, 129, 0], [1, 255, 0]],
            [[0, 49, 0], [0, 173, 0]],
        ],
        dtype=np.uint8,
    )
    _save(tmp_path / "rgb-target.png", target)
    profile = VisualProfile(
        {
            "rgb": {
                "kind": "template",
                "template": "rgb-target.png",
                "context": _context_config(),
                "search_region": [8, 0, 12, 4],
                "score": 0.999,
            }
        },
        base_path=tmp_path,
    )
    pixels = np.full((12, 20, 3), 25, dtype=np.uint8)
    _put(pixels, context, 1, 1)
    _put(pixels, impostor, 8, 1)

    reading = profile.read(
        Sensor(SensorKind.VISUAL, (0, 0, 12, 8), "rgb"), Frame(pixels)
    )

    assert reading.quality is Quality.KNOWN
    assert reading.value["present"] is False
    assert reading.value["count"] == 0
    assert reading.value["score"] < 0.999


def test_guarded_digit_count_distinguishes_zero_from_unknown(tmp_path):
    context, _ = _patterns(tmp_path)
    profile = VisualProfile(
        {
            "supply.count": {
                "kind": "count",
                "digits": _digit_paths(),
                "context": _context_config(),
                "count_region": [8, 0, 30, 9],
                "foreground": [255, 255, 255],
                "tolerance": 0,
                "score": 0.96,
                "ambiguity_margin": 0.02,
                "max_digits": 3,
            }
        },
        base_path=tmp_path,
    )
    sensor = Sensor(SensorKind.VISUAL, (3, 2, 35, 13), "supply.count")

    zero = np.full((18, 40, 3), 25, dtype=np.uint8)
    _put(zero, context, 4, 3)
    _put(zero, _rgb(GLYPHS["0"]), 14, 4)
    reading = profile.read(sensor, Frame(zero))
    assert reading.quality is Quality.KNOWN
    assert reading.value["count"] == 0
    assert reading.value["present"] is True
    assert reading.value["position"] == (5, 4)

    empty = zero.copy()
    empty[2:11, 11:33] = 25
    unknown = profile.read(sensor, Frame(empty))
    assert unknown.quality is Quality.UNKNOWN
    assert unknown.value is None


def test_count_rejects_clipped_and_ambiguous_digits(tmp_path):
    context, _ = _patterns(tmp_path)
    paths = _digit_paths()
    config = {
        "kind": "count",
        "digits": paths,
        "context": _context_config(),
        "count_region": [8, 0, 30, 9],
        "foreground": [255, 255, 255],
        "tolerance": 0,
        "score": 0.90,
        "ambiguity_margin": 0.02,
        "max_digits": 3,
    }
    profile = VisualProfile({"count": config}, base_path=tmp_path)
    sensor = Sensor(SensorKind.VISUAL, (0, 0, 32, 11), "count")
    clipped = np.full((14, 36, 3), 25, dtype=np.uint8)
    _put(clipped, context, 1, 1)
    _put(clipped, _rgb(GLYPHS["4"]), 8, 2)
    reading = profile.read(sensor, Frame(clipped))
    assert reading.quality is Quality.UNKNOWN
    assert "edge" in reading.detail

    _save(tmp_path / "7.png", _rgb(GLYPHS["1"]))
    ambiguous_profile = VisualProfile({"count": config}, base_path=tmp_path)
    ambiguous = clipped.copy()
    ambiguous[0:9, 8:30] = 25
    _put(ambiguous, _rgb(GLYPHS["1"]), 12, 2)
    reading = ambiguous_profile.read(sensor, Frame(ambiguous))
    assert reading.quality is Quality.UNKNOWN
    assert "ambiguous digit" in reading.detail

    boundary_config = dict(config, ambiguity_margin=0.06640625)
    boundary_profile = VisualProfile({"count": boundary_config}, base_path=tmp_path)
    boundary = clipped.copy()
    boundary[0:9, 8:30] = 25
    _put(boundary, _rgb(GLYPHS["3"]), 12, 2)
    reading = boundary_profile.read(sensor, Frame(boundary))
    assert reading.quality is Quality.UNKNOWN
    assert "ambiguous digit" in reading.detail


def test_count_accepts_strict_thousands_groups_and_ungrouped_integers(tmp_path):
    context, _ = _patterns(tmp_path)
    _save(tmp_path / "comma.png", _rgb(("1", "1")))
    profile = VisualProfile(
        {"mana": _grouped_count_config()}, base_path=tmp_path
    )
    sensor = Sensor(SensorKind.VISUAL, (0, 0, 60, 11), "mana")

    grouped = np.full((14, 64, 3), 25, dtype=np.uint8)
    _put(grouped, context, 1, 1)
    _put_count(grouped, "1,085")
    reading = profile.read(sensor, Frame(grouped))
    assert reading.quality is Quality.KNOWN
    assert reading.value["count"] == 1085
    assert reading.value["score"] == pytest.approx(1.0)

    ungrouped = np.full((14, 64, 3), 25, dtype=np.uint8)
    _put(ungrouped, context, 1, 1)
    _put_count(ungrouped, "1085")
    reading = profile.read(sensor, Frame(ungrouped))
    assert reading.quality is Quality.KNOWN
    assert reading.value["count"] == 1085


def test_grouped_count_matches_short_variant_at_its_asset_height(tmp_path):
    context, _ = _patterns(tmp_path)
    short_one = _rgb(("1", "1"))
    short_comma = _rgb(("1",))
    _save(tmp_path / "1-short.png", short_one)
    _save(tmp_path / "comma-short.png", short_comma)
    profile = VisualProfile(
        {
            "mana": _grouped_count_config(
                digit_variants={"1": ["1-short.png"]},
                thousands_separator="comma-short.png",
            )
        },
        base_path=tmp_path,
    )
    sensor = Sensor(SensorKind.VISUAL, (0, 0, 60, 11), "mana")

    for text in ("1,111", "1111"):
        pixels = np.full((14, 64, 3), 25, dtype=np.uint8)
        _put(pixels, context, 1, 1)
        x = 10
        for character in text:
            glyph = short_comma if character == "," else short_one
            _put(pixels, glyph, x, 5)
            x += glyph.shape[1] + 1

        reading = profile.read(sensor, Frame(pixels))

        assert reading.quality is Quality.KNOWN
        assert reading.value["count"] == 1111


@pytest.mark.parametrize(
    "text",
    (",123", "123,", "1,,085", "12,34", "1234,567", "1,23,456", "0,123"),
)
def test_count_rejects_malformed_thousands_groups_and_leading_zero(tmp_path, text):
    context, _ = _patterns(tmp_path)
    _save(tmp_path / "comma.png", _rgb(("1", "1")))
    profile = VisualProfile(
        {"mana": _grouped_count_config()}, base_path=tmp_path
    )
    pixels = np.full((14, 64, 3), 25, dtype=np.uint8)
    _put(pixels, context, 1, 1)
    _put_count(pixels, text)

    reading = profile.read(
        Sensor(SensorKind.VISUAL, (0, 0, 60, 11), "mana"), Frame(pixels)
    )

    assert reading.quality is Quality.UNKNOWN
    assert reading.value is None


def test_separator_geometry_rejects_tiny_blob_and_clipped_comma(tmp_path):
    context, _ = _patterns(tmp_path)
    _save(tmp_path / "comma.png", _rgb(("1", "1")))
    profile = VisualProfile(
        {"mana": _grouped_count_config()}, base_path=tmp_path
    )
    sensor = Sensor(SensorKind.VISUAL, (0, 0, 60, 11), "mana")

    tiny = np.full((14, 64, 3), 25, dtype=np.uint8)
    _put(tiny, context, 1, 1)
    _put_count(tiny, "1.085")
    reading = profile.read(sensor, Frame(tiny))
    assert reading.quality is Quality.UNKNOWN
    assert "ambiguous glyph" in reading.detail

    clipped = np.full((14, 64, 3), 25, dtype=np.uint8)
    _put(clipped, context, 1, 1)
    _put_count(clipped, "1,085", comma_y=8)
    reading = profile.read(sensor, Frame(clipped))
    assert reading.quality is Quality.UNKNOWN
    assert "edge" in reading.detail


def test_separator_ambiguity_with_distinct_digit_is_unknown(tmp_path):
    context, _ = _patterns(tmp_path)
    ambiguous = _rgb(("01", "11", "01", "01"))
    _save(tmp_path / "comma.png", ambiguous)
    _save(tmp_path / "1-short.png", ambiguous)
    profile = VisualProfile(
        {
            "mana": _grouped_count_config(
                digit_variants={"1": ["1-short.png"]}
            )
        },
        base_path=tmp_path,
    )
    pixels = np.full((14, 64, 3), 25, dtype=np.uint8)
    _put(pixels, context, 1, 1)
    _put(pixels, _rgb(GLYPHS["1"]), 10, 2)
    _put(pixels, ambiguous, 14, 3)
    _put(pixels, _rgb(GLYPHS["0"]), 17, 2)
    _put(pixels, _rgb(GLYPHS["8"]), 21, 2)
    _put(pixels, _rgb(GLYPHS["5"]), 25, 2)

    reading = profile.read(
        Sensor(SensorKind.VISUAL, (0, 0, 60, 11), "mana"), Frame(pixels)
    )

    assert reading.quality is Quality.UNKNOWN
    assert "ambiguous glyph" in reading.detail


def test_digit_variants_are_one_digit_class_and_match_alternate_shape(tmp_path):
    context, _ = _patterns(tmp_path)
    alternate = _rgb(("010", "010", "010", "010", "010"))
    _save(tmp_path / "1-alt.png", alternate)
    config = {
        "kind": "count",
        "digits": _digit_paths(),
        "digit_variants": {"1": ["1.png", "1-alt.png"]},
        "context": _context_config(),
        "count_region": [8, 0, 30, 9],
        "foreground": [255, 255, 255],
        "tolerance": 0,
        "score": 0.99,
        "ambiguity_margin": 0.02,
        "max_digits": 3,
    }
    profile = VisualProfile({"count": config}, base_path=tmp_path)
    pixels = np.full((14, 36, 3), 25, dtype=np.uint8)
    _put(pixels, context, 1, 1)
    _put(pixels, alternate, 12, 2)

    reading = profile.read(
        Sensor(SensorKind.VISUAL, (0, 0, 32, 11), "count"), Frame(pixels)
    )

    assert reading.quality is Quality.KNOWN
    assert reading.value["count"] == 1

    duplicate_match = np.full((14, 36, 3), 25, dtype=np.uint8)
    _put(duplicate_match, context, 1, 1)
    _put(duplicate_match, _rgb(GLYPHS["1"]), 12, 2)
    reading = profile.read(
        Sensor(SensorKind.VISUAL, (0, 0, 32, 11), "count"),
        Frame(duplicate_match),
    )
    assert reading.quality is Quality.KNOWN
    assert reading.value["count"] == 1


def test_separator_and_variants_are_strict_and_fingerprinted(tmp_path):
    _patterns(tmp_path)
    _save(tmp_path / "comma.png", _rgb(("1", "1")))
    _save(tmp_path / "comma-wide.png", _rgb(("11", "10")))
    _save(tmp_path / "1-alt.png", _rgb(("010", "010", "010", "010", "010")))
    legacy_config = {
        "kind": "count",
        "digits": _digit_paths(),
        "context": _context_config(),
        "count_region": [8, 0, 30, 9],
        "foreground": [255, 255, 255],
        "tolerance": 0,
        "score": 0.90,
        "ambiguity_margin": 0.02,
        "max_digits": 3,
    }
    legacy = VisualProfile({"count": legacy_config}, base_path=tmp_path)
    assert legacy.fingerprint == (
        "9e2f104648a0e9c611da500bbac176bd7ec64d7b6739be89545b1c619bf038a3"
    )
    plain = VisualProfile(
        {"count": _grouped_count_config(thousands_separator="comma.png")},
        base_path=tmp_path,
    )
    wide = VisualProfile(
        {"count": _grouped_count_config(thousands_separator="comma-wide.png")},
        base_path=tmp_path,
    )
    variant = VisualProfile(
        {
            "count": _grouped_count_config(
                thousands_separator="comma.png",
                digit_variants={"1": ["1-alt.png"]},
            )
        },
        base_path=tmp_path,
    )
    assert plain.fingerprint != wide.fingerprint
    assert plain.fingerprint != variant.fingerprint

    template = {
        "kind": "template",
        "template": "target.png",
        "context": _context_config(),
        "thousands_separator": "comma.png",
    }
    with pytest.raises(VisualProfileError, match="count-only"):
        VisualProfile({"bad": template}, base_path=tmp_path)
    template.pop("thousands_separator")
    template["digit_variants"] = {"1": ["1-alt.png"]}
    with pytest.raises(VisualProfileError, match="count-only"):
        VisualProfile({"bad": template}, base_path=tmp_path)

    with pytest.raises(VisualProfileError, match="non-empty list"):
        VisualProfile(
            {"bad": _grouped_count_config(digit_variants={"1": []})},
            base_path=tmp_path,
        )
    with pytest.raises(VisualProfileError, match="non-empty list"):
        VisualProfile(
            {
                "bad": _grouped_count_config(
                    digit_variants={"1": {"asset": "1-alt.png"}}
                )
            },
            base_path=tmp_path,
        )
    with pytest.raises(VisualProfileError, match="non-empty list"):
        VisualProfile(
            {"bad": _grouped_count_config(digit_variants={"1": {"1-alt.png"}})},
            base_path=tmp_path,
        )
    with pytest.raises(VisualProfileError, match="only the keys"):
        VisualProfile(
            {"bad": _grouped_count_config(digit_variants={"10": ["1-alt.png"]})},
            base_path=tmp_path,
        )


def test_profile_is_fingerprintable_immutable_and_strict(tmp_path):
    _patterns(tmp_path)
    data = {
        "corpse": {
            "kind": "template",
            "template": "target.png",
            "context": _context_config(),
        }
    }
    first = VisualProfile(data, base_path=tmp_path)
    second = VisualProfile(data, base_path=tmp_path)
    assert first.fingerprint == second.fingerprint
    assert first.spec_ids == ("corpse",)
    assert len(first.fingerprint) == 64
    assert isinstance(first._specs, MappingProxyType)
    with pytest.raises(TypeError):
        first._specs["new"] = first["corpse"]

    with pytest.raises(VisualProfileError, match="unknown field"):
        VisualProfile(
            {
                "bad": {
                    "kind": "template",
                    "template": "target.png",
                    "context": _context_config(),
                    "probability": 0.9,
                }
            },
            base_path=tmp_path,
        )


@pytest.mark.parametrize(
    "change",
    (
        {"score": 0.49},
        {"context": {"template": "context.png", "region": [0, 0, 8, 8], "score": 0.49}},
        {"ambiguity_margin": 0.0},
    ),
)
def test_profile_rejects_unsafe_evidence_thresholds(tmp_path, change):
    _patterns(tmp_path)
    config = {
        "kind": "template",
        "template": "target.png",
        "context": _context_config(),
    }
    config.update(change)

    with pytest.raises(VisualProfileError):
        VisualProfile({"unsafe": config}, base_path=tmp_path)


def test_direct_adaptive_read_recovers_a_row_missing_during_setup(tmp_path):
    from pathlib import Path
    from tibia_client.resource_reader import ResourceReader
    from tibia_client.resource_profiles import apply_adaptive_resource_preset

    path = Path(__file__).parents[1] / 'tests/fixtures/resources/paulistinha-skills.png'
    with Image.open(path) as image:
        pixels = np.asarray(image.convert('RGB'))
    complete = Frame(pixels)
    reader = ResourceReader()
    original = reader.discover(complete)
    capacity = next(a for a in original.layout.anchors if a.field == 'resource_capacity')
    partial = pixels.copy()
    left, top, right, bottom = capacity.box
    partial[top:bottom, left:right] = 42
    source = Frame(partial)
    result = apply_adaptive_resource_preset(
        {'bindings': {}, 'visual': {}}, source,
        calibration_size=source.size, base_dir=tmp_path,
    )
    assert result.readings['resource_capacity'].quality is Quality.UNKNOWN
    profile = VisualProfile(result.document['visual'], base_path=tmp_path)
    restored = profile.read(result.bindings['resource_capacity'], complete)
    assert restored.quality is Quality.KNOWN
    assert restored.value['count'] == 192


def test_adaptive_fingerprint_covers_actual_bundle_not_inactive_count_options(tmp_path, monkeypatch):
    from pathlib import Path
    import shutil

    _patterns(tmp_path)
    _save(tmp_path / 'comma.png', _rgb(('1', '1')))
    root = Path(__file__).parents[1]
    bundle = tmp_path / 'bundle'
    shutil.copytree(root / 'profiles/paulistinha-skills-v1', bundle)
    monkeypatch.setattr(visual, 'project_path', lambda _path: str(bundle))
    config = _grouped_count_config(adaptive={
        'reader': 'paulistinha-skills-v1', 'field': 'resource_mana',
    })
    before = VisualProfile({'mana': config}, base_path=tmp_path).fingerprint
    config['score'] = 0.999
    config['context']['score'] = 0.9
    assert VisualProfile({'mana': config}, base_path=tmp_path).fingerprint == before

    # The alternate anchor bank is used at runtime even when this individual
    # spec's historical context template points somewhere else.
    native = bundle / 'labels/native/soul.png'
    with Image.open(native) as image:
        pixels = np.asarray(image.convert('RGB')).copy()
    pixels[0, 0] = (255, 0, 0)
    _save(native, pixels)
    after = VisualProfile({'mana': config}, base_path=tmp_path).fingerprint
    assert after != before
    # Later rendering banks and separator variants also invalidate cached
    # readings even if the legacy JSON points only to the original bank.
    alternate = bundle / 'labels/alternate/test/soul.png'
    alternate.parent.mkdir(parents=True)
    _save(alternate, pixels)
    alternate_fingerprint = VisualProfile({'mana': config}, base_path=tmp_path).fingerprint
    assert alternate_fingerprint != after
    _save(bundle / 'comma-test.png', _rgb(('1', '1')))
    separator_fingerprint = VisualProfile({'mana': config}, base_path=tmp_path).fingerprint
    assert separator_fingerprint != alternate_fingerprint
    config['max_digits'] = 5
    assert VisualProfile({'mana': config}, base_path=tmp_path).fingerprint != separator_fingerprint
