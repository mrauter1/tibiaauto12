"""Local battle-list text recognition for freely entered creature names.

OCR runs in one background lane. Its result is usable only when the current
frame has the same name pixels; old row coordinates are never reused after a
name changes or moves. Health bars and selection borders are read separately.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from fnmatch import fnmatchcase
from functools import lru_cache
import hashlib
import os
import threading
import time

import cv2
import numpy as np

from core.paths import project_path
from tibia_client.models import Quality, Reading, Target
from core.perception import validate_region


def name_key(name: str) -> str:
    """Ignore case/spacing while retaining punctuation in creature names."""
    return "".join(name.casefold().split())


@lru_cache(maxsize=128)
def bundled_target(name: str):
    # Resolve through an inventory, never interpret entered names as paths.
    # Patterns use the shared OCR rows, not a scan of every matching template.
    if "*" in name or "?" in name:
        return None
    key = name_key(name)
    for path in project_path("images/Targets/Names").glob("*.png"):
        if name_key(path.stem) == key:
            return path
    return None


def read_target(frame, region, name, scale, reader):
    """The bot and Readings use the same target path and thresholds."""
    from core.HookWindow import LocateMatch, LocateMatches
    from engine.CaveBot import Scanners

    if not name.strip():
        raise ValueError('target sensor needs a monster name')
    text = reader.read(frame, region, name, scale)
    if text.quality is Quality.KNOWN and isinstance(text.value, Target) and text.value.count > 0:
        return text
    asset = bundled_target(name)
    if asset is None:
        return text
    matches = LocateMatches(asset, Region=region, Precision=.8, frame=frame, Scale=scale)
    best = LocateMatch(asset, Region=region, Precision=.86, frame=frame, Scale=scale)
    if best is None:
        # A template miss must not override an unfinished/unreadable OCR row
        # with a false zero. Both views use this same decision path.
        return text
    position = (region[0]+best.center[0], region[1]+best.center[1])
    selected = bool(Scanners.target_selected(region, frame=frame, target=asset.stem, scale=scale))
    return Reading(Target(len(matches), position, selected, count_complete=False),
                   detail='Template-confirmed target; exact OCR count unavailable', diagnostics={
        'reader': 'bundled-template', 'effective_region': region, 'capture_position': position,
        'count_complete': False, 'ocr_detail': text.detail, 'ui_scale': scale,
        'count_for_loot': len(matches),
        'score': None if best is None else best.score,
        'boxes': [(region[0]+m.x, region[1]+m.y, region[0]+m.x+m.width, region[1]+m.y+m.height)
                  for m in matches]})


def join_rows(lines):
    """Reconstruct names when OCR treats repeated words as separate columns."""
    rows = []
    for label, box in sorted(lines, key=lambda item: (item[1][1], item[1][0])):
        x1, y1, x2, y2 = box
        height = y2-y1
        candidates = []
        for index, (_, other) in enumerate(rows):
            rx1, ry1, rx2, ry2 = other
            rh = ry2-ry1
            overlap = min(y2, ry2)-max(y1, ry1)
            gap = max(x1-rx2, rx1-x2, 0)
            if (min(height, rh) > 0 and max(height, rh) <= 2*min(height, rh)
                    and overlap >= .6*min(height, rh)
                    and abs((y1+y2)-(ry1+ry2))/2 <= .45*max(height, rh)
                    and gap <= 3*max(height, rh)):
                candidates.append(index)
        if len(candidates) != 1:
            rows.append(([(label, box)], box))
            continue
        index = candidates[0]
        parts, other = rows[index]
        parts.append((label, box))
        rows[index] = (parts, (min(other[0], x1), min(other[1], y1),
                              max(other[2], x2), max(other[3], y2)))
    return tuple((" ".join(p[0] for p in sorted(parts, key=lambda p: p[1][0])), box)
                 for parts, box in rows)


def _same_baseline(first, second):
    """Two OCR boxes compete for one text row only when their baselines overlap."""
    height = min(first[3]-first[1], second[3]-second[1])
    return (height > 0
            and min(first[3], second[3])-max(first[1], second[1]) >= .6*height
            and abs((first[1]+first[3])-(second[1]+second[3]))/2 <= .45*height)


def _text_bands(mask):
    """Find glyph-height ink strips; isolated specks are not name rows."""
    ys = np.flatnonzero(mask.sum(axis=1) >= 2)
    groups = np.split(ys, np.flatnonzero(np.diff(ys) > 2)+1)
    return [(int(g[0]), int(g[-1])+1) for g in groups
            if len(g) >= 4 and mask[g[0]:g[-1]+1].sum() >= 15]


async def _windows_lines(mask: np.ndarray):
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    language = Language("en-US")
    engine = (OcrEngine.try_create_from_language(language)
              if OcrEngine.is_language_supported(language) else
              OcrEngine.try_create_from_user_profile_languages())
    if engine is None:
        raise RuntimeError("Windows OCR has no installed recognition language")
    enlarged = cv2.resize(np.where(mask, 0, 255).astype(np.uint8), None,
                          fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
    bgra = cv2.cvtColor(enlarged, cv2.COLOR_GRAY2BGRA)
    if max(bgra.shape[:2]) > OcrEngine.max_image_dimension:
        raise ValueError("battle-list crop exceeds the Windows OCR size limit")
    with DataWriter() as writer:
        writer.write_bytes(bgra.tobytes())
        with SoftwareBitmap.create_copy_from_buffer(
            writer.detach_buffer(), BitmapPixelFormat.BGRA8, bgra.shape[1], bgra.shape[0]
        ) as bitmap:
            result = await engine.recognize_async(bitmap)
            lines = []
            for line in result.lines:
                words = tuple(line.words)
                if not words:
                    continue
                boxes = [word.bounding_rect for word in words]
                left = min(box.x for box in boxes) / 3
                top = min(box.y for box in boxes) / 3
                right = max(box.x + box.width for box in boxes) / 3
                bottom = max(box.y + box.height for box in boxes) / 3
                lines.append((line.text, (left, top, right, bottom)))
            return tuple(lines)


def _read_lines(mask):
    if os.name != "nt":
        raise RuntimeError("custom creature names require Windows OCR")
    from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
    init_apartment(ApartmentType.MULTI_THREADED)
    try:
        return asyncio.run(_windows_lines(mask))
    finally:
        uninit_apartment()


class BattleTextReader:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = OrderedDict()
        self._worker = None
        self._closed = False
        self._prepared = None

    def _recognize(self, key, mask):
        try:
            raw = _read_lines(mask)
            lines, error = join_rows(raw), ""
            # Retry only missing or ambiguously split text strips. No
            # extra OCR for correctly assembled names or unchanged pixels.
            bands = _text_bands(mask)
            missing = [(a, b) for a, b in bands if sum(
                a-2 <= (box[1]+box[3])/2 <= b+2 for _, box in lines) != 1]
            clipped = [(a, b) for a, b in missing if a == 0 or b == len(mask)]
            retried, unresolved = 0, []
            deadline = time.monotonic()+.12
            for a, b in missing:
                covered = [box for _, box in lines if a-2 <= (box[1]+box[3])/2 <= b+2]
                if len(covered) > 1 and any(
                        not _same_baseline(box, other) for index, box in enumerate(covered)
                        for other in covered[index+1:]):
                    # Neutral borders can connect several real name rows into
                    # one pixel band. Preserve their OCR boxes; a second OCR
                    # pass on the broad band cannot establish a single row.
                    unresolved.append((a, b))
                    continue
                # Missing glyph pixels cannot be recovered by repeating OCR.
                if (a, b) in clipped:
                    unresolved.append((a, b))
                    continue
                if retried >= 4 or time.monotonic() >= deadline:
                    unresolved.append((a, b))
                    continue
                top, bottom = max(0, a-2), min(len(mask), b+2)
                extra = join_rows(_read_lines(np.pad(mask[top:bottom], ((4, 4), (4, 4)))))
                retried += 1
                if len(extra) != 1:
                    unresolved.append((a, b))
                else:
                    lines = tuple((name, box) for name, box in lines
                                  if not a-2 <= (box[1]+box[3])/2 <= b+2)
                    lines += tuple((name, (box[0]-4, box[1]+top-4, box[2]-4, box[3]+top-4))
                                   for name, box in extra)
            if not lines:
                error = "battle-list text was visible but could not be read"
            elif unresolved:
                error = (f"{len(unresolved)} battle-list text bands could not be fully verified"
                         + ('; name clipped at crop boundary — adjust BattleBox' if clipped else ''))
            diagnostics = {"raw_names": [name for name, _ in raw],
                           "raw_boxes": [box for _, box in raw],
                           "row_retries": retried, "unreadable_rows": unresolved, "clipped_rows": clipped,
                           "retryable": False}
            lines = tuple(sorted(lines, key=lambda item: (item[1][1], item[1][0])))
        except Exception as exc:
            lines, error = (), f"battle-list OCR: {type(exc).__name__}: {exc}"
            diagnostics = {"retryable": True}
        with self._lock:
            if not self._closed:
                self._cache[key] = (lines, error, time.monotonic(), diagnostics)
                self._cache.move_to_end(key)
                while len(self._cache) > 32:
                    self._cache.popitem(last=False)

    @staticmethod
    def _selected(pixels, center_y, scale):
        # A target name must have a red/pink selection rectangle around the
        # adjacent creature icon in this same, current battle-list row.
        half = round(16 * scale)
        y = round(center_y)
        icon = pixels[max(0, y-half):min(len(pixels), y+half), :round(23*scale)]
        if not icon.size:
            return False
        rgb = icon.astype(np.int16)
        red = (rgb[:, :, 0] > 130) & (rgb[:, :, 1] < 100)
        minimum = max(9, round(12*scale))
        return bool((red.sum(axis=0) >= minimum).sum() >= 2
                    and (red.sum(axis=1) >= minimum).sum() >= 2)

    def read_all(self, frame, region, scale=1.0):
        """Read every visible name once; expose row evidence to debug viewers."""
        left, top, right, bottom = validate_region(region, frame.size)
        if (right-left)*(bottom-top) > 500_000:
            return Reading(None, Quality.UNKNOWN, "battle-list text region is too large")
        pixels = frame.pixels[top:bottom, left:right]
        # Exclude the animated creature icons. Bright neutral pixels contain
        # the names; coloured health bars/selection borders cannot enter OCR.
        inset = max(1, round(20*scale))
        text = pixels[:, inset:]
        if text.size == 0:
            return Reading(None, Quality.UNKNOWN, "battle-list name area is too narrow")
        prepared = self._prepared
        geometry = (left, top, right, bottom, scale)
        if prepared is not None and prepared[0] is frame and prepared[1] == geometry:
            mask, key = prepared[2:]
        else:
            values = text.astype(np.int16)
            mask = (values.min(axis=2) >= 100) & (np.ptp(values, axis=2) < 40)
            key = (mask.shape, hashlib.blake2b(mask.tobytes(), digest_size=16).digest())
            self._prepared = (frame, geometry, mask, key)
        diagnostics = {"reader": "windows-ocr", "effective_region": region, "ui_scale": scale}
        if not mask.any():
            return Reading((), diagnostics=diagnostics)
        with self._lock:
            cached = self._cache.get(key)
            retry = cached is None or (cached[3].get('retryable') and time.monotonic()-cached[2] > 1)
            if retry and not self._closed and (self._worker is None or not self._worker.is_alive()):
                self._worker = threading.Thread(
                    target=self._recognize, args=(key, mask.copy()),
                    name="battle-text", daemon=True)
                self._worker.start()
        if cached is None:
            return Reading(None, Quality.UNKNOWN, "reading battle-list creature names", diagnostics)
        lines, error, _, extra = cached
        diagnostics.update(extra)
        rows = []
        for index, (label, box) in enumerate(lines):
            absolute = (max(left+inset, left+inset+round(box[0])), max(top, top+round(box[1])),
                        min(right, left+inset+round(box[2])), min(bottom, top+round(box[3])))
            if absolute[2] <= absolute[0] or absolute[3] <= absolute[1]:
                continue
            position = (left+inset+round((box[0]+box[2])/2), top+round((box[1]+box[3])/2))
            # A failed band-completeness check does not invalidate a distinct
            # complete name. Only competing OCR boxes on this baseline do.
            ambiguous = any(_same_baseline(box, other) for other_index, (_, other) in enumerate(lines)
                            if other_index != index)
            rows.append({"name": label, "box": absolute, "position": position,
                         "selected": self._selected(pixels, (box[1]+box[3])/2, scale),
                         "ambiguous": ambiguous,
                         "complete": not ambiguous and not label.endswith(("...", "…"))
                                     and box[1] > 0 and box[3] < len(mask)})
        diagnostics["names"] = [row["name"] for row in rows]
        diagnostics['boxes'] = [row['box'] for row in rows]
        diagnostics['recognized_rows'] = [
            {key: row[key] for key in ('name', 'box', 'complete', 'ambiguous')} for row in rows]
        return Reading(tuple(rows), Quality.UNKNOWN if error else Quality.KNOWN, error, diagnostics)

    def read(self, frame, region, name, scale):
        reading = self.read_all(frame, region, scale)
        wildcard = "*" in name or "?" in name
        diagnostics = {**reading.diagnostics, 'target_pattern': name,
                       'match_mode': 'wildcard' if wildcard else 'exact'}
        if reading.value is None or reading.diagnostics.get('retryable'):
            return Reading(None, reading.quality, reading.detail, diagnostics)
        rows = reading.value
        # CaveBot uses recognized counts directly, including partial counts.
        # A completed read with no matching names means absent by policy, even
        # when other text bands are uncertain. Pending/failed OCR is not zero.
        # Preserve the existing case/spacing normalization. Only * and ? are
        # operators: quote brackets so punctuation in exact names stays literal.
        # fnmatchcase caches compiled patterns; this adds no OCR work.
        pattern = name_key(name).replace('[', '[[]')
        complete = reading.quality is Quality.KNOWN and all(row['complete'] for row in rows)
        matches = [row for row in rows
                   if row['complete'] and fnmatchcase(name_key(row['name']), pattern)]
        diagnostics['matched_names'] = [row['name'] for row in matches]
        if not matches:
            return Reading(Target(count=0, position=None, selected=False, count_complete=complete),
                           detail='' if complete else 'No matching names recognized; treated as absent for CaveBot',
                           diagnostics={**diagnostics, 'count_complete': complete,
                                        'count_for_loot': 0, 'absence_assumed': not complete})
        selected = [row for row in matches if row['selected']]
        row = (selected or matches)[0]
        details = (['Matched: '+', '.join(dict.fromkeys(diagnostics['matched_names']))]
                   if wildcard else [])
        if not complete:
            details.append(f'Partial count; CaveBot uses {len(matches)} for loot')
        return Reading(Target(len(matches), row['position'], bool(selected), complete),
                       detail=' · '.join(details),
                       diagnostics={**diagnostics, "matched_name": row['name'],
                                    'capture_position': row['position'], 'selected': bool(selected),
                                    'count_complete': complete, 'count_for_loot': len(matches),
                                    'boxes': [match['box'] for match in matches]})

    def close(self):
        with self._lock:
            self._closed = True
            self._cache.clear()
            self._prepared = None
