# Frozen-reader baseline: 2026-10-17 collection

The 22 new primary screenshots yield **62/88 correct values with correct row
association (70.5%), 26 unknowns, zero incorrect accepted values and zero wrong-row
associations**. Five panels are not discovered; five more have incomplete numeric
coverage. All 12 remaining positive images are exact. No failure was removed,
reclassified or used to tune the reader.

| Evidence | Images | Readable integer fields | Correct + right row | Unknown | Incorrect accepted | Wrong-row accepted |
|---|---:|---:|---:|---:|---:|---:|
| New primary | 22 | 88 | 62 | 26 | 0 | 0 |
| ↳ No known existing-corpus relative | 19 | 76 | 51 | 25 | 0 | 0 |
| ↳ Related to an existing source | 3 | 12 | 11 | 1 | 0 | 0 |
| New reference only | 3 | 7 | 2 | 5 | 0 | 0 |
| Prior primary regression | 7 | 28 | 28 | 0 | 0 | 0 |
| Prior reference regression | 10 | 40 | 39 | 1 | 0 | 0 |

New safety controls: **6/6 negatives and 1/1 duplicate-panel ambiguity control
abstain**, with all 28 final fields unknown and no accepted panel. The prior
negative and ambiguity controls also abstain (8/8 fields unknown). Safety
abstentions are separate from readable-value coverage. The reference decimal
Capacity `104966.56` remains exact text outside the integer scoring denominator.

| Preserved primary miss | Observed result |
|---|---|
| `dfg-gentebra-BJEI`, `dfg-quintera-VS41`, `allegro-antica-4c39a50f`, `reddit-deleted-09s6sbl`, `reddit-chrissykes-4ppe2dp` | No panel; 20 unknown fields |
| `dfg-gentebra-3XWB` | Mana has no unique complete value region; clipped label prefixes remain annotated |
| `dfg-gladera-SSVX` | Health separator and Soul digit rejected; two unknowns |
| `reddit-blake-m91oix5` | Mana digit rejected |
| `reddit-daralion-0tx21ye`, `reddit-dworc-o0ap20c` | Green Capacity has no unique complete value region |

These are observed rejection stages, not established root causes. In the partial
Blakee1 reference, Health and Capacity are unknown; the different Demolidores
reference has no discovered panel. They remain reference diagnostics.

The separate latency run uses **50 cached reads per discovered positive image**,
with no OCR or generated inputs. All cases complete without timeout/error.

| Algorithm phase | Samples | Median | p95 | Maximum |
|---|---:|---:|---:|---:|
| New primary cold discovery | 22 | 5.11 s | 8.99 s | 11.09 s |
| New safety cold discovery | 7 | 8.17 s | 17.46 s | 21.10 s |
| New primary cached reads | 850 | 17.34 ms | 74.87 ms | 75.92 ms |
| Prior primary cold discovery | 7 | 2.90 s | 5.35 s | 5.44 s |
| Prior primary cached reads | 350 | 17.24 ms | 59.04 ms | 59.43 ms |

The 850 new cached samples cover only the 17 discovered layouts and include
unknown readings; only 12 images are exact on every repeated read. The missing
five layouts contribute no cached timings. These are static-image algorithm
measurements, not live capture, recovery or freshness evidence. Earlier reported
8/26 ms cached measurements used a different run/corpus and are not a controlled
performance comparison.

The comparison's safety gate passes, but no coverage threshold was preregistered;
its zero exit status does **not** mean complete recognition. The latency tool's
correctness summary fails because of the retained misses. Its workflow preserves
this failure as evidence with `continue-on-error`; a green workflow is not an
accuracy claim.

Selection/annotations were committed in `12b52ff` before inference. The hosted
run evaluated head `6f3b429` on Linux 6.17 / Python 3.12.15, OpenCV 4.11.0, NumPy
2.2.6 and Pillow 11.3.0. Its manifest hash matches the pre-inference Git blob, and
all seven reader/benchmark code hashes plus all 43 profile files match the merged
PR #5 base `92195dd`. Windows working-copy CRLF hashes differ from Git's LF
encoding; the Git-blob and canonical annotation audits establish unchanged
content. The redundant, slower local Windows comparison was stopped after the
complete hosted report became available; its partial timings are not used here.

Download the [raw baseline and latency ZIP](https://github.com/mrauter1/tibiaauto12/actions/runs/39039950936/artifacts/12103842391)
(SHA-256 `01e183fc9315319d3020a8b4ea7659df7c91561b280834d811049218a2f75d3f`;
artifact expires January 15, 2027). A local copy is retained at
`.work/corpus-20261017/remote-baseline.zip`. The
[concise JSON scorecard](resource-corpus-2026-10-17-summary.json) includes report
hashes, cohort counts, misses and implementation-identity checks. The approximately
3 MB of verbose JSON stays outside Git history.

Reproduce with the unchanged tools:

```bash
python tools/compare_resource_readers.py --technique adaptive --warm-reads 3 --json-out .work/baseline.json --markdown-out .work/baseline.md
python tools/benchmark_resource_latency.py --warm-reads 50 --timeout 60 --json-out .work/latency.json --markdown-out .work/latency.md
```

See the [collection review](../docs/resource-corpus-2026-10-17.md) for coverage,
source lineage, navigation sheets, unknown metadata and remaining collection gaps.

Validation: the corpus-focused local suite passes 70 tests. All four
[Windows/Linux × Python 3.10/3.12 CI jobs](https://github.com/mrauter1/tibiaauto12/actions/runs/39040790236)
pass pytest, compileall and Ruff: Linux has 843 passed / 1 skipped; Windows has
841 passed / 3 skipped. This validates the historical regression and corpus
integrity contracts, not recognition of every newly collected screenshot.
