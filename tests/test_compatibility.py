from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

import tibia_client.compatibility as compatibility
from tibia_client.compatibility import (
    AlignmentEvidence,
    CaptureCheck,
    CompatibilityError,
    CompatibilityProfile,
    DesktopContext,
    EnvironmentMetadata,
    EvidenceSource,
    Outcome,
    ProbeEvidence,
    ProbeFeature,
    activation_guard,
    export_evidence_bundle,
    key_operation,
    load_profile,
    save_profile,
    validate_profile_context,
)
from tibia_client.models import (
    Action,
    ActionKind,
    ActionResult,
    FeatureContext,
    Observation,
    ResultState,
    Sensor,
    SensorKind,
    Snapshot,
)


NOW = datetime.now(timezone.utc).isoformat()


def environment(**changes):
    values = dict(
        client_build="13.40",
        ot_server="Example OT",
        windows_version="Windows 11 24H2",
        renderer="DirectX 11",
        obs_version="32.0",
        obs_configuration="Game Capture > window projector",
        dpi_scaling="100% per-monitor aware",
        layout="classic panels; chat open",
        input_mode="physical",
        test_date="2026-10-13",
        battleye_state="reported active",
        battleye_provenance="operator-reported client UI",
    )
    values.update(changes)
    return EnvironmentMetadata(**values)


def context(**changes):
    values = dict(
        environment=environment(),
        calibration_digest="a" * 64,
        capture_size=(800, 600),
        input_size=(800, 600),
    )
    values.update(changes)
    return DesktopContext(**values)


def alignment(ctx):
    width, height = ctx.capture_size
    points = (
        (round((width - 1) * .1), round((height - 1) * .1)),
        (round((width - 1) * .9), round((height - 1) * .1)),
        (round((width - 1) * .5), round((height - 1) * .5)),
        (round((width - 1) * .1), round((height - 1) * .9)),
        (round((width - 1) * .9), round((height - 1) * .9)),
    )
    return tuple(
        AlignmentEvidence(
            ctx.fingerprint, NOW, point, point, Outcome.PASS, EvidenceSource.LIVE,
            "operator confirmed response at the exact mapped point",
        )
        for point in points
    )


def capture_check(ctx, source=EvidenceSource.LIVE):
    return CaptureCheck(
        ctx.fingerprint,
        NOW,
        Outcome.PASS,
        Outcome.PASS,
        Outcome.PASS,
        Outcome.UNVERIFIED,
        source,
        "operator triggered a visible client change; continuous freshness is unknown",
    )


def operation(ctx, name="press", source=EvidenceSource.LIVE, **changes):
    values = dict(
        operation=name,
        context_fingerprint=ctx.fingerprint,
        recorded_at=NOW,
        dispatch=Outcome.PASS,
        operator_confirmation=Outcome.PASS,
        source=source,
        key=("Ctrl + F12" if name == "modified_press" else "F12")
        if name in ("press", "modified_press") else "",
        intended_target="visible test control",
        before=("frame_valid:-=True",),
        after=("frame_valid:-=True",),
    )
    values.update(changes)
    return ProbeEvidence(**values)


def verified_profile(required="press"):
    ctx = context()
    return CompatibilityProfile(
        "Hunt.json",
        ctx,
        (required,),
        (operation(ctx, required),),
        alignment(ctx),
        (capture_check(ctx),),
    )


def test_context_fingerprint_ignores_test_date_but_changes_environment_and_calibration():
    original = context()
    later_date = context(environment=replace(original.environment, test_date="2026-10-14"))
    changed_obs = context(environment=replace(original.environment, obs_version="32.1"))
    changed_calibration = context(calibration_digest="b" * 64)
    assert later_date.fingerprint == original.fingerprint
    assert changed_obs.fingerprint != original.fingerprint
    assert changed_calibration.fingerprint != original.fingerprint
    assert "hwnd" not in json.dumps(original.to_dict()).lower()


def test_activation_requires_capture_alignment_and_exact_operation_evidence():
    ctx = context()
    empty = CompatibilityProfile("Hunt.json", ctx, ("press",))
    check = validate_profile_context(empty, ctx, ("press",))
    assert not check.allowed
    assert any("visible-change" in reason for reason in check.reasons)
    assert any("alignment" in reason for reason in check.reasons)
    assert any("press" in reason for reason in check.reasons)

    ready = validate_profile_context(verified_profile(), ctx, ("press",))
    assert ready.allowed
    assert ready.reasons == ()


def test_hotkey_operation_matches_supported_input_grammar_and_probe_category():
    for key in ("F1", "enter", "esc", "left_arrow", "a"):
        assert key_operation(key) == "press"
    for key in ("ctrl+F1", "Ctrl + enter", "SHIFT+esc", "shift + left_arrow", "ctrl+a"):
        assert key_operation(key) == "modified_press"
    with pytest.raises(CompatibilityError, match="unsupported probe hotkey"):
        key_operation("Alt + F1")
    with pytest.raises(CompatibilityError, match="unsupported probe hotkey"):
        key_operation("A")
    with pytest.raises(CompatibilityError, match="modified_press"):
        ProbeFeature("press", Action(ActionKind.PRESS, key="ctrl+F1"))
    with pytest.raises(CompatibilityError, match="press"):
        ProbeFeature("modified_press", Action(ActionKind.PRESS, key="F1"))


def test_explicitly_mislabeled_key_evidence_cannot_replace_valid_press_evidence():
    ctx = context()
    valid = operation(ctx, "press", key="F1")
    mislabeled = operation(ctx, "press", key="Ctrl + F1")
    profile = CompatibilityProfile(
        "Hunt.json", ctx, ("press",), (valid, mislabeled), alignment(ctx),
        (capture_check(ctx),),
    )
    assert profile.operation_outcome("press") is Outcome.PASS
    only_mislabeled = replace(profile, evidence=(mislabeled,))
    assert only_mislabeled.operation_outcome("press") is Outcome.UNVERIFIED


def test_physical_mouse_validation_includes_cursor_restore_but_messages_do_not():
    physical = context()
    physical_profile = CompatibilityProfile(
        "Hunt.json", physical, ("left_click",),
        (operation(physical, "left_click"),), alignment(physical),
        (capture_check(physical),),
    )
    direct = validate_profile_context(physical_profile, physical, ("left_click",))
    assert not direct.allowed
    assert any("cursor_restore" in reason for reason in direct.reasons)

    class MouseFeature:
        @staticmethod
        def required_operations():
            return ("left_click",)

    guarded = activation_guard(physical_profile, physical)(MouseFeature())
    assert guarded == direct

    messages = context(environment=environment(input_mode="messages"))
    message_profile = CompatibilityProfile(
        "Hunt.json", messages, ("left_click",),
        (operation(messages, "left_click"),), alignment(messages),
        (capture_check(messages),),
    )
    assert validate_profile_context(message_profile, messages, ("left_click",)).allowed


def test_physical_dependency_expansion_reports_unhashable_unknown_operations():
    physical = context()
    profile = verified_profile()
    check = validate_profile_context(profile, physical, ("left_click", ["malformed"]))
    assert not check.allowed
    assert any("unknown operation" in reason for reason in check.reasons)
    assert any("cursor_restore" in reason for reason in check.reasons)


def test_simulation_cannot_unlock_capture_alignment_or_operation():
    ctx = context()
    profile = CompatibilityProfile(
        "Hunt.json",
        ctx,
        ("press",),
        (operation(ctx, source=EvidenceSource.SIMULATION),),
        tuple(replace(item, source=EvidenceSource.SIMULATION) for item in alignment(ctx)),
        (capture_check(ctx, EvidenceSource.SIMULATION),),
    )
    assert profile.operation_outcome("press") is Outcome.UNVERIFIED
    assert profile.alignment_outcome() is Outcome.UNVERIFIED
    assert profile.capture_outcome() is Outcome.UNVERIFIED
    assert not validate_profile_context(profile, ctx, ("press",)).allowed


def test_alignment_requires_four_distributed_quadrants_and_center_and_in_bounds():
    ctx = context()
    diagonal = tuple(
        AlignmentEvidence(
            ctx.fingerprint, NOW, point, point, Outcome.PASS, EvidenceSource.LIVE
        )
        for point in ((0, 0), (799, 599), (400, 300), (300, 200), (500, 400))
    )
    profile = CompatibilityProfile("Hunt.json", ctx, ("press",), (), diagonal)
    assert profile.alignment_outcome() is Outcome.UNVERIFIED
    with pytest.raises(CompatibilityError, match="outside"):
        CompatibilityProfile(
            "Hunt.json",
            ctx,
            ("press",),
            (),
            (AlignmentEvidence(
                ctx.fingerprint, NOW, (800, 0), (800, 0), Outcome.PASS,
                EvidenceSource.LIVE,
            ),),
        )


def test_cursor_restore_requires_physical_telemetry_or_separate_operator_attestation():
    ctx = context()
    base = verified_profile("cursor_restore")
    assert base.operation_outcome("cursor_restore") is Outcome.UNVERIFIED
    attested = replace(
        base.evidence[0], cursor_restore_confirmation=Outcome.PASS
    )
    profile = CompatibilityProfile(
        base.calibration_profile, ctx, base.declared_operations, (attested,),
        base.alignment_evidence, base.capture_checks,
    )
    assert profile.operation_outcome("cursor_restore") is Outcome.PASS
    message_context = replace(ctx, environment=replace(ctx.environment, input_mode="messages"))
    message_evidence = replace(attested, context_fingerprint=message_context.fingerprint)
    messages = CompatibilityProfile(
        "Hunt.json", message_context, ("cursor_restore",), (message_evidence,),
        tuple(replace(item, context_fingerprint=message_context.fingerprint)
              for item in base.alignment_evidence),
        (replace(base.capture_checks[0], context_fingerprint=message_context.fingerprint),),
    )
    assert messages.operation_outcome("cursor_restore") is Outcome.UNVERIFIED


def test_cursor_telemetry_is_recorded_as_automatic_confirmation():
    probe = ProbeFeature(
        "cursor_restore",
        Action(ActionKind.LEFT_CLICK, point=(10, 20)),
        evidence_source=EvidenceSource.LIVE,
    )
    probe.tick(frame_context())
    probe.on_dispatch_metadata(cursor_original=(3, 4), cursor_final=(3, 4))
    probe.on_result(ActionResult(
        "probe:1", probe.name, "compatibility-probe", ResultState.EXECUTED, 10.1
    ))
    probe.tick(frame_context(2))
    item = probe.evidence(context())
    assert item.automatic_confirmation is Outcome.PASS
    profile = CompatibilityProfile(
        "Hunt.json", context(), ("cursor_restore",), (item,),
        alignment(context()), (capture_check(context()),),
    )
    assert profile.operation_outcome("cursor_restore") is Outcome.PASS


def test_signed_cursor_telemetry_round_trips_when_cursor_starts_outside_client(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(compatibility, "scripts_path", lambda name: tmp_path / str(name))
    ctx = context()
    probe = ProbeFeature(
        "cursor_restore", Action(ActionKind.LEFT_CLICK, point=(10, 20)),
        evidence_source=EvidenceSource.LIVE,
    )
    probe.tick(frame_context())
    probe.on_dispatch_metadata(cursor_original=(-12, 4), cursor_final=(-12, 4))
    probe.on_result(ActionResult(
        "probe:1", probe.name, "compatibility-probe", ResultState.EXECUTED, 10.1
    ))
    item = probe.evidence(ctx)
    assert item.automatic_confirmation is Outcome.PASS
    profile = CompatibilityProfile(
        "Hunt.json", ctx, ("cursor_restore",), (item,), alignment(ctx),
        (capture_check(ctx),),
    )
    save_profile(profile, "signed.json")
    loaded = load_profile("signed.json")
    assert loaded.evidence[0].cursor_original == (-12, 4)
    assert loaded.operation_outcome("cursor_restore") is Outcome.PASS


def frame_context(frame_id=1):
    sensor = Sensor(SensorKind.FRAME_VALID)
    observation = Observation(sensor, True, 10.0, frame_id, 1)
    return FeatureContext(Snapshot({sensor: observation}, frame_id, 1, 10.0), 10.0)


def test_mouse_probe_emits_exactly_one_request_with_fresh_frame_requirement_and_evidence():
    probe = ProbeFeature(
        "left_click",
        Action(ActionKind.LEFT_CLICK, point=(10, 20)),
        evidence_source=EvidenceSource.LIVE,
        intended_target="red test square",
    )
    first = probe.tick(frame_context())
    assert len(first.requests) == 1
    assert first.requests[0].action.point == (10, 20)
    assert first.requests[0].requirements[0].sensor.kind is SensorKind.FRAME_VALID
    probe.on_result(ActionResult(
        "probe:1", probe.name, "compatibility-probe", ResultState.EXECUTED, 10.1
    ))
    second = probe.tick(frame_context(2))
    assert second.finished
    assert not second.requests
    evidence = probe.evidence(context(), operator_confirmation=Outcome.PASS)
    assert evidence.outcome is Outcome.PASS
    assert evidence.point == (10, 20)
    assert evidence.intended_target == "red test square"
    assert evidence.before and evidence.after


def test_cancel_or_reject_before_dispatch_stays_unverified_while_native_failure_is_fail():
    for state in (ResultState.CANCELLED, ResultState.REJECTED):
        probe = ProbeFeature("press", Action(ActionKind.PRESS, key="F12"),
                             evidence_source=EvidenceSource.LIVE)
        probe.on_result(ActionResult("probe:1", probe.name, "compatibility-probe", state, 10.0))
        assert probe.evidence(context()).dispatch is Outcome.UNVERIFIED
    failed = ProbeFeature("press", Action(ActionKind.PRESS, key="F12"),
                          evidence_source=EvidenceSource.LIVE)
    failed.on_result(ActionResult(
        "probe:1", failed.name, "compatibility-probe", ResultState.FAILED, 10.0
    ))
    assert failed.evidence(context()).dispatch is Outcome.FAIL


def test_capture_preview_probe_has_no_operation_and_no_native_request():
    preview = ProbeFeature("capture_preview", None)
    assert preview.required_operations() == ()
    step = preview.tick(frame_context())
    assert step.finished
    assert step.requests == ()
    with pytest.raises(CompatibilityError, match="does not create action evidence"):
        preview.evidence(context())


def test_profile_storage_is_bounded_atomic_and_non_overwriting(tmp_path, monkeypatch):
    monkeypatch.setattr(compatibility, "scripts_path", lambda name: tmp_path / str(name))
    profile = verified_profile()
    path = save_profile(profile, "profile.json")
    assert load_profile("profile.json") == profile
    with pytest.raises(FileExistsError):
        save_profile(profile, "profile.json")
    path.write_bytes(b"{" + b"x" * compatibility.MAX_PROFILE_BYTES)
    with pytest.raises(CompatibilityError, match="exceeds"):
        load_profile("profile.json")


def test_profile_reader_rejects_duplicate_keys_and_boolean_schema_version(tmp_path, monkeypatch):
    monkeypatch.setattr(compatibility, "scripts_path", lambda name: tmp_path / str(name))
    path = save_profile(verified_profile(), "profile.json")
    encoded = path.read_text(encoding="utf-8")
    path.write_text(encoded.replace('"version": 1', '"version": 1, "version": 1'), encoding="utf-8")
    with pytest.raises(CompatibilityError, match="duplicate JSON key"):
        load_profile("profile.json")
    data = verified_profile().to_dict()
    data["version"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CompatibilityError, match="schema/version"):
        load_profile("profile.json")


def test_export_keeps_frames_separate_from_trace_and_never_overwrites(tmp_path):
    from PIL import Image
    from tibia_client.trace import JsonlRecorder

    frame = tmp_path / "before.png"
    Image.new("RGB", (2, 2), "red").save(frame)
    trace = tmp_path / "trace.jsonl"
    JsonlRecorder(trace).close()
    target = export_evidence_bundle(
        verified_profile(), tmp_path / "bundle", frame_files=(frame,), trace_file=trace
    )
    manifest = json.loads((target / "evidence.json").read_text(encoding="utf-8"))
    assert manifest["frames"] == ["frames/before.png"]
    assert manifest["trace"] == "trace-v1.jsonl"
    assert "frame" not in manifest["profile"]
    with pytest.raises(FileExistsError):
        export_evidence_bundle(verified_profile(), target)


def test_export_rejects_non_trace_data_and_removes_partial_destination(tmp_path):
    from tibia_client.trace import TraceError

    bad = tmp_path / "not-trace.jsonl"
    bad.write_text('{"version": 1}\n', encoding="utf-8")
    target = tmp_path / "bundle"
    with pytest.raises(TraceError, match="trace|schema"):
        export_evidence_bundle(verified_profile(), target, trace_file=bad)
    assert not target.exists()
