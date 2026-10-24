"""Desktop implementation of the public client adapter contract.

The module is safe to import without a display or native Windows packages.
Native input is constructed lazily by :class:`conf.Hotkeys.Hotkey`, and native
capture runs in :class:`tibia_client.capture.CaptureProcess`.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from numbers import Integral
import threading
import time
from typing import Any, Sequence

from conf.Constants import LifeColor, LifeColorFull, ManaColor, ManaColorFull
from conf.Hotkeys import Hotkey
from core import runtime
from core.HookWindow import LocateMatch
from core.perception import CaptureError, Frame, as_frame, validate_region
from engine.ScanStages import ScanStages
from tibia_client.capture import CaptureProcess, TransientCaptureError, _retryable_capture_error
from tibia_client.adaptive_runtime import AdaptiveResourceTracker, CaptureInputMapping
from tibia_client.diagnostics import AdapterCapabilities
from tibia_client.models import (
    Action,
    ActionKind,
    Marker,
    ObservationBatch,
    Quality,
    Reading,
    Sensor,
    SensorKind,
    Target,
)
from tibia_client.visual import (
    MAX_SENSOR_PIXELS,
    MAX_VISUAL_WORK_PIXELS,
    VisualProfile,
)


class DesktopAdapterError(RuntimeError):
    """A desktop operation failed without producing unsafe evidence."""


def _cancelled(token: Any) -> bool:
    value = getattr(token, "cancelled", False)
    return bool(value() if callable(value) else value)


def _region(sensor: Sensor, length: int) -> tuple[int, ...]:
    if len(sensor.region) != length:
        label = "x/y anchor" if length == 2 else "left/top/right/bottom box"
        raise ValueError(f"{sensor.kind.value} sensor needs a {label}")
    if length == 4:
        left, top, right, bottom = sensor.region
        if left < 0 or top < 0 or right <= left or bottom <= top:
            raise ValueError(f"{sensor.kind.value} sensor has an invalid region {sensor.region!r}")
    elif any(value < 0 for value in sensor.region):
        raise ValueError(f"{sensor.kind.value} sensor has an invalid anchor {sensor.region!r}")
    return sensor.region


class DesktopAdapter:
    """Read one coherent client frame and dispatch validated desktop actions."""

    availability_policy = "on_error"

    def __init__(
        self,
        mouse_mode: int,
        *,
        hotkey: Any | None = None,
        capture_provider: Any | None = None,
        clock: Any | None = None,
        visual_profile: VisualProfile | Mapping[str, object] | None = None,
        adaptive_tracker: AdaptiveResourceTracker | None = None,
        coordinate_mapping: CaptureInputMapping | None = None,
        ui_scale: float = 1.0,
    ) -> None:
        if isinstance(mouse_mode, bool) or mouse_mode not in (0, 1):
            raise ValueError("mouse_mode must be 0 (message) or 1 (physical)")
        compiled_visual_profile = (
            visual_profile
            if isinstance(visual_profile, VisualProfile)
            else VisualProfile(visual_profile)
        )
        self.mouse_mode = int(mouse_mode)
        self.ui_scale = float(ui_scale)
        from tibia_client.battle_text import BattleTextReader
        self._battle_text = BattleTextReader()
        self._follow_matches = {}
        from tibia_client.visual_readings import MarkerReader
        self._markers = MarkerReader()
        native_hotkey = hotkey is None
        self.hotkey = hotkey if hotkey is not None else Hotkey(self.mouse_mode)
        if capture_provider is None:
            # Configuration has already been calibrated by the composition
            # root.  Supplying its exact extent avoids reserving the 64 MiB
            # safety cap for every normal worker.
            from conf.conf_manager import ConfManager

            capture_size = ConfManager.get("conf.json").get("capture_size")
            self.capture_provider = CaptureProcess(capture_size=capture_size, startup_timeout=15.0)
        else:
            self.capture_provider = capture_provider
        self._evidence_source = (
            "live"
            if native_hotkey
            and isinstance(self.capture_provider, CaptureProcess)
            and getattr(self.capture_provider, "provider", object()) is None
            else "simulation"
        )
        self._system_clock = clock is None or clock is time
        self.clock = clock if clock is not None else time
        if not callable(getattr(self.clock, "monotonic", None)):
            raise TypeError("clock must provide monotonic()")
        if not callable(self.capture_provider) and not callable(getattr(self.capture_provider, "capture", None)):
            raise TypeError("capture_provider must be callable or expose capture()")
        self._closed = False
        self._close_complete = False
        configured_size = getattr(self.capture_provider, "capture_size", None)
        self._capture_size: tuple[int, int] | None = (
            tuple(configured_size) if configured_size is not None else None
        )
        self._last_capture_at: float | None = None
        self._capture_error: str | None = None
        self.last_error_diagnostic = None
        self._visual_profile = compiled_visual_profile
        if adaptive_tracker is not None and not isinstance(
            adaptive_tracker, AdaptiveResourceTracker
        ):
            raise TypeError("adaptive_tracker must be an AdaptiveResourceTracker")
        if coordinate_mapping is not None:
            if not isinstance(coordinate_mapping, CaptureInputMapping):
                raise TypeError("coordinate_mapping must be a CaptureInputMapping")
            if not coordinate_mapping.identity:
                raise ValueError("desktop adapter currently supports only 1:1 capture/input mapping")
        self._adaptive_tracker = adaptive_tracker or AdaptiveResourceTracker()
        self._coordinate_mapping = coordinate_mapping
        self._last_cursor_transition: (
            tuple[tuple[int, int], tuple[int, int]] | None
        ) = None
        self._readings_inspector = None
        self._readings_detectors = None
        self._readings_tracker = None
        self._readings_reset = threading.Event()

    @property
    def evidence_source(self) -> str:
        """Report provenance only; this does not claim a probe passed."""

        return self._evidence_source

    @property
    def last_cursor_transition(
        self,
    ) -> tuple[tuple[int, int], tuple[int, int]] | None:
        return self._last_cursor_transition

    def capabilities(self) -> AdapterCapabilities:
        """Declare implementation support without claiming live verification."""

        operations = [
            "press",
            "modified_press",
            "left_click",
            "right_click",
            "drag",
            "battle_list_click",
        ]
        if self.mouse_mode == 1:
            operations.append("cursor_restore")
        sensors = [kind.value for kind in SensorKind]
        if not self._visual_profile:
            sensors.remove(SensorKind.VISUAL.value)
        return AdapterCapabilities(
            operations=tuple(operations),
            sensors=tuple(sorted(sensors)),
        )

    @property
    def visual_profile(self) -> VisualProfile:
        """Return the immutable currently installed visual registry."""

        return self._visual_profile

    def configure_visual_profile(
        self,
        profile: VisualProfile | Mapping[str, object] | None,
    ) -> None:
        """Atomically install a compiled profile without capture or input."""

        if self._closed:
            raise DesktopAdapterError("desktop adapter is closed")
        self._visual_profile = profile if isinstance(profile, VisualProfile) else VisualProfile(profile)
        self._adaptive_tracker.reset()

    def _backend_check(self, name: str) -> bool:
        backend = getattr(self.hotkey, "SendToClient", None)
        checker = getattr(backend, name, None)
        if checker is None:
            checker = getattr(self.hotkey, name, None)
        if not callable(checker):
            raise TypeError(f"input backend must provide {name}()")
        return bool(checker())

    def ready(self) -> bool:
        """Explicit manual diagnostic; normal ticks/actions do not call this."""

        if self._closed:
            return False
        try:
            size = self.hotkey.MainWindowSize()
            if (
                not isinstance(size, (tuple, list))
                or len(size) != 2
                or any(isinstance(item, bool) or not isinstance(item, Integral) or item <= 0 for item in size)
            ):
                return False
            actual_size = (int(size[0]), int(size[1]))
            if self._capture_size is not None and actual_size != self._capture_size:
                return False
            if self.mouse_mode == 0:
                return self._backend_check("IsReady")
            return self._backend_check("IsFocused")
        except Exception:
            return False

    def _diagnose_operation_error(self, error: Exception) -> None:
        """Keep a failed operation's diagnostic without repeating its input."""
        current = error
        visited = set()
        detail = None
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            detail = getattr(current, "client_availability", None)
            if detail is not None:
                break
            current = current.__cause__
        if detail is None:
            try:
                diagnose = getattr(self.hotkey, "diagnose_availability", None)
                detail = (diagnose(error) if callable(diagnose)
                          else {"is_window": self._backend_check("IsReady")})
            except Exception as exc:
                detail = {"diagnostic_error": f"{type(exc).__name__}: {exc}"}
        self.last_error_diagnostic = detail
        try:
            error.client_availability = detail
        except Exception:
            pass

    def _capture(self, token: Any) -> Frame:
        provider = self.capture_provider
        if isinstance(provider, CaptureProcess):
            return provider.capture(token=token)
        method = getattr(provider, "capture", None)
        value = method() if callable(method) else provider()
        return as_frame(value, source="desktop-adapter")

    def _capture_failure(self, message: str, *, retryable=False):
        failure = (TransientCaptureError if retryable else CaptureError)(message)
        self._diagnose_operation_error(failure)
        raise failure

    @staticmethod
    def _bar(sensor: Sensor, frame: Frame) -> Reading:
        if len(sensor.region) == 4:
            from engine.ScanStages import read_bar_region
            value = read_bar_region(frame, sensor.region, sensor.kind.value)
            return Reading(value) if value is not None else Reading(
                None, Quality.UNKNOWN, "calibrated bar is not readable")
        anchor = _region(sensor, 2)
        if sensor.kind is SensorKind.HEALTH:
            scanner = ScanStages("Health")
            value = scanner.scan(anchor, LifeColor, LifeColorFull, frame=frame)
        else:
            scanner = ScanStages("Mana")
            value = scanner.scan(anchor, ManaColor, ManaColorFull, frame=frame)
        if value is None:
            return Reading(
                None,
                Quality.UNKNOWN,
                f"{sensor.kind.value} bar contains no readable calibrated stage",
            )
        return Reading(int(value))

    def _present(self, template: str, sensor: Sensor, frame: Frame, *, precision: float = 0.9) -> Reading:
        region = _region(sensor, 4)
        match = LocateMatch(template, Region=region, Precision=precision, frame=frame,
                            Scale=self.ui_scale)
        return Reading(match is not None, detail=f'Template presence at threshold {precision:.2f}',
                       diagnostics={'reader': 'template', 'ui_scale': self.ui_scale,
                                    'score': None if match is None else match.score})

    def _target(self, sensor: Sensor, frame: Frame) -> Reading:
        region = _region(sensor, 4)
        from tibia_client.battle_text import read_target
        return read_target(frame, region, sensor.name, self.ui_scale, self._battle_text)

    @staticmethod
    def _marker(sensor: Sensor, frame: Frame, scale: float = 1.0, reader=None) -> Reading:
        region = _region(sensor, 4)
        if not sensor.name.strip():
            raise ValueError("marker sensor needs a marker name")
        from tibia_client.visual_readings import read_marker
        if reader is not None:
            return reader.read(frame, region, sensor.name, scale)
        return read_marker(frame, region, sensor.name, scale)

    @staticmethod
    def _follow(sensor: Sensor, frame: Frame, scale: float = 1.0, matches=None) -> Reading:
        from tibia_client.follow_controls import follow_action
        return follow_action(sensor, frame, scale, matches)

    def _validate_sensor(self, sensor: Sensor, frame: Frame) -> None:
        """Validate every requested geometry before any detector executes."""

        if not isinstance(sensor, Sensor):
            raise TypeError("observe sensors must be Sensor values")
        if sensor.kind in (SensorKind.HEALTH, SensorKind.MANA):
            if len(sensor.region) == 4:
                validate_region(_region(sensor, 4), frame.size)
                return
            anchor_x, anchor_y = _region(sensor, 2)
            # ScanStages reads x+5 through x+100 inclusive on one row.
            validate_region((anchor_x + 5, anchor_y, anchor_x + 101, anchor_y + 1), frame.size)
            return
        if sensor.kind is SensorKind.FRAME_VALID:
            if sensor.region or sensor.name:
                raise ValueError("frame_valid sensor does not take a region or name")
            return
        if sensor.kind is SensorKind.READINGS:
            if sensor.region or sensor.name:
                raise ValueError('readings sensor does not take a region or name')
            return
        if sensor.kind in (
            SensorKind.RING_EMPTY,
            SensorKind.AMULET_EMPTY,
            SensorKind.HASTED,
            SensorKind.HUNGRY,
            SensorKind.TARGET,
            SensorKind.MARKER,
        ):
            validate_region(_region(sensor, 4), frame.size)
            if sensor.kind in (SensorKind.TARGET, SensorKind.MARKER) and not sensor.name.strip():
                raise ValueError(f"{sensor.kind.value} sensor needs a name")
            return
        if sensor.kind is SensorKind.FOLLOW:
            if sensor.region:
                raise ValueError("follow sensor does not take a region")
            if sensor.name not in ("", "disable"):
                raise ValueError("follow sensor name must be empty (enable) or 'disable'")
            return
        if sensor.kind is SensorKind.VISUAL:
            region = validate_region(_region(sensor, 4), frame.size)
            if (region[2] - region[0]) * (region[3] - region[1]) > MAX_SENSOR_PIXELS:
                raise ValueError(
                    f"visual sensor exceeds the {MAX_SENSOR_PIXELS} pixel budget"
                )
            if not sensor.name.strip():
                raise ValueError("visual sensor needs a spec id")
            if sensor.name not in self._visual_profile:
                raise ValueError(f"visual profile has no spec {sensor.name!r}")
            return
        raise ValueError(f"unsupported sensor kind {sensor.kind!r}")

    def _read(self, sensor: Sensor, frame: Frame, *, follow_matches=None) -> Reading:
        if sensor.kind is SensorKind.FRAME_VALID:
            return Reading(True)
        if sensor.kind in (SensorKind.HEALTH, SensorKind.MANA):
            return self._bar(sensor, frame)
        if sensor.kind is SensorKind.RING_EMPTY:
            return self._present("images/PlayerStats/NoRing.png", sensor, frame)
        if sensor.kind is SensorKind.AMULET_EMPTY:
            return self._present("images/PlayerStats/NoAmulet.png", sensor, frame)
        if sensor.kind is SensorKind.HASTED:
            return self._present("images/PlayerStats/Hur.png", sensor, frame)
        if sensor.kind is SensorKind.HUNGRY:
            return self._present("images/PlayerStats/Starving.png", sensor, frame)
        if sensor.kind is SensorKind.TARGET:
            return self._target(sensor, frame)
        if sensor.kind is SensorKind.MARKER:
            return self._marker(sensor, frame, self.ui_scale, self._markers)
        if sensor.kind is SensorKind.FOLLOW:
            return self._follow(sensor, frame, self.ui_scale, follow_matches)
        if sensor.kind is SensorKind.VISUAL:
            return self._visual_profile.read(sensor, frame)
        raise ValueError(f"unsupported sensor kind {sensor.kind!r}")

    def _input_reading(self, reading: Reading) -> Reading:
        """Translate action positions at the adapter boundary, after detection."""

        mapping = self._coordinate_mapping
        if mapping is None or reading.value is None:
            return reading
        value = reading.value
        diagnostics = dict(reading.diagnostics)
        if isinstance(value, Target) and value.position is not None:
            diagnostics.setdefault("capture_position", value.position)
            value = Target(value.count, mapping.capture_to_input(value.position), value.selected, value.count_complete)
        elif isinstance(value, Marker) and value.position is not None:
            diagnostics.setdefault("capture_position", value.position)
            value = Marker(mapping.capture_to_input(value.position), value.reached)
        elif (
            isinstance(value, tuple)
            and len(value) == 2
            and all(type(item) is int and item >= 0 for item in value)
        ):
            diagnostics.setdefault("capture_position", value)
            value = mapping.capture_to_input(value)
        elif isinstance(value, Mapping) and value.get("position") is not None:
            position = tuple(value["position"])
            diagnostics.setdefault("capture_position", position)
            changed = dict(value)
            changed["position"] = mapping.capture_to_input(position)
            value = changed
        return Reading(value, reading.quality, reading.detail, diagnostics)

    def reset_readings(self) -> None:
        """Signal recalibration without constructing readers on the caller's thread."""
        if self._closed:
            raise DesktopAdapterError('desktop adapter is closed')
        self._readings_reset.set()

    def _inspect_readings(self, frame, resolve, cache, checkpoint):
        if self._readings_reset.is_set():
            self._readings_reset.clear()
            if self._readings_inspector is not None:
                self._readings_inspector.close()
                self._readings_inspector = None
            if self._readings_tracker is not None:
                self._readings_tracker.reset()
        result = {}
        visual_started = float(self.clock.monotonic())
        try:
            if self._readings_inspector is None:
                from tibia_client.follow_controls import FollowControls
                from tibia_client.visual_readings import BattleTextReader, MarkerReader, VisualReadings
                if self._readings_detectors is None:
                    # Recalibration replaces settings, not active worker lanes.
                    # These private detectors never share learned contexts with
                    # automation and remain bounded across repeated UI resets.
                    self._readings_detectors = dict(
                        battle=BattleTextReader(), follow=FollowControls(), markers=MarkerReader())
                self._readings_inspector = VisualReadings(**self._readings_detectors)

            def battle_for(region, scale):
                if scale == self.ui_scale and any(
                        kind is SensorKind.TARGET and box == region for kind, box, _ in cache):
                    return self._battle_text
                return None

            follow = None
            if self._readings_inspector.scale == self.ui_scale and any(
                    kind is SensorKind.FOLLOW for kind, _, _ in cache):
                follow = self._follow_matches.get('controls')
            result.update(self._readings_inspector.read(
                frame, resolve=resolve, battle_for=battle_for, follow=follow))
        except Exception as exc:
            result['visual_error'] = Reading(None, Quality.ERROR,
                f'visual readings failed: {type(exc).__name__}: {exc}',
                {'label': 'Visual readings', 'group': 'Diagnostics'})
        visual_decode_ms = (float(self.clock.monotonic()) - visual_started) * 1000
        checkpoint()
        try:
            if self._readings_tracker is None:
                from tibia_client.retained_tracker import RetainedResourceTracker
                self._readings_tracker = RetainedResourceTracker()
            result.update(self._readings_tracker.read(frame))
        except Exception as exc:
            from tibia_client.adaptive import FIELDS
            result.update({name: Reading(None, Quality.ERROR,
                           f'resource reader failed: {type(exc).__name__}: {exc}')
                           for name in FIELDS})
        checkpoint()
        return Reading(result, diagnostics={
            'discovery_ms': getattr(self._readings_tracker, 'discovery_ms', 0.0),
            'discovery_detail': getattr(self._readings_tracker, 'detail', ''),
            'recovery': getattr(self._readings_tracker, 'detail', ''),
            'discovery_busy': getattr(self._readings_tracker, 'busy', False),
            'visual_decode_ms': visual_decode_ms,
        })

    def observe(self, sensors: Sequence[Sensor], token: Any) -> ObservationBatch:
        if self._closed:
            raise DesktopAdapterError("desktop adapter is closed")
        token.checkpoint()
        self.last_error_diagnostic = None
        observed_at = float(self.clock.monotonic())
        capture_requested_at = observed_at
        try:
            frame = self._capture(token)
        except Exception as exc:
            self._capture_error = f"{type(exc).__name__}: {exc}"
            if _cancelled(token):
                raise
            self._diagnose_operation_error(exc)
            failure_type = TransientCaptureError if _retryable_capture_error(exc) else CaptureError
            failure = failure_type(f"desktop observation capture failed: {exc}")
            failure.client_availability = self.last_error_diagnostic
            raise failure from exc
        capture_finished = float(self.clock.monotonic())
        if self._capture_size is None:
            self._capture_size = frame.size
        elif frame.size != self._capture_size:
            self._capture_error = (
                "capture geometry changed from "
                f"{self._capture_size[0]}x{self._capture_size[1]} to "
                f"{frame.size[0]}x{frame.size[1]}"
            )
            self._capture_failure(self._capture_error + "; recalibrate the client profile")
        if self._coordinate_mapping is None:
            self._coordinate_mapping = CaptureInputMapping(frame.size, frame.size)
        elif self._coordinate_mapping.capture_size != frame.size:
            self._capture_failure("capture geometry differs from the configured coordinate mapping")
        # An exactly uniform black PrintWindow result is an observable capture
        # failure.  This check says nothing about a static or frozen upstream
        # source; non-black identical frames remain explicitly unverified.
        if not bool(frame.pixels.any()):
            self._capture_error = "capture returned an exactly uniform black frame"
            self._capture_failure(self._capture_error, retryable=True)
        if isinstance(self.capture_provider, CaptureProcess) and self._system_clock:
            capture_started = self.capture_provider.last_observed_at
            returned_at = float(self.clock.monotonic())
            if (
                capture_started is None
                or not math.isfinite(capture_started)
                or not 0 <= returned_at - capture_started
            ):
                self._capture_failure("capture process returned an invalid observation timestamp")
            observed_at = capture_started
        self._last_capture_at = observed_at
        self._capture_error = None
        token.checkpoint()

        readings: dict[Sensor, Reading] = {}
        cache: dict[tuple[SensorKind, tuple[int, ...], str], Reading] = {}
        follow_matches = self._follow_matches
        invalid: dict[tuple[SensorKind, tuple[int, ...], str], Reading] = {}
        visual_work = 0
        budgeted_visual: set[tuple[SensorKind, tuple[int, ...], str]] = set()
        # Geometry and required names are checked as a distinct pass so an
        # invalid request cannot invoke a detector before it is rejected.
        for sensor in sensors:
            token.checkpoint()
            try:
                self._validate_sensor(sensor, frame)
            except Exception as exc:
                if not isinstance(sensor, Sensor):
                    raise
                key = sensor.kind, sensor.region, sensor.name
                invalid[key] = Reading(
                    None,
                    Quality.ERROR,
                    f"{sensor.kind.value} detector failed: {type(exc).__name__}: {exc}",
                )
            else:
                key = sensor.kind, sensor.region, sensor.name
                if sensor.kind is SensorKind.VISUAL and key not in budgeted_visual:
                    budgeted_visual.add(key)
                    left, top, right, bottom = sensor.region
                    # A spec can inspect both its context and target/count
                    # subregions. Charging twice the enclosing sensor area is
                    # a conservative upper bound independent of overlap.
                    cost = 2 * (right - left) * (bottom - top)
                    if visual_work + cost > MAX_VISUAL_WORK_PIXELS:
                        invalid[key] = Reading(
                            None,
                            Quality.UNKNOWN,
                            "visual observation work budget exceeded",
                        )
                    else:
                        visual_work += cost
        adaptive_requests: dict[Sensor, object] = {}
        # Read ordinary sensors first so health/support work is never delayed by
        # adaptive layout bookkeeping. Discovery itself always runs off-thread.
        for sensor in sensors:
            token.checkpoint()
            key = sensor.kind, sensor.region, sensor.name
            if sensor.kind is SensorKind.READINGS:
                continue
            reading = invalid.get(key) or cache.get(key)
            if reading is None:
                adaptive = (
                    self._visual_profile[sensor.name].adaptive
                    if sensor.kind is SensorKind.VISUAL
                    else None
                )
                if adaptive is not None:
                    adaptive_requests[sensor] = adaptive
                    continue
                try:
                    reading = self._input_reading(self._read(sensor, frame, follow_matches=follow_matches))
                except Exception as exc:
                    if _cancelled(token):
                        raise
                    reading = Reading(
                        None,
                        Quality.ERROR,
                        f"{sensor.kind.value} detector failed: {type(exc).__name__}: {exc}",
                    )
                cache[key] = reading
        if adaptive_requests:
            token.checkpoint()
            try:
                adaptive_readings = self._adaptive_tracker.read(frame, adaptive_requests)
            except Exception as exc:
                if _cancelled(token):
                    raise
                adaptive_readings = {
                    sensor: Reading(
                        None,
                        Quality.UNKNOWN,
                        f"adaptive visual tracker failed: {type(exc).__name__}: {exc}",
                    )
                    for sensor in adaptive_requests
                }
            token.checkpoint()
            for sensor, reading in adaptive_readings.items():
                key = sensor.kind, sensor.region, sensor.name
                try:
                    cache[key] = self._input_reading(reading)
                except Exception as exc:
                    cache[key] = Reading(
                        None,
                        Quality.UNKNOWN,
                        f"adaptive visual mapping failed: {type(exc).__name__}: {exc}",
                    )
        inspector_requests = [sensor for sensor in sensors if sensor.kind is SensorKind.READINGS
                              and (sensor.kind, sensor.region, sensor.name) not in invalid]
        if inspector_requests:
            def resolve(sensor, fallback, scale):
                token.checkpoint()
                # A reloaded inspector calibration can have a different scale
                # from automation. Only identical detector settings share results.
                if scale != self.ui_scale and sensor.kind not in (SensorKind.HEALTH, SensorKind.MANA):
                    return fallback()
                key = sensor.kind, sensor.region, sensor.name
                if key in invalid:
                    return invalid[key]
                # Inspector-only regions keep their own learned detector state.
                return cache[key] if key in cache else fallback()
            token.checkpoint()
            try:
                composite = self._inspect_readings(frame, resolve, cache, token.checkpoint)
            except Exception as exc:
                if _cancelled(token):
                    raise
                composite = Reading(None, Quality.ERROR,
                                    f'readings detector failed: {type(exc).__name__}: {exc}')
            for sensor in inspector_requests:
                cache[sensor.kind, sensor.region, sensor.name] = composite
        for sensor in sensors:
            key = sensor.kind, sensor.region, sensor.name
            reading = invalid.get(key) or cache.get(key)
            if reading is None:
                reading = Reading(None, Quality.UNKNOWN, "adaptive visual reading unavailable")
            readings[sensor] = reading
        token.checkpoint()
        if inspector_requests:
            finished = float(self.clock.monotonic())
            diagnostics = {'capture_ms': (capture_finished - capture_requested_at) * 1000,
                           'decode_ms': (finished - capture_finished) * 1000,
                           'total_ms': (finished - capture_requested_at) * 1000}
            composite = Reading(composite.value, composite.quality, composite.detail,
                                {**composite.diagnostics, **diagnostics})
            for sensor in inspector_requests:
                readings[sensor] = composite
        return ObservationBatch(readings=readings, observed_at=observed_at, frame=frame)

    def _restore_cursor(self, point: object, token: Any, *, screen: bool) -> None:
        if _cancelled(token):
            return
        if (
            not isinstance(point, (tuple, list))
            or len(point) != 2
            or any(isinstance(item, bool) or not isinstance(item, Integral) for item in point)
        ):
            return
        try:
            x, y = int(point[0]), int(point[1])
            if screen:
                if not _cancelled(token):
                    self.hotkey.RestoreScreenPosition((x, y))
                return
            # Compatibility for small injected test/third-party facades that
            # predate the dedicated screen-coordinate restore API.
            if not _cancelled(token) and x >= 0 and y >= 0:
                self.hotkey.MoveTo(x, y)
        except Exception as exc:
            # Restoration is best effort after the requested action; changed
            # focus or dimensions must not cause another input attempt.
            if not _cancelled(token):
                self._diagnose_operation_error(exc)
            return

    def activate_input(self) -> bool:
        """Activate once at an explicit lifecycle start, before capture or dispatch."""
        if self._closed:
            raise DesktopAdapterError("desktop adapter is closed")
        if self.mouse_mode != 1:
            return False
        activate = getattr(self.hotkey, "Activate", None)
        if not callable(activate):
            return False
        try:
            return bool(activate())
        except Exception as exc:
            self._diagnose_operation_error(exc)
            failure = DesktopAdapterError(f"could not activate input client: {exc}")
            failure.client_availability = self.last_error_diagnostic
            raise failure from exc

    def execute(self, action: Action, token: Any) -> None:
        if self._closed:
            raise DesktopAdapterError("desktop adapter is closed")
        if not isinstance(action, Action):
            raise TypeError("execute action must be an Action value")
        token.checkpoint()
        self._last_cursor_transition = None
        self.last_error_diagnostic = None
        previous = None
        park = None
        preserve = self.mouse_mode == 1 and action.kind is not ActionKind.PRESS
        restore_support = getattr(self.hotkey, "SupportsScreenRestore", None)
        if callable(restore_support):
            screen_restore = preserve and bool(restore_support())
        else:
            screen_restore = preserve and all(
                callable(getattr(self.hotkey, name, None))
                for name in ("ScreenPosition", "RestoreScreenPosition")
            )
        try:
            with runtime.input_transaction(f"desktop {action.kind.value}"):
                token.checkpoint()
                if preserve:
                    previous = (
                        self.hotkey.ScreenPosition()
                        if screen_restore
                        else self.hotkey.Position()
                    )
                    token.checkpoint()
                    controls = self._follow_matches.get('controls')
                    geometry = controls.parking_geometry() if controls is not None else None
                    if geometry is not None:
                        region, destination = geometry
                        mapping = self._coordinate_mapping
                        if mapping is not None:
                            region = (*mapping.capture_to_input(region[:2]),
                                      *mapping.capture_to_input((region[2]-1, region[3]-1)))
                            destination = mapping.capture_to_input(destination)
                        def over_controls(point):
                            return point is not None and region[0] <= point[0] <= region[2] and region[1] <= point[1] <= region[3]
                        # Also avoid restoring onto these buttons after a target
                        # or loot click when the pointer began over a control.
                        began_over_controls = False
                        if not over_controls(action.point):
                            try:
                                began_over_controls = over_controls(
                                    self.hotkey.Position() if screen_restore else previous)
                            except Exception:
                                # Optional parking must not prevent an attack
                                # if the extra cursor-position read fails.
                                pass
                        if over_controls(action.point) or began_over_controls:
                            park = destination
                try:
                    if action.kind is ActionKind.PRESS:
                        self.hotkey.Press(action.key)
                    elif action.kind is ActionKind.LEFT_CLICK:
                        self.hotkey.LeftClick(action.point[0], action.point[1])
                    elif action.kind is ActionKind.RIGHT_CLICK:
                        self.hotkey.RightClick(action.point[0], action.point[1])
                    elif action.kind is ActionKind.DRAG:
                        self.hotkey.DragTo(action.point, action.destination)
                    else:
                        raise ValueError(f"unsupported action kind {action.kind!r}")
                    token.checkpoint()
                finally:
                    if preserve and previous is not None:
                        # Stay inside the guest viewport so Sandbox receives a
                        # move away from the control, without a follow-up click.
                        self._restore_cursor(park if park is not None else previous, token,
                                             screen=False if park is not None else screen_restore)
                        if not _cancelled(token):
                            try:
                                final = (
                                    self.hotkey.ScreenPosition()
                                    if screen_restore
                                    else self.hotkey.Position()
                                )
                            except Exception:
                                final = None
                            if (
                                isinstance(previous, (tuple, list))
                                and isinstance(final, (tuple, list))
                                and len(previous) == len(final) == 2
                                and all(
                                    not isinstance(value, bool)
                                    and isinstance(value, Integral)
                                    for value in (*previous, *final)
                                )
                            ):
                                self._last_cursor_transition = (
                                    (int(previous[0]), int(previous[1])),
                                    (int(final[0]), int(final[1])),
                                )
        except Exception as exc:
            if _cancelled(token):
                raise
            self._diagnose_operation_error(exc)
            failure = DesktopAdapterError(
                f"{action.kind.value} action failed: {type(exc).__name__}: {exc}"
            )
            failure.client_availability = self.last_error_diagnostic
            raise failure from exc

    def close(self) -> None:
        if self._close_complete:
            return
        self._closed = True
        if self._readings_inspector is not None:
            self._readings_inspector.close()
        if self._readings_tracker is not None:
            self._readings_tracker.close()
        if self._readings_detectors is not None:
            for reader in self._readings_detectors.values():
                reader.close()
        self._battle_text.close()
        self._markers.close()
        controls = self._follow_matches.get("controls")
        if controls is not None:
            controls.close()
        self._follow_matches.clear()
        self._adaptive_tracker.close()
        close = getattr(self.capture_provider, "close", None)
        if callable(close):
            close()
        self._close_complete = True


class KeyboardDesktopAdapter(DesktopAdapter):
    """Production keyboard modules without pretending image calibration succeeded."""

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(operations=("press", "modified_press"),
                                   sensors=(SensorKind.READINGS.value,))

    def observe(self, sensors: Sequence[Sensor], token: Any) -> ObservationBatch:
        if any(not isinstance(sensor, Sensor) or sensor.kind is not SensorKind.READINGS
               for sensor in sensors):
            raise DesktopAdapterError("image calibration is required for visual sensors")
        return super().observe(sensors, token)

    def execute(self, action: Action, token: Any) -> None:
        if action.kind is not ActionKind.PRESS:
            raise DesktopAdapterError("keyboard modules only support key presses")
        return super().execute(action, token)


__all__ = ["DesktopAdapter", "DesktopAdapterError", "KeyboardDesktopAdapter"]
