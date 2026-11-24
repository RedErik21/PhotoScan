"""Impostazioni > Aggiornamenti: stato, ricerca, download e approvazione dell'installazione.

L'installazione vera la fa il launcher (fotoscan.desktop) al prossimo avvio, prima di accendere il server:
qui si scarica e si prepara soltanto. Eventi SSE `update` (stesso contenuto di GET /api/update) quando
cambia qualcosa; il download e' un job (`update_download`) con avanzamento in byte.
"""
from __future__ import annotations

import logging
import threading
import time

from fastapi import APIRouter, HTTPException

from .. import updater
from ..config import FROZEN, ROOT, Config
from ..version import __version__, update_repo
from . import jobs
from .deps import get_cfg

log = logging.getLogger("fotoscan")
router = APIRouter(prefix="/api", tags=["update"])

JOB = "update_download"
SETTING_KEYS = ("update_check_on_start", "update_auto_download", "update_auto_install", "update_prerelease")
STARTUP_DELAY = 4.0  # secondi: il controllo all'avvio non rallenta l'apertura della finestra
_check_lock = threading.Lock()


def status(cfg: Config | None = None) -> dict:
    cfg = cfg or get_cfg()
    state = updater.read_state(cfg.cache_dir) or None
    latest = (state or {}).get("latest")
    pending = updater.read_pending(cfg.cache_dir)
    if pending and not updater.is_newer(pending.get("version") or "0", __version__):
        updater.discard_pending(cfg.cache_dir)   # gia' installato (o superato): non serve piu'
        pending = None
    job = jobs.running_job(JOB)
    return {
        "current": __version__,
        "repo": update_repo() or None,
        "configured": bool(update_repo()),
        "git_checkout": updater.is_git_checkout(ROOT),
        "frozen": FROZEN,
        "settings": {k: getattr(cfg, k) for k in SETTING_KEYS},
        "checking": _check_lock.locked(),
        "check": None if not state else {
            "checked_at": state.get("checked_at"),
            "error": state.get("error"),
            "latest": latest,
            # ricalcolato: dopo un'installazione la versione corrente non e' piu' quella del controllo
            "available": bool(latest and updater.is_newer(latest.get("version") or "0", __version__)),
        },
        "pending": None if not pending else {k: pending.get(k) for k in
                                             ("version", "name", "notes", "approved", "downloaded_at", "kind")},
        "exe_ready": _exe_ready(latest),
        "download_job": job.public() if job else None,
        "last_install": updater.read_last_install(cfg.cache_dir),
    }


def _publish(cfg: Config | None = None) -> dict:
    st = status(cfg)
    jobs.bus.publish("update", st)
    return st


def _start_download(cfg: Config, info: dict) -> jobs.Job:
    running = jobs.running_job(JOB)
    if running:
        return running

    def run(job: jobs.Job):
        def progress(done: int, total: int) -> None:
            job.update(stage="download", done=done, total=total,
                       message=f"Download di PhotoScan {info['version']}: {done / 1e6:.1f} MB")
        try:
            pending = updater.download(cfg.cache_dir, info, progress=progress, cancelled=lambda: job.cancel_requested,
                                       exe=FROZEN)
            job.update(stage="pronto", message=f"PhotoScan {pending['version']} pronto da installare")
            return {"version": pending["version"]}
        finally:
            _publish()

    return jobs.start_job(JOB, run)


def run_check(cfg: Config | None = None, auto: bool = False) -> dict:
    """Controlla GitHub; se c'e' una versione nuova e il download automatico e' attivo, la scarica."""
    cfg = cfg or get_cfg()
    if not _check_lock.acquire(blocking=False):
        return status(cfg)               # un controllo e' gia' in corso
    try:
        jobs.bus.publish("update", {**status(cfg), "checking": True})
        state = updater.check(cfg.cache_dir, prerelease=cfg.update_prerelease)
    finally:
        _check_lock.release()
    latest = state.get("latest")
    pending = updater.read_pending(cfg.cache_dir)
    if (state.get("available") and cfg.update_auto_download and not _self_update_off() and _exe_ready(latest)
            and not (pending and pending.get("version") == latest.get("version"))):
        log.info("aggiornamento %s disponibile: download automatico", latest.get("version"))
        _start_download(cfg, latest)
    elif auto and state.get("error"):
        log.info("controllo aggiornamenti all'avvio: %s", state["error"])
    return _publish(cfg)


def startup_check() -> None:
    """Chiamato dal server all'avvio (fotoscan.server.__main__): controllo in un thread, se attivo."""
    def run() -> None:
        time.sleep(STARTUP_DELAY)
        try:
            cfg = get_cfg()
            if cfg.update_check_on_start and update_repo():
                run_check(cfg, auto=True)
        except Exception as e:  # noqa: BLE001 - mai bloccare l'avvio per un aggiornamento
            log.warning("controllo aggiornamenti all'avvio non riuscito: %s", e)

    threading.Thread(target=run, daemon=True, name="update-check").start()


def _self_update_off() -> bool:
    """In una copia git l'app non sostituisce i propri file (si usa git pull). L'eseguibile si aggiorna da solo."""
    return updater.is_git_checkout(ROOT)


def _self_update_msg() -> str:
    return "Questa copia di PhotoScan viene da git: aggiornala con «git pull»."


def _exe_ready(info: dict | None) -> bool:
    """Nell'eseguibile serve l'asset Windows della Release, altrimenti non c'e' nulla da installare."""
    return not FROZEN or bool(info and info.get("exe_assets"))


# ------------------------------------------------------------------ API
@router.get("/update")
def update_status():
    return status()


@router.post("/update/check")
def update_check():
    if not update_repo():
        raise HTTPException(400, "Aggiornamenti non configurati in questa copia di PhotoScan.")
    return run_check()


@router.post("/update/download")
def update_download():
    cfg = get_cfg()
    if _self_update_off():
        raise HTTPException(409, _self_update_msg())
    st = status(cfg)
    if not (st["check"] and st["check"]["available"]):
        raise HTTPException(400, "Nessun aggiornamento disponibile. Cerca prima gli aggiornamenti.")
    job = _start_download(cfg, st["check"]["latest"])
    return {"job_id": job.id}


@router.post("/update/install")
def update_install():
    """Approva l'installazione dell'aggiornamento scaricato: avviene al prossimo avvio di PhotoScan."""
    cfg = get_cfg()
    if _self_update_off():
        raise HTTPException(409, _self_update_msg())
    try:
        updater.set_approved(cfg.cache_dir, True)
    except updater.UpdateError as e:
        raise HTTPException(400, str(e)) from e
    return _publish(cfg)


@router.delete("/update/pending")
def update_discard():
    cfg = get_cfg()
    job = jobs.running_job(JOB)
    if job:
        job.cancel_requested = True
    updater.discard_pending(cfg.cache_dir)
    return _publish(cfg)
