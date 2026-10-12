"""Cooperative worker lifecycle and serialized input primitives.

Workers are identified by name.  At most one generation for a name is allowed
to run at a time; if a restart is requested while an old generation is still
stopping, the replacement starts only after the old thread exits.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import math
from numbers import Real
import threading
import time
import traceback
from typing import Callable, Dict, Iterator, Optional


class CancelledError(Exception):
    """Raised at a cooperative cancellation point."""


class CancellationToken:
    """Cancellation state belonging to one worker generation."""

    def __init__(self, name: str, generation: int) -> None:
        self.name = name
        self.generation = generation
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()

    def checkpoint(self) -> None:
        if self.cancelled:
            raise CancelledError(
                f"worker {self.name!r} generation {self.generation} was cancelled"
            )

    def wait(self, seconds: float) -> None:
        """Wait for *seconds*, waking immediately if this token is cancelled."""
        seconds = _duration(seconds, "seconds")
        self.checkpoint()
        if self._cancelled.wait(seconds):
            self.checkpoint()
        self.checkpoint()


@dataclass
class _WorkerRecord:
    name: str
    generation: int = 0
    target: Optional[Callable[[], None]] = None
    thread: Optional[threading.Thread] = None
    token: Optional[CancellationToken] = None
    pending: Optional[tuple[int, Callable[[], None]]] = None
    error: Optional[BaseException] = None
    traceback: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    target_done: Optional[threading.Event] = None
    phase: str = "Stopped"
    lock: threading.RLock = field(default_factory=threading.RLock)


_registry: Dict[str, _WorkerRecord] = {}
_registry_lock = threading.RLock()
_worker_local = threading.local()
_input_lock = threading.RLock()


def _duration(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a real number")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return value


def current_token() -> Optional[CancellationToken]:
    """Return the token for the calling managed worker, if any."""
    return getattr(_worker_local, "token", None)


@contextmanager
def use_token(token) -> Iterator[None]:
    """Temporarily expose *token* through the legacy runtime helpers.

    SDK actions have cancellation scopes narrower than their shared worker.
    Adapters receive that token directly, while this binding also makes older
    input facades which call :func:`checkpoint` observe the same scope.
    Nested bindings restore the previous worker token on every exit path.
    """
    previous_token = getattr(_worker_local, "token", None)
    previous_name = getattr(_worker_local, "name", None)
    _worker_local.token = token
    _worker_local.name = getattr(token, "name", previous_name)
    try:
        yield
    finally:
        _worker_local.token = previous_token
        _worker_local.name = previous_name


def checkpoint() -> None:
    """Raise :class:`CancelledError` if the current worker was cancelled."""
    token = current_token()
    if token is not None:
        token.checkpoint()


def set_phase(phase: str) -> bool:
    """Set the current managed worker's user-visible phase.

    Returns ``False`` when called outside a managed worker. A cancelled or
    superseded generation cannot overwrite the phase of its replacement.
    """
    if not isinstance(phase, str) or not phase.strip():
        raise ValueError("phase must be a non-empty string")
    token = current_token()
    if token is None:
        return False
    token.checkpoint()
    with _registry_lock:
        record = _registry.get(token.name)
        if record is None or record.token is not token:
            return False
        record.phase = phase.strip()
        return True


def wait(seconds: float) -> None:
    """Sleep cooperatively when called by a managed worker."""
    seconds = _duration(seconds, "seconds")
    token = current_token()
    if token is None:
        time.sleep(seconds)
        return
    token.wait(seconds)


def _launch_locked(
    record: _WorkerRecord,
    generation: int,
    target: Callable[[], None],
) -> threading.Thread:
    token = CancellationToken(record.name, generation)
    target_done = threading.Event()

    def run() -> None:
        _worker_local.token = token
        _worker_local.name = record.name
        try:
            token.checkpoint()
            target()
        except CancelledError:
            # Cooperative cancellation is normal worker termination.
            pass
        except BaseException as exc:
            with _registry_lock:
                if record.thread is threading.current_thread():
                    record.error = exc
                    record.traceback = traceback.format_exc()
                    record.phase = "Failed"
        finally:
            target_done.set()
            _worker_local.token = None
            _worker_local.name = None
            with _registry_lock:
                if record.thread is threading.current_thread():
                    record.finished_at = time.monotonic()
                    if record.error is None:
                        record.phase = "Stopped"
                    pending = record.pending
                    record.pending = None
                    if pending is not None and pending[0] == record.generation:
                        _launch_locked(record, pending[0], pending[1])

    thread = threading.Thread(
        target=run,
        name=f"{record.name}-{generation}",
        daemon=True,
    )
    record.generation = generation
    record.target = target
    record.thread = thread
    record.token = token
    record.error = None
    record.traceback = None
    record.started_at = time.monotonic()
    record.finished_at = None
    record.target_done = target_done
    record.phase = "Starting"
    # Publish and start under the registry lock. Other lifecycle operations
    # therefore never observe an unstarted, non-joinable Thread reservation.
    thread.start()
    return thread


def start(name: str, target: Callable[[], None]) -> threading.Thread:
    """Start *target* for *name*, or return its already active worker.

    When the previous generation is cancelled but has not exited yet, the new
    generation is queued and launched by the old worker's finalizer.  This
    prevents rapid stop/start cycles from briefly running two workers with the
    same name.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("worker name must be a non-empty string")
    if not callable(target):
        raise TypeError("worker target must be callable")

    with _registry_lock:
        record = _registry.setdefault(name, _WorkerRecord(name=name))
        active = record.thread
        if active is not None and active.is_alive():
            if (record.token is not None and not record.token.cancelled
                    and record.target_done is not None
                    and not record.target_done.is_set()):
                return active
            record.generation += 1
            record.target = target
            record.pending = (record.generation, target)
            return active

        generation = record.generation + 1
        return _launch_locked(record, generation, target)


def cancel(name: str, timeout: float = 0.5, *, forget: bool = False) -> bool:
    """Cancel the current generation and wait at most *timeout* seconds.

    Returns ``True`` if no worker remains alive when the bounded wait ends.
    Pending restarts are invalidated.  ``forget`` also discards the remembered
    target once the active thread has stopped.
    """
    timeout = _duration(timeout, "timeout")
    with _registry_lock:
        record = _registry.get(name)
        if record is None:
            return True
        record.generation += 1
        record.pending = None
        if forget:
            record.target = None
        token = record.token
        thread = record.thread
        if token is not None:
            token.cancel()

    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout)
    stopped = thread is None or not thread.is_alive()

    if stopped and forget:
        with _registry_lock:
            if _registry.get(name) is record and (
                record.thread is None or not record.thread.is_alive()
            ):
                _registry.pop(name, None)
    return stopped


def stop_all(timeout: float = 1.0) -> bool:
    """Cancel every worker and spend at most *timeout* seconds joining them."""
    timeout = _duration(timeout, "timeout")
    with _registry_lock:
        records = list(_registry.values())
        for record in records:
            record.generation += 1
            record.pending = None
            if record.token is not None:
                record.token.cancel()
        threads = [
            record.thread
            for record in records
            if record.thread is not None
            and record.thread is not threading.current_thread()
        ]

    deadline = time.monotonic() + timeout
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(remaining)
    return all(not thread.is_alive() for thread in threads)


def status(name: str) -> Optional[dict]:
    """Return a snapshot of one worker, including any uncaught exception."""
    with _registry_lock:
        record = _registry.get(name)
        if record is None:
            return None
        thread = record.thread
        token = record.token
        return {
            "name": record.name,
            "generation": record.generation,
            "alive": bool(thread and thread.is_alive()),
            "cancelled": bool(token and token.cancelled),
            "pending_restart": record.pending is not None,
            "error": record.error,
            "traceback": record.traceback,
            "phase": record.phase,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "thread": thread,
        }


@contextmanager
def input_transaction(reason: Optional[str] = None) -> Iterator[None]:
    """Serialize one complete input action or sequence.

    Lock acquisition remains cancellable for managed workers.  ``reason`` is
    accepted for diagnostics at call sites and intentionally has no scheduling
    semantics.
    """
    del reason
    checkpoint()
    acquired = False
    try:
        while not acquired:
            acquired = _input_lock.acquire(timeout=0.05)
            if not acquired:
                checkpoint()
        checkpoint()
        yield
        checkpoint()
    finally:
        if acquired:
            _input_lock.release()


__all__ = [
    "CancelledError",
    "CancellationToken",
    "cancel",
    "checkpoint",
    "current_token",
    "input_transaction",
    "set_phase",
    "start",
    "status",
    "stop_all",
    "use_token",
    "wait",
]
