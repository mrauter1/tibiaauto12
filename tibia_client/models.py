"""Public, immutable values shared by features, the runtime and client adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
import math
from types import MappingProxyType
from typing import Any, Mapping


Point = tuple[int, int]


def _freeze(value: Any) -> Any:
    """Copy mutable containers into recursively immutable public values."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _text(value: object, name: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        requirement = "non-empty text" if nonempty else "text"
        raise ValueError(f"{name} must be {requirement}")
    return value


def _point(value: Point | None) -> Point | None:
    if value is None:
        return None
    try:
        point = tuple(value)
    except TypeError:
        raise ValueError("client point must contain non-negative integer coordinates") from None
    if len(point) != 2 or any(type(n) is not int or n < 0 for n in point):
        raise ValueError("client point must contain non-negative integer coordinates")
    return point


def _identity(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def finite(value: float, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'non-negative'}")
    return float(value)


class SensorKind(str, Enum):
    FRAME_VALID = "frame_valid"
    HEALTH = "health"
    MANA = "mana"
    RING_EMPTY = "ring_empty"
    AMULET_EMPTY = "amulet_empty"
    HASTED = "hasted"
    HUNGRY = "hungry"
    TARGET = "target"
    MARKER = "marker"
    FOLLOW = "follow"
    VISUAL = "visual"


@dataclass(frozen=True)
class Sensor:
    kind: SensorKind
    region: tuple[int, ...] = ()
    name: str = ""

    def __post_init__(self):
        object.__setattr__(self, "kind", SensorKind(self.kind))
        try:
            region = tuple(self.region)
        except TypeError:
            raise ValueError("sensor coordinates must be non-negative integers") from None
        object.__setattr__(self, "region", region)
        if any(type(n) is not int or n < 0 for n in self.region):
            raise ValueError("sensor coordinates must be non-negative integers")
        if not isinstance(self.name, str):
            raise ValueError("sensor name must be text")


@dataclass(frozen=True)
class Target:
    count: int
    position: Point | None = None
    selected: bool = False

    def __post_init__(self):
        _identity(self.count, "target count")
        object.__setattr__(self, "position", _point(self.position))
        if type(self.selected) is not bool:
            raise ValueError("selected must be boolean")


@dataclass(frozen=True)
class Marker:
    position: Point | None
    reached: bool = False

    def __post_init__(self):
        object.__setattr__(self, "position", _point(self.position))
        if type(self.reached) is not bool:
            raise ValueError("reached must be boolean")


class Quality(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    ERROR = "error"


@dataclass(frozen=True)
class Reading:
    value: Any = None
    quality: Quality = Quality.KNOWN
    detail: str = ""
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "quality", Quality(self.quality))
        object.__setattr__(self, "value", _freeze(self.value))
        _text(self.detail, "reading detail")
        if not isinstance(self.diagnostics, Mapping):
            raise ValueError("reading diagnostics must be a mapping")
        object.__setattr__(self, "diagnostics", _freeze(self.diagnostics))


@dataclass(frozen=True)
class Observation:
    sensor: Sensor
    value: Any
    observed_at: float
    frame_id: int
    epoch: int
    quality: Quality = Quality.KNOWN
    detail: str = ""
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.sensor, Sensor):
            raise ValueError("observation sensor must be a Sensor")
        finite(self.observed_at, "observation timestamp")
        _identity(self.frame_id, "frame id")
        _identity(self.epoch, "epoch")
        object.__setattr__(self, "quality", Quality(self.quality))
        object.__setattr__(self, "value", _freeze(self.value))
        _text(self.detail, "observation detail")
        if not isinstance(self.diagnostics, Mapping):
            raise ValueError("observation diagnostics must be a mapping")
        object.__setattr__(self, "diagnostics", _freeze(self.diagnostics))

    def fresh(self, now: float, max_age: float) -> bool:
        return self.quality is Quality.KNOWN and 0 <= now - self.observed_at <= max_age


class ObservationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    observations: Mapping[Sensor, Observation] = field(default_factory=dict)
    frame_id: int = 0
    epoch: int = 0
    created_at: float = 0.0

    def __post_init__(self):
        _identity(self.frame_id, "snapshot frame id")
        _identity(self.epoch, "snapshot epoch")
        try:
            observations = dict(self.observations)
        except (TypeError, ValueError):
            raise ValueError("snapshot observations must be a mapping") from None
        created_at = self.created_at
        finite(created_at, "snapshot timestamp")
        for sensor, observation in observations.items():
            if not isinstance(sensor, Sensor) or not isinstance(observation, Observation):
                raise ValueError("snapshot must map Sensor values to Observation values")
            if (
                observation.sensor != sensor
                or observation.epoch != self.epoch
                or observation.frame_id > self.frame_id
                or observation.observed_at > created_at
            ):
                raise ValueError("snapshot contains inconsistent observation identity")
        object.__setattr__(self, "observations", MappingProxyType(observations))

    def observation(self, sensor: Sensor) -> Observation:
        try:
            return self.observations[sensor]
        except KeyError:
            raise ObservationUnavailable(f"{sensor.kind.value}: no observation") from None

    def require(self, sensor: Sensor, *, now: float, max_age: float = 0.5) -> Any:
        obs = self.observation(sensor)
        if obs.epoch != self.epoch or not obs.fresh(now, max_age):
            raise ObservationUnavailable(
                f"{sensor.kind.value}: {obs.detail or 'unknown or stale observation'}"
            )
        return obs.value


@dataclass(frozen=True)
class ObservationBatch:
    readings: Mapping[Sensor, Reading]
    observed_at: float
    # Optional immutable Frame for recording; domain features never receive it.
    frame: Any = None

    def __post_init__(self):
        finite(self.observed_at, "batch timestamp")
        try:
            readings = dict(self.readings)
        except (TypeError, ValueError):
            raise ValueError("batch readings must be a mapping") from None
        if any(
            not isinstance(sensor, Sensor) or not isinstance(reading, Reading)
            for sensor, reading in readings.items()
        ):
            raise ValueError("batch must map Sensor values to Reading values")
        object.__setattr__(self, "readings", MappingProxyType(readings))


class ActionKind(str, Enum):
    PRESS = "press"
    LEFT_CLICK = "left_click"
    RIGHT_CLICK = "right_click"
    DRAG = "drag"


class Priority(IntEnum):
    BACKGROUND = 10
    NAVIGATION = 20
    LOOT = 30
    COMBAT = 50
    SUPPORT = 70
    HEAL = 100


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    key: str = ""
    point: Point | None = None
    destination: Point | None = None

    def __post_init__(self):
        object.__setattr__(self, "kind", ActionKind(self.kind))
        if not isinstance(self.key, str):
            raise ValueError("action key must be text")
        for name in ("point", "destination"):
            point = getattr(self, name)
            if point is not None:
                try:
                    point = tuple(point)
                except TypeError:
                    raise ValueError(
                        "action coordinates must be non-negative integer pairs"
                    ) from None
                if len(point) != 2 or any(type(n) is not int or n < 0 for n in point):
                    raise ValueError("action coordinates must be non-negative integer pairs")
                object.__setattr__(self, name, point)
        if self.kind is ActionKind.PRESS and (
            not isinstance(self.key, str) or not self.key.strip()
        ):
            raise ValueError("press action needs a key")
        if self.kind is not ActionKind.PRESS and self.point is None:
            raise ValueError("mouse action needs a client-local point")
        if self.kind is ActionKind.DRAG and self.destination is None:
            raise ValueError("drag action needs a destination")
        if self.kind is ActionKind.PRESS and (
            self.point is not None or self.destination is not None
        ):
            raise ValueError("press action cannot contain mouse coordinates")
        if (
            self.kind in (ActionKind.LEFT_CLICK, ActionKind.RIGHT_CLICK)
            and self.destination is not None
        ):
            raise ValueError("click action cannot contain a destination")
        if self.kind is not ActionKind.PRESS and self.key:
            raise ValueError("mouse action cannot contain a key")


@dataclass(frozen=True)
class Requirement:
    sensor: Sensor
    max_age: float = 0.5

    def __post_init__(self):
        if not isinstance(self.sensor, Sensor):
            raise ValueError("requirement sensor must be a Sensor")
        finite(self.max_age, "observation max age", positive=True)


@dataclass(frozen=True)
class Confirmation:
    sensor: Sensor
    expected: Any
    field: str = ""
    timeout: float = 2.0

    def __post_init__(self):
        if not isinstance(self.sensor, Sensor):
            raise ValueError("confirmation sensor must be a Sensor")
        finite(self.timeout, "confirmation timeout", positive=True)
        object.__setattr__(self, "expected", _freeze(self.expected))
        _text(self.field, "confirmation field")
        if self.field and (not self.field.isidentifier() or self.field.startswith("_")):
            raise ValueError("confirmation field must be public")


@dataclass(frozen=True)
class Request:
    key: str
    action: Action
    priority: int = Priority.BACKGROUND
    ttl: float = 0.5
    requirements: tuple[Requirement, ...] = ()
    confirmation: Confirmation | None = None
    reason: str = ""

    def __post_init__(self):
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("request key must be non-empty")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ValueError("priority must be an integer")
        if not isinstance(self.action, Action):
            raise ValueError("request action must be an Action")
        finite(self.ttl, "request ttl", positive=True)
        object.__setattr__(self, "requirements", tuple(self.requirements))
        if any(not isinstance(item, Requirement) for item in self.requirements):
            raise ValueError("request requirements must be Requirement values")
        if self.confirmation is not None and not isinstance(self.confirmation, Confirmation):
            raise ValueError("request confirmation must be a Confirmation")
        _text(self.reason, "request reason")


class ResultState(str, Enum):
    EXECUTED = "executed"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True)
class ActionResult:
    request_id: str
    owner: str
    key: str
    state: ResultState
    at: float
    reason: str = ""
    final: bool = True

    def __post_init__(self):
        _text(self.request_id, "result request_id", nonempty=True)
        _text(self.owner, "result owner", nonempty=True)
        _text(self.key, "result key", nonempty=True)
        object.__setattr__(self, "state", ResultState(self.state))
        finite(self.at, "result timestamp")
        _text(self.reason, "result reason")
        if type(self.final) is not bool:
            raise ValueError("result final must be boolean")
        if not self.final and self.state is not ResultState.EXECUTED:
            raise ValueError("only an executed result may be non-final")


@dataclass(frozen=True)
class Step:
    requests: tuple[Request, ...] = ()
    cancel_keys: tuple[str, ...] = ()
    phase: str = "Running"
    finished: bool = False
    error: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "requests", tuple(self.requests))
        object.__setattr__(self, "cancel_keys", tuple(self.cancel_keys))
        if any(not isinstance(item, Request) for item in self.requests):
            raise ValueError("step requests must be Request values")
        if any(not isinstance(key, str) or not key.strip() for key in self.cancel_keys):
            raise ValueError("cancellation keys must be non-empty text")
        if self.error is not None and (not isinstance(self.error, str) or not self.error.strip()):
            raise ValueError("step error must be non-empty text")
        _text(self.phase, "step phase", nonempty=True)
        if type(self.finished) is not bool:
            raise ValueError("step finished must be boolean")


@dataclass(frozen=True)
class FeatureContext:
    snapshot: Snapshot
    now: float

    def __post_init__(self):
        if not isinstance(self.snapshot, Snapshot):
            raise ValueError("feature context snapshot must be a Snapshot")
        finite(self.now, "feature context timestamp")

    def require(self, sensor: Sensor, max_age: float = 0.5) -> Any:
        return self.snapshot.require(sensor, now=self.now, max_age=max_age)


@dataclass(frozen=True)
class FeatureStatus:
    name: str
    generation: int
    active: bool
    phase: str
    error: str | None = None

    def __post_init__(self):
        _text(self.name, "feature status name", nonempty=True)
        _identity(self.generation, "feature generation")
        if type(self.active) is not bool:
            raise ValueError("feature active must be boolean")
        _text(self.phase, "feature phase", nonempty=True)
        if self.error is not None:
            _text(self.error, "feature error", nonempty=True)


@dataclass(frozen=True)
class Event:
    sequence: int
    at: float
    kind: str
    owner: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        _identity(self.sequence, "event sequence")
        finite(self.at, "event timestamp")
        _text(self.kind, "event kind", nonempty=True)
        _text(self.owner, "event owner")
        try:
            data = dict(self.data)
        except (TypeError, ValueError):
            raise ValueError("event data must be a mapping") from None
        if any(not isinstance(key, str) for key in data):
            raise ValueError("event data keys must be text")
        object.__setattr__(self, "data", _freeze(data))
