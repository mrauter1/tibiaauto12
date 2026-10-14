import sys,json,collections,ast,hashlib,subprocess
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.compare_resource_readers import implementation_hashes,environment
baseline=json.load(open('reports/resource-benchmark.json'))
rows=[json.loads(s) for s in open('reports/diagnosis/failure-traces.jsonl')]
old={r['case_id']:r for r in baseline['techniques']['adaptive_templates_ocr']['cases']}
counts=collections.Counter(); reasons=collections.defaultdict(collections.Counter); diffs=[]
for r in rows:
 b=old[r['case_id']]
 if r['panel_found']!=(b['panel_status']=='found'):diffs.append([r['case_id'],'panel'])
 if not r['panel_found']:
  key='anchor_constellation_missing' if not r['trace']['constellations'][-1]['groups'] else 'value_column_missing'
  counts[key]+=4
 for f,v in r['readings'].items():
  actual=ast.literal_eval(v['value'])['count'] if v['quality']=='known' else None
  if actual!=b['fields'][f]['actual'] or v['quality']!=b['fields'][f]['quality']:diffs.append([r['case_id'],f])
  if v['quality']!='unknown':continue
  detail=v['detail'];reasons[detail]['count']+=1;s=v['diagnostics'].get('text')
  if s and s.replace(',','').isdigit():reasons[detail]['top_candidate_matches_truth']+=int(int(s.replace(',',''))==v['expected'])
  if detail.startswith('resource row'):
   counts['individual_anchor_missing' if f not in r['trace']['layouts'][-1]['fields'] else 'individual_region_rejected']+=1
  else:counts['numeric_reader_rejected']+=1
result={'scope':'Diagnostic rerun of all 193 cases with eligible unknown fields in adaptive_templates_ocr, not a new technique benchmark. No production reader changes.','git_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'baseline_sha256':hashlib.sha256(Path('reports/resource-benchmark.json').read_bytes()).hexdigest(),'traced_cases':len(rows),'failure_fields_by_stage':counts,'numeric_and_row_reasons':reasons,'baseline_output_differences':diffs,'implementation_matches_baseline':implementation_hashes()==baseline['implementation'],'environment':environment()}
Path('reports/diagnosis/summary.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
