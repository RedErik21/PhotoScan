"""Persone e volti: elenco persone, foto per persona, ritaglio volto, rinomina, unione, assegnazione, esclusione, recluster."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
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


def _not_same_pairs(conn: sqlite3.Connection) -> set[tuple[int, int]]:
    return {(int(r[0]), int(r[1])) for r in conn.execute("SELECT a, b FROM persons_not_same")}


def _pair_key(x: int, y: int) -> tuple[int, int]:
    return (x, y) if x < y else (y, x)


def _visible_person_vectors(conn: sqlite3.Connection):
    """Persone visibili con centroide: (items, matrice di vettori normalizzati, righe 1:1 con items)."""
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
    mat = np.stack(vecs) if vecs else np.zeros((0, 0), dtype=np.float32)
    return items, mat


MAX_SUGGEST_PAIRS = 200_000  # tetto agli archi tenuti in memoria (i piu' simili) con moltissime persone


def _similar_pairs(mat: np.ndarray, min_sim: float, block: int = 1024) -> list[tuple[int, int, float]]:
    """Coppie (i<j, similarita') con sim >= min_sim, calcolate a blocchi (mai la matrice P x P intera)."""
    n = len(mat)
    out: list[tuple[int, int, float]] = []
    for i in range(0, n, block):
        sims = mat[i:i + block] @ mat[i:].T          # righe i..i+block contro colonne i..fine
        rows, cols = np.nonzero(sims >= min_sim)
        for r, c in zip(rows.tolist(), cols.tolist()):
            gi, gj = i + r, i + c
            if gj > gi:
                out.append((gi, gj, float(sims[r, c])))
        if len(out) > MAX_SUGGEST_PAIRS * 2:
            out.sort(key=lambda e: e[2], reverse=True)
            del out[MAX_SUGGEST_PAIRS:]
    out.sort(key=lambda e: e[2], reverse=True)
    return out[:MAX_SUGGEST_PAIRS]


@router.get("/people/suggestions")
def suggest_merges(
    limit: int = Query(12, ge=1, le=50),
    min_sim: float = Query(0.38, ge=0.2, le=0.95),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    """Coppie di persone visibili con centroide simile che potrebbero essere la stessa persona (esclude le coppie «Diverse»)."""
    if not deps.get_cfg().enable_faces:
        return []
    items, mat = _visible_person_vectors(conn)
    if len(items) < 2:
        return []
    not_same = _not_same_pairs(conn)
    pairs = []
    for i, j, sim_ij in _similar_pairs(mat, float(min_sim)):
        a, b = items[i], items[j]
        if _pair_key(a["id"], b["id"]) in not_same:
            continue
        # Ordina mettendo prima la persona con nome (o con piu' foto) come destinazione naturale
        a_key = (1 if a["name"] else 0, a["count"], -a["id"])
        b_key = (1 if b["name"] else 0, b["count"], -b["id"])
        if b_key > a_key:
            a, b = b, a
        pairs.append({
            "a": a,
            "b": b,
            "similarity": round(sim_ij, 4),
        })
    pairs.sort(key=lambda x: x["similarity"], reverse=True)
    return pairs[:limit]


@router.get("/people/suggestion-groups")
def suggestion_groups(
    limit: int = Query(12, ge=1, le=50),
    min_sim: float = Query(0.38, ge=0.2, le=0.95),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    """Gruppi di persone visibili probabilmente uguali: componenti connesse delle coppie sopra `min_sim`.

    Ogni gruppo: `{members: [persona...], similarity}` con `similarity` = somiglianza massima interna.
    Le coppie marcate «Diverse» non creano collegamenti. Gruppi da 2+ persone, ordinati per dimensione e somiglianza.
    """
    if not deps.get_cfg().enable_faces:
        return []
    items, mat = _visible_person_vectors(conn)
    if len(items) < 2:
        return []
    not_same = _not_same_pairs(conn)

    # Unione greedy dalle coppie piu' simili; due gruppi non si fondono se contengono una coppia «Diverse»
    edges: list[tuple[int, int, float]] = []
    for i, j, s_ij in _similar_pairs(mat, float(min_sim)):
        if _pair_key(items[i]["id"], items[j]["id"]) in not_same:
            continue
        edges.append((i, j, s_ij))
    edges.sort(key=lambda e: e[2], reverse=True)
    # indice persona -> indici delle persone marcate «Diverse» (controllo O(grado) invece di O(|A|*|B|))
    idx_of = {it["id"]: k for k, it in enumerate(items)}
    ns_adj: dict[int, set[int]] = {}
    for a, b in not_same:
        if a in idx_of and b in idx_of:
            ns_adj.setdefault(idx_of[a], set()).add(idx_of[b])
            ns_adj.setdefault(idx_of[b], set()).add(idx_of[a])

    group_of = list(range(len(items)))          # indice persona -> id gruppo
    members: dict[int, set[int]] = {k: {k} for k in range(len(items))}
    for i, j, _s in edges:
        gi, gj = group_of[i], group_of[j]
        if gi == gj:
            continue
        if any(ns_adj.get(x, set()) & members[gj] for x in members[gi]):
            continue
        if len(members[gi]) < len(members[gj]):
            gi, gj = gj, gi
        for x in members[gj]:
            group_of[x] = gi
        members[gi] |= members.pop(gj)

    group_sim: dict[int, float] = {}
    for i, j, s_ij in edges:
        if group_of[i] == group_of[j]:
            g = group_of[i]
            group_sim[g] = max(group_sim.get(g, 0.0), s_ij)

    groups = []
    for g, idxs in members.items():
        if len(idxs) < 2 or g not in group_sim:
            continue
        ordered = sorted(
            idxs,
            key=lambda k: ((1 if items[k]["name"] else 0), items[k]["count"], -items[k]["id"]),
            reverse=True,
        )
        groups.append({
            "members": [items[k] for k in ordered],
            "similarity": round(group_sim[g], 4),
        })
    groups.sort(key=lambda g: (len(g["members"]), g["similarity"]), reverse=True)
    return groups[:limit]


@router.get("/people/not-same")
def list_not_same(conn: sqlite3.Connection = Depends(deps.get_db)):
    """Coppie di persone segnalate come «Diverse»."""
    return [
        {"a": int(r["a"]), "b": int(r["b"])}
        for r in conn.execute("SELECT a, b FROM persons_not_same ORDER BY a, b")
    ]


@router.post("/people/not-same")
def add_not_same(body: dict = Body(...), conn: sqlite3.Connection = Depends(deps.get_db)):
    """Segna due persone come «Diverse»: non verranno piu' proposte come suggerimento."""
    try:
        a, b = int(body.get("a")), int(body.get("b"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "ID persona non validi.") from exc
    if a == b:
        raise HTTPException(400, "Servono due persone diverse.")
    _person_row(conn, a)
    _person_row(conn, b)
    x, y = _pair_key(a, b)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO persons_not_same(a, b, created) VALUES(?, ?, datetime('now'))",
            (x, y),
        )
    return {"a": x, "b": y}


@router.delete("/people/not-same/{a}/{b}")
def remove_not_same(a: int, b: int, conn: sqlite3.Connection = Depends(deps.get_db)):
    """Annulla la segnalazione «Diverse» per la coppia."""
    x, y = _pair_key(int(a), int(b))
    with conn:
        conn.execute("DELETE FROM persons_not_same WHERE a=? AND b=?", (x, y))
    return {"a": x, "b": y}


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
    unique = list(dict.fromkeys(id_list))
    if len([x for x in unique if x != into_id]) < 1:
        raise HTTPException(400, "Servono almeno due persone distinte da unire.")
    _person_row(conn, into_id)
    for pid in unique:
        _person_row(conn, pid)
    try:
        return faces_mod.merge_persons(conn, unique, into_id)
    except KeyError as exc:
        raise HTTPException(404, "Persona non trovata.") from exc


@router.post("/people/merge/{log_id}/undo")
def undo_merge_route(log_id: int, conn: sqlite3.Connection = Depends(deps.get_db)):
    """Annulla l'ultima unione di persone (solo la piu' recente non ancora annullata)."""
    try:
        return faces_mod.undo_merge(conn, log_id)
    except KeyError as exc:
        raise HTTPException(409, "Solo l'ultima unione puo' essere annullata.") from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/people/split")
def split_people(body: dict = Body(...), conn: sqlite3.Connection = Depends(deps.get_db)):
    """Sposta i volti selezionati da una persona in una nuova persona."""
    try:
        person_id = int(body.get("person_id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "ID persona non valido.") from exc
    face_ids = body.get("face_ids")
    if not isinstance(face_ids, list) or not face_ids:
        raise HTTPException(400, "Seleziona almeno un volto.")
    try:
        fids = [int(x) for x in face_ids]
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "ID volto non validi.") from exc
    _person_row(conn, person_id)
    try:
        return faces_mod.split_person(conn, person_id, fids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "Persona non trovata.") from exc


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
        "SELECT fc.bbox, fc.file_id, f.path, f.size, f.mtime FROM faces fc JOIN files f ON f.id=fc.file_id WHERE fc.id=?",
        (face_id,),
    ).fetchone()
    if r is None:
        raise HTTPException(404, "Volto non trovato.")
    cfg = deps.get_cfg()
    out_dir = Path(cfg.cache_dir) / "faces"
    out_dir.mkdir(parents=True, exist_ok=True)
    # La chiave include file, riquadro, size e mtime: gli id dei volti possono essere riusati dopo un reset o una
    # cancellazione, e un ritaglio vecchio non deve mai essere servito per un volto diverso.
    ver = hashlib.sha1(
        f"{r['file_id']}|{r['bbox']}|{r['size']}|{r['mtime']}".encode("utf-8", "replace")
    ).hexdigest()[:10]
    out = out_dir / f"{face_id}_{size}_{ver}.jpg"
    if not out.is_file():
        src_path = str(r["path"])
        if not os.path.isfile(faces_mod._fs_path(src_path)):
            raise HTTPException(404, "File originale non trovato.")
        try:
            bbox = json.loads(r["bbox"] or "[]")
            # Stesso caricamento del rilevamento (EXIF applicato, lato max 1600): le coordinate del riquadro coincidono
            # senza indovinare la scala, e non si decodifica l'originale a piena risoluzione.
            im = faces_mod._load_image_rgb(src_path, max_side=1600)
            w, h = im.size
            if len(bbox) == 4:
                x1, y1, x2, y2 = [float(v) for v in bbox]
                cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                side = max(x2 - x1, y2 - y1) * 1.65
                left = max(0, int(round(cx - side / 2.0)))
                top = max(0, int(round(cy - side / 2.0)))
                right = min(w, int(round(cx + side / 2.0)))
                bottom = min(h, int(round(cy + side / 2.0)))
                if right > left + 4 and bottom > top + 4:
                    im = im.crop((left, top, right, bottom))
            im = ImageOps.fit(im, (size, size), method=Image.LANCZOS)
            # file temporaneo univoco: due richieste simultanee dello stesso volto non si pestano i piedi
            tmp = out.with_name(f"{out.stem}.{uuid.uuid4().hex[:8]}.jpg.tmp")
            try:
                im.save(tmp, format="JPEG", quality=88)
                try:
                    tmp.replace(out)
                except OSError:
                    if not out.is_file():  # su Windows il replace puo' fallire se l'altra richiesta ha gia' vinto
                        raise
            finally:
                tmp.unlink(missing_ok=True)
            # elimina i ritagli obsoleti dello stesso volto/dimensione (versioni precedenti, formato vecchio senza hash)
            for old in list(out_dir.glob(f"{face_id}_{size}_*.jpg")) + [out_dir / f"{face_id}_{size}.jpg"]:
                if old != out:
                    try:
                        old.unlink(missing_ok=True)
                    except OSError:
                        pass
        except HTTPException:
            raise
        except faces_mod.PermanentImageError as exc:
            raise HTTPException(404, f"Immagine non leggibile: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"Impossibile creare il ritaglio del volto: {exc}") from exc
    # no-cache + ETag (FileResponse): il browser rivalida a ogni uso, cosi' un reset non mostra mai ritagli vecchi
    return FileResponse(str(out), media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


_FACE_JOBS = ("volti_scan", "volti_cluster", "volti_modelli")  # nomi letti anche dall'indicizzazione: non rinominare


def _active_face_job():
    for name in _FACE_JOBS:
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
            if job.cancel_requested:  # fermato dall'utente: il lavoro svolto e' salvato, niente raggruppamento lungo
                return {"faces_found": found, "people": None}
            job.update(stage="volti_cluster", message="Raggruppamento delle persone…")
            n = faces_mod.cluster_faces(conn, cfg=cfg, reset_unlocked=reset_unlocked)
            job.update(
                stage="completato",
                message=f"Volti completati: {n} {'persona' if n == 1 else 'persone'} trovate.",
            )
            return {"faces_found": found, "people": n}
        finally:
            conn.close()

    job, created = jobs.start_job_unique("volti_scan", _run, conflicts=_FACE_JOBS)
    if not created:
        raise HTTPException(409, "Un'operazione sui volti e' gia' in corso.")
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

    job, created = jobs.start_job_unique("volti_cluster", _run, conflicts=_FACE_JOBS)
    if not created:
        raise HTTPException(409, "Un'operazione sui volti e' gia' in corso.")
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

    job, created = jobs.start_job_unique("volti_modelli", _run, conflicts=_FACE_JOBS)
    if not created:
        raise HTTPException(409, "Un'operazione sui volti e' gia' in corso.")
    return {"job_id": job.id, "job": job.public()}


@router.post("/people/reset-scan")
def reset_face_scan(conn: sqlite3.Connection = Depends(deps.get_db)):
    """Azzera la scansione volti per rianalizzare con la nuova risoluzione (mantiene nomi, volti bloccati ed esclusi)."""
    if routes_index._current_run() is not None:
        raise HTTPException(409, "Attendi il termine dell'indicizzazione prima di azzerare i volti.")
    if _active_face_job() is not None:
        raise HTTPException(409, "Un'operazione sui volti e' gia' in corso.")
    with conn:
        # I volti delle persone con nome diventano «bloccati»: restano assegnati (con il nome) e il rilevamento non li
        # duplica. Altrimenti la persona con nome resterebbe senza volti e senza centroide, e il nome andrebbe perso.
        conn.execute(
            "UPDATE faces SET locked=1 WHERE COALESCE(locked,0)=0 AND person_id IN "
            "(SELECT id FROM persons WHERE name IS NOT NULL AND TRIM(name)<>'')"
        )
        conn.execute("DELETE FROM faces_done")
        cur = conn.execute("DELETE FROM faces WHERE COALESCE(locked,0)=0 AND COALESCE(excluded,0)=0")
        removed = int(cur.rowcount or 0)
        conn.execute(
            "DELETE FROM persons WHERE (name IS NULL OR TRIM(name)='') "
            "AND id NOT IN (SELECT DISTINCT person_id FROM faces WHERE person_id IS NOT NULL)"
        )
        for (pid,) in conn.execute("SELECT id FROM persons ORDER BY id").fetchall():
            faces_mod.recompute_person(conn, int(pid))
    # gli id dei volti verranno riusati: i ritagli in cache non sono piu' validi
    faces_mod.clear_crop_cache(deps.get_cfg())
    return {"ok": True, "removed_faces": removed}
