The PR #4 preset depends on one reference rendering. Moved panels, different glyph sizes and shifted value columns need fresh semantic bindings before resource-dependent automation can trust them.

This follow-up adds:

- Multi-anchor Skills discovery, per-panel scale and value-column resolution, guarded numbers, and optional bounded OCR.
- Panel-selection UI, cached validation, invalidation and background reacquisition. Capture-to-input mapping remains independently verified.
- Bounded font-phase, divider and numeric-corroboration fixes, with exhaustive global ambiguity checks.
- Acceptance based on **19 unchanged real images**: 7 positive full screenshots, 1 closed-panel negative, 1 natural two-panel control, 9 source crops and 1 older Beta reference. Full source information, hashes and visual reviews are retained. Generated stress is excluded from acceptance.
- Source-balanced scorecards, explicit abstention/safety metrics, OCR ablations, per-reading latency, and native-session recording/replay with provenance and freshness checks.

Two formerly overclaimed full screenshots are correctly classified as crops. Visual equivalence is documented separately from exact client-version and native-resolution evidence.

### Results

| Reader | Exact primary values | Exact across all eligible originals | Incorrect accepted |
|---|---:|---:|---:|
| PR #4 preset | 4/28 | 4/68 | 0 |
| Adaptive templates | 28/28 | 67/68 | 0 |
| Full OCR, label-only OCR, digit-only OCR | 28/28 each | 67/68 each | 0 |

Both real safety controls abstain. Soul in the older Beta reference remains unknown. The initial new-corpus run had 24/28 template reads; its failures and the final changes are preserved. Calmera and Beta were moved to train after diagnosis, so these repairs are regression evidence.

**350 four-resource cached reads** were exact: p50 **7.99 ms**, p95 **26.17 ms**, max **35.39 ms**. Positive cold discovery is **2.35–6.39 s**, handled outside the action loop. Timings exclude capture and input.

Validation: **841 tests passed**, one Windows GUI smoke test skipped on Linux; Ruff, compilation and diff checks passed. Independent reviews checked recognition safety and replay semantics.

### Evidence and limits

- [Review summary](https://github.com/mrauter1/tibiaauto12/blob/feature/adaptive-resource-reader/reports/README.md)
- [Final benchmark](https://github.com/mrauter1/tibiaauto12/blob/feature/adaptive-resource-reader/reports/resource-benchmark.md)
- [Latency and individual samples](https://github.com/mrauter1/tibiaauto12/blob/feature/adaptive-resource-reader/reports/resource-latency.json)
- [First evaluation](https://github.com/mrauter1/tibiaauto12/blob/feature/adaptive-resource-reader/reports/iterations/iteration-03-real-originals.json)
- [Field-level repair evidence](https://github.com/mrauter1/tibiaauto12/blob/feature/adaptive-resource-reader/reports/diagnosis/real-original-gaps.json)
- [Native collection/replay protocol](https://github.com/mrauter1/tibiaauto12/blob/feature/adaptive-resource-reader/docs/resource-sequences.md)

No actual native gameplay recording was available. Live recovery/freshness, Windows DPI and physical-input behavior remain unverified. Source transcodes and visual similarity do not establish exact builds or native resolution. Historical generated diagnostics remain explicitly outside acceptance.
