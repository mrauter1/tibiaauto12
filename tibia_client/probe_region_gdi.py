"""Experimental physical-region GDI readback; no production/default changes.

Only pixel acquisition differs from the overlay-aware screen probe: BitBlt copies
the requested host-client rectangle directly into a top-down DIB. The same host
foreground, bounds, size, annotation qualification, point-hit, and before/after
checks run around it. Importing this module never accesses the desktop.
"""

from __future__ import annotations

from tibia_client.probe_overlay import _OverlayScreenBackend, _capture_client_with_backend
from tibia_client.region_capture import _GDIReadback


class _RegionGDIOverlayBackend(_OverlayScreenBackend):
    def grab(self, rect):
        return _GDIReadback().capture(rect)


def capture_client_region_gdi_overlay_aware(hwnd, expected_size=None):
    """Run identical annotation-aware checks around physical-region GDI pixels."""
    return _capture_client_with_backend(hwnd, expected_size, _RegionGDIOverlayBackend)


__all__ = ["capture_client_region_gdi_overlay_aware"]
