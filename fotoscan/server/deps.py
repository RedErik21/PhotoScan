"""Dipendenze condivise dalle route: configurazione, connessione DB, serializzazione dei file."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterator

from fastapi import HTTPException

from .. import db as dbm
from ..config import CONFIG_FILE, Config, load_config

_lock = threading.Lock()
_cfg: Config | None = None
_cfg_stamp: tuple | None = None


def _stamp() -> tuple:
    try:
        m = CONFIG_FILE.stat().st_mtime
    except OSError:
        m = None
    return (m,)


def get_cfg() -> Config:
    """Config corrente; si ricarica da solo se config.json cambia."""
    global _cfg, _cfg_stamp
    with _lock:
        st = _stamp()
        if _cfg is None or st != _cfg_stamp:
            _cfg, _cfg_stamp = load_config(), st
        return _cfg


def reload_cfg() -> Config:
    """Forza la rilettura (dopo aver salvato config.json)."""
    global _cfg
    with _lock:
        _cfg = None
    return get_cfg()


def get_db() -> Iterator[sqlite3.Connection]:
    """Dipendenza FastAPI: una connessione per richiesta (sqlite, WAL), chiusa a fine richiesta."""
    conn = dbm.connect(get_cfg().db_path)
    try:
        yield conn
    finally:
        conn.close()


def open_db() -> sqlite3.Connection:
    """Connessione fuori da una richiesta (thread di job). Chiudila tu."""
    return dbm.connect(get_cfg().db_path)


def volume_online(path: str) -> bool:
    """False se nessuna cartella antenata del file esiste (disco esterno scollegato, NAS spento, unita'
    di rete non raggiungibile): in quel caso un file assente non e' "mancante", e' solo non raggiungibile."""
    try:
        parent = Path(path).parent
        return any(c.exists() for c in (parent, *parent.parents))
    except (OSError, ValueError):
        return False


def valid_id(n: int) -> int:
    """Id file/album entro i limiti di SQLite (INTEGER a 64 bit): oltre darebbe OverflowError (500)."""
    if not 0 < n <= 2**63 - 1:
        raise HTTPException(404, "non trovato")
    return n


# Colonne standard di un file + info piano/preferiti. Usare SELECT_FILE come base di ogni query "lista file".
SELECT_FILE = (
    "SELECT f.id, f.name, f.kind, f.size, f.path, f.date_taken, f.date_source, f.data_incerta, f.lat, f.lon, "
    "f.width, f.height, f.duration, f.hash, f.status, "
    "p.folder AS plan_folder, p.context AS plan_context, p.score AS plan_score, "
    "COALESCE(p.is_dup,0) AS is_dup, p.dup_of AS dup_of, (fv.file_id IS NOT NULL) AS favorite "
    "FROM files f LEFT JOIN plan p ON p.file_id=f.id LEFT JOIN favorites fv ON fv.file_id=f.id "
)


def file_to_dict(r: sqlite3.Row) -> dict:
    """Oggetto File del contratto API (vedi docs/CONTRACT.md)."""
    fid = r["id"]
    return {
        "id": fid,
        "name": r["name"],
        "kind": r["kind"],
        "size": r["size"],
        "path": r["path"],
        "date": r["date_taken"],
        "date_source": r["date_source"],
        "date_uncertain": bool(r["data_incerta"]),
        "lat": r["lat"],
        "lon": r["lon"],
        "width": r["width"],
        "height": r["height"],
        "duration": r["duration"],
        "folder": r["plan_folder"],
        "context": r["plan_context"],
        "score": r["plan_score"],
        "is_dup": r["is_dup"],
        "dup_of": r["dup_of"],
        "favorite": bool(r["favorite"]),
        "thumb": f"/api/files/{fid}/thumb",
        "display": f"/api/files/{fid}/display",
        "media": f"/api/files/{fid}/media",
    }
