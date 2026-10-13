import pytest


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


def test_pause_hotkey_uses_navigation_scope_and_off_remains_a_stop():
    import modules.CaveBot as cavebot_module

    calls = []

    class Client:
        def pause_navigation(self):
            calls.append("pause")
            return True

        def resume_navigation(self):
            calls.append("resume")
            return True

        def stop_feature(self, name):
            calls.append(("stop", name))

    owner = object.__new__(cavebot_module.CaveBot)
    owner._client = Client()
    owner._enabled = True
    owner._paused = False
    owner._closed = False
    owner._set_message = lambda message: calls.append(message)
    owner._update_controls = lambda: None

    owner._toggle_pause()
    assert owner._enabled is True and owner._paused is True
    owner._toggle_pause()
    assert owner._enabled is True and owner._paused is False
    owner._toggle()
    assert owner._enabled is False and owner._paused is False
    assert calls[0] == "pause"
    assert "resume" in calls
    assert ("stop", "CaveBot") in calls


def test_pause_state_changes_only_when_session_accepts_request():
    import modules.CaveBot as cavebot_module

    class Client:
        def pause_navigation(self):
            return False

        def resume_navigation(self):
            return False

    messages = []
    owner = object.__new__(cavebot_module.CaveBot)
    owner._client = Client()
    owner._enabled = True
    owner._paused = False
    owner._set_message = messages.append
    owner._toggle_pause()
    assert owner._paused is False
    assert "not applied" in messages[-1]
    owner._paused = True
    owner._toggle_pause()
    assert owner._paused is True
    assert "not applied" in messages[-1]


def test_snapshot_rejects_unsaved_editor_route_before_start(monkeypatch):
    import modules.CaveBot as cavebot_module
    from engine.CaveBot.routes import Route, RouteError, Waypoint

    class Variable:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

    owner = object.__new__(cavebot_module.CaveBot)
    owner.script_name = Variable("loop")
    owner._route = Route("loop", (Waypoint("one", "Star"),))
    owner._validate_pack_activation = lambda: None
    class Existing:
        @staticmethod
        def is_file():
            return True
    monkeypatch.setattr(cavebot_module, "route_file", lambda _name: Existing())
    monkeypatch.setattr(
        cavebot_module,
        "load_route",
        lambda _name: Route("loop", (Waypoint("one", "Flag"),)),
    )
    with pytest.raises(RouteError, match="unsaved changes.*Save"):
        owner._snapshot()
