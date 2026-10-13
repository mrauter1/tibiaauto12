from __future__ import annotations

import threading
from types import MappingProxyType

import pytest

from core import runtime
from tibia_client import (
    Action,
    ActionKind,
    ClientSession,
    Confirmation,
    ObservationBatch,
    Priority,
    Quality,
    Reading,
    Request,
    Requirement,
    ResultState,
    Sensor,
    SensorKind,
    Step,
)


HEALTH = Sensor(SensorKind.HEALTH)
RING = Sensor(SensorKind.RING_EMPTY)


class Clock:
    def __init__(self) -> None:
        self.value = 10.0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Adapter:
    def __init__(self, clock: Clock, readings=None, ready=None) -> None:
        self.clock = clock
        self.readings = list(readings or [{HEALTH: Reading(20), RING: Reading(True)}])
        self.ready_values = list(ready or [True])
        self.observed = []
        self.actions = []
        self.closed = 0
        self.observe_error = None
        self.execute_error = None

    def ready(self):
        if len(self.ready_values) > 1:
            return self.ready_values.pop(0)
        return self.ready_values[0]

    def observe(self, sensors, token):
        token.checkpoint()
        self.observed.append(tuple(sensors))
        if self.observe_error:
            raise self.observe_error
        values = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        return ObservationBatch({sensor: values[sensor] for sensor in sensors if sensor in values}, self.clock.monotonic())

    def execute(self, action, token):
        token.checkpoint()
        if self.execute_error:
            raise self.execute_error
        self.actions.append(action)
        token.checkpoint()

    def close(self):
        self.closed += 1


class Feature:
    interval = 0.1

    def __init__(self, name, sensors=(), steps=()) -> None:
        self.name = name
        self._sensors = tuple(sensors)
        self.steps = list(steps)
        self.results = []
        self.contexts = []

    def sensors(self):
        return self._sensors

    def tick(self, context):
        self.contexts.append(context)
        if len(self.steps) > 1:
            step = self.steps.pop(0)
        elif self.steps:
            step = self.steps[0]
        else:
            step = Step()
        if isinstance(step, BaseException):
            raise step
        return step

    def on_result(self, result):
        self.results.append(result)


def observed_press(key, priority, request_key):
    return Request(
        request_key,
        Action(ActionKind.PRESS, key=key),
        priority=priority,
        requirements=(Requirement(HEALTH),),
    )


def test_due_features_share_one_snapshot_and_missing_reading_is_unknown():
    clock = Clock()
    adapter = Adapter(clock, readings=[{HEALTH: Reading(45)}])
    first = Feature("first", (HEALTH, RING))
    second = Feature("second", (HEALTH,))
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(first)
    session.start_feature(second)

    snapshot = session.tick_once()

    assert len(adapter.observed) == 1
    assert set(adapter.observed[0]) == {HEALTH, RING}
    assert first.contexts[0].snapshot is second.contexts[0].snapshot is snapshot
    assert snapshot.observation(RING).quality is Quality.UNKNOWN
    assert snapshot.observation(HEALTH).observed_at == clock.value
    session.close()


def test_priority_dispatches_one_action_then_low_priority_regenerates_on_new_frame():
    clock = Clock()
    adapter = Adapter(clock)
    heal = Feature(
        "heal", (HEALTH,),
        (Step((observed_press("F1", Priority.HEAL, "heal"),)), Step()),
    )
    loot = Feature(
        "loot", (HEALTH,),
        (Step((observed_press("F2", Priority.LOOT, "loot"),)),),
    )
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(loot)
    session.start_feature(heal)

    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F1"]

    clock.advance(0.1)
    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F1", "F2"]
    assert any(
        result.state is ResultState.REJECTED and "superseded" in result.reason
        for result in loot.results
    )
    session.close()


def test_stale_bound_observation_is_rejected_before_dispatch():
    clock = Clock()
    adapter = Adapter(clock)
    two = Step((
        observed_press("F1", Priority.HEAL, "first"),
        observed_press("F2", Priority.BACKGROUND, "second"),
    ))
    owner = Feature("owner", (HEALTH,), (two, Step()))
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(owner)
    session.tick_once()
    clock.advance(0.1)
    session.tick_once()

    assert [action.key for action in adapter.actions] == ["F1"]
    rejected = [result for result in owner.results if result.key == "second"]
    assert len(rejected) == 1
    assert rejected[0].state is ResultState.REJECTED
    assert rejected[0].final
    session.close()


def test_unrelated_fast_sensor_refresh_retains_slow_observation_and_intent():
    clock = Clock()
    adapter = Adapter(clock)
    slow = Feature("slow", (HEALTH,), (Step((
        observed_press("F1", Priority.HEAL, "first"),
        observed_press("F2", Priority.BACKGROUND, "second"),
    )),))
    slow.interval = 10.0
    fast = Feature("fast", (RING,), (Step(),))
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(slow)
    session.start_feature(fast)
    first_snapshot = session.tick_once()
    health = first_snapshot.observation(HEALTH)

    clock.advance(0.1)
    second_snapshot = session.tick_once()

    assert second_snapshot.frame_id > health.frame_id
    assert second_snapshot.observation(HEALTH) is health
    assert second_snapshot.observation(HEALTH).observed_at == 10.0
    assert [action.key for action in adapter.actions] == ["F1", "F2"]
    assert not any(result.state is ResultState.REJECTED for result in slow.results)
    session.stop_feature("slow")
    assert HEALTH not in session.snapshot.observations
    session.close()


def test_stop_cancels_generation_queue_and_replacement_never_gets_old_results():
    clock = Clock()
    adapter = Adapter(clock)
    old = Feature("same", (HEALTH,), (Step((
        observed_press("F1", Priority.HEAL, "one"),
        observed_press("F2", Priority.BACKGROUND, "two"),
    )),))
    session = ClientSession(adapter, clock=clock, autostart=False)
    first_generation = session.start_feature(old)
    session.tick_once()
    assert session.stop_feature("same")

    replacement = Feature("same")
    second_generation = session.start_feature(replacement)
    clock.advance(0.1)
    session.tick_once()

    assert second_generation == first_generation + 1
    assert replacement.results == []
    terminal = [
        event.data["result"] for event in session.events()
        if event.kind == "result" and event.data["result"].key == "two"
    ]
    assert len(terminal) == 1
    assert terminal[0].state is ResultState.CANCELLED
    with pytest.raises(ValueError, match="cannot be reused"):
        session.start_feature(old)
    session.close()


def test_delayed_generation_stop_cannot_cancel_replacement():
    clock = Clock()
    adapter = Adapter(clock)
    session = ClientSession(adapter, clock=clock, autostart=False)
    first = Feature("same")
    first_generation = session.start_feature(first)
    release_stop = threading.Event()
    outcome = []

    def delayed_stop():
        assert release_stop.wait(1)
        outcome.append(
            session.stop_feature(
                "same", expected_generation=first_generation
            )
        )

    delayed = threading.Thread(target=delayed_stop)
    delayed.start()
    replacement = Feature("same")
    replacement_generation = session.start_feature(replacement)
    release_stop.set()
    delayed.join(1)

    assert not delayed.is_alive()
    assert outcome == [False]
    current = session._records["same"]
    assert current.feature is replacement
    assert current.active and not current.cancelled.is_set()
    assert session.stop_feature(
        "same", expected_generation=replacement_generation
    )
    assert not session.is_active("same")
    session.close()


def test_confirmation_samples_sensor_while_feature_is_not_due():
    clock = Clock()
    adapter = Adapter(clock, readings=[
        {HEALTH: Reading(20), RING: Reading(True)},
        {RING: Reading(False)},
    ])
    request = Request(
        "equip",
        Action(ActionKind.PRESS, key="F3"),
        priority=Priority.SUPPORT,
        requirements=(Requirement(RING),),
        confirmation=Confirmation(RING, False, timeout=1.0),
    )
    feature = Feature("ring", (RING,), (Step((request,)),))
    feature.interval = 10.0
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(feature)
    session.tick_once()
    assert [(r.state, r.final) for r in feature.results] == [(ResultState.EXECUTED, False)]

    clock.advance(0.1)
    session.tick_once()
    assert [(r.state, r.final) for r in feature.results] == [
        (ResultState.EXECUTED, False), (ResultState.CONFIRMED, True)
    ]
    assert adapter.observed[-1] == (RING,)
    session.close()


def test_confirmation_missing_field_fails_at_bounded_timeout():
    clock = Clock()
    adapter = Adapter(clock, readings=[{RING: Reading({"other": 1})}])
    request = Request(
        "confirm",
        Action(ActionKind.PRESS, key="F4"),
        confirmation=Confirmation(RING, 2, field="missing", timeout=0.2),
    )
    feature = Feature("confirm", (RING,), (Step((request,)),))
    feature.interval = 10.0
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(feature)
    session.tick_once()
    clock.advance(0.2)
    session.tick_once()
    assert feature.results[-1].state is ResultState.FAILED
    assert feature.results[-1].reason == "confirmation timed out"
    session.close()


def test_initial_readiness_blocks_observation_and_input_then_recovers():
    clock = Clock()
    adapter = Adapter(clock, ready=[False, False, True])
    feature = Feature("timer", (), (Step((Request("timer", Action(ActionKind.PRESS, key="F5")),)),))
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(feature)

    session.tick_once()
    assert session.feature_status("timer").phase == "Waiting for game focus"
    assert adapter.observed == [] and adapter.actions == []
    clock.advance(0.1)
    session.tick_once()
    assert adapter.actions == []
    clock.advance(0.1)
    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F5"]
    session.close()


def test_readiness_timeout_and_capture_error_fail_closed():
    clock = Clock()
    adapter = Adapter(clock, ready=[False])
    feature = Feature("wait", (HEALTH,))
    session = ClientSession(adapter, clock=clock, ready_timeout=0.2, autostart=False)
    session.start_feature(feature)
    session.tick_once()
    clock.advance(0.2)
    session.tick_once()
    assert session.feature_status("wait").phase == "Failed"
    assert adapter.closed == 0
    adapter.ready_values = [True]
    recovered = Feature(
        "recovered", (),
        (Step((Request("ready", Action(ActionKind.PRESS, key="F10")),)),),
    )
    session.start_feature(recovered)
    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F10"]
    session.close()

    second_adapter = Adapter(clock)
    second_adapter.observe_error = OSError("capture source lost")
    second = ClientSession(second_adapter, clock=clock, autostart=False)
    second.start_feature(Feature("capture", (HEALTH,)))
    second.tick_once()
    assert second.feature_status("capture").phase == "Failed"
    assert second_adapter.actions == [] and second_adapter.closed == 1


def test_feature_and_execute_failures_are_isolated_and_fail_closed():
    clock = Clock()
    adapter = Adapter(clock)
    broken = Feature("broken", (HEALTH,), (RuntimeError("bad policy"),))
    healthy = Feature("healthy", (), (Step((Request("ok", Action(ActionKind.PRESS, key="F6")),)),))
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(broken)
    session.start_feature(healthy)
    session.tick_once()
    assert session.feature_status("broken").phase == "Failed"
    assert [action.key for action in adapter.actions] == ["F6"]
    assert session.feature_status("healthy").active
    session.close()

    failing_adapter = Adapter(clock)
    failing_adapter.execute_error = OSError("focus lost")
    failing = Feature("input", (), (Step((Request("key", Action(ActionKind.PRESS, key="F7")),)),))
    failed_session = ClientSession(failing_adapter, clock=clock, autostart=False)
    failed_session.start_feature(failing)
    failed_session.tick_once()
    assert failing.results[-1].state is ResultState.FAILED
    assert failed_session.feature_status("input").phase == "Failed"
    failed_session.close()


def test_stop_cancels_action_specific_token_inside_execute():
    clock = Clock()
    began = threading.Event()
    release = threading.Event()

    class BlockingAdapter(Adapter):
        def execute(self, action, token):
            assert runtime.current_token() is token
            began.set()
            while not release.wait(0.002):
                token.checkpoint()

    adapter = BlockingAdapter(clock)
    feature = Feature("blocking", (), (Step((Request("key", Action(ActionKind.PRESS, key="F8")),)),))
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(feature)
    thread = threading.Thread(target=session.tick_once)
    thread.start()
    assert began.wait(1)
    assert session.stop_feature("blocking")
    thread.join(1)
    release.set()
    assert not thread.is_alive()
    assert adapter.actions == []
    session.close()


def test_recorder_failure_prevents_native_dispatch_and_history_is_bounded():
    clock = Clock()
    adapter = Adapter(clock)

    def broken_recorder(_event):
        raise OSError("disk full")

    feature = Feature("audit", (), (Step((Request("key", Action(ActionKind.PRESS, key="F9")),)),))
    session = ClientSession(
        adapter, clock=clock, recorder=broken_recorder,
        event_history=3, autostart=False,
    )
    session.start_feature(feature)
    session.tick_once()
    assert adapter.actions == []
    assert session.feature_status("audit").phase == "Failed"
    assert len(session.events()) <= 3
    session.close()


def test_close_is_idempotent_and_prevents_restart():
    clock = Clock()
    adapter = Adapter(clock)
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(Feature("close"))
    session.close()
    session.close()
    assert adapter.closed == 1
    with pytest.raises(RuntimeError, match="closed"):
        session.start_feature(Feature("late"))


def test_malformed_observation_after_adapter_return_fails_session_and_restart_is_descriptive():
    clock = Clock()

    class MalformedAdapter(Adapter):
        def observe(self, sensors, token):
            batch = object.__new__(ObservationBatch)
            object.__setattr__(batch, "readings", MappingProxyType({HEALTH: object()}))
            object.__setattr__(batch, "observed_at", self.clock.monotonic())
            object.__setattr__(batch, "frame", None)
            return batch

    adapter = MalformedAdapter(clock)
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(Feature("malformed", (HEALTH,)))
    session.tick_once()

    assert session.closed
    assert session.feature_status("malformed").phase == "Failed"
    assert adapter.closed == 1
    with pytest.raises(RuntimeError, match="reconfiguration.*non-Reading"):
        session.start_feature(Feature("restart"))


def test_dispatch_recorder_failure_terminalizes_popped_request_before_shutdown():
    clock = Clock()
    adapter = Adapter(clock)
    recorded = []

    def recorder(event):
        recorded.append(event)
        if event.kind == "dispatch":
            raise OSError("trace sink unavailable")

    feature = Feature(
        "trace", (),
        (Step((Request("press", Action(ActionKind.PRESS, key="F11")),)),),
    )
    session = ClientSession(adapter, clock=clock, recorder=recorder, autostart=False)
    session.start_feature(feature)
    session.tick_once()

    assert adapter.actions == []
    assert session.closed
    finals = [
        event.data["result"]
        for event in session.events()
        if event.kind == "result" and event.data["result"].key == "press"
        and event.data["result"].final
    ]
    assert len(finals) == 1
    assert finals[0].state is ResultState.FAILED
    assert feature.results == finals


def test_confirmation_comparison_error_stays_bounded_and_times_out():
    clock = Clock()

    class BadEquality:
        def __eq__(self, _other):
            raise RuntimeError("comparison unavailable")

    adapter = Adapter(clock, readings=[{RING: Reading(BadEquality())}])
    request = Request(
        "compare",
        Action(ActionKind.PRESS, key="F12"),
        confirmation=Confirmation(RING, True, timeout=0.1),
    )
    feature = Feature("compare", (RING,), (Step((request,)),))
    feature.interval = 10
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(feature)
    session.tick_once()
    clock.advance(0.1)
    session.tick_once()

    assert not session.closed
    assert feature.results[-1].state is ResultState.FAILED
    assert feature.results[-1].reason == "confirmation timed out"
    session.close()


def test_stop_and_restart_while_old_sensors_block_ignores_old_generation_step():
    clock = Clock()
    adapter = Adapter(clock)
    began = threading.Event()
    release = threading.Event()

    class BlockingFeature(Feature):
        def sensors(self):
            began.set()
            release.wait(1)
            return self._sensors

    old = BlockingFeature(
        "generation", (),
        (Step((Request("old", Action(ActionKind.PRESS, key="F1")),)),),
    )
    replacement = Feature(
        "generation", (),
        (Step((Request("new", Action(ActionKind.PRESS, key="F2")),)),),
    )
    session = ClientSession(adapter, clock=clock, autostart=False)
    old_generation = session.start_feature(old)
    thread = threading.Thread(target=session.tick_once)
    thread.start()
    assert began.wait(1)
    stopped = threading.Event()
    stop_result = []

    def stop():
        stop_result.append(session.stop_feature("generation"))
        stopped.set()

    stopper = threading.Thread(target=stop)
    stopper.start()
    assert not stopped.wait(0.05)
    release.set()
    stopper.join(1)
    assert stopped.is_set() and stop_result == [True]
    new_generation = session.start_feature(replacement)
    thread.join(1)

    assert not thread.is_alive()
    assert new_generation == old_generation + 1
    assert adapter.actions == []
    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F2"]
    assert old.results == [] and replacement.results[-1].state is ResultState.EXECUTED
    session.close()


def test_concurrent_runtime_and_ui_events_reach_recorder_in_sequence_order():
    clock = Clock()
    adapter = Adapter(clock)
    first_entered = threading.Event()
    release_first = threading.Event()
    later_entered = threading.Event()
    delivered = []

    class BlockingRecorder:
        def record(self, event):
            if event.sequence == 1:
                first_entered.set()
                release_first.wait(1)
            else:
                later_entered.set()
            delivered.append(event.sequence)

    session = ClientSession(
        adapter, clock=clock, recorder=BlockingRecorder(), autostart=False
    )
    feature = Feature("ordered")
    starter = threading.Thread(target=session.start_feature, args=(feature,))
    starter.start()
    assert first_entered.wait(1)

    ticker = threading.Thread(target=session.tick_once)
    ticker.start()
    assert not later_entered.wait(0.05)

    release_first.set()
    starter.join(1)
    ticker.join(1)
    assert not starter.is_alive() and not ticker.is_alive()
    session.close()

    assert delivered == list(range(1, len(delivered) + 1))


def test_stop_waits_for_inflight_result_callback_before_returning():
    clock = Clock()
    adapter = Adapter(clock)
    callback_entered = threading.Event()
    release_callback = threading.Event()
    callback_finished = threading.Event()

    class BlockingResultFeature(Feature):
        def on_result(self, result):
            callback_entered.set()
            release_callback.wait(1)
            self.results.append(result)
            callback_finished.set()

    old = BlockingResultFeature(
        "callback", (),
        (Step((Request("old", Action(ActionKind.PRESS, key="F1")),)),),
    )
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(old)
    ticker = threading.Thread(target=session.tick_once)
    ticker.start()
    assert callback_entered.wait(1)

    stop_finished = threading.Event()
    stopper = threading.Thread(
        target=lambda: (session.stop_feature("callback"), stop_finished.set())
    )
    stopper.start()
    assert not stop_finished.wait(0.05)
    release_callback.set()
    ticker.join(1)
    stopper.join(1)

    assert callback_finished.is_set() and stop_finished.is_set()
    replacement = Feature("callback")
    assert session.start_feature(replacement) == 2
    assert replacement.results == []
    session.close()


def test_result_event_records_delivery_withheld_by_concurrent_stop():
    clock = Clock()
    execute_entered = threading.Event()
    release_execute = threading.Event()
    executed = threading.Event()

    class SignalingAdapter(Adapter):
        def execute(self, action, token):
            execute_entered.set()
            assert release_execute.wait(1)
            super().execute(action, token)
            executed.set()

    adapter = SignalingAdapter(clock)
    feature = Feature(
        "result-race", (),
        (Step((Request("press", Action(ActionKind.PRESS, key="F1")),)),),
    )
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(feature)
    record = session._records["result-race"]
    ticker = threading.Thread(target=session.tick_once)
    ticker.start()
    assert execute_entered.wait(1)
    record.callback_lock.acquire()
    try:
        release_execute.set()
        assert executed.wait(1)

        stop_result = []
        stopper = threading.Thread(
            target=lambda: stop_result.append(session.stop_feature("result-race"))
        )
        stopper.start()
        assert record.cancelled.wait(1)
    finally:
        record.callback_lock.release()

    ticker.join(1)
    stopper.join(1)
    assert not ticker.is_alive() and not stopper.is_alive()
    assert stop_result == [True]
    assert feature.results == []
    result_events = [event for event in session.events() if event.kind == "result"]
    assert len(result_events) == 1
    assert result_events[0].data["deliver"] is False
    session.close()


def test_slow_feature_cannot_dispatch_with_expired_observation_or_ttl():
    clock = Clock()
    adapter = Adapter(clock)

    class SlowFeature(Feature):
        def tick(self, context):
            self.contexts.append(context)
            clock.advance(1.0)
            return Step((observed_press("F1", Priority.HEAL, "slow"),))

    feature = SlowFeature("slow-dispatch", (HEALTH,))
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(feature)
    session.tick_once()

    assert adapter.actions == []
    assert len(feature.results) == 1
    assert feature.results[0].state is ResultState.REJECTED
    assert "expired" in feature.results[0].reason or "stale" in feature.results[0].reason
    session.close()


def test_dispatch_recorder_delay_is_revalidated_before_adapter_execute():
    clock = Clock()
    adapter = Adapter(clock)

    def recorder(event):
        if event.kind == "dispatch":
            clock.advance(1.0)

    feature = Feature(
        "slow-recorder", (HEALTH,),
        (Step((observed_press("F1", Priority.HEAL, "delayed"),)),),
    )
    session = ClientSession(
        adapter, clock=clock, recorder=recorder, autostart=False
    )
    session.start_feature(feature)
    session.tick_once()

    assert adapter.actions == []
    assert len(feature.results) == 1
    assert feature.results[0].state is ResultState.REJECTED
    assert feature.results[0].final
    assert "expired" in feature.results[0].reason or "stale" in feature.results[0].reason
    session.close()


def test_stop_callback_quiescence_timeout_is_bounded_and_fail_closed():
    clock = Clock()
    adapter = Adapter(clock)
    entered = threading.Event()
    release = threading.Event()

    class StuckFeature(Feature):
        def sensors(self):
            entered.set()
            release.wait()
            return self._sensors

    feature = StuckFeature("stuck", ())
    session = ClientSession(adapter, clock=clock, autostart=False)
    session._CALLBACK_QUIESCE_TIMEOUT = 0.05
    session.start_feature(feature)
    ticker = threading.Thread(target=session.tick_once)
    ticker.start()
    assert entered.wait(1)

    with pytest.raises(TimeoutError, match="did not quiesce"):
        session.stop_feature("stuck")

    status = session.feature_status("stuck")
    assert not status.active
    assert status.phase == "Failed"
    assert status.error and "did not quiesce" in status.error
    assert adapter.actions == []
    release.set()
    ticker.join(1)
    assert not ticker.is_alive()
    session.close()


def test_explicit_close_propagates_adapter_failure_and_can_retry():
    clock = Clock()

    class RetryCloseAdapter(Adapter):
        def close(self):
            self.closed += 1
            if self.closed == 1:
                raise OSError("capture worker did not stop")

    adapter = RetryCloseAdapter(clock)
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(Feature("close-failure"))

    with pytest.raises(OSError, match="did not stop"):
        session.close()
    assert session.closed
    assert isinstance(session.close_error, OSError)
    with pytest.raises(RuntimeError, match="closed by caller"):
        session.start_feature(Feature("blocked-replacement"))

    session.close()
    assert adapter.closed == 2
    assert session.close_error is None


class FailableClock(Clock):
    def __init__(self) -> None:
        super().__init__()
        self.fail = False

    def monotonic(self) -> float:
        if self.fail:
            raise OSError("clock disappeared")
        return super().monotonic()


@pytest.mark.parametrize("stage", ["before observe", "after observe"])
def test_clock_failure_around_observe_fails_closed(stage):
    clock = FailableClock()

    class ClockFailingAdapter(Adapter):
        def observe(self, sensors, token):
            if stage == "before observe":
                clock.fail = True
                return super().observe(sensors, token)
            token.checkpoint()
            self.observed.append(tuple(sensors))
            return ObservationBatch({HEALTH: Reading(50)}, clock.value)

    def recorder(event):
        if stage == "after observe" and event.kind == "observation":
            clock.fail = True

    adapter = ClockFailingAdapter(clock)
    feature = Feature("clock-observe", (HEALTH,))
    session = ClientSession(
        adapter, clock=clock, recorder=recorder, autostart=False
    )
    session.start_feature(feature)

    session.tick_once()

    assert session.closed
    status = session.feature_status(feature.name)
    assert not status.active and status.phase == "Failed"
    assert status.error and "clock disappeared" in status.error
    assert adapter.closed == 1


@pytest.mark.parametrize("stage", ["before execute", "after execute"])
def test_clock_failure_around_execute_terminalizes_popped_request(stage):
    clock = FailableClock()

    class ClockFailingAdapter(Adapter):
        def execute(self, action, token):
            super().execute(action, token)
            if stage == "after execute":
                clock.fail = True

    adapter = ClockFailingAdapter(clock)

    def recorder(event):
        if stage == "before execute" and event.kind == "dispatch":
            clock.fail = True

    feature = Feature(
        "clock-execute", (),
        (Step((Request("press", Action(ActionKind.PRESS, key="F1")),)),),
    )
    session = ClientSession(
        adapter, clock=clock, recorder=recorder, autostart=False
    )
    session.start_feature(feature)

    session.tick_once()

    assert session.closed
    assert len(adapter.actions) == (1 if stage == "after execute" else 0)
    finals = [
        event.data["result"]
        for event in session.events()
        if event.kind == "result" and event.data["result"].final
    ]
    assert len(finals) == 1
    assert finals[0].state is ResultState.FAILED
    assert stage in finals[0].reason
    assert feature.results == finals


def test_clock_failure_after_finished_result_does_not_leave_feature_active():
    clock = FailableClock()
    adapter = Adapter(clock)

    class FinishingFeature(Feature):
        def on_result(self, result):
            super().on_result(result)
            clock.fail = True

    feature = FinishingFeature(
        "finish-clock", (),
        (Step(
            (Request("press", Action(ActionKind.PRESS, key="F4")),),
            finished=True,
        ),),
    )
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(feature)

    session.tick_once()

    assert adapter.actions == [Action(ActionKind.PRESS, key="F4")]
    assert feature.results[-1].state is ResultState.EXECUTED
    assert session.closed
    status = session.feature_status(feature.name)
    assert not status.active and status.phase == "Failed"
    assert status.error and "clock disappeared" in status.error


@pytest.mark.parametrize("operation", ["replace", "stop"])
def test_lifecycle_clock_failure_leaves_truthful_failed_status(operation):
    clock = FailableClock()
    adapter = Adapter(clock)
    feature = Feature("lifecycle-clock")
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(feature)
    clock.fail = True

    with pytest.raises(OSError, match="clock disappeared"):
        if operation == "replace":
            session.start_feature(Feature(feature.name))
        else:
            session.stop_feature(feature.name)

    assert session.closed
    status = session.feature_status(feature.name)
    assert not status.active and status.phase == "Failed"
    assert status.error and "clock disappeared" in status.error
    assert adapter.closed == 1


def test_autostart_clock_failure_stops_worker_with_truthful_status():
    clock = FailableClock()
    ready_entered = threading.Event()
    release_ready = threading.Event()
    adapter_closed = threading.Event()

    class BlockingReadyAdapter(Adapter):
        def ready(self):
            ready_entered.set()
            assert release_ready.wait(1)
            return True

        def close(self):
            super().close()
            adapter_closed.set()

    adapter = BlockingReadyAdapter(clock)
    feature = Feature("worker-clock")
    session = ClientSession(adapter, clock=clock)
    session.start_feature(feature)
    assert ready_entered.wait(1)
    clock.fail = True
    release_ready.set()
    assert adapter_closed.wait(1)
    worker = runtime.status(session._runtime_name)
    assert worker is not None
    worker["thread"].join(1)

    assert session.closed and not session.running
    status = session.feature_status(feature.name)
    assert not status.active and status.phase == "Failed"
    assert status.error and "clock disappeared" in status.error


@pytest.mark.parametrize("operation", ["replace", "stop", "close", "tick"])
def test_recorder_lifecycle_reentry_is_rejected_before_mutation(operation):
    clock = Clock()
    adapter = Adapter(clock)

    class Recorder:
        session = None

        def __init__(self):
            self.error = None

        def record(self, event):
            if event.kind != "result" or not event.data["deliver"]:
                return
            try:
                if operation == "replace":
                    self.session.start_feature(Feature("recorder-guard"))
                elif operation == "stop":
                    self.session.stop_feature("recorder-guard")
                elif operation == "close":
                    self.session.close()
                else:
                    self.session.tick_once()
            except RuntimeError as exc:
                self.error = exc

    recorder = Recorder()
    feature = Feature(
        "recorder-guard", (),
        (Step((Request("press", Action(ActionKind.PRESS, key="F2")),)),),
    )
    session = ClientSession(
        adapter, clock=clock, recorder=recorder, autostart=False
    )
    recorder.session = session
    session.start_feature(feature)

    session.tick_once()

    assert recorder.error is not None
    assert "observer-only" in str(recorder.error)
    assert feature.results[-1].state is ResultState.EXECUTED
    assert session.is_active(feature.name)
    assert not session.closed
    session.close()


def test_uncaught_recorder_reentry_preserves_delivery_truth_then_fails_closed():
    clock = Clock()
    adapter = Adapter(clock)
    order = []

    class ResultFeature(Feature):
        def on_result(self, result):
            order.append("on_result")
            super().on_result(result)

    class Recorder:
        session = None

        def record(self, event):
            if event.kind == "result" and event.data["deliver"]:
                order.append("recorder")
                self.session.stop_feature("recorder-fatal")

    recorder = Recorder()
    feature = ResultFeature(
        "recorder-fatal", (),
        (Step((Request("press", Action(ActionKind.PRESS, key="F3")),)),),
    )
    session = ClientSession(
        adapter, clock=clock, recorder=recorder, autostart=False
    )
    recorder.session = session
    session.start_feature(feature)

    session.tick_once()

    assert order == ["recorder", "on_result"]
    result_events = [event for event in session.events() if event.kind == "result"]
    assert len(result_events) == 1
    assert result_events[0].data["deliver"] is True
    assert feature.results == [result_events[0].data["result"]]
    assert session.closed
    status = session.feature_status(feature.name)
    assert not status.active and status.phase == "Failed"
    assert status.error and "observer-only" in status.error


def test_recorder_cancellation_exception_is_latched_as_audit_failure():
    clock = Clock()
    adapter = Adapter(clock)

    def recorder(event):
        if event.kind == "dispatch":
            raise runtime.CancelledError("recorder cancelled itself")

    feature = Feature(
        "recorder-cancel", (),
        (Step((Request("press", Action(ActionKind.PRESS, key="F5")),)),),
    )
    session = ClientSession(
        adapter, clock=clock, recorder=recorder, autostart=False
    )
    session.start_feature(feature)

    session.tick_once()

    assert adapter.actions == []
    assert session.closed
    assert len(feature.results) == 1
    assert feature.results[0].final
    assert feature.results[0].state is ResultState.FAILED
    assert session.feature_status(feature.name).phase == "Failed"


def test_record_frame_uses_same_observer_only_recorder_contract():
    clock = Clock()

    class FrameAdapter(Adapter):
        def observe(self, sensors, token):
            return ObservationBatch({HEALTH: Reading(50)}, clock.value, frame=object())

    class Recorder:
        session = None

        def __init__(self):
            self.error = None

        def record(self, _event):
            pass

        def record_frame(self, _frame_id, _frame):
            try:
                self.session.stop_feature("frame-recorder")
            except RuntimeError as exc:
                self.error = exc

    recorder = Recorder()
    adapter = FrameAdapter(clock)
    feature = Feature("frame-recorder", (HEALTH,))
    session = ClientSession(
        adapter, clock=clock, recorder=recorder, autostart=False
    )
    recorder.session = session
    session.start_feature(feature)

    session.tick_once()

    assert recorder.error is not None
    assert "observer-only" in str(recorder.error)
    assert session.is_active(feature.name)
    session.close()
