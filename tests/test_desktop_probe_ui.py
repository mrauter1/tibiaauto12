"""Probe arming/stop/evidence checks without a window or native desktop access."""
from __future__ import annotations

import json
import queue
import sys
import time
from types import SimpleNamespace

from PIL import Image
import pytest

from tools.desktop_probe import DesktopProbe, StopToken, load_target_report


class Value:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Root:
    def __init__(self):
        self.callbacks = []
        self.hidden = False

    def after(self, delay, callback):
        self.callbacks.append((delay, callback))

    def withdraw(self):
        self.hidden = True

    def deiconify(self):
        self.hidden = False


class Log:
    def __init__(self):
        self.entries = []

    def insert(self, _where, value):
        self.entries.append(value)

    def see(self, _where):
        pass


@pytest.fixture
def app(tmp_path):
    probe = DesktopProbe.__new__(DesktopProbe)
    probe.output = tmp_path
    probe.root = Root()
    probe.status = Value()
    probe.safe = Value(False)
    probe.backend = Value("physical")
    probe.operation = Value("ctrl_o")
    probe.method = Value("visible_screen")
    probe.expected = Value("blocked")
    probe.expected_response = Value("Channel chooser opens")
    probe.choice = Value("Sandbox")
    probe.windows = {"Sandbox": {"hwnd": 123, "title": "Windows Sandbox",
                                  "physical_pixels": {"client_size": [160, 120]}}}
    probe.requested_hwnd = None
    probe.manual_target = probe.manual_target_label = None
    probe._inventory = lambda: list(probe.windows.values())
    probe.selector = SimpleNamespace(configure=lambda **values: None)
    probe.target = probe.target_hwnd = None
    probe.point = probe.frame = None
    probe.preview_hwnd = probe.preview_loaded_at = None
    probe.preview_source = None
    probe.last_input = None
    probe.busy = probe.closed = False
    probe.input_blocked = False
    probe.arm_on_point = Value(False)
    probe.reacquire_control = Value(False)
    probe.known_dialog_report = None
    probe.expected_dialog = Value("")
    probe.input_startup_timeout = Value("15")
    probe._armed_mouse_settings = None
    probe.point_label = Value()
    probe.canvas = SimpleNamespace(winfo_width=lambda: 180, winfo_height=lambda: 140)
    probe.render = lambda: None
    probe.token = None
    probe.log = Log()
    probe.reports = []
    probe.results = queue.Queue()
    probe.created_utc = "2026-10-21T00:00:00+00:00"
    probe._emergency_stop = lambda: False
    probe.load_preview = lambda _value: None
    return probe


def configure_target(app):
    from tibia_client.window_target import WindowTarget, make_focus_guard

    active = Image.new("RGB", (160, 120), "blue")
    inactive = Image.new("RGB", (160, 120), "gray")
    for image in (active, inactive):
        image.putpixel((20, 10), (240, 240, 240))
    target = WindowTarget.build((160, 120), (10, 30, 150, 110),
                               make_focus_guard(active, inactive, (15, 5, 95, 25)))
    app.target, app.target_hwnd = target.to_dict(), 123
    return target


@pytest.mark.parametrize("operation", ["ctrl_o", "escape", "left_click", "right_click"])
def test_game_input_cannot_be_armed_without_harmless_logged_in_acknowledgment(app, operation):
    app.operation.set(operation)
    with pytest.raises(ValueError, match="logged in"):
        app._input_work()


def test_cursor_only_probe_needs_a_fresh_selected_window_preview(app):
    app.operation.set("move")
    with pytest.raises(ValueError, match="fresh capture"):
        app._input_work()
    app.point = (50, 50)
    app.frame = Image.new("RGB", (160, 120))
    app.preview_hwnd, app.preview_loaded_at = 999, time.monotonic()
    with pytest.raises(ValueError, match="preview expired"):
        app._input_work()
    app.preview_hwnd, app.preview_loaded_at = 123, time.monotonic() - 31
    with pytest.raises(ValueError, match="preview expired"):
        app._input_work()


def test_cursor_probe_forwards_selected_geometry_without_login_or_click(app, monkeypatch):
    from tibia_client import input_probe

    calls = []
    monkeypatch.setattr(input_probe, "run_input_probe", lambda *a, **k: calls.append((a, k)) or {})
    app.operation.set("move")
    app.point = (50, 50)
    app.frame = Image.new("RGB", (160, 120))
    app.preview_hwnd, app.preview_loaded_at = 123, time.monotonic()
    report = app._input_work()(StopToken())
    assert calls[0][0][:3] == (123, "physical", "move")
    assert calls[0][1]["point"] == (50, 50)
    assert calls[0][1]["expected_size"] == (160, 120)
    assert calls[0][1]["capture_method"] == "visible_screen"
    assert calls[0][1]["point_patch"]["box"] == [34, 34, 67, 67]
    assert report["kind"] == "input"


@pytest.mark.parametrize("actual, expected", [(True, "fail"), (False, "pass"), (None, "unknown")])
def test_readiness_records_false_positive_and_unknown_separately(app, monkeypatch, actual, expected):
    from tibia_client import input_probe

    configured = configure_target(app)
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return {"ready": actual}

    monkeypatch.setattr(input_probe, "run_input_probe", run)
    report = app._input_work(readiness=True)(StopToken())
    assert report["expectation_result"] == expected
    assert calls[0][0][:3] == (123, "guarded_physical", "readiness")
    assert calls[0][1]["target"] == configured
    assert calls[0][1]["expected_size"] == configured.host_size
    assert calls[0][1]["point"] is None


def test_guard_cannot_use_configuration_for_another_window(app):
    configure_target(app)
    app.target_hwnd = 999
    with pytest.raises(ValueError, match="configure experimental"):
        app._input_work(readiness=True)


def test_guard_rejects_preview_size_that_differs_from_setup(app):
    configure_target(app)
    app.preview_hwnd = 123
    app.frame = Image.new("RGB", (150, 100))
    with pytest.raises(ValueError, match="preview size differs"):
        app._input_work(readiness=True)


def test_expected_visible_response_is_required_for_input(app):
    app.safe.set(True)
    app.expected_response.set("   ")
    with pytest.raises(ValueError, match="intended visible"):
        app._input_work()


def test_stop_chord_cancels_even_while_probe_window_is_hidden(app):
    app.run("capture", lambda _token: pytest.fail("cancelled work must not run"))
    assert app.busy and app.root.hidden
    app._emergency_stop = lambda: True
    app.poll()
    assert app.token.cancelled
    app.root.callbacks[0][1]()
    assert not app.busy and not app.root.hidden


def test_loss_of_stop_monitoring_cancels_running_probe(app):
    app.run("capture", lambda _token: None)

    def unavailable():
        raise RuntimeError("hook unavailable")

    app._emergency_stop = unavailable
    app.poll()
    assert app.token.cancelled
    assert "cancelled" in app.log.entries[-1]


def test_unavailable_stop_monitor_prevents_arming(app):
    def unavailable():
        raise RuntimeError("hook unavailable")

    app._emergency_stop = unavailable
    app.run("input", lambda _token: pytest.fail("must not start"))
    assert not app.busy and not app.root.hidden and not app.root.callbacks
    assert "unavailable" in app.status.get()


def test_harness_error_is_persisted_with_unknown_compatibility(app):
    app.results.put(("capture", None, "worker failed"))
    app.poll()
    saved = json.loads((app.output / "session.json").read_text(encoding="utf-8"))
    assert saved["created_utc"] == app.created_utc
    assert saved["overall_compatibility"] == "unknown"
    assert saved["reports"] == [{"kind": "harness_error", "label": "capture", "error": "worker failed"}]


@pytest.mark.parametrize("dispatch, path", [("failed", "after.png"), ("completed", None)])
def test_api_failure_or_missing_after_frame_cannot_be_confirmed_as_success(app, dispatch, path):
    app.last_input = {"dispatch": dispatch, "after_path": path, "operator_confirmation": "unknown"}
    app.confirm("pass")
    assert app.last_input["operator_confirmation"] == "unknown"
    assert not (app.output / "session.json").exists()


def test_visual_confirmation_never_promotes_global_compatibility(app):
    report = {"dispatch": "completed", "after_path": "after.png", "operator_confirmation": "unknown"}
    app.last_input = report
    app.reports.append(report)
    app.confirm("pass")
    saved = json.loads((app.output / "session.json").read_text(encoding="utf-8"))
    assert saved["reports"][0]["operator_confirmation"] == "pass"
    assert saved["overall_compatibility"] == "unknown"


def test_stop_token_propagates_cancellation():
    token = StopToken()
    token.checkpoint()
    assert not token.cancelled
    token.event.set()
    assert token.cancelled
    with pytest.raises(RuntimeError, match="stopped"):
        token.checkpoint()


def test_unreaped_worker_blocks_subsequent_probes(app):
    app.results.put(("input", {"kind": "input", "worker_reaped": False}, None))
    app.poll()
    with pytest.raises(ValueError, match="not reaped"):
        app._input_work(readiness=True)
    app.run("capture", lambda _token: pytest.fail("must remain blocked"))
    assert not app.busy and not app.root.hidden


def test_loading_old_frame_does_not_reset_its_acquisition_age(app):
    path = app.output / "old.png"
    Image.new("RGB", (160, 120)).save(path)
    observed = time.monotonic() - 35
    app.point_label = Value()
    app.render = lambda: None
    DesktopProbe.load_preview(app, {
        "kind": "capture", "selection": {"hwnd": 123},
        "methods": [{"method": "visible_screen", "frames": [
            {"path": str(path), "captured_monotonic": observed}]}],
    })
    app.point = (50, 50)
    app.operation.set("move")
    assert app.preview_loaded_at == observed
    with pytest.raises(ValueError, match="preview expired"):
        app._input_work()


def test_frame_without_acquisition_time_cannot_arm_a_mouse_probe(app):
    path = app.output / "untimed.png"
    Image.new("RGB", (160, 120)).save(path)
    app.point_label = Value()
    app.render = lambda: None
    DesktopProbe.load_preview(app, {
        "kind": "input", "selection": {"hwnd": 123}, "after_path": str(path),
    })
    app.point = (50, 50)
    app.operation.set("move")
    with pytest.raises(ValueError, match="preview expired"):
        app._input_work()


def test_input_cannot_silently_switch_from_printwindow_to_screen_capture(app):
    app.safe.set(True)
    app.method.set("printwindow_physical")
    with pytest.raises(ValueError, match="visible-screen"):
        app._input_work()


def arm_mouse(app, *, operation="left_click"):
    app.safe.set(True)
    app.operation.set(operation)
    app.expected_response.set("Harmless local tab responds")
    app.arm_on_point.set(True)
    app.arm_next_point()


def fresh_mouse_preview(app, *, age=0):
    app.frame = Image.new("RGB", (160, 120))
    app.preview_hwnd, app.preview_loaded_at = 123, time.monotonic() - age


def test_next_preview_click_starts_exactly_one_armed_mouse_probe(app):
    runs = []
    app.run = lambda label, work: runs.append((label, work))
    arm_mouse(app)
    fresh_mouse_preview(app)
    assert app.arm_on_point.get()
    app.select_point(SimpleNamespace(x=60, y=60))
    assert app.point == (50, 50)
    assert len(runs) == 1 and runs[0][0] == "One native input probe"
    assert not app.arm_on_point.get() and app._armed_mouse_settings is None
    app.select_point(SimpleNamespace(x=80, y=80))
    assert len(runs) == 1


def test_expired_preview_consumes_mouse_arming_without_dispatch(app):
    app.run = lambda *args: pytest.fail("expired preview must not start input")
    arm_mouse(app)
    fresh_mouse_preview(app, age=31)
    app.select_point(SimpleNamespace(x=60, y=60))
    assert not app.arm_on_point.get()
    assert "preview expired" in app.status.get()


@pytest.mark.parametrize("operation", ["ctrl_o", "escape", "readiness"])
def test_keyboard_and_readiness_cannot_be_armed_by_preview_click(app, operation):
    arm_mouse(app, operation=operation)
    assert not app.arm_on_point.get() and app._armed_mouse_settings is None


def test_mouse_preview_arming_requires_explicit_harmless_ack_even_for_move(app):
    app.operation.set("move")
    app.arm_on_point.set(True)
    app.arm_next_point()
    assert not app.arm_on_point.get()
    assert "logged in" in app.status.get()


@pytest.mark.parametrize("change", ["backend", "method", "description"])
def test_arming_validates_backend_capture_and_description(app, change):
    app.safe.set(True)
    app.operation.set("left_click")
    if change == "backend":
        app.backend.set("arbitrary")
    elif change == "method":
        app.method.set("printwindow")
    else:
        app.expected_response.set("")
    app.arm_on_point.set(True)
    app.arm_next_point()
    assert not app.arm_on_point.get()


def test_capture_and_preview_loading_preserve_explicit_mouse_arming(app):
    arm_mouse(app)
    app.run = lambda *args: None
    app.capture()
    assert app.arm_on_point.get()
    path = app.output / "fresh.png"
    Image.new("RGB", (160, 120)).save(path)
    DesktopProbe.load_preview(app, {"kind": "capture", "selection": {"hwnd": 123},
                                    "methods": [{"method": "visible_screen", "frames": [
                                        {"path": str(path), "captured_monotonic": time.monotonic()}]}]})
    assert app.arm_on_point.get() and app.point is None


def test_changed_settings_cannot_reuse_old_preview_click_authorization(app):
    arm_mouse(app)
    fresh_mouse_preview(app)
    app.backend.set("messages")
    app.run = lambda *args: pytest.fail("changed settings must be rearmed")
    app.select_point(SimpleNamespace(x=60, y=60))
    assert not app.arm_on_point.get()
    assert "settings changed" in app.status.get()


@pytest.mark.parametrize("action", ["stop", "readiness", "operation_changed", "changed_target",
                                    "mouse_settings_changed", "input"])
def test_cancel_and_other_operations_reset_mouse_arming(app, action):
    arm_mouse(app)
    app.run = lambda *args: None
    getattr(app, action)()
    assert not app.arm_on_point.get() and app._armed_mouse_settings is None


def saved_target_report(app, *, event_only=True):
    target = configure_target(app)
    calibration = {"window_target": target.to_dict(), "target_fingerprint": target.fingerprint}
    report = {"hwnd": 123, "selection": {"hwnd": 123}, "events": [
        {"stage": "calibrated_target", **calibration}]}
    if not event_only:
        report.update(json.loads(json.dumps(calibration)))
    return target, {"reports": [{"kind": "capture"}, report]}


@pytest.mark.parametrize("event_only", [False, True])
def test_loader_recovers_validated_target_from_top_level_or_cached_worker_event(app, event_only):
    target, data = saved_target_report(app, event_only=event_only)
    path = app.output / "saved.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    saved = load_target_report(path)
    assert saved == {"source": str(path.resolve()), "hwnd": 123,
                     "window_target": target.to_dict(), "target_fingerprint": target.fingerprint}


@pytest.mark.parametrize("change", ["fingerprint", "hwnd", "selection", "target"])
def test_loader_rejects_invalid_or_disagreeing_metadata(app, change):
    _target, data = saved_target_report(app)
    report = data["reports"][-1]
    if change == "fingerprint":
        report["events"][0]["target_fingerprint"] = "0" * 64
    elif change == "hwnd":
        report["hwnd"] = True
    elif change == "selection":
        report["selection"]["hwnd"] = 999
    else:
        report["events"][0]["window_target"]["focus_guard"] = None
    path = app.output / "bad.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_target_report(path)


def test_loader_rejects_conflicting_calibrations_in_same_report(app):
    from tibia_client.window_target import WindowTarget

    _target, data = saved_target_report(app, event_only=False)
    event = data["reports"][-1]["events"][0]
    event["window_target"]["viewport"][0] += 1
    target = WindowTarget.from_config({"window_target": event["window_target"]})
    event["target_fingerprint"] = target.fingerprint
    path = app.output / "conflicting.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="conflicting"):
        load_target_report(path)


@pytest.mark.parametrize("mismatch", ["hwnd", "size", "missing_size"])
def test_saved_target_cannot_bind_to_wrong_current_window_or_dimensions(app, mismatch):
    target, data = saved_target_report(app)
    path = app.output / "saved.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    saved = load_target_report(path)
    app.target = app.target_hwnd = None
    if mismatch == "hwnd":
        app.windows["Sandbox"]["hwnd"] = 999
    elif mismatch == "missing_size":
        app.windows["Sandbox"].pop("physical_pixels")
    else:
        app.windows["Sandbox"]["physical_pixels"] = {"client_size": [159, 120]}
    assert app.apply_saved_target(saved) is False
    assert app.target is app.target_hwnd is None


def test_loading_target_binds_returned_hwnd_without_safety_ack_or_arming(app):
    target, data = saved_target_report(app)
    path = app.output / "saved.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    arm_mouse(app)
    app.last_input = {"dispatch": "completed"}
    assert app.apply_saved_target(load_target_report(path)) is True
    assert app.target == target.to_dict() and app.target_hwnd == 123
    assert not app.safe.get() and not app.arm_on_point.get()
    assert app.last_input is None and app.frame is None
    assert app.reports[-1]["kind"] == "loaded_target"


def test_main_passes_target_report_option_without_starting_native_gui(monkeypatch, tmp_path):
    from tools import desktop_probe

    calls = []

    def constructor(output, *, target_report=None):
        calls.append((output, target_report))
        return SimpleNamespace(root=SimpleNamespace(mainloop=lambda: None))

    monkeypatch.setattr(desktop_probe, "DesktopProbe", constructor)
    target_report = tmp_path / "saved.json"
    assert desktop_probe.main(["--output", str(tmp_path), "--target-report", str(target_report)]) == 0
    assert calls == [(tmp_path, target_report)]


def test_stop_polling_fails_closed_on_non_windows_without_loading_api(monkeypatch):
    from tools import desktop_probe

    monkeypatch.setattr(desktop_probe, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(desktop_probe, "_stop_key_state", None)
    monkeypatch.setattr(desktop_probe, "_load_stop_state_reader",
                        lambda: pytest.fail("non-Windows must not load a Windows API"))
    with pytest.raises(RuntimeError, match="unavailable on this OS"):
        desktop_probe.emergency_stop_pressed()


@pytest.mark.parametrize("states,pressed", [
    ((-32768, -32768, -32768), True),
    ((0x8001, 0x8001, 0x8001), True),
    ((1, 1, 1), False),
    ((0, 0x8000, 0x8000), False),
    ((0x8000, 0, 0x8000), False),
    ((0x8000, 0x8000, 0), False),
])
def test_stop_polling_reads_only_exact_chord_high_bits(monkeypatch, states, pressed):
    from tools import desktop_probe

    calls = []
    values = dict(zip((0x11, 0x10, 0x7B), states))
    monkeypatch.setattr(desktop_probe, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(desktop_probe, "_stop_key_state",
                        lambda key: calls.append(key) or values[key])
    monkeypatch.setitem(sys.modules, "keyboard", SimpleNamespace(
        is_pressed=lambda *args: pytest.fail("probe polling must never use keyboard hooks")))
    assert desktop_probe.emergency_stop_pressed() is pressed
    assert calls == [0x11, 0x10, 0x7B]


def test_stop_polling_resolves_and_caches_only_user32_reader(monkeypatch):
    from tools import desktop_probe

    loads = []

    def reader(_key):
        return 0

    def dll(name, **options):
        loads.append((name, options))
        return SimpleNamespace(GetAsyncKeyState=reader)

    fake_ctypes = SimpleNamespace(WinDLL=dll, c_int=int, c_short=int)
    monkeypatch.setattr(desktop_probe, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(desktop_probe, "_stop_key_state", None)
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    assert desktop_probe.emergency_stop_pressed() is False
    assert desktop_probe.emergency_stop_pressed() is False
    assert loads == [("user32", {"use_last_error": True})]
    assert reader.argtypes == [int] and reader.restype is int


def test_stop_polling_load_error_prevents_arming(app, monkeypatch):
    from tools import desktop_probe

    def unavailable():
        raise OSError("user32 unavailable")

    monkeypatch.setattr(desktop_probe, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(desktop_probe, "_stop_key_state", None)
    monkeypatch.setattr(desktop_probe, "_load_stop_state_reader", unavailable)
    app._emergency_stop = desktop_probe.emergency_stop_pressed
    app.run("capture", lambda _token: pytest.fail("availability failure must prevent arming"))
    assert not app.busy and not app.root.hidden and not app.root.callbacks
    assert "unavailable" in app.status.get()


def test_stop_polling_call_error_cancels_running_probe_and_drops_cached_reader(app, monkeypatch):
    from tools import desktop_probe

    app.run("capture", lambda _token: None)
    calls = []

    def failed_reader(key):
        calls.append(key)
        if key == 0x7B:
            raise OSError("key state read unavailable")
        return 0  # Availability still must be checked for the remaining chord keys.

    monkeypatch.setattr(desktop_probe, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(desktop_probe, "_stop_key_state", failed_reader)
    app._emergency_stop = desktop_probe.emergency_stop_pressed
    app.poll()
    assert app.token.cancelled
    assert desktop_probe._stop_key_state is None
    assert calls == [0x11, 0x10, 0x7B]
    assert "cancelled" in app.log.entries[-1]


def test_restart_flags_forward_only_validated_unarmed_defaults(monkeypatch, tmp_path):
    from tools import desktop_probe

    calls = []

    def constructor(output, **settings):
        calls.append((output, settings))
        return SimpleNamespace(root=SimpleNamespace(mainloop=lambda: None))

    monkeypatch.setattr(desktop_probe, "DesktopProbe", constructor)
    target_report = tmp_path / "saved.json"
    desktop_probe.main(["--output", str(tmp_path), "--target-report", str(target_report),
                        "--capture-method", "visible_screen_overlay_aware",
                        "--backend", "guarded_physical_overlay_aware", "--operation", "left_click",
                        "--expected-response", "  Harmless local tab becomes selected  "])
    assert calls == [(tmp_path, {"target_report": target_report,
                                 "capture_method": "visible_screen_overlay_aware",
                                 "backend": "guarded_physical_overlay_aware",
                                 "operation": "left_click",
                                 "expected_response": "Harmless local tab becomes selected"})]
    assert "safe" not in calls[0][1] and "arm" not in calls[0][1]


@pytest.mark.parametrize("flag,value", [("--capture-method", "unknown"), ("--backend", "unknown"),
                                        ("--operation", "readiness"), ("--operation", "type"),
                                        ("--expected-response", "  "),
                                        ("--expected-response", "a" * 501),
                                        ("--expected-response", "one\ntwo")])
def test_restart_flags_reject_invalid_defaults_before_gui(monkeypatch, flag, value):
    from tools import desktop_probe

    monkeypatch.setattr(desktop_probe, "DesktopProbe", lambda *a, **k: pytest.fail("must not create GUI"))
    with pytest.raises(SystemExit) as error:
        desktop_probe.main([flag, value])
    assert error.value.code == 2


@pytest.mark.parametrize("flag", ["--safe", "--arm"])
def test_restart_flags_cannot_acknowledge_safety_or_arm_input(monkeypatch, flag):
    from tools import desktop_probe

    monkeypatch.setattr(desktop_probe, "DesktopProbe", lambda *a, **k: pytest.fail("must not create GUI"))
    with pytest.raises(SystemExit) as error:
        desktop_probe.main([flag])
    assert error.value.code == 2


def test_no_argument_defaults_remain_unchanged_and_do_not_contain_arming():
    from tools import desktop_probe

    assert desktop_probe._validated_defaults() == {"capture_method": "visible_screen",
                                                   "backend": "physical", "operation": "ctrl_o",
                                                   "expected_response": "Channel chooser opens"}


@pytest.mark.parametrize("reacquire_control", [False, True])
@pytest.mark.parametrize("target_hwnd", [None, 3735796])
def test_constructor_applies_restart_defaults_without_arming_or_running(monkeypatch, tmp_path, reacquire_control, target_hwnd):
    from tools import desktop_probe
    from tibia_client import desktop_probe as probe_engine

    class FakeRoot(Root):
        def title(self, *args):
            pass

        geometry = minsize = protocol = title

    class Variable(Value):
        def trace_add(self, *args):
            pass

    class Widget:
        def __init__(self, *args, **kwargs):
            pass

        def pack(self, *args, **kwargs):
            pass

        bind = configure = insert = delete = pack

    root = FakeRoot()
    monkeypatch.setattr(desktop_probe, "tk", SimpleNamespace(
        Tk=lambda: root, StringVar=Variable, BooleanVar=Variable, Canvas=Widget, Text=Widget))
    monkeypatch.setattr(desktop_probe, "ttk", SimpleNamespace(**{
        name: Widget for name in ("Frame", "Label", "Button", "Combobox", "Checkbutton", "Entry")}))
    monkeypatch.setattr(probe_engine, "inventory", lambda: [])
    monkeypatch.setattr(probe_engine, "verify_known_handle",
                        lambda hwnd, **kwargs: handle_diagnostic(hwnd, presence="absent"))
    app = DesktopProbe(tmp_path, capture_method="visible_screen_overlay_aware", backend="messages",
                       operation="move", expected_response="Cursor reaches selected harmless point",
                       reacquire_control=reacquire_control, target_hwnd=target_hwnd)
    assert app.method.get() == "visible_screen_overlay_aware"
    assert app.backend.get() == "messages" and app.operation.get() == "move"
    assert app.expected_response.get() == "Cursor reaches selected harmless point"
    assert app.safe.get() is False and app.arm_on_point.get() is False
    assert app.reacquire_control.get() is reacquire_control
    assert not app.busy and app.token is None and app._armed_mouse_settings is None
    assert not root.hidden
    assert len(root.callbacks) == 1 and root.callbacks[0][0] == 100  # UI result polling only.
    if target_hwnd is not None:
        assert app.selected()["hwnd"] == target_hwnd
        assert app.windows == {} and app.manual_target_label.startswith("Verified manual target:")


def test_region_gdi_cli_choices_remain_explicit_unarmed_defaults(monkeypatch, tmp_path):
    from tools import desktop_probe

    calls = []
    monkeypatch.setattr(desktop_probe, "DesktopProbe", lambda output, **settings:
                        calls.append(settings) or SimpleNamespace(root=SimpleNamespace(mainloop=lambda: None)))
    assert desktop_probe.main(["--capture-method", "visible_region_gdi_overlay_aware",
                               "--backend", "guarded_region_gdi_overlay_aware"]) == 0
    assert calls == [{"target_report": None, "capture_method": "visible_region_gdi_overlay_aware",
                     "backend": "guarded_region_gdi_overlay_aware"}]


def test_region_gdi_input_forwards_candidate_without_silent_backend_switch(app, monkeypatch):
    from tibia_client import input_probe

    calls = []
    monkeypatch.setattr(input_probe, "run_input_probe", lambda *a, **kwargs: calls.append((a, kwargs)) or {})
    configure_target(app)
    app.backend.set("guarded_region_gdi_overlay_aware")
    app.method.set("visible_region_gdi_overlay_aware")
    app.operation.set("move")
    fresh_mouse_preview(app)
    app.point = (50, 50)
    app._input_work()(StopToken())
    assert calls[0][0][:3] == (123, "guarded_region_gdi_overlay_aware", "move")
    assert calls[0][1]["capture_method"] == "visible_region_gdi_overlay_aware"
    assert calls[0][1]["target"].kind == "sandbox"


def test_region_gdi_setup_uses_only_experimental_capture_provider(app, monkeypatch):
    import modules.SandboxSetup as setup_module

    calls = []
    provider = lambda *args: pytest.fail("setup mock must not execute capture")
    monkeypatch.setitem(sys.modules, "tibia_client.probe_region_gdi", SimpleNamespace(
        capture_client_region_gdi_overlay_aware=provider))
    monkeypatch.setattr(setup_module, "setup_sandbox", lambda *args, **settings: calls.append(settings))
    app.method.set("visible_region_gdi_overlay_aware")
    app.configure_target()
    assert calls == [{"capture_provider": provider, "countdown_seconds": 30, "require_focus_samples": True}]


def stable_template(app, *, age=80):
    app.operation.set("left_click")
    app.safe.set(True)
    app.reacquire_control.set(True)
    fresh_mouse_preview(app, age=age)
    app.point = (50, 50)


@pytest.mark.parametrize("changed", [False, True])
def test_explicit_old_template_requires_exact_fresh_pixel_patch_before_dispatch(app, monkeypatch, changed):
    from tibia_client import input_probe

    stable_template(app)
    calls = []

    def checked_worker(hwnd, backend, operation, directory, **settings):
        calls.append(settings)
        assert hwnd == 123 and settings["expected_size"] == app.frame.size
        assert settings["point_patch"] is not None
        fresh = app.frame.copy()
        if changed:
            fresh.putpixel(app.point, (100, 90, 80))
        comparison = input_probe._check_point_patch(fresh, settings["point_patch"])
        return {"status": "failed" if changed else "completed",
                "dispatch": "not_attempted" if changed else "completed",
                "point_patch": comparison, "input_acceptance": "unknown"}

    monkeypatch.setattr(input_probe, "run_input_probe", checked_worker)
    report = app._input_work()(StopToken())
    assert len(calls) == 1
    assert report["point_selection"]["mode"] == "stable_template_reacquisition"
    assert report["point_selection"]["template_age_at_arming_ms"] >= 80_000
    assert report["point_selection"]["template_age_at_worker_request_ms"] >= 80_000
    assert report["point_selection"]["max_template_age_ms"] == 300_000
    assert report["point_selection"]["fresh_before_patch_required"] is True
    assert report["point_patch"]["status"] == ("changed" if changed else "matched")
    assert report["dispatch"] == ("not_attempted" if changed else "completed")
    assert report["input_acceptance"] == "unknown"


def test_ordinary_mode_still_rejects_same_old_template(app):
    stable_template(app)
    app.reacquire_control.set(False)
    with pytest.raises(ValueError, match="preview expired"):
        app._input_work()


@pytest.mark.parametrize("observed", [None, True, float("inf"), float("nan"), "12", -1])
def test_reacquisition_rejects_unknown_or_invalid_acquisition_time(app, observed):
    stable_template(app)
    app.preview_loaded_at = observed
    with pytest.raises(ValueError, match="preview expired"):
        app._input_work()


def test_reacquisition_rejects_future_acquisition_time(app):
    stable_template(app)
    app.preview_loaded_at = time.monotonic() + 60
    with pytest.raises(ValueError, match="future"):
        app._input_work()


def test_reacquisition_rejects_expired_template(app, monkeypatch):
    from tools import desktop_probe

    stable_template(app, age=301)
    app.preview_loaded_at = 699.0
    monkeypatch.setattr(desktop_probe, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    with pytest.raises(ValueError, match="stable template expired"):
        app._input_work()


@pytest.mark.parametrize("mismatch", ["hwnd", "size", "unavailable_size", "safe"])
def test_reacquisition_requires_same_target_geometry_and_explicit_safe_ack(app, mismatch):
    stable_template(app)
    if mismatch == "hwnd":
        app.preview_hwnd = 999
    elif mismatch == "size":
        app.windows["Sandbox"]["physical_pixels"]["client_size"] = [159, 120]
    elif mismatch == "unavailable_size":
        app.windows["Sandbox"].pop("physical_pixels")
    else:
        app.safe.set(False)
    with pytest.raises(ValueError):
        app._input_work()


def test_template_expiring_during_countdown_is_rejected_before_worker_request(app, monkeypatch):
    from tools import desktop_probe
    from tibia_client import input_probe

    stable_template(app, age=299)
    app.preview_loaded_at = 701.0
    monkeypatch.setattr(desktop_probe, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    work = app._input_work()
    observed = app.preview_loaded_at
    monkeypatch.setattr(desktop_probe, "time", SimpleNamespace(monotonic=lambda: observed + 301))
    monkeypatch.setattr(input_probe, "run_input_probe", lambda *a, **k: pytest.fail("expired template must not request worker"))
    with pytest.raises(ValueError, match="expired before"):
        work(StopToken())


def test_reacquisition_mode_change_requires_new_preview_click_arming(app):
    arm_mouse(app)
    fresh_mouse_preview(app)
    app.reacquire_control.set(True)
    app.run = lambda *args: pytest.fail("mode change requires a new arm")
    app.select_point(SimpleNamespace(x=60, y=60))
    assert not app.arm_on_point.get()
    assert "settings changed" in app.status.get()


def test_cli_reacquire_control_sets_mode_only_and_never_safe_or_arm(monkeypatch):
    from tools import desktop_probe

    calls = []
    monkeypatch.setattr(desktop_probe, "DesktopProbe", lambda output, **settings:
                        calls.append(settings) or SimpleNamespace(root=SimpleNamespace(mainloop=lambda: None)))
    assert desktop_probe.main(["--reacquire-control"]) == 0
    assert calls == [{"target_report": None, "reacquire_control": True}]


def test_reacquisition_mode_does_not_add_template_replay_to_keyboard(app, monkeypatch):
    from tibia_client import input_probe

    app.safe.set(True)
    app.reacquire_control.set(True)
    calls = []
    monkeypatch.setattr(input_probe, "run_input_probe", lambda *a, **k: calls.append(k) or {})
    report = app._input_work()(StopToken())
    assert calls[0]["point"] is None and calls[0]["point_patch"] is None
    assert "point_selection" not in report


def handle_diagnostic(hwnd, *, status="verified", presence="present"):
    inspection = {"hwnd": hwnd, "root_hwnd": hwnd, "is_window": True,
                  "visible": status == "verified", "minimized": False, "title": "Tibia host",
                  "class_name": "TibiaTestWindow", "client_size": [160, 120],
                  "foreground_hwnd": 999, "foreground_root_hwnd": 999,
                  "foreground_root_matches": False, "inspected_monotonic": 100.0}
    selection = None
    if status == "verified":
        selection = {"hwnd": hwnd, "title": inspection["title"],
                     "physical_pixels": {"client_size": [160, 120]},
                     "selection_method": "explicit_verified_hwnd", "requested_hwnd": hwnd,
                     "inventory_presence": presence, "handle_diagnostic": {"inspection": inspection}}
    return {"kind": "known_handle_diagnostic", "requested_hwnd": hwnd,
            "selected_hwnd": hwnd if status == "verified" else None, "selection": selection,
            "status": status, "reasons": [] if status == "verified" else ["window not visible"],
            "inspection": inspection, "selection_method": "explicit_verified_hwnd",
            "inventory_presence": presence, "input_armed": False, "capture_started": False}


@pytest.mark.parametrize("argument,expected", [("3735796", 3735796), ("0x38fff4", 3735540)])
def test_cli_explicit_handle_is_forwarded_without_arming(monkeypatch, argument, expected):
    from tools import desktop_probe

    calls = []
    monkeypatch.setattr(desktop_probe, "DesktopProbe", lambda output, **settings:
                        calls.append(settings) or SimpleNamespace(root=SimpleNamespace(mainloop=lambda: None)))
    assert desktop_probe.main(["--target-hwnd", argument]) == 0
    assert calls == [{"target_report": None, "target_hwnd": expected}]


@pytest.mark.parametrize("argument", ["0", "-1", str(2**64), "not-a-handle"])
def test_cli_rejects_malformed_explicit_handles_before_gui(monkeypatch, argument):
    from tools import desktop_probe

    monkeypatch.setattr(desktop_probe, "DesktopProbe", lambda *a, **k: pytest.fail("invalid handle must not create GUI"))
    with pytest.raises(SystemExit) as error:
        desktop_probe.main(["--target-hwnd", argument])
    assert error.value.code == 2


@pytest.mark.parametrize("presence,requested", [("present", 123), ("absent", 3735796)])
def test_explicit_selection_is_separate_from_true_inventory_and_persisted(app, monkeypatch, presence, requested):
    from tibia_client import desktop_probe

    items = list(app.windows.values())
    calls = []
    app._inventory = lambda: list(items)
    app.requested_hwnd = requested

    def verify(hwnd, *, inventory_items=None):
        calls.append((hwnd, inventory_items))
        return handle_diagnostic(hwnd, presence=presence)

    monkeypatch.setattr(desktop_probe, "verify_known_handle", verify)
    app.refresh()
    assert calls == [(requested, items)]
    assert list(app.windows.values()) == items
    assert app.manual_target_label not in app.windows
    assert app.manual_target_label.startswith("Verified manual target:")
    assert app.selected()["hwnd"] == requested
    assert app.selected()["selection_method"] == "explicit_verified_hwnd"
    assert app.selected()["inventory_presence"] == presence
    assert not app.safe.get() and not app.arm_on_point.get() and not app.busy
    saved = json.loads((app.output / "session.json").read_text(encoding="utf-8"))
    assert saved["reports"][-1]["kind"] == "known_handle_diagnostic"
    assert saved["reports"][-1]["inventory_presence"] == presence
    assert saved["overall_compatibility"] == "unknown"


def test_rejected_handle_remains_unselected_and_evidence_is_preserved(app, monkeypatch):
    from tibia_client import desktop_probe

    app.requested_hwnd = 3735796
    monkeypatch.setattr(desktop_probe, "verify_known_handle",
                        lambda hwnd, **kwargs: handle_diagnostic(hwnd, status="rejected", presence="absent"))
    app.refresh()
    assert app.choice.get() == "" and app.manual_target is None
    with pytest.raises(ValueError, match="verified manual"):
        app.selected()
    saved = json.loads((app.output / "session.json").read_text(encoding="utf-8"))
    assert saved["reports"][-1]["status"] == "rejected"
    assert saved["reports"][-1]["reasons"] == ["window not visible"]
    assert not app.safe.get() and not app.arm_on_point.get()


def test_explicit_target_never_accepts_automatic_root_substitution(app, monkeypatch):
    from tibia_client import desktop_probe

    app.requested_hwnd = 3735796
    substituted = handle_diagnostic(3735796, presence="absent")
    substituted["selected_hwnd"] = substituted["selection"]["hwnd"] = 123
    monkeypatch.setattr(desktop_probe, "verify_known_handle", lambda *a, **k: substituted)
    app.refresh()
    assert app.choice.get() == "" and app.manual_target is None
    assert app.reports[-1]["status"] == "rejected"
    assert "exact requested HWND" in app.reports[-1]["reasons"][0]


def test_manual_refresh_uses_fresh_verification_and_clears_prior_input_authorization(app, monkeypatch):
    from tibia_client import desktop_probe

    app.requested_hwnd = 123
    reports = iter([handle_diagnostic(123), handle_diagnostic(123, status="rejected")])
    monkeypatch.setattr(desktop_probe, "verify_known_handle", lambda *a, **k: next(reports))
    app.refresh()
    app.safe.set(True)
    app.arm_on_point.set(True)
    fresh_mouse_preview(app)
    app.refresh()
    assert app.choice.get() == "" and app.manual_target is None
    assert app.frame is None and not app.safe.get() and not app.arm_on_point.get()
    assert [report["status"] for report in app.reports] == ["verified", "rejected"]


def test_inventory_failure_remains_unknown_when_explicit_native_helper_verifies(app, monkeypatch):
    from tibia_client import desktop_probe

    def unavailable():
        raise RuntimeError("inventory snapshot unavailable")

    calls = []
    app._inventory = unavailable
    app.requested_hwnd = 3735796

    def verify(hwnd, *, inventory_items=None):
        calls.append(inventory_items)
        return handle_diagnostic(hwnd, presence="unknown")

    monkeypatch.setattr(desktop_probe, "verify_known_handle", verify)
    app.refresh()
    assert calls == [None] and app.windows == {}
    assert app.selected()["hwnd"] == 3735796
    assert app.reports[0]["kind"] == "inventory_error"
    assert app.reports[-1]["inventory_presence"] == "unknown"


def test_inventory_cli_can_persist_rejected_handle_without_starting_gui(monkeypatch, tmp_path, capsys):
    from tools import desktop_probe
    from tibia_client import desktop_probe as engine

    items = [{"hwnd": 123, "title": "Windows Sandbox"}]
    monkeypatch.setattr(engine, "inventory", lambda: items)
    monkeypatch.setattr(engine, "verify_known_handle", lambda hwnd, **kwargs:
                        handle_diagnostic(hwnd, status="rejected", presence="absent"))
    monkeypatch.setattr(desktop_probe, "DesktopProbe", lambda *a, **k: pytest.fail("inventory diagnostic must not start GUI"))
    status = desktop_probe.main(["--inventory", "--target-hwnd", "3735796", "--output", str(tmp_path)])
    assert status == 1
    saved = json.loads((tmp_path / "known-handle-diagnostic.json").read_text(encoding="utf-8"))
    assert saved["requested_hwnd"] == 3735796 and saved["status"] == "rejected"
    assert json.loads(capsys.readouterr().out)["inventory"] == items


def test_diagnostic_startup_flag_is_bounded_and_does_not_arm_gui(monkeypatch):
    from tools import desktop_probe

    calls = []
    monkeypatch.setattr(desktop_probe, "DesktopProbe", lambda output, **settings:
                        calls.append(settings) or SimpleNamespace(root=SimpleNamespace(mainloop=lambda: None)))
    assert desktop_probe.main(["--input-startup-timeout", "45"]) == 0
    assert calls == [{"target_report": None, "input_startup_timeout": 45.0}]
    assert "safe" not in calls[0] and "arm" not in calls[0]


@pytest.mark.parametrize("value", ["0", "-1", "61", "nan", "inf", "not-a-number"])
def test_diagnostic_startup_flag_rejects_invalid_values_before_gui(monkeypatch, value):
    from tools import desktop_probe

    monkeypatch.setattr(desktop_probe, "DesktopProbe", lambda *a, **k: pytest.fail("must not create GUI"))
    with pytest.raises(SystemExit) as error:
        desktop_probe.main(["--input-startup-timeout", value])
    assert error.value.code == 2


def test_diagnostic_startup_pass_through_changes_only_startup_and_is_persisted(app, monkeypatch):
    from tibia_client import input_probe

    calls = []
    app.safe.set(True)
    app.input_startup_timeout.set("45")
    monkeypatch.setattr(input_probe, "run_input_probe", lambda *a, **settings: calls.append(settings) or {})
    report = app._input_work()(StopToken())
    assert calls[0]["startup_timeout"] == 45
    assert "action_timeout" not in calls[0]
    assert "max_frame_age_ms" not in calls[0] and "max_action_scope_ms" not in calls[0]
    assert report["selected_input_startup_timeout_s"] == 45
    app.save()
    saved = json.loads((app.output / "session.json").read_text(encoding="utf-8"))
    assert saved["input_startup_timeout_s"] == 45


def test_diagnostic_startup_default_remains_fifteen_seconds(app):
    assert app.selected_startup_timeout() == 15


def test_error_only_arm_hands_off_focus_once_without_input_countdown_or_hiding(app, monkeypatch):
    from tools import desktop_probe

    calls = []
    app.keep_visible = True
    app.safe.set(True)
    app.backend.set("error_only_physical")
    app.method.set("direct_visible_region_gdi")
    work = app._input_work()
    assert work.input_hwnd == 123
    monkeypatch.setattr(desktop_probe, "_activate_input_target", lambda hwnd: calls.append(("focus", hwnd)))
    monkeypatch.setattr(desktop_probe.threading, "Thread", lambda **kwargs:
                        SimpleNamespace(start=kwargs["target"]))
    from tibia_client import input_probe

    monkeypatch.setattr(input_probe, "run_input_probe", lambda *args, **kwargs:
                        calls.append(("dispatch", args[0])) or {"dispatch": "completed"})
    app.run("input", work)
    assert calls == [("focus", 123)]
    assert not app.root.hidden
    delay, begin = app.root.callbacks.pop()
    assert delay == 0
    begin()
    assert calls == [("focus", 123), ("dispatch", 123)]
    app.root.deiconify = lambda: pytest.fail("completion must not steal focus from the client")
    app.poll()
    assert not app.busy
    assert calls == [("focus", 123), ("dispatch", 123)]


def test_focus_action_failure_records_error_without_scheduling_input(app, monkeypatch):
    from tools import desktop_probe

    def failed_focus(hwnd):
        error = OSError("activation failed")
        error.client_availability = {"hwnd": hwnd, "is_window": False}
        raise error

    monkeypatch.setattr(desktop_probe, "_activate_input_target", failed_focus)
    work = lambda token: pytest.fail("input must not follow a failed activation")
    work.input_hwnd = 123
    app.run("input", work)
    assert not app.busy and not app.root.callbacks and not app.root.hidden
    assert app.reports[-1]["client_availability"] == {"hwnd": 123, "is_window": False}
    assert "activation failed" in app.status.get()


def test_explicit_input_countdown_remains_available_for_controlled_experiments(app, monkeypatch):
    from tools import desktop_probe

    app.input_countdown_seconds = 2
    work = lambda token: None
    work.input_hwnd = 123
    monkeypatch.setattr(desktop_probe, "_activate_input_target", lambda hwnd: None)
    app.run("input", work)
    assert app.root.callbacks[0][0] == 2000


def test_zero_countdown_cli_does_not_arm_input(monkeypatch):
    from tools import desktop_probe

    calls = []
    monkeypatch.setattr(desktop_probe, "DesktopProbe", lambda output, **settings:
                        calls.append(settings) or SimpleNamespace(root=SimpleNamespace(mainloop=lambda: None)))
    assert desktop_probe.main(["--countdown-seconds", "0"]) == 0
    assert calls == [{"target_report": None, "countdown_seconds": 0}]


def test_other_screen_placement_handles_negative_origin_and_duplicate_monitors():
    from tools.desktop_probe import _other_screen_bounds

    assert _other_screen_bounds((100, 50, 1100, 800),
                               [(0, 0, 1536, 816), (-1920, -182, 0, 850), (0, 0, 1536, 816)]) == (
                                   -1912, -174, 1000, 750)
