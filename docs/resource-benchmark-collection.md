# Collecting representative resource-reader evidence

Acceptance uses preserved real screenshots and recorded gameplay frames.
Screenshot correctness alone is not a measurement of success during ordinary
gameplay. Use the [session recorder and replay benchmark](resource-sequences.md)
for native capture sessions.

The [latest expansion and 80/20 split](resource-corpus-latest-2026-10-17.md)
adds 30 complete contemporary captures. The current authoritative sidecar puts
57 of 71 full screenshots in development and 14 untouched new captures in
test, with ten supplemental crops outside the ratio. Older manifest split
labels remain historical metadata.

The [2026-10-17 public collection](resource-corpus-2026-10-17.md) adds 31 full
screenshots and one partial reference, with a pre-inference annotation freeze,
source/session grouping, duplicate audit and navigation contact sheets. It is
static screenshot evidence; it does not replace the native-session work below.

Do not resize, crop, paste, sharpen, or otherwise manufacture benchmark inputs.
A proposed rendering transform is admissible only after comparison with actual
captures proves that a documented, supported game configuration renders the
same pixels. No current generated transform has that proof. Historical artificial
stress reports are diagnostic artifacts, outside acceptance.

## Capture sessions

Record the complete client through the capture path used by the application.
Use the recorder's capture mode or import original full frames with their actual
capture timestamps. Capture mode is read-only: it must never send input or
change the client settings for the operator. Raw captures are unreviewed drafts;
recording successfully is not a benchmark pass.

Capture actual configurations used by operators before deliberately uncommon
ones. Record the client build, interface language, font/theme when known, client
resolution, UI scale, Windows DPI, capture backend, and any crop or resize in
the capture pipeline. Unknown settings stay null. Equal dimensions do not prove
that a capture is client-local or that its input coordinates are valid.

Collect resolution and UI/DPI scale changes in the game itself. Preserve the
resulting original files. A downloaded image's dimensions do not prove the
native client resolution or the absence of publisher resizing.

A first collection should cover these scenarios across multiple sessions and
characters, with repeated independent captures of the intended client:

| Scenario | What to preserve | What to annotate |
|---|---|---|
| Ordinary gameplay, stable layout | Full frames including nearby panels and chat | Readable values, exact rows, geometry and time |
| Resource use and recovery | Damage, healing, mana use/regeneration and Soul changes when observable | First frame showing the new value; do not backdate the change |
| Digit transitions | Zero, repeated digits, changing digit count and separators | Entire visible number, including punctuation |
| Move or resize panel | Frames before, during and after the operator's change | Layout-change onset and identity of the intended panel |
| Close and reopen panel | Disappearance, absence and reappearance | Visibility, expected unknown periods and recovery onset |
| Occlusion and clipping | Tooltips, overlapping windows and partial glyphs | Each affected field's readability and reason |
| Similar text and multiple panels | Chat label phrases, numeric tables, duplicated resource panels | Intended panel identity or genuine ambiguity |
| Capture disruption | Pauses/freezes and recovery through the actual capture path | Independent freshness evidence when available |

Record what actually happens during gameplay or controlled in-client demonstrations.

Compare each candidate with the target's resource labels, row order, numeric
alignment, panel chrome, surrounding controls, font rendering and client edges.
Record visible layout/OS/scale variation and any version-defining incompatibility.
When no relevant incompatibility is found, versions may be treated as visually
interchangeable for the inspected resource-reader contract. Keep the exact build
unknown unless independently evidenced; cropped views cannot establish whole-client
equivalence. Store that review in `render_equivalence`, separately from values.

## Annotation and review

Annotate the intended panel and complete numeric regions using half-open pixel
boxes. A correct value in the wrong row is an error. Include resource-label boxes
where available, and annotate each resource independently.

For every evaluated field, decide whether the current pixels support a numeric
value. Distinguish unreviewed, readable, hidden, clipped, ambiguous, and visually
insufficient evidence. An earlier known value is not ground truth for an
obscured frame. Independent review is particularly useful for tiny digits and
ambiguous separators; unresolved annotations remain excluded or explicitly
unknown, with their reasons visible in the report.

A local capture timestamp does not prove that upstream pixels are fresh. Repeated
identical frames can be legitimate. If freeze status cannot be established from
independent evidence, mark it unverified. Image-reader replay does not, by itself,
validate the application's complete capture-health or physical-input path.

Declare timing deadlines before evaluating a candidate, based on the consuming
feature. Health response, Soul confirmation and startup readiness need not share
a deadline. Report startup discovery, steady reading and recovery separately;
near-real-time steady reads do not imply near-real-time reacquisition.

## Development and evaluation splits

Use capture-session/source lineage as the split unit. Adjacent frames and any
historical derivatives from a source all belong to the same split. Separate
sessions from the same recording cannot be made independent by
renaming files. Check source hashes as well as identifiers.

The original fourteen screenshot sources have been inspected during diagnosis.
Their old `train`/`heldout` names remain for reproducibility, but both now serve
known regressions. Five new sources were selected and annotated before inference
on 2026-10-14. Keep their first evaluation and record any later tuning. Acquire
new untouched target-client recordings before claiming generalization of fixes
chosen from known failures. Once a failure guides a fix,
retain it as a regression and refresh the untouched evaluation evidence.

## Reporting and acceptance

Publish separate scorecards for native full-client sessions, accepted target or
visually equivalent original full screenshots, and reference-only crops or
visibly different interfaces. Keep historical artificial stress outside acceptance.
Within each, show pooled and source/session-macro
results. Do not invent production-usage weights; report the collection mix until
actual usage data supports weights.

Track incorrect accepted values/associations, correct fresh readings before
onset deadlines, readable-target coverage, invalidation/recovery delays, longest
unknown intervals and latency percentiles. Report each resource separately.
A zero-error safety result is not a coverage pass or a statistical guarantee.
Show failed/missed deadlines and incomplete sessions rather than dropping them.

For every reader change, run the frozen real-screenshot set and affected recorded session
scenarios. Compare OCR for labels only, numbers only and both when attributing
its effect. Keep previous known errors and all intentional-unknown cases as
required regressions. Broader discovery can expose previously hidden numeric
errors, so panel recall alone cannot approve a change.
