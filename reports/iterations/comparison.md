# Historical resource-reader repair: iteration 01 to iteration 02

This historical diagnostic is outside current real-image acceptance. Its final
report is preserved as [iteration 02](iteration-02-before-realism.json); see the
[current review](../README.md) for preserved-original evaluation.

This compares two complete 404-case, three-technique runs on the same frozen pixels and truth. Iteration 01 exposed three incorrect accepted OCR values. The final run removed all three unsafe acceptances and passed the strict safety gate.

| Run | Valid | Safety gate | Incorrect accepted | Report SHA-256 |
|---|---:|---:|---:|---|
| Iteration 01 | true | FAIL | 3 | `a2cbb82b992f2f081ee9deb9656d74088a2e302f7486c649c8c9f6276552bd95` |
| Final | true | PASS | 0 | `121aeb6deba068f2245c52ba4ffb11861ccbd206bd0f2cc967d7a8589125f251` |

## Aggregate changes

| Technique | Exact before | Exact final | Exact change | Unknown before | Unknown final | Incorrect before | Incorrect final | Final acceptance precision |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Legacy exact-scale PR4 | 23/1491 (1.54%) | 23/1491 (1.54%) | +0 | 1468 | 1468 | 0 | 0 | 100.00% |
| Adaptive templates | 538/1491 (36.08%) | 535/1491 (35.88%) | -3 | 953 | 956 | 0 | 0 | 100.00% |
| Adaptive templates + OCR | 977/1491 (65.53%) | 979/1491 (65.66%) | +2 | 511 | 512 | 3 | 0 | 100.00% |

The final adaptive-template reader without OCR remains conservative: it has zero incorrect accepted values but three additional abstentions in dense scale stress. The OCR technique converts the three known unsafe values to exact reads, while one previously exact 0.65-scale capacity value becomes a safe abstention. Its net exact count rises by two and accepted precision rises from 99.69% to 100%.

## Changed field outcomes

| Technique | Case | Field | Before | Final | Interpretation |
|---|---|---|---|---|---|
| Adaptive templates | `user-paulistinha-20261013::panel-scale-0.65` | `resource_capacity` | exact `192` | UNKNOWN | coverage traded for safe abstention |
| Adaptive templates | `user-paulistinha-20261013::panel-scale-0.75` | `resource_mana` | exact `1085` | UNKNOWN | coverage traded for safe abstention |
| Adaptive templates | `user-paulistinha-20261013::panel-scale-1.05` | `resource_mana` | exact `1085` | UNKNOWN | coverage traded for safe abstention |
| Adaptive templates + OCR | `user-paulistinha-20261013::panel-scale-0.65` | `resource_capacity` | exact `192` | UNKNOWN | coverage traded for safe abstention |
| Adaptive templates + OCR | `portal-tibia-2016-02-VtukRkf::panel-scale-0.9` | `resource_capacity` | wrong `304` | exact `394` | known unsafe regression repaired |
| Adaptive templates + OCR | `dfg-pvp-ek133::panel-scale-1.1` | `resource_health` | wrong `7060` | exact `2060` | known unsafe regression repaired |
| Adaptive templates + OCR | `dfg-pvp-ek133::panel-scale-1.3` | `resource_health` | wrong `7060` | exact `2060` | known unsafe regression repaired |

Every changed outcome is in `lightweight_numeric_roi` / `dense_roi_scale`. Original full screenshots, supplemental crops, generated full-frame perturbations, legacy outputs, panel discovery counts, and heldout results are unchanged.

## Final evidence by family

Panel counts show successful discovery over expected positive panels. Exact counts show strict numeric recognition with row correspondence over eligible fields.

| Family | Legacy panels → exact | Adaptive panels → exact | Adaptive + OCR panels → exact |
|---|---:|---:|---:|
| Original captures | 1/13 → 4/52 | 13/13 → 52/52 | 13/13 → 52/52 |
| Translation | 1/13 → 4/52 | 13/13 → 52/52 | 13/13 → 52/52 |
| Width reflow | 1/13 → 0/52 | 13/13 → 52/52 | 13/13 → 52/52 |
| Full-frame scale | 0/26 → 0/104 | 5/26 → 19/104 | 11/26 → 39/104 |
| Dense ROI scale | 1/273 → 4/1092 | 64/273 → 221/1092 | 200/273 → 645/1092 |
| Cached wrong size | 1/13 → 4/52 | 13/13 → 52/52 | 13/13 → 52/52 |
| Cached relocation | 1/12 → 4/48 | 12/12 → 48/48 | 12/12 → 48/48 |
| Cached digit occlusion | 1/13 → 3/39 | 13/13 → 39/39 | 13/13 → 39/39 |

All three techniques have zero reader errors and zero false-panel acceptances in the final report. Both adaptive modes have zero cached invalidation failures and safely reject all 65 intentional-unknown targets. Duplicate panels are rejected as ambiguous rather than accepted.

## Provenance checks

- Manifest SHA-256 is identical: `529a04bfb2b40de17520a81adc6e51f89549ce9c1d67b3bed3af5577495d85e4`.
- Ordered case IDs, transformed case SHA-256 values, and source SHA-256 values match for every row.
- Corpus generator hash is identical: `a9e7ef4b35a53d8e8dac3601c11921643afc3384a76e1fe3872e28c4bde692f1`.
- Comparison scorer hash is identical: `c6099032bdd6bbfd8d1cc64dd2fe33a7545dfbac7fdcea1bbe7a966ba25567b9`.
- Profile bundle hash is identical: `fc2e98c9dc5d4aab568e30c645dd427ee9fde9dbdc99055346966a81236b05af` (43 files).
- The six other hashed implementation files and the complete recorded environment are identical.
- `tibia_client/adaptive_text.py` is the only changed measured implementation file: `7cb5a36565ecd3c2bca045630a04fd42794f5bb835efa89c0cb11f60400cbdea` → `d0c6d0e35d4f05f9b8dfa8d8a287ceefb32433e3d555dc96a05da90941af1a58`.
- Both reports passed their pre/post-run implementation and corpus hash checks (`run_valid=true`).

## Reproducing iteration 01

Use `reports/iterations/restore-iteration-01.patch` only in a separate checkout of commit `b5e30916491595fa8d4ca67ce44b459a62178437`, whose default comparator still contains the historical generated matrix. The current comparator defaults to unchanged real originals. The patch SHA-256 is `fff2102ba0b9ad352f7093d1963e010ec76bff8cf56901e8623c23b1538044fa` and restores `tibia_client/adaptive_text.py` to `7cb5a36565ecd3c2bca045630a04fd42794f5bb835efa89c0cb11f60400cbdea`. Then run:

```bash
git apply reports/iterations/restore-iteration-01.patch
python tools/compare_resource_readers.py --ocr --warm-reads 3 \
  --json-out reports/resource-benchmark.json \
  --markdown-out reports/resource-benchmark.md
```

The three repaired cases were selected from iteration 01 failures, so their final outcomes are regression evidence rather than a new heldout generalization estimate. Generated variants from one source are also correlated. The unchanged heldout slice and original-capture results provide supporting evidence, while additional independent screenshots remain the appropriate generalization test.

A discarded intermediate generic-margin-only change was not used for the final report: the full test suite caught its loss of a confirmed 0.75-scale cropped health value before the final reader was frozen.
