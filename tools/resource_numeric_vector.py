"""Batch glyph scoring with the baseline's scores and acceptance guards."""
from __future__ import annotations

from functools import lru_cache
from itertools import product
import math

import numpy as np

from tibia_client.adaptive_text import _Decoded, _Glyph, _Rank, _alternatives, _glyph, _glyph_score, _valid_text
from tibia_client.adaptive_text import _ink as numeric_ink
from tools.resource_reader_experiments import ExperimentalNumberReader


class VectorNumberReader(ExperimentalNumberReader):
    supports_font_family = True

    def __init__(self, features=(), **kwargs):
        super().__init__(features, **kwargs)
        self._packed_digits = lru_cache(maxsize=24)(self._packed_digits)
        self._rank_key = lru_cache(maxsize=512)(self._rank_key)

    def _packed_digits(self, scale, family):
        refs=self._scaled_digit_references(scale,family)
        flat=[g for variants in refs for g in variants]
        labels=np.array([i for i,variants in enumerate(refs) for _ in variants])
        return (np.stack([g.normalized for g in flat]),np.array([g.ratio for g in flat]),
                np.array([g.holes for g in flat]),labels)

    def _rank_digit(self, candidate, scale, family, force_strict=False):
        if 'glyph_cache' in self.features:
            return self._rank_key(candidate.normalized.tobytes(),candidate.ratio,
                                  candidate.height,candidate.holes,scale,family,force_strict)
        return self._rank_uncached(candidate,scale,family,force_strict)

    def _rank_key(self, pixels, ratio, height, holes, scale, family, force_strict):
        candidate=_Glyph(np.frombuffer(pixels,dtype=bool).reshape(28,20),ratio,height,holes)
        return self._rank_uncached(candidate,scale,family,force_strict)

    def _rank_uncached(self, candidate, scale, family, force_strict=False):
        masks,ratios,holes,labels=self._packed_digits(scale,family)
        pixels=1.-np.mean(masks!=candidate.normalized,axis=(1,2))
        ratio=np.minimum(ratios,candidate.ratio)/np.maximum(ratios,candidate.ratio)
        scores=.85*pixels+.15*ratio
        if 'topology' in self.features and not force_strict:
            scores-=.08*np.abs(holes-candidate.holes)
        else:
            scores=np.where(holes==candidate.holes,scores,-np.inf)
        ranked=[]
        for number in range(10):
            score=float(np.max(scores[labels==number]))
            if np.isfinite(score):
                ranked.append(_Rank(str(number),score))
        if not ranked:
            raise ValueError('number contains a glyph with unsupported topology')
        ranked.sort(key=lambda item:item.score,reverse=True)
        return tuple(ranked[:3])

    def _decode_touching_digits(self, image, scale, max_digits, family=None):
        if 'touching_cache' not in self.features:
            return super()._decode_touching_digits(image, scale, max_digits, family)
        mask = numeric_ink(image)
        _, columns = np.where(mask)
        if not len(columns):
            return None
        tight = mask[:, columns.min():columns.max() + 1]
        width = tight.shape[1]
        if 'prune' in self.features:
            max_digits = min(max_digits, width // math.ceil(5.0 * scale))
        candidates = {}
        for count in range(2, max_digits + 1):
            cell_width = width / count
            if not 2.5 * scale <= cell_width <= 12.0 * scale:
                continue
            choices = []
            for index in range(1, count):
                center = round(index * width / count)
                choices.append(tuple(x for x in range(center - 1, center + 2) if 1 <= x < width))
            for internal in product(*choices):
                boundaries = (0, *internal, width)
                if any(right - left < math.ceil(5.0 * scale)
                       for left, right in zip(boundaries, boundaries[1:])):
                    continue
                ranks = []
                for left, right in zip(boundaries, boundaries[1:]):
                    try:
                        glyph = _glyph(tight[:, left:right])
                        # Keep this fallback's original strict hole policy,
                        # even when segmented glyphs use soft topology.
                        ranked = self._rank_digit(glyph, scale, family, force_strict=True)
                    except ValueError:
                        break
                    ranks.append(ranked)
                if len(ranks) != count:
                    continue
                text = ''.join(items[0].token for items in ranks)
                if not _valid_text(text, max_digits, False):
                    continue
                scores = [items[0].score for items in ranks]
                minimum, average = min(scores), sum(scores) / len(scores)
                old = candidates.get(text)
                if old is None or (minimum, average) > old[:2]:
                    candidates[text] = minimum, average, tuple(ranks)
        ordered = sorted(candidates.items(), key=lambda item:(item[1][0], item[1][1]), reverse=True)
        if not ordered:
            return None
        text, (minimum, average, ranks) = ordered[0]
        runner_up = ordered[1][1][0] if len(ordered) > 1 else 0.0
        if minimum < 0.75 or average < 0.86 or minimum - runner_up <= 0.03:
            return None
        return _Decoded(text, minimum, average, ranks, _alternatives(ranks, max_digits, False))


    def _decode(
        self,
        image: np.ndarray,
        scale: float,
        max_digits: int,
        allow_grouping: bool,
        family: str | None = None,
    ) -> _Decoded:
        ink = numeric_ink(image)
        if not bool(np.any(ink)):
            raise ValueError("number contains no supported digit foreground")
        if (
            bool(np.any(ink[0]))
            or bool(np.any(ink[-1]))
            or bool(np.any(ink[:, 0]))
            or bool(np.any(ink[:, -1]))
        ):
            raise ValueError("number foreground touches the ROI edge and may be clipped")

        occupied = np.any(ink, axis=0)
        starts = np.flatnonzero(occupied & np.r_[True, ~occupied[:-1]])
        ends = np.flatnonzero(occupied & np.r_[~occupied[1:], True]) + 1
        if len(starts) == 0 or len(starts) > max_digits * 2 + 1:
            raise ValueError("number has an unsupported number of glyph segments")
        gaps = starts[1:] - ends[:-1]
        maximum_gap = max(3, math.ceil(5.0 * scale))
        if len(gaps) and int(np.max(gaps)) > maximum_gap:
            raise ValueError("number contains abnormally separated glyph runs")
        glyphs = tuple(_glyph(ink[:, start:end]) for start, end in zip(starts, ends))
        tall_height = max(item.height for item in glyphs)
        expected_height = 9.5 * scale
        if tall_height < max(4.0, expected_height * 0.62) or tall_height > expected_height * 1.55:
            raise ValueError("number glyph height is inconsistent with the estimated scale")

        scaled_digits = self._scaled_digit_references(scale, family)
        ranks: list[tuple[_Rank, ...]] = []
        for candidate in glyphs:
            is_short = candidate.height <= tall_height * 0.68
            if is_short:
                commas = self._comma_references(scale, family)
                comma_score = max(_glyph_score(candidate, comma) for comma in commas)
                ranks.append((_Rank(",", comma_score),))
                continue
            ranks.append(self._rank_digit(candidate, scale, family))

        text = "".join(items[0].token for items in ranks)
        token_scores = [items[0].score for items in ranks]
        digit_scores = [items[0].score for items in ranks if items[0].token != ","]
        if not digit_scores:
            raise ValueError("number contains no digit glyphs")
        alternatives = _alternatives(tuple(ranks), max_digits, allow_grouping)
        return _Decoded(
            text,
            min(token_scores),
            sum(digit_scores) / len(digit_scores),
            tuple(ranks),
            alternatives,
        )
