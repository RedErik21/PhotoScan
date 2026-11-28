"""Media: miniature quadrate, anteprime, originali in streaming (HTTP Range) e fotogrammi video.

Tutto il lavoro su file (decodifica, ffmpeg, stat) gira in un ThreadPoolExecutor condiviso da 8 thread,
mai nell'event loop. Richieste concorrenti per lo stesso risultato condividono una sola generazione.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import stat as statmod
import threading
from collections import OrderedDict
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

import subprocess
import sys

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from PIL import Image, ImageDraw, ImageOps
from starlette.concurrency import run_in_threadpool

from .. import scanner_metadata  # noqa: F401  (registra l'apertura HEIC via pillow-heif)
from ..scanner_metadata import extract_frames
from . import deps

log = logging.getLogger("fotoscan.media")
router = APIRouter(prefix="/api", tags=["media"])

# Tre pool separati: la generazione di miniature foto non fa aspettare la lettura del catalogo / l'apertura di un
# originale (click su "play"), e ffmpeg (lento) ha un pool suo con pochi thread.
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="media")
_FAST_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="media-fast")
_VIDEO_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-video")
MAX_PIXELS = 500_000_000  # oltre questo limite l'immagine non si decodifica (RAM): niente miniatura, ma non e' "rotta"


class _NoPreview(Exception):
    """Anteprima impossibile per motivi che NON dipendono dal file (ffmpeg assente, formato non supportato qui,
    immagine enorme, errore di rete): il file non va segnato come danneggiato."""


class _NoFfmpeg(_NoPreview, RuntimeError):
    pass
_lock = threading.Lock()
_inflight: dict[str, Future] = {}      # chiave cache -> generazione in corso (deduplica)
_KEY_LOCKS = [threading.Lock() for _ in range(64)]  # lock a strisce: memoria fissa, non uno per chiave


class _BoundedSet:
    """Insieme con al massimo `cap` elementi (i piu' vecchi escono): non cresce per tutta la sessione."""

    def __init__(self, cap: int) -> None:
        self._d: OrderedDict[str, None] = OrderedDict()
        self._cap = cap
        self._l = threading.Lock()

    def __contains__(self, key: str) -> bool:
        with self._l:
            return key in self._d

    def add(self, key: str) -> None:
        with self._l:
            self._d[key] = None
            self._d.move_to_end(key)
            while len(self._d) > self._cap:
                self._d.popitem(last=False)

    def clear(self) -> None:
        with self._l:
            self._d.clear()


_bad = _BoundedSet(5000)  # generazioni fallite per questa versione del file

NEUTRAL = (226, 226, 230)
IMMUTABLE = "public, max-age=31536000, immutable"
CHUNK = 1 << 20
MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".heic": "image/heic", ".heif": "image/heif", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo", ".3gp": "video/3gpp", ".wmv": "video/x-ms-wmv",
}


@dataclass(frozen=True)
class _Src:
    id: int
    path: str
    name: str
    kind: str
    duration: float | None
    stamp: str | None  # None = file non presente su disco


# ------------------------------------------------------------------ utilita'
async def _run(fn: Callable, *args, pool: ThreadPoolExecutor | None = None):
    """Esegue fn in un pool (di default quello veloce: catalogo, apertura file) senza bloccare l'event loop."""
    return await asyncio.get_running_loop().run_in_executor(pool or _FAST_POOL, fn, *args)


def _pool_for(kind: str) -> ThreadPoolExecutor:
    return _VIDEO_POOL if kind == "video" else _POOL


async def _shared(key: str, build: Callable[[], Path], pool: ThreadPoolExecutor | None = None) -> Path:
    """Esegue build() nel pool; richieste concorrenti con la stessa chiave aspettano la stessa generazione."""
    with _lock:
        fut = _inflight.get(key)
        if fut is None:
            fut = (pool or _POOL).submit(_build_and_forget, key, build)
            _inflight[key] = fut
    # shield: se il client si disconnette non si annulla la generazione condivisa
    return await asyncio.shield(asyncio.wrap_future(fut))


def _build_and_forget(key: str, build: Callable[[], Path]) -> Path:
    try:
        return build()
    finally:
        with _lock:
            _inflight.pop(key, None)


def _key_lock(key: str) -> threading.Lock:
    return _KEY_LOCKS[hash(key) % len(_KEY_LOCKS)]


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _usable(p: Path) -> bool:
    try:
        return p.stat().st_size > 0
    except OSError:
        return False


def _source(fid: int) -> _Src:
    """Riga del catalogo + stato su disco. Unico punto da cui si leggono percorsi: solo quelli del DB."""
    conn = deps.open_db()
    try:
        row = conn.execute("SELECT path, name, kind, duration, status FROM files WHERE id=?", (deps.valid_id(fid),)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, "file non trovato")
    if row["status"] == "error":  # file danneggiato: nessuna anteprima ne' visione
        raise HTTPException(404, "File danneggiato: non disponibile per la visione.")
    path = row["path"]
    stamp = None
    try:
        st = os.stat(path)
        if statmod.S_ISREG(st.st_mode):
            raw = f"{path}|{st.st_mtime_ns}|{st.st_size}".encode("utf-8", "surrogatepass")
            stamp = hashlib.sha1(raw).hexdigest()[:16]
    except OSError:
        pass
    return _Src(fid, path, row["name"], row["kind"], row["duration"], stamp)


def _not_modified(request: Request, etag: str) -> bool:
    inm = request.headers.get("if-none-match")
    if not inm:
        return False
    tags = {t.strip() for t in inm.split(",")}
    return "*" in tags or etag in tags or f"W/{etag}" in tags


def _save_jpeg_atomic(im: Image.Image, dst: Path, quality: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name}.{uuid.uuid4().hex}.tmp")
    try:
        im.save(tmp, "JPEG", quality=quality)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def _load_rgb(path: str | Path, min_side: int) -> Image.Image:
    """Immagine RGB orientata secondo EXIF; trasparenza su sfondo neutro. min_side guida il draft JPEG."""
    with Image.open(path) as im:
        # Il limite di Pillow e' disattivato (scanner_metadata): qui ne vale uno finito, controllato sull'intestazione
        # prima di decodificare, cosi' un file con dimensioni assurde non esaurisce la RAM.
        if im.size[0] * im.size[1] > MAX_PIXELS:
            raise _NoPreview(f"immagine troppo grande ({im.size[0]}x{im.size[1]})")
        if im.format == "JPEG":
            im.draft("RGB", (min_side, min_side))  # decodifica ridotta, ma mai sotto min_side
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "La", "RGBa", "PA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, NEUTRAL + (255,))
            return Image.alpha_composite(bg, rgba).convert("RGB")
        if im.mode in ("I", "F") or im.mode.startswith("I;16"):
            return _gray_to_rgb(im)
        return im.convert("RGB")


def _gray_to_rgb(im: Image.Image) -> Image.Image:
    """Grigi a 16/32 bit (PNG e TIFF scientifici o da scanner): scalati a 8 bit invece di saturare a bianco."""
    import numpy as np

    arr = np.asarray(im, dtype=np.float32)
    hi = float(arr.max()) if arr.size else 0.0
    if hi > 255:
        arr = arr / 256.0 if hi <= 65535 else arr * (255.0 / hi)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "L").convert("RGB")


# ------------------------------------------------------------------ video
def _ensure_frames(src: _Src) -> list[tuple[int, Path]]:
    """Fotogrammi in cache (cache/frames/{id}_{i}.jpg); li estrae con ffmpeg se mancano."""
    cfg = deps.get_cfg()
    out = cfg.frames_dir
    n = max(1, len(cfg.video_fractions))
    want = [(i, out / f"{src.id}_{i}.jpg") for i in range(n)]
    if all(_usable(p) for _, p in want):
        return [(i, p) for i, p in want]
    with _key_lock(f"frames:{src.id}"):
        if not all(_usable(p) for _, p in want):
            ffmpeg = cfg.find_ffmpeg()[0]
            if not ffmpeg:
                raise _NoFfmpeg("ffmpeg non trovato")
            extract_frames(src.path, src.duration, out, src.id, cfg.video_fractions, ffmpeg)
    return [(i, p) for i, p in want if _usable(p)]


def _video_frame(src: _Src) -> Path:
    """Fotogramma centrale (indice 1, 50%) o il primo disponibile."""
    frames = deps.get_cfg().frames_dir
    mid = frames / f"{src.id}_1.jpg"
    if _usable(mid):
        return mid
    found = _ensure_frames(src)
    if not found:
        raise RuntimeError("nessun fotogramma disponibile")
    return next((p for i, p in found if i == 1), found[0][1])


def _origin(src: _Src) -> Path | str:
    return _video_frame(src) if src.kind == "video" else src.path


# ------------------------------------------------------------ generazione
def _build_thumb(src: _Src, size: int, dst: Path) -> Path:
    if _usable(dst):
        return dst
    im = _load_rgb(_origin(src), size)
    w, h = im.size
    s = min(w, h)
    left, top = (w - s) // 2, (h - s) // 2
    im = im.crop((left, top, left + s, top + s)).resize((size, size), Image.Resampling.LANCZOS)
    _save_jpeg_atomic(im, dst, 85)
    return dst


def _build_display(src: _Src, mx: int, dst: Path) -> Path:
    if _usable(dst):
        return dst
    im = _load_rgb(_origin(src), mx)
    im.thumbnail((mx, mx), Image.Resampling.LANCZOS)
    _save_jpeg_atomic(im, dst, 90)
    return dst


@lru_cache(maxsize=128)
def _placeholder_png(size: int, kind: str) -> bytes:
    """Segnaposto neutro generato in memoria (file mancante o non decodificabile)."""
    im = Image.new("RGB", (size, size), NEUTRAL)
    d = ImageDraw.Draw(im)
    c, r = size / 2, size * 0.18
    col = (150, 150, 158)
    if kind == "video":
        d.polygon([(c - r * 0.6, c - r), (c - r * 0.6, c + r), (c + r, c)], fill=col)
    else:
        d.polygon([(c - r * 1.4, c + r * 0.8), (c - r * 0.3, c - r * 0.3), (c + r * 0.4, c + r * 0.4),
                   (c + r * 0.9, c - r * 0.1), (c + r * 1.4, c + r * 0.8)], fill=col)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


async def _placeholder(size: int, kind: str) -> Response:
    data = await _run(_placeholder_png, size, kind)
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-cache"})


# -------------------------------------------------------------- endpoint
_checked = _BoundedSet(20000)  # (file, versione) gia' riconciliati con la tabella broken: niente scritture ripetute


def _mark_broken(fid: int, reason: str | None, only_reason: str | None = None) -> None:
    """Registra (reason) o toglie (None) un file dalla tabella broken: la galleria puo' nasconderli.
    Scrive solo se serve (nessuna scrittura se lo stato e' gia' quello). only_reason: toglie solo se il motivo coincide."""
    try:
        conn = deps.open_db()
        try:
            row = conn.execute("SELECT reason FROM broken WHERE file_id=?", (fid,)).fetchone()
            if reason:
                if row is not None and row["reason"] == reason:
                    return
                conn.execute("INSERT INTO broken(file_id, reason, checked) VALUES(?,?,datetime('now')) "
                             "ON CONFLICT(file_id) DO UPDATE SET reason=excluded.reason, checked=excluded.checked",
                             (fid, reason))
            else:
                if row is None or (only_reason and row["reason"] != only_reason):
                    return
                conn.execute("DELETE FROM broken WHERE file_id=?", (fid,))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # mai bloccare una miniatura per questo
        log.exception("stato 'broken' di %s non salvato", fid)


def _is_corrupt(exc: BaseException, src: _Src) -> bool:
    """True solo se l'errore dice che il FILE e' danneggiato. Non lo e' se manca ffmpeg, se il formato non e'
    supportato da questa installazione (HEIC senza pillow-heif), se l'immagine e' enorme, se la RAM e' finita o se
    la lettura e' fallita per un errore di sistema (disco di rete, permessi, file in uso)."""
    if isinstance(exc, (_NoPreview, MemoryError, subprocess.TimeoutExpired)):
        return False
    if isinstance(exc, OSError) and exc.errno is not None:
        return False
    if Path(src.name).suffix.lower() in (".heic", ".heif") and "pillow_heif" not in sys.modules:
        return False
    return True


async def _note_ok(fid: int, key: str) -> None:
    """Miniatura riuscita: il file non e' piu' (o non e' mai stato) illeggibile."""
    if key not in _checked:
        _checked.add(key)
        await _run(_mark_broken, fid, None)


@router.get("/files/{fid}/thumb")
async def thumb(fid: int, request: Request, size: int = 256, strict: bool = False):
    """Miniatura quadrata (crop centrale), JPEG, 96..512 px.

    strict=1: 404 invece del segnaposto (chi mostra solo immagini vere, es. la Home, puo' scartarle con onerror)."""
    size = _clamp(size, 96, 512)
    src = await _run(_source, fid)
    if src.stamp is None:
        # "mancante" solo se la cartella e' raggiungibile: un disco scollegato o un NAS spento non e' un file perso
        if await _run(deps.volume_online, src.path):
            await _run(_mark_broken, fid, "mancante")
        if strict:
            return Response(status_code=404)
        return await _placeholder(size, src.kind)
    back = f"back:{fid}:{src.stamp}"
    if back not in _checked:  # il file c'e': se era segnato "mancante" e' tornato
        _checked.add(back)
        await _run(_mark_broken, fid, None, "mancante")
    etag = f'"{src.stamp}-t{size}"'
    if _not_modified(request, etag):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": IMMUTABLE})
    key = f"thumb:{fid}:{size}:{src.stamp}"
    if key in _bad:
        return Response(status_code=404) if strict else await _placeholder(size, src.kind)
    dst = deps.get_cfg().thumbs_dir / f"{fid}_{size}_{src.stamp}.jpg"
    try:
        path = await _shared(key, lambda: _build_thumb(src, size, dst), _pool_for(src.kind))
    except Exception as exc:  # file corrotto o non decodificabile: segnaposto, mai 500
        log.warning("miniatura %s non generabile: %s", fid, exc)
        if _is_corrupt(exc, src):
            _bad.add(key)
            await _run(_mark_broken, fid, "illeggibile")
        return Response(status_code=404) if strict else await _placeholder(size, src.kind)
    await _note_ok(fid, f"ok:{fid}:{src.stamp}")
    return FileResponse(path, media_type="image/jpeg", headers={"ETag": etag, "Cache-Control": IMMUTABLE})


@router.get("/files/{fid}/display")
async def display(fid: int, request: Request, max: int = 2048):
    """JPEG ridimensionato (lato lungo <= max), per il visualizzatore."""
    mx = _clamp(max, 256, 4096)
    src = await _run(_source, fid)
    if src.stamp is None:  # niente segnaposto: il visualizzatore mostra l'errore (come per /media)
        raise HTTPException(404, "file non disponibile su disco")
    etag = f'"{src.stamp}-d{mx}"'
    if _not_modified(request, etag):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": IMMUTABLE})
    key = f"display:{fid}:{mx}:{src.stamp}"
    if key in _bad:
        return await _placeholder(min(mx, 512), src.kind)
    dst = Path(deps.get_cfg().cache_dir) / "display" / f"{fid}_{mx}_{src.stamp}.jpg"
    try:
        path = await _shared(key, lambda: _build_display(src, mx, dst), _pool_for(src.kind))
    except Exception as exc:
        log.warning("anteprima %s non generabile: %s", fid, exc)
        if not _is_corrupt(exc, src):  # ffmpeg/formato non supportato/immagine enorme: il file non e' rotto
            raise HTTPException(404, "anteprima non disponibile su questo computer") from None
        _bad.add(key)
        return await _placeholder(min(mx, 512), src.kind)
    return FileResponse(path, media_type="image/jpeg", headers={"ETag": etag, "Cache-Control": IMMUTABLE})


def _parse_range(header: str, size: int) -> tuple[int, int] | str | None:
    """(start, end) inclusivi; 'unsat' se fuori dal file (416); None se non interpretabile (si serve tutto)."""
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header)
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    a, b = m.group(1), m.group(2)
    if size == 0:  # file vuoto: niente intervalli da servire, si risponde 200 con corpo vuoto
        return None
    if a == "":  # suffisso: ultimi N byte
        n = int(b)
        if n == 0:
            return "unsat"
        return max(0, size - n), size - 1
    start = int(a)
    if start >= size:
        return "unsat"
    if b == "":
        return start, size - 1
    end = int(b)
    if end < start:
        return None
    return start, min(end, size - 1)


async def _iter_file(f, length: int):
    """Legge a blocchi dal file aperto (mai tutto in RAM) e lo chiude a fine invio, anche se il client si
    disconnette (cancellazione): su Windows un handle rimasto aperto impedisce di spostare o cancellare il file."""
    try:
        remaining = length
        while remaining > 0:
            chunk = await run_in_threadpool(f.read, min(CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        f.close()


def _open_media(path: str):
    f = open(path, "rb")
    try:
        return f, os.fstat(f.fileno()).st_size
    except Exception:
        f.close()
        raise


@router.get("/files/{fid}/media")
async def media(fid: int, request: Request):
    """Originale in sola lettura, in streaming; Range (206) per riproduzione video."""
    src = await _run(_source, fid)
    if src.stamp is None:
        raise HTTPException(404, "file non disponibile su disco")
    mime = MIME.get(Path(src.name).suffix.lower(), "application/octet-stream")
    etag = f'"{src.stamp}-m"'
    rng = request.headers.get("range")
    if not rng and _not_modified(request, etag):
        return Response(status_code=304, headers={"ETag": etag})
    try:
        f, size = await _run(_open_media, src.path)
    except OSError:
        raise HTTPException(404, "file non disponibile su disco")
    base = {"Accept-Ranges": "bytes", "ETag": etag, "Cache-Control": "private, max-age=3600"}
    parsed = _parse_range(rng, size) if rng else None
    if parsed == "unsat":
        f.close()
        return Response(status_code=416, headers={**base, "Content-Range": f"bytes */{size}"})
    if parsed is None:
        return StreamingResponse(_iter_file(f, size), media_type=mime,
                                 headers={**base, "Content-Length": str(size)})
    start, end = parsed
    try:
        f.seek(start)
    except OSError:
        f.close()
        raise HTTPException(404, "file non disponibile su disco") from None
    length = end - start + 1
    headers = {**base, "Content-Length": str(length), "Content-Range": f"bytes {start}-{end}/{size}"}
    return StreamingResponse(_iter_file(f, length), status_code=206, media_type=mime, headers=headers)


@router.get("/files/{fid}/frames")
async def frames(fid: int):
    """URL dei fotogrammi in cache di un video (anteprima a scorrimento); [] per le foto."""
    src = await _run(_source, fid)
    if src.kind != "video" or src.stamp is None:
        return []
    try:
        found = await _run(_ensure_frames, src, pool=_VIDEO_POOL)
    except Exception as exc:
        log.warning("fotogrammi %s non disponibili: %s", fid, exc)
        return []
    return [f"/api/files/{fid}/frames/{i}" for i, _ in found]


@router.get("/files/{fid}/frames/{index}")
async def frame(fid: int, index: int):
    """Singolo fotogramma JPEG (indice come da `frames`)."""
    cfg = deps.get_cfg()
    if not 0 <= index < max(1, len(cfg.video_fractions)):
        raise HTTPException(404, "fotogramma non trovato")
    src = await _run(_source, fid)
    if src.kind != "video" or src.stamp is None:
        raise HTTPException(404, "fotogramma non disponibile")
    p = cfg.frames_dir / f"{fid}_{index}.jpg"
    if not _usable(p):
        try:
            await _run(_ensure_frames, src, pool=_VIDEO_POOL)
        except Exception as exc:
            raise HTTPException(404, f"fotogramma non disponibile: {exc}")
    if not _usable(p):
        raise HTTPException(404, "fotogramma non disponibile")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
