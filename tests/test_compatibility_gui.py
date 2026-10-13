import subprocess
import sys

import pytest

from core.paths import PROJECT_ROOT
from modules.Compatibility import Compatibility, PreviewMapping, _window_titles, probe_completion
from tibia_client import Action, ActionKind, ActionResult, FeatureStatus, ResultState, Sensor, SensorKind
from tibia_client.compatibility import ProbeFeature


def test_preview_mapping_preserves_aspect_ratio_and_maps_only_image_bounds():
    mapping = PreviewMapping((720, 390), (800, 600))
    left, top, width, height = mapping.bounds
    assert width / height == pytest.approx(4 / 3, rel=0.01)
    assert (left, top) == ((720 - width) // 2, (390 - height) // 2)
    assert mapping.client_point(left, top) == (0, 0)
    assert mapping.client_point(left + width - 1, top + height - 1) == (799, 599)
    assert mapping.canvas_point((400, 300)) == pytest.approx(
        (left + 400 / 799 * (width - 1), top + 300 / 599 * (height - 1))
    )
    with pytest.raises(ValueError, match="inside"):
        mapping.client_point(0, 0)


def test_probe_completion_waits_for_inactive_and_post_observations():
    probe = ProbeFeature("press", Action(ActionKind.PRESS, key="F1"))
    active = FeatureStatus(probe.name, 1, True, "Waiting")
    assert probe_completion(active, probe, capture_only=False)[0] == "running"

    inactive = FeatureStatus(probe.name, 1, False, "Stopped")
    state, detail = probe_completion(inactive, probe, capture_only=False)
    assert state == "failed" and "before producing" in detail

    probe.result = ActionResult(
        "CompatibilityProbe:1:1", probe.name, "compatibility-probe",
        ResultState.EXECUTED, 1.0,
    )
    assert probe_completion(inactive, probe, capture_only=False)[0] == "failed"
    probe.after = {Sensor(SensorKind.FRAME_VALID): True}
    assert probe_completion(inactive, probe, capture_only=False)[0] == "complete"

    failed = ProbeFeature("press", Action(ActionKind.PRESS, key="F1"))
    failed.result = ActionResult(
        "CompatibilityProbe:1:2", failed.name, "compatibility-probe",
        ResultState.REJECTED, 2.0, "focus lost",
    )
    errored = FeatureStatus(failed.name, 2, False, "Failed", "adapter unavailable")
    state, detail = probe_completion(errored, failed, capture_only=False)
    assert state == "complete"
    assert "terminal action result" in detail


def test_capture_completion_never_requires_or_invents_an_action_result():
    probe = ProbeFeature("capture_preview", None)
    assert probe.required_operations() == ()
    inactive = FeatureStatus(probe.name, 1, False, "Finished")
    assert probe_completion(inactive, probe, capture_only=True)[0] == "failed"
    probe.after = {Sensor(SensorKind.FRAME_VALID): True}
    assert probe_completion(inactive, probe, capture_only=True)[0] == "complete"
    assert probe.result is None


def test_window_identity_comes_from_configuration_without_string_coercion():
    assert _window_titles({"capture_title": " OBS ", "input_title": " Game "}) == (
        "OBS", "Game"
    )
    assert _window_titles({"capture_title": 123, "input_title": None}) == ("", "")


def test_drag_requires_an_explicit_distinct_mapped_destination():
    class Variable:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

    viewer = object.__new__(Compatibility)
    viewer.operation = Variable("drag")
    viewer.hotkey = Variable("F1")
    viewer.selected_point = (10, 20)
    viewer.drag_destination = None
    with pytest.raises(ValueError, match="Shift-click"):
        viewer._action()
    viewer.drag_destination = (30, 40)
    action = viewer._action()
    assert action == Action(ActionKind.DRAG, point=(10, 20), destination=(30, 40))


def test_canvas_selection_is_ignored_while_probe_context_is_armed():
    class Status:
        def __init__(self):
            self.message = ""

        def set(self, value):
            self.message = value

    viewer = object.__new__(Compatibility)
    viewer.pending_probe = object()
    viewer._capture_before = None
    viewer.selected_point = (10, 20)
    viewer.drag_destination = (30, 40)
    viewer.status = Status()
    viewer._preview_mapping = PreviewMapping((200, 150), (100, 100))
    event = type("Event", (), {"x": 100, "y": 75, "state": 1})()

    viewer._select_canvas_point(event)

    assert viewer.selected_point == (10, 20)
    assert viewer.drag_destination == (30, 40)
    assert "locked" in viewer.status.message


def test_gui_module_import_is_native_free():
    script = """
import sys
import modules.Compatibility
assert not {'tibia_client.desktop', 'core.HookWindow', 'core.MoveMouse', 'pyautogui', 'win32gui'} & set(sys.modules)
"""
    subprocess.run([sys.executable, "-c", script], cwd=PROJECT_ROOT, check=True)
