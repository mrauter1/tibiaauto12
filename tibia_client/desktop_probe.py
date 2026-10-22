"""Experimental capture measurements, independent of production configuration.

An API returning pixels proves only transport. Neither nonblack pixels nor
pixel changes prove that the game accepted input or that a frame is current.
Native imports are lazy; importing this module never inspects the desktop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from numbers import Integral, Real
from pathlib import Path
import statistics
import time
from typing import Any

import numpy as np
from PIL import Image

from core.perception import as_frame
from tibia_client.capture import CaptureProcess, DEFAULT_MAX_PIXEL_BYTES


METHODS = ("printwindow", "printwindow_physical", "visible_screen", "visible_screen_overlay_aware",
           "visible_region_gdi_overlay_aware", "direct_visible_region_gdi")
MAX_SAMPLES = 100
_EVIDENCE_LIMIT = (
    "Pixel availability, nonblack content, hashes, and differences measure transport only; "
    "they do not confirm Tibia content, freshness, or game acceptance."
)


def _hwnd(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
        raise ValueError("hwnd must be a positive integer")
    return int(value)


def _method(value: str) -> str:
    if value not in METHODS:
        raise ValueError("method must be one of: " + ", ".join(METHODS))
    return value


def _size(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(isinstance(item, bool) or not isinstance(item, Integral) or item <= 0
                   for item in value)):
        raise ValueError("expected_size must contain two positive integers")
    result = tuple(int(item) for item in value)
    if result[0] * result[1] * 3 > DEFAULT_MAX_PIXEL_BYTES:
        raise ValueError("expected_size exceeds the inherited capture byte limit")
    return result


@dataclass(frozen=True)
class CaptureSource:
    """Spawn-pickleable request for the actual repository capture backend."""

    hwnd: int
    method: str

    def __post_init__(self):
        object.__setattr__(self, "hwnd", _hwnd(self.hwnd))
        _method(self.method)

    def capture(self):
        if self.method == "direct_visible_region_gdi":
            from tibia_client.direct_capture import capture_direct_host_client

            return capture_direct_host_client(self.hwnd)
        if self.method == "printwindow":
            from core.perception import _capture_windows

            return _capture_windows(self.hwnd)
        if self.method == "printwindow_physical":
            from core.perception import _capture_windows
            from tibia_client.window_target import _native_backend

            # This is a separately named experiment: preserve the original
            # PrintWindow baseline and change only the caller's DPI context.
            with _native_backend().physical_pixels():
                return _capture_windows(self.hwnd)
        if self.method == "visible_screen_overlay_aware":
            from tibia_client.probe_overlay import capture_client_overlay_aware

            return capture_client_overlay_aware(self.hwnd)
        if self.method == "visible_region_gdi_overlay_aware":
            from tibia_client.probe_region_gdi import capture_client_region_gdi_overlay_aware

            return capture_client_region_gdi_overlay_aware(self.hwnd)
        from tibia_client.window_target import capture_host_client

        return capture_host_client(self.hwnd)


def inspect_window(hwnd: int) -> dict:
    """Read native geometry/focus without changing focus or sending input."""
    from tibia_client.window_target import _native_backend

    selected = _hwnd(hwnd)
    native = _native_backend()
    with native.physical_pixels():
        size = _size(tuple(native.client_size(selected)))
        root = int(native.api.GetAncestor(selected, 2) or 0)
        foreground = int(native.api.GetForegroundWindow() or 0)
        foreground_root = int(native.api.GetAncestor(foreground, 2) or 0)
        return {
            "client_size": list(size),
            "root_hwnd": root,
            "foreground_hwnd": foreground,
            "foreground_root_hwnd": foreground_root,
            "foreground_root_matches": bool(root and root == foreground_root),
            "visible": bool(native.api.IsWindowVisible(selected)),
            "minimized": bool(native.api.IsIconic(selected)),
        }


def inventory() -> list[dict]:
    """Enumerate visible titled top-level windows, including physical geometry."""
    from conf.WindowTitles import get_titles

    result = []
    for hwnd, title in get_titles().items():
        item = {"hwnd": int(hwnd), "title": str(title)}
        try:
            item["physical_pixels"] = inspect_window(hwnd)
        except Exception as exc:
            item["physical_pixels"] = {"error": f"{type(exc).__name__}: {exc}"[:2048]}
        result.append(item)
    return result


def _inspect_known_handle(hwnd: int) -> dict:
    """Read one explicit native handle; no enumeration, focus change, or input."""
    from tibia_client.window_target import _native_backend

    native = _native_backend()
    result = {
        "hwnd": hwnd, "is_window": False, "root_hwnd": None, "visible": False,
        "minimized": None, "title": "", "class_name": "", "client_size": None,
        "foreground_hwnd": None, "foreground_root_hwnd": None,
        "foreground_root_matches": False, "inspection_errors": [],
    }
    with native.physical_pixels():
        result["is_window"] = bool(native.api.IsWindow(hwnd))
        if result["is_window"]:
            root = int(native.api.GetAncestor(hwnd, 2) or 0)
            foreground = int(native.api.GetForegroundWindow() or 0)
            foreground_root = int(native.api.GetAncestor(foreground, 2) or 0)
            result.update(root_hwnd=root, visible=bool(native.api.IsWindowVisible(hwnd)),
                          minimized=bool(native.api.IsIconic(hwnd)), foreground_hwnd=foreground,
                          foreground_root_hwnd=foreground_root,
                          foreground_root_matches=bool(root and root == foreground_root))
            for field_name, method_name in (("title", "GetWindowTextW"),
                                            ("class_name", "GetClassNameW")):
                try:
                    buffer = native.ctypes.create_unicode_buffer(256)
                    count = int(getattr(native.api, method_name)(hwnd, buffer, 256))
                    result[field_name] = buffer.value.strip() if count > 0 else ""
                    result[f"{field_name}_character_count"] = count
                    result[f"{field_name}_possibly_truncated"] = count >= 255
                except Exception as exc:
                    result["inspection_errors"].append(f"{method_name}: {type(exc).__name__}: {exc}"[:2048])
            try:
                result["client_size"] = list(_size(tuple(native.client_size(hwnd))))
            except Exception as exc:
                result["inspection_errors"].append(f"physical client size: {type(exc).__name__}: {exc}"[:2048])
            result["is_window_after"] = bool(native.api.IsWindow(hwnd))
            result["root_hwnd_after"] = int(native.api.GetAncestor(hwnd, 2) or 0)
        result["inspected_monotonic"] = time.monotonic()
        result["inspected_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def verify_known_handle(hwnd, *, inventory_items=None) -> dict:
    """Offer a separate, verified manual target without inventing enumeration.

    Child, hidden, minimized, captionless, or invalid windows remain unselected.
    Foreground state is recorded, not changed; capture/input retain their own
    current foreground checks. This function never starts capture or arms input.
    """
    valid_number = (isinstance(hwnd, Integral) and not isinstance(hwnd, bool)
                    and 0 < int(hwnd) < 2**64)
    requested = int(hwnd) if valid_number else None
    report = {
        "schema_version": 1, "kind": "known_handle_diagnostic", "requested_hwnd": requested,
        "selected_hwnd": None, "selection": None, "status": "rejected", "reasons": [],
        "inspection": {}, "selection_method": "explicit_verified_hwnd",
        "inventory_presence": "unknown", "inventory_count": None,
        "input_armed": False, "capture_started": False,
        "root_substitution": "never automatic",
    }
    if inventory_items is not None:
        try:
            handles = [int(item["hwnd"]) for item in inventory_items
                       if isinstance(item, dict) and isinstance(item.get("hwnd"), Integral)
                       and not isinstance(item["hwnd"], bool)]
            report["inventory_count"] = len(handles)
            report["inventory_presence"] = "present" if requested in handles else "absent"
        except Exception as exc:
            report["inventory_snapshot_error"] = f"{type(exc).__name__}: {exc}"[:2048]
    if not valid_number:
        report["reasons"].append("requested handle must be a positive 64-bit integer")
        return report
    try:
        current = _inspect_known_handle(requested)
        report["inspection"] = current
    except Exception as exc:
        report["reasons"].append(f"fresh native inspection failed: {type(exc).__name__}: {exc}"[:2048])
        return report
    reasons = report["reasons"]
    if not current.get("is_window"):
        reasons.append("IsWindow returned false; the supplied identifier is not a current native window")
        return report
    if current.get("root_hwnd") != requested:
        reasons.append(f"requested handle is not its own GA_ROOT (root={current.get('root_hwnd')}); no root was substituted")
    if not current.get("visible"):
        reasons.append("IsWindowVisible returned false")
    if current.get("minimized") is not False:
        reasons.append("window is minimized or minimized state is unavailable")
    if not current.get("title"):
        reasons.append("native window title is empty or unavailable")
    if not current.get("class_name"):
        reasons.append("native window class is empty or unavailable")
    try:
        _size(current.get("client_size"))
    except ValueError as exc:
        reasons.append(f"physical client size is unavailable or invalid: {exc}")
    if not current.get("is_window_after") or current.get("root_hwnd_after") != current.get("root_hwnd"):
        reasons.append("window validity or root changed during native inspection")
    reasons.extend(current.get("inspection_errors", []))
    if reasons:
        return report
    report.update(status="verified", selected_hwnd=requested)
    physical = {key: current[key] for key in (
        "client_size", "root_hwnd", "foreground_hwnd", "foreground_root_hwnd",
        "foreground_root_matches", "visible", "minimized")}
    provenance = {key: value for key, value in report.items() if key != "selection"}
    report["selection"] = {
        "hwnd": requested, "title": current["title"], "physical_pixels": physical,
        "selection_method": "explicit_verified_hwnd", "requested_hwnd": requested,
        "inventory_presence": report["inventory_presence"], "handle_diagnostic": provenance,
    }
    return report


def _latencies(values):
    """Nearest-rank p95; a single observation remains one observation."""
    if not values:
        return {"count": 0, "median": None, "p95": None}
    ordered = sorted(float(value) for value in values)
    return {"count": len(ordered), "median": statistics.median(ordered),
            "p95": ordered[math.ceil(0.95 * len(ordered)) - 1]}


def _gate(status, reason):
    return {"status": status, "reason": reason}


def collect_capture(hwnd: int, method: str, output_dir, samples: int = 5, token=None,
                    *, expected_size=None) -> dict:
    """Capture a bounded sequence, save original RGB PNGs, and return evidence.

    The first fatal capture/persistence error stops the sequence. There are no
    retries or backend fallbacks. ``expected_size``, when supplied, comes from
    an operator-selected native geometry snapshot, never a saved bot profile.
    No configuration, compatibility sidecar, or authentication data is read.
    """
    source = CaptureSource(hwnd, method)
    if isinstance(samples, bool) or not isinstance(samples, Integral) or not 1 <= samples <= MAX_SAMPLES:
        raise ValueError(f"samples must be an integer from 1 through {MAX_SAMPLES}")
    samples = int(samples)
    expected = None if expected_size is None else _size(expected_size)
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "kind": "experimental_capture_probe",
        "hwnd": source.hwnd,
        "method": source.method,
        "requested_samples": samples,
        "expected_size": None if expected is None else list(expected),
        "output_dir": str(directory),
        "limits": {"startup_timeout_s": 15.0, "capture_timeout_s": 2.0,
                   "max_restarts": 0, "max_pixel_bytes": DEFAULT_MAX_PIXEL_BYTES},
        "frames": [],
        "errors": [],
        "evidence_limit": _EVIDENCE_LIMIT,
    }
    if method in ("visible_screen_overlay_aware", "visible_region_gdi_overlay_aware"):
        from tibia_client.probe_overlay import ANNOTATION_CLASS, ANNOTATION_TITLES

        report["overlay_annotation_policy"] = {
            "experimental": True, "class_name": ANNOTATION_CLASS,
            "allowed_titles": list(ANNOTATION_TITLES),
            "required_extended_styles": ["WS_EX_TRANSPARENT", "WS_EX_NOACTIVATE"],
            "point_hit_checks": "unchanged",
            "runtime_qualification_metadata": "not_transported_by_capture_process",
        }
    if method == "visible_region_gdi_overlay_aware":
        report["region_capture_policy"] = {
            "experimental": True, "grab": "BitBlt to top-down 32-bit DIB",
            "source": "physical host-client rectangle", "full_virtual_desktop_grab": False,
            "raster_operation": "SRCCOPY", "include_layered_windows": False,
            "foreground_geometry_occlusion_point_checks": "unchanged",
        }
    capture = None
    reference_size = expected
    previous = None
    try:
        capture = CaptureProcess(source, startup_timeout=15.0, timeout=2.0,
                                 max_restarts=0, capture_size=expected)
        for index in range(1, samples + 1):
            item = {"sequence": index, "capture_ok": False}
            started = time.monotonic()
            try:
                frame = as_frame(capture.capture(token=token), source=f"probe-{method}")
                returned = time.monotonic()
                parent_return_utc = datetime.now(timezone.utc).isoformat()
                item["wall_ms"] = max(0.0, (returned - started) * 1000)
                observed = getattr(capture, "last_observed_at", None)
                valid_observed = (isinstance(observed, Real) and not isinstance(observed, bool)
                                  and math.isfinite(float(observed))
                                  and 0 < float(observed) <= returned)
                item["captured_monotonic"] = float(observed) if valid_observed else returned
                item["captured_monotonic_basis"] = (
                    "worker_observation_start" if valid_observed else "parent_return_fallback"
                )
                item["captured_utc"] = parent_return_utc
                item["captured_utc_basis"] = "parent_return"
                item["native_capture_ms"] = (
                    max(0.0, (returned - float(observed)) * 1000)
                    if valid_observed
                    else None
                )
                pixels = frame.pixels
                if pixels.nbytes > DEFAULT_MAX_PIXEL_BYTES:
                    raise ValueError("frame exceeds the inherited capture byte limit")
                if reference_size is None:
                    reference_size = frame.size
                raw = pixels.tobytes()
                item.update(capture_ok=True, size=list(frame.size),
                            size_matches=frame.size == reference_size,
                            nonblack=bool(np.any(pixels)), sha256_rgb=hashlib.sha256(raw).hexdigest())
                if previous is not None and previous.shape == pixels.shape:
                    item["changed_ratio_from_previous"] = float(
                        np.mean(np.any(previous != pixels, axis=2)))
                else:
                    item["changed_ratio_from_previous"] = None
                # Exclusive creation preserves previous evidence on repeated calls.
                path = directory / f"{method}-{index:03d}.png"
                with path.open("xb") as stream:
                    frame.image.save(stream, format="PNG")
                item["path"] = str(path)
                previous = pixels.copy()
                report["frames"].append(item)
                if not item["size_matches"]:
                    report["errors"].append(f"sample {index}: capture dimensions changed")
                    break
            except Exception as exc:
                item.setdefault("wall_ms", max(0.0, (time.monotonic() - started) * 1000))
                detail = f"{type(exc).__name__}: {exc}"[:2048]
                item["error"] = detail
                report["frames"].append(item)
                report["errors"].append(f"sample {index}: {detail}")
                break
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}"[:2048])
    finally:
        if capture is not None:
            try:
                capture.close()
            except Exception as exc:
                report["errors"].append(f"close: {type(exc).__name__}: {exc}"[:2048])

    frames = report["frames"]
    complete = (len(frames) == samples and not report["errors"]
                and all(item.get("capture_ok") and item.get("size_matches") for item in frames))
    valid = [item for item in frames if item.get("capture_ok")]
    report["summary"] = {
        "received_samples": len(valid),
        "cold_wall_ms": frames[0].get("wall_ms") if frames else None,
        "warm_wall_ms": _latencies([item["wall_ms"] for item in frames[1:]
                                    if item.get("capture_ok")]),
        "native_capture_ms": _latencies([item["native_capture_ms"] for item in valid
                                         if item.get("native_capture_ms") is not None]),
        "native_capture_ms_definition": (
            "Parent return minus worker observation start; includes native capture, "
            "frame conversion, and transport, not an isolated native API duration."
        ),
        "unique_rgb_hashes": len({item["sha256_rgb"] for item in valid}),
        "observed_size": valid[0]["size"] if valid else None,
    }
    if expected is None:
        geometry = _gate("unknown", "No expected physical client size supplied.")
    elif not valid:
        geometry = _gate("unknown", "No returned frame available for dimension comparison; inspect errors.")
    elif any(item["size"] != list(expected) for item in valid):
        geometry = _gate("fail", "A received frame differs from the supplied physical client size.")
    else:
        geometry = _gate("pass", "All received frames match the supplied physical client size.")
    report["gates"] = {
        "transport": _gate("pass" if complete else "fail",
                           "All requested frames received at consistent dimensions."
                           if complete else "Sequence incomplete, inconsistent, or failed; inspect errors."),
        "native_geometry": geometry,
        "nonblack": _gate("unknown" if not valid else "pass" if all(item["nonblack"] for item in valid)
                          else "fail", "Nonblack pixels are transport evidence only."),
        "game_content": _gate("unknown", "Operator must confirm saved pixels contain the intended Tibia client."),
        "freshness": _gate("unknown", "Operator-controlled visible state change and recognition are required."),
        "input_acceptance": _gate("not_run", "This capture probe never sends input."),
    }
    return report


def compare_frames(path_a, path_b, roi=None) -> dict:
    """Compare saved pixels; a difference does not establish game freshness."""
    with Image.open(path_a) as first, Image.open(path_b) as second:
        if any(image.width * image.height * 3 > DEFAULT_MAX_PIXEL_BYTES
               for image in (first, second)):
            raise ValueError("comparison image exceeds the inherited capture byte limit")
        same_size = first.size == second.size
        result = {"same_size": same_size, "size_a": list(first.size), "size_b": list(second.size),
                  "roi": None, "changed_ratio": None, "mean_absdiff": None,
                  "freshness_confirmed": False, "evidence_limit": _EVIDENCE_LIMIT}
        if not same_size:
            return result
        if roi is not None:
            if (not isinstance(roi, (tuple, list)) or len(roi) != 4
                    or any(isinstance(item, bool) or not isinstance(item, Integral) for item in roi)):
                raise ValueError("roi must contain four integer coordinates")
            left, top, right, bottom = tuple(int(item) for item in roi)
            if not 0 <= left < right <= first.width or not 0 <= top < bottom <= first.height:
                raise ValueError("roi must be nonempty and inside both images")
            roi = (left, top, right, bottom)
            result["roi"] = list(roi)
            first, second = first.crop(roi), second.crop(roi)
        a, b = np.asarray(first.convert("RGB")), np.asarray(second.convert("RGB"))
        result["changed_ratio"] = float(np.mean(np.any(a != b, axis=2)))
        result["mean_absdiff"] = float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())
        return result


def write_report(path, report) -> Path:
    """Write only the explicitly selected report, never a compatibility sidecar."""
    payload = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(payload, encoding="utf-8")
    return destination


__all__ = ["CaptureSource", "METHODS", "MAX_SAMPLES", "collect_capture", "compare_frames",
           "inspect_window", "inventory", "verify_known_handle", "write_report"]
