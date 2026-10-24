from __future__ import annotations

import numpy as np
from PIL import Image
import pytest
import time

from conf.Hotkeys import Hotkey
from core.perception import CaptureError, Frame, Match
from tibia_client import desktop
from tibia_client.desktop import DesktopAdapter, DesktopAdapterError
from tibia_client.capture import CaptureProcess
from tibia_client.adaptive_runtime import AdaptiveResourceTracker, CaptureInputMapping
from tibia_client.models import (
    Action, ActionKind, Marker, Quality, Reading, Sensor, SensorKind, Target,
)
from tibia_client.visual import VisualProfile


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
        pixels = np.zeros((120, 180, 3), dtype=np.uint8)
        pixels[0, 0, 0] = 1
        return Frame(pixels)

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


class ScreenHotkey(FakeHotkey):
    def __init__(self, token=None):
        super().__init__(token)
        self.screen_cursor = (-40, 160)
        self.fail_action = False
        self.lose_focus = False

    def ScreenPosition(self):
        if not self.focused:
            raise RuntimeError("focus lost")
        self.events.append(("screen_position",))
        return self.screen_cursor

    def RestoreScreenPosition(self, position):
        if not self.focused:
            raise RuntimeError("focus lost")
        self.events.append(("screen_restore", tuple(position)))
        self.screen_cursor = tuple(position)

    def RightClick(self, x, y):
        self.events.append(("right", x, y))
        self.screen_cursor = (x + 100, y + 200)
        if self.token is not None:
            self.token.cancelled = True
        if self.lose_focus:
            self.focused = False
        if self.fail_action:
            raise RuntimeError("click failed")


def moderately_slow_provider():
    time.sleep(0.08)
    pixels = np.zeros((3, 4, 3), dtype=np.uint8)
    pixels[0, 0, 0] = 1
    return pixels


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


def test_capabilities_declare_implemented_input_and_sensors_only():
    message = DesktopAdapter(0, hotkey=FakeHotkey(), capture_provider=Provider())
    physical = DesktopAdapter(1, hotkey=FakeHotkey(), capture_provider=Provider())

    assert "battle_list_click" in message.capabilities().operations
    assert "cursor_restore" not in message.capabilities().operations
    assert "cursor_restore" in physical.capabilities().operations
    expected = {kind.value for kind in SensorKind} - {SensorKind.VISUAL.value}
    assert set(message.capabilities().sensors) == expected
    assert message.evidence_source == "simulation"


def test_visual_profile_dispatches_on_observation_frame_and_can_be_reconfigured(tmp_path):
    context = np.asarray(
        [
            [[1, 90, 210], [220, 4, 30], [20, 200, 60]],
            [[250, 130, 9], [50, 70, 230], [170, 15, 150]],
        ],
        dtype=np.uint8,
    )
    target = np.asarray(
        [
            [[210, 10, 20], [10, 180, 60]],
            [[15, 220, 190], [230, 110, 30]],
        ],
        dtype=np.uint8,
    )
    Image.fromarray(context).save(tmp_path / "context.png")
    Image.fromarray(target).save(tmp_path / "target.png")
    profile = VisualProfile(
        {
            "trade.ready": {
                "kind": "template",
                "template": "target.png",
                "context": {"template": "context.png", "region": [0, 0, 8, 8]},
                "search_region": [8, 0, 20, 12],
            }
        },
        base_path=tmp_path,
    )

    class VisualProvider:
        def __init__(self):
            self.calls = 0

        def capture(self):
            self.calls += 1
            pixels = np.full((120, 180, 3), 31, dtype=np.uint8)
            pixels[2:4, 2:5] = context
            pixels[5:7, 11:13] = target
            return Frame(pixels)

    provider = VisualProvider()
    adapter = DesktopAdapter(
        0,
        hotkey=FakeHotkey(),
        capture_provider=provider,
        visual_profile=profile,
    )
    sensor = Sensor(SensorKind.VISUAL, (0, 0, 20, 12), "trade.ready")

    batch = adapter.observe([sensor], Token())

    assert provider.calls == 1
    assert adapter.visual_profile is profile
    assert SensorKind.VISUAL.value in adapter.capabilities().sensors
    assert batch.readings[sensor].quality is Quality.KNOWN
    assert batch.readings[sensor].value["position"] == (12, 6)
    adapter.configure_visual_profile(None)
    assert adapter.visual_profile.spec_ids == ()
    assert SensorKind.VISUAL.value not in adapter.capabilities().sensors


def test_visual_work_budget_returns_unknown_without_suppressing_other_sensors(
    tmp_path, monkeypatch
):
    context = np.asarray(
        [
            [[1, 90, 210], [220, 4, 30], [20, 200, 60]],
            [[250, 130, 9], [50, 70, 230], [170, 15, 150]],
        ],
        dtype=np.uint8,
    )
    target = np.asarray(
        [
            [[210, 10, 20], [10, 180, 60]],
            [[15, 220, 190], [230, 110, 30]],
        ],
        dtype=np.uint8,
    )
    Image.fromarray(context).save(tmp_path / "context.png")
    Image.fromarray(target).save(tmp_path / "target.png")
    profile = VisualProfile(
        {
            "ready": {
                "kind": "template",
                "template": "target.png",
                "context": {
                    "template": "context.png",
                    "region": [0, 0, 8, 8],
                },
                "search_region": [8, 0, 20, 12],
            }
        },
        base_path=tmp_path,
    )

    class TwoRegionProvider:
        def capture(self):
            pixels = np.full((120, 180, 3), 31, dtype=np.uint8)
            for offset in (0, 30):
                pixels[2:4, offset + 2 : offset + 5] = context
                pixels[5:7, offset + 11 : offset + 13] = target
            return Frame(pixels)

    monkeypatch.setattr(desktop, "MAX_VISUAL_WORK_PIXELS", 480)
    adapter = DesktopAdapter(
        0,
        hotkey=FakeHotkey(),
        capture_provider=TwoRegionProvider(),
        visual_profile=profile,
    )
    first = Sensor(SensorKind.VISUAL, (0, 0, 20, 12), "ready")
    over_budget = Sensor(SensorKind.VISUAL, (30, 0, 50, 12), "ready")
    heartbeat = Sensor(SensorKind.FRAME_VALID)

    batch = adapter.observe((first, over_budget, heartbeat), Token())

    assert batch.readings[first].quality is Quality.KNOWN
    assert batch.readings[first].value["present"] is True
    assert batch.readings[over_budget].quality is Quality.UNKNOWN
    assert "work budget" in batch.readings[over_budget].detail
    assert batch.readings[heartbeat] == Reading(True)


def test_adaptive_visuals_keep_sensor_identity_and_do_not_delay_health(tmp_path, monkeypatch):
    context = np.asarray(
        [[[10, 20, 30], [40, 50, 60]], [[70, 80, 90], [100, 110, 120]]],
        dtype=np.uint8,
    )
    Image.fromarray(context).save(tmp_path / "context.png")
    digit = np.asarray([[[255, 255, 255]]], dtype=np.uint8)
    for number in range(10):
        Image.fromarray(digit).save(tmp_path / f"{number}.png")
    profile = VisualProfile(
        {
            "resource_health": {
                "kind": "count",
                "context": {"template": "context.png", "region": [0, 0, 4, 4]},
                "count_region": [4, 0, 10, 6],
                "digits": {str(number): f"{number}.png" for number in range(10)},
                "adaptive": {
                    "reader": "paulistinha-skills-v1",
                    "field": "resource_health",
                },
            }
        },
        base_path=tmp_path,
    )
    order = []

    class Bars:
        def __init__(self, _name):
            pass

        def scan(self, *_args, **_kwargs):
            order.append("health")
            return 42

    class Tracker(AdaptiveResourceTracker):
        def read(self, _frame, requests):
            assert order == ["health"]
            order.append("adaptive")
            return {
                sensor: Reading(
                    {"present": True, "position": (4, 5), "count": 710, "score": 0.99},
                    diagnostics={"layout_revision": "panel-a"},
                )
                for sensor in requests
            }

    monkeypatch.setattr(desktop, "ScanStages", Bars)
    visual_sensor = Sensor(SensorKind.VISUAL, (0, 0, 10, 6), "resource_health")
    health_sensor = Sensor(SensorKind.HEALTH, (1, 1))
    adapter = DesktopAdapter(
        0,
        hotkey=FakeHotkey(),
        capture_provider=Provider(),
        visual_profile=profile,
        adaptive_tracker=Tracker(),
    )

    batch = adapter.observe((visual_sensor, health_sensor), Token())

    assert order == ["health", "adaptive"]
    assert batch.readings[health_sensor].value == 42
    assert batch.readings[visual_sensor].value["count"] == 710
    assert batch.readings[visual_sensor].value["position"] == (4, 5)
    assert batch.readings[visual_sensor].diagnostics["capture_position"] == (4, 5)
    assert set(batch.readings) == {health_sensor, visual_sensor}

    class BrokenTracker(AdaptiveResourceTracker):
        def read(self, _frame, _requests):
            raise RuntimeError("worker bookkeeping failed")

    broken = DesktopAdapter(
        0,
        hotkey=FakeHotkey(),
        capture_provider=Provider(),
        visual_profile=profile,
        adaptive_tracker=BrokenTracker(),
    ).observe((visual_sensor, health_sensor), Token())
    assert broken.readings[health_sensor].value == 42
    assert broken.readings[visual_sensor].quality is Quality.UNKNOWN
    assert "tracker failed" in broken.readings[visual_sensor].detail


def test_desktop_rejects_nonidentity_capture_input_mapping_before_observation():
    mapping = CaptureInputMapping((180, 120), (360, 240))

    with pytest.raises(ValueError, match="only 1:1 capture/input mapping"):
        DesktopAdapter(
            0,
            hotkey=FakeHotkey(),
            capture_provider=Provider(),
            coordinate_mapping=mapping,
        )


def test_live_evidence_provenance_requires_default_native_composition(monkeypatch):
    class Process:
        provider = None
        capture_size = (180, 120)

        def __init__(self, *, capture_size, startup_timeout):
            self.capture_size = tuple(capture_size)
            assert startup_timeout == 15.0

        def capture(self):
            pixels = np.ones((120, 180, 3), dtype=np.uint8)
            return Frame(pixels)

        def close(self):
            pass

    monkeypatch.setattr(desktop, "CaptureProcess", Process)
    monkeypatch.setattr(desktop, "Hotkey", lambda _mode: FakeHotkey())
    monkeypatch.setattr(
        "conf.conf_manager.ConfManager.get",
        lambda *_args, **_kwargs: {"capture_size": [180, 120]},
    )

    adapter = DesktopAdapter(0)
    assert adapter.evidence_source == "live"
    adapter.close()


def test_default_process_uses_exact_calibrated_capture_size(monkeypatch):
    made = []

    class Process:
        def __init__(self, *, capture_size, startup_timeout):
            assert startup_timeout == 15.0
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

    def locate(template, *, Region=None, Precision=None, frame=None, Scale=1.0):
        seen_frames.append(frame)
        if "NoRing" in template or "Hur" in template:
            return Match(0, 0, 2, 2, 0.9)
        return None

    def target(frame, region, name, scale, reader):
        seen_frames.append(frame)
        assert region == (50, 30, 170, 100) and name == "Orc"
        return Reading(Target(2, (58, 37), True))

    def marker(frame, region, name, scale):
        seen_frames.append(frame)
        assert region == (20, 40, 60, 80) and name == "Flag"
        return Reading(Marker((40, 60), True))

    def follow(sensor, frame, scale=1.0, matches=None, *, observation_cycle=None):
        seen_frames.append(frame)
        return Reading((8, 8))

    monkeypatch.setattr(desktop, "ScanStages", Bars)
    monkeypatch.setattr(desktop, "LocateMatch", locate)
    monkeypatch.setattr("tibia_client.battle_text.read_target", target)
    monkeypatch.setattr("tibia_client.visual_readings.read_marker", marker)
    monkeypatch.setattr(DesktopAdapter, "_follow", staticmethod(follow))

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


def test_frame_valid_is_capture_backed_and_exact_black_fails_transport():
    sensor = Sensor(SensorKind.FRAME_VALID)
    batch = DesktopAdapter(
        0, hotkey=FakeHotkey(), capture_provider=Provider()
    ).observe([sensor], Token())
    assert batch.readings[sensor] == Reading(True)

    class BlackProvider:
        def capture(self):
            return Frame(np.zeros((120, 180, 3), dtype=np.uint8))

    adapter = DesktopAdapter(0, hotkey=FakeHotkey(), capture_provider=BlackProvider())
    with pytest.raises(CaptureError, match="uniform black"):
        adapter.observe([sensor], Token())


def test_process_timestamp_excludes_lazy_startup_but_includes_capture_delay(monkeypatch):
    capture = CaptureProcess(moderately_slow_provider, timeout=5, startup_timeout=15,
                             capture_size=(4, 3))
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
    assert adapter.last_cursor_transition == ((17, 23), (17, 23))


@pytest.mark.parametrize("click,start", [((150, 30), (17, 23)), ((40, 50), (150, 30))])
def test_control_click_or_cursor_over_controls_parks_inside_client(click, start):
    from types import SimpleNamespace

    hotkey = ScreenHotkey()
    hotkey.cursor = start
    adapter = DesktopAdapter(1, hotkey=hotkey, capture_provider=Provider())
    adapter._follow_matches['controls'] = SimpleNamespace(
        parking_geometry=lambda: ((140, 10, 170, 70), (4, 116)))
    adapter.execute(Action(ActionKind.LEFT_CLICK, point=click), Token())
    assert ("left", *click) in hotkey.events
    assert ("move", 4, 116) in hotkey.events
    assert not any(event[0] == "screen_restore" for event in hotkey.events)
    assert hotkey.events.index(("left", *click)) < hotkey.events.index(("move", 4, 116))


def test_hotkey_facade_falls_back_when_wrapped_backend_lacks_screen_restore():
    class LegacyBackend:
        def __init__(self):
            self.cursor = (17, 23)
            self.events = []

        def MainWindowSize(self):
            return 180, 120

        def IsFocused(self):
            return True

        def Position(self):
            return self.cursor

        def RightClick(self, position):
            self.events.append(("right", position))
            self.cursor = tuple(position)

        def MoveTo(self, x, y):
            self.events.append(("move", x, y))
            self.cursor = (x, y)

    backend = LegacyBackend()
    hotkey = Hotkey(1, backend=backend)
    adapter = DesktopAdapter(1, hotkey=hotkey, capture_provider=Provider())

    adapter.execute(Action(ActionKind.RIGHT_CLICK, point=(4, 5)), Token())

    assert not hotkey.SupportsScreenRestore()
    assert backend.events == [("right", (4, 5)), ("move", 17, 23)]
    assert adapter.last_cursor_transition == ((17, 23), (17, 23))


@pytest.mark.parametrize(
    "screen_cursor",
    ((-1, 240), (1000, 240), (240, -1), (240, 1000)),
)
def test_physical_cursor_restore_uses_screen_coordinates_outside_client(screen_cursor):
    hotkey = ScreenHotkey()
    hotkey.screen_cursor = screen_cursor
    adapter = DesktopAdapter(1, hotkey=hotkey, capture_provider=Provider())

    adapter.execute(Action(ActionKind.RIGHT_CLICK, point=(4, 5)), Token())

    assert ("screen_restore", screen_cursor) in hotkey.events
    assert hotkey.screen_cursor == screen_cursor
    assert adapter.last_cursor_transition == (screen_cursor, screen_cursor)


def test_failed_action_still_restores_screen_cursor():
    hotkey = ScreenHotkey()
    hotkey.fail_action = True
    original = hotkey.screen_cursor
    adapter = DesktopAdapter(1, hotkey=hotkey, capture_provider=Provider())

    with pytest.raises(DesktopAdapterError, match="click failed"):
        adapter.execute(Action(ActionKind.RIGHT_CLICK, point=(4, 5)), Token())

    assert ("screen_restore", original) in hotkey.events
    assert hotkey.screen_cursor == original


def test_focus_loss_during_action_prevents_screen_cursor_restore():
    hotkey = ScreenHotkey()
    hotkey.lose_focus = True
    adapter = DesktopAdapter(1, hotkey=hotkey, capture_provider=Provider())

    adapter.execute(Action(ActionKind.RIGHT_CLICK, point=(4, 5)), Token())

    assert not any(event[0] == "screen_restore" for event in hotkey.events)
    assert adapter.last_cursor_transition is None


def test_cancelled_action_prevents_screen_cursor_restore():
    token = Token()
    hotkey = ScreenHotkey(token)
    adapter = DesktopAdapter(1, hotkey=hotkey, capture_provider=Provider())

    with pytest.raises(RuntimeError, match="cancelled"):
        adapter.execute(Action(ActionKind.RIGHT_CLICK, point=(4, 5)), token)

    assert not any(event[0] == "screen_restore" for event in hotkey.events)


def test_dispatch_does_not_poll_geometry_after_capture():
    hotkey = FakeHotkey()
    adapter = DesktopAdapter(1, hotkey=hotkey, capture_provider=Provider())
    adapter.observe([], Token())
    hotkey.size = (181, 120)

    adapter.execute(Action(ActionKind.PRESS, key="F1"), Token())
    assert hotkey.events == [("press", "F1")]
    assert adapter.last_error_diagnostic is None


def test_input_failure_checks_availability_after_error_without_retry():
    class FailingHotkey(FakeHotkey):
        def Press(self, key):
            self.events.append(("press", key))
            raise OSError("key dispatch failed")

    hotkey = FailingHotkey()
    adapter = DesktopAdapter(0, hotkey=hotkey, capture_provider=Provider())
    with pytest.raises(DesktopAdapterError, match="key dispatch failed"):
        adapter.execute(Action(ActionKind.PRESS, key="F1"), Token())
    assert hotkey.events == [("press", "F1"), ("ready",)]
    assert adapter.last_error_diagnostic == {"is_window": True}


def test_input_native_failure_diagnostic_is_reused_without_another_query():
    failure = OSError("key dispatch failed")
    failure.client_availability = {"hwnd": 41, "is_window": False}

    class FailingHotkey(FakeHotkey):
        def Press(self, key):
            self.events.append(("press", key))
            raise failure

    hotkey = FailingHotkey()
    adapter = DesktopAdapter(0, hotkey=hotkey, capture_provider=Provider())
    with pytest.raises(DesktopAdapterError) as caught:
        adapter.execute(Action(ActionKind.PRESS, key="F1"), Token())
    assert caught.value.__cause__ is failure
    assert hotkey.events == [("press", "F1")]
    assert adapter.last_error_diagnostic == failure.client_availability


def test_capture_failure_checks_availability_only_after_provider_failure():
    events = []

    class FailingProvider:
        def capture(self):
            events.append("capture")
            raise OSError("capture failed")

    class DiagnosticHotkey(FakeHotkey):
        def IsReady(self):
            events.append("diagnose")
            return False

    adapter = DesktopAdapter(0, hotkey=DiagnosticHotkey(), capture_provider=FailingProvider())
    with pytest.raises(CaptureError, match="capture failed"):
        adapter.observe([], Token())
    assert events == ["capture", "diagnose"]


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
