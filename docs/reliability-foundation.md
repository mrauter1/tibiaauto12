# Reliability foundation: engineering contract

This branch repairs the interface between a screen-driven client and future automation policies. Its acceptance criteria are deterministic offline behavior, bounded failures, explicit ownership, truthful controls, and reproducible tests. Live-client compatibility remains a separate acceptance step.

## Ownership and cancellation

A named worker owns a cancellation token for one generation. Calling Start twice does not create two active targets. Stop cancels that generation and pending restart. Starting while the previous target is still exiting defers the replacement until the previous target returns. Exceptions are retained in worker status and shown by the UI.

Use `runtime.wait(seconds)` and `runtime.checkpoint()` instead of uninterruptible policy sleeps. Wrap complete multi-step inputs in `runtime.input_transaction(reason)`; nested facade calls reuse the same lock. Lock waiting checks cancellation. Cancellation prevents subsequent actions; cleanup still attempts to release keys or buttons already held. Python cannot forcibly terminate a native OS call, so individual native calls and joining workers have bounded waits rather than a promise of instantaneous termination.

The shared input executor serializes actions but is not a priority scheduler. A loot sequence can occupy it until its bounded budget expires. Add explicit scheduling priorities, deadlines, and stale-observation rejection before implementing concurrent high-level objectives that require guaranteed emergency preemption.

## Perception and coordinate spaces

`Frame` contains immutable RGB pixels, dimensions, timestamp, and source. `frame_scope()` shares one snapshot across detectors within a policy tick. Separate module ticks may use separate frames. Templates are cached by path and file metadata. Detection failure raises an error; no match is represented by `None` in the new API.

`LocateMatch` / `locate_match` matches inside a supplied region and returns **region-relative** coordinates. A client action requires **client-local** coordinates. The detector adapter must translate once. `Scanners.locate_target` returns a client-local match; `LegacyPerception.find_marker` translates minimap coordinates. Keep this distinction in adapter tests whenever adding a detector.

Legacy tuple wrappers retain `(0, 0)` as a no-match sentinel for compatibility. New detectors should use the optional `Match` API because a real match at the origin is valid. Match scores are template similarity, not calibrated probabilities.

The capture actor serializes native provider calls and prevents timed-out calls from spawning unbounded replacement threads. It cannot kill a stuck `PrintWindow`; after a hung capture, requests fail within their deadline while that call remains outstanding. Do not respond to this failure by sending speculative input.

## Input boundary

All mouse points passed to the input facade are client-local, integral, and inside the current input client area. Physical input converts them using `ClientToScreen`, checks the selected window is foreground, and does not force focus. Window-message input addresses the selected window directly and bounds synchronous sends. Neither backend can guarantee that an arbitrary client accepted an event; success at this layer means the adapter completed its OS calls.

`capture_hwnd` and `input_hwnd` have different roles. The legacy `hwnd` field remains a fallback. Handles are loaded when needed rather than captured at module import. Profiles store `calibration_size`; native capture and input reject a changed client extent. Legacy profiles without a saved extent require explicit recalibration before desktop activation. A matching capture/input size does not establish identical content or DPI alignment. Adding scaling support requires an explicit transform and recorded-image plus live validation, not a new magic offset.

Do not add direct `keyboard`, `pyautogui`, or Win32 calls inside policy modules. Extend the facade or inject an action adapter, so input ownership and cancellation remain enforceable.

## Route and configuration migration

Legacy routes contain a list of marker/type/status entries. The loader validates them and imports a single active boolean status as the initial cursor. Saving produces a versioned route document with stable waypoint IDs and supported actions. Route definitions are not rewritten to record runtime progress.

Progress lives in `<route>.state.json` and includes the ordered route definition's fingerprint. A changed definition or invalid checkpoint is rejected instead of silently resuming another destination. Remove the stale checkpoint deliberately to start at the beginning after editing a route. Empty routes may be edited and saved; they cannot be started.

Configuration writes use an atomic temporary-file replacement. Read-modify-write updates are serialized rather than letting two module windows overwrite each other's saved settings. Configuration names must be plain JSON filenames in `scripts/`. Preserve existing preferences; validate before activating a profile.

## CaveBot extension seam

`CaveBotSession` depends on `Perception`, `Actions`, a clock, and a token. It manages marker search, navigation, combat interruption, optional loot, and checkpointing through observable states and reason codes. Its operations have deadlines and attempts, and it re-observes a marker after combat instead of recursively restarting navigation.

Before introducing a new route action, define:

1. Required observations and settings.
2. The exact action transaction and cancellation boundaries.
3. Evidence that the action completed.
4. A timeout, retry budget, and terminal reason.
5. Replay tests for success, ambiguity, cancellation, and timeout.

Rope, Shovel, depot, refill, and reconnect should remain unavailable until those contracts exist. Random movement and unbounded retries obscure failures and do not belong in recovery.

The current policy does not provide map pathfinding, exact world coordinates, resource forecasting, calibrated confidence, or corpse identity. These are future capabilities, not implied guarantees of screen matching.

## Verification and remaining client checks

The automated suite covers lifecycle races, cancellation, input transaction sequencing and cleanup, coordinate bounds, fresh window resolution, capture failure/timeout/concurrency, snapshot coherence, matching/regions, distinct target counts, unknown health, policy priorities and timers, immutable routes/checkpoints, bounded cavebot transitions, configuration persistence, and GUI lifecycle where a display is available. Tests must not connect to the game or emit real input.

Before enabling automation on a target client installation, validate these acceptance scenarios with a controlled session:

| Scenario | Evidence needed |
|---|---|
| Client layout and DPI | Saved points/regions overlay the intended client features; capture and input coordinate origins align |
| Normal / OBS capture | Current frames are complete and fresh; projector has no cropping/scaling/letterboxing |
| Health and mana | Recorded fixtures cover full, low, empty, obscured, and theme-dependent colors; unknown never triggers an action |
| Target recognition | One/duplicate species, selected row, scrolling, and partially hidden battle list yield expected counts and selection |
| Waypoint arrival | Marker-at-center evidence corresponds to actual arrival for the selected minimap zoom |
| Input backend | Intended hotkeys, click, drag, and modifier release are accepted; focus loss stops physical actions |
| Pause / close / error | No subsequent policy actions; any held modifier/button is released; the UI reports terminal reason |
| Multi-module run | Healthy simultaneous captures and serialized inputs remain responsive under load |

No static inspection or synthetic test can replace these observations. Keep any new real screenshots sanitized and label their capture layout so they become useful regression fixtures.
