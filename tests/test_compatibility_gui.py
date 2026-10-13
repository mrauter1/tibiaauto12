import subprocess
import sys
from types import SimpleNamespace

import pytest

import modules.Compatibility as compatibility_module
from core.paths import PROJECT_ROOT
from modules.Compatibility import (
    Compatibility,
    PreviewMapping,
    _add_probe_evidence,
    _window_titles,
    probe_completion,
)
from test_compatibility import context, operation
from tibia_client import Action, ActionKind, ActionResult, FeatureStatus, ResultState, Sensor, SensorKind
from tibia_client.compatibility import CompatibilityProfile, Outcome, ProbeFeature


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


def test_arming_a_key_probe_displays_its_actual_operation_before_queueing():
    class Variable:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    viewer = object.__new__(Compatibility)
    viewer.profile = object()
    viewer._preview_ready = lambda: True
    viewer.controller = None
    viewer.session = SimpleNamespace(probe_evidence_source=lambda: "simulation")
    viewer.operation = Variable("press")
    viewer.hotkey = Variable("Ctrl + F1")
    viewer.target_label = Variable("visible control")
    viewer.alignment_confirmation = Variable(Outcome.PASS.value)
    viewer.cursor_confirmation = Variable(Outcome.PASS.value)
    viewer.selected_point = None
    viewer.drag_destination = None
    viewer.status = Variable("")
    queued = []
    viewer._queue_probe = lambda probe, kind, token: queued.append((probe, kind, token))

    viewer.arm_probe()

    assert viewer.operation.get() == "modified_press"
    assert queued[0][0].operation == "modified_press"
    assert queued[0][0].action.key == "Ctrl + F1"


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


def test_confirmed_probe_declares_a_supported_profile_operation_with_its_evidence():
    ctx = context()
    original = CompatibilityProfile("Hunt.json", ctx, ("press",))
    evidence = operation(ctx, "right_click", point=(10, 20))
    updated = _add_probe_evidence(original, evidence)
    restored = CompatibilityProfile.from_dict(updated.to_dict())

    assert updated.declared_operations == ("press", "right_click")
    assert updated.evidence == (evidence,)
    assert restored == updated
    assert original.declared_operations == ("press",)
    assert original.evidence == ()


def test_cancel_discards_a_completed_probe_without_stopping_or_saving():
    class Status:
        def __init__(self):
            self.message = ""

        def set(self, value):
            self.message = value

    viewer = object.__new__(Compatibility)
    viewer.pending_probe = object()
    viewer._capture_before = None
    viewer._probe_started = True
    viewer._awaiting_confirmation = True
    viewer._cancel_requested = False
    viewer.status = Status()
    viewer.session = SimpleNamespace(
        stop_feature=lambda _name: pytest.fail("inactive completed probe must not be stopped")
    )
    cleared = []
    viewer._clear_pending = lambda: cleared.append(True)

    viewer.cancel_probe()

    assert cleared == [True]
    assert "discarded" in viewer.status.message
    assert "no evidence" in viewer.status.message


def test_repeated_cancel_does_not_issue_another_stop_request():
    class Status:
        def __init__(self):
            self.message = ""

        def set(self, value):
            self.message = value

    viewer = object.__new__(Compatibility)
    viewer.pending_probe = object()
    viewer._capture_before = None
    viewer._probe_started = True
    viewer._awaiting_confirmation = False
    viewer._cancel_requested = True
    viewer._cancel_stop_pending = True
    viewer.status = Status()
    viewer._run_background = lambda *_args: pytest.fail(
        "a second cancellation request must not be issued"
    )

    viewer.cancel_probe()

    assert "already requested" in viewer.status.message


def test_cancelled_action_completion_cannot_enter_operator_confirmation():
    class Status:
        def __init__(self):
            self.message = ""

        def set(self, value):
            self.message = value

    probe = ProbeFeature("press", Action(ActionKind.PRESS, key="F1"))
    probe.result = ActionResult(
        "CompatibilityProbe:1:1", probe.name, "compatibility-probe",
        ResultState.CANCELLED, 1.0,
    )
    viewer = object.__new__(Compatibility)
    viewer._closed = False
    viewer.pending_probe = probe
    viewer._probe_started = True
    viewer._cancel_requested = True
    viewer._cancel_stop_pending = False
    viewer._awaiting_confirmation = False
    viewer.session = SimpleNamespace(
        feature_status=lambda _name: FeatureStatus(probe.name, 1, False, "Stopped")
    )
    viewer.status = Status()
    viewer.window = SimpleNamespace(after=lambda *_args: None)
    cleared = []
    viewer._clear_pending = lambda: cleared.append(True)
    viewer._complete_probe = lambda _probe: pytest.fail(
        "cancelled probe must not reach confirmation"
    )

    viewer._poll()

    assert cleared == [True]
    assert "no evidence" in viewer.status.message


def test_probe_start_captures_the_session_generation():
    class Status:
        def set(self, _value):
            pass

    probe = ProbeFeature("press", Action(ActionKind.PRESS, key="F1"))
    viewer = object.__new__(Compatibility)
    viewer._closed = False
    viewer.pending_probe = probe
    viewer._probe_started = False
    viewer._probe_generation = None
    viewer._countdown_id = object()
    viewer.session = SimpleNamespace(start_probe=lambda _probe: 17)
    viewer.status = Status()

    viewer._countdown(0)

    assert viewer._probe_started
    assert viewer._probe_generation == 17


def test_cancellation_keeps_retry_blocked_until_the_stop_operation_finishes():
    class Status:
        def __init__(self):
            self.message = ""

        def set(self, value):
            self.message = value

    class Button:
        def configure(self, **_values):
            pass

    probe = ProbeFeature("press", Action(ActionKind.PRESS, key="F1"))
    probe.result = ActionResult(
        "CompatibilityProbe:1:1", probe.name, "compatibility-probe",
        ResultState.CANCELLED, 1.0,
    )
    viewer = object.__new__(Compatibility)
    viewer._closed = False
    viewer.pending_probe = probe
    viewer._capture_before = None
    viewer._probe_started = True
    viewer._awaiting_confirmation = False
    viewer._cancel_requested = False
    viewer._cancel_stop_pending = False
    viewer._probe_generation = 23
    viewer.status = Status()
    viewer.cancel_button = Button()
    viewer._set_confirm_enabled = lambda _enabled: None
    stopped = []
    viewer.session = SimpleNamespace(
        stop_feature=lambda name, *, expected_generation: stopped.append(
            (name, expected_generation)
        ) or True,
        feature_status=lambda _name: FeatureStatus(probe.name, 1, False, "Stopped"),
    )
    viewer.window = SimpleNamespace(after=lambda *_args: None)
    background = []
    viewer._run_background = lambda operation, done: background.append((operation, done))
    cleared = []
    viewer._clear_pending = lambda: cleared.append(True)

    viewer.cancel_probe()
    viewer._poll()

    assert viewer._cancel_stop_pending
    assert cleared == []
    operation, done = background.pop()
    done(operation(), None)
    viewer._poll()
    assert cleared == [True]
    assert stopped == [(ProbeFeature.name, 23)]


def test_close_worker_keeps_the_old_probe_generation_after_fields_are_cleared(monkeypatch):
    targets = []

    class DeferredThread:
        def __init__(self, *, target, name, daemon):
            assert name == "compatibility-close-cancel"
            assert daemon is True
            targets.append(target)

        def start(self):
            pass

    stopped = []
    destroyed = []
    viewer = object.__new__(Compatibility)
    viewer._closed = False
    viewer._window_key = 12345
    viewer._countdown_id = None
    viewer.pending_probe = object()
    viewer.pending_token = object()
    viewer._probe_started = True
    viewer._probe_generation = 31
    viewer.session = SimpleNamespace(
        stop_feature=lambda name, *, expected_generation: stopped.append(
            (name, expected_generation)
        )
    )
    viewer.window = SimpleNamespace(destroy=lambda: destroyed.append(True))
    monkeypatch.setattr(compatibility_module.threading, "Thread", DeferredThread)

    viewer.close()
    assert viewer.pending_probe is None
    assert viewer._probe_generation is None
    targets[0]()

    assert stopped == [(ProbeFeature.name, 31)]
    assert destroyed == [True]


def test_post_action_preview_requires_after_observations_and_current_context():
    matching = SimpleNamespace(size=(800, 600))
    stale = SimpleNamespace(size=(640, 480))
    probe = ProbeFeature("press", Action(ActionKind.PRESS, key="F1"))
    probe.after = {Sensor(SensorKind.FRAME_VALID): True}
    viewer = object.__new__(Compatibility)
    viewer.profile = SimpleNamespace(context=SimpleNamespace(capture_size=(800, 600)))
    viewer.pending_probe = probe
    viewer.pending_token = None
    viewer.controller = None
    displayed = []
    viewer._set_preview = displayed.append
    viewer.session = SimpleNamespace(preview_frame=lambda: matching)

    assert viewer._refresh_after_action_preview(probe)
    assert displayed == [matching]

    viewer.session = SimpleNamespace(preview_frame=lambda: stale)
    assert not viewer._refresh_after_action_preview(probe)
    assert displayed == [matching]

    viewer.controller = SimpleNamespace(
        validate_probe_token=lambda _token: SimpleNamespace(
            allowed=False, reasons=("context changed",)
        )
    )
    viewer.session = SimpleNamespace(
        preview_frame=lambda: pytest.fail("stale probe token must prevent preview refresh")
    )
    assert not viewer._refresh_after_action_preview(probe)

    viewer.controller = None
    probe.after = {}
    viewer.session = SimpleNamespace(
        preview_frame=lambda: pytest.fail("before frame must not be presented as post-action")
    )
    assert not viewer._refresh_after_action_preview(probe)


def test_gui_module_import_is_native_free():
    script = """
import sys
import modules.Compatibility
assert not {'tibia_client.desktop', 'core.HookWindow', 'core.MoveMouse', 'pyautogui', 'win32gui'} & set(sys.modules)
"""
    subprocess.run([sys.executable, "-c", script], cwd=PROJECT_ROOT, check=True)
