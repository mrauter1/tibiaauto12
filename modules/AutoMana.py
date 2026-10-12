from __future__ import annotations

from dataclasses import dataclass
import tkinter as tk
from tkinter import ttk

from conf.Constants import ManaColor, ManaColorFull
from conf.Hotkeys import Hotkey
from core import runtime
from core.ThreadManager import ThreadManager
from engine.support import Cooldown, Threshold, arm_input, choose_threshold
from modules.support_ui import (
    SupportWindow,
    hotkeys,
    integer,
    read_loader,
    set_enabled,
    write_loader,
)

EnabledAutoMana = False


@dataclass(frozen=True)
class _Settings:
    location: tuple[int, int]
    rules: tuple[Threshold, ...]


class AutoMana:
    def __init__(self, ManaLocation, MOUSE_OPTION):
        self.ThreadManager = ThreadManager("ThreadAutoMana")
        self._sender = Hotkey(MOUSE_OPTION)
        self._location = tuple(ManaLocation)
        self.ui = SupportWindow("AutoMana", "Module: Auto Mana", self.close)
        self.AutoMana = self.ui.window
        if not self.ui.owner:
            return
        data, self._vars = read_loader("ManaLoader"), []
        ttk.Label(self.ui.body, text="Lowest matching threshold has priority.").grid(
            row=0, column=0, columnspan=4, sticky="w"
        )
        for index, label in enumerate(("One", "Two"), 1):
            enabled = tk.BooleanVar(value=bool(data.get(f"ManaCheckStage{label}", False)))
            percent = tk.StringVar(value=str(data.get(f"ManaPercentageStage{label}", 50)))
            key = tk.StringVar(value=data.get(f"ManaHotkeyStage{label}", "F1"))
            widgets = (
                ttk.Checkbutton(self.ui.body, text=f"Stage {index}", variable=enabled),
                ttk.Spinbox(self.ui.body, from_=0, to=100, width=5, textvariable=percent),
                ttk.Combobox(
                    self.ui.body, values=hotkeys(), width=13, textvariable=key, state="readonly"
                ),
            )
            widgets[0].grid(row=index, column=0, sticky="w", pady=3)
            widgets[1].grid(row=index, column=1)
            ttk.Label(self.ui.body, text="%").grid(row=index, column=2)
            widgets[2].grid(row=index, column=3, padx=4)
            self._vars.append((label, enabled, percent, key, widgets))
        ttk.Label(self.ui.body, text="Legacy print and buff options are unsupported.").grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(6, 2)
        )
        self.button = ttk.Button(self.ui.body, text="AutoMana: OFF", command=self.toggle)
        self.button.grid(row=4, column=0, columnspan=4, sticky="ew", pady=5)
        self.ui.status_label.grid(row=5, column=0, columnspan=4, sticky="w")
        self.ui.report_worker(self.ThreadManager, on_error=self._worker_failed)

    def _worker_failed(self, error):
        del error
        global EnabledAutoMana
        EnabledAutoMana = False
        self.button.configure(text="AutoMana: OFF")
        set_enabled((w for v in self._vars for w in v[4]), True)

    def _snapshot(self):
        rules = tuple(
            Threshold(v[1].get(), integer(v[2].get(), f"stage {i} threshold"), v[3].get())
            for i, v in enumerate(self._vars, 1)
        )
        if not any(r.enabled for r in rules):
            raise ValueError("enable at least one mana stage")
        values = {}
        for label, e, p, k, _ in self._vars:
            values.update(
                {
                    f"ManaCheckStage{label}": e.get(),
                    f"ManaPercentageStage{label}": int(p.get()),
                    f"ManaHotkeyStage{label}": k.get(),
                }
            )
        write_loader("ManaLoader", values)
        return _Settings(self._location, rules)

    def toggle(self):
        global EnabledAutoMana
        if EnabledAutoMana:
            EnabledAutoMana = False
            self.ThreadManager.PauseThread()
            self.button.configure(text="AutoMana: OFF")
            set_enabled((w for v in self._vars for w in v[4]), True)
            self.ui.status("Stopped")
            return
        try:
            settings = self._snapshot()
        except ValueError as exc:
            self.ui.status(f"Error: {exc}")
            return
        EnabledAutoMana = True
        set_enabled((w for v in self._vars for w in v[4]), False)
        self.button.configure(text="AutoMana: ON")
        self.ui.status("Armed — focus game")
        self.ThreadManager.NewThread(lambda: self._run(settings))

    def _run(self, settings):
        from engine.ScanStages import ScanStages
        from core import HookWindow

        arm_input(self._sender)
        scan, cooldown = ScanStages("Mana"), Cooldown(0.10)
        while True:
            runtime.checkpoint()
            try:
                with HookWindow.frame_scope():
                    value = scan.ScanStages(settings.location, ManaColor, ManaColorFull)
            except runtime.CancelledError:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"mana sensor failed; check the configured mana bar: {exc}"
                ) from exc
            if value is None:
                raise RuntimeError("mana sensor returned no readable bar; check its coordinates")
            rule = choose_threshold(value, settings.rules)
            if rule is not None and cooldown.ready():
                with runtime.input_transaction("auto mana"):
                    self._sender.Press(rule.hotkey)
                cooldown.mark()
            runtime.wait(0.05)

    def close(self):
        global EnabledAutoMana
        EnabledAutoMana = False
        self.ThreadManager.KillThread()
        if getattr(self, "ui", None) is not None and self.ui.owner:
            try:
                self._snapshot()
            except Exception:
                pass
