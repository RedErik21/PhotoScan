"""Test delle route "Etichette suggerite" (/api/labels/suggestions*, /api/labels/visual*).

La ricerca dei temi (POST /labels/suggestions/run) richiede il modello: qui non viene mai avviata.
I suggerimenti sono inseriti direttamente in label_suggestions, come farebbe il job.
Esecuzione: `python tests\\test_api_suggest.py` oppure `python -m pytest tests\\test_api_suggest.py`.
"""
from __future__ import annotations

import contextlib
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fotoscan import db as dbm  # noqa: E402
from fotoscan.config import Config  # noqa: E402
from fotoscan.server import deps  # noqa: E402
from fotoscan.server.app import app  # noqa: E402

DIM = 4


@contextlib.contextmanager
def fresh_env():
    """Catalogo temporaneo vuoto (solo schema) con config di prova."""
    tmp = Path(tempfile.mkdtemp(prefix="fs_suggest_"))
    cfg = Config(db_path=str(tmp / "c.db"), cache_dir=str(tmp / "cache"))
    # db.SCHEMA crea l'indice ix_plan_folder prima della tabella plan: su un catalogo nuovo serve la tabella prima
    raw = sqlite3.connect(cfg.db_path)
    raw.executescript("CREATE TABLE plan(file_id INTEGER PRIMARY KEY, folder TEXT, context TEXT, score REAL, "
                      "is_dup INTEGER DEFAULT 0, dup_of INTEGER);")
    raw.close()
    conn = dbm.connect(cfg.db_path)

    def get_db_override():
        c = dbm.connect(cfg.db_path)
        try:
            yield c
        finally:
            c.close()

    try:
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(deps, "get_cfg", lambda: cfg))
            app.dependency_overrides[deps.get_db] = get_db_override
            try:
                yield {"cfg": cfg, "conn": conn, "client": TestClient(app)}
            finally:
                app.dependency_overrides.pop(deps.get_db, None)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def add_suggestion(conn, sid, name, alt=None, sample=(1, 2, 3, 4, 5, 6, 7, 8, 9), n=12, days=4):
    vec = np.arange(DIM, dtype=np.float32) + sid
    conn.execute(
        "INSERT INTO label_suggestions(id, name, alt, vec, thr, n, days, months, sample, created) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (sid, name, alt, dbm.to_blob(vec), 0.8, n, days, 2, "[" + ",".join(map(str, sample)) + "]",
         "2026-11-20T10:00:00"))
    conn.commit()


def add_visual(conn, name, sample=(10, 11, 12, 13, 14), n=5):
    conn.execute("INSERT INTO visual_labels(name, vec, thr, n, sample, created) VALUES(?,?,?,?,?,?)",
                 (name, dbm.to_blob(np.ones(DIM, dtype=np.float32)), 0.8, n,
                  "[" + ",".join(map(str, sample)) + "]", "2026-11-20T10:00:00"))
    conn.commit()


# ------------------------------------------------------------ elenco suggerimenti
def test_list_suggestions_empty_then_filled():
    with fresh_env() as env:
        c = env["client"]
        out = c.get("/api/labels/suggestions").json()
        assert out == {"items": [], "created": None, "job_id": None}
        add_suggestion(env["conn"], 1, "Palestra", alt="Fitness", sample=(7, 8, 9))
        out = c.get("/api/labels/suggestions").json()
        assert out["created"] == "2026-11-20T10:00:00"
        item = out["items"][0]
        assert item["name"] == "Palestra" and item["alt"] == "Fitness"
        assert item["count"] == 12 and item["days"] == 4 and item["months"] == 2
        assert item["sample"] == [7, 8, 9]


def test_accept_creates_visual_label_and_removes_suggestion():
    with fresh_env() as env:
        c, conn = env["client"], env["conn"]
        add_suggestion(conn, 1, "Palestra", sample=(1, 2, 3, 4, 5, 6, 7, 8, 9))
        r = c.post("/api/labels/suggestions/1/accept", json={"name": "Palestra Sala"})
        assert r.status_code == 200 and r.json() == {"name": "Palestra Sala"}
        assert conn.execute("SELECT COUNT(*) FROM label_suggestions").fetchone()[0] == 0
        row = conn.execute("SELECT name, n, sample FROM visual_labels").fetchone()
        assert row["name"] == "Palestra Sala" and row["n"] == 12
        assert row["sample"] == "[1,2,3,4,5,6,7,8,9]"


def test_accept_without_name_uses_suggested_name():
    with fresh_env() as env:
        c = env["client"]
        add_suggestion(env["conn"], 2, "Scuola")
        r = c.post("/api/labels/suggestions/2/accept", json={})
        assert r.status_code == 200 and r.json()["name"] == "Scuola"


def test_accept_duplicate_names_are_409():
    with fresh_env() as env:
        c, conn = env["client"], env["conn"]
        add_visual(conn, "Palestra")
        add_suggestion(conn, 3, "Negozio")
        # stesso nome di un'etichetta da esempi (senza distinzione di maiuscole)
        r = c.post("/api/labels/suggestions/3/accept", json={"name": "palestra"})
        assert r.status_code == 409
        # stesso nome di un'etichetta di contesto del catalogo
        label = env["cfg"].labels[0]["name"]
        r = c.post("/api/labels/suggestions/3/accept", json={"name": label})
        assert r.status_code == 409
        # il suggerimento resta disponibile dopo il rifiuto
        assert conn.execute("SELECT COUNT(*) FROM label_suggestions WHERE id=3").fetchone()[0] == 1


def test_accept_invalid_names_are_400():
    with fresh_env() as env:
        c, conn = env["client"], env["conn"]
        add_suggestion(conn, 4, "Tema")
        assert c.post("/api/labels/suggestions/4/accept", json={"name": "   "}).status_code == 400
        assert c.post("/api/labels/suggestions/4/accept", json={"name": "a/b"}).status_code == 400
        assert c.post("/api/labels/suggestions/4/accept", json={"name": "x" * 61}).status_code == 400
        assert conn.execute("SELECT COUNT(*) FROM visual_labels").fetchone()[0] == 0


def test_accept_unknown_suggestion_is_404():
    with fresh_env() as env:
        assert env["client"].post("/api/labels/suggestions/999/accept", json={"name": "X"}).status_code == 404


def test_dismiss_records_vector_and_removes_suggestion():
    with fresh_env() as env:
        c, conn = env["client"], env["conn"]
        add_suggestion(conn, 5, "Cucina")
        assert c.post("/api/labels/suggestions/5/dismiss").json() == {"ok": True}
        assert conn.execute("SELECT COUNT(*) FROM label_suggestions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM label_dismissed").fetchone()[0] == 1
        assert c.post("/api/labels/suggestions/5/dismiss").status_code == 404


# ------------------------------------------------------------ etichette da esempi
def test_list_visual_returns_at_most_four_samples():
    with fresh_env() as env:
        c = env["client"]
        add_visual(env["conn"], "Palestra", sample=(1, 2, 3, 4, 5, 6))
        out = c.get("/api/labels/visual").json()
        assert len(out) == 1
        assert out[0]["name"] == "Palestra" and out[0]["count"] == 5
        assert out[0]["sample"] == [1, 2, 3, 4]


def test_rename_visual():
    with fresh_env() as env:
        c, conn = env["client"], env["conn"]
        add_visual(conn, "Palestra")
        add_visual(conn, "Scuola")
        r = c.put("/api/labels/visual/Palestra", json={"name": "Palestra Sala"})
        assert r.status_code == 200 and r.json() == {"name": "Palestra Sala"}
        names = [v["name"] for v in c.get("/api/labels/visual").json()]
        assert names == ["Palestra Sala", "Scuola"]
        # solo maiuscole/minuscole: consentito
        assert c.put("/api/labels/visual/Scuola", json={"name": "scuola"}).status_code == 200
        # collisione con un'altra etichetta da esempi: 409
        assert c.put("/api/labels/visual/Palestra Sala", json={"name": "scuola"}).status_code == 409
        # etichetta inesistente: 404
        assert c.put("/api/labels/visual/Nope", json={"name": "Altro"}).status_code == 404


def test_delete_visual():
    with fresh_env() as env:
        c, conn = env["client"], env["conn"]
        add_visual(conn, "Palestra")
        assert c.delete("/api/labels/visual/Palestra").json() == {"ok": True}
        assert c.get("/api/labels/visual").json() == []
        assert c.delete("/api/labels/visual/Palestra").status_code == 404


def test_dismissed_count_and_clear():
    with fresh_env() as env:
        c, conn = env["client"], env["conn"]
        assert c.get("/api/labels/suggestions/dismissed").json() == {"count": 0}
        add_suggestion(conn, 10, "Cucina")
        c.post("/api/labels/suggestions/10/dismiss")
        assert c.get("/api/labels/suggestions/dismissed").json() == {"count": 1}
        assert c.delete("/api/labels/suggestions/dismissed").json() == {"cleared": 1}
        assert c.get("/api/labels/suggestions/dismissed").json() == {"count": 0}


def test_update_visual_threshold():
    with fresh_env() as env:
        c, conn = env["client"], env["conn"]
        add_visual(conn, "Palestra")
        assert c.put("/api/labels/visual/Palestra", json={"thr": 0.88}).status_code == 200
        assert c.get("/api/labels/visual").json()[0]["thr"] == 0.88
        assert c.put("/api/labels/visual/Palestra", json={"thr": 0.2}).status_code == 400


def test_preview_suggestion_and_visual():
    with fresh_env() as env:
        c, conn = env["client"], env["conn"]
        conn.execute("INSERT INTO files(id, path, name, kind, status) VALUES(1, 'a.jpg', 'a.jpg', 'image', 'embedded')")
        conn.execute("INSERT INTO embeddings(file_id, vec) VALUES(1, ?)",
                     (dbm.to_blob(np.ones(DIM, dtype=np.float32) / np.sqrt(DIM)),))
        conn.commit()
        add_suggestion(conn, 11, "Palestra", sample=(1,))
        add_visual(conn, "Palestra", sample=(1,))
        r1 = c.get("/api/labels/suggestions/11/preview")
        assert r1.status_code == 200 and len(r1.json()["items"]) == 1
        r2 = c.get("/api/labels/visual/Palestra/preview")
        assert r2.status_code == 200 and len(r2.json()["items"]) == 1
        assert c.get("/api/labels/suggestions/999/preview").status_code == 404
        assert c.get("/api/labels/visual/Nope/preview").status_code == 404


def test_list_and_preview_context_labels():
    with fresh_env() as env:
        c, conn = env["client"], env["conn"]
        label = env["cfg"].labels[0]["name"]
        conn.execute("INSERT INTO files(id, path, name, kind, status) VALUES(1, 'a.jpg', 'a.jpg', 'image', 'embedded')")
        conn.execute("INSERT INTO plan(file_id, folder, context, score, is_dup) VALUES(1, '2024/01', ?, 0.9, 0)", (label,))
        conn.commit()
        ctx_list = c.get("/api/labels/contexts").json()
        hit = next(x for x in ctx_list if x["name"] == label)
        assert hit["count"] == 1 and hit["in_plan"] is True and hit["sample"] == [1]
        r = c.get(f"/api/labels/contexts/{label}/preview?limit=1&offset=0")
        assert r.status_code == 200 and r.json()["total"] == 1 and len(r.json()["items"]) == 1
        r_empty = c.get(f"/api/labels/contexts/{label}/preview?limit=1&offset=1")
        assert r_empty.status_code == 200 and r_empty.json()["items"] == []
        assert c.get("/api/labels/contexts/Inesistente/preview").status_code == 404


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} test superati")
