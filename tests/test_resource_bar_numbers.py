"""Caption component transfer checks and generated guards, not a full benchmark."""
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
import pytest

from core.perception import Frame
from tibia_client.adaptive_text import _ink
from tools.resource_bar_evidence import BarEvidence, frame_token
from tools.resource_bar_numbers import BarCaptionReader


ROOT = Path(__file__).parents[1]
ORIGINALS = ROOT / "tests/fixtures/screenshots/originals"
cv2.setNumThreads(1)


def caption(text="710/1085", *, background=(24, 150, 24), foreground=(192, 250, 192)):
    """Synthetic composition of the existing licensed project profile digits."""
    pieces = []
    for token in text:
        if token == "/":
            glyph = np.zeros((14, 7), bool)
            for y in range(14):
                x = round((13 - y) * .4)
                glyph[y, x:x + 2] = True
            pieces.append((glyph, 3))
        else:
            path = ROOT / "profiles/paulistinha-skills-v1/digits" / (token + ".png")
            mask = _ink(np.asarray(Image.open(path).convert("RGB")))
            yy, xx = np.where(mask)
            mask = mask[yy.min():yy.max() + 1, xx.min():xx.max() + 1]
            pieces.append((mask, 15 - mask.shape[0]))
    width = sum(item.shape[1] for item, _ in pieces) + 2 * (len(pieces) - 1) + 16
    image = np.empty((21, width, 3), np.uint8)
    image[:] = background
    x = 8
    for mask, y in pieces:
        region = image[y:y + mask.shape[0], x:x + mask.shape[1]]
        region[mask] = foreground
        x += mask.shape[1] + 2
    return image


def evidence(frame):
    return BarEvidence(field="resource_health", fraction_interval=(0.0, 1.0),
                       bounds=(0, 0, frame.width, frame.height),
                       caption_bounds=(1, 1, frame.width - 1, frame.height - 1),
                       direction="left_to_right", frame_timestamp=frame.captured_at,
                       layout_fingerprint="current-guarded-layout", frame_token=frame_token(frame))


@pytest.mark.parametrize("background,foreground", [
    ((24, 150, 24), (192, 250, 192)),
    ((15, 70, 180), (192, 220, 250)),
    ((48, 48, 48), (192, 192, 192)),
])
def test_complete_caption_is_decoded_on_both_bar_colors_and_empty_track(background, foreground):
    reader = BarCaptionReader()
    result = reader.parse_caption(caption(background=background, foreground=foreground))
    assert result is not None, reader.last_diagnostics
    assert (result.current, result.maximum) == (710, 1085)


@pytest.mark.parametrize("side", ["left", "right", "top", "bottom"])
def test_any_caption_foreground_at_the_boundary_abstains(side):
    image = caption()
    yy, xx = np.where(np.min(image, axis=2) >= 125)
    if side == "left":
        image = image[:, xx.min():]
    elif side == "right":
        image = image[:, :xx.max() + 1]
    elif side == "top":
        image = image[yy.min():]
    else:
        image = image[:yy.max() + 1]
    reader = BarCaptionReader()
    assert reader.parse_caption(image) is None
    assert "boundary" in reader.last_diagnostics["detail"]


@pytest.mark.parametrize("text", ["7101085", "/1085", "710/", "710/1085/2000", "1085/710", "710/0"])
def test_missing_duplicate_or_invalid_current_max_caption_abstains(text):
    assert BarCaptionReader().parse_caption(caption(text)) is None


@pytest.mark.parametrize("kind", ["cut_slash", "vertical_slash", "reverse_slash", "debris", "erase_digit"])
def test_obscured_separator_unexplained_foreground_and_missing_digit_abstain(kind):
    image = caption()
    mask = np.min(image, axis=2) >= 125
    # Slash is the only component extending below the digit baseline.
    columns = np.flatnonzero(mask[16])
    slash_left = int(columns.min())
    # The slash's bottom pixels are its leftmost pixels.
    if kind == "cut_slash":
        image[10, slash_left:slash_left + 7] = (24, 150, 24)
    elif kind == "vertical_slash":
        image[3:17, slash_left:slash_left + 7] = (24, 150, 24)
        image[3:17, slash_left + 3:slash_left + 5] = (192, 250, 192)
    elif kind == "reverse_slash":
        image[3:17, slash_left:slash_left + 7] = image[3:17, slash_left:slash_left + 7, :][:, ::-1]
    elif kind == "debris":
        image[9:11, 3:5] = (192, 250, 192)
    else:
        # Remove an entire leading digit; a readable suffix must not become a
        # complete caption merely because it is independently a valid number.
        image[:, 8:16] = (24, 150, 24)
    assert BarCaptionReader().parse_caption(image) is None


def test_caption_must_remain_centered_in_verified_geometry():
    image = np.pad(caption(), ((0, 0), (8, 0), (0, 0)), constant_values=24)
    assert BarCaptionReader().parse_caption(image) is None


def test_success_is_bound_to_current_frame_and_layout_without_caching_values():
    reader = BarCaptionReader()
    first = Frame(caption(), captured_at=10.0)
    proof = evidence(first)
    maximum = reader.read(first, proof)
    assert maximum is not None, reader.last_diagnostics
    assert maximum.value == 1085 and maximum.current == 710
    assert maximum.independently_verified
    assert maximum.frame_token == proof.frame_token
    assert maximum.layout_fingerprint == proof.layout_fingerprint
    changed = Frame(caption("710/2000"), captured_at=10.1)
    assert reader.read(changed, proof) is None
    refreshed = reader.read(changed, evidence(changed))
    assert refreshed is not None and refreshed.value == 2000


def test_same_timestamp_different_pixels_do_not_reuse_maximum():
    reader = BarCaptionReader()
    first = Frame(caption(), captured_at=10.0)
    changed = Frame(caption("710/2000"), captured_at=10.0)
    assert reader.read(changed, evidence(first)) is None


def test_caption_maximum_requires_a_consistent_narrow_bar_interval():
    frame = Frame(caption())
    fraction = 710 / 1085
    proof = replace(evidence(frame), fraction_interval=(fraction - .001, fraction + .001))
    maximum = BarCaptionReader().read(frame, proof)
    assert maximum is not None and maximum.value == 1085


def test_unverified_missing_or_escaped_geometry_has_no_maximum():
    reader = BarCaptionReader()
    frame = Frame(caption())
    proof = evidence(frame)
    for invalid in [replace(proof, fraction_interval=None),
                    replace(proof, layout_fingerprint=None),
                    replace(proof, caption_bounds=None),
                    replace(proof, caption_bounds=(-1, 0, 20, 20)),
                    replace(proof, field="resource_capacity"),
                    replace(proof, bounds=(10, 10, 20, 20))]:
        assert reader.read(frame, invalid) is None


@pytest.mark.parametrize("interval", [(0.0, 0.1), (0.9, 1.0), (float("nan"), 1.0), (0.8, 0.2)])
def test_caption_must_corroborate_its_own_bar_before_maximum_is_reliable(interval):
    frame = Frame(caption())
    reader = BarCaptionReader()
    assert reader.read(frame, replace(evidence(frame), fraction_interval=interval)) is None
    assert reader.last_diagnostics["known"] is False


@pytest.mark.parametrize("filename,bounds,expected", [
    ("904190487-char-tibia-lvl-302-em-gentebra-royal-palladin-BJEI.webp", (11, 27, 612, 40), (2992, 3125)),
    ("904190487-char-tibia-lvl-302-em-gentebra-royal-palladin-BJEI.webp", (618, 27, 1218, 40), (3628, 4500)),
    ("953559644-vendo-royal-paladin-383-tibia-global-gladera-SSVX.webp", (170, 4, 539, 17), (3935, 3935)),
    ("953559644-vendo-royal-paladin-383-tibia-global-gladera-SSVX.webp", (541, 4, 910, 17), (5715, 5715)),
    ("09s6sbltm1db1.png", (186, 4, 852, 19), (4799, 4799)),
    ("09s6sbltm1db1.png", (856, 4, 1521, 19), (3216, 7200)),
])
def test_development_original_caption_component_transfer(filename, bounds, expected):
    # Manually reviewed bar crops test the parser, not autonomous discovery.
    pixels = np.asarray(Image.open(ORIGINALS / filename).convert("RGB"))
    left, top, right, bottom = bounds
    reader = BarCaptionReader()
    result = reader.parse_caption(pixels[top:bottom, left:right])
    assert result is not None, reader.last_diagnostics
    assert (result.current, result.maximum) == expected


@pytest.mark.parametrize("bounds", [(15, 34, 738, 52), (744, 34, 1466, 52)])
def test_paulistinha_overlay_prevents_a_reliable_complete_caption(bounds):
    pixels = np.asarray(Image.open(ORIGINALS / "user-paulistinha-20261013.png").convert("RGB"))
    left, top, right, bottom = bounds
    assert BarCaptionReader().parse_caption(pixels[top:bottom, left:right]) is None


def test_optional_caption_never_enters_combinatorial_touching_digit_search(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("optional caption entered exhaustive digit partitions")
    monkeypatch.setattr("tools.resource_numeric_vector.VectorNumberReader._decode_touching_digits", forbidden)
    pixels = np.asarray(Image.open(ORIGINALS /
        "904190487-char-tibia-lvl-302-em-gentebra-royal-palladin-BJEI.webp").convert("RGB"))
    # This real caption previously entered the partition search before its
    # existing bounded scale corroboration accepted the same complete number.
    result = BarCaptionReader().parse_caption(pixels[25:40, 11:613])
    assert result is not None and (result.current, result.maximum) == (2992, 3125)
