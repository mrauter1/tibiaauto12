"""Image-only semantic resource discovery and guarded panel-local reading.

Application callers use ``tibia_client.resource_reader.ResourceReader``. This
module retains the base implementation for composition and baseline comparisons.

Coordinates are captured pixels, never input coordinates. Discovery is bounded
but intentionally expensive; desktop adapters must schedule it off the action
loop. A resolved layout is evidence for one arrangement, not a live calibration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import math
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

import cv2
import numpy as np
from PIL import Image

from core.paths import project_path
from core.perception import Frame, validate_region
from tibia_client.models import Quality, Reading

FIELDS = ("resource_health", "resource_mana", "resource_soul", "resource_capacity")
_LABELS = ("health", "mana", "soul", "capacity")
MAX_FRAME_PIXELS = 16_777_216
MAX_ANCHORS = 12


def _ink(rgb: np.ndarray) -> np.ndarray:
    low = rgb.min(axis=2)
    high = rgb.max(axis=2).astype(np.int16)
    return ((low >= 130) & (high - low < 35)).astype(np.uint8) * 255


def _text_components(mask: np.ndarray) -> np.ndarray:
    """Return cheap word-like proposals; templates remain acceptance evidence."""
    joined = cv2.morphologyEx(
        (mask > 0).astype(np.uint8),
        cv2.MORPH_CLOSE,
        np.ones((1, 3), np.uint8),
    )
    _, _, stats, _ = cv2.connectedComponentsWithStats(joined, 8)
    return stats[1:]


def _component_matches(
    mask: np.ndarray,
    template: np.ndarray,
    components: np.ndarray,
    threshold: float,
) -> list[tuple[float, int, int]]:
    """Run the unchanged matcher inside broadly bounded text proposals."""
    height, width = template.shape
    windows = set()
    for x, y, component_width, component_height, _ in components:
        if not max(2, 0.4 * height) <= component_height <= 1.65 * height:
            continue
        if not max(1, 0.5 * width) <= component_width <= 1.6 * width:
            continue
        horizontal_margin = max(3, math.ceil(0.4 * width))
        left = max(0, int(x) - horizontal_margin)
        top = max(0, int(y) - height)
        right = min(mask.shape[1], int(x + component_width) + horizontal_margin)
        bottom = min(mask.shape[0], int(y + component_height) + height)
        if right - left >= width and bottom - top >= height:
            windows.add((left, top, right, bottom))

    found: dict[tuple[int, int], float] = {}
    for left, top, right, bottom in windows:
        scores = cv2.matchTemplate(
            mask[top:bottom, left:right], template, cv2.TM_CCOEFF_NORMED
        )
        for _ in range(MAX_ANCHORS):
            _, score, _, (x, y) = cv2.minMaxLoc(scores)
            if score < threshold:
                break
            location = (x + left, y + top)
            found[location] = max(float(score), found.get(location, -1.0))
            scores[
                max(0, y - height // 2) : y + height // 2 + 1,
                max(0, x - width // 2) : x + width // 2 + 1,
            ] = -1
    return [
        (score, x, y)
        for (x, y), score in sorted(
            found.items(), key=lambda item: item[1], reverse=True
        )[:MAX_ANCHORS]
    ]


def _full_matches(
    mask: np.ndarray, template: np.ndarray, threshold: float
) -> list[tuple[float, int, int]]:
    """Preserve the exhaustive matcher as a recall fallback."""
    height, width = template.shape
    scores = cv2.matchTemplate(mask, template, cv2.TM_CCOEFF_NORMED)
    found = []
    for _ in range(MAX_ANCHORS):
        _, score, _, (x, y) = cv2.minMaxLoc(scores)
        if score < threshold:
            break
        found.append((float(score), x, y))
        scores[
            max(0, y - height // 2) : y + height // 2 + 1,
            max(0, x - width // 2) : x + width // 2 + 1,
        ] = -1
    return found


def _proposal_matches(
    mask: np.ndarray,
    template: np.ndarray,
    proposals: list[tuple[float, int, int]],
    threshold: float,
) -> list[tuple[float, int, int]]:
    """Score a rendered variant only near a coarse binary proposal."""
    height, width = template.shape
    found: dict[tuple[int, int], float] = {}
    for _, x, y in proposals:
        margin = 3
        left = max(0, x - margin)
        top = max(0, y - margin)
        right = min(mask.shape[1], x + width + margin)
        bottom = min(mask.shape[0], y + height + margin)
        if right - left < width or bottom - top < height:
            continue
        _, score, _, (local_x, local_y) = cv2.minMaxLoc(
            cv2.matchTemplate(
                mask[top:bottom, left:right], template, cv2.TM_CCOEFF_NORMED
            )
        )
        if score >= threshold:
            location = (local_x + left, local_y + top)
            found[location] = max(float(score), found.get(location, -1.0))
    return [
        (score, x, y)
        for (x, y), score in sorted(
            found.items(), key=lambda item: item[1], reverse=True
        )[:MAX_ANCHORS]
    ]


def _tight(mask: np.ndarray) -> np.ndarray:
    yy, xx = np.where(mask)
    if not len(xx):
        raise ValueError("anchor contains no foreground")
    return np.ascontiguousarray(mask[yy.min() : yy.max() + 1, xx.min() : xx.max() + 1])


def _crop(frame: Frame, box: tuple[int, int, int, int]) -> np.ndarray:
    x, y, right, bottom = box
    return frame.pixels[y:bottom, x:right]


@dataclass(frozen=True)
class Anchor:
    field: str
    box: tuple[int, int, int, int]
    score: float
    scale: float
    method: str = "template"


@dataclass(frozen=True)
class PanelLayout:
    bounds: tuple[int, int, int, int]
    anchors: tuple[Anchor, ...]
    regions: Mapping[str, tuple[int, int, int, int]]
    scale: float
    fingerprint: str
    frame_size: tuple[int, int]
    guards: tuple[bytes, ...] = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "regions", MappingProxyType(dict(self.regions)))


@dataclass(frozen=True)
class DiscoveryResult:
    layout: PanelLayout | None
    detail: str = ""
    candidates: tuple[PanelLayout, ...] = ()


class ResourceReader:
    """Discover label constellations, infer columns, and read verified rows."""

    def __init__(self, *, number_reader=None, ocr=None):
        from tibia_client.adaptive_text import NumberReader, TesseractOCR

        self.ocr = ocr or TesseractOCR()
        self.number_reader = number_reader or NumberReader(ocr=self.ocr)
        self._number_reader_supports_family = bool(
            vars(type(self.number_reader)).get("supports_font_family", False)
        )
        root = Path(project_path("profiles/paulistinha-skills-v1/labels"))
        self._templates = {}
        self._raw_templates = {}
        self._native_templates = {}
        self._alternate_templates = {}
        self._alternate_raw_templates = {}
        for key, name in zip(FIELDS, _LABELS):
            with Image.open(root / f"{name}.png") as image:
                raw = np.asarray(image.convert("RGB"))
                self._raw_templates[key] = raw
                self._templates[key] = _tight(_ink(raw))
            with Image.open(root / "native" / f"{name}.png") as image:
                self._native_templates[key] = _tight(_ink(np.asarray(image.convert("RGB"))))
            alternate = root / "alternate" / "yonabra" / f"{name}.png"
            if alternate.is_file():
                with Image.open(alternate) as image:
                    raw = np.asarray(image.convert("RGB"))
                    self._alternate_raw_templates[key] = raw
                    self._alternate_templates[key] = _tight(_ink(raw))

    def _anchors(
        self, frame: Frame, box, checkpoint, *, fields=None, exhaustive: bool = False
    ) -> list[Anchor]:
        mask = _ink(_crop(frame, box))
        components = None if exhaustive else _text_components(mask)
        found = []
        # A bounded rendered-height bank covers UI scale independently of the
        # desktop resolution. Rounded dimensions are deduplicated per template.
        for key, template in self._templates.items():
            if fields is not None and key not in fields:
                continue
            checkpoint()
            candidates = []
            seen = set()
            for scale in (
                0.65,
                0.7,
                0.75,
                0.8,
                0.85,
                0.9,
                0.95,
                1.0,
                1.05,
                1.1,
                1.15,
                1.2,
                1.25,
                1.3,
                1.4,
                1.5,
                1.6,
                1.75,
                2.0,
                2.25,
                2.5,
            ):
                checkpoint()
                width = max(1, round(template.shape[1] * scale))
                height = max(1, round(template.shape[0] * scale))
                if (width, height) in seen or width > mask.shape[1] or height > mask.shape[0]:
                    continue
                seen.add((width, height))
                coarse = []
                for interpolation in (cv2.INTER_NEAREST, cv2.INTER_LINEAR):
                    resized = cv2.resize(template, (width, height), interpolation=interpolation)
                    resized = (resized >= 128).astype(np.uint8) * 255
                    matches = (
                        _full_matches(mask, resized, 0.65)
                        if exhaustive
                        else _component_matches(mask, resized, components, 0.65)
                    )
                    coarse.extend(matches)
                    for score, x, y in matches:
                        if score < 0.76:
                            continue
                        candidates.append(
                            Anchor(
                                key,
                                (x + box[0], y + box[1], x + width + box[0], y + height + box[1]),
                                float(score),
                                scale,
                            )
                        )

                # Downsampling can retain the primary font's antialiasing in a
                # phase that neither resized binary mask represents. Preserve
                # RGB while testing two canonical phases, but only beside a
                # plausible binary proposal. The final 0.76 acceptance score
                # and all constellation/value-column proofs remain unchanged.
                plausible = [item for item in coarse if 0.65 <= item[0] < 0.76]
                if plausible:
                    raw = self._raw_templates[key]
                    for offset_x in (0.0, 0.5):
                        matrix = np.float32(
                            [[scale, 0, offset_x], [0, scale, 0.0]]
                        )
                        rendered = cv2.warpAffine(
                            raw,
                            matrix,
                            (
                                round(raw.shape[1] * scale) + 2,
                                round(raw.shape[0] * scale) + 2,
                            ),
                            flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_REPLICATE,
                        )
                        rendered_mask = _tight(_ink(rendered))
                        rh, rw = rendered_mask.shape
                        for score, x, y in _proposal_matches(
                            mask, rendered_mask, plausible, 0.76
                        ):
                            candidates.append(
                                Anchor(
                                    key,
                                    (
                                        x + box[0],
                                        y + box[1],
                                        x + rw + box[0],
                                        y + rh + box[1],
                                    ),
                                    score,
                                    scale,
                                )
                            )
            selected = []
            for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
                if any(
                    abs(candidate.box[0] - old.box[0]) < 8 * candidate.scale
                    and abs(candidate.box[1] - old.box[1]) < 5 * candidate.scale
                    for old in selected
                ):
                    continue
                selected.append(candidate)
                if len(selected) >= MAX_ANCHORS:
                    break
            found.extend(selected)
        return found

    def _native_anchors(
        self, frame: Frame, box, checkpoint, *, fields=None, exhaustive: bool = False
    ) -> list[Anchor]:
        """Search the bundled alternate label-font bank without invoking OCR."""
        mask = _ink(_crop(frame, box))
        components = None if exhaustive else _text_components(mask)
        found = []
        for key, template in self._native_templates.items():
            if fields is not None and key not in fields:
                continue
            candidates = []
            for factor in (0.75, 1.0, 1.25, 1.5, 1.75, 2.0):
                checkpoint()
                w, h = round(template.shape[1] * factor), round(template.shape[0] * factor)
                if w > mask.shape[1] or h > mask.shape[0]:
                    continue
                resized = cv2.resize(template, (w, h), interpolation=cv2.INTER_NEAREST)
                matches = (
                    _full_matches(mask, resized, 0.84)
                    if exhaustive
                    else _component_matches(mask, resized, components, 0.84)
                )
                for score, x, y in matches:
                    candidates.append(
                        Anchor(
                            key,
                            (x + box[0], y + box[1], x + w + box[0], y + h + box[1]),
                            float(score),
                            factor * 0.8,
                            "native_template",
                        )
                    )
            selected = []
            for candidate in sorted(candidates, key=lambda a: a.score, reverse=True):
                if any(
                    abs(candidate.box[0] - old.box[0]) < 8 * candidate.scale
                    and abs(candidate.box[1] - old.box[1]) < 5 * candidate.scale
                    for old in selected
                ):
                    continue
                selected.append(candidate)
                if len(selected) >= MAX_ANCHORS:
                    break
            found.extend(selected)
        return found

    def _alternate_anchors(
        self, frame: Frame, box, checkpoint, *, fields=None, exhaustive: bool = False
    ) -> list[Anchor]:
        """Search documented rendered label variants from the training bank."""
        if not self._alternate_templates:
            return []
        mask = _ink(_crop(frame, box))
        components = None if exhaustive else _text_components(mask)
        found = []
        for key, template in self._alternate_templates.items():
            if fields is not None and key not in fields:
                continue
            candidates = []
            for factor in (0.75, 1.0, 1.25, 1.4, 1.5, 1.75, 2.0, 2.25, 2.5):
                checkpoint()
                w, h = round(template.shape[1] * factor), round(template.shape[0] * factor)
                if w > mask.shape[1] or h > mask.shape[0]:
                    continue
                resized = cv2.resize(template, (w, h), interpolation=cv2.INTER_NEAREST)
                coarse = (
                    _full_matches(mask, resized, 0.66)
                    if exhaustive
                    else _component_matches(mask, resized, components, 0.66)
                )
                coarse_score = coarse[0][0] if coarse else -1.0
                for score, x, y in coarse:
                    if score < 0.84:
                        continue
                    candidates.append(
                        Anchor(
                            key,
                            (x + box[0], y + box[1], x + w + box[0], y + h + box[1]),
                            float(score),
                            factor * 0.7,
                            "alternate_template",
                        )
                    )

                # Preserve antialiasing while scaling the documented rendered
                # family only where the cheap mask supplied a plausible coarse
                # proposal. A single canonical subpixel phase bounds full-frame
                # cost, and the strict final threshold is unchanged.
                if not 0.66 <= coarse_score < 0.84:
                    continue
                raw = self._alternate_raw_templates[key]
                matrix = np.float32([[factor, 0, 0.5], [0, factor, 0.5]])
                rendered = cv2.warpAffine(
                    raw,
                    matrix,
                    (
                        round(raw.shape[1] * factor) + 2,
                        round(raw.shape[0] * factor) + 2,
                    ),
                    flags=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REPLICATE,
                )
                rendered_mask = _tight(_ink(rendered))
                rh, rw = rendered_mask.shape
                if rh <= mask.shape[0] and rw <= mask.shape[1]:
                    matches = (
                        _full_matches(mask, rendered_mask, 0.84)
                        if exhaustive
                        else _component_matches(mask, rendered_mask, components, 0.84)
                    )
                    for score, x, y in matches:
                        candidates.append(
                            Anchor(
                                key,
                                (
                                    x + box[0],
                                    y + box[1],
                                    x + rw + box[0],
                                    y + rh + box[1],
                                ),
                                float(score),
                                factor * 0.7,
                                "alternate_template",
                            )
                        )
            selected = []
            for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
                if any(
                    abs(candidate.box[0] - old.box[0]) < 8 * candidate.scale
                    and abs(candidate.box[1] - old.box[1]) < 5 * candidate.scale
                    for old in selected
                ):
                    continue
                selected.append(candidate)
                if len(selected) >= MAX_ANCHORS:
                    break
            found.extend(selected)
        return found

    def _refine_anchors(
        self, frame: Frame, seeds: list[Anchor], checkpoint, selection=None
    ) -> list[Anchor]:
        """Resolve subpixel resampling phases locally around promising labels.

        Downsampling before binarization differs from resizing a binary glyph.
        Keep the original RGB label and test a bounded phase bank, restricted to
        candidate panels so this fallback does not multiply full-frame work.
        """
        found = list(seeds)
        selection = selection or (0, 0, frame.width, frame.height)
        locations = []
        for seed in sorted(seeds, key=lambda a: a.score, reverse=True):
            if seed.score < 0.80 or any(
                abs(seed.box[0] - old.box[0]) < 12 * seed.scale
                and abs(seed.box[1] - old.box[1]) < 90 * seed.scale
                for old in locations
            ):
                continue
            locations.append(seed)
            if len(locations) == 4:
                break
        for seed in locations:
            left = max(selection[0], seed.box[0] - round(10 * seed.scale))
            top = max(selection[1], seed.box[1] - round(80 * seed.scale))
            right = min(selection[2], seed.box[0] + round(150 * seed.scale))
            bottom = min(selection[3], seed.box[3] + round(80 * seed.scale))
            mask = _ink(frame.pixels[top:bottom, left:right])
            for key, raw in self._raw_templates.items():
                checkpoint()
                candidates = []
                for scale in (seed.scale - 0.025, seed.scale, seed.scale + 0.025):
                    # Published captures can have slightly different horizontal
                    # and vertical resampling. Keep this small aspect bank local
                    # to a semantic panel, with the same score/geometry guards.
                    for vertical_scale in (
                        scale,
                        scale - 0.05,
                        scale + 0.05,
                        scale - 0.10,
                        scale + 0.10,
                    ):
                        for dx in (0.0, 0.25, 0.5, 0.75):
                            for dy in (0.0, 0.25, 0.5, 0.75):
                                matrix = np.float32([[scale, 0, dx], [0, vertical_scale, dy]])
                                resized = cv2.warpAffine(
                                    raw,
                                    matrix,
                                    (
                                        round(raw.shape[1] * scale) + 2,
                                        round(raw.shape[0] * vertical_scale) + 2,
                                    ),
                                    flags=cv2.INTER_LINEAR,
                                    borderMode=cv2.BORDER_REPLICATE,
                                )
                                template = _tight(_ink(resized))
                                h, w = template.shape
                                if h > mask.shape[0] or w > mask.shape[1]:
                                    continue
                                scores = cv2.matchTemplate(mask, template, cv2.TM_CCOEFF_NORMED)
                                _, score, _, (x, y) = cv2.minMaxLoc(scores)
                                if score >= 0.80:
                                    candidates.append(
                                        Anchor(
                                            key,
                                            (x + left, y + top, x + w + left, y + h + top),
                                            float(score),
                                            scale,
                                        )
                                    )
                if candidates:
                    found.append(max(candidates, key=lambda a: a.score))
        return found

    def _ocr_anchors(self, frame: Frame, box) -> list[Anchor]:
        from tibia_client.adaptive_text import TesseractOCR

        ocr = self.ocr or TesseractOCR()
        if not ocr.available:
            return []
        aliases = {
            "hit points": FIELDS[0],
            "health": FIELDS[0],
            "mana": FIELDS[1],
            "soul points": FIELDS[2],
            "soul": FIELDS[2],
            "capacity": FIELDS[3],
        }
        words = ocr.read_text_boxes(_crop(frame, box), psm=11)
        found = []
        # Detection provides word boxes. Combine adjacent words on one baseline
        # before alias lookup, never accept substring labels from chat sentences.
        for word in words:
            text = word.text.strip().lower().rstrip(":")
            b = word.box
            options = [(text, b, word.score)]
            for other in words:
                if (
                    0 <= other.box[0] - b[2] <= (b[3] - b[1]) * 1.5
                    and abs(other.box[1] - b[1]) <= 3
                ):
                    options.append(
                        (
                            text + " " + other.text.strip().lower().rstrip(":"),
                            (b[0], min(b[1], other.box[1]), other.box[2], max(b[3], other.box[3])),
                            min(word.score, other.score),
                        )
                    )
            for text, bounds, score in sorted(options, key=lambda item: len(item[0]), reverse=True):
                if text in aliases and score >= 0.80:
                    key = aliases[text]
                    x, y, right, bottom = bounds
                    found.append(
                        Anchor(
                            key,
                            (x + box[0], y + box[1], right + box[0], bottom + box[1]),
                            score,
                            (bottom - y) / self._templates[key].shape[0],
                            "ocr",
                        )
                    )
                    break
        return found

    def _constellations(self, anchors: list[Anchor]) -> list[tuple[Anchor, ...]]:
        groups = []
        for origin in anchors:
            nearby = [
                item
                for item in anchors
                if abs(item.box[0] - origin.box[0]) <= 5 * max(item.scale, origin.scale)
                and abs(item.box[1] - origin.box[1]) <= 100 * origin.scale
                and 0.65 <= item.scale / origin.scale <= 1.5
            ]
            chosen = {}
            # OCR may complete a missing semantic row, but must not displace a
            # matching bundled-font anchor merely because its engine score uses
            # a different scale.  Scores from the two methods are not comparable.
            for item in sorted(
                nearby, key=lambda item: (item.method == "ocr", -item.score)
            ):
                chosen.setdefault(item.field, item)
            ordered = tuple(chosen[key] for key in FIELDS if key in chosen)
            if len(ordered) < 3:
                continue
            centers = [(item.box[1] + item.box[3]) / 2 for item in ordered]
            steps = [
                (centers[i + 1] - centers[i])
                / (FIELDS.index(ordered[i + 1].field) - FIELDS.index(ordered[i].field))
                for i in range(len(ordered) - 1)
            ]
            scale = float(np.median([item.scale for item in ordered]))
            if not steps or min(steps) < 9 * scale or max(steps) > 28 * scale:
                continue
            if max(steps) - min(steps) > max(3, 4 * scale):
                continue
            signature = tuple((item.field, item.box[:2]) for item in ordered)
            if not any(
                signature == tuple((item.field, item.box[:2]) for item in old) for old in groups
            ):
                groups.append(ordered)
        return groups

    def _layout(self, frame: Frame, anchors: tuple[Anchor, ...]) -> PanelLayout | None:
        scale = float(np.median([a.scale for a in anchors]))
        # Find text runs and a common right edge. Independent rows prevent an
        # isolated unrelated number on the same horizontal line becoming a field.
        runs = {}
        for anchor in anchors:
            # Every row starts after its own label. A longer neighboring label
            # must never cut the leading digits off a compact value column.
            search_left = anchor.box[2] + max(2, round(3 * scale))
            search_right = min(frame.width, search_left + round(280 * scale))
            top = max(0, anchor.box[1] - max(3, round(3 * scale)))
            bottom = min(frame.height, anchor.box[3] + max(3, round(3 * scale)))
            if search_right <= search_left or bottom <= top:
                return None
            ink = _ink(frame.pixels[top:bottom, search_left:search_right])
            occupied = np.any(ink, axis=0).astype(np.uint8)
            # Join only normal inter-glyph spaces; keep independently spaced
            # columns separate so duplicate panels/foreign text are ambiguous.
            gap = max(2, round(4 * scale))
            closed = cv2.morphologyEx(
                occupied[None, :], cv2.MORPH_CLOSE, np.ones((1, gap + 1), np.uint8)
            )[0].astype(bool)
            starts = np.flatnonzero(closed & np.r_[True, ~closed[:-1]])
            ends = np.flatnonzero(closed & np.r_[~closed[1:], True]) + 1
            row = []
            for start, end in zip(starts, ends):
                ys, xs = np.where(ink[:, start:end])
                if len(xs) == 0:
                    continue
                actual_left, actual_right = start + int(xs.min()), start + int(xs.max()) + 1
                height = int(ys.max() - ys.min() + 1)
                width = actual_right - actual_left
                if height < 5 * scale or height > 16 * scale or width > 130 * scale:
                    continue
                if int(ys.min()) == 0 or int(ys.max()) == ink.shape[0] - 1:
                    continue
                row.append(
                    (
                        search_left + actual_left,
                        search_left + actual_right,
                        top + int(ys.min()),
                        top + int(ys.max()) + 1,
                    )
                )
            runs[anchor.field] = row
        columns = []
        for row in runs.values():
            for candidate in row:
                matches = {}
                for key, alternatives in runs.items():
                    aligned = [
                        r for r in alternatives if abs(r[1] - candidate[1]) <= max(2, 3 * scale)
                    ]
                    if len(aligned) == 1:
                        matches[key] = aligned[0]
                if len(matches) >= 3:
                    right = round(float(np.median([r[1] for r in matches.values()])))
                    if not any(abs(right - old[0]) <= max(2, 3 * scale) for old in columns):
                        columns.append((right, matches))

        panel_top = min(a.box[1] for a in anchors)
        panel_bottom = max(a.box[3] for a in anchors)

        def panel_boundary(column_right: int) -> int | None:
            """Find continuous chrome just beyond a proposed value column.

            Numeric glyphs recur only inside their individual rows.  A panel
            divider instead has a strong horizontal luminance transition across
            nearly the full label constellation.  Keeping this test relative to
            each proposed column lets chrome remove only candidates on its far
            side; two real value columns on the same side remain ambiguous.
            """
            limit = min(frame.width - 1, column_right + round(32 * scale))
            for x in range(column_right + max(3, round(3 * scale)), limit):
                stripe = frame.pixels[panel_top:panel_bottom, x : x + 2].mean(axis=2)
                difference = np.abs(stripe[:, 1] - stripe[:, 0])
                if float(np.median(difference)) >= 20 and float(np.mean(difference >= 15)) >= 0.8:
                    return x
            return None

        boundary = None
        if len(columns) > 1:
            # A verified divider may itself pass the three-row run filter and
            # look like a second aligned value column.  Resolve that specific
            # shape before enforcing uniqueness.  A divider cannot choose
            # between multiple columns that all lie on its near side.
            bounded = []
            for right, matches in columns:
                candidate_boundary = panel_boundary(right)
                if candidate_boundary is None:
                    continue
                other_columns = [
                    (other_right, other_matches)
                    for other_right, other_matches in columns
                    if other_right != right
                ]
                # Discard only columns made from the divider stroke itself.
                # A complete value column beyond real chrome remains evidence
                # of an ambiguous/duplicated panel rather than decoration.
                if other_columns and all(
                    other_right >= candidate_boundary
                    and all(
                        run[1] - run[0] <= max(2, math.ceil(scale))
                        and abs(run[0] - candidate_boundary) <= max(2, math.ceil(scale))
                        for run in other_matches.values()
                    )
                    for other_right, other_matches in other_columns
                ):
                    bounded.append((right, matches, candidate_boundary))
            if len(bounded) == 1:
                right, matches, boundary = bounded[0]
                columns = [(right, matches)]
        if len(columns) != 1:
            return None
        right, matches = columns[0]
        # A continuous panel divider separates the Skills values from nearby
        # minimap/inventory pixels. Only discard runs beyond independently
        # observed chrome; without that evidence extra runs stay ambiguous.
        if boundary is None:
            boundary = panel_boundary(right)
        # Commas extend below the label baseline. Preserve a real dark border
        # even when downsampling rounds the nominal margin to two pixels.
        pad = max(3, round(3 * scale))
        regions = {}
        for anchor in anchors:
            # A majority identifies the column, but cannot authorize cropping
            # a dissenting row to that column: that could turn 1111111 into 1111.
            # Extra runs on either side are ambiguous prefixes/suffixes, never
            # disposable decoration. Keep unaffected rows independently useful.
            local_runs = [
                run for run in runs[anchor.field] if boundary is None or run[0] < boundary
            ]
            if anchor.field not in matches or len(local_runs) != 1:
                continue
            run = local_runs[0]
            left = anchor.box[2] + pad
            # Use the observed numeric run for vertical geometry. Labels and
            # values can have different ascenders and resample to different
            # heights, so label-relative padding can clip valid high-scale
            # digits. A small antialias margin around the run includes the full
            # glyph without reaching the next resource row.
            vertical_margin = max(3, math.ceil(3.5 * scale))
            top = run[2] - vertical_margin
            bottom = run[3] + vertical_margin
            region = (left, top, right + pad, bottom)
            try:
                validate_region(region, frame.size)
            except (ValueError, RuntimeError):
                return None
            regions[anchor.field] = region
        bounds = (
            max(0, min(a.box[0] for a in anchors) - pad),
            max(0, min(a.box[1] for a in anchors) - pad),
            min(frame.width, right + pad),
            min(frame.height, max(a.box[3] for a in anchors) + pad),
        )
        guards = tuple(_ink(_crop(frame, a.box)).tobytes() for a in anchors)
        # Visibility of a numeric value is not a layout revision. Bounds and
        # anchors identify geometry, while current per-row eligibility is
        # checked afresh before every read.
        identity = repr((frame.size, bounds, tuple((a.field, a.box) for a in anchors))).encode()
        return PanelLayout(
            bounds, anchors, regions, scale, sha256(identity).hexdigest(), frame.size, guards
        )

    def discover(
        self,
        frame: Frame,
        *,
        selection=None,
        use_ocr: bool = False,
        checkpoint: Callable[[], None] | None = None,
    ) -> DiscoveryResult:
        if not isinstance(frame, Frame):
            raise TypeError("discovery requires an immutable Frame")
        if frame.width * frame.height > MAX_FRAME_PIXELS:
            raise ValueError("frame exceeds adaptive discovery pixel budget")
        checkpoint = checkpoint or (lambda: None)
        box = (
            (0, 0, frame.width, frame.height)
            if selection is None
            else validate_region(selection, frame.size)
        )

        def search(search_box, *, fields=None, exhaustive=False):
            found = self._anchors(
                frame, search_box, checkpoint, fields=fields, exhaustive=exhaustive
            )
            found.extend(
                self._native_anchors(
                    frame, search_box, checkpoint, fields=fields, exhaustive=exhaustive
                )
            )
            found.extend(
                self._alternate_anchors(
                    frame, search_box, checkpoint, fields=fields, exhaustive=exhaustive
                )
            )
            return found

        # Two separated semantic rows seed every valid three-of-four
        # constellation. Search all families globally for those rows, then run
        # the complete banks only in bounded panel neighborhoods. Both seed rows
        # are always searched so an equivalent second panel cannot be hidden by
        # stopping after the first valid layout.
        seed_fields = frozenset((FIELDS[0], FIELDS[3]))
        # These two global rows are exhaustive: proposal completeness must never
        # decide whether a second valid panel exists outside the first panel's
        # local window. A valid three-of-four panel contains at least one of the
        # two separated rows, so the remaining rows can be searched locally.
        seeds = search(box, fields=seed_fields, exhaustive=True)
        remaining_fields = frozenset(FIELDS) - seed_fields

        def exhaustive_anchors():
            # The seed rows were already searched exhaustively. Reuse them so
            # the conservative fallback costs the same four full-frame field
            # searches as the original implementation rather than six.
            return [
                *seeds,
                *search(box, fields=remaining_fields, exhaustive=True),
            ]

        def panel_search_box(seed):
            return (
                max(box[0], seed.box[0] - round(25 * seed.scale)),
                max(box[1], seed.box[1] - round(120 * seed.scale)),
                min(box[2], seed.box[2] + round(260 * seed.scale)),
                min(box[3], seed.box[3] + round(120 * seed.scale)),
            )

        locations = []
        covered = []
        for seed in sorted(seeds, key=lambda item: item.score, reverse=True):
            local = panel_search_box(seed)
            # Deduplicate only when this seed would search no pixels outside a
            # neighborhood already scheduled. Coordinate/scale proximity is
            # insufficient: a large nearby panel can otherwise be swallowed by
            # a small panel whose local window does not reach its constellation.
            if any(
                old[0] <= local[0]
                and old[1] <= local[1]
                and old[2] >= local[2]
                and old[3] >= local[3]
                for old in covered
            ):
                continue
            locations.append(seed)
            covered.append(local)

        anchors = list(seeds)
        for seed in locations:
            local = panel_search_box(seed)
            if local[2] > local[0] and local[3] > local[1]:
                # Once a strong semantic seed bounds a small panel neighborhood,
                # exhaustive matching is cheap and preserves weak-row recall.
                anchors.extend(search(local, exhaustive=True))

        groups = self._constellations(anchors)
        if groups and any(len(group) < len(FIELDS) for group in groups):
            anchors = self._refine_anchors(frame, anchors, checkpoint, box)
            groups = self._constellations(anchors)
        used_full_frame = False
        if not groups or any(len(group) < len(FIELDS) for group in groups):
            # Proposal recall is deliberately not trusted for partial panels.
            # Preserve the prior exhaustive search before OCR or acceptance.
            anchors = exhaustive_anchors()
            used_full_frame = True
            groups = self._constellations(anchors)
        if not groups or any(len(group) < len(FIELDS) for group in groups):
            anchors = self._refine_anchors(frame, anchors, checkpoint, box)
            groups = self._constellations(anchors)
        if use_ocr and (not groups or any(len(group) < len(FIELDS) for group in groups)):
            checkpoint()
            anchors.extend(self._ocr_anchors(frame, box))
            groups = self._constellations(anchors)
        layouts = []
        for group in groups:
            checkpoint()
            layout = self._layout(frame, group)
            if layout is not None:
                layouts.append(layout)
        if not layouts and not used_full_frame:
            # A geometrically complete local constellation can still fail the
            # independent value-column proof. In that case, do not let the fast
            # path mask a different valid full-frame constellation.
            anchors = exhaustive_anchors()
            groups = self._constellations(anchors)
            if not groups or any(len(group) < len(FIELDS) for group in groups):
                anchors = self._refine_anchors(frame, anchors, checkpoint, box)
                groups = self._constellations(anchors)
            if use_ocr and (not groups or any(len(group) < len(FIELDS) for group in groups)):
                checkpoint()
                anchors.extend(self._ocr_anchors(frame, box))
                groups = self._constellations(anchors)
            for group in groups:
                checkpoint()
                layout = self._layout(frame, group)
                if layout is not None:
                    layouts.append(layout)
        if len(layouts) != 1:
            return DiscoveryResult(
                None,
                "resource panel is ambiguous"
                if layouts
                else "no verified resource panel and value column found",
                tuple(layouts),
            )
        return DiscoveryResult(layouts[0], candidates=tuple(layouts))

    def _current_layout(self, frame: Frame, layout: PanelLayout) -> PanelLayout | None:
        if frame.size != layout.frame_size:
            return None
        if len(layout.anchors) != len(layout.guards) or len(layout.anchors) < 3:
            return None
        for anchor, saved in zip(layout.anchors, layout.guards):
            observed = _ink(_crop(frame, anchor.box))
            expected = np.frombuffer(saved, dtype=np.uint8).reshape(observed.shape)
            union = np.count_nonzero((observed > 0) | (expected > 0))
            mismatch = np.count_nonzero(observed != expected)
            if not union or mismatch / union > 0.12:
                return None
        current = self._layout(frame, layout.anchors)
        return (
            current if current is not None and current.fingerprint == layout.fingerprint else None
        )

    def validate(self, frame: Frame, layout: PanelLayout) -> bool:
        """Recheck semantic guards and column geometry without full discovery."""
        return self._current_layout(frame, layout) is not None

    def read(
        self, frame: Frame, layout: PanelLayout, *, use_ocr: bool = False
    ) -> dict[str, Reading]:
        def unknown(message):
            return {key: Reading(None, Quality.UNKNOWN, message) for key in FIELDS}

        current = self._current_layout(frame, layout)
        if current is None:
            return unknown("resource layout moved, changed, or is obscured; rediscovery required")
        result = unknown(
            "resource row does not have a unique complete value in the verified column"
        )
        alternate_count = sum(
            anchor.method == "alternate_template" for anchor in current.anchors
        )
        number_family = (
            "alternate"
            if alternate_count >= 2 and alternate_count * 2 >= len(current.anchors)
            else None
        )
        for key, region in current.regions.items():
            number_options = {
                "scale": layout.scale,
                "max_digits": 9,
                "allow_grouping": key != "resource_soul",
                "use_ocr": use_ocr,
            }
            if number_family is not None and self._number_reader_supports_family:
                number_options["family"] = number_family
            reading = self.number_reader.read(_crop(frame, region), **number_options)
            diagnostics = {
                **dict(reading.diagnostics),
                "numeric_eligible": True,
                "effective_region": region,
                "layout_fingerprint": layout.fingerprint,
                "scale": layout.scale,
                "anchor_method": next(a.method for a in layout.anchors if a.field == key),
                "font_family": number_family or "primary",
            }
            if reading.quality is Quality.KNOWN:
                result[key] = Reading(
                    {
                        "present": True,
                        "position": None,
                        "count": reading.value,
                        "score": reading.diagnostics.get("score", 0.0),
                    },
                    diagnostics=diagnostics,
                )
            else:
                result[key] = Reading(None, reading.quality, reading.detail, diagnostics)
        return result
