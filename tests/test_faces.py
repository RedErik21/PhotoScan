"""Test unitari per fotoscan/faces.py e migrazione schema db.py (nessun modello reale scaricato, DB isolato)."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fotoscan import db as dbm  # noqa: E402
from fotoscan import faces  # noqa: E402
from fotoscan.config import Config  # noqa: E402


def _unit_vec(seed: int, dim: int = 512) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _perturb(base: np.ndarray, seed: int, noise: float = 0.02) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = base + rng.standard_normal(base.shape).astype(np.float32) * noise
    return (v / np.linalg.norm(v)).astype(np.float32)


def _make_db(tmp: Path) -> tuple[sqlite3.Connection, Config]:
    db_path = str(tmp / "test_faces.db")
    cfg = Config(db_path=db_path, cache_dir=str(tmp / "cache"), enable_faces=True, faces_threshold=0.50, faces_min_photos=3)
    conn = dbm.connect(db_path)
    return conn, cfg


def _add_file(conn: sqlite3.Connection, fid: int, path: str = "") -> None:
    p = path or f"D:/fake/{fid}.jpg"
    conn.execute(
        "INSERT INTO files(id,path,name,kind,size,status) VALUES(?,?,?,'image',100,'embedded')",
        (fid, p, f"{fid}.jpg"),
    )


def _pack(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def _add_face(
    conn: sqlite3.Connection,
    file_id: int,
    emb: np.ndarray,
    *,
    quality: float = 0.9,
    det_score: float = 0.95,
    person_id: int | None = None,
    locked: int = 0,
    excluded: int = 0,
) -> int:
    cur = conn.execute(
        "INSERT INTO faces(file_id,bbox,emb,person_id,locked,excluded,det_score,quality) VALUES(?,?,?,?,?,?,?,?)",
        (file_id, json.dumps([10, 10, 120, 120]), _pack(emb), person_id, locked, excluded, det_score, quality),
    )
    return int(cur.lastrowid)


# ------------------------------------------------------------------ migrazione schema
def test_migrate_legacy_faces_schema_to_persons():
    """Un catalogo vecchio con la sola colonna faces.cluster deve migrare senza errori e creare persons."""
    with tempfile.TemporaryDirectory(prefix="fs_faces_mig_") as td:
        db_path = str(Path(td) / "legacy.db")
        raw = sqlite3.connect(db_path)
        try:
            raw.executescript("""
                CREATE TABLE files (
                    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                    kind TEXT NOT NULL, size INTEGER, mtime REAL, hash TEXT,
                    date_taken TEXT, date_source TEXT, data_incerta INTEGER DEFAULT 0,
                    lat REAL, lon REAL, width INTEGER, height INTEGER, duration REAL,
                    status TEXT DEFAULT 'new', error TEXT
                );
                CREATE TABLE faces (
                    id INTEGER PRIMARY KEY,
                    file_id INTEGER NOT NULL,
                    bbox TEXT,
                    emb BLOB,
                    cluster INTEGER
                );
            """)
            v0 = _unit_vec(10)
            v1 = _unit_vec(20)
            for fid in (1, 2, 3):
                raw.execute("INSERT INTO files(id,path,name,kind,status) VALUES(?,?,?,'image','embedded')", (fid, f"D:/{fid}.jpg", f"{fid}.jpg"))
            raw.execute("INSERT INTO faces(file_id,bbox,emb,cluster) VALUES(1,'[0,0,50,50]',?,0)", (_pack(v0),))
            raw.execute("INSERT INTO faces(file_id,bbox,emb,cluster) VALUES(2,'[0,0,50,50]',?,0)", (_pack(v0),))
            raw.execute("INSERT INTO faces(file_id,bbox,emb,cluster) VALUES(3,'[0,0,50,50]',?,1)", (_pack(v1),))
            raw.commit()
        finally:
            raw.close()

        conn = dbm.connect(db_path)
        try:
            persons = conn.execute("SELECT * FROM persons ORDER BY n DESC").fetchall()
            assert len(persons) == 2
            assert persons[0]["n"] == 2
            assert persons[1]["n"] == 1
            f_rows = conn.execute("SELECT file_id, person_id, locked, excluded FROM faces ORDER BY file_id").fetchall()
            assert all(r["person_id"] is not None for r in f_rows)
            assert f_rows[0]["person_id"] == f_rows[1]["person_id"]
            assert f_rows[0]["person_id"] != f_rows[2]["person_id"]
            # Seconda apertura: migrazione idempotente
        finally:
            conn.close()
        conn2 = dbm.connect(db_path)
        try:
            assert conn2.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 2
        finally:
            conn2.close()


# ------------------------------------------------------------------ clustering stabile
def test_cluster_faces_order_independent_and_idempotent():
    """Tre identita' sintetiche (4 foto ciascuna) + 2 isolati: stesso risultato indipendentemente dall'ordine."""
    base_a, base_b, base_c = _unit_vec(1), _unit_vec(2), _unit_vec(3)
    noise_1, noise_2 = _unit_vec(99), _unit_vec(100)

    def run_with_order(order: list[int]):
        with tempfile.TemporaryDirectory(prefix="fs_faces_ord_") as td:
            conn, cfg = _make_db(Path(td))
            try:
                samples = []
                for i in range(4):
                    samples.append(("A", _perturb(base_a, 10 + i)))
                    samples.append(("B", _perturb(base_b, 20 + i)))
                    samples.append(("C", _perturb(base_c, 30 + i)))
                samples.append(("N1", noise_1))
                samples.append(("N2", noise_2))

                fid_to_label = {}
                for idx in order:
                    lbl, emb = samples[idx]
                    fid = idx + 1
                    _add_file(conn, fid)
                    _add_face(conn, fid, emb)
                    fid_to_label[fid] = lbl
                conn.commit()

                n1 = faces.cluster_faces(conn, cfg=cfg)
                assert n1 == 3

                # Idempotenza: rilanciare cluster_faces mantiene gli stessi ID persona
                before = {
                    r["file_id"]: r["person_id"]
                    for r in conn.execute("SELECT file_id, person_id FROM faces").fetchall()
                }
                n2 = faces.cluster_faces(conn, cfg=cfg)
                assert n2 == 3
                after = {
                    r["file_id"]: r["person_id"]
                    for r in conn.execute("SELECT file_id, person_id FROM faces").fetchall()
                }
                assert before == after

                # Raggruppamento per etichetta reale
                groups: dict[int, set[str]] = {}
                for fid, pid in after.items():
                    if pid is not None:
                        groups.setdefault(pid, set()).add(fid_to_label[fid])
                assert sorted(list(s) for s in groups.values()) == [["A"], ["B"], ["C"]]
                # Gli isolati (< min_photos=3) restano non assegnati
                for fid, lbl in fid_to_label.items():
                    if lbl.startswith("N"):
                        assert after[fid] is None
            finally:
                conn.close()

    run_with_order(list(range(14)))
    run_with_order(list(reversed(range(14))))


def test_incremental_clustering_preserves_named_locked_and_excluded():
    """Nuove foto si agganciano alle persone esistenti; locked, excluded e nomi restano intatti."""
    base_a, base_b = _unit_vec(11), _unit_vec(22)
    with tempfile.TemporaryDirectory(prefix="fs_faces_inc_") as td:
        conn, cfg = _make_db(Path(td))
        try:
            for fid in range(1, 4):
                _add_file(conn, fid)
                _add_face(conn, fid, _perturb(base_a, 100 + fid))
            for fid in range(4, 7):
                _add_file(conn, fid)
                _add_face(conn, fid, _perturb(base_b, 200 + fid))
            conn.commit()

            assert faces.cluster_faces(conn, cfg=cfg) == 2
            pid_a = conn.execute("SELECT person_id FROM faces WHERE file_id=1").fetchone()[0]
            pid_b = conn.execute("SELECT person_id FROM faces WHERE file_id=4").fetchone()[0]
            conn.execute("UPDATE persons SET name='Marco' WHERE id=?", (pid_a,))
            # Blocca un volto manualmente su Marco anche se assomiglia a B
            _add_file(conn, 7)
            locked_fid = _add_face(conn, 7, _perturb(base_b, 299), person_id=pid_a, locked=1)
            # Esclude un volto di A
            _add_file(conn, 8)
            excl_fid = _add_face(conn, 8, _perturb(base_a, 199), excluded=1)
            # Aggiunge una nuova foto non assegnata di Marco
            _add_file(conn, 9)
            new_a_fid = _add_face(conn, 9, _perturb(base_a, 155))
            conn.commit()

            faces.cluster_faces(conn, cfg=cfg)

            # Marco mantiene lo stesso id e nome, e guadagna il volto 9
            row_a = conn.execute("SELECT id, name FROM persons WHERE id=?", (pid_a,)).fetchone()
            assert row_a["name"] == "Marco"
            assert conn.execute("SELECT person_id FROM faces WHERE id=?", (new_a_fid,)).fetchone()[0] == pid_a
            # Il volto bloccato resta su Marco
            assert conn.execute("SELECT person_id, locked FROM faces WHERE id=?", (locked_fid,)).fetchone()[:2] == (pid_a, 1)
            # Il volto escluso resta escluso e senza persona
            assert conn.execute("SELECT person_id, excluded FROM faces WHERE id=?", (excl_fid,)).fetchone()[:2] == (None, 1)
            assert pid_b != pid_a
        finally:
            conn.close()


def test_merge_assign_and_exclude_helpers():
    """merge_persons, assign_faces e set_face_excluded aggiornano volti e centroidi in modo coerente."""
    base_a, base_b = _unit_vec(31), _unit_vec(32)
    with tempfile.TemporaryDirectory(prefix="fs_faces_ops_") as td:
        conn, cfg = _make_db(Path(td))
        try:
            for fid in range(1, 4):
                _add_file(conn, fid)
                _add_face(conn, fid, _perturb(base_a, 300 + fid))
            for fid in range(4, 7):
                _add_file(conn, fid)
                _add_face(conn, fid, _perturb(base_b, 400 + fid))
            conn.commit()
            assert faces.cluster_faces(conn, cfg=cfg) == 2

            pid_a = conn.execute("SELECT person_id FROM faces WHERE file_id=1").fetchone()[0]
            pid_b = conn.execute("SELECT person_id FROM faces WHERE file_id=4").fetchone()[0]
            conn.execute("UPDATE persons SET name='Giulia' WHERE id=?", (pid_a,))
            conn.commit()

            # Merge B -> A
            faces.merge_persons(conn, [pid_a, pid_b], pid_a)
            conn.commit()
            assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 1
            assert conn.execute("SELECT n FROM persons WHERE id=?", (pid_a,)).fetchone()[0] == 6

            # Unassign 1 face -> locked=1, person_id=NULL
            face_1 = conn.execute("SELECT id FROM faces WHERE file_id=1").fetchone()[0]
            faces.assign_faces(conn, [face_1], None)
            conn.commit()
            r1 = conn.execute("SELECT person_id, locked FROM faces WHERE id=?", (face_1,)).fetchone()
            assert r1["person_id"] is None and r1["locked"] == 1
            assert conn.execute("SELECT n FROM persons WHERE id=?", (pid_a,)).fetchone()[0] == 5

            # Exclude and restore another face
            face_2 = conn.execute("SELECT id FROM faces WHERE file_id=2").fetchone()[0]
            faces.set_face_excluded(conn, face_2, True)
            conn.commit()
            r2 = conn.execute("SELECT person_id, excluded FROM faces WHERE id=?", (face_2,)).fetchone()
            assert r2["person_id"] is None and r2["excluded"] == 1
            assert conn.execute("SELECT n FROM persons WHERE id=?", (pid_a,)).fetchone()[0] == 4

            faces.set_face_excluded(conn, face_2, False)
            conn.commit()
            assert conn.execute("SELECT excluded FROM faces WHERE id=?", (face_2,)).fetchone()[0] == 0
        finally:
            conn.close()


def test_detect_faces_with_fake_detector():
    """detect_faces salva i volti, imposta faces_model e salta le immagini gia' processate al giro successivo."""
    v = _unit_vec(77)
    calls = []

    class _FakeDetector:
        def detect(self, img):
            calls.append(img.size)
            return [
                faces.DetectedFace(
                    bbox=[10.0, 10.0, 90.0, 90.0],
                    emb=v,
                    det_score=0.96,
                    quality=0.9,
                    landmarks=[[30, 35], [70, 35], [50, 55], [35, 75], [65, 75]],
                )
            ]

    fake_detector = _FakeDetector()

    with tempfile.TemporaryDirectory(prefix="fs_faces_det_") as td:
        tmp = Path(td)
        img1 = tmp / "p1.jpg"
        img2 = tmp / "p2.jpg"
        Image.new("RGB", (200, 200), (120, 140, 160)).save(img1)
        Image.new("RGB", (200, 200), (160, 140, 120)).save(img2)

        conn, cfg = _make_db(tmp)
        try:
            _add_file(conn, 1, str(img1))
            _add_file(conn, 2, str(img2))
            conn.commit()

            found = faces.detect_faces(conn, cfg=cfg, detector=fake_detector)
            assert found == 2
            assert len(calls) == 2
            assert dbm.get_meta(conn, "faces_model") == faces.FACES_MODEL_ID
            assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 2

            # Secondo passaggio: le 2 immagini sono gia' state analizzate -> 0 chiamate al detector
            found2 = faces.detect_faces(conn, cfg=cfg, detector=fake_detector)
            assert found2 == 0
            assert len(calls) == 2
        finally:
            conn.close()


def test_detect_faces_sliding_window_batch_and_stop():
    """A1+A2: prefetch scorrevole con batching cross-foto ArcFace, ordine preservato e stop/riprendi sicuro."""
    v = _unit_vec(88)
    box_calls: list[int] = []
    embed_batch_sizes: list[int] = []

    class _FakeBatchedEngine:
        def detect_boxes(self, img):
            # Il canale R codifica l'indice immagine per verificare l'ordine
            r_val = int(img.getpixel((0, 0))[0])
            box_calls.append(r_val)
            crop = np.full((112, 112, 3), r_val, dtype=np.uint8)
            return [
                faces.FaceCandidate(
                    bbox=[10.0, 10.0, 90.0, 90.0],
                    crop=crop,
                    det_score=0.95,
                    quality=0.88,
                    landmarks=[[30, 35], [70, 35], [50, 55], [35, 75], [65, 75]],
                )
            ]

        def embed_crops(self, crops, batch_size=64):
            embed_batch_sizes.append(len(crops))
            return np.stack([v for _ in crops]).astype(np.float32)

    with tempfile.TemporaryDirectory(prefix="fs_faces_batch_") as td:
        tmp = Path(td)
        conn, cfg = _make_db(tmp)
        try:
            for fid in range(1, 13):
                p = tmp / f"b_{fid}.png"
                Image.new("RGB", (120, 120), (fid * 10, 50, 50)).save(p)
                _add_file(conn, fid, str(p))
            conn.commit()

            # Stop dopo 4 immagini: deve salvare le 4 elaborate e riprendere dalle 8 rimanenti
            engine = _FakeBatchedEngine()
            n_first = faces.detect_faces(
                conn,
                cfg=cfg,
                detector=engine,
                should_stop=lambda: len(box_calls) >= 4,
            )
            assert n_first == 4
            assert box_calls == [10, 20, 30, 40]
            assert embed_batch_sizes == [4]
            assert conn.execute("SELECT COUNT(*) FROM faces_done").fetchone()[0] == 4

            # Ripresa: elabora le 8 rimanenti in un solo batch ArcFace
            n_rest = faces.detect_faces(conn, cfg=cfg, detector=engine)
            assert n_rest == 8
            assert box_calls == [i * 10 for i in range(1, 13)]
            assert embed_batch_sizes == [4, 8]
            assert conn.execute("SELECT COUNT(*) FROM faces_done").fetchone()[0] == 12
            assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 12
        finally:
            conn.close()


def test_detect_faces_skips_overlapping_locked_face():
    """Dopo reset-scan, detect_faces non duplica un volto locked/excluded gia' presente sulla stessa foto."""
    v = _unit_vec(91)

    class _FakeTwoFaces:
        def detect(self, _img):
            return [
                faces.DetectedFace(bbox=[12.0, 12.0, 88.0, 88.0], emb=v, det_score=0.95, quality=0.9),
                faces.DetectedFace(bbox=[120.0, 120.0, 190.0, 190.0], emb=v, det_score=0.95, quality=0.9),
            ]

    with tempfile.TemporaryDirectory(prefix="fs_faces_ov_") as td:
        tmp = Path(td)
        p = tmp / "p1.jpg"
        Image.new("RGB", (220, 220), (100, 100, 100)).save(p)
        conn, cfg = _make_db(tmp)
        try:
            _add_file(conn, 1, str(p))
            conn.execute(
                "INSERT INTO faces(file_id,bbox,emb,locked,excluded,det_score,quality) VALUES(1,?,?,1,0,0.95,0.9)",
                (json.dumps([10.0, 10.0, 90.0, 90.0]), _pack(v)),
            )
            conn.commit()
            added = faces.detect_faces(conn, cfg=cfg, detector=_FakeTwoFaces())
            assert added == 1
            assert conn.execute("SELECT COUNT(*) FROM faces WHERE file_id=1").fetchone()[0] == 2
        finally:
            conn.close()


def test_verify_model_file_sha256():
    """verify_model_file controlla l'impronta SHA-256 reale e segnala file mancanti o alterati."""
    import hashlib

    with tempfile.TemporaryDirectory(prefix="fs_faces_sha_") as td:
        p = Path(td) / "dummy.onnx"
        payload = b"fotoscan-onnx-test-payload"
        p.write_bytes(payload)
        good_sha = hashlib.sha256(payload).hexdigest()
        assert faces.verify_model_file(p, good_sha) is True
        assert faces.verify_model_file(p, "0" * 64) is False
        assert faces.verify_model_file(Path(td) / "missing.onnx", good_sha) is False


def test_faces_assign_threshold_respected():
    """Config.faces_assign_threshold governa l'aggancio di nuovi volti alle persone gia' esistenti."""
    base_a = _unit_vec(55)
    orth = _unit_vec(56)
    orth = orth - float(orth @ base_a) * base_a
    orth = orth / np.linalg.norm(orth)
    # Vettore con coseno esatto ~0.60 rispetto a base_a
    cand = (0.60 * base_a + 0.80 * orth).astype(np.float32)
    cand = cand / np.linalg.norm(cand)

    with tempfile.TemporaryDirectory(prefix="fs_faces_athr_") as td:
        conn, cfg = _make_db(Path(td))
        try:
            for fid in (1, 2, 3):
                _add_file(conn, fid)
                _add_face(conn, fid, _perturb(base_a, 500 + fid))
            conn.commit()
            assert faces.cluster_faces(conn, cfg=cfg) == 1
            pid_a = conn.execute("SELECT person_id FROM faces WHERE file_id=1").fetchone()[0]

            _add_file(conn, 4)
            f4 = _add_face(conn, 4, cand)
            conn.commit()

            # Con soglia 0.70 il volto 4 non si aggancia
            cfg.faces_assign_threshold = 0.70
            faces.cluster_faces(conn, cfg=cfg)
            assert conn.execute("SELECT person_id FROM faces WHERE id=?", (f4,)).fetchone()[0] is None

            # Con soglia 0.55 il volto 4 si aggancia a pid_a
            cfg.faces_assign_threshold = 0.55
            faces.cluster_faces(conn, cfg=cfg)
            assert conn.execute("SELECT person_id FROM faces WHERE id=?", (f4,)).fetchone()[0] == pid_a
        finally:
            conn.close()


def test_real_model_smoke_if_present():
    """Smoke test opzionale sui pesi ONNX reali (saltato automaticamente se i pesi non sono in cache)."""
    real_cfg = Config()
    if not faces.models_present(real_cfg):
        return
    engine = faces.OnnxFaceEngine(real_cfg, det_size=480)
    blank = Image.new("RGB", (240, 240), (128, 128, 128))
    assert engine.detect_boxes(blank) == []
    assert engine.embed_crops([]).shape == (0, 512)
    assert engine.detect(blank) == []


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} test superati")
