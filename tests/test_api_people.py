"""Test API persone e volti (routes_people.py + person_id in routes_files.py + faces in routes_status.py)."""
from __future__ import annotations

import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from fastapi import FastAPI
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from fotoscan import db as dbm  # noqa: E402
from fotoscan import faces  # noqa: E402
from fotoscan.config import Config  # noqa: E402
from fotoscan.server import deps, routes_files, routes_people, routes_status  # noqa: E402


def _unit_vec(seed: int, dim: int = 512) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _perturb(base: np.ndarray, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = base + rng.standard_normal(base.shape).astype(np.float32) * 0.02
    return (v / np.linalg.norm(v)).astype(np.float32)


def _make_app(cfg: Config) -> FastAPI:
    deps.get_cfg = lambda: cfg
    app = FastAPI()
    app.include_router(routes_people.router)
    app.include_router(routes_files.router)
    app.include_router(routes_status.router)
    return app


class _Server:
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

    def req(self, method: str, path: str, body=None, raw: bool = False):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                content = resp.read()
                return resp.status, (content if raw else json.loads(content))
        except urllib.error.HTTPError as e:
            content = e.read()
            return e.code, (content if raw else json.loads(content))

    def get(self, path: str, raw: bool = False):
        return self.req("GET", path, raw=raw)

    def post(self, path: str, body=None):
        return self.req("POST", path, body=body if body is not None else {})

    def patch(self, path: str, body=None):
        return self.req("PATCH", path, body=body if body is not None else {})

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


class _Tmp:
    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fs_api_people_"))
        return self.dir

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)


def _setup_catalog(tmp: Path, enable_faces: bool = True) -> Config:
    db_path = str(tmp / "catalogo.db")
    cfg = Config(db_path=db_path, cache_dir=str(tmp / "cache"), enable_faces=enable_faces, faces_min_photos=2)
    conn = dbm.connect(db_path)
    va, vb = _unit_vec(101), _unit_vec(202)
    for fid in range(1, 7):
        img_path = tmp / f"img_{fid}.jpg"
        Image.new("RGB", (240, 240), (fid * 30 % 255, 100, 150)).save(img_path)
        conn.execute(
            "INSERT INTO files(id,path,name,kind,size,date_taken,status) VALUES(?,?,?,'image',500,?,'embedded')",
            (fid, str(img_path), img_path.name, f"2023-06-0{fid}T12:00:00"),
        )
        emb = _perturb(va, fid) if fid <= 3 else _perturb(vb, fid)
        conn.execute(
            "INSERT INTO faces(file_id,bbox,emb,det_score,quality) VALUES(?,?,?,?,?)",
            (fid, json.dumps([40, 40, 160, 160]), emb.astype(np.float32).tobytes(), 0.95, 0.9),
        )
    conn.commit()
    faces.cluster_faces(conn, cfg=cfg)
    conn.close()
    return cfg


def test_people_list_disabled_and_enabled():
    with _Tmp() as tmp:
        cfg = _setup_catalog(tmp, enable_faces=False)
        srv = _Server(_make_app(cfg))
        try:
            assert srv.get("/api/people") == (200, [])
        finally:
            srv.stop()

        cfg.enable_faces = True
        srv = _Server(_make_app(cfg))
        try:
            st, ppl = srv.get("/api/people")
            assert st == 200 and len(ppl) == 2
            assert all(p["count"] == 3 for p in ppl)
            assert all(p["cover_face_id"] is not None for p in ppl)
        finally:
            srv.stop()


def test_people_rename_hide_files_and_gallery_filter():
    with _Tmp() as tmp:
        cfg = _setup_catalog(tmp, enable_faces=True)
        srv = _Server(_make_app(cfg))
        try:
            _, ppl = srv.get("/api/people")
            p1, p2 = ppl[0]["id"], ppl[1]["id"]

            # Rinomina valida + errore su nome troppo lungo + pulizia nome con stringa vuota
            st, bad = srv.patch(f"/api/people/{p1}", {"name": "A" * 100})
            assert st == 400 and "caratteri" in bad["detail"].lower()

            st, renamed = srv.patch(f"/api/people/{p1}", {"name": "Elena"})
            assert st == 200 and renamed["name"] == "Elena"

            # File della persona + filtro Galleria /api/files?person_id=
            st, pfiles = srv.get(f"/api/people/{p1}/files?limit=2&offset=0")
            assert st == 200 and pfiles["total"] == 3 and len(pfiles["items"]) == 2
            assert pfiles["items"][0]["face_id"] is not None

            st, gfiles = srv.get(f"/api/files?person_id={p1}")
            assert st == 200 and gfiles["total"] == 3

            # Nascondi p2 -> sparisce di default, ricompare con include_hidden=1
            st, hid = srv.patch(f"/api/people/{p2}", {"hidden": True})
            assert st == 200 and hid["hidden"] is True
            _, visible = srv.get("/api/people")
            assert [p["id"] for p in visible] == [p1]
            _, all_ppl = srv.get("/api/people?include_hidden=1")
            assert len(all_ppl) == 2

            # 404 su persona inesistente
            assert srv.patch("/api/people/99999", {"name": "X"})[0] == 404
            assert srv.get("/api/people/99999/files")[0] == 404

            # Cambio foto profilo (cover_face_id) valido, errore su volto di altra persona e persistenza a recompute
            _, p1_all = srv.get(f"/api/people/{p1}/files?limit=10")
            alt_face = next(it["face_id"] for it in p1_all["items"] if it["face_id"] != p1_all["person"]["cover_face_id"])
            st, upd = srv.patch(f"/api/people/{p1}", {"cover_face_id": alt_face})
            assert st == 200 and upd["cover_face_id"] == alt_face

            # Volto appartenente a p2 -> 400
            _, p2_all = srv.get(f"/api/people/{p2}/files?limit=10")
            other_face = p2_all["items"][0]["face_id"]
            assert srv.patch(f"/api/people/{p1}", {"cover_face_id": other_face})[0] == 400

            # Sopravvive a recompute_person
            conn = dbm.connect(cfg.db_path)
            try:
                faces.recompute_person(conn, p1)
                conn.commit()
                assert conn.execute("SELECT cover_face_id FROM persons WHERE id=?", (p1,)).fetchone()[0] == alt_face
            finally:
                conn.close()
        finally:
            srv.stop()


def test_merge_assign_exclude_restore_and_crop():
    with _Tmp() as tmp:
        cfg = _setup_catalog(tmp, enable_faces=True)
        srv = _Server(_make_app(cfg))
        try:
            _, ppl = srv.get("/api/people")
            p1, p2 = ppl[0]["id"], ppl[1]["id"]

            # Face crop JPEG
            face_id = ppl[0]["cover_face_id"]
            st, raw_bytes = srv.get(f"/api/faces/{face_id}/crop?size=128", raw=True)
            assert st == 200 and raw_bytes[:2] == b"\xff\xd8"  # JPEG magic bytes
            assert srv.get("/api/faces/99999/crop")[0] == 404

            # Exclude + restore
            st, ex = srv.post(f"/api/faces/{face_id}/exclude")
            assert st == 200 and ex["excluded"] is True
            assert srv.get(f"/api/files?person_id={p1}")[1]["total"] == 2

            st, res = srv.post(f"/api/faces/{face_id}/restore")
            assert st == 200 and res["excluded"] is False

            # Assign back to p1
            st, asg = srv.post("/api/faces/assign", {"face_ids": [face_id], "person_id": p1})
            assert st == 200 and asg["updated"] == 1
            assert srv.get(f"/api/files?person_id={p1}")[1]["total"] == 3

            # Merge p2 into p1
            st, merged = srv.post("/api/people/merge", {"ids": [p1, p2], "into": p1})
            assert st == 200 and merged["into"] == p1
            _, after_ppl = srv.get("/api/people")
            assert len(after_ppl) == 1 and after_ppl[0]["count"] == 6

            # Status espone blocco faces
            st, status = srv.get("/api/status")
            assert st == 200 and "faces" in status and "available" in status["faces"]
        finally:
            srv.stop()


def test_people_error_cases_and_reset_scan():
    """Verifica gli errori 400/404, GET /api/people/job e POST /api/people/reset-scan (preserva nomi/locked/excluded)."""
    with _Tmp() as tmp:
        cfg = _setup_catalog(tmp, enable_faces=True)
        srv = _Server(_make_app(cfg))
        try:
            _, ppl = srv.get("/api/people")
            p1, p2 = ppl[0]["id"], ppl[1]["id"]
            f1 = ppl[0]["cover_face_id"]
            f2 = ppl[1]["cover_face_id"]

            # 400 / 404 su merge, assign, exclude, restore, patch
            assert srv.post("/api/people/merge", {"ids": [p1], "into": p1})[0] == 400
            assert srv.post("/api/people/merge", {"ids": [p1, p2], "into": 99999})[0] == 404
            assert srv.post("/api/faces/assign", {"face_ids": [], "person_id": p1})[0] == 400
            assert srv.post("/api/faces/assign", {"face_ids": [f1], "person_id": 99999})[0] == 404
            assert srv.post("/api/faces/99999/exclude")[0] == 404
            assert srv.post("/api/faces/99999/restore")[0] == 404
            assert srv.patch(f"/api/people/{p1}", {})[0] == 400
            assert srv.patch(f"/api/people/{p1}", {"hidden": "si"})[0] == 400

            # GET /api/people/job senza job attivo
            st, job_info = srv.get("/api/people/job")
            assert st == 200 and job_info["job"] is None

            # 409 quando e' in corso un'indicizzazione
            from fotoscan.server import routes_index

            orig_cur = routes_index._current_run
            routes_index._current_run = lambda: object()
            try:
                assert srv.post("/api/people/scan", {})[0] == 409
                assert srv.post("/api/people/recluster", {})[0] == 409
                assert srv.post("/api/people/reset-scan", {})[0] == 409
            finally:
                routes_index._current_run = orig_cur

            # Rinomina p1, blocca f1 su p1, escludi f2, segna faces_done e poi chiama reset-scan
            assert srv.patch(f"/api/people/{p1}", {"name": "Anna"})[0] == 200
            assert srv.post("/api/faces/assign", {"face_ids": [f1], "person_id": p1})[0] == 200
            assert srv.post(f"/api/faces/{f2}/exclude")[0] == 200

            conn = dbm.connect(cfg.db_path)
            with conn:
                for fid in range(1, 7):
                    conn.execute("INSERT OR IGNORE INTO faces_done(file_id) VALUES(?)", (fid,))
            conn.close()

            st, rst = srv.post("/api/people/reset-scan", {})
            assert st == 200 and rst["ok"] is True and rst["removed_faces"] == 4

            conn = dbm.connect(cfg.db_path)
            try:
                assert conn.execute("SELECT COUNT(*) FROM faces_done").fetchone()[0] == 0
                assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 2
                row_p1 = conn.execute("SELECT name, n FROM persons WHERE id=?", (p1,)).fetchone()
                assert row_p1 is not None and row_p1["name"] == "Anna" and row_p1["n"] == 1
                assert conn.execute("SELECT excluded FROM faces WHERE id=?", (f2,)).fetchone()[0] == 1
            finally:
                conn.close()
        finally:
            srv.stop()


def test_settings_faces_expert_validation():
    """Valida faces_det_size e faces_assign_threshold via routes_status._validate e GET /api/settings/defaults."""
    valid, errs = routes_status._validate({
        "faces_det_size": 800,
        "faces_assign_threshold": 0.58,
        "faces_threshold": 0.48,
        "faces_min_photos": 4,
    })
    assert not errs
    assert valid == {
        "faces_det_size": 800,
        "faces_assign_threshold": 0.58,
        "faces_threshold": 0.48,
        "faces_min_photos": 4,
    }

    _, bad = routes_status._validate({"faces_det_size": 500, "faces_assign_threshold": 1.5})
    assert "faces_det_size" in bad and "faces_assign_threshold" in bad

    with _Tmp() as tmp:
        cfg = _setup_catalog(tmp, enable_faces=True)
        srv = _Server(_make_app(cfg))
        try:
            st, defs = srv.get("/api/settings/defaults")
            assert st == 200
            assert defs["faces_det_size"] == 640
            assert defs["faces_assign_threshold"] == 0.52
            assert defs["faces_threshold"] == 0.50
            assert defs["faces_min_photos"] == 3
        finally:
            srv.stop()


def test_people_merge_suggestions():
    """GET /api/people/suggestions propone coppie con centroide simile e ignora quelle distanti o nascoste."""
    with _Tmp() as tmp:
        cfg = _setup_catalog(tmp, enable_faces=True)
        # Inserisce una terza persona con centroide vicino alla persona 1 (coseno ~0.60)
        conn = dbm.connect(cfg.db_path)
        try:
            p1_row = conn.execute("SELECT id, centroid FROM persons ORDER BY id LIMIT 1").fetchone()
            v1 = dbm.from_blob(p1_row["centroid"])
            v1 = v1 / np.linalg.norm(v1)
            orth = _unit_vec(777)
            orth = orth - float(orth @ v1) * v1
            orth = orth / np.linalg.norm(orth)
            v3 = (0.60 * v1 + 0.80 * orth).astype(np.float32)
            v3 = v3 / np.linalg.norm(v3)
            with conn:
                cur = conn.execute("INSERT INTO persons(name, created, centroid, n, hidden) VALUES(NULL, '2026-01-01', ?, 1, 0)", (v3.tobytes(),))
                p3 = int(cur.lastrowid)
                img7 = tmp / "img_7.jpg"
                Image.new("RGB", (200, 200), (80, 90, 100)).save(img7)
                conn.execute("INSERT INTO files(id,path,name,kind,size,status) VALUES(7,?,?,'image',500,'embedded')", (str(img7), img7.name))
                conn.execute(
                    "INSERT INTO faces(file_id,bbox,emb,person_id,det_score,quality) VALUES(7,'[10,10,90,90]',?,?,0.95,0.9)",
                    (v3.tobytes(), p3),
                )
        finally:
            conn.close()

        srv = _Server(_make_app(cfg))
        try:
            st, sugg = srv.get("/api/people/suggestions?min_sim=0.45")
            assert st == 200 and len(sugg) == 1
            pair_ids = {sugg[0]["a"]["id"], sugg[0]["b"]["id"]}
            assert pair_ids == {int(p1_row["id"]), p3}
            assert 0.55 <= sugg[0]["similarity"] <= 0.65
        finally:
            srv.stop()


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} test superati")
