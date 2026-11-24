"""PhotoScan-Setup.exe: installatore leggero di PhotoScan.

All'avvio legge l'hardware (CPU, RAM, GPU NVIDIA e VRAM, spazio libero), propone il pacchetto
(CPU o GPU) e il modello di riconoscimento più adatti, mostra cosa è già installato e scarica
dalle Release GitHub solo le parti necessarie. Il programma finisce in %LOCALAPPDATA%\\PhotoScan\\app.

Il modello non viene scaricato qui: lo scarica PhotoScan al primo utilizzo, come già oggi.

Pacchetti attesi nella Release (il nome decide il runtime):
  PhotoScan-<v>-windows.zip(.001, .002, ...)        runtime GPU (CUDA), compatibile con l'aggiornamento automatico
  PhotoScan-<v>-windows-cpu.zip(.001, .002, ...)    runtime CPU
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO = "RedErik21/PhotoScan"
GB = 1024 ** 3

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


_PART_RE = re.compile(r"^PhotoScan-(?P<v>\d[\w.\-]*?)-(?P<rt>windows|windows-cpu)\.zip(?:\.(?P<n>\d{3}))?$")


def package_parts(assets: list[dict], version: str, runtime: str) -> list[dict]:
    """Parti del pacchetto del runtime richiesto, in ordine. Ogni asset: {'name','url','size'}."""
    want = "windows" if runtime == "gpu" else "windows-cpu"
    found = []
    for a in assets:
        m = _PART_RE.match(a.get("name") or "")
        if m and m.group("v") == version and m.group("rt") == want and a.get("browser_download_url"):
            found.append((int(m.group("n") or 0), {"name": a["name"], "url": a["browser_download_url"],
                                                   "size": int(a.get("size") or 0)}))
    return [p for _, p in sorted(found, key=lambda t: t[0])]


def latest_release(repo: str = REPO) -> dict:
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "PhotoScan-Setup"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def download_parts(parts: list[dict], dest: Path, progress=None) -> Path:
    """Scarica le parti in `dest` e le concatena in un unico zip. Controlla le dimensioni dichiarate."""
    dest.mkdir(parents=True, exist_ok=True)
    total = sum(p["size"] for p in parts) or 1
    done = 0
    zip_path = dest / "package.zip"  # nome distinto dalle parti, che vengono cancellate man mano
    with open(zip_path, "wb") as out:
        for p in parts:
            with urllib.request.urlopen(p["url"], timeout=60) as r, open(dest / p["name"], "wb") as part:
                while chunk := r.read(8 * 1024 * 1024):
                    part.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done / total, f"Download {p['name']}")
            got = (dest / p["name"]).stat().st_size
            if p["size"] and got != p["size"]:
                raise RuntimeError(f"Parte {p['name']} incompleta ({got} di {p['size']} byte).")
            with open(dest / p["name"], "rb") as part:
                shutil.copyfileobj(part, out, 8 * 1024 * 1024)
            (dest / p["name"]).unlink()
    return zip_path


def extract(zip_path: Path, app_dir: Path, progress=None) -> None:
    """Estrae in app_dir, sostituendo una versione precedente solo dopo un'estrazione riuscita."""
    staging = app_dir.with_name(app_dir.name + ".new")
    if staging.exists():
        shutil.rmtree(staging)
    with zipfile.ZipFile(zip_path) as z:
        members = z.infolist()
        for i, m in enumerate(members, 1):
            z.extract(m, staging)
            if progress and i % 200 == 0:
                progress(i / len(members), "Estrazione")
    if app_dir.exists():
        shutil.rmtree(app_dir)
    staging.rename(app_dir)


def write_config(config_file: Path, model_id: str) -> None:
    cfg = {}
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    cfg["model_name"] = model_id
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def installed_state(root: Path) -> dict | None:
    marker = root / "install.json"
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_install_marker(root: Path, version: str, runtime: str, model: str) -> None:
    (root / "install.json").write_text(json.dumps({"version": version, "runtime": runtime, "model": model},
                                                  indent=2), encoding="utf-8")


def create_desktop_shortcut(exe: Path) -> None:
    desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    if not desktop.exists():
        return
    lnk = desktop / "PhotoScan.lnk"
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
          f"$s.TargetPath='{exe}';$s.WorkingDirectory='{exe.parent}';$s.IconLocation='{exe},0';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=60,
                   creationflags=0x08000000)


def main() -> int:  # pragma: no cover - interfaccia grafica, provata a mano
    from setup_gui import run_gui

    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
