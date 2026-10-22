from __future__ import annotations

import os
from multiprocessing.shared_memory import SharedMemory
import time
from types import SimpleNamespace

import numpy as np
import pytest

from core.perception import CaptureError
from tibia_client.capture import CaptureProcess
from tibia_client.capture import _bounded_diagnostic


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
    capture = CaptureProcess(solid_provider, timeout=5, startup_timeout=15)
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
    capture = CaptureProcess(solid_provider, timeout=5, startup_timeout=15, capture_size=(4, 3))
    assert capture.capture().size == (4, 3)
    name = capture._shared_memory.name
    assert capture._shared_memory.size == 4 * 3 * 3
    assert capture.capture().size == (4, 3)
    assert capture._shared_memory.name == name

    capture.close()
    with pytest.raises(FileNotFoundError):
        SharedMemory(name=name, create=False)


def test_provider_error_is_contextual_and_does_not_restart_unavailable_source():
    capture = CaptureProcess(failing_provider, timeout=5, startup_timeout=15, max_restarts=3)
    with pytest.raises(CaptureError, match="RuntimeError: camera unavailable"):
        capture.capture()
    assert capture._starts == 1
    assert capture.process is None


def test_worker_error_preserves_error_only_availability_diagnostic():
    capture = CaptureProcess(solid_provider)
    diagnostic = {"hwnd": 41, "is_window": False}
    with pytest.raises(CaptureError, match="provider failed") as caught:
        capture._decode(("error", 7, "BitBlt failed", diagnostic), 7, time.monotonic() + 1)
    assert caught.value.client_availability == diagnostic
    assert capture.process is None


def test_error_diagnostics_cannot_make_the_worker_pipe_payload_unbounded():
    assert _bounded_diagnostic({"is_window": False}) == {"is_window": False}
    bounded = _bounded_diagnostic({"unexpected": "x" * 10_000})
    assert "metadata limit" in bounded["diagnostic_error"]


def test_timeout_terminates_and_reaps_hung_worker():
    capture = CaptureProcess(slow_provider, timeout=0.08)
    with pytest.raises(CaptureError, match="timed out"):
        capture.capture()
    assert capture.process is None


def test_cancellation_terminates_and_reaps_worker():
    capture = CaptureProcess(slow_provider, timeout=2)
    with pytest.raises(RuntimeError, match="cancelled"):
        capture.capture(CancelAfterTwoChecks())
    assert capture.process is None


def test_worker_crash_has_strict_bounded_restart_count():
    capture = CaptureProcess(crashing_provider, timeout=5, startup_timeout=15, max_restarts=1)
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
    capture = CaptureProcess(solid_provider, timeout=5, startup_timeout=15, capture_size=(8, 6))
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


@pytest.mark.parametrize("value", [0, -1, 61, float("inf"), float("nan")])
def test_startup_timeout_must_be_finite_positive_and_bounded(value):
    with pytest.raises(ValueError, match="startup_timeout"):
        CaptureProcess(solid_provider, startup_timeout=value)


@pytest.mark.parametrize("value", [True, "15", object()])
def test_startup_timeout_rejects_nonreal_values(value):
    with pytest.raises(TypeError, match="startup_timeout"):
        CaptureProcess(solid_provider, startup_timeout=value)


class StartupClock:
    def __init__(self):
        self.value = 100.0

    def monotonic(self):
        return self.value


class StartupProcess:
    def __init__(self):
        self.alive = True
        self.closed = False
        self.exitcode = 0

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.alive = False

    def join(self, _timeout):
        pass

    def close(self):
        self.closed = True


class StartupBuffer:
    def __init__(self):
        self.buf = bytearray(range(36))
        self.closed = self.unlinked = False

    def close(self):
        self.closed = True

    def unlink(self):
        self.unlinked = True


class StartupConnection:
    def __init__(self, clock, responses):
        self.clock, self.responses = clock, list(responses)
        self.sent, self.received = [], []
        self.closed = False

    def poll(self, wait):
        if self.responses and self.responses[0][0] <= self.clock.value + wait:
            self.clock.value = max(self.clock.value, self.responses[0][0])
            return True
        self.clock.value += wait
        return False

    def recv(self):
        _, message = self.responses.pop(0)
        self.received.append(message)
        return message

    def send(self, message):
        # Startup must never queue a native provider invocation before readiness.
        if message[0] == "capture":
            assert ("ready",) in self.received
        self.sent.append(message)

    def close(self):
        self.closed = True


def simulated_startup(monkeypatch, responses, *, startup_timeout=10, timeout=0.2):
    import tibia_client.capture as module

    clock = StartupClock()
    connection = StartupConnection(clock, responses)
    process, buffer = StartupProcess(), StartupBuffer()
    capture = CaptureProcess(solid_provider, timeout=timeout,
                             startup_timeout=startup_timeout, capture_size=(4, 3))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=clock.monotonic))

    def start():
        if capture._process is not None and capture._process.is_alive():
            return
        capture._process, capture._connection = process, connection
        capture._shared_memory = buffer
        capture._awaiting_startup = True
        capture._startup_started_at = clock.value
        capture._starts += 1

    monkeypatch.setattr(capture, "_start", start)
    return capture, clock, connection, process, buffer


def frame_response(request_id, observed_at):
    return ("frame", request_id, 4, 3, 1.0, "simulation", observed_at)


def test_startup_allowance_resets_capture_deadline_and_reuses_ready_worker(monkeypatch):
    capture, clock, connection, process, _ = simulated_startup(monkeypatch, [
        (107.0, ("ready",)), (107.1, frame_response(1, 107.0)),
        (107.2, frame_response(2, 107.1)),
    ])
    try:
        assert capture.capture().size == (4, 3)
        assert capture.capture().size == (4, 3)
        assert capture._starts == 1
        assert connection.sent == [("capture", 1), ("capture", 2)]
        assert not capture._awaiting_startup
        assert capture.last_observed_at == 107.1
        assert clock.value == 107.2
    finally:
        capture.close()
    assert process.closed


def test_ready_worker_cannot_reuse_startup_allowance_for_slow_later_capture(monkeypatch):
    capture, clock, _, process, _ = simulated_startup(monkeypatch, [
        (107.0, ("ready",)), (107.1, frame_response(1, 107.0)),
        (108.0, frame_response(2, 107.1)),
    ])
    capture.capture()
    with pytest.raises(CaptureError, match="capture timed out after 0.2"):
        capture.capture()
    assert clock.value == pytest.approx(107.3)
    assert capture.process is None and process.closed


def test_startup_timeout_reaps_and_never_invokes_provider(monkeypatch):
    capture, clock, connection, process, buffer = simulated_startup(
        monkeypatch, [(102.0, ("ready",))], startup_timeout=1,
    )
    with pytest.raises(CaptureError, match="startup timed out after 1"):
        capture.capture()
    assert clock.value == pytest.approx(101.0)
    assert connection.sent == []
    assert capture.process is None and process.closed and connection.closed
    assert buffer.closed and buffer.unlinked
    assert not capture._awaiting_startup and capture._startup_started_at is None


def test_default_none_retains_single_original_capture_deadline(monkeypatch):
    capture, clock, connection, _, _ = simulated_startup(monkeypatch, [
        (100.15, ("ready",)), (100.25, frame_response(1, 100.15)),
    ], startup_timeout=None)
    with pytest.raises(CaptureError, match="capture timed out after 0.2"):
        capture.capture()
    assert clock.value == pytest.approx(100.2)
    assert connection.sent == [("capture", 1)]


@pytest.mark.parametrize("message", [("ready", 1), ("ready", None),
                                          frame_response(1, 100.0), ["ready"]])
def test_malformed_startup_handshake_fails_closed_without_dispatch(monkeypatch, message):
    capture, _, connection, process, _ = simulated_startup(
        monkeypatch, [(100.1, message)],
    )
    with pytest.raises(CaptureError, match="handshake|readiness"):
        capture.capture()
    assert connection.sent == []
    assert capture.process is None and process.closed


def test_duplicate_startup_handshake_on_reused_worker_is_rejected(monkeypatch):
    capture, _, _, process, _ = simulated_startup(monkeypatch, [
        (100.05, ("ready",)), (100.1, frame_response(1, 100.05)),
        (100.15, ("ready",)),
    ])
    capture.capture()
    with pytest.raises(CaptureError, match="malformed startup handshake"):
        capture.capture()
    assert capture.process is None and process.closed


def test_cancel_during_startup_reaps_before_provider_request(monkeypatch):
    capture, _, connection, process, _ = simulated_startup(monkeypatch, [
        (101.0, ("ready",)),
    ])

    class CancelWhenReady:
        def checkpoint(self):
            if connection.received:
                raise RuntimeError("cancelled during startup")

    with pytest.raises(RuntimeError, match="cancelled during startup"):
        capture.capture(CancelWhenReady())
    assert connection.sent == []
    assert capture.process is None and process.closed
