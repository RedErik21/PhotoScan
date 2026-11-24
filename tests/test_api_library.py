"""Test API album (fotoscan/server/routes_library.py) su un catalogo temporaneo.

Esecuzione: `python tests/test_api_library.py` (o `python -m pytest tests/test_api_library.py`).
Richiede httpx per il TestClient di FastAPI; senza httpx i test vengono saltati con avviso.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fotoscan import db as dbm  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
except Exception:  # httpx mancante
    TestClient = None

_state: dict = {}


def _seed(path: Path, n_files: int = 6) -> None:
    # Workaround: in db.SCHEMA l'indice ix_plan_folder precede CREATE TABLE plan, quindi su un
    # catalogo nuovo connect() si interrompe prima di creare plan. Creiamo plan prima.
    import sqlite3
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE IF NOT EXISTS plan(file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE, "
        "folder TEXT, context TEXT, score REAL, is_dup INTEGER DEFAULT 0, dup_of INTEGER)"
    )
    raw.close()
    conn = dbm.connect(str(path))
    for i in range(1, n_files + 1):
        # file 1 senza data: deve finire in coda
        date = None if i == 1 else f"2020-06-{i:02d}T10:00:00"
        conn.execute(
            "INSERT INTO files(id, path, name, kind, date_taken, status) VALUES(?,?,?,?,?,'embedded')",
            (i, f"D:/tmp/IMG_{i}.jpg", f"IMG_{i}.jpg", "image", date),
        )
    conn.commit()
    conn.close()


def _client():
    from fastapi.testclient import TestClient as TC
    from fotoscan.server import deps
    from fotoscan.server.app import create_app

    tmp = Path(tempfile.mkdtemp(prefix="fs_lib_"))
    db_path = tmp / "catalogo.db"
    _seed(db_path)
    app = create_app()

    def _get_db():
        conn = dbm.connect(str(db_path))
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[deps.get_db] = _get_db
    return TC(app)


def _c():
    if "c" not in _state:
        _state["c"] = _client()
    return _state["c"]


def _skip_if_no_client() -> bool:
    if TestClient is None:
        print("SKIP: httpx non installato, test API album saltati")
        return True
    return False


def test_create_trim_and_validation():
    if _skip_if_no_client():
        return
    c = _c()
    r = c.post("/api/albums", json={"name": "  Vacanze  "})
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["name"] == "Vacanze" and a["count"] == 0 and a["cover_id"] is None and a["created"]
    assert c.post("/api/albums", json={"name": "   "}).status_code == 400
    assert c.post("/api/albums", json={}).status_code == 400
    r = c.post("/api/albums", json={"name": "x" * 81})
    assert r.status_code == 400 and "80" in r.json()["detail"]
    assert c.post("/api/albums", json={"name": "x" * 80}).status_code == 201


def test_unique_name_case_insensitive_italian_message():
    if _skip_if_no_client():
        return
    c = _c()
    c.post("/api/albums", json={"name": "Natale"})
    r = c.post("/api/albums", json={"name": " natale "})
    assert r.status_code == 409
    assert "già" in r.json()["detail"]
    # rinomina verso un nome gia' usato -> 409; verso se stesso -> ok
    other = c.post("/api/albums", json={"name": "Altro"}).json()
    assert c.put(f"/api/albums/{other['id']}", json={"name": "NATALE"}).status_code == 409
    assert c.put(f"/api/albums/{other['id']}", json={"name": "Altro"}).status_code == 200


def test_add_items_idempotent_and_missing_ignored():
    if _skip_if_no_client():
        return
    c = _c()
    aid = c.post("/api/albums", json={"name": "Mare"}).json()["id"]
    r = c.post(f"/api/albums/{aid}/items", json={"file_ids": [2, 3, 3, 999, 998]})
    assert r.status_code == 200, r.text
    assert r.json() == {"added": 2, "already_present": 0, "missing": 2}
    r = c.post(f"/api/albums/{aid}/items", json={"file_ids": [2, 4]})
    assert r.json() == {"added": 1, "already_present": 1, "missing": 0}
    album =[x for x in c.get("/api/albums").json() if x["id"] == aid][0]
    assert album["count"] == 3
    assert c.post(f"/api/albums/{aid}/items", json={"file_ids": []}).status_code == 400
    assert c.post("/api/albums/99999/items", json={"file_ids": [1]}).status_code == 404


def test_album_files_paginated_ordered_by_date():
    if _skip_if_no_client():
        return
    c = _c()
    aid = c.post("/api/albums", json={"name": "Ordine"}).json()["id"]
    c.post(f"/api/albums/{aid}/items", json={"file_ids": [5, 1, 3, 2]})
    r = c.get(f"/api/albums/{aid}/files", params={"limit": 2, "offset": 0}).json()
    assert r["total"] == 4
    # file 1 senza data in coda; altrimenti per data crescente: 2, 3, 5, 1
    assert [f["id"] for f in r["items"]] == [2, 3]
    assert [f["id"] for f in c.get(f"/api/albums/{aid}/files", params={"offset": 2}).json()["items"]] == [5, 1]
    item = r["items"][0]
    assert item["thumb"] == "/api/files/2/thumb" and item["favorite"] is False
    assert c.get("/api/albums/99999/files").status_code == 404


def test_cover_default_and_explicit():
    if _skip_if_no_client():
        return
    c = _c()
    aid = c.post("/api/albums", json={"name": "Copertina"}).json()["id"]
    c.post(f"/api/albums/{aid}/items", json={"file_ids": [4, 3, 6]})
    # senza copertina impostata: la prima per data (3)
    assert _album(c, aid)["cover_id"] == 3
    r = c.put(f"/api/albums/{aid}", json={"cover_file_id": 6})
    assert r.status_code == 200 and r.json()["cover_id"] == 6
    # una foto che non e' nell'album non puo' essere copertina
    assert c.put(f"/api/albums/{aid}", json={"cover_file_id": 1}).status_code == 400
    # rimuovere la copertina dall'album la annulla
    c.request("DELETE", f"/api/albums/{aid}/items", json={"file_ids": [6]})
    assert _album(c, aid)["cover_id"] == 3
    # reset esplicito a null
    c.put(f"/api/albums/{aid}", json={"cover_file_id": 4})
    assert _album(c, aid)["cover_id"] == 4
    assert c.put(f"/api/albums/{aid}", json={"cover_file_id": None}).json()["cover_id"] == 3


def test_remove_items_keeps_files():
    if _skip_if_no_client():
        return
    c = _c()
    aid = c.post("/api/albums", json={"name": "Togli"}).json()["id"]
    c.post(f"/api/albums/{aid}/items", json={"file_ids": [2, 3]})
    r = c.request("DELETE", f"/api/albums/{aid}/items", json={"file_ids": [2, 77]})
    assert r.status_code == 200 and r.json() == {"removed": 1}
    assert c.get(f"/api/albums/{aid}/files").json()["total"] == 1
    assert c.get("/api/files/2").status_code == 200  # il file resta nel catalogo


def test_delete_album_keeps_files():
    if _skip_if_no_client():
        return
    c = _c()
    aid = c.post("/api/albums", json={"name": "Da cancellare"}).json()["id"]
    c.post(f"/api/albums/{aid}/items", json={"file_ids": [1, 2]})
    assert c.delete(f"/api/albums/{aid}").status_code == 200
    assert c.get(f"/api/albums/{aid}/files").status_code == 404
    assert c.delete(f"/api/albums/{aid}").status_code == 404
    assert c.get("/api/files/1").status_code == 200 and c.get("/api/files/2").status_code == 200


def test_file_albums():
    if _skip_if_no_client():
        return
    c = _c()
    a1 = c.post("/api/albums", json={"name": "B album"}).json()["id"]
    a2 = c.post("/api/albums", json={"name": "A album"}).json()["id"]
    c.post(f"/api/albums/{a1}/items", json={"file_ids": [5]})
    c.post(f"/api/albums/{a2}/items", json={"file_ids": [5]})
    # il catalogo e' condiviso tra i test: il file 5 puo' stare anche in altri album
    names = [a["name"] for a in c.get("/api/files/5/albums").json()]
    assert [n for n in names if n in ("A album", "B album")] == ["A album", "B album"]
    assert names == sorted(names, key=str.lower)
    assert c.get("/api/files/99999/albums").status_code == 404
    assert c.get("/api/files/99999/albums").status_code == 404


def test_list_performance_50_albums():
    if _skip_if_no_client():
        return
    c = _c()
    for i in range(50):
        aid = c.post("/api/albums", json={"name": f"Perf {i:02d}"}).json()["id"]
        c.post(f"/api/albums/{aid}/items", json={"file_ids": [1, 2, 3, 4, 5, 6]})
    c.get("/api/albums")  # riscaldamento
    tempi = []
    for _ in range(3):  # il minimo riduce il rumore dovuto al carico della macchina
        t0 = time.perf_counter()
        r = c.get("/api/albums")
        tempi.append((time.perf_counter() - t0) * 1000)
    dt = min(tempi)
    assert r.status_code == 200 and len(r.json()) >= 50
    print(f"  GET /albums con {len(r.json())} album: {dt:.1f} ms")
    assert dt < 50, f"GET /albums troppo lento: {dt:.1f} ms"


def _album(c, aid):
    return [a for a in c.get("/api/albums").json() if a["id"] == aid][0]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK   {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e!r}")
    print(f"{len(tests) - failed}/{len(tests)} ok")
    sys.exit(1 if failed else 0)
