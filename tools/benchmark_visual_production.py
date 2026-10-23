"""Compare production checkouts using isolated, reproducible offline probes.

Run once per checkout and pair the resulting per-case discrete_outputs. The
correctness mode scores all 81 originals with independent cold readers. Timing
mode uses eight predeclared sources and records raw post-discovery samples.
Run timing invocations serially with other CPU work stopped. Neither mode
measures live capture, changing resource values, or GPU hardware.

Example:
  python tools/benchmark_visual_production.py --checkout /tmp/base --ref BASE_SHA \
    --python /workspace/tibiaauto12/.venv/bin/python --mode correctness --out base.json
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


HARNESS = Path(__file__).resolve()
TIMING_IDS = (
    'user-paulistinha-20261013', 'dfg-calmera-ek246-D9BJ',
    'dfg-gentebra-BJEI', 'dfg-gladera-SSVX', 'reddit-chrissykes-4ppe2dp',
    'reddit-deleted-09s6sbl', 'dfg-gentebra-3XWB', 'reddit-blake-m91oix5',
)
THREAD_ENV = ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
              'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS')
EXTRA_SOURCES = (
    'tibia_client/capture.py', 'core/native_visual_guard.py', 'core/native_visual_guard.c',
    'tools/resource_numeric_cache.py', 'tools/resource_discovery_cache.py',
    'tools/build_visual_native.py',
)


def discrete_outputs(row):
    """Exclude machine timings and floating scores from exact parity checks."""
    return {'panel_status': row['panel_status'], 'fields': {
        name: {key: field[key] for key in ('actual', 'quality', 'detected_region')}
        for name, field in row['fields'].items()}}


def checkout_revision(root):
    # Archive roots can live beneath another repository. Never report its HEAD.
    if not (root / '.git').exists():
        return None
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def native_metadata(root):
    if importlib.util.find_spec('core.native_visual_guard') is None:
        return {'available': False, 'reason': 'backend absent from checkout'}
    from core import native_visual_guard
    path = native_visual_guard.library_path()
    return {'available': native_visual_guard.available(),
            'path': str(path.relative_to(root)),
            'sha256': sha256(path.read_bytes()).hexdigest() if path.is_file() else None}


def fingerprints(root, benchmark, native):
    # Reuse the tracked benchmark's complete frozen input/source inventory.
    # Its helper also verifies every original against the corpus manifest.
    names = set(benchmark.fingerprints())
    names.update(name for name in EXTRA_SOURCES if (root / name).is_file())
    if native.get('sha256'):
        names.add(native['path'])
    return {name: sha256((root / name).read_bytes()).hexdigest() for name in sorted(names)}


def evaluate(args):
    root = args.checkout
    # The harness may live in the current checkout while evaluating an archive.
    # Imports must come exclusively from the requested checkout, not that harness.
    sys.path[:] = [str(root), *(path for path in sys.path if path and
                    Path(path).resolve() not in (HARNESS.parent, HARNESS.parent.parent, root))]
    import cv2
    from tools import benchmark_resource_reader as benchmark
    from tools.compare_resource_readers import environment, load_cases, summarize
    from tools.screenshot_split import load_split

    cv2.setNumThreads(1)
    harness_hash = sha256(HARNESS.read_bytes()).hexdigest()
    native = native_metadata(root)
    before = fingerprints(root, benchmark, native)
    revision = checkout_revision(root)
    manifest, cases = load_cases(root / 'tests/fixtures/screenshots/manifest.json',
                                 include_generated=False)
    if len(cases) != 81:
        raise ValueError('Expected the complete 81-original frozen corpus')
    _, membership = load_split(root / 'tests/fixtures/screenshots/split-2026-10-17-latest.json',
                               manifest['screenshots'])
    if args.mode == 'timing':
        indexed = {case.id: case for case in cases}
        cases = [indexed[name] for name in TIMING_IDS]
    warm = args.samples if args.mode == 'timing' else 0
    # probe's default background loop must also be disabled for cold correctness.
    benchmark.BACKGROUND_READS = warm
    rows = []
    checkpoint = args.out.with_suffix('.jsonl')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text('', encoding='utf-8')
    for case in cases:
        row = benchmark.probe(case, warm_reads=warm)
        row['evaluation_split'] = membership[case.id]
        row['discrete_outputs'] = discrete_outputs(row)
        rows.append(row)
        with checkpoint.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, allow_nan=False) + '\n')
        print(case.id, row['panel_status'], flush=True)
    after_native = native_metadata(root)
    after = fingerprints(root, benchmark, after_native)
    if (before != after or revision != checkout_revision(root) or native != after_native
            or harness_hash != sha256(HARNESS.read_bytes()).hexdigest()):
        raise ValueError('Production sources, inputs, revision or native backend changed during run')
    result = dict(
        schema='tibiaauto.visual-production-comparison.v1', valid=True,
        mode=args.mode, checkout=str(root), supplied_ref=args.ref, revision=revision,
        harness_sha256=harness_hash,
        fingerprints=before, environment=environment(), python_executable=sys.executable,
        thread_environment={name: os.environ.get(name) for name in THREAD_ENV},
        opencv_threads=cv2.getNumThreads(), native_backend=native,
        requested_case_ids=[case.id for case in cases], warm_reads=warm, background_reads=warm,
        original_score_scope='Cold independent discovery and first read on unchanged originals',
        timing_scope=('Timings excluded: correctness invocations may overlap' if not warm else
                      'Post-discovery reads on newly constructed Frames; one outside-evidence '
                      'background pixel changed per background sample. Capture, frame '
                      'construction, discovery and initialization excluded from read samples.'),
        timing_valid=args.mode == 'timing', reader_receives_annotations=False,
        summary=summarize(rows), cases=rows,
    )
    args.out.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return 0


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--checkout', type=Path, required=True)
    result.add_argument('--python', default=sys.executable,
                        help='Python environment used for the isolated checkout subprocess')
    result.add_argument('--ref', required=True, help='Evaluated commit/ref, including for Git archives')
    result.add_argument('--mode', choices=('correctness', 'timing'), required=True)
    result.add_argument('--samples', type=int, default=20)
    result.add_argument('--out', type=Path, required=True)
    result.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    args.checkout, args.out = args.checkout.resolve(), args.out.resolve()
    if not 1 <= args.samples <= 1000:
        raise ValueError('samples must be from 1 through 1000')
    if not (args.checkout / 'tools/benchmark_resource_reader.py').is_file():
        raise ValueError('checkout lacks the production resource-reader benchmark')
    if args.child:
        return evaluate(args)
    env = {**os.environ, **{name: '1' for name in THREAD_ENV},
           'PYTHONHASHSEED': '0', 'PYTHONPATH': str(args.checkout)}
    command = [args.python, str(HARNESS), '--child', '--checkout', str(args.checkout),
               '--ref', args.ref, '--mode', args.mode, '--samples', str(args.samples),
               '--out', str(args.out)]
    return subprocess.run(command, cwd=args.checkout, env=env).returncode


if __name__ == '__main__':
    raise SystemExit(main())
