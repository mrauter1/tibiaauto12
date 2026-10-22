"""Probe-only cheap host checks between phases of one short frame lease.

No screenshot, EnumWindows, foreground change, or full occlusion scan is made.
These partial native checks cannot re-establish guest focus or pixel readiness.
Construct/bind only after an explicitly armed worker receives its run command.
"""

from __future__ import annotations

from numbers import Integral
import time

from tibia_client.window_target import _native_backend, _region, _size, WindowTargetError


MAX_REGIONS = 16
MAX_SAMPLE_POINTS = 64
MAX_TELEMETRY = 128


def _samples(size, point, regions):
    if regions is None or isinstance(regions, (list, tuple)) and not regions:
        regions = [(0, 0, *size)]
    if not isinstance(regions, (list, tuple)) or len(regions) > MAX_REGIONS:
        raise ValueError(f"regions must contain at most {MAX_REGIONS} bounded rectangles")
    boxes = [_region(value, size, "cheap-check region") for value in regions]
    points = set()
    for left, top, right, bottom in boxes:
        points.update(((left, top), (right - 1, top), (left, bottom - 1),
                       (right - 1, bottom - 1),
                       (left + (right - left - 1) // 2, top + (bottom - top - 1) // 2)))
    if point is not None:
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or any(isinstance(value, bool) or not isinstance(value, Integral) for value in point)
                or not 0 <= point[0] < size[0] or not 0 <= point[1] < size[1]):
            raise ValueError("selected point must be inside the physical host client")
        points.add(tuple(int(value) for value in point))
    if len(points) > MAX_SAMPLE_POINTS:
        raise ValueError(f"cheap checks exceed the {MAX_SAMPLE_POINTS} point-hit limit")
    return sorted(points), boxes


class _Checker:
    def __init__(self, hwnd, expected_size, points, regions, native):
        self.hwnd, self.expected_size, self.points = hwnd, expected_size, points
        self.regions, self.native = regions, native
        self.telemetry = []
        self.check_count = 0
        self.baseline = self._snapshot()
        self.bound_monotonic = time.monotonic()

    def _snapshot(self):
        native = self.native
        with native.physical_pixels():
            api = native.api
            if not api.IsWindow(self.hwnd) or not api.IsWindowVisible(self.hwnd) or api.IsIconic(self.hwnd):
                raise WindowTargetError("frame-lease host window is invalid, hidden, or minimized")
            root = int(api.GetAncestor(self.hwnd, 2) or 0)
            foreground = int(api.GetForegroundWindow() or 0)
            foreground_root = int(api.GetAncestor(foreground, 2) or 0)
            if root != self.hwnd or foreground_root != root:
                raise WindowTargetError("frame-lease host root/focus changed; focus is never adjusted")
            try:
                size = _size(tuple(native.client_size(self.hwnd)))
            except ValueError as exc:
                raise WindowTargetError(f"frame-lease physical client size is invalid: {exc}") from exc
            if size != self.expected_size:
                raise WindowTargetError("frame-lease physical client size changed")
            origin = native.types.POINT(0, 0)
            if not api.ClientToScreen(self.hwnd, native.ctypes.byref(origin)):
                raise WindowTargetError("frame-lease host origin could not be read")
            rect = (origin.x, origin.y, origin.x + size[0], origin.y + size[1])
            screen_left, screen_top = api.GetSystemMetrics(76), api.GetSystemMetrics(77)
            screen = (screen_left, screen_top, screen_left + api.GetSystemMetrics(78),
                      screen_top + api.GetSystemMetrics(79))
            if not (screen[0] <= rect[0] < rect[2] <= screen[2]
                    and screen[1] <= rect[1] < rect[3] <= screen[3]):
                raise WindowTargetError("frame-lease host client is outside the visible desktop")
            for x, y in self.points:
                hit = api.WindowFromPoint(native.types.POINT(rect[0] + x, rect[1] + y))
                if int(api.GetAncestor(hit, 2) or 0) != root:
                    raise WindowTargetError(f"frame-lease sampled host point is obscured: {(x, y)}")
            return {"hwnd": self.hwnd, "root_hwnd": root, "foreground_root_hwnd": foreground_root,
                    "client_size": list(size), "screen_rect": list(rect), "desktop_rect": list(screen),
                    "local_sample_points": [list(point) for point in self.points],
                    "regions": [list(region) for region in self.regions],
                    "partial_point_hits_only": True, "full_occlusion_scan": False,
                    "guest_focus_pixels_rechecked": False}

    def recheck(self):
        self.check_count += 1
        item = {"sequence": self.check_count, "checked_monotonic": time.monotonic(),
                "partial_point_hits_only": True, "guest_focus_pixels_rechecked": False,
                "full_occlusion_scan": False, "valid": False, "error": None}
        try:
            snapshot = self._snapshot()
            if snapshot != self.baseline:
                raise WindowTargetError("frame-lease host geometry/desktop changed after binding")
            item["valid"] = True
            return True
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"[:2048]
            raise
        finally:
            self.telemetry.append(item)
            if len(self.telemetry) > MAX_TELEMETRY:
                self.telemetry.pop(0)

    def __call__(self):
        return self.recheck()


def bind(hwnd, expected_size, point=None, regions=None):
    """Bind a fresh physical baseline; never capture pixels or enumerate windows."""
    if (isinstance(hwnd, bool) or not isinstance(hwnd, Integral)
            or not 0 < int(hwnd) < 2**64):
        raise ValueError("frame-lease HWND must be a positive 64-bit integer")
    size = _size(expected_size)
    points, boxes = _samples(size, point, regions)
    return _Checker(int(hwnd), size, points, boxes, _native_backend())


__all__ = ["bind", "MAX_REGIONS", "MAX_SAMPLE_POINTS"]
