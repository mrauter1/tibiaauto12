"""Offline known-dialog veto evaluation from explicitly supplied saved frames."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tibia_client.probe_known_dialog import DEFAULT_THRESHOLD, create_template, evaluate_dataset


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template-source", type=Path, required=True)
    parser.add_argument("--template-box", type=int, nargs=4, required=True, metavar=("L", "T", "R", "B"))
    parser.add_argument("--viewport", type=int, nargs=4, required=True, metavar=("L", "T", "R", "B"))
    parser.add_argument("--name", default="known_dialog")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--positive", type=Path, action="append", default=[])
    parser.add_argument("--negative", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    destination = args.output.expanduser().resolve()
    inputs = [args.template_source, *args.positive, *args.negative]
    if destination in {path.expanduser().resolve() for path in inputs}:
        print(json.dumps({"report": None, "error": "output must differ from every input evidence path",
                          "overall_safety": "unknown"}, indent=2))
        return 2
    try:
        template = create_template(args.template_source, args.template_box,
                                   viewport=args.viewport, name=args.name)
        report = evaluate_dataset(template, args.positive, args.negative, threshold=args.threshold)
    except Exception as exc:
        report = {"schema": "tibiaauto.experimental-known-dialog-veto", "version": 1,
                  "summary": {"status": "unknown"}, "verdict": "unknown",
                  "error": f"{type(exc).__name__}: {exc}"[:2048],
                  "overall_safety": "unknown", "production_wiring": "none"}
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    except FileExistsError:
        print(json.dumps({"report": None, "error": "output exists; select a fresh evidence report path",
                          "overall_safety": "unknown"}, indent=2))
        return 2
    print(json.dumps({"report": str(destination), "summary": report["summary"],
                      "overall_safety": "unknown"}, indent=2))
    return 0 if report["summary"]["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
