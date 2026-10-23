"""Touching search reuses intervals without pruning accepted or competing paths."""
from functools import lru_cache
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from tibia_client.adaptive_text import _Rank
from tools import resource_numeric_vector as numeric
from tools.resource_numeric_vector import VectorNumberReader
from tools.resource_reader import FEATURES


def touching_crop():
    corpus = Path(__file__).parent / 'fixtures/screenshots'
    manifest = json.loads((corpus / 'manifest.json').read_text())
    entry = next(item for item in manifest['screenshots'] if item['id'] == 'dfg-yonabra-ek195')
    left, top, right, bottom = entry['labels']['resource_soul']['box']
    with Image.open(corpus / entry['file']) as image:
        return np.asarray(image.convert('RGB'))[top - 3:bottom + 3, left - 8:right + 3].copy()


def test_real_and_random_masks_match_full_uncached_enumeration(monkeypatch):
    reader = VectorNumberReader(FEATURES)
    original = touching_crop()
    damaged = original.copy()
    damaged[3:5, 8:10] = 72
    random = np.random.default_rng(90210)
    cases = [(original, .7, 3), (damaged, .7, 3), (np.full_like(original, 72), .7, 3)]
    for width in (10, 17, 24, 36, 49):
        for _ in range(3):
            mask = random.random((12, width, 1)) > .55
            cases.append((np.repeat(np.where(mask, 190, 72).astype(np.uint8), 3, axis=2), .7, 6))
    actual = [reader._decode_touching_digits(*case) for case in cases]
    assert actual[0] is not None and actual[1] is None
    # Disabling only this per-call cache runs the previous exhaustive algorithm.
    # Existing reader LRUs are already constructed and remain unchanged.
    monkeypatch.setattr(numeric, 'lru_cache', lambda **kwargs: lambda function: function)
    assert [reader._decode_touching_digits(*case) for case in cases] == actual


@pytest.mark.parametrize('ambiguous', [False, True])
def test_same_text_tie_order_and_distinct_text_runner_up_survive(monkeypatch, ambiguous):
    reader = VectorNumberReader(FEATURES)
    mask = np.broadcast_to(np.arange(1, 16, dtype=np.uint8), (3, 15))
    image = np.repeat(mask[..., None], 3, axis=2)
    monkeypatch.setattr(numeric, 'numeric_ink', lambda pixels: pixels[:, :, 0])
    monkeypatch.setattr(numeric, '_glyph', lambda crop: (int(crop[0, 0]) - 1, int(crop[0, -1])))

    def rank(segment, scale, family, force_strict=False):
        left, right = segment
        token = '2' if right - left == 5 else '1'
        score = .94 if token == '1' else (.93 if ambiguous else .75)
        alternate = '3' if right - left == 7 else '8'
        return (_Rank(token, score), _Rank(alternate, .5))

    monkeypatch.setattr(reader, '_rank_digit', rank)
    actual = reader._decode_touching_digits(image, 1., 3)
    if ambiguous:
        assert actual is None
    else:
        assert actual.text == '11'
        assert actual.ranks[0][1].token == '3'
    monkeypatch.setattr(numeric, 'lru_cache', lambda **kwargs: lambda function: function)
    assert reader._decode_touching_digits(image, 1., 3) == actual


def test_repeated_intervals_rank_once_and_recompute_for_changed_pixels(monkeypatch):
    reader = VectorNumberReader(FEATURES)
    original_glyph = numeric._glyph
    calls = []

    def glyph(mask):
        calls.append((mask.shape, mask.tobytes()))
        return original_glyph(mask)

    monkeypatch.setattr(numeric, '_glyph', glyph)
    random = np.random.default_rng(932)
    mask = random.random((12, 43, 1)) > .45
    image = np.repeat(np.where(mask, 190, 72).astype(np.uint8), 3, axis=2)
    result = reader._decode_touching_digits(image, .7, 6)
    memoized_calls = len(calls)
    assert memoized_calls == len(set(calls))
    calls.clear()
    monkeypatch.setattr(numeric, 'lru_cache', lambda **kwargs: lambda function: function)
    assert reader._decode_touching_digits(image, .7, 6) == result
    assert len(calls) > memoized_calls
    calls.clear()
    monkeypatch.setattr(numeric, 'lru_cache', lru_cache)
    image[0, 5] ^= 255
    reader._decode_touching_digits(image, .7, 6)
    assert calls
