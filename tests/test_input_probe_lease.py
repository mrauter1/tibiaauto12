"""One-frame worker integration with synthetic pixels and fake native adapters."""

from contextlib import nullcontext
import hashlib
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from tibia_client import input_probe as probe
from tibia_client.window_target import WindowTarget, make_focus_guard


BACKEND = "frame_lease_region_gdi_overlay_aware"
METHOD = "visible_region_gdi_overlay_aware"


def fixture_target():
    active = Image.new("RGB", (160, 120), (20, 60, 160))
    inactive = Image.new("RGB", active.size, (120, 120, 120))
    for image in (active, inactive):
        image.putpixel((20, 10), (240, 240, 240))
    target = WindowTarget.build(active.size, (10, 30, 150, 110),
                               make_focus_guard(active, inactive, (15, 5, 95, 25)))
    return target, active, inactive


class Native:
    def __init__(self, clock):
        self.events, self.clock, self.cursor = [], clock, (20, 30)
        self.slow_modifier = False

    def is_window(self, hwnd):
        return hwnd == 123

    def foreground_window(self):
        return 123

    def client_size(self, hwnd):
        return (160, 120)

    def client_to_screen(self, hwnd, point):
        return point[0] + 100, point[1] + 200

    def cursor_position(self):
        return self.cursor

    def set_cursor_position(self, point):
        self.cursor = tuple(point)
        self.events.append(("cursor", self.cursor))

    def key_event(self, code, up):
        self.events.append(("key", code, up))
        if self.slow_modifier and code == 0x11 and not up:
            self.clock.value += 0.3

    def mouse_event(self, flags):
        self.events.append(("button", flags))


def leased_worker(monkeypatch, tmp_path, *, operation="ctrl_o", verdict="no_known_veto",
                  expected_dialog=None, capture_duration=0.1, fail_on=None,
                  inactive=False, changed_patch=False, slow_modifier=False, slow_png=False):
    import core.MoveMouse as physical
    from tibia_client import probe_known_dialog, probe_lease_native, probe_region_gdi, window_target

    target, active, inactive_image = fixture_target()
    image = inactive_image if inactive else active
    point = (50, 50) if operation in {"move", "left_click", "right_click"} else None
    patch = None
    if point is not None:
        box = [34, 34, 67, 67]
        patch = {"box": box, "sha256_rgb": hashlib.sha256(active.crop(box).tobytes()).hexdigest()}
        if changed_patch:
            image.putpixel(point, (200, 30, 40))
    clock = SimpleNamespace(value=100.0)
    native = Native(clock)
    native.slow_modifier = slow_modifier
    events, capture_calls = [], []
    checker = SimpleNamespace(baseline={"root_hwnd": 123}, telemetry=[])

    def recheck():
        checker.telemetry.append({"sequence": len(checker.telemetry) + 1})
        if fail_on == len(checker.telemetry):
            raise RuntimeError("host focus changed between key phases")
        return True

    checker.recheck = recheck

    def capture(hwnd, size):
        capture_calls.append((hwnd, size))
        clock.value += capture_duration
        return image.copy()

    def sleep(seconds):
        clock.value += seconds

    template = SimpleNamespace(name="open_channel", source_size=target.host_size,
                               viewport=target.viewport, metadata=lambda: {"name": "open_channel"})
    monkeypatch.setattr(physical, "_WindowsPhysicalBackend", lambda: native)
    monkeypatch.setattr(window_target, "_native_backend", lambda: SimpleNamespace(physical_pixels=nullcontext))
    monkeypatch.setattr(probe_lease_native, "bind", lambda *a, **k: checker)
    monkeypatch.setattr(probe_region_gdi, "capture_client_region_gdi_overlay_aware", capture)
    monkeypatch.setattr(probe_known_dialog, "load_template_report", lambda path:
                        (template, 0.9, {"report_sha256": "b" * 64}))
    monkeypatch.setattr(probe_known_dialog, "evaluate_image", lambda t, frame, **kwargs:
                        {"verdict": verdict, "frame": {"rgb_sha256": hashlib.sha256(frame.tobytes()).hexdigest()}})
    monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=lambda: clock.value, sleep=sleep))
    original_save = Image.Image.save

    def save(frame, path, *args, **kwargs):
        if isinstance(path, (str, Path)):
            events.append(("png_save", str(path)))
            if slow_png:
                clock.value += 2.0
        return original_save(frame, path, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", save)
    spec = {"hwnd": 123, "target": target.to_dict(), "backend": BACKEND,
            "capture_method": METHOD, "operation": operation, "point": point,
            "point_patch": patch, "expected_size": target.host_size,
            "known_dialog_report": str(tmp_path / "known.json"), "expected_dialog": expected_dialog,
            "before_path": str(tmp_path / "before.png"), "after_path": str(tmp_path / "after.png")}

    def emit(stage, **fields):
        events.append((stage, fields))

    bundle = probe._build_facade(spec, emit)
    result = probe._run_frame_lease(bundle, spec, emit)
    return result, native, events, capture_calls, checker


def test_one_frame_is_shared_by_checks_and_png_save_follows_dispatch(monkeypatch, tmp_path):
    result, native, events, captures, checker = leased_worker(monkeypatch, tmp_path, slow_png=True)
    assert result["status"] == "completed"
    assert result["frame_lease"]["status"] == "closed"
    assert result["frame_lease"]["max_frame_age_ms"] == 1000
    assert result["frame_lease"]["max_action_scope_ms"] == 250
    assert len(captures) == 2  # Before once, then after once; never within compound input.
    stages = [event[0] for event in events]
    assert stages.index("dispatch_complete") < stages.index("png_save")
    assert stages.index("frame_lease_complete") < stages.index("png_save")
    assert native.events == [("key", 0x11, False), ("key", 0x4F, False),
                             ("key", 0x4F, True), ("key", 0x11, True)]
    assert len(checker.telemetry) >= 5
    assert result["frame_assessment"]["frame_rgb_sha256"] == result["known_dialog"]["frame"]["rgb_sha256"]
    assert result["frame_lease"]["guest_changes_between_input_phases"] == "unverified"


@pytest.mark.parametrize("operation", ["ctrl_o", "left_click", "right_click", "move"])
def test_known_modal_refuses_open_or_mouse_input_before_any_dispatch(monkeypatch, tmp_path, operation):
    result, native, _events, captures, _ = leased_worker(monkeypatch, tmp_path,
                                                       operation=operation, verdict="blocked_known_dialog")
    assert result["status"] == "failed" and native.events == []
    assert result["frame_lease"]["status"] == "revoked" and len(captures) == 1
    assert (tmp_path / "before.png").exists()


@pytest.mark.parametrize("expected,allowed", [(None, False), ("other_dialog", False), ("open_channel", True)])
def test_only_explicit_recognized_escape_can_close_modal(monkeypatch, tmp_path, expected, allowed):
    result, native, _, captures, _ = leased_worker(monkeypatch, tmp_path, operation="escape",
                                                  verdict="blocked_known_dialog", expected_dialog=expected)
    assert (result["status"] == "completed") is allowed
    assert native.events == ([("key", 0x1B, False), ("key", 0x1B, True)] if allowed else [])
    assert len(captures) == (2 if allowed else 1)


@pytest.mark.parametrize("settings", [{"inactive": True}, {"verdict": "unknown"},
                                      {"capture_duration": 1.1}, {"operation": "left_click", "changed_patch": True}])
def test_false_unknown_or_stale_frame_evidence_never_dispatches(monkeypatch, tmp_path, settings):
    result, native, _, captures, _ = leased_worker(monkeypatch, tmp_path, **settings)
    assert result["status"] == "failed" and native.events == [] and len(captures) == 1
    assert result["frame_lease"]["status"] == "revoked"


@pytest.mark.parametrize("settings", [{"fail_on": 5}, {"slow_modifier": True}])
def test_phase_failure_or_scope_expiry_releases_held_ctrl_without_recapture(monkeypatch, tmp_path, settings):
    result, native, _, captures, _ = leased_worker(monkeypatch, tmp_path, **settings)
    assert result["status"] == "failed" and result["frame_lease"]["status"] == "revoked"
    assert native.events == [("key", 0x11, False), ("key", 0x11, True)]
    assert len(captures) == 2  # The second frame is evidence after the failed dispatch.


@pytest.mark.parametrize("verdict,ready", [("no_known_veto", True), ("blocked_known_dialog", False)])
def test_readiness_uses_one_frame_without_input(monkeypatch, tmp_path, verdict, ready):
    result, native, _, captures, _ = leased_worker(monkeypatch, tmp_path, operation="readiness", verdict=verdict)
    assert result["readiness"]["ready"] is ready
    assert result["status"] == "completed" and native.events == [] and len(captures) == 1


def test_mouse_mapping_and_destination_use_calibrated_host_point(monkeypatch, tmp_path):
    result, native, _, captures, _ = leased_worker(monkeypatch, tmp_path, operation="left_click")
    assert result["status"] == "completed" and len(captures) == 2
    assert ("cursor", (150, 250)) in native.events
    assert result["cursor_destination"]["screen_position"] == [150, 250]
    assert native.events[-1] == ("cursor", (20, 30))


@pytest.mark.parametrize("fail_on", [10, 12])
def test_revoked_endpoint_or_restore_check_marks_result_failed_after_completed_click(monkeypatch, tmp_path, fail_on):
    result, native, events, captures, _ = leased_worker(monkeypatch, tmp_path,
                                                      operation="left_click", fail_on=fail_on)
    assert result["status"] == "failed" and result["error"]
    assert result["frame_lease"]["status"] == "revoked"
    assert "dispatch_complete" in [stage for stage, _value in events]
    assert result["cursor_restore"]["status"] == "failed"
    assert native.events.count(("button", 2)) == native.events.count(("button", 4)) == 1
    assert len(captures) == 2  # Before-action frame and after evidence; no input-phase recapture.


def test_lease_public_api_requires_explicit_capture_template_and_mouse_patch(tmp_path):
    target, _, _ = fixture_target()
    with pytest.raises(ValueError, match="region-GDI"):
        probe.run_input_probe(123, BACKEND, "ctrl_o", tmp_path, target=target)
    with pytest.raises(ValueError, match="known_dialog_report"):
        probe.run_input_probe(123, BACKEND, "ctrl_o", tmp_path, target=target, capture_method=METHOD)
    with pytest.raises(ValueError, match="point_patch"):
        probe.run_input_probe(123, BACKEND, "move", tmp_path, target=target, capture_method=METHOD,
                              known_dialog_report=tmp_path / "report.json", point=(50, 50))
