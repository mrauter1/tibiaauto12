from __future__ import annotations

from copy import deepcopy
import json
import subprocess
import sys

import pytest

import conf.conf_manager as manager_module
from conf.conf_manager import ConfManager
from core.Getters import CalibrationError, validate_calibration
from modules.ChooseConfig import (
    calibrate_profile, is_profile_document, prepare_profile, profile_filename,
)


def valid_profile():
    profile = ConfManager.get("Json.json")
    profile["Stats"] = True
    points = {
        "LifePosition": (20, 20), "ManaPosition": (20, 30),
        "BattlePosition": (300, 50), "PlayerPosition": (400, 300),
    }
    for name, (x, y) in points.items():
        profile["Positions"][name][0].update(x=x, y=y, Stats=True)
    boxes = {
        "StatusBarBox": (500, 200, 605, 210), "BattleBox": (300, 50, 450, 400),
        "RingBox": (500, 142, 531, 173), "AmuletBox": (500, 70, 531, 101),
        "GameWindowBox": (100, 50, 700, 550), "MapBox": (650, 50, 755, 158),
    }
    for name, values in boxes.items():
        profile["Boxes"][name][0].update(
            x=values[0], y=values[1], w=values[2], h=values[3], Stats=True
        )
    coords = [(360, 350), (400, 350), (440, 350), (360, 300), (400, 300),
              (440, 300), (360, 250), (400, 250), (440, 250)]
    for number, (x, y) in enumerate(coords, 1):
        profile["SQM"][f"SQM{number}"][0].update(x=x, y=y, Stats=True)
    return profile


def test_calibration_validator_accepts_complete_profile():
    assert validate_calibration(valid_profile(), (800, 600)) is True


def test_offline_validation_uses_saved_calibration_extent():
    profile = valid_profile()
    profile["calibration_size"] = [700, 500]
    with pytest.raises(CalibrationError, match="outside|exceeds"):
        validate_calibration(profile)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda p: p["Positions"]["LifePosition"][0].update(x=0), "LifePosition has missing"),
        (lambda p: p["Boxes"]["MapBox"][0].update(w=900), "MapBox exceeds"),
        (lambda p: p["SQM"]["SQM4"][0].update(Stats=False), "SQM4 was not calculated"),
        (lambda p: p["Positions"]["ManaPosition"][0].update(x=True), "ManaPosition has missing"),
        (lambda p: p.update(calibration_size=[1024, 768]), "profile was calibrated for"),
    ],
)
def test_calibration_validator_rejects_incomplete_or_invalid_data(mutate, expected):
    profile = valid_profile()
    mutate(profile)
    with pytest.raises(CalibrationError, match=expected):
        validate_calibration(profile, (800, 600))


def test_profile_filename_rejects_path_escape():
    with pytest.raises(Exception, match="plain filename"):
        profile_filename("../escape")


@pytest.mark.parametrize("name", ["conf", "GUILoader.json", "Json", "walk.state"])
def test_profile_filename_reserves_application_and_route_state_names(name):
    with pytest.raises(Exception, match="reserved"):
        profile_filename(name)


def test_profile_shape_does_not_accept_route_data():
    assert is_profile_document(valid_profile())
    assert not is_profile_document({"name": "route", "waypoints": []})


def test_calibrate_profile_is_in_memory_and_validates_before_return():
    class FakeGetters:
        @staticmethod
        def GetHealthPosition(): return (20, 20)
        @staticmethod
        def GetManaPosition(): return (20, 30)
        @staticmethod
        def GetBattlePosition(): return (300, 50, 450, 400)
        @staticmethod
        def GetStatsPosition(): return (500, 200, 605, 210)
        @staticmethod
        def GetMapPosition(): return (650, 50, 755, 158)
        @staticmethod
        def GetPlayerPosition(): return (400, 300, 100, 50, 700, 550)
        @staticmethod
        def SetSQMs(player, game):
            return (360, 350, 400, 350, 440, 350, 360, 300, 400, 300,
                    440, 300, 360, 250, 400, 250, 440, 250)

    template = ConfManager.get("Json.json")
    original = deepcopy(template)
    built = calibrate_profile(template, (800, 600), getters=FakeGetters)
    assert template == original
    assert built["Stats"] is True
    assert validate_calibration(built, (800, 600))
    assert built["calibration_size"] == [800, 600]


def test_prepare_profile_rejects_route_collision(tmp_path, monkeypatch):
    def resolve(filename): return tmp_path / str(filename)
    monkeypatch.setattr(manager_module, "scripts_path", resolve)
    ConfManager.set({"name": "route", "waypoints": []}, "Hunt.json")
    with pytest.raises(Exception, match="route or application data"):
        prepare_profile("Hunt.json", (800, 600), 1, "Frames")


def test_failed_recalibration_preserves_existing_profile(tmp_path, monkeypatch):
    existing = valid_profile()
    existing["MouseOption"] = 1
    before = deepcopy(existing)

    def resolve(filename): return tmp_path / str(filename)
    monkeypatch.setattr(manager_module, "scripts_path", resolve)
    ConfManager.set(existing, "Saved.json")

    class FailedCapture:
        @staticmethod
        def GetHealthPosition():
            raise RuntimeError("capture timed out; check the selected capture window")

    with pytest.raises(RuntimeError, match="capture timed out"):
        prepared = prepare_profile(
            "Saved.json", (800, 600), 1, "Frames", recalibrate=True,
            getters=FailedCapture,
        )
        ConfManager.set(prepared, "Saved.json")
    assert ConfManager.get("Saved.json") == before


def test_prepare_profile_rejects_invalid_mouse_mode_before_activation(tmp_path, monkeypatch):
    profile = valid_profile()
    profile["calibration_size"] = [800, 600]
    def resolve(filename): return tmp_path / str(filename)
    monkeypatch.setattr(manager_module, "scripts_path", resolve)
    ConfManager.set(profile, "Saved.json")
    with pytest.raises(CalibrationError, match="input mode"):
        prepare_profile("Saved.json", (800, 600), 4, "Frames")


def test_legacy_profile_requires_explicit_recalibration(tmp_path, monkeypatch):
    profile = valid_profile()
    assert "calibration_size" not in profile
    def resolve(filename): return tmp_path / str(filename)
    monkeypatch.setattr(manager_module, "scripts_path", resolve)
    ConfManager.set(profile, "Legacy.json")
    with pytest.raises(CalibrationError, match="no bound capture size"):
        prepare_profile("Legacy.json", (800, 600), 1, "Frames")


def test_start_module_cold_import_does_not_import_tk_or_modules_package():
    code = "import sys; before=set(sys.modules); import start; new=set(sys.modules)-before; print('tkinter' in new, 'modules' in new)"
    completed = subprocess.run(
        [sys.executable, "-c", code], text=True, capture_output=True, check=True
    )
    assert completed.stdout.strip() == "False False"


def test_modules_package_import_does_not_enumerate_windows():
    completed = subprocess.run(
        [sys.executable, "-c", "import modules; print('ok')"],
        text=True, capture_output=True, check=True,
    )
    assert completed.stdout.strip() == "ok"


def test_unsupported_platform_stops_before_chooser(monkeypatch):
    import start
    called = False
    def chooser():
        nonlocal called
        called = True
    monkeypatch.setattr(start.os, "name", "posix")
    with pytest.raises(start.UnsupportedPlatformError, match="requires Windows"):
        start.main([], chooser=chooser)
    assert called is False


def test_prepare_config_migrates_legacy_handle_without_overwriting_preferences(tmp_path, monkeypatch):
    def resolve(filename): return tmp_path / str(filename)
    monkeypatch.setattr(manager_module, "scripts_path", resolve)
    (tmp_path / "conf.json").write_text(json.dumps({
        "configured": True, "hwnd": 42, "preferences_name": "Saved.json", "custom": "keep"
    }), encoding="utf-8")
    import start
    result = start.prepare_config()
    assert result["capture_hwnd"] == 42
    assert result["input_hwnd"] == 42
    assert result["preferences_name"] == "Saved.json"
    assert result["custom"] == "keep"

@pytest.mark.skipif(sys.platform != "win32", reason="Windows GUI smoke gate")
def test_windows_real_gui_constructors_smoke(tmp_path, monkeypatch):
    """Build every enabled window without starting capture, workers, or input."""
    profile = valid_profile()
    profile["calibration_size"] = [800, 600]
    profile["MouseOption"] = 1
    profile["ItemsMode"] = "Frames"
    profile["Paths"].update(
        MainPath="images", ItemsPath="images/Items/Frames/",
        ChestsPath="images/Depot/Chests/", ContainersNamePath="images/Items/ContainersName/",
        CavebotScriptsPath="scripts/",
    )
    gui_loader = ConfManager.get("GUILoader.json")

    def resolve(filename): return tmp_path / str(filename)
    monkeypatch.setattr(manager_module, "scripts_path", resolve)
    ConfManager.set(profile, "Smoke.json")
    ConfManager.set(gui_loader, "GUILoader.json")
    ConfManager.set({
        "configured": True, "platform": "windows", "hwnd": 1,
        "capture_hwnd": 1, "input_hwnd": 1, "capture_size": [800, 600],
        "preferences_name": "Smoke.json",
    }, "conf.json")

    class DummyBackend:
        def MainWindowSize(self): return (800, 600)
        def __getattr__(self, _name): return lambda *_args, **_kwargs: None

    from conf.Hotkeys import Hotkey
    from core import GUI as gui_module
    from core import runtime
    monkeypatch.setattr(Hotkey, "_make_backend", lambda self: DummyBackend())
    monkeypatch.setattr(gui_module.GUI, "loop", lambda self: None)

    import modules.Root as root_module
    try:
        root_module.root("Smoke Client", "Smoke.json")
        root_module.OpenAutoHeal()
        root_module.OpenAutoMana()
        root_module.OpenAutoHur()
        root_module.OpenAutoRing()
        root_module.OpenAutoSSA()
        root_module.OpenFoodEater()
        root_module.OpenTimedSpells()
        root_module.OpenCaveBot()
        gui_module.application_root().update_idletasks()
    finally:
        runtime.stop_all(0.5)
        gui_module.shutdown()
