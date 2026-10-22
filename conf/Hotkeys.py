"""Stable input facade shared by automation modules."""

import math
from numbers import Real
import time

from core import runtime
from core.runtime import input_transaction
from conf.HexMapKeys import ACTION_HOTKEYS


class Hotkey:
    def __init__(self, MOUSE_OPTION, backend=None, *, preflight=False):
        if type(preflight) is not bool:
            raise TypeError("preflight must be a boolean")
        self.preflight = preflight
        self.MOUSE_OPTION = MOUSE_OPTION
        self.SendToClient = backend if backend is not None else self._make_backend()

    def _make_backend(self):
        # Imports stay inside construction so importing conf.Hotkeys never
        # imports an OS-specific package. The backends themselves load native
        # APIs only on first use.
        from conf.conf_manager import ConfManager
        from tibia_client.window_target import WindowTarget, TargetInput

        config = ConfManager.get("conf.json")
        target = WindowTarget.from_config(config)
        if target.kind == "sandbox" and self.MOUSE_OPTION != 1:
            raise ValueError("Windows Sandbox requires physical mouse/keyboard input")
        if self.MOUSE_OPTION == 0:
            from core.SendToClient import SendToClient
            return SendToClient(preflight=self.preflight)

        try:
            platform = config.get("platform", "windows")
        except (OSError, ValueError, TypeError):
            platform = "windows"
        if str(platform).lower() == "linux":
            from core.MoveMouseLinux import MoveLinuxMouse
            return MoveLinuxMouse()
        from core.MoveMouse import MoveMouse
        hwnd = config.get("input_hwnd", config.get("hwnd", 0))
        size = target.host_size if target.kind == "sandbox" else config.get("capture_size")
        native = MoveMouse(hwnd_resolver=lambda: hwnd,
                           expected_size_resolver=lambda: size,
                           preflight=self.preflight)
        if target.kind == "sandbox":
            if config.get("capture_hwnd") != config.get("input_hwnd"):
                raise ValueError("Sandbox capture and input must use the same host window")
            # Bind the session's native target and geometry once. A file change
            # is latched by DesktopProfileController before production dispatch.
            wrapped = TargetInput(native, target, lambda: hwnd, preflight=self.preflight)
            if self.preflight:
                native.readiness_guard = wrapped.check_ready
            return wrapped
        return native

    Hotkeys = list(ACTION_HOTKEYS)

    def Activate(self):
        """One focus handoff on start/resume; message and Linux modes retain their behavior."""
        if self.MOUSE_OPTION != 1:
            return False
        activate = getattr(self.SendToClient, "Activate", None)
        return activate() if callable(activate) else False

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
        """Compatibility entry point; normal dispatch does not poll availability."""
        if isinstance(timeout, bool) or not isinstance(timeout, Real):
            raise TypeError("timeout must be a real number")
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and non-negative")

        if not self.preflight:
            runtime.checkpoint()
            runtime.set_phase("Running")
            return True

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

    @property
    def last_error_diagnostic(self):
        return getattr(self.SendToClient, "last_error_diagnostic", None)

    def diagnose_availability(self, error):
        return self.SendToClient.diagnose_availability(error)

    def DragTo(self, From, To):
        with input_transaction("drag"):
            return self.SendToClient.DragTo(From, To)

    def UseOn(self, From, To):
        with input_transaction("use-on"):
            return self.SendToClient.UseOn(From, To)
