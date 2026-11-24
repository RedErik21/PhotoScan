"""API duplicati (routes_duplicates) su un piccolo catalogo sintetico in una cartella temporanea.

Usa uvicorn in un thread e richieste HTTP reali (urllib): nessuna dipendenza extra (httpx non c'e' nel venv).
Esecuzione: `.\\.venv\\Scripts\\python.exe tests\\test_api_duplicates.py` oppure con pytest.
Non tocca catalogo.db ne' cartelle reali: il DB e' in una cartella temporanea e la config e' sostituita.
"""
from __future__ import annotations

import contextlib
import json
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fotoscan import db as dbm  # noqa: E402
from fotoscan.config import Config  # noqa: E402
from fotoscan.server import deps, routes_duplicates  # noqa: E402

E0 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
E1 = np.array([0.0, 1.0, 0.0], dtype=np.float32)
E_NEAR = np.array([0.6, 0.8, 0.0], dtype=np.float32)  # coseno 0.6 con E0: quasi uguale solo per immagine


def _seed(conn) -> None:
    """Gruppo: 1 master, 2 copia esatta (hash), 3 'modificato' (nome), 5 quasi uguale (immagine).
    Foto 4 e 6 sono normali (fuori gruppo)."""
    base = "D:/foto"
    files = [
        (1, "IMG_1.jpg", 1000, "h1"),
        (2, "IMG_1 copia.jpg", 900, "h1"),
        (3, "IMG_1-modificato.jpg", 800, None),
        (4, "IMG_4.jpg", 700, "h4"),
        (5, "IMG_5.jpg", 600, None),
        (6, "IMG_6.jpg", 500, "h6"),
    ]
    for fid, name, size, h in files:
        conn.execute(
            "INSERT INTO files(id,path,name,kind,size,hash,date_taken,date_source,status,width,height) "
            "VALUES(?,?,?,?,?,?,?,?, 'embedded', 100, 100)",
            (fid, f"{base}/{name}", name, "image", size, h, "2020-06-09T10:00:00", "exif"),
        )
    for fid, vec in [(1, E0), (2, E0), (3, E0), (4, E1), (5, E_NEAR), (6, E1)]:
        conn.execute("INSERT INTO embeddings(file_id, vec) VALUES(?,?)", (fid, dbm.to_blob(vec)))
    for fid, folder, is_dup, dup_of in PLAN_BASE:
        conn.execute("INSERT INTO plan(file_id,folder,context,score,is_dup,dup_of) VALUES(?,?,?,?,?,?)",
                     (fid, folder, None, 0.5, is_dup, dup_of))
    conn.commit()


PLAN_BASE = [
    (1, "2020/06 - A", 0, None),
    (2, "_Duplicati", 1, 1),
    (3, "_Duplicati/Simili", 2, 1),
    (4, "2020/06 - A", 0, None),
    (5, "_Duplicati/Simili", 2, 1),
    (6, "2020/07 - B", 0, None),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Api:
    def __init__(self, base: str, db_path: str):
        self.base, self.db_path = base, db_path

    def req(self, method: str, path: str, body=None, **params):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def get(self, path, **params):
        return self.req("GET", path, **params)

    def post(self, path, body):
        return self.req("POST", path, body)

    def plan_row(self, fid: int) -> dict:
        conn = dbm.connect(self.db_path)
        try:
            return dict(conn.execute("SELECT folder, is_dup, dup_of FROM plan WHERE file_id=?", (fid,)).fetchone())
        finally:
            conn.close()

    def db(self):
        return dbm.connect(self.db_path)


@contextlib.contextmanager
def server(seed=_seed):
    tmp = Path(tempfile.mkdtemp(prefix="fs_dup_"))
    db_path = str(tmp / "catalogo.db")
    conn = dbm.connect(db_path)
    if seed:
        seed(conn)
    conn.close()
    cfg = Config(db_path=db_path, dup_mode="subfolder")
    orig_get_cfg = deps.get_cfg
    deps.get_cfg = lambda: cfg  # noqa: E731
    app = FastAPI()
    app.include_router(routes_duplicates.router)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.02)
    try:
        yield Api(f"http://127.0.0.1:{port}", db_path)
    finally:
        srv.should_exit = True
        t.join(timeout=5)
        deps.get_cfg = orig_get_cfg


# ------------------------------------------------------------------ test
def test_list_exact_group():
    with server() as api:
        st, body = api.get("/api/duplicates", type="exact")
        assert st == 200
        assert body["total"] == 1
        g = body["groups"][0]
        assert g["master"]["id"] == 1 and g["master"]["is_dup"] == 0 and g["master"]["dup_of"] is None
        ids = {m["id"]: m for m in g["members"]}
        assert set(ids) == {2, 3, 5}
        assert ids[2]["is_dup"] == 1 and ids[2]["dup_of"] == 1
        assert ids[3]["is_dup"] == 2
        # il gruppo ha copie esatte (reason hash); la similarita' misura solo i quasi-uguali: foto 3 (1.0) e 5 (0.6)
        assert g["reason"] == "hash" and g["similarity"] == 0.6


def test_pagination_and_bad_type():
    with server() as api:
        st, body = api.get("/api/duplicates", type="similar", limit=1, offset=0)
        assert st == 200 and body["total"] == 1 and len(body["groups"]) == 1
        st, body = api.get("/api/duplicates", type="similar", limit=1, offset=1)
        assert body["groups"] == []
        st, body = api.get("/api/duplicates", type="boh")
        assert st == 400 and "detail" in body


def test_reason_name_and_image_for_similar_only_group():
    with server() as api:
        c = api.db()
        c.execute("UPDATE plan SET is_dup=0, dup_of=NULL WHERE file_id=2")  # niente piu' copia esatta
        c.commit()
        c.close()
        st, body = api.get("/api/duplicates", type="similar")
        g = body["groups"][0]
        assert {m["id"]: m["is_dup"] for m in g["members"]} == {3: 2, 5: 2}
        assert g["reason"] == "immagine"  # 5 non ha il nome '...-modificato'
        assert 0.5 < g["similarity"] < 1.0  # minimo dei coseni: 0.6 con la foto 5


def test_summary():
    with server() as api:
        st, s = api.get("/api/duplicates/summary")
        assert st == 200
        assert s["exact"] == {"groups": 1, "files": 1, "bytes": 900}
        assert s["similar"] == {"groups": 1, "files": 2, "bytes": 800 + 600}


def test_keep_swaps_master_and_stays_coherent():
    with server() as api:
        st, body = api.post("/api/duplicates/keep", {"master_id": 2, "member_ids": [1, 3, 5]})
        assert st == 200, body
        assert body["changed"] is True
        st, body = api.get("/api/duplicates", type="exact")
        g = body["groups"][0]
        assert g["master"]["id"] == 2 and g["master"]["is_dup"] == 0
        ids = {m["id"]: m for m in g["members"]}
        assert set(ids) == {1, 3, 5}
        assert ids[1]["is_dup"] == 1 and ids[1]["dup_of"] == 2
        assert ids[3]["dup_of"] == 2 and ids[5]["dup_of"] == 2
        # plan coerente: galleria (dup=hide) e apply (cartelle)
        assert api.plan_row(2) == {"folder": "2020/06 - A", "is_dup": 0, "dup_of": None}
        assert api.plan_row(1)["folder"] == "_Duplicati" and api.plan_row(1)["dup_of"] == 2
        st, s = api.get("/api/duplicates/summary")
        assert s["exact"] == {"groups": 1, "files": 1, "bytes": 1000}


def test_keep_survives_plan_rebuild():
    """Simula build_plan: ricalcola i ruoli e le cartelle dal motore (file_override incluso)."""
    with server() as api:
        api.post("/api/duplicates/keep", {"master_id": 2, "member_ids": [1, 3, 5]})
        c = api.db()
        overrides = {r["file_id"]: r["folder"] for r in c.execute("SELECT file_id, folder FROM file_override")}
        c.execute("DELETE FROM plan")
        for fid, folder, d, of in PLAN_BASE:
            c.execute("INSERT INTO plan(file_id,folder,context,score,is_dup,dup_of) VALUES(?,?,?,?,?,?)",
                      (fid, overrides.get(fid, folder), None, 0.5, d, of))
        c.commit()
        c.close()
        st, body = api.get("/api/duplicates", type="exact")
        assert body["groups"][0]["master"]["id"] == 2
        assert api.plan_row(1)["is_dup"] == 1 and api.plan_row(1)["dup_of"] == 2
        assert api.plan_row(2)["folder"] == "2020/06 - A"


def test_keep_rejects_stale_or_foreign_input():
    with server() as api:
        st, _ = api.post("/api/duplicates/keep", {"master_id": 2, "member_ids": [1]})
        assert st == 409  # elenco membri incompleto
        st, _ = api.post("/api/duplicates/keep", {"master_id": 4, "member_ids": [1]})
        assert st == 404  # foto fuori gruppo
        st, body = api.post("/api/duplicates/keep", {"master_id": 1, "member_ids": [2, 3, 5]})
        assert st == 200 and body["changed"] is False


def test_empty_catalog():
    with server(seed=None) as api:
        assert api.get("/api/duplicates") == (200, {"total": 0, "groups": []})
        st, s = api.get("/api/duplicates/summary")
        assert s["exact"]["groups"] == 0 and s["similar"]["groups"] == 0


def test_similarity_is_per_member():
    with server() as api:
        st, body = api.get("/api/duplicates", type="exact")
        ids = {m["id"]: m for m in body["groups"][0]["members"]}
        assert ids[2]["similarity"] == 1.0  # copia esatta
        assert ids[3]["similarity"] == 1.0  # quasi uguale con vettore identico
        assert ids[5]["similarity"] == 0.6  # quasi uguale con vettore a 0.6: non il minimo del gruppo, il suo


def test_reviewed_flag_roundtrip():
    with server() as api:
        st, s = api.get("/api/duplicates/summary")
        assert st == 200 and s["reviewed"] is False
        st, body = api.post("/api/duplicates/reviewed", {"value": True})
        assert st == 200 and body == {"reviewed": True}
        st, s = api.get("/api/duplicates/summary")
        assert s["reviewed"] is True
        assert s["exact"]["groups"] == 1  # il controllo non cambia i gruppi
        api.post("/api/duplicates/reviewed", {"value": False})
        st, s = api.get("/api/duplicates/summary")
        assert s["reviewed"] is False


def test_keep_largest_all_dry_run_and_apply():
    with server() as api:
        c = api.db()
        c.execute("UPDATE files SET size=2000 WHERE id=2")  # la copia esatta diventa la piu' grande
        c.commit()
        c.close()
        st, body = api.post("/api/duplicates/keep-largest", {"dry_run": True})
        assert st == 200 and body == {"groups": 1, "changed": 1, "dry_run": True}
        assert api.plan_row(1)["is_dup"] == 0  # il dry run non tocca nulla
        st, body = api.post("/api/duplicates/keep-largest", {"dry_run": False})
        assert st == 200 and body["changed"] == 1
        st, body = api.get("/api/duplicates", type="exact")
        g = body["groups"][0]
        assert g["master"]["id"] == 2
        assert {m["id"] for m in g["members"]} == {1, 3, 5}
        assert api.plan_row(2)["folder"] == "2020/06 - A"
        # una seconda esecuzione non cambia piu' niente
        st, body = api.post("/api/duplicates/keep-largest", {"dry_run": False})
        assert body["changed"] == 0


def test_keep_many_allows_keeping_multiple_photos():
    with server() as api:
        # Conserva anche la copia 2 (identica) e 3 (simile): diventano foto normali (is_dup=0)
        st, body = api.post("/api/duplicates/keep-many", {"file_ids": [2, 3]})
        assert st == 200 and body == {"kept": 2, "groups_updated": 1}
        assert api.plan_row(2)["is_dup"] == 0 and api.plan_row(2)["dup_of"] is None
        assert api.plan_row(3)["is_dup"] == 0 and api.plan_row(3)["dup_of"] is None
        assert api.plan_row(5)["is_dup"] == 2 and api.plan_row(5)["dup_of"] == 1
        # Il tab exact ora e' vuoto, mentre similar ha ancora il gruppo con 5
        st, ex = api.get("/api/duplicates", type="exact")
        assert st == 200 and ex["total"] == 0
        st, sim = api.get("/api/duplicates", type="similar")
        assert st == 200 and sim["total"] == 1


def test_trash_all_copies_dry_run_and_apply():
    from unittest import mock
    from fotoscan.server import routes_cleanup

    with server() as api:
        st, dry = api.post("/api/duplicates/trash-copies", {"type": "exact", "dry_run": True})
        assert st == 200 and dry == {"groups": 1, "files": 1, "bytes": 900, "dry_run": True}
        # senza confirm=True viene rifiutato
        st, _ = api.post("/api/duplicates/trash-copies", {"type": "exact", "dry_run": False, "confirm": False})
        assert st == 400
        with mock.patch.object(routes_cleanup, "_send_to_recycle_bin"):
            st, res = api.post("/api/duplicates/trash-copies", {"type": "exact", "dry_run": False, "confirm": True})
            assert st == 200 and res["trashed"] == 1 and res["freed_bytes"] == 900
        st, ex = api.get("/api/duplicates", type="exact")
        assert st == 200 and ex["total"] == 0


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, type(e).__name__, e)
    sys.exit(1 if fails else 0)
