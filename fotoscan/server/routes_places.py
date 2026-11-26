"""Luoghi (cluster GPS) e statistiche per la pagina Statistiche. Vedi docs/CONTRACT.md §2."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from . import deps

router = APIRouter(prefix="/api", tags=["places"])

# File con coordinate valide: lat/lon presenti e non il punto nullo (0,0) (errore tipico dei GPS senza fix).
GPS_OK = "f.lat IS NOT NULL AND f.lon IS NOT NULL AND NOT (f.lat = 0 AND f.lon = 0)"
MAX_PAGE = 500


def _check_precision(precision: int) -> int:
    if not 0 <= precision <= 4:
        raise HTTPException(400, "precisione non valida: usa un valore tra 0 e 4")
    return precision


def _coord_label(lat: float, lon: float, p: int) -> str:
    """'Lat 41,9° N · Lon 12,5° E' in italiano (virgola decimale), senza geocoding."""
    def fmt(v: float, pos: str, neg: str) -> str:
        return f"{abs(v):.{p}f}".replace(".", ",") + "° " + (pos if v >= 0 else neg)
    return f"Lat {fmt(lat, 'N', 'S')} · Lon {fmt(lon, 'E', 'W')}"


@router.get("/places")
def places(precision: int = 1, conn: sqlite3.Connection = Depends(deps.get_db)):
    """Cluster GPS su griglia: precision = decimali (1 ~ 11 km, 2 ~ 1 km)."""
    p = _check_precision(precision)
    rows = conn.execute(
        f"SELECT ROUND(f.lat, ?) AS glat, ROUND(f.lon, ?) AS glon, COUNT(*) AS count, MIN(f.id) AS sample_id "
        f"FROM files f WHERE {GPS_OK} GROUP BY glat, glon ORDER BY count DESC, glat, glon",
        (p, p),
    ).fetchall()
    out = []
    for r in rows:
        lat, lon = float(r["glat"]) + 0.0, float(r["glon"]) + 0.0  # +0.0 evita "-0.0"
        out.append({"lat": lat, "lon": lon, "count": r["count"], "sample_id": r["sample_id"],
                    "label": _coord_label(lat, lon, p)})
    return out


@router.get("/places/files")
def places_files(lat: float | None = None, lon: float | None = None, precision: int = 1,
                 offset: int = 0, limit: int = 120, conn: sqlite3.Connection = Depends(deps.get_db)):
    """File di una cella della griglia (stessa arrotondatura di /places)."""
    if lat is None or lon is None:
        raise HTTPException(400, "coordinate mancanti: servono lat e lon")
    p = _check_precision(precision)
    offset = max(offset, 0)
    limit = min(max(limit, 1), MAX_PAGE)
    where = f"{GPS_OK} AND ROUND(f.lat, ?) = ROUND(?, ?) AND ROUND(f.lon, ?) = ROUND(?, ?)"
    args = (p, lat, p, p, lon, p)
    total = conn.execute(f"SELECT COUNT(*) FROM files f WHERE {where}", args).fetchone()[0]
    rows = conn.execute(
        deps.SELECT_FILE + f"WHERE {where} ORDER BY f.date_taken DESC, f.id DESC LIMIT ? OFFSET ?",
        args + (limit, offset),
    ).fetchall()
    return {"total": total, "items": [deps.file_to_dict(r) for r in rows]}


@router.get("/stats/overview")
def stats_overview(conn: sqlite3.Connection = Depends(deps.get_db)):
    """Una query per sezione. Le date valide sono quelle che iniziano con AAAA-."""
    valid_date = "date_taken IS NOT NULL AND date_taken GLOB '[0-9][0-9][0-9][0-9]-*'"

    per_year = [
        {"year": int(r["y"]), "photos": r["photos"], "videos": r["videos"]}
        for r in conn.execute(
            "SELECT substr(date_taken, 1, 4) AS y, SUM(kind = 'image') AS photos, SUM(kind = 'video') AS videos "
            f"FROM files WHERE {valid_date} GROUP BY y ORDER BY y"
        )
    ]

    per_context = [
        {"context": r["context"], "count": r["count"]}
        for r in conn.execute(
            "SELECT p.context AS context, COUNT(*) AS count FROM files f JOIN plan p ON p.file_id = f.id "
            "WHERE p.context IS NOT NULL AND p.context <> '' AND COALESCE(p.is_dup, 0) = 0 "
            "GROUP BY p.context ORDER BY count DESC, context"
        )
    ]

    # conteggio e dimensione per tipo in una sola scansione
    kind_rows = conn.execute(
        "SELECT kind, COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes FROM files GROUP BY kind ORDER BY count DESC"
    ).fetchall()
    per_kind = [{"kind": r["kind"], "count": r["count"]} for r in kind_rows]

    date_sources = [
        {"source": r["source"], "count": r["count"]}
        for r in conn.execute(
            "SELECT COALESCE(date_source, 'nessuna') AS source, COUNT(*) AS count FROM files "
            "GROUP BY source ORDER BY count DESC, source"
        )
    ]

    top_days = [
        {"day": r["day"], "count": r["count"]}
        for r in conn.execute(
            "SELECT substr(date_taken, 1, 10) AS day, COUNT(*) AS count FROM files "
            f"WHERE {valid_date} GROUP BY day ORDER BY count DESC, day DESC LIMIT 10"
        )
    ]

    size_by_kind = {"image": 0, "video": 0}
    for r in kind_rows:
        size_by_kind[r["kind"]] = r["bytes"]

    return {
        "per_year": per_year,
        "per_context": per_context,
        "per_kind": per_kind,
        "date_sources": date_sources,
        "top_days": top_days,
        "size_total": sum(size_by_kind.values()),
        "size_by_kind": size_by_kind,
    }
