"""Production composition gate: file changes and mismatched modes fail closed."""
from dataclasses import replace

import pytest

from conf.conf_manager import ConfManager
from test_startup import valid_profile
from tibia_client import compatibility as compat
from tibia_client.desktop_profile import DesktopProfileController
from tibia_client.diagnostics import AdapterCapabilities


@pytest.fixture
def configured(tmp_path, monkeypatch):
    calibration = valid_profile()
    calibration.update(calibration_size=[800, 600], MouseOption=1)
    monkeypatch.setattr(ConfManager, "path", staticmethod(lambda name: tmp_path / name))
    monkeypatch.setattr(compat, "scripts_path", lambda name: tmp_path / name)
    ConfManager.set(calibration, "Test.json")
    ConfManager.set({"configured": True, "preferences_name": "Test.json", "capture_size": [800, 600],
                     "capture_hwnd": 11, "input_hwnd": 22,
                     "capture_title": "OBS projector", "input_title": "Tibia"}, "conf.json")
    environment = compat.EnvironmentMetadata("build", "ot", "Windows", "OpenGL", "OBS",
                                             "1:1 projector", "100%", "test", "physical",
                                             capture_title="OBS projector", input_title="Tibia")
    context = compat.context_from_calibration(calibration, environment)
    profile = compat.CompatibilityProfile("Test.json", context)
    at = "2026-10-13T00:00:00+00:00"
    # These are isolated parser/guard fixtures, never shipped as live-client evidence.
    for point in ((0, 0), (799, 0), (0, 599), (799, 599), (400, 300)):
        profile = profile.add_alignment(compat.AlignmentEvidence(
            context.fingerprint, at, point, point, compat.Outcome.PASS, compat.EvidenceSource.LIVE))
    profile = profile.add_capture_check(compat.CaptureCheck(
        context.fingerprint, at, compat.Outcome.PASS, compat.Outcome.PASS, compat.Outcome.PASS,
        source=compat.EvidenceSource.LIVE))
    profile = profile.add_evidence(compat.ProbeEvidence(
        "press", context.fingerprint, at, dispatch=compat.Outcome.PASS,
        operator_confirmation=compat.Outcome.PASS, source=compat.EvidenceSource.LIVE,
        key="F12"))
    compat.save_profile(profile)
    return profile


def controller(size=(800, 600), *, verified=True):
    gate = DesktopProfileController("Test.json", input_size=lambda: size,
        capabilities=AdapterCapabilities(("press", "left_click"), ("health", "target")))
    if verified and gate.profile is not None and size == (800, 600):
        verify_binding(gate)
    return gate


def verify_binding(gate):
    token = gate.begin_probe()
    check = replace(gate.profile.capture_checks[-1],
                    recorded_at="2026-10-13T00:00:01+00:00")
    compat.save_profile(gate.profile.add_capture_check(check), overwrite=True)
    assert not gate.reload().allowed
    gate.confirm_binding(token, check)


def test_only_verified_operations_project_to_capabilities(configured):
    gate = controller()
    assert gate.check(("press",)).allowed
    assert not gate.check(("left_click",)).allowed
    assert gate.status()["capabilities"] == ("press", "sensor.health", "sensor.target")


def test_controller_uses_shared_physical_cursor_restore_dependency(configured):
    gate = controller()
    ctx = gate.profile.context
    at = "2026-10-13T00:00:02+00:00"
    gate.profile = gate.profile.add_evidence(compat.ProbeEvidence(
        "left_click", ctx.fingerprint, at, dispatch=compat.Outcome.PASS,
        operator_confirmation=compat.Outcome.PASS, source=compat.EvidenceSource.LIVE,
        point=(10, 20),
    ))
    check = gate.check(("left_click",))
    assert not check.allowed
    assert any("cursor_restore" in reason for reason in check.reasons)


def test_binding_change_latches_until_explicit_reload_but_handles_are_not_durable(configured):
    gate = controller()
    ConfManager.update("conf.json", lambda value: {**value, "input_hwnd": 33})
    assert not gate.check(("press",)).allowed
    assert "binding changed" in gate.check().reasons[0]
    assert not gate.reload().allowed
    token = gate.begin_probe()
    check = replace(gate.profile.capture_checks[-1], recorded_at="2026-10-13T00:00:02+00:00")
    compat.save_profile(gate.profile.add_capture_check(check), overwrite=True)
    gate.reload()
    assert gate.confirm_binding(token, check).allowed
    assert gate.profile.context.fingerprint == configured.context.fingerprint


def test_recalibration_cannot_reuse_old_input_evidence(configured):
    gate = controller()
    def move(data):
        data["Positions"]["LifePosition"][0]["x"] += 1
        return data
    ConfManager.update("Test.json", move)
    assert not gate.check().allowed
    assert not gate.reload().allowed
    assert any("calibration changed" in reason for reason in gate.check().reasons)


@pytest.mark.parametrize("size", [(1024, 768), (0, 0)])
def test_current_input_geometry_must_match(configured, size):
    gate = controller(size)
    assert not gate.check().allowed


def test_mode_change_cannot_reuse_evidence(configured):
    ConfManager.update("Test.json", lambda data: {**data, "MouseOption": 0})
    gate = controller()
    assert not gate.check().allowed
    assert "input mode" in gate.check().reasons[0]


def test_missing_corrupt_and_replaced_sidecar_fail_closed(configured):
    gate = controller()
    path = ConfManager.path("Test.compatibility.json")
    path.write_text("{broken", encoding="utf-8")
    assert not gate.check().allowed
    assert not gate.reload().allowed
    path.unlink()
    assert not gate.reload().allowed
    compat.save_profile(configured)
    assert not gate.reload().allowed
    verify_binding(gate)
    assert gate.check().allowed


def test_undeclared_feature_is_blocked(configured):
    gate = controller()
    assert not gate(object()).allowed


def test_sidecar_from_other_calibration_is_not_accepted(configured):
    compat.save_profile(replace(configured, calibration_profile="Other.json"),
                        "Test.compatibility.json", overwrite=True)
    assert not controller().check().allowed


def test_startup_requires_new_binding_evidence_and_rejects_historical_record(configured):
    gate = controller(verified=False)
    assert not gate.check().allowed
    token = gate.begin_probe()
    with pytest.raises(ValueError, match="newly collected"):
        gate.confirm_binding(token, configured.capture_checks[-1])
    verify_binding(gate)
    assert gate.check().allowed


def test_probe_token_rejects_window_switch_even_with_same_size(configured):
    gate = controller(verified=False)
    token = gate.begin_probe()
    ConfManager.update("conf.json", lambda data: {**data, "input_hwnd": 99})
    assert not gate.validate_probe_token(token).allowed
    gate.reload()
    assert not gate.validate_probe_token(token).allowed
    assert not gate.check().allowed


def test_profile_edit_after_arm_invalidates_probe_before_dispatch(configured):
    gate = controller(verified=False)
    token = gate.begin_probe()
    compat.save_profile(replace(configured, evidence=()), overwrite=True)
    assert not gate.validate_probe_token(token).allowed
