"""Finestra di PhotoScan-Setup: mostra l'hardware, propone runtime e modello, installa e avvia."""
from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from setup_photoscan import (
    MODELS,
    detect_hardware,
    download_parts,
    extract,
    installed_state,
    latest_release,
    package_parts,
    recommend,
    space_warning,
    create_desktop_shortcut,
    write_config,
    write_install_marker,
)

RUNTIME_LABELS = {
    "gpu": "GPU NVIDIA (CUDA), più veloce, circa 3 GB",
    "cpu": "solo CPU, più lento, circa 1 GB",
}


def data_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "PhotoScan"


def run_gui() -> int:
    root_dir = data_root()
    root_dir.mkdir(parents=True, exist_ok=True)
    hw = detect_hardware(root_dir)
    rec_runtime, rec_model = recommend(hw)
    state = installed_state(root_dir)

    win = tk.Tk()
    win.title("PhotoScan — installazione")
    win.geometry("560x520")
    win.resizable(False, False)
    frame = ttk.Frame(win, padding=16)
    frame.pack(fill="both", expand=True)

    gpu_txt = f"{hw.gpu_name} ({hw.vram_mb // 1024} GB VRAM)" if hw.gpu_name else "nessuna GPU NVIDIA rilevata"
    ttk.Label(frame, text="Il tuo computer", font=("Segoe UI", 12, "bold")).pack(anchor="w")
    ttk.Label(frame, text=f"CPU: {hw.cores} core   RAM: {hw.ram_gb:.0f} GB   GPU: {gpu_txt}").pack(anchor="w")
    ttk.Label(frame, text=f"Spazio libero: {hw.free_disk_gb:.0f} GB").pack(anchor="w", pady=(0, 10))

    if state:
        ttk.Label(frame, text=f"Già installato: PhotoScan {state.get('version')} "
                              f"({state.get('runtime')}, {state.get('model')})").pack(anchor="w", pady=(0, 10))

    runtime_var = tk.StringVar(value=rec_runtime)
    model_var = tk.StringVar(value=rec_model)

    ttk.Label(frame, text="Programma", font=("Segoe UI", 10, "bold")).pack(anchor="w")
    for key, text in RUNTIME_LABELS.items():
        suffix = "  (consigliato)" if key == rec_runtime else ""
        ttk.Radiobutton(frame, text=text + suffix, value=key, variable=runtime_var).pack(anchor="w")

    ttk.Label(frame, text="Modello di riconoscimento (scaricato al primo avvio)",
              font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(10, 0))
    for key, m in MODELS.items():
        suffix = "  (consigliato)" if key == rec_model else ""
        ttk.Radiobutton(frame, text=f"{m['label']} — download ~{m['download_gb']} GB{suffix}",
                        value=key, variable=model_var).pack(anchor="w")

    warn = ttk.Label(frame, text="", foreground="#b45309", wraplength=520)
    warn.pack(anchor="w", pady=(10, 0))

    def refresh_warning(*_):
        w = space_warning(hw, runtime_var.get(), model_var.get())
        warn.config(text=w or "")

    runtime_var.trace_add("write", refresh_warning)
    model_var.trace_add("write", refresh_warning)
    refresh_warning()

    status = ttk.Label(frame, text="Pronto.")
    status.pack(anchor="w", pady=(12, 4))
    bar = ttk.Progressbar(frame, maximum=100)
    bar.pack(fill="x")

    buttons = ttk.Frame(frame)
    buttons.pack(anchor="e", pady=(16, 0))
    install_btn = ttk.Button(buttons, text="Installa")
    launch_btn = ttk.Button(buttons, text="Avvia PhotoScan")
    install_btn.pack(side="left", padx=4)
    launch_btn.pack(side="left", padx=4)
    launch_btn.state(["disabled"])

    def set_progress(frac: float, text: str) -> None:
        win.after(0, lambda: (bar.config(value=frac * 100), status.config(text=text)))

    def job() -> None:
        try:
            runtime, model = runtime_var.get(), model_var.get()
            set_progress(0, "Controllo la versione più recente…")
            release = latest_release()
            version = release["tag_name"].lstrip("vV")
            app_dir = root_dir / "app"
            same = state and state.get("version") == version and state.get("runtime") == runtime
            if not same:
                parts = package_parts(release.get("assets", []), version, runtime)
                if not parts:
                    raise RuntimeError(f"La Release {release['tag_name']} non ha il pacchetto «{runtime}».")
                dl = root_dir / "download"
                zip_path = download_parts(parts, dl, set_progress)
                set_progress(1.0, "Installazione…")
                extract(zip_path, app_dir, set_progress)
                shutil_rm(dl)
            write_config(root_dir / "config.json", MODELS[model]["id"])
            write_install_marker(root_dir, version, runtime, model)
            create_desktop_shortcut(app_dir / "PhotoScan.exe")
            win.after(0, lambda: finish(app_dir))
        except Exception as e:  # mostra l'errore all'utente, non chiudere in silenzio
            win.after(0, lambda: fail(str(e)))

    def finish(app_dir: Path) -> None:
        bar.config(value=100)
        status.config(text="Installazione completata. Il modello verrà scaricato al primo avvio.")
        install_btn.state(["disabled"])
        launch_btn.state(["!disabled"])
        launch_btn.config(command=lambda: subprocess.Popen([str(app_dir / "PhotoScan.exe")], cwd=str(app_dir)))

    def fail(msg: str) -> None:
        status.config(text="Installazione non riuscita.")
        install_btn.state(["!disabled"])
        messagebox.showerror("PhotoScan", msg)

    def start() -> None:
        install_btn.state(["disabled"])
        threading.Thread(target=job, daemon=True).start()

    install_btn.config(command=start)
    win.mainloop()
    return 0


def shutil_rm(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)
