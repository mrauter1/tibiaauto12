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
    owner.walk_enabled = Variable(True)
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


def test_route_selection_and_reorder_do_not_requeue_selection_forever(monkeypatch):
    import modules.CaveBot as cavebot_module
    from engine.CaveBot.editor import RouteEditor
    from engine.CaveBot.routes import Route, Waypoint

    class Variable:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class Widget:
        def configure(self, **_kwargs):
            pass

    class EventTree:
        """Queue TreeviewSelect whenever SET is used, matching Tk's behavior."""

        def __init__(self):
            self.rows = []
            self.selected = ()
            self.events = []
            self.handler = None

        def bind(self, _event, handler):
            self.handler = handler

        def get_children(self):
            return tuple(self.rows)

        def delete(self, iid):
            self.rows.remove(iid)
            if iid in self.selected:
                self.selected = ()
                self.events.append(self.handler)

        def insert(self, _parent, _where, *, iid, values):
            del values
            self.rows.append(iid)

        def selection(self):
            return self.selected

        def selection_set(self, iid):
            self.selected = (iid,)
            self.events.append(self.handler)

        def focus(self, _iid):
            pass

        def see(self, _iid):
            pass

        def drain(self, limit=10):
            processed = 0
            while self.events:
                processed += 1
                if processed > limit:
                    pytest.fail("TreeviewSelect was requeued indefinitely")
                self.events.pop(0)()
            return processed

    class PreviewImage:
        def convert(self, _mode):
            return self

        def thumbnail(self, _size):
            pass

    route = Route("loop", (Waypoint("one", "Star"), Waypoint("two", "Flag")))
    owner = object.__new__(cavebot_module.CaveBot)
    owner.window = object()
    owner.script_name = Variable("loop")
    owner.mark_name = Variable("Star")
    owner.route_label = owner.route_detail = owner.preview_label = Widget()
    owner.route_table = EventTree()
    owner.route_table.bind("<<TreeviewSelect>>", owner._select_waypoint)
    owner._route = route
    owner._editor = RouteEditor(route)
    owner._editor_index = 0
    owner._set_message = lambda _message: None
    monkeypatch.setattr(cavebot_module.Image, "open", lambda _path: PreviewImage())
    monkeypatch.setattr(cavebot_module.ImageTk, "PhotoImage", lambda image, master: image)

    owner._render_route()
    assert owner.route_table.drain() == 1

    owner.route_table.selection_set("1")
    assert owner.route_table.drain() == 1
    assert owner._editor.selected_index == 1

    owner._move_waypoint_up()
    assert owner.route_table.drain() <= 2
    assert owner._editor.selected_index == 0
    assert owner._route.waypoints[0].id == "two"
