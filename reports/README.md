# Adaptive resource-reader review

This follow-up to PR #4 adds semantic resource discovery, guarded numeric reads,
panel-selection UI, cached-layout invalidation and background reacquisition. It
also replaces generated stress as the headline benchmark with preserved real
screenshots and adds native-session recording/replay tools.

## Real evidence

The corpus contains **19 unchanged source images**: 7 reviewed positive full
screenshots, 1 closed-panel negative, 1 natural two-panel ambiguity control,
9 source crops, and 1 visibly different older Beta full-window reference.
Source URLs, filenames, player names, hashes, dimensions, manual truth and
visual-equivalence evidence are retained in the
[manifest](../tests/fixtures/screenshots/manifest.json) and
[visual review](../tests/fixtures/screenshots/visual-review.json).

No local resizing, relocation, cropping or occlusion contributes to acceptance.
Historical artificial stress remains explicit opt-in diagnosis. Kalibra and PvP
were corrected from full screenshots to source crops after visual review showed
truncated or uncertain client extent. Publisher transcodes are preserved as
received; their dimensions do not prove native game resolution or DPI.

## Final recognition

All five techniques ran on all 19 originals with the same pixels and numeric
truth. Exactness requires both the correct value and correct row association.
The final report passed its integrity and safety checks.

| Reader | Primary values | All eligible original values | Incorrect accepted |
|---|---:|---:|---:|
| PR #4 exact-scale preset | 4 / 28 | 4 / 68 | 0 |
| Adaptive templates | 28 / 28 | 67 / 68 | 0 |
| Adaptive + full OCR | 28 / 28 | 67 / 68 | 0 |
| Adaptive + label OCR only | 28 / 28 | 67 / 68 | 0 |
| Adaptive + digit OCR only | 28 / 28 | 67 / 68 | 0 |

Both real safety controls abstained from selecting a layout and returning known
values. The remaining unknown is Soul in the visibly different Beta reference;
it stays in the diagnostic denominator. The report's missing-panel count includes
expected control abstentions, not just positive-image misses.

The [initial expanded-corpus run](iterations/iteration-03-real-originals.json)
recorded 24/28 primary template values and 27/28 with label OCR. The Calmera
failure exposed label raster-phase loss, a weak comma, and a digit ambiguity at
the median scale. Bounded label refinement and same-text corroboration recover
all four values while retaining the final thresholds. The same generic label
change also recovers three Beta reference values.
[Field-level before/after evidence](diagnosis/real-original-gaps.json) verifies
identical source pixels and numeric truth. Calmera and Beta moved to train after
failure inspection; this is regression evidence, not an untouched holdout claim.

## Reading cost

The separate [latency run](resource-latency.md) passed on the seven positive
originals and both controls. **350 complete four-resource cached reads** were
all exact with correct row association: p50 **7.99 ms**, p95 **26.17 ms**, maximum
**35.39 ms**. Each positive source contributed 50 reads.

Cold positive discovery took **2.35–6.39 seconds** (median 2.60 s). Negative and
ambiguity controls took 9.78 s and 5.54 s respectively. Discovery runs outside the
action loop; these timings must not be confused with cached-read latency. No
production timing budget was inferred from the results.

This is offline algorithm cost on repeated unchanged images. It excludes capture,
frame delivery, action scheduling and input. The native recorder/replay toolkit
is implemented and tested, but no actual native gameplay recording is available.
Live recovery, freshness, Windows DPI and physical-input behavior remain
unverified. Exact client builds are unknown; visual interchangeability is scoped
to the observed resource interface.

## Validation and reproduction

The full suite passed **841 tests**, with one Windows GUI smoke test skipped on
Linux. Ruff, compilation and `git diff --check` passed. Independent review
checked duplicate-panel rejection, numeric recovery guards and session semantics.

Run the measurement commands sequentially:

```bash
python tools/compare_resource_readers.py --ocr \
  --ocr-ablation labels-only --ocr-ablation digits-only \
  --json-out reports/resource-benchmark.json \
  --markdown-out reports/resource-benchmark.md
python tools/benchmark_resource_latency.py --warm-reads 50 \
  --json-out reports/resource-latency.json \
  --markdown-out reports/resource-latency.md
python -m pytest -q
python -m ruff check .
```

- [Final comparison](resource-benchmark.md) and [case JSON](resource-benchmark.json)
- [Latency samples and provenance](resource-latency.json)
- [Test output](pytest.txt)
- [Benchmark methodology](../docs/resource-benchmark.md)
- [Native recording/replay](../docs/resource-sequences.md) and [collection protocol](../docs/resource-benchmark-collection.md)
- [Historical recognition diagnosis](recognition-analysis.md)

The archived artificial matrix and relocation timings are historical diagnostics,
outside current acceptance. This small public-image corpus supports the measured
cases; it is not a gameplay failure-rate estimate.
