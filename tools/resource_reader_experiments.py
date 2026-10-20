"""Opt-in reader experiments; production defaults and frozen fixtures stay intact.

Readers receive only pixels. No case IDs, annotations or expected values are
available here. The companion runner owns scoring and experiment selection.
"""
from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from hashlib import sha256
from itertools import product
import math

import cv2
import numpy as np

from core.perception import Frame, validate_region
from tibia_client.adaptive import (
    Anchor, FIELDS, MAX_ANCHORS, PanelLayout, ResourceReader, _component_matches,
    _crop, _full_matches, _ink, _proposal_matches, _text_components, _tight,
)
from tibia_client.adaptive_text import (
    NumberReader, _Decoded, _Rank, _alternatives, _glyph, _glyph_score,
    _ink as numeric_ink, _valid_text,
)
from tibia_client.models import Quality, Reading


def neutralize_green(rgb: np.ndarray) -> np.ndarray:
    """Preserve brightness while including the observed green text color family."""
    values = rgb.astype(np.int16)
    red, green, blue = (values[:, :, i] for i in range(3))
    selected = (green >= 90) & (green - red >= 30) & (green - blue >= 30)
    if not np.any(selected):
        return rgb
    result = rgb.copy()
    result[selected] = np.repeat(green[selected, None], 3, axis=1)
    return result


class ExperimentalNumberReader(NumberReader):
    supports_font_family = True

    def __init__(self, features=(), **kwargs):
        super().__init__(**kwargs)
        self.features = frozenset(features)
        if 'cache' in self.features:
            self._scaled_digit_references = lru_cache(maxsize=24)(self._scaled_digit_references)
            self._comma_references = lru_cache(maxsize=24)(self._comma_references)

    def _decode_touching_digits(self, image, scale, max_digits, family=None):
        if 'prune' in self.features:
            columns = np.flatnonzero(np.any(numeric_ink(image), axis=0))
            if not len(columns):
                return None
            width = int(columns[-1] - columns[0] + 1)
            # Every accepted cell already must have this width in the baseline.
            max_digits = min(max_digits, width // math.ceil(5.0 * scale))
        return super()._decode_touching_digits(image, scale, max_digits, family)

    def read(self, image, *, scale=1.0, max_digits=9, allow_grouping=True,
             use_ocr=False, family=None):
        if 'green' in self.features:
            image = neutralize_green(image)
        if 'small' in self.features and scale < 0.5:
            image = cv2.resize(image, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
            scale *= 2
        options = dict(scale=scale, max_digits=max_digits, allow_grouping=allow_grouping,
                       use_ocr=use_ocr, family=family)
        baseline = super().read(image, **options)
        if baseline.quality is Quality.KNOWN or 'consensus' not in self.features or use_ocr:
            return baseline
        if baseline.detail not in {
            'number contains a weakly matched digit',
            'number contains a weakly matched thousands separator',
            'number contains an ambiguous digit',
            'number shape is unstable near the foreground threshold',
        }:
            return baseline
        supported = []
        for delta in (-10, -5, 5, 10):
            variant = np.clip(image.astype(np.int16) + delta, 0, 255).astype(np.uint8)
            reading = super().read(variant, **options)
            if reading.quality is Quality.KNOWN:
                supported.append((delta, reading))
        values = {reading.value for _, reading in supported}
        if len(supported) >= 2 and len(values) == 1:
            reading = min((r for _, r in supported), key=lambda r:r.diagnostics['score'])
            return Reading(reading.value, Quality.KNOWN, diagnostics={
                **dict(reading.diagnostics), 'experiment':'threshold_consensus',
                'threshold_deltas':[d for d,_ in supported],
                'baseline_rejection':baseline.detail,
            })
        return baseline


    def _decode(
        self,
        image: np.ndarray,
        scale: float,
        max_digits: int,
        allow_grouping: bool,
        family: str | None = None,
    ) -> _Decoded:
        ink = numeric_ink(image)
        if not bool(np.any(ink)):
            raise ValueError("number contains no supported digit foreground")
        if (
            bool(np.any(ink[0]))
            or bool(np.any(ink[-1]))
            or bool(np.any(ink[:, 0]))
            or bool(np.any(ink[:, -1]))
        ):
            raise ValueError("number foreground touches the ROI edge and may be clipped")

        occupied = np.any(ink, axis=0)
        starts = np.flatnonzero(occupied & np.r_[True, ~occupied[:-1]])
        ends = np.flatnonzero(occupied & np.r_[~occupied[1:], True]) + 1
        if len(starts) == 0 or len(starts) > max_digits * 2 + 1:
            raise ValueError("number has an unsupported number of glyph segments")
        gaps = starts[1:] - ends[:-1]
        maximum_gap = max(3, math.ceil(5.0 * scale))
        if len(gaps) and int(np.max(gaps)) > maximum_gap:
            raise ValueError("number contains abnormally separated glyph runs")
        glyphs = tuple(_glyph(ink[:, start:end]) for start, end in zip(starts, ends))
        tall_height = max(item.height for item in glyphs)
        expected_height = 9.5 * scale
        if tall_height < max(4.0, expected_height * 0.62) or tall_height > expected_height * 1.55:
            raise ValueError("number glyph height is inconsistent with the estimated scale")

        scaled_digits = self._scaled_digit_references(scale, family)
        ranks: list[tuple[_Rank, ...]] = []
        for candidate in glyphs:
            is_short = candidate.height <= tall_height * 0.68
            if is_short:
                commas = self._comma_references(scale, family)
                comma_score = max(_glyph_score(candidate, comma) for comma in commas)
                ranks.append((_Rank(",", comma_score),))
                continue
            ranked: list[_Rank] = []
            for number, variants in enumerate(scaled_digits):
                compatible = [item for item in variants if 'topology' in self.features or item.holes == candidate.holes]
                if compatible:
                    ranked.append(
                        _Rank(
                            str(number),
                            max(_glyph_score(candidate, item) - (0.08 * abs(candidate.holes-item.holes) if 'topology' in self.features else 0.0) for item in compatible),
                        )
                    )
            if not ranked:
                raise ValueError("number contains a glyph with unsupported topology")
            ranked.sort(key=lambda item: item.score, reverse=True)
            ranks.append(tuple(ranked[:3]))

        text = "".join(items[0].token for items in ranks)
        token_scores = [items[0].score for items in ranks]
        digit_scores = [items[0].score for items in ranks if items[0].token != ","]
        if not digit_scores:
            raise ValueError("number contains no digit glyphs")
        alternatives = _alternatives(tuple(ranks), max_digits, allow_grouping)
        return _Decoded(
            text,
            min(token_scores),
            sum(digit_scores) / len(digit_scores),
            tuple(ranks),
            alternatives,
        )


class ExperimentalResourceReader(ResourceReader):
    def __init__(self, features=(), **kwargs):
        self.features = frozenset(features)
        numbers = kwargs.pop('number_reader', None)
        if numbers is None:
            if 'vector' in self.features:
                from tools.resource_numeric_vector import VectorNumberReader
                numbers = VectorNumberReader(features)
            else:
                numbers = ExperimentalNumberReader(features)
        super().__init__(number_reader=numbers, **kwargs)

    @property
    def label_scales(self):
        regular = (0.65, .7, .75, .8, .85, .9, .95, 1., 1.05, 1.1, 1.15,
                   1.2, 1.25, 1.3, 1.4, 1.5, 1.6, 1.75, 2., 2.25, 2.5)
        extra = tuple(np.arange(.65, .851, .025)) if 'labels' in self.features else ()
        small = tuple(np.arange(.4, .651, .025)) if 'small' in self.features else ()
        return tuple(sorted({round(float(s), 3) for s in (*regular, *extra, *small)}))

    def _value_ink(self, pixels):
        return _ink(neutralize_green(pixels) if 'green' in self.features else pixels)

    def _partial_anchors(self, frame, anchors):
        if 'partial' not in self.features or len(anchors) != 3:
            return anchors
        missing = [f for f in FIELDS if f not in {a.field for a in anchors}]
        scale = float(np.median([a.scale for a in anchors]))
        indices = np.array([FIELDS.index(a.field) for a in anchors])
        centers = np.array([(a.box[1]+a.box[3])/2 for a in anchors])
        slope, offset = np.polyfit(indices, centers, 1)
        left = round(float(np.median([a.box[0] for a in anchors])))
        key = missing[0]
        raw = self._raw_templates[key]
        found = []
        for dx in (0., .25, .5, .75):
            for dy in (0., .25, .5, .75):
                matrix = np.float32([[scale,0,dx],[0,scale,dy]])
                rendered = cv2.warpAffine(raw,matrix,
                    (round(raw.shape[1]*scale)+2,round(raw.shape[0]*scale)+2),
                    flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_REPLICATE)
                template = _tight(_ink(rendered))
                h,w = template.shape
                top = round(offset+slope*FIELDS.index(key)-h/2)
                # A short visible suffix alone never locates a panel: three
                # verified complete row bindings must already establish it.
                for cut in (round(w*.25),round(w*.35),round(w*.45)):
                    if w-cut < 14*scale:
                        continue
                    suffix = template[:,cut:]
                    region=(max(0,left+cut-2),max(0,top-2),
                            min(frame.width,left+w+3),min(frame.height,top+h+3))
                    mask=_ink(_crop(frame,region))
                    if mask.shape[0]<h or mask.shape[1]<w-cut:
                        continue
                    _,score,_,(x,y)=cv2.minMaxLoc(cv2.matchTemplate(mask,suffix,cv2.TM_CCOEFF_NORMED))
                    if score >= .90:
                        ax,ay=region[0]+x-cut,region[1]+y
                        if ax >= 0 and abs(ax-left)<=2:
                            found.append(Anchor(key,(ax,ay,ax+w,ay+h),score,scale,'partial_template'))
        if not found:
            return anchors
        candidate=max(found,key=lambda a:a.score)
        return tuple(sorted((*anchors,candidate),key=lambda a:FIELDS.index(a.field)))


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
            for scale in self.label_scales:
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
                    for offset_x, offset_y in (product((0.0, 0.5), repeat=2) if 'labels' in self.features else ((0.0,0.0),(0.5,0.0))):
                        matrix = np.float32(
                            [[scale, 0, offset_x], [0, scale, offset_y]]
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
            if seed.score < (0.76 if 'labels' in self.features else 0.80) or any(
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


    def _layout(self, frame: Frame, anchors: tuple[Anchor, ...]) -> PanelLayout | None:
        anchors = self._partial_anchors(frame, anchors)
        scale = float(np.median([a.scale for a in anchors]))
        minimum_pad = 1 if 'small' in self.features and scale < 0.55 else 3
        # Find text runs and a common right edge. Independent rows prevent an
        # isolated unrelated number on the same horizontal line becoming a field.
        runs = {}
        for anchor in anchors:
            # Every row starts after its own label. A longer neighboring label
            # must never cut the leading digits off a compact value column.
            search_left = anchor.box[2] + max(2, round(3 * scale))
            search_right = min(frame.width, search_left + round(280 * scale))
            top = max(0, anchor.box[1] - max(minimum_pad, round(3 * scale)))
            bottom = min(frame.height, anchor.box[3] + max(minimum_pad, round(3 * scale)))
            if search_right <= search_left or bottom <= top:
                return None
            ink = self._value_ink(frame.pixels[top:bottom, search_left:search_right])
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
            for x in range(column_right + max(minimum_pad, round(3 * scale)), limit):
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
        pad = max(minimum_pad, round(3 * scale))
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
            vertical_margin = (1 if minimum_pad == 1 else max(3, math.ceil(3.5 * scale)))
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


VARIANTS = {
    'baseline': (),
    'cache': ('cache',),
    'prune': ('prune',),
    'fast': ('cache','prune'),
    'green': ('cache','prune','green'),
    'labels': ('cache','prune','labels'),
    'small': ('cache','prune','labels','small'),
    'consensus': ('cache','prune','consensus'),
    'topology': ('cache','prune','topology'),
    'partial': ('cache','prune','partial'),
    'vector': ('cache','vector'),
    'glyph_cache': ('cache','vector','glyph_cache'),
    'consensus_glyph': ('cache','vector','glyph_cache','consensus'),
    'robust_numeric': ('cache','vector','glyph_cache','topology','consensus'),
    'combined_fast': ('cache','vector','glyph_cache','green','labels','topology','partial'),
    'combined_robust': ('cache','vector','glyph_cache','green','labels','topology','partial','consensus'),
    'combined_pruned_fast': ('cache','vector','glyph_cache','green','labels','topology','partial','prune'),
    'combined_pruned_robust': ('cache','vector','glyph_cache','green','labels','topology','partial','consensus','prune'),
    'accelerated_fast': ('cache','vector','glyph_cache','green','labels','topology','partial','prune','touching_cache'),
    'accelerated_robust': ('cache','vector','glyph_cache','green','labels','topology','partial','consensus','prune','touching_cache'),
}


def make_reader(variant):
    return ResourceReader() if variant == 'baseline' else ExperimentalResourceReader(VARIANTS[variant])


def make_number_reader(variant):
    if variant=='baseline':
        return NumberReader()
    if 'vector' in VARIANTS[variant]:
        from tools.resource_numeric_vector import VectorNumberReader
        return VectorNumberReader(VARIANTS[variant])
    return ExperimentalNumberReader(VARIANTS[variant])
