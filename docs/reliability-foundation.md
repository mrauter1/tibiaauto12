# Reliability foundation: engineering contract

The reliability foundation places one public SDK between feature decisions and the desktop. Its acceptance criteria are deterministic offline behavior, bounded failures, explicit ownership, truthful UI state, and reproducible tests. Compatibility with a current game client remains a separate acceptance step.

## Session ownership and cancellation

The Tk application configures one client through `tibia_client.application.configure_client(mouse_mode)`. Feature windows retrieve that session with `get_client()`, submit configured feature objects with `start_feature(feature)`, stop them by name, and poll `FeatureStatus` on the Tk thread. Application shutdown calls `close_client()` once.

`ClientSession` owns feature generations, readiness, observation snapshots, the request queue, native dispatch, status, and event history. Starting a different feature instance with an active name invalidates that generation and its queued or pending-confirmation requests before installing the replacement. Stopping a name has the same ownership check. A stopped generation cannot submit later actions.

The production session loop is cancellable and all native actions are serialized. Python cannot forcibly terminate an OS call already in progress, so cancellation prevents subsequent work rather than promising instantaneous interruption. Adapter calls and capture requests must remain bounded, and cleanup must attempt to release any held buttons or modifiers.

Stop and replacement cancel the generation before waiting for its feature hooks to finish. That wait is bounded to one second: success means the previous hooks have returned; timeout marks the generation failed, rejects its late output, and raises an explicit error. An extension's uncooperative Python hook can still unwind later, so hooks must remain short and must never send input themselves. Explicit adapter-close errors propagate and remain retryable; a failed close prevents replacing the application's client.

Readiness is checked on each working tick and again before dispatch, after recorder callbacks. Losing readiness clears the observation epoch, rejects queued commands, and reports already executed but unconfirmed commands as failed confirmation. Features wait for focus for at most `ready_timeout` (30 seconds by default). Recovery makes every active feature due for fresh observation and a new decision; old commands are never resumed. Policy deadlines continue to use elapsed monotonic time, so a policy may time out after a long interruption. Native backends still validate the target immediately before input.

An injected clock must return finite monotonic timestamps. A clock failure during an active session closes the session, fails its active features, and terminalizes outstanding work. Failure reporting uses the last valid timestamp so cleanup does not depend on a working clock. An action that reached the adapter is never automatically retried because its completion timestamp failed.

## Feature contract

A feature implements the small protocol in `tibia_client.ports`:

- `name` and a positive `interval` identify its scheduled generation.
- `sensors()` declares the immutable `Sensor` values needed for its next tick.
- `tick(FeatureContext)` reads the supplied `Snapshot` and returns a `Step`.
- `on_result(ActionResult)` advances policy state after execution, confirmation, rejection, cancellation, or failure.

Features must not import Tk, capture modules, native input, `ThreadManager`, or the legacy runtime. They do not sleep or run private loops. Time comes from `FeatureContext.now`; delayed behavior is represented as state and future ticks.

A `Step` may contain requests, cancellation keys, a phase, and a terminal result. Each `Request` contains one `Action`, a bounded TTL, a priority, and optional `Requirement` and `Confirmation` values. Requirements bind dispatch to the observations that justified the request. Unknown, stale, wrong-epoch, or replaced evidence must fail closed.

## Observation and scheduling

For each scheduler iteration, the session collects all sensors required by due features and asks the adapter for one observation batch. It publishes an immutable `Snapshot` with a frame ID, epoch, timestamp, and immutable observation map. Due features therefore make decisions from one coherent capture rather than privately capturing at different times.

The request queue is bounded. It rejects duplicate owner/key work, expired TTLs, stale requirements, obsolete feature generations, and requests whose confirmation can no longer be established. Each tick has three dispatch slots: one health hotkey, one mana hotkey, then one other action. Resource hotkeys are PRESS requests bound to health or mana observations; other support, combat, loot and navigation actions use the third slot. Each slot selects by effective priority, deadline, then insertion order. Native calls remain serialized and each call revalidates cancellation, TTL and observation freshness. No feature is re-run between these calls, so the same frame cannot cause repeated healing. Healing and mana refill can accompany a CaveBot action without waiting for another capture, but cannot preempt a native call already in progress.

The in-memory event history is bounded and contains small status, observation, request, and result metadata rather than captured frames. Trace and replay are observation-only, use versioned JSONL and virtual time, and never initialize a native adapter during replay. See [Recording and replay](recording-replay.md) for the format and workflow.

## Desktop and native boundary

`DesktopAdapter` is the only production implementation that combines capture, detectors, readiness, and native actions. Domain features and Tk windows depend on SDK models and ports rather than `HookWindow`, scanners, `Hotkey`, `keyboard`, or Win32 APIs.

The adapter captures one immutable RGB frame for an observation batch and evaluates every requested sensor against it. A detection error is distinct from a valid no-match, and an unreadable sensor is `Quality.UNKNOWN`; neither is permission to act. Target and marker matches are translated from region-relative detector coordinates to client-local points exactly once.

All SDK mouse actions use non-negative client-local integer coordinates. Physical input converts those points to screen coordinates, requires the selected game window to be foreground, and never forces focus. Window-message input addresses the selected input window and bounds synchronous sends. Successful OS dispatch does not prove that the client accepted or acted on the event.

The capture process serializes provider calls and transfers pixels through a bounded shared-memory buffer, with small request-ID messages on its control pipe. Capture-begin monotonic time comes from the worker, excluding process startup without hiding capture or detector latency. A timeout or cancellation terminates and reaps the worker; retries inside one request have a strict budget. A successfully reaped timeout or exhausted transport disconnect raises `TransientObservationError`: the session invalidates observations and queued work, retains enabled policies, and retries capture on a later tick after 250 ms. Pending confirmations are reconciled after recovery, not replayed. Malformed data and geometry errors remain terminal. If the operating system does not terminate a worker, its handle and buffer stay quarantined and replacement capture is blocked. The session sends no speculative input while capture is unresolved.

## Calibration and coordinate rules

`capture_hwnd` and `input_hwnd` have distinct roles; the legacy `hwnd` field is only a migration fallback. The selected capture and input client areas must have equal positive dimensions before calibration or activation. A profile records `calibration_size`, while application configuration records `capture_size`. Both are validated whenever a profile is loaded.

Legacy profiles without a bound calibration size require explicit recalibration. A resize invalidates existing geometry. Equal sizes do not prove equal content, DPI transforms, projector cropping, letterboxing, or pixel/input alignment. Supporting scale or offset requires an explicit transform plus recorded-image and controlled live validation.

Health and mana sensors use an `(x, y)` anchor with the existing 100-pixel bar geometry. Box sensors and route regions use `(left, top, right, bottom)` with increasing bounds. Action points and equipment drag sources are client-local. The UI validates configured drag coordinates against saved capture dimensions without querying or moving the live cursor.

## Feature behavior and CaveBot

Support behavior lives in `features/support.py`. Threshold, status, equipment, and timed-spell policies use SDK observations and return requests; cooldowns and deadlines use monotonic context time. Their Tk windows only validate and persist settings, start or stop a feature, and reconcile controls from SDK status.

`features/cavebot.py` is an iterative state machine. Marker search, movement, arrival, target selection, follow, combat observation, and bounded loot each advance through short ticks. Operations have deadlines and attempt budgets, re-observe after interruptions, and expose a phase through the shared session. Route progress is saved only after a confirmed waypoint transition.

Navigation requires fresh, known-zero observations for every configured target, and binds those observations to the movement request alongside its marker. Missing, stale, errored, or malformed target evidence fails CaveBot with the affected target and available detector detail. An unreadable higher-priority target cannot be treated as absent to select a lower-priority target. Loot also requires valid evidence for its active target.

Before adding a route action, define:

1. Required sensors and calibrated regions.
2. One or more bounded SDK requests and their cancellation keys.
3. Fresh evidence that confirms the outcome.
4. Timeout, retry budget, and terminal error.
5. Offline tests for success, ambiguity, cancellation, stale evidence, and timeout.

Rope, Shovel, depot, refill, reconnect, random recovery movement, and unbounded retries remain unavailable until these contracts exist. The current CaveBot does not provide pathfinding, exact world coordinates, resource forecasting, calibrated confidence, or corpse identity.

## Routes and configuration

Legacy routes contain marker/type/status entries. The loader validates them and imports a single active status as the initial cursor. Saving writes a versioned route definition with stable waypoint IDs and supported actions; runtime progress never mutates the definition.

Progress lives in `<route>.state.json` with the ordered route fingerprint. A changed definition or invalid checkpoint is rejected. Remove a stale checkpoint deliberately when the intended behavior is to restart at the first waypoint. Empty routes may be edited and saved, but cannot start.

Configuration writes use locked read-modify-write and atomic temporary-file replacement. Names must be plain JSON filenames under `scripts/`. UI snapshots retain legacy loader keys so existing profiles remain usable. Validate a complete snapshot before activating it or replacing persisted values.

## Verification and remaining client checks

The offline suite covers session generations, cancellation, readiness, bounded queues, priority ordering, stale and wrong-epoch rejection, confirmations, recorder failure, deterministic clocks, coherent snapshots, capture timeout and concurrency, detector geometry, support policies, CaveBot transitions, routes/checkpoints, configuration persistence, and GUI lifecycle where a display is available. Tests must not connect to the game or emit real input.

Before enabling automation on a target installation, collect controlled evidence for each scenario:

| Scenario | Evidence needed |
|---|---|
| Client layout and DPI | Saved points and regions overlay the intended features; capture and input origins align |
| Normal / OBS capture | Frames are fresh, complete, and free of scaling, cropping, or letterboxing |
| Health and mana | Fixtures cover full, low, empty, obscured, and theme-dependent colors; unknown never acts |
| Target recognition | Duplicate species, selection, scrolling, and partial rows produce the intended count and click point |
| Waypoint arrival | Centered marker evidence corresponds to arrival at the configured minimap zoom |
| Input backend | Hotkeys, clicks, drags, modifier release, focus loss, and message timeouts behave as expected |
| Pause / stop / close | No later generation action is dispatched and terminal status is visible |
| Concurrent features | Healing and support requests interleave acceptably with navigation, combat, and bounded loot |

No static inspection, synthetic image, or replay can replace these live observations. Sanitize any retained screenshots and record the layout, dimensions, DPI, capture mode, and profile that produced them.
