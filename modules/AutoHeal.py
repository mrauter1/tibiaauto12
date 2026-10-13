from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from tibia_client.application import get_client
from tibia_client.models import Sensor, SensorKind
from features.support import HealFeature
from engine.support import Threshold
from modules.support_ui import (
    SupportWindow,
    hotkeys,
    integer,
    read_loader,
    set_enabled,
    write_loader,
)


class AutoHeal:
    def __init__(self, HealthLocation, MOUSE_OPTION):
        del MOUSE_OPTION
        self._client, self._location = get_client(), tuple(HealthLocation)
        self._enabled = False
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
        self.ui.bind_loader("HealthLoader", self._apply_settings)
        self.ui.report_feature(self._client, "AutoHeal", on_inactive=self._feature_stopped)

    def _apply_settings(self, data):
        for label, enabled, percent, key, _ in self._vars:
            enabled.set(data[f"LifeCheckStage{label}"])
            percent.set(str(data[f"LifePercentageStage{label}"]))
            key.set(data[f"LifeHotkeyStage{label}"])
        self._feature_stopped()

    def _feature_stopped(self, error=None):
        del error
        self._enabled = False
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
        return rules

    def toggle(self):
        if self._enabled:
            self._client.stop_feature("AutoHeal")
            self._enabled = False
            self.ui._expected_active = False
            self.button.configure(text="AutoHealing: OFF")
            set_enabled((w for v in self._vars for w in v[4]), True)
            self.ui.status("Stopped")
            return
        try:
            rules = self._snapshot()
            self._client.start_feature(
                HealFeature(Sensor(SensorKind.HEALTH, self._location), rules)
            )
        except (ValueError, RuntimeError) as exc:
            self.ui.status(f"Error: {exc}")
            return
        self._enabled = True
        self.ui._expected_active = True
        set_enabled((w for v in self._vars for w in v[4]), False)
        self.button.configure(text="AutoHealing: ON")
        self.ui.status("Armed — focus game")

    def close(self):
        self._enabled = False
        try:
            self._client.stop_feature("AutoHeal")
        finally:
            if getattr(self, "ui", None) is not None and self.ui.owner:
                try:
                    self._snapshot()
                except Exception:
                    pass
