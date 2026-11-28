"""Test API indicizzazione (fotoscan/server/routes_index.py) con sottoprocesso finto.

Nessun vero `python -m fotoscan index` viene lanciato: Popen e taskkill sono sostituiti.
Le route sono chiamate come funzioni (TestClient richiede httpx, non installato nel progetto).

Esecuzione: `python -m pytest tests\\test_api_index.py` oppure `python tests\\test_api_index.py`.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="fs_index_test_"))
os.environ["FOTOSCAN_DB"] = str(_TMP / "catalogo.db")  # mai il catalogo vero
os.environ["FOTOSCAN_CACHE"] = str(_TMP / "cache")

from fastapi import HTTPException  # noqa: E402

from fotoscan import db as dbm  # noqa: E402
from fotoscan.server import deps, routes_index as ri  # noqa: E402

SOURCE = _TMP / "sorgente"
SOURCE.mkdir(exist_ok=True)
(SOURCE / "sub").mkdir(exist_ok=True)


class FakeProc:
    """Sostituisce subprocess.Popen: resta vivo finche' returncode non viene impostato."""

    pid = 4242

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.returncode = None

    def poll(self):
        return self.returncode


def _fake_taskkill(cmd, **kwargs):
    if cmd[0] == "taskkill":
        for p in FAKE_PROCS:
            p.returncode = 1
    return mock.Mock(returncode=0, stdout="")


FAKE_PROCS: list[FakeProc] = []


def _reset(source_dir: str | None = str(SOURCE)):
    """Stato pulito: catalogo vuoto con file di prova, nessuna run in corso, config di test."""
    ri._current = None
    FAKE_PROCS.clear()
    cache = Path(os.environ["FOTOSCAN_CACHE"])
    cache.mkdir(parents=True, exist_ok=True)
    for f in cache.glob("index.*"):
        f.unlink()
    db_path = Path(os.environ["FOTOSCAN_DB"])
    for ext in ("", "-wal", "-shm"):
        Path(str(db_path) + ext).unlink(missing_ok=True)
    deps.reload_cfg()
    cfg = deps.get_cfg()
    cfg.source_dir = source_dir or ""
    cfg.cache_dir = str(cache)
    # db.SCHEMA crea l'indice ix_plan_folder prima della tabella plan: su catalogo nuovo va pre-creata
    import sqlite3
    pre = sqlite3.connect(str(db_path))
    pre.execute("CREATE TABLE IF NOT EXISTS plan(file_id INTEGER PRIMARY KEY, folder TEXT, context TEXT, "
                "score REAL, is_dup INTEGER DEFAULT 0, dup_of INTEGER)")
    pre.commit()
    pre.close()
    conn = dbm.connect(str(db_path))
    conn.executemany(
        "INSERT INTO files(path,name,kind,status,error,hash) VALUES(?,?,?,?,?,?)",
        [
            ("/s/a.jpg", "a.jpg", "image", "error", "decodifica fallita", "h1"),
            ("/s/b.jpg", "b.jpg", "image", "error", "file corrotto", None),
            ("/s/c.jpg", "c.jpg", "image", "embedded", None, "h3"),
        ],
    )
    conn.commit()
    conn.close()


def _start_fake(body=None):
    def popen(args, **kwargs):
        p = FakeProc(args, **kwargs)
        FAKE_PROCS.append(p)
        return p

    with mock.patch.object(ri.subprocess, "Popen", side_effect=popen), \
            mock.patch.object(ri.subprocess, "run", side_effect=_fake_taskkill):
        return ri.index_start(body)


def _wait_idle(timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if ri._current is None:
            return True
        time.sleep(0.05)
    return False


def _status_code(fn, *a):
    try:
        fn(*a)
    except HTTPException as e:
        return e.status_code, e.detail
    return 200, None


# ------------------------------------------------------------ test
def test_status_idle():
    _reset()
    st = ri.index_status()
    assert st["running"] is False
    assert st["stage"] == ""
    assert st["stats"]["totale"] == 3
    assert st["eta_seconds"] is None and st["rate_per_sec"] is None
    assert isinstance(st["log_tail"], list)


def test_start_stop_and_double_start():
    _reset()
    assert _start_fake({"limit": 5}) == {"started": True}
    args = FAKE_PROCS[0].args
    assert args[1:5] == ["-m", "fotoscan", "index", "--source"] and args[5] == str(SOURCE)
    assert args[-2:] == ["--limit", "5"]
    assert FAKE_PROCS[0].kwargs["cwd"] == str(ROOT)
    assert (Path(os.environ["FOTOSCAN_CACHE"]) / "index.pid").exists()
    assert ri.index_status()["running"] is True
    code, detail = _status_code(_start_fake)
    assert code == 409 and "in corso" in detail
    with mock.patch.object(ri.subprocess, "run", side_effect=_fake_taskkill):
        assert ri.index_stop() == {"stopped": True}
    assert _wait_idle()
    assert ri.index_status()["running"] is False
    assert not (Path(os.environ["FOTOSCAN_CACHE"]) / "index.pid").exists()


def test_progress_rate_and_eta():
    _reset()
    _start_fake()
    run = ri._current
    run.started = time.time() - 60  # progresso ritenuto valido (epoch >= started)
    db = Path(os.environ["FOTOSCAN_DB"])
    conn = dbm.connect(str(db))
    now = time.time()
    dbm.set_meta(conn, "progress", f"metadati|10|100|{now:.0f}")
    conn.close()
    st = ri.index_status()
    assert st["running"] is True and st["stage"] == "metadati"
    assert st["done"] == 10 and st["total"] == 100 and st["percent"] == 10.0
    assert st["rate_per_sec"] is None and st["eta_seconds"] is None  # un solo campione: nessuna velocita'
    FAKE_PROCS[0].returncode = 1
    assert _wait_idle()


def test_rate_moving_average():
    ri._rate_pts.clear()
    ri._rate_stage = None
    assert ri._rate("metadati", 0, 1000.0) is None
    assert abs(ri._rate("metadati", 100, 1010.0) - 10.0) < 1e-6
    # nuova fase: la media riparte da zero
    assert ri._rate("embedding", 5, 1011.0) is None


def test_stale_progress_is_not_reported():
    _reset()
    _start_fake()
    run = ri._current
    conn = dbm.connect(os.environ["FOTOSCAN_DB"])
    dbm.set_meta(conn, "progress", "completato|1|1|1000")  # residuo di una run precedente
    conn.close()
    st = ri._snapshot(run, True)
    assert st["stage"] == "avvio"
    FAKE_PROCS[0].returncode = 0
    assert _wait_idle()


def test_start_errors_in_italian():
    _reset(source_dir=None)
    # nessuna sorgente nel config ne' nel catalogo
    conn = dbm.connect(os.environ["FOTOSCAN_DB"])
    conn.execute("DELETE FROM meta WHERE key='source_dir'")
    conn.commit()
    conn.close()
    code, detail = _status_code(_start_fake)
    assert code == 400 and "sorgente" in detail.lower()
    code, detail = _status_code(_start_fake, {"limit": 0})
    assert code == 400 and "positivo" in detail
    _reset(source_dir=str(_TMP / "non_esiste"))
    code, detail = _status_code(_start_fake)
    assert code == 400 and "non esiste" in detail
    _reset()
    code, detail = _status_code(_start_fake, {"subdir": "..\\fuori"})
    assert code == 400
    code, detail = _status_code(_start_fake, {"subdir": "manca"})
    assert code == 400 and "non esiste" in detail
    assert _start_fake({"subdir": "sub"}) == {"started": True}
    assert FAKE_PROCS[-1].args[-2:] == ["--subdir", "sub"]
    FAKE_PROCS[-1].returncode = 0
    assert _wait_idle()


def test_orphan_detected_from_pid_file():
    _reset()
    cache = Path(os.environ["FOTOSCAN_CACHE"])
    (cache / "index.pid").write_text(f"777\n{time.time():.3f}", encoding="utf-8")
    alive = {"v": True}

    def fake_taskkill(cmd, **kwargs):
        alive["v"] = False  # il processo orfano viene terminato
        return mock.Mock(returncode=0, stdout="")

    with mock.patch.object(ri, "_pid_alive", side_effect=lambda pid: alive["v"]), \
            mock.patch.object(ri, "_pid_is_ours", return_value=True):
        st = ri.index_status()
        assert st["running"] is True
        code, _ = _status_code(_start_fake)
        assert code == 409
        with mock.patch.object(ri.subprocess, "run", side_effect=fake_taskkill) as run_mock:
            assert ri.index_stop() == {"stopped": True}
            assert run_mock.call_args[0][0][:3] == ["taskkill", "/PID", "777"]
        assert _wait_idle()


def test_stale_pid_file_is_removed():
    _reset()
    cache = Path(os.environ["FOTOSCAN_CACHE"])
    (cache / "index.pid").write_text("888\n1", encoding="utf-8")
    with mock.patch.object(ri, "_pid_alive", return_value=False):
        assert ri.index_status()["running"] is False
    assert not (cache / "index.pid").exists()


def test_errors_and_retry():
    _reset()
    errs = ri.index_errors(limit=200)
    assert {e["path"] for e in errs} == {"/s/a.jpg", "/s/b.jpg"}
    assert ri.index_retry_errors() == {"retried": 2}
    conn = dbm.connect(os.environ["FOTOSCAN_DB"])
    st = dict(conn.execute("SELECT path, status FROM files").fetchall())
    conn.close()
    assert st["/s/a.jpg"] == "meta"  # ha l'hash: riparte dai metadati
    assert st["/s/b.jpg"] == "new"  # senza hash: riparte da capo
    assert ri.index_errors() == []


def test_retry_refused_while_running():
    _reset()
    _start_fake()
    code, _ = _status_code(ri.index_retry_errors)
    assert code == 409
    FAKE_PROCS[0].returncode = 0
    assert _wait_idle()


def test_log_tail_filters_noise():
    _reset()
    cache = Path(os.environ["FOTOSCAN_CACHE"])
    (cache / "index.log").write_text(
        "--- avvio ---\n"
        "[scansione] 10/100\r  5%|##   | 5/100 [00:01<00:02, 5.00it/s]\r\n"
        "\n"
        "UserWarning: qualcosa\n"
        "[metadati] 50/100\n"
        "\x1b[32mindicizzazione completata\x1b[0m\n",
        encoding="utf-8",
    )
    tail = ri._log_tail(str(cache))
    assert tail == ["--- avvio ---", "[scansione] 10/100", "[metadati] 50/100", "indicizzazione completata"]


def test_zero_size_phase_is_declared_without_percent():
    """Fase senza righe (es. metadati 0/0 su una ri-esecuzione): stato 'metadati', niente percentuale ne' ETA."""
    _reset()
    _start_fake()
    run = ri._current
    run.started = time.time() - 60
    conn = dbm.connect(os.environ["FOTOSCAN_DB"])
    dbm.set_meta(conn, "progress", f"metadati|0|0|{time.time():.0f}")
    conn.close()
    st = ri.index_status()
    assert st["running"] is True and st["stage"] == "metadati"
    assert st["percent"] is None and st["eta_seconds"] is None
    FAKE_PROCS[0].returncode = 0
    assert _wait_idle()


def test_pid_riusato_da_un_altro_processo_non_e_seguito():
    _reset()
    cache = Path(os.environ["FOTOSCAN_CACHE"])
    (cache / "index.pid").write_text(f"999\n{time.time():.3f}", encoding="utf-8")
    with mock.patch.object(ri, "_pid_alive", return_value=True), \
            mock.patch.object(ri, "_proc_start_time", return_value=time.time() - 86400):  # avviato ieri: non e' il nostro
        assert ri.index_status()["running"] is False
    assert not (cache / "index.pid").exists()
    # stesso pid ma avviato subito dopo `started`: e' il nostro
    started = time.time()
    with mock.patch.object(ri, "_proc_start_time", return_value=started + 1.5):
        assert ri._pid_is_ours(1, started) is True
    with mock.patch.object(ri, "_proc_start_time", return_value=None):  # non determinabile: resta il controllo di esistenza
        assert ri._pid_is_ours(1, started) is True


def test_start_rifiutato_se_modello_diverso_dagli_embedding():
    _reset()
    conn = dbm.connect(os.environ["FOTOSCAN_DB"])
    conn.execute("INSERT INTO embeddings(file_id, vec) VALUES(3, x'00000000')")
    dbm.set_meta(conn, "embedding_model", "google/siglip2-so400m-patch14-384")
    conn.commit()
    conn.close()
    deps.get_cfg().model_name = "google/siglip2-base-patch16-224"
    code, detail = _status_code(_start_fake)
    assert code == 409 and "so400m" in detail and not FAKE_PROCS
    deps.get_cfg().model_name = "google/siglip2-so400m-patch14-384"
    assert _start_fake() == {"started": True}
    FAKE_PROCS[0].returncode = 0
    assert _wait_idle()
    deps.get_cfg().model_name = "google/siglip2-base-patch16-224"


def test_start_rifiutato_con_job_volti_in_corso():
    _reset()
    with mock.patch.object(ri, "_face_job_running", return_value=object()):
        code, detail = _status_code(_start_fake)
    assert code == 409 and "volti" in detail and not FAKE_PROCS


def test_sottocartella_annidata_con_entrambi_i_separatori():
    _reset()
    (SOURCE / "sub" / "inner").mkdir(parents=True, exist_ok=True)
    for raw in ("sub/inner", "sub\\inner"):
        assert _start_fake({"subdir": raw}) == {"started": True}
        assert FAKE_PROCS[-1].args[-2:] == ["--subdir", os.sep.join(["sub", "inner"])]
        FAKE_PROCS[-1].returncode = 0
        assert _wait_idle()
    for bad in ("/etc", "\\x", "C:\\x", "sub/../..", "  "):
        code, _ = _status_code(_start_fake, {"subdir": bad})
        assert code == 400, bad


def test_stop_senza_taskkill_su_posix_usa_i_segnali():
    _reset()
    _start_fake()
    sent = []

    def fake_kill(pid, force):
        sent.append((pid, force))
        FAKE_PROCS[0].returncode = -15

    with mock.patch.object(ri, "_kill_tree", side_effect=fake_kill):
        assert ri.index_stop() == {"stopped": True}
    assert sent and sent[0][0] == FAKE_PROCS[0].pid
    assert _wait_idle()


def test_log_ruotato_se_troppo_grande():
    _reset()
    cache = Path(os.environ["FOTOSCAN_CACHE"])
    lf = cache / "index.log"
    lf.write_bytes(b"x" * (ri.LOG_MAX_BYTES + 10))
    ri._rotate_log(str(cache))
    assert not lf.exists() and (cache / "index.log.1").stat().st_size > ri.LOG_MAX_BYTES
    (cache / "index.log.1").unlink()


def test_retry_errors_e_reset_sotto_lock_rifiutati_con_indicizzazione_attiva():
    _reset()
    _start_fake()
    code, _ = _status_code(ri.index_reset_embeddings, {"confirm": True})
    assert code == 409
    FAKE_PROCS[0].returncode = 0
    assert _wait_idle()
    assert ri.index_reset_embeddings({"confirm": True})["reset"] == 1  # c.jpg era 'embedded'


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} test superati")
