from __future__ import annotations

import csv, gzip, hashlib, json, re, sqlite3, time, zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import pandas as pd
import requests

OUT=Path('build/core'); RAW=OUT/'raw_sources'; OUT.mkdir(parents=True,exist_ok=True); RAW.mkdir(parents=True,exist_ok=True)
UA={'User-Agent':'Mozilla/5.0 (compatible; DouyaJobDB/1.0; public-data-archive)'}
EXPECTED={2024:18948,2025:20810,2026:20714}
SOURCES={
 2024:['https://u3.huatu.com/uploads/htzximg/2024gkzw/2024gkzw.xlsx','https://attachment.gaodun.com/uploads/202312/202312151105330.xls'],
 2025:['https://u3.huatu.com/uploads/soft/241014/ah/2025gkzw.xlsx','https://imgbdb4.bendibao.com/szbdb/edu/202410/14/20241014172143_38283.zip'],
 2026:['https://u3.huatu.com/uploads/soft/251014/2026gkzw.xlsx','https://u3.huatu.com/uploads/htzximg/2026gkzw/2026gkzw.xlsx']}
ALIASES={
'department_code':['部门代码'],'department_name':['部门名称','招录机关'],'employing_department':['用人司局','用人单位'],
'agency_nature':['机构性质','单位性质'],'position_name':['招考职位','职位名称','岗位名称'],'position_attribute':['职位属性'],
'position_distribution':['职位分布'],'position_intro':['职位简介','岗位简介'],'position_code':['职位代码','岗位代码'],
'organization_level':['机构层级'],'exam_category':['考试类别','笔试类别'],'recruit_count':['招考人数','招录人数','招聘人数','计划人数'],
'major_raw':['专业'],'education_raw':['学历'],'degree_raw':['学位'],'political_status_raw':['政治面貌'],
'grassroots_years_raw':['基层工作最低年限','基层工作经历'],'service_project_raw':['服务基层项目工作经历','服务基层项目经历'],
'professional_test_raw':['是否在面试阶段组织专业能力测试','专业能力测试'],'interview_ratio_raw':['面试人员比例','面试比例'],
'work_location':['工作地点','职位工作地点'],'settlement_location':['落户地点'],'remarks_raw':['备注'],
'department_website':['部门网站','单位网站'],'consult_phone_1':['咨询电话1','咨询电话（一）','咨询电话'],
'consult_phone_2':['咨询电话2','咨询电话（二）'],'consult_phone_3':['咨询电话3','咨询电话（三）']}
HINTS={'部门代码','部门名称','用人司局','招考职位','职位代码','招考人数','专业','学历'}

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def clean(v:Any):
    if v is None:return None
    try:
        if pd.isna(v):return None
    except:pass
    s=str(v).replace('\u3000',' ').strip()
    if not s or s.lower() in {'nan','none','null'}:return None
    if re.fullmatch(r'\d+\.0',s):s=s[:-2]
    return s
def sha(p:Path):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()
def valid(p:Path):
    return p.exists() and p.stat().st_size>1000 and (p.read_bytes()[:8].startswith(b'PK') or p.read_bytes()[:8].startswith(bytes.fromhex('D0CF11E0A1B11AE1')))
def download(year:int):
    yd=RAW/str(year);yd.mkdir(exist_ok=True);logs=[]
    for i,u in enumerate(SOURCES[year],1):
        ext=Path(urlparse(u).path).suffix or '.bin';dest=yd/f'source_{i}{ext}';rec={'year':year,'url':u,'downloaded_at':now()}
        try:
            r=requests.get(u,headers=UA,timeout=(30,180),allow_redirects=True);rec.update(status=r.status_code,final_url=r.url,content_type=r.headers.get('content-type'))
            if r.status_code!=200: raise RuntimeError(f'HTTP {r.status_code}')
            dest.write_bytes(r.content);rec.update(bytes=dest.stat().st_size,sha256=sha(dest))
            files=[]
            if zipfile.is_zipfile(dest):
                ed=yd/f'unzip_{i}';ed.mkdir(exist_ok=True)
                with zipfile.ZipFile(dest) as z:z.extractall(ed)
                files=[p for p in ed.rglob('*') if p.suffix.lower() in {'.xls','.xlsx','.xlsm'}]
            elif valid(dest):files=[dest]
            logs.append(rec)
            if files:return files,logs
        except Exception as e:rec['error']=repr(e);logs.append(rec)
    return [],logs
def headers(vals:Iterable[Any]):
    c=defaultdict(int);out=[]
    for i,v in enumerate(vals):
        b=re.sub(r'\s+','',clean(v) or f'unnamed_{i+1}');c[b]+=1;out.append(b if c[b]==1 else f'{b}_{c[b]}')
    return out
def header_idx(df):
    best=None
    for i in range(min(30,len(df))):
        j='|'.join(clean(v) or '' for v in df.iloc[i].tolist());score=sum(h in j for h in HINTS)+(3 if '职位代码' in j else 0)+(2 if ('招考人数' in j or '招录人数' in j) else 0)
        if best is None or score>best[1]:best=(i,score)
    return best[0] if best and best[1]>=6 else None
def tables(p):
    x=pd.ExcelFile(p)
    for s in x.sheet_names:
        try:r=pd.read_excel(p,sheet_name=s,header=None,dtype=object)
        except Exception as e: print('SHEET_ERROR',p,s,repr(e),flush=True);continue
        i=header_idx(r)
        if i is None:continue
        d=r.iloc[i+1:].copy();d.columns=headers(r.iloc[i].tolist());d=d.dropna(how='all');yield str(s),d,i+2
def pick(row,als):
    for a in als:
        if a in row and clean(row[a]) is not None:return clean(row[a])
    for k,v in row.items():
        kk=re.sub(r'\s+','',str(k))
        for a in als:
            if kk==a or kk.startswith(a+'_'):
                x=clean(v)
                if x is not None:return x
    return None
def integer(v):
    if not v:return None
    m=re.search(r'\d+',v.replace(',',''));return int(m.group()) if m else None
def edu_rank(v):
    if not v:return None
    if '博士' in v:return 5
    if '硕士' in v or '研究生' in v:return 4
    if '本科' in v:return 3
    if '大专' in v or '专科' in v:return 2
    return 1 if ('高中' in v or '中专' in v) else None
def db(path):
    if path.exists():path.unlink()
    c=sqlite3.connect(path);c.executescript('''
    PRAGMA journal_mode=WAL;PRAGMA synchronous=NORMAL;
    CREATE TABLE sources(source_id TEXT PRIMARY KEY,year INTEGER,source_url TEXT,final_url TEXT,local_path TEXT,sha256 TEXT,bytes INTEGER,source_level TEXT,downloaded_at TEXT,notes TEXT);
    CREATE TABLE raw_positions(raw_position_id INTEGER PRIMARY KEY AUTOINCREMENT,year INTEGER,exam_type TEXT,source_id TEXT,source_file TEXT,source_sheet TEXT,source_row_no INTEGER,source_record_json TEXT,source_record_hash TEXT,UNIQUE(year,source_record_hash));
    CREATE TABLE positions(position_id TEXT PRIMARY KEY,raw_position_id INTEGER,year INTEGER,exam_type TEXT,department_code TEXT,department_name TEXT,employing_department TEXT,agency_nature TEXT,position_name TEXT,position_attribute TEXT,position_distribution TEXT,position_intro TEXT,position_code TEXT,organization_level TEXT,exam_category TEXT,recruit_count INTEGER,work_location TEXT,settlement_location TEXT,department_website TEXT,consult_phone_1 TEXT,consult_phone_2 TEXT,consult_phone_3 TEXT,remarks_raw TEXT,source_id TEXT);
    CREATE TABLE position_requirements(position_id TEXT PRIMARY KEY,major_raw TEXT,education_raw TEXT,education_min_rank INTEGER,degree_raw TEXT,degree_required INTEGER,political_status_raw TEXT,grassroots_years_raw TEXT,service_project_raw TEXT,professional_test_raw TEXT,interview_ratio_raw TEXT,rule_json TEXT);
    CREATE TABLE application_stats(stat_id INTEGER PRIMARY KEY AUTOINCREMENT,position_id TEXT,stat_date TEXT,registered_count INTEGER,approved_count INTEGER,paid_count INTEGER,confirmed_count INTEGER,competition_ratio REAL,stat_scope TEXT,is_final INTEGER,source_url TEXT,source_note TEXT);
    CREATE TABLE entry_scores(score_id INTEGER PRIMARY KEY AUTOINCREMENT,position_id TEXT,score_type TEXT,min_written_score REAL,min_entry_score REAL,max_entry_score REAL,min_final_score REAL,max_final_score REAL,source_url TEXT,source_note TEXT);
    CREATE TABLE coverage(year INTEGER PRIMARY KEY,expected_positions INTEGER,loaded_positions INTEGER,distinct_position_codes INTEGER,status TEXT,notes TEXT);
    CREATE INDEX idx_pos_filter ON positions(year,work_location,organization_level,exam_category);CREATE INDEX idx_pos_code ON positions(year,position_code);CREATE INDEX idx_req ON position_requirements(education_min_rank,political_status_raw,major_raw);
    ''');return c
def ingest(c,year,files,logs):
    for l in logs:
        if not l.get('sha256'):continue
        sid=hashlib.sha1(f"{year}|{l['sha256']}".encode()).hexdigest();c.execute('INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)',(sid,year,l.get('url'),l.get('final_url'),None,l.get('sha256'),l.get('bytes'),'B-public mirror of official attachment',l.get('downloaded_at'),None))
    codes=set();ins=0
    for p in files:
        if not valid(p):continue
        fh=sha(p);sid=hashlib.sha1(f'{year}|{fh}'.encode()).hexdigest();c.execute('INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)',(sid,year,None,None,str(p),fh,p.stat().st_size,'B-public mirror of official attachment',now(),'Parsed workbook'))
        for sheet,frame,first in tables(p):
            for off,rec in enumerate(frame.to_dict(orient='records')):
                raw={str(k):clean(v) for k,v in rec.items()};m={k:pick(raw,a) for k,a in ALIASES.items()};code=m.get('position_code');name=m.get('position_name');n=integer(m.get('recruit_count'))
                if not code or not re.search(r'\d{5,}',code) or not name or n is None:continue
                cn=re.sub(r'\D','',code)
                if cn in codes:continue
                codes.add(cn);rj=json.dumps(raw,ensure_ascii=False,sort_keys=True);rh=hashlib.sha256(f'{year}|{rj}'.encode()).hexdigest();cur=c.execute('INSERT OR IGNORE INTO raw_positions(year,exam_type,source_id,source_file,source_sheet,source_row_no,source_record_json,source_record_hash) VALUES (?,?,?,?,?,?,?,?)',(year,'国考',sid,str(p),sheet,first+off,rj,rh))
                if cur.rowcount==0:continue
                pid=f'GK-{year}-{cn}';c.execute('INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(pid,cur.lastrowid,year,'国考',m.get('department_code'),m.get('department_name'),m.get('employing_department'),m.get('agency_nature'),name,m.get('position_attribute'),m.get('position_distribution'),m.get('position_intro'),cn,m.get('organization_level'),m.get('exam_category'),n,m.get('work_location'),m.get('settlement_location'),m.get('department_website'),m.get('consult_phone_1'),m.get('consult_phone_2'),m.get('consult_phone_3'),m.get('remarks_raw'),sid))
                degree=m.get('degree_raw');rule={'major_raw':m.get('major_raw'),'education_raw':m.get('education_raw'),'education_min_rank':edu_rank(m.get('education_raw')),'degree_raw':degree,'degree_required':bool(degree and '不限' not in degree and '无要求' not in degree),'political_status_raw':m.get('political_status_raw'),'grassroots_years_raw':m.get('grassroots_years_raw'),'service_project_raw':m.get('service_project_raw'),'professional_test_raw':m.get('professional_test_raw'),'interview_ratio_raw':m.get('interview_ratio_raw'),'remarks_raw':m.get('remarks_raw')}
                c.execute('INSERT OR REPLACE INTO position_requirements VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(pid,m.get('major_raw'),m.get('education_raw'),rule['education_min_rank'],degree,int(rule['degree_required']),m.get('political_status_raw'),m.get('grassroots_years_raw'),m.get('service_project_raw'),m.get('professional_test_raw'),m.get('interview_ratio_raw'),json.dumps(rule,ensure_ascii=False)));ins+=1
    c.commit();return ins
def export(c,p):
    q='''SELECT p.*,r.major_raw,r.education_raw,r.education_min_rank,r.degree_raw,r.degree_required,r.political_status_raw,r.grassroots_years_raw,r.service_project_raw,r.professional_test_raw,r.interview_ratio_raw,r.rule_json FROM positions p LEFT JOIN position_requirements r USING(position_id) ORDER BY year,position_code''';cur=c.execute(q);h=[d[0] for d in cur.description];n=0
    with gzip.open(p,'wt',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(h)
        for row in cur:w.writerow(row);n+=1
    return n
def main():
    path=OUT/'national_core_2024_2026.sqlite';c=db(path);manifest={'generated_at':now(),'scope':'2024-2026国考原始职位表核心库','years':{},'sources':[],'limitations':['报名人数、进面分扩展表在独立流程补充；本核心包只声称职位表数据覆盖。','资格判定以source_record_json中的原始字段及官方附件为最终依据。']}
    for y in (2024,2025,2026):
        files,logs=download(y);manifest['sources']+=logs;loaded=ingest(c,y,files,logs);distinct=c.execute('SELECT COUNT(DISTINCT position_code) FROM positions WHERE year=?',(y,)).fetchone()[0];expected=EXPECTED[y];status='complete' if loaded==expected else ('substantial_partial' if loaded>=expected*.95 else 'partial');c.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?)',(y,expected,loaded,distinct,status,'年度岗位数与公开职位总数核验'));c.commit();manifest['years'][str(y)]={'expected':expected,'loaded':loaded,'distinct_codes':distinct,'status':status};print('YEAR',y,json.dumps(manifest['years'][str(y)],ensure_ascii=False),flush=True)
    manifest['csv_rows']=export(c,OUT/'national_core_2024_2026.csv.gz');manifest['totals']={'positions':c.execute('SELECT COUNT(*) FROM positions').fetchone()[0],'raw_positions':c.execute('SELECT COUNT(*) FROM raw_positions').fetchone()[0],'requirements':c.execute('SELECT COUNT(*) FROM position_requirements').fetchone()[0]};c.commit();c.close();manifest['db_bytes']=path.stat().st_size;manifest['db_sha256']=sha(path);(OUT/'national_core_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8');(OUT/'README.txt').write_text('2024-2026国考真实原始职位表核心数据库。导入前先检查national_core_manifest.json。未知或未补数据为NULL。\n',encoding='utf-8')
if __name__=='__main__':main()
