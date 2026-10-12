from __future__ import annotations

from dataclasses import dataclass
import tkinter as tk
from tkinter import ttk

from conf.Constants import LifeColor, LifeColorFull
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

EnabledAutoHeal = False
Life = None  # compatibility only; other modules obtain a fresh sample


@dataclass(frozen=True)
class _Settings:
    location: tuple[int, int]
    rules: tuple[Threshold, ...]


class AutoHeal:
    def __init__(self, HealthLocation, MOUSE_OPTION):
        self.ThreadManager = ThreadManager("ThreadAutoHeal")
        self._sender, self._location = Hotkey(MOUSE_OPTION), tuple(HealthLocation)
        self.ui = SupportWindow("AutoHeal", "Module: Auto Heal", self.close)
        self.AutoHeal = self.ui.window
        if not self.ui.owner:
            return
        data, self._vars = read_loader("HealthLoader"), []
        ttk.Label(self.ui.body, text="Lowest matching threshold has priority.").grid(
            row=0, column=0, columnspan=4, sticky="w"
        )
        for index, label in enumerate(("One", "Two", "Three"), 1):
            enabled = tk.BooleanVar(value=bool(data.get(f"LifeCheckStage{label}", False)))
            percent = tk.StringVar(value=str(data.get(f"LifePercentageStage{label}", 50)))
            key = tk.StringVar(value=data.get(f"LifeHotkeyStage{label}", "F1"))
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
        ttk.Label(self.ui.body, text="Legacy print, buff, and cure options are unsupported.").grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(6, 2)
        )
        self.button = ttk.Button(self.ui.body, text="AutoHealing: OFF", command=self.toggle)
        self.button.grid(row=5, column=0, columnspan=4, sticky="ew", pady=5)
        self.ui.status_label.grid(row=6, column=0, columnspan=4, sticky="w")
        self.ui.report_worker(self.ThreadManager, on_error=self._worker_failed)

    def _worker_failed(self, error):
        del error
        global EnabledAutoHeal
        EnabledAutoHeal = False
        self.button.configure(text="AutoHealing: OFF")
        set_enabled((w for v in self._vars for w in v[4]), True)

    def _snapshot(self):
        rules = tuple(
            Threshold(v[1].get(), integer(v[2].get(), f"stage {i} threshold"), v[3].get())
            for i, v in enumerate(self._vars, 1)
        )
        if not any(r.enabled for r in rules):
            raise ValueError("enable at least one healing stage")
        values = {}
        for label, e, p, k, _ in self._vars:
            values.update(
                {
                    f"LifeCheckStage{label}": e.get(),
                    f"LifePercentageStage{label}": int(p.get()),
                    f"LifeHotkeyStage{label}": k.get(),
                }
            )
        write_loader("HealthLoader", values)
        return _Settings(self._location, rules)

    def toggle(self):
        global EnabledAutoHeal
        if EnabledAutoHeal:
            EnabledAutoHeal = False
            self.ThreadManager.PauseThread()
            self.button.configure(text="AutoHealing: OFF")
            set_enabled((w for v in self._vars for w in v[4]), True)
            self.ui.status("Stopped")
            return
        try:
            settings = self._snapshot()
        except ValueError as exc:
            self.ui.status(f"Error: {exc}")
            return
        EnabledAutoHeal = True
        set_enabled((w for v in self._vars for w in v[4]), False)
        self.button.configure(text="AutoHealing: ON")
        self.ui.status("Armed — focus game")
        self.ThreadManager.NewThread(lambda: self._run(settings))

    def _run(self, settings):
        global Life
        from engine.ScanStages import ScanStages
        from core import HookWindow

        arm_input(self._sender)
        scan, cooldown = ScanStages("Life"), Cooldown(0.15)
        while True:
            runtime.checkpoint()
            try:
                with HookWindow.frame_scope():
                    value = scan.ScanStages(settings.location, LifeColor, LifeColorFull)
            except runtime.CancelledError:
                raise
            except Exception as exc:
                Life = None
                raise RuntimeError(
                    f"life sensor failed; check the configured life bar: {exc}"
                ) from exc
            if value is None:
                Life = None
                raise RuntimeError("life sensor returned no readable bar; check its coordinates")
            Life = value
            rule = choose_threshold(value, settings.rules)
            if rule is not None and cooldown.ready():
                with runtime.input_transaction("auto heal"):
                    self._sender.Press(rule.hotkey)
                cooldown.mark()
            runtime.wait(0.05)

    def close(self):
        global EnabledAutoHeal
        EnabledAutoHeal = False
        self.ThreadManager.KillThread()
        if getattr(self, "ui", None) is not None and self.ui.owner:
            try:
                self._snapshot()
            except Exception:
                pass
