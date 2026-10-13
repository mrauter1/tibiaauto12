from __future__ import annotations

from dataclasses import FrozenInstanceError
import threading

import pytest
import numpy as np

from core.perception import Frame

from tibia_client import (
    Action,
    ActionKind,
    ClientSession,
    ObservationBatch,
    Reading,
    Request,
    ResultState,
    Step,
)
from tibia_client.compatibility import ProbeFeature


class Clock:
    def __init__(self) -> None:
        self.value = 10.0

    def monotonic(self) -> float:
        return self.value


class Adapter:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.actions: list[Action] = []
        self.closed = False

    def ready(self) -> bool:
        return True

    def observe(self, sensors, token):
        token.checkpoint()
        return ObservationBatch({}, self.clock.value)

    def execute(self, action, token) -> None:
        token.checkpoint()
        self.actions.append(action)

    def close(self) -> None:
        self.closed = True


class Feature:
    interval = 0.01

    def __init__(self, name: str, keys: tuple[str, ...], required=()) -> None:
        self.name = name
        self.keys = list(keys)
        self.results = []
        self.required = tuple(required)

    def required_operations(self):
        return self.required

    def sensors(self):
        return ()

    def tick(self, _context):
        if not self.keys:
            return Step()
        requests = tuple(
            Request(key, Action(ActionKind.PRESS, key=key)) for key in self.keys
        )
        self.keys.clear()
        return Step(requests)

    def on_result(self, result):
        self.results.append(result)


def test_stop_all_invalidates_queued_work_but_keeps_adapter_for_explicit_restart():
    clock = Clock()
    adapter = Adapter(clock)
    session = ClientSession(adapter, clock=clock, autostart=False)
    old = Feature("old", ("F1", "F2"))
    session.start_feature(old)
    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F1"]

    assert session.stop_all() == ("old",)
    clock.value += 1
    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F1"]
    assert old.results[-1].state is ResultState.EXECUTED
    assert not adapter.closed

    new = Feature("new", ("F3",))
    session.start_feature(new)
    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F1", "F3"]
    assert session.snapshot.epoch >= 2
    session.close()


def test_stop_all_cancels_inflight_generation_and_never_dispatches_its_remainder():
    clock = Clock()
    entered = threading.Event()
    release = threading.Event()

    class BlockingAdapter(Adapter):
        def execute(self, action, token):
            entered.set()
            assert release.wait(1)
            self.actions.append(action)

    adapter = BlockingAdapter(clock)
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(Feature("old", ("F1", "F2")))
    worker = threading.Thread(target=session.tick_once)
    worker.start()
    assert entered.wait(1)

    assert session.stop_all() == ("old",)
    release.set()
    worker.join(1)
    assert not worker.is_alive()
    clock.value += 1
    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F1"]
    assert not session.is_active("old")
    session.close()


def test_concurrent_stop_all_keeps_start_latched_until_owner_finishes():
    clock = Clock()
    adapter = Adapter(clock)
    entered = threading.Event()
    release = threading.Event()

    class BlockingFeature(Feature):
        def tick(self, context):
            entered.set()
            assert release.wait(1)
            return super().tick(context)

    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(BlockingFeature("old", ("F1",)))
    tick = threading.Thread(target=session.tick_once)
    tick.start()
    assert entered.wait(1)
    stopped = []
    first = threading.Thread(target=lambda: stopped.append(session.stop_all()))
    first.start()
    while not session._stopping_all:
        pass

    assert session.stop_all() == ()
    with pytest.raises(RuntimeError, match="stop all"):
        session.start_feature(Feature("new", ("F2",)))
    release.set()
    tick.join(1)
    first.join(1)
    assert stopped == [("old",)]
    assert not adapter.actions
    session.close()


def test_replacement_started_before_stop_all_cannot_install_after_it_returns():
    clock = Clock()
    adapter = Adapter(clock)
    session = ClientSession(adapter, clock=clock, autostart=False)
    old = Feature("same", ("F1",))
    stale_replacement = Feature("same", ("F2",))
    explicit_restart = Feature("same", ("F3",))
    session.start_feature(old)
    old_record = session._records["same"]
    outcome = []

    old_record.callback_lock.acquire()
    try:
        def replace():
            try:
                outcome.append(session.start_feature(stale_replacement))
            except Exception as exc:
                outcome.append(exc)

        worker = threading.Thread(target=replace)
        worker.start()
        assert old_record.cancelled.wait(1)

        # RLock ownership reproduces the window where Stop All can quiesce the
        # old callback while the replacement remains blocked outside lifecycle.
        assert session.stop_all() == ("same",)
        assert not session.is_active("same")
        # This generation begins after Stop All and must survive the older
        # replacement when that thread is allowed to continue.
        assert session.start_feature(explicit_restart) == 2
    finally:
        old_record.callback_lock.release()

    worker.join(1)
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], RuntimeError)
    assert "stop all" in str(outcome[0])
    assert session.is_active("same")
    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F3"]
    session.close()


def test_profile_invalidation_is_rechecked_before_later_dispatch():
    clock = Clock()
    adapter = Adapter(clock)
    allowed = {"value": True}

    class Check:
        def __init__(self, ok):
            self.allowed = ok
            self.reasons = () if ok else ("calibration binding changed",)

    session = ClientSession(
        adapter,
        clock=clock,
        activation_guard=lambda _feature: Check(allowed["value"]),
        autostart=False,
    )
    feature = Feature("guarded", ("F1", "F2"), ("press",))
    session.start_feature(feature)
    session.tick_once()
    allowed["value"] = False
    clock.value += 0.01
    session.tick_once()

    assert [action.key for action in adapter.actions] == ["F1"]
    status = session.feature_status("guarded")
    assert not status.active
    assert "calibration binding changed" in status.error
    assert session.diagnostics().profile_reasons == ("calibration binding changed",)
    session.close()


def test_pause_navigation_keeps_support_running_and_resume_is_fresh():
    clock = Clock()
    adapter = Adapter(clock)

    class NavigationFeature(Feature):
        def __init__(self):
            super().__init__("CaveBot", ("F1",))
            self.pause_called = False
            self.paused_for = None

        # A generic zero-argument hook remains valid.
        def on_pause(self):
            self.pause_called = True

        def on_resume(self, *, paused_for):
            self.paused_for = paused_for

    cavebot = NavigationFeature()
    support = Feature("AutoHeal", ("F2",))
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(cavebot)
    session.start_feature(support)

    assert session.pause_navigation()
    assert cavebot.pause_called
    assert session.diagnostics().paused_features == ("CaveBot",)
    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F2"]

    clock.value += 0.01
    assert session.resume_navigation()
    assert cavebot.paused_for == pytest.approx(0.01)
    session.tick_once()
    assert [action.key for action in adapter.actions] == ["F2", "F1"]
    assert session.feature_status("CaveBot").active
    session.close()


@pytest.mark.parametrize("hook", ("pause", "resume"))
def test_stop_all_finishes_when_navigation_hook_does_not_return(hook):
    clock = Clock()
    entered = threading.Event()
    release = threading.Event()

    class HookFeature(Feature):
        def on_pause(self):
            if hook == "pause":
                entered.set()
                assert release.wait(3)

        def on_resume(self):
            if hook == "resume":
                entered.set()
                assert release.wait(3)

    session = ClientSession(Adapter(clock), clock=clock, autostart=False)
    session.start_feature(HookFeature("CaveBot", ("F1",)))
    if hook == "resume":
        assert session.pause_navigation()
        transition = threading.Thread(target=session.resume_navigation)
    else:
        transition = threading.Thread(target=session.pause_navigation)
    transition.start()
    assert entered.wait(1)

    stopped = threading.Event()
    result = []

    def stop():
        result.append(session.stop_all())
        stopped.set()

    stopper = threading.Thread(target=stop)
    stopper.start()
    assert stopped.wait(2), "Stop All waited indefinitely for a feature hook"
    assert result == [("CaveBot",)]
    assert not session.is_active("CaveBot")

    release.set()
    transition.join(1)
    stopper.join(1)
    session.close()


def test_diagnostics_are_immutable_and_do_not_claim_source_freshness():
    clock = Clock()
    session = ClientSession(Adapter(clock), clock=clock, autostart=False)
    session.start_feature(Feature("one", ("F1",)))
    session.tick_once()
    clock.value += 2
    diagnostics = session.diagnostics()

    assert diagnostics.capture.source_freshness == "unverified"
    # Sensorless policies still receive a capture-health heartbeat before input.
    assert diagnostics.capture.captured_age == 2
    assert diagnostics.capture.transport_ok is True
    assert diagnostics.last_action.operation == "press"
    assert diagnostics.last_result.state == "executed"
    assert session.probe_evidence_source() == "simulation"
    with pytest.raises(FrozenInstanceError):
        diagnostics.ready = False
    session.close()


def test_probe_bypasses_profile_evidence_but_is_strictly_exclusive():
    clock = Clock()

    class ProbeAdapter(Adapter):
        last_cursor_transition = ((1, 2), (1, 2))

        def observe(self, sensors, token):
            token.checkpoint()
            return ObservationBatch(
                {sensor: Reading(True) for sensor in sensors}, self.clock.value
            )

    adapter = ProbeAdapter(clock)
    session = ClientSession(
        adapter,
        clock=clock,
        activation_guard=lambda _feature: "profile deliberately unavailable",
        autostart=False,
    )
    probe = ProbeFeature(
        "left_click", Action(ActionKind.LEFT_CLICK, point=(4, 5))
    )
    session.start_probe(probe)
    with pytest.raises(RuntimeError, match="probe is active"):
        session.start_feature(Feature("ordinary", ("F1",)))
    with pytest.raises(RuntimeError, match="all automation"):
        session.start_probe(ProbeFeature("press", Action(ActionKind.PRESS, key="F2")))

    session.tick_once()
    assert adapter.actions == [Action(ActionKind.LEFT_CLICK, point=(4, 5))]
    assert probe.result is not None and probe.result.state is ResultState.EXECUTED
    assert probe.cursor_original == probe.cursor_final == (1, 2)
    session.stop_all()
    with pytest.raises(RuntimeError, match="profile deliberately unavailable"):
        session.start_feature(Feature("ordinary", ("F1",)))
    session.close()


def test_probe_metadata_getter_failure_does_not_rewrite_executed_action():
    clock = Clock()

    class TelemetryAdapter(Adapter):
        @property
        def last_cursor_transition(self):
            raise OSError("cursor API unavailable")

    adapter = TelemetryAdapter(clock)
    session = ClientSession(adapter, clock=clock, autostart=False)
    probe = ProbeFeature("press", Action(ActionKind.PRESS, key="F1"))
    session.start_probe(probe)

    session.tick_once()

    assert adapter.actions == [Action(ActionKind.PRESS, key="F1")]
    assert probe.result is not None and probe.result.state is ResultState.EXECUTED
    assert session.feature_status(probe.name).phase != "Failed"
    assert any(
        "cursor API unavailable" in reason
        for reason in session.diagnostics().failure_reasons
    )
    session.close()


def test_probe_metadata_callback_failure_does_not_rewrite_executed_action():
    clock = Clock()

    class TelemetryAdapter(Adapter):
        last_cursor_transition = ((1, 2), (1, 2))

    adapter = TelemetryAdapter(clock)
    session = ClientSession(adapter, clock=clock, autostart=False)
    probe = ProbeFeature("press", Action(ActionKind.PRESS, key="F1"))

    def broken_metadata(**_metadata):
        raise ValueError("malformed optional telemetry")

    probe.on_dispatch_metadata = broken_metadata
    session.start_probe(probe)

    session.tick_once()

    assert adapter.actions == [Action(ActionKind.PRESS, key="F1")]
    assert probe.result is not None and probe.result.state is ResultState.EXECUTED
    assert session.feature_status(probe.name).phase != "Failed"
    assert any(
        "malformed optional telemetry" in reason
        for reason in session.diagnostics().failure_reasons
    )
    session.close()


def test_capture_only_probe_populates_bounded_preview_and_stop_invalidates_it():
    clock = Clock()
    frame = Frame(np.ones((3, 4, 3), dtype=np.uint8))

    class PreviewAdapter(Adapter):
        def observe(self, sensors, token):
            token.checkpoint()
            return ObservationBatch(
                {sensor: Reading(True) for sensor in sensors},
                self.clock.value,
                frame,
            )

    session = ClientSession(PreviewAdapter(clock), clock=clock, autostart=False)
    probe = ProbeFeature(None, None)
    session.start_probe(probe)
    session.tick_once()

    assert session.preview_frame() is frame
    assert not session.is_active("CompatibilityProbe")
    session.stop_all()
    assert session.preview_frame() is None
    session.close()


def test_probe_context_token_is_rechecked_before_capture_or_dispatch():
    clock = Clock()
    allowed = {"value": True}

    class CountingAdapter(Adapter):
        def __init__(self, clock):
            super().__init__(clock)
            self.observations = 0

        def observe(self, sensors, token):
            self.observations += 1
            return ObservationBatch(
                {sensor: Reading(True) for sensor in sensors}, self.clock.value
            )

    adapter = CountingAdapter(clock)
    session = ClientSession(adapter, clock=clock, autostart=False)
    probe = ProbeFeature(
        None,
        None,
        context_guard=lambda: (
            True if allowed["value"] else "probe binding token was invalidated"
        ),
    )
    session.start_probe(probe)
    allowed["value"] = False
    session.tick_once()

    assert adapter.observations == 0
    assert not adapter.actions
    status = session.feature_status("CompatibilityProbe")
    assert not status.active
    assert "binding token was invalidated" in status.error
    session.close()
