"""Registro degli scarichi dal telefono: storico delle importazioni (la piu' recente per prima).

Usa la tabella `meta` del catalogo (chiave/valore), quindi non richiede tabelle nuove.
L'ultimo scarico resta disponibile anche sotto KEY (usato dallo stato dell'archivio).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import db as dbm

KEY = "phone_last_import"          # ultimo scarico (compatibilita' con /status)
HISTORY_KEY = "phone_import_history"
HISTORY_MAX = 200


@dataclass
class ImportRecord:
    when: str            # ISO, ora locale (inizio dell'importazione)
    device: str          # es. "Pixel 11 Pro"
    new_files: int       # file copiati in questo scarico
    total_seen: int      # file letti dal telefono in questo scarico
    state: str = "done"  # done | cancelled | error
    storage: str = ""
    folders: list[str] = field(default_factory=list)   # cartelle scelte, es. "DCIM/Camera"
    dest: str = ""
    skipped_known: int = 0      # gia' presenti rilevati prima della copia (non trasferiti)
    skipped_existing: int = 0   # gia' presenti rilevati dopo la copia
    errors: int = 0
    error: str = ""

    @property
    def when_dt(self) -> datetime:
        return datetime.fromisoformat(self.when)


def _from_raw(raw: str | None) -> ImportRecord | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        known = {k: data[k] for k in ImportRecord.__dataclass_fields__ if k in data}
        return ImportRecord(**known)
    except (ValueError, TypeError):
        return None  # valore corrotto: meglio non mostrarlo che sbagliare


def last_import(conn: sqlite3.Connection) -> ImportRecord | None:
    return _from_raw(dbm.get_meta(conn, KEY))


def history(conn: sqlite3.Connection) -> list[ImportRecord]:
    raw = dbm.get_meta(conn, HISTORY_KEY)
    try:
        items = json.loads(raw) if raw else []
    except ValueError:
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        rec = _from_raw(json.dumps(it))
        if rec is not None:
            out.append(rec)
    return out


def record_import(conn: sqlite3.Connection, device: str, new_files: int, total_seen: int, **extra) -> ImportRecord:
    """Registra uno scarico: aggiorna l'ultimo e aggiunge allo storico. `extra` = campi opzionali di ImportRecord."""
    rec = ImportRecord(when=extra.pop("when", None) or datetime.now().isoformat(timespec="seconds"),
                       device=device, new_files=int(new_files), total_seen=int(total_seen), **extra)
    payload = json.dumps(asdict(rec), ensure_ascii=False)
    dbm.set_meta(conn, KEY, payload)
    items = [asdict(r) for r in history(conn)]
    items.insert(0, asdict(rec))
    dbm.set_meta(conn, HISTORY_KEY, json.dumps(items[:HISTORY_MAX], ensure_ascii=False))
    return rec


def days_since(rec: ImportRecord | None, now: datetime | None = None) -> int | None:
    if rec is None:
        return None
    now = now or datetime.now()
    return (now - rec.when_dt).days
