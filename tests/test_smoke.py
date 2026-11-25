"""Smoke test senza modello: date da nome file, EXIF, piano e apply su cartelle temporanee.

Esecuzione: `python -m pytest tests` oppure `python tests/test_smoke.py` (senza pytest).
Gli embedding sono finti: la cache delle etichette e' precalcolata con vettori one-hot.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fotoscan import db as dbm  # noqa: E402
from fotoscan import organizer  # noqa: E402
from fotoscan.config import Config  # noqa: E402
from fotoscan.embedder import label_vectors  # noqa: E402
from fotoscan.scanner_metadata import _parse_exif_date, date_from_filename, read_image_meta  # noqa: E402


# ------------------------------------------------------------ date e EXIF
# Regole di raggruppamento "piccole" (valori di prima): i cataloghi di prova hanno pochi file,
# con i predefiniti (evento/contesto >= 10 foto) finirebbero tutti in Varie.
_SMALL = dict(min_context_size=1, min_event_size=3, event_gap_hours=6.0, max_event_days=3.0,
              event_burst_ratio=0.0, event_names="data", context_period="mese")

def test_date_from_filename():
    assert date_from_filename("IMG-20200512-WA0001.jpg") == "2020-05-12T00:00:00"
    assert date_from_filename("20190704_143022.jpg") == "2019-07-04T14:30:22"
    assert date_from_filename("copia_di_a0.jpg") is None
    assert date_from_filename("foto_19000101.jpg") is None  # anno fuori intervallo


def test_parse_exif_date_tolerates_double_space():
    assert _parse_exif_date("2021:08:10  10:10:00") == "2021-08-10T10:10:00"
    assert _parse_exif_date("2021:12:20 18:00:00") == "2021-12-20T18:00:00"
    assert _parse_exif_date("0000:00:00 00:00:00") is None


def test_read_image_meta_exif(tmp_path: Path):
    ex = Image.Exif()
    ex.get_ifd(0x8769)[36867] = "2021:08:10  10:10:00"  # DateTimeOriginal con doppio spazio
    p = tmp_path / "exif.jpg"
    Image.new("RGB", (32, 24), (30, 120, 200)).save(p, exif=ex)
    meta = read_image_meta(str(p))
    assert meta.date == "2021-08-10T10:10:00" and meta.date_source == "exif"
    assert (meta.width, meta.height) == (32, 24)


# ------------------------------------------------------- piano e apply
def _make_catalog(tmp: Path, src: Path, out: Path):
    """Catalogo con 4 file (dimensione-uguale incluse), embedding one-hot, nessun modello."""
    cfg = Config(source_dir=str(src), output_dir=str(out), db_path=str(tmp / "c.db"), cache_dir=str(tmp / "cache"),
                 **_SMALL)
    conn = dbm.connect(cfg.db_path)
    n = len(cfg.labels)
    beach = 0  # "Mare e spiaggia"
    dbm.set_meta(conn, "logit_scale", "100")
    dbm.set_meta(conn, "label_model", cfg.model_name)
    for i, l in enumerate(cfg.labels):
        v = np.zeros(n, dtype=np.float32)
        v[i] = 1.0
        conn.execute("INSERT INTO label_cache(prompt, vec) VALUES(?,?)", (l["prompt"], dbm.to_blob(v)))
    for p in cfg.null_prompts:  # classi nulle: vettore zero = similarita' neutra
        conn.execute("INSERT INTO label_cache(prompt, vec) VALUES(?,?)", (p, dbm.to_blob(np.zeros(n, np.float32))))

    def add(path: Path, name: str, h: str, date: str, incerta: int, lab: int = beach):
        st = path.stat()
        cur = conn.execute(
            "INSERT INTO files(path,name,kind,size,mtime,hash,date_taken,date_source,data_incerta,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,'embedded')",
            (str(path), name, "image", st.st_size, st.st_mtime, h, date, "exif", incerta))
        v = np.zeros(n, dtype=np.float32)
        v[lab] = 1.0
        conn.execute("INSERT INTO embeddings(file_id, vec) VALUES(?,?)", (cur.lastrowid, dbm.to_blob(v)))

    # stessa dimensione, contenuto diverso: il secondo non deve essere scambiato per il primo
    (src).mkdir(parents=True, exist_ok=True)
    (src / "sub").mkdir(exist_ok=True)
    a = src / "x.jpg"
    a.write_bytes(b"A" * 100)
    b = src / "sub" / "x.jpg"
    b.write_bytes(b"B" * 100)
    dup = src / "sub" / "zz_copia_di_x.jpg"
    dup.write_bytes(b"A" * 100)
    und = src / "sub" / "senza_data.jpg"
    und.write_bytes(b"C" * 50)
    add(a, "x.jpg", "hash_a", "2021-08-10T10:00:00", 0)
    add(b, "x.jpg", "hash_b", "2021-08-11T10:00:00", 0)
    add(dup, "zz_copia_di_x.jpg", "hash_a", "2021-08-10T10:00:00", 0)
    add(und, "senza_data.jpg", "hash_c", "2022-03-01T09:00:00", 1)
    return cfg, conn


def test_plan_and_apply(tmp_path: Path):
    src, out = tmp_path / "src", tmp_path / "out"
    cfg, conn = _make_catalog(tmp_path, src, out)
    emb_stats = organizer.build_plan(conn, cfg, embedder=None)  # nessun modello: cache etichette gia' piena
    assert emb_stats["duplicati"] == 1
    rows = {r["name"] + "|" + str(r["file_id"]): r["folder"] for r in conn.execute("SELECT f.name, f.id AS file_id, p.folder "
                                                                                   "FROM plan p JOIN files f ON f.id=p.file_id")}
    folders = sorted(rows.values())
    assert "2021/08 - Mare e spiaggia" in folders
    assert "_Duplicati" in folders
    assert "Data incerta/2022 - Mare e spiaggia" in folders

    # simulazione: nessun file creato
    sim = organizer.apply_plan(conn, cfg, apply=False)
    assert sim["totale"] == 4 and not out.exists()

    # apply reale: hard link, collisione di nome gestita, rerun senza duplicati
    res = organizer.apply_plan(conn, cfg, apply=True)
    assert res["errori"] == 0 and res["link"] == 4, res
    folder = out / "2021" / "08 - Mare e spiaggia"
    files = sorted(folder.iterdir())
    assert len(files) == 2, files  # x.jpg + x_<id>.jpg (ordine di apply non garantito)
    assert sorted(p.read_bytes() for p in files) == [b"A" * 100, b"B" * 100]  # stessa dimensione, contenuti diversi
    a_link = next(p for p in files if p.read_bytes() == b"A" * 100)
    assert os.stat(a_link).st_ino == os.stat(src / "x.jpg").st_ino  # hard link vero
    assert os.stat(a_link).st_nlink >= 2
    assert (out / "_Duplicati" / "zz_copia_di_x.jpg").exists()
    again = organizer.apply_plan(conn, cfg, apply=True)
    assert again["link"] == 0 and again["gia_presenti"] == 4, again
    # la sorgente resta intatta
    assert (src / "x.jpg").read_bytes() == b"A" * 100 and (src / "sub" / "x.jpg").read_bytes() == b"B" * 100
    conn.close()


def test_output_inside_source_is_rejected(tmp_path: Path):
    src = tmp_path / "src"
    cfg, conn = _make_catalog(tmp_path, src, src / "dentro")
    try:
        organizer.apply_plan(conn, cfg, apply=True)
    except ValueError:
        pass
    else:
        raise AssertionError("output dentro la sorgente non rifiutato")
    assert not (src / "dentro").exists()
    cfg.output_dir = str(tmp_path)  # contenitore della sorgente
    try:
        organizer.apply_plan(conn, cfg, apply=False)
    except ValueError:
        pass
    else:
        raise AssertionError("output che contiene la sorgente non rifiutato")
    conn.close()


def test_label_vectors_use_cache_without_model(tmp_path: Path):
    cfg = Config(db_path=str(tmp_path / "c.db"), cache_dir=str(tmp_path / "cache"))
    conn = dbm.connect(cfg.db_path)
    dbm.set_meta(conn, "label_model", cfg.model_name)
    prompts = [l["prompt"] for l in cfg.labels]
    for p in prompts:
        conn.execute("INSERT INTO label_cache(prompt, vec) VALUES(?,?)", (p, dbm.to_blob(np.ones(4, np.float32))))
    conn.commit()
    assert label_vectors(conn, prompts, None, cfg).shape == (len(prompts), 4)
    conn.close()


# ------------------------------------------------ quasi-duplicati
def _near_catalog(tmp: Path, specs: list[dict], **cfg_kw):
    """specs: name, dir, kind, hash, date, date_source, vec=(x,y), w, h, size.
    Embedding = vec sulle prime due dimensioni (il resto zero); la cache etichette e' one-hot."""
    src = tmp / "src"
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = Config(source_dir=str(src), output_dir=str(tmp / "out"), db_path=str(tmp / "c.db"),
                 cache_dir=str(tmp / "cache"), **{**_SMALL, **cfg_kw})
    conn = dbm.connect(cfg.db_path)
    n = len(cfg.labels)
    dbm.set_meta(conn, "logit_scale", "100")
    dbm.set_meta(conn, "label_model", cfg.model_name)
    for i, l in enumerate(cfg.labels):
        v = np.zeros(n, dtype=np.float32)
        v[i] = 1.0
        conn.execute("INSERT INTO label_cache(prompt, vec) VALUES(?,?)", (l["prompt"], dbm.to_blob(v)))
    for p in cfg.null_prompts:
        conn.execute("INSERT INTO label_cache(prompt, vec) VALUES(?,?)", (p, dbm.to_blob(np.zeros(n, np.float32))))
    ids: dict[str, int] = {}
    for s in specs:
        d = src / s.get("dir", "")
        d.mkdir(parents=True, exist_ok=True)
        p = d / s["name"]
        p.write_bytes(b"\x01" * s.get("size", 100))
        v = np.zeros(n, dtype=np.float32)
        v[0], v[1] = s["vec"]
        cur = conn.execute(
            "INSERT INTO files(path,name,kind,size,mtime,hash,date_taken,date_source,data_incerta,width,height,status) "
            "VALUES(?,?,?,?,?,?,?,?,0,?,?,'embedded')",
            (str(p), p.name, s.get("kind", "image"), p.stat().st_size, p.stat().st_mtime, s["hash"], s["date"],
             s.get("date_source", "exif"), s.get("w", 1000), s.get("h", 800)))
        conn.execute("INSERT INTO embeddings(file_id, vec) VALUES(?,?)", (cur.lastrowid, dbm.to_blob(v)))
        ids[s["name"] if "dir" not in s else s["dir"] + "/" + s["name"]] = cur.lastrowid
    conn.commit()
    return cfg, conn, ids


def _plan_of(conn, fid: int):
    return conn.execute("SELECT folder, is_dup, dup_of FROM plan WHERE file_id=?", (fid,)).fetchone()


def test_near_dup_modificato_master_by_resolution(tmp_path: Path):
    base = dict(hash="h1", date="2021-08-10T10:00:00", vec=(0.0, 1.0))
    for edited_bigger in (False, True):
        specs = [
            dict(name="IMG_0624.HEIC", kind="image", w=4032, h=3024, size=1000, **{**base, "hash": "h1"}),
            # versione modificata: stessa scena (coseno ~0.995 con l'originale, sopra near_dup_name_min_sim)
            dict(name="IMG_0624-modificato.jpg", kind="image", w=1920 if not edited_bigger else 6000,
                 h=1440 if not edited_bigger else 4500, size=500, **{**base, "hash": "h2", "vec": (0.1, 0.995)}),
            dict(name="IMG_0625.jpg", kind="image", w=10, h=10, size=10, hash="h3", date="2019-01-01T00:00:00",
                 vec=(0.0, -1.0)),
        ]
        cfg, conn, ids = _near_catalog(tmp_path / str(edited_bigger), specs, near_dup_threshold=0.0)
        res = organizer.build_plan(conn, cfg)
        assert res["simili"] == 1 and res["duplicati"] == 0, res
        master, loser = ("IMG_0624-modificato.jpg", "IMG_0624.HEIC") if edited_bigger else \
            ("IMG_0624.HEIC", "IMG_0624-modificato.jpg")
        lp = _plan_of(conn, ids[loser])
        assert lp["is_dup"] == 2 and lp["dup_of"] == ids[master] and lp["folder"] == organizer.SIMILI_FOLDER, dict(lp)
        mp = _plan_of(conn, ids[master])
        assert mp["is_dup"] == 0 and mp["folder"] != organizer.SIMILI_FOLDER
        assert _plan_of(conn, ids["IMG_0625.jpg"])["is_dup"] == 0
        conn.close()


def test_near_dup_live_photo_is_not_duplicate(tmp_path: Path):
    # foto e video live: stesso nome, stessa data, embedding identico -> restano entrambi (tipi diversi)
    specs = [dict(name="IMG_1039.HEIC", kind="image", hash="ha", date="2021-08-10T10:00:00", vec=(1.0, 0.0)),
             dict(name="IMG_1039.MP4", kind="video", hash="hb", date="2021-08-10T10:00:00", vec=(1.0, 0.0))]
    cfg, conn, ids = _near_catalog(tmp_path, specs)
    res = organizer.build_plan(conn, cfg)
    assert _plan_of(conn, ids["IMG_1039.HEIC"])["is_dup"] == 0
    assert _plan_of(conn, ids["IMG_1039.MP4"])["is_dup"] == 0
    assert res["simili"] == 0, res
    conn.close()


def test_near_dup_window_chain_and_exact_copies(tmp_path: Path):
    t0 = "2021-08-10T10:00:"
    th8, th16 = np.deg2rad(8.0), np.deg2rad(16.0)  # cos8 ~ 0.990 (>= 0.985), cos16 ~ 0.961 (<)
    specs = [
        dict(name="A.jpg", hash="hA", date=t0 + "00", vec=(1.0, 0.0), w=4000, h=3000),
        dict(name="B.jpg", hash="hB", date=t0 + "10", vec=(np.cos(th8), np.sin(th8)), w=2000, h=1500),
        dict(name="C.jpg", hash="hC", date=t0 + "20", vec=(np.cos(th16), np.sin(th16)), w=1000, h=750),
        dict(name="D.jpg", hash="hD", date="2021-08-10T11:00:00", vec=(1.0, 0.0), w=4000, h=3000),
        dict(name="E.jpg", hash="hE", date=t0 + "05", vec=(1.0, 0.0), w=100, h=100, date_source="filename"),
        dict(dir="sub_lunga_copia", name="B_copia.jpg", hash="hB", date=t0 + "10", vec=(np.cos(th8), np.sin(th8)),
             w=2000, h=1500),
    ]
    cfg, conn, ids = _near_catalog(tmp_path, specs)
    res = organizer.build_plan(conn, cfg)
    assert res["simili"] == 1, res  # solo B: C e' solo "catena" di B, D e' fuori finestra, E non e' EXIF
    assert _plan_of(conn, ids["B.jpg"])["dup_of"] == ids["A.jpg"]
    assert _plan_of(conn, ids["B.jpg"])["is_dup"] == 2
    for ok in ("C.jpg", "D.jpg", "E.jpg", "A.jpg"):
        assert _plan_of(conn, ids[ok])["is_dup"] == 0, ok
    # la copia esatta di B ora punta al master A, resta in _Duplicati (esatti)
    bc = _plan_of(conn, ids["sub_lunga_copia/B_copia.jpg"])
    assert bc["is_dup"] == 1 and bc["dup_of"] == ids["A.jpg"] and bc["folder"] == organizer.DUP_FOLDER
    conn.close()


def test_near_dup_threshold_zero_and_skip_mode(tmp_path: Path):
    t0 = "2021-08-10T10:00:"
    th8 = np.deg2rad(8.0)
    specs = [dict(name="A.jpg", hash="hA", date=t0 + "00", vec=(1.0, 0.0), w=4000, h=3000),
             dict(name="B.jpg", hash="hB", date=t0 + "10", vec=(np.cos(th8), np.sin(th8)), w=2000, h=1500)]
    cfg, conn, ids = _near_catalog(tmp_path / "off", specs, near_dup_threshold=0.0)
    assert organizer.build_plan(conn, cfg)["simili"] == 0
    assert _plan_of(conn, ids["B.jpg"])["is_dup"] == 0
    conn.close()

    cfg, conn, ids = _near_catalog(tmp_path / "skip", specs, dup_mode="skip")
    assert organizer.build_plan(conn, cfg)["simili"] == 1
    assert _plan_of(conn, ids["B.jpg"])["folder"] is None
    conn.close()


def test_near_dup_similar_pairs_match_bruteforce():
    rng = np.random.default_rng(0)
    n = 2600  # piu' di un blocco da 1024: attraversa i confini di blocco
    centers = rng.normal(size=(40, 8))
    mat = centers[rng.integers(0, 40, n)] + rng.normal(scale=0.05, size=(n, 8))
    mat = (mat / np.linalg.norm(mat, axis=1, keepdims=True)).astype(np.float32)
    t = rng.uniform(0, 20000, n)
    ids = np.arange(n, dtype=np.int64)
    thr, win = 0.9, 300.0
    got = {(min(a, b), max(a, b)) for a, b in organizer._similar_pairs(ids, t, mat, thr, win)}
    sim = mat @ mat.T
    close = np.abs(t[:, None] - t[None, :]) <= win
    ii, jj = np.nonzero(np.triu(close & (sim >= thr), k=1))
    want = set(zip(ii.tolist(), jj.tolist()))
    assert got == want and len(want) > 100, (len(got), len(want))


# ------------------------------------------------------------ eventi temporali
def _ev_items(base: datetime, hours: list[float]):
    return [(base + timedelta(hours=h), i, 0.0) for i, h in enumerate(hours)]


def test_events_split_on_gap_only_above_threshold():
    cfg = Config(event_gap_hours=6.0, max_event_days=100.0)
    # pause 5.9h (unite), 6.1h (separa), 5.9h (unite)
    items = _ev_items(datetime(2021, 8, 10, 8), [0, 5.9, 11.8, 11.8 + 6.1, 11.8 + 6.1 + 5.9])
    evs = organizer._group_events(items, cfg)
    assert [len(e) for e in evs] == [3, 2], [len(e) for e in evs]


def test_events_cut_at_longest_inner_pause_when_too_long():
    # due blocchi da 10 foto (pause interne 1h) e una pausa 5.5h tra i due, poi 5.9h verso il terzo blocco
    base = datetime(2021, 8, 10, 0)
    hours = list(range(0, 10)) + [x + 14.5 for x in range(10)] + [x + 29.4 for x in range(10)]
    cfg = Config(event_gap_hours=6.0, max_event_days=1.0)  # 24h
    evs = organizer._group_events(_ev_items(base, hours), cfg)
    assert [len(e) for e in evs] == [20, 10], [len(e) for e in evs]  # taglio alla pausa piu' lunga (5.9h)


def test_events_equal_gaps_split_balanced_and_respect_max_span():
    base = datetime(2021, 8, 10, 9)
    items = _ev_items(base, [24.0 * d for d in range(10)])  # 10 giorni, pause uguali (24h > 6h: catene di 1)
    cfg = Config(event_gap_hours=30.0, max_event_days=3.0)  # chain unica di 10 foto, span 9 giorni
    evs = organizer._group_events(items, cfg)
    sizes = [len(e) for e in evs]
    assert sum(sizes) == 10 and sizes == [2, 3, 2, 3], sizes  # taglio centrale, niente foto isolate
    for e in evs:
        assert e[-1][0] - e[0][0] <= timedelta(days=3)
    assert [x[1] for e in evs for x in e] == list(range(10))  # ordine cronologico preservato


def test_event_names_single_day_and_multi_day():
    one = _ev_items(datetime(2021, 8, 10, 9), [0, 2, 5])
    assert organizer._event_name(one) == "Evento 2021-08-10"
    two = _ev_items(datetime(2021, 8, 10, 23), [0, 3])  # attraversa la mezzanotte: 2 giorni di calendario
    assert organizer._event_name(two) == "Evento 2021-08-10 (2 giorni)"
    three = _ev_items(datetime(2021, 8, 10, 9), [0, 24 * 2 + 1])
    assert organizer._event_name(three) == "Evento 2021-08-10 (3 giorni)"


def test_plan_event_folders_names_varie_and_month_of_first_photo(tmp_path: Path):
    t = "2021-08-10T09:00:"
    specs = [dict(name=f"a{i}.jpg", hash=f"ha{i}", date=t + f"{i:02d}", vec=(0.0, 0.0), w=10, h=10)
             for i in range(5)]  # 5 foto stesso giorno -> evento di un giorno (>= min_event_size)
    specs += [dict(name=f"b{i}.jpg", hash=f"hb{i}", date=d, vec=(0.0, 0.0), w=10, h=10) for i, d in enumerate(
        ["2021-08-31T22:00:00", "2021-08-31T23:00:00", "2021-09-01T01:00:00"])]  # 3 foto, a cavallo del mese
    specs += [dict(name="c0.jpg", hash="hc0", date="2021-10-05T10:00:00", vec=(0.0, 0.0), w=10, h=10),
              dict(name="c1.jpg", hash="hc1", date="2021-10-05T11:00:00", vec=(0.0, 0.0), w=10, h=10)]  # 2: Varie
    cfg, conn, ids = _near_catalog(tmp_path, specs, near_dup_threshold=0.0, near_dup_name_rule=False)
    organizer.build_plan(conn, cfg)
    assert _plan_of(conn, ids["a0.jpg"])["folder"] == "2021/08 - Evento 2021-08-10"
    assert _plan_of(conn, ids["b0.jpg"])["folder"] == "2021/08 - Evento 2021-08-31 (2 giorni)"
    assert _plan_of(conn, ids["b2.jpg"])["folder"] == "2021/08 - Evento 2021-08-31 (2 giorni)"
    assert _plan_of(conn, ids["c0.jpg"])["folder"] == "2021/10 - Varie"
    conn.close()


def test_rename_and_move_still_work_on_event_folders(tmp_path: Path):
    specs = [dict(name=f"a{i}.jpg", hash=f"ha{i}", date=f"2021-08-10T09:00:0{i}", vec=(0.0, 0.0), w=10, h=10)
             for i in range(5)]
    cfg, conn, ids = _near_catalog(tmp_path, specs, near_dup_threshold=0.0, near_dup_name_rule=False)
    organizer.build_plan(conn, cfg)
    organizer.rename_group(conn, "2021/08 - Evento 2021-08-10", "2021/08 - Vacanza")
    assert _plan_of(conn, ids["a0.jpg"])["folder"] == "2021/08 - Vacanza"
    organizer.build_plan(conn, cfg)  # il ricalcolo rispetta la rinomina
    assert _plan_of(conn, ids["a1.jpg"])["folder"] == "2021/08 - Vacanza"
    organizer.move_files(conn, [ids["a2.jpg"]], "2021/08 - Altro")
    assert _plan_of(conn, ids["a2.jpg"])["folder"] == "2021/08 - Altro"
    conn.close()


# ------------------------------------------------------------ indicizzazione: progresso e file modificati
def test_index_progress_declared_and_changed_files_reprocessed(tmp_path: Path):
    from fotoscan.embedder import embed_pending
    from fotoscan.scanner_metadata import process_metadata, scan_into_db

    src = tmp_path / "src"
    src.mkdir()
    for i in range(3):
        Image.new("RGB", (64, 48), (i * 40, 10, 10)).save(src / f"f{i}.jpg")
    cfg = Config(source_dir=str(src), db_path=str(tmp_path / "c.db"), cache_dir=str(tmp_path / "cache"))
    conn = dbm.connect(cfg.db_path)
    events: list = []

    def prog(stage, done, total):
        events.append((stage, done, total))

    assert scan_into_db(conn, cfg, progress=prog) == 3
    assert events[0] == ("scansione", 0, 3) and events[-1] == ("scansione", 3, 3)
    events.clear()
    process_metadata(conn, cfg, progress=prog)
    assert events[0] == ("metadati", 0, 3) and events[-1] == ("metadati", 3, 3)

    # tutto gia' elaborato: ogni fase si dichiara comunque (niente 'scansione 3/3' residua)
    conn.execute("UPDATE files SET status='embedded'")
    conn.commit()
    events.clear()
    assert scan_into_db(conn, cfg, progress=prog) == 0
    assert process_metadata(conn, cfg, progress=prog) == 0
    assert embed_pending(conn, cfg, embedder=None, progress=prog) == 0
    assert events[-1] == ("embedding", 0, 0) and ("metadati", 0, 0) in events

    # file modificato (dimensione diversa): torna 'new', perde embedding e fotogrammi; gli altri restano
    fid = conn.execute("SELECT id FROM files WHERE name='f1.jpg'").fetchone()[0]
    conn.execute("INSERT INTO embeddings(file_id, vec) VALUES(?, ?)", (fid, dbm.to_blob(np.ones(4, dtype=np.float32))))
    conn.commit()
    Image.new("RGB", (200, 160), (5, 90, 200)).save(src / "f1.jpg")
    assert scan_into_db(conn, cfg, progress=prog) == 1
    rows = {r["name"]: (r["status"], r["hash"]) for r in conn.execute("SELECT name, status, hash FROM files")}
    assert rows["f1.jpg"] == ("new", None)
    assert rows["f0.jpg"][0] == "embedded" and rows["f2.jpg"][0] == "embedded"
    assert conn.execute("SELECT count(*) FROM embeddings WHERE file_id=?", (fid,)).fetchone()[0] == 0
    conn.close()


# ------------------------------------------ controllo di contenuto dei quasi-duplicati
def _write_pixels(conn, ids: dict, arrays: dict) -> None:
    """Sostituisce il contenuto (finto) dei file del catalogo con immagini JPEG vere."""
    for name, arr in arrays.items():
        path = conn.execute("SELECT path FROM files WHERE id=?", (ids[name],)).fetchone()[0]
        Image.fromarray(arr).save(path, "JPEG", quality=95)


def _texture(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, (64, 80, 3), dtype=np.uint8)


def _pair_specs(vec_b=(1.0, 0.0)) -> list[dict]:
    # stessa scena per il coseno (embedding identico), stessa data EXIF: l'unico criterio e' il contenuto
    return [dict(name="A.jpg", hash="hA", date="2021-08-10T10:00:00", vec=(1.0, 0.0)),
            dict(name="B.jpg", hash="hB", date="2021-08-10T10:00:30", vec=vec_b)]


def test_content_dark_and_uniform_pairs_are_dropped(tmp_path: Path):
    cases = {
        "nere": {"A.jpg": np.full((64, 80, 3), 2, np.uint8), "B.jpg": np.full((64, 80, 3), 3, np.uint8)},
        "uniformi": {"A.jpg": np.full((64, 80, 3), 128, np.uint8), "B.jpg": np.full((64, 80, 3), 128, np.uint8)},
    }
    for case, arrays in cases.items():
        cfg, conn, ids = _near_catalog(tmp_path / case, _pair_specs(), near_dup_threshold=0.985)
        _write_pixels(conn, ids, arrays)
        res = organizer.build_plan(conn, cfg)
        assert res["simili"] == 0 and res["simili_scartati"] == 1, (case, res)
        pb = _plan_of(conn, ids["B.jpg"])
        assert pb["is_dup"] == 0 and pb["folder"] != organizer.SIMILI_FOLDER, (case, dict(pb))
        conn.close()
        # con il controllo disattivato la stessa coppia e' un quasi-duplicato
        cfg2, conn2, ids2 = _near_catalog(tmp_path / (case + "_off"), _pair_specs(), near_dup_threshold=0.985,
                                          near_dup_min_luma=0, near_dup_min_std=0, near_dup_min_corr=-1)
        _write_pixels(conn2, ids2, arrays)
        assert organizer.build_plan(conn2, cfg2)["simili"] == 1, case
        conn2.close()


def test_content_true_near_duplicate_is_kept(tmp_path: Path):
    a = _texture(1)
    edited = np.clip(a.astype(np.int16) + 12 + np.random.default_rng(2).normal(0, 3, a.shape), 0, 255).astype(np.uint8)
    cfg, conn, ids = _near_catalog(tmp_path, _pair_specs(), near_dup_threshold=0.985)
    _write_pixels(conn, ids, {"A.jpg": a, "B.jpg": edited})
    res = organizer.build_plan(conn, cfg)
    assert res["simili"] == 1 and res["simili_scartati"] == 0, res
    assert _plan_of(conn, ids["B.jpg"])["dup_of"] == ids["A.jpg"]
    conn.close()


def test_content_different_framing_with_high_cosine_is_dropped(tmp_path: Path):
    # embedding quasi identico (coseno ~0.99) ma inquadrature diverse: le miniature non si assomigliano
    cfg, conn, ids = _near_catalog(tmp_path, _pair_specs(vec_b=(np.cos(np.deg2rad(8)), np.sin(np.deg2rad(8)))),
                                   near_dup_threshold=0.985)
    _write_pixels(conn, ids, {"A.jpg": _texture(3), "B.jpg": _texture(4)})
    res = organizer.build_plan(conn, cfg)
    assert res["simili"] == 0 and res["simili_scartati"] == 1, res
    conn.close()


def test_content_video_uses_cached_frame(tmp_path: Path):
    specs = [dict(name="V1.MP4", kind="video", hash="v1", date="2021-08-10T10:00:00", vec=(1.0, 0.0)),
             dict(name="V2.MP4", kind="video", hash="v2", date="2021-08-10T10:00:20", vec=(1.0, 0.0))]
    cfg, conn, ids = _near_catalog(tmp_path, specs, near_dup_threshold=0.985)
    frames = Path(cfg.frames_dir)
    frames.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.zeros((48, 64, 3), np.uint8)).save(frames / f"{ids['V1.MP4']}_1.jpg")  # fotogramma nero
    Image.fromarray(np.zeros((48, 64, 3), np.uint8)).save(frames / f"{ids['V2.MP4']}_1.jpg")
    res = organizer.build_plan(conn, cfg)
    assert res["simili"] == 0 and res["simili_scartati"] == 1, res
    conn.close()


def test_name_rule_requires_embedding_similarity(tmp_path: Path):
    base = dict(hash="h1", date="2021-08-10T10:00:00", vec=(0.0, 1.0))
    specs = [dict(name="IMG_7.jpg", w=4000, h=3000, size=900, **base),
             dict(name="IMG_7-modificata.jpg", w=1000, h=750, size=100, **{**base, "hash": "h2", "vec": (1.0, 0.0)})]
    cfg, conn, ids = _near_catalog(tmp_path / "diversa", specs, near_dup_threshold=0.0)
    res = organizer.build_plan(conn, cfg)  # stesso nome, scena diversa (coseno 0): niente gruppo
    assert res["simili"] == 0 and res["simili_scartati"] == 1, res
    conn.close()
    # senza embedding la regola nome resta valida come prima
    cfg, conn, ids = _near_catalog(tmp_path / "senza_emb", specs, near_dup_threshold=0.0)
    conn.execute("DELETE FROM embeddings WHERE file_id=?", (ids["IMG_7-modificata.jpg"],))
    conn.commit()
    assert organizer.build_plan(conn, cfg)["simili"] == 1
    conn.close()



def _run_all() -> int:
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            try:
                kwargs = {"tmp_path": Path(d)} if "tmp_path" in fn.__code__.co_varnames[: fn.__code__.co_argcount] else {}
                fn(**kwargs)
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL", name, type(e).__name__, e)
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
