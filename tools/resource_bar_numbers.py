"""Read an independent current-frame maximum from a verified status-bar caption.

This experiment does not replace a resource value with the duplicate caption.
The entire ``current/max`` caption must be visible and readable.  No maximum is
inferred from bar fill, character metadata, annotations, or an earlier frame.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np

from core.perception import Frame, validate_region
from tibia_client.models import Quality
from tools.resource_numeric_vector import VectorNumberReader
from tools.resource_reader_experiments import VARIANTS


@dataclass(frozen=True, slots=True)
class CaptionNumbers:
    current: int
    maximum: int
    scale: float
    text: str
    current_score: float
    maximum_score: float


class BoundedCaptionNumberReader(VectorNumberReader):
    """Use the established acceptance guards without partition enumeration.

    A maximum is optional corroboration.  Captions needing a combinatorial
    touching-digit search remain unavailable; cached font references and the
    ordinary fixed-count scale/threshold checks are retained.  No numeric result
    is cached and no acceptance threshold is weakened.
    """

    def __init__(self):
        super().__init__(VARIANTS["accelerated_fast"])

    def _decode_touching_digits(self, image, scale, max_digits, family=None):
        return None


def _runs(occupied: np.ndarray) -> list[tuple[int, int]]:
    starts = np.flatnonzero(occupied & np.r_[True, ~occupied[:-1]])
    ends = np.flatnonzero(occupied & np.r_[~occupied[1:], True]) + 1
    return [(int(a), int(b)) for a, b in zip(starts, ends)]


def _slash(mask: np.ndarray) -> bool:
    """Require one connected, thin rising diagonal, not a 1, 7, or separator."""
    rows, columns = np.where(mask)
    if not len(rows):
        return False
    tight = mask[rows.min():rows.max() + 1, columns.min():columns.max() + 1]
    height, width = tight.shape
    if height < 7 or not 0.25 <= width / height <= 0.70:
        return False
    positions = [np.flatnonzero(row) for row in tight]
    if any(not len(row) or row[-1] - row[0] + 1 != len(row) for row in positions):
        return False
    if max(map(len, positions)) > max(2, math.ceil(height * 0.30)):
        return False
    centers = np.array([row.mean() for row in positions])
    if centers[0] - centers[-1] < 2 or np.any(np.diff(centers) > 0.5):
        return False
    slope, intercept = np.polyfit(np.arange(height), centers, 1)
    return bool(-0.8 <= slope <= -0.2 and
                np.max(np.abs(centers - (slope * np.arange(height) + intercept))) <= 0.8)


class BarCaptionReader:
    """Conservative complete-caption parser with a fresh-geometry adapter.

    ``parse_caption`` is useful for component diagnostics only.  Consumers must
    use ``read(frame, evidence)`` to obtain a frame-bound independent maximum.
    Like the existing experimental readers, one instance is used by one worker.
    """

    def __init__(self, number_reader: Any | None = None):
        self.number_reader = number_reader or BoundedCaptionNumberReader()
        self.last_diagnostics: dict[str, Any] = {}

    def _reject(self, reason: str):
        self.last_diagnostics["detail"] = reason
        return None

    def parse_caption(self, pixels: np.ndarray, *, expected_center: float | None = None
                      ) -> CaptionNumbers | None:
        self.last_diagnostics = {"reader": "complete_bar_caption", "known": False}
        if (not isinstance(pixels, np.ndarray) or pixels.dtype != np.uint8 or
                pixels.ndim != 3 or pixels.shape[2] != 3 or
                not 3 <= pixels.shape[0] <= 128 or not 3 <= pixels.shape[1] <= 4096):
            return self._reject("unsupported caption image")
        # White lettering is tinted by green/blue bar backgrounds after capture
        # resampling.  The minimum channel suppresses both colored backgrounds
        # without changing the existing numeric reader's foreground threshold.
        gray = np.repeat(np.min(pixels, axis=2)[:, :, None], 3, axis=2)
        mask = gray[:, :, 0] >= 125
        if not np.any(mask):
            return self._reject("caption has no foreground")
        if (np.any(mask[0]) or np.any(mask[-1]) or
                np.any(mask[:, 0]) or np.any(mask[:, -1])):
            return self._reject("caption foreground touches its boundary")
        runs = _runs(mask.any(axis=0))
        if not 3 <= len(runs) <= 19:
            return self._reject("caption has an unsupported number of components")
        slashes = [index for index, (left, right) in enumerate(runs)
                   if _slash(mask[:, left:right])]
        if len(slashes) != 1 or slashes[0] in (0, len(runs) - 1):
            return self._reject("caption does not contain one complete interior slash")
        slash_index = slashes[0]
        left, right = runs[slash_index]
        digit_extents = []
        for index, (start, end) in enumerate(runs):
            if index == slash_index:
                continue
            rows = np.flatnonzero(mask[:, start:end].any(axis=1))
            digit_extents.append((int(rows[0]), int(rows[-1]) + 1))
        digit_top = int(round(float(np.median([a for a, _ in digit_extents]))))
        digit_bottom = int(round(float(np.median([b for _, b in digit_extents]))))
        digit_height = digit_bottom - digit_top
        if not 5 <= digit_height <= 40 or any(
                abs(a - digit_top) > 1 or abs(b - digit_bottom) > 1
                for a, b in digit_extents):
            return self._reject("caption has unexplained foreground or incomplete digits")
        slash_rows = np.flatnonzero(mask[:, left:right].any(axis=1))
        if (not digit_top - 2 <= int(slash_rows[0]) <= digit_top + 1 or
                not digit_bottom + 1 <= int(slash_rows[-1]) + 1 <= digit_bottom + 5):
            return self._reject("slash baseline does not agree with complete digits")
        scale = digit_height / 10.0
        text_center = (runs[0][0] + runs[-1][1]) / 2.0
        center = pixels.shape[1] / 2.0 if expected_center is None else expected_center
        if not math.isfinite(center) or abs(text_center - center) > max(2.0, 2.0 * scale):
            return self._reject("complete caption is not centered in the verified bar")
        # Each half retains all foreground and the original blank margins.  Do
        # not cut digits at fixed gaps or discard an inconvenient prefix/suffix.
        values = []
        for side in (gray[:, :left], gray[:, right:]):
            reading = self.number_reader.read(side, scale=scale, max_digits=9,
                                              allow_grouping=False, use_ocr=False)
            values.append(reading)
        self.last_diagnostics.update(
            scale=scale, current_detail=values[0].detail, maximum_detail=values[1].detail,
            current_text=values[0].diagnostics.get("text"),
            maximum_text=values[1].diagnostics.get("text"))
        if any(value.quality is not Quality.KNOWN or type(value.value) is not int
               for value in values):
            return self._reject("both complete caption numbers must be known")
        current, maximum = (value.value for value in values)
        if not 0 <= current <= maximum or maximum <= 0:
            return self._reject("caption current/max relationship is invalid")
        self.last_diagnostics.update(known=True, detail="complete current-frame current/max caption")
        return CaptionNumbers(current, maximum, scale, f"{current}/{maximum}",
                              float(values[0].diagnostics.get("score", 0.0)),
                              float(values[1].diagnostics.get("score", 0.0)))

    def read(self, frame: Frame, evidence):
        """Return ``FreshMaximum`` only for the verified geometry's same frame."""
        # Import here to keep the parser independent and avoid an import cycle
        # when the geometry module optionally uses this caption reader.
        from tools.resource_bar_evidence import BarEvidence, FreshMaximum, RESOURCES, frame_token

        self.last_diagnostics = {"reader": "complete_bar_caption", "known": False}
        if (not isinstance(frame, Frame) or not isinstance(evidence, BarEvidence) or
                not evidence.usable or evidence.field not in RESOURCES):
            return self._reject("bar geometry is not verified")
        if (evidence.frame_timestamp != frame.captured_at or
                evidence.frame_token != frame_token(frame) or
                not evidence.layout_fingerprint or evidence.caption_bounds is None or
                evidence.bounds is None):
            return self._reject("bar geometry does not belong to this frame")
        try:
            left, top, right, bottom = validate_region(evidence.caption_bounds, frame.size)
        except (ValueError, TypeError, RuntimeError):
            return self._reject("caption bounds are invalid")
        bar_left, bar_top, bar_right, bar_bottom = evidence.bounds
        if not (bar_left <= left < right <= bar_right and
                bar_top <= top < bottom <= bar_bottom):
            return self._reject("caption bounds escape their verified bar")
        parsed = self.parse_caption(frame.pixels[top:bottom, left:right],
                                    expected_center=(bar_left + bar_right) / 2.0 - left)
        if parsed is None:
            return None
        low, high = evidence.fraction_interval
        if not (math.isfinite(low) and math.isfinite(high) and 0 <= low <= high <= 1):
            self.last_diagnostics["known"] = False
            return self._reject("bar fraction interval is invalid")
        # The caption is a second pixel source, not an infallible denominator.
        # It must first corroborate its own bar before it can challenge another
        # reading or authorize a temporal-change bypass.
        fraction = parsed.current / parsed.maximum
        if not low - 1 / parsed.maximum <= fraction <= high + 1 / parsed.maximum:
            self.last_diagnostics["known"] = False
            return self._reject("caption current/max contradicts its measured bar")
        return FreshMaximum(value=parsed.maximum, frame_timestamp=frame.captured_at,
                            layout_fingerprint=evidence.layout_fingerprint,
                            frame_token=evidence.frame_token, independently_verified=True,
                            field=evidence.field, current=parsed.current)
