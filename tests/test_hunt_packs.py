from copy import deepcopy
import json
import os

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
