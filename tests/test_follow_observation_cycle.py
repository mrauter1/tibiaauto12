"""Discovery advances between observations, including reused capture frames."""
from types import SimpleNamespace

import numpy as np
import pytest

from core.perception import Frame
from tibia_client.desktop import DesktopAdapter
from tibia_client.follow_controls import FollowControls
from tibia_client.models import Quality, Reading, Sensor, SensorKind
from tibia_client.visual_readings import VisualReadings


@pytest.fixture
def follow_reader(monkeypatch):
    frame = Frame(np.full((40, 40, 3), 72, np.uint8))
    controls = FollowControls()
    # Model an in-flight discovery deterministically, without scheduling sleeps.
    controls._worker = SimpleNamespace(is_alive=lambda: True)
    calls = []
    known = Reading(False, diagnostics={
        'follow_position': (10, 10), 'stand_position': (10, 5),
        'follow_visible': True, 'stand_visible': True})

    def locate(*args):
        calls.append(args)
        return known

    monkeypatch.setattr(controls, '_locate', locate)
    candidate = (frame.size, 1., (0, 0, 20, 20), dict(known.diagnostics))
    yield frame, controls, candidate, known, calls
    controls.close()


def test_candidate_waits_for_next_cycle_even_when_frame_is_reused(follow_reader):
    frame, controls, candidate, known, calls = follow_reader
    cycle = object()
    initial = controls.read(frame, observation_cycle=cycle)
    assert initial.quality is Quality.UNKNOWN

    controls._candidate = candidate
    assert controls.read(frame, observation_cycle=cycle) is initial
    assert not calls
    assert controls.read(frame, observation_cycle=object()) is known
    assert len(calls) == 1


def test_standalone_reads_adopt_discovery_on_reused_frame(follow_reader):
    frame, controls, candidate, known, calls = follow_reader
    assert controls.read(frame).quality is Quality.UNKNOWN
    controls._candidate = candidate
    assert controls.read(frame) is known
    assert len(calls) == 1


@pytest.mark.parametrize('with_inspector', [False, True])
def test_adapter_reused_frame_advances_follow_on_next_observation(
        follow_reader, monkeypatch, with_inspector):
    frame, controls, candidate, _, calls = follow_reader
    adapter = DesktopAdapter(0, hotkey=object(), capture_provider=lambda: frame)
    adapter._follow_matches['controls'] = controls
    follow_sensor = Sensor(SensorKind.FOLLOW)
    readings_sensor = Sensor(SensorKind.READINGS)
    sensors = [follow_sensor]
    if with_inspector:
        inspector = VisualReadings(profile={}, follow=controls)
        adapter._readings_inspector = inspector
        adapter._readings_tracker = SimpleNamespace(read=lambda frame: {}, close=lambda: None)
        read = inspector.read

        def complete_discovery_then_inspect(*args, **kwargs):
            # Discovery finishes after automation's read but before inspection.
            controls._candidate = candidate
            return read(*args, **kwargs)

        monkeypatch.setattr(inspector, 'read', complete_discovery_then_inspect)
        sensors.append(readings_sensor)
    token = SimpleNamespace(cancelled=False, checkpoint=lambda: None)
    try:
        first = adapter.observe(sensors, token)
        assert first.frame is frame
        assert first.readings[follow_sensor].quality is Quality.UNKNOWN
        if with_inspector:
            assert first.readings[readings_sensor].value['status:follow'].quality is Quality.UNKNOWN
        else:
            controls._candidate = candidate

        second = adapter.observe(sensors, token)
        assert second.frame is first.frame
        assert second.readings[follow_sensor].quality is Quality.KNOWN
        assert second.readings[follow_sensor].value == (10, 10)
        if with_inspector:
            assert second.readings[readings_sensor].value['status:follow'].value == 'Standing'
        assert len(calls) == 1
    finally:
        adapter.close()
