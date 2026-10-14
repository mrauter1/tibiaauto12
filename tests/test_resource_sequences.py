from __future__ import annotations

import json
import multiprocessing
import os
from hashlib import sha256
from time import perf_counter
from time import sleep
from types import SimpleNamespace

import numpy as np
import pytest

from core.perception import Frame
from tibia_client.models import Quality, Reading
from tibia_client.resource_sequences import (
    RESOURCE_FIELDS,
    ResourceSequenceError,
    ResourceSequenceRecorder,
    load_resource_sequence,
    unreviewed_truth,
)
from tools import benchmark_resource_sequences as benchmark
from tools import record_resource_sequence as recorder_tool


def _emit_large_worker_payload(queue):
    queue.put({"ok": {"blob": "x" * (2 * 1024 * 1024)}})
    queue.close()
    queue.join_thread()


def provenance():
    return {
        "recorded_at_utc": None,
        "client_build": None,
        "client_family": None,
        "operating_system": None,
        "ui_scale": None,
        "display_scale": None,
        "capture_provider": "test",
        "capture_coordinate_space": "test pixels",
    }


def truth(value, *, reviewed=True, fresh=True, readable=True):
    if not reviewed:
        return unreviewed_truth()
    return {
        "panel": {
            "annotation_status": "reviewed",
            "visible": True,
            "readable": readable,
            "reason": "test truth",
            "box": [0, 0, 20, 20],
        },
        "resources": {
            field: {
                "annotation_status": "reviewed",
                "visible": True,
                "readable": readable,
                "reason": "test truth",
                "label_box": [1, 1 + index * 4, 7, 4 + index * 4],
                "value_box": [8, 1 + index * 4, 14, 4 + index * 4],
                "value": value + index if readable else None,
            }
            for index, field in enumerate(RESOURCE_FIELDS)
        },
        "capture_freshness": {
            "annotation_status": "reviewed" if fresh else "unreviewed",
            "state": "fresh" if fresh else "unknown",
            "reason": "independent test evidence" if fresh else "not reviewed",
            "evidence": "counter" if fresh else None,
        },
    }


def make_sequence(tmp_path, *, third_unreviewed=False, deadline=100.0):
    path = tmp_path / "sequence"
    pixels = np.zeros((20, 20, 3), dtype=np.uint8)
    with ResourceSequenceRecorder(
        path,
        session_id="test-sequence",
        origin="derived_corpus",
        provenance=provenance(),
        source_lineage={"source": "unit-test", "native_evidence": False},
    ) as recorder:
        recorder.record(
            Frame(pixels, captured_at=1.0, source="first"),
            **truth(10),
            events=[{
                "id": "startup-health",
                "resource": "resource_health",
                "kind": "startup",
                "expected_value": 10,
                "deadline_ms": deadline,
                "annotation_status": "reviewed",
            }],
        )
        recorder.record(
            Frame(pixels + 1, captured_at=2.0, source="second"),
            **truth(20),
            events=[{
                "id": "change-health",
                "resource": "resource_health",
                "kind": "change",
                "expected_value": 20,
                "deadline_ms": deadline,
                "annotation_status": "reviewed",
            }],
        )
        if third_unreviewed:
            recorder.record(Frame(pixels + 2, captured_at=3.0), **truth(30, reviewed=False))
    return path


def make_invalidation_sequence(tmp_path, *, stale_capture=False):
    path = tmp_path / ("stale-sequence" if stale_capture else "hidden-sequence")
    pixels = np.zeros((20, 20, 3), dtype=np.uint8)
    with ResourceSequenceRecorder(
        path,
        session_id=path.name,
        origin="derived_corpus",
        provenance=provenance(),
        source_lineage={"source": "unit-test", "native_evidence": False},
    ) as recorder:
        recorder.record(Frame(pixels, captured_at=1), **truth(10))
        changed = truth(20, readable=stale_capture)
        if stale_capture:
            changed["capture_freshness"] = {
                "annotation_status": "reviewed",
                "state": "stale",
                "reason": "external counter did not advance",
                "evidence": "test capture counter remained 7",
            }
            events = []
        else:
            events = [{
                "id": "hide-health",
                "resource": "resource_health",
                "kind": "invalidation",
                "expected_value": None,
                "deadline_ms": 100,
                "annotation_status": "reviewed",
            }]
        recorder.record(Frame(pixels + 1, captured_at=2), **changed, events=events)
    return path


def diagnostics(sequence, frame_index):
    result = {}
    frame = sequence.frames[frame_index]
    for field in RESOURCE_FIELDS:
        item = frame.resources[field]
        result[field] = {
            "effective_region": item["value_box"],
            "layout_bounds": frame.panel["box"],
            "layout_revision": f"layout-{frame_index}",
            "anchor_method": "test",
        }
    return result


def values(base):
    return {field: base + index for index, field in enumerate(RESOURCE_FIELDS)}


def test_sequence_roundtrip_preserves_frame_timestamp_hash_truth_and_deduplicates(tmp_path):
    path = tmp_path / "sequence"
    pixels = np.zeros((20, 20, 3), dtype=np.uint8)
    with ResourceSequenceRecorder(
        path,
        session_id="roundtrip",
        origin="native_capture",
        provenance=provenance(),
    ) as recorder:
        recorder.record(Frame(pixels, captured_at=123.25, source="capture"))
        recorder.record(Frame(pixels, captured_at=123.5, source="capture"))

    sequence = load_resource_sequence(path)

    assert [item.frame.captured_at for item in sequence.frames] == [123.25, 123.5]
    assert sequence.frames[0].pixel_sha256 == sequence.frames[1].pixel_sha256
    assert len(list((path / "frames").iterdir())) == 1
    assert all(
        item["annotation_status"] == "unreviewed"
        for item in sequence.frames[0].resources.values()
    )


def test_dedup_identity_includes_dimensions_for_equal_rgb_byte_streams(tmp_path):
    path = tmp_path / "reshape-sequence"
    with ResourceSequenceRecorder(
        path,
        session_id="reshape",
        origin="native_capture",
        provenance=provenance(),
    ) as recorder:
        recorder.record(Frame(np.zeros((2, 2, 3), dtype=np.uint8), captured_at=1))
        recorder.record(Frame(np.zeros((1, 4, 3), dtype=np.uint8), captured_at=2))

    sequence = load_resource_sequence(path)

    assert [item.frame.size for item in sequence.frames] == [(2, 2), (4, 1)]
    assert len(list((path / "frames").iterdir())) == 2


def test_loader_rejects_symlinked_frame_directory(tmp_path):
    path = make_sequence(tmp_path)
    outside = tmp_path / "outside-frames"
    (path / "frames").rename(outside)
    os.symlink(outside, path / "frames", target_is_directory=True)

    with pytest.raises(ResourceSequenceError, match="symlink"):
        load_resource_sequence(path)


def test_loader_rejects_symlinked_final_frame_file(tmp_path):
    path = make_sequence(tmp_path)
    document = json.loads((path / "session.json").read_text(encoding="utf-8"))
    frame_path = path / document["frames"][0]["path"]
    outside = tmp_path / "outside.png"
    frame_path.replace(outside)
    try:
        os.symlink(outside, frame_path)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    with pytest.raises(ResourceSequenceError, match="symlink"):
        load_resource_sequence(path)


def test_reviewed_event_must_match_reviewed_fresh_onset_truth(tmp_path):
    path = tmp_path / "invalid-event"
    pixels = np.zeros((20, 20, 3), dtype=np.uint8)

    with pytest.raises(ResourceSequenceError, match="conflicts with onset-frame truth"):
        with ResourceSequenceRecorder(
            path,
            session_id="invalid-event",
            origin="derived_corpus",
            provenance=provenance(),
            source_lineage={"source": "test"},
        ) as recorder:
            recorder.record(
                Frame(pixels, captured_at=1),
                **truth(10),
                events=[{
                    "id": "invented-target",
                    "resource": "resource_health",
                    "kind": "change",
                    "expected_value": 999,
                    "deadline_ms": 100,
                    "annotation_status": "reviewed",
                }],
            )
    assert not path.exists()


@pytest.mark.parametrize("corruption", ["escape", "hash"])
def test_loader_rejects_path_escape_and_hash_corruption_before_returning_frames(tmp_path, corruption):
    path = make_sequence(tmp_path)
    manifest = path / "session.json"
    document = json.loads(manifest.read_text())
    if corruption == "escape":
        document["frames"][0]["path"] = "../outside.png"
    else:
        document["frames"][0]["file_sha256"] = "0" * 64
    manifest.write_text(json.dumps(document))

    with pytest.raises(ResourceSequenceError, match="unsafe path|SHA-256"):
        load_resource_sequence(path)


def test_scorer_reports_stale_then_wrong_known_and_uses_reviewed_frame_denominators(tmp_path):
    sequence = load_resource_sequence(make_sequence(tmp_path, third_unreviewed=True))
    observations = [
        {"at": 1.0, "values": values(10), "diagnostics": diagnostics(sequence, 0)},
        # The old accepted health value is a stale transient after the onset.
        {"at": 2.0, "values": values(10), "diagnostics": diagnostics(sequence, 1)},
        {"at": 2.05, "values": {**values(20), "resource_health": 999}, "diagnostics": diagnostics(sequence, 1)},
        {"at": 2.10, "values": values(20), "diagnostics": diagnostics(sequence, 1)},
        {"at": 3.0, "values": values(30), "diagnostics": {}},
    ]

    result = benchmark.score_observations(sequence, observations)

    health_errors = [item for item in result["accepted_errors"] if item["resource"] == "resource_health"]
    assert [item["classification"] for item in health_errors] == ["stale_transient", "wrong_known"]
    assert result["events"][1]["delay_ms"] == pytest.approx(100.0)
    assert result["events"][1]["met_deadline"] is True
    coverage = result["coverage"]["resource_health"]
    assert coverage["reviewed_frames"] == 2
    assert coverage["unreviewed_frames"] == 1
    assert coverage["reviewed_coverage_rate"] == pytest.approx(2 / 3)


def test_scorer_requires_region_association_even_when_value_is_correct(tmp_path):
    sequence = load_resource_sequence(make_sequence(tmp_path))
    wrong = diagnostics(sequence, 0)
    wrong["resource_health"] = {
        **wrong["resource_health"],
        # Spanning every row must not count as association merely because it
        # overlaps the expected health value box.
        "effective_region": [0, 0, 20, 20],
    }
    observations = [
        {"at": 1.0, "values": values(10), "diagnostics": wrong},
        {"at": 2.0, "values": values(20), "diagnostics": diagnostics(sequence, 1)},
    ]

    result = benchmark.score_observations(sequence, observations)

    assert result["accepted_error_count"] == 0
    assert result["association_error_count"] == 1
    assert result["association_errors"][0]["resource"] == "resource_health"
    assert not result["passed"]


def test_unknown_intervals_and_deadline_failure_use_observation_timing(tmp_path):
    sequence = load_resource_sequence(make_sequence(tmp_path, deadline=50))
    observations = [
        {"at": 1.0, "values": values(10), "diagnostics": diagnostics(sequence, 0)},
        {"at": 2.0, "values": {**values(20), "resource_health": None}, "diagnostics": diagnostics(sequence, 1)},
        {"at": 2.08, "values": {**values(20), "resource_health": None}, "diagnostics": diagnostics(sequence, 1)},
        {"at": 2.10, "values": values(20), "diagnostics": diagnostics(sequence, 1)},
    ]

    result = benchmark.score_observations(sequence, observations)

    assert result["events"][1]["delay_ms"] == pytest.approx(100.0)
    assert result["events"][1]["met_deadline"] is False
    assert result["coverage"]["resource_health"]["unknown_intervals"] == [
        {"start": 2.0, "end": 2.05, "duration_ms": pytest.approx(50.0)}
    ]


def test_invalidation_fails_sustained_safety_when_known_value_reappears(tmp_path):
    sequence = load_resource_sequence(make_invalidation_sequence(tmp_path))
    unknown = {field: None for field in RESOURCE_FIELDS}
    unsafe = {**unknown, "resource_health": 10}
    observations = [
        {"at": 1.0, "values": values(10), "diagnostics": diagnostics(sequence, 0)},
        {"at": 2.0, "values": unknown, "diagnostics": {}},
        {"at": 2.05, "values": unsafe, "diagnostics": {}},
    ]

    result = benchmark.score_observations(sequence, observations)
    event = result["events"][0]

    assert event["delay_ms"] == 0
    assert event["met_deadline"] is True
    assert event["safety_held"] is False
    assert event["passed"] is False
    assert event["unsafe_known_after_invalidation"] == [
        {
            "observation": 2,
            "at": 2.05,
            "actual": 10,
            "classification": "unsafe_known_on_unreadable",
        }
    ]
    assert result["unsafe_known_on_unreadable_count"] == 1
    assert result["deadline_success_rate"] == 0
    assert not result["passed"]


def test_known_output_from_reviewed_stale_capture_is_separate_safety_violation(tmp_path):
    sequence = load_resource_sequence(
        make_invalidation_sequence(tmp_path, stale_capture=True)
    )
    observations = [
        {"at": 1.0, "values": values(10), "diagnostics": diagnostics(sequence, 0)},
        {"at": 2.0, "values": values(20), "diagnostics": diagnostics(sequence, 1)},
    ]

    result = benchmark.score_observations(sequence, observations)

    assert result["accepted_known_from_stale_capture_count"] == 4
    assert result["unsafe_known_on_unreadable_count"] == 0
    assert {item["classification"] for item in result["safety_violations"]} == {
        "accepted_known_from_stale_capture"
    }
    assert result["accepted_error_count"] == 0
    assert not result["passed"]


def test_actual_availability_counts_one_correct_poll_then_long_unknown_interval(tmp_path):
    sequence = load_resource_sequence(make_sequence(tmp_path, deadline=100))
    observations = [
        {"at": 1.0, "values": values(10), "diagnostics": diagnostics(sequence, 0)},
        {"at": 1.1, "values": {field: None for field in RESOURCE_FIELDS}, "diagnostics": {}},
        {"at": 2.0, "values": {field: None for field in RESOURCE_FIELDS}, "diagnostics": {}},
    ]

    result = benchmark.score_observations(sequence, observations)
    coverage = result["coverage"]["resource_health"]

    assert coverage["inference_eligible_ms"] == pytest.approx(1100)
    assert coverage["time_correct_fresh_ms"] == pytest.approx(100)
    assert coverage["time_unknown_on_readable_ms"] == pytest.approx(1000)
    assert coverage["time_wrong_known_on_readable_ms"] == 0
    assert coverage["time_correct_fresh_rate"] == pytest.approx(1 / 11)
    assert (
        coverage["time_correct_fresh_ms"]
        + coverage["time_unknown_on_readable_ms"]
        + coverage["time_wrong_known_on_readable_ms"]
    ) == pytest.approx(coverage["inference_eligible_ms"])


def test_actual_availability_resets_to_unknown_at_frame_boundary(tmp_path):
    path = tmp_path / "same-value-next-frame"
    pixels = np.zeros((20, 20, 3), dtype=np.uint8)
    with ResourceSequenceRecorder(
        path,
        session_id="same-value-next-frame",
        origin="derived_corpus",
        provenance=provenance(),
        source_lineage={"source": "unit-test"},
    ) as recorder:
        recorder.record(Frame(pixels, captured_at=1), **truth(10))
        recorder.record(Frame(pixels + 1, captured_at=2), **truth(10))
    sequence = load_resource_sequence(path)
    observations = [{
        "at": 1.1,
        "started_at": 1.1,
        "values": values(10),
        "diagnostics": diagnostics(sequence, 0),
        "presented_frame": 0,
    }]

    coverage = benchmark.score_observations(sequence, observations)["coverage"][
        "resource_health"
    ]

    assert coverage["inference_eligible_ms"] == pytest.approx(1050)
    assert coverage["time_correct_fresh_ms"] == pytest.approx(900)
    assert coverage["time_unknown_on_readable_ms"] == pytest.approx(150)


def test_invalidation_sustained_safety_rejects_known_from_later_stale_capture(tmp_path):
    path = tmp_path / "stale-after-invalidation"
    pixels = np.zeros((20, 20, 3), dtype=np.uint8)
    hidden = truth(20, readable=False)
    stale = truth(20, readable=True)
    stale["capture_freshness"] = {
        "annotation_status": "reviewed",
        "state": "stale",
        "reason": "capture counter did not advance",
        "evidence": "counter stayed 8",
    }
    with ResourceSequenceRecorder(
        path,
        session_id="stale-after-invalidation",
        origin="derived_corpus",
        provenance=provenance(),
        source_lineage={"source": "unit-test"},
    ) as recorder:
        recorder.record(Frame(pixels, captured_at=1), **truth(10))
        recorder.record(
            Frame(pixels + 1, captured_at=2),
            **hidden,
            events=[{
                "id": "hide-health",
                "resource": "resource_health",
                "kind": "invalidation",
                "expected_value": None,
                "deadline_ms": 100,
                "annotation_status": "reviewed",
            }],
        )
        recorder.record(Frame(pixels + 2, captured_at=2.02), **stale)
    sequence = load_resource_sequence(path)
    unknown = {field: None for field in RESOURCE_FIELDS}
    observations = [
        {"at": 1.0, "values": values(10), "diagnostics": diagnostics(sequence, 0)},
        {"at": 2.0, "values": unknown, "diagnostics": {}},
        {"at": 2.03, "values": values(20), "diagnostics": diagnostics(sequence, 2)},
    ]

    result = benchmark.score_observations(sequence, observations)
    event = result["events"][0]

    assert event["met_deadline"] is True
    assert event["safety_held"] is False
    assert event["passed"] is False
    assert event["unsafe_known_after_invalidation"][0]["classification"] == (
        "accepted_known_from_stale_capture"
    )


def test_unknown_freshness_is_evidence_gap_without_invented_safety_violation(tmp_path):
    path = tmp_path / "unknown-freshness"
    pixels = np.zeros((20, 20, 3), dtype=np.uint8)
    uncertain = truth(10)
    uncertain["capture_freshness"] = {
        "annotation_status": "reviewed",
        "state": "unknown",
        "reason": "no independent freshness signal was recorded",
        "evidence": None,
    }
    with ResourceSequenceRecorder(
        path,
        session_id="unknown-freshness",
        origin="derived_corpus",
        provenance=provenance(),
        source_lineage={"source": "unit-test", "native_evidence": False},
    ) as recorder:
        recorder.record(Frame(pixels, captured_at=1), **uncertain)
    sequence = load_resource_sequence(path)
    observations = [{
        "at": 1.0,
        "values": values(10),
        "diagnostics": diagnostics(sequence, 0),
    }]

    result = benchmark.score_observations(sequence, observations)

    assert result["safety_violation_count"] == 0
    assert result["annotation_complete"] is True
    assert result["freshness_evidence_complete"] is False
    assert not result["passed"]


def test_replay_has_finite_wall_timeout_when_async_tracker_never_completes(tmp_path, monkeypatch):
    sequence = load_resource_sequence(make_sequence(tmp_path, deadline=10_000))

    class NeverCompletes:
        def read(self, _frame, requests):
            sleep(0.2)
            return {sensor: Reading(None, Quality.UNKNOWN, "still pending") for sensor in requests}

        def close(self):
            pass

    monkeypatch.setattr(benchmark, "AdaptiveResourceTracker", NeverCompletes)
    monkeypatch.setattr(benchmark, "_last_hold_seconds", lambda *_args: 0.01)
    started = perf_counter()
    with pytest.raises(TimeoutError, match="wall timeout"):
        benchmark.replay_tracker(
            sequence,
            poll_interval=0.005,
            playback_rate=100,
            max_gap_ms=20_000,
            wall_timeout=0.1,
        )
    assert perf_counter() - started < 0.5


def test_non_realtime_playback_excludes_deadline_claims(tmp_path):
    sequence = load_resource_sequence(make_sequence(tmp_path))
    observations = [
        {"at": 1.0, "values": values(10), "diagnostics": diagnostics(sequence, 0)},
        {"at": 2.0, "values": values(20), "diagnostics": diagnostics(sequence, 1)},
    ]

    result = benchmark.score_observations(
        sequence, observations, deadline_timing_valid=False
    )

    assert result["scored_event_count"] == 0
    assert not result["deadline_timing_comparable"]
    assert all("playback rate" in item["excluded_reason"] for item in result["events"])


def test_reviewed_annotation_requires_pixel_and_dimension_binding():
    frame = Frame(np.zeros((20, 20, 3), dtype=np.uint8), captured_at=1)
    row = truth(10)
    row["expected_pixel_sha256"] = "0" * 64
    row["expected_dimensions"] = [20, 20]

    with pytest.raises(ValueError, match="does not match"):
        recorder_tool._verify_frame_binding(row, frame)


def test_reviewed_freshness_requires_independent_evidence(tmp_path):
    invalid = truth(10)
    invalid["capture_freshness"]["evidence"] = None

    with pytest.raises(ResourceSequenceError, match="evidence must be non-empty"):
        with ResourceSequenceRecorder(
            tmp_path / "no-evidence",
            session_id="no-evidence",
            origin="derived_corpus",
            provenance=provenance(),
            source_lineage={"source": "test"},
        ) as recorder:
            recorder.record(Frame(np.zeros((20, 20, 3), dtype=np.uint8)), **invalid)


def test_markdown_separates_evidence_by_origin():
    origins = {
        origin: {
            "session_count": 0,
            "passed_session_count": 0,
            "macro_deadline_success_rate": None,
            "macro_visible_readable_rate": None,
            "macro_correct_fresh_availability_rate": None,
        }
        for origin in ("native_capture", "imported_native", "derived_corpus")
    }
    report = {
        "summary": {
            "passed": False,
            "by_session_macro_deadline_success_rate": None,
            "by_session_macro_visible_readable_rate": None,
            "by_session_macro_correct_fresh_availability_rate": None,
            "by_origin": origins,
        },
        "sessions": [],
    }

    markdown = benchmark.render_markdown(report)

    assert "## Evidence by origin" in markdown
    assert "`native_capture`" in markdown
    assert "`derived_corpus`" in markdown


def test_bounded_worker_drains_large_payload_before_joining_child():
    context = multiprocessing.get_context("spawn")
    queue = context.Queue(maxsize=1)
    process = context.Process(target=_emit_large_worker_payload, args=(queue,))
    process.start()

    message = benchmark._receive_worker_message(process, queue, perf_counter() + 5)
    process.join(2)
    benchmark._close_result_queue(queue)

    assert message is not None
    assert len(message["ok"]["blob"]) == 2 * 1024 * 1024
    assert process.exitcode == 0


def test_report_rejects_mixed_worker_run_identity(monkeypatch, tmp_path):
    expected_environment = {"python": "test"}
    expected_implementation = {"code": "stable"}
    monkeypatch.setattr(benchmark, "environment", lambda: expected_environment)
    monkeypatch.setattr(
        benchmark, "_implementation_fingerprints", lambda: expected_implementation
    )
    monkeypatch.setattr(
        benchmark,
        "benchmark_session_bounded",
        lambda *_args, **_kwargs: {
            "error": None,
            "run_environment": {"python": "different"},
            "run_implementation": expected_implementation,
            "run_options": _kwargs,
        },
    )

    with pytest.raises(RuntimeError, match="mixed run identity"):
        benchmark.build_report(
            [tmp_path / "session"],
            poll_interval=0.01,
            playback_rate=1.0,
            max_gap_ms=1000.0,
            wall_timeout=5.0,
        )


def test_session_result_binds_manifest_provenance_and_implementation(tmp_path, monkeypatch):
    path = make_sequence(tmp_path)
    sequence = load_resource_sequence(path)
    observations = [
        {"at": 1.0, "values": values(10), "diagnostics": diagnostics(sequence, 0)},
        {"at": 2.0, "values": values(20), "diagnostics": diagnostics(sequence, 1)},
    ]
    monkeypatch.setattr(benchmark, "replay_tracker", lambda *_args, **_kwargs: observations)

    result = benchmark.benchmark_session(
        path,
        poll_interval=0.01,
        playback_rate=1,
        max_gap_ms=2000,
        wall_timeout=5,
    )

    assert result["session_manifest_sha256"]
    assert result["provenance"]["capture_provider"] == "test"
    assert result["source_lineage"]["native_evidence"] is False
    assert result["run_fingerprint_sha256"]
    fingerprints = benchmark._implementation_fingerprints()
    assert "tibia_client/resource_sequences.py" in fingerprints["sequence_code_files"]
    assert "tools/benchmark_resource_sequences.py" in fingerprints["sequence_code_files"]


def test_recorder_enforces_cumulative_decoded_pixel_budget(tmp_path, monkeypatch):
    import tibia_client.resource_sequences as sequences

    monkeypatch.setattr(sequences, "_MAX_SESSION_PIXEL_BYTES", 12)
    with pytest.raises(ResourceSequenceError, match="pixel budget"):
        with ResourceSequenceRecorder(
            tmp_path / "too-large",
            session_id="too-large",
            origin="native_capture",
            provenance=provenance(),
        ) as recorder:
            recorder.record(Frame(np.zeros((2, 2, 3), dtype=np.uint8), captured_at=1))
            recorder.record(Frame(np.zeros((2, 2, 3), dtype=np.uint8), captured_at=2))


def test_native_import_rejects_encoded_file_before_decode(tmp_path, monkeypatch):
    source = tmp_path / "large.png"
    source.write_bytes(b"x" * 11)
    manifest = tmp_path / "stream.jsonl"
    manifest.write_text(json.dumps({
        "path": source.name,
        "captured_at": 1,
        "source_sha256": sha256(source.read_bytes()).hexdigest(),
    }) + "\n")
    monkeypatch.setattr(recorder_tool, "_MAX_FRAME_BYTES", 10)
    args = SimpleNamespace(
        input=manifest,
        output=tmp_path / "output",
        session_id="bounded-import",
        source_assertion="unit test",
        client_build=None,
        client_family=None,
        operating_system=None,
        ui_scale=None,
        display_scale=None,
        coordinate_space="source pixels",
    )

    with pytest.raises(ValueError, match="bounded regular file"):
        recorder_tool.import_native(args)
