# Desktop compatibility and operator controls

The desktop workflow keeps the original integration path: OBS Game Capture →
OBS projector → `PrintWindow` pixels → recognition → foreground Win32 input.
It adds evidence and validation around that path. It does not add game-memory
access, automation injection, packet interception, or anti-cheat bypasses.
OBS has its own capture mechanism; this is not a claim that OBS is hookless or
that any input method is undetectable or approved by a server.

## First run

1. Select the OBS projector as the capture window and the intended game as the
   input window. Use matching client-area dimensions and an unscaled, uncropped
   1:1 view. Select or recalibrate the existing character profile.
2. Open **Test client** in the launcher. Create a compatibility profile with the
   actual client build, OT server, Windows version, renderer, OBS version and
   configuration, DPI/scaling and layout. Input mode and capture mode describe
   this particular test. Window titles come from the selected windows. Report
   BattlEye state with its provenance; leave it unknown if it is unknown.
   Profile creation and replacement use the selected input window's measured
   client size. If it differs from the capture calibration, the studio reports
   both sizes and leaves the existing profile unchanged until the windows are
   resized or reselected and calibration is corrected.
3. Use the explicit capture-preview control. Foreground the game during the
   countdown. Opening the window itself never captures or sends game input.
   Inspect the captured image and calibration overlays before choosing points.
4. Complete the guided before/after capture check: manually cause a visible
   change in the game, capture again, and confirm that the selected capture view
   shows that change. This validates the current window binding for this launch.
   A historical saved check cannot unlock newly selected windows.
5. Select one input operation, its key or points and the intended visible
   response. Arm it, foreground the game, and wait for the result. Confirm what
   actually happened after completion. Test plain and modified keys, left/right
   clicks, dragging, cursor restoration and battle-list selection independently
   as needed. A mouse target must be selected from a current captured preview;
   Shift-click selects the drag destination. Key probes automatically select
   plain or modified-key testing from the entered hotkey. Testing an additional
   supported operation adds it to the profile when you confirm its result;
   cancelling discards the pending evidence and leaves the tool ready to retry.
6. Verify the mapped point separately from the operation's visible response.
   Alignment needs the center and points in all four corners of the view.
   Equal dimensions alone are insufficient. Use safe visible controls; the
   tool sends the selected action, so a click or drag can change game state.
   A failed point is marked in red with its exact coordinates until that same
   point is retested successfully. Cursor-restoration testing is available only
   for physical input; message-mode profiles do not require it.
7. Open **Hunt Console** to inspect blocking reasons and capabilities. Start only
   the modules whose required operations have passed for this context. Physical
   mouse actions also require a passed cursor-restoration check.

After a restart or window rebind, repeat the current-window visible-change
check. Durable operation evidence can be reused only while its environment,
calibration, layout, geometry and input mode still match. Recalibration or an
external client/OBS setting change requires resetting the compatibility context
and collecting new evidence. The reset workflow explicitly replaces old results;
it does not silently carry passes into a different context.

## What a result means

| Field | Meaning |
|---|---|
| Adapter capability | This implementation provides the operation or sensor |
| Dispatch | The native operation returned, or was rejected/canceled/failed |
| Automatic confirmation | A configured observation predicate completed within its bound |
| Operator confirmation | The operator assessed the intended visible result |
| Compatibility pass | Live-source dispatch plus confirmation for the same context |
| Simulation/replay result | Useful offline evidence; cannot unlock a live profile |
| Alignment | Separately confirmed capture/input point mapping across the view |
| Capture check | Transport, visual validity and an operator-observed visible change |

Cursor telemetry can include positions outside the client area. Missing or
invalid telemetry does not change an already completed native dispatch into a
failure; cursor restoration remains a separately assessed result.
For physical input, restoration uses the original desktop screen coordinate,
including a position outside the game window and a position retained while the
window moves. Existing focus checks and cancellation boundaries still apply.

These are local evidence records, not cryptographic attestations. A live-source
label identifies the actual desktop pipeline used; it does not prove a game
accepted an action. A pass still requires observed confirmation. No real-client
passes or universal client compatibility claims ship with this implementation.

Calibration and compatibility files are separate. The sidecar is
`scripts/<profile>.compatibility.json`; it contains metadata, fingerprints and
bounded evidence. HWNDs remain transient bindings in `conf.json` and are excluded
from the portable context identity. The production guard checks bound file
identity before dispatch, checks current native readiness/geometry, and blocks
changes until the operator explicitly reloads and verifies the selected windows.
External client/OBS settings are operator-reported; changing them outside the
application is not something a local screenshot timestamp can discover.

## Capture health and stopping

Every due feature performs a shared capture health check, including sensorless
hotkey features. The adapter rejects capture failures, invalid geometry and an
all-black frame. Detectors retain their own unknown/error results; structural
frame validity does not prove every detector can read the layout. Observations
and queued work are invalidated when readiness or capture fails.

The console distinguishes local capture age from upstream OBS/game freshness.
A static scene may be legitimate, and a frozen relay may yield new screenshots
of old pixels. Continuous upstream freshness remains **unverified**. The guided
visible-change check demonstrates one observed transition, not future freshness.
Do not interpret repeated identical images as conclusive freeze detection.

- **Pause navigation** suspends CaveBot while independent support modules keep
  running. Its navigation, combat, loot and stand timers retain their remaining
  time. The route remains locked. Resume requires fresh observations and valid
  profile requirements.
- **STOP ALL**, also available as **Ctrl+Shift+F12** when the global keyboard hook
  is available, cancels all feature generations, queued actions and pending
  confirmations, including start requests waiting when Stop All was pressed.
  Restart modules explicitly. An already executing OS call
  cannot be undone; cancellation prevents subsequent work and releases input
  through the existing backend cleanup path.
- **Hunt Console** keeps Stop All available while a navigation start, pause, or
  resume control is pending, so a slow control operation cannot hide the stop
  path.
- Recoverable capture timeouts or disconnected workers retain enabled features
  in **Waiting for capture; retrying**. Old observations and queued actions are
  discarded, and the next attempt follows a 250 ms delay. Already executed
  actions awaiting confirmation are reconciled after recovery, never replayed.
- Invalid capture geometry, malformed capture or fatal scheduler failure can close the
  session. Fix the cause and reopen/reselect the client; Stop All does not
  silently revive a failed session.

The launcher reports when the global shortcut is unavailable. The visible Stop
button remains usable. Control waits run outside Tk's event loop.

## Evidence and headless inspection

Frames are opt-in exports, separate from observation-only trace v1. Choose only
frames appropriate to share. Exported metadata should not contain credentials.
The GUI can save selected preview frames and an evidence bundle. The bundle
contains profile metadata and can include a valid bounded trace; it does not
invent missing history when the session event buffer has rolled over.

```console
python -m tools.compatibility --help
python -m tools.compatibility show MyProfile.compatibility.json
python -m tools.compatibility validate MyProfile.compatibility.json --require press
python -m tools.compatibility export MyProfile.compatibility.json compatibility-run
python -m tools.trace_viewer compatibility-run/trace-v1.jsonl --limit 100
```

`init`, `show`, `confirm`, `validate` and `export` are headless commands. They
cannot perform native probes. `validate` checks saved calibration/evidence; it
is not the production current-window binding check. CLI confirmation cannot
turn simulation into live evidence. See [recording and replay](recording-replay.md)
for trace limits, complete-session recording and native-free inspection.

## Live acceptance checklist

Offline tests cover state transitions, strict schemas, context changes, queues,
thread races, parser limits and simulated adapters. Before considering one real
client/OBS environment verified, collect its own evidence for:

| Scenario | Expected behavior / evidence |
|---|---|
| Each required input operation | Native dispatch and actual visible result recorded separately |
| Center and four corners | Selected preview point corresponds to the intended client-local point |
| Capture view before/after a deliberate change | Current game window and visible relay update confirmed |
| Focus loss and regain | No input while unready; old observations cannot authorize a later action |
| Window movement | Client-local points remain correct; physical cursor restoration is checked |
| Resizing or changed selection | Geometry/context gate blocks old calibration/evidence |
| Capture-window closure or all-black capture | Automation fails closed with a visible reason |
| Relay interrupted or frozen | Record the observable result; do not claim perfect freeze detection |
| Pause, resume and Stop during queued/pending work | No canceled action is resurrected; support pauses only on Stop All |

Walking-only routes derive only their walking requirements. They require a loot
click only when configured targets can trigger loot behavior.

This checklist is the remaining environment-specific release gate. Passing the
repository's offline suite does not substitute for it.
