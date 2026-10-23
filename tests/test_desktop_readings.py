"""The inspector shares capture and detections without producing input actions."""
from collections import Counter
import threading

import numpy as np
import pytest

from core.perception import Frame, Match
from tibia_client import desktop, visual_readings
from tibia_client.adaptive import FIELDS
from tibia_client.desktop import DesktopAdapter, DesktopAdapterError
from tibia_client.models import Marker, Quality, Reading, Sensor, SensorKind, Target
from tibia_client.visual_readings import MarkerReader as RealMarkerReader


class Token:
    cancelled = False

    def checkpoint(self):
        pass


class Handle:
    def __init__(self):
        self.closed = 0
        self.calls = Counter()

    def close(self):
        self.closed += 1


class Battle(Handle):
    def read_all(self, frame, region, scale):
        self.calls['all'] += 1
        return Reading(({'name': 'Orc', 'complete': True, 'selected': True,
                         'position': (55, 40), 'box': (40, 30, 70, 50)},))


class Markers(Handle):
    def read(self, frame, region, name, scale):
        self.calls[name] += 1
        return Reading(Marker((25, 25), False), diagnostics={
            'count': 1, 'ui_scale': scale, 'scale_source': 'configured', 'score': .95})


class Controls(Handle):
    def read(self, frame, scale):
        # FollowControls itself caches the observation for identical Frame objects.
        if getattr(self, 'frame', None) is not frame:
            self.calls['decode'] += 1
            self.frame = frame
        return Reading(False, diagnostics={'follow_position': (7, 8), 'stand_position': (7, 4),
                                          'follow_visible': True})


class Tracker(Handle):
    discovery_ms = 12.5
    detail = 'retained layout'
    busy = False

    def read(self, frame):
        self.calls['read'] += 1
        return {name: Reading(100 + index) for index, name in enumerate(FIELDS)}

    def reset(self):
        self.calls['reset'] += 1


@pytest.fixture
def setup(monkeypatch):
    from tibia_client import retained_tracker
    calls = Counter()
    boxes = {'BattleBox': (30, 20, 100, 70), 'MapBox': (10, 10, 50, 50),
             'StatusBarBox': (0, 70, 90, 90), 'RingBox': (90, 70, 110, 90),
             'AmuletBox': (110, 70, 130, 90)}
    profile = {'ui_scale': 1, 'calibration_size': (180, 120),
               'ResourceBars': {'HealthBar': (0, 0, 50, 5), 'ManaBar': (0, 5, 50, 10)},
               'Boxes': {name: [dict(zip(('x', 'y', 'w', 'h'), box))]
                         for name, box in boxes.items()}}
    monkeypatch.setattr(visual_readings, 'load_visual_config', lambda: (profile, ('Orc',)))

    def capture():
        calls['capture'] += 1
        return Frame(np.full((120, 180, 3), 72, np.uint8))

    def bar(frame, region, kind):
        calls[kind] += 1
        return 75 if kind == 'health' else 55

    def target(frame, region, name, scale, reader):
        calls['target'] += 1
        return Reading(Target(1, (55, 40), True), diagnostics={'score': .9})

    def locate(template, **kwargs):
        calls[template.rsplit('/', 1)[-1]] += 1
        return Match(0, 0, 2, 2, .95)

    monkeypatch.setattr('engine.ScanStages.read_bar_region', bar)
    monkeypatch.setattr(visual_readings, 'read_bar_region', bar)
    monkeypatch.setattr('tibia_client.battle_text.read_target', target)
    monkeypatch.setattr(visual_readings, 'read_target', target)
    monkeypatch.setattr(desktop, 'LocateMatch', locate)
    monkeypatch.setattr(visual_readings, 'locate_match', locate)
    monkeypatch.setattr(visual_readings, 'BattleTextReader', Battle)
    monkeypatch.setattr(visual_readings, 'MarkerReader', Markers)
    monkeypatch.setattr('tibia_client.follow_controls.FollowControls', Controls)
    adapter = DesktopAdapter(0, hotkey=object(), capture_provider=capture)
    adapter._battle_text.close()
    adapter._markers.close()
    battle, markers, controls, tracker = Battle(), Markers(), Controls(), Tracker()
    adapter._battle_text, adapter._markers = battle, markers
    adapter._follow_matches['controls'] = controls
    monkeypatch.setattr(retained_tracker, 'RetainedResourceTracker', lambda: tracker)
    yield adapter, calls, profile, battle, markers, controls, tracker
    adapter.close()


def test_one_capture_and_shared_overlap_decodes_with_full_table(setup):
    adapter, calls, _, battle, markers, controls, tracker = setup
    inspector = Sensor(SensorKind.READINGS)
    health = Sensor(SensorKind.HEALTH, (0, 0, 50, 5))
    target = Sensor(SensorKind.TARGET, (30, 20, 100, 70), 'Orc')
    marker = Sensor(SensorKind.MARKER, (10, 10, 50, 50), 'Flag')
    haste = Sensor(SensorKind.HASTED, (0, 70, 90, 90))
    follow = Sensor(SensorKind.FOLLOW)
    result = adapter.observe([inspector, health, target, marker, haste, follow, inspector], Token())
    composite = result.readings[inspector]
    table = composite.value
    assert composite.quality is Quality.KNOWN
    assert calls['capture'] == calls['health'] == calls['mana'] == calls['target'] == 1
    assert calls['Hur.png'] == markers.calls['Flag'] == controls.calls['decode'] == 1
    assert tracker.calls['read'] == 1
    assert battle.calls['all'] == 1
    assert result.readings[follow].value == (7, 8)  # Automation gets an action point.
    assert table['status:follow'].value == 'Standing'  # Inspector gets the state.
    assert table['bar:health'].value == result.readings[health].value == 75
    assert table['bar:health'].diagnostics['format'] == 'percent'
    assert table['target:orc'].value == result.readings[target].value
    expected = {*FIELDS, 'bar:health', 'bar:mana', 'battle', 'creature:0', 'target:orc',
                'minimap', 'status:hasted', 'status:hungry', 'status:pz',
                'status:ring_empty', 'status:amulet_empty', 'status:follow',
                *(f'marker:{name}' for name in visual_readings.NameMarks)}
    assert set(table) == expected
    assert table['status:hasted'].diagnostics['score'] == .95
    assert composite.diagnostics['discovery_ms'] == 12.5


def test_reset_is_deferred_and_does_not_close_shared_automation_handles(setup):
    adapter, calls, profile, battle, markers, controls, tracker = setup
    sensor = Sensor(SensorKind.READINGS)
    adapter.observe([sensor], Token())
    original = adapter._readings_inspector
    adapter.reset_readings()
    assert adapter._readings_inspector is original and tracker.calls['reset'] == 0
    profile['ResourceBars']['HealthBar'] = (0, 0, 40, 5)
    result = adapter.observe([sensor], Token())
    assert adapter._readings_inspector is not original and tracker.calls['reset'] == 1
    assert result.readings[sensor].value['bar:health'].diagnostics['effective_region'] == (0, 0, 40, 5)
    assert battle.closed == markers.closed == controls.closed == 0
    adapter.close()
    assert tracker.closed == battle.closed == markers.closed == controls.closed == 1
    with pytest.raises(DesktopAdapterError, match='closed'):
        adapter.reset_readings()


def test_missing_calibration_and_numeric_failure_preserve_table(setup, monkeypatch):
    adapter, _, profile, *_, tracker = setup
    profile['ResourceBars'] = {}
    monkeypatch.setattr(tracker, 'read', lambda frame: (_ for _ in ()).throw(RuntimeError('failed')))
    reading = adapter.observe([Sensor(SensorKind.READINGS)], Token()).readings[Sensor(SensorKind.READINGS)]
    assert reading.value['bar:health'].quality is Quality.UNKNOWN
    assert 'not calibrated' in reading.value['bar:health'].detail
    assert reading.value['battle'].value == '1 rows'
    assert all(reading.value[name].quality is Quality.ERROR for name in FIELDS)


@pytest.mark.parametrize('sensor', [Sensor(SensorKind.READINGS, (0, 0, 10, 10)),
                                   Sensor(SensorKind.READINGS, name='generation')])
def test_invalid_composite_request_does_not_create_inspector(setup, sensor):
    adapter, *_ = setup
    result = adapter.observe([sensor], Token())
    assert result.readings[sensor].quality is Quality.ERROR
    assert adapter._readings_inspector is None and adapter._readings_tracker is None


def test_standalone_inspector_owns_only_its_own_detectors(monkeypatch):
    battle, controls, markers = Battle(), Controls(), Markers()
    monkeypatch.setattr(visual_readings, 'BattleTextReader', lambda: battle)
    monkeypatch.setattr(visual_readings, 'MarkerReader', lambda: markers)
    monkeypatch.setattr('tibia_client.follow_controls.FollowControls', lambda: controls)
    inspector = visual_readings.VisualReadings(profile={})
    inspector.close()
    assert battle.closed == controls.closed == markers.closed == 1


def test_composite_reports_shared_timing_without_restamping_capture(setup, monkeypatch):
    adapter, _, _, *_, tracker = setup

    class Clock:
        now = 10.0

        def monotonic(self):
            return self.now
    clock = Clock()
    adapter.clock, adapter._system_clock = clock, False
    capture = adapter.capture_provider

    def slow_capture():
        frame = capture()
        clock.now += .020
        return frame

    read = tracker.read

    def slow_resources(frame):
        clock.now += .030
        return read(frame)

    adapter.capture_provider = slow_capture
    monkeypatch.setattr(tracker, 'read', slow_resources)
    result = adapter.observe([Sensor(SensorKind.READINGS)], Token())
    diagnostics = result.readings[Sensor(SensorKind.READINGS)].diagnostics
    assert result.observed_at == 10.0
    assert diagnostics['capture_ms'] == pytest.approx(20)
    assert diagnostics['decode_ms'] == pytest.approx(30)
    assert diagnostics['total_ms'] == pytest.approx(50)
    assert diagnostics['visual_decode_ms'] == 0
    assert diagnostics['recovery'] == 'retained layout'


def test_follow_discovery_candidate_cannot_change_existing_frame_observation(monkeypatch):
    from tibia_client.follow_controls import FollowControls
    controls = FollowControls()
    frame = Frame(np.full((30, 30, 3), 72, np.uint8))
    original = Reading(False)
    controls._frame, controls._reading = frame, original
    controls._candidate = (frame.size, 1., (0, 0, 20, 20), {})
    changed = Reading(True, diagnostics={'stand_visible': False, 'follow_visible': True})
    monkeypatch.setattr(controls, '_locate', lambda *args: changed)
    assert controls.read(frame) is original
    assert controls._candidate is not None
    assert controls.read(Frame(frame.pixels)) is changed
    assert controls._candidate is None
    controls.close()


def test_real_adapter_session_monitor_share_frame_values_timing_and_recovery(setup, monkeypatch):
    from tibia_client.models import Step
    from tibia_client.session import ClientSession
    from tibia_client.session_monitor import SessionResourceMonitor

    adapter, calls, _, *_, tracker = setup

    class Clock:
        now = 10.0

        def monotonic(self):
            return self.now
    clock = Clock()
    adapter.clock, adapter._system_clock = clock, False
    capture, resources = adapter.capture_provider, tracker.read

    def capture_with_cost():
        clock.now += .005
        return capture()

    def resources_with_cost(frame):
        clock.now += .007
        return resources(frame)

    adapter.capture_provider = capture_with_cost
    monkeypatch.setattr(tracker, 'read', resources_with_cost)
    health = Sensor(SensorKind.HEALTH, (0, 0, 50, 5))

    class Feature:
        name, interval = 'health-reader', .1

        def sensors(self):
            return (health,)

        def tick(self, context):
            self.snapshot = context.snapshot
            return Step()

        def on_result(self, result):
            pass

    feature = Feature()
    session = ClientSession(adapter, clock=clock, autostart=False)
    monitor = SessionResourceMonitor(session)
    try:
        session.start_feature(feature)
        assert monitor.start()
        session.tick_once()
        state = monitor.state()
        view = session.observation_view(monitor._subscription)
        assert calls['capture'] == calls['health'] == tracker.calls['read'] == 1
        assert state.sample.frame is state.visual.frame is view.frame
        assert feature.snapshot.observation(health).value == state.visual.readings['bar:health'].value == 75
        assert state.sample.observed_at == state.visual.observed_at == 10.0
        assert state.sample.capture_ms == pytest.approx(5)
        assert state.sample.decode_ms == pytest.approx(7)
        assert state.sample.total_ms == pytest.approx(12)
        assert state.sample.discovery_ms == 12.5
        assert state.recovery == 'retained layout'
        assert state.sample.readings[FIELDS[0]].value == 100
        monitor.recalibrate()
        assert monitor.state().sample is None
        assert tracker.calls['reset'] == 0  # Signaling never runs decode/reset on the UI thread.
        clock.now += .2
        session.tick_once()
        assert tracker.calls['reset'] == 1 and monitor.state().sample is not None
    finally:
        monitor.close()
        session.close()


def test_inspector_different_marker_region_and_scale_never_reset_automation_learning(setup, monkeypatch):
    adapter, _, profile, *_ = setup
    region = (10, 10, 50, 50)
    markers = RealMarkerReader()
    markers._closed = True  # Keep this state-isolation test independent of discovery threads.
    markers._context = ((180, 120), region, 1.)
    markers._scale, markers._anchors = 1.25, ('Flag',)
    adapter._markers = markers

    def marker(frame, box, name, scale):
        return Reading(Marker((25, 25), False), diagnostics={
            'count': 1, 'ui_scale': scale, 'score': .95})
    monkeypatch.setattr(visual_readings, 'read_marker', marker)
    profile['Boxes']['MapBox'] = [dict(zip(('x', 'y', 'w', 'h'), (60, 10, 100, 50)))]
    request = Sensor(SensorKind.MARKER, region, 'Flag')
    for scale in (1., 1.5, 1.):
        profile['ui_scale'] = scale
        adapter.reset_readings()
        result = adapter.observe([request, Sensor(SensorKind.READINGS)], Token())
        assert markers._context == ((180, 120), region, 1.) and markers._scale == 1.25
        assert result.readings[request].diagnostics['ui_scale'] == 1.25
        table = result.readings[Sensor(SensorKind.READINGS)].value
        assert table['marker:Flag'].diagnostics['ui_scale'] == scale
        assert table['marker:Flag'].diagnostics['effective_region'] == (60, 10, 100, 50)


def test_inspector_private_follow_scale_and_battle_region_do_not_reuse_wrong_context(setup, monkeypatch):
    adapter, _, profile, battle, _, controls, _ = setup

    class ScaledControls(Controls):
        def read(self, frame, scale):
            self.calls['read'] += 1
            return Reading(scale == 1.5)
    monkeypatch.setattr('tibia_client.follow_controls.FollowControls', ScaledControls)
    profile['ui_scale'] = 1.5
    profile['Boxes']['BattleBox'] = [dict(zip(('x', 'y', 'w', 'h'), (50, 20, 120, 80)))]
    sensors = [Sensor(SensorKind.FOLLOW), Sensor(SensorKind.TARGET, (30, 20, 100, 70), 'Orc'),
               Sensor(SensorKind.READINGS)]
    result = adapter.observe(sensors, Token())
    assert result.readings[sensors[0]].value == (7, 8)
    assert result.readings[sensors[-1]].value['status:follow'].value == 'Following'
    assert adapter._readings_inspector.follow is not controls
    assert controls.calls['decode'] == 1
    assert battle.calls['all'] == 0 and adapter._readings_inspector.battle.calls['all'] == 1


@pytest.mark.parametrize('broken', ['visual', 'numeric'])
def test_inspector_constructor_failures_leave_independent_rows_available(setup, monkeypatch, broken):
    from tibia_client import retained_tracker
    adapter, _, profile, *_ = setup
    if broken == 'visual':
        profile['ui_scale'] = 99
    else:
        def fail():
            raise ValueError('cache configuration unavailable')
        monkeypatch.setattr(retained_tracker, 'RetainedResourceTracker', fail)
    reading = adapter.observe([Sensor(SensorKind.READINGS)], Token()).readings[Sensor(SensorKind.READINGS)]
    assert reading.quality is Quality.KNOWN
    if broken == 'visual':
        assert reading.value['visual_error'].quality is Quality.ERROR
        assert all(reading.value[name].quality is Quality.KNOWN for name in FIELDS)
    else:
        assert reading.value['bar:health'].value == 75
        assert all(reading.value[name].quality is Quality.ERROR for name in FIELDS)


def test_keyboard_adapter_supports_shared_monitor_without_enabling_automation_vision(setup):
    from tibia_client.desktop import KeyboardDesktopAdapter
    from tibia_client.session import ClientSession
    from tibia_client.session_monitor import SessionResourceMonitor
    adapter, _, profile, *_ = setup
    profile['ResourceBars'], profile['Boxes'] = {}, {}
    keyboard = KeyboardDesktopAdapter(0, hotkey=object(), capture_provider=adapter.capture_provider)
    session = ClientSession(keyboard, autostart=False)
    monitor = SessionResourceMonitor(session)
    try:
        assert keyboard.capabilities().sensors == ('readings',)
        assert monitor.start()
        session.tick_once()
        state = monitor.state()
        assert not session.closed and state.sample is not None
        assert state.sample.readings[FIELDS[0]].quality is Quality.KNOWN
        assert state.visual.readings['bar:health'].quality is Quality.UNKNOWN
        with pytest.raises(DesktopAdapterError, match='calibration'):
            keyboard.observe([Sensor(SensorKind.HEALTH, (0, 0, 50, 5))], Token())
    finally:
        monitor.close()
        session.close()


def test_repeated_recalibration_reuses_private_worker_lane_until_adapter_close(setup, monkeypatch):
    adapter, _, profile, *_ = setup

    class BlockingBattle(Battle):
        def __init__(self):
            super().__init__()
            self.release = threading.Event()
            self.worker = None

        def read_all(self, frame, region, scale):
            if self.worker is None:
                self.calls['workers'] += 1
                self.worker = threading.Thread(target=self.release.wait, daemon=True)
                self.worker.start()
            return Reading(())

        def close(self):
            super().close()
            self.release.set()
            if self.worker is not None:
                self.worker.join(timeout=1)

    private = BlockingBattle()
    monkeypatch.setattr(visual_readings, 'BattleTextReader', lambda: private)
    sensor = Sensor(SensorKind.READINGS)
    adapter.observe([sensor], Token())
    handles = adapter._readings_detectors
    wrapper = adapter._readings_inspector
    for width in (45, 40, 35):
        profile['ResourceBars']['HealthBar'] = (0, 0, width, 5)
        adapter.reset_readings()
        result = adapter.observe([sensor], Token())
        assert adapter._readings_inspector is not wrapper
        wrapper = adapter._readings_inspector
        assert adapter._readings_detectors is handles
        assert wrapper.battle is private
        assert result.readings[sensor].value['bar:health'].diagnostics['effective_region'][2] == width
        assert all(reader.closed == 0 for reader in handles.values())
    assert private.calls['workers'] == 1 and private.worker.is_alive()
    adapter.close()
    adapter.close()
    assert all(reader.closed == 1 for reader in handles.values())
    assert not private.worker.is_alive()


def test_actual_desktop_cleanup_failure_remains_retryable_through_session(setup):
    from tibia_client.session import ClientSession
    adapter, *_ = setup
    capture = adapter.capture_provider

    class Provider:
        closes = 0

        def capture(self):
            return capture()

        def close(self):
            self.closes += 1
            if self.closes == 1:
                raise OSError('capture worker did not stop')

    provider = Provider()
    adapter.capture_provider = provider
    session = ClientSession(adapter, autostart=False)
    session.subscribe_observations((Sensor(SensorKind.READINGS),))
    session.tick_once()
    try:
        with pytest.raises(OSError, match='did not stop'):
            session.close()
        assert session.closed and isinstance(session.close_error, OSError)
        assert adapter._closed and not adapter._close_complete
        session.close()
        assert provider.closes == 2 and session.close_error is None
        session.close()
        adapter.close()
        assert provider.closes == 2 and adapter._close_complete
    finally:
        session.close()
