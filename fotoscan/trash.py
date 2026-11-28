"""Cestino del sistema: sposta un file nel Cestino, mai una cancellazione definitiva.

- Windows: IFileOperation con FOFX_RECYCLEONDELETE, che FALLISCE invece di cancellare quando il file non puo'
  andare nel Cestino (Cestino disattivato sul volume, file piu' grande della capienza). Se IFileOperation non e'
  utilizzabile si ripiega su SHFileOperation, ma solo dopo aver verificato che il volume abbia il Cestino attivo
  e spazio sufficiente.
- Linux: Cestino freedesktop (`gio trash` se c'e', altrimenti implementazione XDG con file e .trashinfo).
- Percorsi di rete rifiutati: li' il Cestino non esiste e il sistema cancellerebbe davvero.

Ogni errore e' un TrashError (OSError) con messaggio in italiano. Dopo l'operazione si verifica che il file
non sia piu' al suo posto.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote


class TrashError(OSError):
    """Spostamento nel Cestino non riuscito (il file non e' stato eliminato)."""


def send_to_trash(path: str) -> None:
    """Sposta `path` nel Cestino. Solleva TrashError se non e' possibile: in quel caso il file resta dov'e'."""
    full = os.path.abspath(path)
    if not os.path.lexists(full):
        raise TrashError(f"Il file non esiste: {full}")
    if os.path.isdir(full) and not os.path.islink(full):
        raise TrashError("Si possono spostare nel Cestino solo file, non cartelle.")
    from . import sourcefs

    if sourcefs.is_network(full):
        raise TrashError("Il file e' su un disco di rete: li' il Cestino non esiste, non lo elimino.")
    if os.name == "nt":
        _trash_windows(full)
    elif sys.platform.startswith("linux"):
        _trash_linux(full)
    else:
        raise TrashError("Lo spostamento nel Cestino non e' disponibile su questo sistema.")
    if os.path.lexists(full):
        raise TrashError("Spostamento nel Cestino non riuscito: il file e' ancora al suo posto.")


# ------------------------------------------------------------------ Windows
FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400
FOFX_RECYCLEONDELETE = 0x00080000
FOFX_EARLYFAILURE = 0x00100000
_FLAGS = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOFX_RECYCLEONDELETE | FOFX_EARLYFAILURE

_CLSID_FILE_OPERATION = "{3AD05575-8857-4850-9277-11B85BDB8E09}"
_IID_IFILE_OPERATION = "{947AAB5F-0A5C-4C13-B4D6-4BF7836FC9F8}"
_IID_ISHELL_ITEM = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"
# indici nella vtable di IFileOperation (IUnknown = 0..2)
_VT_SET_FLAGS, _VT_DELETE_ITEM, _VT_PERFORM, _VT_ABORTED = 5, 18, 21, 22
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106


def _co_init(ctypes) -> int:
    """CoInitializeEx(apartment) senza eccezioni: l'HRESULT negativo (gia' inizializzato diversamente) si legge."""
    fn = ctypes.WinDLL("ole32").CoInitializeEx
    fn.restype = ctypes.c_long
    return fn(None, 2)


class _ComUnavailable(Exception):
    """IFileOperation non utilizzabile su questo sistema: si prova il ripiego."""


def _trash_windows(full: str) -> None:
    try:
        _trash_ifileoperation(full)
        return
    except _ComUnavailable:
        pass
    _trash_shfileoperation(full)


def _trash_ifileoperation(full: str) -> None:
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                    ("Data4", ctypes.c_ubyte * 8)]

    ole32, shell32 = ctypes.OleDLL("ole32"), ctypes.OleDLL("shell32")
    ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                    ctypes.POINTER(ctypes.c_void_p)]

    def guid(s: str) -> GUID:
        g = GUID()
        ole32.CLSIDFromString(s, ctypes.byref(g))
        return g

    def call(obj: ctypes.c_void_p, index: int, *args, argtypes=()):
        vtbl = ctypes.cast(ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])
        return fn(obj, *args)

    def release(obj: ctypes.c_void_p) -> None:
        if obj and obj.value:
            call(obj, 2)

    # COM: il job gira in un thread proprio, va inizializzato (apartment); RPC_E_CHANGED_MODE = gia' inizializzato
    hr_init = _co_init(ctypes)
    must_uninit = hr_init in (0, 1)
    op = ctypes.c_void_p()
    item = ctypes.c_void_p()
    try:
        try:
            ole32.CoCreateInstance(ctypes.byref(guid(_CLSID_FILE_OPERATION)), None, 1,
                                   ctypes.byref(guid(_IID_IFILE_OPERATION)), ctypes.byref(op))
            shell32.SHCreateItemFromParsingName(full, None, ctypes.byref(guid(_IID_ISHELL_ITEM)), ctypes.byref(item))
        except OSError as e:
            if not os.path.lexists(full):
                raise TrashError("Il file non esiste piu'.") from e
            raise _ComUnavailable(str(e)) from e
        if hr_init < 0 and hr_init != _RPC_E_CHANGED_MODE:
            raise _ComUnavailable("COM non inizializzabile")
        hr = call(op, _VT_SET_FLAGS, _FLAGS, argtypes=(wintypes.DWORD,))
        if hr < 0:
            raise _ComUnavailable(f"SetOperationFlags {hr:#x}")
        hr = call(op, _VT_DELETE_ITEM, item, None, argtypes=(ctypes.c_void_p, ctypes.c_void_p))
        if hr < 0:
            raise TrashError(f"Spostamento nel Cestino non riuscito (codice {hr & 0xFFFFFFFF:#x}).")
        hr = call(op, _VT_PERFORM)
        aborted = wintypes.BOOL(0)
        call(op, _VT_ABORTED, ctypes.byref(aborted), argtypes=(ctypes.POINTER(wintypes.BOOL),))
        if hr < 0 or aborted.value or os.path.lexists(full):
            raise TrashError("Il file non puo' andare nel Cestino (Cestino disattivato o troppo piccolo per questo "
                             "file): non e' stato eliminato.")
    finally:
        release(item)
        release(op)
        if must_uninit:
            ctypes.WinDLL("ole32").CoUninitialize()


def _volume_guid(drive: str) -> str | None:
    import ctypes

    buf = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(drive + "\\", buf, 64):
        return None
    name = buf.value  # \\?\Volume{guid}\
    i, j = name.find("{"), name.find("}")
    return name[i:j + 1] if 0 <= i < j else None


def _bin_settings(drive: str) -> tuple[bool, int | None]:
    """(Cestino attivo sul volume, capienza massima in byte se impostata). Legge le impostazioni di Esplora risorse."""
    import winreg

    nuke, max_mb = False, None
    guid = _volume_guid(drive)
    base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket"
    for sub in ([f"{base}\\Volume\\{guid}"] if guid else []) + [base]:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
                for name in ("NukeOnDelete", "MaxCapacity"):
                    try:
                        v, _t = winreg.QueryValueEx(k, name)
                    except OSError:
                        continue
                    if name == "NukeOnDelete" and v:
                        nuke = True
                    elif name == "MaxCapacity" and max_mb is None:
                        max_mb = int(v)
        except OSError:
            continue
    return (not nuke), (max_mb * 1024 * 1024 if max_mb else None)


def _trash_shfileoperation(full: str) -> None:
    """Ripiego: SHFileOperation sa solo 'provare' il Cestino e altrimenti cancella. Quindi prima si controlla
    che il Cestino del volume sia attivo e abbastanza capiente."""
    import ctypes
    from ctypes import wintypes

    drive = os.path.splitdrive(full)[0]
    size = os.path.getsize(full)
    enabled, cap = _bin_settings(drive) if drive else (False, None)
    if not enabled:
        raise TrashError("Il Cestino e' disattivato su questo disco: il file non verrebbe recuperabile, non lo elimino.")
    if cap is None:  # capienza non impostata: valore predefinito di Windows, in pratica una frazione del volume
        cap = int(shutil.disk_usage(drive + "\\").total * 0.05)
    if size > cap:
        raise TrashError("Il file e' piu' grande della capienza del Cestino di questo disco: non lo elimino.")

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    buf = ctypes.create_unicode_buffer(full + "\0\0")  # elenco chiuso da due caratteri nulli
    op = SHFILEOPSTRUCTW(hwnd=None, wFunc=3, pFrom=ctypes.cast(buf, wintypes.LPCWSTR), pTo=None,
                         fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
                         fAnyOperationsAborted=False, hNameMappings=None, lpszProgressTitle=None)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code != 0 or op.fAnyOperationsAborted:
        raise TrashError(f"Spostamento nel Cestino non riuscito (codice {code}).")


# -------------------------------------------------------------------- Linux
def _trash_linux(full: str) -> None:
    gio = shutil.which("gio")
    if gio:
        try:
            r = subprocess.run([gio, "trash", "--", full], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            r = None
        if r is not None and r.returncode == 0:
            return
        if os.path.lexists(full) is False:
            return
    _trash_xdg(full)


def _mount_root(path: str) -> str:
    p = os.path.realpath(path)
    while not os.path.ismount(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def _trash_dirs_for(full: str) -> tuple[Path, str | None]:
    """(cartella del Cestino da usare, radice a cui e' relativo Path= nel .trashinfo; None = percorso assoluto)."""
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    home_trash = data / "Trash"
    home_trash.mkdir(parents=True, exist_ok=True)
    parent = os.path.dirname(full)
    if os.stat(parent).st_dev == os.stat(home_trash).st_dev:
        return home_trash, None
    root = _mount_root(full)
    uid = os.getuid()
    shared = Path(root) / ".Trash"
    try:
        st = os.lstat(shared)
        import stat as _st

        if _st.S_ISDIR(st.st_mode) and st.st_mode & _st.S_ISVTX and not _st.S_ISLNK(st.st_mode):
            t = shared / str(uid)
            t.mkdir(parents=True, exist_ok=True, mode=0o700)
            return t, root
    except OSError:
        pass
    t = Path(root) / f".Trash-{uid}"
    t.mkdir(parents=True, exist_ok=True, mode=0o700)
    return t, root


def _trash_xdg(full: str) -> None:
    try:
        trash, root = _trash_dirs_for(full)
        files, info = trash / "files", trash / "info"
        files.mkdir(parents=True, exist_ok=True, mode=0o700)
        info.mkdir(parents=True, exist_ok=True, mode=0o700)
        rel = full if root is None else os.path.relpath(full, root)
        base = os.path.basename(full)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        n = 0
        while True:
            name = base if n == 0 else f"{base}.{n}"
            try:  # riserva il nome creando il .trashinfo in modo esclusivo
                fd = os.open(info / f"{name}.trashinfo", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                n += 1
                continue
            break
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"[Trash Info]\nPath={quote(rel)}\nDeletionDate={stamp}\n")
        try:
            os.rename(full, files / name)  # stesso volume: mai copia + cancellazione
        except OSError:
            (info / f"{name}.trashinfo").unlink(missing_ok=True)
            raise
    except TrashError:
        raise
    except OSError as e:
        raise TrashError(f"Spostamento nel Cestino non riuscito: {e}") from e
