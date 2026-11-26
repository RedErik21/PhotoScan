"""Piano di organizzazione: riepilogo gruppi, ricalcolo, rinomina, spostamenti (docs/CONTRACT.md §2)."""
from __future__ import annotations

import re
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db as dbm
from .. import organizer
from . import deps, jobs, routes_duplicates

router = APIRouter(prefix="/api", tags=["piano"])

JOB = "piano"
KINDS = ("context", "event", "varie", "incerta", "dup", "similar")
UNDATED_PREFIX = organizer.UNDATED + "/"
MAX_FOLDER_LEN = 180
MAX_MOVE_IDS = 20000
_FORBIDDEN = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$", re.IGNORECASE)

# Aggregazione per gruppo: una sola query GROUP BY (niente N+1). cover_id = primo file immagine per id.
_AGG = (
    "SELECT p.folder AS folder, COUNT(*) AS count, COALESCE(SUM(f.kind='video'),0) AS videos, "
    "COALESCE(SUM(f.size),0) AS bytes, MIN(f.date_taken) AS first, MAX(f.date_taken) AS last, "
    "MIN(CASE WHEN f.kind='image' THEN f.id END) AS cover_id, MAX(p.gkind) AS gkind "
    "FROM plan p JOIN files f ON f.id=p.file_id WHERE p.folder IS NOT NULL "
)


class RenameBody(BaseModel):
    old: str
    new: str


class MoveBody(BaseModel):
    file_ids: list[int] = Field(default_factory=list)
    folder: str


# ------------------------------------------------------------ helper
def classify(folder: str, gkind: str | None = None) -> str:
    """Tipo di gruppo: quello salvato dal piano (gkind) se c'e', altrimenti ricavato dal nome della cartella."""
    if folder == organizer.SIMILI_FOLDER:
        return "similar"
    if folder == organizer.DUP_FOLDER:
        return "dup"
    if folder.startswith(UNDATED_PREFIX):
        return "incerta"
    if gkind in ("event", "varie", "context"):
        return gkind
    if folder.endswith("Varie") or folder.rsplit(" - ", 1)[-1] in organizer.MESI:
        return "varie"
    if "Evento" in folder:
        return "event"
    return "context"


def _query_groups(conn: sqlite3.Connection, folder: str | None = None) -> list[sqlite3.Row]:
    if folder is None:
        return conn.execute(_AGG + "GROUP BY p.folder ORDER BY p.folder").fetchall()
    return conn.execute(_AGG + "AND p.folder=? GROUP BY p.folder", (folder,)).fetchall()


def _group_dict(r: sqlite3.Row) -> dict:
    return {
        "folder": r["folder"],
        "kind": classify(r["folder"], r["gkind"]),
        "count": r["count"],
        "videos": r["videos"],
        "bytes": r["bytes"],
        "first": r["first"],
        "last": r["last"],
        "cover_id": r["cover_id"],
    }


def _one_group(conn: sqlite3.Connection, folder: str) -> dict:
    rows = _query_groups(conn, folder)
    if rows:
        return _group_dict(rows[0])
    return {"folder": folder, "kind": classify(folder), "count": 0, "videos": 0, "bytes": 0,
            "first": None, "last": None, "cover_id": None}


def _is_protected(folder: str) -> bool:
    """Gruppi gestiti dall'algoritmo dei duplicati: non rinominabili, non bersaglio di spostamenti."""
    return folder == organizer.DUP_FOLDER or folder.startswith(organizer.DUP_FOLDER + "/")


def _clean_folder(raw: str) -> str:
    """Valida un nome di cartella del piano ('/' separa i livelli). Errori in italiano."""
    parts = [p.strip() for p in (raw or "").replace("\\", "/").split("/")]
    parts = [p for p in parts if p]
    if not parts:
        raise HTTPException(400, "Il nome della cartella non puo' essere vuoto.")
    for p in parts:
        if p in (".", ".."):
            raise HTTPException(400, "Il nome non puo' contenere '..'.")
        if _FORBIDDEN.search(p):
            raise HTTPException(400, 'Il nome contiene caratteri non ammessi: < > : " | ? *')
        if p.endswith("."):
            raise HTTPException(400, "Il nome non puo' finire con un punto.")
        if _RESERVED.match(p.split(".")[0]):
            raise HTTPException(400, f"\"{p}\" e' un nome riservato di Windows.")
    out = "/".join(parts)
    if len(out) > MAX_FOLDER_LEN:
        raise HTTPException(400, f"Il nome e' troppo lungo (massimo {MAX_FOLDER_LEN} caratteri).")
    if _is_protected(out):
        raise HTTPException(400, "La cartella dei duplicati e' gestita automaticamente.")
    return out


def _ensure_idle() -> None:
    if jobs.running_job(JOB) is not None:
        raise HTTPException(409, "Il piano e' in ricalcolo: riprova tra un momento.")


def _needs_text_model(conn: sqlite3.Connection, cfg) -> bool:
    """Il modello di testo serve solo se ci sono embedding e mancano vettori delle etichette."""
    if not cfg.labels:
        return False
    if conn.execute("SELECT 1 FROM embeddings LIMIT 1").fetchone() is None:
        return False
    if dbm.get_meta(conn, "label_model") != cfg.model_name:
        return True
    prompts = {l["prompt"] for l in cfg.labels} | set(cfg.null_prompts)
    cached = {r[0] for r in conn.execute("SELECT prompt FROM label_cache")}
    return not prompts <= cached


def _run_rebuild(job):
    """Thread del job: ricalcola il piano; carica il modello (CPU) solo se mancano i vettori etichetta."""
    from ..embedder import Embedder

    conn = deps.open_db()
    try:
        cfg = deps.get_cfg()
        embedder = None
        if _needs_text_model(conn, cfg):
            job.update(stage="modello", message="Carico il modello di testo (CPU)")
            embedder = Embedder(cfg, device="cpu")
        job.update(stage="piano", message="Calcolo dei gruppi")
        try:
            return organizer.build_plan(conn, cfg, embedder)
        finally:
            if embedder is not None:  # il server non tiene il modello di testo dopo il piano
                from ..embedder import release
                release(embedder)
    finally:
        conn.close()


def _launch_rebuild() -> dict:
    _ensure_idle()
    job = jobs.start_job(JOB, _run_rebuild)
    return {"job_id": job.id}


# ------------------------------------------------------------ route
@router.get("/plan/summary")
def plan_summary(conn: sqlite3.Connection = Depends(deps.get_db)):
    built = conn.execute("SELECT EXISTS(SELECT 1 FROM plan)").fetchone()[0] == 1
    groups = [_group_dict(r) for r in _query_groups(conn)]
    counts = {k: 0 for k in KINDS}
    for g in groups:
        counts[g["kind"]] += 1
    # Foto analizzate dopo l'ultimo calcolo: il piano non le include ancora (piano da aggiornare).
    missing = 0
    if built:
        missing = conn.execute(
            "SELECT COUNT(*) FROM files f WHERE f.status='embedded' "
            "AND NOT EXISTS (SELECT 1 FROM plan p WHERE p.file_id=f.id)").fetchone()[0]
    return {"built": built, "total": sum(g["count"] for g in groups), "groups": groups, "counts": counts,
            "missing": missing}


@router.post("/plan/rebuild")
def plan_rebuild():
    return _launch_rebuild()


@router.get("/plan/folder")
def plan_folder(
    name: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(120, ge=1, le=500),
    sort: Literal["date", "name"] = "date",
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    order = "f.date_taken IS NULL, f.date_taken, f.id" if sort == "date" else "f.name COLLATE NOCASE, f.id"
    total = conn.execute("SELECT COUNT(*) FROM plan p WHERE p.folder=?", (name,)).fetchone()[0]
    rows = conn.execute(
        deps.SELECT_FILE + f"WHERE p.folder=? ORDER BY {order} LIMIT ? OFFSET ?", (name, limit, offset)
    ).fetchall()
    return {"folder": name, "count": total, "total": total, "items": [deps.file_to_dict(r) for r in rows]}


@router.post("/plan/rename")
def plan_rename(body: RenameBody, conn: sqlite3.Connection = Depends(deps.get_db)):
    _ensure_idle()
    old = body.old
    if not old:
        raise HTTPException(400, "Gruppo da rinominare non indicato.")
    if _is_protected(old):
        raise HTTPException(400, "I gruppi dei duplicati non si rinominano.")
    new = _clean_folder(body.new)
    if not _query_groups(conn, old):
        raise HTTPException(404, "Gruppo non trovato: ricalcola il piano e riprova.")
    if new != old:
        organizer.rename_group(conn, old, new)
    return _one_group(conn, new)


@router.post("/plan/move")
def plan_move(body: MoveBody, conn: sqlite3.Connection = Depends(deps.get_db)):
    _ensure_idle()
    ids = list(dict.fromkeys(body.file_ids))
    if not ids:
        raise HTTPException(400, "Nessun file selezionato.")
    if len(ids) > MAX_MOVE_IDS:
        raise HTTPException(400, f"Troppi file in una sola volta (massimo {MAX_MOVE_IDS}).")
    folder = _clean_folder(body.folder)
    found = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        found += len(conn.execute(
            f"SELECT id FROM files WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall())
    if found != len(ids):
        raise HTTPException(400, "Alcuni file selezionati non esistono nel catalogo.")
    organizer.move_files(conn, ids, folder)
    return {"moved": len(ids), "folder": folder, "group": _one_group(conn, folder)}


@router.post("/plan/reset-overrides")
def plan_reset_overrides(conn: sqlite3.Connection = Depends(deps.get_db)):
    _ensure_idle()
    conn.execute("DELETE FROM rename_map")
    conn.execute("DELETE FROM file_override")
    routes_duplicates._ensure(conn)
    conn.execute("DELETE FROM dup_keep")  # anche gli scambi master/duplicato dell'utente
    conn.commit()
    return _launch_rebuild()


@router.get("/plan/folders/tree")
def plan_tree(conn: sqlite3.Connection = Depends(deps.get_db)):
    root: dict = {"children": {}}
    for r in _query_groups(conn):
        node = root
        for part in r["folder"].split("/"):
            node = node["children"].setdefault(part, {"name": part, "folder": None, "count": 0, "children": {}})
            node["count"] += r["count"]
        node["folder"] = r["folder"]

    def out(node: dict) -> list[dict]:
        return [
            {"name": n["name"], "folder": n["folder"], "count": n["count"], "children": out(n)}
            for n in sorted(node["children"].values(), key=lambda n: n["name"].casefold())
        ]

    return out(root)
