import json
from dataclasses import replace
from pathlib import Path

import pytest

from engine.CaveBot.routes import Route, Waypoint
from features.cavebot import TargetSpec
from features.hunt_cycle import CyclePhase, HuntCheckpointError, HuntCycleFeature
from tibia_client import ClientSession
from tibia_client.hunt_plan import hunt_plan_from_data, hunt_plan_template
from tibia_client.models import (
    Action,
    ActionKind,
    ActionResult,
    FeatureContext,
    Marker,
    Observation,
    Quality,
    Priority,
    Request,
    ResultState,
    Sensor,
    SensorKind,
    Snapshot,
    ObservationBatch,
    Reading,
    Step,
    Target,
)


def route_values():
    return {
        "outbound_route": Route("outbound_route", (Waypoint("out", "Flag"),)),
        "hunt_route": Route("hunt_route", (Waypoint("hunt", "Sword"),)),
        "return_route": Route("return_route", (Waypoint("back", "Money"),)),
    }


def plan(*, loot=False):
    data = hunt_plan_template()
    if not loot:
        data.pop("loot")
    data["supplies"][0].update(return_below=6, emergency_reserve=2, refill_to=10)
    data["actions"]["prepare"] = [
        {
            "id": "prepare_supply",
            "kind": "press",
            "key": "F1",
            "confirmation": {"sensor": "prepared", "field": "count", "op": "increased"},
        }
    ]
    data["actions"]["refill"] = [
        {
            "id": "buy_supply",
            "kind": "press",
            "key": "F2",
            "confirmation": {"sensor": "mana_potions", "field": "count", "op": "increased"},
        }
    ]
    return hunt_plan_from_data(data, route_values())


def bindings(value):
    return {
        sensor_id: Sensor(SensorKind.VISUAL, (0, 0, 20, 20), f"detector_{sensor_id}")
        for sensor_id in value.sensor_ids()
    }


def context(
    feature,
    frame,
    now,
    visual,
    *,
    reached=False,
    unknown=(),
    stale=(),
    targets=None,
):
    observations = {}
    for sensor in feature.sensors():
        observed_at = now - 2 if sensor.name in stale else now
        if sensor.kind is SensorKind.VISUAL:
            reading = visual.get(sensor.name, {"present": False, "position": None, "count": None})
            quality = Quality.UNKNOWN if sensor.name in unknown else Quality.KNOWN
            observations[sensor] = Observation(sensor, reading, observed_at, frame, 1, quality)
        elif sensor.kind is SensorKind.MARKER:
            observations[sensor] = Observation(sensor, Marker((10, 10), reached), now, frame, 1)
        elif sensor.kind is SensorKind.TARGET:
            observations[sensor] = Observation(
                sensor,
                (targets or {}).get(sensor.name, Target(0)),
                now,
                frame,
                1,
            )
    return FeatureContext(Snapshot(observations, frame, 1, now), now)


def result(request, at, state=ResultState.EXECUTED):
    return ActionResult(f"r-{at}", "CaveBot", request.key, state, at)


def make_feature(tmp_path=None, *, loot=False, resume=False, max_refill_rounds=10):
    value = replace(plan(loot=loot), max_refill_rounds=max_refill_rounds)
    return HuntCycleFeature(
        value,
        bindings(value),
        binding_revision="effective-layout-v1",
        map_region=(0, 0, 50, 50),
        battle_region=(0, 0, 50, 50),
        checkpoint_path=None if tmp_path is None else Path(tmp_path) / "hunt.state.json",
        resume=resume,
    )


def test_complete_cycle_refills_incrementally_and_resumes_outbound():
    feature = make_feature()
    visual = {
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
        "detector_prepared": {"present": True, "position": (2, 2), "count": 0},
    }
    frame, now = 1, 0.0

    step = feature.tick(context(feature, frame, now, visual))
    assert step.requests[0].action.key == "F1"
    feature.on_result(result(step.requests[0], now))
    frame += 1
    now += 0.2
    visual["detector_prepared"]["count"] = 1
    feature.tick(context(feature, frame, now, visual))

    # Finish prepare and outbound-before, then start/walk/finish the finite route.
    for _ in range(3):
        frame += 1
        now += 0.2
        feature.tick(context(feature, frame, now, visual))
    frame += 1
    now += 0.2
    movement = feature.tick(context(feature, frame, now, visual)).requests[0]
    feature.on_result(result(movement, now))
    frame += 1
    now += 0.2
    feature.tick(context(feature, frame, now, visual, reached=True))
    frame += 1
    now += 0.2
    feature.tick(context(feature, frame, now, visual))  # outbound-after -> hunt
    assert feature.status.phase == "hunt"

    # A low observed count requests a safe hunt exit, then executes return travel.
    visual["detector_mana_potions"]["count"] = 5
    frame += 1
    now += 0.2
    feature.tick(context(feature, frame, now, visual))  # create hunt bot
    frame += 1
    now += 0.2
    feature.tick(context(feature, frame, now, visual))  # request exit, enter return-before
    assert feature.status.return_requested
    for _ in range(2):
        frame += 1
        now += 0.2
        feature.tick(context(feature, frame, now, visual))
    frame += 1
    now += 0.2
    movement = feature.tick(context(feature, frame, now, visual)).requests[0]
    feature.on_result(result(movement, now))
    frame += 1
    now += 0.2
    feature.tick(context(feature, frame, now, visual, reached=True))

    # Return-after, deposit and sell are empty. Refill performs two separately
    # observed transactions instead of assuming the first purchase hit target.
    while feature.status.phase != "refill":
        frame += 1
        now += 0.2
        feature.tick(context(feature, frame, now, visual))
    frame += 1
    now += 0.2
    buy1 = feature.tick(context(feature, frame, now, visual)).requests[0]
    feature.on_result(result(buy1, now))
    visual["detector_mana_potions"]["count"] = 8
    frame += 1
    now += 0.2
    feature.tick(context(feature, frame, now, visual))
    frame += 1
    now += 0.2
    feature.tick(context(feature, frame, now, visual))  # evaluate target and start next round
    frame += 1
    now += 0.2
    buy2 = feature.tick(context(feature, frame, now, visual)).requests[0]
    feature.on_result(result(buy2, now))
    visual["detector_mana_potions"]["count"] = 12
    frame += 1
    now += 0.2
    feature.tick(context(feature, frame, now, visual))
    frame += 1
    now += 0.2
    feature.tick(context(feature, frame, now, visual))

    assert feature.status.phase == "outbound_before"
    assert feature.status.cycle_count == 1
    assert feature.status.supply_counts["mana_potions"] == 12


@pytest.mark.parametrize("limit", [1, 2])
@pytest.mark.parametrize("reach_target", [False, True])
def test_refill_limit_includes_first_pass_and_allows_success_on_last(limit, reach_target):
    feature = make_feature(max_refill_rounds=limit)
    feature._enter(CyclePhase.SELL, "test")
    visual = {"detector_mana_potions": {"present": True, "position": (1, 1), "count": 3}}
    purchases = []
    for frame in range(1, 20):
        now = frame * 0.2
        step = feature.tick(context(feature, frame, now, visual))
        for request in step.requests:
            purchases.append(request.action.key)
            feature.on_result(result(request, now))
            visual["detector_mana_potions"]["count"] += 1
            if reach_target and len(purchases) == limit:
                visual["detector_mana_potions"]["count"] = 10
        if feature.status.manual_required or feature.status.phase == "outbound_before":
            break
    assert purchases == ["F2"] * limit
    if reach_target:
        assert feature.status.phase == "outbound_before"
        assert feature.status.cycle_count == 1
        assert feature.status.refill_round == 0
    else:
        assert feature.status.manual_reason == "refill_target_not_reached"


@pytest.mark.parametrize("restart_at", ["between_passes", "pending", "confirmed"])
def test_refill_checkpoint_resume_preserves_pass_budget(tmp_path, restart_at):
    feature = make_feature(tmp_path, max_refill_rounds=2)
    feature._enter(CyclePhase.REFILL, "test")
    visual = {"detector_mana_potions": {"present": True, "position": (1, 1), "count": 3}}
    purchases, restarted = [], False
    for frame in range(1, 25):
        now = frame * 0.2
        step = feature.tick(context(feature, frame, now, visual))
        for request in step.requests:
            purchases.append(request.action.key)
            feature.on_result(result(request, now))
            visual["detector_mana_potions"]["count"] += 1
        checkpoint_ready = {
            "between_passes": feature.status.reason == "refill_target_pending",
            "pending": len(purchases) == 2 and bool(step.requests),
            "confirmed": len(purchases) == 2 and feature.status.reason == "machine_confirmed",
        }[restart_at]
        if checkpoint_ready and not restarted:
            saved = json.loads((tmp_path / "hunt.state.json").read_text())
            assert saved["refill_round"] == 1
            feature = make_feature(tmp_path, resume=True, max_refill_rounds=2)
            restarted = True
        if feature.status.manual_required:
            break
    assert restarted
    assert purchases == ["F2", "F2"]
    assert feature.status.manual_reason == "refill_target_not_reached"


def test_refill_limit_waits_for_known_counts_without_repeating_the_pass():
    feature = make_feature(max_refill_rounds=1)
    feature._enter(CyclePhase.REFILL, "test")
    visual = {"detector_mana_potions": {"present": True, "position": (1, 1), "count": 3}}
    request = feature.tick(context(feature, 1, 0, visual)).requests[0]
    feature.on_result(result(request, 0))
    visual["detector_mana_potions"]["count"] = 4
    feature.tick(context(feature, 2, 0.2, visual))
    for frame in range(3, 6):
        step = feature.tick(context(feature, frame, frame * 0.2, visual, unknown=("detector_mana_potions",)))
        assert not step.requests
        assert feature.status.reason == "waiting_for_supply_counts"
    assert not feature.tick(context(feature, 6, 1.2, visual)).requests
    assert feature.status.manual_reason == "refill_target_not_reached"


def test_refill_pass_limit_preserves_guarded_action_retries():
    feature = make_feature(max_refill_rounds=1)
    feature._enter(CyclePhase.REFILL, "test")
    visual = {"detector_mana_potions": {"present": True, "position": (1, 1), "count": 3}}
    first = feature.tick(context(feature, 1, 0, visual)).requests[0]
    feature.on_result(result(first, 0))
    retry = feature.tick(context(feature, 2, 2.1, visual)).requests[0]
    assert retry.action.key == "F2"
    feature.on_result(result(retry, 2.1))
    visual["detector_mana_potions"]["count"] = 4
    assert not feature.tick(context(feature, 3, 2.3, visual)).requests
    assert not feature.tick(context(feature, 4, 2.5, visual)).requests
    assert feature.status.manual_reason == "refill_target_not_reached"


@pytest.mark.parametrize("pending", [False, True])
def test_legacy_checkpoint_in_extra_refill_pass_cannot_send_more_input(tmp_path, pending):
    feature = make_feature(tmp_path, max_refill_rounds=1)
    feature._enter(CyclePhase.REFILL, "test")
    visual = {"detector_mana_potions": {"present": True, "position": (1, 1), "count": 3}}
    if pending:
        request = feature.tick(context(feature, 1, 0, visual)).requests[0]
        feature.on_result(result(request, 0))
        visual["detector_mana_potions"]["count"] = 4
    # Old v1 writers could persist round == limit after starting an extra pass.
    path = tmp_path / "hunt.state.json"
    checkpoint = json.loads(path.read_text())
    checkpoint["refill_round"] = 1
    path.write_text(json.dumps(checkpoint))
    resumed = make_feature(tmp_path, resume=True, max_refill_rounds=1)
    reasons = []
    for frame in range(2, 8):
        step = resumed.tick(context(resumed, frame, frame * 0.2, visual))
        assert not step.requests
        reasons.append(resumed.status.reason)
        if resumed.status.manual_required:
            break
    if pending:
        assert "machine_confirmed_after_resume" in reasons
        assert resumed.status.manual_reason == "refill_target_not_reached"
    else:
        assert resumed.status.manual_reason == "refill_round_limit_reached"


def test_unknown_and_stale_visuals_never_authorize_input():
    feature = make_feature()
    visual = {
        "detector_prepared": {"present": True, "position": (1, 1), "count": 0},
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
    }
    assert feature.tick(context(feature, 1, 0.0, visual, unknown=("detector_prepared",))).requests == ()
    assert feature.tick(context(feature, 2, 2.0, visual, stale=("detector_prepared",))).requests == ()


def test_input_failure_reconciles_before_retry_and_unknown_timeout_goes_manual():
    feature = make_feature()
    visual = {
        "detector_prepared": {"present": True, "position": (1, 1), "count": 0},
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
    }
    first = feature.tick(context(feature, 1, 0.0, visual)).requests[0]
    feature.on_result(result(first, 0.0, ResultState.FAILED))
    assert feature.tick(context(feature, 2, 0.5, visual)).requests == ()
    retry = feature.tick(context(feature, 3, 2.1, visual)).requests
    assert len(retry) == 1
    feature.on_result(result(retry[0], 2.1, ResultState.FAILED))
    feature.tick(context(feature, 4, 4.2, visual, unknown=("detector_prepared",)))
    assert feature.status.manual_required
    assert feature.status.manual_reason == "confirmation_unknown_after_timeout"


def test_pause_discards_no_evidence_and_resume_reconciles_postcondition():
    feature = make_feature()
    visual = {
        "detector_prepared": {"present": True, "position": (1, 1), "count": 0},
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
    }
    request = feature.tick(context(feature, 1, 0.0, visual)).requests[0]
    feature.on_result(result(request, 0.0))
    feature.on_pause()
    visual["detector_prepared"]["count"] = 1
    assert feature.tick(context(feature, 2, 1.0, visual)).requests == ()
    feature.on_resume(paused_for=1.0)
    feature.tick(context(feature, 3, 1.2, visual))
    feature.tick(context(feature, 4, 1.4, visual))
    assert feature.status.reason == "machine_confirmed"


def test_interrupted_mutation_resume_confirms_or_requires_manual(tmp_path):
    feature = make_feature(tmp_path, resume=False)
    visual = {
        "detector_prepared": {"present": True, "position": (1, 1), "count": 0},
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
    }
    feature.tick(context(feature, 1, 0.0, visual))

    resumed = make_feature(tmp_path, resume=True)
    visual["detector_prepared"]["count"] = 1
    resumed.tick(context(resumed, 2, 0.2, visual))
    assert resumed.status.reason == "machine_confirmed_after_resume"

    # Recreate the interrupted checkpoint and show unchanged evidence never repeats it.
    Path(tmp_path, "hunt.state.json").unlink()
    feature = make_feature(tmp_path, resume=False)
    visual["detector_prepared"]["count"] = 0
    feature.tick(context(feature, 3, 0.4, visual))
    resumed = make_feature(tmp_path, resume=True)
    resumed.tick(context(resumed, 4, 0.6, visual))
    assert resumed.status.manual_required
    assert resumed.status.manual_reason == "interrupted_mutation_ambiguous"

    # A fresh machine re-check keeps the interrupted baseline and can resolve it.
    assert resumed.acknowledge_manual(operator_reconciled=False)
    visual["detector_prepared"]["count"] = 1
    resumed.tick(context(resumed, 5, 0.8, visual))
    assert resumed.status.reason == "machine_confirmed_after_manual_check"
    assert resumed.status.action_id is None

    # An explicit operator reconciliation advances the exact interrupted action
    # rather than taking a new relative baseline and dispatching it again.
    Path(tmp_path, "hunt.state.json").unlink()
    feature = make_feature(tmp_path, resume=False)
    visual["detector_prepared"]["count"] = 0
    feature.tick(context(feature, 6, 1.0, visual))
    resumed = make_feature(tmp_path, resume=True)
    resumed.tick(context(resumed, 7, 1.2, visual))
    assert resumed.status.manual_required
    visual["detector_prepared"]["count"] = 1
    assert resumed.acknowledge_manual(operator_reconciled=True)
    assert resumed.status.action_id is None
    assert resumed.tick(context(resumed, 8, 1.4, visual)).requests == ()
    assert resumed.tick(context(resumed, 9, 1.6, visual)).requests == ()

    with pytest.raises(HuntCheckpointError, match="start fresh with resume=False"):
        value = plan()
        HuntCycleFeature(
            value,
            bindings(value),
            binding_revision="changed-layout",
            map_region=(0, 0, 50, 50),
            checkpoint_path=Path(tmp_path) / "hunt.state.json",
        )


def test_verified_loot_requires_source_and_destination_deltas():
    feature = make_feature(loot=True)
    feature._phase = CyclePhase.HUNT
    feature._looting = True
    feature._loot_opened = True
    visual = {
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
        "detector_prepared": {"present": True, "position": (1, 1), "count": 1},
        "detector_loot_corpse": {"present": True, "position": (5, 5), "count": 1},
        "detector_loot_container": {"present": True, "position": (6, 6), "count": 1},
        "detector_loot_backpack": {"present": True, "position": (10, 10), "count": 1},
        "detector_loot_free_slots": {"present": True, "position": (10, 10), "count": 5},
        "detector_loot_backpack_items": {"present": True, "position": (10, 10), "count": 20},
        "detector_gold_coin": {"present": True, "position": (7, 7), "count": 100},
    }
    transfer = feature.tick(context(feature, 1, 0.0, visual)).requests[0]
    feature.on_result(result(transfer, 0.0))
    visual["detector_gold_coin"]["count"] = 90
    # Source disappearance alone is insufficient while destination count is unchanged.
    feature.tick(context(feature, 2, 0.2, visual))
    assert feature.status.reason == "waiting_for_postcondition"
    visual["detector_loot_backpack_items"]["count"] = 30
    feature.tick(context(feature, 3, 0.4, visual))
    assert feature.status.reason == "machine_confirmed"

    # A destination increase must match the exact source decrease. A partial or
    # unrelated increase cannot authorize another transfer.
    mismatched = make_feature(loot=True)
    mismatched._phase = CyclePhase.HUNT
    mismatched._looting = True
    mismatched._loot_opened = True
    visual["detector_gold_coin"]["count"] = 100
    visual["detector_loot_backpack_items"]["count"] = 20
    transfer = mismatched.tick(context(mismatched, 10, 2.0, visual)).requests[0]
    mismatched.on_result(result(transfer, 2.0))
    visual["detector_gold_coin"]["count"] = 90
    visual["detector_loot_backpack_items"]["count"] = 25
    mismatched.tick(context(mismatched, 11, 4.1, visual))
    assert mismatched.status.manual_required
    assert mismatched.status.manual_reason == "postcondition_changed_unexpectedly"


def test_interrupted_dispatch_waits_for_result_and_arms_timeout_from_execution():
    feature = make_feature()
    visual = {
        "detector_prepared": {"present": True, "position": (1, 1), "count": 0},
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
    }
    request = feature.tick(context(feature, 1, 0.0, visual)).requests[0]

    assert feature.tick(context(feature, 2, 0.9, visual)).requests == ()
    assert feature.status.reason == "waiting_for_dispatch_result"
    feature.on_result(result(request, 0.9))
    assert feature.tick(context(feature, 3, 2.8, visual)).requests == ()
    retry = feature.tick(context(feature, 4, 2.9, visual)).requests
    assert len(retry) == 1

    lost = make_feature()
    lost.tick(context(lost, 1, 0.0, visual))
    lost.tick(context(lost, 2, 1.0, visual))
    assert lost.status.manual_required
    assert lost.status.manual_reason == "dispatch_result_unknown"


@pytest.mark.parametrize("paused_for", [True, float("nan"), float("inf"), -0.1])
def test_resume_rejects_invalid_pause_durations_without_corrupting_deadline(paused_for):
    feature = make_feature()
    visual = {
        "detector_prepared": {"present": True, "position": (1, 1), "count": 0},
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
    }
    request = feature.tick(context(feature, 1, 0.0, visual)).requests[0]
    feature.on_result(result(request, 0.0))
    deadline = feature._pending.deadline
    feature.on_pause()

    with pytest.raises(ValueError, match="finite non-negative"):
        feature.on_resume(paused_for=paused_for)
    assert feature._pending.deadline == deadline
    assert feature._paused


def test_checkpoint_rejects_phase_inconsistent_or_loosely_typed_state(tmp_path):
    feature = make_feature(tmp_path, resume=False)
    visual = {
        "detector_prepared": {"present": True, "position": (1, 1), "count": 0},
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
    }
    feature.tick(context(feature, 1, 0.0, visual))
    path = Path(tmp_path, "hunt.state.json")
    original = json.loads(path.read_text(encoding="utf-8"))

    corruptions = (
        ("action index", lambda data: data.update(action_index=99)),
        ("return_requested", lambda data: data.update(return_requested="yes")),
        ("pending action", lambda data: data["pending"].update(action_id="buy_supply")),
        ("fields", lambda data: data.update(unexpected=True)),
    )
    for message, corrupt in corruptions:
        data = json.loads(json.dumps(original))
        corrupt(data)
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(HuntCheckpointError, match=message):
            make_feature(tmp_path, resume=True)


def test_missing_loot_evidence_is_bounded_and_does_not_block_return():
    feature = make_feature(loot=True)
    feature._phase = CyclePhase.HUNT
    feature._looting = True
    feature._loot_opened = True
    feature._return_requested = True
    feature._return_mode = "after_fight"
    visual = {
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
        "detector_prepared": {"present": True, "position": (1, 1), "count": 1},
        "detector_loot_corpse": {"present": True, "position": (5, 5), "count": 1},
        "detector_loot_container": {"present": True, "position": (6, 6), "count": 1},
        "detector_loot_backpack": {"present": True, "position": (10, 10), "count": 1},
        "detector_loot_free_slots": {"present": True, "position": (10, 10), "count": 5},
        "detector_loot_backpack_items": {"present": True, "position": (10, 10), "count": 20},
        "detector_gold_coin": {"present": True, "position": (7, 7), "count": 100},
    }
    unknown = ("detector_gold_coin",)
    feature.tick(context(feature, 1, 0.0, visual, unknown=unknown))
    assert feature.status.reason == "waiting_for_loot_item_evidence"
    feature.tick(context(feature, 2, 2.0, visual, unknown=unknown))
    assert not feature._looting
    assert feature.status.reason == "loot_evidence_timeout_skipped"


@pytest.mark.parametrize("immediate", [False, True])
def test_operator_return_uses_fresh_route_evidence_when_required_supply_is_unknown(
    immediate,
):
    feature = make_feature()
    feature._phase = CyclePhase.HUNT
    feature.request_return(now=immediate)
    visual = {
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
        "detector_prepared": {"present": True, "position": (1, 1), "count": 1},
    }
    unknown = ("detector_mana_potions",)

    feature.tick(context(feature, 1, 0.0, visual, unknown=unknown))
    feature.tick(context(feature, 2, 0.2, visual, unknown=unknown))
    assert feature.status.phase == "return_before"
    assert feature.status.supply_counts["mana_potions"] is None

    feature.tick(context(feature, 3, 0.4, visual, unknown=unknown))
    feature.tick(context(feature, 4, 0.6, visual, unknown=unknown))
    navigation = feature.tick(
        context(feature, 5, 0.8, visual, unknown=unknown)
    )
    assert feature.status.phase == "return"
    assert len(navigation.requests) == 1
    assert navigation.requests[0].action.point == (10, 10)
    assert feature.status.supply_counts["mana_potions"] is None


def test_return_intent_only_upgrades_and_emergency_scan_wins_across_supplies():
    data = hunt_plan_template()
    data.pop("loot")
    data["supplies"][0].update(
        return_below=6, emergency_reserve=2, refill_to=10
    )
    data["supplies"].append(
        {
            "id": "health_potions",
            "sensor": "health_potions",
            "return_below": 6,
            "emergency_reserve": 2,
            "refill_to": 10,
            "optional": False,
        }
    )
    value = hunt_plan_from_data(data, route_values())
    feature = HuntCycleFeature(
        value,
        bindings(value),
        binding_revision="two-supply-layout-v1",
        map_region=(0, 0, 50, 50),
        battle_region=(0, 0, 50, 50),
        resume=False,
    )
    feature._supply_counts.update(mana_potions=5, health_potions=2)

    due, reason = feature._return_due()

    assert due
    assert reason == "emergency_reserve:health_potions"
    assert feature.status.return_mode is None  # status changes on the next public transition
    assert feature._return_mode == "now"
    feature.request_return(now=False)
    assert feature._return_mode == "now"


def test_warm_pause_uses_new_frame_without_relocalizing_live_navigation():
    feature = make_feature()
    feature._phase = CyclePhase.HUNT
    feature._inner = feature._new_inner(CyclePhase.HUNT)
    visual = {
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
        "detector_prepared": {"present": True, "position": (1, 1), "count": 1},
    }
    movement = feature.tick(context(feature, 1, 0.0, visual)).requests[0]
    feature.on_result(result(movement, 0.0))
    inner = feature._inner
    assert inner is not None
    assert inner._mode == "arriving"

    feature.on_pause()
    feature.on_resume(paused_for=2.0)
    same_frame = feature.tick(context(feature, 1, 2.0, visual))
    assert same_frame.requests == ()
    assert feature.status.reason == "resume_waiting_for_new_frame"
    assert feature._inner is inner
    assert inner._mode == "arriving"

    feature.tick(context(feature, 2, 2.2, visual))
    assert feature.status.reason == "resume_observations_reconciled"
    assert feature._inner is inner
    assert inner._mode == "arriving"


def test_warm_pause_during_combat_retains_combat_state_without_anchor_resume():
    value = plan()
    feature = HuntCycleFeature(
        value,
        bindings(value),
        binding_revision="combat-pause-layout-v1",
        targets=(TargetSpec(1, "dragon"),),
        map_region=(0, 0, 50, 50),
        battle_region=(0, 0, 50, 50),
        resume=False,
    )
    feature._phase = CyclePhase.HUNT
    feature._inner = feature._new_inner(CyclePhase.HUNT)
    visual = {
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
    }
    active = {"dragon": Target(1, None, True)}
    feature.tick(context(feature, 1, 0.0, visual, targets=active))
    inner = feature._inner
    assert inner is not None
    assert inner._mode == "combat"

    feature.on_pause()
    feature.on_resume(paused_for=3.0)
    feature.tick(context(feature, 2, 3.2, visual, targets=active))

    assert feature.status.reason == "resume_observations_reconciled"
    assert feature._inner is inner
    assert inner._mode == "combat"


def test_checkpoint_route_recovery_polls_then_accepts_explicit_anchor(tmp_path):
    two_hunt = Route(
        "hunt_route",
        (Waypoint("hunt-a", "Sword"), Waypoint("hunt-b", "Star")),
    )
    data = hunt_plan_template()
    data.pop("loot")
    data["supplies"][0].update(return_below=6, emergency_reserve=2, refill_to=10)
    value = hunt_plan_from_data(
        data, {**route_values(), "hunt_route": two_hunt}
    )
    checkpoint = Path(tmp_path) / "anchors.state.json"
    original = HuntCycleFeature(
        value,
        bindings(value),
        binding_revision="anchor-layout-v1",
        map_region=(0, 0, 50, 50),
        battle_region=(0, 0, 50, 50),
        checkpoint_path=checkpoint,
        resume=False,
    )
    original._phase = CyclePhase.HUNT
    original._save_checkpoint()
    resumed = HuntCycleFeature(
        value,
        bindings(value),
        binding_revision="anchor-layout-v1",
        map_region=(0, 0, 50, 50),
        battle_region=(0, 0, 50, 50),
        checkpoint_path=checkpoint,
        resume=True,
    )
    visual = {
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
    }

    resumed.tick(context(resumed, 1, 0.0, visual))
    resumed.tick(context(resumed, 2, 0.2, visual, reached=True))
    assert resumed.status.reason == "route_anchor_ambiguous_select_candidate"
    assert resumed.status.recovery_candidates == (0, 1)
    assert not resumed.status.manual_required

    assert not resumed.select_recovery_anchor(99)
    assert resumed.status.reason == "route_anchor_selection_rejected"
    assert resumed.status.recovery_candidates == (0, 1)
    assert resumed.request_recheck()
    assert resumed.status.reason == "recovery_recheck_requested"
    assert resumed.select_recovery_anchor(1)
    resumed.tick(context(resumed, 3, 0.4, visual, reached=True))
    assert resumed.status.reason == "resume_observations_reconciled"
    assert resumed._inner is not None
    assert resumed._inner.index == 1


def test_checkpoint_route_recovery_keeps_polling_until_unique_anchor_appears(
    tmp_path,
):
    original = make_feature(tmp_path, resume=False)
    original._phase = CyclePhase.HUNT
    original._save_checkpoint()
    resumed = make_feature(tmp_path, resume=True)
    visual = {
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
    }

    resumed.on_pause()
    resumed.on_resume(paused_for=1.0)
    assert resumed.status.recovery_reason == "checkpoint_fresh_observation_required"
    resumed.tick(context(resumed, 1, 0.0, visual))
    resumed.tick(context(resumed, 2, 0.2, visual, reached=False))
    assert resumed.status.reason == "route_anchor_waiting_for_fresh_evidence"
    assert not resumed.status.manual_required

    resumed.tick(context(resumed, 3, 0.4, visual, reached=True))
    assert resumed.status.reason == "resume_observations_reconciled"
    assert resumed._inner is not None
    assert resumed._inner.index == 0


def test_manual_recheck_without_pending_effect_reports_actionable_status():
    feature = make_feature()
    feature._manual("operator_decision_required")
    assert feature.acknowledge_manual(operator_reconciled=False)

    feature.tick(context(feature, 1, 0.0, {}))

    assert feature.status.reason == "manual_recheck_has_no_machine_reconcilable_effect"
    assert feature.status.manual_required


def test_interrupted_loot_transfer_keeps_both_deltas_for_manual_recheck(tmp_path):
    feature = make_feature(tmp_path, loot=True, resume=False)
    feature._phase = CyclePhase.HUNT
    feature._looting = True
    feature._loot_opened = True
    visual = {
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
        "detector_prepared": {"present": True, "position": (1, 1), "count": 1},
        "detector_loot_corpse": {"present": True, "position": (5, 5), "count": 1},
        "detector_loot_container": {"present": True, "position": (6, 6), "count": 1},
        "detector_loot_backpack": {"present": True, "position": (10, 10), "count": 1},
        "detector_loot_free_slots": {"present": True, "position": (10, 10), "count": 5},
        "detector_loot_backpack_items": {"present": True, "position": (10, 10), "count": 20},
        "detector_gold_coin": {"present": True, "position": (7, 7), "count": 100},
    }
    feature.tick(context(feature, 1, 0.0, visual))

    resumed = make_feature(tmp_path, loot=True, resume=True)
    resumed.tick(context(resumed, 2, 0.2, visual))
    assert resumed.status.manual_reason == "interrupted_loot_transfer_requires_reconciliation"
    assert resumed.acknowledge_manual(operator_reconciled=False)
    visual["detector_gold_coin"]["count"] = 90
    visual["detector_loot_backpack_items"]["count"] = 30
    resumed.tick(context(resumed, 3, 0.4, visual))
    assert resumed.status.reason == "machine_confirmed_after_manual_check"
    assert resumed._loot_moves == 1


def test_immediate_return_cancels_queued_outer_loot_but_reconciles_dispatched():
    visual = {
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
        "detector_prepared": {"present": True, "position": (1, 1), "count": 1},
        "detector_loot_corpse": {"present": True, "position": (5, 5), "count": 1},
        "detector_loot_container": {"present": True, "position": (6, 6), "count": 1},
        "detector_loot_backpack": {"present": True, "position": (10, 10), "count": 1},
        "detector_loot_free_slots": {"present": True, "position": (10, 10), "count": 5},
        "detector_loot_backpack_items": {"present": True, "position": (10, 10), "count": 20},
        "detector_gold_coin": {"present": True, "position": (7, 7), "count": 100},
    }
    queued = make_feature(loot=True)
    queued._phase = CyclePhase.HUNT
    queued._looting = True
    queued._loot_opened = True
    request = queued.tick(context(queued, 1, 0.0, visual)).requests[0]

    visual["detector_mana_potions"]["count"] = 0
    cancelled = queued.tick(context(queued, 2, 0.1, visual))

    assert cancelled.cancel_keys == (request.key,)
    assert queued.status.return_mode == "now"
    assert queued._pending is None
    assert not queued._looting
    assert queued.status.reason == "immediate_return_cancelled_queued_loot"

    dispatched = make_feature(loot=True)
    dispatched._phase = CyclePhase.HUNT
    dispatched._looting = True
    dispatched._loot_opened = True
    visual["detector_mana_potions"]["count"] = 8
    request = dispatched.tick(context(dispatched, 3, 0.2, visual)).requests[0]
    dispatched.on_result(result(request, 0.2))
    visual["detector_mana_potions"]["count"] = 0

    waiting = dispatched.tick(context(dispatched, 4, 0.4, visual))

    assert waiting.requests == ()
    assert dispatched.status.return_mode == "now"
    assert dispatched._pending is not None
    visual["detector_gold_coin"]["count"] = 90
    visual["detector_loot_backpack_items"]["count"] = 30

    reconciled = dispatched.tick(context(dispatched, 5, 0.6, visual))

    assert reconciled.cancel_keys == ()
    assert dispatched._pending is None
    assert dispatched._loot_moves == 1
    assert dispatched.status.reason == "machine_confirmed"


def test_immediate_return_suppresses_only_fresh_unchanged_loot_retry():
    visual = {
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
        "detector_loot_corpse": {"present": True, "position": (5, 5), "count": 1},
        "detector_loot_container": {"present": True, "position": (6, 6), "count": 1},
        "detector_loot_backpack": {"present": True, "position": (10, 10), "count": 1},
        "detector_loot_free_slots": {"present": True, "position": (10, 10), "count": 5},
        "detector_loot_backpack_items": {"present": True, "position": (10, 10), "count": 20},
        "detector_gold_coin": {"present": True, "position": (7, 7), "count": 100},
    }
    feature = make_feature(loot=True)
    feature._phase = CyclePhase.HUNT
    feature._looting = True
    feature._loot_opened = True
    request = feature.tick(context(feature, 1, 0.0, visual)).requests[0]
    feature.on_result(result(request, 0.0))
    feature.request_return(now=True)
    assert feature._pending is not None
    deadline = feature._pending.deadline
    assert deadline is not None

    suppressed = feature.tick(context(feature, 2, deadline + 0.01, visual))

    assert suppressed.requests == ()
    assert feature._pending is None
    assert not feature._looting
    assert feature.status.reason == "immediate_return_suppressed_loot_retry"

    unknown = make_feature(loot=True)
    unknown._phase = CyclePhase.HUNT
    unknown._looting = True
    unknown._loot_opened = True
    request = unknown.tick(context(unknown, 3, 0.2, visual)).requests[0]
    unknown.on_result(result(request, 0.2))
    unknown.request_return(now=True)
    assert unknown._pending is not None
    deadline = unknown._pending.deadline
    assert deadline is not None

    unresolved = unknown.tick(
        context(
            unknown,
            4,
            deadline + 0.01,
            visual,
            unknown=("detector_gold_coin",),
        )
    )

    assert unresolved.requests == ()
    assert unknown._pending is not None
    assert unknown.status.manual_required
    assert unknown.status.manual_reason == "confirmation_unknown_after_timeout"


def test_feature_runs_only_through_one_shared_client_session():
    class Clock:
        value = 0.0

        def monotonic(self):
            return self.value

    class Adapter:
        def __init__(self, clock):
            self.clock = clock
            self.prepared = 0
            self.captures = 0
            self.actions = []

        def ready(self):
            return True

        def observe(self, sensors, token):
            token.checkpoint()
            self.captures += 1
            readings = {}
            for sensor in sensors:
                if sensor.kind is SensorKind.VISUAL:
                    count = self.prepared if sensor.name == "detector_prepared" else 8
                    readings[sensor] = Reading({"present": True, "position": (1, 1), "count": count})
                elif sensor.kind is SensorKind.MARKER:
                    readings[sensor] = Reading(Marker((10, 10), False))
            return ObservationBatch(readings, self.clock.value)

        def execute(self, action, token):
            token.checkpoint()
            self.actions.append(action)
            if action.key == "F1":
                self.prepared += 1

        def close(self):
            pass

    clock = Clock()
    adapter = Adapter(clock)
    feature = make_feature()
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(feature)
    session.tick_once()
    clock.value = 0.2
    session.tick_once()

    assert [action.key for action in adapter.actions] == ["F1"]
    assert adapter.captures == 2
    assert feature.status.reason == "machine_confirmed"
    session.close()


def test_shared_session_completes_cycle_with_priority_arbitration_and_no_duplicate_writes():
    routes = route_values()
    data = hunt_plan_template()
    data.pop("loot")
    data["supplies"][0].update(
        return_below=6, emergency_reserve=2, refill_to=10
    )
    data["actions"] = {
        "prepare": [{
            "id": "prepare_supply",
            "kind": "press",
            "key": "F1",
            "confirmation": {
                "sensor": "prepared", "field": "count", "op": "increased"
            },
        }],
        "deposit": [{
            "id": "deposit_inventory",
            "kind": "press",
            "key": "F3",
            "confirmation": {
                "sensor": "deposited", "field": "count", "op": "increased"
            },
        }],
        "sell": [{
            "id": "sell_inventory",
            "kind": "press",
            "key": "F4",
            "confirmation": {
                "sensor": "sold", "field": "count", "op": "increased"
            },
        }],
        "refill": [{
            "id": "buy_supply",
            "kind": "press",
            "key": "F2",
            "confirmation": {
                "sensor": "mana_potions", "field": "count", "op": "increased"
            },
        }],
    }
    cycle_plan = hunt_plan_from_data(data, routes)

    class Clock:
        value = 0.0

        def monotonic(self):
            return self.value

    class Adapter:
        points = {"Flag": (10, 10), "Sword": (20, 20), "Money": (30, 30)}

        def __init__(self, clock):
            self.clock = clock
            self.counts = {
                "mana_potions": 8,
                "prepared": 0,
                "deposited": 0,
                "sold": 0,
            }
            self.reached = set()
            self.actions = []
            self.transitions = []

        def ready(self):
            return True

        def observe(self, sensors, token):
            token.checkpoint()
            readings = {}
            for sensor in sensors:
                if sensor.kind is SensorKind.VISUAL:
                    sensor_id = sensor.name.removeprefix("detector_")
                    readings[sensor] = Reading({
                        "present": True,
                        "position": (1, 1),
                        "count": self.counts.get(sensor_id, 0),
                    })
                elif sensor.kind is SensorKind.MARKER:
                    point = self.points[sensor.name]
                    readings[sensor] = Reading(Marker(point, point in self.reached))
                else:
                    readings[sensor] = Reading(None)
            return ObservationBatch(readings, self.clock.value)

        def execute(self, action, token):
            token.checkpoint()
            identity = action.key if action.kind is ActionKind.PRESS else action.point
            self.actions.append(identity)
            if action.kind is ActionKind.LEFT_CLICK:
                self.reached.add(action.point)
                if action.point == self.points["Sword"]:
                    before = self.counts["mana_potions"]
                    self.counts["mana_potions"] = 5
                    self.transitions.append(("mana_potions", before, 5))
            elif action.key == "F1":
                before = self.counts["prepared"]
                self.counts["prepared"] += 1
                self.transitions.append(("prepared", before, self.counts["prepared"]))
            elif action.key == "F3":
                before = self.counts["deposited"]
                self.counts["deposited"] += 1
                self.transitions.append(("deposited", before, self.counts["deposited"]))
            elif action.key == "F4":
                before = self.counts["sold"]
                self.counts["sold"] += 1
                self.transitions.append(("sold", before, self.counts["sold"]))
            elif action.key == "F2":
                before = self.counts["mana_potions"]
                self.counts["mana_potions"] += 2
                self.transitions.append(
                    ("mana_potions", before, self.counts["mana_potions"])
                )

        def close(self):
            pass

    class OneShotSupport:
        name = "Support"
        interval = 0.2

        def __init__(self):
            self.issued = False
            self.results = []

        def sensors(self):
            return ()

        def tick(self, context):
            if self.issued:
                return Step(phase="Support idle")
            self.issued = True
            return Step((Request(
                "support:once",
                Action(ActionKind.PRESS, key="F9"),
                priority=Priority.SUPPORT,
                reason="one-shot support arbitration",
            ),), phase="Support requested")

        def on_result(self, result):
            self.results.append(result)

    clock = Clock()
    adapter = Adapter(clock)
    feature = HuntCycleFeature(
        cycle_plan,
        bindings(cycle_plan),
        binding_revision="full-cycle-layout-v1",
        map_region=(0, 0, 50, 50),
        battle_region=(0, 0, 50, 50),
        resume=False,
    )
    support = OneShotSupport()
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(feature)
    session.start_feature(support)

    for _ in range(100):
        session.tick_once()
        if adapter.actions.count(Adapter.points["Flag"]) == 2:
            break
        clock.value += 0.2
    else:
        pytest.fail(f"full cycle did not reach next outbound: {feature.inspection_status()}")

    assert adapter.actions == [
        "F9", "F1", (10, 10), (20, 20), (30, 30),
        "F3", "F4", "F2", "F2", "F2", (10, 10),
    ]
    assert adapter.transitions == [
        ("prepared", 0, 1),
        ("mana_potions", 8, 5),
        ("deposited", 0, 1),
        ("sold", 0, 1),
        ("mana_potions", 5, 7),
        ("mana_potions", 7, 9),
        ("mana_potions", 9, 11),
    ]
    assert adapter.counts == {
        "mana_potions": 11, "prepared": 1, "deposited": 1, "sold": 1,
    }
    assert feature.status.cycle_count == 1
    assert feature.status.phase == "outbound"
    assert not feature.status.manual_required
    assert not feature.failed
    assert [(item.key, item.state) for item in support.results] == [
        ("support:once", ResultState.EXECUTED)
    ]
    executed = [
        event.data["result"]
        for event in session.events()
        if event.kind == "result"
        and event.data["result"].state is ResultState.EXECUTED
    ]
    assert len(executed) == len(adapter.actions)
    assert {item.owner for item in executed} == {"CaveBot", "Support"}
    session.close()


def test_stop_hook_prevents_further_requests():
    feature = make_feature()
    visual = {
        "detector_prepared": {"present": True, "position": (1, 1), "count": 0},
        "detector_mana_potions": {"present": True, "position": (1, 1), "count": 8},
    }
    feature.on_stop()
    assert feature.tick(context(feature, 1, 0.0, visual)).requests == ()
    assert feature.status.reason == "stopped"

