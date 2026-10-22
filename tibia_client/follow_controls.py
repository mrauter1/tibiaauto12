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
        self._templates = {}
        self._scaled = {}
        self._worker = None
        self._candidate = None
        self._closed = False

    def _locate(self, frame, gray, scale, region=None):
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
        if stand is None or chase is None:
            return None
        # A glyph elsewhere on the screen cannot establish the control pair.
        width, height = max(stand.width, chase.width), max(stand.height, chase.height)
        dx, dy = chase.center[0]-stand.center[0], chase.center[1]-stand.center[1]
        if abs(dx) > .3*width or not .6*height <= dy <= 1.5*height:
            return None
        if (stand.score+chase.score)/2 < .8:
            return None

        def coloured(match, following):
            pixels = frame.pixels[match.y:match.y+match.height,
                                  match.x:match.x+match.width].astype("int16")
            red, green, blue = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
            mask = ((green > 100) & (green > red+35) & (green > blue+25) if following else
                    (red > 100) & (red > green+35) & (red > blue+35))
            return int(mask.sum()) >= max(3, round(5*scale*scale))

        standing, following = coloured(stand, False), coloured(chase, True)
        if standing == following:
            return None
        boxes = [(m.x, m.y, m.x+m.width, m.y+m.height) for m in (stand, chase)]
        region = (max(0, min(b[0] for b in boxes)-3), max(0, min(b[1] for b in boxes)-3),
                  min(frame.width, max(b[2] for b in boxes)+3),
                  min(frame.height, max(b[3] for b in boxes)+3))
        return Reading(following, diagnostics={
            "reader": "follow-controls", "ui_scale": scale, "effective_region": region,
            "stand_position": stand.center, "follow_position": chase.center,
            "boxes": boxes, "scores": (stand.score, chase.score),
        })

    def read(self, frame, configured_scale=1.0):
        if self._closed:
            return Reading(None, Quality.UNKNOWN, "follow reader is closed")
        if self._candidate is not None:
            self._layout, self._candidate = self._candidate, None
            self._frame = None
        if frame is self._frame and self._reading is not None:
            return self._reading
        self._frame = frame
        self._reading = None
        if self._layout is not None:
            size, scale, region = self._layout
            if size == frame.size:
                reading = self._locate(frame, None, scale, region)
                if reading is not None:
                    self._reading = reading
                    return reading
        # Acquiring a small control must not hold up health/mana observations.
        # A background result supplies geometry only: current pixels must still
        # validate both buttons before any action can use it.
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._discover,
                args=(frame, configured_scale), name="follow-controls", daemon=True)
            self._worker.start()
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
                        self._candidate = (frame.size, scale, reading.diagnostics["effective_region"])
                    return
        except Exception:
            # The next frame retries acquisition; no remembered state authorizes input.
            return

    def close(self):
        self._closed = True
        self._frame = self._reading = self._candidate = None


def follow_action(sensor, frame, scale=1.0, cache=None):
    if sensor.region or sensor.name not in ("", "disable"):
        raise ValueError("follow sensor needs no region and an empty name or 'disable'")
    cache = {} if cache is None else cache
    if "controls" not in cache:
        cache["controls"] = FollowControls()
    state = cache["controls"].read(frame, scale)
    desired = sensor.name != "disable"
    diagnostics = {**state.diagnostics, "desired_follow": desired, "following": state.value}
    if state.quality is not Quality.KNOWN:
        return Reading(None, state.quality, state.detail, diagnostics)
    point = state.diagnostics["follow_position" if desired else "stand_position"]
    return Reading(None if state.value == desired else point, diagnostics=diagnostics)
