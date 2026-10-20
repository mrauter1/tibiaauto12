"""Focused cached-layout interaction trial for width pruning and consensus."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from time import perf_counter

import cv2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.benchmark_resource_techniques import MANIFEST, fingerprint  # noqa: E402
from tools.benchmark_resource_latency import _observation, _timings  # noqa: E402
from tools.compare_resource_readers import AdaptiveTechnique, environment, load_cases, score_case  # noqa: E402
from tools.resource_reader_experiments import VARIANTS, make_reader  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--warm-reads", type=int, default=30)
    args = parser.parse_args()
    cv2.setNumThreads(1)
    before = fingerprint()
    _, cases = load_cases(MANIFEST, include_generated=False)
    results = {}
    variants = ("combined_pruned_fast", "combined_pruned_robust", "accelerated_fast", "accelerated_robust")
    for case in cases:
        if case.id not in ("dfg-gladera-SSVX", "reddit-chrissykes-4ppe2dp"):
            continue
        discovered = make_reader("combined_fast").discover(case.frame, use_ocr=False)
        assert discovered.layout is not None, discovered.detail
        output = {}
        for variant in variants:
            reader = make_reader(variant)
            times, scores = [], []
            for _ in range(args.warm_reads + 1):
                start = perf_counter()
                readings = AdaptiveTechnique._bounded(reader.read(case.frame, discovered.layout, use_ocr=False))
                times.append((perf_counter() - start) * 1000)
                scores.append(score_case(case, _observation(readings, discovered.layout, discovered.detail)))
            signature = lambda row: tuple((k, f["actual"], f["exact"], f["detected_region"]) for k, f in row["fields"].items())
            assert all(signature(s) == signature(scores[0]) for s in scores)
            output[variant] = {"score":scores[-1], "first_read_ms":times[0], "cached_timings":_timings(times[1:])}
            print(case.id, variant, "correct", sum(f["exact"] for f in scores[-1]["fields"].values()), "p95", round(output[variant]["cached_timings"]["p95_ms"], 3), flush=True)
        for fast, pruned in (("combined_pruned_fast", "accelerated_fast"), ("combined_pruned_robust", "accelerated_robust")):
            assert signature(output[fast]["score"]) == signature(output[pruned]["score"])
        results[case.id] = output
    assert fingerprint() == before
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"scope":"All-four-field focused timing using a layout discovered from the same original pixels; discovery is shared because the compared change only prunes impossible numeric splits. No annotated coordinates supplied. Not the independent-discovery full benchmark.", "environment":environment(), "fingerprints":before, "features":{v:VARIANTS[v] for v in variants}, "results":results}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
