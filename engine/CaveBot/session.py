"""Headless, deterministic cavebot session orchestration.

The session knows policy and state transitions. Screen capture and client input are
injected, which keeps route execution testable without a game or desktop session.
"""

from __future__ import annotations

import threading
import time
import math
from numbers import Real
from contextlib import nullcontext
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Iterable, Optional, Protocol

from engine.CaveBot.routes import Route, Waypoint, WaypointAction


class SessionState(str, Enum):
    IDLE = "idle"
    FINDING_MARKER = "finding_marker"
    NAVIGATING = "navigating"
    FIGHTING = "fighting"
    LOOTING = "looting"
    CHECKPOINTING = "checkpointing"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class SessionStatus:
    state: SessionState = SessionState.IDLE
    reason: str = "not_started"
    waypoint_index: int = 0
    waypoint_id: Optional[str] = None
    target: Optional[str] = None
    completed_waypoints: int = 0


@dataclass(frozen=True, order=True)
class TargetSpec:
    priority: int
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("target name must not be empty")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise ValueError("target priority must be an integer")


@dataclass(frozen=True)
class SessionConfig:
    stand_seconds: float = 0.0
    poll_interval: float = 0.2
    marker_timeout: float = 8.0
    arrival_timeout: float = 20.0
    combat_timeout: float = 90.0
    movement_attempts: int = 3
    count_stable_samples: int = 2
    follow: bool = True
    loot: bool = True

    def __post_init__(self) -> None:
        for field_name in ("stand_seconds", "poll_interval", "marker_timeout", "arrival_timeout", "combat_timeout"):
            value = getattr(self, field_name)
            if not isinstance(value, Real) or isinstance(value, bool) or not math.isfinite(value):
                raise ValueError("{} must be a finite number".format(field_name))
        if self.stand_seconds < 0:
            raise ValueError("stand_seconds must not be negative")
        if self.poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        if self.marker_timeout <= 0 or self.arrival_timeout <= 0 or self.combat_timeout <= 0:
            raise ValueError("operation timeouts must be greater than zero")
        if (
            not isinstance(self.movement_attempts, int)
            or isinstance(self.movement_attempts, bool)
            or not isinstance(self.count_stable_samples, int)
            or isinstance(self.count_stable_samples, bool)
            or self.movement_attempts < 1
            or self.count_stable_samples < 1
        ):
            raise ValueError("attempt and stability limits must be at least one")


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)


class Perception(Protocol):
    def frame_scope(self): ...
    def find_marker(self, waypoint: Waypoint, frame: Any) -> Optional[tuple[int, int]]: ...
    def waypoint_reached(self, waypoint: Waypoint, frame: Any) -> bool: ...
    def target_count(self, name: str, frame: Any) -> int: ...
    def target_position(self, name: str, frame: Any) -> Optional[tuple[int, int]]: ...
    def target_selected(self, name: str, frame: Any) -> bool: ...
    def need_follow(self, frame: Any) -> bool: ...


class Actions(Protocol):
    def click_map(self, point: tuple[int, int], token: Any) -> None: ...
    def click_target(self, point: tuple[int, int], token: Any) -> None: ...
    def enable_follow(self, token: Any) -> None: ...
    def loot(self, token: Any) -> None: ...


class CaveBotSession:
    def __init__(
        self,
        route: Route,
        perception: Perception,
        actions: Actions,
        token: Any,
        *,
        targets: Iterable[TargetSpec] = (),
        config: Optional[SessionConfig] = None,
        start_index: int = 0,
        save_progress: Optional[Callable[[Route, int], Any]] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        if not isinstance(start_index, int) or isinstance(start_index, bool):
            raise ValueError("start index must be an integer")
        if route.waypoints and (start_index < 0 or start_index >= len(route.waypoints)):
            raise ValueError("start index is outside the route")
        self.route = route
        self.perception = perception
        self.actions = actions
        self.token = token
        self.targets = tuple(sorted(targets))
        self.config = config or SessionConfig()
        self.index = start_index
        self.save_progress = save_progress
        self.clock = clock or SystemClock()
        self._status = SessionStatus(waypoint_index=start_index)
        self._status_lock = threading.Lock()

    @property
    def status(self) -> SessionStatus:
        with self._status_lock:
            return self._status

    def _set_status(self, state: SessionState, reason: str, **changes: Any) -> None:
        with self._status_lock:
            self._status = replace(self._status, state=state, reason=reason, **changes)

    def _checkpoint(self) -> None:
        self.token.checkpoint()

    def _wait(self, seconds: float) -> None:
        self._checkpoint()
        if seconds <= 0:
            return
        waiter = getattr(self.token, "wait", None)
        if waiter is not None:
            waiter(seconds)
        else:
            self.clock.sleep(seconds)
        self._checkpoint()

    def _frame_scope(self):
        scope = getattr(self.perception, "frame_scope", None)
        return scope() if scope is not None else nullcontext(None)

    def _find_marker(self, waypoint: Waypoint) -> Optional[tuple[int, int]]:
        self._set_status(SessionState.FINDING_MARKER, "searching_marker")
        deadline = self.clock.monotonic() + self.config.marker_timeout
        while self.clock.monotonic() < deadline:
            self._checkpoint()
            with self._frame_scope() as frame:
                point = self.perception.find_marker(waypoint, frame)
            if point is not None:
                return point
            self._wait(self.config.poll_interval)
        return None

    def _choose_target(self, frame: Any) -> tuple[Optional[TargetSpec], int]:
        for target in self.targets:
            count = max(0, int(self.perception.target_count(target.name, frame)))
            if count:
                return target, count
        return None, 0

    def _fight(self, initial: TargetSpec) -> bool:
        self._set_status(SessionState.FIGHTING, "target_interrupted_walk", target=initial.name)
        deadline = self.clock.monotonic() + self.config.combat_timeout
        active_name: Optional[str] = None
        confirmed_count: Optional[int] = None
        candidate_count: Optional[int] = None
        candidate_samples = 0
        empty_samples = 0

        while self.clock.monotonic() < deadline:
            self._checkpoint()
            with self._frame_scope() as frame:
                target, count = self._choose_target(frame)
                if target is None:
                    empty_samples += 1
                    if empty_samples >= self.config.count_stable_samples:
                        if self.config.loot and confirmed_count:
                            self._set_status(SessionState.LOOTING, "stable_target_count_drop", target=active_name)
                            self._checkpoint()
                            self.actions.loot(self.token)
                        self._set_status(SessionState.NAVIGATING, "combat_clear", target=None)
                        return True
                else:
                    empty_samples = 0
                    if active_name is not None and active_name != target.name:
                        confirmed_count = None
                        candidate_count, candidate_samples = None, 0
                    if active_name != target.name or not self.perception.target_selected(target.name, frame):
                        point = self.perception.target_position(target.name, frame)
                        if point is not None:
                            self._checkpoint()
                            self.actions.click_target(point, self.token)
                    active_name = target.name
                    if self.config.follow and self.perception.need_follow(frame):
                        self._checkpoint()
                        self.actions.enable_follow(self.token)

                    if confirmed_count is None:
                        confirmed_count = count
                    elif count == candidate_count:
                        candidate_samples += 1
                    elif count != confirmed_count:
                        candidate_count, candidate_samples = count, 1
                    else:
                        candidate_count, candidate_samples = None, 0

                    if candidate_count is not None and candidate_samples >= self.config.count_stable_samples:
                        previous = confirmed_count
                        confirmed_count = candidate_count
                        candidate_count, candidate_samples = None, 0
                        if self.config.loot and confirmed_count < previous:
                            # A stable count decrease is only a conservative loot hint, not corpse confirmation.
                            self._set_status(SessionState.LOOTING, "stable_target_count_drop", target=target.name)
                            self._checkpoint()
                            self.actions.loot(self.token)
                            self._set_status(SessionState.FIGHTING, "combat_continues", target=target.name)
            self._wait(self.config.poll_interval)

        self._set_status(SessionState.FAILED, "combat_timeout", target=active_name or initial.name)
        return False

    def _wait_for_arrival(self, waypoint: Waypoint) -> str:
        deadline = self.clock.monotonic() + self.config.arrival_timeout
        while self.clock.monotonic() < deadline:
            self._checkpoint()
            with self._frame_scope() as frame:
                target, _ = self._choose_target(frame)
                if target is None and self.perception.waypoint_reached(waypoint, frame):
                    return "arrived"
            if target is not None:
                return "target:" + target.name
            self._wait(self.config.poll_interval)
        return "timeout"

    def _execute_waypoint(self, waypoint: Waypoint) -> bool:
        if waypoint.action is not WaypointAction.WALK:
            # Route validation should prevent this; retain a runtime guard as an input safety boundary.
            self._set_status(SessionState.FAILED, "unsupported_action", waypoint_id=waypoint.id)
            return False
        movement_failures = 0
        while movement_failures < self.config.movement_attempts:
            self._checkpoint()
            point = self._find_marker(waypoint)
            if point is None:
                self._set_status(SessionState.FAILED, "marker_timeout", waypoint_id=waypoint.id)
                return False
            self._set_status(SessionState.NAVIGATING, "marker_found", waypoint_id=waypoint.id)
            self._checkpoint()
            self.actions.click_map(point, self.token)
            outcome = self._wait_for_arrival(waypoint)
            if outcome == "arrived":
                self._wait(self.config.stand_seconds)
                return True
            if outcome.startswith("target:"):
                target_name = outcome.split(":", 1)[1]
                target = next(target for target in self.targets if target.name == target_name)
                if not self._fight(target):
                    return False
                # Targeting cancels client walking; locate and click the same waypoint again.
                continue
            movement_failures += 1
        self._set_status(SessionState.FAILED, "arrival_timeout", waypoint_id=waypoint.id)
        return False

    def run(self, *, max_completed_waypoints: Optional[int] = None) -> SessionStatus:
        if max_completed_waypoints is not None and (
            not isinstance(max_completed_waypoints, int)
            or isinstance(max_completed_waypoints, bool)
            or max_completed_waypoints < 0
        ):
            raise ValueError("completion limit must be a non-negative integer or None")
        if not self.route.waypoints:
            self._set_status(SessionState.STOPPED, "empty_route")
            return self.status
        completed = 0
        try:
            while max_completed_waypoints is None or completed < max_completed_waypoints:
                self._checkpoint()
                waypoint = self.route.waypoints[self.index]
                self._set_status(
                    SessionState.FINDING_MARKER,
                    "waypoint_started",
                    waypoint_index=self.index,
                    waypoint_id=waypoint.id,
                    target=None,
                )
                if not self._execute_waypoint(waypoint):
                    return self.status
                self.index = (self.index + 1) % len(self.route.waypoints)
                completed += 1
                self._set_status(
                    SessionState.CHECKPOINTING,
                    "waypoint_complete",
                    waypoint_index=self.index,
                    waypoint_id=self.route.waypoints[self.index].id,
                    completed_waypoints=completed,
                )
                self._checkpoint()
                if self.save_progress is not None:
                    self.save_progress(self.route, self.index)
            self._set_status(SessionState.STOPPED, "completion_limit", completed_waypoints=completed)
        except BaseException as exc:
            cancelled = getattr(self.token, "cancelled", False)
            cancelled = cancelled() if callable(cancelled) else cancelled
            if cancelled:
                self._set_status(SessionState.STOPPED, "cancelled")
                return self.status
            self._set_status(SessionState.FAILED, "exception:{}".format(type(exc).__name__))
            raise
        return self.status
