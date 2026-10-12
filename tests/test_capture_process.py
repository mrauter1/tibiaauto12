from __future__ import annotations

import os
from multiprocessing.shared_memory import SharedMemory
import time

import numpy as np
import pytest

from core.perception import CaptureError
from tibia_client.capture import CaptureProcess


def solid_provider():
    pixels = np.zeros((3, 4, 3), dtype=np.uint8)
    pixels[1, 2] = (7, 11, 19)
    return pixels


def failing_provider():
    raise RuntimeError("camera unavailable")


def slow_provider():
    time.sleep(5)
    return np.zeros((2, 2, 3), dtype=np.uint8)


def crashing_provider():
    os._exit(23)


class CancelAfterTwoChecks:
    def __init__(self):
        self.checks = 0

    def checkpoint(self):
        self.checks += 1
        if self.checks >= 2:
            raise RuntimeError("cancelled")


class UnkillableProcess:
    def __init__(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def terminate(self):
        pass

    def kill(self):
        pass

    def join(self, timeout):
        pass


def test_process_is_lazy_reused_and_closed_without_leaking_worker():
    capture = CaptureProcess(solid_provider, timeout=5)
    assert capture.process is None

    first = capture.capture()
    process = capture.process
    second = capture.capture()

    assert process is not None and process.is_alive()
    assert capture.process is process
    assert capture._starts == 1
    assert first.size == second.size == (4, 3)
    assert tuple(first.pixels[1, 2]) == (7, 11, 19)
    assert not first.pixels.flags.writeable

    pid = process.pid
    capture.close()
    assert capture.process is None
    assert pid not in {child.pid for child in __import__("multiprocessing").active_children()}


def test_shared_pixel_buffer_is_reused_then_unlinked_by_parent_owner():
    capture = CaptureProcess(solid_provider, timeout=5, capture_size=(4, 3))
    assert capture.capture().size == (4, 3)
    name = capture._shared_memory.name
    assert capture._shared_memory.size == 4 * 3 * 3
    assert capture.capture().size == (4, 3)
    assert capture._shared_memory.name == name

    capture.close()
    with pytest.raises(FileNotFoundError):
        SharedMemory(name=name, create=False)


def test_provider_error_is_contextual_and_does_not_restart_unavailable_source():
    capture = CaptureProcess(failing_provider, timeout=5, max_restarts=3)
    with pytest.raises(CaptureError, match="RuntimeError: camera unavailable"):
        capture.capture()
    assert capture._starts == 1
    assert capture.process is None


def test_timeout_terminates_and_reaps_hung_worker():
    capture = CaptureProcess(slow_provider, timeout=0.08)
    started = time.monotonic()
    with pytest.raises(CaptureError, match="timed out"):
        capture.capture()
    assert time.monotonic() - started < 2
    assert capture.process is None


def test_cancellation_terminates_and_reaps_worker():
    capture = CaptureProcess(slow_provider, timeout=2)
    with pytest.raises(RuntimeError, match="cancelled"):
        capture.capture(CancelAfterTwoChecks())
    assert capture.process is None


def test_worker_crash_has_strict_bounded_restart_count():
    capture = CaptureProcess(crashing_provider, timeout=5, max_restarts=1)
    with pytest.raises(CaptureError, match="2 bounded attempt"):
        capture.capture()
    assert capture._starts == 2
    assert capture.process is None


def test_unkillable_worker_handle_is_retained_and_trips_terminal_circuit():
    capture = CaptureProcess(solid_provider, timeout=0.1)
    worker = UnkillableProcess()
    pixels = SharedMemory(create=True, size=3)
    buffer_name = pixels.name
    capture._process = worker
    capture._shared_memory = pixels

    assert capture._reap() is False
    assert capture.process is worker
    observer = SharedMemory(name=buffer_name, create=False)
    observer.close()
    with pytest.raises(CaptureError, match="could not be stopped"):
        capture.capture()
    assert capture.process is worker
    assert capture._starts == 0
    try:
        with pytest.raises(CaptureError, match="could not be stopped"):
            capture.close()
        replacement = CaptureProcess(solid_provider, timeout=0.1)
        with pytest.raises(CaptureError, match="quarantined"):
            replacement.capture()
    finally:
        worker.alive = False
        capture.close()
    assert capture.process is None
    with pytest.raises(FileNotFoundError):
        SharedMemory(name=buffer_name, create=False)


def test_calibrated_size_is_validated_in_child():
    capture = CaptureProcess(solid_provider, timeout=5, capture_size=(8, 6))
    with pytest.raises(CaptureError, match="size changed.*recalibrate"):
        capture.capture()
    assert capture.process is None


@pytest.mark.parametrize("size", [(0, 1), (1,), (True, 2), "2x2"])
def test_capture_size_configuration_is_strict(size):
    with pytest.raises(ValueError, match="capture_size"):
        CaptureProcess(solid_provider, capture_size=size)


def test_capture_size_cannot_allocate_beyond_pixel_cap():
    with pytest.raises(ValueError, match="exceeds max_pixel_bytes"):
        CaptureProcess(solid_provider, capture_size=(4, 3), max_pixel_bytes=35)
