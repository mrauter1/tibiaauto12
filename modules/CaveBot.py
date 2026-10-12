"""Cavebot configuration UI backed by one cancellable headless session."""

from __future__ import annotations

import uuid
from pathlib import Path
import tkinter as tk

from conf.Constants import Hotkeys, Priority
from conf.MarksConf import NameMarks
from core.GUI import GUI
from core.GUISetter import GUISetter
from core.ThreadManager import ThreadManager
from core.paths import project_path
from engine.CaveBot.CaveBotController import CaveBotController
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
from engine.CaveBot.session import SessionConfig, TargetSpec


EnabledCaveBot = False
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
        self.CaveBot.DefaultWindow("CaveBot", [830, 634], [1.2, 2.29])
        self.window = self.CaveBot.windowID
        self.Setter = GUISetter("CaveBotLoader")
        self.ThreadManager = ThreadManager("ThreadCaveBot")
        self.map_positions = tuple(MapPositions)
        self.battle_positions = tuple(BattlePositions)
        self.sqms = tuple(SQMs)
        self.mouse_option = MOUSE_OPTION
        self._controller = None
        self._worker_done = True
        self._worker_message = "Idle"
        self._hotkey_down = False
        self._closed = False
        self._route = Route("untitled", ())
        self._editor_index = 0

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
        self.loot_enabled = tk.BooleanVar(value=True)
        self.mark_name = tk.StringVar(value=NameMarks[0])

        self.attack_enabled = []
        self.monster_names = []
        self.priorities = []
        for number, suffix in enumerate(("One", "Two", "Three", "Four"), start=1):
            enabled, initial_enabled = variables.Bool("Attack" + suffix)
            monster, initial_monster = variables.Str("SelectedMonster" + ("" if number == 1 else str(number)))
            if monster.get() not in AVAILABLE_MONSTERS and AVAILABLE_MONSTERS:
                monster.set(AVAILABLE_MONSTERS[0])
            priority, initial_priority = variables.Int("Priority" + suffix)
            self.attack_enabled.append((enabled, initial_enabled, "Attack" + suffix))
            self.monster_names.append((monster, initial_monster, "SelectedMonster" + ("" if number == 1 else str(number))))
            self.priorities.append((priority, initial_priority, "Priority" + suffix))

    def _build_ui(self):
        add_label = self.CaveBot.addLabel
        add_label("Targets (lower number attacks first)", [20, 12])
        for row in range(4):
            y = 42 + row * 38
            self.CaveBot.addCheck(self.attack_enabled[row][0], [20, y], self.attack_enabled[row][1],
                                  "Target {}".format(row + 1))
            self.CaveBot.addOption(self.monster_names[row][0], AVAILABLE_MONSTERS, [135, y - 5], 16)
            self.CaveBot.addOption(self.priorities[row][0], Priority, [300, y - 5], 3)

        self.CaveBot.addCheck(self.follow, [20, 205], self.initial_follow, "Enable follow while fighting")
        self.CaveBot.addCheck(self.loot_enabled, [20, 230], True,
                              "Loot on stable target-count decrease (heuristic)")
        add_label("Attack stance, player safety, range and unreachable rules: unavailable until verified", [20, 265])
        add_label("Depot, seller, banker and loot sorting: unavailable in CaveBot", [20, 290])

        self.CaveBot.addCheck(self.hotkey_pause, [20, 330], self.initial_hotkey_pause,
                              "Enable pause hotkey")
        self.CaveBot.addOption(self.hotkey_name, Hotkeys, [190, 325], 10)
        self.CaveBot.addCheck(self.walk_enabled, [20, 370], self.initial_walk_enabled, "Enable route walking")
        add_label("Stand after arrival (seconds):", [20, 405])
        self.CaveBot.addEntry([210, 405], self.stand, 5)

        add_label("Route", [430, 12])
        self.CaveBot.addEntry([430, 40], self.script_name, 20)
        self.CaveBot.addButton("Load", self._load_script, [75, 28], [590, 35])
        self.CaveBot.addButton("Save", self._save_script, [75, 28], [675, 35])
        add_label("Marker:", [430, 85])
        self.CaveBot.addOption(self.mark_name, NameMarks, [485, 80], 15)
        self.CaveBot.addButton("Add after", self._add_waypoint, [90, 28], [430, 120])
        self.CaveBot.addButton("Remove", self._remove_waypoint, [80, 28], [530, 120])
        self.CaveBot.addButton("Previous", self._previous_waypoint, [80, 28], [620, 120])
        self.CaveBot.addButton("Next", self._next_waypoint, [70, 28], [710, 120])
        add_label("Waypoint action: Walk", [430, 165])
        add_label("Rope and Shovel are rejected until their outcome can be verified", [430, 190])
        self.route_label = add_label("Empty route", [430, 225])
        self.route_detail = add_label("", [430, 250])

        self.status_label = add_label("Idle", [20, 490])
        self.toggle_button = self.CaveBot.addButton("CaveBot: OFF", self._toggle, [200, 40], [20, 535])
        self.CaveBot.addButton("Close", self._close, [100, 32], [700, 575])

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
        except Exception as exc:
            self._set_message("Route error: {}".format(exc))
        self._render_route()

    def _save_script(self):
        try:
            route = Route(self._route_name(), self._route.waypoints)
            save_route(route)
            self._route = route
            if route.waypoints:
                self._editor_index = min(self._editor_index, len(route.waypoints) - 1)
                save_checkpoint(route, self._editor_index)
            self._set_message("Saved route '{}'".format(route.name))
        except Exception as exc:
            self._set_message("Save failed: {}".format(exc))
        self._render_route()

    def _add_waypoint(self):
        try:
            waypoint = Waypoint("waypoint-" + uuid.uuid4().hex[:12], self.mark_name.get(), WaypointAction.WALK)
            entries = list(self._route.waypoints)
            insert_at = self._editor_index + 1 if entries else 0
            entries.insert(insert_at, waypoint)
            self._route = Route(self._route_name(), tuple(entries))
            self._editor_index = insert_at
            self._set_message("Waypoint added; Save writes the route")
        except Exception as exc:
            self._set_message("Cannot add waypoint: {}".format(exc))
        self._render_route()

    def _remove_waypoint(self):
        if self._route.waypoints:
            entries = list(self._route.waypoints)
            entries.pop(self._editor_index)
            self._route = Route(self._route_name(), tuple(entries))
            self._editor_index = min(self._editor_index, max(0, len(entries) - 1))
            self._set_message("Waypoint removed; Save writes the route")
        self._render_route()

    def _previous_waypoint(self):
        if self._route.waypoints:
            self._editor_index = (self._editor_index - 1) % len(self._route.waypoints)
            self._render_route()

    def _next_waypoint(self):
        if self._route.waypoints:
            self._editor_index = (self._editor_index + 1) % len(self._route.waypoints)
            self._render_route()

    def _render_route(self):
        if not self._route.waypoints:
            self.route_label.configure(text="Empty route")
            self.route_detail.configure(text="Walk is disabled until a waypoint exists")
            return
        waypoint = self._route.waypoints[self._editor_index]
        self.route_label.configure(text="Waypoint {} of {}".format(self._editor_index + 1, len(self._route.waypoints)))
        self.route_detail.configure(text="{} · {} · {}".format(waypoint.id, waypoint.mark, waypoint.action.value))

    def _snapshot(self):
        route_name = self._route_name()
        route = load_route(route_name)
        if not route.waypoints:
            raise RouteError("route has no waypoints")
        if not self.walk_enabled.get():
            raise RouteError("route walking must be enabled")
        targets = []
        for enabled, monster, priority in zip(self.attack_enabled, self.monster_names, self.priorities):
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
        global EnabledCaveBot
        if EnabledCaveBot:
            EnabledCaveBot = False
            self.ThreadManager.PauseThread()
            self._set_message("Stopping CaveBot…")
            self._update_controls()
            return
        if not self._worker_done:
            self._set_message("The previous CaveBot session is still stopping")
            return
        try:
            snapshot = self._snapshot()
        except Exception as exc:
            self._set_message("Cannot start: {}".format(exc))
            return

        self._worker_done = False
        self._worker_message = "Starting"
        EnabledCaveBot = True

        def worker():
            try:
                from core.runtime import current_token
                token = current_token()
                controller = CaveBotController(
                    self.mouse_option,
                    snapshot["route"].name,
                    "right",
                    snapshot["stand"],
                    True,
                    snapshot["loot"],
                    False,
                    self.map_positions,
                    self.battle_positions,
                    self.sqms,
                )
                controller.follow_enabled = snapshot["follow"]
                self._controller = controller
                controller.actions.hotkey.wait_until_ready(timeout=30)
                config = SessionConfig(
                    stand_seconds=snapshot["stand"],
                    follow=snapshot["follow"],
                    loot=snapshot["loot"],
                )
                controller.create_session(
                    snapshot["route"],
                    snapshot["targets"],
                    token,
                    config=config,
                    start_index=snapshot["start_index"],
                ).run()
                self._worker_message = controller.session.status.reason
            except BaseException as exc:
                self._worker_message = "CaveBot stopped: {}".format(exc)
            finally:
                self._worker_done = True

        self.ThreadManager.NewThread(worker)
        self._update_controls()

    def _update_controls(self):
        if self._closed:
            return
        if EnabledCaveBot:
            self.toggle_button.configure(text="CaveBot: ON", relief=tk.SUNKEN)
        else:
            self.toggle_button.configure(text="CaveBot: OFF", relief=tk.RAISED)

    def _poll_hotkey(self):
        pressed = False
        if self.hotkey_pause.get():
            try:
                import keyboard
                pressed = keyboard.is_pressed(self.hotkey_name.get().lower())
            except Exception:
                pressed = False
        if pressed and not self._hotkey_down:
            self._toggle()
        self._hotkey_down = pressed

    def _poll(self):
        global EnabledCaveBot
        if self._closed:
            return
        self._poll_hotkey()
        if self._controller is not None and self._controller.session is not None and not self._worker_done:
            status = self._controller.session.status
            self.status_label.configure(text="{}: {} (waypoint {})".format(
                status.state.value, status.reason, status.waypoint_index + 1
            ))
        elif not self._worker_done:
            worker_status = self.ThreadManager.Status() or {}
            self.status_label.configure(text=worker_status.get("phase") or "Starting CaveBot")
        elif self._worker_done and EnabledCaveBot:
            EnabledCaveBot = False
            self._set_message(self._worker_message)
            self._update_controls()
        self.window.after(100, self._poll)

    def _save_preferences(self):
        values = [
            ("Debugging", self.debug.get()),
            ("HotkeyPause", self.hotkey_pause.get()),
            ("HotkeyToPause", self.hotkey_name.get()),
            ("Script", self.script_name.get()),
            ("EnableWalking", self.walk_enabled.get()),
            ("Follow", self.follow.get()),
            ("Stand", self.stand.get()),
        ]
        values.extend((key, variable.get()) for variable, _, key in self.attack_enabled)
        values.extend((key, variable.get()) for variable, _, key in self.monster_names)
        values.extend((key, variable.get()) for variable, _, key in self.priorities)
        for key, value in values:
            try:
                self.Setter.SetVariables.SetVar(key, value)
            except Exception:
                pass

    def _close(self):
        global EnabledCaveBot, _ACTIVE_CAVEBOT
        if self._closed:
            return
        self._closed = True
        EnabledCaveBot = False
        self.ThreadManager.PauseThread()
        try:
            self._save_preferences()
        except Exception:
            pass
        finally:
            self.CaveBot.destroyWindow()
            _ACTIVE_CAVEBOT = None
