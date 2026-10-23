"""Region readback tests with native API substitutes; no desktop operations."""

from contextlib import contextmanager
from types import SimpleNamespace

from PIL import Image
import pytest

from tibia_client import probe_region_gdi as region
from tibia_client import region_capture as readback
from tibia_client.probe_overlay import _OverlayScreenBackend
from tibia_client.window_target import WindowTargetError


class FakeGDI:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []
        self.header = None

    def GetDC(self, hwnd):
        self.calls.append(("GetDC", hwnd))
        return 0 if self.fail == "GetDC" else 100

    def ReleaseDC(self, hwnd, dc):
        self.calls.append(("ReleaseDC", hwnd, dc))
        return 0 if self.fail == "ReleaseDC" else 1

    def CreateCompatibleDC(self, dc):
        self.calls.append(("CreateCompatibleDC", dc))
        return 0 if self.fail == "CreateCompatibleDC" else 200

    def CreateDIBSection(self, dc, info, usage, bits, section, offset):
        self.calls.append(("CreateDIBSection", dc, usage, section, offset))
        self.header = info._obj.header
        bits._obj.value = 0 if self.fail == "null_bits" else 400
        return 0 if self.fail == "CreateDIBSection" else 300

    def SelectObject(self, dc, bitmap):
        self.calls.append(("SelectObject", dc, bitmap))
        if bitmap == 300:
            return 0 if self.fail == "SelectObject" else 500
        return 0 if self.fail == "restore" else 300

    def BitBlt(self, *arguments):
        self.calls.append(("BitBlt", *arguments))
        if self.fail == "BitBlt_exception":
            raise OSError("native copy unavailable")
        return self.fail != "BitBlt"

    def GdiFlush(self):
        self.calls.append(("GdiFlush",))
        return self.fail != "GdiFlush"

    def DeleteDC(self, dc):
        self.calls.append(("DeleteDC", dc))
        if self.fail == "DeleteDC_exception":
            raise OSError("delete DC unavailable")
        return self.fail != "DeleteDC"

    def DeleteObject(self, bitmap):
        self.calls.append(("DeleteObject", bitmap))
        return self.fail != "DeleteObject"


def readback_fake(fail=None):
    native = object.__new__(region._GDIReadback)
    api = FakeGDI(fail)
    native.user = native.gdi = api
    # Top-down pixels: red, green, then blue, white. X bytes are ignored.
    raw = bytes((0, 0, 255, 9, 0, 255, 0, 8, 255, 0, 0, 7, 255, 255, 255, 6))
    native.read_pixels = lambda pointer, count: raw
    return native, api


def test_region_bitblt_uses_exact_source_rectangle_and_top_down_32bit_dib():
    native, api = readback_fake()
    image = native.capture((13, 29, 15, 31))
    assert image.size == (2, 2) and image.mode == "RGB"
    assert list(image.getdata()) == [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
    assert api.header.biSize == 40
    assert (api.header.biWidth, api.header.biHeight) == (2, -2)
    assert (api.header.biPlanes, api.header.biBitCount, api.header.biCompression) == (1, 32, 0)
    assert api.header.biSizeImage == 16
    assert ("BitBlt", 200, 0, 0, 2, 2, 100, 13, 29, readback.SRCCOPY) in api.calls
    assert readback.SRCCOPY == 0x00CC0020
    assert api.calls[-4:] == [("SelectObject", 200, 500), ("DeleteDC", 200),
                              ("DeleteObject", 300), ("ReleaseDC", 0, 100)]
    assert ("DeleteObject", 500) not in api.calls
    assert ("DeleteDC", 100) not in api.calls


def test_negative_physical_screen_coordinates_are_preserved():
    native, api = readback_fake()
    native.capture((-12, -24, -10, -22))
    assert ("BitBlt", 200, 0, 0, 2, 2, 100, -12, -24, readback.SRCCOPY) in api.calls


@pytest.mark.parametrize("rect", [
    (0, 0, 0, 2), (2, 0, 1, 2), (0, 0, 20000, 2), (0, 0, 10000, 10000),
    (True, 0, 2, 2), (0.5, 0, 2, 2), (0, 0, 2), (2**31, 0, 2**31 + 2, 2),
])
def test_invalid_region_is_rejected_before_any_resource_acquisition(rect):
    native, api = readback_fake()
    with pytest.raises(WindowTargetError, match="GDI region"):
        native.capture(rect)
    assert api.calls == []


@pytest.mark.parametrize("failure,error,expected_cleanup", [
    ("GetDC", "GetDC failed", []),
    ("CreateCompatibleDC", "CreateCompatibleDC failed", [("ReleaseDC", 0, 100)]),
    ("CreateDIBSection", "CreateDIBSection failed", [("DeleteDC", 200), ("ReleaseDC", 0, 100)]),
    ("null_bits", "no pixel buffer", [("DeleteDC", 200), ("DeleteObject", 300), ("ReleaseDC", 0, 100)]),
    ("SelectObject", "SelectObject failed", [("DeleteDC", 200), ("DeleteObject", 300), ("ReleaseDC", 0, 100)]),
    ("BitBlt", "BitBlt failed", [("SelectObject", 200, 500), ("DeleteDC", 200),
                                  ("DeleteObject", 300), ("ReleaseDC", 0, 100)]),
    ("GdiFlush", "GdiFlush failed", [("SelectObject", 200, 500), ("DeleteDC", 200),
                                      ("DeleteObject", 300), ("ReleaseDC", 0, 100)]),
    ("BitBlt_exception", "native copy unavailable", [("SelectObject", 200, 500), ("DeleteDC", 200),
                                                    ("DeleteObject", 300), ("ReleaseDC", 0, 100)]),
])
def test_partial_allocation_and_native_failures_release_owned_and_borrowed_resources(
        failure, error, expected_cleanup):
    native, api = readback_fake(failure)
    with pytest.raises(WindowTargetError, match=error):
        native.capture((13, 29, 15, 31))
    if expected_cleanup:
        assert api.calls[-len(expected_cleanup):] == expected_cleanup
    else:
        assert api.calls == [("GetDC", 0)]


def test_failed_readback_and_wrong_byte_count_still_release_every_resource():
    native, api = readback_fake()
    native.read_pixels = lambda pointer, count: b"short"
    with pytest.raises(WindowTargetError, match="inconsistent byte count"):
        native.capture((13, 29, 15, 31))
    assert api.calls[-3:] == [("DeleteDC", 200), ("DeleteObject", 300), ("ReleaseDC", 0, 100)]

    def unavailable(pointer, count):
        raise OSError("readback unavailable")

    native.read_pixels = unavailable
    with pytest.raises(WindowTargetError, match="readback unavailable"):
        native.capture((13, 29, 15, 31))
    assert api.calls[-3:] == [("DeleteDC", 200), ("DeleteObject", 300), ("ReleaseDC", 0, 100)]


@pytest.mark.parametrize("failure", ["restore", "DeleteDC", "DeleteDC_exception", "DeleteObject", "ReleaseDC"])
def test_cleanup_failure_is_reported_and_does_not_skip_remaining_cleanup(failure):
    native, api = readback_fake(failure)
    with pytest.raises(WindowTargetError, match="cleanup"):
        native.capture((13, 29, 15, 31))
    assert ("DeleteObject", 300) in api.calls
    assert api.calls[-1] == ("ReleaseDC", 0, 100)


def test_region_backend_inherits_the_exact_annotation_and_visibility_checks():
    assert region._RegionGDIOverlayBackend.snapshot is _OverlayScreenBackend.snapshot
    assert region._RegionGDIOverlayBackend._is_cloaked is _OverlayScreenBackend._is_cloaked
    assert region._RegionGDIOverlayBackend.physical_pixels is _OverlayScreenBackend.physical_pixels


def test_native_function_bindings_are_declared_without_calling_native_functions(monkeypatch):
    class Function:
        def __call__(self, *arguments):
            raise AssertionError("native function must not be invoked by construction")

    class Library:
        def __init__(self):
            self.functions = {}

        def __getattr__(self, name):
            return self.functions.setdefault(name, Function())

    libraries = {"user32": Library(), "gdi32": Library()}
    monkeypatch.setattr(readback, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(readback.ctypes, "WinDLL", lambda name, **kwargs: libraries[name], raising=False)
    native = region._GDIReadback()
    assert native.user is libraries["user32"]
    assert native.gdi is libraries["gdi32"]
    assert len(native.gdi.CreateDIBSection.argtypes) == 6
    assert len(native.gdi.BitBlt.argtypes) == 9
    assert native.gdi.GdiFlush.argtypes == []


def test_region_backend_swaps_only_pixel_grab(monkeypatch):
    calls = []
    expected = Image.new("RGB", (2, 2), "red")
    monkeypatch.setattr(region, "_GDIReadback", lambda: SimpleNamespace(
        capture=lambda rect: (calls.append(rect), expected)[1]))
    backend = object.__new__(region._RegionGDIOverlayBackend)
    assert backend.grab((13, 29, 15, 31)) is expected
    assert calls == [(13, 29, 15, 31)]


class FakeScreen:
    qualifications = [{"hwnd": 88, "qualified": True}]

    def __init__(self, changed=False):
        self.changed = changed
        self.calls = []
        self.restored = False

    @contextmanager
    def physical_pixels(self):
        try:
            yield
        finally:
            self.restored = True

    def snapshot(self, hwnd, expected, regions):
        self.calls.append(("snapshot", hwnd, expected, regions))
        offset = int(self.changed and len(self.calls) > 1)
        return 44, (10 + offset, 20, 14 + offset, 23)

    def grab(self, rect):
        self.calls.append(("grab", rect))
        return Image.new("RGB", (4, 3), "red")


def test_public_region_capture_keeps_before_after_geometry_and_qualification_metadata(monkeypatch):
    native = FakeScreen()
    monkeypatch.setattr(region, "_RegionGDIOverlayBackend", lambda: native)
    image = region.capture_client_region_gdi_overlay_aware(44, (4, 3))
    assert native.calls == [("snapshot", 44, (4, 3), None), ("grab", (10, 20, 14, 23)),
                            ("snapshot", 44, (4, 3), None)]
    assert native.restored
    assert image.info["probe_overlay_qualification"] == native.qualifications


def test_public_region_capture_rejects_geometry_change_and_restores_context(monkeypatch):
    native = FakeScreen(changed=True)
    monkeypatch.setattr(region, "_RegionGDIOverlayBackend", lambda: native)
    with pytest.raises(WindowTargetError, match="geometry changed"):
        region.capture_client_region_gdi_overlay_aware(44, (4, 3))
    assert native.restored
