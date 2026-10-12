"""Health/mana bar scanning against one coherent client snapshot."""

from __future__ import annotations

from typing import Optional

import numpy as np

from core.HookWindow import DetectionError, Frame, TakeImage


def _rgb(color, label: str) -> np.ndarray:
    try:
        channels = tuple(int(value) for value in color)
    except (TypeError, ValueError) as exc:
        raise DetectionError(f"{label} must contain three RGB channels") from exc
    if len(channels) != 3 or any(value < 0 or value > 255 for value in channels):
        raise DetectionError(f"{label} must contain three RGB channels from 0 through 255")
    return np.asarray(channels, dtype=np.int16)


class ScanStages:
    """Quantize a calibrated 100-pixel status bar in five-percent steps."""

    def __init__(self, name, color_tolerance: int = 3):
        if not isinstance(color_tolerance, int) or color_tolerance < 0:
            raise ValueError("color_tolerance must be a non-negative integer")
        self.stage: Optional[int] = None
        self.name = name
        self.color_tolerance = color_tolerance

    def scan(self, localization, color, color_full, *, frame=None, tolerance: Optional[int] = None):
        try:
            anchor_x, anchor_y = int(localization[0]), int(localization[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise DetectionError("bar localization must contain x and y") from exc
        allowed = self.color_tolerance if tolerance is None else tolerance
        if not isinstance(allowed, int) or allowed < 0:
            raise DetectionError("color tolerance must be a non-negative integer")

        normal = _rgb(color, "bar color")
        full = _rgb(color_full, "full bar color")
        # One crop means one capture even without an enclosing frame_scope.
        samples = np.asarray(
            TakeImage((anchor_x + 5, anchor_y, anchor_x + 101, anchor_y + 1), frame=frame),
            dtype=np.int16,
        )[0]

        def close(pixel: np.ndarray, expected: np.ndarray) -> bool:
            return bool(np.max(np.abs(pixel - expected)) <= allowed)

        # The legacy calibration anchors the far endpoint at x+100. Full bars
        # use a distinct color; some themes retain the regular fill color.
        if close(samples[95], full) or close(samples[95], normal):
            self.stage = 100
            return self.stage
        for percent in range(95, 0, -5):
            if close(samples[percent - 5], normal):
                self.stage = percent
                return self.stage
        self.stage = None
        return None

    def ScanStages(self, Localization, color, colorFull, frame: Optional[Frame] = None, tolerance=None):
        """Compatibility spelling used by the existing automation modules."""

        return self.scan(Localization, color, colorFull, frame=frame, tolerance=tolerance)
