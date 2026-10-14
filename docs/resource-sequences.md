# Resource capture sequences

Resource sequences are lossless, full-client-frame recordings for replaying the
production `AdaptiveResourceTracker`. They complement the observation-only
policy traces described in [recording-replay.md](recording-replay.md); the two
formats have different evidence and should not be relabeled as one another.

## Format and evidence

A session is a directory containing `session.json` and content-addressed PNGs
under `frames/`. The manifest records:

- session origin: `native_capture`, `imported_native`, or `derived_corpus`;
- client build/family, operating system, UI and display scale, capture provider,
  coordinate space, and recording time. Unknown facts are explicit `null`;
- every original `Frame.captured_at`, source string, dimensions, encoded-file
  SHA-256, pixel SHA-256, and relative time;
- per-frame reviewed or unreviewed panel truth, including its box, visibility,
  readability, and reason;
- the same status for all four resource rows, with separate label and value
  boxes, expected value, visibility, readability, and reason;
- capture-freshness status, reason, and independent evidence; and
- reviewed or unreviewed onset events with a frame, timestamp, resource,
  expected value (or unknown for invalidation), and declared deadline.

The loader verifies all hashes, sizes, dimensions, monotonic timestamps, boxes,
event references, and paths before returning any frame. Repeated identical
frames reuse one PNG but retain separate frame records and timestamps.
Decoded pixels are capped at 1 GiB per session. Split longer recordings into
bounded clips, retaining the same provenance/session-family identity; quota overflow
is a validation error, never an unknown recognition result.

An unannotated capture is recorded as `annotation_status: unreviewed` with null
visibility/readability/value fields. It does **not** mean hidden or unreadable.
Replay reports exclude unreviewed truth and cannot pass solely on draft data.
Likewise, a timestamp or identical pixels do not prove capture freshness. Only a
reviewed `capture_freshness.state: fresh` annotation makes a readable frame
eligible for “correct and fresh” scoring.

## Collect a native session

Run the recorder in the same desktop session and environment used by the
application. It calls the production, process-isolated `CaptureProcess`, and
passes the returned `Frame` objects directly to `ResourceSequenceRecorder`:

```bash
python tools/record_resource_sequence.py capture \
  captures/hunt-20261014 \
  --session-id hunt-20261014 \
  --count 120 \
  --interval 0.1 \
  --capture-timeout 2
```

Client build/family and UI/display scale are optional. Supply them only when
observed or operator-reported for this collection; omitted values remain null.

Do not type truth live. The command creates a valid unreviewed capture draft
whose frames and timestamps can be reviewed later. Export one JSONL annotation
object per frame from that review, then create a new validated session (the
draft remains unchanged):

```bash
python tools/record_resource_sequence.py annotate \
  captures/hunt-20261014 annotations/hunt-20261014.jsonl \
  captures/hunt-20261014-reviewed
```

Each reviewed row must bind to the recorded frame using
`expected_pixel_sha256` and `expected_dimensions`, copied from `session.json`.
This prevents a shifted annotation list from labeling a different capture. An
annotation row has this shape:

```json
{"expected_pixel_sha256":"<SHA-256 from session.json>","expected_dimensions":[1919,1079],"panel":{"annotation_status":"reviewed","visible":true,"readable":true,"reason":"panel fully visible","box":[1481,29,1697,313]},"resources":{"resource_health":{"annotation_status":"reviewed","visible":true,"readable":true,"reason":"label and value legible","label_box":[1484,58,1638,74],"value_box":[1644,61,1673,72],"value":710},"resource_mana":{"annotation_status":"unreviewed","visible":null,"readable":null,"reason":"not reviewed","label_box":null,"value_box":null,"value":null},"resource_soul":{"annotation_status":"unreviewed","visible":null,"readable":null,"reason":"not reviewed","label_box":null,"value_box":null,"value":null},"resource_capacity":{"annotation_status":"unreviewed","visible":null,"readable":null,"reason":"not reviewed","label_box":null,"value_box":null,"value":null}},"capture_freshness":{"annotation_status":"reviewed","state":"fresh","reason":"external frame counter advanced and capture completed in order","evidence":"operator log frame counter 4812"},"events":[{"id":"health-change-1","resource":"resource_health","kind":"change","expected_value":710,"deadline_ms":250,"annotation_status":"reviewed"}]}
```

All four resource objects are required even when some remain unreviewed. Boxes
use half-open client-local pixel coordinates. A visible/readable row requires
both its label box and value box. Deadlines are declared annotations for the
session; this benchmark does not establish that those numbers are product SLOs.

Saved full-frame streams can be imported when every line supplies a path and an
explicit capture timestamp. `--source-assertion` is required so the basis for
calling the material native is reviewable. Every row also supplies
`source_sha256` for the original file, and paths must remain beneath the JSONL
directory. The source operating system defaults to null because the importing
host is not evidence about the capture host:

```bash
python tools/record_resource_sequence.py import-native \
  saved-stream.jsonl captures/imported-hunt \
  --session-id imported-hunt \
  --source-assertion "PNG frames and CLOCK_REALTIME timestamps exported by capture host run 42"
```

The importer never infers timestamps from file modification times. Generated or
transformed data must never be imported or relabeled as native.

## Acceptance evidence

Acceptance replay uses only original, full-frame native recordings collected by
`capture` or integrity-checked native streams supplied to `import-native`. No
physical native recording is currently checked into this repository, so native
acceptance evidence is unavailable until a collection is supplied and reviewed.

The schema retains `derived_corpus` as an explicit origin for historical
development/stress sessions. Such sessions remain separated in reports and
never count as native acceptance evidence. This collection tool does not create
resized, relocated, occluded, composited, or otherwise artificial sessions.

## Offline original-image latency

`benchmark_resource_latency.py` measures the reader's algorithm time on the
preserved original full screenshots in the validated corpus. Positive inputs
must be documented real captures with reviewed `target_family` or
`visually_equivalent` rendering; real negative originals are cold discovery
safety controls. A reviewed original containing multiple valid resource panels
is a separate ambiguity safety control: discovery must abstain with no selected
layout, and the benchmark performs no resource reads for that control. Generated,
resized, relocated, occluded, and cropped cases are never loaded by this benchmark.

```bash
python tools/benchmark_resource_latency.py \
  --warm-reads 50 \
  --json-out .work/resource-original-latency.json \
  --markdown-out .work/resource-original-latency.md
```

For every positive source, the tool measures one full-frame discovery followed
by at least 50 cached `ResourceReader.read` calls on the unchanged original. It
records every individual timing and scores every read for both exact numeric
value and row-region correspondence using the same comparator scorer. Negative
originals and multi-panel ambiguity controls must not yield a discovered panel.
They are reported as separate cold-control counts and do not enter cached-read
timing or numeric-correctness denominators. `--warm-reads` is bounded from 50
through 1000, and each source runs in a killable worker with `--timeout`.

No reading-time threshold is assumed. A caller can declare one with
`--reading-budget-ms`; when present, every cached read must meet it. Reports bind
the environment, reader/profile/tool hashes, manifest SHA-256, and each source
and decoded-pixel hash, while checking that inputs and code stay unchanged.

This is repeated-image, offline algorithm latency. It does not measure desktop
capture, frame delivery, scheduling, input latency, changing values, freshness,
or live-session behavior.

## Replay and report

Replay one or more session directories and write machine-readable and reviewable
reports:

```bash
python tools/benchmark_resource_sequences.py \
  captures/hunt-20261014-reviewed \
  --poll-interval 0.02 \
  --wall-timeout 90 \
  --json-out .work/resource-sequences.json \
  --markdown-out .work/resource-sequences.md
```

Replay preserves the recorded inter-frame timing by default and polls the
tracker while each frame is current. `--playback-rate` can accelerate exploratory
runs, but changes the wall time available to asynchronous discovery. Deadline
evidence is produced only at 1x playback; accelerated stress/exploratory runs
explicitly exclude their deadline rows from scoring. Excessive frame gaps are
rejected by `--max-gap-ms` instead of silently compressed. Each session also
runs in a killable worker with a hard outer deadline, so stuck native or
recognition work cannot hang a suite.

The report includes:

- correct-and-fresh results before each declared onset deadline;
- every accepted wrong value and stale intermediate value;
- known outputs on reviewed unreadable/hidden frames, including reacceptance
  after an initially successful invalidation;
- known outputs from frames independently reviewed as stale capture input,
  reported separately from stale value transients on fresh changed frames;
- label/value association errors using the tracker's `effective_region` and
  `layout_bounds`, even when the number itself happens to match;
- reviewed, visible, visible-and-readable, and fresh-scoring coverage;
- duration-weighted actual reader availability on reviewed fresh readable time:
  correct-and-associated, unknown (including before the first delivery and after
  each frame boundary), and wrong-known durations and rates. These availability
  metrics are informational until native SLOs are declared;
- invalidation and recovery delay p50, p95, and maximum;
- unknown intervals for each resource; and
- by-session macro deadline and visibility/readability rates, so a long session
  cannot dominate several shorter sessions, plus separate native, imported-native,
  and derived-origin summaries.

Every session result binds the exact `session.json` SHA-256, provenance, source
lineage, replay options, and implementation hashes into a run fingerprint. The
report also records the runtime environment. Replay fails if the session
manifest or benchmark implementation changes during the run.

This is a production-tracker recognition benchmark. It does not exercise or
certify the native capture process, acquisition latency, dropped-frame behavior,
or upstream freshness. Those properties require capture-path telemetry and
independent freshness evidence from a real collection run.
