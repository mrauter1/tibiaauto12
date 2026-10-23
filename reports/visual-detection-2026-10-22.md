# Visual detection benchmark — 2026-10-22

Offline regression on 81 original images. Detector and annotation hashes stayed fixed during this run.
Bars, targets and markers receive manually calibrated regions. Minimap and Skills discovery search the original full frame.
No native capture, input, transformed acceptance images, or test suite was run. This is not the resource-reader promotion gate.

| Detector | Scored | Passed | Criterion |
|---|---:|---:|---|
| Health bar | 54 | 53 | Error ≤ 2 percentage points; unknown is a failure |
| Mana bar | 54 | 53 | Error ≤ 2 percentage points; unknown is a failure |
| Minimap visible | 71 | 57 | Automatic location IoU ≥ 0.8 / correct absence |
| Minimap absent | 10 | 10 | Automatic location IoU ≥ 0.8 / correct absence |
| Battle targets, positive queries | 11 | 9 | Count, click location and selection all correct |
| Markers, positive queries | 6 | 6 | Point inside a labelled icon / no point |
| Battle targets, negative queries | 31 | 26 | Count, click location and selection all correct |
| Markers, negative queries | 64 | 64 | Point inside a labelled icon / no point |

## Skills numeric regression

| Production reader | Eligible fields | Exact value and location | Incorrect accepted | Unknown |
|---|---:|---:|---:|---:|
| sdk_current | 239 | 151 | 0 | 88 |
| monitor_retained | 239 | 166 | 0 | 73 |

Skills rows use the existing corpus scoring and exclusions. Full screenshots and supplemental crops are separated in JSON.
The SDK/default reader and Resource Monitor retained-layout reader are separate production paths; neither is substituted for the other.

## Interpretation and timing

- The bar failures are the genuinely empty health and mana bars: the detector returns unknown instead of 0%. Every scored non-empty bar was within two percentage points.
- Draken Warmaster (four visible rows) and Glooth Golem (five coloured-name rows) were both returned as zero matches. The only positively selected target example was among the missed Glooth Golems, so successful selection recognition is not established here.
- The five failed negative target queries were unknown/error results, not invented creatures. One error was a bundled template wider than the small battle panel.
- The automatic minimap locator found 57 of 71 visible maps. Its fixed-size template and geometry were evaluated as implemented; manually calibrated map regions are a different input contract.
- Marker successes cover six positive Cross queries and 64 absent Cross/CheckMark queries. They do not establish all marker types, movement, or arrival.
- SDK Skills discovery hit the declared cooperative 15-second deadline on 32 images. Its 88 unknown numeric fields include these deadline failures; this is not directly comparable with an unlimited-discovery accuracy run. The retained monitor reader had no discovery timeouts, but still abstained on 73 fields. Neither reader accepted an incorrect eligible field in this run.

| Timing (median across original-image cases) | SDK Skills | Retained monitor Skills |
|---|---:|---:|
| Cold initialization + discovery + first read, including failed/deadline cases | 12.79 s | 1.50 s |
| Cached read after successful discovery | 5.76 ms | 0.76 ms |

The cached Skills medians cover 40 and 43 successfully discovered panels respectively; no capture cost is included. Final-run cached bar medians were 9.98 ms for health and 10.90 ms for mana. These are static-image timings, not a live response-time guarantee.

## Coverage and limits

Skills measurements were reused from the completed initial run after verifying unchanged production code, assets, corpus hashes and timing parameters. Only visual checks were rerun after the annotation erratum.

Annotation erratum: Final audit: positive-marker center/arrival labels were not independently established by static screenshots. All positive-marker center_overlap labels are now unknown. Detection boxes and every other label are unchanged. This correction was made after the initial run, before the final visual rerun.

- Single annotator; no independent second reviewer.
- Some visible bars and battle lists remain unscored, with explicit reasons. Coverage is reported, not treated as a pass.
- No positive Lizard Henchman label or positive waypoint-arrival example in this labelled subset.
- Map location boxes tolerate 3 px manual boundary uncertainty; IoU >= 0.8 is predeclared.
- Bar error tolerance is 2 percentage points; integer rounding and rasterization are expected.
- Static screenshots do not establish successful movement, targeting input, upstream freshness or capture latency.
- Existing development/test membership is preserved and reported separately; this run is regression evidence, not a new unseen test set.
- Latency is original-image computation only. Cached timings repeat identical pixels; they do not measure capture freshness or live changing-frame behavior.
- Zero-resource, scaled UI, coloured names and circular map icons remain in the denominator when independently labelled.

## Failed visual cases

- `user-paulistinha-20261013`: automatic minimap location
- `dfg-pvp-ek133`: automatic minimap location
- `dfg-yonabra-ek195`: automatic minimap location
- `dfg-loyalty-ek277-OL5J`: automatic minimap location
- `dfg-calmera-ek246-D9BJ`: automatic minimap location
- `dfg-laudera-knight64-PD1G`: automatic minimap location
- `drenox-client`: target Lizard Henchman: expected 0, got None
- `dfg-gentebra-BJEI`: automatic minimap location
- `dfg-quintera-VS41`: automatic minimap location
- `tibiabr-sidebars-filled`: automatic minimap location; target Lizard Henchman: expected 0, got None
- `dfg-gladera-SSVX`: automatic minimap location
- `tibiaqa-flash-v1QxvNN`: automatic minimap location
- `allegro-antica-4c39a50f`: automatic minimap location; target Lizard Henchman: expected 0, got None; target Rat: expected 0, got None
- `reddit-deleted-09s6sbl`: automatic minimap location
- `reddit-valouran-pl7tust`: target Draken Warmaster: expected 4, got {'count': 0, 'position': None, 'selected': False}
- `reddit-chrissykes-4ppe2dp`: automatic minimap location
- `reddit-frosty-economics-557-89mlozz`: health bar: None vs 0.0; mana bar: None vs 0.0
- `reddit-cowboyisaac-9muurr2`: target Lizard Henchman: expected 0, got None
- `reddit-reverofsipcabej-d7ze1nn`: target Glooth Golem: expected 5, got {'count': 0, 'position': None, 'selected': False}

Detailed predictions, timing samples, split summaries, exclusions and fingerprints are in the adjacent JSON report.
