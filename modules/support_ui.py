"""Small Tk helpers for support modules; all methods run on the Tk thread."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from dataclasses import dataclass
from typing import Callable, Mapping

from conf.Hotkeys import Hotkey
from conf.conf_manager import ConfManager


_windows = {}


def read_loader(name: str) -> dict:
    return dict(ConfManager.get("GUILoader.json")["Loaders"].get(name, {}))


def write_loader(name: str, values: Mapping[str, object]) -> None:
    def update(data):
        data.setdefault("Loaders", {}).setdefault(name, {}).update(values)
        return data

    ConfManager.update("GUILoader.json", update)


def integer(value: object, label: str, low: int = 0, high: int = 100) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number") from None
    if not low <= parsed <= high:
        raise ValueError(f"{label} must be between {low} and {high}")
    return parsed


def client_point(x: object, y: object) -> tuple[int, int]:
    return integer(x, "client X", 0, 100000), integer(y, "client Y", 0, 100000)


class SupportWindow:
    """A non-modal, single-instance Toplevel with lifecycle/status helpers."""

    def __init__(self, key: str, title: str, on_close: Callable[[], None]) -> None:
        old = _windows.get(key)
        if old is not None:
            try:
                if old.winfo_exists():
                    old.deiconify()
                    old.lift()
                    self.window = old
                    self.owner = False
                    return
            except tk.TclError:
                pass
        self.window = tk.Toplevel()
        self.window.title(title)
        self.window.resizable(False, False)
        self.owner = True
        _windows[key] = self.window
        self.body = ttk.Frame(self.window, padding=10)
        self.body.grid(sticky="nsew")
        self.status_var = tk.StringVar(value="Stopped")
        self.status_label = ttk.Label(self.body, textvariable=self.status_var)
        self._on_close = on_close
        self.window.protocol("WM_DELETE_WINDOW", self.close)

    def close(self) -> None:
        if not self.owner:
            return
        try:
            self._on_close()
        finally:
            _windows.pop(next((k for k, v in _windows.items() if v is self.window), ""), None)
            self.window.destroy()

    def status(self, text: str) -> None:
        if self.owner:
            self.status_var.set(text)

    def report_worker(self, manager, interval: int = 250, on_error=None) -> None:
        if not self.owner:
            return
        info = manager.Status()
        if info and info.get("error") is not None:
            marker = (info.get("generation"), id(info["error"]))
            if getattr(self, "_reported_error", None) != marker:
                self._reported_error = marker
                self.status(f"Error: {info['error']}")
                if on_error is not None:
                    on_error(info["error"])
        elif info and info.get("phase"):
            phase = info["phase"]
            if getattr(self, "_reported_phase", None) != phase:
                self._reported_phase = phase
                self.status(phase)
        try:
            self.window.after(interval, self.report_worker, manager, interval, on_error)
        except tk.TclError:
            pass


def hotkeys() -> tuple[str, ...]:
    return tuple(Hotkey.Hotkeys)


def set_enabled(widgets, enabled: bool) -> None:
    state = "normal" if enabled else "disabled"
    for widget in widgets:
        try:
            widget.configure(state=state)
        except tk.TclError:
            pass


@dataclass(frozen=True)
class EquipmentSettings:
    item: str
    mode: int
    hotkey: str
    source: tuple[int, int]
    slot: tuple[int, int, int, int]
    life_location: tuple[int, int]
    require_below: bool
    threshold: int


class EquipmentSupport:
    """Shared UI/runtime for ring and amulet replacement."""

    def _equipment_init(
        self,
        *,
        key,
        title,
        loader,
        item_label,
        items,
        positions,
        life_location,
        mouse_option,
        empty_sensor,
        worker_name,
    ):
        from core.ThreadManager import ThreadManager

        self._loader, self._empty_sensor = loader, empty_sensor
        self._positions, self._life_location = tuple(positions), tuple(life_location)
        self._sender, self.ThreadManager = Hotkey(mouse_option), ThreadManager(worker_name)
        self._enabled = False
        self.ui = SupportWindow(key, title, self.close)
        setattr(self, key, self.ui.window)
        if not self.ui.owner:
            return
        data = read_loader(loader)
        prefix = "Ring" if loader == "RingLoader" else "Amulet"
        self.item = tk.StringVar(value=data.get(f"Name{prefix}", next(iter(items))))
        self.mode = tk.IntVar(value=int(data.get("RadioButton", 0)))
        self.key = tk.StringVar(value=data.get(f"Hotkey{prefix}", "F12"))
        self.x, self.y = (
            tk.StringVar(value=str(data.get("TextEntryX", "0"))),
            tk.StringVar(value=str(data.get("TextEntryY", "0"))),
        )
        self.condition = tk.BooleanVar(value=bool(data.get("LifeBellowThan", False)))
        self.threshold = tk.StringVar(value=str(data.get("BellowThan", 30)))
        ttk.Label(self.ui.body, text=item_label).grid(row=0, column=0, sticky="w")
        self.item_widget = ttk.Combobox(
            self.ui.body, values=tuple(items), textvariable=self.item, state="readonly", width=22
        )
        self.item_widget.grid(row=0, column=1, columnspan=3, sticky="ew")
        self.hotkey_radio = ttk.Radiobutton(
            self.ui.body, text="Use hotkey", variable=self.mode, value=0
        )
        self.hotkey_radio.grid(row=1, column=0, sticky="w")
        self.key_widget = ttk.Combobox(
            self.ui.body, values=hotkeys(), textvariable=self.key, state="readonly", width=14
        )
        self.key_widget.grid(row=1, column=1, columnspan=2, sticky="w")
        self.drag_radio = ttk.Radiobutton(
            self.ui.body, text="Drag from client-local", variable=self.mode, value=1
        )
        self.drag_radio.grid(row=2, column=0, sticky="w")
        ttk.Label(self.ui.body, text="X").grid(row=2, column=1)
        self.x_widget = ttk.Entry(self.ui.body, textvariable=self.x, width=6)
        self.x_widget.grid(row=2, column=2)
        ttk.Label(self.ui.body, text="Y").grid(row=2, column=3)
        self.y_widget = ttk.Entry(self.ui.body, textvariable=self.y, width=6)
        self.y_widget.grid(row=2, column=4)
        self.condition_widget = ttk.Checkbutton(
            self.ui.body, text="Only when life ≤", variable=self.condition
        )
        self.condition_widget.grid(row=3, column=0, columnspan=2, sticky="w")
        self.threshold_widget = ttk.Spinbox(
            self.ui.body, from_=0, to=100, width=5, textvariable=self.threshold
        )
        self.threshold_widget.grid(row=3, column=2, sticky="w")
        self.button = ttk.Button(self.ui.body, text=f"{key}: OFF", command=self.toggle)
        self.button.grid(row=4, column=0, columnspan=5, sticky="ew", pady=5)
        self.ui.status_label.grid(row=5, column=0, columnspan=5, sticky="w")
        self._controls = (
            self.item_widget,
            self.hotkey_radio,
            self.key_widget,
            self.drag_radio,
            self.x_widget,
            self.y_widget,
            self.condition_widget,
            self.threshold_widget,
        )
        self.ui.report_worker(self.ThreadManager, on_error=self._equipment_failed)

    def _equipment_failed(self, error):
        del error
        self._enabled = False
        self.button.configure(text=f"{self.__class__.__name__}: OFF")
        set_enabled(self._controls, True)

    def _equipment_snapshot(self):
        from engine.support import EquipmentPolicy

        mode = integer(self.mode.get(), "mode", 0, 1)
        threshold = integer(self.threshold.get(), "life threshold")
        source = client_point(self.x.get(), self.y.get()) if mode == 1 else (0, 0)
        if mode == 1:
            try:
                width, height = self._sender.MainWindowSize()
            except Exception as exc:
                raise ValueError(f"could not validate client-local coordinates: {exc}") from exc
            if source[0] >= width or source[1] >= height:
                raise ValueError(f"client-local X/Y must fit inside {width} × {height}")
        prefix = "Ring" if self._loader == "RingLoader" else "Amulet"
        values = {
            "RadioButton": mode,
            f"Name{prefix}": self.item.get(),
            f"Hotkey{prefix}": self.key.get(),
            "TextEntryX": str(self.x.get()),
            "TextEntryY": str(self.y.get()),
            "LifeBellowThan": self.condition.get(),
            "BellowThan": threshold,
        }
        if mode == 0 and not self.key.get().strip():
            raise ValueError("select a hotkey")
        EquipmentPolicy(self.condition.get(), threshold)
        write_loader(self._loader, values)
        return EquipmentSettings(
            self.item.get(),
            mode,
            self.key.get(),
            source,
            self._positions,
            self._life_location,
            self.condition.get(),
            threshold,
        )

    def toggle(self):
        if self._enabled:
            self._enabled = False
            self.ThreadManager.PauseThread()
            self.button.configure(text=f"{self.__class__.__name__}: OFF")
            set_enabled(self._controls, True)
            self.ui.status("Stopped")
            return
        try:
            settings = self._equipment_snapshot()
        except ValueError as exc:
            self.ui.status(f"Error: {exc}")
            return
        self._enabled = True
        set_enabled(self._controls, False)
        self.button.configure(text=f"{self.__class__.__name__}: ON")
        self.ui.status("Armed — focus game")
        self.ThreadManager.NewThread(lambda: self._equipment_run(settings))

    def _equipment_run(self, settings):
        from conf.Constants import LifeColor, LifeColorFull
        from core import HookWindow, runtime
        from engine.ScanStages import ScanStages
        from engine.support import Cooldown, EquipmentPolicy, arm_input

        arm_input(self._sender)
        policy, cooldown = (
            EquipmentPolicy(settings.require_below, settings.threshold),
            Cooldown(0.5),
        )
        life_scan = ScanStages(f"Life from {self.__class__.__name__}")
        while True:
            runtime.checkpoint()
            try:
                with HookWindow.frame_scope():
                    empty = self._empty_sensor(settings.slot)
                    life = (
                        life_scan.ScanStages(settings.life_location, LifeColor, LifeColorFull)
                        if settings.require_below
                        else None
                    )
            except runtime.CancelledError:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"equipment sensor failed; check slot/life coordinates: {exc}"
                ) from exc
            if settings.require_below and life is None:
                raise RuntimeError("life sensor returned no readable bar; check its coordinates")
            if policy.should_equip(empty, life) and cooldown.ready():
                with runtime.input_transaction(f"equip {settings.item}"):
                    if settings.mode == 0:
                        self._sender.Press(settings.hotkey)
                    else:
                        before = self._sender.Position()
                        self._sender.DragTo(
                            settings.source, (settings.slot[0] + 16, settings.slot[1] + 16)
                        )
                        try:
                            self._sender.MoveTo(before[0], before[1])
                        except Exception:
                            pass
                cooldown.mark()
            runtime.wait(0.1)

    def close(self):
        self._enabled = False
        self.ThreadManager.KillThread()
        if getattr(self, "ui", None) is not None and self.ui.owner:
            try:
                self._equipment_snapshot()
            except Exception:
                pass
