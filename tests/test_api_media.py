"""Test di /api/files/{id}/thumb|display|media|frames con TestClient.

DB e cache sono temporanei (FOTOSCAN_DB / FOTOSCAN_CACHE): il catalogo vero non viene mai toccato.
Esecuzione: `python -m pytest tests/test_api_media.py` oppure `python tests/test_api_media.py`.
Il test video usa ffmpeg se disponibile (altrimenti viene saltato).
"""
from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="fs_media_test_"))
os.environ["FOTOSCAN_DB"] = str(_TMP / "catalogo.db")
os.environ["FOTOSCAN_CACHE"] = str(_TMP / "cache")

from fastapi.testclient import TestClient  # noqa: E402

from fotoscan import db as dbm  # noqa: E402
from fotoscan.server import deps  # noqa: E402
from fotoscan.server.app import create_app  # noqa: E402

SRC = _TMP / "src"
SRC.mkdir()
APP = create_app()


def _client() -> TestClient:
    return TestClient(APP)


def _jpeg_bytes(w: int, h: int) -> bytes:
    yy, xx = np.mgrid[0:h, 0:w]
    arr = np.stack([xx % 256, yy % 256, (xx + yy) % 256], axis=-1).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _ffmpeg() -> str | None:
    return deps.get_cfg().find_ffmpeg()[0]


def _setup() -> dict[str, int]:
    """Crea file di prova e le righe nel catalogo temporaneo. Ritorna {nome: id}."""
    deps.reload_cfg()
    files: dict[str, tuple[str, str, Path, float | None]] = {}
    jpg = SRC / "foto.jpg"
    jpg.write_bytes(_jpeg_bytes(800, 600))
    files["jpeg"] = ("foto.jpg", "image", jpg, None)

    png = SRC / "alpha.png"
    rgba = Image.new("RGBA", (300, 200), (255, 0, 0, 0))
    rgba.paste((0, 128, 255, 255), (0, 0, 150, 200))
    rgba.save(png)
    files["png_alpha"] = ("alpha.png", "image", png, None)

    heic = SRC / "IMG_0001.HEIC"
    try:
        import pillow_heif  # noqa: F401
        Image.new("RGB", (640, 480), (30, 160, 90)).save(heic, "HEIF")
        files["heic"] = ("IMG_0001.HEIC", "image", heic, None)
    except Exception as exc:  # pragma: no cover
        print("HEIC di prova non creabile:", exc)

    bad = SRC / "corrotto.jpg"
    bad.write_bytes(b"non e' un jpeg" * 50)
    files["corrotto"] = ("corrotto.jpg", "image", bad, None)

    files["mancante"] = ("sparito.jpg", "image", SRC / "sparito.jpg", None)  # non creato su disco

    ff = _ffmpeg()
    if ff:
        mp4 = SRC / "clip.mp4"
        subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
                        "-pix_fmt", "yuv420p", str(mp4)], check=True, capture_output=True)
        files["video"] = ("clip.mp4", "video", mp4, 3.0)

    # Bug noto in db.SCHEMA: l'indice ix_plan_folder precede la tabella plan e fa fallire connect() su
    # un catalogo nuovo. Qui creiamo lo schema senza quella riga; poi connect() funziona normalmente.
    raw = sqlite3.connect(deps.get_cfg().db_path)
    raw.executescript(dbm.SCHEMA.replace("CREATE INDEX IF NOT EXISTS ix_plan_folder ON plan(folder);", ""))
    raw.close()
    ids: dict[str, int] = {}
    conn = dbm.connect(deps.get_cfg().db_path)
    for key, (name, kind, path, dur) in files.items():
        cur = conn.execute(
            "INSERT INTO files(path,name,kind,size,mtime,date_taken,date_source,status,duration) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (str(path), name, kind, path.stat().st_size if path.exists() else 0, 0.0,
             "2020-06-09T09:28:06", "exif", "embedded", dur))
        ids[key] = cur.lastrowid
    conn.commit()
    conn.close()
    return ids


IDS: dict[str, int] = {}


def setup_module(_=None):
    IDS.update(_setup())


def _img_size(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


# ------------------------------------------------------------------ miniature
def test_thumb_jpeg_square_and_headers():
    c = _client()
    r = c.get(f"/api/files/{IDS['jpeg']}/thumb", params={"size": 256})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    assert _img_size(r.content) == (256, 256)
    assert "immutable" in r.headers["cache-control"] and "max-age=31536000" in r.headers["cache-control"]
    etag = r.headers["etag"]
    r2 = c.get(f"/api/files/{IDS['jpeg']}/thumb", params={"size": 256}, headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert not r2.content


def test_thumb_size_is_clamped():
    c = _client()
    assert _img_size(c.get(f"/api/files/{IDS['jpeg']}/thumb", params={"size": 10}).content) == (96, 96)
    assert _img_size(c.get(f"/api/files/{IDS['jpeg']}/thumb", params={"size": 9999}).content) == (512, 512)


def test_thumb_heic():
    if "heic" not in IDS:
        print("SKIP heic (pillow-heif non disponibile)")
        return
    r = _client().get(f"/api/files/{IDS['heic']}/thumb", params={"size": 128})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert _img_size(r.content) == (128, 128)


def test_thumb_png_alpha_on_neutral_background():
    r = _client().get(f"/api/files/{IDS['png_alpha']}/thumb", params={"size": 128})
    assert r.status_code == 200
    im = Image.open(io.BytesIO(r.content)).convert("RGB")
    assert im.size == (128, 128)
    # angolo in alto a destra: parte trasparente -> sfondo neutro, non nero
    assert min(im.getpixel((120, 4))) > 150


def test_thumb_corrupt_and_missing_give_placeholder():
    c = _client()
    for key in ("corrotto", "mancante"):
        r = c.get(f"/api/files/{IDS[key]}/thumb", params={"size": 128})
        assert r.status_code == 200, (key, r.text)
        assert r.headers["content-type"] == "image/png"
        assert Image.open(io.BytesIO(r.content)).size == (128, 128)
        assert "immutable" not in r.headers["cache-control"]


def test_unknown_id_is_404_json():
    r = _client().get("/api/files/999999/thumb")
    assert r.status_code == 404
    assert "detail" in r.json()


def test_video_thumb_and_frames():
    if "video" not in IDS:
        print("SKIP video (ffmpeg non trovato)")
        return
    c = _client()
    r = c.get(f"/api/files/{IDS['video']}/thumb", params={"size": 192})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert _img_size(r.content) == (192, 192)
    frames = c.get(f"/api/files/{IDS['video']}/frames").json()
    assert frames and all(u.startswith(f"/api/files/{IDS['video']}/frames/") for u in frames)
    f0 = c.get(frames[0])
    assert f0.status_code == 200 and f0.headers["content-type"] == "image/jpeg"
    assert c.get(f"/api/files/{IDS['jpeg']}/frames").json() == []


def test_display_missing_on_disk_is_404():
    r = _client().get(f"/api/files/{IDS['mancante']}/display")
    assert r.status_code == 404
    assert "detail" in r.json()


def test_display_limits_long_side():
    c = _client()
    r = c.get(f"/api/files/{IDS['jpeg']}/display", params={"max": 300})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    w, h = _img_size(r.content)
    assert max(w, h) == 300 and (w, h) == (300, 225)
    full = c.get(f"/api/files/{IDS['jpeg']}/display")
    assert _img_size(full.content) == (800, 600)  # sotto il default 2048: nessun ingrandimento


# --------------------------------------------------------------------- media
def test_media_full_and_mime():
    c = _client()
    raw = (SRC / "foto.jpg").read_bytes()
    r = c.get(f"/api/files/{IDS['jpeg']}/media")
    assert r.status_code == 200 and r.content == raw
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["accept-ranges"] == "bytes"


def test_media_range_cases():
    if "video" not in IDS:
        print("SKIP range video (ffmpeg non trovato)")
        return
    c = _client()
    vid = IDS["video"]
    raw = (SRC / "clip.mp4").read_bytes()
    n = len(raw)
    assert c.get(f"/api/files/{vid}/media").headers["content-type"] == "video/mp4"

    r = c.get(f"/api/files/{vid}/media", headers={"Range": "bytes=0-1"})
    assert r.status_code == 206 and r.content == raw[:2]
    assert r.headers["content-range"] == f"bytes 0-1/{n}"
    assert r.headers["content-length"] == "2"

    r = c.get(f"/api/files/{vid}/media", headers={"Range": f"bytes={n - 1}-{n - 1}"})
    assert r.status_code == 206 and r.content == raw[-1:]
    assert r.headers["content-range"] == f"bytes {n - 1}-{n - 1}/{n}"

    r = c.get(f"/api/files/{vid}/media", headers={"Range": f"bytes={n - 10}-"})
    assert r.status_code == 206 and r.content == raw[-10:]

    r = c.get(f"/api/files/{vid}/media", headers={"Range": "bytes=-5"})
    assert r.status_code == 206 and r.content == raw[-5:]

    r = c.get(f"/api/files/{vid}/media", headers={"Range": f"bytes={n}-{n + 100}"})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{n}"

    r = c.get(f"/api/files/{vid}/media", headers={"Range": f"bytes=0-{n + 500}"})
    assert r.status_code == 206 and r.content == raw  # fine oltre il file: tronca alla fine


def test_media_missing_on_disk_is_404():
    r = _client().get(f"/api/files/{IDS['mancante']}/media")
    assert r.status_code == 404 and "detail" in r.json()


# --------------------------------------------------------------- carico
def test_parallel_requests_no_errors():
    keys = [k for k in ("jpeg", "png_alpha", "heic", "corrotto", "mancante") if k in IDS]
    urls = [f"/api/files/{IDS[keys[i % len(keys)]]}/thumb?size={128 + 32 * (i % 4)}" for i in range(50)]

    def hit(u: str) -> int:
        return _client().get(u).status_code

    with ThreadPoolExecutor(max_workers=50) as ex:
        codes = list(ex.map(hit, urls))
    assert all(code == 200 for code in codes), codes


def test_concurrent_same_file_shared_generation():
    fresh = {"size": 176}
    c = _client()
    url = f"/api/files/{IDS['jpeg']}/thumb"
    with ThreadPoolExecutor(max_workers=20) as ex:
        codes = list(ex.map(lambda _: _client().get(url, params=fresh).status_code, range(20)))
    assert codes == [200] * 20
    thumbs = deps.get_cfg().thumbs_dir
    assert not list(thumbs.glob("*.tmp")), "scrittura atomica: nessun file temporaneo residuo"
    assert c.get(url, params=fresh).status_code == 200


# ------------------------------------------------- broken / file particolari
def _broken(fid: int) -> str | None:
    conn = dbm.connect(deps.get_cfg().db_path)
    try:
        row = conn.execute("SELECT reason FROM broken WHERE file_id=?", (fid,)).fetchone()
        return row["reason"] if row else None
    finally:
        conn.close()


def _add_file(name: str, path: Path, kind: str = "image") -> int:
    conn = dbm.connect(deps.get_cfg().db_path)
    cur = conn.execute("INSERT INTO files(path,name,kind,size,mtime,date_taken,date_source,status) "
                       "VALUES(?,?,?,?,?,?,?,?)", (str(path), name, kind, 0, 0.0, "2020-01-01T00:00:00", "exif", "embedded"))
    conn.commit()
    conn.close()
    return cur.lastrowid


def test_corrupt_marks_illeggibile_then_recovers():
    p = SRC / "recupero.jpg"
    p.write_bytes(b"non e' un jpeg" * 50)
    fid = _add_file("recupero.jpg", p)
    c = _client()
    assert c.get(f"/api/files/{fid}/thumb?size=128").status_code == 200  # segnaposto
    assert _broken(fid) == "illeggibile"
    p.write_bytes(_jpeg_bytes(300, 200))  # file sostituito: nuova versione, ora si legge
    r = c.get(f"/api/files/{fid}/thumb?size=128")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert _broken(fid) is None, "miniatura riuscita: il segno 'illeggibile' va tolto"


def test_missing_marks_and_return_clears():
    p = SRC / "va_e_torna.jpg"
    fid = _add_file("va_e_torna.jpg", p)
    c = _client()
    c.get(f"/api/files/{fid}/thumb?size=128")
    assert _broken(fid) == "mancante"
    p.write_bytes(_jpeg_bytes(200, 200))
    assert c.get(f"/api/files/{fid}/thumb?size=128").headers["content-type"] == "image/jpeg"
    assert _broken(fid) is None


def test_offline_volume_not_marked_missing():
    if os.name != "nt":
        return  # su Linux la radice esiste sempre: il caso (unita' scollegata) e' solo Windows
    import string
    libera = next((d for d in reversed(string.ascii_uppercase) if not Path(f"{d}:\\").exists()), None)
    if libera is None:
        return
    fid = _add_file("offline.jpg", Path(f"{libera}:/foto/offline.jpg"))
    assert _client().get(f"/api/files/{fid}/thumb?size=128").status_code == 200
    assert _broken(fid) is None, "disco non raggiungibile: il file non e' 'mancante'"


def test_oversized_image_not_marked_broken():
    from fotoscan.server import routes_media

    p = SRC / "grande.png"
    Image.new("RGB", (64, 64), (10, 200, 10)).save(p)
    fid = _add_file("grande.png", p)
    old = routes_media.MAX_PIXELS
    routes_media.MAX_PIXELS = 100
    try:
        c = _client()
        assert c.get(f"/api/files/{fid}/thumb?size=128").status_code == 200
        assert _broken(fid) is None
        assert c.get(f"/api/files/{fid}/display").status_code == 404
    finally:
        routes_media.MAX_PIXELS = old


def test_16bit_gray_png_not_white():
    arr = (np.tile(np.linspace(0, 65535, 200), (200, 1))).astype(np.uint16)
    p = SRC / "grigio16.png"
    Image.fromarray(arr).save(p)
    fid = _add_file("grigio16.png", p)
    r = _client().get(f"/api/files/{fid}/thumb?size=128")
    assert r.status_code == 200
    im = np.asarray(Image.open(io.BytesIO(r.content)).convert("L"))
    assert 60 < im.mean() < 200 and im[:, :10].mean() < 60 < 150 < im[:, -10:].mean()


def test_empty_file_and_ranges():
    p = SRC / "vuoto.mp4"
    p.write_bytes(b"")
    fid = _add_file("vuoto.mp4", p, "video")
    c = _client()
    r = c.get(f"/api/files/{fid}/media", headers={"Range": "bytes=0-"})
    assert r.status_code == 200 and r.content == b""
    q = SRC / "dieci.bin"
    q.write_bytes(bytes(range(10)))
    fid2 = _add_file("dieci.mp4", q, "video")
    assert c.get(f"/api/files/{fid2}/media", headers={"Range": "bytes=20-"}).status_code == 416
    r = c.get(f"/api/files/{fid2}/media", headers={"Range": "bytes=8-99"})
    assert r.status_code == 206 and r.content == bytes([8, 9])
    r = c.get(f"/api/files/{fid2}/media", headers={"Range": "bytes=0-1,4-5"})  # multiplo: si serve tutto
    assert r.status_code == 200 and len(r.content) == 10


def test_huge_id_is_404_not_500():
    c = _client()
    for path in ("thumb", "display", "media", "frames"):
        assert c.get(f"/api/files/{2**70}/{path}").status_code == 404, path


def test_binary_responses_not_gzipped():
    c = _client()
    r = c.get(f"/api/files/{IDS['jpeg']}/display", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and "content-encoding" not in r.headers


if __name__ == "__main__":
    setup_module()
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("OK  ", name)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print("FAIL", name, type(exc).__name__, exc)
    print("fallimenti:", failed)
    sys.exit(1 if failed else 0)
