from contextlib import contextmanager

import pytest

from engine.CaveBot.CaveBotController import ClientActions
from engine.CaveBot.routes import Route, Waypoint
from engine.CaveBot.session import (
    CaveBotSession,
    SessionConfig,
    SessionState,
    TargetSpec,
)


class Cancelled(Exception):
    pass


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeToken:
    def __init__(self, clock):
        self.clock = clock
        self.cancelled = False
        self.checkpoints = 0

    def checkpoint(self):
        self.checkpoints += 1
        if self.cancelled:
            raise Cancelled()

    def wait(self, seconds):
        self.checkpoint()
        self.clock.sleep(seconds)
        self.checkpoint()


class FakePerception:
    def __init__(self, *, marker=(10, 20), reached=False, counts=None):
        self.marker = marker
        self.reached = reached
        self.counts = dict(counts or {})
        self.find_calls = 0
        self.cancel_on_find = None
        self.token = None

    @contextmanager
    def frame_scope(self):
        yield self

    def find_marker(self, waypoint, frame):
        self.find_calls += 1
        if self.cancel_on_find == self.find_calls:
            self.token.cancelled = True
        return self.marker

    def waypoint_reached(self, waypoint, frame):
        return self.reached or (self.counts and not any(self.counts.values()))

    def target_count(self, name, frame):
        return self.counts.get(name, 0)

    def target_position(self, name, frame):
        return {"urgent": (1, 1), "ordinary": (2, 2)}.get(name, (3, 3))

    def target_selected(self, name, frame):
        return False

    def need_follow(self, frame):
        return True


class FakeActions:
    def __init__(self, perception=None):
        self.perception = perception
        self.map_clicks = []
        self.target_clicks = []
        self.follow_clicks = 0
        self.loot_calls = 0

    def click_map(self, point, token):
        token.checkpoint()
        self.map_clicks.append(point)

    def click_target(self, point, token):
        token.checkpoint()
        self.target_clicks.append(point)
        if self.perception:
            for name, target_point in (("urgent", (1, 1)), ("ordinary", (2, 2))):
                if point == target_point:
                    self.perception.counts[name] = 0

    def enable_follow(self, token):
        token.checkpoint()
        self.follow_clicks += 1

    def loot(self, token):
        token.checkpoint()
        self.loot_calls += 1


def make_session(perception, actions=None, *, config=None, targets=()):
    clock = FakeClock()
    token = FakeToken(clock)
    perception.token = token
    actions = actions or FakeActions(perception)
    route = Route("test", (Waypoint("same", "CheckMark"),))
    session = CaveBotSession(
        route,
        perception,
        actions,
        token,
        targets=targets,
        config=config or SessionConfig(poll_interval=0.1, marker_timeout=0.3,
                                       arrival_timeout=0.3, combat_timeout=1.0),
        save_progress=lambda route, index: None,
        clock=clock,
    )
    return session, token, actions


def test_missing_marker_is_bounded_without_refresh_movement():
    session, token, actions = make_session(FakePerception(marker=None))
    status = session.run(max_completed_waypoints=1)
    assert status.state is SessionState.FAILED
    assert status.reason == "marker_timeout"
    assert actions.map_clicks == []
    assert token.checkpoints > 1


def test_no_arrival_retries_iteratively_then_stops():
    config = SessionConfig(poll_interval=0.1, marker_timeout=0.2,
                           arrival_timeout=0.25, combat_timeout=1.0, movement_attempts=2)
    session, _, actions = make_session(FakePerception(reached=False), config=config)
    status = session.run(max_completed_waypoints=1)
    assert status.reason == "arrival_timeout"
    assert len(actions.map_clicks) == 2


def test_cancellation_during_marker_search_performs_no_input():
    perception = FakePerception(marker=None)
    perception.cancel_on_find = 1
    session, _, actions = make_session(perception)
    status = session.run(max_completed_waypoints=1)
    assert status.state is SessionState.STOPPED
    assert status.reason == "cancelled"
    assert actions.map_clicks == []


def test_target_interrupts_walk_and_real_priority_wins():
    perception = FakePerception(counts={"ordinary": 1, "urgent": 1})
    actions = FakeActions(perception)
    targets = (TargetSpec(5, "ordinary"), TargetSpec(1, "urgent"))
    session, _, actions = make_session(perception, actions, targets=targets)
    status = session.run(max_completed_waypoints=1)
    assert status.reason == "completion_limit"
    assert actions.target_clicks[:2] == [(1, 1), (2, 2)]
    assert len(actions.map_clicks) == 2  # combat interrupted the first client walk
    assert actions.follow_clicks >= 1


def test_resume_starts_at_requested_same_waypoint():
    route = Route("test", (Waypoint("first", "CheckMark"), Waypoint("same", "CheckMark")))
    clock = FakeClock()
    token = FakeToken(clock)
    perception = FakePerception(reached=True)
    actions = FakeActions(perception)
    saved = []
    session = CaveBotSession(route, perception, actions, token, start_index=1,
                             save_progress=lambda route, index: saved.append(index), clock=clock)
    session.run(max_completed_waypoints=1)
    assert session.status.waypoint_id == "first"
    assert saved == [0]


def test_loot_setting_is_honored():
    perception = FakePerception(counts={"urgent": 1})
    actions = FakeActions(perception)
    config = SessionConfig(poll_interval=0.1, marker_timeout=0.2, arrival_timeout=0.2,
                           combat_timeout=1.0, loot=False)
    session, _, actions = make_session(perception, actions, config=config,
                                       targets=(TargetSpec(1, "urgent"),))
    session.run(max_completed_waypoints=1)
    assert actions.loot_calls == 0


def test_loot_sequence_stops_clicking_immediately_on_cancellation():
    clock = FakeClock()
    token = FakeToken(clock)

    class CancellingHotkey:
        def __init__(self):
            self.clicks = []

        def RightClick(self, x, y):
            self.clicks.append((x, y))
            token.cancelled = True

    actions = object.__new__(ClientActions)
    actions.mouse_option = 0
    actions.hotkey = CancellingHotkey()
    actions.loot_button = "right"
    actions.sqms = tuple(range(18))
    actions.loot_budget = 9
    actions.loot_max_seconds = 2.0

    with pytest.raises(Cancelled):
        actions.loot(token)
    assert actions.hotkey.clicks == [(0, 1)]


def test_duplicate_cavebot_window_reuses_existing_owner(monkeypatch):
    import modules.CaveBot as cavebot_module

    class FakeWindow:
        def __init__(self):
            self.lifts = 0
            self.focuses = 0

        def lift(self):
            self.lifts += 1

        def focus_force(self):
            self.focuses += 1

    existing = object.__new__(cavebot_module.CaveBot)
    existing._closed = False
    existing.window = FakeWindow()
    monkeypatch.setattr(cavebot_module, "_ACTIVE_CAVEBOT", existing)
    duplicate = cavebot_module.CaveBot.__new__(cavebot_module.CaveBot)
    assert duplicate is existing
    assert existing.window.lifts == 1
    assert existing.window.focuses == 1
