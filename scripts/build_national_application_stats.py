from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

OUT = Path('build/national_application_stats')
OUT.mkdir(parents=True, exist_ok=True)
DB = OUT / 'national_application_stats_2024_2026.sqlite'
SLUGS = [
 'anhui','beijing','chongqing','fujian','gansu','guangdong','guangxi','guizhou','hainan','hebei',
 'heilongjiang','henan','hubei','hunan','jiangsu','jiangxi','jilin','liaoning','neimenggu','ningxia',
 'qinghai','shandong','shanghai','shanxi','shanxisheng','sichuan','tianjin','xicang','xinjiang','yunnan','zhejiang'
]
YEARS=(2024,2025,2026)
S=requests.Session(); S.headers.update({'User-Agent':'Mozilla/5.0','Accept':'text/html,application/xhtml+xml'})

def norm(s): return re.sub(r'[\s（）()【】\[\]、，,。.;；:：/\\_-]+','',str(s or '')).lower()
def to_int(v):
 m=re.search(r'\d+',str(v or '').replace(',','')); return int(m.group()) if m else None

def fetch(url):
 for attempt in range(4):
  try:
   r=S.get(url,timeout=(20,120));
   if r.status_code==200 and len(r.content)>500: return r
  except Exception: pass
  time.sleep(1.5*(attempt+1))
 return None

if DB.exists(): DB.unlink()
con=sqlite3.connect(DB)
con.executescript('''
CREATE TABLE stats(
 id INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER NOT NULL, region_slug TEXT NOT NULL,
 department_name TEXT NOT NULL, position_name TEXT, position_code TEXT NOT NULL,
 education_raw TEXT, major_raw TEXT, recruit_count INTEGER, registered_count INTEGER,
 detail_url TEXT, source_url TEXT NOT NULL, source_hash TEXT,
 UNIQUE(year,department_name,position_code)
);
CREATE INDEX idx_stats_match ON stats(year,department_name,position_code);
CREATE TABLE coverage(year INTEGER,region_slug TEXT,source_url TEXT,status TEXT,rows INTEGER,notes TEXT,PRIMARY KEY(year,region_slug));
''')
manifest={'generated_at':datetime.now(timezone.utc).isoformat(),'pages':[]}
for year in YEARS:
 for slug in SLUGS:
  url=f'https://www.gwyzwb.com/{slug}zw/{year}.html'
  r=fetch(url)
  if not r:
   con.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?)',(year,slug,url,'failed',0,'download failed'))
   manifest['pages'].append({'year':year,'slug':slug,'url':url,'status':'failed'})
   continue
  html=r.content
  soup=BeautifulSoup(html,'lxml')
  table=soup.find('table',id='all_list') or soup.find('table',class_=lambda x:x and 'layui-table' in x)
  rows=0
  if table:
   heads=[x.get_text(' ',strip=True) for x in table.find_all('th')]
   hmap={norm(x):i for i,x in enumerate(heads)}
   def idx(*names):
    for n in names:
     if norm(n) in hmap:return hmap[norm(n)]
    return None
   indexes={
    'code':idx('岗位代码','职位代码'),'dept':idx('部门名称','招录机关','招考部门'),
    'name':idx('招考职位','职位名称'),'edu':idx('学历要求','学历'),
    'major':idx('专业要求','专业'),'recruit':idx('招考人数','招录人数'),
    'reg':idx('报名人数')}
   for tr in table.select('tbody tr'):
    tds=tr.find_all('td')
    if not tds: continue
    def val(k):
     i=indexes[k]; return tds[i].get_text(' ',strip=True) if i is not None and i<len(tds) else None
    code=val('code'); dept=val('dept')
    if not code or not dept or not re.search(r'\d{6,}',code): continue
    detail=None
    a=tr.find('a',href=True)
    if a: detail=urljoin(url,a['href'])
    try:
     con.execute('''INSERT OR REPLACE INTO stats(year,region_slug,department_name,position_name,position_code,education_raw,major_raw,recruit_count,registered_count,detail_url,source_url,source_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
      (year,slug,dept,val('name'),code,val('edu'),val('major'),to_int(val('recruit')),to_int(val('reg')),detail,url,hashlib.sha256(html).hexdigest()))
     rows+=1
    except Exception as e: pass
  status='ok' if rows else 'no_table_rows'
  con.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?)',(year,slug,url,status,rows,None))
  con.commit()
  manifest['pages'].append({'year':year,'slug':slug,'url':url,'status':status,'rows':rows,'bytes':len(html)})
  print(year,slug,status,rows,flush=True)
  time.sleep(.15)

# summary and CSV
summary=[]
for y in YEARS:
 row=con.execute('SELECT COUNT(*),SUM(CASE WHEN registered_count IS NOT NULL THEN 1 ELSE 0 END),SUM(recruit_count),SUM(registered_count) FROM stats WHERE year=?',(y,)).fetchone()
 summary.append({'year':y,'positions':row[0],'with_registration':row[1],'recruits':row[2],'registered_total':row[3]})
manifest['summary']=summary
manifest['total_rows']=con.execute('SELECT COUNT(*) FROM stats').fetchone()[0]
manifest['quick_check']=con.execute('PRAGMA quick_check').fetchone()[0]
con.commit()
with open(OUT/'national_application_stats_2024_2026.csv','w',newline='',encoding='utf-8-sig') as f:
 cur=con.execute('SELECT year,region_slug,department_name,position_name,position_code,education_raw,major_raw,recruit_count,registered_count,detail_url,source_url FROM stats ORDER BY year,region_slug,position_code')
 w=csv.writer(f); w.writerow([d[0] for d in cur.description]); w.writerows(cur)
con.close()
(OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False),flush=True)
