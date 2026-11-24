"""Etichette suggerite (temi ricorrenti scoperti dalle foto) ed etichette da esempi accettate."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import numpy as np
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from .. import db as dbm
from .. import suggest
from . import deps, jobs

router = APIRouter(prefix="/api", tags=["labels"])
JOB = "suggerimenti"
NAME_MAX = 60


def _run(job):
    conn = deps.open_db()
    try:
        cfg = deps.get_cfg()
        items = suggest.discover(conn, cfg, None, progress=lambda m: job.update(stage="suggerimenti", message=m))
        return {"count": len(items)}
    finally:
        conn.close()


@router.post("/labels/suggestions/run")
def run_suggestions():
    """Avvia la ricerca dei temi ricorrenti (job in background, ~30 s)."""
    if jobs.running_job(JOB) is not None:
        raise HTTPException(409, "La ricerca dei temi e' gia' in corso.")
    job = jobs.start_job(JOB, _run)
    return {"job_id": job.id}


@router.get("/labels/suggestions")
def list_suggestions(conn: sqlite3.Connection = Depends(deps.get_db)):
    rows = conn.execute("SELECT id, name, alt, n, days, months, sample, created FROM label_suggestions "
                        "ORDER BY days DESC").fetchall()
    running = jobs.running_job(JOB)
    return {
        "items": [{"id": r["id"], "name": r["name"], "alt": r["alt"], "count": r["n"], "days": r["days"],
                   "months": r["months"], "sample": json.loads(r["sample"] or "[]")} for r in rows],
        "created": rows[0]["created"] if rows else None,
        "job_id": running.id if running else None,
    }


def _clean_name(conn: sqlite3.Connection, name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "Scrivi un nome per l'etichetta.")
    if len(name) > NAME_MAX:
        raise HTTPException(400, f"Il nome e' troppo lungo (massimo {NAME_MAX} caratteri).")
    if any(ch in name for ch in '<>:"/\\|?*'):
        raise HTTPException(400, 'Il nome non puo\' contenere i caratteri < > : " / \\ | ? *')
    taken = {l["name"].lower() for l in deps.get_cfg().labels}
    taken |= {r[0].lower() for r in conn.execute("SELECT name FROM visual_labels")}
    if name.lower() in taken:
        raise HTTPException(409, f"Esiste gia' un'etichetta «{name}».")
    return name


@router.post("/labels/suggestions/{sid}/accept")
def accept_suggestion(sid: int, body: dict = Body(default={}), conn: sqlite3.Connection = Depends(deps.get_db)):
    """Il suggerimento diventa un'etichetta da esempi (nome scelto dall'utente)."""
    r = conn.execute("SELECT * FROM label_suggestions WHERE id=?", (sid,)).fetchone()
    if r is None:
        raise HTTPException(404, "Suggerimento non trovato: cerca di nuovo i temi.")
    name = _clean_name(conn, body.get("name") or r["name"])
    conn.execute("INSERT INTO visual_labels(name, vec, thr, n, sample, created) VALUES(?,?,?,?,?,?)",
                 (name, r["vec"], r["thr"], r["n"], r["sample"], datetime.now().isoformat(timespec="seconds")))
    conn.execute("DELETE FROM label_suggestions WHERE id=?", (sid,))
    conn.commit()
    return {"name": name}


@router.post("/labels/suggestions/{sid}/dismiss")
def dismiss_suggestion(sid: int, conn: sqlite3.Connection = Depends(deps.get_db)):
    """Ignora il tema: non verra' piu' proposto."""
    r = conn.execute("SELECT vec FROM label_suggestions WHERE id=?", (sid,)).fetchone()
    if r is None:
        raise HTTPException(404, "Suggerimento non trovato.")
    conn.execute("INSERT INTO label_dismissed(vec) VALUES(?)", (r["vec"],))
    conn.execute("DELETE FROM label_suggestions WHERE id=?", (sid,))
    conn.commit()
    return {"ok": True}


def _preview_for_vector(conn: sqlite3.Connection, vec_blob: bytes, thr: float, sample_json: str | None,
                        limit: int, offset: int = 0) -> dict:
    """Foto più vicine al vettore dato, paginate (con fallback ai sample salvati se non ci sono embedding)."""
    vec = dbm.from_blob(vec_blob)
    ids, mat = dbm.load_embeddings(conn)
    fids: list[int] = []
    total = 0
    if len(ids) > 0 and mat.shape[1] == len(vec):
        sims = mat @ vec
        dup = {r[0] for r in conn.execute("SELECT file_id FROM plan WHERE is_dup>0")}
        imgs = {r[0] for r in conn.execute("SELECT id FROM files WHERE kind='image' AND status='embedded'")}
        mask = np.fromiter(((int(i) in imgs and int(i) not in dup) for i in ids), dtype=bool, count=len(ids))
        masked = np.where(mask, sims, -np.inf)
        above = np.flatnonzero(masked >= thr)
        total = int(len(above))
        pool = above if len(above) > 0 else np.flatnonzero(mask)
        if len(pool) > 0:
            order = pool[np.argsort(-masked[pool])]
            page = order[offset:offset + limit]
            fids = [int(ids[i]) for i in page]
    if not fids and offset == 0 and sample_json:
        all_samples = [int(x) for x in json.loads(sample_json)]
        fids = all_samples[offset:offset + limit]
        total = max(total, len(all_samples))
    if not fids:
        return {"items": [], "total": total, "threshold": round(float(thr), 3)}
    marks = ",".join("?" * len(fids))
    rows = {r["id"]: r for r in conn.execute(deps.SELECT_FILE + f"WHERE f.id IN ({marks})", fids)}
    items = [deps.file_to_dict(rows[fid]) for fid in fids if fid in rows]
    return {"items": items, "total": total, "threshold": round(float(thr), 3)}


@router.get("/labels/suggestions/dismissed")
def count_dismissed(conn: sqlite3.Connection = Depends(deps.get_db)):
    n = conn.execute("SELECT COUNT(*) FROM label_dismissed").fetchone()[0]
    return {"count": int(n)}


@router.delete("/labels/suggestions/dismissed")
def clear_dismissed(conn: sqlite3.Connection = Depends(deps.get_db)):
    cur = conn.execute("DELETE FROM label_dismissed")
    conn.commit()
    return {"cleared": int(cur.rowcount)}


@router.get("/labels/suggestions/{sid}/preview")
def preview_suggestion(
    sid: int,
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    r = conn.execute("SELECT vec, thr, sample FROM label_suggestions WHERE id=?", (sid,)).fetchone()
    if r is None:
        raise HTTPException(404, "Suggerimento non trovato.")
    return _preview_for_vector(conn, r["vec"], float(r["thr"] or 0.8), r["sample"], limit, offset)


@router.get("/labels/visual")
def list_visual(conn: sqlite3.Connection = Depends(deps.get_db)):
    rows = conn.execute("SELECT name, thr, n, sample, created FROM visual_labels ORDER BY name").fetchall()
    return [{"name": r["name"], "count": r["n"], "thr": round(float(r["thr"]), 3),
             "sample": json.loads(r["sample"] or "[]")[:4], "created": r["created"]} for r in rows]


@router.get("/labels/visual/{name}/preview")
def preview_visual(
    name: str,
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    r = conn.execute("SELECT vec, thr, sample FROM visual_labels WHERE name=?", (name,)).fetchone()
    if r is None:
        raise HTTPException(404, "Etichetta non trovata.")
    return _preview_for_vector(conn, r["vec"], float(r["thr"] or 0.8), r["sample"], limit, offset)


@router.delete("/labels/visual/{name}")
def delete_visual(name: str, conn: sqlite3.Connection = Depends(deps.get_db)):
    cur = conn.execute("DELETE FROM visual_labels WHERE name=?", (name,))
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "Etichetta non trovata.")
    return {"ok": True}


@router.put("/labels/visual/{name}")
def rename_visual(name: str, body: dict = Body(...), conn: sqlite3.Connection = Depends(deps.get_db)):
    row = conn.execute("SELECT thr FROM visual_labels WHERE name=?", (name,)).fetchone()
    if row is None:
        raise HTTPException(404, "Etichetta non trovata.")
    new = (body.get("name") if "name" in body else name) or ""
    new = new.strip()
    if new.lower() != name.lower():
        new = _clean_name(conn, new)
    elif not new:
        new = _clean_name(conn, new)
    thr = float(row["thr"])
    if "thr" in body and body["thr"] is not None:
        try:
            thr = float(body["thr"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "Soglia non valida.") from exc
        if not (0.65 <= thr <= 0.98):
            raise HTTPException(400, "La soglia deve essere tra 0,65 e 0,98.")
    conn.execute("UPDATE visual_labels SET name=?, thr=? WHERE name=?", (new, thr, name))
    conn.commit()
    return {"name": new}


# ------------------------------------------------------------ etichette di contesto (come album automatici)
CTX_CACHE_VERSION = 2  # 2: formula del piano + calibrazione per modello (i conteggi della v1 erano gonfiati)


def _ctx_sig(cfg) -> str:
    """Firma della calibrazione: i conteggi salvati valgono solo per lo stesso modello, temperatura e soglia."""
    return f"{cfg.model_name}|{cfg.context_temperature():.4f}|{cfg.score_threshold:.4f}"


def _ctx_cache_key(name: str) -> str:
    return f"ctx_cache:{name.strip().lower()}"


@router.get("/labels/contexts")
def list_context_labels(conn: sqlite3.Connection = Depends(deps.get_db)):
    """Tutte le etichette di contesto configurate, con conteggio e 4 miniature (dal piano o dalla cache live)."""
    cfg = deps.get_cfg()
    counts = {
        r["context"]: int(r["c"])
        for r in conn.execute("SELECT context, COUNT(*) AS c FROM plan WHERE context IS NOT NULL AND is_dup=0 GROUP BY context")
    }
    out = []
    for lbl in cfg.labels:
        name = lbl["name"]
        prompt = lbl.get("prompt", "")
        rows = conn.execute(
            "SELECT file_id FROM plan WHERE context=? AND is_dup=0 ORDER BY score DESC LIMIT 4",
            (name,),
        ).fetchall()
        cnt = counts.get(name, 0)
        sample = [int(r["file_id"]) for r in rows]
        ready = cnt > 0
        if not ready:
            raw = dbm.get_meta(conn, _ctx_cache_key(name))
            if raw:
                try:
                    cached = json.loads(raw)
                    if cached.get("prompt") == prompt and cached.get("v") == CTX_CACHE_VERSION                             and cached.get("sig") == _ctx_sig(cfg):
                        cnt = int(cached.get("count", 0))
                        sample = [int(x) for x in cached.get("sample", [])][:4]
                        ready = True
                except Exception:  # noqa: BLE001
                    pass
        out.append({
            "name": name,
            "prompt": prompt,
            "count": cnt,
            "in_plan": ready,
            "sample": sample,
        })
    return out


@router.get("/labels/contexts/{name}/preview")
def preview_context_label(
    name: str,
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    """Foto di un'etichetta di contesto: paginate dal piano oppure calcolate al volo con SigLIP2."""
    cfg = deps.get_cfg()
    lbl = next((l for l in cfg.labels if l["name"].lower() == name.lower()), None)
    if lbl is None:
        raise HTTPException(404, "Etichetta non trovata.")
    real_name = lbl["name"]
    total = int(conn.execute("SELECT COUNT(*) FROM plan WHERE context=? AND is_dup=0", (real_name,)).fetchone()[0])
    if total > 0:
        rows = conn.execute(
            deps.SELECT_FILE
            + "WHERE p.context=? AND p.is_dup=0 ORDER BY p.score DESC LIMIT ? OFFSET ?",
            (real_name, limit, offset),
        ).fetchall()
        return {"items": [deps.file_to_dict(r) for r in rows], "total": total, "live": False}
    # Etichetta nuova o non ancora nel piano: calcolo al volo completo con paginazione e salvataggio in cache.
    from . import routes_search  # import locale per evitare cicli

    from ..organizer import context_probs

    emb = routes_search._require_embedder()
    i_lbl = cfg.labels.index(lbl)
    # Stessa formula del piano (organizer.score_contexts): l'etichetta conta una foto solo se batte le altre
    # etichette e lo sfondo generico. Prima bastava battere lo sfondo: "Hot" prendeva oltre meta' dell'archivio.
    tv = routes_search.label_vectors(conn, [l["prompt"] for l in cfg.labels], emb, cfg)
    null_vecs = routes_search.label_vectors(conn, list(cfg.null_prompts), emb, cfg)
    ids, mat, is_video = routes_search._embedding_matrix(conn)
    if len(ids) == 0:
        return {"items": [], "total": 0, "live": True}
    if mat.shape[1] != tv.shape[1]:
        # modello selezionato diverso da quello con cui sono state analizzate le foto: niente errore tecnico di NumPy
        raise HTTPException(409, "Le foto sono state analizzate con un altro modello: rifai l'analisi "
                                 "(Impostazioni > Modello AI) prima di cercare con questa etichetta.")
    scale = float(dbm.get_meta(conn, "logit_scale", "100") or 100)
    p = context_probs(mat, tv, null_vecs, scale, cfg.context_temperature())
    prob = np.where(p.argmax(axis=1) == i_lbl, p[:, i_lbl], 0.0)
    mask = routes_search._candidate_mask(conn, ids, is_video, "")
    total_live, idx = routes_search._top(prob, mask, limit, offset, floor=cfg.score_threshold)
    items = routes_search._files_for(conn, [int(ids[i]) for i in idx], prob[idx])
    if offset == 0:
        dbm.set_meta(conn, _ctx_cache_key(real_name), json.dumps({
            "v": CTX_CACHE_VERSION,
            "sig": _ctx_sig(cfg),
            "prompt": lbl.get("prompt", ""),
            "count": int(total_live),
            "sample": [int(f["id"]) for f in items[:4]],
        }))
    return {"items": items, "total": int(total_live), "live": True}
