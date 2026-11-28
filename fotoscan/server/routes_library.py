"""Album (raccolte di foto) e collegamenti file<->album. Vedi docs/CONTRACT.md §2 (routes_library).

Gli album sono solo collegamenti nel catalogo: eliminare un album o togliere foto non tocca mai i file.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Annotated

from pydantic import BaseModel, Field

from . import deps

router = APIRouter(prefix="/api", tags=["library"])

NAME_MAX = 80
_CHUNK = 500  # parametri per IN (...) per query, sotto il limite SQLite

# Una sola query per la lista: conteggio via subquery correlata (niente N+1).
# cover_id = cover_file_id se ancora nell'album, altrimenti la prima foto per data di scatto.
ALBUM_SQL = """
SELECT a.id, a.name, a.created, a.cover_file_id,
  (SELECT COUNT(*) FROM album_items ai WHERE ai.album_id=a.id) AS count,
  COALESCE(
    CASE WHEN EXISTS(SELECT 1 FROM album_items x WHERE x.album_id=a.id AND x.file_id=a.cover_file_id)
         THEN a.cover_file_id END,
    (SELECT ai.file_id FROM album_items ai JOIN files f ON f.id=ai.file_id
      WHERE ai.album_id=a.id ORDER BY (f.date_taken IS NULL), f.date_taken, f.id LIMIT 1)
  ) AS cover_id
FROM albums a
"""


class AlbumIn(BaseModel):
    name: str | None = None


class AlbumPatch(BaseModel):
    # Distinguiamo "campo assente" da "null" con model_fields_set.
    name: str | None = None
    cover_file_id: Annotated[int, Field(gt=0, le=2**63 - 1)] | None = None


class ItemsIn(BaseModel):
    file_ids: list[Annotated[int, Field(gt=0, le=2**63 - 1)]] = []


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _album_dict(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "name": r["name"], "count": r["count"], "cover_id": r["cover_id"], "created": r["created"]}


def _require_album(conn: sqlite3.Connection, album_id: int) -> None:
    if not 0 < album_id <= 2**63 - 1 or conn.execute("SELECT 1 FROM albums WHERE id=?", (album_id,)).fetchone() is None:
        raise HTTPException(404, "Album non trovato")


def _clean_name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not name:
        raise HTTPException(400, "Il nome dell'album è obbligatorio")
    if len(name) > NAME_MAX:
        raise HTTPException(400, f"Il nome non può superare {NAME_MAX} caratteri")
    return name


def _check_unique_name(conn: sqlite3.Connection, name: str, exclude_id: int = -1) -> None:
    dup = conn.execute("SELECT 1 FROM albums WHERE name=? COLLATE NOCASE AND id!=?", (name, exclude_id)).fetchone()
    if dup:
        raise HTTPException(409, "Esiste già un album con questo nome")


def _unique_ids(ids: list[int]) -> list[int]:
    return list(dict.fromkeys(ids))


def _existing_file_ids(conn: sqlite3.Connection, ids: list[int]) -> list[int]:
    """Gli id che esistono davvero in `files`, nell'ordine ricevuto."""
    found: set[int] = set()
    for i in range(0, len(ids), _CHUNK):
        part = ids[i:i + _CHUNK]
        marks = ",".join("?" * len(part))
        found.update(r[0] for r in conn.execute(f"SELECT id FROM files WHERE id IN ({marks})", part))
    return [x for x in ids if x in found]


def _is_member(conn: sqlite3.Connection, album_id: int, file_id: int) -> bool:
    return conn.execute("SELECT 1 FROM album_items WHERE album_id=? AND file_id=?", (album_id, file_id)).fetchone() is not None


@router.get("/albums")
def list_albums(conn: sqlite3.Connection = Depends(deps.get_db)):
    rows = conn.execute(ALBUM_SQL + "ORDER BY a.name COLLATE NOCASE, a.id").fetchall()
    return [_album_dict(r) for r in rows]


@router.post("/albums", status_code=201)
def create_album(body: AlbumIn, conn: sqlite3.Connection = Depends(deps.get_db)):
    name = _clean_name(body.name)
    _check_unique_name(conn, name)
    try:
        cur = conn.execute("INSERT INTO albums(name, created) VALUES(?,?)", (name, _now()))
        conn.commit()
    except sqlite3.IntegrityError:  # due richieste insieme con lo stesso nome
        raise HTTPException(409, "Esiste già un album con questo nome") from None
    return _album_dict(conn.execute(ALBUM_SQL + "WHERE a.id=?", (cur.lastrowid,)).fetchone())


@router.put("/albums/{album_id}")
def update_album(album_id: int, body: AlbumPatch, conn: sqlite3.Connection = Depends(deps.get_db)):
    _require_album(conn, album_id)
    sets: list[str] = []
    vals: list = []
    if "name" in body.model_fields_set:
        name = _clean_name(body.name)
        _check_unique_name(conn, name, album_id)
        sets.append("name=?")
        vals.append(name)
    if "cover_file_id" in body.model_fields_set:
        cid = body.cover_file_id
        if cid is not None and not _is_member(conn, album_id, cid):
            raise HTTPException(400, "La copertina deve essere una foto dell'album")
        sets.append("cover_file_id=?")
        vals.append(cid)
    if sets:
        try:
            conn.execute(f"UPDATE albums SET {', '.join(sets)} WHERE id=?", (*vals, album_id))
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Esiste già un album con questo nome") from None
    return _album_dict(conn.execute(ALBUM_SQL + "WHERE a.id=?", (album_id,)).fetchone())


@router.delete("/albums/{album_id}")
def delete_album(album_id: int, conn: sqlite3.Connection = Depends(deps.get_db)):
    """Elimina solo l'album e i suoi collegamenti: i file restano dove sono."""
    _require_album(conn, album_id)
    conn.execute("DELETE FROM album_items WHERE album_id=?", (album_id,))
    conn.execute("DELETE FROM albums WHERE id=?", (album_id,))
    conn.commit()
    return {"ok": True}


@router.get("/albums/{album_id}/files")
def album_files(
    album_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(120, ge=1, le=1000),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    _require_album(conn, album_id)
    total = conn.execute("SELECT COUNT(*) FROM album_items WHERE album_id=?", (album_id,)).fetchone()[0]
    rows = conn.execute(
        deps.SELECT_FILE
        + "JOIN album_items ai ON ai.file_id=f.id WHERE ai.album_id=? "
        + "ORDER BY (f.date_taken IS NULL), f.date_taken, f.id LIMIT ? OFFSET ?",
        (album_id, limit, offset),
    ).fetchall()
    return {"total": total, "items": [deps.file_to_dict(r) for r in rows]}


@router.post("/albums/{album_id}/items")
def add_items(album_id: int, body: ItemsIn, conn: sqlite3.Connection = Depends(deps.get_db)):
    """Aggiunge foto all'album. Idempotente: le foto gia' presenti non vengono duplicate.
    Gli id inesistenti vengono ignorati e contati in `missing`."""
    _require_album(conn, album_id)
    ids = _unique_ids(body.file_ids)
    if not ids:
        raise HTTPException(400, "Nessuna foto selezionata")
    existing = _existing_file_ids(conn, ids)
    now = _now()
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO album_items(album_id, file_id, added) VALUES(?,?,?)",
        [(album_id, fid, now) for fid in existing],
    )
    conn.commit()
    added = conn.total_changes - before
    return {"added": added, "already_present": len(existing) - added, "missing": len(ids) - len(existing)}


@router.delete("/albums/{album_id}/items")
def remove_items(album_id: int, body: ItemsIn, conn: sqlite3.Connection = Depends(deps.get_db)):
    """Toglie foto dall'album (i file restano intatti)."""
    _require_album(conn, album_id)
    ids = _unique_ids(body.file_ids)
    if not ids:
        raise HTTPException(400, "Nessuna foto selezionata")
    before = conn.total_changes
    conn.executemany("DELETE FROM album_items WHERE album_id=? AND file_id=?", [(album_id, fid) for fid in ids])
    removed = conn.total_changes - before
    # se la copertina e' stata tolta dall'album, la si rimuove anche dal dato salvato
    conn.execute(
        "UPDATE albums SET cover_file_id=NULL WHERE id=? AND cover_file_id IS NOT NULL "
        "AND cover_file_id NOT IN (SELECT file_id FROM album_items WHERE album_id=?)",
        (album_id, album_id),
    )
    conn.commit()
    return {"removed": removed}


@router.get("/files/{file_id}/albums")
def file_albums(file_id: int, conn: sqlite3.Connection = Depends(deps.get_db)):
    if not 0 < file_id <= 2**63 - 1 or conn.execute("SELECT 1 FROM files WHERE id=?", (file_id,)).fetchone() is None:
        raise HTTPException(404, "File non trovato")
    rows = conn.execute(
        ALBUM_SQL + "WHERE a.id IN (SELECT album_id FROM album_items WHERE file_id=?) ORDER BY a.name COLLATE NOCASE, a.id",
        (file_id,),
    ).fetchall()
    return [_album_dict(r) for r in rows]
