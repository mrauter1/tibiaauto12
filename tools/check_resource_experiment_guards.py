"""Run existing recognition safety diagnostics against an opt-in experiment.

Uses existing tests unchanged; it replaces their constructors only inside this
pytest process. Historical generated diagnostics are not acceptance screenshots.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

import cv2  # noqa: E402
import pytest  # noqa: E402
from tools.resource_reader_experiments import VARIANTS, ExperimentalNumberReader, ExperimentalResourceReader  # noqa: E402


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variant',choices=tuple(VARIANTS),required=True)
    parser.add_argument('--numeric-only',action='store_true')
    parser.add_argument('--junit',type=Path)
    args=parser.parse_args()
    features=VARIANTS[args.variant]
    cv2.setNumThreads(1)
    from tools.resource_numeric_vector import VectorNumberReader
    base=VectorNumberReader if 'vector' in features else ExperimentalNumberReader

    class Numbers(base):
        supports_font_family=True

        def __init__(self,**kwargs):
            super().__init__(features,**kwargs)

    class Resources(ExperimentalResourceReader):
        def __init__(self,**kwargs):
            self.features=frozenset(features)
            number_reader=kwargs.pop('number_reader',None) or Numbers()
            from tibia_client.adaptive import ResourceReader
            ResourceReader.__init__(self,number_reader=number_reader,**kwargs)

    class Plugin:
        def pytest_collection_modifyitems(self,items):
            for item in items:
                if item.module.__name__.endswith(('test_adaptive_text','test_adaptive_resources')):
                    item.module.NumberReader=Numbers
                    if hasattr(item.module,'ResourceReader'):
                        item.module.ResourceReader=Resources

    options=['tests/test_adaptive_text.py','-q','-p','no:cacheprovider']
    if not args.numeric_only:
        options+=['tests/test_adaptive_resources.py','-k',
                  'not existing_regression_screenshot and not calmera_resampled and not manually_boxed']
    if args.junit:
        args.junit.parent.mkdir(parents=True,exist_ok=True)
        options+=['--junitxml',str(args.junit)]
    return pytest.main(options,plugins=[Plugin()])


if __name__=='__main__':
    raise SystemExit(main())
