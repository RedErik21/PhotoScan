"""NAS: stato dello spazio, invio e ricezione verificati, pulizia del PC con conferma (Cestino).

Ogni operazione che scrive gira come job (progresso via SSE, annullabile). La pulizia richiede
un'anteprima e la conferma del numero di file mostrato: se l'elenco cambia nel frattempo, rifiuta.
"""
from __future__ import annotations

import dataclasses
import os
import threading
import time

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from .. import nas
from . import deps, jobs

router = APIRouter(prefix="/api/nas", tags=["nas"])

SEND, RECEIVE, CLEANUP = "nas_send", "nas_receive", "nas_cleanup"


def _check_dir(value, field: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > 1000:
        raise HTTPException(400, f"{field}: percorso non valido.")
    if not os.path.isabs(value) and not value.startswith("\\\\"):
        raise HTTPException(400, f"{field}: usa un percorso assoluto (es. Z:\\foto o \\\\NAS\\foto).")
    return value


def _system_dirs() -> list[str]:
    from ..config import CODE_ROOT

    out = [str(CODE_ROOT)]
    for var in ("WINDIR", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
        v = os.environ.get(var)
        if v:
            out.append(v)
    if os.name != "nt":
        out += ["/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/sbin", "/sys", "/usr", "/var"]
    return out


def _check_write_dir(value, field: str) -> str:
    """Cartella in cui PhotoScan scrive file ricevuti: assoluta, non la radice di un disco, non una cartella
    di sistema o del programma (le API non hanno password: un percorso qualsiasi non va accettato)."""
    value = _check_dir(value, field)
    if not value:
        return value
    full = os.path.normcase(os.path.abspath(value)).rstrip("\\/")
    drive, rest = os.path.splitdrive(full)
    if not rest.strip("\\/") or (value.startswith("\\\\") and full.count(os.sep) <= 3):
        raise HTTPException(400, f"{field}: scegli una cartella, non la radice di un disco o di una condivisione.")
    for sysdir in _system_dirs():
        sd = os.path.normcase(os.path.abspath(sysdir)).rstrip("\\/")
        if full == sd or full.startswith(sd + os.sep):
            raise HTTPException(400, f"{field}: non puo' essere una cartella di sistema o del programma ({sysdir}).")
    return value


CLEANUP_TTL_S = 10.0  # /status e' chiamato a ogni aggiornamento della pagina: l'anteprima pulizia fa uno stat per file
_cleanup_cache: tuple | None = None
_cleanup_lock = threading.Lock()


def _db_stamp(db_path: str) -> tuple:
    out = []
    for suf in ("", "-wal"):
        try:
            st = os.stat(db_path + suf)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _cleanup_summary(conn, db_path: str) -> dict:
    """cleanup_preview riusata per CLEANUP_TTL_S se il catalogo non e' cambiato (nessun job NAS in corso)."""
    global _cleanup_cache
    key = (db_path, _db_stamp(db_path))
    now = time.monotonic()
    with _cleanup_lock:
        c = _cleanup_cache
    if c is not None and c[0] == key and now - c[1] < CLEANUP_TTL_S:
        return c[2]
    out = nas.cleanup_preview(conn)
    with _cleanup_lock:
        _cleanup_cache = (key, now, out)
    return out


@router.get("/status")
def status(conn=Depends(deps.get_db)):
    cfg = deps.get_cfg()
    sp = nas.space(cfg.nas_dir)
    total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    copied = conn.execute("SELECT COUNT(*) FROM nas_copies").fetchone()[0]
    running_jobs = [j for j in (jobs.running_job(n) for n in (SEND, RECEIVE, CLEANUP)) if j]
    cleanup = nas.cleanup_preview(conn) if running_jobs else _cleanup_summary(conn, cfg.db_path)
    return {
        "nas_dir": cfg.nas_dir,
        "receive_dir": cfg.nas_receive_dir,
        "layout": cfg.nas_layout,
        "source_dir": cfg.source_dir,
        "space": sp,
        "catalog_files": total,
        "verified_copies": copied,
        "cleanup_ready": cleanup["count"],
        "cleanup_bytes": cleanup["bytes"],
        "albums": nas.list_albums(conn),
        "running": [j.name for j in running_jobs],
        "active_job": running_jobs[0].public() if running_jobs else None,
    }


@router.get("/log")
def nas_log(limit: int = Query(50, ge=1, le=300)):
    """Ultime voci del registro NAS (cache/nas_log.txt), dalla piu' recente."""
    from pathlib import Path

    path = Path(deps.get_cfg().cache_dir) / "nas_log.txt"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines[-limit:]):
        parts = line.split("\t", 2)
        if len(parts) == 3:
            out.append({"ts": parts[0], "action": parts[1], "detail": parts[2]})
    return out


def _overlap(a: str, b: str) -> bool:
    """True se due percorsi coincidono o uno e' dentro l'altro."""
    if not a or not b:
        return False
    na = os.path.normcase(os.path.abspath(a)).rstrip("\\/")
    nb = os.path.normcase(os.path.abspath(b)).rstrip("\\/")
    return na == nb or na.startswith(nb + os.sep) or nb.startswith(na + os.sep)


@router.put("/settings")
def put_settings(body: dict = Body(...)):
    from .routes_status import _write_config  # stessa scrittura atomica di config.json

    cur = deps.get_cfg()
    updates = {}
    if "nas_dir" in body:
        updates["nas_dir"] = _check_dir(body["nas_dir"], "Cartella NAS")
    if "nas_receive_dir" in body:
        updates["nas_receive_dir"] = _check_write_dir(body["nas_receive_dir"], "Cartella di ricezione")
    if "nas_layout" in body:
        if body["nas_layout"] not in nas.LAYOUTS:
            raise HTTPException(400, "Struttura non valida.")
        updates["nas_layout"] = body["nas_layout"]
    eff_nas = updates.get("nas_dir", cur.nas_dir)
    eff_recv = updates.get("nas_receive_dir", cur.nas_receive_dir)
    if eff_nas and cur.source_dir and _overlap(eff_nas, cur.source_dir):
        raise HTTPException(400, "La cartella NAS non puo' coincidere con (o essere dentro) la cartella sorgente.")
    if eff_nas and eff_recv and _overlap(eff_nas, eff_recv):
        raise HTTPException(400, "La cartella di ricezione non puo' coincidere con (o essere dentro) la cartella NAS.")
    if updates:
        _write_config(dataclasses.replace(cur, **updates))
    return {"ok": True, "saved": sorted(updates)}


def _parse_album_ids(raw) -> list[int] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        try:
            return [int(x) for x in raw]
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "ID album non validi.") from exc
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            return [int(x) for x in raw.split(",") if x.strip()]
        except ValueError as exc:
            raise HTTPException(400, "ID album non validi.") from exc
    raise HTTPException(400, "ID album non validi.")


@router.get("/plan")
def plan(
    layout: str = Query("as_is"),
    album_ids: str | None = Query(None),
    include_unsorted: bool = Query(True),
    conn=Depends(deps.get_db),
):
    cfg = deps.get_cfg()
    parsed_albums = _parse_album_ids(album_ids)
    try:
        items = nas.plan_send(conn, cfg, layout, album_ids=parsed_albums, include_unsorted=include_unsorted)
    except nas.NasError as e:
        raise HTTPException(400, str(e)) from e
    counts = {s: sum(1 for i in items if i["state"] == s) for s in ("da_copiare", "presente", "conflitto")}
    bytes_to_copy = sum(i.get("size", 0) for i in items if i["state"] == "da_copiare")
    sample = [{"name": i["name"], "rel": i["rel"], "state": i["state"]} for i in items if i["state"] != "presente"][:30]
    return {"layout": layout, "total": len(items), "counts": counts, "bytes_to_copy": bytes_to_copy, "sample": sample}


@router.post("/send")
def send(body: dict = Body(default={})):
    cfg = deps.get_cfg()
    layout = body.get("layout", cfg.nas_layout)
    if layout not in nas.LAYOUTS:
        raise HTTPException(400, "Struttura non valida.")
    parsed_albums = _parse_album_ids(body.get("album_ids"))
    include_unsorted = bool(body.get("include_unsorted", True))
    if jobs.running_job(SEND):
        raise HTTPException(409, "Un invio al NAS e' gia' in corso.")
    try:
        nas.require_nas(cfg)
    except nas.NasError as e:
        raise HTTPException(400, str(e)) from e

    def run(job):
        conn = deps.open_db()
        try:
            return nas.send_job(job, conn, cfg, layout, album_ids=parsed_albums, include_unsorted=include_unsorted)
        finally:
            conn.close()
    return jobs.start_job(SEND, run).public()


@router.post("/receive")
def receive(body: dict = Body(default={})):
    cfg = deps.get_cfg()
    dest = _check_write_dir(body.get("dest") or cfg.nas_receive_dir, "Cartella di ricezione")
    if not dest:
        raise HTTPException(400, "Scegli la cartella dove ricevere i file.")
    if jobs.running_job(RECEIVE):
        raise HTTPException(409, "Una ricezione dal NAS e' gia' in corso.")
    try:
        nas.require_nas(cfg)
    except nas.NasError as e:
        raise HTTPException(400, str(e)) from e
    return jobs.start_job(RECEIVE, lambda j: nas.receive_job(j, cfg, dest)).public()


@router.get("/cleanup")
def cleanup_preview(conn=Depends(deps.get_db)):
    return nas.cleanup_preview(conn)


@router.post("/cleanup")
def cleanup(body: dict = Body(...), conn=Depends(deps.get_db)):
    cfg = deps.get_cfg()
    if body.get("confirm") != "CESTINO":
        raise HTTPException(400, "Conferma non valida: scrivi CESTINO per confermare.")
    expected = body.get("expected_count")
    if not isinstance(expected, int) or expected < 1:
        raise HTTPException(400, "Numero di file da confermare non valido.")
    if jobs.running_job(CLEANUP):
        raise HTTPException(409, "Una pulizia e' gia' in corso.")
    if jobs.running_job(SEND):
        raise HTTPException(409, "Attendi la fine dell'invio al NAS prima di pulire il PC.")
    now = nas.cleanup_candidates(conn)
    if len(now) != expected:
        raise HTTPException(409, f"L'elenco e' cambiato dall'anteprima ({len(now)} file ora). Ricontrolla prima di confermare.")

    def run(job):
        c = deps.open_db()
        try:
            return nas.cleanup_job(job, c, cfg, expected)
        finally:
            c.close()
    return jobs.start_job(CLEANUP, run).public()
