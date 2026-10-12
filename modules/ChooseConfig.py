"""Profile selection and one-shot client calibration."""
from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Mapping
import tkinter as tk
from tkinter import messagebox

from conf.conf_manager import ConfManager, ConfigError
from core.GUI import GUI, rgb
from core.Getters import CalibrationError, validate_calibration


ITEM_SQUARE = 32
RESERVED_PROFILE_NAMES = {
    "conf.json", "guiloader.json", "json.json", "defaultwalk.json", "void.json",
}


def profile_filename(raw_name: str) -> str:
    name = raw_name.strip()
    if name.lower().endswith(".json"):
        name = name[:-5]
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ConfigError("profile name must be a plain filename without folders")
    filename = f"{name}.json"
    ConfManager.path(filename)  # strict extension/traversal validation
    folded = filename.casefold()
    if folded in RESERVED_PROFILE_NAMES or folded.endswith(".state.json"):
        raise ConfigError(f"{filename!r} is reserved for application or route data")
    return filename


def is_profile_document(data: Any) -> bool:
    return (isinstance(data, Mapping)
            and all(isinstance(data.get(key), Mapping) for key in ("Positions", "Boxes", "SQM")))


def _set_point(profile, name, point):
    x, y = map(int, point)
    profile["Positions"][name][0].update(x=x, y=y, Stats=True)


def _set_box(profile, name, box):
    x, y, right, bottom = map(int, box)
    profile["Boxes"][name][0].update(x=x, y=y, w=right, h=bottom, Stats=True)


def calibrate_profile(
    template: Mapping[str, Any], frame_size: tuple[int, int], *, getters=None
) -> dict[str, Any]:
    """Build a complete profile in memory and validate it before any write."""
    profile = deepcopy(template)
    capture = None
    if getters is None:
        from core import Getters as getters
        from core.HookWindow import frame_scope
        from tibia_client.capture import CaptureProcess

        capture = CaptureProcess(capture_size=frame_size)
        try:
            frame = capture.capture()
        except Exception:
            capture.close()
            raise
        scope = frame_scope(frame)
    else:
        scope = nullcontext()

    try:
        with scope:
            health = getters.GetHealthPosition()
            mana = getters.GetManaPosition()
            battle = getters.GetBattlePosition()
            stats = getters.GetStatsPosition()
            map_box = getters.GetMapPosition()
            player_result = getters.GetPlayerPosition()
            player = player_result[:2]
            game_box = player_result[2:]
            sqms = getters.SetSQMs(player, game_box)
    finally:
        if capture is not None:
            capture.close()

    _set_point(profile, "LifePosition", health)
    _set_point(profile, "ManaPosition", mana)
    _set_point(profile, "BattlePosition", battle[:2])
    _set_point(profile, "PlayerPosition", player)
    _set_box(profile, "BattleBox", battle)
    _set_box(profile, "StatusBarBox", stats)
    _set_box(profile, "MapBox", map_box)
    _set_box(profile, "GameWindowBox", game_box)

    ring = (stats[0], stats[1] - 58, stats[0] + ITEM_SQUARE - 1, stats[1] - 27)
    amulet = (stats[0], stats[1] - 130, stats[0] + ITEM_SQUARE - 1, stats[1] - 99)
    _set_box(profile, "RingBox", ring)
    _set_box(profile, "AmuletBox", amulet)

    for number in range(1, 10):
        item = profile["SQM"][f"SQM{number}"][0]
        item.update(x=int(sqms[(number - 1) * 2]), y=int(sqms[(number - 1) * 2 + 1]), Stats=True)
    profile["Stats"] = True
    profile["calibration_size"] = list(frame_size)
    validate_calibration(profile, frame_size)
    return profile


def _add_runtime_options(profile: dict[str, Any], mouse_mode: int, items_mode: str) -> None:
    if type(mouse_mode) is not int or mouse_mode not in (0, 1):
        raise CalibrationError("input mode must be physical (1) or window messages (0)")
    if items_mode not in {"Frames", "Corners", "None"}:
        raise CalibrationError(f"unsupported item image mode: {items_mode!r}")
    from conf.Constants import (
        CavebotScriptsPath, ChestsPath, ContainersNamePath, CornersItemsPath,
        FramesItemsPath, MainPath, NoneItemsPath,
    )
    profile["MouseOption"] = mouse_mode
    profile["ItemsMode"] = items_mode
    item_paths = {"Frames": FramesItemsPath, "Corners": CornersItemsPath, "None": NoneItemsPath}
    profile["Paths"].update(
        MainPath=MainPath,
        ItemsPath=item_paths[items_mode],
        ChestsPath=ChestsPath,
        ContainersNamePath=ContainersNamePath,
        CavebotScriptsPath=CavebotScriptsPath,
    )


def prepare_profile(filename: str, frame_size: tuple[int, int], mouse_mode: int,
                    items_mode: str, *, recalibrate: bool = False, getters=None) -> dict[str, Any]:
    """Prepare a profile fully in memory; callers write only after success."""
    filename = profile_filename(filename)
    path = ConfManager.path(filename)
    if path.exists():
        existing = ConfManager.get(filename)
        if not is_profile_document(existing):
            raise ConfigError(
                f"{filename!r} already contains route or application data; choose another profile name"
            )
        if recalibrate:
            profile = calibrate_profile(existing, frame_size, getters=getters)
        else:
            if existing.get("calibration_size") is None:
                raise CalibrationError(
                    "legacy profile has no bound capture size; enable "
                    "'Recalibrate existing profile' once before activation"
                )
            try:
                validate_calibration(existing, frame_size)
            except CalibrationError as exc:
                raise CalibrationError(
                    [*exc.problems, "enable 'Recalibrate existing profile' to repair it"]
                ) from exc
            profile = deepcopy(existing)
    else:
        template = ConfManager.get("Json.json")
        profile = calibrate_profile(template, frame_size, getters=getters)
    _add_runtime_options(profile, mouse_mode, items_mode)
    validate_calibration(profile, frame_size)
    return profile


class ChooseConfig:
    def __init__(self, selected_window_name):
        self.selected_window_name = selected_window_name
        self.window = GUI("ChooseConfig", "Choose your config")
        self.window.MainWindow("Config", [414, 202], [2, 2.36])

        name = tk.StringVar(master=self.window.windowID, value="NewConfig")
        mouse_mode = tk.IntVar(master=self.window.windowID, value=1)
        items_mode = tk.StringVar(master=self.window.windowID, value="Frames")
        recalibrate = tk.BooleanVar(master=self.window.windowID, value=False)

        def open_root(filename):
            self.window.withdraw()
            from modules.Root import root
            try:
                root(self.selected_window_name, filename)
            except Exception:
                self.window.deiconify()
                raise
            self.window.windowID.destroy()

        def bootstrap():
            try:
                filename = profile_filename(name.get())
                config = ConfManager.get("conf.json")
                frame_size = tuple(config.get("capture_size", ()))
                if len(frame_size) != 2:
                    raise CalibrationError("capture size is unavailable; return to window selection")

                profile = prepare_profile(
                    filename, frame_size, mouse_mode.get(), items_mode.get(),
                    recalibrate=recalibrate.get(),
                )
                # Failed detection never reaches this atomic replacement, preserving any old profile.
                ConfManager.set(profile, filename)

                def activate(current):
                    current["configured"] = True
                    current["preferences_name"] = filename

                ConfManager.update("conf.json", activate)
                open_root(filename)
            except (RuntimeError, KeyError, TypeError, ValueError) as exc:
                messagebox.showerror("Calibration could not be completed", str(exc), parent=self.window.windowID)

        self.window.addEntry([165, 35], name, 28)
        self.window.addLabel("Name Of The Json Conf", [24, 35])
        label = self.window.addLabel("Input mode", [30, 76])
        label.configure(bg=rgb((114, 94, 48)), fg="black")
        movement = self.window.addRadio("Physical mouse/keyboard", mouse_mode, 1, [10, 95])
        movement.configure(bg=rgb((114, 94, 48)), activebackground=rgb((114, 94, 48)), selectcolor=rgb((114, 94, 48)))
        messages = self.window.addRadio("Window messages (compatible clients)", mouse_mode, 0, [10, 114])
        messages.configure(bg=rgb((114, 94, 48)), activebackground=rgb((114, 94, 48)), selectcolor=rgb((114, 94, 48)))

        items_label = self.window.addLabel("Item images", [290, 76])
        items_label.configure(bg=rgb((114, 94, 48)), fg="black")
        for text, y in (("Frames", 95), ("Corners", 114), ("None", 133)):
            radio = self.window.addRadio(text, items_mode, text, [310, y])
            radio.configure(bg=rgb((114, 94, 48)), activebackground=rgb((114, 94, 48)), selectcolor=rgb((114, 94, 48)))

        recapture = self.window.addCheck(
            recalibrate, [10, 158], False, textOfButton="Recalibrate existing profile"
        )
        recapture.configure(bg=rgb((114, 94, 48)), activebackground=rgb((114, 94, 48)),
                            selectcolor=rgb((114, 94, 48)))

        self.window.addButton("Create / Load", bootstrap, [95, 23], [290, 166])
        def close():
            from core.GUI import application_root, shutdown
            host = application_root()
            if host is self.window.windowID:
                shutdown()
                return
            self.window.destroyWindow()
            if host is not None:
                host.deiconify()

        self.window.Protocol(close)
        self.window.loop()
