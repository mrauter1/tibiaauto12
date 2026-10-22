"""Headless lifecycle checks for the delayed Sandbox calibration UI."""
from copy import deepcopy
from types import SimpleNamespace
import sys

import pytest

import core.GUI as gui_module
import modules.ChooseConfig as chooser_module


class Value:
    def __init__(self, *, master=None, value=None):
        self.value = value

    def get(self):
        return self.value


class Widget:
    def __init__(self, *_args, **options):
        self.options = dict(options)

    def configure(self, **options):
        self.options.update(options)

    def place(self, **options):
        self.options.update(options)


class Window:
    def __init__(self):
        self.exists = True
        self.callbacks = {}
        self.cancelled = []
        self.actions = []
        self.bindings = {}
        self.next_timer = 0

    def winfo_exists(self):
        return self.exists

    def bind(self, sequence, callback, **_options):
        self.bindings[sequence] = callback

    def after(self, milliseconds, callback):
        self.next_timer += 1
        timer = str(self.next_timer)
        self.callbacks[timer] = milliseconds, callback
        return timer

    def after_cancel(self, timer):
        self.cancelled.append(timer)
        self.callbacks.pop(timer, None)

    def run_timer(self, timer):
        _milliseconds, callback = self.callbacks.pop(timer)
        callback()

    def geometry(self, value):
        self.actions.append(("geometry", value))

    def deiconify(self):
        self.actions.append("show")

    def destroy(self):
        self.exists = False
        self.actions.append("destroy")
        callback = self.bindings.get("<Destroy>")
        if callback:
            callback(SimpleNamespace(widget=self))


class GUI:
    def __init__(self, *_args):
        self.windowID = Window()
        self.buttons = {}
        self.radios = {}
        self.windows = []

    def MainWindow(self, *args):
        self.windows.append(args)

    def addEntry(self, *_args):
        return Widget()

    def addLabel(self, *_args):
        return Widget()

    def addRadio(self, text, *_args):
        self.radios[text] = Widget()
        return self.radios[text]

    def addCheck(self, *_args, **_options):
        return Widget()

    def addButton(self, text, command, *_args):
        self.buttons[text] = command
        return Widget()

    def withdraw(self):
        self.windowID.actions.append("hide")

    def deiconify(self):
        self.windowID.deiconify()

    def destroyWindow(self):
        self.windowID.destroy()

    def Protocol(self, callback):
        self.close = callback

    def loop(self):
        pass


@pytest.fixture
def ui(monkeypatch):
    config = {"capture_size": [800, 600], "window_target": {"kind": "sandbox"}}
    saved, opened, errors, workers = [], [], [], []
    host = Window()

    class Thread:
        def __init__(self, *, target, **_options):
            self.target = target

        def start(self):
            workers.append(self.target)

    monkeypatch.setattr(chooser_module, "GUI", GUI)
    for name in ("StringVar", "IntVar", "BooleanVar"):
        monkeypatch.setattr(chooser_module.tk, name, Value)
    monkeypatch.setattr(chooser_module.tk, "Label", Widget)
    monkeypatch.setattr(chooser_module, "profile_filename", lambda _name: "Draft.json")
    monkeypatch.setattr(chooser_module.ConfManager, "get", lambda _filename: config)
    monkeypatch.setattr(chooser_module.ConfManager, "set",
                        lambda value, filename: saved.append((deepcopy(value), filename)))

    def update(filename, callback):
        callback(config)
        saved.append(("activate", filename))

    monkeypatch.setattr(chooser_module.ConfManager, "update", update)
    monkeypatch.setattr(chooser_module, "prepare_profile", lambda *_args, **_kw: {"valid": True})
    monkeypatch.setattr(chooser_module.threading, "Thread", Thread)
    monkeypatch.setattr(chooser_module.messagebox, "showerror", lambda *args, **_kw: errors.append(args))
    monkeypatch.setattr(gui_module, "application_root", lambda: host)
    monkeypatch.setitem(sys.modules, "modules.Root", SimpleNamespace(
        root=lambda *args: opened.append(args),
    ))
    chooser = chooser_module.ChooseConfig("Windows Sandbox")
    return SimpleNamespace(chooser=chooser, config=config, saved=saved, opened=opened,
                           errors=errors, workers=workers, host=host,
                           window=chooser.window.windowID,
                           start=chooser.window.buttons["Create / Load"])


def test_sandbox_controls_are_added_to_one_window_and_physical_only(ui):
    assert len(ui.chooser.window.windows) == 1
    assert ("geometry", "414x245") in ui.window.actions
    assert ui.chooser.window.radios["Window messages (compatible clients)"].options["state"] == "disabled"


def test_close_during_countdown_cancels_timer_and_stale_callback(ui):
    ui.start()
    timer = ui.chooser._prepare_timer
    milliseconds, delayed_begin = ui.window.callbacks[timer]
    assert milliseconds == 3000 and not ui.workers
    ui.chooser.window.close()
    assert timer in ui.window.cancelled
    assert ui.chooser._closed and not ui.chooser._preparing
    delayed_begin()  # Even an already-delivered callback cannot start work.
    assert not ui.workers and not ui.saved and not ui.opened and not ui.errors
    assert ui.host.actions == ["show"]


@pytest.mark.parametrize("failure", [False, True])
def test_close_while_worker_runs_discards_late_result(ui, monkeypatch, failure):
    if failure:
        def prepare(*_args, **_options):
            raise RuntimeError("capture failed")
        monkeypatch.setattr(chooser_module, "prepare_profile", prepare)
    ui.start()
    ui.window.run_timer(ui.chooser._prepare_timer)
    assert len(ui.workers) == 1
    timer = ui.chooser._prepare_timer
    _milliseconds, stale_poll = ui.window.callbacks[timer]
    ui.chooser.window.close()
    actions = list(ui.window.actions)
    ui.workers[0]()
    stale_poll()
    assert not ui.saved and not ui.opened and not ui.errors
    assert ui.window.actions == actions


def test_external_window_destruction_invalidates_pending_work(ui):
    ui.start()
    ui.window.run_timer(ui.chooser._prepare_timer)
    _milliseconds, stale_poll = ui.window.callbacks[ui.chooser._prepare_timer]
    ui.window.destroy()
    ui.workers[0]()
    stale_poll()
    assert ui.chooser._closed and not ui.saved and not ui.opened and not ui.errors


def test_child_destruction_does_not_cancel_calibration(ui):
    ui.start()
    ui.chooser._on_destroy(SimpleNamespace(widget=object()))
    assert not ui.chooser._closed and ui.chooser._preparing


def test_valid_completion_saves_then_activates_once(ui):
    ui.start()
    ui.start()
    assert len(ui.window.callbacks) == 1
    ui.window.run_timer(ui.chooser._prepare_timer)
    ui.workers[0]()
    ui.window.run_timer(ui.chooser._prepare_timer)
    assert ui.saved == [({"valid": True}, "Draft.json"), ("activate", "conf.json")]
    assert ui.config["configured"] is True and ui.config["preferences_name"] == "Draft.json"
    assert ui.opened == [("Windows Sandbox", "Draft.json")]
    assert ui.chooser._closed and not ui.window.exists and not ui.errors


def test_open_window_capture_failure_shows_error_and_allows_retry(ui, monkeypatch):
    def prepare(*_args, **_options):
        raise RuntimeError("Sandbox is not foreground")
    monkeypatch.setattr(chooser_module, "prepare_profile", prepare)
    ui.start()
    ui.window.run_timer(ui.chooser._prepare_timer)
    ui.workers[0]()
    ui.window.run_timer(ui.chooser._prepare_timer)
    assert not ui.saved and not ui.opened and not ui.chooser._preparing
    assert "Sandbox is not foreground" in ui.errors[0][1]
    assert ui.window.actions[-1] == "show"
    previous_generation = ui.chooser._prepare_generation
    ui.start()
    assert ui.chooser._prepare_generation > previous_generation
