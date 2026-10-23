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
            value = follow.get(sensor.name) if isinstance(follow, dict) else follow
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


def test_walking_only_route_does_not_require_unreachable_loot_operation():
    feature = bot(
        config=SessionConfig(loot=True, loot_button="right"),
        loot_points=((1, 2),),
    )

    assert feature.required_operations() == ("left_click",)


def test_targeted_route_requires_configured_loot_operation():
    feature = bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(loot=True, loot_button="right"),
        loot_points=((1, 2),),
    )

    assert feature.required_operations() == (
        "left_click", "battle_list_click", "right_click"
    )


def test_missing_marker_retries_after_timeout_and_recovers_without_restart():
    feature = bot(config=SessionConfig(marker_timeout=0.4))
    sensors = feature.sensors()
    assert [(sensor.kind, sensor.region, sensor.name) for sensor in sensors] == [
        (SensorKind.MARKER, (1, 2, 3, 4), "CheckMark"),
        (SensorKind.FOLLOW, (), ""),
    ]

    assert feature.tick(context(feature, 0.0, marker=Marker(None), frame=1)).requests == ()
    for frame, now in enumerate((0.4, 1.0, 5.0), start=2):
        waiting = feature.tick(context(feature, now, marker=Marker(None), frame=frame))
        assert not waiting.finished and not feature.failed
        assert waiting.requests == () and feature.error is None
    recovered = feature.tick(context(feature, 5.2, marker=Marker((10, 20)), frame=5))
    assert recovered.requests[0].action.point == (10, 20)


def test_arrival_timeout_renews_attempts_on_same_waypoint_until_explicit_stop():
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
    assert not final.finished and not feature.failed
    retry = feature.tick(context(feature, 1.3, marker=Marker((10, 20)), frame=6))
    assert retry.requests[0].action.point == (10, 20)
    assert feature.index == 0 and feature.status.completed_waypoints == 0
    feature.request_abort()
    stopped = feature.tick(context(feature, 1.4, marker=Marker((10, 20)), frame=7))
    assert stopped.finished and not stopped.requests
    assert retry.requests[0].key in stopped.cancel_keys


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
    assert not at_deadline.finished
    retry = feature.tick(context(feature, 10.6, marker=Marker((1, 1)), frame=4))
    assert retry.requests[0].action.point == (1, 1)
    assert feature.index == 0


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
        ("zero", Quality.KNOWN, 3.0, "", "invalid type str"),
    ),
)
def test_unavailable_target_evidence_waits_without_disabling_and_recovers(
    value, quality, observed_at, detail, expected
):
    feature = bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(follow=False),
    )
    observations = {}
    for sensor in feature.sensors():
        if sensor.kind is SensorKind.MARKER:
            observation = Observation(sensor, Marker((9, 9)), 3.0, 1, 1)
        else:
            observation = Observation(
                sensor, value, observed_at, 1, 1, quality, detail
            )
        observations[sensor] = observation
    step = feature.tick(
        FeatureContext(Snapshot(observations, frame_id=1, epoch=1, created_at=3.0), 3.0)
    )

    assert step.requests == ()
    assert not step.finished
    assert not feature.failed and feature.error is None
    assert feature.status.reason == "target_evidence_unavailable"
    assert "dragon" in step.phase and expected in step.phase
    recovered = feature.tick(context(feature, 3.2, marker=Marker((9, 9)), frame=2))
    assert recovered.requests[0].action.point == (9, 9)


def test_missing_target_observation_keeps_navigation_enabled_without_input():
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
    assert not step.finished and feature.error is None
    assert feature.status.reason == "target_evidence_unavailable"
    assert "no observation" in step.phase


def test_readable_target_can_be_attacked_when_higher_priority_name_is_unreadable():
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

    assert not step.finished and feature.error is None
    assert len(step.requests) == 1
    assert step.requests[0].action.point == (20, 20)
    assert step.requests[0].requirements[0].sensor.name == "ordinary"


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


def test_follow_is_rechecked_while_attack_pending_and_without_new_target_readings():
    feature = bot(targets=(TargetSpec(1, "Lizard *"),), config=SessionConfig(walking=False))
    first = feature.tick(context(feature, 0, targets={"Lizard *": Target(1, (4, 4))}))
    assert first.requests[0].action.point == (4, 4)
    # Leave the attack pending: follow has its own action slot.
    waiting = feature.tick(context(feature, 0.1, follow=(30, 31), frame=2))
    request = waiting.requests[0]
    assert request.requirements[0].sensor.kind is SensorKind.FOLLOW
    feature.on_result(result(request, ResultState.EXECUTED, 0.1))
    assert feature.tick(context(feature, 0.11, follow=(30, 31), frame=2)).requests == ()
    again = feature.tick(context(feature, 0.12, follow=(30, 31), frame=3))
    assert again.requests[0].action.point == (30, 31)  # no follow cooldown

    feature.on_result(result(again.requests[0], ResultState.EXECUTED, 0.12))
    feature.on_result(result(first.requests[0], ResultState.EXECUTED, 0.12))
    unreadable = context(feature, 0.2, follow=(30, 31), frame=4)
    observations = dict(unreadable.snapshot.observations)
    for sensor in observations:
        if sensor.kind is SensorKind.TARGET:
            observations[sensor] = Observation(sensor, None, 0.2, 4, 1, Quality.UNKNOWN)
    step = feature.tick(FeatureContext(Snapshot(observations, 4, 1, 0.2), 0.2))
    assert not step.finished and feature.error is None
    assert [request.action.point for request in step.requests] == [(30, 31)]


def test_partial_count_drop_attacks_next_target_before_loot_then_restores_follow():
    feature = bot(
        targets=(TargetSpec(1, "Lizard *"),),
        config=SessionConfig(walking=False, count_stable_samples=1, loot_button="right"),
        loot_points=((10, 10),),
    )
    feature.tick(context(feature, 0, targets={"Lizard *": Target(3, (5, 5), True, False)}))
    killed = feature.tick(context(feature, 0.2, frame=2,
        targets={"Lizard *": Target(2, (6, 6), False, False)}, follow={"disable": (30, 31)}))
    assert [(r.requirements[0].sensor.kind, r.action.point) for r in killed.requests] == [
        (SensorKind.FOLLOW, (30, 31)), (SensorKind.TARGET, (6, 6)),
    ]
    assert all(r.action.kind is ActionKind.LEFT_CLICK for r in killed.requests)
    for request in killed.requests:
        feature.on_result(result(request, ResultState.EXECUTED, 0.2))

    looting = feature.tick(context(feature, 0.4, frame=3,
        targets={"Lizard *": Target(2, (6, 6), True, False)}))
    assert [r.action for r in looting.requests] == [Action(ActionKind.RIGHT_CLICK, point=(10, 10))]
    feature.on_result(result(looting.requests[0], ResultState.EXECUTED, 0.4))
    restored = feature.tick(context(feature, 0.6, frame=4,
        targets={"Lizard *": Target(2, (6, 6), True, False)}, follow={"": (30, 31)}))
    assert [r.action.point for r in restored.requests] == [(30, 31)]
    assert restored.requests[0].requirements[0].sensor.name == ""
    assert not restored.finished and not feature.failed


def test_unreadable_follow_does_not_block_next_target_after_a_kill():
    feature = bot(
        targets=(TargetSpec(1, "Lizard *"),),
        config=SessionConfig(walking=False, count_stable_samples=1, loot_button="right"),
        loot_points=((10, 10),),
    )
    feature.tick(context(feature, 0, targets={"Lizard *": Target(3, (5, 5), True)}))
    killed = context(feature, 0.2, frame=2,
                     targets={"Lizard *": Target(2, (6, 6), False)})
    observations = dict(killed.snapshot.observations)
    for sensor in observations:
        if sensor.kind is SensorKind.FOLLOW:
            observations[sensor] = Observation(sensor, None, 0.2, 2, 1, Quality.UNKNOWN)
    step = feature.tick(FeatureContext(Snapshot(observations, 2, 1, 0.2), 0.2))
    assert not step.finished and not feature.failed
    assert [request.action for request in step.requests] == [Action(ActionKind.LEFT_CLICK, point=(6, 6))]
    assert step.requests[0].requirements[0].sensor.kind is SensorKind.TARGET


def test_unreadable_stand_times_out_to_follow_and_skips_loot_until_recovered():
    feature = bot(
        targets=(TargetSpec(1, "Lizard *"),),
        config=SessionConfig(walking=False, follow=False, count_stable_samples=1,
                             loot_button="right"),
        loot_points=((10, 10),),
    )

    def obscured(now, frame, target, *, targets_unreadable=False):
        value = context(feature, now, frame=frame, targets={"Lizard *": target},
                        follow={"": (30, 31)})
        observations = dict(value.snapshot.observations)
        for sensor in observations:
            if (sensor.kind is SensorKind.FOLLOW and sensor.name == "disable") or (
                targets_unreadable and sensor.kind is SensorKind.TARGET
            ):
                observations[sensor] = Observation(sensor, None, now, frame, 1, Quality.UNKNOWN)
        return FeatureContext(Snapshot(observations, frame, 1, now), now)

    feature.tick(context(feature, 0, targets={"Lizard *": Target(3, (5, 5), True)}))
    feature.tick(obscured(0.2, 2, Target(2, (6, 6), True)))
    feature.tick(obscured(0.4, 3, Target(2), targets_unreadable=True))
    waiting = feature.tick(obscured(2.39, 4, Target(2), targets_unreadable=True))
    assert not waiting.requests
    # Target-reading waits must not extend the stand timeout. Even with the
    # normal follow setting off, recovery requests follow and the next attack.
    recovered = feature.tick(obscured(2.41, 5, Target(2, (6, 6), False)))
    assert [r.action.point for r in recovered.requests] == [(30, 31), (6, 6)]
    assert recovered.requests[0].requirements[0].sensor.name == ""
    assert not recovered.finished and not feature.failed
    for request in recovered.requests:
        feature.on_result(result(request, ResultState.EXECUTED, 2.41))
    another_kill = feature.tick(obscured(2.6, 6, Target(1, (6, 6), True)))
    assert all(r.action.kind is not ActionKind.RIGHT_CLICK for r in another_kill.requests)
    assert not another_kill.finished
    feature.tick(context(feature, 2.8, frame=7,
                         targets={"Lizard *": Target(1, (6, 6), True)}))
    loot = feature.tick(context(feature, 3, frame=8, targets={"Lizard *": Target(0)}))
    assert any(r.action == Action(ActionKind.RIGHT_CLICK, point=(10, 10)) for r in loot.requests)


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


def test_input_failure_is_reported_but_combat_timeout_keeps_attacking():
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
    assert not final.finished and not timed.failed
    retarget = timed.tick(context(timed, 1.3, marker=Marker((1, 1)),
                                 targets={"dragon": Target(1, (2, 2), False)}, frame=4))
    assert retarget.requests[0].action.point == (2, 2)


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
    assert not at_deadline.finished and not feature.failed
    feature.request_abort()
    stopped = feature.tick(context(feature, 10.8, marker=Marker((1, 1)),
                                   targets=target, frame=5))
    assert stopped.finished and not stopped.requests


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


def test_runtime_cancels_old_navigation_and_selects_current_readable_target():
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

    assert adapter.actions == [Action(ActionKind.PRESS, key="F1"),
                               Action(ActionKind.LEFT_CLICK, point=(20, 20))]
    status = session.feature_status("CaveBot")
    assert status.active and status.error is None
    # The old waypoint click must never be replayed on the changed battle list.
    assert all(action.point != (10, 10) for action in adapter.actions)
    session.close()
