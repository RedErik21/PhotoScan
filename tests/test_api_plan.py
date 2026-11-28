"""Test API /api/plan su un catalogo SQLite isolato (dati sintetici, nessun modello caricato).

Nessuna dipendenza extra: usa un server uvicorn reale su porta libera e urllib.
Esecuzione: `.\\.venv\\Scripts\\python.exe tests\\test_api_plan.py` (oppure pytest, se disponibile).
"""
from __future__ import annotations

import contextlib
import json
import socket
import sqlite3
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
from fotoscan import embedder as embedder_mod  # noqa: E402
from fotoscan.config import Config  # noqa: E402
from fotoscan.server import deps, jobs, routes_duplicates, routes_plan  # noqa: E402

# (id, nome, tipo, dimensione, data)
FILES = [
    (1, "IMG1.jpg", "image", 1000, "2020-06-01T10:00:00"),
    (2, "IMG2.jpg", "image", 2000, "2020-06-02T10:00:00"),
    (3, "IMG3.jpg", "image", 3000, "2020-06-03T10:00:00"),
    (4, "VID4.mp4", "video", 4000, "2020-06-04T10:00:00"),
    (5, "IMG5.jpg", "image", 500, "2020-06-09T09:00:00"),
    (6, "IMG6.jpg", "image", 600, "2020-06-09T12:00:00"),
    (7, "VID7.mp4", "video", 700, "2020-07-01T10:00:00"),
    (8, "IMG8.jpg", "image", 800, "2019-03-03T10:00:00"),
    (9, "IMG9.jpg", "image", 900, "2020-01-01T10:00:00"),
    (10, "IMG10.jpg", "image", 1000, "2020-01-02T10:00:00"),
    (11, "IMG11.jpg", "image", 1100, None),
]
PLAN = {
    1: "2020/06 - Mare", 2: "2020/06 - Mare", 3: "2020/06 - Mare", 4: "2020/06 - Mare",
    5: "2020/06 - Evento 2020-06-09", 6: "2020/06 - Evento 2020-06-09",
    7: "2020/07 - Varie", 8: "Data incerta/2019 - Varie",
    9: "_Duplicati", 10: "_Duplicati/Simili", 11: None,
}
LABELS = [{"name": "Mare", "prompt": "mare"}, {"name": "Tramonti", "prompt": "tramonto"}]


# ------------------------------------------------------------ client HTTP minimale
class Resp:
    def __init__(self, status: int, body: bytes):
        self.status_code = status
        self._body = body

    def json(self):
        return json.loads(self._body.decode("utf-8"))


class Client:
    def __init__(self, base: str):
        self.base = base

    def _req(self, method: str, path: str, params=None, json_body=None) -> Resp:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return Resp(r.status, r.read())
        except urllib.error.HTTPError as e:
            return Resp(e.code, e.read())

    def get(self, path, params=None):
        return self._req("GET", path, params=params)

    def post(self, path, json=None):  # noqa: A002 - stesso nome di httpx/TestClient
        return self._req("POST", path, json_body=json if json is not None else {})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


@contextlib.contextmanager
def fresh_catalog(files=FILES, plan=PLAN, cfg_extra=None):
    """Catalogo isolato in una cartella temporanea + server API su porta libera. Yield: (Client, Config, db_path)."""
    tmp = tempfile.mkdtemp(prefix="plan_test_")
    cfg = Config(db_path=str(Path(tmp) / "catalogo.db"), labels=LABELS, null_prompts=["una foto"],
                 model_name="test-model", **(cfg_extra or {}))
    # db.SCHEMA crea l'indice ix_plan_folder prima della tabella plan: su un catalogo nuovo fallisce.
    # Pre-creo la tabella (stessa definizione) cosi' lo script funziona anche su catalogo vuoto.
    raw = sqlite3.connect(cfg.db_path)
    raw.execute("CREATE TABLE IF NOT EXISTS plan(file_id INTEGER PRIMARY KEY, folder TEXT, context TEXT, "
                "score REAL, is_dup INTEGER DEFAULT 0, dup_of INTEGER)")
    raw.commit()
    raw.close()
    conn = dbm.connect(cfg.db_path)
    conn.executemany(
        "INSERT INTO files(id,path,name,kind,size,date_taken,date_source,status) VALUES(?,?,?,?,?,?,?, 'embedded')",
        [(i, f"D:/src/{n}", n, k, s, d, "exif" if d else None) for i, n, k, s, d in files],
    )
    conn.executemany("INSERT INTO plan(file_id,folder) VALUES(?,?)", list(plan.items()))
    conn.commit()
    conn.close()

    app = FastAPI()
    app.include_router(routes_plan.router)
    app.include_router(routes_duplicates.router)

    @app.get("/api/jobs/{job_id}")  # come nel core (app.py), senza importare l'app completa
    def job_get(job_id: str):
        j = jobs.get_job(job_id)
        return j.public() if j else {"state": "error", "error": "job non trovato"}

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    with _patched(deps, "get_cfg", lambda: cfg):
        th.start()
        end = time.time() + 10
        while not server.started and time.time() < end:
            time.sleep(0.02)
        try:
            yield Client(f"http://127.0.0.1:{port}"), cfg
        finally:
            server.should_exit = True
            th.join(timeout=10)


def _wait_job(client: Client, job_id: str, timeout: float = 20) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job non terminato")


def _groups(client: Client) -> dict:
    return {g["folder"]: g for g in client.get("/api/plan/summary").json()["groups"]}


def _db(cfg: Config):
    return dbm.connect(cfg.db_path)


# ------------------------------------------------------------ summary
def test_summary_classifica_e_aggrega():
    with fresh_catalog() as (c, _):
        body = c.get("/api/plan/summary").json()
        assert body["built"] is True
        assert body["total"] == 10  # il file senza gruppo (folder NULL) non conta
        assert body["counts"] == {"context": 1, "event": 1, "varie": 1, "incerta": 1, "dup": 1, "similar": 1}

        g = _groups(c)
        mare = g["2020/06 - Mare"]
        assert mare["kind"] == "context"
        assert (mare["count"], mare["videos"], mare["bytes"]) == (4, 1, 10000)
        assert mare["first"] == "2020-06-01T10:00:00"
        assert mare["last"] == "2020-06-04T10:00:00"
        assert mare["cover_id"] == 1  # primo file immagine
        assert g["2020/06 - Evento 2020-06-09"]["kind"] == "event"
        assert g["2020/07 - Varie"]["kind"] == "varie"
        assert g["2020/07 - Varie"]["cover_id"] is None  # solo video: nessuna copertina immagine
        assert g["Data incerta/2019 - Varie"]["kind"] == "incerta"  # prevale su 'Varie'
        assert g["_Duplicati"]["kind"] == "dup"
        assert g["_Duplicati/Simili"]["kind"] == "similar"


def test_summary_piano_vuoto():
    with fresh_catalog(plan={}) as (c, _):
        body = c.get("/api/plan/summary").json()
        assert body == {"built": False, "total": 0, "groups": [], "counts": {k: 0 for k in routes_plan.KINDS},
                        "missing": 0}


def test_summary_prestazioni_14k_file():
    """14.000 file in ~1.500 gruppi: una sola query, deve restare sotto 150 ms (HTTP incluso)."""
    n = 14000
    files = [(i, f"{i}.jpg", "video" if i % 9 == 0 else "image", 2_000_000, "2015-01-01T00:00:00")
             for i in range(100, 100 + n)]
    plan = {100 + i: f"{2010 + (i % 12)}/{(i % 1500) % 12 + 1:02d} - Gruppo {i % 1500:04d}" for i in range(n)}
    with fresh_catalog(files=files, plan=plan) as (c, _):
        c.get("/api/plan/summary")  # riscaldamento
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            r = c.get("/api/plan/summary")
            best = min(best, time.perf_counter() - t0)
        assert r.status_code == 200
        assert len(r.json()["groups"]) >= 1000
        assert best < 0.150, f"summary troppo lento: {best * 1000:.0f} ms"


# ------------------------------------------------------------ cartella
def test_folder_paginata_e_ordinata():
    with fresh_catalog() as (c, _):
        r = c.get("/api/plan/folder", params={"name": "2020/06 - Mare", "limit": 2}).json()
        assert r["folder"] == "2020/06 - Mare"
        assert r["count"] == 4 and r["total"] == 4
        assert [i["id"] for i in r["items"]] == [1, 2]
        r2 = c.get("/api/plan/folder", params={"name": "2020/06 - Mare", "limit": 2, "offset": 2}).json()
        assert [i["id"] for i in r2["items"]] == [3, 4]
        assert r2["items"][0]["folder"] == "2020/06 - Mare"
        empty = c.get("/api/plan/folder", params={"name": "non esiste"}).json()
        assert empty["count"] == 0 and empty["items"] == []


# ------------------------------------------------------------ rinomina
def test_rinomina_e_unione():
    with fresh_catalog() as (c, _):
        r = c.post("/api/plan/rename", json={"old": "2020/06 - Mare", "new": "2020/06 - Liguria"})
        assert r.status_code == 200
        assert r.json()["folder"] == "2020/06 - Liguria" and r.json()["count"] == 4
        g = _groups(c)
        assert "2020/06 - Mare" not in g and g["2020/06 - Liguria"]["count"] == 4
        # unione: il gruppo 'Varie' confluisce in uno esistente
        r = c.post("/api/plan/rename", json={"old": "2020/07 - Varie", "new": "2020/06 - Liguria"})
        assert r.json()["count"] == 5
        assert "2020/07 - Varie" not in _groups(c)


def test_rinomina_nomi_non_validi():
    bad = ["", "   ", "..", "a/../b", "a:b", "CON", "foto.", 'x"y', "a*b", "a|b", "x" * 200]
    with fresh_catalog() as (c, _):
        for nome in bad:
            r = c.post("/api/plan/rename", json={"old": "2020/06 - Mare", "new": nome})
            assert r.status_code == 400, nome
            assert isinstance(r.json()["detail"], str) and r.json()["detail"], nome


def test_rinomina_duplicati_senza_distinguere_maiuscole_e_nomi_lunghi():
    with fresh_catalog() as (c, _):
        for nome in ("_duplicati", "_DUPLICATI/Simili", "a/" + "x" * 121):
            r = c.post("/api/plan/rename", json={"old": "2020/06 - Mare", "new": nome})
            assert r.status_code == 400, nome


def test_ricalcolo_doppio_clic_una_sola_volta():
    """Due richieste ravvicinate: la seconda trova il job in corso (start_job_unique e' atomico)."""
    import threading

    gate = threading.Event()
    with fresh_catalog() as (c, _), _patched(routes_plan, "_run_rebuild", lambda job: gate.wait(5) and {"ok": True}):
        r1 = c.post("/api/plan/rebuild")
        r2 = c.post("/api/plan/rebuild")
        gate.set()
        assert r1.status_code == 200 and r2.status_code == 409, (r1.status_code, r2.status_code)
        _wait_job(c, r1.json()["job_id"])


def test_ricalcolo_rifiutato_durante_applicazione():
    with fresh_catalog() as (c, _), _patched(routes_plan.jobs, "running_job",
                                             lambda name: object() if name == "apply" else None):
        assert c.post("/api/plan/rebuild").status_code == 409


def test_rinomina_gruppi_speciali_e_inesistenti():
    with fresh_catalog() as (c, _):
        assert c.post("/api/plan/rename", json={"old": "_Duplicati", "new": "X"}).status_code == 400
        assert c.post("/api/plan/rename", json={"old": "2020/06 - Mare", "new": "_Duplicati/Y"}).status_code == 400
        assert c.post("/api/plan/rename", json={"old": "non esiste", "new": "X"}).status_code == 404


# ------------------------------------------------------------ sposta
def test_sposta_file():
    with fresh_catalog() as (c, _):
        r = c.post("/api/plan/move", json={"file_ids": [1, 2, 2], "folder": "2021/01 - Test"})
        assert r.status_code == 200
        assert r.json()["moved"] == 2 and r.json()["group"]["count"] == 2
        items = c.get("/api/plan/folder", params={"name": "2021/01 - Test"}).json()["items"]
        assert sorted(i["id"] for i in items) == [1, 2]
        assert _groups(c)["2020/06 - Mare"]["count"] == 2


def test_sposta_errori():
    bad = [
        {"file_ids": [], "folder": "X"},
        {"file_ids": [999], "folder": "X"},
        {"file_ids": [1], "folder": "../x"},
        {"file_ids": [1], "folder": "_Duplicati"},
    ]
    with fresh_catalog() as (c, _):
        for body in bad:
            r = c.post("/api/plan/move", json=body)
            assert r.status_code == 400, body
            assert r.json()["detail"], body


# ------------------------------------------------------------ albero
def test_albero_cartelle():
    with fresh_catalog() as (c, _):
        tree = c.get("/api/plan/folders/tree").json()
        assert [n["name"] for n in tree] == ["2020", "_Duplicati", "Data incerta"]
        anno = tree[0]
        assert anno["folder"] is None and anno["count"] == 7
        assert [ch["name"] for ch in anno["children"]] == ["06 - Evento 2020-06-09", "06 - Mare", "07 - Varie"]
        assert anno["children"][1] == {"name": "06 - Mare", "folder": "2020/06 - Mare", "count": 4, "children": []}
        dup = tree[1]
        assert dup["folder"] == "_Duplicati" and dup["children"][0]["folder"] == "_Duplicati/Simili"


# ------------------------------------------------------------ ricalcolo
def test_ricalcolo_senza_caricare_il_modello():
    """Vettori etichetta gia' in cache: il job non deve caricare nessun Embedder."""

    def _no_model(*a, **k):
        raise AssertionError("il modello non doveva essere caricato")

    with fresh_catalog() as (c, cfg):
        conn = _db(cfg)
        conn.executemany("INSERT INTO embeddings(file_id,vec) VALUES(?,?)",
                         [(i, dbm.to_blob(np.eye(3, dtype=np.float32)[i % 3])) for i, *_ in FILES])
        dbm.set_meta(conn, "label_model", cfg.model_name)
        conn.executemany("INSERT INTO label_cache(prompt,vec) VALUES(?,?)",
                         [("mare", dbm.to_blob([1, 0, 0])), ("tramonto", dbm.to_blob([0, 1, 0])),
                          ("una foto", dbm.to_blob([0, 0, 1]))])
        conn.commit()
        conn.close()
        with _patched(embedder_mod, "Embedder", _no_model):
            r = c.post("/api/plan/rebuild")
            assert r.status_code == 200
            j = _wait_job(c, r.json()["job_id"])
        assert j["state"] == "done", j.get("error")
        assert j["result"]["file"] == len(FILES)
        assert c.get("/api/plan/summary").json()["total"] > 0
        # plan.context = etichetta assegnata: mai sotto soglia (Libera spazio e album la usano cosi')
        conn = _db(cfg)
        rows = conn.execute("SELECT context, score FROM plan").fetchall()
        conn.close()
        assert all(r["context"] is None or r["score"] >= cfg.score_threshold for r in rows), [tuple(r) for r in rows]


def test_ricalcolo_occupato_409():
    with fresh_catalog() as (c, _):
        with _patched(routes_plan.jobs, "running_job", lambda name: object()):
            assert c.post("/api/plan/rebuild").status_code == 409
            assert c.post("/api/plan/rename", json={"old": "2020/06 - Mare", "new": "X"}).status_code == 409


def test_reset_overrides_svuota_e_ricalcola():
    # build_plan reale richiederebbe embedding: qui il ricalcolo e' finto, conta solo che parta
    with fresh_catalog() as (c, cfg), _patched(routes_plan, "_run_rebuild", lambda job: {"ricalcolato": True}):
        c.post("/api/plan/move", json={"file_ids": [1], "folder": "2021/01 - Test"})
        c.post("/api/plan/rename", json={"old": "2020/07 - Varie", "new": "Altro"})
        assert c.post("/api/plan/reset-overrides").status_code == 400  # senza conferma non cancella nulla
        r = c.post("/api/plan/reset-overrides", json={"confirm": True})
        assert r.status_code == 200
        j = _wait_job(c, r.json()["job_id"])
        assert j["state"] == "done" and j["result"]["ricalcolato"] is True
        conn = _db(cfg)
        assert conn.execute("SELECT COUNT(*) FROM file_override").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM rename_map").fetchone()[0] == 0
        conn.close()


# ------------------------------------------------------------ persistenza (ricalcolo reale)
def _rebuild_real(client: Client) -> dict:
    """Ricalcolo vero (organizer.build_plan, senza modello: il catalogo di test non ha embedding)."""
    j = _wait_job(client, client.post("/api/plan/rebuild").json()["job_id"])
    assert j["state"] == "done", j.get("error")
    return j["result"]


def _folder_of(client: Client, fid: int):
    for g in client.get("/api/plan/summary").json()["groups"]:
        ids = [i["id"] for i in client.get("/api/plan/folder", params={"name": g["folder"], "limit": 500}).json()["items"]]
        if fid in ids:
            return g["folder"]
    return None


def _dup_master(client: Client, fid: int = 9):
    groups = client.get("/api/duplicates", params={"type": "exact"}).json()["groups"]
    for g in groups:
        if g["master"]["id"] == fid or any(m["id"] == fid for m in g["members"]):
            return g["master"]["id"]
    return None


def test_ricalcolo_reale_conserva_rinomina_e_spostamenti():
    with fresh_catalog() as (c, _):
        _rebuild_real(c)
        base = _folder_of(c, 1)
        assert base and base != "2020/06 - Estate"
        assert c.post("/api/plan/rename", json={"old": base, "new": "2020/06 - Estate"}).status_code == 200
        assert c.post("/api/plan/move", json={"file_ids": [7], "folder": "2020/06 - Estate"}).status_code == 200
        assert c.post("/api/plan/move", json={"file_ids": [2], "folder": "Nuova/X1"}).status_code == 200
        # la destinazione di uno spostamento viene rinominata: il file deve seguire la rinomina
        assert c.post("/api/plan/rename", json={"old": "Nuova/X1", "new": "Nuova/X2"}).status_code == 200
        _rebuild_real(c)
        assert _folder_of(c, 1) == "2020/06 - Estate"
        assert _folder_of(c, 7) == "2020/06 - Estate"
        assert _folder_of(c, 2) == "Nuova/X2"
        assert base not in _groups(c)


def test_rinomina_con_caratteri_speciali_sopravvive():
    nome = "2020/06 - Festa & #1 àèì (test) 100%"
    with fresh_catalog() as (c, _):
        _rebuild_real(c)  # la rinomina conta solo per gruppi che il calcolo genera davvero
        base = _folder_of(c, 1)
        n = _groups(c)[base]["count"]
        r = c.post("/api/plan/rename", json={"old": base, "new": nome})
        assert r.status_code == 200 and r.json()["folder"] == nome and r.json()["count"] == n
        _rebuild_real(c)
        assert _groups(c)[nome]["count"] == n


def test_cartella_vuota_dopo_spostamento():
    with fresh_catalog() as (c, _):
        assert c.post("/api/plan/move", json={"file_ids": [1, 2, 3, 4], "folder": "2021/02 - Altra"}).status_code == 200
        assert "2020/06 - Mare" not in _groups(c)
        r = c.get("/api/plan/folder", params={"name": "2020/06 - Mare"})
        assert r.status_code == 200 and r.json()["count"] == 0 and r.json()["items"] == []
        # rinominare un gruppo inesistente (ora vuoto) e' un 404 con messaggio, non un errore 500
        r = c.post("/api/plan/rename", json={"old": "2020/06 - Mare", "new": "X"})
        assert r.status_code == 404 and r.json()["detail"]


def test_scambio_master_sopravvive_al_ricalcolo_e_alla_rinomina():
    with fresh_catalog() as (c, cfg):
        conn = _db(cfg)
        conn.execute("UPDATE files SET hash='stesso' WHERE id IN (9, 10)")
        conn.commit()
        conn.close()
        _rebuild_real(c)
        assert _dup_master(c) == 9
        assert c.post("/api/duplicates/keep", json={"master_id": 10, "member_ids": [9]}).status_code == 200
        _rebuild_real(c)
        assert _dup_master(c) == 10                      # il piano grezzo non annulla lo scambio
        assert c.post("/api/plan/rename", json={"old": _folder_of(c, 10), "new": "Scambio"}).status_code == 200
        _rebuild_real(c)
        assert _dup_master(c) == 10                      # la rinomina della cartella dell'originale lo segue
        j = _wait_job(c, c.post("/api/plan/reset-overrides", json={"confirm": True}).json()["job_id"])
        assert j["state"] == "done", j.get("error")
        assert _dup_master(c) == 9                       # reset: scelte dell'utente cancellate


def test_summary_missing_foto_non_nel_piano():
    with fresh_catalog() as (c, cfg):
        assert c.get("/api/plan/summary").json()["missing"] == 0      # il piano copre tutti gli embedded
        conn = _db(cfg)
        conn.execute("INSERT INTO files(id,path,name,kind,size,date_taken,status) "
                     "VALUES(12,'D:/src/NUOVA.jpg','NUOVA.jpg','image',100,'2021-01-01T10:00:00','embedded')")
        conn.execute("INSERT INTO files(id,path,name,kind,size,status) "
                     "VALUES(13,'D:/src/NEW.jpg','NEW.jpg','image',100,'new')")  # non ancora analizzata: non conta
        conn.commit()
        conn.close()
        assert c.get("/api/plan/summary").json()["missing"] == 1


if __name__ == "__main__":
    ok = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            ok += 1
            print(f"ok  {name}")
    print(f"{ok} test superati")
