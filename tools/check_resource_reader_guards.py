"""Run the selected resource reader's component and mutation safety checks.

These checks preserve exact-value, visibility and ambiguity contracts.  Full
original screenshot accuracy is evaluated separately by the benchmark.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import platform
import sys
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import pytest  # noqa: E402

SCHEMA = "tibiaauto.resource-reader-guards"
GUARD_MODULES = (
    "tests/test_adaptive_text.py",
    "tests/test_adaptive_resources.py",
    "tests/test_resource_row_geometry.py",
    "tests/test_resource_panel_guard.py",
    "tests/test_resource_reader_benchmark.py",
    "tests/test_resource_numeric_memoization.py",
    "tests/test_resource_numeric_cache.py",
    "tests/test_resource_discovery_cache.py",
    "tests/test_native_visual_guard.py",
    "tests/test_frame_ownership.py",
    "tests/test_adaptive_reader_lanes.py",
    "tests/test_visual_production_benchmark.py",
)
# A native build is required for this promotion gate: optional-native tests may
# skip in ordinary pytest, but this runner rejects every skipped guard case.
# No shortened module list can satisfy the selected-identity/provenance checks.
MIN_GUARD_TESTS = 246
EXCLUDE_ORIGINAL_ACCEPTANCE = (
    "not existing_regression_screenshot and not calmera_resampled and not manually_boxed"
)


class _CurrentReaderPlugin:
    def __init__(self):
        self.selected_nodeids = []
        self._originals = {}

    def pytest_collection_modifyitems(self, items):
        from tibia_client.resource_reader import NumberReader, ResourceReader

        for item in items:
            module = item.module
            if not module.__name__.endswith(("test_adaptive_text", "test_adaptive_resources")):
                continue
            for name, selected in (("NumberReader", NumberReader),
                                   ("ResourceReader", ResourceReader)):
                if hasattr(module, name):
                    key = (module, name)
                    self._originals.setdefault(key, getattr(module, name))
                    setattr(module, name, selected)

    def pytest_collection_finish(self, session):
        self.selected_nodeids = [item.nodeid for item in session.items]

    def restore(self):
        for (module, name), original in self._originals.items():
            setattr(module, name, original)


def _xml_counts(path):
    """Count leaf test cases rather than double-counting nested suites."""
    if not path.is_file():
        return None
    try:
        cases = list(ElementTree.parse(path).getroot().iter("testcase"))
    except ElementTree.ParseError:
        return None
    return {
        "tests": len(cases),
        "failures": sum(item.find("failure") is not None for item in cases),
        "errors": sum(item.find("error") is not None for item in cases),
        "skipped": sum(item.find("skipped") is not None for item in cases),
    }


def run_guards(out_dir: Path | None = None, *, junit: Path | None = None,
               metadata: Path | None = None) -> int:
    """Run one selected composition and write verifiable JUnit plus metadata.

    ``out_dir`` emits ``guards.xml`` and ``guard-metadata.json``.  Explicit
    ``junit``/``metadata`` paths also support callers that manage artifact names.
    The benchmark owns the shared source/input fingerprint inventory.
    """
    from tools.benchmark_resource_reader import fingerprints, revision, verify_revision

    if out_dir is not None:
        if junit is not None or metadata is not None:
            raise ValueError("out_dir cannot be combined with explicit artifact paths")
        out_dir = Path(out_dir)
        junit, metadata = out_dir / "guards.xml", out_dir / "guard-metadata.json"
    elif junit is not None:
        junit = Path(junit)
        metadata = Path(metadata) if metadata is not None else junit.with_suffix(".json")
    else:
        raise ValueError("out_dir or junit is required")
    if junit.resolve() == metadata.resolve():
        raise ValueError("JUnit and metadata paths must differ")
    junit.parent.mkdir(parents=True, exist_ok=True)
    metadata.parent.mkdir(parents=True, exist_ok=True)
    before, evaluated_revision = fingerprints(), revision()
    verify_revision(evaluated_revision, before)
    cv2.setNumThreads(1)
    plugin = _CurrentReaderPlugin()
    options = [*(str(ROOT / module) for module in GUARD_MODULES), "-q",
               "-p", "no:cacheprovider", "-k", EXCLUDE_ORIGINAL_ACCEPTANCE,
               "--junitxml", str(junit)]
    try:
        pytest_exit_code = int(pytest.main(options, plugins=[plugin]))
    finally:
        plugin.restore()
    after, ending_revision = fingerprints(), revision()
    verify_revision(ending_revision, after)
    counts = _xml_counts(junit)
    by_module = dict(Counter(nodeid.split("::", 1)[0].replace("\\", "/")
                             for nodeid in plugin.selected_nodeids))
    issues = []
    if before != after or evaluated_revision != ending_revision:
        issues.append("implementation, inputs or revision changed during the guard run")
    if pytest_exit_code:
        issues.append("pytest did not complete successfully")
    if counts is None:
        issues.append("JUnit output is missing or malformed")
    elif (counts["tests"] < MIN_GUARD_TESTS or counts["failures"]
          or counts["errors"] or counts["skipped"]):
        issues.append("JUnit is incomplete or contains failed, errored or skipped cases")
    if counts is not None and counts["tests"] != len(plugin.selected_nodeids):
        issues.append("JUnit case count differs from the selected test identities")
    if len(plugin.selected_nodeids) != len(set(plugin.selected_nodeids)):
        issues.append("duplicate selected test identities")
    if set(by_module) != set(GUARD_MODULES):
        issues.append("the complete required guard module set was not exercised")
    result = {
        "schema": SCHEMA, "version": 1, "reader_id": "current",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "revision": evaluated_revision, "revision_after": ending_revision,
        "fingerprints": before, "fingerprints_after": after,
        "modules": list(GUARD_MODULES), "minimum_guard_tests": MIN_GUARD_TESTS,
        "selected_nodeids": plugin.selected_nodeids,
        "selected_nodeids_sha256": sha256(json.dumps(
            plugin.selected_nodeids, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest(),
        "counts_by_module": by_module,
        "pytest_exit_code": pytest_exit_code,
        "runner_exit_code": pytest_exit_code or int(bool(issues)),
        "xml_sha256": sha256(junit.read_bytes()).hexdigest() if junit.is_file() else None,
        "counts": counts, "valid": not issues, "issues": issues,
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "opencv": cv2.__version__, "opencv_threads": cv2.getNumThreads()},
        "scope": "Component and mutation safety checks; full original accuracy is separate.",
        "excluded_acceptance_expression": EXCLUDE_ORIGINAL_ACCEPTANCE,
    }
    metadata.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Saved", junit, "and", metadata, flush=True)
    if issues:
        print("Invalid guard artifact:", "; ".join(issues), flush=True)
    return result["runner_exit_code"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--out-dir", type=Path)
    destination.add_argument("--junit", type=Path)
    parser.add_argument("--metadata", type=Path)
    args = parser.parse_args(argv)
    if args.metadata is not None and args.out_dir is not None:
        parser.error("--metadata requires --junit")
    return run_guards(args.out_dir, junit=args.junit, metadata=args.metadata)


if __name__ == "__main__":
    raise SystemExit(main())
