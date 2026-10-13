"""Inspect a bounded TibiaAuto observation trace without replaying actions.

Run ``python -m tools.trace_viewer TRACE.jsonl`` for a terminal timeline or add
``--gui`` for the Tk viewer.  Neither mode initializes capture or input.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from tibia_client.trace_view import (
    DEFAULT_PRESENTATION_LIMIT,
    MAX_PRESENTATION_LIMIT,
    filter_events,
    load_trace_document,
)


def _clip(value: object, width: int) -> str:
    text = " ".join(str(value).splitlines())
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def _print_report(document, filtered) -> None:
    stats = document.statistics
    source = Path(document.source).name if document.source else "trace"
    print(
        f"{source}: {stats.total_events} events, {stats.duration:.3f}s, "
        f"{stats.requests} requests, {stats.dispatches} dispatches, "
        f"{stats.failures} failures"
    )
    if stats.kinds:
        print("Kinds: " + ", ".join(f"{key}={value}" for key, value in stats.kinds.items()))
    if stats.results:
        print(
            "Results: "
            + ", ".join(f"{key}={value}" for key, value in stats.results.items())
        )
    print(f"{'SEQ':>6} {'ELAPSED':>10} {'KIND':<12} {'OWNER':<18} {'REQUEST':<25} SUMMARY")
    for row in filtered.rows:
        print(
            f"{row.event.sequence:>6} {row.elapsed:>9.3f}s "
            f"{_clip(row.event.kind, 12):<12} {_clip(row.event.owner, 18):<18} "
            f"{_clip(row.request, 25):<25} {_clip(row.summary, 90)}"
        )
    suffix = f"; showing first {len(filtered.rows)}" if filtered.truncated else ""
    print(f"Matched {filtered.matched} event(s){suffix}.")
    print("Observation-only inspection; no actions were replayed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", nargs="?", help="complete trace-v1 JSONL file")
    parser.add_argument("--kind", default="", help="exact event kind (case-insensitive)")
    parser.add_argument("--owner", default="", help="owner name substring")
    parser.add_argument(
        "--request", default="", help="request id, key, action, state, or reason substring"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_PRESENTATION_LIMIT,
        help=f"maximum rows to show (1-{MAX_PRESENTATION_LIMIT})",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--gui", action="store_true", help="open the Tk timeline viewer")
    args = parser.parse_args(argv)

    if args.gui:
        # Keep headless CLI imports independent from Tk.
        from modules.TraceViewer import TraceViewer

        viewer = TraceViewer(trace_path=args.trace)
        viewer.run()
        return 0
    if not args.trace:
        parser.error("TRACE is required unless --gui is used")

    try:
        document = load_trace_document(args.trace)
        filtered = filter_events(
            document.events,
            kind=args.kind,
            owner=args.owner,
            request=args.request,
            limit=args.limit,
        )
    except (OSError, ValueError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"trace viewer: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "source": document.source,
                    "statistics": document.statistics.as_dict(),
                    "matched": filtered.matched,
                    "shown": len(filtered.rows),
                    "truncated": filtered.truncated,
                    "events": [row.as_dict() for row in filtered.rows],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        _print_report(document, filtered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
