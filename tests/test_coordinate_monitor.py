"""Offline coordinate module checks: synthetic/recorded frames, no game input."""
from dataclasses import replace
from pathlib import Path
import threading
import time

import numpy as np
from PIL import Image
import pytest

from core.paths import project_path
from core.perception import CaptureError, Frame
from tibia_client.coordinate_monitor import (
    DEFAULT_MAPS, CoordinateSettings, CoordinateMonitor, CoordinateState,
    CoordinateSample, create_locator, display_coordinate, find_crosshair,
)
from tibia_client.minimap_localization import MatchResult


def picture(cross=True, dark=False):
    rgb = np.full((80, 80, 3), 153, np.uint8)
    if cross:
        value = 0 if dark else 255
        rgb[37:43, 39:41] = value
        rgb[39:41, 37:43] = value
    return Frame(rgb)


def settings(**changes):
    return replace(CoordinateSettings(Path('unused'), (0, 0, 80, 80), (1.,)), **changes)


def match(accepted=True):
    return MatchResult(accepted, 32204 if accepted else None, 32300 if accepted else None,
                       6 if accepted else None, 1. if accepted else None, .99, .5, 200, 'matched', 5.)


def sample(**changes):
    return replace(CoordinateSample(picture(), (0, 0, 80, 80), (40., 40.), match(),
                                     10., 40., 'Player crosshair'), **changes)


def await_condition(predicate):
    end = time.monotonic()+4
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.005)
    pytest.fail('Coordinate monitor did not reach the expected state')


class Capture:
    def __init__(self, frame=None):
        self.frame = frame if frame is not None else picture()
        self.closed = False
        self.last_observed_at = None

    def capture(self, token):
        token.checkpoint()
        self.last_observed_at = time.monotonic()
        if isinstance(self.frame, Exception):
            raise self.frame
        return self.frame

    def close(self):
        self.closed = True


class Locator:
    def __init__(self):
        self.calls = []

    def locate(self, rgb, **kwargs):
        self.calls.append(kwargs)
        return match()


@pytest.fixture
def launch():
    monitors = []

    def make(capture=None, locator=None, options=None):
        capture = capture or Capture()
        locator = locator or Locator()
        monitor = CoordinateMonitor(options or settings(), capture_factory=lambda: capture,
                                    locator_factory=lambda _s, _c: locator, interval=.01)
        monitors.append(monitor)
        assert monitor.start()
        return monitor, capture, locator

    yield make
    for monitor in monitors:
        monitor.close()
        assert monitor.wait()


@pytest.mark.parametrize('dark', [False, True])
def test_unique_player_crosshair(dark):
    point, detail = find_crosshair(picture(dark=dark).pixels)
    assert point == (40., 40.)
    assert detail == 'Player crosshair'


def test_missing_ambiguous_or_solid_shapes_do_not_become_player_anchors():
    assert find_crosshair(picture(False).pixels)[0] is None
    rgb = picture().pixels.copy()
    rgb[17:23, 19:21] = 255
    rgb[19:21, 17:23] = 255
    assert find_crosshair(rgb)[0] is None
    rgb = picture(False).pixels.copy()
    rgb[37:43, 37:43] = 255
    assert find_crosshair(rgb)[0] is None


def test_display_stale_error_disabled_and_map_center():
    state = CoordinateState('Monitoring', True, sample())
    assert '32204' in display_coordinate(state, 10.1).value
    assert display_coordinate(state, 10.8).status == 'Stale'
    assert '32204' not in display_coordinate(state, 10.8).value
    assert display_coordinate(state, 9.).status == 'Stale'
    assert display_coordinate(replace(state, running=False), 10.1).value == 'X —   Y —   Z —'
    unknown = replace(state, sample=sample(result=match(False)))
    assert display_coordinate(unknown, 10.1).status == 'Unknown'
    centered = replace(state, sample=sample(reference='Map center'))
    assert display_coordinate(centered, 10.1).status == 'Map-center estimate'


def test_enable_stop_and_default_all_floor_search(launch):
    monitor, capture, locator = launch()
    await_condition(lambda: monitor.state().sample is not None)
    assert not monitor.start()  # no duplicate capture lanes
    assert locator.calls[0]['floor'] is None
    assert locator.calls[0]['method'] == 'index'
    assert 'prior' not in locator.calls[0]
    assert locator.calls[0]['anchor'] == (40., 40.)
    monitor.stop()
    assert monitor.state().sample is None
    assert monitor.wait()
    assert capture.closed


@pytest.mark.parametrize('ambiguous', [False, True])
def test_missing_or_ambiguous_crosshair_uses_center_and_recovers(launch, ambiguous):
    frame = picture(False)
    if ambiguous:
        rgb = picture().pixels.copy()
        rgb[17:23, 19:21] = 255
        rgb[19:21, 17:23] = 255
        frame = Frame(rgb)
    monitor, capture, locator = launch(Capture(frame))
    await_condition(lambda: monitor.state().sample is not None)
    assert locator.calls[0]['anchor'] == (39.5, 39.5)
    assert monitor.state().sample.anchor == (39.5, 39.5)
    assert monitor.state().sample.reference == 'Map center'
    assert display_coordinate(monitor.state(), time.monotonic()).status == 'Map-center estimate'
    capture.frame = picture()
    await_condition(lambda: monitor.state().sample.reference == 'Player crosshair')
    assert locator.calls[-1]['anchor'] == (40., 40.)
    assert display_coordinate(monitor.state(), time.monotonic()).status == 'Predicted'


def test_explicit_map_center_is_labelled_estimate(launch):
    monitor, _, locator = launch(Capture(picture(False)), options=settings(use_center=True))
    await_condition(lambda: monitor.state().sample is not None)
    assert locator.calls[0]['anchor'] == (39.5, 39.5)
    assert display_coordinate(monitor.state(), time.monotonic()).status == 'Map-center estimate'


def test_capture_failure_clears_prediction(launch):
    monitor, capture, _ = launch(Capture(CaptureError('closed window')))
    await_condition(lambda: monitor.state().phase == 'Error')
    assert 'closed window' in monitor.state().detail
    assert monitor.state().sample is None
    assert monitor.wait()
    assert capture.closed


def test_capture_size_change_is_not_rescaled(launch):
    capture = Capture()
    monitor, _, _ = launch(capture)
    await_condition(lambda: monitor.state().sample is not None)
    capture.frame = Frame(Image.new('RGB', (90, 80), 'gray'))
    await_condition(lambda: monitor.state().phase == 'Error')
    assert 'size changed' in monitor.state().detail
    assert monitor.state().sample is None


def test_cleanup_failure_is_visible_after_disable(launch):
    class BrokenCleanup(Capture):
        def close(self):
            raise CaptureError('could not close capture')

    monitor, _, _ = launch(BrokenCleanup())
    await_condition(lambda: monitor.state().sample is not None)
    monitor.stop()
    assert monitor.wait()
    assert monitor.state().phase == 'Error'
    assert 'cleanup failed' in monitor.state().detail
    assert monitor.state().sample is None


def test_stop_during_loading_never_starts_capture():
    entered, release = threading.Event(), threading.Event()
    captures = []

    def load(_settings, checkpoint):
        entered.set()
        assert release.wait(3)
        checkpoint()
        return Locator()

    monitor = CoordinateMonitor(settings(), locator_factory=load,
                                capture_factory=lambda: captures.append(True))
    try:
        monitor.start()
        assert entered.wait(2)
        monitor.stop()
        assert not monitor.start()
        release.set()
        assert monitor.wait()
        assert captures == []
        assert monitor.state().sample is None
    finally:
        release.set()
        monitor.close()
        monitor.wait()


def test_late_match_after_disable_is_discarded(launch):
    entered, release = threading.Event(), threading.Event()

    class SlowLocator(Locator):
        def locate(self, *_args, **_kwargs):
            entered.set()
            assert release.wait(3)
            return match()

    monitor, capture, _ = launch(locator=SlowLocator())
    try:
        assert entered.wait(2)
        monitor.stop()
        release.set()
        assert monitor.wait()
        assert monitor.state().sample is None
        assert not monitor.state().running
        assert capture.closed
    finally:
        release.set()


@pytest.mark.parametrize('changes', [dict(box=(0,0,0,0)), dict(box=(0,0,20)),
                                    dict(scales=(.0625,)), dict(scales=(float('nan'),)), dict(floor=16)])
def test_invalid_settings(changes):
    with pytest.raises(ValueError):
        settings(**changes)


def test_supplied_capture_crosshair_and_atlas_match():
    if not (DEFAULT_MAPS/'manifest.json').is_file():
        pytest.skip('Local reference atlas is not installed')
    frame = Frame(Image.open(project_path('tests/fixtures/screenshots/originals/user-paulistinha-20261013.png')))
    box = (1711, 35, 1843, 171)
    crop = frame.pixels[35:171, 1711:1843]
    anchor, _ = find_crosshair(crop)
    assert anchor is not None
    monitor = CoordinateMonitor(CoordinateSettings(DEFAULT_MAPS, box),
                                capture_factory=lambda: Capture(frame))
    try:
        monitor.start()
        await_condition(lambda: monitor.state().sample is not None or monitor.state().phase == 'Error')
        assert monitor.state().sample is not None, monitor.state().detail
        result = monitor.state().sample.result
        # Full worker, automatic crosshair, automatic zoom, all-floor search.
        # Reproduces an inspected match, not independently measured live truth.
        assert result is not None and result.accepted
        assert (result.x, result.y, result.z) == (32204, 32300, 6)
    finally:
        monitor.close()
        assert monitor.wait()


def test_window_is_opt_in_updates_and_clears_stale_coordinates():
    import tkinter as tk
    from modules.CoordinateMonitor import CoordinateMonitorWindow

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip('Tk display unavailable')
    root.withdraw()

    class ViewMonitor:
        busy = False
        started = 0
        current = CoordinateState()

        def state(self): return self.current
        def start(self): self.started += 1; return True
        def stop(self): self.current = CoordinateState()
        close = stop

    monitor = ViewMonitor()
    now = [10.1]
    ui = CoordinateMonitorWindow(root, box=(0,0,80,80), monitor_factory=lambda _s: monitor,
                                 clock=lambda: now[0])
    ui.window.withdraw()

    def refresh():
        ui.window.after_cancel(ui._after)
        ui._refresh()

    try:
        assert monitor.started == 0
        assert not ui.enabled.get()
        ui.enabled.set(True)
        ui._toggle()
        assert monitor.started == 1
        monitor.current = CoordinateState('Monitoring', True, sample())
        refresh()
        assert '32204' in ui.coordinate.get()
        assert ui._photo is not None
        now[0] = 11.
        refresh()
        assert ui.status.get() == 'Stale'
        assert '32204' not in ui.coordinate.get()
        ui.stop()
        refresh()
        assert not ui.enabled.get()
        assert ui.status.get() == 'Disabled'
        ui.bounds.set('invalid')
        ui.enabled.set(True)
        ui._toggle()
        refresh()
        assert ui.status.get() == 'Cannot enable'
    finally:
        ui.close()
        root.destroy()
