"""Profile selection and one-shot client calibration."""
from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Mapping
import queue
import threading
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

        capture = CaptureProcess(capture_size=frame_size, startup_timeout=15.0)
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
    except CalibrationError as exc:
        if capture is not None:
            exc.frame = frame
        raise
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
                    items_mode: str, *, recalibrate: bool = False, getters=None,
                    window_target=None) -> dict[str, Any]:
    """Prepare a profile fully in memory; callers write only after success."""
    filename = profile_filename(filename)
    from tibia_client.window_target import WindowTarget

    target = WindowTarget.from_config({"window_target": window_target})
    if target.kind == "sandbox" and mouse_mode != 1:
        raise CalibrationError("Windows Sandbox requires physical mouse/keyboard input")
    if target.kind == "sandbox" and frame_size != target.size:
        raise CalibrationError("capture size differs from the Sandbox viewport")
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
            if WindowTarget.from_config(existing).fingerprint != target.fingerprint:
                raise CalibrationError(
                    "desktop target or Sandbox viewport/focus samples changed; "
                    "enable 'Recalibrate existing profile'"
                )
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
    if target.kind == "sandbox":
        profile["window_target"] = target.to_dict()
    else:
        profile.pop("window_target", None)
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
        sandbox = ConfManager.get("conf.json").get("window_target", {}).get("kind") == "sandbox"
        self._preparing = False
        self._closed = False
        self._prepare_generation = 0
        self._prepare_timer = None
        self.window.windowID.bind("<Destroy>", self._on_destroy, add="+")

        def open_root(filename, generation=None):
            if not self._is_open(generation):
                return
            self.window.withdraw()
            from modules.Root import root
            try:
                root(self.selected_window_name, filename)
            except Exception:
                if self._is_open(generation):
                    self.window.deiconify()
                raise
            if self._is_open(generation):
                self._cancel_preparation()
                self.window.windowID.destroy()

        def bootstrap():
            if self._preparing or not self._is_open():
                return
            try:
                filename = profile_filename(name.get())
                config = ConfManager.get("conf.json")
                frame_size = tuple(config.get("capture_size", ()))
                if len(frame_size) != 2:
                    raise CalibrationError("capture size is unavailable; return to window selection")

                options = dict(recalibrate=recalibrate.get(),
                               window_target=deepcopy(config.get("window_target")))
                selected_mouse, selected_items = mouse_mode.get(), items_mode.get()
                direct_capture = sandbox or config.get("capture_hwnd") == config.get("input_hwnd")
                if direct_capture:
                    self._preparing = True
                    self._prepare_generation += 1
                    generation = self._prepare_generation
                    result = queue.Queue(maxsize=1)
                    self.window.withdraw()
                    from core.MoveMouse import MoveMouse
                    try:
                        input_hwnd = config.get("input_hwnd", config.get("hwnd"))
                        MoveMouse(hwnd_resolver=lambda: input_hwnd).Activate()
                    except RuntimeError:
                        self._preparing = False
                        self.window.deiconify()
                        raise

                    def prepare():
                        try:
                            value = prepare_profile(filename, frame_size, selected_mouse,
                                                    selected_items, **options)
                            result.put((value, None))
                        except Exception as exc:
                            result.put((None, exc))

                    def poll():
                        self._prepare_timer = None
                        if not self._is_open(generation):
                            return
                        try:
                            value, error = result.get_nowait()
                        except queue.Empty:
                            self._prepare_timer = self.window.windowID.after(50, poll)
                            return
                        self._preparing = False
                        if error:
                            self.window.deiconify()
                            if isinstance(error, CalibrationError) and hasattr(error, "frame"):
                                from modules.ImageCalibration import ImageCalibration
                                draft = ImageCalibration(self.window.windowID, error.frame, str(error)).run()
                                if draft is not None and self._is_open(generation):
                                    _add_runtime_options(draft, selected_mouse, selected_items)
                                    if options.get("window_target"):
                                        draft["window_target"] = options["window_target"]
                                    save_and_open(draft, filename, generation)
                                return
                            messagebox.showerror("Calibration could not be completed", error,
                                                 parent=self.window.windowID)
                            return
                        save_and_open(value, filename, generation)

                    def begin():
                        self._prepare_timer = None
                        if not self._is_open(generation):
                            return
                        try:
                            threading.Thread(target=prepare, name="sandbox-calibration",
                                             daemon=True).start()
                        except RuntimeError as exc:
                            self._preparing = False
                            self.window.deiconify()
                            messagebox.showerror("Calibration could not be completed", str(exc),
                                                 parent=self.window.windowID)
                            return
                        poll()

                    self._prepare_timer = self.window.windowID.after(350, begin)
                    return
                profile = prepare_profile(filename, frame_size, selected_mouse, selected_items,
                                          **options)
                save_and_open(profile, filename)
            except (RuntimeError, KeyError, TypeError, ValueError) as exc:
                if self._is_open():
                    messagebox.showerror("Calibration could not be completed", str(exc), parent=self.window.windowID)

        def save_and_open(profile, filename, generation=None):
            if not self._is_open(generation):
                return
            try:
                # Failed detection never reaches this atomic replacement, preserving any old profile.
                ConfManager.set(profile, filename)
                if not self._is_open(generation):
                    return

                def activate(current):
                    current["configured"] = True
                    current["preferences_name"] = filename

                ConfManager.update("conf.json", activate)
                open_root(filename, generation)
            except (RuntimeError, KeyError, TypeError, ValueError) as exc:
                if self._is_open(generation):
                    messagebox.showerror("Calibration could not be completed", str(exc), parent=self.window.windowID)

        self.window.addEntry([165, 35], name, 28)
        self.window.addLabel("Name Of The Json Conf", [24, 35])
        label = self.window.addLabel("Input mode", [30, 76])
        label.configure(bg=rgb((114, 94, 48)), fg="black")
        movement = self.window.addRadio("Physical mouse/keyboard", mouse_mode, 1, [10, 95])
        movement.configure(bg=rgb((114, 94, 48)), activebackground=rgb((114, 94, 48)), selectcolor=rgb((114, 94, 48)))
        messages = self.window.addRadio("Window messages (compatible clients)", mouse_mode, 0, [10, 114])
        messages.configure(bg=rgb((114, 94, 48)), activebackground=rgb((114, 94, 48)), selectcolor=rgb((114, 94, 48)))
        if sandbox:
            messages.configure(state="disabled")
            self.window.windowID.geometry("414x245")
            tk.Label(self.window.windowID, text="Create / Load brings Sandbox forward automatically.\n"
                     "Keep Tibia active in the guest and its window layout fixed.",
                     wraplength=400).place(x=8, y=203)

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

        def open_keyboard_modules():
            if self._preparing or not self._is_open():
                return
            self.window.withdraw()
            from modules.Root import root
            try:
                root(self.selected_window_name, None, keyboard_only=True)
            except Exception:
                self.window.deiconify()
                raise

        self.window.windowID.geometry("414x285")
        self.window.addButton("Open keyboard modules (no image calibration)",
                              open_keyboard_modules, [390, 28], [10, 245])
        def close():
            self._cancel_preparation()
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

    def _is_open(self, generation=None) -> bool:
        if self._closed or (generation is not None and generation != self._prepare_generation):
            return False
        try:
            return bool(self.window.windowID.winfo_exists())
        except tk.TclError:
            return False

    def _cancel_preparation(self) -> None:
        """Invalidate countdowns and results; native work may finish without saving."""
        self._closed = True
        self._preparing = False
        self._prepare_generation += 1
        timer, self._prepare_timer = self._prepare_timer, None
        if timer is not None:
            try:
                self.window.windowID.after_cancel(timer)
            except tk.TclError:
                pass

    def _on_destroy(self, event) -> None:
        # Tk also delivers child-widget destruction events to this binding.
        if event.widget is self.window.windowID:
            self._cancel_preparation()
