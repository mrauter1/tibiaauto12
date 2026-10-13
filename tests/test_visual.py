from __future__ import annotations

from types import MappingProxyType

import numpy as np
from PIL import Image
import pytest

from core.perception import Frame
from tibia_client.models import Quality, Sensor, SensorKind
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
