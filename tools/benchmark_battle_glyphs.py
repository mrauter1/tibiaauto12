"""Offline name-row recognition benchmark; never captures or sends game input.

Frozen human annotations select crops and supply scoring truth only. Decoders
receive RGB pixels and UI scale, never the expected name or a name dictionary.
This evaluates transcription of supplied rows, not battle-list discovery/counts.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from typing import Callable

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CORPUS = ROOT / "tests/fixtures/screenshots"
DEFAULT_LABELS = CORPUS / "visual-annotations-2026-10-22.json"
DEFAULT_FONT = ROOT / "profiles/tibia-15.33-bitmap-font"

PROTOCOL = {
    "scope": "Supplied independently annotated name-row crops; no battle detection or live input.",
    "primary": "Exact case, punctuation and whitespace equality on complete included positive rows.",
    "secondary": "Casefold and remove all whitespace, retaining punctuation (production name_key).",
    "selection": [
        "Include explicitly transcribed rows of reviewed battle panels.",
        "Exclude names in excluded_queries, invalid/empty boxes, and boxes outside the declared panel or image.",
        "partial_names is retained as a panel-level flag; other complete transcribed rows remain eligible.",
        "Never clip annotated boxes or select/exclude cases using a decoder prediction.",
        "Reviewed empty panels without partial/excluded names are separate negative controls.",
    ],
    "input": "Identical original RGB row crops, including health bars; decoder owns text segmentation.",
    "negative_input": "Declared empty battle panel with production max(1, round(20*scale)) icon inset.",
    "windows_path": "RGB min>=100 and channel range<40; production _read_lines then join_rows; internal 3x nearest resize.",
    "windows_acceptance": "Exactly one nonempty joined line without a trailing ellipsis. No confidence is supplied by WinRT.",
    "windows_scope": "Direct OCR transcription only; no BattleTextReader cache, background polling, whole-panel completeness or retry policy.",
    "latency": "perf_counter_ns around each decoder, including mask preprocessing. Corpus loading and reader initialization are separate.",
    "repeats": "First call per case plus N fresh recognition calls; no result-cache lookup is timed. Font/template initialization may be reused.",
    "ordering": "Fixed corpus order each pass; rotate method order by (pass_index + case_index) modulo method count.",
    "errors": "Exceptions counted separately from abstentions. Coverage denominator retains errors; CER excludes errors and reports its denominator.",
    "limitations": [
        "Small, single-annotator screenshot subset; capture scale/version and publisher processing are not controlled.",
        "Development-corpus observations, not a locked held-out promotion result; no production enablement implied.",
        "Negative full panels differ in size from positive rows and are reported separately.",
        "First call per case is not a cold-process measurement; only the first invocation is process-first.",
    ],
}


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def text_fingerprint(path: Path):
    data = path.read_bytes()
    return {"raw_sha256": sha256(data).hexdigest(),
            "lf_normalized_sha256": sha256(data.replace(b"\r\n", b"\n")).hexdigest()}


def verify_freeze(annotation_path, sidecar):
    expected = sidecar.read_text(encoding="utf-8").strip().split()[0]
    data = annotation_path.read_bytes()
    actual = sha256(data).hexdigest()
    mode = "byte_exact"
    if actual != expected:
        lf = data.replace(b"\r\n", b"\n")
        alternatives = {"newline_only_lf": sha256(lf).hexdigest(),
                        "newline_only_crlf": sha256(lf.replace(b"\n", b"\r\n")).hexdigest()}
        mode = next((key for key, value in alternatives.items() if value == expected), None)
        if mode is None:
            raise ValueError("Annotation hash differs from its pre-inference freeze (including newline-only equivalents)")
    return {"expected_annotation_sha256": expected, "actual_annotation_sha256": actual,
            "raw_hash_matches": actual == expected, "match_mode": mode,
            "note": "Only CRLF/LF conversion is allowed; no JSON reserialization or label edits."}


def normalized(text: str) -> str:
    return "".join(text.casefold().split())


def valid_box(box, width, height) -> bool:
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(isinstance(v, int) and not isinstance(v, bool) for v in box)
            and 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)


def contains(outer, inner) -> bool:
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and inner[2] <= outer[2] and inner[3] <= outer[3])


@dataclass
class Case:
    id: str
    screenshot_id: str
    kind: str
    expected: str | None
    pixels: np.ndarray = field(repr=False)
    scale: float = 1.0
    metadata: dict = field(default_factory=dict)


def load_corpus(annotation_path: Path, manifest_path: Path):
    """Freeze selection before any inference, validating source bytes and labels."""
    sidecar = Path(str(annotation_path) + ".sha256")
    freeze = verify_freeze(annotation_path, sidecar)
    document = json.loads(annotation_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (document.get("schema") != "tibiaauto.visual-annotations"
            or document.get("box_format") != "xyxy-half-open"):
        raise ValueError("Unsupported visual annotation schema")
    sources = {row["id"]: row for row in manifest["screenshots"]}
    annotations = document["screenshots"]
    if (len(sources) != len(manifest["screenshots"])
            or len({row["id"] for row in annotations}) != len(annotations)
            or {row["id"] for row in annotations} != set(sources)):
        raise ValueError("Manifest and annotations must cover the same unique screenshot IDs")
    cases, audit, source_hashes = [], [], {}
    for annotation in annotations:
        source = sources[annotation["id"]]
        path = (manifest_path.parent / source["file"]).resolve()
        if not path.is_relative_to(manifest_path.parent.resolve()):
            raise ValueError(f"Source path escapes corpus: {source['id']}")
        actual_hash = digest(path)
        if not actual_hash == source["integrity"]["sha256"] == annotation["source_sha256"]:
            raise ValueError(f"Source hash mismatch: {source['id']}")
        source_hashes[source["file"]] = actual_hash
        battle = annotation.get("battle_list", {})
        entry = {"screenshot_id": source["id"], "source": source["file"],
                 "source_sha256": actual_hash, "split": source.get("split"),
                 "status": battle.get("status"), "region": battle.get("region"),
                 "partial_names": battle.get("partial_names", False),
                 "excluded_queries": battle.get("excluded_queries", []),
                 "negative_queries_not_scored_as_name_rows": battle.get("negative_queries", []),
                 "rows": []}
        audit.append(entry)
        if battle.get("status") != "reviewed":
            entry.update(included=False, reason=battle.get("reason", "battle panel not reviewed"))
            continue
        scale = float(battle.get("ui_scale", 1.0))
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError(f"Invalid annotated UI scale: {source['id']}")
        with Image.open(path) as image:
            pixels = np.asarray(image.convert("RGB"))
        height, width = pixels.shape[:2]
        region = battle.get("region")
        region_valid = valid_box(region, width, height)
        for index, row in enumerate(battle.get("rows", [])):
            case_id = f"{source['id']}:row:{index}"
            box, expected = row.get("box"), row.get("name")
            reasons = []
            if not isinstance(expected, str) or not expected.strip():
                reasons.append("missing transcribed name")
            if expected in battle.get("excluded_queries", []):
                reasons.append("name listed in excluded_queries")
            if not region_valid:
                reasons.append("invalid battle region")
            if not valid_box(box, width, height):
                reasons.append("invalid row box or outside source image")
            elif region_valid and not contains(region, box):
                reasons.append("row box outside declared battle region")
            detail = {"id": case_id, "expected": expected, "box": box,
                      "included": not reasons, "exclusion_reasons": reasons}
            entry["rows"].append(detail)
            if not reasons:
                crop = pixels[box[1]:box[3], box[0]:box[2]].copy()
                cases.append(Case(case_id, source["id"], "positive_row", expected, crop, scale,
                                  {"box": box, "partial_names": entry["partial_names"],
                                   "source_sha256": actual_hash, "split": source.get("split"),
                                   "rgb_sha256": sha256(crop.tobytes()).hexdigest()}))
        entry["included"] = any(row["included"] for row in entry["rows"])
        if not battle.get("rows"):
            eligible = region_valid and not entry["partial_names"] and not entry["excluded_queries"]
            box = ([region[0] + max(1, round(20 * scale)), *region[1:]]
                   if region_valid else None)
            eligible = eligible and valid_box(box, width, height)
            entry["negative_control"] = {"included": eligible, "box": box,
                "reason": "reviewed empty panel" if eligible else "empty panel has uncertain truth or invalid geometry"}
            if eligible:
                crop = pixels[box[1]:box[3], box[0]:box[2]].copy()
                cases.append(Case(f"{source['id']}:negative-panel", source["id"],
                                  "negative_panel", None, crop, scale,
                                  {"box": box, "source_sha256": actual_hash,
                                   "split": source.get("split"),
                                   "rgb_sha256": sha256(crop.tobytes()).hexdigest()}))
                entry["included"] = True
        if not entry["included"]:
            entry["reason"] = "No eligible annotated rows or negative control"
    return cases, {"annotation_path": str(annotation_path),
                   "annotation_sha256": digest(annotation_path), "freeze_sha256": digest(sidecar),
                   "annotation_fingerprint": text_fingerprint(annotation_path), "freeze_verification": freeze,
                   "manifest_path": str(manifest_path), "manifest_sha256": digest(manifest_path),
                   "manifest_fingerprint": text_fingerprint(manifest_path),
                   "sources_sha256": source_hashes, "selection_audit": audit,
                   "annotation_limitations": document.get("limitations", [])}


def production_mask(rgb):
    values = rgb.astype(np.int16)
    return (values.min(axis=2) >= 100) & (np.ptp(values, axis=2) < 40)


def windows_decode(rgb, scale):
    from tibia_client.battle_text import _read_lines, join_rows

    rows = join_rows(_read_lines(production_mask(rgb)))
    candidate = " | ".join(label for label, _ in rows)
    accepted = len(rows) == 1 and bool(rows[0][0].strip()) and not rows[0][0].endswith(("...", "…"))
    return {"text": rows[0][0] if accepted else None, "candidate": candidate,
            "score": None, "margin": None, "boxes": [list(box) for _, box in rows],
            "rows": [{"text": label, "box": list(box)} for label, box in rows],
            "reason": "accepted single joined line" if accepted else "empty, multiple or partial joined lines"}


def glyph_decoder(reader, masked=False):
    def decode(rgb, scale):
        if masked:
            rgb = np.repeat((production_mask(rgb).astype(np.uint8) * 255)[:, :, None], 3, axis=2)
        result = reader.read_line(rgb, scale=scale)
        return {"text": result.text, "candidate": result.candidate, "score": result.score,
                "margin": result.margin, "boxes": [list(box) for box in result.boxes],
                "reason": result.reason}
    return decode


def make_readers(requested, font_dir, include_mask=False):
    readers, availability = {}, {}
    names = ["glyph", "windows-ocr"] if requested == "both" else [requested]
    if include_mask and "glyph" in names:
        names.insert(names.index("glyph") + 1, "glyph-production-mask")
    for name in names:
        started = time.perf_counter_ns()
        try:
            if name.startswith("glyph"):
                from tibia_client.glyph_text import GlyphReader
                readers[name] = glyph_decoder(GlyphReader(font_dir=font_dir), name != "glyph")
            else:
                if os.name != "nt":
                    raise RuntimeError("Windows WinRT OCR requires Windows; no substitute OCR is used")
                # Import only: the timed first call must include first recognition.
                from winrt.windows.media.ocr import OcrEngine  # noqa: F401
                from winrt.runtime import init_apartment  # noqa: F401
                readers[name] = windows_decode
            availability[name] = {"status": "available"}
        except Exception as exc:
            availability[name] = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
        availability[name]["initialization_ms"] = (time.perf_counter_ns() - started) / 1e6
    return readers, availability


def edit_distance(expected, actual):
    previous = list(range(len(actual) + 1))
    for i, left in enumerate(expected, 1):
        current = [i]
        for j, right in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def score_observation(expected, prediction, error=None):
    if error is not None:
        return {"status": "error", "exact": False, "normalized_exact": False}
    text = prediction.get("text")
    accepted = text is not None and bool(text.strip())
    result = {"status": "accepted" if accepted else "abstained",
              "exact": accepted and expected is not None and text == expected,
              "normalized_exact": accepted and expected is not None and normalized(text) == normalized(expected)}
    if expected is not None:
        result.update(character_edits=edit_distance(expected, text if accepted else ""),
                      expected_characters=len(expected),
                      normalized_character_edits=edit_distance(normalized(expected), normalized(text) if accepted else ""),
                      normalized_expected_characters=len(normalized(expected)))
    return result


def timing_summary(values):
    return {"samples": len(values), "p50_ms": float(np.percentile(values, 50)) if values else None,
            "p95_ms": float(np.percentile(values, 95)) if values else None,
            "worst_ms": max(values) if values else None}


def aggregate(observations, *, positive):
    scores = [item["score"] for item in observations]
    accepted = sum(s["status"] == "accepted" for s in scores)
    exact = sum(s["exact"] for s in scores)
    normalized_exact = sum(s["normalized_exact"] for s in scores)
    characters = sum(s.get("expected_characters", 0) for s in scores)
    normalized_characters = sum(s.get("normalized_expected_characters", 0) for s in scores)
    return {"cases": len(scores), "accepted": accepted,
            "abstentions": sum(s["status"] == "abstained" for s in scores),
            "errors": sum(s["status"] == "error" for s in scores),
            "exact": exact if positive else None,
            "normalized_exact": normalized_exact if positive else None,
            "accepted_precision": exact / accepted if accepted and positive else None,
            "normalized_accepted_precision": normalized_exact / accepted if accepted and positive else None,
            "exact_coverage": exact / len(scores) if scores and positive else None,
            "normalized_exact_coverage": normalized_exact / len(scores) if scores and positive else None,
            "false_accepted": accepted - exact if positive else accepted,
            "normalized_false_accepted": accepted - normalized_exact if positive else accepted,
            "false_acceptance_rate": accepted / len(scores) if scores and not positive else None,
            "character_error_rate": sum(s.get("character_edits", 0) for s in scores) / characters if characters else None,
            "normalized_character_error_rate": sum(s.get("normalized_character_edits", 0) for s in scores) / normalized_characters if normalized_characters else None,
            "cer_expected_characters": characters, "cer_error_cases_excluded": sum(s["status"] == "error" for s in scores),
            "successful_latency": timing_summary([o["elapsed_ms"] for o in observations if not o["error"]]),
            "error_latency": timing_summary([o["elapsed_ms"] for o in observations if o["error"]])}


def benchmark(cases: list[Case], readers: dict[str, Callable], availability: dict, repeats: int):
    """No expected label, ID, metadata or truth-derived crop changes reach decoders."""
    results = {case.id: {"id": case.id, "screenshot_id": case.screenshot_id,
                         "kind": case.kind, "expected": case.expected, "scale": case.scale,
                         "shape": list(case.pixels.shape), **case.metadata,
                         "readers": {name: [] for name in readers}} for case in cases}
    invocation_order, process_first = [], {}
    names = list(readers)
    for pass_index in range(repeats + 1):
        for case_index, case in enumerate(cases):
            offset = (pass_index + case_index) % len(names) if names else 0
            for name in names[offset:] + names[:offset]:
                # Independent input allocation outside timing prevents a decoder
                # mutating the next method's pixels, without timing crop I/O.
                pixels = case.pixels.copy()
                prediction, error = None, None
                started = time.perf_counter_ns()
                try:
                    prediction = readers[name](pixels, case.scale)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                elapsed = (time.perf_counter_ns() - started) / 1e6
                ordinal = len(invocation_order)
                observation = {"pass": pass_index, "phase": "first" if pass_index == 0 else "repeat_uncached",
                               "invocation": ordinal, "elapsed_ms": elapsed,
                               "prediction": prediction, "error": error,
                               "score": score_observation(case.expected, prediction, error)}
                results[case.id]["readers"][name].append(observation)
                invocation_order.append({"case": case.id, "reader": name, "pass": pass_index})
                process_first.setdefault(name, {"case": case.id, "elapsed_ms": elapsed, "error": error})
    summaries = {}
    for name, state in availability.items():
        if state["status"] != "available":
            summaries[name] = {**state, "primary_rows": None, "negative_panels": None,
                               "process_first_invocation": None}
            continue
        summary = {**state, "process_first_invocation": process_first.get(name)}
        for kind, label in (("positive_row", "primary_rows"), ("negative_panel", "negative_panels")):
            subset = [r for r in results.values() if r["kind"] == kind]
            first = [r["readers"][name][0] for r in subset]
            repeat = [o for r in subset for o in r["readers"][name][1:]]
            summary[label] = {"first": aggregate(first, positive=kind == "positive_row"),
                              "repeat_uncached": aggregate(repeat, positive=kind == "positive_row"),
                              "unstable_cases": [r["id"] for r in subset if any(
                                  (o["prediction"], o["error"]) != (r["readers"][name][0]["prediction"], r["readers"][name][0]["error"])
                                  for o in r["readers"][name][1:])]}
        summaries[name] = summary
    return {"readers": summaries, "cases": list(results.values()), "invocation_order": invocation_order}


def provenance(font_dir):
    def git(*args):
        try:
            return subprocess.check_output(["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    packages = {}
    for name in ("numpy", "Pillow", "opencv-python", "opencv-python-headless", "winrt-runtime", "winrt-Windows.Media.Ocr",
                 "winrt-Windows.Globalization", "winrt-Windows.Graphics.Imaging", "winrt-Windows.Storage.Streams"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    code = [Path(__file__), ROOT / "tibia_client/glyph_text.py", ROOT / "tibia_client/battle_text.py",
            ROOT / "core/paths.py", ROOT / "tibia_client/models.py"]
    assets = sorted(p for p in font_dir.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    return {"created_at": datetime.now(timezone.utc).isoformat(), "commit": git("rev-parse", "HEAD"),
            "working_tree_status": git("status", "--porcelain"),
            "python": sys.version, "platform": platform.platform(), "machine": platform.machine(),
            "processor": platform.processor(), "logical_cpu_count": os.cpu_count(),
            "packages": packages, "code_fingerprints": {str(p.relative_to(ROOT)): text_fingerprint(p) for p in code if p.exists()},
            "font_dir": str(font_dir), "font_sha256": {str(p.relative_to(font_dir)): digest(p) for p in assets},
            "font_text_fingerprints": {str(p.relative_to(font_dir)): text_fingerprint(p) for p in assets if p.suffix in (".fnt", ".json", ".txt", ".md")}}


def text_report(report):
    lines = ["Offline battle-name glyph / Windows OCR benchmark", PROTOCOL["scope"],
             f"Commit: {report['environment']['commit']}",
             f"Included: {report['counts']['positive_rows']} name rows across {report['counts']['positive_screenshots']} screenshots, "
             f"{report['counts']['negative_panels']} negative panels; "
             f"excluded annotated rows: {report['counts']['excluded_rows']}.",
             f"Annotation freeze: {report['corpus']['freeze_verification']['match_mode']}; "
             f"raw bytes match sidecar: {report['corpus']['freeze_verification']['raw_hash_matches']}.",
             f"Timing: first per case + {report['repeats']} uncached repeats; milliseconds, preprocessing included.",
             "Primary exact case/spacing; secondary casefold with all whitespace removed.",
             "CER treats abstentions as deletions; engine errors are excluded from CER and counted separately."]
    def percentage(value):
        return "N/A" if value is None else f"{value:.1%}"
    def milliseconds(value):
        return "N/A" if value is None else f"{value:.3f}"
    for name, result in report["readers"].items():
        lines.append(f"\n{name}: {result['status']}")
        if result["status"] == "unavailable":
            lines.append(result["error"])
            continue
        process_first = result["process_first_invocation"]
        lines.append(f"Initialization {milliseconds(result['initialization_ms'])} ms; process-first recognition "
                     f"{milliseconds(process_first['elapsed_ms'] if process_first else None)} ms "
                     f"on {process_first['case'] if process_first else 'N/A'}.")
        first = result["primary_rows"]["first"]
        lines.append(f"Exact {first['exact']}/{first['cases']}; accepted precision {percentage(first['accepted_precision'])}; "
                     f"coverage {percentage(first['exact_coverage'])}; CER {percentage(first['character_error_rate'])}.")
        lines.append(f"Normalized exact {first['normalized_exact']}/{first['cases']}; precision "
                     f"{percentage(first['normalized_accepted_precision'])}; coverage {percentage(first['normalized_exact_coverage'])}.")
        lines.append(f"False accepted {first['false_accepted']}; abstentions {first['abstentions']}; errors {first['errors']}.")
        for phase in ("first", "repeat_uncached"):
            timing = result["primary_rows"][phase]["successful_latency"]
            lines.append(f"{phase}: n={timing['samples']}, p50={milliseconds(timing['p50_ms'])}, "
                         f"p95={milliseconds(timing['p95_ms'])}, worst={milliseconds(timing['worst_ms'])} ms.")
        negative = result["negative_panels"]["first"]
        lines.append(f"Negative panels: false accepted {negative['false_accepted']}/{negative['cases']}, "
                     f"abstentions {negative['abstentions']}, errors {negative['errors']}.")
        lines.append(f"Unstable positive cases: {len(result['primary_rows']['unstable_cases'])}.")
    lines += ["", "Selection exclusions (fixed before inference):"]
    for screenshot in report["corpus"]["selection_audit"]:
        for row in screenshot["rows"]:
            if not row["included"]:
                lines.append(f"{row['id']}: {'; '.join(row['exclusion_reasons'])}")
    lines += ["", "Per-case predictions (first call):"]
    for case in report["cases"]:
        for name, calls in case["readers"].items():
            call = calls[0]
            prediction = call["prediction"]
            lines.append(f"{case['id']} [{name}] expected={case['expected']!r} "
                         f"text={prediction.get('text') if prediction else None!r} "
                         f"candidate={prediction.get('candidate') if prediction else None!r} "
                         f"status={call['score']['status']} error={call['error']!r}")
    lines += ["", *PROTOCOL["limitations"], "JSON preserves every selection decision, invocation, prediction, error and fingerprint."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readers", choices=("glyph", "windows-ocr", "both"), default="both")
    parser.add_argument("--repeats", type=int, default=5, help="Fresh recognition calls after each case's first call")
    parser.add_argument("--annotations", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--manifest", type=Path, default=CORPUS / "manifest.json")
    parser.add_argument("--font-dir", type=Path, default=DEFAULT_FONT)
    parser.add_argument("--glyph-mask", action="store_true", help="Also evaluate glyphs with the production neutral-pixel mask")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--text-out", type=Path)
    args = parser.parse_args(argv)
    if args.repeats < 0:
        parser.error("--repeats must be nonnegative")
    if args.glyph_mask and args.readers == "windows-ocr":
        parser.error("--glyph-mask requires a glyph reader")
    cases, corpus = load_corpus(args.annotations.resolve(), args.manifest.resolve())
    readers, availability = make_readers(args.readers, args.font_dir.resolve(), args.glyph_mask)
    report = {"schema": "tibiaauto.battle-glyph-benchmark.v1", "protocol": PROTOCOL,
              "repeats": args.repeats, "environment": provenance(args.font_dir.resolve()), "corpus": corpus,
              "counts": {"positive_rows": sum(c.kind == "positive_row" for c in cases),
                         "positive_screenshots": len({c.screenshot_id for c in cases if c.kind == "positive_row"}),
                         "negative_panels": sum(c.kind == "negative_panel" for c in cases),
                         "excluded_rows": sum(not row["included"] for s in corpus["selection_audit"] for row in s["rows"])},
              **benchmark(cases, readers, availability, args.repeats)}
    rendered = text_report(report)
    for path, content in ((args.json_out, json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"),
                          (args.text_out, rendered)):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    print(rendered, end="")
    return 0 if readers else 2


if __name__ == "__main__":
    raise SystemExit(main())
