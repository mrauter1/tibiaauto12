"""Current-frame protection against a second translated resource panel.

Compose ``PanelGuardMixin`` before the row-localization mixin and resource reader.
The inherited reader validates labels and decodes current numbers on every read.
A bounded layout cache reuses geometry only when every declared evidence pixel
and anchor is unchanged.  A separate guard retains one immutable frame and its
duplicate-panel decision.  Full pixel equality can reuse that decision; matching
dimensions, timestamps, labels, or resource values cannot.

This is a bounded check for translated copies of the verified label constellation,
not a complete detector for remote panels with a different font or capture scale.
Those broader ambiguities still require full discovery.  There is no font
calibration or stored numeric reading in this module.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from hashlib import sha256

import cv2
import numpy as np

from core.perception import Frame
from core import native_visual_guard
from tibia_client.adaptive import PanelLayout, _crop, _ink


_LAYOUT_CACHE_LIMIT = 32


def _candidate_coordinates(pixels, sentinels, seed_coarse, out_height, out_width):
    """Exact proposal filter; missing optional native code uses the NumPy path."""
    samples = tuple((int(dy), int(dx), bool(expected), bool(seed_coarse[dy, dx]))
                    for (dy, dx), expected in sentinels)
    native = native_visual_guard.try_candidates(pixels, samples, out_width, out_height)
    if native is not None:
        return native
    coarse = cv2.inRange(pixels, (130, 130, 130), (255, 255, 255)) > 0
    proposed = None
    for (dy, dx), _ in sentinels[:4]:
        expected = seed_coarse[dy, dx]
        part = coarse[dy:dy + out_height, dx:dx + out_width]
        matched = part if expected else ~part
        if proposed is None:
            proposed = matched.copy()
        else:
            proposed &= matched
    ys, xs = np.nonzero(proposed)
    for (dy, dx), expected in sentinels:
        if not len(xs):
            break
        rgb = pixels[ys + dy, xs + dx]
        low = np.minimum(np.minimum(rgb[:, 0], rgb[:, 1]), rgb[:, 2])
        high = np.maximum(np.maximum(rgb[:, 0], rgb[:, 1]), rgb[:, 2])
        keep = ((low >= 130) & (high - low < 35)) == expected
        ys, xs = ys[keep], xs[keep]
    # Only the first 129 row-major survivors matter: >128 always rejects.
    return ys[:129], xs[:129]


class PanelGuardMixin:
    """Verify current rows and reject independently supported translated panels."""

    def __init__(self, *args, **kwargs):
        self._layout_cache = OrderedDict()
        self._duplicate_frame = None
        self._duplicate_identity = None
        self._duplicate_result = False
        self.panel_guard_stats = dict(duplicate_checks=0, duplicate_pixel_hits=0,
                                      frame_identity_hits=0, discovery_seed_hits=0,
                                      layout_hits=0, layout_misses=0)
        super().__init__(*args, **kwargs)

    def _layout(self, frame, anchors):
        evidence_boxes = getattr(super(), "geometry_evidence_boxes", None)
        boxes = tuple(evidence_boxes(frame, anchors)) if evidence_boxes and anchors else ()
        if not boxes:
            return super()._layout(frame, anchors)
        # StructuralRowMixin declares every label, row, possible added column,
        # and panel-edge pixel used by layout construction, beyond accepted ROIs.
        digest = sha256()
        for box in boxes:
            digest.update(np.ascontiguousarray(_crop(frame, box)))
        key = (frame.size, anchors, boxes, digest.digest())
        if key in self._layout_cache:
            self.panel_guard_stats["layout_hits"] += 1
            self._layout_cache.move_to_end(key)
            return self._layout_cache[key]
        self.panel_guard_stats["layout_misses"] += 1
        result = super()._layout(frame, anchors)
        self._layout_cache[key] = result
        while len(self._layout_cache) > _LAYOUT_CACHE_LIMIT:
            self._layout_cache.popitem(last=False)
        return result

    @staticmethod
    def _panel_guard_identity(layout: PanelLayout):
        # A caller-supplied fingerprint alone is not enough to identify all
        # evidence used by this decision.  Store immutable geometry/guards too.
        return (layout.fingerprint, layout.bounds, layout.frame_size, layout.scale,
                layout.anchors, layout.guards, tuple(sorted(layout.regions.items())))

    def discover(self, frame: Frame, **kwargs):
        result = super().discover(frame, **kwargs)
        if result.layout is not None and kwargs.get("selection") is None:
            # Unrestricted successful discovery already established uniqueness
            # on this immutable frame. Scoped discovery cannot make that claim.
            self._duplicate_frame = frame
            self._duplicate_identity = self._panel_guard_identity(result.layout)
            self._duplicate_result = False
            self.panel_guard_stats["discovery_seed_hits"] += 1
        elif result.layout is None:
            self._duplicate_frame = None
            self._duplicate_identity = None
            self._duplicate_result = False
        return result

    def _has_translated_duplicate(self, frame: Frame, layout: PanelLayout) -> bool:
        identity = self._panel_guard_identity(layout)
        if self._duplicate_identity == identity:
            if self._duplicate_frame is frame:
                self.panel_guard_stats["frame_identity_hits"] += 1
                return self._duplicate_result
            if (self._duplicate_frame is not None and frame.size == self._duplicate_frame.size
                    and frame._rgb == self._duplicate_frame._rgb):
                self.panel_guard_stats["duplicate_pixel_hits"] += 1
                self._duplicate_frame = frame
                return self._duplicate_result
        self.panel_guard_stats["duplicate_checks"] += 1
        self._duplicate_frame = frame
        self._duplicate_identity = identity
        self._duplicate_result = False
        seed = max(layout.anchors, key=lambda a: (a.box[2] - a.box[0]) * (a.box[3] - a.box[1]))
        template = _ink(_crop(frame, seed.box)) > 0
        height, width = template.shape
        if height > frame.height or width > frame.width:
            return False
        pixels = frame.pixels
        # One brightness proposal plus sparse chroma tests avoids a full-frame
        # sweep for every sentinel. Full label/row proof still decides candidates.
        seed_coarse = cv2.inRange(_crop(frame, seed.box), (130, 130, 130), (255, 255, 255)) > 0
        positive, negative = np.argwhere(template), np.argwhere(~template)
        if not len(positive):
            return False
        positives = [(tuple(positive[i]), True) for i in
                     np.linspace(0, len(positive) - 1, min(12, len(positive)), dtype=int)]
        negatives = [(tuple(negative[i]), False) for i in
                     np.linspace(0, len(negative) - 1, min(8, len(negative)), dtype=int)]
        sentinels = [item for pair in zip(positives, negatives) for item in pair]
        sentinels.extend(positives[len(negatives):])
        out_height, out_width = frame.height - height + 1, frame.width - width + 1
        ys, xs = _candidate_coordinates(pixels, sentinels, seed_coarse, out_height, out_width)
        # Bound costly verification under pathological repeated word patterns.
        if len(xs) > 128:
            self._duplicate_result = True
            return True
        for x, y in zip(xs, ys):
            dx, dy = int(x) - seed.box[0], int(y) - seed.box[1]
            if abs(dx) <= 3 * layout.scale and abs(dy) <= 3 * layout.scale:
                continue
            translated = []
            for anchor, saved in zip(layout.anchors, layout.guards):
                left, top, right, bottom = anchor.box
                box = (left + dx, top + dy, right + dx, bottom + dy)
                if box[0] < 0 or box[1] < 0 or box[2] > frame.width or box[3] > frame.height:
                    break
                observed = _ink(_crop(frame, box)) > 0
                expected = np.frombuffer(saved, np.uint8).reshape(observed.shape) > 0
                union = np.count_nonzero(observed | expected)
                if not union or np.count_nonzero(observed != expected) / union > .12:
                    break
                translated.append(replace(anchor, box=box))
            if (len(translated) == len(layout.anchors)
                    and self._layout(frame, tuple(translated)) is not None):
                self._duplicate_result = True
                return True
        return False

    def _current_layout(self, frame: Frame, layout: PanelLayout) -> PanelLayout | None:
        current = super()._current_layout(frame, layout)
        if current is None or not current.anchors or len(current.guards) != len(current.anchors):
            return None
        if self._has_translated_duplicate(frame, current):
            return None
        return current
