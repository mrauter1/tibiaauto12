"""Capture-only resource inspection; no session, input adapter, or config writes.

One worker owns capture, discovery and decoding. Only its latest result is
retained. Presentation ages results independently, including while discovery
or capture is slow. This monitor does not authorize automation.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import json
import math
import threading
import time
from types import MappingProxyType
from typing import Any, Callable

from core.paths import scripts_path
from core.perception import CaptureError, DetectionError, Frame, validate_region
from tibia_client.adaptive import FIELDS
from tibia_client.models import Quality, Reading

FIELD_NAMES = dict(zip(FIELDS, ("HP", "Mana", "Soul", "Capacity")))
BUDGET_MS = 100.0
STALE_AFTER = 0.5


@dataclass(frozen=True)
class WindowSource:
    """Pickleable, pinned capture target; native imports stay in the child."""

    hwnd: int

    def __post_init__(self) -> None:
        if type(self.hwnd) is not int or self.hwnd <= 0:
            raise ValueError("Capture window handle must be a positive integer")

    def capture(self):
        import win32gui
        from core.perception import _capture_windows

        if not win32gui.IsWindow(self.hwnd) or win32gui.IsIconic(self.hwnd):
            raise CaptureError("Selected capture window is closed or minimized")
        return _capture_windows(self.hwnd)


def create_capture(hwnd: int | None = None):
    """Read the selected target once per Start, without changing configuration."""
    from tibia_client.capture import CaptureProcess

    size = None
    if hwnd is None:
        with scripts_path("conf.json").open("r", encoding="utf-8") as stream:
            config = json.load(stream)
        if config.get("platform") != "windows":
            raise ValueError("Select a Windows capture window first")
        hwnd = config.get("capture_hwnd", config.get("hwnd"))
        size = config.get("capture_size")
        if not size:
            raise ValueError("Configure the capture window first, or use --hwnd HANDLE")
    return CaptureProcess(WindowSource(hwnd), capture_size=size, timeout=2.0, max_restarts=0)


def create_reader():
    from tibia_client.resource_reader import ResourceReader

    return ResourceReader()


@dataclass(frozen=True)
class Sample:
    sequence: int
    frame: Frame
    readings: Mapping[str, Reading]
    observed_at: float
    capture_ms: float
    discovery_ms: float
    decode_ms: float
    total_ms: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "readings", MappingProxyType(dict(self.readings)))


@dataclass(frozen=True)
class MonitorState:
    phase: str = "Stopped"
    sample: Sample | None = None
    detail: str = "Start monitoring to capture the selected window."
    source: str = ""
    running: bool = False


@dataclass(frozen=True)
class DisplayReading:
    field: str
    value: str
    status: str
    age_ms: float | None
    detail: str
    region: tuple[int, int, int, int] | None = None


def display_readings(state: MonitorState, now: float) -> tuple[DisplayReading, ...]:
    """Never present an old, unknown, errored or malformed value as accepted."""
    sample = state.sample
    age = None if sample is None else now - sample.observed_at
    output = []
    for name in FIELDS:
        value, region = "N/A", None
        if sample is None or not state.running:
            status, detail = state.phase, state.detail
        else:
            reading = sample.readings.get(name, Reading(None, Quality.UNKNOWN, "No reading"))
            raw_region = reading.diagnostics.get("effective_region")
            if raw_region is not None:
                try:
                    region = validate_region(raw_region, sample.frame.size)
                except (TypeError, ValueError, DetectionError):
                    pass
            if age is None or not math.isfinite(age) or not 0 <= age <= STALE_AFTER:
                status, detail = "Stale", "No result from a capture begun within 500 ms."
            elif reading.quality is Quality.ERROR:
                status, detail = "Error", reading.detail or "Reader error"
            elif reading.quality is not Quality.KNOWN:
                status, detail = "N/A", reading.detail or "Pixels are not reliably readable"
            else:
                count = reading.value.get("count") if isinstance(reading.value, Mapping) else None
                if type(count) is int and count >= 0:
                    value, status, detail = f"{count:,}", "Accepted", reading.detail
                else:
                    status, detail = "N/A", "Reader did not return a supported integer"
        age_ms = age * 1000 if age is not None and math.isfinite(age) and age >= 0 else None
        output.append(DisplayReading(name, value, status, age_ms, detail, region))
    return tuple(output)


class _Stopped(Exception):
    pass


class _Cancellation:
    def __init__(self) -> None:
        self.event = threading.Event()

    def checkpoint(self) -> None:
        if self.event.is_set():
            raise _Stopped()


class ResourceMonitor:
    """One bounded capture lane, no queued frames and no input capabilities."""

    def __init__(
        self,
        capture_factory: Callable[[], Any] = create_capture,
        reader_factory: Callable[[], Any] = create_reader,
        *,
        interval: float = 0.1,
        rediscovery_interval: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        for name, value in (("interval", interval), ("rediscovery_interval", rediscovery_interval)):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        self._capture_factory, self._reader_factory = capture_factory, reader_factory
        self._interval, self._rediscovery_interval = interval, rediscovery_interval
        self._clock = clock
        self._lock = threading.Lock()
        self._state = MonitorState()
        self._worker: threading.Thread | None = None
        self._token = _Cancellation()
        self._closed = False

    def state(self) -> MonitorState:
        with self._lock:
            return self._state

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self._closed:
                raise RuntimeError("Monitor is closed")
            if self._worker is not None and self._worker.is_alive():
                return False
            self._token = _Cancellation()
            self._state = MonitorState("Starting", detail="Preparing the reader…", running=True)
            self._worker = threading.Thread(
                target=self._run, args=(self._token,), name="resource-monitor", daemon=True
            )
            self._worker.start()
            return True

    def stop(self) -> None:
        with self._lock:
            self._token.event.set()
            self._state = MonitorState(
                "Stopped", detail="Monitoring stopped. Previous readings were cleared.",
                source=self._state.source,
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.stop()

    def wait(self, timeout: float = 3.0) -> bool:
        """Bounded shutdown wait for command-line/application teardown."""
        with self._lock:
            worker = self._worker
        if worker is not None:
            worker.join(timeout)
        return not self.busy

    def _publish(self, token: _Cancellation, **changes) -> None:
        with self._lock:
            if not token.event.is_set() and not self._closed:
                self._state = replace(self._state, **changes)

    def _run(self, token: _Cancellation) -> None:
        capture = None
        try:
            reader = self._reader_factory()
            token.checkpoint()
            capture = self._capture_factory()
            source = getattr(capture, "provider", None)
            hwnd = getattr(source, "hwnd", None)
            self._publish(token, source=f"Capture window: {hwnd}" if hwnd else "Injected capture")
            layout, sequence, next_discovery = None, 0, 0.0
            rescan = True
            expected_size = None
            while not token.event.is_set():
                begun = self._clock()
                self._publish(token, phase="Capturing", detail="Waiting for a new capture…")
                frame = capture.capture(token=token)
                captured = self._clock()
                token.checkpoint()
                if not isinstance(frame, Frame):
                    raise CaptureError("Capture did not return an immutable frame")
                if expected_size is not None and frame.size != expected_size:
                    raise CaptureError("Capture size changed. Stop and restart for the new size.")
                expected_size = frame.size
                if not frame.pixels.any():
                    raise CaptureError("Capture is all black; readings are unavailable")
                observed_at = getattr(capture, "last_observed_at", None)
                if observed_at is None:
                    observed_at = begun
                if not math.isfinite(observed_at) or not begun <= observed_at <= captured:
                    raise CaptureError("Capture returned an invalid observation timestamp")
                discovery_ms, detail = 0.0, ""
                if rescan and captured >= next_discovery:
                    self._publish(token, phase="Searching", detail="Locating visible resource rows…")
                    started = self._clock()
                    discovered = reader.discover(frame, use_ocr=False, checkpoint=token.checkpoint)
                    discovery_ms = (self._clock() - started) * 1000
                    token.checkpoint()
                    layout, detail = discovered.layout, discovered.detail
                    next_discovery = self._clock() + self._rediscovery_interval
                self._publish(token, phase="Reading", detail="Reading the current capture…")
                started = self._clock()
                readings = (
                    reader.read(frame, layout, use_ocr=False) if layout is not None else
                    {name: Reading(None, Quality.UNKNOWN, detail or "Searching for a unique panel")
                     for name in FIELDS}
                )
                finished = self._clock()
                token.checkpoint()
                # A missing/hidden row may become visible without moving the other
                # rows. Retry discovery at a bounded rate, never borrow old values.
                rescan = any(
                    name not in readings or readings[name].quality is not Quality.KNOWN
                    for name in FIELDS
                )
                sequence += 1
                sample = Sample(
                    sequence, frame, readings, observed_at, (captured - begun) * 1000,
                    discovery_ms, (finished - started) * 1000, (finished - begun) * 1000,
                )
                self._publish(token, phase="Monitoring", sample=sample, detail="")
                token.event.wait(max(0.0, self._interval - (self._clock() - begun)))
        except _Stopped:
            pass
        except Exception as exc:
            self._publish(token, phase="Error", sample=None, detail=str(exc), running=False)
        finally:
            if capture is not None:
                try:
                    capture.close()
                except Exception as exc:
                    with self._lock:
                        self._state = MonitorState(
                            "Error", detail=f"Capture cleanup failed: {exc}",
                            source=self._state.source,
                        )
