The best tested configuration is `accelerated_robust`: **77/88 correct readable values on the 22 new primary screenshots**, up from 62/88, with **zero incorrect accepted values**. Its cached four-resource read is **5.19 ms median / 20.89 ms p95**; the slowest individual image is **27.95 ms p95**. It recovers 15 of the 26 misses, a 57.7% reduction. `accelerated_fast` trades four of those recoveries for a 7.70 ms aggregate p95.

The working target was 50 ms p95 after layout discovery. These are opt-in experiment readers in `tools/resource_reader_experiments.py`; production defaults, profiles, original screenshots and frozen annotations are unchanged. Unknown or obscured information remains unknown. No hidden numeric value is inferred.

The final paired full-corpus comparison evaluated all 51 originals at commit `8fc417c302812cfed5a36a36e37fb3714a56db88`. Each image/variant has independent full-frame discovery, followed by 50 repeated cached reads where a layout exists. The reader receives pixels, not case IDs, truth or annotation coordinates. All 51 case identities, original hashes and implementation fingerprints were verified across every variant and shard.

| Final-round variant | Correct / 88 | Unknown | Incorrect | Cached median, ms | Cached p95, ms | Every detected new-primary image below 50 ms p95? |
|---|---:|---:|---:|---:|---:|---|
| Baseline | 62 | 26 | 0 | 9.77 | 32.11 | Yes |
| Combined, without consensus | 73 | 15 | 0 | 4.93 | 22.36 | Yes |
| Combined, with consensus | 77 | 11 | 0 | 5.20 | 94.46 | No |
| Combined + width pruning, without consensus | 73 | 15 | 0 | 4.90 | 11.25 | Yes |
| Combined + width pruning, with consensus | 77 | 11 | 0 | 5.20 | 44.50 | **No: one image is 60.88 ms** |
| `accelerated_fast` | 73 | 15 | 0 | 4.93 | 7.70 | Yes |
| **`accelerated_robust`** | **77** | **11** | **0** | **5.19** | **20.89** | **Yes** |

Accuracy includes all 88 fields, including discovery failures. Baseline timing has 850 samples from 17 detected new-primary layouts; the combinations have 1,050 from 21 layouts. Unknown readings remain in the timing samples. On the same 17 layouts shared with baseline, `accelerated_robust` measures 5.16 ms median / 27.54 ms p95 versus baseline 9.77 / 32.11. The aggregate table includes the four additionally detected layouts, so it is not a pure speed-only comparison. Aggregate percentiles also hide individual slow images; the per-image gate is necessary.

The techniques were tested first on the failure pattern they address, then promoted to full originals when useful. Numeric crop trials supplied known crop geometry/scale for component diagnosis only; their results were never substituted for autonomous full-frame acceptance.

| Technique | Specific test and finding | Full-corpus finding |
|---|---|---|
| Cache immutable scaled templates | Repeated numeric crops with identical output checks | Preserves accuracy; first-round cached p95 falls from 23.55 to 19.54 ms. |
| Vectorize glyph ranking and bound its cache | 52 numeric fields: cache, vector and glyph-cache variants produce identical values; vectorization alone has no clear benefit | Glyph cache preserves accuracy and lowers first-round cached p95 to 17.33 ms. |
| Match more label scales and vertical raster phases | Autonomous discovery on the four missed panels; all four recovered without supplied coordinates | Seven additional correct values. Final shape/geometry acceptance guards remain in place. |
| Include green numeric foreground | Both green Capacity component crops decode correctly; then test whole screenshots | Dworc Capacity 730 recovered. Daralion Capacity 1433 still fails the value-region guard. |
| Penalize uncertain hole topology instead of excluding digits | Numeric ambiguity and topology diagnostics, plus historical numeric tests | Blake Mana 18,475 recovered; also helps BJEI Capacity 1,264 once label discovery succeeds. |
| Recover a visible label suffix using three verified rows | Gentebra 3XWB full frame plus clipping/ambiguity controls | Mana 1,451 recovered. This infers label identity and position, never hidden digits. |
| Bounded threshold consensus after rejection | Four brightness offsets; require at least two accepted agreeing reads and no accepted disagreement | No gain alone while panels remain undiscovered; adds BJEI Health and three Deleted values after the other fixes. Initially too slow. |
| Extend the small-font bank and internally enlarge tiny text | Allegro full-frame trial | Still no detected panel; not promoted as a useful full-benchmark candidate. |
| Prune impossible touching-digit counts | The existing minimum cell width proves some split counts impossible before their Cartesian products are enumerated | All values and regions unchanged on all 51 originals; major reduction in fallback cost. Pruning alone still leaves a 60.88 ms image. |
| Cache/vectorize touching-digit fragment rankings too | Focused tests on Gladera and Chrissykes, then full independent discovery and safety tests | All values and regions unchanged. Robust Gladera p95 falls 109.54 → 60.88 → 27.95 ms; Chrissykes 94.46 → 44.50 → 20.89 ms for unpruned, pruned, then accelerated. |

The main interaction is ordering: numeric improvements cannot recover a field that discovery or value-region extraction never supplies. The other major finding is that caching only separated digits leaves the expensive touching-digit fallback untouched. Extending bounded per-glyph ranking reuse to that path makes consensus affordable without caching complete numeric readings. The fallback retains its original strict topology, split, score and ambiguity rules.

All variants preserve the prior primary regression at **28/28**, keep the reference diagnostic set at **41/47**, and abstain on all **nine negative/ambiguous controls**. There are no incorrect accepts, accepted wrong-row associations, previously correct field regressions or accepted N/A/unscored values anywhere in the 51-image corpus. The recommended variant is 146/163 over all scored readable integer fields; reference results remain separate from primary acceptance.

The recommended variant still leaves these **11 readable new-primary values unknown**. They remain misses, not relabeled N/A:

| Screenshot | Remaining values | Rejection |
|---|---|---|
| Allegro Antica | Health 2845, Mana 4080, Soul 100, Capacity 1301 | Tiny-font panel discovery |
| Gentebra BJEI | Mana 3628 | Weak separator |
| Gladera SSVX | Health 3935, Soul 100 | Separator / digit ambiguity |
| Reddit Chrissykes | Health 2810, Soul 200 | Separator / digit ambiguity |
| Quintera VS41 | Mana 4123 | Non-unique/incomplete value-region proof |
| Reddit Daralion | Capacity 1433 | Non-unique/incomplete value-region proof |

Cold discovery is still unsuitable for every-frame use: `accelerated_robust` takes **3.96 s median / 8.60 s p95**, with an 11.98 s maximum on the new primary images. Initialization is about 12.6 ms median. Its first read after discovery is 7.86 ms median / 34.63 ms p95, maximum 38.94 ms. Use a persistent verified layout and retain current-frame validation; when invalidated, return unknown while reacquiring it. An asynchronous acquisition scheduler was not implemented or benchmarked here.

The cached benchmark repeats static originals and excludes screen capture and downstream action latency. A separate changing-value diagnostic exercised 80 synthetic frames per variant/mode, including clearing glyph rankings before every frame. Baseline and accelerated readers each produced the same 282 correct and 38 unknown fields out of 320, with zero stale or incorrect accepted values. The 38 shared abstentions come from the baseline column guard rejecting some profile-pasted glyph spacing. This diagnostic supports current-pixel behavior; it is not a claim of complete synthetic coverage or live gameplay latency. On that one-font diagnostic, clearing rankings gives accelerated robust p95 5.08 ms, but this cannot establish a full-screen live deadline.

All four follow-up variants passed the same **60 existing safety tests each**, covering changed values, occlusion, clipping, stale layout invalidation, decoy numbers, multiple columns and duplicate panels. The accelerated robust numeric suite also passed all 31 tests. All four Windows/Linux × Python 3.10/3.12 CI jobs passed pytest, compileall and Ruff. The measured reader implementation is unchanged by the subsequent reporting commit.

There were two full rounds, totaling **14 distinct variants, 867 image/variant evaluations and 31,550 cached four-resource reads**. The follow-up was deliberately chosen after inspecting round one. These previously diagnosed screenshots are development/regression evidence, not an untouched holdout. Runs use one OpenCV thread and paired variants on each shard runner; pooled timing quantiles span several hosted machines. Use contemporaneous paired baselines, not the historical 74.87 ms result from the earlier baseline run, to attribute performance changes.

The [trial protocol](resource-techniques-2026-10-17-plan.json), [focused results](resource-techniques-2026-10-17-focused.json), [first-round summary](resource-techniques-2026-10-17-round1-summary.json) and [final summary](resource-techniques-2026-10-17-summary.json) preserve selection decisions, counts, hashes, gates and artifact links. Raw JSON/JSONL and JUnit artifacts are in [round one](https://github.com/mrauter1/tibiaauto12/actions/runs/39053337015) and [round two](https://github.com/mrauter1/tibiaauto12/actions/runs/39054630066), with artifact expiry January 15, 2027. They are also saved locally under `.work/reader-techniques-20261017/hosted/` and `.work/reader-techniques-20261017/hosted-round2/`. Focused raw results are in their parent directory. Bulky raw files remain outside Git history.

Reproduce the recommended comparison from the repository root:

```powershell
.venv\Scripts\python.exe tools/benchmark_resource_techniques.py --mode full --subset all --variants baseline accelerated_fast accelerated_robust --threads 1 --warm-reads 50 --budget-ms 50 --out .work/recheck/resource-techniques.json
.venv\Scripts\python.exe tools/check_resource_experiment_guards.py --variant accelerated_robust --junit .work/recheck/guards.xml
```

The six-shard workflow uses the same command with `--shards 6 --shard N`; `tools/summarize_resource_techniques.py` verifies complete shard coverage, hashes, output equivalence and timing gates before producing the summary. A successful workflow only means the experiment ran; the summary reports recognition and latency outcomes separately.
