"""Local Windows font rendering for opt-in recognition experiments.

No font file or converted glyph asset is distributed.  GDI renders the installed
Verdana face into memory.  Importing this module is safe on non-Windows hosts;
requesting native rendering there raises ``NativeFontUnavailable``.  The source
font hash is provenance, not a font-version whitelist.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import math
import os
from pathlib import Path
import sys

import cv2
import numpy as np


class NativeFontUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NativeGlyph:
    token: str
    coverage: np.ndarray
    advance: float


@dataclass(frozen=True, slots=True)
class NativeFontBank:
    scale: float
    glyphs: tuple[NativeGlyph, ...]
    height: int
    source_sha256: str
    name: str = "Verdana Bold 11px / Windows GDI NONANTIALIASED_QUALITY"


def _readonly(array):
    contiguous = np.ascontiguousarray(array, dtype=np.float32)
    # A bytes-backed array cannot be made writeable again by a cache consumer.
    return np.frombuffer(contiguous.tobytes(), dtype=np.float32).reshape(contiguous.shape)


@lru_cache(maxsize=1)
def font_provenance() -> dict[str, str]:
    if sys.platform != "win32":
        raise NativeFontUnavailable("native GDI font rendering requires Windows")
    source = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "verdanab.ttf"
    try:
        digest = sha256(source.read_bytes()).hexdigest()
    except OSError as exc:
        raise NativeFontUnavailable("installed Verdana Bold font file is unavailable") from exc
    return {"font": "Verdana Bold", "pixel_size": "11", "renderer": "Windows GDI",
            "quality": "NONANTIALIASED_QUALITY", "source_sha256": digest,
            "asset_policy": "installed font rendered locally in memory; no font redistribution"}


def _render(text: str) -> tuple[np.ndarray, tuple[int, ...], int]:
    """Return glyph cells in native font coordinates, advances and top bearing."""
    font_provenance()
    import ctypes as C
    from ctypes import wintypes as W

    class Header(C.Structure):
        _fields_ = [("size", W.DWORD), ("width", W.LONG), ("height", W.LONG),
                    ("planes", W.WORD), ("bits", W.WORD), ("compression", W.DWORD),
                    ("image_size", W.DWORD), ("xp", W.LONG), ("yp", W.LONG),
                    ("used", W.DWORD), ("important", W.DWORD)]

    class Info(C.Structure):
        _fields_ = [("header", Header), ("colors", W.DWORD * 3)]

    class Size(C.Structure):
        _fields_ = [("cx", W.LONG), ("cy", W.LONG)]

    gdi = C.WinDLL("gdi32", use_last_error=True)
    gdi.CreateCompatibleDC.argtypes = [W.HDC]
    gdi.CreateCompatibleDC.restype = W.HDC
    gdi.CreateDIBSection.argtypes = [W.HDC, C.POINTER(Info), W.UINT,
                                   C.POINTER(C.c_void_p), W.HANDLE, W.DWORD]
    gdi.CreateDIBSection.restype = W.HBITMAP
    gdi.SelectObject.argtypes = [W.HDC, W.HANDLE]
    gdi.SelectObject.restype = W.HANDLE
    gdi.CreateFontW.argtypes = [C.c_int] * 5 + [W.DWORD] * 8 + [W.LPCWSTR]
    gdi.CreateFontW.restype = W.HFONT
    gdi.GetTextFaceW.argtypes = [W.HDC, C.c_int, W.LPWSTR]
    gdi.GetTextExtentPoint32W.argtypes = [W.HDC, W.LPCWSTR, C.c_int, C.POINTER(Size)]
    gdi.TextOutW.argtypes = [W.HDC, C.c_int, C.c_int, W.LPCWSTR, C.c_int]
    gdi.TextOutW.restype = W.BOOL
    gdi.SetTextColor.argtypes = [W.HDC, W.DWORD]
    gdi.SetBkColor.argtypes = [W.HDC, W.DWORD]
    gdi.DeleteObject.argtypes = [W.HANDLE]
    gdi.DeleteDC.argtypes = [W.HDC]
    width, height = max(64, len(text) * 16 + 8), 32
    info = Info()
    info.header = Header(C.sizeof(Header), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = C.c_void_p()
    dc = gdi.CreateCompatibleDC(None)
    if not dc:
        raise NativeFontUnavailable("CreateCompatibleDC failed")
    bitmap = gdi.CreateDIBSection(dc, C.byref(info), 0, C.byref(bits), None, 0)
    font = gdi.CreateFontW(-11, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 3, 0, "Verdana")
    previous_bitmap = previous_font = None
    try:
        if not bitmap or not bits.value or not font:
            raise NativeFontUnavailable("GDI font or bitmap creation failed")
        previous_bitmap = gdi.SelectObject(dc, bitmap)
        previous_font = gdi.SelectObject(dc, font)
        face = C.create_unicode_buffer(128)
        gdi.GetTextFaceW(dc, len(face), face)
        if face.value.casefold() != "verdana":
            raise NativeFontUnavailable("GDI substituted a different installed font")
        C.memset(bits, 0, width * height * 4)
        gdi.SetTextColor(dc, 0xffffff)
        gdi.SetBkColor(dc, 0)
        advances = []
        for char in text:
            extent = Size()
            if not gdi.GetTextExtentPoint32W(dc, char, 1, C.byref(extent)):
                raise NativeFontUnavailable("GDI could not measure text")
            advances.append(int(extent.cx))
        if not gdi.TextOutW(dc, 2, 2, text, len(text)):
            raise NativeFontUnavailable("GDI could not render text")
        gdi.GdiFlush()
        pixels = np.ctypeslib.as_array((C.c_ubyte * (width * height * 4)).from_address(
            bits.value)).reshape(height, width, 4)[:, :, 0].copy()
        return pixels[:, 2:2 + sum(advances)] > 127, tuple(advances), 2
    finally:
        if previous_font:
            gdi.SelectObject(dc, previous_font)
        if previous_bitmap:
            gdi.SelectObject(dc, previous_bitmap)
        if font:
            gdi.DeleteObject(font)
        if bitmap:
            gdi.DeleteObject(bitmap)
        gdi.DeleteDC(dc)


@lru_cache(maxsize=1)
def _base_font() -> NativeFontBank:
    # Immutable base preparation is shared even in uncached-scale experiments.
    # No reading result or observed image is retained.
    text = "0123456789,"
    rendered, advances, _ = _render(text)
    rows = np.flatnonzero(np.any(rendered, axis=1))
    top, bottom = int(rows[0]), int(rows[-1] + 1)
    x = 0
    glyphs = []
    for token, advance in zip(text, advances):
        glyphs.append(NativeGlyph(token, _readonly(rendered[top:bottom, x:x + advance]),
                                  float(advance)))
        x += advance
    return NativeFontBank(.8, tuple(glyphs), bottom - top,
                          font_provenance()["source_sha256"])


def _scaled_font(scale: float) -> NativeFontBank:
    base = _base_font()
    ratio = scale / .8
    height = max(1, round(base.height * ratio))
    glyphs = tuple(NativeGlyph(g.token, _readonly(cv2.resize(
        g.coverage, (max(1, round(g.advance * ratio)), height),
        interpolation=cv2.INTER_AREA if ratio < 1 else cv2.INTER_LINEAR)),
        g.advance * ratio) for g in base.glyphs)
    return NativeFontBank(scale, glyphs, height, base.source_sha256)


_cached_scaled_font = lru_cache(maxsize=64)(_scaled_font)


def get_font(scale: float = .8, *, family: str | None = None,
             cache: bool = False) -> NativeFontBank:
    """Immutable scale-specific numeric bank; ``cache`` is an explicit ablation."""
    if not math.isfinite(scale) or not .35 <= scale <= 4:
        raise ValueError("native font scale must be finite and from .35 through 4")
    if family not in (None, "alternate"):
        raise ValueError("unsupported font family")
    # Alternate is not silently rendered as another family. The numeric reader
    # may still use its declared existing-font fallback for that family.
    if family == "alternate":
        raise NativeFontUnavailable("native Verdana model does not cover alternate family")
    return (_cached_scaled_font if cache else _scaled_font)(float(scale))


def render_native_text(text: str, scale: float = .8, *, cache: bool = False) -> np.ndarray:
    """Render a local label/complete string as a tight immutable coverage image."""
    if not isinstance(text, str) or not text or len(text) > 128:
        raise ValueError("native text must contain 1 through 128 characters")
    return (_cached_text if cache else _render_text)(text, float(scale))


def _render_text(text: str, scale: float) -> np.ndarray:
    if not math.isfinite(scale) or not .35 <= scale <= 4:
        raise ValueError("native font scale must be finite and from .35 through 4")
    rendered, _, _ = _render(text)
    yy, xx = np.where(rendered)
    tight = rendered[yy.min():yy.max() + 1, xx.min():xx.max() + 1].astype(np.float32)
    ratio = scale / .8
    return _readonly(cv2.resize(tight, (max(1, round(tight.shape[1] * ratio)),
                                      max(1, round(tight.shape[0] * ratio))),
                               interpolation=cv2.INTER_AREA if ratio < 1 else cv2.INTER_LINEAR))


_cached_text = lru_cache(maxsize=64)(_render_text)
