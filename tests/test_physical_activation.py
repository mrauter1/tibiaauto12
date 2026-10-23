"""Production focus, pulse, mapping and lifecycle regressions without live input."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from conf.Hotkeys import Hotkey
from conf.conf_manager import ConfManager
from core import runtime
import core.MoveMouse as physical
from test_input import FakePhysicalBackend
from test_sdk_controls import Adapter, Clock, Feature
from test_sdk_desktop import Provider, Token
from tibia_client.compatibility import ProbeFeature
from tibia_client.desktop import DesktopAdapter
from tibia_client.models import Action, ActionKind
from tibia_client.session import ClientSession
from tibia_client import window_target


@pytest.mark.parametrize("key,code", [("O", 0x4F), ("o", 0x4F), ("f3", 0x72), ("F3", 0x72)])
def test_both_native_backends_accept_case_insensitive_keys(key, code):
    from core.SendToClient import SendToClient
    assert physical.MoveMouse._key_code(key) == code
    assert SendToClient._key_code(key) == code


def test_keyboard_session_runs_real_timed_spells_without_image_calibration(monkeypatch):
    from engine.support import TimedSpell
    from features.support import TimedSpellsFeature
    from tibia_client.desktop import KeyboardDesktopAdapter, DesktopAdapterError
    from tibia_client.models import Sensor, SensorKind

    monkeypatch.setattr(ConfManager, "get", lambda filename: {
        "platform": "windows", "input_hwnd": 41, "capture_size": [800, 600]})
    native = Native()
    monkeypatch.setattr(physical, "_WindowsPhysicalBackend", lambda: native)
    monkeypatch.setattr(physical, "wait", lambda seconds: None)
    adapter = KeyboardDesktopAdapter(1, hotkey=Hotkey(1), capture_provider=Provider())
    clock = Clock()
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(TimedSpellsFeature((TimedSpell("Ctrl + O", 3600),), immediate=True))
    session.tick_once()
    assert ("key", 0x4F, False) in native.events
    assert adapter.capabilities().sensors == (SensorKind.READINGS.value,)
    with pytest.raises(DesktopAdapterError, match="image calibration"):
        adapter.observe((Sensor(SensorKind.FRAME_VALID),), Token())
    with pytest.raises(DesktopAdapterError, match="key presses"):
        adapter.execute(Action(ActionKind.LEFT_CLICK, point=(1, 2)), Token())
    session.close()


class Native(FakePhysicalBackend):
    def activate_window(self, hwnd):
        self.events.append(("activate", hwnd))
        self.foreground = hwnd

    def is_window(self, hwnd):
        pytest.fail("availability preflight")

    def foreground_window(self):
        pytest.fail("foreground preflight")

    def client_size(self, hwnd):
        pytest.fail("size preflight")


@pytest.mark.parametrize("sandbox,expected", [(False, (20, 30)), (True, (120, 110))])
def test_production_factory_activates_bound_target_then_dispatches_without_refocusing(
    monkeypatch, sandbox, expected
):
    config = {"platform": "windows", "input_hwnd": 41, "capture_hwnd": 41,
              "capture_size": [800, 600]}
    if sandbox:
        config["window_target"] = window_target.WindowTarget.build(
            (1000, 720), (100, 80, 900, 680)).to_dict()
    monkeypatch.setattr(ConfManager, "get", lambda filename: config)
    native = Native()
    monkeypatch.setattr(physical, "_WindowsPhysicalBackend", lambda: native)
    monkeypatch.setattr(physical, "wait", lambda seconds: native.events.append(("hold", seconds)))
    monkeypatch.setattr(window_target, "_native_backend", lambda:
                        SimpleNamespace(physical_pixels=nullcontext))
    hotkey = Hotkey(1)
    # A later config mutation must not silently redirect this session's target.
    config["input_hwnd"] = 99
    adapter = DesktopAdapter(1, hotkey=hotkey, capture_provider=Provider())
    assert adapter.activate_input()
    adapter.execute(Action(ActionKind.PRESS, key="ctrl+o"), Token())
    adapter.execute(Action(ActionKind.LEFT_CLICK, point=(20, 30)), Token())
    assert native.events[:6] == [
        ("activate", 41), ("key", 0x11, False), ("key", 0x4F, False),
        ("hold", 0.05), ("key", 0x4F, True), ("key", 0x11, True),
    ]
    assert ("client_to_screen", 41, expected) in native.events
    assert [e for e in native.events if e[0] == "activate"] == [("activate", 41)]
    assert native.cursor == (130, 260)
    assert adapter.last_error_diagnostic is None
    adapter.close()


def test_activation_failure_diagnoses_once_and_never_dispatches():
    native = Native()
    error = OSError("focus denied")

    def fail(hwnd):
        native.events.append(("activate", hwnd))
        raise error

    native.activate_window = fail
    client = physical.MoveMouse(backend=native, hwnd_resolver=lambda: 41,
                                error_diagnostic=lambda hwnd, exc:
                                native.events.append(("diagnose", hwnd)) or {"hwnd": hwnd})
    with pytest.raises(OSError) as caught:
        client.Activate()
    assert caught.value is error
    assert native.events == [("activate", 41), ("diagnose", 41)]
    assert error.client_availability == {"hwnd": 41}


def test_cancellation_during_production_pulse_releases_both_keys(monkeypatch):
    native = Native()
    token = runtime.CancellationToken("pulse", 1)

    def cancel(seconds):
        assert seconds == 0.05
        token.cancel()
        token.checkpoint()

    monkeypatch.setattr(physical, "wait", cancel)
    client = physical.MoveMouse(backend=native, hwnd_resolver=lambda: 41,
                                error_diagnostic=lambda *args: pytest.fail("cancel is not native failure"))
    with runtime.use_token(token), pytest.raises(runtime.CancelledError):
        client.PressHotkey("ctrl", "o")
    assert native.events == [("key", 0x11, False), ("key", 0x4F, False),
                             ("key", 0x4F, True), ("key", 0x11, True)]


@pytest.mark.parametrize("failure", [False, True])
def test_native_coordinate_conversion_uses_physical_pixels_and_restores_dpi(failure):
    import ctypes
    from ctypes import wintypes

    native = physical._WindowsPhysicalBackend.__new__(physical._WindowsPhysicalBackend)
    native.ctypes, native.wintypes = ctypes, wintypes
    state = {"dpi": 777}
    contexts = []

    def dpi(value):
        value = value.value if isinstance(value, ctypes.c_void_p) else value
        previous = state["dpi"]
        state["dpi"] = value
        contexts.append(value)
        return previous

    def translate(hwnd, point):
        assert state["dpi"] == ctypes.c_void_p(-4).value
        assert hwnd == 41
        point._obj.x -= 1920
        point._obj.y += 100
        return not failure

    native.user32 = SimpleNamespace(SetThreadDpiAwarenessContext=dpi, ClientToScreen=translate)
    if failure:
        with pytest.raises(physical.InputUnavailableError, match="translate"):
            native.client_to_screen(41, (20, 30))
    else:
        assert native.client_to_screen(41, (20, 30)) == (-1900, 130)
    assert contexts == [ctypes.c_void_p(-4).value, 777] and state["dpi"] == 777


class ActivatingAdapter(Adapter):
    availability_policy = "on_error"

    def __init__(self, clock):
        super().__init__(clock)
        self.activations = 0
        self.activation_error = None

    def activate_input(self):
        self.activations += 1
        if self.activation_error:
            raise self.activation_error

    def ready(self):
        pytest.fail("availability must not be polled")


def test_session_activation_is_per_start_and_resume_not_per_tick():
    clock = Clock()
    adapter = ActivatingAdapter(clock)
    session = ClientSession(adapter, clock=clock, autostart=False)
    feature = Feature("CaveBot", ("F1", "F2"))
    generation = session.start_feature(feature)
    assert session.start_feature(feature) == generation
    assert adapter.activations == 1
    for _ in range(5):
        session.tick_once()
        clock.value += 0.05
    assert adapter.activations == 1 and len(adapter.actions) == 2
    assert session.pause_navigation()
    assert session.resume_navigation()
    assert adapter.activations == 2
    assert not session.resume_navigation()
    session.stop_all()
    session.start_probe(ProbeFeature("press", Action(ActionKind.PRESS, key="F3")))
    assert adapter.activations == 3
    session.close()


def test_failed_activation_leaves_no_feature_or_queued_input():
    clock = Clock()
    adapter = ActivatingAdapter(clock)
    adapter.activation_error = RuntimeError("focus denied")
    session = ClientSession(adapter, clock=clock, autostart=False)
    with pytest.raises(RuntimeError, match="focus denied"):
        session.start_feature(Feature("test", ("F1",)))
    session.tick_once()
    assert not adapter.actions
    assert not session.feature_status("test").active
    session.close()


def test_failed_resume_activation_keeps_navigation_paused():
    clock = Clock()
    adapter = ActivatingAdapter(clock)
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(Feature("CaveBot", ("F1",)))
    session.pause_navigation()
    adapter.activation_error = RuntimeError("focus denied")
    with pytest.raises(RuntimeError, match="focus denied"):
        session.resume_navigation()
    session.tick_once()
    assert not adapter.actions
    assert session.feature_status("CaveBot").phase == "Paused"
    session.close()
