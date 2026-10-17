# Recent screenshot expansion and frozen 80/20 split

Thirty additional **complete, real client screenshots** were selected and
manually annotated before reader inference. The corpus now contains **81
unchanged originals: 71 full screenshots and ten supplemental crops**.

The additions were published July 13–October 14, 2026, after the
[July 13 summer update](https://www.tibia.com/news/?id=8845&subtopic=newsarchive).
Echo Raid and Make Believe content in several captures supports that update
period. These are contemporary public captures, not independently verified
executables: **all exact build numbers, native resolutions, DPI settings and
capture timestamps remain unknown**. Publication dates do not prove a specific
installed patch. The complete login window is an intentional negative case.

## Files and review

- [Original files](../tests/fixtures/screenshots/originals/), with names and
  existing publisher marks preserved; no local resize, crop or enhancement.
- [Manifest](../tests/fixtures/screenshots/manifest.json), including original
  attachment URLs, post/comment dates, dimensions, SHA-256, lineage, visible
  evidence and half-open label/value boxes.
- [Manual visual reviews](../tests/fixtures/screenshots/visual-review.json).
- [Pre-inference collection freeze](../tests/fixtures/screenshots/collection-2026-10-17-latest.json).
- [Authoritative current split](../tests/fixtures/screenshots/split-2026-10-17-latest.json).
- Navigation contact sheets
  [1](../tests/fixtures/screenshots/navigation/collection-2026-10-17-latest-1.jpg) /
  [2](../tests/fixtures/screenshots/navigation/collection-2026-10-17-latest-2.jpg).
  These resized previews are **never benchmark inputs**.

The collection reviewed 197 public attachment candidates. Gameplay-only crops,
monitor photos, web/tool screenshots, illustrations and uncertain/incomplete
client boundaries were rejected. The retained 30 are original still-image
attachments from 20 conservative author/source groups on r/TibiaMMO. This adds
capture diversity but only one publishing community; it is not a representative
sample of ordinary gameplay.

Byte SHA-256 and dimension-tagged decoded-RGB hashes found no identical images
among all 81 originals. A 63-bit DCT perceptual hash with Hamming distance <=10
found no near-duplicate candidate involving an addition. Shared author and
party captures remain grouped even when their perceptual hashes differ.

## Readability and coverage

| New-capture property | Count |
|---|---:|
| Four readable Skills values | 18 images / 72 values |
| Readable Hit Points, other three rows hidden | 4 images / 4 values |
| All four values N/A | 8 images |
| Total readable resource values | 76 |
| Total N/A fields | 44 |
| Complete client body | 29 images |
| Complete desktop containing client | 1 image |
| Conservative source groups | 20 |

N/A reasons are recorded individually: 27 fields hidden by panel height,
12 with Skills absent, four scrolled out of view, and one severely clipped
Hit Points fragment. Four other Hit Points rows are clipped at their lower edge
but retain enough glyph structure to uniquely read the number; they remain
scored readable targets. No hidden value is copied from status bars, equipment,
another frame, or a reader prediction. A death-state image has readable zeros
for both Hit Points and Mana. Compressed green Capacity text is retained where
it is still reliably readable.

The additions include left and right docking, lower panels, dense surrounding
trackers/backpacks, different window proportions, green numeric text, one- to
five-digit values, and thousands separators. Image dimensions range from
1536×793 to 3456×2166; these are attachment dimensions, not verified native
rendering settings. The annotated table is [here](resource-latest-annotations.md).

## Split policy

| Current partition | Full screenshots | Readable integer targets | Purpose |
|---|---:|---:|---|
| Development | 57 (80.28%) | 155 | Refinement and regression checks |
| Test | 14 (19.72%) | 44 | First evaluation of frozen readers |
| Supplemental references | 10, outside ratio | 40 | Diagnostic crop coverage |

The full-image ratio includes natural negatives, ambiguity controls and the
three older full images with different interfaces. Their scorecards remain
separate from visually equivalent positive acceptance. Ambiguity controls
require abstention even though their resource numbers are visible.

All 51 older originals have already been examined in earlier work. Their 41
full screenshots therefore remain in development, and their ten crops remain
reference diagnostics. The new 30 supply 16 development and 14 test images.
No previously inspected image is presented as an untouched holdout.

The split unit is a conservative source group. Repeated author captures and
the Local_Ad3036/Eurbina14 party captures stay together. Groups are ordered by
SHA-256 of `latest-20261017-v1`, newline and group name. The first reachable
whole-group subset of 14 images becomes test. Neither numeric values nor
reader outcomes participate in that assignment. The resulting test set has
11 fully readable images and three all-N/A images, across 11 groups.

The manifest's older `train`/`heldout` values are preserved as historical
metadata. `split-2026-10-17-latest.json` is authoritative for this evaluation;
the new runner explicitly records both memberships. Frozen annotation hashes
and tests prevent silently changing membership or labels after evaluation.

## Frozen evaluation

The production baseline and the previously promoted `accelerated_fast` and
`accelerated_robust` configurations are unchanged from the preceding experiment
branch (`834ff7c`; last reader implementation `8fc417c`). No tuning is performed
during collection or after inspecting the new test results.

Each variant processes every full original autonomously; no annotated crop,
panel position or expected number reaches discovery or reading. For each image
there is a fresh reader and discovery, a separately measured first read, then
50 cached four-resource reads with one OpenCV thread and OCR disabled. The
predeclared cached p95 target is 50 ms. Missing layouts have no cached samples;
their missing readable values remain coverage failures, not N/A.

```powershell
python tools/benchmark_latest_screenshots.py --out .work/latest-screenshots/shard-0.json
```

CI uses six disjoint shards and retains raw JSON/JSONL as downloadable artifacts.
The [benchmark report](../reports/latest-screenshots-2026-10-17.md) separates
development, test, references, N/A safety, wrong values/regions and timing.
Static repeats do not measure capture/action overhead, live freshness or
reacquisition. If test failures later guide tuning, those cases become known
regressions and new untouched test evidence is required.
