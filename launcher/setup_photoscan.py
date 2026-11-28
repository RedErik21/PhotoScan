"""PhotoScan-Setup.exe: installatore leggero di PhotoScan.

All'avvio legge l'hardware (CPU, RAM, GPU NVIDIA e VRAM, spazio libero), propone il pacchetto
(CPU o GPU) e il modello di riconoscimento più adatti, mostra cosa è già installato e scarica
dalle Release GitHub solo le parti necessarie. Il programma finisce in %LOCALAPPDATA%\\PhotoScan\\app,
il componente di calcolo (PyTorch) in %LOCALAPPDATA%\\PhotoScan\\runtime\\<versione>-<cpu|gpu>.

Il modello non viene scaricato qui: lo scarica PhotoScan al primo utilizzo, come già oggi.

Pacchetti nella Release (il nome decide il tipo):
  PhotoScan-app-<v>-windows.zip(.001, ...)                      app leggera (senza PyTorch)
  PhotoScan-runtime-<rv>-windows[-cpu].zip(.001, ...)           componente di calcolo, GPU (CUDA) o CPU. Si pubblica solo
                                                                quando cambia: puo' stare in una Release più vecchia
  PhotoScan-<v>-windows.zip / -windows-cpu.zip (.001, ...)      pacchetto completo (versioni <= 0.1.10 e ripiego)
  SHA256SUMS, release-manifest.json                             impronte dei file e versione del runtime richiesto

Uso da riga di comando (la via di riserva se l'aggiornamento dall'app non funziona):
  PhotoScan-Setup.exe                          installazione guidata (versione più recente)
  PhotoScan-Setup.exe --version 0.1.9          installazione guidata, versione preselezionata
  PhotoScan-Setup.exe --silent [--cpu|--gpu] [--model base|large|so400m] [--version X]   senza finestre
  PhotoScan-Setup.exe --uninstall [--keep-data|--delete-data]                            disinstalla
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO = "RedErik21/PhotoScan"
GB = 1024 ** 3
SUMS_NAME = "SHA256SUMS"
MANIFEST_NAME = "release-manifest.json"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\PhotoScan"
RETRIES = 4

# Modelli: stessi id e dimensioni di fotoscan/config.py (AVAILABLE_MODELS).
MODELS = {
    "base": {"id": "google/siglip2-base-patch16-224", "label": "SigLIP 2 Base — veloce", "download_gb": 1.5},
    "large": {"id": "google/siglip2-large-patch16-384", "label": "SigLIP 2 Large — alta precisione", "download_gb": 3.5},
    "so400m": {"id": "google/siglip2-so400m-patch14-384", "label": "SigLIP 2 SO400M — massima precisione", "download_gb": 4.5},
}
RUNTIME_GB = {"gpu": 3.0, "cpu": 1.0}


@dataclass
class Hardware:
    cores: int
    ram_gb: float
    gpu_name: str | None
    vram_mb: int
    free_disk_gb: float


def detect_hardware(data_dir: Path) -> Hardware:
    import psutil

    cores = psutil.cpu_count(logical=False) or psutil.cpu_count() or 1
    ram_gb = psutil.virtual_memory().total / GB
    gpu_name, vram_mb = None, 0
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, creationflags=0x08000000,  # CREATE_NO_WINDOW
        ).stdout.strip().splitlines()
        if out:
            name, mem = out[0].rsplit(",", 1)
            gpu_name, vram_mb = name.strip(), int(float(mem.strip()))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # niente nvidia-smi: nessuna GPU NVIDIA utilizzabile
    probe = data_dir
    while not probe.exists() and probe != probe.parent:  # disco su cui verrà creata la cartella dati
        probe = probe.parent
    free_gb = shutil.disk_usage(str(probe)).free / GB
    return Hardware(cores, ram_gb, gpu_name, vram_mb, free_gb)


def recommend(hw: Hardware) -> tuple[str, str]:
    """(runtime, modello) consigliati. Soglie da verificare su PC reali (vedi README del launcher)."""
    if hw.gpu_name and hw.vram_mb >= 6000 and hw.ram_gb >= 16:
        return "gpu", "so400m"
    if hw.gpu_name and hw.vram_mb >= 4000 and hw.ram_gb >= 8:
        return "gpu", "large"
    return "cpu", "base"


def space_warning(hw: Hardware, runtime: str, model: str) -> str | None:
    need = RUNTIME_GB[runtime] + MODELS[model]["download_gb"] + 1.0
    if hw.free_disk_gb < need:
        return f"Spazio libero {hw.free_disk_gb:.1f} GB, ne servono circa {need:.1f} GB."
    return None


# ------------------------------------------------------------------ nomi degli asset
_PART_RE = re.compile(r"^PhotoScan-(?P<v>\d[\w.\-]*?)-(?P<rt>windows|windows-cpu)\.zip(?:\.(?P<n>\d{3}))?$")
_APP_RE = re.compile(r"^PhotoScan-app-(?P<v>\d[\w.\-]*?)-windows\.zip(?:\.(?P<n>\d{3}))?$")
_RUNTIME_RE = re.compile(r"^PhotoScan-runtime-(?P<rv>[\w.]+?)-windows(?P<cpu>-cpu)?\.zip(?:\.(?P<n>\d{3}))?$")


def _complete(found: list[tuple[int, dict]]) -> list[dict]:
    """Parti in ordine, solo se complete: un file unico oppure .001 ... .NNN senza buchi (Release ancora in caricamento)."""
    if not found:
        return []
    nums = sorted(n for n, _ in found)
    if nums == [0]:
        return [p for _, p in found]
    if nums != list(range(1, len(nums) + 1)):
        return []
    return [p for _, p in sorted(found, key=lambda t: t[0])]


def _part(a: dict) -> dict:
    return {"name": a["name"], "url": a["browser_download_url"], "size": int(a.get("size") or 0)}


def package_parts(assets: list[dict], version: str, runtime: str) -> list[dict]:
    """Parti del pacchetto COMPLETO del runtime richiesto, in ordine. Ogni asset: {'name','url','size'}."""
    want = "windows" if runtime == "gpu" else "windows-cpu"
    found = []
    for a in assets:
        m = _PART_RE.match(a.get("name") or "")
        if m and m.group("v") == version and m.group("rt") == want and a.get("browser_download_url"):
            found.append((int(m.group("n") or 0), _part(a)))
    return _complete(found)


def app_parts(assets: list[dict], version: str) -> list[dict]:
    found = []
    for a in assets:
        m = _APP_RE.match(a.get("name") or "")
        if m and m.group("v") == version and a.get("browser_download_url"):
            found.append((int(m.group("n") or 0), _part(a)))
    return _complete(found)


def asset_url(release: dict, name: str) -> str | None:
    for a in release.get("assets") or []:
        if a.get("name") == name and a.get("browser_download_url"):
            return a["browser_download_url"]
    return None


def runtime_index(releases: list[dict]) -> dict:
    """{versione_runtime: {gpu|cpu: {parts, sums_url}}} da TUTTE le Release (il runtime sta dove e' stato pubblicato)."""
    out: dict = {}
    for r in releases:
        if r.get("draft"):
            continue
        groups: dict = {}
        for a in r.get("assets") or []:
            m = _RUNTIME_RE.match(a.get("name") or "")
            if m and a.get("browser_download_url"):
                groups.setdefault((m.group("rv"), "cpu" if m.group("cpu") else "gpu"), []).append(
                    (int(m.group("n") or 0), _part(a)))
        for (rv, flavor), found in groups.items():
            parts = _complete(found)
            if parts and flavor not in out.get(rv, {}):
                out.setdefault(rv, {})[flavor] = {"parts": parts, "sums_url": asset_url(r, SUMS_NAME)}
    return out


# ------------------------------------------------------------------ rete
def _get(url: str, accept: str = "application/vnd.github+json", timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "PhotoScan-Setup"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_release(repo: str = REPO) -> dict:
    return json.loads(_get(f"https://api.github.com/repos/{repo}/releases/latest").decode("utf-8"))


def list_releases(repo: str = REPO) -> list[dict]:
    """Le Release pubblicate (non bozze), piu' recenti per prime."""
    data = json.loads(_get(f"https://api.github.com/repos/{repo}/releases?per_page=30").decode("utf-8"))
    return [r for r in data if isinstance(r, dict) and not r.get("draft") and r.get("tag_name")]


def version_key(v: str) -> tuple:
    s = v.strip().lstrip("vV").split("+", 1)[0]
    main, _, pre = s.partition("-")
    nums = [int(x) for x in re.findall(r"\d+", main)]
    nums += [0] * (3 - len(nums))
    while len(nums) > 3 and nums[-1] == 0:
        nums.pop()
    return (tuple(nums), 0 if pre else 1, tuple((0, int(t)) if t.isdigit() else (1, t.lower())
                                                 for t in re.findall(r"\d+|[A-Za-z]+", pre)))


def pick_release(releases: list[dict], version: str | None = None, prerelease: bool = False) -> dict:
    """La Release richiesta (`version`) o la piu' recente stabile."""
    if version:
        want = version_key(version)
        for r in releases:
            if version_key(r["tag_name"]) == want:
                return r
        raise RuntimeError(f"La versione {version} non è tra le Release di GitHub.")
    best = None
    for r in releases:
        if r.get("prerelease") and not prerelease:
            continue
        if best is None or version_key(r["tag_name"]) > version_key(best["tag_name"]):
            best = r
    if best is None:
        raise RuntimeError("Nessuna Release pubblicata su GitHub.")
    return best


def parse_sums(text: str) -> dict[str, str]:
    out = {}
    for line in (text or "").splitlines():
        m = re.match(r"^([0-9A-Fa-f]{64})\s+\*?(.+?)\s*$", line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def load_sums(url: str | None) -> dict[str, str] | None:
    """None = la Release non pubblica SHA256SUMS (versioni vecchie): si prosegue senza verifica."""
    if not url:
        return None
    sums = parse_sums(_get(url, "application/octet-stream").decode("utf-8-sig"))
    if not sums:
        raise RuntimeError(f"{SUMS_NAME} della Release è vuoto o illeggibile.")
    return sums


def attach_sums(parts: list[dict], sums: dict[str, str] | None) -> list[dict]:
    parts = [dict(p) for p in parts]
    if sums is None:
        return parts
    for p in parts:
        h = sums.get(p["name"])
        if not h:
            raise RuntimeError(f"{p['name']} non è nell'elenco {SUMS_NAME}: file non verificabile.")
        p["sha256"] = h
    return parts


def download_parts(parts: list[dict], dest: Path, progress=None) -> Path:
    """Scarica le parti in `dest` e le concatena in un unico zip. Controlla dimensione e SHA-256 dichiarati;
    riprende da dove si e' interrotto (Range) fino a RETRIES volte per parte."""
    dest.mkdir(parents=True, exist_ok=True)
    total = sum(p["size"] for p in parts) or 1
    done = 0
    zip_path = dest / "package.zip"  # nome distinto dalle parti, che vengono cancellate man mano
    with open(zip_path, "wb") as out:
        for p in parts:
            part_path = dest / p["name"]
            hasher, got, last = hashlib.sha256(), 0, None
            with open(part_path, "wb") as part:
                for attempt in range(RETRIES):
                    last = None
                    try:
                        headers = {"User-Agent": "PhotoScan-Setup"}
                        if got:
                            headers["Range"] = f"bytes={got}-"
                        with urllib.request.urlopen(urllib.request.Request(p["url"], headers=headers), timeout=60) as r:
                            if got and getattr(r, "status", 200) != 206:     # niente Range: la parte riparte da capo
                                done -= got
                                part.seek(0)
                                part.truncate()
                                hasher, got = hashlib.sha256(), 0
                            while chunk := r.read(8 * 1024 * 1024):
                                part.write(chunk)
                                hasher.update(chunk)
                                got += len(chunk)
                                done += len(chunk)
                                if progress:
                                    progress(min(done / total, 0.9), f"Download {p['name']}")
                        break
                    except OSError as e:
                        code = getattr(e, "code", None)
                        if code and 400 <= code < 500 and code not in (408, 429):
                            raise RuntimeError(f"GitHub ha risposto {code} per {p['name']}.") from e
                        last = e
                        time.sleep(min(2 ** attempt, 8))
            if last is not None:
                raise RuntimeError(f"Download di {p['name']} interrotto: {last}")
            if p["size"] and got != p["size"]:
                raise RuntimeError(f"Parte {p['name']} incompleta ({got} di {p['size']} byte).")
            if p.get("sha256") and hasher.hexdigest() != p["sha256"]:
                raise RuntimeError(f"Parte {p['name']} danneggiata: l'impronta SHA-256 non corrisponde.")
            with open(part_path, "rb") as part:
                shutil.copyfileobj(part, out, 8 * 1024 * 1024)
            part_path.unlink()
    return zip_path


# ------------------------------------------------------------------ estrazione
def _safe_members(z: zipfile.ZipFile, staging: Path):
    base = staging.resolve()
    for m in z.infolist():
        target = (staging / m.filename).resolve()
        if base != target and base not in target.parents:
            raise RuntimeError(f"Pacchetto non valido: percorso sospetto {m.filename!r}.")
        yield m


def extract(zip_path: Path, app_dir: Path, progress=None) -> None:
    """Estrae in app_dir, sostituendo una versione precedente solo dopo un'estrazione riuscita: la vecchia cartella
    viene spostata (non cancellata) e torna al suo posto se la sostituzione fallisce."""
    staging = app_dir.with_name(app_dir.name + ".new")
    old = app_dir.with_name(app_dir.name + ".old")
    for d in (staging, old):
        if d.exists():
            shutil.rmtree(d)
    with zipfile.ZipFile(zip_path) as z:
        members = list(_safe_members(z, staging))
        for i, m in enumerate(members, 1):
            z.extract(m, staging)
            if progress and i % 200 == 0:
                progress(i / len(members), "Estrazione")
    moved = False
    try:
        if app_dir.exists():
            app_dir.rename(old)          # fallisce subito (senza danni) se un file e' in uso
            moved = True
        staging.rename(app_dir)
    except OSError as e:
        if moved and not app_dir.exists():
            old.rename(app_dir)          # ripristino
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"Non riesco a sostituire la cartella di PhotoScan ({e}). Chiudi PhotoScan e riprova.") from e
    shutil.rmtree(old, ignore_errors=True)


def extract_runtime(zip_path: Path, dest: Path) -> None:
    """Zip del runtime (cartella radice `runtime/`) -> dest, in modo atomico (appoggio + rinomina)."""
    staging = dest.with_name(".staging-" + dest.name)
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            for m in _safe_members(z, staging):
                parts = Path(m.filename).parts
                if len(parts) < 2 or m.is_dir():
                    continue
                target = staging.joinpath(*parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
        if not (staging / "runtime.json").is_file() or not (staging / "site").is_dir():
            raise RuntimeError("Il runtime scaricato è incompleto (runtime.json o site mancanti).")
        old = None
        if dest.exists():
            old = dest.with_name(".old-" + dest.name)
            shutil.rmtree(old, ignore_errors=True)
            dest.rename(old)
        staging.rename(dest)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def write_config(config_file: Path, model_id: str) -> None:
    cfg = {}
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            cfg = {}
    cfg["model_name"] = model_id
    config_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_file.with_name(config_file.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, config_file)


def installed_state(root: Path) -> dict | None:
    marker = root / "install.json"
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def write_install_marker(root: Path, version: str, runtime: str, model: str, extra: dict | None = None) -> None:
    data = {"version": version, "runtime": runtime, "model": model, **(extra or {})}
    (root / "install.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ collegamenti
def _known_folder(fid: str) -> Path | None:
    from uninstall_photoscan import known_folder
    return known_folder(fid)


def shortcut_targets() -> list[Path]:
    """Desktop (anche reindirizzato da OneDrive) e menu Start dell'utente."""
    from uninstall_photoscan import FOLDERID_DESKTOP, FOLDERID_PROGRAMS
    out = []
    for fid, fallback in ((FOLDERID_DESKTOP, Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"),
                          (FOLDERID_PROGRAMS, Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" /
                           "Start Menu" / "Programs")):
        d = _known_folder(fid) or fallback
        if d.exists():
            out.append(d / "PhotoScan.lnk")
    return out


def create_shortcut(lnk: Path, exe: Path) -> bool:
    """Collegamento .lnk. I percorsi passano da variabili d'ambiente, non dal testo dello script: apostrofi e accenti
    nel nome utente (D'Angelo, Andrea Stella) non lo rompono. True = creato davvero."""
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:PS_LNK);"
          "$s.TargetPath=$env:PS_EXE;$s.WorkingDirectory=$env:PS_DIR;$s.IconLocation=$env:PS_EXE+',0';$s.Save()")
    env = dict(os.environ, PS_LNK=str(lnk), PS_EXE=str(exe), PS_DIR=str(exe.parent))
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, timeout=60,
                       creationflags=0x08000000, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    return lnk.exists()


def create_desktop_shortcut(exe: Path) -> list[Path]:
    """Collegamenti sul Desktop e nel menu Start. Ritorna quelli creati (vuoto = nessuno: si dice all'utente)."""
    return [lnk for lnk in shortcut_targets() if create_shortcut(lnk, exe)]


# ------------------------------------------------------------------ «App installate» e disinstallatore
def _winreg(reg=None):
    if reg is not None:
        return reg
    import winreg
    return winreg


def register_uninstall(root: Path, version: str, exe: Path, uninstall_cmd: str, size_bytes: int = 0, reg=None) -> None:
    """Voce in «App installate» (HKCU: nessun amministratore)."""
    w = _winreg(reg)
    values = [
        ("DisplayName", w.REG_SZ, "PhotoScan"),
        ("DisplayVersion", w.REG_SZ, version),
        ("Publisher", w.REG_SZ, "PhotoScan"),
        ("DisplayIcon", w.REG_SZ, f"{exe},0"),
        ("InstallLocation", w.REG_SZ, str(root / "app")),
        ("UninstallString", w.REG_SZ, uninstall_cmd),
        ("QuietUninstallString", w.REG_SZ, uninstall_cmd + " --silent --keep-data"),
        ("EstimatedSize", w.REG_DWORD, max(1, int(size_bytes // 1024))),
        ("NoModify", w.REG_DWORD, 1),
        ("NoRepair", w.REG_DWORD, 1),
        ("InstallDate", w.REG_SZ, time.strftime("%Y%m%d")),
    ]
    with w.CreateKeyEx(w.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, w.KEY_WRITE) as key:
        for name, typ, val in values:
            w.SetValueEx(key, name, 0, typ, val)


def install_uninstaller(root: Path) -> str:
    """Copia il Setup in <dati>/uninstall e ritorna il comando di disinstallazione per il registro."""
    if getattr(sys, "frozen", False):
        dst = root / "uninstall" / "PhotoScan-Uninstall.exe"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sys.executable, dst)
        return f'"{dst}" --uninstall'
    return f'"{sys.executable}" "{Path(__file__).resolve()}" --uninstall'     # sviluppo: dal sorgente


def dir_bytes(path: Path) -> int:
    total = 0
    for p in Path(path).rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


# ------------------------------------------------------------------ installazione completa
def running_app_processes(app_dir: Path) -> list:
    try:
        import psutil
    except ImportError:
        return []
    base = str(app_dir).lower()
    out = []
    for p in psutil.process_iter(["pid", "exe"]):
        try:
            if (p.info.get("exe") or "").lower().startswith(base):
                out.append(p)
        except Exception:  # noqa: BLE001
            continue
    return out


def plan_install(release: dict, releases: list[dict], runtime: str) -> dict:
    """Cosa scaricare per questa Release: app leggera + runtime (se serve un runtime che l'app richiede) oppure
    pacchetto completo (Release <= 0.1.10 o runtime non trovato)."""
    version = release["tag_name"].lstrip("vV")
    assets = release.get("assets") or []
    sums_url = asset_url(release, SUMS_NAME)
    manifest_url = asset_url(release, MANIFEST_NAME)
    app = app_parts(assets, version)
    if app and manifest_url:
        manifest = json.loads(_get(manifest_url, "application/octet-stream").decode("utf-8-sig"))
        rv = str(manifest.get("runtime_version") or "")
        entry = (runtime_index(releases).get(rv) or {}).get(runtime)
        if rv and entry:
            return {"kind": "light", "version": version, "app": app, "runtime_version": rv, "runtime_entry": entry,
                    "sums_url": sums_url}
    full = package_parts(assets, version, runtime)
    if not full:
        raise RuntimeError(f"La Release {release['tag_name']} non contiene il pacchetto «{runtime}».")
    return {"kind": "full", "version": version, "full": full, "sums_url": sums_url}


def install(root: Path, runtime: str, model: str, version: str | None = None, progress=None,
            prerelease: bool = False) -> Path:
    """Installazione vera e propria (usata dalla finestra e da --silent). Ritorna il percorso di PhotoScan.exe."""
    say = progress or (lambda f, t: None)
    root.mkdir(parents=True, exist_ok=True)
    say(0.03, "Controllo le versioni disponibili…")
    releases = list_releases()
    release = pick_release(releases, version, prerelease=prerelease or bool(version))
    ver = release["tag_name"].lstrip("vV")
    app_dir = root / "app"
    state = installed_state(root)
    same = (state and state.get("version") == ver and state.get("runtime") == runtime and resolve_exe(app_dir).exists())
    if not same:
        busy = running_app_processes(app_dir)
        if busy:
            raise RuntimeError("PhotoScan è aperto: chiudilo e riprova (serve per sostituire i file del programma).")
        plan = plan_install(release, releases, runtime)
        dl = root / "download"
        shutil.rmtree(dl, ignore_errors=True)
        sums = load_sums(plan["sums_url"])
        if plan["kind"] == "light":
            rt_entry = plan["runtime_entry"]
            rt_parts = attach_sums(rt_entry["parts"], load_sums(rt_entry.get("sums_url")))
            app_p = attach_sums(plan["app"], sums)
            tot = sum(p["size"] for p in rt_parts) + sum(p["size"] for p in app_p) or 1
            rt_share = sum(p["size"] for p in rt_parts) / tot
            rdest = root / "runtime" / f"{plan['runtime_version']}-{runtime}"
            if not (rdest / "runtime.json").is_file():
                z = download_parts(rt_parts, dl / "runtime", lambda f, t: say(f * rt_share * 0.9, t))
                say(rt_share * 0.9, "Estrazione del componente di calcolo…")
                extract_runtime(z, rdest)
                shutil.rmtree(dl / "runtime", ignore_errors=True)
            z = download_parts(app_p, dl / "app", lambda f, t: say(rt_share * 0.9 + f * (1 - rt_share) * 0.9, t))
            say(0.95, "Estrazione dei file…")
            extract(z, app_dir, say)
            for old in (root / "runtime").iterdir():       # un solo runtime: quello appena installato
                if old != rdest and old.is_dir():
                    shutil.rmtree(old, ignore_errors=True)
        else:
            z = download_parts(attach_sums(plan["full"], sums), dl, say)
            say(0.95, "Estrazione dei file…")
            extract(z, app_dir, say)
        shutil.rmtree(dl, ignore_errors=True)
    else:
        say(0.85, "Versione già presente, aggiorno la configurazione…")
    write_config(root / "config.json", MODELS[model]["id"])
    write_install_marker(root, ver, runtime, model)
    exe = resolve_exe(app_dir)
    made = create_desktop_shortcut(exe)
    try:
        cmd = install_uninstaller(root)
        register_uninstall(root, ver, exe, cmd, dir_bytes(root / "app") + dir_bytes(root / "runtime")
                           if (root / "runtime").exists() else dir_bytes(root / "app"))
    except Exception as e:  # noqa: BLE001 - la voce in «App installate» non blocca l'installazione
        say(0.99, f"Voce in «App installate» non creata: {e}")
    if not made:
        say(1.0, "Installato, ma il collegamento sul Desktop non è stato creato: apri PhotoScan da " + str(exe))
    return exe


def resolve_exe(app_dir: Path) -> Path:
    direct = app_dir / "PhotoScan.exe"
    if direct.exists():
        return direct
    nested = app_dir / "PhotoScan" / "PhotoScan.exe"
    if nested.exists():
        return nested
    return direct


def _arg(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - interfaccia grafica, provata a mano
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--uninstall" in argv:
        from uninstall_photoscan import run
        return run(argv)
    if "--silent" in argv:
        data = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "PhotoScan"
        runtime = "cpu" if "--cpu" in argv else "gpu" if "--gpu" in argv else recommend(detect_hardware(data))[0]
        model = _arg(argv, "--model") or "base"
        try:
            exe = install(data, runtime, model, _arg(argv, "--version"),
                          progress=lambda f, t: print(f"{f * 100:5.1f}% {t}", flush=True))
        except Exception as e:  # noqa: BLE001
            print(f"Installazione non riuscita: {e}", file=sys.stderr)
            return 1
        print(f"Installato: {exe}")
        return 0
    from setup_gui import run_gui

    return run_gui(_arg(argv, "--version"))


if __name__ == "__main__":
    sys.exit(main())
