"""Main module launcher and current profile state."""

from __future__ import annotations

from conf.conf_manager import ConfManager
from core.Getters import validate_calibration
from core.GUI import GUI, shutdown
from tibia_client.application import close_client, configure_client

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


class root:
    def __init__(self, CharName, LoadedJson):
        global _active_root
        load_profile(LoadedJson)
        configure_client(MOUSE_OPTION)
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
            ("Load Config", [147, 340]),
            ("Save Config", [245, 340]),
            ("Adjust Config", [147, 366]),
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
        self.root.addButton("General Options (N/A)", lambda: None, [213, 23], [134, 426]).configure(
            state="disabled"
        )

        def close():
            try:
                close_client()
            finally:
                shutdown()

        self.root.addButton("Exit", close, [92, 23], [10, 498])
        self.root.Protocol(close)
        self.root.loop()
