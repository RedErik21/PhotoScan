"""Sorgente su disco di rete (NAS SMB/NFS) e stabilita' del catalogo.

Il catalogo memorizza percorsi assoluti (`files.path`). Se la stessa cartella cambia nome di
accesso (Z:\\ -> \\\\NAS\\foto, /mnt/nas -> /media/nas), `rebase_catalog` riscrive il prefisso
invece di reindicizzare tutto. Il protocollo non conta: l'app vede solo un percorso.
"""
from __future__ import annotations

import errno
import os
import random
import sys
import sqlite3

# Tipi di file system di rete in /proc/mounts (Linux).
_NET_FSTYPES = {"cifs", "smb3", "smbfs", "nfs", "nfs4"}
# errno tollerati durante la scansione di rete: permessi negati o file spariti sono normali.
_BENIGN_ERRNO = {errno.EACCES, errno.EPERM, errno.ENOENT}
# Riallineamento del catalogo: percentuale minima di file campione ritrovati nella nuova radice. Alta di proposito:
# riscrivere i percorsi di un catalogo solo in parte presente farebbe sembrare "spariti" gli altri file.
REBASE_MIN_PERCENT = 95


def norm_root(path: str) -> str:
    """Percorso assoluto senza separatore finale; conserva maiuscole e separatori del sistema."""
    p = os.path.normpath(os.path.abspath(path))
    while len(p) > 1 and p[-1] in "\\/" and not p.endswith((":\\", ":/")):  # tiene "C:\" e "/"
        p = p[:-1]
    return p


def is_network(path: str) -> bool:
    """True se il percorso sta su un disco di rete (UNC, unita' mappata, mount SMB/NFS)."""
    p = os.path.abspath(path)
    if os.name == "nt":
        if p.startswith("\\\\?\\UNC\\") or (p.startswith("\\\\") and not p.startswith("\\\\?\\")):
            return True
        drive = os.path.splitdrive(p)[0]
        if len(drive) == 2 and drive[1] == ":":
            try:
                import ctypes
                return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE
            except Exception:
                return False
        return False
    if sys.platform.startswith("linux"):
        best, fstype = "", ""
        try:
            with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    mnt = parts[1].replace("\\040", " ")
                    inside = mnt == "/" or p == mnt or p.startswith(mnt.rstrip("/") + "/")
                    if inside and len(mnt) > len(best):
                        best, fstype = mnt, parts[2]
        except OSError:
            return False
        return fstype in _NET_FSTYPES
    return False


def network_mount_missing(path: str) -> bool:
    """Linux: il percorso sta sotto un mount di rete previsto in /etc/fstab che ora NON e' montato.
    In quel caso la cartella esiste ma e' vuota e sul disco locale: scriverci sembrerebbe un backup sul NAS."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        p = os.path.realpath(path)
        with open("/etc/fstab", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split("#", 1)[0].split()
                if len(parts) < 3 or parts[2] not in _NET_FSTYPES:
                    continue
                mnt = os.path.realpath(parts[1].replace("\\040", " "))
                if (p == mnt or p.startswith(mnt.rstrip("/") + "/")) and not os.path.ismount(mnt):
                    return True
    except OSError:
        return False
    return False


def reachable(path: str) -> bool:
    """La cartella risponde adesso (legge al massimo una voce)."""
    try:
        with os.scandir(path) as it:
            next(it, None)
        return True
    except OSError:
        return False


def fatal_walk_error(err: OSError) -> bool:
    """Un errore di lettura durante la scansione di rete che rende la lista incompleta."""
    return err.errno not in _BENIGN_ERRNO


def rebase_catalog(conn: sqlite3.Connection, old_root: str, new_root: str) -> int:
    """Riscrive il prefisso dei percorsi da old_root a new_root se la nuova radice contiene gli stessi file.

    Verifica a campione (taglia e presenza) prima di toccare il catalogo: se la nuova cartella non
    corrisponde, non cambia nulla e ritorna 0. Ritorna il numero di percorsi riscritti.
    """
    old_n, new_n = norm_root(old_root), norm_root(new_root)
    if old_n == new_n:
        return 0
    prefix = os.path.normcase(old_n) + os.sep
    rows = [r for r in conn.execute("SELECT id, path, size FROM files")
            if os.path.normcase(r["path"]).startswith(prefix)]
    if not rows:
        return 0
    sample = random.Random(0).sample(rows, min(60, len(rows)))
    ok = 0
    for r in sample:
        candidate = new_n + r["path"][len(old_n):]
        try:
            if os.path.isfile(candidate) and os.path.getsize(candidate) == r["size"]:
                ok += 1
        except OSError:
            pass
    if ok * 100 < len(sample) * REBASE_MIN_PERCENT:  # troppi file campione mancanti: non e' la stessa cartella
        return 0
    moves = [(new_n + r["path"][len(old_n):], r["id"]) for r in rows]
    # ogni nuovo percorso deve restare dentro la nuova radice (niente "..", niente radici diverse)
    new_prefix = os.path.normcase(os.path.normpath(new_n)).rstrip("\\/") + os.sep
    if any(not os.path.normcase(os.path.normpath(p)).startswith(new_prefix) for p, _ in moves):
        return 0
    try:
        conn.executemany("UPDATE files SET path=? WHERE id=?", moves)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        return 0
    return len(moves)
