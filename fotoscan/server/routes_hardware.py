"""Computer dell'utente: configurazione rilevata, modello consigliato e velocita' di analisi (foto al secondo)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from .. import hardware
from ..config import AVAILABLE_MODELS, ROOT
from . import deps, jobs

router = APIRouter(prefix="/api", tags=["hardware"])

JOB_NAME = "hardware_bench"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _library(conn) -> dict:
    """Quanti file ci sono e quanti restano da analizzare (le foto vengono codificate una volta, i video su piu' fotogrammi)."""
    kinds = {r["kind"]: r["n"] for r in conn.execute("SELECT kind, COUNT(*) AS n FROM files GROUP BY kind")}
    pend = {r["kind"]: r["n"] for r in conn.execute(
        "SELECT kind, COUNT(*) AS n FROM files WHERE status IN ('new','meta') GROUP BY kind")}
    return {"photos": kinds.get("image", 0), "videos": kinds.get("video", 0),
            "pending_photos": pend.get("image", 0), "pending_videos": pend.get("video", 0)}


def _indexing_running() -> bool:
    from . import routes_index  # import locale per evitare cicli

    return routes_index._current_run() is not None


@router.get("/hardware")
def hardware_info(refresh: bool = Query(False), conn=Depends(deps.get_db)):
    from .routes_status import _model_cached

    cfg = deps.get_cfg()
    hw = hardware.detect(refresh)
    bench = hardware.load_bench(cfg.cache_dir, hw)
    cached = {m["id"]: _model_cached(m["id"]) for m in AVAILABLE_MODELS}
    report = hardware.build_report(hw, AVAILABLE_MODELS, _library(conn), bench, cached, fractions=len(cfg.video_fractions))
    running = jobs.running_job(JOB_NAME)
    return {
        "hardware": hw,
        **report,
        "benchmark": {"at": bench.get("at"), "stale": bool(bench.get("stale"))} if bench else None,
        "benchmark_job_id": running.id if running else None,
        "indexing_running": _indexing_running(),
    }


@router.post("/hardware/benchmark")
def hardware_benchmark(body: dict = Body(default={})):
    """Misura la velocita' reale su questo computer (sottoprocesso: la memoria video si libera a fine prova)."""
    if _indexing_running():
        raise HTTPException(409, "Ferma l'analisi prima di misurare la velocità: userebbero la scheda video insieme.")
    if jobs.running_job(JOB_NAME) is not None:
        raise HTTPException(409, "La misura è già in corso.")
    from .routes_status import _model_cached

    wanted = body.get("models")
    known = [m["id"] for m in AVAILABLE_MODELS]
    if wanted is None:
        wanted = [m for m in known if _model_cached(m)]    # nessun download a sorpresa: si misurano i modelli gia' scaricati
    if not isinstance(wanted, list) or not all(isinstance(x, str) and x in known for x in wanted):
        raise HTTPException(400, "Modelli non validi.")
    if not wanted:
        raise HTTPException(400, "Nessun modello scaricato da misurare: scarica prima un modello.")

    def _run(job):
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        proc = subprocess.Popen([sys.executable, "-m", "fotoscan.hardware", "bench", "--models", *wanted],
                                cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", creationflags=NO_WINDOW)
        job.update(stage="benchmark", done=0, total=len(wanted), message="Avvio della misura…")
        results: dict = {}
        try:
            for line in proc.stdout:                       # type: ignore[union-attr]
                if job.cancel_requested:
                    proc.terminate()
                    break
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("event") == "start":
                    job.update(done=ev["index"], total=ev["total"], message="Misuro la velocità del modello…")
                elif ev.get("event") == "result":
                    results[ev["model"]] = ev["result"]
                    job.update(done=len(results))
        finally:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        if proc.returncode not in (0, None) and not job.cancel_requested and not results:
            raise RuntimeError("La misura non è riuscita (controlla che PyTorch sia installato).")
        return {"results": results, "at": time.time()}

    job = jobs.start_job(JOB_NAME, _run)
    return {"job_id": job.id}
