"""Desktop implementation of the public client adapter contract.

The module is safe to import without a display or native Windows packages.
Native input is constructed lazily by :class:`conf.Hotkeys.Hotkey`, and native
capture runs in :class:`tibia_client.capture.CaptureProcess`.
"""

from __future__ import annotations

import math
from numbers import Integral
import time
from typing import Any, Sequence

from conf.Constants import LifeColor, LifeColorFull, ManaColor, ManaColorFull
from conf.Hotkeys import Hotkey
from core import runtime
from core.HookWindow import LocateMatch, LocateMatches
from core.perception import CaptureError, Frame, as_frame, validate_region
from engine.CaveBot import Scanners
from engine.ScanStages import ScanStages
from tibia_client.capture import CaptureProcess
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

    def __init__(
        self,
        mouse_mode: int,
        *,
        hotkey: Any | None = None,
        capture_provider: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        if isinstance(mouse_mode, bool) or mouse_mode not in (0, 1):
            raise ValueError("mouse_mode must be 0 (message) or 1 (physical)")
        self.mouse_mode = int(mouse_mode)
        native_hotkey = hotkey is None
        self.hotkey = hotkey if hotkey is not None else Hotkey(self.mouse_mode)
        if capture_provider is None:
            # Configuration has already been calibrated by the composition
            # root.  Supplying its exact extent avoids reserving the 64 MiB
            # safety cap for every normal worker.
            from conf.conf_manager import ConfManager

            capture_size = ConfManager.get("conf.json").get("capture_size")
            self.capture_provider = CaptureProcess(capture_size=capture_size)
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
        configured_size = getattr(self.capture_provider, "capture_size", None)
        self._capture_size: tuple[int, int] | None = (
            tuple(configured_size) if configured_size is not None else None
        )
        self._last_capture_at: float | None = None
        self._capture_error: str | None = None
        self._last_cursor_transition: (
            tuple[tuple[int, int], tuple[int, int]] | None
        ) = None

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
        return AdapterCapabilities(
            operations=tuple(operations),
            sensors=tuple(sorted(kind.value for kind in SensorKind)),
        )

    def _backend_check(self, name: str) -> bool:
        backend = getattr(self.hotkey, "SendToClient", None)
        checker = getattr(backend, name, None)
        if checker is None:
            checker = getattr(self.hotkey, name, None)
        if not callable(checker):
            raise TypeError(f"input backend must provide {name}()")
        return bool(checker())

    def ready(self) -> bool:
        """Read fresh target dimensions and readiness without generating input."""

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

    def _capture(self, token: Any) -> Frame:
        provider = self.capture_provider
        if isinstance(provider, CaptureProcess):
            return provider.capture(token=token)
        method = getattr(provider, "capture", None)
        value = method() if callable(method) else provider()
        return as_frame(value, source="desktop-adapter")

    @staticmethod
    def _bar(sensor: Sensor, frame: Frame) -> Reading:
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

    @staticmethod
    def _present(template: str, sensor: Sensor, frame: Frame, *, precision: float = 0.9) -> Reading:
        region = _region(sensor, 4)
        return Reading(
            LocateMatch(template, Region=region, Precision=precision, frame=frame) is not None
        )

    @staticmethod
    def _target(sensor: Sensor, frame: Frame) -> Reading:
        region = _region(sensor, 4)
        if not sensor.name.strip():
            raise ValueError("target sensor needs a monster name")
        matches = LocateMatches(
            f"images/Targets/Names/{sensor.name}.png",
            Region=region,
            Precision=0.8,
            frame=frame,
        )
        # Counting deliberately uses the broader 0.8 NMS result.  A click
        # requires the legacy scanner's stricter best-match threshold.
        best = LocateMatch(
            f"images/Targets/Names/{sensor.name}.png",
            Region=region,
            Precision=0.86,
            frame=frame,
        )
        position = None if best is None else (
            region[0] + best.center[0],
            region[1] + best.center[1],
        )
        selected = bool(
            Scanners.target_selected(region, frame=frame, target=sensor.name)
        )
        return Reading(Target(count=len(matches), position=position, selected=selected))

    @staticmethod
    def _marker(sensor: Sensor, frame: Frame) -> Reading:
        region = _region(sensor, 4)
        if not sensor.name.strip():
            raise ValueError("marker sensor needs a marker name")
        # Navigation uses stricter click evidence than the legacy arrival
        # predicate, so preserve both thresholds on the same frame.
        match = LocateMatch(
            f"images/MapSettings/{sensor.name}.png",
            Region=region,
            Precision=0.8,
            frame=frame,
        )
        arrival = LocateMatch(
            f"images/MapSettings/{sensor.name}.png",
            Region=region,
            Precision=0.7,
            frame=frame,
        )
        width, height = region[2] - region[0], region[3] - region[1]
        reached = False
        if arrival is not None:
            arrival_x, arrival_y = arrival.center
            reached = (
                abs(arrival_x - width / 2.0) <= math.ceil(arrival.width / 2.0)
                and abs(arrival_y - height / 2.0) <= math.ceil(arrival.height / 2.0)
            )
        if match is None:
            return Reading(Marker(position=None, reached=reached))
        relative_x, relative_y = match.center
        return Reading(
            Marker(
                position=(region[0] + relative_x, region[1] + relative_y),
                reached=reached,
            )
        )

    @staticmethod
    def _follow(sensor: Sensor, frame: Frame) -> Reading:
        if sensor.region:
            raise ValueError("follow sensor does not take a region")
        if not Scanners.NeedFollow(frame=frame):
            return Reading(None)
        button = LocateMatch(
            "images/TibiaSettings/NotFollow.png", Precision=0.7, frame=frame
        )
        if button is None:
            return Reading(
                None,
                Quality.UNKNOWN,
                "follow is needed but the enable-follow button is unreadable",
            )
        return Reading(button.center)

    @staticmethod
    def _validate_sensor(sensor: Sensor, frame: Frame) -> None:
        """Validate every requested geometry before any detector executes."""

        if not isinstance(sensor, Sensor):
            raise TypeError("observe sensors must be Sensor values")
        if sensor.kind in (SensorKind.HEALTH, SensorKind.MANA):
            anchor_x, anchor_y = _region(sensor, 2)
            # ScanStages reads x+5 through x+100 inclusive on one row.
            validate_region((anchor_x + 5, anchor_y, anchor_x + 101, anchor_y + 1), frame.size)
            return
        if sensor.kind is SensorKind.FRAME_VALID:
            if sensor.region or sensor.name:
                raise ValueError("frame_valid sensor does not take a region or name")
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
            return
        raise ValueError(f"unsupported sensor kind {sensor.kind!r}")

    @classmethod
    def _read(cls, sensor: Sensor, frame: Frame) -> Reading:
        if sensor.kind is SensorKind.FRAME_VALID:
            return Reading(True)
        if sensor.kind in (SensorKind.HEALTH, SensorKind.MANA):
            return cls._bar(sensor, frame)
        if sensor.kind is SensorKind.RING_EMPTY:
            return cls._present("images/PlayerStats/NoRing.png", sensor, frame)
        if sensor.kind is SensorKind.AMULET_EMPTY:
            return cls._present("images/PlayerStats/NoAmulet.png", sensor, frame)
        if sensor.kind is SensorKind.HASTED:
            return cls._present("images/PlayerStats/Hur.png", sensor, frame)
        if sensor.kind is SensorKind.HUNGRY:
            return cls._present("images/PlayerStats/Starving.png", sensor, frame)
        if sensor.kind is SensorKind.TARGET:
            return cls._target(sensor, frame)
        if sensor.kind is SensorKind.MARKER:
            return cls._marker(sensor, frame)
        if sensor.kind is SensorKind.FOLLOW:
            return cls._follow(sensor, frame)
        raise ValueError(f"unsupported sensor kind {sensor.kind!r}")

    def observe(self, sensors: Sequence[Sensor], token: Any) -> ObservationBatch:
        if self._closed:
            raise DesktopAdapterError("desktop adapter is closed")
        token.checkpoint()
        observed_at = float(self.clock.monotonic())
        try:
            frame = self._capture(token)
        except Exception as exc:
            self._capture_error = f"{type(exc).__name__}: {exc}"
            if _cancelled(token):
                raise
            raise CaptureError(f"desktop observation capture failed: {exc}") from exc
        if self._capture_size is None:
            self._capture_size = frame.size
        elif frame.size != self._capture_size:
            self._capture_error = (
                "capture geometry changed from "
                f"{self._capture_size[0]}x{self._capture_size[1]} to "
                f"{frame.size[0]}x{frame.size[1]}"
            )
            raise CaptureError(self._capture_error + "; recalibrate the client profile")
        # An exactly uniform black PrintWindow result is an observable capture
        # failure.  This check says nothing about a static or frozen upstream
        # source; non-black identical frames remain explicitly unverified.
        if not bool(frame.pixels.any()):
            self._capture_error = "capture returned an exactly uniform black frame"
            raise CaptureError(self._capture_error)
        if isinstance(self.capture_provider, CaptureProcess) and self._system_clock:
            capture_started = self.capture_provider.last_observed_at
            returned_at = float(self.clock.monotonic())
            if (
                capture_started is None
                or not math.isfinite(capture_started)
                or not 0 <= returned_at - capture_started
            ):
                raise CaptureError("capture process returned an invalid observation timestamp")
            observed_at = capture_started
        self._last_capture_at = observed_at
        self._capture_error = None
        token.checkpoint()

        readings: dict[Sensor, Reading] = {}
        cache: dict[tuple[SensorKind, tuple[int, ...], str], Reading] = {}
        invalid: dict[tuple[SensorKind, tuple[int, ...], str], Reading] = {}
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
        for sensor in sensors:
            token.checkpoint()
            key = sensor.kind, sensor.region, sensor.name
            reading = invalid.get(key) or cache.get(key)
            if reading is None:
                try:
                    reading = self._read(sensor, frame)
                except Exception as exc:
                    if _cancelled(token):
                        raise
                    reading = Reading(
                        None,
                        Quality.ERROR,
                        f"{sensor.kind.value} detector failed: {type(exc).__name__}: {exc}",
                    )
                cache[key] = reading
            readings[sensor] = reading
        token.checkpoint()
        return ObservationBatch(readings=readings, observed_at=observed_at, frame=frame)

    def _restore_cursor(self, point: object, token: Any) -> None:
        if _cancelled(token):
            return
        if (
            not isinstance(point, (tuple, list))
            or len(point) != 2
            or any(isinstance(item, bool) or not isinstance(item, Integral) for item in point)
        ):
            return
        try:
            width, height = self.hotkey.MainWindowSize()
            x, y = int(point[0]), int(point[1])
            if not _cancelled(token) and 0 <= x < int(width) and 0 <= y < int(height):
                self.hotkey.MoveTo(x, y)
        except Exception:
            # Restoration is best effort after the requested action; changed
            # focus or dimensions must not cause another input attempt.
            return

    def execute(self, action: Action, token: Any) -> None:
        if self._closed:
            raise DesktopAdapterError("desktop adapter is closed")
        if not isinstance(action, Action):
            raise TypeError("execute action must be an Action value")
        token.checkpoint()
        self._last_cursor_transition = None
        # Revalidate the live input binding immediately before entering the
        # native transaction.  A moved/recreated window or geometry change must
        # not inherit coordinates from an earlier capture.
        if not self.ready():
            raise DesktopAdapterError(
                "input target is no longer ready or its geometry changed; recalibrate"
            )
        token.checkpoint()
        previous = None
        preserve = self.mouse_mode == 1 and action.kind is not ActionKind.PRESS
        try:
            with runtime.input_transaction(f"desktop {action.kind.value}"):
                token.checkpoint()
                if preserve:
                    previous = self.hotkey.Position()
                    token.checkpoint()
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
                        self._restore_cursor(previous, token)
                        if not _cancelled(token):
                            try:
                                final = self.hotkey.Position()
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
            raise DesktopAdapterError(
                f"{action.kind.value} action failed: {type(exc).__name__}: {exc}"
            ) from exc

    def close(self) -> None:
        self._closed = True
        close = getattr(self.capture_provider, "close", None)
        if callable(close):
            close()


__all__ = ["DesktopAdapter", "DesktopAdapterError"]
