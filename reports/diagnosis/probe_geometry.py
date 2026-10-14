import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.compare_resource_readers import load_cases
from tools.benchmark_resources import DEFAULT_MANIFEST
from tibia_client.adaptive import ResourceReader
_,cases=load_cases(DEFAULT_MANIFEST)
ids={'user-paulistinha-20261013::full-scale-0.75','portal-tibia-2016-01-0i483jb::panel-scale-0.75','dfg-yonabra-ek195::panel-scale-1.5','portal-tibia-2016-02-VtukRkf::panel-scale-0.95'}
reader=ResourceReader()
records=[]
def tracer(frame,event,arg):
 if frame.f_code.co_name != '_layout': return None
 if event=='return' and frame.f_code.co_name=='_layout':
  x=frame.f_locals
  records.append({'function':'_layout','accepted':arg is not None,'runs':x.get('runs'),'columns':x.get('columns'),'regions':x.get('regions'),'boundary':x.get('boundary')})
 return tracer
for case in cases:
 if case.id not in ids:continue
 records=[];sys.settrace(tracer)
 result=reader.discover(case.frame,use_ocr=True)
 if result.layout: readings=reader.read(case.frame,result.layout,use_ocr=True)
 sys.settrace(None)
 print(json.dumps({'case':case.id,'found':bool(result.layout),'records':records}, default=int),flush=True)
