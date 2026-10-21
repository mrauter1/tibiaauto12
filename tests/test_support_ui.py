import importlib.util
import ast
from pathlib import Path
import sys
import pytest
from tibia_client.models import FeatureStatus

SPEC = importlib.util.spec_from_file_location(
    "support_ui_under_test", Path(__file__).parents[1] / "modules" / "support_ui.py"
)
support_ui = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = support_ui
SPEC.loader.exec_module(support_ui)
client_point, integer, write_loader = (
    support_ui.client_point,
    support_ui.integer,
    support_ui.write_loader,
)


def test_support_hotkey_choices_are_action_keys_not_the_cavebot_pause_shortcut():
    from conf.Constants import Hotkeys as pause_hotkeys
    from conf.Hotkeys import Hotkey

    choices = support_ui.hotkeys()
    assert choices == tuple(Hotkey.Hotkeys)
    assert len(choices) == len(set(choices)) == 36
    assert all(
        f"{modifier}F{number}" in choices
        for modifier in ("", "Ctrl + ", "Shift + ")
        for number in range(1, 13)
    )
    assert not set(choices).intersection(pause_hotkeys)


@pytest.mark.parametrize("value", ("", "x", 101, -1))
def test_percentage_validation(value):
    with pytest.raises(ValueError):
        integer(value, "life")


def test_client_point_accepts_zero_and_labels_values_as_client_local():
    assert client_point("0", "42") == (0, 42)
    with pytest.raises(ValueError, match="client X"):
        client_point("left", "42")


def test_write_loader_uses_atomic_update_when_available(monkeypatch):
    calls = []

    class Config:
        @staticmethod
        def update(filename, updater):
            data = {"Loaders": {"X": {"old": 1}}}
            returned = updater(data)
            calls.append((filename, returned))

    monkeypatch.setattr(support_ui, "ConfManager", Config)
    write_loader("X", {"new": 2})
    assert calls == [("GUILoader.json", {"Loaders": {"X": {"old": 1, "new": 2}}})]


def test_feature_error_updates_status_and_resets_owner_once():
    calls = []

    class Window:
        def after(self, *args):
            calls.append(("after", args))

    class Client:
        def feature_status(self, name):
            assert name == "AutoHeal"
            return FeatureStatus(name, 3, False, "Stopped", "capture unavailable")

    ui = object.__new__(support_ui.SupportWindow)
    ui.owner, ui.window = True, Window()
    ui.status = lambda value: calls.append(("status", value))
    ui.report_feature(
        Client(), "AutoHeal", on_inactive=lambda error: calls.append(("reset", str(error)))
    )
    assert ("status", "Error: capture unavailable") in calls
    assert ("reset", "capture unavailable") in calls


def test_feature_phase_is_rendered_on_tk_poll():
    calls = []

    class Window:
        def after(self, *args):
            calls.append(("after", args))

    class Client:
        def feature_status(self, name):
            return FeatureStatus(name, 2, True, "Waiting for game focus")

    ui = object.__new__(support_ui.SupportWindow)
    ui.owner, ui.window = True, Window()
    ui.status = lambda value: calls.append(("status", value))
    ui.report_feature(Client(), "AutoHeal")
    assert ("status", "Waiting for game focus") in calls


def test_feature_external_stop_reenables_controls_once():
    calls = []

    class Window:
        def after(self, *args):
            calls.append(("after", args))

    class Client:
        def feature_status(self, name):
            return FeatureStatus(name, 4, False, "Stopped")

    ui = object.__new__(support_ui.SupportWindow)
    ui.owner, ui.window, ui._expected_active = True, Window(), True
    ui.status = lambda value: calls.append(("status", value))
    ui.report_feature(
        Client(), "AutoHeal", on_inactive=lambda error: calls.append(("reset", error))
    )
    ui.report_feature(
        Client(), "AutoHeal", on_inactive=lambda error: calls.append(("reset", error))
    )
    assert calls.count(("reset", None)) == 1


def test_close_destroys_window_even_when_cleanup_fails():
    class Window:
        destroyed = False

        def destroy(self):
            self.destroyed = True

    ui = object.__new__(support_ui.SupportWindow)
    ui.owner, ui.window = True, Window()
    ui._on_close = lambda: (_ for _ in ()).throw(OSError("disk failed"))
    with pytest.raises(OSError, match="disk failed"):
        ui.close()
    assert ui.window.destroyed


def test_active_ui_modules_do_not_import_native_execution_layers():
    modules = (
        "AutoHeal.py",
        "AutoMana.py",
        "AutoHur.py",
        "AutoRing.py",
        "AutoSSA.py",
        "FoodEater.py",
        "TimedSpells.py",
        "CaveBot.py",
        "support_ui.py",
        "Root.py",
        "ResourceMonitor.py",
    )
    forbidden = {
        "core.ThreadManager",
        "core.runtime",
        "core.HookWindow",
        "core.perception",
        "conf.Hotkeys",
        "engine.ScanStages",
        "engine.CaveBot.Scanners",
        "keyboard",
        "pyautogui",
        "win32api",
        "win32gui",
    }
    root = Path(__file__).parents[1] / "modules"
    for filename in modules:
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            f"{node.module}.{alias.name}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        )
        assert not any(
            imported == prefix or imported.startswith(prefix + ".")
            for imported in imports
            for prefix in forbidden
        ), filename
