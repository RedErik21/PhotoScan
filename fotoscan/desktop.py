"""Archivio Foto - avvio come applicazione: server FastAPI nascosto + finestra dedicata (Edge/Chrome in modalita' app).

Uso: pythonw -m fotoscan.desktop   (oppure l'icona 'FotoScan' sul Desktop, o 'python -m fotoscan ui')

- Se il server e' gia' attivo (seconda istanza) viene riusato: si apre solo la finestra.
- Chiudendo la finestra il server si spegne e con lui un'eventuale indicizzazione
  (riprendibile: alla prossima apertura si riparte dai file non ancora elaborati).
- Errori (server che non parte, nessun browser) sono mostrati in una finestra con il percorso del log.
"""
from __future__ import annotations

import ctypes
import json
import os
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
def _message(title: str, text: str, error: bool = False) -> None:
    """Finestra di dialogo nativa (funziona anche con pythonw, senza console)."""
    flags = (MB_ICONERROR if error else MB_ICONINFO) | MB_TOPMOST | MB_OK
    ctypes.windll.user32.MessageBoxW(0, text, title, flags)


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
    _message("FotoScan - errore", testo, error=True)
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


def _start_server(port: int) -> subprocess.Popen:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    with open(LOG, "a", encoding="utf-8") as log:  # azzerato da main() a ogni avvio
        log.write(f"FotoScan: avvio server su 127.0.0.1:{port}\n")
        log.flush()
        return subprocess.Popen([_python(), "-m", "fotoscan.server", "--port", str(port)], cwd=str(ROOT),
                                env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=NO_WINDOW)


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
            ok, out = updater.install_requirements(ROOT, _python())
            _log_line(out)
            if not ok:
                _message("FotoScan - aggiornamento",
                         f"FotoScan è stato aggiornato alla versione {res['to']}, ma l'installazione delle nuove "
                         "librerie non è riuscita. Se qualcosa non funziona, esegui nella cartella di FotoScan:\n\n"
                         ".venv\\Scripts\\python.exe -m pip install -r requirements.txt\n\n"
                         f"Dettagli nel log:\n{LOG}", error=True)
    except Exception as e:  # noqa: BLE001
        _log_line(f"aggiornamento non installato: {e}")
        _message("FotoScan - aggiornamento", f"{e}\n\nFotoScan si avvia con la versione precedente.", error=True)


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
            "Chiudendo FotoScan ora viene interrotta. L'analisi riprende da dove si è fermata; "
            "la copia dal telefono riprende saltando i file già copiati." + "\n\nChiudere comunque?")
    flags = MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_TOPMOST
    return ctypes.windll.user32.MessageBoxW(0, text, "FotoScan", flags) == IDYES


def _may_close(base: str) -> bool:
    """Chiusura consentita? Se c'e' un'operazione in corso chiede conferma."""
    try:
        desc = _busy_description(base)
    except Exception:
        desc = None
    return desc is None or _confirm_close(desc)


def _set_titlebar_theme(window, dark: bool) -> None:
    """Sincronizza il colore chiaro/scuro della barra del titolo nativa di Windows 10/11 (DWM)."""
    if sys.platform != "win32":
        return
    try:
        native = getattr(window, "native", None)
        hwnd = int(native.Handle.ToInt64()) if native is not None and hasattr(native, "Handle") else 0
        if not hwnd:
            return
        val = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 2004+ / Win11), 19 = fallback Win10 precedenti
        for attr in (20, 19):
            if dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), ctypes.c_int(attr), ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
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
        """Chiude e riapre FotoScan (installa l'aggiornamento pronto). La conferma la chiede il frontend."""
        if restart is None:
            return False
        restart["requested"] = True
        allowed["ok"] = True
        window.destroy()
        return True

    window.expose(minimize, toggle_maximize, close, force_close, busy_description, is_frameless, set_theme, restart_app)


def _set_app_identity() -> None:
    """Nome e icona di FotoScan nella barra delle applicazioni (senza, Windows mostra l'icona di Python)."""
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
        window = webview.create_window(
            APP_NAME, url, width=1400, height=900, min_size=(420, 520),
            resizable=True, frameless=False, easy_drag=False, background_color="#f6f6f9",
        )
        allowed = {"ok": False}
        _expose_window_controls(window, url, allowed, frameless=False, restart=restart)

        def on_closing() -> bool:
            # False = annulla la chiusura (Alt+F4 o X di sistema). Dopo una conferma gia' data non si chiede di nuovo.
            if allowed["ok"]:
                return True
            if _may_close(url):
                allowed["ok"] = True
                return True
            return False

        window.events.closing += on_closing
        webview.start(icon=str(APP_ICON))
    except Exception as e:  # runtime WebView2 mancante, ecc.
        with open(LOG, "a", encoding="utf-8") as log:
            log.write(f"pywebview non disponibile: {e}\n")
        return False
    return True


def _window_open(profile: Path) -> bool:
    """True se almeno un processo del browser usa il profilo dedicato (cioe' la finestra e' aperta)."""
    ps = ("@(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
          "$_.CommandLine.Contains($env:FS_PROFILE) }).Count")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                         env=dict(os.environ, FS_PROFILE=str(profile)), capture_output=True, text=True,
                         creationflags=NO_WINDOW)
    try:
        return int(out.stdout.strip() or 0) > 0
    except ValueError:
        return False


def _wait_window(profile: Path, appear_timeout: float = 20) -> bool:
    """Attende che la finestra compaia e poi che venga chiusa. False se non e' mai comparsa."""
    end = time.time() + appear_timeout
    while not _window_open(profile):
        if time.time() > end:
            return False
        time.sleep(0.5)
    while _window_open(profile):
        time.sleep(1.5)
    return True


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
            return _fail("Il server di FotoScan non è partito.")
        _write_state(port, owner_proc.pid)

    url = f"http://127.0.0.1:{port}/"
    try:
        if _open_app_window(url, restart):
            return 0
        browser = find_browser()
        if browser:
            PROFILE.mkdir(parents=True, exist_ok=True)
            subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={PROFILE}",
                              f"--window-size={WINDOW_SIZE}", "--no-first-run", "--no-default-browser-check",
                              "--disable-session-crashed-bubble"])
            if not _wait_window(PROFILE):
                return _fail(f"La finestra di FotoScan non si è aperta ({browser}).")
        else:
            # nessun Edge/Chrome: browser predefinito; non sappiamo quando la scheda si chiude,
            # quindi il server resta acceso finche' non si preme OK.
            webbrowser.open(url)
            _message("FotoScan",
                     "Edge e Chrome non sono stati trovati: FotoScan è aperto nel browser predefinito.\n\n"
                     "Premi OK qui quando hai finito, per spegnere il server.")
        return 0
    finally:
        if owner_proc is not None:
            _kill_tree(owner_proc.pid)
            _clear_state()
        elif restart["requested"] and state:
            _kill_tree(state["pid"])    # seconda istanza: il riavvio spegne anche il server condiviso
            _clear_state()
        if FROZEN and _install_exe_on_exit():
            pass                      # lo script installa e riapre FotoScan
        elif restart["requested"]:
            _relaunch()


if __name__ == "__main__":
    sys.exit(main())
