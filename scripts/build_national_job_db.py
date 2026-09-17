from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
import sqlite3
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

OUT = Path("build")
RAW = OUT / "raw_sources"
OUT.mkdir(parents=True, exist_ok=True)
RAW.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (compatible; DouyaJobDB/1.0; public-data-archive)"}
EXPECTED = {2024: 18948, 2025: 20810, 2026: 20714}
SOURCE_URLS = {
    2024: [
        "https://u3.huatu.com/uploads/htzximg/2024gkzw/2024gkzw.xlsx",
        "https://attachment.gaodun.com/uploads/202312/202312151105330.xls",
    ],
    2025: [
        "https://u3.huatu.com/uploads/soft/241014/ah/2025gkzw.xlsx",
        "https://imgbdb4.bendibao.com/szbdb/edu/202410/14/20241014172143_38283.zip",
    ],
    2026: [
        "https://u3.huatu.com/uploads/soft/251014/2026gkzw.xlsx",
        "https://u3.huatu.com/uploads/htzximg/2026gkzw/2026gkzw.xlsx",
    ],
}

PROVINCE_SLUGS = [
    "anhui", "beijing", "chongqing", "fujian", "gansu", "guangdong", "guangxi",
    "guizhou", "hainan", "hebei", "heilongjiang", "henan", "hubei", "hunan",
    "jiangsu", "jiangxi", "jilin", "liaoning", "neimenggu", "ningxia", "qinghai",
    "shandong", "shanghai", "shanxi", "shanxisheng", "sichuan", "tianjin", "xicang",
    "xinjiang", "yunnan", "zhejiang",
]

FIELD_ALIASES: dict[str, list[str]] = {
    "department_code": ["部门代码"],
    "department_name": ["部门名称", "招录机关"],
    "employing_department": ["用人司局", "用人单位"],
    "agency_nature": ["机构性质", "单位性质"],
    "position_name": ["招考职位", "职位名称", "岗位名称"],
    "position_attribute": ["职位属性"],
    "position_distribution": ["职位分布"],
    "position_intro": ["职位简介", "岗位简介"],
    "position_code": ["职位代码", "岗位代码"],
    "organization_level": ["机构层级"],
    "exam_category": ["考试类别", "笔试类别"],
    "recruit_count": ["招考人数", "招录人数", "招聘人数", "计划人数"],
    "major_raw": ["专业"],
    "education_raw": ["学历"],
    "degree_raw": ["学位"],
    "political_status_raw": ["政治面貌"],
    "grassroots_years_raw": ["基层工作最低年限", "基层工作经历"],
    "service_project_raw": ["服务基层项目工作经历", "服务基层项目经历"],
    "professional_test_raw": ["是否在面试阶段组织专业能力测试", "专业能力测试"],
    "interview_ratio_raw": ["面试人员比例", "面试比例"],
    "work_location": ["工作地点", "职位工作地点"],
    "settlement_location": ["落户地点"],
    "remarks_raw": ["备注"],
    "department_website": ["部门网站", "单位网站"],
    "consult_phone_1": ["咨询电话1", "咨询电话（一）", "咨询电话"],
    "consult_phone_2": ["咨询电话2", "咨询电话（二）"],
    "consult_phone_3": ["咨询电话3", "咨询电话（三）"],
}

HEADER_HINTS = {"部门代码", "部门名称", "用人司局", "招考职位", "职位代码", "招考人数", "专业", "学历"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    text = str(value).replace("\u3000", " ").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def valid_office(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 1000:
        return False
    sig = path.read_bytes()[:8]
    return sig.startswith(b"PK") or sig.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))


def get(session: requests.Session, url: str, *, stream: bool = False, attempts: int = 4) -> requests.Response:
    error: Exception | None = None
    for i in range(attempts):
        try:
            r = session.get(url, timeout=(30, 180), allow_redirects=True, stream=stream)
            if r.status_code == 200:
                return r
            error = RuntimeError(f"HTTP {r.status_code}: {url}")
        except Exception as exc:
            error = exc
        time.sleep(min(8, 1.5 * (i + 1)))
    raise RuntimeError(str(error))


def download_sources(session: requests.Session, year: int) -> tuple[list[Path], list[dict[str, Any]]]:
    year_dir = RAW / str(year)
    year_dir.mkdir(parents=True, exist_ok=True)
    logs: list[dict[str, Any]] = []
    usable: list[Path] = []
    for index, url in enumerate(SOURCE_URLS[year], 1):
        suffix = Path(urlparse(url).path).suffix.lower() or ".bin"
        dest = year_dir / f"source_{index}{suffix}"
        rec: dict[str, Any] = {"year": year, "url": url, "downloaded_at": now()}
        try:
            r = get(session, url, stream=True)
            with dest.open("wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
            rec.update({"status": 200, "final_url": r.url, "bytes": dest.stat().st_size, "sha256": sha256(dest), "content_type": r.headers.get("content-type")})
            if zipfile.is_zipfile(dest):
                target = year_dir / f"unzipped_{index}"
                target.mkdir(exist_ok=True)
                with zipfile.ZipFile(dest) as z:
                    z.extractall(target)
                usable.extend(p for p in target.rglob("*") if p.suffix.lower() in {".xls", ".xlsx", ".xlsm"})
            elif valid_office(dest):
                usable.append(dest)
            if usable:
                logs.append(rec)
                break
        except Exception as exc:
            rec["error"] = repr(exc)
        logs.append(rec)
    return usable, logs


def unique_headers(values: Iterable[Any]) -> list[str]:
    counts: dict[str, int] = defaultdict(int)
    result: list[str] = []
    for i, value in enumerate(values):
        base = re.sub(r"\s+", "", clean(value) or f"unnamed_{i+1}")
        counts[base] += 1
        result.append(base if counts[base] == 1 else f"{base}_{counts[base]}")
    return result


def header_row(df: pd.DataFrame) -> int | None:
    best: tuple[int, int] | None = None
    for idx in range(min(30, len(df))):
        joined = "|".join(clean(v) or "" for v in df.iloc[idx].tolist())
        score = sum(h in joined for h in HEADER_HINTS)
        score += 3 if "职位代码" in joined else 0
        score += 2 if ("招考人数" in joined or "招录人数" in joined) else 0
        if best is None or score > best[1]:
            best = (idx, score)
    return best[0] if best and best[1] >= 6 else None


def excel_tables(path: Path) -> Iterable[tuple[str, pd.DataFrame, int]]:
    try:
        workbook = pd.ExcelFile(path)
    except Exception as exc:
        print("WORKBOOK_ERROR", path, repr(exc), flush=True)
        return
    for sheet in workbook.sheet_names:
        try:
            raw = pd.read_excel(path, sheet_name=sheet, header=None, dtype=object)
        except Exception as exc:
            print("SHEET_ERROR", path, sheet, repr(exc), flush=True)
            continue
        idx = header_row(raw)
        if idx is None:
            continue
        data = raw.iloc[idx + 1 :].copy()
        data.columns = unique_headers(raw.iloc[idx].tolist())
        data = data.dropna(how="all")
        yield str(sheet), data, idx + 2


def pick(row: dict[str, Any], aliases: list[str]) -> str | None:
    for alias in aliases:
        if alias in row and clean(row[alias]) is not None:
            return clean(row[alias])
    for key, value in row.items():
        compact = re.sub(r"\s+", "", str(key))
        for alias in aliases:
            if compact == alias or compact.startswith(alias + "_"):
                v = clean(value)
                if v is not None:
                    return v
    return None


def integer(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value.replace(",", ""))
    return int(match.group()) if match else None


def education_rank(value: str | None) -> int | None:
    if not value:
        return None
    if "博士" in value:
        return 5
    if "硕士" in value or "研究生" in value:
        return 4
    if "本科" in value:
        return 3
    if "大专" in value or "专科" in value:
        return 2
    if "高中" in value or "中专" in value:
        return 1
    return None


def make_db(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;
    CREATE TABLE sources(source_id TEXT PRIMARY KEY,year INTEGER,source_url TEXT,final_url TEXT,local_path TEXT,sha256 TEXT,bytes INTEGER,source_level TEXT,downloaded_at TEXT,notes TEXT);
    CREATE TABLE raw_positions(raw_position_id INTEGER PRIMARY KEY AUTOINCREMENT,year INTEGER NOT NULL,exam_type TEXT NOT NULL,source_id TEXT,source_file TEXT,source_sheet TEXT,source_row_no INTEGER,source_record_json TEXT NOT NULL,source_record_hash TEXT NOT NULL,UNIQUE(year,source_record_hash));
    CREATE TABLE positions(position_id TEXT PRIMARY KEY,raw_position_id INTEGER,year INTEGER NOT NULL,exam_type TEXT NOT NULL,department_code TEXT,department_name TEXT,employing_department TEXT,agency_nature TEXT,position_name TEXT,position_attribute TEXT,position_distribution TEXT,position_intro TEXT,position_code TEXT,organization_level TEXT,exam_category TEXT,recruit_count INTEGER,work_location TEXT,settlement_location TEXT,department_website TEXT,consult_phone_1 TEXT,consult_phone_2 TEXT,consult_phone_3 TEXT,remarks_raw TEXT,detail_url TEXT,source_id TEXT);
    CREATE TABLE position_requirements(position_id TEXT PRIMARY KEY,major_raw TEXT,education_raw TEXT,education_min_rank INTEGER,degree_raw TEXT,degree_required INTEGER,political_status_raw TEXT,grassroots_years_raw TEXT,service_project_raw TEXT,professional_test_raw TEXT,interview_ratio_raw TEXT,rule_json TEXT NOT NULL);
    CREATE TABLE application_stats(stat_id INTEGER PRIMARY KEY AUTOINCREMENT,position_id TEXT NOT NULL,stat_date TEXT,registered_count INTEGER,approved_count INTEGER,paid_count INTEGER,confirmed_count INTEGER,competition_ratio REAL,stat_scope TEXT,is_final INTEGER DEFAULT 0,source_url TEXT,source_note TEXT,UNIQUE(position_id,stat_scope,source_url));
    CREATE TABLE entry_scores(score_id INTEGER PRIMARY KEY AUTOINCREMENT,position_id TEXT NOT NULL,score_type TEXT,min_written_score REAL,min_entry_score REAL,max_entry_score REAL,min_final_score REAL,max_final_score REAL,source_url TEXT,source_note TEXT);
    CREATE TABLE position_history_groups(history_group_id TEXT PRIMARY KEY,canonical_key TEXT NOT NULL,canonical_org TEXT,canonical_position_name TEXT,canonical_location TEXT);
    CREATE TABLE position_history_members(history_group_id TEXT NOT NULL,position_id TEXT NOT NULL,PRIMARY KEY(history_group_id,position_id));
    CREATE TABLE position_history_links(link_id INTEGER PRIMARY KEY AUTOINCREMENT,position_id TEXT NOT NULL,historical_position_id TEXT NOT NULL,relation_type TEXT NOT NULL,similarity_score REAL,manual_verified INTEGER DEFAULT 0,evidence_json TEXT,UNIQUE(position_id,historical_position_id));
    CREATE TABLE coverage(year INTEGER PRIMARY KEY,expected_positions INTEGER,loaded_positions INTEGER,positions_with_application_count INTEGER,positions_with_entry_score INTEGER,status TEXT,notes TEXT);
    CREATE INDEX idx_positions_filter ON positions(year,work_location,organization_level,exam_category);
    CREATE INDEX idx_positions_code ON positions(year,position_code);
    CREATE INDEX idx_req_filter ON position_requirements(education_min_rank,political_status_raw);
    CREATE INDEX idx_app_position ON application_stats(position_id);
    """)
    return con


def ingest_excel(con: sqlite3.Connection, year: int, paths: list[Path], logs: list[dict[str, Any]]) -> dict[str, int]:
    for log in logs:
        if not log.get("sha256"):
            continue
        sid = hashlib.sha1(f"{year}|{log['sha256']}".encode()).hexdigest()
        con.execute("INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)", (sid,year,log.get("url"),log.get("final_url"),None,log.get("sha256"),log.get("bytes"),"B-public mirror of official attachment",log.get("downloaded_at"),None))
    inserted = 0
    duplicate = 0
    codes: set[str] = set()
    for path in paths:
        if not valid_office(path):
            continue
        file_hash = sha256(path)
        sid = hashlib.sha1(f"{year}|{file_hash}".encode()).hexdigest()
        con.execute("INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)", (sid,year,None,None,str(path),file_hash,path.stat().st_size,"B-public mirror of official attachment",now(),"Parsed workbook"))
        for sheet, frame, first_row in excel_tables(path):
            for offset, record in enumerate(frame.to_dict(orient="records")):
                raw = {str(k): clean(v) for k, v in record.items()}
                mapped = {key: pick(raw, aliases) for key, aliases in FIELD_ALIASES.items()}
                code = mapped.get("position_code")
                name = mapped.get("position_name")
                count = integer(mapped.get("recruit_count"))
                if not code or not re.search(r"\d{5,}", code) or not name or count is None:
                    continue
                code_key = re.sub(r"\D", "", code)
                if code_key in codes:
                    duplicate += 1
                    continue
                codes.add(code_key)
                raw_json = json.dumps(raw, ensure_ascii=False, sort_keys=True)
                raw_hash = hashlib.sha256(f"{year}|{raw_json}".encode()).hexdigest()
                cur = con.execute("INSERT OR IGNORE INTO raw_positions(year,exam_type,source_id,source_file,source_sheet,source_row_no,source_record_json,source_record_hash) VALUES (?,?,?,?,?,?,?,?)", (year,"国考",sid,str(path),sheet,first_row+offset,raw_json,raw_hash))
                if cur.rowcount == 0:
                    duplicate += 1
                    continue
                pid = f"GK-{year}-{code_key}"
                values = (
                    pid,cur.lastrowid,year,"国考",mapped.get("department_code"),mapped.get("department_name"),mapped.get("employing_department"),mapped.get("agency_nature"),name,mapped.get("position_attribute"),mapped.get("position_distribution"),mapped.get("position_intro"),code_key,mapped.get("organization_level"),mapped.get("exam_category"),count,mapped.get("work_location"),mapped.get("settlement_location"),mapped.get("department_website"),mapped.get("consult_phone_1"),mapped.get("consult_phone_2"),mapped.get("consult_phone_3"),mapped.get("remarks_raw"),None,sid
                )
                con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
                degree = mapped.get("degree_raw")
                rule = {
                    "major_raw": mapped.get("major_raw"),
                    "education_raw": mapped.get("education_raw"),
                    "education_min_rank": education_rank(mapped.get("education_raw")),
                    "degree_raw": degree,
                    "degree_required": bool(degree and "不限" not in degree and "无要求" not in degree),
                    "political_status_raw": mapped.get("political_status_raw"),
                    "grassroots_years_raw": mapped.get("grassroots_years_raw"),
                    "service_project_raw": mapped.get("service_project_raw"),
                    "professional_test_raw": mapped.get("professional_test_raw"),
                    "interview_ratio_raw": mapped.get("interview_ratio_raw"),
                    "remarks_raw": mapped.get("remarks_raw"),
                }
                con.execute("INSERT OR REPLACE INTO position_requirements VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (pid,mapped.get("major_raw"),mapped.get("education_raw"),rule["education_min_rank"],degree,int(rule["degree_required"]),mapped.get("political_status_raw"),mapped.get("grassroots_years_raw"),mapped.get("service_project_raw"),mapped.get("professional_test_raw"),mapped.get("interview_ratio_raw"),json.dumps(rule,ensure_ascii=False)))
                inserted += 1
    con.commit()
    return {"inserted": inserted, "duplicates_skipped": duplicate}


def parse_position_table(html: str, base_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    results: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [clean(x.get_text(" ", strip=True)) or "" for x in rows[0].find_all(["th", "td"])]
        joined = "|".join(headers)
        if ("岗位代码" not in joined and "职位代码" not in joined) or "报名人数" not in joined:
            continue
        normalized = {re.sub(r"\s+", "", h): i for i, h in enumerate(headers)}
        for tr in rows[1:]:
            cells = tr.find_all("td")
            values = [clean(td.get_text(" ", strip=True)) for td in cells]
            if not values:
                continue
            def value(*names: str) -> str | None:
                for n in names:
                    idx = normalized.get(n)
                    if idx is not None and idx < len(values):
                        return values[idx]
                return None
            code = value("岗位代码", "职位代码")
            applicants = integer(value("报名人数"))
            if not code or applicants is None:
                continue
            anchor = tr.find("a", href=True)
            results.append({"position_code": re.sub(r"\D", "", code), "registered_count": applicants, "detail_url": urljoin(base_url, anchor["href"]) if anchor else None})
    return results


def scrape_applications(con: sqlite3.Connection, session: requests.Session, year: int) -> dict[str, int]:
    region_urls: set[str] = set()
    dept_urls: set[str] = set()
    errors = 0
    updated = 0
    source_pages = 0
    root = f"https://www.gwyzwb.com/{year}.html"
    try:
        soup = BeautifulSoup(get(session, root).text, "lxml")
        for a in soup.find_all("a", href=True):
            href = urljoin(root, a["href"])
            parsed = urlparse(href)
            if parsed.netloc == "www.gwyzwb.com" and re.fullmatch(r"/[a-z]+/" + str(year) + r"\.html", parsed.path):
                region_urls.add(href)
    except Exception as exc:
        print("ROOT_SCRAPE_ERROR", year, repr(exc), flush=True)
        return {"updated": 0, "region_pages": 0, "department_pages": 0, "errors": 1}
    for region in sorted(region_urls):
        try:
            html = get(session, region).text
            source_pages += 1
            soup = BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = urljoin(region, a["href"])
                parsed = urlparse(href)
                if parsed.netloc == "www.gwyzwb.com" and re.fullmatch(r"/[a-z]+/" + str(year) + r"_\d+\.html", parsed.path):
                    dept_urls.add(href)
        except Exception as exc:
            errors += 1
            print("REGION_SCRAPE_ERROR", region, repr(exc), flush=True)
    for index, dept in enumerate(sorted(dept_urls), 1):
        try:
            html = get(session, dept).text
            source_pages += 1
            for row in parse_position_table(html, dept):
                pid = f"GK-{year}-{row['position_code']}"
                exists = con.execute("SELECT 1 FROM positions WHERE position_id=?", (pid,)).fetchone()
                if not exists:
                    continue
                con.execute("UPDATE positions SET detail_url=COALESCE(detail_url,?) WHERE position_id=?", (row["detail_url"], pid))
                con.execute("INSERT OR IGNORE INTO application_stats(position_id,stat_date,registered_count,approved_count,paid_count,confirmed_count,competition_ratio,stat_scope,is_final,source_url,source_note) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (pid,None,row["registered_count"],None,None,None,None,"第三方职位库归档报名人数",1,dept,"公开归档值；资格条件以官方职位表为准"))
                updated += 1
            if index % 50 == 0:
                con.commit()
                print("APPLICATION_PROGRESS", year, index, len(dept_urls), updated, flush=True)
            time.sleep(0.04)
        except Exception as exc:
            errors += 1
            print("DEPARTMENT_SCRAPE_ERROR", dept, repr(exc), flush=True)
    con.commit()
    return {"updated": updated, "region_pages": len(region_urls), "department_pages": len(dept_urls), "source_pages": source_pages, "errors": errors}


def canonical(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[\s（）()【】\[\]、，,。.;；:：/\\_-]+", "", value).lower()


def build_history(con: sqlite3.Connection) -> dict[str, int]:
    rows = con.execute("SELECT position_id,year,department_name,employing_department,position_name,work_location FROM positions").fetchall()
    groups: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    for row in rows:
        key = "|".join(canonical(v) for v in (row[2],row[3],row[4],row[5]))
        if key.replace("|", ""):
            groups[key].append(row)
    group_count = 0
    link_count = 0
    for key, members in groups.items():
        if len({m[1] for m in members}) < 2:
            continue
        gid = "H-" + hashlib.sha1(key.encode()).hexdigest()[:20]
        sample = members[0]
        con.execute("INSERT OR IGNORE INTO position_history_groups VALUES (?,?,?,?,?)", (gid,key,sample[2],sample[4],sample[5]))
        group_count += 1
        for member in members:
            con.execute("INSERT OR IGNORE INTO position_history_members VALUES (?,?)", (gid,member[0]))
        ordered = sorted(members, key=lambda x:(x[1],x[0]))
        for newer in ordered:
            prior = [item for item in ordered if item[1] < newer[1]]
            if not prior:
                continue
            older = prior[-1]
            con.execute("INSERT OR IGNORE INTO position_history_links(position_id,historical_position_id,relation_type,similarity_score,manual_verified,evidence_json) VALUES (?,?,?,?,?,?)", (newer[0],older[0],"exact_normalized_match",1.0,0,json.dumps({"canonical_key":key},ensure_ascii=False)))
            link_count += 1
    con.commit()
    return {"groups": group_count, "links": link_count}


def export(con: sqlite3.Connection) -> int:
    query = """SELECT p.*,r.major_raw,r.education_raw,r.education_min_rank,r.degree_raw,r.degree_required,r.political_status_raw,r.grassroots_years_raw,r.service_project_raw,r.professional_test_raw,r.interview_ratio_raw,r.rule_json,a.registered_count,a.stat_scope FROM positions p LEFT JOIN position_requirements r USING(position_id) LEFT JOIN application_stats a ON a.position_id=p.position_id AND a.is_final=1 ORDER BY p.year,p.position_code"""
    cur = con.execute(query)
    headers = [d[0] for d in cur.description]
    count = 0
    with gzip.open(OUT / "national_positions_2024_2026.csv.gz", "wt", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for row in cur:
            writer.writerow(row)
            count += 1
    return count


def main() -> None:
    session = requests.Session()
    session.headers.update(UA)
    db_path = OUT / "national_civil_service_2024_2026.sqlite"
    con = make_db(db_path)
    manifest: dict[str, Any] = {"generated_at": now(), "scope": "2024-2026国家公务员职位", "source_downloads": [], "years": {}, "limitations": ["报名人数来自公开职位库归档，非所有岗位均有公开值。", "entry_scores表已建；未公开或未完成采集的进面分保持NULL，绝不以0或估算替代。", "资格判定应以官方职位表原文为最终依据。"]}
    for year in (2024,2025,2026):
        paths, logs = download_sources(session, year)
        manifest["source_downloads"].extend(logs)
        excel_report = ingest_excel(con, year, paths, logs)
        app_report = scrape_applications(con, session, year)
        loaded = con.execute("SELECT COUNT(*) FROM positions WHERE year=?", (year,)).fetchone()[0]
        with_app = con.execute("SELECT COUNT(DISTINCT position_id) FROM application_stats WHERE position_id LIKE ?", (f"GK-{year}-%",)).fetchone()[0]
        with_score = con.execute("SELECT COUNT(DISTINCT position_id) FROM entry_scores WHERE position_id LIKE ?", (f"GK-{year}-%",)).fetchone()[0]
        expected = EXPECTED[year]
        status = "complete_positions" if loaded == expected else ("substantial_partial" if loaded >= expected*0.95 else "partial")
        con.execute("INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?,?)", (year,expected,loaded,with_app,with_score,status,"岗位行数按公开年度职位总数核验；报名与进面分单列覆盖率。"))
        manifest["years"][str(year)] = {"expected_positions":expected,"loaded_positions":loaded,"positions_with_application_count":with_app,"positions_with_entry_score":with_score,"status":status,"excel":excel_report,"applications":app_report}
        con.commit()
        print("YEAR_REPORT", year, json.dumps(manifest["years"][str(year)],ensure_ascii=False), flush=True)
    manifest["history"] = build_history(con)
    manifest["csv_rows"] = export(con)
    manifest["totals"] = {
        "positions": con.execute("SELECT COUNT(*) FROM positions").fetchone()[0],
        "raw_positions": con.execute("SELECT COUNT(*) FROM raw_positions").fetchone()[0],
        "requirements": con.execute("SELECT COUNT(*) FROM position_requirements").fetchone()[0],
        "application_stats": con.execute("SELECT COUNT(*) FROM application_stats").fetchone()[0],
        "entry_scores": con.execute("SELECT COUNT(*) FROM entry_scores").fetchone()[0],
        "history_groups": con.execute("SELECT COUNT(*) FROM position_history_groups").fetchone()[0],
        "history_links": con.execute("SELECT COUNT(*) FROM position_history_links").fetchone()[0],
    }
    con.commit()
    con.close()
    manifest["database_bytes"] = db_path.stat().st_size
    manifest["database_sha256"] = sha256(db_path)
    (OUT / "national_manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    (OUT / "README.txt").write_text("2024-2026国家公务员真实岗位数据库。请先查看national_manifest.json核验年度行数与报名/进面分覆盖率。SQLite表：sources、raw_positions、positions、position_requirements、application_stats、entry_scores、position_history_groups、position_history_members、position_history_links、coverage。未知数据使用NULL。\n",encoding="utf-8")
    print("FINAL_TOTALS", json.dumps(manifest["totals"],ensure_ascii=False), flush=True)

if __name__ == "__main__":
    main()
