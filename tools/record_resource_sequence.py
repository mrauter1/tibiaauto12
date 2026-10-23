#!/usr/bin/env python3
"""Capture, import, or annotate real full-frame resource replay sessions."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from io import BytesIO
import json
from hashlib import sha256
from pathlib import Path
import platform
import sys
from time import sleep
from typing import Any, Mapping

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.perception import Frame  # noqa: E402
from tibia_client.capture import CaptureProcess  # noqa: E402
from tibia_client.resource_sequences import (  # noqa: E402
    _MAX_FRAME_BYTES,
    _MAX_SESSION_PIXEL_BYTES,
    RESOURCE_FIELDS,
    ResourceSequenceRecorder,
    load_resource_sequence,
    unreviewed_truth,
)


def _provenance(args: argparse.Namespace, provider: str) -> dict[str, Any]:
    return {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "client_build": args.client_build,
        "client_family": args.client_family,
        "operating_system": args.operating_system,
        "ui_scale": args.ui_scale,
        "display_scale": args.display_scale,
        "capture_provider": provider,
        "capture_coordinate_space": args.coordinate_space,
    }


def _jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    rows = []
    with path.open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: invalid JSON: {exc}") from None
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{number}: each line must be an object")
            rows.append(value)
    return rows


def _truth(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return unreviewed_truth()
    required = {"panel", "resources", "capture_freshness"}
    if not required.issubset(row):
        raise ValueError(f"annotation row omits {sorted(required - set(row))}")
    return {
        "panel": row["panel"],
        "resources": row["resources"],
        "capture_freshness": row["capture_freshness"],
    }


def _reviewed(row: Mapping[str, Any]) -> bool:
    return (
        row.get("panel", {}).get("annotation_status") == "reviewed"
        or row.get("capture_freshness", {}).get("annotation_status") == "reviewed"
        or any(
            item.get("annotation_status") == "reviewed"
            for item in row.get("resources", {}).values()
        )
        or any(item.get("annotation_status") == "reviewed" for item in row.get("events", ()))
    )


def _verify_frame_binding(row: Mapping[str, Any], frame: Frame) -> None:
    if not _reviewed(row):
        return
    expected = row.get("expected_pixel_sha256")
    dimensions = row.get("expected_dimensions")
    if expected != sha256(frame._rgb).hexdigest() or dimensions != [frame.width, frame.height]:
        raise ValueError(
            "reviewed annotation does not match captured expected_pixel_sha256/expected_dimensions"
        )


def capture(args: argparse.Namespace) -> Path:
    annotations = _jsonl(args.annotations)
    if annotations and len(annotations) != args.count:
        raise ValueError("annotation JSONL must have exactly --count rows")
    with ResourceSequenceRecorder(
        args.output,
        session_id=args.session_id,
        origin="native_capture",
        provenance=_provenance(args, "tibia_client.capture.CaptureProcess"),
    ) as recorder:
        with CaptureProcess(timeout=args.capture_timeout, startup_timeout=15.0) as process:
            for index in range(args.count):
                frame = process.capture(timeout=args.capture_timeout)
                row = annotations[index] if annotations else None
                if row is not None:
                    _verify_frame_binding(row, frame)
                truth = _truth(row)
                recorder.record(
                    frame,
                    **truth,
                    events=() if row is None else row.get("events", ()),
                )
                if index + 1 < args.count:
                    sleep(args.interval)
    return args.output


def import_native(args: argparse.Namespace) -> Path:
    rows = _jsonl(args.input)
    if not rows:
        raise ValueError("import JSONL is empty")
    provenance = _provenance(args, "saved-full-frame-import")
    provenance["imported_at_utc"] = provenance["recorded_at_utc"]
    provenance["recorded_at_utc"] = None
    provenance["source_assertion"] = args.source_assertion
    decoded_total = 0
    with ResourceSequenceRecorder(
        args.output,
        session_id=args.session_id,
        origin="imported_native",
        provenance=provenance,
    ) as recorder:
        for number, row in enumerate(rows, 1):
            if "path" not in row or "captured_at" not in row:
                raise ValueError(f"import row {number} needs path and captured_at")
            declared = Path(row["path"])
            if declared.is_absolute() or ".." in declared.parts:
                raise ValueError(f"import row {number}: path must stay under the JSONL directory")
            source_root = args.input.parent.resolve()
            image_path = (source_root / declared).resolve()
            if source_root not in image_path.parents:
                raise ValueError(f"import row {number}: path escapes the JSONL directory")
            try:
                size = image_path.stat().st_size
            except OSError as exc:
                raise ValueError(f"import row {number}: could not inspect source: {exc}") from None
            if not image_path.is_file() or size > _MAX_FRAME_BYTES:
                raise ValueError(f"import row {number}: source is not a bounded regular file")
            with image_path.open("rb") as stream:
                encoded = stream.read(_MAX_FRAME_BYTES + 1)
            if len(encoded) > _MAX_FRAME_BYTES:
                raise ValueError(f"import row {number}: encoded frame exceeds byte limit")
            if row.get("source_sha256") != sha256(encoded).hexdigest():
                raise ValueError(f"import row {number}: source_sha256 mismatch")
            with Image.open(BytesIO(encoded)) as image:
                width, height = image.size
                decoded_bytes = width * height * 3
                if width <= 0 or height <= 0 or decoded_bytes > _MAX_FRAME_BYTES:
                    raise ValueError(f"import row {number}: decoded frame exceeds byte limit")
                decoded_total += decoded_bytes
                if decoded_total > _MAX_SESSION_PIXEL_BYTES:
                    raise ValueError("import stream exceeds decoded-pixel session budget")
                frame = Frame(
                    image.convert("RGB"),
                    captured_at=row["captured_at"],
                    source=str(row.get("source", image_path)),
                )
            recorder.record(frame, **_truth(row), events=row.get("events", ()))
    return args.output


def annotate(args: argparse.Namespace) -> Path:
    """Create a reviewed version of a capture draft without mutating evidence."""

    sequence = load_resource_sequence(args.input)
    rows = _jsonl(args.annotations)
    if len(rows) != len(sequence.frames):
        raise ValueError("annotation JSONL must have exactly one row per recorded frame")
    with ResourceSequenceRecorder(
        args.output,
        session_id=sequence.session_id,
        origin=sequence.origin,
        provenance=sequence.provenance,
        source_lineage=sequence.source_lineage,
    ) as recorder:
        for captured, row in zip(sequence.frames, rows):
            _verify_frame_binding(row, captured.frame)
            recorder.record(
                captured.frame,
                **_truth(row),
                events=row.get("events", ()),
            )
    return args.output


def _metadata(parser: argparse.ArgumentParser, *, operating_system: str | None) -> None:
    parser.add_argument("--client-build")
    parser.add_argument("--client-family")
    parser.add_argument("--operating-system", default=operating_system)
    parser.add_argument("--ui-scale", type=float)
    parser.add_argument("--display-scale", type=float)
    parser.add_argument("--coordinate-space", default="client-local physical pixels")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    native = commands.add_parser("capture", help="capture frames through CaptureProcess")
    native.add_argument("output", type=Path)
    native.add_argument("--session-id", required=True)
    native.add_argument("--count", type=int, required=True)
    native.add_argument("--interval", type=float, default=0.1)
    native.add_argument("--capture-timeout", type=float, default=2.0)
    native.add_argument("--annotations", type=Path)
    _metadata(native, operating_system=platform.platform())
    imported = commands.add_parser("import-native", help="import a declared native full-frame stream")
    imported.add_argument("input", type=Path, help="JSONL with path, captured_at, and optional truth")
    imported.add_argument("output", type=Path)
    imported.add_argument("--session-id", required=True)
    imported.add_argument("--source-assertion", required=True, help="how native origin and timestamps were established")
    _metadata(imported, operating_system=None)
    annotated = commands.add_parser("annotate", help="bind reviewed truth to an existing capture draft")
    annotated.add_argument("input", type=Path)
    annotated.add_argument("annotations", type=Path)
    annotated.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "capture":
        if args.count <= 0 or args.interval < 0 or args.capture_timeout <= 0:
            parser.error("count and capture timeout must be positive; interval cannot be negative")
        output = capture(args)
    elif args.command == "import-native":
        output = import_native(args)
    else:
        output = annotate(args)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
