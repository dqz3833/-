from __future__ import annotations

import json
from pathlib import Path
import requests

OUT = Path('build/fenbi_api_probe')
OUT.mkdir(parents=True, exist_ok=True)
BASE = 'https://market-api.fenbi.com'
PARAMS = {
    'app':'web','av':'100','hav':'100','kav':'100','apcid':'0',
    'client_context_id':'666f063353e10d30f9a7a103862a7a2c'
}
HEADERS = {
    'User-Agent':'Mozilla/5.0',
    'Accept':'application/json, text/plain, */*',
    'Referer':'https://www.fenbi.com/page/positions-exams',
    'Origin':'https://www.fenbi.com',
}
requests_to_try = [
    ('homeCatalogs','GET','/toolkit/api/v1/pc/exam/homeCatalogs',{}),
    ('basicInfo2026','GET','/toolkit/api/v1/pc/exam/basicInfo',{'examType':0,'examId':425648}),
    ('listByAddress','GET','/toolkit/api/v1/pc/position/listByAddress',{'examId':425648,'districtId':1,'start':0,'len':5}),
    ('statByAddress','GET','/toolkit/api/v1/pc/position/statByAddress',{'examId':425648}),
    ('positionDetail','GET','/toolkit/api/v1/pc/position/detail',{'positionId':22581136}),
    ('provinceExams','GET','/toolkit/api/v1/pc/exam/queryExams',{'examType':1,'districtId':1,'year':2024,'start':0,'len':200}),
    ('institutionExamsAH2024','GET','/toolkit/api/v1/pc/exam/queryExams',{'examType':4,'districtId':1,'year':2024,'start':0,'len':200}),
    ('institutionCatalog','GET','/toolkit/api/v1/pc/exam/catalogByCondition',{'examType':4}),
]
s = requests.Session(); s.headers.update(HEADERS)
report = {}
for name,method,path,extra in requests_to_try:
    params = dict(PARAMS); params.update(extra)
    url = BASE + path
    try:
        r = s.request(method,url,params=params,timeout=(30,120),allow_redirects=True)
        text = r.text
        record = {'status':r.status_code,'url':r.url,'bytes':len(r.content),'content_type':r.headers.get('content-type'),'text_head':text[:1000]}
        try:
            data = r.json(); record['json'] = data
            (OUT/f'{name}.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        except Exception as exc:
            record['json_error'] = repr(exc)
            (OUT/f'{name}.txt').write_text(text,encoding='utf-8')
        report[name] = record
    except Exception as exc:
        report[name] = {'error':repr(exc)}
(OUT/'probe_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
for name,record in report.items():
    print('\n===',name,'===')
    print(json.dumps({k:v for k,v in record.items() if k!='json'},ensure_ascii=False,indent=2))
    if 'json' in record:
        print(json.dumps(record['json'],ensure_ascii=False)[:8000])
