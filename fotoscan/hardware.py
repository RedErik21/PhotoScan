"""Configurazione del PC, modello consigliato e velocita' di analisi (foto al secondo).

- detect(): scheda video, processore e memoria. Non importa torch e non apre CUDA (nvidia-smi + metadati dei pacchetti),
  cosi' non occupa memoria video mentre l'analisi e' in corso.
- build_report(): modelli adatti al computer, consiglio e stima di tempo per l'archivio.
- python -m fotoscan.hardware bench: misura reale (sottoprocesso lanciato da /api/hardware/benchmark).

La velocita' dell'analisi e' il minimo fra due limiti: quanto codifica la scheda video (o il processore) e quanto
velocemente si leggono e decodificano le foto. Il benchmark misura entrambi; i modelli non misurati sono estrapolati
dal costo di calcolo (gflops in config.AVAILABLE_MODELS) e marcati come stima.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TARGET_HOURS = 6.0       # un modello e' "adatto" se analizza l'intero archivio entro queste ore
MAX_BATCH = 64
BENCH_FILE = "hardware_bench.json"
_cache: dict | None = None


# ------------------------------------------------------------------ rilevamento
def _run(cmd: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _cpu_name() -> str:
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return " ".join(str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).split())
        except OSError:
            pass
    return platform.processor() or "Processore sconosciuto"


def _cores() -> tuple[int | None, int | None]:
    """(core fisici, thread logici)."""
    logical = os.cpu_count()
    try:
        import psutil

        return psutil.cpu_count(logical=False), psutil.cpu_count(logical=True) or logical
    except Exception:  # noqa: BLE001 - psutil e' facoltativo
        return None, logical


def _ram_gb() -> float | None:
    try:
        import psutil

        return round(psutil.virtual_memory().total / 2**30, 1)
    except Exception:  # noqa: BLE001
        pass
    if os.name == "nt":
        class MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MEMSTAT()
        m.dwLength = ctypes.sizeof(MEMSTAT)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return round(m.ullTotalPhys / 2**30, 1)
    return None


def _nvidia_gpus() -> list[dict]:
    for query in ("name,memory.total,driver_version,compute_cap", "name,memory.total,driver_version"):
        out = _run(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"])
        gpus = []
        for line in out.splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) < 3:
                continue
            try:
                vram = round(float(p[1]) / 1024, 1)
            except ValueError:
                continue
            gpus.append({"name": p[0], "vendor": "nvidia", "vram_gb": vram, "driver": p[2],
                         "compute_cap": p[3] if len(p) > 3 else None})
        if gpus:
            return gpus
    return []


def _other_gpus() -> list[dict]:
    """Schede non NVIDIA (solo per informare: FotoScan accelera solo con CUDA)."""
    if os.name != "nt":
        return []
    out = _run(["powershell", "-NoProfile", "-Command",
                "(Get-CimInstance Win32_VideoController).Name -join \"`n\""], timeout=15)
    return [{"name": n.strip(), "vendor": "altro", "vram_gb": None} for n in out.splitlines()
            if n.strip() and "nvidia" not in n.lower()]


def _torch_info() -> dict:
    try:
        v = metadata.version("torch")
    except metadata.PackageNotFoundError:
        return {"installed": False, "version": None, "cuda_build": False}
    return {"installed": True, "version": v, "cuda_build": "+cu" in v}


def detect(refresh: bool = False) -> dict:
    """Descrizione del computer (in cache per tutta la vita del processo; refresh=True rilegge)."""
    global _cache
    if _cache is not None and not refresh:
        return _cache
    physical, logical = _cores()
    gpus = _nvidia_gpus()
    others = [] if gpus else _other_gpus()
    torch_i = _torch_info()
    mac_arm = platform.system() == "Darwin" and platform.machine() == "arm64"
    if gpus and torch_i["cuda_build"]:
        device = "cuda"
    elif mac_arm and torch_i["installed"]:
        device = "mps"
    else:
        device = "cpu"
    issues = []
    if not torch_i["installed"]:
        issues.append({"code": "torch_mancante", "message": "PyTorch non è installato: l'analisi non può partire."})
    elif gpus and not torch_i["cuda_build"]:
        issues.append({
            "code": "torch_senza_cuda",
            "message": f"Hai una scheda NVIDIA ({gpus[0]['name']}) ma PyTorch è la versione solo processore: "
                       "l'analisi userebbe il processore, molto più lento.",
            "fix": "pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126",
        })
    elif others and device == "cpu":
        issues.append({"code": "gpu_non_supportata",
                       "message": f"La scheda video ({others[0]['name']}) non è NVIDIA: FotoScan userà il processore."})
    best = max(gpus, key=lambda g: g["vram_gb"]) if gpus else None
    _cache = {
        "os": platform.platform(),
        "cpu": {"name": _cpu_name(), "cores": physical, "threads": logical},
        "ram_gb": _ram_gb(),
        "gpus": gpus or others,
        "gpu": best,
        "torch": torch_i,
        "device": device,
        "issues": issues,
    }
    return _cache


def fingerprint(hw: dict) -> str:
    gpu = hw.get("gpu")
    who = f"{gpu['name']}|{gpu['vram_gb']}" if gpu and hw["device"] == "cuda" else hw["cpu"]["name"]
    return f"{hw['device']}|{who}"


# ------------------------------------------------------------------ modelli adatti
def _pow2_floor(n: int) -> int:
    return 1 << (n.bit_length() - 1) if n >= 1 else 0


def batch_for(hw: dict, model: dict) -> int:
    """Foto per volta che stanno nella memoria (0 = il modello non entra)."""
    if hw["device"] == "cuda":
        free = hw["gpu"]["vram_gb"] * 0.75 - 0.7 - model["weights_gb"]   # margine per Windows, contesto CUDA e frammentazione
        if free <= 0:
            return 0
        b = min(MAX_BATCH, int(free / model["act_gb_per_img"]))
        return _pow2_floor(b) if b >= 4 else 0
    ram = hw.get("ram_gb")
    if ram is not None and ram < model["download_gb"] * 1.5 + 2:   # pesi float32 + sistema
        return 0
    return 16


def library_frames(lib: dict, fractions: int) -> tuple[int, int]:
    """(immagini da codificare per tutto l'archivio, per i soli file in attesa): un video vale piu' fotogrammi."""
    total = lib.get("photos", 0) + lib.get("videos", 0) * fractions
    pend = lib.get("pending_photos", 0) + lib.get("pending_videos", 0) * fractions
    return total, pend


def _predict(models: list[dict], measured: dict) -> dict[str, dict]:
    """Velocita' per modello: misurata se presente, altrimenti estrapolata dal modello misurato piu' pesante."""
    out: dict[str, dict] = {}
    ref = None
    for m in sorted(models, key=lambda x: -x["gflops"]):
        r = measured.get(m["id"])
        if r and r.get("encode_per_s"):
            ref = (m, r)
            break
    for m in models:
        r = measured.get(m["id"])
        if r and r.get("encode_per_s"):
            enc, prep, est = r["encode_per_s"], r.get("prep_per_s"), False
        elif r and r.get("error"):
            out[m["id"]] = {"error": r["error"]}
            continue
        elif ref:
            rm, rr = ref
            enc = rr["encode_per_s"] * rm["gflops"] / m["gflops"]
            prep, est = rr.get("prep_per_s"), True
        else:
            continue
        per_s = min(enc, prep) if prep else enc
        out[m["id"]] = {"encode_per_s": round(enc, 2), "prep_per_s": round(prep, 2) if prep else None,
                        "per_s": round(per_s, 2), "estimated": est,
                        "bottleneck": "foto" if prep and prep < enc else "calcolo"}
    return out


def build_report(hw: dict, models: list[dict], lib: dict, bench: dict | None, cached: dict[str, bool],
                 fractions: int = 3) -> dict:
    """Modelli con adattamento, velocita' prevista e tempo per l'archivio; modello consigliato con motivo."""
    measured = (bench or {}).get("results", {}) if bench and not bench.get("stale") else {}
    pred = _predict(models, measured)
    total_frames, pend_frames = library_frames(lib, fractions)
    rows = []
    for m in sorted(models, key=lambda x: x["rank"]):
        b = batch_for(hw, m)
        p = pred.get(m["id"], {})
        per_s = p.get("per_s")
        rows.append({
            "id": m["id"], "label": m["label"], "rank": m["rank"], "fits": b > 0, "batch_size": b, "cached": cached.get(m["id"], False),
            "download_gb": m["download_gb"], **{k: v for k, v in p.items()},
            "eta_total_s": round(total_frames / per_s) if per_s and total_frames else None,
            "eta_pending_s": round(pend_frames / per_s) if per_s and pend_frames else None,
        })
    rec = _recommend(hw, rows)
    for r in rows:
        r["recommended"] = r["id"] == rec["model"]
    return {"models": rows, "recommendation": rec,
            "library": {**lib, "frames_total": total_frames, "frames_pending": pend_frames, "video_frames": fractions}}


def _recommend(hw: dict, rows: list[dict]) -> dict:
    fit = [r for r in rows if r["fits"]]
    gpu = hw.get("gpu")
    if not fit:
        return {"model": rows[0]["id"], "device": "auto", "batch_size": 8, "workers": 4,
                "reason": "Questo computer ha poca memoria: il modello più leggero è l'unico praticabile."}
    timed = [r for r in fit if r.get("eta_total_s") is not None]
    if hw["device"] == "cuda":
        pick = max(fit, key=lambda r: r["rank"])
        why = f"La tua scheda video ({gpu['name']}, {gpu['vram_gb']:g} GB) è adatta al modello più accurato."
        if pick["rank"] < max(r["rank"] for r in rows):
            why = f"Con {gpu['vram_gb']:g} GB di memoria video il modello più accurato non entra: questo è il migliore possibile."
    elif hw["device"] == "mps":
        big = (hw.get("ram_gb") or 0) >= 16
        pick = max((r for r in fit if r["rank"] <= (2 if big else 1)), key=lambda r: r["rank"], default=fit[0])
        why = "Sul chip Apple conviene un modello medio o leggero."
    else:
        pick = min(fit, key=lambda r: r["rank"])
        why = "Senza scheda video NVIDIA conviene il modello più leggero: quelli grandi richiederebbero giorni."
    # Se abbiamo misure, il tempo conta: scegli il piu' accurato che chiude l'archivio entro TARGET_HOURS.
    if timed and hw["device"] != "mps":
        ok = [r for r in timed if r["eta_total_s"] <= TARGET_HOURS * 3600]
        if ok:
            pick = max(ok, key=lambda r: r["rank"])
            why = (f"È il modello più accurato che analizza tutto l'archivio in meno di {TARGET_HOURS:g} ore "
                   "con la velocità misurata su questo computer.")
        else:
            pick = min(timed, key=lambda r: r["eta_total_s"])
            why = "È il più veloce: con gli altri l'analisi dell'archivio durerebbe troppo."
    threads = (hw["cpu"]["threads"] or 4)
    prep_bound = pick.get("bottleneck") == "foto"
    workers = max(2, min(threads, 16 if prep_bound else 8))
    return {"model": pick["id"], "device": "auto", "batch_size": pick["batch_size"] or 8, "workers": workers,
            "reason": why}


# ------------------------------------------------------------------ misure salvate
def bench_path(cache_dir: str) -> Path:
    return Path(cache_dir) / BENCH_FILE


def load_bench(cache_dir: str, hw: dict) -> dict | None:
    try:
        data = json.loads(bench_path(cache_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    data["stale"] = data.get("fingerprint") != fingerprint(hw)   # misure di un altro computer / altra scheda
    return data


# ------------------------------------------------------------------ benchmark (sottoprocesso)
def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _sample_paths(db_path: str, n: int) -> list[str]:
    """Foto a caso dal catalogo: la lettura da disco e' quella vera, non una prova sintetica."""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        try:
            rows = conn.execute("SELECT path FROM files WHERE kind='image' ORDER BY RANDOM() LIMIT ?", (n * 2,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows if os.path.exists(r[0])][:n]


def _bench_model(hw: dict, cfg, model: dict, db_path: str, workers: int) -> dict:
    import gc
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    import numpy as np

    from .embedder import Embedder

    batch = batch_for(hw, model)
    if batch == 0:
        return {"error": "Il modello non entra nella memoria di questo computer."}
    cpu = hw["device"] == "cpu"
    cfg = replace(cfg, model_name=model["id"], batch_size=4 if cpu else batch, workers=workers)
    t0 = time.time()
    emb = Embedder(cfg)
    load_s = time.time() - t0
    try:
        rng = np.random.default_rng(0)
        dummy = [rng.standard_normal((3, emb.size, emb.size)).astype(np.float32) for _ in range(cfg.batch_size)]
        emb.encode_arrays(dummy)                       # riscaldamento (kernel, allocazioni)
        budget = 20.0 if cpu else 5.0
        n, t1 = 0, time.time()
        while True:
            emb.encode_arrays(dummy)
            n += len(dummy)
            if (time.time() - t1 >= budget and n >= 2 * len(dummy)) or n >= 40 * len(dummy):
                break
        encode_per_s = n / (time.time() - t1)
        prep_per_s = None
        paths = _sample_paths(db_path, 48) if db_path else []
        if paths:
            def one(p: str) -> int:
                try:
                    emb.load_prep(p)
                    return 1
                except Exception:  # noqa: BLE001 - file illeggibile: non conta
                    return 0
            t2 = time.time()
            with ThreadPoolExecutor(max_workers=workers) as ex:
                ok = sum(ex.map(one, paths))
            if ok:
                prep_per_s = ok / (time.time() - t2)
        return {"encode_per_s": round(encode_per_s, 2), "prep_per_s": round(prep_per_s, 2) if prep_per_s else None,
                "batch_size": cfg.batch_size, "workers": workers, "load_s": round(load_s, 1),
                "sample_files": len(paths)}
    finally:
        del emb
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass


def bench_main(argv: list[str]) -> int:
    from .config import AVAILABLE_MODELS, load_config

    ap = argparse.ArgumentParser(prog="fotoscan.hardware bench")
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--device", choices=["cuda", "cpu", "mps"], help="forza il dispositivo (prove)")
    a = ap.parse_args(argv)
    cfg = load_config()
    hw = detect(True)
    if a.device and a.device != hw["device"]:
        hw = {**hw, "device": a.device}
        cfg.device = a.device
    workers = a.workers or cfg.workers
    path = bench_path(cfg.cache_dir)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if saved.get("fingerprint") != fingerprint(hw):
        saved = {}
    results = dict(saved.get("results", {}))
    by_id = {m["id"]: m for m in AVAILABLE_MODELS}
    todo = [m for m in a.models if m in by_id]
    for i, mid in enumerate(todo):
        _emit({"event": "start", "model": mid, "index": i, "total": len(todo)})
        try:
            res = _bench_model(hw, cfg, by_id[mid], cfg.db_path, workers)
        except Exception as e:  # noqa: BLE001 - OOM, download mancante...: si registra e si prosegue
            msg = str(e)
            res = {"error": "Memoria video insufficiente." if "out of memory" in msg.lower() else f"{type(e).__name__}: {msg[:200]}"}
        results[mid] = {**res, "at": time.time()}
        path.write_text(json.dumps({"fingerprint": fingerprint(hw), "device": hw["device"], "at": time.time(),
                                    "results": results}, indent=2), encoding="utf-8")
        _emit({"event": "result", "model": mid, "result": results[mid]})
    _emit({"event": "done"})
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "bench":
        raise SystemExit(bench_main(sys.argv[2:]))
    print(json.dumps(detect(True), indent=2, ensure_ascii=False))
