# Resource reader benchmark

All techniques ran on the same ordered case IDs. Precision is undefined when a technique accepts no numeric values; abstention is reported through coverage.
The headline table uses only reviewed real full-screenshot acceptance evidence and real negative and multi-panel ambiguity safety controls. Crops, unknown or different renders, and generated cases do not affect acceptance.
The historical heldout split is known-case regression evidence after diagnosis; this report makes no independent heldout generalization claim.

**Acceptance safety gate:** PASS

| Technique | Positive originals | Negative controls | Ambiguity controls | Exact coverage | Acceptance coverage | Accepted precision | Value wrong | Location fail | Unknown | Missing | Ambiguous |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `legacy_exact_scale_pr4` | 7 | 1 | 1 | 14.3% | 14.3% | 100.0% | 0 | 0 | 24 | 8 | 0 |
| `adaptive_templates` | 7 | 1 | 1 | 100.0% | 100.0% | 100.0% | 0 | 0 | 0 | 2 | 0 |
| `adaptive_templates_ocr` | 7 | 1 | 1 | 100.0% | 100.0% | 100.0% | 0 | 0 | 0 | 2 | 0 |
| `adaptive_labels_ocr` | 7 | 1 | 1 | 100.0% | 100.0% | 100.0% | 0 | 0 | 0 | 2 | 0 |
| `adaptive_digits_ocr` | 7 | 1 | 1 | 100.0% | 100.0% | 100.0% | 0 | 0 | 0 | 2 | 0 |

## Results by evidence category

| Technique | Category | Cases | Exact coverage | Incorrect accepted | Unknown |
|---|---|---:|---:|---:|---:|
| `legacy_exact_scale_pr4` | `original_full` | 10 | 12.5% | 0 | 28 |
| `legacy_exact_scale_pr4` | `supplemental_crop` | 9 | 0.0% | 0 | 36 |
| `adaptive_templates` | `original_full` | 10 | 96.9% | 0 | 1 |
| `adaptive_templates` | `supplemental_crop` | 9 | 100.0% | 0 | 0 |
| `adaptive_templates_ocr` | `original_full` | 10 | 96.9% | 0 | 1 |
| `adaptive_templates_ocr` | `supplemental_crop` | 9 | 100.0% | 0 | 0 |
| `adaptive_labels_ocr` | `original_full` | 10 | 96.9% | 0 | 1 |
| `adaptive_labels_ocr` | `supplemental_crop` | 9 | 100.0% | 0 | 0 |
| `adaptive_digits_ocr` | `original_full` | 10 | 96.9% | 0 | 1 |
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
| `adaptive_templates` | `acceptance_visually_equivalent_original_full` | 6 | 6 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_templates` | `ambiguous_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_templates` | `negative_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_templates` | `reference_supplemental_crop` | 9 | 9 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_templates` | `reference_visibly_different_original_full` | 1 | 1 | 75.0% | 75.0% | 75.0% | 100.0% | 0 | 0 |
| `adaptive_templates_ocr` | `acceptance_target_family_original_full` | 1 | 1 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_templates_ocr` | `acceptance_visually_equivalent_original_full` | 6 | 6 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_templates_ocr` | `ambiguous_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_templates_ocr` | `negative_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_templates_ocr` | `reference_supplemental_crop` | 9 | 9 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_templates_ocr` | `reference_visibly_different_original_full` | 1 | 1 | 75.0% | 75.0% | 75.0% | 100.0% | 0 | 0 |
| `adaptive_labels_ocr` | `acceptance_target_family_original_full` | 1 | 1 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_labels_ocr` | `acceptance_visually_equivalent_original_full` | 6 | 6 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_labels_ocr` | `ambiguous_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_labels_ocr` | `negative_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_labels_ocr` | `reference_supplemental_crop` | 9 | 9 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_labels_ocr` | `reference_visibly_different_original_full` | 1 | 1 | 75.0% | 75.0% | 75.0% | 100.0% | 0 | 0 |
| `adaptive_digits_ocr` | `acceptance_target_family_original_full` | 1 | 1 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_digits_ocr` | `acceptance_visually_equivalent_original_full` | 6 | 6 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_digits_ocr` | `ambiguous_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_digits_ocr` | `negative_original_full_safety_control` | 1 | 0 | — | — | — | — | 0 | 0 |
| `adaptive_digits_ocr` | `reference_supplemental_crop` | 9 | 9 | 100.0% | 100.0% | 100.0% | 100.0% | 0 | 0 |
| `adaptive_digits_ocr` | `reference_visibly_different_original_full` | 1 | 1 | 75.0% | 75.0% | 75.0% | 100.0% | 0 | 0 |

Macro rates average source-level rates. JSON records the number of sources with a defined denominator for every macro; undefined sources are never silently counted as observations. Safety is shown as source incidence, while the final column is explicitly a pooled event count.

## Offline latency by evidence group

These are case-weighted replay diagnostics within each evidence group, not live deployment latency estimates. Synthetic rows remain correlated with their source.

| Technique | Evidence group | Phase | Samples | p50 | p95 | Max |
|---|---|---|---:|---:|---:|---:|
| `legacy_exact_scale_pr4` | `acceptance_target_family_original_full` | cold total | 1 | 459.7 ms | 459.7 ms | 459.7 ms |
| `legacy_exact_scale_pr4` | `acceptance_visually_equivalent_original_full` | cold total | 6 | 55.0 ms | 101.6 ms | 103.5 ms |
| `legacy_exact_scale_pr4` | `ambiguous_original_full_safety_control` | cold total | 1 | 48.2 ms | 48.2 ms | 48.2 ms |
| `legacy_exact_scale_pr4` | `negative_original_full_safety_control` | cold total | 1 | 105.4 ms | 105.4 ms | 105.4 ms |
| `legacy_exact_scale_pr4` | `reference_supplemental_crop` | cold total | 9 | 15.1 ms | 55.0 ms | 71.9 ms |
| `legacy_exact_scale_pr4` | `reference_visibly_different_original_full` | cold total | 1 | 47.1 ms | 47.1 ms | 47.1 ms |
| `adaptive_templates` | `acceptance_target_family_original_full` | cold total | 1 | 4851.4 ms | 4851.4 ms | 4851.4 ms |
| `adaptive_templates` | `acceptance_target_family_original_full` | discovery | 1 | 4834.3 ms | 4834.3 ms | 4834.3 ms |
| `adaptive_templates` | `acceptance_target_family_original_full` | cached steady | 1 | 4.7 ms | 4.7 ms | 4.7 ms |
| `adaptive_templates` | `acceptance_visually_equivalent_original_full` | cold total | 6 | 2623.0 ms | 4768.0 ms | 4957.2 ms |
| `adaptive_templates` | `acceptance_visually_equivalent_original_full` | discovery | 6 | 2607.6 ms | 4752.3 ms | 4941.0 ms |
| `adaptive_templates` | `acceptance_visually_equivalent_original_full` | cached steady | 6 | 7.3 ms | 22.0 ms | 25.5 ms |
| `adaptive_templates` | `ambiguous_original_full_safety_control` | cold total | 1 | 4271.2 ms | 4271.2 ms | 4271.2 ms |
| `adaptive_templates` | `ambiguous_original_full_safety_control` | discovery | 1 | 4262.8 ms | 4262.8 ms | 4262.8 ms |
| `adaptive_templates` | `negative_original_full_safety_control` | cold total | 1 | 7681.6 ms | 7681.6 ms | 7681.6 ms |
| `adaptive_templates` | `negative_original_full_safety_control` | discovery | 1 | 7674.2 ms | 7674.2 ms | 7674.2 ms |
| `adaptive_templates` | `reference_supplemental_crop` | cold total | 9 | 612.6 ms | 2015.3 ms | 2104.2 ms |
| `adaptive_templates` | `reference_supplemental_crop` | discovery | 9 | 593.9 ms | 1999.6 ms | 2087.5 ms |
| `adaptive_templates` | `reference_supplemental_crop` | cached steady | 9 | 7.4 ms | 8.0 ms | 8.0 ms |
| `adaptive_templates` | `reference_visibly_different_original_full` | cold total | 1 | 3835.2 ms | 3835.2 ms | 3835.2 ms |
| `adaptive_templates` | `reference_visibly_different_original_full` | discovery | 1 | 3819.8 ms | 3819.8 ms | 3819.8 ms |
| `adaptive_templates` | `reference_visibly_different_original_full` | cached steady | 1 | 6.1 ms | 6.1 ms | 6.1 ms |
| `adaptive_templates_ocr` | `acceptance_target_family_original_full` | cold total | 1 | 4462.7 ms | 4462.7 ms | 4462.7 ms |
| `adaptive_templates_ocr` | `acceptance_target_family_original_full` | discovery | 1 | 4449.8 ms | 4449.8 ms | 4449.8 ms |
| `adaptive_templates_ocr` | `acceptance_target_family_original_full` | cached steady | 1 | 4.2 ms | 4.2 ms | 4.2 ms |
| `adaptive_templates_ocr` | `acceptance_visually_equivalent_original_full` | cold total | 6 | 2754.8 ms | 4736.1 ms | 4811.1 ms |
| `adaptive_templates_ocr` | `acceptance_visually_equivalent_original_full` | discovery | 6 | 2737.9 ms | 4719.9 ms | 4796.4 ms |
| `adaptive_templates_ocr` | `acceptance_visually_equivalent_original_full` | cached steady | 6 | 9.5 ms | 22.7 ms | 26.2 ms |
| `adaptive_templates_ocr` | `ambiguous_original_full_safety_control` | cold total | 1 | 5301.9 ms | 5301.9 ms | 5301.9 ms |
| `adaptive_templates_ocr` | `ambiguous_original_full_safety_control` | discovery | 1 | 5293.6 ms | 5293.6 ms | 5293.6 ms |
| `adaptive_templates_ocr` | `negative_original_full_safety_control` | cold total | 1 | 8589.9 ms | 8589.9 ms | 8589.9 ms |
| `adaptive_templates_ocr` | `negative_original_full_safety_control` | discovery | 1 | 8580.9 ms | 8580.9 ms | 8580.9 ms |
| `adaptive_templates_ocr` | `reference_supplemental_crop` | cold total | 9 | 532.6 ms | 2014.7 ms | 2034.5 ms |
| `adaptive_templates_ocr` | `reference_supplemental_crop` | discovery | 9 | 517.9 ms | 1998.1 ms | 2016.8 ms |
| `adaptive_templates_ocr` | `reference_supplemental_crop` | cached steady | 9 | 6.7 ms | 7.8 ms | 8.1 ms |
| `adaptive_templates_ocr` | `reference_visibly_different_original_full` | cold total | 1 | 3672.2 ms | 3672.2 ms | 3672.2 ms |
| `adaptive_templates_ocr` | `reference_visibly_different_original_full` | discovery | 1 | 3658.9 ms | 3658.9 ms | 3658.9 ms |
| `adaptive_templates_ocr` | `reference_visibly_different_original_full` | cached steady | 1 | 5.5 ms | 5.5 ms | 5.5 ms |
| `adaptive_labels_ocr` | `acceptance_target_family_original_full` | cold total | 1 | 4438.1 ms | 4438.1 ms | 4438.1 ms |
| `adaptive_labels_ocr` | `acceptance_target_family_original_full` | discovery | 1 | 4425.8 ms | 4425.8 ms | 4425.8 ms |
| `adaptive_labels_ocr` | `acceptance_target_family_original_full` | cached steady | 1 | 4.7 ms | 4.7 ms | 4.7 ms |
| `adaptive_labels_ocr` | `acceptance_visually_equivalent_original_full` | cold total | 6 | 3075.3 ms | 4851.1 ms | 5057.4 ms |
| `adaptive_labels_ocr` | `acceptance_visually_equivalent_original_full` | discovery | 6 | 3050.8 ms | 4834.4 ms | 5040.3 ms |
| `adaptive_labels_ocr` | `acceptance_visually_equivalent_original_full` | cached steady | 6 | 9.2 ms | 27.5 ms | 30.8 ms |
| `adaptive_labels_ocr` | `ambiguous_original_full_safety_control` | cold total | 1 | 6250.9 ms | 6250.9 ms | 6250.9 ms |
| `adaptive_labels_ocr` | `ambiguous_original_full_safety_control` | discovery | 1 | 6243.1 ms | 6243.1 ms | 6243.1 ms |
| `adaptive_labels_ocr` | `negative_original_full_safety_control` | cold total | 1 | 8557.0 ms | 8557.0 ms | 8557.0 ms |
| `adaptive_labels_ocr` | `negative_original_full_safety_control` | discovery | 1 | 8549.4 ms | 8549.4 ms | 8549.4 ms |
| `adaptive_labels_ocr` | `reference_supplemental_crop` | cold total | 9 | 582.3 ms | 2063.8 ms | 2139.3 ms |
| `adaptive_labels_ocr` | `reference_supplemental_crop` | discovery | 9 | 567.7 ms | 2047.0 ms | 2122.1 ms |
| `adaptive_labels_ocr` | `reference_supplemental_crop` | cached steady | 9 | 7.0 ms | 8.1 ms | 8.3 ms |
| `adaptive_labels_ocr` | `reference_visibly_different_original_full` | cold total | 1 | 4661.3 ms | 4661.3 ms | 4661.3 ms |
| `adaptive_labels_ocr` | `reference_visibly_different_original_full` | discovery | 1 | 4642.9 ms | 4642.9 ms | 4642.9 ms |
| `adaptive_labels_ocr` | `reference_visibly_different_original_full` | cached steady | 1 | 6.1 ms | 6.1 ms | 6.1 ms |
| `adaptive_digits_ocr` | `acceptance_target_family_original_full` | cold total | 1 | 4925.6 ms | 4925.6 ms | 4925.6 ms |
| `adaptive_digits_ocr` | `acceptance_target_family_original_full` | discovery | 1 | 4913.2 ms | 4913.2 ms | 4913.2 ms |
| `adaptive_digits_ocr` | `acceptance_target_family_original_full` | cached steady | 1 | 4.5 ms | 4.5 ms | 4.5 ms |
| `adaptive_digits_ocr` | `acceptance_visually_equivalent_original_full` | cold total | 6 | 2570.4 ms | 4968.5 ms | 5089.1 ms |
| `adaptive_digits_ocr` | `acceptance_visually_equivalent_original_full` | discovery | 6 | 2555.3 ms | 4951.2 ms | 5071.4 ms |
| `adaptive_digits_ocr` | `acceptance_visually_equivalent_original_full` | cached steady | 6 | 8.2 ms | 21.6 ms | 25.2 ms |
| `adaptive_digits_ocr` | `ambiguous_original_full_safety_control` | cold total | 1 | 4363.0 ms | 4363.0 ms | 4363.0 ms |
| `adaptive_digits_ocr` | `ambiguous_original_full_safety_control` | discovery | 1 | 4355.5 ms | 4355.5 ms | 4355.5 ms |
| `adaptive_digits_ocr` | `negative_original_full_safety_control` | cold total | 1 | 7744.6 ms | 7744.6 ms | 7744.6 ms |
| `adaptive_digits_ocr` | `negative_original_full_safety_control` | discovery | 1 | 7737.3 ms | 7737.3 ms | 7737.3 ms |
| `adaptive_digits_ocr` | `reference_supplemental_crop` | cold total | 9 | 646.6 ms | 2106.2 ms | 2147.7 ms |
| `adaptive_digits_ocr` | `reference_supplemental_crop` | discovery | 9 | 631.0 ms | 2088.9 ms | 2131.0 ms |
| `adaptive_digits_ocr` | `reference_supplemental_crop` | cached steady | 9 | 8.0 ms | 12.2 ms | 14.5 ms |
| `adaptive_digits_ocr` | `reference_visibly_different_original_full` | cold total | 1 | 3909.2 ms | 3909.2 ms | 3909.2 ms |
| `adaptive_digits_ocr` | `reference_visibly_different_original_full` | discovery | 1 | 3895.4 ms | 3895.4 ms | 3895.4 ms |
| `adaptive_digits_ocr` | `reference_visibly_different_original_full` | cached steady | 1 | 6.5 ms | 6.5 ms | 6.5 ms |

## Results by robustness family

| Technique | Family | Cases | Exact coverage | Value wrong | Location fail | Unknown |
|---|---|---:|---:|---:|---:|---:|
| `legacy_exact_scale_pr4` | `original` | 19 | 5.9% | 0 | 0 | 64 |
| `adaptive_templates` | `original` | 19 | 98.5% | 0 | 0 | 1 |
| `adaptive_templates_ocr` | `original` | 19 | 98.5% | 0 | 0 | 1 |
| `adaptive_labels_ocr` | `original` | 19 | 98.5% | 0 | 0 | 1 |
| `adaptive_digits_ocr` | `original` | 19 | 98.5% | 0 | 0 | 1 |

## Change from baseline

| Technique | Baseline | Exact coverage | Acceptance coverage | Incorrect accepted | Unknown | False panels |
|---|---|---:|---:|---:|---:|---:|
| `adaptive_templates` | `legacy_exact_scale_pr4` | +85.7 pp | +85.7 pp | +0 | -24 | +0 |
| `adaptive_templates_ocr` | `legacy_exact_scale_pr4` | +85.7 pp | +85.7 pp | +0 | -24 | +0 |
| `adaptive_labels_ocr` | `legacy_exact_scale_pr4` | +85.7 pp | +85.7 pp | +0 | -24 | +0 |
| `adaptive_digits_ocr` | `legacy_exact_scale_pr4` | +85.7 pp | +85.7 pp | +0 | -24 | +0 |

The schema-v1 all-case aggregate remains in JSON under `summary.aggregate` for historical comparison only. Historical heldout names are regression labels, not an untouched validation claim. Generated diagnostics and supplemental crops are separated in the evidence scorecards.
