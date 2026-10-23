"""Recognize the adjacent stand/chase buttons and retain their local geometry."""
from __future__ import annotations

import cv2
import threading

from core.perception import Match, load_template
from tibia_client.models import Quality, Reading


class FollowControls:
    def __init__(self):
        self._layout = None
        self._frame = None
        self._reading = None
        self._observation_cycle = None
        self._configured_scale = None
        self._templates = {}
        self._scaled = {}
        self._worker = None
        self._candidate = None
        self._closed = False

    def _locate(self, frame, gray, scale, region=None, remembered=None):
        matches = {}
        search = gray if region is None else cv2.cvtColor(
            frame.pixels[region[1]:region[3], region[0]:region[2]], cv2.COLOR_RGB2GRAY)
        for name in ("Idle", "NotIdle", "Following", "NotFollow"):
            if name not in self._templates:
                self._templates[name] = load_template(f"images/TibiaSettings/{name}.png")
            key = name, scale
            if key not in self._scaled:
                template = self._templates[name]
                if scale != 1:
                    template = cv2.resize(template, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
                self._scaled[key] = cv2.cvtColor(template, cv2.COLOR_RGB2GRAY)
            template = self._scaled[key]
            if template.shape[0] > search.shape[0] or template.shape[1] > search.shape[1]:
                continue
            scores = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, position = cv2.minMaxLoc(scores)
            if score >= .7:
                found = Match(*position, template.shape[1], template.shape[0], float(score))
                matches[name] = found.translated(region[0], region[1]) if region else found
        stand = max((matches[n] for n in ("Idle", "NotIdle") if n in matches),
                    key=lambda m: m.score, default=None)
        chase = max((matches[n] for n in ("Following", "NotFollow") if n in matches),
                    key=lambda m: m.score, default=None)
        if remembered:
            # A hidden stand glyph must not be replaced by the similar chase
            # glyph below it. Validate each match against its retained position.
            def aligned(match, name):
                point = remembered.get(name+'_position')
                if match and name == 'stand' and point is None:
                    follow = remembered['follow_position']
                    return match if (abs(match.center[0]-follow[0]) <= match.width*.3 and
                                     .6*match.height <= follow[1]-match.center[1] <= 1.5*match.height) else None
                return match if match and point and (
                    abs(match.center[0]-point[0]) <= match.width*.4 and
                    abs(match.center[1]-point[1]) <= match.height*.4) else None
            stand, chase = aligned(stand, 'stand'), aligned(chase, 'follow')
        else:
            pair = False
            if stand is not None and chase is not None:
                width, height = max(stand.width, chase.width), max(stand.height, chase.height)
                dx, dy = chase.center[0]-stand.center[0], chase.center[1]-stand.center[1]
                pair = (abs(dx) <= .3*width and .6*height <= dy <= 1.5*height
                        and (stand.score+chase.score)/2 >= .8)
            if not pair:
                # A clear follow button is sufficient to enable follow, even
                # when its neighboring stand button is covered by a tooltip.
                if chase is None or chase.score < .85:
                    return None
                stand = None

        def coloured(match, following):
            if match is None:
                return False
            pixels = frame.pixels[match.y:match.y+match.height,
                                  match.x:match.x+match.width].astype("int16")
            red, green, blue = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
            mask = ((green > 100) & (green > red+35) & (green > blue+25) if following else
                    (red > 100) & (red > green+35) & (red > blue+35))
            return int(mask.sum()) >= max(3, round(5*scale*scale))

        standing, following = coloured(stand, False), coloured(chase, True)
        known = standing != following
        boxes = [m.box for m in (stand, chase) if m is not None]
        if region is None:
            region = (max(0, min(b[0] for b in boxes)-3),
                      max(0, min(b[1] for b in boxes)-(3 if stand else chase.height)),
                      min(frame.width, max(b[2] for b in boxes)+3),
                      min(frame.height, max(b[3] for b in boxes)+3))
        follow_position = chase.center if chase else (remembered or {}).get('follow_position')
        return Reading(following if known else None, Quality.KNOWN if known else Quality.UNKNOWN,
                       '' if known else 'follow state obscured; button position retained', diagnostics={
            "reader": "follow-controls", "ui_scale": scale, "effective_region": region,
            "stand_position": stand.center if stand else None, "follow_position": follow_position,
            "stand_visible": stand is not None, "follow_visible": chase is not None,
            "follow_position_source": 'visible' if chase else 'remembered',
            "boxes": boxes, "scores": tuple(m.score if m else None for m in (stand, chase)),
        })

    def parking_geometry(self):
        if self._layout is None:
            return None
        size, _, region, _ = self._layout
        return region, (min(4, size[0]-1), max(0, size[1]-4))

    def read(self, frame, configured_scale=1.0, *, observation_cycle=None):
        if self._closed:
            return Reading(None, Quality.UNKNOWN, "follow reader is closed")
        # Automation and inspection share a result within one observation cycle.
        # A capture provider may reuse the same immutable Frame in later cycles.
        # Standalone calls without a cycle each count as a fresh observation.
        if (observation_cycle is not None and observation_cycle is self._observation_cycle
                and frame is self._frame and configured_scale == self._configured_scale
                and self._reading is not None):
            return self._reading
        if self._candidate is not None:
            candidate, self._candidate = self._candidate, None
            if candidate[0] == frame.size:
                self._layout = candidate
        self._frame = frame
        self._observation_cycle = observation_cycle
        self._configured_scale = configured_scale
        self._reading = None
        if self._layout is not None:
            size, scale, region, remembered = self._layout
            if size == frame.size:
                reading = self._locate(frame, None, scale, region, remembered)
                if reading is not None:
                    self._reading = reading
                    if reading.diagnostics['stand_visible']:
                        remembered = {**remembered, 'stand_position': reading.diagnostics['stand_position']}
                        self._layout = (size, scale, region, remembered)
                    if reading.diagnostics['stand_visible'] or reading.diagnostics['follow_visible']:
                        return reading
            else:
                self._layout = None
        # Acquiring a small control must not hold up health/mana observations.
        # Keep remembered geometry while reacquiring obscured controls. Enabling
        # follow is idempotent and may use its last position in the same layout.
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._discover,
                args=(frame, configured_scale), name="follow-controls", daemon=True)
            self._worker.start()
        if self._reading is None:
            self._reading = Reading(None, Quality.UNKNOWN, "locating follow controls",
                                    {"reader": "follow-controls", "configured_scale": configured_scale})
        return self._reading

    def _discover(self, frame, configured_scale):
        # Settings icons and map-marker assets have different reference sizes.
        # Resolve this on acquisition/recovery only; normal reads use the saved ROI.
        scales = sorted(set((configured_scale, 1.0, 1.25, 1.5, 2.0, .75, 1.75, 2.25, 2.5, 2.75, 3.0, .5)),
                        key=lambda value: abs(value-configured_scale))
        if self._layout is not None and self._layout[1] in scales:
            scales.remove(self._layout[1])
            scales.insert(0, self._layout[1])
        try:
            gray = cv2.cvtColor(frame.pixels, cv2.COLOR_RGB2GRAY)
            for scale in scales:
                if self._closed:
                    return
                reading = self._locate(frame, gray, scale)
                if reading is not None:
                    if not self._closed:
                        self._candidate = (frame.size, scale, reading.diagnostics["effective_region"],
                                           dict(reading.diagnostics))
                    return
        except Exception:
            # The next frame retries acquisition; remembered geometry remains available.
            return

    def close(self):
        self._closed = True
        self._frame = self._reading = self._candidate = self._layout = None
        self._observation_cycle = self._configured_scale = None


def follow_action(sensor, frame, scale=1.0, cache=None, *, observation_cycle=None):
    if sensor.region or sensor.name not in ("", "disable"):
        raise ValueError("follow sensor needs no region and an empty name or 'disable'")
    cache = {} if cache is None else cache
    if "controls" not in cache:
        cache["controls"] = FollowControls()
    state = cache["controls"].read(frame, scale, observation_cycle=observation_cycle)
    desired = sensor.name != "disable"
    diagnostics = {**state.diagnostics, "desired_follow": desired, "following": state.value}
    if state.quality is Quality.KNOWN and state.value == desired:
        return Reading(None, diagnostics=diagnostics)
    point = state.diagnostics.get("follow_position" if desired else "stand_position")
    if point is not None:
        return Reading(point, detail=('Enable follow using remembered position' if desired and
                       not state.diagnostics.get('follow_visible') else ''), diagnostics=diagnostics)
    return Reading(None, Quality.UNKNOWN, 'follow button unavailable' if desired else
                   'stand button unavailable', diagnostics)
