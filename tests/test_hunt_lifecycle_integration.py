import threading

import pytest

from engine.CaveBot.routes import Route, Waypoint
from features.cavebot import TargetSpec
from features.hunt_cycle import CyclePhase, HuntCycleFeature
from tibia_client import ClientSession
from tibia_client.hunt_plan import hunt_plan_from_data, hunt_plan_template
from tibia_client.models import (
    Action,
    ActionKind,
    Marker,
    ObservationBatch,
    Priority,
    Reading,
    Request,
    ResultState,
    Sensor,
    SensorKind,
    Step,
    Target,
)


@pytest.mark.parametrize("next_tick", [0.1, 0.2])
def test_immediate_hunt_return_prevents_combat_dispatch_while_support_runs(
    next_tick,
):
    routes = {
        "outbound_route": Route("outbound", (Waypoint("out", "Flag"),)),
        "hunt_route": Route("hunt", (Waypoint("hunt", "Sword"),)),
        "return_route": Route("return", (Waypoint("back", "Money"),)),
    }
    data = hunt_plan_template()
    data.pop("loot")
    cycle_plan = hunt_plan_from_data(data, routes)
    bindings = {
        sensor_id: Sensor(
            SensorKind.VISUAL, (0, 0, 20, 20), f"detector_{sensor_id}"
        )
        for sensor_id in cycle_plan.sensor_ids()
    }

    class Clock:
        value = 0.0

        def monotonic(self):
            return self.value

    class Adapter:
        def __init__(self, clock):
            self.clock = clock
            self.actions = []

        def ready(self):
            return True

        def observe(self, sensors, token):
            token.checkpoint()
            readings = {}
            for sensor in sensors:
                if sensor.kind is SensorKind.VISUAL:
                    readings[sensor] = Reading(
                        {"present": True, "position": (1, 1), "count": 100}
                    )
                elif sensor.kind is SensorKind.MARKER:
                    readings[sensor] = Reading(Marker((10, 10), False))
                elif sensor.kind is SensorKind.TARGET:
                    readings[sensor] = Reading(Target(1, (50, 60), False))
                elif sensor.kind is SensorKind.FOLLOW:
                    readings[sensor] = Reading((5, 5))
            return ObservationBatch(readings, self.clock.value)

        def execute(self, action, token):
            token.checkpoint()
            self.actions.append(action)

        def close(self):
            pass

    class Support:
        name = "Support"
        interval = 0.2

        def __init__(self):
            self.sequence = 0

        def sensors(self):
            return ()

        def tick(self, context):
            self.sequence += 1
            return Step(
                (
                    Request(
                        f"support:{self.sequence}",
                        Action(ActionKind.PRESS, key=f"F{8 + self.sequence}"),
                        Priority.HEAL,
                        reason="support remains active during hunt drain",
                    ),
                ),
                phase="Support",
            )

        def on_result(self, result):
            pass

    clock = Clock()
    adapter = Adapter(clock)
    hunt = HuntCycleFeature(
        cycle_plan,
        bindings,
        binding_revision="lifecycle-integration-v1",
        targets=(TargetSpec(1, "dragon"),),
        map_region=(0, 0, 100, 100),
        battle_region=(0, 0, 100, 100),
        resume=False,
    )
    hunt._phase = CyclePhase.HUNT
    hunt._inner = hunt._new_inner(CyclePhase.HUNT)
    session = ClientSession(adapter, clock=clock, autostart=False)
    generation = session.start_feature(hunt)
    session.start_feature(Support())

    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F9"]
    assert session.feature_command(
        "CaveBot", generation, "request_return", now=True
    )
    clock.value = next_tick
    session.tick_once()

    expected = ["F9"] if next_tick < 0.2 else ["F9", "F10"]
    assert [action.key for action in adapter.actions] == expected
    assert hunt.status.phase == "return_before"
    combat_results = [
        event.data["result"]
        for event in session.events()
        if event.kind == "result"
        and event.data["result"].owner == "CaveBot"
        and event.data["result"].key.startswith("select:")
    ]
    assert len(combat_results) == 1
    assert combat_results[0].state in (ResultState.CANCELLED, ResultState.REJECTED)
    session.close()


def _prepare_feature():
    routes = {
        "outbound_route": Route("outbound", (Waypoint("out", "Flag"),)),
        "hunt_route": Route("hunt", (Waypoint("hunt", "Sword"),)),
        "return_route": Route("return", (Waypoint("back", "Money"),)),
    }
    data = hunt_plan_template()
    data.pop("loot")
    data["actions"]["prepare"] = [
        {
            "id": "prepare_supply",
            "kind": "press",
            "key": "F1",
            "confirmation": {
                "sensor": "prepared",
                "field": "count",
                "op": "increased",
            },
        }
    ]
    cycle_plan = hunt_plan_from_data(data, routes)
    bindings = {
        sensor_id: Sensor(
            SensorKind.VISUAL, (0, 0, 20, 20), f"detector_{sensor_id}"
        )
        for sensor_id in cycle_plan.sensor_ids()
    }
    return HuntCycleFeature(
        cycle_plan,
        bindings,
        binding_revision="pause-integration-v1",
        map_region=(0, 0, 100, 100),
        battle_region=(0, 0, 100, 100),
        resume=False,
    )


class _Clock:
    value = 0.0

    def monotonic(self):
        return self.value


class _PrepareAdapter:
    def __init__(self, clock, *, block_execute=False):
        self.clock = clock
        self.prepared = 0
        self.actions = []
        self.block_execute = block_execute
        self.started = threading.Event()
        self.release = threading.Event()

    def ready(self):
        return True

    def observe(self, sensors, token):
        token.checkpoint()
        readings = {}
        for sensor in sensors:
            if sensor.kind is SensorKind.VISUAL:
                count = self.prepared if sensor.name == "detector_prepared" else 100
                readings[sensor] = Reading(
                    {"present": True, "position": (1, 1), "count": count}
                )
            elif sensor.kind is SensorKind.MARKER:
                readings[sensor] = Reading(Marker((10, 10), False))
        return ObservationBatch(readings, self.clock.value)

    def execute(self, action, token):
        self.actions.append(action)
        if action.key == "F1":
            self.prepared += 1
        if self.block_execute:
            self.started.set()
            assert self.release.wait(2)

    def close(self):
        pass


class _OneShotSupport:
    name = "Support"
    interval = 0.2

    def __init__(self):
        self.issued = False

    def sensors(self):
        return ()

    def tick(self, context):
        if self.issued:
            return Step(phase="Support")
        self.issued = True
        return Step(
            (
                Request(
                    "support:once",
                    Action(ActionKind.PRESS, key="F9"),
                    Priority.HEAL,
                    reason="leave prepare queued for pause",
                ),
            ),
            phase="Support",
        )

    def on_result(self, result):
        pass


def test_pause_proven_queued_guarded_action_resumes_with_a_fresh_baseline():
    clock = _Clock()
    adapter = _PrepareAdapter(clock)
    hunt = _prepare_feature()
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(hunt)
    session.start_feature(_OneShotSupport())

    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F9"]
    assert hunt._pending is not None
    assert session.pause_navigation()
    assert hunt._pending is None
    clock.value = 1.0
    assert session.resume_navigation()
    session.tick_once()  # fresh warm-resume observation
    clock.value = 1.2
    session.tick_once()  # dispatch a newly guarded prepare action
    clock.value = 1.4
    session.tick_once()  # reconcile its observed effect

    assert [action.key for action in adapter.actions].count("F1") == 1
    assert hunt._pending is None
    assert not hunt.status.manual_required
    session.close()


def test_pause_during_inflight_mutation_preserves_baseline_and_never_repeats():
    clock = _Clock()
    adapter = _PrepareAdapter(clock, block_execute=True)
    hunt = _prepare_feature()
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(hunt)
    worker = threading.Thread(target=session.tick_once)
    worker.start()
    assert adapter.started.wait(2)

    assert session.pause_navigation()
    pending = hunt._pending
    assert pending is not None
    assert pending.dispatch_unknown
    adapter.release.set()
    worker.join(2)
    assert not worker.is_alive()

    clock.value = 1.0
    assert session.resume_navigation()
    session.tick_once()  # fresh warm-resume observation
    clock.value = 1.2
    session.tick_once()  # reconcile the effect from the suppressed result

    assert [action.key for action in adapter.actions].count("F1") == 1
    assert hunt._pending is None
    assert hunt.status.reason == "machine_confirmed_after_uncertain_dispatch"
    assert not hunt.status.manual_required
    session.close()

