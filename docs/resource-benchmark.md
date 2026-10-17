# Resource-reader benchmark

`tools/compare_resource_readers.py` compares the shipped exact-scale preset with
the adaptive reader on the same frozen screenshot cases. Tesseract is an
explicit optional technique because its installed version and latency can differ
between machines.

The current corpus and first evaluation of its 32 new sources are documented in
[the 2026-10-17 collection review](resource-corpus-2026-10-17.md). Keep those
results separate from the prior 19 regression sources. Verbose original-only
comparison and latency reports are downloadable artifacts of the
`Frozen screenshot corpus baseline` workflow; concise summaries belong in Git.

Run the comparison from the repository root:

```bash
mkdir -p reports
python tools/compare_resource_readers.py \
  --ocr \
  --json-out reports/resource-benchmark.json \
  --markdown-out reports/resource-benchmark.md
```

The default runs only preserved real source images. Acceptance uses reviewed
real full screenshots annotated `target_family` or `visually_equivalent` under
`render_equivalence`, plus the real panel-closed negative as a separate safety
control. A reviewed real full screenshot with multiple legitimate resource
panels is an ambiguity safety control: the reader must abstain from selecting a
unique layout and from returning all four values. Its visible values remain
annotated for audit, but are excluded from numeric-coverage denominators. Crops,
visibly different references and unknown equivalence remain diagnostics. A
source transcode may have reviewed visual equivalence, but it is not evidence of
native resolution, DPI or an exact client build.

Historical artificial transformations require `--include-generated-diagnostics`.
They are outside the current real-image collection and acceptance workflow.

Generated diagnostics never affect `safety_passed`, the acceptance score, or
the acceptance deltas. They are correlated replay probes and do not establish
supported in-game render configurations. The dense ROI cases additionally use
truth-derived crops and therefore cannot serve as end-to-end acceptance
evidence.

The report keeps four evidence categories separate:

- `original_full`: preserved full screenshots;
- `supplemental_crop`: preserved source crops, which broaden visual evidence but
  do not represent additional full-client captures;
- `generated_perturbation`: deterministic robustness checks derived from a
  preserved source.
- `lightweight_numeric_roi`: dense scale-bank stress cases cropped to the
  annotated panel before scaling. These are generated recognition stress tests,
  not full screenshots or additional client evidence.

Train and heldout summaries are also separate in JSON. A generated case retains
its source ID, source SHA-256, transformed-pixel SHA-256, transformation name,
and split.
Variants of one source are correlated observations. The matrix size must not be
interpreted as the number of independent client captures or installations.

The historical opt-in diagnostic matrix contains the current originals plus
generated tests and an explicit negative-source exclusion. Ambiguity controls
do not produce generated variants. This count is not an acceptance sample size.
The default comparison runs the preserved originals in the current manifest and
selects acceptance evidence from their reviewed equivalence and
capture-provenance annotations.

## Representative use and scorecards

The headline table and gates use only acceptance originals, the real negative
safety control, and reviewed ambiguity safety controls. The legacy all-case
aggregate remains in JSON for schema-v1 comparison and is not estimated gameplay
reliability. Source-macro scorecards
keep accepted target-family or reviewed visually equivalent originals separate
from visibly different or unknown references, supplemental crops, generated
full-source stress, and cropped recognition stress. Equivalence is never inferred
from a filename, apparent version number, dimensions or visual similarity that
has not been reviewed and recorded in the manifest.

Each field records its rejection detail, bounded reader diagnostics, failure
stage, raw match score and `calibrated_correctness_probability: null`. Match
scores are not probabilities. Conditional numeric metrics use only fields with correct location
correspondence, while overall coverage retains discovery failures. Safety and
coverage gates are separate: zero incorrect accepts does not make an
all-unknown reader useful, and no product coverage threshold is invented here.

The original fourteen sources have been examined in diagnosis. Their historical
train/heldout names are preserved for reproducibility, but they are known
regression cases. Five new real sources were selected and manually annotated
before reader inference on 2026-10-14. Reports must identify any later
failure-driven tuning; affected sources then become train data. New untouched
captures are required for a fresh generalization estimate. Loader checks
keep derived IDs, source hashes and splits together and reject identical source
hashes assigned across splits. Variants from one source remain correlated.

To isolate label OCR from numeric OCR on the preserved originals, run:

```bash
python tools/compare_resource_readers.py --ocr \
  --ocr-ablation labels-only --ocr-ablation digits-only \
  --json-out reports/resource-benchmark.json \
  --markdown-out reports/resource-benchmark.md
```

For capture, annotation, event deadlines and production-tracker replay, see
[resource sequences](resource-sequences.md). The [collection protocol](resource-benchmark-collection.md)
defines representative sessions, native scale/DPI evidence, difficult negatives,
annotation review and source-level split discipline. Native-session acceptance
requires actual recordings; repeated static screenshots do not provide it.

## Metrics

`exact_coverage` is exact accepted values divided by eligible expected fields.
`numeric_acceptance_coverage` is all accepted values, exact or wrong, divided by
eligible fields. `accepted_value_precision` is exact values divided by accepted
values. It is `null` when a reader accepts nothing, so an all-unknown reader is
never reported as perfectly accurate. `false_acceptance_share` is wrong accepted
values divided by accepted values.

Unknown fields and incorrect accepted values are separate counts. The latter is
the central safety regression. Location correspondence requires the detected
region to cover at least 90% of its annotated numeric box and rejects a region
that contains the center of any other visible resource value box. This prevents
a whole-panel or wrong-row region from passing when values happen to repeat.
Negative screenshots do not enter numeric accuracy; finding a panel in one is a
false panel acceptance.

An ambiguity safety control declares all four fields in
`expected_final_unknown_fields`, records a nonempty `abstention_reason` and
`ambiguity_evidence`, and sets `expected_discovery` false. Finding any unique
layout is a false panel acceptance even if every subsequent field read is
unknown. Returning a known value is an additional unsafe ambiguity acceptance.

`numeric_mismatch_accepted` counts decoded numbers unequal to truth.
`accepted_location_mismatch` counts numerically equal accepted values whose
detected region does not meet the strict containment gate; these remain outside
strict exact coverage. `incorrect_accepted` is their union. The report groups
translation, width reflow, full scaling, dense ROI scaling, ambiguity, and each
cached invalidation family separately.

`cold_total_ms` is the comparable cold path: reader construction, full-frame
discovery/import, and the first guarded numeric read. A missing or ambiguous
panel stops after discovery. Adaptive `discovery_ms` isolates its search phase;
the legacy public preset does not expose search separately, so its value is
null. `cached_steady_ms_median` covers guarded reads on unchanged ordinary
frames. `cached_invalidation_ms_median` covers one-shot stale-layout validation
on generated sequence frames; these are not pooled into one latency statistic.
The legacy preset has no cached-layout API, so its cached timing remains null.
`recovery_ms` is populated only for generated frame sequences. It measures
synchronous `ResourceReader` rediscovery plus the first guarded read on the
changed frame; it does not measure the background tracker's retry/backoff
wall-clock delay. Otherwise it remains null. These phases should not be compared
as though they perform the same work.

The JSON includes Python, platform, OpenCV, NumPy, Pillow, pytesseract, and
Tesseract versions; technique options; the manifest hash; ordered case IDs; and
per-case hashes. Wall-clock timings will vary by host even though the cases and
reader outputs are reproducible.
The implementation section hashes the reader, visual backend, corpus generator,
comparison scorer, and complete profile asset bundle so a report can be tied to
the code and assets that produced it.

The baseline-to-adaptive deltas compare complete reader techniques. They do not
isolate the causal effect of an individual template bank, digit rule, geometry
change, or OCR fallback. Isolating one implementation change requires a separate
controlled ablation on the same case IDs and implementation hashes.

## Before-and-after repair evidence

Archive a report before changing the reader, then rerun the default preserved
original comparison into the usual final report paths. Run generated diagnostics
separately only when they answer a specific diagnostic question. A valid
iteration comparison requires identical
ordered case IDs, case SHA-256 values, source SHA-256 values, manifest hash,
corpus-generator hash, and scorer hash. Reader and profile hashes identify the
implementation change and are expected to differ. Compare each technique's
`incorrect_accepted`, `numeric_exact`, `unknown`, and
`numeric_acceptance_coverage` at aggregate, evidence-category, and transform-family
levels. Do not splice old and new technique rows when reporting latency; a full
rerun keeps execution conditions and post-run provenance checks consistent.

A repair driven by failures in the archived iteration is regression evidence on
those known cases. It is not a heldout estimate of how the repair generalizes.
Keep train and heldout summaries separate, and identify the exact known cases
that motivated the repair.

## Latency and runtime

Run this separately from the comparison to avoid CPU contention in the timing
measurements:

```bash
python tools/benchmark_resource_latency.py --warm-reads 50 \
  --json-out reports/resource-latency.json \
  --markdown-out reports/resource-latency.md
```

This uses unchanged accepted full screenshots, the real negative control, and
reviewed ambiguity controls. Positive cases measure full-frame discovery and
every individual guarded four-resource cached read, verify the values and row
associations, and report p50/p95/max. Safety controls perform discovery only and
are excluded from positive cached-read and numeric denominators.
Use `--reading-budget-ms` to declare an engineering budget before a run; the
benchmark does not infer a production SLO from the observed results.

Repeated static-image reads measure offline algorithm cost. They exclude capture,
game-frame delivery, action scheduling and physical input. Recovery and freshness
claims require [recorded sessions](resource-sequences.md), including actual panel
movement or closing/reopening in the game. The historical
`benchmark_resource_runtime.py` performs artificial relocations, requires an
explicit diagnostic flag, and has no acceptance effect.
