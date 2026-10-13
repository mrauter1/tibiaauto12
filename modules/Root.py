"""Main module launcher and current profile state."""

from __future__ import annotations

from conf.conf_manager import ConfManager
from core.Getters import validate_calibration
from core.GUI import GUI, shutdown
from tibia_client.application import (
    close_client, configure_client, get_client, poll_hotkey, set_compatibility_provider,
)

MOUSE_OPTION = None
ItemsSquare = 32
mark = [0, 0]
Player = [0, 0]
Target = [0, 0]
gameWindow = [0, 0, 0, 0]
ManaLocation = [0, 0]
MapPositions = [0, 0, 0, 0]
RingPositions = [0, 0, 0, 0]
StatsPositions = [0, 0, 0, 0]
HealthLocation = [0, 0]
BattlePositions = [0, 0, 0, 0]
AmuletPositions = [0, 0, 0, 0]
SQMs = [0] * 18
MainPath = ItemsPath = ChestsPath = ContainersNamePath = CavebotScriptsPath = None
_active_root = None
_profile_controller = None
_profile_filename = None


def _values(data, group, name, keys):
    entry = data[group][name][0]
    return [entry[key] for key in keys]


def load_profile(filename: str) -> dict:
    """Reload all globals from disk on every root construction/reconfigure."""
    global MOUSE_OPTION, MainPath, ItemsPath, ChestsPath, ContainersNamePath, CavebotScriptsPath
    data = ConfManager.get(filename)
    config = ConfManager.get("conf.json")
    size = config.get("capture_size")
    validate_calibration(data, tuple(size) if isinstance(size, list) and len(size) == 2 else None)
    if data.get("calibration_size") is None:
        raise ValueError(
            "legacy profile has no bound capture size; select Reconfigure and recalibrate it"
        )

    MOUSE_OPTION = data.get("MouseOption", 1)
    if type(MOUSE_OPTION) is not int or MOUSE_OPTION not in (0, 1):
        raise ValueError("profile MouseOption must be physical (1) or window messages (0)")
    HealthLocation[:] = _values(data, "Positions", "LifePosition", ("x", "y"))
    ManaLocation[:] = _values(data, "Positions", "ManaPosition", ("x", "y"))
    Player[:] = _values(data, "Positions", "PlayerPosition", ("x", "y"))
    BattlePositions[:] = _values(data, "Boxes", "BattleBox", ("x", "y", "w", "h"))
    StatsPositions[:] = _values(data, "Boxes", "StatusBarBox", ("x", "y", "w", "h"))
    RingPositions[:] = _values(data, "Boxes", "RingBox", ("x", "y", "w", "h"))
    AmuletPositions[:] = _values(data, "Boxes", "AmuletBox", ("x", "y", "w", "h"))
    MapPositions[:] = _values(data, "Boxes", "MapBox", ("x", "y", "w", "h"))
    gameWindow[:] = _values(data, "Boxes", "GameWindowBox", ("x", "y", "w", "h"))
    SQMs[:] = [
        coordinate
        for number in range(1, 10)
        for coordinate in _values(data, "SQM", f"SQM{number}", ("x", "y"))
    ]
    paths = data.get("Paths", {})
    MainPath = paths.get("MainPath")
    ItemsPath = paths.get("ItemsPath")
    ChestsPath = paths.get("ChestsPath")
    ContainersNamePath = paths.get("ContainersNamePath")
    CavebotScriptsPath = paths.get("CavebotScriptsPath")
    return data


def _open(module_name, class_name, *args):
    import importlib

    constructor = getattr(importlib.import_module(module_name), class_name)
    return constructor(*args)


def OpenAutoHeal():
    return _open("modules.AutoHeal", "AutoHeal", HealthLocation, MOUSE_OPTION)


def OpenAutoHur():
    return _open("modules.AutoHur", "AutoHur", StatsPositions, MOUSE_OPTION)


def OpenAutoMana():
    return _open("modules.AutoMana", "AutoMana", ManaLocation, MOUSE_OPTION)


def OpenAutoRing():
    return _open(
        "modules.AutoRing",
        "AutoRing",
        _active_root,
        RingPositions,
        HealthLocation,
        MOUSE_OPTION,
        ItemsPath,
    )


def OpenAutoSSA():
    return _open(
        "modules.AutoSSA",
        "AutoSSA",
        _active_root,
        AmuletPositions,
        HealthLocation,
        MOUSE_OPTION,
        ItemsPath,
    )


def OpenCaveBot():
    return _open("modules.CaveBot", "CaveBot", MapPositions, BattlePositions, SQMs, MOUSE_OPTION)


def OpenFoodEater():
    return _open("modules.FoodEater", "FoodEater", _active_root, StatsPositions, MOUSE_OPTION)


def OpenTimedSpells():
    return _open("modules.TimedSpells", "TimedSpells", _active_root, MOUSE_OPTION)


def OpenCompatibility():
    from modules.Compatibility import Compatibility
    return Compatibility(_active_root.windowID, get_client(), _profile_filename,
                         controller=_profile_controller)


def OpenTraceViewer():
    from modules.TraceViewer import TraceViewer
    return TraceViewer(_active_root.windowID, session=get_client())


def OpenHuntConsole():
    from modules.HuntConsole import HuntConsole
    return HuntConsole(_active_root.windowID, get_client(), _profile_controller)


def OpenHuntWorkflow():
    from modules.HuntWorkflow import HuntWorkflow
    from tibia_client.hunt_workflow import HuntWorkflowController

    controller = getattr(_active_root, "hunt_workflow_controller", None)
    if controller is None:
        controller = HuntWorkflowController(get_client(), _profile_filename, _profile_controller)
        _active_root.hunt_workflow_controller = controller
    return HuntWorkflow(_active_root.windowID, controller)


class root:
    def __init__(self, CharName, LoadedJson):
        global _active_root, _profile_controller, _profile_filename
        load_profile(LoadedJson)
        from tibia_client.desktop import DesktopAdapter
        from tibia_client.desktop_profile import DesktopProfileController

        adapter = DesktopAdapter(MOUSE_OPTION)
        controller = DesktopProfileController(LoadedJson,
            input_size=adapter.hotkey.MainWindowSize, capabilities=adapter.capabilities())
        configure_client(MOUSE_OPTION, adapter=adapter, activation_guard=controller)
        set_compatibility_provider(controller.status)
        _profile_controller, _profile_filename = controller, LoadedJson
        self.root = GUI("root", "TibiaAuto V12")
        self.root.MainWindow("Main", [357, 530], [2, 2.36])
        self.root.deiconify()
        _active_root = self.root
        self.root.addMinimalLabel(f"Logged as: {CharName}", [14, 14])

        unavailable = (
            ("Healer Friend", [23, 56]),
            ("Color Change", [23, 108]),
            ("Ammo Restack", [23, 135]),
            ("Auto Looter", [23, 160]),
            ("Auto Grouping", [23, 236]),
            ("Sort Loot", [23, 262]),
            ("Auto Banker", [23, 288]),
            ("Auto Seller", [23, 340]),
            ("FPS Changer", [23, 366]),
            ("Auto Fish", [245, 83]),
            ("Auto Login", [245, 135]),
            ("Creature Info", [147, 188]),
            ("Monsters", [245, 188]),
            ("Show Map", [147, 290]),
            ("Modules", [245, 366]),
            ("Python Scripts", [245, 392]),
        )
        for text, position in unavailable:
            self.root.addButton(f"{text} (N/A)", lambda: None, [92, 23], position).configure(
                state="disabled"
            )

        self.root.addButton("Food Eater", OpenFoodEater, [92, 23], [23, 210])
        self.root.addButton("Auto Life", OpenAutoHeal, [92, 23], [147, 56])
        self.root.addButton("Auto Hur", OpenAutoHur, [92, 23], [245, 56])
        self.root.addButton("Auto Mana", OpenAutoMana, [92, 23], [147, 83])
        self.root.addButton("Auto Amulet", OpenAutoSSA, [92, 23], [147, 108])
        self.root.addButton("Auto Ring", OpenAutoRing, [92, 23], [245, 108])
        self.root.addButton("Timed Spells", OpenTimedSpells, [92, 23], [147, 135])
        self.root.addButton("Cave Bot", OpenCaveBot, [92, 23], [245, 290])
        self.root.addButton("Test client", OpenCompatibility, [92, 23], [147, 340])
        self.root.addButton("Trace viewer", OpenTraceViewer, [92, 23], [245, 340])
        self.root.addButton("Hunt Console", OpenHuntConsole, [213, 23], [134, 426])
        self.root.addButton("Hunt workflow", OpenHuntWorkflow, [92, 23], [147, 366])

        import tkinter as tk
        from modules.HuntConsole import run_control
        self._stopping = False
        self._closing = False
        self._stop_latched = False
        self.control_status = tk.StringVar(value="Stop shortcut: Ctrl+Shift+F12")
        tk.Label(self.root.windowID, textvariable=self.control_status, bg="black", fg="white",
                 wraplength=330).place(x=12, y=459, width=335, height=34)

        def stop_all():
            if self._stopping or self._closing:
                return
            self._stopping = True
            self.control_status.set("Stopping all automation…")
            def done(_value, error):
                self._stopping = False
                self.control_status.set(f"Stop failed: {error}" if error else "Stopped. Restart modules explicitly.")
            run_control(self.root.windowID, lambda: get_client().stop_all("operator Stop All"), done)

        self.root.addButton("STOP ALL", stop_all, [213, 23], [134, 498])

        def poll_stop():
            if self._closing:
                return
            try:
                pressed = poll_hotkey("ctrl+shift+f12")
                if pressed and not self._stop_latched:
                    stop_all()
                self._stop_latched = pressed
            except Exception:
                # A missing global keyboard hook must not disable the visible Stop button.
                self.control_status.set("Global stop shortcut unavailable; use STOP ALL.")
                return
            self.root.windowID.after(100, poll_stop)

        self.root.windowID.after(100, poll_stop)

        def close():
            self._closing = True
            try:
                close_client()
            finally:
                shutdown()

        self.root.addButton("Exit", close, [92, 23], [10, 498])
        self.root.Protocol(close)
        self.root.loop()
