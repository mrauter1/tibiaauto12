"""Guest readiness can fail between physical action phases without stuck keys."""
from contextlib import contextmanager

import pytest

from conf.HexMapKeys import KeyToHex
from core import runtime
from core.MoveMouse import MoveMouse
from test_input import FakePhysicalBackend
from test_sandbox_integration import sandbox_target
from tibia_client import window_target as targets
from tibia_client.window_target import WindowTargetError


@pytest.fixture(autouse=True)
def stop_runtime():
    yield
    runtime.stop_all(timeout=1)


def mouse_with_guard(backend, unsafe):
    def guard():
        if unsafe():
            raise WindowTargetError("guest focus changed")
        return True

    return MoveMouse(preflight=True, backend=backend, hwnd_resolver=lambda: 41, readiness_guard=guard)


@pytest.mark.parametrize("method", ["LeftClick", "RightClick"])
def test_guest_focus_loss_after_cursor_move_prevents_button_down(method):
    backend = FakePhysicalBackend()
    mouse = mouse_with_guard(backend, lambda: any(e[0] == "move" for e in backend.events))

    with pytest.raises(WindowTargetError, match="guest focus changed"):
        getattr(mouse, method)((7, 9))

    assert ("move", (107, 209)) in backend.events
    assert not any(e[0] == "mouse" for e in backend.events)


def test_guest_focus_loss_after_modifier_down_releases_modifier_and_blocks_key():
    backend = FakePhysicalBackend()
    ctrl = KeyToHex["ctrl"]
    mouse = mouse_with_guard(backend, lambda: ("key", ctrl, False) in backend.events)

    with pytest.raises(WindowTargetError, match="guest focus changed"):
        mouse.PressHotkey("ctrl", "F12")

    assert backend.events == [("key", ctrl, False), ("key", ctrl, True)]


def test_guest_focus_loss_during_drag_releases_button_without_destination_move():
    backend = FakePhysicalBackend()
    mouse = mouse_with_guard(
        backend, lambda: ("mouse", MoveMouse.MOUSEEVENTF_LEFTDOWN) in backend.events
    )

    with pytest.raises(WindowTargetError, match="guest focus changed"):
        mouse.DragTo((7, 9), (100, 120))

    assert [e for e in backend.events if e[0] == "move"] == [("move", (107, 209))]
    assert [e for e in backend.events if e[0] == "mouse"] == [
        ("mouse", MoveMouse.MOUSEEVENTF_LEFTDOWN),
        ("mouse", MoveMouse.MOUSEEVENTF_LEFTUP),
    ]


def test_guest_focus_loss_during_use_on_releases_right_button_and_blocks_left_click():
    backend = FakePhysicalBackend()
    mouse = mouse_with_guard(
        backend, lambda: ("mouse", MoveMouse.MOUSEEVENTF_RIGHTDOWN) in backend.events
    )

    with pytest.raises(WindowTargetError, match="guest focus changed"):
        mouse.UseOn((7, 9), (100, 120))

    assert [e for e in backend.events if e[0] == "move"] == [("move", (107, 209))]
    assert [e for e in backend.events if e[0] == "mouse"] == [
        ("mouse", MoveMouse.MOUSEEVENTF_RIGHTDOWN),
        ("mouse", MoveMouse.MOUSEEVENTF_RIGHTUP),
    ]


def test_guest_focus_loss_prevents_absolute_cursor_restoration():
    backend = FakePhysicalBackend()
    safe = [True]
    mouse = mouse_with_guard(backend, lambda: not safe[0])
    previous = mouse.ScreenPosition()
    safe[0] = False

    with pytest.raises(WindowTargetError, match="guest focus changed"):
        mouse.RestoreScreenPosition(previous)

    assert not any(e[0] == "move" for e in backend.events)


@pytest.mark.parametrize("method, arguments, expected", [
    ("LeftClick", ((7, 9),), ((107, 89),)),
    ("DragTo", ((0, 0), (799, 599)), ((100, 80), (899, 679))),
    ("Position", (), ()),
    ("RestoreScreenPosition", ((-20, 900),), ((-20, 900),)),
])
def test_sandbox_dispatch_keeps_physical_pixel_context_through_guard_and_native_input(
    sandbox_target, monkeypatch, method, arguments, expected
):
    depth = [0]
    calls = []

    class Native:
        @contextmanager
        def physical_pixels(self):
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1

    native = Native()

    def guard_capture(hwnd, target):
        assert (hwnd, target) == (41, sandbox_target)
        assert depth[0] == 1
        # Real capture temporarily establishes its own DPI context, then
        # restores the caller's physical-pixel context before input proceeds.
        with native.physical_pixels():
            assert depth[0] == 2
        assert depth[0] == 1

    class Input:
        def __getattr__(self, name):
            def call(*args):
                assert depth[0] == 1
                calls.append((name, args))
                return (130, 140) if name == "Position" else None
            return call

    monkeypatch.setattr(targets, "_native_backend", lambda: native)
    monkeypatch.setattr(targets, "capture_target", guard_capture)
    wrapper = targets.TargetInput(Input(), sandbox_target, lambda: 41, preflight=True)
    result = getattr(wrapper, method)(*arguments)

    assert calls == [(method, expected)]
    assert depth[0] == 0
    if method == "Position":
        assert result == (30, 60)
