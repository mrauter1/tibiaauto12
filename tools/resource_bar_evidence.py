"""Current-frame, approximate evidence from native horizontal HP/Mana bars.

This optional experiment never manufactures an integer resource reading.  Its
only numeric action is a conservative veto when an independently decoded maximum
and a bar measurement both belong to the same immutable frame and panel layout.
No installation graphics, corpus annotations or saved game values are used.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import blake2b
import math
from collections.abc import Mapping

import cv2
import numpy as np

from core.perception import Frame, validate_region
from tibia_client.adaptive import PanelLayout, _crop, _ink
from tibia_client.models import Quality, Reading

RESOURCES = ("resource_health", "resource_mana")
Box = tuple[int, int, int, int]
_LAST_TOKEN: tuple[Frame, str] | None = None


def frame_token(frame: Frame) -> str:
    """Pixel identity; timestamps alone do not prove evidence belongs together."""
    global _LAST_TOKEN
    previous = _LAST_TOKEN
    if previous is not None and previous[0] is frame:
        return previous[1]
    digest = blake2b(digest_size=16)
    digest.update(f"{frame.width}x{frame.height}:".encode("ascii"))
    digest.update(frame.pixels)
    token = digest.hexdigest()
    _LAST_TOKEN = (frame, token)
    return token


@dataclass(frozen=True)
class BarEvidence:
    field: str
    fraction_interval: tuple[float, float] | None
    bounds: Box | None
    caption_bounds: Box | None
    direction: str | None
    frame_timestamp: float
    layout_fingerprint: str | None
    frame_token: str
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.fraction_interval is not None and self.bounds is not None


@dataclass(frozen=True)
class FreshMaximum:
    """Independent current/max caption evidence, never inferred from bar fill."""
    value: int
    frame_timestamp: float
    layout_fingerprint: str
    frame_token: str
    independently_verified: bool
    field: str
    current: int | None = None


def validated_maximum(frame: Frame, evidence: BarEvidence,
                      maximum: FreshMaximum | None) -> bool:
    """Require independently read caption and bar to agree before cross-checks."""
    if (not evidence.usable or evidence.field not in RESOURCES
            or not evidence.layout_fingerprint
            or maximum is None or maximum.independently_verified is not True
            or type(maximum.value) is not int or maximum.value <= 0
            or type(maximum.current) is not int
            or not 0 <= maximum.current <= maximum.value
            or evidence.field != maximum.field
            or evidence.layout_fingerprint != maximum.layout_fingerprint
            or evidence.frame_timestamp != frame.captured_at
            or maximum.frame_timestamp != frame.captured_at
            or evidence.frame_token != maximum.frame_token
            or evidence.frame_token != frame_token(frame)):
        return False
    low, high = evidence.fraction_interval
    if not (math.isfinite(low) and math.isfinite(high) and 0 <= low <= high <= 1):
        return False
    return low - 1 / maximum.value <= maximum.current / maximum.value <= high + 1 / maximum.value


def cross_check(frame: Frame, evidence: BarEvidence, reading: Reading,
                maximum: FreshMaximum | None) -> Reading:
    """Keep exact/UNKNOWN output unless reliable same-frame evidence contradicts.

    A missing maximum or unsupported/occluded bar is absence of evidence, not a
    reason to discard a good reading.  The independent caption's current count
    is deliberately never substituted for the original target field.
    """
    if reading.quality != Quality.KNOWN or not validated_maximum(frame, evidence, maximum):
        return reading
    value = reading.value
    count = value.get("count") if isinstance(value, Mapping) else value
    if type(count) is not int or count < 0:
        return reading
    low, high = evidence.fraction_interval
    # One integer unit permits the unavoidable discrete rounding at the bar.
    fraction = count / maximum.value
    if low - 1 / maximum.value <= fraction <= high + 1 / maximum.value:
        return reading
    diagnostics = dict(reading.diagnostics)
    diagnostics["bar_cross_check"] = {
        "fraction_interval": (low, high), "maximum": maximum.value,
        "field": evidence.field, "bounds": evidence.bounds,
        "frame_token": evidence.frame_token,
    }
    return Reading(None, Quality.UNKNOWN,
                   "current resource count contradicts verified bar fill and fresh maximum",
                   diagnostics)


class ResourceBarEvidence:
    """Recognize the paired horizontal status bars and measure fill intervals.

    The caller supplies the resource reader's uniquely verified current layout.
    Its label guards are checked again here.  Bars must have complete native
    recessed frames, a matching HP/Mana pair and a visible sidebar-divider path
    to that layout.  Parallel/side bars and Mana-shield variants are unsupported
    and intentionally return unavailable evidence.
    """

    def measure(self, frame: Frame, layout: PanelLayout | None) -> dict[str, BarEvidence]:
        token = frame_token(frame)
        fingerprint = layout.fingerprint if layout is not None else None

        def unavailable(detail):
            return {field: BarEvidence(field, None, None, None, None,
                                      frame.captured_at, fingerprint, token, detail)
                    for field in RESOURCES}

        if not self._current_identity(frame, layout):
            return unavailable("no verified current resource-panel identity")
        boxes = self._rectangles(frame, layout.scale)
        pairs = []
        for health in boxes:
            for mana in boxes:
                if self._pair_geometry(health, mana, layout.scale):
                    pairs.append((health, mana))
        # Overlapping alternative bounds are ambiguity too: do not select the
        # most convenient denominator when chrome is damaged or resampled.
        if len(pairs) != 1:
            return unavailable("horizontal status-bar pair absent or ambiguous")
        health, mana = pairs[0]
        if not self._sidebar_binding(frame, layout, mana):
            return unavailable("status-bar pair is not linked to the current resource sidebar")
        measured = [self._fill(frame, box, field, layout.scale)
                    for field, box in zip(RESOURCES, (health, mana))]
        # At least one color identity is necessary: two generic gray progress
        # bars do not establish that these are the player's resource bars.
        if not any(item[1] for item in measured):
            return unavailable("status-bar pair has no visible resource color identity")
        result = {}
        for field, box, (interval, _colored, detail) in zip(
                RESOURCES, (health, mana), measured):
            result[field] = BarEvidence(
                field, interval, box, box,
                "left_to_right" if field == RESOURCES[0] else "right_to_left",
                frame.captured_at, fingerprint, token, detail)
        return result

    @staticmethod
    def _current_identity(frame, layout):
        if (layout is None or layout.frame_size != frame.size
                or not layout.fingerprint or len(layout.anchors) < 2
                or len(layout.guards) != len(layout.anchors)
                or not math.isfinite(layout.scale) or not .3 <= layout.scale <= 3):
            return False
        fields = {anchor.field for anchor in layout.anchors}
        if not set(RESOURCES).issubset(fields):
            return False
        for anchor, guard in zip(layout.anchors, layout.guards):
            try:
                validate_region(anchor.box, frame.size)
            except (ValueError, RuntimeError):
                return False
            observed = _ink(_crop(frame, anchor.box)).ravel()
            saved = np.frombuffer(guard, dtype=np.uint8)
            if (saved.size != observed.size or not observed.size or not np.any(observed)
                    or anchor.score < .84
                    or float(np.mean(observed != saved)) > .12):
                return False
        return True

    @staticmethod
    def _pair_geometry(health, mana, scale):
        hl, ht, hr, hb = health
        ml, mt, mr, mb = mana
        width = hr - hl
        return (abs(ht - mt) <= max(1, round(scale))
                and abs(hb - mb) <= max(1, round(scale))
                and 2 * scale <= ml - hr <= 100 * scale
                and abs(width - (mr - ml)) <= max(3, .015 * width)
                and mr - hl >= 500 * scale)

    @staticmethod
    def _rectangles(frame, scale):
        # Status bars are at the top of the client; screenshots may include a
        # window title.  Bounding the scan also excludes world-space health bars.
        height = min(frame.height, max(50, round(100 * scale)))
        rgb = frame.pixels[:height]
        gray = (rgb[:, :, 0].astype(np.float32) + rgb[:, :, 1] + rgb[:, :, 2]) / 3
        light = (gray >= 80).astype(np.uint8)
        light = cv2.morphologyEx(light, cv2.MORPH_CLOSE,
                                 np.ones((1, max(3, round(3 * scale)) | 1), np.uint8))
        minimum_width = max(61, round(100 * scale) | 1)
        opened = cv2.morphologyEx(light, cv2.MORPH_OPEN,
                                   np.ones((1, minimum_width), np.uint8))
        changes = np.diff(np.pad(opened, ((0, 0), (1, 1))).astype(np.int8), axis=1)
        candidates = set()
        for bottom in range(max(5, round(7 * scale)), height - 1):
            starts, ends = np.flatnonzero(changes[bottom] == 1), np.flatnonzero(changes[bottom] == -1)
            for start, end in zip(starts, ends):
                left, right = int(start) - 1, int(end)
                if left < 1 or right >= frame.width - 1:
                    continue
                if float(np.mean(gray[bottom, left + 1:right] -
                                 gray[bottom + 1, left + 1:right] >= 12)) < .75:
                    continue
                for top in range(max(1, bottom - round(24 * scale)),
                                 bottom - max(4, round(7 * scale)) + 1):
                    span = gray[top, left + 1:right]
                    if float(np.mean(span <= 68)) < .9:
                        continue
                    if float(np.mean(gray[top - 1, left + 1:right] - span >= 12)) < .75:
                        continue
                    inside = gray[top + 1:bottom, left + 1:right - 1]
                    if inside.size == 0:
                        continue
                    # Recessed frame: left edge dark, right edge highlighted.
                    if (float(np.mean(gray[top + 1:bottom, left] <= 85)) < .8
                            or float(np.median(gray[top + 1:bottom, left])) > 65
                            or float(np.mean(gray[top + 1:bottom, right - 1] >= 82)) < .8):
                        continue
                    candidates.add((left, top, right, bottom + 1))
        # Downsampling can spread a one-pixel top edge over two rows.  Their
        # denominator is identical; retain the complete outer margin.  Distinct
        # horizontal bounds are never merged into a convenient value width.
        merged = []
        for box in sorted(candidates):
            equivalent = next((old for old in merged
                               if old[0] == box[0] and old[2:] == box[2:]
                               and abs(old[1] - box[1]) <= max(2, round(scale))), None)
            if equivalent is None:
                merged.append(box)
        return tuple(merged)

    @staticmethod
    def _sidebar_binding(frame, layout, mana):
        _, top, right, bottom = mana
        anchor_left = min(anchor.box[0] for anchor in layout.anchors)
        scale = layout.scale
        if not right < anchor_left or anchor_left - right > 900 * scale:
            return False
        end = min(frame.height, max(a.box[3] for a in layout.anchors))
        start = bottom + max(2, round(3 * scale))
        if end - start < 45 * scale:
            return False
        left = max(0, right)
        stop = min(frame.width - 1, anchor_left)
        gray = frame.pixels[start:end, left:stop + 1].mean(axis=2)
        delta = np.abs(np.diff(gray, axis=1))
        edges = left + np.flatnonzero((np.median(delta, axis=0) >= 20)
                                      & (np.mean(delta >= 15, axis=0) >= .65))
        near = [int(x) for x in edges if 2 * scale <= x - right <= 22 * scale]
        goal = [int(x) for x in edges if 3 * scale <= anchor_left - x <= 25 * scale]
        reachable = set(near)
        for edge in sorted(int(x) for x in edges):
            if any(200 * scale <= edge - old <= 240 * scale for old in reachable):
                reachable.add(edge)
        return bool(reachable.intersection(goal))

    @staticmethod
    def _fill(frame, box, field, scale):
        left, top, right, bottom = box
        rgb = frame.pixels[top + 1:bottom - 1, left + 1:right - 1].astype(np.int16)
        red, green, blue = rgb.transpose(2, 0, 1)
        low, high = rgb.min(axis=2), rgb.max(axis=2)
        white = (low >= 125) & (high - low <= 100)
        if field == RESOURCES[0]:
            colored = (((green - red >= 35) & (green - blue >= 30) & (green >= 85))
                       | ((red - blue >= 35) & (red >= 90) & (green >= 45))) & ~white
        else:
            colored = ((blue - red >= 30) & (blue - green >= 15)
                       & (blue >= 65)) & ~white
        background = (low >= 18) & (high <= 85) & (high - low <= 24)
        allowed = colored | background | white
        # Only the centered caption may hide bar pixels.  Unexplained colored
        # overlays or a black block cannot silently become an empty suffix.
        width = rgb.shape[1]
        if not width or rgb.shape[0] < 4:
            return None, False, "bar interior too small"
        caption = np.flatnonzero(np.any(white, axis=0))
        if (not len(caption) or caption[0] < .25 * width
                or caption[-1] > .75 * width
                or abs((caption[0] + caption[-1]) / 2 - width / 2) > 6 * scale):
            return None, bool(np.any(colored)), "missing or off-center complete resource caption"
        # Permit JPEG/scale halos near the frame and color transition only.
        unclassified = np.mean(~allowed, axis=0)
        usable = np.mean(colored | background, axis=0)
        filled = (np.mean(colored, axis=0) >= .45) & (usable >= .5)
        empty = (np.mean(background, axis=0) >= .55) & (usable >= .5)
        uncertain = ~(filled | empty)
        margin = max(2, math.ceil(3 * scale))
        if np.mean(unclassified[margin:-margin] > .4) > .025:
            return None, bool(np.any(colored)), "bar pixels obscured or outside supported palette"
        if field == RESOURCES[1]:
            filled, empty, uncertain = filled[::-1], empty[::-1], uncertain[::-1]
        yes = np.flatnonzero(filled)
        no = np.flatnonzero(empty)
        lower = int(yes[-1] + 1) if len(yes) else 0
        upper = int(no[0]) if len(no) else width
        if lower > upper + margin:
            return None, bool(len(yes)), "fill is not one continuous edge-anchored interval"
        if upper - lower > max(2 * margin, .04 * width):
            return None, bool(len(yes)), "fill boundary too obscured for a useful interval"
        interval = (max(0., (min(lower, upper) - margin) / width),
                    min(1., (max(lower, upper) + margin) / width))
        return interval, bool(len(yes)), "current horizontal status bar; approximate fill only"
