from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from tibia_client.application import get_client
from tibia_client.models import Sensor, SensorKind
from features.support import ManaFeature
from engine.support import Threshold
from modules.support_ui import (
    SupportWindow,
    hotkeys,
    integer,
    read_loader,
    set_enabled,
    write_loader,
)


class AutoMana:
    def __init__(self, ManaLocation, MOUSE_OPTION):
        del MOUSE_OPTION
        self._client, self._location = get_client(), tuple(ManaLocation)
        self._enabled = False
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
        self.ui.report_feature(self._client, "AutoMana", on_inactive=self._feature_stopped)

    def _feature_stopped(self, error=None):
        del error
        self._enabled = False
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
        return rules

    def toggle(self):
        if self._enabled:
            self._client.stop_feature("AutoMana")
            self._enabled = False
            self.ui._expected_active = False
            self.button.configure(text="AutoMana: OFF")
            set_enabled((w for v in self._vars for w in v[4]), True)
            self.ui.status("Stopped")
            return
        try:
            rules = self._snapshot()
            self._client.start_feature(ManaFeature(Sensor(SensorKind.MANA, self._location), rules))
        except (ValueError, RuntimeError) as exc:
            self.ui.status(f"Error: {exc}")
            return
        self._enabled = True
        self.ui._expected_active = True
        set_enabled((w for v in self._vars for w in v[4]), False)
        self.button.configure(text="AutoMana: ON")
        self.ui.status("Armed — focus game")

    def close(self):
        self._enabled = False
        try:
            self._client.stop_feature("AutoMana")
        finally:
            if getattr(self, "ui", None) is not None and self.ui.owner:
                try:
                    self._snapshot()
                except Exception:
                    pass
