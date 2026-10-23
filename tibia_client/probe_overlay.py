"""Probe-only visible capture with a narrowly qualified annotation exception.

Production capture/visibility checks are unchanged. Only the named Computer Use
cursor annotation can be omitted from the bounding-rectangle obstruction check,
and only when both transparent-input and no-activation extended styles exist.
The inherited point-hit, foreground, bounds, size, and before/after checks remain.
Importing this module performs no native operation.
"""

from __future__ import annotations

from numbers import Integral

from PIL import Image

from tibia_client.window_target import _WindowsScreenBackend, _size, WindowTargetError


ANNOTATION_CLASS = "CodexComputerUseCursorOverlay"
ANNOTATION_TITLE = "Codex Computer Use Cursor Overlay"
ANNOTATION_TITLES = (ANNOTATION_TITLE, "ChatGPT is using your computer. Esc to cancel")
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000
_REQUIRED = WS_EX_TRANSPARENT | WS_EX_NOACTIVATE


def _qualification(hwnd, class_name, title, extended_style):
    """Pure, JSON-safe qualification; flags never qualify a different window."""
    style = int(extended_style) & 0xFFFFFFFF
    exact_name = class_name == ANNOTATION_CLASS and title in ANNOTATION_TITLES
    return {
        "hwnd": int(hwnd), "class_name": str(class_name), "title": str(title),
        "extended_style": style, "extended_style_hex": f"0x{style:08x}",
        "ws_ex_transparent": bool(style & WS_EX_TRANSPARENT),
        "ws_ex_noactivate": bool(style & WS_EX_NOACTIVATE),
        "ws_ex_layered": bool(style & WS_EX_LAYERED),
        "exact_annotation_name": exact_name,
        "qualified": exact_name and style & _REQUIRED == _REQUIRED,
    }


class _OverlayScreenBackend(_WindowsScreenBackend):
    """Retain base snapshot checks; extend only rectangle occluder qualification."""

    def __init__(self):
        super().__init__()
        self._qualifications = {}
        self.api.GetClassNameW.argtypes = [self.types.HWND, self.types.LPWSTR, self.ctypes.c_int]
        self.api.GetClassNameW.restype = self.ctypes.c_int
        self.api.GetWindowTextW.argtypes = [self.types.HWND, self.types.LPWSTR, self.ctypes.c_int]
        self.api.GetWindowTextW.restype = self.ctypes.c_int
        self._get_style = getattr(self.api, "GetWindowLongPtrW", None)
        if self._get_style is None:
            self._get_style = self.api.GetWindowLongW
        self._get_style.argtypes = [self.types.HWND, self.ctypes.c_int]
        self._get_style.restype = self.ctypes.c_ssize_t

    def qualify(self, hwnd):
        class_name = self.ctypes.create_unicode_buffer(256)
        title = self.ctypes.create_unicode_buffer(256)
        if not self.api.GetClassNameW(hwnd, class_name, len(class_name)):
            raise WindowTargetError("annotation class could not be inspected")
        self.api.GetWindowTextW(hwnd, title, len(title))
        self.ctypes.set_last_error(0)
        style = self._get_style(hwnd, -20)
        error = self.ctypes.get_last_error()
        if not style and error:
            raise WindowTargetError(f"annotation extended style query failed: winerror={error}")
        return _qualification(hwnd, class_name.value, title.value, style)

    @property
    def qualifications(self):
        return [dict(item) for item in self._qualifications.values()]

    def _is_cloaked(self, hwnd):
        if super()._is_cloaked(hwnd):
            return True
        try:
            info = self.qualify(hwnd)
        except Exception:
            # Failed inspection cannot authorize an exception. The inherited
            # rectangle and point checks still consider this surface.
            return False
        if info["class_name"] == ANNOTATION_CLASS or info["title"] in ANNOTATION_TITLES:
            self._qualifications[int(hwnd)] = info
        return info["qualified"]

    def snapshot(self, hwnd, expected_size, regions):
        try:
            return super().snapshot(hwnd, expected_size, regions)
        except WindowTargetError as exc:
            rejected = [item for item in self.qualifications
                        if item["class_name"] == ANNOTATION_CLASS and not item["qualified"]]
            if rejected:
                details = "; ".join(
                    f"annotation hwnd={item['hwnd']} exstyle={item['extended_style_hex']} "
                    f"title={item['title']!r} allowed_title={item['exact_annotation_name']} "
                    f"WS_EX_TRANSPARENT={item['ws_ex_transparent']} "
                    f"WS_EX_NOACTIVATE={item['ws_ex_noactivate']}"
                    for item in rejected
                )
                raise WindowTargetError(f"{exc}; annotation exception rejected: {details}") from exc
            raise


def _native_backend():
    return _OverlayScreenBackend()


def inspect_overlay(hwnd):
    """Explicit read-only qualification metadata for one selected host HWND."""
    if isinstance(hwnd, bool) or not isinstance(hwnd, Integral) or hwnd <= 0:
        raise WindowTargetError("overlay HWND must be a positive integer")
    native = _native_backend()
    with native.physical_pixels():
        return native.qualify(int(hwnd))


def _capture_client_with_backend(hwnd, expected_size, native_factory):
    """Share every visibility/geometry check across experimental grab methods."""
    if isinstance(hwnd, bool) or not isinstance(hwnd, Integral) or hwnd <= 0:
        raise WindowTargetError("configured host HWND is invalid")
    expected = None if expected_size is None else _size(expected_size)
    try:
        native = native_factory()
        with native.physical_pixels():
            before = native.snapshot(int(hwnd), expected, None)
            image = native.grab(before[1])
            after = native.snapshot(int(hwnd), expected, None)
            if before != after:
                raise WindowTargetError("host window geometry changed during experimental capture")
            actual_size = before[1][2] - before[1][0], before[1][3] - before[1][1]
            if not isinstance(image, Image.Image) or image.size != actual_size:
                raise WindowTargetError("experimental capture returned inconsistent physical pixels")
            image = image.convert("RGB")
            image.info["probe_overlay_qualification"] = native.qualifications
            return image
    except WindowTargetError:
        raise
    except Exception as exc:
        raise WindowTargetError(f"experimental overlay-aware capture unavailable: {exc}") from exc


def capture_client_overlay_aware(hwnd, expected_size=None):
    """Experimental RGB capture; qualified annotation metadata is in image.info.

    This does not hide, move, click, activate, or modify any window. A qualification
    exception affects only inherited rectangle enumeration, never point-hit checks.
    """
    return _capture_client_with_backend(hwnd, expected_size, _native_backend)


__all__ = ["capture_client_overlay_aware", "inspect_overlay"]
