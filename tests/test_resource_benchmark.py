from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from math import ceil, floor
from pathlib import Path
import shutil

import cv2
import numpy as np
import pytest
from PIL import Image

from core.perception import Frame
from tibia_client.adaptive import FIELDS
from tools import benchmark_resources as corpus
from tools import compare_resource_readers as comparison
from tools.compare_resource_readers import (
    BenchmarkCase,
    TechniqueObservation,
    _acceptance_role,
    _evidence_group,
    _techniques,
    _validate_lineage,
    load_cases,
    render_markdown,
    run_benchmark,
)


def _labels(values=(10, 20, 30, 40)):
    return {
        name: {
            "value": value,
            "visible": True,
            "box": [index * 10, 0, index * 10 + 5, 5],
            "verification": "manual_verified",
        }
        for index, (name, value) in enumerate(zip(FIELDS, values))
    }


def _case(identifier: str = "source-a") -> BenchmarkCase:
    frame = Frame(np.zeros((12, 50, 3), dtype=np.uint8))
    return BenchmarkCase(
        id=identifier,
        source_id=identifier,
        source_sha256="a" * 64,
        case_sha256="b" * 64,
        category="original_full",
        split="heldout",
        classification="confirmed",
        frame=frame,
        labels=_labels(),
        source_coverage="full_screenshot",
        data_origin="real",
        asset_fidelity="source_original",
        render_equivalence_status="target_family",
        render_equivalence_evidence="reviewed fixture evidence",
    )


class _Technique:
    def __init__(self, identifier, observation, *, supports_cached_layout=False):
        self.id = identifier
        self.options = {
            "fixture": True,
            "supports_cached_layout": supports_cached_layout,
        }
        self.supports_cached_layout = supports_cached_layout
        self.observation = observation
        self.seen = []

    def evaluate(self, case, *, warm_reads):
        self.seen.append((id(case.frame), warm_reads))
        return self.observation


def test_accounting_separates_wrong_acceptance_from_unknown():
    wrong = _Technique(
        "wrong",
        TechniqueObservation(
            "found",
            {
                "resource_health": 999,  # accepted, but false
                "resource_mana": 20,  # accepted and exact
                "resource_soul": None,  # abstention
                "resource_capacity": None,
            },
            {name: label["box"] for name, label in _labels().items()},
        ),
    )
    null = _Technique("null", TechniqueObservation("missing", {}))
    report = run_benchmark([_case()], [wrong, null], warm_reads=2)

    wrong_metrics = report["wrong"]["summary"]["aggregate"]
    assert wrong_metrics["eligible_fields"] == 4
    assert wrong_metrics["numeric_exact"] == 1
    assert wrong_metrics["incorrect_accepted"] == 1
    assert wrong_metrics["unknown"] == 2
    assert wrong_metrics["numeric_acceptance_coverage"] == pytest.approx(0.5)
    assert wrong_metrics["accepted_value_precision"] == pytest.approx(0.5)

    null_metrics = report["null"]["summary"]["aggregate"]
    assert null_metrics["numeric_exact"] == 0
    assert null_metrics["unknown"] == 4
    assert null_metrics["numeric_acceptance_coverage"] == 0
    assert null_metrics["accepted_value_precision"] is None


def test_field_rejection_details_and_bounded_raw_diagnostics_are_auditable():
    observation = TechniqueObservation(
        "found",
        {name: None for name in FIELDS},
        {"resource_health": _labels()["resource_health"]["box"]},
        field_details={"resource_health": "number contains an ambiguous digit"},
        field_diagnostics={
            "resource_health": {
                "numeric_eligible": True,
                "score": 0.73,
                "alternatives": list(range(30)),
            }
        },
    )

    result = run_benchmark([_case()], [_Technique("diagnostic", observation)])[
        "diagnostic"
    ]
    health = result["cases"][0]["fields"]["resource_health"]
    mana = result["cases"][0]["fields"]["resource_mana"]

    assert health["detail"] == "number contains an ambiguous digit"
    assert health["rejection_stage"] == "numeric_read"
    assert health["raw_reader_score"] == pytest.approx(0.73)
    assert health["calibrated_correctness_probability"] is None
    assert health["diagnostics"]["alternatives"][-1] == {"_truncated_items": 18}
    assert mana["rejection_stage"] == "value_region"
    assert result["summary"]["aggregate"]["rejection_stage_failures"] == {
        "numeric_read": 1,
        "value_region": 3,
    }


def test_numeric_metrics_are_conditioned_on_correct_location():
    values = dict(zip(FIELDS, (10, 999, None, 40)))
    observation = TechniqueObservation(
        "found",
        values,
        {name: label["box"] for name, label in _labels().items()},
    )

    metric = run_benchmark([_case()], [_Technique("conditional", observation)])[
        "conditional"
    ]["summary"]["aggregate"]

    assert metric["correct_location_coverage"] == 1.0
    assert metric["numeric_acceptance_given_correct_location"] == pytest.approx(0.75)
    assert metric["numeric_exact_given_correct_location"] == pytest.approx(0.5)
    assert metric["incorrect_accepted"] == 1


def test_macro_scorecards_weight_sources_not_synthetic_case_count():
    exact = TechniqueObservation(
        "found",
        dict(zip(FIELDS, (10, 20, 30, 40))),
        {name: label["box"] for name, label in _labels().items()},
    )
    missing = TechniqueObservation("missing", {})

    class _BySource(_Technique):
        def evaluate(self, case, *, warm_reads):
            return exact if case.frame.source == "source-c" else missing

    source_c = replace(
        _case("source-c::one"),
        source_id="source-c",
        category="lightweight_numeric_roi",
        classification="similar",
        source_coverage="full_screenshot",
        frame=Frame(np.zeros((12, 50, 3), dtype=np.uint8), source="source-c"),
    )
    source_d = replace(
        _case("source-d::one"),
        source_id="source-d",
        category="lightweight_numeric_roi",
        classification="similar",
        source_coverage="full_screenshot",
        frame=Frame(np.zeros((12, 50, 3), dtype=np.uint8), source="source-d"),
    )
    cases = [replace(source_c, id=f"source-c::{index}") for index in range(5)]
    cases.append(source_d)

    card = run_benchmark(cases, [_BySource("by-source", missing)])["by-source"][
        "summary"
    ]["evidence_scorecards"]["synthetic_cropped_stress"]

    assert card["source_count"] == 2
    assert card["pooled"]["exact_coverage"] == pytest.approx(5 / 6)
    assert card["macro_per_source"]["exact_coverage"] == pytest.approx(0.5)
    assert card["macro_sources_with_denominator"]["exact_coverage"] == 2
    assert card["macro_sources_with_denominator"]["accepted_value_precision"] == 1
    assert card["sources_with_safety_violation"] == 0


def test_negative_scorecard_has_no_numeric_denominator():
    negative = replace(
        _case("negative"),
        classification="negative",
        expected_discovery=False,
    )
    result = run_benchmark(
        [negative], [_Technique("negative", TechniqueObservation("found", {}))]
    )["negative"]["summary"]

    card = result["evidence_scorecards"]["negative_original_full_safety_control"]
    assert card["source_count"] == 1
    assert card["pooled"]["cases"] == 1
    assert card["pooled"]["eligible_fields"] == 0
    assert card["pooled"]["exact_coverage"] is None
    assert card["macro_per_source"]["exact_coverage"] is None
    assert card["macro_sources_with_denominator"]["exact_coverage"] == 0
    assert card["macro_per_source"]["false_panel_acceptance_rate"] == 1.0
    assert card["macro_sources_with_denominator"][
        "false_panel_acceptance_rate"
    ] == 1
    assert card["sources_with_safety_violation"] == 1


def test_evidence_scorecard_latency_reports_p50_p95_max_and_samples():
    class _ByLatency(_Technique):
        def evaluate(self, case, *, warm_reads):
            duration = float(case.frame.source)
            return TechniqueObservation(
                "missing",
                {},
                cold_total_ms=duration,
                discovery_ms=duration / 2,
            )

    cases = [
        replace(
            _case(f"source-{duration}::stress"),
            source_id=f"source-{duration}",
            category="generated_perturbation",
            classification="similar",
            source_coverage="full_screenshot",
            frame=Frame(
                np.zeros((12, 50, 3), dtype=np.uint8), source=str(duration)
            ),
        )
        for duration in (10, 20, 30)
    ]

    card = run_benchmark(
        cases, [_ByLatency("latency", TechniqueObservation("missing", {}))]
    )["latency"]["summary"]["evidence_scorecards"][
        "synthetic_full_frame_stress"
    ]["pooled"]

    assert card["cold_total_ms_samples"] == 3
    assert card["cold_total_ms_median"] == 20
    assert card["cold_total_ms_p95"] == pytest.approx(29)
    assert card["cold_total_ms_max"] == 30


def test_acceptance_excludes_crops_different_renders_and_generated_cases():
    accepted = _case("accepted")
    crop = replace(
        _case("crop"),
        category="supplemental_crop",
        source_coverage="supplemental_crop",
    )
    different = replace(
        _case("different"),
        render_equivalence_status="visibly_different",
        render_equivalence_evidence="reviewed visible differences",
    )
    generated = replace(
        _case("accepted::scaled"),
        source_id="accepted",
        category="generated_perturbation",
        transform="full-scale-1.5",
    )
    exact = TechniqueObservation(
        "found",
        dict(zip(FIELDS, (10, 20, 30, 40))),
        {name: label["box"] for name, label in _labels().items()},
    )

    result = run_benchmark(
        [accepted, crop, different, generated], [_Technique("scope", exact)]
    )["scope"]
    summary = result["summary"]

    assert summary["acceptance"]["cases"] == 1
    assert summary["acceptance"]["eligible_fields"] == 4
    assert summary["reference_diagnostics"]["cases"] == 2
    assert summary["generated_diagnostics"]["cases"] == 1
    assert [row["acceptance_role"] for row in result["cases"]] == [
        "positive_acceptance",
        "reference_diagnostic",
        "reference_diagnostic",
        "generated_diagnostic",
    ]


def test_equivalence_requires_reviewed_evidence_but_not_native_capture_claim():
    no_evidence = replace(
        _case(),
        render_equivalence_status="visually_equivalent",
        render_equivalence_evidence="",
    )
    transcoded = replace(
        _case("transcoded"),
        asset_fidelity="source_transcode",
        render_equivalence_status="visually_equivalent",
        render_equivalence_evidence="reviewed pixel-level appearance",
    )
    observation = TechniqueObservation("missing", {})

    result = run_benchmark(
        [no_evidence, transcoded], [_Technique("equivalence", observation)]
    )["equivalence"]["cases"]

    assert result[0]["acceptance_role"] == "reference_diagnostic"
    assert result[0]["acceptance_reason"] == "render equivalence has no reviewed evidence"
    assert result[1]["acceptance_role"] == "positive_acceptance"
    assert result[1]["asset_fidelity"] == "source_transcode"


def test_cli_defaults_to_originals_and_generated_diagnostics_are_opt_in(monkeypatch):
    seen = []

    def fake_build_report(*args, **kwargs):
        seen.append(kwargs["include_generated"])
        return {"safety_passed": True, "techniques": {}}

    monkeypatch.setattr(comparison, "build_report", fake_build_report)
    monkeypatch.setattr(comparison, "render_markdown", lambda report: "report\n")

    assert comparison.main([]) == 0
    assert comparison.main(["--include-generated-diagnostics"]) == 0
    assert seen == [False, True]


def test_ocr_ablation_ids_are_opt_in_and_keep_main_three():
    main = _techniques(("legacy", "adaptive"), include_ocr=True)
    assert [item.id for item in main] == [
        "legacy_exact_scale_pr4",
        "adaptive_templates",
        "adaptive_templates_ocr",
    ]

    with_ablations = _techniques(
        ("legacy", "adaptive"),
        include_ocr=True,
        ocr_ablations=("labels-only", "digits-only"),
    )
    assert [item.id for item in with_ablations[-2:]] == [
        "adaptive_labels_ocr",
        "adaptive_digits_ocr",
    ]
    assert with_ablations[-2].options["label_ocr"] is True
    assert with_ablations[-2].options["digit_ocr"] is False
    assert with_ablations[-1].options["label_ocr"] is False
    assert with_ablations[-1].options["digit_ocr"] is True


def test_lineage_rejects_derived_split_and_cross_split_source_aliases():
    source = _case("source")
    bad_derived = replace(
        source,
        id="source::scaled",
        source_id="source",
        transform="panel-scale-1.5",
        split="train",
    )
    with pytest.raises(ValueError, match="changed source split"):
        _validate_lineage([source, bad_derived])

    alias = replace(
        _case("source-alias"),
        source_sha256=source.source_sha256,
        split="train",
    )
    with pytest.raises(ValueError, match="both train and heldout"):
        _validate_lineage([source, alias])


def test_every_technique_receives_identical_ordered_case_objects():
    cases = [_case("one"), _case("two")]
    observation = TechniqueObservation("missing", {})
    first = _Technique("first", observation)
    second = _Technique("second", observation)

    report = run_benchmark(cases, [first, second], warm_reads=3)

    expected = [(id(case.frame), 3) for case in cases]
    assert first.seen == expected
    assert second.seen == expected
    assert report["first"]["case_ids"] == ["one", "two"]
    assert report["second"]["case_ids"] == ["one", "two"]


def test_inference_adapter_cannot_access_ground_truth():
    class _Isolated(_Technique):
        def evaluate(self, case, *, warm_reads):
            assert not hasattr(case, "labels")
            assert not hasattr(case, "expected_discovery")
            assert not hasattr(case, "expected_final_unknown_fields")
            assert not hasattr(case, "id")
            return self.observation

    technique = _Isolated("isolated", TechniqueObservation("missing", {}))
    run_benchmark([_case()], [technique])


def test_cached_invalidation_gate_is_not_applicable_to_legacy_capability():
    case = replace(
        _case(),
        prior_frame=_case().frame,
        sequence_kind="cached_invalidation",
        expected_cached_unknown_fields=tuple(FIELDS),
    )
    unsupported = _Technique("unsupported", TechniqueObservation("missing", {}))
    supported = _Technique(
        "supported",
        TechniqueObservation("missing", {}, cached_unknown_fields=()),
        supports_cached_layout=True,
    )
    report = run_benchmark([case], [unsupported, supported])
    unsupported_row = report["unsupported"]["cases"][0]
    supported_row = report["supported"]["cases"][0]
    assert unsupported_row["cached_invalidation_ok"] is None
    assert report["unsupported"]["summary"]["aggregate"][
        "cached_invalidation_failures"
    ] == 0
    assert supported_row["cached_invalidation_ok"] is False
    assert report["supported"]["summary"]["aggregate"][
        "cached_invalidation_failures"
    ] == 1


def test_technique_exception_becomes_auditable_error_row():
    class _Throwing(_Technique):
        def evaluate(self, case, *, warm_reads):
            raise RuntimeError("fixture read failed")

    technique = _Throwing("throwing", TechniqueObservation("missing", {}))
    report = run_benchmark([_case("one"), _case("two")], [technique])
    assert [row["panel_status"] for row in report["throwing"]["cases"]] == [
        "error", "error"
    ]
    assert report["throwing"]["summary"]["aggregate"]["reader_errors"] == 2
    assert all(
        row["detail"] == "RuntimeError: fixture read failed"
        for row in report["throwing"]["cases"]
    )
    assert all(
        row["panel_failure_stage"] == "reader_error"
        and all(
            field["rejection_stage"] == "reader_error"
            and field["detail"] == "RuntimeError: fixture read failed"
            for field in row["fields"].values()
        )
        for row in report["throwing"]["cases"]
    )


def test_negative_panel_detection_is_not_numeric_accuracy():
    original = _case()
    negative = BenchmarkCase(
        id="negative",
        source_id="negative",
        source_sha256=original.source_sha256,
        case_sha256=original.case_sha256,
        category=original.category,
        split=original.split,
        classification="negative",
        frame=original.frame,
        labels=_labels(),
        expected_discovery=False,
    )
    technique = _Technique(
        "false-panel", TechniqueObservation("found", {name: 1 for name in FIELDS})
    )

    report = run_benchmark([negative], [technique])
    metrics = report["false-panel"]["summary"]["aggregate"]
    assert metrics["eligible_fields"] == 0
    assert metrics["exact_coverage"] is None
    assert metrics["false_panel_acceptances"] == 1


def test_non_found_observation_cannot_smuggle_accepted_values():
    with pytest.raises(ValueError, match="non-found panel"):
        TechniqueObservation("missing", {"resource_health": 10})
    with pytest.raises(ValueError, match="non-found panel"):
        TechniqueObservation("ambiguous", {}, {"resource_health": [0, 0, 5, 5]})


def test_equal_number_from_wrong_region_is_incorrect_association():
    values = {name: value for name, value in zip(FIELDS, (10, 20, 30, 40))}
    regions = {name: [45, 6, 49, 10] for name in FIELDS}
    technique = _Technique(
        "misbound", TechniqueObservation("found", values, regions)
    )

    report = run_benchmark([_case()], [technique])
    metrics = report["misbound"]["summary"]["aggregate"]
    assert metrics["numeric_exact"] == 0
    assert metrics["incorrect_accepted"] == 4
    assert metrics["locations_correspond"] == 0


def test_full_panel_region_cannot_pass_row_association_guard():
    values = {name: value for name, value in zip(FIELDS, (10, 20, 30, 40))}
    technique = _Technique(
        "whole-panel",
        TechniqueObservation(
            "found", values, {name: [0, 0, 50, 12] for name in FIELDS}
        ),
    )

    result = run_benchmark([_case()], [technique])["whole-panel"]
    metrics = result["summary"]["aggregate"]
    assert metrics["numeric_exact"] == 0
    assert metrics["incorrect_accepted"] == 4
    assert metrics["accepted_location_mismatch"] == 4
    assert all(
        field["other_field_overlap_max"] == 1.0
        and field["other_field_centers_contained"] == 3
        for field in result["cases"][0]["fields"].values()
    )


def test_shared_corpus_association_guard_rejects_whole_panel_region():
    labels = _labels()
    assert not corpus.region_corresponds(
        "resource_health", [0, 0, 50, 12], labels
    )
    assert corpus.region_corresponds(
        "resource_health", labels["resource_health"]["box"], labels
    )


def test_intentional_unknown_is_scored_as_safe_rejection_not_lost_recall():
    case = replace(_case(), expected_final_unknown_fields=("resource_soul",))
    labels = _labels()
    values = {name: value for name, value in zip(FIELDS, (10, 20, 30, 40))}
    values["resource_soul"] = None
    safe = _Technique(
        "safe",
        TechniqueObservation(
            "found", values, {name: label["box"] for name, label in labels.items()}
        ),
    )
    report = run_benchmark([case], [safe])
    metrics = report["safe"]["summary"]["aggregate"]
    assert metrics["eligible_fields"] == 3
    assert metrics["numeric_exact"] == 3
    assert metrics["unknown"] == 0
    assert metrics["intentional_unknown_targets"] == 1
    assert metrics["safe_intentional_unknown"] == 1
    assert metrics["unsafe_intentional_unknown_acceptances"] == 0

    values["resource_soul"] = 30
    unsafe = _Technique(
        "unsafe",
        TechniqueObservation(
            "found", values, {name: label["box"] for name, label in labels.items()}
        ),
    )
    metrics = run_benchmark([case], [unsafe])["unsafe"]["summary"]["aggregate"]
    assert metrics["unsafe_intentional_unknown_acceptances"] == 1
    assert metrics["incorrect_accepted"] == 0


def test_markdown_does_not_show_all_unknown_as_perfect():
    null = _Technique("null", TechniqueObservation("missing", {}))
    techniques = run_benchmark([_case()], [null])
    markdown = render_markdown({"techniques": techniques})
    row = next(line for line in markdown.splitlines() if line.startswith("| `null`"))
    assert "100.0%" not in row
    assert "0.0%" in row


def test_missing_manifest_image_fails_instead_of_skipping(tmp_path: Path):
    manifest = {
        "schema_version": 1,
        "box_format": "xyxy-half-open",
        "screenshots": [
            {
                "id": "missing",
                "file": "originals/missing.png",
                "split": "heldout",
                "classification": "confirmed",
                "source": {"kind": "fixture"},
                "integrity": {
                    "sha256": "0" * 64,
                    "width": 50,
                    "height": 12,
                    "format": "PNG",
                },
                "client": {"evidence": "fixture"},
                "client_class": "fixture",
                "expected_discovery": True,
                "coverage": "full_screenshot",
                "data_origin": "real",
                "asset_fidelity": "source_original",
                "skills_panel": {"visible": True, "box": [0, 0, 50, 12]},
                "labels": _labels(),
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(corpus.ManifestError, match="missing file"):
        load_cases(manifest_path, include_generated=False)


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (
            {
                "expected_final_unknown_fields": list(FIELDS),
                "expected_discovery": False,
                "ambiguity_evidence": "two reviewed resource panels",
            },
            "non-empty abstention_reason",
        ),
        (
            {
                "expected_final_unknown_fields": ["resource_soul"],
                "expected_discovery": False,
                "abstention_reason": "selection is ambiguous",
                "ambiguity_evidence": "two reviewed resource panels",
            },
            "all four resources",
        ),
        (
            {
                "expected_final_unknown_fields": list(FIELDS),
                "expected_discovery": True,
                "abstention_reason": "selection is ambiguous",
                "ambiguity_evidence": "two reviewed resource panels",
            },
            "must not expect automatic layout selection",
        ),
        (
            {
                "expected_discovery": False,
                "abstention_reason": "selection is ambiguous",
            },
            "requires expected_final_unknown_fields",
        ),
        (
            {
                "expected_final_unknown_fields": list(FIELDS),
                "expected_discovery": False,
                "classification": "similar",
                "abstention_reason": "selection is ambiguous",
                "ambiguity_evidence": "two reviewed resource panels",
            },
            "needs reviewed visually-equivalent evidence",
        ),
        (
            {
                "expected_final_unknown_fields": list(FIELDS),
                "expected_discovery": False,
                "abstention_reason": "selection is ambiguous",
                "ambiguity_evidence": "two reviewed resource panels",
                "render_equivalence": {
                    "status": "visually_equivalent",
                    "evidence": "reviewed matching labels, rows, and chrome",
                },
            },
            "classified similar",
        ),
    ],
)
def test_manifest_rejects_incomplete_ambiguity_contract(
    tmp_path: Path, metadata: dict[str, object], message: str
):
    document = json.loads(corpus.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    entry = document["screenshots"][0]
    entry.update(metadata)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(corpus.ManifestError, match=message):
        corpus.load_manifest(manifest_path)


@pytest.fixture(scope="module")
def historical_manifest(tmp_path_factory):
    # Historical synthetic unit diagnostics do not need to manufacture variants
    # of newly collected originals. Keep their scope fixed and CI memory bounded.
    document = json.loads(corpus.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    document["screenshots"] = [
        entry for entry in document["screenshots"] if not entry.get("collection_batch")
    ]
    path = tmp_path_factory.mktemp("historical-corpus") / "manifest.json"
    (path.parent / "originals").mkdir()
    for entry in document["screenshots"]:
        shutil.copyfile(corpus.DEFAULT_MANIFEST.parent / entry["file"], path.parent / entry["file"])
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_generated_dense_scale_bank_has_stable_ids_and_hashes(historical_manifest):
    _, cases = load_cases(historical_manifest, include_generated=True)
    target = [
        case for case in cases
        if case.source_id == "user-paulistinha-20261013"
        and case.category == "lightweight_numeric_roi"
    ]
    factors = {float(case.transform.removeprefix("panel-scale-")) for case in target}

    assert factors == set(corpus.SCALE_BANK)
    assert len({case.id for case in cases}) == len(cases)
    assert all(len(case.source_sha256) == 64 and len(case.case_sha256) == 64 for case in cases)


def test_case_loader_defaults_to_preserved_originals_only():
    document, cases = load_cases(corpus.DEFAULT_MANIFEST)

    assert len(cases) == len(document["screenshots"])
    assert all(case.id == case.source_id and case.transform == "original" for case in cases)


def test_acceptance_selection_matches_reviewed_manifest_without_inference():
    document, cases = load_cases(corpus.DEFAULT_MANIFEST)
    by_id = {case.id: case for case in cases}
    expected_positive = {
        entry["id"]
        for entry in document["screenshots"]
        if entry["coverage"] == "full_screenshot"
        and entry["data_origin"] == "real"
        and entry["classification"] != "negative"
        and not entry.get("expected_final_unknown_fields")
        and entry["asset_fidelity"] in {"source_original", "source_transcode"}
        and entry["render_equivalence"]["status"]
        in {"target_family", "visually_equivalent"}
        and entry["render_equivalence"]["evidence"].strip()
    }
    expected_negative = {
        entry["id"]
        for entry in document["screenshots"]
        if entry["coverage"] == "full_screenshot"
        and entry["data_origin"] == "real"
        and entry["classification"] == "negative"
        and entry["asset_fidelity"] in {"source_original", "source_transcode"}
    }
    expected_ambiguity = {
        entry["id"]
        for entry in document["screenshots"]
        if entry["coverage"] == "full_screenshot"
        and entry["data_origin"] == "real"
        and set(entry.get("expected_final_unknown_fields") or ()) == set(FIELDS)
        and entry["asset_fidelity"] in {"source_original", "source_transcode"}
    }

    assert {
        identifier
        for identifier, case in by_id.items()
        if _acceptance_role(case)[0] == "positive_acceptance"
    } == expected_positive
    assert {
        identifier
        for identifier, case in by_id.items()
        if _acceptance_role(case)[0] == "negative_safety_control"
    } == expected_negative
    assert {
        identifier
        for identifier, case in by_id.items()
        if _acceptance_role(case)[0] == "ambiguous_safety_control"
    } == expected_ambiguity
    assert _acceptance_role(by_id["dfg-kalibra-ek362"])[0] == "reference_diagnostic"
    assert _acceptance_role(by_id["dfg-pvp-ek133"])[0] == "reference_diagnostic"


def test_existing_case_ids_and_hashes_match_schema_v1_reference_report(historical_manifest):
    reference = json.loads(
        (Path("reports") / "resource-benchmark.json").read_text(encoding="utf-8")
    )
    _, cases = load_cases(historical_manifest, include_generated=True)
    reference_rows = next(iter(reference["techniques"].values()))["cases"]

    current = {
        case.id: (case.source_sha256, case.case_sha256) for case in cases
    }
    assert all(
        current[row["case_id"]] == (row["source_sha256"], row["case_sha256"])
        for row in reference_rows
    )


def test_generated_case_inventory_and_all_visible_boxes_are_in_bounds(historical_manifest):
    document, originals = load_cases(corpus.DEFAULT_MANIFEST)
    historical, cases = load_cases(historical_manifest, include_generated=True)
    generated = [case for case in cases if "::" in case.id]

    assert len(document["screenshots"]) == len(originals)
    manifest_files = {
        (corpus.DEFAULT_MANIFEST.parent / entry["file"]).resolve()
        for entry in document["screenshots"]
    }
    corpus_files = {
        path.resolve()
        for path in (corpus.DEFAULT_MANIFEST.parent / "originals").iterdir()
        if path.is_file()
        and path.suffix.lower()
        in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
    }
    assert manifest_files == corpus_files

    by_source = {
        entry["id"]: [case for case in generated if case.source_id == entry["id"]]
        for entry in document["screenshots"]
    }
    mandatory_transforms = {
        "translated",
        "width-reflow",
        "full-scale-0.75",
        "full-scale-1.5",
        "duplicate-panel",
        "cached-wrong-size",
        "cached-label-occlusion",
        "cached-digit-occlusion",
        *(f"panel-scale-{factor:g}" for factor in corpus.SCALE_BANK),
    }
    for entry in historical["screenshots"]:
        source_cases = by_source[entry["id"]]
        if entry.get("expected_final_unknown_fields"):
            assert source_cases == []
            continue
        if entry["classification"] == "negative":
            assert [case.transform for case in source_cases] == ["excluded"]
            continue
        transforms = {case.transform for case in source_cases}
        panel_left, _top, panel_right, _bottom = entry["skills_panel"]["box"]
        panel_width = panel_right - panel_left
        width = entry["integrity"]["width"]
        relocatable = (
            panel_left >= panel_width + 8 or width - panel_right >= panel_width + 8
        )
        assert transforms == mandatory_transforms | (
            {"cached-relocation"} if relocatable else set()
        )
    assert all(case.source_coverage is not None for case in cases)
    groups = {_evidence_group(case) for case in generated}
    assert groups == {"synthetic_full_frame_stress", "synthetic_cropped_stress"}
    for case in [*cases, *originals]:
        for label in case.labels.values():
            if not label["visible"]:
                continue
            left, top, right, bottom = label["box"]
            assert 0 <= left < right <= case.frame.width, case.id
            assert 0 <= top < bottom <= case.frame.height, case.id


def test_real_multi_panel_control_requires_layout_and_value_abstention():
    ambiguity = replace(
        _case("real-multi-panel"),
        expected_discovery=False,
        expected_final_unknown_fields=tuple(FIELDS),
        render_equivalence_status="visually_equivalent",
        abstention_reason="two legitimate resource panels show the same fields",
        ambiguity_evidence="reviewed full screenshot contains Skills and Character dialogs",
    )
    safe = run_benchmark(
        [ambiguity], [_Technique("safe", TechniqueObservation("ambiguous", {}))]
    )["safe"]
    safe_metric = safe["summary"]["acceptance"]

    assert safe["cases"][0]["acceptance_role"] == "ambiguous_safety_control"
    for field in FIELDS:
        assert safe["cases"][0]["fields"][field]["expected"] == ambiguity.labels[field]["value"]
        assert safe["cases"][0]["fields"][field]["eligible"] is False
    assert safe_metric["eligible_fields"] == 0
    assert safe_metric["intentional_unknown_targets"] == 4
    assert safe_metric["safe_intentional_unknown"] == 4
    assert safe_metric["unsafe_intentional_unknown_acceptances"] == 0
    assert safe_metric["false_panel_acceptances"] == 0

    values = dict(zip(FIELDS, (10, 20, 30, 40)))
    unsafe = run_benchmark(
        [ambiguity],
        [
            _Technique(
                "unsafe",
                TechniqueObservation(
                    "found",
                    values,
                    {name: label["box"] for name, label in _labels().items()},
                ),
            )
        ],
    )["unsafe"]["summary"]["acceptance"]
    assert unsafe["eligible_fields"] == 0
    assert unsafe["unsafe_intentional_unknown_acceptances"] == 4
    assert unsafe["false_panel_acceptances"] == 1

    unreviewed = replace(ambiguity, render_equivalence_evidence="")
    assert _acceptance_role(unreviewed)[0] == "reference_diagnostic"


def test_simple_benchmark_preserves_ambiguity_contract_and_excludes_numeric_recall():
    document = json.loads(corpus.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    entry = document["screenshots"][0]
    entry.update(
        expected_discovery=False,
        expected_final_unknown_fields=list(FIELDS),
        abstention_reason="two legitimate resource panels",
        ambiguity_evidence="reviewed full screenshot",
    )

    class _AbstainingReader:
        def discover(self, _frame, *, use_ocr):
            assert use_ocr is False
            return type(
                "Discovery",
                (),
                {"layout": None, "candidates": (), "detail": "ambiguous"},
            )()

    row = corpus.benchmark_entry(
        _AbstainingReader(),
        entry,
        corpus.DEFAULT_MANIFEST,
        use_ocr=False,
        warm_reads=1,
    )
    summary = corpus.summarize([row], max_discovery_seconds=60)
    group = summary["groups"][f"{entry['data_origin']}/{entry['split']}"]

    assert row.expected_final_unknown_fields == tuple(FIELDS)
    assert row.abstention_reason == "two legitimate resource panels"
    assert row.ambiguity_evidence == "reviewed full screenshot"
    assert row.intentional_unknown_targets == 4
    assert row.safe_intentional_unknown == 4
    assert group["fields_expected"] == 0
    assert summary["passed"] is True


def test_generated_pixels_boxes_and_digests_match_declared_transforms(historical_manifest):
    document, cases = load_cases(historical_manifest, include_generated=True)
    entry = next(
        item for item in document["screenshots"]
        if item["id"] == "user-paulistinha-20261013"
    )
    with Image.open(corpus.DEFAULT_MANIFEST.parent / entry["file"]) as image:
        source = np.asarray(image.convert("RGB"))
    by_transform = {
        case.transform: case for case in cases if case.source_id == entry["id"]
    }

    translated = by_transform["translated"]
    assert np.array_equal(
        translated.frame.pixels[31:31 + source.shape[0], 53:53 + source.shape[1]], source
    )
    for name in FIELDS:
        assert translated.labels[name]["box"] == [
            entry["labels"][name]["box"][0] + 53,
            entry["labels"][name]["box"][1] + 31,
            entry["labels"][name]["box"][2] + 53,
            entry["labels"][name]["box"][3] + 31,
        ]

    panel_left, panel_top, panel_right, panel_bottom = entry["skills_panel"]["box"]
    value_left = min(entry["labels"][name]["box"][0] for name in FIELDS)
    split_x = max(panel_left + 1, value_left - 8)
    shift = max(24, round((panel_right - panel_left) * 0.20))
    reflow = by_transform["width-reflow"]
    assert np.array_equal(reflow.frame.pixels[:, :split_x], source[:, :split_x])
    assert np.array_equal(reflow.frame.pixels[:, split_x + shift:], source[:, split_x:])
    assert all(
        reflow.labels[name]["box"][0] == entry["labels"][name]["box"][0] + shift
        for name in FIELDS
    )

    for factor in (0.75, 1.5):
        scaled = by_transform[f"full-scale-{factor:g}"]
        expected_pixels = cv2.resize(
            source, None, fx=factor, fy=factor, interpolation=cv2.INTER_LINEAR
        )
        assert np.array_equal(scaled.frame.pixels, expected_pixels)
        for name in FIELDS:
            left, top, right, bottom = entry["labels"][name]["box"]
            assert scaled.labels[name]["box"] == [
                floor(left * factor), floor(top * factor),
                ceil(right * factor), ceil(bottom * factor),
            ]

    factor = 0.65
    roi_scaled = by_transform[f"panel-scale-{factor:g}"]
    expected_roi = cv2.resize(
        source[panel_top:panel_bottom, panel_left:panel_right],
        None,
        fx=factor,
        fy=factor,
        interpolation=cv2.INTER_LINEAR,
    )
    assert np.array_equal(roi_scaled.frame.pixels, expected_roi)
    for name in FIELDS:
        left, top, right, bottom = entry["labels"][name]["box"]
        assert roi_scaled.labels[name]["box"] == [
            floor((left - panel_left) * factor),
            floor((top - panel_top) * factor),
            ceil((right - panel_left) * factor),
            ceil((bottom - panel_top) * factor),
        ]

    for case in (translated, reflow, roi_scaled):
        assert case.case_sha256 == sha256(case.frame.pixels.tobytes()).hexdigest()


def test_kalibra_location_regression_cases_remain_in_dense_matrix(historical_manifest):
    # These strict containment misses were observed before the final reader
    # freeze. Keep their stable IDs/affected fields explicit for the final run.
    regressions = {
        1.25: {"resource_soul"},
        1.3: {"resource_health", "resource_mana", "resource_soul"},
        1.5: {"resource_health", "resource_mana", "resource_soul"},
        1.75: {"resource_health", "resource_mana", "resource_soul"},
        2.0: {"resource_mana", "resource_soul"},
        2.5: {"resource_health", "resource_mana", "resource_soul"},
    }
    _, cases = load_cases(historical_manifest, include_generated=True)
    ids = {case.id for case in cases}
    for factor, fields in regressions.items():
        identifier = f"dfg-kalibra-ek362::panel-scale-{factor:g}"
        assert identifier in ids
        assert fields <= set(FIELDS)
