"""Shared, generation-safe runtime for client automation features.

The session owns one perception snapshot and one priority input queue.  Feature
objects are policies: they never capture the client or emit native input
directly.  This keeps observation, scheduling, cancellation, and audit events
at one enforceable boundary.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
import inspect
import itertools
import math
from types import MappingProxyType
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Mapping
import weakref

from core import runtime
from tibia_client.diagnostics import (
    ActionDiagnostic,
    AdapterCapabilities,
    CaptureHealth,
    ResultDiagnostic,
    SessionDiagnostics,
)
from tibia_client.models import (
    ActionKind,
    ActionResult,
    Event,
    FeatureContext,
    FeatureStatus,
    Observation,
    ObservationBatch,
    Priority,
    Quality,
    Reading,
    Request,
    Requirement,
    ResultState,
    Sensor,
    Snapshot,
    Step,
)
from tibia_client.ports import ClientAdapter, Clock, Feature

if TYPE_CHECKING:
    from core.perception import Frame


_SESSION_IDS = itertools.count(1)


def _positive(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a real number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return result


def _capacity(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


class _SystemClock:
    monotonic = staticmethod(time.monotonic)


class _CombinedToken:
    """Cancellation token scoped to one feature generation and the session."""

    def __init__(
        self,
        session_token: runtime.CancellationToken,
        cancelled: threading.Event,
        name: str,
        generation: int,
    ) -> None:
        self._session_token = session_token
        self._feature_cancelled = cancelled
        self.name = name
        self.generation = generation

    @property
    def cancelled(self) -> bool:
        return self._session_token.cancelled or self._feature_cancelled.is_set()

    def checkpoint(self) -> None:
        if self.cancelled:
            raise runtime.CancelledError(
                f"feature {self.name!r} generation {self.generation} was cancelled"
            )

    def wait(self, seconds: float) -> None:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise TypeError("seconds must be a real number")
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("seconds must be finite and non-negative")
        deadline = time.monotonic() + seconds
        self.checkpoint()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._feature_cancelled.wait(min(remaining, 0.02))
            self.checkpoint()
        self.checkpoint()


@dataclass
class _FeatureRecord:
    feature: Feature
    name: str
    generation: int
    interval: float
    next_due: float
    phase: str = "Starting"
    active: bool = True
    error: str | None = None
    sensors: tuple[Sensor, ...] = ()
    cancelled: threading.Event = field(default_factory=threading.Event)
    callback_lock: threading.RLock = field(default_factory=threading.RLock)
    paused: bool = False
    paused_at: float | None = None
    transition: str | None = None


@dataclass(frozen=True)
class _Binding:
    requirement: Requirement
    observation: Observation
    value: Any


@dataclass
class _Queued:
    request_id: str
    owner: str
    generation: int
    request: Request
    enqueued_at: float
    deadline: float
    order: int
    bindings: tuple[_Binding, ...]
    nonfinal_emitted: bool = False
    finalized: bool = False

    @property
    def effective_priority(self) -> int:
        return max(int(Priority.BACKGROUND), min(int(Priority.HEAL), self.request.priority))


@dataclass
class _PendingConfirmation:
    queued: _Queued
    executed_at: float
    executed_frame_id: int
    deadline: float


class ClientSession:
    """Coordinate features over one capture and one serialized action stream.

    ``autostart=False`` leaves scheduling entirely under the caller's control;
    each :meth:`tick_once` performs at most one capture and one action.
    """

    # Feature hooks are required to be short, but a broken hook must not hang a
    # UI stop/replacement forever.  Cancellation takes effect before this wait;
    # a timeout then invalidates the generation and is reported to the caller.
    _CALLBACK_QUIESCE_TIMEOUT = 1.0

    def __init__(
        self,
        adapter: ClientAdapter,
        *,
        clock: Clock | None = None,
        tick_interval: float = 0.02,
        recorder: Callable[[Event], None] | Any | None = None,
        event_history: int = 2048,
        queue_limit: int = 256,
        ready_timeout: float = 30.0,
        activation_guard: Callable[[Feature], object] | None = None,
        autostart: bool = True,
    ) -> None:
        for method in ("ready", "observe", "execute", "close"):
            if not callable(getattr(adapter, method, None)):
                raise TypeError(f"adapter must provide {method}()")
        if clock is not None and not callable(getattr(clock, "monotonic", None)):
            raise TypeError("clock must provide monotonic()")
        if recorder is not None and not (
            callable(recorder)
            or any(callable(getattr(recorder, name, None)) for name in ("record", "append", "emit"))
        ):
            raise TypeError("recorder must be callable or provide record/append/emit")
        if activation_guard is not None and not callable(activation_guard):
            raise TypeError("activation_guard must be callable")

        self._adapter = adapter
        self._clock = clock or _SystemClock()
        self._tick_interval = _positive(tick_interval, "tick interval")
        self._ready_timeout = _positive(ready_timeout, "ready timeout")
        self._queue_limit = _capacity(queue_limit, "queue limit")
        self._autostart = bool(autostart)
        self._recorder = recorder
        self._activation_guard = activation_guard

        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._event_lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._recorder_context = threading.local()
        self._records: dict[str, _FeatureRecord] = {}
        self._generations: dict[str, int] = {}
        self._feature_instances: list[weakref.ReferenceType[Any]] = []
        self._queue: list[_Queued] = []
        self._confirming: dict[str, _PendingConfirmation] = {}
        self._history: deque[Event] = deque(maxlen=_capacity(event_history, "event history"))
        self._sequence = 0
        self._request_sequence = 0
        self._order = 0
        self._frame_id = 0
        self._epoch = 1
        # A broken injected clock must not be consulted again while failing
        # closed.  Keep the last validated value for terminal/status events.
        self._last_now = time.monotonic()
        self._snapshot = Snapshot(epoch=self._epoch, created_at=self._now())
        self._closed = False
        self._closed_reason: str | None = None
        self._ready = False
        self._ready_started_at: float | None = None
        self._runtime_name = f"tibia-client-session-{next(_SESSION_IDS)}"
        self._manual_token = runtime.CancellationToken(self._runtime_name, 1)
        self._loop_token: runtime.CancellationToken | None = None
        self._adapter_closed = False
        self._close_error: BaseException | None = None
        self._close_status_recorded = False
        self._recorder_error: BaseException | None = None
        self._stopping_all = False
        # Unlike ``_stopping_all``, this remains changed after Stop All returns.
        # A start that was already waiting on a callback can therefore tell that
        # it crossed a completed stop boundary and must not install itself.
        self._stop_epoch = 0
        self._readiness_reason: str | None = "not checked"
        self._profile_allowed: bool | None = None
        self._profile_reasons: tuple[str, ...] = ()
        self._capture_transport_ok: bool | None = None
        self._capture_visual_valid: bool | None = None
        self._capture_at: float | None = None
        self._capture_reasons: tuple[str, ...] = ()
        self._last_action: ActionDiagnostic | None = None
        self._last_result: ResultDiagnostic | None = None
        self._preview_frame: Frame | None = None
        self._failure_reasons: deque[str] = deque(maxlen=16)

    def _now(self) -> float:
        try:
            value = self._clock.monotonic()
        except runtime.CancelledError as exc:
            # Cancellation belongs to runtime tokens, not the injected clock.
            # Normalize a clock that misuses it into an ordinary clock failure
            # so the session still fails closed with truthful feature status.
            raise RuntimeError("clock.monotonic() raised cancellation") from exc
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("clock.monotonic() must return a real number")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("clock.monotonic() must return a finite value")
        with self._lock:
            self._last_now = value
        return value

    def _failure_time(self, fallback: float | None = None) -> float:
        """Return a timestamp without consulting a clock that may be broken."""
        if (
            fallback is not None
            and not isinstance(fallback, bool)
            and isinstance(fallback, (int, float))
            and math.isfinite(float(fallback))
        ):
            return float(fallback)
        with self._lock:
            return self._last_now

    def _guard_recorder_reentry(self, operation: str) -> None:
        if getattr(self._recorder_context, "depth", 0):
            raise RuntimeError(
                "recorder callbacks are observer-only and cannot " + operation
            )

    @contextmanager
    def _recorder_callback(self) -> Any:
        depth = getattr(self._recorder_context, "depth", 0)
        self._recorder_context.depth = depth + 1
        try:
            yield
        finally:
            self._recorder_context.depth = depth

    @property
    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    @property
    def active_features(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                sorted(
                    name for name, record in self._records.items()
                    if record.active and not record.paused and not record.cancelled.is_set()
                )
            )

    @property
    def statuses(self) -> Mapping[str, FeatureStatus]:
        with self._lock:
            result = {
                name: FeatureStatus(
                    name=name,
                    generation=record.generation,
                    active=record.active and not record.cancelled.is_set(),
                    phase=record.phase,
                    error=record.error,
                )
                for name, record in self._records.items()
            }
        return MappingProxyType(result)

    @property
    def running(self) -> bool:
        worker = runtime.status(self._runtime_name)
        return bool(worker and worker["alive"])

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def close_error(self) -> BaseException | None:
        with self._lock:
            return self._close_error

    @staticmethod
    def _is_probe(feature: object) -> bool:
        try:
            from tibia_client.compatibility import ProbeFeature
        except ImportError:
            return False
        return type(feature) is ProbeFeature

    @staticmethod
    def _required_operations(feature: Feature) -> tuple[str, ...]:
        callback = getattr(feature, "required_operations", None)
        if callback is None:
            return ()
        if not callable(callback):
            raise TypeError("feature.required_operations must be callable")
        operations = callback()
        if not isinstance(operations, tuple):
            raise TypeError("feature.required_operations() must return a tuple")
        if any(not isinstance(item, str) or not item.strip() for item in operations):
            raise ValueError("required operations must be non-empty text")
        return tuple(dict.fromkeys(item.strip() for item in operations))

    def _adapter_capabilities(self) -> AdapterCapabilities:
        callback = getattr(self._adapter, "capabilities", None)
        if not callable(callback):
            return AdapterCapabilities()
        value = callback()
        if isinstance(value, AdapterCapabilities):
            return value
        operations = tuple(getattr(value, "operations", ()))
        sensors = tuple(getattr(value, "sensors", ()))
        return AdapterCapabilities(operations, sensors)

    @staticmethod
    def _guard_result(result: object) -> tuple[bool, tuple[str, ...]]:
        if result is None or result is True:
            return True, ()
        if result is False:
            return False, ("active compatibility profile rejected this feature",)
        if isinstance(result, str):
            return False, (result.strip() or "active compatibility profile rejected this feature",)
        if hasattr(result, "allowed"):
            allowed = bool(getattr(result, "allowed"))
            reasons = tuple(str(item) for item in getattr(result, "reasons", ()) if str(item))
            return allowed, reasons if reasons else (() if allowed else ("profile requirements are not satisfied",))
        try:
            reasons = tuple(str(item) for item in result if str(item))  # type: ignore[union-attr]
        except TypeError:
            raise TypeError("activation_guard returned an unsupported result") from None
        return not reasons, reasons

    def _activation_reasons(
        self, feature: Feature, *, bypass_profile: bool = False, update: bool = True
    ) -> tuple[str, ...]:
        required = self._required_operations(feature)
        capabilities = self._adapter_capabilities()
        implementation_reasons: tuple[str, ...] = ()
        if capabilities.operations:
            missing = tuple(op for op in required if op not in capabilities.operations)
            implementation_reasons = tuple(
                f"adapter does not implement required operation {op!r}" for op in missing
            )
        allowed = not implementation_reasons
        reasons = implementation_reasons
        if allowed:
            guard: Callable[[], object] | None = None
            label = "activation guard"
            if bypass_profile:
                provider = getattr(feature, "probe_context_check", None)
                if callable(provider):
                    guard = provider
                    label = "probe context guard"
            elif self._activation_guard is not None:
                guard = lambda: self._activation_guard(feature)
            if guard is not None:
                try:
                    allowed, reasons = self._guard_result(guard())
                except Exception as exc:
                    allowed = False
                    reasons = (f"{label} failed: {type(exc).__name__}: {exc}",)
        if update:
            with self._lock:
                self._profile_allowed = (
                    allowed
                    if (
                        self._activation_guard is not None
                        or bypass_profile
                        or implementation_reasons
                    )
                    else None
                )
                self._profile_reasons = reasons
                if not allowed:
                    self._preview_frame = None
        return () if allowed else reasons

    def diagnostics(self) -> SessionDiagnostics:
        """Return a bounded immutable view without invoking capture or input."""

        try:
            value = self._clock.monotonic()
            now = float(value)
            if not math.isfinite(now):
                raise ValueError
        except Exception:
            with self._lock:
                now = self._last_now
        with self._lock:
            captured_at = self._capture_at
            age = None if captured_at is None else max(0.0, now - captured_at)
            capture_reasons = self._capture_reasons
            if self._capture_transport_ok is None:
                capture_reasons = ("no capture has completed",)
            active = tuple(sorted(
                record.name for record in self._records.values()
                if record.active and not record.paused and not record.cancelled.is_set()
            ))
            paused = tuple(sorted(
                record.name for record in self._records.values()
                if record.active and record.paused
            ))
            return SessionDiagnostics(
                ready=self._ready,
                readiness_reason=self._readiness_reason,
                profile_allowed=self._profile_allowed,
                profile_reasons=self._profile_reasons,
                active_features=active,
                paused_features=paused,
                capture=CaptureHealth(
                    transport_ok=self._capture_transport_ok,
                    captured_at=captured_at,
                    captured_age=age,
                    visual_valid=self._capture_visual_valid,
                    reasons=capture_reasons,
                ),
                last_action=self._last_action,
                last_result=self._last_result,
                failure_reasons=tuple(self._failure_reasons),
                capabilities=self._adapter_capabilities(),
                closed=self._closed,
            )

    def probe_evidence_source(self) -> str:
        """Return live only for an adapter-authenticated native composition."""

        value = getattr(self._adapter, "evidence_source", "simulation")
        if callable(value):
            value = value()
        return "live" if value == "live" else "simulation"

    def preview_frame(self) -> Frame | None:
        """Return the last immutable successful frame without triggering capture."""

        with self._lock:
            return self._preview_frame

    def feature_status(self, name: str) -> FeatureStatus:
        if not isinstance(name, str) or not name:
            raise ValueError("feature name must be non-empty")
        with self._lock:
            record = self._records.get(name)
            if record is None:
                return FeatureStatus(
                    name=name,
                    generation=self._generations.get(name, 0),
                    active=False,
                    phase="Stopped",
                )
            return FeatureStatus(
                name=name,
                generation=record.generation,
                active=record.active and not record.cancelled.is_set(),
                phase=record.phase,
                error=record.error,
            )

    def is_active(self, name: str) -> bool:
        return self.feature_status(name).active

    def events(self, after: int = 0) -> tuple[Event, ...]:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after must be a non-negative integer")
        with self._lock:
            return tuple(event for event in self._history if event.sequence > after)

    def _record(self, kind: str, at: float, owner: str = "", **data: Any) -> Event:
        # Event payloads contain only small domain metadata.  ObservationBatch.frame
        # is deliberately never retained here.
        # Sequence assignment and sink delivery share a lock so concurrent UI
        # lifecycle events cannot overtake events emitted by the runtime loop.
        # The registry lock is released before recorder I/O.
        with self._event_lock:
            with self._lock:
                self._sequence += 1
                event = Event(self._sequence, at, kind, owner, data)
                self._history.append(event)
            sink = self._recorder
            if sink is not None:
                try:
                    with self._recorder_callback():
                        if callable(sink):
                            sink(event)
                        else:
                            for method in ("record", "append", "emit"):
                                callback = getattr(sink, method, None)
                                if callable(callback):
                                    callback(event)
                                    break
                except Exception as exc:
                    # Preserve the in-memory event, then prevent the next native
                    # dispatch: configured audit output must not silently develop
                    # a hole while automation continues.
                    with self._lock:
                        if self._recorder_error is None:
                            self._recorder_error = exc
            return event

    def start_feature(self, feature: Feature) -> int:
        self._guard_recorder_reentry("start or replace a feature")
        if self._is_probe(feature):
            raise ValueError("ProbeFeature must be started with start_probe()")
        name = getattr(feature, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("feature.name must be non-empty text")
        name = name.strip()
        _positive(getattr(feature, "interval", None), "feature interval")
        for method in ("sensors", "tick", "on_result"):
            if not callable(getattr(feature, method, None)):
                raise TypeError(f"feature must provide {method}()")
        try:
            weakref.ref(feature)
        except TypeError:
            raise TypeError("feature instances must support weak references") from None
        # Probe exclusivity is a lifecycle invariant and takes precedence over
        # ordinary profile-gate reporting.
        with self._lock:
            if self._closed:
                detail = f": {self._closed_reason}" if self._closed_reason else ""
                raise RuntimeError(f"client session is closed{detail}")
            if self._stopping_all:
                raise RuntimeError("stop all automation is in progress")
            stop_epoch = self._stop_epoch
            if any(
                record.active and self._is_probe(record.feature)
                for record in self._records.values()
            ):
                raise RuntimeError("automation cannot start while a compatibility probe is active")
        reasons = self._activation_reasons(feature)
        if reasons:
            raise RuntimeError("activation blocked: " + "; ".join(reasons))

        while True:
            with self._lock:
                if self._closed:
                    detail = f": {self._closed_reason}" if self._closed_reason else ""
                    raise RuntimeError(f"client session is closed{detail}")
                if self._stopping_all:
                    raise RuntimeError("stop all automation is in progress")
                if self._stop_epoch != stop_epoch:
                    raise RuntimeError("stop all automation completed while feature was starting")
                if any(
                    record.active and self._is_probe(record.feature)
                    for record in self._records.values()
                ):
                    raise RuntimeError("automation cannot start while a compatibility probe is active")
                previous = self._records.get(name)
                if (
                    previous is not None
                    and previous.active
                    and not previous.cancelled.is_set()
                    and previous.feature is feature
                ):
                    return previous.generation
                self._feature_instances = [
                    reference for reference in self._feature_instances
                    if reference() is not None
                ]
                if any(reference() is feature for reference in self._feature_instances):
                    raise ValueError("a feature instance cannot be reused across generations")
                if previous is not None and previous.active:
                    # Invalidate dispatch immediately; quiescing pure feature
                    # code can then wait without leaving native input enabled.
                    previous.cancelled.set()
                    previous.phase = "Superseding"
            barrier = self._callback_barrier(previous, "replace")
            with barrier:
                with self._lifecycle_lock:
                    with self._lock:
                        if self._records.get(name) is not previous:
                            continue
                        if self._stopping_all:
                            raise RuntimeError("stop all automation is in progress")
                        if any(
                            record.active and self._is_probe(record.feature)
                            for record in self._records.values()
                        ):
                            raise RuntimeError(
                                "automation cannot start while a compatibility probe is active"
                            )
                    return self._start_feature(feature, stop_epoch=stop_epoch)

    def start_probe(self, feature: Feature) -> int:
        """Start the one concrete compatibility probe in exclusive mode."""

        self._guard_recorder_reentry("start a compatibility probe")
        try:
            from tibia_client.compatibility import ProbeFeature
        except ImportError as exc:
            raise RuntimeError("compatibility probe support is unavailable") from exc
        if type(feature) is not ProbeFeature:
            raise TypeError("start_probe() requires tibia_client.compatibility.ProbeFeature")
        with self._lock:
            if self._closed:
                raise RuntimeError("client session is closed")
            if self._stopping_all:
                raise RuntimeError("stop all automation is in progress")
            stop_epoch = self._stop_epoch
        reasons = self._activation_reasons(feature, bypass_profile=True)
        if reasons:
            raise RuntimeError("probe activation blocked: " + "; ".join(reasons))
        with self._lifecycle_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("client session is closed")
                if self._stopping_all:
                    raise RuntimeError("stop all automation is in progress")
                if self._stop_epoch != stop_epoch:
                    raise RuntimeError("stop all automation completed while probe was starting")
                if any(record.active for record in self._records.values()):
                    raise RuntimeError("compatibility probes require all automation to be stopped")
            return self._start_feature(feature, stop_epoch=stop_epoch)

    def _start_feature(self, feature: Feature, *, stop_epoch: int) -> int:
        name = getattr(feature, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("feature.name must be non-empty text")
        name = name.strip()
        interval = _positive(getattr(feature, "interval", None), "feature interval")
        for method in ("sensors", "tick", "on_result"):
            if not callable(getattr(feature, method, None)):
                raise TypeError(f"feature must provide {method}()")

        # Synchronize with any callback already in progress before replacing its
        # generation.  The session registry lock is never held for this wait.
        with self._lock:
            if self._closed:
                detail = f": {self._closed_reason}" if self._closed_reason else ""
                raise RuntimeError(f"client session is closed{detail}")
            if self._stopping_all or self._stop_epoch != stop_epoch:
                raise RuntimeError("stop all automation completed while feature was starting")
            previous = self._records.get(name)
            if (
                previous is not None
                and previous.active
                and not previous.cancelled.is_set()
                and previous.feature is feature
            ):
                return previous.generation
            self._feature_instances = [
                reference for reference in self._feature_instances
                if reference() is not None
            ]
            if any(reference() is feature for reference in self._feature_instances):
                raise ValueError("a feature instance cannot be reused across generations")
            had_active = any(record.active for record in self._records.values())
        try:
            feature_reference = weakref.ref(feature)
        except TypeError:
            raise TypeError("feature instances must support weak references") from None
        try:
            if previous is not None:
                self._invalidate_record(previous, "superseded", self._now())
            now = self._now()
        except runtime.CancelledError:
            raise
        except Exception as exc:
            self._fatal(exc, self._failure_time())
            raise
        with self._lock:
            if self._closed:
                detail = f": {self._closed_reason}" if self._closed_reason else ""
                raise RuntimeError(f"client session is closed{detail}")
            if self._stopping_all or self._stop_epoch != stop_epoch:
                raise RuntimeError("stop all automation completed while feature was starting")
            generation = self._generations.get(name, 0) + 1
            self._generations[name] = generation
            self._feature_instances.append(feature_reference)
            if not had_active:
                self._ready = False
                self._ready_started_at = None
            record = _FeatureRecord(feature, name, generation, interval, now)
            self._records[name] = record
        self._record(
            "status", now, name,
            status=FeatureStatus(name, generation, True, "Starting"),
            reason="started",
        )
        if self._autostart:
            runtime.start(self._runtime_name, self._run)
        return generation

    def stop_feature(
        self, name: str, *, expected_generation: int | None = None
    ) -> bool:
        self._guard_recorder_reentry("stop a feature")
        if not isinstance(name, str) or not name:
            raise ValueError("feature name must be non-empty")
        with self._lock:
            record = self._records.get(name)
            if record is None or not record.active:
                return False
            if (
                expected_generation is not None
                and (
                    type(expected_generation) is not int
                    or record.generation != expected_generation
                )
            ):
                return False
            # Native actions see cancellation before stop waits for a feature
            # callback, sensor calculation, or policy tick to return.
            record.cancelled.set()
            record.phase = "Stopping"
        with self._callback_barrier(record, "stop"):
            with self._lifecycle_lock:
                with self._lock:
                    if self._records.get(name) is not record:
                        return False
                return self._stop_feature(name)

    @contextmanager
    def _callback_barrier(
        self, record: _FeatureRecord | None, operation: str
    ) -> Any:
        if record is None:
            yield
            return
        if record.callback_lock.acquire(timeout=self._CALLBACK_QUIESCE_TIMEOUT):
            try:
                yield
            finally:
                record.callback_lock.release()
            return

        error = TimeoutError(
            f"feature {record.name!r} generation {record.generation} callback "
            f"did not quiesce within {self._CALLBACK_QUIESCE_TIMEOUT:g}s during {operation}"
        )
        try:
            now = self._now()
        except runtime.CancelledError:
            raise
        except Exception:
            now = self._failure_time()
        with self._lock:
            if self._records.get(record.name) is record and record.active:
                record.phase = "Failed"
                record.error = str(error)
        # The callback may still be unwinding, but its post-callback generation
        # check cannot commit a Step/result after this invalidation.
        self._invalidate_record(record, "callback quiescence timeout", now)
        raise error

    def _stop_feature(self, name: str) -> bool:
        if not isinstance(name, str) or not name:
            raise ValueError("feature name must be non-empty")
        with self._lock:
            record = self._records.get(name)
        if record is None or not record.active:
            return False
        try:
            now = self._now()
        except runtime.CancelledError:
            raise
        except Exception as exc:
            self._fatal(exc, self._failure_time())
            raise
        return self._invalidate_record(record, "stopped", now)

    def stop_all(self, reason: str = "stopped by operator") -> tuple[str, ...]:
        """Stop every generation without closing the reusable adapter.

        Cancellation is visible to in-flight adapter calls before this method
        waits for callbacks.  A native call already executing may return, but
        its generation can no longer publish work or dispatch another action.
        """

        self._guard_recorder_reentry("stop all automation")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("stop reason must be non-empty text")
        reason = reason.strip()
        with self._lock:
            if self._closed:
                return ()
            if self._stopping_all:
                return ()
            self._stopping_all = True
            self._stop_epoch += 1
            records = [record for record in self._records.values() if record.active]
            for record in records:
                record.cancelled.set()
                record.phase = "Stopping"

        quiescence_errors: list[str] = []
        acquired: list[_FeatureRecord] = []
        lifecycle_acquired = False
        try:
            for record in records:
                if record.callback_lock.acquire(timeout=self._CALLBACK_QUIESCE_TIMEOUT):
                    acquired.append(record)
                else:
                    quiescence_errors.append(
                        f"{record.name} callback did not quiesce during stop all"
                    )
            self._lifecycle_lock.acquire()
            lifecycle_acquired = True
            with self._lock:
                if self._closed:
                    return ()
            try:
                now = self._now()
            except Exception:
                now = self._failure_time()
            with self._lock:
                current = [
                    record for record in records
                    if self._records.get(record.name) is record and record.active
                ]
                for record in current:
                    record.active = False
                    record.paused = False
                    record.paused_at = None
                    record.transition = None
                    record.phase = "Stopped"
                    record.error = None
                removed = list(self._queue)
                removed.extend(value.queued for value in self._confirming.values())
                self._queue.clear()
                self._confirming.clear()
                self._ready = False
                self._readiness_reason = "automation stopped; restart requires readiness"
                self._ready_started_at = None
                self._epoch += 1
                self._snapshot = Snapshot({}, self._frame_id, self._epoch, now)
                self._preview_frame = None
                if quiescence_errors:
                    self._failure_reasons.extend(quiescence_errors)
            for item in removed:
                self._terminal(item, ResultState.CANCELLED, now, reason, deliver=False)
            for record in current:
                self._record(
                    "status", now, record.name,
                    status=FeatureStatus(record.name, record.generation, False, "Stopped"),
                    reason=reason,
                )
            self._record(
                "status", now,
                status=FeatureStatus("ClientSession", 0, False, "Stopped"),
                reason=reason,
            )
            return tuple(sorted(record.name for record in current))
        finally:
            for record in reversed(acquired):
                record.callback_lock.release()
            if lifecycle_acquired:
                self._lifecycle_lock.release()
            with self._lock:
                self._stopping_all = False

    def pause_navigation(self) -> bool:
        """Pause CaveBot only; independent support features keep running."""

        self._guard_recorder_reentry("pause navigation")
        with self._lock:
            if self._stopping_all:
                return False
            record = self._records.get("CaveBot")
            if (
                record is None
                or not record.active
                or record.paused
                or record.transition is not None
            ):
                return False
            # Cancellation is immediate.  The feature-owned hook runs later
            # without the lifecycle lock, so Stop All can still finish within
            # its bounded callback wait if that hook is broken.
            record.transition = "pause"
            record.paused = True
            record.cancelled.set()
            record.phase = "Paused"
        try:
            paused_at = self._now()
        except Exception:
            paused_at = self._failure_time()
        with self._lock:
            if (
                self._records.get(record.name) is record
                and record.active
                and record.transition == "pause"
            ):
                # Freeze feature time from the operator's pause request, not
                # from the end of a potentially slow callback-quiescence wait.
                record.paused_at = paused_at
        try:
            with self._callback_barrier(record, "pause navigation"):
                try:
                    now = self._now()
                except Exception:
                    now = self._failure_time()
                with self._lock:
                    if (
                        self._records.get(record.name) is not record
                        or not record.active
                        or record.transition != "pause"
                    ):
                        return False
                    removed = [item for item in self._queue if self._owns(item, record)]
                    self._queue = [item for item in self._queue if not self._owns(item, record)]
                    pending = [
                        value for value in self._confirming.values()
                        if self._owns(value.queued, record)
                    ]
                    for value in pending:
                        self._confirming.pop(value.queued.request_id, None)
                    self._prune_snapshot_locked(now)
                for item in removed + [value.queued for value in pending]:
                    self._terminal(
                        item, ResultState.CANCELLED, now, "navigation paused", deliver=False
                    )
                self._call_feature_hook(record.feature, "on_pause", at=now)
        except Exception:
            with self._lock:
                if self._records.get(record.name) is record and record.active:
                    record.transition = None
                    record.phase = "Paused"
            raise
        with self._lifecycle_lock:
            with self._lock:
                if (
                    self._stopping_all
                    or self._records.get(record.name) is not record
                    or not record.active
                    or record.transition != "pause"
                ):
                    return False
                record.transition = None
                record.phase = "Paused"
        self._record(
            "status", now, record.name,
            status=FeatureStatus(record.name, record.generation, False, "Paused"),
            reason="navigation paused",
        )
        return True

    def resume_navigation(self) -> bool:
        """Resume the paused CaveBot generation after forcing fresh evidence."""

        self._guard_recorder_reentry("resume navigation")
        with self._lock:
            if self._stopping_all:
                return False
            record = self._records.get("CaveBot")
            if (
                record is None
                or not record.active
                or not record.paused
                or record.transition is not None
            ):
                return False
            record.transition = "resume"
        reasons = self._activation_reasons(record.feature)
        if reasons:
            with self._lock:
                if self._records.get(record.name) is record and record.active:
                    record.transition = None
            raise RuntimeError("activation blocked: " + "; ".join(reasons))
        try:
            now = self._now()
        except Exception as exc:
            with self._lock:
                if self._records.get(record.name) is record and record.active:
                    record.transition = None
            self._fatal(exc, self._failure_time())
            raise
        try:
            with self._callback_barrier(record, "resume navigation"):
                with self._lock:
                    if (
                        self._stopping_all
                        or self._records.get(record.name) is not record
                        or not record.active
                        or not record.paused
                        or record.transition != "resume"
                    ):
                        return False
                with self._lock:
                    paused_at = record.paused_at
                paused_for = 0.0 if paused_at is None else max(0.0, now - paused_at)
                self._call_feature_hook(
                    record.feature, "on_resume", at=now, paused_for=paused_for
                )
        except Exception:
            with self._lock:
                if self._records.get(record.name) is record and record.active:
                    record.transition = None
                    record.phase = "Paused"
            raise
        with self._lifecycle_lock:
            with self._lock:
                if (
                    self._stopping_all
                    or self._records.get(record.name) is not record
                    or not record.active
                    or not record.paused
                    or record.transition != "resume"
                ):
                    return False
                record.transition = None
                record.cancelled = threading.Event()
                record.paused = False
                record.paused_at = None
                record.next_due = now
                record.phase = "Resuming"
                self._ready = False
                self._ready_started_at = None
                self._preview_frame = None
        self._record(
            "status", now, record.name,
            status=FeatureStatus(record.name, record.generation, True, "Resuming"),
            reason="navigation resumed; fresh evidence required",
        )
        if self._autostart:
            runtime.start(self._runtime_name, self._run)
        return True

    @staticmethod
    def _call_feature_hook(feature: Feature, name: str, **metadata: float) -> None:
        """Invoke an optional hook, supplying only metadata it opts into."""

        callback = getattr(feature, name, None)
        if not callable(callback):
            return
        try:
            parameters = inspect.signature(callback).parameters
        except (TypeError, ValueError):
            callback()
            return
        accepts_metadata = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        kwargs = {
            key: value
            for key, value in metadata.items()
            if accepts_metadata
            or (
                key in parameters
                and parameters[key].kind is not inspect.Parameter.POSITIONAL_ONLY
            )
        }
        callback(**kwargs)

    def _invalidate_record(self, record: _FeatureRecord, reason: str, now: float) -> bool:
        with self._lock:
            current = self._records.get(record.name)
            if current is not record or not record.active:
                return False
            record.active = False
            if reason == "stopped":
                record.phase = "Stopped"
            elif reason == "superseded":
                record.phase = "Superseded"
            elif reason == "finished":
                if not record.phase or record.phase == "Running":
                    record.phase = "Finished"
                if record.error is None and record.phase.lower().startswith(("failed", "error")):
                    record.error = record.phase
            record.cancelled.set()
            removed = [item for item in self._queue if self._owns(item, record)]
            self._queue = [item for item in self._queue if not self._owns(item, record)]
            pending = [
                value for value in self._confirming.values() if self._owns(value.queued, record)
            ]
            for item in pending:
                self._confirming.pop(item.queued.request_id, None)
            self._prune_snapshot_locked(now)
        for item in removed + [value.queued for value in pending]:
            self._terminal(item, ResultState.CANCELLED, now, reason, deliver=False)
        self._record(
            "status", now, record.name,
            status=FeatureStatus(record.name, record.generation, False, record.phase, record.error),
            reason=reason,
        )
        return True

    @staticmethod
    def _owns(item: _Queued, record: _FeatureRecord) -> bool:
        return item.owner == record.name and item.generation == record.generation

    def _run(self) -> None:
        token = runtime.current_token()
        if token is None:
            raise RuntimeError("client runtime started without a cancellation token")
        with self._lock:
            self._loop_token = token
        try:
            while True:
                token.checkpoint()
                with self._lock:
                    if self._closed:
                        return
                self.tick_once()
                token.wait(self._tick_interval)
        finally:
            with self._lock:
                if self._loop_token is token:
                    self._loop_token = None

    def _session_token(self) -> runtime.CancellationToken:
        with self._lock:
            return self._loop_token or self._manual_token

    def tick_once(self) -> Snapshot:
        """Run one deterministic scheduler iteration and return its snapshot."""
        self._guard_recorder_reentry("run a scheduler tick")
        with self._tick_lock:
            try:
                return self._tick_once_locked()
            except runtime.CancelledError:
                raise
            except Exception as exc:
                self._fatal(exc, self._failure_time())
                return self.snapshot

    def _tick_once_locked(self) -> Snapshot:
        """Run one tick while the caller holds ``_tick_lock``."""
        with self._lock:
            if self._closed:
                return self._snapshot
            has_work = any(record.active for record in self._records.values())
            has_work = has_work or bool(self._queue) or bool(self._confirming)
        if not has_work:
            return self.snapshot

        now = self._now()
        self._record("tick", now)
        if not self._ensure_ready(now):
            return self.snapshot

        due, sensors = self._collect_due(now)
        # Every due policy gets a capture-health heartbeat, including features
        # such as timed spells that do not consume a visual sensor.  This keeps
        # a failed transport/layout from being bypassed by sensorless input.
        if due or sensors:
            if not self._observe(sensors, now):
                return self.snapshot
        snapshot = self.snapshot
        now = self._now()
        self._check_confirmations(snapshot, now)
        self._reject_stale_queued(snapshot, now)
        finished: list[_FeatureRecord] = []
        for record in due:
            if self._run_feature(record, snapshot, now):
                finished.append(record)
        dispatch_at = self._now()
        self._dispatch_one(dispatch_at)
        for record in finished:
            self._invalidate_record(record, "finished", self._now())
        return self.snapshot

    def _ensure_ready(self, now: float) -> bool:
        # Readiness is a current condition, not a successful-startup latch.
        # Native backends still recheck their target at the input boundary.
        with self._lock:
            if self._closed:
                return False
        try:
            ready = bool(self._adapter.ready())
        except runtime.CancelledError:
            raise
        except Exception as exc:
            self._fatal(exc, now)
            return False

        statuses: list[FeatureStatus] = []
        rejected: list[_Queued] = []
        unconfirmed: list[_Queued] = []
        with self._lock:
            if self._closed:
                return False
            was_ready = self._ready
            self._ready = ready
            self._readiness_reason = None if ready else "client target is not ready"
            records = [
                record for record in self._records.values()
                if self._is_current_locked(record)
            ]
            if ready:
                self._ready_started_at = None
                if not was_ready:
                    for record in records:
                        # Resume by observing and deciding again, even when the
                        # feature's ordinary interval has not elapsed yet.
                        record.next_due = min(record.next_due, now)
                        record.phase = "Running"
                        statuses.append(FeatureStatus(
                            record.name, record.generation, True, record.phase
                        ))
                started = None
            else:
                if self._ready_started_at is None:
                    self._ready_started_at = now
                started = self._ready_started_at
                if was_ready:
                    self._epoch += 1
                    self._snapshot = Snapshot({}, self._frame_id, self._epoch, now)
                    self._preview_frame = None
                    rejected = list(self._queue)
                    unconfirmed = [value.queued for value in self._confirming.values()]
                    self._queue.clear()
                    self._confirming.clear()
                for record in records:
                    if record.phase != "Waiting for game focus":
                        record.phase = "Waiting for game focus"
                        statuses.append(FeatureStatus(
                            record.name, record.generation, True, record.phase
                        ))

        for status in statuses:
            self._record(
                "status", now, status.name, status=status,
                reason="ready" if ready else "not ready",
            )
        for item in rejected:
            self._terminal(item, ResultState.REJECTED, now, "client readiness lost")
        for item in unconfirmed:
            # Input already happened; its outcome is now unknown. Do not imply
            # that cancellation undid it or silently retry it on recovery.
            self._terminal(
                item, ResultState.FAILED, now,
                "client readiness lost before confirmation",
            )
        if ready:
            with self._lock:
                return not self._closed
        assert started is not None
        if now >= started + self._ready_timeout:
            error = TimeoutError(
                f"game focus was not ready within {self._ready_timeout:g} seconds"
            )
            for record in records:
                self._fail_feature(record, error, now)
            with self._lock:
                self._ready_started_at = None
        return False

    def _collect_due(self, now: float) -> tuple[list[_FeatureRecord], tuple[Sensor, ...]]:
        with self._lock:
            due = [
                record for record in self._records.values()
                if record.active and not record.paused and now >= record.next_due
            ]
            confirmation_sensors = [
                pending.queued.request.confirmation.sensor
                for pending in self._confirming.values()
                if pending.queued.request.confirmation is not None
            ]
        sensors: set[Sensor] = set(confirmation_sensors)
        valid_due: list[_FeatureRecord] = []
        for record in due:
            activation_reasons = self._activation_reasons(
                record.feature, bypass_profile=self._is_probe(record.feature)
            )
            if activation_reasons:
                reason = "activation blocked: " + "; ".join(activation_reasons)
                self._fail_feature(record, RuntimeError(reason), now, detail=reason)
                continue
            try:
                with record.callback_lock:
                    with self._lock:
                        if not self._is_current_locked(record):
                            continue
                    supplied = record.feature.sensors()
                    if not isinstance(supplied, tuple):
                        raise TypeError("feature sensors() must return a tuple")
                    requested = supplied
                    if len(requested) > 1024:
                        raise ValueError("feature sensors() returned too many sensors")
                    if any(not isinstance(sensor, Sensor) for sensor in requested):
                        raise TypeError("feature sensors() must return Sensor values")
            except Exception as exc:
                self._fail_feature(record, exc, now)
                continue
            with self._lock:
                if not self._is_current_locked(record):
                    continue
                record.sensors = requested
            sensors.update(requested)
            valid_due.append(record)
        return valid_due, tuple(sorted(sensors, key=lambda sensor: (sensor.kind.value, sensor.name, sensor.region)))

    def _observe(self, sensors: tuple[Sensor, ...], now: float) -> bool:
        token = self._session_token()
        try:
            token.checkpoint()
            with runtime.use_token(token):
                batch = self._adapter.observe(sensors, token)
            token.checkpoint()
            if not isinstance(batch, ObservationBatch):
                raise TypeError("adapter.observe() must return ObservationBatch")
            adapter_timestamp = float(batch.observed_at)
            if not math.isfinite(adapter_timestamp):
                raise ValueError("observation timestamp must be finite")
        except runtime.CancelledError:
            return False
        except Exception as exc:
            with self._lock:
                self._capture_transport_ok = False
                self._capture_visual_valid = False
                self._capture_reasons = (f"{type(exc).__name__}: {exc}",)
                self._preview_frame = None
                self._failure_reasons.append(self._capture_reasons[0])
            self._fatal(exc, now)
            return False

        with self._lock:
            if self._closed:
                return False
            self._frame_id += 1
            frame_id = self._frame_id
            epoch = self._epoch
            previous_snapshot = self._snapshot
            watched = {
                sensor
                for record in self._records.values()
                if record.active
                for sensor in record.sensors
            }
            watched.update(
                pending.queued.request.confirmation.sensor
                for pending in self._confirming.values()
                if pending.queued.request.confirmation is not None
            )
        # ObservationBatch timestamps belong to the adapter's monotonic clock.
        # A Frame may separately carry wall time and is never consulted here.
        observed_at = adapter_timestamp
        try:
            observations: dict[Sensor, Observation] = {}
            for sensor in sensors:
                reading = batch.readings.get(sensor)
                if reading is None:
                    observations[sensor] = Observation(
                        sensor, None, observed_at, frame_id, epoch,
                        Quality.UNKNOWN, "adapter omitted requested sensor",
                    )
                else:
                    if not isinstance(reading, Reading):
                        raise TypeError(
                            f"adapter returned non-Reading value for {sensor.kind.value}"
                        )
                    observations[sensor] = Observation(
                        sensor, reading.value, observed_at, frame_id, epoch,
                        reading.quality, reading.detail,
                    )
            for sensor in watched.difference(observations):
                previous = previous_snapshot.observations.get(sensor)
                if previous is not None and previous.epoch == epoch:
                    observations[sensor] = previous
            published_at = self._now()
            visual_reasons = tuple(
                observation.detail or f"{observation.sensor.kind.value} observation failed"
                for observation in observations.values()
                if observation.quality is Quality.ERROR
            )
            with self._lock:
                if self._closed:
                    return False
                current_watched = {
                    sensor
                    for record in self._records.values()
                    if record.active
                    for sensor in record.sensors
                }
                current_watched.update(
                    pending.queued.request.confirmation.sensor
                    for pending in self._confirming.values()
                    if pending.queued.request.confirmation is not None
                )
                observations = {
                    sensor: observation
                    for sensor, observation in observations.items()
                    if sensor in current_watched
                }
                snapshot = Snapshot(observations, frame_id, epoch, published_at)
                self._snapshot = snapshot
                self._capture_transport_ok = True
                self._capture_at = observed_at
                self._capture_visual_valid = not visual_reasons
                self._capture_reasons = visual_reasons
                # ObservationBatch.frame is contractually an immutable Frame;
                # avoid importing desktop perception in the headless SDK path.
                self._preview_frame = batch.frame
        except Exception as exc:
            self._fatal(exc, now)
            return False
        self._record(
            "observation", published_at, snapshot=snapshot,
        )
        frame_recorder = getattr(self._recorder, "record_frame", None)
        if batch.frame is not None and callable(frame_recorder):
            try:
                with self._recorder_callback():
                    frame_recorder(frame_id, batch.frame)
            except Exception as exc:
                with self._lock:
                    if self._recorder_error is None:
                        self._recorder_error = exc
        return True

    def _run_feature(self, record: _FeatureRecord, snapshot: Snapshot, now: float) -> bool:
        activation_reasons = self._activation_reasons(
            record.feature, bypass_profile=self._is_probe(record.feature)
        )
        if activation_reasons:
            reason = "activation blocked: " + "; ".join(activation_reasons)
            self._fail_feature(record, RuntimeError(reason), now, detail=reason)
            return False
        try:
            with record.callback_lock:
                with self._lock:
                    if not self._is_current_locked(record):
                        return False
                step = record.feature.tick(FeatureContext(snapshot, now))
                if not isinstance(step, Step):
                    raise TypeError("feature.tick() must return Step")
        except Exception as exc:
            self._fail_feature(record, exc, now)
            return False
        with self._lock:
            if not self._is_current_locked(record):
                return False
            record.next_due = now + record.interval
            record.phase = step.phase.strip() if isinstance(step.phase, str) and step.phase.strip() else "Running"
            record.error = step.error
        self._record(
            "decision", now, record.name, generation=record.generation,
            step=step, snapshot=snapshot,
            status=FeatureStatus(
                record.name, record.generation, True, record.phase, record.error
            ),
        )
        if step.error is not None:
            self._fail_feature(record, RuntimeError(step.error), now, detail=step.error)
            return False
        for key in step.cancel_keys:
            if not isinstance(key, str) or not key:
                self._fail_feature(record, ValueError("cancel keys must be non-empty text"), now)
                return False
            self._cancel_key(record, key, now)
        for request in step.requests:
            if not isinstance(request, Request):
                self._fail_feature(record, TypeError("Step.requests must contain Request values"), now)
                return False
            self._enqueue(record, request, snapshot, now)
        return bool(step.finished)

    def _next_request_id(self, record: _FeatureRecord) -> str:
        with self._lock:
            self._request_sequence += 1
            return f"{record.name}:{record.generation}:{self._request_sequence}"

    def _enqueue(self, record: _FeatureRecord, request: Request, snapshot: Snapshot, now: float) -> None:
        request_id = self._next_request_id(record)
        self._record(
            "request", now, record.name, request=request,
            request_id=request_id, generation=record.generation,
        )
        bindings: list[_Binding] = []
        if request.action.kind is not ActionKind.PRESS and not request.requirements:
            item = _Queued(request_id, record.name, record.generation, request, now, now, 0, ())
            self._terminal(item, ResultState.REJECTED, now, "observation requirements required")
            return
        try:
            for requirement in request.requirements:
                if not isinstance(requirement, Requirement):
                    raise TypeError("request requirements must be Requirement values")
                observation = snapshot.observation(requirement.sensor)
                if observation.epoch != snapshot.epoch or not observation.fresh(now, requirement.max_age):
                    raise ValueError("required observation is unknown or stale")
                bindings.append(_Binding(requirement, observation, observation.value))
        except Exception as exc:
            item = _Queued(request_id, record.name, record.generation, request, now, now, 0, ())
            self._terminal(item, ResultState.REJECTED, now, str(exc))
            return

        with self._lock:
            if not self._is_current_locked(record):
                inactive = True
            else:
                inactive = False
            if inactive:
                duplicate = False
                full = False
                self._order += 1
                item = _Queued(
                    request_id, record.name, record.generation, request, now,
                    now + request.ttl, self._order, tuple(bindings),
                )
            else:
                duplicate = any(
                    item.owner == record.name and item.generation == record.generation
                    and item.request.key == request.key
                    for item in self._queue
                ) or any(
                    pending.queued.owner == record.name
                    and pending.queued.generation == record.generation
                    and pending.queued.request.key == request.key
                    for pending in self._confirming.values()
                )
                self._order += 1
                item = _Queued(
                    request_id, record.name, record.generation, request, now,
                    now + request.ttl, self._order, tuple(bindings),
                )
                full = len(self._queue) + len(self._confirming) >= self._queue_limit
                if not duplicate and not full:
                    self._queue.append(item)
        if inactive:
            self._terminal(item, ResultState.CANCELLED, now, "owner generation is inactive", deliver=False)
            return
        if duplicate:
            self._terminal(item, ResultState.REJECTED, now, "duplicate owner/generation/key")
            return
        if full:
            self._terminal(item, ResultState.REJECTED, now, "action queue is full")
            return
        self._record(
            "queue", now, record.name, request_id=request_id, generation=record.generation,
            key=request.key, priority=item.effective_priority, deadline=item.deadline,
        )

    def _cancel_key(self, record: _FeatureRecord, key: str, now: float) -> None:
        with self._lock:
            queued = [
                item for item in self._queue
                if self._owns(item, record) and item.request.key == key
            ]
            self._queue = [item for item in self._queue if item not in queued]
            pending = [
                value for value in self._confirming.values()
                if self._owns(value.queued, record) and value.queued.request.key == key
            ]
            for value in pending:
                self._confirming.pop(value.queued.request_id, None)
            self._prune_snapshot_locked(now)
        for item in queued + [value.queued for value in pending]:
            self._terminal(item, ResultState.CANCELLED, now, "cancelled by feature")

    def _dispatch_one(self, now: float) -> None:
        with self._lock:
            recorder_error = self._recorder_error
        if recorder_error is not None:
            self._fatal(
                RuntimeError(
                    f"event recorder failed: {type(recorder_error).__name__}: {recorder_error}"
                ),
                now,
            )
            return
        attempts = 0
        while attempts < self._queue_limit:
            try:
                now = self._now()
            except runtime.CancelledError:
                raise
            except Exception as exc:
                self._fatal(exc, now)
                return
            with self._lock:
                if not self._queue or self._closed:
                    return
                self._queue.sort(key=lambda item: (-item.effective_priority, item.deadline, item.order))
                item = self._queue.pop(0)
                record = self._records.get(item.owner)
                snapshot = self._snapshot
            attempts += 1
            reason = self._validate_dispatch(item, record, snapshot, now)
            if reason:
                self._terminal(item, ResultState.REJECTED, now, reason)
                continue
            assert record is not None
            activation_reasons = self._activation_reasons(
                record.feature, bypass_profile=self._is_probe(record.feature)
            )
            if activation_reasons:
                reason = "activation blocked: " + "; ".join(activation_reasons)
                self._terminal(item, ResultState.REJECTED, now, reason)
                self._fail_feature(record, RuntimeError(reason), now, detail=reason)
                return
            session_token = self._session_token()
            token = _CombinedToken(
                session_token, record.cancelled, record.name, record.generation
            )
            self._record(
                "dispatch", now, item.owner, request_id=item.request_id,
                generation=item.generation, key=item.request.key,
                action=item.request.action.kind.value,
            )
            with self._lock:
                self._last_action = ActionDiagnostic(
                    item.owner,
                    item.request.key,
                    item.request.action.kind.value,
                    now,
                )
            with self._lock:
                recorder_error = self._recorder_error
            if recorder_error is not None:
                failure = RuntimeError(
                    f"event recorder failed: {type(recorder_error).__name__}: {recorder_error}"
                )
                self._terminal(
                    item,
                    ResultState.FAILED,
                    now,
                    str(failure),
                )
                self._fatal(
                    failure,
                    now,
                )
                return
            try:
                ready = self._ensure_ready(now)
            except runtime.CancelledError:
                self._terminal(
                    item, ResultState.CANCELLED, self._failure_time(now),
                    "session cancelled before execute",
                )
                raise
            if not ready:
                with self._lock:
                    failed = self._closed
                self._terminal(
                    item,
                    ResultState.FAILED if failed else ResultState.REJECTED,
                    now,
                    "session failed during readiness check"
                    if failed else "client is not ready",
                )
                return
            try:
                execution_at = self._now()
            except runtime.CancelledError:
                self._terminal(
                    item, ResultState.CANCELLED, self._failure_time(now),
                    "session cancelled before execute",
                )
                raise
            except Exception as exc:
                self._fatal_popped(item, exc, now, "clock failed before execute")
                return
            with self._lock:
                record = self._records.get(item.owner)
                current_snapshot = self._snapshot
            reason = self._validate_dispatch(
                item, record, current_snapshot, execution_at
            )
            if reason:
                self._terminal(item, ResultState.REJECTED, execution_at, reason)
                return
            assert record is not None
            activation_reasons = self._activation_reasons(
                record.feature, bypass_profile=self._is_probe(record.feature)
            )
            if activation_reasons:
                reason = "activation blocked: " + "; ".join(activation_reasons)
                self._terminal(item, ResultState.REJECTED, execution_at, reason)
                self._fail_feature(
                    record, RuntimeError(reason), execution_at, detail=reason
                )
                return
            try:
                token.checkpoint()
                with runtime.use_token(token):
                    self._adapter.execute(item.request.action, token)
                token.checkpoint()
            except runtime.CancelledError:
                self._terminal(
                    item, ResultState.CANCELLED, self._failure_time(execution_at),
                    "generation cancelled",
                )
                if session_token.cancelled:
                    raise
                return
            except Exception as exc:
                failed_at = self._failure_time(execution_at)
                self._terminal(
                    item, ResultState.FAILED, failed_at,
                    f"adapter execute failed: {type(exc).__name__}: {exc}",
                )
                self._fail_feature(record, exc, failed_at)
                return

            if self._is_probe(record.feature):
                try:
                    transition = getattr(self._adapter, "last_cursor_transition", None)
                    if callable(transition):
                        transition = transition()
                    if transition is not None:
                        original, final = transition
                        callback = getattr(record.feature, "on_dispatch_metadata", None)
                        if callable(callback):
                            with record.callback_lock:
                                with self._lock:
                                    current = self._is_current_locked(record)
                                if current:
                                    callback(cursor_original=original, cursor_final=final)
                except Exception as exc:
                    # Cursor restoration telemetry is useful evidence, but it is
                    # observed only after the native action has already happened.
                    # Losing it must not rewrite that executed action as failed.
                    detail = (
                        "probe dispatch telemetry unavailable: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    with self._lock:
                        self._failure_reasons.append(detail)
                    self._record(
                        "telemetry", self._failure_time(execution_at), item.owner,
                        request_id=item.request_id, generation=item.generation,
                        key=item.request.key, available=False, detail=detail,
                    )

            try:
                completed_at = self._now()
            except runtime.CancelledError:
                raise
            except Exception as exc:
                self._fatal_popped(item, exc, execution_at, "clock failed after execute")
                return
            with self._lock:
                still_current = self._is_current_locked(record)
            if not still_current:
                self._terminal(item, ResultState.CANCELLED, completed_at, "generation cancelled")
                return
            confirmation = item.request.confirmation
            if confirmation is None:
                self._terminal(item, ResultState.EXECUTED, completed_at, "adapter call completed")
            else:
                self._result(item, ResultState.EXECUTED, completed_at, "awaiting confirmation", False)
                pending = _PendingConfirmation(
                    item, completed_at, snapshot.frame_id, completed_at + confirmation.timeout
                )
                with self._lock:
                    generation_cancelled = not self._is_current_locked(record)
                    if not generation_cancelled:
                        self._confirming[item.request_id] = pending
                if generation_cancelled:
                    self._terminal(
                        item, ResultState.CANCELLED, completed_at,
                        "generation cancelled", deliver=False,
                    )
            return

    def _fatal_popped(
        self,
        item: _Queued,
        exc: BaseException,
        fallback: float,
        stage: str,
    ) -> None:
        """Terminalize a request removed from the queue, then fail closed."""
        at = self._failure_time(fallback)
        self._terminal(
            item,
            ResultState.FAILED,
            at,
            f"{stage}: {type(exc).__name__}: {exc}",
        )
        self._fatal(exc, at)

    def _validate_dispatch(
        self,
        item: _Queued,
        record: _FeatureRecord | None,
        snapshot: Snapshot,
        now: float,
    ) -> str:
        if (
            record is None
            or not record.active
            or record.cancelled.is_set()
            or record.generation != item.generation
        ):
            return "owner generation is inactive"
        if now > item.deadline:
            return "request ttl expired"
        for binding in item.bindings:
            try:
                current = snapshot.observation(binding.requirement.sensor)
            except Exception:
                return "required observation is absent from current snapshot"
            if current is not binding.observation:
                return "required observation frame was superseded"
            if current.epoch != snapshot.epoch:
                return "required observation epoch changed"
            if current.value is not binding.value:
                return "required observation value identity changed"
            if not current.fresh(now, binding.requirement.max_age):
                return "required observation is unknown or stale"
        return ""

    def _reject_stale_queued(self, snapshot: Snapshot, now: float) -> None:
        """Reject old-frame intents before due policies regenerate decisions."""
        with self._lock:
            candidates = list(self._queue)
        rejected: list[tuple[_Queued, str]] = []
        for item in candidates:
            if not item.bindings:
                continue
            with self._lock:
                record = self._records.get(item.owner)
            reason = self._validate_dispatch(item, record, snapshot, now)
            if reason:
                rejected.append((item, reason))
        if not rejected:
            return
        rejected_ids = {id(item) for item, _reason in rejected}
        with self._lock:
            self._queue = [item for item in self._queue if id(item) not in rejected_ids]
        for item, reason in rejected:
            self._terminal(item, ResultState.REJECTED, now, reason)

    def _check_confirmations(self, snapshot: Snapshot, now: float) -> None:
        with self._lock:
            pending_values = list(self._confirming.values())
        for pending in pending_values:
            item = pending.queued
            with self._lock:
                record = self._records.get(item.owner)
                current_pending = self._confirming.get(item.request_id) is pending
                active = (
                    record is not None and record.active
                    and not record.cancelled.is_set()
                    and record.generation == item.generation
                )
            if not current_pending:
                continue
            if not active:
                with self._lock:
                    self._confirming.pop(item.request_id, None)
                self._terminal(item, ResultState.CANCELLED, now, "owner generation is inactive", deliver=False)
                continue
            confirmation = item.request.confirmation
            assert confirmation is not None
            matched = False
            try:
                observation = snapshot.observation(confirmation.sensor)
                if (
                    observation.quality is Quality.KNOWN
                    and observation.epoch == snapshot.epoch
                    and observation.frame_id > pending.executed_frame_id
                    and observation.observed_at >= pending.executed_at
                ):
                    actual = observation.value
                    if confirmation.field:
                        if isinstance(actual, Mapping):
                            actual = actual[confirmation.field]
                        else:
                            actual = getattr(actual, confirmation.field)
                    matched = bool(actual == confirmation.expected)
            except Exception:
                matched = False
            if matched:
                with self._lock:
                    self._confirming.pop(item.request_id, None)
                self._terminal(item, ResultState.CONFIRMED, now, "confirmation observed")
            elif now >= pending.deadline:
                with self._lock:
                    self._confirming.pop(item.request_id, None)
                self._terminal(item, ResultState.FAILED, now, "confirmation timed out")

    def _terminal(
        self,
        item: _Queued,
        state: ResultState,
        at: float,
        reason: str,
        *,
        deliver: bool = True,
    ) -> None:
        self._result(item, state, at, reason, True, deliver=deliver)

    def _result(
        self,
        item: _Queued,
        state: ResultState,
        at: float,
        reason: str,
        final: bool,
        *,
        deliver: bool = True,
    ) -> None:
        with self._lock:
            if item.finalized or (not final and item.nonfinal_emitted):
                return
            if final:
                item.finalized = True
            else:
                item.nonfinal_emitted = True
        result = ActionResult(
            item.request_id, item.owner, item.request.key, state, at, reason, final
        )
        if deliver:
            self._deliver(item, result)
        else:
            self._record_result(item, result, deliver=False)

    def _record_result(
        self, item: _Queued, result: ActionResult, *, deliver: bool
    ) -> None:
        with self._lock:
            self._last_result = ResultDiagnostic(
                result.owner,
                result.key,
                result.state.value,
                result.reason,
                result.at,
                result.final,
            )
        self._record(
            "result", result.at, item.owner, result=result,
            generation=item.generation, deliver=deliver,
        )

    def _deliver(self, item: _Queued, result: ActionResult) -> None:
        with self._lock:
            record = self._records.get(item.owner)
        if record is None:
            self._record_result(item, result, deliver=False)
            return
        with record.callback_lock:
            with self._lock:
                deliver = (
                    self._is_current_locked(record)
                    and record.generation == item.generation
                )
            self._record_result(item, result, deliver=deliver)
            if not deliver:
                return
            try:
                record.feature.on_result(result)
            except Exception as exc:
                self._fail_feature(record, exc, result.at)
            with self._lock:
                recorder_error = self._recorder_error
            if recorder_error is not None:
                self._fatal(
                    RuntimeError(
                        "event recorder failed: "
                        f"{type(recorder_error).__name__}: {recorder_error}"
                    ),
                    result.at,
                )

    def _is_current_locked(self, record: _FeatureRecord) -> bool:
        return (
            self._records.get(record.name) is record
            and record.active
            and not record.cancelled.is_set()
        )

    def _prune_snapshot_locked(self, now: float) -> None:
        watched = {
            sensor
            for record in self._records.values()
            if record.active
            for sensor in record.sensors
        }
        watched.update(
            pending.queued.request.confirmation.sensor
            for pending in self._confirming.values()
            if pending.queued.request.confirmation is not None
        )
        kept = {
            sensor: observation
            for sensor, observation in self._snapshot.observations.items()
            if sensor in watched
        }
        if len(kept) != len(self._snapshot.observations):
            self._snapshot = Snapshot(
                kept,
                self._snapshot.frame_id,
                self._snapshot.epoch,
                now,
            )

    def _fail_feature(
        self,
        record: _FeatureRecord,
        exc: BaseException,
        now: float,
        *,
        detail: str | None = None,
    ) -> None:
        message = detail or f"{type(exc).__name__}: {exc}"
        with self._lock:
            if not self._is_current_locked(record):
                return
            record.active = False
            record.phase = "Failed"
            record.error = message
            record.cancelled.set()
            removed = [item for item in self._queue if self._owns(item, record)]
            self._queue = [item for item in self._queue if not self._owns(item, record)]
            pending = [
                value for value in self._confirming.values() if self._owns(value.queued, record)
            ]
            for value in pending:
                self._confirming.pop(value.queued.request_id, None)
            self._prune_snapshot_locked(now)
            self._failure_reasons.append(message)
        for item in removed + [value.queued for value in pending]:
            self._terminal(item, ResultState.CANCELLED, now, "feature failed", deliver=False)
        self._record(
            "status", now, record.name,
            status=FeatureStatus(record.name, record.generation, False, "Failed", message),
            reason="feature failed",
        )

    def _fatal(self, exc: BaseException, now: float) -> None:
        with self._lifecycle_lock:
            self._fatal_locked(exc, now)

    def _fatal_locked(self, exc: BaseException, now: float) -> None:
        message = f"{type(exc).__name__}: {exc}"
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._closed_reason = f"failure requires client reconfiguration ({message})"
            self._readiness_reason = self._closed_reason
            self._failure_reasons.append(message)
            self._epoch += 1
            self._snapshot = Snapshot({}, self._frame_id, self._epoch, now)
            self._preview_frame = None
            self._manual_token.cancel()
            records = [record for record in self._records.values() if record.active]
            for record in records:
                record.active = False
                record.phase = "Failed"
                record.error = message
                record.cancelled.set()
            removed = list(self._queue)
            removed.extend(value.queued for value in self._confirming.values())
            self._queue.clear()
            self._confirming.clear()
        for item in removed:
            self._terminal(item, ResultState.CANCELLED, now, "session failed", deliver=False)
        for record in records:
            self._record(
                "status", now, record.name,
                status=FeatureStatus(record.name, record.generation, False, "Failed", message),
                reason="session failed",
            )
        runtime.cancel(self._runtime_name, timeout=0)
        self._close_adapter(suppress_errors=True)

    def _close_adapter(self, *, suppress_errors: bool) -> None:
        with self._lock:
            if self._adapter_closed:
                return
        try:
            self._adapter.close()
        except Exception as exc:
            with self._lock:
                self._close_error = exc
            if not suppress_errors:
                raise
        else:
            with self._lock:
                self._adapter_closed = True
                self._close_error = None

    def close(self) -> None:
        """Cancel all generations, stop the loop, and close the adapter once."""
        self._guard_recorder_reentry("close the session")
        with self._lifecycle_lock:
            self._close()

    def _close(self) -> None:
        try:
            now = self._now()
        except runtime.CancelledError:
            raise
        except Exception:
            now = self._failure_time()
        with self._lock:
            already_closed = self._closed
            if already_closed:
                records = []
            else:
                self._closed = True
                if self._closed_reason is None:
                    self._closed_reason = "closed by caller"
                self._epoch += 1
                self._snapshot = Snapshot({}, self._frame_id, self._epoch, now)
                self._preview_frame = None
                self._manual_token.cancel()
                records = [record for record in self._records.values() if record.active]
        for record in records:
            self._invalidate_record(record, "stopped", now)
        runtime.cancel(self._runtime_name, timeout=1.0, forget=True)
        self._close_adapter(suppress_errors=False)
        with self._lock:
            record_close = (
                self._closed_reason == "closed by caller"
                and not self._close_status_recorded
            )
            if record_close:
                self._close_status_recorded = True
        if record_close:
            self._record(
                "status",
                now,
                status=FeatureStatus("ClientSession", 0, False, "Closed"),
                reason="closed",
            )

    def __enter__(self) -> "ClientSession":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = ["ClientSession"]
