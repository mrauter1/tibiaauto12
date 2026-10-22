"""Error-only probe uses real input facades with fake native operations."""

from contextlib import nullcontext
from types import SimpleNamespace

from PIL import Image
import pytest

from tibia_client import input_probe as probe


def test_optional_target_trace_does_not_block_delivery_when_diagnostic_fails():
    calls, events = [], []

    def failed_trace():
        raise OSError("foreground telemetry unavailable")

    native = SimpleNamespace(foreground_window=failed_trace,
                             key_event=lambda code, up: calls.append((code, up)))
    tracked = probe._TrackedPhysical(native, lambda stage, **fields: events.append((stage, fields)),
                                     trace_key_target=True)
    tracked.key_event(79, False)
    tracked.key_event(79, True)
    assert calls == [(79, False), (79, True)]
    traces = [fields for stage, fields in events if stage == "key_dispatch_target"]
    assert len(traces) == 2
    assert all("foreground telemetry unavailable" in trace["diagnostic_error"] for trace in traces)


def test_experimental_hold_releases_key_even_when_wait_raises(monkeypatch):
    calls = []
    native = SimpleNamespace(key_event=lambda code, up: calls.append((code, up)))
    tracked = probe._TrackedPhysical(native, lambda *args, **kwargs: None, key_hold_ms=50)
    monkeypatch.setattr(probe.time, "monotonic", lambda: 0)

    def interrupted(seconds):
        raise RuntimeError("wait interrupted")

    monkeypatch.setattr(probe.time, "sleep", interrupted)
    tracked.key_event(79, False)
    with pytest.raises(RuntimeError, match="wait interrupted"):
        tracked.key_event(79, True)
    assert calls == [(79, False), (79, True)]


class Native:
    def __init__(self, calls, failure=None):
        self.calls, self.failure, self.cursor = calls, failure, (40, 50)

    def is_window(self, hwnd):
        pytest.fail("availability checked before a reported error")

    def foreground_window(self):
        pytest.fail("foreground preflight must not run")

    def client_size(self, hwnd):
        pytest.fail("input geometry preflight must not run")

    def client_to_screen(self, hwnd, point):
        self.calls.append(("translate", tuple(point)))
        return point[0] + 100, point[1] + 200

    def cursor_position(self):
        return self.cursor

    def set_cursor_position(self, point):
        self.cursor = tuple(point)
        self.calls.append(("cursor", self.cursor))

    def key_event(self, code, up):
        self.calls.append(("key", code, up))
        if self.failure is not None and code == 0x4F and not up:
            raise self.failure

    def mouse_event(self, flags):
        self.calls.append(("button", flags))


def run(monkeypatch, tmp_path, operation="ctrl_o", *, failure=None, capture_failure=False, target=None):
    import core.MoveMouse as physical
    from tibia_client import direct_capture, window_target

    calls, events, diagnostics = [], [], []
    native = Native(calls, failure)
    monkeypatch.setattr(physical, "_WindowsPhysicalBackend", lambda: native)
    monkeypatch.setattr(window_target, "_native_backend", lambda:
                        SimpleNamespace(physical_pixels=nullcontext))
    monkeypatch.setattr(probe.time, "sleep", lambda seconds: None)

    def diagnose(hwnd):
        calls.append(("diagnose", hwnd))
        diagnostics.append({"hwnd": hwnd, "is_window": False})
        return diagnostics[-1]

    def capture(hwnd, size):
        calls.append(("capture", hwnd))
        if capture_failure:
            error = RuntimeError("capture failed")
            error.client_availability = diagnose(hwnd)
            raise error
        return Image.new("RGB", size or (160, 120), "green")

    monkeypatch.setattr(direct_capture, "diagnose_client_availability", diagnose)
    monkeypatch.setattr(direct_capture, "capture_direct_host_client", capture)
    spec = {"hwnd": 123, "backend": "error_only_physical", "operation": operation,
            "target": None if target is None else target.to_dict(), "expected_size": (160, 120),
            "point": (50, 60) if operation == "left_click" else None,
            "point_patch": None, "capture_method": "direct_visible_region_gdi",
            "after_path": str(tmp_path / "after.png")}
    emit = lambda stage, **fields: events.append({"stage": stage, **fields})
    bundle = probe._build_facade(spec, emit)
    result = probe._run_error_only(bundle, spec, emit)
    return result, calls, events, diagnostics


@pytest.mark.parametrize("operation", ["ctrl_o", "escape", "left_click"])
def test_success_dispatches_before_evidence_with_zero_checks(monkeypatch, tmp_path, operation):
    result, calls, events, diagnostics = run(monkeypatch, tmp_path, operation)
    assert result["status"] == "completed" and result["error"] is None
    assert result["availability_policy"] == "on_error"
    assert result["pre_action_capture"] is False and diagnostics == []
    stages = [event["stage"] for event in events]
    assert stages.index("dispatch_complete") < stages.index("after_capture_start")
    assert "before_capture_start" not in stages and "guard_start" not in stages
    assert result["availability_diagnostics"] == []
    assert calls[-1] == ("capture", 123)


def test_failure_releases_keys_then_diagnoses_once_without_retry(monkeypatch, tmp_path):
    failure = RuntimeError("native key failed")
    result, calls, events, diagnostics = run(monkeypatch, tmp_path, failure=failure)
    assert result["status"] == "failed" and "native key failed" in result["error"]
    assert calls[:4] == [("key", 0x11, False), ("key", 0x4F, False),
                         ("key", 0x4F, True), ("key", 0x11, True)]
    assert calls[4] == ("diagnose", 123) and len(diagnostics) == 1
    assert result["client_availability"] == diagnostics[0]
    assert [event["stage"] for event in events].count("dispatch_start") == 1


def test_capture_error_is_after_dispatch_and_keeps_its_diagnostic(monkeypatch, tmp_path):
    result, calls, events, diagnostics = run(monkeypatch, tmp_path, capture_failure=True)
    assert result["status"] == "failed" and result["error"] is None
    assert result["capture_error"] == "RuntimeError: capture failed"
    assert calls[-2:] == [("capture", 123), ("diagnose", 123)]
    assert len(diagnostics) == 1 and result["client_availability"] == diagnostics[0]
    assert any(event["stage"] == "dispatch_complete" for event in events)


def test_sandbox_maps_viewport_point_without_readiness_pixels(monkeypatch, tmp_path):
    from tibia_client.window_target import WindowTarget

    target = WindowTarget.build((160, 120), (10, 30, 150, 110))
    result, calls, events, diagnostics = run(monkeypatch, tmp_path, "left_click", target=target)
    assert result["status"] == "completed" and diagnostics == []
    # Probe point is host-client local. The target facade maps viewport and back once.
    assert ("translate", (50, 60)) in calls
    assert ("button", 2) in calls and ("button", 4) in calls
