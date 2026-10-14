import sys,json,collections
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import cv2,numpy as np
from tools.compare_resource_readers import load_cases
from tools.benchmark_resources import DEFAULT_MANIFEST
from tibia_client.adaptive_text import NumberReader,TesseractOCR,OCRBox,_ink
traces=[json.loads(s) for s in open('reports/diagnosis/failure-traces.jsonl')]
selected={r['case_id']:r for r in traces if any(v['detail']=='number contains an ambiguous digit' for v in r['readings'].values())}
_,cases=load_cases(DEFAULT_MANIFEST)
class Prepared:
 def __init__(self,kind):self.backend=TesseractOCR();self.kind=kind
 def read_number_boxes(self,image,**kw):
  if self.kind=='gray': prepared=255-cv2.cvtColor(image,cv2.COLOR_RGB2GRAY)
  else:prepared=(~_ink(image)).astype(np.uint8)*255
  prepared=cv2.resize(prepared,None,fx=3,fy=3,interpolation=cv2.INTER_LINEAR if self.kind=='gray' else cv2.INTER_NEAREST)
  prepared=np.pad(prepared,12,constant_values=255)
  prepared=np.repeat(prepared[:,:,None],3,axis=2)
  boxes=self.backend.read_number_boxes(prepared,**kw)
  return tuple(OCRBox(b.text,tuple(round((x-12)/3) for x in b.box),b.score) for b in boxes)
readers={kind:NumberReader(ocr=Prepared(kind)) for kind in ('gray','binary')}
output=[]
for c in cases:
 if c.id not in selected:continue
 for f,r in selected[c.id]['readings'].items():
  if r['detail']!='number contains an ambiguous digit':continue
  d=r['diagnostics'];x,y,a,b=d['effective_region'];roi=c.frame.pixels[y:b,x:a]
  row={'case_id':c.id,'field':f,'expected':r['expected'],'before':'unknown','variants':{}}
  for kind,reader in readers.items():
   v=reader.read(roi,scale=d['scale'],max_digits=9,allow_grouping=f!='resource_soul',use_ocr=True)
   row['variants'][kind]={'quality':v.quality.value,'value':v.value,'detail':v.detail}
  output.append(row)
print(json.dumps({'scope':'Failure-directed exploratory probe on 8 strong ambiguous numeric fields; not a full benchmark or heldout estimate. All template beam and confidence gates retained.','rows':output},indent=2))
