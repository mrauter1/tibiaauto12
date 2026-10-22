from __future__ import annotations

import ast
from pathlib import Path

import pytest

from engine.support import Threshold, TimedSpell
from features.support import (
    EquipmentFeature,
    FoodFeature,
    HasteFeature,
    HealFeature,
    ManaFeature,
    TimedSpellsFeature,
)
from tibia_client.models import (
    ActionKind,
    ActionResult,
    FeatureContext,
    Observation,
    ObservationUnavailable,
    Priority,
    Quality,
    ResultState,
    Sensor,
    SensorKind,
    Snapshot,
)


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def context(self, *values: tuple[Sensor, object, Quality]) -> FeatureContext:
        observations = {
            sensor: Observation(
                sensor,
                value,
                self.now,
                frame_id=1,
                epoch=1,
                quality=quality,
                detail="scanner could not read configured region" if quality is not Quality.KNOWN else "",
            )
            for sensor, value, quality in values
        }
        return FeatureContext(
            Snapshot(observations, frame_id=1, epoch=1, created_at=self.now),
            self.now,
        )


def known(sensor: Sensor, value: object) -> tuple[Sensor, object, Quality]:
    return sensor, value, Quality.KNOWN


def result(step, state: ResultState, at: float, *, final: bool = True) -> ActionResult:
    request = step.requests[0]
    return ActionResult("request-1", "feature", request.key, state, at, final=final)


def test_feature_modules_never_sleep_or_execute_client_actions_directly():
    feature_dir = Path(__file__).parents[1] / "features"
    violations = []
    for path in sorted(feature_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else ""
            )
            if called in {"execute", "sleep"}:
                violations.append(f"{path.name}:{node.lineno} calls {called}()")

    assert violations == []


def test_heal_uses_most_urgent_threshold_and_heal_priority():
    health = Sensor(SensorKind.HEALTH, (10, 20), "life")
    feature = HealFeature(
        health,
        (
            Threshold(True, 80, "F1"),
            Threshold(True, 40, "F3"),
            Threshold(True, 60, "F2"),
        ),
    )
    clock = Clock(10)

    step = feature.tick(clock.context(known(health, 35)))

    assert feature.name == "AutoHeal"
    assert feature.sensors() == (health,)
    assert len(step.requests) == 1
    request = step.requests[0]
    assert request.action.kind is ActionKind.PRESS
    assert request.action.key == "F3"
    assert request.priority == Priority.HEAL
    assert request.ttl <= 0.5
    assert tuple(item.sensor for item in request.requirements) == (health,)


def test_mana_waits_for_result_and_starts_cooldown_only_after_execution():
    mana = Sensor(SensorKind.MANA, (1, 2), "mana")
    feature = ManaFeature(mana, (Threshold(True, 50, "F4"),))
    clock = Clock(1)

    first = feature.tick(clock.context(known(mana, 20)))
    assert first.requests[0].priority == Priority.SUPPORT
    clock.now = 20
    assert feature.tick(clock.context(known(mana, 20))).requests == ()

    feature.on_result(result(first, ResultState.REJECTED, clock.now))
    retry = feature.tick(clock.context(known(mana, 20)))
    assert len(retry.requests) == 1
    feature.on_result(result(retry, ResultState.EXECUTED, clock.now))

    clock.now = 20.099
    assert feature.tick(clock.context(known(mana, 20))).requests == ()
    clock.now = 20.1
    assert len(feature.tick(clock.context(known(mana, 20))).requests) == 1


@pytest.mark.parametrize("state", [ResultState.CANCELLED, ResultState.FAILED])
def test_cancelled_and_failed_requests_can_retry_immediately(state):
    hungry = Sensor(SensorKind.HUNGRY, (3, 4), "food")
    feature = FoodFeature(hungry, "F8")
    clock = Clock(3)
    first = feature.tick(clock.context(known(hungry, True)))

    feature.on_result(result(first, state, clock.now))

    assert len(feature.tick(clock.context(known(hungry, True))).requests) == 1


def test_executed_request_awaiting_confirmation_remains_pending_then_cools_down():
    hasted = Sensor(SensorKind.HASTED, (5, 6), "haste")
    feature = HasteFeature(hasted, "F6")
    clock = Clock(8)
    first = feature.tick(clock.context(known(hasted, False)))

    feature.on_result(result(first, ResultState.EXECUTED, 8, final=False))
    clock.now = 9
    assert feature.tick(clock.context(known(hasted, False))).requests == ()
    feature.on_result(result(first, ResultState.CONFIRMED, 9))

    # Cooldown is measured from execution, so it has already elapsed while the
    # runtime waited for confirmation.
    assert len(feature.tick(clock.context(known(hasted, False))).requests) == 1


def test_status_features_only_trigger_on_the_explicit_status_value():
    hasted = Sensor(SensorKind.HASTED)
    hungry = Sensor(SensorKind.HUNGRY)
    clock = Clock()

    assert HasteFeature(hasted, "F6").tick(clock.context(known(hasted, True))).requests == ()
    food = FoodFeature(hungry, "F7").tick(clock.context(known(hungry, True)))
    assert food.requests[0].action.key == "F7"
    assert food.requests[0].priority == Priority.SUPPORT


def test_unknown_and_invalid_observations_fail_closed_with_actionable_errors():
    health = Sensor(SensorKind.HEALTH, (100, 200), "life")
    feature = HealFeature(health, (Threshold(True, 50, "F1"),))
    clock = Clock(4)

    with pytest.raises(ObservationUnavailable, match="AutoHeal.*check.*coordinates"):
        feature.tick(clock.context((health, None, Quality.UNKNOWN)))
    with pytest.raises(ObservationUnavailable, match="expected a whole percentage"):
        feature.tick(clock.context(known(health, "40")))


def test_equipment_hotkey_requires_empty_slot_and_optional_health_evidence():
    empty = Sensor(SensorKind.RING_EMPTY, (10, 10, 32, 32), "ring")
    health = Sensor(SensorKind.HEALTH, (1, 2), "life")
    feature = EquipmentFeature(
        "AutoRing",
        empty,
        (26, 26),
        hotkey="F12",
        health_sensor=health,
        health_threshold=30,
    )
    clock = Clock(5)

    assert feature.tick(clock.context(known(empty, False), known(health, 1))).requests == ()
    assert feature.tick(clock.context(known(empty, True), known(health, 31))).requests == ()
    step = feature.tick(clock.context(known(empty, True), known(health, 30)))

    request = step.requests[0]
    assert request.action.kind is ActionKind.PRESS
    assert request.action.key == "F12"
    assert request.priority == Priority.SUPPORT
    assert tuple(item.sensor for item in request.requirements) == (empty, health)


def test_equipment_drag_validates_points_and_targets_slot_center():
    empty = Sensor(SensorKind.AMULET_EMPTY)
    feature = EquipmentFeature("AutoSSA", empty, (80, 90), source=(20, 30))
    clock = Clock()

    request = feature.tick(clock.context(known(empty, True))).requests[0]
    assert request.action.kind is ActionKind.DRAG
    assert request.action.point == (20, 30)
    assert request.action.destination == (80, 90)
    with pytest.raises(ValueError, match="exactly one"):
        EquipmentFeature("AutoSSA", empty, (1, 1), hotkey="F1", source=(2, 2))
    with pytest.raises(ValueError, match="non-negative"):
        EquipmentFeature("AutoSSA", empty, (-1, 1), hotkey="F1")


def test_timed_spells_arm_on_first_tick_and_do_not_catch_up_missed_cycles():
    feature = TimedSpellsFeature((TimedSpell("F1", 2),))
    clock = Clock(100)

    assert feature.tick(clock.context()).requests == ()
    clock.now = 101.99
    assert feature.tick(clock.context()).requests == ()
    clock.now = 102
    first = feature.tick(clock.context())
    assert first.requests[0].action.key == "F1"
    assert first.requests[0].priority == Priority.BACKGROUND

    # Execution is late by several periods. Only one request is emitted and
    # the next deadline advances beyond the execution time.
    feature.on_result(result(first, ResultState.EXECUTED, 110))
    clock.now = 110.15
    assert feature.tick(clock.context()).requests == ()
    clock.now = 112
    assert len(feature.tick(clock.context()).requests) == 1


def test_immediate_timed_spells_are_serialized_with_spacing_and_retry_rejection():
    feature = TimedSpellsFeature(
        (TimedSpell("F1", 10), TimedSpell("F2", 10)),
        immediate=True,
        spacing=0.2,
    )
    clock = Clock(50)
    first = feature.tick(clock.context())
    assert first.requests[0].action.key == "F1"

    feature.on_result(result(first, ResultState.REJECTED, 50))
    retry = feature.tick(clock.context())
    assert retry.requests[0].action.key == "F1"
    feature.on_result(result(retry, ResultState.EXECUTED, 50))

    clock.now = 50.19
    assert feature.tick(clock.context()).requests == ()
    clock.now = 50.2
    second = feature.tick(clock.context())
    assert second.requests[0].action.key == "F2"
