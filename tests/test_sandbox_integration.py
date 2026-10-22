"""Sandbox composition tests; native capture and input are never invoked."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from PIL import Image, ImageDraw
import pytest

import conf.conf_manager as manager_module
from conf.conf_manager import ConfManager
from conf.Hotkeys import Hotkey
from core.Getters import CalibrationError
from core.perception import CaptureError
from modules.ChooseConfig import prepare_profile
from test_startup import valid_profile
from tibia_client import compatibility as compat
from tibia_client import window_target as targets
from tibia_client import direct_capture as direct
from tibia_client.desktop_profile import DesktopProfileController


@pytest.fixture
def sandbox_target():
    size = (1000, 720)
    active = Image.new("RGB", size, (20, 90, 170))
    inactive = Image.new("RGB", size, (70, 70, 70))
    for image in (active, inactive):
        draw = ImageDraw.Draw(image)
        for x in range(115, 445, 20):
            draw.rectangle((x, 24, x + 5, 39), fill=(235, 235, 235))
    guard = targets.make_focus_guard(active, inactive, (110, 20, 450, 44))
    return targets.WindowTarget.build(size, (100, 80, 900, 680), guard)


@pytest.fixture
def config_store(tmp_path, monkeypatch):
    # Read the shipped template before redirecting every configuration access.
    profile = valid_profile()
    profile.update(calibration_size=[800, 600], MouseOption=1, ItemsMode="Frames")
    monkeypatch.setattr(manager_module, "scripts_path", lambda name: tmp_path / str(name))
    monkeypatch.setattr(compat, "scripts_path", lambda name: tmp_path / str(name))
    monkeypatch.setattr(direct, "diagnose_client_availability", lambda hwnd: {"hwnd": hwnd})
    ConfManager.set(profile, "Saved.json")
    ConfManager.set({
        "configured": True, "platform": "windows", "preferences_name": "Saved.json",
        "hwnd": 41, "capture_hwnd": 41, "input_hwnd": 41,
        "capture_title": "Windows Sandbox", "input_title": "Windows Sandbox",
        "capture_size": [800, 600],
    }, "conf.json")
    return profile


def select_target(target):
    ConfManager.update("conf.json", lambda data: {**data, "window_target": target.to_dict()})


def bind_profile(target, profile):
    profile = deepcopy(profile)
    profile["window_target"] = target.to_dict()
    ConfManager.set(profile, "Saved.json")
    return profile


def shifted(target):
    left, top, right, bottom = target.viewport
    return targets.WindowTarget.build(
        target.host_size, (left + 1, top, right + 1, bottom), target.focus_guard
    )


def environment(**changes):
    data = dict(
        client_build="test", ot_server="test", windows_version="Windows 11",
        renderer="DirectX", obs_version="none", obs_configuration="none",
        dpi_scaling="physical pixels", layout="windowed guest", input_mode="physical",
        capture_mode="sandbox_screen", capture_title="Windows Sandbox",
        input_title="Windows Sandbox",
    )
    data.update(changes)
    return compat.EnvironmentMetadata(**data)


class CalibrationGetters:
    GetHealthPosition = staticmethod(lambda: (20, 20))
    GetManaPosition = staticmethod(lambda: (20, 30))
    GetBattlePosition = staticmethod(lambda: (300, 50, 450, 400))
    GetStatsPosition = staticmethod(lambda: (500, 200, 605, 210))
    GetMapPosition = staticmethod(lambda: (650, 50, 755, 158))
    GetPlayerPosition = staticmethod(lambda: (400, 300, 100, 50, 700, 550))
    SetSQMs = staticmethod(lambda *_: (
        360, 350, 400, 350, 440, 350, 360, 300, 400, 300,
        440, 300, 360, 250, 400, 250, 440, 250,
    ))


def test_sandbox_hotkey_factory_rejects_messages_before_native_construction(
    config_store, sandbox_target, monkeypatch
):
    import core.SendToClient as messages

    select_target(sandbox_target)
    monkeypatch.setattr(messages, "SendToClient", lambda **_: pytest.fail("message backend built"))
    with pytest.raises((ValueError, RuntimeError), match="[Ss]andbox|physical"):
        Hotkey(0)


def test_sandbox_hotkey_factory_wraps_physical_backend_with_full_host_extent(
    config_store, sandbox_target
):
    from core.MoveMouse import MoveMouse

    select_target(sandbox_target)
    hotkey = Hotkey(1)
    assert isinstance(hotkey.SendToClient, targets.TargetInput)
    assert hotkey.SendToClient.target == sandbox_target
    assert isinstance(hotkey.SendToClient.backend, MoveMouse)
    assert tuple(hotkey.SendToClient.backend._expected_size_resolver()) == sandbox_target.host_size
    # Constructing the facade must not load Windows APIs or acquire a frame.
    assert hotkey.SendToClient.backend._backend is None


def test_absent_local_target_keeps_existing_native_backend_selection(config_store):
    from core.MoveMouse import MoveMouse
    from core.SendToClient import SendToClient

    assert isinstance(Hotkey(1).SendToClient, MoveMouse)
    assert isinstance(Hotkey(0).SendToClient, SendToClient)


def test_configured_sandbox_capture_uses_viewport_backend_not_printwindow(
    config_store, sandbox_target, monkeypatch
):
    from core import perception

    select_target(sandbox_target)
    calls = []
    expected = Image.new("RGB", sandbox_target.size, (50, 60, 70))
    monkeypatch.setattr(direct, "capture_direct_target", lambda hwnd, target: (
        calls.append((hwnd, target)) or expected
    ))
    monkeypatch.setattr(perception, "_capture_windows", lambda _: pytest.fail("PrintWindow used"))
    assert perception._default_capture() is expected
    assert calls[0][0] == 41
    assert calls[0][1].viewport == sandbox_target.viewport
    assert calls[0][1].focus_guard is None


def test_sandbox_capture_rejects_different_capture_and_input_windows(
    config_store, sandbox_target, monkeypatch
):
    from core import perception

    select_target(sandbox_target)
    ConfManager.update("conf.json", lambda data: {**data, "input_hwnd": 42})
    monkeypatch.setattr(direct, "capture_direct_target", lambda *_: pytest.fail("invalid binding captured"))
    with pytest.raises(CaptureError, match="[Ss]andbox|same|match"):
        perception._default_capture()


def test_sandbox_capture_still_rejects_changed_viewport_pixel_extent(
    config_store, sandbox_target, monkeypatch
):
    from core import perception

    select_target(sandbox_target)
    monkeypatch.setattr(direct, "capture_direct_target", lambda *_: Image.new("RGB", (799, 600)))
    with pytest.raises(CaptureError, match="size changed|recalibrat"):
        perception._default_capture()


def test_local_capture_uses_direct_client_without_printwindow(config_store, monkeypatch):
    from core import perception

    expected = Image.new("RGB", (800, 600))
    calls = []
    monkeypatch.setattr(direct, "capture_direct_host_client", lambda hwnd: calls.append(hwnd) or expected)
    monkeypatch.setattr(perception, "_capture_windows", lambda _: pytest.fail("PrintWindow used"))
    monkeypatch.setattr(direct, "capture_direct_target", lambda *_: pytest.fail("Sandbox capture used"))
    assert perception._default_capture() is expected
    assert calls == [41]


def test_separate_obs_projector_capture_retains_existing_transport(config_store, monkeypatch):
    from core import perception

    ConfManager.update("conf.json", lambda data: {**data, "input_hwnd": 42})
    image = Image.new("RGB", (800, 600))
    monkeypatch.setattr(direct, "capture_direct_host_client", lambda *_: pytest.fail("direct capture"))
    monkeypatch.setattr(perception, "_capture_windows", lambda hwnd: image)
    assert perception._default_capture() is image


@pytest.mark.parametrize("old_kind", ["local", "sandbox"])
def test_same_size_target_change_requires_recalibration_and_preserves_saved_profile(
    config_store, sandbox_target, old_kind
):
    old = config_store
    requested = sandbox_target
    if old_kind == "sandbox":
        old = bind_profile(sandbox_target, config_store)
        requested = shifted(sandbox_target)
    before = ConfManager.path("Saved.json").read_bytes()
    with pytest.raises(CalibrationError, match="target|recalibrat"):
        prepare_profile("Saved.json", (800, 600), 1, "Frames",
                        window_target=requested.to_dict())
    assert ConfManager.path("Saved.json").read_bytes() == before
    assert ConfManager.get("Saved.json") == old


def test_explicit_recalibration_records_normalized_sandbox_target(
    config_store, sandbox_target
):
    built = prepare_profile(
        "Saved.json", (800, 600), 1, "Frames", recalibrate=True,
        getters=CalibrationGetters, window_target=sandbox_target.to_dict(),
    )
    assert built["window_target"] == sandbox_target.to_dict()
    assert built["calibration_size"] == [800, 600]
    assert "window_target" not in ConfManager.get("Saved.json")


def test_preparing_sandbox_profile_with_messages_is_rejected(config_store, sandbox_target):
    bind_profile(sandbox_target, config_store)
    with pytest.raises((CalibrationError, ValueError), match="physical|[Ss]andbox"):
        prepare_profile("Saved.json", (800, 600), 0, "Frames",
                        window_target=sandbox_target.to_dict())


def test_root_profile_loading_requires_exact_target_binding(config_store, sandbox_target):
    from modules.Root import load_profile

    bind_profile(sandbox_target, config_store)
    select_target(sandbox_target)
    assert load_profile("Saved.json")["window_target"] == sandbox_target.to_dict()
    select_target(shifted(sandbox_target))
    with pytest.raises(ValueError, match="target|recalibrat"):
        load_profile("Saved.json")


def test_root_profile_loading_rejects_sandbox_message_mode(config_store, sandbox_target):
    from modules.Root import load_profile

    profile = bind_profile(sandbox_target, config_store)
    profile["MouseOption"] = 0
    ConfManager.set(profile, "Saved.json")
    select_target(sandbox_target)
    with pytest.raises(ValueError, match="physical|[Ss]andbox"):
        load_profile("Saved.json")


def test_local_calibration_digest_is_backward_compatible(config_store):
    relevant = {key: config_store.get(key) for key in (
        "Stats", "calibration_size", "Positions", "Boxes", "SQM", "MouseOption", "ItemsMode",
    )}
    expected = hashlib.sha256(json.dumps(
        relevant, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")).hexdigest()
    assert compat.calibration_digest(config_store) == expected
    explicit_local = {**config_store, "window_target": {"kind": "local"}}
    assert compat.calibration_digest(explicit_local) == expected


def test_sandbox_target_geometry_and_focus_samples_bind_compatibility_digest(
    config_store, sandbox_target
):
    profile = {**config_store, "window_target": sandbox_target.to_dict()}
    first = compat.calibration_digest(profile)
    assert first != compat.calibration_digest(config_store)
    moved = {**profile, "window_target": shifted(sandbox_target).to_dict()}
    assert compat.calibration_digest(moved) != first
    changed_guard = sandbox_target.focus_guard
    changed_guard["active"], changed_guard["inactive"] = (
        changed_guard["inactive"], changed_guard["active"]
    )
    changed = targets.WindowTarget.build(
        sandbox_target.host_size, sandbox_target.viewport, changed_guard
    )
    assert compat.calibration_digest({**profile, "window_target": changed.to_dict()}) != first


def test_sandbox_context_rejects_message_input_before_evidence_is_created(
    config_store, sandbox_target
):
    profile = {**config_store, "MouseOption": 0, "window_target": sandbox_target.to_dict()}
    with pytest.raises((compat.CompatibilityError, ValueError), match="physical|[Ss]andbox"):
        compat.context_from_calibration(profile, environment(input_mode="messages"))


def test_standalone_compatibility_context_rejects_viewport_calibration_size_mismatch(
    config_store, sandbox_target
):
    left, top, right, bottom = sandbox_target.viewport
    smaller = targets.WindowTarget.build(
        sandbox_target.host_size, (left, top, right - 1, bottom), sandbox_target.focus_guard
    )
    profile = {**config_store, "window_target": smaller.to_dict()}
    with pytest.raises((compat.CompatibilityError, ValueError), match="size|viewport"):
        compat.context_from_calibration(profile, environment())


def test_profile_controller_requires_matching_target_and_sandbox_capture_metadata(
    config_store, sandbox_target
):
    bind_profile(sandbox_target, config_store)
    select_target(sandbox_target)
    gate = DesktopProfileController("Saved.json", input_size=lambda: (800, 600))
    assert gate.current_context(environment()).aligned
    with pytest.raises(ValueError, match="capture|[Ss]andbox"):
        gate.current_context(environment(capture_mode="direct_client"))
    select_target(shifted(sandbox_target))
    with pytest.raises(ValueError, match="target|recalibrat"):
        gate.current_context(environment())


def test_readings_monitor_pins_sandbox_target_but_explicit_hwnd_stays_local(
    config_store, sandbox_target, monkeypatch
):
    import tibia_client.capture as captures
    import tibia_client.resource_monitor as monitor

    select_target(sandbox_target)
    monkeypatch.setattr(monitor, "scripts_path", ConfManager.path)
    monkeypatch.setattr(captures, "CaptureProcess", lambda provider, **kw: (provider, kw))
    before = ConfManager.path("conf.json").read_bytes()
    source, options = monitor.create_capture()
    assert source.hwnd == 41
    assert source.target == sandbox_target
    assert options["capture_size"] == [800, 600]
    assert ConfManager.path("conf.json").read_bytes() == before
    select_target(shifted(sandbox_target))
    calls = []
    expected = Image.new("RGB", sandbox_target.size)
    monkeypatch.setattr(direct, "capture_direct_target", lambda hwnd, target: (
        calls.append((hwnd, target)) or expected
    ))
    assert source.capture() is expected
    assert calls == [(41, sandbox_target)]
    # Capture construction and capture itself do not modify selected settings.
    assert json.loads(before)["window_target"] == sandbox_target.to_dict()
    after = ConfManager.path("conf.json").read_bytes()
    assert json.loads(after)["window_target"] == shifted(sandbox_target).to_dict()
    explicit, options = monitor.create_capture(42)
    assert explicit.hwnd == 42
    assert explicit.target is None or explicit.target.kind == "local"
    assert options["capture_size"] is None
    assert ConfManager.path("conf.json").read_bytes() == after
