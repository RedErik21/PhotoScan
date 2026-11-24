"""Blocco della rete in uscita per tutto il processo Python di FotoScan.

Installato all'import del pacchetto `fotoscan` (quindi anche nel server e in ogni sottoprocesso
`python -m fotoscan...`). Usa un audit hook di Python (`sys.addaudithook`), che una volta installato
non si puo' piu' rimuovere: ogni connessione, risoluzione DNS o invio di pacchetti verso un indirizzo
diverso da questo PC (127.0.0.1 / ::1 / localhost) viene rifiutata con ConnectionRefusedError.
Vale per tutto il codice Python: FotoScan, transformers, huggingface_hub, urllib, requests, httpx...

Unica eccezione: `download_window()`, aperta solo quando un modello manca dalla cache locale o per
gli aggiornamenti di FotoScan (fotoscan/updater.py: elenco delle Release e zip da GitHub).
Anche allora sono raggiungibili soltanto Hugging Face e GitHub e solo per il tempo della richiesta.
Nessuna foto o dato del catalogo passa da queste funzioni.

Limite noto: codice nativo (C/C++) che apre socket per conto proprio non passa dagli audit hook.
torch e onnxruntime non lo fanno; per una garanzia anche a livello di sistema serve il firewall.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("fotoscan")

# Server raggiungibili SOLO durante una finestra di download (suffissi di dominio).
DOWNLOAD_HOSTS = ("huggingface.co", "hf.co", "github.com", "githubusercontent.com")

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", ""}

_installed = False
_window = 0                    # finestre di download aperte (contatore, thread-safe con _lock)
_lock = threading.Lock()
_resolved: set[str] = set()    # IP dei server di download risolti mentre la finestra e' aperta
_blocked_count = 0
_reason: str | None = None     # motivo dell'ultima finestra di download aperta
_local = threading.local()     # evita la ricorsione mentre si scrive il registro

# Registro dei tentativi verso internet (condiviso da server e sottoprocessi, una riga JSON per evento).
LOG_FILE = Path(os.environ.get("FOTOSCAN_NETLOG")
                or Path(os.environ.get("FOTOSCAN_CACHE") or Path(__file__).resolve().parent.parent / "cache") / "rete.jsonl")
LOG_MAX_BYTES = 512 * 1024
_log_lock = threading.Lock()
_last_logged: dict[tuple, float] = {}  # (host, esito, origine) -> ultima scrittura (niente raffiche)

# Moduli di rete "di passaggio": l'origine e' il primo modulo chiamante fuori da questi.
_NET_PREFIXES = ("socket", "ssl", "selectors", "http", "urllib", "requests", "httpx", "httpcore",
                 "anyio", "asyncio", "h11", "certifi", "concurrent", "threading", "contextlib", "fotoscan.netguard")


class NetworkBlocked(ConnectionRefusedError):
    """Connessione verso internet rifiutata da FotoScan (sottoclasse di OSError: le librerie la gestiscono)."""


def _host_str(host: Any) -> str:
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    return str(host or "").strip().lower().rstrip(".")


def is_loopback(host: Any) -> bool:
    h = _host_str(host)
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _is_download_host(host: Any) -> bool:
    h = _host_str(host)
    return any(h == d or h.endswith("." + d) for d in DOWNLOAD_HOSTS)


def _allowed(host: Any) -> bool:
    if is_loopback(host):
        return True
    if _window > 0:
        h = _host_str(host)
        return _is_download_host(h) or h.split("%", 1)[0] in _resolved
    return False


def _is_net_module(mod: str) -> bool:
    return any(mod == p or mod.startswith(p + ".") or (p in ("urllib", "http") and mod.startswith(p))
               for p in _NET_PREFIXES)


def _origin() -> tuple[str, str]:
    """(libreria che ha aperto la connessione, funzione di FotoScan da cui parte la richiesta)."""
    lib, fs = "", ""
    f = sys._getframe(1)
    while f is not None:
        mod = str(f.f_globals.get("__name__", ""))
        if not lib and mod and not _is_net_module(mod):
            lib = mod
        if mod.startswith("fotoscan.") and mod != "fotoscan.netguard":
            fs = f"{mod}:{f.f_code.co_qualname}"
            break
        f = f.f_back
    return lib, fs


def _purpose(lib: str, fs: str) -> str:
    """Spiegazione in italiano del perche' di una connessione."""
    if _window > 0 and _reason:
        return _reason
    if "routes_network" in fs:
        return "Prova del blocco dalle Impostazioni"
    if "updater" in fs or "routes_update" in fs:
        return "Aggiornamenti di FotoScan"
    if "embedder" in fs:
        return "Caricamento del modello di riconoscimento"
    if "faces" in fs:
        return "Modelli per il riconoscimento dei volti"
    if "routes_models" in fs:
        return "Verifica di un modello personalizzato"
    if lib.startswith(("huggingface_hub", "transformers")):
        return "Hugging Face (controllo o download modelli)"
    if lib.startswith(("torch", "onnxruntime")):
        return "Libreria di calcolo (" + lib.split(".")[0] + ")"
    return "Origine non riconosciuta" + (f" ({lib})" if lib else "")


def _process_name() -> str:
    argv = list(getattr(sys, "orig_argv", sys.argv))
    if "-m" in argv:
        i = argv.index("-m")
        mod = argv[i + 1] if i + 1 < len(argv) else ""
        rest = argv[i + 2:i + 3]
        if mod == "fotoscan.server":
            return "server"
        return " ".join([mod.replace("fotoscan.", "fotoscan "), *rest]).strip()
    return Path(sys.argv[0] or "python").stem


def _record(event: str, host: Any, port: Any, allowed: bool) -> None:
    """Annota il tentativo nel registro (mai un'eccezione: il registro non deve rompere nulla)."""
    if getattr(_local, "busy", False):
        return
    _local.busy = True
    try:
        lib, fs = _origin()
        h = _host_str(host)
        key = (h, allowed, fs or lib)
        now = time.monotonic()
        if now - _last_logged.get(key, -99.0) < 5.0:
            return
        _last_logged[key] = now
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "outcome": "consentito" if allowed else "bloccato",
            "host": h, "port": port if isinstance(port, int) else None,
            "event": event.removeprefix("socket."), "purpose": _purpose(lib, fs),
            "library": lib, "caller": fs, "process": _process_name(), "pid": os.getpid(),
        }
        with _log_lock:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
                keep = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-1000:]
                LOG_FILE.write_text("\n".join(keep) + "\n", encoding="utf-8")
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
    finally:
        _local.busy = False


def read_log(limit: int = 200) -> list[dict]:
    """Ultimi tentativi registrati, dal piu' recente."""
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


def clear_log() -> None:
    with _log_lock:
        _last_logged.clear()
        try:
            LOG_FILE.unlink(missing_ok=True)
        except OSError:
            pass


def _deny(event: str, host: Any, port: Any = None) -> None:
    global _blocked_count
    _blocked_count += 1
    _record(event, host, port, allowed=False)
    msg = f"FotoScan: connessione a internet bloccata ({event} -> {_host_str(host)})"
    log.warning(msg)
    raise NetworkBlocked(msg)


def _audit(event: str, args: tuple) -> None:
    if not event.startswith("socket."):
        return
    if event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
        sock, address = args[0], args[1]
        if address is None:  # sendmsg su socket gia' connesso: controllato al connect
            return
        if getattr(sock, "family", None) not in (socket.AF_INET, socket.AF_INET6):
            return       # socket locali (AF_UNIX ecc.)
        host = address[0] if isinstance(address, tuple) and address else address
        port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
        if not _allowed(host):
            _deny(event, host, port)
        elif not is_loopback(host):
            _record(event, host, port, allowed=True)
    elif event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex"):
        host = args[0] if args else None
        port = args[1] if event == "socket.getaddrinfo" and len(args) > 1 else None
        if host is not None and not _allowed(host):
            _deny(event, host, port)
    elif event == "socket.gethostbyaddr":
        host = args[0] if args else None
        if not is_loopback(host):
            _deny(event, host)


def _wrap_getaddrinfo() -> None:
    """Annota gli IP dei server di download risolti durante la finestra (per autorizzarne il connect)."""
    orig = socket.getaddrinfo

    def getaddrinfo(host, *a, **kw):  # type: ignore[no-untyped-def]
        res = orig(host, *a, **kw)
        if _window > 0 and _is_download_host(host):
            for r in res:
                try:
                    _resolved.add(str(r[4][0]).lower().split("%", 1)[0])
                except (IndexError, TypeError):
                    pass
        return res

    getaddrinfo._fotoscan_wrapped = True  # type: ignore[attr-defined]
    if not getattr(orig, "_fotoscan_wrapped", False):
        socket.getaddrinfo = getaddrinfo


def install() -> None:
    """Attiva il blocco (idempotente). Da chiamare prima di importare transformers/huggingface_hub."""
    global _installed
    if _installed:
        return
    _installed = True
    # Niente telemetria e niente statistiche d'uso dalle librerie (ereditato anche dai sottoprocessi).
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"
    os.environ["DISABLE_TELEMETRY"] = "1"
    _wrap_getaddrinfo()
    sys.addaudithook(_audit)


def window_open() -> bool:
    return _window > 0


def status() -> dict:
    """Stato del blocco in questo processo (per Impostazioni > Rete)."""
    return {
        "installed": _installed,
        "window_open": _window > 0,
        "window_reason": _reason,
        "blocked_this_process": _blocked_count,
        "download_hosts": list(DOWNLOAD_HOSTS),
        "telemetry_env": {k: os.environ.get(k) for k in
                          ("HF_HUB_DISABLE_TELEMETRY", "HF_HUB_DISABLE_IMPLICIT_TOKEN", "DO_NOT_TRACK", "DISABLE_TELEMETRY")},
        "log_file": str(LOG_FILE),
    }


def blocked_count() -> int:
    return _blocked_count


@contextmanager
def download_window(reason: str):
    """Apre la rete SOLO verso Hugging Face / GitHub per scaricare un modello mancante."""
    global _window, _reason
    with _lock:
        _window += 1
        _reason = reason
    log.warning("rete aperta solo verso %s per: %s", ", ".join(DOWNLOAD_HOSTS), reason)
    try:
        yield
    finally:
        with _lock:
            _window -= 1
            if _window == 0:
                _resolved.clear()
                _reason = None
        log.warning("rete di nuovo bloccata (%s)", reason)


def load_pretrained(fn: Callable[..., Any], name: str, **kw: Any) -> Any:
    """`fn(name)` di transformers leggendo SOLO la cache locale; se il modello manca, lo scarica
    (unico caso in cui la rete viene aperta, e solo verso Hugging Face)."""
    try:
        return fn(name, local_files_only=True, **kw)
    except (OSError, ValueError) as e:
        log.info("modello %s non completo in cache (%s): download", name, str(e).splitlines()[0][:160] if str(e) else e)
    with download_window(f"download del modello {name}"):
        return fn(name, **kw)


def quiet_onnxruntime(ort: Any) -> None:
    """Disattiva gli eventi di telemetria di onnxruntime (su Windows passano dalla telemetria di sistema)."""
    try:
        ort.disable_telemetry_events()
    except Exception:  # noqa: BLE001 - versioni senza la funzione
        pass
