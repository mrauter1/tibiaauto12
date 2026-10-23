# Live desktop probe results — 21 October 2026

Paladjinn was signed in successfully in PaulistinhaOT inside Windows Sandbox
and, independently, in the host client. Trials 01–09 concern the host-side
Sandbox window, HWND 5114006. Trial 10 concerns host Tibia, HWND 3735796, with a
1275 × 812 physical client. Basic physical keyboard acceptance was observed in
both arrangements; full compatibility remains unknown.

The original evidence is preserved in separate trial directories under `.work/`.
No bot module was started and no implementation was committed.

| Trial | Method | Observed result |
|---|---|---|
| 01 / 02 | Original PrintWindow | Returned 1214 × 684 while the physical client measured 1518 × 855. Capture validation rejected the size mismatch. |
| 02 | Experimental PrintWindow with physical DPI context | Five 1518 × 855 frames arrived. All were identical, uniformly white images when visually inspected. Correct dimensions did not establish usable game content. |
| 01 / 02 | Strict visible-screen capture | Rejected an overlapping host window before returning an image. Added diagnostics identified `CodexComputerUseCursorOverlay`, a fullscreen Computer Use annotation surface. |
| 03 | Experimental qualified-overlay screen capture | After qualifying both exact annotation titles, five 1518 × 855 game images arrived. Paladjinn and the intended game controls were visually identified. Warm median was 1203.5 ms; maximum/p95 was 1454 ms. |
| 03 | Exact physical Ctrl+O | Before image had no chooser; after image contained the Open Channel chooser. Dispatch completed in 31 ms. Visible response was recorded as observed. |
| 03 | Exact physical Escape | Before image contained the chooser; after image showed it closed. Visible response was recorded as observed. |
| 03 | Host obstruction negative | An Explorer window was detected by point-hit checks over Sandbox. Input dispatch remained `not_attempted`; the worker was reaped. |
| 04 | Paired guest-focus calibration | A text-only guest title patch passed distinctiveness checks without changing thresholds. The configured host image is 1518 × 855, viewport `[2,73,1518,796]`, focus patch `[30,47,130,63]`. |
| 04 | Guest taskbar focus negative | Readiness returned false with the expected inactive-focus error, in 828 ms; no input was dispatched. |
| 04 | Active Paladjinn baseline | After reconnecting and explicitly focusing the guest game, readiness returned true in 531 ms. |
| 04 | In-client modal negative | Open Channel remained visible but readiness returned true in 485 ms. This disproves using the title patch as complete game-state readiness. The test dispatched no input. |
| 04 | Exact message Escape | Delivery completed in 15 ms; before/after images still showed Open Channel. Recorded visible acceptance failed. |
| 04 | Guarded physical Escape | Open Channel closed. Two focus checks took 1109 ms total; dispatch including those checks took 1109 ms. Recorded visible acceptance passed. |
| 08 | Region-GDI foreground negative | Capture refused the frame while the host Paulistinha launcher had foreground. Diagnostics identified that exact launcher HWND; no image or input was returned. |
| 08 | Region-GDI foreground baseline | Five 1518 × 855 game frames arrived and were visually inspected. Warm request median was 508 ms, maximum/p95 547 ms; cold request was 4844 ms. This is a small-run improvement over the earlier Pillow trial, not a controlled end-to-end action benchmark. |
| 09 | Changed-control negative | An idle disconnect changed the selected Cancel patch before dispatch. Fresh-pixel validation rejected it; dispatch stayed `not_attempted` and the worker was reaped. |
| 09 | Annotation interference negative | A second Cancel attempt encountered different pixels near the visible cursor annotation. Exact patch validation rejected it before dispatch. No tolerance was relaxed. |
| 09 | Guarded physical left click | A verified Local Chat tab patch matched. Native host-client point `[96,689]` reached desktop point `[175,689]`; Local Chat visibly became selected, and cursor restoration completed. Visible acceptance was recorded as observed. |
| 10 / host | Original PrintWindow | No frame arrived: the capture worker exceeded its 15-second startup allowance. This attempt did not establish the native PrintWindow API's result. |
| 10 / host | PrintWindow with physical DPI context | Five equal 1275 × 812 frames arrived, but original-image inspection found invalid white/black content rather than the game. The transport/nonblack gates alone passed. |
| 10 / host | Strict visible screen | Capture rejected the Computer Use cursor annotation as an overlapping host window. No frame was returned. |
| 10 / host | Qualified-overlay Pillow screen capture | Five correctly sized frames arrived. Four warm requests had a 515.5 ms median and 625 ms maximum/p95. This establishes transport for that sequence, with game-state assessment remaining separate. |
| 10 / host | Qualified-overlay region GDI | Five correctly sized frames arrived. Four warm requests had a 640.5 ms median and 1156 ms maximum/p95. The saved sequence included an idle Connection Lost dialog; it is not evidence of an unobstructed gameplay state or a controlled speed improvement over Pillow. |
| 10 / host | Exact physical Ctrl+O | After reconnecting, before/after originals showed Open Channel opening. Dispatch completed and visible acceptance was recorded as observed. Worker startup was 13625 ms, before/after capture phases 1610/1171 ms, and native dispatch 31 ms. |
| 10 / host | Exact physical Escape | Before/after originals showed Open Channel closing. Dispatch completed and visible acceptance was recorded as observed. Worker startup was 10328 ms and before/after capture phases 1266/891 ms. Dispatch measured 0 ms at the timer's resolution; this does not mean zero latency. |
| 11 / Sandbox | One-frame lease startup attempts | Both explicitly armed Ctrl+O trials timed out before a before-frame or input dispatch. Total reported durations were 29641/21360 ms; the first calibrated-target events appeared at 29485/14172 ms. Both workers were reaped. The configured 15-second readiness wait was not a hard end-to-end wall-clock startup bound. |
| 12 / Sandbox | One-frame lease with diagnostic startup allowance | An explicit 45-second startup allowance let this attempt reach worker readiness in 10343 ms. Acquisition took 671 ms; the recorded before-capture phase took 703 ms. By action-scope entry, the frame was 1578 ms old against the unchanged 1000 ms limit, so the lease correctly revoked and dispatch stayed `not_attempted`. |

For trial 02, the physical-DPI PrintWindow experiment took 6.829 seconds for the
first frame. Four warm requests had a 172 ms median and 203 ms maximum/p95.
These timings describe unusable white frames, not a working game reader.

An explicitly separate `visible_screen_overlay_aware` experiment was added.
It qualifies only the exact Computer Use cursor annotation class and two observed titles with both
transparent-input and no-activation styles. Foreground, size, desktop bounds,
point-hit and before/after checks remain active. Production capture stays strict.
The Ctrl+O and Escape trials establish deliberate local UI freshness and basic
native key acceptance through this Sandbox installation. Focus checks establish
only the tested distinction between active Tibia and the guest taskbar. They
do not detect every in-client dialog. A separate experimental known-dialog
detector uses the trial-03 header as a template, with threshold 0.90 fixed before
the trial-04 evaluation. A distinct modal frame scored 0.9971 and was vetoed;
a distinct clean frame scored 0.4909 and produced `no_known_veto`. Absence of
this known template never establishes general safety. The original offline
report is `.work/known-dialog-evaluation/trial04-header-eval.json`.

An idle disconnection occurred between the initial inactive check and the first
attempted active check. That attempt returned false; it is not the clean active
baseline above. Paladjinn was reconnected before repeating the baseline.
Several initial mouse attempts were rejected by the preview-expiry gate before a
worker or input dispatch was started. The probe now supports explicit one-shot
preview selection and stable-template reacquisition. The latter permits a
known, same-window control template up to 300 seconds old only when its exact
pixel patch matches a fresh worker capture before dispatch. Ordinary preview
mode retains its 30-second limit. Neither mode starts input automatically.

Trial 05 exposed a probe startup freeze before any worker/input began. A saved
Python stack trace locates it in `platform._wmi_query`, triggered by importing
the `keyboard` package for stop-key polling. The probe now reads the three stop
keys directly through `GetAsyncKeyState`; subsequent capture trials completed.
This change keeps the fail-closed stop check and removes the hook-library import.

The region-GDI experiment changes only screen acquisition: it uses a physical
host-client rectangle, top-down 32-bit DIB and plain SRCCOPY. Foreground,
geometry, narrow annotation qualification and point-hit checks remain active.
The probe still displays the Computer Use annotation where it is visible.
Production defaults have not been changed to this candidate.

The accepted Sandbox mouse trial still made 12 readiness captures taking 6344 ms total.
Dispatch itself, including its event-boundary checks, took 3391 ms; separate
before/after captures took 1468/1203 ms. This establishes one accepted click and
one coordinate location, not five-point viewport mapping or suitable latency
for healing. The guard needs a bounded shared-frame design before promotion.

Trials 11 and 12 tested a separate one-frame lease candidate, not a promoted
replacement. In trial 11 the 15-second deadline covered the worker-readiness
wait after `process.start()` returned. Process startup and other elapsed time
made the recorded totals longer; neither attempt reached capture or dispatch.
The original event timings are preserved in
`.work/desktop-probe-live-trial11-lease/session.json`.

Trial 12 changed only the explicit diagnostic startup allowance to 45 seconds;
the lease budgets remained 1000 ms acquisition age and 250 ms action scope.
The same before-frame passed the calibrated focus check and scored 0.55938 for
the known Open Channel template, producing `no_known_veto`. Those assessments
did not override the expired acquisition-age gate and do not establish general
game readiness. Approximately 907 ms elapsed between acquisition completion
and scope entry, including all subsequent assessment and instrumentation work;
it is not an isolated matcher benchmark. No accepted input or fast-action success
has been established for this candidate. Its frame, assessment, refusal and cleanup
evidence are recorded in
`.work/desktop-probe-live-trial12-lease-startup/session.json`.

A bounded offline replay then evaluated that exact saved RGB frame five times
with the default OpenCV thread setting and five times in a separate process
configured for one thread. The threshold, full RGB viewport and template stayed
fixed. Default-setting totals ranged from 467 to 762 ms (median 686 ms);
one-thread totals ranged from 491 to 769 ms (median 524 ms). All ten replays
preserved the exact score, verdict and frame hashes. Matching remained costly
after the first call. These small samples in different processes were not a
controlled A/B benchmark. Even the observed best one-thread replay, 491 ms,
plus the original 671 ms live acquisition exceeds the 1000 ms age limit before
other work. This comparison is illustrative rather than a predicted combined
live timing. The candidate therefore remains a measured no-go for this budget;
no thread, warmup, threshold, search or runtime changes were made to force a
pass. Results are preserved in `.work/probe-lease-timing-summary.txt` and its
referenced replay JSON files. Earlier instrumented import profiles describe
cold diagnostic startup and do not establish live input or production
performance.

The host originals and operator confirmations are preserved in
`.work/desktop-probe-live-trial10-host/session.json` and its referenced PNGs.
The Ctrl+O/Escape results establish only those two physical operations and a
deliberate visible UI transition. Host mouse acceptance, five-point mapping,
OBS projector capture, message input, and other client actions remain unverified.
The capture phase timings include image persistence; they are not isolated
native API timings. The original local OBS-projector path was not exercised by
capturing the game HWND directly and cannot be validated or disproved by those
direct-capture results.

These trials do not establish full mouse mapping, all action timings, or
complete compatibility for either arrangement. The sessions' overall
compatibility remains unknown.

Final automated validation of the probe covered nine focused suites: 499 tests
passed. Ruff and `git diff --check` also passed. These checks validate probe
logic and cleanup; they do not turn the live latency refusal into compatibility.

See [probe procedure](desktop-probe.md) for evidence gates and the remaining
freshness, input, mapping, focus-negative and independent host-client tests.
# Current investigation update — 2026-10-21

The later error-only trials and foreground-routing diagnosis are documented in
[input diagnosis](input-diagnosis.md). That update supersedes the historical
guard-default discussion above: current input defaults check availability only
after a reported error. After unlocking and reconnecting Paladjinn, the one-time
Arm activation repair passed two Ctrl+O trials and two mouse Cancel-click trials
in trial 19. Keyboard dispatch was 47–62 ms, mouse dispatch 62–94 ms, with zero
guard calls. The first immediate keyboard after-image missed the transition,
but later read-only observation and capture showed it; the other three
after-images show the expected state directly. Full results and limitations are
in the linked diagnosis.
