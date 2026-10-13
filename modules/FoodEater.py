from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from tibia_client.application import get_client
from tibia_client.models import Sensor, SensorKind
from features.support import FoodFeature
from modules.support_ui import SupportWindow, hotkeys, read_loader, set_enabled, write_loader


class FoodEater:
    enabled = False

    def __init__(self, root, stats_positions, MOUSE_OPTION):
        del root, MOUSE_OPTION
        self._positions = tuple(stats_positions)
        self._client = get_client()
        self.enabled = False
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
        self.ui.bind_loader("FoodEaterLoader", self._apply_settings)
        self.ui.report_feature(self._client, "FoodEater", on_inactive=self._feature_stopped)

    def _apply_settings(self, data):
        self.key.set(data["HotkeyFood"])
        self._feature_stopped()

    def _feature_stopped(self, error=None):
        del error
        self.enabled = False
        self.button.configure(text="FoodEater: OFF")
        set_enabled((self.key_widget,), True)

    def toggle(self):
        if self.enabled:
            self._client.stop_feature("FoodEater")
            self.enabled = False
            self.ui._expected_active = False
            set_enabled((self.key_widget,), True)
            self.button.configure(text="FoodEater: OFF")
            self.ui.status("Stopped")
            return
        if not self.key.get().strip():
            self.ui.status("Error: select a hotkey")
            return
        try:
            write_loader("FoodEaterLoader", {"HotkeyFood": self.key.get()})
            self._client.start_feature(
                FoodFeature(Sensor(SensorKind.HUNGRY, self._positions), self.key.get())
            )
        except (ValueError, RuntimeError) as exc:
            self.ui.status(f"Error: {exc}")
            return
        self.enabled = True
        self.ui._expected_active = True
        set_enabled((self.key_widget,), False)
        self.button.configure(text="FoodEater: ON")
        self.ui.status("Armed — focus game")

    def close(self):
        self.enabled = False
        try:
            self._client.stop_feature("FoodEater")
        finally:
            if getattr(self, "ui", None) is not None and self.ui.owner and self.key.get().strip():
                try:
                    write_loader("FoodEaterLoader", {"HotkeyFood": self.key.get()})
                except Exception:
                    pass

    destroy = close
