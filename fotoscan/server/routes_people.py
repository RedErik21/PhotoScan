"""Persone e volti: elenco persone, foto per persona, ritaglio volto, rinomina, unione, assegnazione, esclusione, recluster."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from PIL import Image, ImageOps

from .. import db as dbm
from .. import faces as faces_mod
from . import deps, jobs, routes_index

router = APIRouter(prefix="/api", tags=["people"])

NAME_MAX = 80
MAX_PAGE = 500


def _person_row(conn: sqlite3.Connection, person_id: int) -> sqlite3.Row:
    r = conn.execute("SELECT id, name, n, cover_face_id, hidden, created FROM persons WHERE id=?", (person_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "Persona non trovata.")
    return r


@router.get("/people")
def list_people(include_hidden: bool = Query(False), conn: sqlite3.Connection = Depends(deps.get_db)):
    """Persone riconosciute ordinate per numero di foto distinte. Vuoto se enable_faces e' spento."""
    if not deps.get_cfg().enable_faces:
        return []
    where_hidden = "" if include_hidden else "AND COALESCE(p.hidden,0)=0"
    rows = conn.execute(
        f"""
        SELECT p.id, p.name, p.cover_face_id, COALESCE(p.hidden,0) AS hidden,
               COUNT(DISTINCT f.file_id) AS count,
               COALESCE(
                 (SELECT file_id FROM faces WHERE id=p.cover_face_id AND COALESCE(excluded,0)=0),
                 MIN(f.file_id)
               ) AS sample_file_id
        FROM persons p
        JOIN faces f ON f.person_id=p.id AND COALESCE(f.excluded,0)=0
        WHERE 1=1 {where_hidden}
        GROUP BY p.id
        HAVING count > 0
        ORDER BY (p.name IS NULL OR TRIM(p.name)='') ASC, count DESC, p.id ASC
        """
    ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "cluster": int(r["id"]),  # alias compatibile con la prima versione
            "name": r["name"] or "",
            "count": int(r["count"]),
            "sample_file_id": int(r["sample_file_id"]) if r["sample_file_id"] is not None else None,
            "cover_face_id": int(r["cover_face_id"]) if r["cover_face_id"] is not None else None,
            "hidden": bool(r["hidden"]),
        }
        for r in rows
    ]


@router.get("/people/suggestions")
def suggest_merges(
    limit: int = Query(12, ge=1, le=50),
    min_sim: float = Query(0.38, ge=0.2, le=0.95),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    """Coppie di persone visibili con centroide simile che potrebbero essere la stessa persona."""
    if not deps.get_cfg().enable_faces:
        return []
    rows = conn.execute(
        """
        SELECT p.id, p.name, p.cover_face_id, p.centroid,
               COUNT(DISTINCT f.file_id) AS count,
               COALESCE(
                 (SELECT file_id FROM faces WHERE id=p.cover_face_id AND COALESCE(excluded,0)=0),
                 MIN(f.file_id)
               ) AS sample_file_id
        FROM persons p
        JOIN faces f ON f.person_id=p.id AND COALESCE(f.excluded,0)=0
        WHERE COALESCE(p.hidden,0)=0 AND p.centroid IS NOT NULL
        GROUP BY p.id
        HAVING count > 0
        ORDER BY p.id ASC
        """
    ).fetchall()
    if len(rows) < 2:
        return []

    items = []
    vecs = []
    for r in rows:
        v = np.asarray(dbm.from_blob(r["centroid"]), dtype=np.float32)
        n = float(np.linalg.norm(v))
        if n <= 0:
            continue
        vecs.append(v / n)
        items.append({
            "id": int(r["id"]),
            "name": r["name"] or "",
            "count": int(r["count"]),
            "sample_file_id": int(r["sample_file_id"]) if r["sample_file_id"] is not None else None,
            "cover_face_id": int(r["cover_face_id"]) if r["cover_face_id"] is not None else None,
        })
    if len(items) < 2:
        return []

    mat = np.stack(vecs)
    sims = mat @ mat.T
    i_idx, j_idx = np.where(np.triu(sims, k=1) >= float(min_sim))
    pairs = []
    for i, j in zip(i_idx.tolist(), j_idx.tolist()):
        a, b = items[i], items[j]
        # Ordina mettendo prima la persona con nome (o con piu' foto) come destinazione naturale
        a_key = (1 if a["name"] else 0, a["count"], -a["id"])
        b_key = (1 if b["name"] else 0, b["count"], -b["id"])
        if b_key > a_key:
            a, b = b, a
        pairs.append({
            "a": a,
            "b": b,
            "similarity": round(float(sims[i, j]), 4),
        })
    pairs.sort(key=lambda x: x["similarity"], reverse=True)
    return pairs[:limit]


@router.get("/people/{person_id}/files")
def person_files(
    person_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(120, ge=1, le=MAX_PAGE),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    p = _person_row(conn, person_id)
    total = int(conn.execute(
        "SELECT COUNT(DISTINCT file_id) FROM faces WHERE person_id=? AND COALESCE(excluded,0)=0",
        (person_id,),
    ).fetchone()[0])
    rows = conn.execute(
        deps.SELECT_FILE
        + """
        JOIN (
            SELECT file_id, MIN(id) AS face_id
            FROM faces WHERE person_id=? AND COALESCE(excluded,0)=0
            GROUP BY file_id
        ) fc ON fc.file_id=f.id
        ORDER BY (f.date_taken IS NULL), f.date_taken DESC, f.id DESC
        LIMIT ? OFFSET ?
        """,
        (person_id, limit, offset),
    ).fetchall()
    # Mappa file_id -> face_id per le azioni sulla singola foto ("Non e' questa persona" / "Escludi volto")
    fids = [int(r["id"]) for r in rows]
    face_by_file: dict[int, int] = {}
    if fids:
        marks = ",".join("?" * len(fids))
        for fr in conn.execute(
            f"SELECT file_id, MIN(id) AS face_id FROM faces WHERE person_id=? AND COALESCE(excluded,0)=0 AND file_id IN ({marks}) GROUP BY file_id",
            [person_id, *fids],
        ):
            face_by_file[int(fr["file_id"])] = int(fr["face_id"])
    items = [{**deps.file_to_dict(r), "face_id": face_by_file.get(int(r["id"]))} for r in rows]
    return {
        "total": total,
        "person": {
            "id": int(p["id"]),
            "name": p["name"] or "",
            "cover_face_id": int(p["cover_face_id"]) if p["cover_face_id"] is not None else None,
            "hidden": bool(p["hidden"]),
        },
        "items": items,
    }


@router.patch("/people/{person_id}")
def update_person(person_id: int, body: dict = Body(...), conn: sqlite3.Connection = Depends(deps.get_db)):
    _person_row(conn, person_id)
    sets: list[str] = []
    vals: list = []
    if "name" in body:
        raw = body["name"]
        if raw is None:
            name = None
        elif isinstance(raw, str):
            s = raw.strip()
            if len(s) > NAME_MAX:
                raise HTTPException(400, f"Il nome non puo' superare {NAME_MAX} caratteri.")
            name = s or None
        else:
            raise HTTPException(400, "Nome non valido.")
        sets.append("name=?")
        vals.append(name)
    if "hidden" in body:
        if not isinstance(body["hidden"], bool):
            raise HTTPException(400, "Valore hidden non valido.")
        sets.append("hidden=?")
        vals.append(1 if body["hidden"] else 0)
    reset_auto_cover = False
    if "cover_face_id" in body:
        raw_cover = body["cover_face_id"]
        if raw_cover is None:
            sets.append("cover_locked=?")
            vals.append(0)
            reset_auto_cover = True
        else:
            try:
                cfid = int(raw_cover)
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, "ID volto non valido.") from exc
            ok_face = conn.execute(
                "SELECT id FROM faces WHERE id=? AND person_id=? AND COALESCE(excluded,0)=0",
                (cfid, person_id),
            ).fetchone()
            if ok_face is None:
                raise HTTPException(400, "Il volto selezionato non appartiene a questa persona.")
            sets.append("cover_face_id=?")
            vals.append(cfid)
            sets.append("cover_locked=?")
            vals.append(1)
    if not sets:
        raise HTTPException(400, "Nessun campo da aggiornare.")
    with conn:
        conn.execute(f"UPDATE persons SET {', '.join(sets)} WHERE id=?", [*vals, person_id])
        if reset_auto_cover:
            faces_mod.recompute_person(conn, person_id)
    r = _person_row(conn, person_id)
    return {
        "id": int(r["id"]),
        "name": r["name"] or "",
        "hidden": bool(r["hidden"]),
        "cover_face_id": int(r["cover_face_id"]) if r["cover_face_id"] is not None else None,
    }


@router.post("/people/merge")
def merge_people(body: dict = Body(...), conn: sqlite3.Connection = Depends(deps.get_db)):
    ids = body.get("ids")
    into = body.get("into")
    if not isinstance(ids, list) or len(ids) < 2 or into is None:
        raise HTTPException(400, "Servono almeno due persone da unire e la persona di destinazione.")
    try:
        into_id = int(into)
        id_list = [int(x) for x in ids]
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "ID persona non validi.") from exc
    _person_row(conn, into_id)
    return faces_mod.merge_persons(conn, id_list, into_id)


@router.post("/faces/assign")
def assign_faces_route(body: dict = Body(...), conn: sqlite3.Connection = Depends(deps.get_db)):
    face_ids = body.get("face_ids")
    if not isinstance(face_ids, list) or not face_ids:
        raise HTTPException(400, "Seleziona almeno un volto.")
    target = body.get("person_id")
    if target is not None and target != "new":
        try:
            target = int(target)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "Destinazione persona non valida.") from exc
        _person_row(conn, target)
    try:
        fids = [int(x) for x in face_ids]
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "ID volto non validi.") from exc
    return faces_mod.assign_faces(conn, fids, target)


@router.post("/faces/{face_id}/exclude")
def exclude_face(face_id: int, conn: sqlite3.Connection = Depends(deps.get_db)):
    try:
        return faces_mod.set_face_excluded(conn, face_id, True)
    except KeyError as exc:
        raise HTTPException(404, "Volto non trovato.") from exc


@router.post("/faces/{face_id}/restore")
def restore_face(face_id: int, conn: sqlite3.Connection = Depends(deps.get_db)):
    try:
        return faces_mod.set_face_excluded(conn, face_id, False)
    except KeyError as exc:
        raise HTTPException(404, "Volto non trovato.") from exc


@router.get("/faces/{face_id}/crop")
def face_crop(
    face_id: int,
    size: int = Query(256, ge=64, le=512),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    """Ritaglio quadrato del volto con margine, salvato in cache/faces/."""
    r = conn.execute(
        "SELECT fc.bbox, f.path FROM faces fc JOIN files f ON f.id=fc.file_id WHERE fc.id=?",
        (face_id,),
    ).fetchone()
    if r is None:
        raise HTTPException(404, "Volto non trovato.")
    cfg = deps.get_cfg()
    out_dir = Path(cfg.cache_dir) / "faces"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{face_id}_{size}.jpg"
    if not out.is_file():
        src = Path(r["path"])
        if not src.is_file():
            raise HTTPException(404, "File originale non trovato.")
        try:
            bbox = json.loads(r["bbox"] or "[]")
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                w, h = im.size
                if len(bbox) == 4:
                    x1, y1, x2, y2 = [float(v) for v in bbox]
                    # Scala se il bbox e' stato salvato su immagine ridimensionata a max 1600 px
                    max_orig = max(w, h)
                    if max_orig > 1600 and max(x2, y2) <= 1605:
                        sc = max_orig / 1600.0
                        x1, y1, x2, y2 = x1 * sc, y1 * sc, x2 * sc, y2 * sc
                    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                    side = max(x2 - x1, y2 - y1) * 1.65
                    left = max(0, int(round(cx - side / 2.0)))
                    top = max(0, int(round(cy - side / 2.0)))
                    right = min(w, int(round(cx + side / 2.0)))
                    bottom = min(h, int(round(cy + side / 2.0)))
                    if right > left + 4 and bottom > top + 4:
                        im = im.crop((left, top, right, bottom))
                im = ImageOps.fit(im, (size, size), method=Image.LANCZOS)
                tmp = out.with_suffix(".jpg.tmp")
                im.save(tmp, format="JPEG", quality=88)
                tmp.replace(out)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"Impossibile creare il ritaglio del volto: {exc}") from exc
    return FileResponse(str(out), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


def _active_face_job():
    for name in ("volti_scan", "volti_cluster", "volti_modelli"):
        j = jobs.running_job(name)
        if j is not None:
            return j
    return None


@router.get("/people/job")
def active_face_job():
    """Ritorna il job volti attualmente in corso (se presente), per sincronizzare l'interfaccia cross-pagina."""
    j = _active_face_job()
    return {"job": j.public() if j is not None else None}


@router.post("/people/scan")
def scan_people(body: dict = Body(default={})):
    """Rileva i volti sulle foto non ancora analizzate e raggruppa le persone in background (riprendibile e cross-pagina)."""
    if not faces_mod.available():
        raise HTTPException(400, "Installa onnxruntime-gpu (o onnxruntime) per analizzare i volti.")
    if routes_index._current_run() is not None:
        raise HTTPException(409, "Attendi il termine dell'indicizzazione prima di analizzare i volti.")
    if _active_face_job() is not None:
        raise HTTPException(409, "Un'operazione sui volti e' gia' in corso.")
    reset_unlocked = bool(body.get("reset", False))

    def _run(job):
        cfg = deps.get_cfg()
        if not faces_mod.models_present(cfg):
            faces_mod.ensure_models(cfg, progress=lambda m: job.update(stage="volti_modelli", message=m))
            if job.cancel_requested:
                return {"faces_found": 0, "people": 0}
        conn = deps.open_db()
        try:
            job.update(stage="volti_rilevamento", done=0, total=0, message="Preparazione rilevamento volti…")

            def _prog(_st, d, t):
                msg = f"Rilevamento volti: {d} di {t} foto…" if t else "Nessuna nuova foto da analizzare per volti."
                job.update(stage="volti_rilevamento", done=d, total=t, message=msg)

            found = faces_mod.detect_faces(conn, cfg=cfg, progress=_prog, should_stop=lambda: job.cancel_requested)
            job.update(stage="volti_cluster", message="Raggruppamento delle persone…")
            n = faces_mod.cluster_faces(conn, cfg=cfg, reset_unlocked=reset_unlocked)
            job.update(
                stage="completato",
                message=f"Volti completati: {n} {'persona' if n == 1 else 'persone'} trovate.",
            )
            return {"faces_found": found, "people": n}
        finally:
            conn.close()

    job = jobs.start_job("volti_scan", _run)
    return {"job_id": job.id, "job": job.public()}


@router.post("/people/recluster")
def recluster_people(body: dict = Body(default={})):
    """Ricalcola i gruppi di persone in background (rispetta locked, excluded e nomi)."""
    if routes_index._current_run() is not None:
        raise HTTPException(409, "Attendi il termine dell'indicizzazione prima di ricalcolare le persone.")
    if _active_face_job() is not None:
        raise HTTPException(409, "Un'operazione sui volti e' gia' in corso.")
    reset_unlocked = bool(body.get("reset", False))

    def _run(job):
        conn = deps.open_db()
        try:
            job.update(stage="volti_cluster", message="Ricalcolo dei gruppi di persone…")
            n = faces_mod.cluster_faces(conn, cfg=deps.get_cfg(), reset_unlocked=reset_unlocked)
            job.update(stage="completato", message=f"Persone ricalcolate: {n}.")
            return {"people": n}
        finally:
            conn.close()

    job = jobs.start_job("volti_cluster", _run)
    return {"job_id": job.id, "job": job.public()}


@router.post("/people/download-models")
def download_face_models():
    """Scarica i modelli ONNX dei volti in background (una tantum)."""
    if not faces_mod.available():
        raise HTTPException(400, "Installa onnxruntime-gpu (o onnxruntime) prima di scaricare i modelli.")
    if _active_face_job() is not None:
        raise HTTPException(409, "Un'operazione sui volti e' gia' in corso.")

    def _run(job):
        faces_mod.ensure_models(deps.get_cfg(), progress=lambda m: job.update(stage="volti_modelli", message=m))
        job.update(stage="completato", message="Modelli volti pronti.")
        return {"ok": True}

    job = jobs.start_job("volti_modelli", _run)
    return {"job_id": job.id, "job": job.public()}


@router.post("/people/reset-scan")
def reset_face_scan(conn: sqlite3.Connection = Depends(deps.get_db)):
    """Azzera la scansione volti per rianalizzare con la nuova risoluzione (mantiene nomi, volti bloccati ed esclusi)."""
    if routes_index._current_run() is not None:
        raise HTTPException(409, "Attendi il termine dell'indicizzazione prima di azzerare i volti.")
    if _active_face_job() is not None:
        raise HTTPException(409, "Un'operazione sui volti e' gia' in corso.")
    with conn:
        conn.execute("DELETE FROM faces_done")
        cur = conn.execute("DELETE FROM faces WHERE COALESCE(locked,0)=0 AND COALESCE(excluded,0)=0")
        removed = int(cur.rowcount or 0)
        conn.execute(
            "DELETE FROM persons WHERE (name IS NULL OR TRIM(name)='') "
            "AND id NOT IN (SELECT DISTINCT person_id FROM faces WHERE person_id IS NOT NULL)"
        )
        for (pid,) in conn.execute("SELECT id FROM persons ORDER BY id").fetchall():
            faces_mod.recompute_person(conn, int(pid))
    return {"ok": True, "removed_faces": removed}
