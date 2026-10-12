"""Validated, immutable cavebot routes and independent progress checkpoints."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from core.paths import project_path, scripts_path


ROUTE_VERSION = 1
_RESERVED_ROUTE_NAMES = {"conf", "json", "guiloader", "defaultwalk", "void"}


class RouteError(ValueError):
    """A route or its checkpoint is invalid."""


class UnsupportedRouteAction(RouteError):
    """The route requests an action that cannot yet be confirmed reliably."""


class WaypointAction(str, Enum):
    WALK = "Walk"


_LEGACY_ACTIONS = {1: "Walk", 2: "Rope", 3: "Shovel"}


def _action(value: Any) -> WaypointAction:
    if isinstance(value, WaypointAction):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        value = _LEGACY_ACTIONS.get(value, value)
    if isinstance(value, str) and value.casefold() == "walk":
        return WaypointAction.WALK
    if value in (2, 3, "Rope", "Shovel", "rope", "shovel"):
        raise UnsupportedRouteAction(
            "Rope and Shovel waypoints are not executable until their result can be verified"
        )
    raise UnsupportedRouteAction("unsupported waypoint action: {!r}".format(value))


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise RouteError("route name must not be empty")
    clean = name.strip()
    if clean in (".", "..") or Path(clean).name != clean or any(c in clean for c in ("/", "\\")):
        raise RouteError("route name must be a file name, not a path")
    clean = clean[:-5] if clean.casefold().endswith(".json") else clean
    if not clean or clean.casefold().endswith((".json", ".state")):
        raise RouteError("route name is empty or uses a reserved suffix")
    if clean.casefold() in _RESERVED_ROUTE_NAMES:
        raise RouteError("route name is reserved by application configuration")
    return clean


@dataclass(frozen=True)
class Waypoint:
    id: str
    mark: str
    action: WaypointAction = WaypointAction.WALK

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise RouteError("waypoint id must not be empty")
        if not isinstance(self.mark, str) or not self.mark.strip():
            raise RouteError("waypoint mark must not be empty")
        if Path(self.mark).name != self.mark or any(c in self.mark for c in ("/", "\\")):
            raise RouteError("waypoint mark must be an asset name, not a path")
        object.__setattr__(self, "action", _action(self.action))


@dataclass(frozen=True)
class Route:
    name: str
    waypoints: tuple[Waypoint, ...]
    version: int = ROUTE_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _safe_name(self.name))
        object.__setattr__(self, "waypoints", tuple(self.waypoints))
        if isinstance(self.version, bool) or self.version != ROUTE_VERSION:
            raise RouteError("unsupported route version: {}".format(self.version))
        if any(not isinstance(waypoint, Waypoint) for waypoint in self.waypoints):
            raise RouteError("route waypoints must be Waypoint values")
        ids = [waypoint.id for waypoint in self.waypoints]
        if len(ids) != len(set(ids)):
            raise RouteError("waypoint ids must be unique")

    def validate_assets(self, exists: Optional[Callable[[Path], bool]] = None) -> None:
        exists = exists or Path.is_file
        missing = []
        for waypoint in self.waypoints:
            asset = Path(project_path(Path("images") / "MapSettings" / (waypoint.mark + ".png")))
            if not exists(asset):
                missing.append(waypoint.mark)
        if missing:
            raise RouteError("missing waypoint image(s): {}".format(", ".join(sorted(set(missing)))))

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "waypoints": [
                {"id": waypoint.id, "mark": waypoint.mark, "action": waypoint.action.value}
                for waypoint in self.waypoints
            ],
        }

    @property
    def fingerprint(self) -> str:
        definition = [
            {"id": waypoint.id, "mark": waypoint.mark, "action": waypoint.action.value}
            for waypoint in self.waypoints
        ]
        payload = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def route_from_data(name: str, data: Any, *, validate_assets: bool = True) -> Route:
    """Read versioned routes and the historical list/status representation."""
    clean_name = _safe_name(name)
    entries: Iterable[Mapping[str, Any]]
    legacy = isinstance(data, list)
    if legacy:
        if not data:
            entries = ()
        elif len(data) == 1 and isinstance(data[0], Mapping) and data[0].get("status") == "NotConfigured":
            if set(data[0]) != {"status"}:
                raise RouteError("NotConfigured legacy sentinel must not contain waypoint fields")
            entries = ()
        else:
            entries = data
    elif isinstance(data, dict):
        if isinstance(data.get("version"), bool) or data.get("version") != ROUTE_VERSION:
            raise RouteError("unsupported or missing route version")
        if data.get("name") not in (None, clean_name):
            raise RouteError("route name does not match its file name")
        entries = data.get("waypoints")
        if not isinstance(entries, list):
            raise RouteError("waypoints must be a list")
    else:
        raise RouteError("route document must be an object or legacy list")

    waypoints = []
    for index, raw in enumerate(entries):
        if not isinstance(raw, Mapping):
            raise RouteError("waypoint {} must be an object".format(index + 1))
        if legacy and "status" in raw and not isinstance(raw["status"], bool):
            raise RouteError("legacy waypoint {} status must be true or false".format(index + 1))
        allowed = {"mark", "type", "status", "id"} if legacy else {"id", "mark", "action"}
        unknown = set(raw) - allowed
        if unknown:
            raise RouteError(
                "waypoint {} has unknown field(s): {}".format(index + 1, ", ".join(sorted(unknown)))
            )
        mark = raw.get("mark")
        waypoint_id = raw.get("id") or "waypoint-{}".format(index + 1)
        waypoints.append(Waypoint(str(waypoint_id), mark, _action(raw.get("action", raw.get("type", 1)))))
    route = Route(clean_name, tuple(waypoints))
    if validate_assets:
        route.validate_assets()
    return route


def route_file(name: str) -> Path:
    return Path(scripts_path(_safe_name(name) + ".json"))


def checkpoint_file(name: str) -> Path:
    return Path(scripts_path(_safe_name(name) + ".state.json"))


def load_route(name: str, *, validate_assets: bool = True) -> Route:
    path = route_file(name)
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise RouteError("cannot load route {}: {}".format(path, exc)) from exc
    return route_from_data(name, data, validate_assets=validate_assets)


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=4)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def save_route(route: Route) -> Path:
    route.validate_assets()
    path = route_file(route.name)
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as stream:
                existing = json.load(stream)
            route_from_data(route.name, existing, validate_assets=False)
        except (OSError, json.JSONDecodeError, RouteError) as exc:
            raise RouteError("refusing to overwrite non-route file {}: {}".format(path, exc)) from exc
    _atomic_json(path, route.as_dict())
    return path


def load_checkpoint(route: Route) -> int:
    """Return the current waypoint index; stale/corrupt checkpoints fail closed."""
    path = checkpoint_file(route.name)
    if not path.exists() or not route.waypoints:
        return 0
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, Mapping):
            raise RouteError("checkpoint document must be an object")
        if isinstance(data.get("version"), bool) or data.get("version") != ROUTE_VERSION:
            raise RouteError("unsupported checkpoint version")
        if data.get("route") != route.name:
            raise RouteError("checkpoint belongs to another route")
        if data.get("fingerprint") != route.fingerprint:
            raise RouteError("checkpoint route definition has changed")
        waypoint_id = data["waypoint_id"]
        index = data["index"]
        if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index >= len(route.waypoints):
            raise RouteError("checkpoint index is outside the route")
        if route.waypoints[index].id != waypoint_id:
            # Reordering keeps resume tied to identity rather than silently changing destination.
            matches = [i for i, waypoint in enumerate(route.waypoints) if waypoint.id == waypoint_id]
            if not matches:
                raise RouteError("checkpoint waypoint no longer exists")
            index = matches[0]
        return index
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RouteError("invalid checkpoint {}: {}".format(path, exc)) from exc


def save_checkpoint(route: Route, index: int) -> Path:
    if not route.waypoints:
        raise RouteError("cannot checkpoint an empty route")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index >= len(route.waypoints):
        raise RouteError("checkpoint index is outside the route")
    path = checkpoint_file(route.name)
    _atomic_json(path, {
        "version": ROUTE_VERSION,
        "route": route.name,
        "fingerprint": route.fingerprint,
        "index": index,
        "waypoint_id": route.waypoints[index].id,
    })
    return path


def import_legacy_active_index(data: Any, route: Route) -> int:
    """Import the old mutable ``status`` cursor without retaining it in route data."""
    if not isinstance(data, list) or not route.waypoints:
        return 0
    active = [i for i, raw in enumerate(data) if isinstance(raw, Mapping) and raw.get("status") is True]
    if len(active) > 1:
        raise RouteError("legacy route has multiple active waypoints")
    return active[0] if active else 0
