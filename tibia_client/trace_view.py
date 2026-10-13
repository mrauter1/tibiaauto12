"""Bounded, observation-only presentation helpers for trace files.

This module intentionally depends only on the domain models and the strict trace
reader.  Loading or inspecting a trace never constructs an adapter, imports a
desktop backend, or replays an action.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Any

from tibia_client.models import ActionResult, Event, Request, Snapshot, Step
from tibia_client.trace import JsonlRecorder, TraceError, load_trace


DEFAULT_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_EVENTS = 100_000
DEFAULT_PRESENTATION_LIMIT = 2_000
MAX_PRESENTATION_LIMIT = 10_000
MAX_DETAIL_CHARS = 64 * 1024
_MAX_VALUE_ITEMS = 200
_MAX_VALUE_DEPTH = 8
_MAX_STRING_CHARS = 4_096


@dataclass(frozen=True)
class TraceStatistics:
    total_events: int
    first_at: float | None
    last_at: float | None
    duration: float
    kinds: Mapping[str, int]
    owners: Mapping[str, int]
    results: Mapping[str, int]
    requests: int
    dispatches: int
    failures: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_events": self.total_events,
            "first_at": self.first_at,
            "last_at": self.last_at,
            "duration": self.duration,
            "kinds": dict(self.kinds),
            "owners": dict(self.owners),
            "results": dict(self.results),
            "requests": self.requests,
            "dispatches": self.dispatches,
            "failures": self.failures,
        }


@dataclass(frozen=True)
class EventRow:
    event: Event
    elapsed: float
    request: str
    summary: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.event.sequence,
            "at": self.event.at,
            "elapsed": self.elapsed,
            "kind": self.event.kind,
            "owner": self.event.owner,
            "request": self.request,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class FilteredEvents:
    rows: tuple[EventRow, ...]
    matched: int
    limit: int

    @property
    def truncated(self) -> bool:
        return self.matched > len(self.rows)


@dataclass(frozen=True)
class TraceDocument:
    events: tuple[Event, ...]
    statistics: TraceStatistics
    source: str = ""

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(self.statistics.kinds)

    @property
    def owners(self) -> tuple[str, ...]:
        return tuple(self.statistics.owners)


def _ordered_counts(values: Sequence[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items(), key=lambda item: (-item[1], item[0])))


def trace_statistics(events: Sequence[Event]) -> TraceStatistics:
    """Compute small aggregate counts without retaining additional payload data."""
    checked = _validated_events(events, require_session_start=False)
    first_at = checked[0].at if checked else None
    last_at = checked[-1].at if checked else None
    results = []
    failures = 0
    for event in checked:
        result = event.data.get("result")
        if isinstance(result, ActionResult):
            state = result.state.value
            results.append(state)
            if state in ("failed", "rejected", "cancelled"):
                failures += 1
        status = event.data.get("status")
        if event.kind == "status" and getattr(status, "error", None):
            failures += 1
    return TraceStatistics(
        total_events=len(checked),
        first_at=first_at,
        last_at=last_at,
        duration=max(0.0, (last_at - first_at)) if checked else 0.0,
        kinds=_ordered_counts([event.kind for event in checked]),
        owners=_ordered_counts([event.owner for event in checked if event.owner]),
        results=_ordered_counts(results),
        requests=sum(event.kind == "request" for event in checked),
        dispatches=sum(event.kind == "dispatch" for event in checked),
        failures=failures,
    )


def load_trace_document(
    path: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> TraceDocument:
    """Load a complete trace through the SDK's strict, bounded trace reader."""
    events = load_trace(path, max_bytes=max_bytes, max_events=max_events)
    return TraceDocument(events, trace_statistics(events), str(Path(path)))


def session_trace_document(
    session: Any, *, max_events: int = DEFAULT_MAX_EVENTS
) -> TraceDocument:
    """Take one read-only snapshot of a session's bounded in-memory history."""
    if type(max_events) is not int or max_events < 1:
        raise ValueError("max_events must be a positive integer")
    callback = getattr(session, "events", None)
    if not callable(callback):
        raise TypeError("session must provide events()")
    events = _validated_events(tuple(callback()), require_session_start=False)
    if len(events) > max_events:
        raise TraceError("session event limit exceeded")
    return TraceDocument(events, trace_statistics(events), "live session history")


def export_session_trace(
    session: Any,
    path: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> int:
    """Export complete retained session history using the existing v1 recorder.

    Session history is bounded.  If its first events have already rolled off,
    exporting would create a misleading partial recording, so it is rejected
    before the destination file is created.
    """
    document = session_trace_document(session, max_events=max_events)
    events = _validated_events(document.events, require_session_start=True)
    with JsonlRecorder(path, max_bytes=max_bytes) as recorder:
        for event in events:
            recorder.record(event)
    return len(events)


def _validated_events(
    events: Sequence[Event], *, require_session_start: bool
) -> tuple[Event, ...]:
    checked = tuple(events)
    if not checked:
        return checked
    first = checked[0].sequence
    if require_session_start and first != 1:
        raise TraceError(
            "session history is incomplete: event 1 is no longer retained; "
            "attach JsonlRecorder when the session starts"
        )
    for offset, event in enumerate(checked):
        if not isinstance(event, Event) or event.sequence != first + offset:
            raise TraceError("event history must be contiguous and ordered")
    return checked


def _request_terms(event: Event) -> tuple[str, ...]:
    terms: list[str] = []
    for key in ("request_id", "key"):
        value = event.data.get(key)
        if isinstance(value, str):
            terms.append(value)
    request = event.data.get("request")
    if isinstance(request, Request):
        terms.extend((request.key, request.reason, request.action.kind.value))
    result = event.data.get("result")
    if isinstance(result, ActionResult):
        terms.extend(
            (result.request_id, result.key, result.state.value, result.reason)
        )
    step = event.data.get("step")
    if isinstance(step, Step):
        for item in step.requests:
            terms.extend((item.key, item.reason, item.action.kind.value))
    return tuple(term for term in terms if term)


def request_label(event: Event) -> str:
    """Return the most useful request identity shown in the timeline."""
    request_id = event.data.get("request_id")
    result = event.data.get("result")
    request = event.data.get("request")
    if not isinstance(request_id, str) and isinstance(result, ActionResult):
        request_id = result.request_id
    key = event.data.get("key")
    if not isinstance(key, str) and isinstance(request, Request):
        key = request.key
    if not isinstance(key, str) and isinstance(result, ActionResult):
        key = result.key
    if isinstance(request_id, str) and isinstance(key, str):
        return f"{request_id} / {key}"
    return request_id if isinstance(request_id, str) else key if isinstance(key, str) else ""


def event_matches(
    event: Event, *, kind: str = "", owner: str = "", request: str = ""
) -> bool:
    """Apply case-insensitive kind, owner, and request filters."""
    kind = kind.strip().casefold()
    owner = owner.strip().casefold()
    request = request.strip().casefold()
    if kind and event.kind.casefold() != kind:
        return False
    if owner and owner not in event.owner.casefold():
        return False
    if request and not any(request in term.casefold() for term in _request_terms(event)):
        return False
    return True


def filter_events(
    events: Sequence[Event],
    *,
    kind: str = "",
    owner: str = "",
    request: str = "",
    limit: int = DEFAULT_PRESENTATION_LIMIT,
) -> FilteredEvents:
    """Build a bounded timeline while still reporting the full match count."""
    if type(limit) is not int or not 1 <= limit <= MAX_PRESENTATION_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_PRESENTATION_LIMIT}")
    checked = _validated_events(events, require_session_start=False)
    origin = checked[0].at if checked else 0.0
    rows: list[EventRow] = []
    matched = 0
    for event in checked:
        if not event_matches(event, kind=kind, owner=owner, request=request):
            continue
        matched += 1
        if len(rows) < limit:
            rows.append(
                EventRow(
                    event,
                    max(0.0, event.at - origin),
                    request_label(event),
                    event_summary(event),
                )
            )
    return FilteredEvents(tuple(rows), matched, limit)


def event_summary(event: Event) -> str:
    """Create a compact event-specific timeline description."""
    data = event.data
    if event.kind == "tick":
        return "scheduler tick"
    if event.kind == "observation":
        snapshot = data.get("snapshot")
        if isinstance(snapshot, Snapshot):
            known = sum(
                getattr(item.quality, "value", item.quality) == "known"
                for item in snapshot.observations.values()
            )
            return (
                f"frame {snapshot.frame_id}, epoch {snapshot.epoch}; "
                f"{known}/{len(snapshot.observations)} known"
            )
    if event.kind == "status":
        status = data.get("status")
        phase = getattr(status, "phase", "")
        reason = data.get("reason", "")
        error = getattr(status, "error", None)
        return _join_summary(phase, reason, error)
    if event.kind == "decision":
        step = data.get("step")
        if isinstance(step, Step):
            counts = f"{len(step.requests)} request(s), {len(step.cancel_keys)} cancellation(s)"
            return _join_summary(step.phase, counts, step.error)
    if event.kind == "request":
        request = data.get("request")
        if isinstance(request, Request):
            return _join_summary(
                f"{request.key}: {request.action.kind.value}", request.reason
            )
    if event.kind == "queue":
        return _join_summary(
            f"queued {data.get('key', '')}",
            f"priority {data.get('priority', '?')}",
        )
    if event.kind == "dispatch":
        return _join_summary(
            f"dispatch {data.get('key', '')}", str(data.get("action", ""))
        )
    if event.kind == "result":
        result = data.get("result")
        if isinstance(result, ActionResult):
            suffix = "final" if result.final else "awaiting confirmation"
            return _join_summary(
                f"{result.key}: {result.state.value}", suffix, result.reason
            )
    keys = ", ".join(sorted(str(key) for key in data)[:8])
    return f"data: {keys}" if keys else ""


def _join_summary(*parts: Any) -> str:
    return "; ".join(str(part).strip() for part in parts if str(part or "").strip())


def event_detail(event: Event, *, max_chars: int = MAX_DETAIL_CHARS) -> str:
    """Return bounded inert JSON suitable for a text pane or terminal."""
    if type(max_chars) is not int or max_chars < 256:
        raise ValueError("max_chars must be an integer of at least 256")
    value = _present(event)
    rendered = json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True)
    if len(rendered) <= max_chars:
        return rendered
    marker = "\n... detail truncated ..."
    return rendered[: max_chars - len(marker)] + marker


def _present(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_VALUE_DEPTH:
        return "<maximum detail depth reached>"
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, str):
        if len(value) <= _MAX_STRING_CHARS:
            return value
        return value[:_MAX_STRING_CHARS] + "... <text truncated>"
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "type": type(value).__name__,
            **{
                field.name: _present(getattr(value, field.name), depth + 1)
                for field in fields(value)
            },
        }
    if isinstance(value, Mapping):
        items = list(value.items())
        shown = items[:_MAX_VALUE_ITEMS]
        if all(isinstance(key, str) for key, _ in shown):
            result = {key: _present(item, depth + 1) for key, item in shown}
            if len(items) > len(shown):
                result["<truncated>"] = f"{len(items) - len(shown)} more entries"
            return result
        result = [
            {"key": _present(key, depth + 1), "value": _present(item, depth + 1)}
            for key, item in shown
        ]
        if len(items) > len(shown):
            result.append({"truncated": len(items) - len(shown)})
        return result
    if isinstance(value, (tuple, list)):
        result = [_present(item, depth + 1) for item in value[:_MAX_VALUE_ITEMS]]
        if len(value) > len(result):
            result.append(f"<{len(value) - len(result)} more items>")
        return result
    return f"<{type(value).__name__}>"


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_EVENTS",
    "DEFAULT_PRESENTATION_LIMIT",
    "MAX_DETAIL_CHARS",
    "MAX_PRESENTATION_LIMIT",
    "EventRow",
    "FilteredEvents",
    "TraceDocument",
    "TraceStatistics",
    "event_detail",
    "event_matches",
    "event_summary",
    "export_session_trace",
    "filter_events",
    "load_trace_document",
    "request_label",
    "session_trace_document",
    "trace_statistics",
]
