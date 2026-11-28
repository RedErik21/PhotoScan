"""Robustezza delle route (file, album, libreria) e difesa Host/Origin del server locale.

Catalogo e cache temporanei: il catalogo vero non viene mai toccato.
Esecuzione: `python tests/test_api_robustezza.py` oppure `python tools/run_tests.py robustezza`.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="fs_robust_test_"))
os.environ["FOTOSCAN_CONFIG"] = str(_TMP / "config.json")
os.environ["FOTOSCAN_DB"] = str(_TMP / "catalogo.db")
os.environ["FOTOSCAN_CACHE"] = str(_TMP / "cache")

import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fotoscan import db as dbm  # noqa: E402
from fotoscan.server import deps  # noqa: E402
from fotoscan.server.app import create_app  # noqa: E402

APP = create_app()
CLIENT = TestClient(APP)
SRC = _TMP / "src"
SRC.mkdir()
IDS: dict[str, int] = {}


def _conn():
    return dbm.connect(deps.get_cfg().db_path)


def setup_module(_=None):
    deps.reload_cfg()
    conn = _conn()
    for key, kind, esiste in (("a", "image", True), ("b", "image", True), ("sparito", "image", False)):
        p = SRC / f"{key}.jpg"
        if esiste:
            p.write_bytes(b"x")
        cur = conn.execute(
            "INSERT INTO files(path,name,kind,size,mtime,date_taken,date_source,status) VALUES(?,?,?,?,?,?,?,?)",
            (str(p), p.name, kind, 1, 0.0, "2021-05-05T10:00:00", "exif", "embedded"))
        IDS[key] = cur.lastrowid
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ id limite
def test_id_enormi_o_unicode_non_danno_500():
    assert CLIENT.get(f"/api/files/{2**70}").status_code == 404
    assert CLIENT.get("/api/files/²").status_code == 422          # '²'.isdigit() ma int() fallisce
    assert CLIENT.get(f"/api/files/{2**70}/albums").status_code == 404
    assert CLIENT.get(f"/api/albums/{2**70}/files").status_code == 404
    r = CLIENT.post("/api/files/favorite", json={"ids": [2**70], "value": True})
    assert r.status_code == 422
    r = CLIENT.post(f"/api/files/{2**70}/favorite", json={"value": True})
    assert r.status_code == 404
    r = CLIENT.post("/api/albums", json={"name": "Limite"})
    aid = r.json()["id"]
    assert CLIENT.post(f"/api/albums/{aid}/items", json={"file_ids": [2**70]}).status_code == 422


def test_album_nome_unico_senza_maiuscole():
    assert CLIENT.post("/api/albums", json={"name": "Vacanze"}).status_code == 201
    assert CLIENT.post("/api/albums", json={"name": "vacanze"}).status_code == 409


# --------------------------------------------------- date anomale e timeline
def test_date_anomale_non_rompono_timeline_e_ricordi():
    conn = _conn()
    conn.execute("UPDATE files SET date_taken='' WHERE id=?", (IDS["b"],))
    conn.commit()
    conn.close()
    for url in ("/api/timeline/buckets", "/api/timeline/buckets?granularity=day", "/api/timeline/years",
                "/api/memories?on=2030-05-05"):
        r = CLIENT.get(url)
        assert r.status_code == 200, (url, r.text)
    b = CLIENT.get("/api/timeline/buckets").json()
    assert b["total"] == CLIENT.get("/api/files").json()["total"]


# ------------------------------------------------------------ libreria broken
def test_broken_remove_riverifica():
    conn = _conn()
    conn.execute("DELETE FROM broken")
    for key in ("a", "sparito"):
        conn.execute("INSERT INTO broken(file_id, reason, checked) VALUES(?, 'mancante', datetime('now'))", (IDS[key],))
    conn.commit()
    conn.close()
    assert CLIENT.post("/api/library/broken/remove", json={}).status_code == 400
    r = CLIENT.post("/api/library/broken/remove", json={"confirm": True}).json()
    assert r["removed"] == 1 and r["recovered"] == 1, r  # 'a' esiste ancora: resta nel catalogo
    conn = _conn()
    resta = {x[0] for x in conn.execute("SELECT id FROM files")}
    segni = conn.execute("SELECT COUNT(*) FROM broken").fetchone()[0]
    conn.close()
    assert IDS["a"] in resta and IDS["sparito"] not in resta and segni == 0


def test_broken_remove_salta_disco_non_raggiungibile():
    if os.name != "nt":
        return
    import string
    libera = next((d for d in reversed(string.ascii_uppercase) if not Path(f"{d}:/").exists()), None)
    if libera is None:
        return
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO files(path,name,kind,size,mtime,date_taken,date_source,status) VALUES(?,?,?,?,?,?,?,?)",
        (f"{libera}:/foto/x.jpg", "x.jpg", "image", 1, 0.0, "2021-01-01T00:00:00", "exif", "embedded"))
    fid = cur.lastrowid
    conn.execute("INSERT INTO broken(file_id, reason, checked) VALUES(?, 'mancante', datetime('now'))", (fid,))
    conn.commit()
    conn.close()
    r = CLIENT.post("/api/library/broken/remove", json={"confirm": True}).json()
    assert r["skipped"] == 1 and r["removed"] == 0, r
    conn = _conn()
    assert conn.execute("SELECT 1 FROM files WHERE id=?", (fid,)).fetchone() is not None
    conn.close()


# --------------------------------------------------------------- embedding
def test_embedding_di_dimensioni_miste():
    conn = _conn()
    conn.execute("DELETE FROM embeddings")
    conn.execute("INSERT INTO embeddings(file_id, vec) VALUES(?,?)", (IDS["a"], dbm.to_blob(np.ones(8))))
    conn.execute("INSERT INTO embeddings(file_id, vec) VALUES(?,?)", (IDS["b"], dbm.to_blob(np.ones(8))))
    conn.execute("INSERT INTO embeddings(file_id, vec) VALUES(?,?)", (IDS["sparito"], dbm.to_blob(np.ones(4))))
    conn.commit()
    ids, mat = dbm.load_embeddings(conn)
    conn.execute("DELETE FROM embeddings")
    conn.commit()
    conn.close()
    assert mat.shape == (2, 8) and len(ids) == 2


# ------------------------------------------------------------ Host / Origin
def test_host_non_locale_rifiutato():
    assert CLIENT.get("/api/health").status_code == 200  # TestClient: Host "testserver"
    for host in ("evil.com", "127.0.0.1.evil.com", "192.168.1.5:8000", ""):
        r = CLIENT.get("/api/health", headers={"host": host})
        assert r.status_code == 403, host
    for host in ("127.0.0.1:8765", "localhost:8765", "localhost", "[::1]:8765"):
        assert CLIENT.get("/api/health", headers={"host": host}).status_code == 200, host


def test_origin_estraneo_rifiutato_sulle_scritture():
    h = {"host": "127.0.0.1:8765"}
    post = lambda origin: CLIENT.post("/api/jobs/inesistente/cancel", headers={**h, **({"origin": origin} if origin else {})})  # noqa: E731
    assert post(None).status_code == 404                              # nessun Origin: ammesso
    assert post("http://127.0.0.1:8765").status_code == 404           # stessa origine
    assert post("http://localhost:8765").status_code == 404           # altro nome locale, stessa porta
    for brutta in ("http://evil.com", "http://127.0.0.1:9999", "null", "https://evil.com:8765", "file://"):
        assert post(brutta).status_code == 403, brutta
    # le letture non sono soggette al controllo Origin
    assert CLIENT.get("/api/health", headers={**h, "origin": "http://evil.com"}).status_code == 200


def test_statici_e_indice_funzionano():
    assert CLIENT.get("/", headers={"host": "127.0.0.1:8765"}).status_code in (200, 404)  # 404 solo senza frontend


if __name__ == "__main__":
    import shutil
    import traceback

    setup_module()
    ok = fail = 0
    for nome, fn in list(globals().items()):
        if nome.startswith("test_") and callable(fn):
            try:
                fn()
                ok += 1
                print("OK  ", nome)
            except Exception:
                fail += 1
                print("FAIL", nome)
                traceback.print_exc()
    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{ok} ok, {fail} falliti")
    sys.exit(1 if fail else 0)
