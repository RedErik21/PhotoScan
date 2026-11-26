"""Piano di organizzazione (dry-run) e applicazione reale tramite hard link / copia.

REGOLA: la cartella sorgente non viene mai modificata. L'unica scrittura e' in output_dir.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageOps

from .config import Config
from .db import get_meta, load_embeddings
from .embedder import Embedder, label_vectors

log = logging.getLogger("fotoscan")

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DUP_FOLDER = "_Duplicati"
SIMILI_FOLDER = f"{DUP_FOLDER}/Simili"  # quasi-duplicati: separati dagli esatti, mai cancellati
UNDATED = "Data incerta"
_MODIF = re.compile(r"^(?P<base>.+?)[-_ ](modificato|modificata)$", re.IGNORECASE)
_EPOCH = datetime(1970, 1, 1)


def safe_name(s: str) -> str:
    s = _BAD.sub("_", s).strip().rstrip(".")
    return s or "_"


def safe_folder(folder: str) -> str:
    return "/".join(safe_name(p) for p in folder.replace("\\", "/").split("/") if p)


# ------------------------------------------------------------ contesto
def context_probs(mat: np.ndarray, tv: np.ndarray, nv: np.ndarray, scale: float, temperature: float) -> np.ndarray:
    """Probabilita' [n_file, n_etichette]: softmax fra tutte le etichette + classe nulla (la migliore delle frasi
    generiche). Formula unica per piano, anteprima e prova etichetta: un'etichetta conta una foto solo se batte
    sia le altre etichette sia lo sfondo generico."""
    logits = np.concatenate([mat @ tv.T, (mat @ nv.T).max(axis=1, keepdims=True)], axis=1)
    logits = logits * (scale / max(temperature, 0.1))
    logits -= logits.max(axis=1, keepdims=True)
    p = np.exp(logits)
    p /= p.sum(axis=1, keepdims=True)
    return p[:, :-1]


def score_contexts(conn: sqlite3.Connection, cfg: Config, embedder: Embedder | None = None,
                   emb_data: tuple[np.ndarray, np.ndarray] | None = None):
    """Per ogni file indicizzato: (ids, indice etichetta migliore, probabilita' softmax).
    emb_data: (ids, matrice) gia' letti da load_embeddings, per non rileggerli."""
    ids, mat = emb_data if emb_data is not None else load_embeddings(conn)
    if len(ids) == 0 or not cfg.labels:
        return ids, np.full(len(ids), -1), np.zeros(len(ids), dtype=np.float32)
    # etichette e classi nulle in una sola chiamata: il modello di testo (se serve) si carica una volta
    lab = [l["prompt"] for l in cfg.labels]
    allv = label_vectors(conn, lab + list(cfg.null_prompts), embedder, cfg)
    tv, nv = allv[:len(lab)], allv[len(lab):]
    scale = float(get_meta(conn, "logit_scale", "100") or 100)
    # Softmax con temperatura morbida + classi "nulle" generiche ("una foto"): il contesto vince solo se
    # batte sia le altre etichette sia lo sfondo generico. Cosi' la soglia ha un significato reale
    # (sigmoid assoluta: ~0 su foto vere; softmax puro: ~0.9 anche per immagini fuori contesto).
    p = context_probs(mat, tv, nv, scale, cfg.context_temperature())
    best = p.argmax(axis=1)
    return ids, best, p[np.arange(len(ids)), best].astype(np.float32)


# ------------------------------------------------- quasi-duplicati
def _dir_key(path: str) -> str:
    return os.path.normcase(str(Path(path).parent))


def _name_edges(files) -> list[tuple[int, int]]:
    """Coppie (versione -modificato/-modificata, originale) con stesso nome base nella stessa cartella.
    Solo tra file dello stesso tipo: una foto e il video live (stesso nome) NON sono duplicati."""
    by_key: dict[tuple[str, str, str], list] = defaultdict(list)
    for f in files:
        p = Path(f["path"])
        by_key[(_dir_key(f["path"]), p.stem.lower(), f["kind"])].append(f["id"])
    edges = []
    for f in files:
        m = _MODIF.match(Path(f["path"]).stem)
        if not m:
            continue
        for base_id in by_key.get((_dir_key(f["path"]), m.group("base").lower(), f["kind"]), []):
            if base_id != f["id"]:
                edges.append((f["id"], base_id))
    return edges


def _similar_pairs(ids: np.ndarray, t: np.ndarray, mat: np.ndarray, thr: float, win: float) -> list[tuple[int, int]]:
    """Coppie con coseno >= thr e |t_i - t_j| <= win. Ordinate per tempo: ogni file confronta solo
    la finestra temporale che lo segue (blocchi numpy), non tutte le coppie."""
    order = np.argsort(t, kind="stable")
    ids, t, mat = ids[order], t[order], mat[order]
    end = np.searchsorted(t, t + win, side="right")  # primo indice con tempo > t_i + win
    out: list[tuple[int, int]] = []
    n, block = len(ids), 1024
    for a in range(0, n, block):
        b = min(n, a + block)
        hi = int(end[a:b].max())
        if hi - a < 2:
            continue
        sim = mat[a:b] @ mat[a:hi].T
        rows = np.arange(a, b)[:, None]
        cols = np.arange(a, hi)[None, :]
        ri, ci = np.nonzero((cols > rows) & (cols < end[a:b][:, None]) & (sim >= thr))
        out.extend(zip(ids[a + ri].tolist(), ids[a + ci].tolist()))
    return out


# ------------------------------------------ controllo di contenuto (quasi-duplicati)
# Il solo coseno degli embedding non basta: foto nere/uniformi hanno coseno .99+ fra loro. Sulle sole
# coppie candidate si confrontano miniature 64x64 in scala di grigi (quadrato centrale, come le miniature).
_MINI = 64
_THUMB_NAME = re.compile(r"^(\d+)_(\d+)_.*\.jpg$", re.IGNORECASE)  # cache/thumbs/{id}_{size}_{hash}.jpg
try:  # HEIC: stesso registro usato dallo scanner (idempotente)
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # noqa: BLE001 - pillow-heif assente: HEIC senza miniatura, la coppia non viene filtrata
    pass


class _Mini:
    __slots__ = ("mean", "std", "arr")

    def __init__(self, arr: np.ndarray):
        self.arr = arr
        self.mean = float(arr.mean())
        self.std = float(arr.std())


def _thumb_index(thumbs_dir: Path) -> dict[int, Path]:
    """file_id -> miniatura in cache (la piu' grande disponibile). Una sola lettura della cartella."""
    best: dict[int, tuple[int, Path]] = {}
    try:
        with os.scandir(thumbs_dir) as it:
            for e in it:
                m = _THUMB_NAME.match(e.name)
                if m:
                    fid, size = int(m.group(1)), int(m.group(2))
                    if fid not in best or size > best[fid][0]:
                        best[fid] = (size, Path(e.path))
    except OSError:
        pass
    return {fid: p for fid, (_, p) in best.items()}


def _load_mini(fid: int, path: str, kind: str, thumbs: dict[int, Path], cfg: Config) -> _Mini | None:
    """Miniatura 64x64 in grigio: da cache/thumbs, per i video dal fotogramma in cache/frames, altrimenti
    dall'originale (draft JPEG, HEIC via pillow-heif). None se non leggibile: la coppia non viene filtrata."""
    try:
        if fid in thumbs:
            src: str | Path = thumbs[fid]
        elif kind == "video":
            frames = Path(cfg.frames_dir)
            src = next((p for p in (frames / f"{fid}_1.jpg", frames / f"{fid}_0.jpg") if p.exists()), None)
            if src is None:
                return None
        else:
            src = path
        with Image.open(src) as im:
            if im.format == "JPEG":
                im.draft("RGB", (_MINI, _MINI))
            im = ImageOps.exif_transpose(im).convert("L")
            w, h = im.size
            s = min(w, h)
            im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
            return _Mini(np.asarray(im.resize((_MINI, _MINI), Image.Resampling.LANCZOS), dtype=np.float32))
    except Exception as e:  # noqa: BLE001 - file corrotto, formato non supportato, cache mancante
        log.debug("miniatura non leggibile per id %s: %s", fid, e)
        return None


def _content_rejects(a: _Mini | None, b: _Mini | None, cfg: Config) -> bool:
    """True se la coppia va scartata: una foto e' troppo scura o uniforme, oppure le due miniature
    non si assomigliano (inquadrature diverse con embedding simile). Senza miniatura: non si scarta."""
    if a is None or b is None:
        return False
    for m in (a, b):
        if m.mean < cfg.near_dup_min_luma or m.std < cfg.near_dup_min_std:
            return True
    x, y = a.arr.ravel() - a.mean, b.arr.ravel() - b.mean
    corr = float(x @ y / (np.sqrt((x @ x) * (y @ y)) + 1e-9))
    return corr < cfg.near_dup_min_corr


def _near_dups(conn: sqlite3.Connection, cfg: Config, files, dup_of: dict[int, int],
               stats: dict[str, int] | None = None,
               emb_data: tuple[np.ndarray, np.ndarray] | None = None) -> dict[int, int]:
    """Quasi-duplicati: ritorna {perdente: master} (su 'rappresentanti', cioe' i master degli esatti).
    Un perdente e' tale solo se e' collegato DIRETTAMENTE al master (niente catene transitive).
    stats['simili_scartati'] = file che sarebbero quasi-duplicati senza i controlli (regola nome con
    embedding diverso, controllo di contenuto) e che con i controlli tornano normali."""
    if not (cfg.near_dup_name_rule or cfg.near_dup_threshold > 0):
        return {}
    rep = lambda fid: dup_of.get(fid, fid)  # noqa: E731
    info = {f["id"]: f for f in files}
    raw: set[tuple[int, int]] = set()  # tutte le coppie candidate, prima dei controlli
    edges: set[tuple[int, int]] = set()  # quelle che superano i controlli di nome/embedding

    emb_ids, mat = emb_data if emb_data is not None else load_embeddings(conn)
    pos: dict[int, int] = {}
    norm = np.zeros((0, 0), dtype=np.float32)
    if len(emb_ids):
        pos = {int(i): k for k, i in enumerate(emb_ids)}
        norm = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)

    if cfg.near_dup_name_rule:
        for a, b in _name_edges(files):
            ra, rb = rep(a), rep(b)
            if ra == rb:
                continue
            e = (min(ra, rb), max(ra, rb))
            raw.add(e)
            if a in pos and b in pos and float(norm[pos[a]] @ norm[pos[b]]) < cfg.near_dup_name_min_sim:
                continue  # "-modificato" con un'immagine diversa: niente embedding simile, niente gruppo
            edges.add(e)

    if cfg.near_dup_threshold > 0 and len(emb_ids):
        by_kind: dict[str, list] = defaultdict(list)
        for f in files:
            if f["id"] in dup_of or f["id"] not in pos or f["data_incerta"]:
                continue  # solo master esatti; niente date incerte o da nome file
            if f["date_source"] not in ("exif", "video") or not f["date_taken"]:
                continue
            try:
                dt = datetime.fromisoformat(f["date_taken"])
            except ValueError:
                continue
            by_kind[f["kind"]].append((f["id"], (dt - _EPOCH).total_seconds()))
        for items in by_kind.values():
            if len(items) < 2:
                continue
            fids = np.array([x[0] for x in items], dtype=np.int64)
            ts = np.array([x[1] for x in items], dtype=np.float64)
            rows_idx = np.array([pos[int(x)] for x in fids], dtype=np.int64)
            for a, b in _similar_pairs(fids, ts, norm[rows_idx], cfg.near_dup_threshold,
                                       float(cfg.near_dup_seconds)):
                if a != b:
                    e = (min(a, b), max(a, b))
                    raw.add(e)
                    edges.add(e)

    # controllo di contenuto: solo sulle coppie candidate (le miniature si leggono una volta per file)
    if edges and (cfg.near_dup_min_luma > 0 or cfg.near_dup_min_std > 0 or cfg.near_dup_min_corr > -1):
        thumbs = _thumb_index(Path(cfg.thumbs_dir))
        need = sorted({x for e in edges for x in e})
        with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as ex:
            minis = dict(zip(need, ex.map(
                lambda fid: _load_mini(fid, info[fid]["path"], info[fid]["kind"], thumbs, cfg), need)))
        edges = {e for e in edges if not _content_rejects(minis[e[0]], minis[e[1]], cfg)}

    def rank(fid: int):  # master: risoluzione piu' alta, poi file piu' grande, poi nome piu' corto
        f = info[fid]
        return (-((f["width"] or 0) * (f["height"] or 0)), -(f["size"] or 0), len(f["name"]), f["path"])

    def resolve(es: set[tuple[int, int]]) -> dict[int, int]:
        parent: dict[int, int] = {}

        def find(x: int) -> int:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        adj: dict[int, set[int]] = defaultdict(set)
        for a, b in es:
            adj[a].add(b)
            adj[b].add(a)
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
        comps: dict[int, list[int]] = defaultdict(list)
        for x in list(parent):
            comps[find(x)].append(x)
        losers: dict[int, int] = {}
        for members in comps.values():
            members.sort(key=rank)
            master = members[0]
            for m in members[1:]:
                if master in adj[m]:
                    losers[m] = master
        return losers

    losers = resolve(edges)
    if stats is not None:
        stats["simili_scartati"] = len(resolve(raw).keys() - losers.keys()) if raw != edges else 0
    return losers


# ---------------------------------------------------------------- eventi
def _group_events(items: list[tuple[datetime, int, float]], cfg: Config) -> list[list[tuple[datetime, int, float]]]:
    """items ordinati per data. Prima catene: nuovo evento a ogni pausa > event_gap_hours.
    Poi ogni evento piu' lungo di max_event_days (dalla prima foto all'ultima) si taglia nella
    pausa piu' lunga al suo interno, ricorsivamente, finche' ogni pezzo non e' abbastanza corto."""
    gap = timedelta(hours=cfg.event_gap_hours)
    chains: list[list[tuple[datetime, int, float]]] = []
    for item in items:
        if chains and item[0] - chains[-1][-1][0] <= gap:
            chains[-1].append(item)
        else:
            chains.append([item])

    max_span = timedelta(days=cfg.max_event_days)
    out: list[list[tuple[datetime, int, float]]] = []
    stack = chains[::-1]  # pila esplicita: niente ricorsione profonda su archivi con pause crescenti
    while stack:
        ev = stack.pop()
        if len(ev) < 2 or ev[-1][0] - ev[0][0] <= max_span:
            out.append(ev)
            continue
        gaps = [ev[k][0] - ev[k - 1][0] for k in range(1, len(ev))]
        # taglio alla pausa piu' lunga; a parita' di pausa, quella piu' vicina al centro (niente "sbuccia" di un giorno alla volta)
        mid = len(ev) / 2
        k = max(range(1, len(ev)), key=lambda j: (gaps[j - 1], -abs(j - mid)))
        stack.append(ev[k:])
        stack.append(ev[:k])
    return out


def _count_days(ev) -> dict:
    out: dict = defaultdict(int)
    for dt, _, _ in ev:
        out[dt.date()] += 1
    return out


def _event_name(ev: list[tuple[datetime, int, float]]) -> str:
    """'Evento AAAA-MM-GG' se tutte le foto cadono in un giorno di calendario, altrimenti con i giorni coperti."""
    first, last = ev[0][0].date(), ev[-1][0].date()
    days = (last - first).days + 1
    if days == 1:
        return f"Evento {first:%Y-%m-%d}"
    return f"Evento {first:%Y-%m-%d} ({days} giorni)"


MESI = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", "Luglio", "Agosto",
        "Settembre", "Ottobre", "Novembre", "Dicembre"]
_CITIES_FILE = Path(__file__).resolve().parent.parent / "frontend" / "data" / "cities.json"
_cities: list[tuple[str, float, float]] | None = None


def _load_cities() -> list[tuple[str, float, float]]:
    """Elenco offline di citta' (lo stesso della pagina Luoghi). Vuoto se il file manca."""
    global _cities
    if _cities is None:
        try:
            import json

            _cities = [(n, float(a), float(b)) for n, a, b in json.loads(_CITIES_FILE.read_text(encoding="utf-8"))]
        except Exception:  # nomi senza luogo: nessun problema
            _cities = []
    return _cities


_city_grid: dict[tuple[int, int], list[int]] | None = None


def _cities_near(lat: float, lon: float, max_km: float) -> list[tuple[str, float, float]]:
    """Citta' nelle celle di 1 grado attorno al punto (abbastanza per max_km), nell'ordine dell'elenco:
    stesso risultato della scansione completa, ma una manciata di confronti invece di migliaia per foto."""
    import math

    global _city_grid
    cities = _load_cities()
    if _city_grid is None:
        grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, (_n, a, b) in enumerate(cities):
            grid[(math.floor(a), math.floor(b))].append(i)
        _city_grid = dict(grid)
    dlat = int(math.ceil(max_km / 110.5)) + 1
    coslat = min(math.cos(math.radians(min(89.0, abs(lat) + dlat))), 1.0)
    if coslat < 0.02:  # vicino ai poli: meglio la scansione completa
        return cities
    dlon = int(math.ceil(max_km / (111.3 * coslat))) + 1
    if dlon >= 180:
        return cities
    fy, fx = math.floor(lat), math.floor(lon)
    idx: list[int] = []
    for y in range(fy - dlat, fy + dlat + 1):
        for x in range(fx - dlon, fx + dlon + 1):
            xx = ((x + 180) % 360) - 180  # attraversa l'antimeridiano
            idx.extend(_city_grid.get((y, xx), ()))
    idx.sort()
    return [cities[i] for i in idx]


def _nearest_city(lat: float, lon: float, max_km: float = 30.0) -> str | None:
    import math

    best, best_d = None, max_km
    for name, a, b in _cities_near(lat, lon, max_km):
        dlat, dlon = math.radians(a - lat), math.radians(b - lon)
        h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat)) * math.cos(math.radians(a)) * math.sin(dlon / 2) ** 2
        d = 12742 * math.asin(math.sqrt(h))
        if d <= best_d:
            best, best_d = name, d
    return best


def _day_span(first, last) -> str:
    """'12 ottobre', '12-14 ottobre', '30 settembre - 2 ottobre' (anno e mese sono gia' nel percorso)."""
    m1, m2 = MESI[first.month - 1].lower(), MESI[last.month - 1].lower()
    if first == last:
        return f"{first.day} {m1}"
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.day}-{last.day} {m1}"
    return f"{first.day} {m1} - {last.day} {m2}"


def _describe_event(ev, ctx_of: dict, pos_of: dict, home: str | None) -> str:
    """Nome parlante: data + luogo (citta' piu' vicina alle foto con GPS, se non e' quella di casa) + tema
    (contesto prevalente, se riguarda almeno un quarto delle foto)."""
    parts = [_day_span(ev[0][0].date(), ev[-1][0].date())]
    cities: dict = defaultdict(int)
    for _, fid, _ in ev:
        p = pos_of.get(fid)
        if p:
            c = _nearest_city(*p)
            if c:
                cities[c] += 1
    if cities:
        city = max(cities, key=cities.get)
        if city != home:
            parts.append(city)
    themes: dict = defaultdict(int)
    for _, fid, _ in ev:
        ctx, sc = ctx_of.get(fid, (None, 0.0))
        if ctx and sc >= 0.15:
            themes[ctx] += 1
    if themes:
        theme = max(themes, key=themes.get)
        if themes[theme] * 4 >= len(ev):
            parts.append(theme)
    return " - ".join(parts)


# ---------------------------------------------------------------- piano
def build_plan(conn: sqlite3.Connection, cfg: Config, embedder: Embedder | None = None,
               progress: Callable[[str, int, int], None] | None = None) -> dict[str, int]:
    """Calcola il piano e lo salva nella tabella plan (nessun file toccato)."""
    emb_data = load_embeddings(conn)  # una lettura sola per contesti, temi visivi e quasi-duplicati
    ids, best, score = score_contexts(conn, cfg, embedder, emb_data=emb_data)
    ctx_of = {int(i): (cfg.labels[b]["name"] if b >= 0 else None, float(s)) for i, b, s in zip(ids, best, score)}
    # etichette da esempi (temi suggeriti e accettati): solo per le foto che nessuna etichetta di testo copre
    from .suggest import assign_visual

    if len(ids):
        mat = emb_data[1]
        images = {r[0] for r in conn.execute("SELECT id FROM files WHERE kind='image'")}
        not_image = np.array([int(i) not in images for i in ids], dtype=bool)
        taken = ((best >= 0) & (score >= cfg.score_threshold)) | not_image  # solo foto: i video restano agli eventi
        ctx_of.update(assign_visual(conn, ids, mat, taken))

    files = conn.execute(
        "SELECT id,path,name,hash,date_taken,date_source,data_incerta,kind,size,width,height,lat,lon "
        "FROM files WHERE status='embedded' ORDER BY id"
    ).fetchall()

    # --- duplicati esatti (stesso hash): il "master" e' il file con data piu' affidabile / percorso piu' corto
    by_hash: dict[str, list] = defaultdict(list)
    for f in files:
        if f["hash"]:
            by_hash[f["hash"]].append(f)
    dup_of: dict[int, int] = {}
    for grp in by_hash.values():
        if len(grp) > 1:
            grp.sort(key=lambda r: (r["data_incerta"], len(r["path"]), r["path"]))
            for d in grp[1:]:
                dup_of[d["id"]] = grp[0]["id"]

    # --- quasi-duplicati (versioni modificate, scatti quasi identici): {perdente: master}
    stats: dict[str, int] = {}
    near_of = _near_dups(conn, cfg, files, dup_of, stats, emb_data=emb_data)

    rows: dict[int, dict] = {}
    pending_events: list[tuple[datetime, int, float]] = []
    ctx_members: dict[str, list[tuple[datetime, int, float]]] = defaultdict(list)

    for f in files:
        fid = f["id"]
        ctx, sc = ctx_of.get(fid, (None, 0.0))
        good = ctx is not None and sc >= cfg.score_threshold
        # plan.context = etichetta ASSEGNATA, non la migliore candidata: chi legge il piano (Libera spazio, album
        # suggeriti, statistiche, visualizzatore) la usa come "questo file e' uno screenshot". Sotto soglia resta
        # solo il punteggio. Prima veniva salvata sempre: 39 mila file su 48 mila avevano un contesto mai assegnato.
        if not good:
            ctx = None
        if fid in dup_of:  # copia esatta: punta al master, anche se il master e' a sua volta un quasi-duplicato
            rows[fid] = dict(folder=None if cfg.dup_mode == "skip" else DUP_FOLDER, context=ctx, score=sc,
                             is_dup=1, dup_of=near_of.get(dup_of[fid], dup_of[fid]))
            continue
        if fid in near_of:
            rows[fid] = dict(folder=None if cfg.dup_mode == "skip" else SIMILI_FOLDER, context=ctx, score=sc,
                             is_dup=2, dup_of=near_of[fid])
            continue
        try:
            dt = datetime.fromisoformat(f["date_taken"]) if f["date_taken"] else None
        except ValueError:
            dt = None
        if dt is None or f["data_incerta"]:
            year = dt.year if dt else "sconosciuto"
            rows[fid] = dict(folder=f"{UNDATED}/{year} - {ctx if good else 'Varie'}", context=ctx, score=sc,
                             is_dup=0, dup_of=None)
        elif good:
            rows[fid] = dict(folder=cfg.folder_template.format(anno=dt.year, mese=dt.month, contesto=ctx),
                             context=ctx, score=sc, is_dup=0, dup_of=None)
            ctx_members[rows[fid]["folder"]].append((dt, fid, sc))
        else:
            pending_events.append((dt, fid, sc))
            rows[fid] = dict(folder=None, context=ctx, score=sc, is_dup=0, dup_of=None)

    # --- cartelle di contesto troppo piccole (es. 1 foto di "Animali" in un mese): niente cartella propria,
    # le foto tornano nel flusso degli eventi e finiscono nell'evento del giorno o in "MM - Varie"
    # Temi ricorrenti con poche foto al mese (es. la palestra): si raccolgono in una cartella dell'anno
    # ("2024/Palestra e sport") invece di finire negli eventi; solo se anche nell'anno sono poche tornano agli eventi.
    min_ctx = max(1, int(getattr(cfg, "min_context_size", 1)))
    small: dict[tuple[int, str], list] = defaultdict(list)
    yearly = getattr(cfg, "context_period", "anno") == "anno"
    for folder, members in list(ctx_members.items()):
        if yearly or len(members) < min_ctx:
            dt0 = members[0][0]
            small[(dt0.year, rows[members[0][1]]["context"])].extend(members)
            del ctx_members[folder]
    for (year, ctx), members in small.items():
        if len(members) >= min_ctx:
            folder = f"{year}/{ctx}"
            for item in members:
                rows[item[1]]["folder"] = folder
            ctx_members[folder] = members
        else:
            for item in members:
                rows[item[1]]["folder"] = None
                pending_events.append(item)

    # --- eventi temporali per i file senza contesto affidabile
    pending_events.sort()
    events = _group_events(pending_events, cfg)

    # "Giorno tipico" di ogni anno: mediana delle foto per giorno con almeno una foto (date certe, senza copie).
    per_day: dict = defaultdict(int)
    for f in files:
        r = rows.get(f["id"])
        if r and r["is_dup"] == 0 and f["date_taken"] and not f["data_incerta"]:
            per_day[f["date_taken"][:10]] += 1
    by_year: dict[str, list[int]] = defaultdict(list)
    for day, n in per_day.items():
        by_year[day[:4]].append(n)
    typical = {y: sorted(v)[len(v) // 2] for y, v in by_year.items()}
    ratio = float(getattr(cfg, "event_burst_ratio", 0) or 0)
    descr = getattr(cfg, "event_names", "descrittivi") != "data"

    def is_event(ev) -> bool:
        if len(ev) < cfg.min_event_size:
            return False
        if ratio <= 0:
            return True
        peak = max(_count_days(ev).values())
        return peak >= ratio * typical.get(f"{ev[0][0].year:04d}", 1)

    pos_of = {f["id"]: (f["lat"], f["lon"]) for f in files
              if f["lat"] is not None and f["lon"] is not None and not (f["lat"] == 0 and f["lon"] == 0)}
    home = None
    if descr and pos_of:
        votes: dict[str, int] = defaultdict(int)
        for la, lo in pos_of.values():
            c = _nearest_city(la, lo)
            if c:
                votes[c] += 1
        home = max(votes, key=votes.get) if votes else None  # citta' di casa: non serve ripeterla nei nomi

    kind_of: dict[str, str] = {}
    used: set[str] = set()
    n_events = 0
    for ev in events:
        start = ev[0][0]  # il mese e' quello della prima foto, anche se l'evento attraversa due mesi
        if is_event(ev):
            n_events += 1
            name = _describe_event(ev, ctx_of, pos_of, home) if descr else _event_name(ev)
            folder = cfg.folder_template.format(anno=start.year, mese=start.month, contesto=name)
            base, k = folder, 2
            while folder in used:  # due eventi con lo stesso nome nello stesso mese
                folder, k = f"{base} ({k})", k + 1
            used.add(folder)
            kind_of[folder] = "event"
        else:
            # foto del mese senza un giorno speciale: un'unica cartella per mese ("2019/10 - Ottobre")
            month_name = MESI[start.month - 1] if descr else "Varie"
            folder = cfg.folder_template.format(anno=start.year, mese=start.month, contesto=month_name)
            kind_of[folder] = "varie"
        for _, fid, _ in ev:
            rows[fid]["folder"] = folder
    for folder in ctx_members:
        kind_of.setdefault(folder, "context")

    # --- override utente (rinomina gruppi / spostamenti singoli)
    rename = {r["old"]: r["new"] for r in conn.execute("SELECT old,new FROM rename_map")}
    override = {r["file_id"]: r["folder"] for r in conn.execute("SELECT file_id,folder FROM file_override")}
    for fid, r in rows.items():
        r["gkind"] = kind_of.get(r["folder"]) if r["folder"] else None
        if fid in override:
            r["folder"] = override[fid]
            r["gkind"] = None  # spostato a mano: il tipo si ricava dagli altri file della cartella
        elif r["folder"] in rename:
            r["folder"] = rename[r["folder"]]
        if r["folder"]:
            r["folder"] = safe_folder(r["folder"])

    conn.execute("DELETE FROM plan")
    conn.executemany(
        "INSERT INTO plan(file_id,folder,context,score,is_dup,dup_of,gkind) VALUES(?,?,?,?,?,?,?)",
        [(fid, r["folder"], r["context"], r["score"], r["is_dup"], r["dup_of"], r.get("gkind"))
         for fid, r in rows.items()],
    )
    conn.commit()
    # Ruoli master/duplicato scelti dall'utente (tabella dup_keep): il piano grezzo non li deve annullare.
    from .server.routes_duplicates import reapply  # import locale: il server dipende da questo modulo

    reapply(conn)
    return {"file": len(rows), "cartelle": len({r["folder"] for r in rows.values() if r["folder"]}),
            "duplicati": len(dup_of), "simili": len(near_of), "eventi": n_events,
            "simili_scartati": stats.get("simili_scartati", 0)}


def plan_table(conn: sqlite3.Connection):
    """Piano come lista di righe (per UI/CLI)."""
    return conn.execute(
        "SELECT p.file_id, p.folder, p.context, p.score, p.is_dup, p.dup_of, f.name, f.path, f.kind, f.date_taken, f.data_incerta "
        "FROM plan p JOIN files f ON f.id=p.file_id ORDER BY p.folder, f.date_taken"
    ).fetchall()


def folder_summary(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT folder, COUNT(*) n, SUM(f.kind='video') videos FROM plan p JOIN files f ON f.id=p.file_id "
        "WHERE folder IS NOT NULL GROUP BY folder ORDER BY folder"
    ).fetchall()


def rename_group(conn: sqlite3.Connection, old: str, new: str) -> None:
    """Rinomina (o unisce) un gruppo: vale anche per i futuri ricalcoli del piano."""
    new = safe_folder(new)
    conn.execute("INSERT OR REPLACE INTO rename_map(old,new) VALUES(?,?)", (old, new))
    conn.execute("UPDATE rename_map SET new=? WHERE new=?", (new, old))  # catene: A->B poi B->C
    conn.execute("UPDATE plan SET folder=? WHERE folder=?", (new, old))
    # Spostamenti singoli e scambi del master (dup_keep.expect_folder) puntano alla cartella: vanno seguiti
    # la rinomina, altrimenti al ricalcolo i file tornerebbero nella cartella vecchia (gruppo "orfano").
    conn.execute("UPDATE file_override SET folder=? WHERE folder=?", (new, old))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dup_keep'").fetchone():
        conn.execute("UPDATE dup_keep SET expect_folder=? WHERE expect_folder=?", (new, old))
    conn.commit()


def move_files(conn: sqlite3.Connection, file_ids: list[int], folder: str) -> None:
    folder = safe_folder(folder)
    for fid in file_ids:
        conn.execute("INSERT OR REPLACE INTO file_override(file_id,folder) VALUES(?,?)", (fid, folder))
        conn.execute("UPDATE plan SET folder=? WHERE file_id=?", (folder, fid))
    conn.commit()


# -------------------------------------------------------------- applica
def _check_dirs(cfg: Config, need_output: bool) -> tuple[Path | None, Path | None]:
    if not cfg.source_dir:
        raise ValueError("cartella sorgente sconosciuta: rilancia 'index --source ...'")
    if need_output and not cfg.output_dir:
        raise ValueError("specifica la destinazione con --output")
    src = Path(cfg.source_dir).resolve()
    if not cfg.output_dir:  # simulazione senza destinazione
        return src, None
    out = Path(cfg.output_dir).resolve()
    # confronto per identita' di cartella (samefile): i nomi 8.3 di Windows (ANDREA~1) non sono normalizzati da resolve()
    if _inside(out, src) or _inside(src, out):
        raise ValueError("output_dir non puo' essere dentro (o contenere) la cartella sorgente")
    return src, out


def _inside(child: Path, parent: Path) -> bool:
    """True se child coincide con parent o ne e' sottocartella (verificato sugli antenati esistenti)."""
    for p in [child, *child.parents]:
        try:
            if p.exists() and os.path.samefile(p, parent):
                return True
        except OSError:
            continue
    return False


def _copy_atomic(src: str, dest: Path) -> None:
    """Copia in un file temporaneo e poi rinomina: un'interruzione non lascia mai una copia parziale col nome finale."""
    tmp = dest.with_name(dest.name + ".fotoscan-tmp")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except BaseException:  # errore o annullamento: niente residui parziali
        tmp.unlink(missing_ok=True)
        raise


def apply_plan(conn: sqlite3.Connection, cfg: Config, apply: bool = False,
               progress: Callable[[str, int, int], None] | None = None,
               result: dict[str, int] | None = None) -> dict[str, int]:
    """apply=False: solo simulazione. apply=True: crea l'albero in output_dir (link, fallback copia).
    progress(stage, fatti, totale) viene chiamato PRIMA di ogni file: puo' sollevare eccezioni (annullamento).
    result: dizionario da riempire in tempo reale (contatori live); se None ne viene creato uno nuovo."""
    src, out = _check_dirs(cfg, need_output=apply)
    rows = conn.execute(
        "SELECT p.file_id, p.folder, f.path, f.name, f.size, f.hash FROM plan p JOIN files f ON f.id=p.file_id "
        "WHERE p.folder IS NOT NULL ORDER BY p.folder, f.name"
    ).fetchall()
    res = result if result is not None else {}
    res.update({"totale": len(rows), "link": 0, "copie": 0, "gia_presenti": 0, "errori": 0})
    used: set[str] = set()
    log_file = None
    if apply:
        out.mkdir(parents=True, exist_ok=True)
        log_file = open(out / "_fotoscan_apply.log", "a", encoding="utf-8")
        log_file.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} apply, {len(rows)} file ===\n")
    try:
        for i, r in enumerate(rows):
            if apply and progress:
                progress("applicazione", i, len(rows))
            if out is None:  # simulazione senza destinazione: solo conteggio
                res["link" if cfg.link_mode == "hardlink" else "copie"] += 1
                continue
            dest_dir = out / safe_folder(r["folder"])
            dest = dest_dir / safe_name(r["name"])
            key = str(dest).lower()
            # anteprima: niente hash (su 60 GB sarebbe lentissimo); esecuzione: confronto di contenuto
            # apply_verify="full" (predefinito): hash del file gia' presente. "fast" (avanzata): solo dimensione,
            # niente rilettura dell'archivio, ma un file danneggiato di stessa dimensione non viene rilevato.
            deep = apply and getattr(cfg, "apply_verify", "full") != "fast"
            already = dest.exists() and _is_same_content(Path(r["path"]), dest, r["size"], r["hash"], deep=deep)
            if key in used or (dest.exists() and not already):
                dest = dest_dir / safe_name(f"{Path(r['name']).stem}_{r['file_id']}{Path(r['name']).suffix}")
                key = str(dest).lower()
            used.add(key)
            if dest.exists():
                res["gia_presenti"] += 1
                continue
            if not apply:
                res["link" if cfg.link_mode == "hardlink" else "copie"] += 1
                continue
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                method = "copia"
                if cfg.link_mode == "hardlink":
                    try:
                        os.link(r["path"], dest)
                        method = "hardlink"
                    except OSError:
                        _copy_atomic(r["path"], dest)
                else:
                    _copy_atomic(r["path"], dest)
                res["link" if method == "hardlink" else "copie"] += 1
                log_file.write(f"{method}\t{r['path']}\t{dest}\n")
            except Exception as e:
                res["errori"] += 1
                log_file.write(f"ERRORE\t{r['path']}\t{dest}\t{e}\n")
            log_file.flush()
        if apply and progress:
            progress("applicazione", len(rows), len(rows))
    finally:
        if log_file:
            log_file.write(f"=== fine: {res} ===\n")
            log_file.close()
    return res


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _is_same_content(src: Path, dest: Path, size: int | None, hash_: str | None, deep: bool = True) -> bool:
    """True se dest e' gia' il file sorgente (hard link) o ne ha lo stesso contenuto.
    deep=True (esecuzione): confronto dell'hash, la sola dimensione non basta.
    deep=False (anteprima): basta la dimensione, per non rileggere tutto l'archivio."""
    if _same_file(src, dest):
        return True
    try:
        if dest.stat().st_size != size or not hash_:
            return False
        if not deep:
            return True
        from .scanner_metadata import file_hash

        return file_hash(str(dest)) == hash_
    except OSError:
        return False
