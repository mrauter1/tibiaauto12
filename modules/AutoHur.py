from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from tibia_client.application import get_client
from tibia_client.models import Sensor, SensorKind
from features.support import HasteFeature
from modules.support_ui import SupportWindow, hotkeys, read_loader, set_enabled, write_loader


class AutoHur:
    def __init__(self, StatsPositions, MOUSE_OPTION):
        del MOUSE_OPTION
        self._positions = tuple(StatsPositions)
        self._client = get_client()
        self._enabled = False
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
        self.ui.report_feature(self._client, "AutoHur", on_inactive=self._feature_stopped)

    def _feature_stopped(self, error=None):
        del error
        self._enabled = False
        self.button.configure(text="AutoHur: OFF")
        set_enabled((self.key_widget,), True)

    def toggle(self):
        if self._enabled:
            self._client.stop_feature("AutoHur")
            self._enabled = False
            self.ui._expected_active = False
            set_enabled((self.key_widget,), True)
            self.button.configure(text="AutoHur: OFF")
            self.ui.status("Stopped")
            return
        if not self.key.get().strip():
            self.ui.status("Error: select a hotkey")
            return
        try:
            write_loader("HurLoader", {"HotkeyHur": self.key.get()})
            self._client.start_feature(
                HasteFeature(Sensor(SensorKind.HASTED, self._positions), self.key.get())
            )
        except (ValueError, RuntimeError) as exc:
            self.ui.status(f"Error: {exc}")
            return
        self._enabled = True
        self.ui._expected_active = True
        set_enabled((self.key_widget,), False)
        self.button.configure(text="AutoHur: ON")
        self.ui.status("Armed — focus game")

    def close(self):
        self._enabled = False
        try:
            self._client.stop_feature("AutoHur")
        finally:
            if getattr(self, "ui", None) is not None and self.ui.owner and self.key.get().strip():
                try:
                    write_loader("HurLoader", {"HotkeyHur": self.key.get()})
                except Exception:
                    pass
