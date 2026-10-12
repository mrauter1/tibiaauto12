"""Desktop composition root and the only shared SDK session used by the UI."""

from __future__ import annotations

import atexit
import threading


_client = None
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
    global _client
    with _lock:
        previous = _client
        if previous is not None:
            try:
                previous.close()
            except BaseException:
                session.close()
                raise
        _client = session
    return session


def get_client():
    with _lock:
        if _client is None:
            raise RuntimeError("Select a calibrated client before opening an automation module")
        return _client


def close_client() -> None:
    global _client
    with _lock:
        selected = _client
        if selected is not None:
            selected.close()
        if _client is selected:
            _client = None


def poll_hotkey(key: str) -> bool:
    """Read a user control shortcut; never generates game input or steals focus."""
    if not isinstance(key, str) or not key.strip():
        raise ValueError("control hotkey must not be empty")
    # The UI polls through this boundary on its event loop; automated features
    # never import keyboard or subscribe directly to native hooks.
    import keyboard

    return bool(keyboard.is_pressed(key.replace(" ", "").lower()))


atexit.register(close_client)
