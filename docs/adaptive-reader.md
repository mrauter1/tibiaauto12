# Adaptive resource reader

The adaptive reader locates the Health, Mana, Soul Points, and Capacity rows in
an immutable client screenshot. It uses captured-pixel coordinates and returns
`UNKNOWN` when geometry, labels, or digits cannot be verified. Discovery is the
expensive path. A runtime should cache the returned `PanelLayout`, use `read()`
for subsequent frames of the same size, and rediscover after invalidation.

```python
from core.perception import Frame
from tibia_client.adaptive import ResourceReader

reader = ResourceReader()
result = reader.discover(frame, use_ocr=False)
if result.layout is not None:
    readings = reader.read(frame, result.layout, use_ocr=False)
```

OCR is disabled by default and is only attempted when callers pass
`use_ocr=True`. Ordinary template and digit recognition therefore has no
Tesseract runtime dependency.

Discovery keeps rendered font family separate from position and scale. The
alternate label family has a bounded RGB refinement path, and verified alternate
anchors enable appropriately scaled digit and comma assets. A continuous panel
divider can be removed as a competing column only when every discarded run is
the narrow divider stroke; complete duplicate value columns remain ambiguous.
Custom number readers keep the existing `read()` signature unless their concrete
class explicitly declares `supports_font_family = True`.

Cold discovery searches Health and Capacity across the full frame and completes
all four labels inside bounded candidate neighborhoods. Seed windows are merged
only by complete containment, so a nearby larger panel cannot disappear. Failed
local geometry falls back to the complete search.

Primary-font proposals also retain RGB antialiasing during bounded phase
refinement. A weak thousands separator may recover only when the identical
number passes all normal checks after a small foreground-threshold change.
An ambiguous digit near a scale-bank boundary requires the same text to pass
at both neighboring scales. The original screenshot bytes remain unchanged;
these are internal recognition operations, not generated benchmark captures.

## Screenshot corpus

`tests/fixtures/screenshots/manifest.json` is the source of truth for benchmark
inputs. Boxes are absolute `[left, top, right, bottom]` coordinates with an
exclusive right and bottom. Resource boxes tightly enclose the displayed
numeric glyphs. The benchmark checks every listed file's existence, SHA-256,
dimensions, annotations, and classification before running the reader.

Entries marked `confirmed` contain four visually verified target-family values.
`similar` entries are positive transfer cases with visible resource rows; every
readable positive is required to have `expected_discovery: true`, and its misses
remain visible in the metrics. A reviewed multi-panel ambiguity control instead
requires no unique binding and all four readings unknown. `negative` entries must not produce a layout. `coverage` distinguishes
full screenshots from supplemental panel crops. `data_origin` separates real
screenshots from generated perturbations; generated samples must never be
reported as real evidence. The `train` and `heldout` split is also preserved in
the report. The original fourteen sources have now been inspected during diagnosis; they
are known regression cases. Five new sources were selected and annotated before
inference on 2026-10-14.

Run the full corpus benchmark separately from the normal test suite:

```console
python tools/compare_resource_readers.py --ocr
```

The report includes layout discovery, correspondence between inferred regions
and annotated value boxes, exact numeric matches, unknown readings, incorrect
known readings, discovery latency, and cached warm-read latency. Acceptance uses
real original full screenshots with documented visual equivalence and negative
or multi-panel ambiguity controls. Crops and unknown/different interfaces remain reference diagnostics.

The pytest suite runs every manifest entry once and adds deterministic
perturbations for relocation, scaling, value-column reflow, duplicate panels,
label and digit occlusion, adjacent decoy numbers, changed values, clipped
captures, and stale layouts used against a different frame size.

The corpus distinguishes full screenshots, source crops, and real negatives.
Visual review corrected two previously misclassified source crops. Crops broaden
reference evidence but do not establish full-client resolutions. The manifest
records exact-version evidence separately from observed visual equivalence.

The [comparison benchmark](resource-benchmark.md) runs the PR #4 exact-scale
preset, adaptive templates, and optional adaptive OCR over identical cases. Its
reports separate accepted full screenshots from supplemental reference crops
and historical diagnostic stress, and preserve both misses and incorrect accepted readings. The rendered
scale search bank is not a guarantee that every interpolated font will be read.
Unknown results at unsupported scales remain a coverage limitation.
Source-balanced scorecards and separate OCR label/digit ablations are documented
there. The [session recorder and replay benchmark](resource-sequences.md) adds
timed production-tracker evaluation of real recordings. No native session is
claimed until actual capture evidence is available.

## Runtime and input

`AdaptiveResourceTracker` performs discovery outside the action loop and checks
cached layouts against fresh captures. A changed or ambiguous panel invalidates
dependent readings; replacement bindings are published together under a new
layout revision. The workflow cancels queued dependent actions when it observes
that revision change. This does not remove the time between capture and physical
input.

`CaptureInputMapping` validates coordinate conversion separately. The production
desktop adapter currently accepts only verified 1:1 capture/client mapping for
input; it rejects resized or cropped captures instead of guessing a transform.
Offline screenshot detection does not establish a live input mapping.
