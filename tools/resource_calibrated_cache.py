"""Opt-in current-frame geometry and native-font calibration experiment.

Compose ``GeometryCacheMixin`` before ``StructuralRowMixin`` (when used) and
``ExperimentalResourceReader``.  The cache stores geometry and font evidence,
never resource readings.  Its key includes every current pixel consulted by
the local layout builder, including all number rows and potential extra columns.
Thus even a one-pixel numeric edit rebuilds the row eligibility proof.

The baseline already caches glyph computations.  This experiment additionally
memoizes unchanged *layout evidence*, fits native capture scale from labels,
and enables the native decoder's explicitly optional scaled-font cache.  It
does not recover a value solely because that value was visible in an old frame.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from hashlib import sha256
import math

import cv2
import numpy as np

from core.perception import Frame
from tibia_client.adaptive import Anchor, PanelLayout, _crop, _ink, _tight


@dataclass(frozen=True, slots=True)
class FontCalibration:
    """Current visible-label evidence; no numeric transcript is retained."""

    scale: float | None
    family: str | None
    score: float
    matched_labels: int
    detail: str


def _bounded_put(cache: OrderedDict, key, value, limit: int):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def _foreground_fast(rgb: np.ndarray) -> np.ndarray:
    """The exact adaptive._ink predicate using OpenCV channel operations."""
    red, green, blue = cv2.split(rgb)
    low = cv2.min(cv2.min(red, green), blue)
    high = cv2.max(cv2.max(red, green), blue)
    return (low >= 130) & (cv2.subtract(high, low) < 35)


def _aligned_f1(observed: np.ndarray, expected: np.ndarray) -> float:
    """Small translation allowance; no resampling of observed evidence."""
    observed = observed > 0
    expected = expected > 0
    if abs(observed.shape[0] - expected.shape[0]) > 2 or abs(
        observed.shape[1] - expected.shape[1]
    ) > 2:
        return 0.0
    denominator = int(np.count_nonzero(observed) + np.count_nonzero(expected))
    if not denominator:
        return 0.0
    canvas = np.pad(observed.astype(np.uint8), 2)
    if canvas.shape[0] < expected.shape[0] or canvas.shape[1] < expected.shape[1]:
        return 0.0
    overlap = cv2.matchTemplate(canvas.astype(np.float32),
                               expected.astype(np.float32), cv2.TM_CCORR)
    return min(1.0, 2 * float(overlap.max()) / denominator)


class GeometryCacheMixin:
    """Bounded geometry caching plus conservatively fitted capture scale.

    ``geometry_cache=False`` disables geometry memoization for diagnostics.
    ``calibration_enabled=False`` disables fitting and decoder calibration.
    ``duplicate_guard`` detects translated duplicates of the currently verified
    label constellation.  It is an additional current-frame check, not a claim
    that all differently scaled/fonted remote panels can be found cheaply.
    A full discover remains necessary when those broader ambiguities matter.
    """

    def __init__(self, *args, geometry_cache: bool = True,
                 calibration_enabled: bool = True, duplicate_guard: bool = True,
                 cache_size: int = 32, **kwargs):
        if not isinstance(cache_size, int) or not 1 <= cache_size <= 256:
            raise ValueError("cache_size must be an integer from 1 through 256")
        self.geometry_cache_enabled = bool(geometry_cache)
        self.calibration_enabled = bool(calibration_enabled)
        self.duplicate_guard_enabled = bool(duplicate_guard)
        self._geometry_cache_limit = cache_size
        self._geometry_cache = OrderedDict()
        self._font_calibrations = OrderedDict()
        self._duplicate_frame = None
        self._duplicate_fingerprint = None
        self._duplicate_result = False
        self._active_calibration = None
        self.cache_stats = dict(layout_hits=0, layout_misses=0,
                                calibration_hits=0, calibration_misses=0,
                                duplicate_checks=0, duplicate_pixel_hits=0)
        super().__init__(*args, **kwargs)
        # Both attributes are explicit experiment knobs on NativeNumberReader.
        # Existing numeric readers receive no unsupported keyword arguments.
        if hasattr(self.number_reader, "cache_fonts"):
            self.number_reader.cache_fonts = True
        if hasattr(self.number_reader, "calibrate_scale"):
            self.number_reader.calibrate_scale = self.calibration_enabled

    def discover(self, frame: Frame, **kwargs):
        """Prepare the unique selected model during background acquisition."""
        result = super().discover(frame, **kwargs)
        if result.layout is not None:
            if self.calibration_enabled:
                self._set_decoder_calibration(self._label_calibration(frame, result.layout))
            # An unrestricted successful discovery has already established
            # unique panel evidence on this exact immutable snapshot. A scoped
            # discovery cannot establish that fact for the rest of the frame.
            if kwargs.get("selection") is None:
                self._duplicate_frame = frame
                self._duplicate_fingerprint = result.layout.fingerprint
                self._duplicate_result = False
        else:
            self._set_decoder_calibration(None)
        return result

    def geometry_evidence_boxes(self, frame: Frame, anchors: tuple[Anchor, ...]):
        """Conservative envelope covering baseline and structural layout reads.

        A composed geometry builder may declare its own larger evidence boxes.
        The baseline's partial-label recovery and all value-column searches are
        covered by this envelope, including pixels outside accepted number ROIs.
        """
        delegate = getattr(super(), "geometry_evidence_boxes", None)
        if delegate is not None:
            return delegate(frame, anchors)
        scale = max(anchor.scale for anchor in anchors)
        return ((max(0, min(a.box[0] for a in anchors) - math.ceil(30 * scale)),
                 max(0, min(a.box[1] for a in anchors) - math.ceil(130 * scale)),
                 min(frame.width, max(a.box[2] for a in anchors) + math.ceil(300 * scale)),
                 min(frame.height, max(a.box[3] for a in anchors) + math.ceil(130 * scale))),)

    def _layout(self, frame: Frame, anchors: tuple[Anchor, ...]) -> PanelLayout | None:
        if not self.geometry_cache_enabled or not anchors:
            return super()._layout(frame, anchors)
        boxes = self.geometry_evidence_boxes(frame, anchors)
        digest = sha256()
        for box in boxes:
            digest.update(repr(box).encode())
            digest.update(np.ascontiguousarray(_crop(frame, box)))
        key = (frame.size, anchors, digest.digest())
        if key in self._geometry_cache:
            self.cache_stats["layout_hits"] += 1
            self._geometry_cache.move_to_end(key)
            return self._geometry_cache[key]
        self.cache_stats["layout_misses"] += 1
        result = super()._layout(frame, anchors)
        _bounded_put(self._geometry_cache, key, result, self._geometry_cache_limit)
        return result

    def _label_calibration(self, frame: Frame, layout: PanelLayout) -> FontCalibration:
        key = (layout.scale, tuple((a.field, a.box, a.method) for a in layout.anchors),
               tuple(_ink(_crop(frame, a.box)).tobytes() for a in layout.anchors))
        if key in self._font_calibrations:
            self.cache_stats["calibration_hits"] += 1
            self._font_calibrations.move_to_end(key)
            return self._font_calibrations[key]
        self.cache_stats["calibration_misses"] += 1
        result = self._fit_label_calibration(frame, layout)
        _bounded_put(self._font_calibrations, key, result, self._geometry_cache_limit)
        return result

    def _fit_label_calibration(self, frame: Frame, layout: PanelLayout) -> FontCalibration:
        # There is no support for inferring a scale from one weak or elided row.
        labels = {
            "resource_health": ("Hit Points", "Health"),
            "resource_mana": ("Mana",),
            "resource_soul": ("Soul Points",),
            "resource_capacity": ("Capacity",),
        }
        usable = [a for a in layout.anchors if a.method not in ("partial_template", "ocr")]
        if len(usable) < 2:
            return FontCalibration(None, None, 0, len(usable),
                                   "at least two complete labels are required for scale calibration")
        try:
            from tools.resource_native_font import NativeFontUnavailable, render_native_text
        except ImportError:
            return FontCalibration(None, None, 0, 0, "native font renderer unavailable")
        try:
            observed = {a.field: _tight(_ink(_crop(frame, a.box))) for a in usable}
            width_scales = []
            for anchor in usable:
                for text in labels[anchor.field]:
                    native = render_native_text(text, .8, cache=True)
                    width_scales.append(.8 * observed[anchor.field].shape[1] / native.shape[1])
            nearby = [s for s in width_scales if abs(s - layout.scale) <= max(.08, .12 * layout.scale)]
            center = float(np.median(nearby)) if nearby else layout.scale
            scales = {round(layout.scale, 4), round(center, 4)}
            scales.update(round(center + offset, 4) for offset in (-.04, -.02, .02, .04))
            # The installed native raster scale is an explicit model candidate,
            # accepted only through current visible labels, never by filename.
            if abs(layout.scale - .8) <= .1:
                scales.add(.8)
            candidates = []
            for scale in sorted(scales):
                if not .35 <= scale <= 4:
                    continue
                scores, signature = [], []
                for anchor in usable:
                    choices = []
                    for text in labels[anchor.field]:
                        expected = render_native_text(text, scale, cache=True) >= .5
                        choices.append((_aligned_f1(observed[anchor.field], expected), expected))
                    score, expected = max(choices, key=lambda item: item[0])
                    scores.append(score)
                    signature.append((expected.shape, expected.tobytes()))
                strong = sum(score >= .90 for score in scores)
                candidates.append((float(np.mean(scores)), strong, scale, tuple(signature)))
        except (NativeFontUnavailable, ValueError):
            return FontCalibration(None, None, 0, 0, "native font cannot explain current labels")
        candidates.sort(key=lambda item: (item[0], -abs(item[2] - center)), reverse=True)
        best = candidates[0]
        rival = next((item for item in candidates[1:] if item[3] != best[3]), None)
        if best[0] < .92 or best[1] < 2 or (rival is not None and best[0] - rival[0] < .025):
            return FontCalibration(None, None, best[0], best[1],
                                   "native scale lacks a unique strong multi-label fit")
        return FontCalibration(best[2], None, best[0], best[1],
                               "native scale established by current complete labels")

    def _has_translated_duplicate(self, frame: Frame, layout: PanelLayout) -> bool:
        # Reusing a check for the very same immutable Frame is safe. New capture
        # objects must supply identical *complete pixel bytes* to reuse it;
        # matching dimensions, timestamps, labels or numeric values is not enough.
        if self._duplicate_frame is frame and self._duplicate_fingerprint == layout.fingerprint:
            return self._duplicate_result
        if (self._duplicate_frame is not None and
            self._duplicate_fingerprint == layout.fingerprint and
            frame.size == self._duplicate_frame.size and
            frame._rgb == self._duplicate_frame._rgb):
            self.cache_stats["duplicate_pixel_hits"] += 1
            self._duplicate_frame = frame
            return self._duplicate_result
        self.cache_stats["duplicate_checks"] += 1
        self._duplicate_frame = frame
        self._duplicate_fingerprint = layout.fingerprint
        self._duplicate_result = False
        seed = max(layout.anchors, key=lambda a: (a.box[2] - a.box[0]) * (a.box[3] - a.box[1]))
        template = _ink(_crop(frame, seed.box)) > 0
        height, width = template.shape
        if height > frame.height or width > frame.width:
            return False
        pixels = frame.pixels
        # A single cheap brightness proposal replaces building several complete
        # RGB min/max planes. Chroma is checked at sparse proposed coordinates.
        # Coarse expected bits use the same predicate on the current seed, so
        # exact translated copies cannot disappear in this proposal step.
        coarse = cv2.inRange(pixels, (130, 130, 130), (255, 255, 255)) > 0
        seed_coarse = coarse[seed.box[1]:seed.box[3], seed.box[0]:seed.box[2]]
        positive = np.argwhere(template)
        negative = np.argwhere(~template)
        if not len(positive):
            return False
        # Exact sentinel pixels cheaply propose translated copies. The full
        # saved label constellation and independent layout proof decide them.
        positives = [(tuple(positive[i]), True) for i in
                     np.linspace(0, len(positive) - 1, min(12, len(positive)), dtype=int)]
        negatives = [(tuple(negative[i]), False) for i in
                     np.linspace(0, len(negative) - 1, min(8, len(negative)), dtype=int)]
        # Include contrasting foreground/background in the coarse pass; broad
        # bright surfaces otherwise leave very many expensive proposals.
        sentinels = [item for pair in zip(positives, negatives) for item in pair]
        sentinels.extend(positives[len(negatives):])
        out_height, out_width = frame.height - height + 1, frame.width - width + 1
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
        # After four coarse sentinels, continue only at candidate coordinates.
        # Sweeping the complete desktop once per remaining pixel was wasteful
        # and materially increased fresh-capture latency on large screenshots.
        for (dy, dx), expected in sentinels:
            if not len(xs):
                return False
            rgb = pixels[ys + dy, xs + dx]
            low = np.minimum(np.minimum(rgb[:, 0], rgb[:, 1]), rgb[:, 2])
            high = np.maximum(np.maximum(rgb[:, 0], rgb[:, 1]), rgb[:, 2])
            keep = ((low >= 130) & (high - low < 35)) == expected
            ys, xs = ys[keep], xs[keep]
        # Thousands of exact word-like repetitions are evidence of ambiguous
        # content, not permission to spend an unbounded amount of hot-loop work.
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
            if len(translated) == len(layout.anchors) and self._layout(frame, tuple(translated)) is not None:
                self._duplicate_result = True
                return True
        return False

    def _set_decoder_calibration(self, calibration: FontCalibration | None):
        self._active_calibration = calibration
        configure = getattr(self.number_reader, "configure_calibration", None)
        if configure is not None:
            configure(calibration.scale if calibration is not None else None,
                      family=calibration.family if calibration is not None else None)

    def _current_layout(self, frame: Frame, layout: PanelLayout) -> PanelLayout | None:
        current = super()._current_layout(frame, layout)
        if current is None or (self.duplicate_guard_enabled and
                               self._has_translated_duplicate(frame, current)):
            self._set_decoder_calibration(None)
            return None
        self._set_decoder_calibration(
            self._label_calibration(frame, current) if self.calibration_enabled else None
        )
        return current

