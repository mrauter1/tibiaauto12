"""Validate complete frozen benchmark shards and report current split scorecards."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.screenshot_split import group_assignments, load_split  # noqa: E402
from tools.summarize_resource_techniques import changes, timings  # noqa: E402

CORPUS = ROOT / "tests/fixtures/screenshots"
BATCH = "2026-10-17-latest"


def scorecard(rows, entries, groups):
    fields = [(row, name, field) for row in rows for name, field in row["fields"].items()]
    eligible = [(r, n, f) for r, n, f in fields if f["eligible"]]
    na = [(r, n, f) for r, n, f in fields
          if entries[r["case_id"]]["labels"][n]["value"] is None
          and entries[r["case_id"]].get("resource_annotations", {}).get(n, {}).get("display_text") is None]
    controls = [r for r in rows if r["acceptance_role"].endswith("safety_control")]
    group_fields = defaultdict(list)
    for row, _, field in eligible:
        group_fields[groups[row["case_id"]]].append(field)
    rates = [sum(f["exact"] for f in values) / len(values) for values in group_fields.values()]
    cached = [r for r in rows if r["cached_timings"]["samples_ms"]]
    by_resource = {}
    for name in ("resource_health", "resource_mana", "resource_soul", "resource_capacity"):
        selected = [f for _, n, f in eligible if n == name]
        unknown = [f for _, n, f in na if n == name]
        by_resource[name] = {
            "readable": len(selected), "correct": sum(f["exact"] for f in selected),
            "unknown": sum(f["unknown"] for f in selected),
            "incorrect_accepted": sum(f["incorrect_accepted"] for f in selected),
            "na": len(unknown), "unsafe_na_accepts": sum(f["accepted"] for f in unknown),
        }
    return {
        "images": len(rows), "source_groups": len({groups[r["case_id"]] for r in rows}),
        "readable_integer_fields": len(eligible),
        "correct_value_and_region": sum(f["exact"] for _, _, f in eligible),
        "unknown": sum(f["unknown"] for _, _, f in eligible),
        "incorrect_accepted": sum(f["incorrect_accepted"] for _, _, f in eligible),
        "wrong_numeric_value": sum(f["numeric_mismatch_accepted"] for _, _, f in eligible),
        "accepted_unbound_region": sum(f["accepted"] and f["location_corresponds"] is not True for _, _, f in eligible),
        "accepted_region_contains_other_resource_center": sum(f["accepted"] and (f["other_field_centers_contained"] or 0) > 0 for _, _, f in eligible),
        "na_fields": len(na), "na_abstentions": sum(not f["accepted"] for _, _, f in na),
        "unsafe_na_accepts": sum(f["accepted"] for _, _, f in na),
        "ambiguity_required_unknown_fields": sum(f["expected_unknown"] for _, _, f in fields),
        "unsafe_ambiguity_accepts": sum(f["unsafe_expected_unknown_acceptance"] for _, _, f in fields),
        "unscored_accepted": sum(f["accepted"] and not f["eligible"] for _, _, f in fields),
        "control_images": len(controls),
        "control_accepted_values": sum(f["accepted"] for r in controls for f in r["fields"].values()),
        "control_false_panels": sum(r["false_panel_acceptance"] for r in controls),
        "panel_statuses": dict(Counter(r["panel_status"] for r in rows)),
        "miss_stages": dict(Counter(f["rejection_stage"] for _, _, f in eligible if f["unknown"])),
        "group_macro": {"groups_with_readable_targets": len(rates),
                        "mean_correct_fraction": round(float(np.mean(rates)), 6) if rates else None},
        "per_resource": by_resource,
        "initialization": timings([r["initialization_ms"] for r in rows]),
        "discovery": timings([r["discovery_ms"] for r in rows]),
        "first_read": timings([r["first_read_ms"] for r in rows if r["first_read_ms"] is not None]),
        "ready_on_detected_images": timings([r["initialization_ms"] + r["discovery_ms"] + r["first_read_ms"] for r in cached]),
        "cached_four_field_reads": timings([t for r in cached for t in r["cached_timings"]["samples_ms"]]),
        "cached_images": len(cached),
        "cached_images_above_50ms_p95": [r["case_id"] for r in cached if r["cached_timings"]["p95_ms"] > 50],
        "slowest_image_cached_p95_ms": max((r["cached_timings"]["p95_ms"] for r in cached), default=None),
        "inconsistent_cached_outputs": [r["case_id"] for r in rows if not r["cached_outputs_consistent"]],
    }


def summarize(directory, revision, run_url):
    manifest = json.loads((CORPUS / "manifest.json").read_text())
    entries = {e["id"]: e for e in manifest["screenshots"]}
    split, membership = load_split(CORPUS / "split-2026-10-17-latest.json", list(entries.values()))
    groups = group_assignments(list(entries.values()))
    reports = sorted(directory.glob("shard-*.json"))
    if not reports:
        raise ValueError("No completed shard reports")
    all_variants = {}; seen_shards = set(); fingerprints = None; implementation = None
    raw_files = []; environments = []; features = None
    for path in reports:
        report = json.loads(path.read_text())
        assert report["schema"] == "tibiaauto.latest-screenshot-benchmark"
        assert report["valid"] and report["reader_receives_annotations"] is False
        assert report["warm_reads"] == 50 and report["opencv_threads"] == 1
        assert report["shard"] not in seen_shards
        seen_shards.add(report["shard"])
        if fingerprints is None:
            fingerprints, implementation, features = report["fingerprints"], report["implementation"], report["features"]
            assert set(features) == {"baseline", "accelerated_fast", "accelerated_robust"}
            all_variants = {v: {} for v in features}
        assert report["fingerprints"] == fingerprints and report["implementation"] == implementation
        assert report["features"] == features and set(report["techniques"]) == set(features)
        expected_shard_ids = list(entries)[report["shard"]::report["shards"]]
        for variant, technique in report["techniques"].items():
            assert technique["case_ids"] == expected_shard_ids
            assert technique["case_ids"] == [r["case_id"] for r in technique["cases"]]
            for row in technique["cases"]:
                identifier = row["case_id"]
                assert identifier not in all_variants[variant]
                assert row["source_sha256"] == entries[identifier]["integrity"]["sha256"]
                assert row["evaluation_split"] == membership[identifier]
                assert row["historical_manifest_split"] == entries[identifier]["split"]
                assert row["transform"] == "original" and row["data_origin"] == "real"
                assert row["cached_timings"]["sample_count"] == (50 if row["panel_status"] == "found" else 0)
                assert not any(f["eligible"] and sum((f["exact"], f["unknown"], f["incorrect_accepted"])) != 1 for f in row["fields"].values())
                all_variants[variant][identifier] = row
        raw_files.append({"file": path.name, "sha256": sha256(path.read_bytes()).hexdigest()})
        environments.append({"shard": report["shard"], **report["environment"]})
    assert seen_shards == set(range(report["shards"]))
    for rows in all_variants.values():
        assert set(rows) == set(entries)
        assert all(row["case_sha256"] == all_variants["baseline"][identifier]["case_sha256"] for identifier, row in rows.items())
    for name, digest in {**fingerprints, **implementation["code_files"]}.items():
        blob = subprocess.check_output(["git", "show", f"{revision}:{name}"], cwd=ROOT)
        assert sha256(blob).hexdigest() == digest, (name, "revision mismatch")
    freeze = json.loads((CORPUS / "collection-2026-10-17-latest.json").read_text())
    assert freeze["implementation"] == implementation
    assert all(fingerprints[name] == digest for name, digest in freeze["experimental_reader_files_sha256"].items())
    variants = {}
    baseline = all_variants["baseline"]
    for variant, indexed in all_variants.items():
        rows = list(indexed.values())
        cohorts = {}
        for partition in ("development", "test", "reference"):
            selected = [r for r in rows if membership[r["case_id"]] == partition]
            cohorts[partition] = {"all": scorecard(selected, entries, groups)}
            for role in sorted({r["acceptance_role"] for r in selected}):
                cohorts[partition][role] = scorecard([r for r in selected if r["acceptance_role"] == role], entries, groups)
        new = [r for r in rows if entries[r["case_id"]].get("collection_batch") == BATCH]
        full = [r for r in rows if r["source_coverage"] == "full_screenshot"]
        new_full_readable = [r for r in new if entries[r["case_id"]]["expected_discovery"]]
        new_partial = [r for r in new if sum(f["eligible"] for f in r["fields"].values()) in (1, 2, 3)]
        new_negative = [r for r in new if r["acceptance_role"] == "negative_safety_control"]
        change = changes(baseline, indexed)
        common = [r["case_id"] for r in full if r["acceptance_role"] == "positive_acceptance"
                  and r["panel_status"] == baseline[r["case_id"]]["panel_status"] == "found"]
        total = scorecard(rows, entries, groups)
        variants[variant] = {
            "features": features[variant], "all_originals": total,
            "all_full_screenshots": scorecard(full, entries, groups), "partitions": cohorts,
            "new_batch": {"all": scorecard(new, entries, groups),
                          "four_readable": scorecard(new_full_readable, entries, groups),
                          "partly_readable": scorecard(new_partial, entries, groups),
                          "all_na": scorecard(new_negative, entries, groups)},
            "changes_from_baseline": change,
            "paired_full_primary_cached": {
                "images_detected_by_both": len(common),
                "baseline": timings([t for i in common for t in baseline[i]["cached_timings"]["samples_ms"]]),
                "candidate": timings([t for i in common for t in indexed[i]["cached_timings"]["samples_ms"]]),
            },
            "new_batch_misses": [{"case_id": r["case_id"], "partition": membership[r["case_id"]],
                "field": name, "expected": f["expected"], "actual": f["actual"],
                "stage": f["rejection_stage"], "detail": f["detail"], "incorrect_accepted": f["incorrect_accepted"]}
                for r in new for name, f in r["fields"].items() if f["eligible"] and not f["exact"]],
            "gates": {
                "no_incorrect_accepted": total["incorrect_accepted"] == 0,
                "no_na_or_ambiguity_accepts": total["unsafe_na_accepts"] == total["unsafe_ambiguity_accepts"] == 0,
                "no_unscored_accepts": total["unscored_accepted"] == 0,
                "all_safety_controls_abstain": total["control_accepted_values"] == total["control_false_panels"] == 0,
                "no_correct_field_regressions": not change["regressed"],
                "cached_outputs_consistent": not total["inconsistent_cached_outputs"],
                "every_detected_full_image_p95_within_50ms": not scorecard(full, entries, groups)["cached_images_above_50ms_p95"],
            },
        }
    return {
        "schema": "tibiaauto.latest-screenshot-summary", "version": 1,
        "evaluated_revision": revision, "run_url": run_url,
        "originals": len(entries), "image_variant_cases": len(entries) * len(variants),
        "partition_counts": {key: len(ids) for key, ids in split["partitions"].items()},
        "annotation_freeze": "tests/fixtures/screenshots/collection-2026-10-17-latest.json",
        "no_test_tuning": True, "fingerprints": fingerprints, "implementation": implementation,
        "raw_files": raw_files, "environments": environments, "variants": variants,
        "limitations": freeze["limits"] + ["Cached latency is conditional on successful discovery. Missing layouts do not count as fast successful reads.", "One run per image/variant; 50 repeats measure warm static behavior, not independent gameplay samples."],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.input, args.revision, args.run_url)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({v: {"test": r["partitions"]["test"]["all"], "gates": r["gates"]}
                      for v, r in result["variants"].items()}, indent=2))
