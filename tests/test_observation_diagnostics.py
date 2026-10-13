"""Visual diagnostics survive the observation, inspection and replay boundaries."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from tibia_client import (
    ClientSession, Event, Observation, ObservationBatch, Quality, Reading,
    Sensor, SensorKind, Snapshot, Step,
)
from tibia_client.hunt_workflow import HuntWorkflowController
from tibia_client.trace import JsonlRecorder, ReplayAdapter, ReplayClock, load_trace


SENSOR = Sensor(SensorKind.VISUAL, (0, 0, 20, 20), "inventory_context")


def test_diagnostics_survive_session_inspection_and_trace_replay(tmp_path):
    diagnostics = {
        "type": "visual_ambiguity", "scope": "context",
        "boxes": [[1, 1, 4, 4], [3, 1, 6, 4]], "scores": [1.0, 1.0],
    }
    reading = Reading(None, Quality.UNKNOWN, "two context matches", diagnostics)
    diagnostics["boxes"][0][0] = 99
    clock = ReplayClock(10)

    class Adapter:
        def ready(self):
            return True

        def observe(self, sensors, token):
            token.checkpoint()
            return ObservationBatch({SENSOR: reading}, clock.monotonic())

        def execute(self, action, token):
            raise AssertionError("inspection cannot issue input")

        def close(self):
            pass

    class Feature:
        name = "Diagnostics"
        interval = 0.1
        observed = None

        def sensors(self):
            return (SENSOR,)

        def tick(self, context):
            self.observed = context.snapshot.observation(SENSOR)
            return Step(finished=True)

        def on_result(self, result):
            pass

    feature = Feature()
    path = tmp_path / "diagnostics.jsonl"
    with JsonlRecorder(path) as recorder:
        with ClientSession(Adapter(), clock=clock, recorder=recorder, autostart=False) as session:
            session.start_feature(feature)
            session.tick_once()
    observed = feature.observed
    assert observed.quality is Quality.UNKNOWN and observed.value is None
    assert observed.diagnostics["boxes"][0] == (1, 1, 4, 4)
    with pytest.raises(TypeError):
        observed.diagnostics["scope"] = "target"
    inspected = HuntWorkflowController._reading(observed)
    assert inspected["diagnostics"] == reading.diagnostics
    assert inspected["quality"] == "unknown"

    snapshots = [event.data["snapshot"] for event in load_trace(path) if event.kind == "observation"]
    adapter = ReplayAdapter(snapshots, ReplayClock())
    replayed = adapter.observe((SENSOR,), SimpleNamespace(checkpoint=lambda: None))
    assert replayed.readings[SENSOR] == reading


def test_version_one_traces_without_diagnostics_still_load(tmp_path):
    observation = Observation(SENSOR, None, 1.0, 1, 1, Quality.UNKNOWN, "no match")
    snapshot = Snapshot({SENSOR: observation}, 1, 1, 1.0)
    event = Event(1, 1.0, "observation", data={"snapshot": snapshot, "reading": Reading(0)})
    path = tmp_path / "legacy.jsonl"
    with JsonlRecorder(path) as recorder:
        recorder.record(event)

    def remove_diagnostics(value):
        if isinstance(value, dict):
            if value.get("type") in {"Reading", "Observation"}:
                value["fields"].pop("diagnostics", None)
            for child in value.values():
                remove_diagnostics(child)
        elif isinstance(value, list):
            for child in value:
                remove_diagnostics(child)

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    for line in lines:
        remove_diagnostics(line)
    payload = "".join(json.dumps(line) + "\n" for line in lines[1:-1]).encode("utf-8")
    lines[-1]["sha256"] = hashlib.sha256(payload).hexdigest()
    path.write_bytes(
        (json.dumps(lines[0]) + "\n").encode("utf-8") + payload
        + (json.dumps(lines[-1]) + "\n").encode("utf-8")
    )
    loaded = load_trace(path)[0]
    assert loaded.data["snapshot"].observation(SENSOR) == observation
    assert loaded.data["reading"] == Reading(0)


@pytest.mark.parametrize("factory", [
    lambda value: Reading(diagnostics=value),
    lambda value: Observation(SENSOR, None, 0.0, 0, 0, diagnostics=value),
])
def test_diagnostics_reject_non_mapping_values(factory):
    with pytest.raises(ValueError, match="diagnostics must be a mapping"):
        factory([])
