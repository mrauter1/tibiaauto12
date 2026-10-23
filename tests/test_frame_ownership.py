"""Frame snapshots and ROI images must outlive reusable capture storage."""

from multiprocessing.shared_memory import SharedMemory
import time

import numpy as np
from PIL import Image
import pytest

from core.perception import CaptureError, DetectionError, Frame
from tibia_client.capture import CaptureProcess


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "LA", "P", "1", "CMYK", "I", "F"])
def test_pil_snapshot_preserves_rgb_conversion_and_owns_its_pixels(mode):
    pixels = np.arange(4 * 5 * 3, dtype=np.uint8).reshape(4, 5, 3)
    source = Image.fromarray(pixels).convert(mode)
    expected = source.convert("RGB").tobytes()
    frame = Frame(source, captured_at=12.5, source="pil-capture")
    source.paste(0, (0, 0, source.width, source.height))
    source.close()

    assert frame._rgb == expected
    assert frame.size == (5, 4)
    assert frame.timestamp == 12.5 and frame.source == "pil-capture"
    assert not frame.pixels.flags.writeable


@pytest.mark.parametrize("size", [(0, 0), (0, 2), (2, 0)])
def test_empty_pil_images_remain_invalid(size):
    with pytest.raises(CaptureError, match="non-empty"):
        Frame(Image.new("RGB", size))


@pytest.mark.parametrize("region", [
    (1, 1, 4, 3),  # Noncontiguous rows in the source array.
    (2, 2, 3, 3),
    (0, 1, 5, 3),  # Contiguous full-width strip.
    (1, 0, 4, 4),
    (0, 0, 5, 4),
])
def test_crop_materializes_only_roi_and_returns_independent_pixels(monkeypatch, region):
    pixels = np.arange(4 * 5 * 3, dtype=np.uint8).reshape(4, 5, 3)
    frame = Frame(pixels, captured_at=12.5, source="ownership-test")

    def full_image_not_needed(_frame):
        raise AssertionError("cropping must not materialize the full Frame image")

    monkeypatch.setattr(Frame, "image", property(full_image_not_needed))
    left, top, right, bottom = region
    expected = pixels[top:bottom, left:right]
    crop = frame.crop(region)
    sibling = frame.crop(region)
    assert crop.mode == "RGB"
    assert crop.size == (right - left, bottom - top)
    np.testing.assert_array_equal(np.asarray(crop), expected)

    crop.putpixel((0, 0), (255, 255, 255))
    np.testing.assert_array_equal(frame.pixels, pixels)
    np.testing.assert_array_equal(np.asarray(sibling), expected)
    assert frame.captured_at == 12.5 and frame.source == "ownership-test"


@pytest.mark.parametrize("region", [
    (False, 0, 1, 1), (0, 0, 1, True), (0, 0, 1.0, 1),
    (0, -1, 1, 1), (0, 0, 2, 3), (0, 1, 1, 1),
    (0, 0, 1), "0011",
])
def test_crop_still_rejects_invalid_regions(region):
    frame = Frame(np.zeros((2, 2, 3), dtype=np.uint8))
    with pytest.raises(DetectionError):
        frame.crop(region)


def test_owned_rgb_bytes_are_adopted_without_copy_and_remain_read_only():
    pixels = bytes(range(18))
    frame = Frame._from_rgb_bytes(pixels, 3, 2, captured_at=42.5, source="shared-capture")
    assert frame._rgb is pixels
    assert frame.size == (3, 2)
    assert frame.timestamp == 42.5 and frame.source == "shared-capture"
    assert not frame.pixels.flags.writeable
    with pytest.raises(ValueError):
        frame.pixels.flags.writeable = True
    image = frame.image
    image.putpixel((0, 0), (255, 255, 255))
    assert tuple(frame.pixels[0, 0]) == (0, 1, 2)


@pytest.mark.parametrize("pixels", [
    bytearray(6), memoryview(bytearray(6)), memoryview(bytearray(6)).toreadonly(),
])
def test_rgb_adoption_rejects_views_even_when_marked_read_only(pixels):
    with pytest.raises(CaptureError, match="immutable bytes"):
        Frame._from_rgb_bytes(pixels, 2, 1, captured_at=1, source="test")


@pytest.mark.parametrize("width,height", [(0, 1), (-1, 1), (True, 2), (2.0, 1), (2, 2)])
def test_rgb_adoption_rejects_invalid_shape(width, height):
    with pytest.raises(CaptureError, match="dimensions"):
        Frame._from_rgb_bytes(bytes(6), width, height, captured_at=1, source="test")


def test_process_decode_snapshots_survive_buffer_reuse_and_release():
    capture = CaptureProcess(capture_size=(3, 2))
    shared = SharedMemory(create=True, size=18)
    capture._shared_memory = shared
    original = bytes(range(18))
    source = "capture-source:" + "x" * 300
    try:
        shared.buf[:] = original
        observed_at = time.monotonic()
        first = capture._decode(
            ("frame", 1, 3, 2, 17.25, source, observed_at),
            1, observed_at + 10,
        )
        assert first.timestamp == 17.25 and first.source == source[:256]
        assert capture.last_observed_at == observed_at
        shared.buf[:] = bytes([200]) * 18
        second = capture._decode(
            ("frame", 2, 3, 2, 18.5, "next", observed_at),
            2, observed_at + 10,
        )
    finally:
        capture.close()

    assert first._rgb == original
    assert second._rgb == bytes([200]) * 18
    assert first.crop((1, 1, 3, 2)).tobytes() == original[12:18]
    assert not first.pixels.flags.writeable
    assert not second.pixels.flags.writeable
