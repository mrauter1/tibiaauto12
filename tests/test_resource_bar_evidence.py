"""Generated safety tests for approximate bar evidence, not corpus accuracy."""
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
import pytest

from core.perception import Frame
from tibia_client.adaptive import Anchor, PanelLayout, _ink
from tibia_client.models import Quality, Reading
from tools.resource_bar_evidence import (
    BarEvidence, FreshMaximum, RESOURCES, ResourceBarEvidence,
    cross_check, frame_token, validated_maximum,
)


def _scene(health=.75, mana=.4, *, scale=1, caption=True, sidebar=True,
           duplicate=False, health_color=(55, 185, 55)):
    """Simple native-style frames; approved semantic templates bind identity."""
    pixels = np.full((300, 900, 3), 70, np.uint8)
    boxes = ((20, 10, 320, 26), (326, 10, 626, 26))

    def draw_bar(box, fraction, color, rtl):
        left, top, right, bottom = box
        pixels[top:bottom, left:right] = 25
        pixels[top + 1:bottom, right - 1] = 120
        pixels[bottom - 1, left + 1:right] = 120
        pixels[top + 1:bottom - 1, left + 1:right - 1] = 40
        width = round((right - left - 2) * fraction)
        x = right - 1 - width if rtl else left + 1
        pixels[top + 1:bottom - 1, x:x + width] = color

    for box, fraction, color, rtl in zip(
            boxes, (health, mana), (health_color, (25, 70, 130)), (False, True)):
        draw_bar(box, fraction, color, rtl)
    if caption:
        image = Image.fromarray(pixels)
        draw = ImageDraw.Draw(image)
        for box, text in zip(boxes, ("750/1000", "400/1000")):
            draw.text(((box[0] + box[2]) / 2, 12), text, fill=(245, 245, 245), anchor="mt")
        pixels = np.asarray(image).copy()
    if duplicate:
        pixels[40:56, 20:626] = pixels[10:26, 20:626]
    if sidebar:
        pixels[29:240, 635] = 15
        pixels[29:240, 636] = 170
    anchors = []
    for field, name, y in zip(RESOURCES, ("health", "mana"), (100, 120)):
        with Image.open(Path("profiles/paulistinha-skills-v1/labels") / (name + ".png")) as source:
            label = np.asarray(source.convert("RGB"))
        h, w = label.shape[:2]
        pixels[y:y + h, 646:646 + w] = label
        anchors.append(Anchor(field, (646, y, 646 + w, y + h), .99, 1.))
    if scale != 1:
        pixels = np.asarray(Image.fromarray(pixels).resize(
            (round(900 * scale), round(300 * scale)), Image.Resampling.NEAREST))
        anchors = [replace(a, box=tuple(round(v * scale) for v in a.box), scale=scale)
                   for a in anchors]
    frame = Frame(pixels, captured_at=10.)
    guards = tuple(_ink(frame.pixels[a.box[1]:a.box[3], a.box[0]:a.box[2]]).tobytes()
                   for a in anchors)
    layout = PanelLayout((round(640 * scale), round(90 * scale),
                          round(850 * scale), round(160 * scale)),
                         tuple(anchors), {}, scale, "verified-test-panel", frame.size, guards)
    return frame, layout


@pytest.fixture(autouse=True)
def _single_cv_thread():
    cv2.setNumThreads(1)


@pytest.mark.parametrize("health,mana", [(.75, .4), (1., 1.), (0., .5), (.5, 0.), (.01, .99)])
def test_fraction_intervals_contain_fill_with_correct_opposite_directions(health, mana):
    frame, layout = _scene(health, mana)
    evidence = ResourceBarEvidence().measure(frame, layout)
    for field, fraction in zip(RESOURCES, (health, mana)):
        observed = evidence[field]
        assert observed.usable, observed.detail
        assert observed.fraction_interval[0] <= fraction <= observed.fraction_interval[1]
        assert observed.fraction_interval[1] - observed.fraction_interval[0] <= .04
        assert observed.frame_token == frame_token(frame)
    assert evidence[RESOURCES[0]].direction == "left_to_right"
    assert evidence[RESOURCES[1]].direction == "right_to_left"


@pytest.mark.parametrize("color", [(185, 55, 30), (195, 180, 25), (55, 185, 55)])
def test_health_threshold_colors_are_supported(color):
    frame, layout = _scene(health_color=color)
    assert ResourceBarEvidence().measure(frame, layout)[RESOURCES[0]].usable


def test_both_empty_generic_gray_bars_do_not_establish_resource_identity():
    frame, layout = _scene(0, 0)
    assert not any(e.usable for e in ResourceBarEvidence().measure(frame, layout).values())


@pytest.mark.parametrize("scale", [.8, 1.25, 1.5, 2.])
def test_capture_scale_preserves_fraction_with_wider_pixel_uncertainty(scale):
    frame, layout = _scene(scale=scale)
    evidence = ResourceBarEvidence().measure(frame, layout)
    for field, fraction in zip(RESOURCES, (.75, .4)):
        item = evidence[field]
        assert item.usable, item.detail
        assert item.fraction_interval[0] <= fraction <= item.fraction_interval[1]
        assert item.fraction_interval[1] - item.fraction_interval[0] <= .04


def test_tiny_capture_abstains_when_complete_bar_chrome_disappears():
    frame, layout = _scene(scale=.5)
    assert not any(e.usable for e in ResourceBarEvidence().measure(frame, layout).values())


@pytest.mark.parametrize("change", ["none", "size", "label", "blank", "no_sidebar", "duplicate", "no_caption", "border"])
def test_absent_or_unreliable_current_binding_abstains(change):
    frame, layout = _scene(sidebar=change != "no_sidebar", duplicate=change == "duplicate",
                           caption=change != "no_caption")
    if change == "none":
        layout = None
    elif change == "size":
        layout = replace(layout, frame_size=(1, 1))
    elif change == "blank":
        layout = replace(layout, guards=tuple(bytes(len(g)) for g in layout.guards))
    elif change in ("label", "border"):
        pixels = frame.pixels.copy()
        if change == "label":
            x0, y0, x1, y1 = layout.anchors[0].box
            pixels[y0:y1, x0:x1] = 40
        else:
            pixels[10, 20:320] = 70
        frame = Frame(pixels, captured_at=10.)
    assert not any(e.usable for e in ResourceBarEvidence().measure(frame, layout).values())


@pytest.mark.parametrize("obstruction", ["black", "foreign", "discontinuous", "offcenter_caption"])
def test_obscured_or_nonmonotonic_fill_does_not_supply_a_ratio(obstruction):
    frame, layout = _scene()
    pixels = frame.pixels.copy()
    if obstruction == "black":
        pixels[11:25, 220:270] = 0
    elif obstruction == "foreign":
        pixels[11:25, 220:270] = (240, 0, 240)
    elif obstruction == "discontinuous":
        pixels[11:25, 80:110] = 40
    else:
        pixels[12:19, 40:55] = 240
    changed = Frame(pixels, captured_at=10.)
    evidence = ResourceBarEvidence().measure(changed, layout)[RESOURCES[0]]
    assert not evidence.usable


def _trusted_check():
    frame, layout = _scene()
    evidence = ResourceBarEvidence().measure(frame, layout)[RESOURCES[0]]
    maximum = FreshMaximum(1000, frame.captured_at, layout.fingerprint,
                           frame_token(frame), True, RESOURCES[0], 750)
    return frame, evidence, maximum


def test_cross_check_vetoes_conflict_and_never_recovers_unknown():
    frame, evidence, maximum = _trusted_check()
    assert validated_maximum(frame, evidence, maximum)
    wrong = Reading({"present": True, "count": 150}, Quality.KNOWN)
    assert cross_check(frame, evidence, wrong, maximum).quality is Quality.UNKNOWN
    exact = Reading({"present": True, "count": 750}, Quality.KNOWN)
    assert cross_check(frame, evidence, exact, maximum) is exact
    unknown = Reading(None, Quality.UNKNOWN, "number obscured")
    assert cross_check(frame, evidence, unknown, maximum) is unknown


@pytest.mark.parametrize("change", ["absent", "not_independent", "stale", "layout", "pixels", "field", "zero", "no_current", "internal_conflict"])
def test_unavailable_or_internally_conflicting_maximum_never_vetoes(change):
    frame, evidence, maximum = _trusted_check()
    if change == "absent":
        maximum = None
    elif change == "not_independent":
        maximum = replace(maximum, independently_verified=False)
    elif change == "stale":
        maximum = replace(maximum, frame_timestamp=9.)
    elif change == "layout":
        maximum = replace(maximum, layout_fingerprint="other")
    elif change == "pixels":
        pixels = frame.pixels.copy()
        pixels[-1, -1] = 0
        frame = Frame(pixels, captured_at=frame.captured_at)
    elif change == "field":
        maximum = replace(maximum, field=RESOURCES[1])
    elif change == "zero":
        maximum = replace(maximum, value=0)
    elif change == "no_current":
        maximum = replace(maximum, current=None)
    else:
        maximum = replace(maximum, current=150)
    reading = Reading({"present": True, "count": 150}, Quality.KNOWN)
    assert not validated_maximum(frame, evidence, maximum)
    assert cross_check(frame, evidence, reading, maximum) is reading


def test_frame_token_memo_is_pixel_sensitive_and_not_timestamp_only():
    frame, _ = _scene()
    original = frame_token(frame)
    assert frame_token(frame) == original
    assert frame_token(Frame(frame.pixels, captured_at=100.)) == original
    pixels = frame.pixels.copy()
    pixels[0, 0] = 20
    assert frame_token(Frame(pixels, captured_at=frame.captured_at)) != original
