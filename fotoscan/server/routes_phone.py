"""Importazione dal telefono (Android via MTP): elenco dispositivi/cartelle e copia delle cartelle scelte.

Sola lettura sul telefono. La destinazione puo' essere dentro la cartella delle foto (scelta esplicita):
i file esistenti non vengono mai sovrascritti.
"""
from __future__ import annotations

import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from .. import phone_log, phone_mtp
from . import deps, jobs

router = APIRouter(prefix="/api/phone", tags=["phone"])

JOB_NAME = "phone_import"
PROGRESS_SECONDS = 0.5


def _run_import(job, body: dict, dest: Path):
    last = {"t": 0.0}
    when = datetime.now().isoformat(timespec="seconds")

    def progress(done: int, total: int, rel: str, count: int, n: int) -> None:
        # aggiornamento al massimo ogni 0.5 s (o all'ultimo file): evita di inondare SSE e interfaccia
        now = time.monotonic()
        if count < n and now - last["t"] < PROGRESS_SECONDS:
            return
        last["t"] = now
        job.update(stage="copia", done=done, total=total, message=f"{count} di {n} file · {rel}")

    res = None
    err = ""
    try:
        res = phone_mtp.copy_folders(
            device=body["device"],
            storage=body["storage"],
            folders=body["folders"],
            dest=dest,
            progress=progress,
            cancelled=lambda: job.cancel_requested,
            skip=set(body.get("skip") or []),
        )
        return asdict(res)
    except Exception as e:  # noqa: BLE001 - si registra nello storico e poi si rilancia
        err = f"{type(e).__name__}: {e}"
        raise
    finally:
        state = "error" if err else ("cancelled" if job.cancel_requested else "done")
        conn = deps.open_db()
        try:
            phone_log.record_import(
                conn, body["device"],
                res.new_files if res else 0, res.total_seen if res else 0,
                when=when, state=state, storage=body["storage"], folders=["/".join(f) for f in body["folders"]],
                dest=str(dest), skipped_known=res.skipped_known if res else 0,
                skipped_existing=res.skipped_existing if res else 0,
                errors=len(res.errors) if res else 0, error=err)
        finally:
            conn.close()


@router.get("/history")
def history(conn=Depends(deps.get_db)):
    """Storico delle importazioni dal telefono, la piu' recente per prima."""
    return {"items": [asdict(r) for r in phone_log.history(conn)]}


@router.get("/devices")
def devices():
    """Telefoni collegati e storage. Vuoto se il telefono non e' collegato o non e' sbloccato."""
    try:
        return {"devices": phone_mtp.list_devices()}
    except phone_mtp.PhoneError as e:
        raise HTTPException(503, str(e))


@router.get("/folder")
def folder(device: str = Query(...), storage: str = Query(...), path: list[str] = Query(default=[])):
    """Contenuto di una cartella del telefono: sottocartelle con numero di foto/video."""
    try:
        return phone_mtp.list_folder(device, storage, path)
    except phone_mtp.PhoneError as e:
        raise HTTPException(503, str(e))


def _dedupe_folders(folders: list[list[str]]) -> list[list[str]]:
    """Toglie le cartelle gia' coperte da un'altra scelta (la sottocartella di una cartella scelta e' inclusa).
    Evita di contare e copiare due volte gli stessi file."""
    keep: list[list[str]] = []
    for f in sorted(folders, key=len):
        if any(f[:len(k)] == k for k in keep):
            continue
        keep.append(f)
    return keep


def _check_selection(body: dict) -> tuple[str, str, list[list[str]]]:
    device, storage, folders = body.get("device"), body.get("storage"), body.get("folders")
    if not isinstance(device, str) or not device or not isinstance(storage, str) or not storage:
        raise HTTPException(400, "Scegli telefono e memoria.")
    if not isinstance(folders, list) or not folders or not all(
            isinstance(f, list) and f and all(isinstance(x, str) and x for x in f) for f in folders):
        raise HTTPException(400, "Scegli almeno una cartella da importare.")
    return device, storage, _dedupe_folders(folders)


@router.post("/scan")
def scan(body: dict = Body(...)):
    """Anteprima prima di copiare: quanti file sono nuovi e quanti risultano gia' presenti
    (stesso nome e stessa dimensione nel catalogo o nella destinazione). Nessun file viene copiato."""
    device, storage, folders = _check_selection(body)
    dest_raw = body.get("dest")
    dest = Path(dest_raw.strip()).expanduser() if isinstance(dest_raw, str) and dest_raw.strip() else None
    try:
        items = phone_mtp.scan_folders(device, storage, folders)
    except phone_mtp.PhoneError as e:
        raise HTTPException(503, str(e))
    conn = deps.open_db()
    try:
        catalog, dest_keys = phone_mtp.existing_keys(conn, dest)
    finally:
        conn.close()
    parts = phone_mtp.classify(items, catalog, dest_keys)
    new_bytes = sum(max(0, int(i.get("size", 0))) for i in parts["new"])
    return {
        "total": len(items),
        "new": len(parts["new"]),
        "new_bytes": new_bytes,
        "known": len(parts["known"]),
        # Nessun limite: il client rimanda questa lista come `skip`, quindi deve essere completa.
        "known_items": [{"rel": k["rel"], "name": k["name"], "size": k["size"], "reason": k["reason"]}
                        for k in parts["known"]],
    }


@router.post("/import")
def start_import(body: dict = Body(...)):
    """Avvia la copia in background. body: device, storage, folders (liste di nomi), dest (cartella), skip."""
    device, storage, folders = _check_selection(body)
    dest_raw = body.get("dest")
    if not isinstance(dest_raw, str) or not dest_raw.strip():
        raise HTTPException(400, "Scegli la cartella di destinazione.")

    # Eccezione voluta: la destinazione puo' stare dentro la cartella delle foto (es. Foto\Pixel).
    # Restano le protezioni: nessun file esistente viene sovrascritto (duplicati saltati, conflitti rinominati)
    # e il telefono e' sempre solo letto.
    dest = Path(dest_raw.strip()).expanduser()
    if jobs.running_job(JOB_NAME):
        raise HTTPException(409, "Un'importazione dal telefono e' gia' in corso.")

    skip = body.get("skip") or []
    if not isinstance(skip, list) or not all(isinstance(x, str) for x in skip):
        raise HTTPException(400, "Elenco dei file da saltare non valido.")
    clean = {"device": device, "storage": storage, "folders": folders, "skip": skip}
    job = jobs.start_job(JOB_NAME, lambda j: _run_import(j, clean, dest))
    return job.public()
