"""Aggiornamenti di FotoScan dalle Release di GitHub.

Tre passi, ognuno separato:
1. check()          chiede a GitHub l'elenco delle Release e confronta la versione con quella installata.
2. download()       scarica lo zip della Release, lo estrae in <cache>/updates/staging/<versione> e lo verifica.
                    Il programma in uso non viene toccato: l'aggiornamento resta "pronto" (pending.json).
3. apply_pending()  installa l'aggiornamento pronto. Lo chiama il launcher (fotoscan.desktop) PRIMA di avviare
                    il server, quindi nessun file e' in uso. Prima fa una copia di riserva dei file sostituiti;
                    se qualcosa va storto la ripristina e FotoScan parte con la versione precedente.

Rete: le connessioni passano da netguard.download_window(), aperta solo verso github.com e
githubusercontent.com e solo per la durata della richiesta. A GitHub arriva soltanto la richiesta
dell'elenco delle Release e dello zip: nessuna foto, nessun dato del catalogo.

Dati dell'utente mai toccati: config.json, cataloghi .db, cache/, .venv/, .git/ e qualsiasi file fuori
dalle cartelle del programma.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import netguard
from .version import __version__, update_repo

log = logging.getLogger("fotoscan")

API = "https://api.github.com"
USER_AGENT = "FotoScan-updater"
TIMEOUT = 20
CHUNK = 256 * 1024
KEEP_BACKUPS = 2

# Cartelle del programma: sostituite per intero (i file tolti nella nuova versione spariscono,
# altrimenti un vecchio routes_*.py resterebbe caricato).
MANAGED_DIRS = ("fotoscan", "frontend", "tools", "tests", "docs")
# Mai copiati dalla Release e mai cancellati: dati e ambiente dell'utente.
PROTECTED = {".git", ".venv", "venv", "env", "cache", "_privato", "config.json", "node_modules", "__pycache__"}
PROTECTED_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".log")
# File che devono esserci in una Release valida.
REQUIRED = ("fotoscan/__init__.py", "fotoscan/version.py", "fotoscan/server/app.py", "frontend/index.html")

_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)


class UpdateError(RuntimeError):
    """Errore con un messaggio in italiano da mostrare all'utente."""


# ------------------------------------------------------------------ versioni
def parse_version(text: str) -> tuple:
    """'v2.1.0' -> (2, 1, 0, 1, ()); '2.1.0-beta.2' -> (2, 1, 0, 0, (2,)). Una pre-release precede la finale."""
    s = (text or "").strip().lstrip("vV")
    main, _, pre = s.partition("-")
    nums = [int(x) for x in re.findall(r"\d+", main)[:3]]
    nums += [0] * (3 - len(nums))
    pre_nums = tuple(int(x) for x in re.findall(r"\d+", pre))
    return (*nums, 0 if pre else 1, pre_nums)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def read_version(root: Path) -> str | None:
    """Versione scritta in <root>/fotoscan/version.py (senza importarlo)."""
    try:
        m = _VERSION_RE.search((root / "fotoscan" / "version.py").read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def is_git_checkout(root: Path) -> bool:
    """Installazione da sorgente (git clone): si aggiorna con git pull, non con gli zip."""
    return (root / ".git").exists()


# ------------------------------------------------------------------ percorsi e stato
def updates_dir(cache_dir: str | Path) -> Path:
    return Path(cache_dir) / "updates"


def _read_json(p: Path) -> dict | None:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def read_state(cache_dir) -> dict:
    """Ultimo controllo: {checked_at, current, latest, available, error}."""
    return _read_json(updates_dir(cache_dir) / "state.json") or {}


def read_pending(cache_dir) -> dict | None:
    """Aggiornamento scaricato e pronto: {version, dir, notes, approved, downloaded_at}."""
    p = _read_json(updates_dir(cache_dir) / "pending.json")
    if p and p.get("dir") and Path(p["dir"]).is_dir():
        return p
    return None


def read_last_install(cache_dir) -> dict | None:
    return _read_json(updates_dir(cache_dir) / "installed.json")


def set_approved(cache_dir, approved: bool = True) -> dict:
    p = read_pending(cache_dir)
    if not p:
        raise UpdateError("Nessun aggiornamento scaricato da installare.")
    p["approved"] = bool(approved)
    _write_json(updates_dir(cache_dir) / "pending.json", p)
    return p


def discard_pending(cache_dir) -> None:
    d = updates_dir(cache_dir)
    p = _read_json(d / "pending.json")
    (d / "pending.json").unlink(missing_ok=True)
    if p and p.get("dir"):
        shutil.rmtree(p["dir"], ignore_errors=True)


# ------------------------------------------------------------------ 1. controllo
def _open(url: str, accept: str = "application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def _friendly(e: Exception) -> str:
    if isinstance(e, UpdateError):
        return str(e)
    code = getattr(e, "code", None)
    if code == 404:
        return "Repository degli aggiornamenti non trovato su GitHub."
    if code == 403:
        return "GitHub ha rifiutato la richiesta (troppi controlli in poco tempo). Riprova tra un'ora."
    if isinstance(e, (TimeoutError, OSError)):
        return "GitHub non raggiungibile. Controlla la connessione a internet."
    return f"Controllo non riuscito: {type(e).__name__}: {e}"


def _pick_release(releases: list[dict], prerelease: bool) -> dict | None:
    best = None
    for r in releases:
        if not isinstance(r, dict) or r.get("draft") or not r.get("tag_name"):
            continue
        if r.get("prerelease") and not prerelease:
            continue
        if best is None or parse_version(r["tag_name"]) > parse_version(best["tag_name"]):
            best = r
    return best


def _release_info(r: dict) -> dict:
    return {
        "version": r["tag_name"].lstrip("vV"),
        "tag": r["tag_name"],
        "name": r.get("name") or r["tag_name"],
        "notes": (r.get("body") or "")[:20000],
        "published_at": r.get("published_at"),
        "prerelease": bool(r.get("prerelease")),
        "html_url": r.get("html_url"),
        "zip_url": r.get("zipball_url"),
    }


def fetch_releases(repo: str) -> list[dict]:
    """Elenco delle Release (unica richiesta del controllo). Sostituibile nei test."""
    with netguard.download_window("controllo aggiornamenti di FotoScan"):
        with _open(f"{API}/repos/{repo}/releases?per_page=20") as r:
            return json.loads(r.read().decode("utf-8"))


def check(cache_dir, prerelease: bool = False, current: str = __version__) -> dict:
    """Chiede a GitHub l'ultima Release e salva l'esito in state.json. Non solleva eccezioni."""
    repo = update_repo()
    state: dict[str, Any] = {"checked_at": datetime.now().isoformat(timespec="seconds"), "current": current,
                             "latest": None, "available": False, "error": None}
    if not repo:
        state["error"] = "Aggiornamenti non configurati in questa copia di FotoScan."
    else:
        try:
            rel = _pick_release(fetch_releases(repo), prerelease)
            if rel:
                info = _release_info(rel)
                state["latest"] = info
                state["available"] = is_newer(info["version"], current)
        except Exception as e:  # noqa: BLE001 - rete, JSON, rate limit: diventano un messaggio
            log.warning("controllo aggiornamenti: %s", e)
            state["error"] = _friendly(e)
    _write_json(updates_dir(cache_dir) / "state.json", state)
    return state


# ------------------------------------------------------------------ 2. download
def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Estrae togliendo la cartella radice dello zip di GitHub (<repo>-<sha>/), senza uscire da dest."""
    dest = dest.resolve()
    for m in zf.infolist():
        parts = PurePosixPath(m.filename.replace("\\", "/")).parts
        if len(parts) < 2 or m.is_dir():
            continue
        rel = parts[1:]
        if any(p in ("..", "") or ":" in p for p in rel) or PurePosixPath(m.filename).is_absolute():
            raise UpdateError(f"Pacchetto non valido: percorso sospetto {m.filename!r}.")
        target = dest.joinpath(*rel)
        if not str(target.resolve()).startswith(str(dest)):
            raise UpdateError(f"Pacchetto non valido: percorso sospetto {m.filename!r}.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(m) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def fetch_zip(url: str, dest: Path, progress: Callable[[int, int], None] | None = None,
              cancelled: Callable[[], bool] | None = None) -> None:
    """Scarica lo zip della Release in dest. Sostituibile nei test."""
    tmp = dest.with_name(dest.name + ".part")
    with netguard.download_window("download dell'aggiornamento di FotoScan"):
        with _open(url, accept="application/octet-stream") as r, open(tmp, "wb") as out:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancelled and cancelled():
                    raise UpdateError("Download annullato.")
                buf = r.read(CHUNK)
                if not buf:
                    break
                out.write(buf)
                done += len(buf)
                if progress:
                    progress(done, total)
    os.replace(tmp, dest)


def download(cache_dir, info: dict, approved: bool = False,
             progress: Callable[[int, int], None] | None = None, cancelled: Callable[[], bool] | None = None) -> dict:
    """Scarica, estrae e verifica la Release `info` (da check()). Ritorna il nuovo pending."""
    if not info or not info.get("zip_url"):
        raise UpdateError("Nessun aggiornamento da scaricare.")
    d = updates_dir(cache_dir)
    (d / "download").mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", info["version"])
    zpath = d / "download" / f"fotoscan-{safe}.zip"
    staging = d / "staging" / safe
    try:
        fetch_zip(info["zip_url"], zpath, progress, cancelled)
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            with zipfile.ZipFile(zpath) as zf:
                _safe_extract(zf, staging)
        except zipfile.BadZipFile as e:
            raise UpdateError("Il file scaricato è danneggiato. Riprova.") from e
        missing = [p for p in REQUIRED if not (staging / p).is_file()]
        if missing:
            raise UpdateError("Il pacchetto scaricato non contiene FotoScan (" + ", ".join(missing) + ").")
        staged_version = read_version(staging)
        if not staged_version or not is_newer(staged_version, read_version(Path(__file__).resolve().parent.parent) or __version__):
            raise UpdateError(f"Il pacchetto scaricato ha la versione {staged_version or '?'}, non più recente di quella installata.")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        zpath.unlink(missing_ok=True)
    pending = {"version": staged_version, "tag": info.get("tag"), "name": info.get("name"), "notes": info.get("notes", ""),
               "dir": str(staging), "approved": bool(approved), "downloaded_at": datetime.now().isoformat(timespec="seconds")}
    _write_json(d / "pending.json", pending)
    return pending


# ------------------------------------------------------------------ 3. installazione
def _protected(name: str) -> bool:
    return name in PROTECTED or name.lower().endswith(PROTECTED_SUFFIXES)


def _plan(staged: Path) -> list[str]:
    """Voci di primo livello da installare (relative alla radice)."""
    return sorted(p.name for p in staged.iterdir() if not _protected(p.name))


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _replace_dir(src: Path, dst: Path) -> None:
    """dst diventa uguale a src; le cartelle __pycache__ si ricreano da sole."""
    if dst.exists():
        shutil.rmtree(dst)
    _copy_tree(src, dst)


def _restore(root: Path, backup: Path, names: list[str]) -> None:
    for name in names:
        target = root / name
        saved = backup / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink(missing_ok=True)
        if saved.is_dir():
            _copy_tree(saved, target)
        elif saved.exists():
            shutil.copy2(saved, target)


def _prune_backups(bdir: Path) -> None:
    olds = sorted((p for p in bdir.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in olds[KEEP_BACKUPS:]:
        shutil.rmtree(p, ignore_errors=True)


def apply_pending(root: Path, cache_dir, force: bool = False, logger: Callable[[str], None] | None = None) -> dict | None:
    """Installa l'aggiornamento pronto se approvato (o force). Ritorna {from, to, requirements_changed, backup}
    oppure None se non c'e' nulla da fare. Se fallisce ripristina i file e solleva UpdateError."""
    say = logger or (lambda m: log.info(m))
    pending = read_pending(cache_dir)
    if not pending or not (pending.get("approved") or force):
        return None
    if is_git_checkout(root):
        say("aggiornamento non installato: questa copia è un clone git (usa git pull)")
        return None
    staged = Path(pending["dir"])
    old_version = read_version(root) or "?"
    new_version = read_version(staged) or pending.get("version") or "?"
    names = _plan(staged)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = updates_dir(cache_dir) / "backup"
    backup = bdir / f"{old_version}-{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    old_req = (root / "requirements.txt").read_bytes() if (root / "requirements.txt").exists() else b""
    say(f"installazione aggiornamento {old_version} -> {new_version}")

    # copia di riserva di tutto cio' che verra' sostituito
    existing = [n for n in names if (root / n).exists()]
    for name in existing:
        if (root / name).is_dir():
            _copy_tree(root / name, backup / name)
        else:
            shutil.copy2(root / name, backup / name)
    try:
        for name in names:
            src, dst = staged / name, root / name
            if src.is_dir():
                if name in MANAGED_DIRS:
                    _replace_dir(src, dst)
                else:
                    _copy_tree(src, dst)
            else:
                if dst.is_dir():
                    shutil.rmtree(dst)
                shutil.copy2(src, dst)
    except Exception as e:
        say(f"installazione fallita ({type(e).__name__}: {e}): ripristino della versione {old_version}")
        _restore(root, backup, names)  # le voci nuove, senza copia di riserva, vengono solo tolte
        raise UpdateError(f"Installazione dell'aggiornamento non riuscita: {e}. FotoScan resta alla versione {old_version}.") from e

    new_req = (root / "requirements.txt").read_bytes() if (root / "requirements.txt").exists() else b""
    result = {"from": old_version, "to": new_version, "requirements_changed": new_req != old_req,
              "backup": str(backup), "at": datetime.now().isoformat(timespec="seconds"), "notes": pending.get("notes", "")}
    _write_json(updates_dir(cache_dir) / "installed.json", result)
    discard_pending(cache_dir)
    try:
        _prune_backups(bdir)
    except OSError:
        pass
    say(f"aggiornamento installato: ora alla versione {new_version}")
    return result


def install_requirements(root: Path, python: str, timeout: int = 1800) -> tuple[bool, str]:
    """pip install -r requirements.txt (le dipendenze sono cambiate). pip e' un processo separato:
    non passa dal blocco di rete di FotoScan e scarica da PyPI."""
    try:
        proc = subprocess.run([python, "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"],
                              cwd=str(root), capture_output=True, text=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out[-4000:]
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
