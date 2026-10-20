"""Validate and summarize the original-only technique benchmark shards."""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import subprocess

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests/fixtures/screenshots/manifest.json"


def timings(samples):
    return {
        "samples": len(samples),
        "p50_ms": round(float(np.percentile(samples, 50)), 3) if samples else None,
        "p95_ms": round(float(np.percentile(samples, 95)), 3) if samples else None,
        "max_ms": round(float(max(samples)), 3) if samples else None,
    }


def metrics(rows):
    fields = [f for r in rows for f in r["fields"].values()]
    eligible = [f for f in fields if f["eligible"]]
    controls = [r for r in rows if r["acceptance_role"].endswith("safety_control")]
    cached = [r for r in rows if r["cached_timings"]["samples_ms"]]
    return {
        "images": len(rows),
        "readable_integer_fields": len(eligible),
        "correct_value_and_region": sum(f["exact"] for f in eligible),
        "unknown": sum(f["unknown"] for f in eligible),
        "incorrect_accepted": sum(f["incorrect_accepted"] for f in eligible),
        "wrong_numeric_value": sum(f["numeric_mismatch_accepted"] for f in eligible),
        "accepted_unbound_region": sum(f["accepted"] and f["location_corresponds"] is not True for f in eligible),
        "accepted_region_contains_other_resource_center": sum(f["accepted"] and (f["other_field_centers_contained"] or 0) > 0 for f in eligible),
        "panel_statuses": dict(Counter(r["panel_status"] for r in rows)),
        "control_images": len(controls),
        "control_accepted_values": sum(f["accepted"] for r in controls for f in r["fields"].values()),
        "control_false_panels": sum(r["false_panel_acceptance"] for r in controls),
        "expected_unknown_accepted": sum(f["unsafe_expected_unknown_acceptance"] for f in fields),
        "unscored_accepted": sum(f["accepted"] and not f["eligible"] for f in fields),
        "initialization": timings([r["initialization_ms"] for r in rows]),
        "discovery": timings([r["discovery_ms"] for r in rows]),
        "first_read": timings([r["first_read_ms"] for r in rows if r["first_read_ms"] is not None]),
        "cached_four_field_reads": timings([t for r in cached for t in r["cached_timings"]["samples_ms"]]),
        "cached_images": len(cached),
        "cached_images_above_50ms_p95": [r["case_id"] for r in cached if r["cached_timings"]["p95_ms"] > 50],
        "inconsistent_cached_outputs": [r["case_id"] for r in rows if not r["cached_outputs_consistent"]],
    }


def changes(baseline, candidate):
    gained, lost = [], []
    for case_id, old in baseline.items():
        new = candidate[case_id]
        for name, previous in old["fields"].items():
            current = new["fields"][name]
            item = {"case_id": case_id, "field": name, "expected": previous["expected"],
                    "actual": current["actual"], "role": old["acceptance_role"],
                    "previous_stage": previous["rejection_stage"],
                    "current_stage": current["rejection_stage"], "detail": current["detail"]}
            if current["exact"] and not previous["exact"]:
                gained.append(item)
            if previous["exact"] and not current["exact"]:
                lost.append(item)
    return {"gained": gained, "regressed": lost}


def summarize(directory, revision):
    entries = {e["id"]: e for e in json.loads(MANIFEST.read_text())["screenshots"]}
    expected_ids = set(entries)
    files = sorted(directory.glob("shard-*.json"))
    if not files:
        raise ValueError("No completed shard reports found")
    all_variants = {}
    fingerprints = None
    features = None
    environments, raw_files, shard_ids = [], [], set()
    for path in files:
        report = json.loads(path.read_text())
        assert report["valid"] and report["reader_receives_annotations"] is False
        assert report["warm_reads"] == 50 and report["opencv_threads"] == 1
        assert report["shard"] not in shard_ids
        shard_ids.add(report["shard"])
        if fingerprints is None:
            fingerprints, features = report["fingerprints"], report["features"]
            all_variants = {v: {} for v in features}
        assert report["fingerprints"] == fingerprints and report["features"] == features
        assert set(report["techniques"]) == set(all_variants)
        for variant, result in report["techniques"].items():
            assert result["case_ids"] == [r["case_id"] for r in result["cases"]]
            for row in result["cases"]:
                case_id = row["case_id"]
                assert case_id not in all_variants[variant]
                assert row["source_sha256"] == entries[case_id]["integrity"]["sha256"]
                assert row["transform"] == "original" and row["data_origin"] == "real"
                assert row["cached_timings"]["sample_count"] == (50 if row["panel_status"] == "found" else 0)
                all_variants[variant][case_id] = row
        environments.append({"shard": report["shard"], **report["environment"]})
        raw_files.append({"file": path.name, "sha256": sha256(path.read_bytes()).hexdigest()})
    assert shard_ids == set(range(report["shards"]))
    for rows in all_variants.values():
        assert set(rows) == expected_ids
        for case_id, row in rows.items():
            assert row["case_sha256"] == all_variants["baseline"][case_id]["case_sha256"]
    for name, digest in fingerprints.items():
        blob = subprocess.check_output(["git", "show", f"{revision}:{name}"], cwd=ROOT)
        assert sha256(blob).hexdigest() == digest, (name, "evaluated code/input mismatch")
    baseline = all_variants["baseline"]
    variants = {}
    for variant, indexed in all_variants.items():
        rows = list(indexed.values())
        groups = {}
        for cohort in ("new", "prior"):
            selected = [r for r in rows if (entries[r["case_id"]].get("collection_batch") == "2026-10-17") == (cohort == "new")]
            groups[cohort] = {role: metrics([r for r in selected if r["acceptance_role"] == role])
                              for role in sorted({r["acceptance_role"] for r in selected})}
        change = changes(baseline, indexed)
        common = [r for r in rows if r["acceptance_role"] == "positive_acceptance" and entries[r["case_id"]].get("collection_batch") == "2026-10-17" and r["panel_status"] == baseline[r["case_id"]]["panel_status"] == "found"]
        common_ids = [r["case_id"] for r in common]
        variants[variant] = {"features": features[variant], "all_originals": metrics(rows),
            "groups": groups, "changes_from_baseline": change,
            "paired_new_primary_cached": {
                "common_ids": common_ids,
                "baseline": timings([t for i in common_ids for t in baseline[i]["cached_timings"]["samples_ms"]]),
                "candidate": timings([t for r in common for t in r["cached_timings"]["samples_ms"]]),
                "per_image_median_speedup": {r["case_id"]: round(baseline[r["case_id"]]["cached_timings"]["p50_ms"] / r["cached_timings"]["p50_ms"], 3) for r in common}},
            "remaining_new_primary_unknowns": [{"case_id":r["case_id"],"field":name,"expected":f["expected"],"stage":f["rejection_stage"],"detail":f["detail"]} for r in rows if entries[r["case_id"]].get("collection_batch") == "2026-10-17" and r["acceptance_role"] == "positive_acceptance" for name,f in r["fields"].items() if f["unknown"]]}
        total = variants[variant]["all_originals"]
        variants[variant]["gates"] = {
            "no_incorrect_accepted": total["incorrect_accepted"] == 0,
            "no_control_accepts": total["control_accepted_values"] == total["control_false_panels"] == 0,
            "no_unscored_accepts": total["unscored_accepted"] == 0,
            "no_correct_field_regressions": not change["regressed"],
            "consistent_static_repeats": not total["inconsistent_cached_outputs"],
            "new_primary_aggregate_cached_p95_within_50ms": groups["new"]["positive_acceptance"]["cached_four_field_reads"]["p95_ms"] <= 50,
            "every_new_primary_detected_image_cached_p95_within_50ms": not groups["new"]["positive_acceptance"]["cached_images_above_50ms_p95"],
        }
    equivalence = {}
    for earlier, later in (("combined_fast", "combined_pruned_fast"),
                           ("combined_robust", "combined_pruned_robust"),
                           ("combined_pruned_fast", "accelerated_fast"),
                           ("combined_pruned_robust", "accelerated_robust")):
        if earlier not in all_variants or later not in all_variants:
            continue
        differences = []
        for case_id, old in all_variants[earlier].items():
            new = all_variants[later][case_id]
            if old["panel_status"] != new["panel_status"]:
                differences.append({"case_id":case_id, "difference":"panel_status"})
            for field, previous in old["fields"].items():
                current = new["fields"][field]
                for key in ("actual", "exact", "detected_region"):
                    if previous[key] != current[key]:
                        differences.append({"case_id":case_id, "field":field, "difference":key})
        equivalence[f"{earlier} -> {later}"] = {"identical_values_and_regions":not differences,
                                                "differences":differences}
    return {"schema":"tibiaauto.resource-technique-summary", "version":1,
        "evaluated_revision":revision, "original_count":len(entries), "variant_count":len(variants),
        "working_cached_p95_budget_ms":50, "fingerprints":fingerprints,
        "raw_files":raw_files, "environments":environments, "variants":variants,
        "optimization_equivalence":equivalence,
        "limitations":["Previously inspected corpus is development/regression evidence, not an unseen holdout.",
            "Cached timings repeat static originals. All four resource rows are processed each time; glyph rankings, not output values, are memoized. These are not live capture/freshness or changing-frame latency measurements.",
            "Cases are paired on the same shard host/settings; aggregate quantiles pool several hosted machines and are descriptive, not confidence intervals.",
            "N/A/unscored fields remain outside integer accuracy; accepted values on those fields are separately checked."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    summary = summarize(args.directory, args.revision)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    for name, result in summary["variants"].items():
        new = result["groups"]["new"]["positive_acceptance"]
        print(name, new["correct_value_and_region"], new["unknown"], new["incorrect_accepted"],
              new["cached_four_field_reads"], "gates", result["gates"])


if __name__ == "__main__":
    main()
