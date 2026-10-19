"""Opt-in whole-number fitting to locally rendered native Verdana text.

The recognizer places tabular digits at their measured advances. It never
segments the observation at blank columns. Every supported foreground pixel in
the complete supplied region contributes to the score, including prefix/suffix
pixels outside the winning text. The existing recognizer can be explicitly
supplied as a conservative fallback; confident disagreement remains unknown.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import cv2
import numpy as np

from tibia_client.adaptive_text import MAX_DIGITS, MAX_NUMBER_PIXELS, _rgb_image, _valid_text
from tibia_client.models import Quality, Reading
from tools.resource_native_font import NativeFontUnavailable, font_provenance, get_font


def native_foreground(image: np.ndarray) -> np.ndarray:
    """Current-frame foreground for the documented gray/green/orange/red palette."""
    channels = image.astype(np.int16)
    r, g, b = (channels[:, :, k] for k in range(3))
    gray = (channels.mean(2) >= 125) & (channels.max(2) - channels.min(2) <= 40)
    green = (g >= 95) & (g - r >= 25) & (g - b >= 25)
    orange = (r >= 150) & (g >= 85) & (b <= 140) & (r - g >= 20) & (g - b >= 20)
    red = (r >= 130) & (r - g >= 40) & (r - b >= 30)
    return gray | green | orange | red


@dataclass(frozen=True, slots=True)
class _Fit:
    text: str
    score: float
    minimum: float
    margin: float
    unexplained: float
    unexplained_pixels: int
    scale: float
    position: tuple[int, int]
    alternatives: tuple[tuple[str, float], ...]


class NativeNumberReader:
    supports_font_family = True

    def __init__(self, *, fallback=None, cache=False, cache_fonts=None,
                 calibrate_scale=False, ocr=None):
        self.fallback = fallback
        self.cache_fonts = bool(cache if cache_fonts is None else cache_fonts)
        self.calibrate_scale = bool(calibrate_scale)
        self._calibration = None
        # No implicit OCR process is ever started. If a fallback is supplied it
        # retains its own explicit use_ocr contract.
        self._ocr = ocr

    def configure_calibration(self, scale: float | None, family: str | None = None):
        if scale is not None and (not math.isfinite(scale) or not .35 <= scale <= 4):
            raise ValueError("calibrated scale must be finite and from .35 through 4")
        if family not in (None, "alternate"):
            raise ValueError("unsupported font family")
        self._calibration = None if scale is None else (float(scale), family)

    @property
    def native_available(self):
        try:
            font_provenance()
            return True
        except NativeFontUnavailable:
            return False

    def read(self, image, *, scale=1.0, max_digits=9, allow_grouping=True,
             use_ocr=False, family=None):
        pixels = _rgb_image(image, pixel_limit=MAX_NUMBER_PIXELS)
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or not .35 <= scale <= 4:
            raise ValueError("scale must be a finite number from .35 through 4")
        if type(max_digits) is not int or not 1 <= max_digits <= MAX_DIGITS:
            raise ValueError(f"max_digits must be an integer from 1 through {MAX_DIGITS}")
        if type(allow_grouping) is not bool or type(use_ocr) is not bool:
            raise ValueError("allow_grouping and use_ocr must be boolean")
        if family not in (None, "alternate"):
            raise ValueError("family must be None or 'alternate'")
        mask = native_foreground(pixels)
        diagnostics = {"reader": "native_whole_number", "text": None, "score": 0.,
                       "alternatives": (), "scale": float(scale),
                       "font_cache": self.cache_fonts, "numeric_value_cache": False}
        if np.any(mask) and (np.any(mask[0]) or np.any(mask[-1]) or np.any(mask[:, 0]) or np.any(mask[:, -1])):
            return Reading(None, Quality.UNKNOWN, "number foreground touches the ROI edge and may be clipped", diagnostics)
        native = self._read_native(mask, float(scale), max_digits, allow_grouping, family, diagnostics)
        if self.fallback is None:
            return native
        # Unknown native results do not relax existing-template acceptance.
        baseline = self.fallback.read(pixels, scale=scale, max_digits=max_digits,
                                      allow_grouping=allow_grouping, use_ocr=use_ocr, family=family)
        if baseline.quality is Quality.KNOWN and native.detail in {
                "number has ambiguous native digit alternatives",
                "number has competing whole-string native readings"}:
            # Those reasons are reached only after every native evidence and
            # unexplained-foreground gate passed. A second recognizer choosing
            # one member does not make a strong visual tie readable.
            return Reading(None, Quality.UNKNOWN, native.detail, diagnostics={
                **dict(native.diagnostics), "fallback_value": baseline.value,
                "fallback_blocked_by_native_ambiguity": True})
        if native.quality is Quality.KNOWN and baseline.quality is Quality.KNOWN and native.value != baseline.value:
            return Reading(None, Quality.UNKNOWN, "native and existing recognizers disagree", {
                **dict(native.diagnostics), "fallback_value": baseline.value,
                "fallback_reader": baseline.diagnostics.get("reader")})
        if native.quality is Quality.KNOWN:
            return Reading(native.value, Quality.KNOWN, diagnostics={**dict(native.diagnostics),
                "fallback_agreed": baseline.quality is Quality.KNOWN,
                "fallback_detail": baseline.detail})
        if baseline.quality is Quality.KNOWN:
            return Reading(baseline.value, Quality.KNOWN, diagnostics={**dict(baseline.diagnostics),
                "native_status": native.detail, "native_score": native.diagnostics.get("score"),
                "native_fallback": True})
        return Reading(None, baseline.quality, baseline.detail, diagnostics={
            **dict(baseline.diagnostics), "native_status": native.detail,
            "native_score": native.diagnostics.get("score"),
            "native_text": native.diagnostics.get("text"), "native_fallback": True})

    def _read_native(self, mask, scale, max_digits, allow_grouping, family, diagnostics):
        if not np.any(mask):
            return Reading(None, Quality.UNKNOWN, "number contains no supported native foreground", diagnostics)
        scales = [scale]
        if self.calibrate_scale and self._calibration is not None and self._calibration[1] == family:
            calibrated = self._calibration[0]
            if abs(calibrated - scale) <= max(.08, scale * .12):
                scales.append(calibrated)
        fits = []
        try:
            for candidate_scale in dict.fromkeys(scales):
                bank = get_font(candidate_scale, family=family, cache=self.cache_fonts)
                fits.extend(_fit_number(mask, bank, max_digits, allow_grouping))
            diagnostics["font_sha256"] = bank.source_sha256
        except NativeFontUnavailable as exc:
            return Reading(None, Quality.UNKNOWN, str(exc), diagnostics)
        if not fits:
            return Reading(None, Quality.UNKNOWN, "number does not fit native font geometry", diagnostics)
        by_text = {}
        for fit in fits:
            previous = by_text.get(fit.text)
            if previous is None or fit.score > previous.score:
                by_text[fit.text] = fit
        ordered = sorted(by_text.values(), key=lambda x: x.score, reverse=True)
        best = ordered[0]
        alternatives = tuple({"text": f.text, "score": f.score} for f in ordered[:4])
        diagnostics.update(text=best.text, score=best.score, minimum_glyph_score=best.minimum,
                           digit_margin=best.margin, unexplained_fraction=best.unexplained,
                           unexplained_pixels=best.unexplained_pixels,
                           alternatives=alternatives, native_scale=best.scale, position=best.position)
        if best.score < .89 or best.minimum < .80:
            reason = "number has insufficient whole-string native evidence"
        elif best.unexplained > .02 or best.unexplained_pixels >= 2:
            reason = "number contains unexplained native foreground"
        elif best.margin < .05:
            reason = "number has ambiguous native digit alternatives"
        elif len(ordered) > 1 and ordered[1].score >= best.score - .025:
            reason = "number has competing whole-string native readings"
        else:
            return Reading(int(best.text.replace(",", "")), Quality.KNOWN, diagnostics=diagnostics)
        return Reading(None, Quality.UNKNOWN, reason, diagnostics)


def _patterns(max_digits, allow_grouping):
    for count in range(1, max_digits + 1):
        yield "d" * count
        if count > 3 and allow_grouping:
            first = count % 3 or 3
            yield "d" * first + ",ddd" * ((count - first) // 3)


def _fit_number(mask, bank, max_digits, allow_grouping):
    """Enumerate geometry, then jointly score complete tabular-digit strings.

    Each digit occupies the measured font advance even if its visible strokes
    are narrow. The foreground never determines character boundaries.
    """
    ys, xs = np.where(mask)
    ymin, ymax, xmin, xmax = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())
    ratio = bank.scale / .8
    if ymax - ymin + 1 < 5 * ratio or ymax - ymin + 1 > 12 * ratio:
        return ()
    observed = mask.astype(np.float32)
    total_observed = float(observed.sum())
    digit_advance = bank.glyphs[0].advance
    comma_advance = bank.glyphs[-1].advance
    digits = np.stack([glyph.coverage for glyph in bank.glyphs[:10]])
    comma = bank.glyphs[-1].coverage
    # Compute every placement once per frame. These score maps are ephemeral;
    # no observations, number strings or values survive the read call.
    maps = {}
    for kind, references in (("d", digits), (",", comma[None])):
        h, w = references.shape[1:]
        if h > observed.shape[0] or w > observed.shape[1]:
            return ()
        patch_mass = cv2.matchTemplate(observed, np.ones((h, w), np.float32), cv2.TM_CCORR)
        masses = references.sum(axis=(1, 2))
        intersections = np.stack([cv2.matchTemplate(observed, glyph, cv2.TM_CCORR)
                                  for glyph in references])
        np.maximum(intersections, 0, out=intersections)
        scores = 2 * intersections / np.maximum(1, masses[:, None, None] + patch_mass)
        maps[kind] = (references, masses, intersections, scores)
    hypotheses = []
    for pattern in _patterns(max_digits, allow_grouping):
        extent = sum(digit_advance if token == "d" else comma_advance for token in pattern)
        if not extent - 7 * ratio <= xmax - xmin + 1 <= extent + ratio:
            continue
        for top in range(max(0, ymin - max(1, round(ratio))), min(ymin + 2, mask.shape[0])):
            for start in range(max(0, xmin - max(2, round(4 * ratio))), xmin + 1):
                # Round advances relative to the string origin. Rounding the
                # absolute ROI coordinate makes half-pixel ties depend on the
                # integer origin's parity and changes readings after padding.
                cursor = 0.0
                text = []
                scores = []
                margins = []
                placed = []
                template_total = 0.
                intersection = 0.
                viable = True
                for token in pattern:
                    left = start + round(cursor)
                    references, masses, intersections_map, score_map = maps[token]
                    advance = digit_advance if token == "d" else comma_advance
                    width, height = references.shape[2], references.shape[1]
                    if left < 0 or left + width > mask.shape[1] or top + height > mask.shape[0]:
                        viable = False
                        break
                    local_scores = score_map[:, top, left].copy()
                    intersections = intersections_map[:, top, left]
                    # No multi-digit leading-zero interpretation is permitted.
                    if token == "d" and not text and pattern.count("d") > 1:
                        local_scores[0] = -1
                    order = np.argsort(local_scores)[::-1]
                    choice = int(order[0])
                    if float(local_scores[choice]) < .55:
                        viable = False
                        break
                    text.append(str(choice) if token == "d" else ",")
                    scores.append(float(local_scores[choice]))
                    if token == "d":
                        margins.append(float(local_scores[choice] - local_scores[order[1]]))
                    placed.append((left, top, references[choice]))
                    template_total += float(masses[choice])
                    intersection += float(intersections[choice])
                    cursor += advance
                if not viable:
                    continue
                transcript = "".join(text)
                if not _valid_text(transcript, max_digits, allow_grouping):
                    continue
                # Actual compositing handles fractional advance overlap at
                # resampled scales; all ROI foreground remains in denominator.
                model = np.zeros_like(observed)
                for left, y, glyph in placed:
                    area = model[y:y + glyph.shape[0], left:left + glyph.shape[1]]
                    np.maximum(area, glyph, out=area)
                template_total = float(model.sum())
                intersection = float((model * observed).sum())
                score = 2 * intersection / (template_total + total_observed)
                # Distinguish adjacent antialiasing/compression residue from
                # evidence the complete transcript cannot explain anywhere.
                # The undilated model still determines all recognition scores.
                nearby = cv2.dilate((model >= .45).astype(np.uint8), np.ones((3, 3), np.uint8))
                unexplained = float((observed * (1 - nearby)).sum()) / total_observed
                hypotheses.append(_Fit(transcript, score, min(scores), min(margins),
                                       unexplained, int(round(unexplained * total_observed)),
                                       bank.scale, (start, top), ()))
    return tuple(hypotheses)
