from __future__ import annotations

import numpy as np
import time

from core.perception import Frame, Match
from tibia_client import desktop
from tibia_client.desktop import DesktopAdapter, DesktopAdapterError
from tibia_client.capture import CaptureProcess
from tibia_client.models import Action, ActionKind, Marker, Quality, Sensor, SensorKind, Target


class Token:
    def __init__(self):
        self.cancelled = False
        self.checks = 0

    def checkpoint(self):
        self.checks += 1
        if self.cancelled:
            raise RuntimeError("cancelled")


class Clock:
    def __init__(self, value=10.0):
        self.value = value

    def monotonic(self):
        return self.value


class Provider:
    def __init__(self, clock=None):
        self.calls = 0
        self.closed = False
        self.clock = clock

    def capture(self):
        self.calls += 1
        if self.clock is not None:
            self.clock.value += 7
        return Frame(np.zeros((120, 180, 3), dtype=np.uint8))

    def close(self):
        self.closed = True


class FakeHotkey:
    def __init__(self, token=None):
        self.SendToClient = self
        self.size = (180, 120)
        self.focused = True
        self.ready = True
        self.cursor = (17, 23)
        self.events = []
        self.token = token

    def MainWindowSize(self):
        self.events.append(("size",))
        return self.size

    def IsFocused(self):
        self.events.append(("focus",))
        return self.focused

    def IsReady(self):
        self.events.append(("ready",))
        return self.ready

    def Position(self):
        self.events.append(("position",))
        return self.cursor

    def MoveTo(self, x, y):
        self.events.append(("move", x, y))

    def Press(self, key):
        self.events.append(("press", key))

    def LeftClick(self, x, y):
        self.events.append(("left", x, y))
        if self.token is not None:
            self.token.cancelled = True

    def RightClick(self, x, y):
        self.events.append(("right", x, y))

    def DragTo(self, start, end):
        self.events.append(("drag", start, end))


def moderately_slow_provider():
    time.sleep(0.08)
    return np.zeros((3, 4, 3), dtype=np.uint8)


def test_ready_is_read_only_and_mode_specific():
    physical = FakeHotkey()
    physical.focused = False
    adapter = DesktopAdapter(1, hotkey=physical, capture_provider=Provider())
    assert not adapter.ready()
    assert physical.events == [("size",), ("focus",)]

    message = FakeHotkey()
    message.focused = False
    adapter = DesktopAdapter(0, hotkey=message, capture_provider=Provider())
    assert adapter.ready()
    assert message.events == [("size",), ("ready",)]


def test_default_process_uses_exact_calibrated_capture_size(monkeypatch):
    made = []

    class Process:
        def __init__(self, *, capture_size):
            made.append(tuple(capture_size))

        def capture(self):
            return Frame(np.zeros((60, 80, 3), dtype=np.uint8))

        def close(self):
            pass

    monkeypatch.setattr(desktop, "CaptureProcess", Process)
    monkeypatch.setattr(
        "conf.conf_manager.ConfManager.get",
        lambda *_args, **_kwargs: {"capture_size": [80, 60]},
    )

    adapter = DesktopAdapter(0, hotkey=FakeHotkey())
    assert made == [(80, 60)]
    adapter.close()


def test_observe_uses_one_frame_for_union_and_returns_domain_values(monkeypatch):
    provider = Provider()
    seen_frames = []

    class Bars:
        def __init__(self, name):
            self.name = name

        def scan(self, anchor, color, full, *, frame):
            seen_frames.append(frame)
            return 75 if self.name == "Health" else None

    def locate(template, *, Region=None, Precision=None, frame=None):
        seen_frames.append(frame)
        if "MapSettings" in template:
            return Match(17, 17, 6, 6, 0.8)
        if "Targets" in template:
            return Match(3, 4, 10, 6, 0.9)
        if "NotFollow" in template:
            return Match(4, 5, 8, 6, 0.8)
        if "NoRing" in template or "Hur" in template:
            return Match(0, 0, 2, 2, 0.9)
        return None

    def locate_all(template, *, Region=None, Precision=None, frame=None):
        seen_frames.append(frame)
        return [Match(1, 2, 10, 6, 0.9), Match(20, 22, 10, 6, 0.88)]

    def selected(region, *, frame, target):
        seen_frames.append(frame)
        assert target == "Orc"
        return True

    def need_follow(*, frame):
        seen_frames.append(frame)
        return True

    monkeypatch.setattr(desktop, "ScanStages", Bars)
    monkeypatch.setattr(desktop, "LocateMatch", locate)
    monkeypatch.setattr(desktop, "LocateMatches", locate_all)
    monkeypatch.setattr(desktop.Scanners, "target_selected", selected)
    monkeypatch.setattr(desktop.Scanners, "NeedFollow", need_follow)

    sensors = [
        Sensor(SensorKind.HEALTH, (1, 1)),
        Sensor(SensorKind.MANA, (1, 2)),
        Sensor(SensorKind.RING_EMPTY, (0, 0, 20, 20)),
        Sensor(SensorKind.AMULET_EMPTY, (0, 0, 20, 20)),
        Sensor(SensorKind.HASTED, (0, 0, 20, 20)),
        Sensor(SensorKind.HUNGRY, (0, 0, 20, 20)),
        Sensor(SensorKind.TARGET, (50, 30, 170, 100), "Orc"),
        Sensor(SensorKind.MARKER, (20, 40, 60, 80), "Flag"),
        Sensor(SensorKind.FOLLOW),
    ]
    batch = DesktopAdapter(
        0, hotkey=FakeHotkey(), capture_provider=provider, clock=Clock()
    ).observe(sensors, Token())

    assert provider.calls == 1
    assert all(frame is batch.frame for frame in seen_frames)
    assert batch.observed_at == 10.0
    assert batch.readings[sensors[0]].value == 75
    assert batch.readings[sensors[1]].quality is Quality.UNKNOWN
    assert batch.readings[sensors[2]].value is True
    assert batch.readings[sensors[3]].value is False
    assert batch.readings[sensors[4]].value is True
    assert batch.readings[sensors[5]].value is False
    assert batch.readings[sensors[6]].value == Target(2, (58, 37), True)
    assert batch.readings[sensors[7]].value == Marker((40, 60), True)
    assert batch.readings[sensors[8]].value == (8, 8)


def test_observed_at_is_capture_start_and_detector_errors_fail_closed(monkeypatch):
    clock = Clock(31)
    provider = Provider(clock)
    sensor = Sensor(SensorKind.RING_EMPTY, (0, 0, 20, 20))

    def broken(*args, **kwargs):
        raise OSError("template unreadable")

    monkeypatch.setattr(desktop, "LocateMatch", broken)
    batch = DesktopAdapter(
        0, hotkey=FakeHotkey(), capture_provider=provider, clock=clock
    ).observe([sensor], Token())

    assert batch.observed_at == 31
    assert clock.value == 38
    assert batch.readings[sensor].quality is Quality.ERROR
    assert batch.readings[sensor].value is None
    assert "ring_empty detector failed" in batch.readings[sensor].detail


def test_process_timestamp_excludes_lazy_startup_but_includes_capture_delay(monkeypatch):
    capture = CaptureProcess(moderately_slow_provider, timeout=5, capture_size=(4, 3))
    original_start = capture._start

    def delayed_start():
        time.sleep(0.08)
        original_start()

    monkeypatch.setattr(capture, "_start", delayed_start)
    adapter = DesktopAdapter(0, hotkey=FakeHotkey(), capture_provider=capture)
    called_at = time.monotonic()
    try:
        batch = adapter.observe([], Token())
        returned_at = time.monotonic()
    finally:
        adapter.close()

    # Worker startup happened before the capture timestamp.
    assert batch.observed_at - called_at >= 0.06
    # Slow provider work happened after it, preserving conservative freshness.
    assert returned_at - batch.observed_at >= 0.06


def test_repeated_equivalent_sensor_is_detected_once(monkeypatch):
    calls = 0

    def locate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(desktop, "LocateMatch", locate)
    sensor = Sensor(SensorKind.HUNGRY, (0, 0, 20, 20))
    batch = DesktopAdapter(
        0, hotkey=FakeHotkey(), capture_provider=Provider()
    ).observe([sensor, sensor], Token())
    assert calls == 1
    assert batch.readings[sensor].value is False


def test_execute_maps_only_public_actions_and_restores_valid_physical_cursor():
    token = Token()
    hotkey = FakeHotkey()
    adapter = DesktopAdapter(1, hotkey=hotkey, capture_provider=Provider())

    adapter.execute(Action(ActionKind.PRESS, key="F1"), token)
    adapter.execute(Action(ActionKind.RIGHT_CLICK, point=(4, 5)), token)
    adapter.execute(Action(ActionKind.DRAG, point=(1, 2), destination=(8, 9)), token)

    assert ("press", "F1") in hotkey.events
    assert ("right", 4, 5) in hotkey.events
    assert ("drag", (1, 2), (8, 9)) in hotkey.events
    assert hotkey.events.count(("move", 17, 23)) == 2


def test_cancelled_mouse_action_never_restores_cursor():
    token = Token()
    hotkey = FakeHotkey(token)
    adapter = DesktopAdapter(1, hotkey=hotkey, capture_provider=Provider())

    try:
        adapter.execute(Action(ActionKind.LEFT_CLICK, point=(4, 5)), token)
    except RuntimeError as exc:
        assert str(exc) == "cancelled"
    else:
        raise AssertionError("cancelled action did not raise")
    assert ("left", 4, 5) in hotkey.events
    assert not any(event[0] == "move" for event in hotkey.events)


def test_close_closes_capture_and_blocks_later_operations():
    provider = Provider()
    adapter = DesktopAdapter(0, hotkey=FakeHotkey(), capture_provider=provider)
    adapter.close()
    adapter.close()
    assert provider.closed
    assert not adapter.ready()
    try:
        adapter.execute(Action(ActionKind.PRESS, key="F1"), Token())
    except DesktopAdapterError as exc:
        assert "closed" in str(exc)
    else:
        raise AssertionError("closed adapter accepted input")
