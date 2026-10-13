"""Linux physical input facade backed by xdotool."""

from __future__ import annotations

from numbers import Integral
from typing import Callable, Optional

from core.LinuxClient import Execute, FindWindow
from core.MoveMouse import InputUnavailableError
from core.runtime import checkpoint, input_transaction


def _shell_values(output: str) -> dict:
    values = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


class MoveLinuxMouse:
    def __init__(self, executor: Callable = Execute,
                 window_resolver: Optional[Callable[[], str]] = None):
        self._execute = executor
        self._window_resolver = window_resolver or FindWindow

    def _active_window(self) -> str:
        window = str(self._window_resolver()).strip()
        if not window:
            raise InputUnavailableError("configured input window is unavailable")
        self._ensure_active(window)
        return window

    def _ensure_active(self, window: str) -> None:
        active = self._execute(["xdotool", "getactivewindow"]).strip()
        if active != window:
            raise InputUnavailableError(
                "physical input requires the configured window to be foreground"
            )

    @property
    def Window(self):
        return self._active_window()

    def _geometry(self, window: str) -> dict:
        return _shell_values(
            self._execute(["xdotool", "getwindowgeometry", "--shell", window])
        )

    def _validate_position(self, window: str, position):
        if (not isinstance(position, (tuple, list)) or len(position) != 2
                or any(isinstance(value, bool) or not isinstance(value, Integral)
                       for value in position)):
            raise ValueError("client position must contain two integral coordinates")
        x, y = int(position[0]), int(position[1])
        geometry = self._geometry(window)
        size = int(geometry["WIDTH"]), int(geometry["HEIGHT"])
        if not (0 <= x < size[0] and 0 <= y < size[1]):
            raise ValueError(f"client position {(x, y)!r} is outside {size!r}")
        return x, y

    def Position(self):
        with input_transaction("linux cursor position"):
            window = self._active_window()
            cursor = _shell_values(
                self._execute(["xdotool", "getmouselocation", "--shell"])
            )
            geometry = self._geometry(window)
            return (int(cursor["X"]) - int(geometry["X"]),
                    int(cursor["Y"]) - int(geometry["Y"]))

    def ScreenPosition(self):
        """Snapshot the cursor in absolute desktop coordinates."""

        with input_transaction("linux screen cursor position"):
            self._active_window()
            cursor = _shell_values(
                self._execute(["xdotool", "getmouselocation", "--shell"])
            )
            return int(cursor["X"]), int(cursor["Y"])

    def RestoreScreenPosition(self, position):
        """Restore an absolute snapshot while the client remains focused."""

        if (not isinstance(position, (tuple, list)) or len(position) != 2
                or any(isinstance(value, bool) or not isinstance(value, Integral)
                       for value in position)):
            raise ValueError("screen position must contain two integral coordinates")
        with input_transaction("linux screen cursor restore"):
            window = self._active_window()
            self._ensure_active(window)
            checkpoint()
            self._execute([
                "xdotool", "mousemove", "--",
                str(int(position[0])), str(int(position[1]))
            ])

    def MainWindowSize(self):
        with input_transaction("linux client size"):
            geometry = self._geometry(self._active_window())
            return int(geometry["WIDTH"]), int(geometry["HEIGHT"])

    def MoveTo(self, X, Y):
        with input_transaction("linux cursor move"):
            window = self._active_window()
            position = self._validate_position(window, (X, Y))
            self._ensure_active(window)
            self._execute([
                "xdotool", "mousemove", "--window", window,
                str(position[0]), str(position[1])
            ])

    def MoveMouse(self, X, Y):
        return self.MoveTo(X, Y)

    def Click(self, X, Y, click_type=1):
        if click_type not in (1, 3):
            raise ValueError("click_type must be 1 (left) or 3 (right)")
        with input_transaction("linux click"):
            window = self._active_window()
            position = self._validate_position(window, (X, Y))
            down = False
            try:
                self._ensure_active(window)
                down = True
                self._execute([
                    "xdotool", "mousemove", "--window", window,
                    str(position[0]), str(position[1]), "mousedown", str(click_type)
                ])
                checkpoint()
            finally:
                if down:
                    self._execute(["xdotool", "mouseup", str(click_type)])

    def LeftClick(self, Position):
        return self.Click(Position[0], Position[1], click_type=1)

    def RightClick(self, Position, Y=None):
        if Y is not None:
            Position = (Position, Y)
        return self.Click(Position[0], Position[1], click_type=3)

    def DragTo(self, From, To=None, ToX=None, ToY=None):
        # Accept both DragTo((x1, y1), (x2, y2)) and the legacy four numbers.
        if ToX is not None and ToY is not None:
            start = (From, To)
            destination = (ToX, ToY)
        else:
            start = From
            destination = To
        with input_transaction("linux drag"):
            window = self._active_window()
            start = self._validate_position(window, start)
            destination = self._validate_position(window, destination)
            down = False
            try:
                self._ensure_active(window)
                down = True
                self._execute([
                    "xdotool", "mousemove", "--window", window,
                    str(int(start[0])), str(int(start[1])), "mousedown", "1"
                ])
                checkpoint()
                self._ensure_active(window)
                self._execute([
                    "xdotool", "mousemove", "--window", window,
                    str(int(destination[0])), str(int(destination[1]))
                ])
                checkpoint()
            finally:
                if down:
                    self._execute(["xdotool", "mouseup", "1"])

    def UseOn(self, From, To):
        with input_transaction("linux use-on"):
            window = self._active_window()
            source = self._validate_position(window, From)
            destination = self._validate_position(window, To)
            right_down = False
            try:
                self._ensure_active(window)
                right_down = True
                self._execute([
                    "xdotool", "mousemove", "--window", window,
                    str(source[0]), str(source[1]), "mousedown", "3"
                ])
                checkpoint()
            finally:
                if right_down:
                    self._execute(["xdotool", "mouseup", "3"])
            checkpoint()
            left_down = False
            try:
                self._ensure_active(window)
                left_down = True
                self._execute([
                    "xdotool", "mousemove", "--window", window,
                    str(destination[0]), str(destination[1]), "mousedown", "1"
                ])
                checkpoint()
            finally:
                if left_down:
                    self._execute(["xdotool", "mouseup", "1"])

    def Press(self, key):
        if not isinstance(key, str) or not key:
            raise ValueError("key must be a non-empty string")
        with input_transaction(f"linux key {key}"):
            window = self._active_window()
            self._execute(["xdotool", "key", "--window", window, key])

    def PressHotkey(self, option, key):
        if not all(isinstance(value, str) and value for value in (option, key)):
            raise ValueError("hotkey keys must be non-empty strings")
        if option not in {"ctrl", "shift", "alt"}:
            raise ValueError(f"unsupported modifier: {option!r}")
        with input_transaction(f"linux hotkey {option}+{key}"):
            window = self._active_window()
            modifier_down = False
            try:
                modifier_down = True
                self._execute(["xdotool", "keydown", "--window", window, option])
                checkpoint()
                self._ensure_active(window)
                self._execute(["xdotool", "key", "--window", window, key])
                checkpoint()
            finally:
                if modifier_down:
                    self._execute(["xdotool", "keyup", "--window", window, option])

    def KeyDown(self, key):
        with input_transaction(f"linux key down {key}"):
            window = self._active_window()
            self._execute(["xdotool", "keydown", "--window", window, key])

    def KeyUp(self, key):
        with input_transaction(f"linux key up {key}"):
            window = self._active_window()
            self._execute(["xdotool", "keyup", "--window", window, key])


__all__ = ["MoveLinuxMouse"]
