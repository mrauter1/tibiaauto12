"""Production exact-result reuse must never weaken numeric acceptance guards."""
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from tibia_client.models import Quality, Reading
from tools.resource_numeric_cache import NumericResultCacheMixin
from tools.resource_numeric_vector import VectorNumberReader
from tools.resource_reader import FEATURES


class CountingReader(VectorNumberReader):
    def __init__(self, **kwargs):
        self.calls = 0
        super().__init__(FEATURES, **kwargs)

    def read(self, *args, **kwargs):
        self.calls += 1
        return super().read(*args, **kwargs)


class CachedReader(NumericResultCacheMixin, CountingReader):
    supports_font_family = True


@pytest.fixture
def crop():
    path = Path(__file__).parent / 'fixtures/resources/paulistinha-skills.png'
    with Image.open(path) as source:
        return np.asarray(source.convert('RGB'))[3:24, 116:193].copy()


def test_exact_pixels_reuse_full_immutable_reading_and_options(crop):
    reader = CachedReader()
    first = reader.read(crop, max_digits=6)
    assert first.quality is Quality.KNOWN and first.value == 710
    assert reader.read(crop.copy(), max_digits=6) is first
    assert reader.calls == 1
    with pytest.raises(TypeError):
        first.diagnostics['score'] = 0
    # Equal logical pixels with a different memory layout remain equal evidence.
    storage = np.empty((crop.shape[0], crop.shape[1] * 2, 3), np.uint8)
    storage[:, ::2] = crop
    assert reader.read(storage[:, ::2], max_digits=6) is first
    for options in ({'max_digits': 5}, {'max_digits': 6, 'allow_grouping': False},
                    {'max_digits': 6, 'family': 'alternate'},
                    {'max_digits': 6, 'scale': 1.1}):
        reader.read(crop, **options)
    assert reader.calls == 5


@pytest.mark.parametrize('options', [
    {'scale': True}, {'scale': float('nan')}, {'scale': 5.0}, {'max_digits': True},
    {'max_digits': 0}, {'allow_grouping': 1}, {'use_ocr': 0}, {'family': 'unsupported'},
])
def test_invalid_options_cannot_hit_valid_cache(crop, options):
    reader = CachedReader()
    reader.read(crop)
    with pytest.raises(ValueError) as expected:
        CountingReader().read(crop, **options)
    with pytest.raises(ValueError) as actual:
        reader.read(crop, **options)
    assert str(actual.value) == str(expected.value)
    assert reader.calls == 2
    assert reader.numeric_cache_info()['hits'] == 0


def test_changed_pixels_and_shape_recompute(crop):
    reader = CachedReader()
    reader.read(crop)
    changed = crop.copy()
    changed[0, 0, 0] ^= 1
    reader.read(changed)
    reader.read(crop.reshape(crop.shape[1], crop.shape[0], 3))
    assert reader.calls == 3


def test_ocr_unknown_and_errors_are_not_retained(crop, monkeypatch):
    reader = CachedReader()
    reader.read(crop)
    reader.read(crop, use_ocr=True)
    reader.read(crop, use_ocr=True)
    assert reader.calls == 3
    blank = np.full_like(crop, 72)
    assert reader.read(blank).quality is Quality.UNKNOWN
    assert reader.read(blank).quality is Quality.UNKNOWN
    assert reader.calls == 5 and reader.numeric_cache_info()['entries'] == 1

    def error(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError('backend failed')
    monkeypatch.setattr(CountingReader, 'read', error)
    for _ in range(2):
        with pytest.raises(RuntimeError, match='backend failed'):
            reader.read(crop)
    assert reader.calls == 7 and reader.numeric_cache_info()['entries'] == 0


@pytest.mark.parametrize('value,diagnostics', [
    (np.array([1]), {}), (7, {'nested': (np.array([1]),)}),
    (7, {'nested': {1, 2}}),
])
def test_reading_with_mutable_nested_values_bypasses_cache(crop, monkeypatch, value, diagnostics):
    def mutable(self, *args, **kwargs):
        self.calls += 1
        return Reading(value, diagnostics=diagnostics)
    monkeypatch.setattr(CountingReader, 'read', mutable)
    reader = CachedReader()
    reader.read(crop)
    reader.read(crop)
    assert reader.calls == 2 and reader.numeric_cache_info()['entries'] == 0


def test_replacing_template_packer_invalidates_accepted_result(crop, monkeypatch):
    reader = CachedReader()
    reader.read(crop)

    def broken(*args, **kwargs):
        raise RuntimeError('replaced packer')
    monkeypatch.setattr(reader, '_packed_digits', broken)
    with pytest.raises(RuntimeError, match='replaced packer'):
        reader.read(crop)
    assert reader.calls == 2 and reader.numeric_cache_info()['entries'] == 0


def test_decoder_changing_input_does_not_install_old_pixel_key(crop, monkeypatch):
    def mutate(self, image, **kwargs):
        self.calls += 1
        image[0, 0] ^= 1
        return Reading(710)
    monkeypatch.setattr(CountingReader, 'read', mutate)
    reader = CachedReader()
    reader.read(crop)
    assert reader.numeric_cache_info()['entries'] == 0


def test_threshold_and_template_changes_invalidate_results_and_derived_ranks(crop):
    reader = CachedReader()
    # Asset-loaded RGB arrays are read-only; callers can replace them with a
    # writable bank, whose subsequent in-place changes still need detection.
    reader._digit_images = ((reader._digit_images[0][0].copy(), *reader._digit_images[0][1:]),
                            *reader._digit_images[1:])
    reader._digits = ((replace(reader._digits[0][0],
                              normalized=reader._digits[0][0].normalized.copy()),
                       *reader._digits[0][1:]), *reader._digits[1:])
    assert reader.read(crop).quality is Quality.KNOWN
    reader._ambiguity_margin = 1.0
    assert reader.read(crop).quality is Quality.UNKNOWN
    reader._ambiguity_margin = .03
    assert reader.read(crop).quality is Quality.KNOWN
    assert reader.calls == 3

    # A rank cached against the old template bank must be recomputed too.
    reference = reader._digits[0][0]
    reader._rank_digit(reference, 1., None)
    assert reader._rank_key.cache_info().currsize
    reader._digit_images[0][0][0, 0, 0] ^= 1
    reader._check_numeric_model()
    assert reader.numeric_cache_info()['entries'] == 0
    assert reader._rank_key.cache_info().currsize == 0
    assert reader._packed_digits.cache_info().currsize == 0
    assert reader._scaled_digit_references.cache_info().currsize == 0
    reader.read(crop)
    assert reader.calls == 4
    # Normalized masks are separate live evidence from original RGB templates.
    reader._digits[0][0].normalized[0, 0] ^= True
    reader._check_numeric_model()
    assert reader.numeric_cache_info()['entries'] == 0


@pytest.mark.parametrize('bound', ['numeric_cache_entries', 'numeric_cache_bytes'])
def test_lru_evicts_with_entry_and_pixel_byte_bounds(crop, bound):
    reader = CachedReader(**{bound: 2 if bound.endswith('entries') else 2 * crop.nbytes})
    reader.read(crop, max_digits=6)
    reader.read(crop, max_digits=7)
    reader.read(crop, max_digits=6)  # Retain the most recently used entry.
    reader.read(crop, max_digits=8)
    assert reader.numeric_cache_info()['entries'] == 2
    assert reader.numeric_cache_info()['pixel_bytes'] == 2 * crop.nbytes
    reader.read(crop, max_digits=6)
    assert reader.calls == 3
    reader.read(crop, max_digits=7)
    assert reader.calls == 4
    reader.clear_numeric_cache()
    assert reader.numeric_cache_info()['entries'] == 0
    assert reader.numeric_cache_info()['pixel_bytes'] == 0


@pytest.mark.parametrize('bound', ['numeric_cache_entries', 'numeric_cache_bytes'])
def test_zero_bound_disables_result_reuse(crop, bound):
    reader = CachedReader(**{bound: 0})
    assert reader.read(crop) == reader.read(crop)
    assert reader.calls == 2 and reader.numeric_cache_info()['entries'] == 0


def test_image_larger_than_budget_bypasses(crop):
    reader = CachedReader(numeric_cache_bytes=crop.nbytes - 1)
    reader.read(crop)
    reader.read(crop)
    assert reader.calls == 2 and reader.numeric_cache_info()['pixel_bytes'] == 0


@pytest.mark.parametrize('bound', ['numeric_cache_entries', 'numeric_cache_bytes'])
@pytest.mark.parametrize('value', [-1, True, 1.5])
def test_cache_bound_validation(bound, value):
    with pytest.raises(ValueError, match='non-negative integer'):
        CachedReader(**{bound: value})
