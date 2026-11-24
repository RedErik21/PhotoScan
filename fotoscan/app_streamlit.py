"""Interfaccia Streamlit locale: indicizzazione, dry-run, ricerca semantica, galleria, applicazione.

Prestazioni: le query pesanti sono in st.cache_data / st.cache_resource e invalidate da un'impronta
(conteggi e massimi delle tabelle) invece che a ogni rerun. Le miniature mancanti vengono generate in
un ThreadPool in background: la pagina si mostra subito con segnaposto e si aggiorna da sola.
"""
from __future__ import annotations

import io
import json
import math
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import streamlit as st
from PIL import Image, ImageOps

from fotoscan import db as dbm
from fotoscan import organizer
from fotoscan.config import ROOT, load_config

try:  # HEIC: il decoder va registrato anche se scanner_metadata non e' ancora stato importato
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass

st.set_page_config(page_title="PhotoScan", page_icon="📷", layout="wide")

CSS = """
<style>
.block-container {padding-top: 1.4rem; padding-bottom: 3rem; max-width: 1480px;}
[data-testid="stHorizontalBlock"] {gap: 0.5rem;}
.fs-hero {display:flex; align-items:center; gap:.8rem; margin: .2rem 0 1.1rem 0;}
.fs-logo {font-size:2.1rem; line-height:1;}
.fs-title {font-size:1.7rem; font-weight:700; letter-spacing:-.01em; line-height:1.1;}
.fs-sub {opacity:.65; font-size:.92rem;}
.fs-section {margin: .4rem 0 .9rem 0;}
.fs-section h3 {margin:0; font-size:1.3rem; font-weight:650;}
.fs-section p {margin:.15rem 0 0 0; opacity:.65; font-size:.9rem;}
.fs-date {display:flex; align-items:baseline; gap:.6rem; margin: 1.4rem 0 .5rem 0;
          padding-bottom:.35rem; border-bottom:1px solid rgba(128,128,128,.25);}
.fs-date b {font-size:1.02rem; font-weight:650;}
.fs-date span {opacity:.6; font-size:.85rem;}
.fs-muted {opacity:.65; font-size:.88rem;}
.fs-pill {display:inline-block; padding:.18rem .65rem; border-radius:999px; font-size:.82rem;
          background:rgba(26,115,232,.14); color:#1a73e8; font-weight:550;}
.fs-pill.off {background:rgba(128,128,128,.16); color:inherit;}
.fs-pill.ok {background:rgba(30,142,62,.16); color:#1e8e3e;}
.fs-pill.warn {background:rgba(242,139,0,.16); color:#b06000;}
.fs-empty {aspect-ratio:1/1; border-radius:10px; background:rgba(128,128,128,.16);
           display:flex; align-items:center; justify-content:center; font-size:1.6rem; opacity:.75;}
[class*="st-key-t_"] [data-testid="stImage"] {position:relative;}
[class*="st-key-t_"] [data-testid="stImage"] img {aspect-ratio:1/1; object-fit:cover; border-radius:10px;}
[class*="st-key-t_"] [data-testid="stImage"]::after {
    content: none; position:absolute; left:6px; bottom:6px; padding:2px 8px; border-radius:6px;
    background:rgba(0,0,0,.62); color:#fff; font-size:.74rem; font-weight:600; pointer-events:none;}
[class*="st-key-o_"] button {min-height:0; padding:.12rem .5rem; font-size:.8rem; border-radius:8px;}
.st-key-searchbar input {font-size:1.15rem !important; padding:.85rem 1.1rem !important;}
.st-key-searchbar [data-baseweb="input"] {border-radius:999px !important;}
.fs-tree {font-size:.93rem; line-height:1.55;}
.fs-meta {display:grid; grid-template-columns: repeat(auto-fill, minmax(180px,1fr)); gap:.6rem; margin:.6rem 0 1rem 0;}
.fs-meta-item {border:1px solid rgba(128,128,128,.25); border-radius:12px; padding:.55rem .75rem;}
.fs-meta-item span {display:block; font-size:.74rem; text-transform:uppercase; letter-spacing:.04em; opacity:.6;}
.fs-meta-item b {font-weight:600; font-size:.92rem; word-break:break-word;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)
cfg = load_config()

WEEKDAYS = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MONTHS = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
          "agosto", "settembre", "ottobre", "novembre", "dicembre"]
SRC_LABEL = {"exif": "Metadati EXIF", "filename": "Nome del file", "video": "Metadati video",
             "mtime": "Data di modifica (incerta)"}
ITEM_COLS = "id,path,name,kind,date_taken,date_source,duration,lat,lon,width,height,size"
PAGE_SIZE = 96
CATEGORIES = ["Contesti", "Eventi", "Varie", "Data incerta", "_Duplicati", "_Duplicati/Simili"]
THUMB_WORKERS = max(2, min(8, os.cpu_count() or 4))
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@st.cache_resource
def get_conn(path: str):
    return dbm.connect(path)


conn = get_conn(cfg.db_path)
DB_KEY = str(cfg.db_path)
# Sorgente: dal config.json, altrimenti quella salvata nel catalogo (il dry-run la conosce gia').
DEFAULT_SRC = cfg.source_dir or dbm.get_meta(conn, "source_dir", "") or ""


@st.cache_resource(show_spinner="Carico il modello di ricerca (CPU)… la prima volta richiede qualche secondo")
def text_model():
    from fotoscan.embedder import Embedder

    return Embedder(cfg, device="cpu")


# ------------------------------------------------------------ impronte (invalidano le cache)
def files_version() -> tuple:
    return tuple(conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(id),0), COALESCE(SUM(status='embedded'),0) FROM files").fetchone())


def plan_version() -> tuple:
    return tuple(conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(file_id),0), COALESCE(SUM(LENGTH(COALESCE(folder,''))),0), "
        "COUNT(DISTINCT folder), COALESCE(SUM(is_dup),0), "
        "(SELECT COUNT(*) FROM rename_map), (SELECT COALESCE(SUM(LENGTH(old)+LENGTH(new)),0) FROM rename_map), "
        "(SELECT COUNT(*) FROM file_override) FROM plan").fetchone())


def emb_version() -> tuple:
    return tuple(conn.execute("SELECT COUNT(*), COALESCE(MAX(file_id),0) FROM embeddings").fetchone())


# ------------------------------------------------------------ utilita'
def parse_dt(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((s or "")[:19])
    except ValueError:
        return None


def day_label(d: datetime) -> str:
    return f"{WEEKDAYS[d.weekday()].capitalize()} {d.day} {MONTHS[d.month - 1]} {d.year}"


def month_label(d: datetime) -> str:
    return f"{MONTHS[d.month - 1].capitalize()} {d.year}"


def fmt_dur(sec: float | None) -> str:
    s = int(sec or 0)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def human_size(n: float | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "—"


def md(s: str) -> str:
    """Escape minimo per usare nomi di file dentro markdown."""
    for ch in "\\`*_[]<>|#":
        s = s.replace(ch, "\\" + ch)
    return s


def section(title: str, sub: str = "") -> None:
    st.markdown(f'<div class="fs-section"><h3>{escape(title)}</h3>'
                f'{"<p>" + escape(sub) + "</p>" if sub else ""}</div>', unsafe_allow_html=True)


def guarded(fn, *args) -> None:
    """Nessuna eccezione Python a schermo: l'errore diventa un messaggio in italiano."""
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001
        st.error(f"Questa parte non è disponibile: {e}")


# ------------------------------------------------------------ miniature (ThreadPool, non bloccanti)
@st.cache_resource
def thumb_pool() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=THUMB_WORKERS, thread_name_prefix="miniature")


@st.cache_resource
def thumb_jobs() -> dict[int, Future]:
    return {}


@st.cache_resource
def ffmpeg_bin() -> str | None:
    return cfg.find_ffmpeg()[0]


def thumb_file(fid: int) -> Path:
    return cfg.thumbs_dir / f"{fid}_sq.jpg"


def _render_thumb(fid: int, path: str, kind: str, duration: float | None) -> str | None:
    """Miniatura quadrata 320px (ritaglio centrale = object-fit: cover). Thread-safe, mai eccezioni."""
    out = thumb_file(fid)
    if out.exists():
        return str(out)
    tmp_frame: Path | None = None
    tmp_out: Path | None = None
    try:
        cfg.thumbs_dir.mkdir(parents=True, exist_ok=True)
        src = path
        if kind == "video":
            fr = cfg.frames_dir / f"{fid}_1.jpg"
            if fr.exists():
                src = str(fr)
            else:  # fotogramma al volo con ffmpeg (a circa 15% della durata)
                ff = ffmpeg_bin()
                if not ff:
                    return None
                tmp_frame = cfg.thumbs_dir / f"{fid}_{threading.get_ident()}_frame.jpg"
                seek = max(0.0, float(duration or 4.0) * 0.15)
                subprocess.run([ff, "-v", "error", "-ss", f"{seek:.2f}", "-i", path, "-frames:v", "1",
                                "-vf", "scale=640:-2", "-y", str(tmp_frame)],
                               check=True, timeout=60, capture_output=True, creationflags=_NO_WINDOW)
                src = str(tmp_frame)
        with Image.open(src) as im:
            if im.format == "JPEG":
                im.draft("RGB", (640, 640))
            im = ImageOps.fit(ImageOps.exif_transpose(im).convert("RGB"), (320, 320),
                              Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            tmp_out = out.with_name(f"{out.stem}.{threading.get_ident()}.tmp.jpg")
            im.save(tmp_out, quality=82)
        os.replace(tmp_out, out)  # scrittura atomica: nessuna miniatura a meta'
        return str(out)
    except Exception:  # file illeggibile, HEIC corrotto, ffmpeg assente: segnaposto
        for p in (tmp_out,):
            if p is not None and p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        return None
    finally:
        if tmp_frame is not None and tmp_frame.exists():
            try:
                tmp_frame.unlink()
            except OSError:
                pass


def request_thumbs(items: list[dict]) -> None:
    """Accoda in background le miniature mancanti (una sola volta per file)."""
    pool, jobs = thumb_pool(), thumb_jobs()
    for it in items:
        fid = it["id"]
        if thumb_file(fid).exists():
            continue
        job = jobs.get(fid)
        if job is not None and (not job.done() or job.result() is None):
            continue  # in corso, oppure gia' fallita: niente retry a ogni rerun
        jobs[fid] = pool.submit(_render_thumb, fid, it["path"], it["kind"], it.get("duration"))


def thumb_ready(it: dict) -> str | None:
    out = thumb_file(it["id"])
    return str(out) if out.exists() else None


def thumb_pending(it: dict) -> bool:
    job = thumb_jobs().get(it["id"])
    return job is not None and not job.done() and not thumb_file(it["id"]).exists()


def live(items: list[dict], fn, *args, **kwargs) -> None:
    """Disegna fn(...) subito; finche' ci sono miniature in lavorazione la ridisegna ogni secondo
    dentro un fragment (la pagina resta reattiva), poi fa un rerun completo e smette."""
    request_thumbs(items)
    if not any(thumb_pending(it) for it in items):
        fn(*args, **kwargs)
        return

    @st.fragment(run_every=1.0)
    def tick() -> None:
        if any(thumb_pending(it) for it in items):
            fn(*args, **kwargs)
        else:
            st.rerun(scope="app")

    tick()


@st.cache_data(show_spinner=False, max_entries=48)
def preview_bytes(path: str) -> bytes | None:
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((1800, 1800))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
            return buf.getvalue()
    except Exception:
        return None


def tile_grid(items: list[dict], prefix: str, all_items: list[dict] | None = None,
              offset: int = 0, cols: int = 6, captions: list[str] | None = None) -> None:
    """Griglia di miniature quadrate con pulsante «Apri» (visualizzatore che scorre su all_items)."""
    ctx = all_items if all_items is not None else items
    badges: list[str] = []
    for start in range(0, len(items), cols):
        cs = st.columns(cols, gap="small")
        for j, (c, it) in enumerate(zip(cs, items[start:start + cols])):
            k = f"{prefix}{start + j}"
            idx = offset + start + j
            with c:
                with st.container(key=f"t_{k}"):
                    tp = thumb_ready(it)
                    if tp:
                        st.image(tp, width="stretch")
                    else:
                        icon = "⏳" if thumb_pending(it) else ("🎬" if it["kind"] == "video" else "🖼")
                        st.markdown(f'<div class="fs-empty">{icon}</div>', unsafe_allow_html=True)
                    if it["kind"] == "video":
                        badges.append(f'.st-key-t_{k} [data-testid="stImage"]::after'
                                      f'{{content:"🎬 {fmt_dur(it.get("duration"))}"; display:block;}}')
                if captions and captions[start + j]:
                    st.caption(captions[start + j])
                with st.container(key=f"o_{k}"):
                    if st.button("Apri", key=f"btn_{k}", width="stretch"):
                        open_viewer(ctx, idx)
    if badges:
        st.markdown("<style>" + "".join(badges) + "</style>", unsafe_allow_html=True)


# ------------------------------------------------------------ visualizzatore
def _shift(delta: int, n: int) -> None:
    st.session_state["v_idx"] = max(0, min(n - 1, st.session_state.get("v_idx", 0) + delta))


def meta_cards(it: dict) -> str:
    d = parse_dt(it.get("date_taken"))
    rows = [("Data di scatto", escape(f"{day_label(d)} · {d:%H:%M}") if d else "—"),
            ("Origine data", escape(SRC_LABEL.get(it.get("date_source") or "", it.get("date_source") or "—")))]
    if it.get("duration"):
        rows.append(("Durata", escape(fmt_dur(it["duration"]))))
    if it.get("width") and it.get("height"):
        rows.append(("Risoluzione", f"{it['width']} × {it['height']} px"))
    rows.append(("Peso", escape(human_size(it.get("size")))))
    if it.get("lat") is not None and it.get("lon") is not None:
        la, lo = float(it["lat"]), float(it["lon"])
        rows.append(("Posizione GPS", f'<a href="https://www.openstreetmap.org/?mlat={la}&amp;mlon={lo}#map=14/{la}/{lo}" '
                                      f'target="_blank">{la:.5f}, {lo:.5f}</a>'))
    else:
        rows.append(("Posizione GPS", "non disponibile"))
    return '<div class="fs-meta">' + "".join(
        f'<div class="fs-meta-item"><span>{k}</span><b>{v}</b></div>' for k, v in rows) + "</div>"


@st.dialog("Visualizzatore", width="large")
def viewer() -> None:
    items = st.session_state.get("v_items") or []
    if not items:
        st.info("Nessun elemento da mostrare.")
        return
    n = len(items)
    i = min(max(0, st.session_state.get("v_idx", 0)), n - 1)
    it = items[i]
    n1, n2, n3 = st.columns([1, 3, 1], vertical_alignment="center")
    n1.button("‹ Precedente", key="v_prev", disabled=i == 0, on_click=_shift, args=(-1, n), width="stretch")
    n2.markdown(f"<div style='text-align:center'><b>{escape(it['name'])}</b><br>"
                f"<span class='fs-muted'>{i + 1} di {n}</span></div>", unsafe_allow_html=True)
    n3.button("Successivo ›", key="v_next", disabled=i >= n - 1, on_click=_shift, args=(1, n), width="stretch")

    if it["kind"] == "video":
        st.video(it["path"])
    else:
        data = preview_bytes(it["path"])
        if data:
            st.image(data, width="stretch")
        else:
            st.error(f"Impossibile aprire l'immagine: {it['path']}")
    st.markdown(meta_cards(it), unsafe_allow_html=True)
    st.caption("Percorso del file originale")
    st.code(it["path"], language=None)


def open_viewer(items: list[dict], idx: int) -> None:
    st.session_state["v_items"] = items
    st.session_state["v_idx"] = idx
    viewer()


# ------------------------------------------------------------ dati (cache per impronta)
@st.cache_data(show_spinner=False, max_entries=8)
def gallery_years(ver: tuple) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(date_taken,1,4) y FROM files WHERE status='embedded' AND data_incerta=0 ORDER BY y DESC")]


@st.cache_data(show_spinner=False, max_entries=32)
def gallery_months(ver: tuple, year: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(date_taken,6,2) m FROM files WHERE status='embedded' AND data_incerta=0 "
        "AND substr(date_taken,1,4)=? ORDER BY m", (year,))]


@st.cache_data(show_spinner=False, max_entries=64)
def gallery_page(ver: tuple, year: str, month: str, page: int) -> tuple[int, list[dict]]:
    where, args = "status='embedded' AND data_incerta=0 AND substr(date_taken,1,4)=?", [year]
    if month != "tutti":
        where += " AND substr(date_taken,6,2)=?"
        args.append(month)
    total = conn.execute(f"SELECT COUNT(*) FROM files WHERE {where}", args).fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        f"SELECT {ITEM_COLS} FROM files WHERE {where} ORDER BY date_taken, id LIMIT ? OFFSET ?",
        args + [PAGE_SIZE, page * PAGE_SIZE]).fetchall()]
    return total, rows


@st.cache_data(show_spinner=False, max_entries=8)
def folder_stats(ver: tuple) -> list[dict]:
    """Una riga per cartella finale del piano: file, video, byte."""
    return [dict(r) for r in conn.execute(
        "SELECT p.folder, COUNT(*) n, COALESCE(SUM(f.kind='video'),0) videos, COALESCE(SUM(f.size),0) bytes "
        "FROM plan p JOIN files f ON f.id=p.file_id WHERE p.folder IS NOT NULL "
        "GROUP BY p.folder ORDER BY p.folder").fetchall()]


@st.cache_data(show_spinner=False, max_entries=16)
def group_members(ver: tuple, folder: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT f.id, f.path, f.name, f.kind, f.date_taken, f.date_source, f.duration, f.lat, f.lon, "
        "f.width, f.height, f.size, p.context, p.score, p.is_dup, p.dup_of "
        "FROM plan p JOIN files f ON f.id=p.file_id WHERE p.folder=? ORDER BY f.date_taken, f.id", (folder,)).fetchall()]


@st.cache_data(show_spinner=False, max_entries=16)
def dup_pairs(ver: tuple, folder: str) -> list[tuple[dict, dict | None]]:
    """(duplicato, originale) per i gruppi _Duplicati e _Duplicati/Simili."""
    dups = group_members(ver, folder)
    ids = sorted({d["dup_of"] for d in dups if d["dup_of"]})
    origs = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, path, name, kind, width, height, size, date_taken FROM files "
        "WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(ids),)).fetchall()}
    return [(d, origs.get(d["dup_of"])) for d in dups if d["dup_of"] is not None]


def folder_kind(folder: str) -> str:
    """Tipo di cartella del piano, per i filtri della scheda dry-run."""
    if folder == organizer.SIMILI_FOLDER:
        return "_Duplicati/Simili"
    if folder == organizer.DUP_FOLDER:
        return "_Duplicati"
    if folder.startswith(organizer.UNDATED + "/"):
        return "Data incerta"
    leaf = folder.rsplit("/", 1)[-1]
    ctx = leaf.split(" - ", 1)[1] if " - " in leaf else leaf
    if ctx == "Varie":
        return "Varie"
    if ctx.startswith("Evento "):
        return "Eventi"
    return "Contesti"


# ------------------------------------------------------------ albero cartelle
def build_tree(rows: list[dict]) -> dict:
    root: dict = {}
    for r in rows:
        node = root
        for p in [p for p in r["folder"].split("/") if p]:
            node = node.setdefault(p, {"children": {}, "files": 0, "videos": 0})
        node["files"] += r["n"]
        node["videos"] += r["videos"] or 0
    return root


def _totals(node: dict) -> tuple[int, int]:
    f, v = node["files"], node["videos"]
    for c in node["children"].values():
        cf, cv = _totals(c)
        f, v = f + cf, v + cv
    return f, v


def tree_lines(children: dict, depth: int = 0) -> list[str]:
    lines: list[str] = []
    for name, child in sorted(children.items()):
        f, v = _totals(child)
        icon = "📁" if child["children"] else "🗂️"
        extra = f" · 🎬 {v}" if v else ""
        lines.append("    " * depth + f"- {icon} **{md(name)}** — {f:,} file{extra}")
        lines += tree_lines(child["children"], depth + 1)
    return lines


# ------------------------------------------------------------ volumi (hard link o copia?)
def _existing(p: Path) -> Path:
    for q in [p, *p.parents]:
        if q.exists():
            return q
    return p


def same_volume(a: str, b: str) -> bool | None:
    """True se due percorsi stanno sullo stesso volume (st_dev = numero di serie del volume su Windows)."""
    try:
        return os.stat(_existing(Path(a))).st_dev == os.stat(_existing(Path(b))).st_dev
    except OSError:
        return None


# ================================================================= scheda 1: indicizzazione
def tab_index() -> None:
    section("Cartelle e modello", "Dove sono le foto e quale modello usare per riconoscerle.")
    c1, c2 = st.columns(2)
    cfg.source_dir = c1.text_input("Cartella sorgente (sola lettura)", DEFAULT_SRC)
    cfg.output_dir = c2.text_input("Cartella di destinazione", cfg.output_dir or str(ROOT / "Organizzato"))
    c3, c4, c5 = st.columns(3)
    limit = c3.number_input("Test: limite file (0 = tutti)", 0, 1_000_000, 300, step=100)
    subdir = c4.text_input("Test: sottocartella (opzionale)", "")
    cfg.model_name = c5.text_input("Modello HuggingFace", cfg.model_name)
    if st.button("💾 Salva impostazioni"):
        cfg.save()
        st.toast("Impostazioni salvate in config.json", icon="✅")

    ffmpeg = ffmpeg_bin()
    st.markdown(
        f'<span class="fs-pill off">Device: {escape(cfg.resolve_device())}</span> '
        f'<span class="fs-pill {"ok" if ffmpeg else "off"}">ffmpeg: '
        f'{"trovato" if ffmpeg else "non trovato · i video senza fotogrammi non avranno miniatura"}</span>',
        unsafe_allow_html=True)

    st.divider()
    section("Indicizzazione", "Riprendibile: i file già elaborati non vengono rifatti.")
    proc = st.session_state.get("proc")
    running = proc is not None and proc.poll() is None
    b1, b2, _ = st.columns([1.3, 1.3, 4])
    if b1.button("▶ Avvia / riprendi", disabled=running, type="primary", width="stretch"):
        cfg.save()
        cmd = [sys.executable, "-m", "fotoscan", "index"]
        if limit:
            cmd += ["--limit", str(int(limit))]
        if subdir:
            cmd += ["--subdir", subdir]
        logf = open(ROOT / "index.log", "w", encoding="utf-8")
        st.session_state["proc"] = subprocess.Popen(cmd, cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT,
                                                    creationflags=_NO_WINDOW)
        st.rerun()
    if b2.button("⏹ Ferma", disabled=not running, width="stretch",
                 help="Il lavoro svolto resta nel catalogo: potrai riprenderlo."):
        proc.terminate()
        st.rerun()

    # si aggiorna ogni 2 s solo mentre l'indicizzazione gira
    @st.fragment(run_every=2 if running else None)
    def status_panel() -> None:
        p = st.session_state.get("proc")
        is_run = p is not None and p.poll() is None
        s = dbm.stats(conn)
        prog = dbm.get_meta(conn, "progress", "") or ""
        stage, done, total = (prog.split("|") + ["0", "0", "0"])[:3]
        if is_run:
            st.markdown('<span class="fs-pill">● In esecuzione</span>', unsafe_allow_html=True)
        elif stage == "completato":
            st.markdown('<span class="fs-pill ok">✓ Completato</span>', unsafe_allow_html=True)
        else:
            st.markdown('<span class="fs-pill off">⏸ Fermo</span>', unsafe_allow_html=True)
        try:
            tot_i, done_i = int(total), int(done)
        except ValueError:
            tot_i = done_i = 0
        if stage != "completato" and tot_i > 0:
            pct = min(1.0, done_i / tot_i)
            st.progress(pct, text=f"{stage} · {done_i:,} di {tot_i:,} ({pct:.0%})")
        m = st.columns(6)
        for col, (label, key) in zip(m, [("Totale", "totale"), ("Foto", "foto"), ("Video", "video"),
                                         ("Indicizzati", "indicizzati"), ("Duplicati esatti", "duplicati"),
                                         ("Errori", "errori")]):
            col.metric(label, f"{s[key]:,}")
        m = st.columns(3)
        m[0].metric("Foto senza data EXIF", f"{s['senza_data_exif']:,}")
        m[1].metric("Data incerta (solo mtime)", f"{s['data_incerta']:,}")
        m[2].metric("Da elaborare", f"{s['nuovi'] + (s['con_metadati'] - s['indicizzati']):,}")
        log = ROOT / "index.log"
        if log.exists():
            with st.expander("Log dell'ultima esecuzione"):
                st.code("".join(log.read_text(encoding="utf-8", errors="replace").splitlines(True)[-25:]))

    status_panel()
    errs = conn.execute("SELECT path,error FROM files WHERE status='error' LIMIT 200").fetchall()
    if errs:
        with st.expander(f"File con errori ({len(errs)}+) — saltati, non bloccano la scansione"):
            st.dataframe(pd.DataFrame([dict(r) for r in errs]), width="stretch")


# ================================================================= scheda 2: dry-run
def tab_dryrun() -> None:
    section("Regole e anteprima", "Calcola il piano (dry-run) e controlla come verranno organizzate le foto. Nulla viene ancora copiato.")
    left, right = st.columns([3, 2])
    with left:
        with st.expander("Etichette di contesto (cartelle tematiche)", expanded=False):
            st.caption("Nome della cartella + descrizione usata dal modello per riconoscere il contesto.")
            df = st.data_editor(pd.DataFrame(cfg.labels), num_rows="dynamic", width="stretch", key="labels_editor")
    with right:
        cfg.score_threshold = st.slider("Soglia assegnazione contesto", 0.05, 0.95, float(cfg.score_threshold), 0.01,
                                        help="Probabilità minima. Sotto soglia il file va in un 'Evento' temporale.")
        r1, r2 = st.columns(2)
        cfg.event_gap_hours = r1.number_input("Pausa tra eventi (ore)", 1.0, 720.0, float(cfg.event_gap_hours))
        cfg.min_event_size = int(r2.number_input("Min. file per un evento", 1, 500, int(cfg.min_event_size)))
        cfg.folder_template = st.text_input("Struttura cartelle", cfg.folder_template,
                                            help="Variabili: {anno} {mese:02d} {contesto}")
        cfg.dup_mode = st.selectbox("Duplicati esatti", ["subfolder", "skip"],
                                    index=["subfolder", "skip"].index(cfg.dup_mode),
                                    format_func=lambda x: "Sposta in _Duplicati (decido io)" if x == "subfolder"
                                    else "Escludi dal piano")
    if st.button("🔄 Ricalcola piano (dry-run)", type="primary"):
        try:
            cfg.labels = [r for r in df.fillna("").to_dict("records") if r.get("name") and r.get("prompt")]
            cfg.save()
            with st.spinner("Calcolo… (la prima volta carica il modello per le etichette nuove)"):
                from fotoscan.embedder import Embedder

                cached = {r[0] for r in conn.execute("SELECT prompt FROM label_cache")}
                emb = Embedder(cfg, device="cpu") if any(l["prompt"] not in cached for l in cfg.labels) else None
                res = organizer.build_plan(conn, cfg, embedder=emb)
            st.success(f"Piano: {res['file']} file in {res['cartelle']} cartelle · {res['duplicati']} duplicati · "
                       f"{res['eventi']} eventi")
        except Exception as e:  # noqa: BLE001
            st.error(f"Ricalcolo non riuscito: {e}")

    stats = folder_stats(plan_version())
    if not stats:
        st.info("Nessun piano ancora. Indicizza qualche file e premi «Ricalcola piano».")
        return
    tot_files = sum(r["n"] for r in stats)
    m = st.columns(4)
    m[0].metric("File nel piano", f"{tot_files:,}")
    m[1].metric("Cartelle finali", f"{len(stats):,}")
    m[2].metric("Video", f"{sum(r['videos'] for r in stats):,}")
    m[3].metric("Dimensione totale", human_size(sum(r["bytes"] for r in stats)))

    # ---- filtri
    f1, f2, f3 = st.columns([3, 2, 2])
    cats = f1.multiselect("Tipo di cartella", CATEGORIES, default=CATEGORIES, key="dry_cats")
    text = f2.text_input("Cerca nel nome cartella", "", key="dry_text", placeholder="es. 2020/06 o Mare")
    order = f3.selectbox("Ordina per", ["Nome", "Numero di file", "Dimensione"], key="dry_order",
                         format_func=lambda x: {"Nome": "Nome cartella", "Numero di file": "Numero di file (decrescente)",
                                                "Dimensione": "Dimensione (decrescente)"}[x])
    needle = text.strip().lower()
    rows = [r for r in stats if folder_kind(r["folder"]) in cats and (not needle or needle in r["folder"].lower())]
    if order == "Numero di file":
        rows.sort(key=lambda r: (-r["n"], r["folder"]))
    elif order == "Dimensione":
        rows.sort(key=lambda r: (-r["bytes"], r["folder"]))
    if not rows:
        st.info("Nessuna cartella corrisponde ai filtri.")
        return
    st.caption(f"{len(rows):,} cartelle su {len(stats):,} · {sum(r['n'] for r in rows):,} file")

    left, right = st.columns([2, 3], gap="large")
    with left:
        st.markdown("**Albero delle cartelle**")
        with st.container(height=540, border=True):
            st.markdown("\n".join(tree_lines(build_tree(rows))), unsafe_allow_html=True)
    with right:
        st.markdown("**Ispeziona e rinomina un gruppo**")
        names = [r["folder"] for r in rows]
        by_name = {r["folder"]: r for r in rows}
        sel = st.selectbox("Gruppo", names, key="dry_group",
                           format_func=lambda f: f"{f}  ·  {by_name[f]['n']:,} file  ·  {human_size(by_name[f]['bytes'])}")
        with st.expander("Elenco gruppi (ordinato come sopra)"):
            st.dataframe(pd.DataFrame([{"Cartella": r["folder"], "Tipo": folder_kind(r["folder"]), "File": r["n"],
                                        "Video": r["videos"], "Dimensione": human_size(r["bytes"])} for r in rows]),
                         width="stretch", hide_index=True)
        new_name = st.text_input("Rinomina o unisci con un altro gruppo", sel, key=f"rn_{sel}",
                                 help="Scrivi il nuovo nome: se coincide con un gruppo esistente le cartelle vengono unite.")
        if st.button("Applica rinomina") and new_name and new_name != sel:
            try:
                organizer.rename_group(conn, sel, new_name)
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"Rinomina non riuscita: {e}")

        ver = plan_version()
        members = group_members(ver, sel)
        if sel in (organizer.DUP_FOLDER, organizer.SIMILI_FOLDER):
            view = st.radio("Vista", ["Miniature", "Confronto originale / duplicato"], horizontal=True, key="dry_view")
            if view.startswith("Confronto"):
                show_dup_compare(ver, sel)
                return
        show_members(members, sel, ver)


def show_members(members: list[dict], sel: str, ver: tuple) -> None:
    total = len(members)
    if not total:
        st.info("Gruppo vuoto.")
        return
    pages = max(1, math.ceil(total / PAGE_SIZE))
    # niente key: cambiando gruppo (quindi numero di pagine) il widget riparte da pagina 1
    page = int(st.number_input("Pagina", 1, pages, 1, help=f"{total:,} file · {PAGE_SIZE} per pagina")) - 1
    page = min(page, pages - 1)
    start = page * PAGE_SIZE
    items = members[start:start + PAGE_SIZE]
    st.caption(f"{total:,} file nel gruppo · pagina {page + 1} di {pages} · clic su «Apri» per ingrandire")
    caps = [f"{m['context'] or '—'} · {m['score'] or 0:.2f}" for m in items]
    live(items, tile_grid, items, f"dry{page}_", members, start, 6, caps)

    with st.expander("Sposta singoli file in un'altra cartella"):
        by_id = {m["id"]: m for m in members}
        pick = st.multiselect("File da spostare", list(by_id), format_func=lambda i: by_id[i]["name"], key="dry_pick")
        dest = st.text_input("Cartella di destinazione", placeholder="es. 2022/08 - Mare e spiaggia", key="dry_dest")
        if st.button("Sposta selezionati") and pick and dest:
            try:
                organizer.move_files(conn, pick, dest)
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"Spostamento non riuscito: {e}")


def _res(d: dict | None) -> str:
    return f"{d['width']} × {d['height']} px" if d and d.get("width") and d.get("height") else "—"


def show_dup_compare(ver: tuple, folder: str) -> None:
    pairs = dup_pairs(ver, folder)
    if not pairs:
        st.info("Nessuna coppia: i duplicati di questo gruppo non hanno un originale collegato.")
        return
    st.caption(f"{len(pairs):,} coppie duplicato → originale. Il duplicato è quello che finisce in questa cartella.")
    table = [{"Duplicato": d["name"], "Originale": o["name"] if o else "—",
              "Risoluzione dup.": _res(d), "Risoluzione orig.": _res(o),
              "Peso dup.": human_size(d["size"]), "Peso orig.": human_size(o["size"] if o else None),
              "dup_of (id)": d["dup_of"], "Percorso duplicato": d["path"],
              "Percorso originale": o["path"] if o else "—"} for d, o in pairs]
    st.dataframe(pd.DataFrame(table), width="stretch", hide_index=True, height=260)

    idx = int(st.number_input("Coppia da confrontare", 1, len(pairs), 1, key=f"pair_{folder}")) - 1
    dup, orig = pairs[idx]
    shown = [x for x in (orig, dup) if x]

    def side_by_side() -> None:
        c1, c2 = st.columns(2)
        for col, label, it in ((c1, "Originale", orig), (c2, "Duplicato", dup)):
            with col:
                st.markdown(f"**{label}**")
                if it is None:
                    st.warning("Originale non trovato nel catalogo.")
                    continue
                tp = thumb_ready(it)
                if tp:
                    st.image(tp, width="stretch")
                else:
                    st.markdown('<div class="fs-empty">' + ("⏳" if thumb_pending(it) else "🖼") + "</div>",
                                unsafe_allow_html=True)
                st.write(f"**{md(it['name'])}**")
                st.markdown(f"Risoluzione: **{_res(it)}**  \nPeso: **{human_size(it['size'])}**  \n"
                            f"Data: **{it.get('date_taken') or '—'}**")
                st.caption("Percorso")
                st.code(it["path"], language=None)
                if label == "Duplicato":
                    st.caption(f"dup_of: {it['dup_of']}")
        if orig and dup and orig["size"] and dup["size"]:
            better = "il duplicato" if (dup["width"] or 0) * (dup["height"] or 0) > (orig["width"] or 0) * (orig["height"] or 0) else "l'originale"
            st.caption(f"Risoluzione più alta: {better}. Rapporto di peso dup./orig.: {dup['size'] / orig['size']:.2f}")

    live(shown, side_by_side)


# ================================================================= scheda 3: ricerca
@st.cache_resource(max_entries=2, show_spinner=False)
def emb_matrix(db_key: str, n: int, max_id: int):
    return dbm.load_embeddings(conn)


@st.cache_data(show_spinner=False, max_entries=200)
def query_vec(q: str):
    return text_model().encode_texts([q])[0]


@st.cache_data(show_spinner=False, max_entries=200)
def search_hits(q: str, ver: tuple, k: int = 120) -> list[dict]:
    """Top-k file per somiglianza col testo (cache per testo e per numero di embedding)."""
    ids, mat = emb_matrix(DB_KEY, ver[0], ver[1])
    if len(ids) == 0 or k <= 0:
        return []
    sims = mat @ query_vec(q)
    k = min(k, len(sims))
    top = np.argpartition(-sims, k - 1)[:k]
    top = top[np.argsort(-sims[top])]
    scores = {int(ids[i]): float(sims[i]) for i in top}
    info = {r["id"]: dict(r) for r in conn.execute(
        f"SELECT {ITEM_COLS} FROM files WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(list(scores)),)).fetchall()}
    out = []
    for i in top:
        r = info.get(int(ids[i]))
        if r:
            r["score"] = scores[int(ids[i])]
            out.append(r)
    return out


def tab_search() -> None:
    section("Ricerca", "Descrivi la foto che cerchi in linguaggio naturale: il modello confronta il testo con il contenuto delle immagini.")
    with st.container(key="searchbar"):
        q = st.text_input("Cerca", placeholder="🔍  tramonto in montagna · cane sul divano · scontrino",
                          label_visibility="collapsed", key="q")
    n_res = st.slider("Risultati da mostrare", 12, 120, 36, 6)
    if not q.strip():
        st.caption("Esempi: «festa di compleanno», «neve e sci», «documento scritto», «mare d'estate».")
        return
    ver = emb_version()
    if ver[0] == 0:
        st.info("Nessun file indicizzato: avvia l'indicizzazione nella scheda 1.")
        return
    hits = search_hits(q.strip(), ver)[:n_res]
    if not hits:
        st.info("Nessun risultato.")
        return
    caps = []
    for r in hits:
        d = parse_dt(r.get("date_taken"))
        caps.append(f"{d:%d/%m/%Y} · **{r['score']:.3f}**" if d else f"**{r['score']:.3f}**")
    st.caption(f"{len(hits)} risultati per «{q}», dal più pertinente. Il punteggio è la somiglianza del modello.")
    live(hits, tile_grid, hits, "ric", None, 0, 6, caps)


# ================================================================= scheda 4: galleria
def tab_gallery() -> None:
    section("Galleria", "Foto e video in ordine cronologico, raggruppati per giorno o per mese.")
    ver = files_version()
    years = gallery_years(ver)
    if not years:
        st.info("Nessun file indicizzato con data affidabile.")
        return
    g1, g2, g3, g4 = st.columns([1, 1, 1.4, 1])
    year = g1.selectbox("Anno", years)
    months = gallery_months(ver, year)
    month = g2.selectbox("Mese", ["tutti"] + months,
                         format_func=lambda m: m if m == "tutti" else f"{MONTHS[int(m) - 1].capitalize()} ({m})")
    group = g3.radio("Raggruppa per", ["giorno", "mese"], horizontal=True)
    total, _ = gallery_page(ver, year, month, 0)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = int(g4.number_input("Pagina", 1, pages, 1, help=f"{total:,} elementi · {PAGE_SIZE} per pagina")) - 1
    page = min(page, pages - 1)
    st.caption(f"{total:,} elementi · pagina {page + 1} di {pages}")
    _, rows = gallery_page(ver, year, month, page)

    # blocchi consecutivi per giorno o mese; ogni blocco: (chiave, offset, elementi, etichetta)
    blocks: list[dict] = []
    for off, r in enumerate(rows):
        d = parse_dt(r.get("date_taken"))
        if d is None:
            key, label = "—", "Data sconosciuta"
        elif group == "giorno":
            key, label = d.strftime("%Y-%m-%d"), day_label(d)
        else:
            key, label = d.strftime("%Y-%m"), month_label(d)
        if blocks and blocks[-1]["key"] == key:
            blocks[-1]["items"].append(r)
        else:
            blocks.append({"key": key, "label": label, "offset": off, "items": [r]})

    def render() -> None:
        for b, blk in enumerate(blocks):
            items_b = blk["items"]
            n_vid = sum(1 for x in items_b if x["kind"] == "video")
            extra = f" · 🎬 {n_vid} video" if n_vid else ""
            st.markdown(f'<div class="fs-date"><b>{escape(blk["label"])}</b>'
                        f'<span>{len(items_b)} elementi{extra}</span></div>', unsafe_allow_html=True)
            tile_grid(items_b, prefix=f"gal{b}_", all_items=rows, offset=blk["offset"], cols=8)

    live(rows, render)


# ================================================================= scheda 5: applica
@st.cache_data(ttl=120, show_spinner=False, max_entries=8)
def simulate_apply(ver: tuple, src: str, out: str, mode: str) -> dict:
    """apply_plan(apply=False) costa ~2 s su 14k file (stat per riga): cache breve per impronta del piano."""
    return organizer.apply_plan(conn, cfg, apply=False)


def tab_apply() -> None:
    section("Riepilogo e applicazione", "Controlla il piano prima di creare le cartelle reali. Gli originali non vengono mai toccati.")
    stats = folder_stats(plan_version())
    if not stats:
        st.info("Calcola prima il piano nella scheda 2.")
        return
    src, out = cfg.source_dir or DEFAULT_SRC, cfg.output_dir
    if not out:
        st.error("Scegli una cartella di destinazione nella scheda 1.")
        return
    cfg.link_mode = st.radio("Metodo", ["hardlink", "copy"], index=["hardlink", "copy"].index(cfg.link_mode),
                             horizontal=True,
                             format_func=lambda x: "Hard link (nessuno spazio extra sullo stesso disco)" if x == "hardlink"
                             else "Copia sempre")
    try:
        sim = simulate_apply(plan_version(), src, out, cfg.link_mode)  # sola simulazione: nessuna scrittura
    except ValueError as e:
        st.error(str(e))
        return
    except Exception as e:  # noqa: BLE001
        st.error(f"Simulazione non riuscita: {e}")
        return

    total_bytes = sum(r["bytes"] for r in stats)
    n_files = sum(r["n"] for r in stats)
    same = same_volume(src, out)
    hard_ok = cfg.link_mode == "hardlink" and same is True
    extra_bytes = 0 if hard_ok else total_bytes
    try:
        free = shutil.disk_usage(_existing(Path(out))).free
    except OSError:
        free = None

    st.markdown(f"**Sorgente (non verrà toccata):** `{src or '—'}`  \n**Destinazione:** `{out}`")
    if same is False:
        st.warning(f"Sorgente e destinazione sono su volumi diversi: gli hard link non sono possibili, quindi "
                   f"PhotoScan **copierà** i file. Spazio necessario: **{human_size(total_bytes)}** (somma delle dimensioni). "
                   "Valore massimo: i file già presenti nella destinazione non vengono ricopiati.")
    elif same is None:
        st.warning("Impossibile verificare il volume della destinazione: la stima assume una copia.")
    elif cfg.link_mode == "hardlink":
        st.success("Stesso volume: con gli hard link non serve spazio aggiuntivo (0 byte).")
    else:
        st.info(f"Modalità copia: spazio necessario {human_size(total_bytes)}.")
    if free is not None and extra_bytes > free:
        st.error(f"Spazio libero insufficiente sulla destinazione: liberi {human_size(free)}, servono {human_size(extra_bytes)}.")

    m = st.columns(5)
    m[0].metric("File nel piano", f"{n_files:,}")
    m[1].metric("Cartelle", f"{len(stats):,}")
    m[2].metric("Già presenti", f"{sim['gia_presenti']:,}")
    m[3].metric("Spazio extra", "0 B" if extra_bytes == 0 else f"≤ {human_size(extra_bytes)}")
    m[4].metric("Spazio libero (dest.)", human_size(free))
    n_dup = sum(r["n"] for r in stats if folder_kind(r["folder"]) in ("_Duplicati", "_Duplicati/Simili"))
    st.caption(f"Di cui nelle cartelle di duplicati: {n_dup:,} file.")

    st.warning("Con gli hard link originale e copia sono lo STESSO file: modificare uno modifica l'altro, "
               "e cancellare un nome non cancella i dati finché esiste l'altro. Gli originali non vengono mai spostati o cancellati.")
    ok = st.checkbox("Ho controllato l'anteprima e voglio creare le cartelle reali")
    if st.button("🚀 Crea cartelle ora", type="primary", disabled=not ok):
        try:
            cfg.save()
            bar = st.progress(0.0, text="Applico…")
            res = organizer.apply_plan(conn, cfg, apply=True,
                                       progress=lambda s, d, t: bar.progress(min(1.0, d / max(t, 1)),
                                                                             text=f"{d:,}/{t:,}"))
            st.success(f"Fatto: {res}")
            st.caption(f"Log: {Path(out) / '_fotoscan_apply.log'}")
        except Exception as e:  # noqa: BLE001
            st.error(f"Applicazione non riuscita: {e}")


# ================================================================= pagina
st.markdown('<div class="fs-hero"><span class="fs-logo">📷</span><div>'
            '<div class="fs-title">PhotoScan</div>'
            '<div class="fs-sub">Il tuo archivio di foto e video, ordinato in locale</div>'
            '</div></div>', unsafe_allow_html=True)
tabs = st.tabs(["1 · Indicizzazione", "2 · Anteprima & regole (dry-run)", "3 · Ricerca", "4 · Galleria", "5 · Esporta / Applica"])
with tabs[0]:
    guarded(tab_index)
with tabs[1]:
    guarded(tab_dryrun)
with tabs[2]:
    guarded(tab_search)
with tabs[3]:
    guarded(tab_gallery)
with tabs[4]:
    guarded(tab_apply)
