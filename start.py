"""TibiaAuto application entry point."""
from __future__ import annotations

import os
import sys
from typing import Callable, Sequence

from conf.conf_manager import ConfManager, ConfigError


DEFAULT_CONFIG = {
    "configured": False,
    "hwnd": 0,
    "capture_hwnd": 0,
    "input_hwnd": 0,
    "capture_size": [],
    "preferences_name": "NewConfig.json",
    "platform": "windows",
}


class UnsupportedPlatformError(RuntimeError):
    pass


def prepare_config() -> dict:
    """Upgrade missing settings without replacing valid saved preferences."""
    ConfManager.ensure_defaults(DEFAULT_CONFIG, "conf.json")

    def migrate(current):
        legacy = current.get("hwnd", 0)
        if not current.get("capture_hwnd") and legacy:
            current["capture_hwnd"] = legacy
        if not current.get("input_hwnd") and legacy:
            current["input_hwnd"] = legacy
        current["platform"] = "windows"

    return ConfManager.update("conf.json", migrate)


def main(argv: Sequence[str] | None = None, chooser: Callable[[], None] | None = None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if os.name != "nt" or "linux" in arguments:
        raise UnsupportedPlatformError(
            "TibiaAuto currently requires Windows for native capture and input. "
            "Linux mode is not supported."
        )
    prepare_config()
    if chooser is None:
        # Import only after the platform check; importing start is headless.
        from modules import choose_capture_window
        chooser = choose_capture_window
    return chooser()


if __name__ == "__main__":
    try:
        main()
    except (UnsupportedPlatformError, ConfigError) as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        raise SystemExit(2)
