# Hunt Workflow

Hunt Workflow is the operator panel for a closed-loop hunting cycle. It combines saved
routes, supply thresholds, guarded station actions, and visual evidence into one reviewed
configuration. Opening the panel is inert. It does not capture the client or send input.

This feature is separate from the portable HuntPack version 1 format. A HuntPack contains
portable CaveBot and support settings and deliberately has an inactive supply policy. A hunt
workflow is a machine-local configuration tied at validation time to the current calibration
and compatibility profile. Do not distribute it as though its coordinates or templates apply
to another client layout.

## Configure a cycle

1. On **Plan & routes**, give the plan an identifier and assign saved Route Studio routes for
   outbound travel, hunting, and return travel. Set a finite refill-round limit so a station
   loop cannot continue forever.
2. On **Supplies**, add each count sensor and its three thresholds. The emergency reserve must
   be less than or equal to the return threshold, which must be less than or equal to the
   refill target. Optional supplies do not block the whole workflow when their reading is
   unavailable.
3. On **Actions & confirmation**, assign each press, click, or drag to a lifecycle phase.
   Every action requires a visual confirmation condition. Press actions name a key; mouse
   actions name client-local bindings. A drag names both source and destination bindings.
4. On **Visual bindings**, map a binding ID to a detector ID and a client-local
   `[left, top, right, bottom]` region. Then define the detector. A template detector has a
   template and a relative search region. A count detector has digit templates `0.png` through
   `9.png` and a relative count region. Every detector also has a context template and context
   region that guard against reading pixels from the wrong panel. Detector regions use the
   binding's top-left corner as their origin; binding regions use the full client's origin.
5. Choose **Validate**. Validation resolves routes and templates, checks every cross-reference,
   and verifies the current local profile. The panel shows the exact failing field or reason.
   Correct the draft and validate again; an old validator error is cleared at the start of each
   attempt.
6. Choose **Save**. Template paths saved inside the workflow directory remain relative to that
   directory. Optional targets, navigation settings, loot policy, and detector tuning can be
   reviewed in **Advanced JSON**; the structured tabs remain the normal configuration flow.

On **Visual bindings**, **Detect resource panel from saved capture** finds the Skills resource
panel by its health, mana, soul, and capacity labels across supported UI scales, identifies the
shared value column, and reads each discovered field. It adds adaptive count detectors to the
open draft without overwriting existing binding or detector IDs and does not save the workflow.
At runtime, these detectors use a non-blocking tracker. A new or invalidated layout reports an
`unknown` reading while a background worker discovers and checks the panel; the scheduler does
not wait for discovery. The tracker then reads the named field from current frames while the
layout remains valid. Their JSON contains an `adaptive` declaration such as
`{"reader":"paulistinha-skills-v1","field":"resource_health"}`. That declaration selects the
bundled semantic label and number reader. Its `field` and optional `use_ocr` values, together
with the detector's `max_digits`, affect adaptive reads. The adjacent legacy context template,
digit templates, count region, and match scores remain in the document for ordinary-detector
compatibility; they do not tune adaptive recognition.

When an adaptive detector is selected, the structured editor labels that mode and makes those
legacy fields read-only. **Convert to ordinary count** explicitly removes the `adaptive`
declaration and makes the ordinary template, region, digit, and score fields active. After that
conversion, you can also change its kind to an ordinary template detector. Advanced JSON can
edit the adaptive declaration and `max_digits` directly, subject to normal validation.

Normally discovery scans the whole saved client capture. If more than one plausible panel or
value column is present, a bounded offline chooser highlights every verified candidate in the
same immutable saved frame. Click the intended panel once to finish detection; its fields and
readings then appear in the setup overlay. The draft remains unsaved until you press **Save**.
The candidate choice does not recapture the client or reopen the image, and it cannot replace
existing binding or detector IDs.

The **Advanced resource detection fallback** accepts a client-local panel region as
`left, top, right, bottom` when automatic candidates still cannot isolate the intended area.
Manual coordinates are not part of the normal setup path. **OCR fallback** is optional and off
by default. When selected, the workflow saves `"use_ocr": true` in each adaptive declaration.
Panel discovery and numeric OCR fallback run in the background; ordinary scheduler reads remain
non-blocking. Numeric OCR results are reused only for the same verified layout and exact value
region pixels. An installed OCR provider is required for that fallback, while the bundled label
and digit readers work without it. **Add exact legacy preset** remains available for the original
fixed-scale layout.

The saved image must contain the full client-local capture and have the exact dimensions
recorded by the current calibration. Wrong-size desktop captures are rejected; matching
dimensions alone cannot prove the image source, so use a client-only capture and review the
overlay and any missing, ambiguous, or unexpected readings. The field boxes and values shown
after import come from that offline image. A value wider than that detector's configured
`max_digits` is shown as `unknown`, including in this preview. Offline values are not
current-client evidence and do not satisfy calibration, Compatibility, Inspect, or Start gates.

Save, Validate, Inspect and Start include intentional edits in the open forms; pressing
Add/update first is optional. If a form cannot be parsed, the editor preserves the draft and
your entered values so you can correct it. Save accepts unfinished drafts; validation and Start
check whether a workflow is ready to run. Removing an entry clears its form, and loading a new
document replaces the old forms. Save As refreshes the displayed template paths after rebasing.

The four station groups are `prepare`, `deposit`, `sell`, and `refill`. Travel also supports
actions immediately before and after the outbound and return routes. Confirmation fields are
`present` or `count`. Relative comparisons (`changed`, `increased`, and `decreased`) do not
take a comparison value.

When loot is enabled, `loot.destination_count` is required and must differ from every source
item sensor. Source and destination counts must use the same item units. A transfer is
machine-confirmed only when the source count decreases and the destination count increases by
the same positive amount; a one-sided or unequal change remains unconfirmed.

## Author templates from a saved frame

Hunt Workflow can crop a template from a local image file:

1. Save the workflow once to establish its directory. If you intend to use **Save As**, do it
   before authoring templates. A later Save As rebases existing asset paths to keep them pointed
   at the same files.
2. In **Desktop Compatibility Studio**, capture a preview and choose **Save displayed frame**.
3. In Hunt Workflow, choose **Load saved client capture** and select the binding whose pixels
   the crop belongs to. With no selection, the editor can use the detector's sole matching
   binding. The crop dialog identifies the detector, purpose and binding before you save.
4. Choose whether the crop is the match template, context template, or a digit, then choose
   **Select crop and save template**. Drag a non-empty box and save the PNG inside the workflow
   directory. Each detector receives its own suggested subdirectory. Digit crops use the names
   `0.png` through `9.png` in that one directory. The editor puts the relative path or folder in
   the detector form and, when the crop lies inside the selected binding, calculates its
   binding-relative box.
5. Add or update the detector, validate, and run **Inspect evidence**. A count detector requires
   all ten digit PNGs. Its foreground color and tolerance determine which pixels participate in
   digit recognition; review non-default values in Advanced JSON when the client text is not
   white or has antialiasing that needs a different tolerance.

Loading and cropping a saved image is offline authoring. It does not prove that the current
client is visible, fresh, aligned, or displaying the same pixels. A crop is limited to 512 by
512 pixels and is never used as a captured frame or replay source.

The crop dialog keeps the detector, binding and crop purpose it was opened for. A later
selection or document change cannot attach the image to a different detector. Validation and
file errors preserve the crop selection for another attempt.

## Inspect live evidence

**Inspect evidence** begins an explicit observation-only probe through the workflow controller.
It may capture the configured client, but it sends no input. Inspection reports each binding as
`known`, `unknown`, or `error` and preserves the detector detail. Unknown is not converted to
zero or absence. For example, a known count of zero is distinct from a digit tie that produced
no value.

A detector match score is an image-match score, not a probability. A template detector may
report known absence only when its context guard is valid. A count detector can report a known
zero. Missing context, clipping, and ambiguous matches remain unknown and block decisions that
need that evidence.

The displayed frame and binding overlays come from the configuration snapshot submitted to that
inspection. Configuration stays locked until the probe finishes, so later edits cannot relabel
an earlier result.

Ambiguous context or excess target matches include competing boxes and scores in the preview.
Use these boxes to narrow the detector region or choose a more distinctive template. Overlapping
but distinct matches remain ambiguous; they cannot authorize an action. These diagnostic details
also survive trace recording and replay, and existing traces without them remain readable.

Inspection does not validate action effects and does not arm the workflow. Stop an active
workflow before inspecting.

## Run and intervene

Choose **Start** after validation. Start compiles an immutable snapshot of the current document.
Configuration controls stay disabled while the workflow is active. The status area shows the
current phase and reason, supply readings, pending confirmation, and any manual reconciliation
request. The existing Hunt Console remains the place for the global **STOP ALL** control.
Start and Inspect preparation run outside the UI thread. While either activation is pending,
**Cancel pending** invalidates that exact queued request before the window can close. A Stop All
that occurred after the request was queued also invalidates it; the older request cannot start a
new generation afterward.

Closing the Hunt Workflow panel hides it; it does not stop the workflow or discard the loaded
draft and filename. Opening Hunt Workflow again restores the same panel and its controls for the
original generation. Use **Stop workflow** to stop that generation, or Hunt Console **STOP ALL**
for the global stop.

- **Return after fight** requests a return after the current combat boundary.
- **Return now** cancels pending hunt/combat input and begins the authored return path, using
  fresh route evidence and the configured guards. Emergency reserves use this same immediate
  path. Independent support features continue running. An input already dispatched to the
  client cannot be undone; an uncertain inventory-changing action still needs reconciliation.
  Return requests only increase in urgency. During outbound travel, the request is reported as
  queued until that authored route completes; the controller does not guess a reverse route.
  Return and town-side phases report that the request is already being satisfied.
- **Stop workflow** stops this workflow only.
- **Re-check evidence** requests another recovery or confirmation check without claiming that
  an operator corrected the client. Route recovery checks automatically as new frames arrive.
- **I reconciled manually** means the operator personally inspected and corrected the client
  state. This is recorded as `operator_reconciled`. It is never labeled machine-confirmed, and
  fresh evidence is still required before automation proceeds.

When **Resume saved cycle** is selected, Start may resume a compatible checkpoint. The
controller still checks the configuration and local binding identity; stale or mismatched state
is not resumed silently.

Pausing the existing run preserves its route progress, combat state and remaining timers. Resume
waits for a newer valid observation, then continues without requiring a reached waypoint.
Restarting from a checkpoint reconstructs lost route state instead: a unique recognized anchor
resumes automatically, while multiple candidates let you select the intended waypoint with
**Resume here**. That selection is checked against fresh evidence before movement resumes.
Actions cancelled while still queued can retry with fresh evidence. Purchases and transfers
that may already have reached the client retain their original confirmation baselines. If
dispatch feedback was interrupted, an unchanged counter does not authorize a retry: the effect
must be confirmed from new evidence or reconciled manually.

## Workflow document boundary

The version 1 root object uses schema `tibiaauto.hunt-workflow`. It contains:

- `plan`: lifecycle routes, supplies, guarded actions, loot settings, and refill limit;
- `bindings`: sensor IDs mapped to a client-local region and visual detector name;
- `visual`: template or digit detector definitions with relative asset paths;
- optional `targets` and `navigation` settings.

Map and battle-list regions come from the current calibrated profile and are not duplicated in
the workflow document. The current profile, compatibility evidence, window handles, captured
frames, checkpoints, and live readings are not embedded as proof in the JSON file. Validation,
Start, and Inspect resolve the current machine-local state each time.
