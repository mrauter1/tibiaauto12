"""Native numeric tests use locally rendered text, never manufactured benchmark inputs."""
import sys

import numpy as np
import pytest

from tibia_client.models import Quality, Reading
from tools.resource_native_font import (
    NativeFontUnavailable, font_provenance, get_font, render_native_text,
)
from tools.resource_native_numbers import NativeNumberReader


native = pytest.mark.skipif(sys.platform != "win32", reason="native Verdana requires local Windows GDI")


def rendered(text, color=(192, 192, 192), margin=6):
    mask = render_native_text(text)
    rgb = np.full((mask.shape[0] + 2 * margin, mask.shape[1] + 2 * margin, 3), 64, np.uint8)
    rgb[margin:-margin, margin:-margin] = np.rint(
        64 + mask[:, :, None] * (np.asarray(color) - 64)).astype(np.uint8)
    return rgb


@native
@pytest.mark.parametrize("text", ["0", "1", "8", "100", "2,116", "4,112", "1,176", "999,999,999"])
def test_full_native_strings_with_real_advances(text):
    reading = NativeNumberReader().read(rendered(text), scale=.8)
    assert reading.quality is Quality.KNOWN, reading
    assert reading.value == int(text.replace(",", ""))
    assert reading.diagnostics["score"] == pytest.approx(1)
    assert reading.diagnostics["numeric_value_cache"] is False


@native
@pytest.mark.parametrize("color", [(192, 192, 192), (68, 173, 37), (255, 152, 84), (211, 60, 60)])
def test_supported_palette_is_recognized_without_gray_conversion(color):
    reading = NativeNumberReader().read(rendered("1,433", color), scale=.8)
    assert reading.quality is Quality.KNOWN
    assert reading.value == 1433


@native
@pytest.mark.parametrize("side", ["left", "right", "top", "bottom"])
def test_clipped_foreground_never_returns_a_partial_number(side):
    image = rendered("9,888")
    bright = np.where(image[:, :, 0] > 125)
    x1, x2, y1, y2 = bright[1].min(), bright[1].max(), bright[0].min(), bright[0].max()
    clipped = {"left": image[:, x1:], "right": image[:, :x2 + 1],
               "top": image[y1:], "bottom": image[:y2 + 1]}[side]
    result = NativeNumberReader().read(clipped, scale=.8)
    assert result.quality is Quality.UNKNOWN
    assert "clipped" in result.detail


@native
@pytest.mark.parametrize("side", ["prefix", "suffix"])
def test_separate_unexplained_evidence_is_not_silently_dropped(side):
    image = rendered("444,444,444", margin=12)
    x = 3 if side == "prefix" else image.shape[1] - 4
    image[12:15, x] = 192
    result = NativeNumberReader().read(image, scale=.8)
    assert result.quality is Quality.UNKNOWN


@native
def test_digit_tie_abstains_instead_of_using_a_numeric_prior():
    bank = get_font(.8)
    common = np.minimum(bank.glyphs[0].coverage, bank.glyphs[8].coverage)
    image = np.full((common.shape[0] + 12, common.shape[1] + 12, 3), 64, np.uint8)
    image[6:-6, 6:-6] += (common[:, :, None] * 128).astype(np.uint8)
    result = NativeNumberReader().read(image, scale=.8)
    assert result.quality is Quality.UNKNOWN


@native
def test_obscured_middle_does_not_impute_missing_digits():
    image = rendered("2,975")
    image[:, image.shape[1] // 2 - 2:image.shape[1] // 2 + 2] = 64
    assert NativeNumberReader().read(image, scale=.8).quality is Quality.UNKNOWN


@native
def test_unsupported_comma_or_decimal_cannot_be_accepted_as_suffix():
    reader = NativeNumberReader()
    assert reader.read(rendered("1,234"), scale=.8, allow_grouping=False).quality is Quality.UNKNOWN
    assert reader.read(rendered("123.45"), scale=.8).quality is Quality.UNKNOWN
    assert reader.read(rendered("1,23"), scale=.8).quality is Quality.UNKNOWN


@native
def test_cache_changes_computation_reuse_not_readings():
    a, b = get_font(.8, cache=True), get_font(.8, cache=True)
    assert a is b
    assert get_font(.8, cache=False) is not a
    with pytest.raises(ValueError):
        a.glyphs[0].coverage.setflags(write=True)
    reader = NativeNumberReader(cache=True)
    assert reader.read(rendered("130"), scale=.8).value == 130
    assert reader.read(rendered("195"), scale=.8).value == 195
    obscured = np.full_like(rendered("195"), 64)
    assert reader.read(obscured, scale=.8).quality is Quality.UNKNOWN


class StubReader:
    def __init__(self, reading):
        self.reading = reading

    def read(self, *args, **kwargs):
        return self.reading


@native
def test_confident_disagreement_abstains():
    reader = NativeNumberReader(fallback=StubReader(Reading(888, Quality.KNOWN)))
    result = reader.read(rendered("100"), scale=.8)
    assert result.quality is Quality.UNKNOWN
    assert "disagree" in result.detail


@native
def test_strong_native_tie_cannot_be_resolved_by_fallback_choice():
    bank = get_font(.8)
    common = np.minimum(bank.glyphs[0].coverage, bank.glyphs[8].coverage)
    image = np.full((common.shape[0] + 12, common.shape[1] + 12, 3), 64, np.uint8)
    image[6:-6, 6:-6] += (common[:, :, None] * 128).astype(np.uint8)
    result = NativeNumberReader(fallback=StubReader(Reading(0, Quality.KNOWN))).read(image, scale=.8)
    assert result.quality is Quality.UNKNOWN
    assert result.diagnostics["fallback_blocked_by_native_ambiguity"] is True


def test_missing_native_backend_keeps_explicit_fallback(monkeypatch):
    def missing(*args, **kwargs):
        raise NativeFontUnavailable("local font unavailable")
    monkeypatch.setattr("tools.resource_native_numbers.get_font", missing)
    image = np.full((20, 50, 3), 64, np.uint8)
    image[5:12, 10:15] = 192
    result = NativeNumberReader(fallback=StubReader(Reading(123, Quality.KNOWN))).read(image, scale=.8)
    assert result.value == 123
    assert result.diagnostics["native_fallback"] is True
    assert result.diagnostics["native_status"] == "local font unavailable"


@native
def test_provenance_identifies_installed_font_without_bundling_it():
    details = font_provenance()
    assert len(details["source_sha256"]) == 64
    assert details["pixel_size"] == "11"
    assert "no font redistribution" in details["asset_policy"]
