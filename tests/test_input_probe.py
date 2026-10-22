"""Probe orchestration and adapters; no test executes a native desktop API."""

import json
from contextlib import nullcontext
import hashlib
import sys
from types import SimpleNamespace

from PIL import Image
import pytest

from tibia_client import input_probe as probe
from tibia_client.window_target import WindowTarget, make_focus_guard


@pytest.fixture(autouse=True)
def isolated_quarantine(monkeypatch):
    monkeypatch.setattr(probe, "_quarantined", set())


def sandbox_target():
    active = Image.new("RGB", (160, 120), (20, 60, 160))
    inactive = Image.new("RGB", (160, 120), (120, 120, 120))
    for image in (active, inactive):
        image.putpixel((20, 10), (240, 240, 240))
    return WindowTarget.build((160, 120), (10, 30, 150, 110),
                              make_focus_guard(active, inactive, (15, 5, 95, 25)))


class FakeClock:
    value = 0.0

    def monotonic(self):
        return self.value


class FakeProcess:
    def __init__(self):
        self.pid = 456
        self.alive = False
        self.starts = self.terminations = self.kills = 0
        self.joins = []
        self.closed = False

    def start(self):
        self.starts += 1
        self.alive = True

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminations += 1
        self.alive = False

    def kill(self):
        self.kills += 1
        self.alive = False

    def join(self, timeout):
        self.joins.append(timeout)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, clock, responses=(), command="run_once"):
        self.clock, self.responses, self.command = clock, list(responses), command
        self.sent, self.received = [], []
        self.closed = False

    def poll(self, timeout):
        if self.responses and self.responses[0][0] <= self.clock.value + timeout:
            self.clock.value = max(self.clock.value, self.responses[0][0])
            return True
        self.clock.value += timeout
        return False

    def recv(self):
        if not self.responses:
            return self.command
        _, item = self.responses.pop(0)
        self.received.append(item)
        return item

    def send(self, item):
        if item == "run_once":
            assert {"type": "ready"} in self.received
        self.sent.append(item)

    def close(self):
        self.closed = True


def orchestrator(monkeypatch, responses):
    clock, process = FakeClock(), FakeProcess()
    parent, child = FakeConnection(clock, responses), FakeConnection(clock)
    calls, cleanup = [], []

    def new_process(**kwargs):
        calls.append(kwargs)
        return process

    context = SimpleNamespace(Pipe=lambda **kwargs: (parent, child), Process=new_process)
    monkeypatch.setattr(probe, "multiprocessing", SimpleNamespace(get_context=lambda kind: context))
    monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=clock.monotonic))

    def release(hwnd, backend, held, point):
        cleanup.append((hwnd, backend, set(held), point))
        return {"attempted": True, "results": [{"status": "completed"}]}

    monkeypatch.setattr(probe, "_cleanup_held", release)
    return SimpleNamespace(clock=clock, process=process, parent=parent, child=child,
                           calls=calls, cleanup=cleanup)


def event(stage, **values):
    return {"type": "event", "stage": stage, **values}


def test_cold_start_has_independent_action_deadline_and_one_command(monkeypatch, tmp_path):
    state = orchestrator(monkeypatch, [(7, {"type": "ready"}),
                                      (7.1, event("dispatch_start")),
                                      (7.2, event("dispatch_complete")),
                                      (7.25, {"type": "result", "status": "completed"})])
    result = probe.run_input_probe(123, "physical", "ctrl_o", tmp_path,
                                   startup_timeout=8, action_timeout=0.5)
    assert result["status"] == result["dispatch"] == "completed"
    assert state.parent.sent == ["run_once"]
    assert state.process.starts == 1 and state.process.closed
    assert result["worker_reaped"]
    assert result["input_acceptance"] == result["operator_confirmation"] == "unknown"
    assert state.cleanup == []
    json.dumps(result, allow_nan=False)


def test_startup_timeout_never_arms_action(monkeypatch, tmp_path):
    state = orchestrator(monkeypatch, [(3, {"type": "ready"})])
    result = probe.run_input_probe(123, "messages", "escape", tmp_path, startup_timeout=1)
    assert result["status"] == "startup_timeout"
    assert result["dispatch"] == "not_attempted"
    assert state.parent.sent == [] and state.cleanup == []
    assert state.process.terminations == 1 and state.process.closed


def test_delayed_spawn_is_separate_from_readiness_wait_allowance(monkeypatch, tmp_path):
    state = orchestrator(monkeypatch, [(6, {"type": "ready"}),
                                      (6.1, {"type": "result", "status": "completed"})])
    original_start = state.process.start

    def delayed_start():
        state.clock.value += 5
        original_start()

    state.process.start = delayed_start
    result = probe.run_input_probe(123, "physical", "escape", tmp_path,
                                   startup_timeout=1.5, action_timeout=1)
    assert result["status"] == "completed"
    assert result["process_start_duration_ms"] == 5000
    assert result["startup_deadline_basis"] == "after_process_start_returns"
    assert result["timings"]["startup_wall_ms"] == 6000
    assert result["events"][:2] == [
        {"stage": "spawn_start", "elapsed_ms": 0},
        {"stage": "spawn_complete", "succeeded": True, "duration_ms": 5000,
         "elapsed_ms": 5000},
    ]
    assert state.parent.sent == ["run_once"]
    assert result["worker_reaped"]
    json.dumps(result, allow_nan=False)


def test_action_timeout_reaps_and_releases_only_possibly_held_control(monkeypatch, tmp_path):
    state = orchestrator(monkeypatch, [(0.1, {"type": "ready"}),
                                      (0.2, event("dispatch_start")),
                                      (0.3, event("held", kind="key", code=0x11, held=True))])
    result = probe.run_input_probe(123, "guarded_physical", "ctrl_o", tmp_path,
                                   target=sandbox_target(), action_timeout=0.5)
    assert result["status"] == "action_timeout"
    assert state.parent.sent == ["run_once"]
    assert state.cleanup == [(123, "guarded_physical", {("key", 0x11)}, None)]
    assert result["cleanup"]["attempted"] and result["worker_reaped"]
    assert state.process.starts == 1


def test_cancellation_during_slow_guard_reaps_without_repeated_dispatch(monkeypatch, tmp_path):
    state = orchestrator(monkeypatch, [(0.1, {"type": "ready"}),
                                      (0.2, event("held", kind="key", code=0x11, held=True))])

    class Token:
        def checkpoint(self):
            if state.clock.value >= 0.25:
                raise RuntimeError("cancelled by operator")

    result = probe.run_input_probe(123, "physical", "ctrl_o", tmp_path, token=Token())
    assert result["status"] == "cancelled"
    assert state.parent.sent == ["run_once"]
    assert state.cleanup[0][2] == {("key", 0x11)}
    assert state.process.closed and state.process.starts == 1


def test_cancelled_at_ready_never_sends_run(monkeypatch, tmp_path):
    state = orchestrator(monkeypatch, [(0.1, {"type": "ready"})])

    class Token:
        def checkpoint(self):
            if state.parent.received:
                raise RuntimeError("cancelled before arm")

    result = probe.run_input_probe(123, "physical", "escape", tmp_path, token=Token())
    assert result["status"] == "cancelled"
    assert state.parent.sent == [] and not state.cleanup


def test_confirmed_release_prevents_redundant_parent_release(monkeypatch, tmp_path):
    state = orchestrator(monkeypatch, [(0.1, {"type": "ready"}),
                                      (0.2, event("held", kind="button", code=2, held=True)),
                                      (0.3, event("held", kind="button", code=2, held=False))])
    result = probe.run_input_probe(123, "messages", "left_click", tmp_path,
                                   point=(3, 4), action_timeout=0.5)
    assert result["status"] == "action_timeout" and state.cleanup == []


def test_cleanup_is_attempted_even_when_worker_stop_raises(monkeypatch, tmp_path):
    state = orchestrator(monkeypatch, [(0.1, {"type": "ready"}),
                                      (0.2, event("held", kind="key", code=0x11, held=True))])

    def fail_stop(_process):
        raise OSError("termination failed")

    monkeypatch.setattr(probe, "_stop_and_reap", fail_stop)
    result = probe.run_input_probe(123, "physical", "ctrl_o", tmp_path, action_timeout=0.5)
    assert result["status"] == "failed" and result["worker_reaped"] is False
    assert state.cleanup[0][2] == {("key", 0x11)}
    assert "termination failed" in result["stop_error"]


def test_cleanup_key_parameter_has_release_transition_and_scan_code():
    calls = []

    def mapper(code, map_type):
        calls.append((code, map_type))
        return 0x1D

    parameter = probe._release_key_parameter(0x11, mapper)
    assert parameter == 0xC01D0001
    assert calls == [(0x11, 0)]


@pytest.mark.parametrize("operation", ["type", "enter", "ctrl_a", "login", "drag", "", None])
def test_arbitrary_operations_rejected_before_worker(operation, tmp_path):
    with pytest.raises(ValueError, match="operation"):
        probe.run_input_probe(123, "physical", operation, tmp_path)


@pytest.mark.parametrize("point", [None, (True, 3), (1.5, 3), (-1, 3), (1, 40000), "3,4"])
def test_strict_mouse_coordinates(point, tmp_path):
    with pytest.raises(ValueError, match="point"):
        probe.run_input_probe(123, "physical", "left_click", tmp_path, point=point)


@pytest.mark.parametrize("name", ["startup_timeout", "action_timeout"])
@pytest.mark.parametrize("value", [True, "2", 0, -1, 61, float("nan"), float("inf")])
def test_deadline_bounds_are_strict(name, value, tmp_path):
    with pytest.raises((TypeError, ValueError), match=name):
        probe.run_input_probe(123, "physical", "escape", tmp_path, **{name: value})


def test_guarded_backend_requires_calibrated_sandbox(tmp_path):
    for target in (None, WindowTarget()):
        with pytest.raises(ValueError, match="target|Sandbox"):
            probe.run_input_probe(123, "guarded_physical", "escape", tmp_path, target=target)


def test_viewport_mapping_uses_host_coordinate_offset():
    target = sandbox_target()
    assert probe._mapped_point("guarded_physical", target, (14, 35)) == (4, 5)
    assert probe._mapped_point("physical", target, (14, 35)) == (14, 35)
    for point in ((9, 40), (14, 29), (150, 40), (15, 110)):
        with pytest.raises(ValueError, match="outside"):
            probe._mapped_point("guarded_physical", target, point)


def test_expected_size_is_forwarded_and_point_must_fit_preview(monkeypatch, tmp_path):
    state = orchestrator(monkeypatch, [(0.1, {"type": "ready"}),
                                      (0.2, {"type": "result", "status": "completed"})])
    result = probe.run_input_probe(123, "physical", "move", tmp_path,
                                   point=(5, 6), expected_size=(100, 200))
    assert state.calls[0]["args"][1]["expected_size"] == (100, 200)
    assert result["expected_size"] == [100, 200]
    with pytest.raises(ValueError, match="outside expected_size"):
        probe.run_input_probe(123, "physical", "move", tmp_path,
                              point=(101, 6), expected_size=(100, 200))


class Engine:
    def __init__(self):
        self.calls = []

    def ScreenPosition(self):
        self.calls.append(("snapshot",))
        return (30, 40)

    def RestoreScreenPosition(self, point):
        self.calls.append(("restore", point))

    def PressHotkey(self, *args):
        self.calls.append(("hotkey", *args))

    def Press(self, *args):
        self.calls.append(("key", *args))

    def LeftClick(self, point):
        self.calls.append(("left", point))

    def RightClick(self, point):
        self.calls.append(("right", point))

    def MoveTo(self, *point):
        self.calls.append(("move", *point))


@pytest.mark.parametrize("operation,expected", [("ctrl_o", ("hotkey", "ctrl", "o")),
                                               ("escape", ("key", "esc")),
                                               ("left_click", ("left", (4, 5))),
                                               ("right_click", ("right", (4, 5))),
                                               ("move", ("move", 4, 5))])
def test_dispatch_has_one_allowed_action_and_restores_mouse(operation, expected):
    engine = Engine()
    events = []
    result = probe._perform_input(engine, operation, (4, 5),
                                   lambda stage, **kw: events.append(stage))
    assert engine.calls.count(expected) == 1
    assert events.count("dispatch_start") == events.count("dispatch_complete") == 1
    assert result["error"] is None
    if operation in probe._MOUSE:
        assert engine.calls == [("snapshot",), expected, ("snapshot",), ("restore", (30, 40))]
        assert result["cursor_restore"]["status"] == "completed"
        assert result["cursor_destination"]["screen_position"] == [30, 40]


def test_restoration_failure_does_not_overwrite_dispatch_success():
    engine = Engine()

    def failed(_point):
        raise RuntimeError("focus changed after click")

    engine.RestoreScreenPosition = failed
    result = probe._perform_input(engine, "left_click", (4, 5), lambda *a, **kw: None)
    assert result["error"] is None
    assert result["cursor_restore"]["status"] == "failed"
    assert "focus changed" in result["cursor_restore"]["error"]


def test_invalid_mouse_point_has_no_cursor_side_effects():
    engine = Engine()
    with pytest.raises(ValueError):
        probe._perform_input(engine, "move", None, lambda *a, **kw: None)
    assert engine.calls == []


def test_message_adapter_refuses_background_and_never_steals_focus():
    calls = []
    native = SimpleNamespace(is_window=lambda hwnd: True, is_iconic=lambda hwnd: False,
                             foreground_window=lambda: 999,
                             send_message=lambda *a: calls.append(a),
                             post_message=lambda *a: calls.append(a))
    adapter = probe._ForegroundMessages(native, 123, lambda *a, **kw: None)
    with pytest.raises(RuntimeError, match="foreground"):
        adapter.send_message(123, 0x100, 0x11, 0)
    with pytest.raises(RuntimeError, match="foreground"):
        adapter.post_message(123, 0x200, 0, 0)
    assert calls == []
    adapter.send_message(123, 0x101, 0x11, 0)
    assert len(calls) == 1  # A held modifier must still be released after focus loss.


def test_readiness_worker_measures_guard_directly_without_capture_or_input(monkeypatch):
    connection = FakeConnection(FakeClock())
    checks = []

    def check():
        checks.append(True)
        raise RuntimeError("guest focus inactive")

    monkeypatch.setattr(probe, "_build_facade", lambda *a: SimpleNamespace(
        engine=SimpleNamespace(check_ready=check)))
    probe._worker(connection, {"operation": "readiness"})
    result = connection.sent[-1]
    assert result["status"] == "completed"
    assert result["readiness"]["ready"] is False
    assert len(checks) == 1
    stages = [item.get("stage") for item in connection.sent]
    assert "before_capture_start" not in stages and "dispatch_start" not in stages


def test_guard_rejection_has_complete_telemetry_and_preserves_exception():
    events = []

    def reject():
        raise RuntimeError("inactive guest")

    with pytest.raises(RuntimeError, match="inactive guest"):
        probe._check_ready(reject, lambda stage, **values: events.append((stage, values)))
    assert events[0] == ("guard_start", {})
    assert events[1][0] == "guard_complete"
    assert events[1][1]["ready"] is False


def test_worker_does_nothing_if_parent_never_arms(monkeypatch):
    connection = FakeConnection(FakeClock(), command="close")
    monkeypatch.setattr(probe, "_build_facade", lambda *a: None)
    probe._worker(connection, {"operation": "escape"})
    assert connection.sent[-1] == {"type": "ready"}
    assert not any(item.get("stage") == "dispatch_start" for item in connection.sent)


def test_readiness_report_keeps_acceptance_unknown(monkeypatch, tmp_path):
    orchestrator(monkeypatch, [(0.1, {"type": "ready"}),
                              (0.2, {"type": "result", "status": "completed",
                                     "readiness": {"ready": False, "error": "inactive"}})])
    result = probe.run_input_probe(123, "guarded_physical", "readiness", tmp_path,
                                   target=sandbox_target())
    assert result["ready"] is False and result["status"] == "completed"
    assert result["input_acceptance"] == "unknown"
    assert result["dispatch"] == "not_attempted"
    assert result["before_path"] is result["after_path"] is None


def test_worker_rejects_changed_preview_geometry_before_any_dispatch(monkeypatch, tmp_path):
    from tibia_client import window_target

    connection = FakeConnection(FakeClock())
    engine = Engine()
    monkeypatch.setattr(probe, "_build_facade", lambda *a: SimpleNamespace(
        engine=engine, geometry={"size": (160, 120)}, target=None, pixels=nullcontext))

    def changed_capture(hwnd, expected_size):
        assert hwnd == 123 and expected_size == (160, 120)
        raise RuntimeError("host client size changed after preview")

    monkeypatch.setattr(window_target, "capture_host_client", changed_capture)
    probe._worker(connection, {"hwnd": 123, "operation": "left_click", "backend": "physical",
                               "point": (5, 6), "before_path": str(tmp_path / "before.png")})
    assert engine.calls == []
    assert connection.sent[-1]["status"] == "failed"
    assert "changed after preview" in connection.sent[-1]["error"]


def test_timing_summary_uses_worker_durations_and_parent_startup_wall():
    events = [
        {"stage": "worker_ready", "elapsed_ms": 7000},
        {"stage": "before_capture_start", "elapsed_ms": 7001, "worker_elapsed_ms": 5},
        {"stage": "before_capture_complete", "elapsed_ms": 7100, "worker_elapsed_ms": 105},
        {"stage": "dispatch_start", "elapsed_ms": 7100, "worker_elapsed_ms": 106},
        {"stage": "guard_start", "elapsed_ms": 7110, "worker_elapsed_ms": 110},
        {"stage": "guard_complete", "elapsed_ms": 7150, "worker_elapsed_ms": 160},
        {"stage": "guard_start", "elapsed_ms": 7160, "worker_elapsed_ms": 170},
        {"stage": "guard_complete", "elapsed_ms": 7190, "worker_elapsed_ms": 190},
        {"stage": "dispatch_complete", "elapsed_ms": 7190, "worker_elapsed_ms": 191},
    ]
    timings = probe._timing_summary(events)
    assert timings["startup_wall_ms"] == 7000
    assert timings["before_capture_ms"] == 100
    assert timings["dispatch_ms"] == 85
    assert timings["guard_calls"] == 2
    assert timings["guard_incomplete_calls"] == 0
    assert timings["total_guard_ms"] == 70
    assert timings["after_capture_ms"] is None
    json.dumps(timings, allow_nan=False)


def test_incomplete_guard_and_dispatch_have_null_duration():
    events = [{"stage": "dispatch_start", "elapsed_ms": 100},
              {"stage": "guard_start", "elapsed_ms": 101},
              {"stage": "guard_complete", "elapsed_ms": 111},
              {"stage": "guard_start", "elapsed_ms": 112}]
    timings = probe._timing_summary(events)
    assert timings["dispatch_ms"] is None
    assert timings["total_guard_ms"] is None
    assert timings["guard_calls"] == 2 and timings["guard_incomplete_calls"] == 1


def test_unreaped_process_blocks_new_probes_until_dead_and_joined(monkeypatch, tmp_path):
    state = orchestrator(monkeypatch, [(0.1, {"type": "ready"})])
    monkeypatch.setattr(probe, "_stop_and_reap", lambda process: False)
    first = probe.run_input_probe(123, "physical", "escape", tmp_path, action_timeout=0.1)
    assert first["status"] == "failed" and first["quarantined"] is True
    assert first["worker_pid"] == 456 and first["worker_live"] is True
    second = probe.run_input_probe(123, "physical", "escape", tmp_path)
    assert second["status"] == "quarantined"
    assert second["dispatch"] == "not_attempted"
    assert state.process.starts == 1 and len(state.calls) == 1
    assert second["quarantined_workers"] == [{"pid": 456, "alive": True, "reaped": False}]
    state.process.alive = False
    assert probe._reject_quarantined() == []
    assert state.process.closed and state.process.joins[-1] == 0
    assert not probe._quarantined


def test_dead_but_unjoinable_process_remains_quarantined():
    process = FakeProcess()

    def unjoinable(timeout):
        raise RuntimeError("join could not confirm process reaping")

    process.join = unjoinable
    probe._quarantined.add(process)
    blocked = probe._reject_quarantined()
    assert len(blocked) == 1 and blocked[0]["alive"] is False
    assert "join could not confirm" in blocked[0]["error"]
    assert process in probe._quarantined and not process.closed


def test_mouse_destination_is_observed_before_restore_without_claiming_acceptance():
    engine = Engine()
    positions = iter([(30, 40), (105, 206)])

    def screen_position():
        engine.calls.append(("snapshot",))
        return next(positions)

    engine.ScreenPosition = screen_position
    events = []
    result = probe._perform_input(engine, "move", (5, 6),
                                   lambda stage, **kw: events.append(stage))
    assert result["cursor_destination"]["status"] == "completed"
    assert result["cursor_destination"]["screen_position"] == [105, 206]
    assert "does not prove game acceptance" in result["cursor_destination"]["evidence"]
    assert events.index("cursor_destination_complete") < events.index("cursor_restore_start")
    assert engine.calls[-1] == ("restore", (30, 40))


def test_destination_read_failure_is_separate_from_dispatch_and_cursor_restored():
    engine = Engine()
    reads = []

    def screen_position():
        if reads:
            raise RuntimeError("destination focus unavailable")
        reads.append(True)
        return 30, 40

    engine.ScreenPosition = screen_position
    result = probe._perform_input(engine, "left_click", (5, 6), lambda *a, **kw: None)
    assert result["error"] is None
    assert result["cursor_destination"]["status"] == "failed"
    assert result["cursor_restore"]["status"] == "completed"


def patch_for(image, box=(0, 0, 16, 16)):
    return {"box": list(box), "sha256_rgb": hashlib.sha256(
        image.convert("RGB").crop(box).tobytes()).hexdigest()}


@pytest.mark.parametrize("change", [
    {"box": [0, 0, 16, 16], "sha256_rgb": "not a digest"},
    {"box": [0, 0, 16, True], "sha256_rgb": "a" * 64},
    {"box": [-1, 0, 16, 16], "sha256_rgb": "a" * 64},
    {"box": [0, 0, 500, 500], "sha256_rgb": "a" * 64},
    {"box": [0, 0, 4, 4], "sha256_rgb": "a" * 64},
    {"box": [0, 0, 16, 16], "sha256_rgb": "a" * 64, "extra": 1},
])
def test_point_patch_strict_validation_before_worker(change, tmp_path):
    with pytest.raises(ValueError, match="point_patch"):
        probe.run_input_probe(123, "physical", "move", tmp_path, point=(5, 6),
                              expected_size=(160, 120), point_patch=change)


def test_point_patch_requires_mouse_point_and_bounded_preview(tmp_path):
    patch = {"box": [0, 0, 16, 16], "sha256_rgb": "a" * 64}
    with pytest.raises(ValueError, match="selected mouse point"):
        probe.run_input_probe(123, "physical", "escape", tmp_path, point_patch=patch)
    with pytest.raises(ValueError, match="outside expected_size"):
        probe.run_input_probe(123, "physical", "move", tmp_path, point=(5, 6),
                              expected_size=(10, 10), point_patch=patch)


def test_patch_digest_compares_fresh_rgb_region_exactly():
    image = Image.new("RGBA", (160, 120), (20, 30, 40, 0))
    patch = patch_for(image)
    assert probe._check_point_patch(image, patch)["status"] == "matched"
    image.putpixel((5, 6), (20, 30, 41, 0))
    result = probe._check_point_patch(image, patch)
    assert result["status"] == "changed"
    assert result["expected_sha256_rgb"] != result["actual_sha256_rgb"]
    assert "changed after preview" in result["error"]


def worker_input(monkeypatch, tmp_path, *, patch=None, image=None,
                 capture_method="visible_screen"):
    from tibia_client import window_target

    image = image if image is not None else Image.new("RGB", (160, 120), "blue")
    connection, engine, clock = FakeConnection(FakeClock()), Engine(), FakeClock()
    order = []
    monkeypatch.setattr(probe, "_build_facade", lambda *a: SimpleNamespace(
        engine=engine, geometry={"size": (160, 120)}, target=None, pixels=nullcontext))

    def capture(hwnd, expected_size):
        order.append("capture")
        return image.copy()

    def sleep(seconds):
        order.append(("sleep", seconds))
        clock.value += seconds

    monkeypatch.setattr(window_target, "capture_host_client", capture)
    if capture_method in ("visible_screen_overlay_aware", "visible_region_gdi_overlay_aware"):
        monkeypatch.setitem(sys.modules, "tibia_client.probe_overlay", SimpleNamespace(
            capture_client_overlay_aware=capture))
        monkeypatch.setitem(sys.modules, "tibia_client.probe_region_gdi", SimpleNamespace(
            capture_client_region_gdi_overlay_aware=capture))

        def wrong_strict_capture(*args):
            raise AssertionError("overlay-aware before/after must use its selected capture method")

        monkeypatch.setattr(window_target, "capture_host_client", wrong_strict_capture)
    monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=sleep))
    probe._worker(connection, {"hwnd": 123, "operation": "left_click", "backend": "physical",
                               "capture_method": capture_method,
                               "point": (5, 6), "point_patch": patch,
                               "before_path": str(tmp_path / "before.png"),
                               "after_path": str(tmp_path / "after.png")})
    return connection, engine, order


def test_changed_point_patch_rejected_before_cursor_or_dispatch(monkeypatch, tmp_path):
    image = Image.new("RGB", (160, 120), "blue")
    patch = patch_for(image)
    image.putpixel((5, 6), (0, 1, 255))
    connection, engine, order = worker_input(monkeypatch, tmp_path, patch=patch, image=image)
    assert engine.calls == [] and order == ["capture"]
    assert connection.sent[-1]["status"] == "failed"
    assert "changed after preview" in connection.sent[-1]["error"]
    check = next(item for item in connection.sent if item.get("stage") == "point_patch_complete")
    assert check["result"]["status"] == "changed"


def test_successful_mouse_probe_settles_once_before_after_capture(monkeypatch, tmp_path):
    image = Image.new("RGB", (160, 120), "blue")
    connection, engine, order = worker_input(monkeypatch, tmp_path,
                                            patch=patch_for(image), image=image)
    assert order == ["capture", ("sleep", 0.35), "capture"]
    assert engine.calls.count(("left", (5, 6))) == 1
    stages = [item.get("stage") for item in connection.sent]
    assert stages.index("dispatch_complete") < stages.index("settle_start")
    assert stages.index("settle_complete") < stages.index("after_capture_start")
    assert connection.sent[-1]["status"] == "completed"


def test_settle_has_no_duration_if_interrupted():
    assert probe._timing_summary([{"stage": "settle_start", "elapsed_ms": 10}])["settle_ms"] is None


@pytest.mark.parametrize("capture_method", ["printwindow", "printwindow_physical", "", None, True])
def test_input_capture_method_rejects_nonvisible_choices(capture_method, tmp_path):
    with pytest.raises(ValueError, match="capture_method"):
        probe.run_input_probe(123, "physical", "escape", tmp_path, capture_method=capture_method)


@pytest.mark.parametrize("capture_method", ["visible_screen_overlay_aware", "visible_region_gdi_overlay_aware"])
def test_overlay_aware_input_capture_is_explicit_and_reported(monkeypatch, tmp_path, capture_method):
    state = orchestrator(monkeypatch, [(0.1, {"type": "ready"}),
                                      (0.2, {"type": "result", "status": "completed"})])
    result = probe.run_input_probe(123, "physical", "escape", tmp_path,
                                   capture_method=capture_method)
    assert result["capture_method"] == capture_method
    assert state.calls[0]["args"][1]["capture_method"] == capture_method


@pytest.mark.parametrize("capture_method", ["visible_screen_overlay_aware", "visible_region_gdi_overlay_aware"])
def test_overlay_aware_worker_uses_selected_capture_only_before_and_after(monkeypatch, tmp_path, capture_method):
    connection, engine, order = worker_input(monkeypatch, tmp_path,
                                             capture_method=capture_method)
    assert connection.sent[-1]["status"] == "completed"
    assert order == ["capture", ("sleep", 0.35), "capture"]
    assert engine.calls.count(("left", (5, 6))) == 1


def test_default_capture_method_remains_strict_visible_screen(monkeypatch, tmp_path):
    state = orchestrator(monkeypatch, [(0.1, {"type": "ready"}),
                                      (0.2, {"type": "result", "status": "completed"})])
    result = probe.run_input_probe(123, "physical", "escape", tmp_path)
    assert result["capture_method"] == "visible_screen"
    assert state.calls[0]["args"][1]["capture_method"] == "visible_screen"


@pytest.mark.parametrize("backend", ["guarded_physical_overlay_aware", "guarded_region_gdi_overlay_aware"])
def test_experimental_guarded_backend_requires_sandbox_and_maps_host_points(tmp_path, backend):
    for target in (None, WindowTarget()):
        with pytest.raises(ValueError, match="target|Sandbox"):
            probe.run_input_probe(123, backend, "readiness", tmp_path,
                                  target=target)
    assert probe._mapped_point(backend, sandbox_target(), (14, 35)) == (4, 5)
    with pytest.raises(ValueError, match="outside"):
        probe._mapped_point(backend, sandbox_target(), (9, 35))


@pytest.mark.parametrize("backend", ["guarded_physical_overlay_aware", "guarded_region_gdi_overlay_aware"])
def test_experimental_guarded_readiness_is_identified_in_report(monkeypatch, tmp_path, backend):
    orchestrator(monkeypatch, [(0.1, {"type": "ready"}),
                              (0.2, {"type": "result", "status": "completed",
                                     "readiness": {"ready": True, "error": None}})])
    result = probe.run_input_probe(123, backend, "readiness", tmp_path,
                                   target=sandbox_target())
    assert result["ready"] is True and result["dispatch"] == "not_attempted"
    assert result["backend"] == backend
    assert result["capture_method"] == "visible_screen"


def build_fake_guarded(monkeypatch, backend, image):
    import core.MoveMouse as physical_module
    from tibia_client import window_target

    captures, strict, telemetry = [], [], []

    def overlay_capture(hwnd, expected_size):
        captures.append((hwnd, expected_size))
        return image.copy()

    monkeypatch.setitem(sys.modules, "tibia_client.probe_overlay", SimpleNamespace(
        capture_client_overlay_aware=(overlay_capture if backend != "guarded_region_gdi_overlay_aware"
                                     else lambda *args: pytest.fail("region candidate must use GDI helper"))))
    monkeypatch.setitem(sys.modules, "tibia_client.probe_region_gdi", SimpleNamespace(
        capture_client_region_gdi_overlay_aware=overlay_capture))
    monkeypatch.setattr(physical_module, "_WindowsPhysicalBackend", lambda: object())
    monkeypatch.setattr(window_target, "_native_backend", lambda: SimpleNamespace(
        physical_pixels=nullcontext))
    monkeypatch.setattr(window_target, "capture_target", lambda hwnd, target: strict.append((hwnd, target)))
    target = sandbox_target()
    bundle = probe._build_facade({"hwnd": 123, "backend": backend,
                                  "target": target.to_dict(), "expected_size": target.host_size},
                                 lambda stage, **values: telemetry.append((stage, values)))
    return bundle, captures, strict, telemetry


@pytest.mark.parametrize("backend", ["guarded_physical_overlay_aware", "guarded_region_gdi_overlay_aware"])
def test_experimental_guard_uses_overlay_capture_and_unchanged_focus_comparison(monkeypatch, backend):
    active = Image.new("RGB", (160, 120), (20, 60, 160))
    active.putpixel((20, 10), (240, 240, 240))
    bundle, captures, strict, telemetry = build_fake_guarded(
        monkeypatch, backend, active)
    assert bundle.engine.check_ready() is True
    assert captures == [(123, (160, 120))] and strict == []
    assert telemetry == [("guard_start", {}), ("guard_complete", {"ready": True})]
    # The same instrumented callback is retained between compound input phases.
    assert bundle.engine.backend.readiness_guard is bundle.engine.check_ready


@pytest.mark.parametrize("backend", ["guarded_physical_overlay_aware", "guarded_region_gdi_overlay_aware"])
def test_experimental_guard_rejects_inactive_guest_using_original_guard(monkeypatch, backend):
    inactive = Image.new("RGB", (160, 120), (120, 120, 120))
    inactive.putpixel((20, 10), (240, 240, 240))
    bundle, captures, strict, telemetry = build_fake_guarded(
        monkeypatch, backend, inactive)
    with pytest.raises(RuntimeError, match="inactive or changed"):
        bundle.engine.check_ready()
    assert len(captures) == 1 and strict == []
    assert telemetry[-1][0] == "guard_complete" and telemetry[-1][1]["ready"] is False


def test_strict_guarded_backend_retains_original_capture_target(monkeypatch):
    image = Image.new("RGB", (160, 120))
    bundle, captures, strict, telemetry = build_fake_guarded(monkeypatch, "guarded_physical", image)
    assert bundle.engine.check_ready() is True
    assert len(strict) == 1 and captures == []
    assert telemetry == [("guard_start", {}), ("guard_complete", {"ready": True})]


def test_calibration_and_fingerprint_are_preserved_in_parent_report(monkeypatch, tmp_path):
    orchestrator(monkeypatch, [(0.1, {"type": "ready"}),
                              (0.2, {"type": "result", "status": "completed",
                                     "readiness": {"ready": True, "error": None}})])
    target = sandbox_target()
    report = probe.run_input_probe(123, "guarded_physical_overlay_aware", "readiness", tmp_path,
                                   target=target)
    assert report["window_target"] == target.to_dict()
    assert report["target_fingerprint"] == target.fingerprint
    json.dumps(report, allow_nan=False)


def test_absent_calibration_is_null_in_parent_report(monkeypatch, tmp_path):
    orchestrator(monkeypatch, [(0.1, {"type": "ready"}),
                              (0.2, {"type": "result", "status": "completed"})])
    report = probe.run_input_probe(123, "physical", "escape", tmp_path)
    assert report["window_target"] is None and report["target_fingerprint"] is None


@pytest.mark.parametrize("calibrated", [False, True])
def test_worker_emits_reproducible_calibration_before_backend_build(monkeypatch, calibrated):
    connection = FakeConnection(FakeClock(), command="close")
    monkeypatch.setattr(probe, "_build_facade", lambda *a: None)
    target = sandbox_target() if calibrated else None
    probe._worker(connection, {"operation": "escape", "target": None if target is None else target.to_dict()})
    metadata = connection.sent[0]
    assert metadata["stage"] == "calibrated_target"
    assert metadata["window_target"] == (None if target is None else target.to_dict())
    assert metadata["target_fingerprint"] == (None if target is None else target.fingerprint)
    assert connection.sent[1]["stage"] == "backend_build_start"
    json.dumps(metadata, allow_nan=False)
