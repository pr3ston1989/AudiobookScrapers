"""Fetch selected public pages into an isolated research directory."""
import json,sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from main import load_settings
from http_client import Client
from bs4 import BeautifulSoup
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'reports'/'new_sources'
def fetch(pair):
 name,url=pair;c=Client(load_settings(ROOT/'config.json'));r={'name':name,'url':url}
 try:
  x=c.get(url);(OUT/f'{name}.html').write_text(x.text,encoding='utf-8');r.update(final=x.url,size=len(x.content))
  try:
   d=json.loads(x.text);r['fields']=list(d)[:25]
  except ValueError:
   s=BeautifulSoup(x.text,'lxml');r['title']=s.title.get_text() if s.title else None;r['text']=s.get_text(' ',strip=True)[-1600:] if name=='nexto_notice' else ''
 except Exception as e:r['error']=str(e)
 finally:c.close()
 print(json.dumps(r,ensure_ascii=True),flush=True);return r
with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(fetch,json.loads((OUT/'jobs.json').read_text()).items()))
(OUT/'samples.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
