import sys,json,collections,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import cv2,numpy as np
from tibia_client.adaptive_text import TesseractOCR,OCRBox
from tibia_client.adaptive import ResourceReader
from tools.compare_resource_readers import load_cases,AdaptiveTechnique,InferenceCase,score_case
from tools.benchmark_resources import DEFAULT_MANIFEST
class Prepared:
 def __init__(self): self.backend=TesseractOCR();self.available=self.backend.available
 def read_number_boxes(self,image,**kw):return self.backend.read_number_boxes(image,**kw)
 def read_text_boxes(self,image,**kw):
  gray=255-cv2.cvtColor(image,cv2.COLOR_RGB2GRAY)
  enlarged=cv2.resize(gray,None,fx=3,fy=3,interpolation=cv2.INTER_LINEAR)
  prepared=np.repeat(np.pad(enlarged,12,constant_values=255)[:,:,None],3,axis=2)
  boxes=self.backend.read_text_boxes(prepared,**kw)
  return tuple(OCRBox(b.text,tuple(round((x-12)/3) for x in b.box),b.score) for b in boxes)
traces=[json.loads(s) for s in open('reports/diagnosis/failure-traces.jsonl')]
selected={r['case_id'] for r in traces if not r['panel_found'] and r['transform'].startswith('panel-scale')}
_,cases=load_cases(DEFAULT_MANIFEST)
reader=ResourceReader(ocr=Prepared())
from tools.compare_resource_readers import TechniqueObservation
with open('reports/diagnosis/label-preprocessing-probe.jsonl','w') as out:
 for c in cases:
  if c.id not in selected:continue
  d=reader.discover(c.frame,use_ocr=True);reads=reader.read(c.frame,d.layout,use_ocr=True) if d.layout else {}
  values={k:v.value['count'] if v.value is not None else None for k,v in reads.items()}
  obs=TechniqueObservation(panel_status='found' if d.layout else 'ambiguous' if d.candidates else 'missing',values=values,regions=d.layout.regions if d.layout else {},qualities={k:v.quality.value for k,v in reads.items()},detail=d.detail)
  scored=score_case(c,obs)
  out.write(json.dumps(scored)+'\n');out.flush()
print('DONE',len(selected))
