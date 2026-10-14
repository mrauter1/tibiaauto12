"""Validated, lossless resource-capture sessions for tracker replay.

This is deliberately separate from :mod:`tibia_client.trace`: policy traces do
not contain screenshots, while a resource sequence is a small, annotated set of
complete client frames.  A session's declared origin is evidence, not a guess;
derived and imported inputs can never be serialized as native captures by the
recorder's convenience constructors.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from core.perception import Frame, validate_region


SCHEMA = "tibiaauto.resource-sequence"
SCHEMA_VERSION = 1
RESOURCE_FIELDS = (
    "resource_health",
    "resource_mana",
    "resource_soul",
    "resource_capacity",
)
ORIGINS = {"native_capture", "imported_native", "derived_corpus"}
ANNOTATION_STATUSES = {"unreviewed", "reviewed"}
FRESHNESS_STATES = {"fresh", "stale", "unknown"}
EVENT_KINDS = {"startup", "change", "invalidation", "recovery"}
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_FRAME_BYTES = 64 * 1024 * 1024
_MAX_SESSION_PIXEL_BYTES = 1024 * 1024 * 1024
_MAX_FRAMES = 10_000


class ResourceSequenceError(ValueError):
    """A resource sequence is incomplete, corrupt, unsafe, or inconsistent."""


@dataclass(frozen=True, slots=True)
class SequenceFrame:
    index: int
    frame: Frame
    relative_ms: float
    panel: Mapping[str, Any]
    resources: Mapping[str, Mapping[str, Any]]
    capture_freshness: Mapping[str, Any]
    event_ids: tuple[str, ...]
    file_sha256: str
    pixel_sha256: str


@dataclass(frozen=True, slots=True)
class ResourceSequence:
    path: Path
    session_id: str
    origin: str
    provenance: Mapping[str, Any]
    source_lineage: Mapping[str, Any] | None
    frames: tuple[SequenceFrame, ...]
    events: tuple[Mapping[str, Any], ...]
    document: Mapping[str, Any]


def unreviewed_truth(reason: str = "capture has not been annotated") -> dict[str, Any]:
    """Return explicit draft truth; it is never interpreted as invisible."""

    return {
        "panel": {
            "annotation_status": "unreviewed",
            "visible": None,
            "readable": None,
            "reason": reason,
            "box": None,
        },
        "resources": {
            field: {
                "annotation_status": "unreviewed",
                "visible": None,
                "readable": None,
                "reason": reason,
                "label_box": None,
                "value_box": None,
                "value": None,
            }
            for field in RESOURCE_FIELDS
        },
        "capture_freshness": {
            "annotation_status": "unreviewed",
            "state": "unknown",
            "reason": "capture freshness has not been independently reviewed",
            "evidence": None,
        },
    }


def _json_copy(value: Any, name: str) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ResourceSequenceError(f"{name} must be finite JSON data: {exc}") from None


def _box(value: Any, size: tuple[int, int], name: str) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ResourceSequenceError(f"{name} must be an xyxy box or null")
    try:
        return list(validate_region(value, size))
    except Exception as exc:
        raise ResourceSequenceError(f"{name} is invalid: {exc}") from None


def _annotation(value: Any, size: tuple[int, int], name: str, *, panel: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResourceSequenceError(f"{name} must be an object")
    status = value.get("annotation_status")
    if status not in ANNOTATION_STATUSES:
        raise ResourceSequenceError(f"{name}.annotation_status must be reviewed or unreviewed")
    visible, readable = value.get("visible"), value.get("readable")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ResourceSequenceError(f"{name}.reason must be non-empty text")
    if status == "unreviewed":
        if visible is not None or readable is not None:
            raise ResourceSequenceError(f"{name}: unreviewed visibility/readability must be null")
    elif type(visible) is not bool or type(readable) is not bool:
        raise ResourceSequenceError(f"{name}: reviewed visibility/readability must be booleans")
    elif readable and not visible:
        raise ResourceSequenceError(f"{name}: a readable item must be visible")
    result = {
        "annotation_status": status,
        "visible": visible,
        "readable": readable,
        "reason": reason,
    }
    if panel:
        result["box"] = _box(value.get("box"), size, f"{name}.box")
        if status == "reviewed" and visible and result["box"] is None:
            raise ResourceSequenceError(f"{name}.box is required when the panel is visible")
    else:
        result["label_box"] = _box(value.get("label_box"), size, f"{name}.label_box")
        result["value_box"] = _box(value.get("value_box"), size, f"{name}.value_box")
        raw = value.get("value")
        if raw is not None and (type(raw) is not int or raw < 0):
            raise ResourceSequenceError(f"{name}.value must be a non-negative integer or null")
        if status == "reviewed" and readable:
            if raw is None:
                raise ResourceSequenceError(f"{name}.value is required when readable")
            if result["label_box"] is None or result["value_box"] is None:
                raise ResourceSequenceError(
                    f"{name} needs label_box and value_box when readable"
                )
        if status == "reviewed" and not readable and raw is not None:
            raise ResourceSequenceError(f"{name}.value must be null when unreadable")
        if status == "unreviewed" and raw is not None:
            raise ResourceSequenceError(f"{name}: unreviewed value must be null")
        result["value"] = raw
    return result


def _freshness(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResourceSequenceError(f"{name} must be an object")
    status, state = value.get("annotation_status"), value.get("state")
    reason, evidence = value.get("reason"), value.get("evidence")
    if status not in ANNOTATION_STATUSES or state not in FRESHNESS_STATES:
        raise ResourceSequenceError(f"{name} has invalid annotation_status or state")
    if status == "unreviewed" and state != "unknown":
        raise ResourceSequenceError(f"{name}: unreviewed freshness must be unknown")
    if not isinstance(reason, str) or not reason.strip():
        raise ResourceSequenceError(f"{name}.reason must be non-empty text")
    if evidence is not None and not isinstance(evidence, str):
        raise ResourceSequenceError(f"{name}.evidence must be text or null")
    if (
        status == "reviewed"
        and state in {"fresh", "stale"}
        and (not isinstance(evidence, str) or not evidence.strip())
    ):
        raise ResourceSequenceError(
            f"{name}.evidence must be non-empty for reviewed {state} state"
        )
    return {
        "annotation_status": status,
        "state": state,
        "reason": reason,
        "evidence": evidence,
    }


def _inside(outer: list[int] | None, inner: list[int] | None) -> bool:
    return bool(
        outer and inner
        and outer[0] <= inner[0] and outer[1] <= inner[1]
        and outer[2] >= inner[2] and outer[3] >= inner[3]
    )


def _validate_frame_truth(
    panel: Mapping[str, Any], resources: Mapping[str, Mapping[str, Any]], name: str
) -> None:
    for field, truth in resources.items():
        if truth["annotation_status"] == "reviewed" and truth["readable"]:
            if not (
                panel["annotation_status"] == "reviewed"
                and panel["visible"] is True
                and panel["readable"] is True
            ):
                raise ResourceSequenceError(
                    f"{name}.{field}: readable resource requires a reviewed readable panel"
                )
            if not _inside(panel["box"], truth["label_box"]) or not _inside(
                panel["box"], truth["value_box"]
            ):
                raise ResourceSequenceError(
                    f"{name}.{field}: label and value boxes must be inside the panel"
                )


def _event(value: Any, frame_count: int, timestamps: list[float]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResourceSequenceError("event must be an object")
    event_id = value.get("id")
    field = value.get("resource")
    frame_index = value.get("frame_index")
    at = value.get("at")
    deadline_ms = value.get("deadline_ms")
    expected = value.get("expected_value")
    status = value.get("annotation_status")
    if not isinstance(event_id, str) or not _SESSION_ID.fullmatch(event_id):
        raise ResourceSequenceError("event.id is invalid")
    if field not in RESOURCE_FIELDS or value.get("kind") not in EVENT_KINDS:
        raise ResourceSequenceError(f"event {event_id}: invalid resource or kind")
    if type(frame_index) is not int or not 0 <= frame_index < frame_count:
        raise ResourceSequenceError(f"event {event_id}: invalid frame_index")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        raise ResourceSequenceError(f"event {event_id}: at must be finite")
    if abs(float(at) - timestamps[frame_index]) > 1e-6:
        raise ResourceSequenceError(f"event {event_id}: at must equal its frame timestamp")
    if isinstance(deadline_ms, bool) or not isinstance(deadline_ms, (int, float)):
        raise ResourceSequenceError(f"event {event_id}: deadline_ms must be numeric")
    if not math.isfinite(float(deadline_ms)) or float(deadline_ms) < 0:
        raise ResourceSequenceError(f"event {event_id}: deadline_ms must be finite and non-negative")
    if expected is not None and (type(expected) is not int or expected < 0):
        raise ResourceSequenceError(f"event {event_id}: expected_value is invalid")
    if status not in ANNOTATION_STATUSES:
        raise ResourceSequenceError(f"event {event_id}: invalid annotation_status")
    result = dict(_json_copy(value, f"event {event_id}"))
    result.update({"at": float(at), "deadline_ms": float(deadline_ms)})
    return result


class ResourceSequenceRecorder:
    """Build a new session from actual :class:`Frame` objects.

    The destination is published by one directory rename only after the
    manifest and every PNG are durable.  Existing paths are never overwritten.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        session_id: str,
        origin: str,
        provenance: Mapping[str, Any],
        source_lineage: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("session_id must be a safe 1-128 character identifier")
        if origin not in ORIGINS:
            raise ValueError(f"origin must be one of {sorted(ORIGINS)}")
        if origin == "derived_corpus" and not source_lineage:
            raise ValueError("derived_corpus sessions require source_lineage")
        if origin != "derived_corpus" and source_lineage is not None:
            raise ValueError("source_lineage is reserved for derived_corpus sessions")
        required = {
            "recorded_at_utc", "client_build", "client_family", "operating_system",
            "ui_scale", "display_scale", "capture_provider", "capture_coordinate_space",
        }
        provenance_copy = _json_copy(provenance, "provenance")
        if not isinstance(provenance_copy, dict) or not required.issubset(provenance_copy):
            missing = sorted(required - set(provenance_copy or {}))
            raise ValueError(f"provenance must explicitly include fields (null if unknown): {missing}")
        self.path = Path(path)
        if self.path.exists():
            raise FileExistsError(self.path)
        self._staging = self.path.with_name(f".{self.path.name}.tmp-{uuid4().hex}")
        self._staging.mkdir(parents=True, exist_ok=False)
        (self._staging / "frames").mkdir()
        self._session_id = session_id
        self._origin = origin
        self._provenance = provenance_copy
        self._source_lineage = _json_copy(source_lineage, "source_lineage")
        self._frames: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self._written: dict[tuple[int, int, str], tuple[str, str, int]] = {}
        self._closed = False
        self._pixel_bytes = 0

    def record(
        self,
        frame: Frame,
        *,
        panel: Mapping[str, Any] | None = None,
        resources: Mapping[str, Mapping[str, Any]] | None = None,
        capture_freshness: Mapping[str, Any] | None = None,
        events: Iterable[Mapping[str, Any]] = (),
    ) -> int:
        if self._closed:
            raise ResourceSequenceError("resource sequence recorder is closed")
        if not isinstance(frame, Frame):
            raise TypeError("record requires an actual Frame")
        if len(self._frames) >= _MAX_FRAMES:
            raise ResourceSequenceError("resource sequence frame limit reached")
        frame_bytes = frame.width * frame.height * 3
        if self._pixel_bytes + frame_bytes > _MAX_SESSION_PIXEL_BYTES:
            raise ResourceSequenceError("resource sequence decoded-pixel budget exceeded")
        captured_at = float(frame.captured_at)
        if not math.isfinite(captured_at):
            raise ResourceSequenceError("frame timestamp must be finite")
        if self._frames and captured_at < self._frames[-1]["captured_at"]:
            raise ResourceSequenceError("frame timestamps must be monotonic")
        draft = unreviewed_truth()
        panel_value = _annotation(panel or draft["panel"], frame.size, "panel", panel=True)
        supplied = resources or draft["resources"]
        if set(supplied) != set(RESOURCE_FIELDS):
            raise ResourceSequenceError("resources must contain exactly the four resource fields")
        resource_values = {
            field: _annotation(supplied[field], frame.size, field, panel=False)
            for field in RESOURCE_FIELDS
        }
        freshness = _freshness(
            capture_freshness or draft["capture_freshness"], "capture_freshness"
        )
        _validate_frame_truth(panel_value, resource_values, f"frame {len(self._frames)}")
        pixel_digest = sha256(frame._rgb).hexdigest()
        identity = (frame.width, frame.height, pixel_digest)
        asset_id = sha256(
            f"RGB:{frame.width}x{frame.height}:".encode() + frame._rgb
        ).hexdigest()
        relative_path = f"frames/{asset_id}.png"
        if identity not in self._written:
            output = BytesIO()
            frame.image.save(output, format="PNG", optimize=False)
            encoded = output.getvalue()
            if len(encoded) > _MAX_FRAME_BYTES:
                raise ResourceSequenceError("encoded frame exceeds the frame byte limit")
            file_digest = sha256(encoded).hexdigest()
            file_path = self._staging / PurePosixPath(relative_path)
            file_path.write_bytes(encoded)
            self._written[identity] = relative_path, file_digest, len(encoded)
        relative_path, file_digest, byte_size = self._written[identity]
        index = len(self._frames)
        event_ids = []
        for raw in events:
            event = dict(_json_copy(raw, "event"))
            event.setdefault("frame_index", index)
            event.setdefault("at", captured_at)
            self._events.append(event)
            event_ids.append(event.get("id"))
        self._frames.append(
            {
                "index": index,
                "path": relative_path,
                "file_sha256": file_digest,
                "pixel_sha256": pixel_digest,
                "byte_size": byte_size,
                "width": frame.width,
                "height": frame.height,
                "captured_at": captured_at,
                "relative_ms": 0.0 if not self._frames else (
                    captured_at - self._frames[0]["captured_at"]
                ) * 1000.0,
                "source": frame.source,
                "panel": panel_value,
                "resources": resource_values,
                "capture_freshness": freshness,
                "event_ids": event_ids,
            }
        )
        self._pixel_bytes += frame_bytes
        return index

    def close(self) -> Path:
        if self._closed:
            return self.path
        if not self._frames:
            raise ResourceSequenceError("resource sequence must contain at least one frame")
        timestamps = [float(item["captured_at"]) for item in self._frames]
        validated_events = [_event(item, len(self._frames), timestamps) for item in self._events]
        _validate_event_truth(validated_events, self._frames)
        ids = [item["id"] for item in validated_events]
        if len(ids) != len(set(ids)):
            raise ResourceSequenceError("event ids must be unique")
        for frame in self._frames:
            expected_ids = [
                event["id"] for event in validated_events if event["frame_index"] == frame["index"]
            ]
            if frame["event_ids"] != expected_ids:
                raise ResourceSequenceError("frame event_ids do not match declared events")
        document = {
            "schema": SCHEMA,
            "version": SCHEMA_VERSION,
            "session_id": self._session_id,
            "origin": self._origin,
            "provenance": self._provenance,
            "source_lineage": self._source_lineage,
            "frames": self._frames,
            "events": validated_events,
        }
        encoded = (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise ResourceSequenceError("resource sequence manifest exceeds byte limit")
        try:
            manifest = self._staging / "session.json"
            with manifest.open("wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            self._staging.rename(self.path)
            self._closed = True
            return self.path
        except BaseException:
            self.abort()
            raise

    def abort(self) -> None:
        if not self._closed:
            shutil.rmtree(self._staging, ignore_errors=True)
            self._closed = True

    def __enter__(self) -> "ResourceSequenceRecorder":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is None:
            try:
                self.close()
            except BaseException:
                self.abort()
                raise
        else:
            self.abort()


def _regular_file(path: Path, root: Path, *, max_bytes: int) -> bytes:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ResourceSequenceError("resource sequence path escapes the session") from None
    if any(part in ("", ".", "..") for part in relative.parts):
        raise ResourceSequenceError("resource sequence contains an unsafe path")
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise ResourceSequenceError(f"could not inspect session directory: {exc}") from None
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ResourceSequenceError("resource sequence root must be a real directory")
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ResourceSequenceError(f"could not inspect {current}: {exc}") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ResourceSequenceError("resource sequence path contains a symlink or non-directory")
    try:
        path_info = path.lstat()
    except OSError as exc:
        raise ResourceSequenceError(f"could not inspect {relative.as_posix()}: {exc}") from None
    if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
        raise ResourceSequenceError(
            f"{relative.as_posix()} is a symlink or non-regular file"
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ResourceSequenceError(f"could not open {relative.as_posix()}: {exc}") from None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ResourceSequenceError(f"{relative.as_posix()} is not a bounded regular file")
        # O_NOFOLLOW is unavailable on Windows. Bind the opened handle to the
        # file inspected above wherever the filesystem supplies stable IDs.
        if (path_info.st_ino or info.st_ino) and not os.path.samestat(path_info, info):
            raise ResourceSequenceError(
                f"{relative.as_posix()} changed while it was being opened"
            )
        return stream.read(max_bytes + 1)


def load_resource_sequence(path: str | Path) -> ResourceSequence:
    """Load and fully validate a bounded session before returning any frame."""

    root = Path(path)
    manifest_path = root / "session.json"
    raw = _regular_file(manifest_path, root, max_bytes=_MAX_MANIFEST_BYTES)
    try:
        document = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResourceSequenceError(f"invalid session manifest: {exc}") from None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA or document.get("version") != 1:
        raise ResourceSequenceError("unsupported resource sequence schema")
    session_id, origin = document.get("session_id"), document.get("origin")
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ResourceSequenceError("invalid session_id")
    if origin not in ORIGINS:
        raise ResourceSequenceError("invalid session origin")
    if origin == "derived_corpus" and not isinstance(document.get("source_lineage"), dict):
        raise ResourceSequenceError("derived session lacks source lineage")
    if origin != "derived_corpus" and document.get("source_lineage") is not None:
        raise ResourceSequenceError("non-derived session has source lineage")
    provenance = document.get("provenance")
    required = {
        "recorded_at_utc", "client_build", "client_family", "operating_system",
        "ui_scale", "display_scale", "capture_provider", "capture_coordinate_space",
    }
    if not isinstance(provenance, dict) or not required.issubset(provenance):
        raise ResourceSequenceError("session provenance omits explicit fields")
    records = document.get("frames")
    if not isinstance(records, list) or not 1 <= len(records) <= _MAX_FRAMES:
        raise ResourceSequenceError("session must contain a bounded non-empty frame list")
    loaded: list[SequenceFrame] = []
    timestamps: list[float] = []
    decoded_total = 0
    for index, record in enumerate(records):
        if not isinstance(record, dict) or record.get("index") != index:
            raise ResourceSequenceError("frame indices must be contiguous")
        relative = record.get("path")
        if not isinstance(relative, str):
            raise ResourceSequenceError(f"frame {index}: path must be text")
        posix = PurePosixPath(relative)
        if posix.is_absolute() or not posix.parts or any(part in ("", ".", "..") for part in posix.parts):
            raise ResourceSequenceError(f"frame {index}: unsafe path")
        encoded = _regular_file(root.joinpath(*posix.parts), root, max_bytes=_MAX_FRAME_BYTES)
        if len(encoded) != record.get("byte_size") or sha256(encoded).hexdigest() != record.get("file_sha256"):
            raise ResourceSequenceError(f"frame {index}: file size or SHA-256 mismatch")
        try:
            with Image.open(BytesIO(encoded)) as image:
                declared_size = (record.get("width"), record.get("height"))
                if (
                    any(type(item) is not int or item <= 0 for item in declared_size)
                    or image.size != declared_size
                    or declared_size[0] * declared_size[1] * 3 > _MAX_FRAME_BYTES
                ):
                    raise ResourceSequenceError(
                        f"frame {index}: invalid or oversized decoded dimensions"
                    )
                decoded_total += declared_size[0] * declared_size[1] * 3
                if decoded_total > _MAX_SESSION_PIXEL_BYTES:
                    raise ResourceSequenceError(
                        "resource sequence decoded-pixel budget exceeded"
                    )
                image.load()
                rgb = image.convert("RGB")
        except (UnidentifiedImageError, OSError) as exc:
            raise ResourceSequenceError(f"frame {index}: invalid PNG: {exc}") from None
        if image.format != "PNG" or rgb.size != (record.get("width"), record.get("height")):
            raise ResourceSequenceError(f"frame {index}: format or dimensions mismatch")
        pixels = rgb.tobytes()
        if sha256(pixels).hexdigest() != record.get("pixel_sha256"):
            raise ResourceSequenceError(f"frame {index}: pixel SHA-256 mismatch")
        captured_at = record.get("captured_at")
        relative_ms = record.get("relative_ms")
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in (captured_at, relative_ms)
        ):
            raise ResourceSequenceError(f"frame {index}: timestamps must be finite")
        captured_at = float(captured_at)
        if timestamps and captured_at < timestamps[-1]:
            raise ResourceSequenceError("frame timestamps must be monotonic")
        expected_relative = (captured_at - (timestamps[0] if timestamps else captured_at)) * 1000
        if abs(float(relative_ms) - expected_relative) > 1e-4:
            raise ResourceSequenceError(f"frame {index}: relative timestamp mismatch")
        timestamps.append(captured_at)
        size = rgb.size
        panel = _annotation(record.get("panel"), size, f"frame {index}.panel", panel=True)
        resources = record.get("resources")
        if not isinstance(resources, dict) or set(resources) != set(RESOURCE_FIELDS):
            raise ResourceSequenceError(f"frame {index}: resources are incomplete")
        resources = {
            field: MappingProxyType(_annotation(resources[field], size, f"frame {index}.{field}", panel=False))
            for field in RESOURCE_FIELDS
        }
        _validate_frame_truth(panel, resources, f"frame {index}")
        freshness = _freshness(record.get("capture_freshness"), f"frame {index}.capture_freshness")
        event_ids = record.get("event_ids")
        if not isinstance(event_ids, list) or any(not isinstance(item, str) for item in event_ids):
            raise ResourceSequenceError(f"frame {index}: event_ids must be text")
        frame = Frame(rgb, captured_at=captured_at, source=str(record.get("source", "sequence")))
        loaded.append(
            SequenceFrame(
                index, frame, float(relative_ms), MappingProxyType(panel),
                MappingProxyType(resources), MappingProxyType(freshness), tuple(event_ids),
                record["file_sha256"], record["pixel_sha256"],
            )
        )
    raw_events = document.get("events")
    if not isinstance(raw_events, list):
        raise ResourceSequenceError("events must be a list")
    events = [_event(item, len(loaded), timestamps) for item in raw_events]
    _validate_event_truth(events, records)
    ids = [item["id"] for item in events]
    if len(ids) != len(set(ids)):
        raise ResourceSequenceError("event ids must be unique")
    for frame in loaded:
        expected = tuple(item["id"] for item in events if item["frame_index"] == frame.index)
        if frame.event_ids != expected:
            raise ResourceSequenceError(f"frame {frame.index}: event_ids mismatch")
    return ResourceSequence(
        root, session_id, origin, MappingProxyType(provenance),
        None if document.get("source_lineage") is None else MappingProxyType(document["source_lineage"]),
        tuple(loaded), tuple(MappingProxyType(item) for item in events), MappingProxyType(document),
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ResourceSequenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_event_truth(
    events: Iterable[Mapping[str, Any]], frames: list[Mapping[str, Any]]
) -> None:
    for event in events:
        if event["annotation_status"] != "reviewed":
            continue
        frame = frames[event["frame_index"]]
        freshness = frame["capture_freshness"]
        truth = frame["resources"][event["resource"]]
        if not (
            freshness["annotation_status"] == "reviewed"
            and freshness["state"] == "fresh"
            and truth["annotation_status"] == "reviewed"
        ):
            raise ResourceSequenceError(
                f"event {event['id']}: reviewed event requires reviewed fresh onset truth"
            )
        if event["kind"] == "invalidation":
            valid = event["expected_value"] is None and truth["readable"] is False
        else:
            valid = (
                event["expected_value"] is not None
                and truth["readable"] is True
                and event["expected_value"] == truth["value"]
            )
        if not valid:
            raise ResourceSequenceError(
                f"event {event['id']}: declaration conflicts with onset-frame truth"
            )


__all__ = [
    "ANNOTATION_STATUSES", "EVENT_KINDS", "FRESHNESS_STATES", "ORIGINS",
    "RESOURCE_FIELDS", "ResourceSequence", "ResourceSequenceError",
    "ResourceSequenceRecorder", "SequenceFrame", "load_resource_sequence",
    "unreviewed_truth",
]
