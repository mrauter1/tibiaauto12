"""Client adapters for the headless :mod:`engine.CaveBot.session` engine."""

from __future__ import annotations

import time
from typing import Any, Iterable, Optional

from conf.Hotkeys import Hotkey
from core.HookWindow import LocateCenterImage, LocateMatch
from core.perception import frame_scope
from core import runtime
from engine.CaveBot import Scanners
from engine.CaveBot.routes import (
    Route,
    RouteError,
    checkpoint_file,
    import_legacy_active_index,
    load_checkpoint,
    route_from_data,
    save_checkpoint,
)
from engine.CaveBot.session import CaveBotSession, SessionConfig, TargetSpec

class LegacyPerception:
    """Translate existing image scanners to the session perception contract."""

    def __init__(self, map_position, battle_position):
        self.map_position = tuple(map_position)
        self.battle_position = tuple(battle_position)

    def frame_scope(self):
        return frame_scope()

    def find_marker(self, waypoint, frame):
        match = LocateMatch(
            "images/MapSettings/" + waypoint.mark + ".png",
            Region=self.map_position,
            Precision=0.8,
            frame=frame,
        )
        if match is None:
            return None
        center_x, center_y = match.center
        return self.map_position[0] + center_x, self.map_position[1] + center_y

    def waypoint_reached(self, waypoint, frame):
        return bool(Scanners.CheckWaypoint(waypoint.mark, self.map_position, frame=frame))

    def target_count(self, name, frame):
        return Scanners.NumberOfTargets(self.battle_position, name, frame=frame)

    def target_position(self, name, frame):
        match = Scanners.locate_target(self.battle_position, name, frame=frame)
        return None if match is None else match.center

    def target_selected(self, name, frame):
        selected = getattr(Scanners, "target_selected", None)
        if selected is not None:
            return bool(selected(self.battle_position, frame=frame, target=name))
        return not bool(Scanners.IsAttacking(self.battle_position, frame=frame))

    def need_follow(self, frame):
        return bool(Scanners.NeedFollow(frame=frame))


class ClientActions:
    """Serialized, cancellation-gated client input used by a cavebot session."""

    def __init__(self, mouse_option, loot_button, sqms, *, loot_budget=9, loot_max_seconds=2.0):
        self.mouse_option = mouse_option
        self.hotkey = Hotkey(mouse_option)
        self.loot_button = loot_button
        self.sqms = tuple(sqms)
        self.loot_budget = max(0, min(int(loot_budget), len(self.sqms) // 2))
        self.loot_max_seconds = max(0.1, float(loot_max_seconds))

    @staticmethod
    def _transaction(reason):
        return runtime.input_transaction(reason=reason)

    def _click(self, method, point, token, reason):
        token.checkpoint()
        with self._transaction(reason):
            token.checkpoint()
            previous = self.hotkey.Position() if self.mouse_option == 1 else None
            method(point[0], point[1])
            token.checkpoint()
            if previous is not None:
                self._restore_cursor(previous, token)

    def _restore_cursor(self, previous, token):
        cancelled = getattr(token, "cancelled", False)
        cancelled = cancelled() if callable(cancelled) else cancelled
        if cancelled:
            return
        try:
            width, height = self.hotkey.MainWindowSize()
            if 0 <= previous[0] < width and 0 <= previous[1] < height:
                self.hotkey.MoveTo(previous[0], previous[1])
        except runtime.CancelledError:
            raise
        except Exception:
            # Cursor restoration is best effort and must not turn a successful
            # game action into a failed session after focus/window changes.
            return

    def click_map(self, point, token):
        self._click(self.hotkey.LeftClick, point, token, "cavebot-map-click")

    def click_target(self, point, token):
        self._click(self.hotkey.LeftClick, point, token, "cavebot-target-click")

    def enable_follow(self, token):
        token.checkpoint()
        point = LocateCenterImage("images/TibiaSettings/NotFollow.png", Precision=0.7)
        if point == (0, 0):
            return
        self._click(self.hotkey.LeftClick, point, token, "cavebot-follow-click")

    def loot(self, token):
        deadline = time.monotonic() + self.loot_max_seconds
        with self._transaction("cavebot-loot"):
            previous = self.hotkey.Position() if self.mouse_option == 1 else None
            try:
                for index in range(self.loot_budget):
                    token.checkpoint()
                    if time.monotonic() >= deadline:
                        break
                    x, y = self.sqms[index * 2:index * 2 + 2]
                    if self.loot_button == "right":
                        self.hotkey.RightClick(x, y)
                    elif self.loot_button == "left":
                        self.hotkey.LeftClick(x, y)
                    else:
                        raise ValueError("loot button must be 'left' or 'right'")
            finally:
                if previous is not None:
                    self._restore_cursor(previous, token)


class CaveBotController:
    """Construct and run one cavebot session; positional arguments remain compatible."""

    def __init__(self, MOUSE_OPTION, ScriptName, LootButton, TimeToStand, Walk, Loot,
                 ForRefresh, MapPosition, BattlePosition, SQMs):
        self.mouse_option = MOUSE_OPTION
        self.script_name = ScriptName
        self.map_position = tuple(MapPosition)
        self.battle_position = tuple(BattlePosition)
        self.walk_enabled = bool(Walk)
        self.loot_enabled = bool(Loot)
        self.follow_enabled = True
        self.stand_seconds = float(TimeToStand)
        self.perception = LegacyPerception(MapPosition, BattlePosition)
        self.actions = ClientActions(MOUSE_OPTION, LootButton, SQMs)
        self.session: Optional[CaveBotSession] = None

    def create_session(self, route: Route, targets: Iterable[TargetSpec], token: Any, *,
                       config: Optional[SessionConfig] = None, start_index: Optional[int] = None,
                       perception=None, actions=None, clock=None) -> CaveBotSession:
        if not self.walk_enabled:
            raise RouteError("attack-only mode is not supported by the route session")
        if start_index is None:
            start_index = load_checkpoint(route)
        config = config or SessionConfig(
            stand_seconds=self.stand_seconds,
            follow=self.follow_enabled,
            loot=self.loot_enabled,
        )
        self.session = CaveBotSession(
            route,
            perception or self.perception,
            actions or self.actions,
            token,
            targets=targets,
            config=config,
            start_index=start_index,
            save_progress=save_checkpoint,
            clock=clock,
        )
        return self.session

    def StartCaveBot(self, data, MonstersToAttack, token=None, *, priorities=None,
                     max_completed_waypoints=None):
        """Compatibility entry point; unlike the old engine it never mutates ``data``."""
        route = route_from_data(self.script_name, data)
        legacy_index = import_legacy_active_index(data, route)
        start_index = load_checkpoint(route) if checkpoint_file(route.name).exists() else legacy_index
        if token is None:
            token = runtime.current_token()
        if token is None:
            raise RuntimeError("CaveBot must run in a managed worker")
        priorities = priorities or range(1, len(MonstersToAttack) + 1)
        targets = [TargetSpec(int(priority), name) for priority, name in zip(priorities, MonstersToAttack)]
        return self.create_session(route, targets, token, start_index=start_index).run(
            max_completed_waypoints=max_completed_waypoints
        )

    def TakeLoot(self, token=None):
        """Compatibility helper which now honors the configured loot setting."""
        if not self.loot_enabled:
            return
        if token is None:
            token = runtime.current_token()
        if token is None:
            raise RuntimeError("loot must run in a managed worker")
        self.actions.loot(token)
