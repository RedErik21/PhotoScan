"""Accesso al telefono su Linux, senza script PowerShell.

GNOME e KDE montano da soli Android (MTP) e iPhone (AFC) in gvfs, come cartelle normali sotto
    $XDG_RUNTIME_DIR/gvfs/   (di norma /run/user/<uid>/gvfs/)
Qui si legge quella cartella in sola lettura e si emettono gli stessi messaggi JSON dello script Windows
(phone_mtp.ps1), cosi' il resto del programma non cambia.

Sola lettura: nessun file del telefono viene scritto o cancellato; si copia solo verso lo staging.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterator

from .phone_mtp import MEDIA_EXT, PhoneError

SKIP_TOP = {"Android"}          # cartelle di sistema di Android: mai mostrate in cima
APPLE_STORAGE = "Fotocamera"    # nome mostrato per l'iPhone (la radice e' la cartella DCIM)


def gvfs_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "gvfs"


def _devices() -> list[dict]:
    """Telefoni montati in gvfs: nome, storage (cartelle di primo livello) e percorso di ogni storage."""
    root = gvfs_dir()
    if not root.is_dir():
        raise PhoneError("Il sistema non espone i telefoni (gvfs non attivo). Su GNOME o KDE collega il telefono "
                         "e sbloccalo: compare da solo tra i dischi.")
    out: list[dict] = []
    for entry in sorted(root.iterdir()):
        name = entry.name
        if name.startswith("mtp:") or name.startswith("gphoto2:"):
            storages = {}
            try:
                children = sorted(entry.iterdir())
            except OSError:
                children = []  # telefono montato ma bloccato: nessuna memoria visibile finche' non lo sblocchi
            for child in children:
                if child.is_dir():
                    storages[child.name] = child
            out.append({"name": _pretty(name), "storages": list(storages), "apple": False,
                        "_paths": storages, "_key": name})
        elif name.startswith("afc:"):   # iPhone
            out.append({"name": _pretty(name), "storages": [APPLE_STORAGE], "apple": True,
                        "_paths": {APPLE_STORAGE: entry}, "_key": name})
    return out


def _pretty(gvfs_name: str) -> str:
    """'mtp:host=SAMSUNG_SM-G991B_R58' -> 'SAMSUNG SM-G991B'."""
    host = gvfs_name.split("host=", 1)[-1].split(",")[0]
    return host.replace("_", " ").strip() or gvfs_name


def _find(device: str, storage: str) -> Path:
    for d in _devices():
        if d["name"] == device:
            p = d["_paths"].get(storage)
            if p is None:
                raise PhoneError(f"Memoria «{storage}» non trovata sul telefono.")
            return p
    raise PhoneError(f"Telefono non collegato o non sbloccato: {device}")


def _safe_parts(parts: list[str]) -> list[str]:
    for p in parts:
        if not p or p in (".", "..") or "/" in p or "\\" in p:
            raise PhoneError("Percorso non valido.")
    return list(parts)


def _is_media(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in MEDIA_EXT


def _direct_media(folder: Path) -> tuple[bool, int]:
    sub, direct = False, 0
    try:
        for c in folder.iterdir():
            if c.is_dir():
                sub = True
            elif _is_media(c):
                direct += 1
    except OSError:
        pass
    return sub, direct


def list_payload(device: str, storage: str, path: list[str]) -> dict:
    base = _find(device, storage)
    parts = _safe_parts(path)
    node = base.joinpath(*parts)
    if not node.is_dir():
        raise PhoneError("Cartella non trovata sul telefono.")
    folders, media = [], 0
    for child in sorted(node.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir():
            if not parts and child.name in SKIP_TOP:
                continue
            if not parts and not child.name.startswith("DCIM") and storage != APPLE_STORAGE:
                continue
            sub, direct = _direct_media(child)
            folders.append({"name": child.name, "media": direct, "sub": sub})
        elif _is_media(child):
            media += 1
    return {"storage": storage, "path": parts, "media": media, "folders": folders}


def _collect(base: Path, rel_base: list[str]) -> list[dict]:
    """Tutti i file multimediali sotto `base`, con percorso relativo e dimensione."""
    found: list[dict] = []
    stack = [(base, rel_base)]
    while stack:
        folder, rel = stack.pop()
        try:
            children = sorted(folder.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for c in children:
            if c.is_dir():
                stack.append((c, rel + [c.name]))
            elif _is_media(c):
                try:
                    size = c.stat().st_size
                except OSError:
                    size = -1
                found.append({"path": c, "rel": rel + [c.name], "name": c.name, "size": size})
    return found


def _folder_list(device: str, storage: str, folders: list[list[str]]) -> list[dict]:
    base = _find(device, storage)
    items: list[dict] = []
    for f in folders:
        parts = _safe_parts(f)
        node = base.joinpath(*parts)
        if node.is_dir():
            items.extend(_collect(node, parts))
    return items


def run(args: dict) -> Iterator[dict]:
    """Stessi messaggi di phone_mtp.ps1 per le azioni list / scan / copy. Gli errori di lettura (telefono
    scollegato o bloccato, gvfs che non risponde) diventano PhoneError, con un messaggio comprensibile."""
    try:
        yield from _run_action(args)
    except OSError as e:
        raise PhoneError(f"Il telefono non risponde: {e}. Sbloccalo, controlla il cavo e riprova.") from e


def _device_present(device: str, storage: str) -> bool:
    try:
        _find(device, storage)
        return True
    except (PhoneError, OSError):
        return False


def _run_action(args: dict) -> Iterator[dict]:
    action = args.get("action")
    device, storage = args.get("device", ""), args.get("storage", "")
    if action == "list" and not device:
        yield {"devices": [{"name": d["name"], "storages": d["storages"], "apple": d["apple"]} for d in _devices()]}
        return
    if action == "list":
        yield list_payload(device, storage, args.get("path") or [])
        return
    if action == "scan":
        items = _folder_list(device, storage, args.get("folders") or [])
        yield {"type": "scan", "items": [{"rel": "/".join(i["rel"]), "name": i["name"], "size": i["size"]}
                                         for i in items]}
        return
    if action == "copy":
        items = _folder_list(device, storage, args.get("folders") or [])
        skip = set(args.get("skip") or [])
        staging = Path(args["staging"])
        kept, skipped = [], 0
        for it in items:
            if "/".join(it["rel"]) in skip:
                skipped += 1
            else:
                kept.append(it)
        total_bytes = sum(i["size"] for i in kept if i["size"] >= 0)
        if any(i["size"] < 0 for i in kept):
            total_bytes = -1
        yield {"type": "total", "n": len(kept), "bytes": total_bytes, "skipped": skipped}
        fails = 0
        for idx, it in enumerate(kept, start=1):
            rel = "/".join(it["rel"])
            dst = staging.joinpath(*it["rel"])
            actual = 0
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(it["path"], dst)   # sola lettura sul telefono: si copia e basta
                actual = dst.stat().st_size
                if it["size"] >= 0 and actual != it["size"]:
                    raise OSError(f"dimensione diversa ({actual} di {it['size']} byte)")
                ok, err = True, None
            except OSError as e:
                ok, err = False, str(e)
                dst.unlink(missing_ok=True)   # un file a meta' non deve restare nello staging
                actual = 0
            yield {"type": "file", "i": idx, "rel": rel, "ok": ok, "bytes": actual, "size": it["size"], "error": err}
            fails = 0 if ok else fails + 1
            if fails >= 3 and not _device_present(device, storage):
                raise PhoneError("Telefono scollegato o bloccato: importazione interrotta. "
                                 "Sbloccalo, ricollega il cavo e riprova.")
        yield {"type": "done"}
        return
    raise PhoneError(f"Azione non supportata: {action}")
