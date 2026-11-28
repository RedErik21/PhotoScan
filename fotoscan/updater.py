"""Aggiornamenti di PhotoScan dalle Release di GitHub.

Tre passi, ognuno separato:
1. check()          chiede a GitHub l'elenco delle Release e confronta la versione con quella installata.
2. download()       scarica lo zip della Release, lo estrae in <cache>/updates/staging/<versione> e lo verifica.
                    Il programma in uso non viene toccato: l'aggiornamento resta "pronto" (pending.json).
3. apply_pending()  installa l'aggiornamento pronto. Lo chiama il launcher (fotoscan.desktop) PRIMA di avviare
                    il server, quindi nessun file e' in uso. Prima fa una copia di riserva dei file sostituiti;
                    se qualcosa va storto la ripristina e PhotoScan parte con la versione precedente.

Rete: le connessioni passano da netguard.download_window(), aperta solo verso github.com e
githubusercontent.com e solo per la durata della richiesta. A GitHub arriva soltanto la richiesta
dell'elenco delle Release e dello zip: nessuna foto, nessun dato del catalogo.

Dati dell'utente mai toccati: config.json, cataloghi .db, cache/, .venv/, .git/ e qualsiasi file fuori
dalle cartelle del programma.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import netguard, runtime
from .version import __version__, update_repo

log = logging.getLogger("fotoscan")

API = "https://api.github.com"
USER_AGENT = "PhotoScan-updater"
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
    """'v2.1.0' -> ((2, 1, 0), 1, ()); '2.1.0-beta.2' -> ((2, 1, 0), 0, ((1, 'beta'), (0, 2))).
    Una pre-release precede la finale; le etichette si confrontano come testo (alpha < beta < rc), i numeri come numeri.
    Una versione con quattro numeri (0.1.10.1) segue 0.1.10."""
    s = (text or "").strip().lstrip("vV").split("+", 1)[0]
    main, _, pre = s.partition("-")
    nums = [int(x) for x in re.findall(r"\d+", main)]
    nums += [0] * (3 - len(nums))
    while len(nums) > 3 and nums[-1] == 0:
        nums.pop()
    pre_key = tuple((0, int(tok)) if tok.isdigit() else (1, tok.lower()) for tok in re.findall(r"\d+|[A-Za-z]+", pre))
    return (tuple(nums), 0 if pre else 1, pre_key)


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
        data = json.loads(p.read_text(encoding="utf-8-sig"))  # tollera il BOM di PowerShell
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
    if p and p.get("dir") and Path(p["dir"]).exists():   # cartella (exe Windows) o file (AppImage Linux)
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
        target = Path(p["dir"])
        if target.is_file():          # AppImage scaricato: e' un file, rmtree non lo toglierebbe
            target.unlink(missing_ok=True)
        else:
            shutil.rmtree(target, ignore_errors=True)
    clean_downloads(cache_dir)


def clean_downloads(cache_dir) -> None:
    """Toglie gli avanzi di download interrotti (.part e zip a meta'): possono pesare alcuni GB."""
    dl = updates_dir(cache_dir) / "download"
    if dl.is_dir():
        for f in dl.iterdir():
            if f.is_file() and (f.suffix == ".part" or f.suffix == ".zip"):
                try:
                    f.unlink()
                except OSError:
                    pass    # download ancora in corso (file aperto): lo toglie chi lo sta scrivendo


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


_EXE_ASSET_RE = re.compile(r"^PhotoScan-(?P<v>\d[\w.\-]*?)-windows\.zip(?:\.(?P<n>\d{3}))?$")
_EXE_CPU_ASSET_RE = re.compile(r"^PhotoScan-(?P<v>\d[\w.\-]*?)-windows-cpu\.zip(?:\.(?P<n>\d{3}))?$")
# Linux: un unico AppImage, PhotoScan-<v>-x86_64.AppImage (si sostituisce il file in uso, vedi start_exe_update)
_APPIMAGE_ASSET_RE = re.compile(r"^PhotoScan-(?P<v>\d[\w.\-]*?)-x86_64\.AppImage$")
# App leggera (senza PyTorch) e runtime a parte. I nomi NON corrispondono alle espressioni sopra (e a quelle degli
# updater <= 0.1.10 e del Setup vecchio): "PhotoScan-" seguito da una lettera, non da una cifra.
_APP_ASSET_RE = re.compile(r"^PhotoScan-app-(?P<v>\d[\w.\-]*?)-windows\.zip(?:\.(?P<n>\d{3}))?$")
_RUNTIME_ASSET_RE = re.compile(r"^PhotoScan-runtime-(?P<rv>[\w.]+?)-windows(?P<cpu>-cpu)?\.zip(?:\.(?P<n>\d{3}))?$")
_SETUP_ASSET_RE = re.compile(r"^PhotoScan-Setup(?:-(?P<v>\d[\w.\-]*?))?\.exe$")
SUMS_NAME = "SHA256SUMS"
MANIFEST_NAME = "release-manifest.json"


def _detect_windows_runtime() -> str:
    """Determina se l'installazione Windows corrente usa il pacchetto 'cpu' o 'gpu'."""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        inst = _read_json(Path(local) / "PhotoScan" / "install.json")
        if inst and inst.get("runtime") in ("cpu", "gpu"):
            return inst["runtime"]
    for d in (runtime.bundled_dir(),):          # il runtime dentro il pacchetto dice di che tipo e'
        m = runtime.read_manifest(d)
        if m and m.get("flavor") in ("cpu", "gpu"):
            return m["flavor"]
    base = runtime.runtimes_dir()
    if base.is_dir():
        for d in sorted(base.iterdir(), reverse=True):
            m = runtime.read_manifest(d) if d.is_dir() else None
            if m and m.get("flavor") in ("cpu", "gpu"):
                return m["flavor"]
    torch_mod = sys.modules.get("torch")
    if torch_mod is not None:
        return "gpu" if getattr(getattr(torch_mod, "version", None), "cuda", None) else "cpu"
    return "gpu"


def _complete(found: list[tuple[int, dict]]) -> list[dict]:
    """Parti in ordine, solo se l'insieme e' completo: un file unico oppure .001 ... .NNN senza buchi.
    Una Release ancora in caricamento (parti mancanti) non deve produrre uno zip troncato."""
    if not found:
        return []
    nums = sorted(n for n, _ in found)
    if nums == [0]:
        return [p for _, p in found]
    if nums != list(range(1, len(nums) + 1)):
        return []
    return [p for _, p in sorted(found, key=lambda t: t[0])]


def _exe_assets(assets: list, version: str, platform: str | None = None, runtime: str | None = None) -> list[dict]:
    """Asset dell'eseguibile COMPLETO della versione `version`: su Windows PhotoScan-<v>-windows[-cpu].zip (o le parti
    .001, .002...), su Linux PhotoScan-<v>-x86_64.AppImage. In ordine di parte; [] se le parti sono incomplete."""
    plat = platform or sys.platform
    if plat == "win32":
        rt = runtime or _detect_windows_runtime()
        rx = _EXE_CPU_ASSET_RE if rt == "cpu" else _EXE_ASSET_RE
    else:
        rx = _APPIMAGE_ASSET_RE
    found = []
    for a in assets or []:
        if not isinstance(a, dict):
            continue
        m = rx.match(a.get("name") or "")
        if m and m.group("v") == version and a.get("browser_download_url"):
            part_num = int(m.group("n") or 0) if "n" in rx.groupindex else 0
            found.append((part_num, {"name": a["name"], "url": a["browser_download_url"],
                                     "size": int(a.get("size") or 0)}))
    return _complete(found)


def _app_assets(assets: list, version: str) -> list[dict]:
    """App leggera di Windows (senza il runtime): PhotoScan-app-<v>-windows.zip[.NNN]."""
    found = []
    for a in assets or []:
        m = _APP_ASSET_RE.match((a or {}).get("name") or "") if isinstance(a, dict) else None
        if m and m.group("v") == version and a.get("browser_download_url"):
            found.append((int(m.group("n") or 0), {"name": a["name"], "url": a["browser_download_url"],
                                                   "size": int(a.get("size") or 0)}))
    return _complete(found)


def _asset_url(release: dict, name: str) -> str | None:
    for a in release.get("assets") or []:
        if isinstance(a, dict) and a.get("name") == name and a.get("browser_download_url"):
            return a["browser_download_url"]
    return None


def _runtime_index(releases: list[dict]) -> dict:
    """{versione_runtime: {gpu|cpu: {parts, sums_url, tag}}} con i runtime pubblicati in QUALUNQUE Release
    (il runtime si ripubblica solo quando cambia: quello che serve puo' stare in una Release piu' vecchia)."""
    out: dict[str, dict] = {}
    for r in releases or []:
        if not isinstance(r, dict) or r.get("draft"):
            continue
        groups: dict[tuple[str, str], list] = {}
        for a in r.get("assets") or []:
            m = _RUNTIME_ASSET_RE.match((a or {}).get("name") or "") if isinstance(a, dict) else None
            if m and a.get("browser_download_url"):
                key = (m.group("rv"), "cpu" if m.group("cpu") else "gpu")
                groups.setdefault(key, []).append((int(m.group("n") or 0), {
                    "name": a["name"], "url": a["browser_download_url"], "size": int(a.get("size") or 0)}))
        for (rv, flavor), found in groups.items():
            parts = _complete(found)
            if parts and flavor not in out.get(rv, {}):
                out.setdefault(rv, {})[flavor] = {"parts": parts, "sums_url": _asset_url(r, SUMS_NAME),
                                                  "tag": r.get("tag_name")}
    return out


def _release_info(r: dict, releases: list[dict] | None = None) -> dict:
    version = r["tag_name"].lstrip("vV")
    return {
        "version": version,
        "tag": r["tag_name"],
        "name": r.get("name") or r["tag_name"],
        "notes": (r.get("body") or "")[:20000],
        "published_at": r.get("published_at"),
        "prerelease": bool(r.get("prerelease")),
        "html_url": r.get("html_url"),
        "zip_url": r.get("zipball_url"),
        "exe_assets": _exe_assets(r.get("assets"), version),
        "app_assets": _app_assets(r.get("assets"), version) if sys.platform == "win32" else [],
        "manifest_url": _asset_url(r, MANIFEST_NAME),
        "sums_url": _asset_url(r, SUMS_NAME),
        "setup_url": _setup_url(r, version),
        "runtime_assets": _runtime_index(releases if releases is not None else [r]),
    }


def _setup_url(r: dict, version: str) -> str | None:
    """Installer (PhotoScan-Setup.exe o PhotoScan-Setup-<v>.exe) di quella Release, se pubblicato."""
    best = None
    for a in r.get("assets") or []:
        m = _SETUP_ASSET_RE.match((a or {}).get("name") or "") if isinstance(a, dict) else None
        if m and a.get("browser_download_url") and (m.group("v") in (None, version)):
            if best is None or m.group("v"):
                best = a["browser_download_url"]
    return best


def list_releases(cache_dir, prerelease: bool = False, limit: int = 20) -> list[dict]:
    """Release scaricabili (piu' recenti per prime), per «Altre versioni». Non scrive nulla."""
    repo = update_repo()
    if not repo:
        raise UpdateError("Aggiornamenti non configurati in questa copia di PhotoScan.")
    releases = fetch_releases(repo)
    out = []
    for r in releases:
        if not isinstance(r, dict) or r.get("draft") or not r.get("tag_name"):
            continue
        if r.get("prerelease") and not prerelease:
            continue
        info = _release_info(r, releases)
        out.append({k: info[k] for k in ("version", "tag", "name", "notes", "published_at", "prerelease", "html_url",
                                         "setup_url")} | {"installable": bool(info["exe_assets"] or info["app_assets"] or
                                                                              sys.platform != "win32")})
    out.sort(key=lambda x: parse_version(x["version"]), reverse=True)
    return out[:limit]


def release_info(version: str, prerelease: bool = True) -> dict:
    """Informazioni di scaricamento di UNA versione precisa (anche piu' vecchia della installata)."""
    repo = update_repo()
    if not repo:
        raise UpdateError("Aggiornamenti non configurati in questa copia di PhotoScan.")
    releases = fetch_releases(repo)
    want = parse_version(version)
    for r in releases:
        if isinstance(r, dict) and not r.get("draft") and r.get("tag_name") and parse_version(r["tag_name"]) == want:
            if r.get("prerelease") and not prerelease:
                continue
            return _release_info(r, releases)
    raise UpdateError(f"La versione {version} non è tra le Release di GitHub.")


def fetch_releases(repo: str) -> list[dict]:
    """Elenco delle Release (unica richiesta del controllo). Sostituibile nei test."""
    with netguard.download_window("controllo aggiornamenti di PhotoScan"):
        with _open(f"{API}/repos/{repo}/releases?per_page=30") as r:
            return json.loads(r.read().decode("utf-8"))


def check(cache_dir, prerelease: bool = False, current: str = __version__) -> dict:
    """Chiede a GitHub l'ultima Release e salva l'esito in state.json. Non solleva eccezioni."""
    repo = update_repo()
    state: dict[str, Any] = {"checked_at": datetime.now().isoformat(timespec="seconds"), "current": current,
                             "latest": None, "available": False, "error": None}
    if not repo:
        state["error"] = "Aggiornamenti non configurati in questa copia di PhotoScan."
    else:
        try:
            releases = fetch_releases(repo)
            rel = _pick_release(releases, prerelease)
            if rel:
                info = _release_info(rel, releases)
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
    try:
        with netguard.download_window("download dell'aggiornamento di PhotoScan"):
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
                if total and done != total:
                    raise UpdateError("Il download si è interrotto prima della fine. Riprova.")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, dest)


RETRIES = 4


def _fetch_one(p: dict, out, done: int, total: int, progress, cancelled) -> int:
    """Scarica UNA parte in coda a `out`, con ripresa (Range) dopo un'interruzione, controllo della dimensione
    dichiarata e dell'impronta SHA-256 (se nota). Ritorna i byte totali scaricati finora."""
    start_pos, start_done = out.tell(), done
    hasher, got, last_err = hashlib.sha256(), 0, None
    for attempt in range(RETRIES):
        if cancelled and cancelled():
            raise UpdateError("Download annullato.")
        try:
            req = urllib.request.Request(p["url"], headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream",
                                                            **({"Range": f"bytes={got}-"} if got else {})})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                if got and getattr(r, "status", 200) != 206:   # il server ignora Range: la parte riparte da capo
                    out.seek(start_pos)
                    out.truncate()
                    hasher, got = hashlib.sha256(), 0
                expected = int(r.headers.get("Content-Length") or 0)
                read_now = 0
                while True:
                    if cancelled and cancelled():
                        raise UpdateError("Download annullato.")
                    buf = r.read(CHUNK)
                    if not buf:
                        break
                    out.write(buf)
                    hasher.update(buf)
                    got += len(buf)
                    read_now += len(buf)
                    done = start_done + got
                    if progress:
                        progress(done, total)
                if expected and read_now != expected:
                    raise OSError(f"download troncato ({read_now} di {expected} byte)")
            last_err = None
            break
        except UpdateError:
            raise
        except OSError as e:           # include HTTPError, timeout, connessione interrotta
            code = getattr(e, "code", None)
            if code and 400 <= code < 500 and code not in (408, 429):
                raise UpdateError(f"GitHub ha risposto {code} per {p['name']}.") from e
            last_err = e
            time.sleep(min(2 ** attempt, 8))
    if last_err is not None:
        raise UpdateError(f"Download di {p['name']} interrotto: {last_err}. Controlla la connessione e riprova.")
    size = int(p.get("size") or 0)
    if size and got != size:
        raise UpdateError(f"La parte {p['name']} è incompleta ({got} di {size} byte). Riprova.")
    want = (p.get("sha256") or "").lower()
    if want and hasher.hexdigest() != want:
        raise UpdateError(f"La parte {p['name']} è danneggiata: l'impronta SHA-256 non corrisponde. Riprova.")
    return done


def fetch_parts(parts: list[dict], dest: Path, progress: Callable[[int, int], None] | None = None,
                cancelled: Callable[[], bool] | None = None) -> None:
    """Scarica in sequenza le parti dell'eseguibile e le unisce in un solo zip (dest)."""
    tmp = dest.with_name(dest.name + ".part")
    total = sum(p.get("size") or 0 for p in parts)
    done = 0
    try:
        with netguard.download_window("download dell'aggiornamento di PhotoScan"):
            with open(tmp, "wb") as out:
                for p in parts:
                    done = _fetch_one(p, out, done, total, progress, cancelled)
    except BaseException:
        tmp.unlink(missing_ok=True)       # niente avanzi da GB dopo un errore o un annullamento
        raise
    os.replace(tmp, dest)


# ---- integrita': SHA256SUMS pubblicato con la Release
# Punto d'aggancio per una FIRMA vera (es. minisign/Ed25519): SHA256SUMS e' un file di testo scaricato dallo stesso
# posto dei pacchetti, quindi protegge da download corrotti e parti scambiate ma NON da una Release manomessa.
# Per la firma: pubblicare SHA256SUMS.sig e verificarlo qui, in _load_sums(), con una chiave pubblica incorporata
# in PhotoScan (la chiave privata resta a chi pubblica, mai nel repository).
def parse_sums(text: str) -> dict[str, str]:
    out = {}
    for line in (text or "").splitlines():
        m = re.match(r"^([0-9A-Fa-f]{64})\s+\*?(.+?)\s*$", line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def _fetch_text(url: str) -> str:
    """Piccolo file di testo di una Release (SHA256SUMS, release-manifest.json). Sostituibile nei test."""
    with netguard.download_window("controllo aggiornamenti di PhotoScan"):
        with _open(url, accept="application/octet-stream") as r:
            return r.read(2_000_000).decode("utf-8-sig")


def _load_sums(info: dict) -> dict[str, str] | None:
    """Impronte della Release. None = la Release non ha SHA256SUMS (versioni vecchie): si prosegue con un avviso."""
    url = (info or {}).get("sums_url")
    if not url:
        log.warning("la Release non pubblica %s: integrità dei file non verificabile", SUMS_NAME)
        return None
    try:
        sums = parse_sums(_fetch_text(url))
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"Non riesco a scaricare {SUMS_NAME} per verificare i file: {_friendly(e)}") from e
    if not sums:
        raise UpdateError(f"{SUMS_NAME} della Release è vuoto o illeggibile.")
    return sums


def _attach_sums(parts: list[dict], sums: dict[str, str] | None) -> list[dict]:
    parts = [dict(p) for p in parts]
    if sums is None:
        return parts
    for p in parts:
        h = sums.get(p["name"])
        if not h:
            raise UpdateError(f"{p['name']} non è nell'elenco {SUMS_NAME} della Release: file non verificabile.")
        p["sha256"] = h
    return parts


def _check_space(path: Path, need_bytes: int) -> None:
    """Spazio libero sul disco di `path` (la cartella piu' vicina che esiste)."""
    if need_bytes <= 0:
        return
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(str(probe)).free
    if free < need_bytes:
        raise UpdateError(f"Spazio insufficiente sul disco: servono circa {need_bytes / 1e9:.1f} GB, "
                          f"ne restano {free / 1e9:.1f} GB.")


def backup_catalog(cache_dir, db_path: str | Path, from_version: str) -> Path | None:
    """Copia coerente del catalogo prima di un downgrade (il catalogo puo' essere stato migrato da una versione piu'
    nuova): cache/updates/backup-<versione>.db. None se il catalogo non esiste."""
    import sqlite3
    src = Path(db_path)
    if not src.is_file():
        return None
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", from_version)
    dst = updates_dir(cache_dir) / f"backup-{safe}.db"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    conn = sqlite3.connect(str(src), timeout=30)
    try:
        out = sqlite3.connect(str(dst))
        try:
            conn.backup(out)
        finally:
            out.close()
    finally:
        conn.close()
    return dst


def download(cache_dir, info: dict, approved: bool = False,
             progress: Callable[[int, int], None] | None = None, cancelled: Callable[[], bool] | None = None,
             exe: bool = False, allow_older: bool = False) -> dict:
    """Scarica, estrae e verifica la Release `info` (da check()). Ritorna il nuovo pending.
    exe=True (PhotoScan.exe): scarica gli asset Windows della Release, la cartella del programma va in staging
    e l'installazione la fa start_exe_update() alla chiusura. exe=False (sorgente): zip del sorgente.
    allow_older=True: installazione di una versione scelta dall'utente, anche piu' vecchia di quella in uso."""
    if exe:
        return _download_exe(cache_dir, info, approved, progress, cancelled, allow_older)
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
            raise UpdateError("Il pacchetto scaricato non contiene PhotoScan (" + ", ".join(missing) + ").")
        staged_version = read_version(staging)
        if not staged_version or not (allow_older or is_newer(
                staged_version, read_version(Path(__file__).resolve().parent.parent) or __version__)):
            raise UpdateError(f"Il pacchetto scaricato ha la versione {staged_version or '?'}, non più recente di quella installata.")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        zpath.unlink(missing_ok=True)
    pending = {"version": staged_version, "tag": info.get("tag"), "name": info.get("name"), "notes": info.get("notes", ""),
               "dir": str(staging), "approved": bool(approved), "downloaded_at": datetime.now().isoformat(timespec="seconds"),
               "downgrade": not is_newer(staged_version, __version__)}
    _write_json(d / "pending.json", pending)
    return pending


class _LightUnavailable(Exception):
    """L'app leggera non si puo' usare per questa Release (runtime non trovato): si ripiega sul pacchetto completo."""


def _extract_app_zip(zpath: Path, staging: Path) -> None:
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(zpath) as zf:
            _safe_extract(zf, staging)
    except zipfile.BadZipFile as e:
        raise UpdateError("Il file scaricato è danneggiato. Riprova.") from e
    if not (staging / EXE_NAME).is_file() or not (staging / "_internal").is_dir():
        raise UpdateError(f"Il pacchetto scaricato non contiene {EXE_NAME}.")


def _offset(progress, offset: int, grand_total: int):
    if progress is None:
        return None
    return lambda done, total: progress(offset + done, grand_total)


def install_runtime(cache_dir, rv: str, flavor: str, entry: dict, progress=None, cancelled=None) -> Path:
    """Scarica e installa il runtime (rv, flavor) in <dati>/runtime/<rv>-<flavor>: verifica SHA-256, estrazione in una
    cartella di appoggio, controllo del manifesto e dell'impronta dei file, poi rinomina (atomica). Il runtime in uso
    non viene toccato; in caso di errore resta quello di prima."""
    parts = _attach_sums(entry["parts"], _load_sums({"sums_url": entry.get("sums_url")}))
    d = updates_dir(cache_dir)
    (d / "download").mkdir(parents=True, exist_ok=True)
    base = runtime.runtimes_dir()
    base.mkdir(parents=True, exist_ok=True)
    final = runtime.external_dir(rv, flavor)
    staging = base / (".staging-" + final.name)
    zpath = d / "download" / f"runtime-{rv}-{flavor}.zip"
    _check_space(base, int(2.5 * sum(p.get("size") or 0 for p in parts)))
    try:
        fetch_parts(parts, zpath, progress, cancelled)
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            with zipfile.ZipFile(zpath) as zf:
                _safe_extract(zf, staging)
        except zipfile.BadZipFile as e:
            raise UpdateError("Il runtime scaricato è danneggiato. Riprova.") from e
        ok, why = runtime.check_dir(staging, rv)
        manifest = runtime.read_manifest(staging) or {}
        if not ok or manifest.get("flavor") != flavor:
            raise UpdateError(f"Il runtime scaricato non è utilizzabile: {why or 'tipo cpu/gpu diverso'}.")
        if manifest.get("tree_sha256") and runtime.tree_hash(staging / runtime.SITE) != manifest["tree_sha256"]:
            raise UpdateError("Il runtime scaricato è incompleto (file mancanti dopo l'estrazione). Riprova.")
        old = None
        if final.exists():
            old = base / (".old-" + final.name)
            shutil.rmtree(old, ignore_errors=True)
            os.replace(final, old)
        try:
            os.replace(staging, final)
        except OSError:
            if old is not None:
                os.replace(old, final)
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        zpath.unlink(missing_ok=True)
    return final


def ensure_runtime(cache_dir, progress=None, cancelled=None) -> Path:
    """Garantisce il runtime che questa app richiede (lo scarica dalle Release se manca o ha un'altra versione)."""
    rv = runtime.required_version()
    flavor = _detect_windows_runtime()
    found = runtime.installed(rv, flavor)
    if found:
        return found
    repo = update_repo()
    if not repo:
        raise UpdateError("Il componente di calcolo manca e gli aggiornamenti non sono configurati.")
    entry = (_runtime_index(fetch_releases(repo)).get(rv) or {}).get(flavor)
    if not entry:
        raise UpdateError(f"Il componente di calcolo versione {rv} ({flavor}) non è tra le Release di GitHub: "
                          "reinstalla PhotoScan con PhotoScan-Setup.")
    return install_runtime(cache_dir, rv, flavor, entry, progress, cancelled)


def _download_light(cache_dir, info: dict, approved: bool, progress, cancelled, allow_older: bool) -> dict:
    """Windows: app leggera + (solo se cambia) runtime. Aggiornamento normale: poche centinaia di MB."""
    try:
        manifest = json.loads(_fetch_text(info["manifest_url"]))
    except Exception as e:  # noqa: BLE001
        raise _LightUnavailable(f"{MANIFEST_NAME} illeggibile ({e})") from e
    rv = str(manifest.get("runtime_version") or "")
    if not rv:
        raise _LightUnavailable(f"{MANIFEST_NAME} senza runtime_version")
    flavor = _detect_windows_runtime()
    have = runtime.installed(rv, flavor)
    entry = None
    if have is None:
        entry = (info.get("runtime_assets") or {}).get(rv, {}).get(flavor)
        if not entry:
            raise _LightUnavailable(f"runtime {rv} ({flavor}) non pubblicato")
    elif have == runtime.bundled_dir():
        try:
            runtime.adopt_bundled(rv, flavor)   # il runtime dentro il pacchetto sopravvive alla sostituzione della cartella
        except Exception as e:  # noqa: BLE001
            raise _LightUnavailable(f"runtime del pacchetto non riutilizzabile ({e})") from e
    d = updates_dir(cache_dir)
    (d / "download").mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", info["version"])
    app_parts = _attach_sums(info["app_assets"], _load_sums(info))
    rt_total = sum(p.get("size") or 0 for p in entry["parts"]) if entry else 0
    app_total = sum(p.get("size") or 0 for p in app_parts)
    grand = rt_total + app_total
    _check_space(d, int(2.5 * app_total))
    if entry:
        install_runtime(cache_dir, rv, flavor, entry, _offset(progress, 0, grand), cancelled)
    zpath = d / "download" / f"photoscan-app-{safe}.zip"
    staging = d / "staging" / f"exe-{safe}"
    try:
        fetch_parts(app_parts, zpath, _offset(progress, rt_total, grand), cancelled)
        _extract_app_zip(zpath, staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        zpath.unlink(missing_ok=True)
    pending = {"version": info["version"], "tag": info.get("tag"), "name": info.get("name"),
               "notes": info.get("notes", ""), "dir": str(staging), "kind": "exe", "approved": bool(approved),
               "runtime_version": rv, "runtime_flavor": flavor, "light": True,
               "downgrade": not is_newer(info["version"], __version__),
               "downloaded_at": datetime.now().isoformat(timespec="seconds")}
    _write_json(d / "pending.json", pending)
    return pending


def _download_exe(cache_dir, info: dict, approved: bool, progress, cancelled, allow_older: bool = False) -> dict:
    d = updates_dir(cache_dir)
    (d / "download").mkdir(parents=True, exist_ok=True)
    clean_downloads(cache_dir)                     # avanzi di un download interrotto
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", info["version"])
    if sys.platform == "win32" and (info or {}).get("app_assets") and info.get("manifest_url"):
        try:
            return _download_light(cache_dir, info, approved, progress, cancelled, allow_older)
        except _LightUnavailable as e:
            log.warning("app leggera non utilizzabile (%s): uso il pacchetto completo", e)
            if not info.get("exe_assets"):
                raise UpdateError(f"Questa versione non si può installare dall'app: {e}. "
                                  "Scarica PhotoScan-Setup dalla pagina della Release.") from e
    parts = (info or {}).get("exe_assets") or []
    if not parts:
        raise UpdateError("Questa versione non include l'eseguibile per questo sistema: scaricala dalla pagina Release.")
    parts = _attach_sums(parts, _load_sums(info))
    _check_space(d, int(2.5 * sum(p.get("size") or 0 for p in parts)))
    if sys.platform != "win32":
        # Linux: l'AppImage scaricato e' gia' il programma completo: nessuna estrazione
        app = d / "download" / f"PhotoScan-{safe}-x86_64.AppImage"
        try:
            fetch_parts(parts, app, progress, cancelled)
            expected = sum(int(p.get("size") or 0) for p in parts)
            if expected and app.stat().st_size != expected:
                raise UpdateError("Il file scaricato è incompleto. Riprova.")
            app.chmod(0o755)
        except Exception:
            app.unlink(missing_ok=True)
            raise
        pending = {"version": info["version"], "tag": info.get("tag"), "name": info.get("name"),
                   "notes": info.get("notes", ""), "dir": str(app), "kind": "appimage", "approved": bool(approved),
                   "downgrade": not is_newer(info["version"], __version__),
                   "downloaded_at": datetime.now().isoformat(timespec="seconds")}
        _write_json(d / "pending.json", pending)
        return pending
    zpath = d / "download" / f"photoscan-exe-{safe}.zip"
    staging = d / "staging" / f"exe-{safe}"
    try:
        fetch_parts(parts, zpath, progress, cancelled)
        _extract_app_zip(zpath, staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        zpath.unlink(missing_ok=True)
    pending = {"version": info["version"], "tag": info.get("tag"), "name": info.get("name"),
               "notes": info.get("notes", ""), "dir": str(staging), "kind": "exe", "approved": bool(approved),
               "downgrade": not is_newer(info["version"], __version__),
               "downloaded_at": datetime.now().isoformat(timespec="seconds")}
    _write_json(d / "pending.json", pending)
    return pending


# ------------------------------------------------------------------ 2b. installazione dell'eseguibile
EXE_NAME = "PhotoScan.exe"

# Script PowerShell lanciato dal launcher alla chiusura di PhotoScan (PhotoScan.exe non puo' sostituire se stesso).
# Aspetta che il programma esca, sposta la cartella vecchia in backup, mette la nuova e riapre PhotoScan.
# Se qualcosa va storto ripristina la vecchia. Scritto in UTF-8 con BOM per PowerShell 5.1.
EXE_HELPER = r"""param(
  [string]$AppDir, [string]$Staged, [string]$Backup, [string]$ExeName, [string]$ProcessName = 'PhotoScan',
  [string]$Version, [string]$FromVersion, [string]$PendingFile, [string]$InstalledFile, [string]$LogFile,
  [switch]$NoLaunch
)
$ErrorActionPreference = 'Stop'
# Non tenere aperta la cartella del programma: un processo con la cartella come directory corrente blocca lo spostamento.
Set-Location -LiteralPath $env:TEMP
$utf8 = New-Object Text.UTF8Encoding $false
$failureFile = Join-Path (Split-Path -Parent $InstalledFile) 'last_failure.json'
function Log($m) { Add-Content -LiteralPath $LogFile -Value ("{0} {1}" -f (Get-Date -Format s), $m) -Encoding utf8 }
# Scrive il motivo dell'errore: Python lo mostra in Impostazioni > Aggiornamenti.
function Write-Failure([string]$msg, [bool]$keepPackage) {
  $f = [ordered]@{ from = $FromVersion; to = $Version; at = (Get-Date -Format s); message = $msg; keepPackage = $keepPackage }
  [IO.File]::WriteAllText($failureFile, ($f | ConvertTo-Json), $utf8)
  Log "errore: $msg"
  if ($keepPackage) {
    # il pacchetto scaricato resta pronto: si riprova con «Installa», senza scaricarlo di nuovo
    $p = Get-Content -LiteralPath $PendingFile -Raw -Encoding utf8 | ConvertFrom-Json
    $p.approved = $false
    [IO.File]::WriteAllText($PendingFile, ($p | ConvertTo-Json), $utf8)
  } else {
    Remove-Item -LiteralPath $PendingFile -Force -ErrorAction SilentlyContinue
  }
}
# Antivirus e indicizzazione possono tenere aperti i file per qualche secondo: si riprova.
function Move-Retry([string]$src, [string]$dst) {
  for ($i = 1; ; $i++) {
    try { Move-Item -LiteralPath $src -Destination $dst -ErrorAction Stop; return }
    catch { if ($i -ge 40) { throw }; Start-Sleep -Milliseconds 500 }
  }
}
Log "installazione $FromVersion -> $Version"
if (Test-Path -LiteralPath $failureFile) { Remove-Item -LiteralPath $failureFile -Force }
$deadline = (Get-Date).AddSeconds(120)
while (Get-Process -Name $ProcessName -ErrorAction SilentlyContinue) {
  if ((Get-Date) -gt $deadline) {
    Write-Failure "PhotoScan non si è chiuso entro 2 minuti. Chiudilo (anche la finestra di analisi) e riprova." $true
    exit 2
  }
  Start-Sleep -Milliseconds 500
}
$movedOld = $false
try {
  Move-Retry $AppDir $Backup
  $movedOld = $true
  Move-Retry $Staged $AppDir
  if (-not (Test-Path -LiteralPath (Join-Path $AppDir $ExeName))) { throw "$ExeName mancante nel pacchetto scaricato" }
} catch {
  $why = $_.Exception.Message
  if ($movedOld) {
    try {
      if (Test-Path -LiteralPath $AppDir) { Remove-Item -LiteralPath $AppDir -Recurse -Force }
      Move-Retry $Backup $AppDir
    } catch {
      Write-Failure "ripristino della versione $FromVersion non riuscito: $($_.Exception.Message). Copia di riserva in: $Backup" $false
      exit 1
    }
  }
  $broken = $why -like '*mancante nel pacchetto*'
  if ($broken) {
    Write-Failure "il pacchetto scaricato è danneggiato ($why). Nessuna modifica: resta la versione $FromVersion. Scarica di nuovo l'aggiornamento." $false
  } else {
    Write-Failure "impossibile sostituire la cartella di PhotoScan: un file è ancora in uso ($why). Nessuna modifica: resta la versione $FromVersion. Il pacchetto è ancora pronto: premi «Installa» per riprovare." $true
  }
  if (-not $NoLaunch) { Start-Process -FilePath (Join-Path $AppDir $ExeName) -WorkingDirectory $AppDir }
  exit 1
}
Remove-Item -LiteralPath $PendingFile -Force -ErrorAction SilentlyContinue
$info = [ordered]@{ from = $FromVersion; to = $Version; at = (Get-Date -Format s); backup = $Backup;
  requirements_changed = $false; notes = '' }
# senza BOM: Python legge il file con json
[IO.File]::WriteAllText($InstalledFile, ($info | ConvertTo-Json), $utf8)
# tiene solo la copia di riserva piu' recente
$keep = (Get-Item -LiteralPath $Backup).FullName  # percorso lungo: Python puo' passare quello corto (ANDREA~1)
Get-ChildItem -LiteralPath (Split-Path -Parent $keep) -Directory | Where-Object { $_.FullName -ne $keep } |
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Log "installata la versione $Version"
if (-not $NoLaunch) { Start-Process -FilePath (Join-Path $AppDir $ExeName) -WorkingDirectory $AppDir }
"""


def _write_failure(cache_dir, pending: dict | None, message: str, keep_package: bool = False) -> None:
    """Stesso formato di Write-Failure dello script PowerShell: Impostazioni > Aggiornamenti mostra il motivo."""
    d = updates_dir(cache_dir)
    _write_json(d / "last_failure.json", {
        "from": __version__, "to": (pending or {}).get("version"), "at": datetime.now().isoformat(timespec="seconds"),
        "message": message, "keepPackage": keep_package})
    if pending is not None:
        if keep_package:
            pending["approved"] = False
            _write_json(d / "pending.json", pending)
        else:
            (d / "pending.json").unlink(missing_ok=True)


def _install_appimage(cache_dir, pending: dict) -> bool:
    """Linux: sostituisce l'AppImage in uso con quello scaricato. La nuova copia si scrive accanto all'originale
    (stesso filesystem) e si rinomina: atomico anche se l'AppImage e' in esecuzione e se sta su un altro disco della
    cache. La vecchia versione resta come copia di riserva. Il riavvio lo fa chi chiama."""
    target = os.environ.get("APPIMAGE")          # impostata dal runtime AppImage con il percorso del file
    src = Path(pending["dir"])
    d = updates_dir(cache_dir)
    if not target or not src.is_file():
        return False                              # non eseguito come AppImage: niente da sostituire
    target_p = Path(target)
    if not os.access(target_p.parent, os.W_OK):
        _write_failure(cache_dir, pending, f"La cartella {target_p.parent} non è scrivibile: sposta l'AppImage in una "
                       "cartella tua (es. ~/Applicazioni) oppure scarica la nuova versione a mano.")
        return False
    backup = d / "backup-appimage" / f"{__version__}.AppImage"
    tmp = target_p.with_name(target_p.name + ".new")
    try:
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target_p, backup)
        shutil.copyfile(src, tmp)
        tmp.chmod(0o755)
        os.replace(tmp, target_p)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        _write_failure(cache_dir, pending, f"Installazione dell'AppImage non riuscita: {e}.", keep_package=True)
        return False
    src.unlink(missing_ok=True)
    for old in (d / "backup-appimage").glob("*.AppImage"):   # tiene solo la copia di riserva piu' recente
        if old != backup:
            old.unlink(missing_ok=True)
    _write_json(d / "installed.json", {"from": __version__, "to": pending["version"],
                                       "at": datetime.now().isoformat(timespec="seconds"), "backup": str(backup)})
    (d / "pending.json").unlink(missing_ok=True)
    return True


def _writable_dir(path: Path) -> bool:
    """Si puo' creare e togliere un file in `path`? (Program Files: no, senza amministratore.)"""
    probe = path / f".photoscan-write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def start_exe_update(cache_dir, app_dir: Path, *, no_launch: bool = False, process_name: str = "PhotoScan",
                     force: bool = False) -> bool:
    """Se c'e' un eseguibile scaricato e approvato (o force: installazione automatica), lancia lo script che lo
    installa dopo la chiusura. True = installazione avviata (lo script riapre PhotoScan da solo)."""
    pending = read_pending(cache_dir)
    if not pending or not (pending.get("approved") or force):
        return False
    if pending.get("kind") == "appimage":
        return _install_appimage(cache_dir, pending)
    if sys.platform != "win32" or pending.get("kind") != "exe":
        return False
    d = updates_dir(cache_dir)
    app_dir = Path(app_dir)
    if not _writable_dir(app_dir.parent) or not _writable_dir(app_dir):
        _write_failure(cache_dir, pending, f"La cartella di PhotoScan ({app_dir}) non è modificabile senza "
                       "amministratore (es. Program Files). Installa PhotoScan con PhotoScan-Setup: usa una cartella tua.")
        return False
    (d / "backup-exe").mkdir(parents=True, exist_ok=True)
    helper = d / "apply_exe_update.ps1"
    helper.write_text(EXE_HELPER, encoding="utf-8-sig")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = d / "backup-exe" / f"{__version__}-{stamp}"
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(helper),
           "-AppDir", str(app_dir), "-Staged", str(pending["dir"]), "-Backup", str(backup), "-ExeName", EXE_NAME,
           "-Version", str(pending["version"]), "-FromVersion", __version__,
           "-PendingFile", str(d / "pending.json"), "-InstalledFile", str(d / "installed.json"),
           "-LogFile", str(d / "apply-exe.log")]
    cmd += ["-ProcessName", process_name]
    if no_launch:
        cmd.append("-NoLaunch")
    # niente DETACHED_PROCESS: senza console PowerShell non parte. CREATE_NO_WINDOW basta.
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # cwd fuori dalla cartella del programma: lo script non deve tenerla aperta (vedi EXE_HELPER)
    started = time.time()
    proc = subprocess.Popen(cmd, cwd=str(d), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=flags, close_fds=True)
    # Lo script scrive nel suo log appena parte. Se PowerShell esce subito senza averlo fatto (criteri di esecuzione
    # di Group Policy, antivirus) l'utente non vedrebbe mai l'aggiornamento: lo si scrive come errore.
    log_file = d / "apply-exe.log"
    end = started + 15
    while time.time() < end:
        if log_file.exists() and log_file.stat().st_mtime >= started - 1:
            return True
        if proc.poll() is not None:
            time.sleep(0.3)
            if log_file.exists() and log_file.stat().st_mtime >= started - 1:
                return True
            _write_failure(cache_dir, pending, "Windows ha impedito di eseguire lo script di installazione (criteri di "
                           f"esecuzione di PowerShell o antivirus; codice {proc.returncode}). Scarica PhotoScan-Setup "
                           "dalla pagina della Release e installalo sopra a questa versione.", keep_package=False)
            return False
        time.sleep(0.25)
    return True       # PowerShell e' lento a partire (antivirus), ma e' in esecuzione: si lascia lavorare


def read_last_failure(cache_dir) -> dict | None:
    """Ultimo tentativo di installazione dell'eseguibile non riuscito: {from, to, at, message, keepPackage}."""
    return _read_json(updates_dir(cache_dir) / "last_failure.json")


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
    if pending.get("kind") == "exe":  # eseguibile: lo installa start_exe_update() alla chiusura
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
        raise UpdateError(f"Installazione dell'aggiornamento non riuscita: {e}. PhotoScan resta alla versione {old_version}.") from e

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
    non passa dal blocco di rete di PhotoScan e scarica da PyPI."""
    try:
        proc = subprocess.run([python, "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"],
                              cwd=str(root), capture_output=True, text=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out[-4000:]
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
