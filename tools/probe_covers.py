"""Verify advertised image variants and original dimensions without resizing."""
import sys,json,io
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from main import load_settings
from http_client import Client
from PIL import Image
root=Path(__file__).resolve().parents[1];out=root/'reports/new_sources';client=Client(load_settings(root/'config.json'));rows=[]
try:
 for name,url in json.loads((out/'cover_jobs.json').read_text()).items():
  row={'name':name,'url':url}
  try:
   r=client.get(url)
   with Image.open(io.BytesIO(r.content)) as image:row['dimensions']=image.size;row['format']=image.format
  except Exception as e:row['error']=str(e)
  rows.append(row);print(json.dumps(row),flush=True)
finally:
 client.close();(out/'cover_probes.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
