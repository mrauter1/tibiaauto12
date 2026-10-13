from __future__ import annotations

import pytest

from engine.CaveBot.routes import Route, Waypoint
from features.cavebot import CaveBot, SessionConfig, TargetSpec
from tibia_client import (
    Action,
    ActionKind,
    ActionResult,
    ClientSession,
    FeatureContext,
    Marker,
    Observation,
    ObservationBatch,
    Priority,
    Quality,
    Reading,
    Request,
    ResultState,
    SensorKind,
    Snapshot,
    Step,
    Target,
)


def context(bot, now, *, marker=None, targets=None, follow=None, frame=1):
    values = {}
    targets = targets or {}
    for sensor in bot.sensors():
        if sensor.kind is SensorKind.MARKER:
            value = marker
        elif sensor.kind is SensorKind.TARGET:
            value = targets.get(sensor.name, Target(0))
        else:
            value = follow
        values[sensor] = Observation(sensor, value, now, frame, 1)
    return FeatureContext(Snapshot(values, frame_id=frame, epoch=1, created_at=now), now)


def result(request, state, at, *, reason=""):
    return ActionResult(
        request_id=f"id-{request.key}", owner="CaveBot", key=request.key,
        state=state, at=at, reason=reason,
    )


def bot(*, config=None, targets=(), loot_points=(), save_progress=None, route=None):
    route = route or Route("test", (Waypoint("same", "CheckMark"),))
    return CaveBot(
        route,
        targets,
        config=config or SessionConfig(),
        save_progress=save_progress,
        map_region=(1, 2, 3, 4),
        battle_region=(5, 6, 7, 8),
        loot_points=loot_points,
    )


def start_arrival(feature, *, at=0.0, frame=1):
    navigation = feature.tick(
        context(feature, at, marker=Marker((1, 1), False), frame=frame)
    ).requests[0]
    feature.on_result(result(navigation, ResultState.EXECUTED, at + 0.01))


def test_sensors_are_dynamic_sdk_values_and_known_none_marker_times_out():
    feature = bot(config=SessionConfig(marker_timeout=0.4))
    sensors = feature.sensors()
    assert [(sensor.kind, sensor.region, sensor.name) for sensor in sensors] == [
        (SensorKind.MARKER, (1, 2, 3, 4), "CheckMark"),
        (SensorKind.FOLLOW, (), ""),
    ]

    assert feature.tick(context(feature, 0.0, marker=Marker(None), frame=1)).requests == ()
    failed = feature.tick(context(feature, 0.4, marker=Marker(None), frame=2))
    assert failed.finished
    assert feature.failed
    assert feature.status.reason == "marker_timeout"
    assert "timed out" in feature.error


def test_arrival_timeout_retries_are_bounded_and_rejection_does_not_charge_retry():
    config = SessionConfig(marker_timeout=1, arrival_timeout=0.5, movement_attempts=2)
    feature = bot(config=config)

    first = feature.tick(context(feature, 0, marker=Marker((10, 20)), frame=1)).requests[0]
    feature.on_result(result(first, ResultState.REJECTED, 0.01))
    retry_after_rejection = feature.tick(
        context(feature, 0.1, marker=Marker((10, 20)), frame=2)
    ).requests[0]
    feature.on_result(result(retry_after_rejection, ResultState.EXECUTED, 0.1))
    assert feature.tick(context(feature, 0.6, marker=Marker((10, 20)), frame=3)).finished is False

    second = feature.tick(context(feature, 0.7, marker=Marker((10, 20)), frame=4)).requests[0]
    feature.on_result(result(second, ResultState.EXECUTED, 0.7))
    final = feature.tick(context(feature, 1.2, marker=Marker((10, 20)), frame=5))
    assert final.finished
    assert feature.status.reason == "arrival_timeout"


def test_pause_preserves_remaining_arrival_timeout():
    feature = bot(
        config=SessionConfig(arrival_timeout=0.5, movement_attempts=1)
    )
    start_arrival(feature, at=0.0)

    feature.on_pause()
    feature.on_resume(paused_for=10.0)

    before_deadline = feature.tick(
        context(feature, 10.50, marker=Marker((1, 1), False), frame=2)
    )
    assert not before_deadline.finished
    assert feature.status.phase == "Navigating"
    at_deadline = feature.tick(
        context(feature, 10.51, marker=Marker((1, 1), False), frame=3)
    )
    assert at_deadline.finished
    assert feature.status.reason == "arrival_timeout"


def test_invalid_pause_duration_keeps_navigation_paused_and_resumable():
    feature = bot()
    feature.tick(context(feature, 0.0, marker=Marker(None), frame=1))
    original = feature.status
    feature.on_pause()

    with pytest.raises(ValueError, match="paused_for"):
        feature.on_resume(paused_for=-1)
    assert feature.status.phase == "Paused"

    feature.on_resume(paused_for=1.0)
    assert feature.status == original


def test_pending_navigation_is_cancelled_and_low_number_target_wins():
    targets = (TargetSpec(5, "ordinary"), TargetSpec(1, "urgent"))
    feature = bot(targets=targets, config=SessionConfig(follow=False))
    navigation = feature.tick(context(feature, 0, marker=Marker((9, 9)), frame=1)).requests[0]

    combat = feature.tick(
        context(
            feature,
            0.2,
            marker=Marker((9, 9)),
            targets={
                "ordinary": Target(1, (20, 20), False),
                "urgent": Target(1, (10, 10), False),
            },
            frame=2,
        )
    )
    assert combat.cancel_keys == (navigation.key,)
    assert len(combat.requests) == 1
    assert combat.requests[0].action.point == (10, 10)
    assert combat.requests[0].priority == Priority.COMBAT
    assert combat.requests[0].requirements[0].sensor.name == "urgent"


def test_combat_can_preempt_marker_search_before_any_movement():
    feature = bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(follow=False),
    )
    combat = feature.tick(
        context(
            feature,
            0,
            marker=Marker((9, 9)),
            targets={"dragon": Target(1, (4, 5), False)},
            frame=1,
        )
    )
    assert combat.requests[0].action.point == (4, 5)
    assert combat.requests[0].key.startswith("select:")


@pytest.mark.parametrize(
    ("value", "quality", "observed_at", "detail", "expected"),
    (
        (None, Quality.ERROR, 0.0, "detector failed", "detector failed"),
        (None, Quality.UNKNOWN, 0.0, "region unavailable", "region unavailable"),
        (Target(0), Quality.KNOWN, 0.0, "", "unknown or stale observation"),
        ("zero", Quality.KNOWN, 1.0, "", "invalid type str"),
    ),
)
def test_navigation_fails_closed_on_unusable_target_evidence(
    value, quality, observed_at, detail, expected
):
    feature = bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(follow=False),
    )
    observations = {}
    for sensor in feature.sensors():
        if sensor.kind is SensorKind.MARKER:
            observation = Observation(sensor, Marker((9, 9)), 1.0, 1, 1)
        else:
            observation = Observation(
                sensor, value, observed_at, 1, 1, quality, detail
            )
        observations[sensor] = observation
    step = feature.tick(
        FeatureContext(Snapshot(observations, frame_id=1, epoch=1, created_at=1.0), 1.0)
    )

    assert step.requests == ()
    assert step.finished
    assert feature.failed
    assert feature.status.reason == "target_evidence_unavailable"
    assert "dragon" in feature.error
    assert expected in feature.error


def test_navigation_fails_closed_when_target_observation_is_missing():
    feature = bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(follow=False),
    )
    marker_sensor = feature.sensors()[0]
    marker = Observation(marker_sensor, Marker((9, 9)), 0.0, 1, 1)
    step = feature.tick(
        FeatureContext(
            Snapshot({marker_sensor: marker}, frame_id=1, epoch=1, created_at=0.0),
            0.0,
        )
    )

    assert step.requests == ()
    assert step.finished
    assert feature.status.reason == "target_evidence_unavailable"
    assert "no observation" in feature.error


def test_unreadable_high_priority_target_does_not_select_lower_priority_target():
    feature = bot(
        targets=(TargetSpec(1, "urgent"), TargetSpec(2, "ordinary")),
        config=SessionConfig(follow=False),
    )
    observations = {}
    for sensor in feature.sensors():
        if sensor.kind is SensorKind.MARKER:
            reading = Observation(sensor, Marker((9, 9)), 0.0, 1, 1)
        elif sensor.name == "urgent":
            reading = Observation(
                sensor, None, 0.0, 1, 1, Quality.ERROR, "urgent detector failed"
            )
        else:
            reading = Observation(sensor, Target(1, (20, 20)), 0.0, 1, 1)
        observations[sensor] = reading

    step = feature.tick(
        FeatureContext(Snapshot(observations, frame_id=1, epoch=1, created_at=0.0), 0.0)
    )

    assert step.requests == ()
    assert step.finished
    assert "urgent detector failed" in feature.error


def test_navigation_binds_marker_and_known_target_absence_evidence():
    feature = bot(
        targets=(TargetSpec(1, "urgent"), TargetSpec(2, "ordinary")),
        config=SessionConfig(follow=False),
    )
    navigation = feature.tick(
        context(
            feature,
            0.0,
            marker=Marker((9, 9)),
            targets={"urgent": Target(0), "ordinary": Target(0)},
        )
    ).requests[0]

    assert [requirement.sensor.kind for requirement in navigation.requirements] == [
        SensorKind.MARKER,
        SensorKind.TARGET,
        SensorKind.TARGET,
    ]
    assert [requirement.sensor.name for requirement in navigation.requirements[1:]] == [
        "urgent",
        "ordinary",
    ]


def test_selected_species_is_not_clicked_and_follow_can_be_disabled():
    feature = bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(follow=False),
    )
    assert all(sensor.kind is not SensorKind.FOLLOW for sensor in feature.sensors())
    start_arrival(feature)
    step = feature.tick(
        context(
            feature,
            0.2,
            marker=Marker((1, 1)),
            targets={"dragon": Target(1, (4, 4), True)},
            frame=2,
        )
    )
    assert step.requests == ()
    assert feature.status.target == "dragon"


def test_follow_request_uses_fresh_follow_evidence():
    feature = bot(targets=(TargetSpec(1, "dragon"),))
    start_arrival(feature)
    step = feature.tick(
        context(
            feature,
            0.2,
            marker=Marker((1, 1)),
            targets={"dragon": Target(1, (4, 4), True)},
            follow=(30, 31),
            frame=2,
        )
    )
    request = step.requests[0]
    assert request.action.kind is ActionKind.LEFT_CLICK
    assert request.action.point == (30, 31)
    assert request.requirements[0].sensor.kind is SensorKind.FOLLOW


def test_loot_disabled_never_requests_a_loot_click():
    feature = bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(follow=False, loot=False, count_stable_samples=1),
        loot_points=((1, 1),),
    )
    start_arrival(feature)
    feature.tick(
        context(
            feature, 0.2, marker=Marker((2, 2)),
            targets={"dragon": Target(1, (3, 3), True)}, frame=2,
        )
    )
    clear = feature.tick(
        context(
            feature, 0.4, marker=Marker((2, 2)),
            targets={"dragon": Target(0)}, frame=3,
        )
    )
    assert clear.requests == ()
    assert feature.status.reason == "combat_clear"


def test_loot_is_one_click_per_step_and_rejected_click_retries_same_square():
    feature = bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(follow=False, count_stable_samples=1, loot_button="right"),
        loot_points=((1, 2), (3, 4)),
    )
    start_arrival(feature)
    feature.tick(
        context(
            feature, 0.2, marker=Marker((9, 9)),
            targets={"dragon": Target(2, (5, 5), True)}, frame=2,
        )
    )
    loot = feature.tick(
        context(
            feature, 0.4, marker=Marker((9, 9)),
            targets={"dragon": Target(1, (5, 5), True)}, frame=3,
        )
    )
    assert len(loot.requests) == 1
    first = loot.requests[0]
    assert first.action.kind is ActionKind.RIGHT_CLICK
    assert first.action.point == (1, 2)
    assert first.priority == Priority.LOOT

    feature.on_result(result(first, ResultState.REJECTED, 0.41))
    retry = feature.tick(
        context(
            feature, 0.5, marker=Marker((9, 9)),
            targets={"dragon": Target(1, (5, 5), True)}, frame=4,
        )
    ).requests[0]
    assert retry.action.point == (1, 2)
    feature.on_result(result(retry, ResultState.EXECUTED, 0.51))
    second = feature.tick(
        context(
            feature, 0.6, marker=Marker((9, 9)),
            targets={"dragon": Target(1, (5, 5), True)}, frame=5,
        )
    ).requests[0]
    assert second.action.point == (3, 4)


def test_checkpoint_waits_for_observed_arrival_and_cancellable_stand():
    saved = []
    route = Route(
        "test",
        (Waypoint("first", "CheckMark"), Waypoint("second", "Flag")),
    )
    feature = bot(
        route=route,
        config=SessionConfig(stand_seconds=1.0),
        save_progress=lambda route, index: saved.append((route.fingerprint, index)),
    )
    navigation = feature.tick(
        context(feature, 0, marker=Marker((10, 10), False), frame=1)
    ).requests[0]
    feature.on_result(result(navigation, ResultState.EXECUTED, 0.01))
    feature.tick(context(feature, 0.2, marker=Marker((10, 10), True), frame=2))
    assert feature.status.phase == "Standing"
    assert saved == []
    feature.tick(context(feature, 1.19, marker=Marker((10, 10), True), frame=3))
    assert saved == []
    feature.tick(context(feature, 1.2, marker=Marker((10, 10), True), frame=4))
    assert saved == [(route.fingerprint, 1)]
    assert feature.index == 1
    assert feature.status.waypoint_id == "second"
    assert feature.sensors()[0].name == "Flag"


def test_repeated_pause_preserves_remaining_stand_time():
    saved = []
    feature = bot(
        config=SessionConfig(stand_seconds=1.0),
        save_progress=lambda _route, index: saved.append(index),
    )
    start_arrival(feature)
    feature.tick(context(feature, 0.2, marker=Marker((1, 1), True), frame=2))

    feature.on_pause()
    feature.on_resume(paused_for=5.0)
    feature.on_pause()
    feature.on_resume(paused_for=7.0)

    feature.tick(context(feature, 13.19, marker=Marker((1, 1), True), frame=3))
    assert saved == []
    feature.tick(context(feature, 13.20, marker=Marker((1, 1), True), frame=4))
    assert saved == [0]


def test_restart_begins_at_saved_index_and_wraps_checkpoint_progress():
    saved = []
    route = Route(
        "test",
        (Waypoint("first", "CheckMark"), Waypoint("saved", "Flag")),
    )
    feature = CaveBot(
        route,
        config=SessionConfig(),
        start_index=1,
        save_progress=lambda route, index: saved.append(index),
        map_region=(1, 2, 3, 4),
    )
    assert feature.status.waypoint_id == "saved"
    assert feature.sensors()[0].name == "Flag"
    navigation = feature.tick(
        context(feature, 0, marker=Marker((5, 5), False), frame=1)
    ).requests[0]
    feature.on_result(result(navigation, ResultState.EXECUTED, 0.01))
    feature.tick(context(feature, 0.2, marker=Marker((5, 5), True), frame=2))
    assert saved == [0]
    assert feature.status.waypoint_id == "first"


def test_combat_preempts_stand_and_requires_same_waypoint_again_before_checkpoint():
    saved = []
    feature = bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(
            stand_seconds=1, follow=False, loot=False, count_stable_samples=1
        ),
        save_progress=lambda route, index: saved.append(index),
    )
    start_arrival(feature)
    feature.tick(context(feature, 0.2, marker=Marker((1, 1), True), frame=2))
    assert feature.status.phase == "Standing"

    feature.tick(
        context(
            feature, 0.4, marker=Marker((1, 1), True),
            targets={"dragon": Target(1, None, True)}, frame=3,
        )
    )
    feature.tick(
        context(
            feature, 0.6, marker=Marker((1, 1), True),
            targets={"dragon": Target(0)}, frame=4,
        )
    )
    reacquire = feature.tick(
        context(feature, 0.8, marker=Marker((7, 7), False), frame=5)
    )
    assert reacquire.requests[0].action.point == (7, 7)
    assert saved == []


def test_combat_during_arrival_clears_then_refinds_same_waypoint():
    feature = bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(follow=False, loot=False, count_stable_samples=1),
    )
    first = feature.tick(context(feature, 0, marker=Marker((8, 8)), frame=1)).requests[0]
    feature.on_result(result(first, ResultState.EXECUTED, 0.01))

    combat = feature.tick(
        context(
            feature, 0.2, marker=Marker((8, 8)),
            targets={"dragon": Target(1, (4, 4), True)}, frame=2,
        )
    )
    assert combat.requests == ()
    feature.tick(
        context(
            feature, 0.4, marker=Marker((8, 8)),
            targets={"dragon": Target(0)}, frame=3,
        )
    )
    retry = feature.tick(
        context(feature, 0.6, marker=Marker((12, 12)), frame=4)
    ).requests[0]
    assert retry.action.point == (12, 12)
    assert feature.status.waypoint_id == "same"


def test_combat_timeout_and_backend_failure_are_actionable_failures():
    feature = bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(follow=False, combat_timeout=0.5),
    )
    start_arrival(feature)
    select = feature.tick(
        context(
            feature, 0.2, marker=Marker((1, 1)),
            targets={"dragon": Target(1, (2, 2), False)}, frame=2,
        )
    ).requests[0]
    feature.on_result(result(select, ResultState.FAILED, 0.1, reason="window unavailable"))
    assert feature.failed
    assert feature.error == "window unavailable"

    timed = bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(follow=False, combat_timeout=0.5),
    )
    start_arrival(timed)
    timed.tick(
        context(
            timed, 0.2, marker=Marker((1, 1)),
            targets={"dragon": Target(1, None, True)}, frame=2,
        )
    )
    final = timed.tick(
        context(
            timed, 0.7, marker=Marker((1, 1)),
            targets={"dragon": Target(1, None, True)}, frame=3,
        )
    )
    assert final.finished
    assert timed.status.reason == "combat_timeout"


def test_pause_preserves_remaining_combat_timeout():
    feature = bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(follow=False, combat_timeout=0.5),
    )
    start_arrival(feature)
    target = {"dragon": Target(1, None, True)}
    feature.tick(
        context(feature, 0.2, marker=Marker((1, 1)), targets=target, frame=2)
    )

    feature.on_pause()
    feature.on_resume(paused_for=10.0)

    before_deadline = feature.tick(
        context(feature, 10.69, marker=Marker((1, 1)), targets=target, frame=3)
    )
    assert not before_deadline.finished
    at_deadline = feature.tick(
        context(feature, 10.70, marker=Marker((1, 1)), targets=target, frame=4)
    )
    assert at_deadline.finished
    assert feature.status.reason == "combat_timeout"


def test_runtime_cancels_queued_navigation_when_combat_appears():
    class Clock:
        value = 0.0

        def monotonic(self):
            return self.value

    class Adapter:
        def __init__(self, clock):
            self.clock = clock
            self.combat = False
            self.actions = []

        def ready(self):
            return True

        def observe(self, sensors, token):
            token.checkpoint()
            readings = {}
            for sensor in sensors:
                if sensor.kind is SensorKind.MARKER:
                    readings[sensor] = Reading(Marker((10, 10), False))
                elif sensor.kind is SensorKind.TARGET:
                    target = Target(1, (20, 20), False) if self.combat else Target(0)
                    readings[sensor] = Reading(target)
            return ObservationBatch(readings, self.clock.monotonic())

        def execute(self, action, token):
            token.checkpoint()
            self.actions.append(action)

        def close(self):
            pass

    class OneHeal:
        name = "heal"
        interval = 0.2

        def __init__(self):
            self.used = False

        def sensors(self):
            return ()

        def tick(self, context):
            if self.used:
                return Step()
            self.used = True
            return Step((Request("heal", Action(ActionKind.PRESS, key="F1"), Priority.HEAL),))

        def on_result(self, result):
            pass

    clock = Clock()
    adapter = Adapter(clock)
    feature = bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(follow=False),
    )
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(feature)
    session.start_feature(OneHeal())

    session.tick_once()
    assert [(action.kind, action.key) for action in adapter.actions] == [
        (ActionKind.PRESS, "F1")
    ]
    adapter.combat = True
    clock.value = 0.2
    session.tick_once()
    assert [action.point for action in adapter.actions if action.kind is ActionKind.LEFT_CLICK] == [
        (20, 20)
    ]
    assert all(action.point != (10, 10) for action in adapter.actions)
    session.close()


def test_runtime_never_dispatches_queued_navigation_after_target_evidence_fails():
    class Clock:
        value = 0.0

        def monotonic(self):
            return self.value

    class Adapter:
        def __init__(self, clock):
            self.clock = clock
            self.unreadable = False
            self.actions = []

        def ready(self):
            return True

        def observe(self, sensors, token):
            token.checkpoint()
            readings = {}
            for sensor in sensors:
                if sensor.kind is SensorKind.MARKER:
                    readings[sensor] = Reading(Marker((10, 10), False))
                elif not self.unreadable:
                    readings[sensor] = Reading(Target(0))
                elif sensor.name == "urgent":
                    readings[sensor] = Reading(
                        None, Quality.ERROR, "urgent detector failed"
                    )
                else:
                    readings[sensor] = Reading(Target(1, (20, 20), False))
            return ObservationBatch(readings, self.clock.monotonic())

        def execute(self, action, token):
            token.checkpoint()
            self.actions.append(action)

        def close(self):
            pass

    class OneHeal:
        name = "heal"
        interval = 0.2

        def __init__(self):
            self.used = False

        def sensors(self):
            return ()

        def tick(self, context):
            if self.used:
                return Step()
            self.used = True
            return Step(
                (Request("heal", Action(ActionKind.PRESS, key="F1"), Priority.HEAL),)
            )

        def on_result(self, result):
            pass

    clock = Clock()
    adapter = Adapter(clock)
    feature = bot(
        targets=(TargetSpec(1, "urgent"), TargetSpec(2, "ordinary")),
        config=SessionConfig(follow=False),
    )
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(feature)
    session.start_feature(OneHeal())

    session.tick_once()
    assert [(action.kind, action.key) for action in adapter.actions] == [
        (ActionKind.PRESS, "F1")
    ]

    adapter.unreadable = True
    clock.value = 0.2
    session.tick_once()

    assert adapter.actions == [Action(ActionKind.PRESS, key="F1")]
    status = session.feature_status("CaveBot")
    assert not status.active
    assert status.phase == "Failed"
    assert "urgent detector failed" in status.error
    session.close()
