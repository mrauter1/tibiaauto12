"""Evaluate frozen readers on every original under the current 80/20 split."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

import cv2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.benchmark_resource_techniques import full_probe  # noqa: E402
from tools.compare_resource_readers import environment, implementation_hashes, load_cases  # noqa: E402
from tools.resource_reader_experiments import VARIANTS  # noqa: E402
from tools.screenshot_split import load_split, text_digest  # noqa: E402

CORPUS = ROOT / "tests/fixtures/screenshots"
VARIANT_NAMES = ("baseline", "accelerated_fast", "accelerated_robust")
SPLIT = CORPUS / "split-2026-10-17-latest.json"


def fingerprints():
    names = (
        "tools/resource_reader_experiments.py", "tools/resource_numeric_vector.py",
        "tools/benchmark_resource_techniques.py", "tools/benchmark_latest_screenshots.py",
        "tools/screenshot_split.py", "tools/benchmark_resources.py",
        "tools/compare_resource_readers.py", "tests/fixtures/screenshots/manifest.json",
        "tests/fixtures/screenshots/split-2026-10-17-latest.json",
        "tests/fixtures/screenshots/collection-2026-10-17-latest.json",
    )
    return {name: text_digest(ROOT / name) for name in names}


def portable_implementation():
    """Fingerprint the same reader/profile content on Windows and Linux."""
    result = implementation_hashes()
    result["code_files"] = {name: text_digest(ROOT / name) for name in result["code_files"]}
    profile_root = ROOT / "profiles/paulistinha-skills-v1"
    digest = sha256()
    for path in sorted(p for p in profile_root.rglob("*") if p.is_file()):
        relative = path.relative_to(profile_root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        if path.suffix == ".json":
            payload = payload.replace(b"\r\n", b"\n")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    result["profile_bundle_sha256"] = digest.hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.shards < 1 or not 0 <= args.shard < args.shards:
        parser.error("A positive shard count and valid shard index are required")
    cv2.setNumThreads(1)
    before, implementation = fingerprints(), portable_implementation()
    manifest, cases = load_cases(CORPUS / "manifest.json", include_generated=False)
    _, membership = load_split(SPLIT, manifest["screenshots"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = args.out.with_suffix(".jsonl")
    checkpoint.write_text("", encoding="utf-8")
    selected = cases[args.shard::args.shards]
    # full_probe passes pixels only to discovery/read; annotations enter scoring later.
    result = full_probe(selected, VARIANT_NAMES, 50, 50, checkpoint)
    for technique in result["techniques"].values():
        for row in technique["cases"]:
            row["evaluation_split"] = membership[row["case_id"]]
            row["historical_manifest_split"] = row["split"]
    assert fingerprints() == before and portable_implementation() == implementation
    result.update(
        schema="tibiaauto.latest-screenshot-benchmark", version=1,
        environment=environment(), opencv_threads=cv2.getNumThreads(),
        fingerprints=before, implementation=implementation, warm_reads=50,
        shard=args.shard, shards=args.shards, reader_receives_annotations=False,
        features={v: VARIANTS[v] for v in VARIANT_NAMES}, valid=True,
        split_file=str(SPLIT.relative_to(ROOT)),
        timing_scope="One fresh reader/discovery per original/variant, first read separately, then 50 static cached four-field reads; no capture or action costs.",
    )
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Saved", args.out, flush=True)


if __name__ == "__main__":
    main()
