"""Capture-only resource inspection; no session, input adapter, or config writes.

Capture, fixed-row decoding and discovery have independent lanes. Each consumes
only the latest frame; discovery cannot freeze the preview. This monitor does
not authorize automation.
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
from tibia_client.models import Marker, Quality, Reading, Target
from tibia_client.freshness import DEFAULT_STALE_MS, get_stale_ms, validate_stale_ms
from tibia_client.retained_tracker import RetainedResourceTracker, create_reader

FIELD_NAMES = dict(zip(FIELDS, ("HP (Skills)", "Mana (Skills)", "Soul points", "Capacity")))
BUDGET_MS = 100.0
STALE_AFTER = DEFAULT_STALE_MS/1000.0


@dataclass(frozen=True)
class WindowSource:
    """Pickleable, pinned capture target; native imports stay in the child."""

    hwnd: int
    target: Any = None

    def __post_init__(self) -> None:
        if type(self.hwnd) is not int or self.hwnd <= 0:
            raise ValueError("Capture window handle must be a positive integer")

    def capture(self):
        from tibia_client.direct_capture import capture_direct_host_client, capture_direct_target

        if self.target is not None:
            try:
                return capture_direct_target(self.hwnd, self.target)
            except RuntimeError as exc:
                raise CaptureError(str(exc)) from exc
        return capture_direct_host_client(self.hwnd)


def create_capture(hwnd: int | None = None):
    """Read the selected target once per Start, without changing configuration."""
    from tibia_client.capture import CaptureProcess

    size = None
    target = None
    if hwnd is None:
        with scripts_path("conf.json").open("r", encoding="utf-8") as stream:
            config = json.load(stream)
        if config.get("platform") != "windows":
            raise ValueError("Select a Windows capture window first")
        hwnd = config.get("capture_hwnd", config.get("hwnd"))
        size = config.get("capture_size")
        if not size:
            raise ValueError("Configure the capture window first, or use --hwnd HANDLE")
        from tibia_client.window_target import WindowTarget

        selected = WindowTarget.from_config(config)
        if selected.kind == "sandbox":
            if hwnd != config.get("input_hwnd"):
                raise ValueError("Sandbox capture and input must use the same host window")
            target = selected
    return CaptureProcess(WindowSource(hwnd, target), capture_size=size,
                          timeout=2.0, max_restarts=0, startup_timeout=15.0)


def create_visual_reader():
    from tibia_client.visual_readings import VisualReadings
    return VisualReadings()


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
    preview: Sample | None = None
    recovery: str = ""
    visual: Sample | None = None


@dataclass(frozen=True)
class DisplayReading:
    field: str
    value: str
    status: str
    age_ms: float | None
    detail: str
    region: tuple[int, int, int, int] | None = None
    label: str = ""
    group: str = "Resources"


def _display_value(reading):
    value = reading.value
    if isinstance(value, Target):
        return f'{value.count}'+('+' if not value.count_complete else '')+(' · SELECTED' if value.selected else '')
    if isinstance(value, Marker):
        count = reading.diagnostics.get('count', int(value.position is not None))
        return f'{count} found'+(' · CENTER' if value.reached else '')
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        value = value.get('count')
    if type(value) is int and value >= 0:
        return f'{value:,}'+('%' if reading.diagnostics.get('format') == 'percent' else '')
    return None


def display_readings(state: MonitorState, now: float, *, stale_ms=None) -> tuple[DisplayReading, ...]:
    """Never present an old, unknown, errored or malformed value as accepted."""
    stale_ms = get_stale_ms() if stale_ms is None else validate_stale_ms(stale_ms)
    output = []
    names = list(FIELDS)+list(state.visual.readings if state.visual else ())
    for name in names:
        sample = state.sample if name in FIELDS else state.visual
        age = None if sample is None else now-sample.observed_at
        reading = (sample.readings.get(name, Reading(None, Quality.UNKNOWN, 'No reading'))
                   if sample else Reading(None, Quality.UNKNOWN))
        label = reading.diagnostics.get('label', FIELD_NAMES.get(name, name))
        group = reading.diagnostics.get('group', 'Resources')
        value, region = "N/A", None
        if sample is None or not state.running:
            status, detail = state.phase, state.detail
        else:
            raw_region = reading.diagnostics.get("effective_region")
            if raw_region is not None:
                try:
                    region = validate_region(raw_region, sample.frame.size)
                except (TypeError, ValueError, DetectionError):
                    pass
            if age is None or not math.isfinite(age) or not 0 <= age <= stale_ms/1000:
                status, detail = "Stale", f"No result from a capture begun within {stale_ms} ms."
            elif reading.quality is Quality.ERROR:
                status, detail = "Error", reading.detail or "Reader error"
            elif reading.quality is not Quality.KNOWN:
                status, detail = "N/A", reading.detail or "Pixels are not reliably readable"
            else:
                formatted = _display_value(reading)
                if formatted is not None:
                    value, status, detail = formatted, "Accepted", reading.detail
                else:
                    status, detail = "N/A", "Reader did not return a supported value"
        age_ms = age * 1000 if age is not None and math.isfinite(age) and age >= 0 else None
        output.append(DisplayReading(name, value, status, age_ms, detail, region, label, group))
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
        visual_reader_factory: Callable[[], Any] = create_visual_reader,
    ) -> None:
        for name, value in (("interval", interval), ("rediscovery_interval", rediscovery_interval)):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        self._capture_factory, self._reader_factory = capture_factory, reader_factory
        self._interval, self._rediscovery_interval = interval, rediscovery_interval
        self._clock = clock
        self._visual_reader_factory = visual_reader_factory
        self._lock = threading.Lock()
        self._state = MonitorState()
        self._worker: threading.Thread | None = None
        self._token = _Cancellation()
        self._closed = False
        self._decode_worker = None
        self._tracker = None
        self._visual_worker = None
        self._visual_ready = threading.Event()
        self._frame_ready = threading.Event()
        self._layout_epoch = 0
        self._cache_path = None
        if capture_factory is create_capture and reader_factory is create_reader:
            from tibia_client.retained_resources import cache_path
            self._cache_path = cache_path()

    def state(self) -> MonitorState:
        with self._lock:
            return self._state

    @property
    def busy(self) -> bool:
        with self._lock:
            return (any(w is not None and w.is_alive() for w in
                        (self._worker, self._decode_worker, self._visual_worker))
                    or self._tracker is not None and self._tracker.busy)

    def start(self) -> bool:
        with self._lock:
            if self._closed:
                raise RuntimeError("Monitor is closed")
            if (any(w is not None and w.is_alive() for w in
                    (self._worker, self._decode_worker, self._visual_worker))
                    or self._tracker is not None and self._tracker.busy):
                return False
            self._token = _Cancellation()
            self._frame_ready.clear()
            self._visual_ready.clear()
            self._tracker = RetainedResourceTracker(
                self._reader_factory, clock=self._clock,
                rediscovery_interval=self._rediscovery_interval, cache_path=self._cache_path)
            self._layout_epoch += 1
            self._state = MonitorState("Starting", detail="Starting capture…", running=True)
            self._worker = threading.Thread(
                target=self._run, args=(self._token,), name="resource-monitor", daemon=True
            )
            self._worker.start()
            return True

    def stop(self) -> None:
        with self._lock:
            self._token.event.set()
            self._frame_ready.set()
            self._visual_ready.set()
            if self._tracker is not None:
                self._tracker.close()
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
        deadline = time.monotonic() + timeout
        for worker in (self._worker, self._decode_worker, self._visual_worker):
            if worker is not None:
                worker.join(max(0, deadline-time.monotonic()))
        if self._tracker is not None:
            self._tracker.wait(max(0, deadline-time.monotonic()))
        return not self.busy

    def recalibrate(self):
        with self._lock:
            self._layout_epoch += 1
            if self._tracker is not None:
                self._tracker.reset()
            self._frame_ready.set()
            self._visual_ready.set()
            self._state = replace(self._state, sample=None, visual=None)

    def _visual_decode(self, token):
        reader = None
        sequence, epoch = 0, -1
        try:
            while not token.event.is_set():
                self._visual_ready.wait(.2)
                self._visual_ready.clear()
                token.checkpoint()
                with self._lock:
                    latest = self._state.preview
                    current_epoch = self._layout_epoch
                if latest is None or latest.sequence == sequence:
                    continue
                if epoch != current_epoch:
                    if reader is not None:
                        reader.close()
                    reader = self._visual_reader_factory()
                    epoch = current_epoch
                sequence = latest.sequence
                started = self._clock()
                try:
                    readings = reader.read(latest.frame)
                except Exception as exc:
                    readings = {'visual_error': Reading(None, Quality.ERROR, f'{type(exc).__name__}: {exc}',
                                {'label': 'Visual reader', 'group': 'Diagnostics'})}
                finished = self._clock()
                sample = Sample(sequence, latest.frame, readings, latest.observed_at, latest.capture_ms,
                                0, (finished-started)*1000, (finished-latest.observed_at)*1000)
                self._publish(token, _epoch=epoch, visual=sample)
        except _Stopped:
            pass
        except Exception as exc:
            self._publish(token, recovery=f'Visual reader: {exc}')
        finally:
            if reader is not None:
                reader.close()

    def _decode(self, token):
        tracker = self._tracker
        try:
            sequence = 0
            while not token.event.is_set():
                self._frame_ready.wait(.2)
                self._frame_ready.clear()
                token.checkpoint()
                with self._lock:
                    latest = self._state.preview
                    sample_epoch = self._layout_epoch
                if latest is None or latest.sequence == sequence:
                    continue
                sequence, frame = latest.sequence, latest.frame
                started = self._clock()
                readings = tracker.read(frame)
                finished = self._clock()
                sample = Sample(sequence, frame, readings, latest.observed_at, latest.capture_ms,
                                tracker.discovery_ms, (finished-started)*1000,
                                (finished-latest.observed_at)*1000)
                good = sum(r.quality is Quality.KNOWN for r in readings.values())
                self._publish(token, _epoch=sample_epoch, sample=sample, phase="Monitoring",
                    recovery=tracker.detail,
                    detail=f"{good}/4 fields read · saved layout" if tracker.has_layout else "Locating panel; preview is live")
        except _Stopped:
            pass
        except Exception as exc:
            self._publish(token, recovery=f"Reader error: {type(exc).__name__}: {exc}")
        finally:
            tracker.close()

    def _publish(self, token: _Cancellation, *, _epoch=None, **changes) -> None:
        with self._lock:
            if not token.event.is_set() and not self._closed:
                if _epoch is not None and _epoch != self._layout_epoch:
                    return
                if self._state.preview is None:
                    # A capture failure must not be overwritten by a decoder
                    # that finishes an older frame afterwards.
                    if any(changes.get(key) is not None for key in ('sample', 'visual')):
                        return
                self._state = replace(self._state, **changes)

    def _run(self, token: _Cancellation) -> None:
        capture = None
        try:
            capture = self._capture_factory()
            source = getattr(capture, "provider", None)
            hwnd = getattr(source, "hwnd", None)
            self._publish(token, source=f"Capture window: {hwnd}" if hwnd else "Injected capture")
            sequence = 0
            self._decode_worker = threading.Thread(target=self._decode, args=(token,),
                                                   name="resource-decode", daemon=True)
            self._decode_worker.start()
            self._visual_worker = threading.Thread(target=self._visual_decode, args=(token,),
                                                   name='visual-readings', daemon=True)
            self._visual_worker.start()
            expected_size = None
            while not token.event.is_set():
                begun = self._clock()
                try:
                    frame = capture.capture(token=token)
                except CaptureError as exc:
                    token.checkpoint()
                    self._publish(token, phase="Waiting for capture", sample=None, visual=None, preview=None,
                                  detail=str(exc), recovery="")
                    capture.close()
                    if token.event.wait(.25):
                        break
                    capture = self._capture_factory()
                    continue
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
                sequence += 1
                preview = Sample(sequence, frame, {}, observed_at, (captured-begun)*1000, 0, 0, 0)
                self._publish(token, preview=preview)
                self._frame_ready.set()
                self._visual_ready.set()
                token.event.wait(max(0.0, self._interval - (self._clock() - begun)))
        except _Stopped:
            pass
        except Exception as exc:
            self._publish(token, phase="Error", sample=None, visual=None, preview=None, detail=str(exc), running=False)
        finally:
            token.event.set()
            if self._tracker is not None:
                self._tracker.close()
            self._frame_ready.set()
            self._visual_ready.set()
            if capture is not None:
                try:
                    capture.close()
                except Exception as exc:
                    with self._lock:
                        self._state = MonitorState(
                            "Error", detail=f"Capture cleanup failed: {exc}",
                            source=self._state.source,
                        )
