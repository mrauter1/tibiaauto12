"""Offline coverage for scaled selections and transactional Sandbox setup."""
from copy import deepcopy
import queue
from types import SimpleNamespace

from PIL import Image, ImageDraw
import pytest

import modules.SandboxSetup as sandbox_module
from modules.SandboxSetup import PreviewMapping, SandboxSelection, SandboxSetup, _rectangle
from tibia_client.window_target import WindowTarget


class Value:
    def __init__(self, value=""):
        self.value = value

    def set(self, value):
        self.value = value


class Window:
    def __init__(self):
        self.calls = []
        self.callbacks = []

    def after(self, milliseconds, callback):
        self.calls.append(("after", milliseconds))
        self.callbacks.append(callback)
        return str(len(self.callbacks))

    def after_cancel(self, timer):
        self.calls.append(("cancel", timer))

    def grab_release(self):
        self.calls.append("release")

    def grab_set(self):
        self.calls.append("grab")

    def withdraw(self):
        self.calls.append("withdraw")

    def deiconify(self):
        self.calls.append("show")

    def destroy(self):
        self.calls.append("destroy")


def images():
    active = Image.new("RGB", (320, 240), "#203040")
    inactive = active.copy()
    for image, color in ((active, "#116dcc"), (inactive, "#555555")):
        draw = ImageDraw.Draw(image)
        draw.rectangle((12, 10, 139, 29), fill=color)
        draw.rectangle((18, 14, 26, 23), fill="white")
    return active, inactive


def completed_selection():
    active, inactive = images()
    selection = SandboxSelection(require_focus_samples=True)
    selection.set_active(active)
    selection.select("viewport", (12, 30, 300, 220))
    selection.select("focus", (12, 10, 140, 30))
    selection.set_inactive(inactive)
    return selection


def headless_wizard(capture_provider=None, countdown_seconds=3):
    wizard = object.__new__(SandboxSetup)
    wizard.hwnd = 123
    wizard._capture_provider = (sandbox_module.capture_direct_host_client
                                if capture_provider is None else capture_provider)
    wizard.countdown_seconds = countdown_seconds
    wizard.selection = SandboxSelection()
    wizard.result = None
    wizard._closed = wizard._busy = False
    wizard._generation = 0
    wizard._timer = None
    wizard._queue = queue.Queue()
    wizard.status = Value()
    wizard.window = Window()
    wizard._refresh_controls = lambda: None
    wizard._render = lambda: None
    return wizard


def test_scaled_preview_maps_exclusive_edges_and_padding():
    mapping = PreviewMapping((660, 500), (1280, 960))
    left, top, width, height = mapping.bounds
    assert (width, height) == (640, 480)
    assert mapping.image_edge(left, top) == (0, 0)
    assert mapping.image_edge(left + width, top + height) == (1280, 960)
    assert mapping.image_edge(left + width / 4, top + height / 2) == (320, 480)
    assert mapping.image_edge(-500, 6000, clamp=True) == (0, 960)
    assert mapping.canvas_rectangle((320, 240, 960, 720)) == (
        left + 160, top + 120, left + 480, top + 360,
    )
    with pytest.raises(ValueError, match="inside"):
        mapping.image_edge(0, 0)
    assert _rectangle((1280, 960), (0, 0)) == (0, 0, 1280, 960)
    with pytest.raises(ValueError, match="at least"):
        _rectangle((1, 1), (1, 20))


def test_letterboxed_preview_never_maps_padding_into_game_coordinates():
    mapping = PreviewMapping((1000, 400), (800, 800))
    left, top, width, height = mapping.bounds
    assert width == height == 380
    assert left == 310 and top == 10
    assert not mapping.contains(left - 1, top)
    assert mapping.canvas_rectangle((0, 0, 800, 800)) == (
        left, top, left + width, top + height,
    )


def test_instruction_wrapping_tracks_available_width_and_ignores_closed_window():
    wizard = headless_wizard()
    configured = []
    wizard._wrapped_labels = [SimpleNamespace(configure=lambda **values: configured.append(values))]
    wizard._resize_text(SimpleNamespace(width=780))
    assert configured[-1] == {"wraplength": 756}
    wizard._resize_text(SimpleNamespace(width=1080))
    assert configured[-1] == {"wraplength": 1056}
    wizard._closed = True
    wizard._resize_text(SimpleNamespace(width=500))
    assert len(configured) == 2


def test_setup_builds_exact_target_with_host_relative_focus_patch():
    selection = completed_selection()
    target = selection.build_target()
    assert target["host_size"] == [320, 240]
    assert target["viewport"] == [12, 30, 300, 220]
    assert target["focus_guard"]["region"] == [12, 10, 140, 30]
    assert WindowTarget.from_config({"window_target": target}).size == (288, 190)
    target["viewport"][0] = 100
    assert selection.build_target()["viewport"][0] == 12


def test_default_setup_saves_viewport_without_focus_samples():
    selection = SandboxSelection()
    selection.set_active(images()[0])
    with pytest.raises(ValueError, match="select the guest Tibia client area"):
        selection.build_target()
    selection.select("viewport", (12, 30, 300, 220))
    assert selection.focus_region is None and selection.inactive is None
    assert selection.build_target() == {
        "kind": "sandbox", "host_size": [320, 240], "viewport": [12, 30, 300, 220],
    }
    wizard = headless_wizard()
    wizard.selection = selection
    wizard.save()
    assert wizard.result == selection.build_target() and wizard._closed


def test_viewport_only_recapture_keeps_geometry_and_resize_requires_reselection():
    selection = SandboxSelection()
    selection.set_active(images()[0])
    selection.select("viewport", (12, 30, 300, 220))
    selection.set_active(images()[1])
    assert selection.build_target()["viewport"] == [12, 30, 300, 220]
    selection.set_active(Image.new("RGB", (640, 480)))
    with pytest.raises(ValueError, match="select the guest Tibia client area"):
        selection.build_target()
    with pytest.raises(ValueError, match="inside"):
        selection.select("viewport", (0, 0, 641, 480))


def test_indistinct_or_resized_sample_does_not_replace_valid_draft():
    selection = completed_selection()
    before = selection.build_target()
    old_inactive = selection.inactive
    with pytest.raises(ValueError, match="distinguishable"):
        selection.set_inactive(selection.active)
    assert selection.inactive is old_inactive
    assert selection.build_target() == before
    with pytest.raises(ValueError, match="size changed"):
        selection.set_inactive(Image.new("RGB", (321, 240)))
    assert selection.build_target() == before


def test_recapture_resets_focus_evidence_and_geometry_change_clears_rectangles():
    selection = completed_selection()
    active, _ = images()
    selection.set_active(active)
    assert selection.viewport == (12, 30, 300, 220)
    assert selection.focus_region == (12, 10, 140, 30)
    assert selection.inactive is None
    with pytest.raises(ValueError, match="inactive"):
        selection.build_target()
    selection.set_active(Image.new("RGB", (640, 480)))
    assert selection.viewport is selection.focus_region is None


@pytest.mark.parametrize("countdown_seconds", [3, 10])
def test_countdown_withdraws_without_blocking_and_worker_uses_no_tk(monkeypatch, countdown_seconds):
    workers, captures = [], []

    class DeferredThread:
        def __init__(self, *, target, name, daemon):
            workers.append(target)
            assert name == "sandbox-setup-capture" and daemon

        def start(self):
            pass

    def capture(hwnd, expected_size=None):
        captures.append((hwnd, expected_size))
        return images()[1]

    monkeypatch.setattr(sandbox_module.threading, "Thread", DeferredThread)
    monkeypatch.setattr(sandbox_module, "capture_direct_host_client", capture)
    wizard = headless_wizard(countdown_seconds=countdown_seconds)
    wizard.selection = completed_selection()
    wizard.capture("inactive")
    assert wizard.status.value.startswith(f"Capture inactive in {countdown_seconds}")
    assert wizard.window.calls[:2] == ["release", "withdraw"]
    assert not workers and wizard._busy
    for _ in range(countdown_seconds):
        wizard.window.callbacks.pop(0)()
    assert len(workers) == 1 and not captures
    tk_calls = list(wizard.window.calls)
    workers[0]()
    assert wizard.window.calls == tk_calls
    assert captures == [(123, (320, 240))]
    wizard._poll_capture()
    assert not wizard._busy
    assert wizard.window.calls[-2:] == ["show", "grab"]


def test_capture_error_preserves_existing_selection_and_result():
    wizard = headless_wizard()
    wizard.selection = completed_selection()
    before = wizard.selection.build_target()
    wizard._busy = True
    wizard._generation = 7
    wizard._queue.put((7, "active", None, "Sandbox is obscured"))
    wizard._poll_capture()
    assert wizard.selection.build_target() == before
    assert wizard.result is None
    assert "Sandbox is obscured" in wizard.status.value


def test_cancel_during_countdown_discards_callbacks_and_late_capture(monkeypatch):
    wizard = headless_wizard()
    wizard.capture("active")
    countdown = wizard.window.callbacks.pop(0)
    wizard.cancel()
    calls = list(wizard.window.calls)
    monkeypatch.setattr(sandbox_module, "capture_direct_host_client", lambda *_args, **_kw: pytest.fail())
    countdown()
    wizard._queue.put((1, "active", images()[0], None))
    wizard._poll_capture()
    assert wizard.result is None
    assert wizard.window.calls == calls
    assert wizard.selection.active is None


def test_only_explicit_save_returns_the_complete_draft():
    wizard = headless_wizard()
    wizard.save()
    assert wizard.result is None and not wizard._closed
    wizard.selection = completed_selection()
    expected = deepcopy(wizard.selection.build_target())
    assert wizard.result is None
    wizard._busy = True
    wizard.save()
    assert wizard.result is None and not wizard._closed
    wizard._busy = False
    wizard.save()
    assert wizard.result == expected and wizard._closed


def test_public_entry_waits_for_modal_result_without_persisting(monkeypatch):
    result = {"kind": "sandbox"}
    waits = []
    providers = []

    def wizard(parent, hwnd, *, capture_provider=None, countdown_seconds=3,
               require_focus_samples=False):
        providers.append((capture_provider, countdown_seconds, require_focus_samples))
        return SimpleNamespace(
            window=SimpleNamespace(wait_window=lambda: waits.append((parent, hwnd))), result=result,
        )

    monkeypatch.setattr(sandbox_module, "SandboxSetup", wizard)
    assert sandbox_module.setup_sandbox("parent", 123) is result
    assert waits == [("parent", 123)]
    assert providers == [(None, 3, False)]
    alternate = lambda *_args, **_kwargs: images()[0]
    assert sandbox_module.setup_sandbox("parent", 123, capture_provider=alternate,
                                       countdown_seconds=10, require_focus_samples=True) is result
    assert providers == [(None, 3, False), (alternate, 10, True)]


@pytest.mark.parametrize("kind", ["active", "inactive"])
def test_injected_provider_receives_sample_geometry_and_default_is_never_used(monkeypatch, kind):
    captures, workers = [], []

    def alternate(hwnd, *, expected_size):
        captures.append((hwnd, expected_size))
        return images()[kind == "inactive"]

    class DeferredThread:
        def __init__(self, *, target, **_kwargs):
            workers.append(target)

        def start(self):
            pass

    monkeypatch.setattr(sandbox_module.threading, "Thread", DeferredThread)
    monkeypatch.setattr(sandbox_module, "capture_direct_host_client",
                        lambda *_args, **_kwargs: pytest.fail("default provider was called"))
    wizard = headless_wizard(alternate)
    if kind == "inactive":
        wizard.selection = completed_selection()
    wizard.capture(kind)
    for _ in range(3):
        wizard.window.callbacks.pop(0)()
    assert len(workers) == 1 and not captures
    # The worker retains its chosen provider without reading wizard/Tk state.
    wizard._capture_provider = lambda *_args, **_kwargs: pytest.fail("provider changed in flight")
    tk_calls = list(wizard.window.calls)
    workers[0]()
    assert wizard.window.calls == tk_calls
    assert captures == [(123, (320, 240) if kind == "inactive" else None)]
    wizard._poll_capture()
    assert not wizard._busy
    assert (wizard.selection.inactive if kind == "inactive" else wizard.selection.active) is not None


def test_invalid_provider_is_rejected_before_creating_a_window(monkeypatch):
    monkeypatch.setattr(sandbox_module.tk, "Toplevel",
                        lambda *_args: pytest.fail("Tk window must not be created"))
    with pytest.raises(TypeError, match="capture_provider must be callable"):
        SandboxSetup("parent", 123, capture_provider=42)


@pytest.mark.parametrize("seconds", [True, False, 0, -1, 31, 3.0, "10", None])
def test_invalid_countdown_is_rejected_before_creating_a_window(monkeypatch, seconds):
    monkeypatch.setattr(sandbox_module.tk, "Toplevel",
                        lambda *_args: pytest.fail("Tk window must not be created"))
    with pytest.raises(ValueError, match="countdown_seconds must be an integer from 1 to 30"):
        SandboxSetup("parent", 123, countdown_seconds=seconds)


def test_focus_samples_option_must_be_explicit_boolean(monkeypatch):
    monkeypatch.setattr(sandbox_module.tk, "Toplevel",
                        lambda *_args: pytest.fail("Tk window must not be created"))
    with pytest.raises(TypeError, match="require_focus_samples must be a boolean"):
        SandboxSetup("parent", 123, require_focus_samples=1)
    with pytest.raises(TypeError, match="require_focus_samples must be a boolean"):
        SandboxSelection(require_focus_samples="yes")
