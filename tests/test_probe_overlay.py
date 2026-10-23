"""Pure substitutes verify the experimental exception without desktop calls."""

from contextlib import contextmanager
from types import SimpleNamespace

from PIL import Image
import pytest

from tibia_client import probe_overlay as overlay
from tibia_client.window_target import WindowTargetError


FLAGS = overlay.WS_EX_TRANSPARENT | overlay.WS_EX_NOACTIVATE


class Buffer:
    value = ""

    def __len__(self):
        return 256


class FakeAPI:
    def __init__(self):
        self.foreground = 44
        self.hit = 44
        self.style = FLAGS
        self.class_name = overlay.ANNOTATION_CLASS
        self.title = overlay.ANNOTATION_TITLE
        self.z_order = [88, 44]
        self.point_checks = 0

    def IsWindow(self, hwnd):
        return hwnd == 44

    def IsWindowVisible(self, hwnd):
        return True

    def IsIconic(self, hwnd):
        return False

    def GetAncestor(self, hwnd, flag):
        return hwnd

    def GetForegroundWindow(self):
        return self.foreground

    def GetClientRect(self, hwnd, rect):
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, 160, 120
        return True

    def ClientToScreen(self, hwnd, point):
        point.x, point.y = 10, 20
        return True

    def GetSystemMetrics(self, number):
        return {76: 0, 77: 0, 78: 1920, 79: 1080}[number]

    def EnumWindows(self, callback, parameter):
        for hwnd in self.z_order:
            callback(hwnd, parameter)
        return True

    def GetWindowRect(self, hwnd, rect):
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1920, 1080
        return True

    def GetClassNameW(self, hwnd, buffer, capacity):
        buffer.value = self.class_name
        return len(buffer.value)

    def GetWindowTextW(self, hwnd, buffer, capacity):
        buffer.value = self.title
        return len(buffer.value)

    def WindowFromPoint(self, point):
        self.point_checks += 1
        return self.hit


def native_fake():
    native = object.__new__(overlay._OverlayScreenBackend)
    native.api = FakeAPI()
    native.ctypes = SimpleNamespace(byref=lambda value: value,
                                    create_unicode_buffer=lambda capacity: Buffer(),
                                    set_last_error=lambda value: None,
                                    get_last_error=lambda: 0)
    native.types = SimpleNamespace(
        RECT=lambda: SimpleNamespace(), POINT=lambda x, y: SimpleNamespace(x=x, y=y))
    native.enum_callback = lambda callback: callback
    native._get_style = lambda hwnd, index: native.api.style
    native._qualifications = {}
    return native


@pytest.mark.parametrize("flags", [FLAGS, FLAGS | overlay.WS_EX_LAYERED])
def test_only_exact_annotation_with_both_required_styles_is_qualified(flags):
    result = overlay._qualification(88, overlay.ANNOTATION_CLASS, overlay.ANNOTATION_TITLE, flags)
    assert result["qualified"] is True
    assert result["ws_ex_layered"] == bool(flags & overlay.WS_EX_LAYERED)


def test_second_exact_annotation_title_qualifies_without_modifying_indicator():
    native = native_fake()
    native.api.title = "ChatGPT is using your computer. Esc to cancel"
    assert native.snapshot(44, (160, 120), None)[0] == 44
    assert native.qualifications[0]["qualified"] is True
    assert native.api.point_checks > 0


def test_similar_indicator_title_does_not_qualify_and_reports_style_detail():
    native = native_fake()
    native.api.title = "ChatGPT is using your computer. Esc to cancel extra"
    with pytest.raises(WindowTargetError, match="obscures") as caught:
        native.snapshot(44, (160, 120), None)
    assert "exstyle=0x08000020" in str(caught.value)
    assert "allowed_title=False" in str(caught.value)


def test_second_exact_annotation_still_requires_both_styles():
    native = native_fake()
    native.api.title = "ChatGPT is using your computer. Esc to cancel"
    native.api.style = overlay.WS_EX_NOACTIVATE
    with pytest.raises(WindowTargetError, match="WS_EX_TRANSPARENT=False"):
        native.snapshot(44, (160, 120), None)


@pytest.mark.parametrize("class_name,title,flags", [
    ("Other", overlay.ANNOTATION_TITLE, FLAGS),
    (overlay.ANNOTATION_CLASS, "Other", FLAGS),
    (overlay.ANNOTATION_CLASS, overlay.ANNOTATION_TITLE, overlay.WS_EX_TRANSPARENT),
    (overlay.ANNOTATION_CLASS, overlay.ANNOTATION_TITLE, overlay.WS_EX_NOACTIVATE),
    (overlay.ANNOTATION_CLASS, overlay.ANNOTATION_TITLE, 0),
])
def test_names_and_each_style_are_required(class_name, title, flags):
    assert overlay._qualification(88, class_name, title, flags)["qualified"] is False


def test_qualified_rectangle_exception_preserves_point_checks_and_metadata():
    native = native_fake()
    assert native.snapshot(44, (160, 120), None) == (44, (10, 20, 170, 140))
    assert native.api.point_checks > 0
    assert native.qualifications[0]["qualified"] is True


def test_qualified_annotation_hit_still_blocks_capture():
    native = native_fake()
    native.api.hit = 88
    with pytest.raises(WindowTargetError, match="obscured"):
        native.snapshot(44, (160, 120), None)


def test_annotation_missing_flags_is_rejected_with_style_detail():
    native = native_fake()
    native.api.style = overlay.WS_EX_TRANSPARENT
    with pytest.raises(WindowTargetError, match="obscures") as caught:
        native.snapshot(44, (160, 120), None)
    assert "exstyle=0x00000020" in str(caught.value)
    assert "WS_EX_NOACTIVATE=False" in str(caught.value)


def test_other_transparent_window_is_never_ignored():
    native = native_fake()
    native.api.class_name = "Other"
    native.api.title = "Other overlay"
    with pytest.raises(WindowTargetError, match="obscures"):
        native.snapshot(44, (160, 120), None)


def test_foreground_and_size_checks_are_preserved():
    native = native_fake()
    native.api.foreground = 99
    with pytest.raises(WindowTargetError, match="foreground"):
        native.snapshot(44, (160, 120), None)
    native.api.foreground = 44
    with pytest.raises(WindowTargetError, match="size changed"):
        native.snapshot(44, (161, 120), None)


def test_failed_qualification_never_authorizes_rectangle_exception():
    native = native_fake()
    native._get_style = lambda hwnd, index: 0
    native.ctypes.get_last_error = lambda: 5
    with pytest.raises(WindowTargetError, match="obscures"):
        native.snapshot(44, (160, 120), None)


class FakeCapture:
    qualifications = [{"hwnd": 88, "qualified": True}]

    def __init__(self):
        self.snapshots = 0
        self.restored = False
        self.changed = False
        self.image_size = (4, 3)

    @contextmanager
    def physical_pixels(self):
        try:
            yield
        finally:
            self.restored = True

    def snapshot(self, hwnd, expected, regions):
        assert hwnd == 44 and expected == (4, 3) and regions is None
        self.snapshots += 1
        offset = 1 if self.changed and self.snapshots == 2 else 0
        return 44, (10 + offset, 20, 14 + offset, 23)

    def grab(self, rect):
        return Image.new("RGB", self.image_size, "red")


def test_capture_checks_before_after_and_offers_read_only_qualification_metadata(monkeypatch):
    native = FakeCapture()
    monkeypatch.setattr(overlay, "_native_backend", lambda: native)
    image = overlay.capture_client_overlay_aware(44, (4, 3))
    assert image.size == (4, 3)
    assert image.info["probe_overlay_qualification"] == native.qualifications
    assert native.snapshots == 2 and native.restored


def test_capture_rejects_geometry_change_and_restores_context(monkeypatch):
    native = FakeCapture()
    native.changed = True
    monkeypatch.setattr(overlay, "_native_backend", lambda: native)
    with pytest.raises(WindowTargetError, match="geometry changed"):
        overlay.capture_client_overlay_aware(44, (4, 3))
    assert native.restored


def test_capture_rejects_wrong_pixel_dimensions(monkeypatch):
    native = FakeCapture()
    native.image_size = (2, 3)
    monkeypatch.setattr(overlay, "_native_backend", lambda: native)
    with pytest.raises(WindowTargetError, match="inconsistent physical pixels"):
        overlay.capture_client_overlay_aware(44, (4, 3))


def test_explicit_overlay_inspection_is_read_only_metadata(monkeypatch):
    native = native_fake()

    @contextmanager
    def pixels():
        yield

    native.physical_pixels = pixels
    monkeypatch.setattr(overlay, "_native_backend", lambda: native)
    result = overlay.inspect_overlay(88)
    assert result["qualified"] is True
    assert result["extended_style_hex"] == "0x08000020"


@pytest.mark.parametrize("hwnd", [0, -1, True, "44"])
def test_invalid_window_handles_do_not_construct_native_backend(monkeypatch, hwnd):
    def forbidden():
        raise AssertionError("must validate first")

    monkeypatch.setattr(overlay, "_native_backend", forbidden)
    with pytest.raises(WindowTargetError):
        overlay.capture_client_overlay_aware(hwnd)
    with pytest.raises(WindowTargetError):
        overlay.inspect_overlay(hwnd)
