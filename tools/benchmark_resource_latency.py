#!/usr/bin/env python3
"""Offline ResourceReader latency on preserved original full screenshots only.

This measures full-frame discovery and repeated cached algorithm reads. It does
not measure capture, scheduling, changing frames, or live-session freshness.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
import multiprocessing
from pathlib import Path
from queue import Empty
from statistics import median
import sys
from time import perf_counter
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tibia_client.adaptive import FIELDS, ResourceReader  # noqa: E402
from tibia_client.models import Quality, Reading  # noqa: E402
from tools import benchmark_resources as corpus  # noqa: E402
from tools.compare_resource_readers import (  # noqa: E402
    AdaptiveTechnique,
    BenchmarkCase,
    TechniqueObservation,
    _acceptance_role,
    _qualities,
    _reading_details,
    _reading_diagnostics,
    _reading_value,
    environment,
    implementation_hashes,
    load_cases,
    score_case,
)


SCHEMA = "tibiaauto.resource-original-latency"
SCHEMA_VERSION = 1
MIN_WARM_READS = 50
MAX_WARM_READS = 1000


def _percentile(values: Sequence[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _timings(values: Sequence[float]) -> dict[str, Any]:
    return {
        "sample_count": len(values),
        "samples_ms": list(values),
        "p50_ms": median(values) if values else None,
        "p95_ms": _percentile(values, 0.95),
        "max_ms": max(values) if values else None,
    }


def select_original_cases(cases: Sequence[BenchmarkCase]) -> tuple[list[BenchmarkCase], list[dict[str, str]]]:
    """Select reviewed acceptance originals plus real cold safety controls."""

    selected = []
    excluded = []
    for case in cases:
        role, reason = _acceptance_role(case)
        original_real_full = (
            case.id == case.source_id
            and case.transform == "original"
            and case.category == "original_full"
            and case.source_coverage == "full_screenshot"
            and case.data_origin == "real"
        )
        include = original_real_full and role in {
            "positive_acceptance",
            "negative_safety_control",
            "ambiguous_safety_control",
        }
        if include:
            selected.append(case)
        else:
            excluded.append({"case_id": case.id, "reason": reason})
    return selected, excluded


def _observation(readings: Mapping[str, Reading], layout: Any, detail: str) -> TechniqueObservation:
    return TechniqueObservation(
        "found",
        {name: _reading_value(readings.get(name)) for name in FIELDS},
        dict(layout.regions),
        _qualities(readings),
        detail=detail,
        field_details=_reading_details(readings),
        field_diagnostics=_reading_diagnostics(readings),
    )


def benchmark_case(
    case: BenchmarkCase,
    *,
    warm_reads: int,
    reading_budget_ms: float | None,
    reader_factory: Callable[[], Any] = ResourceReader,
) -> dict[str, Any]:
    """Measure one immutable original; annotations are used only after reads."""

    if not MIN_WARM_READS <= warm_reads <= MAX_WARM_READS:
        raise ValueError(f"warm_reads must be from {MIN_WARM_READS} through {MAX_WARM_READS}")
    reader = reader_factory()
    started = perf_counter()
    try:
        discovery = reader.discover(case.frame, use_ocr=False)
    except Exception as exc:
        discovery_ms = (perf_counter() - started) * 1000
        return _error_case(case, discovery_ms, exc)
    discovery_ms = (perf_counter() - started) * 1000
    role, reason = _acceptance_role(case)
    layout = discovery.layout
    if role in {"negative_safety_control", "ambiguous_safety_control"}:
        false_acceptance = layout is not None
        return {
            **_identity(case, role, reason),
            "panel_status": "found" if false_acceptance else (
                "ambiguous" if discovery.candidates else "missing"
            ),
            "discovery_ms": discovery_ms,
            "warm_read_timings": _timings([]),
            "warm_reads": [],
            "all_warm_reads_exact_and_corresponding": None,
            "false_panel_acceptance": false_acceptance,
            "accepted_read_count": 0,
            "abstention_passed": not false_acceptance,
            "reading_budget_ms": reading_budget_ms,
            "budget_met": None,
            "passed": not false_acceptance,
            "detail": discovery.detail,
            "error": None,
        }
    if layout is None:
        return {
            **_identity(case, role, reason),
            "panel_status": "ambiguous" if discovery.candidates else "missing",
            "discovery_ms": discovery_ms,
            "warm_read_timings": _timings([]),
            "warm_reads": [],
            "all_warm_reads_exact_and_corresponding": False,
            "false_panel_acceptance": False,
            "reading_budget_ms": reading_budget_ms,
            "budget_met": False if reading_budget_ms is not None else None,
            "passed": False,
            "detail": discovery.detail,
            "error": None,
        }

    samples = []
    timings = []
    for index in range(warm_reads):
        read_started = perf_counter()
        try:
            readings = AdaptiveTechnique._bounded(
                reader.read(case.frame, layout, use_ocr=False)
            )
        except Exception as exc:
            return _error_case(case, discovery_ms, exc, warm_reads=samples, timings=timings)
        elapsed_ms = (perf_counter() - read_started) * 1000
        timings.append(elapsed_ms)
        observation = _observation(readings, layout, discovery.detail)
        scored = score_case(case, observation, supports_cached_layout=True)
        fields = {
            name: {
                "expected": scored["fields"][name]["expected"],
                "actual": scored["fields"][name]["actual"],
                "exact": scored["fields"][name]["exact"],
                "location_corresponds": scored["fields"][name]["location_corresponds"],
                "detected_region": scored["fields"][name]["detected_region"],
            }
            for name in FIELDS
        }
        samples.append(
            {
                "index": index,
                "elapsed_ms": elapsed_ms,
                "values": dict(observation.values),
                "all_exact": all(item["exact"] for item in fields.values()),
                "all_rows_correspond": all(
                    item["location_corresponds"] is True for item in fields.values()
                ),
                "fields": fields,
            }
        )
    exact = all(item["all_exact"] and item["all_rows_correspond"] for item in samples)
    budget_met = (
        all(value <= reading_budget_ms for value in timings)
        if reading_budget_ms is not None else None
    )
    return {
        **_identity(case, role, reason),
        "panel_status": "found",
        "discovery_ms": discovery_ms,
        "warm_read_timings": _timings(timings),
        "warm_reads": samples,
        "all_warm_reads_exact_and_corresponding": exact,
        "false_panel_acceptance": False,
        "reading_budget_ms": reading_budget_ms,
        "budget_met": budget_met,
        "passed": exact and budget_met is not False,
        "detail": discovery.detail,
        "error": None,
    }


def _identity(case: BenchmarkCase, role: str, reason: str) -> dict[str, Any]:
    return {
        "case_id": case.id,
        "source_id": case.source_id,
        "source_sha256": case.source_sha256,
        "decoded_pixels_sha256": case.case_sha256,
        "split": case.split,
        "acceptance_role": role,
        "acceptance_reason": reason,
        "render_equivalence": {
            "status": case.render_equivalence_status,
            "evidence": case.render_equivalence_evidence,
        },
        "expected_final_unknown_fields": list(case.expected_final_unknown_fields),
        "abstention_reason": case.abstention_reason,
        "ambiguity_evidence": case.ambiguity_evidence,
        "input_kind": "preserved_original_full_screenshot",
    }


def _error_case(
    case: BenchmarkCase,
    discovery_ms: float,
    exc: Exception,
    *,
    warm_reads: list[dict[str, Any]] | None = None,
    timings: list[float] | None = None,
) -> dict[str, Any]:
    role, reason = _acceptance_role(case)
    return {
        **_identity(case, role, reason),
        "panel_status": "error",
        "discovery_ms": discovery_ms,
        "warm_read_timings": _timings(timings or []),
        "warm_reads": warm_reads or [],
        "all_warm_reads_exact_and_corresponding": False,
        "false_panel_acceptance": False,
        "reading_budget_ms": None,
        "budget_met": None,
        "passed": False,
        "detail": f"{type(exc).__name__}: {exc}",
        "error": {"type": type(exc).__name__, "detail": str(exc)},
    }


def _fingerprints() -> dict[str, Any]:
    result = implementation_hashes()
    result["latency_tool_sha256"] = sha256(Path(__file__).read_bytes()).hexdigest()
    return result


def _worker(
    manifest_path: str,
    case_id: str,
    warm_reads: int,
    reading_budget_ms: float | None,
    queue: Any,
) -> None:
    try:
        manifest = Path(manifest_path)
        manifest_sha = sha256(manifest.read_bytes()).hexdigest()
        fingerprints = _fingerprints()
        _document, cases = load_cases(manifest, include_generated=False)
        case = next(item for item in cases if item.id == case_id)
        result = benchmark_case(
            case, warm_reads=warm_reads, reading_budget_ms=reading_budget_ms
        )
        if sha256(manifest.read_bytes()).hexdigest() != manifest_sha:
            raise RuntimeError("manifest changed during case benchmark")
        if _fingerprints() != fingerprints:
            raise RuntimeError("implementation changed during case benchmark")
        message = {"ok": result, "implementation": fingerprints}
    except BaseException as exc:
        message = {"error": {"type": type(exc).__name__, "detail": str(exc)}}
    queue.put(message)
    queue.close()
    queue.join_thread()


def benchmark_case_bounded(
    manifest_path: Path,
    case_id: str,
    *,
    warm_reads: int,
    reading_budget_ms: float | None,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_worker,
        args=(str(manifest_path), case_id, warm_reads, reading_budget_ms, queue),
    )
    deadline = perf_counter() + timeout
    process.start()
    message = None
    while perf_counter() < deadline:
        try:
            message = queue.get(timeout=min(0.1, deadline - perf_counter()))
            break
        except Empty:
            if not process.is_alive():
                break
    if message is None:
        process.terminate()
        process.join(1)
        if process.is_alive():
            process.kill()
            process.join(1)
        row = {
            "case_id": case_id,
            "passed": False,
            "error": {"type": "TimeoutError", "detail": f"case exceeded {timeout:g}s"},
        }
        fingerprints = None
    else:
        process.join(max(0.0, deadline - perf_counter()))
        if process.is_alive():
            process.terminate()
            process.join(1)
        row = message.get("ok") or {
            "case_id": case_id,
            "passed": False,
            "error": message["error"],
        }
        fingerprints = message.get("implementation")
    queue.close()
    queue.join_thread()
    return row, fingerprints


def build_report(
    manifest_path: Path = corpus.DEFAULT_MANIFEST,
    *,
    warm_reads: int = MIN_WARM_READS,
    reading_budget_ms: float | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    if not MIN_WARM_READS <= warm_reads <= MAX_WARM_READS:
        raise ValueError(f"warm_reads must be from {MIN_WARM_READS} through {MAX_WARM_READS}")
    if timeout <= 0 or not math.isfinite(timeout):
        raise ValueError("timeout must be finite and positive")
    if reading_budget_ms is not None and (
        reading_budget_ms <= 0 or not math.isfinite(reading_budget_ms)
    ):
        raise ValueError("reading budget must be finite and positive")
    manifest_sha = sha256(manifest_path.read_bytes()).hexdigest()
    run_environment = environment()
    fingerprints = _fingerprints()
    _document, all_cases = load_cases(manifest_path, include_generated=False)
    selected, excluded = select_original_cases(all_cases)
    if not selected:
        raise ValueError("manifest has no eligible real original full screenshots")
    rows = []
    for case in selected:
        row, worker_fingerprints = benchmark_case_bounded(
            manifest_path,
            case.id,
            warm_reads=warm_reads,
            reading_budget_ms=reading_budget_ms,
            timeout=timeout,
        )
        if worker_fingerprints is not None and worker_fingerprints != fingerprints:
            raise RuntimeError("case workers used mixed implementations")
        role, reason = _acceptance_role(case)
        for key, value in _identity(case, role, reason).items():
            row.setdefault(key, value)
        rows.append(row)
    if sha256(manifest_path.read_bytes()).hexdigest() != manifest_sha:
        raise RuntimeError("manifest changed during benchmark")
    if _fingerprints() != fingerprints:
        raise RuntimeError("implementation changed during benchmark")
    if environment() != run_environment:
        raise RuntimeError("environment changed during benchmark")
    positive = [row for row in rows if row.get("acceptance_role") == "positive_acceptance"]
    negative = [row for row in rows if row.get("acceptance_role") == "negative_safety_control"]
    ambiguous = [row for row in rows if row.get("acceptance_role") == "ambiguous_safety_control"]
    warm_samples = [
        value
        for row in positive
        for value in row.get("warm_read_timings", {}).get("samples_ms", [])
    ]
    def discovery_for(group: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return _timings([
            row["discovery_ms"] for row in group if row.get("discovery_ms") is not None
        ])

    summary = {
        "selected_original_count": len(rows),
        "positive_original_count": len(positive),
        "negative_safety_count": len(negative),
        "ambiguous_safety_count": len(ambiguous),
        "excluded_original_count": len(excluded),
        "all_positive_reads_exact_and_corresponding": bool(positive) and all(
            row.get("all_warm_reads_exact_and_corresponding") is True for row in positive
        ),
        "negative_safety_passed": bool(negative) and all(row.get("passed") for row in negative),
        "ambiguous_safety_passed": (
            all(row.get("passed") for row in ambiguous) if ambiguous else None
        ),
        "discovery": discovery_for(rows),
        "discovery_by_role": {
            "positive_acceptance": discovery_for(positive),
            "negative_safety_control": discovery_for(negative),
            "ambiguous_safety_control": discovery_for(ambiguous),
        },
        "cached_reads": _timings(warm_samples),
        "reading_budget_ms": reading_budget_ms,
        "budget_met": (
            all(row.get("budget_met") is True for row in positive)
            if reading_budget_ms is not None else None
        ),
    }
    summary["passed"] = (
        summary["all_positive_reads_exact_and_corresponding"]
        and summary["negative_safety_passed"]
        and summary["ambiguous_safety_passed"] is not False
        and summary["budget_met"] is not False
        and all(row.get("error") is None for row in rows)
    )
    return {
        "schema": SCHEMA,
        "version": SCHEMA_VERSION,
        "scope": "offline repeated-image algorithm latency; not live capture/session latency",
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "environment": run_environment,
        "implementation": fingerprints,
        "options": {
            "warm_reads": warm_reads,
            "reading_budget_ms": reading_budget_ms,
            "timeout": timeout,
            "include_generated": False,
        },
        "selection": {
            "required": [
                "preserved original",
                "real full screenshot",
                "reviewed target_family or visually_equivalent positive, negative safety control, or multi-panel ambiguity abstention control",
            ],
            "excluded": excluded,
        },
        "cases": rows,
        "summary": summary,
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    timing = summary["cached_reads"]
    lines = [
        "# Original screenshot resource latency", "",
        f"Overall: **{'PASS' if summary['passed'] else 'FAIL'}**", "",
        "Offline algorithm timing on preserved original full screenshots. Repeated reads of the same image are not a live session and do not measure capture or input latency.",
        "",
        "| Source | Role | Discovery | Warm reads | Warm p50/p95/max | Exact + row correspondence | Budget | Result |",
        "|---|---|---:|---:|---:|:---:|:---:|:---:|",
    ]
    for row in report["cases"]:
        if row.get("error"):
            lines.append(f"| `{row['case_id']}` | — | — | — | — | — | — | `{row['error']['type']}` |")
            continue
        warm = row["warm_read_timings"]
        def ms(value: Any) -> str:
            return "—" if value is None else f"{value:.2f} ms"
        exact = row["all_warm_reads_exact_and_corresponding"]
        budget = row["budget_met"]
        lines.append(
            f"| `{row['case_id']}` | `{row['acceptance_role']}` | {ms(row['discovery_ms'])} | "
            f"{warm['sample_count']} | {ms(warm['p50_ms'])}/{ms(warm['p95_ms'])}/{ms(warm['max_ms'])} | "
            f"{'—' if exact is None else ('yes' if exact else 'no')} | "
            f"{'not declared' if budget is None else ('met' if budget else 'missed')} | "
            f"{'PASS' if row['passed'] else 'FAIL'} |"
        )
    lines.extend([
        "",
        f"Aggregate cached reads: **{timing['sample_count']}** samples; p50/p95/max "
        f"**{timing['p50_ms']:.2f}/{timing['p95_ms']:.2f}/{timing['max_ms']:.2f} ms**."
        if timing["sample_count"] else "No cached read samples were produced.",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=corpus.DEFAULT_MANIFEST)
    parser.add_argument("--warm-reads", type=int, default=MIN_WARM_READS)
    parser.add_argument("--reading-budget-ms", type=float)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args(argv)
    try:
        report = build_report(
            args.manifest,
            warm_reads=args.warm_reads,
            reading_budget_ms=args.reading_budget_ms,
            timeout=args.timeout,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    markdown = render_markdown(report)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded, encoding="utf-8")
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(markdown, encoding="utf-8")
    if not args.json_out and not args.markdown_out:
        print(markdown, end="")
    return 0 if report["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
