"""Physical-region GDI pixels shared by production capture and experiments.

Native bindings are lazy; importing this module does not access the desktop.
"""

from __future__ import annotations

import ctypes
from numbers import Integral
import os

from PIL import Image

from tibia_client.window_target import WindowTargetError


MAX_RGB_BYTES = 64 * 1024 * 1024
SRCCOPY = 0x00CC0020  # Match Pillow's default include_layered_windows=False.


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("header", _BitmapInfoHeader), ("color", ctypes.c_uint32 * 1)]


def _rectangle(value):
    if (not isinstance(value, (tuple, list)) or len(value) != 4
            or any(isinstance(item, bool) or not isinstance(item, Integral) for item in value)):
        raise WindowTargetError("GDI region must contain four integral physical coordinates")
    left, top, right, bottom = tuple(int(item) for item in value)
    width, height = right - left, bottom - top
    if (min(width, height) <= 0 or max(width, height) > 16384
            or width * height * 3 > MAX_RGB_BYTES
            or any(abs(item) > 2**30 for item in (left, top, right, bottom))):
        raise WindowTargetError("GDI region dimensions/coordinates exceed the supported bounds")
    return left, top, width, height


class _GDIReadback:
    """Own only per-capture GDI resources; borrowed screen DC uses ReleaseDC."""

    def __init__(self):
        if os.name != "nt":
            raise WindowTargetError("GDI region capture requires Windows")
        from ctypes import wintypes

        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.user.GetDC.argtypes, self.user.GetDC.restype = [wintypes.HWND], wintypes.HDC
        self.user.ReleaseDC.argtypes, self.user.ReleaseDC.restype = [wintypes.HWND, wintypes.HDC], ctypes.c_int
        self.gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self.gdi.CreateCompatibleDC.restype = wintypes.HDC
        self.gdi.CreateDIBSection.argtypes = [
            wintypes.HDC, ctypes.POINTER(_BitmapInfo), ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, ctypes.c_uint32,
        ]
        self.gdi.CreateDIBSection.restype = wintypes.HBITMAP
        self.gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        self.gdi.SelectObject.restype = wintypes.HANDLE
        self.gdi.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_uint32]
        self.gdi.BitBlt.restype = wintypes.BOOL
        self.gdi.GdiFlush.argtypes, self.gdi.GdiFlush.restype = [], wintypes.BOOL
        self.gdi.DeleteDC.argtypes, self.gdi.DeleteDC.restype = [wintypes.HDC], wintypes.BOOL
        self.gdi.DeleteObject.argtypes, self.gdi.DeleteObject.restype = [wintypes.HANDLE], wintypes.BOOL

    @staticmethod
    def _valid_object(handle):
        return handle not in (None, 0, -1, ctypes.c_void_p(-1).value)

    def read_pixels(self, pointer, byte_count):
        return ctypes.string_at(pointer, byte_count)

    def capture(self, rect):
        left, top, width, height = _rectangle(rect)
        screen_dc = memory_dc = bitmap = original = None
        image = None
        failure = None
        cleanup = []
        try:
            screen_dc = self.user.GetDC(0)
            if not screen_dc:
                raise WindowTargetError("GetDC failed for the desktop")
            memory_dc = self.gdi.CreateCompatibleDC(screen_dc)
            if not memory_dc:
                raise WindowTargetError("CreateCompatibleDC failed")
            info = _BitmapInfo()
            info.header.biSize = ctypes.sizeof(_BitmapInfoHeader)
            info.header.biWidth, info.header.biHeight = width, -height
            info.header.biPlanes, info.header.biBitCount = 1, 32
            info.header.biCompression = 0  # BI_RGB; top-down BGRX, no scaling.
            info.header.biSizeImage = width * height * 4
            bits = ctypes.c_void_p()
            bitmap = self.gdi.CreateDIBSection(screen_dc, ctypes.byref(info), 0,
                                                ctypes.byref(bits), None, 0)
            if not bitmap or not bits.value:
                raise WindowTargetError("CreateDIBSection failed or returned no pixel buffer")
            selected = self.gdi.SelectObject(memory_dc, bitmap)
            if not self._valid_object(selected):
                raise WindowTargetError("SelectObject failed for the region DIB")
            original = selected
            if not self.gdi.BitBlt(memory_dc, 0, 0, width, height, screen_dc,
                                   left, top, SRCCOPY):
                raise WindowTargetError("BitBlt failed for the selected physical region")
            if not self.gdi.GdiFlush():
                raise WindowTargetError("GdiFlush failed before DIB readback")
            raw = self.read_pixels(bits.value, width * height * 4)
            if len(raw) != width * height * 4:
                raise WindowTargetError("DIB readback returned an inconsistent byte count")
            image = Image.frombytes("RGB", (width, height), raw, "raw", "BGRX", 0, 1)
        except Exception as exc:
            failure = exc
        finally:
            if original is not None and memory_dc:
                try:
                    if not self._valid_object(self.gdi.SelectObject(memory_dc, original)):
                        cleanup.append("restore selected bitmap failed")
                except Exception as exc:
                    cleanup.append(f"restore selected bitmap: {exc}")
            # Delete the compatible DC before its bitmap so even a failed
            # restoration cannot leave our bitmap selected into a live DC.
            if memory_dc:
                try:
                    if not self.gdi.DeleteDC(memory_dc):
                        cleanup.append("DeleteDC failed")
                except Exception as exc:
                    cleanup.append(f"DeleteDC: {exc}")
            if bitmap:
                try:
                    if not self.gdi.DeleteObject(bitmap):
                        cleanup.append("DeleteObject failed")
                except Exception as exc:
                    cleanup.append(f"DeleteObject: {exc}")
            if screen_dc:
                try:
                    if not self.user.ReleaseDC(0, screen_dc):
                        cleanup.append("ReleaseDC failed")
                except Exception as exc:
                    cleanup.append(f"ReleaseDC: {exc}")
        if failure is not None or cleanup:
            detail = str(failure) if failure is not None else "pixel read completed"
            if cleanup:
                detail += "; cleanup: " + "; ".join(cleanup)
            raise WindowTargetError(f"GDI region capture failed: {detail}") from failure
        return image
