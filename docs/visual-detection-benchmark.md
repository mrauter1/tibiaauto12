# Visual detection regression benchmark

The expanded benchmark replays the 81 preserved originals through the actual
bot detectors. It adds health/mana bars, battle-list targets, automatic minimap
location and map markers alongside both production Skills readers.

Run from the repository root on Windows, using the application's environment:

```powershell
.venv\Scripts\python.exe tools\benchmark_visual_detection.py
```

Outputs:

- [Readable report](../reports/visual-detection-2026-10-22.md)
- [Predictions, metrics and fingerprints](../reports/visual-detection-2026-10-22.json)
- [Interactive annotation viewer](../reports/visual-annotations-2026-10-22.html)
- [Frozen labels](../tests/fixtures/screenshots/visual-annotations-2026-10-22.json)

This command does not capture the desktop, send inputs, or run pytest. It does
not replace the stricter resource-reader promotion gate. Its default five
cached reads measure repeated original pixels, separately from discovery and
first-read costs. Those numbers cannot establish live capture freshness or
changing-frame performance.

## Annotation contract

Every original has a coverage record. Coordinates are half-open absolute image
boxes. Existing Skills annotations and source hashes are preserved. New labels
were visually transcribed before detector inference, then frozen with a SHA-256
sidecar. Inspection crops and contact sheets were used to read small details;
they are never inference inputs. This is a single-annotator review, not an
independently adjudicated dataset.

Not every visible object is labelled. Unquantified bars and untranscribed battle
panels have explicit exclusions, never implicit zero counts. The report includes
coverage denominators. Source categories and existing development/test membership
remain separate; this is regression evidence, not a newly unseen holdout.

| Feature | Detector input | Independent scoring truth |
|---|---|---|
| Health / mana bar | Original frame and calibrated track rectangle | Manually read current/max text, within 2 percentage points |
| Skills | Original full frame only | Existing numeric labels and row locations |
| Battle target | Original frame, calibrated battle panel, UI scale and configured query name | Visible matching row count, click inside a matching row, selected state |
| Minimap location | Original full frame only | Map rectangle, IoU at least 0.8; correct absence on negative images |
| Marker | Original frame, calibrated map rectangle and configured marker name | Point within any labelled matching icon, or no point on an explicit negative query |

Bar calibration and target/marker ROI detection are not claimed as automatic.
The minimap test calls `GetMapPosition()` inside a supplied original frame scope.
Target and marker tests call `DesktopAdapter`'s production detector methods;
native input/capture dependencies are unavailable in the benchmark adapter.
Custom names use the real asynchronous Windows OCR reader with a two-second
readiness budget; bundled names use the real template path. Exceptions count as
failed detections and appear in the report instead of aborting the corpus.

The SDK/default Skills reader and Resource Monitor's `RetainedResourceReader`
are both evaluated, with a fresh reader and discovery per original. Their
results are scored using the established resource scorer, including unknown,
ambiguity and incorrect-acceptance accounting. Skills discovery has a cooperative
15-second per-image budget; timeouts are recorded as errors, never successes.

## Limits of this collection

The labelled battle subset has no positive Lizard Henchman example. That name
is a negative query here, so its successful recognition remains unvalidated by
this benchmark. Scroll-off creatures are outside visible-row counts; incomplete
names are excluded rather than completed from a species dictionary.

The marker subset has positive Cross icons, including newer circular rendering,
and explicit absent Cross/CheckMark queries. It does not establish recognition
of every marker type. There is no positive arrival example. The static
center-overlap check cannot prove that a waypoint was reached by moving the
character, and it is reported separately from marker location.

The first harness run stopped at a small battle panel because the production
bundled template exceeded the ROI width. The harness was changed to record
detector exceptions and then restarted on all images. No production detector
or frozen label was tuned from that failure.

The final annotation audit corrected one category: positive-marker arrival
labels are unknown because static pixels did not establish that state. The
initial labels and completed initial report are preserved, with the correction
and their hashes recorded in the final artifacts. All bar values, rectangles,
names, counts and selection labels remained unchanged. Every visual detector was
rerun after this correction. The completed Skills measurements were reused
after checking that their production code, assets, original images and timing
parameters were identical; the final JSON records the source report and hash.
