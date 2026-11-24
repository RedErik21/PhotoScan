"""Route dei file: lista filtrata e paginata, dettaglio, timeline, ricordi, preferiti.

Paginazione: `GET /files` accetta `limit` tra 1 e 500 (default 120); valori maggiori danno 422.
Per "tutti gli id" di un filtro usare `GET /files/ids` (limit 1..50000, default 5000, stesso ordine di /files).
Il filtro `day=AAAA-MM-GG` vale per /files, /files/ids e /timeline/*.

Ordinamento `date_desc`/`date_asc`: data (ISO, quindi ordinabile come testo), file senza data in coda, poi id.
I bucket della timeline usano lo stesso ordine e gli stessi filtri, quindi `offset` coincide con la posizione in /files.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from . import deps

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["files"])

# FROM/JOIN della query base, condiviso con deps.SELECT_FILE per restare allineati.
_FROM = deps.SELECT_FILE[deps.SELECT_FILE.index(" FROM ") + 1:]

# In SQLite i NULL sono minori di ogni valore: in DESC finiscono in coda da soli.
# Le date incerte (ricavate dalla data di modifica del file) vanno in coda a tutto, in un gruppo proprio
# ("Data incerta"): il primo termine dell'ordinamento le separa dalle date certe.
_ORDER = {
    "date_desc": "COALESCE(f.data_incerta,0), f.date_taken DESC, f.id DESC",
    "date_asc": "COALESCE(f.data_incerta,0), f.date_taken ASC NULLS LAST, f.id ASC",
    "name": "f.name COLLATE NOCASE, f.id",
}

# Indice (status, date_taken): serve WHERE status='embedded' e ORDER BY data senza sort temporaneo (~4 ms su 14k).
_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_files_status_date ON files(status, date_taken)",
]


def _ensure_indexes() -> None:
    try:
        conn = deps.open_db()
        try:
            for sql in _INDEXES:
                conn.execute(sql)
            conn.commit()
        finally:
            conn.close()
    except Exception:  # l'app deve partire anche se l'indice non si crea
        log.exception("indici di routes_files non creati")


router.on_startup.append(_ensure_indexes)


# ------------------------------------------------------------------ parametri
def _intero(nome: str, valore: str | None, minimo: int, massimo: int, default: int | None = None) -> int | None:
    if valore is None or valore == "":
        return default
    try:
        n = int(valore)
    except ValueError:
        raise HTTPException(422, f"'{nome}' deve essere un numero intero") from None
    if not minimo <= n <= massimo:
        raise HTTPException(422, f"'{nome}' deve essere tra {minimo} e {massimo}")
    return n


def _booleano(nome: str, valore: str | None) -> bool | None:
    if valore is None or valore == "":
        return None
    v = valore.strip().lower()
    if v in ("1", "true", "si", "sì"):
        return True
    if v in ("0", "false", "no"):
        return False
    raise HTTPException(422, f"'{nome}' deve essere 1 o 0")


def _ordinamento(valore: str | None) -> str:
    v = valore or "date_desc"
    if v not in _ORDER:
        raise HTTPException(422, "'sort' deve essere date_desc, date_asc o name")
    return v


@dataclass
class Filtri:
    kind: str | None = None
    year: int | None = None
    month: int | None = None
    day: str | None = None
    favorite: bool | None = None
    folder: str | None = None
    dup: str = "hide"
    uncertain: bool | None = None
    broken: str = "all"   # all | hide (esclude i file mancanti/illeggibili gia' scoperti)
    person_id: int | None = None


def filtri(
    kind: str | None = Query(None),
    year: str | None = Query(None),
    month: str | None = Query(None),
    day: str | None = Query(None),
    favorite: str | None = Query(None),
    folder: str | None = Query(None),
    dup: str | None = Query(None),
    uncertain: str | None = Query(None),
    broken: str | None = Query(None),
    person_id: str | None = Query(None),
) -> Filtri:
    """Dipendenza comune a /files, /files/ids e /timeline/*: valida e normalizza i filtri."""
    f = Filtri()
    if kind:
        if kind not in ("image", "video"):
            raise HTTPException(422, "'kind' deve essere image o video")
        f.kind = kind
    f.year = _intero("year", year, 1900, 2200)
    f.month = _intero("month", month, 1, 12)
    if day:
        try:
            f.day = date.fromisoformat(day).isoformat()
        except ValueError:
            raise HTTPException(422, "'day' deve essere nel formato AAAA-MM-GG") from None
    f.favorite = _booleano("favorite", favorite)
    f.uncertain = _booleano("uncertain", uncertain)
    if folder:
        f.folder = folder  # confronto esatto con parametro: nessun problema con caratteri speciali
    if dup:
        if dup not in ("hide", "only", "all"):
            raise HTTPException(422, "'dup' deve essere hide, only o all")
        f.dup = dup
    if broken:
        if broken not in ("hide", "all"):
            raise HTTPException(422, "'broken' deve essere hide o all")
        f.broken = broken
    f.person_id = _intero("person_id", person_id, 1, 10**9)
    return f


def _where(f: Filtri) -> tuple[str, list]:
    """Clausole WHERE (sempre con status='embedded') e relativi parametri."""
    cl = ["f.status='embedded'"]
    args: list = []
    if f.kind:
        cl.append("f.kind=?")
        args.append(f.kind)
    if f.day:
        nxt = (date.fromisoformat(f.day) + timedelta(days=1)).isoformat()
        cl.append("f.date_taken >= ? AND f.date_taken < ?")
        args += [f.day, nxt]
    if f.year:
        cl.append("f.date_taken >= ? AND f.date_taken < ?")
        args += [f"{f.year:04d}-01-01", f"{f.year + 1:04d}-01-01"]
    if f.month:
        cl.append("substr(f.date_taken,6,2)=?")
        args.append(f"{f.month:02d}")
    if f.favorite is True:
        cl.append("fv.file_id IS NOT NULL")
    elif f.favorite is False:
        cl.append("fv.file_id IS NULL")
    if f.folder:
        cl.append("p.folder=?")
        args.append(f.folder)
    if f.dup == "hide":
        cl.append("COALESCE(p.is_dup,0)=0")
    elif f.dup == "only":
        cl.append("COALESCE(p.is_dup,0)>0")
    if f.broken == "hide":
        cl.append("NOT EXISTS (SELECT 1 FROM broken b WHERE b.file_id=f.id)")
    if f.uncertain is True:
        cl.append("f.data_incerta=1")
    elif f.uncertain is False:
        cl.append("COALESCE(f.data_incerta,0)=0")
    if f.person_id is not None:
        cl.append("EXISTS (SELECT 1 FROM faces fc WHERE fc.file_id=f.id AND fc.person_id=? AND COALESCE(fc.excluded,0)=0)")
        args.append(f.person_id)
    return " AND ".join(cl), args


def _id_file(valore: str) -> int:
    if not valore.isdigit():
        raise HTTPException(422, "id file non valido")
    return int(valore)


def _lista(conn: sqlite3.Connection, f: Filtri, sort: str, offset: int, limit: int) -> tuple[int, list]:
    where, args = _where(f)
    total = conn.execute(f"SELECT COUNT(*) {_FROM} WHERE {where}", args).fetchone()[0]
    rows = conn.execute(
        f"{deps.SELECT_FILE} WHERE {where} ORDER BY {_ORDER[sort]} LIMIT ? OFFSET ?", [*args, limit, offset]
    ).fetchall()
    return total, [deps.file_to_dict(r) for r in rows]


# ------------------------------------------------------------------ file
@router.get("/files")
def list_files(
    f: Filtri = Depends(filtri),
    sort: str | None = Query(None),
    offset: str | None = Query(None),
    limit: str | None = Query(None),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    ordine = _ordinamento(sort)
    off = _intero("offset", offset, 0, 10**9, 0)
    lim = _intero("limit", limit, 1, 500, 120)
    total, items = _lista(conn, f, ordine, off, lim)
    return {"total": total, "items": items}


@router.get("/files/ids")
def list_ids(
    f: Filtri = Depends(filtri),
    sort: str | None = Query(None),
    limit: str | None = Query(None),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    """Id dei file che rispettano i filtri, nello stesso ordine di /files (per "seleziona tutti")."""
    ordine = _ordinamento(sort)
    lim = _intero("limit", limit, 1, 50000, 5000)
    where, args = _where(f)
    total = conn.execute(f"SELECT COUNT(*) {_FROM} WHERE {where}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT f.id {_FROM} WHERE {where} ORDER BY {_ORDER[ordine]} LIMIT ?", [*args, lim]
    ).fetchall()
    return {"ids": [r[0] for r in rows], "total": total}


@router.get("/files/{file_id}")
def get_file(file_id: str, conn: sqlite3.Connection = Depends(deps.get_db)):
    fid = _id_file(file_id)
    row = conn.execute(f"{deps.SELECT_FILE} WHERE f.id=?", (fid,)).fetchone()
    if row is None:
        raise HTTPException(404, "file non trovato")
    if row["status"] == "error":  # file danneggiato: non si mostra in visione
        raise HTTPException(404, "File danneggiato: non disponibile per la visione.")
    out = deps.file_to_dict(row)
    out["hash"] = row["hash"]
    out["status"] = row["status"]
    # gruppo: stesso hash, oppure stesso master (dup_of) di questo file
    radice = row["dup_of"] or fid
    cond, args = "f.id=? OR p.dup_of=?", [radice, radice]
    if row["hash"]:
        cond += " OR f.hash=?"
        args.append(row["hash"])
    gruppo = conn.execute(
        f"{deps.SELECT_FILE} WHERE {cond} ORDER BY f.date_taken IS NULL, f.date_taken, f.id LIMIT 200", args
    ).fetchall()
    out["group"] = [deps.file_to_dict(r) for r in gruppo]
    out["exif_summary"] = None  # i metadati EXIF completi non sono nel catalogo
    return out


# ------------------------------------------------------------------ timeline
@router.get("/timeline/buckets")
def timeline_buckets(
    granularity: str | None = Query(None),
    f: Filtri = Depends(filtri),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    gran = granularity or "month"
    if gran not in ("month", "day"):
        raise HTTPException(422, "'granularity' deve essere month o day")
    n = 7 if gran == "month" else 10
    where, args = _where(f)
    # Stesso ordine di _ORDER["date_desc"]: date certe (per giorno/mese), poi senza data, poi date incerte.
    certe = f"{where} AND COALESCE(f.data_incerta,0)=0"
    rows = conn.execute(
        f"SELECT substr(f.date_taken,1,{n}) AS k, COUNT(*) AS c {_FROM} "
        f"WHERE {certe} AND f.date_taken IS NOT NULL GROUP BY k ORDER BY k DESC",
        args,
    ).fetchall()
    senza_data = conn.execute(f"SELECT COUNT(*) {_FROM} WHERE {certe} AND f.date_taken IS NULL", args).fetchone()[0]
    incerte = conn.execute(f"SELECT COUNT(*) {_FROM} WHERE {where} AND f.data_incerta=1", args).fetchone()[0]

    buckets, offset = [], 0
    for r in rows:
        k = r["k"]
        b = {"key": k, "year": int(k[:4]), "month": int(k[5:7])}
        if gran == "day":
            b["day"] = int(k[8:10])
        b["count"] = r["c"]
        b["offset"] = offset
        offset += r["c"]
        buckets.append(b)
    for chiave, n_file in (("unknown", senza_data), ("uncertain", incerte)):
        if not n_file:
            continue
        b = {"key": chiave, "year": None, "month": None}
        if gran == "day":
            b["day"] = None
        b["count"] = n_file
        b["offset"] = offset
        offset += n_file
        buckets.append(b)
    return {"buckets": buckets, "total": offset}


@router.get("/timeline/years")
def timeline_years(f: Filtri = Depends(filtri), conn: sqlite3.Connection = Depends(deps.get_db)):
    where, args = _where(f)
    rows = conn.execute(
        f"SELECT CAST(substr(f.date_taken,1,4) AS INTEGER) AS y, COUNT(*) AS c {_FROM} "
        f"WHERE {where} AND f.date_taken IS NOT NULL GROUP BY y ORDER BY y DESC",
        args,
    ).fetchall()
    return [{"year": r["y"], "count": r["c"]} for r in rows]


# ------------------------------------------------------------------ ricordi
@router.get("/memories")
def memories(
    limit: str | None = Query(None),
    on: str | None = Query(None),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    """File con la stessa data (giorno e mese) negli anni passati. `limit` = massimo per anno."""
    lim = _intero("limit", limit, 1, 100, 12)
    if on:
        try:
            oggi = date.fromisoformat(on)
        except ValueError:
            raise HTTPException(422, "'on' deve essere nel formato AAAA-MM-GG") from None
    else:
        oggi = date.today()
    rows = conn.execute(
        f"{deps.SELECT_FILE} WHERE f.status='embedded' AND COALESCE(p.is_dup,0)=0 "
        "AND f.date_taken IS NOT NULL AND substr(f.date_taken,6,5)=? "
        "AND CAST(substr(f.date_taken,1,4) AS INTEGER) < ? ORDER BY f.date_taken, f.id",
        (oggi.strftime("%m-%d"), oggi.year),
    ).fetchall()
    per_anno: dict[int, list] = {}
    for r in rows:
        gruppo = per_anno.setdefault(int(r["date_taken"][:4]), [])
        if len(gruppo) < lim:
            gruppo.append(deps.file_to_dict(r))
    return [{"year": y, "items": per_anno[y]} for y in sorted(per_anno, reverse=True)]


# ------------------------------------------------------------------ preferiti
def _ora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ids_validi(ids: object) -> list[int]:
    if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise HTTPException(422, "'ids' deve essere una lista di numeri")
    if len(ids) > 50000:
        raise HTTPException(422, "'ids' contiene troppi elementi (massimo 50000)")
    return ids


def _scrivi_preferiti(conn: sqlite3.Connection, ids: list[int], valore: bool) -> int:
    """Aggiunge o toglie i preferiti; ritorna quante righe sono cambiate."""
    prima = conn.total_changes
    if valore:
        conn.executemany(
            "INSERT OR IGNORE INTO favorites(file_id, added) SELECT id, ? FROM files WHERE id=?",
            [(_ora(), i) for i in ids],
        )
    else:
        conn.executemany("DELETE FROM favorites WHERE file_id=?", [(i,) for i in ids])
    conn.commit()
    return conn.total_changes - prima


def _valore(body: dict) -> bool:
    v = body.get("value") if isinstance(body, dict) else None
    if not isinstance(v, bool):
        raise HTTPException(422, "'value' deve essere true o false")
    return v


@router.post("/files/{file_id}/favorite")
def set_favorite(file_id: str, body: dict = Body(...), conn: sqlite3.Connection = Depends(deps.get_db)):
    fid = _id_file(file_id)
    valore = _valore(body)
    if conn.execute("SELECT 1 FROM files WHERE id=?", (fid,)).fetchone() is None:
        raise HTTPException(404, "file non trovato")
    _scrivi_preferiti(conn, [fid], valore)
    return {"favorite": valore}


@router.post("/files/favorite")
def set_favorite_bulk(body: dict = Body(...), conn: sqlite3.Connection = Depends(deps.get_db)):
    ids = _ids_validi(body.get("ids") if isinstance(body, dict) else None)
    valore = _valore(body)
    if not ids:
        return {"changed": 0}
    return {"changed": _scrivi_preferiti(conn, list(dict.fromkeys(ids)), valore)}


# ------------------------------------------------------------------ file non utilizzabili
def _broken_summary(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT b.reason, COUNT(*) AS n FROM broken b JOIN files f ON f.id=b.file_id "
        "WHERE f.status='embedded' GROUP BY b.reason").fetchall()
    by = {r["reason"]: r["n"] for r in rows}
    return {"total": sum(by.values()), "mancanti": by.get("mancante", 0), "illeggibili": by.get("illeggibile", 0)}


@router.get("/library/broken")
def broken_summary(conn: sqlite3.Connection = Depends(deps.get_db)):
    """Quanti file del catalogo risultano mancanti sul disco o illeggibili (gia' scoperti)."""
    return _broken_summary(conn)


@router.post("/library/broken/check")
def broken_check(conn: sqlite3.Connection = Depends(deps.get_db)):
    """Controlla che ogni file del catalogo esista ancora sul disco (solo lettura del disco).
    Segna i mancanti; toglie il segno ai file 'mancanti' che sono ricomparsi."""
    import os

    rows = conn.execute("SELECT id, path FROM files WHERE status='embedded'").fetchall()
    gone = [r["id"] for r in rows if not os.path.isfile(r["path"])]
    gone_set = set(gone)
    back = [r["file_id"] for r in conn.execute("SELECT file_id FROM broken WHERE reason='mancante'")
            if r["file_id"] not in gone_set]
    conn.executemany("INSERT INTO broken(file_id, reason, checked) VALUES(?, 'mancante', ?) "
                     "ON CONFLICT(file_id) DO UPDATE SET reason='mancante', checked=excluded.checked",
                     [(i, _ora()) for i in gone])
    conn.executemany("DELETE FROM broken WHERE file_id=?", [(i,) for i in back])
    conn.commit()
    return {**_broken_summary(conn), "checked": len(rows)}


@router.post("/library/broken/remove")
def broken_remove(body: dict = Body(...), conn: sqlite3.Connection = Depends(deps.get_db)):
    """Toglie dal CATALOGO le voci dei file mancanti/illeggibili. Nessun file sul disco viene toccato."""
    if body.get("confirm") is not True:
        raise HTTPException(400, "Serve la conferma esplicita (confirm: true).")
    ids = [r["file_id"] for r in conn.execute("SELECT file_id FROM broken")]
    if ids:
        conn.executemany("UPDATE albums SET cover_file_id=NULL WHERE cover_file_id=?", [(i,) for i in ids])
        conn.executemany("DELETE FROM file_override WHERE file_id=?", [(i,) for i in ids])
        conn.executemany("DELETE FROM faces_done WHERE file_id=?", [(i,) for i in ids])
        conn.executemany("UPDATE plan SET dup_of=NULL WHERE dup_of=?", [(i,) for i in ids])
        # embeddings, plan, favorites, album_items, faces e broken: ON DELETE CASCADE
        conn.executemany("DELETE FROM files WHERE id=?", [(i,) for i in ids])
        conn.commit()
    return {"removed": len(ids)}
