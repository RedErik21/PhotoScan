"""Archivio Foto - avvio come applicazione: server FastAPI nascosto + finestra dedicata (Edge/Chrome in modalita' app).

Uso: pythonw -m fotoscan.desktop   (oppure l'icona 'PhotoScan' sul Desktop, o 'python -m fotoscan ui')

- Se il server e' gia' attivo (seconda istanza) viene riusato: si apre solo la finestra.
- Chiudendo la finestra il server si spegne e con lui un'eventuale indicizzazione
  (riprendibile: alla prossima apertura si riparte dai file non ancora elaborati).
- Errori (server che non parte, nessun browser) sono mostrati in una finestra con il percorso del log.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from .config import CODE_ROOT, FROZEN, ROOT

LOG = ROOT / "fotoscan_ui.log"
CACHE = ROOT / "cache"
STATE = CACHE / "desktop_server.json"   # {"port", "pid"} del server avviato da questo launcher
WINDOW_STATE = CACHE / "window_state.json"  # dimensione e posizione finestra salvate alla chiusura
PROFILE = CACHE / "webprofile"          # profilo dedicato: finestra separata dal browser di uso
PREFERRED_PORT = 8765
WINDOW_SIZE = "1400,900"
APP_NAME = "PhotoScan"
APP_ICON = CODE_ROOT / "fotoscan" / "photoscan.ico"
APP_USER_MODEL_ID = "FotoScan.App"       # identità nella barra delle applicazioni (raggruppa e mostra il nome)
START_TIMEOUT = 120  # secondi: il primo avvio puo' importare torch
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

MB_OK, MB_ICONERROR, MB_ICONINFO, MB_TOPMOST = 0x0, 0x10, 0x40, 0x40000
MB_YESNO, MB_ICONWARNING, MB_DEFBUTTON2 = 0x4, 0x30, 0x100
IDYES = 6
JOB_LABELS = {
    "phone_import": "importazione dal telefono",
    "apply": "creazione delle cartelle",
    "plan": "calcolo del piano",
    "volti_scan": "analisi dei volti",
    "volti_cluster": "raggruppamento delle persone",
    "volti_modelli": "download modelli volti",
    "update_download": "download dell'aggiornamento",
}


# ------------------------------------------------------------------ utilita'
def _zenity(args: list[str]) -> int | None:
    """Finestra di dialogo di GNOME/KDE tramite zenity. None = zenity non disponibile."""
    import shutil
    if not shutil.which("zenity"):
        return None
    return subprocess.run(["zenity", *args], capture_output=True).returncode


def _message(title: str, text: str, error: bool = False) -> None:
    """Finestra di dialogo nativa (funziona anche con pythonw, senza console)."""
    if sys.platform != "win32":
        kind = "--error" if error else "--info"
        if _zenity([kind, f"--title={title}", f"--text={text}", "--width=480"]) is None:
            _log_line(f"{title}: {text}")
            print(f"{title}: {text}", file=sys.stderr)
        return
    flags = (MB_ICONERROR if error else MB_ICONINFO) | MB_TOPMOST | MB_OK
    ctypes.windll.user32.MessageBoxW(0, text, title, flags)


def _busy_notice(title: str, text: str):
    """Avviso non bloccante durante un'attesa lunga (es. pip install). Ritorna la funzione che lo chiude."""
    if sys.platform != "win32":
        import shutil
        if not shutil.which("zenity"):
            _log_line(f"{title}: {text}")
            return lambda: None
        try:
            proc = subprocess.Popen(["zenity", "--progress", "--pulsate", "--no-cancel", "--auto-close",
                                     f"--title={title}", f"--text={text}", "--width=480"],
                                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return lambda: None

        def close_linux() -> None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return close_linux

    import threading

    threading.Thread(target=lambda: ctypes.windll.user32.MessageBoxW(0, text, title, MB_ICONINFO | MB_TOPMOST | MB_OK),
                     daemon=True, name="busy-notice").start()

    def close_win() -> None:
        try:
            u32 = ctypes.windll.user32
            hwnd = u32.FindWindowW(None, title)
            if hwnd:
                u32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        except Exception:  # noqa: BLE001
            pass
    return close_win


def _python() -> str:
    """Interprete 'console' (pythonw non ha stdout per il sottoprocesso)."""
    py = sys.executable
    if py.lower().endswith("pythonw.exe"):
        cand = Path(py).with_name("python.exe")
        if cand.exists():
            return str(cand)
    return py


def _log_tail(n: int = 4, limit: int = 500) -> str:
    try:
        lines = LOG.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    tail = "\n".join(lines[-n:])
    return tail[-limit:]


def _fail(motivo: str) -> int:
    testo = (f"{motivo}\n\nDettagli nel file di log:\n{LOG}\n")
    tail = _log_tail()
    if tail:
        testo += f"\nUltime righe:\n{tail}"
    _message("PhotoScan - errore", testo, error=True)
    return 1


def free_port(preferred: int = PREFERRED_PORT) -> int:
    """Porta preferita se libera, altrimenti una porta qualsiasi libera."""
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("nessuna porta libera")


def find_browser() -> str | None:
    """Edge, poi Chrome. None = usare il browser predefinito."""
    if sys.platform != "win32":
        import shutil
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
                     "microsoft-edge", "microsoft-edge-stable", "brave-browser"):
            found = shutil.which(name)
            if found:
                return found
        return None
    bases = [os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
             os.environ.get("LOCALAPPDATA", "")]
    rel = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
    for r in rel:
        for base in bases:
            if base and (Path(base) / r).exists():
                return str(Path(base) / r)
    return None


def _health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_healthy(port: int, proc: subprocess.Popen, timeout: float = START_TIMEOUT) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False
        if _health(port):
            return True
        time.sleep(0.5)
    return False


def _kill_tree(pid: int) -> None:
    """Termina il server e tutti i sottoprocessi (es. l'indicizzazione)."""
    if sys.platform != "win32":
        import signal
        try:
            os.killpg(pid, signal.SIGTERM)   # il server e' il capo del suo gruppo di processi (start_new_session)
        except OSError:
            pass
        return
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)


def _read_state() -> dict | None:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_state(port: int, pid: int) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"port": port, "pid": pid}), encoding="utf-8")


def _clear_state() -> None:
    STATE.unlink(missing_ok=True)


def _server_stopped(proc: subprocess.Popen | None, port: int, timeout: float = 10.0) -> bool:
    """Attende che il server sia davvero spento (processo raccolto e porta muta). Se non si spegne, lo stato
    resta: la prossima istanza lo ritrova invece di avviarne un secondo sullo stesso catalogo."""
    end = time.time() + timeout
    if proc is not None:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if sys.platform != "win32":
                import signal
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if proc.poll() is None:
            return False
    while _health(port):
        if time.time() > end:
            return False
        time.sleep(0.3)
    return True


def _start_server(port: int) -> subprocess.Popen:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    with open(LOG, "a", encoding="utf-8") as log:  # azzerato da main() a ogni avvio
        log.write(f"PhotoScan: avvio server su 127.0.0.1:{port}\n")
        log.flush()
        extra = {"creationflags": NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}
        return subprocess.Popen([_python(), "-m", "fotoscan.server", "--port", str(port)], cwd=str(ROOT),
                                env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                **extra)


def _log_line(text: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%H:%M:%S')} {text}\n")
    except OSError:
        pass


def _install_pending_update() -> None:
    """Installa l'aggiornamento scaricato (se approvato, o se l'installazione automatica e' attiva).
    Gira prima di avviare il server: nessun file del programma e' in uso. Mai bloccante per l'avvio."""
    try:
        from . import updater
        from .config import load_config

        cfg = load_config()
        if FROZEN:  # PhotoScan.exe: nessun file del programma da sostituire (vedi routes_update)
            return
        if not updater.read_pending(cfg.cache_dir):
            return
        res = updater.apply_pending(ROOT, cfg.cache_dir, force=cfg.update_auto_install, logger=_log_line)
        if res and res.get("requirements_changed"):
            _log_line("dipendenze cambiate: pip install -r requirements.txt")
            close_notice = _busy_notice(
                "PhotoScan - aggiornamento",
                f"PhotoScan si sta aggiornando alla versione {res['to']} e installa le nuove librerie.\n\n"
                "Può richiedere alcuni minuti: la finestra si apre da sola appena finito.")
            try:
                ok, out = updater.install_requirements(ROOT, _python())
            finally:
                close_notice()
            _log_line(out)
            if not ok:
                _message("PhotoScan - aggiornamento",
                         f"PhotoScan è stato aggiornato alla versione {res['to']}, ma l'installazione delle nuove "
                         "librerie non è riuscita. Se qualcosa non funziona, esegui nella cartella di PhotoScan:\n\n"
                         ".venv\\Scripts\\python.exe -m pip install -r requirements.txt\n\n"
                         f"Dettagli nel log:\n{LOG}", error=True)
    except Exception as e:  # noqa: BLE001
        _log_line(f"aggiornamento non installato: {e}")
        _message("PhotoScan - aggiornamento", f"{e}\n\nPhotoScan si avvia con la versione precedente.", error=True)


def _install_exe_on_exit() -> bool:
    """PhotoScan.exe: installa l'eseguibile approvato a programma chiuso. True = lo script e' partito."""
    try:
        from . import updater
        from .config import load_config

        return updater.start_exe_update(load_config().cache_dir, Path(sys.executable).parent)
    except Exception as e:  # noqa: BLE001 - la chiusura non si blocca per un aggiornamento
        _log_line(f"aggiornamento non installato: {e}")
        return False


def _relaunch() -> None:
    """Nuova istanza del launcher (dopo «Installa e riavvia»): installa l'aggiornamento e riapre la finestra."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    cmd = [sys.executable] if FROZEN else [sys.executable, "-m", "fotoscan.desktop"]
    subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)


def _busy_description(base: str) -> str | None:
    """Descrizione delle operazioni in corso (analisi, importazione, ...) o None se il programma e' fermo."""
    parts: list[str] = []
    try:
        with urllib.request.urlopen(base + "api/index/status", timeout=2) as r:
            st = json.loads(r.read().decode("utf-8"))
        if st.get("running"):
            parts.append(f"analisi delle foto ({st.get('done', 0)} di {st.get('total', 0)})")
    except Exception:  # server non risponde: nessuna operazione verificabile
        pass
    try:
        with urllib.request.urlopen(base + "api/jobs", timeout=2) as r:
            jobs_list = json.loads(r.read().decode("utf-8"))
        for j in jobs_list:
            if j.get("state") == "running":
                name = JOB_LABELS.get(j.get("name"), j.get("name") or "operazione")
                parts.append(f"{name} ({j.get('done', 0)} di {j.get('total', 0)})")
    except Exception:
        pass
    return ", ".join(parts) or None


def _confirm_close(desc: str) -> bool:
    """Avviso con Si/No (No di default). True = l'utente vuole chiudere comunque."""
    text = (f"Un'operazione è in corso: {desc}." + "\n\n"
            "Chiudendo PhotoScan ora viene interrotta. L'analisi riprende da dove si è fermata; "
            "la copia dal telefono riprende saltando i file già copiati." + "\n\nChiudere comunque?")
    if sys.platform != "win32":
        # zenity --question: codice 0 = Sì. Senza zenity la chiusura non si puo' chiedere: si chiude e
        # l'operazione riprende alla prossima apertura (e' riprendibile).
        rc = _zenity(["--question", "--title=PhotoScan", f"--text={text}", "--width=480", "--default-cancel"])
        return rc is None or rc == 0
    flags = MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_TOPMOST
    return ctypes.windll.user32.MessageBoxW(0, text, "PhotoScan", flags) == IDYES


def _may_close(base: str) -> bool:
    """Chiusura consentita? Se c'e' un'operazione in corso chiede conferma."""
    try:
        desc = _busy_description(base)
    except Exception:
        desc = None
    return desc is None or _confirm_close(desc)


def _windows_prefers_dark() -> bool:
    """True se Windows usa il tema scuro per le app (evita flash bianco all'avvio)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            val, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return int(val) == 0
    except Exception:
        return False


def _colorref(hex_rgb: str) -> int:
    """Converte '#RRGGBB' in COLORREF Windows (0x00BBGGRR)."""
    h = hex_rgb.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r


def _window_hwnd(window) -> int:
    native = getattr(window, "native", None)
    return int(native.Handle.ToInt64()) if native is not None and hasattr(native, "Handle") else 0


def _set_titlebar_theme(window, dark: bool) -> None:
    """Sincronizza tema e colori della barra del titolo nativa di Windows 10/11 con la palette di PhotoScan (DWM)."""
    if sys.platform != "win32":
        return
    try:
        hwnd = _window_hwnd(window)
        if not hwnd:
            return
        dwm = ctypes.windll.dwmapi
        val = ctypes.c_int(1 if dark else 0)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 2004+ / Win11), 19 = fallback Win10 precedenti
        for attr in (20, 19):
            if dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), ctypes.c_int(attr), ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
        # Windows 11: barra superiore fusa con lo sfondo dell'app (tokens-color.css), testo discreto (ignorati su Win10)
        caption = ctypes.c_uint32(_colorref("#0e0e13" if dark else "#f6f6f9"))
        text = ctypes.c_uint32(_colorref("#a6a5b8" if dark else "#5a5a6e"))
        border = ctypes.c_uint32(_colorref("#272632" if dark else "#e1e0ea"))
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), ctypes.c_int(35), ctypes.byref(caption), ctypes.sizeof(caption))
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), ctypes.c_int(36), ctypes.byref(text), ctypes.sizeof(text))
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), ctypes.c_int(34), ctypes.byref(border), ctypes.sizeof(border))
        # Nasconde icona e scritta "PhotoScan" nella barra del titolo (restano in taskbar e Alt+Tab)
        class _WTA_OPTIONS(ctypes.Structure):
            _fields_ = [("dwFlags", ctypes.c_uint32), ("dwMask", ctypes.c_uint32)]
        opts = _WTA_OPTIONS(0x00000003, 0x00000003)  # WTNCA_NODRAWCAPTION | WTNCA_NODRAWICON
        ctypes.windll.uxtheme.SetWindowThemeAttribute(ctypes.c_void_p(hwnd), ctypes.c_int(1), ctypes.byref(opts), ctypes.sizeof(opts))
    except Exception:
        pass


def _set_window_identity(window) -> None:
    """Imposta nome e icona di PhotoScan sul menu tasto destro della barra delle applicazioni (anche da python.exe)."""
    if sys.platform != "win32":
        return
    try:
        hwnd = _window_hwnd(window)
        if not hwnd:
            return
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            ]

            @classmethod
            def from_str(cls, s: str) -> "GUID":
                u = uuid.UUID(s)
                b = u.bytes_le
                return cls.from_buffer_copy(b)

        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]

        class PROPVARIANT(ctypes.Structure):
            _fields_ = [
                ("vt", ctypes.c_ushort),
                ("wReserved1", ctypes.c_ushort),
                ("wReserved2", ctypes.c_ushort),
                ("wReserved3", ctypes.c_ushort),
                ("pwszVal", ctypes.c_wchar_p),
                ("pad", ctypes.c_uint64),
            ]

        iid_store = GUID.from_str("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
        store_ptr = ctypes.c_void_p()
        hr = ctypes.windll.shell32.SHGetPropertyStoreForWindow(
            ctypes.c_void_p(hwnd), ctypes.byref(iid_store), ctypes.byref(store_ptr),
        )
        if hr != 0 or not store_ptr.value:
            return

        vtbl = ctypes.cast(ctypes.cast(store_ptr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        release_fn = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
        set_value_fn = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT),
        )(vtbl[6])
        commit_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[7])
        try:
            fmt_aumid = GUID.from_str("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}")
            relaunch_cmd = f'"{sys.executable}"' if FROZEN else f'"{sys.executable}" -m fotoscan.desktop'
            props = [
                (5, APP_USER_MODEL_ID),         # PKEY_AppUserModel_ID
                (2, relaunch_cmd),              # PKEY_AppUserModel_RelaunchCommand
                (4, APP_NAME),                  # PKEY_AppUserModel_RelaunchDisplayNameResource
                (3, f"{APP_ICON},0"),           # PKEY_AppUserModel_RelaunchIconResource
            ]
            for pid, text_val in props:
                pk = PROPERTYKEY(fmt_aumid, pid)
                pv = PROPVARIANT(vt=31, pwszVal=text_val)  # VT_LPWSTR = 31
                set_value_fn(store_ptr, ctypes.byref(pk), ctypes.byref(pv))
            commit_fn(store_ptr)
        finally:
            release_fn(store_ptr)
    except Exception:
        pass


def _load_window_state() -> dict:
    """Dimensioni e posizione salvate dell'ultima chiusura, se valide."""
    try:
        data = json.loads(WINDOW_STATE.read_text(encoding="utf-8"))
        w = int(data.get("width", 1400))
        h = int(data.get("height", 900))
        x = data.get("x")
        y = data.get("y")
        out: dict = {"width": max(420, min(w, 3840)), "height": max(520, min(h, 2160))}
        if isinstance(x, int) and isinstance(y, int) and -16000 < x < 16000 and -16000 < y < 16000:
            out["x"], out["y"] = x, y
        return out
    except Exception:
        return {"width": 1400, "height": 900}


def _save_window_state(window) -> None:
    try:
        w, h = int(window.width), int(window.height)
        x, y = int(window.x), int(window.y)
        if w < 420 or h < 520 or x <= -16000 or y <= -16000:
            return
        CACHE.mkdir(parents=True, exist_ok=True)
        WINDOW_STATE.write_text(json.dumps({"width": w, "height": h, "x": x, "y": y}), encoding="utf-8")
    except Exception:
        pass


def _expose_window_controls(window, base: str, allowed: dict, frameless: bool = False,
                            restart: dict | None = None) -> None:
    """Comandi della finestra esposti al frontend come window.pywebview.api.*."""
    state = {"maximized": False}

    def minimize() -> None:
        window.minimize()

    def toggle_maximize() -> bool:
        window.restore() if state["maximized"] else window.maximize()
        state["maximized"] = not state["maximized"]
        return state["maximized"]

    def busy_description() -> str | None:
        return _busy_description(base)

    def force_close() -> None:
        allowed["ok"] = True
        window.destroy()

    def close() -> None:
        if _may_close(base):
            allowed["ok"] = True
            window.destroy()

    def is_frameless() -> bool:
        return bool(frameless)

    def set_theme(dark: bool) -> None:
        _set_titlebar_theme(window, bool(dark))

    def restart_app() -> bool:
        """Chiude e riapre PhotoScan (installa l'aggiornamento pronto). La conferma la chiede il frontend."""
        if restart is None:
            return False
        restart["requested"] = True
        allowed["ok"] = True
        window.destroy()
        return True

    window.expose(minimize, toggle_maximize, close, force_close, busy_description, is_frameless, set_theme, restart_app)


def _set_app_identity() -> None:
    """Nome e icona di PhotoScan nella barra delle applicazioni (senza, Windows mostra l'icona di Python)."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass  # non Windows o shell non disponibile: nessun effetto


def _open_app_window(url: str, restart: dict | None = None) -> bool:
    """Finestra senza bordi con pywebview (WebView2 di Edge). True = finestra aperta e poi chiusa.
    False = pywebview non disponibile o non partito: si ripiega su Edge/Chrome in modalita' app.
    restart["requested"] = True se la finestra e' stata chiusa con «Installa e riavvia»."""
    try:
        import webview
    except ImportError:
        return False
    _set_app_identity()
    try:
        dark = _windows_prefers_dark()
        geom = _load_window_state()
        window = webview.create_window(
            APP_NAME, url,
            width=geom["width"], height=geom["height"],
            x=geom.get("x"), y=geom.get("y"),
            min_size=(420, 520),
            resizable=True, frameless=False, easy_drag=False,
            background_color="#0e0e13" if dark else "#f6f6f9",
        )
        allowed = {"ok": False}
        _expose_window_controls(window, url, allowed, frameless=False, restart=restart)

        def on_shown() -> None:
            _set_titlebar_theme(window, dark)
            _set_window_identity(window)

        def on_closing() -> bool:
            # False = annulla la chiusura (Alt+F4 o X di sistema). Dopo una conferma gia' data non si chiede di nuovo.
            if allowed["ok"]:
                _save_window_state(window)
                return True
            if _may_close(url):
                allowed["ok"] = True
                _save_window_state(window)
                return True
            return False

        window.events.shown += on_shown
        window.events.closing += on_closing
        webview.start(icon=str(APP_ICON))
    except Exception as e:  # runtime WebView2 mancante, ecc.
        with open(LOG, "a", encoding="utf-8") as log:
            log.write(f"pywebview non disponibile: {e}\n")
        return False
    return True


def _window_open(profile: Path) -> bool:
    """True se almeno un processo del browser usa il profilo dedicato (cioe' la finestra e' aperta)."""
    if sys.platform != "win32":
        needle = str(profile).encode()
        for cmd in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                if needle in cmd.read_bytes():
                    return True
            except OSError:
                continue
        return False
    ps =("@(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
          "$_.CommandLine.Contains($env:FS_PROFILE) }).Count")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                         env=dict(os.environ, FS_PROFILE=str(profile)), capture_output=True, text=True,
                         creationflags=NO_WINDOW)
    try:
        return int(out.stdout.strip() or 0) > 0
    except ValueError:
        return False


def _profile_pids(profile: Path) -> list[int]:
    """Windows: pid dei processi del browser che usano il profilo dedicato (una sola chiamata PowerShell)."""
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
          "$_.CommandLine.Contains($env:FS_PROFILE) } | ForEach-Object { $_.ProcessId }")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                         env=dict(os.environ, FS_PROFILE=str(profile)), capture_output=True, text=True,
                         creationflags=NO_WINDOW)
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


def _pid_running(pid: int) -> bool:
    """Windows: processo ancora vivo (OpenProcess + GetExitCodeProcess, nessun processo esterno)."""
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def _wait_window(profile: Path, appear_timeout: float = 20) -> bool:
    """Attende che la finestra compaia e poi che venga chiusa. False se non e' mai comparsa.

    Windows: PowerShell solo per scoprire i pid del profilo (all'apertura e una conferma alla fine);
    durante la sessione si controllano quei pid via API, senza lanciare processi ogni secondo."""
    end = time.time() + appear_timeout
    if sys.platform != "win32":
        while not _window_open(profile):
            if time.time() > end:
                return False
            time.sleep(0.5)
        while _window_open(profile):
            time.sleep(2.0)
        return True
    pids = _profile_pids(profile)
    while not pids:
        if time.time() > end:
            return False
        time.sleep(1.0)
        pids = _profile_pids(profile)
    while pids:
        while any(_pid_running(p) for p in pids):
            time.sleep(1.5)
        pids = _profile_pids(profile)  # conferma: il browser puo' aver riavviato i suoi processi
    return True


def _install_linux_launcher() -> None:
    """AppImage Linux: scrive in ~/.local/share il file .desktop e l'icona, cosi' menu e barra
    delle applicazioni mostrano PhotoScan (e non l'icona di Chromium). Si aggiorna a ogni avvio."""
    appimage = os.environ.get("APPIMAGE")
    appdir = os.environ.get("APPDIR")
    if sys.platform == "win32" or not appimage or not appdir:
        return
    try:
        data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        icon_src = Path(appdir) / "PhotoScan.png"
        icon_dst = data / "icons" / "photoscan.png"
        if icon_src.exists():
            icon_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(icon_src, icon_dst)
        lines = [
            "[Desktop Entry]",
            "Type=Application",
            "Name=PhotoScan",
            "Comment=Organizza l'archivio di foto e video, tutto sul tuo computer",
            f'Exec="{appimage}"',
            f"Icon={icon_dst}",
            "Categories=Graphics;Photography;",
            "Terminal=false",
            "StartupWMClass=PhotoScan",
        ]
        text = "\n".join(lines) + "\n"
        target = data / "applications" / "photoscan.desktop"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
    except OSError:
        pass  # menu non aggiornabile: la finestra si apre comunque


# ------------------------------------------------------------------ main
def main() -> int:
    state = _read_state()
    owner_proc: subprocess.Popen | None = None
    restart = {"requested": False}
    if state and _health(state["port"]):
        port = state["port"]          # seconda istanza: il server c'e' gia', non lo tocchiamo
    else:
        try:
            LOG.write_text("", encoding="utf-8")   # log riscritto a ogni avvio
        except OSError:
            pass
        _install_pending_update()     # server spento: i file del programma si possono sostituire
        port = free_port()
        owner_proc = _start_server(port)
        if not _wait_healthy(port, owner_proc):
            _kill_tree(owner_proc.pid)
            return _fail("Il server di PhotoScan non è partito.")
        _write_state(port, owner_proc.pid)

    url = f"http://127.0.0.1:{port}/"
    try:
        # Windows: finestra WebView2 (pywebview). Linux: browser in modalita' app (niente GTK/Qt nel pacchetto).
        if sys.platform == "win32" and _open_app_window(url, restart):
            return 0
        browser = find_browser()
        if browser:
            PROFILE.mkdir(parents=True, exist_ok=True)
            extra = [] if sys.platform == "win32" else ["--class=PhotoScan"]  # WM_CLASS = StartupWMClass del .desktop
            if sys.platform != "win32":
                _install_linux_launcher()
            bproc = subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={PROFILE}", *extra,
                                      f"--window-size={WINDOW_SIZE}", "--no-first-run", "--no-default-browser-check",
                                      "--disable-session-crashed-bubble"])
            import threading
            # il lanciatore del browser spesso esce subito (passa la finestra a un'istanza gia' aperta):
            # wait() in un thread lo raccoglie, senza lasciare un processo zombie su Linux
            threading.Thread(target=bproc.wait, daemon=True, name="browser-reaper").start()
            if not _wait_window(PROFILE):
                return _fail(f"La finestra di PhotoScan non si è aperta ({browser}).")
        else:
            # nessun Edge/Chrome: browser predefinito; non sappiamo quando la scheda si chiude,
            # quindi il server resta acceso finche' non si preme OK.
            webbrowser.open(url)
            _message("PhotoScan",
                     "Edge e Chrome non sono stati trovati: PhotoScan è aperto nel browser predefinito.\n\n"
                     "Premi OK qui quando hai finito, per spegnere il server.")
        return 0
    finally:
        if owner_proc is not None:
            _kill_tree(owner_proc.pid)
            if _server_stopped(owner_proc, port):
                _clear_state()
        elif restart["requested"] and state:
            _kill_tree(state["pid"])    # seconda istanza: il riavvio spegne anche il server condiviso
            if _server_stopped(None, state["port"]):
                _clear_state()
        if FROZEN and _install_exe_on_exit():
            pass                      # lo script installa e riapre PhotoScan
        elif restart["requested"]:
            _relaunch()


if __name__ == "__main__":
    sys.exit(main())
