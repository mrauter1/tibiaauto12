"""Observation subscribers share the scheduler without acquiring input ownership."""
import threading

import pytest

from core import runtime
from tibia_client.models import (
    ObservationBatch, Reading, Sensor, SensorKind, Step, TransientObservationError,
)
from tibia_client.session import ClientSession


HEALTH = Sensor(SensorKind.HEALTH, (1, 1, 10, 2))
MANA = Sensor(SensorKind.MANA, (1, 3, 10, 4))


class Clock:
    value = 10.0

    def monotonic(self):
        return self.value


class Adapter:
    availability_policy = "on_error"

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.activations = 0
        self.actions = []
        self.closed = 0
        self.error = None
        self.entered = self.release = None
        self.frames = []

    def activate_input(self):
        self.activations += 1

    def ready(self):
        return True

    def observe(self, sensors, token):
        self.calls.append(sensors)
        at = self.clock.monotonic()
        frame = object()
        self.frames.append(frame)
        if self.entered:
            self.entered.set()
            assert self.release.wait(3)
        if self.error:
            raise self.error
        return ObservationBatch({sensor: Reading(len(self.calls)) for sensor in sensors}, at, frame)

    def execute(self, action, token):
        self.actions.append(action)

    def close(self):
        self.closed += 1

    def configure_visual_profile(self, profile):
        pass


class Feature:
    name = "heal"
    interval = 0.1

    def __init__(self):
        self.snapshots = []

    def sensors(self):
        return (HEALTH,)

    def tick(self, context):
        self.snapshots.append(context.snapshot)
        return Step()

    def on_result(self, result):
        pass


@pytest.fixture
def setup():
    clock = Clock()
    adapter = Adapter(clock)
    session = ClientSession(adapter, clock=clock, autostart=False)
    yield clock, adapter, session
    session.close()


def test_observer_and_automation_share_one_capture_and_observation(setup):
    clock, adapter, session = setup
    feature = Feature()
    session.start_feature(feature)
    handle = session.subscribe_observations((HEALTH, MANA, HEALTH))
    assert not adapter.calls
    snapshot = session.tick_once()
    view = session.observation_view(handle)
    assert adapter.calls == [(HEALTH, MANA)]
    assert adapter.activations == 1  # The feature, never the subscriber.
    assert view.snapshot.observation(HEALTH) is feature.snapshots[0].observation(HEALTH)
    assert view.snapshot.observation(HEALTH) is snapshot.observation(HEALTH)
    assert view.frame is adapter.frames[0]
    assert view.snapshot.frame_id == 1
    assert not adapter.actions
    session.unsubscribe_observations(handle)
    assert session.active_features == (feature.name,)
    assert adapter.closed == 0
    clock.value += 0.2
    session.tick_once()
    assert adapter.calls[-1] == (HEALTH,)


def test_observation_only_start_is_lazy_and_stop_removes_capture_demand(setup):
    clock, adapter, session = setup
    assert not session.running
    session.tick_once()
    assert not adapter.calls
    handle = session.subscribe_observations((HEALTH,), interval=0.1)
    session.tick_once()
    assert len(adapter.calls) == 1
    assert adapter.activations == 0
    assert session.active_features == ()
    for _ in range(4):
        clock.value += 0.01
        session.tick_once()
    assert len(adapter.calls) == 1
    assert session.unsubscribe_observations(handle)
    assert not session.unsubscribe_observations(handle)
    assert session.observation_view(handle) is None
    clock.value += 1
    session.tick_once()
    assert len(adapter.calls) == 1
    assert session.snapshot.observations == {}


def test_observer_view_retains_its_frame_while_faster_feature_updates(setup):
    clock, adapter, session = setup
    session.start_feature(Feature())
    handle = session.subscribe_observations((HEALTH,), interval=1)
    session.tick_once()
    first = session.observation_view(handle)
    clock.value += 0.2
    session.tick_once()
    assert session.snapshot.frame_id == 2
    assert session.observation_view(handle) is first
    assert first.frame is adapter.frames[0]
    assert first.snapshot.observation(HEALTH).observed_at == 10


@pytest.mark.parametrize("operation", ["stop_all", "close", "reconfigure"])
def test_lifecycle_invalidates_subscriber_views(setup, operation):
    _, adapter, session = setup
    handle = session.subscribe_observations((HEALTH,))
    session.tick_once()
    if operation == "reconfigure":
        session.configure_visual_profile({})
    else:
        getattr(session, operation)()
    assert session.observation_view(handle) is None
    session.tick_once()
    assert len(adapter.calls) == 1


def test_transient_failure_clears_view_and_recovers_without_restamping_old_frame(setup):
    clock, adapter, session = setup
    handle = session.subscribe_observations((HEALTH,))
    session.tick_once()
    clock.value += 0.2
    adapter.error = TransientObservationError("capture unavailable")
    session.tick_once()
    assert session.observation_view(handle).frame is None
    assert not session.observation_view(handle).snapshot.observations
    assert not session.closed
    adapter.error = None
    clock.value += 0.1
    session.tick_once()
    assert len(adapter.calls) == 2
    clock.value += 0.2
    session.tick_once()
    view = session.observation_view(handle)
    assert view.frame is adapter.frames[-1]
    assert view.snapshot.observation(HEALTH).observed_at == clock.value


def test_fatal_capture_failure_invalidates_subscription(setup):
    _, adapter, session = setup
    handle = session.subscribe_observations((HEALTH,))
    adapter.error = ValueError("bad capture geometry")
    session.tick_once()
    assert session.closed
    assert session.observation_view(handle) is None
    assert adapter.activations == 0


@pytest.mark.parametrize("stop_all", [False, True])
def test_late_capture_cannot_publish_into_replacement_subscription(setup, stop_all):
    clock, adapter, session = setup
    old = session.subscribe_observations((HEALTH,))
    adapter.entered, adapter.release = threading.Event(), threading.Event()
    worker = threading.Thread(target=session.tick_once)
    worker.start()
    try:
        assert adapter.entered.wait(2)
        if stop_all:
            session.stop_all()
        else:
            session.unsubscribe_observations(old)
        new = session.subscribe_observations((HEALTH,))
    finally:
        adapter.release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert session.observation_view(old) is None
    assert session.observation_view(new).frame is None
    assert not session.observation_view(new).snapshot.observations
    clock.value += 0.2
    session.tick_once()
    assert session.observation_view(new).frame is adapter.frames[-1]


def test_default_autostart_uses_the_same_runtime_without_input(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "start", lambda name, callback: calls.append((name, callback)))
    clock = Clock()
    adapter = Adapter(clock)
    with ClientSession(adapter, clock=clock) as session:
        assert not calls
        handle = session.subscribe_observations((HEALTH,))
        assert calls == [(session._runtime_name, session._run)]
        assert adapter.activations == 0
        session.unsubscribe_observations(handle)


@pytest.mark.parametrize("error", [RuntimeError("old failure"),
                                 TransientObservationError("old temporary failure")])
def test_obsolete_capture_failure_cannot_invalidate_restart(setup, error):
    _, adapter, session = setup
    session.subscribe_observations((HEALTH,))
    adapter.entered, adapter.release = threading.Event(), threading.Event()
    adapter.error = error
    worker = threading.Thread(target=session.tick_once)
    worker.start()
    try:
        assert adapter.entered.wait(2)
        session.stop_all()
        new = session.subscribe_observations((HEALTH,))
        view = session.observation_view(new)
    finally:
        adapter.release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not session.closed
    assert session.observation_view(new) is view
    assert not session.diagnostics().failure_reasons


@pytest.mark.parametrize("sensors, interval", [((), .1), ([HEALTH], .1), ((HEALTH,), 0),
                                             ((HEALTH,), float("nan")), ((True,), .1)])
def test_invalid_subscription_does_not_capture_or_activate(setup, sensors, interval):
    _, adapter, session = setup
    with pytest.raises((TypeError, ValueError)):
        session.subscribe_observations(sensors, interval=interval)
    session.tick_once()
    assert not adapter.calls and not adapter.activations
