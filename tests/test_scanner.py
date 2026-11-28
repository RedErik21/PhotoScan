"""Test della pipeline di indicizzazione (scanner_metadata, embedder, config) senza modelli ne' rete.

Esecuzione: `python tests\\test_scanner.py` (o tools\\run_tests.py scanner).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="fs_scanner_test_"))
os.environ["FOTOSCAN_DB"] = str(_TMP / "catalogo.db")  # mai il catalogo vero
os.environ["FOTOSCAN_CACHE"] = str(_TMP / "cache")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from fotoscan import config as cfgmod  # noqa: E402
from fotoscan import db as dbm  # noqa: E402
from fotoscan import embedder as emb  # noqa: E402
from fotoscan import scanner_metadata as sm  # noqa: E402
from fotoscan.config import Config  # noqa: E402


def _cfg(source: Path) -> Config:
    cfg = Config(source_dir=str(source), db_path=str(_TMP / "c.db"), cache_dir=str(_TMP / "cache"), workers=2)
    cfg.find_ffmpeg = lambda: (None, "ffprobe-finto")  # type: ignore[method-assign]
    return cfg


def _conn():
    for ext in ("", "-wal", "-shm"):
        Path(str(_TMP / "c.db") + ext).unlink(missing_ok=True)
    return dbm.connect(str(_TMP / "c.db"))


def _tree(name: str) -> Path:
    d = _TMP / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _jpg(path: Path, color=(120, 80, 40)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), color).save(path, "JPEG")


# ------------------------------------------------------------ date dei video
def test_data_video_iso_con_T_e_utc_convertita_in_locale():
    got = sm.parse_video_date("2021-05-01T10:00:00.000000Z")
    assert got is not None
    expected = datetime(2021, 5, 1, 10, 0, 0, tzinfo=sm.timezone.utc).astimezone().replace(tzinfo=None)
    assert got == expected.isoformat(timespec="seconds")
    # con offset esplicito
    off = sm.parse_video_date("2021-05-01T12:00:00+02:00")
    assert off == expected.isoformat(timespec="seconds")
    # senza fuso: ora gia' locale, nessuna conversione
    assert sm.parse_video_date("2021-05-01 10:00:00") == "2021-05-01T10:00:00"


def test_data_video_orologio_non_impostato_o_assurda():
    assert sm.parse_video_date("1904-01-01T00:00:00.000000Z") is None
    assert sm.parse_video_date("1970-01-01T00:00:00Z") is None
    assert sm.parse_video_date("2999-01-01T00:00:00Z") is None
    assert sm.parse_video_date("non una data") is None
    assert sm.parse_video_date(None) is None
    assert sm.parse_video_date("2021-13-45T10:00:00Z") is None


def test_probe_video_usa_creation_time():
    out = json.dumps({"format": {"duration": "12.5", "tags": {"creation_time": "2021-05-01T10:00:00.000000Z"}},
                      "streams": [{"codec_type": "video", "width": 1920, "height": 1080}]})
    with mock.patch.object(sm.subprocess, "run", return_value=mock.Mock(stdout=out)):
        m = sm.probe_video("x.mp4", "ffprobe")
    assert m.date_source == "video" and m.date.startswith("2021-05-0")
    assert m.duration == 12.5 and (m.width, m.height) == (1920, 1080)


def test_mtime_assurdo_non_fa_crashare():
    assert sm._mtime_iso(-5e9).startswith("19")  # negativo: niente OSError su Windows
    assert sm._mtime_iso(1e30) == "1970-01-01T00:00:00"
    assert sm._mtime_iso(None) == "1970-01-01T00:00:00"


# ------------------------------------------------------------ scansione
def test_cartelle_di_servizio_nas_escluse():
    src = _tree("nas")
    _jpg(src / "foto.jpg")
    _jpg(src / "@eaDir" / "foto.jpg" / "SYNOPHOTO_THUMB_M.jpg")
    _jpg(src / "#recycle" / "cestinata.jpg")
    _jpg(src / "$RECYCLE.BIN" / "x.jpg")
    _jpg(src / ".Trash-1000" / "y.jpg")
    _jpg(src / "Vacanze" / "mare.jpg")
    names = sorted(p.name for p in sm.list_media(str(src), _cfg(src)))
    assert names == ["foto.jpg", "mare.jpg"]


def test_cambio_ora_legale_non_rifa_il_file():
    assert sm._mtime_changed(1000.0, 1000.4) is False
    assert sm._mtime_changed(1000.0, 1000.0 + 3600) is False   # FAT: +1 h
    assert sm._mtime_changed(1000.0, 1000.0 - 7200 + 1) is False
    assert sm._mtime_changed(1000.0, 1000.0 + 90) is True      # modifica vera
    assert sm._mtime_changed(None, 5.0) is True


def test_scan_e_metadati_end_to_end():
    src = _tree("e2e")
    _jpg(src / "IMG_20200315_101500.jpg")
    cfg = _cfg(src)
    conn = _conn()
    assert sm.scan_into_db(conn, cfg) == 1
    assert sm.process_metadata(conn, cfg) == 1
    r = conn.execute("SELECT status, date_source, width, height FROM files").fetchone()
    assert r["status"] == "meta" and r["date_source"] == "filename" and (r["width"], r["height"]) == (32, 24)
    conn.close()


def test_disco_scollegato_non_crea_errori_permanenti():
    src = _tree("usb")
    for i in range(3):
        _jpg(src / f"f{i}.jpg")
    cfg = _cfg(src)
    conn = _conn()
    sm.scan_into_db(conn, cfg)
    # simula il disco che sparisce: la lettura fallisce e la sorgente non risponde piu'
    with mock.patch.object(sm, "hash_and_bytes", side_effect=FileNotFoundError("sparito")), \
            mock.patch.object(sm.sourcefs, "reachable", return_value=False):
        try:
            sm.process_metadata(conn, cfg)
            raise AssertionError("SourceUnavailable attesa")
        except sm.SourceUnavailable as e:
            assert "non risponde" in str(e)
    st = {r["status"] for r in conn.execute("SELECT status FROM files")}
    assert st == {"new"}  # nessun file in 'error': alla prossima esecuzione ripartono
    # con la sorgente raggiungibile un file davvero illeggibile resta un errore del singolo file
    with mock.patch.object(sm, "hash_and_bytes", side_effect=PermissionError("negato")):
        sm.process_metadata(conn, cfg)
    assert {r["status"] for r in conn.execute("SELECT status FROM files")} == {"error"}
    conn.close()


def test_exif_corrotto_conserva_le_dimensioni():
    src = _tree("exif")
    _jpg(src / "a.jpg")
    with mock.patch.object(Image.Image, "getexif", side_effect=SyntaxError("exif rotto")):
        m = sm.read_image_meta(str(src / "a.jpg"))
    assert (m.width, m.height) == (32, 24) and m.date is None


def test_ricalcolo_data_video_una_tantum():
    src = _tree("vid")
    cfg = _cfg(src)
    conn = _conn()
    conn.executemany(
        "INSERT INTO files(path,name,kind,status,date_taken,date_source,data_incerta,duration,hash) VALUES(?,?,?,?,?,?,?,?,?)",
        [(str(src / "a.mp4"), "a.mp4", "video", "embedded", "2024-01-01T00:00:00", "mtime", 1, 10.0, "h1"),
         (str(src / "b.mp4"), "b.mp4", "video", "meta", "2020-05-05T00:00:00", "sidecar", 0, 5.0, "h2"),
         (str(src / "c.mp4"), "c.mp4", "video", "embedded", "2024-01-01T00:00:00", "mtime", 1, None, "h3")])
    conn.execute("INSERT INTO embeddings(file_id, vec) VALUES(1, ?)", (dbm.to_blob(np.ones(4)),))
    conn.commit()
    calls = []

    def fake_probe(path, ffprobe):
        calls.append(os.path.basename(path))
        m = sm.Meta()
        m.date, m.date_source = "2021-05-01T10:00:00", "video"
        m.duration, m.width, m.height = 7.0, 640, 480
        return m

    with mock.patch.object(sm, "probe_video", side_effect=fake_probe):
        assert sm.refresh_video_metadata(conn, cfg) == 2
        assert sorted(calls) == ["a.mp4", "c.mp4"]  # b.mp4 ha la data dal sidecar: non si tocca
        assert sm.refresh_video_metadata(conn, cfg) == 0  # una sola volta
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM files")}
    assert rows["a.mp4"]["date_taken"] == "2021-05-01T10:00:00" and rows["a.mp4"]["date_source"] == "video"
    assert rows["a.mp4"]["data_incerta"] == 0 and rows["a.mp4"]["duration"] == 10.0  # durata gia' presente: invariata
    assert rows["c.mp4"]["duration"] == 7.0 and rows["c.mp4"]["width"] == 640
    assert rows["b.mp4"]["date_source"] == "sidecar" and rows["b.mp4"]["date_taken"] == "2020-05-05T00:00:00"
    assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 1  # embedding intatto
    assert rows["a.mp4"]["status"] == "embedded"
    conn.close()


def test_ricalcolo_video_senza_ffprobe_non_segna_il_passaggio():
    cfg = _cfg(_tree("vid2"))
    cfg.find_ffmpeg = lambda: (None, None)  # type: ignore[method-assign]
    conn = _conn()
    assert sm.refresh_video_metadata(conn, cfg) == 0
    assert dbm.get_meta(conn, "video_refresh_rev") is None  # si ripete quando ffprobe compare
    conn.close()


# ------------------------------------------------------------ embedder
class _FakeModel:
    """Finto modello: 'esaurisce la memoria' oltre `limit` immagini per lotto."""

    def __init__(self, limit: int, cuda_broken: bool = False):
        self.limit, self.cuda_broken, self.sizes = limit, cuda_broken, []

    def get_image_features(self, pixel_values):
        import torch

        self.sizes.append(pixel_values.shape[0])
        if self.cuda_broken:
            raise RuntimeError("CUDA error: no kernel image is available for execution on the device")
        if pixel_values.shape[0] > self.limit:
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
        return torch.ones(pixel_values.shape[0], 4)

    def to(self, dev):
        return self

    def float(self):
        return self


class _TorchShim:
    """torch vero, ma i tensori restano sul processore anche quando il test finge di usare 'cuda'."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def from_numpy(self, a):
        t = self._real.from_numpy(a)

        class _OnCpu:
            def to(self, dev, dtype=None):
                return t.to(dtype=dtype)

        return _OnCpu()


def _fake_embedder(model, device="cuda"):
    import torch

    e = emb.Embedder.__new__(emb.Embedder)
    e.torch, e.cfg = _TorchShim(torch), Config(batch_size=8)
    e.model, e.device, e.dtype, e._batch_cap = model, device, torch.float32, None
    return e


def _arrays(n):
    return [np.zeros((3, 4, 4), dtype=np.float32) for _ in range(n)]


def test_oom_dimezza_il_lotto_e_lo_ricorda():
    m = _FakeModel(limit=2)
    e = _fake_embedder(m, device="cpu")  # su CPU l'OOM non si gestisce: deve propagare
    try:
        e.encode_arrays(_arrays(5))
        raise AssertionError("atteso errore su CPU")
    except RuntimeError:
        pass
    e = _fake_embedder(_FakeModel(limit=2), device="cuda")
    with mock.patch.object(e.torch.cuda, "empty_cache", lambda: None):
        out = e.encode_arrays(_arrays(5))
    assert out.shape == (5, 4) and e._batch_cap == 2 and e.device == "cuda"
    assert max(e.model.sizes[-3:]) <= 2  # dopo la riduzione i lotti sono <= 2
    with mock.patch.object(e.torch.cuda, "empty_cache", lambda: None):
        e.encode_arrays(_arrays(6))
    assert max(e.model.sizes[-3:]) <= 2  # il limite resta per i blocchi successivi


def test_gpu_rotta_ripiega_su_cpu():
    m = _FakeModel(limit=100, cuda_broken=True)
    e = _fake_embedder(m, device="cuda")
    with mock.patch.object(e.torch.cuda, "empty_cache", lambda: None):
        e.to_cpu()
    assert e.device == "cpu"
    # il ripiego automatico dentro encode_arrays: dopo to_cpu il modello finto smette di fallire
    m2 = _FakeModel(limit=100, cuda_broken=True)
    e2 = _fake_embedder(m2, device="cuda")
    orig = e2.to_cpu

    def to_cpu_and_heal():
        orig()
        m2.cuda_broken = False

    e2.to_cpu = to_cpu_and_heal  # type: ignore[method-assign]
    with mock.patch.object(e2.torch.cuda, "empty_cache", lambda: None):
        out = e2.encode_arrays(_arrays(3))
    assert out.shape == (3, 4) and e2.device == "cpu"


def test_modello_diverso_blocca_l_embedding():
    src = _tree("mm")
    cfg = _cfg(src)
    conn = _conn()
    conn.execute("INSERT INTO files(path,name,kind,status) VALUES('a.jpg','a.jpg','image','embedded')")
    conn.execute("INSERT INTO files(path,name,kind,status) VALUES('b.jpg','b.jpg','image','meta')")
    conn.execute("INSERT INTO embeddings(file_id, vec) VALUES(1, ?)", (dbm.to_blob(np.ones(4)),))
    dbm.set_meta(conn, "embedding_model", "google/siglip2-so400m-patch14-384")
    conn.commit()
    cfg.model_name = "google/siglip2-base-patch16-224"
    assert emb.catalog_model_conflict(conn, cfg) == "google/siglip2-so400m-patch14-384"
    try:
        emb.embed_pending(conn, cfg)
        raise AssertionError("ModelMismatch attesa")
    except emb.ModelMismatch:
        pass
    # il meta del modello non viene sovrascritto
    assert dbm.get_meta(conn, "embedding_model") == "google/siglip2-so400m-patch14-384"
    cfg.model_name = "google/siglip2-so400m-patch14-384"
    assert emb.catalog_model_conflict(conn, cfg) is None
    conn.close()


def test_load_prep_percorso_lungo_usa_fs():
    # _fs e' no-op sotto i 240 caratteri e fuori da Windows; su Windows prefissa i percorsi lunghi
    short = r"C:\a\b.jpg" if os.name == "nt" else "/a/b.jpg"
    assert sm._fs(short) == short
    if os.name == "nt":
        long = "C:\\" + "\\".join(["x" * 50] * 6) + "\\f.jpg"
        assert sm._fs(long).startswith("\\\\?\\")


# ------------------------------------------------------------ config
def test_config_corrotta_va_in_quarantena():
    p = _TMP / "cfg1" / "config.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text("{ non json", encoding="utf-8")
    cfg = cfgmod.load_config(p)
    assert cfg.workers == Config().workers
    assert not p.exists() and (p.parent / "config.json.bad").read_text(encoding="utf-8") == "{ non json"


def test_config_con_bom_e_tipi_sbagliati():
    p = _TMP / "cfg2" / "config.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps({"source_dir": "D:/Foto", "workers": "otto", "batch_size": 0, "enable_faces": True}),
                 encoding="utf-8-sig")
    cfg = cfgmod.load_config(p)
    assert cfg.source_dir == "D:/Foto" and cfg.enable_faces is True
    assert cfg.workers == Config().workers  # stringa scartata
    assert cfg.batch_size == 1  # 0 portato a 1
    assert not (p.parent / "config.json.bad").exists()


def test_config_cache_non_creabile_ripiega():
    p = _TMP / "cfg3" / "config.json"
    p.parent.mkdir(exist_ok=True)
    c = Config(cache_dir=str(_TMP / "cfg3" / "file_non_cartella" / "cache"))
    (p.parent / "file_non_cartella").write_text("x")  # un file al posto della cartella: mkdir fallisce
    c.save(p)
    env = {k: v for k, v in os.environ.items() if k != "FOTOSCAN_CACHE"}
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(cfgmod, "ROOT", _TMP / "root"):
        cfg = cfgmod.load_config(p)
    assert Path(cfg.cache_dir).is_dir() and "file_non_cartella" not in cfg.cache_dir


def test_config_save_atomico():
    p = _TMP / "cfg4" / "config.json"
    p.parent.mkdir(exist_ok=True)
    Config(source_dir="A").save(p)
    assert json.loads(p.read_text(encoding="utf-8"))["source_dir"] == "A"
    assert not (p.parent / "config.json.part").exists()
    # errore durante la scrittura: il file buono resta intatto e il temporaneo sparisce
    with mock.patch.object(cfgmod.os, "replace", side_effect=OSError("disco pieno")):
        try:
            Config(source_dir="B").save(p)
            raise AssertionError("OSError attesa")
        except OSError:
            pass
    assert json.loads(p.read_text(encoding="utf-8"))["source_dir"] == "A"
    assert not (p.parent / "config.json.part").exists()


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} test superati")
