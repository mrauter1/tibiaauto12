"""Default resource reader: frozen-original regression and post-discovery latency.

Run twelve shards, then summarize their matching focused-guard artifact.  Only
the first unchanged original is scored for accuracy.  Replays with fresh Frame
objects and changed background pixels measure algorithm cost, not live capture.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from time import perf_counter
import xml.etree.ElementTree as ET

import cv2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.perception import Frame, validate_region  # noqa: E402
from tibia_client.adaptive import FIELDS  # noqa: E402
from tools.benchmark_resource_latency import _observation, _timings  # noqa: E402
from tools.compare_resource_readers import (  # noqa: E402
    AdaptiveTechnique, TechniqueObservation, _reading_value, environment,
    load_cases, score_case, summarize,
)
from tools.screenshot_split import load_split, text_digest  # noqa: E402

CORPUS = ROOT / "tests/fixtures/screenshots"
SCHEMA = "tibiaauto.resource-reader-originals"
SHARDS, WARM_READS, BACKGROUND_READS, BUDGET_MS = 12, 50, 20, 100
EXPECTED_CASES, EXPECTED_FIELDS, EXPECTED_CONTROLS, MIN_EXACT = 81, 239, 17, 221
TEXT_SUFFIXES = {".py", ".c", ".json", ".md", ".txt", ".yml", ".yaml"}
METRICS = ("cases", "eligible_fields", "numeric_exact", "unknown", "incorrect_accepted",
           "unsafe_intentional_unknown_acceptances", "accepted_location_mismatch", "reader_errors")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def revision():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()


def fingerprints():
    """Bind remaining code, guards, profiles, truth and actual original bytes."""
    from tools.check_resource_reader_guards import GUARD_MODULES

    names = {
        "tools/resource_reader.py", "tools/resource_panel_guard.py",
        "tools/resource_reader_experiments.py", "tools/resource_numeric_vector.py",
        "tools/resource_row_geometry.py", "tools/benchmark_resource_reader.py",
        "tools/benchmark_visual_production.py", "tests/test_visual_production_benchmark.py",
        "tools/resource_numeric_cache.py", "tools/resource_discovery_cache.py",
        "tools/build_visual_native.py", "core/native_visual_guard.py", "core/native_visual_guard.c",
        "tools/check_resource_reader_guards.py", "tests/test_resource_reader_benchmark.py",
        "tools/compare_resource_readers.py", "tools/benchmark_resources.py",
        "tools/benchmark_resource_latency.py", "tools/screenshot_split.py",
        "tibia_client/adaptive.py", "tibia_client/adaptive_text.py",
        "tibia_client/adaptive_runtime.py", "tibia_client/models.py", "tibia_client/capture.py",
        "tibia_client/resource_profiles.py", "tibia_client/visual.py",
        "tibia_client/resource_reader.py", "tests/test_resource_reader_default.py",
        "core/perception.py", "core/paths.py", "requirements.txt", "requirements-dev.txt",
        "tests/fixtures/screenshots/manifest.json",
        "tests/fixtures/screenshots/split-2026-10-17-latest.json",
        ".github/workflows/resource-reader.yml", ".github/workflows/ci.yml", *GUARD_MODULES,
    }
    files = {ROOT / name for name in names}
    files.update(path for path in (ROOT / "profiles/paulistinha-skills-v1").rglob("*")
                 if path.is_file())
    files.update(path for path in (ROOT / "tests/fixtures/resources").rglob("*")
                 if path.is_file())
    result = {path.relative_to(ROOT).as_posix(): (
        text_digest(path) if path.suffix in TEXT_SUFFIXES else sha256(path.read_bytes()).hexdigest())
        for path in sorted(files)}
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["screenshots"]:
        path = (CORPUS / entry["file"]).resolve()
        require(CORPUS.resolve() in path.parents, "Original path escapes corpus")
        digest = sha256(path.read_bytes()).hexdigest()
        require(digest == entry["integrity"]["sha256"], "Original screenshot hash mismatch")
        result[path.relative_to(ROOT).as_posix()] = digest
    return dict(sorted(result.items()))


def verify_revision(evaluated_revision, hashes):
    """Permit later reports-only commits, but never uncommitted inference code."""
    require(isinstance(evaluated_revision, str) and
            re.fullmatch(r"[0-9a-f]{40}", evaluated_revision), "Invalid evaluated revision")
    names = sorted(hashes)
    try:
        tracked = subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", evaluated_revision, "--", *names], cwd=ROOT
        ).decode().splitlines()
        status = subprocess.run(["git", "diff", "--quiet", evaluated_revision, "--", *names], cwd=ROOT)
    except subprocess.CalledProcessError as exc:
        raise ValueError("Evaluated revision is unavailable") from exc
    require(set(tracked) == set(names) and status.returncode == 0,
            "Source/input state does not match its evaluated Git revision")


def observation_signature(readings):
    return tuple((name, _reading_value(readings.get(name)), readings[name].quality.value,
                  tuple(readings[name].diagnostics.get("effective_region") or ()),
                  readings[name].diagnostics.get("numeric_eligible"),
                  readings[name].diagnostics.get("layout_fingerprint")) for name in FIELDS)


def probe(case, warm_reads=WARM_READS):
    from tibia_client.resource_reader import ResourceReader

    started = perf_counter()
    reader = ResourceReader()
    initialization_ms = (perf_counter() - started) * 1000
    started = perf_counter()
    discovery = reader.discover(case.frame, use_ocr=False)
    discovery_ms = (perf_counter() - started) * 1000
    first_ms, changed_pixel, boxes = None, None, []
    samples, background = [], []
    stable, background_stable = True, True
    if discovery.layout is None:
        observation = TechniqueObservation(
            "ambiguous" if discovery.candidates else "missing", {}, detail=discovery.detail)
    else:
        layout = discovery.layout
        started = perf_counter()
        readings = AdaptiveTechnique._bounded(reader.read(case.frame, layout, use_ocr=False))
        first_ms = (perf_counter() - started) * 1000
        observation = _observation(readings, layout, discovery.detail)
        signature = observation_signature(readings)
        for index in range(warm_reads):
            fresh = Frame(case.frame.pixels, source=case.frame.source,
                          captured_at=case.frame.captured_at + (index + 1) * .05)
            started = perf_counter()
            current = AdaptiveTechnique._bounded(reader.read(fresh, layout, use_ocr=False))
            samples.append((perf_counter() - started) * 1000)
            stable &= observation_signature(current) == signature
        boxes = [layout.bounds, *reader.geometry_evidence_boxes(case.frame, layout.anchors)]
        corners = [(0, case.frame.height - 1), (case.frame.width - 1, case.frame.height - 1),
                   (0, 0), (case.frame.width - 1, 0)]
        changed_pixel = next(((x, y) for x, y in corners if not any(
            left <= x < right and top <= y < bottom for left, top, right, bottom in boxes)), None)
        require(changed_pixel is not None, "No background pixel outside panel evidence")
        x, y = changed_pixel
        for index in range(BACKGROUND_READS):
            pixels = case.frame.pixels.copy()
            pixels[y, x, 0] ^= 1 + index % 2
            fresh = Frame(pixels, source=case.frame.source,
                          captured_at=case.frame.captured_at + (warm_reads + index + 1) * .05)
            started = perf_counter()
            current = AdaptiveTechnique._bounded(reader.read(fresh, layout, use_ocr=False))
            background.append((perf_counter() - started) * 1000)
            background_stable &= observation_signature(current) == signature
    observation = replace(observation, discovery_ms=discovery_ms,
                          cold_total_ms=discovery_ms + (first_ms or 0))
    row = score_case(case, observation, supports_cached_layout=True)
    row.update(initialization_ms=initialization_ms, first_read_ms=first_ms,
               new_frame_timings=_timings(samples), background_timings=_timings(background),
               repeated_outputs_consistent=stable, background_outputs_consistent=background_stable,
               changed_pixel=changed_pixel, evidence_boxes=boxes)
    return row


def run(args):
    cv2.setNumThreads(1)
    before, evaluated_revision = fingerprints(), revision()
    verify_revision(evaluated_revision, before)
    manifest, cases = load_cases(CORPUS / "manifest.json", include_generated=False)
    _, membership = load_split(CORPUS / "split-2026-10-17-latest.json", manifest["screenshots"])
    require(len(cases) == EXPECTED_CASES, "Unexpected frozen corpus size")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = args.out.with_suffix(".jsonl")
    checkpoint.write_text("", encoding="utf-8")
    rows = []
    for case in cases[args.shard::SHARDS]:
        row = probe(case)
        row["evaluation_split"] = membership[case.id]
        rows.append(row)
        with checkpoint.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, allow_nan=False) + "\n")
        print(case.id, sum(field["exact"] for field in row["fields"].values()),
              row["new_frame_timings"]["p95_ms"], flush=True)
    require(before == fingerprints() and evaluated_revision == revision(),
            "Implementation, inputs or revision changed during benchmark")
    result = dict(schema=SCHEMA, version=1, valid=True, reader_id="current",
                  revision=evaluated_revision, fingerprints=before, environment=environment(),
                  opencv_threads=1, shard=args.shard, shards=SHARDS, warm_reads=WARM_READS,
                  background_reads=BACKGROUND_READS, budget_ms=BUDGET_MS,
                  requested_case_ids=[case.id for case in cases], reader_receives_annotations=False,
                  original_score_scope="First read of each unchanged original; independent reader per image",
                  timing_scope="After discovery; capture/copy excluded. Fifty fresh Frame objects and twenty background pixel changes; no native freshness evidence.",
                  cases=rows)
    args.out.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _validate_guards(directory, first):
    from tools.check_resource_reader_guards import GUARD_MODULES, MIN_GUARD_TESTS

    xml, metadata_path = directory / "guards.xml", directory / "guard-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    require(metadata.get("schema") == "tibiaauto.resource-reader-guards" and
            metadata.get("version") == 1 and metadata.get("reader_id") == "current" and
            metadata.get("valid") is True and metadata.get("pytest_exit_code") == 0 and
            metadata.get("runner_exit_code") == 0 and
            metadata.get("revision") == metadata.get("revision_after") == first["revision"] and
            metadata.get("fingerprints") == metadata.get("fingerprints_after") == first["fingerprints"] and
            metadata.get("modules") == list(GUARD_MODULES) and
            metadata.get("xml_sha256") == sha256(xml.read_bytes()).hexdigest(),
            "Focused guard provenance does not match benchmark")
    root = ET.parse(xml).getroot()
    tests = list(root.iter("testcase"))
    counts = {"tests": len(tests), **{key: sum(len(list(t.iter(tag))) for t in tests)
              for key, tag in (("failures", "failure"), ("errors", "error"), ("skipped", "skipped"))}}
    require(len(tests) >= MIN_GUARD_TESTS and metadata.get("counts") == counts and
            all(counts[key] == 0 for key in ("failures", "errors", "skipped")) and
            all(int(suite.get(key, "0")) == 0 for suite in root.iter("testsuite")
                for key in ("failures", "errors", "skipped")), "Incomplete or failed focused guards")
    nodeids = metadata.get("selected_nodeids", [])
    require(len(nodeids) == len(set(nodeids)) == len(tests) and
            metadata.get("selected_nodeids_sha256") == sha256(
                json.dumps(nodeids, separators=(",", ":")).encode()).hexdigest(),
            "Focused guard collection does not match executed tests")
    expected = set()
    for nodeid in nodeids:
        # Pytest keeps the complete parameter ID in the test name, including
        # literal "::" that must not be mistaken for a class boundary.
        address, bracket, parameters = nodeid.partition("[")
        parts = address.split("::")
        require(parts[0] in GUARD_MODULES and len(parts) >= 2, "Unexpected focused guard test")
        expected.add((".".join([parts[0][:-3].replace("/", "."), *parts[1:-1]]),
                      parts[-1] + bracket + parameters))
    require(expected == {(t.get("classname"), t.get("name")) for t in tests},
            "Focused guard test identities do not match collection")
    require(all(any(node.startswith(module + "::") for node in nodeids) for module in GUARD_MODULES),
            "Required focused guard module did not run")
    return len(tests), {p.name: sha256(p.read_bytes()).hexdigest() for p in (xml, metadata_path)}


def _validated_row(row, case, membership):
    require(set(row.get("fields", {})) == set(FIELDS), "Missing or extra resource fields")
    found = row["panel_status"] == "found"
    values = {name: field["actual"] for name, field in row["fields"].items()}
    require(all(value is None or type(value) is int and value >= 0 for value in values.values()),
            "Invalid recorded numeric reading")
    require(found or all(value is None for value in values.values()), "Values without a discovered panel")
    observation = TechniqueObservation(
        row["panel_status"], values if found else {},
        {name: field["detected_region"] for name, field in row["fields"].items()
         if field["detected_region"] is not None} if found else {},
        {name: field["quality"] for name, field in row["fields"].items()},
        cold_total_ms=row["cold_total_ms"], discovery_ms=row["discovery_ms"],
        detail=row["detail"],
        field_details={name: field["detail"] for name, field in row["fields"].items()},
        field_diagnostics={name: field["diagnostics"] for name, field in row["fields"].items()})
    scored = score_case(case, observation, supports_cached_layout=True)
    require(all(json.dumps(row.get(key), sort_keys=True, allow_nan=False) ==
                json.dumps(value, sort_keys=True, allow_nan=False) for key, value in scored.items()),
            "Recorded score, identity or source hashes disagree with frozen original")
    require(row["evaluation_split"] == membership[case.id], "Wrong source split")
    for mode, count in (("new_frame_timings", WARM_READS), ("background_timings", BACKGROUND_READS)):
        timing = row[mode]
        samples = timing["samples_ms"]
        require(len(samples) == (count if found else 0), "Missing or unexpected latency samples")
        require(all(type(value) in (int, float) and math.isfinite(value) and value >= 0
                    for value in samples), "Latency samples must be finite and nonnegative")
        require(timing == _timings(samples), "Forged latency statistics")
    require(all(type(row[key]) is bool for key in
                ("repeated_outputs_consistent", "background_outputs_consistent")), "Invalid repeat verdict")
    if found:
        require(row["evidence_boxes"] and row["changed_pixel"] is not None, "Missing background evidence")
        boxes = [validate_region(box, case.frame.size) for box in row["evidence_boxes"]]
        x, y = row["changed_pixel"]
        require(type(x) is int and type(y) is int and 0 <= x < case.frame.width and
                0 <= y < case.frame.height and not any(
                    left <= x < right and top <= y < bottom for left, top, right, bottom in boxes),
                "Changed pixel overlaps panel evidence")
    else:
        require(row["first_read_ms"] is None and row["changed_pixel"] is None and
                not row["evidence_boxes"], "Undiscovered panel has reading evidence")
    for key in ("initialization_ms", "discovery_ms", "cold_total_ms", "first_read_ms"):
        value = row[key]
        require((key == "first_read_ms" and not found and value is None) or
                type(value) in (int, float) and math.isfinite(value) and value >= 0,
                "Invalid discovery or first-read timing")
    return row


def summarize_shards(paths, guard_directory):
    documents = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    require(len(documents) == SHARDS and {d["shard"] for d in documents} == set(range(SHARDS)),
            "Incomplete, duplicate or invalid shard set")
    first = documents[0]
    current = fingerprints()
    require(first["fingerprints"] == current, "Source/input hashes do not match evaluation")
    verify_revision(first["revision"], current)
    manifest, cases = load_cases(CORPUS / "manifest.json", include_generated=False)
    _, membership = load_split(CORPUS / "split-2026-10-17-latest.json", manifest["screenshots"])
    require(len(cases) == EXPECTED_CASES, "Unexpected frozen corpus size")
    expected_ids = [case.id for case in cases]
    rows = []
    for document in documents:
        require(document.get("schema") == SCHEMA and document.get("version") == 1 and
                document.get("reader_id") == "current" and document.get("valid") is True,
                "Invalid benchmark document")
        for key in ("revision", "fingerprints", "environment"):
            require(document[key] == first[key], f"Mixed {key}")
        require(document["shards"] == SHARDS and document["warm_reads"] == WARM_READS and
                document["background_reads"] == BACKGROUND_READS and document["budget_ms"] == BUDGET_MS and
                document.get("opencv_threads") == 1 and document.get("reader_receives_annotations") is False,
                "Wrong evaluation protocol")
        require(document["requested_case_ids"] == expected_ids, "Subset evaluation cannot pass")
        shard_cases = cases[document["shard"]::SHARDS]
        require([row["case_id"] for row in document["cases"]] == [case.id for case in shard_cases],
                "Wrong shard image coverage or order")
        rows.extend(_validated_row(row, case, membership)
                    for row, case in zip(document["cases"], shard_cases))
    guard_count, guard_hashes = _validate_guards(guard_directory, first)
    rows.sort(key=lambda row: expected_ids.index(row["case_id"]))
    metrics = summarize(rows)["aggregate"]
    controls = [row for row in rows if row["acceptance_role"].endswith("safety_control")]
    require(metrics["eligible_fields"] == EXPECTED_FIELDS and len(controls) == EXPECTED_CONTROLS,
            "Frozen eligibility or control denominator changed")
    control_values = sum(field["accepted"] for row in controls for field in row["fields"].values())
    control_panels = sum(row["false_panel_acceptance"] for row in controls)
    breaches = [{"case_id": row["case_id"], "mode": mode, "p95_ms": row[mode]["p95_ms"]}
                for row in rows for mode in ("new_frame_timings", "background_timings")
                if row[mode]["p95_ms"] is not None and row[mode]["p95_ms"] > BUDGET_MS]
    gates = {
        "accuracy_floor_preserved": metrics["numeric_exact"] >= MIN_EXACT and metrics["unknown"] <= 18,
        "no_wrong_unsafe_or_misassociated_values": not any(metrics[key] for key in
            ("incorrect_accepted", "unsafe_intentional_unknown_acceptances", "accepted_location_mismatch", "reader_errors")),
        "all_safety_controls_abstain": control_values == control_panels == 0,
        "per_image_p95_within_100ms": not breaches,
        "repeat_outputs_consistent": all(row["repeated_outputs_consistent"] and
                                         row["background_outputs_consistent"] for row in rows),
        "focused_guards_pass": True,
    }
    latency = {}
    for mode in ("new_frame_timings", "background_timings"):
        samples = [sample for row in rows for sample in row[mode]["samples_ms"]]
        latency[mode] = {"pooled_p95_ms": _timings(samples)["p95_ms"],
                         "worst_image_p95_ms": max((row[mode]["p95_ms"] or 0) for row in rows),
                         "max_observed_ms": max(samples, default=None)}
    return dict(schema=SCHEMA + "-summary", version=1, valid=True, passed=all(gates.values()),
                reader_id="current", evaluated_revision=first["revision"], fingerprints=current,
                environment=first["environment"], metrics={key: metrics[key] for key in METRICS},
                by_split={split: {key: value for key, value in summarize([
                    row for row in rows if row["evaluation_split"] == split])["aggregate"].items()
                    if key in METRICS} for split in ("development", "test", "reference")},
                controls={"images": len(controls), "accepted_values": control_values, "false_panels": control_panels},
                gates=gates, latency=latency, budget_breaches=breaches, reading_budget_ms=BUDGET_MS,
                focused_guard_tests=guard_count, guard_artifacts=guard_hashes,
                raw_shards=[{"file": path.name, "sha256": sha256(path.read_bytes()).hexdigest()} for path in paths],
                native_sequence_count=0, limitations=[
                    "First-read original accuracy; repeats are offline algorithm latency, not live freshness.",
                    "The previously inspected test split is regression evidence.",
                    "The 221-field aggregate floor does not replace a per-field comparison with the historical artifact."])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=SHARDS)
    parser.add_argument("--warm-reads", type=int, default=WARM_READS)
    parser.add_argument("--summarize", type=Path)
    parser.add_argument("--guards", type=Path)
    parser.add_argument("--run-guards", action="store_true")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.run_guards:
        from tools.check_resource_reader_guards import run_guards
        return run_guards(out_dir=args.out)
    if args.summarize:
        require(args.guards is not None, "Matching guard directory is required")
        result = summarize_shards(sorted(args.summarize.glob("shard-*.json")), args.guards)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        return 0 if result["passed"] else 1
    require(args.shards == SHARDS and 0 <= args.shard < SHARDS and args.warm_reads == WARM_READS,
            "Protocol requires twelve shards and fifty fresh-frame reads")
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
