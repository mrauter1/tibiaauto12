"""Pure probe tests: no desktop, native capture, input, or live process calls."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from datetime import datetime, timezone
import json
import time

from PIL import Image
import pytest

from core.perception import CaptureError, Frame
from tibia_client import desktop_probe as probe


class FakeCapture:
    frames = []
    instances = []
    close_error = None

    def __init__(self, source, **kwargs):
        self.source = source
        self.kwargs = kwargs
        self.calls = 0
        self.closed = False
        self.last_observed_at = None
        self.results = iter(self.frames)
        self.instances.append(self)

    def capture(self, token=None):
        self.calls += 1
        if token is not None:
            token.checkpoint()
        self.last_observed_at = time.monotonic() - 0.001
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return Frame(result)

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture
def fake_capture(monkeypatch):
    FakeCapture.frames = []
    FakeCapture.instances = []
    FakeCapture.close_error = None
    monkeypatch.setattr(probe, "CaptureProcess", FakeCapture)
    return FakeCapture


@pytest.mark.parametrize("hwnd", [0, -1, True, 1.5, "123", None])
def test_source_rejects_invalid_window_handle(hwnd):
    with pytest.raises(ValueError, match="positive integer"):
        probe.CaptureSource(hwnd, "printwindow")


def test_source_rejects_unknown_backend():
    with pytest.raises(ValueError, match="printwindow_physical"):
        probe.CaptureSource(123, "automatic")


def test_source_calls_exact_printwindow_implementation(monkeypatch):
    from core import perception

    marker = object()
    calls = []

    def capture(hwnd):
        calls.append(hwnd)
        return marker

    monkeypatch.setattr(perception, "_capture_windows", capture)
    assert probe.CaptureSource(123, "printwindow").capture() is marker
    assert calls == [123]


def test_source_calls_exact_screen_implementation(monkeypatch):
    from tibia_client import window_target

    marker = object()
    calls = []

    def capture(hwnd):
        calls.append(hwnd)
        return marker

    monkeypatch.setattr(window_target, "capture_host_client", capture)
    assert probe.CaptureSource(456, "visible_screen").capture() is marker
    assert calls == [456]


def test_source_calls_explicit_overlay_aware_candidate(monkeypatch):
    from tibia_client import probe_overlay

    marker = object()
    calls = []

    def capture(hwnd):
        calls.append(hwnd)
        return marker

    monkeypatch.setattr(probe_overlay, "capture_client_overlay_aware", capture)
    assert probe.CaptureSource(456, "visible_screen_overlay_aware").capture() is marker
    assert calls == [456]


def test_source_calls_explicit_gdi_region_candidate(monkeypatch):
    from tibia_client import probe_region_gdi

    marker = object()
    calls = []

    def capture(hwnd):
        calls.append(hwnd)
        return marker

    monkeypatch.setattr(probe_region_gdi, "capture_client_region_gdi_overlay_aware", capture)
    assert probe.CaptureSource(456, "visible_region_gdi_overlay_aware").capture() is marker
    assert calls == [456]


def test_physical_printwindow_changes_only_thread_context_and_calls_exact_backend(monkeypatch):
    from core import perception
    from tibia_client import window_target

    marker = object()
    calls = []

    class Native:
        @contextmanager
        def physical_pixels(self):
            calls.append("enter_physical_pixels")
            try:
                yield
            finally:
                calls.append("restore_context")

    def capture(hwnd):
        calls.append(("printwindow", hwnd))
        return marker

    monkeypatch.setattr(window_target, "_native_backend", Native)
    monkeypatch.setattr(perception, "_capture_windows", capture)
    assert probe.CaptureSource(123, "printwindow_physical").capture() is marker
    assert calls == ["enter_physical_pixels", ("printwindow", 123), "restore_context"]


def test_physical_printwindow_restores_context_on_backend_failure(monkeypatch):
    from core import perception
    from tibia_client import window_target

    calls = []

    class Native:
        @contextmanager
        def physical_pixels(self):
            calls.append("enter")
            try:
                yield
            finally:
                calls.append("restore")

    def capture(hwnd):
        raise CaptureError("native unavailable")

    monkeypatch.setattr(window_target, "_native_backend", Native)
    monkeypatch.setattr(perception, "_capture_windows", capture)
    with pytest.raises(CaptureError, match="native unavailable"):
        probe.CaptureSource(123, "printwindow_physical").capture()
    assert calls == ["enter", "restore"]


@pytest.mark.parametrize("count", [0, -1, 101, True, 1.5, "5"])
def test_capture_sample_count_is_strictly_bounded(tmp_path, fake_capture, count):
    with pytest.raises(ValueError, match="samples"):
        probe.collect_capture(123, "printwindow", tmp_path, samples=count)
    assert not fake_capture.instances
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("size", [(0, 1), (1, -1), (True, 3), (50000, 50000), "4x3"])
def test_expected_size_is_bounded_before_capture(tmp_path, fake_capture, size):
    with pytest.raises(ValueError, match="expected_size"):
        probe.collect_capture(123, "printwindow", tmp_path, expected_size=size)
    assert not fake_capture.instances


def test_capture_saves_originals_and_separates_cold_warm_timing(tmp_path, fake_capture):
    image = Image.new("RGB", (4, 3), (1, 2, 3))
    fake_capture.frames = [image, image.copy(), Image.new("RGB", (4, 3), (4, 5, 6))]
    report = probe.collect_capture(123, "visible_screen", tmp_path, samples=3, expected_size=(4, 3))

    instance = fake_capture.instances[0]
    assert instance.kwargs == {"startup_timeout": 15.0, "timeout": 2.0,
                               "max_restarts": 0, "capture_size": (4, 3)}
    assert instance.source == probe.CaptureSource(123, "visible_screen")
    assert instance.closed and instance.calls == 3
    assert report["summary"]["received_samples"] == 3
    assert report["summary"]["unique_rgb_hashes"] == 2
    assert report["summary"]["cold_wall_ms"] >= 0
    assert report["summary"]["warm_wall_ms"]["count"] == 2
    assert report["summary"]["native_capture_ms"]["count"] == 3
    assert report["frames"][-1]["captured_monotonic"] == instance.last_observed_at
    assert report["frames"][-1]["captured_monotonic_basis"] == "worker_observation_start"
    assert report["frames"][-1]["captured_utc_basis"] == "parent_return"
    assert datetime.fromisoformat(report["frames"][-1]["captured_utc"]).tzinfo == timezone.utc
    assert report["frames"][0]["sha256_rgb"] == hashlib.sha256(image.tobytes()).hexdigest()
    assert report["frames"][1]["changed_ratio_from_previous"] == 0
    assert report["frames"][2]["changed_ratio_from_previous"] == 1
    assert report["gates"]["transport"]["status"] == "pass"
    assert report["gates"]["native_geometry"]["status"] == "pass"
    assert report["gates"]["game_content"]["status"] == "unknown"
    assert report["gates"]["freshness"]["status"] == "unknown"
    assert report["gates"]["input_acceptance"]["status"] == "not_run"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "visible_screen-001.png", "visible_screen-002.png", "visible_screen-003.png"]
    with Image.open(report["frames"][0]["path"]) as saved:
        assert saved.mode == "RGB" and saved.size == image.size
        assert saved.tobytes() == image.tobytes()
    assert json.loads(json.dumps(report)) == report


def test_nonblack_api_success_never_asserts_game_content_or_freshness(tmp_path, fake_capture):
    fake_capture.frames = [Image.new("RGB", (2, 2), "white")]
    report = probe.collect_capture(123, "printwindow", tmp_path, samples=1)
    assert report["gates"]["transport"]["status"] == "pass"
    assert report["gates"]["nonblack"]["status"] == "pass"
    assert report["gates"]["native_geometry"]["status"] == "unknown"
    assert report["gates"]["game_content"]["status"] == "unknown"
    assert report["gates"]["freshness"]["status"] == "unknown"
    assert report["summary"]["warm_wall_ms"] == {"count": 0, "median": None, "p95": None}


def test_overlay_candidate_reports_policy_without_claiming_runtime_metadata(tmp_path, fake_capture):
    fake_capture.frames = [Image.new("RGB", (2, 2), "white")]
    report = probe.collect_capture(123, "visible_screen_overlay_aware", tmp_path, samples=1)
    policy = report["overlay_annotation_policy"]
    assert policy["experimental"] is True
    assert policy["point_hit_checks"] == "unchanged"
    assert policy["runtime_qualification_metadata"] == "not_transported_by_capture_process"
    assert report["gates"]["game_content"]["status"] == "unknown"


def test_gdi_candidate_reports_region_and_unchanged_check_policy(tmp_path, fake_capture):
    fake_capture.frames = [Image.new("RGB", (2, 2), "white")]
    report = probe.collect_capture(123, "visible_region_gdi_overlay_aware", tmp_path, samples=1)
    policy = report["region_capture_policy"]
    assert policy["experimental"] is True
    assert policy["full_virtual_desktop_grab"] is False
    assert policy["include_layered_windows"] is False
    assert policy["raster_operation"] == "SRCCOPY"
    assert policy["foreground_geometry_occlusion_point_checks"] == "unchanged"
    assert "overlay_annotation_policy" in report
    assert report["gates"]["game_content"]["status"] == "unknown"


@pytest.mark.parametrize("observed", [None, True, float("nan"), float("inf"), -1, 1e300])
def test_invalid_observation_timestamp_uses_explicit_parent_return_fallback(
        tmp_path, fake_capture, monkeypatch, observed):
    fake_capture.frames = [Image.new("RGB", (2, 2), "white")]
    original = fake_capture.capture

    def capture(self, token=None):
        frame = original(self, token)
        self.last_observed_at = observed
        return frame

    monkeypatch.setattr(fake_capture, "capture", capture)
    before = time.monotonic()
    report = probe.collect_capture(123, "printwindow", tmp_path, samples=1)
    after = time.monotonic()
    item = report["frames"][0]
    assert before <= item["captured_monotonic"] <= after
    assert item["captured_monotonic_basis"] == "parent_return_fallback"
    assert item["native_capture_ms"] is None
    assert item["captured_utc_basis"] == "parent_return"
    assert report["gates"]["freshness"]["status"] == "unknown"


def test_black_frames_are_transport_success_but_not_game_success(tmp_path, fake_capture):
    fake_capture.frames = [Image.new("RGB", (2, 2))]
    report = probe.collect_capture(123, "printwindow", tmp_path, samples=1)
    assert report["gates"]["transport"]["status"] == "pass"
    assert report["gates"]["nonblack"]["status"] == "fail"
    assert report["gates"]["game_content"]["status"] == "unknown"


def test_fatal_capture_failure_stops_sampling_and_closes_worker(tmp_path, fake_capture):
    fake_capture.frames = [Image.new("RGB", (2, 2), "red"), CaptureError("blocked"),
                           Image.new("RGB", (2, 2), "green")]
    report = probe.collect_capture(123, "printwindow", tmp_path, samples=3)
    instance = fake_capture.instances[0]
    assert instance.calls == 2 and instance.closed
    assert report["summary"]["received_samples"] == 1
    assert len(report["frames"]) == 2
    assert "blocked" in report["frames"][1]["error"]
    assert report["gates"]["transport"]["status"] == "fail"
    assert not (tmp_path / "printwindow-003.png").exists()


def test_first_frame_failure_has_no_successful_latency_or_nonblack_claim(tmp_path, fake_capture):
    fake_capture.frames = [CaptureError("unavailable")]
    report = probe.collect_capture(123, "printwindow", tmp_path, samples=5)
    assert fake_capture.instances[0].calls == 1
    assert report["summary"]["received_samples"] == 0
    assert report["summary"]["native_capture_ms"]["count"] == 0
    assert report["gates"]["nonblack"]["status"] == "unknown"
    assert report["gates"]["transport"]["status"] == "fail"


def test_capture_failure_does_not_report_expected_size_as_observed(tmp_path, fake_capture):
    fake_capture.frames = [CaptureError("host window obscured")]
    report = probe.collect_capture(123, "visible_screen", tmp_path, samples=5, expected_size=(4, 3))
    assert report["expected_size"] == [4, 3]
    assert report["summary"]["observed_size"] is None
    assert report["gates"]["native_geometry"]["status"] == "unknown"
    assert report["gates"]["transport"]["status"] == "fail"


def test_actual_returned_mismatch_is_reported_as_observed_size(tmp_path, fake_capture):
    fake_capture.frames = [Image.new("RGB", (3, 2), "red")]
    report = probe.collect_capture(123, "printwindow", tmp_path, samples=1, expected_size=(4, 3))
    assert report["expected_size"] == [4, 3]
    assert report["summary"]["observed_size"] == [3, 2]
    assert report["gates"]["native_geometry"]["status"] == "fail"
    assert report["gates"]["transport"]["status"] == "fail"


def test_observed_geometry_can_match_even_when_later_capture_fails(tmp_path, fake_capture):
    fake_capture.frames = [Image.new("RGB", (4, 3), "red"), CaptureError("host window obscured")]
    report = probe.collect_capture(123, "printwindow_physical", tmp_path, samples=2,
                                   expected_size=(4, 3))
    assert report["summary"]["observed_size"] == [4, 3]
    assert report["gates"]["native_geometry"]["status"] == "pass"
    assert report["gates"]["transport"]["status"] == "fail"


def test_dimension_change_is_fatal_without_silent_resizing(tmp_path, fake_capture):
    fake_capture.frames = [Image.new("RGB", (2, 2)), Image.new("RGB", (3, 2)),
                           Image.new("RGB", (2, 2))]
    report = probe.collect_capture(123, "printwindow", tmp_path, samples=3)
    assert fake_capture.instances[0].calls == 2
    assert report["frames"][1]["size"] == [3, 2]
    assert report["frames"][1]["size_matches"] is False
    assert report["gates"]["transport"]["status"] == "fail"
    with Image.open(report["frames"][1]["path"]) as saved:
        assert saved.size == (3, 2)


def test_existing_evidence_is_never_overwritten(tmp_path, fake_capture):
    original = tmp_path / "printwindow-001.png"
    original.write_bytes(b"earlier evidence")
    fake_capture.frames = [Image.new("RGB", (2, 2), "red")]
    report = probe.collect_capture(123, "printwindow", tmp_path, samples=1)
    assert original.read_bytes() == b"earlier evidence"
    assert report["gates"]["transport"]["status"] == "fail"
    assert "FileExistsError" in report["frames"][0]["error"]


def test_close_failure_is_reported(tmp_path, fake_capture):
    fake_capture.frames = [Image.new("RGB", (2, 2), "red")]
    fake_capture.close_error = RuntimeError("worker still running")
    report = probe.collect_capture(123, "printwindow", tmp_path, samples=1)
    assert report["gates"]["transport"]["status"] == "fail"
    assert "close:" in report["errors"][0]


def test_token_cancellation_stops_and_closes(tmp_path, fake_capture):
    class Cancelled:
        def checkpoint(self):
            raise RuntimeError("cancelled")

    report = probe.collect_capture(123, "printwindow", tmp_path, token=Cancelled())
    assert fake_capture.instances[0].closed
    assert "cancelled" in report["frames"][0]["error"]


def test_compare_saved_images_and_roi_do_not_claim_freshness(tmp_path):
    first = Image.new("RGB", (4, 2))
    second = first.copy()
    second.putpixel((3, 1), (255, 0, 0))
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    first.save(a)
    second.save(b)
    result = probe.compare_frames(a, b)
    assert result["same_size"] is True
    assert result["changed_ratio"] == 1 / 8
    assert result["mean_absdiff"] == 255 / 24
    assert result["freshness_confirmed"] is False
    unchanged = probe.compare_frames(a, b, (0, 0, 2, 2))
    assert unchanged["changed_ratio"] == 0
    assert unchanged["mean_absdiff"] == 0


@pytest.mark.parametrize("roi", [(0, 0, 0, 2), (-1, 0, 1, 2), (0, 0, 9, 9), (0.5, 0, 1, 1),
                                  (False, 0, 1, 1), "0,0,1,1"])
def test_compare_rejects_invalid_roi(tmp_path, roi):
    path = tmp_path / "image.png"
    Image.new("RGB", (2, 2)).save(path)
    with pytest.raises(ValueError, match="roi"):
        probe.compare_frames(path, path, roi)


def test_compare_different_dimensions_reports_no_difference_metric(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (2, 2)).save(a)
    Image.new("RGB", (3, 2)).save(b)
    report = probe.compare_frames(a, b)
    assert report["same_size"] is False
    assert report["changed_ratio"] is None and report["mean_absdiff"] is None
    assert report["freshness_confirmed"] is False


def test_inventory_only_reports_selected_native_metadata_and_continues_geometry_error(monkeypatch):
    from conf import WindowTitles

    monkeypatch.setattr(WindowTitles, "get_titles", lambda: {123: "Sandbox", 456: "Tibia"})

    def inspect(hwnd):
        if hwnd == 456:
            raise RuntimeError("closed during enumeration")
        return {"client_size": [800, 600], "foreground_root_matches": True}

    monkeypatch.setattr(probe, "inspect_window", inspect)
    report = probe.inventory()
    assert report[0] == {"hwnd": 123, "title": "Sandbox", "physical_pixels": {
        "client_size": [800, 600], "foreground_root_matches": True}}
    assert "closed during enumeration" in report[1]["physical_pixels"]["error"]


def test_write_report_is_explicit_json_only(tmp_path):
    path = tmp_path / "probe" / "report.json"
    report = {"gates": {"game_content": {"status": "unknown"}}}
    assert probe.write_report(path, report) == path.resolve()
    assert json.loads(path.read_text(encoding="utf-8")) == report
    assert list(path.parent.iterdir()) == [path]


def test_write_report_rejects_nonfinite_values_before_touching_path(tmp_path):
    path = tmp_path / "report.json"
    with pytest.raises(ValueError):
        probe.write_report(path, {"latency": float("nan")})
    assert not path.exists()


@pytest.fixture
def known_native(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from tibia_client import window_target

    class Native:
        def __init__(self):
            self.calls = []
            self.valid = True
            self.valid_after = True
            self.valid_checks = 0
            self.root = 123
            self.visible = True
            self.minimized = False
            self.title = "Paulistinha Ot - Paladjinn"
            self.class_name = "TibiaClient"
            self.size = (800, 600)
            self.foreground = 999
            self.api = SimpleNamespace(
                IsWindow=self.is_window,
                GetAncestor=lambda hwnd, flag: self.root if hwnd == 123 else hwnd,
                GetForegroundWindow=lambda: self.foreground,
                IsWindowVisible=lambda hwnd: self.visible,
                IsIconic=lambda hwnd: self.minimized,
                GetWindowTextW=lambda hwnd, buffer, capacity: self.text(buffer, capacity, self.title),
                GetClassNameW=lambda hwnd, buffer, capacity: self.text(buffer, capacity, self.class_name))
            self.ctypes = SimpleNamespace(create_unicode_buffer=lambda capacity: SimpleNamespace(value=""))

        def is_window(self, hwnd):
            self.valid_checks += 1
            return self.valid if self.valid_checks == 1 else self.valid_after

        def text(self, buffer, capacity, value):
            self.calls.append(("native_text", capacity))
            buffer.value = value[:capacity - 1]
            return len(buffer.value)

        def client_size(self, hwnd):
            self.calls.append(("client_size", hwnd))
            return self.size

        @contextmanager
        def physical_pixels(self):
            self.calls.append("physical_pixels_enter")
            try:
                yield
            finally:
                self.calls.append("physical_pixels_restore")

    native = Native()
    monkeypatch.setattr(window_target, "_native_backend", lambda: native)
    return native


def test_known_handle_absent_from_inventory_is_separate_fresh_verified_selection(known_native):
    original_inventory = []
    report = probe.verify_known_handle(123, inventory_items=original_inventory)
    assert report["status"] == "verified"
    assert report["requested_hwnd"] == report["selected_hwnd"] == 123
    assert report["inventory_presence"] == "absent"
    assert report["inventory_count"] == 0 and original_inventory == []
    assert report["input_armed"] is False and report["capture_started"] is False
    selected = report["selection"]
    assert selected["selection_method"] == "explicit_verified_hwnd"
    assert selected["title"] == "Paulistinha Ot - Paladjinn"
    assert selected["physical_pixels"]["client_size"] == [800, 600]
    # Selecting a window is allowed while the probe GUI is foreground. Native
    # capture/input still independently require target focus at execution time.
    assert selected["physical_pixels"]["foreground_root_matches"] is False
    assert selected["handle_diagnostic"]["inventory_presence"] == "absent"
    assert "selection" not in selected["handle_diagnostic"]
    assert json.loads(json.dumps(report)) == report
    assert known_native.calls[0] == "physical_pixels_enter"
    assert known_native.calls[-1] == "physical_pixels_restore"
    assert report["inspection"]["inspected_monotonic"] > 0


def test_known_handle_current_inventory_presence_is_not_rewritten_as_absence(known_native):
    original = [{"hwnd": 123, "title": "older caption"}]
    report = probe.verify_known_handle(123, inventory_items=original)
    assert report["status"] == "verified" and report["inventory_presence"] == "present"
    assert original == [{"hwnd": 123, "title": "older caption"}]
    assert report["selection"]["title"] == known_native.title


def test_known_handle_no_inventory_snapshot_is_explicit_unknown(known_native):
    report = probe.verify_known_handle(123)
    assert report["status"] == "verified"
    assert report["inventory_presence"] == "unknown" and report["inventory_count"] is None


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "123", None, 2**64])
def test_known_handle_invalid_numeric_request_never_inspects_native(monkeypatch, value):
    def forbidden(hwnd):
        raise AssertionError("invalid request must never inspect a native handle")

    monkeypatch.setattr(probe, "_inspect_known_handle", forbidden)
    report = probe.verify_known_handle(value, inventory_items=[])
    assert report["status"] == "rejected" and report["selection"] is None
    assert report["selected_hwnd"] is None and report["input_armed"] is False


def test_known_child_handle_is_rejected_without_substituting_its_root(known_native):
    known_native.root = 456
    report = probe.verify_known_handle(123, inventory_items=[])
    assert report["inspection"]["root_hwnd"] == 456
    assert report["selection"] is None and report["selected_hwnd"] is None
    assert any("no root was substituted" in reason for reason in report["reasons"])


@pytest.mark.parametrize("field,value,reason", [
    ("visible", False, "IsWindowVisible"),
    ("minimized", True, "minimized"),
    ("title", "", "title"),
    ("class_name", "", "class"),
    ("size", (0, 600), "client size"),
    ("valid_after", False, "changed during"),
])
def test_known_unready_or_changed_handle_remains_unselected(known_native, field, value, reason):
    setattr(known_native, field, value)
    report = probe.verify_known_handle(123, inventory_items=[])
    assert report["status"] == "rejected"
    assert report["selection"] is None and report["selected_hwnd"] is None
    assert any(reason in explanation for explanation in report["reasons"])
    assert report["inspection"]["root_hwnd"] == 123
    assert report["input_armed"] is False


def test_known_invalid_native_window_has_clear_rejection_and_no_followup_queries(known_native):
    known_native.valid = False
    report = probe.verify_known_handle(123, inventory_items=[])
    assert report["status"] == "rejected" and report["selection"] is None
    assert report["inspection"]["is_window"] is False
    assert "IsWindow returned false" in report["reasons"][0]
    assert known_native.calls == ["physical_pixels_enter", "physical_pixels_restore"]


def test_known_handle_inspection_failure_is_a_diagnostic_not_a_selection(monkeypatch):
    def unavailable(hwnd):
        raise OSError("read-only native inspection unavailable")

    monkeypatch.setattr(probe, "_inspect_known_handle", unavailable)
    report = probe.verify_known_handle(123, inventory_items=[])
    assert report["status"] == "rejected" and report["selection"] is None
    assert "inspection unavailable" in report["reasons"][0]
