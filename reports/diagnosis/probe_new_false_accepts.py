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

from tibia_client.adaptive_text import _ink,_glyph
_,cases=load_cases(DEFAULT_MANIFEST)
ids={'portal-tibia-2016-03-dUqF9aj::panel-scale-0.85':'resource_health','dfg-ustebra-ek177::panel-scale-0.9':'resource_capacity'}
reader=ResourceReader(ocr=Prepared());out=[]
for c in cases:
 if c.id not in ids:continue
 d=reader.discover(c.frame,use_ocr=True);f=ids[c.id];x,y,a,b=d.layout.regions[f];roi=c.frame.pixels[y:b,x:a];decoded=reader.number_reader._decode(roi,d.layout.scale,9,True)
 mask=_ink(roi);occupied=np.any(mask,axis=0);starts=np.flatnonzero(occupied & np.r_[True,~occupied[:-1]]);ends=np.flatnonzero(occupied & np.r_[~occupied[1:],True])+1
 out.append({'case_id':c.id,'field':f,'expected':c.labels[f]['value'],'scale':d.layout.scale,'text':decoded.text,'minimum_score':decoded.score,'average_score':decoded.average_score,'ranks':[[{'digit':v.token,'score':v.score} for v in g] for g in decoded.ranks],'glyph_holes':[_glyph(mask[:,s:e]).holes for s,e in zip(starts,ends)]})
print(json.dumps(out,indent=2))
