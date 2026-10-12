from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import ctypes
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from core.HookWindow import (
    CaptureError,
    DetectionError,
    Frame,
    LocateAllImages,
    LocateImage,
    LocateMatch,
    PixelMatchesColor,
    TakeImage,
    frame_scope,
    reset_capture_provider,
    set_capture_provider,
)
from core.perception import capture_frame, locate_match, locate_matches
from engine.ScanStages import ScanStages
from core import perception


@pytest.fixture(autouse=True)
def _provider_cleanup():
    reset_capture_provider()
    yield
    reset_capture_provider()


def _pattern() -> np.ndarray:
    return np.asarray(
        [
            [[10, 30, 80], [40, 90, 20], [200, 60, 10]],
            [[90, 10, 150], [20, 220, 60], [70, 40, 190]],
            [[130, 70, 20], [10, 120, 240], [180, 30, 90]],
        ],
        dtype=np.uint8,
    )


def test_frame_is_rgb_timestamped_and_immutable():
    source = np.zeros((3, 4, 3), dtype=np.uint8)
    source[0, 0] = (1, 2, 3)
    frame = Frame(source, captured_at=12.5)
    source[0, 0] = 255

    assert frame.timestamp == 12.5
    assert tuple(frame.pixels[0, 0]) == (1, 2, 3)
    assert not frame.pixels.flags.writeable
    with pytest.raises(ValueError):
        frame.pixels[0, 0] = 9


def test_frame_scope_captures_once_and_take_image_uses_client_local_crop():
    calls = 0
    pixels = np.zeros((8, 9, 3), dtype=np.uint8)
    pixels[3, 2] = (17, 33, 201)

    def provider():
        nonlocal calls
        calls += 1
        return Image.fromarray(pixels)

    set_capture_provider(provider)
    with frame_scope() as frame:
        assert TakeImage((2, 3, 3, 4)).getpixel((0, 0)) == (17, 33, 201)
        assert PixelMatchesColor(2, 3, (17, 33, 201))
        assert TakeImage(frame=frame).size == (9, 8)
    assert calls == 1


@pytest.mark.parametrize("region", [(-1, 0, 2, 2), (0, 0, 5, 2), (1, 1, 1, 2), (0.0, 0, 1, 1)])
def test_roi_validation_is_strict_and_nonempty(region):
    with pytest.raises(DetectionError):
        TakeImage(region, frame=Frame(np.zeros((4, 4, 3), dtype=np.uint8)))


def test_optional_match_preserves_real_origin_and_legacy_tuple_is_compatible(tmp_path):
    template = _pattern()
    path = tmp_path / "pattern.png"
    Image.fromarray(template).save(path)
    haystack = np.zeros((12, 12, 3), dtype=np.uint8)
    haystack[:3, :3] = template
    frame = Frame(haystack)

    match = LocateMatch(path, Precision=0.99, frame=frame)
    assert match is not None and match.position == (0, 0)
    assert LocateImage(path, Precision=0.99, frame=frame) == (0, 0)


def test_template_matching_handles_brightness_and_constant_templates(tmp_path):
    pattern = _pattern()
    pattern_path = tmp_path / "variable.png"
    Image.fromarray(pattern).save(pattern_path)
    brighter = np.clip(pattern.astype(np.int16) + 20, 0, 255).astype(np.uint8)
    haystack = np.full((15, 16, 3), 2, dtype=np.uint8)
    haystack[5:8, 7:10] = brighter
    assert locate_match(pattern_path, frame=Frame(haystack), precision=0.99).position == (7, 5)

    constant_path = tmp_path / "constant.png"
    Image.fromarray(np.full((3, 4, 3), 77, dtype=np.uint8)).save(constant_path)
    constant_haystack = np.zeros((12, 14, 3), dtype=np.uint8)
    constant_haystack[4:7, 6:10] = 77
    assert locate_match(constant_path, frame=Frame(constant_haystack), precision=0.99).position == (6, 4)

    zero_path = tmp_path / "zero.png"
    Image.fromarray(np.zeros((3, 3, 3), dtype=np.uint8)).save(zero_path)
    zero_haystack = np.full((10, 10, 3), 255, dtype=np.uint8)
    zero_haystack[2:5, 4:7] = 0
    assert locate_match(zero_path, frame=Frame(zero_haystack), precision=0.999).position == (4, 2)


def test_large_uniform_response_fails_fast_as_ambiguous(tmp_path):
    path = tmp_path / "black.png"
    Image.fromarray(np.zeros((3, 3, 3), dtype=np.uint8)).save(path)
    uniform = Frame(np.zeros((500, 500, 3), dtype=np.uint8))
    started = time.monotonic()
    with pytest.raises(DetectionError, match="ambiguous"):
        locate_matches(path, frame=uniform, precision=0.99)
    assert time.monotonic() - started < 0.5


def test_nms_counts_actual_shipped_template_objects_once():
    template_path = Path(__file__).parents[1] / "images" / "Targets" / "Names" / "Orc.png"
    template = np.asarray(Image.open(template_path).convert("RGB"))
    height, width = template.shape[:2]
    canvas = np.full((height + 12, width * 2 + 24, 3), 113, dtype=np.uint8)
    canvas[2 : 2 + height, 3 : 3 + width] = template
    canvas[8 : 8 + height, width + 17 : width * 2 + 17] = template
    frame = Frame(canvas)

    matches = locate_matches(template_path, frame=frame, precision=0.99)
    assert [match.position for match in matches] == [(3, 2), (width + 17, 8)]
    assert LocateAllImages(template_path, Precision=0.99, frame=frame) == 2


def test_missing_and_unreadable_templates_have_clear_errors(tmp_path):
    frame = Frame(np.zeros((5, 5, 3), dtype=np.uint8))
    with pytest.raises(DetectionError, match="does not exist or is unreadable"):
        locate_match(tmp_path / "missing.png", frame=frame)
    broken = tmp_path / "broken.png"
    broken.write_text("not an image")
    with pytest.raises(DetectionError, match="unreadable"):
        locate_match(broken, frame=frame)


def test_capture_deadline_does_not_spawn_workers_for_hung_provider():
    release = threading.Event()

    def hung():
        release.wait()
        return np.zeros((2, 2, 3), dtype=np.uint8)

    before = {thread.ident for thread in threading.enumerate() if thread.name == "capture-actor"}
    with pytest.raises(CaptureError, match="timed out"):
        capture_frame(hung, max_attempts=3, timeout=0.03)
    with pytest.raises(CaptureError, match="timed out"):
        capture_frame(hung, timeout=0.03)
    after = {thread.ident for thread in threading.enumerate() if thread.name == "capture-actor"}
    assert len(after - before) <= 1
    release.set()
    time.sleep(0.02)


def test_concurrent_capture_calls_serialize_one_provider_invocation_at_a_time():
    active = 0
    maximum = 0
    guard = threading.Lock()

    def provider():
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.01)
        with guard:
            active -= 1
        return np.zeros((2, 2, 3), dtype=np.uint8)

    with ThreadPoolExecutor(max_workers=4) as executor:
        frames = list(executor.map(lambda _: capture_frame(provider, timeout=0.3), range(4)))
    assert len(frames) == 4
    assert maximum == 1


def test_default_windows_capture_rejects_stale_calibrated_size(monkeypatch):
    monkeypatch.setattr(
        perception,
        "_load_capture_config",
        lambda: {"platform": "windows", "capture_hwnd": 42, "capture_size": [800, 600]},
    )
    monkeypatch.setattr(perception, "_capture_windows", lambda hwnd: Image.new("RGB", (1024, 768)))

    with pytest.raises(CaptureError, match="client size changed.*recalibrate"):
        perception._default_capture()


def test_default_windows_capture_accepts_current_calibrated_size(monkeypatch):
    image = Image.new("RGB", (800, 600))
    monkeypatch.setattr(
        perception,
        "_load_capture_config",
        lambda: {"platform": "windows", "capture_hwnd": 42, "capture_size": [800, 600]},
    )
    monkeypatch.setattr(perception, "_capture_windows", lambda hwnd: image)

    assert perception._default_capture() is image


@pytest.mark.parametrize("print_result", [1, 0])
def test_windows_capture_releases_gdi_resources_in_safe_order(monkeypatch, print_result):
    events = []

    class PrintWindow:
        argtypes = None
        restype = None

        def __call__(self, hwnd, hdc, flags):
            events.append("print")
            return print_result

    class Bitmap:
        def CreateCompatibleBitmap(self, dc, width, height):
            events.append("create_bitmap_storage")

        def GetHandle(self):
            return 77

        def GetInfo(self):
            events.append("bitmap_info")
            return {"bmWidth": 3, "bmHeight": 2}

        def GetBitmapBits(self, signed):
            events.append("bitmap_bits")
            return bytes(3 * 2 * 4)

    class SaveDC:
        def SelectObject(self, bitmap):
            events.append("select_bitmap")

        def GetSafeHdc(self):
            return 88

        def DeleteDC(self):
            events.append("delete_compatible_dc")

    class BorrowedDC:
        def CreateCompatibleDC(self):
            events.append("create_compatible_dc")
            return SaveDC()

        def Detach(self):
            events.append("detach_borrowed_wrapper")

        def DeleteDC(self):
            events.append("incorrect_delete_borrowed_dc")

    fake_gui = SimpleNamespace(
        GetClientRect=lambda hwnd: (0, 0, 3, 2),
        GetWindowDC=lambda hwnd: events.append("get_window_dc") or 99,
        DeleteObject=lambda handle: events.append("delete_bitmap"),
        ReleaseDC=lambda hwnd, dc: events.append("release_window_dc"),
    )
    fake_ui = SimpleNamespace(
        CreateDCFromHandle=lambda handle: events.append("wrap_borrowed_dc") or BorrowedDC(),
        CreateBitmap=lambda: events.append("create_bitmap") or Bitmap(),
    )
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(PrintWindow=PrintWindow())), raising=False)
    monkeypatch.setitem(sys.modules, "win32gui", fake_gui)
    monkeypatch.setitem(sys.modules, "win32ui", fake_ui)

    if print_result:
        assert perception._capture_windows(42).size == (3, 2)
    else:
        with pytest.raises(CaptureError, match="PrintWindow failed"):
            perception._capture_windows(42)

    assert "incorrect_delete_borrowed_dc" not in events
    assert events.index("delete_compatible_dc") < events.index("delete_bitmap")
    assert events.index("delete_bitmap") < events.index("detach_borrowed_wrapper")
    assert events.index("detach_borrowed_wrapper") < events.index("release_window_dc")


def test_scan_stages_uses_one_capture_tolerance_and_detects_five_percent():
    normal = (219, 79, 79)
    full = (194, 74, 74)
    pixels = np.zeros((4, 120, 3), dtype=np.uint8)
    pixels[1, 5] = (220, 78, 80)
    calls = 0

    def provider():
        nonlocal calls
        calls += 1
        return pixels

    set_capture_provider(provider)
    scanner = ScanStages("life", color_tolerance=2)
    assert scanner.ScanStages((0, 1), normal, full) == 5
    assert calls == 1

    unknown = Frame(np.zeros_like(pixels))
    assert scanner.ScanStages((0, 1), normal, full, frame=unknown) is None
    assert scanner.stage is None
