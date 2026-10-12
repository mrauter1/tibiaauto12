from __future__ import annotations

import json
import subprocess
import sys

import pytest

from engine.CaveBot.routes import Route, Waypoint
from engine.support import Threshold, TimedSpell
from features import CaveBot, HealFeature, SessionConfig, TargetSpec, TimedSpellsFeature
from tibia_client import (
    Action,
    ActionKind,
    ClientSession,
    Event,
    Marker,
    Observation,
    ObservationBatch,
    Quality,
    Reading,
    Sensor,
    SensorKind,
    Snapshot,
    Target,
)
from tibia_client.trace import (
    JsonlRecorder,
    ReplayAdapter,
    ReplayClock,
    TraceError,
    load_trace,
    replay_policies,
)


HEALTH = Sensor(SensorKind.HEALTH, (0, 0))


def factory(name, _generation):
    if name == "AutoHeal":
        return HealFeature(HEALTH, (Threshold(True, 50, "F1"),))
    return TimedSpellsFeature((TimedSpell("F2", 1),), immediate=True)


def record_session(path):
    clock = ReplayClock(10)

    class Adapter:
        def ready(self):
            return True

        def observe(self, sensors, token):
            return ObservationBatch({HEALTH: Reading(20)}, clock.monotonic())

        def execute(self, action, token):
            token.checkpoint()

        def close(self):
            pass

    with JsonlRecorder(path) as recorder:
        with ClientSession(Adapter(), clock=clock, recorder=recorder, autostart=False) as session:
            session.start_feature(factory("TimedSpells", 1))
            session.start_feature(factory("AutoHeal", 1))
            for at in (10, 10.1, 10.2, 10.4, 11.0):
                clock.advance_to(at)
                session.tick_once()
    return load_trace(path)


def test_trace_roundtrips_typed_events_and_replays_real_feature_feedback(tmp_path):
    events = record_session(tmp_path / "trace.jsonl")
    snapshots = [e.data["snapshot"] for e in events if e.kind == "observation"]
    assert snapshots[0].observation(HEALTH).value == 20
    assert snapshots[0].observation(HEALTH).quality is Quality.KNOWN
    actions = [e.data["request"].action for e in events if e.kind == "request"]
    assert Action(ActionKind.PRESS, key="F1") in actions
    report = replay_policies(events, factory)
    assert report.decisions == 10
    assert report.requests >= 3 and report.feedback >= 3
    with pytest.raises(TraceError, match="diverged"):
        replay_policies(
            events,
            lambda name, generation: (
                HealFeature(HEALTH, (Threshold(True, 10, "F9"),))
                if name == "AutoHeal"
                else factory(name, generation)
            ),
        )


def test_trace_replays_cavebot_combat_interruption_with_heal_and_restart(tmp_path):
    """Replay preserves a real cross-feature route/combat arbitration history."""
    path = tmp_path / "cavebot-heal.jsonl"
    clock = ReplayClock(10)
    route = Route("trace-hunt", (Waypoint("center", "CheckMark"),))
    map_region = (0, 0, 100, 100)
    battle_region = (100, 0, 250, 400)
    health = Sensor(SensorKind.HEALTH, (10, 10))
    config = SessionConfig(
        follow=False,
        loot=False,
        count_stable_samples=2,
        marker_timeout=2,
        arrival_timeout=2,
        combat_timeout=5,
    )

    def scenario_factory(name, _generation):
        if name == "CaveBot":
            return CaveBot(
                route,
                (TargetSpec(1, "dragon"),),
                config=config,
                save_progress=lambda _route, _index: None,
                map_region=map_region,
                battle_region=battle_region,
            )
        if name == "AutoHeal":
            return HealFeature(health, (Threshold(True, 50, "F1"),))
        raise AssertionError(f"unexpected feature {name}")

    frames = {
        10.0: (20, Marker((10, 10), False), Target(0)),
        10.2: (20, Marker((10, 10), False), Target(1, (20, 20), False)),
        10.4: (80, Marker((10, 10), False), Target(1, (20, 20), True)),
        10.6: (20, Marker((10, 10), False), Target(0)),
        10.8: (80, Marker((10, 10), False), Target(0)),
        11.0: (80, Marker((12, 12), False), Target(0)),
        11.2: (80, Marker((12, 12), True), Target(0)),
    }

    class Adapter:
        def __init__(self):
            self.actions = []

        def ready(self):
            return True

        def observe(self, sensors, token):
            token.checkpoint()
            health_value, marker, target = frames[round(clock.monotonic(), 1)]
            readings = {}
            for sensor in sensors:
                if sensor.kind is SensorKind.HEALTH:
                    readings[sensor] = Reading(health_value)
                elif sensor.kind is SensorKind.MARKER:
                    readings[sensor] = Reading(marker)
                elif sensor.kind is SensorKind.TARGET:
                    readings[sensor] = Reading(target)
            return ObservationBatch(readings, clock.monotonic())

        def execute(self, action, token):
            token.checkpoint()
            self.actions.append((clock.monotonic(), action))

        def close(self):
            pass

    adapter = Adapter()
    with JsonlRecorder(path) as recorder:
        with ClientSession(adapter, clock=clock, recorder=recorder, autostart=False) as session:
            session.start_feature(scenario_factory("CaveBot", 1))
            session.start_feature(scenario_factory("AutoHeal", 1))

            # Healing wins the first arbitration; the second scheduler pass
            # executes the queued walk without taking another observation.
            session.tick_once()
            session.tick_once()
            for at in (10.2, 10.4):
                clock.advance_to(at)
                session.tick_once()
                if at == 10.2:
                    session.tick_once()  # dispatch the target click queued behind healing

            # A fresh generation must receive only its own feedback in replay.
            assert session.stop_feature("AutoHeal")
            session.start_feature(scenario_factory("AutoHeal", 2))
            for at in (10.6, 10.8, 11.0, 11.2):
                clock.advance_to(at)
                session.tick_once()

    events = load_trace(path)
    actions = adapter.actions
    assert actions[:4] == [
        (10.0, Action(ActionKind.PRESS, key="F1")),
        (10.0, Action(ActionKind.LEFT_CLICK, point=(10, 10))),
        (10.2, Action(ActionKind.PRESS, key="F1")),
        (10.2, Action(ActionKind.LEFT_CLICK, point=(20, 20))),
    ]
    assert (10.6, Action(ActionKind.PRESS, key="F1")) in actions
    assert (11.0, Action(ActionKind.LEFT_CLICK, point=(12, 12))) in actions

    heal_starts = [
        event.data["status"].generation
        for event in events
        if event.kind == "status"
        and event.owner == "AutoHeal"
        and event.data["reason"] == "started"
    ]
    assert heal_starts == [1, 2]
    cave_steps = [
        event.data["step"]
        for event in events
        if event.kind == "decision" and event.owner == "CaveBot"
    ]
    assert any(step.phase == "Fighting dragon" for step in cave_steps)
    assert any(step.phase == "Finding marker" for step in cave_steps)

    report = replay_policies(events, scenario_factory)
    assert report.decisions == sum(event.kind == "decision" for event in events)
    assert report.requests == sum(
        len(event.data["step"].requests) for event in events if event.kind == "decision"
    )
    assert report.feedback >= len(actions)


def test_replay_adapter_runs_real_runtime_without_native_input(tmp_path):
    events = record_session(tmp_path / "trace.jsonl")
    snapshots = tuple(e.data["snapshot"] for e in events if e.kind == "observation")
    clock = ReplayClock(10)
    adapter = ReplayAdapter(snapshots, clock)
    with ClientSession(adapter, clock=clock, autostart=False) as session:
        session.start_feature(factory("AutoHeal", 1))
        for at in (10, 10.1, 10.2, 10.4, 11.0):
            clock.advance_to(at)
            session.tick_once()
    assert adapter.closed
    assert adapter.actions == [
        (at, Action(ActionKind.PRESS, key="F1")) for at in (10, 10.2, 10.4, 11.0)
    ]


def test_old_replay_frame_remains_stale_and_cannot_create_input():
    clock = ReplayClock(10)
    snapshot = Snapshot({HEALTH: Observation(HEALTH, 20, 9, 1, 1)}, 1, 1, 10)
    adapter = ReplayAdapter((snapshot,), clock)
    with ClientSession(adapter, clock=clock, autostart=False) as session:
        session.start_feature(factory("AutoHeal", 1))
        session.tick_once()
        assert not session.feature_status("AutoHeal").active
    assert adapter.actions == []


@pytest.mark.parametrize(
    "mutation",
    ["truncated", "missing_footer", "checksum", "unknown_type", "duplicate_key", "nonfinite"],
)
def test_trace_rejects_corruption_before_replay(tmp_path, mutation):
    path = tmp_path / "trace.jsonl"
    record_session(path)
    lines = path.read_bytes().splitlines(keepends=True)
    if mutation == "truncated":
        lines[-1] = lines[-1][:-3]
    elif mutation == "missing_footer":
        lines.pop()
    elif mutation == "checksum":
        lines[1] = lines[1].replace(b"Starting", b"SomethingElse")
    elif mutation == "unknown_type":
        lines[1] = lines[1].replace(b'"Event"', b'"os.system"')
    elif mutation == "duplicate_key":
        lines[1] = b'{"type":"Event","type":"Event"}\n'
    else:
        lines[1] = b'{"at":NaN}\n'
    path.write_bytes(b"".join(lines))
    with pytest.raises(TraceError):
        load_trace(path)


def test_recording_quotas_stop_runtime_and_never_overwrite_existing_file(tmp_path):
    path = tmp_path / "trace.jsonl"
    with JsonlRecorder(path, max_bytes=1024) as recorder:
        recorder.record(Event(1, 0, "example"))
        with pytest.raises(TraceError, match="limit"):
            recorder.record(Event(2, 0, "example", data={"huge": "x" * 1024}))
        with pytest.raises(TraceError, match="unavailable"):
            recorder.record(Event(2, 0, "example"))
    assert path.stat().st_size <= 1024
    with pytest.raises(FileExistsError):
        JsonlRecorder(path)
    with pytest.raises(TraceError, match="incomplete"):
        load_trace(path)


def test_reader_event_limit_and_schema_version(tmp_path):
    path = tmp_path / "trace.jsonl"
    record_session(path)
    with pytest.raises(TraceError, match="event limit"):
        load_trace(path, max_events=1)
    lines = path.read_text().splitlines(keepends=True)
    header = json.loads(lines[0])
    header["version"] = 99
    lines[0] = json.dumps(header) + "\n"
    path.write_text("".join(lines))
    with pytest.raises(TraceError, match="schema"):
        load_trace(path)


@pytest.mark.parametrize("kind", ["observation", "decision", "status", "result"])
def test_replay_checks_payloads_before_calling_factory(kind):
    called = []
    with pytest.raises(TraceError, match="payload"):
        replay_policies((Event(1, 0, kind),), lambda *args: called.append(args))
    assert called == []


def test_replay_import_is_headless_and_has_no_native_dependencies():
    script = """
import sys
import tibia_client.trace
for name in ('tkinter', 'cv2', 'PIL', 'keyboard', 'pyautogui', 'win32gui', 'tibia_client.desktop'):
    assert name not in sys.modules, name
"""
    subprocess.run([sys.executable, "-c", script], check=True, timeout=10)
