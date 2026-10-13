"""Closed-loop hunting lifecycle composed over the shared client session.

The policy owns no capture, scheduler, native input, or worker.  Travel is
delegated to finite/looping ``CaveBotFeature`` instances and every configured
interaction is reconciled from a later shared snapshot before progress is
committed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import json
import math
from numbers import Real
import os
from pathlib import Path
import tempfile
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from features.cavebot import CaveBotFeature, SessionConfig, TargetSpec
from tibia_client.hunt_plan import (
    ConditionField,
    ConditionOp,
    GuardedAction,
    HuntPlan,
    PlannedActionKind,
)
from tibia_client.models import (
    Action,
    ActionKind,
    ActionResult,
    FeatureContext,
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


class CyclePhase(str, Enum):
    PREPARE = "prepare"
    OUTBOUND_BEFORE = "outbound_before"
    OUTBOUND = "outbound"
    OUTBOUND_AFTER = "outbound_after"
    HUNT = "hunt"
    RETURN_BEFORE = "return_before"
    RETURN = "return"
    RETURN_AFTER = "return_after"
    DEPOSIT = "deposit"
    SELL = "sell"
    REFILL = "refill"
    MANUAL = "manual"
    FINISHED = "finished"
    FAILED = "failed"


class RecoveryOrigin(str, Enum):
    WARM_PAUSE = "warm_pause"
    CHECKPOINT = "checkpoint"


@dataclass
class RecoveryState:
    origin: RecoveryOrigin
    baseline_frame: int
    reason: str
    candidates: tuple[int, ...] = ()
    selected_anchor: int | None = None


@dataclass(frozen=True)
class HuntCycleStatus:
    phase: str
    reason: str
    supply_counts: Mapping[str, int | None]
    pending_confirmation: str | None = None
    manual_required: bool = False
    manual_reason: str | None = None
    return_requested: bool = False
    return_mode: str | None = None
    cycle_count: int = 0
    action_id: str | None = None
    refill_round: int = 0
    finished: bool = False
    failed: bool = False
    error: str | None = None
    recovery_reason: str | None = None
    recovery_candidates: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "supply_counts", MappingProxyType(dict(self.supply_counts)))


@dataclass
class _PendingAction:
    action: GuardedAction
    baselines: dict[str, bool | int]
    baseline_frame: int
    attempts: int
    issued_at: float
    request_key: str
    dispatched_at: float | None = None
    deadline: float | None = None
    extra: tuple[tuple[str, ConditionField, ConditionOp], ...] = ()
    loot_item: str | None = None
    matched_count_delta: tuple[str, str] | None = None
    dispatch_unknown: bool = False


class HuntCheckpointError(ValueError):
    pass


class HuntCycleFeature:
    """Run prepare/travel/hunt/return/depot/sell/refill as one CaveBot feature."""

    name = "CaveBot"
    interval = 0.2
    _MAX_AGE = 0.5
    _CHECKPOINT_VERSION = 1

    def __init__(
        self,
        plan: HuntPlan,
        bindings: Mapping[str, Sensor],
        *,
        binding_revision: str,
        targets: Iterable[TargetSpec] = (),
        cavebot_config: SessionConfig | None = None,
        map_region: Iterable[int] = (),
        battle_region: Iterable[int] = (),
        checkpoint_path: str | os.PathLike[str] | None = None,
        resume: bool = True,
    ) -> None:
        if not isinstance(plan, HuntPlan):
            raise TypeError("plan must be a HuntPlan")
        if not isinstance(binding_revision, str) or not binding_revision.strip():
            raise ValueError("binding_revision must be the non-empty effective binding fingerprint")
        try:
            checked = dict(bindings)
        except (TypeError, ValueError):
            raise ValueError("bindings must map plan ids to Sensor values") from None
        missing = set(plan.sensor_ids()) - set(checked)
        if missing:
            raise ValueError("missing sensor binding(s): " + ", ".join(sorted(missing)))
        for sensor_id, sensor in checked.items():
            if not isinstance(sensor_id, str) or not isinstance(sensor, Sensor):
                raise ValueError("bindings must map text ids to Sensor values")
            if sensor_id in plan.sensor_ids() and (
                getattr(sensor.kind, "value", None) != "visual" or not sensor.name.strip()
            ):
                raise ValueError(
                    f"binding {sensor_id!r} must be a VISUAL Sensor with a non-empty detector name"
                )
        self.plan = plan
        self.bindings = MappingProxyType(checked)
        self.binding_revision = binding_revision.strip()
        self.targets = tuple(targets)
        if any(not isinstance(item, TargetSpec) for item in self.targets):
            raise TypeError("targets must contain TargetSpec values")
        config = cavebot_config or SessionConfig()
        self._cavebot_config = replace(config, loot=False)
        self.interval = self._cavebot_config.poll_interval
        self._map_region = tuple(map_region)
        self._battle_region = tuple(battle_region)
        self._checkpoint_path = Path(checkpoint_path) if checkpoint_path is not None else None

        self._phase = CyclePhase.PREPARE
        self._resume_phase: CyclePhase | None = None
        self._action_index = 0
        self._segment_index = 0
        self._cycle_count = 0
        self._refill_round = 0
        self._request_sequence = 0
        self._pending: _PendingAction | None = None
        self._inner: CaveBotFeature | None = None
        self._return_requested = False
        self._return_mode: str | None = None
        self._combat_seen = False
        self._looting = False
        self._loot_opened = False
        self._loot_moves = 0
        self._loot_started: float | None = None
        self._loot_evidence_wait_started: float | None = None
        self._paused = False
        self._last_frame_id = -1
        self._recovery: RecoveryState | None = None
        self._resume_pending: dict[str, Any] | None = None
        self._manual_origin: CyclePhase | None = None
        self._manual_reason: str | None = None
        self._manual_check_requested = False
        self._stopped = False
        self._supply_counts: dict[str, int | None] = {item.id: None for item in plan.supplies}
        self._status = HuntCycleStatus(CyclePhase.PREPARE.value, "not_started", self._supply_counts)
        if type(resume) is not bool:
            raise ValueError("resume must be boolean")
        if resume:
            self._load_checkpoint()

    @property
    def status(self) -> HuntCycleStatus:
        return self._status

    @property
    def finished(self) -> bool:
        return self._status.finished

    @property
    def failed(self) -> bool:
        return self._status.failed

    @property
    def error(self) -> str | None:
        return self._status.error

    def inspection_status(self) -> dict[str, Any]:
        status = self._status
        return {
            "phase": status.phase,
            "reason": status.reason,
            "supply_counts": dict(status.supply_counts),
            "pending_confirmation": status.pending_confirmation,
            "manual_required": status.manual_required,
            "manual_reason": status.manual_reason,
            "return_requested": status.return_requested,
            "return_mode": status.return_mode,
            "cycle_count": status.cycle_count,
            "action_id": status.action_id,
            "refill_round": status.refill_round,
            "finished": status.finished,
            "failed": status.failed,
            "error": status.error,
            "recovery_reason": status.recovery_reason,
            "recovery_candidates": status.recovery_candidates,
        }

    def _set_status(self, reason: str, **changes: Any) -> None:
        values = {
            "phase": self._phase.value,
            "reason": reason,
            "supply_counts": self._supply_counts,
            "pending_confirmation": self._pending.action.id if self._pending else None,
            "manual_required": self._phase is CyclePhase.MANUAL,
            "manual_reason": self._manual_reason,
            "return_requested": self._return_requested,
            "return_mode": self._return_mode,
            "cycle_count": self._cycle_count,
            "action_id": self._current_action().id if self._current_action() else None,
            "refill_round": self._refill_round,
            "finished": self._phase is CyclePhase.FINISHED,
            "failed": self._phase is CyclePhase.FAILED,
            "error": None,
            "recovery_reason": self._recovery.reason if self._recovery else None,
            "recovery_candidates": self._recovery.candidates if self._recovery else (),
        }
        values.update(changes)
        self._status = HuntCycleStatus(**values)

    def required_operations(self) -> tuple[str, ...]:
        operations = {item.kind.value for group in self._all_action_groups() for item in group}
        if self.plan.loot is not None:
            operations.update(("right_click", "drag"))
        operations.add("left_click")  # CaveBot travel and combat selection.
        if self.targets:
            operations.add("battle_list_click")
        return tuple(sorted(operations))

    def sensors(self) -> tuple[Sensor, ...]:
        result = list(self.bindings[sensor_id] for sensor_id in self.plan.sensor_ids())
        if self._inner is not None:
            result.extend(self._inner.sensors())
            if self._recovery is not None and self._recovery.origin is RecoveryOrigin.CHECKPOINT and self._phase in (
                CyclePhase.OUTBOUND,
                CyclePhase.HUNT,
                CyclePhase.RETURN,
            ):
                result.extend(self._inner.anchor_sensors())
        return tuple(dict.fromkeys(result))

    def request_return(self, now: bool = False) -> None:
        if type(now) is not bool:
            raise ValueError("now must be boolean")
        if self._phase in (CyclePhase.FINISHED, CyclePhase.FAILED):
            return
        if self._phase in (
            CyclePhase.PREPARE,
            CyclePhase.DEPOSIT,
            CyclePhase.SELL,
            CyclePhase.REFILL,
        ):
            self._set_status("return_already_at_destination")
            return
        self._upgrade_return_intent("now" if now else "after_fight")
        if self._phase is CyclePhase.HUNT and self._inner is not None:
            if self._return_mode == "now":
                self._inner.request_abort()
            else:
                self._inner.request_exit()
        if self._phase in (
            CyclePhase.RETURN_BEFORE,
            CyclePhase.RETURN,
            CyclePhase.RETURN_AFTER,
        ):
            reason = "return_already_in_progress"
        elif self._phase in (
            CyclePhase.OUTBOUND_BEFORE,
            CyclePhase.OUTBOUND,
            CyclePhase.OUTBOUND_AFTER,
        ):
            reason = "return_queued_until_outbound_route_complete"
        else:
            reason = "operator_return_requested"
        self._set_status(reason)
        self._save_checkpoint()

    def _upgrade_return_intent(self, mode: str) -> None:
        rank = {None: 0, "after_fight": 1, "now": 2}
        if mode not in ("after_fight", "now"):
            raise ValueError("return mode is invalid")
        self._return_requested = True
        if rank[mode] > rank[self._return_mode]:
            self._return_mode = mode

    def select_recovery_anchor(self, waypoint_index: int) -> bool:
        """Choose a currently observed anchor for checkpoint reconstruction."""

        if not isinstance(waypoint_index, int) or isinstance(waypoint_index, bool):
            raise ValueError("waypoint_index must be an integer")
        recovery = self._recovery
        if recovery is None or recovery.origin is not RecoveryOrigin.CHECKPOINT:
            return False
        if waypoint_index not in recovery.candidates:
            recovery.selected_anchor = None
            recovery.reason = (
                "route_anchor_waiting_for_fresh_evidence"
                if not recovery.candidates
                else "route_anchor_ambiguous_select_candidate"
            )
            self._set_status("route_anchor_selection_rejected")
            return False
        recovery.selected_anchor = waypoint_index
        recovery.reason = "route_anchor_selection_pending_fresh_evidence"
        self._set_status("route_anchor_selection_pending_fresh_evidence")
        return True

    def request_recheck(self) -> bool:
        """Request an immediate read-only recovery or reconciliation poll."""

        if self._phase is CyclePhase.MANUAL:
            self._manual_check_requested = True
            self._set_status("manual_ack_waiting_for_fresh_checks")
            return True
        if self._recovery is not None:
            self._set_status("recovery_recheck_requested")
            return True
        return False

    def acknowledge_manual(self, *, operator_reconciled: bool = False) -> bool:
        if type(operator_reconciled) is not bool:
            raise ValueError("operator_reconciled must be boolean")
        if self._phase is not CyclePhase.MANUAL:
            return False
        if not operator_reconciled:
            return self.request_recheck()
        origin = self._manual_origin or CyclePhase.PREPARE
        self._phase = origin
        self._manual_origin = None
        self._manual_reason = None
        if self._pending is not None:
            pending = self._pending
            self._pending = None
            self._complete_pending_effect(pending)
        recovery_origin = (
            RecoveryOrigin.CHECKPOINT
            if self._inner is None
            and origin in (CyclePhase.OUTBOUND, CyclePhase.HUNT, CyclePhase.RETURN)
            else RecoveryOrigin.WARM_PAUSE
        )
        self._recovery = RecoveryState(
            recovery_origin,
            self._last_frame_id,
            "operator_reconciliation_fresh_observation_required",
        )
        self._set_status("operator_reconciled")
        self._save_checkpoint()
        return True

    def on_pause(
        self, *, cancelled_before_dispatch: tuple[str, ...] = ()
    ) -> None:
        if not isinstance(cancelled_before_dispatch, tuple) or any(
            not isinstance(key, str) or not key
            for key in cancelled_before_dispatch
        ):
            raise ValueError(
                "cancelled_before_dispatch must contain non-empty request keys"
            )
        self._paused = True
        if self._pending is not None:
            if self._pending.request_key in cancelled_before_dispatch:
                self._pending = None
            elif self._pending.dispatched_at is None:
                # The native call runs outside the feature callback lock.  If
                # the session did not prove this key was still queued, missing
                # feedback leaves its effect uncertain and it must not retry.
                self._pending.dispatch_unknown = True
        if self._inner is not None:
            self._inner.on_pause()
        self._set_status("paused")
        self._save_checkpoint()

    def on_resume(self, *, paused_for: float = 0.0) -> None:
        if (
            not isinstance(paused_for, Real)
            or isinstance(paused_for, bool)
            or not math.isfinite(paused_for)
            or paused_for < 0
        ):
            raise ValueError("paused_for must be a finite non-negative number")
        self._paused = False
        if self._pending is not None and self._pending.deadline is not None:
            self._pending.deadline += float(paused_for)
        if self._inner is not None:
            self._inner.on_resume(paused_for=paused_for)
        if self._recovery is None or self._recovery.origin is RecoveryOrigin.WARM_PAUSE:
            self._recovery = RecoveryState(
                RecoveryOrigin.WARM_PAUSE,
                self._last_frame_id,
                "warm_resume_waiting_for_new_frame",
            )
        self._set_status("resuming_fresh_observation_required")

    def on_stop(self) -> None:
        self._stopped = True
        self._set_status("stopped")
        self._save_checkpoint()

    def _all_action_groups(self) -> tuple[tuple[GuardedAction, ...], ...]:
        return (
            self.plan.prepare,
            *(segment.before for segment in self.plan.outbound),
            *(segment.after for segment in self.plan.outbound),
            *(segment.before for segment in self.plan.return_segments),
            *(segment.after for segment in self.plan.return_segments),
            self.plan.deposit,
            self.plan.sell,
            self.plan.refill,
        )

    def _actions_for(
        self, phase: CyclePhase, segment_index: int
    ) -> tuple[GuardedAction, ...]:
        outbound = self.plan.outbound[min(segment_index, len(self.plan.outbound) - 1)]
        returning = self.plan.return_segments[
            min(segment_index, len(self.plan.return_segments) - 1)
        ]
        return {
            CyclePhase.PREPARE: self.plan.prepare,
            CyclePhase.OUTBOUND_BEFORE: outbound.before,
            CyclePhase.OUTBOUND_AFTER: outbound.after,
            CyclePhase.RETURN_BEFORE: returning.before,
            CyclePhase.RETURN_AFTER: returning.after,
            CyclePhase.DEPOSIT: self.plan.deposit,
            CyclePhase.SELL: self.plan.sell,
            CyclePhase.REFILL: self.plan.refill,
        }.get(phase, ())

    def _actions(self) -> tuple[GuardedAction, ...]:
        return self._actions_for(self._phase, self._segment_index)

    def _current_action(self) -> GuardedAction | None:
        actions = self._actions()
        return actions[self._action_index] if self._action_index < len(actions) else None

    @staticmethod
    def _visual(value: Any) -> tuple[bool, tuple[int, int] | None, int | None] | None:
        if not isinstance(value, Mapping):
            return None
        present = value.get("present")
        position = value.get("position")
        count = value.get("count")
        if type(present) is not bool:
            return None
        if position is not None:
            try:
                position = tuple(position)
            except TypeError:
                return None
            if len(position) != 2 or any(type(item) is not int or item < 0 for item in position):
                return None
        if count is not None and (type(count) is not int or count < 0):
            return None
        return present, position, count

    def _read(self, context: FeatureContext, sensor_id: str) -> tuple[bool, tuple[int, int] | None, int | None] | None:
        try:
            value = context.require(self.bindings[sensor_id], self._MAX_AGE)
        except ObservationUnavailable:
            return None
        return self._visual(value)

    def _field(self, context: FeatureContext, sensor_id: str, field: ConditionField) -> bool | int | None:
        value = self._read(context, sensor_id)
        if value is None:
            return None
        return value[0] if field is ConditionField.PRESENT else value[2]

    @staticmethod
    def _matches(current: bool | int, baseline: bool | int | None, op: ConditionOp, expected: Any = None) -> bool:
        if op is ConditionOp.EQ:
            return current == expected
        if op is ConditionOp.NE:
            return current != expected
        if op is ConditionOp.GTE:
            return type(current) is int and current >= expected
        if op is ConditionOp.LTE:
            return type(current) is int and current <= expected
        if baseline is None:
            return False
        if op is ConditionOp.CHANGED:
            return current != baseline
        if op is ConditionOp.INCREASED:
            return type(current) is int and type(baseline) is int and current > baseline
        if op is ConditionOp.DECREASED:
            return type(current) is int and type(baseline) is int and current < baseline
        return False

    def _supply_observations(self, context: FeatureContext) -> bool:
        known = True
        for supply in self.plan.supplies:
            visual = self._read(context, supply.sensor_id)
            count = None if visual is None or not visual[0] else visual[2]
            self._supply_counts[supply.id] = count
            if count is None and not supply.optional:
                known = False
        return known

    def _make_action(self, context: FeatureContext, action: GuardedAction) -> Action | None:
        kind = ActionKind(action.kind.value)
        if action.kind is PlannedActionKind.PRESS:
            return Action(kind, key=action.key)
        source = self._read(context, action.target_id or "")
        if source is None or not source[0] or source[1] is None:
            return None
        if action.kind is PlannedActionKind.DRAG:
            destination = self._read(context, action.destination_id or "")
            if destination is None or not destination[0] or destination[1] is None:
                return None
            return Action(kind, point=source[1], destination=destination[1])
        return Action(kind, point=source[1])

    def _dispatch_action(
        self,
        context: FeatureContext,
        action: GuardedAction,
        *,
        attempts: int = 1,
        extra: tuple[tuple[str, ConditionField, ConditionOp], ...] = (),
        loot_item: str | None = None,
        matched_count_delta: tuple[str, str] | None = None,
    ) -> Step:
        ids = set(action.require_present)
        if action.target_id:
            ids.add(action.target_id)
        if action.destination_id:
            ids.add(action.destination_id)
        ids.add(action.confirmation.sensor_id)
        ids.update(item[0] for item in extra)
        readings = {sensor_id: self._read(context, sensor_id) for sensor_id in ids}
        if any(value is None for value in readings.values()):
            self._set_status("waiting_for_fresh_visual_evidence")
            return self._step()
        if any(not readings[sensor_id][0] for sensor_id in action.require_present):  # type: ignore[index]
            self._set_status("waiting_for_guard")
            return self._step()
        native = self._make_action(context, action)
        if native is None:
            self._set_status("waiting_for_action_position")
            return self._step()
        condition = action.confirmation
        baseline = self._field(context, condition.sensor_id, condition.field)
        if baseline is None:
            self._set_status("waiting_for_confirmation_baseline")
            return self._step()
        if condition.op not in (ConditionOp.CHANGED, ConditionOp.INCREASED, ConditionOp.DECREASED) and self._matches(baseline, None, condition.op, condition.value):
            self._action_completed("already_confirmed")
            return self._step()
        baselines: dict[str, bool | int] = {condition.sensor_id: baseline}
        for sensor_id, field, _ in extra:
            value = self._field(context, sensor_id, field)
            if value is None:
                self._set_status("waiting_for_confirmation_baseline")
                return self._step()
            baselines[sensor_id] = value
        self._request_sequence += 1
        key = f"hunt:{action.id}:{self._request_sequence}"
        self._pending = _PendingAction(
            action=action,
            baselines=baselines,
            baseline_frame=context.snapshot.frame_id,
            attempts=attempts,
            issued_at=context.now,
            request_key=key,
            extra=extra,
            loot_item=loot_item,
            matched_count_delta=matched_count_delta,
        )
        self._set_status("dispatching_guarded_action")
        self._save_checkpoint()  # Persist intent before the session can dispatch it.
        requirements = tuple(Requirement(self.bindings[item], self._MAX_AGE) for item in sorted(ids))
        return self._step(Request(key, native, Priority.LOOT if loot_item else Priority.NAVIGATION, self._MAX_AGE, requirements, reason=f"hunt lifecycle action {action.id}"))

    def _pending_confirmed(self, context: FeatureContext, pending: _PendingAction) -> bool | None:
        if context.snapshot.frame_id <= pending.baseline_frame:
            return None
        condition = pending.action.confirmation
        current = self._field(context, condition.sensor_id, condition.field)
        if current is None:
            return None
        if not self._matches(current, pending.baselines.get(condition.sensor_id), condition.op, condition.value):
            return False
        for sensor_id, field, op in pending.extra:
            extra_current = self._field(context, sensor_id, field)
            if extra_current is None:
                return None
            if not self._matches(extra_current, pending.baselines.get(sensor_id), op):
                return False
        if pending.matched_count_delta is not None:
            source_id, destination_id = pending.matched_count_delta
            source = self._field(context, source_id, ConditionField.COUNT)
            destination = self._field(context, destination_id, ConditionField.COUNT)
            source_baseline = pending.baselines.get(source_id)
            destination_baseline = pending.baselines.get(destination_id)
            if not all(
                type(value) is int
                for value in (source, destination, source_baseline, destination_baseline)
            ):
                return None
            source_delta = source_baseline - source  # type: ignore[operator]
            destination_delta = destination - destination_baseline  # type: ignore[operator]
            if source_delta <= 0 or source_delta != destination_delta:
                return False
        return True

    def _pending_unchanged(self, context: FeatureContext, pending: _PendingAction) -> bool | None:
        condition = pending.action.confirmation
        checks = ((condition.sensor_id, condition.field),) + tuple(
            (sensor_id, field) for sensor_id, field, _ in pending.extra
        )
        for sensor_id, field in checks:
            current = self._field(context, sensor_id, field)
            if current is None:
                return None
            if current != pending.baselines.get(sensor_id):
                return False
        return True

    def _tick_pending(self, context: FeatureContext) -> Step:
        pending = self._pending
        if pending is None:
            return self._step()
        if pending.dispatched_at is None:
            if pending.dispatch_unknown:
                confirmed = self._pending_confirmed(context, pending)
                if confirmed is True:
                    self._pending = None
                    self._complete_pending_effect(pending)
                    self._set_status("machine_confirmed_after_uncertain_dispatch")
                    self._save_checkpoint()
                    return self._step()
                if context.now - pending.issued_at >= max(1.0, self.interval * 4):
                    return self._manual("dispatch_result_unknown")
                self._set_status("reconciling_uncertain_dispatch")
                return self._step()
            if context.now - pending.issued_at >= max(1.0, self.interval * 4):
                return self._manual("dispatch_result_unknown")
            self._set_status("waiting_for_dispatch_result")
            return self._step()
        confirmed = self._pending_confirmed(context, pending)
        if confirmed is True:
            self._pending = None
            self._complete_pending_effect(pending)
            self._set_status("machine_confirmed")
            self._save_checkpoint()
            return self._step()
        if pending.deadline is not None and context.now < pending.deadline:
            self._set_status("waiting_for_postcondition")
            return self._step()
        if confirmed is None:
            return self._manual("confirmation_unknown_after_timeout")
        # A fresh unchanged observation proves that the intended effect did not
        # happen; only then may a bounded retry be issued.
        unchanged = self._pending_unchanged(context, pending)
        if unchanged is None:
            return self._manual("confirmation_unknown_after_timeout")
        if not unchanged:
            return self._manual("postcondition_changed_unexpectedly")
        if (
            self._phase is CyclePhase.HUNT
            and self._return_requested
            and self._return_mode == "now"
            and pending.loot_item is not None
        ):
            self._pending = None
            self._clear_loot_state()
            self._set_status("immediate_return_suppressed_loot_retry")
            self._save_checkpoint()
            return self._step()
        if pending.attempts <= pending.action.retries:
            action = pending.action
            attempts = pending.attempts + 1
            extra = pending.extra
            loot_item = pending.loot_item
            matched_count_delta = pending.matched_count_delta
            self._pending = None
            return self._dispatch_action(
                context,
                action,
                attempts=attempts,
                extra=extra,
                loot_item=loot_item,
                matched_count_delta=matched_count_delta,
            )
        return self._manual("postcondition_not_reached")

    def _complete_pending_effect(self, pending: _PendingAction) -> None:
        if pending.loot_item == "__open__":
            self._loot_opened = True
        elif pending.loot_item is not None:
            self._loot_moves += 1
        else:
            self._action_index += 1

    def _action_completed(self, reason: str) -> None:
        self._action_index += 1
        self._set_status(reason)
        self._save_checkpoint()

    def _step(self, request: Request | None = None, *, cancel_keys: tuple[str, ...] = ()) -> Step:
        return Step(
            () if request is None else (request,),
            cancel_keys,
            self._status.phase,
            self._status.finished,
            self._status.error,
        )

    def _manual(self, reason: str) -> Step:
        if self._phase is not CyclePhase.MANUAL:
            self._manual_origin = self._phase
        self._phase = CyclePhase.MANUAL
        self._manual_reason = reason
        self._set_status("manual_reconciliation_required")
        self._save_checkpoint()
        return self._step()

    def _fail(self, reason: str, error: str) -> Step:
        self._phase = CyclePhase.FAILED
        self._set_status(reason, failed=True, error=error)
        self._save_checkpoint()
        return self._step()

    def _new_inner(self, phase: CyclePhase) -> CaveBotFeature:
        if phase is CyclePhase.OUTBOUND:
            route, targets, loop = self.plan.outbound[self._segment_index].route, (), False
        elif phase is CyclePhase.RETURN:
            route, targets, loop = self.plan.return_segments[self._segment_index].route, (), False
        else:
            route, targets, loop = self.plan.hunt, self.targets, True
        return CaveBotFeature(
            route,
            targets,
            config=self._cavebot_config,
            loop=loop,
            save_progress=None,
            map_region=self._map_region,
            battle_region=self._battle_region,
        )

    def _enter(self, phase: CyclePhase, reason: str) -> None:
        self._phase = phase
        self._action_index = 0
        self._inner = None
        self._set_status(reason)
        self._save_checkpoint()

    def _finish_action_phase(self, context: FeatureContext) -> Step:
        transitions = {
            CyclePhase.PREPARE: CyclePhase.OUTBOUND_BEFORE,
            CyclePhase.OUTBOUND_BEFORE: CyclePhase.OUTBOUND,
            CyclePhase.RETURN_BEFORE: CyclePhase.RETURN,
            CyclePhase.DEPOSIT: CyclePhase.SELL,
            CyclePhase.SELL: CyclePhase.REFILL,
        }
        if self._phase is CyclePhase.OUTBOUND_AFTER:
            if self._segment_index + 1 < len(self.plan.outbound):
                self._segment_index += 1
                self._enter(CyclePhase.OUTBOUND_BEFORE, "next_outbound_segment")
            else:
                self._segment_index = 0
                self._enter(CyclePhase.HUNT, "outbound_complete")
            return self._step()
        if self._phase is CyclePhase.RETURN_AFTER:
            if self._segment_index + 1 < len(self.plan.return_segments):
                self._segment_index += 1
                self._enter(CyclePhase.RETURN_BEFORE, "next_return_segment")
            else:
                self._segment_index = 0
                self._enter(CyclePhase.DEPOSIT, "return_complete")
            return self._step()
        if self._phase is CyclePhase.REFILL:
            if not self._supply_observations(context):
                self._set_status("waiting_for_supply_counts")
                return self._step()
            below = [supply for supply in self.plan.supplies if self._supply_counts[supply.id] is not None and self._supply_counts[supply.id] < supply.refill_to]
            if below:
                if self._refill_round >= self.plan.max_refill_rounds:
                    return self._manual("refill_target_not_reached")
                self._refill_round += 1
                self._action_index = 0
                self._set_status("refill_target_pending")
                self._save_checkpoint()
                return self._step()
            self._cycle_count += 1
            self._refill_round = 0
            self._segment_index = 0
            self._return_requested = False
            self._return_mode = None
            self._enter(CyclePhase.OUTBOUND_BEFORE, "cycle_resuming")
            return self._step()
        next_phase = transitions[self._phase]
        self._enter(next_phase, "phase_complete")
        return self._step()

    def _tick_action_phase(self, context: FeatureContext) -> Step:
        action = self._current_action()
        if action is None:
            return self._finish_action_phase(context)
        return self._dispatch_action(context, action)

    def _tick_route(self, context: FeatureContext) -> Step:
        if self._inner is None:
            self._inner = self._new_inner(self._phase)
            self._set_status("route_starting")
            return self._step()
        step = self._inner.tick(context)
        self._set_status(self._inner.status.reason)
        if self._inner.failed:
            return self._fail("route_failed", self._inner.error or "route failed")
        if self._inner.finished:
            if self._phase is CyclePhase.OUTBOUND:
                self._enter(CyclePhase.OUTBOUND_AFTER, "outbound_complete")
            else:
                self._enter(CyclePhase.RETURN_AFTER, "return_complete")
            return self._step(cancel_keys=step.cancel_keys)
        return Step(step.requests, step.cancel_keys, self._status.phase, False, step.error)

    def _return_due(self) -> tuple[bool, str]:
        emergencies = []
        thresholds = []
        for supply in self.plan.supplies:
            count = self._supply_counts[supply.id]
            if count is not None and count <= supply.emergency_reserve:
                emergencies.append(supply.id)
            elif count is not None and count < supply.return_below:
                thresholds.append(supply.id)
        if emergencies:
            self._upgrade_return_intent("now")
            return True, f"emergency_reserve:{emergencies[0]}"
        if thresholds:
            self._upgrade_return_intent("after_fight")
            return True, f"return_threshold:{thresholds[0]}"
        if self._return_requested:
            return True, "operator_return"
        return False, ""

    def _loot_wait(self, context: FeatureContext, reason: str) -> Step:
        policy = self.plan.loot
        if policy is None:
            self._looting = False
            return self._step()
        if self._loot_evidence_wait_started is None:
            self._loot_evidence_wait_started = context.now
        if context.now - self._loot_evidence_wait_started >= max(
            policy.open_timeout, policy.transfer_timeout
        ):
            self._looting = False
            self._loot_opened = False
            self._loot_started = None
            self._loot_evidence_wait_started = None
            self._set_status("loot_evidence_timeout_skipped")
            return self._step()
        self._set_status(reason)
        return self._step()

    def _combat_target_present(self, context: FeatureContext) -> bool:
        if self._inner is None:
            return False
        for sensor in self._inner.sensors():
            if sensor.kind is not SensorKind.TARGET:
                continue
            try:
                value = context.require(sensor, self._MAX_AGE)
            except ObservationUnavailable:
                continue
            if isinstance(value, Target) and value.count > 0:
                return True
        return False

    def _tick_loot(self, context: FeatureContext) -> Step:
        policy = self.plan.loot
        if policy is None:
            self._looting = False
            return self._step()
        if self._loot_started is None:
            self._loot_started = context.now
        loot_budget = 2 * policy.open_timeout + 2 * policy.max_moves * policy.transfer_timeout
        if context.now - self._loot_started >= loot_budget:
            self._looting = False
            self._loot_opened = False
            self._loot_started = None
            self._loot_evidence_wait_started = None
            self._set_status("loot_time_budget_complete")
            return self._step()
        corpse = self._read(context, policy.corpse_id)
        container = self._read(context, policy.container_id)
        backpack = self._read(context, policy.backpack_id)
        capacity = self._read(context, policy.capacity_id)
        if any(item is None for item in (corpse, container, backpack, capacity)):
            return self._loot_wait(context, "waiting_for_loot_evidence")
        if backpack is None or capacity is None or not backpack[0] or not capacity[0] or capacity[2] is None or capacity[2] <= 0:
            self._looting = False
            self._loot_opened = False
            self._loot_started = None
            self._loot_evidence_wait_started = None
            self._set_status("loot_backpack_unavailable")
            return self._step()
        if not self._loot_opened:
            if container is not None and container[0]:
                self._loot_opened = True
            elif corpse is not None and corpse[0] and corpse[1] is not None:
                action = GuardedAction(
                    "loot_open", PlannedActionKind.RIGHT_CLICK,
                    confirmation=self._condition(policy.container_id, ConditionField.PRESENT, ConditionOp.EQ, True),
                    target_id=policy.corpse_id, require_present=(policy.corpse_id,), timeout=policy.open_timeout,
                    retries=1, mutating=False,
                )
                return self._dispatch_action(context, action, loot_item="__open__")
            else:
                self._looting = False
                self._loot_started = None
                self._loot_evidence_wait_started = None
                self._set_status("no_openable_corpse")
                return self._step()
        if self._loot_moves >= policy.max_moves:
            self._looting = False
            self._loot_opened = False
            self._loot_started = None
            self._loot_evidence_wait_started = None
            self._set_status("loot_budget_complete")
            return self._step()
        items = [(item_id, self._read(context, item_id)) for item_id in policy.item_ids]
        if any(item is None for _, item in items):
            return self._loot_wait(context, "waiting_for_loot_item_evidence")
        self._loot_evidence_wait_started = None
        for item_id, item in items:
            assert item is not None
            if item[0] and item[1] is not None and (item[2] is None or item[2] > 0):
                if item[2] is None:
                    return self._manual("loot_source_count_unavailable")
                confirmation = self._condition(item_id, ConditionField.COUNT, ConditionOp.DECREASED)
                action = GuardedAction(
                    "loot_transfer", PlannedActionKind.DRAG, confirmation,
                    target_id=item_id, destination_id=policy.backpack_id,
                    require_present=(item_id, policy.backpack_id, policy.capacity_id),
                    timeout=policy.transfer_timeout, retries=1, mutating=True,
                )
                destination_count_id = policy.destination_count_id
                extra = ((destination_count_id, ConditionField.COUNT, ConditionOp.INCREASED),)
                return self._dispatch_action(
                    context,
                    action,
                    extra=extra,
                    loot_item=item_id,
                    matched_count_delta=(item_id, destination_count_id),
                )
        self._looting = False
        self._loot_opened = False
        self._loot_started = None
        self._loot_evidence_wait_started = None
        self._set_status("verified_loot_complete")
        return self._step()

    @staticmethod
    def _condition(sensor_id: str, field: ConditionField, op: ConditionOp, value: Any = None):
        from tibia_client.hunt_plan import ObservationCondition
        return ObservationCondition(sensor_id, field, op, value)

    def _tick_hunt(self, context: FeatureContext) -> Step:
        supplies_known = self._supply_observations(context)
        if not supplies_known and not self._return_requested:
            self._set_status("waiting_for_supply_counts")
            return self._step()
        due, reason = self._return_due()
        if due:
            self._return_requested = True
        if due and self._return_mode == "now" and self._looting:
            self._looting = False
            self._loot_opened = False
            self._loot_started = None
            self._loot_evidence_wait_started = None
            self._set_status("immediate_return_skipped_loot")
        if self._looting and self._combat_target_present(context):
            self._looting = False
            self._loot_opened = False
            self._loot_started = None
            self._loot_evidence_wait_started = None
            self._set_status("loot_preempted_by_combat")
        if self._looting:
            result = self._tick_loot(context)
            return result
        if self._inner is None:
            self._inner = self._new_inner(CyclePhase.HUNT)
            self._set_status("hunt_starting")
            return self._step()
        if due:
            if self._return_mode == "now":
                self._inner.request_abort()
            else:
                self._inner.request_exit()
            self._set_status(reason)
        before_target = self._inner.status.target
        step = self._inner.tick(context)
        after = self._inner.status
        self._combat_seen = self._combat_seen or before_target is not None or after.target is not None
        if self._combat_seen and after.reason == "combat_clear" and self.plan.loot is not None:
            self._combat_seen = False
            self._looting = True
            self._loot_opened = False
            self._loot_moves = 0
            self._loot_started = context.now
            self._loot_evidence_wait_started = None
            self._set_status("combat_complete_loot_pending")
            return Step((), step.cancel_keys, self._status.phase)
        self._set_status(after.reason)
        if after.failed:
            return self._fail("hunt_failed", after.error or "hunt route failed")
        if after.finished and self._return_requested:
            self._enter(CyclePhase.RETURN_BEFORE, "hunt_exit_complete")
            return self._step(cancel_keys=step.cancel_keys)
        return Step(step.requests, step.cancel_keys, self._status.phase, False, step.error)

    def _restore_pending_action(
        self, context: FeatureContext, data: Mapping[str, Any]
    ) -> _PendingAction | None:
        loot_item = data.get("loot_item")
        extra: tuple[tuple[str, ConditionField, ConditionOp], ...] = ()
        matched_count_delta: tuple[str, str] | None = None
        if loot_item is None:
            action = self._find_action(data.get("action_id"))
        else:
            policy = self.plan.loot
            if policy is None:
                return None
            if loot_item == "__open__":
                action = GuardedAction(
                    "loot_open",
                    PlannedActionKind.RIGHT_CLICK,
                    confirmation=self._condition(
                        policy.container_id,
                        ConditionField.PRESENT,
                        ConditionOp.EQ,
                        True,
                    ),
                    target_id=policy.corpse_id,
                    require_present=(policy.corpse_id,),
                    timeout=policy.open_timeout,
                    retries=1,
                    mutating=False,
                )
            elif loot_item in policy.item_ids:
                action = GuardedAction(
                    "loot_transfer",
                    PlannedActionKind.DRAG,
                    self._condition(
                        loot_item, ConditionField.COUNT, ConditionOp.DECREASED
                    ),
                    target_id=loot_item,
                    destination_id=policy.backpack_id,
                    require_present=(
                        loot_item,
                        policy.backpack_id,
                        policy.capacity_id,
                    ),
                    timeout=policy.transfer_timeout,
                    retries=1,
                    mutating=True,
                )
                extra = ((
                    policy.destination_count_id,
                    ConditionField.COUNT,
                    ConditionOp.INCREASED,
                ),)
                matched_count_delta = (loot_item, policy.destination_count_id)
            else:
                return None
        baselines = data.get("baselines")
        if action is None or not isinstance(baselines, Mapping):
            return None
        return _PendingAction(
            action=action,
            baselines=dict(baselines),
            baseline_frame=context.snapshot.frame_id,
            attempts=data["attempts"],
            issued_at=context.now,
            request_key=data["request_key"],
            dispatched_at=data.get("dispatched_at"),
            deadline=None,
            extra=extra,
            loot_item=loot_item,
            matched_count_delta=matched_count_delta,
        )

    def _tick_resume(self, context: FeatureContext) -> Step | None:
        if self._resume_pending is not None:
            data = self._resume_pending
            pending = self._restore_pending_action(context, data)
            if pending is None:
                self._resume_pending = None
                return self._manual("interrupted_action_requires_reconciliation")
            # The restored baseline frame is this first post-restart frame. Check
            # it directly once; later manual re-checks require a newer frame.
            pending.baseline_frame = -1
            confirmed = self._pending_confirmed(context, pending)
            pending.baseline_frame = context.snapshot.frame_id
            if confirmed is True:
                self._resume_pending = None
                self._complete_pending_effect(pending)
                self._set_status("machine_confirmed_after_resume")
                self._save_checkpoint()
                return self._step()
            self._resume_pending = None
            self._pending = pending
            reason = (
                "interrupted_loot_transfer_requires_reconciliation"
                if pending.loot_item is not None
                else "interrupted_mutation_ambiguous"
            )
            return self._manual(reason)
        recovery = self._recovery
        if recovery is None:
            return None
        if context.snapshot.frame_id <= recovery.baseline_frame:
            recovery.reason = "resume_waiting_for_new_frame"
            self._set_status(recovery.reason)
            return self._step()
        if not self._supply_observations(context):
            recovery.reason = "resume_waiting_for_fresh_observations"
            self._set_status(recovery.reason)
            return self._step()
        if recovery.origin is RecoveryOrigin.WARM_PAUSE:
            if self._inner is not None:
                for sensor in self._inner.sensors():
                    if sensor.kind not in (SensorKind.MARKER, SensorKind.TARGET):
                        continue
                    try:
                        context.require(sensor, self._MAX_AGE)
                    except ObservationUnavailable:
                        recovery.reason = "resume_waiting_for_fresh_route_evidence"
                        self._set_status(recovery.reason)
                        return self._step()
            self._recovery = None
            self._set_status("resume_observations_reconciled")
            return self._step()
        if self._phase in (CyclePhase.OUTBOUND, CyclePhase.HUNT, CyclePhase.RETURN):
            if (
                self._phase is CyclePhase.HUNT
                and self._return_requested
                and self._return_mode == "now"
            ):
                self._recovery = None
                self._enter(CyclePhase.RETURN_BEFORE, "checkpoint_immediate_return_continuing")
                return self._step()
            if self._inner is None:
                self._inner = self._new_inner(self._phase)
            candidates = self._inner.recognized_anchor_candidates(context)
            recovery.candidates = candidates
            selected = recovery.selected_anchor
            if selected is not None and selected not in candidates:
                recovery.reason = "selected_route_anchor_not_currently_recognized"
                self._set_status(recovery.reason)
                return self._step()
            if selected is None and len(candidates) != 1:
                recovery.reason = (
                    "route_anchor_waiting_for_fresh_evidence"
                    if not candidates
                    else "route_anchor_ambiguous_select_candidate"
                )
                self._set_status(recovery.reason)
                return self._step()
            anchor = selected if selected is not None else candidates[0]
            self._inner.resume_from_recognized_anchor(context, waypoint_index=anchor)
        self._recovery = None
        self._set_status("resume_observations_reconciled")
        return self._step()

    def tick(self, context: FeatureContext) -> Step:
        self._last_frame_id = max(self._last_frame_id, context.snapshot.frame_id)
        if self._paused or self._stopped:
            return self._step()
        if self._phase is CyclePhase.MANUAL:
            if self._manual_check_requested:
                self._manual_check_requested = False
                if self._pending is None:
                    self._set_status("manual_recheck_has_no_machine_reconcilable_effect")
                    return self._step()
                confirmed = self._pending_confirmed(context, self._pending)
                if confirmed is True:
                    origin = self._manual_origin or CyclePhase.PREPARE
                    pending = self._pending
                    self._pending = None
                    self._phase = origin
                    self._manual_origin = None
                    self._manual_reason = None
                    self._complete_pending_effect(pending)
                    self._set_status("machine_confirmed_after_manual_check")
                    self._save_checkpoint()
                else:
                    self._set_status("manual_reconciliation_still_required")
            return self._step()
        if self._phase in (CyclePhase.FINISHED, CyclePhase.FAILED):
            return self._step()
        if self._phase is CyclePhase.HUNT:
            # Supply urgency must remain live while a guarded loot effect is
            # pending.  Unknown counts never authorize a threshold, but known
            # emergency evidence can still upgrade an existing return intent.
            self._supply_observations(context)
            self._return_due()
        cancelled = self._cancel_queued_loot_for_immediate_return()
        if cancelled is not None:
            return cancelled
        resumed = self._tick_resume(context)
        if resumed is not None:
            return resumed
        if self._pending is not None:
            return self._tick_pending(context)
        if self._phase in (CyclePhase.OUTBOUND, CyclePhase.RETURN):
            return self._tick_route(context)
        if self._phase is CyclePhase.HUNT:
            return self._tick_hunt(context)
        return self._tick_action_phase(context)

    def _cancel_queued_loot_for_immediate_return(self) -> Step | None:
        pending = self._pending
        if not (
            self._phase is CyclePhase.HUNT
            and self._return_requested
            and self._return_mode == "now"
            and pending is not None
            and pending.loot_item is not None
            and pending.dispatched_at is None
            and not pending.dispatch_unknown
        ):
            return None
        self._pending = None
        self._clear_loot_state()
        self._set_status("immediate_return_cancelled_queued_loot")
        self._save_checkpoint()
        return self._step(cancel_keys=(pending.request_key,))

    def _clear_loot_state(self) -> None:
        self._looting = False
        self._loot_opened = False
        self._loot_started = None
        self._loot_evidence_wait_started = None

    def on_result(self, result: ActionResult) -> None:
        if self._pending is not None and result.key == self._pending.request_key:
            if result.state in (ResultState.REJECTED, ResultState.CANCELLED):
                # The session guarantees these states were not dispatched.  A
                # later tick must still gather a new baseline before retrying.
                self._pending = None
                self._set_status(f"action_{result.state.value}")
                self._save_checkpoint()
                return
            if result.state in (ResultState.EXECUTED, ResultState.CONFIRMED, ResultState.FAILED):
                if self._pending.dispatched_at is None:
                    self._pending.dispatched_at = result.at
                    self._pending.deadline = result.at + self._pending.action.timeout
                self._set_status("reconciling_after_dispatch")
                self._save_checkpoint()
                return
        if self._inner is not None:
            self._inner.on_result(result)

    def _find_action(self, action_id: object) -> GuardedAction | None:
        if not isinstance(action_id, str):
            return None
        return next((item for group in self._all_action_groups() for item in group if item.id == action_id), None)

    def _checkpoint_data(self) -> dict[str, Any]:
        pending = None
        if self._pending is not None:
            pending = {
                "action_id": self._pending.action.id,
                "mutating": self._pending.action.mutating,
                "baselines": self._pending.baselines,
                "attempts": self._pending.attempts,
                "baseline_frame": self._pending.baseline_frame,
                "request_key": self._pending.request_key,
                "dispatched_at": self._pending.dispatched_at,
                "loot_item": self._pending.loot_item,
            }
        phase = self._manual_origin.value if self._phase is CyclePhase.MANUAL and self._manual_origin else self._phase.value
        return {
            "version": self._CHECKPOINT_VERSION,
            "plan_id": self.plan.id,
            "plan_fingerprint": self.plan.fingerprint,
            "binding_revision": self.binding_revision,
            "phase": phase,
            "action_index": self._action_index,
            "segment_index": self._segment_index,
            "cycle_count": self._cycle_count,
            "refill_round": self._refill_round,
            "return_requested": self._return_requested,
            "return_mode": self._return_mode,
            "pending": pending,
        }

    def _save_checkpoint(self) -> None:
        path = self._checkpoint_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self._checkpoint_data(), stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _load_checkpoint(self) -> None:
        path = self._checkpoint_path
        if path is None or not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
            if not isinstance(data, Mapping):
                raise HuntCheckpointError("checkpoint must be an object")
            expected_keys = {
                "version", "plan_id", "plan_fingerprint", "binding_revision",
                "phase", "action_index", "segment_index", "cycle_count",
                "refill_round", "return_requested", "return_mode", "pending",
            }
            if set(data) != expected_keys:
                raise HuntCheckpointError("checkpoint fields are invalid")
            if data.get("version") != self._CHECKPOINT_VERSION:
                raise HuntCheckpointError(
                    "unsupported checkpoint version; discard it or start fresh with resume=False"
                )
            if data.get("plan_id") != self.plan.id or data.get("plan_fingerprint") != self.plan.fingerprint:
                raise HuntCheckpointError(
                    "checkpoint belongs to a changed hunt plan; review the route change, then discard it or start fresh with resume=False"
                )
            if data.get("binding_revision") != self.binding_revision:
                raise HuntCheckpointError(
                    "checkpoint belongs to a changed visual binding revision; recalibrate/review, then discard it or start fresh with resume=False"
                )
            phase = CyclePhase(data.get("phase"))
            if phase in (CyclePhase.MANUAL, CyclePhase.FINISHED, CyclePhase.FAILED):
                raise HuntCheckpointError("checkpoint contains a non-resumable phase")
            action_index = data.get("action_index")
            segment_index = data.get("segment_index", 0)
            cycle_count = data.get("cycle_count")
            refill_round = data.get("refill_round")
            if any(type(item) is not int or item < 0 for item in (action_index, segment_index, cycle_count, refill_round)):
                raise HuntCheckpointError("checkpoint counters are invalid")
            if phase in (CyclePhase.RETURN_BEFORE, CyclePhase.RETURN, CyclePhase.RETURN_AFTER):
                segment_limit = len(self.plan.return_segments)
            elif phase in (CyclePhase.OUTBOUND_BEFORE, CyclePhase.OUTBOUND, CyclePhase.OUTBOUND_AFTER):
                segment_limit = len(self.plan.outbound)
            else:
                segment_limit = 1
            if segment_index >= segment_limit:
                raise HuntCheckpointError("checkpoint travel segment no longer exists")
            actions = self._actions_for(phase, segment_index)
            if action_index > len(actions):
                raise HuntCheckpointError("checkpoint action index is outside the current phase")
            if refill_round > self.plan.max_refill_rounds:
                raise HuntCheckpointError("checkpoint refill round exceeds the plan limit")
            return_requested = data.get("return_requested")
            mode = data.get("return_mode")
            if type(return_requested) is not bool:
                raise HuntCheckpointError("checkpoint return_requested must be boolean")
            if mode not in (None, "now", "after_fight"):
                raise HuntCheckpointError("checkpoint return mode is invalid")
            if return_requested != (mode is not None):
                raise HuntCheckpointError("checkpoint return request and mode disagree")
            pending = data.get("pending")
            checked_pending: dict[str, Any] | None = None
            if pending is not None:
                if not isinstance(pending, Mapping):
                    raise HuntCheckpointError("checkpoint pending action must be an object")
                pending_keys = {
                    "action_id", "mutating", "baselines", "attempts",
                    "baseline_frame", "request_key", "dispatched_at", "loot_item",
                }
                if set(pending) != pending_keys:
                    raise HuntCheckpointError("checkpoint pending action fields are invalid")
                action_id = pending.get("action_id")
                mutating = pending.get("mutating")
                baselines = pending.get("baselines")
                attempts = pending.get("attempts")
                baseline_frame = pending.get("baseline_frame")
                request_key = pending.get("request_key")
                dispatched_at = pending.get("dispatched_at")
                loot_item = pending.get("loot_item")
                if type(mutating) is not bool:
                    raise HuntCheckpointError("checkpoint pending mutation flag is invalid")
                if (
                    not isinstance(baselines, Mapping)
                    or not baselines
                    or any(
                        not isinstance(key, str)
                        or (type(value) is not bool and type(value) is not int)
                        for key, value in baselines.items()
                    )
                ):
                    raise HuntCheckpointError("checkpoint pending baselines are invalid")
                if type(attempts) is not int or attempts < 1:
                    raise HuntCheckpointError("checkpoint pending attempts are invalid")
                if type(baseline_frame) is not int or baseline_frame < 0:
                    raise HuntCheckpointError("checkpoint pending frame is invalid")
                if not isinstance(request_key, str) or not request_key.strip():
                    raise HuntCheckpointError("checkpoint pending request key is invalid")
                if dispatched_at is not None and (
                    isinstance(dispatched_at, bool)
                    or not isinstance(dispatched_at, (int, float))
                    or not math.isfinite(dispatched_at)
                    or dispatched_at < 0
                ):
                    raise HuntCheckpointError("checkpoint pending dispatch time is invalid")
                current = actions[action_index] if action_index < len(actions) else None
                configured = (
                    loot_item is None
                    and current is not None
                    and action_id == current.id
                    and mutating is current.mutating
                    and current.confirmation.sensor_id in baselines
                )
                expected_loot_mutating = action_id == "loot_transfer"
                loot_pending = (
                    phase is CyclePhase.HUNT
                    and isinstance(loot_item, str)
                    and loot_item in ("__open__", *self.plan.loot.item_ids)
                    and action_id in ("loot_open", "loot_transfer")
                    and mutating is expected_loot_mutating
                    if self.plan.loot is not None
                    else False
                )
                if not configured and not loot_pending:
                    raise HuntCheckpointError(
                        "checkpoint pending action does not match the current phase"
                    )
                checked_pending = dict(pending)
            self._phase = phase
            self._action_index = action_index
            self._segment_index = segment_index
            self._cycle_count = cycle_count
            self._refill_round = refill_round
            self._return_requested = return_requested
            self._return_mode = mode
            self._resume_pending = checked_pending
            self._recovery = RecoveryState(
                RecoveryOrigin.CHECKPOINT,
                -1,
                "checkpoint_fresh_observation_required",
            )
            self._set_status("checkpoint_loaded_fresh_observation_required")
        except HuntCheckpointError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HuntCheckpointError(f"invalid checkpoint {path}: {exc}") from exc


__all__ = [
    "CyclePhase",
    "HuntCheckpointError",
    "HuntCycleFeature",
    "HuntCycleStatus",
    "RecoveryOrigin",
    "RecoveryState",
]
