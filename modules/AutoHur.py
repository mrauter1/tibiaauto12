from __future__ import annotations
from dataclasses import dataclass
import tkinter as tk
from tkinter import ttk
from conf.Hotkeys import Hotkey
from core import runtime
from core.ThreadManager import ThreadManager
from engine.support import Cooldown, arm_input
from modules.support_ui import SupportWindow, hotkeys, read_loader, set_enabled, write_loader

EnabledAutoHur = False


@dataclass(frozen=True)
class _Settings:
    positions: tuple[int, int, int, int]
    hotkey: str


class AutoHur:
    def __init__(self, StatsPositions, MOUSE_OPTION):
        self._positions = tuple(StatsPositions)
        self._sender = Hotkey(MOUSE_OPTION)
        self.ThreadManager = ThreadManager("ThreadAutoHur")
        self.ui = SupportWindow("AutoHur", "Module: Auto Hur", self.close)
        self.AutoHur = self.ui.window
        if not self.ui.owner:
            return
        data = read_loader("HurLoader")
        self.key = tk.StringVar(value=data.get("HotkeyHur", "F6"))
        ttk.Label(self.ui.body, text="Haste hotkey").grid(row=0, column=0, sticky="w")
        self.key_widget = ttk.Combobox(
            self.ui.body, values=hotkeys(), textvariable=self.key, state="readonly"
        )
        self.key_widget.grid(row=0, column=1)
        ttk.Label(self.ui.body, text="Casts only when the haste status icon is absent.").grid(
            row=1, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(
            self.ui.body, text="Legacy print, buff, and low-mana options are unsupported."
        ).grid(row=2, column=0, columnspan=2, sticky="w")
        self.button = ttk.Button(self.ui.body, text="AutoHur: OFF", command=self.toggle)
        self.button.grid(row=3, column=0, columnspan=2, sticky="ew", pady=5)
        self.ui.status_label.grid(row=4, column=0, columnspan=2, sticky="w")
        self.ui.report_worker(self.ThreadManager, on_error=self._worker_failed)

    def _worker_failed(self, error):
        del error
        global EnabledAutoHur
        EnabledAutoHur = False
        self.button.configure(text="AutoHur: OFF")
        set_enabled((self.key_widget,), True)

    def toggle(self):
        global EnabledAutoHur
        if EnabledAutoHur:
            EnabledAutoHur = False
            self.ThreadManager.PauseThread()
            set_enabled((self.key_widget,), True)
            self.button.configure(text="AutoHur: OFF")
            self.ui.status("Stopped")
            return
        if not self.key.get().strip():
            self.ui.status("Error: select a hotkey")
            return
        settings = _Settings(self._positions, self.key.get())
        write_loader("HurLoader", {"HotkeyHur": settings.hotkey})
        EnabledAutoHur = True
        set_enabled((self.key_widget,), False)
        self.button.configure(text="AutoHur: ON")
        self.ui.status("Armed — focus game")
        self.ThreadManager.NewThread(lambda: self._run(settings))

    def _run(self, settings):
        from core import HookWindow
        from engine.ScanHur import ScanHur

        arm_input(self._sender)
        cooldown = Cooldown(0.6)
        while True:
            runtime.checkpoint()
            try:
                with HookWindow.frame_scope():
                    needed = ScanHur(settings.positions)
            except runtime.CancelledError:
                raise
            except Exception as exc:
                raise RuntimeError(f"haste sensor failed; check stats coordinates: {exc}") from exc
            if needed and cooldown.ready():
                with runtime.input_transaction("auto haste"):
                    self._sender.Press(settings.hotkey)
                cooldown.mark()
            runtime.wait(0.2)

    def close(self):
        global EnabledAutoHur
        EnabledAutoHur = False
        self.ThreadManager.KillThread()
        if getattr(self, "ui", None) is not None and self.ui.owner and self.key.get().strip():
            try:
                write_loader("HurLoader", {"HotkeyHur": self.key.get()})
            except Exception:
                pass
