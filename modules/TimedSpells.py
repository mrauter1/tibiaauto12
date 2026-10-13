from __future__ import annotations
import tkinter as tk
from tkinter import ttk
from engine.support import parse_spells
from modules.support_ui import SupportWindow, hotkeys, read_loader, set_enabled, write_loader
from tibia_client.application import get_client
from features.support import TimedSpellsFeature


class TimedSpells:
    enabled = False

    def __init__(self, root, MOUSE_OPTION):
        del root
        del MOUSE_OPTION
        self._client = get_client()
        self.ui = SupportWindow("TimedSpells", "Module: Timed spells", self.close)
        self.window = self.ui
        if not self.ui.owner:
            return
        data = read_loader("TimedSpellsLoader")
        self.key = tk.StringVar(value=data.get("Hotkey", "Shift + F12"))
        self.interval = tk.StringVar(value=str(data.get("CastEvery", "")))
        self.instant = tk.BooleanVar(value=bool(data.get("InstantExecute", False)))
        ttk.Label(self.ui.body, text="Hotkey").grid(row=0, column=0, sticky="w")
        self.key_widget = ttk.Combobox(
            self.ui.body, values=hotkeys(), textvariable=self.key, state="readonly"
        )
        self.key_widget.grid(row=0, column=1)
        ttk.Label(self.ui.body, text="Every seconds").grid(row=1, column=0, sticky="w")
        self.interval_widget = ttk.Entry(self.ui.body, textvariable=self.interval, width=10)
        self.interval_widget.grid(row=1, column=1, sticky="w")
        self.add_button = ttk.Button(self.ui.body, text="Add / replace", command=self.add_item)
        self.add_button.grid(row=2, column=0, sticky="ew")
        self.remove_button = ttk.Button(self.ui.body, text="Remove", command=self.remove_item)
        self.remove_button.grid(row=2, column=1, sticky="ew")
        self.table = ttk.Treeview(
            self.ui.body, columns=("hotkey", "interval"), show="headings", height=7
        )
        self.table.heading("hotkey", text="Hotkey")
        self.table.heading("interval", text="Seconds")
        self.table.column("hotkey", width=135)
        self.table.column("interval", width=80)
        self.table.grid(row=3, column=0, columnspan=2)
        for row in data.get("Spells", []):
            try:
                spell = parse_spells((row,))[0]
            except ValueError:
                continue
            self.table.insert("", "end", values=(spell.hotkey, spell.interval))
        self.instant_widget = ttk.Checkbutton(
            self.ui.body, text="Cast each once on start", variable=self.instant
        )
        self.instant_widget.grid(row=4, column=0, columnspan=2, sticky="w")
        self.button = ttk.Button(self.ui.body, text="TimedSpells: OFF", command=self.toggle)
        self.button.grid(row=5, column=0, columnspan=2, sticky="ew", pady=5)
        self.ui.status_label.grid(row=6, column=0, columnspan=2, sticky="w")
        self._controls = (
            self.key_widget,
            self.interval_widget,
            self.add_button,
            self.remove_button,
            self.table,
            self.instant_widget,
        )
        self.ui.bind_loader("TimedSpellsLoader", self._apply_settings)
        self.ui.report_feature(self._client, "TimedSpells", on_inactive=self._worker_failed)

    def _apply_settings(self, data):
        spells = parse_spells(data["Spells"])
        self.instant.set(data["InstantExecute"])
        for item in self.table.get_children():
            self.table.delete(item)
        for spell in spells:
            self.table.insert("", "end", values=(spell.hotkey, spell.interval))
        self._worker_failed()

    def _worker_failed(self, error=None):
        del error
        self.enabled = False
        self.button.configure(text="TimedSpells: OFF")
        set_enabled(self._controls, True)

    def _rows(self):
        return tuple(self.table.item(item, "values") for item in self.table.get_children())

    def add_item(self):
        try:
            spell = parse_spells(((self.key.get(), self.interval.get()),))[0]
        except ValueError as exc:
            self.ui.status(f"Error: {exc}")
            return
        for item in self.table.get_children():
            if self.table.item(item, "values")[0] == spell.hotkey:
                self.table.delete(item)
        self.table.insert("", "end", values=(spell.hotkey, spell.interval))
        self.ui.status("Spell saved in table")

    def remove_item(self):
        for item in self.table.selection():
            self.table.delete(item)

    def _snapshot(self):
        spells = parse_spells(self._rows())
        write_loader(
            "TimedSpellsLoader",
            {
                "InstantExecute": self.instant.get(),
                "Hotkey": self.key.get(),
                "CastEvery": self.interval.get(),
                "Spells": [[s.hotkey, s.interval] for s in spells],
            },
        )
        return spells, self.instant.get()

    def toggle(self):
        if self.enabled:
            self.enabled = False
            self._client.stop_feature("TimedSpells")
            self.ui._expected_active = False
            set_enabled(self._controls, True)
            self.button.configure(text="TimedSpells: OFF")
            self.ui.status("Stopped")
            return
        try:
            spells, instant = self._snapshot()
        except ValueError as exc:
            self.ui.status(f"Error: {exc}")
            return
        self.enabled = True
        set_enabled(self._controls, False)
        self.button.configure(text="TimedSpells: ON")
        self.ui.status("Armed — focus game")
        try:
            self._client.start_feature(TimedSpellsFeature(spells, immediate=instant, spacing=0.15))
        except (ValueError, RuntimeError) as exc:
            self.enabled = False
            set_enabled(self._controls, True)
            self.button.configure(text="TimedSpells: OFF")
            self.ui.status(f"Error: {exc}")
            return
        self.ui._expected_active = True

    def close(self):
        self.enabled = False
        try:
            self._client.stop_feature("TimedSpells")
        finally:
            try:
                self._snapshot()
            except Exception:
                pass

    destroy = close
