from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
import sqlite3
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import pandas as pd
import requests

OUT = Path("build/national_v2")
RAW = OUT / "raw_sources"
OUT.mkdir(parents=True, exist_ok=True)
RAW.mkdir(parents=True, exist_ok=True)

EXPECTED = {2024: 18948, 2025: 20810, 2026: 20714}
SOURCES = {
    2024: [
        "https://u3.huatu.com/uploads/htzximg/2024gkzw/2024gkzw.xlsx",
        "https://attachment.gaodun.com/uploads/202312/202312151105330.xls",
    ],
    2025: [
        "https://u3.huatu.com/uploads/soft/241014/ah/2025gkzw.xlsx",
        "https://imgbdb4.bendibao.com/szbdb/edu/202410/14/20241014172143_38283.zip",
    ],
    2026: [
        "https://imgbdb4.bendibao.com/excel/2026gwyks.xls",
        "https://u3.huatu.com/uploads/soft/251014/2026gkzw.xlsx",
    ],
}
UA = {"User-Agent": "Mozilla/5.0 (compatible; DouyaJobDB/2.0; public-data-archive)"}

ALIASES: dict[str, list[str]] = {
    "department_code": ["部门代码"],
    "department_name": ["部门名称", "招录机关"],
    "employing_department": ["用人司局", "用人单位"],
    "agency_nature": ["机构性质", "单位性质"],
    "position_name": ["招考职位", "招录职位", "职位名称", "岗位名称"],
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
HEADER_MARKERS = ["部门代码", "职位代码", "招考人数", "招录人数", "招考职位", "招录职位", "专业", "学历"]


def utc_now() -> str:
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


def digits(value: str | None) -> str | None:
    if not value:
        return None
    result = re.sub(r"\D", "", value)
    return result or None


def integer(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value.replace(",", ""))
    return int(match.group()) if match else None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def valid_office(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 1000:
        return False
    signature = path.read_bytes()[:8]
    return signature.startswith(b"PK") or signature.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))


def acquire(year: int) -> tuple[list[Path], list[dict[str, Any]]]:
    year_dir = RAW / str(year)
    year_dir.mkdir(parents=True, exist_ok=True)
    logs: list[dict[str, Any]] = []
    for index, url in enumerate(SOURCES[year], 1):
        extension = Path(urlparse(url).path).suffix.lower() or ".bin"
        target = year_dir / f"source_{index}{extension}"
        record: dict[str, Any] = {"year": year, "url": url, "downloaded_at": utc_now()}
        try:
            response = requests.get(url, headers=UA, timeout=(30, 180), allow_redirects=True)
            record.update(status_code=response.status_code, final_url=response.url, content_type=response.headers.get("content-type"))
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            target.write_bytes(response.content)
            record.update(bytes=target.stat().st_size, sha256=sha256_file(target))
            files: list[Path] = []
            if zipfile.is_zipfile(target):
                extract_to = year_dir / f"unzipped_{index}"
                extract_to.mkdir(exist_ok=True)
                with zipfile.ZipFile(target) as archive:
                    archive.extractall(extract_to)
                files = [p for p in extract_to.rglob("*") if p.suffix.lower() in {".xls", ".xlsx", ".xlsm"}]
            elif valid_office(target):
                files = [target]
            logs.append(record)
            if files:
                return files, logs
        except Exception as exc:
            record["error"] = repr(exc)
            logs.append(record)
    return [], logs


def unique_headers(values: Iterable[Any]) -> list[str]:
    counts: dict[str, int] = defaultdict(int)
    output: list[str] = []
    for index, value in enumerate(values):
        base = re.sub(r"\s+", "", clean(value) or f"unnamed_{index + 1}")
        counts[base] += 1
        output.append(base if counts[base] == 1 else f"{base}_{counts[base]}")
    return output


def detect_header(frame: pd.DataFrame) -> int | None:
    best_index: int | None = None
    best_score = -1
    for index in range(min(40, len(frame))):
        text = "|".join(clean(v) or "" for v in frame.iloc[index].tolist())
        score = sum(marker in text for marker in HEADER_MARKERS)
        if "部门代码" in text:
            score += 3
        if "职位代码" in text:
            score += 3
        if "招考人数" in text or "招录人数" in text:
            score += 2
        if score > best_score:
            best_index, best_score = index, score
    return best_index if best_index is not None and best_score >= 6 else None


def iter_tables(path: Path):
    workbook = pd.ExcelFile(path)
    for sheet_name in workbook.sheet_names:
        try:
            raw = pd.read_excel(path, sheet_name=sheet_name, header=None, dtype=object)
        except Exception as exc:
            print("SHEET_READ_ERROR", path, sheet_name, repr(exc), flush=True)
            continue
        header_index = detect_header(raw)
        if header_index is None:
            print("NO_HEADER", path, sheet_name, raw.shape, flush=True)
            continue
        table = raw.iloc[header_index + 1 :].copy()
        table.columns = unique_headers(raw.iloc[header_index].tolist())
        table = table.dropna(how="all")
        yield str(sheet_name), table, header_index + 2


def pick(row: dict[str, Any], aliases: list[str]) -> str | None:
    for alias in aliases:
        if alias in row:
            value = clean(row[alias])
            if value is not None:
                return value
    for key, raw_value in row.items():
        normalized = re.sub(r"\s+", "", str(key))
        for alias in aliases:
            if normalized == alias or normalized.startswith(alias + "_"):
                value = clean(raw_value)
                if value is not None:
                    return value
    return None


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


def create_database(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE sources(
          source_id TEXT PRIMARY KEY, year INTEGER, source_url TEXT, final_url TEXT,
          local_path TEXT, sha256 TEXT, bytes INTEGER, source_level TEXT,
          downloaded_at TEXT, notes TEXT
        );
        CREATE TABLE raw_positions(
          raw_position_id INTEGER PRIMARY KEY AUTOINCREMENT,
          year INTEGER NOT NULL, exam_type TEXT NOT NULL, source_id TEXT,
          source_file TEXT, source_sheet TEXT, source_row_no INTEGER,
          source_record_json TEXT NOT NULL, source_record_hash TEXT NOT NULL,
          UNIQUE(year, source_record_hash)
        );
        CREATE TABLE positions(
          position_id TEXT PRIMARY KEY, raw_position_id INTEGER, year INTEGER NOT NULL,
          exam_type TEXT NOT NULL, department_code TEXT, department_name TEXT,
          employing_department TEXT, agency_nature TEXT, position_name TEXT,
          position_attribute TEXT, position_distribution TEXT, position_intro TEXT,
          position_code TEXT, organization_level TEXT, exam_category TEXT,
          recruit_count INTEGER, work_location TEXT, settlement_location TEXT,
          department_website TEXT, consult_phone_1 TEXT, consult_phone_2 TEXT,
          consult_phone_3 TEXT, remarks_raw TEXT, source_id TEXT
        );
        CREATE TABLE position_requirements(
          position_id TEXT PRIMARY KEY, major_raw TEXT, education_raw TEXT,
          education_min_rank INTEGER, degree_raw TEXT, degree_required INTEGER,
          political_status_raw TEXT, grassroots_years_raw TEXT,
          service_project_raw TEXT, professional_test_raw TEXT,
          interview_ratio_raw TEXT, rule_json TEXT NOT NULL
        );
        CREATE TABLE application_stats(
          stat_id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT,
          stat_date TEXT, registered_count INTEGER, approved_count INTEGER,
          paid_count INTEGER, confirmed_count INTEGER, competition_ratio REAL,
          stat_scope TEXT, is_final INTEGER, source_url TEXT, source_note TEXT
        );
        CREATE TABLE entry_scores(
          score_id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT,
          score_type TEXT, min_written_score REAL, min_entry_score REAL,
          max_entry_score REAL, min_final_score REAL, max_final_score REAL,
          source_url TEXT, source_note TEXT
        );
        CREATE TABLE coverage(
          year INTEGER PRIMARY KEY, expected_positions INTEGER,
          loaded_positions INTEGER, distinct_composite_keys INTEGER,
          duplicate_rows_skipped INTEGER, status TEXT, notes TEXT
        );
        CREATE INDEX idx_positions_filter
          ON positions(year, work_location, organization_level, exam_category);
        CREATE INDEX idx_positions_composite
          ON positions(year, department_code, position_code);
        CREATE INDEX idx_requirements
          ON position_requirements(education_min_rank, political_status_raw, major_raw);
        """
    )
    return connection


def ingest(connection: sqlite3.Connection, year: int, files: list[Path], logs: list[dict[str, Any]]) -> dict[str, Any]:
    for record in logs:
        if not record.get("sha256"):
            continue
        source_id = hashlib.sha1(f"{year}|{record['sha256']}".encode()).hexdigest()
        connection.execute(
            "INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source_id, year, record.get("url"), record.get("final_url"), None,
             record.get("sha256"), record.get("bytes"), "B-public mirror of official attachment",
             record.get("downloaded_at"), None),
        )

    seen: set[str] = set()
    inserted = 0
    duplicate_rows = 0
    invalid_rows = 0
    sheet_counts: list[dict[str, Any]] = []

    for path in files:
        if not valid_office(path):
            continue
        file_hash = sha256_file(path)
        source_id = hashlib.sha1(f"{year}|{file_hash}".encode()).hexdigest()
        connection.execute(
            "INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source_id, year, None, None, str(path), file_hash, path.stat().st_size,
             "B-public mirror of official attachment", utc_now(), "Parsed workbook"),
        )
        for sheet_name, frame, first_data_row in iter_tables(path):
            sheet_inserted = 0
            for offset, record in enumerate(frame.to_dict(orient="records")):
                raw = {str(key): clean(value) for key, value in record.items()}
                mapped = {field: pick(raw, aliases) for field, aliases in ALIASES.items()}
                department_code = digits(mapped.get("department_code"))
                position_code = digits(mapped.get("position_code"))
                position_name = mapped.get("position_name")
                recruit_count = integer(mapped.get("recruit_count"))
                if not department_code or not position_code or not position_name or recruit_count is None:
                    invalid_rows += 1
                    continue
                composite = f"{department_code}|{position_code}"
                if composite in seen:
                    duplicate_rows += 1
                    continue
                seen.add(composite)
                raw_json = json.dumps(raw, ensure_ascii=False, sort_keys=True)
                raw_hash = hashlib.sha256(f"{year}|{raw_json}".encode()).hexdigest()
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO raw_positions
                    (year,exam_type,source_id,source_file,source_sheet,source_row_no,source_record_json,source_record_hash)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (year, "国考", source_id, str(path), sheet_name,
                     first_data_row + offset, raw_json, raw_hash),
                )
                if cursor.rowcount == 0:
                    duplicate_rows += 1
                    continue
                position_id = f"GK-{year}-{department_code}-{position_code}"
                connection.execute(
                    "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (position_id, cursor.lastrowid, year, "国考", department_code,
                     mapped.get("department_name"), mapped.get("employing_department"),
                     mapped.get("agency_nature"), position_name,
                     mapped.get("position_attribute"), mapped.get("position_distribution"),
                     mapped.get("position_intro"), position_code,
                     mapped.get("organization_level"), mapped.get("exam_category"),
                     recruit_count, mapped.get("work_location"), mapped.get("settlement_location"),
                     mapped.get("department_website"), mapped.get("consult_phone_1"),
                     mapped.get("consult_phone_2"), mapped.get("consult_phone_3"),
                     mapped.get("remarks_raw"), source_id),
                )
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
                connection.execute(
                    "INSERT OR REPLACE INTO position_requirements VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (position_id, mapped.get("major_raw"), mapped.get("education_raw"),
                     rule["education_min_rank"], degree, int(rule["degree_required"]),
                     mapped.get("political_status_raw"), mapped.get("grassroots_years_raw"),
                     mapped.get("service_project_raw"), mapped.get("professional_test_raw"),
                     mapped.get("interview_ratio_raw"), json.dumps(rule, ensure_ascii=False)),
                )
                inserted += 1
                sheet_inserted += 1
            sheet_counts.append({"file": str(path), "sheet": sheet_name, "inserted": sheet_inserted})
    connection.commit()
    return {
        "inserted": inserted,
        "duplicate_rows_skipped": duplicate_rows,
        "invalid_rows_skipped": invalid_rows,
        "distinct_composite_keys": len(seen),
        "sheets": sheet_counts,
    }


def export_csv(connection: sqlite3.Connection, path: Path) -> int:
    query = """
    SELECT p.*,r.major_raw,r.education_raw,r.education_min_rank,r.degree_raw,
           r.degree_required,r.political_status_raw,r.grassroots_years_raw,
           r.service_project_raw,r.professional_test_raw,r.interview_ratio_raw,
           r.rule_json
    FROM positions p LEFT JOIN position_requirements r USING(position_id)
    ORDER BY p.year,p.department_code,p.position_code
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
    database_path = OUT / "national_core_v2_2024_2026.sqlite"
    connection = create_database(database_path)
    manifest: dict[str, Any] = {
        "generated_at": utc_now(),
        "scope": "2024-2026国考完整原始职位表核心库",
        "primary_key": "year + department_code + position_code",
        "years": {},
        "sources": [],
        "limitations": [
            "本核心库只声称职位表数据覆盖；报名人数和进面分在扩展流程补入。",
            "资格判定以raw_positions.source_record_json保存的原始字段为最终依据。",
            "未知数据保持NULL。",
        ],
    }
    for year in (2024, 2025, 2026):
        files, logs = acquire(year)
        manifest["sources"].extend(logs)
        report = ingest(connection, year, files, logs)
        expected = EXPECTED[year]
        loaded = connection.execute("SELECT COUNT(*) FROM positions WHERE year=?", (year,)).fetchone()[0]
        status = "complete" if loaded == expected else ("substantial_partial" if loaded >= expected * 0.95 else "partial")
        connection.execute(
            "INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?,?)",
            (year, expected, loaded, report["distinct_composite_keys"],
             report["duplicate_rows_skipped"], status,
             "年度岗位数与公开职位总数核验；联合键为部门代码+职位代码。"),
        )
        connection.commit()
        report.update(expected=expected, loaded=loaded, status=status)
        manifest["years"][str(year)] = report
        print("YEAR_REPORT", year, json.dumps(report, ensure_ascii=False), flush=True)
    manifest["csv_rows"] = export_csv(connection, OUT / "national_core_v2_2024_2026.csv.gz")
    manifest["totals"] = {
        "positions": connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0],
        "raw_positions": connection.execute("SELECT COUNT(*) FROM raw_positions").fetchone()[0],
        "requirements": connection.execute("SELECT COUNT(*) FROM position_requirements").fetchone()[0],
    }
    connection.commit()
    connection.close()
    manifest["database_bytes"] = database_path.stat().st_size
    manifest["database_sha256"] = sha256_file(database_path)
    (OUT / "national_core_v2_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "README.txt").write_text(
        "2024-2026国考真实职位表核心数据库V2。主键为年度+部门代码+职位代码。导入前检查manifest年度行数。\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
