"""Physical input using client-local coordinates and post-error diagnostics.

The Windows API is loaded only when an action is performed, so importing this
module is safe in headless and non-Windows test environments.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from numbers import Integral
from typing import Callable, Optional, Tuple

from conf.HexMapKeys import KEY_CODES_CASEFOLDED
from conf.conf_manager import ConfManager
from core.input_diagnostics import InputDiagnostics
from core.runtime import checkpoint, wait


class InputUnavailableError(RuntimeError):
    """A native input operation or explicit availability probe failed."""


def _configured_input_hwnd() -> int:
    data = ConfManager.get("conf.json")
    return int(data.get("input_hwnd", data.get("hwnd", 0)) or 0)


def _configured_capture_size():
    config = ConfManager.get("conf.json")
    from tibia_client.window_target import WindowTarget

    target = WindowTarget.from_config(config)
    return target.host_size if target.kind == "sandbox" else config.get("capture_size")


class _WindowsPhysicalBackend:
    KEYEVENTF_KEYDOWN = 0x0000
    KEYEVENTF_KEYUP = 0x0002
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010

    def __init__(self) -> None:
        if os.name != "nt":
            raise InputUnavailableError("physical Windows input is unavailable")
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.user32 = ctypes.windll.user32
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.GetForegroundWindow.argtypes = []
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        self.user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        self.user32.ClientToScreen.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.POINT)
        ]
        self.user32.ClientToScreen.restype = wintypes.BOOL
        self.user32.ScreenToClient.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.POINT)
        ]
        self.user32.ScreenToClient.restype = wintypes.BOOL
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user32.GetCursorPos.restype = wintypes.BOOL
        self.user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.user32.SetCursorPos.restype = wintypes.BOOL
        self.user32.keybd_event.argtypes = [
            wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t
        ]
        self.user32.keybd_event.restype = None
        self.user32.mouse_event.argtypes = [
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, ctypes.c_size_t
        ]
        self.user32.mouse_event.restype = None
        self.user32.GetClientRect.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.RECT)
        ]
        self.user32.GetClientRect.restype = wintypes.BOOL

    def is_window(self, hwnd: int) -> bool:
        return bool(self.user32.IsWindow(hwnd))

    def foreground_window(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def activate_window(self, hwnd: int) -> None:
        if not self.user32.SetForegroundWindow(hwnd):
            raise InputUnavailableError("Windows did not activate the selected input client")

    @contextmanager
    def physical_pixels(self):
        previous = self.user32.SetThreadDpiAwarenessContext(self.ctypes.c_void_p(-4))
        if not previous:
            raise InputUnavailableError("could not establish physical-pixel input coordinates")
        try:
            yield
        finally:
            self.user32.SetThreadDpiAwarenessContext(previous)

    def client_to_screen(self, hwnd: int, position: Tuple[int, int]):
        point = self.wintypes.POINT(int(position[0]), int(position[1]))
        with self.physical_pixels():
            if not self.user32.ClientToScreen(hwnd, self.ctypes.byref(point)):
                raise InputUnavailableError("could not translate client coordinates")
        return point.x, point.y

    def screen_to_client(self, hwnd: int, position: Tuple[int, int]):
        point = self.wintypes.POINT(int(position[0]), int(position[1]))
        with self.physical_pixels():
            if not self.user32.ScreenToClient(hwnd, self.ctypes.byref(point)):
                raise InputUnavailableError("could not translate cursor coordinates")
        return point.x, point.y

    def cursor_position(self):
        point = self.wintypes.POINT()
        with self.physical_pixels():
            if not self.user32.GetCursorPos(self.ctypes.byref(point)):
                raise InputUnavailableError("could not read cursor position")
        return point.x, point.y

    def set_cursor_position(self, position: Tuple[int, int]) -> None:
        with self.physical_pixels():
            if not self.user32.SetCursorPos(int(position[0]), int(position[1])):
                raise InputUnavailableError("could not move cursor")

    def key_event(self, virtual_key: int, key_up: bool) -> None:
        flags = self.KEYEVENTF_KEYUP if key_up else self.KEYEVENTF_KEYDOWN
        self.user32.keybd_event(virtual_key, 0, flags, 0)

    def mouse_event(self, flags: int) -> None:
        self.user32.mouse_event(flags, 0, 0, 0, 0)

    def client_size(self, hwnd: int):
        rect = self.wintypes.RECT()
        with self.physical_pixels():
            if not self.user32.GetClientRect(hwnd, self.ctypes.byref(rect)):
                raise InputUnavailableError("could not read client size")
        return rect.right - rect.left, rect.bottom - rect.top

    @staticmethod
    def availability_diagnostic(hwnd):
        from tibia_client.direct_capture import diagnose_client_availability

        return diagnose_client_availability(hwnd)


class MoveMouse(InputDiagnostics):
    KEYEVENTF_KEYDOWN = _WindowsPhysicalBackend.KEYEVENTF_KEYDOWN
    KEYEVENTF_KEYUP = _WindowsPhysicalBackend.KEYEVENTF_KEYUP
    MOUSEEVENTF_LEFTDOWN = _WindowsPhysicalBackend.MOUSEEVENTF_LEFTDOWN
    MOUSEEVENTF_LEFTUP = _WindowsPhysicalBackend.MOUSEEVENTF_LEFTUP
    MOUSEEVENTF_RIGHTDOWN = _WindowsPhysicalBackend.MOUSEEVENTF_RIGHTDOWN
    MOUSEEVENTF_RIGHTUP = _WindowsPhysicalBackend.MOUSEEVENTF_RIGHTUP
    MOUSEEVENTF_LEFTCLICK = MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_LEFTUP
    MOUSEEVENTF_RIGHTCLICK = MOUSEEVENTF_RIGHTDOWN | MOUSEEVENTF_RIGHTUP

    def __init__(self, backend=None, hwnd_resolver: Optional[Callable[[], int]] = None,
                 expected_size_resolver: Optional[Callable[[], object]] = None,
                 readiness_guard: Optional[Callable[[], object]] = None,
                 *, preflight=False, error_diagnostic=None, key_hold_ms=50):
        if type(preflight) is not bool:
            raise TypeError("preflight must be a boolean")
        self.preflight = preflight
        if type(key_hold_ms) is not int or not 0 <= key_hold_ms <= 250:
            raise ValueError("key_hold_ms must be an integer from 0 to 250")
        self.key_hold_ms = key_hold_ms
        self._init_diagnostics(error_diagnostic)
        native_resolution = backend is None and hwnd_resolver is None
        self._backend = backend
        self.readiness_guard = readiness_guard
        self._hwnd_resolver = hwnd_resolver or _configured_input_hwnd
        self._expected_size_resolver = (
            expected_size_resolver
            if expected_size_resolver is not None
            else (_configured_capture_size if native_resolution else None)
        )

    @property
    def backend(self):
        if self._backend is None:
            self._backend = _WindowsPhysicalBackend()
        return self._backend

    def _active_window(self) -> int:
        if not self.preflight:
            return self._configured_window()
        hwnd = self._valid_window()
        if self.backend.foreground_window() != hwnd:
            raise InputUnavailableError(
                "physical input requires the configured window to be foreground"
            )
        if self.readiness_guard is not None:
            self.readiness_guard()
            checkpoint()
        return hwnd

    def Activate(self):
        """Hand focus to the bound host window once on an explicit start/resume."""
        with self._operation("activate input client"):
            hwnd = self._configured_window()
            self._call("activate_window", hwnd)
        return True

    def _configured_window(self):
        hwnd = self._hwnd_resolver()
        if isinstance(hwnd, bool) or not isinstance(hwnd, Integral) or hwnd <= 0:
            raise ValueError("configured input_hwnd must be a positive integer")
        self._operation_hwnd = int(hwnd)
        return int(hwnd)

    def _valid_window(self) -> int:
        hwnd = self._configured_window()
        if not hwnd or not self.backend.is_window(hwnd):
            raise InputUnavailableError("configured input_hwnd is not a valid window")
        self._validate_expected_size(hwnd)
        return hwnd

    def _validate_expected_size(self, hwnd: int) -> None:
        if self._expected_size_resolver is None:
            return
        expected = self._expected_size_resolver()
        if expected is None:
            return
        if (not isinstance(expected, (tuple, list)) or len(expected) != 2
                or any(isinstance(value, bool) or not isinstance(value, Integral)
                       or int(value) <= 0 for value in expected)):
            raise InputUnavailableError("configured capture_size is invalid")
        expected_size = int(expected[0]), int(expected[1])
        current_size = tuple(int(value) for value in self.backend.client_size(hwnd))
        if current_size != expected_size:
            raise InputUnavailableError(
                "input client size changed after calibration: "
                f"expected {expected_size!r}, got {current_size!r}"
            )

    def _ensure_active(self, hwnd: int) -> None:
        if not self.preflight:
            return
        if not hwnd or not self.backend.is_window(hwnd):
            raise InputUnavailableError("configured input_hwnd is not a valid window")
        if self.backend.foreground_window() != hwnd:
            raise InputUnavailableError(
                "physical input requires the configured window to be foreground"
            )
        if self.readiness_guard is not None:
            self.readiness_guard()
            checkpoint()

    def _validate_position(self, hwnd: int, position):
        if (not isinstance(position, (tuple, list)) or len(position) != 2
                or any(isinstance(value, bool) or not isinstance(value, Integral)
                       for value in position)):
            raise ValueError("client position must contain two integral coordinates")
        x, y = int(position[0]), int(position[1])
        if self.preflight:
            size = self.backend.client_size(hwnd)
        else:
            size = self._expected_size_resolver() if self._expected_size_resolver else None
        if size is None:
            if x < 0 or y < 0:
                raise ValueError("client position must be non-negative")
            return x, y
        if (not isinstance(size, (tuple, list)) or len(size) != 2
                or any(isinstance(value, bool) or not isinstance(value, Integral)
                       or value <= 0 for value in size)):
            raise ValueError("configured capture_size is invalid")
        width, height = size
        if not (0 <= x < int(width) and 0 <= y < int(height)):
            raise ValueError(
                f"client position {(x, y)!r} is outside {(width, height)!r}"
            )
        return x, y

    @staticmethod
    def _key_code(key: str) -> int:
        try:
            return KEY_CODES_CASEFOLDED[key.casefold()]
        except (KeyError, TypeError, AttributeError):
            raise ValueError(f"unsupported key: {key!r}") from None

    def MainWindowSize(self):
        return self.backend.client_size(self._valid_window())

    def IsFocused(self):
        hwnd = self._valid_window()
        return self.backend.foreground_window() == hwnd

    def Position(self):
        with self._operation("physical cursor position"):
            hwnd = self._active_window()
            return self._call("screen_to_client", hwnd, self._call("cursor_position"))

    def ScreenPosition(self):
        """Snapshot the cursor in screen coordinates for later restoration."""

        with self._operation("physical screen cursor position"):
            self._active_window()
            position = self._call("cursor_position")
            if (not isinstance(position, (tuple, list)) or len(position) != 2
                    or any(isinstance(value, bool) or not isinstance(value, Integral)
                           for value in position)):
                raise InputUnavailableError("could not read a valid cursor position")
            return int(position[0]), int(position[1])

    def RestoreScreenPosition(self, position):
        """Restore a screen snapshot while the configured client stays focused."""

        if (not isinstance(position, (tuple, list)) or len(position) != 2
                or any(isinstance(value, bool) or not isinstance(value, Integral)
                       for value in position)):
            raise ValueError("screen position must contain two integral coordinates")
        with self._operation("physical screen cursor restore"):
            hwnd = self._active_window()
            self._ensure_active(hwnd)
            checkpoint()
            self._call("set_cursor_position", (int(position[0]), int(position[1])))

    def Press(self, Key):
        code = self._key_code(Key)
        with self._operation(f"physical key {Key}"):
            self._active_window()
            down = True
            try:
                self._call("key_event", code, False)
                checkpoint()
                if self.key_hold_ms:
                    wait(self.key_hold_ms / 1000)
            finally:
                if down:
                    self._release("key_event", code, True)

    def PressHotkey(self, Option, Key):
        if Option not in {"ctrl", "shift", "alt"}:
            raise ValueError(f"unsupported modifier: {Option!r}")
        modifier = self._key_code(Option)
        key = self._key_code(Key)
        with self._operation(f"physical hotkey {Option}+{Key}"):
            hwnd = self._active_window()
            modifier_down = False
            key_down = False
            try:
                modifier_down = True
                self._call("key_event", modifier, False)
                checkpoint()
                self._ensure_active(hwnd)
                key_down = True
                self._call("key_event", key, False)
                checkpoint()
                if self.key_hold_ms:
                    wait(self.key_hold_ms / 1000)
            finally:
                try:
                    if key_down:
                        self._release("key_event", key, True)
                finally:
                    if modifier_down:
                        self._release("key_event", modifier, True)

    def _move_local(self, hwnd: int, position) -> None:
        self._ensure_active(hwnd)
        screen_position = self._call("client_to_screen",
            hwnd, (int(position[0]), int(position[1]))
        )
        self._ensure_active(hwnd)
        self._call("set_cursor_position", screen_position)

    def _click(self, position, down_flag: int, up_flag: int, reason: str) -> None:
        with self._operation(reason):
            hwnd = self._active_window()
            position = self._validate_position(hwnd, position)
            self._move_local(hwnd, position)
            down = False
            try:
                self._ensure_active(hwnd)
                down = True
                self._call("mouse_event", down_flag)
                checkpoint()
            finally:
                if down:
                    self._release("mouse_event", up_flag)

    def LeftClick(self, Position):
        self._click(Position, self.MOUSEEVENTF_LEFTDOWN, self.MOUSEEVENTF_LEFTUP,
                    "physical left click")

    def RightClick(self, Position):
        self._click(Position, self.MOUSEEVENTF_RIGHTDOWN, self.MOUSEEVENTF_RIGHTUP,
                    "physical right click")

    def MoveTo(self, X, Y):
        with self._operation("physical cursor move"):
            hwnd = self._active_window()
            position = self._validate_position(hwnd, (X, Y))
            self._move_local(hwnd, position)

    def DragTo(self, From, To):
        with self._operation("physical drag"):
            hwnd = self._active_window()
            start = self._validate_position(hwnd, From)
            destination = self._validate_position(hwnd, To)
            self._move_local(hwnd, start)
            down = False
            try:
                self._ensure_active(hwnd)
                down = True
                self._call("mouse_event", self.MOUSEEVENTF_LEFTDOWN)
                checkpoint()
                self._move_local(hwnd, destination)
                checkpoint()
            finally:
                if down:
                    self._release("mouse_event", self.MOUSEEVENTF_LEFTUP)

    def UseOn(self, From, To):
        with self._operation("physical use-on"):
            hwnd = self._active_window()
            source = self._validate_position(hwnd, From)
            destination = self._validate_position(hwnd, To)
            self._move_local(hwnd, source)
            right_down = False
            try:
                self._ensure_active(hwnd)
                right_down = True
                self._call("mouse_event", self.MOUSEEVENTF_RIGHTDOWN)
                checkpoint()
            finally:
                if right_down:
                    self._release("mouse_event", self.MOUSEEVENTF_RIGHTUP)
            checkpoint()
            self._move_local(hwnd, destination)
            left_down = False
            try:
                self._ensure_active(hwnd)
                left_down = True
                self._call("mouse_event", self.MOUSEEVENTF_LEFTDOWN)
                checkpoint()
            finally:
                if left_down:
                    self._release("mouse_event", self.MOUSEEVENTF_LEFTUP)


__all__ = ["InputUnavailableError", "MoveMouse"]
