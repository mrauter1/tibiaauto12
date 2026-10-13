import copy

import pytest

from engine.CaveBot.routes import Route, Waypoint
from tibia_client.hunt_plan import (
    ConditionOp,
    HuntPlanError,
    hunt_plan_from_data,
    hunt_plan_template,
)


def routes():
    return {
        "outbound_route": Route("outbound_route", (Waypoint("out", "Flag"),)),
        "hunt_route": Route("hunt_route", (Waypoint("hunt", "Sword"),)),
        "return_route": Route("return_route", (Waypoint("back", "Money"),)),
    }


def document():
    value = hunt_plan_template()
    value["travel"]["outbound"][0]["before"] = [
        {
            "id": "open_door",
            "kind": "right_click",
            "target": "door",
            "require_present": ["door"],
            "confirmation": {
                "sensor": "door_open",
                "field": "present",
                "op": "eq",
                "value": True,
            },
        }
    ]
    value["actions"]["refill"] = [
        {
            "id": "buy_mana",
            "kind": "press",
            "key": "F3",
            "confirmation": {
                "sensor": "mana_potions",
                "field": "count",
                "op": "increased",
            },
        }
    ]
    return value


def test_strict_plan_round_trip_and_route_fingerprint():
    plan = hunt_plan_from_data(document(), routes())

    assert plan.outbound[0].before[0].id == "open_door"
    assert plan.refill[0].confirmation.op is ConditionOp.INCREASED
    assert plan.max_refill_rounds == 10
    assert hunt_plan_from_data(plan.as_dict(), routes()).as_dict() == plan.as_dict()

    changed = dict(routes())
    changed["hunt_route"] = Route("hunt_route", (Waypoint("different", "Sword"),))
    assert hunt_plan_from_data(document(), changed).fingerprint != plan.fingerprint


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda value: value.update({"surprise": True}), "unknown field"),
        (lambda value: value["travel"]["outbound"][0].pop("after"), "missing field"),
        (lambda value: value["supplies"][0].update({"return_below": 5}), "thresholds"),
        (
            lambda value: value["actions"]["refill"][0]["confirmation"].update(
                {"value": 4}
            ),
            "relative conditions",
        ),
        (lambda value: value["loot"].pop("destination_count"), "missing field"),
        (
            lambda value: value["loot"].update(destination_count="gold_coin"),
            "must differ",
        ),
    ],
)
def test_invalid_or_open_ended_plan_fails_closed(change, message):
    value = copy.deepcopy(document())
    change(value)
    with pytest.raises(HuntPlanError, match=message):
        hunt_plan_from_data(value, routes())


def test_plan_requires_loaded_routes_and_unique_action_ids():
    value = document()
    value["travel"]["return"][0]["after"] = [copy.deepcopy(value["actions"]["refill"][0])]
    with pytest.raises(HuntPlanError, match="action ids must be unique"):
        hunt_plan_from_data(value, routes())

    value = document()
    value["hunt_route"] = "not_loaded"
    with pytest.raises(HuntPlanError, match="does not name a loaded route"):
        hunt_plan_from_data(value, routes())

