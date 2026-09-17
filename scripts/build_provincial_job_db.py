from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

PROVINCES = {
    "ah":"安徽","bj":"北京","cq":"重庆","fj":"福建","gs":"甘肃","gd":"广东","gx":"广西","gz":"贵州",
    "hi":"海南","he":"河北","ha":"河南","hl":"黑龙江","hb":"湖北","hn":"湖南","jl":"吉林","js":"江苏",
    "jx":"江西","ln":"辽宁","nm":"内蒙古","nx":"宁夏","qh":"青海","sd":"山东","sx":"山西","sn":"陕西",
    "sh":"上海","sc":"四川","tj":"天津","xz":"西藏","xj":"新疆","yn":"云南","zj":"浙江"
}
YEARS = (2024, 2025, 2026)
UA = {"User-Agent":"Mozilla/5.0 (compatible; DouyaJobDB/1.0; public-data-archive)"}
THREAD = threading.local()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def session() -> requests.Session:
    s = getattr(THREAD, "session", None)
    if s is None:
        s = requests.Session(); s.headers.update(UA); THREAD.session = s
    return s


def fetch(url: str, attempts: int = 5) -> str:
    err: Exception | None = None
    for i in range(attempts):
        try:
            r = session().get(url, timeout=(20, 90), allow_redirects=True)
            if r.status_code == 200 and len(r.content) > 100:
                r.encoding = r.apparent_encoding or r.encoding
                return r.text
            err = RuntimeError(f"HTTP {r.status_code} {url}")
        except Exception as exc:
            err = exc
        time.sleep(min(10, 1.5 * (i + 1)))
    raise RuntimeError(str(err))


def clean(value: Any) -> str | None:
    if value is None: return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def integer(value: str | None) -> int | None:
    if not value: return None
    m = re.search(r"\d+", value.replace(",", ""))
    return int(m.group()) if m else None


def number(value: str | None) -> float | None:
    if not value: return None
    m = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
    return float(m.group()) if m else None


def norm_key(value: str | None) -> str:
    if not value: return ""
    return re.sub(r"[\s（）()【】\[\]、，,。.;；:：/\\_-]+", "", value).lower()


def parse_advertised_count(html: str) -> int | None:
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    patterns = [r"招聘岗位数[：:]?\s*(\d+)", r"(\d+)\s*个职位[、，,]\s*\d+\s*人", r"(\d+)\s*个岗位[、，,]\s*\d+\s*人"]
    for p in patterns:
        m = re.search(p, text)
        if m: return int(m.group(1))
    return None


def same_host_link(base: str, href: str) -> str | None:
    url = urljoin(base, href)
    if urlparse(url).netloc != urlparse(base).netloc: return None
    return url.split("#",1)[0]


def discover_unit_pages(root: str, year: int) -> tuple[set[str], int | None, list[dict[str, Any]]]:
    logs: list[dict[str, Any]] = []
    root_html = fetch(root)
    expected = parse_advertised_count(root_html)
    host = urlparse(root).netloc
    region_pattern = re.compile(rf"^/[A-Za-z0-9_-]+/{year}\.html$")
    unit_pattern = re.compile(rf"^/[A-Za-z0-9_-]+/{year}_\d+\.html$")
    region_pages: set[str] = set()
    unit_pages: set[str] = set()
    soup = BeautifulSoup(root_html, "lxml")
    for a in soup.find_all("a", href=True):
        url = same_host_link(root, a["href"])
        if not url: continue
        path = urlparse(url).path
        if unit_pattern.fullmatch(path): unit_pages.add(url)
        elif region_pattern.fullmatch(path): region_pages.add(url)
    logs.append({"url":root,"type":"root","ok":True,"expected_positions":expected,"region_links":len(region_pages),"unit_links":len(unit_pages)})
    for region in sorted(region_pages):
        try:
            html = fetch(region)
            rsoup = BeautifulSoup(html, "lxml")
            found = 0
            for a in rsoup.find_all("a", href=True):
                url = same_host_link(region, a["href"])
                if url and unit_pattern.fullmatch(urlparse(url).path):
                    unit_pages.add(url); found += 1
            logs.append({"url":region,"type":"region","ok":True,"unit_links":found})
        except Exception as exc:
            logs.append({"url":region,"type":"region","ok":False,"error":repr(exc)})
    return unit_pages, expected, logs


def table_headers(table) -> list[str]:
    row = table.find("tr")
    if not row: return []
    return [clean(cell.get_text(" ", strip=True)) or "" for cell in row.find_all(["th","td"])]


def parse_unit_page(url: str, html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        headers = table_headers(table)
        joined = "|".join(headers)
        if not (("岗位代码" in joined or "职位代码" in joined) and ("职位名称" in joined or "岗位名称" in joined)):
            continue
        index = {re.sub(r"\s+", "", h): i for i,h in enumerate(headers)}
        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all("td")
            vals = [clean(td.get_text(" ", strip=True)) for td in cells]
            if not vals: continue
            def val(*names: str) -> str | None:
                for name in names:
                    i = index.get(name)
                    if i is not None and i < len(vals): return vals[i]
                return None
            code = val("岗位代码","职位代码")
            name = val("职位名称","岗位名称")
            if not code or not name: continue
            anchor = tr.find("a", href=True)
            detail = urljoin(url, anchor["href"]) if anchor else None
            rows.append({
                "position_code":code,"position_name":name,"education_raw":val("学历要求","学历"),"major_raw":val("专业要求","专业"),
                "recruit_count":integer(val("招考人数","招录人数","招聘人数")),"registered_count":integer(val("报名人数")),
                "approved_count":integer(val("合格人数","审核通过人数","过审人数")),"paid_count":integer(val("缴费人数")),
                "detail_url":detail,"unit_url":url
            })
    return rows


def parse_detail(url: str, html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    best: dict[str,str | None] = {}
    for table in soup.find_all("table"):
        pairs: dict[str,str | None] = {}
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th","td"])
            if len(cells) == 2:
                key = clean(cells[0].get_text(" ", strip=True)); value = clean(cells[1].get_text(" ", strip=True))
                if key and len(key) <= 30: pairs[key] = value
        if len(pairs) > len(best): best = pairs
    best["source_url"] = url
    return best


def create_db(path: Path) -> sqlite3.Connection:
    if path.exists(): path.unlink()
    con = sqlite3.connect(path)
    con.executescript("""
    PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;
    CREATE TABLE raw_positions(raw_position_id INTEGER PRIMARY KEY AUTOINCREMENT,year INTEGER,province TEXT,source_url TEXT,source_record_json TEXT,source_record_hash TEXT UNIQUE);
    CREATE TABLE positions(position_id TEXT PRIMARY KEY,raw_position_id INTEGER,year INTEGER,exam_type TEXT,province TEXT,city TEXT,recruiting_org TEXT,agency_nature TEXT,organization_level TEXT,position_name TEXT,position_code TEXT,recruit_count INTEGER,position_category TEXT,duty_rank TEXT,essay_category TEXT,professional_subject TEXT,consult_phone TEXT,remarks_raw TEXT,detail_url TEXT,unit_url TEXT,source_level TEXT);
    CREATE TABLE position_requirements(position_id TEXT PRIMARY KEY,age_raw TEXT,education_raw TEXT,degree_raw TEXT,major_raw TEXT,political_status_raw TEXT,experience_raw TEXT,other_qualification_raw TEXT,rule_json TEXT);
    CREATE TABLE application_stats(stat_id INTEGER PRIMARY KEY AUTOINCREMENT,position_id TEXT,registered_count INTEGER,approved_count INTEGER,paid_count INTEGER,stat_scope TEXT,is_final INTEGER,source_url TEXT,UNIQUE(position_id,stat_scope));
    CREATE TABLE entry_scores(score_id INTEGER PRIMARY KEY AUTOINCREMENT,position_id TEXT,score_type TEXT,min_written_score REAL,min_entry_score REAL,max_entry_score REAL,min_final_score REAL,max_final_score REAL,source_url TEXT,source_note TEXT);
    CREATE TABLE position_history_groups(history_group_id TEXT PRIMARY KEY,canonical_key TEXT,canonical_org TEXT,canonical_position_name TEXT,canonical_city TEXT);
    CREATE TABLE position_history_members(history_group_id TEXT,position_id TEXT,PRIMARY KEY(history_group_id,position_id));
    CREATE TABLE position_history_links(link_id INTEGER PRIMARY KEY AUTOINCREMENT,position_id TEXT,historical_position_id TEXT,relation_type TEXT,similarity_score REAL,manual_verified INTEGER,evidence_json TEXT,UNIQUE(position_id,historical_position_id));
    CREATE TABLE coverage(year INTEGER,province TEXT,expected_positions INTEGER,loaded_positions INTEGER,with_applications INTEGER,with_entry_scores INTEGER,failed_unit_pages INTEGER,failed_detail_pages INTEGER,status TEXT,PRIMARY KEY(year,province));
    CREATE TABLE source_log(source_url TEXT PRIMARY KEY,source_type TEXT,status TEXT,error TEXT,collected_at TEXT);
    CREATE INDEX idx_pos_filter ON positions(year,province,city,position_category);
    CREATE INDEX idx_pos_code ON positions(year,province,position_code);
    CREATE INDEX idx_req ON position_requirements(education_raw,major_raw,political_status_raw,age_raw);
    """)
    return con


def first(data: dict[str,Any], *keys: str) -> str | None:
    for key in keys:
        if key in data and clean(data[key]) is not None: return clean(data[key])
    return None


def ingest_year(con: sqlite3.Connection, slug: str, province: str, year: int, workers: int) -> dict[str,Any]:
    root = f"https://{slug}.gwyzwb.com/{year}.html"
    unit_pages, expected, discovery_logs = discover_unit_pages(root, year)
    failed_units = 0; summaries: dict[str,dict[str,Any]] = {}
    for log in discovery_logs:
        con.execute("INSERT OR REPLACE INTO source_log VALUES (?,?,?,?,?)",(log["url"],log["type"],"ok" if log.get("ok") else "error",log.get("error"),now()))
    def unit_task(url: str): return url, fetch(url)
    with ThreadPoolExecutor(max_workers=max(1,min(workers,6))) as pool:
        future_map = {pool.submit(unit_task,url):url for url in unit_pages}
        for n,future in enumerate(as_completed(future_map),1):
            url = future_map[future]
            try:
                _,html = future.result(); parsed = parse_unit_page(url,html)
                for row in parsed:
                    detail = row.get("detail_url")
                    if detail: summaries[detail] = row
                con.execute("INSERT OR REPLACE INTO source_log VALUES (?,?,?,?,?)",(url,"unit","ok",None,now()))
            except Exception as exc:
                failed_units += 1; con.execute("INSERT OR REPLACE INTO source_log VALUES (?,?,?,?,?)",(url,"unit","error",repr(exc),now()))
            if n % 100 == 0: con.commit(); print("UNIT_PROGRESS",slug,year,n,len(unit_pages),len(summaries),flush=True)
    detail_urls = sorted(summaries)
    failed_details = 0; inserted = 0
    def detail_task(url: str): return url, parse_detail(url,fetch(url))
    with ThreadPoolExecutor(max_workers=max(1,min(workers,8))) as pool:
        future_map = {pool.submit(detail_task,url):url for url in detail_urls}
        for n,future in enumerate(as_completed(future_map),1):
            url = future_map[future]; summary = summaries[url]
            try:
                _,raw = future.result()
                code = first(raw,"职位代码","岗位代码") or summary.get("position_code")
                code_norm = re.sub(r"\s+", "", code or "")
                name = first(raw,"职位名称","岗位名称") or summary.get("position_name")
                if not code_norm or not name: raise ValueError("missing position code/name")
                pid = f"SK-{year}-{slug.upper()}-{code_norm}"
                raw_json = json.dumps(raw,ensure_ascii=False,sort_keys=True)
                raw_hash = hashlib.sha256(f"{year}|{slug}|{raw_json}".encode()).hexdigest()
                cur = con.execute("INSERT OR IGNORE INTO raw_positions(year,province,source_url,source_record_json,source_record_hash) VALUES (?,?,?,?,?)",(year,province,url,raw_json,raw_hash))
                raw_id = cur.lastrowid if cur.rowcount else con.execute("SELECT raw_position_id FROM raw_positions WHERE source_record_hash=?",(raw_hash,)).fetchone()[0]
                city = first(raw,"地区","工作地点")
                org = first(raw,"招录机关","招考单位","招聘单位","用人单位")
                count = integer(first(raw,"招录人数","招考人数","招聘人数")) or summary.get("recruit_count")
                remarks = first(raw,"备注（职位简介）","备注","职位简介","岗位简介")
                con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
                    pid,raw_id,year,"省考",province,city,org,first(raw,"机构性质","单位性质"),first(raw,"机构层级"),name,code_norm,count,
                    first(raw,"职位类别","岗位类别"),first(raw,"职务层次","职位层次"),first(raw,"申论类别","试卷类型"),first(raw,"专业科目","专业能力测试"),
                    first(raw,"咨询电话","联系电话"),remarks,url,summary.get("unit_url"),"B-公开职位库归档；资格以官方附件为准"))
                age = first(raw,"年龄","年龄要求"); education = first(raw,"学历","学历要求") or summary.get("education_raw"); degree = first(raw,"学位","学位要求")
                major = first(raw,"专业","专业要求") or summary.get("major_raw"); political = first(raw,"政治面貌","其他资格"); experience = first(raw,"经历要求","基层工作经历","基层工作最低年限")
                other = first(raw,"其他资格","其他条件","资格条件")
                rule = {"age_raw":age,"education_raw":education,"degree_raw":degree,"major_raw":major,"political_status_raw":political,"experience_raw":experience,"other_qualification_raw":other,"remarks_raw":remarks}
                con.execute("INSERT OR REPLACE INTO position_requirements VALUES (?,?,?,?,?,?,?,?,?)",(pid,age,education,degree,major,political,experience,other,json.dumps(rule,ensure_ascii=False)))
                registered = integer(first(raw,"报名人数")); approved = integer(first(raw,"合格人数","审核通过人数","过审人数")); paid = integer(first(raw,"缴费人数"))
                registered = registered if registered is not None else summary.get("registered_count"); approved = approved if approved is not None else summary.get("approved_count"); paid = paid if paid is not None else summary.get("paid_count")
                if any(x is not None for x in (registered,approved,paid)):
                    con.execute("INSERT OR REPLACE INTO application_stats(position_id,registered_count,approved_count,paid_count,stat_scope,is_final,source_url) VALUES (?,?,?,?,?,?,?)",(pid,registered,approved,paid,"公开职位库最终/归档统计",1,url))
                con.execute("INSERT OR REPLACE INTO source_log VALUES (?,?,?,?,?)",(url,"detail","ok",None,now()))
                inserted += 1
            except Exception as exc:
                failed_details += 1; con.execute("INSERT OR REPLACE INTO source_log VALUES (?,?,?,?,?)",(url,"detail","error",repr(exc),now()))
            if n % 250 == 0: con.commit(); print("DETAIL_PROGRESS",slug,year,n,len(detail_urls),inserted,failed_details,flush=True)
    con.commit()
    loaded = con.execute("SELECT COUNT(*) FROM positions WHERE year=? AND province=?",(year,province)).fetchone()[0]
    with_app = con.execute("SELECT COUNT(DISTINCT a.position_id) FROM application_stats a JOIN positions p USING(position_id) WHERE p.year=? AND p.province=?",(year,province)).fetchone()[0]
    with_score = con.execute("SELECT COUNT(DISTINCT e.position_id) FROM entry_scores e JOIN positions p USING(position_id) WHERE p.year=? AND p.province=?",(year,province)).fetchone()[0]
    status = "complete_public_index" if expected is not None and loaded == expected else ("substantial_partial" if expected and loaded >= expected*.95 else "partial")
    con.execute("INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?,?,?,?)",(year,province,expected,loaded,with_app,with_score,failed_units,failed_details,status))
    con.commit()
    return {"expected":expected,"loaded":loaded,"with_applications":with_app,"with_entry_scores":with_score,"unit_pages":len(unit_pages),"detail_pages":len(detail_urls),"failed_unit_pages":failed_units,"failed_detail_pages":failed_details,"status":status}


def build_history(con: sqlite3.Connection) -> dict[str,int]:
    rows = con.execute("SELECT position_id,year,recruiting_org,position_name,city FROM positions").fetchall(); groups: dict[str,list[tuple]]=defaultdict(list)
    for row in rows:
        key = "|".join(norm_key(v) for v in (row[2],row[3],row[4]))
        if key.replace("|",""): groups[key].append(row)
    gc=0; lc=0
    for key,members in groups.items():
        if len({m[1] for m in members}) < 2: continue
        gid="H-"+hashlib.sha1(key.encode()).hexdigest()[:20]; sample=members[0]
        con.execute("INSERT OR IGNORE INTO position_history_groups VALUES (?,?,?,?,?)",(gid,key,sample[2],sample[3],sample[4])); gc+=1
        for m in members: con.execute("INSERT OR IGNORE INTO position_history_members VALUES (?,?)",(gid,m[0]))
        ordered=sorted(members,key=lambda x:(x[1],x[0]))
        for newer in ordered:
            older=[x for x in ordered if x[1]<newer[1]]
            if not older: continue
            old=older[-1]; con.execute("INSERT OR IGNORE INTO position_history_links(position_id,historical_position_id,relation_type,similarity_score,manual_verified,evidence_json) VALUES (?,?,?,?,?,?)",(newer[0],old[0],"exact_normalized_match",1.0,0,json.dumps({"canonical_key":key},ensure_ascii=False))); lc+=1
    con.commit(); return {"groups":gc,"links":lc}


def export_csv(con: sqlite3.Connection,path: Path) -> int:
    query="""SELECT p.*,r.age_raw,r.education_raw,r.degree_raw,r.major_raw,r.political_status_raw,r.experience_raw,r.other_qualification_raw,r.rule_json,a.registered_count,a.approved_count,a.paid_count,a.stat_scope FROM positions p LEFT JOIN position_requirements r USING(position_id) LEFT JOIN application_stats a USING(position_id) ORDER BY year,position_code"""
    cur=con.execute(query); headers=[d[0] for d in cur.description]; count=0
    with gzip.open(path,"wt",encoding="utf-8-sig",newline="") as f:
        w=csv.writer(f); w.writerow(headers)
        for row in cur: w.writerow(row); count+=1
    return count


def main() -> None:
    ap=argparse.ArgumentParser(); ap.add_argument("--province",required=True,choices=sorted(PROVINCES)); ap.add_argument("--workers",type=int,default=6); args=ap.parse_args()
    slug=args.province; province=PROVINCES[slug]; out=Path("build/provincial")/slug; out.mkdir(parents=True,exist_ok=True)
    db=out/f"provincial_{slug}_2024_2026.sqlite"; con=create_db(db); manifest={"province":province,"province_slug":slug,"generated_at":now(),"years":{},"limitations":["来源为公开职位库归档，资格判断以官方公告和职位附件为最终依据。","未公开进面分保持NULL。","2026仅包含截至构建时已公开并被职位库收录的数据。"]}
    for year in YEARS:
        try: report=ingest_year(con,slug,province,year,args.workers)
        except Exception as exc:
            report={"expected":None,"loaded":0,"with_applications":0,"with_entry_scores":0,"status":"failed","error":repr(exc)}
            con.execute("INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?,?,?,?)",(year,province,None,0,0,0,0,0,"failed")); con.commit()
        manifest["years"][str(year)]=report; print("YEAR_REPORT",slug,year,json.dumps(report,ensure_ascii=False),flush=True)
    manifest["history"]=build_history(con); manifest["csv_rows"]=export_csv(con,out/f"provincial_{slug}_2024_2026.csv.gz")
    manifest["totals"]={"positions":con.execute("SELECT COUNT(*) FROM positions").fetchone()[0],"raw_positions":con.execute("SELECT COUNT(*) FROM raw_positions").fetchone()[0],"requirements":con.execute("SELECT COUNT(*) FROM position_requirements").fetchone()[0],"application_stats":con.execute("SELECT COUNT(*) FROM application_stats").fetchone()[0],"entry_scores":con.execute("SELECT COUNT(*) FROM entry_scores").fetchone()[0],"history_groups":con.execute("SELECT COUNT(*) FROM position_history_groups").fetchone()[0],"history_links":con.execute("SELECT COUNT(*) FROM position_history_links").fetchone()[0]}
    con.commit(); con.close(); manifest["database_bytes"]=db.stat().st_size; manifest["database_sha256"]=hashlib.sha256(db.read_bytes()).hexdigest(); (out/f"provincial_{slug}_manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    print("FINAL",json.dumps(manifest["totals"],ensure_ascii=False),flush=True)

if __name__=="__main__": main()
