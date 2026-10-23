"""One-shot timing/intent checks with a fake clock; no native or image APIs."""

from dataclasses import FrozenInstanceError, replace
import json

import pytest

from tibia_client.probe_frame_lease import FrameLease, FrameLeaseError, ProbeIntent


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def intent(**changes):
    values = dict(hwnd=44, target_fingerprint="a" * 64, capture_method="region_gdi",
                  backend="experimental_frame_lease", operation="left_click", point=(96, 689))
    values.update(changes)
    return ProbeIntent(**values)


def lease(clock, selected=None, **changes):
    values = dict(capture_started_at=99.8, capture_completed_at=99.9,
                  frame_rgb_sha256="b" * 64, focus_verified=True, known_state="no_known_veto",
                  point_patch_verified=True, cheap_check=lambda: True, clock=clock)
    values.update(changes)
    return FrameLease(selected or intent(), **values)


def test_one_action_reuses_evidence_but_checks_each_boundary_and_closes():
    clock = Clock()
    native_calls = []
    selected = intent()
    current = lease(clock, selected, cheap_check=lambda: native_calls.append(clock()) is None)
    with current.action(selected):
        for _ in range(4):
            clock.advance(.01)
            assert current.check(selected) is True
    report = current.metadata()
    assert report["status"] == "closed" and report["reason"] is None
    assert len(native_calls) == report["check_count"] == report["cheap_check_count"] == 5
    assert report["frame_age_at_scope_start_ms"] == pytest.approx(200)
    assert report["scope_elapsed_ms"] == pytest.approx(40)
    assert report["overall_safety"] == "unknown"
    assert report["guest_changes_between_input_phases"] == "unverified"
    assert json.loads(json.dumps(report)) == report


def test_intent_is_immutable_and_normalizes_mutable_point_input():
    point = [96, 689]
    selected = intent(point=point)
    point[0] = 1
    assert selected.point == (96, 689)
    with pytest.raises(FrozenInstanceError):
        selected.operation = "right_click"
    current = lease(Clock(), selected)
    with pytest.raises(AttributeError):
        current.intent = intent(operation="right_click")


def test_evidence_cannot_be_reused_for_a_second_action():
    selected = intent()
    current = lease(Clock(), selected)
    with current.action(selected):
        pass
    with pytest.raises(FrameLeaseError, match="already used"):
        with current.action(selected):
            pytest.fail("second action must not run")


def test_check_outside_scope_revokes_instead_of_silently_starting_an_action():
    selected = intent()
    current = lease(Clock(), selected)
    with pytest.raises(FrameLeaseError, match="single active"):
        current.check(selected)
    with pytest.raises(FrameLeaseError, match="already used or revoked"):
        with current.action(selected):
            pytest.fail("a revoked lease must not activate")


@pytest.mark.parametrize("change", [
    {"hwnd": 88}, {"target_fingerprint": "c" * 64}, {"capture_method": "screen"},
    {"backend": "different"}, {"operation": "right_click"}, {"point": (97, 689)},
])
def test_any_intent_change_rejects_before_native_check(change):
    selected = intent()
    calls = []
    current = lease(Clock(), selected, cheap_check=lambda: calls.append("native") is None)
    with pytest.raises(FrameLeaseError, match="differs from the leased intent"):
        with current.action(replace(selected, **change)):
            pytest.fail("changed operation must not run")
    assert not calls


def test_age_includes_capture_and_any_encoding_delay_without_timestamp_refresh():
    clock = Clock()
    current = lease(clock, capture_started_at=99.1, capture_completed_at=99.99)
    clock.advance(.101)  # Includes a hypothetical slow PNG write after capture.
    with pytest.raises(FrameLeaseError, match="maximum acquisition age"):
        with current.action(current.intent):
            pytest.fail("old image must not authorize input")
    assert current.metadata()["cheap_check_count"] == 0


def test_action_expiry_between_down_and_next_phase_allows_unconditional_up():
    clock = Clock()
    current = lease(clock)
    events = []
    with pytest.raises(FrameLeaseError, match="action scope exceeded"):
        with current.action(current.intent):
            try:
                events.append("ctrl_down")
                clock.advance(.251)
                current.check(current.intent)
                events.append("o_down")
            finally:
                events.append("ctrl_up")
    assert events == ["ctrl_down", "ctrl_up"]
    assert current.metadata()["status"] == "revoked"


def test_time_budget_is_checked_on_scope_exit_even_if_no_later_boundary_occurs():
    clock = Clock()
    current = lease(clock)
    with pytest.raises(FrameLeaseError, match="action scope exceeded"):
        with current.action(current.intent):
            clock.advance(.251)
    assert current.metadata()["scope_elapsed_ms"] == pytest.approx(251)


def test_slow_native_callback_cannot_silently_consume_the_action_budget():
    clock = Clock()

    def slow_check():
        clock.advance(.251)
        return True

    current = lease(clock, cheap_check=slow_check)
    with pytest.raises(FrameLeaseError, match="action scope exceeded"):
        with current.action(current.intent):
            pytest.fail("timed-out callback must not permit input")


@pytest.mark.parametrize("reason", ["host focus", "geometry", "hit root", "cancelled"])
def test_native_boundary_exception_revokes_and_preserves_failure_reason(reason):
    clock = Clock()
    calls = []

    def check():
        if calls:
            raise RuntimeError(reason)
        calls.append("baseline")
        return True

    current = lease(clock, cheap_check=check)
    with pytest.raises(FrameLeaseError, match=reason):
        with current.action(current.intent):
            current.check(current.intent)
    assert current.metadata()["status"] == "revoked"
    with pytest.raises(FrameLeaseError, match="already used or revoked"):
        with current.action(current.intent):
            pytest.fail("failed boundary must remain revoked")


@pytest.mark.parametrize("answer", [False, None, 1, {"ready": True}])
def test_only_explicit_true_native_result_can_affirm_boundary(answer):
    current = lease(Clock(), cheap_check=lambda: answer)
    with pytest.raises(FrameLeaseError, match="did not affirm"):
        with current.action(current.intent):
            pytest.fail("ambiguous native state must not permit input")


@pytest.mark.parametrize("focus", [False, None])
def test_false_or_unknown_before_action_focus_blocks_without_native_dispatch(focus):
    current = lease(Clock(), focus_verified=focus)
    with pytest.raises(FrameLeaseError, match="focus evidence"):
        with current.action(current.intent):
            pytest.fail("unfocused input must not run")


@pytest.mark.parametrize("known_state", ["unknown", "blocked_known_dialog"])
def test_veto_or_unknown_game_state_rejects_regular_operation(known_state):
    current = lease(Clock(), known_state=known_state, known_dialog_name="open_channel")
    with pytest.raises(FrameLeaseError, match="known-state veto"):
        with current.action(current.intent):
            pytest.fail("known modal must not permit a regular action")


@pytest.mark.parametrize("patch", [False, None])
def test_mouse_control_patch_must_be_affirmed_from_the_before_image(patch):
    current = lease(Clock(), point_patch_verified=patch)
    with pytest.raises(FrameLeaseError, match="selected-control patch"):
        with current.action(current.intent):
            pytest.fail("changed control must not receive a click")


def test_explicit_escape_can_only_target_the_exact_recognized_expected_dialog():
    selected = intent(operation="escape", point=None, expected_dialog="open_channel")
    current = lease(Clock(), selected, known_state="blocked_known_dialog",
                    known_dialog_name="open_channel", point_patch_verified=None)
    with current.action(selected):
        assert current.check(selected)
    assert current.metadata()["status"] == "closed"
    assert current.metadata()["intent"]["expected_dialog"] == "open_channel"
    assert current.metadata()["overall_safety"] == "unknown"


def test_readiness_only_scope_accepts_no_point_and_does_not_require_mouse_patch():
    selected = intent(operation="readiness", point=None)
    current = lease(Clock(), selected, point_patch_verified=None)
    with current.action(selected):
        assert current.check(selected)
    assert current.metadata()["status"] == "closed"
    assert current.metadata()["intent"]["operation"] == "readiness"


@pytest.mark.parametrize("state,name", [
    ("unknown", "open_channel"), ("no_known_veto", "open_channel"),
    ("blocked_known_dialog", "other_dialog"), ("blocked_known_dialog", None),
])
def test_escape_expectation_does_not_bypass_unknown_absent_or_other_modal(state, name):
    selected = intent(operation="escape", point=None, expected_dialog="open_channel")
    current = lease(Clock(), selected, known_state=state, known_dialog_name=name,
                    point_patch_verified=None)
    with pytest.raises(FrameLeaseError, match="exact expected recognized dialog"):
        with current.action(selected):
            pytest.fail("Escape expectation must not be a generic veto bypass")


def test_parent_or_dispatch_exception_closes_the_scope_and_is_not_swallowed():
    current = lease(Clock())
    failure = RuntimeError("probe cancelled")
    with pytest.raises(RuntimeError) as caught:
        with current.action(current.intent):
            raise failure
    assert caught.value is failure
    assert current.metadata()["status"] == "revoked"
    assert "probe cancelled" in current.metadata()["reason"]


def test_swallowed_endpoint_guard_failure_still_fails_the_enclosing_scope():
    calls = []

    def check():
        if calls:
            raise RuntimeError("host focus lost after completed click")
        calls.append("baseline")
        return True

    current = lease(Clock(), cheap_check=check)
    events = []
    with pytest.raises(FrameLeaseError, match="host focus lost after completed click"):
        with current.action(current.intent):
            events.append("click_completed")
            try:
                current.check(current.intent)
            except FrameLeaseError:
                events.append("endpoint_failure_recorded")
    assert events == ["click_completed", "endpoint_failure_recorded"]
    assert current.metadata()["status"] == "revoked"


def test_future_capture_and_clock_regression_revoke():
    clock = Clock()
    future = lease(clock, capture_started_at=100.1, capture_completed_at=100.2)
    with pytest.raises(FrameLeaseError, match="future"):
        with future.action(future.intent):
            pytest.fail("future timestamp must not authorize input")
    current = lease(clock)
    with pytest.raises(FrameLeaseError, match="backwards"):
        with current.action(current.intent):
            clock.advance(-.01)
            current.check(current.intent)


def test_metadata_does_not_read_clock_or_refresh_evidence():
    clock = Clock()
    current = lease(clock)
    first = current.metadata()
    clock.advance(100)
    assert current.metadata() == first
    with pytest.raises(FrameLeaseError, match="maximum acquisition age"):
        with current.action(current.intent):
            pytest.fail("reading metadata must not refresh evidence")


@pytest.mark.parametrize("change", [
    {"hwnd": True}, {"hwnd": 0}, {"target_fingerprint": "invalid"},
    {"capture_method": ""}, {"backend": ""}, {"operation": "drag"},
    {"point": (True, 1)}, {"point": (-1, 1)}, {"expected_dialog": "open_channel"},
])
def test_invalid_intent_is_rejected(change):
    with pytest.raises(ValueError):
        intent(**change)


@pytest.mark.parametrize("change", [
    {"capture_started_at": float("nan")}, {"capture_completed_at": float("inf")},
    {"capture_started_at": 100, "capture_completed_at": 99},
    {"frame_rgb_sha256": "not_a_hash"}, {"focus_verified": 1},
    {"known_state": "safe"}, {"point_patch_verified": 1},
    {"max_frame_age_ms": True}, {"max_frame_age_ms": 0}, {"max_frame_age_ms": 1001},
    {"max_action_scope_ms": 0}, {"max_action_scope_ms": 251},
])
def test_invalid_evidence_and_widened_budgets_are_rejected(change):
    with pytest.raises(ValueError):
        lease(Clock(), **change)
