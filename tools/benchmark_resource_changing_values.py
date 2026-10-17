"""Synthetic current-frame/cache diagnostic, separate from original-corpus accuracy."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from time import perf_counter

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.perception import Frame  # noqa: E402
from tools.resource_reader_experiments import make_reader  # noqa: E402
from tools.summarize_resource_techniques import timings  # noqa: E402


def probe(variant):
    reader = make_reader(variant)
    with Image.open(ROOT / "tests/fixtures/resources/paulistinha-skills.png") as image:
        original = Frame(np.asarray(image.convert("RGB")))
    discovery = reader.discover(original, use_ocr=False)
    assert discovery.layout is not None, discovery.detail
    left, top, right, bottom = discovery.layout.regions["resource_soul"]
    glyphs = {}
    for digit in "0123456789":
        with Image.open(ROOT / f"profiles/paulistinha-skills-v1/digits/{digit}.png") as image:
            glyphs[digit] = np.asarray(image.convert("RGB"))
    results = {}
    for mode in ("bounded_glyph_cache", "clear_glyph_rank_cache_each_frame"):
        samples, errors = [], []
        for index in range(80):
            value = (index * 17 + 7) % 200
            pixels = original.pixels.copy()
            pixels[top:bottom, left:right] = 72
            shapes = [glyphs[digit] for digit in str(value)]
            x = right - 4 - sum(g.shape[1] + 2 for g in shapes)
            for glyph in shapes:
                height, width = glyph.shape[:2]
                pixels[top + 2:top + 2 + height, x:x + width] = glyph
                x += width + 2
            numbers = reader.number_reader
            if mode.startswith("clear") and hasattr(numbers, "_rank_key"):
                numbers._rank_key.cache_clear()
            frame = Frame(pixels)
            start = perf_counter()
            readings = reader.read(frame, discovery.layout, use_ocr=False)
            samples.append((perf_counter() - start) * 1000)
            expected = {"resource_health":710, "resource_mana":1085,
                        "resource_soul":value, "resource_capacity":192}
            for field, count in expected.items():
                actual = readings[field].value
                if actual is None or actual["count"] != count:
                    errors.append({"frame":index, "field":field, "expected":count,
                                   "actual":actual, "detail":readings[field].detail})
        results[mode] = {"frames":80, "fields":320, "errors":errors,
                         "correct":320 - len(errors),
                         "unknown":sum(e["actual"] is None for e in errors),
                         "incorrect":sum(e["actual"] is not None for e in errors),
                         "all_four_field_read_ms":timings(samples)}
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    cv2.setNumThreads(1)
    results = {v:probe(v) for v in ("baseline", "glyph_cache", "combined_fast", "combined_robust")}
    for modes in results.values():
        for mode, result in modes.items():
            result["rejections_match_baseline"] = result["errors"] == results["baseline"][mode]["errors"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"scope":__doc__, "note":"Prebuilt synthetic frames at one known scale; excludes capture, rendering and discovery. Some profile-pasted digit spacing is rejected by the baseline column guard. Keep all frames and compare rejection sets; this is a freshness/equivalence diagnostic, not a claim of complete numeric coverage. Never pooled into original-corpus accuracy/latency.", "variants":results}, indent=2) + "\n", encoding="utf-8")
    for variant, modes in results.items():
        for mode, result in modes.items():
            print(variant, mode, "errors", len(result["errors"]), result["all_four_field_read_ms"])
    return int(any(r["incorrect"] or not r["rejections_match_baseline"] for modes in results.values() for r in modes.values()))


if __name__ == "__main__":
    raise SystemExit(main())
