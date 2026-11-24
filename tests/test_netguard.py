"""Test del blocco di rete (fotoscan/netguard.py): nessuna connessione esterna, solo 127.0.0.1."""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="netguard_test_"))
os.environ["FOTOSCAN_NETLOG"] = str(_TMP / "rete.jsonl")      # registro isolato (mai quello vero)
os.environ["FOTOSCAN_CONFIG"] = str(_TMP / "config.json")
os.environ["FOTOSCAN_DB"] = str(_TMP / "catalogo.db")
os.environ["FOTOSCAN_CACHE"] = str(_TMP / "cache")
(_TMP / "config.json").write_text(json.dumps({"source_dir": str(_TMP)}), encoding="utf-8")

import fotoscan  # noqa: E402,F401  (installa il blocco)
from fotoscan import netguard  # noqa: E402


def _expect_blocked(fn, what: str) -> None:
    try:
        fn()
    except netguard.NetworkBlocked:
        return
    except urllib.error.URLError as e:  # urllib incapsula l'errore del socket
        if isinstance(e.reason, netguard.NetworkBlocked):
            return
        raise
    raise AssertionError(f"non bloccato: {what}")


def test_external_blocked() -> None:
    _expect_blocked(lambda: socket.getaddrinfo("example.com", 443), "DNS example.com")
    _expect_blocked(lambda: socket.gethostbyname("google.com"), "gethostbyname")
    _expect_blocked(lambda: socket.create_connection(("8.8.8.8", 53), timeout=2), "connect 8.8.8.8")
    _expect_blocked(lambda: socket.create_connection(("2001:4860:4860::8888", 53), timeout=2), "connect IPv6")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        _expect_blocked(lambda: s.sendto(b"x", ("1.1.1.1", 53)), "UDP sendto")
    finally:
        s.close()
    _expect_blocked(lambda: urllib.request.urlopen("https://huggingface.co", timeout=3), "HTTPS fuori finestra")
    print("ok external_blocked")


def test_loopback_allowed() -> None:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    t = threading.Thread(target=lambda: srv.accept()[0].close(), daemon=True)
    t.start()
    with socket.create_connection(("localhost", port), timeout=3):
        pass
    t.join(3)
    srv.close()
    print("ok loopback_allowed")


def test_window_only_download_hosts() -> None:
    with netguard.download_window("test"):
        assert netguard.window_open()
        _expect_blocked(lambda: socket.getaddrinfo("example.com", 443), "DNS non consentito in finestra")
        _expect_blocked(lambda: socket.create_connection(("8.8.8.8", 53), timeout=2), "IP qualsiasi in finestra")
        assert netguard._allowed("huggingface.co") and netguard._allowed("cdn-lfs.hf.co")
        assert netguard._allowed("release-assets.githubusercontent.com")
        assert not netguard._allowed("huggingface.co.evil.com")
    assert not netguard.window_open()
    assert not netguard._allowed("huggingface.co")
    print("ok window_only_download_hosts")


def test_load_pretrained_local_first() -> None:
    calls = []

    def fake(name, **kw):
        calls.append((kw.get("local_files_only"), netguard.window_open()))
        if kw.get("local_files_only"):
            raise OSError("non in cache")
        return "scaricato"

    assert netguard.load_pretrained(fake, "x/y") == "scaricato"
    assert calls == [(True, False), (None, True)], calls
    calls.clear()
    assert netguard.load_pretrained(lambda n, **kw: calls.append(kw) or "cache", "x/y") == "cache"
    assert calls == [{"local_files_only": True}]
    print("ok load_pretrained_local_first")


def _probe_from_fotoscan_embedder() -> None:
    # simula un tentativo partito da codice di PhotoScan (il registro deve dire "per cosa")
    src = "import socket\ndef go():\n    socket.getaddrinfo('huggingface.co', 443)\n"
    code = compile(src, "embedder.py", "exec")
    ns = {"__name__": "fotoscan.embedder"}
    exec(code, ns)
    ns["go"]()


def test_log_records_blocked_with_purpose() -> None:
    netguard.clear_log()
    _expect_blocked(_probe_from_fotoscan_embedder, "probe embedder")
    rows = netguard.read_log()
    assert rows, "registro vuoto"
    r = rows[0]
    assert r["outcome"] == "bloccato" and r["host"] == "huggingface.co" and r["port"] == 443, r
    assert r["purpose"] == "Caricamento del modello di riconoscimento", r
    assert r["caller"].startswith("fotoscan.embedder:"), r
    # raffica dello stesso tentativo: una riga sola
    _expect_blocked(_probe_from_fotoscan_embedder, "probe embedder 2")
    assert len(netguard.read_log()) == 1
    print("ok log_records_blocked_with_purpose")


def test_api_network() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from fotoscan.server import routes_network

    app = FastAPI()
    app.include_router(routes_network.router)
    c = TestClient(app)
    netguard.clear_log()
    r = c.post("/api/network/test").json()
    assert r["blocked"] is True, r
    d = c.get("/api/network").json()
    assert d["guard"]["installed"] is True and d["guard"]["telemetry_env"]["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert d["summary"]["blocked"] >= 1 and d["events"][0]["host"] == "example.com"
    assert d["events"][0]["purpose"] == "Prova del blocco dalle Impostazioni", d["events"][0]
    assert {x["id"] for x in d["expected"]} == {"huggingface", "github", "github-updates", "osm", "microsoft"}
    assert c.delete("/api/network/log").json()["ok"] is True
    assert c.get("/api/network").json()["events"] == []
    print("ok api_network")


if __name__ == "__main__":
    test_external_blocked()
    test_loopback_allowed()
    test_window_only_download_hosts()
    test_load_pretrained_local_first()
    test_log_records_blocked_with_purpose()
    test_api_network()
    print("TUTTI OK")
