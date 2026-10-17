Added and manually annotated **30 recent, complete Tibia screenshots**, bringing
the corpus to **81 unchanged originals: 71 full screenshots and ten supplemental
crops**. The source-group split is **57 development / 14 test** (80.28% / 19.72%);
the crops remain separate diagnostics. All 81 originals were evaluated with the
three frozen readers, totaling **243 image/variant evaluations and 8,350 cached
four-resource reads**.

The fresh test set gives both improved readers **41/44 correct readable values
(93.2%)**, versus **40/44 (90.9%)** for baseline. There are **zero incorrect
accepted values, wrong-row associations or accepted N/A values** anywhere in
this run. The fast reader meets the predeclared 50 ms cached p95 target on every
detected full screenshot. Robust has the highest development coverage, but one
older development image reaches **55.70 ms p95**, so its previous universal
per-image timing pass does **not** hold in this run.

The [annotation table](../docs/resource-latest-annotations.md) links every new
original and its public source. The [collection review](../docs/resource-corpus-latest-2026-10-17.md)
records dimensions, equivalence, provenance, duplicate checks and selection.
The additions were published July 13–October 14, 2026 and show contemporary
client interfaces; **exact executable builds are unverified**. They come from
20 conservative author/source groups on one publishing community, r/TibiaMMO.

| Frozen partition | Full images | Readable integer targets | N/A fields | Safety controls |
|---|---:|---:|---:|---:|
| Development | 57 | 155 | 64 | 14 |
| Test | 14 | 44 | 12 | 3 |
| Supplemental crops, outside 80/20 ratio | 10 | 40 | 0 | 0 |

All 51 previously examined originals stay outside test. The new captures supply
16 development and 14 test images. Related authors and the shared party captures
stay together. Test contains 11 fully readable screenshots and three all-N/A
screenshots, across 11 source groups; eight groups contain readable targets.
Membership was selected without reader outputs, using the deterministic
[source-group split](../tests/fixtures/screenshots/split-2026-10-17-latest.json).
No reader parameters, labels or memberships were changed after evaluation.

The 30 additions contain **76 readable fields and 44 N/A fields**. N/A reasons
are explicit: 27 hidden by panel height, 12 with Skills absent, four scrolled
away and one severely clipped. Four other partially clipped Hit Points values
are reliably readable and remain scored targets. Their missed readings below
are not converted to N/A. No hidden number is copied from another frame or HUD.

| Reader | Test correct / 44 | Test unknown | Incorrect | Test cached median / p95, ms |
|---|---:|---:|---:|---:|
| Baseline | 40 | 4 | 0 | 9.32 / 13.12 |
| `accelerated_fast` | 41 | 3 | 0 | 3.79 / 5.59 |
| `accelerated_robust` | 41 | 3 | 0 | 3.80 / 5.61 |

All readers discover the 11 readable test layouts and abstain on all 12 test N/A
fields. Test timing has 550 cached samples per reader. Equal weighting of the
eight source groups with readable targets gives 93.23% correct for baseline and
94.27% for both improved readers. These are descriptive results on a small public
sample, not estimates of live gameplay reliability.

| Evidence cohort | Readable targets | Baseline correct / unknown | Fast correct / unknown | Robust correct / unknown |
|---|---:|---:|---:|---:|
| Development: equivalent positive full screenshots | 148 | 116 / 32 | 129 / 19 | 133 / 15 |
| Development: different-interface full references | 7 | 3 / 4 | 3 / 4 | 3 / 4 |
| Test: equivalent positive full screenshots | 44 | 40 / 4 | 41 / 3 | 41 / 3 |
| All 71 full screenshots | 199 | 159 / 40 | 173 / 26 | 177 / 22 |
| Ten supplemental crops | 40 | 38 / 2 | 38 / 2 | 38 / 2 |
| All 81 originals | 239 | 197 / 42 | 211 / 28 | 215 / 24 |
| New 30 only, overlapping development/test above | 76 | 66 / 10 | 69 / 7 | 69 / 7 |

There are no previously correct field regressions. Fast recovers 14 baseline
misses across the corpus, and robust recovers 18. Robust's four additional
recoveries are all in previously examined development images; it adds no
coverage over fast on the new 30. All variants preserve the original seven-image
primary regression at 28/28 and all 13 reference diagnostics at 41/47.

All **17 safety-control images** produce no accepted values or false accepted
panels. All **76 N/A fields** in the complete corpus remain unknown, as do the
eight visible but ambiguity-controlled fields. The separately annotated visible
decimal Capacity is unscored by the integer schema and is not counted as N/A.
No unscored value is accepted. Static repeated outputs are consistent throughout.

The improved readers' seven remaining misses in the additions have two causes:

| Cases | Split | Readable value(s) left unknown | Observed rejection |
|---|---|---|---|
| Four Musquitu96 captures | Development | Hit Points 5,277; 5,365; 5,369; 5,017 | Panel discovery requires more visible row/column evidence than these partial panels provide. |
| Fearless Drawing, `z6oqvmc` | Test | Hit Points 2,116 | Unique complete value-region proof fails before numeric recognition. |
| Cowboy Isaac, `dhlws3d` | Test | Capacity 1,176 | Same value-region guard. |
| Keasol, `2yqk8uk` | Test | Mana 4,112 | Same value-region guard. |

Both improved configurations recover Capacity 7,671 in Local Ad's `w1iymdg`,
2,349 in Eurbina's capture, and 2,391 in Cowboy Isaac's `9muurr2`. The remaining
three test misses occur before numeric recognition, so the robust numeric
consensus fallback cannot resolve them. These outcomes are preserved for future
work; this evaluation does not tune against them. If test failures guide a later
change, fresh untouched test evidence will be needed.

| Reader | Detected full images / 71 | Full-image cached samples | Cached median / p95, ms | Slowest image p95, ms | Every detected image ≤50 ms p95? |
|---|---:|---:|---:|---:|---|
| Baseline | 43 | 2,150 | 9.64 / 19.73 | 57.25 | No |
| Fast | 47 | 2,350 | 4.79 / 8.25 | 14.82 | Yes |
| Robust | 47 | 2,350 | 4.85 / 20.32 | 55.70 | No |

The slowest case for baseline and robust is the existing `dfg-gladera-SSVX`
development capture. Robust's first read there is also costly; the overall
full-image first-read maximum is 74.98 ms. This is the unchanged reader on a
different hosted run, so the timing difference from the earlier 27.95 ms result
cannot be attributed to a code change. No selective rerun replaces this result.
The fast reader is the only configuration satisfying the strict per-image
cached deadline across this corpus in the recorded run. Robust remains an
accuracy/latency tradeoff on development evidence.

On the same 42 positive full layouts detected by baseline and both candidates,
cached median/p95 is **9.88/19.76 ms baseline, 4.76/6.97 ms fast and 4.77/6.99 ms
robust**. Pooled percentiles hide expensive outliers; the per-image gate above
is necessary. Timing is conditional on discovery: missing layouts have no cached
samples, and their readable values remain misses in the accuracy denominator.

Cold discovery remains expensive. Across all 71 full screenshots, median/p95 is
**4.95/8.91 seconds baseline, 5.25/9.76 seconds fast and 5.25/9.77 seconds robust**.
The complete 3456×2166 login desktop is the slowest negative, at 35.41 seconds
for robust. A verified persistent layout is still necessary for near-real-time
reads. Static measurements exclude screen capture, action latency, changing
gameplay, freshness checks and reacquisition scheduling; they do not establish
an end-to-end live deadline.

Annotations and split were committed before inference in `7af3111`. The
[successful six-shard benchmark](https://github.com/mrauter1/tibiaauto12/actions/runs/39078190167)
evaluated `9fbc6d089f71db5855cac20e0e95ccb48cb3b371`, using the same reader code
and profiles as the preceding experiment. Every image uses a fresh autonomous
full-frame discovery with pixels only, a separate first read, then 50 cached
four-field reads. OCR is disabled and OpenCV uses one thread. The six Linux
runners use Python 3.12.15, OpenCV 4.11.0, NumPy 2.2.6 and Pillow 11.3.0.

The [machine-readable summary](latest-screenshots-2026-10-17-summary.json)
verifies all 81 identities per variant, source/pixel hashes, frozen annotation
and code fingerprints, split membership and exact shard coverage. The initial
workflow stopped before inference on Windows/Linux text serialization; the
correction changed no images, annotations, split or reader settings. Profile
aggregate hashes also differ with platform filename ordering and README line
endings. The summarizer reconstructs both encodings and verifies that all 43
profile files have the identical Git tree before collection and at evaluation.

The [artifact index](latest-screenshots-2026-10-17-artifacts.json) records the six
ZIP hashes and download links. Raw JSON, JSONL and ZIPs are saved locally in
`.work/corpus-latest-20261017/benchmark/` and retained by the linked Actions run
until January 15, 2027. Bulky raw outcomes stay outside Git history. The evaluated
revision passed all four push CI jobs (Windows/Linux × Python 3.10/3.12), including
pytest, compileall and Ruff. Linux reports 847 passed / one skipped; Windows
reports 845 passed / three skipped. A duplicate PR job hit an existing
configuration-lock test timeout and passed unchanged on retry; it does not alter
benchmark outcomes.

Reproduce the complete run and summarize it from the repository root:

```powershell
.venv\Scripts\python.exe tools/benchmark_latest_screenshots.py --out .work/latest-screenshots/shard-0.json
.venv\Scripts\python.exe tools/summarize_latest_screenshots.py --input .work/corpus-latest-20261017/benchmark --revision 9fbc6d089f71db5855cac20e0e95ccb48cb3b371 --run-url https://github.com/mrauter1/tibiaauto12/actions/runs/39078190167 --out .work/latest-screenshots-summary.json
```

The first command runs all originals on one machine. CI uses six disjoint shards
with `--shards 6 --shard N`. The second command audits the downloaded recorded
shards; use the matching revision and input directory when auditing a new run.
