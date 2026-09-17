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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

PROVINCES = {
    "ah": "安徽", "bj": "北京", "cq": "重庆", "fj": "福建", "gs": "甘肃", "gd": "广东",
    "gx": "广西", "gz": "贵州", "hi": "海南", "he": "河北", "ha": "河南", "hl": "黑龙江",
    "hb": "湖北", "hn": "湖南", "jl": "吉林", "js": "江苏", "jx": "江西", "ln": "辽宁",
    "nm": "内蒙古", "nx": "宁夏", "qh": "青海", "sd": "山东", "sx": "山西", "sn": "陕西",
    "sh": "上海", "sc": "四川", "tj": "天津", "xz": "西藏", "xj": "新疆", "yn": "云南",
    "zj": "浙江"
}
DEFAULT_YEARS = (2024, 2025, 2026)
UA = {"User-Agent": "Mozilla/5.0 (compatible; DouyaJobDB/2.0; public-data-archive)"}
THREAD = threading.local()
RESERVED = {
    "search", "bmrs", "zhuanye", "news", "about", "book", "v", "zt", "rank", "fenshu",
    "tiaoji", "mingdan", "paiming", "default", "favicon.ico"
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_session() -> requests.Session:
    current = getattr(THREAD, "session", None)
    if current is None:
        current = requests.Session()
        current.headers.update(UA)
        THREAD.session = current
    return current


def fetch(url: str, attempts: int = 4) -> str:
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = get_session().get(url, timeout=(20, 90), allow_redirects=True)
            if response.status_code == 200 and len(response.content) > 100:
                response.encoding = response.apparent_encoding or response.encoding
                return response.text
            error = RuntimeError(f"HTTP {response.status_code}: {url}")
        except Exception as exc:
            error = exc
        time.sleep(min(6, 1.2 * (attempt + 1)))
    raise RuntimeError(str(error))


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def normalize_label(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[\s：:（）()]+", "", value)


def integer(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value.replace(",", ""))
    return int(match.group()) if match else None


def norm_key(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[\s（）()【】\[\]、，,。.;；:：/\\_-]+", "", value).lower()


def same_host(base: str, href: str) -> str | None:
    result = urljoin(base, href).split("#", 1)[0]
    if urlparse(result).netloc != urlparse(base).netloc:
        return None
    return result


def first_match(text: str, patterns: list[str]) -> int | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def advertised_position_count(html: str) -> int | None:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    candidates = [
        r"(\d+)\s*招聘岗位数",
        r"招聘岗位数\s*[：:]?\s*(\d+)",
        r"共\s*(\d+)\s*个职位",
        r"\((\d+)\s*个职位[、，,]",
        r"(\d+)\s*个职位[、，,]\s*\d+\s*人",
        r"(\d+)\s*个岗位[、，,]\s*\d+\s*人",
    ]
    values: list[int] = []
    for pattern in candidates:
        for match in re.finditer(pattern, text):
            values.append(int(match.group(1)))
    return max(values) if values else None


def area_position_count(html: str) -> int | None:
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    return first_match(text, [r"\((\d+)\s*个职位[、，,]", r"共\s*(\d+)\s*个职位"])


def url_path(url: str) -> str:
    return urlparse(url).path


def discover_pages(root: str, year: int) -> tuple[set[str], set[str], int | None, list[dict[str, Any]]]:
    root_html = fetch(root)
    expected = advertised_position_count(root_html)
    host = urlparse(root).netloc
    soup = BeautifulSoup(root_html, "lxml")
    area_candidates: set[str] = set()
    region_pages: set[str] = set()
    unit_pages: set[str] = set()
    logs: list[dict[str, Any]] = []

    unit_patterns = [
        re.compile(rf"^/[A-Za-z0-9_-]+/{year}_\d+\.html$"),
        re.compile(rf"^/{year}_\d+\.html$"),
    ]
    region_pattern = re.compile(rf"^/[A-Za-z0-9_-]+/{year}\.html$")
    plain_area_pattern = re.compile(r"^/([A-Za-z0-9_-]+)/?$")

    for anchor in soup.find_all("a", href=True):
        linked = same_host(root, anchor["href"])
        if not linked:
            continue
        path = url_path(linked)
        if any(pattern.fullmatch(path) for pattern in unit_patterns):
            unit_pages.add(linked)
            continue
        if region_pattern.fullmatch(path):
            region_pages.add(linked)
            continue
        plain = plain_area_pattern.fullmatch(path)
        if plain:
            area = plain.group(1).lower()
            if area not in RESERVED and not area.isdigit():
                area_candidates.add(area)

    for area in sorted(area_candidates):
        region_pages.add(f"https://{host}/{area}/{year}.html")

    accepted_regions: set[str] = set()
    expected_area_sum = 0
    expected_area_count = 0
    for region in sorted(region_pages):
        try:
            html = fetch(region, attempts=2)
            page_text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
            if str(year) not in page_text or ("职位" not in page_text and "岗位" not in page_text):
                continue
            region_soup = BeautifulSoup(html, "lxml")
            found = 0
            for anchor in region_soup.find_all("a", href=True):
                linked = same_host(region, anchor["href"])
                if linked and any(pattern.fullmatch(url_path(linked)) for pattern in unit_patterns):
                    unit_pages.add(linked)
                    found += 1
            if found:
                accepted_regions.add(region)
                count = area_position_count(html)
                if count is not None:
                    expected_area_sum += count
                    expected_area_count += 1
                logs.append({"url": region, "type": "region", "status": "ok", "unit_links": found, "position_count": count})
        except Exception as exc:
            logs.append({"url": region, "type": "region", "status": "error", "error": repr(exc)})

    if expected is None and expected_area_count:
        expected = expected_area_sum
    logs.insert(0, {
        "url": root, "type": "root", "status": "ok", "expected_positions": expected,
        "plain_area_candidates": len(area_candidates), "accepted_regions": len(accepted_regions),
        "unit_links": len(unit_pages)
    })
    return accepted_regions, unit_pages, expected, logs


def header_cells(table) -> list[str]:
    for row in table.find_all("tr")[:4]:
        cells = row.find_all(["th", "td"])
        labels = [clean(cell.get_text(" ", strip=True)) or "" for cell in cells]
        joined = "|".join(labels)
        if ("职位代码" in joined or "岗位代码" in joined) and ("职位名称" in joined or "岗位名称" in joined):
            return labels
    return []


def parse_unit_page(url: str, html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    parsed: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        headers = header_cells(table)
        if not headers:
            continue
        normalized = {normalize_label(label): index for index, label in enumerate(headers)}
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            values = [clean(cell.get_text(" ", strip=True)) for cell in cells]
            if len(values) < 2:
                continue

            def value(*names: str) -> str | None:
                for name in names:
                    index = normalized.get(normalize_label(name))
                    if index is not None and index < len(values):
                        return values[index]
                return None

            code = value("职位代码", "岗位代码")
            name = value("职位名称", "岗位名称")
            if not code or not name:
                continue
            detail_url = None
            for anchor in row.find_all("a", href=True):
                candidate = same_host(url, anchor["href"])
                if candidate and re.search(r"/\d{4}/\d+\.html$", url_path(candidate)):
                    detail_url = candidate
                    break
            if detail_url is None:
                anchor = row.find("a", href=True)
                detail_url = same_host(url, anchor["href"]) if anchor else None
            parsed.append({
                "position_code": code,
                "position_name": name,
                "education_raw": value("学历要求", "学历"),
                "major_raw": value("专业要求", "专业"),
                "recruit_count": integer(value("招考人数", "招录人数", "招聘人数")),
                "registered_count": integer(value("报名人数")),
                "approved_count": integer(value("合格人数", "审核通过人数", "过审人数")),
                "paid_count": integer(value("缴费人数")),
                "detail_url": detail_url,
                "unit_url": url,
            })
    return parsed


def parse_detail(url: str, html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    best: dict[str, str | None] = {}
    for table in soup.find_all("table"):
        values: dict[str, str | None] = {}
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            texts = [clean(cell.get_text(" ", strip=True)) for cell in cells]
            for index in range(0, len(texts) - 1, 2):
                key = texts[index]
                val = texts[index + 1]
                if key and len(key) <= 40:
                    values[normalize_label(key)] = val
        if len(values) > len(best):
            best = values
    best["source_url"] = url
    return best


def first(data: dict[str, Any], *labels: str) -> str | None:
    for label in labels:
        normalized = normalize_label(label)
        if normalized in data and clean(data[normalized]) is not None:
            return clean(data[normalized])
    return None


def create_database(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE raw_positions(
          raw_position_id INTEGER PRIMARY KEY AUTOINCREMENT,
          year INTEGER, province TEXT, source_url TEXT,
          source_record_json TEXT, source_record_hash TEXT UNIQUE
        );
        CREATE TABLE positions(
          position_id TEXT PRIMARY KEY, raw_position_id INTEGER, year INTEGER,
          exam_type TEXT, province TEXT, city TEXT, recruiting_org TEXT,
          agency_nature TEXT, organization_level TEXT, position_name TEXT,
          position_code TEXT, recruit_count INTEGER, position_category TEXT,
          duty_rank TEXT, essay_category TEXT, professional_subject TEXT,
          consult_phone TEXT, remarks_raw TEXT, detail_url TEXT, unit_url TEXT,
          source_level TEXT
        );
        CREATE TABLE position_requirements(
          position_id TEXT PRIMARY KEY, age_raw TEXT, education_raw TEXT,
          degree_raw TEXT, major_raw TEXT, political_status_raw TEXT,
          experience_raw TEXT, fresh_graduate_raw TEXT, hukou_raw TEXT,
          service_project_raw TEXT, other_qualification_raw TEXT, rule_json TEXT
        );
        CREATE TABLE application_stats(
          stat_id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT,
          registered_count INTEGER, approved_count INTEGER, paid_count INTEGER,
          stat_scope TEXT, is_final INTEGER, source_url TEXT,
          UNIQUE(position_id, stat_scope)
        );
        CREATE TABLE entry_scores(
          score_id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT,
          score_type TEXT, min_written_score REAL, min_entry_score REAL,
          max_entry_score REAL, min_final_score REAL, max_final_score REAL,
          source_url TEXT, source_note TEXT
        );
        CREATE TABLE position_history_groups(
          history_group_id TEXT PRIMARY KEY, canonical_key TEXT,
          canonical_org TEXT, canonical_position_name TEXT, canonical_city TEXT
        );
        CREATE TABLE position_history_members(
          history_group_id TEXT, position_id TEXT,
          PRIMARY KEY(history_group_id, position_id)
        );
        CREATE TABLE position_history_links(
          link_id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT,
          historical_position_id TEXT, relation_type TEXT,
          similarity_score REAL, manual_verified INTEGER, evidence_json TEXT,
          UNIQUE(position_id, historical_position_id)
        );
        CREATE TABLE coverage(
          year INTEGER, province TEXT, expected_positions INTEGER,
          loaded_positions INTEGER, with_applications INTEGER,
          with_entry_scores INTEGER, region_pages INTEGER, unit_pages INTEGER,
          detail_pages INTEGER, failed_unit_pages INTEGER,
          failed_detail_pages INTEGER, status TEXT,
          PRIMARY KEY(year, province)
        );
        CREATE TABLE source_log(
          source_url TEXT PRIMARY KEY, source_type TEXT, status TEXT,
          error TEXT, collected_at TEXT
        );
        CREATE INDEX idx_positions_filter
          ON positions(year, province, city, position_category);
        CREATE INDEX idx_positions_code
          ON positions(year, province, position_code);
        CREATE INDEX idx_requirements
          ON position_requirements(education_raw, major_raw, political_status_raw, age_raw);
        """
    )
    return connection


def position_id(year: int, province_slug: str, code: str, detail_url: str, organization: str | None, name: str) -> str:
    normalized_code = re.sub(r"\s+", "", code)
    suffix = hashlib.sha1(f"{detail_url}|{organization or ''}|{name}".encode()).hexdigest()[:8]
    return f"SK-{year}-{province_slug.upper()}-{normalized_code}-{suffix}"


def ingest_year(connection: sqlite3.Connection, slug: str, province: str, year: int, workers: int) -> dict[str, Any]:
    root = f"https://{slug}.gwyzwb.com/{year}.html"
    regions, unit_pages, expected, discovery_logs = discover_pages(root, year)
    for record in discovery_logs:
        connection.execute(
            "INSERT OR REPLACE INTO source_log VALUES (?,?,?,?,?)",
            (record["url"], record["type"], record.get("status", "ok"), record.get("error"), utc_now()),
        )

    summaries: dict[str, dict[str, Any]] = {}
    failed_units = 0

    def unit_task(url: str):
        return url, fetch(url)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 10))) as executor:
        futures = {executor.submit(unit_task, url): url for url in unit_pages}
        for index, future in enumerate(as_completed(futures), 1):
            url = futures[future]
            try:
                _, html = future.result()
                rows = parse_unit_page(url, html)
                for row in rows:
                    detail = row.get("detail_url")
                    if detail:
                        summaries[detail] = row
                connection.execute(
                    "INSERT OR REPLACE INTO source_log VALUES (?,?,?,?,?)",
                    (url, "unit", "ok", None, utc_now()),
                )
            except Exception as exc:
                failed_units += 1
                connection.execute(
                    "INSERT OR REPLACE INTO source_log VALUES (?,?,?,?,?)",
                    (url, "unit", "error", repr(exc), utc_now()),
                )
            if index % 100 == 0:
                connection.commit()
                print("UNIT_PROGRESS", slug, year, index, len(unit_pages), len(summaries), flush=True)

    failed_details = 0
    inserted = 0

    def detail_task(url: str):
        return url, parse_detail(url, fetch(url))

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 12))) as executor:
        futures = {executor.submit(detail_task, url): url for url in sorted(summaries)}
        for index, future in enumerate(as_completed(futures), 1):
            url = futures[future]
            summary = summaries[url]
            try:
                _, raw = future.result()
                code = first(raw, "职位代码", "岗位代码") or summary.get("position_code")
                name = first(raw, "职位名称", "岗位名称") or summary.get("position_name")
                if not code or not name:
                    raise ValueError("missing position code or name")
                organization = first(raw, "招录机关", "招考单位", "招聘单位", "用人单位")
                pid = position_id(year, slug, code, url, organization, name)
                combined_raw = {"detail": raw, "unit_summary": summary}
                raw_json = json.dumps(combined_raw, ensure_ascii=False, sort_keys=True)
                raw_hash = hashlib.sha256(f"{year}|{slug}|{url}|{raw_json}".encode()).hexdigest()
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO raw_positions(year,province,source_url,source_record_json,source_record_hash) VALUES (?,?,?,?,?)",
                    (year, province, url, raw_json, raw_hash),
                )
                if cursor.rowcount:
                    raw_id = cursor.lastrowid
                else:
                    raw_id = connection.execute(
                        "SELECT raw_position_id FROM raw_positions WHERE source_record_hash=?", (raw_hash,)
                    ).fetchone()[0]

                city = first(raw, "地区", "工作地点", "招录地区")
                recruit_count = integer(first(raw, "招录人数", "招考人数", "招聘人数"))
                if recruit_count is None:
                    recruit_count = summary.get("recruit_count")
                remarks = first(raw, "备注职位简介", "备注", "职位简介", "岗位简介")
                connection.execute(
                    "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        pid, raw_id, year, "省考", province, city, organization,
                        first(raw, "机构性质", "单位性质"), first(raw, "机构层级"),
                        name, re.sub(r"\s+", "", code), recruit_count,
                        first(raw, "职位类别", "岗位类别"), first(raw, "职务层次", "职位层次"),
                        first(raw, "申论类别", "试卷类型"), first(raw, "专业科目", "专业能力测试"),
                        first(raw, "咨询电话", "联系电话"), remarks, url,
                        summary.get("unit_url"), "B-公开职位库归档；资格以官方公告及职位附件为准",
                    ),
                )

                age = first(raw, "年龄", "年龄要求")
                education = first(raw, "学历", "学历要求") or summary.get("education_raw")
                degree = first(raw, "学位", "学位要求")
                major = first(raw, "专业", "专业要求") or summary.get("major_raw")
                political = first(raw, "政治面貌")
                experience = first(raw, "经历要求", "基层工作经历", "基层工作最低年限")
                fresh = first(raw, "应届毕业生", "应届要求", "招录对象")
                hukou = first(raw, "户籍", "生源地", "户籍要求")
                service = first(raw, "服务基层项目经历", "服务基层项目工作经历")
                other = first(raw, "其他资格", "其他条件", "资格条件")
                rule = {
                    "age_raw": age,
                    "education_raw": education,
                    "degree_raw": degree,
                    "major_raw": major,
                    "political_status_raw": political,
                    "experience_raw": experience,
                    "fresh_graduate_raw": fresh,
                    "hukou_raw": hukou,
                    "service_project_raw": service,
                    "other_qualification_raw": other,
                    "remarks_raw": remarks,
                }
                connection.execute(
                    "INSERT OR REPLACE INTO position_requirements VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, age, education, degree, major, political, experience, fresh, hukou, service, other,
                     json.dumps(rule, ensure_ascii=False)),
                )

                registered = integer(first(raw, "报名人数"))
                approved = integer(first(raw, "合格人数", "审核通过人数", "过审人数"))
                paid = integer(first(raw, "缴费人数"))
                if registered is None:
                    registered = summary.get("registered_count")
                if approved is None:
                    approved = summary.get("approved_count")
                if paid is None:
                    paid = summary.get("paid_count")
                if any(value is not None for value in (registered, approved, paid)):
                    connection.execute(
                        "INSERT OR REPLACE INTO application_stats(position_id,registered_count,approved_count,paid_count,stat_scope,is_final,source_url) VALUES (?,?,?,?,?,?,?)",
                        (pid, registered, approved, paid, "公开职位库最终或归档统计", 1, url),
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO source_log VALUES (?,?,?,?,?)",
                    (url, "detail", "ok", None, utc_now()),
                )
                inserted += 1
            except Exception as exc:
                failed_details += 1
                connection.execute(
                    "INSERT OR REPLACE INTO source_log VALUES (?,?,?,?,?)",
                    (url, "detail", "error", repr(exc), utc_now()),
                )
            if index % 250 == 0:
                connection.commit()
                print("DETAIL_PROGRESS", slug, year, index, len(summaries), inserted, failed_details, flush=True)

    connection.commit()
    loaded = connection.execute(
        "SELECT COUNT(*) FROM positions WHERE year=? AND province=?", (year, province)
    ).fetchone()[0]
    with_applications = connection.execute(
        """SELECT COUNT(DISTINCT a.position_id) FROM application_stats a
           JOIN positions p USING(position_id) WHERE p.year=? AND p.province=?""",
        (year, province),
    ).fetchone()[0]
    with_scores = connection.execute(
        """SELECT COUNT(DISTINCT e.position_id) FROM entry_scores e
           JOIN positions p USING(position_id) WHERE p.year=? AND p.province=?""",
        (year, province),
    ).fetchone()[0]
    if expected is not None and loaded == expected:
        status = "complete_public_index"
    elif expected is not None and loaded >= expected * 0.98:
        status = "near_complete"
    else:
        status = "partial"
    connection.execute(
        "INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (year, province, expected, loaded, with_applications, with_scores, len(regions), len(unit_pages),
         len(summaries), failed_units, failed_details, status),
    )
    connection.commit()
    return {
        "expected_positions": expected,
        "loaded_positions": loaded,
        "with_applications": with_applications,
        "with_entry_scores": with_scores,
        "region_pages": len(regions),
        "unit_pages": len(unit_pages),
        "detail_pages": len(summaries),
        "failed_unit_pages": failed_units,
        "failed_detail_pages": failed_details,
        "status": status,
    }


def build_history(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        "SELECT position_id,year,recruiting_org,position_name,city FROM positions"
    ).fetchall()
    groups: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    for row in rows:
        key = "|".join(norm_key(value) for value in (row[2], row[3], row[4]))
        if key.replace("|", ""):
            groups[key].append(row)
    group_count = 0
    link_count = 0
    for key, members in groups.items():
        if len({member[1] for member in members}) < 2:
            continue
        group_id = "H-" + hashlib.sha1(key.encode()).hexdigest()[:20]
        sample = members[0]
        connection.execute(
            "INSERT OR IGNORE INTO position_history_groups VALUES (?,?,?,?,?)",
            (group_id, key, sample[2], sample[3], sample[4]),
        )
        group_count += 1
        for member in members:
            connection.execute(
                "INSERT OR IGNORE INTO position_history_members VALUES (?,?)", (group_id, member[0])
            )
        ordered = sorted(members, key=lambda item: (item[1], item[0]))
        for newer in ordered:
            earlier = [item for item in ordered if item[1] < newer[1]]
            if not earlier:
                continue
            older = earlier[-1]
            connection.execute(
                """INSERT OR IGNORE INTO position_history_links
                (position_id,historical_position_id,relation_type,similarity_score,manual_verified,evidence_json)
                VALUES (?,?,?,?,?,?)""",
                (newer[0], older[0], "exact_normalized_match", 1.0, 0,
                 json.dumps({"canonical_key": key}, ensure_ascii=False)),
            )
            link_count += 1
    connection.commit()
    return {"groups": group_count, "links": link_count}


def export_csv(connection: sqlite3.Connection, path: Path) -> int:
    query = """
    SELECT p.*,r.age_raw,r.education_raw,r.degree_raw,r.major_raw,
           r.political_status_raw,r.experience_raw,r.fresh_graduate_raw,
           r.hukou_raw,r.service_project_raw,r.other_qualification_raw,r.rule_json,
           a.registered_count,a.approved_count,a.paid_count,a.stat_scope
    FROM positions p
    LEFT JOIN position_requirements r USING(position_id)
    LEFT JOIN application_stats a USING(position_id)
    ORDER BY p.year,p.position_code,p.position_id
    """
    cursor = connection.execute(query)
    headers = [description[0] for description in cursor.description]
    count = 0
    with gzip.open(path, "wt", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(headers)
        for row in cursor:
            writer.writerow(row)
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--province", required=True, choices=sorted(PROVINCES))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--years", nargs="*", type=int, default=list(DEFAULT_YEARS))
    args = parser.parse_args()

    slug = args.province
    province = PROVINCES[slug]
    years = tuple(args.years)
    output = Path("build/provincial_v2") / slug
    output.mkdir(parents=True, exist_ok=True)
    database_path = output / f"provincial_{slug}_{min(years)}_{max(years)}.sqlite"
    connection = create_database(database_path)
    manifest: dict[str, Any] = {
        "province": province,
        "province_slug": slug,
        "generated_at": utc_now(),
        "years": {},
        "limitations": [
            "来源为公开职位库归档，资格判断以官方公告和职位附件为最终依据。",
            "未公开进面分保持NULL，不以0或估算替代。",
            "2026仅包含截至构建时公开并进入职位库的数据。",
        ],
    }
    for year in years:
        try:
            report = ingest_year(connection, slug, province, year, args.workers)
        except Exception as exc:
            report = {
                "expected_positions": None,
                "loaded_positions": 0,
                "with_applications": 0,
                "with_entry_scores": 0,
                "status": "failed",
                "error": repr(exc),
            }
        manifest["years"][str(year)] = report
        print("YEAR_REPORT", slug, year, json.dumps(report, ensure_ascii=False), flush=True)

    manifest["history"] = build_history(connection)
    manifest["csv_rows"] = export_csv(
        connection, output / f"provincial_{slug}_{min(years)}_{max(years)}.csv.gz"
    )
    manifest["totals"] = {
        "positions": connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0],
        "raw_positions": connection.execute("SELECT COUNT(*) FROM raw_positions").fetchone()[0],
        "requirements": connection.execute("SELECT COUNT(*) FROM position_requirements").fetchone()[0],
        "application_stats": connection.execute("SELECT COUNT(*) FROM application_stats").fetchone()[0],
        "entry_scores": connection.execute("SELECT COUNT(*) FROM entry_scores").fetchone()[0],
        "history_groups": connection.execute("SELECT COUNT(*) FROM position_history_groups").fetchone()[0],
        "history_links": connection.execute("SELECT COUNT(*) FROM position_history_links").fetchone()[0],
    }
    connection.commit()
    connection.close()
    manifest["database_bytes"] = database_path.stat().st_size
    manifest["database_sha256"] = hashlib.sha256(database_path.read_bytes()).hexdigest()
    (output / f"provincial_{slug}_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("FINAL", slug, json.dumps(manifest["totals"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
