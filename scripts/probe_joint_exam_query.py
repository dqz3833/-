from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

import requests

OUT=Path('build/joint_exam_query_probe'); OUT.mkdir(parents=True,exist_ok=True)
URL='https://market-api.fenbi.com/toolkit/api/v1/pc/exam/queryByCondition'
HEAD={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36','Accept':'application/json, text/plain, */*','Content-Type':'application/json;charset=UTF-8','Origin':'https://www.fenbi.com','Referer':'https://www.fenbi.com/'}
report={'generated_at':datetime.now(timezone.utc).isoformat(),'endpoint':URL,'years':{}}
for year in (2024,2025,2026):
    payload={'districtId':None,'examType':16,'year':year,'enrollStatus':None,'recruitNumCode':None,'start':0,'len':5000,'needTotal':True}
    rec={'payload':payload}
    try:
        r=requests.post(URL,headers=HEAD,json=payload,timeout=(30,180))
        rec.update({'status':r.status_code,'content_type':r.headers.get('content-type'),'bytes':len(r.content),'text_head':r.text[:2000]})
        (OUT/f'{year}_raw.bin').write_bytes(r.content)
        try:
            data=r.json(); rec['json']=data
            (OUT/f'{year}.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        except Exception as e: rec['json_error']=repr(e)
    except Exception as e: rec['error']=repr(e)
    report['years'][str(year)]=rec
    print('YEAR',year,'status',rec.get('status'),'bytes',rec.get('bytes'),'keys',list((rec.get('json') or {}).keys()) if isinstance(rec.get('json'),dict) else None,flush=True)
    if isinstance(rec.get('json'),dict): print(json.dumps(rec['json'],ensure_ascii=False)[:10000],flush=True)
(OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
