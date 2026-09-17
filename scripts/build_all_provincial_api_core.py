from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

OUT = Path('build/provincial_api_core')
OUT.mkdir(parents=True, exist_ok=True)
DB = OUT / 'provincial_civil_service_core_2024_2026.sqlite'
BASE = 'https://market-api.fenbi.com'
PARAMS = {'app':'web','av':'100','hav':'100','kav':'100','apcid':'0','client_context_id':'666f063353e10d30f9a7a103862a7a2c'}
HEADERS = {'User-Agent':'Mozilla/5.0 (compatible; DouyaJobDB/2.0; public-data-archive)','Accept':'application/json, text/plain, */*','Referer':'https://www.fenbi.com/page/positions-exams','Origin':'https://www.fenbi.com'}
YEARS = {2024,2025,2026}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def api(session: requests.Session, path: str, extra: dict[str,Any] | None = None, attempts: int = 6) -> dict[str,Any]:
    params=dict(PARAMS); params.update(extra or {})
    error=None
    for attempt in range(attempts):
        try:
            r=session.get(BASE+path,params=params,timeout=(20,120),allow_redirects=True)
            if r.status_code==200:
                data=r.json()
                if data.get('code')==1:
                    return data
                error=RuntimeError(f"API code={data.get('code')} msg={data.get('msg')}")
            else:
                error=RuntimeError(f'HTTP {r.status_code}')
        except Exception as exc:
            error=exc
        time.sleep(min(10,1.5*(attempt+1)))
    raise RuntimeError(f'{path} {extra}: {error}')


def number(value: Any) -> int | None:
    if value is None:return None
    m=re.search(r'\d+',str(value).replace(',',''))
    return int(m.group()) if m else None


def canonical(value: Any) -> str:
    if value is None:return ''
    return re.sub(r'[\s（）()【】\[\]、，,。.;；:：/\\_-]+','',str(value)).lower()


def create_db(path: Path) -> sqlite3.Connection:
    if path.exists():path.unlink()
    con=sqlite3.connect(path)
    con.executescript('''
    PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;
    CREATE TABLE exams(
      exam_id INTEGER PRIMARY KEY, exam_type INTEGER, year INTEGER, province TEXT,
      district_id INTEGER, exam_name TEXT, expected_positions INTEGER,
      recruit_count INTEGER, total_signup INTEGER, total_verify INTEGER,
      total_wait_verify INTEGER, raw_basic_json TEXT, source_url TEXT
    );
    CREATE TABLE raw_positions(
      raw_position_id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id INTEGER,
      api_position_id INTEGER, raw_json TEXT, raw_hash TEXT UNIQUE
    );
    CREATE TABLE positions(
      position_id TEXT PRIMARY KEY, raw_position_id INTEGER, exam_id INTEGER,
      api_position_id INTEGER, year INTEGER, exam_type TEXT, province TEXT,
      position_code TEXT, position_name TEXT, department TEXT, sub_department TEXT,
      recruit_count INTEGER, work_address TEXT, major_raw TEXT,
      education_raw TEXT, birthday_cutoff_raw TEXT, source_url TEXT
    );
    CREATE TABLE position_requirements(
      position_id TEXT PRIMARY KEY, major_raw TEXT, education_raw TEXT,
      birthday_cutoff_raw TEXT, age_rule_note TEXT, rule_json TEXT
    );
    CREATE TABLE application_stats(
      stat_id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT,
      metric_name TEXT, metric_value INTEGER, registered_count INTEGER,
      approved_count INTEGER, paid_count INTEGER, stat_scope TEXT,
      is_final INTEGER, source_url TEXT
    );
    CREATE TABLE entry_scores(
      score_id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT,
      score_year INTEGER, score_level TEXT, score_value REAL,
      source_url TEXT, source_note TEXT
    );
    CREATE TABLE competition_history(
      history_id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT,
      history_year INTEGER, level TEXT, ratio_text TEXT,
      source_url TEXT, source_note TEXT
    );
    CREATE TABLE history_groups(
      history_group_id TEXT PRIMARY KEY, canonical_key TEXT,
      canonical_department TEXT, canonical_position_name TEXT,
      canonical_work_address TEXT
    );
    CREATE TABLE history_members(
      history_group_id TEXT, position_id TEXT,
      PRIMARY KEY(history_group_id,position_id)
    );
    CREATE TABLE history_links(
      link_id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT,
      historical_position_id TEXT, relation_type TEXT,
      similarity_score REAL, evidence_json TEXT,
      UNIQUE(position_id,historical_position_id)
    );
    CREATE TABLE coverage(
      exam_id INTEGER PRIMARY KEY, year INTEGER, province TEXT,
      exam_name TEXT, expected_positions INTEGER, loaded_positions INTEGER,
      status TEXT, notes TEXT
    );
    CREATE INDEX idx_positions_filter ON positions(year,province,work_address,education_raw);
    CREATE INDEX idx_positions_code ON positions(exam_id,position_code);
    CREATE INDEX idx_requirements_major ON position_requirements(major_raw,education_raw);
    CREATE INDEX idx_application_position ON application_stats(position_id);
    ''')
    return con


def extract_exam_items(home: dict[str,Any]) -> list[dict[str,Any]]:
    out=[]; seen=set()
    districts=((home.get('data') or {}).get('catalogByDistrict') or {}).get('districtExams') or []
    for district in districts:
        province=district.get('name'); district_id=district.get('districtId')
        for item in district.get('itemList') or []:
            if item.get('examType')!=1 or item.get('year') not in YEARS or not item.get('examId'):
                continue
            key=int(item['examId'])
            if key in seen:continue
            seen.add(key)
            out.append({'exam_id':key,'year':int(item['year']),'province':province,'district_id':int(item.get('districtId') or district_id),'catalog_name':item.get('name')})
    return sorted(out,key=lambda x:(x['year'],x['province'],x['exam_id']))


def fetch_all_positions(session: requests.Session, exam_id: int, district_id: int, expected: int | None) -> tuple[list[dict[str,Any]],dict[str,Any]]:
    positions=[]; start=0; page_size=500
    meta={}
    while True:
        result=api(session,'/toolkit/api/v1/pc/position/listByAddress',{'examId':exam_id,'districtId':district_id,'start':start,'len':page_size})
        data=result.get('data') or {}
        if not meta: meta={k:v for k,v in data.items() if k!='positions'}
        batch=data.get('positions') or []
        positions.extend(batch)
        total=number(data.get('total')) or expected or len(positions)
        if not batch or len(positions)>=total:break
        start+=len(batch)
        if len(batch)<page_size:break
    # Deduplicate API position IDs, preserving first.
    unique={}
    for item in positions:
        pid=item.get('id')
        if pid is not None and pid not in unique:unique[pid]=item
    return list(unique.values()),meta


def ingest_exam(con: sqlite3.Connection, session: requests.Session, item: dict[str,Any]) -> dict[str,Any]:
    exam_id=item['exam_id']; year=item['year']; province=item['province']; district_id=item['district_id']
    basic=api(session,'/toolkit/api/v1/pc/exam/basicInfo',{'examType':1,'examId':exam_id}).get('data') or {}
    top={entry.get('name'):entry.get('value') for entry in basic.get('topDataList') or []}
    expected=number(next((v for k,v in top.items() if '岗位数' in str(k) or '职位数' in str(k)),None))
    if expected is None:
        # Fall back to catalog/basic hot structure.
        expected=number((basic.get('hotPositionVO') or {}).get('totalPosition'))
    positions,meta=fetch_all_positions(session,exam_id,district_id,expected)
    expected=expected or number(meta.get('total')) or len(positions)
    recruit=number(next((v for k,v in top.items() if '招聘人数' in str(k) or '招录人数' in str(k)),None))
    hot=basic.get('hotPositionVO') or {}
    source=f'{BASE}/toolkit/api/v1/pc/exam/basicInfo?examType=1&examId={exam_id}'
    con.execute('INSERT OR REPLACE INTO exams VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',(
        exam_id,1,year,province,district_id,basic.get('name') or item.get('catalog_name'),expected,recruit,
        number(hot.get('totalSignUp')),number(hot.get('totalVerify')),number(hot.get('totalWaitVerify')),
        json.dumps(basic,ensure_ascii=False),source))
    inserted=0
    for raw in positions:
        api_id=int(raw['id']); raw_json=json.dumps(raw,ensure_ascii=False,sort_keys=True)
        raw_hash=hashlib.sha256(f'{exam_id}|{api_id}|{raw_json}'.encode()).hexdigest()
        cur=con.execute('INSERT OR IGNORE INTO raw_positions(exam_id,api_position_id,raw_json,raw_hash) VALUES (?,?,?,?)',(exam_id,api_id,raw_json,raw_hash))
        if cur.rowcount:raw_id=cur.lastrowid
        else:raw_id=con.execute('SELECT raw_position_id FROM raw_positions WHERE raw_hash=?',(raw_hash,)).fetchone()[0]
        position_id=f'SK-{year}-{exam_id}-{api_id}'
        code=str(raw.get('positionCode') or '')
        recruit_count=number(raw.get('employCountDesc'))
        url=f'{BASE}/toolkit/api/v1/pc/position/detail?positionId={api_id}'
        con.execute('INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(
            position_id,raw_id,exam_id,api_id,year,'省考',province,code,raw.get('positionName'),raw.get('department'),raw.get('subDepartment'),
            recruit_count,raw.get('workAddress'),raw.get('major'),raw.get('majorDegree'),str(raw.get('birthday') or ''),url))
        rule={'major_raw':raw.get('major'),'education_raw':raw.get('majorDegree'),'birthday_cutoff_raw':raw.get('birthday'),'note':'birthday字段为职位库标准化年龄截止日期；最终资格以公告原表为准'}
        con.execute('INSERT OR REPLACE INTO position_requirements VALUES (?,?,?,?,?,?)',(
            position_id,raw.get('major'),raw.get('majorDegree'),str(raw.get('birthday') or ''),rule['note'],json.dumps(rule,ensure_ascii=False)))
        pair=raw.get('dataPair') or {}
        metric_name=pair.get('name'); metric_value=number(pair.get('value'))
        if metric_name and metric_value is not None:
            registered=metric_value if '报名' in metric_name else None
            approved=metric_value if ('过审' in metric_name or '审核' in metric_name or '合格' in metric_name) else None
            paid=metric_value if '缴费' in metric_name else None
            con.execute('INSERT INTO application_stats(position_id,metric_name,metric_value,registered_count,approved_count,paid_count,stat_scope,is_final,source_url) VALUES (?,?,?,?,?,?,?,?,?)',(
                position_id,metric_name,metric_value,registered,approved,paid,'公开职位库当前或归档指标',1,url))
        inserted+=1
    status='complete' if inserted==expected else ('near_complete' if expected and inserted>=expected*.98 else 'partial')
    con.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?,?,?)',(
        exam_id,year,province,basic.get('name') or item.get('catalog_name'),expected,inserted,status,'逐考试按basicInfo岗位数核验；重复考试批次保留独立exam_id。'))
    con.commit()
    return {'exam_id':exam_id,'year':year,'province':province,'name':basic.get('name') or item.get('catalog_name'),'expected':expected,'loaded':inserted,'status':status,'metric_name_sample':next((p.get('dataPair',{}).get('name') for p in positions if p.get('dataPair')),None)}


def build_history(con: sqlite3.Connection) -> dict[str,int]:
    rows=con.execute('SELECT position_id,year,department,position_name,work_address FROM positions').fetchall(); groups=defaultdict(list)
    for row in rows:
        key='|'.join(canonical(v) for v in (row[2],row[3],row[4]))
        if key.replace('|',''):groups[key].append(row)
    gc=0;lc=0
    for key,members in groups.items():
        if len({m[1] for m in members})<2:continue
        gid='PH-'+hashlib.sha1(key.encode()).hexdigest()[:20];s=members[0]
        con.execute('INSERT OR IGNORE INTO history_groups VALUES (?,?,?,?,?)',(gid,key,s[2],s[3],s[4]));gc+=1
        for m in members:con.execute('INSERT OR IGNORE INTO history_members VALUES (?,?)',(gid,m[0]))
        ordered=sorted(members,key=lambda x:(x[1],x[0]))
        for newer in ordered:
            old=[x for x in ordered if x[1]<newer[1]]
            if not old:continue
            prior=old[-1]
            con.execute('INSERT OR IGNORE INTO history_links(position_id,historical_position_id,relation_type,similarity_score,evidence_json) VALUES (?,?,?,?,?)',(
                newer[0],prior[0],'exact_normalized_org_title_location',1.0,json.dumps({'canonical_key':key},ensure_ascii=False)))
            lc+=1
    con.commit();return {'groups':gc,'links':lc}


def export_csv(con: sqlite3.Connection,path: Path) -> int:
    q='''SELECT p.*,r.major_raw AS req_major_raw,r.education_raw AS req_education_raw,r.birthday_cutoff_raw,r.age_rule_note,r.rule_json,a.metric_name,a.metric_value,a.registered_count,a.approved_count,a.paid_count FROM positions p LEFT JOIN position_requirements r USING(position_id) LEFT JOIN application_stats a USING(position_id) ORDER BY year,province,exam_id,position_code,api_position_id'''
    cur=con.execute(q); headers=[d[0] for d in cur.description]; count=0
    with gzip.open(path,'wt',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(headers)
        for row in cur:w.writerow(row);count+=1
    return count


def main():
    session=requests.Session();session.headers.update(HEADERS)
    con=create_db(DB)
    home=api(session,'/toolkit/api/v1/pc/exam/homeCatalogs')
    exams=extract_exam_items(home)
    manifest={'generated_at':now(),'scope':'2024-2026全国省考公开职位核心库','exam_count':len(exams),'exams':[],'limitations':['核心库来自公开职位API；专业、学历、年龄等最终资格以官方职位表原文为准。','dataPair仅保存该职位库公开的报名/缴费/过审指标，未公开字段保持NULL。','entry_scores与competition_history预留为详情扩展表，本核心构建不声称全量进面分覆盖。']}
    for index,item in enumerate(exams,1):
        try:report=ingest_exam(con,session,item)
        except Exception as exc:
            report={**item,'status':'failed','error':repr(exc)}
        manifest['exams'].append(report)
        print('EXAM',index,'/',len(exams),json.dumps(report,ensure_ascii=False),flush=True)
    manifest['history']=build_history(con)
    manifest['csv_rows']=export_csv(con,OUT/'provincial_civil_service_core_2024_2026.csv.gz')
    manifest['totals']={
        'exams':con.execute('SELECT COUNT(*) FROM exams').fetchone()[0],
        'positions':con.execute('SELECT COUNT(*) FROM positions').fetchone()[0],
        'raw_positions':con.execute('SELECT COUNT(*) FROM raw_positions').fetchone()[0],
        'requirements':con.execute('SELECT COUNT(*) FROM position_requirements').fetchone()[0],
        'application_stats':con.execute('SELECT COUNT(*) FROM application_stats').fetchone()[0],
        'entry_scores':con.execute('SELECT COUNT(*) FROM entry_scores').fetchone()[0],
        'history_groups':con.execute('SELECT COUNT(*) FROM history_groups').fetchone()[0],
        'history_links':con.execute('SELECT COUNT(*) FROM history_links').fetchone()[0],
        'failed_exams':sum(1 for x in manifest['exams'] if x.get('status')=='failed'),
        'complete_exams':sum(1 for x in manifest['exams'] if x.get('status')=='complete'),
    }
    con.commit();con.close()
    manifest['database_bytes']=DB.stat().st_size
    manifest['database_sha256']=hashlib.sha256(DB.read_bytes()).hexdigest()
    (OUT/'provincial_core_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/'README.txt').write_text('2024-2026全国省考真实公开职位核心数据库。导入前请检查manifest的逐考试expected/loaded/status；未知数据为NULL。\n',encoding='utf-8')
    print('TOTALS',json.dumps(manifest['totals'],ensure_ascii=False),flush=True)

if __name__=='__main__':main()
