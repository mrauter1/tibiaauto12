"""Check installation, assets, saved calibration and routes without client access.

Run ``python -m tools.doctor --help`` from the repository. No capture provider,
keyboard hook, native window API, or input backend is initialized here.
"""
from __future__ import annotations

import argparse
from importlib import metadata
import json
import os
from pathlib import Path
import sys
from typing import Any

from core.paths import PROJECT_ROOT, scripts_path


def check_installation(*, assets: bool = False, profile: str | None = None,
                       route: str | None = None) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    def record(name: str, level: str, detail: str) -> None:
        checks.append({"check": name, "level": level, "detail": detail})

    supported_python = (3, 10) <= sys.version_info[:2] <= (3, 12)
    record("python", "ok" if supported_python else "warning",
           f"{sys.version.split()[0]}; CI covers Python 3.10 and 3.12")
    record("desktop", "ok" if os.name == "nt" else "warning",
           "Windows desktop available" if os.name == "nt" else
           "Desktop automation requires Windows; offline checks work here")
    packages = ["numpy", "opencv-python-headless", "Pillow"]
    if os.name == "nt":
        packages.extend(["pywin32", "keyboard", "PyAutoGUI", "PyGetWindow"])
    for package in packages:
        try:
            record(package, "ok", metadata.version(package))
        except metadata.PackageNotFoundError:
            record(package, "error", "missing; install requirements.txt")

    if assets:
        from PIL import Image
        files = sorted(p for p in (PROJECT_ROOT / "images").rglob("*")
                       if p.is_file() and p.suffix.lower() == ".png")
        broken = []
        for path in files:
            try:
                with Image.open(path) as image:
                    image.verify()
            except (OSError, ValueError) as exc:
                broken.append(f"{path.relative_to(PROJECT_ROOT)}: {exc}")
        if not files:
            broken.append("images directory contains no PNG assets")
        record("assets", "error" if broken else "ok",
               "; ".join(broken) if broken else f"{len(files)} PNG files decoded")

    if profile is not None:
        from core.Getters import validate_calibration
        try:
            # Read directly: a doctor check must not create config lock files.
            with scripts_path(profile).open(encoding="utf-8") as stream:
                data = json.load(stream)
            validate_calibration(data)
            record("calibration", "ok", f"{profile}: saved geometry is structurally valid")
        except (OSError, ValueError, TypeError) as exc:
            record("calibration", "error", str(exc))

    if route is not None:
        from engine.CaveBot.routes import load_checkpoint, load_route
        try:
            selected = load_route(route)
            if not selected.waypoints:
                raise ValueError("route is empty; add a Walk waypoint before starting")
            index = load_checkpoint(selected)
            record("route", "ok", f"{selected.name}: {len(selected.waypoints)} waypoints; resume index {index}")
        except (OSError, ValueError, TypeError) as exc:
            record("route", "error", str(exc))

    return {"ok": not any(item["level"] == "error" for item in checks),
            "mode": "offline", "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", action="store_true", help="decode every bundled PNG")
    parser.add_argument("--profile", help="saved calibration JSON filename in scripts/")
    parser.add_argument("--route", help="route name in scripts/ (with or without .json)")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)
    report = check_installation(assets=args.assets, profile=args.profile, route=args.route)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for item in report["checks"]:
            print(f"{item['level'].upper():7} {item['check']}: {item['detail']}")
        print("No live capture or input was performed.")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
