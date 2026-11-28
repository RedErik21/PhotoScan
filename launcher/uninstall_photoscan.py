"""Disinstallatore di PhotoScan (Windows): PhotoScan-Setup.exe --uninstall.

Il Setup registra PhotoScan in «App installate» (HKCU, senza amministratore) e si copia in
%LOCALAPPDATA%\\PhotoScan\\uninstall\\PhotoScan-Uninstall.exe. Quel file non puo' cancellare la propria cartella
mentre gira: al lancio si copia in %TEMP% e si rilancia da li (fase 2), che fa il lavoro vero.

Cosa fa: chiude PhotoScan (chiede conferma), mostra cosa verra' rimosso e quanto spazio libera, toglie programma,
runtime, collegamenti sul Desktop e nel menu Start e la voce di registro. I DATI (catalogo, cache, miniature, modelli,
impostazioni) restano, a meno che l'utente non chieda di eliminarli: predefinito = conservarli.
Non tocca MAI le cartelle delle foto (sorgente, destinazione, NAS), ne' nulla fuori da %LOCALAPPDATA%\\PhotoScan.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\PhotoScan"
UNINSTALLER_REL = Path("uninstall") / "PhotoScan-Uninstall.exe"
# Sempre rimossi (il programma e i suoi scarti); tutto il resto sono dati dell'utente.
PROGRAM_ITEMS = ("app", "app.new", "app.old", "runtime", "download", "uninstall", "install.json")
MB_YESNO, MB_OK, MB_ICONINFO, MB_ICONQUESTION, MB_ICONWARNING, MB_DEFBUTTON2, MB_TOPMOST = (
    0x4, 0x0, 0x40, 0x20, 0x30, 0x100, 0x40000)
IDYES = 6


def data_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "PhotoScan"


def is_safe_root(root: Path) -> bool:
    """Solo una cartella chiamata PhotoScan, mai la home o una radice di disco."""
    try:
        r = Path(root).resolve()
    except OSError:
        return False
    return r.name.lower() == "photoscan" and len(r.parts) >= 3 and r != Path.home().resolve()


def dir_size(path: Path, max_seconds: float = 8.0) -> int:
    path = Path(path)
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    total, end, stack = 0, time.time() + max_seconds, [path]
    while stack and time.time() < end:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def fmt_size(n: int) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


# ------------------------------------------------------------------ cartelle protette (le foto dell'utente)
def protected_paths(root: Path) -> list[Path]:
    """Cartelle di foto/NAS indicate in config.json: non si cancellano mai, nemmeno se stanno dentro la cartella dati."""
    out: list[Path] = []
    try:
        cfg = json.loads((Path(root) / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return out
    for key in ("source_dir", "output_dir", "nas_dir", "nas_receive_dir"):
        v = cfg.get(key) if isinstance(cfg, dict) else None
        if isinstance(v, str) and v.strip():
            try:
                out.append(Path(v).resolve())
            except OSError:
                pass
    return out


def _overlaps(path: Path, protected: list[Path]) -> bool:
    """`path` contiene (o e' dentro) una cartella protetta."""
    try:
        p = path.resolve()
    except OSError:
        return True      # nel dubbio, non si tocca
    for q in protected:
        if p == q or p in q.parents or q in p.parents:
            return True
    return False


# ------------------------------------------------------------------ piano
def build_plan(root: Path, delete_data: bool) -> dict:
    """{items: [{label, path, bytes, kind}], skipped: [...], total}. Nulla viene cancellato qui."""
    root = Path(root)
    prot = protected_paths(root)
    items, skipped = [], []
    labels = {"app": "Programma", "runtime": "Componente di calcolo (PyTorch)", "download": "Download temporanei",
              "uninstall": "Disinstallatore", "install.json": "Dati dell'installazione", "app.new": "Installazione interrotta",
              "app.old": "Versione precedente"}
    for name in PROGRAM_ITEMS:
        p = root / name
        if p.exists():
            items.append({"label": labels.get(name, name), "path": str(p), "bytes": dir_size(p), "kind": "programma"})
    if delete_data and root.is_dir():
        for p in sorted(root.iterdir()):
            if p.name in PROGRAM_ITEMS:
                continue
            if _overlaps(p, prot):
                skipped.append(str(p))
                continue
            items.append({"label": "Dati: " + p.name, "path": str(p), "bytes": dir_size(p), "kind": "dati"})
    return {"items": items, "skipped": skipped, "total": sum(i["bytes"] for i in items)}


def data_bytes(root: Path) -> int:
    root = Path(root)
    return sum(dir_size(p) for p in root.iterdir() if p.name not in PROGRAM_ITEMS) if root.is_dir() else 0


# ------------------------------------------------------------------ processi
def running_processes(root: Path) -> list:
    """Processi che usano la cartella di PhotoScan (programma, server, finestra WebView2)."""
    try:
        import psutil
    except ImportError:
        return []
    root_s = str(Path(root)).lower()
    me = os.getpid()
    out = []
    for p in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if p.info["pid"] == me:
                continue
            exe = (p.info.get("exe") or "").lower()
            cmd = " ".join(p.info.get("cmdline") or []).lower()
            if exe.startswith(root_s) or root_s in cmd:
                out.append(p)
        except Exception:  # noqa: BLE001 - processi spariti o negati
            continue
    return out


def stop_processes(procs: list, timeout: float = 10.0) -> list:
    """Chiude i processi (prima con le buone, poi con la forza). Ritorna quelli che non si sono fermati."""
    for p in procs:
        try:
            p.terminate()
        except Exception:  # noqa: BLE001
            pass
    end = time.time() + timeout
    left = [p for p in procs]
    while left and time.time() < end:
        left = [p for p in left if _alive(p)]
        time.sleep(0.3)
    for p in left:
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass
    time.sleep(0.5)
    return [p for p in left if _alive(p)]


def _alive(p) -> bool:
    try:
        return p.is_running()
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ cancellazione
def _force_remove(func, path, _exc) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_path(path: Path, retries: int = 5) -> str | None:
    """Rimuove file o cartella (con qualche tentativo: antivirus e indicizzazione tengono i file aperti).
    None = fatto, altrimenti il messaggio dell'errore."""
    path = Path(path)
    last = None
    for _ in range(retries):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, onerror=_force_remove)
            elif path.exists() or path.is_symlink():
                os.chmod(path, stat.S_IWRITE)
                path.unlink()
        except OSError as e:
            last = str(e)
        if not path.exists() and not path.is_symlink():
            return None
        time.sleep(1.0)
    return last or "non rimosso"


def known_folder(folder_id: str) -> Path | None:
    """Cartella speciale di Windows (Desktop, Menu Start) anche se spostata da OneDrive."""
    if sys.platform != "win32":
        return None
    try:
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]

        g = uuid.UUID(folder_id)
        guid = GUID.from_buffer_copy(g.bytes_le)
        out = ctypes.c_wchar_p()
        fn = ctypes.windll.shell32.SHGetKnownFolderPath
        fn.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        if fn(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0 or not out.value:
            return None
        path = Path(out.value)
        ctypes.windll.ole32.CoTaskMemFree(out)
        return path
    except Exception:  # noqa: BLE001
        return None


FOLDERID_DESKTOP = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"
FOLDERID_PROGRAMS = "A77F5D77-2E2B-44C3-A6A2-ABA601054A51"


def shortcut_paths() -> list[Path]:
    """Collegamenti PhotoScan.lnk: Desktop (anche reindirizzato) e menu Start dell'utente."""
    out: list[Path] = []
    for fid, fallback in ((FOLDERID_DESKTOP, Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"),
                          (FOLDERID_PROGRAMS, Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" /
                           "Start Menu" / "Programs")):
        d = known_folder(fid) or fallback
        out.append(d / "PhotoScan.lnk")
    return out


# ------------------------------------------------------------------ registro
def _winreg(reg=None):
    if reg is not None:
        return reg
    import winreg
    return winreg


def unregister_uninstall(reg=None) -> bool:
    """Toglie la voce da «App installate». True = c'era o non c'e' piu'."""
    w = _winreg(reg)
    try:
        w.DeleteKey(w.HKEY_CURRENT_USER, UNINSTALL_KEY)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ dialoghi
def _box(text: str, title: str = "Disinstalla PhotoScan", flags: int = MB_OK | MB_ICONINFO) -> int:
    if sys.platform != "win32":
        print(f"{title}: {text}")
        return IDYES
    return ctypes.windll.user32.MessageBoxW(0, text, title, flags | MB_TOPMOST)


def describe(plan: dict) -> str:
    lines = [f"  - {i['label']} ({fmt_size(i['bytes'])})" for i in plan["items"]]
    return "\n".join(lines) or "  (niente da rimuovere)"


# ------------------------------------------------------------------ esecuzione
def uninstall(root: Path, delete_data: bool | None, silent: bool = False, reg=None,
              shortcuts: list[Path] | None = None) -> dict:
    """Fase 2 (gira da %TEMP%). delete_data None = si chiede all'utente (predefinito: conservare).
    Ritorna {done, delete_data, removed, errors, freed}."""
    root = Path(root)
    result = {"done": False, "delete_data": False, "removed": [], "errors": [], "freed": 0}
    if not is_safe_root(root):
        result["errors"].append(f"cartella dati non riconosciuta: {root}")
        return result
    if delete_data is None and not silent:
        db = data_bytes(root)
        ans = _box("Eliminare anche i dati di PhotoScan?\n\nCatalogo, miniature, modelli scaricati e impostazioni "
                   f"occupano {fmt_size(db)}.\n\nSì = elimina anche i dati\nNo = conserva i dati (consigliato: se "
                   "reinstalli PhotoScan ritrovi tutto)\n\nLe tue foto non vengono toccate in nessun caso.",
                   flags=MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2)
        delete_data = ans == IDYES
    delete_data = bool(delete_data)
    result["delete_data"] = delete_data
    plan = build_plan(root, delete_data)
    if not silent:
        extra = ("\n\nCartelle non rimosse perché contengono le tue foto:\n  " + "\n  ".join(plan["skipped"])
                 if plan["skipped"] else "")
        ok = _box(f"Verrà rimosso da questo PC:\n\n{describe(plan)}\n\nSpazio liberato: circa {fmt_size(plan['total'])}.\n"
                  + ("I dati dell'app verranno ELIMINATI." if delete_data else "I dati dell'app (catalogo, impostazioni, "
                     f"cache) restano in {root}.") + "\nLe tue foto non vengono toccate." + extra +
                  "\n\nContinuare?", flags=MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2)
        if ok != IDYES:
            return result
    procs = running_processes(root)
    if procs:
        if not silent and _box("PhotoScan è ancora aperto. Chiuderlo ora per continuare?",
                               flags=MB_YESNO | MB_ICONQUESTION) != IDYES:
            return result
        still = stop_processes(procs)
        if still:
            result["errors"].append("PhotoScan non si è chiuso: chiudilo e riprova.")
            if not silent:
                _box(result["errors"][-1], flags=MB_OK | MB_ICONWARNING)
            return result
    for lnk in (shortcuts if shortcuts is not None else shortcut_paths()):
        if lnk.exists():
            err = remove_path(lnk, retries=2)
            (result["errors"].append(f"{lnk}: {err}") if err else result["removed"].append(str(lnk)))
    for item in plan["items"]:
        err = remove_path(Path(item["path"]))
        if err:
            result["errors"].append(f"{item['path']}: {err}")
        else:
            result["removed"].append(item["path"])
            result["freed"] += item["bytes"]
    if delete_data and not plan["skipped"]:
        try:
            Path(root).rmdir()          # la cartella vuota; se non lo e' non fa nulla
        except OSError:
            pass
    if not unregister_uninstall(reg):
        result["errors"].append("voce di registro non rimossa")
    result["done"] = not result["errors"]
    if not silent:
        if result["errors"]:
            _box("PhotoScan è stato rimosso quasi del tutto. Non sono riuscito a togliere:\n\n  "
                 + "\n  ".join(result["errors"][:8]) + "\n\nChiudi i programmi che usano questi file e riprova.",
                 flags=MB_OK | MB_ICONWARNING)
        else:
            _box(f"PhotoScan è stato disinstallato. Spazio liberato: circa {fmt_size(result['freed'])}."
                 + ("" if delete_data else f"\n\nI tuoi dati sono rimasti in {root}."))
    return result


def run(argv: list[str]) -> int:
    """Punto d'ingresso di `PhotoScan-Setup.exe --uninstall [--delete-data|--keep-data] [--silent]`."""
    delete = True if "--delete-data" in argv else False if "--keep-data" in argv else None
    silent = "--silent" in argv
    root = data_root()
    if "--root" in argv:
        root = Path(argv[argv.index("--root") + 1])
    if "--stage2" not in argv and getattr(sys, "frozen", False):
        # fase 1: il disinstallatore non puo' cancellare se stesso mentre gira -> copia in TEMP e rilancia da li
        tmp = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / f"PhotoScan-Uninstall-{os.getpid()}.exe"
        shutil.copy2(sys.executable, tmp)
        subprocess.Popen([str(tmp), *[a for a in argv if a != "--uninstall"], "--uninstall", "--stage2", "--root", str(root)],
                         cwd=str(tmp.parent), close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return 0
    res = uninstall(root, delete, silent=silent)
    if "--stage2" in argv and getattr(sys, "frozen", False):
        # la copia in TEMP si toglie da sola dopo la chiusura
        subprocess.Popen(["cmd", "/c", "ping 127.0.0.1 -n 3 >nul & del /f /q \"" + sys.executable + "\""],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
    return 0 if res["done"] or not res["errors"] else 1
