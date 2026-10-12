"""Readiness loss must invalidate decisions and require fresh observations."""

from tibia_client import (
    Action, ActionKind, ClientSession, Confirmation, ObservationBatch,
    Reading, Request, ResultState, Sensor, SensorKind, Step,
)


HEALTH = Sensor(SensorKind.HEALTH)


class Clock:
    value = 10.0

    def monotonic(self):
        return self.value


class Adapter:
    def __init__(self, clock):
        self.clock = clock
        self.available = True
        self.ready_calls = 0
        self.frames = 0
        self.actions = []

    def ready(self):
        self.ready_calls += 1
        return self.available

    def observe(self, sensors, token):
        token.checkpoint()
        self.frames += 1
        return ObservationBatch({s: Reading(self.frames) for s in sensors}, self.clock.value)

    def execute(self, action, token):
        token.checkpoint()
        assert self.available, "dispatch bypassed readiness"
        self.actions.append(action)

    def close(self):
        pass


class Feature:
    name = "readiness-test"
    interval = 10.0

    def __init__(self, first_step=Step()):
        self.first_step = first_step
        self.contexts = []
        self.results = []

    def sensors(self):
        return (HEALTH,)

    def tick(self, context):
        self.contexts.append(context)
        return self.first_step if len(self.contexts) == 1 else Step()

    def on_result(self, result):
        self.results.append(result)


def press(key, **options):
    return Request(key, Action(ActionKind.PRESS, key=key), **options)


def test_focus_loss_rejects_unbound_queue_and_recovery_observes_before_next_interval():
    clock = Clock()
    adapter = Adapter(clock)
    feature = Feature(Step((press("F1"), press("F2"))))
    with ClientSession(adapter, clock=clock, autostart=False) as session:
        session.start_feature(feature)
        first = session.tick_once()
        assert [action.key for action in adapter.actions] == ["F1"]

        adapter.available = False
        clock.value += 0.01
        waiting = session.tick_once()
        assert waiting.epoch == first.epoch + 1
        assert not waiting.observations
        assert adapter.frames == 1
        assert session.feature_status(feature.name).phase == "Waiting for game focus"
        assert [(r.key, r.state) for r in feature.results] == [
            ("F1", ResultState.EXECUTED), ("F2", ResultState.REJECTED),
        ]

        clock.value += 0.01
        session.tick_once()
        waiting_events = [e for e in session.events() if e.data.get("reason") == "not ready"]
        assert len(waiting_events) == 1
        assert adapter.frames == 1

        adapter.available = True
        clock.value += 0.01
        recovered = session.tick_once()
        assert len(feature.contexts) == 2  # Ten-second interval has not elapsed.
        assert recovered.require(HEALTH, now=clock.value) == 2
        assert recovered.epoch == waiting.epoch
        assert [action.key for action in adapter.actions] == ["F1"]
        assert session.is_active(feature.name)


def test_loss_after_decision_is_checked_before_native_dispatch():
    clock = Clock()
    adapter = Adapter(clock)

    class LosingFocus(Feature):
        def tick(self, context):
            step = super().tick(context)
            adapter.available = False
            return step

    feature = LosingFocus(Step((press("F1"),)))
    with ClientSession(adapter, clock=clock, autostart=False) as session:
        session.start_feature(feature)
        session.tick_once()
        assert not adapter.actions
        assert feature.results[0].state is ResultState.REJECTED
        assert session.feature_status(feature.name).phase == "Waiting for game focus"
        assert session.is_active(feature.name)


def test_slow_final_readiness_check_cannot_rejuvenate_expired_request():
    clock = Clock()

    class SlowReady(Adapter):
        def ready(self):
            ready = super().ready()
            if self.ready_calls == 2:
                clock.value += 1.0
            return ready

    adapter = SlowReady(clock)
    feature = Feature(Step((press("F1", ttl=0.5),)))
    with ClientSession(adapter, clock=clock, autostart=False) as session:
        session.start_feature(feature)
        session.tick_once()
        assert not adapter.actions
        assert feature.results[0].state is ResultState.REJECTED
        assert "ttl" in feature.results[0].reason


def test_focus_is_checked_after_dispatch_recorder_returns():
    clock = Clock()
    adapter = Adapter(clock)
    feature = Feature(Step((press("F1"),)))

    def recorder(event):
        if event.kind == "dispatch":
            adapter.available = False

    with ClientSession(adapter, clock=clock, recorder=recorder, autostart=False) as session:
        session.start_feature(feature)
        session.tick_once()
        assert not adapter.actions
        assert feature.results[0].state is ResultState.REJECTED
        assert session.feature_status(feature.name).phase == "Waiting for game focus"


def test_readiness_loss_terminalizes_executed_but_unconfirmed_request():
    clock = Clock()
    adapter = Adapter(clock)
    feature = Feature(Step((press("F1", confirmation=Confirmation(HEALTH, 2)),)))
    with ClientSession(adapter, clock=clock, autostart=False) as session:
        session.start_feature(feature)
        session.tick_once()
        assert [(r.state, r.final) for r in feature.results] == [(ResultState.EXECUTED, False)]
        adapter.available = False
        clock.value += 0.01
        session.tick_once()
        assert [(r.state, r.final) for r in feature.results] == [
            (ResultState.EXECUTED, False), (ResultState.FAILED, True),
        ]
        assert "before confirmation" in feature.results[-1].reason
        adapter.available = True
        clock.value += 0.01
        session.tick_once()
        assert len(feature.results) == 2
        assert len(adapter.actions) == 1


def test_each_focus_loss_gets_a_new_bounded_recovery_window():
    clock = Clock()
    adapter = Adapter(clock)
    feature = Feature()
    with ClientSession(adapter, clock=clock, ready_timeout=1.0, autostart=False) as session:
        session.start_feature(feature)
        session.tick_once()
        for _ in range(2):
            clock.value += 100
            adapter.available = False
            session.tick_once()
            assert session.is_active(feature.name)
            clock.value += 0.5
            adapter.available = True
            session.tick_once()
            assert session.is_active(feature.name)
        clock.value += 100
        adapter.available = False
        session.tick_once()
        clock.value += 1.0
        session.tick_once()
        assert not session.is_active(feature.name)
        assert "within 1 seconds" in session.feature_status(feature.name).error
