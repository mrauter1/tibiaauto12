"""Opt-in structural row localization; numeric decoding remains injectable.

Compose ``class Reader(StructuralRowMixin, ExperimentalResourceReader)`` and
pass the ordinary experiment features plus an optional ``number_reader``.  This
module consumes pixels and existing semantic-label templates only.  It never
uses corpus identity, annotations, saved values, or installation assets.
"""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import math

import numpy as np

from core.perception import Frame, validate_region
from tibia_client.adaptive import Anchor, FIELDS, PanelLayout, _crop, _ink


class StructuralRowMixin:
    """Use verified panel edges to authorize a whole, independent value row.

    Existing accepted regions are retained.  A rejected row can be recovered
    only inside the same observed panel divider and at the same value edge.
    One/two-label panels additionally require both vertical chrome edges with
    native-sidebar-compatible geometry.  The decoder sees every foreground
    component between label and divider; it must reject competing strings,
    non-digits, abnormal spacing and clipping itself.
    """

    def geometry_evidence_boxes(self, frame, anchors):
        """Conservative dependency envelope for the independent cache mixin."""
        if not anchors:
            return ()
        scale = max(a.scale for a in anchors)
        return ((
            max(0, min(a.box[0] for a in anchors) - math.ceil(30 * scale)),
            max(0, min(a.box[1] for a in anchors) - math.ceil(130 * scale)),
            min(frame.width, max(a.box[2] for a in anchors) + math.ceil(300 * scale)),
            min(frame.height, max(a.box[3] for a in anchors) + math.ceil(130 * scale)),
        ),)

    def _constellations(self, anchors):
        groups = list(super()._constellations(anchors))
        # A singleton is only a proposal here.  _layout must independently
        # demonstrate panel chrome; a chat label followed by digits cannot pass
        # on semantic matching alone.
        for origin in sorted(anchors, key=lambda a: a.score, reverse=True):
            if origin.method == "ocr" or origin.score < .84:
                continue
            nearby = [a for a in anchors
                      if a.method != "ocr" and a.score >= .84
                      and abs(a.box[0] - origin.box[0]) <= 5 * max(a.scale, origin.scale)
                      and abs(a.box[1] - origin.box[1]) <= 100 * origin.scale
                      and .75 <= a.scale / origin.scale <= 1.33]
            if any(any(a.field == origin.field
                           and abs(a.box[0] - origin.box[0]) <= 5 * max(a.scale, origin.scale)
                           and abs(a.box[1] - origin.box[1]) <= max(3, 5 * origin.scale)
                       for a in group) for group in groups):
                continue
            # Repeated captions on nearby stacked panels are separate evidence.
            # Do not let the strongest occurrence of this same field hide an
            # independent panel merely because it lies within the search span.
            chosen = {origin.field: origin}
            for anchor in sorted(nearby, key=lambda a: a.score, reverse=True):
                chosen.setdefault(anchor.field, anchor)
            group = tuple(chosen[key] for key in FIELDS if key in chosen)
            if not 1 <= len(group) < 3:
                continue
            if len(group) == 2:
                first, second = group
                step = ((second.box[1] - first.box[1]) /
                        (FIELDS.index(second.field) - FIELDS.index(first.field)))
                if not 9 * origin.scale <= step <= 28 * origin.scale:
                    continue
            groups.append(group)
        return groups

    @staticmethod
    def _vertical_edges(frame, left, right, top, bottom):
        """Continuous luminance transitions, not isolated digit strokes."""
        left, right = max(0, left), min(frame.width - 1, right)
        top, bottom = max(0, top), min(frame.height, bottom)
        if right <= left or bottom - top < 16:
            return ()
        stripe = frame.pixels[top:bottom, left:right + 1].mean(axis=2)
        changes = np.abs(np.diff(stripe, axis=1))
        selected = ((np.median(changes, axis=0) >= 20)
                    & (np.mean(changes >= 15, axis=0) >= .8))
        return tuple(left + int(x) for x in np.flatnonzero(selected))

    def _right_divider(self, frame, anchors, column_right, scale):
        top = min(a.box[1] for a in anchors)
        bottom = max(a.box[3] for a in anchors)
        # Short panels need chrome outside the text line to establish that the
        # proposed edge is a panel boundary, not a tall numeric glyph.
        if bottom - top < 24 * scale:
            top -= round(20 * scale)
            bottom += round(4 * scale)
        edges = self._vertical_edges(
            frame, column_right + max(3, round(3 * scale)),
            column_right + round(32 * scale), top, bottom)
        return edges[0] if edges else None

    def _whole_row(self, frame, anchor, right, divider, scale):
        """Bound the entire current numeric foreground without joining gaps."""
        pad = max(3, round(3 * scale))
        left = anchor.box[2] + pad
        top = max(0, anchor.box[1] - pad)
        bottom = min(frame.height, anchor.box[3] + pad)
        # Include all foreground up to chrome, not just a convenient suffix at
        # the inferred value edge.  A foreign column stays in the decoder ROI.
        edge = divider
        if edge <= left or bottom <= top:
            return None
        ink = self._value_ink(frame.pixels[top:bottom, left:edge])
        yy, xx = np.where(ink)
        if not len(xx):
            return None
        observed_right = left + int(xx.max()) + 1
        height = int(yy.max() - yy.min()) + 1
        if (abs(observed_right - right) > max(2, 3 * scale)
                or height < 5 * scale or height > 16 * scale
                or int(yy.min()) == 0 or int(yy.max()) == ink.shape[0] - 1
                or bool(np.any(ink[:, 0])) or bool(np.any(ink[:, -1]))):
            return None
        margin = max(3, math.ceil(3.5 * scale))
        region = (left, top + int(yy.min()) - margin,
                  right + pad, top + int(yy.max()) + 1 + margin)
        try:
            validate_region(region, frame.size)
        except (ValueError, RuntimeError):
            return None
        return region

    def _partial_panel_layout(self, frame, anchors):
        scale = float(np.median([a.scale for a in anchors]))
        left = round(float(np.median([a.box[0] for a in anchors])))
        top = min(a.box[1] for a in anchors) - round(20 * scale)
        bottom = max(a.box[3] for a in anchors) + round(4 * scale)
        left_edges = self._vertical_edges(frame, left - round(20 * scale),
                                         left - max(2, round(3 * scale)), top, bottom)
        # A native 176px sidebar becomes roughly 220 units in the historical
        # template scale.  The value pane ends before its scrollbar; allow its
        # observed 150..215-unit width without fixing any desktop coordinate.
        right_edges = self._vertical_edges(frame, left + round(150 * scale),
                                          left + round(215 * scale), top, bottom)
        if not left_edges or not right_edges:
            return None
        candidate_rows = []
        for divider in right_edges:
            regions = {}
            observed_rights = []
            for anchor in anchors:
                pad = max(3, round(3 * scale))
                x0 = anchor.box[2] + pad
                y0, y1 = max(0, anchor.box[1] - pad), min(frame.height, anchor.box[3] + pad)
                if divider <= x0:
                    continue
                ink = self._value_ink(frame.pixels[y0:y1, x0:divider])
                yy, xx = np.where(ink)
                if not len(xx):
                    continue
                right = x0 + int(xx.max()) + 1
                # Values have a small inset from independently seen chrome.
                if not max(3, 3 * scale) <= divider - right <= max(9, 12 * scale):
                    continue
                region = self._whole_row(frame, anchor, right, divider, scale)
                if region is not None:
                    regions[anchor.field] = region
                    observed_rights.append(right)
            if regions and max(observed_rights) - min(observed_rights) <= max(2, 3 * scale):
                signature = tuple(regions.items())
                if not any(signature == old[0] for old in candidate_rows):
                    candidate_rows.append((signature, regions, int(np.median(observed_rights)), divider))
        if len(candidate_rows) != 1:
            return None
        _, regions, right, divider = candidate_rows[0]
        pad = max(3, round(3 * scale))
        # The observed chrome identifies geometry.  A changed final digit can
        # change the last occupied pixel through its side bearing; that is not
        # a new panel layout and must not require full-frame rediscovery.
        bounds = (max(0, left - pad), max(0, min(a.box[1] for a in anchors) - pad),
                  divider, min(frame.height, max(a.box[3] for a in anchors) + pad))
        guards = tuple(_ink(_crop(frame, a.box)).tobytes() for a in anchors)
        identity = repr((frame.size, bounds, tuple((a.field, a.box) for a in anchors),
                         "structural_partial", divider)).encode()
        return PanelLayout(bounds, anchors, regions, scale, sha256(identity).hexdigest(), frame.size, guards)

    def _layout(self, frame: Frame, anchors: tuple[Anchor, ...]):
        if not anchors:
            return None
        if len(anchors) < 3:
            return self._partial_panel_layout(frame, anchors)
        baseline = super()._layout(frame, anchors)
        if baseline is None or len(baseline.regions) == len(baseline.anchors):
            return baseline
        scale = baseline.scale
        right = baseline.bounds[2] - max(3, round(3 * scale))
        divider = self._right_divider(frame, baseline.anchors, right, scale)
        if divider is None:
            return baseline
        regions = dict(baseline.regions)
        for anchor in baseline.anchors:
            if anchor.field not in regions:
                region = self._whole_row(frame, anchor, right, divider, scale)
                if region is not None:
                    regions[anchor.field] = region
        return replace(baseline, regions=regions)

    def _current_layout(self, frame, layout):
        if frame.size != layout.frame_size or not layout.anchors:
            return None
        if len(layout.anchors) != len(layout.guards):
            return None
        for anchor, saved in zip(layout.anchors, layout.guards):
            observed = _ink(_crop(frame, anchor.box))
            if observed.size != len(saved):
                return None
            expected = np.frombuffer(saved, dtype=np.uint8).reshape(observed.shape)
            union = np.count_nonzero((observed > 0) | (expected > 0))
            if not union or np.count_nonzero(observed != expected) / union > .12:
                return None
        current = self._layout(frame, layout.anchors)
        return current if current is not None and current.fingerprint == layout.fingerprint else None
