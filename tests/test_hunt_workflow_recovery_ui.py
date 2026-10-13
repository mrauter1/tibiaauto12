"""Operator recovery controls and immutable live inspection integration."""

from types import SimpleNamespace

from test_hunt_workflow_ui import Value, headless_ui
from tibia_client.hunt_workflow import HuntWorkflowController
from tibia_client.models import Observation, Quality, Sensor, SensorKind


class Widget:
    def __init__(self):
        self.options = {}
        self.visible = False

    def configure(self, **options):
        self.options.update(options)

    def pack(self, **options):
        self.visible = True

    def pack_forget(self):
        self.visible = False


def polling_ui(tmp_path, status, inspection):
    ui = headless_ui(tmp_path)
    ui.controller.status = lambda: status
    ui.controller.inspection_status = lambda: inspection
    ui.window = SimpleNamespace(after=lambda *_args: None)
    ui.runtime, ui.inspection = Value(), Value()
    ui.recovery_choice = Value("")
    ui._recovery_candidates = {}
    ui._inspection_readings = {}
    ui._last_inspection_frame = None
    ui.notebook = object()
    for name in (
        "manual_button", "recheck_button", "return_button", "return_now_button",
        "recovery_panel", "recovery_selector", "recovery_button",
    ):
        setattr(ui, name, Widget())
    rendered = []
    ui._render_inspection_frame = lambda: rendered.append(
        (ui._last_inspection_frame, ui._inspection_readings)
    )
    return ui, rendered


def test_poll_preserves_frozen_live_diagnostics_and_clears_old_results(tmp_path):
    sensor = Sensor(SensorKind.VISUAL, (0, 0, 20, 20), "context")
    observation = Observation(
        sensor, None, 1.0, 1, 1, Quality.UNKNOWN, "context is ambiguous",
        {"type": "visual_ambiguity", "scope": "context",
         "boxes": ((1, 1, 4, 4), (3, 1, 6, 4)), "scores": (1.0, 1.0)},
    )
    frame = object()
    inspected = {
        "active": False, "frame": frame,
        "readings": {"mana": HuntWorkflowController._reading(observation)},
        "bindings": {"mana": {"region": sensor.region, "name": sensor.name}},
    }
    ui, rendered = polling_ui(tmp_path, {}, inspected)
    ui._poll()
    assert "context is ambiguous" in ui.inspection.get()
    assert len(rendered) == 1 and rendered[0][0] is frame
    assert rendered[0][1]["mana"]["diagnostics"] == observation.diagnostics

    inspected.update(frame=None, readings={}, error="inspection was replaced")
    ui._poll()
    assert rendered[-1] == (None, {})
    assert "inspection was replaced" in ui.inspection.get()


def test_recovery_controls_choose_current_anchor_without_manual_claim(tmp_path):
    status = {
        "active": True, "phase": "hunt", "manual_required": False,
        "recovery_reason": "route_anchor_ambiguous_select_candidate",
        "recovery_candidates": (0, 2),
    }
    ui, _rendered = polling_ui(tmp_path, status, {})
    chosen = []
    ui.controller.select_recovery_anchor = chosen.append
    ui._poll()
    assert ui.recheck_button.options["state"] == "normal"
    assert ui.manual_button.options["state"] == "disabled"
    assert ui.recovery_panel.visible
    assert ui.recovery_selector.options["values"] == ("Waypoint 1", "Waypoint 3")
    assert ui.recovery_button.options["state"] == "disabled"

    ui.recovery_choice.set("Waypoint 3")
    ui._poll()
    assert ui.recovery_button.options["state"] == "normal"
    ui.resume_at_selected_anchor()
    assert chosen == [2]
    assert "fresh evidence" in ui.message.get()
    assert not any(call[0] == "ack" for call in ui.controller.calls)

    # A selected anchor can disappear before the next frame. Keep the remaining
    # candidate selectable even when only one is left, so recovery cannot trap
    # the operator behind a hidden control.
    status.update(
        recovery_reason="selected_route_anchor_not_currently_recognized",
        recovery_candidates=(0,),
    )
    ui._poll()
    assert ui.recovery_panel.visible
    assert ui.recovery_choice.get() == ""
    ui.recovery_choice.set("Waypoint 1")
    ui.resume_at_selected_anchor()
    assert chosen == [2, 0]

    status.update(recovery_reason=None, recovery_candidates=())
    ui._poll()
    assert not ui.recovery_panel.visible
    assert ui.recovery_choice.get() == ""
    assert ui.recheck_button.options["state"] == "disabled"


def test_paused_controls_explain_why_recovery_commands_are_unavailable(tmp_path):
    ui, _rendered = polling_ui(
        tmp_path,
        {"active": True, "paused": True, "manual_required": True},
        {},
    )
    ui._poll()
    assert "Navigation is paused" in ui.runtime.get()
    for button in (ui.manual_button, ui.recheck_button, ui.return_now_button):
        assert button.options["state"] == "disabled"
