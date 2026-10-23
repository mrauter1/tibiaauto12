"""Replay frozen real screenshots through the bot's production visual readers.

This is an offline regression benchmark, not the resource promotion gate or a
live-input test. Calibration ROIs are explicit inputs for bars, targets and
markers. Full-frame minimap and Skills discovery receive no annotation boxes.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import platform
import statistics
import sys
import time

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.Getters import CalibrationError, GetMapPosition
from core.perception import frame_scope, validate_region
from engine.ScanStages import read_bar_region
from tibia_client.desktop import DesktopAdapter
from tibia_client.models import Quality, Reading, Sensor, SensorKind
from tools.compare_resource_readers import (
    AdaptiveTechnique, FIELDS, TechniqueObservation, _reading_value,
    load_cases, score_case, summarize,
)
from tools.screenshot_split import load_split

CORPUS = ROOT / 'tests/fixtures/screenshots'
DEFAULT_LABELS = CORPUS / 'visual-annotations-2026-10-22.json'
BAR_TOLERANCE = 2.0
MAP_IOU = .8
OCR_TIMEOUT = 2.0
DISCOVERY_TIMEOUT = 15.0


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def fingerprints(annotation_path):
    paths = [CORPUS/'manifest.json', CORPUS/'split-2026-10-17-latest.json', annotation_path,
             Path(str(annotation_path)+'.sha256')]
    for folder in ('core', 'engine', 'tibia_client', 'conf', 'tools'):
        paths.extend((ROOT/folder).rglob('*.py'))
    for folder in ('images', 'profiles/paulistinha-skills-v1'):
        paths.extend(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    return {p.relative_to(ROOT).as_posix(): digest(p) for p in sorted(set(paths))}


def timings(values):
    return {'samples':len(values), 'median_ms':statistics.median(values) if values else None,
            'p95_ms':float(np.percentile(values,95)) if values else None,
            'max_ms':max(values) if values else None}


def measure(call, repeats):
    started = time.perf_counter()
    value = call()
    cold_ms = (time.perf_counter()-started)*1000
    samples = []
    stable = True
    for _ in range(repeats):
        started = time.perf_counter()
        current = call()
        samples.append((time.perf_counter()-started)*1000)
        stable &= current == value
    return value, {'first_ms':cold_ms,'warm':timings(samples),'stable':bool(stable)}


def inside(point, box):
    return point is not None and box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]


def iou(a,b):
    if a is None or b is None:
        return 0.0
    intersection = max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    union = (a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection
    return intersection/union if union else 0.0


def load_annotations(path, cases):
    frozen = Path(str(path)+'.sha256').read_text().strip()
    if digest(path) != frozen:
        raise ValueError('Annotation hash differs from the pre-inference freeze')
    document = json.loads(path.read_text(encoding='utf-8'))
    if document.get('schema') != 'tibiaauto.visual-annotations' or document.get('box_format') != 'xyxy-half-open':
        raise ValueError('Unsupported visual annotation schema')
    rows = document['screenshots']
    if len(rows) != len(cases) or len({r['id'] for r in rows}) != len(rows):
        raise ValueError('Annotations must cover every corpus image exactly once')
    by_id = {r['id']:r for r in rows}
    for case in cases:
        row = by_id[case.id]
        if row['source_sha256'] != case.source_sha256:
            raise ValueError(f'Source changed: {case.id}')
        for kind in ('health_bar','mana_bar','minimap','battle_list','markers'):
            label = row[kind]
            for key in ('box','region'):
                if key in label:
                    validate_region(label[key], case.frame.size)
            if kind.endswith('_bar') and label['status'] == 'readable':
                if not 0 <= label['current'] <= label['maximum'] or label['maximum'] <= 0:
                    raise ValueError(f'Invalid bar ratio: {case.id}/{kind}')
                if abs(label['percent']-100*label['current']/label['maximum']) > 1e-8:
                    raise ValueError('Inconsistent percentage label')
            for item in label.get('rows',[]):
                validate_region(item['box'],case.frame.size)
            for query in label.get('queries',[]):
                for box in query['boxes']:
                    validate_region(box,case.frame.size)
    return document, by_id


class NoInput:
    """Fail closed if a future detector accidentally tries an input operation."""
    def __getattr__(self,name):
        raise RuntimeError(f'Input is unavailable in the screenshot benchmark: {name}')


def forbidden_capture():
    raise RuntimeError('Native capture is unavailable in the screenshot benchmark')


def visual_probe(case, labels, warm_reads):
    frame = case.frame
    output = {'bars':{},'minimap':{},'targets':[],'markers':[]}
    for kind in ('health','mana'):
        label = labels[kind+'_bar']
        if label['status'] != 'readable':
            output['bars'][kind] = {'scored':False, 'reason':label.get('reason',label['status'])}
            continue
        try:
            value, latency = measure(lambda:read_bar_region(frame, label['region'],kind),warm_reads)
            error = None if value is None else abs(value-label['percent'])
            output['bars'][kind] = {'scored':True,'expected':label['percent'],'actual':value,
                'absolute_error_pp':error,'pass':error is not None and error <= BAR_TOLERANCE,
                'unknown':value is None,'region':label['region'],'latency':latency,
                'threshold_decisions':{str(t):None if value is None else
                     (value<t)==(label['percent']<t) for t in (55,70,75,95)}}
        except Exception as exc:
            output['bars'][kind] = {'scored':True,'pass':False,'unknown':True,'error':str(exc)}
    # Actual automatic calibration function, under the original immutable frame.
    def locate_map():
        try:
            with frame_scope(frame):
                box = tuple(GetMapPosition())
            validate_region(box,frame.size)
            return box
        except CalibrationError:
            return None
    try:
        actual, latency = measure(locate_map, min(3,warm_reads))
        label = labels['minimap']
        expected = label.get('box')
        overlap = iou(actual,expected)
        output['minimap'] = {'expected_visible':expected is not None,'expected':expected,
            'actual':actual,'iou':overlap,'pass':overlap>=MAP_IOU if expected else actual is None,
            'latency':latency}
    except Exception as exc:
        output['minimap'] = {'expected_visible':labels['minimap']['status']=='visible',
                             'pass':False,'error':str(exc)}
    battle = labels['battle_list']
    if battle['status'] == 'reviewed':
        adapter = DesktopAdapter(0, hotkey=NoInput(),capture_provider=forbidden_capture,
                                 ui_scale=battle['ui_scale'])
        try:
            queries = sorted({r['name'] for r in battle['rows']} | set(battle['negative_queries']) | {'Rat'})
            queries = [q for q in queries if q not in battle.get('excluded_queries',[])]
            for name in queries:
                truth = [r for r in battle['rows'] if r['name']==name]
                sensor = Sensor(SensorKind.TARGET,tuple(battle['region']),name)
                def read_target():
                    try:
                        return adapter._target(sensor,frame)
                    except Exception as exc:
                        return Reading(None,Quality.ERROR,f'{type(exc).__name__}: {exc}')
                started = time.perf_counter()
                reading = read_target()
                initial_unknown = reading.quality is not Quality.KNOWN
                while (reading.quality is Quality.UNKNOWN and reading.detail=='reading battle-list creature names'
                       and time.perf_counter()-started < OCR_TIMEOUT):
                    time.sleep(.005)
                    reading = read_target()
                ready_ms = (time.perf_counter()-started)*1000
                value = reading.value if reading.quality is Quality.KNOWN else None
                count_ok = value is not None and value.count==len(truth)
                position_ok = value is not None and (any(inside(value.position,r['box']) for r in truth)
                                                     if truth else value.position is None)
                selected = any(r['selected'] for r in truth)
                selected_ok = value is not None and value.selected==selected
                _, latency = measure(read_target,warm_reads)
                output['targets'].append({'name':name,'expected_count':len(truth),
                    'expected_selected':selected,'expected_rows':truth,
                    'actual':None if value is None else asdict(value),'quality':reading.quality.value,
                    'detail':reading.detail,'diagnostics':dict(reading.diagnostics),
                    'count_pass':count_ok,'position_pass':position_ok,'selected_pass':selected_ok,
                    'pass':count_ok and position_ok and selected_ok,
                    'initial_unknown':initial_unknown,'ready_ms':ready_ms,'latency':latency})
        finally:
            adapter._battle_text.close()
    for query in labels['markers'].get('queries',[]):
        sensor = Sensor(SensorKind.MARKER,tuple(labels['markers']['region']),query['name'])
        try:
            scale = labels['markers'].get('ui_scale', battle.get('ui_scale', 1.0))
            reading, latency = measure(lambda:DesktopAdapter._marker(sensor,frame,scale),warm_reads)
            value = reading.value
            location_ok = (any(inside(value.position,b) for b in query['boxes']) if query['boxes']
                           else value.position is None)
            overlap = query['center_overlap']
            output['markers'].append({'name':query['name'],'expected_boxes':query['boxes'],
                'ui_scale':scale,
                'actual':asdict(value),'location_pass':location_ok,
                'expected_center_overlap':overlap,
                'center_overlap_pass':None if overlap is None else value.reached==overlap,
                'latency':latency})
        except Exception as exc:
            output['markers'].append({'name':query['name'],'expected_boxes':query['boxes'],
                'location_pass':False,'error':str(exc),'center_overlap_pass':False})
    return output


def skills_probe(case, reader_type, warm_reads):
    started = time.perf_counter()
    def checkpoint():
        if time.perf_counter()-started > DISCOVERY_TIMEOUT:
            raise TimeoutError('Skills discovery exceeded the cooperative 15 s budget')
    try:
        reader = reader_type()
        discovery = reader.discover(case.frame,use_ocr=False,checkpoint=checkpoint)
        discovery_ms = (time.perf_counter()-started)*1000
        if discovery.layout is None:
            observation = TechniqueObservation('ambiguous' if discovery.candidates else 'missing',{},
                discovery_ms=discovery_ms,cold_total_ms=discovery_ms,detail=discovery.detail)
            latency = None
        else:
            def read():
                return AdaptiveTechnique._bounded(reader.read(case.frame,discovery.layout,use_ocr=False))
            readings, latency = measure(read,warm_reads)
            observation = TechniqueObservation('found',
                {name:_reading_value(readings.get(name)) for name in FIELDS},
                dict(discovery.layout.regions),{k:v.quality.value for k,v in readings.items()},
                cold_total_ms=discovery_ms+latency['first_ms'],discovery_ms=discovery_ms,
                cached_ms=latency['warm']['median_ms'],detail=discovery.detail,
                field_details={k:v.detail for k,v in readings.items()},
                field_diagnostics={k:dict(v.diagnostics) for k,v in readings.items()})
        result = score_case(case,observation,supports_cached_layout=True)
        result['latency'] = latency
        return result
    except Exception as exc:
        return score_case(case,TechniqueObservation('error',{},detail=f'{type(exc).__name__}: {exc}',
            cold_total_ms=(time.perf_counter()-started)*1000),supports_cached_layout=True)


def visual_summary(rows):
    result={}
    for kind in ('health','mana'):
        bars=[r['visual']['bars'][kind] for r in rows if r['visual']['bars'][kind]['scored']]
        errors=[b['absolute_error_pp'] for b in bars if b.get('absolute_error_pp') is not None]
        result[kind+'_bar']={'scored':len(bars),'passed':sum(b['pass'] for b in bars),
            'unknown':sum(b['unknown'] for b in bars),'mae_pp':statistics.mean(errors) if errors else None,
            'max_error_pp':max(errors) if errors else None,
            'warm':timings([b['latency']['warm']['median_ms'] for b in bars if 'latency' in b])}
    maps=[r['visual']['minimap'] for r in rows]
    result['minimap']={label:{'scored':len(group),'passed':sum(r['pass'] for r in group)}
        for label,group in [('visible',[m for m in maps if m['expected_visible']]),
                            ('absent',[m for m in maps if not m['expected_visible']])]}
    targets=[t for r in rows for t in r['visual']['targets']]
    result['targets']={label:{'queries':len(group),'passed':sum(t['pass'] for t in group),
        'count_pass':sum(t['count_pass'] for t in group),'position_pass':sum(t['position_pass'] for t in group),
        'selected_pass':sum(t['selected_pass'] for t in group),'unknown':sum(t['actual'] is None for t in group)}
        for label,group in [('positive',[t for t in targets if t['expected_count']]),
                           ('negative',[t for t in targets if not t['expected_count']])]}
    result['targets']['expected_selected_positive_queries']=sum(t['expected_selected'] for t in targets)
    result['targets']['ready_latency']=timings([t['ready_ms'] for t in targets if t['initial_unknown']])
    markers=[m for r in rows for m in r['visual']['markers']]
    result['markers']={label:{'queries':len(group),'location_pass':sum(m['location_pass'] for m in group)}
        for label,group in [('positive',[m for m in markers if m['expected_boxes']]),
                           ('negative',[m for m in markers if not m['expected_boxes']])]}
    result['markers']['center_overlap']={'scored':sum(m.get('center_overlap_pass') is not None for m in markers),
        'passed':sum(m.get('center_overlap_pass') is True for m in markers),
        'positive_examples':sum(m.get('expected_center_overlap') is True for m in markers)}
    return result


def markdown(report):
    s=report['summary']['visual']
    lines=['# Visual detection benchmark — 2026-10-22','',
        'Offline regression on 81 original images. Detector and annotation hashes stayed fixed during this run.',
        'Bars, targets and markers receive manually calibrated regions. Minimap and Skills discovery search the original full frame.',
        'No native capture, input, transformed acceptance images, or test suite was run. This is not the resource-reader promotion gate.','',
        '| Detector | Scored | Passed | Criterion |','|---|---:|---:|---|']
    for kind in ('health','mana'):
        b=s[kind+'_bar'];lines.append(f'| {kind.title()} bar | {b["scored"]} | {b["passed"]} | Error ≤ 2 percentage points; unknown is a failure |')
    for category,label in [('visible','Minimap visible'),('absent','Minimap absent')]:
        b=s['minimap'][category];lines.append(f'| {label} | {b["scored"]} | {b["passed"]} | Automatic location IoU ≥ 0.8 / correct absence |')
    for category in ('positive','negative'):
        b=s['targets'][category];lines.append(f'| Battle targets, {category} queries | {b["queries"]} | {b["passed"]} | Count, click location and selection all correct |')
        b=s['markers'][category];lines.append(f'| Markers, {category} queries | {b["queries"]} | {b["location_pass"]} | Point inside a labelled icon / no point |')
    lines+=['','## Skills numeric regression','',
            '| Production reader | Eligible fields | Exact value and location | Incorrect accepted | Unknown |',
            '|---|---:|---:|---:|---:|']
    for name,summary in report['summary']['skills'].items():
        a=summary['aggregate'];lines.append(f'| {name} | {a["eligible_fields"]} | {a["numeric_exact"]} | {a["incorrect_accepted"]} | {a["unknown"]} |')
    lines+=['','Skills rows use the existing corpus scoring and exclusions. Full screenshots and supplemental crops are separated in JSON.',
            'The SDK/default reader and Resource Monitor retained-layout reader are separate production paths; neither is substituted for the other.','',
            '## Coverage and limits','']
    if report.get('skills_provenance'):
        lines += ['Skills measurements were reused from the completed initial run after verifying unchanged production code, assets, corpus hashes and timing parameters. Only visual checks were rerun after the annotation erratum.', '']
    for erratum in report.get('annotation_errata',[]):
        lines += ['Annotation erratum: '+erratum['reason'], '']
    lines += ['- '+text for text in report['annotation_limits']]
    lines += ['- Existing development/test membership is preserved and reported separately; this run is regression evidence, not a new unseen test set.',
              '- Latency is original-image computation only. Cached timings repeat identical pixels; they do not measure capture freshness or live changing-frame behavior.',
              '- Zero-resource, scaled UI, coloured names and circular map icons remain in the denominator when independently labelled.','',
              '## Failed visual cases','']
    for r in report['cases']:
        failures=[]
        for kind,b in r['visual']['bars'].items():
            if b.get('scored') and not b['pass']:failures.append(f'{kind} bar: {b.get("actual")} vs {b.get("expected")}')
        if not r['visual']['minimap']['pass']:failures.append('automatic minimap location')
        for t in r['visual']['targets']:
            if not t['pass']:failures.append(f'target {t["name"]}: expected {t["expected_count"]}, got {t["actual"]}')
        for m in r['visual']['markers']:
            if not m['location_pass']:failures.append(f'marker {m["name"]}')
        if failures:lines.append(f'- `{r["id"]}`: '+ '; '.join(failures))
    lines+=['','Detailed predictions, timing samples, split summaries, exclusions and fingerprints are in the adjacent JSON report.','']
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--annotations',type=Path,default=DEFAULT_LABELS)
    parser.add_argument('--out',type=Path,default=ROOT/'reports/visual-detection-2026-10-22.json')
    parser.add_argument('--warm-reads',type=int,default=5)
    parser.add_argument('--skills-from',type=Path,
        help='Reuse a completed report after verifying unchanged Skills inputs/code; rerun every visual check')
    args=parser.parse_args()
    if args.warm_reads<1:parser.error('--warm-reads must be positive')
    cv2.setNumThreads(1)
    manifest,cases=load_cases(CORPUS/'manifest.json',include_generated=False)
    annotation_document,labels=load_annotations(args.annotations,cases)
    _,membership=load_split(CORPUS/'split-2026-10-17-latest.json',manifest['screenshots'])
    before=fingerprints(args.annotations)
    from tibia_client.resource_reader import ResourceReader
    from tibia_client.retained_resources import RetainedResourceReader
    readers={'sdk_current':ResourceReader,'monitor_retained':RetainedResourceReader}
    reused_skills={}
    skills_provenance=None
    if args.skills_from:
        previous=json.loads(args.skills_from.read_text(encoding='utf-8'))
        permitted_changes={Path(__file__).relative_to(ROOT).as_posix(),
                           args.annotations.relative_to(ROOT).as_posix(),
                           Path(str(args.annotations)+'.sha256').relative_to(ROOT).as_posix()}
        if (previous.get('schema')!='tibiaauto.visual-detection-benchmark'
                or previous.get('valid') is not True or previous.get('warm_reads')!=args.warm_reads
                or previous['parameters']['skills_discovery_cooperative_timeout_s']!=DISCOVERY_TIMEOUT):
            raise ValueError('Incompatible completed Skills measurements')
        prior_hashes=previous['fingerprints']
        if ({k:v for k,v in prior_hashes.items() if k not in permitted_changes}
                != {k:v for k,v in before.items() if k not in permitted_changes}):
            raise ValueError('Production code/assets or corpus changed; rerun Skills measurements')
        if [(r['id'],r['source_sha256']) for r in previous['cases']] != [(c.id,c.source_sha256) for c in cases]:
            raise ValueError('Prior Skills result does not cover the same original images')
        if any(set(r['skills'])!=set(readers) for r in previous['cases']):
            raise ValueError('Prior report is missing a production Skills reader')
        reused_skills={r['id']:r['skills'] for r in previous['cases']}
        skills_provenance={'report':args.skills_from.relative_to(ROOT).as_posix(),
                           'sha256':digest(args.skills_from),'started_at':previous['started_at'],
                           'completed_at':previous['completed_at']}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    checkpoint=args.out.with_suffix('.jsonl')
    checkpoint.write_text('',encoding='utf-8')
    rows=[]
    started=datetime.now(timezone.utc).isoformat()
    for index,case in enumerate(cases,1):
        print(f'{index}/{len(cases)} {case.id}',flush=True)
        visual=visual_probe(case,labels[case.id],args.warm_reads)
        skills=(reused_skills[case.id] if reused_skills else
                {name:skills_probe(case,reader,args.warm_reads) for name,reader in readers.items()})
        row={'id':case.id,'coverage':case.source_coverage,'evaluation_split':membership[case.id],
             'source_sha256':case.source_sha256,'visual':visual,'skills':skills}
        rows.append(row)
        with checkpoint.open('a',encoding='utf-8') as f:f.write(json.dumps(row,allow_nan=False)+'\n')
    after=fingerprints(args.annotations)
    # Revalidate original bytes as well as annotations and the transitive code/assets.
    load_cases(CORPUS/'manifest.json',include_generated=False)
    if before!=after:raise RuntimeError('Implementation, assets or labels changed during evaluation')
    summary={'visual':visual_summary(rows),
             'skills':{name:summarize([r['skills'][name] for r in rows]) for name in readers}}
    report={'schema':'tibiaauto.visual-detection-benchmark','version':1,'valid':True,
        'started_at':started,'completed_at':datetime.now(timezone.utc).isoformat(),
        'environment':{'python':platform.python_version(),'platform':platform.platform(),
                       'opencv':cv2.__version__,'numpy':np.__version__,'opencv_threads':1},
        'fingerprints':before,'annotations_sha256':digest(args.annotations),'warm_reads':args.warm_reads,
        'parameters':{'bar_tolerance_pp':BAR_TOLERANCE,'minimap_iou':MAP_IOU,
                      'battle_ocr_timeout_s':OCR_TIMEOUT,'skills_discovery_cooperative_timeout_s':DISCOVERY_TIMEOUT},
        'annotation_limits':annotation_document['limitations'],
        'annotation_errata':annotation_document.get('errata',[]),'skills_provenance':skills_provenance,
        'coverage':{kind:dict(Counter(a[kind]['status'] for a in labels.values()))
                    for kind in ('health_bar','mana_bar','minimap','battle_list','markers')},
        'summary':summary,
        'by_evaluation_split':{split:visual_summary([r for r in rows if r['evaluation_split']==split])
                               for split in sorted(set(membership.values()))},
        'by_source_coverage':{coverage:visual_summary([r for r in rows if r['coverage']==coverage])
                              for coverage in ('full_screenshot','supplemental_crop')},
        'cases':rows}
    args.out.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    args.out.with_suffix('.md').write_text(markdown(report),encoding='utf-8')
    print(json.dumps(summary['visual'],indent=2),flush=True)
    print(f'Saved {args.out}',flush=True)


if __name__=='__main__':
    main()
