# Recording and offline replay

`tibia_client.trace` provides observation-only diagnostic recording and native-free replay. A trace records sensor values, quality, timestamps, frame/epoch identities, exact decision snapshots, requests, dispatches, feedback, and lifecycle changes. It deliberately contains no screenshots and cannot test the image detectors themselves.

## Record a session

Create the recorder before the session and close it after the session. Existing files are never overwritten.

```python
from tibia_client import ClientSession
from tibia_client.desktop import DesktopAdapter
from tibia_client.trace import JsonlRecorder

# Run after loading and validating the selected client/profile.
with JsonlRecorder("session.jsonl", max_bytes=16 * 1024 * 1024) as recorder:
    with ClientSession(DesktopAdapter(mouse_mode=1), recorder=recorder) as client:
        client.start_feature(configured_feature)
        # Keep the application's normal event loop alive here.
        run_application_event_loop()
```

For an embedded UI, pass `recorder=recorder` to `configure_client`, then call `close_client()` before closing the recorder. Recording is opt-in through this SDK composition API. The stock launcher retains a bounded in-memory event history available through `client.events(after=sequence)`.

The default file limit is 16 MiB. Events are flushed after each write. A quota or I/O error makes the recorder unavailable; the session stops dispatching instead of silently continuing an incomplete audit. Choose the quota for the scenario being diagnosed. A trace contains configured keys, coordinates, sensor names, and settings-derived decisions, so inspect it before sharing.

Custom event and frame recorder callbacks are read-only observers. They may inspect session state, but must not synchronously call `start_feature()`, `stop_feature()`, `close()`, or `tick_once()` on that session. These reentrant calls raise before changing its lifecycle; an uncaught exception fails the recorder and stops automation. Send any requested lifecycle change to the application's event loop after the recorder returns. This keeps result delivery and the recorded `deliver` flag consistent and prevents callbacks from an obsolete generation.

## Compare feature policy

`load_trace()` validates the entire file before returning events. `replay_policies()` feeds the original decision snapshots and monotonic times into fresh features, supplies recorded action feedback, and compares the complete resulting `Step` values. A changed request, priority, requirement, phase, or cancellation key is a divergence.

```python
from engine.support import Threshold
from features import HealFeature
from tibia_client import Sensor, SensorKind
from tibia_client.trace import load_trace, replay_policies

health = Sensor(SensorKind.HEALTH, (100, 50))

def factory(name, generation):
    if name != "AutoHeal":
        raise ValueError(f"No replay settings supplied for {name}")
    return HealFeature(health, (Threshold(True, 50, "F1"),))

report = replay_policies(load_trace("session.jsonl"), factory)
print(report.decisions, report.requests, report.feedback)
```

Supply the original immutable feature settings and sensor coordinates for every recorded generation. The trace does not deserialize feature classes or import arbitrary code. For CaveBot, provide an in-memory or no-op `save_progress` callback so the replay cannot update real route checkpoints. The caller-supplied factory and features are trusted Python code; the replay module itself never constructs a desktop adapter or executes generated actions.

Policy replay checks successful recorded decision steps and their feedback. A failure that occurred before a feature returned a `Step` remains diagnostic status data. This is not replay of operating-system thread scheduling or native input effects.

## Exercise the runtime with virtual time

For scheduler scenarios, combine `ReplayClock`, `ReplayAdapter`, and `ClientSession(..., autostart=False)`. Build the adapter from the `Snapshot` values in `observation` events. Advance the clock to each scenario tick and call `tick_once()`. The adapter advances to the recorded frame publication time, preserves capture-begin timestamps, and collects actions in `adapter.actions` without sending input.

Each observation call consumes one recorded capture. Only sensors actually sampled in that frame may be requested; older observations retained in the shared snapshot keep their old identity and age and are managed by `ClientSession`. Therefore the scenario must reproduce sensor demand and tick timing, or deliberately supply its own snapshots. Missing frames, mismatched demand, backwards time, and stale evidence fail closed. `tests/test_sdk_trace.py` demonstrates both policy comparison and runtime replay using the real healing policy.

## Format and integrity

The UTF-8 JSONL format starts with format name `tibia-client-trace`, version `1`, and mode `observations`. Model and enum tags are allowlisted; maps encode key/value pairs so `Sensor` keys round-trip without string conversion. No pickle, dynamic import, or executable payload is accepted.

Events have contiguous sequence numbers. A final footer records the event count and SHA-256 of the encoded event lines. The reader rejects unknown versions/tags, duplicate keys, nonfinite numbers, inconsistent model identities, size/count overruns, checksum mismatch, missing completion, and truncated lines. The checksum detects corruption; it is not an authenticity signature. A process crash or a recording error leaves an incomplete file which may be inspected manually but is not accepted as a complete replay.
