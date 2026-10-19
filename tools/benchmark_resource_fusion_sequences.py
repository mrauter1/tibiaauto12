"""Generated decision-input traces, explicitly NOT native gameplay recordings."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import perf_counter

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.perception import Frame  # noqa: E402
from tibia_client.models import Quality, Reading  # noqa: E402
from tools.resource_bar_evidence import (  # noqa: E402
    BarEvidence, FreshMaximum, cross_check, frame_token, validated_maximum,
)
from tools.resource_temporal_checks import (  # noqa: E402
    TemporalBarInterval, TemporalIdentity, TemporalPolicy, TemporalResourceGuard,
)

FIELD = "resource_health"
VARIANTS = ("robust", "bar", "temporal", "combined")


def step(truth, candidate, *, t=None, bar=True, maximum=1000, readable=True,
         source="generated-trace", session="one", caption_current=None):
    return dict(truth=truth, candidate=candidate, at=t, bar=bar, maximum=maximum,
                readable=readable, source=source, session=session,
                caption_current=truth if caption_current is None else caption_current)


def scenarios():
    return {
        "stable": [step(500, 500) for _ in range(4)],
        "isolated_wrong_digit": [step(500, 500), step(500, 900), step(500, 500)],
        "persistent_wrong_digit": [step(500, 500), step(500, 900), step(500, 900)],
        "damage": [step(500, 500), step(250, 250), step(250, 250)],
        "healing": [step(250, 250), step(500, 500), step(500, 500)],
        "rapid_damage": [step(value, value) for value in (500, 400, 300, 200, 100)],
        "damage_without_bar": [step(value, value, bar=False) for value in (500, 250, 250)],
        "stale_number_changed_bar": [step(500, 500), step(250, 500), step(250, 500)],
        "occluded_number": [step(500, 500), step(400, None, readable=False), step(400, 400)],
        "out_of_order": [step(500, 500, t=1), step(250, 250, t=.9),
                         step(250, 250, t=1.05), step(250, 250, t=1.1)],
        "duplicate_timestamp": [step(500, 500, t=1), step(500, 500, t=1),
                                step(500, 500, t=1.05), step(500, 500, t=1.1)],
        "new_capture_clock": [step(500, 500, t=10), step(250, 250, t=1, session="two")],
        "changed_maximum": [step(500, 500), step(1000, 1000, maximum=2000),
                            step(1000, 1000, maximum=2000)],
        "caption_bar_disagree": [step(500, 500), step(500, 500, caption_current=900)],
        "expiration_is_not_confirmation": [step(500, 500, t=1, bar=False),
                                            step(250, 250, t=1.05, bar=False),
                                            step(250, 250, t=1.35, bar=False)],
    }


def run_trace(rows, variant):
    guard = TemporalResourceGuard(TemporalPolicy(confirm_changes=True))
    output = []
    clocks = {}
    for index, item in enumerate(rows):
        timestamp = 1 + index * .05 if item["at"] is None else item["at"]
        scope = (item["source"], item["session"])
        timestamp_advances = scope not in clocks or timestamp > clocks[scope]
        clocks[scope] = max(timestamp, clocks.get(scope, timestamp))
        frame = Frame(np.full((10, 10, 3), index, dtype=np.uint8),
                      captured_at=timestamp, source=item["source"])
        token = frame_token(frame)
        candidate = item["candidate"]
        reading = (Reading(None, Quality.UNKNOWN, "generated unreadable observation")
                   if candidate is None else Reading({"count": candidate}))
        fraction = item["truth"] / item["maximum"]
        interval = (max(0, fraction - .005), min(1, fraction + .005)) if item["bar"] else None
        evidence = BarEvidence(FIELD, interval, (0, 0, 10, 10) if interval else None,
                               (1, 1, 9, 9) if interval else None, "left_to_right",
                               timestamp, "generated-layout", token)
        maximum = FreshMaximum(item["maximum"], timestamp, "generated-layout", token,
                               True, FIELD, item["caption_current"])
        identity = TemporalIdentity(item["source"], item["session"], frame.size,
                                    "generated-layout", (0, 0, 10, 10))
        start = perf_counter()
        if variant in ("bar", "combined"):
            reading = cross_check(frame, evidence, reading, maximum)
        if variant in ("temporal", "combined"):
            bars = {}
            if variant == "combined" and interval:
                bound = validated_maximum(frame, evidence, maximum)
                bars[FIELD] = TemporalBarInterval(*interval, timestamp, "generated-layout", token,
                                                  maximum.value if bound else None, bound)
            reading = guard.check({FIELD: reading}, captured_at=timestamp, identity=identity,
                                  frame_token=token, bars=bars)[FIELD]
        elapsed = (perf_counter() - start) * 1000
        actual = reading.value["count"] if reading.quality is Quality.KNOWN else None
        output.append(dict(index=index, at=timestamp, expected=item["truth"],
                           candidate=candidate, current_output=actual, readable=item["readable"],
                           incorrect_known=actual is not None and actual != item["truth"],
                           unsafe_unknown_promotion=actual is not None and candidate is None,
                           accepted_nonadvancing_timestamp=actual is not None and not timestamp_advances,
                           unknown=actual is None, guard_ms=elapsed,
                           detail=reading.detail,
                           temporal=dict(reading.diagnostics.get("temporal", {}))))
    return output


def build_report():
    traces = {name: {v: run_trace(rows, v) for v in VARIANTS}
              for name, rows in scenarios().items()}
    summaries = {}
    for variant in VARIANTS:
        samples = [row for arms in traces.values() for row in arms[variant]]
        summaries[variant] = dict(
            frames=len(samples), incorrect_known=sum(row["incorrect_known"] for row in samples),
            unsafe_unknown_promotions=sum(row["unsafe_unknown_promotion"] for row in samples),
            accepted_nonadvancing_timestamps=sum(row["accepted_nonadvancing_timestamp"] for row in samples),
            unknown=sum(row["unknown"] for row in samples),
            max_guard_ms=max(row["guard_ms"] for row in samples),
            confirmations_ms=[row["temporal"]["confirmation_delay_s"] * 1000 for row in samples
                              if "confirmation_delay_s" in row["temporal"]])
    return dict(
        schema="tibiaauto.resource-fusion-generated-policy-traces", version=1,
        origin="generated_decision_inputs", native_sequence_count=0,
        scope="Hand-constructed current decoder outputs and independently supplied bar/max evidence. Exercises actual guard policy, not visual detection, OCR, capture, live error frequency or native recovery.",
        capture_interval_ms=50, confirmation_window_ms=250,
        interpretation="Counts describe this deliberately adversarial trace set only; never combine with original-image accuracy or claim measured gameplay improvement.",
        source_hashes={name: sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            "tools/resource_bar_evidence.py", "tools/resource_temporal_checks.py",
            "tools/benchmark_resource_fusion_sequences.py")},
        summaries=summaries, traces=traces)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    report = build_report()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summaries"], indent=2))


if __name__ == "__main__":
    main()
