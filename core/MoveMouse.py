"""Foreground physical input using client-local coordinates.

The Windows API is loaded only when an action is performed, so importing this
module is safe in headless and non-Windows test environments.
"""

from __future__ import annotations

import os
from numbers import Integral
from typing import Callable, Optional, Tuple

from conf.HexMapKeys import KeyToHex
from conf.conf_manager import ConfManager
from core.runtime import checkpoint, input_transaction


class InputUnavailableError(RuntimeError):
    """The configured input window cannot safely receive physical input."""


def _configured_input_hwnd() -> int:
    data = ConfManager.get("conf.json")
    return int(data.get("input_hwnd", data.get("hwnd", 0)) or 0)


def _configured_capture_size():
    return ConfManager.get("conf.json").get("capture_size")


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

    def client_to_screen(self, hwnd: int, position: Tuple[int, int]):
        point = self.wintypes.POINT(int(position[0]), int(position[1]))
        if not self.user32.ClientToScreen(hwnd, self.ctypes.byref(point)):
            raise InputUnavailableError("could not translate client coordinates")
        return point.x, point.y

    def screen_to_client(self, hwnd: int, position: Tuple[int, int]):
        point = self.wintypes.POINT(int(position[0]), int(position[1]))
        if not self.user32.ScreenToClient(hwnd, self.ctypes.byref(point)):
            raise InputUnavailableError("could not translate cursor coordinates")
        return point.x, point.y

    def cursor_position(self):
        point = self.wintypes.POINT()
        if not self.user32.GetCursorPos(self.ctypes.byref(point)):
            raise InputUnavailableError("could not read cursor position")
        return point.x, point.y

    def set_cursor_position(self, position: Tuple[int, int]) -> None:
        if not self.user32.SetCursorPos(int(position[0]), int(position[1])):
            raise InputUnavailableError("could not move cursor")

    def key_event(self, virtual_key: int, key_up: bool) -> None:
        flags = self.KEYEVENTF_KEYUP if key_up else self.KEYEVENTF_KEYDOWN
        self.user32.keybd_event(virtual_key, 0, flags, 0)

    def mouse_event(self, flags: int) -> None:
        self.user32.mouse_event(flags, 0, 0, 0, 0)

    def client_size(self, hwnd: int):
        rect = self.wintypes.RECT()
        if not self.user32.GetClientRect(hwnd, self.ctypes.byref(rect)):
            raise InputUnavailableError("could not read client size")
        return rect.right - rect.left, rect.bottom - rect.top


class MoveMouse:
    KEYEVENTF_KEYDOWN = _WindowsPhysicalBackend.KEYEVENTF_KEYDOWN
    KEYEVENTF_KEYUP = _WindowsPhysicalBackend.KEYEVENTF_KEYUP
    MOUSEEVENTF_LEFTDOWN = _WindowsPhysicalBackend.MOUSEEVENTF_LEFTDOWN
    MOUSEEVENTF_LEFTUP = _WindowsPhysicalBackend.MOUSEEVENTF_LEFTUP
    MOUSEEVENTF_RIGHTDOWN = _WindowsPhysicalBackend.MOUSEEVENTF_RIGHTDOWN
    MOUSEEVENTF_RIGHTUP = _WindowsPhysicalBackend.MOUSEEVENTF_RIGHTUP
    MOUSEEVENTF_LEFTCLICK = MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_LEFTUP
    MOUSEEVENTF_RIGHTCLICK = MOUSEEVENTF_RIGHTDOWN | MOUSEEVENTF_RIGHTUP

    def __init__(self, backend=None, hwnd_resolver: Optional[Callable[[], int]] = None,
                 expected_size_resolver: Optional[Callable[[], object]] = None):
        native_resolution = backend is None and hwnd_resolver is None
        self._backend = backend
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
        hwnd = self._valid_window()
        if self.backend.foreground_window() != hwnd:
            raise InputUnavailableError(
                "physical input requires the configured window to be foreground"
            )
        return hwnd

    def _valid_window(self) -> int:
        hwnd = int(self._hwnd_resolver() or 0)
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
        if not hwnd or not self.backend.is_window(hwnd):
            raise InputUnavailableError("configured input_hwnd is not a valid window")
        if self.backend.foreground_window() != hwnd:
            raise InputUnavailableError(
                "physical input requires the configured window to be foreground"
            )

    def _validate_position(self, hwnd: int, position):
        if (not isinstance(position, (tuple, list)) or len(position) != 2
                or any(isinstance(value, bool) or not isinstance(value, Integral)
                       for value in position)):
            raise ValueError("client position must contain two integral coordinates")
        x, y = int(position[0]), int(position[1])
        width, height = self.backend.client_size(hwnd)
        if not (0 <= x < int(width) and 0 <= y < int(height)):
            raise ValueError(
                f"client position {(x, y)!r} is outside {(width, height)!r}"
            )
        return x, y

    @staticmethod
    def _key_code(key: str) -> int:
        try:
            return KeyToHex[key]
        except (KeyError, TypeError):
            raise ValueError(f"unsupported key: {key!r}") from None

    def MainWindowSize(self):
        return self.backend.client_size(self._valid_window())

    def IsFocused(self):
        hwnd = self._valid_window()
        return self.backend.foreground_window() == hwnd

    def Position(self):
        with input_transaction("physical cursor position"):
            hwnd = self._active_window()
            return self.backend.screen_to_client(hwnd, self.backend.cursor_position())

    def Press(self, Key):
        code = self._key_code(Key)
        with input_transaction(f"physical key {Key}"):
            self._active_window()
            down = True
            try:
                self.backend.key_event(code, False)
                checkpoint()
            finally:
                if down:
                    self.backend.key_event(code, True)

    def PressHotkey(self, Option, Key):
        if Option not in {"ctrl", "shift", "alt"}:
            raise ValueError(f"unsupported modifier: {Option!r}")
        modifier = self._key_code(Option)
        key = self._key_code(Key)
        with input_transaction(f"physical hotkey {Option}+{Key}"):
            hwnd = self._active_window()
            modifier_down = False
            key_down = False
            try:
                modifier_down = True
                self.backend.key_event(modifier, False)
                checkpoint()
                self._ensure_active(hwnd)
                key_down = True
                self.backend.key_event(key, False)
                checkpoint()
            finally:
                try:
                    if key_down:
                        self.backend.key_event(key, True)
                finally:
                    if modifier_down:
                        self.backend.key_event(modifier, True)

    def _move_local(self, hwnd: int, position) -> None:
        self._ensure_active(hwnd)
        screen_position = self.backend.client_to_screen(
            hwnd, (int(position[0]), int(position[1]))
        )
        self._ensure_active(hwnd)
        self.backend.set_cursor_position(screen_position)

    def _click(self, position, down_flag: int, up_flag: int, reason: str) -> None:
        with input_transaction(reason):
            hwnd = self._active_window()
            position = self._validate_position(hwnd, position)
            self._move_local(hwnd, position)
            down = False
            try:
                self._ensure_active(hwnd)
                down = True
                self.backend.mouse_event(down_flag)
                checkpoint()
            finally:
                if down:
                    self.backend.mouse_event(up_flag)

    def LeftClick(self, Position):
        self._click(Position, self.MOUSEEVENTF_LEFTDOWN, self.MOUSEEVENTF_LEFTUP,
                    "physical left click")

    def RightClick(self, Position):
        self._click(Position, self.MOUSEEVENTF_RIGHTDOWN, self.MOUSEEVENTF_RIGHTUP,
                    "physical right click")

    def MoveTo(self, X, Y):
        with input_transaction("physical cursor move"):
            hwnd = self._active_window()
            position = self._validate_position(hwnd, (X, Y))
            self._move_local(hwnd, position)

    def DragTo(self, From, To):
        with input_transaction("physical drag"):
            hwnd = self._active_window()
            start = self._validate_position(hwnd, From)
            destination = self._validate_position(hwnd, To)
            self._move_local(hwnd, start)
            down = False
            try:
                self._ensure_active(hwnd)
                down = True
                self.backend.mouse_event(self.MOUSEEVENTF_LEFTDOWN)
                checkpoint()
                self._move_local(hwnd, destination)
                checkpoint()
            finally:
                if down:
                    self.backend.mouse_event(self.MOUSEEVENTF_LEFTUP)

    def UseOn(self, From, To):
        with input_transaction("physical use-on"):
            hwnd = self._active_window()
            source = self._validate_position(hwnd, From)
            destination = self._validate_position(hwnd, To)
            self._move_local(hwnd, source)
            right_down = False
            try:
                self._ensure_active(hwnd)
                right_down = True
                self.backend.mouse_event(self.MOUSEEVENTF_RIGHTDOWN)
                checkpoint()
            finally:
                if right_down:
                    self.backend.mouse_event(self.MOUSEEVENTF_RIGHTUP)
            checkpoint()
            self._move_local(hwnd, destination)
            left_down = False
            try:
                self._ensure_active(hwnd)
                left_down = True
                self.backend.mouse_event(self.MOUSEEVENTF_LEFTDOWN)
                checkpoint()
            finally:
                if left_down:
                    self.backend.mouse_event(self.MOUSEEVENTF_LEFTUP)


__all__ = ["InputUnavailableError", "MoveMouse"]
