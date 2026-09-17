from __future__ import annotations

import json
from pathlib import Path
import requests

OUT = Path('build/fenbi_api_probe')
OUT.mkdir(parents=True, exist_ok=True)
BASE = 'https://market-api.fenbi.com'
PARAMS = {'app':'web','av':'100','hav':'100','kav':'100','apcid':'0','client_context_id':'666f063353e10d30f9a7a103862a7a2c'}
HEADERS = {'User-Agent':'Mozilla/5.0','Accept':'application/json, text/plain, */*','Referer':'https://www.fenbi.com/page/positions-exams','Origin':'https://www.fenbi.com'}
s = requests.Session(); s.headers.update(HEADERS)
report = {}

def get(name,path,extra=None):
    params=dict(PARAMS); params.update(extra or {})
    try:
        r=s.get(BASE+path,params=params,timeout=(30,120),allow_redirects=True)
        rec={'status':r.status_code,'url':r.url,'bytes':len(r.content),'content_type':r.headers.get('content-type'),'text_head':r.text[:1000]}
        try:
            data=r.json(); rec['json']=data
            (OUT/f'{name}.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        except Exception as exc:
            rec['json_error']=repr(exc); (OUT/f'{name}.txt').write_text(r.text,encoding='utf-8')
        report[name]=rec
        return rec.get('json')
    except Exception as exc:
        report[name]={'error':repr(exc)}
        return None

# Existing catalog and national probes.
get('homeCatalogs','/toolkit/api/v1/pc/exam/homeCatalogs')
get('institutionCatalog','/toolkit/api/v1/pc/exam/catalogByCondition',{'examType':4})
get('nationalList','/toolkit/api/v1/pc/position/listByAddress',{'examId':425648,'districtId':1,'start':0,'len':5})
get('nationalDetail','/toolkit/api/v1/pc/position/detail',{'positionId':22581136})

# Dynamic probes for 2024 Anhui province exam and 2024 Anhui public-institution unified exam.
for prefix,exam_type,exam_id in [('province',1,201114),('institution',4,210218)]:
    get(prefix+'Basic','/toolkit/api/v1/pc/exam/basicInfo',{'examType':exam_type,'examId':exam_id})
    districts=get(prefix+'Districts','/toolkit/api/v1/pc/position/list/searchableDistrict',{'examId':exam_id}) or {}
    district_data=(districts.get('datas') or (districts.get('data') or {}).get('datas') or []) if isinstance(districts,dict) else []
    if district_data:
        did=district_data[0].get('value')
        listing=get(prefix+'ListByAddress','/toolkit/api/v1/pc/position/listByAddress',{'examId':exam_id,'districtId':did,'start':0,'len':5}) or {}
    else:
        listing=get(prefix+'ListByRootAddress','/toolkit/api/v1/pc/position/listByAddress',{'examId':exam_id,'districtId':1,'start':0,'len':5}) or {}
    stat=get(prefix+'StatDepartment','/toolkit/api/v1/pc/position/statByDepartment',{'examId':exam_id}) or {}
    items=((stat.get('data') or {}).get('itemList') or []) if isinstance(stat,dict) else []
    if items:
        department=items[0].get('value') or items[0].get('name')
        get(prefix+'ListByDepartment','/toolkit/api/v1/pc/position/listByDepartment',{'examId':exam_id,'department':department,'start':0,'len':5})
    data=listing.get('data') if isinstance(listing,dict) else None
    positions=(data or {}).get('positions') or listing.get('datas') or [] if isinstance(listing,dict) else []
    if positions:
        get(prefix+'Detail','/toolkit/api/v1/pc/position/detail',{'positionId':positions[0].get('id')})

(OUT/'probe_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
for name,rec in report.items():
    print('\n===',name,'===')
    print(json.dumps({k:v for k,v in rec.items() if k!='json'},ensure_ascii=False,indent=2))
    if 'json' in rec: print(json.dumps(rec['json'],ensure_ascii=False)[:10000])
