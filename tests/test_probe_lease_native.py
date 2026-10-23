"""Cheap phase-check tests with ordinary Python substitutes for native APIs."""

from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest

from tibia_client import probe_lease_native as lease_native
from tibia_client.window_target import WindowTargetError


@pytest.fixture
def native(monkeypatch):
    class Native:
        def __init__(self):
            self.valid, self.visible, self.minimized = True, True, False
            self.root, self.foreground, self.hit = 44, 44, 44
            self.size, self.origin, self.desktop = (160, 120), (10, 20), (0, 0, 1920, 1080)
            self.hit_calls = []
            self.contexts = []
            self.origin_available = True
            self.api = SimpleNamespace(
                IsWindow=lambda hwnd: self.valid, IsWindowVisible=lambda hwnd: self.visible,
                IsIconic=lambda hwnd: self.minimized,
                GetAncestor=lambda hwnd, flag: self.root if hwnd == 44 else hwnd,
                GetForegroundWindow=lambda: self.foreground, ClientToScreen=self.client_to_screen,
                GetSystemMetrics=lambda number: dict(zip((76, 77, 78, 79), self.desktop))[number],
                WindowFromPoint=self.point_hit, EnumWindows=self.forbidden,
                SetForegroundWindow=self.forbidden)
            self.types = SimpleNamespace(POINT=lambda x, y: SimpleNamespace(x=x, y=y))
            self.ctypes = SimpleNamespace(byref=lambda value: value)

        @contextmanager
        def physical_pixels(self):
            self.contexts.append("enter")
            try:
                yield
            finally:
                self.contexts.append("restore")

        def forbidden(self, *arguments):
            raise AssertionError("cheap check must not grab, enumerate, or change focus")

        grab = forbidden

        def client_size(self, hwnd):
            return self.size

        def client_to_screen(self, hwnd, point):
            point.x, point.y = self.origin
            return self.origin_available

        def point_hit(self, point):
            self.hit_calls.append((point.x, point.y))
            return self.hit

    result = Native()
    monkeypatch.setattr(lease_native, "_native_backend", lambda: result)
    return result


def test_bind_and_phase_checks_have_only_bounded_partial_native_evidence(native):
    checker = lease_native.bind(44, (160, 120), point=(70, 80), regions=[(10, 20, 20, 30)])
    assert checker.baseline["screen_rect"] == [10, 20, 170, 140]
    assert checker.baseline["partial_point_hits_only"] is True
    assert checker.baseline["full_occlusion_scan"] is False
    assert checker.baseline["guest_focus_pixels_rechecked"] is False
    assert [70, 80] in checker.baseline["local_sample_points"]
    assert len(checker.baseline["local_sample_points"]) == 6
    assert checker.recheck() is True and checker() is True
    assert checker.telemetry[-1]["sequence"] == 2
    assert checker.telemetry[-1]["valid"] is True
    assert json.loads(json.dumps(checker.telemetry)) == checker.telemetry
    assert native.contexts == ["enter", "restore"] * 3


def test_default_sampling_is_five_client_points_not_full_viewport_grid(native):
    checker = lease_native.bind(44, (160, 120))
    assert checker.baseline["local_sample_points"] == [[0, 0], [0, 119], [79, 59], [159, 0], [159, 119]]
    assert len(native.hit_calls) == 5
    assert len(lease_native.bind(44, (160, 120), regions=()).points) == 5


@pytest.mark.parametrize("field,value,reason", [
    ("valid", False, "invalid"), ("visible", False, "hidden"), ("minimized", True, "minimized"),
    ("root", 99, "root/focus"), ("foreground", 99, "root/focus"),
    ("size", (161, 120), "size changed"), ("size", (0, 120), "size is invalid"),
    ("origin", (11, 20), "geometry/desktop changed"),
    ("origin", (-1, 20), "outside"), ("desktop", (0, 0, 1600, 900), "geometry/desktop changed"),
    ("hit", 99, "obscured"), ("origin_available", False, "origin"),
])
def test_phase_changes_fail_closed_and_are_recorded(native, field, value, reason):
    checker = lease_native.bind(44, (160, 120))
    setattr(native, field, value)
    with pytest.raises(WindowTargetError, match=reason):
        checker.recheck()
    assert checker.telemetry[-1]["valid"] is False
    assert checker.telemetry[-1]["error"]
    assert native.contexts[-1] == "restore"


@pytest.mark.parametrize("hwnd,size,point,regions", [
    (0, (160, 120), None, None), (True, (160, 120), None, None),
    (44, (0, 120), None, None), (44, (160, 120), (-1, 5), None),
    (44, (160, 120), (True, 5), None), (44, (160, 120), None, [(0, 0, 200, 5)]),
    (44, (160, 120), None, [(0, 0, 2, 2)] * 17),
    (44, (160, 120), None, [(5 * i, 0, 5 * i + 3, 3) for i in range(16)]),
])
def test_invalid_or_excessive_bind_request_precedes_native_construction(monkeypatch, hwnd, size, point, regions):
    def forbidden():
        raise AssertionError("invalid request must not construct native adapter")

    monkeypatch.setattr(lease_native, "_native_backend", forbidden)
    with pytest.raises(ValueError):
        lease_native.bind(hwnd, size, point=point, regions=regions)


def test_telemetry_is_bounded_without_changing_original_baseline(native):
    checker = lease_native.bind(44, (160, 120))
    baseline = dict(checker.baseline)
    for _ in range(140):
        checker.recheck()
    assert len(checker.telemetry) == lease_native.MAX_TELEMETRY
    assert checker.telemetry[-1]["sequence"] == 140
    assert checker.baseline == baseline
