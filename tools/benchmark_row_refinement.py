"""Separate original-only comparison of robust numbers and improved row geometry.

This runner changes no production/default/factorial reader. Its three factories
are injected only into this process while the existing full-frame probe runs.
Use --guards for the new composition's generated safety checks, or --summarize
to validate all twelve completed shards and their matching guard artifact.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter

import cv2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import benchmark_native_resources as shared_probe  # noqa: E402
from tools.compare_resource_readers import environment, load_cases  # noqa: E402
from tools.resource_calibrated_cache import GeometryCacheMixin  # noqa: E402
from tools.resource_reader_experiments import ExperimentalResourceReader, VARIANTS  # noqa: E402
from tools.resource_row_geometry import StructuralRowMixin  # noqa: E402
from tools.screenshot_split import group_assignments, load_split, text_digest  # noqa: E402
from tools.summarize_native_resources import card, guard_result, require  # noqa: E402
from tools.summarize_resource_techniques import changes, timings  # noqa: E402

CORPUS = ROOT / "tests/fixtures/screenshots"
SPLIT = CORPUS / "split-2026-10-17-latest.json"
SCHEMA = "tibiaauto.row-refinement"
SHARDS = 12
FEATURES = {
    "robust": {"existing_reader": "accelerated_robust", "mixins": [],
               "numeric_features": list(VARIANTS["accelerated_robust"])},
    "rows_cache": {"existing_reader": "accelerated_fast", "mixins": ["C", "A"],
                   "numeric_features": list(VARIANTS["accelerated_fast"])},
    "rows_cache_robust": {"existing_reader": "accelerated_robust", "mixins": ["C", "A"],
                          "numeric_features": list(VARIANTS["accelerated_robust"])},
}
TEXT_SUFFIXES = {".py", ".json", ".md", ".txt", ".yml", ".yaml"}


class RowsCacheReader(GeometryCacheMixin, StructuralRowMixin, ExperimentalResourceReader):
    """The existing A+C composition; its numeric feature set is explicit."""


def make_reader(variant, **kwargs):
    config = FEATURES[variant]
    cls = RowsCacheReader if config["mixins"] else ExperimentalResourceReader
    return cls(VARIANTS[config["existing_reader"]], **kwargs)


def fingerprints():
    result = shared_probe.fingerprints()
    for name in ("tools/benchmark_row_refinement.py", ".github/workflows/row-refinement.yml",
                 "tools/check_native_resource_guards.py", "requirements.txt", "requirements-dev.txt"):
        result[name] = text_digest(ROOT / name)
    # Include unchanged original bytes, not only the manifest's claimed hashes.
    document = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    for entry in document["screenshots"]:
        path = (CORPUS / entry["file"]).resolve()
        digest = sha256(path.read_bytes()).hexdigest()
        require(digest == entry["integrity"]["sha256"], "Original screenshot hash mismatch")
        result[path.relative_to(ROOT).as_posix()] = digest
    return dict(sorted(result.items()))


def verified_native_environment():
    native = shared_probe.native_environment()
    require(native["system"] == "Windows" and native["font_present"],
            "This comparison requires Windows and installed Verdana Bold")
    from tools.resource_native_font import get_font
    start = perf_counter()
    bank = get_font(.8, cache=False)
    native["font_preparation_ms"] = (perf_counter() - start) * 1000
    require(bank.source_sha256 == native["font_sha256"], "Native font verification failed")
    native["native_render_verified"] = True
    return native


def revision():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()


def run_probe(args):
    cv2.setNumThreads(1)
    native = verified_native_environment()
    before = fingerprints()
    manifest, cases = load_cases(CORPUS / "manifest.json", include_generated=False)
    _, membership = load_split(SPLIT, manifest["screenshots"])
    if args.ids:
        require(set(args.ids) <= {case.id for case in cases}, "Unknown screenshot IDs")
        cases = [case for case in cases if case.id in args.ids]
    requested_ids = [case.id for case in cases]
    cases = cases[args.shard::args.shards]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = args.out.with_suffix(".jsonl")
    checkpoint.write_text("", encoding="utf-8")
    original_factory = shared_probe.make_reader
    try:
        shared_probe.make_reader = make_reader
        result = shared_probe.full_probe(cases, list(FEATURES), args.warm_reads, 50,
                                         checkpoint, membership, args.shard)
    finally:
        shared_probe.make_reader = original_factory
    require(fingerprints() == before, "Inputs or implementation changed during evaluation")
    result.update(
        scope="Original-only autonomous robust/rows/cache refinement comparison",
        schema=SCHEMA, version=1, valid=True, revision=revision(),
        environment=environment(), native_environment=native, opencv_threads=1,
        fingerprints=before, features=FEATURES, warm_reads=args.warm_reads, budget_ms=50,
        background_diagnostic_reads=20, shard=args.shard, shards=args.shards,
        partition="all", requested_case_ids=requested_ids, reader_receives_annotations=False,
        test_policy="Previously inspected historical test partition; regression evidence only",
        timing_scope=("Shared native font preparation separately; fresh readers and autonomous discovery. "
                      "50 static and 50 new-Frame identical-pixel repeats in the full run. Capture/copy "
                      "excluded. 20 generated outside-panel pixel changes are timing/guard diagnostics, "
                      "never original accuracy samples. No live gameplay freshness claim."),
        refinement_policy=("Retain existing robust numeric threshold consensus while independently "
                           "checking A+C row gains and the unchanged 50ms per-image p95 budget."),
    )
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Saved", args.out, flush=True)


def run_guards(out):
    """Process-local true-subclass adapter; never rewrite shared guard code."""
    from tools import check_native_resource_guards as guards
    native = verified_native_environment()
    before = fingerprints()
    out.parent.mkdir(parents=True, exist_ok=True)
    old_features, old_factory, old_argv = guards.BASE_FEATURES, guards.make_reader, sys.argv
    try:
        guards.BASE_FEATURES = VARIANTS["accelerated_robust"]
        guards.make_reader = lambda _variant, **kwargs: make_reader("rows_cache_robust", **kwargs)
        sys.argv = [old_argv[0], "--variant", "rows_cache", "--junit", str(out)]
        code = int(guards.main())
    finally:
        guards.BASE_FEATURES, guards.make_reader, sys.argv = old_features, old_factory, old_argv
    require(fingerprints() == before, "Guard inputs or implementation changed")
    metadata = {
        "schema": SCHEMA + "-guards", "version": 1, "variant": "rows_cache_robust",
        "revision": revision(), "fingerprints": before, "features": FEATURES["rows_cache_robust"],
        "native_environment": native, "exit_code": code,
        "scope": "Generated safety diagnostics, excluded from original accuracy",
        "junit_sha256": sha256(out.read_bytes()).hexdigest() if out.is_file() else None,
    }
    out.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return code


def combine(reports, entries, membership, evaluated_revision):
    """Reject partial coverage, altered inputs, mixed revisions and short runs."""
    require(bool(reports), "No completed refinement shards")
    first = reports[0]
    require(len(entries) == 81, "This frozen comparison requires all 81 originals")
    require(first["requested_case_ids"] == list(entries), "Full ordered corpus required")
    require(first["features"] == FEATURES and first["shards"] == SHARDS,
            "All three variants and twelve shards required")
    rows, seen = {variant: {} for variant in FEATURES}, set()
    for report in reports:
        require(report["schema"] == SCHEMA and report["valid"], "Invalid refinement schema/status")
        require(report["revision"] == evaluated_revision, "Revision mismatch")
        require(report["reader_receives_annotations"] is False, "Reader received annotations")
        require(report["warm_reads"] == 50 and report["opencv_threads"] == 1 and
                report["budget_ms"] == 50 and report["background_diagnostic_reads"] == 20,
                "Different repetition/thread/budget settings")
        for key in ("fingerprints", "features", "shards", "requested_case_ids", "partition"):
            require(report[key] == first[key], "Mismatched " + key)
        require(report["partition"] == "all", "Full corpus partition required")
        native = report["native_environment"]
        require(native["system"] == "Windows" and native.get("native_render_verified") is True,
                "Native Windows rendering was not verified")
        require(native["font_sha256"] == first["native_environment"]["font_sha256"],
                "Different installed font versions")
        shard = report["shard"]
        require(shard not in seen and 0 <= shard < SHARDS, "Duplicate or invalid shard")
        seen.add(shard)
        expected = list(entries)[shard::SHARDS]
        require(set(report["techniques"]) == set(FEATURES), "Missing or extra variant")
        for variant, technique in report["techniques"].items():
            require(technique["case_ids"] == expected and
                    [row["case_id"] for row in technique["cases"]] == expected,
                    "Wrong case coverage/order")
            for row in technique["cases"]:
                identifier = row["case_id"]
                require(identifier not in rows[variant], "Duplicate case")
                require(row["source_sha256"] == entries[identifier]["integrity"]["sha256"],
                        "Wrong original screenshot hash")
                require(row["transform"] == "original" and row["data_origin"] == "real",
                        "Generated input cannot enter acceptance totals")
                require(row["evaluation_split"] == membership[identifier] and
                        row["historical_manifest_split"] == entries[identifier]["split"],
                        "Split mismatch")
                count = 50 if row["panel_status"] == "found" else 0
                for name in ("cached_timings", "new_frame_timings"):
                    require(row[name]["sample_count"] == len(row[name]["samples_ms"]) == count,
                            "Missing repeated reads")
                diagnostic = row["changing_background_diagnostic"]
                if count:
                    require(diagnostic is not None, "Missing changing-background diagnostic")
                    diagnostic_count = 20 if diagnostic["changed_pixel"] is not None else 0
                    require(diagnostic["timings"]["sample_count"] ==
                            len(diagnostic["timings"]["samples_ms"]) == diagnostic_count,
                            "Incomplete changing-background diagnostic")
                else:
                    require(diagnostic is None, "Undetected layout has a timing diagnostic")
                for field in row["fields"].values():
                    require(not field["eligible"] or sum((field["exact"], field["unknown"],
                                                          field["incorrect_accepted"])) == 1,
                            "Field missing or counted twice")
                rows[variant][identifier] = row
    require(seen == set(range(SHARDS)), "Missing completed shards")
    for indexed in rows.values():
        require(set(indexed) == set(entries), "Incomplete original corpus")
        for identifier, row in indexed.items():
            require(row["case_sha256"] == rows["robust"][identifier]["case_sha256"],
                    "Variants did not receive identical pixels")
    return rows


def summarize(directory, evaluated_revision, run_url):
    entries = {entry["id"]: entry for entry in
               json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))["screenshots"]}
    split, membership = load_split(SPLIT, list(entries.values()))
    groups = group_assignments(list(entries.values()))
    paths = sorted(directory.glob("shard-*.json"))
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    indexed = combine(reports, entries, membership, evaluated_revision)
    freeze = reports[0]["fingerprints"]
    for name, digest in freeze.items():
        blob = subprocess.check_output(["git", "show", f"{evaluated_revision}:{name}"], cwd=ROOT)
        if Path(name).suffix in TEXT_SUFFIXES:
            blob = blob.replace(b"\r\n", b"\n")
        require(sha256(blob).hexdigest() == digest, "Evaluated revision hash mismatch: " + name)
    guard_path = directory / "rows_cache_robust.xml"
    guard = guard_result(guard_path)
    guard_metadata = json.loads(guard_path.with_suffix(".json").read_text(encoding="utf-8"))
    require(guard_metadata["schema"] == SCHEMA + "-guards" and
            guard_metadata["variant"] == "rows_cache_robust", "Wrong guard artifact")
    require(guard_metadata["revision"] == evaluated_revision and guard_metadata["fingerprints"] == freeze,
            "Guard revision or implementation mismatch")
    require(guard_metadata["features"] == FEATURES["rows_cache_robust"] and
            guard_metadata["junit_sha256"] == guard["sha256"], "Guard feature/hash mismatch")
    require(guard_metadata["native_environment"]["native_render_verified"] is True and
            guard_metadata["native_environment"]["font_sha256"] ==
            reports[0]["native_environment"]["font_sha256"], "Guard native environment mismatch")
    require((guard_metadata["exit_code"] == 0) == guard["passed"], "Guard exit status mismatch")
    variants = {}
    for variant, lookup in indexed.items():
        rows = [lookup[identifier] for identifier in entries]
        full = [row for row in rows if row["source_coverage"] == "full_screenshot"]
        total = card(rows, entries, groups)
        deltas = {baseline: changes(indexed[baseline], lookup) for baseline in ("robust", "rows_cache")}
        paired = {}
        for baseline in ("robust", "rows_cache"):
            common = [row["case_id"] for row in full if
                      row["panel_status"] == indexed[baseline][row["case_id"]]["panel_status"] == "found"]
            paired[baseline] = {
                "common_detected_full_images": len(common),
                "baseline": timings([t for identifier in common for t in
                                     indexed[baseline][identifier]["new_frame_timings"]["samples_ms"]]),
                "candidate": timings([t for identifier in common for t in
                                      lookup[identifier]["new_frame_timings"]["samples_ms"]]),
            }
        diagnostic = total["changing_background_timing_diagnostic"]
        gates = {
            "no_incorrect_accepts": total["incorrect_accepted"] == 0,
            "no_na_or_ambiguity_accepts": total["unsafe_na_accepts"] == total["unsafe_ambiguity_accepts"] == 0,
            "no_unscored_accepts": total["unscored_accepted"] == 0,
            "no_wrong_or_unbound_rows": total["accepted_unbound_region"] ==
                                        total["accepted_region_contains_other_resource_center"] == 0,
            "controls_abstain": total["control_accepted_values"] == total["control_false_panels"] == 0,
            "retains_robust_correct_fields": not deltas["robust"]["regressed"],
            "retains_rows_cache_correct_fields": not deltas["rows_cache"]["regressed"],
            "consistent_repeats": not total["inconsistent_cached_outputs"],
            "each_image_static_p95_within_50ms": not total["cached_images_above_50ms_p95"],
            "each_image_new_frame_p95_within_50ms": not total["new_frame_images_above_50ms_p95"],
            "changing_background_diagnostic_p95_within_50ms": not diagnostic["images_above_50ms_p95"],
            "changing_background_diagnostic_preserves_values": not diagnostic["changed_outputs"],
        }
        if variant == "rows_cache_robust":
            gates["new_composition_diagnostic_guards_pass"] = guard["passed"]
        variants[variant] = {
            "features": FEATURES[variant], "all_originals": total,
            "all_full_screenshots": card(full, entries, groups),
            "partitions": {part: card([row for row in rows if membership[row["case_id"]] == part], entries, groups)
                           for part in ("development", "test", "reference")},
            "changes_from_comparators": deltas, "paired_new_frame_latency": paired,
            "gates": gates, "diagnostic_guards": guard if variant == "rows_cache_robust" else None,
            "misses": [{"case_id": row["case_id"], "field": name,
                        "file": str(CORPUS.relative_to(ROOT) / entries[row["case_id"]]["file"]),
                        "partition": membership[row["case_id"]], "expected": field["expected"],
                        "actual": field["actual"], "stage": field["rejection_stage"],
                        "detail": field["detail"], "incorrect_accepted": field["incorrect_accepted"]}
                       for row in rows for name, field in row["fields"].items()
                       if field["eligible"] and not field["exact"]],
        }
    return {
        "schema": SCHEMA + "-summary", "version": 1, "evaluated_revision": evaluated_revision,
        "run_url": run_url, "originals": len(entries), "image_variant_cases": len(entries) * len(FEATURES),
        "partition_counts": {name: len(ids) for name, ids in split["partitions"].items()},
        "fingerprints": freeze,
        "raw_files": [{"file": path.name, "sha256": sha256(path.read_bytes()).hexdigest()} for path in paths],
        "environments": [{"shard": report["shard"], **report["environment"],
                          "native": report["native_environment"]} for report in reports],
        "variants": variants,
        "limitations": [
            "Historical test failures were inspected; these are regression results, not untouched holdout generalization.",
            "The three variants are paired on each shard host; pooled quantiles combine twelve hosted Windows machines.",
            "Static and identical-pixel new-Frame timing exclude acquisition/copy and do not establish gameplay freshness.",
            "One-pixel background changes and legacy mutations are generated diagnostics, never real accuracy samples.",
            "The new composition's complete guard suite is rerun; the two comparison readers are not independently guard-rerun here.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--guards", action="store_true")
    mode.add_argument("--summarize", type=Path, metavar="DIRECTORY")
    parser.add_argument("--revision")
    parser.add_argument("--run-url")
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--warm-reads", type=int, default=50)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=SHARDS)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.summarize:
        if not args.revision or not args.run_url:
            parser.error("--summarize requires --revision and --run-url")
        result = summarize(args.summarize, args.revision, args.run_url)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({variant: {"correct": value["all_originals"]["correct_value_and_region"],
                                    "wrong": value["all_originals"]["incorrect_accepted"],
                                    "gates": value["gates"]}
                          for variant, value in result["variants"].items()}, indent=2))
    elif args.guards:
        if args.out.suffix != ".xml":
            parser.error("--guards requires an XML --out path")
        return run_guards(args.out)
    else:
        if args.shards < 1 or not 0 <= args.shard < args.shards or args.warm_reads < 1:
            parser.error("Positive warm reads and valid shard index/count required")
        run_probe(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
