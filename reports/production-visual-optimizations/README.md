# Production visual optimizations

The five production optimizations preserve the baseline's results on all **81
original screenshots**. In the measured eight-source replay, paired read speedups
are **2.74× for stable pixels** and **1.93× for a changed background pixel**.
These workloads retain unchanged numeric pixels; they do not establish a
speedup for every live workload.

[Machine-readable analysis](analysis.json) · [Raw evidence](evidence.tar.gz) ·
[Checksums](checksums.json) · [Analysis script](analyze.py)

| Item | Evaluated version |
| --- | --- |
| Baseline | `d3731316a69e4e96c0d422d870f562723710a6b9` |
| Production changes | `d00c48ff0185b214a716814094cd066d84a6aa79` |
| Environment | Linux x86-64; AMD EPYC 9V74; five exposed logical CPUs, four-CPU quota; Python 3.12.14; NumPy 2.2.6; OpenCV 4.11.0 |
| Native backend | Built and loaded for current-version measurements; absent from baseline |

The default reader includes bounded exact numeric-result reuse, touching-digit
interval memoization, fused native candidate filtering, ROI-first crops with
fewer capture copies, and exact discovery evidence caching. The earlier opt-in
experiment framework is excluded from this PR. Implementation and configuration
are documented in [the reader guide](../../docs/adaptive-reader.md).

## Correctness

Each version independently discovers and reads every unchanged original. Input
bytes, manifest, split, decoded-image hashes and case IDs match. The reader is
not given truth regions or expected values. There are **zero discrete output
mismatches**, including panel status and each field's value, quality and region.
Floating diagnostic scores are outside that parity comparison.

| Metric | Baseline | Current |
| --- | ---: | ---: |
| Eligible fields | 239 | 239 |
| Exact values at corresponding regions | 221 | 221 |
| UNKNOWN eligible fields | 18 | 18 |
| Incorrect accepted eligible fields | 0 | 0 |
| Accepted location mismatches | 0 | 0 |
| Safety controls: all UNKNOWN, no accepted panel | 17/17 | 17/17 |
| Unsafe expected-UNKNOWN acceptances / reader errors | 0 / 0 | 0 / 0 |

One inherited reference diagnostic, `dfg-demolidores-4KCJ`, still produces an
unexpected panel and an accepted Soul value of 48 in both versions. It is
outside the 17 safety controls. The Soul value matches its eligible numeric
annotation, while the panel status contradicts the expected absence of a panel.
Therefore zero incorrect numeric fields does not mean zero false-panel
acceptance across the entire corpus. The 18 UNKNOWN eligible fields also remain
unresolved.

## Read latency

Eight sources were fixed in the harness before timing. Four invocations ran
serially in baseline/current/current/baseline order, with other local tests and
benchmarks stopped. Each source contributes 20 stable-pixel reads and 20 reads
with one changed background pixel outside panel/geometry evidence in each of
two blocks. Samples use fresh immutable Frames; numeric pixels stay unchanged.

| Workload | Baseline p50 / p95 ms | Current p50 / p95 ms | Paired speedup |
| --- | ---: | ---: | ---: |
| Stable pixels | 10.85 / 29.70 | 1.52 / 22.62 | 2.74× |
| One changed background pixel | 17.16 / 37.19 | 7.74 / 28.19 | 1.93× |

Speedup is the geometric mean of baseline/current median-latency ratios for
16 matching source/block pairs. Percentiles pool 320 samples per version and
workload; p95 uses linear interpolation. All paired medians improved in this
workload. The smallest gain was 1.04× on `reddit-chrissykes-4ppe2dp`, background
block 2. All recorded repeat/background consistency checks passed.

These are correlated repetitions of eight images. Two alternating blocks do
not establish confidence bounds or live FPS. The harness retains raw timings
and sequence-consistency flags, rather than every repeated Reading object.

## Scope and validation

- Timings cover guarded CPU reads after discovery. Capture, Frame construction,
  initialization, discovery and retry delays are excluded. This measures the
  combined read path, without attributing gains to individual techniques or
  measuring copy-reduction and repeated-discovery benefits.
- Changing resource values, cold/all-miss caches, broad animation and live
  freshness are not measured by this production replay. No recognition threshold,
  font coverage or numeric precision tradeoff was introduced.
- Exact caches retain bounded per-reader evidence. Current geometry, label,
  visibility, ambiguity and duplicate checks remain. UNKNOWN/OCR numeric results,
  errors and incomplete discovery are not reused. Diagnostics expose snapshots
  that cannot modify internal cache accounting.
- All **247 focused production guard tests passed**, without skips. They cover
  changed values, invalidation, cancellation, duplicates, native/NumPy parity,
  immutable capture ownership and cache limits.
- [CI for the evaluated implementation](https://github.com/mrauter1/tibiaauto12/actions/runs/39975348051)
  passed on Linux and Windows with Python 3.10 and 3.12. Python 3.12 results were
  **1,772 passed / 2 skipped on Linux** and **1,771 passed / 3 skipped on Windows**.
  Skips are expected GUI/platform cases; native tests ran on both platforms.
  Ruff and compilation checks passed.
- Native building is explicit; an absent or incompatible backend falls back to
  NumPy. Windows native compilation/loading was verified in CI. **Live Windows
  capture and GPU performance remain unmeasured.**

## Reproduction

Run from a repository with both commits available and the configured `.venv`.
These Linux commands extract isolated archives, so each artifact records its
full supplied commit plus source/input hashes. Build the current native backend
before producing any current artifacts, then leave both archives unchanged.

```bash
PROD_BASE_REF=d3731316a69e4e96c0d422d870f562723710a6b9
PROD_CURRENT_REF=d00c48ff0185b214a716814094cd066d84a6aa79
PROD_BASE=$(mktemp -d /tmp/tibiaauto12-production-base-XXXXXX)
PROD_CURRENT=$(mktemp -d /tmp/tibiaauto12-production-current-XXXXXX)
PROD_PYTHON="$PWD/.venv/bin/python"
PROD_OUT="$PWD/.work/production-visual"
git archive "$PROD_BASE_REF" | tar -x -C "$PROD_BASE"
git archive "$PROD_CURRENT_REF" | tar -x -C "$PROD_CURRENT"
PROD_HARNESS="$PROD_CURRENT/tools/benchmark_visual_production.py"
"$PROD_PYTHON" "$PROD_CURRENT/tools/build_visual_native.py" --compiler cc

"$PROD_PYTHON" "$PROD_HARNESS" --checkout "$PROD_BASE" --ref "$PROD_BASE_REF" --python "$PROD_PYTHON" --mode correctness --out "$PROD_OUT/base-correctness.json"
"$PROD_PYTHON" "$PROD_HARNESS" --checkout "$PROD_CURRENT" --ref "$PROD_CURRENT_REF" --python "$PROD_PYTHON" --mode correctness --out "$PROD_OUT/current-correctness.json"

# Serial ABBA; stop other test/benchmark CPU work for all four invocations.
"$PROD_PYTHON" "$PROD_HARNESS" --checkout "$PROD_BASE" --ref "$PROD_BASE_REF" --python "$PROD_PYTHON" --mode timing --samples 20 --out "$PROD_OUT/base-timing-1.json"
"$PROD_PYTHON" "$PROD_HARNESS" --checkout "$PROD_CURRENT" --ref "$PROD_CURRENT_REF" --python "$PROD_PYTHON" --mode timing --samples 20 --out "$PROD_OUT/current-timing-1.json"
"$PROD_PYTHON" "$PROD_HARNESS" --checkout "$PROD_CURRENT" --ref "$PROD_CURRENT_REF" --python "$PROD_PYTHON" --mode timing --samples 20 --out "$PROD_OUT/current-timing-2.json"
"$PROD_PYTHON" "$PROD_HARNESS" --checkout "$PROD_BASE" --ref "$PROD_BASE_REF" --python "$PROD_PYTHON" --mode timing --samples 20 --out "$PROD_OUT/base-timing-2.json"

"$PROD_PYTHON" reports/production-visual-optimizations/analyze.py --inputs "$PROD_OUT"
```

The harness fixes OpenCV and numerical-library thread counts to one, verifies
that sources/inputs/native backend remain unchanged during each invocation, and
stores raw samples and provenance. The stdlib-only analyzer does not rerun
inference: it verifies cross-artifact identities and signatures, rejects invalid
timing samples or failed sequence-consistency flags, and writes `analysis.json`.

The evidence archive contains the six raw JSON runs, the focused guard JUnit
and provenance files, and the CI status snapshot. To recompute analysis without
rerunning inference, extract it into an empty directory and pass that directory
as `--inputs` to `analyze.py`. `checksums.json` records every archived file.
