"""Scansione sorgente (sola lettura), metadati, hash, fotogrammi video."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from PIL import Image

from . import db as dbm
from . import sourcefs
from .config import IMAGE_EXT, VIDEO_EXT, Config

log = logging.getLogger("fotoscan")

try:  # HEIC opzionale
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass

try:
    import xxhash
except Exception:  # pragma: no cover
    xxhash = None

Image.MAX_IMAGE_PIXELS = None  # niente falsi allarmi su panorami enormi

_NO_WINDOW = 0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

# Video piu' lunghi di questa soglia: fotogrammi da keyframe (molto piu' veloce su 4K/lunghi).
FAST_SEEK_MIN_DURATION = 8.0


def _fs(path: str) -> str:
    """Percorso da passare alle API di sistema. Su Windows oltre 260 caratteri serve il prefisso \\\\?\\
    (se LongPathsEnabled e' spento). Non cambia i percorsi memorizzati nel catalogo."""
    if os.name != "nt" or len(path) < 240 or path.startswith("\\\\?\\"):
        return path
    ap = os.path.abspath(path)
    if ap.startswith("\\\\"):  # percorso UNC
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def _unfs(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


@dataclass
class Meta:
    date: str | None = None
    date_source: str | None = None
    data_incerta: bool = False
    lat: float | None = None
    lon: float | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None


# ----------------------------------------------------------------- scansione
def list_media(source: str, cfg: Config, subdir: str | None = None) -> list[Path]:
    root = Path(source)
    if subdir:
        root = root / subdir
    if not root.is_dir():
        raise FileNotFoundError(f"Cartella sorgente non trovata: {root}")
    exts = set(IMAGE_EXT) | set(VIDEO_EXT)
    out: list[Path] = []
    walk_errors: list[OSError] = []
    network = sourcefs.is_network(str(root))
    # os.walk invece di rglob: una sola scandir per cartella, errori di permesso ignorati,
    # niente seguito dei symlink a cartelle (come rglob), percorsi lunghi tramite _fs.
    for dirpath, dirs, files in os.walk(_fs(str(root)), onerror=walk_errors.append):
        # cartella di appoggio dell'import dal telefono: i file vengono spostati altrove a fine copia,
        # indicizzarli lascerebbe voci orfane (miniature grigie) quando la cartella viene svuotata
        dirs[:] = [d for d in dirs if d != "_fotoscan_tmp_telefono"]
        for name in files:
            if name.startswith("._"):
                continue
            if os.path.splitext(name)[1].lower() in exts:
                out.append(Path(_unfs(os.path.join(dirpath, name))))
    if network and (any(sourcefs.fatal_walk_error(e) for e in walk_errors) or not sourcefs.reachable(str(root))):
        # su rete una lista incompleta sembra un archivio vuoto: meglio fermarsi che indicizzare a meta'
        raise FileNotFoundError(f"Il disco di rete non risponde durante la scansione di {root}: "
                                "il catalogo non e' stato modificato. Controlla il NAS e riprova.")
    out.sort(key=lambda x: str(x).lower())
    return out


def scan_into_db(conn: sqlite3.Connection, cfg: Config, limit: int | None = None, subdir: str | None = None,
                 progress: Callable[[str, int, int], None] | None = None) -> int:
    """Registra i file nuovi nel catalogo e riporta a 'new' quelli modificati (size o mtime diversi).

    Con limit: campione uniforme di N file. Ritorna il numero di file nuovi o modificati da elaborare.
    """
    # Cache dei JSON per cartella: va svuotata a ogni scansione, altrimenti i sidecar aggiunti dopo
    # il primo scan (nello stesso processo server) non vengono trovati.
    _json_dir_cache.clear()
    root = sourcefs.norm_root(cfg.source_dir)
    # stessa cartella con un altro nome di accesso (lettera, UNC, mount): riallinea il catalogo
    # prima di confrontare i file, cosi' non si reindicizza tutto
    old_root = dbm.get_meta(conn, "catalog_root")
    if old_root and old_root != root:
        moved = sourcefs.rebase_catalog(conn, old_root, root)
        dbm.set_meta(conn, "last_rebase", json.dumps({
            "moved": moved, "from": old_root, "to": root, "at": int(time.time()),
        }))
        if moved:
            log.info("catalogo riallineato alla nuova cartella sorgente: %d percorsi", moved)
    paths = list_media(cfg.source_dir, cfg, subdir)
    if limit and len(paths) > limit:
        step = len(paths) / limit
        paths = [paths[int(i * step)] for i in range(limit)]
    known = {r["path"]: r for r in conn.execute("SELECT id, path, size, mtime FROM files")}
    new = 0
    total = len(paths)
    if progress:
        progress("scansione", 0, total)
    for i, p in enumerate(paths):
        sp = str(p)
        row = known.get(sp)
        try:
            st = p.stat()
        except OSError:
            continue
        if row is not None:
            # gia' nel catalogo: se e' cambiato (size o mtime) va rielaborato da capo
            if row["size"] != st.st_size or row["mtime"] is None or abs(row["mtime"] - st.st_mtime) >= 1:
                _reset_changed(conn, cfg, row["id"], st)
                new += 1
        else:
            kind = "video" if p.suffix.lower() in VIDEO_EXT else "image"
            conn.execute("INSERT OR IGNORE INTO files(path,name,kind,size,mtime) VALUES(?,?,?,?,?)",
                         (sp, p.name, kind, st.st_size, st.st_mtime))
            new += 1
        if (i + 1) % 500 == 0:  # progresso anche quando i file sono gia' noti (scansioni lunghe)
            conn.commit()
            if progress:
                progress("scansione", i + 1, total)
    conn.commit()
    dbm.set_meta(conn, "catalog_root", root)  # percorsi del catalogo registrati sotto questa radice
    if progress:
        progress("scansione", total, total)
    return new


def _reset_changed(conn: sqlite3.Connection, cfg: Config, fid: int, st: os.stat_result) -> None:
    """File modificato: torna in 'new' e scarta embedding, volti e fotogrammi vecchi (le miniature sono
    chiavate su size+mtime e si rigenerano da sole)."""
    conn.execute("UPDATE files SET size=?, mtime=?, hash=NULL, status='new', error=NULL WHERE id=?",
                 (st.st_size, st.st_mtime, fid))
    conn.execute("DELETE FROM embeddings WHERE file_id=?", (fid,))
    conn.execute("DELETE FROM faces WHERE file_id=?", (fid,))
    conn.execute("DELETE FROM faces_done WHERE file_id=?", (fid,))
    for f in cfg.frames_dir.glob(f"{fid}_*.jpg"):
        f.unlink(missing_ok=True)


# -------------------------------------------------------------------- hash
def file_hash(path: str) -> str:
    h = xxhash.xxh3_128() if xxhash else hashlib.sha256()
    with open(_fs(path), "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


# -------------------------------------------------------------- date / exif
_FN_PATTERNS = [
    re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?:[-_. T]?([01]\d|2[0-3])([0-5]\d)([0-5]\d))?(?!\d)"),
]


def date_from_filename(name: str) -> str | None:
    for pat in _FN_PATTERNS:
        m = pat.search(name)
        if not m:
            continue
        y, mo, d, hh, mm, ss = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))
        except ValueError:
            continue
        if 1990 <= dt.year <= datetime.now().year + 1:
            return dt.isoformat(timespec="seconds")
    return None


def _parse_exif_date(val) -> str | None:
    if not val:
        return None
    s = str(val).strip().replace("\x00", "")
    # alcuni software scrivono doppi spazi o spazi finali: tollerati
    m = re.match(r"(\d{4})[:\-](\d{2})[:\-](\d{2})\s+(\d{2}):(\d{2}):(\d{2})", s)
    if not m:
        return None
    try:
        dt = datetime(*map(int, m.groups()))
    except ValueError:
        return None
    if dt.year < 1990 or dt.year > datetime.now().year + 1:
        return None
    return dt.isoformat(timespec="seconds")


def _gps_to_deg(vals, ref) -> float | None:
    try:
        d, m, s = (float(x) for x in vals)
        deg = d + m / 60 + s / 3600
        return -deg if str(ref).upper() in ("S", "W") else deg
    except Exception:
        return None


ONE_READ_MAX = 64 * 1024 * 1024  # foto fino a 64 MB: lette dal disco una volta sola per hash ed EXIF


def hash_and_bytes(path: str) -> tuple[str, bytes | None]:
    """Hash del file e, per file piccoli, il contenuto gia' letto (per non riaprirlo per l'EXIF).
    Stesso hash di file_hash."""
    try:
        size = os.stat(_fs(path)).st_size
    except OSError:
        size = None
    if size is None or size > ONE_READ_MAX:
        return file_hash(path), None
    with open(_fs(path), "rb") as f:
        data = f.read()
    h = xxhash.xxh3_128() if xxhash else hashlib.sha256()
    h.update(data)
    return h.hexdigest(), data


def read_image_meta(path: str, data: bytes | None = None) -> Meta:
    meta = Meta()
    src = io.BytesIO(data) if data is not None else _fs(path)
    with Image.open(src) as im:
        meta.width, meta.height = im.size
        exif = im.getexif()
        if exif:
            sub = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else {}
            for tag, src in ((36867, sub), (36868, sub), (306, exif)):  # DateTimeOriginal, CreateDate, DateTime
                d = _parse_exif_date(src.get(tag))
                if d:
                    meta.date, meta.date_source = d, "exif"
                    break
            gps = exif.get_ifd(0x8825) if hasattr(exif, "get_ifd") else {}
            if gps and 2 in gps and 4 in gps:
                meta.lat = _gps_to_deg(gps[2], gps.get(1))
                meta.lon = _gps_to_deg(gps[4], gps.get(3))
    return meta


def probe_video(path: str, ffprobe: str | None) -> Meta:
    meta = Meta()
    if not ffprobe:
        return meta
    r = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", _fs(path)],
                       capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW)
    data = json.loads(r.stdout or "{}")
    fmt = data.get("format", {})
    try:
        meta.duration = float(fmt.get("duration")) if fmt.get("duration") else None
    except ValueError:
        pass
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            meta.width, meta.height = s.get("width"), s.get("height")
            break
    ct = (fmt.get("tags") or {}).get("creation_time")
    if ct:
        d = _parse_exif_date(ct.replace("Z", "")[:19])
        if d and not d.startswith("1970") and not d.startswith("1904"):
            meta.date, meta.date_source = d, "video"
    return meta


_SIDECAR_SUFFIX = ".supplemental-metadata"
_json_dir_cache: dict[str, list[tuple[str, str]]] = {}


def find_sidecar(path: str) -> str | None:
    """JSON di Google Takeout accanto al file. Gestisce i nomi vecchi (foto.jpg.json), i nuovi
    (foto.jpg.supplemental-metadata.json), quelli troncati, le versioni '-modificato' e le copie '(1)'."""
    d, name = os.path.split(path)
    entries = _json_dir_cache.get(d)
    if entries is None:
        try:
            entries = [(f[:-5].lower(), os.path.join(d, f)) for f in os.listdir(d) if f.lower().endswith(".json")]
        except OSError:
            entries = []
        _json_dir_cache[d] = entries
    if not entries:
        return None
    stem, ext = os.path.splitext(name)
    bases = [name]
    core, k = stem, None
    m = re.match(r"^(.*)\((\d+)\)$", stem)  # IMG(1).jpg -> IMG.jpg.supplemental-metadata(1).json
    if m:
        core, k = m.group(1), m.group(2)
        bases.append(f"{core}{ext}({k})")
    em = re.match(r"^(.*)-modificat[oa]$", core)  # versioni modificate da Google Foto
    if em:
        core = em.group(1)
        bases.append(core + ext)
    if k:
        suffix = f"({k})"
        cands = [(s, p) for s, p in entries if s.startswith(core.lower() + ext.lower() + ".") and s.endswith(suffix)]
        if cands:
            return min(cands, key=lambda c: len(c[0]))[1]
    for base in bases:
        b = base.lower()
        full = b + _SIDECAR_SUFFIX
        best = None
        for s, p in entries:
            # esatto | con suffisso (anche troncato) | nome lungo troncato da Google (~46 caratteri)
            if s == b or s.startswith(b + ".") or (len(s) >= 40 and full.startswith(s)):
                if best is None or len(s) > len(best[0]):
                    best = (s, p)
        if best:
            return best[1]
    # video "live photo": nessun JSON proprio, usa quello della foto con lo stesso nome (IMG_1.MP4 <- IMG_1.HEIC.json)
    pre = core.lower() + "."
    cands = []
    for s, p in entries:
        if not s.startswith(pre):
            continue
        has_k = re.search(r"\(\d+\)$", s) is not None
        if (k and s.endswith(f"({k})")) or (not k and not has_k):
            cands.append((s, p))
    if cands:
        return min(cands, key=lambda c: len(c[0]))[1]
    return None


def read_sidecar(path: str) -> tuple[str | None, float | None, float | None]:
    sc = find_sidecar(path)
    if not sc:
        return None, None, None
    # utf-8-sig: i JSON con BOM (alcuni esportatori) altrimenti fallirebbero e perderebbero la data
    with open(_fs(sc), "r", encoding="utf-8-sig", errors="replace") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return None, None, None
    date = None
    try:
        ts = (data.get("photoTakenTime") or {}).get("timestamp")
        if ts:
            dt = datetime.fromtimestamp(int(ts))  # ora locale, come l'EXIF
            if 1990 <= dt.year <= datetime.now().year + 1:
                date = dt.isoformat(timespec="seconds")
    except (AttributeError, TypeError, ValueError, OSError, OverflowError):
        pass
    lat = lon = None
    for key in ("geoData", "geoDataExif"):
        try:
            g = data.get(key) or {}
            if g.get("latitude") or g.get("longitude"):
                lat, lon = float(g["latitude"]), float(g["longitude"])
                break
        except (AttributeError, TypeError, ValueError):
            lat = lon = None  # coordinate non numeriche: si ignorano, la data resta
    return date, lat, lon


def extract_metadata(path: str, kind: str, name: str, mtime: float, ffprobe: str | None,
                     data: bytes | None = None) -> Meta:
    meta = read_image_meta(path, data) if kind == "image" else probe_video(path, ffprobe)
    if meta.date_source != "exif":
        try:
            sdate, slat, slon = read_sidecar(path)
        except Exception as e:  # JSON malformato: non e' fatale
            log.warning("sidecar illeggibile per %s: %s", path, e)
            sdate = slat = slon = None
        if sdate:
            meta.date, meta.date_source = sdate, "sidecar"
        if meta.lat is None and slat is not None:
            meta.lat, meta.lon = slat, slon
    if not meta.date:
        d = date_from_filename(name)
        if d:
            meta.date, meta.date_source = d, "filename"
    if not meta.date:
        meta.date = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
        meta.date_source = "mtime"
        meta.data_incerta = True
    return meta


def process_metadata(conn: sqlite3.Connection, cfg: Config,
                     progress: Callable[[str, int, int], None] | None = None) -> int:
    """status new -> meta (oppure error). Riprendibile: elabora solo status='new'."""
    _, ffprobe = cfg.find_ffmpeg()
    rows = conn.execute("SELECT id,path,kind,name,mtime FROM files WHERE status='new' ORDER BY id").fetchall()
    total, done = len(rows), 0
    if progress:  # fase sempre dichiarata, anche senza righe: la UI non resta sulla fase precedente
        progress("metadati", 0, total)

    def work(r):
        try:
            if r["kind"] == "image":
                h, data = hash_and_bytes(r["path"])  # una lettura sola per hash ed EXIF
            else:
                h, data = file_hash(r["path"]), None
        except Exception as e:  # file illeggibile
            return r["id"], None, None, f"hash: {e}"
        try:
            m = extract_metadata(r["path"], r["kind"], r["name"], r["mtime"], ffprobe, data)
        except Exception as e:  # EXIF corrotto / video non leggibile: data da nome/mtime, non e' fatale
            log.warning("metadati non leggibili %s: %s", r["path"], e)
            m = Meta()
            d = date_from_filename(r["name"])
            if d:
                m.date, m.date_source = d, "filename"
            else:
                m.date = datetime.fromtimestamp(r["mtime"]).isoformat(timespec="seconds")
                m.date_source, m.data_incerta = "mtime", True
        return r["id"], h, m, None

    with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
        for batch_start in range(0, total, 400):
            batch = rows[batch_start:batch_start + 400]
            for fid, h, m, err in ex.map(work, batch):
                if err:
                    conn.execute("UPDATE files SET status='error', error=? WHERE id=?", (err, fid))
                else:
                    conn.execute(
                        "UPDATE files SET hash=?,date_taken=?,date_source=?,data_incerta=?,lat=?,lon=?,"
                        "width=?,height=?,duration=?,status='meta' WHERE id=?",
                        (h, m.date, m.date_source, int(m.data_incerta), m.lat, m.lon, m.width, m.height, m.duration, fid))
                done += 1
            conn.commit()
            if progress:
                progress("metadati", done, total)
    return done


# ------------------------------------------------------- fotogrammi video
def _ffmpeg_frames(ffmpeg: str, path: str, jobs: list[tuple[float, Path]], keyframes_only: bool) -> list[Path]:
    """Fotogrammi (t, destinazione) con UN solo processo ffmpeg (su Windows l'avvio costa ~0.2 s).
    Ogni output va su un file temporaneo e poi viene rinominato: un'interruzione non lascia mai
    un JPEG troncato nella cache (che verrebbe riusato come valido). Ritorna quelli riusciti."""
    cmd = [ffmpeg, "-y", "-loglevel", "error"]
    tmps: list[tuple[Path, Path]] = []
    for k, (t, out) in enumerate(jobs):
        tmp = out.with_name(out.stem + ".part.jpg")
        tmps.append((tmp, out))
        if keyframes_only:
            cmd += ["-skip_frame", "nokey"]  # solo keyframe: l'istante e' approssimato a pochi secondi
        cmd += ["-ss", f"{t:.2f}", "-i", _fs(path)]
    # ogni uscita dal SUO ingresso: senza -map ffmpeg sceglie per tutte lo stesso flusso (il primo input),
    # e tutti i fotogrammi sarebbero uguali al primo
    for k, (tmp, _out) in enumerate(tmps):
        cmd += ["-map", f"{k}:v:0", "-frames:v", "1", "-vf", "scale='min(512,iw)':-2", "-q:v", "3", _fs(str(tmp))]
    try:
        subprocess.run(cmd, capture_output=True, timeout=120 * len(jobs), creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        for tmp, _ in tmps:
            tmp.unlink(missing_ok=True)
        raise
    ok: list[Path] = []
    for tmp, out in tmps:
        if tmp.exists() and tmp.stat().st_size > 0:
            os.replace(tmp, out)
            ok.append(out)
        else:
            tmp.unlink(missing_ok=True)
    return ok


def extract_frames(path: str, duration: float | None, out_dir: Path, file_id: int,
                   fractions: list[float], ffmpeg: str | None) -> list[Path]:
    """Estrae N fotogrammi in cache (mai toccando l'originale). Riusa quelli gia' presenti."""
    if not ffmpeg:
        raise RuntimeError("ffmpeg non trovato")
    out_dir.mkdir(parents=True, exist_ok=True)
    fast = bool(duration and duration >= FAST_SEEK_MIN_DURATION)
    targets = [(max(0.0, (duration or 0.0) * frac), out_dir / f"{file_id}_{i}.jpg") for i, frac in enumerate(fractions)]
    missing = [(t, out) for t, out in targets if not out.exists() or out.stat().st_size == 0]
    if missing:
        # video lunghi: prima dai keyframe in un solo processo; video brevi: seek esatto, sempre in un solo
        # processo (stessi fotogrammi, byte per byte, di un processo per fotogramma). Quelli ancora mancanti
        # si riprovano uno alla volta col seek esatto.
        _ffmpeg_frames(ffmpeg, path, missing, keyframes_only=fast)
        missing = [(t, out) for t, out in missing if not out.exists() or out.stat().st_size == 0]
        for t, out in missing:
            _ffmpeg_frames(ffmpeg, path, [(t, out)], keyframes_only=False)
    frames: list[Path] = [out for _, out in targets if out.exists() and out.stat().st_size > 0]
    if not frames:
        raise RuntimeError("nessun fotogramma estratto (video non decodificabile?)")
    return frames
