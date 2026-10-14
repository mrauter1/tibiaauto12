from time import perf_counter
from types import SimpleNamespace

import pytest

from tibia_client.adaptive import FIELDS
from tibia_client.models import Quality, Reading, Sensor, SensorKind
from tibia_client.visual import AdaptiveVisual
from tools import benchmark_resources as corpus
from tools import benchmark_resource_runtime as runtime_benchmark
from tools.benchmark_resource_runtime import (
    _incorrect_acceptances,
    _measure_hot_reads,
    _runtime_cases,
    _runtime_profile,
    _run_case_rows,
    _safety_passed,
    _wait_all_known,
)


def test_runtime_benchmark_compiles_public_adaptive_profile_metadata():
    profile = _runtime_profile()

    assert set(profile) == set(FIELDS)
    assert {
        field: (profile[field].adaptive.reader, profile[field].adaptive.field)
        for field in FIELDS
        if isinstance(profile[field].adaptive, AdaptiveVisual)
    } == {field: ("paulistinha-skills-v1", field) for field in FIELDS}


def test_runtime_benchmark_selects_reviewed_real_full_pairs_without_truth_crops():
    _document, entries = corpus.load_manifest(corpus.DEFAULT_MANIFEST)
    pairs = _runtime_cases(corpus.DEFAULT_MANIFEST)
    expected = {
        entry["id"]
        for entry in entries
        if entry["coverage"] == "full_screenshot"
        and entry["classification"] != "negative"
        and entry["expected_discovery"]
        and not entry.get("expected_final_unknown_fields")
        and entry["data_origin"] == "real"
        and entry["asset_fidelity"] in {"source_original", "source_transcode"}
        and entry["render_equivalence"]["status"]
        in {"target_family", "visually_equivalent"}
        and entry["render_equivalence"]["evidence"].strip()
    }

    assert {entry["id"] for entry, _case in pairs} == expected
    assert all(entry["coverage"] == "full_screenshot" for entry, _case in pairs)
    assert all(case.transform == "cached-relocation" for _entry, case in pairs)
    assert all(case.prior_frame is not None for _entry, case in pairs)


def test_runtime_case_selection_allows_future_positive_full_corpus_growth(
    monkeypatch, tmp_path
):
    entries = [
        {
            "id": identifier,
            "coverage": "full_screenshot",
            "classification": "similar",
            "expected_discovery": True,
            "data_origin": "real",
            "asset_fidelity": "source_original",
            "render_equivalence": {
                "status": "visually_equivalent",
                "evidence": "reviewed future fixture",
            },
        }
        for identifier in ("future-a", "future-b")
    ]
    cases = [
        SimpleNamespace(source_id=entry["id"], transform="cached-relocation")
        for entry in entries
    ]
    monkeypatch.setattr(corpus, "load_manifest", lambda _path: ({}, entries))
    monkeypatch.setattr(
        corpus, "generated_benchmark_cases", lambda _entries, _path: iter(cases)
    )

    pairs = _runtime_cases(tmp_path / "manifest.json")

    assert [entry["id"] for entry, _case in pairs] == ["future-a", "future-b"]


def test_historical_runtime_benchmark_requires_explicit_generated_opt_in(tmp_path):
    with pytest.raises(ValueError, match="--include-generated-diagnostics"):
        runtime_benchmark.build_report(tmp_path / "unused-manifest.json")


def test_transient_wrong_known_poll_fails_post_inference_safety_scoring():
    health = Sensor(SensorKind.VISUAL, (0, 0, 10, 10), "resource_health")
    mana = Sensor(SensorKind.VISUAL, (0, 0, 10, 10), "resource_mana")
    sensors = {"resource_health": health, "resource_mana": mana}
    metadata = AdaptiveVisual("paulistinha-skills-v1", "resource_health")
    requests = {health: metadata, mana: metadata}
    initial = {
        health: Reading({"count": 999}),
        mana: Reading(None, Quality.UNKNOWN, "pending"),
    }

    class Tracker:
        def read(self, _frame, _requests):
            return {
                health: Reading({"count": 10}),
                mana: Reading({"count": 20}),
            }

    final, _elapsed, polls = _wait_all_known(
        Tracker(),
        None,
        requests,
        sensors,
        initial=initial,
        started=perf_counter(),
        timeout=1.0,
        poll_interval=0.0001,
    )

    assert final[mana].quality is Quality.KNOWN
    assert _incorrect_acceptances(
        polls, {"resource_health": 10, "resource_mana": 20}
    ) == [
        {
            "poll": 0,
            "field": "resource_health",
            "expected": 10,
            "actual": 999,
        }
    ]
    assert not _safety_passed(
        [
            {
                "first_start_read_unknown": True,
                "first_invalidated_read_unknown": True,
                "any_incorrect_accepted_poll": True,
                "revision_changed": True,
                "exact_final_values": True,
            }
        ]
    )


def test_wrong_first_hot_read_cannot_be_hidden_by_exact_last_read():
    sensor = Sensor(SensorKind.VISUAL, (0, 0, 10, 10), "resource_health")
    sensors = {"resource_health": sensor}
    requests = {
        sensor: AdaptiveVisual("paulistinha-skills-v1", "resource_health")
    }

    class Tracker:
        def __init__(self):
            self.values = iter((999, 10))

        def read(self, _frame, _requests):
            return {
                sensor: Reading(
                    {"count": next(self.values)},
                    diagnostics={"layout_revision": "layout-a"},
                )
            }

    final, timings, observations = _measure_hot_reads(
        Tracker(), None, requests, sensors, 2
    )
    incorrect = _incorrect_acceptances(
        [observation["values"] for observation in observations],
        {"resource_health": 10},
    )

    assert len(timings) == 2
    assert observations[0]["revision"] == observations[1]["revision"] == "layout-a"
    assert final[sensor].value["count"] == 10
    assert incorrect == [
        {
            "poll": 0,
            "field": "resource_health",
            "expected": 10,
            "actual": 999,
        }
    ]
    assert not _safety_passed(
        [
            {
                "first_start_read_unknown": True,
                "first_invalidated_read_unknown": True,
                "any_incorrect_accepted_poll": bool(incorrect),
                "revision_changed": True,
                "exact_final_values": True,
            }
        ]
    )


def test_failed_case_is_reported_and_does_not_hide_following_case(monkeypatch):
    labels = {field: {"value": index} for index, field in enumerate(FIELDS)}
    pairs = [
        (
            {"labels": labels},
            SimpleNamespace(
                id=identifier,
                source_id=identifier,
                source_sha256="a" * 64,
                case_sha256="b" * 64,
                split="train",
            ),
        )
        for identifier in ("fails", "continues")
    ]
    seen = []

    def fake_benchmark(_entry, case, **_options):
        seen.append(case.id)
        if case.id == "fails":
            raise TimeoutError("unknown fields: resource_soul")
        return {
            "id": case.id,
            "source_id": case.source_id,
            "first_start_read_unknown": True,
            "first_invalidated_read_unknown": True,
            "any_incorrect_accepted_poll": False,
            "revision_changed": True,
            "exact_final_values": True,
        }

    monkeypatch.setattr(runtime_benchmark, "benchmark_case", fake_benchmark)
    rows = _run_case_rows(
        pairs, hot_reads=1, timeout=1.0, poll_interval=0.001
    )

    assert seen == ["fails", "continues"]
    assert len(rows) == 2
    assert rows[0]["error"] == {
        "type": "TimeoutError",
        "detail": "unknown fields: resource_soul",
    }
    assert rows[1]["error"] is None
    assert not _safety_passed(rows)
