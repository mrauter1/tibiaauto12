#!/usr/bin/env python3
"""Replay annotated frame sequences through the production adaptive tracker.

The outer runner isolates every session in a process and enforces a hard wall
timeout.  The inner replay polls the asynchronous production tracker while each
captured frame is current.  Ground truth is consulted only by the scorer.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import multiprocessing
import os
from pathlib import Path
from queue import Empty
from statistics import median
import sys
from time import perf_counter, sleep
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tibia_client.adaptive_runtime import AdaptiveResourceTracker  # noqa: E402
from tibia_client.models import Quality, Sensor, SensorKind  # noqa: E402
from tibia_client.resource_sequences import (  # noqa: E402
    RESOURCE_FIELDS,
    ResourceSequence,
    load_resource_sequence,
)
from tibia_client.visual import AdaptiveVisual  # noqa: E402
from tools.compare_resource_readers import (  # noqa: E402
    environment,
    implementation_hashes,
)


SCHEMA = "tibiaauto.resource-sequence-benchmark"
SCHEMA_VERSION = 1


def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percent / 100) - 1)]


def _count(reading: Any) -> int | None:
    if reading is None or reading.quality is not Quality.KNOWN:
        return None
    value = reading.value
    count = value.get("count") if isinstance(value, Mapping) else None
    return count if type(count) is int else None


def _requests(frame: Any) -> tuple[dict[str, Sensor], dict[Sensor, AdaptiveVisual]]:
    sensors = {
        field: Sensor(SensorKind.VISUAL, (0, 0, frame.width, frame.height), field)
        for field in RESOURCE_FIELDS
    }
    requests = {
        sensor: AdaptiveVisual("paulistinha-skills-v1", field)
        for field, sensor in sensors.items()
    }
    return sensors, requests


def _observe(
    tracker: AdaptiveResourceTracker,
    frame: Any,
    requests: Mapping[Sensor, AdaptiveVisual],
    sensors: Mapping[str, Sensor],
    logical_started_at: float,
    presented_frame: int,
    playback_rate: float,
) -> dict[str, Any]:
    started = perf_counter()
    readings = tracker.read(frame, requests)
    elapsed_ms = (perf_counter() - started) * 1000
    diagnostics = {}
    for field, sensor in sensors.items():
        reading = readings.get(sensor)
        evidence = {} if reading is None else reading.diagnostics
        diagnostics[field] = {
            "effective_region": evidence.get("effective_region"),
            "layout_bounds": evidence.get("layout_bounds"),
            "layout_revision": evidence.get("layout_revision"),
            "anchor_method": evidence.get("anchor_method"),
        }
    return {
        "started_at": logical_started_at,
        "at": logical_started_at + elapsed_ms * playback_rate / 1000,
        "presented_frame": presented_frame,
        "read_ms": elapsed_ms,
        "values": {field: _count(readings.get(sensor)) for field, sensor in sensors.items()},
        "diagnostics": diagnostics,
    }


def replay_tracker(
    sequence: ResourceSequence,
    *,
    poll_interval: float,
    playback_rate: float,
    max_gap_ms: float,
    wall_timeout: float,
) -> list[dict[str, Any]]:
    """Run one already validated sequence through ``AdaptiveResourceTracker``."""

    if min(poll_interval, playback_rate, max_gap_ms, wall_timeout) <= 0:
        raise ValueError("replay timing options must be positive")
    logical_duration = (
        sequence.frames[-1].frame.captured_at
        + _last_hold_seconds(sequence, sequence.frames[-1].frame.captured_at)
        - sequence.frames[0].frame.captured_at
    )
    minimum_timeout = logical_duration / playback_rate + 0.05
    if wall_timeout < minimum_timeout:
        raise ValueError(
            f"wall timeout {wall_timeout:g}s is too short for this replay; "
            f"requires at least {minimum_timeout:.2f}s"
        )
    sensors, requests = _requests(sequence.frames[0].frame)
    tracker = AdaptiveResourceTracker()
    observations: list[dict[str, Any]] = []
    wall_origin = perf_counter()
    wall_deadline = wall_origin + wall_timeout
    logical_origin = sequence.frames[0].frame.captured_at
    try:
        for index, item in enumerate(sequence.frames):
            if perf_counter() >= wall_deadline:
                raise TimeoutError("resource sequence replay exceeded its wall timeout")
            target_wall = wall_origin + (item.frame.captured_at - logical_origin) / playback_rate
            remaining_to_target = target_wall - perf_counter()
            if remaining_to_target > 0:
                sleep(remaining_to_target)
            lateness = perf_counter() - target_wall
            if lateness > max(0.01, poll_interval * 2):
                raise TimeoutError(
                    f"replay could not preserve frame {index} presentation time; "
                    f"late by {lateness * 1000:.1f}ms"
                )
            next_at = (
                sequence.frames[index + 1].frame.captured_at
                if index + 1 < len(sequence.frames)
                else item.frame.captured_at + _last_hold_seconds(sequence, item.frame.captured_at)
            )
            gap_ms = (next_at - item.frame.captured_at) * 1000
            if gap_ms > max_gap_ms:
                raise ValueError(
                    f"frame {index} gap {gap_ms:.1f}ms exceeds --max-gap-ms {max_gap_ms:g}"
                )
            presented = target_wall
            logical_started = logical_origin + (perf_counter() - wall_origin) * playback_rate
            observations.append(
                _observe(
                    tracker, item.frame, requests, sensors, logical_started,
                    index, playback_rate,
                )
            )
            if perf_counter() >= wall_deadline:
                raise TimeoutError("resource sequence replay exceeded its wall timeout")
            hold_wall = max(0.0, next_at - item.frame.captured_at) / playback_rate
            while perf_counter() - presented < hold_wall:
                if perf_counter() >= wall_deadline:
                    raise TimeoutError("resource sequence replay exceeded its wall timeout")
                remaining = hold_wall - (perf_counter() - presented)
                sleep(min(poll_interval, max(0.0, remaining)))
                elapsed = perf_counter() - presented
                # The observation at the exact boundary belongs to the next
                # frame. Never score one last old-frame poll against new truth.
                if elapsed >= hold_wall:
                    break
                logical = logical_origin + (perf_counter() - wall_origin) * playback_rate
                observations.append(
                    _observe(
                        tracker, item.frame, requests, sensors, logical,
                        index, playback_rate,
                    )
                )
        return observations
    finally:
        tracker.close()


def _last_hold_seconds(sequence: ResourceSequence, last_at: float) -> float:
    deadlines = [
        float(event["at"]) + float(event["deadline_ms"]) / 1000
        for event in sequence.events
        if event["annotation_status"] == "reviewed"
    ]
    return max(0.05, max(deadlines, default=last_at + 0.05) - last_at)


def _truth_frame(sequence: ResourceSequence, at: float):
    found = sequence.frames[0]
    for frame in sequence.frames:
        if frame.frame.captured_at > at + 1e-9:
            break
        found = frame
    return found


def _fresh(frame: Any) -> bool:
    truth = frame.capture_freshness
    return truth["annotation_status"] == "reviewed" and truth["state"] == "fresh"


def _eligible(frame: Any, field: str) -> bool:
    truth = frame.resources[field]
    return (
        truth["annotation_status"] == "reviewed"
        and truth["readable"] is True
        and _fresh(frame)
    )


def _unknown_intervals(
    observations: list[Mapping[str, Any]], field: str, session_end: float
) -> list[dict[str, float]]:
    intervals: list[dict[str, float]] = []
    start: float | None = None
    for observation in observations:
        at = float(observation["at"])
        if at > session_end:
            break
        unknown = observation["values"].get(field) is None
        if unknown and start is None:
            start = at
        elif not unknown and start is not None:
            intervals.append({"start": start, "end": at, "duration_ms": (at - start) * 1000})
            start = None
    if start is not None:
        intervals.append(
            {"start": start, "end": session_end, "duration_ms": max(0.0, session_end - start) * 1000}
        )
    return intervals


def score_observations(
    sequence: ResourceSequence,
    observations: list[Mapping[str, Any]],
    *,
    deadline_timing_valid: bool = True,
) -> dict[str, Any]:
    """Score externally supplied tracker observations; useful for regression tests."""

    _validate_observations(sequence, observations)
    prior_values: dict[str, int | None] = {field: None for field in RESOURCE_FIELDS}
    accepted_errors: list[dict[str, Any]] = []
    association_errors: list[dict[str, Any]] = []
    safety_violations: list[dict[str, Any]] = []
    for observation_index, observation in enumerate(observations):
        frame = _truth_frame(sequence, float(observation["at"]))
        for field in RESOURCE_FIELDS:
            truth = frame.resources[field]
            actual = observation["values"].get(field)
            freshness = frame.capture_freshness
            if truth["annotation_status"] == "reviewed" and actual is not None:
                if (
                    freshness["annotation_status"] == "reviewed"
                    and freshness["state"] == "stale"
                ):
                    safety_violations.append(
                        {
                            "observation": observation_index,
                            "at": float(observation["at"]),
                            "frame_index": frame.index,
                            "resource": field,
                            "actual": actual,
                            "classification": "accepted_known_from_stale_capture",
                        }
                    )
                elif (
                    freshness["annotation_status"] == "reviewed"
                    and freshness["state"] == "fresh"
                    and truth["readable"] is False
                ):
                    safety_violations.append(
                        {
                            "observation": observation_index,
                            "at": float(observation["at"]),
                            "frame_index": frame.index,
                            "resource": field,
                            "actual": actual,
                            "classification": "unsafe_known_on_unreadable",
                        }
                    )
            if _eligible(frame, field) and actual is not None and actual != truth["value"]:
                stale = prior_values[field] is not None and actual == prior_values[field]
                accepted_errors.append(
                    {
                        "observation": observation_index,
                        "at": float(observation["at"]),
                        "frame_index": frame.index,
                        "resource": field,
                        "expected": truth["value"],
                        "actual": actual,
                        "classification": "stale_transient" if stale else "wrong_known",
                    }
                )
            if _eligible(frame, field) and actual is not None:
                diagnostics = observation.get("diagnostics", {}).get(field, {})
                effective = diagnostics.get("effective_region")
                layout = diagnostics.get("layout_bounds")
                value_box, label_box = truth["value_box"], truth["label_box"]
                value_associated = _value_region_associated(effective, frame, field)
                label_associated = _layout_associated(layout, frame, field)
                if not value_associated or not label_associated:
                    association_errors.append(
                        {
                            "observation": observation_index,
                            "at": float(observation["at"]),
                            "frame_index": frame.index,
                            "resource": field,
                            "effective_region": effective,
                            "expected_value_box": value_box,
                            "layout_bounds": layout,
                            "expected_label_box": label_box,
                            "value_region_overlap": value_associated,
                            "label_inside_layout": label_associated,
                        }
                    )
            if truth["annotation_status"] == "reviewed" and truth["readable"]:
                prior_values[field] = truth["value"]

    event_rows: list[dict[str, Any]] = []
    recovery_delays: list[float] = []
    invalidation_delays: list[float] = []
    for event in sequence.events:
        row = {
            "id": event["id"],
            "resource": event["resource"],
            "kind": event["kind"],
            "annotation_status": event["annotation_status"],
            "deadline_ms": event["deadline_ms"],
            "delay_ms": None,
            "met_deadline": None,
            "excluded_reason": None,
            "safety_held": None,
            "unsafe_known_after_invalidation": [],
        }
        if event["annotation_status"] != "reviewed":
            row["excluded_reason"] = "event is unreviewed"
            event_rows.append(row)
            continue
        onset_frame = sequence.frames[event["frame_index"]]
        if not deadline_timing_valid:
            row["excluded_reason"] = "deadline timing is non-comparable when playback rate is not 1x"
            event_rows.append(row)
            continue
        if not _fresh(onset_frame):
            row["excluded_reason"] = "onset frame lacks reviewed fresh-capture evidence"
            event_rows.append(row)
            continue
        if event["expected_value"] is None and not _invalidation_exercised(
            sequence, observations, event
        ):
            row["excluded_reason"] = "invalidation not exercised: no prior associated known reading"
            event_rows.append(row)
            continue
        next_onset = min(
            (
                float(other["at"])
                for other in sequence.events
                if other["resource"] == event["resource"]
                and float(other["at"]) > float(event["at"])
            ),
            default=float("inf"),
        )
        first_at = None
        for observation in observations:
            if float(observation["at"]) + 1e-9 < float(event["at"]):
                continue
            if float(observation["at"]) >= next_onset:
                break
            truth_frame = _truth_frame(sequence, float(observation["at"]))
            truth = truth_frame.resources[event["resource"]]
            if not _fresh(truth_frame) or truth["annotation_status"] != "reviewed":
                continue
            actual = observation["values"].get(event["resource"])
            if event["expected_value"] is None:
                satisfied = truth["readable"] is False and actual is None
            else:
                satisfied = (
                    truth["readable"] is True
                    and truth["value"] == event["expected_value"]
                    and actual == event["expected_value"]
                    and _observation_associated(observation, event["resource"], truth_frame)
                )
            if satisfied:
                first_at = float(observation["at"])
                break
        if first_at is not None:
            row["delay_ms"] = max(0.0, first_at - float(event["at"])) * 1000
            row["met_deadline"] = row["delay_ms"] <= float(event["deadline_ms"]) + 1e-6
            if event["expected_value"] is None:
                invalidation_delays.append(row["delay_ms"])
            else:
                recovery_delays.append(row["delay_ms"])
        else:
            row["met_deadline"] = False
        if event["expected_value"] is None:
            unsafe = []
            for observation_index, observation in enumerate(observations):
                at = float(observation["at"])
                if at + 1e-9 < float(event["at"]) or at >= next_onset:
                    continue
                truth_frame = _truth_frame(sequence, at)
                truth = truth_frame.resources[event["resource"]]
                freshness = truth_frame.capture_freshness
                actual = observation["values"].get(event["resource"])
                classification = None
                if (
                    truth["annotation_status"] == "reviewed"
                    and freshness["annotation_status"] == "reviewed"
                    and freshness["state"] == "stale"
                    and actual is not None
                ):
                    classification = "accepted_known_from_stale_capture"
                elif (
                    truth["annotation_status"] == "reviewed"
                    and truth["readable"] is False
                    and freshness["annotation_status"] == "reviewed"
                    and freshness["state"] == "fresh"
                    and actual is not None
                ):
                    classification = "unsafe_known_on_unreadable"
                if classification is not None:
                    unsafe.append(
                        {
                            "observation": observation_index,
                            "at": at,
                            "actual": actual,
                            "classification": classification,
                        }
                    )
            row["unsafe_known_after_invalidation"] = unsafe
            row["safety_held"] = not unsafe
        row["passed"] = bool(row["met_deadline"]) and row["safety_held"] is not False
        event_rows.append(row)

    coverage: dict[str, Any] = {}
    session_end = (
        sequence.frames[-1].frame.captured_at
        + _last_hold_seconds(sequence, sequence.frames[-1].frame.captured_at)
    )
    availability = _availability(sequence, observations, session_end)
    for field in RESOURCE_FIELDS:
        reviewed = [
            frame for frame in sequence.frames
            if frame.resources[field]["annotation_status"] == "reviewed"
        ]
        visible = [frame for frame in reviewed if frame.resources[field]["visible"]]
        readable = [frame for frame in visible if frame.resources[field]["readable"]]
        eligible = [frame for frame in readable if _fresh(frame)]
        fresh = [frame for frame in sequence.frames if _fresh(frame)]
        durations = _frame_durations(sequence, session_end)
        reviewed_ms = sum(durations[frame.index] for frame in reviewed) * 1000
        visible_ms = sum(durations[frame.index] for frame in visible) * 1000
        readable_ms = sum(durations[frame.index] for frame in readable) * 1000
        eligible_ms = sum(durations[frame.index] for frame in eligible) * 1000
        fresh_ms = sum(durations[frame.index] for frame in fresh) * 1000
        total_ms = sum(durations) * 1000
        coverage[field] = {
            "total_frames": len(sequence.frames),
            "reviewed_frames": len(reviewed),
            "unreviewed_frames": len(sequence.frames) - len(reviewed),
            "visible_frames": len(visible),
            "visible_readable_frames": len(readable),
            "fresh_scoring_frames": len(eligible),
            "visible_readable_rate": len(readable) / len(visible) if visible else None,
            "reviewed_coverage_rate": len(reviewed) / len(sequence.frames),
            "timeline_ms": total_ms,
            "reviewed_ms": reviewed_ms,
            "visible_ms": visible_ms,
            "visible_readable_ms": readable_ms,
            "fresh_scoring_ms": eligible_ms,
            "fresh_evidence_ms": fresh_ms,
            "fresh_evidence_rate": fresh_ms / total_ms if total_ms else None,
            "time_reviewed_coverage_rate": reviewed_ms / total_ms if total_ms else None,
            "time_visible_readable_rate": readable_ms / visible_ms if visible_ms else None,
            **availability[field],
            "unknown_intervals": _unknown_intervals(observations, field, session_end),
        }
    scored_events = [row for row in event_rows if row["met_deadline"] is not None]
    deadline_rate = (
        sum(row["passed"] is True for row in scored_events) / len(scored_events)
        if scored_events else None
    )
    read_samples = [float(item["read_ms"]) for item in observations if "read_ms" in item]
    annotation_complete = all(
        frame.panel["annotation_status"] == "reviewed"
        and frame.capture_freshness["annotation_status"] == "reviewed"
        and all(item["annotation_status"] == "reviewed" for item in frame.resources.values())
        for frame in sequence.frames
    ) and all(event["annotation_status"] == "reviewed" for event in sequence.events)
    freshness_evidence_complete = all(_fresh(frame) for frame in sequence.frames)
    availability_rates = [
        item["time_correct_fresh_rate"]
        for item in coverage.values()
        if item["time_correct_fresh_rate"] is not None
    ]
    return {
        "session_id": sequence.session_id,
        "origin": sequence.origin,
        "frame_count": len(sequence.frames),
        "observation_count": len(observations),
        "coverage": coverage,
        "events": event_rows,
        "scored_event_count": len(scored_events),
        "deadline_success_rate": deadline_rate,
        "accepted_errors": accepted_errors,
        "accepted_error_count": len(accepted_errors),
        "association_errors": association_errors,
        "association_error_count": len(association_errors),
        "safety_violations": safety_violations,
        "safety_violation_count": len(safety_violations),
        "unsafe_known_on_unreadable_count": sum(
            item["classification"] == "unsafe_known_on_unreadable"
            for item in safety_violations
        ),
        "accepted_known_from_stale_capture_count": sum(
            item["classification"] == "accepted_known_from_stale_capture"
            for item in safety_violations
        ),
        "stale_transient_count": sum(
            item["classification"] == "stale_transient" for item in accepted_errors
        ),
        "recovery_ms": _timings(recovery_delays),
        "invalidation_ms": _timings(invalidation_delays),
        "tracker_read_ms": _timings(read_samples),
        "deadline_timing_comparable": deadline_timing_valid,
        "annotation_complete": annotation_complete,
        "freshness_evidence_complete": freshness_evidence_complete,
        "correct_fresh_availability_rate": (
            sum(availability_rates) / len(availability_rates)
            if availability_rates else None
        ),
        "passed": (
            bool(scored_events)
            and deadline_rate == 1.0
            and not accepted_errors
            and not association_errors
            and not safety_violations
            and annotation_complete
            and freshness_evidence_complete
        ),
        "evaluation_boundary": (
            "Production AdaptiveResourceTracker recognition replay only; native capture "
            "process health, acquisition latency, and upstream freshness are not inferred."
        ),
    }


def _availability(
    sequence: ResourceSequence,
    observations: list[Mapping[str, Any]],
    session_end: float,
) -> dict[str, dict[str, float | None]]:
    """Time-weight delivered output over reviewed fresh readable truth only."""

    start = sequence.frames[0].frame.captured_at
    boundaries = {start, session_end}
    boundaries.update(
        frame.frame.captured_at
        for frame in sequence.frames
        if start <= frame.frame.captured_at <= session_end
    )
    boundaries.update(
        float(observation["at"])
        for observation in observations
        if start <= float(observation["at"]) <= session_end
    )
    ordered = sorted(boundaries)
    totals = {
        field: {"eligible": 0.0, "correct": 0.0, "unknown": 0.0, "wrong": 0.0}
        for field in RESOURCE_FIELDS
    }
    delivered: Mapping[str, Any] | None = None
    delivered_frame_index: int | None = None
    observation_index = 0
    sorted_observations = list(observations)
    for left, right in zip(ordered, ordered[1:]):
        frame = _truth_frame(sequence, left)
        if delivered_frame_index != frame.index:
            delivered = None
            delivered_frame_index = frame.index
        while (
            observation_index < len(sorted_observations)
            and float(sorted_observations[observation_index]["at"]) <= left + 1e-12
        ):
            candidate = sorted_observations[observation_index]
            presented = candidate.get("presented_frame")
            if presented is None:
                presented = _truth_frame(
                    sequence, float(candidate.get("started_at", candidate["at"]))
                ).index
            if presented == frame.index:
                delivered = candidate
            observation_index += 1
        duration = max(0.0, right - left)
        if duration == 0:
            continue
        for field in RESOURCE_FIELDS:
            if not _eligible(frame, field):
                continue
            totals[field]["eligible"] += duration
            actual = None if delivered is None else delivered["values"].get(field)
            if actual is None:
                totals[field]["unknown"] += duration
            elif (
                actual == frame.resources[field]["value"]
                and _observation_associated(delivered, field, frame)
            ):
                totals[field]["correct"] += duration
            else:
                totals[field]["wrong"] += duration
    result = {}
    for field, values in totals.items():
        denominator = values["eligible"] * 1000
        correct = values["correct"] * 1000
        unknown = values["unknown"] * 1000
        wrong = values["wrong"] * 1000
        result[field] = {
            "inference_eligible_ms": denominator,
            "time_correct_fresh_ms": correct,
            "time_unknown_on_readable_ms": unknown,
            "time_wrong_known_on_readable_ms": wrong,
            "time_correct_fresh_rate": correct / denominator if denominator else None,
            "time_unknown_on_readable_rate": unknown / denominator if denominator else None,
            "time_wrong_known_on_readable_rate": wrong / denominator if denominator else None,
        }
    return result


def _validate_observations(
    sequence: ResourceSequence, observations: list[Mapping[str, Any]]
) -> None:
    if not observations:
        raise ValueError("at least one observation is required")
    previous = -float("inf")
    first_at = sequence.frames[0].frame.captured_at
    for index, observation in enumerate(observations):
        at = observation.get("at")
        started = observation.get("started_at", at)
        presented = observation.get("presented_frame")
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in (at, started)
        ):
            raise ValueError(f"observation {index} has invalid timing")
        if float(at) < previous or float(at) < first_at or float(started) > float(at):
            raise ValueError(f"observation {index} is unsorted or outside the sequence")
        if presented is None:
            presented = _truth_frame(sequence, float(started)).index
        if type(presented) is not int or not 0 <= presented < len(sequence.frames):
            raise ValueError(f"observation {index} has invalid presented_frame")
        if _truth_frame(sequence, float(started)).index != presented:
            raise ValueError(f"observation {index} does not match its presented frame")
        values = observation.get("values")
        if not isinstance(values, Mapping) or set(values) != set(RESOURCE_FIELDS):
            raise ValueError(f"observation {index} must contain all resource values")
        previous = float(at)


def _observation_associated(observation: Mapping[str, Any], field: str, frame: Any) -> bool:
    diagnostics = observation.get("diagnostics", {}).get(field, {})
    return _value_region_associated(
        diagnostics.get("effective_region"), frame, field
    ) and _layout_associated(diagnostics.get("layout_bounds"), frame, field)


def _invalidation_exercised(
    sequence: ResourceSequence, observations: list[Mapping[str, Any]], event: Mapping[str, Any]
) -> bool:
    for observation in reversed(observations):
        if float(observation["at"]) >= float(event["at"]):
            continue
        truth_frame = _truth_frame(sequence, float(observation["at"]))
        truth = truth_frame.resources[event["resource"]]
        actual = observation["values"].get(event["resource"])
        if (
            _eligible(truth_frame, event["resource"])
            and actual == truth["value"]
            and _observation_associated(observation, event["resource"], truth_frame)
        ):
            return True
    return False


def _frame_durations(sequence: ResourceSequence, session_end: float) -> list[float]:
    result = []
    for index, frame in enumerate(sequence.frames):
        end = (
            sequence.frames[index + 1].frame.captured_at
            if index + 1 < len(sequence.frames)
            else session_end
        )
        result.append(max(0.0, end - frame.frame.captured_at))
    return result


def _region(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        return None
    left, top, right, bottom = (float(item) for item in value)
    return (left, top, right, bottom) if left < right and top < bottom else None


def _overlaps(actual: Any, expected: Any) -> bool:
    first, second = _region(actual), _region(expected)
    return bool(
        first and second
        and max(first[0], second[0]) < min(first[2], second[2])
        and max(first[1], second[1]) < min(first[3], second[3])
    )


def _contains(outer: Any, inner: Any) -> bool:
    first, second = _region(outer), _region(inner)
    return bool(
        first and second
        and first[0] <= second[0] and first[1] <= second[1]
        and first[2] >= second[2] and first[3] >= second[3]
    )


def _value_region_associated(actual: Any, frame: Any, field: str) -> bool:
    region = _region(actual)
    expected = _region(frame.resources[field]["value_box"])
    if not region or not expected:
        return False
    intersection = max(0.0, min(region[2], expected[2]) - max(region[0], expected[0])) * max(
        0.0, min(region[3], expected[3]) - max(region[1], expected[1])
    )
    expected_area = (expected[2] - expected[0]) * (expected[3] - expected[1])
    if intersection / expected_area < 0.8:
        return False
    for other_field in RESOURCE_FIELDS:
        if other_field == field:
            continue
        other = _region(frame.resources[other_field]["value_box"])
        if other:
            center = ((other[0] + other[2]) / 2, (other[1] + other[3]) / 2)
            if region[0] <= center[0] <= region[2] and region[1] <= center[1] <= region[3]:
                return False
    return True


def _layout_associated(actual: Any, frame: Any, field: str) -> bool:
    layout = _region(actual)
    panel = _region(frame.panel["box"])
    label = _region(frame.resources[field]["label_box"])
    if not layout or not panel or not label:
        return False
    center = ((label[0] + label[2]) / 2, (label[1] + label[3]) / 2)
    if not (layout[0] <= center[0] <= layout[2] and layout[1] <= center[1] <= layout[3]):
        return False
    layout_area = (layout[2] - layout[0]) * (layout[3] - layout[1])
    panel_area = (panel[2] - panel[0]) * (panel[3] - panel[1])
    return layout_area <= panel_area * 1.5 and _overlaps(layout, panel)


def _timings(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50": median(values) if values else None,
        "p95": _percentile(values, 95),
        "max": max(values) if values else None,
    }


def benchmark_session(
    path: Path,
    *,
    poll_interval: float,
    playback_rate: float,
    max_gap_ms: float,
    wall_timeout: float,
) -> dict[str, Any]:
    manifest_path = path / "session.json"
    manifest_sha256 = sha256(manifest_path.read_bytes()).hexdigest()
    fingerprints = _implementation_fingerprints()
    run_environment = environment()
    sequence = load_resource_sequence(path)
    observations = replay_tracker(
        sequence,
        poll_interval=poll_interval,
        playback_rate=playback_rate,
        max_gap_ms=max_gap_ms,
        wall_timeout=wall_timeout,
    )
    result = score_observations(
        sequence, observations, deadline_timing_valid=playback_rate == 1.0
    )
    if sha256(manifest_path.read_bytes()).hexdigest() != manifest_sha256:
        raise RuntimeError("session manifest changed during replay")
    if _implementation_fingerprints() != fingerprints:
        raise RuntimeError("benchmark implementation changed during replay")
    if environment() != run_environment:
        raise RuntimeError("benchmark environment changed during replay")
    run_identity = {
        "session_manifest_sha256": manifest_sha256,
        "options": {
            "poll_interval": poll_interval,
            "playback_rate": playback_rate,
            "max_gap_ms": max_gap_ms,
            "wall_timeout": wall_timeout,
        },
        "implementation": fingerprints,
        "environment": run_environment,
    }
    result["path"] = str(path)
    result["session_manifest_sha256"] = manifest_sha256
    result["provenance"] = dict(sequence.provenance)
    result["source_lineage"] = (
        None if sequence.source_lineage is None else dict(sequence.source_lineage)
    )
    result["run_fingerprint_sha256"] = sha256(
        json.dumps(run_identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    result["run_options"] = run_identity["options"]
    result["run_implementation"] = fingerprints
    result["run_environment"] = run_environment
    return result


def _implementation_fingerprints() -> dict[str, Any]:
    result = implementation_hashes()
    result["sequence_code_files"] = {
        name: sha256((ROOT / name).read_bytes()).hexdigest()
        for name in (
            "tibia_client/resource_sequences.py",
            "tools/record_resource_sequence.py",
            "tools/benchmark_resource_sequences.py",
        )
    }
    return result


def _worker(path: str, options: dict[str, float], queue: Any) -> None:
    try:
        message = {"ok": benchmark_session(Path(path), **options)}
    except BaseException as exc:
        message = {"error": {"type": type(exc).__name__, "detail": str(exc)}}
    # Adaptive work is intentionally daemonized in production. A short-lived
    # isolation worker may otherwise enter native-library teardown while one
    # superseded discovery is draining. Flush the one bounded result first,
    # then exit without running unrelated interpreter finalizers.
    queue.put(message)
    queue.close()
    queue.join_thread()
    os._exit(0)


def benchmark_session_bounded(path: Path, **options: float) -> dict[str, Any]:
    """Run one session with a killable outer deadline, including load time."""

    limit = float(options["wall_timeout"])
    context = multiprocessing.get_context("spawn")
    queue = context.Queue(maxsize=1)
    process = context.Process(target=_worker, args=(str(path), options, queue), daemon=False)
    deadline = perf_counter() + limit
    process.start()
    message = _receive_worker_message(process, queue, deadline)
    if message is None:
        process.terminate()
        process.join(1.0)
        if process.is_alive():
            process.kill()
            process.join(1.0)
        _close_result_queue(queue)
        return {
            "session_id": path.name,
            "path": str(path),
            "error": {"type": "TimeoutError", "detail": f"session exceeded {limit:g}s hard deadline"},
            "passed": False,
        }
    process.join(max(0.0, deadline - perf_counter()))
    if process.is_alive():
        process.terminate()
        process.join(1.0)
    if process.exitcode not in (0, None):
        _close_result_queue(queue)
        return {
            "session_id": path.name,
            "path": str(path),
            "error": {
                "type": "WorkerError",
                "detail": f"worker exited with code {process.exitcode} after returning a result",
            },
            "passed": False,
        }
    if "error" in message:
        _close_result_queue(queue)
        return {
            "session_id": path.name,
            "path": str(path),
            "error": message["error"],
            "passed": False,
        }
    result = message["ok"]
    result["error"] = None
    _close_result_queue(queue)
    return result


def _receive_worker_message(process: Any, queue: Any, deadline: float) -> Any | None:
    """Drain the result pipe while the worker lives, avoiding feeder deadlock."""

    while True:
        remaining = deadline - perf_counter()
        if remaining <= 0:
            return None
        try:
            return queue.get(timeout=min(0.1, remaining))
        except Empty:
            if not process.is_alive():
                try:
                    return queue.get_nowait()
                except Empty:
                    return None


def _close_result_queue(queue: Any) -> None:
    try:
        queue.close()
        queue.join_thread()
    except (OSError, ValueError):
        pass


def build_report(paths: list[Path], **options: float) -> dict[str, Any]:
    if not paths:
        raise ValueError("at least one sequence path is required")
    report_environment = environment()
    report_implementation = _implementation_fingerprints()
    sessions = [benchmark_session_bounded(path, **options) for path in paths]
    if environment() != report_environment:
        raise RuntimeError("benchmark environment changed across sessions")
    if _implementation_fingerprints() != report_implementation:
        raise RuntimeError("benchmark implementation changed across sessions")
    successful = [row for row in sessions if row.get("error") is None]
    if any(
        row.get("run_environment") != report_environment
        or row.get("run_implementation") != report_implementation
        or row.get("run_options") != options
        for row in successful
    ):
        raise RuntimeError("session results contain mixed run identity")
    deadline_rates = [row["deadline_success_rate"] for row in successful if row["deadline_success_rate"] is not None]
    per_session_coverage = []
    per_session_availability = []
    for row in successful:
        rates = [
            item["time_visible_readable_rate"]
            for item in row["coverage"].values()
            if item["time_visible_readable_rate"] is not None
        ]
        if rates:
            per_session_coverage.append(sum(rates) / len(rates))
        availability_rates = [
            item["time_correct_fresh_rate"]
            for item in row["coverage"].values()
            if item["time_correct_fresh_rate"] is not None
        ]
        if availability_rates:
            per_session_availability.append(
                sum(availability_rates) / len(availability_rates)
            )
    by_origin = {}
    for origin in ("native_capture", "imported_native", "derived_corpus"):
        rows = [row for row in successful if row["origin"] == origin]
        origin_deadlines = [row["deadline_success_rate"] for row in rows if row["deadline_success_rate"] is not None]
        origin_coverage = []
        origin_availability = []
        for row in rows:
            rates = [item["time_visible_readable_rate"] for item in row["coverage"].values() if item["time_visible_readable_rate"] is not None]
            if rates:
                origin_coverage.append(sum(rates) / len(rates))
            availability_rates = [
                item["time_correct_fresh_rate"]
                for item in row["coverage"].values()
                if item["time_correct_fresh_rate"] is not None
            ]
            if availability_rates:
                origin_availability.append(
                    sum(availability_rates) / len(availability_rates)
                )
        by_origin[origin] = {
            "session_count": len(rows),
            "macro_deadline_success_rate": sum(origin_deadlines) / len(origin_deadlines) if origin_deadlines else None,
            "macro_visible_readable_rate": sum(origin_coverage) / len(origin_coverage) if origin_coverage else None,
            "macro_correct_fresh_availability_rate": (
                sum(origin_availability) / len(origin_availability)
                if origin_availability else None
            ),
            "passed_session_count": sum(row["passed"] for row in rows),
        }
    return {
        "schema": SCHEMA,
        "version": SCHEMA_VERSION,
        "options": options,
        "environment": report_environment,
        "implementation": report_implementation,
        "sessions": sessions,
        "summary": {
            "session_count": len(sessions),
            "successful_session_count": len(successful),
            "passed_session_count": sum(row.get("passed") is True for row in sessions),
            "by_session_macro_deadline_success_rate": (
                sum(deadline_rates) / len(deadline_rates) if deadline_rates else None
            ),
            "by_session_macro_visible_readable_rate": (
                sum(per_session_coverage) / len(per_session_coverage)
                if per_session_coverage else None
            ),
            "by_session_macro_correct_fresh_availability_rate": (
                sum(per_session_availability) / len(per_session_availability)
                if per_session_availability else None
            ),
            "passed": len(successful) == len(sessions) and all(row["passed"] for row in successful),
            "by_origin": by_origin,
        },
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Resource sequence replay", "",
        f"Overall: **{'PASS' if summary['passed'] else 'FAIL'}**", "",
        "This replays the production `AdaptiveResourceTracker`. It does not measure native capture-process health or prove upstream frame freshness.",
        "",
        "| Session | Origin | Frames | Scored events | Deadline rate | Actual correct/fresh time | Accepted errors | Safety (hidden/stale capture) | Recovery p50/p95/max | Result |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    def show(value: Any) -> str:
        return "—" if value is None else f"{value:.1f}"
    for row in report["sessions"]:
        if row.get("error"):
            lines.append(
                f"| `{row['session_id']}` | — | — | — | — | — | — | — | `{row['error']['type']}` | FAIL |"
            )
            continue
        timing = row["recovery_ms"]
        rate = row["deadline_success_rate"]
        availability_rate = row["correct_fresh_availability_rate"]
        lines.append(
            f"| `{row['session_id']}` | `{row['origin']}` | {row['frame_count']} | "
            f"{row['scored_event_count']} | {'—' if rate is None else f'{rate:.1%}'} | "
            f"{'—' if availability_rate is None else f'{availability_rate:.1%}'} | "
            f"{row['accepted_error_count']} | {row['safety_violation_count']} "
            f"({row['unsafe_known_on_unreadable_count']}/"
            f"{row['accepted_known_from_stale_capture_count']}) | "
            f"{show(timing['p50'])}/{show(timing['p95'])}/{show(timing['max'])} ms | "
            f"{'PASS' if row['passed'] else 'FAIL'} |"
        )
    macro_deadline = summary["by_session_macro_deadline_success_rate"]
    macro_coverage = summary["by_session_macro_visible_readable_rate"]
    macro_availability = summary["by_session_macro_correct_fresh_availability_rate"]
    macro_deadline_text = "—" if macro_deadline is None else f"{macro_deadline:.1%}"
    macro_coverage_text = "—" if macro_coverage is None else f"{macro_coverage:.1%}"
    macro_availability_text = (
        "—" if macro_availability is None else f"{macro_availability:.1%}"
    )
    lines.extend([
        "",
        "Pooled cross-origin macros are descriptive only: deadline success "
        f"**{macro_deadline_text}**, annotation visible-readable coverage "
        f"**{macro_coverage_text}**, actual correct/fresh time **{macro_availability_text}**.",
        "",
        "## Evidence by origin", "",
        "| Origin | Sessions | Passed | Deadline macro | Annotation visible-readable macro | Actual correct/fresh macro |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for origin, values in summary["by_origin"].items():
        deadline = values["macro_deadline_success_rate"]
        coverage = values["macro_visible_readable_rate"]
        availability = values["macro_correct_fresh_availability_rate"]
        lines.append(
            f"| `{origin}` | {values['session_count']} | {values['passed_session_count']} | "
            f"{'—' if deadline is None else f'{deadline:.1%}'} | "
            f"{'—' if coverage is None else f'{coverage:.1%}'} | "
            f"{'—' if availability is None else f'{availability:.1%}'} |"
        )
    lines.append("")
    errors = [row for row in report["sessions"] if row.get("error")]
    if errors:
        lines.extend(["## Errors", ""])
        for row in errors:
            lines.append(f"- `{row['session_id']}`: `{row['error']['type']}` — {row['error']['detail']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sequences", nargs="+", type=Path)
    parser.add_argument("--poll-interval", type=float, default=0.02)
    parser.add_argument("--playback-rate", type=float, default=1.0)
    parser.add_argument("--max-gap-ms", type=float, default=20000.0)
    parser.add_argument("--wall-timeout", type=float, default=90.0)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args(argv)
    options = {
        "poll_interval": args.poll_interval,
        "playback_rate": args.playback_rate,
        "max_gap_ms": args.max_gap_ms,
        "wall_timeout": args.wall_timeout,
    }
    if any(not math.isfinite(value) or value <= 0 for value in options.values()):
        parser.error("timing options must be finite and positive")
    report = build_report(args.sequences, **options)
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
