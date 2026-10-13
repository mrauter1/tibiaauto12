"""Support automation expressed entirely in the public client SDK.

The classes in this module decide what should happen.  Observation and input
remain responsibilities of the runtime and its client adapter.
"""

from __future__ import annotations

import math
from typing import Sequence

from engine.support import EquipmentPolicy, Threshold, TimedSpell, choose_threshold
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
)


_MAX_AGE = 0.5
_TTL = 0.5


def _hotkey(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("hotkey must be non-empty text")
    return value


def _press_operations(hotkeys: Sequence[str]) -> tuple[str, ...]:
    operations: list[str] = []
    for hotkey in hotkeys:
        operation = "modified_press" if "+" in hotkey else "press"
        if operation not in operations:
            operations.append(operation)
    return tuple(operations)


def _point(value: tuple[int, int], label: str) -> tuple[int, int]:
    try:
        point = tuple(value)
    except TypeError:
        raise ValueError(f"{label} must be an X/Y pair") from None
    if len(point) != 2 or any(type(part) is not int or part < 0 for part in point):
        raise ValueError(f"{label} must be a non-negative integer X/Y pair")
    return point


def _sensor(value: Sensor, kind: SensorKind, label: str) -> Sensor:
    if not isinstance(value, Sensor):
        raise TypeError(f"{label} must be a Sensor")
    if value.kind is not kind:
        raise ValueError(f"{label} must use SensorKind.{kind.name}")
    return value


def _percentage(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= 100:
        raise ObservationUnavailable(
            f"{label} sensor returned {value!r}; expected a whole percentage from 0 to 100"
        )
    return value


def _flag(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ObservationUnavailable(
            f"{label} sensor returned {value!r}; expected true or false"
        )
    return value


class _ActionFeature:
    """Small shared state machine for one outstanding request and cooldown."""

    name: str
    interval: float

    def __init__(self, *, cooldown: float, request_key: str) -> None:
        self._cooldown = float(cooldown)
        self._request_key = request_key
        self._pending = False
        self._executed_at: float | None = None
        self._last_action_at: float | None = None

    def _ready(self, now: float) -> bool:
        return (
            not self._pending
            and (
                self._last_action_at is None
                or now - self._last_action_at >= self._cooldown
            )
        )

    def _request(
        self,
        action: Action,
        *,
        priority: Priority,
        requirements: tuple[Requirement, ...] = (),
        reason: str,
    ) -> Step:
        self._pending = True
        self._executed_at = None
        return Step(
            requests=(
                Request(
                    key=self._request_key,
                    action=action,
                    priority=priority,
                    ttl=_TTL,
                    requirements=requirements,
                    reason=reason,
                ),
            )
        )

    def on_result(self, result: ActionResult) -> None:
        if result.key != self._request_key or not self._pending:
            return
        if result.state is ResultState.EXECUTED:
            if result.final:
                self._complete(result.at)
            else:
                self._executed_at = result.at
            return
        if result.state is ResultState.CONFIRMED:
            self._complete(self._executed_at if self._executed_at is not None else result.at)
            return
        if result.state in (
            ResultState.REJECTED,
            ResultState.CANCELLED,
            ResultState.FAILED,
        ):
            self._pending = False
            self._executed_at = None

    def _complete(self, at: float) -> None:
        self._last_action_at = at
        self._pending = False
        self._executed_at = None
        self._on_executed(at)

    def _on_executed(self, at: float) -> None:
        del at

    def _read(self, context: FeatureContext, sensor: Sensor, label: str) -> object:
        try:
            return context.require(sensor, _MAX_AGE)
        except ObservationUnavailable as exc:
            raise ObservationUnavailable(
                f"{self.name} cannot act: {label} observation unavailable; "
                f"check its configured coordinates ({exc})"
            ) from None


class _ThresholdFeature(_ActionFeature):
    sensor_kind: SensorKind
    priority: Priority

    def __init__(self, sensor: Sensor, rules: Sequence[Threshold], *, cooldown: float) -> None:
        super().__init__(cooldown=cooldown, request_key=f"{self.name}.action")
        self.sensor = _sensor(sensor, self.sensor_kind, self.sensor_kind.value)
        self.rules = tuple(rules)
        if not self.rules:
            raise ValueError("add at least one threshold")
        if any(not isinstance(rule, Threshold) for rule in self.rules):
            raise TypeError("rules must contain Threshold values")
        if not any(rule.enabled for rule in self.rules):
            raise ValueError("enable at least one threshold")

    def sensors(self) -> tuple[Sensor, ...]:
        return (self.sensor,)

    def required_operations(self) -> tuple[str, ...]:
        return _press_operations(tuple(rule.hotkey for rule in self.rules if rule.enabled))

    def tick(self, context: FeatureContext) -> Step:
        value = _percentage(self._read(context, self.sensor, self.sensor_kind.value), self.name)
        rule = choose_threshold(value, self.rules)
        if rule is None or not self._ready(context.now):
            return Step()
        return self._request(
            Action(ActionKind.PRESS, key=rule.hotkey),
            priority=self.priority,
            requirements=(Requirement(self.sensor, _MAX_AGE),),
            reason=f"{self.sensor_kind.value} is {value}% (threshold {rule.percent}%)",
        )


class HealFeature(_ThresholdFeature):
    name = "AutoHeal"
    interval = 0.05
    sensor_kind = SensorKind.HEALTH
    priority = Priority.HEAL

    def __init__(self, sensor: Sensor, rules: Sequence[Threshold]) -> None:
        super().__init__(sensor, rules, cooldown=0.15)


class ManaFeature(_ThresholdFeature):
    name = "AutoMana"
    interval = 0.05
    sensor_kind = SensorKind.MANA
    priority = Priority.SUPPORT

    def __init__(self, sensor: Sensor, rules: Sequence[Threshold]) -> None:
        super().__init__(sensor, rules, cooldown=0.10)


class _StatusFeature(_ActionFeature):
    sensor_kind: SensorKind
    trigger_value: bool

    def __init__(self, sensor: Sensor, hotkey: str, *, cooldown: float) -> None:
        super().__init__(cooldown=cooldown, request_key=f"{self.name}.action")
        self.sensor = _sensor(sensor, self.sensor_kind, self.sensor_kind.value)
        self.hotkey = _hotkey(hotkey)

    def sensors(self) -> tuple[Sensor, ...]:
        return (self.sensor,)

    def required_operations(self) -> tuple[str, ...]:
        return _press_operations((self.hotkey,))

    def tick(self, context: FeatureContext) -> Step:
        value = _flag(self._read(context, self.sensor, self.sensor_kind.value), self.name)
        if value is not self.trigger_value or not self._ready(context.now):
            return Step()
        return self._request(
            Action(ActionKind.PRESS, key=self.hotkey),
            priority=Priority.SUPPORT,
            requirements=(Requirement(self.sensor, _MAX_AGE),),
            reason=(
                "haste status is absent"
                if self.sensor_kind is SensorKind.HASTED
                else "character is hungry"
            ),
        )


class HasteFeature(_StatusFeature):
    name = "AutoHur"
    interval = 0.2
    sensor_kind = SensorKind.HASTED
    trigger_value = False

    def __init__(self, sensor: Sensor, hotkey: str) -> None:
        super().__init__(sensor, hotkey, cooldown=0.6)


class FoodFeature(_StatusFeature):
    name = "FoodEater"
    interval = 0.2
    sensor_kind = SensorKind.HUNGRY
    trigger_value = True

    def __init__(self, sensor: Sensor, hotkey: str) -> None:
        super().__init__(sensor, hotkey, cooldown=0.5)


class EquipmentFeature(_ActionFeature):
    """Equip a ring or amulet when its slot is explicitly observed empty."""

    interval = 0.1

    def __init__(
        self,
        name: str,
        empty_sensor: Sensor,
        slot_center: tuple[int, int],
        *,
        hotkey: str | None = None,
        source: tuple[int, int] | None = None,
        health_sensor: Sensor | None = None,
        health_threshold: int | None = None,
    ) -> None:
        if name not in ("AutoRing", "AutoSSA"):
            raise ValueError("equipment feature name must be AutoRing or AutoSSA")
        expected_kind = (
            SensorKind.RING_EMPTY if name == "AutoRing" else SensorKind.AMULET_EMPTY
        )
        self.name = name
        super().__init__(cooldown=0.5, request_key=f"{name}.action")
        self.empty_sensor = _sensor(empty_sensor, expected_kind, "empty-slot sensor")
        self.slot_center = _point(slot_center, "slot center")
        if (hotkey is None) == (source is None):
            raise ValueError("configure exactly one of hotkey or drag source")
        self.hotkey = _hotkey(hotkey) if hotkey is not None else None
        self.source = _point(source, "drag source") if source is not None else None

        conditional = health_sensor is not None or health_threshold is not None
        if conditional and (health_sensor is None or health_threshold is None):
            raise ValueError("health sensor and threshold must be configured together")
        if health_sensor is not None:
            self.health_sensor = _sensor(health_sensor, SensorKind.HEALTH, "health sensor")
            if type(health_threshold) is not int:
                raise ValueError("health threshold must be a whole percentage")
            self.policy = EquipmentPolicy(True, health_threshold)
        else:
            self.health_sensor = None
            self.policy = EquipmentPolicy(False)

    def sensors(self) -> tuple[Sensor, ...]:
        if self.health_sensor is None:
            return (self.empty_sensor,)
        return (self.empty_sensor, self.health_sensor)

    def required_operations(self) -> tuple[str, ...]:
        if self.hotkey is not None:
            return _press_operations((self.hotkey,))
        return ("drag",)

    def tick(self, context: FeatureContext) -> Step:
        empty = _flag(
            self._read(context, self.empty_sensor, "equipment slot"),
            f"{self.name} empty-slot",
        )
        life: int | None = None
        if self.health_sensor is not None:
            life = _percentage(
                self._read(context, self.health_sensor, "health"),
                f"{self.name} health",
            )
        if not self.policy.should_equip(empty, life) or not self._ready(context.now):
            return Step()

        requirements = [Requirement(self.empty_sensor, _MAX_AGE)]
        if self.health_sensor is not None:
            requirements.append(Requirement(self.health_sensor, _MAX_AGE))
        if self.hotkey is not None:
            action = Action(ActionKind.PRESS, key=self.hotkey)
        else:
            action = Action(
                ActionKind.DRAG,
                point=self.source,
                destination=self.slot_center,
            )
        condition = "empty slot"
        if life is not None:
            condition += f" and health {life}% <= {self.policy.threshold}%"
        return self._request(
            action,
            priority=Priority.SUPPORT,
            requirements=tuple(requirements),
            reason=condition,
        )


class TimedSpellsFeature(_ActionFeature):
    """Deadline-based timed spells, armed by the first runtime tick."""

    name = "TimedSpells"
    interval = 0.05

    def __init__(
        self,
        spells: Sequence[TimedSpell],
        *,
        immediate: bool = False,
        spacing: float = 0.15,
    ) -> None:
        super().__init__(cooldown=0.0, request_key="TimedSpells.action")
        self.spells = tuple(spells)
        if not self.spells:
            raise ValueError("add at least one timed spell")
        if any(not isinstance(spell, TimedSpell) for spell in self.spells):
            raise TypeError("spells must contain TimedSpell values")
        if not isinstance(immediate, bool):
            raise ValueError("immediate must be true or false")
        if isinstance(spacing, bool) or not isinstance(spacing, (int, float)):
            raise ValueError("spacing must be a finite non-negative number")
        if not math.isfinite(spacing) or spacing < 0:
            raise ValueError("spacing must be a finite non-negative number")
        self.immediate = immediate
        self.spacing = float(spacing)
        self._deadlines: list[float] | None = None
        self._available_at: float | None = None
        self._requested_index: int | None = None

    def sensors(self) -> tuple[Sensor, ...]:
        return ()

    def required_operations(self) -> tuple[str, ...]:
        return _press_operations(tuple(spell.hotkey for spell in self.spells))

    def tick(self, context: FeatureContext) -> Step:
        if self._deadlines is None:
            self._deadlines = [
                context.now if self.immediate else context.now + spell.interval
                for spell in self.spells
            ]
            self._available_at = context.now
        if self._pending or context.now < self._available_at:
            return Step()
        due = [index for index, deadline in enumerate(self._deadlines) if deadline <= context.now]
        if not due:
            return Step()
        index = min(due, key=lambda item: (self._deadlines[item], item))
        self._requested_index = index
        spell = self.spells[index]
        return self._request(
            Action(ActionKind.PRESS, key=spell.hotkey),
            priority=Priority.BACKGROUND,
            reason=f"timed spell is due every {spell.interval:g}s",
        )

    def _on_executed(self, at: float) -> None:
        if self._requested_index is None or self._deadlines is None:
            return
        index = self._requested_index
        deadline = self._deadlines[index]
        interval = self.spells[index].interval
        missed = max(1, math.floor((at - deadline) / interval) + 1)
        self._deadlines[index] = deadline + missed * interval
        self._available_at = at + self.spacing
        self._requested_index = None

    def on_result(self, result: ActionResult) -> None:
        retry = (
            result.key == self._request_key
            and self._pending
            and result.state
            in (ResultState.REJECTED, ResultState.CANCELLED, ResultState.FAILED)
        )
        super().on_result(result)
        if retry:
            self._requested_index = None


__all__ = [
    "EquipmentFeature",
    "FoodFeature",
    "HasteFeature",
    "HealFeature",
    "ManaFeature",
    "TimedSpellsFeature",
]
