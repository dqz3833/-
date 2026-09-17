from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

OUT = Path('build/fenbi_endpoint_probe')
OUT.mkdir(parents=True, exist_ok=True)
S = requests.Session()
S.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36',
    'Accept': '*/*',
})
PAGES = [
    'https://www.fenbi.com/page/positions?examType=4',
    'https://www.fenbi.com/page/positions-exams?examType=4',
    'https://www.fenbi.com/page/positions-exams',
]
report = {'pages': [], 'scripts': [], 'api_strings': [], 'context_hits': []}
script_urls = []
for page in PAGES:
    try:
        r = S.get(page, timeout=(30,120), allow_redirects=True)
        rec = {'url': page, 'status': r.status_code, 'final_url': r.url, 'bytes': len(r.content)}
        soup = BeautifulSoup(r.text, 'html.parser')
        rec['scripts'] = [urljoin(r.url, x.get('src')) for x in soup.find_all('script') if x.get('src')]
        script_urls.extend(rec['scripts'])
        (OUT / ('page_' + str(len(report['pages'])) + '.html')).write_text(r.text, encoding='utf-8')
        report['pages'].append(rec)
    except Exception as e:
        report['pages'].append({'url': page, 'error': repr(e)})

script_urls = list(dict.fromkeys(script_urls))
patterns = [
    re.compile(r'["\']([^"\']*?/toolkit/api/v1/pc/[^"\']+?)["\']'),
    re.compile(r'["\']([^"\']*?/api/v1/pc/[^"\']+?)["\']'),
    re.compile(r'["\']([^"\']*?exam[^"\']{0,100})["\']', re.I),
]
keywords = ['catalogByCondition','homeCatalogs','basicInfo','examId','examType','positions-exams','position/list','exam/list','exam/search','examList']
for i, url in enumerate(script_urls):
    try:
        r = S.get(url, timeout=(30,180))
        rec = {'url': url, 'status': r.status_code, 'bytes': len(r.content), 'content_type': r.headers.get('content-type')}
        report['scripts'].append(rec)
        if r.status_code != 200 or len(r.content) < 100:
            continue
        text = r.text
        (OUT / f'script_{i:03d}.js').write_text(text, encoding='utf-8', errors='ignore')
        for pat in patterns[:2]:
            for m in pat.finditer(text):
                report['api_strings'].append({'script': url, 'value': m.group(1)})
        for kw in keywords:
            start = 0
            count = 0
            while count < 50:
                pos = text.find(kw, start)
                if pos < 0: break
                report['context_hits'].append({'script': url, 'keyword': kw, 'context': text[max(0,pos-500):pos+800]})
                start = pos + len(kw)
                count += 1
    except Exception as e:
        report['scripts'].append({'url': url, 'error': repr(e)})

# dedupe
seen=set(); apis=[]
for x in report['api_strings']:
    k=x['value']
    if k not in seen:
        seen.add(k); apis.append(x)
report['api_strings']=apis
(OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'page_count':len(report['pages']),'script_count':len(report['scripts']),'api_count':len(apis),'context_hits':len(report['context_hits'])},ensure_ascii=False))
for x in apis[:300]: print('API',x['value'])
