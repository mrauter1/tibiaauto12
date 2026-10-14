"""Reproduce a failure-directed chrome intervention; never modify source images."""
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from PIL import Image
from core.perception import Frame
from tibia_client.adaptive import ResourceReader
from tools.compare_resource_readers import load_cases
from tools.benchmark_resources import DEFAULT_MANIFEST
_,cases=load_cases(DEFAULT_MANIFEST)
case=next(c for c in cases if c.id=='dfg-retro-pvp-rp535::panel-scale-2')
reader=ResourceReader();original=reader.discover(case.frame,use_ocr=True)
pixels=case.frame.pixels.copy();pixels[:,319:322]=42
changed=Frame(Image.fromarray(pixels));result=reader.discover(changed,use_ocr=True)
readings=reader.read(changed,result.layout,use_ocr=True) if result.layout else {}
values={k:v.value['count'] if v.value is not None else None for k,v in readings.items()}
report={'scope':'Manual pixel intervention on one inspected failure, not a detector implementation or benchmark improvement.','case_id':case.id,'before_found':bool(original.layout),'after_found':bool(result.layout),'values':values,'exact':len(values)==4 and all(value==case.labels[k]['value'] for k,value in values.items())}
Path('reports/diagnosis/divider-reproduction.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
