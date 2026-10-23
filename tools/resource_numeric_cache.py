"""Bounded exact-result reuse inside the guarded resource reader.

This cache owns accepted numeric ROI results only. ResourceReader still checks
current geometry, labels and duplicate panels before calling the numeric reader.
A result contains no capture timestamp; the caller supplies current observations.
"""
from __future__ import annotations

from collections import OrderedDict
import math
from types import MappingProxyType

import numpy as np

from tibia_client.adaptive_text import MAX_DIGITS, MAX_NUMBER_PIXELS
from tibia_client.models import Quality, Reading


def _array_evidence(array):
    return array.shape, array.dtype.str, array.tobytes()


def _glyph_evidence(glyph):
    return _array_evidence(glyph.normalized), glyph.ratio, glyph.height, glyph.holes


def _immutable(value):
    """Reading freezes containers but can still contain mutable custom values."""
    if type(value) in (str, bytes, int, float, bool, type(None)):
        return True
    if type(value) is tuple:
        return all(_immutable(item) for item in value)
    if type(value) is MappingProxyType:
        return all(_immutable(key) and _immutable(item) for key, item in value.items())
    return False


class NumericResultCacheMixin:
    """Cache deterministic KNOWN results in one private serialized reader lane.

    Both entry count and retained ROI bytes are bounded. The byte limit covers
    key pixel payloads; at most max_entries immutable Reading objects and one
    snapshot of the reader's existing small template bank are also retained.
    Configuration/template changes invalidate derived caches as well as results.
    OCR, invalid inputs/options, errors and UNKNOWN readings always recompute.
    """

    def __init__(self, *args, numeric_cache_entries=128,
                 numeric_cache_bytes=2 * 1024 * 1024, **kwargs):
        if type(numeric_cache_entries) is not int or numeric_cache_entries < 0:
            raise ValueError('numeric_cache_entries must be a non-negative integer')
        if type(numeric_cache_bytes) is not int or numeric_cache_bytes < 0:
            raise ValueError('numeric_cache_bytes must be a non-negative integer')
        self._numeric_limit = numeric_cache_entries
        self._numeric_byte_limit = numeric_cache_bytes
        self._numeric_results = OrderedDict()
        self._numeric_bytes = 0
        self._numeric_model = None
        self._numeric_hits = self._numeric_misses = 0
        super().__init__(*args, **kwargs)

    def clear_numeric_cache(self):
        self._numeric_results.clear()
        self._numeric_bytes = 0

    def numeric_cache_info(self):
        return dict(entries=len(self._numeric_results), pixel_bytes=self._numeric_bytes,
                    max_entries=self._numeric_limit, max_pixel_bytes=self._numeric_byte_limit,
                    hits=self._numeric_hits, misses=self._numeric_misses)

    def _numeric_model_evidence(self):
        # Exact bytes catch in-place edits, while scalar configuration and method
        # identities catch threshold changes and replaced recognition helpers.
        images = tuple(tuple(tuple(_array_evidence(image) for image in row) for row in bank)
                       for bank in (self._digit_images, self._alternate_digit_images))
        glyphs = tuple(tuple(tuple(_glyph_evidence(glyph) for glyph in row) for row in bank)
                       for bank in (self._digits, self._alternate_digits))
        methods = tuple(getattr(method, '__func__', method) for method in
                        (super().read, self._decode, self._decode_touching_digits, self._rank_digit,
                         self._rank_uncached, self._rank_key, self._packed_digits,
                         self._rejection_reason, self._scaled_digit_references,
                         self._comma_references))
        return (self._template_score, self._ambiguity_margin, frozenset(self.features),
                images, glyphs, _glyph_evidence(self._comma),
                tuple(_glyph_evidence(glyph) for glyph in self._small_scale_commas),
                tuple(_array_evidence(image) for image in self._alternate_comma_images), methods)

    def _check_numeric_model(self):
        evidence = self._numeric_model_evidence()
        if evidence != self._numeric_model:
            self.clear_numeric_cache()
            if self._numeric_model is not None:
                for name in ('_scaled_digit_references', '_comma_references',
                             '_packed_digits', '_rank_key'):
                    clear = getattr(getattr(self, name), 'cache_clear', None)
                    if clear is not None:
                        clear()
            self._numeric_model = evidence

    def read(self, image, *, scale=1.0, max_digits=9, allow_grouping=True,
             use_ocr=False, family=None):
        if not self._numeric_limit or not self._numeric_byte_limit:
            self._numeric_misses += 1
            return super().read(image, scale=scale, max_digits=max_digits,
                                allow_grouping=allow_grouping, use_ocr=use_ocr, family=family)
        # Do not let Python's True == 1 or NaN/hash behavior make an invalid
        # request hit a valid cache entry. Leave validation/error details to the
        # underlying reader; this predicate only decides eligibility for reuse.
        valid_options = (type(scale) in (int, float) and math.isfinite(scale)
                         and .5 <= scale <= 4.0
                         and type(max_digits) is int and 1 <= max_digits <= MAX_DIGITS
                         and type(allow_grouping) is bool and use_ocr is False
                         and (family is None or type(family) is str and family == 'alternate'))
        valid_image = (type(image) is np.ndarray and image.dtype == np.uint8
                       and image.ndim == 3 and image.shape[2] == 3
                       and image.shape[0] > 0 and image.shape[1] > 0
                       and image.shape[0] * image.shape[1] <= MAX_NUMBER_PIXELS)
        # Model changes matter even on an OCR/oversized bypass, since the
        # pre-existing scaled-template/rank caches also depend on those inputs.
        self._check_numeric_model()
        key = None
        if valid_options and valid_image and image.nbytes <= self._numeric_byte_limit:
            pixels = image.tobytes()
            key = (image.shape, float(scale), max_digits, allow_grouping, family, pixels)
            cached = self._numeric_results.get(key)
            if cached is not None:
                self._numeric_results.move_to_end(key)
                self._numeric_hits += 1
                return cached
        self._numeric_misses += 1
        result = super().read(image, scale=scale, max_digits=max_digits,
                              allow_grouping=allow_grouping, use_ocr=use_ocr, family=family)
        if (key is not None and type(result) is Reading and result.quality is Quality.KNOWN
                and _immutable(result.value) and _immutable(result.detail)
                and _immutable(result.diagnostics)
                and image.tobytes() == key[-1]
                and self._numeric_model_evidence() == self._numeric_model):
            self._numeric_results[key] = result
            self._numeric_bytes += len(key[-1])
            while (len(self._numeric_results) > self._numeric_limit
                   or self._numeric_bytes > self._numeric_byte_limit):
                removed, _ = self._numeric_results.popitem(last=False)
                self._numeric_bytes -= len(removed[-1])
        return result
