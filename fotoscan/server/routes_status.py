"""Gruppo "status": stato dell'app, impostazioni (config.json), dialoghi e apertura cartelle, spazio disco."""
from __future__ import annotations

import dataclasses
import os
import re
import shutil
import string
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from .. import db as dbm
from .. import sourcefs
from ..config import CONFIG_FILE, DEFAULT_LABELS, Config, all_models
from ..version import __version__ as APP_VERSION
from .deps import get_cfg, get_db, reload_cfg

router = APIRouter(prefix="/api", tags=["status"])

SETTING_KEYS = [
    "source_dir", "output_dir", "model_name", "device", "batch_size", "workers", "score_threshold",
    "score_temperature", "event_gap_hours", "max_event_days", "min_event_size", "event_burst_ratio", "event_names", "min_context_size", "near_dup_threshold",
    "near_dup_seconds", "near_dup_name_rule", "dup_mode", "link_mode", "folder_template", "labels",
    "null_prompts", "video_fractions", "enable_faces", "faces_threshold", "faces_assign_threshold",
    "faces_min_photos", "faces_det_size",
    "update_check_on_start", "update_auto_download", "update_auto_install", "update_prerelease",
    "faces_cluster_mode", "faces_topk", "apply_verify",
]
_save_lock = threading.Lock()
_dialog_lock = threading.Lock()


# ---------------------------------------------------------------- utilita'
def _effective_source(cfg: Config, conn) -> str:
    """Sorgente da config.json; se assente, quella registrata nel catalogo dall'ultima indicizzazione."""
    return cfg.source_dir or dbm.get_meta(conn, "source_dir", "") or ""


def _inside(child: Path, parent: Path) -> bool:
    """True se child coincide con parent o ne e' sottocartella (confronto per identita' di cartella:
    i nomi 8.3 di Windows come ANDREA~1 non sono normalizzati da resolve())."""
    for p in [child, *child.parents]:
        try:
            if p.exists() and os.path.samefile(p, parent):
                return True
        except OSError:
            continue
    return False


def _nearest_existing(p: Path) -> Path | None:
    for c in [p, *p.parents]:
        if c.exists():
            return c
    return None


def _model_cached(model_name: str) -> bool:
    """Il modello HuggingFace e' gia' nella cache locale, completo di config e pesi (nessun download necessario)."""
    from .. import netguard

    return netguard.model_in_cache(model_name)


def _catalog_bytes(db_path: str) -> int | None:
    """Dimensione del catalogo (file SQLite + WAL), None se non ancora creato."""
    total = 0
    for suffix in ("", "-wal"):
        try:
            total += os.path.getsize(db_path + suffix)
        except OSError:
            pass
    return total or None


def _settings_view(cfg: Config, conn) -> dict[str, Any]:
    out: dict[str, Any] = {k: getattr(cfg, k) for k in SETTING_KEYS}
    out["source_dir"] = _effective_source(cfg, conn)
    out["labels"] = [{"name": x.get("name", ""), "prompt": x.get("prompt", "")} for x in cfg.labels]
    out["null_prompts"] = list(cfg.null_prompts)
    out["video_fractions"] = list(cfg.video_fractions)
    return out


def _write_config(new: Config) -> None:
    """Scrittura atomica di config.json (file temporaneo + fsync + replace), poi ricarica la config in memoria."""
    tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    new.save(tmp)
    try:  # i dati su disco prima dello scambio: un blackout non lascia un config.json vuoto
        fd = os.open(tmp, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
    os.replace(tmp, CONFIG_FILE)
    reload_cfg()


# ---------------------------------------------------------------- sonde con tempo massimo
# Su un NAS spento o un disco scollegato os.scandir / disk_usage possono restare bloccati decine di secondi:
# se girassero direttamente nel threadpool delle richieste, poche chiamate di /status riempirebbero tutti i thread.
_probe_lock = threading.Lock()
_probe_cache: dict[str, tuple[float, Any]] = {}
_probe_busy: dict[str, threading.Thread] = {}
PROBE_TTL_S = 10.0
PROBE_TIMEOUT_S = 2.0


def _probe(key: str, fn: Callable[[], Any], default: Any, ttl: float = PROBE_TTL_S) -> Any:
    """Risultato di fn() con cache (PROBE_TTL_S) e al massimo una esecuzione per chiave. Se non risponde entro
    PROBE_TIMEOUT_S si usa l'ultimo valore noto (o `default`): il thread resta in fondo senza bloccare le richieste."""
    import time as _time

    with _probe_lock:
        hit = _probe_cache.get(key)
        if hit is not None and _time.monotonic() - hit[0] < ttl:
            return hit[1]
        th = _probe_busy.get(key)
        if th is None:
            def run() -> None:
                try:
                    val = fn()
                except Exception:  # noqa: BLE001
                    val = default
                with _probe_lock:
                    _probe_cache[key] = (_time.monotonic(), val)
                    _probe_busy.pop(key, None)

            th = threading.Thread(target=run, daemon=True, name="probe")
            _probe_busy[key] = th
            th.start()
    th.join(PROBE_TIMEOUT_S)
    with _probe_lock:
        hit = _probe_cache.get(key)
    return hit[1] if hit is not None else default


# ---------------------------------------------------------------- validazione impostazioni
def _text(label: str, max_len: int = 200) -> Callable[[Any], str]:
    def check(v: Any) -> str:
        s = v.strip() if isinstance(v, str) else ""
        if not s:
            raise ValueError(f"{label} non puo' essere vuoto.")
        if len(s) > max_len:
            raise ValueError(f"{label} e' troppo lungo (massimo {max_len} caratteri).")
        return s
    return check


def _num(label: str, lo: float, hi: float, integer: bool = False) -> Callable[[Any], float | int]:
    def check(v: Any) -> float | int:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{label} deve essere un numero.")
        if integer and not float(v).is_integer():
            raise ValueError(f"{label} deve essere un numero intero.")
        if not lo <= v <= hi:
            raise ValueError(f"{label} deve essere tra {_fmt(lo)} e {_fmt(hi)}.")
        return int(v) if integer else float(v)
    return check


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x).replace(".", ",")


def _choice(label: str, options: tuple[str, ...]) -> Callable[[Any], str]:
    def check(v: Any) -> str:
        if v not in options:
            raise ValueError(f"{label} non valido (valori ammessi: {', '.join(options)}).")
        return v
    return check


def _int_choice(label: str, options: tuple[int, ...]) -> Callable[[Any], int]:
    def check(v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not float(v).is_integer() or int(v) not in options:
            raise ValueError(f"{label} non valido (valori ammessi: {', '.join(str(x) for x in options)}).")
        return int(v)
    return check


def _flag(label: str) -> Callable[[Any], bool]:
    def check(v: Any) -> bool:
        if not isinstance(v, bool):
            raise ValueError(f"{label} deve essere vero o falso.")
        return v
    return check


def _folder_path(label: str) -> Callable[[Any], str]:
    """Cartella esistente; stringa vuota = non impostata."""
    def check(v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError(f"{label} non valida.")
        s = v.strip()
        if not s:
            return ""
        if not os.path.isdir(s):
            raise ValueError(f"{label} non esiste: {s}")
        return s
    return check


def _output_path(label: str) -> Callable[[Any], str]:
    """Cartella di destinazione: puo' non esistere ancora (la crea POST /apply/destination o l'applicazione),
    purche' il primo antenato esistente sia una cartella. Vuota = non impostata."""
    def check(v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError(f"{label} non valida.")
        s = v.strip()
        if not s:
            return ""
        near = _nearest_existing(Path(os.path.abspath(s)))
        if near is None or not near.is_dir():
            raise ValueError(f"{label} non valida: {s}")
        return s
    return check


def _text_list(label: str, lo: int, hi: int, max_len: int = 200) -> Callable[[Any], list[str]]:
    def check(v: Any) -> list[str]:
        if not isinstance(v, list) or not lo <= len(v) <= hi:
            raise ValueError(f"{label} deve contenere da {lo} a {hi} voci.")
        out = []
        for item in v:
            s = item.strip() if isinstance(item, str) else ""
            if not s or len(s) > max_len:
                raise ValueError(f"{label}: ogni voce deve essere testo non vuoto (massimo {max_len} caratteri).")
            out.append(s)
        return out
    return check


def _labels(v: Any) -> list[dict[str, str]]:
    if not isinstance(v, list) or not 1 <= len(v) <= 60:
        raise ValueError("Le etichette devono essere da 1 a 60.")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in v:
        name = item.get("name", "").strip() if isinstance(item, dict) and isinstance(item.get("name"), str) else ""
        prompt = item.get("prompt", "").strip() if isinstance(item, dict) and isinstance(item.get("prompt"), str) else ""
        if not name or not prompt:
            raise ValueError("Ogni etichetta richiede un nome e una descrizione.")
        if len(name) > 60 or len(prompt) > 200:
            raise ValueError(f"Etichetta '{name[:30]}': nome o descrizione troppo lunghi.")
        if any(c in name for c in "/\\") or name in (".", ".."):
            raise ValueError(f"Il nome dell'etichetta '{name}' non puo' contenere / o \\.")
        if name.casefold() in seen:
            raise ValueError(f"Etichetta duplicata: '{name}'.")
        seen.add(name.casefold())
        out.append({"name": name, "prompt": prompt})
    return out


_TEMPLATE_FIELDS = {("anno", ""), ("mese", "02d"), ("contesto", "")}  # stessi token di settings-rules.js
_TEMPLATE_TOKEN = re.compile(r"\{(anno|mese:02d|contesto)\}")


def _folder_template(v: Any) -> str:
    """Modello di cartella: solo {anno}, {mese:02d}, {contesto}; niente percorsi assoluti o risalite."""
    if not isinstance(v, str) or not v.strip():
        raise ValueError("Il modello delle cartelle non puo' essere vuoto.")
    s = v.strip()
    if ".." in s:
        raise ValueError("Il modello delle cartelle non puo' contenere '..'.")
    if s[0] in "/\\" or re.match(r"^[A-Za-z]:", s):
        raise ValueError("Il modello delle cartelle deve essere relativo (non puo' iniziare con / o con una lettera di unita').")
    try:
        parsed = list(string.Formatter().parse(s))
    except ValueError:
        raise ValueError("Modello delle cartelle non valido: usa {anno}, {mese:02d} e {contesto}.")
    for _lit, field, spec, conv in parsed:
        if field is None:
            continue
        if (field, spec or "") not in _TEMPLATE_FIELDS or conv:
            raise ValueError("Puoi usare solo {anno}, {mese:02d} e {contesto} nel modello delle cartelle.")
    if "{anno}" not in s:
        raise ValueError("Il modello delle cartelle deve contenere {anno}.")
    rest = _TEMPLATE_TOKEN.sub("", s)
    if re.search(r'[<>:"|?*]', rest):
        raise ValueError('Il nome delle cartelle contiene caratteri non ammessi: < > : " | ? *')
    return s


def _fractions(v: Any) -> list[float]:
    if not isinstance(v, list) or not 1 <= len(v) <= 10:
        raise ValueError("Le posizioni dei fotogrammi video devono essere da 1 a 10.")
    out = []
    for x in v:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 < x < 1:
            raise ValueError("Le posizioni dei fotogrammi video devono essere numeri tra 0 e 1 (esclusi).")
        out.append(float(x))
    return out


_CHECKS: dict[str, Callable[[Any], Any]] = {
    "source_dir": _folder_path("La cartella sorgente"),
    "output_dir": _output_path("La cartella di destinazione"),
    "model_name": _text("Il nome del modello", 200),
    "device": _choice("Il dispositivo", ("auto", "cuda", "mps", "cpu")),
    "batch_size": _num("La dimensione del lotto", 1, 512, integer=True),
    "workers": _num("Il numero di processi", 1, 32, integer=True),
    "score_threshold": _num("La soglia di punteggio", 0, 1),
    "score_temperature": _num("La temperatura del punteggio", 0.1, 20),
    "event_gap_hours": _num("La pausa tra eventi (ore)", 0.5, 168),
    "max_event_days": _num("La durata massima di un evento (giorni)", 0.5, 365),
    "min_event_size": _num("La dimensione minima di un evento", 1, 100, integer=True),
    "event_burst_ratio": _num("Quanto deve essere speciale un giorno per diventare un evento", 0, 20),
    "min_context_size": _num("La dimensione minima di una cartella di contesto", 1, 100, integer=True),
    "near_dup_threshold": _num("La soglia dei quasi-uguali", 0, 1),
    "near_dup_seconds": _num("La finestra dei quasi-uguali (secondi)", 0, 86400),
    "near_dup_name_rule": _flag("La regola sul nome dei modificati"),
    "dup_mode": _choice("La gestione dei duplicati", ("subfolder", "skip")),
    "link_mode": _choice("La modalita' di copia", ("hardlink", "copy")),
    "event_names": _choice("Il tipo di nome degli eventi", ("descrittivi", "data")),
    "folder_template": _folder_template,
    "labels": _labels,
    "null_prompts": _text_list("Le descrizioni neutre", 1, 50),
    "video_fractions": _fractions,
    "enable_faces": _flag("Il riconoscimento volti"),
    "faces_threshold": _num("La soglia volti", 0.2, 0.9),
    "faces_assign_threshold": _num("La soglia di assegnazione volti", 0.2, 0.9),
    "faces_min_photos": _num("Le foto minime per persona", 1, 50, integer=True),
    "faces_det_size": _int_choice("La dimensione di rilevamento volti", (480, 640, 800, 960)),
    "faces_cluster_mode": _choice("La modalita' di raggruppamento volti", ("exact", "approx")),
    "faces_topk": _num("I vicini per volto", 5, 500, integer=True),
    "apply_verify": _choice("La verifica in riapplicazione", ("full", "fast")),
    "update_check_on_start": _flag("La ricerca degli aggiornamenti all'avvio"),
    "update_auto_download": _flag("Il download automatico degli aggiornamenti"),
    "update_auto_install": _flag("L'installazione automatica degli aggiornamenti"),
    "update_prerelease": _flag("La scelta delle versioni di prova"),
}


def _validate(body: dict) -> tuple[dict[str, Any], dict[str, str]]:
    """Normalizza i soli campi presenti nel corpo. Ritorna (valori validi, {campo: messaggio italiano}).
    Campi sconosciuti vengono ignorati."""
    out: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, value in body.items():
        check = _CHECKS.get(key)
        if check is None:
            continue
        try:
            out[key] = check(value)
        except ValueError as e:
            errors[key] = str(e)
    return out, errors


# ---------------------------------------------------------------- /status
STATUS_TTL_S = 3.0  # /api/status e' chiamato spesso da piu' viste: risposta riusata se nulla e' cambiato
_status_cache: tuple | None = None  # (chiave, istante, risposta)
_status_lock = threading.Lock()


def _file_stamp(path: str) -> tuple:
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (None, None)


def _status_key(cfg: Config) -> tuple:
    """Cambia quando cambia la configurazione o quando qualcuno scrive nel catalogo (file principale o WAL)."""
    return (_file_stamp(str(CONFIG_FILE)), cfg.db_path, _file_stamp(cfg.db_path), _file_stamp(cfg.db_path + "-wal"))


def _device_label(cfg: Config) -> str:
    """Dispositivo da mostrare senza importare torch nel server (resterebbe in RAM): se torch e' gia'
    caricato si chiede a lui, altrimenti basta il rilevamento leggero (nvidia-smi + pacchetto installato)."""
    if cfg.device != "auto" or "torch" in sys.modules:
        return cfg.resolve_device()
    try:
        from .. import hardware
        return hardware.detect()["device"]
    except Exception:  # noqa: BLE001
        return "cpu"


def invalidate_status() -> None:
    global _status_cache
    with _status_lock:
        _status_cache = None


@router.get("/status")
def status(conn=Depends(get_db)):
    import time as _time

    global _status_cache
    cfg = get_cfg()
    key = _status_key(cfg)
    now = _time.monotonic()
    with _status_lock:
        hit = _status_cache
    if hit is not None and hit[0] == key and now - hit[1] < STATUS_TTL_S:
        return hit[2]
    out = _status_uncached(cfg, conn)
    with _status_lock:
        _status_cache = (key, now, out)
    return out


def _status_uncached(cfg: Config, conn) -> dict:
    source = _effective_source(cfg, conn)
    st = dbm.stats(conn)
    plan_rows = conn.execute(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT CASE WHEN folder IS NOT NULL THEN folder END) AS folders, "
        "SUM(folder IS NOT NULL) AS files FROM plan"
    ).fetchone()
    # Foto indicizzate che il piano non conosce ancora (0 se il piano non e' stato costruito).
    missing = 0
    if plan_rows["n"] > 0:
        missing = conn.execute(
            "SELECT COUNT(*) FROM files f LEFT JOIN plan p ON p.file_id = f.id "
            "WHERE f.status = 'embedded' AND p.file_id IS NULL"
        ).fetchone()[0]
    from .. import faces as faces_mod
    faces_info = faces_mod.status_info(cfg)
    src_net = bool(source) and sourcefs.is_network(source)
    src_reach = _probe("reach:" + source, lambda: sourcefs.reachable(source), False) if source else None
    return {
        "version": APP_VERSION,
        "first_run": not source and st["totale"] == 0,
        "source_dir": source,
        "source_network": src_net,
        "source_reachable": src_reach,
        "catalog_root": dbm.get_meta(conn, "catalog_root") or "",
        "output_dir": cfg.output_dir,
        "db_path": cfg.db_path,
        "counts": {k: st[k] for k in (
            "totale", "foto", "video", "indicizzati", "errori", "duplicati", "data_incerta", "senza_data_exif")},
        "plan": {"built": plan_rows["n"] > 0, "folders": plan_rows["folders"], "files": plan_rows["files"] or 0,
                 "missing": missing},
        "device": _device_label(cfg),
        "ffmpeg": cfg.find_ffmpeg()[0] is not None,
        "model": cfg.model_name,
        "model_cached": _model_cached(cfg.model_name),
        "embedding_model": dbm.get_meta(conn, "embedding_model") or (
            "google/siglip2-base-patch16-224" if st["indicizzati"] > 0 else cfg.model_name
        ),
        "model_mismatch": st["indicizzati"] > 0 and (
            (dbm.get_meta(conn, "embedding_model") or "google/siglip2-base-patch16-224") != cfg.model_name
        ),
        "available_models": [
            {**m, "cached": _model_cached(m["id"])} for m in all_models(cfg)
        ],
        "faces": faces_info,
        "faces_available": faces_info["available"],
        "faces_reason": faces_info["reason"],
        "catalog_bytes": _catalog_bytes(cfg.db_path),
        "phone_import": _phone_import_summary(conn),
    }


def _phone_import_summary(conn) -> dict | None:
    """Ultimo scarico dal telefono (null se non ne e' mai stato fatto uno)."""
    from ..phone_log import days_since, last_import

    rec = last_import(conn)
    if rec is None:
        return None
    return {"when": rec.when, "device": rec.device, "new_files": rec.new_files,
            "total_seen": rec.total_seen, "days_ago": days_since(rec)}


# ---------------------------------------------------------------- /settings
@router.get("/settings")
def get_settings(conn=Depends(get_db)):
    return _settings_view(get_cfg(), conn)


@router.put("/settings")
def put_settings(body: dict = Body(...), conn=Depends(get_db)):
    """Aggiornamento parziale: i campi non inviati restano invariati."""
    with _save_lock:
        cur = get_cfg()
        updates, errors = _validate(body)
        src = updates.get("source_dir", cur.source_dir) or _effective_source(cur, conn)
        out = updates.get("output_dir", cur.output_dir)
        if "output_dir" not in errors and src and out and (_inside(Path(out), Path(src)) or _inside(Path(src), Path(out))):
            errors["output_dir"] = "La cartella di destinazione non puo' essere dentro (o contenere) la cartella sorgente."
        # La sorgente non puo' coincidere o sovrapporsi alla cartella di backup sul NAS (si copierebbe su se' stessa).
        nas = cur.nas_dir
        if "source_dir" in updates and "source_dir" not in errors and updates["source_dir"] and nas \
                and (_inside(Path(nas), Path(updates["source_dir"])) or _inside(Path(updates["source_dir"]), Path(nas))):
            errors["source_dir"] = "La cartella sorgente non puo' coincidere o sovrapporsi alla cartella di backup sul NAS."
        # Modello e dispositivo devono esistere davvero su questo computer, altrimenti l'analisi fallisce piu' tardi.
        mn = updates.get("model_name")
        if mn is not None and mn != cur.model_name and mn not in {m["id"] for m in all_models(cur)}:
            errors["model_name"] = "Modello sconosciuto: scegline uno dell'elenco o aggiungilo da Impostazioni > Per esperti."
        dev = updates.get("device")
        if dev in ("cuda", "mps") and dev != cur.device:
            try:
                from .. import hardware

                disponibile = hardware.detect()["device"] == dev
            except Exception:  # noqa: BLE001 - rilevamento non riuscito: non si blocca l'utente
                disponibile = True
            if not disponibile:
                errors["device"] = ("Questo computer non ha una scheda video "
                                    f"{'NVIDIA con PyTorch CUDA' if dev == 'cuda' else 'Apple compatibile'}: usa Automatico o Processore.")
        if errors:
            # Stesso formato di HTTPException (detail = testo per l'avviso), con i messaggi anche per campo.
            return JSONResponse(status_code=400, content={"detail": " ".join(errors.values()), "fields": errors})
        new = dataclasses.replace(cur, **updates)
        _write_config(new)
        if "model_name" in updates and updates["model_name"] != cur.model_name:
            from . import routes_search  # import locale per evitare cicli

            routes_search.reset_model()
        return _settings_view(get_cfg(), conn)


# Valori di fabbrica (dalla dataclass Config): la UI li usa per "Ripristina predefiniti" senza duplicarli.
_DEFAULT_KEYS = ["score_threshold", "score_temperature", "event_gap_hours", "max_event_days", "min_event_size",
                 "min_context_size", "event_burst_ratio", "event_names", "near_dup_threshold", "near_dup_seconds", "near_dup_name_rule", "folder_template",
                 "labels", "null_prompts", "faces_threshold", "faces_assign_threshold", "faces_min_photos", "faces_det_size"]


@router.get("/settings/defaults")
def settings_defaults():
    base = Config()
    return {k: getattr(base, k) for k in _DEFAULT_KEYS}


@router.post("/settings/reset-labels")
def reset_labels(conn=Depends(get_db)):
    with _save_lock:
        new = dataclasses.replace(get_cfg(), labels=[dict(x) for x in DEFAULT_LABELS])
        _write_config(new)
        return _settings_view(get_cfg(), conn)


# ---------------------------------------------------------------- /system
def _pick_folder_blocking(title: str | None, initial: str | None) -> tuple[bool, str | None]:
    """Finestra nativa di scelta cartella. Ritorna (disponibile, percorso o None se annullata)."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return False, None
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        kwargs: dict[str, Any] = {"parent": root, "title": title or "Scegli una cartella", "mustexist": True}
        if initial and os.path.isdir(initial):
            kwargs["initialdir"] = initial
        chosen = filedialog.askdirectory(**kwargs)
        return True, (os.path.normpath(chosen) if chosen else None)
    except Exception:  # nessun display, Tk non disponibile, ecc.
        return False, None
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


@router.post("/system/pick-folder")
def pick_folder(body: dict | None = Body(default=None)):
    """Sync di proposito: FastAPI lo esegue in un thread, il loop non si blocca."""
    body = body or {}
    title = body.get("title") if isinstance(body.get("title"), str) else None
    initial = body.get("initial") if isinstance(body.get("initial"), str) else None
    if not _dialog_lock.acquire(blocking=False):
        raise HTTPException(409, "Una finestra di scelta cartella e' gia' aperta.")
    try:
        available, path = _pick_folder_blocking(title, initial)
        return {"path": path, "available": available}
    finally:
        _dialog_lock.release()


def _spawn_open(args: list[str]) -> None:
    """Avvia xdg-open/open senza aspettarlo. Nell'AppImage l'ambiente (LD_LIBRARY_PATH, ...) e' quello del pacchetto
    e romperebbe l'app di sistema: lo si ripulisce; il processo e' staccato e raccolto da un thread (niente zombie)."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH", "APPDIR", "APPIMAGE")}
    try:
        proc = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        raise HTTPException(500, "Impossibile aprire la cartella o il file con le app di sistema.") from None
    threading.Thread(target=proc.wait, daemon=True, name="open-wait").start()


@router.post("/system/open-folder")
def open_folder(body: dict = Body(...), conn=Depends(get_db)):
    """Apre in Esplora risorse solo cartelle esistenti dentro output_dir o source_dir."""
    raw = body.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(400, "Indica la cartella da aprire.")
    cfg = get_cfg()
    roots = [Path(os.path.abspath(r)) for r in (cfg.output_dir, _effective_source(cfg, conn)) if r]
    target = Path(os.path.abspath(raw.strip()))
    if not target.is_dir():
        raise HTTPException(404, "La cartella non esiste.")
    if not any(_inside(target, r) for r in roots if r.is_dir()):
        raise HTTPException(400, "Si possono aprire solo le cartelle di destinazione o sorgente.")
    if sys.platform == "win32":
        os.startfile(str(target))  # noqa: S606 - percorso verificato sopra
    else:
        _spawn_open(["open" if sys.platform == "darwin" else "xdg-open", str(target)])
    return {"ok": True}


@router.post("/system/open-file")
def open_file(body: dict = Body(...), conn=Depends(get_db)):
    """Apre un file con l'app predefinita (action=open) oppure lo mostra nella sua cartella (action=reveal).
    Solo file esistenti dentro la cartella sorgente o di destinazione. Il file non viene mai modificato."""
    raw = body.get("path")
    action = body.get("action", "open")
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(400, "Indica il file da aprire.")
    if action not in ("open", "reveal"):
        raise HTTPException(400, "Azione non valida.")
    cfg = get_cfg()
    roots = [Path(os.path.abspath(r)) for r in (cfg.output_dir, _effective_source(cfg, conn)) if r]
    target = Path(os.path.abspath(raw.strip()))
    if not target.is_file():
        raise HTTPException(404, "Il file non esiste.")
    if not any(_inside(target, r) for r in roots if r.is_dir()):
        raise HTTPException(400, "Si possono aprire solo i file della sorgente o della destinazione.")
    if sys.platform == "win32":
        if action == "reveal":
            subprocess.Popen(["explorer", f"/select,{target}"])
        else:
            os.startfile(str(target))  # noqa: S606 - percorso verificato sopra
    else:
        if action == "reveal":
            _spawn_open(["xdg-open", str(target.parent)])
        else:
            _spawn_open(["open" if sys.platform == "darwin" else "xdg-open", str(target)])
    return {"ok": True}


@router.get("/system/disk")
def disk(path: str | None = Query(default=None), conn=Depends(get_db)):
    cfg = get_cfg()
    source = _effective_source(cfg, conn)
    target = (path or "").strip() or cfg.output_dir or source
    if not target:
        raise HTTPException(400, "Indica una cartella.")
    if "\0" in target:
        raise HTTPException(400, "Percorso non valido.")
    p = Path(os.path.abspath(target))
    target_network = sourcefs.is_network(target)
    source_network = bool(source) and sourcefs.is_network(source)
    nulla = {"exists": False, "is_file": False, "reachable": False, "free_bytes": None, "total_bytes": None,
             "drive": None, "same_volume_as_source": False, "network": target_network,
             "source_network": source_network}

    def misura():
        probe = _nearest_existing(p)  # per lo spazio libero: la cartella stessa o il primo antenato esistente
        if probe is None:
            return None
        src_probe = _nearest_existing(Path(os.path.abspath(source))) if source else None
        try:
            usage = shutil.disk_usage(probe)
            is_dir = p.is_dir()
            return (probe, usage, is_dir, p.is_file(), sourcefs.reachable(str(p)) if is_dir else False,
                    src_probe is not None and os.stat(probe).st_dev == os.stat(src_probe).st_dev)
        except OSError:
            return None

    res = _probe("disk:" + str(p) + "|" + source, misura, None, ttl=1.0)
    if res is None:
        return nulla
    probe, usage, is_dir, is_file, is_reach, same_vol = res
    return {
        "exists": is_dir,
        "is_file": is_file,  # un file al posto della cartella: messaggio diverso dal "non esiste"
        "reachable": is_reach,
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "drive": probe.anchor or str(probe),
        "same_volume_as_source": same_vol,
        "network": target_network,
        "source_network": source_network,
    }
