"""Libera spazio: analisi dello spazio recuperabile (duplicati, video pesanti, screenshot, documenti)
e spostamento sicuro nel Cestino di Windows (reversibile, con conferma esplicita).
"""
from __future__ import annotations

import ctypes
import os
import sqlite3
import sys
from ctypes import wintypes

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from . import deps

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


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def _send_to_recycle_bin(path: str) -> None:
    """Sposta un file nel Cestino di Windows (FO_DELETE + FOF_ALLOWUNDO). Mai cancellazione permanente."""
    if not os.path.exists(path):
        return
    if sys.platform != "win32":
        raise RuntimeError("Spostamento nel Cestino supportato solo su Windows.")
    op = _SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = os.path.abspath(path) + "\0\0"
    op.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400  # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if res != 0 or op.fAnyOperationsAborted:
        raise OSError(f"Impossibile spostare nel Cestino (codice {res}).")


def _cat_stats(conn: sqlite3.Connection, where: str, params: tuple) -> dict[str, int]:
    row = conn.execute(
        f"SELECT COUNT(*) AS c, COALESCE(SUM(f.size),0) AS b FROM files f LEFT JOIN plan p ON p.file_id=f.id {where}",
        params,
    ).fetchone()
    return {"count": int(row["c"]), "bytes": int(row["b"])}


@router.get("/cleanup/summary")
def cleanup_summary(conn: sqlite3.Connection = Depends(deps.get_db)):
    dup_row = conn.execute(
        "SELECT COUNT(*) AS c, COALESCE(SUM(f.size),0) AS b, COUNT(DISTINCT p.dup_of) AS g "
        "FROM plan p JOIN files f ON f.id=p.file_id WHERE p.is_dup > 0"
    ).fetchone()
    dups = {"count": int(dup_row["c"]), "bytes": int(dup_row["b"]), "groups": int(dup_row["g"])}

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


@router.get("/cleanup/items")
def cleanup_items(
    category: str = Query(...),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    if category not in CATEGORY_SQL:
        raise HTTPException(400, f"Categoria non valida: {category}")
    where, params = CATEGORY_SQL[category]
    st = _cat_stats(conn, where, params)
    rows = conn.execute(
        deps.SELECT_FILE + f"{where} ORDER BY COALESCE(f.size,0) DESC, f.id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return {
        "category": category,
        "total": st["count"],
        "total_bytes": st["bytes"],
        "items": [deps.file_to_dict(r) for r in rows],
    }


def trash_file_ids(conn: sqlite3.Connection, raw_ids: list[int]) -> dict:
    """Sposta i file indicati nel Cestino di Windows, li rimuove dal catalogo e ripara eventuali gruppi duplicati."""
    ids = list(dict.fromkeys(int(x) for x in raw_ids))
    if not ids:
        raise HTTPException(400, "Nessun file selezionato.")
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT id, path, size FROM files WHERE id IN ({marks})", ids).fetchall()
    trashed_ids: list[int] = []
    freed = 0
    errors: list[dict] = []
    for r in rows:
        try:
            _send_to_recycle_bin(r["path"])
            trashed_ids.append(int(r["id"]))
            freed += int(r["size"] or 0)
        except Exception as exc:  # noqa: BLE001
            errors.append({"id": int(r["id"]), "path": r["path"], "error": str(exc)})
    if trashed_ids:
        tmarks = ",".join("?" * len(trashed_ids))
        # Se tra i file eliminati c'era il master di un gruppo duplicati, promuovi uno dei superstiti.
        orphans = conn.execute(
            f"SELECT file_id, dup_of FROM plan WHERE dup_of IN ({tmarks}) AND file_id NOT IN ({tmarks})",
            (*trashed_ids, *trashed_ids),
        ).fetchall()
        by_old_root: dict[int, list[int]] = {}
        for o in orphans:
            by_old_root.setdefault(int(o["dup_of"]), []).append(int(o["file_id"]))
        for old_root, survivors in by_old_root.items():
            survivors.sort()
            new_master = survivors[0]
            # Recupera la cartella dell'ex master per assegnarla al nuovo master se era in _Duplicati
            old_row = conn.execute("SELECT folder FROM plan WHERE file_id=?", (old_root,)).fetchone()
            old_folder = old_row["folder"] if old_row else None
            if old_folder:
                conn.execute("UPDATE plan SET is_dup=0, dup_of=NULL, folder=? WHERE file_id=?", (old_folder, new_master))
            else:
                conn.execute("UPDATE plan SET is_dup=0, dup_of=NULL WHERE file_id=?", (new_master,))
            for other in survivors[1:]:
                conn.execute("UPDATE plan SET dup_of=? WHERE file_id=?", (new_master, other))
        conn.execute(f"DELETE FROM files WHERE id IN ({tmarks})", trashed_ids)
        # Pulisci eventuali righe in dup_keep se la tabella esiste
        has_dk = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dup_keep'").fetchone()
        if has_dk:
            conn.execute(f"DELETE FROM dup_keep WHERE file_id IN ({tmarks})", trashed_ids)
        conn.commit()
    return {"trashed": len(trashed_ids), "freed_bytes": freed, "errors": errors}


@router.post("/cleanup/trash")
def cleanup_trash(body: TrashIn, conn: sqlite3.Connection = Depends(deps.get_db)):
    """Sposta i file scelti nel Cestino di Windows e li rimuove dal catalogo."""
    if not body.confirm:
        raise HTTPException(400, "Serve la conferma esplicita per spostare i file nel Cestino.")
    return trash_file_ids(conn, body.file_ids)
