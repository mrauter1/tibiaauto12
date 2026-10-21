"""Integrity checks using tiny logical originals; no discovery or font backend."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from core.perception import Frame
from tibia_client.adaptive import FIELDS
from tibia_client.models import Quality, Reading
from tools import benchmark_resource_reader as benchmark
from tools.check_resource_reader_guards import GUARD_MODULES, MIN_GUARD_TESTS
from tools.compare_resource_readers import BenchmarkCase, TechniqueObservation, score_case


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    frame = Frame(np.zeros((20, 20, 3), np.uint8), captured_at=1, source="fixture")
    cases, rows, membership, eligible_seen = [], [], {}, 0
    for index in range(81):
        control = index >= 64
        labels = {name: {"value": None if index < 17 and field == 3 else 10 + field,
                         "visible": True, "verification": "manual_verified",
                         "box": [3, 1 + field * 4, 9, 4 + field * 4]}
                  for field, name in enumerate(FIELDS)}
        identifier = f"original-{index:03}"
        case = BenchmarkCase(
            id=identifier, source_id=identifier, source_sha256=sha256(identifier.encode()).hexdigest(),
            case_sha256=sha256(frame._rgb).hexdigest(), category="original_full", split="train",
            classification="negative" if control else "confirmed", expected_discovery=not control,
            frame=frame, labels=labels, source_coverage="full_screenshot", data_origin="real",
            asset_fidelity="source_original", render_equivalence_status="target_family",
            render_equivalence_evidence="fixture reviewed interface")
        values = {}
        if not control:
            for name, label in labels.items():
                value = label["value"]
                values[name] = value if eligible_seen >= 18 else None
                eligible_seen += value is not None
        observation = TechniqueObservation(
            "missing" if control else "found", values,
            {} if control else {name: label["box"] for name, label in labels.items()},
            discovery_ms=1.0, cold_total_ms=1.0 if control else 2.0)
        row = score_case(case, observation, supports_cached_layout=True)
        membership[identifier] = "development" if index < 57 else "test" if index < 71 else "reference"
        row.update(evaluation_split=membership[identifier], initialization_ms=1.0,
                   first_read_ms=None if control else 1.0,
                   new_frame_timings=benchmark._timings([] if control else [1.0] * 50),
                   background_timings=benchmark._timings([] if control else [2.0] * 20),
                   repeated_outputs_consistent=True, background_outputs_consistent=True,
                   changed_pixel=None if control else [0, 19], evidence_boxes=[] if control else [[1, 0, 18, 19]])
        cases.append(case)
        rows.append(row)
    hashes = {"tools/resource_reader.py": "reader", "originals": "pixels"}
    state = SimpleNamespace(cases=cases, rows=rows, hashes=hashes, root=tmp_path, membership=membership)
    monkeypatch.setattr(benchmark, "fingerprints", lambda: deepcopy(hashes))
    monkeypatch.setattr(benchmark, "verify_revision", lambda *args: None)
    monkeypatch.setattr(benchmark, "load_cases", lambda *a, **k: ({"screenshots": []}, cases))
    monkeypatch.setattr(benchmark, "load_split", lambda *a: ({}, membership))
    state.documents = [dict(schema=benchmark.SCHEMA, version=1, valid=True, reader_id="current",
        revision="a" * 40, fingerprints=deepcopy(hashes), environment={"system": "Windows"},
        shard=index, shards=12, warm_reads=50, background_reads=20, budget_ms=100, opencv_threads=1,
        requested_case_ids=[case.id for case in cases], reader_receives_annotations=False,
        cases=rows[index::12]) for index in range(12)]
    state.guard_root = ET.Element("testsuites")
    suite = ET.SubElement(state.guard_root, "testsuite", tests=str(MIN_GUARD_TESTS),
                          failures="0", errors="0", skipped="0")
    nodeids = []
    for index in range(MIN_GUARD_TESTS):
        module = GUARD_MODULES[index % len(GUARD_MODULES)]
        name = f"test_guard_{index}"
        nodeids.append(module + "::" + name)
        ET.SubElement(suite, "testcase", classname=module[:-3].replace("/", "."), name=name)
    state.guard = dict(schema="tibiaauto.resource-reader-guards", version=1, reader_id="current",
        valid=True, revision="a" * 40, revision_after="a" * 40, fingerprints=deepcopy(hashes),
        fingerprints_after=deepcopy(hashes), modules=list(GUARD_MODULES), pytest_exit_code=0,
        runner_exit_code=0, selected_nodeids=nodeids,
        selected_nodeids_sha256=sha256(json.dumps(nodeids, separators=(",", ":")).encode()).hexdigest(),
        counts={"tests": MIN_GUARD_TESTS, "failures": 0, "errors": 0, "skipped": 0})

    def write_guard():
        data = ET.tostring(state.guard_root)
        (tmp_path / "guards.xml").write_bytes(data)
        state.guard["xml_sha256"] = sha256(data).hexdigest()

    def summarize():
        paths = []
        for index, document in enumerate(state.documents):
            path = tmp_path / f"shard-{index}.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            paths.append(path)
        (tmp_path / "guard-metadata.json").write_text(json.dumps(state.guard), encoding="utf-8")
        return benchmark.summarize_shards(paths, tmp_path)

    state.write_guard, state.summarize = write_guard, summarize
    write_guard()
    return state


def rescore(evidence, index=6, *, actual=999, region=None, control=False):
    case, old = evidence.cases[index], evidence.rows[index]
    values = {name: field["actual"] for name, field in old["fields"].items()}
    values[FIELDS[0]] = actual
    regions = {name: label["box"] for name, label in case.labels.items()}
    if region is not None:
        regions[FIELDS[0]] = region
    old.update(score_case(case, TechniqueObservation("found", values, regions,
                         discovery_ms=1.0, cold_total_ms=2.0), supports_cached_layout=True))
    if control:
        old.update(first_read_ms=1.0, new_frame_timings=benchmark._timings([1.0] * 50),
                   background_timings=benchmark._timings([2.0] * 20), changed_pixel=[0, 19],
                   evidence_boxes=[[1, 0, 18, 19]])


def test_complete_artifact_recomputes_221_exact_18_unknown_and_17_safe_controls(evidence):
    report = evidence.summarize()
    assert report["valid"] is report["passed"] is True
    assert report["metrics"]["eligible_fields"] == 239
    assert (report["metrics"]["numeric_exact"], report["metrics"]["unknown"]) == (221, 18)
    assert report["controls"] == {"images": 17, "accepted_values": 0, "false_panels": 0}
    assert report["native_sequence_count"] == 0
    assert "cases" not in report and "samples_ms" not in json.dumps(report)


def test_guard_parameter_id_with_double_colon_matches_junit_exactly(evidence):
    module = "tests/test_adaptive_resources.py"
    name = ("test_resampled_narrow_digit_leads_remain_ambiguous["
            "portal-tibia-2016-02-VtukRkf::panel-scale-0.9-"
            "fb57041c9a99370c30c34ad26f8119854d7c75c9d0489ee2956717433ca79f4f-"
            "region0-0.7136363636363636-304]")
    evidence.guard["selected_nodeids"][0] = module + "::" + name
    evidence.guard["selected_nodeids_sha256"] = sha256(json.dumps(
        evidence.guard["selected_nodeids"], separators=(",", ":")).encode()).hexdigest()
    testcase = next(evidence.guard_root.iter("testcase"))
    testcase.set("classname", module[:-3].replace("/", "."))
    testcase.set("name", name)
    evidence.write_guard()
    assert evidence.summarize()["passed"] is True

    # Matching counts and an updated XML hash cannot hide a substituted parameter.
    testcase.set("name", name.replace("::panel-scale-0.9", "::panel-scale-1.3"))
    evidence.write_guard()
    with pytest.raises(ValueError, match="identities"):
        evidence.summarize()


@pytest.mark.parametrize("problem", [
    "partial", "duplicate", "subset", "mixed_revision", "mixed_environment", "changed_code",
    "source_hash", "wrong_score", "wrong_split", "short_repeats", "nonfinite", "fake_percentile",
    "guard_fingerprint", "guard_xml", "guard_short", "guard_skip", "extra_field",
])
def test_incomplete_mixed_forged_or_failed_artifacts_are_rejected(evidence, problem):
    row = evidence.rows[6]
    if problem == "partial":
        evidence.documents.pop()
    elif problem == "duplicate":
        evidence.documents[-1]["shard"] = 0
    elif problem == "subset":
        evidence.documents[0]["requested_case_ids"].pop()
    elif problem.startswith("mixed_"):
        evidence.documents[1][problem[6:]] = "different"
    elif problem == "changed_code":
        evidence.hashes["tools/resource_reader.py"] = "changed"
    elif problem == "source_hash":
        row["source_sha256"] = "forged"
    elif problem == "wrong_score":
        row["fields"][FIELDS[0]]["exact"] = False
    elif problem == "wrong_split":
        row["evaluation_split"] = "test"
    elif problem == "short_repeats":
        row["new_frame_timings"]["samples_ms"].pop()
    elif problem == "nonfinite":
        row["background_timings"]["samples_ms"][0] = float("nan")
    elif problem == "fake_percentile":
        row["new_frame_timings"]["samples_ms"] = [101.0] * 50
    elif problem == "guard_fingerprint":
        evidence.guard["fingerprints_after"] = {}
    elif problem == "guard_xml":
        (evidence.root / "guards.xml").write_text("<testsuites/>")
    elif problem == "guard_short":
        suite = next(evidence.guard_root.iter("testsuite"))
        suite.remove(list(suite)[0])
        evidence.write_guard()
    elif problem == "guard_skip":
        ET.SubElement(next(evidence.guard_root.iter("testcase")), "skipped")
        evidence.write_guard()
    elif problem == "extra_field":
        row["fields"]["other"] = deepcopy(row["fields"][FIELDS[0]])
    with pytest.raises(ValueError):
        evidence.summarize()


@pytest.mark.parametrize("problem", ["wrong_value", "wrong_row", "new_unknown", "control_accept", "slow_image", "unstable"])
def test_real_regressions_fail_even_if_artifact_injects_passing_gates(evidence, problem):
    row = evidence.rows[6]
    if problem == "wrong_value":
        rescore(evidence)
    elif problem == "wrong_row":
        rescore(evidence, actual=10, region=[3, 5, 9, 8])
    elif problem == "new_unknown":
        rescore(evidence, actual=None)
    elif problem == "control_accept":
        rescore(evidence, index=64, control=True)
    elif problem == "slow_image":
        row["new_frame_timings"] = benchmark._timings([101.0] * 50)
    else:
        row["background_outputs_consistent"] = False
    for document in evidence.documents:
        document.update(passed=True, gates={"everything": True})
    report = evidence.summarize()
    assert report["valid"] is True and report["passed"] is False
    if problem == "slow_image":
        assert report["latency"]["new_frame_timings"]["pooled_p95_ms"] == 1
        assert report["budget_breaches"][0]["p95_ms"] == 101


def test_revision_binding_permits_a_later_reports_commit_but_not_changed_inputs(monkeypatch):
    hashes = {"tools/resource_reader.py": "hash"}
    monkeypatch.setattr(benchmark.subprocess, "check_output", lambda *a, **k: b"tools/resource_reader.py\n")
    monkeypatch.setattr(benchmark.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    benchmark.verify_revision("a" * 40, hashes)
    monkeypatch.setattr(benchmark.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(ValueError, match="Git revision"):
        benchmark.verify_revision("a" * 40, hashes)


def test_probe_scores_only_first_original_and_uses_fresh_frames_for_all_repeats(evidence, monkeypatch):
    case = evidence.cases[6]
    frames = []
    regions = {name: label["box"] for name, label in case.labels.items()}
    layout = SimpleNamespace(bounds=(1, 0, 18, 19), regions=regions, anchors=())

    class Reader:
        def discover(self, frame, **kwargs):
            return SimpleNamespace(layout=layout, detail="", candidates=())

        def geometry_evidence_boxes(self, *args):
            return [layout.bounds]

        def read(self, frame, *args, **kwargs):
            frames.append(frame)
            return {name: Reading({"count": 10 + index + (len(frames) > 1)}, Quality.KNOWN,
                                 diagnostics={"effective_region": regions[name], "numeric_eligible": True,
                                              "layout_fingerprint": "verified"})
                    for index, name in enumerate(FIELDS)}

    monkeypatch.setattr("tibia_client.resource_reader.ResourceReader", Reader)
    row = benchmark.probe(case)
    assert row["fields"][FIELDS[0]]["actual"] == 10
    assert row["repeated_outputs_consistent"] is False
    assert len(frames) == len({id(frame) for frame in frames}) == 71
    assert frames[0] is case.frame
    assert all(frame._rgb == case.frame._rgb for frame in frames[:51])
    assert all(frame._rgb != case.frame._rgb for frame in frames[51:])
