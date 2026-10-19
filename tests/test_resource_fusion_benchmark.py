"""Integrity gates for fusion evidence, using small fake originals and no OCR."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from core.perception import Frame
from tibia_client.adaptive import FIELDS
from tools import benchmark_resource_fusion as benchmark
from tools.compare_resource_readers import BenchmarkCase, TechniqueObservation, score_case


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    """81 logical originals preserve the real coverage gate without opening assets."""
    frame = Frame(np.zeros((20, 20, 3), dtype=np.uint8), captured_at=1, source="fixture")
    labels = {name: {"value": 10 + index, "visible": True, "verification": "manual_verified",
                     "box": [10, 1 + index * 4, 16, 4 + index * 4]}
              for index, name in enumerate(FIELDS)}
    cases, rows = [], []
    membership = {}
    for index in range(81):
        identifier = f"original-{index:03d}"
        digest = sha256(identifier.encode()).hexdigest()
        case = BenchmarkCase(
            id=identifier, source_id=identifier, source_sha256=digest, case_sha256=digest,
            category="original_full", split="train", classification="confirmed", frame=frame,
            labels=labels, source_coverage="full_screenshot", data_origin="real",
            asset_fidelity="source_original", render_equivalence_status="target_family",
            render_equivalence_evidence="fixture-reviewed interface",
        )
        membership[identifier] = "development" if index < 64 else "test" if index < 80 else "reference"
        row = score_case(case, TechniqueObservation(
            "found", {name: label["value"] for name, label in labels.items()},
            {name: label["box"] for name, label in labels.items()}))
        row.update(evaluation_split=membership[identifier], first_frame_fusion={},
                   initialization_ms=1.0, discovery_ms=1.0, first_read_ms=1.0,
                   new_frame_timings=benchmark._timings([1.0] * 50),
                   background_timings=benchmark._timings([2.0] * 20),
                   repeated_outputs_consistent=True, background_outputs_consistent=True,
                   changed_pixel=[0, 19])
        cases.append(case)
        rows.append(row)
    current_hashes = {"tools/resource_fusion.py": "reader-digest", "original-assets": "input-digest"}
    state = SimpleNamespace(cases=cases, rows=rows, current_hashes=current_hashes,
                            root=tmp_path, membership=membership)
    manifest = {"screenshots": [{"id": case.id, "integrity": {"sha256": case.source_sha256}}
                                for case in cases]}

    def load_cases(path, *, include_generated):
        assert include_generated is False
        return manifest, state.cases

    monkeypatch.setattr(benchmark, "load_cases", load_cases)
    monkeypatch.setattr(benchmark, "load_split", lambda *args: ({}, membership))
    monkeypatch.setattr(benchmark, "fingerprints", lambda: deepcopy(state.current_hashes))
    state.documents = [dict(
        schema=benchmark.SCHEMA, version=1, valid=True, revision="a" * 40,
        fingerprints=deepcopy(current_hashes), environment={"system": "Windows", "python": "3.11"},
        shard=shard, shards=3, warm_reads=50, budget_ms=100, opencv_threads=1,
        requested_case_ids=[case.id for case in cases], reader_receives_annotations=False,
        techniques={variant: {"cases": deepcopy(rows[shard::3])} for variant in benchmark.VARIANTS},
    ) for shard in range(3)]
    state.guard_path = tmp_path / "guards.xml"
    state.guard_root = ET.Element("testsuites")
    suite = ET.SubElement(state.guard_root, "testsuite", tests="25", failures="0", errors="0", skipped="0")
    for module in benchmark.GUARD_MODULES:
        for index in range(5):
            ET.SubElement(suite, "testcase", classname="tests." + Path(module).stem,
                          name=f"test_diagnostic_{index}")
    state.guard_metadata = dict(
        schema=benchmark.SCHEMA + "-guards", revision="a" * 40,
        fingerprints=deepcopy(current_hashes), modules=list(benchmark.GUARD_MODULES),
        pytest_exit_code=0, xml_sha256="",
    )

    def write_guard(*, rehash=True):
        payload = ET.tostring(state.guard_root, encoding="utf-8", xml_declaration=True)
        state.guard_path.write_bytes(payload)
        if rehash:
            state.guard_metadata["xml_sha256"] = sha256(payload).hexdigest()

    def summarize():
        paths = []
        for index, document in enumerate(state.documents):
            path = tmp_path / f"shard-{index}.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            paths.append(path)
        state.guard_path.with_suffix(".json").write_text(json.dumps(state.guard_metadata), encoding="utf-8")
        return benchmark.summarize_shards(paths, state.guard_path)

    state.write_guard = write_guard
    state.summarize = summarize
    write_guard()
    return state


def target_row(evidence, variant="combined"):
    return evidence.documents[0]["techniques"][variant]["cases"][0]


def test_complete_81_original_four_variant_run_passes_without_real_discovery(evidence):
    result = evidence.summarize()
    assert result["valid"] is True and result["native_sequence_count"] == 0
    assert result["focused_guard_tests"] == 25
    assert set(result["variants"]) == set(benchmark.VARIANTS)
    for card in result["variants"].values():
        assert card["metrics"]["cases"] == 81
        assert card["metrics"]["numeric_exact"] == 324
        assert card["worst_image_reading_p95_ms"] == 1
        assert card["worst_image_background_p95_ms"] == 2
        assert all(card["gates"].values())


@pytest.mark.parametrize("problem", [
    "missing_shard", "duplicate_shard", "invalid_shard_index", "mixed_revision",
    "mixed_environment", "mixed_fingerprints", "changed_current_fingerprints",
    "missing_variant", "extra_variant", "missing_case", "duplicate_case", "wrong_case_order",
    "subset_ids", "reordered_ids", "wrong_split", "wrong_schema", "invalid_document",
])
def test_incomplete_mixed_or_wrong_coverage_reports_are_rejected(evidence, problem):
    document = evidence.documents[1]
    if problem == "missing_shard":
        evidence.documents.pop()
    elif problem == "duplicate_shard":
        document["shard"] = 0
    elif problem == "invalid_shard_index":
        document["shard"] = 3
    elif problem.startswith("mixed_"):
        key = problem.removeprefix("mixed_")
        document[key] = {"changed": "value"} if key != "revision" else "b" * 40
    elif problem == "changed_current_fingerprints":
        evidence.current_hashes["tools/resource_fusion.py"] = "code-changed-after-run"
    elif problem == "missing_variant":
        del document["techniques"]["bar"]
    elif problem == "extra_variant":
        document["techniques"]["unrequested"] = deepcopy(document["techniques"]["bar"])
    elif problem in {"missing_case", "duplicate_case", "wrong_case_order"}:
        rows = document["techniques"]["bar"]["cases"]
        if problem == "missing_case":
            rows.pop()
        elif problem == "duplicate_case":
            rows[1] = deepcopy(rows[0])
        else:
            rows.reverse()
    elif problem in {"subset_ids", "reordered_ids"}:
        for item in evidence.documents:
            if problem == "subset_ids":
                item["requested_case_ids"].pop()
            else:
                item["requested_case_ids"].reverse()
    elif problem == "wrong_split":
        target_row(evidence)["evaluation_split"] = "test"
    elif problem == "wrong_schema":
        document["schema"] = "other-experiment"
    elif problem == "invalid_document":
        document["valid"] = False
    with pytest.raises(ValueError):
        evidence.summarize()


@pytest.mark.parametrize("field,bad_value", [
    ("source_id", "different-original"), ("source_sha256", "changed-source-pixels"),
    ("case_sha256", "changed-evaluated-pixels"), ("transform", "generated-resize"),
    ("category", "generated_perturbation"), ("data_origin", "reconstructed"),
    ("source_coverage", "supplemental_crop"),
])
def test_row_identity_and_pixels_must_match_the_loaded_unchanged_original(evidence, field, bad_value):
    target_row(evidence)[field] = bad_value
    with pytest.raises(ValueError):
        evidence.summarize()


@pytest.mark.parametrize("field,bad_value", [
    ("reader_receives_annotations", True), ("opencv_threads", 2), ("version", 999),
    ("warm_reads", 49), ("budget_ms", 200),
])
def test_out_of_scope_evaluation_settings_cannot_pass(evidence, field, bad_value):
    for document in evidence.documents:
        document[field] = bad_value
    with pytest.raises(ValueError):
        evidence.summarize()


def test_an_unexpected_corpus_size_cannot_be_labeled_complete(evidence):
    evidence.cases.pop()
    with pytest.raises(ValueError, match="corpus size"):
        evidence.summarize()


@pytest.mark.parametrize("field,bad_value", [
    ("schema", "other-guards"), ("revision", "b" * 40),
    ("fingerprints", {"different.py": "digest"}), ("modules", []),
    ("pytest_exit_code", 1), ("xml_sha256", "tampered-xml"),
])
def test_guard_metadata_must_match_code_revision_module_set_and_xml(evidence, field, bad_value):
    evidence.guard_metadata[field] = bad_value
    with pytest.raises(ValueError, match="guard provenance"):
        evidence.summarize()


def test_xml_tampering_is_rejected_even_when_test_counts_still_pass(evidence):
    list(evidence.guard_root.iter("testcase"))[0].set("name", "modified-after-guard-run")
    evidence.write_guard(rehash=False)
    with pytest.raises(ValueError, match="guard provenance"):
        evidence.summarize()


@pytest.mark.parametrize("node", ["failure", "error", "skipped"])
def test_failed_errored_or_skipped_guard_cannot_be_hidden_by_accuracy(evidence, node):
    ET.SubElement(list(evidence.guard_root.iter("testcase"))[0], node).text = "diagnostic did not pass"
    evidence.write_guard()
    with pytest.raises(ValueError, match="focused guards"):
        evidence.summarize()


@pytest.mark.parametrize("counter", ["failures", "errors", "skipped"])
def test_nonzero_junit_suite_outcomes_cannot_claim_a_clean_guard_run(evidence, counter):
    list(evidence.guard_root.iter("testsuite"))[0].set(counter, "1")
    evidence.write_guard()
    with pytest.raises(ValueError):
        evidence.summarize()


@pytest.mark.parametrize("replacement", ["tests.unrelated_module", "tests.test_resource_fusion_benchmark_fake"])
def test_each_required_guard_module_must_actually_appear_in_junit(evidence, replacement):
    module = Path(benchmark.GUARD_MODULES[-1]).stem
    for test in evidence.guard_root.iter("testcase"):
        if module in test.get("classname", "").split("."):
            test.set("classname", replacement)
    evidence.write_guard()
    with pytest.raises(ValueError, match="module did not run"):
        evidence.summarize()


def test_too_few_focused_guards_cannot_pass_by_listing_module_names(evidence):
    suite = list(evidence.guard_root.iter("testsuite"))[0]
    suite.remove(list(suite)[0])
    suite.set("tests", "24")
    evidence.write_guard()
    with pytest.raises(ValueError, match="focused guards"):
        evidence.summarize()


@pytest.mark.parametrize("mode", ["new_frame_timings", "background_timings"])
@pytest.mark.parametrize("bad_sample", [-0.01, float("nan"), float("inf"), True, "1.0"])
def test_all_latency_samples_must_be_finite_nonnegative_numbers(evidence, mode, bad_sample):
    target_row(evidence)[mode]["samples_ms"][0] = bad_sample
    with pytest.raises(ValueError, match="finite and nonnegative"):
        evidence.summarize()


@pytest.mark.parametrize("mode", ["new_frame_timings", "background_timings"])
def test_missing_replay_samples_are_rejected_for_each_timing_mode(evidence, mode):
    target_row(evidence)[mode]["samples_ms"].pop()
    with pytest.raises(ValueError, match="Missing latency samples"):
        evidence.summarize()


def test_forged_percentile_cannot_hide_slow_raw_samples(evidence):
    timing = target_row(evidence)["new_frame_timings"]
    timing["samples_ms"] = [101.0] * 50
    with pytest.raises(ValueError, match="Wrong stored p95"):
        evidence.summarize()


@pytest.mark.parametrize("mode,count", [("new_frame_timings", 50), ("background_timings", 20)])
def test_one_slow_image_fails_budget_even_when_pooled_percentile_is_fast(evidence, mode, count):
    target_row(evidence)[mode] = benchmark._timings([101.0] * count)
    card = evidence.summarize()["variants"]["combined"]
    assert card["pooled_reading_p95_ms"] == 1
    assert card["gates"]["per_image_p95_within_100ms"] is False
    assert card["budget_breaches"] == [{"case_id": "original-000", "mode": mode, "p95_ms": 101.0}]


def test_an_abstention_regression_remains_visible_in_summary(evidence):
    field = target_row(evidence)["fields"][FIELDS[0]]
    field.update(actual=None, quality="unknown", accepted=False, exact=False, unknown=True,
                 detail="temporal confirmation pending")
    card = evidence.summarize()["variants"]["combined"]
    assert card["metrics"]["numeric_exact"] == 323
    assert card["metrics"]["unknown"] == 1
    assert card["gates"]["preserves_baseline_correct_fields"] is False
    assert card["gates"]["no_incorrect_or_unsafe_accepts"] is True


def test_a_wrong_accepted_number_fails_safety_even_with_all_fast_timings(evidence):
    field = target_row(evidence)["fields"][FIELDS[0]]
    field.update(actual=999, exact=False, incorrect_accepted=True, numeric_mismatch_accepted=True)
    card = evidence.summarize()["variants"]["combined"]
    assert card["metrics"]["incorrect_accepted"] == 1
    assert card["gates"]["no_incorrect_or_unsafe_accepts"] is False
    assert card["gates"]["per_image_p95_within_100ms"] is True


@pytest.mark.parametrize("key", ["repeated_outputs_consistent", "background_outputs_consistent"])
def test_replay_instability_is_reported_separately_from_first_frame_accuracy(evidence, key):
    target_row(evidence)[key] = False
    card = evidence.summarize()["variants"]["combined"]
    assert card["metrics"]["numeric_exact"] == 324
    assert card["gates"]["repeat_outputs_consistent"] is False


def test_reference_diagnostic_panel_flag_is_not_a_safety_control_accept(evidence):
    evidence.cases[0] = replace(evidence.cases[0], render_equivalence_status="visibly_different",
                                expected_discovery=False)
    for variant in benchmark.VARIANTS:
        row = target_row(evidence, variant)
        row.update(acceptance_role="reference_diagnostic", false_panel_acceptance=True)
    card = evidence.summarize()["variants"]["combined"]
    assert card["metrics"]["false_panel_acceptances"] == 1  # legacy aggregate retained
    assert card["control_scope"]["reference_diagnostic_panel_flags"] == ["original-000"]
    assert card["control_scope"]["false_panels"] == 0
    assert card["gates"]["no_incorrect_or_unsafe_accepts"] is True


def test_cannot_reclassify_a_control_by_editing_report_role(evidence):
    target_row(evidence).update(acceptance_role="reference_diagnostic")
    with pytest.raises(ValueError, match="acceptance role"):
        evidence.summarize()


def test_true_safety_control_panel_and_values_still_fail_gate(evidence):
    evidence.cases[0] = replace(evidence.cases[0], classification="negative",
                                expected_discovery=False)
    for variant in benchmark.VARIANTS:
        target_row(evidence, variant).update(acceptance_role="negative_safety_control",
                                            false_panel_acceptance=True)
    card = evidence.summarize()["variants"]["combined"]
    assert card["control_scope"]["images"] == 1
    assert card["control_scope"]["false_panels"] == 1
    assert card["control_scope"]["accepted_values"] == 4
    assert card["gates"]["no_incorrect_or_unsafe_accepts"] is False


def test_analysis_only_change_is_bound_to_original_git_blob(evidence, monkeypatch):
    name = "tools/benchmark_resource_fusion.py"
    old = b"recorded analysis\n"
    old_hash = sha256(old).hexdigest()
    for document in evidence.documents:
        document["fingerprints"][name] = old_hash
    evidence.guard_metadata["fingerprints"][name] = old_hash
    evidence.current_hashes[name] = sha256(b"corrected analysis\n").hexdigest()
    monkeypatch.setattr(benchmark.subprocess, "check_output", lambda *a, **k: old)
    result = evidence.summarize()
    assert result["analysis_only_changes"][name]["evaluated_sha256"] == old_hash
    assert result["fingerprints"][name] == old_hash
    monkeypatch.setattr(benchmark.subprocess, "check_output", lambda *a, **k: b"wrong blob")
    with pytest.raises(ValueError, match="evaluated Git revision"):
        evidence.summarize()
