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
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from PIL import Image, ImageDraw, ImageOps

from .. import scanner_metadata  # noqa: F401  (registra l'apertura HEIC via pillow-heif)
from ..scanner_metadata import extract_frames
from . import deps

log = logging.getLogger("fotoscan.media")
router = APIRouter(prefix="/api", tags=["media"])

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="media")
_lock = threading.Lock()
_inflight: dict[str, Future] = {}      # chiave cache -> generazione in corso (deduplica)
_key_locks: dict[str, threading.Lock] = {}
_bad: set[str] = set()                 # generazioni fallite per questa versione del file

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
async def _run(fn: Callable, *args):
    """Esegue fn nel pool condiviso senza bloccare l'event loop."""
    return await asyncio.get_running_loop().run_in_executor(_POOL, fn, *args)


async def _shared(key: str, build: Callable[[], Path]) -> Path:
    """Esegue build() nel pool; richieste concorrenti con la stessa chiave aspettano la stessa generazione."""
    with _lock:
        fut = _inflight.get(key)
        if fut is None:
            fut = _POOL.submit(_build_and_forget, key, build)
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
    with _lock:
        return _key_locks.setdefault(key, threading.Lock())


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
        row = conn.execute("SELECT path, name, kind, duration, status FROM files WHERE id=?", (fid,)).fetchone()
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
        if im.format == "JPEG":
            im.draft("RGB", (min_side, min_side))  # decodifica ridotta, ma mai sotto min_side
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, NEUTRAL + (255,))
            return Image.alpha_composite(bg, rgba).convert("RGB")
        return im.convert("RGB")


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
                raise RuntimeError("ffmpeg non trovato")
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
def _mark_broken(fid: int, reason: str | None) -> None:
    """Registra (reason) o toglie (None) un file dalla tabella broken: la galleria puo' nasconderli."""
    try:
        conn = deps.open_db()
        try:
            if reason:
                conn.execute("INSERT INTO broken(file_id, reason, checked) VALUES(?,?,datetime('now')) "
                             "ON CONFLICT(file_id) DO UPDATE SET reason=excluded.reason, checked=excluded.checked",
                             (fid, reason))
            else:
                conn.execute("DELETE FROM broken WHERE file_id=?", (fid,))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # mai bloccare una miniatura per questo
        log.exception("stato 'broken' di %s non salvato", fid)


@router.get("/files/{fid}/thumb")
async def thumb(fid: int, request: Request, size: int = 256, strict: bool = False):
    """Miniatura quadrata (crop centrale), JPEG, 96..512 px.

    strict=1: 404 invece del segnaposto (chi mostra solo immagini vere, es. la Home, puo' scartarle con onerror)."""
    size = _clamp(size, 96, 512)
    src = await _run(_source, fid)
    if src.stamp is None:
        await _run(_mark_broken, fid, "mancante")
        if strict:
            return Response(status_code=404)
        return await _placeholder(size, src.kind)
    etag = f'"{src.stamp}-t{size}"'
    if _not_modified(request, etag):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": IMMUTABLE})
    key = f"thumb:{fid}:{size}:{src.stamp}"
    if key in _bad:
        return Response(status_code=404) if strict else await _placeholder(size, src.kind)
    dst = deps.get_cfg().thumbs_dir / f"{fid}_{size}_{src.stamp}.jpg"
    try:
        path = await _shared(key, lambda: _build_thumb(src, size, dst))
    except Exception as exc:  # file corrotto o non decodificabile: segnaposto, mai 500
        log.warning("miniatura %s non generabile: %s", fid, exc)
        _bad.add(key)
        await _run(_mark_broken, fid, "illeggibile")
        return Response(status_code=404) if strict else await _placeholder(size, src.kind)
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
        path = await _shared(key, lambda: _build_display(src, mx, dst))
    except Exception as exc:
        log.warning("anteprima %s non generabile: %s", fid, exc)
        _bad.add(key)
        return await _placeholder(min(mx, 512), src.kind)
    return FileResponse(path, media_type="image/jpeg", headers={"ETag": etag, "Cache-Control": IMMUTABLE})


def _parse_range(header: str, size: int) -> tuple[int, int] | str | None:
    """(start, end) inclusivi; 'unsat' se fuori dal file (416); None se non interpretabile (si serve tutto)."""
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header)
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    a, b = m.group(1), m.group(2)
    if size == 0:
        return "unsat"
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


def _iter_file(f, length: int) -> Iterator[bytes]:
    """Legge a blocchi dal file aperto (mai tutto in RAM) e lo chiude a fine invio."""
    try:
        remaining = length
        while remaining > 0:
            chunk = f.read(min(CHUNK, remaining))
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
    f.seek(start)
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
        found = await _run(_ensure_frames, src)
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
            await _run(_ensure_frames, src)
        except Exception as exc:
            raise HTTPException(404, f"fotogramma non disponibile: {exc}")
    if not _usable(p):
        raise HTTPException(404, "fotogramma non disponibile")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
