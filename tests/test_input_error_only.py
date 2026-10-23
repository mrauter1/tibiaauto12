"""Successful actions never poll availability; native errors diagnose once."""

from contextlib import contextmanager

import pytest

from conf.Hotkeys import Hotkey
from core import runtime
from core.MoveMouse import MoveMouse
from core.SendToClient import SendToClient
from test_input import FakeMessageBackend, FakePhysicalBackend
from tibia_client import window_target as wt


class NoAvailabilityPhysical(FakePhysicalBackend):
    def is_window(self, hwnd):
        pytest.fail("availability queried before an operation error")

    def foreground_window(self):
        pytest.fail("foreground queried before an operation error")

    def client_size(self, hwnd):
        pytest.fail("live geometry queried before an operation error")


class NoAvailabilityMessages(FakeMessageBackend):
    is_window = NoAvailabilityPhysical.is_window
    foreground_window = NoAvailabilityPhysical.foreground_window
    client_size = NoAvailabilityPhysical.client_size


@pytest.mark.parametrize("method,args", [
    ("Press", ("F1",)), ("PressHotkey", ("ctrl", "F2")),
    ("LeftClick", ((2, 3),)), ("RightClick", ((2, 3),)),
    ("MoveTo", (2, 3)), ("DragTo", ((2, 3), (4, 5))),
    ("UseOn", ((2, 3), (4, 5))), ("Position", ()),
])
@pytest.mark.parametrize("cls,native", [
    (MoveMouse, NoAvailabilityPhysical), (SendToClient, NoAvailabilityMessages),
])
def test_default_success_dispatches_without_any_availability_poll(cls, native, method, args):
    backend = native()
    options = dict(backend=backend, hwnd_resolver=lambda: 41,
                   expected_size_resolver=lambda: (800, 600))
    if cls is MoveMouse:
        options["readiness_guard"] = lambda: pytest.fail("visual guard queried")
    client = cls(**options)
    getattr(client, method)(*args)
    assert client.last_error_diagnostic is None


def test_physical_cursor_restore_does_not_poll_focus_or_geometry():
    backend = NoAvailabilityPhysical()
    mouse = MoveMouse(backend=backend, hwnd_resolver=lambda: 41)
    mouse.RestoreScreenPosition(mouse.ScreenPosition())
    assert backend.cursor == (130, 260)


@pytest.mark.parametrize("cls,base,event,down,up", [
    (MoveMouse, FakePhysicalBackend, "mouse_event", MoveMouse.MOUSEEVENTF_LEFTDOWN,
     MoveMouse.MOUSEEVENTF_LEFTUP),
    (SendToClient, FakeMessageBackend, "send_message", SendToClient.WM_LBUTTONDOWN,
     SendToClient.WM_LBUTTONUP),
])
def test_failed_down_diagnoses_once_after_release_without_retry(cls, base, event, down, up):
    timeline = []
    original = OSError("native dispatch failed")

    class Backend(base):
        def mouse_event(self, flags):
            timeline.append(flags)
            if flags == down:
                raise original

        def send_message(self, hwnd, message, *args, **kwargs):
            timeline.append(message)
            if message == down:
                raise original

    def diagnostic(hwnd, error):
        assert (hwnd, error) == (41, original)
        timeline.append("diagnostic")
        return {"is_window": False}

    client = cls(backend=Backend(), hwnd_resolver=lambda: 41,
                 error_diagnostic=diagnostic)
    with pytest.raises(OSError) as caught:
        client.LeftClick((2, 3))
    assert caught.value is original
    assert timeline == [down, up, "diagnostic"]
    assert client.last_error_diagnostic == {"is_window": False}
    assert original.client_availability == client.last_error_diagnostic


def test_release_and_diagnostic_errors_do_not_replace_original_failure():
    original = OSError("DOWN failed")
    events = []

    class Backend(FakePhysicalBackend):
        def key_event(self, key, key_up):
            events.append((key, key_up))
            if key_up:
                raise TimeoutError("UP failed")
            raise original

    def diagnostic(*args):
        events.append("diagnostic")
        raise OSError("diagnostic failed")

    mouse = MoveMouse(backend=Backend(), hwnd_resolver=lambda: 41,
                      error_diagnostic=diagnostic)
    with pytest.raises(OSError) as caught:
        mouse.Press("F1")
    assert caught.value is original
    assert events == [(0x70, False), (0x70, True), "diagnostic"]
    assert "diagnostic_error" in mouse.last_error_diagnostic


def test_default_diagnostic_queries_availability_only_after_failed_dispatch_and_up():
    timeline = []
    original = OSError("injection failed")

    class Backend(FakePhysicalBackend):
        def key_event(self, key, key_up):
            timeline.append((key, key_up))
            if not key_up:
                raise original

        def is_window(self, hwnd):
            timeline.append(("is_window", hwnd))
            return False

        def foreground_window(self):
            timeline.append("foreground")
            return 99

        def client_size(self, hwnd):
            timeline.append(("size", hwnd))
            raise OSError("client closed")

    mouse = MoveMouse(backend=Backend(), hwnd_resolver=lambda: 41)
    with pytest.raises(OSError) as caught:
        mouse.Press("F1")
    assert caught.value is original
    assert timeline == [(0x70, False), (0x70, True), ("is_window", 41),
                        "foreground", ("size", 41)]
    assert mouse.last_error_diagnostic["is_window"] is False
    assert mouse.last_error_diagnostic["foreground_hwnd"] == 99
    assert "client closed" in mouse.last_error_diagnostic["client_size"]


def test_compound_failure_releases_key_and_modifier_then_diagnoses_once():
    timeline = []
    original = OSError("key DOWN failed")

    class Backend(FakePhysicalBackend):
        def key_event(self, key, key_up):
            timeline.append((key, key_up))
            if key == 0x70 and not key_up:
                raise original

    mouse = MoveMouse(backend=Backend(), hwnd_resolver=lambda: 41,
                      error_diagnostic=lambda *_: timeline.append("diagnostic"))
    with pytest.raises(OSError) as caught:
        mouse.PressHotkey("ctrl", "F1")
    assert caught.value is original
    assert timeline == [(0x11, False), (0x70, False), (0x70, True), (0x11, True), "diagnostic"]


def test_cancel_still_releases_without_availability_diagnostic():
    backend = NoAvailabilityPhysical()
    backend.cancel_on_first_down = True
    mouse = MoveMouse(backend=backend, hwnd_resolver=lambda: 41,
                      error_diagnostic=lambda *_: pytest.fail("cancel is not native failure"))
    token = runtime.CancellationToken("error-only-cancel", 1)
    with runtime.use_token(token), pytest.raises(runtime.CancelledError):
        mouse.PressHotkey("ctrl", "F1")
    assert backend.events == [("key", 0x11, False), ("key", 0x11, True)]


def test_invalid_coordinates_are_rejected_from_configuration_without_diagnostics():
    mouse = MoveMouse(backend=NoAvailabilityPhysical(), hwnd_resolver=lambda: 41,
                      expected_size_resolver=lambda: (800, 600),
                      error_diagnostic=lambda *_: pytest.fail("argument error is not native failure"))
    with pytest.raises(ValueError, match="outside"):
        mouse.DragTo((2, 3), (800, 600))
    assert mouse.backend.events == []


def test_sandbox_mapping_uses_no_focus_samples_capture_or_live_size(monkeypatch):
    class Native:
        @contextmanager
        def physical_pixels(self):
            yield

    monkeypatch.setattr(wt, "_native_backend", Native)
    monkeypatch.setattr(wt, "capture_target", lambda *_: pytest.fail("guard captured"))
    configured = wt.WindowTarget.build((1000, 800), (10, 20, 810, 620))
    assert configured.focus_guard is None
    assert wt.WindowTarget.from_config({"window_target": configured.to_dict()}) == configured
    native = NoAvailabilityPhysical()
    backend = MoveMouse(backend=native, hwnd_resolver=lambda: 41,
                        expected_size_resolver=lambda: (1000, 800))
    wrapper = wt.TargetInput(backend, configured, lambda: 41)
    assert wrapper.MainWindowSize() == (800, 600)
    wrapper.LeftClick((2, 3))
    assert ("client_to_screen", 41, (12, 23)) in native.events
    assert wrapper.last_error_diagnostic is None
    with pytest.raises(wt.WindowTargetError, match="legacy readiness"):
        wrapper.check_ready()


def test_hotkey_wait_entry_point_is_immediate_and_never_queries_backend():
    class NoChecks:
        def IsFocused(self):
            pytest.fail("wait polled focus")

        def IsReady(self):
            pytest.fail("wait polled readiness")

    for mode in (0, 1):
        assert Hotkey(mode, backend=NoChecks()).wait_until_ready(timeout=0)
