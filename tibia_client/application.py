"""Desktop composition root and the only shared SDK session used by the UI."""

from __future__ import annotations

import atexit
import threading
from types import MappingProxyType
from typing import Callable, Mapping, Any


_client = None
_compatibility_provider: Callable[[], Mapping[str, Any]] | None = None
_lock = threading.RLock()


def configure_client(mouse_mode: int, *, adapter=None, **session_options):
    """Replace the client only after shutting down the previous session.

    Configuration happens on the Tk thread before any feature windows open.
    Construction is lazy: it does not capture or send input.
    """
    from tibia_client.session import ClientSession
    from tibia_client.desktop import DesktopAdapter

    if type(mouse_mode) is not int or mouse_mode not in (0, 1):
        raise ValueError("input mode must be 0 (messages) or 1 (physical)")
    selected = adapter if adapter is not None else DesktopAdapter(mouse_mode)
    try:
        session = ClientSession(selected, **session_options)
    except BaseException:
        close = getattr(selected, "close", None)
        if callable(close):
            close()
        raise
    global _client, _compatibility_provider
    with _lock:
        previous = _client
        if previous is not None:
            try:
                previous.close()
            except BaseException:
                session.close()
                raise
        _client = session
        _compatibility_provider = None
    return session


def get_client():
    with _lock:
        if _client is None:
            raise RuntimeError("Select a calibrated client before opening an automation module")
        return _client


def set_compatibility_provider(
    provider: Callable[[], Mapping[str, Any]] | None,
) -> None:
    """Install the composition-owned, read-only compatibility projection."""

    if provider is not None and not callable(provider):
        raise TypeError("compatibility provider must be callable")
    global _compatibility_provider
    with _lock:
        _compatibility_provider = provider


def compatibility_status() -> Mapping[str, Any]:
    """Return an immutable status mapping without starting capture or input."""

    with _lock:
        provider = _compatibility_provider
    if provider is None:
        return MappingProxyType({
            "profile_id": None,
            "calibration_id": None,
            "ready": False,
            "capabilities": (),
            "problems": ("no compatibility profile is active",),
        })
    try:
        value = dict(provider())
    except Exception as exc:
        return MappingProxyType({
            "profile_id": None,
            "calibration_id": None,
            "ready": False,
            "capabilities": (),
            "problems": (f"compatibility status failed: {type(exc).__name__}: {exc}",),
        })
    return MappingProxyType({
        "profile_id": value.get("profile_id"),
        "calibration_id": value.get("calibration_id"),
        "ready": bool(value.get("ready", False)),
        "capabilities": tuple(value.get("capabilities", ())),
        "problems": tuple(value.get("problems", ())),
    })


def close_client() -> None:
    global _client, _compatibility_provider
    with _lock:
        selected = _client
        if selected is not None:
            selected.close()
        if _client is selected:
            _client = None
        _compatibility_provider = None


def poll_hotkey(key: str) -> bool:
    """Read a user control shortcut; never generates game input or steals focus."""
    if not isinstance(key, str) or not key.strip():
        raise ValueError("control hotkey must not be empty")
    # The UI polls through this boundary on its event loop; automated features
    # never import keyboard or subscribe directly to native hooks.
    import keyboard

    return bool(keyboard.is_pressed(key.replace(" ", "").lower()))


class HotkeyLatch:
    """Retain even brief shortcuts between Tk polls; the hook never touches Tk."""

    def __init__(self, key: str, *, keyboard_backend=None):
        self._native = None
        self._pending = threading.Event()
        import os
        if keyboard_backend is None and os.name == "nt" and key.replace(" ", "").lower() == "ctrl+shift+f11":
            from core.global_stop import WindowsStopHotkey
            self._native = WindowsStopHotkey(self._pending.set)
            return
        if keyboard_backend is None:
            import keyboard as keyboard_backend
        self._keyboard = keyboard_backend
        self._handle = self._keyboard.add_hotkey(
            key.replace(" ", "").lower(), self._pending.set, suppress=False)

    def consume(self) -> bool:
        if not self._pending.is_set():
            return False
        self._pending.clear()
        return True

    def close(self) -> None:
        if self._native is not None:
            self._native.close()
            self._pending.clear()
            return
        if self._handle is not None:
            self._keyboard.remove_hotkey(self._handle)
            self._handle = None
        self._pending.clear()


atexit.register(close_client)
