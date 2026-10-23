"""Fixed resource rows: retain geometry, never retain an unreadable value."""
from dataclasses import asdict, replace
import hashlib
import json
import os
import asyncio
from collections import OrderedDict

import cv2
import numpy as np

from core.paths import scripts_path
from core.perception import validate_region
from tibia_client.adaptive import Anchor, DiscoveryResult, FIELDS, PanelLayout, _crop, _ink
from tibia_client.models import Quality, Reading
from tibia_client.resource_reader import ResourceReader


class RetainedResourceReader(ResourceReader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._numbers = OrderedDict()

    def discover(self, frame, *, use_ocr=False, checkpoint=None):
        """Read semantic labels once with Windows OCR, then retain actual pixels."""
        checkpoint = checkpoint or (lambda: None)
        from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
        checkpoint()
        init_apartment(ApartmentType.MULTI_THREADED)
        try:
            lines = asyncio.run(_label_lines(frame))
        finally:
            uninit_apartment()
        checkpoint()
        aliases = {"hitpoints": FIELDS[0], "health": FIELDS[0], "mana": FIELDS[1],
                   "soulpoints": FIELDS[2], "soul": FIELDS[2], "capacity": FIELDS[3]}
        anchors, regions, observed_values = [], {}, {}
        numbers = {(text, box) for words in lines for text, box in words
                   if text.replace(",", "").replace(".", "").isdigit()}
        for words in lines:
            for split in (2, 1):
                if len(words) < split:
                    continue
                label = "".join(w[0].lower().strip(":") for w in words[:split])
                key = aliases.get(label)
                if key is None:
                    continue
                boxes = [w[1] for w in words[:split]]
                box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                       max(b[2] for b in boxes), max(b[3] for b in boxes))
                values = [b for _, b in numbers if 3 <= b[0]-box[2] <= 300
                          and abs((b[1]+b[3]-box[1]-box[3])/2) <= 3]
                if len(values) != 1 or any(a.field == key and a.box == box for a in anchors):
                    continue
                # This font bank is calibrated to the primary 11-pixel labels.
                scale = (box[3]-box[1]) / self._templates[key].shape[0]
                anchors.append(Anchor(key, box, 1.0, scale, "ocr"))
                observed_values[key] = int(next(text for text, b in numbers if b == values[0]).replace(",", "").replace(".", ""))
                regions[key] = (box[2]+3, max(0, min(b[1] for b in values)-3),
                                min(frame.width, max(b[2] for b in values)+5),
                                min(frame.height, max(b[3] for b in values)+3))
                break
        complete = []
        for group in self._constellations(anchors):
            if len(group) != 4:
                continue
            if max(regions[a.field][2] for a in group)-min(regions[a.field][2] for a in group) > 4:
                continue
            scale = float(np.median([a.scale for a in group]))
            bounds = (min(a.box[0] for a in group), min(a.box[1] for a in group),
                      max(regions[a.field][2] for a in group), max(regions[a.field][3] for a in group))
            geometry = {a.field: regions[a.field] for a in group}
            # Select one numeric font scale during calibration, corroborated by
            # independent OCR. Ordinary reads never sweep scales or font banks.
            center = round(scale/.05)*.05
            choices = []
            for candidate_scale in (center-.05, center, center+.05):
                checkpoint()
                decoded = {key: self.number_reader.read(_crop(frame, region),
                           scale=candidate_scale, max_digits=9,
                           allow_grouping=key != "resource_soul", use_ocr=False)
                           for key, region in geometry.items()}
                accepted = [r for key, r in decoded.items() if r.quality is Quality.KNOWN
                            and r.value == observed_values[key]]
                if any(r.quality is Quality.KNOWN and r.value != observed_values[key]
                       for key, r in decoded.items()):
                    continue
                choices.append((len(accepted), sum(r.diagnostics.get("score", 0) for r in accepted), candidate_scale))
            if choices:
                scale = max(choices)[2]
            identity = hashlib.sha256(repr((frame.size, bounds, geometry, scale)).encode()).hexdigest()
            complete.append(PanelLayout(bounds, group, geometry, scale, identity, frame.size,
                                        tuple(_ink(_crop(frame, a.box)).tobytes() for a in group)))
        if len(complete) == 1:
            return DiscoveryResult(complete[0])
        return DiscoveryResult(None, "Show one complete Skills panel with HP, Mana, Soul and Capacity")

    def visible_fields(self, frame, layout):
        if frame.size != layout.frame_size:
            return set()
        visible = set()
        for anchor, saved in zip(layout.anchors, layout.guards):
            observed = _ink(_crop(frame, anchor.box))
            if observed.size != len(saved):
                continue
            expected = np.frombuffer(saved, dtype=np.uint8).reshape(observed.shape)
            union = np.count_nonzero((observed > 0) | (expected > 0))
            if union and np.count_nonzero(observed != expected) / union <= .12:
                visible.add(anchor.field)
        return visible

    def read(self, frame, layout, *, use_ocr=False):
        visible = self.visible_fields(frame, layout)
        alternate = sum(a.method == "alternate_template" for a in layout.anchors)
        family = "alternate" if alternate >= 2 and alternate * 2 >= len(layout.anchors) else None
        results = {}
        for field in FIELDS:
            region = layout.regions.get(field)
            diagnostics = {"layout_visible": field in visible, "reader": "retained-rows"}
            if region is not None and frame.size == layout.frame_size:
                diagnostics["effective_region"] = region
            if field not in visible or region is None:
                results[field] = Reading(None, Quality.UNKNOWN,
                    "Saved row is hidden or displaced; layout retained", diagnostics)
                continue
            # Fixed, full-width row rectangles. A failed digit never changes geometry.
            pixels = _crop(frame, region)
            key = (pixels.shape, hashlib.blake2b(pixels.tobytes(), digest_size=16).digest(),
                   layout.scale, family, field, use_ocr)
            reading = self._numbers.get(key)
            if reading is None:
                reading = self.number_reader.read(pixels, scale=layout.scale,
                    max_digits=9, allow_grouping=field != "resource_soul",
                    use_ocr=use_ocr, family=family)
                self._numbers[key] = reading
                while len(self._numbers) > 128:
                    self._numbers.popitem(last=False)
            self._numbers.move_to_end(key)
            diagnostics.update(reading.diagnostics)
            diagnostics.update(numeric_eligible=True, scale=layout.scale,
                               layout_fingerprint=layout.fingerprint)
            value = ({"present": True, "position": None, "count": reading.value,
                      "score": reading.diagnostics.get("score", 0)}
                     if reading.quality is Quality.KNOWN else None)
            results[field] = Reading(value, reading.quality, reading.detail, diagnostics)
        return results

    def relocate(self, frame, layout, radius=80):
        """Search nearby at the saved scale; require multiple agreeing labels."""
        if frame.size != layout.frame_size:
            return None
        offsets = []
        for anchor, saved in zip(layout.anchors, layout.guards):
            x, y, right, bottom = anchor.box
            bounds = (max(0, x-radius), max(0, y-radius),
                      min(frame.width, right+radius), min(frame.height, bottom+radius))
            template = np.frombuffer(saved, dtype=np.uint8).reshape(bottom-y, right-x)
            mask = _ink(_crop(frame, bounds))
            scores = cv2.matchTemplate(mask, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, point = cv2.minMaxLoc(scores)
            if score >= .9:
                offsets.append((bounds[0]+point[0]-x, bounds[1]+point[1]-y))
        if len(offsets) < 2:
            return None
        dx, dy = (round(float(v)) for v in np.median(offsets, axis=0))
        if sum(abs(x-dx) <= 1 and abs(y-dy) <= 1 for x, y in offsets) < 2:
            return None
        def shift(box):
            return validate_region((box[0]+dx, box[1]+dy, box[2]+dx, box[3]+dy), frame.size)
        try:
            shifted = replace(layout, bounds=shift(layout.bounds),
                anchors=tuple(replace(a, box=shift(a.box)) for a in layout.anchors),
                regions={key: shift(box) for key, box in layout.regions.items()},
                fingerprint=hashlib.sha256(f"{layout.fingerprint}:{dx}:{dy}".encode()).hexdigest())
        except ValueError:
            return None
        return shifted if len(self.visible_fields(frame, shifted)) >= 2 else None


def cache_path():
    """Separate geometry caches by selected profile and capture configuration."""
    config = json.loads(scripts_path("conf.json").read_text(encoding="utf-8"))
    identity = {key: config.get(key) for key in
                ("preferences_name", "capture_size", "window_target", "capture_title")}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    return scripts_path(f"resource-layout-{key}.json")


async def _label_lines(frame):
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter
    language = Language("en-US")
    engine = (OcrEngine.try_create_from_language(language) if OcrEngine.is_language_supported(language)
              else OcrEngine.try_create_from_user_profile_languages())
    if engine is None:
        raise RuntimeError("Windows OCR requires an installed recognition language")
    lines = []
    # Smaller overlapping strips keep short numeric columns legible to OCR.
    for left in range(0, frame.width, 650):
        pixels = frame.pixels[:, left:min(frame.width, left+800)]
        factor = min(3.0, OcrEngine.max_image_dimension/max(pixels.shape[:2]))
        gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
        mask = cv2.resize(gray, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
        bgra = cv2.cvtColor(255-mask, cv2.COLOR_GRAY2BGRA)
        with DataWriter() as writer:
            writer.write_bytes(bgra.tobytes())
            with SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8,
                                                        bgra.shape[1], bgra.shape[0]) as bitmap:
                result = await engine.recognize_async(bitmap)
                lines.extend(tuple((word.text, (left+round(word.bounding_rect.x/factor),
                         round(word.bounding_rect.y/factor),
                         left+round((word.bounding_rect.x+word.bounding_rect.width)/factor),
                         round((word.bounding_rect.y+word.bounding_rect.height)/factor)))
                         for word in line.words) for line in result.lines)
    return tuple(lines)


def save_layout(path, layout):
    data = {"version": 1, "bounds": layout.bounds, "frame_size": layout.frame_size,
            "scale": layout.scale, "fingerprint": layout.fingerprint,
            "anchors": [asdict(a) for a in layout.anchors],
            "regions": dict(layout.regions), "guards": [g.hex() for g in layout.guards]}
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temporary, path)


def load_layout(path):
    try:
        if path.stat().st_size > 128_000:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["version"] != 1:
            return None
        size = tuple(data["frame_size"])
        if len(size) != 2 or any(type(v) is not int or v <= 0 for v in size):
            return None
        anchors = tuple(Anchor(a["field"], validate_region(tuple(a["box"]), size),
                               float(a["score"]), float(a["scale"]), a["method"])
                        for a in data["anchors"])
        guards = tuple(bytes.fromhex(g) for g in data["guards"])
        if not 2 <= len(anchors) == len(guards) <= 4:
            return None
        if any(a.field not in FIELDS or len(g) != (a.box[2]-a.box[0])*(a.box[3]-a.box[1])
               for a, g in zip(anchors, guards)):
            return None
        scale = float(data["scale"])
        if not .1 <= scale <= 8:
            return None
        return PanelLayout(validate_region(tuple(data["bounds"]), size), anchors,
            {k: validate_region(tuple(v), size) for k, v in data["regions"].items() if k in FIELDS},
            scale, str(data["fingerprint"]), size, guards)
    except (OSError, ValueError, TypeError, KeyError):
        return None
