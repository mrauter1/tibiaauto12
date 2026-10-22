"""Bind calibration to the selected desktop without importing native APIs."""
from __future__ import annotations

from tibia_client.window_target import WindowTarget


def validate_target_binding(configuration, calibration):
    selected = WindowTarget.from_config(configuration)
    calibrated = WindowTarget.from_config(calibration)
    if selected.fingerprint != calibrated.fingerprint:
        raise ValueError(
            "Desktop target or Sandbox viewport/focus samples changed; "
            "reconfigure and recalibrate this profile"
        )
    if selected.kind == "sandbox":
        if calibration.get("MouseOption", 1) != 1:
            raise ValueError("Windows Sandbox requires physical mouse/keyboard input")
        if configuration.get("capture_hwnd") != configuration.get("input_hwnd"):
            raise ValueError("Sandbox capture and input must select the same host window")
        if tuple(configuration.get("capture_size", ())) != selected.size:
            raise ValueError("Sandbox capture size differs from the selected viewport")
    return selected
