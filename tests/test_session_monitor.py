"""Main-menu monitoring only subscribes to the shared session's observations."""
from types import SimpleNamespace

import numpy as np
import pytest

from core.perception import Frame
from tibia_client.models import Observation, Quality, Reading, Sensor, SensorKind, Snapshot
from tibia_client.resource_monitor import display_readings
from tibia_client.session_monitor import SessionResourceMonitor


class Session:
    def __init__(self):
        self.closed = False
        self.views = {}
        self.calls = []
        self.serial = 0
        self.reason = ""

    def subscribe_observations(self, sensors, *, interval):
        self.serial += 1
        self.calls.append(("subscribe", self.serial, sensors, interval))
        self.views[self.serial] = SimpleNamespace(snapshot=Snapshot(), frame=None)
        return self.serial

    def unsubscribe_observations(self, identity):
        self.calls.append(("unsubscribe", identity))
        return self.views.pop(identity, None) is not None

    def observation_view(self, identity):
        return self.views.get(identity)

    def reset_readings(self):
        self.calls.append(("reset",))

    def diagnostics(self):
        return SimpleNamespace(capture=SimpleNamespace(reasons=(self.reason,) if self.reason else ()),
                               failure_reasons=(), readiness_reason=None)

    def publish(self, rows, *, frame_id=3, snapshot_id=None, at=10, quality=Quality.KNOWN):
        sensor = Sensor(SensorKind.READINGS)
        observed = Observation(sensor, rows, at, frame_id, 1, quality, "composite failed",
                               {"capture_ms": 12, "decode_ms": 8, "visual_decode_ms": 3, "discovery_ms": 4,
                                "total_ms": 24, "recovery": "Checking panel"})
        frame = Frame(np.full((6, 8, 3), 42, np.uint8))
        view = SimpleNamespace(snapshot=Snapshot({sensor: observed}, snapshot_id or frame_id, 1, at + .1),
                               frame=frame)
        self.views[self.serial] = view
        return view


def rows():
    region = {"effective_region": (1, 1, 5, 4)}
    return {
        "resource_health": Reading({"count": 710}, diagnostics=region),
        "resource_mana": Reading(None, Quality.UNKNOWN, "hidden", region),
        "resource_soul": Reading(None, Quality.ERROR, "decoder failed", region),
        "resource_capacity": Reading({"count": 192}, diagnostics=region),
        "health_bar": Reading(75, diagnostics={**region, "format": "percent", "label": "HP bar"}),
        "mana_bar": Reading(40, diagnostics={**region, "format": "percent", "label": "Mana bar"}),
        "creature_0": Reading("Dragon", diagnostics={**region, "group": "Creatures"}),
        "map_marker": Reading(True, diagnostics={**region, "group": "Map"}),
    }


def test_opening_is_passive_start_is_idempotent_and_close_only_unsubscribes():
    client = Session()
    monitor = SessionResourceMonitor(client)
    assert not monitor.busy and not client.calls
    assert monitor.start()
    assert monitor.busy and not monitor.start()
    assert client.calls == [("subscribe", 1, (Sensor(SensorKind.READINGS),), .1)]
    monitor.close()
    monitor.close()
    assert client.calls[-1] == ("unsubscribe", 1)
    assert len(client.calls) == 2
    assert not client.closed and not monitor.busy and monitor.wait()
    with pytest.raises(RuntimeError, match="closed"):
        monitor.start()


def test_complete_rows_timestamps_timing_and_source_frame_are_preserved():
    client = Session()
    monitor = SessionResourceMonitor(client)
    monitor.start()
    view = client.publish(rows())
    state = monitor.state()
    assert state.preview is state.sample
    assert state.sample.frame is state.visual.frame is view.frame
    assert state.sample.sequence == state.visual.sequence == 3
    assert state.sample.observed_at == state.visual.observed_at == 10
    assert (state.sample.capture_ms, state.sample.discovery_ms,
            state.sample.decode_ms, state.sample.total_ms) == (12, 4, 8, 24)
    assert state.visual.decode_ms == 3
    assert state.recovery == "Checking panel"
    assert monitor.state() is state  # Polling does not manufacture a fresh result.
    displayed = {row.field: row for row in display_readings(state, 10.1, stale_ms=2000)}
    assert set(displayed) == set(rows())
    assert displayed["resource_health"].value == "710"
    assert displayed["resource_mana"].status == "N/A"
    assert displayed["resource_soul"].status == "Error"
    assert displayed["health_bar"].value == "75%"
    assert displayed["mana_bar"].value == "40%"
    assert displayed["creature_0"].value == "Dragon"
    assert all(row.region == (1, 1, 5, 4) for row in displayed.values())
    assert all(row.status == "Stale" and row.value == "N/A"
               for row in display_readings(monitor.state(), 12.1, stale_ms=2000))


def test_stop_all_invalidates_view_and_requires_explicit_restart():
    client = Session()
    monitor = SessionResourceMonitor(client)
    monitor.start()
    client.publish(rows())
    assert monitor.state().sample is not None
    client.views.clear()
    state = monitor.state()
    assert state.phase == "Stopped" and not state.running
    assert state.sample is state.visual is state.preview is None
    assert not monitor.busy and client.serial == 1
    assert monitor.start() and client.serial == 2
    assert monitor.state().sample is None


def test_closed_session_clears_values_and_reports_failure_without_closing_client():
    client = Session()
    monitor = SessionResourceMonitor(client)
    monitor.start()
    client.publish(rows())
    assert monitor.state().sample is not None
    client.closed, client.reason = True, "capture worker could not be stopped"
    state = monitor.state()
    assert state.phase == "Error" and client.reason in state.detail
    assert state.sample is state.visual is state.preview is None
    assert not monitor.busy


def test_capture_retry_clears_display_but_keeps_subscription():
    client = Session()
    monitor = SessionResourceMonitor(client)
    monitor.start()
    client.publish(rows())
    monitor.state()
    client.reason = "capture timed out; retrying"
    client.views[1] = SimpleNamespace(snapshot=Snapshot(), frame=None)
    waiting = monitor.state()
    assert waiting.running and waiting.sample is None
    assert client.reason in waiting.detail
    client.reason = ""
    client.publish(rows(), frame_id=4, at=11)
    assert monitor.state().sample.sequence == 4
    assert client.serial == 1


def test_different_observation_and_preview_frames_are_never_paired():
    client = Session()
    monitor = SessionResourceMonitor(client)
    monitor.start()
    client.publish(rows(), frame_id=3, snapshot_id=4)
    state = monitor.state()
    assert state.running and state.sample is state.preview is None


def test_recalibration_discards_old_view_and_resubscribes_only_when_running():
    client = Session()
    monitor = SessionResourceMonitor(client)
    monitor.recalibrate()
    assert client.calls == [("reset",)] and not monitor.busy
    monitor.start()
    client.publish(rows())
    monitor.state()
    monitor.recalibrate()
    assert [call[0] for call in client.calls[-3:]] == ["unsubscribe", "reset", "subscribe"]
    assert client.serial == 2 and monitor.busy
    assert monitor.state().sample is None


@pytest.mark.parametrize("value,quality,status", [
    (None, Quality.UNKNOWN, "N/A"),
    (None, Quality.ERROR, "Error"),
    ({"resource_health": 17}, Quality.KNOWN, "Error"),
])
def test_invalid_or_failed_composite_never_promotes_values(value, quality, status):
    client = Session()
    monitor = SessionResourceMonitor(client)
    monitor.start()
    client.publish(value, quality=quality)
    displayed = display_readings(monitor.state(), 10.1)
    assert all(row.value == "N/A" and row.status == status for row in displayed)


def test_ui_constructs_shared_facade_before_opening_window(monkeypatch):
    from modules import ResourceMonitor as ui
    from tibia_client import session_monitor

    client = Session()
    created = []
    monkeypatch.setattr(session_monitor, "SessionResourceMonitor",
                        lambda supplied: created.append(supplied) or object())

    class WindowReached(Exception):
        pass

    def window():
        raise WindowReached()

    monkeypatch.setattr(ui.tk, "Tk", window)
    with pytest.raises(WindowReached):
        ui.ResourceMonitorWindow(client=client)
    assert created == [client]
    assert not client.calls
    with pytest.raises(ValueError, match="either"):
        ui.ResourceMonitorWindow(client=client, monitor=object())


def test_real_session_supplies_facade_without_activating_input_or_owning_shutdown():
    from tibia_client.models import ObservationBatch
    from tibia_client.session import ClientSession

    class Adapter:
        captures = closes = 0

        def ready(self):
            return True

        def observe(self, sensors, token):
            self.captures += 1
            return ObservationBatch(
                {sensor: Reading(rows()) for sensor in sensors}, 10.0,
                Frame(np.full((6, 8, 3), 42, np.uint8)),
            )

        def activate_input(self):
            raise AssertionError("monitoring must not activate input")

        def execute(self, *_args):
            raise AssertionError("monitoring must not execute input")

        def close(self):
            self.closes += 1

    adapter = Adapter()
    session = ClientSession(adapter, clock=SimpleNamespace(monotonic=lambda: 10.0), autostart=False)
    monitor = SessionResourceMonitor(session)
    try:
        assert adapter.captures == 0 and not session.running
        monitor.start()
        session.tick_once()
        state = monitor.state()
        assert adapter.captures == 1 and state.sample.observed_at == 10.0
        assert state.sample.readings["resource_health"].value["count"] == 710
        monitor.close()
        session.tick_once()
        assert adapter.captures == 1 and adapter.closes == 0 and not session.closed
    finally:
        session.close()
