import json,re,requests,pandas as pd
from pathlib import Path
urls=['https://u3.huatu.com/uploads/soft/251014/2026gkzw.xlsx','https://imgbdb4.bendibao.com/excel/2026gwyks.xls']
for n,u in enumerate(urls,1):
    print('\nURL',u,flush=True)
    try:
        r=requests.get(u,headers={'User-Agent':'Mozilla/5.0'},timeout=(30,180),allow_redirects=True)
        print('HTTP',r.status_code,'bytes',len(r.content),'type',r.headers.get('content-type'),'final',r.url,flush=True)
        ext='.xls' if u.endswith('.xls') else '.xlsx';p=Path(f'/tmp/inspect{n}{ext}');p.write_bytes(r.content)
        x=pd.ExcelFile(p);print('SHEETS',x.sheet_names,flush=True)
        for s in x.sheet_names:
            df=pd.read_excel(p,sheet_name=s,header=None,dtype=object)
            print('SHEET',repr(s),'SHAPE',df.shape,flush=True)
            for i in range(min(20,len(df))):
                vals=['' if pd.isna(v) else str(v) for v in df.iloc[i].tolist()]
                txt=' | '.join(vals)
                if txt.strip(' |'):
                    print('ROW',i,txt[:5000],flush=True)
    except Exception as e:
        print('ERROR',repr(e),flush=True)
