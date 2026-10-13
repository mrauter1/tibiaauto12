"""Cavebot configuration UI backed by one cancellable headless session."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from PIL import Image, ImageTk

from conf.Constants import Hotkeys, Priority
from conf.MarksConf import NameMarks
from core.GUI import GUI
from core.GUISetter import GUISetter
from core.paths import project_path
from engine.CaveBot.routes import (
    Route,
    RouteError,
    Waypoint,
    WaypointAction,
    checkpoint_file,
    import_legacy_active_index,
    load_checkpoint,
    load_route,
    route_file,
    save_checkpoint,
    save_route,
)
from engine.CaveBot.editor import RouteEditor
from features.cavebot import CaveBot as CaveBotFeature, SessionConfig, TargetSpec
from tibia_client.application import get_client, poll_hotkey
from tibia_client.hunt_packs import (
    CaveBotSettings,
    HuntPack,
    HuntPackError,
    LocalBinding,
    SupplyPolicy,
    Target,
    apply_support_to_loader_document,
    load_hunt_pack,
    save_hunt_pack,
    support_from_loaders,
    required_capabilities_for,
)
from modules.support_ui import read_loader, set_enabled, write_loader
from conf.conf_manager import ConfManager


_ACTIVE_CAVEBOT = None
AVAILABLE_MONSTERS = sorted(
    path.stem for path in Path(project_path(Path("images") / "Targets" / "Names")).glob("*.png")
)


class CaveBot:
    """Tk owns configuration only; the worker receives an immutable snapshot."""

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        global _ACTIVE_CAVEBOT
        if _ACTIVE_CAVEBOT is not None and not getattr(_ACTIVE_CAVEBOT, "_closed", True):
            _ACTIVE_CAVEBOT.window.lift()
            _ACTIVE_CAVEBOT.window.focus_force()
            return _ACTIVE_CAVEBOT
        instance = super().__new__(cls)
        _ACTIVE_CAVEBOT = instance
        return instance

    def __init__(self, MapPositions, BattlePositions, SQMs, MOUSE_OPTION):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.CaveBot = GUI("CaveBot", "Module: Cave Bot")
        self.CaveBot.DefaultWindow("CaveBot", [960, 680], [1.2, 2.29])
        self.window = self.CaveBot.windowID
        self.Setter = GUISetter("CaveBotLoader")
        self._client = get_client()
        self.map_positions = tuple(MapPositions)
        self.battle_positions = tuple(BattlePositions)
        self.sqms = tuple(SQMs)
        self.mouse_option = MOUSE_OPTION
        self._worker_message = "Idle"
        self._hotkey_down = False
        self._closed = False
        self._enabled = False
        self._paused = False
        self._route = Route("untitled", ())
        self._editor_index = 0
        self._editor = RouteEditor(self._route)
        self._imported_pack = None
        self._local_binding = None
        self._unapplied_import = False
        self._preview_image = None

        self._variables()
        self._build_ui()
        self._load_script()
        self._update_controls()
        self._poll()
        self.CaveBot.Protocol(self._close)
        self.CaveBot.loop()

    def _variables(self):
        variables = self.Setter.Variables
        self.debug, self.initial_debug = variables.Bool("Debugging")
        self.hotkey_pause, self.initial_hotkey_pause = variables.Bool("HotkeyPause")
        self.hotkey_name, self.initial_hotkey_name = variables.Str("HotkeyToPause")
        self.script_name, self.initial_script_name = variables.Str("Script")
        self.walk_enabled, self.initial_walk_enabled = variables.Bool("EnableWalking")
        self.follow, self.initial_follow = variables.Bool("Follow")
        self.stand, self.initial_stand = variables.Int("Stand")
        saved = read_loader("CaveBotLoader")
        self.initial_loot_enabled = bool(saved.get("Loot", True))
        self.loot_enabled = tk.BooleanVar(value=self.initial_loot_enabled)
        self.mark_name = tk.StringVar(value=NameMarks[0])

        self.attack_enabled = []
        self.monster_names = []
        self.priorities = []
        for number, suffix in enumerate(("One", "Two", "Three", "Four"), start=1):
            enabled, initial_enabled = variables.Bool("Attack" + suffix)
            monster, initial_monster = variables.Str(
                "SelectedMonster" + ("" if number == 1 else str(number))
            )
            if monster.get() not in AVAILABLE_MONSTERS and AVAILABLE_MONSTERS:
                monster.set(AVAILABLE_MONSTERS[0])
            priority, initial_priority = variables.Int("Priority" + suffix)
            self.attack_enabled.append((enabled, initial_enabled, "Attack" + suffix))
            self.monster_names.append(
                (monster, initial_monster, "SelectedMonster" + ("" if number == 1 else str(number)))
            )
            self.priorities.append((priority, initial_priority, "Priority" + suffix))

    def _build_ui(self):
        add_label = self.CaveBot.addLabel
        add_label("Targets (lower number attacks first)", [20, 12])
        for row in range(4):
            y = 42 + row * 38
            self.CaveBot.addCheck(
                self.attack_enabled[row][0],
                [20, y],
                self.attack_enabled[row][1],
                "Target {}".format(row + 1),
            )
            self.CaveBot.addOption(self.monster_names[row][0], AVAILABLE_MONSTERS, [135, y - 5], 16)
            self.CaveBot.addOption(self.priorities[row][0], Priority, [300, y - 5], 3)

        self.CaveBot.addCheck(
            self.follow, [20, 205], self.initial_follow, "Enable follow while fighting"
        )
        self.CaveBot.addCheck(
            self.loot_enabled, [20, 230], self.initial_loot_enabled,
            "Loot on stable target-count decrease (heuristic)"
        )
        add_label(
            "Attack stance, player safety, range and unreachable rules: unavailable until verified",
            [20, 265],
        )
        add_label("Depot, seller, banker and loot sorting: unavailable in CaveBot", [20, 290])

        self.CaveBot.addCheck(
            self.hotkey_pause, [20, 330], self.initial_hotkey_pause, "Enable pause hotkey"
        )
        self.CaveBot.addOption(self.hotkey_name, Hotkeys, [190, 325], 10)
        self.CaveBot.addCheck(
            self.walk_enabled, [20, 370], self.initial_walk_enabled, "Enable route walking"
        )
        add_label("Stand after arrival (seconds):", [20, 405])
        self.CaveBot.addEntry([210, 405], self.stand, 5)

        add_label("Route Studio", [430, 12])
        self.CaveBot.addEntry([430, 40], self.script_name, 20)
        self.CaveBot.addButton("Load", self._load_script, [75, 28], [590, 35])
        self.CaveBot.addButton("Save", self._save_script, [75, 28], [675, 35])
        self.CaveBot.addButton("Import pack", self._import_pack, [95, 28], [760, 35])
        self.CaveBot.addButton("Export pack", self._export_pack, [95, 28], [860, 35])
        add_label("Marker:", [430, 83])
        self.CaveBot.addOption(self.mark_name, NameMarks, [485, 76], 15)
        self.CaveBot.addButton("Edit marker", self._edit_marker, [95, 28], [650, 78])
        self.CaveBot.addButton("Bind local", self._bind_local, [95, 28], [750, 78])
        self.binding_label = add_label("Local profile: current route", [430, 115])

        self.route_table = ttk.Treeview(
            self.window, columns=("number", "marker", "action"), show="headings", height=11
        )
        self.route_table.heading("number", text="#")
        self.route_table.heading("marker", text="Marker template")
        self.route_table.heading("action", text="Action")
        self.route_table.column("number", width=42, anchor="center")
        self.route_table.column("marker", width=235)
        self.route_table.column("action", width=75, anchor="center")
        self.route_table.place(x=430, y=145, width=365, height=245)
        self.route_table.bind("<<TreeviewSelect>>", self._select_waypoint)
        self.preview_label = tk.Label(self.window, bg="#720000", fg="white", text="No marker")
        self.preview_label.place(x=815, y=175, width=105, height=105)
        add_label("Bundled asset preview", [805, 145])

        self.CaveBot.addButton("Add after", self._add_waypoint, [90, 28], [430, 405])
        self.CaveBot.addButton("Duplicate", self._duplicate_waypoint, [85, 28], [525, 405])
        self.CaveBot.addButton("Up", self._move_waypoint_up, [55, 28], [615, 405])
        self.CaveBot.addButton("Down", self._move_waypoint_down, [60, 28], [675, 405])
        self.CaveBot.addButton("Remove", self._remove_waypoint, [75, 28], [740, 405])
        self.CaveBot.addButton("Duplicate route", self._duplicate_route, [125, 28], [820, 405])
        add_label("Waypoint action: Walk. Rope and Shovel remain unavailable.", [430, 445])
        self.route_label = add_label("Empty route", [430, 475])
        self.route_detail = add_label("", [430, 500])

        self._config_controls = tuple(self.window.winfo_children())
        self.status_label = add_label("Idle", [20, 550])
        self.toggle_button = self.CaveBot.addButton(
            "CaveBot: OFF", self._toggle, [200, 40], [20, 590]
        )
        self.CaveBot.addButton("Close", self._close, [100, 32], [840, 620])

    def _set_message(self, message):
        self._worker_message = str(message)
        if not self._closed:
            self.status_label.configure(text=self._worker_message[:105])

    def _route_name(self):
        value = self.script_name.get().strip()
        if value.casefold().endswith(".json"):
            value = value[:-5]
        return value

    def _read_legacy_data(self, name):
        path = route_file(name)
        try:
            import json

            with path.open("r", encoding="utf-8") as stream:
                return json.load(stream)
        except Exception:
            return None

    def _load_script(self):
        try:
            name = self._route_name()
            if self._unapplied_import:
                self._restore_loader_preferences()
                self.script_name.set(name)
                self._unapplied_import = False
            if not route_file(name).exists():
                self._route = Route(name, ())
                self._editor_index = 0
                self._set_message("New empty route; add a waypoint and save")
            else:
                legacy_data = self._read_legacy_data(name)
                self._route = load_route(name)
                if checkpoint_file(name).exists():
                    self._editor_index = load_checkpoint(self._route)
                else:
                    self._editor_index = import_legacy_active_index(legacy_data, self._route)
                self._set_message("Loaded route '{}'".format(name))
            self._editor = RouteEditor(self._route, self._editor_index)
            self._imported_pack = None
            self._local_binding = None
            self.binding_label.configure(text="Local profile: current route")
        except Exception as exc:
            self._set_message("Route error: {}".format(exc))
        self._render_route()

    def _save_script(self):
        try:
            route = Route(self._route_name(), self._route.waypoints)
            save_route(route)
            self._route = route
            self._editor.route = route
            if route.waypoints:
                self._editor_index = min(self._editor_index, len(route.waypoints) - 1)
                save_checkpoint(route, self._editor_index)
            write_loader("CaveBotLoader", {"Script": route.name})
            self._set_message("Saved route '{}'".format(route.name))
        except Exception as exc:
            self._set_message("Save failed: {}".format(exc))
        self._render_route()

    def _add_waypoint(self):
        try:
            self._sync_editor_name()
            self._editor.add_after(self.mark_name.get(), WaypointAction.WALK)
            self._take_editor_state()
            self._set_message("Waypoint added; Save writes the route")
        except Exception as exc:
            self._set_message("Cannot add waypoint: {}".format(exc))
        self._render_route()

    def _remove_waypoint(self):
        try:
            if self._editor.selected is not None:
                self._sync_editor_name()
                self._editor.remove()
                self._take_editor_state()
                self._set_message("Waypoint removed; Save writes the route")
        except Exception as exc:
            self._set_message("Cannot remove waypoint: {}".format(exc))
        self._render_route()

    def _previous_waypoint(self):
        if self._route.waypoints:
            self._editor.select((self._editor.selected_index - 1) % len(self._route.waypoints))
            self._take_editor_state()
            self._render_route()

    def _next_waypoint(self):
        if self._route.waypoints:
            self._editor.select((self._editor.selected_index + 1) % len(self._route.waypoints))
            self._take_editor_state()
            self._render_route()

    def _sync_editor_name(self):
        self._editor.rename(self._route_name())

    def _take_editor_state(self):
        self._route = self._editor.route
        self._editor_index = self._editor.selected_index

    def _select_waypoint(self, event=None):
        del event
        selected = self.route_table.selection()
        if not selected:
            return
        try:
            self._editor.select(int(selected[0]))
            self._take_editor_state()
        except (ValueError, RouteError) as exc:
            self._set_message("Cannot select waypoint: {}".format(exc))
            return
        self._render_route(rebuild=False)

    def _edit_marker(self):
        try:
            self._sync_editor_name()
            self._editor.edit_marker(self.mark_name.get())
            self._take_editor_state()
            self._set_message("Marker changed; Save writes the route")
        except Exception as exc:
            self._set_message("Cannot edit marker: {}".format(exc))
        self._render_route()

    def _duplicate_waypoint(self):
        try:
            self._sync_editor_name()
            self._editor.duplicate_waypoint()
            self._take_editor_state()
            self._set_message("Waypoint duplicated with a new id; Save writes the route")
        except Exception as exc:
            self._set_message("Cannot duplicate waypoint: {}".format(exc))
        self._render_route()

    def _move_waypoint_up(self):
        self._move_waypoint(-1)

    def _move_waypoint_down(self):
        self._move_waypoint(1)

    def _move_waypoint(self, offset):
        try:
            self._sync_editor_name()
            before = self._editor.selected_index
            self._editor.move(offset)
            self._take_editor_state()
            if self._editor_index != before:
                self._set_message("Waypoint reordered; Save writes the route")
        except Exception as exc:
            self._set_message("Cannot reorder waypoint: {}".format(exc))
        self._render_route()

    def _duplicate_route(self):
        name = simpledialog.askstring("Duplicate route", "New route name:", parent=self.window)
        if not name:
            return
        try:
            route = self._editor.duplicate_route(name)
            self.script_name.set(route.name)
            self._route = route
            self._editor = RouteEditor(route, min(self._editor_index, max(0, len(route.waypoints) - 1)))
            self._take_editor_state()
            if self._imported_pack is None:
                self._local_binding = None
                self.binding_label.configure(text="Local profile: current route")
            self._set_message("Route duplicated with new waypoint ids; Save writes it")
        except Exception as exc:
            self._set_message("Cannot duplicate route: {}".format(exc))
        self._render_route()

    def _render_route(self, rebuild=True):
        if rebuild:
            for item in self.route_table.get_children():
                self.route_table.delete(item)
            for index, waypoint in enumerate(self._route.waypoints):
                self.route_table.insert(
                    "", "end", iid=str(index),
                    values=(index + 1, waypoint.mark, waypoint.action.value),
                )
        if not self._route.waypoints:
            self.route_label.configure(text="Empty route")
            self.route_detail.configure(text="Walk is disabled until a waypoint exists")
            self.preview_label.configure(image="", text="No marker")
            self._preview_image = None
            return
        self._editor_index = self._editor.selected_index
        waypoint = self._route.waypoints[self._editor_index]
        self.route_table.selection_set(str(self._editor_index))
        self.route_table.focus(str(self._editor_index))
        self.route_table.see(str(self._editor_index))
        self.mark_name.set(waypoint.mark)
        self.route_label.configure(
            text="Waypoint {} of {}".format(self._editor_index + 1, len(self._route.waypoints))
        )
        self.route_detail.configure(
            text="{} · {} · {}".format(waypoint.id, waypoint.mark, waypoint.action.value)
        )
        try:
            asset = Path(project_path(Path("images") / "MapSettings" / (waypoint.mark + ".png")))
            image = Image.open(asset).convert("RGBA")
            image.thumbnail((90, 90))
            self._preview_image = ImageTk.PhotoImage(image, master=self.window)
            self.preview_label.configure(image=self._preview_image, text="")
        except Exception as exc:
            self._preview_image = None
            self.preview_label.configure(image="", text="Preview error")
            self._set_message("Marker preview unavailable: {}".format(exc))

    def _pack_targets(self):
        targets = []
        for enabled, monster, priority in zip(
            self.attack_enabled, self.monster_names, self.priorities
        ):
            if enabled[0].get():
                targets.append(Target(monster[0].get(), int(priority[0].get())))
        return tuple(targets)

    def _current_pack(self):
        # Build a candidate without mutating editor or saved configuration so a
        # validation/export failure leaves the current UI settings untouched.
        route = Route(self._route_name(), self._route.waypoints)
        targets = self._pack_targets()
        support = (
            self._imported_pack.support
            if self._imported_pack is not None and self._local_binding is None
            else support_from_loaders(read_loader)
        )
        cavebot = CaveBotSettings(
            bool(self.walk_enabled.get()), bool(self.follow.get()),
            bool(self.loot_enabled.get()), max(0, int(self.stand.get())),
        )
        revision = self._imported_pack.revision if self._imported_pack is not None else 1
        required = set(required_capabilities_for(route, targets, support, cavebot))
        if self._imported_pack is not None:
            required.update(self._imported_pack.required_capabilities)
        return HuntPack(
            route.name,
            revision,
            route,
            targets,
            support,
            cavebot,
            SupplyPolicy(),
            tuple(sorted(required)),
        )

    def _export_pack(self):
        try:
            pack = self._current_pack()
            path = filedialog.asksaveasfilename(
                parent=self.window,
                title="Export hunt pack",
                defaultextension=".hunt.json",
                initialfile=pack.name + ".hunt.json",
                filetypes=(("Hunt pack", "*.hunt.json"), ("JSON", "*.json")),
            )
            if not path:
                return
            overwrite = False
            if Path(path).exists():
                overwrite = messagebox.askyesno(
                    "Replace hunt pack?",
                    "The selected file exists. Replace it with this validated hunt pack?",
                    parent=self.window,
                )
                if not overwrite:
                    self._set_message("Export cancelled; existing file was not changed")
                    return
            save_hunt_pack(pack, path, overwrite=overwrite)
            self._set_message("Exported hunt pack '{}'".format(pack.name))
        except Exception as exc:
            self._set_message("Export failed: {}".format(exc))

    def _support_features_idle(self):
        active = []
        for name in ("AutoHeal", "AutoMana", "AutoHur", "FoodEater", "TimedSpells"):
            try:
                if self._client.feature_status(name).active:
                    active.append(name)
            except Exception as exc:
                raise HuntPackError(
                    "cannot verify whether support features are stopped: {}".format(exc)
                ) from exc
        if active:
            raise HuntPackError(
                "stop active support feature(s) before applying settings: " + ", ".join(active)
            )

    def _apply_pack_configuration(self, pack):
        """Persist all imported loader settings in one atomic configuration update."""
        self._support_features_idle()
        target_values = {}
        words = ("One", "Two", "Three", "Four")
        for index, word in enumerate(words):
            suffix = "" if index == 0 else str(index + 1)
            if index < len(pack.targets):
                target = pack.targets[index]
                target_values.update({
                    "Attack" + word: True,
                    "SelectedMonster" + suffix: target.name,
                    "Priority" + word: target.priority,
                })
            else:
                target_values["Attack" + word] = False
        target_values.update({
            "EnableWalking": pack.cavebot.walking,
            "Follow": pack.cavebot.follow,
            "Loot": pack.cavebot.loot,
            "Stand": pack.cavebot.stand_seconds,
        })

        def update(document):
            apply_support_to_loader_document(document, pack.support)
            cavebot = document.get("Loaders", {}).get("CaveBotLoader")
            if not isinstance(cavebot, dict):
                raise HuntPackError("configuration is missing CaveBotLoader")
            cavebot.update(target_values)
            return document

        ConfManager.update("GUILoader.json", update)

    def _import_pack(self):
        path = filedialog.askopenfilename(
            parent=self.window,
            title="Import hunt pack",
            filetypes=(("Hunt pack", "*.hunt.json"), ("JSON", "*.json"), ("All files", "*")),
        )
        if not path:
            return
        try:
            pack = load_hunt_pack(path)
            self.script_name.set(pack.route.name)
            self.walk_enabled.set(pack.cavebot.walking)
            self.follow.set(pack.cavebot.follow)
            self.loot_enabled.set(pack.cavebot.loot)
            self.stand.set(pack.cavebot.stand_seconds)
            for index, (enabled, monster, priority) in enumerate(zip(
                self.attack_enabled, self.monster_names, self.priorities
            )):
                if index < len(pack.targets):
                    target = pack.targets[index]
                    enabled[0].set(True)
                    monster[0].set(target.name)
                    priority[0].set(target.priority)
                else:
                    enabled[0].set(False)
            self._route = pack.route
            self._editor = RouteEditor(pack.route)
            self._take_editor_state()
            self._imported_pack = pack
            self._local_binding = None
            self._unapplied_import = True
            self.binding_label.configure(text="Local profile: binding required")
            self._set_message(
                "Imported in memory; Bind local to validate and apply settings, then Save route."
            )
        except Exception as exc:
            self._set_message("Import failed: {}".format(exc))
        self._render_route()

    def _compatibility_context(self):
        try:
            from tibia_client.application import compatibility_status
            context = compatibility_status()
        except Exception as exc:
            raise HuntPackError("local compatibility status is unavailable: {}".format(exc)) from exc
        if not isinstance(context, Mapping):
            raise HuntPackError("local compatibility status is unavailable")
        required = {"profile_id", "calibration_id", "ready", "capabilities", "problems"}
        if not required.issubset(context):
            raise HuntPackError("local compatibility status is incomplete")
        if context["ready"] is not True:
            problems = context.get("problems")
            detail = "; ".join(str(item) for item in problems) if problems else "profile is not ready"
            raise HuntPackError("local compatibility checks failed: " + detail)
        capabilities = context.get("capabilities")
        if not isinstance(capabilities, (tuple, list, set, frozenset)):
            raise HuntPackError("local capability declaration is invalid")
        return context

    def _bind_local(self):
        if self._imported_pack is None:
            self._set_message("Local binding is needed after importing a hunt pack")
            return
        try:
            context = self._compatibility_context()
            binding = LocalBinding(context["profile_id"], context["calibration_id"])
            staged = self._current_pack()
            staged.validate_for_activation(binding, set(context["capabilities"]))
            self._apply_pack_configuration(staged)
            self._imported_pack = staged
            self._local_binding = binding
            self._unapplied_import = False
            self.binding_label.configure(text="Local profile: {}".format(binding.profile))
            self._set_message(
                "Bound and applied; close and reopen support windows before their next start"
            )
        except Exception as exc:
            self._local_binding = None
            self.binding_label.configure(text="Local profile: binding required")
            self._set_message("Cannot bind hunt pack: {}".format(exc))

    def _validate_pack_activation(self):
        if self._imported_pack is None:
            return
        context = self._compatibility_context()
        current = LocalBinding(context["profile_id"], context["calibration_id"])
        if self._local_binding != current:
            self._local_binding = None
            self.binding_label.configure(text="Local profile: binding required")
            raise HuntPackError("local profile or calibration changed; bind the hunt pack again")
        self._imported_pack.validate_for_activation(current, set(context["capabilities"]))

    def _snapshot(self):
        self._validate_pack_activation()
        route_name = self._route_name()
        candidate = Route(route_name, self._route.waypoints)
        if not route_file(route_name).is_file():
            raise RouteError("route has unsaved changes; press Save before starting CaveBot")
        route = load_route(route_name)
        if route != candidate:
            raise RouteError("route has unsaved changes; press Save before starting CaveBot")
        if not route.waypoints:
            raise RouteError("route has no waypoints")
        if not self.walk_enabled.get():
            raise RouteError("route walking must be enabled")
        targets = []
        for enabled, monster, priority in zip(
            self.attack_enabled, self.monster_names, self.priorities
        ):
            if enabled[0].get():
                name = monster[0].get()
                asset = Path(project_path(Path("images") / "Targets" / "Names" / (name + ".png")))
                if not asset.is_file():
                    raise RouteError("missing target image: {}".format(name))
                targets.append(TargetSpec(int(priority[0].get()), name))
        if checkpoint_file(route_name).exists():
            start_index = load_checkpoint(route)
        else:
            start_index = import_legacy_active_index(self._read_legacy_data(route_name), route)
        return {
            "route": route,
            "start_index": start_index,
            "targets": tuple(targets),
            "follow": bool(self.follow.get()),
            "loot": bool(self.loot_enabled.get()),
            "stand": max(0.0, float(self.stand.get())),
        }

    def _toggle(self):
        if self._enabled:
            self._client.stop_feature("CaveBot")
            self._enabled = False
            self._paused = False
            self._set_message("Stopped")
            self._update_controls()
            return
        try:
            snapshot = self._snapshot()
            points = tuple(zip(self.sqms[::2], self.sqms[1::2]))
            feature = CaveBotFeature(
                snapshot["route"],
                snapshot["targets"],
                config=SessionConfig(
                    stand_seconds=snapshot["stand"],
                    follow=snapshot["follow"],
                    loot=snapshot["loot"],
                ),
                start_index=snapshot["start_index"],
                map_region=self.map_positions,
                battle_region=self.battle_positions,
                loot_points=points,
            )
            self._client.start_feature(feature)
        except Exception as exc:
            self._set_message("Cannot start: {}".format(exc))
            return
        self._enabled = True
        self._paused = False
        self._set_message("Armed — focus game")
        self._update_controls()

    def _update_controls(self):
        if self._closed:
            return
        set_enabled(self._config_controls, not self._enabled)
        if self._enabled:
            self.toggle_button.configure(text="CaveBot: ON", relief=tk.SUNKEN)
        else:
            self.toggle_button.configure(text="CaveBot: OFF", relief=tk.RAISED)

    def _poll_hotkey(self):
        pressed = False
        if self.hotkey_pause.get():
            try:
                pressed = poll_hotkey(self.hotkey_name.get())
            except Exception as exc:
                self._set_message("Pause hotkey unavailable: {}".format(exc))
        if pressed and not self._hotkey_down:
            self._toggle_pause()
        self._hotkey_down = pressed

    def _toggle_pause(self):
        if not self._enabled:
            self._set_message("Start CaveBot before using the pause hotkey")
            return
        try:
            if self._paused:
                changed = self._client.resume_navigation()
                if changed is True:
                    self._paused = False
                    self._set_message("Navigation resumed")
                else:
                    self._set_message("Navigation resume was not applied; check session status")
            else:
                changed = self._client.pause_navigation()
                if changed is True:
                    self._paused = True
                    self._set_message("Navigation paused")
                else:
                    self._set_message("Navigation pause was not applied; check session status")
        except Exception as exc:
            self._set_message("Cannot change navigation pause: {}".format(exc))

    def _poll(self):
        if self._closed:
            return
        self._poll_hotkey()
        try:
            status = self._client.feature_status("CaveBot")
            if status.error:
                self._enabled = False
                self._paused = False
                self._set_message("CaveBot stopped: {}".format(status.error))
                self._update_controls()
            elif status.active:
                self._enabled = True
                self._paused = False
                self.status_label.configure(text=status.phase or "Running")
            elif status.phase == "Paused":
                self._enabled = True
                self._paused = True
                self.status_label.configure(text="Paused")
                self._update_controls()
            elif self._enabled:
                self._enabled = False
                self._paused = False
                self._set_message(status.phase or "Stopped")
                self._update_controls()
        except Exception as exc:
            if self._enabled:
                self._enabled = False
                self._paused = False
                self._set_message("CaveBot status unavailable: {}".format(exc))
                self._update_controls()
        self.window.after(100, self._poll)

    def _save_preferences(self):
        if self._unapplied_import:
            return
        values = [
            ("Debugging", self.debug.get()),
            ("HotkeyPause", self.hotkey_pause.get()),
            ("HotkeyToPause", self.hotkey_name.get()),
            ("EnableWalking", self.walk_enabled.get()),
            ("Follow", self.follow.get()),
            ("Loot", self.loot_enabled.get()),
            ("Stand", self.stand.get()),
        ]
        if self._route_is_saved():
            values.append(("Script", self._route_name()))
        values.extend((key, variable.get()) for variable, _, key in self.attack_enabled)
        values.extend((key, variable.get()) for variable, _, key in self.monster_names)
        values.extend((key, variable.get()) for variable, _, key in self.priorities)
        write_loader("CaveBotLoader", dict(values))

    def _route_is_saved(self):
        try:
            candidate = Route(self._route_name(), self._route.waypoints)
            return load_route(candidate.name) == candidate
        except Exception:
            return False

    def _restore_loader_preferences(self):
        """Discard an unbound staged pack without persisting any of its values."""
        saved = read_loader("CaveBotLoader")
        self.walk_enabled.set(bool(saved.get("EnableWalking", True)))
        self.follow.set(bool(saved.get("Follow", True)))
        self.loot_enabled.set(bool(saved.get("Loot", True)))
        self.stand.set(int(saved.get("Stand", 0)))
        words = ("One", "Two", "Three", "Four")
        for index, word in enumerate(words):
            suffix = "" if index == 0 else str(index + 1)
            self.attack_enabled[index][0].set(bool(saved.get("Attack" + word, False)))
            monster = saved.get("SelectedMonster" + suffix)
            if monster in AVAILABLE_MONSTERS:
                self.monster_names[index][0].set(monster)
            self.priorities[index][0].set(int(saved.get("Priority" + word, index + 1)))

    def _close(self):
        global _ACTIVE_CAVEBOT
        if self._closed:
            return
        self._closed = True
        self._enabled = False
        self._paused = False
        try:
            self._client.stop_feature("CaveBot")
        finally:
            try:
                self._save_preferences()
            except Exception:
                pass
            finally:
                self.CaveBot.destroyWindow()
                _ACTIVE_CAVEBOT = None
