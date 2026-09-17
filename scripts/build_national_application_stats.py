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
    'anhui', 'beijing', 'chongqing', 'fujian', 'gansu', 'guangdong', 'guangxi',
    'guizhou', 'hainan', 'hebei', 'heilongjiang', 'henan', 'hubei', 'hunan',
    'jiangsu', 'jiangxi', 'jilin', 'liaoning', 'neimenggu', 'ningxia', 'qinghai',
    'shandong', 'shanghai', 'shanxi', 'shanxisheng', 'sichuan', 'tianjin',
    'xicang', 'xinjiang', 'yunnan', 'zhejiang',
]
YEARS = (2024, 2025, 2026)
EXPECTED = {2024: 18948, 2025: 20810, 2026: 20714}
S = requests.Session()
S.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36',
    'Accept': '*/*',
})


def norm(value: object) -> str:
    return re.sub(r'[\s（）()【】\[\]、，,。.;；:：/\\_-]+', '', str(value or '')).lower()


def to_int(value: object) -> int | None:
    match = re.search(r'\d+', str(value or '').replace(',', ''))
    return int(match.group()) if match else None


def fetch(url: str) -> requests.Response | None:
    for attempt in range(5):
        try:
            response = S.get(url, timeout=(20, 180), allow_redirects=True)
            if response.status_code == 200 and len(response.content) > 200:
                return response
        except Exception:
            pass
        time.sleep(1.5 * (attempt + 1))
    return None


def insert_stat(
    con: sqlite3.Connection,
    *,
    year: int,
    slug: str,
    department_name: str | None,
    position_name: str | None,
    position_code: str | None,
    education_raw: str | None,
    major_raw: str | None,
    recruit_count: int | None,
    registered_count: int | None,
    detail_url: str | None,
    source_url: str,
    source_hash: str,
) -> bool:
    if not position_code or not department_name or not re.search(r'\d{6,}', position_code):
        return False
    con.execute(
        '''INSERT OR REPLACE INTO stats(
             year,region_slug,department_name,position_name,position_code,
             education_raw,major_raw,recruit_count,registered_count,
             detail_url,source_url,source_hash
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            year, slug, department_name.strip(), position_name.strip() if position_name else None,
            position_code.strip(), education_raw.strip() if education_raw else None,
            major_raw.strip() if major_raw else None, recruit_count, registered_count,
            detail_url, source_url, source_hash,
        ),
    )
    return True


def parse_html_table(
    con: sqlite3.Connection,
    *,
    year: int,
    slug: str,
    page_url: str,
    html: bytes,
) -> int:
    soup = BeautifulSoup(html, 'lxml')
    table = soup.find('table', id='all_list') or soup.find(
        'table', class_=lambda value: value and 'layui-table' in value
    )
    if not table:
        return 0
    heads = [cell.get_text(' ', strip=True) for cell in table.find_all('th')]
    hmap = {norm(name): index for index, name in enumerate(heads)}

    def idx(*names: str) -> int | None:
        for name in names:
            if norm(name) in hmap:
                return hmap[norm(name)]
        return None

    indexes = {
        'code': idx('岗位代码', '职位代码'),
        'dept': idx('部门名称', '招录机关', '招考部门'),
        'name': idx('招考职位', '职位名称'),
        'edu': idx('学历要求', '学历'),
        'major': idx('专业要求', '专业'),
        'recruit': idx('招考人数', '招录人数'),
        'reg': idx('报名人数'),
    }
    source_hash = hashlib.sha256(html).hexdigest()
    rows = 0
    for tr in table.select('tbody tr'):
        tds = tr.find_all('td')
        if not tds:
            continue

        def val(key: str) -> str | None:
            position = indexes[key]
            if position is None or position >= len(tds):
                return None
            text = tds[position].get_text(' ', strip=True)
            return text or None

        detail_url = None
        anchor = tr.find('a', href=True)
        if anchor:
            detail_url = urljoin(page_url, anchor['href'])
        if insert_stat(
            con,
            year=year,
            slug=slug,
            department_name=val('dept'),
            position_name=val('name'),
            position_code=val('code'),
            education_raw=val('edu'),
            major_raw=val('major'),
            recruit_count=to_int(val('recruit')),
            registered_count=to_int(val('reg')),
            detail_url=detail_url,
            source_url=page_url,
            source_hash=source_hash,
        ):
            rows += 1
    return rows


def parse_tabletxt_js(
    con: sqlite3.Connection,
    *,
    year: int,
    slug: str,
    js_url: str,
    js_bytes: bytes,
) -> int:
    text = js_bytes.decode('utf-8', errors='ignore')
    match = re.search(r'(?:let|var|const)\s+tabletxt\s*=\s*`(.*?)`\s*;', text, re.S)
    if not match:
        match = re.search(r'tabletxt\s*=\s*`(.*?)`', text, re.S)
    if not match:
        return 0
    payload = match.group(1).strip()
    chunks = payload.split('^')
    if len(chunks) < 2:
        return 0
    headers = [part.strip() for part in chunks[0].split('#')]
    hmap = {norm(name): index for index, name in enumerate(headers)}

    def idx(*names: str) -> int | None:
        for name in names:
            if norm(name) in hmap:
                return hmap[norm(name)]
        return None

    indexes = {
        'code': idx('岗位代码', '职位代码'),
        'dept': idx('部门名称', '招录机关', '招考部门'),
        'name': idx('招考职位', '职位名称'),
        'edu': idx('学历要求', '学历'),
        'major': idx('专业要求', '专业'),
        'recruit': idx('招考人数', '招录人数'),
        'reg': idx('报名人数'),
        'detail': idx('详细'),
    }
    source_hash = hashlib.sha256(js_bytes).hexdigest()
    rows = 0
    for record in chunks[1:]:
        values = [part.strip() for part in record.split('#')]

        def val(key: str) -> str | None:
            position = indexes[key]
            if position is None or position >= len(values):
                return None
            return values[position] or None

        detail_path = val('detail')
        detail_url = None
        if detail_path:
            detail_url = f'https://www.gwyzwb.com/{detail_path.lstrip("/")}.html'
        if insert_stat(
            con,
            year=year,
            slug=slug,
            department_name=val('dept'),
            position_name=val('name'),
            position_code=val('code'),
            education_raw=val('edu'),
            major_raw=val('major'),
            recruit_count=to_int(val('recruit')),
            registered_count=to_int(val('reg')),
            detail_url=detail_url,
            source_url=js_url,
            source_hash=source_hash,
        ):
            rows += 1
    return rows


if DB.exists():
    DB.unlink()
con = sqlite3.connect(DB)
con.executescript(
    '''
    CREATE TABLE stats(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      year INTEGER NOT NULL,
      region_slug TEXT NOT NULL,
      department_name TEXT NOT NULL,
      position_name TEXT,
      position_code TEXT NOT NULL,
      education_raw TEXT,
      major_raw TEXT,
      recruit_count INTEGER,
      registered_count INTEGER,
      detail_url TEXT,
      source_url TEXT NOT NULL,
      source_hash TEXT,
      UNIQUE(year,department_name,position_code)
    );
    CREATE INDEX idx_stats_match ON stats(year,department_name,position_code);
    CREATE TABLE coverage(
      year INTEGER,
      region_slug TEXT,
      source_url TEXT,
      status TEXT,
      rows INTEGER,
      notes TEXT,
      PRIMARY KEY(year,region_slug)
    );
    '''
)
manifest: dict[str, object] = {
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'expected_positions': EXPECTED,
    'pages': [],
}

for year in YEARS:
    for slug in SLUGS:
        page_url = f'https://www.gwyzwb.com/{slug}zw/{year}.html'
        response = fetch(page_url)
        rows = 0
        source_url = page_url
        method = 'html'
        notes: list[str] = []
        if response:
            rows = parse_html_table(
                con, year=year, slug=slug, page_url=page_url, html=response.content
            )
        else:
            notes.append('HTML page download failed')

        if rows == 0:
            js_url = f'https://ah.huatu.com/zw/{slug}zw/{year}.js'
            js_response = fetch(js_url)
            if js_response:
                rows = parse_tabletxt_js(
                    con, year=year, slug=slug, js_url=js_url, js_bytes=js_response.content
                )
                source_url = js_url
                method = 'tabletxt_js'
            else:
                notes.append('tabletxt JS download failed')

        status = 'ok' if rows else 'no_rows'
        con.execute(
            'INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?)',
            (year, slug, source_url, status, rows, '; '.join(notes) or None),
        )
        con.commit()
        manifest['pages'].append(
            {
                'year': year,
                'slug': slug,
                'source_url': source_url,
                'method': method,
                'status': status,
                'rows': rows,
            }
        )
        print(year, slug, method, status, rows, flush=True)
        time.sleep(0.1)

summary = []
for year in YEARS:
    row = con.execute(
        '''SELECT COUNT(*),
                  SUM(CASE WHEN registered_count IS NOT NULL THEN 1 ELSE 0 END),
                  SUM(recruit_count),
                  SUM(registered_count)
           FROM stats WHERE year=?''',
        (year,),
    ).fetchone()
    expected = EXPECTED[year]
    status = 'complete' if row[0] == expected else 'count_mismatch'
    summary.append(
        {
            'year': year,
            'positions': row[0],
            'expected_positions': expected,
            'status': status,
            'with_registration': row[1],
            'recruits': row[2],
            'registered_total': row[3],
        }
    )
manifest['summary'] = summary
manifest['total_rows'] = con.execute('SELECT COUNT(*) FROM stats').fetchone()[0]
manifest['quick_check'] = con.execute('PRAGMA quick_check').fetchone()[0]
con.commit()

with open(OUT / 'national_application_stats_2024_2026.csv', 'w', newline='', encoding='utf-8-sig') as file:
    cursor = con.execute(
        '''SELECT year,region_slug,department_name,position_name,position_code,
                  education_raw,major_raw,recruit_count,registered_count,
                  detail_url,source_url,source_hash
           FROM stats ORDER BY year,region_slug,position_code'''
    )
    writer = csv.writer(file)
    writer.writerow([description[0] for description in cursor.description])
    writer.writerows(cursor)
con.close()
(OUT / 'manifest.json').write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8'
)
print(json.dumps(summary, ensure_ascii=False), flush=True)
