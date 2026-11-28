"""Disinstallazione di PhotoScan: parte condivisa dall'app e Linux.

Windows: il vero disinstallatore e' PhotoScan-Setup.exe --uninstall (launcher/uninstall_photoscan.py), copiato dal
Setup in <dati>/uninstall e registrato in «App installate». L'app lo lancia con launch_windows_uninstaller() e si chiude.
Linux (AppImage): l'app non puo' cancellare i propri file mentre gira. request() scrive una richiesta; a server spento
fotoscan.desktop chiama finish_pending() che toglie l'integrazione nel menu (.desktop e icona) e, a scelta, i dati.

Regole che valgono ovunque:
- mai le cartelle delle foto dell'utente (sorgente e destinazione) e mai nulla fuori dalla cartella dei dati di PhotoScan;
- i dati (catalogo, cache, modelli, impostazioni) si conservano se l'utente non chiede il contrario.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REQUEST_FILE = "uninstall.json"
WIN_UNINSTALLER = Path("uninstall") / "PhotoScan-Uninstall.exe"


def is_data_root(root: Path) -> bool:
    """Una cartella dei dati di PhotoScan e' sempre <qualcosa>/PhotoScan: mai la home o una radice di disco."""
    try:
        root = Path(root).resolve()
    except OSError:
        return False
    return root.name.lower() == "photoscan" and len(root.parts) >= 3 and root != Path.home().resolve()


def dir_size(path: Path, max_seconds: float = 5.0) -> int:
    """Byte occupati da una cartella (o da un file). Si ferma dopo max_seconds: il valore e' allora una stima per difetto."""
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    total, end = 0, time.time() + max_seconds
    stack = [path]
    while stack and time.time() < end:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def request_path(cache_dir) -> Path:
    return Path(cache_dir) / REQUEST_FILE


def request(cache_dir, delete_data: bool = False, delete_appimage: bool = False) -> None:
    """Linux: chiede la rimozione alla chiusura dell'app."""
    p = request_path(cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"delete_data": bool(delete_data), "delete_appimage": bool(delete_appimage),
                             "at": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")


def read_request(cache_dir) -> dict | None:
    try:
        data = json.loads(request_path(cache_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _xdg_data() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def linux_cleanup(data_root: Path, delete_data: bool = False, delete_appimage: bool = False,
                  appimage: str | None = None, log=None) -> list[str]:
    """Toglie l'integrazione nel menu e, a scelta, dati e file AppImage. Ritorna cio' che e' stato rimosso."""
    say = log or (lambda m: None)
    removed: list[str] = []
    data = _xdg_data()
    for f in (data / "applications" / "photoscan.desktop", data / "icons" / "photoscan.png"):
        try:
            if f.exists():
                f.unlink()
                removed.append(str(f))
        except OSError as e:
            say(f"non rimosso {f}: {e}")
    if delete_data:
        if is_data_root(data_root) and Path(data_root).exists():
            shutil.rmtree(data_root, ignore_errors=True)
            removed.append(str(data_root))
        else:
            say(f"cartella dati non riconosciuta, non rimossa: {data_root}")
    if delete_appimage and appimage and Path(appimage).is_file():
        # il file e' in uso finche' il processo vive: lo cancella una shell staccata, due secondi dopo
        subprocess.Popen(["sh", "-c", 'sleep 2; rm -f -- "$0"', appimage], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        removed.append(appimage)
    return removed


def finish_pending(cache_dir, data_root: Path, log=None) -> list[str] | None:
    """Da chiamare a server spento (fotoscan.desktop): esegue la disinstallazione richiesta. None = nessuna richiesta."""
    req = read_request(cache_dir)
    if not req:
        return None
    if sys.platform == "win32":
        request_path(cache_dir).unlink(missing_ok=True)
        return None
    removed = linux_cleanup(data_root, bool(req.get("delete_data")), bool(req.get("delete_appimage")),
                            os.environ.get("APPIMAGE"), log)
    request_path(cache_dir).unlink(missing_ok=True)
    return removed


def windows_uninstaller(data_root: Path) -> Path | None:
    p = Path(data_root) / WIN_UNINSTALLER
    return p if p.is_file() else None


def launch_windows_uninstaller(data_root: Path, delete_data: bool) -> bool:
    """Avvia il disinstallatore (processo staccato). False = PhotoScan non e' stato installato con il Setup."""
    exe = windows_uninstaller(data_root)
    if exe is None:
        return False
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(exe), "--uninstall", "--delete-data" if delete_data else "--keep-data"],
                     cwd=os.environ.get("TEMP") or None, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    return True
