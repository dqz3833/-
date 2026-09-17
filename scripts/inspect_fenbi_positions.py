from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (compatible; DouyaJobDB/2.0; public-data-research)"}
OUT = Path("build/fenbi_inspect")
OUT.mkdir(parents=True, exist_ok=True)

urls = [
    "https://www.fenbi.com/page/positions-exams",
    "https://www.fenbi.com/page/positions-exams/0/425648",
    "https://www.fenbi.com/page/positions/0/425648",
    "https://www.fenbi.com/page/positions/4/469159",
]

session = requests.Session()
session.headers.update(UA)
report = {"pages": [], "scripts": [], "candidates": []}
script_urls: set[str] = set()

for url in urls:
    try:
        response = session.get(url, timeout=(30, 120), allow_redirects=True)
        text = response.text
        page_file = OUT / (re.sub(r"[^A-Za-z0-9]+", "_", url).strip("_") + ".html")
        page_file.write_text(text, encoding="utf-8")
        soup = BeautifulSoup(text, "lxml")
        scripts = [urljoin(response.url, item.get("src")) for item in soup.find_all("script", src=True)]
        script_urls.update(scripts)
        report["pages"].append({
            "url": url,
            "status": response.status_code,
            "final_url": response.url,
            "bytes": len(response.content),
            "title": soup.title.get_text(strip=True) if soup.title else None,
            "scripts": scripts,
            "api_like_strings": sorted(set(re.findall(r"(?:https?://[^\"'<> ]+|/[A-Za-z0-9_./?=&%-]*(?:position|exam|search|list)[A-Za-z0-9_./?=&%-]*)", text, flags=re.I)))[:300],
        })
    except Exception as exc:
        report["pages"].append({"url": url, "error": repr(exc)})

patterns = [
    re.compile(r"https?://[^\"'`<> ]+", re.I),
    re.compile(r"/[A-Za-z0-9_./?=&${}:%-]*(?:position|positions|exam|exams|search|list|job)[A-Za-z0-9_./?=&${}:%-]*", re.I),
]
for index, url in enumerate(sorted(script_urls), 1):
    try:
        response = session.get(url, timeout=(30, 120), allow_redirects=True)
        text = response.text
        values: set[str] = set()
        for pattern in patterns:
            values.update(match.group(0) for match in pattern.finditer(text))
        selected = sorted(value for value in values if any(token in value.lower() for token in ["position", "exam", "job", "search", "list"]))
        report["scripts"].append({"url": url, "status": response.status_code, "bytes": len(response.content), "candidates": selected[:1000]})
        for value in selected:
            report["candidates"].append({"script": url, "value": value})
        (OUT / f"script_{index}.js").write_text(text, encoding="utf-8")
    except Exception as exc:
        report["scripts"].append({"url": url, "error": repr(exc)})

(OUT / "fenbi_endpoint_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({
    "page_count": len(report["pages"]),
    "script_count": len(report["scripts"]),
    "candidate_count": len(report["candidates"]),
    "pages": report["pages"],
}, ensure_ascii=False, indent=2))
