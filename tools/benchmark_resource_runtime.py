#!/usr/bin/env python3
"""Run the historical artificial relocation tracker diagnostic.

Inputs are every preserved positive full screenshot in the validated corpus.
Relocation frames come from the corpus' deterministic generated-case
factory.  Ground-truth boxes and values are used only after inference for
selection and scoring; tracker discovery receives the complete frame.

This is not an acceptance benchmark. Prefer tools/benchmark_resource_latency.py
and reviewed native-session replay for current latency and recovery evidence.
"""

from __future__ import annotations

import argparse
from functools import lru_cache
from hashlib import sha256
from math import ceil
import json
from pathlib import Path
from statistics import median
import sys
from time import perf_counter, sleep
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tibia_client.adaptive import FIELDS  # noqa: E402
from tibia_client.adaptive_runtime import AdaptiveResourceTracker  # noqa: E402
from tibia_client.models import Quality, Reading, Sensor, SensorKind  # noqa: E402
from tibia_client.visual import AdaptiveVisual, VisualProfile  # noqa: E402
from tools import benchmark_resources as corpus  # noqa: E402
from tools.compare_resource_readers import environment, implementation_hashes  # noqa: E402


SCHEMA = "tibiaauto.adaptive-runtime-benchmark"
SCHEMA_VERSION = 1


@lru_cache(maxsize=1)
def _runtime_profile() -> VisualProfile:
    """Compile adaptive metadata through the same public profile API as the SDK."""

    root = ROOT / "profiles/paulistinha-skills-v1"
    document = json.loads((root / "profile.json").read_text(encoding="utf-8"))
    visual = {}
    for field in FIELDS:
        detector = dict(document["resources"][field]["detector"])
        detector["adaptive"] = {
            "reader": "paulistinha-skills-v1",
            "field": field,
        }
        visual[field] = detector
    return VisualProfile(visual, base_path=root)


def _percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile needs at least one value")
    return ordered[max(0, ceil(len(ordered) * percent / 100) - 1)]


def _values(
    readings: Mapping[Sensor, Reading], sensors: Mapping[str, Sensor]
) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for field, sensor in sensors.items():
        reading = readings.get(sensor)
        value = None if reading is None else reading.value
        count = value.get("count") if isinstance(value, Mapping) else None
        result[field] = count if reading is not None and reading.quality is Quality.KNOWN and type(count) is int else None
    return result


def _revision(
    readings: Mapping[Sensor, Reading], sensors: Mapping[str, Sensor]
) -> str | None:
    revisions = {
        readings[sensor].diagnostics.get("layout_revision")
        for sensor in sensors.values()
        if sensor in readings
        and isinstance(readings[sensor].diagnostics.get("layout_revision"), str)
    }
    return next(iter(revisions)) if len(revisions) == 1 else None


def _all_known(
    readings: Mapping[Sensor, Reading], sensors: Mapping[str, Sensor]
) -> bool:
    return all(
        sensor in readings and readings[sensor].quality is Quality.KNOWN
        for sensor in sensors.values()
    )


def _wait_all_known(
    tracker: AdaptiveResourceTracker,
    frame,
    requests: Mapping[Sensor, AdaptiveVisual],
    sensors: Mapping[str, Sensor],
    *,
    initial: Mapping[Sensor, Reading],
    started: float,
    timeout: float,
    poll_interval: float,
) -> tuple[Mapping[Sensor, Reading], float, list[dict[str, int | None]]]:
    readings = initial
    polls = [_values(readings, sensors)]
    deadline = started + timeout
    while True:
        if _all_known(readings, sensors):
            return readings, (perf_counter() - started) * 1000, polls
        if perf_counter() >= deadline:
            unknown = [
                field
                for field, sensor in sensors.items()
                if sensor not in readings or readings[sensor].quality is not Quality.KNOWN
            ]
            raise TimeoutError(
                f"all-known adaptive readings not reached within {timeout:.1f}s; "
                f"unknown fields: {', '.join(unknown) or 'none'}"
            )
        sleep(poll_interval)
        readings = tracker.read(frame, requests)
        polls.append(_values(readings, sensors))


def _incorrect_acceptances(
    polls: list[Mapping[str, int | None]], expected: Mapping[str, int]
) -> list[dict[str, Any]]:
    """Score accepted poll values after inference has completed."""

    return [
        {
            "poll": index,
            "field": field,
            "expected": expected[field],
            "actual": actual,
        }
        for index, values in enumerate(polls)
        for field, actual in values.items()
        if actual is not None and actual != expected[field]
    ]


def _measure_hot_reads(
    tracker: AdaptiveResourceTracker,
    frame,
    requests: Mapping[Sensor, AdaptiveVisual],
    sensors: Mapping[str, Sensor],
    count: int,
) -> tuple[Mapping[Sensor, Reading], list[float], list[dict[str, Any]]]:
    timings: list[float] = []
    observations: list[dict[str, Any]] = []
    readings: Mapping[Sensor, Reading] = {}
    for _ in range(count):
        hot_started = perf_counter()
        readings = tracker.read(frame, requests)
        elapsed = (perf_counter() - hot_started) * 1000
        timings.append(elapsed)
        observations.append(
            {
                "elapsed_ms": elapsed,
                "values": _values(readings, sensors),
                "revision": _revision(readings, sensors),
            }
        )
    return readings, timings, observations


def _runtime_cases(manifest_path: Path):
    _document, entries = corpus.load_manifest(manifest_path)
    positive_full = {
        entry["id"]: entry
        for entry in entries
        if entry["coverage"] == "full_screenshot"
        and entry["classification"] != "negative"
        and entry["expected_discovery"]
        and not entry.get("expected_final_unknown_fields")
        and entry.get("data_origin") == "real"
        and entry.get("asset_fidelity") in {"source_original", "source_transcode"}
        and entry.get("render_equivalence", {}).get("status")
        in {"target_family", "visually_equivalent"}
        and entry.get("render_equivalence", {}).get("evidence", "").strip()
    }
    generated = {
        case.source_id: case
        for case in corpus.generated_benchmark_cases(entries, manifest_path)
        if case.transform == "cached-relocation" and case.source_id in positive_full
    }
    if not positive_full or set(generated) != set(positive_full):
        raise ValueError(
            "runtime benchmark requires every positive full screenshot to have one relocation case"
        )
    return [(positive_full[key], generated[key]) for key in positive_full]


def benchmark_case(
    entry: Mapping[str, Any],
    case,
    *,
    hot_reads: int,
    timeout: float,
    poll_interval: float,
) -> dict[str, Any]:
    if case.prior_frame is None:
        raise ValueError(f"{case.id}: relocation case has no prior frame")
    # Full-frame regions provide configuration shape without passing annotated
    # panel/value ROIs into discovery.
    sensors = {
        field: Sensor(
            SensorKind.VISUAL,
            (0, 0, case.prior_frame.width, case.prior_frame.height),
            field,
        )
        for field in FIELDS
    }
    profile = _runtime_profile()
    requests = {
        sensors[field]: profile[field].adaptive
        for field in FIELDS
    }
    if any(not isinstance(metadata, AdaptiveVisual) for metadata in requests.values()):
        raise RuntimeError("runtime profile did not compile adaptive metadata")
    expected_start = {
        field: int(entry["labels"][field]["value"]) for field in FIELDS
    }
    expected_final = {
        field: int(case.labels[field]["value"]) for field in FIELDS
    }
    tracker = AdaptiveResourceTracker()
    try:
        started = perf_counter()
        first = tracker.read(case.prior_frame, requests)
        first_start_unknown = all(
            reading.quality is Quality.UNKNOWN for reading in first.values()
        )
        startup_readings, startup_ms, startup_polls = _wait_all_known(
            tracker,
            case.prior_frame,
            requests,
            sensors,
            initial=first,
            started=started,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        startup_incorrect = _incorrect_acceptances(startup_polls, expected_start)
        startup_revision = _revision(startup_readings, sensors)

        invalidated_at = perf_counter()
        invalidated = tracker.read(case.frame, requests)
        first_invalidated_unknown = all(
            reading.quality is Quality.UNKNOWN for reading in invalidated.values()
        )
        recovered, recovery_ms, recovery_polls = _wait_all_known(
            tracker,
            case.frame,
            requests,
            sensors,
            initial=invalidated,
            started=invalidated_at,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        recovery_incorrect = _incorrect_acceptances(recovery_polls, expected_final)
        recovery_revision = _revision(recovered, sensors)

        final, timings, hot_observations = _measure_hot_reads(
            tracker, case.frame, requests, sensors, hot_reads
        )
        hot_incorrect = _incorrect_acceptances(
            [observation["values"] for observation in hot_observations],
            expected_final,
        )
        final_values = _values(final, sensors)
        return {
            "id": case.id,
            "source_id": case.source_id,
            "source_sha256": case.source_sha256,
            "relocated_case_sha256": case.case_sha256,
            "split": case.split,
            "first_start_read_unknown": first_start_unknown,
            "first_invalidated_read_unknown": first_invalidated_unknown,
            "startup_ms": startup_ms,
            "recovery_ms": recovery_ms,
            "startup_poll_count": len(startup_polls),
            "recovery_poll_count": len(recovery_polls),
            "startup_incorrect_acceptances": startup_incorrect,
            "recovery_incorrect_acceptances": recovery_incorrect,
            "hot_incorrect_acceptances": hot_incorrect,
            "any_incorrect_accepted_poll": bool(
                startup_incorrect or recovery_incorrect or hot_incorrect
            ),
            "hot_read_ms": {
                "sample_count": hot_reads,
                "samples": timings,
                "p50": median(timings),
                "p95": _percentile(timings, 95),
                "max": max(timings),
            },
            "hot_observations": hot_observations,
            "startup_revision": startup_revision,
            "recovery_revision": recovery_revision,
            "revision_changed": bool(
                startup_revision
                and recovery_revision
                and startup_revision != recovery_revision
            ),
            "expected_final_values": expected_final,
            "final_values": final_values,
            "exact_final_values": final_values == expected_final,
        }
    finally:
        tracker.close()


def _safety_passed(rows: list[Mapping[str, Any]]) -> bool:
    return all(
        row.get("error") is None
        and row["first_start_read_unknown"]
        and row["first_invalidated_read_unknown"]
        and not row["any_incorrect_accepted_poll"]
        and row["revision_changed"]
        and row["exact_final_values"]
        for row in rows
    )


def _error_row(entry: Mapping[str, Any], case, exc: Exception) -> dict[str, Any]:
    return {
        "id": case.id,
        "source_id": case.source_id,
        "source_sha256": case.source_sha256,
        "relocated_case_sha256": case.case_sha256,
        "split": case.split,
        "error": {"type": type(exc).__name__, "detail": str(exc)},
        "first_start_read_unknown": False,
        "first_invalidated_read_unknown": False,
        "startup_ms": None,
        "recovery_ms": None,
        "startup_poll_count": 0,
        "recovery_poll_count": 0,
        "startup_incorrect_acceptances": [],
        "recovery_incorrect_acceptances": [],
        "hot_incorrect_acceptances": [],
        "any_incorrect_accepted_poll": False,
        "hot_read_ms": {
            "sample_count": 0,
            "samples": [],
            "p50": None,
            "p95": None,
            "max": None,
        },
        "hot_observations": [],
        "startup_revision": None,
        "recovery_revision": None,
        "revision_changed": False,
        "expected_final_values": {
            field: entry["labels"][field]["value"] for field in FIELDS
        },
        "final_values": {},
        "exact_final_values": False,
    }


def _run_case_rows(
    pairs,
    *,
    hot_reads: int,
    timeout: float,
    poll_interval: float,
) -> list[dict[str, Any]]:
    rows = []
    for entry, case in pairs:
        try:
            row = benchmark_case(
                entry,
                case,
                hot_reads=hot_reads,
                timeout=timeout,
                poll_interval=poll_interval,
            )
            row["error"] = None
        except Exception as exc:
            row = _error_row(entry, case, exc)
        rows.append(row)
    return rows


def _timing_summary(values: list[float]) -> tuple[float | None, float | None]:
    return (median(values), max(values)) if values else (None, None)


def build_report(
    manifest_path: Path = corpus.DEFAULT_MANIFEST,
    *,
    hot_reads: int = 20,
    timeout: float = 30.0,
    poll_interval: float = 0.02,
    include_generated_diagnostics: bool = False,
) -> dict[str, Any]:
    if not include_generated_diagnostics:
        raise ValueError(
            "historical relocation frames are artificial diagnostics; pass "
            "--include-generated-diagnostics explicitly, or use "
            "tools/benchmark_resource_latency.py and reviewed native-session replay"
        )
    if type(hot_reads) is not int or hot_reads <= 0:
        raise ValueError("hot_reads must be a positive integer")
    if timeout <= 0 or poll_interval <= 0:
        raise ValueError("timeout and poll_interval must be positive")
    manifest_before = sha256(manifest_path.read_bytes()).hexdigest()
    pairs = _runtime_cases(manifest_path)
    rows = _run_case_rows(
        pairs,
        hot_reads=hot_reads,
        timeout=timeout,
        poll_interval=poll_interval,
    )
    manifest_after = sha256(manifest_path.read_bytes()).hexdigest()
    if manifest_after != manifest_before:
        raise RuntimeError("corpus manifest changed during the runtime benchmark")
    successful = [row for row in rows if row["error"] is None]
    case_hot_p50 = [row["hot_read_ms"]["p50"] for row in successful]
    hot = [sample for row in rows for sample in row["hot_read_ms"]["samples"]]
    startup_p50, startup_max = _timing_summary(
        [row["startup_ms"] for row in successful]
    )
    recovery_p50, recovery_max = _timing_summary(
        [row["recovery_ms"] for row in successful]
    )
    case_hot_p50_p50, case_hot_p50_max = _timing_summary(case_hot_p50)
    passed = _safety_passed(rows)
    implementation = implementation_hashes()
    implementation["runtime_benchmark_sha256"] = sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    return {
        "schema": SCHEMA,
        "version": SCHEMA_VERSION,
        "manifest": str(manifest_path),
        "manifest_sha256": manifest_before,
        "configuration": {
            "case_scope": (
                "reviewed positive real full screenshots transformed into artificial "
                "relocation diagnostics"
            ),
            "relocation_factory": "tools.benchmark_resources.generated_benchmark_cases::cached-relocation",
            "diagnostic_only": True,
            "acceptance_effect": "none",
            "recommended_replacement": "tools/benchmark_resource_latency.py plus reviewed native-session replay",
            "reader": "paulistinha-skills-v1",
            "visual_profile_fingerprint": _runtime_profile().fingerprint,
            "ocr": False,
            "hot_reads": hot_reads,
            "timeout_seconds": timeout,
            "poll_interval_seconds": poll_interval,
            "tracker_retry_interval": "production default",
            "clock": "time.perf_counter wall clock",
            "discovery_input": "complete frame; no annotated truth ROI",
        },
        "environment": environment(),
        "implementation": implementation,
        "cases": rows,
        "summary": {
            "cases": len(rows),
            "error_count": len(rows) - len(successful),
            "passed": passed,
            "startup_ms_p50": startup_p50,
            "startup_ms_max": startup_max,
            "recovery_ms_p50": recovery_p50,
            "recovery_ms_max": recovery_max,
            "hot_read_ms_p50": median(hot) if hot else None,
            "hot_read_ms_p95": _percentile(hot, 95) if hot else None,
            "hot_read_ms_max": max(hot) if hot else None,
            "case_hot_read_p50_ms_p50": case_hot_p50_p50,
            "case_hot_read_p50_ms_max": case_hot_p50_max,
        },
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    def duration(value: float | None, digits: int = 1) -> str:
        return "—" if value is None else f"{value:.{digits}f} ms"

    summary = report["summary"]
    lines = [
        "# Historical artificial relocation diagnostic",
        "",
        "**Acceptance effect:** none. These generated relocations are diagnostic only.",
        "",
        "Use `tools/benchmark_resource_latency.py` and reviewed native-session replay for "
        "current acceptance evidence.",
        "",
        f"**Diagnostic safety/result gate:** {'PASS' if summary['passed'] else 'FAIL'}",
        "",
        "Actual wall-clock timings include asynchronous tracker polling. Discovery received "
        "complete preserved frames; annotations were used only for scoring.",
        "",
        "| Source | Status | Startup | Recovery | Hot p50 | Hot p95 | Hot max | Wrong accepted poll | First moved unknown | Revision changed | Exact final |",
        "|---|---|---:|---:|---:|---:|---:|:---:|:---:|:---:|:---:|",
    ]
    for row in report["cases"]:
        hot = row["hot_read_ms"]
        status = "ok" if row["error"] is None else row["error"]["type"]
        lines.append(
            f"| `{row['source_id']}` | `{status}` | {duration(row['startup_ms'])} | "
            f"{duration(row['recovery_ms'])} | {duration(hot['p50'], 2)} | "
            f"{duration(hot['p95'], 2)} | {duration(hot['max'], 2)} | "
            f"{'yes' if row['any_incorrect_accepted_poll'] else 'no'} | "
            f"{'yes' if row['first_invalidated_read_unknown'] else 'no'} | "
            f"{'yes' if row['revision_changed'] else 'no'} | "
            f"{'yes' if row['exact_final_values'] else 'no'} |"
        )
    lines.extend(
        [
            "",
            f"Errors: **{summary['error_count']}**.  ",
            f"Startup p50/max: **{duration(summary['startup_ms_p50'])}/{duration(summary['startup_ms_max'])}**.  ",
            f"Recovery p50/max: **{duration(summary['recovery_ms_p50'])}/{duration(summary['recovery_ms_max'])}**.  ",
            f"Hot reads p50/p95/max: **{duration(summary['hot_read_ms_p50'], 2)}/"
            f"{duration(summary['hot_read_ms_p95'], 2)}/{duration(summary['hot_read_ms_max'], 2)}**.",
            "",
        ]
    )
    errors = [row for row in report["cases"] if row["error"] is not None]
    if errors:
        lines.extend(["## Case errors", ""])
        for row in errors:
            lines.append(
                f"- `{row['source_id']}`: `{row['error']['type']}` — "
                f"{row['error']['detail']}"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=corpus.DEFAULT_MANIFEST)
    parser.add_argument("--hot-reads", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=0.02)
    parser.add_argument(
        "--include-generated-diagnostics",
        action="store_true",
        help=(
            "required opt-in for artificial relocation diagnostics; these results never "
            "affect acceptance"
        ),
    )
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args(argv)
    try:
        report = build_report(
            args.manifest,
            hot_reads=args.hot_reads,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            include_generated_diagnostics=args.include_generated_diagnostics,
        )
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
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
