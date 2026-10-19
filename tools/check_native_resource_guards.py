"""Run existing pixel-mutation diagnostics against one native experiment.

These deliberately altered images are diagnostics, not real acceptance data.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import pytest  # noqa: E402
from tools.resource_native_experiments import FACTORS, make_number_reader, make_reader  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=tuple(FACTORS), required=True)
    parser.add_argument("--junit", type=Path)
    args = parser.parse_args()
    cv2.setNumThreads(1)

    class Numbers:
        def __new__(cls, **kwargs):
            return make_number_reader(args.variant, **kwargs)

    class Resources:
        def __new__(cls, **kwargs):
            return make_reader(args.variant, **kwargs)

    class Plugin:
        def pytest_collection_modifyitems(self, items):
            for item in items:
                if item.module.__name__.endswith(("test_adaptive_text", "test_adaptive_resources")):
                    item.module.NumberReader = Numbers
                    if hasattr(item.module, "ResourceReader"):
                        item.module.ResourceReader = Resources

    options = ["tests/test_adaptive_text.py", "tests/test_adaptive_resources.py", "-q",
               "-p", "no:cacheprovider", "-k",
               "not existing_regression_screenshot and not calmera_resampled and not manually_boxed"]
    if args.junit:
        args.junit.parent.mkdir(parents=True, exist_ok=True)
        options += ["--junitxml", str(args.junit)]
    return pytest.main(options, plugins=[Plugin()])


if __name__ == "__main__":
    raise SystemExit(main())
