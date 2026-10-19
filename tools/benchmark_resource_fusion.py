"""Original-only bar/temporal ablation; generated replay is timing evidence only."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
from pathlib import Path
import subprocess
import sys
from time import perf_counter
import xml.etree.ElementTree as ET

import cv2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.perception import Frame  # noqa: E402
from tools.benchmark_native_resources import observation_signature  # noqa: E402
from tools.benchmark_resource_latency import _observation, _timings  # noqa: E402
from tools.benchmark_row_refinement import fingerprints as base_fingerprints  # noqa: E402
from tools.compare_resource_readers import (  # noqa: E402
    AdaptiveTechnique, TechniqueObservation, _acceptance_role, environment, load_cases,
    score_case, summarize,
)
from tools.screenshot_split import load_split, text_digest  # noqa: E402

CORPUS = ROOT / "tests/fixtures/screenshots"
VARIANTS = ("robust", "bar", "temporal", "combined")
SCHEMA = "tibiaauto.resource-fusion-originals"
GUARD_MODULES = (
    "tests/test_resource_bar_evidence.py", "tests/test_resource_bar_numbers.py",
    "tests/test_resource_temporal_checks.py", "tests/test_resource_fusion.py",
    "tests/test_resource_fusion_benchmark.py",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fingerprints():
    result = base_fingerprints()
    for path in sorted([*ROOT.glob("tools/resource_bar*.py"),
                        *ROOT.glob("tools/resource_temporal*.py"),
                        *ROOT.glob("tools/*resource_fusion*.py"),
                        *ROOT.glob("tests/test_resource_bar*.py"),
                        *ROOT.glob("tests/test_resource_temporal*.py"),
                        *ROOT.glob("tests/test_resource_fusion*.py")]):
        result[path.relative_to(ROOT).as_posix()] = text_digest(path)
    for name in ("reports/resource-fusion-plan-2026-10-19.json",
                 ".github/workflows/resource-fusion.yml"):
        result[name] = text_digest(ROOT / name)
    return dict(sorted(result.items()))


def probe(case, variant, warm_reads):
    from tools.resource_fusion import FusionResourceReader

    start = perf_counter()
    reader = FusionResourceReader(variant, capture_session="offline-original-probe")
    initialization_ms = (perf_counter() - start) * 1000
    start = perf_counter()
    discovery = reader.discover(case.frame, use_ocr=False)
    discovery_ms = (perf_counter() - start) * 1000
    first_ms, first_signature = None, None
    samples, background_samples, changed_pixel = [], [], None
    stable, background_stable = True, True
    fusion = {}
    if discovery.layout is None:
        observation = TechniqueObservation(
            "ambiguous" if discovery.candidates else "missing", {}, detail=discovery.detail)
    else:
        start = perf_counter()
        readings = AdaptiveTechnique._bounded(reader.read(case.frame, discovery.layout))
        first_ms = (perf_counter() - start) * 1000
        # Only this FIRST read on the original contributes to original accuracy.
        observation = _observation(readings, discovery.layout, discovery.detail)
        fusion = reader.last_diagnostics
        first_signature = observation_signature(readings)
        for iteration in range(warm_reads):
            fresh = Frame(case.frame.pixels,
                          captured_at=case.frame.captured_at + (iteration + 1) * .05,
                          source=case.frame.source)
            start = perf_counter()
            current = AdaptiveTechnique._bounded(reader.read(fresh, discovery.layout))
            samples.append((perf_counter() - start) * 1000)
            stable &= observation_signature(current) == first_signature
        boxes = [discovery.layout.bounds]
        boxes.extend(reader.geometry_evidence_boxes(case.frame, discovery.layout.anchors))
        boxes.extend(reader.additional_evidence_boxes())
        corners = [(0, case.frame.height - 1), (case.frame.width - 1, case.frame.height - 1),
                   (0, 0), (case.frame.width - 1, 0)]
        changed_pixel = next(((x, y) for x, y in corners if not any(
            left <= x < right and top <= y < bottom
            for left, top, right, bottom in boxes)), None)
        require(changed_pixel is not None, "No pixel outside reader evidence for timing guard")
        x, y = changed_pixel
        for iteration in range(20):
            pixels = case.frame.pixels.copy()
            pixels[y, x, 0] ^= 1 + iteration % 2
            fresh = Frame(pixels,
                          captured_at=case.frame.captured_at + (warm_reads + iteration + 1) * .05,
                          source=case.frame.source)
            start = perf_counter()
            current = AdaptiveTechnique._bounded(reader.read(fresh, discovery.layout))
            background_samples.append((perf_counter() - start) * 1000)
            background_stable &= observation_signature(current) == first_signature
    row = score_case(case, observation, supports_cached_layout=True)
    row.update(initialization_ms=initialization_ms, discovery_ms=discovery_ms,
               first_read_ms=first_ms, first_frame_fusion=fusion,
               new_frame_timings=_timings(samples), repeated_outputs_consistent=stable,
               background_timings=_timings(background_samples),
               background_outputs_consistent=background_stable, changed_pixel=changed_pixel)
    return row


def run(args):
    cv2.setNumThreads(1)
    before = fingerprints()
    manifest, all_cases = load_cases(CORPUS / "manifest.json", include_generated=False)
    _, membership = load_split(CORPUS / "split-2026-10-17-latest.json", manifest["screenshots"])
    cases = all_cases
    if args.ids:
        require(set(args.ids) <= {case.id for case in cases}, "Unknown case ID")
        cases = [case for case in cases if case.id in args.ids]
    requested_ids = [case.id for case in cases]
    cases = cases[args.shard::args.shards]
    output = {variant: [] for variant in VARIANTS}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = args.out.with_suffix(".jsonl")
    checkpoint.write_text("", encoding="utf-8")
    for index, case in enumerate(cases):
        shift = (index + args.shard) % len(VARIANTS)
        for variant in VARIANTS[shift:] + VARIANTS[:shift]:
            row = probe(case, variant, args.warm_reads)
            row["evaluation_split"] = membership[case.id]
            output[variant].append(row)
            with checkpoint.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"variant": variant, "row": row}) + "\n")
            print(index + 1, len(cases), variant, case.id,
                  sum(field["exact"] for field in row["fields"].values()),
                  row["new_frame_timings"]["p95_ms"], flush=True)
    require(before == fingerprints(), "Implementation or inputs changed during benchmark")
    result = dict(schema=SCHEMA, version=1, valid=True,
                  revision=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
                  fingerprints=before, environment=environment(), opencv_threads=1,
                  shard=args.shard, shards=args.shards, warm_reads=args.warm_reads,
                  budget_ms=100, requested_case_ids=requested_ids,
                  reader_receives_annotations=False,
                  timing_scope="After discovery; capture/copy excluded. Synthetic increasing timestamps on identical originals, then20 outside-evidence pixel changes. No native gameplay/freshness evidence.",
                  original_score_scope="First read of each unchanged original, fresh independent reader per variant/image",
                  techniques={v: {"cases": rows} for v, rows in output.items()})
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def run_guards(out):
    before = fingerprints()
    out.parent.mkdir(parents=True, exist_ok=True)
    xml = out.with_suffix(".xml")
    result = subprocess.run([sys.executable, "-m", "pytest", *GUARD_MODULES, "-q",
                             "--junitxml=" + str(xml)], cwd=ROOT)
    require(result.returncode == 0, "Focused guards failed")
    require(fingerprints() == before, "Implementation changed during focused guards")
    metadata = dict(schema=SCHEMA + "-guards", fingerprints=before,
                    revision=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
                    modules=list(GUARD_MODULES), xml_sha256=sha256(xml.read_bytes()).hexdigest(),
                    pytest_exit_code=result.returncode)
    out.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def summarize_shards(paths, guard_path):
    documents = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    require(bool(documents), "No shards")
    first = documents[0]
    require(len(documents) == first["shards"], "Incomplete shard set")
    require({d["shard"] for d in documents} == set(range(first["shards"])), "Duplicate or missing shard")
    current = fingerprints()
    evaluated_fingerprints = first["fingerprints"]
    changed_analysis = {name for name in set(current) | set(evaluated_fingerprints)
                        if current.get(name) != evaluated_fingerprints.get(name)}
    # Reanalyzing immutable samples may fix this report code, never the reader,
    # inference inputs, annotations, scorer or timing protocol of the recording.
    require(changed_analysis <= {"tools/benchmark_resource_fusion.py",
                                 "tests/test_resource_fusion_benchmark.py"},
            "Source/input hashes do not match this evaluation")
    for name in changed_analysis:
        blob = subprocess.check_output(["git", "show", f"{first['revision']}:{name}"], cwd=ROOT)
        require(sha256(blob.replace(b"\r\n", b"\n")).hexdigest() == evaluated_fingerprints[name],
                "Recorded analysis hash does not match its evaluated Git revision")
    manifest, cases = load_cases(CORPUS / "manifest.json", include_generated=False)
    _, membership = load_split(CORPUS / "split-2026-10-17-latest.json", manifest["screenshots"])
    expected_ids = [case.id for case in cases]
    originals = {case.id: case for case in cases}
    require(len(expected_ids) == 81, "Unexpected frozen corpus size")
    for document in documents:
        require(document.get("schema") == SCHEMA and document.get("version") == 1
                and document.get("valid") is True,
                "Invalid benchmark document")
        for key in ("revision", "fingerprints", "environment", "shards", "warm_reads", "budget_ms", "requested_case_ids"):
            require(document[key] == first[key], f"Mixed {key}")
        require(document["fingerprints"] == evaluated_fingerprints, "Mixed source/input hashes")
        require(document["warm_reads"] >= 50 and document["budget_ms"] == 100, "Wrong repeats or budget")
        require(document.get("reader_receives_annotations") is False and
                document.get("opencv_threads") == 1, "Wrong inference or threading protocol")
        require(document["requested_case_ids"] == expected_ids, "Subset evaluation cannot pass")
        require(set(document["techniques"]) == set(VARIANTS), "Incomplete variant set")
        shard_ids = expected_ids[document["shard"]::document["shards"]]
        for technique in document["techniques"].values():
            require([row["case_id"] for row in technique["cases"]] == shard_ids,
                    "Wrong case coverage/order for shard")
            for row in technique["cases"]:
                original = originals[row["case_id"]]
                require(all(row[key] == getattr(original, key) for key in (
                    "source_id", "source_sha256", "case_sha256", "transform", "data_origin", "category",
                    "source_coverage")),
                    "Row identity or source/pixel hash does not match unchanged original")
                require(row["acceptance_role"] == _acceptance_role(original)[0],
                        "Row acceptance role does not match frozen corpus classification")
    guard_metadata = json.loads(guard_path.with_suffix(".json").read_text(encoding="utf-8"))
    require(guard_metadata.get("schema") == SCHEMA + "-guards" and
            guard_metadata.get("revision") == first["revision"] and
            guard_metadata.get("fingerprints") == evaluated_fingerprints and
            guard_metadata.get("modules") == list(GUARD_MODULES) and
            guard_metadata.get("pytest_exit_code") == 0 and
            guard_metadata.get("xml_sha256") == sha256(guard_path.read_bytes()).hexdigest(),
            "Focused guard provenance does not match benchmark")
    junit = ET.parse(guard_path).getroot()
    require(all(int(suite.get(key, "0")) == 0 for suite in junit.iter("testsuite")
                for key in ("failures", "errors", "skipped")), "Focused suite reports failed or skipped guards")
    tests = list(junit.iter("testcase"))
    require(len(tests) >= 25 and not any(list(t.iter("failure")) or list(t.iter("error"))
                                       or list(t.iter("skipped")) for t in tests),
            "Incomplete or failed focused guards")
    for module in GUARD_MODULES:
        stem = Path(module).stem
        require(any(stem in test.get("classname", "").split(".") for test in tests),
                "Required focused test module did not run")
    by_variant = {v: sorted([row for d in documents for row in d["techniques"][v]["cases"]],
                           key=lambda row: expected_ids.index(row["case_id"])) for v in VARIANTS}
    baseline_correct = {(r["case_id"], f) for r in by_variant["robust"]
                        for f, value in r["fields"].items() if value["exact"]}
    cards = {}
    for variant, rows in by_variant.items():
        require(all(r["evaluation_split"] == membership[r["case_id"]] for r in rows), "Wrong split")
        metrics = summarize(rows)["aggregate"]
        controls = [row for row in rows if row["acceptance_role"].endswith("safety_control")]
        control_values = sum(field["accepted"] for row in controls for field in row["fields"].values())
        control_panels = sum(row["false_panel_acceptance"] for row in controls)
        all_samples, bg_samples, regressions, breaches, evidence = [], [], [], [], []
        for row in rows:
            if row["panel_status"] == "found":
                for key, count in (("new_frame_timings", first["warm_reads"]), ("background_timings", 20)):
                    timing = row[key]
                    require(len(timing["samples_ms"]) == count, "Missing latency samples")
                    require(all(isinstance(t, (float, int)) and not isinstance(t, bool)
                                and math.isfinite(t) and t >= 0 for t in timing["samples_ms"]),
                            "Latency samples must be finite and nonnegative")
                    calculated = _timings(timing["samples_ms"])
                    require(abs(calculated["p95_ms"] - timing["p95_ms"]) < .0001, "Wrong stored p95")
                    if calculated["p95_ms"] > 100:
                        breaches.append({"case_id": row["case_id"], "mode": key, "p95_ms": calculated["p95_ms"]})
                all_samples.extend(row["new_frame_timings"]["samples_ms"])
                bg_samples.extend(row["background_timings"]["samples_ms"])
            for field, value in row["fields"].items():
                if (row["case_id"], field) in baseline_correct and not value["exact"]:
                    regressions.append({"case_id": row["case_id"], "field": field,
                                        "detail": value["detail"]})
            if row["first_frame_fusion"]:
                evidence.append({"case_id": row["case_id"], **row["first_frame_fusion"]})
        cards[variant] = {
            "metrics": {k: metrics[k] for k in (
                "cases", "eligible_fields", "numeric_exact", "unknown", "incorrect_accepted",
                "unsafe_intentional_unknown_acceptances", "accepted_location_mismatch", "false_panel_acceptances")},
            "control_scope": {"images": len(controls), "accepted_values": control_values,
                              "false_panels": control_panels,
                              "reference_diagnostic_panel_flags": [row["case_id"] for row in rows
                                  if row["false_panel_acceptance"] and row not in controls]},
            "by_split": {s: summarize([r for r in rows if r["evaluation_split"] == s])["aggregate"]
                         for s in ("development", "test", "reference")},
            "pooled_reading_p95_ms": _timings(all_samples)["p95_ms"],
            "worst_image_reading_p95_ms": max((r["new_frame_timings"]["p95_ms"] or 0) for r in rows),
            "worst_image_background_p95_ms": max((r["background_timings"]["p95_ms"] or 0) for r in rows),
            "max_observed_read_ms": max([*all_samples, *bg_samples], default=None),
            "regressions": regressions, "budget_breaches": breaches,
            "gates": {
                "no_incorrect_or_unsafe_accepts": not any(metrics[k] for k in (
                    "incorrect_accepted", "unsafe_intentional_unknown_acceptances",
                    "accepted_location_mismatch")) and control_values == control_panels == 0,
                "preserves_baseline_correct_fields": not regressions,
                "per_image_p95_within_100ms": not breaches,
                "repeat_outputs_consistent": all(r["repeated_outputs_consistent"] and
                                                  r["background_outputs_consistent"] for r in rows),
                "focused_guards_pass": True,
            },
            "first_frame_evidence": evidence,
        }
    return dict(schema=SCHEMA + "-summary", version=1, valid=True,
                evaluated_revision=first["revision"], reading_budget_ms=100,
                fingerprints=evaluated_fingerprints, native_sequence_count=0,
                analysis_only_changes={name: {"evaluated_sha256": evaluated_fingerprints[name],
                                               "analysis_sha256": current[name]}
                                       for name in sorted(changed_analysis)},
                limitations=["Static originals test regression and latency, not temporal benefit or live freshness.",
                             "Previously inspected test split is regression evidence.",
                             "Generated policy diagnostics and native sequence acceptance must remain separate."],
                focused_guard_tests=len(tests),
                raw_shards=[{"file": p.name, "sha256": sha256(p.read_bytes()).hexdigest()} for p in paths],
                guard_xml_sha256=sha256(guard_path.read_bytes()).hexdigest(), variants=cards)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--warm-reads", type=int, default=50)
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--summarize", type=Path)
    parser.add_argument("--guards", type=Path)
    parser.add_argument("--run-guards", action="store_true")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.run_guards:
        run_guards(args.out)
    elif args.summarize:
        require(args.guards is not None, "Matching focused guard XML is required")
        result = summarize_shards(sorted(args.summarize.glob("shard-*.json")), args.guards)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    else:
        require(args.shards > 0 and 0 <= args.shard < args.shards and args.warm_reads > 0,
                "Invalid shard/repeat options")
        run(args)


if __name__ == "__main__":
    main()
