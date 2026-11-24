"""Scarica le immagini di Picsum (foto Unsplash, licenza Unsplash: uso libero) + crediti."""
import json, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
UA = {"User-Agent": "demo-fetch/1.0"}
def get(url, t=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=t).read()
items = []
for page in range(1, 12):
    batch = json.loads(get(f"https://picsum.photos/v2/list?page={page}&limit=100"))
    if not batch: break
    items += batch
print("lista", len(items), flush=True)
def dl(it):
    p = out / f"{it['id']}.jpg"
    if p.exists() and p.stat().st_size > 10000: return 1
    w, h = it["width"], it["height"]
    W = 1600; H = max(1, round(W * h / w))
    try:
        p.write_bytes(get(f"https://picsum.photos/id/{it['id']}/{W}/{H}.jpg")); return 1
    except Exception as e:
        print("err", it["id"], e, flush=True); return 0
with ThreadPoolExecutor(8) as ex:
    n = sum(ex.map(dl, items))
json.dump([{"id": i["id"], "author": i["author"], "url": i["url"]} for i in items], open(out / "credits.json", "w"), indent=0)
print("scaricate", n)
