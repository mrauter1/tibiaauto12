"""Pure route-editing operations used by the CaveBot UI.

The editor owns only an in-memory route and a selected row.  It never captures
the desktop, emits input, or writes files, which makes every edit reversible
until the operator presses Save.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import uuid

from engine.CaveBot.routes import Route, RouteError, Waypoint, WaypointAction


def _new_id() -> str:
    return "waypoint-" + uuid.uuid4().hex[:12]


@dataclass
class RouteEditor:
    route: Route
    selected_index: int = 0
    id_factory: Callable[[], str] = _new_id

    def __post_init__(self) -> None:
        if not isinstance(self.route, Route):
            raise RouteError("route editor requires a Route")
        if not callable(self.id_factory):
            raise RouteError("waypoint id factory must be callable")
        self.select(self.selected_index)

    @property
    def selected(self) -> Waypoint | None:
        if not self.route.waypoints:
            return None
        return self.route.waypoints[self.selected_index]

    def select(self, index: int) -> Waypoint | None:
        if not isinstance(index, int) or isinstance(index, bool):
            raise RouteError("waypoint selection must be a whole number")
        if not self.route.waypoints:
            self.selected_index = 0
            return None
        if index < 0 or index >= len(self.route.waypoints):
            raise RouteError("waypoint selection is outside the route")
        self.selected_index = index
        return self.selected

    def _unique_id(self) -> str:
        existing = {waypoint.id for waypoint in self.route.waypoints}
        for _ in range(32):
            try:
                candidate = self.id_factory()
            except (StopIteration, RuntimeError) as exc:
                raise RouteError("could not create a unique waypoint id") from exc
            if isinstance(candidate, str) and candidate.strip() and candidate not in existing:
                return candidate
        raise RouteError("could not create a unique waypoint id")

    def _replace(self, entries: list[Waypoint], selected: int) -> Waypoint | None:
        self.route = Route(self.route.name, tuple(entries))
        self.selected_index = selected if entries else 0
        return self.selected

    def rename(self, name: str) -> Route:
        self.route = Route(name, self.route.waypoints)
        return self.route

    def add_after(self, mark: str, action: WaypointAction = WaypointAction.WALK) -> Waypoint:
        waypoint = Waypoint(self._unique_id(), mark, action)
        entries = list(self.route.waypoints)
        position = self.selected_index + 1 if entries else 0
        entries.insert(position, waypoint)
        self._replace(entries, position)
        return waypoint

    def edit_marker(self, mark: str) -> Waypoint:
        current = self.selected
        if current is None:
            raise RouteError("select a waypoint before editing its marker")
        replacement = Waypoint(current.id, mark, current.action)
        entries = list(self.route.waypoints)
        entries[self.selected_index] = replacement
        self._replace(entries, self.selected_index)
        return replacement

    def duplicate_waypoint(self) -> Waypoint:
        current = self.selected
        if current is None:
            raise RouteError("select a waypoint before duplicating it")
        duplicate = Waypoint(self._unique_id(), current.mark, current.action)
        entries = list(self.route.waypoints)
        position = self.selected_index + 1
        entries.insert(position, duplicate)
        self._replace(entries, position)
        return duplicate

    def duplicate_route(self, name: str) -> Route:
        entries: list[Waypoint] = []
        seen = {waypoint.id for waypoint in self.route.waypoints}
        for waypoint in self.route.waypoints:
            for _ in range(32):
                try:
                    candidate = self.id_factory()
                except (StopIteration, RuntimeError) as exc:
                    raise RouteError("could not create unique ids for the duplicate route") from exc
                if isinstance(candidate, str) and candidate.strip() and candidate not in seen:
                    break
            else:
                raise RouteError("could not create unique ids for the duplicate route")
            seen.add(candidate)
            entries.append(Waypoint(candidate, waypoint.mark, waypoint.action))
        return Route(name, tuple(entries))

    def move(self, offset: int) -> Waypoint:
        if type(offset) is not int or offset not in (-1, 1):
            raise RouteError("waypoints can move only one row up or down")
        current = self.selected
        if current is None:
            raise RouteError("select a waypoint before moving it")
        destination = self.selected_index + offset
        if destination < 0 or destination >= len(self.route.waypoints):
            return current
        entries = list(self.route.waypoints)
        entries[self.selected_index], entries[destination] = (
            entries[destination], entries[self.selected_index]
        )
        self._replace(entries, destination)
        return self.selected  # type: ignore[return-value]

    def move_up(self) -> Waypoint:
        return self.move(-1)

    def move_down(self) -> Waypoint:
        return self.move(1)

    def remove(self) -> Waypoint:
        current = self.selected
        if current is None:
            raise RouteError("select a waypoint before removing it")
        entries = list(self.route.waypoints)
        removed = entries.pop(self.selected_index)
        selected = min(self.selected_index, max(0, len(entries) - 1))
        self._replace(entries, selected)
        return removed


__all__ = ["RouteEditor"]
