"""Validate six frozen production artifacts and write their paired analysis.

Run: python reports/production-visual-optimizations/analyze.py [--inputs DIRECTORY]
No inference is rerun. Ratios are current/base; percentiles use linear interpolation.
"""
import argparse
from hashlib import sha256
import json
import math
from pathlib import Path
from statistics import geometric_mean, median

ROOT = Path(__file__).resolve().parents[2]
REFS = {'base': 'd3731316a69e4e96c0d422d870f562723710a6b9',
        'current': 'd00c48ff0185b214a716814094cd066d84a6aa79'}
TIMING_IDS = {'user-paulistinha-20261013', 'dfg-calmera-ek246-D9BJ',
              'dfg-gentebra-BJEI', 'dfg-gladera-SSVX', 'reddit-chrissykes-4ppe2dp',
              'reddit-deleted-09s6sbl', 'dfg-gentebra-3XWB', 'reddit-blake-m91oix5'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load(path, side, mode):
    doc = json.loads(path.read_text())
    require(doc['valid'] is True and doc['mode'] == mode, f'{path}: invalid mode/run')
    require(doc['schema'] == 'tibiaauto.visual-production-comparison.v1', 'Unknown schema')
    supplied, revision = doc['supplied_ref'], doc['revision']
    require((revision is None and supplied == REFS[side]) or
            (revision == REFS[side] and len(supplied) >= 7 and REFS[side].startswith(supplied)),
            f'{path}: unexpected revision')
    rows = {row['case_id']: row for row in doc['cases']}
    require(len(rows) == len(doc['cases']) == len(doc['requested_case_ids'])
            and set(rows) == set(doc['requested_case_ids']),
            f'{path}: duplicate/missing case IDs')
    require(len(rows) == (81 if mode == 'correctness' else 8), f'{path}: wrong case count')
    require(doc['reader_receives_annotations'] is False, f'{path}: annotations reached reader')
    for row in rows.values():
        require(row['transform'] == 'original', f'{path}: generated accuracy input')
        outputs = dict(panel_status=row['panel_status'], fields={name: {
            key: field[key] for key in ('actual', 'quality', 'detected_region')}
            for name, field in row['fields'].items()})
        require(outputs == row['discrete_outputs'], f'{path}: inconsistent output signature')
    return doc, rows


def identity(row):
    return [row[key] for key in ('source_id', 'source_sha256', 'case_sha256',
                                'expected_panel', 'acceptance_role', 'evaluation_split')]


def score_truth(row):
    keys = ('eligible', 'expected_unknown', 'expected', 'expected_region', 'accepted',
            'exact', 'incorrect_accepted', 'numeric_mismatch_accepted', 'accepted_location_mismatch',
            'unknown', 'safe_expected_unknown', 'unsafe_expected_unknown_acceptance',
            'location_coverage', 'other_field_overlap_max', 'other_field_centers_contained',
            'location_corresponds')
    return (row['eligible'], row['false_panel_acceptance'],
            {name: {key: field[key] for key in keys} for name, field in row['fields'].items()})


def summary(rows):
    fields = [field for row in rows for field in row['fields'].values()]
    eligible = [field for field in fields if field['eligible']]
    controls = [row for row in rows if row['acceptance_role'].endswith('_safety_control')]
    return dict(cases=len(rows), eligible_fields=len(eligible),
                **{key: sum(field[key] for field in eligible)
                   for key in ('exact', 'unknown', 'incorrect_accepted', 'accepted_location_mismatch')},
                unsafe_expected_unknown=sum(f['unsafe_expected_unknown_acceptance'] for f in fields),
                controls=len(controls), controls_all_unknown=sum(all(
                    f['quality'] == 'unknown' for f in row['fields'].values()) for row in controls),
                controls_without_panel=sum(row['panel_status'] != 'found' for row in controls),
                reader_errors=sum(row['panel_status'] == 'error' for row in rows),
                unexpected_panels=[row['case_id'] for row in rows
                                   if not row['expected_panel'] and row['panel_status'] == 'found'])


def pooled(samples):
    ordered = sorted(samples)
    index = (len(ordered) - 1) * .95
    lower = int(index)
    p95 = ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (index - lower)
    return dict(samples=len(samples), p50_ms=median(samples), p95_ms=p95)


def analyze(directory):
    docs, rows, provenance = {}, {}, {}
    for side in REFS:
        for suffix in ('correctness', 'timing-1', 'timing-2'):
            name = f'{side}-{suffix}'
            path = directory / f'{name}.json'
            docs[name], rows[name] = load(path, side, suffix.split('-')[0])
            provenance[path.name] = sha256(path.read_bytes()).hexdigest()
    original = rows['base-correctness']
    require(set(original) == set(rows['current-correctness']), 'Original IDs differ')
    inputs = lambda doc: {k: v for k, v in doc['fingerprints'].items()
                          if k.startswith('tests/fixtures/screenshots/')}
    require(inputs(docs['base-correctness']) == inputs(docs['current-correctness']),
            'Original input/manifest/split hashes differ')
    for name, group in rows.items():
        side = name.split('-')[0]
        require(all(docs[name][key] == docs['base-correctness'][key]
                    for key in ('environment', 'python_executable')),
                f'{name}: recorded environment/Python differs')
        require(docs[name]['native_backend'] == docs[f'{side}-correctness']['native_backend'],
                f'{name}: loaded native backend metadata changed')
        require(docs[name]['fingerprints'] == docs[f'{side}-correctness']['fingerprints'],
                f'{name}: implementation/input fingerprints changed between runs')
        require(docs[name]['harness_sha256'] == docs['base-correctness']['harness_sha256'],
                f'{name}: comparison harness changed')
        for case, row in group.items():
            require(identity(row) == identity(original[case]), f'{name}/{case}: input mismatch')
            require(row['discrete_outputs'] == original[case]['discrete_outputs'],
                    f'{name}/{case}: discrete output mismatch')
            require(score_truth(row) == score_truth(original[case]),
                    f'{name}/{case}: truth/scoring flags differ despite output parity')
    result = dict(schema='tibiaauto.production-visual-analysis.v1', refs=REFS,
                  artifact_sha256=provenance, original_output_mismatches=0,
                  environment=docs['base-correctness']['environment'],
                  python_executable=docs['base-correctness']['python_executable'],
                  native_backends={side: docs[f'{side}-correctness']['native_backend'] for side in REFS},
                  correctness={side: summary(list(rows[f'{side}-correctness'].values())) for side in REFS},
                  timing={}, timing_output_mismatches=0)
    result['inherited_unexpected_panels'] = result['correctness']['base']['unexpected_panels']
    for mode, key, flag in (('stable', 'new_frame_timings', 'repeated_outputs_consistent'),
                            ('background', 'background_timings', 'background_outputs_consistent')):
        pairs, samples = [], {side: [] for side in REFS}
        for block in (1, 2):
            for side in REFS:
                doc = docs[f'{side}-timing-{block}']
                require(set(rows[f'{side}-timing-{block}']) == TIMING_IDS, 'Timing source IDs differ')
                require(doc['timing_valid'] is True and doc['warm_reads'] == doc['background_reads'] == 20,
                        'Wrong timing sample configuration')
                require(doc['opencv_threads'] == 1 and set(doc['thread_environment'].values()) == {'1'},
                        'Timing thread limits differ from the frozen protocol')
            for case in sorted(TIMING_IDS):
                values = {}
                for side in REFS:
                    row = rows[f'{side}-timing-{block}'][case]
                    data = row[key]['samples_ms']
                    require(row[flag] is True, f'{side}/{case}/{mode}: repeated output mismatch')
                    require(len(data) == row[key]['sample_count'] == 20 and all(
                        type(v) in (int, float) and math.isfinite(v) and v > 0 for v in data),
                        f'{side}/{case}/{mode}: invalid timing samples')
                    values[side] = median(data)
                    samples[side].extend(data)
                pairs.append(dict(case_id=case, block=block, base_ms=values['base'],
                                  current_ms=values['current'], ratio=values['current'] / values['base']))
        ratios = [pair['ratio'] for pair in pairs]
        result['timing'][mode] = dict(case_block_pairs=pairs, paired_geomean_ratio=geometric_mean(ratios),
                                     paired_median_ratio=median(ratios), paired_ratio_min=min(ratios),
                                     paired_ratio_max=max(ratios), pooled={side: pooled(data)
                                                                         for side, data in samples.items()})
    result['caveats'] = [
        'Eight predeclared sources, two blocks, 20 stable and 20 background reads per source/block.',
        'Repetitions are correlated; timing inputs keep numeric pixels unchanged, with no fresh values.',
        'Read-only CPU replay excludes capture, Frame construction, initialization and discovery.',
        'Combined production defaults: these read timings do not attribute gains to individual techniques.',
        'Correctness invocation timings are excluded because those invocations may overlap.',
        'Windows live capture and GPU hardware results remain unmeasured.',
        'Zero timing mismatches means all recorded sequence-consistency flags passed; individual outputs are not stored.']
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, default=ROOT / '.work/production-visual')
    parser.add_argument('--out', type=Path, default=Path(__file__).with_name('analysis.json'))
    args = parser.parse_args()
    args.out.write_text(json.dumps(analyze(args.inputs), indent=2, allow_nan=False) + '\n')
    print(args.out)
