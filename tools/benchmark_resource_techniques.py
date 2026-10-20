"""Focused component probes and whole-original comparisons for opt-in readers."""
from __future__ import annotations

import argparse
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import perf_counter

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.benchmark_resource_latency import _observation, _timings  # noqa: E402
from tools.compare_resource_readers import (  # noqa: E402
    AdaptiveTechnique, TechniqueObservation, environment, load_cases, score_case, summarize,
)
from tools.resource_reader_experiments import (  # noqa: E402
    VARIANTS, make_number_reader, make_reader,
)

MANIFEST = ROOT / 'tests/fixtures/screenshots/manifest.json'
FOCUS = (
    'user-paulistinha-20261013', 'dfg-calmera-ek246-D9BJ',
    'dfg-gentebra-BJEI', 'dfg-quintera-VS41', 'reddit-deleted-09s6sbl',
    'reddit-chrissykes-4ppe2dp', 'allegro-antica-4c39a50f',
    'dfg-gentebra-3XWB', 'dfg-gladera-SSVX', 'reddit-blake-m91oix5',
    'reddit-daralion-0tx21ye', 'reddit-dworc-o0ap20c',
    'dfg-belobra-2AHL', 'reddit-illustrious-mo9lbk5', 'dfg-yonabra-8LN9',
)


def fingerprint():
    names = ('tibia_client/adaptive.py', 'tibia_client/adaptive_text.py',
             'tools/resource_reader_experiments.py', 'tools/benchmark_resource_techniques.py',
             'tools/resource_numeric_vector.py',
             'tests/fixtures/screenshots/manifest.json')
    return {name:sha256((ROOT/name).read_bytes()).hexdigest() for name in names}


def numeric_probe(entries, variants, warm_reads, baseline_report):
    saved = {}
    if baseline_report:
        raw = json.loads(baseline_report.read_text())
        saved = {x['case_id']:x for x in raw['techniques']['adaptive_templates']['cases']}
    data = []
    for entry in entries:
        with Image.open(MANIFEST.parent / entry['file']) as image:
            pixels = np.asarray(image.convert('RGB'))
        annotations = entry.get('resource_annotations', {})
        ratios=[]
        widths={'resource_health':75,'resource_mana':40,'resource_soul':88,'resource_capacity':68}
        for field,annotation in annotations.items():
            if annotation['label_visibility']=='complete' and annotation['label_box']:
                x,_,r,_=annotation['label_box']
                ratios.append((r-x)/widths[field])
        fallback_scale=float(np.median(ratios)) if ratios else .8
        for field, truth in entry['labels'].items():
            if truth['value'] is None or not truth['box']:
                continue
            old=saved.get(entry['id'],{}).get('fields',{}).get(field,{})
            box=old.get('detected_region')
            scale=old.get('diagnostics',{}).get('scale',fallback_scale)
            family=old.get('diagnostics',{}).get('font_family')
            family=None if family in (None,'primary') else family
            if box:
                x,y,r,b=box
                scope='baseline_detected_region'
            else:
                x,y,r,b=truth['box']
                x,y,r,b=max(0,x-4),max(0,y-2),min(pixels.shape[1],r+3),min(pixels.shape[0],b+2)
                scope='annotation_derived_component_crop'
            crop=pixels[y:b,x:r]
            row={'id':entry['id'],'field':field,'expected':truth['value'],'scope':scope,
                 'region':[x,y,r,b],'scale':scale,'family':family,'variants':{}}
            for variant in variants:
                reader=make_number_reader(variant)
                values=[]; times=[]; reading=None; error=None
                for _ in range(warm_reads+1):
                    start=perf_counter()
                    try:
                        reading=reader.read(crop,scale=scale,max_digits=9,
                            allow_grouping=field!='resource_soul',family=family,use_ocr=False)
                        values.append(reading.value)
                    except ValueError as exc:
                        error=str(exc); values.append(None)
                    times.append((perf_counter()-start)*1000)
                value=values[-1]
                row['variants'][variant]={
                    'value':value,'exact':value==truth['value'],
                    'incorrect':value is not None and value!=truth['value'],
                    'unknown':value is None,'consistent':len(set(values))==1,
                    'detail':error or (reading.detail if reading else ''),
                    'first_ms':times[0],'cached':_timings(times[1:]),
                }
            data.append(row)
        print('numeric',entry['id'],flush=True)
    summary={}
    for variant in variants:
        rows=[r['variants'][variant] for r in data]
        summary[variant]={k:sum(r[k] for r in rows) for k in ('exact','incorrect','unknown')}
        summary[variant]['cached']=_timings([t for r in rows for t in r['cached']['samples_ms']])
        summary[variant]['wrong_cases']=[(r['id'],r['field'],r['variants'][variant]['value'],r['expected'])
                                       for r in data if r['variants'][variant]['incorrect']]
    return {'scope':'Component diagnosis with known numeric crops/scale; not full-frame acceptance',
            'cases':data,'summary':summary}


def full_probe(cases, variants, warm_reads, budget, checkpoint_path=None):
    output={variant:[] for variant in variants}
    for index,case in enumerate(cases):
        for variant in variants:
            start=perf_counter(); reader=make_reader(variant)
            init_ms=(perf_counter()-start)*1000
            started=perf_counter()
            discovery=reader.discover(case.frame,use_ocr=False)
            discovery_ms=(perf_counter()-started)*1000
            samples=[]; first_ms=None; readings={}; observed=[]
            if discovery.layout:
                for iteration in range(warm_reads+1):
                    started=perf_counter()
                    readings=AdaptiveTechnique._bounded(reader.read(case.frame,discovery.layout,use_ocr=False))
                    elapsed=(perf_counter()-started)*1000
                    if iteration==0:first_ms=elapsed
                    else:samples.append(elapsed)
                    observed.append(tuple((f,None if r.value is None else r.value['count']) for f,r in readings.items()))
                observation=_observation(readings,discovery.layout,discovery.detail)
            else:
                observation=TechniqueObservation('ambiguous' if discovery.candidates else 'missing',{},detail=discovery.detail)
            row=score_case(case,observation,supports_cached_layout=True)
            row.update(initialization_ms=init_ms,discovery_ms=discovery_ms,first_read_ms=first_ms,
                       cached_timings=_timings(samples),cached_outputs_consistent=len(set(observed))<=1,
                       cached_p95_budget_ms=budget,
                       cached_budget_met=bool(samples) and _timings(samples)['p95_ms']<=budget)
            output[variant].append(row)
            if checkpoint_path:
                with checkpoint_path.open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps({'variant':variant,'row':row})+'\n')
            correct=sum(v['exact'] for v in row['fields'].values())
            wrong=sum(v['incorrect_accepted'] for v in row['fields'].values())
            print(f'{index+1}/{len(cases)} {variant} {case.id} {row["panel_status"]} correct={correct} wrong={wrong} cold={discovery_ms:.1f} cached={row["cached_timings"]["p95_ms"]}',flush=True)
    return {'scope':'Original-only autonomous full-frame comparison',
            'techniques':{v:{'case_ids':[r['case_id'] for r in rows],'cases':rows,'summary':summarize(rows)}
                          for v,rows in output.items()}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=('numeric','full'),default='full')
    parser.add_argument('--variants',nargs='+',choices=tuple(VARIANTS),default=['baseline','fast'])
    parser.add_argument('--subset',choices=('all','focus'),default='focus')
    parser.add_argument('--ids',nargs='*')
    parser.add_argument('--warm-reads',type=int,default=10)
    parser.add_argument('--budget-ms',type=float,default=50)
    parser.add_argument('--threads',type=int,default=1)
    parser.add_argument('--shard',type=int,default=0)
    parser.add_argument('--shards',type=int,default=1)
    parser.add_argument('--baseline-report',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    cv2.setNumThreads(args.threads)
    if args.warm_reads < 1 or args.shards < 1 or not 0 <= args.shard < args.shards:
        parser.error('Positive warm reads and a valid shard index are required.')
    args.out.parent.mkdir(parents=True,exist_ok=True)
    before=fingerprint()
    document,cases=load_cases(MANIFEST,include_generated=False)
    ids=set(args.ids if args.ids else FOCUS) if args.subset=='focus' else {c.id for c in cases}
    entries=[e for e in document['screenshots'] if e['id'] in ids]
    cases=[c for c in cases if c.id in ids]
    entries=entries[args.shard::args.shards]; cases=cases[args.shard::args.shards]
    if args.mode=='numeric':
        result=numeric_probe(entries,args.variants,args.warm_reads,args.baseline_report)
    else:
        checkpoint=args.out.with_suffix('.jsonl')
        checkpoint.write_text('',encoding='utf-8')
        result=full_probe(cases,args.variants,args.warm_reads,args.budget_ms,checkpoint)
    assert fingerprint()==before,'Inputs/implementation changed during the experiment'
    result.update(environment=environment(),opencv_threads=cv2.getNumThreads(),fingerprints=before,
                  warm_reads=args.warm_reads,shard=args.shard,shards=args.shards,
                  reader_receives_annotations=False if args.mode=='full' else 'component crop/scale only',
                  features={v:VARIANTS[v] for v in args.variants},valid=True)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print('Saved',args.out,flush=True)


if __name__=='__main__':
    main()
