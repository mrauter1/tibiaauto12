"""Observation-only visual diagnostics from the monitor's existing frame."""
from __future__ import annotations

from collections import Counter, OrderedDict
import hashlib
import json
import math
import threading
import time

import cv2

from conf.MarksConf import NameMarks
from core.paths import scripts_path
from core.perception import load_template, locate_match, locate_matches, validate_region
from engine.ScanStages import read_bar_region
from tibia_client.battle_text import BattleTextReader, name_key, read_target
from tibia_client.models import Marker, Quality, Reading


def read_marker(frame, region, name, scale=1.0):
    """One response map supplies count, click and center-overlap evidence."""
    found = locate_matches(f"images/MapSettings/{name}.png", frame=frame, region=region,
                           precision=.7, scale=scale)
    arrival = max(found, key=lambda m: m.score, default=None)
    clicks = [m for m in found if m.score >= .8]
    best = max(clicks, key=lambda m: m.score, default=None)
    width, height = region[2]-region[0], region[3]-region[1]
    reached = bool(arrival and abs(arrival.center[0]-width/2) <= math.ceil(arrival.width/2)
                   and abs(arrival.center[1]-height/2) <= math.ceil(arrival.height/2))
    positions = [(region[0]+m.center[0], region[1]+m.center[1]) for m in clicks]
    position = None if best is None else (region[0]+best.center[0], region[1]+best.center[1])
    return Reading(Marker(position, reached), diagnostics={
        "reader": "marker-template", "effective_region": region, "positions": positions,
        "ui_scale": scale,
        "score": None if arrival is None else arrival.score, "count": len(clicks),
        "boxes": [(region[0]+m.x, region[1]+m.y, region[0]+m.x+m.width, region[1]+m.y+m.height)
                  for m in clicks]})


class MarkerReader:
    """Retain a minimap-specific scale; learn it from colored marker evidence."""
    def __init__(self):
        self._context = None
        self._scale = None
        self._anchors = ()
        self._candidate = None
        self._worker = None
        self._closed = False
        self._last_scan = None
        self._last_started = float('-inf')
        self._cache = OrderedDict()
        self._templates = {}
        self._frame = self._fingerprint = None
        self._error = ''

    def read(self, frame, region, name, configured_scale=1.0):
        region = tuple(validate_region(region, frame.size))
        context = (frame.size, region, configured_scale)
        if context != self._context:
            self._context, self._scale, self._anchors = context, None, ()
            self._cache.clear()
            self._frame = None
        candidate = self._candidate
        if candidate is not None:
            self._candidate = None
            if candidate[0] == context:
                _, self._scale, self._anchors = candidate
                self._cache.clear()
        scale = self._scale if self._scale is not None else configured_scale
        if frame is not self._frame:
            l, t, r, b = region
            self._fingerprint = hashlib.blake2b(frame.pixels[t:b, l:r].tobytes(), digest_size=16).digest()
            self._frame = frame
        key = (context, scale, self._fingerprint)
        cached = self._cache.setdefault(key, {})
        self._cache.move_to_end(key)
        while len(self._cache) > 4:
            self._cache.popitem(last=False)

        def read(name):
            if name not in cached:
                cached[name] = read_marker(frame, region, name, scale)
            return cached[name]

        result = read(name)
        # An absent waypoint is normal. Keep the scale while another previously
        # recognized marker still confirms it, instead of rescanning all scales.
        confirmed = result.value.position is not None
        if not confirmed and self._scale is not None:
            confirmed = any(read(anchor).value.position is not None for anchor in self._anchors)
        scan_key = (context, self._fingerprint)
        if (not self._closed and (self._scale is None or not confirmed)
                and (self._worker is None or not self._worker.is_alive())
                and scan_key != self._last_scan and time.monotonic()-self._last_started >= 1):
            self._last_scan, self._last_started = scan_key, time.monotonic()
            l, t, r, b = region
            pixels = frame.pixels[t:b, l:r].copy()
            self._worker = threading.Thread(target=self._discover,
                args=(pixels, context), name='marker-scale', daemon=True)
            self._worker.start()
        return Reading(result.value, result.quality, result.detail, {
            **result.diagnostics, 'configured_scale': configured_scale,
            'scale_source': 'detected' if self._scale is not None else 'configured',
            'scale_anchors': self._anchors, 'scale_search_error': self._error,
        })

    def _discover(self, pixels, context):
        configured_scale = context[2]
        scales = sorted({configured_scale, *(n/4 for n in range(2, 13))},
                        key=lambda scale: abs(scale-configured_scale))
        best = None
        try:
            for scale in scales:
                ranked = []
                for name in NameMarks:
                    if self._closed:
                        return
                    key = (name, scale)
                    if key not in self._templates:
                        template = load_template(f'images/MapSettings/{name}.png')
                        self._templates[key] = cv2.resize(template, None, fx=scale, fy=scale,
                                                         interpolation=cv2.INTER_LINEAR)
                    template = self._templates[key]
                    h, w = template.shape[:2]
                    if h > pixels.shape[0] or w > pixels.shape[1]:
                        continue
                    # RGB rejects tiny grayscale coincidences on map terrain.
                    scores = cv2.matchTemplate(pixels, template, cv2.TM_CCOEFF_NORMED)
                    _, score, _, (x, y) = cv2.minMaxLoc(scores)
                    if score >= .8:
                        ranked.append((score, name, x+w/2, y+h/2, w, h))
                distinct = []
                for match in sorted(ranked, reverse=True):
                    if all(abs(match[2]-old[2]) > max(match[4], old[4])*.65 or
                           abs(match[3]-old[3]) > max(match[5], old[5])*.65 for old in distinct):
                        distinct.append(match)
                    if len(distinct) == 2:
                        break
                if not distinct:
                    continue
                confidence = sum(m[0] for m in distinct)/len(distinct)
                # One exceptionally clear icon or agreement between two icons.
                threshold = .93 if len(distinct) == 1 else .85
                if confidence >= threshold and (best is None or confidence > best[0]):
                    best = (confidence, scale, tuple(m[1] for m in distinct))
                if len(distinct) == 2 and min(m[0] for m in distinct) >= .93:
                    break
            self._error = ''
            if best is not None and not self._closed:
                self._candidate = (context, best[1], best[2])
        except Exception as exc:
            self._error = f'{type(exc).__name__}: {exc}'

    def close(self):
        self._closed = True
        self._candidate = self._frame = None
        self._cache.clear()


def load_visual_config():
    with scripts_path('conf.json').open(encoding='utf-8') as stream:
        config = json.load(stream)
    with scripts_path(config['preferences_name']).open(encoding='utf-8') as stream:
        profile = json.load(stream)
    with scripts_path('GUILoader.json').open(encoding='utf-8') as stream:
        cave = json.load(stream).get('Loaders', {}).get('CaveBotLoader', {})
    targets = [cave.get('SelectedMonster'+suffix, '') for suffix, enabled in
               [('', 'AttackOne'), ('2', 'AttackTwo'), ('3', 'AttackThree'), ('4', 'AttackFour')]
               if cave.get(enabled)]
    return profile, tuple(dict.fromkeys(n for n in targets if n))


class VisualReadings:
    """Uses calibrated regions; contains no capture or input adapter."""
    def __init__(self, profile=None, targets=None):
        self.error = ''
        if profile is None:
            try:
                profile, targets = load_visual_config()
            except Exception as exc:
                profile, targets, self.error = {}, (), f'Calibration unavailable: {exc}'
        self.profile = profile
        self.targets = targets or ()
        self.scale = float(profile.get('ui_scale', 1))
        if not math.isfinite(self.scale) or not .5 <= self.scale <= 3:
            raise ValueError('Calibration ui_scale must be between 0.5 and 3')
        self.battle = BattleTextReader()
        from tibia_client.follow_controls import FollowControls
        self.follow = FollowControls()
        self.markers = MarkerReader()

    def close(self):
        self.battle.close()
        self.follow.close()
        self.markers.close()

    def _region(self, key, frame):
        size = self.profile.get('calibration_size')
        if size and tuple(size) != frame.size:
            raise ValueError(f'Calibration size {tuple(size)} differs from capture {frame.size}')
        if key in ('HealthBar', 'ManaBar'):
            box = self.profile.get('ResourceBars', {}).get(key)
        else:
            rows = self.profile.get('Boxes', {}).get(key, [])
            box = tuple(rows[0][k] for k in ('x', 'y', 'w', 'h')) if rows else None
        if not box:
            raise ValueError(self.error or f'{key} is not calibrated; use Image calibration')
        return validate_region(box, frame.size)

    def read(self, frame):
        out = {}

        def put(key, label, group, region_key, callback):
            region = None
            try:
                region = self._region(region_key, frame)
                reading = callback(region)
            except ValueError as exc:
                reading = Reading(None, Quality.UNKNOWN, str(exc))
            except Exception as exc:
                reading = Reading(None, Quality.ERROR, f'{type(exc).__name__}: {exc}')
            out[key] = Reading(reading.value, reading.quality, reading.detail,
                               {**reading.diagnostics, 'label': label, 'group': group,
                                'effective_region': region})
            return reading, region

        for key, kind, label in [('HealthBar', 'health', 'Health bar'), ('ManaBar', 'mana', 'Mana bar')]:
            def bar(region, kind=kind):
                value = read_bar_region(frame, region, kind)
                return Reading(value, Quality.KNOWN if value is not None else Quality.UNKNOWN,
                               '' if value is not None else 'Calibrated bar is unreadable',
                               {'format': 'percent', 'reader': 'bar-fill'})
            put('bar:'+kind, label, 'Resources', key, bar)

        battle, region = put('battle', 'Battle list', 'Creatures', 'BattleBox',
                            lambda box: self.battle.read_all(frame, box, self.scale))
        rows = battle.value or ()
        if battle.quality is Quality.KNOWN:
            counts = Counter(row['name'] for row in rows)
            summary = ', '.join(f'{n} × {count}' for n, count in counts.items()) or 'No readable names'
            out['battle'] = Reading(f'{len(rows)} rows', detail=summary,
                                   diagnostics=out['battle'].diagnostics)
        for index, row in enumerate(rows):
            # Individual names can be read even when the aggregate count is
            # unknown because another row is clipped or unreadable.
            quality = Quality.KNOWN if row['complete'] else Quality.UNKNOWN
            detail = ('Selected attack target' if row['selected'] else 'Not selected')
            if not row['complete']:
                detail += ' · ambiguous name row' if row.get('ambiguous') else ' · clipped name'
            if battle.detail:
                detail += ' · '+battle.detail
            center_y = row['position'][1]
            crop = (region[0], max(region[1], center_y-round(16*self.scale)),
                    region[2], min(region[3], center_y+round(16*self.scale)))
            out[f'creature:{index}'] = Reading(row['name'], quality, detail,
                {'label': f'Row {index+1}', 'group': 'Creatures', 'effective_region': crop,
                 'boxes': [row['box']],
                 'position': row['position'], 'selected': row['selected'], 'reader': 'windows-ocr',
                 'name_complete': row['complete'], 'ambiguous': row.get('ambiguous', False),
                 'raw_names': battle.diagnostics.get('raw_names', ()), 'recognized_name': row['name']})
        for index, (a, b) in enumerate(battle.diagnostics.get('unreadable_rows', ())):
            out[f'unreadable:{index}'] = Reading(None, Quality.UNKNOWN, battle.detail,
                {'label': f'Unverified text band {index+1}', 'group': 'Creatures', 'reader': 'windows-ocr',
                 'effective_region': (region[0], region[1]+a, region[2], region[1]+b)})
        for name in self.targets:
            try:
                reading = (read_target(frame, region, name, self.scale, self.battle) if region else
                           Reading(None, Quality.UNKNOWN, battle.detail))
            except Exception as exc:
                reading = Reading(None, Quality.ERROR, f'{type(exc).__name__}: {exc}')
            out['target:'+name_key(name)] = Reading(reading.value, reading.quality, reading.detail,
                {**reading.diagnostics, 'label': name, 'group': 'Targets', 'effective_region': region})

        put('minimap', 'Minimap region', 'Map', 'MapBox',
            lambda box: Reading(f'{box[2]-box[0]} × {box[3]-box[1]} px',
                                detail='Calibrated map region; location is not rediscovered each frame',
                                diagnostics={'reader': 'calibration'}))
        for name in NameMarks:
            def marker(box, name=name):
                result = self.markers.read(frame, box, name, self.scale)
                detail = ('Center overlap at threshold 0.70' if result.value.reached else 'Away from map center')
                if not result.diagnostics['count']:
                    detail = 'No click match above 0.80'+(' · center candidate at 0.70' if result.value.reached else '')
                detail += f" · scale {result.diagnostics['ui_scale']:g}× ({result.diagnostics['scale_source']})"
                return Reading(result.value, detail=detail, diagnostics=result.diagnostics)
            put('marker:'+name, name, 'Markers', 'MapBox', marker)

        for key, label, box_key, template in [
            ('hasted', 'Haste', 'StatusBarBox', 'Hur'),
            ('hungry', 'Hungry', 'StatusBarBox', 'Starving'),
            ('pz', 'Protection zone', 'StatusBarBox', 'PZ'),
            ('ring_empty', 'Ring slot empty', 'RingBox', 'NoRing'),
            ('amulet_empty', 'Amulet slot empty', 'AmuletBox', 'NoAmulet'),
        ]:
            def present(box, template=template):
                match = locate_match(f'images/PlayerStats/{template}.png', frame=frame,
                                     region=box, precision=.9, scale=self.scale)
                return Reading(match is not None, detail='Template presence at threshold 0.90',
                               diagnostics={'reader': 'template', 'ui_scale': self.scale,
                                            'score': None if match is None else match.score})
            put('status:'+key, label, 'Status / equipment', box_key, present)
        follow = self.follow.read(frame, self.scale)
        out['status:follow'] = Reading(
            ('Following' if follow.value else 'Standing') if follow.quality is Quality.KNOWN else None,
            follow.quality, follow.detail,
            {**follow.diagnostics, 'label': 'Follow mode', 'group': 'Status / equipment'})
        return out
