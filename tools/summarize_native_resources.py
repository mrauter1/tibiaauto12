"""Validate the complete native factorial run and expose gains and regressions."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.resource_native_experiments import FACTORS  # noqa: E402
from tools.screenshot_split import group_assignments, load_split  # noqa: E402
from tools.summarize_latest_screenshots import scorecard  # noqa: E402
from tools.summarize_resource_techniques import changes, timings  # noqa: E402

CORPUS = ROOT / "tests/fixtures/screenshots"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def combine(reports, entries, membership, revision):
    """Reject partial or incompatible reports instead of reporting a subset."""
    require(bool(reports), "No completed shards")
    first = reports[0]
    require(first["requested_case_ids"] == list(entries), "Full corpus is required")
    require(first["budget_ms"] == 50, "Different predeclared timing budget")
    require(first["features"] == {k: list(v) for k, v in FACTORS.items()},
            "All eight factorial variants are required")
    rows = {variant: {} for variant in FACTORS}
    seen = set()
    for report in reports:
        require(report["schema"] == "tibiaauto.native-resource-factorial" and report["valid"],
                "Invalid benchmark schema/status")
        require(report["reader_receives_annotations"] is False, "Reader was given annotations")
        require(report["warm_reads"] == 50 and report["opencv_threads"] == 1,
                "Different repetition/thread settings")
        require(report["revision"] == revision, "Revision mismatch")
        for key in ("fingerprints", "features", "shards", "requested_case_ids", "budget_ms"):
            require(report[key] == first[key], "Mismatched " + key)
        native = report["native_environment"]
        require(native["system"] == "Windows" and native.get("native_render_verified") is True,
                "Native Windows rendering was not verified")
        require(native["font_sha256"] == first["native_environment"]["font_sha256"],
                "Different installed font versions")
        shard = report["shard"]
        require(shard not in seen and 0 <= shard < report["shards"], "Duplicate/invalid shard")
        seen.add(shard)
        expected = list(entries)[shard::report["shards"]]
        require(set(report["techniques"]) == set(rows), "Missing variant")
        for variant, technique in report["techniques"].items():
            require(technique["case_ids"] == expected, "Wrong case coverage")
            require([r["case_id"] for r in technique["cases"]] == expected, "Wrong case rows")
            for row in technique["cases"]:
                identifier = row["case_id"]
                require(identifier not in rows[variant], "Duplicate case")
                require(row["source_sha256"] == entries[identifier]["integrity"]["sha256"],
                        "Original screenshot hash mismatch")
                require(row["transform"] == "original" and row["data_origin"] == "real",
                        "Generated images cannot enter acceptance totals")
                require(row["evaluation_split"] == membership[identifier], "Split mismatch")
                count = 50 if row["panel_status"] == "found" else 0
                for name in ("cached_timings", "new_frame_timings"):
                    require(row[name]["sample_count"] == len(row[name]["samples_ms"]) == count,
                            "Missing read repetitions")
                for field in row["fields"].values():
                    require(not field["eligible"] or sum((field["exact"], field["unknown"],
                                                          field["incorrect_accepted"])) == 1,
                            "Field is missing or counted twice")
                rows[variant][identifier] = row
    require(seen == set(range(first["shards"])), "Missing completed shards")
    for variant, indexed in rows.items():
        require(set(indexed) == set(entries), "Incomplete corpus")
        for identifier, row in indexed.items():
            require(row["case_sha256"] == rows["base"][identifier]["case_sha256"],
                    "Variants did not receive identical pixels")
    return rows


def card(rows, entries, groups):
    result = scorecard(rows, entries, groups)
    measured = [r for r in rows if r["new_frame_timings"]["sample_count"]]
    result.update(
        new_frame_four_field_reads=timings([
            t for r in measured for t in r["new_frame_timings"]["samples_ms"]]),
        new_frame_images_above_50ms_p95=[
            r["case_id"] for r in measured if r["new_frame_timings"]["p95_ms"] > 50],
        slowest_image_new_frame_p95_ms=max(
            (r["new_frame_timings"]["p95_ms"] for r in measured), default=None),
    )
    diagnostics = [(r["case_id"], r["changing_background_diagnostic"])
                   for r in measured if r.get("changing_background_diagnostic")]
    result["changing_background_timing_diagnostic"] = {
        "scope": "Generated diagnostic; excluded from real-original accuracy",
        "timings": timings([t for _, d in diagnostics for t in d["timings"]["samples_ms"]]),
        "images_above_50ms_p95": [i for i, d in diagnostics
                                 if d["timings"]["p95_ms"] is not None and d["timings"]["p95_ms"] > 50],
        "changed_outputs": [i for i, d in diagnostics if d["values_unchanged"] is False],
        "slowest_image_p95_ms": max((d["timings"]["p95_ms"] for _, d in diagnostics
                                     if d["timings"]["p95_ms"] is not None), default=None),
    }
    return result


def guard_result(path):
    """Generated diagnostics remain separate, but unsafe accepts block selection."""
    require(path.is_file(), "Missing guard artifact: " + path.name)
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    counts = {key: sum(int(s.get(key, 0)) for s in suites)
              for key in ("tests", "failures", "errors", "skipped")}
    require(counts["tests"] >= 60, "Incomplete legacy guard suite")
    return {**counts, "passed": counts["failures"] == counts["errors"] == counts["skipped"] == 0,
            "failed_tests": [case.get("name") for case in root.iter("testcase")
                             if case.find("failure") is not None or case.find("error") is not None],
            "sha256": sha256(path.read_bytes()).hexdigest(),
            "scope": "Generated safety diagnostics, excluded from real accuracy"}


def summarize(directory, revision, run_url):
    entries = {e["id"]: e for e in json.loads((CORPUS / "manifest.json").read_text())["screenshots"]}
    split, membership = load_split(CORPUS / "split-2026-10-17-latest.json", list(entries.values()))
    groups = group_assignments(list(entries.values()))
    paths = sorted(directory.glob("shard-*.json"))
    reports = [json.loads(p.read_text()) for p in paths]
    indexed = combine(reports, entries, membership, revision)
    guards = {v: guard_result(directory / (v + ".xml")) for v in FACTORS}
    fingerprints = reports[0]["fingerprints"]
    for name, digest in fingerprints.items():
        blob = subprocess.check_output(["git", "show", f"{revision}:{name}"], cwd=ROOT)
        if Path(name).suffix in {".py", ".json", ".md", ".txt"}:
            blob = blob.replace(b"\r\n", b"\n")
        require(sha256(blob).hexdigest() == digest, "Evaluated revision hash mismatch: " + name)
    variants = {}
    for variant, lookup in indexed.items():
        rows = [lookup[i] for i in entries]
        full = [r for r in rows if r["source_coverage"] == "full_screenshot"]
        total = card(rows, entries, groups)
        change = changes(indexed["base"], lookup)
        common = [r["case_id"] for r in full
                  if r["panel_status"] == indexed["base"][r["case_id"]]["panel_status"] == "found"]
        paired = {
            "common_detected_full_images": len(common),
            "base": timings([t for i in common for t in indexed["base"][i]["new_frame_timings"]["samples_ms"]]),
            "candidate": timings([t for i in common for t in lookup[i]["new_frame_timings"]["samples_ms"]]),
        }
        base_correct = sum(f["exact"] for r in indexed["base"].values() for f in r["fields"].values())
        benefit = (total["correct_value_and_region"] > base_correct or (
            total["correct_value_and_region"] == base_correct and bool(common)
            and paired["candidate"]["p95_ms"] <= .9 * paired["base"]["p95_ms"]))
        gates = {
            "existing_diagnostic_guards_pass": guards[variant]["passed"],
            "no_incorrect_accepts": total["incorrect_accepted"] == 0,
            "no_na_or_ambiguity_accepts": total["unsafe_na_accepts"] == total["unsafe_ambiguity_accepts"] == 0,
            "no_unscored_accepts": total["unscored_accepted"] == 0,
            "no_wrong_or_unbound_rows": total["accepted_unbound_region"] == total["accepted_region_contains_other_resource_center"] == 0,
            "controls_abstain": total["control_accepted_values"] == total["control_false_panels"] == 0,
            "no_correct_regressions": not change["regressed"],
            "consistent_repeats": not total["inconsistent_cached_outputs"],
            "each_image_static_p95_within_50ms": not total["cached_images_above_50ms_p95"],
            "each_image_new_frame_p95_within_50ms": not total["new_frame_images_above_50ms_p95"],
            "changing_background_diagnostic_p95_within_50ms": not total["changing_background_timing_diagnostic"]["images_above_50ms_p95"],
            "changing_background_diagnostic_preserves_values": not total["changing_background_timing_diagnostic"]["changed_outputs"],
            "coverage_gain_or_at_least_10_percent_paired_new_frame_p95_gain": benefit,
        }
        variants[variant] = {
            "features": FACTORS[variant], "all_originals": total,
            "all_full_screenshots": card(full, entries, groups),
            "partitions": {part: card([r for r in rows if membership[r["case_id"]] == part], entries, groups)
                           for part in ("development", "test", "reference")},
            "changes_from_baseline": change, "gates": gates, "diagnostic_guards": guards[variant],
            "paired_new_frame_latency": paired,
            "misses": [{"case_id": r["case_id"], "file": str(CORPUS.relative_to(ROOT) / entries[r["case_id"]]["file"]),
                        "partition": membership[r["case_id"]], "field": name,
                        "expected": f["expected"], "actual": f["actual"],
                        "stage": f["rejection_stage"], "detail": f["detail"],
                        "incorrect_accepted": f["incorrect_accepted"]}
                       for r in rows for name, f in r["fields"].items() if f["eligible"] and not f["exact"]],
        }
    effects = {}
    for factor in ("A", "B", "C"):
        pairs = []
        for name, factors in FACTORS.items():
            if factor in factors:
                continue
            plus = next(n for n, fs in FACTORS.items() if set(fs) == set(factors) | {factor})
            delta = changes(indexed[name], indexed[plus])
            pairs.append({"without": name, "with": plus, **delta,
                          "correct_delta": len(delta["gained"]) - len(delta["regressed"])})
        effects[factor] = pairs
    return {
        "schema": "tibiaauto.native-resource-summary", "version": 1,
        "evaluated_revision": revision, "run_url": run_url,
        "originals": len(entries), "image_variant_cases": len(entries) * len(variants),
        "partition_counts": {k: len(v) for k, v in split["partitions"].items()},
        "fingerprints": fingerprints,
        "raw_files": [{"file": p.name, "sha256": sha256(p.read_bytes()).hexdigest()} for p in paths],
        "environments": [{"shard": r["shard"], **r["environment"], "native": r["native_environment"]} for r in reports],
        "variants": variants, "paired_factor_effects": effects,
        "limitations": [
            "Historical test images were previously inspected; this is regression evidence, not untouched holdout generalization.",
            "Repeated original pixels establish neither live capture latency nor changing gameplay freshness.",
            "New Frame timings exercise fresh-capture guards but exclude acquisition and Frame copying.",
            "Case pairs share hosts; pooled quantiles describe several hosted Windows machines.",
            "Generated mutation tests are reported separately and never added to accuracy totals.",
        ],
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
    print(json.dumps({v: {"correct": r["all_originals"]["correct_value_and_region"],
                          "wrong": r["all_originals"]["incorrect_accepted"], "gates": r["gates"]}
                      for v, r in result["variants"].items()}, indent=2))
