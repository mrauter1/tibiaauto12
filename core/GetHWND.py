"""Native window handle helpers with no import-time platform side effects."""
from __future__ import annotations

import os


class WindowLookupError(RuntimeError):
    pass


def _require_windows() -> None:
    if os.name != "nt":
        raise OSError("TibiaAuto window capture is currently supported only on Windows")


def GetHWND(title: str) -> int:
    """Return the real native handle of the first exact-title window, or zero."""
    if not title:
        return 0
    _require_windows()
    from conf.WindowTitles import get_titles
    for hwnd, candidate in get_titles().items():
        if candidate == title:
            return hwnd
    return 0


def get_client_size(hwnd: int) -> tuple[int, int]:
    _require_windows()
    if not isinstance(hwnd, int) or hwnd <= 0:
        raise WindowLookupError(f"invalid window handle: {hwnd!r}")
    import ctypes
    from ctypes import wintypes

    rect = wintypes.RECT()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindow.argtypes = (wintypes.HWND,)
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetClientRect.restype = wintypes.BOOL
    if not user32.IsWindow(hwnd):
        raise WindowLookupError(f"window handle is no longer valid: {hwnd}")
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    return rect.right - rect.left, rect.bottom - rect.top


def validate_window_pair(capture_hwnd: int, input_hwnd: int) -> tuple[int, int]:
    """Require capture and input clients to use the same coordinate extent."""
    capture_size = get_client_size(capture_hwnd)
    input_size = get_client_size(input_hwnd)
    if capture_size != input_size:
        raise WindowLookupError(
            "capture and game windows have different client sizes "
            f"({capture_size[0]}x{capture_size[1]} vs {input_size[0]}x{input_size[1]}). "
            "Resize them to match before continuing."
        )
    if capture_size[0] <= 0 or capture_size[1] <= 0:
        raise WindowLookupError("selected window has an empty client area")
    return capture_size
