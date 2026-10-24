"""Opt-in, capture-only minimap coordinate monitoring. Never dispatches input."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path
import threading
import time
from typing import Callable

import cv2
import numpy as np

from core.paths import project_path
from core.perception import CaptureError, Frame, validate_region
from tibia_client.minimap_localization import AUTO_SCALES, DEFAULT_MIN_SCORE, MatchResult, MinimapLocator
from tibia_client.resource_monitor import create_capture
from tibia_client.capture import TransientCaptureError

DEFAULT_MAPS = Path(r"C:\SandBox\PaulistinhaOTLauncher\minimap-world")
STALE_AFTER = .75


@dataclass(frozen=True)
class CoordinateSettings:
    maps: Path
    box: tuple[int, int, int, int]
    scales: tuple[float, ...] = AUTO_SCALES
    floor: int | None = None
    use_center: bool = False
    min_score: float = DEFAULT_MIN_SCORE

    def __post_init__(self):
        if not math.isfinite(self.min_score) or not 0 <= self.min_score <= 1:
            raise ValueError('Minimum match must be between 0 and 100 percent')
        object.__setattr__(self, 'maps', Path(self.maps))
        object.__setattr__(self, 'box', tuple(self.box))
        object.__setattr__(self, 'scales', tuple(self.scales))
        if len(self.box) != 4 or any(type(v) is not int for v in self.box):
            raise ValueError('Minimap bounds must contain four integers')
        l, t, r, b = self.box
        if not (0 <= l < r and 0 <= t < b) or min(r-l, b-t) < 20:
            raise ValueError('Provide a valid minimap rectangle, at least 20 pixels wide/high')
        if not self.scales or any(not math.isfinite(s) or not .125 <= s <= 16 for s in self.scales):
            raise ValueError('Pixels per tile must be between 0.125 and 16')
        if self.floor is not None and (type(self.floor) is not int or not 0 <= self.floor <= 15):
            raise ValueError('Floor must be Auto or an integer from 0 to 15')


def find_crosshair(rgb: np.ndarray):
    """Locate a unique isolated light/dark plus, preserving its physical anchor.

    This is a shape check, not a claim that every client skin is supported.
    Ambiguous, clipped or hidden crosshairs return no player anchor.
    """
    candidates = []
    shapes = []
    # Fractional UI scaling and anti-aliasing shift/thin the visible arms.
    for width in (2, 3, 4):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                shape = np.zeros((12, 12), bool)
                start = (12-width)//2
                shape[:, start+dx:start+dx+width] = True
                shape[start+dy:start+dy+width, :] = True
                shapes.append(shape)
    for mask in (np.min(rgb, axis=2) >= 225, np.max(rgb, axis=2) <= 30):
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
        for n in range(1, count):
            x, y, w, h, area = map(int, stats[n])
            if not (4 <= w <= 18 and 4 <= h <= 18 and abs(w-h) <= 2):
                continue
            if x <= 0 or y <= 0 or x+w >= rgb.shape[1] or y+h >= rgb.shape[0]:
                continue
            if not .3 <= area/(w*h) <= .75:
                continue
            component = (labels[y:y+h, x:x+w] == n).astype(np.uint8)
            row, col = int(component.sum(1).argmax()), int(component.sum(0).argmax())
            if not (h*.25 <= row <= h*.75 and w*.25 <= col <= w*.75):
                continue
            if component[row].sum() < .85*w or component[:, col].sum() < .85*h:
                continue
            normalized = cv2.resize(component, (12, 12), interpolation=cv2.INTER_NEAREST).astype(bool)
            score = max(np.count_nonzero(normalized & shape) / np.count_nonzero(normalized | shape)
                        for shape in shapes)
            if score >= .65:
                candidates.append((x+w/2, y+h/2))
    if len(candidates) == 1:
        return candidates[0], 'Player crosshair'
    return None, 'Player crosshair is ambiguous' if candidates else 'Player crosshair not found'


@dataclass(frozen=True)
class CoordinateSample:
    frame: Frame
    box: tuple[int, int, int, int]
    anchor: tuple[float, float] | None
    result: MatchResult | None
    observed_at: float
    total_ms: float
    reference: str
    detail: str = ''


@dataclass(frozen=True)
class CoordinateState:
    phase: str = 'Disabled'
    running: bool = False
    sample: CoordinateSample | None = None
    detail: str = 'Enable coordinate prediction to begin.'


@dataclass(frozen=True)
class CoordinateDisplay:
    value: str
    status: str
    detail: str
    age_ms: float | None = None


def display_coordinate(state: CoordinateState, now: float) -> CoordinateDisplay:
    sample = state.sample
    if not state.running or sample is None:
        return CoordinateDisplay('X —   Y —   Z —', state.phase, state.detail)
    age = now-sample.observed_at
    if not math.isfinite(age) or not 0 <= age <= STALE_AFTER:
        return CoordinateDisplay('X —   Y —   Z —', 'Stale', 'Waiting for a recent capture.')
    result = sample.result
    if result is None or not result.accepted:
        return CoordinateDisplay('X —   Y —   Z —', 'Unknown',
                                 sample.detail or (result.reason if result else 'No match'), age*1000)
    status = ('Ambiguous' if result.reason == 'ambiguous' else
              'Map-center estimate' if sample.reference == 'Map center' else 'Predicted')
    note = 'Exact score tie; showing one candidate. ' if status == 'Ambiguous' else ''
    return CoordinateDisplay(f'X {result.x}   Y {result.y}   Z {result.z}', status,
                             f'{note}{sample.reference} · terrain agreement {result.score:.1%} · '
                             f'{result.pixels_per_tile:g} px/tile' +
                             (f' · ~{result.resolution_tiles}-tile sampling resolution'
                              if result.resolution_tiles > 1 else ''), age*1000)


class _Stopped(Exception):
    pass


class _Cancellation:
    def __init__(self):
        self.event = threading.Event()

    def checkpoint(self):
        if self.event.is_set():
            raise _Stopped()


def create_locator(settings: CoordinateSettings, checkpoint: Callable):
    if not (settings.maps/'manifest.json').is_file():
        raise ValueError('Select the concatenated minimap-world folder containing manifest.json')
    existing = settings.maps.parent/'minimap-localization'/'cache'
    cache = existing if existing.is_dir() else project_path('.cache/minimap-localization')
    locator = MinimapLocator(settings.maps, min_score=settings.min_score, cache_dir=cache, checkpoint=checkpoint)
    locator.prepare_scales(settings.scales)
    return locator


class CoordinateMonitor:
    """One worker, one retained frame, immutable UI snapshots, explicit enable.

    Each match searches all floors unless the operator supplies a floor hint.
    No stale position or floor is reused as a local-search prior.
    """
    def __init__(self, settings, *, capture_factory=create_capture, locator_factory=create_locator,
                 clock=time.monotonic, interval=.2):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError('Interval must be positive')
        self.settings = settings
        self._capture_factory = capture_factory
        self._locator_factory = locator_factory
        self._clock = clock
        self._interval = interval
        self._lock = threading.Lock()
        self._state = CoordinateState()
        self._worker = None
        self._token = _Cancellation()
        self._closed = False

    @property
    def busy(self):
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    def state(self):
        with self._lock:
            return self._state

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError('Coordinate monitor is closed')
            if self._worker is not None and self._worker.is_alive():
                return False
            self._token = _Cancellation()
            self._state = CoordinateState('Loading maps', True,
                                          detail='Preparing the map index; the first build may take a few minutes.')
            self._worker = threading.Thread(target=self._run, args=(self._token,),
                                            name='coordinate-monitor', daemon=True)
            self._worker.start()
            return True

    def stop(self):
        with self._lock:
            self._token.event.set()
            self._state = CoordinateState(detail='Coordinate prediction is disabled.')

    def close(self):
        with self._lock:
            self._closed = True
        self.stop()

    def wait(self, timeout=3.):
        with self._lock:
            worker = self._worker
        if worker is not None:
            worker.join(timeout)
        return not self.busy

    def _publish(self, token, **changes):
        with self._lock:
            if token is self._token and not token.event.is_set() and not self._closed:
                self._state = replace(self._state, **changes)

    def _run(self, token):
        capture = None
        try:
            locator = self._locator_factory(self.settings, token.checkpoint)
            token.checkpoint()
            capture = self._capture_factory()
            expected_size = None
            while not token.event.is_set():
                begun = self._clock()
                self._publish(token, phase='Capturing', detail='Capturing the selected window.')
                try:
                    frame = capture.capture(token=token)
                except TransientCaptureError as exc:
                    token.checkpoint()
                    self._publish(token, phase='Waiting for capture', sample=None, detail=str(exc))
                    # CaptureProcess has already reaped the failed worker.
                    # Retry the next fresh frame without rebuilding the atlas.
                    if token.event.wait(.25):
                        break
                    continue
                captured = self._clock()
                token.checkpoint()
                if not isinstance(frame, Frame):
                    raise CaptureError('Capture did not return an RGB Frame')
                if expected_size is not None and frame.size != expected_size:
                    raise CaptureError('Capture size changed. Disable and recalibrate the minimap bounds.')
                expected_size = frame.size
                box = validate_region(self.settings.box, frame.size)
                observed_at = getattr(capture, 'last_observed_at', None)
                if observed_at is None:
                    observed_at = begun
                if not math.isfinite(observed_at) or not begun <= observed_at <= captured:
                    raise CaptureError('Invalid capture timestamp')
                l, t, r, b = box
                crop = frame.pixels[t:b, l:r]
                player_anchor, player_reference = find_crosshair(crop)
                if self.settings.use_center or player_anchor is None:
                    anchor, reference = ((r-l-1)/2, (b-t-1)/2), 'Map center'
                else:
                    anchor, reference = player_anchor, player_reference
                # A viewport center can fall between tile centers at fractional
                # zoom. Align sampling to the crosshair, then translate the
                # accepted coordinate back to the requested map-center anchor.
                sampling_anchor = player_anchor or anchor
                if sampling_anchor is not None and min(
                        sampling_anchor[0], sampling_anchor[1],
                        crop.shape[1]-1-sampling_anchor[0],
                        crop.shape[0]-1-sampling_anchor[1]) < 8:
                    sampling_anchor = anchor
                token.checkpoint()
                result, detail = None, ''
                if min(anchor[0], anchor[1], crop.shape[1]-1-anchor[0], crop.shape[0]-1-anchor[1]) < 8:
                    detail = 'Player crosshair is too near the minimap edge; center the minimap.'
                else:
                    self._publish(token, phase='Matching', detail='Matching minimap terrain.')
                    result = locator.locate(crop, scales=self.settings.scales, anchor=sampling_anchor,
                                            floor=self.settings.floor, method='index')
                    if result.accepted and sampling_anchor != anchor:
                        result = replace(result,
                            x=result.x + math.floor((anchor[0]-sampling_anchor[0])/result.pixels_per_tile+.5),
                            y=result.y + math.floor((anchor[1]-sampling_anchor[1])/result.pixels_per_tile+.5))
                    token.checkpoint()
                    # The global index is bounded enough for live display. Slow
                    # exhaustive fallback is deliberately excluded from this UI lane.
                sample = CoordinateSample(frame, box, anchor, result, observed_at,
                                          (self._clock()-begun)*1000, reference, detail)
                self._publish(token, phase='Monitoring', sample=sample, detail='')
                token.event.wait(max(0., self._interval-(self._clock()-begun)))
        except _Stopped:
            pass
        except Exception as exc:
            self._publish(token, phase='Error', running=False, sample=None, detail=str(exc))
        finally:
            if capture is not None:
                try:
                    capture.close()
                except Exception as exc:
                    with self._lock:
                        self._state = CoordinateState('Error', detail=f'Capture cleanup failed: {exc}')
