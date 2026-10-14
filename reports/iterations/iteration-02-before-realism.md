# Resource reader benchmark

All techniques ran on the same ordered case IDs. Precision is undefined when a technique accepts no numeric values; abstention is reported through coverage.

**Safety gate:** PASS

| Technique | Exact coverage | Acceptance coverage | Accepted precision | Value wrong | Location fail | Unknown | Missing | Ambiguous | Cold median | Search median | Cached median |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `legacy_exact_scale_pr4` | 1.5% | 1.5% | 100.0% | 0 | 0 | 1468 | 395 | 1 | 12.0 ms | — | — |
| `adaptive_templates` | 35.9% | 35.9% | 100.0% | 0 | 0 | 956 | 244 | 13 | 390.4 ms | 379.3 ms | 7.9 ms |
| `adaptive_templates_ocr` | 65.7% | 65.7% | 100.0% | 0 | 0 | 512 | 102 | 13 | 643.8 ms | 593.3 ms | 10.1 ms |

## Results by evidence category

| Technique | Category | Cases | Exact coverage | Incorrect accepted | Unknown |
|---|---|---:|---:|---:|---:|
| `legacy_exact_scale_pr4` | `generated_perturbation` | 117 | 4.3% | 0 | 332 |
| `legacy_exact_scale_pr4` | `lightweight_numeric_roi` | 273 | 0.4% | 0 | 1088 |
| `legacy_exact_scale_pr4` | `original_full` | 7 | 16.7% | 0 | 20 |
| `legacy_exact_scale_pr4` | `supplemental_crop` | 7 | 0.0% | 0 | 28 |
| `adaptive_templates` | `generated_perturbation` | 117 | 75.5% | 0 | 85 |
| `adaptive_templates` | `lightweight_numeric_roi` | 273 | 20.2% | 0 | 871 |
| `adaptive_templates` | `original_full` | 7 | 100.0% | 0 | 0 |
| `adaptive_templates` | `supplemental_crop` | 7 | 100.0% | 0 | 0 |
| `adaptive_templates_ocr` | `generated_perturbation` | 117 | 81.3% | 0 | 65 |
| `adaptive_templates_ocr` | `lightweight_numeric_roi` | 273 | 59.1% | 0 | 447 |
| `adaptive_templates_ocr` | `original_full` | 7 | 100.0% | 0 | 0 |
| `adaptive_templates_ocr` | `supplemental_crop` | 7 | 100.0% | 0 | 0 |

## Results by robustness family

| Technique | Family | Cases | Exact coverage | Value wrong | Location fail | Unknown |
|---|---|---:|---:|---:|---:|---:|
| `legacy_exact_scale_pr4` | `cached_digit_occlusion` | 13 | 7.7% | 0 | 0 | 36 |
| `legacy_exact_scale_pr4` | `cached_label_occlusion` | 13 | — | 0 | 0 | 0 |
| `legacy_exact_scale_pr4` | `cached_relocation` | 12 | 8.3% | 0 | 0 | 44 |
| `legacy_exact_scale_pr4` | `cached_wrong_size` | 13 | 7.7% | 0 | 0 | 48 |
| `legacy_exact_scale_pr4` | `dense_roi_scale` | 273 | 0.4% | 0 | 0 | 1088 |
| `legacy_exact_scale_pr4` | `duplicate_panel` | 13 | — | 0 | 0 | 0 |
| `legacy_exact_scale_pr4` | `excluded` | 1 | — | 0 | 0 | 0 |
| `legacy_exact_scale_pr4` | `full_scale` | 26 | 0.0% | 0 | 0 | 104 |
| `legacy_exact_scale_pr4` | `original` | 14 | 7.7% | 0 | 0 | 48 |
| `legacy_exact_scale_pr4` | `translated` | 13 | 7.7% | 0 | 0 | 48 |
| `legacy_exact_scale_pr4` | `width_reflow` | 13 | 0.0% | 0 | 0 | 52 |
| `adaptive_templates` | `cached_digit_occlusion` | 13 | 100.0% | 0 | 0 | 0 |
| `adaptive_templates` | `cached_label_occlusion` | 13 | — | 0 | 0 | 0 |
| `adaptive_templates` | `cached_relocation` | 12 | 100.0% | 0 | 0 | 0 |
| `adaptive_templates` | `cached_wrong_size` | 13 | 100.0% | 0 | 0 | 0 |
| `adaptive_templates` | `dense_roi_scale` | 273 | 20.2% | 0 | 0 | 871 |
| `adaptive_templates` | `duplicate_panel` | 13 | — | 0 | 0 | 0 |
| `adaptive_templates` | `excluded` | 1 | — | 0 | 0 | 0 |
| `adaptive_templates` | `full_scale` | 26 | 18.3% | 0 | 0 | 85 |
| `adaptive_templates` | `original` | 14 | 100.0% | 0 | 0 | 0 |
| `adaptive_templates` | `translated` | 13 | 100.0% | 0 | 0 | 0 |
| `adaptive_templates` | `width_reflow` | 13 | 100.0% | 0 | 0 | 0 |
| `adaptive_templates_ocr` | `cached_digit_occlusion` | 13 | 100.0% | 0 | 0 | 0 |
| `adaptive_templates_ocr` | `cached_label_occlusion` | 13 | — | 0 | 0 | 0 |
| `adaptive_templates_ocr` | `cached_relocation` | 12 | 100.0% | 0 | 0 | 0 |
| `adaptive_templates_ocr` | `cached_wrong_size` | 13 | 100.0% | 0 | 0 | 0 |
| `adaptive_templates_ocr` | `dense_roi_scale` | 273 | 59.1% | 0 | 0 | 447 |
| `adaptive_templates_ocr` | `duplicate_panel` | 13 | — | 0 | 0 | 0 |
| `adaptive_templates_ocr` | `excluded` | 1 | — | 0 | 0 | 0 |
| `adaptive_templates_ocr` | `full_scale` | 26 | 37.5% | 0 | 0 | 65 |
| `adaptive_templates_ocr` | `original` | 14 | 100.0% | 0 | 0 | 0 |
| `adaptive_templates_ocr` | `translated` | 13 | 100.0% | 0 | 0 | 0 |
| `adaptive_templates_ocr` | `width_reflow` | 13 | 100.0% | 0 | 0 | 0 |

## Change from baseline

| Technique | Baseline | Exact coverage | Acceptance coverage | Incorrect accepted | Unknown | False panels |
|---|---|---:|---:|---:|---:|---:|
| `adaptive_templates` | `legacy_exact_scale_pr4` | +34.3 pp | +34.3 pp | +0 | -512 | +0 |
| `adaptive_templates_ocr` | `legacy_exact_scale_pr4` | +64.1 pp | +64.1 pp | +0 | -956 | +0 |

Heldout metrics remain available under `summary.by_split.heldout` in JSON. Generated perturbations and supplemental crops are never pooled silently with original full screenshots.
