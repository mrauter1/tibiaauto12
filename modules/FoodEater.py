from __future__ import annotations
from dataclasses import dataclass
import tkinter as tk
from tkinter import ttk
from conf.Hotkeys import Hotkey
from core import runtime
from core.ThreadManager import ThreadManager
from engine.support import Cooldown, arm_input
from modules.support_ui import SupportWindow, hotkeys, read_loader, set_enabled, write_loader


@dataclass(frozen=True)
class _Settings:
    positions: tuple[int, int, int, int]
    hotkey: str


class FoodEater:
    enabled = False

    def __init__(self, root, stats_positions, MOUSE_OPTION):
        del root
        self._positions = tuple(stats_positions)
        self._sender = Hotkey(MOUSE_OPTION)
        self.ThreadManager = ThreadManager("ThreadFoodEater")
        self.ui = SupportWindow("FoodEater", "Module: Food Eater", self.close)
        self.window = self.ui
        if not self.ui.owner:
            return
        data = read_loader("FoodEaterLoader")
        self.key = tk.StringVar(value=data.get("HotkeyFood", "Shift + F12"))
        ttk.Label(self.ui.body, text="Food hotkey").grid(row=0, column=0, sticky="w")
        self.key_widget = ttk.Combobox(
            self.ui.body, values=hotkeys(), textvariable=self.key, state="readonly"
        )
        self.key_widget.grid(row=0, column=1)
        ttk.Label(self.ui.body, text="Eats when the starving status icon is visible.").grid(
            row=1, column=0, columnspan=2, sticky="w"
        )
        self.button = ttk.Button(self.ui.body, text="FoodEater: OFF", command=self.toggle)
        self.button.grid(row=2, column=0, columnspan=2, sticky="ew", pady=5)
        self.ui.status_label.grid(row=3, column=0, columnspan=2, sticky="w")
        self.ui.report_worker(self.ThreadManager, on_error=self._worker_failed)

    def _worker_failed(self, error):
        del error
        self.enabled = False
        self.button.configure(text="FoodEater: OFF")
        set_enabled((self.key_widget,), True)

    def toggle(self):
        if self.enabled:
            self.enabled = False
            self.ThreadManager.PauseThread()
            set_enabled((self.key_widget,), True)
            self.button.configure(text="FoodEater: OFF")
            self.ui.status("Stopped")
            return
        if not self.key.get().strip():
            self.ui.status("Error: select a hotkey")
            return
        settings = _Settings(self._positions, self.key.get())
        write_loader("FoodEaterLoader", {"HotkeyFood": settings.hotkey})
        self.enabled = True
        set_enabled((self.key_widget,), False)
        self.button.configure(text="FoodEater: ON")
        self.ui.status("Armed — focus game")
        self.ThreadManager.NewThread(lambda: self._run(settings))

    def _run(self, settings):
        from core import HookWindow
        from engine.ScanFood import scan_food

        arm_input(self._sender)
        cooldown = Cooldown(0.5)
        while True:
            runtime.checkpoint()
            try:
                with HookWindow.frame_scope():
                    hungry = scan_food(settings.positions)
            except runtime.CancelledError:
                raise
            except Exception as exc:
                raise RuntimeError(f"food sensor failed; check stats coordinates: {exc}") from exc
            if hungry and cooldown.ready():
                with runtime.input_transaction("eat food"):
                    self._sender.Press(settings.hotkey)
                cooldown.mark()
            runtime.wait(0.2)

    def close(self):
        self.enabled = False
        self.ThreadManager.KillThread()
        if getattr(self, "ui", None) is not None and self.ui.owner and self.key.get().strip():
            try:
                write_loader("FoodEaterLoader", {"HotkeyFood": self.key.get()})
            except Exception:
                pass

    destroy = close
