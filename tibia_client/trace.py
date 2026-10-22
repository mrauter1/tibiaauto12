"""Bounded observation-only traces and native-free policy/scenario replay.

Traces contain settings-derived intents and coordinates, but no screenshots.
They are diagnostic data, not executable programs or configuration bundles.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import threading
from typing import Any

from tibia_client import models
from tibia_client.models import (
    Action,
    Event,
    FeatureContext,
    ObservationBatch,
    Reading,
    Snapshot,
    Step,
    finite,
)
from tibia_client.ports import Feature


_SCHEMA = {"format": "tibia-client-trace", "version": 1, "mode": "observations"}
_TYPES = {
    cls.__name__: cls
    for cls in (
        models.Sensor,
        models.Target,
        models.Marker,
        models.Reading,
        models.Observation,
        models.Snapshot,
        models.Action,
        models.Requirement,
        models.Confirmation,
        models.Request,
        models.ActionResult,
        models.Step,
        models.FeatureStatus,
        models.Event,
    )
}
_ENUMS = {
    cls.__name__: cls
    for cls in (
        models.SensorKind,
        models.Quality,
        models.ActionKind,
        models.Priority,
        models.ResultState,
    )
}
_MAX_LINE = 4 * 1024 * 1024
_DEFAULT_LIMIT = 16 * 1024 * 1024


class TraceError(ValueError):
    """Invalid, incomplete, oversized, or incompatible trace."""


def _encode(value: Any, depth: int = 0) -> Any:
    if depth > 32:
        raise TraceError("trace value nesting is too deep")
    if type(value) in _ENUMS.values():
        return {"enum": type(value).__name__, "value": value.value}
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        # Negative numeric domain values are legitimate; nonfinite values aren't.
        if not math.isfinite(value):
            raise TraceError("trace contains a nonfinite number")
        return value
    if type(value) in _TYPES.values():
        return {
            "type": type(value).__name__,
            "fields": {
                field.name: _encode(getattr(value, field.name), depth + 1)
                for field in fields(value)
            },
        }
    if isinstance(value, Mapping):
        return {"map": [[_encode(k, depth + 1), _encode(v, depth + 1)] for k, v in value.items()]}
    if isinstance(value, tuple):
        return {"tuple": [_encode(item, depth + 1) for item in value]}
    raise TraceError(f"unsupported trace value: {type(value).__name__}")


def _decode(value: Any, depth: int = 0) -> Any:
    if depth > 32:
        raise TraceError("trace value nesting is too deep")
    if type(value) is float and not math.isfinite(value):
        raise TraceError("trace contains a nonfinite number")
    if value is None or type(value) in (str, bool, int, float):
        return value
    if not isinstance(value, dict):
        raise TraceError("invalid tagged trace value")
    if set(value) == {"enum", "value"} and value["enum"] in _ENUMS:
        return _ENUMS[value["enum"]](value["value"])
    if set(value) == {"type", "fields"} and value["type"] in _TYPES:
        cls = _TYPES[value["type"]]
        supplied = value["fields"]
        expected = {field.name for field in fields(cls)}
        # Diagnostics are an additive observation field. Existing version-1
        # traces predate it and retain an empty diagnostic mapping on decode.
        legacy_observation = (
            cls in (models.Reading, models.Observation)
            and isinstance(supplied, dict)
            and set(supplied) == expected - {"diagnostics"}
        )
        # Earlier targets represented complete counts. Preserve those traces
        # while recording the explicit completeness flag in new traces.
        legacy_target = (
            cls is models.Target
            and isinstance(supplied, dict)
            and set(supplied) == expected - {"count_complete"}
        )
        if not isinstance(supplied, dict) or (
            set(supplied) != expected and not legacy_observation and not legacy_target
        ):
            raise TraceError("trace model fields do not match schema")
        return cls(**{key: _decode(item, depth + 1) for key, item in supplied.items()})
    if set(value) == {"tuple"} and isinstance(value["tuple"], list):
        return tuple(_decode(item, depth + 1) for item in value["tuple"])
    if set(value) == {"map"} and isinstance(value["map"], list):
        result = {}
        for pair in value["map"]:
            if not isinstance(pair, list) or len(pair) != 2:
                raise TraceError("invalid map entry")
            key, item = (_decode(part, depth + 1) for part in pair)
            if key in result:
                raise TraceError("duplicate map key")
            result[key] = item
        return result
    raise TraceError("unknown trace value tag")


def _line(value: Any) -> bytes:
    result = (
        json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(result) > _MAX_LINE:
        raise TraceError("trace event exceeds line limit")
    return result


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TraceError("duplicate JSON key")
        result[key] = value
    return result


def _parse(line: bytes):
    if not line.endswith(b"\n"):
        raise TraceError("truncated trace line")

    def invalid_constant(value):
        raise TraceError(f"nonfinite JSON number: {value}")

    return json.loads(line, object_pairs_hook=_pairs, parse_constant=invalid_constant)


class JsonlRecorder:
    """Create a new trace; quota/I/O errors stop recording and propagate.

    Close after the session. A footer proves orderly completion and detects a
    truncated last event, even if truncation happened at a newline boundary.
    Existing files are never overwritten. Callers own the recorder lifecycle.
    """

    def __init__(self, path: str | Path, *, max_bytes: int = _DEFAULT_LIMIT):
        if type(max_bytes) is not int or max_bytes < 1024:
            raise ValueError("max_bytes must be an integer of at least 1024")
        self._limit = max_bytes
        self._size = 0
        self._count = 0
        self._sequence = 0
        self._digest = hashlib.sha256()
        self._lock = threading.Lock()
        self._failed = False
        self._stream = Path(path).open("xb")
        try:
            self._write(_line(_SCHEMA))
        except BaseException:
            self._stream.close()
            raise

    def _write(self, data: bytes):
        if self._size + len(data) + 256 > self._limit:
            raise TraceError("trace size limit reached")
        self._stream.write(data)
        self._stream.flush()
        self._size += len(data)

    def record(self, event: Event) -> None:
        with self._lock:
            if self._failed or self._stream.closed:
                raise TraceError("trace recorder is unavailable")
            try:
                if not isinstance(event, Event) or event.sequence != self._sequence + 1:
                    raise TraceError("trace requires contiguous events from session start")
                line = _line(_encode(event))
                self._write(line)
                self._digest.update(line)
                self._sequence = event.sequence
                self._count += 1
            except BaseException:
                self._failed = True
                raise

    def close(self) -> None:
        with self._lock:
            if self._stream.closed:
                return
            try:
                if not self._failed:
                    self._stream.write(
                        _line({"end": self._count, "sha256": self._digest.hexdigest()})
                    )
                    self._stream.flush()
            finally:
                self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def load_trace(
    path: str | Path, *, max_bytes: int = _DEFAULT_LIMIT, max_events: int = 100_000
) -> tuple[Event, ...]:
    """Validate the complete bounded file before returning any replayable data."""
    if (
        type(max_bytes) is not int
        or max_bytes < 1024
        or type(max_events) is not int
        or max_events < 1
    ):
        raise ValueError("invalid trace reader limits")
    events = []
    digest = hashlib.sha256()
    total = 0
    try:
        # Check and parse the same open file, even if a log is replaced while
        # being inspected. Nonblocking open lets us reject a FIFO before reading.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(Path(path), flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise TraceError("trace source must be a regular file")
            if info.st_size > max_bytes:
                raise TraceError("trace size limit exceeded")
            header = stream.readline(min(_MAX_LINE, max_bytes) + 1)
            total += len(header)
            if len(header) > _MAX_LINE or total > max_bytes:
                raise TraceError("trace size limit exceeded")
            schema = _parse(header)
            if schema != _SCHEMA or type(schema.get("version")) is not int:
                raise TraceError("unsupported trace schema or mode")
            while True:
                line = stream.readline(_MAX_LINE + 1)
                if not line:
                    raise TraceError("incomplete trace: completion footer is missing")
                total += len(line)
                if len(line) > _MAX_LINE or total > max_bytes:
                    raise TraceError("trace size limit exceeded")
                raw = _parse(line)
                if isinstance(raw, dict) and "end" in raw:
                    if (
                        type(raw["end"]) is not int
                        or raw != {"end": len(events), "sha256": digest.hexdigest()}
                        or stream.read(1)
                    ):
                        raise TraceError("trace footer or checksum does not match")
                    return tuple(events)
                event = _decode(raw)
                if not isinstance(event, Event) or event.sequence != len(events) + 1:
                    raise TraceError("invalid trace event sequence")
                if len(events) >= max_events:
                    raise TraceError("trace event limit exceeded")
                events.append(event)
                digest.update(line)
    except OSError as exc:
        raise TraceError(f"cannot read trace: {exc}") from exc
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError) as exc:
        if isinstance(exc, TraceError):
            raise
        raise TraceError(f"invalid trace: {exc}") from exc


class ReplayClock:
    def __init__(self, at: float = 0.0):
        self._at = finite(at, "replay time")

    def monotonic(self) -> float:
        return self._at

    def advance_to(self, at: float) -> None:
        value = finite(at, "replay time")
        if value < self._at:
            raise TraceError("replay clock cannot move backwards")
        self._at = value


class ReplayAdapter:
    """Feed recorded observation batches to a manually ticked ClientSession.

    A scenario controls the virtual clock between ticks. Each observe consumes
    one snapshot; requesting a sensor not sampled in that frame fails closed.
    Actions are collected in memory and never sent to any desktop.
    """

    def __init__(self, snapshots: Sequence[Snapshot], clock: ReplayClock):
        self._snapshots = tuple(snapshots)
        if any(not isinstance(snapshot, Snapshot) for snapshot in self._snapshots):
            raise ValueError("replay needs Snapshot values")
        self.clock = clock
        self.actions: list[tuple[float, Action]] = []
        self._index = 0
        self.closed = False

    def ready(self) -> bool:
        return not self.closed

    def observe(self, sensors, token):
        token.checkpoint()
        if self.closed or self._index == len(self._snapshots):
            raise TraceError("replay observations exhausted")
        snapshot = self._snapshots[self._index]
        values = {}
        observed_at = None
        for sensor in sensors:
            observation = snapshot.observation(sensor)
            if observation.frame_id != snapshot.frame_id:
                raise TraceError("requested sensor was not sampled in this replay frame")
            if observed_at is not None and observed_at != observation.observed_at:
                raise TraceError("replay frame contains inconsistent sample times")
            observed_at = observation.observed_at
            values[sensor] = Reading(
                observation.value, observation.quality, observation.detail,
                observation.diagnostics,
            )
        if observed_at is None:
            raise TraceError("replay observation needs at least one sensor")
        self.clock.advance_to(snapshot.created_at)
        self._index += 1
        return ObservationBatch(values, observed_at)

    def execute(self, action, token):
        token.checkpoint()
        if self.closed:
            raise TraceError("replay adapter is closed")
        self.actions.append((self.clock.monotonic(), action))

    def close(self):
        self.closed = True


@dataclass(frozen=True)
class ReplayReport:
    decisions: int
    requests: int
    feedback: int


def _validate_policy_event(event: Event) -> None:
    """Validate event payload semantics before any feature factory is invoked."""
    required = {
        "observation": {"snapshot": Snapshot},
        "status": {"status": models.FeatureStatus, "reason": str},
        "decision": {"generation": int, "step": Step, "snapshot": Snapshot},
        "result": {"generation": int, "result": models.ActionResult, "deliver": bool},
    }.get(event.kind, {})
    for name, kind in required.items():
        value = event.data.get(name)
        if not isinstance(value, kind) or (kind is int and type(value) is not int):
            raise TraceError(f"invalid {event.kind} payload: {name}")
    if event.kind in ("decision", "result") and event.data["generation"] < 1:
        raise TraceError("invalid feature generation")
    if event.kind == "result" and event.data["result"].owner != event.owner:
        raise TraceError("result owner does not match event")
    if event.kind == "status" and event.data["reason"] == "started":
        status = event.data["status"]
        if not status.active or status.name != event.owner or status.generation < 1:
            raise TraceError("invalid feature start event")


def replay_policies(
    events: Sequence[Event], factory: Callable[[str, int], Feature]
) -> ReplayReport:
    """Compare pure feature decisions against a trace, using recorded feedback.

    The caller supplies fresh features with the original settings. CaveBot must
    receive a no-op/in-memory checkpoint writer. This verifies feature policy,
    not image recognition, native input, or operating-system thread scheduling.
    No desktop adapter is constructed and no generated action is executed.
    """
    events = tuple(events)
    for sequence, event in enumerate(events, 1):
        if not isinstance(event, Event) or event.sequence != sequence:
            raise TraceError("policy replay needs a complete ordered trace")
        _validate_policy_event(event)
    instances = {}
    active = set()
    decisions = requests = feedback = 0
    for sequence, event in enumerate(events, 1):
        if event.kind == "status":
            status = event.data["status"]
            identity = (status.name, status.generation)
            if event.data.get("reason") == "started":
                feature = factory(*identity)
                if getattr(feature, "name", None) != status.name or identity in instances:
                    raise TraceError("factory returned an incompatible feature")
                instances[identity] = feature
                active.add(identity)
            elif not status.active:
                active.discard(identity)
        elif event.kind == "decision":
            identity = (event.owner, event.data["generation"])
            feature = instances.get(identity)
            if feature is None:
                raise TraceError("decision has no matching feature generation")
            step = feature.tick(FeatureContext(event.data["snapshot"], event.at))
            expected = event.data["step"]
            if not isinstance(step, Step) or step != expected:
                raise TraceError(
                    f"policy diverged at event {sequence} ({event.owner}): expected {expected!r}, got {step!r}"
                )
            decisions += 1
            requests += len(step.requests)
        elif event.kind == "result":
            identity = (event.owner, event.data["generation"])
            feature = instances.get(identity)
            if feature is not None and identity in active and event.data["deliver"]:
                feature.on_result(event.data["result"])
                feedback += 1
    if not decisions:
        raise TraceError("trace contains no feature decisions to replay")
    return ReplayReport(decisions, requests, feedback)
