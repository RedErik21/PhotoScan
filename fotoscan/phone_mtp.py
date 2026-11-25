"""Importazione foto/video dal telefono Android (MTP) tramite lo script PowerShell `phone_mtp.ps1`.

Sola lettura sul telefono. I file vengono prima copiati in una cartella di staging, poi spostati nella
destinazione scelta: cosi' nessun file esistente viene mai sovrascritto e i duplicati (stesso nome e
stessa dimensione) vengono saltati.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

SCRIPT = Path(__file__).with_name("phone_mtp.ps1")
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".dng", ".mp4", ".mov", ".3gp", ".m4v"}
STAGING_NAME = "_fotoscan_tmp_telefono"


class PhoneError(RuntimeError):
    """Problema con il telefono (non collegato, non sbloccato, script fallito)."""


def _run(args: dict, timeout: float | None = None) -> Iterator[dict]:
    """Esegue lo script con `args` (JSON nella variabile d'ambiente) e restituisce i messaggi JSON uno per riga."""
    if sys.platform != "win32":
        # Linux: il telefono e' montato in gvfs (GNOME/KDE), nessuno script esterno (vedi phone_linux.py)
        from . import phone_linux
        yield from phone_linux.run(args)
        return
    env = dict(os.environ, FOTOSCAN_PHONE_ARGS=json.dumps(args, ensure_ascii=False))
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),  # nessuna finestra per ogni chiamata
    )
    _current.append(proc)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line:
                yield json.loads(line)
        err = proc.stderr.read() if proc.stderr else ""
        rc = proc.wait(timeout=timeout)
        if rc != 0:
            raise PhoneError(_clean_error(err) or f"script terminato con codice {rc}")
    finally:
        if proc in _current:
            _current.remove(proc)
        if proc.poll() is None:
            proc.kill()


_current: list[subprocess.Popen] = []


def cancel_all() -> None:
    """Interrompe eventuali script in corso (chiamato all'annullamento del job)."""
    for p in list(_current):
        try:
            p.kill()
        except OSError:
            pass


def _clean_error(err: str) -> str:
    lines = [l.strip() for l in err.splitlines() if l.strip() and not l.strip().startswith(("At ", "+", "CategoryInfo", "FullyQualified"))]
    return lines[0] if lines else ""


def list_devices() -> list[dict]:
    """Telefoni collegati e relativi storage (es. 'Memoria condivisa interna')."""
    out = [m for m in _run({"action": "list"})]
    return out[0].get("devices", []) if out else []


def list_folder(device: str, storage: str, path: list[str]) -> dict:
    """Contenuto di una cartella del telefono: sottocartelle con conteggio foto/video."""
    out = [m for m in _run({"action": "list", "device": device, "storage": storage, "path": path})]
    if not out:
        raise PhoneError("Nessuna risposta dal telefono.")
    return out[-1]


@dataclass
class ImportResult:
    total_seen: int = 0          # file multimediali trovati nelle cartelle scelte
    copied: int = 0             # copiati dal telefono
    new_files: int = 0          # effettivamente aggiunti alla destinazione
    skipped_existing: int = 0   # gia' presenti rilevati dopo la copia (stesso nome e dimensione in destinazione)
    skipped_known: int = 0      # gia' presenti rilevati PRIMA della copia: non trasferiti
    errors: list[str] = field(default_factory=list)
    dest: str = ""


def scan_folders(device: str, storage: str, folders: list[list[str]]) -> list[dict]:
    """Elenco dei file multimediali delle cartelle scelte (nome, dimensione, percorso relativo). Nessuna copia."""
    out = [m for m in _run({"action": "scan", "device": device, "storage": storage, "folders": folders})]
    for m in out:
        if m.get("type") == "scan":
            return list(m.get("items", []))
    raise PhoneError("Nessuna risposta dal telefono.")


def _key(name: str, size: int) -> tuple[str, int]:
    return (name.lower(), int(size))


def existing_keys(conn, dest: Path | None) -> tuple[set, set]:
    """Chiavi (nome, dimensione) gia' presenti: nel catalogo (archivio) e nella cartella di destinazione."""
    catalog = {_key(r[0], r[1]) for r in conn.execute("SELECT name, size FROM files WHERE size IS NOT NULL") if r[0]}
    dest_keys: set = set()
    if dest is not None and dest.is_dir():
        for p in dest.rglob("*"):
            if p.is_file() and STAGING_NAME not in p.parts and p.suffix.lower() in MEDIA_EXT:
                dest_keys.add(_key(p.name, p.stat().st_size))
    return catalog, dest_keys


def classify(items: list[dict], catalog: set, dest_keys: set) -> dict:
    """Divide i file in nuovi e gia' presenti (stesso nome e stessa dimensione). Nessun confronto sul contenuto."""
    new, known = [], []
    for it in items:
        size = int(it.get("size", -1))
        if size < 0:
            new.append(it)  # dimensione sconosciuta: non si puo' confrontare, si copia
            continue
        k = _key(it["name"], size)
        if k in catalog:
            known.append({**it, "reason": "archivio"})
        elif k in dest_keys:
            known.append({**it, "reason": "destinazione"})
        else:
            new.append(it)
    return {"new": new, "known": known}


def copy_folders(
    device: str,
    storage: str,
    folders: list[list[str]],
    dest: Path,
    progress: Callable[[int, int, str, int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    skip: set[str] | None = None,
) -> ImportResult:
    """Copia i file multimediali delle cartelle scelte in `dest`, mantenendo la struttura (es. DCIM/Camera/...).

    progress(done, total, rel, count, n): done/total in byte se la dimensione totale e' nota, altrimenti in file;
    count/n sono sempre file copiati / file totali (per il testo della barra).
    """
    res = ImportResult(dest=str(dest))
    dest.mkdir(parents=True, exist_ok=True)
    staging = dest / STAGING_NAME
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    total_bytes = 0
    bytes_done = 0
    try:
        for msg in _run({"action": "copy", "device": device, "storage": storage, "folders": folders,
                         "staging": str(staging), "skip": sorted(skip or [])}):
            if cancelled and cancelled():
                cancel_all()
                break
            kind = msg.get("type")
            if kind == "total":
                res.total_seen = int(msg.get("n", 0))
                res.skipped_known = int(msg.get("skipped", 0))
                total_bytes = int(msg.get("bytes", 0))
            elif kind == "file":
                if msg.get("ok"):
                    res.copied += 1
                    bytes_done += int(msg.get("bytes", 0))
                    # spostamento immediato: il file e' gia' in destinazione anche se la copia si interrompe dopo
                    _place(staging / Path(str(msg.get("rel", ""))), staging, dest, res)
                else:
                    res.errors.append(f"{msg.get('rel')}: {msg.get('error') or 'copia non riuscita'}")
                if progress:
                    i = int(msg.get("i", 0))
                    if total_bytes > 0:
                        progress(bytes_done, total_bytes, str(msg.get("rel", "")), i, res.total_seen)
                    else:
                        progress(i, res.total_seen, str(msg.get("rel", "")), i, res.total_seen)
        _move_into_dest(staging, dest, res)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return res


def _place(src: Path, staging: Path, dest: Path, res: ImportResult) -> None:
    """Sposta un file dallo staging alla destinazione con le regole di sicurezza (mai sovrascrivere)."""
    if not src.is_file() or src.suffix.lower() not in MEDIA_EXT:
        return
    target = dest / src.relative_to(staging)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.stat().st_size == src.stat().st_size:
            res.skipped_existing += 1
            src.unlink(missing_ok=True)
            return
        target = _unique(target)
    shutil.move(str(src), str(target))
    res.new_files += 1


def _move_into_dest(staging: Path, dest: Path, res: ImportResult) -> None:
    """Sposta eventuali file rimasti nello staging (di norma nessuno: ogni file viene spostato appena copiato)."""
    for src in sorted(p for p in staging.rglob("*") if p.is_file()):
        _place(src, staging, dest, res)


def _unique(p: Path) -> Path:
    n = 2
    while True:
        cand = p.with_name(f"{p.stem}_{n}{p.suffix}")
        if not cand.exists():
            return cand
        n += 1
