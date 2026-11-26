"""Indicizzazione in background: `python -m fotoscan index` come sottoprocesso nascosto.

Stato letto da meta 'progress' del catalogo (formato 'stage|done|total|epoch') e dai conteggi db.stats.
Un processo lasciato in vita da un riavvio del server viene riconosciuto dal pid file in cache_dir.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, HTTPException

from .. import db as dbm
from .. import sourcefs
from ..config import ROOT
from . import deps, jobs

router = APIRouter(prefix="/api", tags=["index"])

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RATE_WINDOW_S = 30.0  # finestra della media mobile della velocita'
LOG_TAIL_LINES = 30
NOISE = re.compile(
    r"it/s\]|s/it\]|\d+%\|| ETA |Downloading|Fetching \d+ files|Loading weights|"
    r"Warning|warnings\.warn|^\s*$"
)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class _Run:
    """Un'indicizzazione in corso: Popen (se avviata da questo server) oppure processo orfano (solo pid)."""

    def __init__(self, pid: int, started: float, proc: subprocess.Popen | None = None):
        self.pid = pid
        self.started = started
        self.proc = proc
        self.exit_code: int | None = None
        self.finished = False

    def alive(self) -> bool:
        if self.finished:
            return False
        if self.proc is not None:
            rc = self.proc.poll()
            if rc is not None:
                self.exit_code, self.finished = rc, True
                return False
            return True
        if not _pid_alive(self.pid):
            self.finished = True
            return False
        return True


_lock = threading.Lock()  # protegge _current
_current: _Run | None = None
_rate_lock = threading.Lock()
_rate_pts: deque = deque(maxlen=400)  # (t, done) per la fase corrente
_rate_stage: str | None = None


# ------------------------------------------------------------ utilita' processo / file
def _pid_alive(pid: int) -> bool:
    """Controlla il pid senza os.kill (su Windows os.kill(pid, 0) TERMINA il processo)."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    alive = _pid_alive_win(pid)
    if alive is not None:
        return alive
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True,
                             text=True, creationflags=NO_WINDOW, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    # riga CSV: "python.exe","1234",...  -> evita di confondere un pid riciclato
    return f'"{pid}"' in out and "python" in out.lower()


def _pid_alive_win(pid: int) -> bool | None:
    """Stesso controllo di tasklist (vivo e eseguibile python/PhotoScan) via API, senza un processo per
    chiamata: il monitor di un'indicizzazione orfana lo chiama ogni secondo. None se l'API non risponde."""
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return None
            if code.value != 259:  # STILL_ACTIVE
                return False
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return None
            exe = os.path.basename(buf.value).lower()
            return "python" in exe or "photoscan" in exe
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return None


def _pid_file(cache_dir: str) -> Path:
    return Path(cache_dir) / "index.pid"


def _log_file(cache_dir: str) -> Path:
    return Path(cache_dir) / "index.log"


def _write_pid(cache_dir: str, pid: int, started: float) -> None:
    _pid_file(cache_dir).write_text(f"{pid}\n{started:.3f}", encoding="utf-8")


def _remove_pid(cache_dir: str) -> None:
    try:
        _pid_file(cache_dir).unlink()
    except OSError:
        pass


def _ensure_orphan_locked() -> None:
    """Se il server e' stato riavviato mentre l'indicizzazione gira, riprende a seguirla dal pid file."""
    global _current
    if _current is not None and not _current.finished:
        return
    cache_dir = deps.get_cfg().cache_dir
    pf = _pid_file(cache_dir)
    if not pf.exists():
        return
    try:
        pid_s, _, started_s = pf.read_text(encoding="utf-8").partition("\n")
        pid = int(pid_s.strip())
        started = float(started_s.strip()) if started_s.strip() else pf.stat().st_mtime
    except (OSError, ValueError):
        _remove_pid(cache_dir)
        return
    if not _pid_alive(pid):
        _remove_pid(cache_dir)
        return
    _current = _Run(pid, started)
    threading.Thread(target=_monitor, args=(_current,), daemon=True, name="index-monitor").start()


def _current_run() -> _Run | None:
    with _lock:
        _ensure_orphan_locked()
        run = _current
    return run if run is not None and run.alive() else None


# ------------------------------------------------------------ stato
def _read_progress(conn) -> tuple[str, int, int, float | None]:
    raw = dbm.get_meta(conn, "progress")
    if not raw:
        return "", 0, 0, None
    try:
        parts = raw.split("|")
        epoch = float(parts[3]) if len(parts) > 3 else None
        return parts[0], int(parts[1]), int(parts[2]), epoch
    except (ValueError, IndexError):
        return "", 0, 0, None


def _rate(stage: str, done: int, now: float) -> float | None:
    """Media mobile (ultimi RATE_WINDOW_S secondi) dei file completati nella fase corrente."""
    global _rate_stage
    with _rate_lock:
        if stage != _rate_stage or (_rate_pts and done < _rate_pts[-1][1]):
            _rate_pts.clear()
            _rate_stage = stage
        if not _rate_pts or now - _rate_pts[-1][0] >= 0.5:
            _rate_pts.append((now, done))
        while len(_rate_pts) > 2 and now - _rate_pts[0][0] > RATE_WINDOW_S:
            _rate_pts.popleft()
        if len(_rate_pts) < 2:
            return None
        (t0, d0), (t1, d1) = _rate_pts[0], _rate_pts[-1]
        if t1 - t0 < 2:
            return None
        return max(d1 - d0, 0) / (t1 - t0)


def _log_tail(cache_dir: str, n: int = LOG_TAIL_LINES) -> list[str]:
    path = _log_file(cache_dir)
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(f.tell() - 65536, 0))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    for chunk in text.replace("\r", "\n").split("\n"):  # le barre tqdm usano \r
        line = ANSI.sub("", chunk).strip()
        if line and not NOISE.search(line):
            out.append(line[:300])
    return out[-n:]


SLOW_TTL_S = 5.0  # conteggi e raggiungibilita' della sorgente: al massimo ogni 5 s durante l'indicizzazione
_slow_lock = threading.Lock()
_slow: dict = {}


def _db_stamp(db_path: str) -> tuple:
    out = []
    for suf in ("", "-wal"):
        try:
            st = os.stat(db_path + suf)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _slow_stats(conn, db_path: str, running: bool) -> dict:
    """db.stats con cache: durante l'indicizzazione al massimo ogni SLOW_TTL_S, a riposo finche' il
    catalogo non cambia (stat del file e del WAL)."""
    now = time.monotonic()
    stamp = _db_stamp(db_path)
    with _slow_lock:
        c = _slow.get("stats")
        if c and c[0] == db_path:
            fresh = (now - c[1] < SLOW_TTL_S) if running else (c[2] == stamp)
            if fresh:
                return c[3]
    stats = dbm.stats(conn)
    with _slow_lock:
        _slow["stats"] = (db_path, now, stamp, stats)
    return stats


def _source_state(source: str) -> tuple[bool, bool | None]:
    """(di rete, raggiungibile) con cache SLOW_TTL_S: su un NAS ogni controllo e' un giro di rete."""
    if not source:
        return False, None
    now = time.monotonic()
    with _slow_lock:
        c = _slow.get("source")
        if c and c[0] == source and now - c[1] < SLOW_TTL_S:
            return c[2], c[3]
    net = sourcefs.is_network(source)
    reach = sourcefs.reachable(source)
    with _slow_lock:
        _slow["source"] = (source, now, net, reach)
    return net, reach


def _snapshot(run: _Run | None, running: bool) -> dict:
    cfg = deps.get_cfg()
    stage, done, total, epoch = "", 0, 0, None
    stats: dict = {}
    error_code: str | None = None
    last_rebase: dict | None = None
    source = cfg.source_dir or ""
    try:
        conn = deps.open_db()
        try:
            stage, done, total, epoch = _read_progress(conn)
            stats = _slow_stats(conn, cfg.db_path, running)
            if not source:
                source = dbm.get_meta(conn, "source_dir", "") or ""
            raw_err = dbm.get_meta(conn, "last_index_error", "") or ""
            if raw_err and not running:
                error_code = raw_err
            raw_rebase = dbm.get_meta(conn, "last_rebase", "") or ""
            if raw_rebase:
                try:
                    last_rebase = json.loads(raw_rebase)
                except ValueError:
                    last_rebase = None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - catalogo bloccato o non ancora creato: stato parziale
        pass
    eta = rate = None
    if run is not None and (epoch is None or epoch < run.started - 5):
        # progresso residuo di un'esecuzione precedente: non lo mostriamo come attuale
        stage, done, total = ("avvio" if running else ""), 0, 0
    if running and run is not None:
        rate = _rate(stage, done, time.time())
        if rate and total > done:
            eta = (total - done) / rate
    if stage == "completato":
        eta = 0.0
    percent = round(done * 100.0 / total, 1) if total else (100.0 if stage == "completato" else None)
    src_net, src_reach = _source_state(source)
    return {
        "running": running,
        "stage": stage,
        "done": done,
        "total": total,
        "percent": percent,
        "eta_seconds": eta,
        "rate_per_sec": round(rate, 2) if rate is not None else None,
        "started": run.started if running and run is not None else None,
        "exit_code": run.exit_code if run is not None and not running else None,
        "error_code": error_code,
        "last_rebase": last_rebase,
        "source_network": src_net,
        "source_reachable": src_reach,
        "log_tail": _log_tail(cfg.cache_dir),
        "stats": stats,
    }


def _monitor(run: _Run) -> None:
    """Pubblica lo stato su SSE ogni ~1 s finche' il processo gira, poi uno stato finale."""
    global _current
    cache_dir = deps.get_cfg().cache_dir
    while True:
        alive = run.alive()
        try:
            jobs.bus.publish("index", _snapshot(run, alive))
        except Exception:  # noqa: BLE001 - il monitor non deve morire per un errore di lettura
            pass
        if not alive:
            break
        time.sleep(1.0)
    with _lock:  # pid file rimosso sotto lock: non cancella il pid di un avvio successivo
        if _current is run:
            _current = None
            _remove_pid(cache_dir)


# ------------------------------------------------------------ API
@router.get("/index/status")
def index_status():
    run = _current_run()
    return _snapshot(run, run is not None)


def _resolve_source(conn) -> str:
    cfg = deps.get_cfg()
    source = cfg.source_dir or dbm.get_meta(conn, "source_dir", "") or ""
    if not source:
        raise HTTPException(400, "Nessuna cartella di foto impostata: scegli prima la cartella sorgente")
    if not Path(source).is_dir():
        if sourcefs.is_network(source):
            raise HTTPException(400, f"Il disco di rete non e' raggiungibile: {source}. Controlla che il NAS sia acceso e connesso.")
        raise HTTPException(400, f"La cartella sorgente non esiste: {source}")
    return source


@router.post("/index/start")
def index_start(body: Optional[dict] = Body(None)):
    global _current
    body = body or {}
    limit, subdir = body.get("limit"), body.get("subdir")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise HTTPException(400, "Il limite di file deve essere un numero intero positivo")
    if subdir is not None and not isinstance(subdir, str):
        raise HTTPException(400, "La sottocartella deve essere un testo")
    with _lock:
        _ensure_orphan_locked()
        if _current is not None and _current.alive():
            raise HTTPException(409, "Indicizzazione gia' in corso")
        cfg = deps.get_cfg()
        conn = deps.open_db()
        try:
            source = _resolve_source(conn)
        finally:
            conn.close()
        args = [sys.executable, "-m", "fotoscan", "index", "--source", source]
        if subdir:
            sub = subdir.strip().replace("/", "\\")
            if not sub or Path(sub).is_absolute() or ".." in Path(sub).parts:
                raise HTTPException(400, "Sottocartella non valida")
            if not (Path(source) / sub).is_dir():
                raise HTTPException(400, f"La sottocartella non esiste: {sub}")
            args += ["--subdir", sub]
        if limit:
            args += ["--limit", str(limit)]

        Path(cfg.cache_dir).mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        started = time.time()
        with open(_log_file(cfg.cache_dir), "a", encoding="utf-8") as log:
            log.write(f"\n--- avvio indicizzazione {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            log.flush()
            try:
                proc = subprocess.Popen(args, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
            except OSError as e:
                raise HTTPException(500, f"Impossibile avviare l'indicizzazione: {e}")
        _write_pid(cfg.cache_dir, proc.pid, started)
        run = _Run(proc.pid, started, proc)
        _current = run
        threading.Thread(target=_monitor, args=(run,), daemon=True, name="index-monitor").start()
    return {"started": True}


@router.post("/index/stop")
def index_stop():
    run = _current_run()
    if run is None:
        return {"stopped": False}
    # /T: l'albero intero (il launcher venv + il python reale); riprendibile al prossimo avvio
    subprocess.run(["taskkill", "/PID", str(run.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    deadline = time.time() + 5
    while run.alive() and time.time() < deadline:
        time.sleep(0.2)
    return {"stopped": not run.alive()}


@router.get("/index/errors")
def index_errors(limit: int = 200):
    conn = deps.open_db()
    try:
        rows = conn.execute("SELECT path, error FROM files WHERE status='error' ORDER BY id LIMIT ?",
                            (max(1, min(limit, 5000)),)).fetchall()
    finally:
        conn.close()
    return [{"path": r["path"], "error": r["error"]} for r in rows]


@router.post("/index/retry-errors")
def index_retry_errors():
    if _current_run() is not None:
        raise HTTPException(409, "Riprova gli errori quando l'indicizzazione e' ferma")
    conn = deps.open_db()
    try:
        with conn:
            a = conn.execute("UPDATE files SET status='meta', error=NULL WHERE status='error' AND hash IS NOT NULL")
            b = conn.execute("UPDATE files SET status='new', error=NULL WHERE status='error' AND hash IS NULL")
        return {"retried": a.rowcount + b.rowcount}
    finally:
        conn.close()


@router.post("/index/reset-embeddings")
def index_reset_embeddings(body: dict = Body(default={})):
    """Svuota solo gli embedding per re-indicizzare con un nuovo modello (preserva foto, metadati, album e preferiti)."""
    if not body.get("confirm"):
        raise HTTPException(400, "Serve conferma esplicita per azzerare gli embedding.")
    if _current_run() is not None:
        raise HTTPException(409, "Ferma prima l'indicizzazione in corso.")
    conn = deps.open_db()
    try:
        with conn:
            conn.execute("DELETE FROM embeddings")
            conn.execute("DELETE FROM label_cache")
            conn.execute("DELETE FROM label_suggestions")
            conn.execute("DELETE FROM visual_labels")
            conn.execute("DELETE FROM meta WHERE key='embedding_model' OR key LIKE 'ctx_cache:%'")
            cur = conn.execute("UPDATE files SET status='meta' WHERE status='embedded'")
        from . import routes_search  # import locale per evitare cicli

        routes_search.reset_model()
        return {"reset": int(cur.rowcount)}
    finally:
        conn.close()
