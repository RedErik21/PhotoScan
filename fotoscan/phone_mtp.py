"""Importazione foto/video dal telefono Android (MTP) tramite lo script PowerShell `phone_mtp.ps1`.

Sola lettura sul telefono. I file vengono prima copiati in una cartella di staging, poi spostati nella
destinazione scelta: cosi' nessun file esistente viene mai sovrascritto. Si sposta in destinazione SOLO un file
che lo script dichiara copiato per intero (e la cui dimensione coincide con quella attesa): un file rimasto a
meta' (telefono scollegato, annullamento, timeout) resta nello staging e viene scartato.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

SCRIPT = Path(__file__).with_name("phone_mtp.ps1")
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".dng", ".mp4", ".mov", ".3gp", ".m4v"}
STAGING_NAME = "_fotoscan_tmp_telefono"
TMP_SUFFIX = ".fotoscan-tmp"
LIST_TIMEOUT = 120.0      # secondi: elenco dispositivi e cartelle (un telefono bloccato non risponde mai)
SCAN_TIMEOUT = 1800.0     # elenco ricorsivo dei file: lungo su telefoni con molte foto


class PhoneError(RuntimeError):
    """Problema con il telefono (non collegato, non sbloccato, script fallito)."""


def _powershell() -> str:
    cand = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(cand) if cand.is_file() else "powershell"


def _run(args: dict, timeout: float | None = None, cancelled: Callable[[], bool] | None = None) -> Iterator[dict]:
    """Esegue lo script con `args` (JSON in un file temporaneo: una variabile d'ambiente e' limitata a 32 KB)
    e restituisce i messaggi JSON uno per riga. `timeout`: secondi totali; `cancelled`: interrompe lo script
    anche quando non arrivano messaggi (telefono fermo)."""
    if sys.platform != "win32":
        # Linux: il telefono e' montato in gvfs (GNOME/KDE), nessuno script esterno (vedi phone_linux.py)
        from . import phone_linux
        yield from phone_linux.run(args)
        return
    argfile = Path(tempfile.gettempdir()) / f"fotoscan_phone_{uuid.uuid4().hex}.json"
    argfile.write_text(json.dumps(args, ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ, FOTOSCAN_PHONE_ARGS_FILE=str(argfile))
    env.pop("FOTOSCAN_PHONE_ARGS", None)
    try:
        proc = subprocess.Popen(
            [_powershell(), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
             "-File", str(SCRIPT)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),  # nessuna finestra per ogni chiamata
        )
    except OSError as e:
        argfile.unlink(missing_ok=True)
        raise PhoneError(f"PowerShell non disponibile: {e}") from e
    _current.append(proc)
    lines: queue.Queue = queue.Queue()

    def pump(stream, tag: str) -> None:  # legge stdout e stderr insieme: nessuno dei due puo' riempire il pipe
        try:
            for line in stream:
                lines.put((tag, line))
        except (OSError, ValueError):
            pass
        lines.put((tag, None))

    for stream, tag in ((proc.stdout, "out"), (proc.stderr, "err")):
        threading.Thread(target=pump, args=(stream, tag), daemon=True, name=f"phone-{tag}").start()
    deadline = time.monotonic() + timeout if timeout else None
    open_streams, err_lines, stopped = 2, [], False
    try:
        while open_streams:
            try:
                tag, line = lines.get(timeout=0.5)
            except queue.Empty:
                if cancelled and cancelled():
                    stopped = True
                    break
                if deadline is not None and time.monotonic() > deadline:
                    raise PhoneError("Il telefono non risponde (tempo scaduto): sbloccalo, lascia lo schermo acceso "
                                     "e controlla il cavo, poi riprova.")
                continue
            if line is None:
                open_streams -= 1
                continue
            if tag == "err":
                err_lines.append(line)
                continue
            line = line.strip().lstrip("\ufeff")
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # riga di servizio di PowerShell (avvisi): non e' un messaggio dello script
            yield msg
        if not stopped:
            try:
                rc = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = -1
            if rc != 0:
                raise PhoneError(_clean_error("".join(err_lines)) or f"script terminato con codice {rc}")
    finally:
        if proc in _current:
            _current.remove(proc)
        if proc.poll() is None:
            proc.kill()
        argfile.unlink(missing_ok=True)


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
    out = [m for m in _run({"action": "list"}, timeout=LIST_TIMEOUT)]
    return out[0].get("devices", []) if out else []


def list_folder(device: str, storage: str, path: list[str]) -> dict:
    """Contenuto di una cartella del telefono: sottocartelle con conteggio foto/video."""
    out = [m for m in _run({"action": "list", "device": device, "storage": storage, "path": path}, timeout=LIST_TIMEOUT)]
    if not out:
        raise PhoneError("Nessuna risposta dal telefono.")
    return out[-1]


@dataclass
class ImportResult:
    total_seen: int = 0          # file multimediali trovati nelle cartelle scelte
    copied: int = 0             # copiati dal telefono
    new_files: int = 0          # effettivamente aggiunti alla destinazione
    skipped_existing: int = 0   # gia' presenti rilevati dopo la copia (stesso nome e stesso contenuto in destinazione)
    skipped_known: int = 0      # gia' presenti rilevati PRIMA della copia: non trasferiti
    errors: list[str] = field(default_factory=list)
    dest: str = ""
    aborted: str = ""           # motivo se l'importazione si e' fermata prima della fine (telefono scollegato...)


def scan_folders(device: str, storage: str, folders: list[list[str]]) -> list[dict]:
    """Elenco dei file multimediali delle cartelle scelte (nome, dimensione, percorso relativo). Nessuna copia."""
    out = [m for m in _run({"action": "scan", "device": device, "storage": storage, "folders": folders},
                           timeout=SCAN_TIMEOUT)]
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
            try:
                if p.is_file() and STAGING_NAME not in p.parts and p.suffix.lower() in MEDIA_EXT:
                    dest_keys.add(_key(p.name, p.stat().st_size))
            except OSError:
                continue
    return catalog, dest_keys


def classify(items: list[dict], catalog: set, dest_keys: set) -> dict:
    """Divide i file in nuovi e gia' presenti (stesso nome e stessa dimensione). Nessun confronto sul contenuto:
    due foto diverse possono avere nome e dimensione uguali. Chi importa "tutte" non rischia nulla, perche'
    la copia in destinazione confronta il contenuto e salta solo i file davvero identici."""
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
        try:
            for msg in _run({"action": "copy", "device": device, "storage": storage, "folders": folders,
                             "staging": str(staging), "skip": sorted(skip or [])}, cancelled=cancelled):
                if cancelled and cancelled():
                    cancel_all()
                    break
                kind = msg.get("type")
                if kind == "total":
                    res.total_seen = int(msg.get("n", 0))
                    res.skipped_known = int(msg.get("skipped", 0))
                    total_bytes = int(msg.get("bytes", 0))
                elif kind == "file":
                    rel = str(msg.get("rel", ""))
                    if msg.get("ok"):
                        # spostamento immediato: il file e' gia' in destinazione anche se la copia si interrompe dopo
                        if _accept(msg, staging, dest, res):
                            res.copied += 1
                            bytes_done += int(msg.get("bytes", 0))
                    else:
                        res.errors.append(f"{rel}: {msg.get('error') or 'copia non riuscita'}")
                    if progress:
                        i = int(msg.get("i", 0))
                        if total_bytes > 0:
                            progress(bytes_done, total_bytes, rel, i, res.total_seen)
                        else:
                            progress(i, res.total_seen, rel, i, res.total_seen)
        except PhoneError as e:
            # telefono scollegato o bloccato a meta': quello che e' gia' al suo posto resta, il resto si riprova
            res.aborted = str(e)
    finally:
        # quanto resta nello staging e' incompleto (o non dichiarato copiato): si scarta, mai spostato in destinazione
        shutil.rmtree(staging, ignore_errors=True)
    return res


def _accept(msg: dict, staging: Path, dest: Path, res: ImportResult) -> bool:
    """File dichiarato copiato: controlla che sia dentro lo staging e della dimensione attesa, poi lo piazza."""
    rel = str(msg.get("rel", ""))
    src = (staging / Path(rel)).resolve()
    try:
        src.relative_to(staging.resolve())
    except ValueError:
        res.errors.append(f"{rel}: percorso non valido")
        return False
    if not src.is_file():
        res.errors.append(f"{rel}: file non trovato dopo la copia")
        return False
    actual = src.stat().st_size
    expected = int(msg.get("size", -1)) if msg.get("size") is not None else -1
    if (expected >= 0 and actual != expected) or actual != int(msg.get("bytes", actual)):
        src.unlink(missing_ok=True)
        res.errors.append(f"{rel}: copia incompleta ({actual} byte)")
        return False
    try:
        _place(src, staging.resolve(), dest, res)
    except OSError as e:  # un errore su un file (disco pieno, permessi) non deve fermare gli altri
        res.errors.append(f"{rel}: {e}")
        return False
    return True


def _content_hash(p: Path) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(p, "rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _same_content(a: Path, b: Path) -> bool:
    return a.stat().st_size == b.stat().st_size and _content_hash(a) == _content_hash(b)


def _publish(tmp: Path, target: Path) -> None:
    """Rende definitivo tmp come target senza mai sovrascrivere: FileExistsError se target e' comparso nel frattempo."""
    if os.name == "nt":
        os.rename(tmp, target)  # su Windows rename non sovrascrive
        return
    try:
        os.link(tmp, target)
    except FileExistsError:
        raise
    except OSError:  # condivisioni senza hard link
        if os.path.lexists(target):
            raise FileExistsError(str(target)) from None
        os.rename(tmp, target)
        return
    os.unlink(tmp)


def _place(src: Path, staging: Path, dest: Path, res: ImportResult) -> None:
    """Sposta un file dallo staging alla destinazione con le regole di sicurezza (mai sovrascrivere).

    Stesso nome gia' presente: se il contenuto e' identico il file e' un duplicato e si salta; altrimenti si cerca
    la variante _2, _3... (se una di queste e' identica e' la stessa foto importata in precedenza: si salta).
    Il file arriva con un nome temporaneo e diventa definitivo solo a copia finita: niente file a meta'."""
    if not src.is_file() or src.suffix.lower() not in MEDIA_EXT:
        return
    target = dest / src.relative_to(staging)
    target.parent.mkdir(parents=True, exist_ok=True)
    cand, n = target, 1
    while True:
        if cand.exists():
            if _same_content(cand, src):
                res.skipped_existing += 1
                src.unlink(missing_ok=True)
                return
            n += 1
            cand = target.with_name(f"{target.stem}_{n}{target.suffix}")
            continue
        tmp = cand.with_name(f"{cand.name}.{uuid.uuid4().hex[:8]}{TMP_SUFFIX}")
        try:
            shutil.move(str(src), str(tmp))
            try:
                _publish(tmp, cand)
            except FileExistsError:  # comparso durante la copia: si riprova con il nome successivo
                shutil.move(str(tmp), str(src))
                continue
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        res.new_files += 1
        return
