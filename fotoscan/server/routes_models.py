"""Modelli AI per esperti: calibrazioni (preset e sostituzioni), modelli personalizzati, calibrazione automatica.

Le sostituzioni e i modelli aggiunti vivono in config.json (Config.model_overrides / custom_models) e si applicano
subito: ricerca (routes_search.search_thresholds) e contesti (Config.context_temperature) leggono config.model_profile.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from .. import db as dbm
from ..config import (BASE_SEARCH_THR, ROOT, SEARCH_THR_KEYS, SUPPORTED_MODEL_TYPES, all_models, model_profile,
                      preset_for, preset_profile)
from ..calibrate import REFERENCE_MODEL
from . import deps, jobs

router = APIRouter(prefix="/api", tags=["models"])

JOB_NAME = "model_calibration"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][\w.\-]{0,95}/[\w.\-]{1,96}$")
# Limiti dei valori modificabili (oltre, ricerca o cartelle smettono di avere senso).
LIMITS = {"temp_factor": (0.2, 3.0), "min_match": (0.0, 0.6), "min_gap": (0.0, 0.5),
          "level_good": (0.0, 0.8), "level_ok": (0.0, 0.8)}
_running_model: dict[str, str] = {}  # job id -> modello in calibrazione
_last_result: dict[str, Any] = {}   # ultimo risultato di calibrazione per modello (proposta, non ancora applicata)


def _save(**changes) -> None:
    from .routes_status import _save_lock, _write_config  # import locale per evitare cicli

    with _save_lock:
        _write_config(dataclasses.replace(deps.get_cfg(), **changes))


def _entry(m: dict, cfg, cached: bool, embedding_model: str | None) -> dict:
    eff = model_profile(m["id"], cfg)
    return {
        "id": m["id"], "label": m.get("label") or m["id"], "hint": m.get("hint", ""),
        "tested": m.get("tested", False), "custom": m.get("custom", False),
        "calibrated": bool((preset_for(m["id"]) or {}).get("calibrated")), "cached": cached,
        "current": m["id"] == cfg.model_name, "in_catalog": m["id"] == embedding_model,
        "preset": preset_profile(m["id"]), "override": cfg.model_overrides.get(m["id"]),
        "effective": {"temp_factor": eff["temp_factor"], "search_thr": eff["search_thr"], "source": eff["source"],
                      "temperature": round(max(cfg.score_temperature * eff["temp_factor"], 0.1), 3)},
        "proposal": _last_result.get(m["id"]),
    }


@router.get("/models")
def list_models(conn=Depends(deps.get_db)):
    from .. import netguard

    cfg = deps.get_cfg()
    emb_model = dbm.get_meta(conn, "embedding_model")
    running = jobs.running_job(JOB_NAME)
    return {
        "current": cfg.model_name, "embedding_model": emb_model,
        "score_temperature": cfg.score_temperature, "score_threshold": cfg.score_threshold,
        "base_thresholds": dict(BASE_SEARCH_THR), "limits": LIMITS,
        "supported_types": list(SUPPORTED_MODEL_TYPES),
        "models": [_entry(m, cfg, netguard.model_in_cache(m["id"]), emb_model) for m in all_models(cfg)],
        "reference_model": REFERENCE_MODEL,
        "calibration_job_id": running.id if running else None,
        "calibration_model": _running_model.get(running.id) if running else None,
    }


def _check_profile(body: dict) -> dict:
    out: dict[str, Any] = {}
    if body.get("temp_factor") is not None:
        out["temp_factor"] = _num("temp_factor", body["temp_factor"])
    thr = body.get("search_thr") or {}
    if not isinstance(thr, dict):
        raise HTTPException(400, "Soglie di ricerca non valide.")
    clean = {k: _num(k, v) for k, v in thr.items() if k in SEARCH_THR_KEYS and v is not None}
    if clean:
        out["search_thr"] = clean
    if not out:
        raise HTTPException(400, "Nessun valore da salvare.")
    return out


def _num(key: str, v: Any) -> float:
    lo, hi = LIMITS[key]
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, f"Valore non numerico per {key}.")
    if not (lo <= x <= hi):
        raise HTTPException(400, f"{key} deve essere tra {lo} e {hi}.")
    return round(x, 4)


@router.put("/models/profile")
def put_profile(body: dict = Body(...)):
    """Sostituisce la calibrazione di un modello (anche solo alcune chiavi). source: manuale | automatica."""
    cfg = deps.get_cfg()
    mid = body.get("id")
    if mid not in {m["id"] for m in all_models(cfg)}:
        raise HTTPException(404, "Modello sconosciuto.")
    ov = _check_profile(body)
    full = {**preset_profile(mid)["search_thr"], **(cfg.model_overrides.get(mid, {}).get("search_thr") or {}),
            **ov.get("search_thr", {})}
    if not (full["min_match"] <= full["level_good"] <= full["level_ok"]):
        raise HTTPException(400, "Le soglie devono essere in ordine: minima ≤ buona ≤ ottima.")
    ov["search_thr"] = full
    ov.setdefault("temp_factor", model_profile(mid, cfg)["temp_factor"])
    ov["source"] = "automatica" if body.get("source") == "automatica" else "manuale"
    ov["at"] = time.time()
    _save(model_overrides={**cfg.model_overrides, mid: ov})
    if ov["source"] == "automatica":
        _last_result.pop(mid, None)
    return {"ok": True, "profile": model_profile(mid, deps.get_cfg())}


@router.delete("/models/profile")
def reset_profile(id: str = Query(...)):
    """Torna alla calibrazione di fabbrica (preset del modello testato o valori di base)."""
    cfg = deps.get_cfg()
    if id in cfg.model_overrides:
        _save(model_overrides={k: v for k, v in cfg.model_overrides.items() if k != id})
    return {"ok": True, "profile": model_profile(id, deps.get_cfg())}


def _inspect_model(mid: str) -> str:
    """model_type del modello (scarica solo config.json da Hugging Face, o lo legge dalla cache)."""
    try:
        from transformers import AutoConfig

        from .. import netguard

        conf = netguard.load_pretrained(AutoConfig.from_pretrained, mid)
    except Exception as e:
        msg = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
        raise HTTPException(400, f"Modello non trovato su Hugging Face o non leggibile: {msg}")
    mtype = str(getattr(conf, "model_type", ""))
    if mtype not in SUPPORTED_MODEL_TYPES or not hasattr(conf, "vision_config") or not hasattr(conf, "text_config"):
        raise HTTPException(400, f"Tipo di modello «{mtype or '?'}» non supportato: servono modelli testo-immagine "
                                 f"({', '.join(SUPPORTED_MODEL_TYPES)}).")
    return mtype


@router.post("/models/custom")
def add_custom(body: dict = Body(...)):
    cfg = deps.get_cfg()
    mid = str(body.get("id") or "").strip()
    if not MODEL_ID_RE.match(mid):
        raise HTTPException(400, "Scrivi l'identificativo Hugging Face nella forma «autore/nome-modello».")
    if mid in {m["id"] for m in all_models(cfg)}:
        raise HTTPException(409, "Il modello è già nell'elenco.")
    mtype = _inspect_model(mid)
    label = str(body.get("label") or "").strip()[:80] or mid.split("/", 1)[1]
    hint = "Modello aggiunto da te: non testato, calibralo sul tuo archivio."
    if mtype == "clip":
        hint += " I modelli CLIP originali capiscono solo l'inglese: scrivi etichette e ricerche in inglese."
    entry = {"id": mid, "label": label, "hint": hint, "model_type": mtype}
    _save(custom_models=[*deps.get_cfg().custom_models, entry])
    return entry


@router.delete("/models/custom")
def remove_custom(id: str = Query(...)):
    cfg = deps.get_cfg()
    if id == cfg.model_name:
        raise HTTPException(409, "È il modello in uso: scegline un altro prima di toglierlo.")
    if id not in {c.get("id") for c in cfg.custom_models}:
        raise HTTPException(404, "Modello personalizzato non trovato.")
    _save(custom_models=[c for c in cfg.custom_models if c.get("id") != id],
          model_overrides={k: v for k, v in cfg.model_overrides.items() if k != id})
    _last_result.pop(id, None)
    return {"ok": True}


@router.post("/models/calibrate")
def calibrate(body: dict = Body(...)):
    """Calibrazione automatica sul campione dell'archivio (sottoprocesso). Il risultato e' una proposta: si applica
    con PUT /models/profile (source=automatica)."""
    from .routes_hardware import _indexing_running

    cfg = deps.get_cfg()
    mid = body.get("id")
    if mid not in {m["id"] for m in all_models(cfg)}:
        raise HTTPException(404, "Modello sconosciuto.")
    if _indexing_running():
        raise HTTPException(409, "Ferma l'analisi prima di calibrare: userebbero la scheda video insieme.")
    n = body.get("n")
    if n is not None and n != "":
        if isinstance(n, bool) or not isinstance(n, (int, float, str)):
            raise HTTPException(400, "Il numero di foto del campione non è valido.")
        try:
            n = min(max(int(n), 300), 5000)
        except (TypeError, ValueError):
            raise HTTPException(400, "Il numero di foto del campione deve essere un numero intero.")
    else:
        n = None     # None: lo decide calibrate (3000 con GPU, 1000 su CPU)

    def _run(job):
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        args = [sys.executable, "-m", "fotoscan.calibrate", "--model", mid] + (["--n", str(n)] if n else [])
        proc = subprocess.Popen(args,
                                cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", creationflags=NO_WINDOW)
        job.update(stage="calibration", done=0, total=2, message="Avvio della calibrazione…")
        result, error = None, None
        try:
            for line in proc.stdout:                       # type: ignore[union-attr]
                if job.cancel_requested:
                    proc.terminate()
                    break
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("event") == "stage":
                    job.update(done=ev.get("done"), total=ev.get("total"), message=ev.get("message"))
                elif ev.get("event") == "result":
                    result = ev["result"]
                elif ev.get("event") == "error":
                    error = ev.get("message")
        finally:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        if job.cancel_requested:
            return None
        if result is None:
            raise RuntimeError(error or "La calibrazione non è riuscita (controlla che PyTorch sia installato).")
        _last_result[mid] = result
        return result

    job, created = jobs.start_job_unique(JOB_NAME, _run, conflicts=("hardware_bench",))
    if not created:
        raise HTTPException(409, "C'è già una misura o una calibrazione in corso.")
    _running_model[job.id] = mid
    for old in [k for k in _running_model if k != job.id and jobs.get_job(k) is None]:
        _running_model.pop(old, None)   # job conclusi e ormai dimenticati dal registro dei job
    return {"job_id": job.id}
