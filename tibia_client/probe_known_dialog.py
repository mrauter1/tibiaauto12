"""Experimental frame-only known-dialog veto; never a readiness authorization.

The operator supplies identifying header/chrome pixels and an explicit search
viewport. A positive match vetoes this known state. Its absence says nothing
about unknown dialogs, focus, freshness, obscuration, or safe input delivery.
There are no desktop, capture, input, profile, or production integration calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from io import BytesIO
from itertools import islice
import json
import math
from numbers import Integral, Real
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
from PIL import Image


DEFAULT_THRESHOLD = 0.90  # Frozen before heldout live-image evaluation.
METHOD = "cv2.TM_CCOEFF_NORMED"
MAX_RGB_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TEMPLATE_PIXELS = 262_144
MAX_CASES = 100
_LIMIT = "Known-state veto only; no_known_veto never establishes safe or ready."


def _digest(payload):
    return hashlib.sha256(payload).hexdigest()


def _box(value, size, label):
    if (not isinstance(value, (tuple, list)) or len(value) != 4
            or any(isinstance(item, bool) or not isinstance(item, Integral) for item in value)):
        raise ValueError(f"{label} must contain four integral coordinates")
    box = tuple(int(item) for item in value)
    left, top, right, bottom = box
    if not (0 <= left < right <= size[0] and 0 <= top < bottom <= size[1]):
        raise ValueError(f"{label} must be nonempty and inside the image")
    return box


def _threshold(value):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("threshold must be a finite number above 0 and at most 1")
    result = float(value)
    if not math.isfinite(result) or not 0 < result <= 1:
        raise ValueError("threshold must be a finite number above 0 and at most 1")
    return result


def _read(path):
    selected = Path(path).expanduser().resolve()
    if selected.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("encoded frame exceeds the file byte limit")
    payload = selected.read_bytes()
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError("encoded frame exceeds the file byte limit")
    with Image.open(BytesIO(payload)) as image:
        if (getattr(image, "n_frames", 1) != 1 or min(image.size) <= 0
                or max(image.size) > 16384 or image.width * image.height * 3 > MAX_RGB_BYTES):
            raise ValueError("frame dimensions/animation exceed the supported bounds")
        pixels = np.asarray(image.convert("RGB"), dtype=np.uint8).copy()
    return {
        "path": str(selected), "file_sha256": _digest(payload),
        "rgb_sha256": _digest(pixels.tobytes()), "size": [pixels.shape[1], pixels.shape[0]],
    }, pixels


def _crop(pixels, box):
    left, top, right, bottom = box
    return np.ascontiguousarray(pixels[top:bottom, left:right])


@dataclass(frozen=True)
class DialogTemplate:
    name: str
    source_path: str
    source_file_sha256: str
    source_rgb_sha256: str
    source_size: tuple[int, int]
    viewport: tuple[int, int, int, int]
    source_viewport_rgb_sha256: str
    template_box: tuple[int, int, int, int]
    template_size: tuple[int, int]
    template_rgb_sha256: str
    spatial_stddev: float
    channel_range: int
    _rgb: bytes = field(repr=False)

    def metadata(self):
        return {
            "name": self.name, "source_path": self.source_path,
            "source_file_sha256": self.source_file_sha256,
            "source_rgb_sha256": self.source_rgb_sha256,
            "source_size": list(self.source_size), "viewport": list(self.viewport),
            "source_viewport_rgb_sha256": self.source_viewport_rgb_sha256,
            "template_box": list(self.template_box), "template_size": list(self.template_size),
            "template_rgb_sha256": self.template_rgb_sha256,
            "spatial_stddev": self.spatial_stddev, "channel_range": self.channel_range,
        }


def create_template(reference_path, template_box, *, viewport, name="known_dialog") -> DialogTemplate:
    """Create an explicit, unscaled header template from one supplied source."""
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError("template name must be nonempty and at most 120 characters")
    source, pixels = _read(reference_path)
    size = tuple(source["size"])
    region = _box(viewport, size, "viewport")
    box = _box(template_box, size, "template_box")
    if not (region[0] <= box[0] < box[2] <= region[2]
            and region[1] <= box[1] < box[3] <= region[3]):
        raise ValueError("template_box must lie inside the configured viewport")
    width, height = box[2] - box[0], box[3] - box[1]
    if width < 12 or height < 8 or width * height > MAX_TEMPLATE_PIXELS:
        raise ValueError("template must be at least 12x8 and within the template pixel limit")
    patch = _crop(pixels, box)
    channels = patch.reshape(-1, 3)
    variation = int(np.ptp(channels, axis=0).max())
    deviation = float(np.std(channels.astype(np.float32), axis=0).mean())
    if variation < 12 or deviation < 4.0:
        raise ValueError("template is constant or lacks identifying spatial variation")
    raw = patch.tobytes()
    return DialogTemplate(name.strip(), source["path"], source["file_sha256"],
                          source["rgb_sha256"], size, region,
                          _digest(_crop(pixels, region).tobytes()), box, (width, height),
                          _digest(raw), deviation, variation, raw)


def _result(template, threshold):
    if not isinstance(template, DialogTemplate):
        raise TypeError("template must be a DialogTemplate")
    return {
        "template": template.metadata(), "frame_path": None,
        "method": METHOD, "threshold": threshold, "score": None, "location": None,
        "match_box": None, "verdict": "unknown", "source_leakage": False,
        "source_leakage_basis": [], "heldout_eligible": False, "error": None,
        "safety_status": "unknown", "evidence_limit": _LIMIT,
    }


def _match_pixels(result, template, frame, pixels, threshold):
    result["frame"] = frame
    if tuple(frame["size"]) != template.source_size:
        raise ValueError("candidate frame dimensions differ from the template source; no automatic resizing")
    viewport = _box(template.viewport, frame["size"], "viewport")
    search = _crop(pixels, viewport)
    viewport_hash = _digest(search.tobytes())
    result["frame"]["viewport_rgb_sha256"] = viewport_hash
    if frame["rgb_sha256"] == template.source_rgb_sha256:
        result["source_leakage_basis"].append("identical_full_source_rgb")
    if viewport_hash == template.source_viewport_rgb_sha256:
        result["source_leakage_basis"].append("identical_source_viewport_rgb")
    result["source_leakage"] = bool(result["source_leakage_basis"])
    width, height = template.template_size
    patch = np.frombuffer(template._rgb, dtype=np.uint8).reshape(height, width, 3)
    scores = cv2.matchTemplate(search, patch, cv2.TM_CCOEFF_NORMED)
    _minimum, score, _minimum_point, point = cv2.minMaxLoc(scores)
    if not math.isfinite(float(score)):
        raise ValueError("normalized matcher returned a nonfinite score")
    score = min(1.0, max(-1.0, float(score)))
    x, y = viewport[0] + int(point[0]), viewport[1] + int(point[1])
    result.update(score=score, location=[x, y], match_box=[x, y, x + width, y + height],
                  verdict="blocked_known_dialog" if score >= threshold else "no_known_veto",
                  heldout_eligible=not result["source_leakage"])


def evaluate_frame(template: DialogTemplate, frame_path, *, threshold=DEFAULT_THRESHOLD) -> dict:
    """Return a known-state veto or absence, retaining explicit unknown failures."""
    threshold = _threshold(threshold)
    result = _result(template, threshold)
    try:
        result["frame_path"] = str(Path(frame_path).expanduser().resolve())
        frame, pixels = _read(frame_path)
        _match_pixels(result, template, frame, pixels, threshold)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:2048]
    return result


def evaluate_image(template: DialogTemplate, image, *, threshold=DEFAULT_THRESHOLD) -> dict:
    """Apply the identical matcher to one in-memory image with no file access."""
    threshold = _threshold(threshold)
    result = _result(template, threshold)
    try:
        if not isinstance(image, Image.Image):
            raise ValueError("candidate must be an in-memory PIL image")
        if (getattr(image, "n_frames", 1) != 1 or min(image.size) <= 0
                or max(image.size) > 16384 or image.width * image.height * 3 > MAX_RGB_BYTES):
            raise ValueError("frame dimensions/animation exceed the supported bounds")
        pixels = np.asarray(image.convert("RGB"), dtype=np.uint8).copy()
        frame = {"path": None, "file_sha256": None, "source_kind": "in_memory_rgb",
                 "rgb_sha256": _digest(pixels.tobytes()), "size": list(image.size)}
        _match_pixels(result, template, frame, pixels, threshold)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:2048]
    return result


def load_template_report(path):
    """Load explicitly configured offline evidence and verify its actual source.

    This experiment enforces the frozen 0.90 threshold. A passed offline report
    supplies a known-state template, never authorization or general readiness.
    """
    selected = Path(path).expanduser().resolve()
    if selected.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("known-dialog report exceeds the JSON byte limit")
    payload = selected.read_bytes()
    if len(payload) > 4 * 1024 * 1024:
        raise ValueError("known-dialog report exceeds the JSON byte limit")
    report = json.loads(payload.decode("utf-8"))
    if (not isinstance(report, dict) or report.get("schema") != "tibiaauto.experimental-known-dialog-veto"
            or type(report.get("version")) is not int or report["version"] != 1
            or report.get("method") != METHOD or report.get("summary", {}).get("status") != "pass"):
        raise ValueError("known-dialog configuration must be a passed version-1 offline evaluation report")
    threshold = _threshold(report.get("threshold"))
    if threshold != DEFAULT_THRESHOLD:
        raise ValueError("frame-lease experiment requires the frozen 0.90 threshold")
    metadata = report.get("template")
    if not isinstance(metadata, dict):
        raise ValueError("known-dialog report lacks template metadata")
    try:
        template = create_template(metadata["source_path"], metadata["template_box"],
                                   viewport=metadata["viewport"], name=metadata["name"])
    except KeyError as exc:
        raise ValueError(f"known-dialog report lacks required source metadata: {exc}") from exc
    actual = template.metadata()
    for field_name in ("source_file_sha256", "source_rgb_sha256", "source_viewport_rgb_sha256",
                       "source_size", "template_box", "viewport", "template_size", "template_rgb_sha256"):
        if metadata.get(field_name) != actual[field_name]:
            raise ValueError(f"known-dialog template/source metadata mismatch: {field_name}")
    provenance = {
        "report_path": str(selected), "report_file_sha256": _digest(payload),
        "method": METHOD, "threshold": threshold, "threshold_enforced": DEFAULT_THRESHOLD,
        "source_hashes_verified": True, "template": actual,
        "evidence_limit": _LIMIT, "report_does_not_authorize_input": True,
    }
    return template, threshold, provenance


def evaluate_dataset(template: DialogTemplate, positives: Iterable, negatives: Iterable,
                     *, threshold=DEFAULT_THRESHOLD) -> dict:
    """Evaluate separately labeled heldout images without adapting the threshold."""
    threshold = _threshold(threshold)
    positives = list(islice(iter(positives), MAX_CASES + 1))
    negatives = list(islice(iter(negatives), MAX_CASES + 1))
    if len(positives) + len(negatives) > MAX_CASES:
        raise ValueError(f"at most {MAX_CASES} evaluation frames are permitted")
    cases = []
    for expected, paths in (("blocked_known_dialog", positives), ("no_known_veto", negatives)):
        for path in paths:
            result = evaluate_frame(template, path, threshold=threshold)
            result["expected_verdict"] = expected
            result["expected_result_observed"] = result["verdict"] == expected
            cases.append(result)
    valid = [item for item in cases if item["heldout_eligible"]]
    invalid = [item for item in cases if not item["heldout_eligible"]]
    positive_hashes = {item["frame"]["viewport_rgb_sha256"] for item in valid
                       if item["expected_verdict"] == "blocked_known_dialog"}
    negative_hashes = {item["frame"]["viewport_rgb_sha256"] for item in valid
                       if item["expected_verdict"] == "no_known_veto"}
    contradictory = bool(positive_hashes & negative_hashes)
    unique_viewports = {item["frame"]["viewport_rgb_sha256"] for item in valid}
    unique_rgb_frames = {item["frame"]["rgb_sha256"] for item in valid}
    complete = bool(positives and negatives)
    passed = (complete and not invalid and not contradictory
              and all(item["expected_result_observed"] for item in cases))
    return {
        "schema": "tibiaauto.experimental-known-dialog-veto", "version": 1,
        "template": template.metadata(), "method": METHOD, "threshold": threshold,
        "threshold_policy": "no automatic adaptation; threshold selection timing is not independently verified",
        "default_threshold": DEFAULT_THRESHOLD, "threshold_matches_default": threshold == DEFAULT_THRESHOLD,
        "threshold_selection_provenance": "operator must establish that selection preceded heldout inspection",
        "cases": cases,
        "summary": {"status": "pass" if passed else "fail" if cases else "unknown",
                    "positive_cases": len(positives), "negative_cases": len(negatives),
                    "eligible_heldout_cases": len(valid), "invalid_or_leaked_cases": len(invalid),
                    "unique_eligible_rgb_frames": len(unique_rgb_frames),
                    "unique_eligible_viewports": len(unique_viewports),
                    "duplicate_eligible_viewports": len(valid) - len(unique_viewports),
                    "unique_positive_viewports": len(positive_hashes),
                    "unique_negative_viewports": len(negative_hashes),
                    "contradictory_viewport_labels": contradictory,
                    "both_labels_required": True, "both_labels_present": complete},
        "overall_safety": "unknown", "production_wiring": "none", "evidence_limit": _LIMIT,
    }


__all__ = ["DEFAULT_THRESHOLD", "DialogTemplate", "create_template", "evaluate_frame", "evaluate_image",
           "evaluate_dataset", "load_template_report"]
