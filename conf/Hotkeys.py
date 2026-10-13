"""Stable input facade shared by automation modules."""

import math
from numbers import Real
import time

from core import runtime
from core.runtime import input_transaction


class Hotkey:
    def __init__(self, MOUSE_OPTION, backend=None):
        self.MOUSE_OPTION = MOUSE_OPTION
        self.SendToClient = backend if backend is not None else self._make_backend()

    def _make_backend(self):
        # Imports stay inside construction so importing conf.Hotkeys never
        # imports an OS-specific package. The backends themselves load native
        # APIs only on first use.
        if self.MOUSE_OPTION == 0:
            from core.SendToClient import SendToClient
            return SendToClient()

        try:
            from conf.conf_manager import ConfManager
            platform = ConfManager.get("conf.json").get("platform", "windows")
        except (OSError, ValueError, TypeError):
            platform = "windows"
        if str(platform).lower() == "linux":
            from core.MoveMouseLinux import MoveLinuxMouse
            return MoveLinuxMouse()
        from core.MoveMouse import MoveMouse
        return MoveMouse()

    Hotkeys = ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "Ctrl + F1", "Ctrl + F2", "Ctrl + F3", "Ctrl + F4", "Ctrl + F5", "Ctrl + F6",
               "Ctrl + F7", "Ctrl + F8", "Ctrl + F9", "Ctrl + F10", "Ctrl + F11", "Ctrl + F12",
               "Shift + F1", "Shift + F2", "Shift + F3", "Shift + F4", "Shift + F5", "Shift + F6",
               "Shift + F7", "Shift + F8", "Shift + F9", "Shift + F10", "Shift + F11", "Shift + F12"]

    def Press(self, Key):
        if not isinstance(Key, str) or not Key.strip():
            raise ValueError("key must be a non-empty string")
        parts = [part.strip() for part in Key.split("+")]
        with input_transaction(f"hotkey {Key}"):
            if len(parts) == 1:
                return self.SendToClient.Press(parts[0])
            if len(parts) != 2 or parts[0].lower() not in {"ctrl", "shift"}:
                raise ValueError(f"unsupported hotkey: {Key!r}")
            return self.SendToClient.PressHotkey(parts[0].lower(), parts[1])

    def Position(self):
        with input_transaction("cursor position"):
            return self.SendToClient.Position()

    def ScreenPosition(self):
        """Return the physical cursor in desktop screen coordinates."""

        with input_transaction("screen cursor position"):
            return self.SendToClient.ScreenPosition()

    def RestoreScreenPosition(self, position):
        """Restore a physical cursor snapshot without action-coordinate use."""

        with input_transaction("screen cursor restore"):
            return self.SendToClient.RestoreScreenPosition(position)

    def SupportsScreenRestore(self):
        """Report support from the wrapped backend rather than this facade."""

        return all(
            callable(getattr(self.SendToClient, name, None))
            for name in ("ScreenPosition", "RestoreScreenPosition")
        )

    def LeftClick(self, X, Y):
        with input_transaction("left click"):
            return self.SendToClient.LeftClick((X, Y))

    def RightClick(self, X, Y):
        with input_transaction("right click"):
            return self.SendToClient.RightClick((X, Y))

    def MoveTo(self, X, Y):
        with input_transaction("cursor move"):
            return self.SendToClient.MoveTo(X, Y)

    def MainWindowSize(self):
        return self.SendToClient.MainWindowSize()

    def wait_until_ready(self, timeout=30):
        """Wait until this backend can safely receive input without focusing it."""
        if isinstance(timeout, bool) or not isinstance(timeout, Real):
            raise TypeError("timeout must be a real number")
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and non-negative")

        if self.MOUSE_OPTION == 0:
            checker = getattr(self.SendToClient, "IsReady", None)
            if checker is None:
                raise TypeError("message backend must provide IsReady()")
            runtime.checkpoint()
            checker()
            runtime.set_phase("Running")
            return True

        checker = getattr(self.SendToClient, "IsFocused", None)
        if checker is None:
            raise TypeError("physical backend must provide IsFocused()")
        runtime.set_phase("Waiting for game focus")
        deadline = time.monotonic() + timeout
        while True:
            runtime.checkpoint()
            if checker():
                runtime.set_phase("Running")
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("game window did not receive focus before timeout")
            runtime.wait(min(0.05, remaining))

    def DragTo(self, From, To):
        with input_transaction("drag"):
            return self.SendToClient.DragTo(From, To)

    def UseOn(self, From, To):
        with input_transaction("use-on"):
            return self.SendToClient.UseOn(From, To)
