"""Collect public catalog samples without modifying application databases."""
import json,sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from main import load_settings
from http_client import Client
from bs4 import BeautifulSoup
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'reports'/'new_sources';OUT.mkdir(exist_ok=True)
STARTS={'nexto':'https://www.nexto.pl/','publio':'https://www.publio.pl/audiobooki.html','bookbeat':'https://www.bookbeat.com/pl/audiobooks','woblink':'https://woblink.com/katalog/audiobooki','legimi':'https://www.legimi.pl/katalog/','wolnelektury':'https://wolnelektury.pl/api/audiobooks/?format=json'}
def probe(pair):
 site,url=pair;client=Client(load_settings(ROOT/'config.json'));item={'site':site,'requested':url}
 try:
  r=client.get(url);(OUT/f'{site}_catalog.html').write_text(r.text,encoding='utf-8');item.update(url=r.url,size=len(r.content))
  try:
   data=json.loads(r.text);item['json_type']=type(data).__name__;item['json_sample']=data[:2] if isinstance(data,list) else list(data)[:5]
  except ValueError:
   soup=BeautifulSoup(r.text,'lxml');item['title']=soup.title.get_text() if soup.title else None
   item['links']=[{'url':a.get('href'),'text':a.get_text(' ',strip=True)[:90]} for a in soup.select('a[href]') if any(t in a.get('href','').lower() for t in ['audiobook','ksiazka','/book/','strona','page=','_p','p='])][:45]
  if site!='wolnelektury':
   from urllib.parse import urlsplit
   root=urlsplit(r.url);robots=client.get(root.scheme+'://'+root.netloc+'/robots.txt');(OUT/f'{site}_robots.txt').write_text(robots.text,encoding='utf-8')
 except Exception as e:item['error']=str(e)
 finally:client.close()
 (OUT/f'{site}_probe.json').write_text(json.dumps(item,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(item,ensure_ascii=True),flush=True)
 return item
if __name__=='__main__':
 with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(probe,STARTS.items()))
 (OUT/'probe.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
