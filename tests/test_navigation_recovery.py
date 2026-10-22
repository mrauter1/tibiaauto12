from __future__ import annotations

import pytest

from engine.CaveBot.routes import Route, Waypoint
from features.cavebot import CaveBot, SessionConfig, TargetSpec
from tibia_client import (
    ActionResult,
    FeatureContext,
    Marker,
    Observation,
    Quality,
    ResultState,
    SensorKind,
    Snapshot,
    Target,
)


MAP_REGION = (1, 2, 30, 40)
BATTLE_REGION = (40, 50, 80, 90)


def make_bot(
    route: Route | None = None,
    *,
    config: SessionConfig | None = None,
    targets: tuple[TargetSpec, ...] = (),
    loop: bool = True,
    save_progress=None,
) -> CaveBot:
    return CaveBot(
        route or Route("route", (Waypoint("only", "Flag"),)),
        targets,
        config=config or SessionConfig(),
        loop=loop,
        save_progress=save_progress,
        map_region=MAP_REGION,
        battle_region=BATTLE_REGION,
    )


def context(
    bot: CaveBot,
    now: float,
    *,
    marker: Marker | None,
    targets: dict[str, Target] | None = None,
    frame: int,
    marker_quality: Quality = Quality.KNOWN,
) -> FeatureContext:
    observations = {}
    for sensor in bot.sensors():
        if sensor.kind is SensorKind.MARKER:
            value = marker
            quality = marker_quality
        elif sensor.kind is SensorKind.TARGET:
            value = (targets or {}).get(sensor.name, Target(0))
            quality = Quality.KNOWN
        else:
            value = None
            quality = Quality.KNOWN
        observations[sensor] = Observation(
            sensor, value, now, frame, 1, quality, "unavailable" if quality is not Quality.KNOWN else ""
        )
    return FeatureContext(
        Snapshot(observations, frame_id=frame, epoch=1, created_at=now), now
    )


def executed(request, at: float) -> ActionResult:
    return ActionResult(
        request_id=f"id-{request.key}",
        owner="CaveBot",
        key=request.key,
        state=ResultState.EXECUTED,
        at=at,
    )


def start_navigation(bot: CaveBot, *, at: float = 0.0, position=(10, 10)):
    request = bot.tick(
        context(bot, at, marker=Marker(position), frame=1)
    ).requests[0]
    bot.on_result(executed(request, at + 0.01))
    return request


def test_progress_history_is_bounded_and_explains_moving_stalled_and_missing():
    bot = make_bot(
        config=SessionConfig(
            arrival_timeout=2,
            navigation_history_size=3,
            navigation_progress_pixels=3,
            navigation_stall_seconds=0.2,
        )
    )
    start_navigation(bot)

    bot.tick(context(bot, 0.10, marker=Marker((8, 10)), frame=2))
    assert bot.status.navigation_state == "moving"
    assert bot.status.marker_position == (8, 10)

    bot.tick(context(bot, 0.30, marker=Marker((9, 10)), frame=3))
    assert bot.status.navigation_state == "stalled"
    assert bot.status.reason == "navigation_stalled"

    bot.tick(context(bot, 0.40, marker=Marker((4, 10)), frame=4))
    assert bot.status.navigation_state == "moving"
    assert bot.status.reason == "navigation_moving"

    bot.tick(
        context(
            bot,
            0.50,
            marker=None,
            marker_quality=Quality.UNKNOWN,
            frame=5,
        )
    )
    progress = bot.navigation_progress
    assert progress["state"] == "missing"
    assert progress["marker_position"] is None
    assert progress["history"] == (
        (0.30, 3, (9, 10)),
        (0.40, 4, (4, 10)),
        (0.50, 5, None),
    )


def test_repeated_tick_of_same_frame_cannot_change_progress_diagnosis():
    bot = make_bot(
        config=SessionConfig(
            arrival_timeout=2,
            navigation_progress_pixels=3,
            navigation_stall_seconds=0.05,
        )
    )
    start_navigation(bot)
    bot.tick(context(bot, 0.10, marker=Marker((6, 10)), frame=2))
    assert bot.status.navigation_state == "moving"

    bot.tick(context(bot, 0.20, marker=Marker((6, 10)), frame=2))

    assert bot.status.navigation_state == "moving"
    assert bot.status.reason == "navigation_moving"
    assert bot.navigation_progress["history"][-1] == (0.10, 2, (6, 10))


def test_pause_excludes_paused_time_from_stall_diagnosis_and_attempt_timeout():
    bot = make_bot(
        config=SessionConfig(
            arrival_timeout=0.5,
            movement_attempts=1,
            navigation_stall_seconds=0.2,
        )
    )
    start_navigation(bot)
    bot.on_pause()
    bot.on_resume(paused_for=10.0)

    before_stall = bot.tick(
        context(bot, 10.19, marker=Marker((10, 10)), frame=2)
    )
    assert not before_stall.finished
    assert bot.status.navigation_state == "moving"

    stalled = bot.tick(context(bot, 10.22, marker=Marker((10, 10)), frame=3))
    assert not stalled.finished
    assert bot.status.navigation_state == "stalled"

    timed_out = bot.tick(context(bot, 10.51, marker=Marker((10, 10)), frame=4))
    assert not timed_out.finished
    retry = bot.tick(context(bot, 10.6, marker=Marker((10, 10)), frame=5))
    assert retry.requests[0].action.point == (10, 10)
    assert bot.index == 0


def test_marker_jitter_does_not_advance_waypoint_and_attempt_batches_keep_retrying():
    bot = make_bot(
        config=SessionConfig(
            arrival_timeout=0.3,
            movement_attempts=2,
            navigation_progress_pixels=3,
            navigation_stall_seconds=0.1,
        )
    )
    start_navigation(bot)

    bot.tick(context(bot, 0.20, marker=Marker((11, 10)), frame=2))
    retry_search = bot.tick(
        context(bot, 0.31, marker=Marker((10, 10)), frame=3)
    )
    assert not retry_search.finished
    assert bot.navigation_progress["attempts_used"] == 1

    second = bot.tick(
        context(bot, 0.32, marker=Marker((11, 10)), frame=4)
    ).requests[0]
    bot.on_result(executed(second, 0.32))
    assert bot.navigation_progress["attempts_used"] == 2
    assert bot.navigation_progress["attempts_remaining"] == 0

    bot.tick(context(bot, 0.50, marker=Marker((10, 10)), frame=5))
    waiting = bot.tick(context(bot, 0.62, marker=Marker((11, 10)), frame=6))
    assert not waiting.finished
    retry = bot.tick(context(bot, 0.7, marker=Marker((11, 10)), frame=7))
    assert retry.requests[0].action.point == (11, 10)
    assert bot.index == 0 and bot.status.completed_waypoints == 0


def test_exit_cancels_pending_navigation_without_advancing_route():
    route = Route("finite", (Waypoint("first", "Flag"), Waypoint("next", "Star")))
    bot = make_bot(route)
    navigation = bot.tick(
        context(bot, 0.0, marker=Marker((10, 10)), frame=1)
    ).requests[0]

    bot.request_exit()
    stopped = bot.tick(context(bot, 0.1, marker=Marker((10, 10)), frame=2))

    assert stopped.finished
    assert stopped.cancel_keys == (navigation.key,)
    assert bot.status.reason == "exit_requested"
    assert bot.index == 0
    assert bot.status.completed_waypoints == 0
    assert bot.status.marker_position is None
    assert bot.status.navigation_attempt == 0
    assert bot.navigation_progress["attempts_used"] == 0


def test_exit_waits_until_combat_and_loot_state_are_clear():
    bot = make_bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(follow=False, loot=False, count_stable_samples=1),
    )
    active = {"dragon": Target(1, None, True)}
    bot.tick(context(bot, 0.0, marker=Marker((10, 10)), targets=active, frame=1))
    bot.request_exit()

    fighting = bot.tick(
        context(bot, 0.2, marker=Marker((10, 10)), targets=active, frame=2)
    )
    assert not fighting.finished
    assert bot.status.phase.startswith("Fighting")

    clearing = bot.tick(
        context(
            bot,
            0.4,
            marker=Marker((10, 10)),
            targets={"dragon": Target(0)},
            frame=3,
        )
    )
    assert not clearing.finished
    stopped = bot.tick(
        context(
            bot,
            0.6,
            marker=Marker((10, 10)),
            targets={"dragon": Target(0)},
            frame=4,
        )
    )
    assert stopped.finished
    assert bot.status.reason == "exit_requested"
    assert bot.status.completed_waypoints == 0


def test_immediate_abort_cancels_pending_combat_request_without_waiting_for_clear():
    bot = make_bot(
        targets=(TargetSpec(1, "dragon"),),
        config=SessionConfig(follow=False, loot=False),
    )
    active = {"dragon": Target(1, (50, 60), False)}
    selecting = bot.tick(
        context(bot, 0.0, marker=Marker((10, 10)), targets=active, frame=1)
    ).requests[0]
    assert selecting.key.startswith("select:")

    bot.request_abort()
    stopped = bot.tick(
        context(bot, 0.1, marker=Marker((10, 10)), targets=active, frame=2)
    )

    assert stopped.finished
    assert stopped.cancel_keys == (selecting.key,)
    assert stopped.requests == ()
    assert bot.status.reason == "abort_requested"


def test_finite_route_finishes_last_waypoint_while_default_still_loops():
    route = Route("finite", (Waypoint("first", "Flag"), Waypoint("last", "Star")))
    saved = []
    finite = make_bot(route, loop=False, save_progress=lambda _route, index: saved.append(index))

    first = finite.tick(
        context(finite, 0.0, marker=Marker((10, 10)), frame=1)
    ).requests[0]
    finite.on_result(executed(first, 0.01))
    finite.tick(context(finite, 0.1, marker=Marker((10, 10), True), frame=2))
    assert finite.index == 1
    assert saved == [1]

    last = finite.tick(
        context(finite, 0.2, marker=Marker((20, 20)), frame=3)
    ).requests[0]
    finite.on_result(executed(last, 0.21))
    completed = finite.tick(
        context(finite, 0.3, marker=Marker((20, 20), True), frame=4)
    )
    assert completed.finished
    assert finite.status.reason == "route_complete"
    assert finite.index == 1
    assert finite.status.completed_waypoints == 2
    assert saved == [1]
    assert finite.status.marker_position is None
    assert finite.status.navigation_attempt == 0
    assert finite.navigation_progress["attempts_used"] == 0

    looping = make_bot(save_progress=lambda _route, index: saved.append(index))
    only = looping.tick(
        context(looping, 1.0, marker=Marker((5, 5)), frame=1)
    ).requests[0]
    looping.on_result(executed(only, 1.01))
    looped = looping.tick(
        context(looping, 1.1, marker=Marker((5, 5), True), frame=2)
    )
    assert not looped.finished
    assert looping.status.reason == "waypoint_complete"


def anchor_context(
    bot: CaveBot,
    now: float,
    *,
    reached_marks: set[str],
    quality: Quality = Quality.KNOWN,
    position: tuple[int, int] | None = (5, 5),
) -> FeatureContext:
    observations = {}
    for sensor in bot.anchor_sensors():
        marker = Marker(position, sensor.name in reached_marks)
        observations[sensor] = Observation(
            sensor,
            marker if quality is Quality.KNOWN else None,
            now,
            1,
            1,
            quality,
            "unknown" if quality is not Quality.KNOWN else "",
        )
    return FeatureContext(
        Snapshot(observations, frame_id=1, epoch=1, created_at=now), now
    )


def test_anchor_resume_requires_fresh_reached_evidence_and_explicit_ambiguity_choice():
    route = Route(
        "anchors",
        (
            Waypoint("north-a", "Flag"),
            Waypoint("north-b", "Flag"),
            Waypoint("south", "Star"),
        ),
    )
    bot = make_bot(route)

    unknown = anchor_context(bot, 0.0, reached_marks={"Flag"}, quality=Quality.UNKNOWN)
    assert bot.recognized_anchor_candidates(unknown) == ()
    with pytest.raises(ValueError, match="no reached"):
        bot.resume_from_recognized_anchor(unknown)

    positionless = anchor_context(
        bot, 0.05, reached_marks={"Flag"}, position=None
    )
    assert bot.recognized_anchor_candidates(positionless) == ()
    with pytest.raises(ValueError, match="no reached"):
        bot.resume_from_recognized_anchor(positionless)

    observed = anchor_context(bot, 0.1, reached_marks={"Flag"})
    assert bot.recognized_anchor_candidates(observed) == (0, 1)
    with pytest.raises(ValueError, match="ambiguous"):
        bot.resume_from_recognized_anchor(observed)
    with pytest.raises(ValueError, match="select"):
        bot.resume_from_recognized_anchor(observed, waypoint_index=2)

    assert bot.resume_from_recognized_anchor(observed, waypoint_index=1) == 1
    assert bot.index == 1
    assert bot.status.reason == "recognized_anchor_resume"
    assert bot.status.finished is False
