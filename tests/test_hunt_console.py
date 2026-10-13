from types import SimpleNamespace

from modules.HuntConsole import HuntConsole


class Button:
    def __init__(self):
        self.state = "normal"

    def configure(self, *, state):
        self.state = state


def console(monkeypatch):
    import importlib

    module = importlib.import_module("modules.HuntConsole")
    ui = object.__new__(HuntConsole)
    ui._pending, ui._control_serial, ui._closed = None, 0, False
    ui.window = object()
    ui.stop, ui.pause, ui.resume = Button(), Button(), Button()
    messages, work = [], []
    ui.message = SimpleNamespace(set=messages.append)
    monkeypatch.setattr(
        module, "run_control", lambda window, operation, done: work.append((operation, done))
    )
    return ui, messages, work


def test_stop_remains_available_and_supersedes_pending_navigation(monkeypatch):
    ui, messages, work = console(monkeypatch)
    ui._control(lambda: True, "Navigation paused")
    assert ui.stop.state == "normal"
    assert ui.pause.state == ui.resume.state == "disabled"
    _, navigation_done = work.pop()

    ui._control(lambda: ("CaveBot",), "All automation stopped", is_stop=True)
    stop, stop_done = work.pop()
    assert ui.stop.state == "disabled"
    stop_done(stop(), None)
    assert messages[-1] == "All automation stopped"

    navigation_done(True, None)
    assert messages[-1] == "All automation stopped"
    assert ui._pending is None
    assert ui.stop.state == ui.pause.state == ui.resume.state == "normal"


def test_noop_navigation_control_does_not_report_success(monkeypatch):
    ui, messages, work = console(monkeypatch)
    ui._control(lambda: False, "Navigation resumed")
    operation, done = work.pop()
    done(operation(), None)
    assert "unchanged" in messages[-1]
    assert messages[-1] != "Navigation resumed"


def test_control_failure_restores_buttons_and_reports_error(monkeypatch):
    ui, messages, work = console(monkeypatch)
    ui._control(lambda: None, "Navigation resumed")
    _, done = work.pop()
    done(None, "profile changed")
    assert messages[-1] == "Control failed: profile changed"
    assert ui.stop.state == ui.pause.state == ui.resume.state == "normal"
