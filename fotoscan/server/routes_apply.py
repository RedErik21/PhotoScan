"""Applicazione del piano: anteprima (sola lettura), esecuzione reale (solo con conferma), progresso e log.

Riusa organizer.apply_plan. Mai scrittura nella sorgente: l'unica scrittura e' in output_dir.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import db as dbm
from .. import organizer
from . import deps, jobs
from .routes_status import _inside, put_settings

router = APIRouter(prefix="/api", tags=["apply"])

JOB_NAME = "apply"
LOG_NAME = "_fotoscan_apply.log"
SUGGEST_NAME = "FotoScan_Organizzato"
PREVIEW_TTL = 30.0  # secondi: l'anteprima si ricalcola anche senza modifiche al piano (es. file scritti altrove)
PROGRESS_EVERY = 20  # file tra un aggiornamento del job e il successivo (annullamento controllato a ogni file)
PROGRESS_SECONDS = 0.5
# Solo per collaudo: ritardo artificiale per file (ms), per osservare il progresso su archivi piccoli. Non impostarla in uso reale.
_TEST_DELAY = float(os.environ.get("FOTOSCAN_APPLY_TEST_DELAY_MS") or 0) / 1000

_run_lock = threading.Lock()  # un solo apply alla volta (anche tra richieste ravvicinate)
_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
# Contatori live dell'ultima esecuzione (scritti dal thread del job, letti da /apply/progress e /apply/log).
_live: dict[str, Any] = {"job_id": None, "counts": {}}


class _Cancelled(Exception):
    """Interrompe apply_plan dal callback di progresso quando l'utente annulla il job."""


def _fmt_bytes(n: int) -> str:
    """Stessa unita' del frontend (formatBytes, base 1024, come Esplora risorse)."""
    for unit, div in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}".replace(".", ",")
    return f"{n} B"


def _plan_fingerprint(conn: sqlite3.Connection) -> str:
    """Impronta del piano: cambia appena cambia una cartella assegnata a un file."""
    h = hashlib.md5()
    for row in conn.execute("SELECT file_id, folder FROM plan ORDER BY file_id"):
        h.update(f"{row[0]}\t{row[1]}\n".encode("utf-8"))
    return h.hexdigest()


def _clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _nearest_existing(p: Path) -> Path:
    """Primo antenato (o il path stesso) che esiste: serve per dischi e spazio libero."""
    for c in [p, *p.parents]:
        if c.exists():
            return c
    return p


def _effective_cfg(conn: sqlite3.Connection):
    """Config per l'applicazione: se la sorgente non e' in config.json, usa quella registrata nel catalogo
    dall'ultima indicizzazione (stessa regola di GET /settings)."""
    cfg = deps.get_cfg()
    if not cfg.source_dir:
        src = dbm.get_meta(conn, "source_dir", "") or ""
        if src:
            cfg = dataclasses.replace(cfg, source_dir=src)
    return cfg


def _counters_text(c: dict, done: int, total: int) -> str:
    return (f"{done} di {total} file · link {c.get('link', 0)} · copie {c.get('copie', 0)} · "
            f"già presenti {c.get('gia_presenti', 0)} · errori {c.get('errori', 0)}")


def _compute_preview(conn: sqlite3.Connection, cfg) -> dict:
    if not cfg.output_dir:
        return {"ok": False, "code": "no_destination", "error": "Scegli la cartella di destinazione per continuare."}
    if not cfg.source_dir or not os.path.isdir(cfg.source_dir):
        return {"ok": False, "error": "Cartella sorgente non trovata: controlla le impostazioni."}
    try:
        res = organizer.apply_plan(conn, cfg, apply=False)  # _check_dirs: output dentro/contenente la sorgente
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    src, out = Path(cfg.source_dir), Path(cfg.output_dir)
    anc = _nearest_existing(out)
    same_volume = os.stat(src).st_dev == os.stat(anc).st_dev
    hardlink = cfg.link_mode == "hardlink" and same_volume
    new_files = res["link"] + res["copie"]
    n_link = new_files if hardlink else 0
    n_copy = 0 if hardlink else new_files

    # Byte da scrivere davvero: solo i file la cui destinazione non esiste ancora (con hard link non occupano spazio).
    new_bytes = 0
    if not hardlink:
        rows = conn.execute(
            "SELECT p.folder, f.name, f.size FROM plan p JOIN files f ON f.id=p.file_id WHERE p.folder IS NOT NULL"
        ).fetchall()
        for folder, name, size in rows:
            if not (out / organizer.safe_folder(folder) / organizer.safe_name(name)).exists():
                new_bytes += size or 0
    free = shutil.disk_usage(anc).free

    warnings: list[str] = []
    if n_link:
        warnings.append("Collegamento: la foto nella nuova cartella è la stessa dell'originale, quindi modificarne una "
                        "cambia anche l'altra.")
    if cfg.link_mode == "copy":
        warnings.append("Modalità copia: i file verranno copiati e occuperanno spazio aggiuntivo.")
    elif not same_volume:
        warnings.append("Sorgente e destinazione sono su dischi diversi: verrà usata la copia, "
                        f"servono {_fmt_bytes(new_bytes)} di spazio extra.")
    if new_bytes > free:
        warnings.append(
            f"Spazio insufficiente sul disco di destinazione: servono circa {_fmt_bytes(new_bytes)}, "
            f"ne risultano liberi {_fmt_bytes(free)}."
        )
    if res["gia_presenti"]:
        warnings.append(f"{res['gia_presenti']} file sono già nella destinazione e verranno saltati.")
    if out.is_dir() and new_files and any(out.iterdir()):
        warnings.append("La cartella di destinazione non è vuota: vengono aggiunti solo i file mancanti, "
                        "quelli già presenti non vengono toccati.")
    if res["totale"] == 0:
        warnings.append("Il piano è vuoto: calcola prima il piano.")

    folders = conn.execute("SELECT COUNT(DISTINCT folder) FROM plan WHERE folder IS NOT NULL").fetchone()[0]
    dups = conn.execute(
        "SELECT COUNT(*) FROM plan WHERE folder IS NOT NULL AND COALESCE(is_dup,0) > 0"
    ).fetchone()[0]
    return {
        "ok": True,
        "total": res["totale"],
        "link": n_link,
        "copies": n_copy,
        "existing": res["gia_presenti"],
        "extra_bytes": 0 if hardlink else new_bytes,
        "same_volume": same_volume,
        "free_bytes": free,
        "output_dir": cfg.output_dir,
        "source_dir": cfg.source_dir,
        "link_mode": cfg.link_mode,
        "warnings": warnings,
        "folders": folders,
        "duplicates_to_folder": dups,
    }


@router.get("/apply/preview")
def apply_preview(conn: sqlite3.Connection = Depends(deps.get_db)):
    cfg = _effective_cfg(conn)
    key = (_plan_fingerprint(conn), cfg.source_dir, cfg.output_dir, cfg.link_mode)
    with _cache_lock:
        hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < PREVIEW_TTL:
        data = hit[1]
    else:
        try:
            data = _compute_preview(conn, cfg)
        except OSError as e:  # per esempio destinazione su un disco non disponibile
            data = {"ok": False, "error": f"Non riesco a leggere la destinazione: {e.strerror or e}"}
        with _cache_lock:
            _cache.clear()
            _cache[key] = (time.monotonic(), data)
    out = dict(data)
    out["warnings"] = list(data.get("warnings", []))
    out["fingerprint"] = key[0]  # il client la rimanda in POST /apply/run: se il piano cambia nel frattempo, si rifiuta
    if out.get("ok") and jobs.running_job(JOB_NAME):
        out["warnings"].append("Un'applicazione è già in corso.")
    return out


@router.get("/apply/suggest")
def apply_suggest(conn: sqlite3.Connection = Depends(deps.get_db)):
    """Destinazione proposta quando non ne e' ancora impostata: una cartella SULLO STESSO DISCO della sorgente
    (cosi' i file si collegano con gli hard link, senza occupare spazio). Non crea nulla."""
    cfg = _effective_cfg(conn)
    if not cfg.source_dir or not os.path.isdir(cfg.source_dir):
        return {"path": None, "drive": None, "exists": False, "same_volume": False}
    src = os.path.abspath(cfg.source_dir)
    anchor = Path(src).anchor or src
    path = Path(anchor) / SUGGEST_NAME
    return {"path": str(path), "drive": anchor, "exists": path.is_dir(), "same_volume": True}


@router.post("/apply/destination")
def apply_destination(body: dict | None = Body(default=None), conn: sqlite3.Connection = Depends(deps.get_db)):
    """Imposta la cartella di destinazione. Se non esiste la crea (vuota): e' l'unica scrittura fuori dal job,
    e avviene solo quando l'utente sceglie esplicitamente la cartella nella procedura."""
    raw = body.get("path") if isinstance(body, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(400, "Indica la cartella di destinazione.")
    if _run_lock.locked() or jobs.running_job(JOB_NAME):
        raise HTTPException(409, "Non puoi cambiare destinazione mentre la creazione è in corso.")
    path = Path(os.path.abspath(raw.strip()))
    cfg = _effective_cfg(conn)
    if cfg.source_dir and os.path.isdir(cfg.source_dir):
        src = Path(cfg.source_dir)
        if _inside(path, src) or _inside(src, path):
            raise HTTPException(400, "La destinazione non può essere dentro (o contenere) la cartella sorgente.")
    if not _nearest_existing(path).is_dir():
        raise HTTPException(400, "Il percorso indicato non è una cartella valida.")
    if not path.is_dir():
        try:
            path.mkdir(parents=True)
        except OSError as e:
            raise HTTPException(400, f"Non riesco a creare la cartella: {e.strerror or e}.")
    test = path / ".fotoscan-scrittura-test"
    try:  # verifica di scrittura: os.access non basta su Windows
        test.write_bytes(b"")
        test.unlink()
    except OSError:
        raise HTTPException(400, "La cartella di destinazione non è scrivibile: scegline un'altra.")
    _clear_cache()  # l'anteprima dipende anche da cosa c'e' gia' nella cartella
    return put_settings({"output_dir": str(path)}, conn)


def _run_job(job: jobs.Job, cfg) -> dict:
    """Corpo del job (thread). Il lock viene sempre rilasciato, anche se fallisce l'apertura del DB."""
    counts: dict[str, Any] = {}
    _live.update(job_id=job.id, counts=counts)
    last = {"t": 0.0, "n": -PROGRESS_EVERY}
    conn = None
    try:
        conn = deps.open_db()

        def progress(stage: str, done: int, total: int) -> None:
            # chiamato prima di ogni file: l'annullamento e' quindi rapido e lo stato resta coerente
            if job.cancel_requested:
                raise _Cancelled()
            if _TEST_DELAY and total:
                time.sleep(_TEST_DELAY)
            now = time.monotonic()
            if done >= total or done - last["n"] >= PROGRESS_EVERY or now - last["t"] >= PROGRESS_SECONDS:
                last.update(t=now, n=done)
                job.update(stage=stage, done=done, total=total, message=_counters_text(counts, done, total))

        try:
            organizer.apply_plan(conn, cfg, apply=True, progress=progress, result=counts)
        except _Cancelled:
            return {**counts, "annullato": True}
        return dict(counts)
    finally:
        if conn is not None:
            conn.close()
        _clear_cache()
        _run_lock.release()


@router.post("/apply/run")
def apply_run(body: dict | None = None):
    if not isinstance(body, dict) or body.get("confirm") is not True:
        raise HTTPException(400, "Conferma richiesta: invia {\"confirm\": true} per applicare il piano.")
    conn = deps.open_db()
    try:
        cfg = _effective_cfg(conn)
        # piano cambiato dopo l'anteprima mostrata all'utente: non si applica ci' che non e' stato confermato
        sent = body.get("fingerprint")
        if sent is not None and sent != _plan_fingerprint(conn):
            raise HTTPException(409, "Il piano è cambiato dopo l'anteprima. Controlla di nuovo le cartelle prima di crearle.")
    finally:
        conn.close()
    if not cfg.output_dir:
        raise HTTPException(400, "Cartella di destinazione non impostata.")
    try:
        organizer._check_dirs(cfg, need_output=True)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(409, "Un'applicazione è già in corso.")
    try:
        job = jobs.start_job(JOB_NAME, lambda j: _run_job(j, cfg))
    except Exception:
        _run_lock.release()
        raise
    return {"job_id": job.id}


@router.get("/apply/progress")
def apply_progress(job_id: str | None = None):
    """Contatori live (link, copie, gia' presenti, errori) dell'ultima applicazione, se corrisponde al job indicato."""
    if _live["job_id"] is None or (job_id and job_id != _live["job_id"]):
        return {"job_id": job_id, "counts": {}}
    return {"job_id": _live["job_id"], "counts": dict(_live["counts"])}


@router.get("/apply/log")
def apply_log(tail: int = 200):
    cfg = deps.get_cfg()
    tail = max(1, min(tail, 5000))
    counts = dict(_live["counts"])
    if not cfg.output_dir:
        return {"exists": False, "path": None, "lines": [], "counts": counts}
    path = Path(cfg.output_dir) / LOG_NAME
    if not path.is_file():
        return {"exists": False, "path": str(path), "lines": [], "counts": counts}
    with path.open(encoding="utf-8", errors="replace") as f:
        lines = deque((ln.rstrip("\r\n") for ln in f), maxlen=tail)
    return {"exists": True, "path": str(path), "lines": list(lines), "counts": counts}
