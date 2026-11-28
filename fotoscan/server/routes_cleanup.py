"""Libera spazio: analisi dello spazio recuperabile (duplicati, video pesanti, screenshot, documenti)
e spostamento sicuro nel Cestino (reversibile, con conferma esplicita).
"""
from __future__ import annotations

import os
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from . import deps, jobs

router = APIRouter(prefix="/api", tags=["cleanup"])

LARGE_VIDEO_BYTES = 50 * 1024 * 1024   # 50 MB
LARGE_PHOTO_BYTES = 8 * 1024 * 1024    # 8 MB

# Screenshot e documenti: solo foto. plan.context e' l'etichetta assegnata dal piano (sopra soglia); i video con
# quell'etichetta sono quasi sempre filmati normali (fotogrammi scuri o con testo) e restano in "Video pesanti".
CATEGORY_SQL: dict[str, tuple[str, tuple]] = {
    "large_videos": (
        "WHERE f.kind='video' AND COALESCE(p.is_dup,0)=0 AND COALESCE(f.size,0) >= ?",
        (LARGE_VIDEO_BYTES,),
    ),
    "screenshots": (
        "WHERE f.kind='image' AND p.context='Screenshot' AND COALESCE(p.is_dup,0)=0",
        (),
    ),
    "documents": (
        "WHERE f.kind='image' AND p.context='Documenti e scontrini' AND COALESCE(p.is_dup,0)=0",
        (),
    ),
    "large_photos": (
        "WHERE f.kind='image' AND COALESCE(p.is_dup,0)=0 AND COALESCE(f.size,0) >= ?",
        (LARGE_PHOTO_BYTES,),
    ),
}


class TrashIn(BaseModel):
    file_ids: list[int] = []
    confirm: bool = False


def _send_to_recycle_bin(path: str) -> bool:
    """Sposta un file nel Cestino (Windows: Cestino; Linux: cestino freedesktop). Mai cancellazione permanente.
    False se il file non esiste piu' (nulla da spostare); OSError se lo spostamento fallisce."""
    if not os.path.exists(path):
        return False
    from ..trash import send_to_trash  # import locale: il modulo e' condiviso con la pulizia del NAS

    send_to_trash(path)
    return True


def _cat_stats(conn: sqlite3.Connection, where: str, params: tuple) -> dict[str, int]:
    row = conn.execute(
        f"SELECT COUNT(*) AS c, COALESCE(SUM(f.size),0) AS b FROM files f LEFT JOIN plan p ON p.file_id=f.id {where}",
        params,
    ).fetchone()
    return {"count": int(row["c"]), "bytes": int(row["b"])}


@router.get("/cleanup/summary")
def cleanup_summary(conn: sqlite3.Connection = Depends(deps.get_db)):
    dup_row = conn.execute(
        "SELECT COUNT(*) AS c, COALESCE(SUM(f.size),0) AS b, COUNT(DISTINCT p.dup_of) AS g, "
        "SUM(CASE WHEN p.is_dup=1 THEN 1 ELSE 0 END) AS ec, "
        "COALESCE(SUM(CASE WHEN p.is_dup=1 THEN f.size ELSE 0 END),0) AS eb, "
        "COUNT(DISTINCT CASE WHEN p.is_dup=1 THEN p.dup_of END) AS eg, "
        "SUM(CASE WHEN p.is_dup=2 THEN 1 ELSE 0 END) AS sc, "
        "COALESCE(SUM(CASE WHEN p.is_dup=2 THEN f.size ELSE 0 END),0) AS sb, "
        "COUNT(DISTINCT CASE WHEN p.is_dup=2 THEN p.dup_of END) AS sg "
        "FROM plan p JOIN files f ON f.id=p.file_id WHERE p.is_dup > 0"
    ).fetchone()
    dups = {
        "count": int(dup_row["c"]),
        "bytes": int(dup_row["b"]),
        "groups": int(dup_row["g"]),
        "exact_count": int(dup_row["ec"] or 0),
        "exact_bytes": int(dup_row["eb"] or 0),
        "exact_groups": int(dup_row["eg"] or 0),
        "similar_count": int(dup_row["sc"] or 0),
        "similar_bytes": int(dup_row["sb"] or 0),
        "similar_groups": int(dup_row["sg"] or 0),
    }

    cats = {name: _cat_stats(conn, where, params) for name, (where, params) in CATEGORY_SQL.items()}

    # Totale unico senza doppi conteggi (un file può essere es. sia video pesante che duplicato)
    tot_row = conn.execute(
        """
        SELECT COUNT(*) AS c, COALESCE(SUM(f.size),0) AS b
        FROM files f LEFT JOIN plan p ON p.file_id=f.id
        WHERE COALESCE(p.is_dup,0) > 0
           OR (f.kind='video' AND COALESCE(p.is_dup,0)=0 AND COALESCE(f.size,0) >= ?)
           OR (f.kind='image' AND p.context IN ('Screenshot', 'Documenti e scontrini') AND COALESCE(p.is_dup,0)=0)
           OR (f.kind='image' AND COALESCE(p.is_dup,0)=0 AND COALESCE(f.size,0) >= ?)
        """,
        (LARGE_VIDEO_BYTES, LARGE_PHOTO_BYTES),
    ).fetchone()

    return {
        "total_files": int(tot_row["c"]),
        "total_bytes": int(tot_row["b"]),
        "duplicates": dups,
        **cats,
    }


_CLEANUP_ORDER: dict[str, str] = {
    "size": "COALESCE(f.size,0) DESC, f.id DESC",
    "date_desc": "(f.date_taken IS NULL), f.date_taken DESC, f.id DESC",
    "date_asc": "(f.date_taken IS NULL), f.date_taken ASC, f.id ASC",
    "context": "(p.context IS NULL OR TRIM(p.context)=''), p.context COLLATE NOCASE, COALESCE(f.size,0) DESC, f.id DESC",
}


@router.get("/cleanup/items")
def cleanup_items(
    category: str = Query(...),
    sort: str = Query("size"),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    if category not in CATEGORY_SQL:
        raise HTTPException(400, f"Categoria non valida: {category}")
    order = _CLEANUP_ORDER.get(sort)
    if order is None:
        raise HTTPException(400, f"Ordinamento non valido: {sort}")
    where, params = CATEGORY_SQL[category]
    st = _cat_stats(conn, where, params)
    rows = conn.execute(
        deps.SELECT_FILE + f"{where} ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return {
        "category": category,
        "sort": sort,
        "total": st["count"],
        "total_bytes": st["bytes"],
        "items": [deps.file_to_dict(r) for r in rows],
    }


TRASH_CHUNK = 400  # file per blocco: ogni blocco aggiorna e conferma il catalogo (limite variabili SQLite, interruzioni)


def _forget_files(conn: sqlite3.Connection, gone: list[int]) -> None:
    """Toglie dal catalogo file non piu' sul disco (cestinati o spariti) e ripara i gruppi di duplicati."""
    tmarks = ",".join("?" * len(gone))
    # Se tra i file tolti c'era il master di un gruppo, uno dei superstiti diventa il nuovo master.
    orphans = conn.execute(
        f"SELECT file_id, dup_of, COALESCE(is_dup,0) AS is_dup FROM plan "
        f"WHERE dup_of IN ({tmarks}) AND file_id NOT IN ({tmarks})",
        (*gone, *gone),
    ).fetchall()
    by_old_root: dict[int, list[tuple[int, int]]] = {}
    for o in orphans:
        by_old_root.setdefault(int(o["dup_of"]), []).append((int(o["file_id"]), int(o["is_dup"])))
    for old_root, survivors in by_old_root.items():
        survivors.sort()
        # Il nuovo master e' una copia ESATTA del vecchio (stesso contenuto) se ne resta una: cosi' le altre copie
        # esatte restano copie del suo stesso contenuto. Solo senza copie esatte tocca a un quasi-duplicato.
        # (Un quasi-duplicato promosso con copie esatte rimaste farebbe cestinare, con "copie esatte", l'unico
        # esemplare del contenuto originale.)
        exact = [fid for fid, d in survivors if d == 1]
        new_master = exact[0] if exact else survivors[0][0]
        old_row = conn.execute("SELECT folder FROM plan WHERE file_id=?", (old_root,)).fetchone()
        old_folder = old_row["folder"] if old_row else None
        if old_folder:
            conn.execute("UPDATE plan SET is_dup=0, dup_of=NULL, folder=? WHERE file_id=?", (old_folder, new_master))
        else:
            conn.execute("UPDATE plan SET is_dup=0, dup_of=NULL WHERE file_id=?", (new_master,))
        for fid, _d in survivors:
            if fid != new_master:
                conn.execute("UPDATE plan SET dup_of=? WHERE file_id=?", (new_master, fid))
    has_dk = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dup_keep'").fetchone()
    if has_dk:
        _fix_dup_keep(conn, set(gone))
        conn.execute(f"DELETE FROM dup_keep WHERE file_id IN ({tmarks})", gone)
    from .. import faces  # import locale: modulo pesante, serve solo qui

    faces.forget_files(conn, gone)  # volti, persone, copertine e ritagli dei file: stessa transazione, senza commit
    conn.execute(f"DELETE FROM files WHERE id IN ({tmarks})", gone)
    # tabelle senza chiave esterna verso files
    conn.execute(f"DELETE FROM file_override WHERE file_id IN ({tmarks})", gone)
    conn.execute(f"DELETE FROM faces_done WHERE file_id IN ({tmarks})", gone)
    conn.commit()


def _fix_dup_keep(conn: sqlite3.Connection, gone: set[int]) -> None:
    """Scambi master scelti dall'utente: se spariscono solo copie (non un master), la chiave del gruppo si aggiorna e
    la scelta resta valida al ricalcolo; se sparisce un master la scelta decade (ci pensa release_stale)."""
    rows = conn.execute("SELECT file_id, cluster, dup_of FROM dup_keep").fetchall()
    by_cluster: dict[str, list] = {}
    for r in rows:
        by_cluster.setdefault(r["cluster"], []).append(r)
    for cluster, members in by_cluster.items():
        ids = {int(x) for x in cluster.split(",") if x}
        if not (ids & gone):
            continue
        if any(m["dup_of"] in gone for m in members):
            continue
        new = ",".join(str(i) for i in sorted(ids - gone))
        conn.execute("UPDATE dup_keep SET cluster=? WHERE cluster=?", (new, cluster))


def trash_file_ids(conn: sqlite3.Connection, raw_ids: list[int]) -> dict:
    """Sposta i file indicati nel Cestino, li rimuove dal catalogo e ripara eventuali gruppi duplicati.
    Lavora a blocchi, con un commit per blocco. Un file gia' sparito dal disco esce dal catalogo ma non viene
    contato come spostato nel Cestino (campo `missing`)."""
    ids = list(dict.fromkeys(int(x) for x in raw_ids))
    if not ids:
        raise HTTPException(400, "Nessun file selezionato.")
    if jobs.running_job("piano") is not None or jobs.running_job("apply") is not None:
        raise HTTPException(409, "Il piano e' in ricalcolo o in applicazione: riprova tra un momento.")
    trashed = missing = freed = 0
    errors: list[dict] = []
    for i in range(0, len(ids), TRASH_CHUNK):
        chunk = ids[i:i + TRASH_CHUNK]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(f"SELECT id, path, size FROM files WHERE id IN ({marks})", chunk).fetchall()
        gone: list[int] = []
        for r in rows:
            try:
                moved = _send_to_recycle_bin(r["path"])
            except Exception as exc:  # noqa: BLE001
                errors.append({"id": int(r["id"]), "path": r["path"], "error": str(exc)})
                continue
            gone.append(int(r["id"]))
            if moved:
                trashed += 1
                freed += int(r["size"] or 0)
            else:
                missing += 1
        if gone:
            _forget_files(conn, gone)
    return {"trashed": trashed, "freed_bytes": freed, "errors": errors, "missing": missing}


@router.post("/cleanup/trash")
def cleanup_trash(body: TrashIn, conn: sqlite3.Connection = Depends(deps.get_db)):
    """Sposta i file scelti nel Cestino di Windows e li rimuove dal catalogo."""
    if not body.confirm:
        raise HTTPException(400, "Serve la conferma esplicita per spostare i file nel Cestino.")
    return trash_file_ids(conn, body.file_ids)
