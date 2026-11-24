"""Test API luoghi / persone / statistiche (fotoscan/server/routes_places.py) su un catalogo temporaneo.

Esecuzione: `python -m pytest tests/test_api_places.py` oppure `python tests/test_api_places.py` (senza pytest).
Non richiede httpx: il server viene avviato in un thread con uvicorn e interrogato con urllib.
"""
from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from fotoscan import db as dbm  # noqa: E402
from fotoscan.config import Config  # noqa: E402
from fotoscan.server import deps, routes_places  # noqa: E402


def _make_app(cfg: Config) -> FastAPI:
    deps.get_cfg = lambda: cfg  # il catalogo di test sostituisce quello reale; il DB reale non viene mai aperto
    app = FastAPI()
    app.include_router(routes_places.router)
    return app


class _Server:
    """Uvicorn in un thread su porta libera; `get(path)` -> (status, json)."""

    def __init__(self, app: FastAPI):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.02)

    def get(self, path: str):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


def _open_catalog(path: str) -> sqlite3.Connection:
    """Crea lo schema del catalogo. dbm.SCHEMA crea l'indice su plan prima della tabella: su un DB nuovo fallisce,
    quindi l'indice si crea a parte, dopo le tabelle."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(dbm.SCHEMA.replace("CREATE INDEX IF NOT EXISTS ix_plan_folder ON plan(folder);", ""))
    conn.execute("CREATE INDEX IF NOT EXISTS ix_plan_folder ON plan(folder)")
    return conn


def _catalog(tmp: Path, extra_sql: str = "") -> tuple[str, Config]:
    db_path = str(tmp / "catalogo.db")
    conn = _open_catalog(db_path)
    rows = [
        # id, name, kind, size, date, lat, lon, is_dup, context
        (1, "a.jpg", "image", 100, "2020-06-09T10:00:00", 41.94, 12.49, 0, "Mare e spiaggia"),
        (2, "b.jpg", "image", 200, "2020-06-09T11:00:00", 41.91, 12.46, 0, "Mare e spiaggia"),
        (3, "c.mp4", "video", 1000, "2021-01-01T08:00:00", 41.94, 12.49, 0, None),
        (4, "d.jpg", "image", 50, "2021-01-01T09:00:00", -33.87, 151.2, 1, "Tramonti"),  # duplicato esatto
        (5, "e.jpg", "image", 10, None, 0.0, 0.0, 0, None),  # punto nullo: escluso
        (6, "f.jpg", "image", 20, "2019-12-31T23:00:00", None, None, 0, None),  # senza GPS
        (7, "g.jpg", "image", 30, "bad", 41.92, 12.51, 0, "Tramonti"),  # data non valida
    ]
    for fid, name, kind, size, date, lat, lon, dup, ctx in rows:
        conn.execute(
            "INSERT INTO files(id,path,name,kind,size,date_taken,date_source,lat,lon,status) "
            "VALUES(?,?,?,?,?,?,'exif',?,?,'embedded')",
            (fid, f"D:/x/{name}", name, kind, size, date, lat, lon),
        )
        conn.execute("INSERT INTO plan(file_id,folder,context,is_dup) VALUES(?,?,?,?)",
                     (fid, "2020/06", ctx, dup))
    conn.executescript(extra_sql)
    conn.commit()
    conn.close()
    return db_path, Config(db_path=db_path, cache_dir=str(tmp / "cache"))


class _Tmp:
    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fs_places_"))
        return self.dir

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)


# ------------------------------------------------------------------ luoghi
def test_places_grid_and_label():
    with _Tmp() as tmp:
        _, cfg = _catalog(tmp)
        srv = _Server(_make_app(cfg))
        try:
            st, cells = srv.get("/api/places?precision=1")
            assert st == 200
            # 41.94/41.91/41.92 -> 41.9 ; 12.49/12.46/12.51 -> 12.5 ; (0,0) e null esclusi ; Sydney a parte
            by_label = {c["label"]: c for c in cells}
            rome = by_label["Lat 41,9° N · Lon 12,5° E"]
            assert rome["count"] == 4, rome  # file 1, 2, 3, 7
            assert rome["lat"] == 41.9 and rome["lon"] == 12.5
            assert rome["sample_id"] == 1  # MIN(id) del cluster
            sydney = by_label["Lat 33,9° S · Lon 151,2° E"]
            assert sydney["count"] == 1 and sydney["sample_id"] == 4
            assert sum(c["count"] for c in cells) == 5  # nessun file senza GPS o in (0,0)
            assert cells[0]["count"] >= cells[-1]["count"]  # ordinati per numero
        finally:
            srv.stop()


def test_places_precision_two_splits_cells():
    with _Tmp() as tmp:
        _, cfg = _catalog(tmp)
        srv = _Server(_make_app(cfg))
        try:
            st, cells = srv.get("/api/places?precision=2")
            assert st == 200
            counts = sorted(c["count"] for c in cells if c["lat"] > 0)
            # 41.94/12.49 (file 1 e 3), 41.91/12.46 (file 2), 41.92/12.51 (file 7): tre celle a 2 decimali
            assert counts == [1, 1, 2], cells
            assert any(c["label"] == "Lat 41,94° N · Lon 12,49° E" and c["count"] == 2 for c in cells)
        finally:
            srv.stop()


def test_places_precision_out_of_range_is_italian_error():
    with _Tmp() as tmp:
        _, cfg = _catalog(tmp)
        srv = _Server(_make_app(cfg))
        try:
            st, body = srv.get("/api/places?precision=9")
            assert st == 400 and "precisione" in body["detail"]
        finally:
            srv.stop()


def test_places_files_pages_the_cell():
    with _Tmp() as tmp:
        _, cfg = _catalog(tmp)
        srv = _Server(_make_app(cfg))
        try:
            st, body = srv.get("/api/places/files?lat=41.9&lon=12.5&precision=1&limit=2&offset=0")
            assert st == 200 and body["total"] == 4 and len(body["items"]) == 2
            assert {"id", "thumb", "lat", "lon", "is_dup"} <= body["items"][0].keys()
            st, page2 = srv.get("/api/places/files?lat=41.9&lon=12.5&precision=1&limit=2&offset=2")
            assert len(page2["items"]) == 2
            assert {i["id"] for i in body["items"]}.isdisjoint({i["id"] for i in page2["items"]})
            st, err = srv.get("/api/places/files?precision=1")
            assert st == 400 and err["detail"]
        finally:
            srv.stop()


# ------------------------------------------------------------------ statistiche
def test_stats_overview():
    with _Tmp() as tmp:
        _, cfg = _catalog(tmp)
        srv = _Server(_make_app(cfg))
        try:
            st, s = srv.get("/api/stats/overview")
            assert st == 200
            years = {y["year"]: y for y in s["per_year"]}
            assert years[2019] == {"year": 2019, "photos": 1, "videos": 0}  # file 6, senza GPS ma con data
            assert years[2020] == {"year": 2020, "photos": 2, "videos": 0}
            assert years[2021] == {"year": 2021, "photos": 1, "videos": 1}
            assert [y["year"] for y in s["per_year"]] == [2019, 2020, 2021]  # file 5 (senza data) e 7 ("bad") esclusi
            # contesto: is_dup=0 e contesto assegnato (file 1, 2 -> Mare; file 7 -> Tramonti; file 4 e' dup)
            assert s["per_context"] == [{"context": "Mare e spiaggia", "count": 2}, {"context": "Tramonti", "count": 1}]
            assert {"kind": "image", "count": 6} in s["per_kind"] and {"kind": "video", "count": 1} in s["per_kind"]
            assert {"source": "exif", "count": 7} in s["date_sources"]
            # 2020-06-09 e 2021-01-01 hanno 2 file ciascuno: a parita' vince il giorno piu' recente
            assert s["top_days"][:2] == [{"day": "2021-01-01", "count": 2}, {"day": "2020-06-09", "count": 2}]
            assert len(s["top_days"]) <= 10
            assert s["size_by_kind"] == {"image": 100 + 200 + 50 + 10 + 20 + 30, "video": 1000}
            assert s["size_total"] == sum(s["size_by_kind"].values())
        finally:
            srv.stop()


def test_stats_and_places_fast_on_14k_files():
    """Catalogo sintetico di 14.245 file: ogni risposta deve stare sotto 150 ms (misura a caldo)."""
    with _Tmp() as tmp:
        db_path = str(tmp / "big.db")
        conn = _open_catalog(db_path)
        rows = []
        for i in range(1, 14246):
            day = f"{2010 + i % 15}-{1 + i % 12:02d}-{1 + i % 28:02d}T12:00:00"
            lat, lon = (41.9 + (i % 50) * 0.001, 12.5) if i % 3 == 0 else (None, None)
            rows.append((i, f"D:/big/{i}.jpg", f"{i}.jpg", "video" if i % 7 == 0 else "image", 1000 + i,
                         day, "exif", lat, lon))
        conn.executemany("INSERT INTO files(id,path,name,kind,size,date_taken,date_source,lat,lon,status) "
                         "VALUES(?,?,?,?,?,?,?,?,?,'embedded')", rows)
        conn.executemany("INSERT INTO plan(file_id,folder,context,is_dup) VALUES(?,?,?,0)",
                         [(i, "f", f"Contesto {i % 5}") for i in range(1, 14246)])
        conn.commit()
        conn.close()
        cfg = Config(db_path=db_path, cache_dir=str(tmp / "cache"))
        srv = _Server(_make_app(cfg))
        try:
            for path in ("/api/stats/overview", "/api/places?precision=2", "/api/places/files?lat=41.9&lon=12.5"):
                srv.get(path)  # riscaldamento
                t0 = time.perf_counter()
                st, _ = srv.get(path)
                ms = (time.perf_counter() - t0) * 1000
                assert st == 200
                print(f"{path}: {ms:.1f} ms")
                assert ms < 150, f"{path} lento: {ms:.0f} ms"
        finally:
            srv.stop()


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} test superati")
