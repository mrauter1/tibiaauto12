"""Strict, data-only plans for a closed-loop hunting lifecycle.

Plans deliberately contain no callbacks, coordinates, image paths, or profile
implementation details.  A plan names observations; the application binds
those names to :class:`~tibia_client.models.Sensor` values for one calibrated
client layout when it creates the feature.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from typing import Any, Mapping

from engine.CaveBot.routes import Route


HUNT_PLAN_VERSION = 1


class HuntPlanError(ValueError):
    """The hunt document is unsafe, ambiguous, or unsupported."""


class ConditionField(str, Enum):
    PRESENT = "present"
    COUNT = "count"


class ConditionOp(str, Enum):
    EQ = "eq"
    NE = "ne"
    GTE = "gte"
    LTE = "lte"
    CHANGED = "changed"
    INCREASED = "increased"
    DECREASED = "decreased"


class PlannedActionKind(str, Enum):
    PRESS = "press"
    LEFT_CLICK = "left_click"
    RIGHT_CLICK = "right_click"
    DRAG = "drag"


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or not value.isidentifier():
        raise HuntPlanError(f"{label} must be a non-empty identifier")
    return value


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise HuntPlanError(f"{label} must be an integer >= {minimum}")
    return value


def _number(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HuntPlanError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise HuntPlanError(f"{label} must be a finite {'positive' if positive else 'non-negative'} number")
    return result


def _strict(raw: object, label: str, required: set[str], optional: set[str] = set()) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise HuntPlanError(f"{label} must be an object")
    keys = set(raw)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise HuntPlanError(f"{label} is missing field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise HuntPlanError(f"{label} has unknown field(s): {', '.join(sorted(unknown))}")
    return raw


@dataclass(frozen=True)
class ObservationCondition:
    sensor_id: str
    field: ConditionField
    op: ConditionOp
    value: bool | int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sensor_id", _identifier(self.sensor_id, "condition sensor_id"))
        object.__setattr__(self, "field", ConditionField(self.field))
        object.__setattr__(self, "op", ConditionOp(self.op))
        relative = self.op in (ConditionOp.CHANGED, ConditionOp.INCREASED, ConditionOp.DECREASED)
        if relative and self.value is not None:
            raise HuntPlanError("relative conditions must not contain value")
        if not relative:
            if self.field is ConditionField.PRESENT and type(self.value) is not bool:
                raise HuntPlanError("present conditions require a boolean value")
            if self.field is ConditionField.COUNT and type(self.value) is not int:
                raise HuntPlanError("count conditions require an integer value")
        if self.field is ConditionField.PRESENT and self.op not in (
            ConditionOp.EQ,
            ConditionOp.NE,
            ConditionOp.CHANGED,
        ):
            raise HuntPlanError("present conditions support only eq, ne, or changed")


@dataclass(frozen=True)
class SupplyRule:
    id: str
    sensor_id: str
    return_below: int
    emergency_reserve: int
    refill_to: int
    optional: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _identifier(self.id, "supply id"))
        object.__setattr__(self, "sensor_id", _identifier(self.sensor_id, "supply sensor_id"))
        emergency = _integer(self.emergency_reserve, "emergency_reserve")
        threshold = _integer(self.return_below, "return_below")
        target = _integer(self.refill_to, "refill_to")
        if not emergency <= threshold <= target:
            raise HuntPlanError("supply thresholds must satisfy emergency_reserve <= return_below <= refill_to")
        if type(self.optional) is not bool:
            raise HuntPlanError("supply optional must be boolean")


@dataclass(frozen=True)
class GuardedAction:
    id: str
    kind: PlannedActionKind
    confirmation: ObservationCondition
    key: str = ""
    target_id: str | None = None
    destination_id: str | None = None
    require_present: tuple[str, ...] = ()
    timeout: float = 2.0
    retries: int = 1
    mutating: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _identifier(self.id, "action id"))
        object.__setattr__(self, "kind", PlannedActionKind(self.kind))
        if not isinstance(self.confirmation, ObservationCondition):
            raise HuntPlanError("action confirmation must be an ObservationCondition")
        required = tuple(_identifier(item, "required sensor id") for item in self.require_present)
        if len(required) != len(set(required)):
            raise HuntPlanError("action require_present values must be unique")
        object.__setattr__(self, "require_present", required)
        if self.kind is PlannedActionKind.PRESS:
            if not isinstance(self.key, str) or not self.key.strip():
                raise HuntPlanError("press actions require key")
            if self.target_id is not None or self.destination_id is not None:
                raise HuntPlanError("press actions cannot name target or destination")
        else:
            if self.key:
                raise HuntPlanError("mouse actions cannot contain key")
            object.__setattr__(self, "target_id", _identifier(self.target_id, "action target_id"))
            if self.kind is PlannedActionKind.DRAG:
                object.__setattr__(
                    self,
                    "destination_id",
                    _identifier(self.destination_id, "action destination_id"),
                )
            elif self.destination_id is not None:
                raise HuntPlanError("click actions cannot name destination")
        object.__setattr__(self, "timeout", _number(self.timeout, "action timeout", positive=True))
        _integer(self.retries, "action retries")
        if type(self.mutating) is not bool:
            raise HuntPlanError("action mutating must be boolean")


@dataclass(frozen=True)
class LootPolicy:
    """Verified stack transfers whose source and destination counts share units."""

    corpse_id: str
    container_id: str
    backpack_id: str
    capacity_id: str
    item_ids: tuple[str, ...]
    destination_count_id: str
    max_moves: int = 20
    open_timeout: float = 2.0
    transfer_timeout: float = 2.0

    def __post_init__(self) -> None:
        for field_name in ("corpse_id", "container_id", "backpack_id", "capacity_id"):
            object.__setattr__(self, field_name, _identifier(getattr(self, field_name), field_name))
        items = tuple(_identifier(item, "loot item id") for item in self.item_ids)
        if not items or len(items) != len(set(items)):
            raise HuntPlanError("loot item_ids must be a non-empty unique list")
        object.__setattr__(self, "item_ids", items)
        object.__setattr__(
            self,
            "destination_count_id",
            _identifier(self.destination_count_id, "destination_count_id"),
        )
        if self.destination_count_id in items:
            raise HuntPlanError("loot destination_count must differ from source item ids")
        _integer(self.max_moves, "loot max_moves", 1)
        object.__setattr__(self, "open_timeout", _number(self.open_timeout, "loot open_timeout", positive=True))
        object.__setattr__(self, "transfer_timeout", _number(self.transfer_timeout, "loot transfer_timeout", positive=True))


@dataclass(frozen=True)
class TravelSegment:
    route: Route
    before: tuple[GuardedAction, ...] = ()
    after: tuple[GuardedAction, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.route, Route) or not self.route.waypoints:
            raise HuntPlanError("travel segment route must be a non-empty Route")
        for name in ("before", "after"):
            actions = tuple(getattr(self, name))
            if any(not isinstance(item, GuardedAction) for item in actions):
                raise HuntPlanError(f"travel segment {name} must contain GuardedAction values")
            object.__setattr__(self, name, actions)


@dataclass(frozen=True)
class HuntPlan:
    id: str
    outbound: tuple[TravelSegment, ...]
    hunt: Route
    return_segments: tuple[TravelSegment, ...]
    supplies: tuple[SupplyRule, ...]
    prepare: tuple[GuardedAction, ...] = ()
    deposit: tuple[GuardedAction, ...] = ()
    sell: tuple[GuardedAction, ...] = ()
    refill: tuple[GuardedAction, ...] = ()
    loot: LootPolicy | None = None
    max_refill_rounds: int = 10
    version: int = HUNT_PLAN_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _identifier(self.id, "plan id"))
        if type(self.version) is not int or self.version != HUNT_PLAN_VERSION:
            raise HuntPlanError(f"unsupported hunt plan version: {self.version!r}")
        for name in ("outbound", "return_segments"):
            value = getattr(self, name)
            value = (value,) if isinstance(value, TravelSegment) else tuple(value)
            if not value or any(not isinstance(item, TravelSegment) for item in value):
                raise HuntPlanError(f"{name} must contain at least one TravelSegment")
            object.__setattr__(self, name, value)
        if not isinstance(self.hunt, Route) or not self.hunt.waypoints:
            raise HuntPlanError("hunt must be a non-empty Route")
        supplies = tuple(self.supplies)
        if not supplies or any(not isinstance(item, SupplyRule) for item in supplies):
            raise HuntPlanError("supplies must contain at least one SupplyRule")
        object.__setattr__(self, "supplies", supplies)
        supply_ids = [item.id for item in supplies]
        if len(supply_ids) != len(set(supply_ids)):
            raise HuntPlanError("supply ids must be unique")
        action_ids: list[str] = []
        for name in ("prepare", "deposit", "sell", "refill"):
            actions = tuple(getattr(self, name))
            if any(not isinstance(item, GuardedAction) for item in actions):
                raise HuntPlanError(f"{name} must contain GuardedAction values")
            object.__setattr__(self, name, actions)
            action_ids.extend(item.id for item in actions)
        for segment in self.outbound + self.return_segments:
            action_ids.extend(item.id for item in segment.before + segment.after)
        if len(action_ids) != len(set(action_ids)):
            raise HuntPlanError("action ids must be unique across the plan")
        if self.loot is not None and not isinstance(self.loot, LootPolicy):
            raise HuntPlanError("loot must be a LootPolicy or null")
        _integer(self.max_refill_rounds, "max_refill_rounds", 1)

    @property
    def fingerprint(self) -> str:
        payload = {
            "plan": self.as_dict(),
            "route_fingerprints": {
                "outbound": [segment.route.fingerprint for segment in self.outbound],
                "hunt": self.hunt.fingerprint,
                "return": [segment.route.fingerprint for segment in self.return_segments],
            },
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def sensor_ids(self) -> tuple[str, ...]:
        result = {item.sensor_id for item in self.supplies}
        for actions in (
            self.prepare,
            *(segment.before for segment in self.outbound),
            *(segment.after for segment in self.outbound),
            *(segment.before for segment in self.return_segments),
            *(segment.after for segment in self.return_segments),
            self.deposit,
            self.sell,
            self.refill,
        ):
            for action in actions:
                result.add(action.confirmation.sensor_id)
                result.update(action.require_present)
                if action.target_id:
                    result.add(action.target_id)
                if action.destination_id:
                    result.add(action.destination_id)
        if self.loot is not None:
            result.update((self.loot.corpse_id, self.loot.container_id, self.loot.backpack_id, self.loot.capacity_id))
            result.update(self.loot.item_ids)
            result.add(self.loot.destination_count_id)
        return tuple(sorted(result))

    def as_dict(self) -> dict[str, Any]:
        def condition(value: ObservationCondition) -> dict[str, Any]:
            result: dict[str, Any] = {"sensor": value.sensor_id, "field": value.field.value, "op": value.op.value}
            if value.value is not None:
                result["value"] = value.value
            return result

        def action(value: GuardedAction) -> dict[str, Any]:
            result: dict[str, Any] = {
                "id": value.id,
                "kind": value.kind.value,
                "confirmation": condition(value.confirmation),
                "require_present": list(value.require_present),
                "timeout": value.timeout,
                "retries": value.retries,
                "mutating": value.mutating,
            }
            if value.key:
                result["key"] = value.key
            if value.target_id:
                result["target"] = value.target_id
            if value.destination_id:
                result["destination"] = value.destination_id
            return result

        result: dict[str, Any] = {
            "version": self.version,
            "id": self.id,
            "travel": {
                "outbound": [
                    {"route": segment.route.name, "before": [action(item) for item in segment.before], "after": [action(item) for item in segment.after]}
                    for segment in self.outbound
                ],
                "return": [
                    {"route": segment.route.name, "before": [action(item) for item in segment.before], "after": [action(item) for item in segment.after]}
                    for segment in self.return_segments
                ],
            },
            "hunt_route": self.hunt.name,
            "max_refill_rounds": self.max_refill_rounds,
            "supplies": [
                {
                    "id": item.id,
                    "sensor": item.sensor_id,
                    "return_below": item.return_below,
                    "emergency_reserve": item.emergency_reserve,
                    "refill_to": item.refill_to,
                    "optional": item.optional,
                }
                for item in self.supplies
            ],
            "actions": {
                "prepare": [action(item) for item in self.prepare],
                "deposit": [action(item) for item in self.deposit],
                "sell": [action(item) for item in self.sell],
                "refill": [action(item) for item in self.refill],
            },
        }
        if self.loot is not None:
            result["loot"] = {
                "corpse": self.loot.corpse_id,
                "container": self.loot.container_id,
                "backpack": self.loot.backpack_id,
                "capacity": self.loot.capacity_id,
                "items": list(self.loot.item_ids),
                "max_moves": self.loot.max_moves,
                "open_timeout": self.loot.open_timeout,
                "transfer_timeout": self.loot.transfer_timeout,
            }
            result["loot"]["destination_count"] = self.loot.destination_count_id
        return result


def _condition(raw: object, label: str) -> ObservationCondition:
    value = _strict(raw, label, {"sensor", "field", "op"}, {"value"})
    return ObservationCondition(value["sensor"], value["field"], value["op"], value.get("value"))


def _action(raw: object, label: str) -> GuardedAction:
    value = _strict(
        raw,
        label,
        {"id", "kind", "confirmation"},
        {"key", "target", "destination", "require_present", "timeout", "retries", "mutating"},
    )
    present = value.get("require_present", ())
    if not isinstance(present, (list, tuple)):
        raise HuntPlanError(f"{label}.require_present must be a list")
    return GuardedAction(
        id=value["id"], kind=value["kind"], confirmation=_condition(value["confirmation"], f"{label}.confirmation"),
        key=value.get("key", ""), target_id=value.get("target"), destination_id=value.get("destination"),
        require_present=tuple(present), timeout=value.get("timeout", 2.0), retries=value.get("retries", 1),
        mutating=value.get("mutating", True),
    )


def hunt_plan_from_data(data: object, routes: Mapping[str, Route]) -> HuntPlan:
    """Parse a closed schema, resolving route names from already loaded Routes."""

    raw = _strict(
        data,
        "hunt plan",
        {"version", "id", "travel", "hunt_route", "supplies", "actions"},
        {"loot", "max_refill_rounds"},
    )
    travel_values = _strict(raw["travel"], "travel", {"outbound", "return"})
    segments: dict[str, tuple[TravelSegment, ...]] = {}
    for phase, segment_value in travel_values.items():
        raw_segments = segment_value if isinstance(segment_value, list) else [segment_value]
        if not raw_segments:
            raise HuntPlanError(f"travel.{phase} must contain at least one segment")
        parsed_segments = []
        for segment_index, raw_segment in enumerate(raw_segments):
            segment = _strict(raw_segment, f"travel.{phase}[{segment_index}]", {"route", "before", "after"})
            route_name = segment["route"]
            if not isinstance(route_name, str) or route_name not in routes:
                raise HuntPlanError(f"travel.{phase}[{segment_index}].route does not name a loaded route")
            before = segment["before"]
            after = segment["after"]
            if not isinstance(before, list) or not isinstance(after, list):
                raise HuntPlanError(f"travel.{phase}[{segment_index}] before and after must be lists")
            parsed_segments.append(TravelSegment(
                routes[route_name],
                tuple(_action(item, f"travel.{phase}[{segment_index}].before[{index}]") for index, item in enumerate(before)),
                tuple(_action(item, f"travel.{phase}[{segment_index}].after[{index}]") for index, item in enumerate(after)),
            ))
        segments[phase] = tuple(parsed_segments)
    hunt_name = raw["hunt_route"]
    if not isinstance(hunt_name, str) or hunt_name not in routes:
        raise HuntPlanError("hunt_route does not name a loaded route")
    supply_values = raw["supplies"]
    if not isinstance(supply_values, list):
        raise HuntPlanError("supplies must be a list")
    supplies = []
    for index, item in enumerate(supply_values):
        value = _strict(item, f"supplies[{index}]", {"id", "sensor", "return_below", "emergency_reserve", "refill_to"}, {"optional"})
        supplies.append(SupplyRule(value["id"], value["sensor"], value["return_below"], value["emergency_reserve"], value["refill_to"], value.get("optional", False)))
    groups = _strict(raw["actions"], "actions", {"prepare", "deposit", "sell", "refill"})
    parsed_groups: dict[str, tuple[GuardedAction, ...]] = {}
    for group, values in groups.items():
        if not isinstance(values, list):
            raise HuntPlanError(f"actions.{group} must be a list")
        parsed_groups[group] = tuple(_action(item, f"actions.{group}[{index}]") for index, item in enumerate(values))
    loot = None
    if raw.get("loot") is not None:
        value = _strict(raw["loot"], "loot", {"corpse", "container", "backpack", "capacity", "items", "destination_count"}, {"max_moves", "open_timeout", "transfer_timeout"})
        if not isinstance(value["items"], list):
            raise HuntPlanError("loot.items must be a list")
        loot = LootPolicy(
            value["corpse"], value["container"], value["backpack"], value["capacity"],
            tuple(value["items"]), value["destination_count"], value.get("max_moves", 20),
            value.get("open_timeout", 2.0), value.get("transfer_timeout", 2.0),
        )
    return HuntPlan(
        id=raw["id"], outbound=segments["outbound"], hunt=routes[hunt_name], return_segments=segments["return"],
        supplies=tuple(supplies), prepare=parsed_groups["prepare"], deposit=parsed_groups["deposit"], sell=parsed_groups["sell"], refill=parsed_groups["refill"], loot=loot,
        max_refill_rounds=raw.get("max_refill_rounds", 10), version=raw["version"],
    )


def hunt_plan_template() -> dict[str, Any]:
    """Return the exact v1 document shape with descriptive placeholder IDs."""

    return {
        "version": 1,
        "id": "my_hunt",
        "travel": {
            "outbound": [{"route": "outbound_route", "before": [], "after": []}],
            "return": [{"route": "return_route", "before": [], "after": []}],
        },
        "hunt_route": "hunt_route",
        "supplies": [{"id": "mana_potions", "sensor": "mana_potions", "return_below": 40, "emergency_reserve": 10, "refill_to": 200, "optional": False}],
        "actions": {"prepare": [], "deposit": [], "sell": [], "refill": []},
        "max_refill_rounds": 10,
        "loot": {
            "corpse": "loot_corpse", "container": "loot_container", "backpack": "loot_backpack",
            "capacity": "loot_free_slots", "destination_count": "loot_backpack_items",
            "items": ["gold_coin"], "max_moves": 20, "open_timeout": 2.0, "transfer_timeout": 2.0,
        },
    }


__all__ = [
    "ConditionField", "ConditionOp", "GuardedAction", "HUNT_PLAN_VERSION", "HuntPlan",
    "HuntPlanError", "LootPolicy", "ObservationCondition", "PlannedActionKind", "SupplyRule",
    "TravelSegment", "hunt_plan_from_data", "hunt_plan_template",
]
