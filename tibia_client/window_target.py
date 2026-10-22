"""Calibrated Sandbox viewport mapping and optional legacy readiness probes.

Local windows retain the existing capture/input backends. Sandbox pixels come
from the visible desktop, in physical pixels, and actions use the same 1:1
viewport offset. Default input only maps coordinates; availability diagnostics
run after native errors. Optional paired title-bar samples support the earlier
guarded experiments, which must explicitly request preflight checking.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
from io import BytesIO
import json
from numbers import Integral
import os

import numpy as np
from PIL import Image, UnidentifiedImageError


MAX_DIMENSION = 16384
MAX_HOST_PIXELS = 33_554_432
MAX_GUARD_PIXELS = 262_144
MAX_ENCODED_PATCH = 100_000
GUARD_TOLERANCE = 3.0


class WindowTargetError(RuntimeError):
    """The calibrated viewport cannot currently be used safely."""


def _integers(value, count, label):
    if (not isinstance(value, (list, tuple)) or len(value) != count
            or any(isinstance(item, bool) or not isinstance(item, Integral)
                   for item in value)):
        raise ValueError(f"{label} must contain {count} integral coordinates")
    return tuple(int(item) for item in value)


def _size(value):
    size = _integers(value, 2, "host_size")
    if (min(size) <= 0 or max(size) > MAX_DIMENSION
            or size[0] * size[1] > MAX_HOST_PIXELS):
        raise ValueError("host_size is outside the supported pixel bounds")
    return size


def _region(value, size, label="viewport"):
    region = _integers(value, 4, label)
    left, top, right, bottom = region
    if not (0 <= left < right <= size[0] and 0 <= top < bottom <= size[1]):
        raise ValueError(f"{label} must be a nonempty rectangle inside host_size")
    return region


def _encode_patch(image):
    buffer = BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    if len(encoded) > MAX_ENCODED_PATCH:
        raise ValueError("focus_guard patch exceeds the encoded size limit")
    return encoded


def _decode_patch(encoded, size):
    if (not isinstance(encoded, str) or not encoded
            or len(encoded) > MAX_ENCODED_PATCH):
        raise ValueError("focus_guard patch must be a bounded base64 PNG")
    try:
        payload = base64.b64decode(encoded, validate=True)
        with Image.open(BytesIO(payload)) as image:
            if image.format != "PNG" or image.size != size or image.n_frames != 1:
                raise ValueError("focus_guard PNG dimensions do not match its region")
            return image.convert("RGB").copy()
    except (binascii.Error, UnidentifiedImageError, OSError,
            Image.DecompressionBombError) as exc:
        raise ValueError("focus_guard patch is not a valid PNG") from exc


def _validate_samples(active, inactive):
    a = np.asarray(active.convert("RGB"), dtype=np.int16)
    b = np.asarray(inactive.convert("RGB"), dtype=np.int16)
    # A solid color cannot identify the chosen guest window. A one-pixel or
    # compression-level difference cannot establish its focus state either.
    for pixels in (a, b):
        variation = np.ptp(pixels.reshape(-1, 3), axis=0)
        if int(variation.max()) < 12:
            raise ValueError("focus_guard samples must contain nonuniform window detail")
    difference = np.abs(a - b)
    if float(difference.mean()) < 8.0 or float(
        np.mean(np.max(difference, axis=2) >= 12)
    ) < 0.1:
        raise ValueError("active and inactive focus_guard samples are not distinguishable")


def make_focus_guard(active_image, inactive_image, region):
    """Build paired patches from full host-client images of the same size.

    Select a stable guest Tibia title-bar region, first with Tibia active and
    then inactive. The region uses full host-client coordinates, not viewport
    coordinates. No capture or desktop operation is performed here.
    """
    if (not isinstance(active_image, Image.Image)
            or not isinstance(inactive_image, Image.Image)
            or active_image.size != inactive_image.size):
        raise ValueError("focus_guard samples must be equally sized PIL images")
    host_size = _size(active_image.size)
    box = _region(region, host_size, "focus_guard.region")
    if (box[2] - box[0]) * (box[3] - box[1]) > MAX_GUARD_PIXELS:
        raise ValueError("focus_guard region exceeds the pixel limit")
    active, inactive = active_image.crop(box), inactive_image.crop(box)
    _validate_samples(active, inactive)
    return {"region": list(box), "active": _encode_patch(active),
            "inactive": _encode_patch(inactive)}


@dataclass(frozen=True, slots=True)
class _FocusGuard:
    region: tuple[int, int, int, int]
    active: str
    inactive: str
    active_rgb: bytes = field(repr=False)
    inactive_rgb: bytes = field(repr=False)

    @classmethod
    def parse(cls, data, host_size):
        if not isinstance(data, Mapping):
            raise ValueError("Sandbox requires paired active/inactive focus_guard samples")
        box = _region(data.get("region"), host_size, "focus_guard.region")
        size = box[2] - box[0], box[3] - box[1]
        if size[0] * size[1] > MAX_GUARD_PIXELS:
            raise ValueError("focus_guard region exceeds the pixel limit")
        active = _decode_patch(data.get("active"), size)
        inactive = _decode_patch(data.get("inactive"), size)
        _validate_samples(active, inactive)
        return cls(box, _encode_patch(active), _encode_patch(inactive),
                   active.tobytes(), inactive.tobytes())

    def to_dict(self):
        return {"region": list(self.region), "active": self.active, "inactive": self.inactive}


@dataclass(frozen=True, slots=True, init=False)
class WindowTarget:
    kind: str
    host_size: tuple[int, int] | None
    viewport: tuple[int, int, int, int] | None
    _guard: _FocusGuard | None = field(repr=False)

    def __init__(self, kind="local", host_size=None, viewport=None, focus_guard=None):
        if kind not in ("local", "sandbox"):
            raise ValueError("window_target.kind must be local or sandbox")
        if kind == "local":
            if any(value is not None for value in (host_size, viewport, focus_guard)):
                raise ValueError("local window_target cannot contain Sandbox calibration")
            size, box, guard = None, None, None
        else:
            size = _size(host_size)
            box = _region(viewport, size)
            guard = None if focus_guard is None else _FocusGuard.parse(focus_guard, size)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "host_size", size)
        object.__setattr__(self, "viewport", box)
        object.__setattr__(self, "_guard", guard)

    @classmethod
    def from_config(cls, config):
        if not isinstance(config, Mapping):
            raise ValueError("configuration must be an object")
        data = config.get("window_target")
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ValueError("window_target must be an object")
        return cls(data.get("kind", "local"), data.get("host_size"),
                   data.get("viewport"), data.get("focus_guard"))

    @classmethod
    def build(cls, host_size, viewport, focus_guard=None):
        return cls("sandbox", host_size, viewport, focus_guard)

    @property
    def focus_guard(self):
        # Return a copy: callers cannot mutate the calibrated target.
        return None if self._guard is None else self._guard.to_dict()

    @property
    def size(self):
        if self.viewport is None:
            return None
        return self.viewport[2] - self.viewport[0], self.viewport[3] - self.viewport[1]

    def to_dict(self):
        result = {"kind": self.kind}
        if self.kind == "sandbox":
            result.update(host_size=list(self.host_size), viewport=list(self.viewport))
            if self._guard is not None:
                result["focus_guard"] = self.focus_guard
        return result

    @property
    def fingerprint(self):
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def target_from_config(config):
    return WindowTarget.from_config(config)


def check_focus_guard(image, target):
    """Raise when the current patch is inactive, changed, or ambiguous."""
    guard = target._guard
    if guard is None or image.size != target.host_size:
        raise WindowTargetError("Sandbox focus guard or calibrated host geometry is unavailable")
    current = np.asarray(image.convert("RGB").crop(guard.region), dtype=np.int16)
    shape = current.shape
    active = np.frombuffer(guard.active_rgb, dtype=np.uint8).reshape(shape).astype(np.int16)
    inactive = np.frombuffer(guard.inactive_rgb, dtype=np.uint8).reshape(shape).astype(np.int16)
    active_difference = np.abs(current - active)
    active_error = float(active_difference.mean())
    inactive_error = float(np.abs(current - inactive).mean())
    if (active_error > GUARD_TOLERANCE
            or int(active_difference.max()) > 12
            or active_error + 4.0 >= inactive_error):
        raise WindowTargetError("guest Tibia focus guard is inactive or changed; refocus/recalibrate")


def _sample_points(rect, step=32):
    left, top, right, bottom = rect
    xs = list(range(left, right, step)) + [right - 1]
    ys = list(range(top, bottom, step)) + [bottom - 1]
    return ((x, y) for x in xs for y in ys)


def _intersects(first, second):
    return (max(first[0], second[0]) < min(first[2], second[2])
            and max(first[1], second[1]) < min(first[3], second[3]))


class _WindowsScreenBackend:
    """Read-only Windows checks; initialized lazily, never at module import."""

    def __init__(self):
        if os.name != "nt":
            raise WindowTargetError("Sandbox screen capture requires Windows")
        import ctypes
        from ctypes import wintypes

        self.ctypes, self.types = ctypes, wintypes
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        declarations = {
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "GetForegroundWindow": ([], wintypes.HWND),
            "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            "GetClientRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "ClientToScreen": ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "WindowFromPoint": ([wintypes.POINT], wintypes.HWND),
            "GetSystemMetrics": ([ctypes.c_int], ctypes.c_int),
            "SetThreadDpiAwarenessContext": ([ctypes.c_void_p], ctypes.c_void_p),
        }
        try:
            for name, (arguments, result) in declarations.items():
                method = getattr(self.api, name)
                method.argtypes, method.restype = arguments, result
        except AttributeError as exc:
            raise WindowTargetError("physical-pixel Windows capture APIs are unavailable") from exc
        self.enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.api.EnumWindows.argtypes = [self.enum_callback, wintypes.LPARAM]
        self.api.EnumWindows.restype = wintypes.BOOL
        self._cloaked = None
        try:
            dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
            query = dwm.DwmGetWindowAttribute
            query.argtypes = [wintypes.HWND, wintypes.DWORD,
                              ctypes.c_void_p, wintypes.DWORD]
            query.restype = wintypes.LONG
            self._cloaked = query
        except (AttributeError, OSError):
            # A missing or failed query does not establish invisibility. Keep
            # the window in the conservative occlusion check instead.
            pass

    @contextmanager
    def physical_pixels(self):
        # Per-thread and restored; do not change global application DPI state.
        previous = self.api.SetThreadDpiAwarenessContext(self.ctypes.c_void_p(-4))
        if not previous:
            raise WindowTargetError("could not establish physical-pixel DPI coordinates")
        try:
            yield
        finally:
            self.api.SetThreadDpiAwarenessContext(previous)

    def client_size(self, hwnd):
        rect = self.types.RECT()
        if not self.api.IsWindow(hwnd) or not self.api.GetClientRect(hwnd, self.ctypes.byref(rect)):
            raise WindowTargetError("configured Sandbox HWND is no longer valid")
        return rect.right - rect.left, rect.bottom - rect.top

    def _is_cloaked(self, hwnd):
        query = getattr(self, "_cloaked", None)
        if query is None:
            return False
        value = self.types.DWORD()
        result = query(hwnd, 14, self.ctypes.byref(value), self.ctypes.sizeof(value))
        return result == 0 and bool(value.value)

    def _describe_obstruction(self, hwnd, rect=None):
        """Describe only a surface that caused rejection; diagnostics never permit capture."""
        hwnd = int(hwnd or 0)
        if rect is None:
            try:
                bounds = self.types.RECT()
                if self.api.GetWindowRect(hwnd, self.ctypes.byref(bounds)):
                    rect = bounds.left, bounds.top, bounds.right, bounds.bottom
            except Exception:
                pass
        details = []
        for label, method_name, capacity in (
                ("class", "GetClassNameW", 129), ("title", "GetWindowTextW", 97)):
            try:
                buffer = self.ctypes.create_unicode_buffer(capacity)
                getattr(self.api, method_name)(hwnd, buffer, capacity)
                value = buffer.value
            except Exception:
                value = "<unavailable>"
            details.append(f"{label}={value!r}")
        return f"hwnd={hwnd}, rect={rect!r}, " + ", ".join(details)

    def snapshot(self, hwnd, expected_size, regions):
        if (not self.api.IsWindow(hwnd) or not self.api.IsWindowVisible(hwnd)
                or self.api.IsIconic(hwnd)):
            raise WindowTargetError("Sandbox window is invalid, hidden, or minimized")
        root = int(self.api.GetAncestor(hwnd, 2) or 0)
        foreground = int(self.api.GetForegroundWindow() or 0)
        foreground_root = int(self.api.GetAncestor(foreground, 2) or 0)
        if not root or foreground_root != root:
            raise WindowTargetError(
                "Sandbox must be foreground; focus is never changed automatically: "
                f"requested_hwnd={int(hwnd)}, requested_root={root}, "
                f"foreground_hwnd={foreground}, foreground_root=("
                + self._describe_obstruction(foreground_root) + ")"
            )
        size = self.client_size(hwnd)
        try:
            _size(size)
        except ValueError as exc:
            raise WindowTargetError(str(exc)) from exc
        if expected_size is not None and size != expected_size:
            raise WindowTargetError("Sandbox host client size changed after calibration")
        origin = self.types.POINT(0, 0)
        if not self.api.ClientToScreen(hwnd, self.ctypes.byref(origin)):
            raise WindowTargetError("could not locate Sandbox client in screen coordinates")
        rect = origin.x, origin.y, origin.x + size[0], origin.y + size[1]
        x, y = self.api.GetSystemMetrics(76), self.api.GetSystemMetrics(77)
        screen = x, y, x + self.api.GetSystemMetrics(78), y + self.api.GetSystemMetrics(79)
        if not (screen[0] <= rect[0] < rect[2] <= screen[2]
                and screen[1] <= rect[1] < rect[3] <= screen[3]):
            raise WindowTargetError("Sandbox client is partly outside the visible desktop")
        local_regions = regions or [(0, 0, *size)]
        boxes = [(rect[0] + r[0], rect[1] + r[1], rect[0] + r[2], rect[1] + r[3])
                 for r in local_regions]
        # Bounding rectangles deliberately reject even transparent foreign
        # windows above the target. Point sampling alone could miss a thin
        # overlay between samples.
        above = []
        seen_root = False
        enumeration_error = []

        @self.enum_callback
        def visit(other, _parameter):
            nonlocal seen_root
            try:
                if int(other) == root:
                    seen_root = True
                    return True
                if (not seen_root and self.api.IsWindowVisible(other)
                        and not self.api.IsIconic(other) and not self._is_cloaked(other)):
                    other_rect = self.types.RECT()
                    if not self.api.GetWindowRect(other, self.ctypes.byref(other_rect)):
                        enumeration_error.append("could not inspect an overlapping window")
                    else:
                        above.append((int(other), (other_rect.left, other_rect.top,
                                                   other_rect.right, other_rect.bottom)))
            except Exception as exc:
                enumeration_error.append(str(exc))
            return True

        if not self.api.EnumWindows(visit, 0) or enumeration_error or not seen_root:
            raise WindowTargetError("could not establish Sandbox window visibility")
        for box in boxes:
            for other, other_rect in above:
                if _intersects(box, other_rect):
                    raise WindowTargetError(
                        "another host window obscures the Sandbox viewport or focus guard: "
                        + self._describe_obstruction(other, other_rect)
                        + f", checked_region={box!r}"
                    )
        for box in boxes:
            for x, y in _sample_points(box):
                visible = self.api.WindowFromPoint(self.types.POINT(x, y))
                visible_root = int(self.api.GetAncestor(visible, 2) or 0)
                if visible_root != root:
                    raise WindowTargetError(
                        "Sandbox viewport is obscured or not on a visible monitor: "
                        f"point={(x, y)!r}, hit_hwnd={int(visible or 0)}, hit_root=("
                        + self._describe_obstruction(visible_root) + ")"
                    )
        return root, rect

    def grab(self, rect):
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=rect, all_screens=True).convert("RGB")


def _native_backend():
    return _WindowsScreenBackend()


def _capture_checked(hwnd, expected_size=None, regions=None):
    if isinstance(hwnd, bool) or not isinstance(hwnd, Integral) or int(hwnd) <= 0:
        raise WindowTargetError("configured Sandbox HWND is invalid")
    expected = None if expected_size is None else _size(expected_size)
    try:
        native = _native_backend()
        with native.physical_pixels():
            before = native.snapshot(int(hwnd), expected, regions)
            image = native.grab(before[1])
            after = native.snapshot(int(hwnd), expected, regions)
            if before != after:
                raise WindowTargetError("Sandbox window geometry changed during capture")
            actual_size = before[1][2] - before[1][0], before[1][3] - before[1][1]
            if not isinstance(image, Image.Image) or image.size != actual_size:
                raise WindowTargetError("Sandbox screen capture returned inconsistent physical pixels")
            return image.convert("RGB")
    except WindowTargetError:
        raise
    except Exception as exc:
        raise WindowTargetError(f"Sandbox screen capture unavailable: {exc}") from exc


def capture_host_client(hwnd, expected_size=None):
    """Capture a visible foreground host client, for user-guided calibration."""
    return _capture_checked(hwnd, expected_size)


def capture_target(hwnd, target, require_guard=True):
    if not isinstance(target, WindowTarget):
        raise TypeError("target must be a WindowTarget")
    if target.kind == "local":
        from core.perception import _capture_windows
        return _capture_windows(hwnd)
    regions = [target.viewport, target._guard.region]
    image = _capture_checked(hwnd, target.host_size, regions)
    if require_guard:
        check_focus_guard(image, target)
    return image.crop(target.viewport)


class TargetInput:
    """Map calibrated game coordinates onto a physical MoveMouse backend.

    Normal dispatch does not capture, check focus, or poll availability.
    ``preflight=True`` retains the earlier explicit guarded experiments.
    Key/button release remains unconditional in the physical backend.
    """

    def __init__(self, backend, target, hwnd_resolver, *, preflight=False):
        if not isinstance(target, WindowTarget):
            raise TypeError("target must be a WindowTarget")
        if not callable(hwnd_resolver):
            raise TypeError("hwnd_resolver must be callable")
        if type(preflight) is not bool:
            raise TypeError("preflight must be a boolean")
        self.preflight = preflight
        self.backend, self.target = backend, target
        self._hwnd_resolver = hwnd_resolver

    def MainWindowSize(self):
        if self.target.kind == "local":
            return self.backend.MainWindowSize()
        if not self.preflight:
            return self.target.size
        native = _native_backend()
        with native.physical_pixels():
            size = tuple(native.client_size(int(self._hwnd_resolver() or 0)))
        if size != self.target.host_size:
            raise WindowTargetError("Sandbox host client size changed after calibration")
        return self.target.size

    def check_ready(self):
        if self.target.kind == "sandbox":
            if self.target._guard is None:
                raise WindowTargetError("legacy readiness probe requires focus samples")
            capture_target(self._hwnd_resolver(), self.target)
        elif not self.backend.IsFocused():
            raise WindowTargetError("configured local game window is not foreground")
        return True

    def IsFocused(self):
        try:
            return self.check_ready()
        except (WindowTargetError, OSError, ValueError):
            return False

    def IsReady(self):
        return self.check_ready()

    def _point(self, point):
        point = _integers(point, 2, "game position")
        if self.target.kind == "local":
            return point
        width, height = self.target.size
        if not (0 <= point[0] < width and 0 <= point[1] < height):
            raise ValueError("game position is outside the calibrated viewport")
        return point[0] + self.target.viewport[0], point[1] + self.target.viewport[1]

    def _dispatch(self, method, *arguments):
        if self.target.kind == "local":
            if self.preflight:
                self.check_ready()
            return getattr(self.backend, method)(*arguments)
        # MoveMouse's ClientToScreen and GetClientRect must use the very same
        # physical-pixel DPI context as capture, including multi-step actions.
        native = _native_backend()
        with native.physical_pixels():
            if self.preflight:
                self.check_ready()
            return getattr(self.backend, method)(*arguments)

    @property
    def last_error_diagnostic(self):
        return getattr(self.backend, "last_error_diagnostic", None)

    def diagnose_availability(self, error):
        return self.backend.diagnose_availability(error)

    def Activate(self):
        return self.backend.Activate()

    def Position(self):
        point = self._dispatch("Position")
        if self.target.kind == "local":
            return point
        return point[0] - self.target.viewport[0], point[1] - self.target.viewport[1]

    def ScreenPosition(self):
        return self._dispatch("ScreenPosition")

    def RestoreScreenPosition(self, position):
        position = _integers(position, 2, "screen position")
        return self._dispatch("RestoreScreenPosition", position)

    def Press(self, key):
        return self._dispatch("Press", key)

    def PressHotkey(self, option, key):
        return self._dispatch("PressHotkey", option, key)

    def LeftClick(self, position):
        point = self._point(position)
        return self._dispatch("LeftClick", point)

    def RightClick(self, position):
        point = self._point(position)
        return self._dispatch("RightClick", point)

    def MoveTo(self, x, y):
        point = self._point((x, y))
        return self._dispatch("MoveTo", *point)

    def DragTo(self, source, destination):
        source, destination = self._point(source), self._point(destination)
        return self._dispatch("DragTo", source, destination)

    def UseOn(self, source, destination):
        source, destination = self._point(source), self._point(destination)
        return self._dispatch("UseOn", source, destination)


__all__ = ["WindowTarget", "WindowTargetError", "TargetInput", "target_from_config",
           "capture_host_client", "capture_target", "make_focus_guard", "check_focus_guard"]
