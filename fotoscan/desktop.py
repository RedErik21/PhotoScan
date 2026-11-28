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
import threading
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
START_TIMEOUT = 120  # secondi: il primo avvio puo' importare torch (si estende finche' il log avanza, fino a 300 s)
START_HARD_LIMIT = 300
LOCK = CACHE / "desktop.lock"            # un solo avvio alla volta: il secondo aspetta e poi riusa il server del primo
LAUNCHERS = CACHE / "launchers"          # un file per finestra aperta: l'ultima che si chiude spegne il server
LAST_PORT = CACHE / "last_port.txt"      # stessa porta a ogni avvio: l'origine (e il localStorage) non cambia
WEBVIEW_STORAGE = CACHE / "webview"      # profilo persistente di WebView2 (preferenze, «Più tardi» del banner)
PORT_RANGE = range(PREFERRED_PORT, PREFERRED_PORT + 20)
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
def _external_env() -> dict:
    """Ambiente per i programmi di sistema (browser, zenity). L'AppImage/PyInstaller imposta LD_LIBRARY_PATH sulle
    proprie librerie: Chromium e zenity di sistema, con quelle, possono non partire."""
    env = dict(os.environ)
    if sys.platform != "win32" and FROZEN:
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig is not None:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def _zenity(args: list[str]) -> int | None:
    """Finestra di dialogo di GNOME/KDE tramite zenity. None = zenity non disponibile."""
    if not shutil.which("zenity"):
        return None
    return subprocess.run(["zenity", *args], capture_output=True, env=_external_env()).returncode


def _message(title: str, text: str, error: bool = False) -> bool:
    """Finestra di dialogo nativa (funziona anche con pythonw, senza console). False = nessun modo di mostrarla."""
    if sys.platform != "win32":
        kind = "--error" if error else "--info"
        if _zenity([kind, f"--title={title}", f"--text={text}", "--width=480"]) is None:
            _log_line(f"{title}: {text}")
            print(f"{title}: {text}", file=sys.stderr)
            return False
        return True
    flags = (MB_ICONERROR if error else MB_ICONINFO) | MB_TOPMOST | MB_OK
    ctypes.windll.user32.MessageBoxW(0, text, title, flags)
    return True


def _busy_notice(title: str, text: str):
    """Avviso non bloccante durante un'attesa lunga (es. pip install). Ritorna la funzione che lo chiude."""
    if sys.platform != "win32":
        if not shutil.which("zenity"):
            _log_line(f"{title}: {text}")
            return lambda: None
        try:
            proc = subprocess.Popen(["zenity", "--progress", "--pulsate", "--no-cancel", "--auto-close",
                                     f"--title={title}", f"--text={text}", "--width=480"],
                                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=_external_env())
        except OSError:
            return lambda: None

        def close_linux() -> None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return close_linux

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


def _can_bind(port: int) -> int | None:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return s.getsockname()[1]
        except OSError:
            return None


def free_port(preferred: int = PREFERRED_PORT, exclude: tuple[int, ...] = ()) -> int:
    """Porta libera: l'ultima usata (l'origine resta la stessa a ogni avvio), poi la preferita, poi 8766-8784,
    infine una qualsiasi."""
    order: list[int] = []
    try:
        order.append(int(LAST_PORT.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        pass
    order += [preferred, *[p for p in PORT_RANGE if p != preferred], 0]
    for port in order:
        if port in exclude:
            continue
        got = _can_bind(port)
        if got is not None and got not in exclude:
            return got
    raise RuntimeError("nessuna porta libera")


def find_browser() -> str | None:
    """Edge, poi Chrome. None = usare il browser predefinito."""
    if sys.platform != "win32":
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


def _profile_for(browser: str) -> Path:
    """Profilo dedicato del browser. Chromium installato come snap non puo' usare le cartelle nascoste della home."""
    if sys.platform != "win32":
        try:
            resolved = str(Path(browser).resolve())
        except OSError:
            resolved = browser
        if "/snap/" in browser or "/snap/" in resolved:
            return Path.home() / "snap" / "chromium" / "common" / "PhotoScan" / "webprofile"
    return PROFILE


def _health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_healthy(port: int, proc: subprocess.Popen, timeout: float = START_TIMEOUT, notice_after: float = 6.0) -> bool:
    """Attende il server. Dopo `timeout` continua solo se il log avanza (primo avvio lento: antivirus, disco),
    fino a START_HARD_LIMIT. Dopo qualche secondo mostra un avviso, cosi' l'utente non riclicca l'icona."""
    start = time.time()
    close_notice = None
    try:
        while True:
            now = time.time()
            if proc.poll() is not None:
                return False
            if _health(port):
                return True
            if close_notice is None and now - start > notice_after:
                close_notice = _busy_notice("PhotoScan - avvio", "PhotoScan si sta avviando...\n\n"
                                            "Al primo avvio o con l'antivirus può servire qualche minuto: "
                                            "la finestra si apre da sola.")
            if now - start > timeout:
                try:
                    alive = now - LOG.stat().st_mtime < 30
                except OSError:
                    alive = False
                if not alive or now - start > START_HARD_LIMIT:
                    return False
            time.sleep(0.5)
    finally:
        if close_notice is not None:
            close_notice()


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


def _proc_ctime(pid: int) -> int | None:
    """Istante di creazione del processo (identifica il processo: un pid puo' essere riciclato). None = non esiste."""
    try:
        if sys.platform == "win32":
            from ctypes import wintypes

            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            h = k32.OpenProcess(0x1000, False, pid)
            if not h:
                return None
            try:
                c, e, k, u = (wintypes.FILETIME() for _ in range(4))
                if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                    return None
                return (c.dwHighDateTime << 32) | c.dwLowDateTime
            finally:
                k32.CloseHandle(h)
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return int(stat.rsplit(")", 1)[1].split()[19])   # campo 22: istante di avvio in tick
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _state_pid_valid(state: dict) -> bool:
    """Il pid dello stato e' ancora il server avviato da PhotoScan? Senza `ctime` (stato vecchio) non si puo' dire."""
    ctime = state.get("ctime")
    return bool(ctime) and _proc_ctime(int(state.get("pid") or 0)) == ctime


def _read_state() -> dict | None:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_state(port: int, pid: int) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"port": port, "pid": pid, "ctime": _proc_ctime(pid)}), encoding="utf-8")
    try:
        LAST_PORT.write_text(str(port), encoding="utf-8")
    except OSError:
        pass


class _StartupLock:
    """Blocco su file: due avvii ravvicinati non lanciano due server sullo stesso catalogo (il secondo aspetta
    che il primo abbia finito e poi riusa il suo server)."""

    def __init__(self, timeout: float = START_HARD_LIMIT + 60) -> None:
        self.timeout, self.fh = timeout, None

    def __enter__(self):
        CACHE.mkdir(parents=True, exist_ok=True)
        try:
            self.fh = open(LOCK, "a+b")
            self.fh.seek(0, os.SEEK_END)
            if self.fh.tell() == 0:
                self.fh.write(b"0")
                self.fh.flush()
        except OSError:
            self.fh = None
            return self
        end = time.time() + self.timeout
        while True:
            try:
                self.fh.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() > end:       # blocco incastrato: si procede comunque, meglio di non partire
                    _log_line("blocco di avvio non ottenuto: proseguo senza")
                    return self
                time.sleep(0.3)

    def __exit__(self, *exc) -> None:
        if self.fh is None:
            return
        try:
            self.fh.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.fh.close()


def _register_launcher() -> Path:
    """Segna questa finestra come aperta (un file per processo, con l'istante di creazione per riconoscerlo)."""
    LAUNCHERS.mkdir(parents=True, exist_ok=True)
    f = LAUNCHERS / f"{os.getpid()}.json"
    f.write_text(json.dumps({"pid": os.getpid(), "ctime": _proc_ctime(os.getpid())}), encoding="utf-8")
    return f


def _other_launchers() -> list[int]:
    """Altre finestre di PhotoScan ancora aperte (i file di processi morti o riciclati si ripuliscono)."""
    alive = []
    if not LAUNCHERS.is_dir():
        return alive
    for f in LAUNCHERS.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            pid = int(data["pid"])
        except (OSError, ValueError, KeyError):
            f.unlink(missing_ok=True)
            continue
        if pid == os.getpid():
            continue
        if data.get("ctime") and _proc_ctime(pid) == data["ctime"]:
            alive.append(pid)
        else:
            f.unlink(missing_ok=True)
    return alive


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


def _spawn_and_wait(first_port: int | None = None) -> tuple[subprocess.Popen | None, int]:
    """Avvia il server e attende che risponda. Se la porta e' stata presa nel frattempo riprova con un'altra."""
    port = first_port or free_port()
    tried: list[int] = []
    for attempt in range(2):
        proc = _start_server(port)
        if _wait_healthy(port, proc):
            return proc, port
        _kill_tree(proc.pid)
        tried.append(port)
        if attempt == 0 and proc.poll() is not None:
            try:
                port = free_port(exclude=tuple(tried))
            except RuntimeError:
                break
            continue
        break
    return None, port


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
    """PhotoScan.exe / AppImage: installa il pacchetto approvato (o, con l'installazione automatica, scaricato) a
    programma chiuso. True = installazione avviata (Windows: lo script riapre PhotoScan)."""
    try:
        from . import updater
        from .config import load_config

        cfg = load_config()
        return updater.start_exe_update(cfg.cache_dir, Path(sys.executable).parent, force=cfg.update_auto_install)
    except Exception as e:  # noqa: BLE001 - la chiusura non si blocca per un aggiornamento
        _log_line(f"aggiornamento non installato: {e}")
        return False


def _ensure_runtime() -> None:
    """PhotoScan.exe leggero: il runtime (PyTorch, ...) sta a parte. Se manca o ha la versione sbagliata lo si scarica
    (dopo conferma: sono GB). Senza, l'app si apre ma l'analisi delle foto non puo' partire."""
    if not FROZEN:
        return
    try:
        from . import runtime, updater
        from .config import load_config

        st = runtime.status()
        if st.get("ok"):
            return
        _log_line(f"runtime non pronto: {st.get('reason')}")
        text = ("PhotoScan ha bisogno del componente di calcolo (PyTorch, versione "
                f"{st['required']}) che non è ancora su questo PC: {st.get('reason') or 'non trovato'}.\n\n"
                "Scaricarlo ora da GitHub? Sono alcuni GB e serve una volta sola per versione.")
        if sys.platform == "win32":
            if ctypes.windll.user32.MessageBoxW(0, text, "PhotoScan", MB_YESNO | MB_ICONINFO | MB_TOPMOST) != IDYES:
                return
        elif _zenity(["--question", "--title=PhotoScan", f"--text={text}", "--width=480"]) not in (0,):
            return
        close = _busy_notice("PhotoScan - componente di calcolo",
                             "Sto scaricando il componente di calcolo: può richiedere diversi minuti.\n"
                             "La finestra di PhotoScan si apre da sola appena finito.")
        try:
            updater.ensure_runtime(load_config().cache_dir)
        finally:
            close()
    except Exception as e:  # noqa: BLE001
        _log_line(f"runtime non installato: {e}")
        _message("PhotoScan - componente di calcolo",
                 f"Non sono riuscito a scaricare il componente di calcolo:\n{e}\n\n"
                 "PhotoScan si apre lo stesso, ma l'analisi delle foto non funzionerà finché non viene installato "
                 "(riavvia PhotoScan per riprovare, oppure reinstallalo con PhotoScan-Setup).", error=True)


def _prune_runtime() -> None:
    """Dopo un avvio riuscito si tolgono i runtime che non servono piu' (quello vecchio resta finche' il nuovo non ha
    funzionato). Si tiene anche quello richiesto da un aggiornamento gia' scaricato e non ancora installato."""
    try:
        from . import runtime, updater
        from .config import load_config

        keep = {runtime.required_version()}
        pending = updater.read_pending(load_config().cache_dir)
        if pending and pending.get("runtime_version"):
            keep.add(str(pending["runtime_version"]))
        removed = runtime.prune(keep)
        if removed:
            _log_line(f"runtime vecchi rimossi: {', '.join(removed)}")
    except Exception as e:  # noqa: BLE001
        _log_line(f"pulizia runtime non riuscita: {e}")


def _relaunch() -> None:
    """Nuova istanza del launcher (dopo «Installa e riavvia»): installa l'aggiornamento e riapre la finestra."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    appimage = os.environ.get("APPIMAGE")      # Linux: sys.executable sta nel mount dell'AppImage, che sparisce all'uscita
    if FROZEN:
        cmd = [appimage] if appimage and sys.platform != "win32" else [sys.executable]
    else:
        cmd = [sys.executable, "-m", "fotoscan.desktop"]
    extra = {"start_new_session": True} if sys.platform != "win32" else {"creationflags": flags}
    subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, env=_external_env() if appimage else None, **extra)


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


def _webview2_installed() -> bool:
    """Windows: runtime WebView2 presente (Edge aggiornato lo porta con se')."""
    if sys.platform != "win32":
        return True
    try:
        import winreg

        guid = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        for hive, path in ((winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
                           (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
                           (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{guid}")):
            try:
                with winreg.OpenKey(hive, path) as k:
                    pv, _ = winreg.QueryValueEx(k, "pv")
                    if pv and pv != "0.0.0.0":
                        return True
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        return True      # non si riesce a verificare: si prova comunque
    return False


def _open_app_window(url: str, restart: dict | None = None) -> bool:
    """Finestra con pywebview (WebView2 di Edge). True = finestra aperta e poi chiusa.
    False = pywebview/WebView2 non disponibili o non partiti: si ripiega su Edge/Chrome in modalita' app.
    restart["requested"] = True se la finestra e' stata chiusa con «Installa e riavvia»."""
    try:
        import webview
    except ImportError:
        return False
    if not _webview2_installed():
        _log_line("WebView2 non installato: apro Edge/Chrome in modalità app")
        return False
    _set_app_identity()
    t0 = time.time()
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
        WEBVIEW_STORAGE.mkdir(parents=True, exist_ok=True)
        # edgechromium esplicito: senza, pywebview puo' ripiegare in silenzio sul vecchio motore di Internet Explorer.
        # private_mode=False: preferenze e localStorage restano tra un avvio e l'altro.
        webview.start(icon=str(APP_ICON), gui="edgechromium" if sys.platform == "win32" else None,
                      private_mode=False, storage_path=str(WEBVIEW_STORAGE))
    except Exception as e:  # runtime WebView2 mancante, ecc.
        _log_line(f"pywebview non disponibile: {e}")
        return False
    if time.time() - t0 < 1.5:       # nessuna finestra e' rimasta aperta cosi' poco: il motore non e' partito
        _log_line("la finestra pywebview si è chiusa subito: ripiego su Edge/Chrome")
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
    return bool(_profile_pids(profile))


def _profile_pids(profile: Path) -> list[int]:
    """Windows: pid dei processi del browser che usano il profilo dedicato (una sola chiamata PowerShell).
    Solleva OSError se PowerShell non e' utilizzabile."""
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
          "$_.CommandLine.Contains($env:FS_PROFILE) } | ForEach-Object { $_.ProcessId }")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                         env=dict(os.environ, FS_PROFILE=str(profile)), capture_output=True, text=True,
                         creationflags=NO_WINDOW, timeout=60)
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


def _wait_window(profile: Path, bproc: subprocess.Popen | None = None, appear_timeout: float = 20) -> bool:
    """Attende che la finestra compaia e poi che venga chiusa. False se non e' mai comparsa.

    Windows: PowerShell solo per scoprire i pid del profilo (all'apertura e una conferma alla fine);
    durante la sessione si controllano quei pid via API, senza lanciare processi ogni secondo.
    Se PowerShell/WMI non sono utilizzabili (criteri aziendali) si aspetta il processo del browser."""
    end = time.time() + appear_timeout
    if sys.platform != "win32":
        while not _window_open(profile):
            if time.time() > end:
                return False
            time.sleep(0.5)
        while _window_open(profile):
            time.sleep(2.0)
        return True
    try:
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
    except (OSError, subprocess.SubprocessError) as e:
        _log_line(f"controllo della finestra via PowerShell non riuscito ({e}): attendo il browser")
        if bproc is None:
            return False
        bproc.wait()
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
def _stop_server(owner_proc: subprocess.Popen | None, state: dict | None, port: int) -> bool:
    """Spegne il server: quello avviato da questo processo, oppure quello dello stato (solo se il pid e' ancora
    il server di PhotoScan: un pid riciclato da un altro programma non si tocca). True = spento."""
    if owner_proc is not None:
        _kill_tree(owner_proc.pid)
        return _server_stopped(owner_proc, port)
    st = _read_state() or state
    if not st:
        return True
    if _state_pid_valid(st):
        _kill_tree(int(st["pid"]))
        return _server_stopped(None, int(st["port"]))
    if not _health(int(st["port"])):
        return True            # il server non c'e' piu': solo lo stato da pulire
    _log_line(f"pid {st.get('pid')} non verificabile: il server non viene terminato")
    return False


def _no_browser_wait(url: str) -> None:
    """Nessun browser e nessuna finestra di dialogo (Linux senza zenity): il server resta acceso finche' il processo
    non viene interrotto (Ctrl+C), altrimenti la pagina si aprirebbe su un server gia' spento."""
    print(f"PhotoScan e' attivo su {url} - premi Ctrl+C per chiuderlo.", file=sys.stderr)
    _log_line(f"nessun browser ne' zenity: server attivo su {url}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    restart = {"requested": False}
    my_file = _register_launcher()
    owner_proc: subprocess.Popen | None = None
    state: dict | None = None
    port = 0
    try:
        with _StartupLock():
            state = _read_state()
            if state and _health(state["port"]) and (_state_pid_valid(state) or not state.get("ctime")):
                port = state["port"]          # un altro avvio ha gia' il server pronto: lo riusiamo, non lo tocchiamo
            else:
                if state:
                    _clear_state()            # stato di un server morto o di un altro programma sulla porta
                    state = None
                try:
                    LOG.write_text("", encoding="utf-8")   # log riscritto a ogni avvio
                except OSError:
                    pass
                _ensure_runtime()             # PhotoScan.exe leggero: serve il runtime (PyTorch) a parte
                _install_pending_update()     # server spento: i file del programma si possono sostituire
                try:
                    owner_proc, port = _spawn_and_wait()
                except Exception as e:  # noqa: BLE001
                    _log_line(f"avvio del server fallito: {e}")
                    return _fail(f"Il server di PhotoScan non è partito ({e}).")
                if owner_proc is None:
                    return _fail("Il server di PhotoScan non è partito.")
                _write_state(port, owner_proc.pid)
                state = _read_state()
                threading.Thread(target=_prune_runtime, daemon=True, name="runtime-prune").start()

        url = f"http://127.0.0.1:{port}/"
        # Windows: finestra WebView2 (pywebview). Linux: browser in modalita' app (niente GTK/Qt nel pacchetto).
        if sys.platform == "win32" and _open_app_window(url, restart):
            return 0
        browser = find_browser()
        if browser:
            profile = _profile_for(browser)
            profile.mkdir(parents=True, exist_ok=True)
            extra = [] if sys.platform == "win32" else ["--class=PhotoScan"]  # WM_CLASS = StartupWMClass del .desktop
            if sys.platform != "win32":
                _install_linux_launcher()
            bproc = subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={profile}", *extra,
                                      f"--window-size={WINDOW_SIZE}", "--no-first-run", "--no-default-browser-check",
                                      "--disable-session-crashed-bubble"], env=_external_env())
            # il lanciatore del browser spesso esce subito (passa la finestra a un'istanza gia' aperta):
            # wait() in un thread lo raccoglie, senza lasciare un processo zombie su Linux
            threading.Thread(target=bproc.wait, daemon=True, name="browser-reaper").start()
            if not _wait_window(profile, bproc):
                return _fail(f"La finestra di PhotoScan non si è aperta ({browser}).")
        else:
            # nessun Edge/Chrome: browser predefinito; non sappiamo quando la scheda si chiude,
            # quindi il server resta acceso finche' non si preme OK.
            webbrowser.open(url)
            shown = _message("PhotoScan",
                             "Edge e Chrome non sono stati trovati: PhotoScan è aperto nel browser predefinito.\n\n"
                             "Premi OK qui quando hai finito, per spegnere il server.")
            if not shown:
                _no_browser_wait(url)
        return 0
    finally:
        my_file.unlink(missing_ok=True)
        others = _other_launchers()
        # il server si spegne con l'ultima finestra (o su «Installa e riavvia»): chiudere la prima di due finestre
        # non deve lasciare l'altra senza server
        if not others or restart["requested"]:
            stopped = _stop_server(owner_proc, state, port)
            if stopped:
                _clear_state()
                try:        # disinstallazione richiesta dall'app (Linux): a server spento si puo' togliere tutto
                    from . import uninstall
                    from .config import load_config

                    done = uninstall.finish_pending(load_config().cache_dir, ROOT, _log_line)
                    if done:
                        _log_line("disinstallazione: " + ", ".join(done))
                        return 0
                except Exception as e:  # noqa: BLE001
                    _log_line(f"disinstallazione non completata: {e}")
            installed = FROZEN and stopped and _install_exe_on_exit()
            if installed and sys.platform == "win32":
                pass                  # lo script installa e riapre PhotoScan
            elif restart["requested"]:
                _relaunch()           # Linux: l'AppImage e' gia' stato sostituito, si riapre qui


if __name__ == "__main__":
    sys.exit(main())
