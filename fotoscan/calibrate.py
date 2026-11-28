"""Calibrazione automatica di un modello sul proprio archivio (Impostazioni > Per esperti).

Riferimento: SigLIP 2 Base, le cui soglie sono tarate a mano (temp_factor 1, config.BASE_SEARCH_THR). Sulle stesse foto
(campione casuale del catalogo) si calcolano gli embedding con Base e con il modello da calibrare, poi:
  - temp_factor: il fattore di temperatura con cui il modello assegna ai contesti lo stesso numero di foto di Base
    (stesse etichette, stessa soglia score_threshold);
  - min_match / level_good / level_ok: quantile matching. Se con Base il 2% delle coppie foto-ricerca supera 0,120,
    la soglia "buona" del modello e' il valore superato dal 2% delle sue coppie;
  - min_gap (stacco del migliore dalla media dell'archivio): sull'archivio intero, se il catalogo e' gia' analizzato con
    il modello, fra le ricerche vere e le frasi senza senso; altrimenti stimato dal rapporto degli stacchi (meno sicuro).
Verificato a posteriori su SO400M (3000 foto): ritrova 0,86 (a mano 0,85) e 0,100 / 0,114 / 0,125 (identiche).

Gira come sottoprocesso (`python -m fotoscan.calibrate --model ID`): la memoria video si libera a fine calibrazione.
Scrive una riga JSON per evento: {"event": "stage", ...}, {"event": "result", "result": {...}}, {"event": "error", ...}.
Il catalogo si apre in sola lettura.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .config import BASE_SEARCH_THR, Config, load_config
from .organizer import context_probs

REFERENCE_MODEL = "google/siglip2-base-patch16-224"
# Campione: con 1000 foto il fattore di temperatura oscilla di +-0,05 fra due prove (solo ~18% riceve un'etichetta);
# 3000 lo stabilizza. Sul processore si resta a 1000 (il modello da calibrare va calcolato da capo).
SAMPLE_GPU, SAMPLE_CPU = 3000, 1000
# Ricerche tipiche (devono trovare qualcosa in un archivio di famiglia) e frasi senza senso (non devono trovare nulla).
REAL_QUERIES = [
    "tramonto", "cane", "gatto", "montagna", "mare", "spiaggia", "neve", "torta di compleanno", "scontrino",
    "bambini che giocano", "automobile", "bicicletta", "fiori", "albero di Natale", "pizza", "chiesa", "concerto",
    "piscina", "matrimonio", "screenshot", "documento", "cena con amici", "città di notte", "lago", "cielo azzurro",
    "selfie", "barca", "aeroplano", "pasta", "palestra",
]
JUNK_QUERIES = [
    "xqzwv blorf", "asdfgh", "lorem ipsum dolor", "qwerty uiop", "zzzzzz", "blarg fnord", "kjhgfd", "pppppp",
    "xyz123 abc", "mnbvcx", "vbnm tyui", "grubnik", "wxqz", "fhqwhgads", "jjjjj kkkkk", "ploof zarg", "qqq www",
    "trzx plim", "aaaa bbbb", "nmnm",
]


def emit(**ev) -> None:
    print(json.dumps(ev, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------- calcoli puri (testabili senza modelli)
def coverage(mat: np.ndarray, tv: np.ndarray, nv: np.ndarray, scale: float, temperature: float, threshold: float):
    """(quante foto hanno un contesto, etichetta vincente per foto, maschera assegnate)."""
    p = context_probs(mat, tv, nv, scale, temperature)
    win = p.argmax(1)
    ok = p.max(1) >= threshold
    return int(ok.sum()), win, ok


def fit_temp_factor(ref: dict, tgt: dict, temperature: float, threshold: float) -> dict:
    """ref/tgt = {mat, lab, nul, scale}. Fattore che fa coincidere la copertura del modello con quella di riferimento."""
    c_ref, w_ref, ok_ref = coverage(ref["mat"], ref["lab"], ref["nul"], ref["scale"], temperature, threshold)
    best = None
    for f in np.round(np.arange(0.20, 3.001, 0.01), 2):
        c, w, ok = coverage(tgt["mat"], tgt["lab"], tgt["nul"], tgt["scale"], temperature * f, threshold)
        if best is None or abs(c - c_ref) < abs(best[1] - c_ref):
            best = (float(f), c, w, ok)
    f, c, w, ok = best
    agree = float(((w == w_ref) & ok & ok_ref).sum() / max(int(ok_ref.sum()), 1))
    return {"temp_factor": f, "coverage_ref": c_ref, "coverage": c, "agreement": round(agree, 3), "sample": len(ref["mat"])}


def fit_levels(ref_sims: np.ndarray, sims: np.ndarray, ref_thr: dict = BASE_SEARCH_THR) -> dict:
    """Quantile matching delle soglie assolute (coppie foto x ricerca dello stesso campione)."""
    out = {}
    for k in ("min_match", "level_good", "level_ok"):
        frac = float((ref_sims >= ref_thr[k]).mean())
        out[k] = round(float(np.quantile(sims, 1.0 - frac)), 4) if 0 < frac < 1 else ref_thr[k]
    out["level_good"] = max(out["level_good"], out["min_match"])
    out["level_ok"] = max(out["level_ok"], out["level_good"])
    return out


def gaps(mat: np.ndarray, qvecs: np.ndarray) -> np.ndarray:
    """Stacco del migliore dalla media dell'archivio, per ricerca (come routes_search.search)."""
    s = mat @ qvecs.T
    return s.max(0) - s.mean(0)


def fit_min_gap(ratio_est: float, real_gaps: np.ndarray | None, junk_gaps: np.ndarray | None) -> dict:
    """Soglia di stacco: la stima dal rapporto, portata fra le frasi senza senso (90%) e le ricerche vere (10%)."""
    if real_gaps is None or junk_gaps is None:
        return {"min_gap": round(ratio_est, 3), "gap_estimated": True}
    lo, hi = float(np.quantile(junk_gaps, 0.9)), float(np.quantile(real_gaps, 0.1))
    t = min(max(ratio_est, lo), hi) if lo < hi else (lo + hi) / 2
    t = round(t, 3)
    return {"min_gap": t, "gap_estimated": False,
            "real_kept": int((real_gaps >= t).sum()), "real_total": int(len(real_gaps)),
            "junk_rejected": int((junk_gaps < t).sum()), "junk_total": int(len(junk_gaps))}


# ---------------------------------------------------------------- esecuzione con i modelli
def _sample(conn, n: int) -> list[tuple[int, str]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT f.id, f.path FROM files f WHERE f.kind='image' AND f.status='embedded' ORDER BY RANDOM() LIMIT ?", (n,))]


def _encode(cfg: Config, model_id: str, rows, labels, nulls, queries, device: str, workers: int, need_images: bool):
    from dataclasses import replace

    from .embedder import Embedder

    emb = Embedder(replace(cfg, model_name=model_id), device=device)
    out = {"scale": emb.logit_scale}
    if need_images:
        def prep(r):
            try:
                return r[0], emb.load_prep(r[1])
            except Exception:
                return r[0], None

        with ThreadPoolExecutor(max(1, workers)) as ex:
            ok = [x for x in ex.map(prep, rows) if x[1] is not None]
        out["ids"] = [i for i, _ in ok]
        out["mat"] = emb.encode_arrays([a for _, a in ok], batch=min(cfg.batch_size, 32))
    out["lab"] = emb.encode_texts(labels)
    out["nul"] = emb.encode_texts(nulls)
    out["q"] = np.stack([emb.encode_query(t) for t in queries])
    del emb
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    return out


def run(cfg: Config, model_id: str, n: int | None = None) -> dict:
    from . import db as dbm

    if model_id == REFERENCE_MODEL:
        raise ValueError("SigLIP 2 Base è il riferimento della calibrazione: i suoi valori sono già tarati a mano.")
    device = cfg.resolve_device()
    n = n or (SAMPLE_GPU if device == "cuda" else SAMPLE_CPU)
    conn = sqlite3.connect(Path(cfg.db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=60)  # as_uri: '#', '?', '%'
    conn.row_factory = sqlite3.Row
    try:
        rows = _sample(conn, n)
        if len(rows) < 200:
            raise ValueError("Servono almeno 200 foto analizzate nel catalogo per calibrare un modello.")
        in_catalog = dbm.get_meta(conn, "embedding_model") == model_id
        full = dbm.load_embeddings(conn) if in_catalog else None
    finally:
        conn.close()
    labels = [x["prompt"] for x in cfg.labels]
    nulls = list(cfg.null_prompts)
    queries = labels + REAL_QUERIES

    emit(event="stage", stage="reference", done=0, total=2,
         message=f"Analizzo {len(rows)} foto con il modello di riferimento (SigLIP 2 Base)…")
    ref = _encode(cfg, REFERENCE_MODEL, rows, labels, nulls, queries, device, cfg.workers, need_images=True)
    emit(event="stage", stage="target", done=1, total=2, message="Analizzo le stesse foto con il modello da calibrare…")
    tgt = _encode(cfg, model_id, rows, labels, nulls, queries + JUNK_QUERIES, device, cfg.workers, need_images=not in_catalog)
    if in_catalog and full[1].shape[1] != tgt["lab"].shape[1]:
        raise ValueError("Il catalogo contiene embedding di un altro modello: rifai l'analisi prima di calibrare.")
    if in_catalog:
        ids, mat = full
        pos = {int(i): k for k, i in enumerate(ids)}
        keep = [i for i in ref["ids"] if i in pos]
        rpos = {i: k for k, i in enumerate(ref["ids"])}
        ref_rows = [rpos[i] for i in keep]
        ref["mat"] = ref["mat"][ref_rows]
        tgt["mat"] = mat[[pos[i] for i in keep]]
    if ref["mat"].shape[0] < 200:
        raise ValueError("Troppe foto illeggibili nel campione: calibrazione non affidabile.")
    emit(event="stage", stage="fit", done=2, total=2, message="Calcolo le soglie…")

    nq = len(queries)
    tq = tgt["q"][:nq]
    temp = fit_temp_factor(ref, tgt, cfg.score_temperature, cfg.score_threshold)
    levels = fit_levels(ref["mat"] @ ref["q"].T, tgt["mat"] @ tq.T)
    ratio = float(np.median(gaps(tgt["mat"], tq)) / max(np.median(gaps(ref["mat"], ref["q"])), 1e-6))
    if in_catalog:
        g = fit_min_gap(BASE_SEARCH_THR["min_gap"] * ratio, gaps(full[1], tgt["q"][len(labels):nq]), gaps(full[1], tgt["q"][nq:]))
    else:
        g = fit_min_gap(BASE_SEARCH_THR["min_gap"] * ratio, None, None)
    search_thr = {"min_match": levels["min_match"], "min_gap": g.pop("min_gap"),
                  "level_good": levels["level_good"], "level_ok": levels["level_ok"]}
    return {"model": model_id, "temp_factor": temp["temp_factor"], "search_thr": search_thr,
            "check": {**{k: v for k, v in temp.items() if k != "temp_factor"}, **g, "full_archive": in_catalog},
            "at": time.time()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m fotoscan.calibrate")
    ap.add_argument("--model", required=True)
    ap.add_argument("--n", type=int, default=None)
    a = ap.parse_args(argv)
    try:
        emit(event="result", result=run(load_config(), a.model, a.n))
        return 0
    except Exception as e:  # messaggio leggibile per la UI
        emit(event="error", message=str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
