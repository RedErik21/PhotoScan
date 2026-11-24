"""Costruisce la base cartografica offline di Luoghi (frontend/vendor/geo/world.json) dai dati Natural Earth
(dominio pubblico, https://www.naturalearthdata.com). Serve solo a chi sviluppa: il file generato e' gia' nel progetto.

  python tools/build_geo.py --src <cartella con i .geojson scaricati>

File attesi in --src (da github.com/nvkelso/natural-earth-vector/tree/master/geojson):
  ne_10m_admin_0_countries.geojson, ne_10m_admin_1_states_provinces_lines.geojson, ne_10m_populated_places_simple.geojson

Formato di uscita (coordinate intere = gradi * 1000, ogni anello come lista piatta [lon0, lat0, dlon1, dlat1, ...] a delta):
  {"scale": 1000, "land": [ring, ...], "borders1": [line, ...], "places": [[name, lon, lat, rank], ...]}
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

SCALE = 1000
EUROPE = (-25.0, 34.0, 45.0, 72.0)   # bbox delle linee regionali (lon_min, lat_min, lon_max, lat_max)


def simplify(pts: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Douglas-Peucker iterativo."""
    if len(pts) < 4:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    tol2 = tol * tol
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if L == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, idx = d, i
        if best > tol2 and idx > 0:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(pts, keep) if k]


def encode(pts) -> list[int]:
    out: list[int] = []
    px = py = 0
    for i, (x, y) in enumerate(pts):
        ix, iy = round(x * SCALE), round(y * SCALE)
        if i == 0:
            out += [ix, iy]
        elif ix != px or iy != py:
            out += [ix - px, iy - py]
        else:
            continue
        px, py = ix, iy
    return out


def rings_of(geom):
    t, c = geom["type"], geom["coordinates"]
    if t == "Polygon":
        return c
    if t == "MultiPolygon":
        return [r for poly in c for r in poly]
    if t == "LineString":
        return [c]
    if t == "MultiLineString":
        return c
    return []


def in_bbox(pts, bb) -> bool:
    return any(bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3] for x, y in pts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "frontend" / "vendor" / "geo" / "world.json"))
    ap.add_argument("--tol", type=float, default=0.008, help="tolleranza di semplificazione in gradi")
    a = ap.parse_args()
    src = Path(a.src)

    land = []
    for f in json.loads((src / "ne_10m_admin_0_countries.geojson").read_text(encoding="utf-8"))["features"]:
        for ring in rings_of(f["geometry"]):
            s = simplify([tuple(p[:2]) for p in ring], a.tol)
            if len(s) >= 4:
                land.append(encode(s))

    borders1 = []
    for f in json.loads((src / "ne_10m_admin_1_states_provinces_lines.geojson").read_text(encoding="utf-8"))["features"]:
        for line in rings_of(f["geometry"]):
            pts = [tuple(p[:2]) for p in line]
            if not in_bbox(pts, EUROPE):
                continue
            s = simplify(pts, a.tol)
            if len(s) >= 2:
                borders1.append(encode(s))

    places = []
    for f in json.loads((src / "ne_10m_populated_places_simple.geojson").read_text(encoding="utf-8"))["features"]:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        rank = int(p.get("scalerank", 10))
        europe = EUROPE[0] <= lon <= EUROPE[2] and EUROPE[1] <= lat <= EUROPE[3]
        if rank <= 3 or europe:
            places.append([p.get("name") or p.get("nameascii"), round(lon * SCALE), round(lat * SCALE), rank])
    places.sort(key=lambda r: r[3])

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"scale": SCALE, "source": "Natural Earth (public domain)", "land": land,
                               "borders1": borders1, "places": places}, separators=(",", ":")), encoding="utf-8")
    print(f"{out}  {out.stat().st_size / 1024:.0f} KB  land={len(land)} borders1={len(borders1)} places={len(places)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
