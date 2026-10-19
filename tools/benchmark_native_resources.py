"""Autonomous original-only 2^3 resource-reader comparison with frozen truth."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from time import perf_counter

import cv2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.benchmark_resource_latency import _observation, _timings  # noqa: E402
from core.perception import Frame  # noqa: E402
from tools.compare_resource_readers import (  # noqa: E402
    AdaptiveTechnique, TechniqueObservation, environment, load_cases, score_case, summarize,
)
from tools.resource_native_experiments import FACTORS, make_reader  # noqa: E402
from tools.screenshot_split import load_split, text_digest  # noqa: E402

CORPUS = ROOT / "tests/fixtures/screenshots"
SPLIT = CORPUS / "split-2026-10-17-latest.json"


def fingerprints():
    files = set(ROOT.glob("tools/resource_native*.py"))
    files.update(ROOT.glob("tools/resource_row_geometry.py"))
    files.update(ROOT.glob("tools/resource_calibrated_cache.py"))
    for name in (
        "tibia_client/adaptive.py", "tibia_client/adaptive_text.py",
        "tools/resource_reader_experiments.py", "tools/resource_numeric_vector.py",
        "tools/benchmark_native_resources.py", "tools/compare_resource_readers.py",
        "tools/benchmark_resources.py", "tools/benchmark_resource_latency.py",
        "tools/screenshot_split.py", "tools/summarize_native_resources.py",
        "tools/summarize_latest_screenshots.py", "tools/summarize_resource_techniques.py",
        "core/perception.py", "tibia_client/models.py",
        "tests/fixtures/screenshots/manifest.json",
        "tests/fixtures/screenshots/split-2026-10-17-latest.json",
        "reports/native-resource-techniques-2026-10-19-plan.json",
    ):
        files.add(ROOT / name)
    files.update(p for p in (ROOT / "profiles/paulistinha-skills-v1").rglob("*")
                 if p.is_file())
    result = {}
    for path in sorted(files, key=lambda p: p.relative_to(ROOT).as_posix()):
        key = path.relative_to(ROOT).as_posix()
        result[key] = (text_digest(path) if path.suffix in {".py", ".json", ".md", ".txt"}
                       else sha256(path.read_bytes()).hexdigest())
    return result


def native_environment():
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/verdanab.ttf"
    return {
        "platform": platform.platform(), "system": platform.system(),
        "font_present": font.is_file(),
        "font_sha256": sha256(font.read_bytes()).hexdigest() if font.is_file() else None,
        "font_name": "Verdana Bold", "font_runtime_rendered": True,
    }


def changing_background_probe(reader, case, layout, reference):
    """Generated timing diagnostic only; never score these frames as originals."""
    bounds = [layout.bounds]
    envelope = getattr(reader, "geometry_evidence_boxes", None)
    if envelope is not None:
        bounds.extend(envelope(case.frame, layout.anchors))
    corners = [(0, 0), (case.frame.width - 1, 0),
               (0, case.frame.height - 1), (case.frame.width - 1, case.frame.height - 1)]
    point = next(((x, y) for x, y in corners if not any(
        left <= x < right and top <= y < bottom for left, top, right, bottom in bounds)), None)
    samples, consistent = [], True
    if point is not None:
        x, y = point
        for iteration in range(20):
            pixels = case.frame.pixels.copy()
            pixels[y, x, 0] ^= 1 + iteration % 2
            fresh = Frame(pixels, source="generated-background-timing-only")
            start = perf_counter()
            readings = AdaptiveTechnique._bounded(reader.read(fresh, layout, use_ocr=False))
            samples.append((perf_counter() - start) * 1000)
            current = tuple((f, None if r.value is None else r.value["count"])
                            for f, r in readings.items())
            consistent &= current == reference
    return {"scope": "Generated one-pixel change outside panel/evidence; timing diagnostic only, excluded from accuracy",
            "changed_pixel": point, "timings": _timings(samples),
            "values_unchanged": consistent if samples else None}


def full_probe(cases, variants, warm_reads, budget, checkpoint, membership, shard=0):
    output = {variant: [] for variant in variants}
    for index, case in enumerate(cases):
        # Rotate execution order to reduce a fixed first/last timing bias.
        shift = (index + shard) % len(variants)
        order = variants[shift:] + variants[:shift]
        for variant in order:
            start = perf_counter()
            reader = make_reader(variant)
            init_ms = (perf_counter() - start) * 1000
            start = perf_counter()
            discovery = reader.discover(case.frame, use_ocr=False)
            discovery_ms = (perf_counter() - start) * 1000
            samples, fresh_samples, observed, readings = [], [], [], {}
            first_ms = None
            if discovery.layout:
                for iteration in range(warm_reads + 1):
                    start = perf_counter()
                    readings = AdaptiveTechnique._bounded(
                        reader.read(case.frame, discovery.layout, use_ocr=False))
                    elapsed = (perf_counter() - start) * 1000
                    if iteration == 0:
                        first_ms = elapsed
                    else:
                        samples.append(elapsed)
                    observed.append(tuple(
                        (f, None if r.value is None else r.value["count"])
                        for f, r in readings.items()))
                for _ in range(warm_reads):
                    fresh = Frame(case.frame.pixels, source="benchmark-new-frame")
                    start = perf_counter()
                    readings = AdaptiveTechnique._bounded(
                        reader.read(fresh, discovery.layout, use_ocr=False))
                    fresh_samples.append((perf_counter() - start) * 1000)
                    observed.append(tuple(
                        (f, None if r.value is None else r.value["count"])
                        for f, r in readings.items()))
                observation = _observation(readings, discovery.layout, discovery.detail)
            else:
                observation = TechniqueObservation(
                    "ambiguous" if discovery.candidates else "missing", {},
                    detail=discovery.detail)
            row = score_case(case, observation, supports_cached_layout=True)
            background = (changing_background_probe(reader, case, discovery.layout, observed[-1])
                          if discovery.layout else None)
            row.update(
                initialization_ms=init_ms, discovery_ms=discovery_ms,
                first_read_ms=first_ms, cached_timings=_timings(samples),
                new_frame_timings=_timings(fresh_samples),
                changing_background_diagnostic=background,
                cached_outputs_consistent=len(set(observed)) <= 1,
                cached_p95_budget_ms=budget,
                cached_budget_met=bool(samples) and _timings(samples)["p95_ms"] <= budget,
                evaluation_split=membership[case.id],
                historical_manifest_split=row["split"],
            )
            output[variant].append(row)
            with checkpoint.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"variant": variant, "row": row}) + "\n")
            correct = sum(f["exact"] for f in row["fields"].values())
            wrong = sum(f["incorrect_accepted"] for f in row["fields"].values())
            print(f"{index+1}/{len(cases)} {variant} {case.id} "
                  f"correct={correct} wrong={wrong} cold={discovery_ms:.1f} "
                  f"p95={row['cached_timings']['p95_ms']}", flush=True)
    return {"scope": "Original-only autonomous full-frame factorial comparison",
            "techniques": {v: {"case_ids": [r["case_id"] for r in rows], "cases": rows,
                               "summary": summarize(rows)} for v, rows in output.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variants", nargs="+", choices=tuple(FACTORS), default=list(FACTORS))
    parser.add_argument("--partition", choices=("all", "development", "test", "reference"),
                        default="all")
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--warm-reads", type=int, default=50)
    parser.add_argument("--budget-ms", type=float, default=50)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--allow-non-native", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.shards < 1 or not 0 <= args.shard < args.shards or args.warm_reads < 1:
        parser.error("Positive warm reads and valid shard index/count required")
    cv2.setNumThreads(1)
    native = native_environment()
    if ("B" in {f for v in args.variants for f in FACTORS[v]}
            and not args.allow_non_native
            and (native["system"] != "Windows" or not native["font_present"])):
        parser.error("Primary native comparison requires Windows and installed Verdana Bold")
    if "B" in {f for v in args.variants for f in FACTORS[v]} and not args.allow_non_native:
        from tools.resource_native_font import get_font
        start = perf_counter()
        bank = get_font(.8, cache=False)
        native["font_preparation_ms"] = (perf_counter() - start) * 1000
        assert bank.source_sha256 == native["font_sha256"]
        native["native_render_verified"] = True
    before = fingerprints()
    manifest, cases = load_cases(CORPUS / "manifest.json", include_generated=False)
    _, membership = load_split(SPLIT, manifest["screenshots"])
    if args.partition != "all":
        cases = [c for c in cases if membership[c.id] == args.partition]
    if args.ids:
        known = {c.id for c in cases}
        if set(args.ids) - known:
            parser.error("Unknown IDs or IDs outside requested partition")
        cases = [c for c in cases if c.id in args.ids]
    requested_ids = [c.id for c in cases]
    cases = cases[args.shard::args.shards]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = args.out.with_suffix(".jsonl")
    checkpoint.write_text("", encoding="utf-8")
    result = full_probe(cases, args.variants, args.warm_reads, args.budget_ms,
                        checkpoint, membership, args.shard)
    assert fingerprints() == before, "Inputs or implementation changed during evaluation"
    result.update(
        schema="tibiaauto.native-resource-factorial", version=1, valid=True,
        revision=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
        environment=environment(), native_environment=native, opencv_threads=1,
        fingerprints=before, warm_reads=args.warm_reads, budget_ms=args.budget_ms,
        shard=args.shard, shards=args.shards, partition=args.partition,
        requested_case_ids=requested_ids, features={v: FACTORS[v] for v in args.variants},
        reader_receives_annotations=False,
        test_policy="Previously inspected historical test partition; regression evidence only",
        timing_scope="Shared native base-font preparation separately; fresh reader/autonomous discovery, first read, static cached reads and identical pixels in new Frame objects. Capture/copy and actions excluded; no live freshness claim.",
    )
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Saved", args.out, flush=True)


if __name__ == "__main__":
    main()
