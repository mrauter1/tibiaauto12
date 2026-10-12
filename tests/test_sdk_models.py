from __future__ import annotations

from types import MappingProxyType

import pytest

from tibia_client.models import (
    Action,
    ActionKind,
    ActionResult,
    Confirmation,
    Event,
    FeatureContext,
    FeatureStatus,
    Observation,
    ObservationBatch,
    Reading,
    Requirement,
    ResultState,
    Sensor,
    SensorKind,
    Snapshot,
    Step,
)


HEALTH = Sensor(SensorKind.HEALTH, (1, 2))


def test_observation_batch_rejects_malformed_adapter_values():
    with pytest.raises(ValueError, match="Sensor values to Reading"):
        ObservationBatch({HEALTH: object()}, 1.0)
    with pytest.raises(ValueError, match="Sensor values to Reading"):
        ObservationBatch({"health": Reading(50)}, 1.0)


def test_public_mapping_values_are_copied_and_recursively_frozen():
    nested = {"items": [1, {"enabled": True}]}
    event = Event(1, 2.0, "decision", data=nested)
    nested["items"].append(3)

    assert isinstance(event.data, MappingProxyType)
    assert event.data["items"] == (1, {"enabled": True})
    assert isinstance(event.data["items"][1], MappingProxyType)
    with pytest.raises(TypeError):
        event.data["items"][1]["enabled"] = False


@pytest.mark.parametrize(
    "action, message",
    [
        (ActionKind.PRESS, "press action cannot contain mouse coordinates"),
        (ActionKind.LEFT_CLICK, "click action cannot contain a destination"),
        (ActionKind.RIGHT_CLICK, "mouse action cannot contain a key"),
    ],
)
def test_actions_reject_ambiguous_unused_fields(action, message):
    kwargs = {"key": "F1", "point": (1, 2), "destination": (3, 4)}
    if action is ActionKind.RIGHT_CLICK:
        kwargs.pop("destination")
    with pytest.raises(ValueError, match=message):
        Action(action, **kwargs)


def test_snapshot_rejects_inconsistent_or_future_observations():
    observation = Observation(HEALTH, 50, 2.0, 2, 1)
    with pytest.raises(ValueError, match="inconsistent observation"):
        Snapshot({HEALTH: observation}, frame_id=1, epoch=1, created_at=2.0)
    with pytest.raises(ValueError, match="inconsistent observation"):
        Snapshot({HEALTH: observation}, frame_id=2, epoch=1, created_at=1.0)


def test_result_step_context_status_and_event_validate_wire_fields():
    result = ActionResult("id", "owner", "key", "executed", 1.0)
    assert result.state is ResultState.EXECUTED
    with pytest.raises(ValueError, match="final must be boolean"):
        ActionResult("id", "owner", "key", ResultState.EXECUTED, 1.0, final=1)
    with pytest.raises(ValueError, match="executed result may be non-final"):
        ActionResult("id", "owner", "key", ResultState.CONFIRMED, 1.0, final=False)
    with pytest.raises(ValueError, match="phase must be non-empty"):
        Step(phase="")
    with pytest.raises(ValueError, match="timestamp"):
        FeatureContext(Snapshot(), float("nan"))
    with pytest.raises(ValueError, match="active must be boolean"):
        FeatureStatus("feature", 1, 1, "Running")
    with pytest.raises(ValueError, match="keys must be text"):
        Event(1, 1.0, "event", data={1: "bad"})


def test_requirements_reject_non_sensor_values():
    with pytest.raises(ValueError, match="sensor must be a Sensor"):
        Requirement("health")


def test_malformed_coordinate_and_confirmation_types_raise_value_errors():
    with pytest.raises(ValueError, match="sensor coordinates"):
        Sensor(SensorKind.HEALTH, None)
    with pytest.raises(ValueError, match="action coordinates"):
        Action(ActionKind.LEFT_CLICK, point=1)
    with pytest.raises(ValueError, match="action key must be text"):
        Action(ActionKind.LEFT_CLICK, key=None, point=(1, 2))
    with pytest.raises(ValueError, match="confirmation field must be text"):
        Confirmation(HEALTH, 50, field=1)
