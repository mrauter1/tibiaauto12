"""Composable, opt-in factorial experiments; readers receive pixels only."""
from __future__ import annotations

from tools.resource_reader_experiments import (
    ExperimentalResourceReader, VARIANTS as OLD_VARIANTS,
)
from tools.resource_numeric_vector import VectorNumberReader

FACTORS = {
    "base": (),
    "rows": ("A",),
    "numbers": ("B",),
    "cache": ("C",),
    "rows_numbers": ("A", "B"),
    "rows_cache": ("A", "C"),
    "numbers_cache": ("B", "C"),
    "all": ("A", "B", "C"),
}
BASE_FEATURES = OLD_VARIANTS["accelerated_fast"]


def make_number_reader(variant, **kwargs):
    factors = FACTORS[variant]
    baseline = VectorNumberReader(BASE_FEATURES, **kwargs)
    if "B" not in factors:
        return baseline
    from tools.resource_native_numbers import NativeNumberReader
    return NativeNumberReader(fallback=baseline, cache="C" in factors,
                              ocr=kwargs.get("ocr"))


def make_reader(variant, **kwargs):
    """Order C before A so cached geometry wraps the selected locator."""
    factors = FACTORS[variant]
    mixins = []
    if "C" in factors:
        from tools.resource_calibrated_cache import GeometryCacheMixin
        mixins.append(GeometryCacheMixin)
    if "A" in factors:
        from tools.resource_row_geometry import StructuralRowMixin
        mixins.append(StructuralRowMixin)
    cls = type("NativeExperiment_" + variant, (*mixins, ExperimentalResourceReader), {})
    numbers = kwargs.pop("number_reader", None)
    return cls(BASE_FEATURES, number_reader=numbers or make_number_reader(variant), **kwargs)
