# Resource reader benchmark

All techniques ran on the same ordered case IDs. Precision is undefined when a technique accepts no numeric values; abstention is reported through coverage.
The headline table uses only reviewed real full-screenshot acceptance evidence and real negative and multi-panel ambiguity safety controls. Crops, unknown or different renders, and generated cases do not affect acceptance.
The historical heldout split is known-case regression evidence after diagnosis; this report makes no independent heldout generalization claim.

**Acceptance safety gate:** PASS

| Technique | Positive originals | Negative controls | Ambiguity controls | Exact coverage | Acceptance coverage | Accepted precision | Value wrong | Location fail | Unknown | Missing | Ambiguous |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `legacy_exact_scale_pr4` | 7 | 1 | 1 | 14.3% | 14.3% | 100.0% | 0 | 0 | 24 | 8 | 0 |
| `adaptive_templates` | 7 | 1 | 1 | 85.7% | 85.7% | 100.0% | 0 | 0 | 4 | 3 | 0 |
| `adaptive_templates_ocr` | 7 | 1 | 1 | 96.4% | 96.4% | 100.0% | 0 | 0 | 1 | 2 | 0 |
| `adaptive_labels_ocr` | 7 | 1 | 1 | 96.4% | 96.4% | 100.0% | 0 | 0 | 1 | 2 | 0 |
| `adaptive_digits_ocr` | 7 | 1 | 1 | 85.7% | 85.7% | 100.0% | 0 | 0 | 4 | 3 | 0 |

## Results by evidence category

| Technique | Category | Cases | Exact coverage | Incorrect accepted | Unknown |
|---|---|---:|---:|---:|---:|
| `legacy_exact_scale_pr4` | `original_full` | 10 | 12.5% | 0 | 28 |
| `legacy_exact_scale_pr4` | `supplemental_crop` | 9 | 0.0% | 0 | 36 |
| `adaptive_templates` | `original_full` | 10 | 75.0% | 0 | 8 |
| `adaptive_templates` | `supplemental_crop` | 9 | 100.0% | 0 | 0 |
| `adaptive_templates_ocr` | `original_full` | 10 | 93.8% | 0 | 2 |
| `adaptive_templates_ocr` | `supplemental_crop` | 9 | 100.0% | 0 | 0 |
| `adaptive_labels_ocr` | `original_full` | 10 | 93.8% | 0 | 2 |
| `adaptive_labels_ocr` | `supplemental_crop` | 9 | 100.0% | 0 | 0 |
| `adaptive_digits_ocr` | `original_full` | 10 | 75.0% | 0 | 8 |
| `adaptive_digits_ocr` | `supplemental_crop` | 9 | 100.0% | 0 | 0 |

## Macro scorecards by source

| Technique | Evidence group | Sources | Exact macro sources | Exact | Acceptance | Correct location | Numeric accepted given location | Sources failing safety | Pooled incorrect accepted |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `legacy_exact_scale_pr4` | `acceptance_target_family_original_full` | 1 | 1 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `legacy_exact_scale_pr4` | `acceptance_visually_equivalent_original_full` | 6 | 6 | 0.0% | 0.0% | 0.0% | — | 0 | 0 |
| `legacy_exact_scale_pr4` | `ambiguous_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `legacy_exact_scale_pr4` | `negative_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `legacy_exact_scale_pr4` | `reference_supplemental_crop` | 9 | 9 | 0.0% | 0.0% | 0.0% | — | 0 | 0 |
| `legacy_exact_scale_pr4` | `reference_visibly_different_original_full` | 1 | 1 | 0.0% | 0.0% | 0.0% | — | 0 | 0 |
| `adaptive_templates` | `acceptance_target_family_original_full` | 1 | 1 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_templates` | `acceptance_visually_equivalent_original_full` | 6 | 6 | 83.3% | 83.3% | 83.3% | 100.0% | 0 | 0 |
| `adaptive_templates` | `ambiguous_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_templates` | `negative_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_templates` | `reference_supplemental_crop` | 9 | 9 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_templates` | `reference_visibly_different_original_full` | 1 | 1 | 0.0% | 0.0% | 0.0% | — | 0 | 0 |
| `adaptive_templates_ocr` | `acceptance_target_family_original_full` | 1 | 1 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_templates_ocr` | `acceptance_visually_equivalent_original_full` | 6 | 6 | 95.8% | 95.8% | 100.0% | 95.8% | 0 | 0 |
| `adaptive_templates_ocr` | `ambiguous_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_templates_ocr` | `negative_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_templates_ocr` | `reference_supplemental_crop` | 9 | 9 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_templates_ocr` | `reference_visibly_different_original_full` | 1 | 1 | 75.0% | 75.0% | 75.0% | 100.0% | 0 | 0 |
| `adaptive_labels_ocr` | `acceptance_target_family_original_full` | 1 | 1 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_labels_ocr` | `acceptance_visually_equivalent_original_full` | 6 | 6 | 95.8% | 95.8% | 100.0% | 95.8% | 0 | 0 |
| `adaptive_labels_ocr` | `ambiguous_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_labels_ocr` | `negative_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_labels_ocr` | `reference_supplemental_crop` | 9 | 9 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_labels_ocr` | `reference_visibly_different_original_full` | 1 | 1 | 75.0% | 75.0% | 75.0% | 100.0% | 0 | 0 |
| `adaptive_digits_ocr` | `acceptance_target_family_original_full` | 1 | 1 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_digits_ocr` | `acceptance_visually_equivalent_original_full` | 6 | 6 | 83.3% | 83.3% | 83.3% | 100.0% | 0 | 0 |
| `adaptive_digits_ocr` | `ambiguous_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_digits_ocr` | `negative_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_digits_ocr` | `reference_supplemental_crop` | 9 | 9 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_digits_ocr` | `reference_visibly_different_original_full` | 1 | 1 | 0.0% | 0.0% | 0.0% | — | 0 | 0 |

Macro rates average source-level rates. JSON records the number of sources with a defined denominator for every macro; undefined sources are never silently counted as observations. Safety is shown as source incidence, while the final column is explicitly a pooled event count.

## Offline latency by evidence group

These are case-weighted replay diagnostics within each evidence group, not live deployment latency estimates. Synthetic rows remain correlated with their source.

| Technique | Evidence group | Phase | Samples | p50 | p95 | Max |
|---|---|---|---:|---:|---:|---:|
| `legacy_exact_scale_pr4` | `acceptance_target_family_original_full` | cold total | 1 | 364.4 ms | 364.4 ms | 364.4 ms |
| `legacy_exact_scale_pr4` | `acceptance_visually_equivalent_original_full` | cold total | 6 | 47.8 ms | 95.4 ms | 95.6 ms |
| `legacy_exact_scale_pr4` | `ambiguous_original_full_safety_control` | cold total | 1 | 46.3 ms | 46.3 ms | 46.3 ms |
| `legacy_exact_scale_pr4` | `negative_original_full_safety_control` | cold total | 1 | 87.3 ms | 87.3 ms | 87.3 ms |
| `legacy_exact_scale_pr4` | `reference_supplemental_crop` | cold total | 9 | 13.0 ms | 35.0 ms | 36.1 ms |
| `legacy_exact_scale_pr4` | `reference_visibly_different_original_full` | cold total | 1 | 35.7 ms | 35.7 ms | 35.7 ms |
| `adaptive_templates` | `acceptance_target_family_original_full` | cold total | 1 | 4522.5 ms | 4522.5 ms | 4522.5 ms |
| `adaptive_templates` | `acceptance_target_family_original_full` | discovery | 1 | 4503.9 ms | 4503.9 ms | 4503.9 ms |
| `adaptive_templates` | `acceptance_target_family_original_full` | cached steady | 1 | 4.9 ms | 4.9 ms | 4.9 ms |
| `adaptive_templates` | `acceptance_visually_equivalent_original_full` | cold total | 6 | 3108.0 ms | 4715.3 ms | 4814.9 ms |
| `adaptive_templates` | `acceptance_visually_equivalent_original_full` | discovery | 6 | 3094.9 ms | 4697.5 ms | 4796.4 ms |
| `adaptive_templates` | `acceptance_visually_equivalent_original_full` | cached steady | 5 | 8.0 ms | 21.2 ms | 24.0 ms |
| `adaptive_templates` | `ambiguous_original_full_safety_control` | cold total | 1 | 4201.2 ms | 4201.2 ms | 4201.2 ms |
| `adaptive_templates` | `ambiguous_original_full_safety_control` | discovery | 1 | 4193.6 ms | 4193.6 ms | 4193.6 ms |
| `adaptive_templates` | `negative_original_full_safety_control` | cold total | 1 | 7427.7 ms | 7427.7 ms | 7427.7 ms |
| `adaptive_templates` | `negative_original_full_safety_control` | discovery | 1 | 7419.4 ms | 7419.4 ms | 7419.4 ms |
| `adaptive_templates` | `reference_supplemental_crop` | cold total | 9 | 572.5 ms | 2089.6 ms | 2197.3 ms |
| `adaptive_templates` | `reference_supplemental_crop` | discovery | 9 | 556.2 ms | 2073.8 ms | 2181.3 ms |
| `adaptive_templates` | `reference_supplemental_crop` | cached steady | 9 | 7.0 ms | 8.2 ms | 8.3 ms |
| `adaptive_templates` | `reference_visibly_different_original_full` | cold total | 1 | 3761.2 ms | 3761.2 ms | 3761.2 ms |
| `adaptive_templates` | `reference_visibly_different_original_full` | discovery | 1 | 3752.3 ms | 3752.3 ms | 3752.3 ms |
| `adaptive_templates_ocr` | `acceptance_target_family_original_full` | cold total | 1 | 4703.1 ms | 4703.1 ms | 4703.1 ms |
| `adaptive_templates_ocr` | `acceptance_target_family_original_full` | discovery | 1 | 4690.1 ms | 4690.1 ms | 4690.1 ms |
| `adaptive_templates_ocr` | `acceptance_target_family_original_full` | cached steady | 1 | 4.9 ms | 4.9 ms | 4.9 ms |
| `adaptive_templates_ocr` | `acceptance_visually_equivalent_original_full` | cold total | 6 | 3446.0 ms | 4970.6 ms | 5005.2 ms |
| `adaptive_templates_ocr` | `acceptance_visually_equivalent_original_full` | discovery | 6 | 3430.3 ms | 4952.3 ms | 4986.5 ms |
| `adaptive_templates_ocr` | `acceptance_visually_equivalent_original_full` | cached steady | 6 | 8.2 ms | 20.9 ms | 24.8 ms |
| `adaptive_templates_ocr` | `ambiguous_original_full_safety_control` | cold total | 1 | 5244.5 ms | 5244.5 ms | 5244.5 ms |
| `adaptive_templates_ocr` | `ambiguous_original_full_safety_control` | discovery | 1 | 5237.2 ms | 5237.2 ms | 5237.2 ms |
| `adaptive_templates_ocr` | `negative_original_full_safety_control` | cold total | 1 | 8721.8 ms | 8721.8 ms | 8721.8 ms |
| `adaptive_templates_ocr` | `negative_original_full_safety_control` | discovery | 1 | 8714.1 ms | 8714.1 ms | 8714.1 ms |
| `adaptive_templates_ocr` | `reference_supplemental_crop` | cold total | 9 | 607.8 ms | 2091.4 ms | 2166.4 ms |
| `adaptive_templates_ocr` | `reference_supplemental_crop` | discovery | 9 | 590.6 ms | 2074.8 ms | 2149.3 ms |
| `adaptive_templates_ocr` | `reference_supplemental_crop` | cached steady | 9 | 7.4 ms | 10.0 ms | 11.4 ms |
| `adaptive_templates_ocr` | `reference_visibly_different_original_full` | cold total | 1 | 4746.4 ms | 4746.4 ms | 4746.4 ms |
| `adaptive_templates_ocr` | `reference_visibly_different_original_full` | discovery | 1 | 4729.3 ms | 4729.3 ms | 4729.3 ms |
| `adaptive_templates_ocr` | `reference_visibly_different_original_full` | cached steady | 1 | 7.4 ms | 7.4 ms | 7.4 ms |
| `adaptive_labels_ocr` | `acceptance_target_family_original_full` | cold total | 1 | 4716.3 ms | 4716.3 ms | 4716.3 ms |
| `adaptive_labels_ocr` | `acceptance_target_family_original_full` | discovery | 1 | 4701.2 ms | 4701.2 ms | 4701.2 ms |
| `adaptive_labels_ocr` | `acceptance_target_family_original_full` | cached steady | 1 | 4.0 ms | 4.0 ms | 4.0 ms |
| `adaptive_labels_ocr` | `acceptance_visually_equivalent_original_full` | cold total | 6 | 3706.9 ms | 4950.3 ms | 4972.9 ms |
| `adaptive_labels_ocr` | `acceptance_visually_equivalent_original_full` | discovery | 6 | 3690.6 ms | 4934.4 ms | 4957.7 ms |
| `adaptive_labels_ocr` | `acceptance_visually_equivalent_original_full` | cached steady | 6 | 8.1 ms | 23.5 ms | 28.3 ms |
| `adaptive_labels_ocr` | `ambiguous_original_full_safety_control` | cold total | 1 | 5547.1 ms | 5547.1 ms | 5547.1 ms |
| `adaptive_labels_ocr` | `ambiguous_original_full_safety_control` | discovery | 1 | 5539.3 ms | 5539.3 ms | 5539.3 ms |
| `adaptive_labels_ocr` | `negative_original_full_safety_control` | cold total | 1 | 8167.6 ms | 8167.6 ms | 8167.6 ms |
| `adaptive_labels_ocr` | `negative_original_full_safety_control` | discovery | 1 | 8160.1 ms | 8160.1 ms | 8160.1 ms |
| `adaptive_labels_ocr` | `reference_supplemental_crop` | cold total | 9 | 541.6 ms | 1989.8 ms | 2051.0 ms |
| `adaptive_labels_ocr` | `reference_supplemental_crop` | discovery | 9 | 526.6 ms | 1973.6 ms | 2034.5 ms |
| `adaptive_labels_ocr` | `reference_supplemental_crop` | cached steady | 9 | 6.9 ms | 7.7 ms | 7.8 ms |
| `adaptive_labels_ocr` | `reference_visibly_different_original_full` | cold total | 1 | 4625.8 ms | 4625.8 ms | 4625.8 ms |
| `adaptive_labels_ocr` | `reference_visibly_different_original_full` | discovery | 1 | 4610.1 ms | 4610.1 ms | 4610.1 ms |
| `adaptive_labels_ocr` | `reference_visibly_different_original_full` | cached steady | 1 | 5.5 ms | 5.5 ms | 5.5 ms |
| `adaptive_digits_ocr` | `acceptance_target_family_original_full` | cold total | 1 | 4493.6 ms | 4493.6 ms | 4493.6 ms |
| `adaptive_digits_ocr` | `acceptance_target_family_original_full` | discovery | 1 | 4480.7 ms | 4480.7 ms | 4480.7 ms |
| `adaptive_digits_ocr` | `acceptance_target_family_original_full` | cached steady | 1 | 4.2 ms | 4.2 ms | 4.2 ms |
| `adaptive_digits_ocr` | `acceptance_visually_equivalent_original_full` | cold total | 6 | 3133.0 ms | 4680.3 ms | 4842.5 ms |
| `adaptive_digits_ocr` | `acceptance_visually_equivalent_original_full` | discovery | 6 | 3121.5 ms | 4665.5 ms | 4827.6 ms |
| `adaptive_digits_ocr` | `acceptance_visually_equivalent_original_full` | cached steady | 5 | 8.0 ms | 21.3 ms | 24.2 ms |
| `adaptive_digits_ocr` | `ambiguous_original_full_safety_control` | cold total | 1 | 4207.9 ms | 4207.9 ms | 4207.9 ms |
| `adaptive_digits_ocr` | `ambiguous_original_full_safety_control` | discovery | 1 | 4200.5 ms | 4200.5 ms | 4200.5 ms |
| `adaptive_digits_ocr` | `negative_original_full_safety_control` | cold total | 1 | 7163.5 ms | 7163.5 ms | 7163.5 ms |
| `adaptive_digits_ocr` | `negative_original_full_safety_control` | discovery | 1 | 7154.6 ms | 7154.6 ms | 7154.6 ms |
| `adaptive_digits_ocr` | `reference_supplemental_crop` | cold total | 9 | 542.8 ms | 1990.8 ms | 2040.4 ms |
| `adaptive_digits_ocr` | `reference_supplemental_crop` | discovery | 9 | 527.4 ms | 1975.1 ms | 2024.4 ms |
| `adaptive_digits_ocr` | `reference_supplemental_crop` | cached steady | 9 | 7.2 ms | 8.2 ms | 8.4 ms |
| `adaptive_digits_ocr` | `reference_visibly_different_original_full` | cold total | 1 | 3691.7 ms | 3691.7 ms | 3691.7 ms |
| `adaptive_digits_ocr` | `reference_visibly_different_original_full` | discovery | 1 | 3682.8 ms | 3682.8 ms | 3682.8 ms |

## Results by robustness family

| Technique | Family | Cases | Exact coverage | Value wrong | Location fail | Unknown |
|---|---|---:|---:|---:|---:|---:|
| `legacy_exact_scale_pr4` | `original` | 19 | 5.9% | 0 | 0 | 64 |
| `adaptive_templates` | `original` | 19 | 88.2% | 0 | 0 | 8 |
| `adaptive_templates_ocr` | `original` | 19 | 97.1% | 0 | 0 | 2 |
| `adaptive_labels_ocr` | `original` | 19 | 97.1% | 0 | 0 | 2 |
| `adaptive_digits_ocr` | `original` | 19 | 88.2% | 0 | 0 | 8 |

## Change from baseline

| Technique | Baseline | Exact coverage | Acceptance coverage | Incorrect accepted | Unknown | False panels |
|---|---|---:|---:|---:|---:|---:|
| `adaptive_templates` | `legacy_exact_scale_pr4` | +71.4 pp | +71.4 pp | +0 | -20 | +0 |
| `adaptive_templates_ocr` | `legacy_exact_scale_pr4` | +82.1 pp | +82.1 pp | +0 | -23 | +0 |
| `adaptive_labels_ocr` | `legacy_exact_scale_pr4` | +82.1 pp | +82.1 pp | +0 | -23 | +0 |
| `adaptive_digits_ocr` | `legacy_exact_scale_pr4` | +71.4 pp | +71.4 pp | +0 | -20 | +0 |

The schema-v1 all-case aggregate remains in JSON under `summary.aggregate` for historical comparison only. Historical heldout names are regression labels, not an untouched validation claim. Generated diagnostics and supplemental crops are separated in the evidence scorecards.
