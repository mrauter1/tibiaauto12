"""Non-blocking runtime ownership for adaptive, screen-only resource layouts.

Discovery may inspect a large part of a frame, so it never runs on the client
scheduler thread.  A completed discovery is accepted only after the proposed
layout reads successfully from the current frame.  Published readings are
therefore always derived from the current observation frame, never from a
worker's older frame.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
import math
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping

from core.perception import Frame, validate_region
from tibia_client.models import Quality, Reading, Sensor


ReaderFactory = Callable[[str], Any]


def bounded_adaptive_count(reading: Reading, max_digits: int) -> Reading:
    """Apply the operator's count-width policy to one adaptive field."""

    if reading.quality is not Quality.KNOWN:
        return reading
    value = reading.value
    count = value.get("count") if isinstance(value, Mapping) else None
    if (
        type(max_digits) is not int
        or max_digits <= 0
        or type(count) is not int
        or count < 0
        or len(str(count)) > max_digits
    ):
        diagnostics = dict(reading.diagnostics)
        diagnostics.update({"configured_max_digits": max_digits, "observed_count": count})
        return Reading(
            None,
            Quality.UNKNOWN,
            "adaptive count exceeds the configured digit limit",
            diagnostics,
        )
    return reading


@dataclass(frozen=True, slots=True)
class CaptureInputMapping:
    """A pure mapping between capture-local and input-client coordinates.

    The desktop compatibility gate currently permits only ``identity``.  The
    general geometry is kept here so capture coordinates do not silently
    become input coordinates in detector code.
    """

    capture_size: tuple[int, int]
    input_size: tuple[int, int]

    def __post_init__(self) -> None:
        for label, size in (("capture_size", self.capture_size), ("input_size", self.input_size)):
            try:
                pair = tuple(size)
            except TypeError:
                raise ValueError(f"{label} must contain two positive integers") from None
            if len(pair) != 2 or any(type(item) is not int or item <= 0 for item in pair):
                raise ValueError(f"{label} must contain two positive integers")
            object.__setattr__(self, label, pair)

    @property
    def identity(self) -> bool:
        return self.capture_size == self.input_size

    def capture_to_input(self, point: tuple[int, int]) -> tuple[int, int]:
        try:
            x, y = tuple(point)
        except (TypeError, ValueError):
            raise ValueError("capture point must contain two non-negative integers") from None
        if any(type(value) is not int or value < 0 for value in (x, y)):
            raise ValueError("capture point must contain two non-negative integers")
        capture_width, capture_height = self.capture_size
        if x >= capture_width or y >= capture_height:
            raise ValueError("capture point is outside the capture client area")
        input_width, input_height = self.input_size
        # Map pixel centers, preserving exact integer coordinates for 1:1.
        mapped_x = int((x + 0.5) * input_width / capture_width)
        mapped_y = int((y + 0.5) * input_height / capture_height)
        return min(mapped_x, input_width - 1), min(mapped_y, input_height - 1)


def _default_reader_factory(reader_id: str) -> Any:
    from tibia_client.adaptive import ResourceReader

    if reader_id != "paulistinha-skills-v1":
        raise ValueError(f"unsupported adaptive reader {reader_id!r}")
    return ResourceReader()


@dataclass(slots=True)
class _Group:
    reader: Any | None = None
    layout: Any | None = None
    last_bounds: tuple[int, int, int, int] | None = None
    ticket: int = 0
    ocr_ticket: int = 0
    # Discovery and OCR each have one bounded lane. A stale, potentially slow
    # OCR subprocess must never delay high-priority layout reacquisition.
    worker: threading.Thread | None = None
    ocr_worker: threading.Thread | None = None
    completed: tuple[
        int,
        Any | None,
        str,
        tuple[int, int, int, int] | None,
        frozenset[str],
    ] | None = None
    ocr_completed: tuple[int, str, dict[str, str], Mapping[str, Reading]] | None = None
    ocr_cache: OrderedDict[tuple[str, str, str], Reading] = field(default_factory=OrderedDict)
    ocr_retry: OrderedDict[tuple[str, str, str], tuple[int, float]] = field(
        default_factory=OrderedDict
    )
    initial_bounds: tuple[int, int, int, int] | None = None
    force_global: bool = False
    next_attempt_at: float = 0.0
    failures: int = 0
    last_failure_detail: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


class AdaptiveResourceTracker:
    """Track adaptive layouts for one adapter without blocking its hot path."""

    def __init__(
        self,
        reader_factory: ReaderFactory | None = None,
        *,
        retry_interval: float = 0.5,
        max_retry_interval: float = 8.0,
        ocr_cache_size: int = 32,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(retry_interval, bool)
            or not isinstance(retry_interval, (int, float))
            or not math.isfinite(retry_interval)
            or retry_interval < 0
        ):
            raise ValueError("retry_interval must be a non-negative number")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if (
            isinstance(max_retry_interval, bool)
            or not isinstance(max_retry_interval, (int, float))
            or not math.isfinite(max_retry_interval)
            or max_retry_interval < retry_interval
        ):
            raise ValueError("max_retry_interval must be at least retry_interval")
        if type(ocr_cache_size) is not int or ocr_cache_size <= 0:
            raise ValueError("ocr_cache_size must be a positive integer")
        self._reader_factory = reader_factory or _default_reader_factory
        self._retry_interval = float(retry_interval)
        self._max_retry_interval = float(max_retry_interval)
        self._ocr_cache_size = ocr_cache_size
        self._clock = clock
        self._groups: dict[tuple[str, bool], _Group] = {}
        # Slots outlive reset generations, so rapid reconfiguration cannot
        # accumulate workers while an old OCR subprocess is still draining.
        self._discovery_slot = threading.BoundedSemaphore(1)
        self._ocr_slot = threading.BoundedSemaphore(1)
        self._closed = False
        self._generation = 0

    def close(self) -> None:
        self._closed = True
        self._generation += 1
        self._groups.clear()

    def reset(self) -> None:
        self.close()
        self._closed = False

    @staticmethod
    def _unknown(
        detail: str, reader_id: str, diagnostics: Mapping[str, Any] | None = None
    ) -> Reading:
        evidence = {"type": "adaptive_layout", "reader": reader_id}
        if diagnostics:
            evidence.update(diagnostics)
        return Reading(
            None,
            Quality.UNKNOWN,
            detail,
            evidence,
        )

    @staticmethod
    def _initial_bounds(
        items: list[tuple[Sensor, Any]], frame: Frame
    ) -> tuple[int, int, int, int] | None:
        regions: list[tuple[int, int, int, int]] = []
        for sensor, _metadata in items:
            if len(sensor.region) != 4:
                return None
            try:
                regions.append(validate_region(sensor.region, frame.size))
            except Exception:
                return None
        if not regions:
            return None
        candidate = (
            min(region[0] for region in regions),
            min(region[1] for region in regions),
            max(region[2] for region in regions),
            max(region[3] for region in regions),
        )
        try:
            return validate_region(candidate, frame.size)
        except Exception:
            return None

    @staticmethod
    def _selection(
        bounds: tuple[int, int, int, int] | None, frame: Frame
    ) -> tuple[int, int, int, int] | None:
        if bounds is None:
            return None
        left, top, right, bottom = bounds
        margin = max(24, (right - left) // 3, (bottom - top) // 3)
        candidate = (
            max(0, left - margin),
            max(0, top - margin),
            min(frame.width, right + margin),
            min(frame.height, bottom + margin),
        )
        try:
            return validate_region(candidate, frame.size)
        except Exception:
            return None

    def _start_discovery(
        self,
        reader_id: str,
        use_ocr: bool,
        group: _Group,
        frame: Frame,
        *,
        required_fields: frozenset[str] = frozenset(),
    ) -> None:
        if self._closed or (group.worker is not None and group.worker.is_alive()):
            return
        now = float(self._clock())
        if now < group.next_attempt_at:
            return
        if not self._discovery_slot.acquire(blocking=False):
            return
        group.next_attempt_at = now + self._retry_interval
        group.ticket += 1
        ticket = group.ticket
        generation = self._generation
        hint = group.last_bounds or group.initial_bounds
        selection = None if group.force_global else self._selection(hint, frame)
        group.force_global = False

        def run() -> None:
            # Keep the local defined even when construction itself fails so
            # the failure detail can be published and rate-limited.
            reader = group.reader
            try:
                if reader is None:
                    reader = self._reader_factory(reader_id)

                def checkpoint() -> None:
                    if self._closed or generation != self._generation or ticket != group.ticket:
                        raise RuntimeError("adaptive discovery was superseded")

                result = reader.discover(
                    frame,
                    selection=selection,
                    use_ocr=use_ocr,
                    checkpoint=checkpoint,
                )
                detail = str(getattr(result, "detail", ""))
            except Exception as exc:
                result = None
                detail = f"adaptive discovery failed: {type(exc).__name__}: {exc}"
            try:
                with group.lock:
                    if (
                        not self._closed
                        and generation == self._generation
                        and ticket == group.ticket
                    ):
                        if reader is not None:
                            group.reader = reader
                        group.completed = (
                            ticket, result, detail, selection, required_fields
                        )
            finally:
                self._discovery_slot.release()

        worker = threading.Thread(
            target=run,
            name=f"adaptive-layout-{reader_id}",
            daemon=True,
        )
        group.worker = worker
        try:
            worker.start()
        except Exception:
            group.worker = None
            self._discovery_slot.release()
            raise

    @staticmethod
    def _roi_hashes(frame: Frame, layout: Any, fields: set[str]) -> dict[str, str]:
        regions = getattr(layout, "regions", {})
        pixels = frame.pixels
        result: dict[str, str] = {}
        for field_name in fields:
            region = regions.get(field_name)
            if region is None:
                continue
            try:
                left, top, right, bottom = validate_region(region, frame.size)
            except Exception:
                continue
            payload = pixels[top:bottom, left:right].tobytes()
            result[field_name] = sha256(payload).hexdigest()
        return result

    def _start_ocr(
        self,
        reader_id: str,
        group: _Group,
        frame: Frame,
        layout: Any,
        hashes: dict[str, str],
    ) -> None:
        if (
            self._closed
            or not hashes
            or (group.ocr_worker is not None and group.ocr_worker.is_alive())
        ):
            return
        if not self._ocr_slot.acquire(blocking=False):
            return
        group.ocr_ticket += 1
        ticket = group.ocr_ticket
        generation = self._generation
        fingerprint = str(layout.fingerprint)

        def run() -> None:
            try:
                # Keep the potentially blocking OCR engine away from both the
                # scheduler and the reader used for current-frame guard checks.
                reader = self._reader_factory(reader_id)
                result = reader.read(frame, layout, use_ocr=True)
                if not isinstance(result, Mapping):
                    raise TypeError("adaptive OCR reader returned invalid readings")
                selected = {
                    field_name: result[field_name]
                    for field_name in hashes
                    if isinstance(result.get(field_name), Reading)
                }
            except Exception as exc:
                selected = {
                    field_name: Reading(
                        None,
                        Quality.UNKNOWN,
                        f"adaptive OCR failed: {type(exc).__name__}: {exc}",
                    )
                    for field_name in hashes
                }
            try:
                with group.lock:
                    if (
                        not self._closed
                        and generation == self._generation
                        and ticket == group.ocr_ticket
                    ):
                        group.ocr_completed = (ticket, fingerprint, hashes, selected)
            finally:
                self._ocr_slot.release()

        worker = threading.Thread(
            target=run,
            name=f"adaptive-ocr-{reader_id}",
            daemon=True,
        )
        group.ocr_worker = worker
        try:
            worker.start()
        except Exception:
            group.ocr_worker = None
            self._ocr_slot.release()
            raise

    def _cache_ocr_completion(self, group: _Group) -> None:
        completed = None
        with group.lock:
            if group.ocr_completed is not None:
                completed, group.ocr_completed = group.ocr_completed, None
        if completed is None:
            return
        _ticket, fingerprint, hashes, readings = completed
        for field_name, roi_hash in hashes.items():
            reading = readings.get(field_name)
            if reading is None:
                continue
            key = fingerprint, field_name, roi_hash
            if reading.quality is Quality.KNOWN:
                group.ocr_cache[key] = reading
                group.ocr_cache.move_to_end(key)
                group.ocr_retry.pop(key, None)
                continue
            failures = group.ocr_retry.get(key, (0, 0.0))[0] + 1
            delay = min(
                self._max_retry_interval,
                self._retry_interval * (2 ** max(0, failures - 1)),
            )
            group.ocr_retry[key] = (failures, float(self._clock()) + delay)
            group.ocr_retry.move_to_end(key)
        while len(group.ocr_cache) > self._ocr_cache_size:
            group.ocr_cache.popitem(last=False)
        while len(group.ocr_retry) > self._ocr_cache_size:
            group.ocr_retry.popitem(last=False)

    @classmethod
    def _eligible_ocr_hashes(
        cls,
        frame: Frame,
        layout: Any,
        readings: Mapping[str, Reading],
        fields: set[str],
    ) -> dict[str, str]:
        """Hash only fields whose current numeric geometry was reverified."""

        fingerprint = str(getattr(layout, "fingerprint"))
        regions = getattr(layout, "regions", {})
        eligible: set[str] = set()
        for field_name in fields:
            reading = readings.get(field_name)
            expected_region = regions.get(field_name)
            if (
                reading is None
                or reading.quality is not Quality.UNKNOWN
                or reading.diagnostics.get("numeric_eligible") is not True
                or str(reading.diagnostics.get("layout_fingerprint")) != fingerprint
                or expected_region is None
            ):
                continue
            try:
                effective = tuple(reading.diagnostics.get("effective_region", ()))
                expected = tuple(expected_region)
            except TypeError:
                continue
            if effective == expected:
                eligible.add(field_name)
        return cls._roi_hashes(frame, layout, eligible)

    @staticmethod
    def _read_current(group: _Group, frame: Frame, layout: Any) -> Mapping[str, Reading]:
        if group.reader is None:
            raise RuntimeError("adaptive reader is not initialized")
        result = group.reader.read(frame, layout, use_ocr=False)
        if not isinstance(result, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, Reading)
            for key, value in result.items()
        ):
            raise TypeError("adaptive reader returned invalid readings")
        return result

    @staticmethod
    def _validated(readings: Mapping[str, Reading]) -> bool:
        return any(reading.quality is Quality.KNOWN for reading in readings.values())

    @classmethod
    def _layout_valid(
        cls, group: _Group, frame: Frame, layout: Any, readings: Mapping[str, Reading]
    ) -> bool:
        if cls._validated(readings):
            return True
        validate = getattr(group.reader, "validate", None)
        if callable(validate):
            return bool(validate(frame, layout))
        return False

    @staticmethod
    def _decorate(reading: Reading, reader_id: str, layout: Any) -> Reading:
        diagnostics = dict(reading.diagnostics)
        bounds = tuple(getattr(layout, "bounds"))
        fingerprint = str(getattr(layout, "fingerprint"))
        diagnostics.update(
            {
                "adaptive_reader": reader_id,
                "layout_revision": fingerprint,
                "layout_fingerprint": fingerprint,
                "layout_bounds": bounds,
                "layout_scale": float(getattr(layout, "scale")),
            }
        )
        value = reading.value
        if isinstance(value, Mapping):
            position = value.get("position")
            if position is not None:
                diagnostics["capture_position"] = tuple(position)
        return Reading(value, reading.quality, reading.detail, diagnostics)

    def _group(self, reader_id: str, use_ocr: bool) -> _Group:
        key = reader_id, use_ocr
        found = self._groups.get(key)
        if found is None:
            found = _Group()
            self._groups[key] = found
        return found

    def read(
        self,
        frame: Frame,
        requests: Mapping[Sensor, Any],
    ) -> Mapping[Sensor, Reading]:
        """Read adaptive sensors from ``frame`` and schedule discovery if needed."""

        if self._closed:
            return MappingProxyType(
                {
                    sensor: self._unknown("adaptive tracker is closed", metadata.reader)
                    for sensor, metadata in requests.items()
                }
            )
        grouped: dict[tuple[str, bool], list[tuple[Sensor, Any]]] = {}
        for sensor, metadata in requests.items():
            grouped.setdefault((metadata.reader, bool(metadata.use_ocr)), []).append(
                (sensor, metadata)
            )
        output: dict[Sensor, Reading] = {}
        for (reader_id, use_ocr), items in grouped.items():
            try:
                group = self._group(reader_id, use_ocr)
                if group.initial_bounds is None:
                    group.initial_bounds = self._initial_bounds(items, frame)
            except Exception as exc:
                detail = f"adaptive reader unavailable: {type(exc).__name__}: {exc}"
                output.update((sensor, self._unknown(detail, reader_id)) for sensor, _ in items)
                continue

            completed = None
            accepted_readings: Mapping[str, Reading] | None = None
            with group.lock:
                if group.completed is not None:
                    completed, group.completed = group.completed, None
            if completed is not None:
                _ticket, discovery, detail, selection, required_fields = completed
                candidate = None if discovery is None else getattr(discovery, "layout", None)
                if candidate is not None and not required_fields.issubset(
                    getattr(candidate, "regions", {})
                ):
                    candidate = None
                    detail = (
                        "resource layout still omits requested fields: "
                        + ", ".join(sorted(required_fields))
                    )
                if candidate is None:
                    group.last_failure_detail = detail or "adaptive layout was not found"
                    group.failures += 1
                    delay = min(
                        self._max_retry_interval,
                        self._retry_interval * (2 ** max(0, group.failures - 1)),
                    )
                    group.next_attempt_at = max(group.next_attempt_at, float(self._clock()) + delay)
                    # A saved-region miss gets one global attempt next.  Once
                    # a previously accepted layout is lost, an ambiguous
                    # global result must remain global: narrowing back to the
                    # old bounds could silently select a different duplicate
                    # panel that happens to occupy the old location.
                    ambiguous = bool(
                        discovery is not None
                        and getattr(discovery, "candidates", ())
                    )
                    group.force_global = selection is not None or ambiguous
                else:
                    try:
                        checked = self._read_current(group, frame, candidate)
                    except Exception:
                        checked = {}
                    if self._layout_valid(group, frame, candidate, checked):
                        previous_fingerprint = (
                            None
                            if group.layout is None
                            else str(group.layout.fingerprint)
                        )
                        group.layout = candidate
                        if previous_fingerprint not in (None, str(candidate.fingerprint)):
                            group.ocr_ticket += 1
                        group.last_bounds = tuple(candidate.bounds)
                        group.failures = 0
                        group.last_failure_detail = ""
                        group.next_attempt_at = 0.0
                        accepted_readings = checked
                    else:
                        # The worker inspected an older frame. Never publish its
                        # values or accept geometry that fails current guards.
                        if not required_fields:
                            group.layout = None
                        group.last_bounds = tuple(candidate.bounds)
                        group.force_global = True
                        group.last_failure_detail = (
                            "discovered resource layout did not match the current frame"
                        )

            self._cache_ocr_completion(group)

            readings: Mapping[str, Reading] = accepted_readings or {}
            if group.layout is not None:
                if accepted_readings is None:
                    try:
                        readings = self._read_current(group, frame, group.layout)
                    except Exception:
                        readings = {}
                if not self._layout_valid(group, frame, group.layout, readings):
                    group.last_bounds = tuple(group.layout.bounds)
                    group.layout = None
                    # The startup region is an explicit panel choice.  It is
                    # not proof that a panel found there after relocation is
                    # the same panel, so reacquisition must first establish
                    # global uniqueness.
                    group.force_global = True
                    group.ticket += 1
                    group.ocr_ticket += 1
                    group.last_failure_detail = (
                        "resource layout moved, changed, or is obscured; rediscovery required"
                    )
                    readings = {}

            if group.layout is None:
                self._start_discovery(reader_id, use_ocr, group, frame)
                working = group.worker is not None and group.worker.is_alive()
                if working and group.last_failure_detail:
                    detail = (
                        "adaptive layout discovery is in progress; previous attempt: "
                        f"{group.last_failure_detail}"
                    )
                elif working:
                    detail = "adaptive layout discovery is in progress"
                else:
                    detail = group.last_failure_detail or "adaptive layout is unavailable"
                diagnostics = {
                    "discovery_detail": group.last_failure_detail,
                    "discovery_failures": group.failures,
                    "retry_after": group.next_attempt_at,
                }
                output.update(
                    (
                        sensor,
                        self._unknown(detail, reader_id, diagnostics),
                    )
                    for sensor, _ in items
                )
                continue

            fields = {metadata.field for _sensor, metadata in items}
            layout_fields = set(getattr(group.layout, "regions", {}))
            missing_fields = frozenset(fields - layout_fields)
            if missing_fields:
                # A three-anchor panel remains useful, but it is incomplete.
                # Reacquire in the background until the obscured requested row
                # reappears; existing known fields remain publishable.
                self._start_discovery(
                    reader_id,
                    use_ocr,
                    group,
                    frame,
                    required_fields=missing_fields,
                )
            hashes = (
                self._eligible_ocr_hashes(frame, group.layout, readings, fields)
                if use_ocr
                else {}
            )
            pending_hashes: dict[str, str] = {}
            if use_ocr:
                updated = dict(readings)
                fingerprint = str(group.layout.fingerprint)
                for field_name, roi_hash in hashes.items():
                    current = updated.get(field_name)
                    if current is None or current.quality is Quality.KNOWN:
                        continue
                    cached = group.ocr_cache.get((fingerprint, field_name, roi_hash))
                    if cached is None:
                        retry = group.ocr_retry.get(
                            (fingerprint, field_name, roi_hash)
                        )
                        if retry is not None and float(self._clock()) < retry[1]:
                            continue
                        pending_hashes[field_name] = roi_hash
                        continue
                    diagnostics = dict(cached.diagnostics)
                    diagnostics["adaptive_ocr_cache"] = "exact_roi"
                    updated[field_name] = Reading(
                        cached.value, cached.quality, cached.detail, diagnostics
                    )
                readings = updated
                self._start_ocr(reader_id, group, frame, group.layout, pending_hashes)

            for sensor, metadata in items:
                reading = readings.get(metadata.field)
                if reading is None:
                    reading = self._unknown(
                        f"adaptive reader omitted field {metadata.field!r}", reader_id
                    )
                reading = bounded_adaptive_count(reading, metadata.max_digits)
                output[sensor] = self._decorate(reading, reader_id, group.layout)
        return MappingProxyType(output)


__all__ = [
    "AdaptiveResourceTracker",
    "CaptureInputMapping",
    "bounded_adaptive_count",
]
