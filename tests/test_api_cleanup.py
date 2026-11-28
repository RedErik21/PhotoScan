"""Test delle route "Libera spazio" (/api/cleanup/summary, /api/cleanup/items, /api/cleanup/trash)."""
from __future__ import annotations

import contextlib
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fotoscan import db as dbm  # noqa: E402
from fotoscan.config import Config  # noqa: E402
from fotoscan.server import deps, routes_cleanup  # noqa: E402
from fotoscan.server.app import app  # noqa: E402


@contextlib.contextmanager
def fresh_env():
    tmp = Path(tempfile.mkdtemp(prefix="fs_cleanup_"))
    cfg = Config(db_path=str(tmp / "c.db"), cache_dir=str(tmp / "cache"))
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
                yield {"cfg": cfg, "conn": conn, "client": TestClient(app), "tmp": tmp}
            finally:
                app.dependency_overrides.pop(deps.get_db, None)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_cleanup_summary_and_items():
    with fresh_env() as env:
        c, conn = env["client"], env["conn"]
        conn.executemany(
            "INSERT INTO files(id, path, name, kind, size, status) VALUES(?,?,?,?,?,'embedded')",
            [
                (1, "vid.mp4", "vid.mp4", "video", 80 * 1024 * 1024),
                (2, "shot.png", "shot.png", "image", 2 * 1024 * 1024),
                (3, "doc.jpg", "doc.jpg", "image", 1 * 1024 * 1024),
                (4, "big.jpg", "big.jpg", "image", 12 * 1024 * 1024),
                (5, "dup.jpg", "dup.jpg", "image", 3 * 1024 * 1024),
                (6, "clip.mp4", "clip.mp4", "video", 1 * 1024 * 1024),   # video con etichetta Screenshot
            ],
        )
        conn.executemany(
            "INSERT INTO plan(file_id, folder, context, score, is_dup, dup_of) VALUES(?,?,?,?,?,?)",
            [
                (1, "2024/01", None, None, 0, None),
                (2, "2024/01", "Screenshot", 0.9, 0, None),
                (3, "2024/01", "Documenti e scontrini", 0.9, 0, None),
                (4, "2024/01", None, None, 0, None),
                (5, "_Duplicati", None, None, 1, 4),
                (6, "2024/Screenshot", "Screenshot", 0.5, 0, None),
            ],
        )
        conn.commit()
        s = c.get("/api/cleanup/summary").json()
        assert s["duplicates"]["count"] == 1 and s["duplicates"]["groups"] == 1
        assert s["large_videos"]["count"] == 1
        assert s["screenshots"]["count"] == 1   # solo foto: il video etichettato Screenshot non c'e'
        assert s["documents"]["count"] == 1
        assert s["large_photos"]["count"] == 1
        assert s["total_files"] == 5

        r = c.get("/api/cleanup/items?category=screenshots").json()
        assert r["total"] == 1 and r["items"][0]["id"] == 2
        assert c.get("/api/cleanup/items?category=invalid").status_code == 400


def test_cleanup_trash_requires_confirm_and_removes_from_db():
    with fresh_env() as env:
        c, conn, tmp = env["client"], env["conn"], env["tmp"]
        f = tmp / "sample.jpg"
        f.write_bytes(b"123456")
        conn.execute("INSERT INTO files(id, path, name, kind, size, status) VALUES(1, ?, 'sample.jpg', 'image', 6, 'embedded')", (str(f),))
        conn.commit()
        assert c.post("/api/cleanup/trash", json={"file_ids": [1], "confirm": False}).status_code == 400
        with mock.patch.object(routes_cleanup, "_send_to_recycle_bin") as m_bin:
            res = c.post("/api/cleanup/trash", json={"file_ids": [1], "confirm": True})
            assert res.status_code == 200
            assert res.json() == {"trashed": 1, "freed_bytes": 6, "errors": [], "missing": 0}
            m_bin.assert_called_once_with(str(f))
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_cleanup_trash_master_promotes_survivor():
    with fresh_env() as env:
        c, conn, tmp = env["client"], env["conn"], env["tmp"]
        f1 = tmp / "a.jpg"
        f2 = tmp / "b.jpg"
        f3 = tmp / "c.jpg"
        for f in (f1, f2, f3):
            f.write_bytes(b"123456")
        conn.executemany(
            "INSERT INTO files(id, path, name, kind, size, status) VALUES(?,?,?,'image',6,'embedded')",
            [(1, str(f1), "a.jpg"), (2, str(f2), "b.jpg"), (3, str(f3), "c.jpg")],
        )
        conn.executemany(
            "INSERT INTO plan(file_id, folder, is_dup, dup_of) VALUES(?,?,?,?)",
            [(1, "2024/01", 0, None), (2, "_Duplicati", 1, 1), (3, "_Duplicati", 1, 1)],
        )
        conn.commit()
        with mock.patch.object(routes_cleanup, "_send_to_recycle_bin"):
            res = c.post("/api/cleanup/trash", json={"file_ids": [1], "confirm": True})
            assert res.status_code == 200 and res.json()["trashed"] == 1
        r2 = conn.execute("SELECT folder, is_dup, dup_of FROM plan WHERE file_id=2").fetchone()
        r3 = conn.execute("SELECT folder, is_dup, dup_of FROM plan WHERE file_id=3").fetchone()
        assert r2["is_dup"] == 0 and r2["dup_of"] is None and r2["folder"] == "2024/01"
        assert r3["is_dup"] == 1 and r3["dup_of"] == 2


def _seed_group(conn, tmp, kinds):
    """kinds: [(id, is_dup, dup_of)]; crea i file veri e le righe di piano."""
    for fid, is_dup, dup_of in kinds:
        f = tmp / f"f{fid}.jpg"
        f.write_bytes(b"x" * 6)
        conn.execute("INSERT INTO files(id, path, name, kind, size, status) VALUES(?,?,?,'image',6,'embedded')",
                     (fid, str(f), f.name))
        conn.execute("INSERT INTO plan(file_id, folder, is_dup, dup_of) VALUES(?,?,?,?)",
                     (fid, "2024/01" if not is_dup else "_Duplicati", is_dup, dup_of))
    conn.commit()


def test_trash_master_prefers_exact_copy_over_similar():
    """Cestinato il master, il nuovo master e' una copia ESATTA (stesso contenuto), non il quasi-duplicato con id piu'
    basso: altrimenti 'cestina copie esatte' butterebbe l'unico esemplare rimasto del contenuto originale."""
    with fresh_env() as env:
        c, conn, tmp = env["client"], env["conn"], env["tmp"]
        _seed_group(conn, tmp, [(1, 0, None), (3, 2, 1), (5, 1, 1), (6, 1, 1)])
        with mock.patch.object(routes_cleanup, "_send_to_recycle_bin", return_value=True):
            assert c.post("/api/cleanup/trash", json={"file_ids": [1], "confirm": True}).json()["trashed"] == 1
        rows = {r["file_id"]: (r["is_dup"], r["dup_of"], r["folder"])
                for r in conn.execute("SELECT file_id, is_dup, dup_of, folder FROM plan")}
        assert rows[5] == (0, None, "2024/01"), rows
        assert rows[3][1] == 5 and rows[3][0] == 2 and rows[6][1] == 5 and rows[6][0] == 1, rows


def test_trash_missing_path_is_not_counted_as_trashed():
    with fresh_env() as env:
        c, conn, tmp = env["client"], env["conn"], env["tmp"]
        conn.execute("INSERT INTO files(id, path, name, kind, size, status) VALUES(1, ?, 'gone.jpg', 'image', 99, 'embedded')",
                     (str(tmp / "gone.jpg"),))
        conn.commit()
        res = c.post("/api/cleanup/trash", json={"file_ids": [1], "confirm": True}).json()  # percorso inesistente
        assert res == {"trashed": 0, "freed_bytes": 0, "errors": [], "missing": 1}, res
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_trash_many_in_chunks_cleans_side_tables():
    """Piu' di un blocco (nessun limite di variabili SQLite), con file_override e faces_done ripuliti."""
    with fresh_env() as env:
        c, conn = env["client"], env["conn"]
        n = routes_cleanup.TRASH_CHUNK * 2 + 37
        conn.executemany("INSERT INTO files(id, path, name, kind, size, status) VALUES(?,?,?,'image',1,'embedded')",
                         [(i, f"x/{i}.jpg", f"{i}.jpg") for i in range(1, n + 1)])
        conn.execute("INSERT INTO file_override(file_id, folder) VALUES(5, 'Altro')")
        conn.execute("INSERT INTO faces_done(file_id) VALUES(5)")
        conn.commit()
        with mock.patch.object(routes_cleanup, "_send_to_recycle_bin", return_value=True):
            res = c.post("/api/cleanup/trash", json={"file_ids": list(range(1, n + 1)), "confirm": True}).json()
        assert res["trashed"] == n and res["errors"] == [], res
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM file_override").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM faces_done").fetchone()[0] == 0


def test_trash_refused_while_plan_rebuilds():
    with fresh_env() as env:
        c = env["client"]
        with mock.patch.object(routes_cleanup.jobs, "running_job", lambda name: object()):
            assert c.post("/api/cleanup/trash", json={"file_ids": [1], "confirm": True}).status_code == 409


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("test_api_cleanup superati")
