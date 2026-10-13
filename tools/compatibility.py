"""Create, inspect, confirm, validate and export desktop compatibility evidence.

This command is headless: it never initializes Tk, capture, native window APIs,
or an input backend.  Live actions must be started by the Compatibility window
through the shared ClientSession.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from conf.conf_manager import ConfManager
from tibia_client.compatibility import (
    CompatibilityError,
    CompatibilityProfile,
    EnvironmentMetadata,
    OPERATIONS,
    Outcome,
    context_from_calibration,
    export_evidence_bundle,
    load_profile,
    save_profile,
    validate_profile_context,
)


def _size(value: str) -> tuple[int, int]:
    try:
        width, height = value.lower().split("x", 1)
        result = int(width), int(height)
    except (AttributeError, ValueError):
        raise argparse.ArgumentTypeError("size must use WIDTHxHEIGHT") from None
    if min(result) <= 0:
        raise argparse.ArgumentTypeError("size values must be positive")
    return result


def _metadata(args) -> EnvironmentMetadata:
    desktop = ConfManager.get("conf.json", default={})
    return EnvironmentMetadata(
        client_build=args.client_build,
        ot_server=args.ot_server,
        windows_version=args.windows_version,
        renderer=args.renderer,
        obs_version=args.obs_version,
        obs_configuration=args.obs_configuration,
        dpi_scaling=args.dpi_scaling,
        layout=args.layout,
        input_mode=args.input_mode,
        capture_mode=args.capture_mode,
        test_date=args.test_date,
        battleye_state=args.battleye_state,
        battleye_provenance=args.battleye_provenance,
        capture_title=desktop.get("capture_title", ""),
        input_title=desktop.get("input_title", ""),
    )


def _profile_dict(profile: CompatibilityProfile) -> dict:
    data = profile.to_dict()
    data["capture_status"] = profile.capture_outcome().value
    data["alignment_status"] = profile.alignment_outcome().value
    data["operation_status"] = {
        operation: profile.operation_outcome(operation).value
        for operation in profile.declared_operations
    }
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("init", help="create a new profile from saved calibration")
    create.add_argument("calibration_profile", help="plain JSON filename in scripts/")
    create.add_argument("--output", help="plain output JSON filename in scripts/")
    create.add_argument("--client-build", required=True)
    create.add_argument("--ot-server", required=True)
    create.add_argument("--windows-version", required=True)
    create.add_argument("--renderer", required=True)
    create.add_argument("--obs-version", required=True)
    create.add_argument("--obs-configuration", required=True)
    create.add_argument("--dpi-scaling", required=True)
    create.add_argument("--layout", required=True)
    create.add_argument("--input-mode", choices=("physical", "messages"), required=True)
    create.add_argument(
        "--capture-mode", choices=("obs_projector", "direct_client"), default="obs_projector"
    )
    create.add_argument("--input-size", type=_size, help="input WIDTHxHEIGHT; defaults to capture")
    create.add_argument("--test-date", default=__import__("datetime").date.today().isoformat())
    create.add_argument("--battleye-state", default="unknown")
    create.add_argument("--battleye-provenance", default="operator-reported")
    create.add_argument(
        "--operation", action="append", choices=OPERATIONS, dest="operations",
        help="declare an implemented operation; repeat as needed (default: all)",
    )

    show = commands.add_parser("show", help="print profile and effective operation results")
    show.add_argument("profile")

    confirm = commands.add_parser(
        "confirm", help="record an operator result for the latest real probe evidence"
    )
    confirm.add_argument("profile")
    confirm.add_argument("operation", choices=OPERATIONS)
    confirm.add_argument("result", choices=tuple(item.value for item in Outcome))
    confirm.add_argument("--detail", default="")

    validate = commands.add_parser("validate", help="check calibration/context and required ops")
    validate.add_argument("profile")
    validate.add_argument(
        "--require", action="append", choices=OPERATIONS, default=[], dest="required"
    )

    export = commands.add_parser("export", help="write a new bounded evidence directory")
    export.add_argument("profile")
    export.add_argument("destination")
    export.add_argument("--frame", action="append", default=[])
    export.add_argument("--trace")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            calibration = ConfManager.get(args.calibration_profile)
            context = context_from_calibration(
                calibration, _metadata(args), input_size=args.input_size
            )
            profile = CompatibilityProfile(
                args.calibration_profile,
                context,
                tuple(args.operations or OPERATIONS),
            )
            path = save_profile(profile, args.output)
            print(path)
            print("All operation results are unverified until an operator runs probes.")
            return 0
        if args.command == "show":
            print(json.dumps(_profile_dict(load_profile(args.profile)), indent=2))
            return 0
        if args.command == "confirm":
            profile = load_profile(args.profile)
            selected = next(
                (
                    (index, item)
                    for index, item in reversed(tuple(enumerate(profile.evidence)))
                    if item.operation == args.operation
                    and item.context_fingerprint == profile.context.fingerprint
                ),
                None,
            )
            if selected is None:
                raise CompatibilityError(
                    "no real probe evidence exists for this operation/context; run it in the GUI first"
                )
            index, evidence = selected
            detail = args.detail.strip() or evidence.detail
            updated = replace(
                evidence, operator_confirmation=Outcome(args.result), detail=detail
            )
            items = list(profile.evidence)
            items[index] = updated
            profile = CompatibilityProfile(
                profile.calibration_profile,
                profile.context,
                profile.declared_operations,
                tuple(items),
                profile.alignment_evidence,
                profile.capture_checks,
            )
            save_profile(profile, args.profile, overwrite=True)
            print(f"{args.operation}: {updated.outcome.value}")
            return 0
        if args.command == "validate":
            profile = load_profile(args.profile)
            calibration = ConfManager.get(profile.calibration_profile)
            current = context_from_calibration(
                calibration,
                profile.context.environment,
                input_size=profile.context.input_size,
            )
            check = validate_profile_context(profile, current, args.required)
            print(json.dumps({"ready": check.allowed, "reasons": check.reasons}, indent=2))
            return 0 if check.allowed else 1
        if args.command == "export":
            path = export_evidence_bundle(
                load_profile(args.profile),
                Path(args.destination),
                frame_files=args.frame,
                trace_file=args.trace,
            )
            print(path)
            return 0
    except (CompatibilityError, FileExistsError, OSError, TypeError, ValueError) as exc:
        print(f"compatibility: {exc}", file=sys.stderr)
        return 2
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
