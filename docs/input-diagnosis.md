# Input delivery and probe delay investigation

Status on 2026-10-21: the one-time foreground activation repair passed two
Ctrl+O trials and two mouse Cancel-click trials in the active Sandbox session.
The earlier attempt was blocked by the Windows lock screen; testing resumed
after unlock and reconnecting Paladjinn. Native API completion continues to be
separate from visible game acceptance.

## Demonstrated working method

From the host application, activate the selected Windows Sandbox host HWND once
using `SetForegroundWindow` when the user arms the operation. Keep that window
foreground while sending physical input. No per-key availability guard is used.

- Keyboard: the existing `keybd_event` backend sent Ctrl down, O down, O up,
  Ctrl up with the probe configured for a 50 ms minimum key hold. Both trials
  opened the channel chooser. All eight event traces named the selected Sandbox
  HWND 2624852. Dispatch durations were 47 and 62 ms as recorded by the probe.
- Mouse: under the physical-pixel DPI context, `ClientToScreen` mapped host-client
  point (865, 578) to screen point (1016, 604). `SetCursorPos` followed by
  `mouse_event` left down/up clicked Cancel. Both trials closed the chooser;
  dispatch took 94 and 62 ms. The original pointer position was restored.

These HWNDs and coordinates describe this session only; obtain the current
window handle and map the actual target coordinate for another layout. A guest
window handle is not used on the host. The tested mouse point was selected on
the full host-client preview, so it required no additional viewport offset.

No scan-code or injection API change was necessary for these four accepted
operations. A missing scan code is therefore not a necessary explanation for
the earlier failures. Earlier trial 18 sent all key events while the probe held
foreground; trial 19 kept the same backend and hold setting but activated the
selected Sandbox window from Arm.

The first Ctrl+O after-image still showed the closed state, while a later
read-only desktop observation and subsequent capture showed the chooser open.
No additional game input occurred between those observations. Its exact render
latency was not measured, so a fixed 350 ms evidence delay cannot be assumed to
prove acceptance or rejection. The second Ctrl+O after-image and both mouse
after-images show the expected state directly.

Evidence: `.work/desktop-probe-live-trial19-activate/session.json`, its original
PNGs, and the separate `acceptance-assessment.json`. There were zero guard calls
in all four operations. This establishes repeatability for these two local UI
actions in one active session, not background delivery or a production stress
test. Worker startup remained 3625–4391 ms; it is a harness overhead, separate
from native dispatch. A persistent initialized controller is the next latency
improvement to evaluate if the harness is retained for repeated actions.

## Measured time, not timeout settings

Twelve recorded error-only operations in trials 13, 14, 16, 17 and 18:

| Phase | Median | Observed range | Interpretation |
| --- | ---: | ---: | --- |
| Worker startup to ready | 3727 ms | 625–7484 ms | Fresh interpreter, imports and backend construction before dispatch |
| Key dispatch | 62 ms | 15–110 ms | Includes the selected experimental hold and event instrumentation |
| Post-dispatch settle | 344 ms | 343–360 ms | Requested 350 ms for visual evidence |
| After-image capture and saving | 999.5 ms | 218–1391 ms | Runs after input; delays result display |
| Guard calls | 0 | 0 in all twelve | No availability preflight or before-image gate |

These are mixed one-shot diagnostic trials, not a controlled production
performance benchmark. Medians of phases must not be added and presented as an
observed whole-operation median. The GUI countdown and Computer Use tool latency
are outside these worker timings. Earlier 10/15-second countdowns deliberately
allowed manual focus; they added that full delay before worker startup. The
default error-only input countdown is now zero. Capture/legacy defaults remain
three seconds. Explicit countdown overrides remain available.

The 45-second diagnostic startup setting is a deadline, not a sleep. Its
readiness deadline begins after synchronous `Process.start()` returns. Reports
record that call separately. Startup does not establish the client's state.
The worker-ready message describes the worker's ability to receive its single
command; it is not a game-availability check.

The derived sample list and min/median/max are in
`.work/probe-error-only-timing-analysis.json`. Each row identifies its original
session and report index. Samples include repeated manual arms in trial 18;
none have been silently counted as accepted game input.

## Hypotheses and discriminating tests

| Hypothesis | Assessment from observations | Prediction and next test |
| --- | --- | --- |
| Probe keeps keyboard focus, so keys go to the wrong host window | Confirmed for trial 18: all four key events in each of five recorded attempts saw foreground HWND 16584962 (probe), while selected Sandbox HWND was 2624852 | Activate the selected window once from Arm; trace all four events and inspect the original after-image. Require both the selected foreground HWND and the expected chooser transition |
| Long delay is mainly native key injection | Strongly contradicted in these trials: dispatch 15–110 ms versus startup 625–7484 ms, plus countdown and evidence work | Continue measuring dispatch separately; do not increase arbitrary delays to conceal delivery failure |
| A longer key hold alone repairs delivery | Weak support: a 50 ms hold still produced no reliably accepted response, and wrong foreground persisted | Hold duration constant while fixing focus; only compare hold durations once the recipient is established |
| Missing hardware scan code prevents Sandbox forwarding | Plausible but untested; current native backend supplies zero scan code | If correct foreground is demonstrated but the client still fails, compare mapped scan code versus zero, changing only that field and keeping the same operation/hold |
| The Sandbox client is frozen | Possible in some user-reported intervals, not established as the cause of these key failures; a direct Computer Use Escape previously closed the chooser | Use a local dialog transition as a positive control immediately adjacent to the probe trial; invalidate an unresponsive baseline rather than blaming the injection API |
| Native failure or privilege mismatch prevents injection | Unresolved alternative, not established by successful void API return | If focus and scan-code tests fail, investigate an input API with an observable delivery count; do not equate call completion with acceptance |

The recipient trace observes the failure; it is an opt-in diagnostic, off by
default, and never refuses or retries input. Production checks client
availability only after an operation reports an error.

## Live experiment performed

1. Establish chooser closed and responsive, with Paladjinn in the intended client.
2. Use the probe on the other monitor. Keep backend, capture method and 50 ms
   experimental hold constant. Arm Ctrl+O once; perform no manual focus action
   during the trial. The repair must activate Sandbox itself.
3. Require every key trace to name the selected Sandbox HWND and the original
   after-image to show the chooser open. A returned native call is insufficient.
4. Separately arm Escape from that open chooser and require it to close.
5. Repeat the pair to distinguish a consistent repair from a coincidental result.
   If foreground is correct but acceptance fails, retain both results and test
   scan-code delivery as the next single-variable hypothesis.

Trial 19 was launched before the zero-countdown default change, with an explicit
three-second countdown, and was used for the four successful operations above.
Trial 20's new process was stopped unarmed before trial 19 was armed, so it could
not steal focus during the experiment. No full bot loop, production retry or
isolation-setting change was used.

Focused automated validation after the changes: 327 tests passed across probe
UI, capture harness, input harness and error-only input suites. Ruff and
`git diff --check` passed. Added checks cover one-time Arm activation, no
scheduled input after activation failure, zero default error-only countdown,
retained explicit countdown, no second-screen hide or completion refocus,
negative monitor coordinates, non-gating diagnostic failures, and key release
when an experimental hold is interrupted. These validate code behavior with
fake native backends; they do not prove Sandbox acceptance.
