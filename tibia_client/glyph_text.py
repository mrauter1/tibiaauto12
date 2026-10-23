"""Experimental character-only bitmap-font decoder; no production imports.

Uses the extracted BMFont map, not a creature dictionary or OCR labels. Callers
explicitly instantiate GlyphReader. Scores describe pixel agreement, not a
probability. Unsupported or competing interpretations return text=None.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from pathlib import Path
import re

import cv2
import numpy as np
from PIL import Image


DEFAULT_FONT = Path(__file__).resolve().parents[1] / 'profiles/tibia-15.33-bitmap-font'


@dataclass(frozen=True)
class GlyphReading:
    text: str | None
    candidate: str
    score: float
    margin: float
    boxes: tuple[tuple[int, int, int, int], ...] = ()
    reason: str = ''


@dataclass(frozen=True)
class Glyph:
    character: str
    pixels: np.ndarray
    left: int
    top: int
    advance: int


@dataclass(frozen=True)
class _Path:
    text: str
    cost: float
    mass: float
    minimum: float
    boxes: tuple[tuple[int, int, int, int], ...]


def foreground(rgb: np.ndarray) -> np.ndarray:
    """Same neutral foreground contract as the production battle-list OCR."""
    values = np.asarray(rgb)
    if (values.dtype != np.uint8 or values.ndim != 3 or values.shape[2] != 3
            or not values.size or values.shape[0] * values.shape[1] > 500_000):
        raise ValueError('Expected a nonempty bounded uint8 RGB crop')
    wide = values.astype(np.int16)
    return (wide.min(axis=2) >= 100) & (np.ptp(wide, axis=2) < 40)


def _fields(line: str) -> dict[str, int]:
    return {key: int(value) for key, value in re.findall(r'(\w+)=(-?\d+)', line)}


class GlyphReader:
    """Decode an unseen name from an explicitly supplied text-row crop.

    No screenshot acquisition, game input, thread, OS OCR or name vocabulary.
    Reference preparation is cached, but recognized text is never cached.
    scale is a calibrated UI scale. A bounded nearby font-scale bank tolerates
    capture rasterization; it does not guarantee support for arbitrary fonts.
    """

    def __init__(self, font_dir=None, *, min_score=.86, min_glyph_score=.68,
                 ambiguity_margin=.04, beam_width=3):
        if not 0 < min_score <= 1 or not 0 < min_glyph_score <= 1:
            raise ValueError('Match thresholds must be in (0, 1]')
        if not 0 <= ambiguity_margin <= 1 or not 1 <= beam_width <= 8:
            raise ValueError('Invalid ambiguity margin or beam width')
        root = Path(font_dir) if font_dir is not None else DEFAULT_FONT
        lines = (root / 'Verdana10px.fnt').read_text(encoding='utf-8').splitlines()
        common = next((_fields(line) for line in lines if line.startswith('common ')), None)
        if common is None or common.get('pages') != 1:
            raise ValueError('A single-page BMFont map is required')
        rgba = np.asarray(Image.open(root / 'Verdana10px.png').convert('RGBA'))
        if rgba.shape[:2] != (common['scaleH'], common['scaleW']):
            raise ValueError('BMFont atlas dimensions do not match')
        self.line_height = common['lineHeight']
        self.baseline = common['base']
        self.space_advance = 3
        glyphs = []
        seen = set()
        for line in lines:
            if not line.startswith('char '):
                continue
            item = _fields(line)
            code = item['id']
            if code in seen or not 0 <= code <= 0x10ffff or item.get('page') != 0:
                raise ValueError('Invalid or duplicate glyph record')
            seen.add(code)
            x, y, w, h = (item[key] for key in ('x', 'y', 'width', 'height'))
            if min(x, y, w, h) < 0 or x+w > rgba.shape[1] or y+h > rgba.shape[0]:
                raise ValueError('Glyph lies outside the atlas')
            if item['xadvance'] <= 0:
                raise ValueError('Glyph advances must be positive')
            if code == 32:
                self.space_advance = item['xadvance']
                continue
            tile = rgba[y:y+h, x:x+w]
            # This atlas stores black outlines and light foreground separately.
            mask = (tile[:, :, :3].max(axis=2) > 0) & (tile[:, :, 3] > 0)
            ys, xs = np.nonzero(mask)
            if not len(xs):
                continue
            left, top, right, bottom = xs.min(), ys.min(), xs.max()+1, ys.max()+1
            pixels = mask[top:bottom, left:right].astype(np.float32)
            pixels.flags.writeable = False
            glyphs.append(Glyph(chr(code), pixels, item['xoffset']+int(left),
                                item['yoffset']+int(top), item['xadvance']))
        if not glyphs:
            raise ValueError('Font map contains no foreground glyphs')
        self.glyphs = tuple(glyphs)
        self.min_score = float(min_score)
        self.min_glyph_score = float(min_glyph_score)
        self.ambiguity_margin = float(ambiguity_margin)
        self.beam_width = int(beam_width)

    @lru_cache(maxsize=24)
    def _scaled(self, scale):
        templates = []
        for glyph in self.glyphs:
            h, w = glyph.pixels.shape
            size = (max(1, round(w*scale)), max(1, round(h*scale)))
            pixels = cv2.resize(glyph.pixels, size, interpolation=cv2.INTER_LINEAR)
            advance = max(1, round((glyph.advance-glyph.left)*scale))
            templates.append((glyph.character, pixels, round(glyph.top*scale), advance))
        return tuple(templates)

    def _decode_at(self, mask, scale, origin):
        height, width = mask.shape
        templates = []
        # Compare complete character cells, including their background. This
        # penalizes unexplained strokes rather than finding small glyph subsets.
        scaled = self._scaled(scale)
        padding = max(max(pixels.shape[1], advance) for _, pixels, _, advance in scaled)
        padded = np.pad(mask.astype(np.float32), ((0, 0), (0, padding)))
        prefix = np.r_[0.0, padded.sum(axis=0).cumsum()]
        for character, pixels, top, advance in scaled:
            y = top+origin
            h, w = pixels.shape
            if y < 0 or y+h > height:
                continue
            cell_width = max(w, advance)
            template = np.zeros((height, cell_width), np.float32)
            template[y:y+h, :w] = pixels
            mass = float(template.sum())
            intersection = cv2.matchTemplate(padded, template, cv2.TM_CCORR)[0, :width]
            observed = prefix[cell_width:cell_width+width]-prefix[:width]
            costs = np.maximum(0, mass+observed-2*intersection)
            scores = np.clip(2*intersection/np.maximum(1, mass+observed), 0, 1)
            templates.append((character, advance, w, y, h, mass, costs, scores))
        if not templates:
            return []
        columns = mask.any(axis=0)
        next_ink = np.full(width+1, width, dtype=int)
        for x in range(width-1, -1, -1):
            next_ink[x] = x if columns[x] else next_ink[x+1]
        paths = [[] for _ in range(width+1)]
        paths[0] = [_Path('', 0., 0., 1., ())]
        finals = []
        space = max(2, round(self.space_advance*scale))
        for position in range(width+1):
            if not paths[position]:
                continue
            active = sorted(paths[position], key=lambda p: p.cost)[:self.beam_width]
            x = int(next_ink[position])
            if x == width:
                finals.extend(active)
                continue
            for state in active:
                if len(state.text) > 96:
                    continue
                gap = ' ' if state.text and x-position >= space else ''
                for character, advance, w, y, h, mass, costs, scores in templates:
                    if x+w > width:
                        continue
                    score = float(scores[x])
                    if score < .50:
                        continue
                    end = min(width, x+advance)
                    if end <= position:
                        continue
                    candidate = _Path(state.text+gap+character, state.cost+float(costs[x]),
                                      state.mass+mass, min(state.minimum, score),
                                      state.boxes+((x, y, x+w, y+h),))
                    bucket = paths[end]
                    bucket.append(candidate)
                    # Bound work before the next column consumes the bucket.
                    if len(bucket) > self.beam_width*4:
                        bucket.sort(key=lambda p: p.cost)
                        del bucket[self.beam_width:]
        return finals

    def read_line(self, rgb, scale=1.0) -> GlyphReading:
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale):
            raise ValueError('Scale must be a finite number')
        if not .5 <= scale <= 3:
            raise ValueError('Scale must be between 0.5 and 3')
        mask = foreground(rgb)
        if not mask.any():
            return GlyphReading(None, '', 0., 0., reason='no supported text foreground')
        # Generic chrome rejection, not a crop adjustment from expected labels.
        row_counts = mask.sum(axis=1)
        mask = mask.copy()
        mask[row_counts >= max(28, math.ceil(mask.shape[1]*.72))] = False
        ys = np.flatnonzero(mask.any(axis=1))
        if not len(ys):
            return GlyphReading(None, '', 0., 0., reason='no glyph-height foreground')
        bands = np.split(ys, np.flatnonzero(np.diff(ys) > max(2, round(scale)))+1)
        bands = [band for band in bands if 3 <= band[-1]-band[0]+1 <= round(18*scale)]
        if len(bands) != 1:
            return GlyphReading(None, '', 0., 0., reason='no single text row')
        band = bands[0]
        top, bottom = int(band[0]), int(band[-1]+1)
        line = mask[top:bottom]
        xs = np.flatnonzero(line.any(axis=0))
        left, right = int(xs[0]), int(xs[-1]+1)
        line = line[:, left:right]
        if right-left > 640 or len(line) > 64:
            return GlyphReading(None, '', 0., 0., reason='text row exceeds bounded decoder size')
        mass = float(line.sum())
        candidates = {}
        # Fixed bounded bank; it is independent of name labels and corpus IDs.
        for factor in (1., .875, 1.125, 1.25):
            font_scale = round(scale*factor, 4)
            origins = sorted({-round(glyph.top*font_scale) for glyph in self.glyphs})
            for origin in origins:
                for path in self._decode_at(line, font_scale, origin):
                    if not path.text:
                        continue
                    score = max(0., 1-path.cost/max(1., mass+path.mass))
                    old = candidates.get(path.text)
                    if old is None or (score, path.minimum) > (old[0], old[1].minimum):
                        candidates[path.text] = (score, path)
            # Exact native pixels need no scale sweep. Retain competition
            # between native sequences, including identical-looking strings.
            if factor == 1. and candidates:
                native = sorted(candidates.items(), key=lambda item: item[1][0], reverse=True)
                best_name, (best_score, best_path) = native[0]
                gap = ((best_score-native[1][1][0])*max(len(best_name), len(native[1][0]))
                       if len(native) > 1 else best_score)
                if best_score >= 1-1e-6 and best_path.minimum >= 1-1e-6 and gap >= self.ambiguity_margin:
                    break
        ranked = sorted(candidates.items(), key=lambda item: (item[1][0], item[1][1].minimum), reverse=True)
        if not ranked:
            return GlyphReading(None, '', 0., 0., reason='no supported glyph sequence')
        candidate, (score, path) = ranked[0]
        # A global average difference shrinks with name length. Express the
        # runner-up margin per character-equivalent rather than rejecting long
        # names merely because one ambiguous glyph contributes a smaller mean.
        margin = (min(1., (score-ranked[1][1][0])*max(len(candidate), len(ranked[1][0])))
                  if len(ranked) > 1 else score)
        boxes = tuple((x+left, y+top, r+left, b+top) for x, y, r, b in path.boxes)
        reason = ''
        if score < self.min_score or path.minimum < self.min_glyph_score:
            reason = 'font or glyph pixels do not match strongly enough'
        elif margin < self.ambiguity_margin:
            reason = 'competing character sequences'
        return GlyphReading(candidate if not reason else None, candidate, score, margin, boxes, reason)

    def render(self, text: str, *, scale=1.0) -> np.ndarray:
        """Render synthetic diagnostic fixtures; never used to label real crops."""
        lookup = {g.character: g for g in self.glyphs}
        if any(c != ' ' and c not in lookup for c in text):
            raise ValueError('Text contains a character absent from the extracted atlas')
        width = 4+sum(self.space_advance if c == ' ' else lookup[c].advance for c in text)
        mask = np.zeros((self.line_height+4, width+2), np.uint8)
        x = 2
        for character in text:
            if character == ' ':
                x += self.space_advance
                continue
            g = lookup[character]
            y, l = 2+g.top, x+g.left
            h, w = g.pixels.shape
            mask[y:y+h, l:l+w] |= (g.pixels*192).astype(np.uint8)
            x += g.advance
        rgb = np.repeat(mask[:, :, None], 3, axis=2)
        if scale != 1:
            rgb = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        return rgb
