"""Bounded window-message input using client-local coordinates."""

from __future__ import annotations

import os
from numbers import Integral
from typing import Callable, Optional

from conf.HexMapKeys import KEY_CODES_CASEFOLDED
from conf.conf_manager import ConfManager
from core.MoveMouse import InputUnavailableError
from core.input_diagnostics import InputDiagnostics
from core.runtime import checkpoint, input_transaction


def _configured_input_hwnd() -> int:
    data = ConfManager.get("conf.json")
    return int(data.get("input_hwnd", data.get("hwnd", 0)) or 0)


def _configured_capture_size():
    return ConfManager.get("conf.json").get("capture_size")


class _WindowsMessageBackend:
    # Values from WinUser.h. Keeping them here also makes fake backends small.
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_MOUSEMOVE = 0x0200
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_RBUTTONDOWN = 0x0204
    WM_RBUTTONUP = 0x0205
    MK_LBUTTON = 0x0001
    MK_RBUTTON = 0x0002
    SMTO_ABORTIFHUNG = 0x0002

    def __init__(self) -> None:
        if os.name != "nt":
            raise InputUnavailableError("Windows message input is unavailable")
        import win32gui

        self.win32gui = win32gui

    def is_window(self, hwnd: int) -> bool:
        return bool(self.win32gui.IsWindow(hwnd))

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self.win32gui.IsIconic(hwnd))

    def foreground_window(self) -> int:
        return int(self.win32gui.GetForegroundWindow())

    def set_foreground(self, hwnd: int) -> None:
        self.win32gui.SetForegroundWindow(hwnd)

    def window_rect(self, hwnd: int):
        return self.win32gui.GetWindowRect(hwnd)

    def client_size(self, hwnd: int):
        left, top, right, bottom = self.win32gui.GetClientRect(hwnd)
        return right - left, bottom - top

    def cursor_position(self):
        return self.win32gui.GetCursorPos()

    def screen_to_client(self, hwnd: int, position):
        return self.win32gui.ScreenToClient(hwnd, position)

    def send_message(self, hwnd: int, message: int, wparam: int, lparam: int,
                     timeout_ms: int = 250):
        return self.win32gui.SendMessageTimeout(
            hwnd, message, wparam, lparam, self.SMTO_ABORTIFHUNG, timeout_ms
        )

    def post_message(self, hwnd: int, message: int, wparam: int, lparam: int):
        return self.win32gui.PostMessage(hwnd, message, wparam, lparam)

    @staticmethod
    def make_long(x: int, y: int) -> int:
        return (int(x) & 0xFFFF) | ((int(y) & 0xFFFF) << 16)

    @staticmethod
    def availability_diagnostic(hwnd):
        from tibia_client.direct_capture import diagnose_client_availability

        return diagnose_client_availability(hwnd)


class SendToClient(InputDiagnostics):
    WM_KEYDOWN = _WindowsMessageBackend.WM_KEYDOWN
    WM_KEYUP = _WindowsMessageBackend.WM_KEYUP
    WM_MOUSEMOVE = _WindowsMessageBackend.WM_MOUSEMOVE
    WM_LBUTTONDOWN = _WindowsMessageBackend.WM_LBUTTONDOWN
    WM_LBUTTONUP = _WindowsMessageBackend.WM_LBUTTONUP
    WM_RBUTTONDOWN = _WindowsMessageBackend.WM_RBUTTONDOWN
    WM_RBUTTONUP = _WindowsMessageBackend.WM_RBUTTONUP
    MK_LBUTTON = _WindowsMessageBackend.MK_LBUTTON
    MK_RBUTTON = _WindowsMessageBackend.MK_RBUTTON

    def __init__(self, backend=None, hwnd_resolver: Optional[Callable[[], int]] = None,
                 timeout_ms: int = 250,
                 expected_size_resolver: Optional[Callable[[], object]] = None,
                 *, preflight=False, error_diagnostic=None):
        if type(preflight) is not bool:
            raise TypeError("preflight must be a boolean")
        self.preflight = preflight
        self._init_diagnostics(error_diagnostic)
        native_resolution = backend is None and hwnd_resolver is None
        self._backend = backend
        self._hwnd_resolver = hwnd_resolver or _configured_input_hwnd
        self._expected_size_resolver = (
            expected_size_resolver
            if expected_size_resolver is not None
            else (_configured_capture_size if native_resolution else None)
        )
        self.timeout_ms = int(timeout_ms)
        if self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")

    @property
    def backend(self):
        if self._backend is None:
            self._backend = _WindowsMessageBackend()
        return self._backend

    @property
    def hwnd(self) -> int:
        """Return the current configured input handle for legacy callers."""
        return int(self._hwnd_resolver() or 0)

    def _window(self) -> int:
        hwnd = self.hwnd
        self._operation_hwnd = hwnd
        if not self.preflight:
            if hwnd <= 0:
                raise ValueError("configured input_hwnd must be a positive integer")
            return hwnd
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

    @staticmethod
    def _key_code(key: str) -> int:
        try:
            return KEY_CODES_CASEFOLDED[key.casefold()]
        except (KeyError, TypeError, AttributeError):
            raise ValueError(f"unsupported key: {key!r}") from None

    def _send(self, hwnd: int, message: int, wparam: int = 0, lparam: int = 0):
        return self._call("send_message",
            hwnd, message, wparam, lparam, timeout_ms=self.timeout_ms
        )

    def _release_message(self, hwnd, message, wparam=0, lparam=0):
        return self._release("send_message", hwnd, message, wparam, lparam,
                             timeout_ms=self.timeout_ms)

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
            width = height = 0x8000
        else:
            if (not isinstance(size, (tuple, list)) or len(size) != 2
                    or any(isinstance(value, bool) or not isinstance(value, Integral)
                           or value <= 0 for value in size)):
                raise ValueError("configured capture_size is invalid")
            width, height = size
        if (not (0 <= x < int(width) and 0 <= y < int(height))
                or x > 0x7FFF or y > 0x7FFF):
            raise ValueError(
                f"client position {(x, y)!r} is outside {(width, height)!r}"
            )
        return x, y

    def _position_param(self, position) -> int:
        return self.backend.make_long(position[0], position[1])

    def SetForeground(self):
        with input_transaction("set foreground window"):
            hwnd = self._window()
            self.backend.set_foreground(hwnd)

    def GetWindowSizes(self):
        with input_transaction("window dimensions"):
            return self.backend.window_rect(self._window())

    def IsFocused(self):
        with input_transaction("window focus state"):
            hwnd = self._window()
            return (not self.backend.is_iconic(hwnd)
                    and self.backend.foreground_window() == hwnd)

    def IsReady(self):
        """Validate the message target without requiring or changing focus."""
        self._window()
        return True

    def Position(self):
        with self._operation("message cursor position"):
            hwnd = self._window()
            return self._call("screen_to_client", hwnd, self._call("cursor_position"))

    def Press(self, Key):
        code = self._key_code(Key)
        with self._operation(f"message key {Key}"):
            hwnd = self._window()
            key_down = False
            try:
                key_down = True
                self._send(hwnd, self.WM_KEYDOWN, code, 0)
                checkpoint()
            finally:
                if key_down:
                    self._release_message(hwnd, self.WM_KEYUP, code, 0)

    def PressHotkey(self, Option, Key):
        if Option not in {"ctrl", "shift", "alt"}:
            raise ValueError(f"unsupported modifier: {Option!r}")
        modifier = self._key_code(Option)
        key = self._key_code(Key)
        with self._operation(f"message hotkey {Option}+{Key}"):
            hwnd = self._window()
            modifier_down = False
            key_down = False
            try:
                modifier_down = True
                self._send(hwnd, self.WM_KEYDOWN, modifier, 0)
                checkpoint()
                key_down = True
                self._send(hwnd, self.WM_KEYDOWN, key, 0)
                checkpoint()
            finally:
                try:
                    if key_down:
                        self._release_message(hwnd, self.WM_KEYUP, key, 0)
                finally:
                    if modifier_down:
                        self._release_message(hwnd, self.WM_KEYUP, modifier, 0)

    def MoveTo(self, X, Y):
        with self._operation("message cursor move"):
            hwnd = self._window()
            position = self._validate_position(hwnd, (X, Y))
            self._call("post_message",
                hwnd, self.WM_MOUSEMOVE, 0, self._position_param(position)
            )

    def _click(self, position, down_message: int, up_message: int,
               button_flag: int, reason: str) -> None:
        with self._operation(reason):
            hwnd = self._window()
            position = self._validate_position(hwnd, position)
            parameter = self._position_param(position)
            self._call("post_message", hwnd, self.WM_MOUSEMOVE, 0, parameter)
            down = False
            try:
                down = True
                self._send(hwnd, down_message, button_flag, parameter)
                checkpoint()
            finally:
                if down:
                    self._release_message(hwnd, up_message, 0, parameter)

    def LeftClick(self, Position):
        self._click(Position, self.WM_LBUTTONDOWN, self.WM_LBUTTONUP,
                    self.MK_LBUTTON, "message left click")

    def RightClick(self, Position):
        self._click(Position, self.WM_RBUTTONDOWN, self.WM_RBUTTONUP,
                    self.MK_RBUTTON, "message right click")

    def DragTo(self, From, To):
        with self._operation("message drag"):
            hwnd = self._window()
            start = self._position_param(self._validate_position(hwnd, From))
            end = self._position_param(self._validate_position(hwnd, To))
            self._call("post_message", hwnd, self.WM_MOUSEMOVE, 0, start)
            down = False
            try:
                down = True
                self._send(hwnd, self.WM_LBUTTONDOWN, self.MK_LBUTTON, start)
                checkpoint()
                self._send(hwnd, self.WM_MOUSEMOVE, self.MK_LBUTTON, end)
                checkpoint()
            finally:
                if down:
                    self._release_message(hwnd, self.WM_LBUTTONUP, 0, end)

    def UseOn(self, From, To):
        with self._operation("message use-on"):
            hwnd = self._window()
            source = self._position_param(self._validate_position(hwnd, From))
            destination = self._position_param(self._validate_position(hwnd, To))
            self._call("post_message", hwnd, self.WM_MOUSEMOVE, 0, source)
            right_down = False
            try:
                right_down = True
                self._send(hwnd, self.WM_RBUTTONDOWN, self.MK_RBUTTON, source)
                checkpoint()
            finally:
                if right_down:
                    self._release_message(hwnd, self.WM_RBUTTONUP, 0, source)
            checkpoint()
            self._call("post_message", hwnd, self.WM_MOUSEMOVE, 0, destination)
            left_down = False
            try:
                left_down = True
                self._send(hwnd, self.WM_LBUTTONDOWN, self.MK_LBUTTON, destination)
                checkpoint()
            finally:
                if left_down:
                    self._release_message(hwnd, self.WM_LBUTTONUP, 0, destination)

    def MainWindowSize(self):
        with input_transaction("message client size"):
            return self.backend.client_size(self._window())


__all__ = ["SendToClient"]
