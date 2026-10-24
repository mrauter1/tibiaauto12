"""Process-isolated desktop capture.

Native capture APIs are allowed to hang.  A process, unlike a Python thread,
can be stopped and reaped when a capture deadline or cancellation is reached.
This module deliberately has no native imports or import-time side effects.
"""

from __future__ import annotations

import math
import json
import multiprocessing
from multiprocessing.connection import Connection
from multiprocessing.shared_memory import SharedMemory
from numbers import Integral, Real
import threading
import time
from typing import Any, Callable

from core.perception import CaptureError, Frame, as_frame
from tibia_client.models import TransientObservationError


class TransientCaptureError(CaptureError, TransientObservationError):
    """A stopped capture worker can be restarted on the next observation."""


def _retryable_capture_error(error):
    """Preserve an explicit native-capture classification through wrappers."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, TransientCaptureError) or getattr(error, 'capture_retryable', None) is True:
            return True
        error = error.__cause__
    return False


DEFAULT_MAX_PIXEL_BYTES = 64 * 1024 * 1024
_POLL_INTERVAL = 0.02
_MAX_ERROR_TEXT = 2_048
_MAX_SOURCE_TEXT = 256
_quarantine_lock = threading.RLock()
_quarantined: set["CaptureProcess"] = set()


def _invoke(provider: Any) -> Any:
    method = getattr(provider, "capture", None)
    return method() if callable(method) else provider()


def _bounded_diagnostic(value):
    if value is None:
        return None
    try:
        payload = json.dumps(value, default=str)
        if len(payload) <= _MAX_ERROR_TEXT:
            return json.loads(payload)
    except Exception:
        pass
    return {"diagnostic_error": "availability diagnostic exceeds the capture metadata limit"}


def _validate_size(value: object | None) -> tuple[int, int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, Integral) or item <= 0 for item in value)
    ):
        raise ValueError("capture_size must contain two positive integers")
    return int(value[0]), int(value[1])


def _worker(
    connection: Connection,
    provider: Callable[[], Any] | Any | None,
    capture_size: tuple[int, int] | None,
    max_pixel_bytes: int,
    shared_memory_name: str,
    shared_memory_capacity: int,
) -> None:
    """Child entry point.  Keep arguments and messages spawn-pickleable."""

    pixels = None
    try:
        pixels = SharedMemory(name=shared_memory_name, create=False)
        connection.send(("ready",))
        while True:
            try:
                message = connection.recv()
            except EOFError:
                return
            if not isinstance(message, tuple) or not message:
                return
            operation = message[0]
            if operation == "close":
                return
            if operation != "capture" or len(message) != 2 or not isinstance(message[1], int):
                return
            request_id = message[1]
            try:
                observed_at = time.monotonic()
                if provider is None:
                    # Import in the child and call the native implementation
                    # directly; the legacy thread actor is intentionally not
                    # part of this production path.
                    from core.perception import _default_capture

                    result = _default_capture()
                else:
                    result = _invoke(provider)
                frame = as_frame(result, source="desktop-process")
                if capture_size is not None and frame.size != capture_size:
                    raise CaptureError(
                        "captured client size changed from calibrated "
                        f"{capture_size[0]}x{capture_size[1]} to "
                        f"{frame.width}x{frame.height}; recalibrate the client profile"
                    )
                byte_count = frame.width * frame.height * 3
                if byte_count > max_pixel_bytes:
                    raise CaptureError(
                        f"capture contains {byte_count} pixel bytes; limit is {max_pixel_bytes}"
                    )
                if byte_count > shared_memory_capacity:
                    raise CaptureError(
                        f"capture contains {byte_count} pixel bytes; shared buffer "
                        f"capacity is {shared_memory_capacity}"
                    )
                # Publish pixels before the small ready message.  The pipe is
                # never used for a multi-megabyte payload, so poll()+recv()
                # cannot become stuck halfway through a frame transfer.
                pixels.buf[:byte_count] = frame._rgb
                connection.send((
                    "frame", request_id, frame.width, frame.height,
                    float(frame.captured_at), str(frame.source)[:_MAX_SOURCE_TEXT],
                    observed_at,
                ))
            except BaseException as exc:
                # Exceptions are data across the process boundary.  Their
                # class is included for diagnostics, never reconstructed.
                detail = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_TEXT]
                try:
                    diagnostic = getattr(exc, "client_availability", None)
                    cause, seen = exc.__cause__, {id(exc)}
                    while diagnostic is None and cause is not None and id(cause) not in seen:
                        seen.add(id(cause))
                        diagnostic = getattr(cause, "client_availability", None)
                        cause = cause.__cause__
                    connection.send(("error", request_id, detail, _bounded_diagnostic(diagnostic),
                                     _retryable_capture_error(exc)))
                except (BrokenPipeError, EOFError, OSError):
                    return
    finally:
        if pixels is not None:
            pixels.close()
        connection.close()


class CaptureProcess:
    """Own one lazily-started spawn worker for bounded client capture."""

    def __init__(
        self,
        provider: Callable[[], Any] | Any | None = None,
        *,
        timeout: float = 2.0,
        startup_timeout: float | None = None,
        max_restarts: int = 1,
        max_pixel_bytes: int = DEFAULT_MAX_PIXEL_BYTES,
        capture_size: object | None = None,
    ) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, Real):
            raise TypeError("capture timeout must be a real number")
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("capture timeout must be finite and positive")
        if startup_timeout is not None:
            if isinstance(startup_timeout, bool) or not isinstance(startup_timeout, Real):
                raise TypeError("capture startup_timeout must be a real number or None")
            startup_timeout = float(startup_timeout)
            if not math.isfinite(startup_timeout) or not 0 < startup_timeout <= 60:
                raise ValueError("capture startup_timeout must be finite and between 0 and 60 seconds")
        self.startup_timeout = startup_timeout
        if isinstance(max_restarts, bool) or not isinstance(max_restarts, int) or not 0 <= max_restarts <= 3:
            raise ValueError("max_restarts must be an integer from 0 through 3")
        if (
            isinstance(max_pixel_bytes, bool)
            or not isinstance(max_pixel_bytes, int)
            or max_pixel_bytes <= 0
            or max_pixel_bytes > DEFAULT_MAX_PIXEL_BYTES
        ):
            raise ValueError(f"max_pixel_bytes must be from 1 through {DEFAULT_MAX_PIXEL_BYTES}")
        if provider is not None and not callable(provider) and not callable(getattr(provider, "capture", None)):
            raise TypeError("capture provider must be callable or expose capture()")

        self.provider = provider
        self.max_restarts = max_restarts
        self.max_pixel_bytes = max_pixel_bytes
        self.capture_size = _validate_size(capture_size)
        if (
            self.capture_size is not None
            and self.capture_size[0] * self.capture_size[1] * 3 > self.max_pixel_bytes
        ):
            raise ValueError("capture_size exceeds max_pixel_bytes")
        self._context = multiprocessing.get_context("spawn")
        self._process: multiprocessing.Process | None = None
        self._connection: Connection | None = None
        self._awaiting_startup = False
        self._startup_started_at: float | None = None
        self._next_request_id = 1
        self._closed = False
        self._capture_guard = threading.Lock()
        self._starts = 0
        self._terminal_error: str | None = None
        self._shared_memory: SharedMemory | None = None
        self.last_observed_at: float | None = None
        self._shared_memory_capacity = (
            self.capture_size[0] * self.capture_size[1] * 3
            if self.capture_size is not None
            else self.max_pixel_bytes
        )

    @property
    def process(self) -> multiprocessing.Process | None:
        """Expose lifecycle state for diagnostics and deterministic tests."""

        return self._process

    def _start(self) -> None:
        if self._closed:
            raise CaptureError("capture process is closed")
        if self._terminal_error is not None:
            raise CaptureError(self._terminal_error)
        self._reject_quarantined_worker()
        if self._process is not None and self._process.is_alive():
            return
        self._reap()
        if self._terminal_error is not None:
            raise CaptureError(self._terminal_error)
        parent, child = self._context.Pipe(duplex=True)
        try:
            shared_pixels = SharedMemory(
                create=True, size=self._shared_memory_capacity
            )
        except BaseException as exc:
            parent.close()
            child.close()
            raise CaptureError(f"could not allocate capture buffer: {exc}") from exc
        process = self._context.Process(
            target=_worker,
            args=(
                child,
                self.provider,
                self.capture_size,
                self.max_pixel_bytes,
                shared_pixels.name,
                self._shared_memory_capacity,
            ),
            name="tibia-capture",
        )
        process.daemon = False
        startup_started_at = time.monotonic()
        try:
            process.start()
        except BaseException as exc:
            parent.close()
            child.close()
            try:
                shared_pixels.close()
            finally:
                try:
                    shared_pixels.unlink()
                except FileNotFoundError:
                    pass
            try:
                process.close()
            except (AttributeError, OSError, ValueError):
                pass
            raise CaptureError(f"could not start capture process: {exc}") from exc
        child.close()
        self._process = process
        self._connection = parent
        self._shared_memory = shared_pixels
        self._awaiting_startup = True
        self._startup_started_at = startup_started_at
        self._starts += 1

    def _reject_quarantined_worker(self) -> None:
        """Prevent a replacement while any abandoned worker is still alive."""

        with _quarantine_lock:
            owners = tuple(_quarantined)
        for owner in owners:
            if owner is self:
                continue
            process = owner._process
            if process is None:
                with _quarantine_lock:
                    _quarantined.discard(owner)
                continue
            try:
                alive = bool(process.is_alive())
            except Exception:
                alive = True
            if alive:
                raise CaptureError(
                    "capture is quarantined because a previous worker could not be stopped"
                )
            owner._reap()

    def _reap(self) -> bool:
        connection, process = self._connection, self._process
        self._connection = None
        self._awaiting_startup = False
        self._startup_started_at = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if process is None:
            self._process = None
            with _quarantine_lock:
                _quarantined.discard(self)
            self._release_shared_memory()
            return True
        try:
            alive = bool(process.is_alive())
            if alive:
                process.terminate()
                process.join(0.25)
                alive = bool(process.is_alive())
            if alive and hasattr(process, "kill"):
                process.kill()
                process.join(0.25)
                alive = bool(process.is_alive())
        except (AssertionError, OSError, ValueError) as exc:
            try:
                alive = bool(process.is_alive())
            except Exception:
                alive = True
            if alive:
                self._terminal_error = (
                    f"capture worker could not be stopped and remains unavailable: {exc}"
                )
        if alive:
            # Retain the handle.  Losing it could permit a replacement process
            # while an unkillable native call is still active.
            self._process = process
            if self._terminal_error is None:
                self._terminal_error = "capture worker could not be stopped and remains unavailable"
            with _quarantine_lock:
                _quarantined.add(self)
            return False
        self._process = None
        with _quarantine_lock:
            _quarantined.discard(self)
        try:
            process.close()
        except (AttributeError, OSError, ValueError):
            pass
        self._release_shared_memory()
        return True

    def _release_shared_memory(self) -> None:
        pixels, self._shared_memory = self._shared_memory, None
        if pixels is None:
            return
        try:
            pixels.close()
        except (BufferError, OSError):
            pass
        try:
            pixels.unlink()
        except FileNotFoundError:
            # The shared resource tracker may already have removed a crashed
            # child's segment.  The owner still clears its handle.
            pass

    @staticmethod
    def _checkpoint(token: Any | None) -> None:
        if token is not None:
            token.checkpoint()

    def _decode(
        self,
        response: object,
        request_id: int,
        deadline: float,
    ) -> Frame:
        if not isinstance(response, tuple) or len(response) < 3:
            raise CaptureError("capture worker returned a malformed response")
        kind, response_id = response[0], response[1]
        if response_id != request_id:
            raise CaptureError(
                f"capture worker returned response {response_id!r} for request {request_id}"
            )
        if kind == "error" and len(response) in (3, 4, 5):
            if len(response) == 5 and type(response[4]) is not bool:
                raise CaptureError("capture worker returned an invalid recovery flag")
            error_type = TransientCaptureError if len(response) == 5 and response[4] else CaptureError
            failure = error_type(f"capture provider failed: {response[2]}")
            if len(response) >= 4 and response[3] is not None:
                failure.client_availability = response[3]
            raise failure
        if kind != "frame" or len(response) != 7:
            raise CaptureError("capture worker returned a malformed response")
        _, _, width, height, captured_at, source, observed_at = response
        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
        ):
            raise CaptureError("capture worker returned invalid frame dimensions")
        byte_count = width * height * 3
        if byte_count > self.max_pixel_bytes or byte_count > self._shared_memory_capacity:
            raise CaptureError("capture worker exceeded the configured pixel limit")
        if not isinstance(captured_at, Real) or not math.isfinite(float(captured_at)):
            raise CaptureError("capture worker returned an invalid timestamp")
        returned_at = time.monotonic()
        if (
            isinstance(observed_at, bool)
            or not isinstance(observed_at, Real)
            or not math.isfinite(float(observed_at))
            or float(observed_at) <= 0
            or float(observed_at) > returned_at
        ):
            raise CaptureError("capture worker returned an invalid monotonic timestamp")
        if time.monotonic() >= deadline:
            raise TimeoutError
        shared_pixels = self._shared_memory
        if shared_pixels is None:
            raise CaptureError("capture worker returned no shared pixel buffer")
        # The worker reuses this buffer. Keep one owned, immutable snapshot;
        # constructing a Frame from an array would copy the same bytes again.
        pixels = bytes(shared_pixels.buf[:byte_count])
        if time.monotonic() >= deadline:
            raise TimeoutError
        frame = Frame._from_rgb_bytes(
            pixels, width, height,
            captured_at=float(captured_at), source=str(source)[:_MAX_SOURCE_TEXT],
        )
        self.last_observed_at = float(observed_at)
        return frame

    def capture(self, token: Any | None = None, *, timeout: float | None = None) -> Frame:
        """Capture one frame, terminating the worker on deadline/cancellation.

        An explicit startup_timeout budgets a cold spawn separately. Once the
        worker reports its shared buffer ready, the ordinary capture deadline
        begins. Omitting it preserves the original single overall deadline.
        """

        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, Real)):
            raise TypeError("capture timeout must be a real number")
        limit = self.timeout if timeout is None else float(timeout)
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError("capture timeout must be finite and positive")
        if not self._capture_guard.acquire(blocking=False):
            raise CaptureError("another capture request is already in progress")
        try:
            self.last_observed_at = None
            deadline = time.monotonic() + limit
            last_failure = "capture worker unavailable"
            for attempt in range(self.max_restarts + 1):
                self._checkpoint(token)
                try:
                    self._start()
                    connection, process = self._connection, self._process
                    if connection is None or process is None:
                        raise CaptureError("capture worker did not start")
                    if self._awaiting_startup and self.startup_timeout is not None:
                        started_at = self._startup_started_at
                        if started_at is None:
                            raise CaptureError("capture worker has no startup timestamp")
                        deadline = started_at + self.startup_timeout
                    request_id = self._next_request_id
                    self._next_request_id += 1
                    self._checkpoint(token)
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    if not self._awaiting_startup:
                        connection.send(("capture", request_id))
                    while True:
                        self._checkpoint(token)
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError
                        if connection.poll(min(_POLL_INTERVAL, remaining)):
                            try:
                                response = connection.recv()
                            except EOFError:
                                raise BrokenPipeError("capture worker exited") from None
                            if (isinstance(response, tuple) and response
                                    and response[0] == "ready"):
                                if response != ("ready",) or not self._awaiting_startup:
                                    raise CaptureError("capture worker returned a malformed startup handshake")
                                if time.monotonic() >= deadline:
                                    raise TimeoutError
                                self._checkpoint(token)
                                self._awaiting_startup = False
                                self._startup_started_at = None
                                if self.startup_timeout is not None:
                                    deadline = time.monotonic() + limit
                                connection.send(("capture", request_id))
                                continue
                            if self._awaiting_startup:
                                raise CaptureError("capture worker did not signal readiness")
                            # Only bounded metadata crosses the pipe.  Pixels
                            # were published into this worker's shared buffer.
                            return self._decode(response, request_id, deadline)
                        if not process.is_alive():
                            raise BrokenPipeError(
                                f"capture worker exited with code {process.exitcode}"
                            )
                except TimeoutError:
                    startup_expired = self._awaiting_startup and self.startup_timeout is not None
                    self._reap()
                    if self._terminal_error is not None:
                        raise CaptureError(self._terminal_error) from None
                    if startup_expired:
                        raise TransientCaptureError(
                            f"capture startup timed out after {self.startup_timeout:g} seconds"
                        ) from None
                    raise TransientCaptureError(f"capture timed out after {limit:g} seconds") from None
                except CaptureError:
                    # Reap this attempt. Explicit transient errors reach the
                    # scheduler's paced retry; malformed data stays terminal.
                    self._reap()
                    if self._terminal_error is not None:
                        raise CaptureError(self._terminal_error) from None
                    raise
                except (BrokenPipeError, ConnectionError, EOFError, OSError) as exc:
                    last_failure = str(exc) or type(exc).__name__
                    self._reap()
                    if attempt >= self.max_restarts or time.monotonic() >= deadline:
                        break
            if self._terminal_error is not None:
                raise CaptureError(self._terminal_error)
            raise TransientCaptureError(
                f"capture worker failed after {self.max_restarts + 1} bounded attempt(s): {last_failure}"
            )
        except BaseException:
            # Cancellation can be any token-specific exception.  Never leave a
            # child running after the parent abandons its request.
            self._reap()
            raise
        finally:
            self._capture_guard.release()

    def close(self) -> None:
        if self._closed:
            if self._process is not None and not self._reap():
                raise CaptureError(self._terminal_error or "capture worker could not be stopped")
            return
        self._closed = True
        connection, process = self._connection, self._process
        if connection is not None and process is not None and process.is_alive():
            try:
                connection.send(("close",))
                process.join(0.25)
            except (BrokenPipeError, EOFError, OSError):
                pass
        if not self._reap():
            raise CaptureError(self._terminal_error or "capture worker could not be stopped")

    def __enter__(self) -> "CaptureProcess":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


__all__ = ["CaptureProcess", "DEFAULT_MAX_PIXEL_BYTES"]
