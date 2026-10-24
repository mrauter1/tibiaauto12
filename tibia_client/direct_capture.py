"""Direct visible-client capture with availability diagnostics only on error.

Successful captures read the selected client's geometry and copy its screen
rectangle. There is no focus, occlusion, title-patch, or screenshot-age preflight.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from numbers import Integral
import os

from PIL import Image

from tibia_client.region_capture import _GDIReadback, _rectangle
from tibia_client.window_target import WindowTargetError


class _DirectClientBackend:
    def __init__(self):
        if os.name != "nt":
            raise WindowTargetError("direct client capture requires Windows")
        from ctypes import wintypes

        self.types = wintypes
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        for name, arguments, result in (
            ("GetClientRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            ("ClientToScreen", [wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            ("SetThreadDpiAwarenessContext", [ctypes.c_void_p], ctypes.c_void_p),
        ):
            method = getattr(self.api, name)
            method.argtypes, method.restype = arguments, result

    @contextmanager
    def physical_pixels(self):
        previous = self.api.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous:
            raise WindowTargetError("could not establish physical-pixel DPI coordinates")
        try:
            yield
        finally:
            self.api.SetThreadDpiAwarenessContext(previous)

    def client_rectangle(self, hwnd):
        bounds = self.types.RECT()
        if not self.api.GetClientRect(hwnd, ctypes.byref(bounds)):
            raise WindowTargetError("GetClientRect failed for the selected capture window")
        origin = self.types.POINT(bounds.left, bounds.top)
        if not self.api.ClientToScreen(hwnd, ctypes.byref(origin)):
            raise WindowTargetError("ClientToScreen failed for the selected capture window")
        rect = (origin.x, origin.y, origin.x + bounds.right - bounds.left,
                origin.y + bounds.bottom - bounds.top)
        _rectangle(rect)  # Validate allocation dimensions, not window availability.
        return rect

    def grab(self, rect):
        return _GDIReadback().capture(rect)


def diagnose_client_availability(hwnd):
    """Best-effort diagnostic, called after a failed operation; never retries it."""
    result = {"hwnd": int(hwnd)}
    try:
        if os.name != "nt":
            raise OSError("client availability diagnostics require Windows")
        from ctypes import wintypes

        api = ctypes.WinDLL("user32", use_last_error=True)
        for label, name in (("is_window", "IsWindow"),
                            ("visible", "IsWindowVisible"), ("minimized", "IsIconic")):
            method = getattr(api, name)
            method.argtypes, method.restype = [wintypes.HWND], wintypes.BOOL
            result[label] = bool(method(hwnd))
    except Exception as exc:
        result["diagnostic_error"] = f"{type(exc).__name__}: {exc}"
    return result


def _diagnose_failure(error, hwnd):
    if getattr(error, "client_availability", None) is not None:
        return
    try:
        error.client_availability = diagnose_client_availability(hwnd)
    except Exception:
        pass


def _validate_handle(hwnd):
    if isinstance(hwnd, bool) or not isinstance(hwnd, Integral) or int(hwnd) <= 0:
        raise ValueError("capture window handle must be a positive integer")
    return int(hwnd)


def capture_direct_host_client(hwnd, expected_size=None):
    """Capture physical client pixels once, without an availability preflight."""
    hwnd = _validate_handle(hwnd)
    try:
        native = _DirectClientBackend()
        try:
            with native.physical_pixels():
                rect = native.client_rectangle(hwnd)
                image = native.grab(rect)
        except (WindowTargetError, OSError) as exc:
            # Only the native operation is retryable. Size/configuration and
            # malformed returned-image checks below remain terminal.
            exc.capture_retryable = True
            raise
        actual_size = (rect[2] - rect[0], rect[3] - rect[1])
        if not isinstance(image, Image.Image) or image.size != actual_size:
            raise WindowTargetError("capture returned inconsistent client pixel dimensions")
        if expected_size is not None and image.size != tuple(expected_size):
            raise WindowTargetError("captured client size changed after calibration")
        return image.convert("RGB") if image.mode != "RGB" else image
    except Exception as exc:
        _diagnose_failure(exc, hwnd)
        raise


def capture_direct_target(hwnd, target):
    """Capture a calibrated viewport; stored focus samples are not consulted."""
    if target.kind == "local":
        return capture_direct_host_client(hwnd)
    image = capture_direct_host_client(hwnd, target.host_size)
    try:
        left, top, right, bottom = target.viewport
        if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
            raise WindowTargetError("calibrated viewport exceeds captured client pixels")
        return image.crop(target.viewport)
    except Exception as exc:
        _diagnose_failure(exc, hwnd)
        raise


__all__ = ["capture_direct_host_client", "capture_direct_target", "diagnose_client_availability"]
