from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

OUT=Path('build/national_entry_scores'); OUT.mkdir(parents=True,exist_ok=True)
DB=OUT/'national_entry_scores_2024_2026.sqlite'
SOURCES={
 2024:'https://u3.huatu.com/anhui/fj/2024gkmsmd.xlsx',
 2025:'https://u3.huatu.com/anhui/fj/2025gkmsmd.xlsx',
 2026:'https://files.gemu.cn/w_uploads/2026/01/15/%E4%B8%AD%E5%A4%AE%E6%9C%BA%E5%85%B3%E5%8F%8A%E5%85%B6%E7%9B%B4%E5%B1%9E%E6%9C%BA%E6%9E%842026%E5%B9%B4%E5%BA%A6%E8%80%83%E8%AF%95%E5%BD%95%E7%94%A8%E5%85%AC%E5%8A%A1%E5%91%98%E9%9D%A2%E8%AF%95%E4%BA%BA%E5%91%98%E5%90%8D%E5%8D%95.xlsx'
}
HEAD={'User-Agent':'Mozilla/5.0'}

def clean(v):
 if v is None or (isinstance(v,float) and pd.isna(v)): return None
 s=str(v).strip().replace('\u3000',' ')
 if not s or s.lower()=='nan':return None
 if re.fullmatch(r'\d+\.0',s):s=s[:-2]
 return s

def norm(s):return re.sub(r'[\s（）()【】\[\]、，,。.;；:：/\\_-]+','',str(s or '')).lower()
def download(url,path):
 r=requests.get(url,headers=HEAD,timeout=(30,240)); r.raise_for_status(); path.write_bytes(r.content)
 return {'url':url,'bytes':len(r.content),'sha256':hashlib.sha256(r.content).hexdigest(),'content_type':r.headers.get('content-type')}

def find_header(df):
 best=(0,None)
 for i in range(min(30,len(df))):
  joined='|'.join(clean(x) or '' for x in df.iloc[i].tolist())
  score=sum(x in joined for x in ['部门名称','职位代码','最低面试分数','准考证号','姓名'])
  if '职位代码' in joined:score+=3
  if '最低面试分数' in joined or '面试最低分数' in joined:score+=4
  if score>best[0]:best=(score,i)
 return best[1] if best[0]>=5 else None

def headers(vals):
 out=[]; seen={}
 for i,v in enumerate(vals):
  b=re.sub(r'\s+','',clean(v) or f'unnamed_{i}')
  seen[b]=seen.get(b,0)+1;out.append(b if seen[b]==1 else f'{b}_{seen[b]}')
 return out

def pick(row,names):
 for n in names:
  for k,v in row.items():
   if norm(k)==norm(n):
    x=clean(v)
    if x is not None:return x
 return None

def to_float(v):
 if not v:return None
 m=re.search(r'\d+(?:\.\d+)?',str(v).replace(',',''))
 return float(m.group()) if m else None

if DB.exists():DB.unlink()
con=sqlite3.connect(DB)
con.executescript('''
CREATE TABLE candidate_rows(id INTEGER PRIMARY KEY AUTOINCREMENT,year INTEGER,department_code TEXT,department_name TEXT,employing_department TEXT,position_name TEXT,position_code TEXT,candidate_name TEXT,admission_ticket TEXT,min_entry_score REAL,raw_json TEXT,source_url TEXT,source_sha256 TEXT);
CREATE TABLE position_scores(year INTEGER,department_code TEXT,department_name TEXT,position_code TEXT,position_name TEXT,min_entry_score REAL,candidate_rows INTEGER,source_url TEXT,source_sha256 TEXT,PRIMARY KEY(year,department_name,position_code));
CREATE TABLE coverage(year INTEGER PRIMARY KEY,status TEXT,raw_rows INTEGER,position_rows INTEGER,source_url TEXT,sha256 TEXT,notes TEXT);
CREATE INDEX idx_score_match ON position_scores(year,department_name,position_code);
''')
manifest={'generated_at':datetime.now(timezone.utc).isoformat(),'years':{}}
for year,url in SOURCES.items():
 p=OUT/f'{year}_interview.xlsx'
 try:
  src=download(url,p)
  xls=pd.ExcelFile(p)
  raw_count=0
  for sheet in xls.sheet_names:
   raw=pd.read_excel(p,sheet_name=sheet,header=None,dtype=object)
   hi=find_header(raw)
   if hi is None:continue
   df=raw.iloc[hi+1:].copy();df.columns=headers(raw.iloc[hi].tolist());df=df.dropna(how='all')
   for rec in df.to_dict(orient='records'):
    row={str(k):clean(v) for k,v in rec.items()}
    code=pick(row,['职位代码','岗位代码'])
    dept=pick(row,['部门名称','招录机关','招考部门'])
    if not code or not dept or not re.search(r'\d{6,}',code):continue
    score=to_float(pick(row,['最低面试分数','面试最低分数','最低进面分数','进入面试最低分数']))
    # Some files repeat one score for every candidate. Keep candidate rows for traceability.
    con.execute('''INSERT INTO candidate_rows(year,department_code,department_name,employing_department,position_name,position_code,candidate_name,admission_ticket,min_entry_score,raw_json,source_url,source_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',(
     year,pick(row,['部门代码']),dept,pick(row,['用人司局','招录单位']),pick(row,['招考职位','职位名称']),code,pick(row,['姓名']),pick(row,['准考证号']),score,json.dumps(row,ensure_ascii=False,sort_keys=True),url,src['sha256']))
    raw_count+=1
  con.execute('''INSERT OR REPLACE INTO position_scores(year,department_code,department_name,position_code,position_name,min_entry_score,candidate_rows,source_url,source_sha256)
  SELECT year,MAX(department_code),department_name,position_code,MAX(position_name),MIN(min_entry_score),COUNT(*),MAX(source_url),MAX(source_sha256)
  FROM candidate_rows WHERE year=? GROUP BY year,department_name,position_code''',(year,))
  positions=con.execute('SELECT COUNT(*) FROM position_scores WHERE year=?',(year,)).fetchone()[0]
  status='ok' if positions else 'no_rows'
  con.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?,?)',(year,status,raw_count,positions,url,src['sha256'],None))
  manifest['years'][str(year)]={'status':status,'raw_rows':raw_count,'position_rows':positions,**src}
 except Exception as e:
  con.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?,?)',(year,'failed',0,0,url,None,repr(e)))
  manifest['years'][str(year)]={'status':'failed','error':repr(e),'url':url}
 con.commit();print(year,json.dumps(manifest['years'][str(year)],ensure_ascii=False),flush=True)

with open(OUT/'national_entry_scores_2024_2026.csv','w',newline='',encoding='utf-8-sig') as f:
 cur=con.execute('SELECT year,department_code,department_name,position_code,position_name,min_entry_score,candidate_rows,source_url,source_sha256 FROM position_scores ORDER BY year,department_name,position_code')
 w=csv.writer(f);w.writerow([d[0] for d in cur.description]);w.writerows(cur)
manifest['quick_check']=con.execute('PRAGMA quick_check').fetchone()[0]
manifest['total_position_scores']=con.execute('SELECT COUNT(*) FROM position_scores').fetchone()[0]
con.close();(OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
