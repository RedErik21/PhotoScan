"""Test API /api/settings (validazione per campo, predefiniti, ripristino etichette) e /system/pick-folder.

Config.json e catalogo sono in una cartella temporanea (FOTOSCAN_CONFIG/FOTOSCAN_DB impostati prima
dell'import): il config.json reale del progetto non viene mai toccato.
Esecuzione: `.\\.venv\\Scripts\\python.exe tests\\test_api_status_settings.py` (oppure pytest).
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="settings_test_"))
_SRC = _TMP / "sorgente"
_OUT = _TMP / "destinazione"
_SRC.mkdir()
_OUT.mkdir()
os.environ["FOTOSCAN_CONFIG"] = str(_TMP / "config.json")
os.environ["FOTOSCAN_DB"] = str(_TMP / "catalogo.db")
os.environ["FOTOSCAN_CACHE"] = str(_TMP / "cache")
(_TMP / "config.json").write_text(json.dumps({"source_dir": str(_SRC), "output_dir": str(_OUT)}), encoding="utf-8")

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from fotoscan.server import routes_status  # noqa: E402
from fotoscan.server.deps import get_cfg  # noqa: E402
from fotoscan import config as config_mod  # noqa: E402


# ------------------------------------------------------------ client HTTP minimale
def _request(base: str, method: str, path: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def server():
    app = FastAPI()
    app.include_router(routes_status.router)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/settings/defaults", timeout=1)
            break
        except Exception:
            time.sleep(0.05)
    try:
        yield base
    finally:
        srv.should_exit = True
        th.join(timeout=5)


def _saved() -> dict:
    return json.loads((_TMP / "config.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------ test
def test_defaults_match_config_class():
    with server() as base:
        code, data = _request(base, "GET", "/api/settings/defaults")
    assert code == 200
    fresh = config_mod.Config()
    assert data["score_threshold"] == fresh.score_threshold
    assert data["near_dup_threshold"] == fresh.near_dup_threshold
    assert data["folder_template"] == fresh.folder_template
    assert data["labels"] == fresh.labels and len(data["labels"]) > 0
    assert "db_path" not in data  # solo le impostazioni editabili


def test_valid_partial_update_is_persisted():
    with server() as base:
        code, data = _request(base, "PUT", "/api/settings", {"score_threshold": 0.55, "near_dup_seconds": 300})
        assert code == 200, data
        assert data["score_threshold"] == 0.55 and data["near_dup_seconds"] == 300.0
    saved = _saved()
    assert saved["score_threshold"] == 0.55 and saved["near_dup_seconds"] == 300.0
    assert get_cfg().score_threshold == 0.55  # ricaricata in memoria dopo il salvataggio


def test_out_of_range_error_is_per_field_and_italian():
    with server() as base:
        code, data = _request(base, "PUT", "/api/settings", {"score_threshold": 1.5})
    assert code == 400
    assert "score_threshold" in data["fields"]
    assert "tra 0 e 1" in data["fields"]["score_threshold"]
    assert data["detail"] == " ".join(data["fields"].values())


def test_several_errors_are_returned_together():
    with server() as base:
        code, data = _request(base, "PUT", "/api/settings", {
            "score_threshold": 9, "batch_size": 0, "device": "tpu", "event_gap_hours": 500,
        })
    assert code == 400
    assert set(data["fields"]) == {"score_threshold", "batch_size", "device", "event_gap_hours"}


def test_output_inside_source_is_rejected_on_output_field():
    inside = _SRC / "sotto"
    inside.mkdir(exist_ok=True)
    before = _saved()
    with server() as base:
        code, data = _request(base, "PUT", "/api/settings", {"output_dir": str(inside)})
    assert code == 400
    assert "output_dir" in data["fields"]
    assert _saved() == before  # nulla viene scritto se la validazione fallisce


def test_source_that_does_not_exist_is_rejected():
    with server() as base:
        code, data = _request(base, "PUT", "/api/settings", {"source_dir": str(_TMP / "non-esiste")})
    assert code == 400 and "source_dir" in data["fields"]


def test_output_that_does_not_exist_is_accepted_if_parent_exists():
    # La destinazione viene creata all'applicazione (o da POST /apply/destination): il genitore basta.
    target = _TMP / "destinazione-nuova"
    assert not target.exists()
    with server() as base:
        code, data = _request(base, "PUT", "/api/settings", {"output_dir": str(target)})
    assert code == 200, data
    assert _saved()["output_dir"] == str(target)
    assert not target.exists()  # PUT /settings non crea nulla



def test_folder_template_rules():
    ok = ["{anno}/{mese:02d} - {contesto}", "{anno}", "{anno}/{contesto}/{mese:02d}"]
    bad = {
        "{anno}/{mese}": "mese senza formato",
        "{anno}/../x": "risalita",
        "/{anno}/x": "percorso assoluto",
        "C:/{anno}": "unita'",
        "{anno}:x": "carattere non ammesso",
        "{contesto}": "manca anno",
        "{anno}/{nome}": "token sconosciuto",
        "{anno}/{mese:03d}": "formato non ammesso",
        "{anno}/{contesto!r}": "conversione",
        "   ": "vuoto",
    }
    with server() as base:
        for t in ok:
            code, data = _request(base, "PUT", "/api/settings", {"folder_template": t})
            assert code == 200, (t, data)
        for t, why in bad.items():
            code, data = _request(base, "PUT", "/api/settings", {"folder_template": t})
            assert code == 400 and "folder_template" in data["fields"], (t, why, data)


def test_reset_labels_restores_defaults():
    with server() as base:
        _request(base, "PUT", "/api/settings", {"labels": [{"name": "Solo", "prompt": "una cosa"}]})
        code, data = _request(base, "POST", "/api/settings/reset-labels", {})
    assert code == 200
    defaults = config_mod.Config().labels
    assert data["labels"] == defaults


def test_pick_folder_reports_when_dialog_unavailable():
    original = routes_status._pick_folder_blocking
    routes_status._pick_folder_blocking = lambda title, initial: (False, None)
    try:
        with server() as base:
            code, data = _request(base, "POST", "/api/system/pick-folder", {"title": "x"})
    finally:
        routes_status._pick_folder_blocking = original
    assert code == 200 and data == {"path": None, "available": False}


def test_catalog_bytes_helper():
    missing = str(_TMP / "nessuno.db")
    assert routes_status._catalog_bytes(missing) is None
    (_TMP / "piccolo.db").write_bytes(b"x" * 123)
    assert routes_status._catalog_bytes(str(_TMP / "piccolo.db")) == 123


def test_status_plan_missing_counts_indexed_files_not_in_plan():
    from fotoscan import db as dbm
    conn = dbm.connect(str(_TMP / "catalogo.db"))
    try:
        conn.execute("DELETE FROM plan")
        conn.execute("DELETE FROM files")
        for i, st in enumerate(["embedded", "embedded", "embedded", "meta"]):
            conn.execute("INSERT INTO files(path,name,kind,status) VALUES(?,?,?,?)",
                         (str(_TMP / f"f{i}.jpg"), f"f{i}.jpg", "image", st))
        conn.commit()
        ids = [r[0] for r in conn.execute("SELECT id FROM files WHERE status='embedded' ORDER BY id")]
        with server() as base:
            # piano non costruito: nessuna foto "mancante"
            code, data = _request(base, "GET", "/api/status")
            assert code == 200 and data["plan"]["built"] is False and data["plan"]["missing"] == 0
            conn.execute("INSERT INTO plan(file_id, folder) VALUES(?, ?)", (ids[0], "2020/01 - X"))
            conn.commit()
            code, data = _request(base, "GET", "/api/status")
        assert code == 200 and data["plan"]["built"] is True
        assert data["plan"]["missing"] == 2  # due indicizzate senza riga nel piano; la 'meta' non conta
    finally:
        conn.close()


def test_disk_reports_missing_output_and_files():
    missing = _TMP / "cartella-da-creare"
    the_file = _TMP / "un-file.txt"
    the_file.write_text("x", encoding="utf-8")
    with server() as base:
        code1, d1 = _request(base, "GET", "/api/system/disk?path=" + urllib.parse.quote(str(missing)))
        code2, d2 = _request(base, "GET", "/api/system/disk?path=" + urllib.parse.quote(str(the_file)))
    assert code1 == 200 and d1["exists"] is False and d1["is_file"] is False and d1["free_bytes"] is not None
    assert code2 == 200 and d2["exists"] is False and d2["is_file"] is True


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} test superati")
