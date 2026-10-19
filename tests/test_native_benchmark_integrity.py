"""Aggregation must never silently turn a partial factorial run into evidence."""
from copy import deepcopy

import pytest

from tools.resource_native_experiments import FACTORS
from tools.summarize_native_resources import combine, guard_result


def fixture_reports():
    entries = {name: {"integrity": {"sha256": name}} for name in ("one", "two")}
    membership = {"one": "development", "two": "test"}
    reports = []
    for shard, identifier in enumerate(entries):
        row = {
            "case_id": identifier, "source_sha256": identifier,
            "case_sha256": identifier, "transform": "original", "data_origin": "real",
            "evaluation_split": membership[identifier], "panel_status": "missing",
            "cached_timings": {"sample_count": 0, "samples_ms": []},
            "new_frame_timings": {"sample_count": 0, "samples_ms": []},
            "fields": {"resource_health": {"eligible": True, "exact": False,
                                            "unknown": True, "incorrect_accepted": False}},
        }
        reports.append({
            "schema": "tibiaauto.native-resource-factorial", "valid": True,
            "requested_case_ids": list(entries), "reader_receives_annotations": False,
            "features": {k: list(v) for k, v in FACTORS.items()}, "revision": "revision",
            "warm_reads": 50, "opencv_threads": 1, "fingerprints": {"reader.py": "digest"},
            "shard": shard, "shards": 2, "budget_ms": 50,
            "native_environment": {"system": "Windows", "native_render_verified": True,
                                   "font_sha256": "font"},
            "techniques": {v: {"case_ids": [identifier], "cases": [deepcopy(row)]}
                           for v in FACTORS},
        })
    return reports, entries, membership


def test_complete_factorial_preserves_unknowns_and_wrong_acceptances():
    reports, entries, membership = fixture_reports()
    field = reports[1]["techniques"]["all"]["cases"][0]["fields"]["resource_health"]
    field.update(unknown=False, incorrect_accepted=True)
    result = combine(reports, entries, membership, "revision")
    assert result["base"]["two"]["fields"]["resource_health"]["unknown"]
    assert result["all"]["two"]["fields"]["resource_health"]["incorrect_accepted"]


@pytest.mark.parametrize("problem", ["missing_shard", "duplicate_shard", "missing_variant",
                                    "different_code", "different_font", "wrong_pixels",
                                    "no_native", "missing_reads", "double_counted"])
def test_incomplete_or_incompatible_runs_are_rejected(problem):
    reports, entries, membership = fixture_reports()
    target = reports[1]
    if problem == "missing_shard":
        reports.pop()
    elif problem == "duplicate_shard":
        reports.append(deepcopy(target))
    elif problem == "missing_variant":
        del target["techniques"]["numbers"]
    elif problem == "different_code":
        target["fingerprints"]["reader.py"] = "different"
    elif problem == "different_font":
        target["native_environment"]["font_sha256"] = "different"
    elif problem == "no_native":
        target["native_environment"]["native_render_verified"] = False
    else:
        row = target["techniques"]["all"]["cases"][0]
        if problem == "wrong_pixels":
            row["case_sha256"] = "different"
        elif problem == "missing_reads":
            row["panel_status"] = "found"
        else:
            row["fields"]["resource_health"]["exact"] = True
    with pytest.raises(ValueError):
        combine(reports, entries, membership, "revision")


def test_diagnostic_failure_cannot_be_hidden_by_original_accuracy(tmp_path):
    report = tmp_path / "all.xml"
    report.write_text('<testsuites><testsuite tests="60" failures="1" errors="0" skipped="0">'
                      '<testcase name="partial_number"><failure>Unsafe suffix</failure></testcase>'
                      '</testsuite></testsuites>')
    result = guard_result(report)
    assert result["passed"] is False
    assert result["failed_tests"] == ["partial_number"]
