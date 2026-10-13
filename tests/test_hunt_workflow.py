import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from PIL import Image

from engine.CaveBot.routes import Route, Waypoint
from modules.HuntWorkflow import default_workflow_document
from tibia_client.diagnostics import AdapterCapabilities
from tibia_client.models import FeatureStatus, Observation, Quality, SensorKind
import tibia_client.hunt_workflow as workflow


class FakeVisualSpec:
    def __init__(self, kind):
        self.kind = kind
        self.context_region = (0, 0, 2, 2)
        self.search_region = (2, 0, 8, 4)


class FakeVisualProfile(dict):
    @classmethod
    def from_data(cls, raw, *, base_path=None):
        result = cls((key, FakeVisualSpec(value["kind"])) for key, value in raw.items())
        result.fingerprint = json.dumps(raw, sort_keys=True)
        result.base_path = Path(base_path)
        return result


class ProfileController:
    def __init__(self):
        self.context = SimpleNamespace(
            fingerprint="live-context",
            calibration_digest=workflow.calibration_digest(calibration()),
        )
        self.capabilities = None
        self.token = object()

    def set_adapter_capabilities(self, capabilities):
        self.capabilities = capabilities

    def begin_probe(self):
        return self.token

    def validate_probe_token(self, token):
        return token is self.token


class Session:
    def __init__(self):
        self.control_epoch = 4
        self.active = ()
        self.paused = ()
        self.calls = []
        self.statuses = {
            "CaveBot": FeatureStatus("CaveBot", 0, False, "Stopped"),
            "CompatibilityProbe": FeatureStatus("CompatibilityProbe", 0, False, "Stopped"),
        }

    def diagnostics(self):
        return SimpleNamespace(
            active_features=self.active,
            paused_features=self.paused,
            capabilities=AdapterCapabilities(
                ("left_click", "right_click", "drag", "battle_list_click"),
                ("visual",),
            ),
        )

    def configure_visual_profile(self, profile, *, expected_stop_epoch):
        self.calls.append(("configure", profile, expected_stop_epoch))
        if expected_stop_epoch != self.control_epoch:
            raise RuntimeError("client controls changed while preparing")
        self.control_epoch += 1
        return self.control_epoch

    def start_feature(self, feature, *, expected_stop_epoch):
        self.calls.append(("start", feature, expected_stop_epoch))
        assert expected_stop_epoch == self.control_epoch
        self.statuses["CaveBot"] = FeatureStatus("CaveBot", 7, True, "Starting")
        self.active = ("CaveBot",)
        return 7

    def start_probe(self, probe, *, expected_stop_epoch):
        self.calls.append(("probe", probe, expected_stop_epoch))
        assert expected_stop_epoch == self.control_epoch
        observations = {}
        for sensor in probe.sensors():
            if sensor.kind is SensorKind.VISUAL:
                observations[sensor] = Observation(
                    sensor,
                    {"present": True, "position": (4, 4), "count": 9},
                    1.0,
                    1,
                    1,
                    Quality.KNOWN,
                )
        probe.observations = MappingProxyType(observations)
        self.statuses["CompatibilityProbe"] = FeatureStatus(
            "CompatibilityProbe", 8, False, "Fresh capture preview ready"
        )
        return 8

    def stop_feature(self, name, *, expected_generation=None):
        self.calls.append(("stop", name, expected_generation))
        current = self.statuses[name]
        if current.generation != expected_generation:
            return False
        self.statuses[name] = FeatureStatus(name, current.generation, False, "Stopped")
        self.active = ()
        return True

    def feature_status(self, name):
        return self.statuses[name]

    def feature_command(self, name, generation, command, **options):
        self.calls.append(("command", name, generation, command, options))
        return self.statuses[name].generation == generation

    def preview_frame(self, *, expected_frame_id=None):
        if expected_frame_id != 1:
            return None
        return "captured-frame"


def calibration():
    result = {
        "Stats": True,
        "calibration_size": [200, 100],
        "Positions": {
            name: [{"Stats": True, "x": 20 + index, "y": 20 + index}]
            for index, name in enumerate(
                ("LifePosition", "ManaPosition", "BattlePosition", "PlayerPosition")
            )
        },
        "Boxes": {
            "MapBox": [{"Stats": True, "x": 10, "y": 10, "w": 60, "h": 60}],
            "BattleBox": [{"Stats": True, "x": 70, "y": 10, "w": 150, "h": 90}],
            "StatusBarBox": [{"Stats": True, "x": 10, "y": 61, "w": 60, "h": 70}],
            "RingBox": [{"Stats": True, "x": 61, "y": 61, "w": 70, "h": 70}],
            "AmuletBox": [{"Stats": True, "x": 71, "y": 61, "w": 80, "h": 70}],
            "GameWindowBox": [{"Stats": True, "x": 1, "y": 1, "w": 199, "h": 99}],
        },
        "SQM": {
            f"SQM{number}": [{"Stats": True, "x": 80 + number, "y": 40}] for number in range(1, 10)
        },
    }
    return result


def route(name):
    return Route(name, (Waypoint(f"{name}_one", "Flag"),))


def document():
    value = default_workflow_document()
    value["plan"].pop("loot")
    value["bindings"] = {
        "mana_potions": {"region": [0, 0, 20, 10], "name": "supply_count"},
    }
    value["visual"] = {"supply_count": {"kind": "count"}}
    value["targets"] = [{"name": "Rat", "priority": 1}]
    value["navigation"] = {"follow": False, "poll_interval": 0.1}
    return value


@pytest.fixture
def controller(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow, "VisualProfile", FakeVisualProfile)
    monkeypatch.setattr(workflow.ConfManager, "get", lambda _name: calibration())
    monkeypatch.setattr(workflow, "load_route", lambda name: route(name))
    monkeypatch.setattr(workflow, "scripts_path", lambda name: tmp_path / name)
    session = Session()
    profiles = ProfileController()
    return workflow.HuntWorkflowController(session, "calibration.json", profiles), session, profiles


def test_validate_returns_readonly_fully_resolved_value(controller, tmp_path):
    service, _session, _profiles = controller
    compiled = service.validate(document(), tmp_path)

    assert compiled.plan.id == "my_hunt"
    assert compiled.map_region == (10, 10, 60, 60)
    assert compiled.battle_region == (70, 10, 150, 90)
    assert compiled.targets[0].name == "Rat"
    assert compiled.navigation.poll_interval == 0.1
    assert compiled.bindings["mana_potions"].name == "supply_count"
    assert compiled.checkpoint_path.name == "hunt-my_hunt.state.json"
    with pytest.raises(TypeError):
        compiled.bindings["new"] = compiled.bindings["mana_potions"]
    with pytest.raises(TypeError):
        compiled.document["version"] = 2


def test_binding_revision_uses_current_context_calibration_visual_and_options(controller, tmp_path):
    service, _session, profiles = controller
    first = service.validate(document(), tmp_path).binding_revision

    changed = document()
    changed["navigation"]["poll_interval"] = 0.3
    assert service.validate(changed, tmp_path).binding_revision != first
    profiles.context.fingerprint = "other-context"
    assert service.validate(document(), tmp_path).binding_revision != first


def test_start_installs_profile_at_one_epoch_and_routes_commands(controller, tmp_path):
    service, session, profiles = controller
    compiled = service.start(document(), tmp_path, resume=False)

    assert [item[0] for item in session.calls[:2]] == ["configure", "start"]
    assert session.calls[0][2] == 4
    assert session.calls[1][2] == 5
    assert session.calls[1][1].binding_revision == compiled.binding_revision
    assert session.calls[1][1].desktop_context_fingerprint == compiled.context_fingerprint
    assert profiles.capabilities.sensors == ("visual",)
    status = service.status()
    assert status["active"] and status["generation"] == 7 and not status["paused"]

    service.request_return(now=True)
    service.acknowledge_manual(operator_reconciled=False)
    assert session.calls[-2][-1] == {"now": True}
    assert session.calls[-1][-1] == {"operator_reconciled": False}
    assert service.stop()
    assert not service.status()["active"]


def test_paused_original_is_active_but_replacement_cannot_be_controlled(controller, tmp_path):
    service, session, _profiles = controller
    service.start(document(), tmp_path, resume=False)
    session.active = ()
    session.paused = ("CaveBot",)
    session.statuses["CaveBot"] = FeatureStatus("CaveBot", 7, False, "Paused")
    assert service.status()["active"]
    assert service.status()["paused"]

    session.paused = ()
    session.active = ("CaveBot",)
    session.statuses["CaveBot"] = FeatureStatus("CaveBot", 8, True, "Starting")
    assert not service.status()["active"]
    with pytest.raises(RuntimeError, match="original hunt workflow generation"):
        service.request_return()
    assert not service.stop()


@pytest.mark.parametrize("paused", [False, True])
def test_start_refuses_to_replace_any_active_or_paused_feature(controller, tmp_path, paused):
    service, session, _profiles = controller
    if paused:
        session.paused = ("AutoHeal",)
    else:
        session.active = ("AutoHeal",)
    with pytest.raises(RuntimeError, match="AutoHeal"):
        service.start(document(), tmp_path)
    assert not session.calls


def test_inspection_compiles_only_visual_bindings_and_is_capture_only(controller, tmp_path):
    service, session, _profiles = controller
    incomplete = document()
    incomplete["plan"] = {"unfinished": True}
    compiled = service.inspect(incomplete, tmp_path)

    assert set(compiled.bindings) == {"mana_potions"}
    probe = session.calls[-1][1]
    assert probe.action is None
    assert probe.required_operations() == ()
    assert probe.probe_context_check()
    observed = service.inspection_status()
    assert not observed["active"]
    assert observed["readings"]["mana_potions"]["quality"] == "known"
    assert observed["readings"]["mana_potions"]["value"]["count"] == 9
    assert observed["frame"] == "captured-frame"
    assert observed["bindings"]["mana_potions"] == {
        "region": (0, 0, 20, 10),
        "name": "supply_count",
    }


def test_strict_wrapper_count_binding_and_bounded_atomic_files(controller, tmp_path):
    service, _session, _profiles = controller
    invalid = document()
    invalid["extra"] = True
    with pytest.raises(workflow.HuntWorkflowError, match="unknown field"):
        service.validate(invalid, tmp_path)

    invalid = document()
    invalid["visual"]["supply_count"]["kind"] = "template"
    with pytest.raises(workflow.HuntWorkflowError, match="count visual"):
        service.validate(invalid, tmp_path)

    filename = tmp_path / "workflow.json"
    assert service.save(filename, document()) == filename
    assert service.load(filename) == document()
    filename.write_text('{"schema": 1, "schema": 2}', encoding="utf-8")
    with pytest.raises(workflow.HuntWorkflowError, match="duplicate JSON key"):
        service.load(filename)
    filename.write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(workflow.HuntWorkflowError, match="non-finite"):
        service.load(filename)
    invalid_number = document()
    invalid_number["navigation"]["poll_interval"] = float("nan")
    with pytest.raises(workflow.HuntWorkflowError, match="not JSON serializable"):
        service.save(filename, invalid_number)


def test_offline_validate_needs_no_live_context_but_start_does(controller, tmp_path):
    service, session, profiles = controller
    profiles.context = None
    assert service.validate(document(), tmp_path).binding_revision
    with pytest.raises(RuntimeError, match="Compatibility profile"):
        service.start(document(), tmp_path)
    assert not session.calls


def test_control_epoch_is_captured_before_expensive_validation(controller, tmp_path, monkeypatch):
    service, session, _profiles = controller
    original = service.validate

    def raced(data, base_dir):
        compiled = original(data, base_dir)
        session.control_epoch += 1  # Concurrent Stop All during compilation.
        return compiled

    monkeypatch.setattr(service, "validate", raced)
    with pytest.raises(RuntimeError, match="controls changed"):
        service.start(document(), tmp_path)
    assert [item[0] for item in session.calls] == ["configure"]


def test_ui_epoch_and_stop_cancel_work_before_detector_install(controller, tmp_path, monkeypatch):
    service, session, _profiles = controller
    token = service.control_epoch
    assert token.session_epoch == 4 and token.operation_revision == 0
    with pytest.raises(RuntimeError, match="controls changed before"):
        service.start(
            document(),
            tmp_path,
            expected_stop_epoch=workflow.WorkflowControlToken(3, 0),
        )

    original = service.validate

    def cancelled(data, base_dir):
        compiled = original(data, base_dir)
        assert not service.stop()
        return compiled

    monkeypatch.setattr(service, "validate", cancelled)
    with pytest.raises(RuntimeError, match="cancelled while preparing"):
        service.start(document(), tmp_path, expected_stop_epoch=token)
    assert not session.calls


def test_context_and_calibration_are_bound_through_activation(controller, tmp_path, monkeypatch):
    service, session, profiles = controller
    profiles.context.calibration_digest = "different-calibration"
    with pytest.raises(workflow.HuntWorkflowError, match="different calibration"):
        service.validate(document(), tmp_path)

    profiles.context.calibration_digest = workflow.calibration_digest(calibration())
    original = session.configure_visual_profile

    def changed_context(profile, *, expected_stop_epoch):
        result = original(profile, expected_stop_epoch=expected_stop_epoch)
        profiles.context.fingerprint = "replacement-context"
        return result

    monkeypatch.setattr(session, "configure_visual_profile", changed_context)
    with pytest.raises(RuntimeError, match="context changed"):
        service.start(document(), tmp_path)
    assert [item[0] for item in session.calls] == ["configure"]


def test_failed_and_replaced_inspections_publish_safe_status(controller, tmp_path, monkeypatch):
    service, session, profiles = controller
    monkeypatch.setattr(profiles, "validate_probe_token", lambda _token: False)
    with pytest.raises(RuntimeError, match="token"):
        service.inspect(document(), tmp_path)
    failed = service.inspection_status()
    assert not failed["active"] and failed["frame"] is None
    assert "token" in failed["error"]
    assert failed["bindings"]["mana_potions"]["name"] == "supply_count"

    monkeypatch.setattr(profiles, "validate_probe_token", lambda token: token is profiles.token)
    service.inspect(document(), tmp_path)
    session.statuses["CompatibilityProbe"] = FeatureStatus(
        "CompatibilityProbe", 9, True, "replacement"
    )
    replaced = service.inspection_status()
    assert not replaced["active"]
    assert replaced["readings"] == {}
    assert replaced["frame"] is None
    assert "replaced" in replaced["error"]


def test_inspection_compile_failure_clears_old_evidence(controller, tmp_path):
    service, _session, _profiles = controller
    service.inspect(document(), tmp_path)
    invalid = document()
    invalid["bindings"]["mana_potions"]["name"] = "missing"
    with pytest.raises(workflow.HuntWorkflowError, match="unknown visual spec"):
        service.inspect(invalid, tmp_path)
    failed = service.inspection_status()
    assert failed["readings"] == {}
    assert failed["bindings"] == {}
    assert failed["frame"] is None
    assert "unknown visual spec" in failed["error"]


def test_real_visual_profile_and_real_calibration_validation(monkeypatch, tmp_path):
    context_file = tmp_path / "context.png"
    target_file = tmp_path / "target.png"
    context_image = Image.new("RGB", (3, 3))
    context_image.putdata(
        [
            (0, 0, 0),
            (255, 0, 0),
            (0, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
            (255, 255, 0),
            (0, 0, 0),
            (255, 0, 255),
            (0, 0, 0),
        ]
    )
    context_image.save(context_file)
    target_image = Image.new("RGB", (2, 2))
    target_image.putdata([(0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 0, 255)])
    target_image.save(target_file)

    data = default_workflow_document()
    data["plan"] = {"unfinished": True}
    data["targets"] = []
    data["navigation"] = {}
    data["bindings"] = {"visible_slot": {"region": [0, 0, 20, 10], "name": "slot_detector"}}
    data["visual"] = {
        "slot_detector": {
            "kind": "template",
            "context": {"template": context_file.name, "region": [0, 0, 3, 3]},
            "template": target_file.name,
            "search_region": [3, 0, 8, 4],
        }
    }
    monkeypatch.setattr(workflow.ConfManager, "get", lambda _name: calibration())
    service = workflow.HuntWorkflowController(Session(), "calibration.json", ProfileController())
    compiled = service.inspect(data, tmp_path)

    assert compiled.visual_profile.spec_ids == ("slot_detector",)
    assert compiled.bindings["visible_slot"].region == (0, 0, 20, 10)
    assert compiled.binding_revision
