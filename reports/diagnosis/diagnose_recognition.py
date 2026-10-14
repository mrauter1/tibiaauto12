import sys,json,collections,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.compare_resource_readers import load_cases
from tools.benchmark_resources import DEFAULT_MANIFEST
from tibia_client.adaptive import ResourceReader
class TraceReader(ResourceReader):
 def __init__(self):
  super().__init__();self.trace={}
 def _constellations(self,anchors):
  groups=super()._constellations(anchors)
  self.trace.setdefault('constellations',[]).append({'anchors':len(anchors),'by_field':dict(collections.Counter(a.field for a in anchors)),'groups':len(groups),'group_fields':[[a.field for a in g] for g in groups]})
  return groups
 def _layout(self,frame,anchors):
  r=super()._layout(frame,anchors)
  self.trace.setdefault('layouts',[]).append({'fields':[a.field for a in anchors],'scale':[a.scale for a in anchors],'methods':[a.method for a in anchors],'result':r is not None,'regions':list(r.regions) if r else []})
  return r
report=json.load(open('reports/resource-benchmark.json'))
selected={r['case_id']:r for r in report['techniques']['adaptive_templates_ocr']['cases'] if any(v['eligible'] and v['unknown'] for v in r['fields'].values())}
_,cases=load_cases(DEFAULT_MANIFEST)
reader=TraceReader(); count=0
with open('reports/diagnosis/failure-traces.jsonl','w') as out:
 for case in cases:
  if case.id not in selected: continue
  reader.trace={}; start=time.monotonic()
  result=reader.discover(case.frame,use_ocr=True)
  readings=reader.read(case.frame,result.layout,use_ocr=True) if result.layout else {}
  record={'case_id':case.id,'source_id':case.source_id,'transform':case.transform,'split':case.split,'prior_panel_status':selected[case.id]['panel_status'],'panel_found':result.layout is not None,'detail':result.detail,'trace':reader.trace,'readings':{k:{'value':v.value,'quality':v.quality.value,'detail':v.detail,'diagnostics':dict(v.diagnostics),'expected':case.labels[k]['value']} for k,v in readings.items()},'seconds':time.monotonic()-start}
  out.write(json.dumps(record,default=str)+'\n');out.flush();count+=1
  if count%10==0: print(count,'/',len(selected),flush=True)
print('DONE',count)
