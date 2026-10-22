"""Non-blocking CaveBot policy built only on the public client SDK.

The feature owns no capture, input, threads, or sleeps.  Each call to ``tick``
examines one immutable snapshot and returns at most one input request.  This
keeps combat, route timeouts, and cancellation deterministic under the shared
runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from numbers import Real
from typing import Any, Callable, Iterable

from engine.CaveBot.routes import Route, save_checkpoint
from tibia_client.freshness import get_stale_seconds
from tibia_client import (
    Action,
    ActionKind,
    ActionResult,
    FeatureContext,
    Marker,
    ObservationUnavailable,
    Priority,
    Request,
    Requirement,
    ResultState,
    Sensor,
    SensorKind,
    Step,
    Target,
)


@dataclass(frozen=True, order=True)
class TargetSpec:
    """A battle-list species and its selection priority (smaller wins)."""

    priority: int
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("target name must not be empty")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise ValueError("target priority must be an integer")


@dataclass(frozen=True)
class SessionConfig:
    """Immutable settings; operation timeouts renew retries, never disable the bot."""

    stand_seconds: float = 0.0
    poll_interval: float = 0.2
    marker_timeout: float = 8.0
    arrival_timeout: float = 20.0
    combat_timeout: float = 90.0
    movement_attempts: int = 3  # Attempts per retry batch; batches repeat until stopped.
    count_stable_samples: int = 2
    follow: bool = True
    walking: bool = True
    loot: bool = True
    loot_button: str = "right"
    loot_budget: int = 9
    loot_max_seconds: float = 2.0
    navigation_history_size: int = 12
    navigation_progress_pixels: float = 3.0
    navigation_stall_seconds: float = 1.0

    def __post_init__(self) -> None:
        durations = (
            "stand_seconds",
            "poll_interval",
            "marker_timeout",
            "arrival_timeout",
            "combat_timeout",
            "loot_max_seconds",
            "navigation_progress_pixels",
            "navigation_stall_seconds",
        )
        for field_name in durations:
            value = getattr(self, field_name)
            if not isinstance(value, Real) or isinstance(value, bool) or not math.isfinite(value):
                raise ValueError(f"{field_name} must be a finite number")
        if self.stand_seconds < 0:
            raise ValueError("stand_seconds must not be negative")
        if self.poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        if self.marker_timeout <= 0 or self.arrival_timeout <= 0 or self.combat_timeout <= 0:
            raise ValueError("operation timeouts must be greater than zero")
        if self.loot_max_seconds <= 0:
            raise ValueError("loot_max_seconds must be greater than zero")
        if self.navigation_progress_pixels <= 0 or self.navigation_stall_seconds <= 0:
            raise ValueError("navigation progress settings must be greater than zero")
        for field_name in (
            "movement_attempts",
            "count_stable_samples",
            "loot_budget",
            "navigation_history_size",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{field_name} must be at least one")
        if self.loot_button not in ("left", "right"):
            raise ValueError("loot_button must be 'left' or 'right'")
        if any(type(value) is not bool for value in (self.follow, self.loot, self.walking)):
            raise ValueError("follow and loot settings must be true or false")


@dataclass(frozen=True)
class CaveBotStatus:
    phase: str = "Starting"
    reason: str = "not_started"
    waypoint_index: int = 0
    waypoint_id: str | None = None
    target: str | None = None
    completed_waypoints: int = 0
    finished: bool = False
    failed: bool = False
    error: str | None = None
    navigation_state: str = "idle"
    marker_position: tuple[int, int] | None = None
    navigation_attempt: int = 0


@dataclass(frozen=True)
class _Pending:
    key: str
    kind: str
    issued_at: float
    evidence_frame: int


class CaveBotFeature:
    """Execute a walk route through the shared SDK without blocking a worker."""

    name = "CaveBot"
    interval = 0.2

    @property
    def _MAX_EVIDENCE_AGE(self):
        return get_stale_seconds()

    def __init__(
        self,
        route: Route,
        targets: Iterable[TargetSpec] = (),
        *,
        config: SessionConfig | None = None,
        start_index: int = 0,
        loop: bool = True,
        save_progress: Callable[[Route, int], Any] | None = save_checkpoint,
        map_region: Iterable[int] = (),
        battle_region: Iterable[int] = (),
        loot_points: Iterable[tuple[int, int]] = (),
    ) -> None:
        if not isinstance(route, Route):
            raise TypeError("route must be a Route")
        if not isinstance(start_index, int) or isinstance(start_index, bool):
            raise ValueError("start index must be an integer")
        if type(loop) is not bool:
            raise ValueError("loop must be true or false")
        if route.waypoints and not 0 <= start_index < len(route.waypoints):
            raise ValueError("start index is outside the route")
        checked_targets = tuple(sorted(targets))
        if any(not isinstance(target, TargetSpec) for target in checked_targets):
            raise TypeError("targets must contain TargetSpec values")
        folded_names = [target.name.casefold() for target in checked_targets]
        if len(folded_names) != len(set(folded_names)):
            raise ValueError("target names must be unique")

        self.route = route
        self.targets = checked_targets
        self.config = config or SessionConfig()
        self.interval = self.config.poll_interval
        self.index = start_index
        self.loop = loop
        self.save_progress = save_progress
        self.map_region = self._region(map_region, "map")
        self.battle_region = self._region(battle_region, "battle")
        if route.waypoints and len(self.map_region) != 4:
            raise ValueError("map region must be configured for a non-empty route")
        if checked_targets and len(self.battle_region) != 4:
            raise ValueError("battle region must be configured when targets are enabled")
        self.loot_points = tuple(self._point(point, "loot") for point in loot_points)

        waypoint_id = route.waypoints[start_index].id if route.waypoints else None
        self._status = CaveBotStatus(waypoint_index=start_index, waypoint_id=waypoint_id)
        self._mode = "find_marker"
        self._mode_started: float | None = None
        self._deadline: float | None = None
        self._pending: _Pending | None = None
        self._request_sequence = 0
        self._arrival_failures = 0
        self._navigation_history: tuple[
            tuple[float, int, tuple[int, int] | None], ...
        ] = ()
        self._last_progress_position: tuple[int, int] | None = None
        self._last_progress_at: float | None = None
        self._exit_requested = False
        self._abort_requested = False
        self._selected_wait: tuple[str, int, float] | None = None
        self._follow_wait: tuple[int, float] | None = None
        self._active_target: str | None = None
        self._counts: dict[str, int] = {}
        self._count_candidates: dict[str, tuple[int, int]] = {}
        self._count_frame: int | None = None
        self._empty_samples = 0
        self._combat_deadline: float | None = None
        self._loot_sensor: Sensor | None = None
        self._loot_cursor = 0
        self._loot_started: float | None = None
        self._loot_resweep = False
        self._pre_pause_status: CaveBotStatus | None = None
        self._evidence_wait_started: float | None = None
        self._pre_evidence_status: CaveBotStatus | None = None

    @staticmethod
    def _region(region: Iterable[int], name: str) -> tuple[int, ...]:
        result = tuple(region)
        if any(type(value) is not int for value in result):
            raise ValueError(f"{name} region coordinates must be integers")
        if result and (
            len(result) != 4
            or any(value < 0 for value in result)
            or result[2] <= result[0]
            or result[3] <= result[1]
        ):
            raise ValueError(f"{name} region must be valid left/top/right/bottom bounds")
        return result

    @staticmethod
    def _point(point: tuple[int, int], name: str) -> tuple[int, int]:
        result = tuple(point)
        if len(result) != 2 or any(type(value) is not int or value < 0 for value in result):
            raise ValueError(f"{name} points must be non-negative integer pairs")
        return result  # type: ignore[return-value]

    @property
    def status(self) -> CaveBotStatus:
        return self._status

    @property
    def failed(self) -> bool:
        return self._status.failed

    @property
    def error(self) -> str | None:
        return self._status.error

    @property
    def finished(self) -> bool:
        return self._status.finished

    @property
    def navigation_progress(self) -> dict[str, Any]:
        """Return a trace-safe snapshot of recent screen-space route progress."""

        return {
            "state": self._status.navigation_state,
            "marker_position": self._status.marker_position,
            "attempts_used": self._arrival_failures,
            "attempts_remaining": max(
                0, self.config.movement_attempts - self._arrival_failures
            ),
            "retry_until_stopped": True,
            "history": self._navigation_history,
        }

    def request_exit(self) -> None:
        """Finish safely on the next tick after any active combat or loot."""

        if not self._status.finished:
            self._exit_requested = True

    def request_abort(self) -> None:
        """Stop immediately, cancelling this feature's outstanding request."""

        if not self._status.finished:
            self._abort_requested = True

    def anchor_sensors(self) -> tuple[Sensor, ...]:
        """Sensors an orchestrator may request while recovering route position."""

        result = []
        for waypoint in self.route.waypoints:
            sensor = Sensor(SensorKind.MARKER, self.map_region, waypoint.mark)
            if sensor not in result:
                result.append(sensor)
        return tuple(result)

    def recognized_anchor_candidates(self, context: FeatureContext) -> tuple[int, ...]:
        """Return route indices backed by fresh, explicitly reached markers."""

        candidates = []
        for index, waypoint in enumerate(self.route.waypoints):
            sensor = Sensor(SensorKind.MARKER, self.map_region, waypoint.mark)
            try:
                marker = context.require(sensor, self._MAX_EVIDENCE_AGE)
            except ObservationUnavailable:
                continue
            if (
                isinstance(marker, Marker)
                and marker.reached
                and marker.position is not None
            ):
                candidates.append(index)
        return tuple(candidates)

    def resume_from_recognized_anchor(
        self,
        context: FeatureContext,
        *,
        waypoint_index: int | None = None,
    ) -> int:
        """Resume at a fresh reached marker, requiring selection if ambiguous."""

        if self._pending is not None:
            raise RuntimeError("cannot resume while an input request is pending")
        candidates = self.recognized_anchor_candidates(context)
        if not candidates:
            raise ValueError("no reached route anchor was recognized")
        if waypoint_index is None:
            if len(candidates) != 1:
                raise ValueError(
                    "recognized route anchor is ambiguous; select waypoint_index"
                )
            waypoint_index = candidates[0]
        if (
            not isinstance(waypoint_index, int)
            or isinstance(waypoint_index, bool)
            or waypoint_index not in candidates
        ):
            raise ValueError("waypoint_index must select a recognized route anchor")
        self.index = waypoint_index
        self._mode = "find_marker"
        self._mode_started = context.now
        self._deadline = context.now + self.config.marker_timeout
        self._arrival_failures = 0
        self._navigation_history = ()
        self._last_progress_position = None
        self._last_progress_at = None
        self._exit_requested = False
        self._abort_requested = False
        self._active_target = None
        self._combat_deadline = None
        self._selected_wait = None
        self._follow_wait = None
        self._set_status(
            "Finding marker",
            "recognized_anchor_resume",
            waypoint_index=self.index,
            waypoint_id=self.route.waypoints[self.index].id,
            target=None,
            finished=False,
            failed=False,
            error=None,
            navigation_state="idle",
            marker_position=None,
            navigation_attempt=0,
        )
        return self.index

    def _marker_sensor(self) -> Sensor | None:
        if not self.config.walking or not self.route.waypoints:
            return None
        return Sensor(SensorKind.MARKER, self.map_region, self.route.waypoints[self.index].mark)

    def _target_sensor(self, target: TargetSpec | str) -> Sensor:
        name = target.name if isinstance(target, TargetSpec) else target
        return Sensor(SensorKind.TARGET, self.battle_region, name)

    def sensors(self) -> tuple[Sensor, ...]:
        sensors: list[Sensor] = []
        marker = self._marker_sensor()
        if marker is not None:
            sensors.append(marker)
        sensors.extend(self._target_sensor(target) for target in self.targets)
        if self.config.follow:
            sensors.append(Sensor(SensorKind.FOLLOW))
        if self.targets and self.config.loot and self.loot_points:
            sensors.append(Sensor(SensorKind.FOLLOW, name="disable"))
        return tuple(sensors)

    def required_operations(self) -> tuple[str, ...]:
        if self.config.walking and not self.route.waypoints:
            return ()
        operations: list[str] = []
        if ((self.config.walking and self.route.waypoints)
                or (self.targets and (self.config.follow or (self.config.loot and self.loot_points)))):
            operations.append("left_click")
        if self.targets:
            operations.append("battle_list_click")
        # Looting is only reachable after combat.  A walking-only route has no
        # transition into the loot phase, even when the default loot setting
        # and calibrated loot points are present.
        if self.targets and self.config.loot and self.loot_points:
            operation = "right_click" if self.config.loot_button == "right" else "left_click"
            if operation not in operations:
                operations.append(operation)
        return tuple(operations)

    def on_pause(self) -> None:
        """Discard any old request intent while retaining route progress."""

        if self._pre_pause_status is None:
            self._pre_pause_status = self._status
        self._pending = None
        self._set_status("Paused", "navigation_paused")

    def on_resume(self, *, paused_for: float = 0.0) -> None:
        """Restore the phase and preserve the remaining time on every timer."""

        previous = self._pre_pause_status
        if (
            not isinstance(paused_for, Real)
            or isinstance(paused_for, bool)
            or not math.isfinite(paused_for)
            or paused_for < 0
        ):
            raise ValueError("paused_for must be a finite non-negative number")
        self._pre_pause_status = None
        elapsed = float(paused_for)
        self._evidence_wait_started = self._shift_time(self._evidence_wait_started, elapsed)
        self._mode_started = self._shift_time(self._mode_started, elapsed)
        self._deadline = self._shift_time(self._deadline, elapsed)
        self._combat_deadline = self._shift_time(self._combat_deadline, elapsed)
        self._loot_started = self._shift_time(self._loot_started, elapsed)
        self._last_progress_at = self._shift_time(self._last_progress_at, elapsed)
        if self._selected_wait is not None:
            target, frame, ready_at = self._selected_wait
            self._selected_wait = (target, frame, ready_at + elapsed)
        if self._follow_wait is not None:
            frame, ready_at = self._follow_wait
            self._follow_wait = (frame, ready_at + elapsed)
        if previous is not None:
            self._status = previous

    @staticmethod
    def _shift_time(value: float | None, elapsed: float) -> float | None:
        return None if value is None else value + elapsed

    def _set_status(self, phase: str, reason: str, **changes: Any) -> None:
        self._status = replace(self._status, phase=phase, reason=reason, **changes)

    def _step(
        self,
        request: Request | None = None,
        *,
        cancel_keys: tuple[str, ...] = (),
    ) -> Step:
        requests = () if request is None else (request,)
        return Step(
            requests=requests,
            cancel_keys=cancel_keys,
            phase=self._status.phase,
            finished=self._status.finished,
            error=self._status.error,
        )

    def _fail(self, reason: str, detail: str | None = None) -> Step:
        error = detail or reason.replace("_", " ")
        self._pending = None
        self._set_status(
            f"Failed: {error}", reason, finished=True, failed=True, error=error
        )
        return self._step()

    def _wait_for_evidence(self, context, detail):
        if self._evidence_wait_started is None:
            self._evidence_wait_started = context.now
            self._pre_evidence_status = self._status
        cancel = (self._pending.key,) if self._pending else ()
        self._pending = None
        # Keep the last recognized count through OCR/capture gaps so a later
        # drop or disappearance can trigger loot. Unavailable is not zero.
        self._count_candidates.clear()
        self._empty_samples = 0
        self._set_status(f'Waiting for creature reading: {detail}', 'target_evidence_unavailable',
                         finished=False, failed=False, error=None)
        return self._step(cancel_keys=cancel)

    def _resume_evidence_clock(self, context):
        if self._evidence_wait_started is None:
            return
        elapsed = max(0, context.now-self._evidence_wait_started)
        for field in ('_mode_started', '_deadline', '_combat_deadline', '_loot_started', '_last_progress_at'):
            setattr(self, field, self._shift_time(getattr(self, field), elapsed))
        if self._pre_evidence_status is not None:
            self._status = self._pre_evidence_status
        self._evidence_wait_started, self._pre_evidence_status = None, None

    def _next_key(self, kind: str) -> str:
        self._request_sequence += 1
        return f"{kind}:{self._request_sequence}"

    def _request(
        self,
        context: FeatureContext,
        kind: str,
        action: Action,
        priority: int,
        sensor: Sensor,
        reason: str,
        additional_sensors: Iterable[Sensor] = (),
    ) -> Request:
        key = self._next_key(kind)
        self._pending = _Pending(key, kind, context.now, context.snapshot.frame_id)
        requirements = (sensor, *additional_sensors)
        return Request(
            key=key,
            action=action,
            priority=priority,
            ttl=0.5,
            requirements=tuple(
                Requirement(required, self._MAX_EVIDENCE_AGE)
                for required in requirements
            ),
            reason=reason,
        )

    def _known(self, context: FeatureContext, sensor: Sensor) -> Any | None:
        try:
            return context.require(sensor, self._MAX_EVIDENCE_AGE)
        except ObservationUnavailable:
            return None

    def _record_navigation_marker(
        self, context: FeatureContext, marker: Marker | None
    ) -> None:
        """Record one bounded sample per frame and expose a progress diagnosis."""

        position = marker.position if isinstance(marker, Marker) else None
        sample = (context.now, context.snapshot.frame_id, position)
        if (
            self._navigation_history
            and self._navigation_history[-1][1] == context.snapshot.frame_id
        ):
            return
        self._navigation_history = (
            *self._navigation_history,
            sample,
        )[-self.config.navigation_history_size :]

        if position is None:
            self._set_status(
                self._status.phase,
                "navigation_marker_missing",
                navigation_state="missing",
                marker_position=None,
            )
            return

        moving = self._last_progress_position is None
        if self._last_progress_position is not None:
            dx = position[0] - self._last_progress_position[0]
            dy = position[1] - self._last_progress_position[1]
            moving = math.hypot(dx, dy) >= self.config.navigation_progress_pixels
        if moving:
            self._last_progress_position = position
            self._last_progress_at = context.now
        progress_at = self._last_progress_at
        stalled = (
            not moving
            and progress_at is not None
            and context.now - progress_at >= self.config.navigation_stall_seconds
        )
        self._set_status(
            self._status.phase,
            "navigation_stalled" if stalled else "navigation_moving",
            navigation_state="stalled" if stalled else "moving",
            marker_position=position,
        )

    def _finish_exit(self, *, immediate: bool = False) -> Step:
        cancel = ()
        if self._pending is not None and (immediate or self._pending.kind == "navigate"):
            cancel = (self._pending.key,)
            self._pending = None
        self._arrival_failures = 0
        self._last_progress_position = None
        self._last_progress_at = None
        self._exit_requested = False
        self._abort_requested = False
        self._set_status(
            "Finished",
            "abort_requested" if immediate else "exit_requested",
            target=None,
            finished=True,
            failed=False,
            error=None,
            navigation_state="idle",
            marker_position=None,
            navigation_attempt=0,
        )
        return self._step(cancel_keys=cancel)

    def _choose_target(
        self, context: FeatureContext
    ) -> tuple[TargetSpec | None, Target | None, str | None]:
        unavailable = []
        for spec in self.targets:
            try:
                value = context.require(
                    self._target_sensor(spec), self._MAX_EVIDENCE_AGE
                )
            except ObservationUnavailable as exc:
                unavailable.append(f'{spec.name}: {exc}')
                continue
            if not isinstance(value, Target):
                unavailable.append(
                    f"target evidence for {spec.name!r} has invalid type "
                    f"{type(value).__name__}; expected Target"
                )
                continue
            if value.count > 0:
                return spec, value, None
        return None, None, '; '.join(unavailable) or None

    def _expire_pending(self, context: FeatureContext) -> None:
        pending = self._pending
        if pending is None:
            return
        # Requests expire in the runtime after 0.5s.  A wider local bound lets
        # the rejection callback arrive, while preventing a lost callback from
        # stalling this feature forever.
        if context.now - pending.issued_at >= max(1.0, self.interval * 4):
            self._pending = None

    def _enter_combat(self, context: FeatureContext, target: TargetSpec) -> tuple[str, ...]:
        cancel: tuple[str, ...] = ()
        if self._pending is not None and self._pending.kind == "navigate":
            cancel = (self._pending.key,)
            self._pending = None
        self._mode = "combat"
        self._mode_started = context.now
        self._combat_deadline = context.now + self.config.combat_timeout
        self._active_target = target.name
        self._counts.clear()
        self._count_candidates.clear()
        self._count_frame = None
        self._empty_samples = 0
        self._selected_wait = None
        self._follow_wait = None
        self._set_status(
            f"Fighting {target.name}",
            "target_interrupted_walk",
            target=target.name,
            # Stationary combat deliberately has no route waypoints.
            waypoint_id=self.route.waypoints[self.index].id if self.route.waypoints else None,
        )
        return cancel

    def _observe_combat_counts(self, context: FeatureContext) -> Sensor | None:
        """Track deaths across species, once per frame, including while looting."""
        if self._count_frame == context.snapshot.frame_id:
            return None
        self._count_frame = context.snapshot.frame_id
        dropped = None
        for spec in self.targets:
            sensor = self._target_sensor(spec)
            value = self._known(context, sensor)
            if not isinstance(value, Target):
                self._count_candidates.pop(spec.name, None)
                continue
            # A partial display such as 3+ supplies the recognized integer 3.
            count = value.count
            previous = self._counts.setdefault(spec.name, count)
            if count == previous:
                self._count_candidates.pop(spec.name, None)
                continue
            candidate, samples = self._count_candidates.get(spec.name, (None, 0))
            samples = samples + 1 if count == candidate else 1
            if samples < self.config.count_stable_samples:
                self._count_candidates[spec.name] = (count, samples)
                continue
            self._counts[spec.name] = count
            self._count_candidates.pop(spec.name, None)
            if count < previous and dropped is None:
                dropped = sensor
        return dropped

    def _begin_loot(self, context: FeatureContext, sensor: Sensor) -> None:
        self._mode = "loot"
        self._loot_sensor = sensor
        self._loot_cursor = 0
        self._loot_started = context.now
        self._loot_resweep = False
        self._selected_wait = None
        self._follow_wait = None
        self._set_status("Holding position for loot", "stable_target_count_drop")

    def _finish_combat(self, context: FeatureContext) -> None:
        self._mode = "find_marker"
        self._mode_started = context.now
        self._deadline = context.now + self.config.marker_timeout
        self._active_target = None
        self._combat_deadline = None
        self._selected_wait = None
        self._follow_wait = None
        self._set_status(
            "Finding marker" if self.config.walking else "Waiting for targets", "combat_clear", target=None,
            waypoint_id=self.route.waypoints[self.index].id if self.route.waypoints else None,
        )

    def _tick_combat(self, context: FeatureContext) -> Step:
        if self._combat_deadline is not None and context.now >= self._combat_deadline:
            self._combat_deadline = context.now + self.config.combat_timeout
            self._selected_wait = self._follow_wait = None
            self._set_status("Combat timeout; retrying", "combat_timeout_retry")

        spec, target, evidence_error = self._choose_target(context)
        if evidence_error is not None:
            return self._wait_for_evidence(context, evidence_error)
        dropped = self._observe_combat_counts(context)
        if dropped is not None and self.config.loot and self.loot_points:
            self._begin_loot(context, dropped)
            return self._tick_loot(context)
        hold_for_loot = bool(self.config.loot and self.loot_points and any(
            count < self._counts[name] for name, (count, _) in self._count_candidates.items()
        ))
        if hold_for_loot:
            # Stop chasing on the first observed decrease, before selecting the
            # next target. The stable-count check still decides whether to loot.
            hold = self._set_follow(context, enabled=False)
            if hold is not None:
                return hold
        if spec is None or target is None:
            self._empty_samples += 1
            if self._empty_samples < self.config.count_stable_samples:
                return self._step()
            self._finish_combat(context)
            return self._step()

        self._empty_samples = 0
        self._set_status(f"Fighting {spec.name}"+(' · partial count' if not target.count_complete else ''),
                         "combat_active", target=spec.name)
        selection = self._maintain_attack(context, spec, target)
        if selection is not None:
            return selection
        if self.config.follow and not hold_for_loot:
            follow = self._set_follow(context, enabled=True)
            if follow is not None:
                return follow
        return self._step()

    def _maintain_attack(self, context: FeatureContext, spec: TargetSpec, target: Target) -> Step | None:
        """Select a live target before spending another action on loot/follow."""
        if self._active_target != spec.name:
            self._active_target = spec.name
            self._selected_wait = None
        if target.selected:
            self._selected_wait = None
            return None
        self._set_status(f"Selecting {spec.name}", "selecting_target", target=spec.name)
        if target.position is None:
            self._set_status(f"Waiting for {spec.name} position", "target_position_unavailable")
            return self._step()
        wait = self._selected_wait
        if wait is not None and wait[0] == spec.name and (
            context.snapshot.frame_id <= wait[1] or context.now < wait[2]
        ):
            return self._step()
        request = self._request(
            context, "select", Action(ActionKind.LEFT_CLICK, point=target.position),
            Priority.COMBAT, self._target_sensor(spec), f"select target {spec.name}",
        )
        return self._step(request)

    def _set_follow(self, context: FeatureContext, *, enabled: bool) -> Step | None:
        """None means the requested mode is observed; unknown mode waits."""
        sensor = Sensor(SensorKind.FOLLOW, name="" if enabled else "disable")
        try:
            value = context.require(sensor, self._MAX_EVIDENCE_AGE)
        except ObservationUnavailable as exc:
            self._set_status(f"Waiting for follow reading: {exc}", "follow_evidence_unavailable")
            return self._step()
        if value is None:
            self._follow_wait = None
            return None
        try:
            point = self._point(value, "follow")
        except (TypeError, ValueError):
            return self._fail("invalid_follow_observation")
        self._set_status("Enabling follow" if enabled else "Disabling follow for loot",
                         "enabling_follow" if enabled else "disabling_follow")
        wait = self._follow_wait
        if wait is not None and (context.snapshot.frame_id <= wait[0] or context.now < wait[1]):
            return self._step()
        return self._step(self._request(
            context, "follow" if enabled else "follow_off",
            Action(ActionKind.LEFT_CLICK, point=point), Priority.COMBAT, sensor,
            "enable target follow" if enabled else "hold position while attacking and looting",
        ))

    def _tick_loot(self, context: FeatureContext) -> Step:
        if self._combat_deadline is not None and context.now >= self._combat_deadline:
            self._combat_deadline = context.now + self.config.combat_timeout
        spec, target, evidence_error = self._choose_target(context)
        if evidence_error is not None:
            return self._wait_for_evidence(context, evidence_error)
        if self._observe_combat_counts(context) is not None:
            # A new corpse may be on a square already visited. Finish this pass,
            # then sweep again without moving or delaying selection of a target.
            self._loot_resweep = True
            self._selected_wait = None
        elapsed = context.now - (self._loot_started if self._loot_started is not None else context.now)
        limit = min(self.config.loot_budget, len(self.loot_points))
        if self._loot_cursor >= limit:
            if self._loot_resweep and limit:
                self._loot_cursor = 0
                self._loot_resweep = False
                self._loot_started = context.now
            else:
                self._mode = "combat"
                self._follow_wait = None
                self._set_status("Fighting", "loot_complete")
                # Re-evaluate current targets now, then restore follow only
                # after an attack is selected (or finish combat if none remain).
                return self._tick_combat(context)
        if elapsed >= self.config.loot_max_seconds:
            # Resume at the next unsent square; slow capture must not truncate
            # the sweep or start it over from the first square.
            self._loot_started = context.now
            self._set_status("Loot timeout; retrying", "loot_timeout_retry")
        hold = self._set_follow(context, enabled=False)
        if hold is not None:
            return hold
        if spec is not None and target is not None:
            selection = self._maintain_attack(context, spec, target)
            if selection is not None:
                return selection
        else:
            self._selected_wait = None
        if self._loot_sensor is None:
            return self._fail("loot_evidence_missing")
        # Requiring a fresh battle-list observation prevents an old count drop
        # from authorizing a burst of later clicks.  The adapter restores the
        # cursor around each individual click; no multi-click input lease exists.
        try:
            loot_evidence = context.require(
                self._loot_sensor, self._MAX_EVIDENCE_AGE
            )
        except ObservationUnavailable as exc:
            return self._wait_for_evidence(context, f'loot target evidence is unavailable: {exc}')
        if not isinstance(loot_evidence, Target):
            return self._fail(
                "target_evidence_unavailable",
                "loot target evidence has invalid type "
                f"{type(loot_evidence).__name__}; expected Target",
            )
        action_kind = (
            ActionKind.RIGHT_CLICK if self.config.loot_button == "right" else ActionKind.LEFT_CLICK
        )
        self._set_status(
            f"Attacking {spec.name} · looting {self._loot_cursor + 1}/{limit}" if spec else
            f"Looting {self._loot_cursor + 1}/{limit}",
            "combat_and_loot" if spec else "looting", target=spec.name if spec else None,
        )
        request = self._request(
            context,
            "loot",
            Action(action_kind, point=self.loot_points[self._loot_cursor]),
            Priority.LOOT,
            self._loot_sensor,
            f"loot square {self._loot_cursor + 1}",
            (Sensor(SensorKind.FOLLOW, name="disable"),)
            + ((self._target_sensor(spec),) if spec else ()),
        )
        return self._step(request)

    def _start_marker_search(self, context: FeatureContext, reason: str) -> None:
        self._mode = "find_marker"
        self._mode_started = context.now
        self._deadline = context.now + self.config.marker_timeout
        self._set_status(
            "Finding marker", reason, target=None,
            waypoint_index=self.index,
            waypoint_id=self.route.waypoints[self.index].id,
        )

    def _tick_route(self, context: FeatureContext) -> Step:
        marker_sensor = self._marker_sensor()
        if marker_sensor is None:
            self._set_status("Stopped: empty route", "empty_route", finished=True)
            return self._step()
        marker = self._known(context, marker_sensor)

        if self._mode == "find_marker":
            if self._mode_started is None:
                self._start_marker_search(context, "waypoint_started")
            if self._deadline is not None and context.now >= self._deadline:
                self._start_marker_search(context, "marker_timeout_retry")
                self._set_status("Marker timeout; retrying", "marker_timeout_retry")
            if not isinstance(marker, Marker) or marker.position is None:
                self._record_navigation_marker(context, None)
                return self._step()
            self._record_navigation_marker(context, marker)
            request = self._request(
                context,
                "navigate",
                Action(ActionKind.LEFT_CLICK, point=marker.position),
                Priority.NAVIGATION,
                marker_sensor,
                f"walk to waypoint {self.route.waypoints[self.index].id}",
                tuple(self._target_sensor(target) for target in self.targets),
            )
            self._set_status("Sending movement", "marker_found")
            return self._step(request)

        if self._mode == "arriving":
            if isinstance(marker, Marker) and marker.reached:
                self._record_navigation_marker(context, marker)
                self._mode = "stand"
                self._mode_started = context.now
                self._deadline = context.now + self.config.stand_seconds
                self._set_status("Standing", "waypoint_arrived")
                return self._tick_route(context) if self.config.stand_seconds == 0 else self._step()
            self._record_navigation_marker(
                context, marker if isinstance(marker, Marker) else None
            )
            if self._deadline is not None and context.now >= self._deadline:
                if self._arrival_failures >= self.config.movement_attempts:
                    # Start another attempt batch without losing this waypoint.
                    self._arrival_failures = 0
                self._start_marker_search(context, "retrying_arrival")
                self._set_status("Arrival timeout; retrying", "retrying_arrival",
                                 navigation_attempt=self._arrival_failures)
            return self._step()

        if self._mode == "stand":
            if self._deadline is not None and context.now < self._deadline:
                return self._step()
            return self._complete_waypoint(context)

        return self._fail("invalid_state", f"unknown CaveBot state {self._mode!r}")

    def _complete_waypoint(self, context: FeatureContext) -> Step:
        completed = self._status.completed_waypoints + 1
        if not self.loop and self.index == len(self.route.waypoints) - 1:
            self._arrival_failures = 0
            self._last_progress_position = None
            self._last_progress_at = None
            self._set_status(
                "Finished",
                "route_complete",
                completed_waypoints=completed,
                target=None,
                finished=True,
                failed=False,
                error=None,
                navigation_state="idle",
                marker_position=None,
                navigation_attempt=0,
            )
            return self._step()
        self.index = (self.index + 1) % len(self.route.waypoints)
        if self.save_progress is not None:
            try:
                self.save_progress(self.route, self.index)
            except Exception as exc:
                return self._fail("checkpoint_failed", f"checkpoint failed: {exc}")
        self._arrival_failures = 0
        self._navigation_history = ()
        self._last_progress_position = None
        self._last_progress_at = None
        self._set_status(
            "Finding marker",
            "waypoint_complete",
            waypoint_index=self.index,
            waypoint_id=self.route.waypoints[self.index].id,
            completed_waypoints=completed,
            target=None,
            navigation_state="idle",
            marker_position=None,
            navigation_attempt=0,
        )
        self._mode = "find_marker"
        self._mode_started = context.now
        self._deadline = context.now + self.config.marker_timeout
        return self._step()

    def tick(self, context: FeatureContext) -> Step:
        if self._status.finished:
            return self._step()
        self._resume_evidence_clock(context)
        if self.config.walking and not self.route.waypoints:
            self._set_status("Stopped: empty route", "empty_route", finished=True)
            return self._step()
        if self._abort_requested:
            return self._finish_exit(immediate=True)
        if self._exit_requested and self._mode not in ("combat", "loot"):
            return self._finish_exit()
        self._expire_pending(context)
        if self._mode not in ("combat", "loot"):
            spec, _, evidence_error = self._choose_target(context)
            if evidence_error is not None:
                return self._wait_for_evidence(context, evidence_error)
            if spec is not None:
                cancel = self._enter_combat(context, spec)
                step = self._tick_combat(context)
                return Step(
                    requests=step.requests,
                    cancel_keys=cancel,
                    phase=step.phase,
                    finished=step.finished,
                    error=step.error,
                )
        if self._pending is not None:
            return self._step()
        if self._mode == "combat":
            return self._tick_combat(context)
        if self._mode == "loot":
            return self._tick_loot(context)
        if not self.config.walking:
            self._set_status("Waiting for targets", "stationary_combat")
            return self._step()
        return self._tick_route(context)

    def on_result(self, result: ActionResult) -> None:
        pending = self._pending
        if pending is None or result.key != pending.key:
            return
        if result.state is ResultState.FAILED:
            detail = result.reason or f"{pending.kind} input failed"
            self._pending = None
            self._fail("input_failed", detail)
            return
        if result.state in (ResultState.CANCELLED, ResultState.REJECTED):
            self._pending = None
            if pending.kind == "navigate" and self._mode != "combat":
                self._mode = "find_marker"
                self._mode_started = None
                self._deadline = None
                self._set_status("Finding marker", f"movement_{result.state.value}")
            return
        if result.state not in (ResultState.EXECUTED, ResultState.CONFIRMED):
            return

        self._pending = None
        settle = max(self.interval * 2, 0.4)
        if pending.kind == "navigate":
            # Arrival is deliberately separate from successful input.  It is
            # confirmed only by a later Marker(reached=True) observation.
            self._mode = "arriving"
            self._mode_started = result.at
            self._deadline = result.at + self.config.arrival_timeout
            self._arrival_failures += 1
            self._last_progress_at = result.at
            self._set_status(
                "Navigating",
                "movement_executed",
                navigation_state="moving",
                navigation_attempt=self._arrival_failures,
            )
        elif pending.kind == "select":
            target = self._active_target or ""
            self._selected_wait = (target, pending.evidence_frame, result.at + settle)
        elif pending.kind in ("follow", "follow_off"):
            self._follow_wait = (pending.evidence_frame, result.at + settle)
        elif pending.kind == "loot":
            self._loot_cursor += 1


# Short public name used by UI wiring; the descriptive name helps code search.
CaveBot = CaveBotFeature


__all__ = [
    "CaveBot",
    "CaveBotFeature",
    "CaveBotStatus",
    "SessionConfig",
    "TargetSpec",
]
