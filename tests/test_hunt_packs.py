from copy import deepcopy
from dataclasses import replace
import json
import os
from types import SimpleNamespace

import pytest

from engine.CaveBot.routes import Route, Waypoint
from tibia_client.hunt_packs import (
    CaveBotSettings,
    HuntPack,
    HuntPackError,
    LocalBinding,
    MissingCapabilities,
    SupplyPolicy,
    SupportSettings,
    Target,
    ThresholdSetting,
    TimedSpellSetting,
    MAX_PACK_BYTES,
    apply_support_to_loader_document,
    hunt_pack_from_data,
    hunt_pack_from_json,
    load_hunt_pack,
    save_hunt_pack,
)


def support():
    return SupportSettings(
        (
            ThresholdSetting(True, 85, "F1"),
            ThresholdSetting(True, 60, "F2"),
            ThresholdSetting(False, 40, "F3"),
        ),
        (
            ThresholdSetting(True, 70, "F3"),
            ThresholdSetting(False, 45, "F4"),
        ),
        "F6",
        "Shift + F12",
        (TimedSpellSetting("F7", 12.5),),
        True,
    )


def pack():
    settings = support()
    route = Route("rat-loop", (Waypoint("start", "Flag"), Waypoint("turn", "Star")))
    targets = (Target("Rat", 1), Target("CaveRat", 2))
    cavebot = CaveBotSettings(True, True, True, 2)
    return HuntPack(
        "rat-loop",
        3,
        route,
        targets,
        settings,
        cavebot,
        SupplyPolicy(),
        (
            "battle_list_click", "left_click", "modified_press", "press", "right_click",
            "sensor.follow", "sensor.hasted", "sensor.health", "sensor.hungry",
            "sensor.mana", "sensor.marker", "sensor.target",
        ),
    )


def test_round_trip_is_data_only_and_does_not_leak_runtime_or_paths(tmp_path):
    original = pack()
    destination = tmp_path / "rat.hunt.json"
    save_hunt_pack(original, destination)
    loaded = load_hunt_pack(destination)
    assert loaded == original

    encoded = destination.read_text(encoding="utf-8")
    lowered = encoded.casefold()
    for forbidden in ("hwnd", "checkpoint", "evidence", "calibration", "profile_id", "machine_path"):
        assert forbidden not in lowered
    assert "scripts/" not in encoded and "scripts\\" not in encoded


def test_export_requires_explicit_overwrite(tmp_path):
    destination = tmp_path / "rat.hunt.json"
    save_hunt_pack(pack(), destination)
    before = destination.read_bytes()
    with pytest.raises(HuntPackError, match="overwrite"):
        save_hunt_pack(pack(), destination)
    assert destination.read_bytes() == before
    save_hunt_pack(pack(), destination, overwrite=True)


def test_no_overwrite_export_cannot_lose_a_race(tmp_path, monkeypatch):
    destination = tmp_path / "raced.hunt.json"
    real_link = os.link

    def competing_writer(source, selected):
        destination.write_text("other process", encoding="utf-8")
        return real_link(source, selected)

    monkeypatch.setattr(os, "link", competing_writer)
    with pytest.raises(HuntPackError, match="overwrite"):
        save_hunt_pack(pack(), destination)
    assert destination.read_text(encoding="utf-8") == "other process"


def test_file_import_read_is_bounded(tmp_path):
    destination = tmp_path / "oversized.hunt.json"
    destination.write_bytes(b"{" + b" " * MAX_PACK_BYTES)
    with pytest.raises(HuntPackError, match="exceeds"):
        load_hunt_pack(destination)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes are unavailable")
def test_file_import_rejects_non_regular_files_without_blocking(tmp_path):
    destination = tmp_path / "pipe.hunt.json"
    os.mkfifo(destination)
    with pytest.raises(HuntPackError, match="regular file"):
        load_hunt_pack(destination)


def test_activation_requires_local_binding_and_all_declared_capabilities():
    selected = pack()
    capabilities = set(selected.required_capabilities)
    with pytest.raises(HuntPackError, match="local profile"):
        selected.validate_for_activation(None, capabilities)
    binding = LocalBinding("official-ot-profile", "calibration-digest")
    with pytest.raises(MissingCapabilities, match="right_click"):
        selected.validate_for_activation(binding, capabilities - {"right_click"})
    selected.validate_for_activation(binding, capabilities)


def test_schema_rejects_a_capability_mask_that_hides_setting_requirements():
    data = pack().as_dict()
    data["required_capabilities"].remove("sensor.hungry")
    with pytest.raises(HuntPackError, match="omits.*sensor.hungry"):
        hunt_pack_from_data(data)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda data: data.update(hwnd=1234), "unknown field"),
        (lambda data: data["route"]["waypoints"][0].update(checkpoint=9), "unknown field"),
        (lambda data: data["targets"][0].update(name="../Rat"), "asset name"),
        (lambda data: data["supply_policy"].update(state="refill"), "inactive"),
        (lambda data: data.update(required_capabilities="left_click"), "must be a list"),
        (lambda data: data["support"].update(timed_spells_immediate=1), "true or false"),
    ),
)
def test_strict_schema_rejects_runtime_fields_paths_and_wrong_types(mutate, message):
    data = pack().as_dict()
    mutate(data)
    with pytest.raises(HuntPackError, match=message):
        hunt_pack_from_data(data)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda route: route.update(future_policy="must-preserve"), "route.*unknown field"),
        (lambda route: route.pop("name"), "route.*missing field.*name"),
        (lambda route: route["waypoints"][0].pop("id"), "waypoint 1.*missing field.*id"),
        (lambda route: route["waypoints"][0].pop("action"), "waypoint 1.*missing field.*action"),
        (lambda route: route["waypoints"][0].update(id=""), "waypoint 1 id"),
        (lambda route: route["waypoints"][0].update(id=7), "waypoint 1 id"),
        (lambda route: route["waypoints"][0].update(action=1), "waypoint 1 action"),
    ),
)
def test_pack_route_never_silently_migrates_or_drops_versioned_fields(mutate, message):
    data = pack().as_dict()
    mutate(data["route"])
    with pytest.raises(HuntPackError, match=message):
        hunt_pack_from_data(data)


def test_standalone_legacy_route_migration_remains_available():
    from engine.CaveBot.routes import route_from_data

    route = route_from_data("legacy", [{"mark": "Flag", "type": 1}])
    assert route.waypoints[0].id == "waypoint-1"
    assert route.waypoints[0].action.value == "Walk"


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_nonfinite_numbers_are_rejected(constant):
    payload = pack().to_json().replace('"stand_seconds": 2', f'"stand_seconds": {constant}')
    with pytest.raises(HuntPackError, match="non-finite"):
        hunt_pack_from_json(payload)


@pytest.mark.parametrize("hotkey", ("Alt + F1", "Ctrl + Shift + F1", "Page Up", "A"))
def test_hotkeys_match_the_native_facade_grammar(hotkey):
    with pytest.raises(HuntPackError, match="supported hotkey"):
        ThresholdSetting(True, 50, hotkey)


def test_versions_and_stand_seconds_require_integers():
    data = pack().as_dict()
    data["version"] = 1.0
    with pytest.raises(HuntPackError, match="version"):
        hunt_pack_from_data(data)
    data = pack().as_dict()
    data["route"]["version"] = 1.0
    with pytest.raises(HuntPackError, match="route.*version"):
        hunt_pack_from_data(data)
    data = pack().as_dict()
    data["cavebot"]["stand_seconds"] = 0.5
    with pytest.raises(HuntPackError, match="whole number"):
        hunt_pack_from_data(data)


def test_duplicate_json_fields_are_rejected():
    with pytest.raises(HuntPackError, match="duplicate JSON field"):
        hunt_pack_from_json('{"schema":"tibiaauto.hunt-pack","schema":"again"}')


def test_only_bundled_marker_and_target_templates_are_accepted():
    data = pack().as_dict()
    data["route"]["waypoints"][0]["mark"] = "DoesNotExist"
    with pytest.raises(HuntPackError, match="missing waypoint image"):
        hunt_pack_from_data(data)
    data = pack().as_dict()
    data["targets"][0]["name"] = "DoesNotExist"
    with pytest.raises(HuntPackError, match="unknown bundled template"):
        hunt_pack_from_data(data)


def test_support_settings_apply_to_existing_loader_keys_in_one_document():
    document = {"Loaders": {
        "HealthLoader": {"unrelated": 1},
        "ManaLoader": {},
        "HurLoader": {},
        "FoodEaterLoader": {},
        "TimedSpellsLoader": {},
    }}
    returned = apply_support_to_loader_document(document, support())
    assert returned is document
    assert document["Loaders"]["HealthLoader"]["LifePercentageStageOne"] == 85
    assert document["Loaders"]["HealthLoader"]["unrelated"] == 1
    assert document["Loaders"]["ManaLoader"]["ManaHotkeyStageTwo"] == "F4"
    assert document["Loaders"]["HurLoader"]["HotkeyHur"] == "F6"
    assert document["Loaders"]["FoodEaterLoader"]["HotkeyFood"] == "Shift + F12"
    assert document["Loaders"]["TimedSpellsLoader"]["Spells"] == [["F7", 12.5]]


def test_invalid_loader_document_fails_before_partial_mutation():
    document = {"Loaders": {
        "HealthLoader": {}, "ManaLoader": {}, "HurLoader": {}, "FoodEaterLoader": {},
    }}
    before = deepcopy(document)
    with pytest.raises(HuntPackError, match="TimedSpellsLoader"):
        apply_support_to_loader_document(document, support())
    # Preparation must be atomic even before ConfManager performs its atomic file replace.
    assert document == before


def test_bind_refreshes_open_support_windows_and_close_keeps_imported_settings(tmp_path, monkeypatch):
    from conf.conf_manager import ConfManager
    from modules.AutoHeal import AutoHeal
    from modules.AutoMana import AutoMana
    from modules.AutoHur import AutoHur
    from modules.FoodEater import FoodEater
    from modules.TimedSpells import TimedSpells
    from modules.CaveBot import CaveBot
    from modules import support_ui
    from tibia_client.hunt_packs import support_from_loaders

    class Variable:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class Table:
        def __init__(self):
            self.rows = {"old": ("F1", 999)}

        def get_children(self):
            return tuple(self.rows)

        def delete(self, key):
            del self.rows[key]

        def insert(self, _parent, _where, *, values):
            self.rows[str(len(self.rows))] = values

        def item(self, key, _column):
            return self.rows[key]

    control = SimpleNamespace(configure=lambda **kwargs: None)
    starts = []
    client = SimpleNamespace(
        feature_status=lambda name: SimpleNamespace(active=False),
        stop_feature=lambda name: None,
        start_feature=lambda feature: starts.append(feature),
    )
    monkeypatch.setattr(ConfManager, "path", lambda name: tmp_path / name)
    selected = pack()
    names = tuple(selected.support.loader_updates())
    ConfManager.set({"Loaders": {name: {} for name in (*names, "CaveBotLoader")}}, "GUILoader.json")
    windows = []
    for cls, loader in zip((AutoHeal, AutoMana, AutoHur, FoodEater, TimedSpells), names):
        module = object.__new__(cls)
        module._client = client
        module.ui = object.__new__(support_ui.SupportWindow)
        module.ui.owner = True
        module.ui.window = SimpleNamespace(winfo_exists=lambda: True, destroy=lambda: None)
        module.ui.status = lambda text: None
        module.ui._on_close = module.close
        module.button = module.key_widget = control
        module.key = Variable("F1")
        module.interval = Variable("999")
        module.instant = Variable(False)
        module._controls = ()
        module.table = Table()
        module._vars = [
            (label, Variable(True), Variable("10"), Variable("F1"), ())
            for label in (("One", "Two", "Three") if cls is AutoHeal else ("One", "Two"))
        ]
        module.ui.bind_loader(loader, module._apply_settings)
        windows.append(module)
    cave = object.__new__(CaveBot)
    cave._client = client
    try:
        cave._apply_pack_configuration(selected)
        assert windows[2].key.get() == selected.support.haste_hotkey
        assert windows[3].key.get() == selected.support.food_hotkey
        assert windows[0]._vars[0][2].get() == "85"
        assert windows[1]._vars[0][2].get() == "70"
        assert windows[4]._rows() == (("F7", 12.5),)
        assert windows[4].instant.get() is True
        assert support_from_loaders(support_ui.read_loader) == selected.support
        for module in windows:
            module.ui.close()
        assert support_from_loaders(support_ui.read_loader) == selected.support
        assert not starts
        assert all(name not in support_ui._loader_windows for name in names)
    finally:
        for name in names:
            support_ui._loader_windows.pop(name, None)


def test_pack_export_preserves_independent_imported_name():
    from modules.CaveBot import CaveBot

    imported = replace(pack(), name="display-name")
    ui = object.__new__(CaveBot)
    ui._imported_pack, ui._unapplied_import = imported, True
    ui._local_binding = None
    ui._route = imported.route
    ui._route_name = lambda: imported.route.name
    def variable(value):
        return SimpleNamespace(get=lambda: value)
    ui.walk_enabled = variable(imported.cavebot.walking)
    ui.follow = variable(imported.cavebot.follow)
    ui.loot_enabled = variable(imported.cavebot.loot)
    ui.stand = variable(imported.cavebot.stand_seconds)
    ui.attack_enabled = [(variable(True),) for target in imported.targets]
    ui.monster_names = [(variable(target.name),) for target in imported.targets]
    ui.priorities = [(variable(target.priority),) for target in imported.targets]
    exported = ui._current_pack()
    assert exported.name == imported.name
    assert exported.route == imported.route
