import importlib.util
from pathlib import Path
import sys
import pytest

SPEC = importlib.util.spec_from_file_location(
    "support_ui_under_test", Path(__file__).parents[1] / "modules" / "support_ui.py"
)
support_ui = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = support_ui
SPEC.loader.exec_module(support_ui)
client_point, integer, write_loader = (
    support_ui.client_point,
    support_ui.integer,
    support_ui.write_loader,
)


@pytest.mark.parametrize("value", ("", "x", 101, -1))
def test_percentage_validation(value):
    with pytest.raises(ValueError):
        integer(value, "life")


def test_client_point_accepts_zero_and_labels_values_as_client_local():
    assert client_point("0", "42") == (0, 42)
    with pytest.raises(ValueError, match="client X"):
        client_point("left", "42")


def test_write_loader_uses_atomic_update_when_available(monkeypatch):
    calls = []

    class Config:
        @staticmethod
        def update(filename, updater):
            data = {"Loaders": {"X": {"old": 1}}}
            returned = updater(data)
            calls.append((filename, returned))

    monkeypatch.setattr(support_ui, "ConfManager", Config)
    write_loader("X", {"new": 2})
    assert calls == [("GUILoader.json", {"Loaders": {"X": {"old": 1, "new": 2}}})]


def test_worker_error_updates_status_and_resets_owner_once():
    calls = []

    class Window:
        def after(self, *args):
            calls.append(("after", args))

    class Manager:
        def Status(self):
            return {"generation": 3, "error": RuntimeError("capture unavailable")}

    ui = object.__new__(support_ui.SupportWindow)
    ui.owner, ui.window = True, Window()
    ui.status = lambda value: calls.append(("status", value))
    ui.report_worker(Manager(), on_error=lambda error: calls.append(("reset", str(error))))
    assert ("status", "Error: capture unavailable") in calls
    assert ("reset", "capture unavailable") in calls


def test_worker_phase_is_rendered_on_tk_poll():
    calls = []

    class Window:
        def after(self, *args):
            calls.append(("after", args))

    class Manager:
        def Status(self):
            return {"generation": 2, "phase": "Waiting for game focus", "error": None}

    ui = object.__new__(support_ui.SupportWindow)
    ui.owner, ui.window = True, Window()
    ui.status = lambda value: calls.append(("status", value))
    ui.report_worker(Manager())
    assert ("status", "Waiting for game focus") in calls


def test_close_destroys_window_even_when_cleanup_fails():
    class Window:
        destroyed = False

        def destroy(self):
            self.destroyed = True

    ui = object.__new__(support_ui.SupportWindow)
    ui.owner, ui.window = True, Window()
    ui._on_close = lambda: (_ for _ in ()).throw(OSError("disk failed"))
    with pytest.raises(OSError, match="disk failed"):
        ui.close()
    assert ui.window.destroyed
