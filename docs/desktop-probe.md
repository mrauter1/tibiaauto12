# Experimental host and Sandbox probe

Use this harness to test capture and input before selecting an implementation.
It does not start bot modules, save a production profile, or write compatibility
sidecars. Run it from the repository root:

```powershell
.\.venv\Scripts\python.exe -m tools.desktop_probe --output .work\desktop-probe\trial-01
```

Use a fresh output directory for each session. The GUI writes `session.json`
and original PNGs under it. Screenshots contain the selected application's
visible content. No account configuration is read.

## Current direct-input path

The defaults are `error_only_physical` input and `direct_visible_region_gdi`
capture. Client availability is checked **only after a reported capture or input
error**. A successful operation has no availability diagnostic calls.

Arming error-only input activates the selected host window once, from the GUI
button callback. This is a focus action, not a recurring availability check.
It has no countdown by default. The one-shot worker still starts cold for each
probe; worker startup is measured separately from dispatch. The GUI remains
visible after **Other screen** succeeds; otherwise it hides while probing.
`--countdown-seconds 0..30` explicitly overrides the countdown for experiments.

The worker dispatches the chosen operation first. It performs no before-image
capture, foreground/visibility preflight, focus-patch match, dialog search,
selected-control patch match, or frame-age check. Sandbox points use the saved
viewport offset. Local points use their client-relative coordinates.

After dispatch, the probe waits 350 ms and captures an after-image as evidence.
This image is not an input gate. If evidence capture fails after dispatch, the
report keeps the completed dispatch fact and records the evidence error plus
availability diagnosis separately. No action is retried automatically.

For a mouse operation, use the preview to choose the intended coordinate. The
error-only backend does not impose preview-age or fresh-patch checks. A preview
is a coordinate-selection aid; inspect the after-image to assess the response.
The **Observed**, **Not observed** and **Unclear** controls record your assessment.
A completed native call alone does not establish that the game accepted it.

`direct_visible_region_gdi` uses a physical-pixel screen-region `BitBlt` into a
top-down 32-bit DIB. It does not call PrintWindow or grab the entire virtual
desktop. It reads the target's current client-screen rectangle for acquisition,
without foreground, overlay, point-hit or focus checks. Sandbox capture crops
the selected viewport from those host-client pixels. Native resources are
released after capture, including error paths. A covering window can appear in
the returned pixels because this is visible-desktop capture.

The report identifies this path with `availability_policy: "on_error"`,
`pre_action_capture: false`, and an `availability_diagnostics` list. On success,
that list is empty. Error diagnostics describe availability after the error;
they do not establish the target's state at an earlier time.

## Select a target

List available host windows without capture or input:

```powershell
.\.venv\Scripts\python.exe -m tools.desktop_probe --inventory
```

In the GUI, refresh the window list after opening a client. Startup enumeration
can precede the client becoming visible or acquiring its final caption. A
separate diagnostic option accepts a currently known native handle:

```powershell
.\.venv\Scripts\python.exe -m tools.desktop_probe --target-hwnd 3735796
```

That number is an example, not a persistent identity. Explicit target selection
checks `IsWindow`, `GA_ROOT`, visibility, minimized state, bounded title/class,
physical client size, final validity/root consistency and foreground root. It
records inspection time and whether the actual inventory contained the handle.
These are explicit selection diagnostics, not per-action availability checks.
Invalid, child, hidden, minimized or captionless handles remain unselected with
diagnostic reasons. Selection does not capture or arm input.

For a host game, select its actual game window for input. An OBS projector is a
capture surface and should not receive game input. For Sandbox, select the host
Sandbox display window; guest HWNDs are not exposed in host enumeration.

## Live protocol

1. Open the intended character. Arrange the host window, guest game placement,
   display scaling and game layout. Leave a harmless control available, such
   as the channel chooser.
2. Select the target and capture five samples. Inspect original PNGs for game
   content, dimensions and readability. **Compare all methods** keeps separate
   evidence for each capture candidate, including failures.
3. For Sandbox, open its viewport setup. Capture Tibia, drag the guest client
   rectangle and save the experimental target. This saves only into the probe
   session. The normal setup needs no focus patch or inactive sample. The
   probe's setup countdown is 30 seconds. Capture and legacy experiments default
   to three seconds; error-only input defaults to zero. Production setup uses
   three seconds by default.
4. Keep `error_only_physical` and `direct_visible_region_gdi` selected. Choose
   `ctrl_o`, describe the expected channel-chooser response, acknowledge the
   harmless operation and arm one operation. Arm activates the selected host
   window once; leave it active until dispatch finishes. Inspect the after-image and record the
   observed response. Test `escape` separately from a known open-dialog state.
5. For mouse mapping, select an identified harmless point in the preview. Test
   `move` before a click. Check native destination readback at the center and
   near each viewport corner, with one arm and assessment for each position.
   Destination readback measures host coordinate delivery; visible game
   responses establish acceptance of a click. Inspect cursor restoration too.
6. Compare dispatch timing separately from worker startup, settling and
   after-image capture. A cold worker can take several seconds to import image
   libraries without adding those seconds to the native keypress itself.

**STOP** requests cancellation. While the probe is hidden, **Ctrl+Shift+F12**
requests stop from the host keyboard. The harness reads this chord without
sending it. Input workers have separate 15-second startup and 10-second action
bounds. Cancellation/timeout terminates and reaps the worker and attempts to
release controls whose DOWN was reported. Inspect `worker_reaped`, `cleanup`
and `cursor_restore`; a stop cannot retract an operation already delivered.
An unreaped worker blocks another native input worker.

`--input-startup-timeout 45` initializes **Startup timeout (s)**. The default
is 15 seconds; explicit values must be finite, positive and at most 60. This
changes the readiness-wait deadline after synchronous `Process.start()` returns,
not the action deadline. Reports expose `process_start_duration_ms` separately.
A startup timeout before dispatch says nothing about game acceptance.

`--trace-key-target` records the foreground HWND at each native key event for
failure investigation. It is off by default, never gates input, and does not
retry or change focus. A failed telemetry read is recorded without preventing
the key event. `--key-hold-ms 0..250` changes only the experimental key hold;
production key timing is unchanged.

See [input diagnosis](input-diagnosis.md) for the hypotheses, measured delays,
and the repeated successful Ctrl+O and mouse trials using the focus handoff.

## Interpret the evidence

| Evidence | Measurement or assessment |
| --- | --- |
| Capture transport | Requested frames arrived with recorded dimensions and bounds. |
| Game content | An operator inspected original pixels and identified Tibia and its controls. |
| Freshness | A deliberate recognizable UI change appeared in later captured pixels. |
| Native dispatch | The selected backend completed its single operation. |
| Game acceptance | The operator recognized the expected response and recorded **Observed**. |
| Error diagnosis | Availability was inspected after an actual error. |

Black/nonblack pixels, hashes and pixel differences are measurements. A uniform
white PrintWindow image can pass transport and dimensions while failing game
content inspection. The harness leaves `overall_compatibility: "unknown"`;
individual accepted operations do not establish every game feature or layout.

`session.json` uses schema `tibiaauto.experimental-desktop-probe`, version 1.
Capture reports include window metadata, per-method measurements and original
PNG paths. Input reports include backend/method/operation, dispatch, timings,
events, after-image, errors, destination readback, cursor restoration, cleanup,
worker reaping and operator assessment. `calibrated_target` identifies the exact
viewport and its fingerprint. An error-only operation has no required
`before_path`; the absence is intentional.

Capture frame data includes size, RGB hash, nonblack measurement, change ratio,
wall/native timing and acquisition timestamps. `native_capture_ms` includes
native capture, conversion and transport; it is not an isolated Win32-call
measurement. Cold startup is excluded from warm capture percentiles.
`captured_monotonic` records worker observation start when available, with an
explicit parent-return fallback; neither timestamp proves in-game freshness.

PNG creation is exclusive. GUI operations use distinct subdirectories and
unique input image names. `compare_frames(path_a, path_b, roi=None)` reports
size and pixel differences without automatically confirming freshness.

## Optional historical experiments

The probe retains older candidates for reproducing the recorded experiments.
They are not required by the direct-input path. The earlier guard's repeated
captures and later frame lease were investigated before switching the default
policy to availability checks on error.

| Candidate | Behavior |
| --- | --- |
| `printwindow` | Calls the original `core.perception._capture_windows` baseline. |
| `printwindow_physical` | Calls that baseline within a temporary physical-pixel DPI context. |
| `visible_screen` | Original strict visible capture with foreground, geometry, bounds and occlusion checks. |
| `visible_screen_overlay_aware` | Strict visible capture with the narrowly qualified Computer Use annotation exception. |
| `visible_region_gdi_overlay_aware` | Region GDI pixels with the same strict checks and annotation exception. |
| `physical` / `messages` | Earlier one-operation probe facades with their existing preflight behavior. |
| `guarded_physical` and overlay-aware variants | Paired title-bar samples plus repeated per-phase readiness captures. |
| `frame_lease_region_gdi_overlay_aware` | One before-image, known-dialog/template checks and a bounded action lease. |

Historical capture uses a separate worker with a 15-second cold-start allowance,
a two-second capture deadline, no restart and a 64 MiB RGB limit. Alternate
methods do not silently replace a failed method.

The annotation exception requires exact class `CodexComputerUseCursorOverlay`,
one of the exact titles `Codex Computer Use Cursor Overlay` or
`ChatGPT is using your computer. Esc to cancel`, and both
`WS_EX_TRANSPARENT` (`0x20`) and `WS_EX_NOACTIVATE` (`0x08000000`). It omits a
qualified annotation only from the inherited rectangle-occlusion check.
Foreground, point-hit and before/after checks remain. The visible indicator and
Escape cancellation remain unchanged. This historical annotation policy is
separate from direct capture, which performs no overlay checks.

Legacy guarded Sandbox experiments require paired samples, selected explicitly
through `setup_sandbox(..., require_focus_samples=True)` or a prior calibrated
target report. The active and inactive title-bar patches must have the same
host geometry and distinguishable nonuniform contents. The experimental patch
can identify a focus change but does not identify every guest dialog. Normal
viewport setup does not request those samples.

The historical **Probe readiness only** action sends no input. Its positive
baseline is Sandbox foreground with Tibia focused and unchanged. Separate
negative experiments inspect host focus loss, guest focus loss, overlap,
minimization, geometry change and guest dialogs. These measurements describe
those legacy guards and do not enable a guard for error-only input.

Legacy mouse probes reject preview points older than 30 seconds and compare a
selected-control RGB patch against a fresh before-image. The explicit
`--reacquire-control` experiment allows a stable template up to 300 seconds old,
then requires an exact new patch match. Neither mechanism runs for
`error_only_physical`.

## Optional known-dialog and frame-lease experiments

`tibia_client.probe_known_dialog` matches a supplied identifying dialog header
inside a bounded viewport using unscaled `cv2.TM_CCOEFF_NORMED` with a frozen
`0.90` threshold. A match reports `blocked_known_dialog`; absence reports
`no_known_veto`, which establishes neither general readiness nor universal
absence of dialogs. Constant templates and invalid geometry are rejected.
It is not evaluated by the default direct-input path.

For example, use inspected source and heldout images with explicit rectangles:

```powershell
.\.venv\Scripts\python.exe -m tools.evaluate_known_dialog `
  --template-source reference.png --template-box 604 253 915 276 `
  --viewport 2 73 1518 796 --name open_channel --threshold 0.90 `
  --positive heldout-modal.png --negative heldout-clean.png `
  --output .work\known-dialog-evaluation\report.json
```

The evaluator records file, RGB, viewport and template hashes, scores,
locations, unique/duplicate counts and source leakage. Whole-source or
whole-viewport duplicates do not count as heldout evidence. Multiple frames
from one capture are one acquisition group. The recorded live drag did not
move the centered dialog, so live recognition at another position remains
unverified.

The optional frame-lease backend requires `--known-dialog-report` pointing to a
passed report with verified hashes, frozen threshold and matching target
geometry. It uses one before-image for focus, known-dialog and control checks.
Its limit is 1000 ms from capture start plus a 250 ms action scope. Cheap
per-phase native callbacks inspect root/focus, visibility, geometry, bounds and
sampled point hits without another screenshot. Guest-focus pixels are not
renewed during an action. Image encoding is deferred until dispatch/refusal.
These checks remain available to reproduce the historical timing results;
they do not run with the default error-only backend.
