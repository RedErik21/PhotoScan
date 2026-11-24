"""Server di sviluppo ISOLATO per un agente: copia il catalogo vero (sola lettura sul sorgente) e avvia l'API su una porta propria.

  python tools/devserver.py start --name gallery --port 9101     -> stampa l'URL
  python tools/devserver.py stop  --name gallery
  python tools/devserver.py restart --name gallery --port 9101   (dopo modifiche al backend)

Il frontend e' servito da disco: le modifiche a frontend/ si vedono con un semplice ricaricamento.
Dati in <DEV_ROOT>/<name>/ (catalogo copiato + cache + log). Il catalogo vero non viene MAI scritto.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEV_ROOT = Path(os.environ.get("FOTOSCAN_DEV_ROOT") or Path(tempfile.gettempdir()) / "fotoscan-dev")
SEED = ROOT / "catalogo.db"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def paths(name: str) -> dict[str, Path]:
    d = DEV_ROOT / name
    return {"dir": d, "db": d / "catalogo.db", "cache": d / "cache", "pid": d / "server.pid", "log": d / "server.log",
            "config": d / "config.json"}


def snapshot(dst: Path) -> None:
    """Copia consistente del catalogo (API di backup SQLite, sicura anche con WAL)."""
    src = sqlite3.connect(f"file:{SEED}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    out.close()
    src.close()


def is_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def stop(name: str) -> None:
    p = paths(name)
    if p["pid"].exists():
        pid = p["pid"].read_text().strip()
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        p["pid"].unlink(missing_ok=True)


def start(name: str, port: int, fresh: bool = False) -> None:
    p = paths(name)
    stop(name)
    p["dir"].mkdir(parents=True, exist_ok=True)
    p["cache"].mkdir(parents=True, exist_ok=True)
    if fresh or not p["db"].exists():
        for ext in ("", "-wal", "-shm"):
            Path(str(p["db"]) + ext).unlink(missing_ok=True)
        snapshot(p["db"])
    # FOTOSCAN_CONFIG isola anche config.json: i test di PUT /settings non toccano mai quello vero del progetto.
    env = dict(os.environ, FOTOSCAN_DB=str(p["db"]), FOTOSCAN_CACHE=str(p["cache"]), FOTOSCAN_CONFIG=str(p["config"]),
               PYTHONIOENCODING="utf-8")
    log = open(p["log"], "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "fotoscan.server", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    p["pid"].write_text(str(proc.pid))
    for _ in range(60):
        if is_up(port):
            print(f"OK http://127.0.0.1:{port}/   (pid {proc.pid}, dati {p['dir']})")
            return
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    print(f"ERRORE: server non partito. Log: {p['log']}", file=sys.stderr)
    print(p["log"].read_text(encoding="utf-8", errors="replace")[-1500:], file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["start", "stop", "restart"])
    ap.add_argument("--name", required=True)
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--fresh", action="store_true", help="ricopia il catalogo dal vero")
    a = ap.parse_args()
    if a.cmd == "stop":
        stop(a.name)
        print("fermato")
    else:
        start(a.name, a.port, fresh=a.fresh)
