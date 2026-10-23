from __future__ import annotations

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


class _Clock:
    value = 0.0

    def monotonic(self):
        return self.value


class _Adapter:
    def __init__(self, clock: _Clock, *, block_ready_call: int | None = None):
        self.clock = clock
        self.block_ready_call = block_ready_call
        self.ready_calls = 0
        self.ready_entered = threading.Event()
        self.ready_release = threading.Event()
        self.actions = []

    def ready(self):
        self.ready_calls += 1
        if self.ready_calls == self.block_ready_call:
            self.ready_entered.set()
            assert self.ready_release.wait(2)
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
                readings[sensor] = Reading(None)  # Already following; isolate command/dispatch ordering.
        return ObservationBatch(readings, self.clock.value)

    def execute(self, action, token):
        token.checkpoint()
        self.actions.append(action)

    def close(self):
        pass


class _Support:
    name = "Support"
    interval = 0.2

    def sensors(self):
        return ()

    def tick(self, context):
        return Step(
            (
                Request(
                    "support:once",
                    Action(ActionKind.PRESS, key="F9"),
                    Priority.HEAL,
                    reason="leave the lower-priority combat request queued",
                ),
            ),
            phase="Support",
        )

    def on_result(self, result):
        pass


class _ObservableTickLock:
    """Signal when the command waits after publishing its dispatch hold."""

    def __init__(self):
        self._lock = threading.Lock()
        self.command_waiting = threading.Event()

    def acquire(self, blocking=True, timeout=-1):
        if threading.current_thread().name == "immediate-return-command":
            self.command_waiting.set()
        return self._lock.acquire(blocking, timeout)

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.release()


def _session(*, block_ready_call: int | None = None):
    routes = {
        "outbound_route": Route("outbound", (Waypoint("out", "Flag"),)),
        "hunt_route": Route("hunt", (Waypoint("hunt", "Sword"),)),
        "return_route": Route("return", (Waypoint("back", "Money"),)),
    }
    data = hunt_plan_template()
    data.pop("loot")
    plan = hunt_plan_from_data(data, routes)
    bindings = {
        sensor_id: Sensor(
            SensorKind.VISUAL, (0, 0, 20, 20), f"detector_{sensor_id}"
        )
        for sensor_id in plan.sensor_ids()
    }
    clock = _Clock()
    adapter = _Adapter(clock, block_ready_call=block_ready_call)
    hunt = HuntCycleFeature(
        plan,
        bindings,
        binding_revision="immediate-command-race-v1",
        targets=(TargetSpec(1, "dragon"),),
        map_region=(0, 0, 100, 100),
        battle_region=(0, 0, 100, 100),
        resume=False,
    )
    hunt._phase = CyclePhase.HUNT
    hunt._inner = hunt._new_inner(CyclePhase.HUNT)
    session = ClientSession(adapter, clock=clock, autostart=False)
    generation = session.start_feature(hunt)
    session.start_feature(_Support())
    return session, generation, hunt, adapter, clock


def _select_results(session: ClientSession):
    return [
        event.data["result"]
        for event in session.events()
        if event.kind == "result"
        and event.data["result"].owner == "CaveBot"
        and event.data["result"].key.startswith("select:")
    ]


def test_immediate_command_blocks_stale_dispatch_from_tick_already_in_progress():
    # Calls 1 and 2 serve the first tick and its Support dispatch. Call 3 blocks
    # the next tick before it can pop the queued combat selection.
    session, generation, hunt, adapter, clock = _session(block_ready_call=3)
    observable_lock = _ObservableTickLock()
    session._tick_lock = observable_lock
    thread_errors = []
    command_results = []

    def scheduler_tick():
        try:
            session.tick_once()
        except BaseException as exc:
            thread_errors.append(exc)

    def request_return():
        try:
            command_results.append(
                session.feature_command(
                    "CaveBot", generation, "request_return", now=True
                )
            )
        except BaseException as exc:
            thread_errors.append(exc)

    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F9"]

    clock.value = 0.1
    scheduler = threading.Thread(target=scheduler_tick, name="blocked-scheduler-tick")
    command = threading.Thread(target=request_return, name="immediate-return-command")
    scheduler.start()
    try:
        assert adapter.ready_entered.wait(2)
        command.start()
        # The command reaches the tick lock only after incrementing the record's
        # dispatch hold, so releasing readiness here is deterministic.
        assert observable_lock.command_waiting.wait(2)
    finally:
        adapter.ready_release.set()
        scheduler.join(2)
        if command.ident is not None:
            command.join(2)

    assert not scheduler.is_alive()
    assert not command.is_alive()
    assert thread_errors == []
    assert command_results == [True]
    assert [action.key for action in adapter.actions] == ["F9"]
    assert [(result.key, result.state) for result in _select_results(session)] == [
        ("select:1", ResultState.REJECTED)
    ]
    assert session._records["CaveBot"].command_dispatch_holds == 0

    # The accepted command is immediately due and the scheduler remains usable.
    session.tick_once()
    assert hunt.status.phase == "return_before"
    assert [action.key for action in adapter.actions] == ["F9"]
    session.close()


def test_immediate_command_callback_failure_releases_dispatch_hold():
    session, generation, hunt, adapter, clock = _session()
    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F9"]

    def fail_return(*, now=False):
        raise RuntimeError("command callback failed")

    hunt.request_return = fail_return
    with pytest.raises(RuntimeError, match="command callback failed"):
        session.feature_command("CaveBot", generation, "request_return", now=True)
    assert session._records["CaveBot"].command_dispatch_holds == 0

    # The failed command did not leave a permanent dispatch barrier.
    clock.value = 0.1
    session.tick_once()
    assert [action.kind for action in adapter.actions] == [
        ActionKind.PRESS,
        ActionKind.LEFT_CLICK,
    ]
    assert [(result.key, result.state) for result in _select_results(session)] == [
        ("select:1", ResultState.EXECUTED)
    ]
    session.close()
