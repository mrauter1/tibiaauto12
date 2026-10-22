"""Sandbox target checks use synthetic pixels and Windows API substitutes only."""

from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from PIL import Image

from tibia_client import window_target as wt


HOST_SIZE = (160, 120)
VIEWPORT = (10, 30, 150, 110)
GUARD_REGION = (15, 5, 95, 25)


def samples():
    active = Image.new("RGB", HOST_SIZE, (20, 60, 160))
    inactive = Image.new("RGB", HOST_SIZE, (120, 120, 120))
    for image in (active, inactive):
        image.putpixel((20, 10), (240, 240, 240))
    return active, inactive


def target():
    active, inactive = samples()
    return wt.WindowTarget.build(HOST_SIZE, VIEWPORT,
                                wt.make_focus_guard(active, inactive, GUARD_REGION))


def test_round_trip_is_immutable_and_fingerprints_include_all_calibration():
    configured = target()
    data = configured.to_dict()
    copied = wt.WindowTarget.from_config({"window_target": data})
    assert configured == copied
    assert configured.size == (140, 80)
    assert copied.fingerprint == configured.fingerprint
    data["viewport"][0] = 11
    changed = wt.WindowTarget.from_config({"window_target": data})
    assert changed.fingerprint != configured.fingerprint
    configured.focus_guard["region"][0] = 0
    assert configured.focus_guard["region"] == list(GUARD_REGION)
    with pytest.raises(FrozenInstanceError):
        configured.kind = "local"


def test_missing_target_preserves_local_backend_semantics():
    configured = wt.target_from_config({})
    assert configured.kind == "local"
    assert configured.size is None
    assert configured.to_dict() == {"kind": "local"}


@pytest.mark.parametrize("change", [
    {"kind": "vm"}, {"host_size": [True, 120]}, {"host_size": [0, 120]},
    {"host_size": [32768, 120]}, {"host_size": [16384, 16384]},
    {"viewport": [-1, 0, 20, 20]}, {"viewport": [0, 0, 161, 20]},
    {"viewport": [0, 5, 20, 5]}, {"viewport": [0, 0, 20.0, 20]},
])
def test_invalid_or_missing_sandbox_geometry_and_guard_are_rejected(change):
    data = target().to_dict()
    data.update(change)
    with pytest.raises(ValueError):
        wt.WindowTarget.from_config({"window_target": data})


def test_guard_rejects_identical_uniform_and_mismatched_samples():
    active, inactive = samples()
    with pytest.raises(ValueError, match="distinguishable"):
        wt.make_focus_guard(active, active.copy(), GUARD_REGION)
    with pytest.raises(ValueError, match="nonuniform"):
        wt.make_focus_guard(Image.new("RGB", HOST_SIZE, "blue"),
                            Image.new("RGB", HOST_SIZE, "red"), GUARD_REGION)
    with pytest.raises(ValueError, match="equally sized"):
        wt.make_focus_guard(active, inactive.resize((80, 60)), GUARD_REGION)
    data = target().to_dict()
    data["focus_guard"]["active"] = "not a png"
    with pytest.raises(ValueError, match="PNG"):
        wt.WindowTarget.from_config({"window_target": data})
    data = target().to_dict()
    data["focus_guard"]["region"][2] += 1
    with pytest.raises(ValueError, match="dimensions"):
        wt.WindowTarget.from_config({"window_target": data})


def test_guard_accepts_active_and_small_noise_but_rejects_inactive_and_unknown():
    configured = target()
    active, inactive = samples()
    wt.check_focus_guard(active, configured)
    noisy = active.copy()
    noisy.putpixel((21, 10), (21, 61, 161))
    wt.check_focus_guard(noisy, configured)
    for image in (inactive, Image.new("RGB", HOST_SIZE, (80, 40, 70))):
        with pytest.raises(wt.WindowTargetError, match="inactive or changed"):
            wt.check_focus_guard(image, configured)
    altered = active.copy()
    altered.putpixel((21, 10), (255, 0, 0))
    with pytest.raises(wt.WindowTargetError, match="inactive or changed"):
        wt.check_focus_guard(altered, configured)


class FakeScreen:
    def __init__(self, image=None):
        self.image = image if image is not None else samples()[0]
        self.rects = [(10, 20, 170, 140), (10, 20, 170, 140)]
        self.snapshots = []
        self.dpi_depth = 0
        self.fail = None
        self.size = HOST_SIZE

    @contextmanager
    def physical_pixels(self):
        self.dpi_depth += 1
        try:
            yield
        finally:
            self.dpi_depth -= 1

    def client_size(self, hwnd):
        assert self.dpi_depth > 0
        return self.size

    def snapshot(self, hwnd, expected, regions):
        assert self.dpi_depth > 0
        if self.fail:
            raise wt.WindowTargetError(self.fail)
        self.snapshots.append((hwnd, expected, regions))
        return 44, self.rects[min(len(self.snapshots) - 1, len(self.rects) - 1)]

    def grab(self, rect):
        assert self.dpi_depth > 0
        return self.image.copy()


def test_capture_checks_before_and_after_and_crops_exact_game_pixels(monkeypatch):
    screen = FakeScreen()
    monkeypatch.setattr(wt, "_native_backend", lambda: screen)
    result = wt.capture_target(44, target())
    assert result.size == (140, 80)
    assert result.tobytes() == screen.image.crop(VIEWPORT).tobytes()
    assert len(screen.snapshots) == 2
    assert screen.snapshots[0][2] == [VIEWPORT, GUARD_REGION]
    assert screen.dpi_depth == 0


def test_capture_fails_on_geometry_change_or_wrong_pixel_dimensions(monkeypatch):
    screen = FakeScreen()
    monkeypatch.setattr(wt, "_native_backend", lambda: screen)
    screen.rects[1] = (11, 20, 171, 140)
    with pytest.raises(wt.WindowTargetError, match="geometry changed"):
        wt.capture_target(44, target())
    screen.rects[1] = screen.rects[0]
    screen.image = screen.image.resize((80, 60))
    with pytest.raises(wt.WindowTargetError, match="inconsistent physical pixels"):
        wt.capture_host_client(44)


def test_local_capture_uses_existing_native_capture(monkeypatch):
    from core import perception
    expected = Image.new("RGB", (2, 3), "green")
    monkeypatch.setattr(perception, "_capture_windows", lambda hwnd: expected if hwnd == 44 else None)
    assert wt.capture_target(44, wt.WindowTarget()) is expected


class RecordingInput:
    def __init__(self, screen):
        self.screen, self.events = screen, []

    def MainWindowSize(self):
        return HOST_SIZE

    def IsFocused(self):
        return True

    def Position(self):
        return 23, 47

    def ScreenPosition(self):
        return -200, 55

    def __getattr__(self, name):
        def call(*args):
            assert self.screen.dpi_depth > 0
            self.events.append((name, *args))
        return call


def test_every_physical_dispatch_uses_guard_and_viewport_translation(monkeypatch):
    screen = FakeScreen()
    backend = RecordingInput(screen)
    monkeypatch.setattr(wt, "_native_backend", lambda: screen)
    adapter = wt.TargetInput(backend, target(), lambda: 44, preflight=True)
    assert adapter.MainWindowSize() == (140, 80)
    assert screen.snapshots == []  # Configuration can query size without capture.
    assert adapter.Position() == (13, 17)
    assert adapter.ScreenPosition() == (-200, 55)
    adapter.Press("F1")
    adapter.PressHotkey("ctrl", "O")
    adapter.LeftClick((0, 0))
    adapter.RightClick((139, 79))
    adapter.MoveTo(4, 5)
    adapter.DragTo((1, 2), (3, 4))
    adapter.UseOn((5, 6), (7, 8))
    adapter.RestoreScreenPosition((-200, 55))
    assert backend.events == [
        ("Press", "F1"), ("PressHotkey", "ctrl", "O"),
        ("LeftClick", (10, 30)), ("RightClick", (149, 109)),
        ("MoveTo", 14, 35), ("DragTo", (11, 32), (13, 34)),
        ("UseOn", (15, 36), (17, 38)), ("RestoreScreenPosition", (-200, 55)),
    ]
    assert len(screen.snapshots) == 20
    assert screen.dpi_depth == 0


@pytest.mark.parametrize("method,args", [
    ("Press", ("F1",)), ("PressHotkey", ("ctrl", "O")),
    ("LeftClick", ((1, 2),)), ("RightClick", ((1, 2),)),
    ("MoveTo", (1, 2)), ("DragTo", ((1, 2), (3, 4))),
    ("UseOn", ((1, 2), (3, 4))), ("RestoreScreenPosition", ((500, 600),)),
])
def test_inactive_guest_never_dispatches_any_input(monkeypatch, method, args):
    screen = FakeScreen(samples()[1])
    backend = RecordingInput(screen)
    monkeypatch.setattr(wt, "_native_backend", lambda: screen)
    adapter = wt.TargetInput(backend, target(), lambda: 44, preflight=True)
    with pytest.raises(wt.WindowTargetError):
        getattr(adapter, method)(*args)
    assert backend.events == []
    assert not adapter.IsFocused()


def test_invalid_positions_and_resized_host_never_dispatch(monkeypatch):
    screen = FakeScreen()
    backend = RecordingInput(screen)
    monkeypatch.setattr(wt, "_native_backend", lambda: screen)
    adapter = wt.TargetInput(backend, target(), lambda: 44, preflight=True)
    for point in ((-1, 0), (140, 0), (0, 80), (True, 3)):
        with pytest.raises(ValueError):
            adapter.LeftClick(point)
    screen.size = (161, 120)
    with pytest.raises(wt.WindowTargetError, match="size changed"):
        adapter.MainWindowSize()
    assert backend.events == []


@pytest.mark.parametrize("failure", ["viewport obscured", "window minimized", "focus changed"])
def test_native_readiness_failure_never_dispatches(monkeypatch, failure):
    screen = FakeScreen()
    screen.fail = failure
    backend = RecordingInput(screen)
    monkeypatch.setattr(wt, "_native_backend", lambda: screen)
    adapter = wt.TargetInput(backend, target(), lambda: 44, preflight=True)
    with pytest.raises(wt.WindowTargetError, match=failure):
        adapter.Press("F1")
    assert backend.events == []
    assert not adapter.IsFocused()
    assert screen.dpi_depth == 0


class FakeWindows:
    """A native API substitute, including Z-order and hit testing."""
    def __init__(self):
        self.foreground = 44
        self.minimized = False
        self.origin = (10, 20)
        self.z_order = [44, 88]
        self.overlay = (0, 0, 0, 0)
        self.hit = 44
        self.window_rects = {}
        self.metadata_calls = []
        self.titles = {}

    def IsWindow(self, hwnd):
        return hwnd == 44

    def IsWindowVisible(self, _hwnd):
        return True

    def IsIconic(self, hwnd):
        return self.minimized if hwnd == 44 else False

    def GetAncestor(self, hwnd, _flag):
        return hwnd

    def GetForegroundWindow(self):
        return self.foreground

    def GetClientRect(self, _hwnd, rect):
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, *HOST_SIZE
        return True

    def ClientToScreen(self, _hwnd, point):
        point.x, point.y = self.origin
        return True

    def GetSystemMetrics(self, number):
        return {76: 0, 77: 0, 78: 1920, 79: 1080}[number]

    def EnumWindows(self, callback, _parameter):
        for hwnd in self.z_order:
            callback(hwnd, 0)
        return True

    def GetWindowRect(self, hwnd, rect):
        rect.left, rect.top, rect.right, rect.bottom = self.window_rects.get(hwnd, self.overlay)
        return True

    def GetClassNameW(self, hwnd, buffer, capacity):
        self.metadata_calls.append(("class", hwnd))
        buffer.value = f"Class{hwnd}"[:capacity - 1]
        return len(buffer.value)

    def GetWindowTextW(self, hwnd, buffer, capacity):
        self.metadata_calls.append(("title", hwnd))
        buffer.value = self.titles.get(hwnd, f"Window {hwnd}")[:capacity - 1]
        return len(buffer.value)

    def WindowFromPoint(self, _point):
        return self.hit


def native_fake():
    native = object.__new__(wt._WindowsScreenBackend)
    native.api = FakeWindows()
    native.ctypes = SimpleNamespace(byref=lambda value: value,
                                    create_unicode_buffer=lambda _size: SimpleNamespace(value=""))
    native.types = SimpleNamespace(
        RECT=lambda: SimpleNamespace(), POINT=lambda x, y: SimpleNamespace(x=x, y=y))
    native.enum_callback = lambda callback: callback
    return native


def test_native_visibility_checks_reject_foreign_focus_minimize_and_offscreen():
    native = native_fake()
    assert native.snapshot(44, HOST_SIZE, [VIEWPORT]) == (44, (10, 20, 170, 140))
    native.api.foreground = 88
    with pytest.raises(wt.WindowTargetError, match="foreground"):
        native.snapshot(44, HOST_SIZE, [VIEWPORT])
    native.api.foreground = 44
    native.api.minimized = True
    with pytest.raises(wt.WindowTargetError, match="minimized"):
        native.snapshot(44, HOST_SIZE, [VIEWPORT])
    native.api.minimized = False
    native.api.origin = (-1, 20)
    with pytest.raises(wt.WindowTargetError, match="outside"):
        native.snapshot(44, HOST_SIZE, [VIEWPORT])


@pytest.mark.parametrize("foreground", [88, 99])
def test_foreground_rejection_identifies_requested_and_actual_roots(foreground):
    native = native_fake()
    native.api.foreground = foreground
    native.api.GetAncestor = lambda hwnd, _flag: 88 if hwnd == 99 else hwnd
    native.api.window_rects[88] = (200, 300, 600, 800)
    native.api.titles[88] = "Unexpected foreground application"
    with pytest.raises(wt.WindowTargetError, match="must be foreground") as caught:
        native.snapshot(44, HOST_SIZE, [VIEWPORT])
    error = str(caught.value)
    assert "requested_hwnd=44, requested_root=44" in error
    assert f"foreground_hwnd={foreground}" in error
    assert "foreground_root=(hwnd=88, rect=(200, 300, 600, 800)" in error
    assert "class='Class88'" in error
    assert "title='Unexpected foreground application'" in error
    assert native.api.metadata_calls == [("class", 88), ("title", 88)]


def test_native_occlusion_detects_thin_overlay_between_grid_points_and_wrong_root():
    native = native_fake()
    native.api.z_order = [88, 44]
    native.api.overlay = (25, 55, 26, 70)  # A thin cover missed by a 32px grid.
    with pytest.raises(wt.WindowTargetError, match="obscures"):
        native.snapshot(44, HOST_SIZE, [VIEWPORT])
    native.api.overlay = (0, 0, 1, 1)
    native.api.hit = 88
    with pytest.raises(wt.WindowTargetError, match="obscured"):
        native.snapshot(44, HOST_SIZE, [VIEWPORT])


def test_occlusion_diagnostics_identify_only_the_intersecting_window():
    native = native_fake()
    native.api.z_order = [77, 88, 44]
    native.api.window_rects = {77: (0, 0, 1, 1), 88: (25, 55, 26, 70)}
    native.api.titles[77] = "Unrelated private window"
    native.api.titles[88] = "Thin overlay"
    with pytest.raises(wt.WindowTargetError, match="obscures") as caught:
        native.snapshot(44, HOST_SIZE, [VIEWPORT])
    error = str(caught.value)
    assert "hwnd=88" in error
    assert "rect=(25, 55, 26, 70)" in error
    assert "class='Class88'" in error
    assert "title='Thin overlay'" in error
    assert "checked_region=(20, 50, 160, 130)" in error
    assert "Unrelated private window" not in error
    assert native.api.metadata_calls == [("class", 88), ("title", 88)]


def test_point_obstruction_diagnostics_include_sample_and_hit_root():
    native = native_fake()
    native.api.hit = 99
    native.api.GetAncestor = lambda hwnd, _flag: 88 if hwnd == 99 else hwnd
    native.api.window_rects[88] = (15, 40, 200, 150)
    with pytest.raises(wt.WindowTargetError, match="obscured") as caught:
        native.snapshot(44, HOST_SIZE, [VIEWPORT])
    error = str(caught.value)
    assert "point=(20, 50)" in error
    assert "hit_hwnd=99" in error
    assert "hit_root=(hwnd=88, rect=(15, 40, 200, 150)" in error
    assert "class='Class88'" in error
    assert "title='Window 88'" in error
    assert native.api.metadata_calls == [("class", 88), ("title", 88)]


def test_obstruction_metadata_is_bounded_and_failure_does_not_allow_capture():
    native = native_fake()
    native.api.z_order = [88, 44]
    native.api.overlay = (25, 55, 26, 70)
    native.api.titles[88] = "x" * 200
    with pytest.raises(wt.WindowTargetError, match="obscures") as caught:
        native.snapshot(44, HOST_SIZE, [VIEWPORT])
    assert f"title={'x' * 96!r}" in str(caught.value)
    assert "x" * 97 not in str(caught.value)

    def unavailable(*_args):
        raise OSError("diagnostic read failed")

    native.api.GetWindowTextW = unavailable
    with pytest.raises(wt.WindowTargetError, match="obscures") as caught:
        native.snapshot(44, HOST_SIZE, [VIEWPORT])
    assert "title='<unavailable>'" in str(caught.value)


def test_unobscured_snapshot_never_queries_window_titles():
    native = native_fake()
    assert native.snapshot(44, HOST_SIZE, [VIEWPORT]) == (44, (10, 20, 170, 140))
    assert native.api.metadata_calls == []


@pytest.mark.parametrize("result,cloaked,allowed", [
    (0, 1, True), (0, 0, False), (-1, 1, False),
])
def test_only_successfully_confirmed_dwm_cloaked_windows_are_skipped(result, cloaked, allowed):
    native = native_fake()
    native.api.z_order = [88, 44]
    native.api.overlay = (25, 55, 26, 70)
    native.types.DWORD = lambda: SimpleNamespace(value=0)
    native.ctypes.sizeof = lambda _value: 4

    def query(hwnd, attribute, output, size):
        assert (hwnd, attribute, size) == (88, 14, 4)
        output.value = cloaked
        return result

    native._cloaked = query
    if allowed:
        assert native.snapshot(44, HOST_SIZE, [VIEWPORT])[0] == 44
    else:
        with pytest.raises(wt.WindowTargetError, match="obscures"):
            native.snapshot(44, HOST_SIZE, [VIEWPORT])


def test_sample_grid_covers_edges_and_interior_of_large_viewport():
    points = set(wt._sample_points((10, 20, 160, 120)))
    assert {(10, 20), (159, 20), (10, 119), (159, 119), (74, 84)} <= points
