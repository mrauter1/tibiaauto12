"""Strict, portable, data-only hunt-pack documents.

Machine bindings are deliberately represented by :class:`LocalBinding` and
never serialized.  Parsing a pack therefore cannot select a window, inherit a
calibration, or make the pack ready to activate on another computer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Callable, Mapping

from core.paths import project_path
from conf.HexMapKeys import KeyToHex
from engine.CaveBot.routes import Route, RouteError, route_from_data


HUNT_PACK_SCHEMA = "tibiaauto.hunt-pack"
HUNT_PACK_VERSION = 1
MAX_PACK_BYTES = 512 * 1024
MAX_WAYPOINTS = 2000
MAX_TARGETS = 4
MAX_CAPABILITIES = 64
MAX_TEXT = 160


class HuntPackError(ValueError):
    """A hunt-pack document or activation request is invalid."""


class MissingCapabilities(HuntPackError):
    """The selected local profile lacks required hunt-pack capabilities."""


def _plain_text(value: Any, label: str, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HuntPackError(f"{label} must be non-empty text")
    value = value.strip()
    if len(value) > maximum or any(ord(char) < 32 for char in value):
        raise HuntPackError(f"{label} is too long or contains control characters")
    return value


def _asset_name(value: Any, label: str) -> str:
    value = _plain_text(value, label)
    if Path(value).name != value or "/" in value or "\\" in value or value in (".", ".."):
        raise HuntPackError(f"{label} must be a bundled asset name, not a path")
    return value


def _number(value: Any, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HuntPackError(f"{label} must be a number")
    parsed = float(value)
    if not math.isfinite(parsed) or not low <= parsed <= high:
        raise HuntPackError(f"{label} must be finite and between {low:g} and {high:g}")
    return parsed


def _integer(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise HuntPackError(f"{label} must be a whole number between {low} and {high}")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise HuntPackError(f"{label} must be true or false")
    return value


def _keys(raw: Any, label: str, expected: set[str], optional: set[str] = frozenset()) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise HuntPackError(f"{label} must be an object")
    names = set(raw)
    if any(not isinstance(name, str) for name in names):
        raise HuntPackError(f"{label} field names must be text")
    missing = expected - names
    unknown = names - expected - optional
    if missing:
        raise HuntPackError(f"{label} is missing field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise HuntPackError(f"{label} has unknown field(s): {', '.join(sorted(unknown))}")
    return raw


def _hotkey(value: Any, label: str) -> str:
    value = _plain_text(value, label, maximum=40)
    parts = [part.strip() for part in value.split("+")]
    valid = (
        len(parts) == 1 and parts[0] in KeyToHex
        or len(parts) == 2 and parts[0] in {"Ctrl", "Shift"} and parts[1] in KeyToHex
    )
    if not valid:
        raise HuntPackError(f"{label} is not a supported hotkey")
    return value


@dataclass(frozen=True)
class Target:
    name: str
    priority: int

    def __post_init__(self) -> None:
        name = _plain_text(self.name, "target name")
        _integer(self.priority, "target priority", 0, 100)
        object.__setattr__(self, "name", name)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "priority": self.priority}


@dataclass(frozen=True)
class ThresholdSetting:
    enabled: bool
    percent: int
    hotkey: str

    def __post_init__(self) -> None:
        _boolean(self.enabled, "threshold enabled")
        _integer(self.percent, "threshold percent", 0, 100)
        object.__setattr__(self, "hotkey", _hotkey(self.hotkey, "threshold hotkey"))

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "percent": self.percent, "hotkey": self.hotkey}


@dataclass(frozen=True)
class TimedSpellSetting:
    hotkey: str
    interval_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "hotkey", _hotkey(self.hotkey, "timed spell hotkey"))
        object.__setattr__(
            self, "interval_seconds", _number(self.interval_seconds, "spell interval", 0.1, 86400)
        )

    def as_dict(self) -> dict[str, Any]:
        return {"hotkey": self.hotkey, "interval_seconds": self.interval_seconds}


@dataclass(frozen=True)
class SupportSettings:
    healing: tuple[ThresholdSetting, ...] = ()
    mana: tuple[ThresholdSetting, ...] = ()
    haste_hotkey: str = "F6"
    food_hotkey: str = "Shift + F12"
    timed_spells: tuple[TimedSpellSetting, ...] = ()
    timed_spells_immediate: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "healing", tuple(self.healing))
        object.__setattr__(self, "mana", tuple(self.mana))
        object.__setattr__(self, "timed_spells", tuple(self.timed_spells))
        if len(self.healing) != 3 or len(self.mana) != 2:
            raise HuntPackError("support settings require three healing and two mana thresholds")
        if any(not isinstance(item, ThresholdSetting) for item in self.healing + self.mana):
            raise HuntPackError("support thresholds have the wrong type")
        if len(self.timed_spells) > 32 or any(
            not isinstance(item, TimedSpellSetting) for item in self.timed_spells
        ):
            raise HuntPackError("timed spells must contain at most 32 valid settings")
        hotkeys = [item.hotkey for item in self.timed_spells]
        if len(hotkeys) != len(set(hotkeys)):
            raise HuntPackError("timed spell hotkeys must be unique")
        object.__setattr__(self, "haste_hotkey", _hotkey(self.haste_hotkey, "haste hotkey"))
        object.__setattr__(self, "food_hotkey", _hotkey(self.food_hotkey, "food hotkey"))
        _boolean(self.timed_spells_immediate, "timed spell immediate setting")

    def as_dict(self) -> dict[str, Any]:
        return {
            "healing": [rule.as_dict() for rule in self.healing],
            "mana": [rule.as_dict() for rule in self.mana],
            "haste_hotkey": self.haste_hotkey,
            "food_hotkey": self.food_hotkey,
            "timed_spells": [spell.as_dict() for spell in self.timed_spells],
            "timed_spells_immediate": self.timed_spells_immediate,
        }

    def loader_updates(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {
            "HealthLoader": {}, "ManaLoader": {},
            "HurLoader": {"HotkeyHur": self.haste_hotkey},
            "FoodEaterLoader": {"HotkeyFood": self.food_hotkey},
            "TimedSpellsLoader": {
                "InstantExecute": self.timed_spells_immediate,
                "Spells": [[spell.hotkey, spell.interval_seconds] for spell in self.timed_spells],
            },
        }
        words = ("One", "Two", "Three")
        for word, rule in zip(words, self.healing):
            result["HealthLoader"].update({
                f"LifeCheckStage{word}": rule.enabled,
                f"LifePercentageStage{word}": rule.percent,
                f"LifeHotkeyStage{word}": rule.hotkey,
            })
        for word, rule in zip(words, self.mana):
            result["ManaLoader"].update({
                f"ManaCheckStage{word}": rule.enabled,
                f"ManaPercentageStage{word}": rule.percent,
                f"ManaHotkeyStage{word}": rule.hotkey,
            })
        return result


@dataclass(frozen=True)
class CaveBotSettings:
    walking: bool = True
    follow: bool = True
    loot: bool = True
    stand_seconds: int = 0

    def __post_init__(self) -> None:
        _boolean(self.walking, "walking")
        _boolean(self.follow, "follow")
        _boolean(self.loot, "loot")
        object.__setattr__(
            self, "stand_seconds", _integer(self.stand_seconds, "stand seconds", 0, 3600)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "walking": self.walking,
            "follow": self.follow,
            "loot": self.loot,
            "stand_seconds": self.stand_seconds,
        }


@dataclass(frozen=True)
class SupplyPolicy:
    state: str = "inactive"

    def __post_init__(self) -> None:
        if self.state != "inactive":
            raise HuntPackError("supply policy is reserved for future use and must be inactive")

    def as_dict(self) -> dict[str, str]:
        return {"state": self.state}


@dataclass(frozen=True)
class LocalBinding:
    """A machine-local profile/calibration selection; never part of pack JSON."""

    profile: str
    calibration: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile", _plain_text(self.profile, "local profile"))
        object.__setattr__(self, "calibration", _plain_text(self.calibration, "local calibration"))


def _press_capability(hotkey: str) -> str:
    return "modified_press" if "+" in hotkey else "press"


def required_capabilities_for(
    route: Route,
    targets: tuple[Target, ...],
    support: SupportSettings,
    cavebot: CaveBotSettings,
) -> tuple[str, ...]:
    """Derive the minimum honest capability declaration for portable settings."""
    required: set[str] = set()
    if route.waypoints:
        required.update(("sensor.marker", "left_click"))
    if targets:
        required.update(("sensor.target", "battle_list_click"))
        if cavebot.loot:
            required.add("right_click")
    if cavebot.follow:
        required.add("sensor.follow")
    for rule in support.healing:
        if rule.enabled:
            required.update(("sensor.health", _press_capability(rule.hotkey)))
    for rule in support.mana:
        if rule.enabled:
            required.update(("sensor.mana", _press_capability(rule.hotkey)))
    # Haste and food are configured support workflows in every v1 pack. Their
    # runtime activation remains explicit, but the pack may not hide what they
    # require from the profile compatibility check.
    required.update(("sensor.hasted", _press_capability(support.haste_hotkey)))
    required.update(("sensor.hungry", _press_capability(support.food_hotkey)))
    for spell in support.timed_spells:
        required.add(_press_capability(spell.hotkey))
    return tuple(sorted(required))


@dataclass(frozen=True)
class HuntPack:
    name: str
    revision: int
    route: Route
    targets: tuple[Target, ...]
    support: SupportSettings
    cavebot: CaveBotSettings = field(default_factory=CaveBotSettings)
    supply_policy: SupplyPolicy = field(default_factory=SupplyPolicy)
    required_capabilities: tuple[str, ...] = ()
    version: int = HUNT_PACK_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _asset_name(self.name, "hunt-pack name"))
        _integer(self.revision, "hunt-pack revision", 1, 2**31 - 1)
        if not isinstance(self.route, Route):
            raise HuntPackError("route has the wrong type")
        _plain_text(self.route.name, "route name")
        for index, waypoint in enumerate(self.route.waypoints):
            _plain_text(waypoint.id, f"waypoint {index + 1} id")
            _asset_name(waypoint.mark, f"waypoint {index + 1} marker")
        self.route.validate_assets()
        object.__setattr__(self, "targets", tuple(self.targets))
        if len(self.route.waypoints) > MAX_WAYPOINTS:
            raise HuntPackError(f"route exceeds {MAX_WAYPOINTS} waypoints")
        if len(self.targets) > MAX_TARGETS or any(not isinstance(item, Target) for item in self.targets):
            raise HuntPackError(f"targets must contain at most {MAX_TARGETS} valid entries")
        names = [target.name for target in self.targets]
        if len(names) != len(set(names)):
            raise HuntPackError("target names must be unique")
        if not isinstance(self.support, SupportSettings) or not isinstance(self.cavebot, CaveBotSettings):
            raise HuntPackError("support or CaveBot settings have the wrong type")
        if not isinstance(self.supply_policy, SupplyPolicy):
            raise HuntPackError("supply policy has the wrong type")
        capabilities = tuple(self.required_capabilities)
        if len(capabilities) > MAX_CAPABILITIES:
            raise HuntPackError(f"required capabilities exceed {MAX_CAPABILITIES}")
        for capability in capabilities:
            value = _plain_text(capability, "required capability", maximum=80)
            if not all(char.islower() or char.isdigit() or char in "._-" for char in value):
                raise HuntPackError(f"invalid required capability: {value!r}")
        if len(capabilities) != len(set(capabilities)):
            raise HuntPackError("required capabilities must be unique")
        implied = set(required_capabilities_for(self.route, self.targets, self.support, self.cavebot))
        undeclared = sorted(implied - set(capabilities))
        if undeclared:
            raise HuntPackError(
                "required_capabilities omits setting requirement(s): " + ", ".join(undeclared)
            )
        object.__setattr__(self, "required_capabilities", capabilities)
        if type(self.version) is not int or self.version != HUNT_PACK_VERSION:
            raise HuntPackError(f"unsupported hunt-pack version: {self.version!r}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": HUNT_PACK_SCHEMA,
            "version": self.version,
            "name": self.name,
            "revision": self.revision,
            "route": self.route.as_dict(),
            "targets": [target.as_dict() for target in self.targets],
            "cavebot": self.cavebot.as_dict(),
            "support": self.support.as_dict(),
            "supply_policy": self.supply_policy.as_dict(),
            "required_capabilities": list(self.required_capabilities),
        }

    def to_json(self) -> str:
        payload = json.dumps(self.as_dict(), indent=2, allow_nan=False, sort_keys=True) + "\n"
        if len(payload.encode("utf-8")) > MAX_PACK_BYTES:
            raise HuntPackError(f"hunt pack exceeds {MAX_PACK_BYTES} bytes")
        return payload

    def validate_for_activation(
        self, binding: LocalBinding | None, available_capabilities: set[str] | frozenset[str]
    ) -> None:
        if not isinstance(binding, LocalBinding):
            raise HuntPackError("select and validate a local profile/calibration before activation")
        if not isinstance(available_capabilities, (set, frozenset)) or any(
            not isinstance(item, str) for item in available_capabilities
        ):
            raise HuntPackError("available capabilities must be a set of names")
        missing = sorted(set(self.required_capabilities) - set(available_capabilities))
        if missing:
            raise MissingCapabilities("selected profile is missing capability/capabilities: " + ", ".join(missing))


def _threshold(raw: Any, label: str) -> ThresholdSetting:
    raw = _keys(raw, label, {"enabled", "percent", "hotkey"})
    return ThresholdSetting(
        _boolean(raw["enabled"], f"{label} enabled"),
        _integer(raw["percent"], f"{label} percent", 0, 100),
        _hotkey(raw["hotkey"], f"{label} hotkey"),
    )


def _support(raw: Any) -> SupportSettings:
    raw = _keys(raw, "support", {
        "healing", "mana", "haste_hotkey", "food_hotkey", "timed_spells",
        "timed_spells_immediate",
    })
    if not isinstance(raw["healing"], list) or not isinstance(raw["mana"], list):
        raise HuntPackError("support healing and mana settings must be lists")
    if not isinstance(raw["timed_spells"], list) or len(raw["timed_spells"]) > 32:
        raise HuntPackError("support timed_spells must be a list of at most 32 entries")
    spells = []
    for index, entry in enumerate(raw["timed_spells"]):
        entry = _keys(entry, f"timed spell {index + 1}", {"hotkey", "interval_seconds"})
        spells.append(TimedSpellSetting(
            _hotkey(entry["hotkey"], f"timed spell {index + 1} hotkey"),
            _number(entry["interval_seconds"], f"timed spell {index + 1} interval", 0.1, 86400),
        ))
    return SupportSettings(
        tuple(_threshold(item, f"healing rule {index + 1}") for index, item in enumerate(raw["healing"])),
        tuple(_threshold(item, f"mana rule {index + 1}") for index, item in enumerate(raw["mana"])),
        _hotkey(raw["haste_hotkey"], "haste hotkey"),
        _hotkey(raw["food_hotkey"], "food hotkey"),
        tuple(spells),
        _boolean(raw["timed_spells_immediate"], "timed spell immediate setting"),
    )


def hunt_pack_from_data(data: Any) -> HuntPack:
    raw = _keys(data, "hunt pack", {
        "schema", "version", "name", "revision", "route", "targets", "cavebot",
        "support", "supply_policy", "required_capabilities",
    })
    if raw["schema"] != HUNT_PACK_SCHEMA:
        raise HuntPackError("unsupported or missing hunt-pack schema")
    if type(raw["version"]) is not int or raw["version"] != HUNT_PACK_VERSION:
        raise HuntPackError("unsupported hunt-pack version")
    name = _asset_name(raw["name"], "hunt-pack name")
    # Hunt packs use the versioned schema; the standalone route reader still
    # accepts historical list routes and their migration defaults.
    route_raw = _keys(raw["route"], "route", {"version", "name", "waypoints"})
    if not isinstance(route_raw["waypoints"], list):
        raise HuntPackError("route waypoints must be a list")
    for index, entry in enumerate(route_raw["waypoints"], 1):
        waypoint = _keys(entry, f"waypoint {index}", {"id", "mark", "action"})
        _plain_text(waypoint["id"], f"waypoint {index} id")
        _plain_text(waypoint["action"], f"waypoint {index} action")
    route_name = route_raw.get("name")
    try:
        route = route_from_data(route_name, route_raw, validate_assets=True)
    except (RouteError, TypeError) as exc:
        raise HuntPackError(f"invalid hunt-pack route: {exc}") from exc
    if len(route.waypoints) > MAX_WAYPOINTS:
        raise HuntPackError(f"route exceeds {MAX_WAYPOINTS} waypoints")
    if not isinstance(raw["targets"], list) or len(raw["targets"]) > MAX_TARGETS:
        raise HuntPackError(f"targets must be a list of at most {MAX_TARGETS} entries")
    targets = []
    for index, item in enumerate(raw["targets"]):
        item = _keys(item, f"target {index + 1}", {"name", "priority"})
        targets.append(Target(item["name"], _integer(item["priority"], "target priority", 0, 100)))
    cavebot_raw = _keys(raw["cavebot"], "cavebot", {"walking", "follow", "loot", "stand_seconds"})
    supply_raw = _keys(raw["supply_policy"], "supply policy", {"state"})
    if not isinstance(raw["required_capabilities"], list):
        raise HuntPackError("required_capabilities must be a list")
    return HuntPack(
        name,
        _integer(raw["revision"], "hunt-pack revision", 1, 2**31 - 1),
        route,
        tuple(targets),
        _support(raw["support"]),
        CaveBotSettings(
            _boolean(cavebot_raw["walking"], "walking"),
            _boolean(cavebot_raw["follow"], "follow"),
            _boolean(cavebot_raw["loot"], "loot"),
            _integer(cavebot_raw["stand_seconds"], "stand seconds", 0, 3600),
        ),
        SupplyPolicy(supply_raw["state"]),
        tuple(raw["required_capabilities"]),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HuntPackError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def hunt_pack_from_json(payload: str | bytes) -> HuntPack:
    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
    elif isinstance(payload, bytes):
        encoded = payload
    else:
        raise HuntPackError("hunt-pack JSON must be text or bytes")
    if len(encoded) > MAX_PACK_BYTES:
        raise HuntPackError(f"hunt pack exceeds {MAX_PACK_BYTES} bytes")
    try:
        data = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(HuntPackError(f"non-finite number: {value}")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise HuntPackError(f"invalid hunt-pack JSON: {exc}") from exc
    return hunt_pack_from_data(data)


def load_hunt_pack(path: str | Path) -> HuntPack:
    selected = Path(path)
    try:
        # Nonblocking/no-follow open avoids blocking on a named pipe or reading
        # a swapped symlink. Size and read the same regular-file descriptor.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(selected, flags)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise HuntPackError("hunt pack must be a regular file")
            if metadata.st_size > MAX_PACK_BYTES:
                raise HuntPackError(f"hunt pack exceeds {MAX_PACK_BYTES} bytes")
            payload = stream.read(MAX_PACK_BYTES + 1)
        if len(payload) > MAX_PACK_BYTES:
            raise HuntPackError(f"hunt pack exceeds {MAX_PACK_BYTES} bytes")
        return hunt_pack_from_json(payload)
    except HuntPackError:
        raise
    except OSError as exc:
        raise HuntPackError(f"cannot read hunt pack: {exc}") from exc


def save_hunt_pack(pack: HuntPack, path: str | Path, *, overwrite: bool = False) -> Path:
    if not isinstance(pack, HuntPack):
        raise HuntPackError("only a validated HuntPack can be exported")
    destination = Path(path)
    if destination.exists() and not overwrite:
        raise HuntPackError("destination exists; explicit overwrite is required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = pack.to_json()
    descriptor, temporary = tempfile.mkstemp(prefix="." + destination.name, suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, destination)
        else:
            try:
                # Atomic publication that cannot replace a destination created
                # after the initial UI check.
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise HuntPackError("destination exists; explicit overwrite is required") from exc
            os.unlink(temporary)
        temporary = ""
    except BaseException:
        try:
            if temporary:
                os.unlink(temporary)
        except OSError:
            pass
        raise
    return destination


def support_from_loaders(read_loader: Callable[[str], Mapping[str, Any]]) -> SupportSettings:
    health, mana = read_loader("HealthLoader"), read_loader("ManaLoader")
    words = ("One", "Two", "Three")
    healing = tuple(ThresholdSetting(
        bool(health.get(f"LifeCheckStage{word}", False)),
        int(health.get(f"LifePercentageStage{word}", 50)),
        str(health.get(f"LifeHotkeyStage{word}", "F1")),
    ) for word in words)
    mana_rules = tuple(ThresholdSetting(
        bool(mana.get(f"ManaCheckStage{word}", False)),
        int(mana.get(f"ManaPercentageStage{word}", 50)),
        str(mana.get(f"ManaHotkeyStage{word}", "F1")),
    ) for word in words[:2])
    haste, food, timed = read_loader("HurLoader"), read_loader("FoodEaterLoader"), read_loader("TimedSpellsLoader")
    raw_spells = timed.get("Spells", [])
    if not isinstance(raw_spells, list):
        raise HuntPackError("saved timed spell settings must be a list")
    spells = tuple(TimedSpellSetting(str(row[0]), float(row[1])) for row in raw_spells)
    return SupportSettings(
        healing, mana_rules,
        str(haste.get("HotkeyHur", "F6")),
        str(food.get("HotkeyFood", "Shift + F12")),
        spells, bool(timed.get("InstantExecute", False)),
    )


def apply_support_to_loaders(
    settings: SupportSettings, write_loader: Callable[[str, Mapping[str, Any]], None]
) -> None:
    """Apply validated settings through the existing support-module stores."""
    if not isinstance(settings, SupportSettings):
        raise HuntPackError("support settings have the wrong type")
    for loader, values in settings.loader_updates().items():
        write_loader(loader, values)


def apply_support_to_loader_document(data: Any, settings: SupportSettings) -> Any:
    """Update one GUILoader document in memory for one atomic configuration write."""
    if not isinstance(data, dict) or not isinstance(data.get("Loaders"), dict):
        raise HuntPackError("support configuration document is invalid")
    if not isinstance(settings, SupportSettings):
        raise HuntPackError("support settings have the wrong type")
    updates = settings.loader_updates()
    for loader in updates:
        section = data["Loaders"].get(loader)
        if not isinstance(section, dict):
            raise HuntPackError(f"support configuration is missing {loader}")
    for loader, values in updates.items():
        section = data["Loaders"][loader]
        section.update(values)
    return data


__all__ = [
    "HUNT_PACK_SCHEMA", "HUNT_PACK_VERSION", "MAX_PACK_BYTES", "HuntPackError",
    "MissingCapabilities", "Target", "ThresholdSetting", "TimedSpellSetting",
    "SupportSettings", "CaveBotSettings", "SupplyPolicy", "LocalBinding", "HuntPack",
    "hunt_pack_from_data", "hunt_pack_from_json", "load_hunt_pack", "save_hunt_pack",
    "support_from_loaders", "apply_support_to_loaders", "apply_support_to_loader_document",
    "required_capabilities_for",
]
