"""Optional prebuilt duplicate-panel filter; never compiles code at runtime.

The ordinary NumPy path remains available without a compiler or library. Only a
project-local library matching this C source, pointer width, and ABI is loaded.
Build explicitly with ``python tools/build_visual_native.py`` before startup.
"""
from __future__ import annotations

import ctypes
from functools import lru_cache
import hashlib
from pathlib import Path
import sys

import numpy as np


ABI_VERSION = 1
SOURCE = Path(__file__).with_suffix('.c')
_BYTE_POINTER = ctypes.POINTER(ctypes.c_uint8)
_INT_POINTER = ctypes.POINTER(ctypes.c_int)


def source_id():
    return hashlib.sha256(SOURCE.read_bytes()).hexdigest()


def library_path():
    suffix = '.dll' if sys.platform == 'win32' else '.dylib' if sys.platform == 'darwin' else '.so'
    bits = ctypes.sizeof(ctypes.c_void_p) * 8
    return SOURCE.parents[1] / '.work/native' / f'visual_guard_{source_id()}_{bits}{suffix}'


def _open_backend(path, expected_source_id):
    library = ctypes.CDLL(str(path))
    version = library.visual_guard_abi
    version.argtypes, version.restype = [], ctypes.c_int
    identity = library.visual_guard_source_id
    identity.argtypes, identity.restype = [], ctypes.c_char_p
    if version() != ABI_VERSION or identity() != expected_source_id.encode('ascii'):
        raise ValueError('Native visual guard does not match the current source and ABI')
    function = library.visual_guard_candidates
    function.argtypes = [_BYTE_POINTER, ctypes.c_int, ctypes.c_int,
                         _INT_POINTER, _INT_POINTER, _BYTE_POINTER, _BYTE_POINTER,
                         ctypes.c_int, ctypes.c_int, ctypes.c_int, _INT_POINTER, ctypes.c_int]
    function.restype = ctypes.c_int
    return function


@lru_cache(maxsize=1)
def _load_backend():
    try:
        path = library_path()
        return _open_backend(path, source_id()) if path.is_file() else None
    except (OSError, AttributeError, ValueError):
        return None


def available():
    """Whether a compatible prebuilt backend can be loaded in this process."""
    return _load_backend() is not None


def try_candidates(pixels, samples, out_width, out_height):
    """Return row-major (ys, xs), capped at 129, or None for NumPy fallback.

    Inputs are checked before entering C. Backend failures never become evidence
    of panel absence: None instructs the caller to run the original full scan.
    ``samples`` contains (dy, dx, foreground, coarse_brightness) tuples.
    """
    function = _load_backend()
    if function is None:
        return None
    if (not isinstance(pixels, np.ndarray) or pixels.dtype != np.uint8 or pixels.ndim != 3
            or pixels.shape[2] != 3 or not pixels.flags.c_contiguous):
        return None
    height, width = pixels.shape[:2]
    integer = lambda value: isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_))
    if (not integer(out_width) or not integer(out_height)
            or not 0 < out_width <= width <= np.iinfo(np.int32).max
            or not 0 < out_height <= height <= np.iinfo(np.int32).max
            or not 1 <= len(samples) <= 20):
        return None
    for item in samples:
        if (len(item) != 4 or not integer(item[0]) or not integer(item[1])
                or not (0 <= item[0] <= height-out_height)
                or not (0 <= item[1] <= width-out_width)
                or item[2] not in (False, True) or item[3] not in (False, True)):
            return None
    data = np.asarray(samples, dtype=np.int32)
    dy, dx = np.ascontiguousarray(data[:, 0]), np.ascontiguousarray(data[:, 1])
    ink, coarse = np.asarray(data[:, 2], dtype=np.uint8), np.asarray(data[:, 3], dtype=np.uint8)
    output = np.empty((129, 2), dtype=np.int32)
    try:
        count = function(pixels.ctypes.data_as(_BYTE_POINTER), height, width,
                         dy.ctypes.data_as(_INT_POINTER), dx.ctypes.data_as(_INT_POINTER),
                         ink.ctypes.data_as(_BYTE_POINTER), coarse.ctypes.data_as(_BYTE_POINTER),
                         len(samples), out_width, out_height, output.ctypes.data_as(_INT_POINTER), 129)
        if not 0 <= count <= 129:
            return None
        points = output[:count]
        if count and (np.any(points < 0) or np.any(points[:, 0] >= out_width)
                      or np.any(points[:, 1] >= out_height)):
            return None
        return points[:, 1], points[:, 0]
    except (OSError, ValueError, TypeError, RuntimeError, ctypes.ArgumentError):
        return None
