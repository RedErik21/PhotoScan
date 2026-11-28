"""Test API /apply (anteprima, destinazione, esecuzione con conferma, progresso, annullamento, log).

Mai sul catalogo vero: ogni test usa catalogo, sorgente e destinazione temporanei. La configurazione
(config.json) non viene mai scritta: i salvataggi di routes_status sono reindirizzati in memoria.
Il server e' un FastAPI minimale con il solo router di apply, su una porta libera (niente TestClient).

Esecuzione: `.\\.venv\\Scripts\\python.exe tests\\test_api_apply.py` (oppure pytest, se installato).
"""
from __future__ import annotations

import collections
import contextlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from fotoscan import db as dbm  # noqa: E402
from fotoscan.config import Config  # noqa: E402
from fotoscan.server import deps, jobs, routes_apply, routes_status  # noqa: E402

FOLDER = "2020/06 - Test"


# ------------------------------------------------------------ utilita'
@contextlib.contextmanager
def patched(obj, name: str, value):
    """Sostituisce un attributo per la durata del blocco (funziona anche senza pytest)."""
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _files(folder: Path) -> list[Path]:
    """File prodotti nella destinazione (esclusi il registro e i temporanei)."""
    return [p for p in folder.rglob("*") if p.is_file() and p.name != routes_apply.LOG_NAME]


# ------------------------------------------------------------ fixture
class Env:
    """Catalogo + sorgente + destinazione temporanei, server di test su porta libera."""

    def __init__(self, n_good: int = 3, n_missing: int = 0, link_mode: str = "hardlink"):
        self.tmp = Path(tempfile.mkdtemp(prefix="apply_test_"))
        self.src = self.tmp / "src"
        self.out = self.tmp / "out"
        self.src.mkdir()
        self.db_path = str(self.tmp / "catalogo.db")
        self.cfg = Config(source_dir=str(self.src), output_dir=str(self.out), db_path=self.db_path,
                          cache_dir=str(self.tmp / "cache"), link_mode=link_mode)
        # db.SCHEMA crea l'indice su plan prima della tabella: su un catalogo nuovo serve la tabella gia' presente
        pre = sqlite3.connect(self.db_path)
        pre.execute("CREATE TABLE plan(file_id INTEGER PRIMARY KEY, folder TEXT, context TEXT, score REAL, "
                    "is_dup INTEGER DEFAULT 0, dup_of INTEGER)")
        pre.commit()
        pre.close()
        conn = dbm.connect(self.db_path)
        for i in range(1, n_good + 1):
            p = self.src / f"IMG_{i:04d}.jpg"
            p.write_bytes(f"foto {i}".encode() * 10)
            self._add(conn, i, p, p.stat().st_size)
        for j in range(n_missing):
            fid = n_good + 1 + j
            self._add(conn, fid, self.src / f"mancante_{fid}.jpg", 100)  # file non esistente su disco
        conn.commit()
        conn.close()
        # la configurazione effettiva dell'app e' sostituita: niente config.json reale
        self._orig_get_cfg = deps.get_cfg
        deps.get_cfg = lambda: self.cfg
        self._orig_rs = (routes_status.get_cfg, routes_status._write_config)
        routes_status.get_cfg = lambda: self.cfg
        routes_status._write_config = self._save_cfg
        self.port = _free_port()
        app = FastAPI()
        app.include_router(routes_apply.router)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)

    def _save_cfg(self, new: Config) -> None:
        """Sostituisce il salvataggio su config.json: tiene la configurazione solo in memoria."""
        self.cfg = new

    def _add(self, conn, fid: int, path: Path, size: int) -> None:
        conn.execute(
            "INSERT INTO files(id,path,name,kind,size,status,date_taken,date_source) "
            "VALUES(?,?,?,?,?,'embedded','2020-06-09T10:00:00','exif')",
            (fid, str(path), path.name, "image", size),
        )
        conn.execute("INSERT INTO plan(file_id,folder,context,score,is_dup,dup_of) VALUES(?,?,?,?,0,NULL)",
                     (fid, FOLDER, "Mare", 0.9))

    def db(self):
        return dbm.connect(self.db_path)

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
        deps.get_cfg = self._orig_get_cfg
        routes_status.get_cfg, routes_status._write_config = self._orig_rs
        shutil.rmtree(self.tmp, ignore_errors=True)


def call(env: Env, method: str, path: str, body=None):
    """Ritorna (status, json). Non solleva per gli errori HTTP."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{env.port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "null")


def wait_job(env: Env, job_id: str, timeout: float = 30.0) -> dict:
    """Attende il job (stesso processo: lo stato si legge da jobs.get_job, la route /api/jobs e' in app.py)."""
    end = time.time() + timeout
    while time.time() < end:
        j = jobs.get_job(job_id).public()
        if j["state"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("job non terminato in tempo")


class FakeJob:
    """Job finto per _run_job: conta gli aggiornamenti e puo' chiedere l'annullamento a un certo punto."""

    def __init__(self, cancel_when=None):
        self.id = "test-" + os.urandom(3).hex()
        self.updates = 0
        self._cancel_when = cancel_when

    @property
    def cancel_requested(self) -> bool:
        return bool(self._cancel_when and self._cancel_when())

    def update(self, **kwargs) -> None:
        self.updates += 1


# ------------------------------------------------------------ test: anteprima
def _make_env() -> Env:
    return Env(n_good=3, n_missing=1)


def test_preview_hardlink_same_volume(env: Env) -> None:
    st, p = call(env, "GET", "/api/apply/preview")
    assert st == 200 and p["ok"] is True, p
    assert p["total"] == 4
    # anteprima senza stat dei sorgenti: il file mancante conta come link (si scopre all'esecuzione)
    assert p["link"] == 4 and p["copies"] == 0
    assert p["existing"] == 0
    assert p["extra_bytes"] == 0 and p["same_volume"] is True
    assert p["folders"] == 1 and p["duplicates_to_folder"] == 0
    assert any("Collegamento" in w for w in p["warnings"]), p["warnings"]
    assert isinstance(p.get("fingerprint"), str) and len(p["fingerprint"]) == 32, p.get("fingerprint")
    # l'anteprima non scrive nulla
    assert not env.out.exists()

    # cache invalidata dalla modifica del piano
    conn = env.db()
    conn.execute("UPDATE plan SET folder='2021/01 - Altro' WHERE file_id=1")
    conn.commit()
    conn.close()
    _, p2 = call(env, "GET", "/api/apply/preview")
    assert p2["folders"] == 2, p2


def test_preview_copy_mode_reports_bytes(env: Env) -> None:
    env.cfg.link_mode = "copy"
    _, p = call(env, "GET", "/api/apply/preview")
    assert p["ok"] is True
    assert p["link"] == 0 and p["copies"] == 4
    # 3 file reali + la riga del file mancante (100 byte nel catalogo), tutte da copiare
    assert p["extra_bytes"] == sum(f.stat().st_size for f in env.src.iterdir()) + 100, p["extra_bytes"]
    assert any("copia" in w.lower() for w in p["warnings"]), p["warnings"]


def test_preview_low_space_warns(env: Env) -> None:
    env.cfg.link_mode = "copy"
    Usage = collections.namedtuple("Usage", "total used free")
    with patched(shutil, "disk_usage", lambda _p: Usage(100, 90, 10)):  # 10 byte liberi
        _, p = call(env, "GET", "/api/apply/preview")
    assert p["ok"] is True
    assert any("Spazio insufficiente" in w for w in p["warnings"]), p["warnings"]


def test_preview_other_volume_uses_copy(env: Env) -> None:
    """Destinazione su un altro disco: niente hard link, avviso con lo spazio necessario (in unita' 1024)."""
    real_stat = os.stat
    other = str(env.tmp)  # e' l'antenato esistente della destinazione (non ancora creata): finge un altro volume

    def fake_stat(p, *a, **k):
        st = real_stat(p, *a, **k)
        if str(p) == other:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, st.st_nlink, st.st_uid, st.st_gid,
                                   st.st_size, int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))
        return st

    env.cfg.output_dir = str(env.tmp / "altro_disco" / "out")
    with patched(os, "stat", fake_stat):
        _, p = call(env, "GET", "/api/apply/preview")
    assert p["ok"] is True and p["same_volume"] is False, p
    assert p["link"] == 0 and p["copies"] == 4 and p["extra_bytes"] > 0, p
    assert any("dischi diversi" in w and "copia" in w for w in p["warnings"]), p["warnings"]


def test_fmt_bytes_matches_frontend_units(env: Env) -> None:
    assert routes_apply._fmt_bytes(1024 ** 3 * 60.2) == "60,2 GB"
    assert routes_apply._fmt_bytes(5 * 1024 ** 2) == "5,0 MB"
    assert routes_apply._fmt_bytes(12) == "12 B"


def test_source_from_catalog_when_not_in_config(env: Env) -> None:
    """Se la sorgente non e' in config.json, si usa quella del catalogo (come GET /settings)."""
    env.cfg.source_dir = ""
    conn = env.db()
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('source_dir', ?)", (str(env.src),))
    conn.commit()
    conn.close()
    _, p = call(env, "GET", "/api/apply/preview")
    assert p["ok"] is True and p["total"] == 4, p


# ------------------------------------------------------------ test: destinazione
def test_suggest_is_same_volume_and_creates_nothing(env: Env) -> None:
    st, s = call(env, "GET", "/api/apply/suggest")
    assert st == 200 and s["same_volume"] is True, s
    assert Path(s["path"]).anchor == Path(os.path.abspath(env.src)).anchor, s
    assert not str(s["path"]).lower().startswith(str(env.src).lower())
    assert not Path(s["path"]).exists()  # proporre non significa creare


def test_destination_creates_empty_folder_and_saves(env: Env) -> None:
    dest = env.tmp / "nuova" / "Foto"
    st, r = call(env, "POST", "/api/apply/destination", {"path": str(dest)})
    assert st == 200 and r["output_dir"] == str(dest), (st, r)
    assert dest.is_dir() and not any(dest.iterdir())  # creata vuota, nessun file scritto
    _, p = call(env, "GET", "/api/apply/preview")
    assert p["output_dir"] == str(dest), p


def test_destination_rejects_bad_paths(env: Env) -> None:
    for bad in (str(env.src), str(env.src / "sotto"), str(env.src.parent)):
        st, r = call(env, "POST", "/api/apply/destination", {"path": bad})
        assert st == 400 and "sorgente" in r["detail"], (bad, st, r)
    assert not (env.src / "sotto").exists()
    st, r = call(env, "POST", "/api/apply/destination", {"path": ""})
    assert st == 400, (st, r)
    (env.tmp / "file.txt").write_text("x", encoding="utf-8")
    st, r = call(env, "POST", "/api/apply/destination", {"path": str(env.tmp / "file.txt" / "dentro")})
    assert st == 400, (st, r)


def test_destination_not_writable(env: Env) -> None:
    dest = env.tmp / "ro"
    dest.mkdir()
    orig = Path.write_bytes

    def deny(self, data):  # simula una cartella in sola lettura: solo il file di prova fallisce
        if self.name == ".fotoscan-scrittura-test":
            raise PermissionError(13, "Accesso negato")
        return orig(self, data)

    with patched(Path, "write_bytes", deny):
        st, r = call(env, "POST", "/api/apply/destination", {"path": str(dest)})
    assert st == 400 and "scrivibile" in r["detail"], (st, r)


# ------------------------------------------------------------ test: esecuzione
def test_output_inside_source_rejected(env: Env) -> None:
    env.cfg.output_dir = str(env.src / "sotto")
    _, p = call(env, "GET", "/api/apply/preview")
    assert p["ok"] is False and "sorgente" in p["error"], p
    st, r = call(env, "POST", "/api/apply/run", {"confirm": True})
    assert st == 400, (st, r)
    assert not (env.src / "sotto").exists()


def test_run_requires_explicit_confirm(env: Env) -> None:
    for body in (None, {}, {"confirm": False}, {"confirm": "yes"}, {"confirm": 1}):
        st, r = call(env, "POST", "/api/apply/run", body)
        assert st == 400, (body, st, r)
    assert not env.out.exists()  # nessuna scrittura senza conferma


def test_run_then_rerun_does_not_duplicate(env: Env) -> None:
    st, r = call(env, "POST", "/api/apply/run", {"confirm": True})
    assert st == 200 and "job_id" in r, (st, r)
    job = wait_job(env, r["job_id"])
    assert job["state"] == "done", job
    res = job["result"]
    assert res["link"] == 3 and res["errori"] == 1, res  # l'errore del file mancante non ferma il job
    dest_dir = env.out / FOLDER
    assert sorted(p.name for p in dest_dir.iterdir()) == ["IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg"]
    assert (dest_dir / "IMG_0001.jpg").read_bytes() == (env.src / "IMG_0001.jpg").read_bytes()
    assert os.stat(dest_dir / "IMG_0001.jpg").st_nlink == 2  # hard link: stesso file della sorgente

    _, p = call(env, "GET", "/api/apply/preview")
    assert p["existing"] == 3, p  # gia' presenti: l'anteprima lo riflette

    st, r2 = call(env, "POST", "/api/apply/run", {"confirm": True})
    job2 = wait_job(env, r2["job_id"])
    assert job2["state"] == "done" and job2["result"]["link"] == 0 and job2["result"]["gia_presenti"] == 3, job2
    assert sorted(p.name for p in dest_dir.iterdir()) == ["IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg"]


def test_copy_mode_creates_independent_files(env: Env) -> None:
    env.cfg.link_mode = "copy"
    st, r = call(env, "POST", "/api/apply/run", {"confirm": True})
    job = wait_job(env, r["job_id"])
    assert job["result"]["copie"] == 3 and job["result"]["link"] == 0, job["result"]
    f = env.out / FOLDER / "IMG_0001.jpg"
    assert os.stat(f).st_nlink == 1 and f.read_bytes() == (env.src / "IMG_0001.jpg").read_bytes()


def test_name_collision_and_special_names(env: Env) -> None:
    """Stesso nome nella stessa cartella: il secondo viene rinominato, nomi con accenti e spazi restano."""
    names = ["Città è bella.jpg", "Città è bella.jpg", "foto & amici #1.jpg"]
    conn = env.db()
    for i, name in enumerate(names, start=10):
        p = env.src / f"sorgente_{i}" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(f"contenuto diverso {i}".encode() * 5)
        conn.execute("INSERT INTO files(id,path,name,kind,size,status,date_taken,date_source) "
                     "VALUES(?,?,?,?,?,'embedded','2020-06-09T10:00:00','exif')",
                     (i, str(p), name, "image", p.stat().st_size))
        conn.execute("INSERT INTO plan(file_id,folder,context,score,is_dup,dup_of) VALUES(?,?,?,?,0,NULL)",
                     (i, FOLDER, "Mare", 0.9))
    conn.commit()
    conn.close()
    st, r = call(env, "POST", "/api/apply/run", {"confirm": True})
    job = wait_job(env, r["job_id"])
    assert job["result"]["errori"] == 1 and job["result"]["link"] == 3 + 3, job["result"]  # 3 vecchi + 3 nuovi
    dest_dir = env.out / FOLDER
    names_out = {p.name for p in dest_dir.iterdir()}
    assert "Città è bella.jpg" in names_out and "foto & amici #1.jpg" in names_out, names_out
    assert any(n.startswith("Città è bella_") and n.endswith(".jpg") for n in names_out), names_out


def test_live_counters_and_log(env: Env) -> None:
    st, r = call(env, "POST", "/api/apply/run", {"confirm": True})
    job = wait_job(env, r["job_id"])
    _, pr = call(env, "GET", f"/api/apply/progress?job_id={r['job_id']}")
    assert pr["counts"]["link"] == job["result"]["link"] == 3 and pr["counts"]["errori"] == 1, pr
    _, lg = call(env, "GET", "/api/apply/log?tail=5")
    assert lg["counts"]["link"] == 3, lg["counts"]
    _, other = call(env, "GET", "/api/apply/progress?job_id=altro")
    assert other["counts"] == {}, other


def test_log_is_readable_and_tail_works(env: Env) -> None:
    st, r = call(env, "POST", "/api/apply/run", {"confirm": True})
    wait_job(env, r["job_id"])
    _, log = call(env, "GET", "/api/apply/log?tail=200")
    assert log["exists"] is True
    text = "\n".join(log["lines"])
    assert "apply" in text and "hardlink" in text and "ERRORE" in text, text
    assert log["lines"][-1].startswith("=== fine:"), log["lines"][-1]
    _, short = call(env, "GET", "/api/apply/log?tail=2")
    assert len(short["lines"]) == 2


def test_log_missing_when_never_run(env: Env) -> None:
    _, log = call(env, "GET", "/api/apply/log")
    assert log["exists"] is False and log["lines"] == []


def test_second_run_while_running_is_409(env: Env) -> None:
    # simula un apply gia' in corso (il lock e' quello usato dal job)
    assert routes_apply._run_lock.acquire(blocking=False)
    try:
        st, r = call(env, "POST", "/api/apply/run", {"confirm": True})
        assert st == 409, (st, r)
        st, r = call(env, "POST", "/api/apply/destination", {"path": str(env.tmp / "altra")})
        assert st == 409, (st, r)  # non si cambia destinazione a job in corso
    finally:
        routes_apply._run_lock.release()


def test_run_rejects_plan_changed_after_preview(env: Env) -> None:
    """L'utente conferma l'anteprima; se il piano cambia prima del click, nulla viene creato (409)."""
    _, p = call(env, "GET", "/api/apply/preview")
    fp = p["fingerprint"]
    conn = env.db()
    conn.execute("UPDATE plan SET folder='2021/01 - Altro' WHERE file_id=1")
    conn.commit()
    conn.close()
    st, r = call(env, "POST", "/api/apply/run", {"confirm": True, "fingerprint": fp})
    assert st == 409 and "cambiato" in r["detail"], (st, r)
    assert not env.out.exists(), "nessuna scrittura con piano diverso da quello confermato"
    # con l'impronta del piano attuale la richiesta passa
    _, p2 = call(env, "GET", "/api/apply/preview")
    assert p2["fingerprint"] != fp
    st, r2 = call(env, "POST", "/api/apply/run", {"confirm": True, "fingerprint": p2["fingerprint"]})
    assert st == 200, (st, r2)
    assert wait_job(env, r2["job_id"])["state"] == "done"


def test_cancel_is_prompt_and_rerun_resumes(env: Env) -> None:
    """Annullo dopo ~7 file: stato coerente (disco == contatori), rilancio riprende senza duplicare."""
    env2 = Env(n_good=40)  # ambiente dedicato, piu' file
    try:
        def created() -> int:
            return len(_files(env2.out))

        job = FakeJob(cancel_when=lambda: created() >= 7)
        assert routes_apply._run_lock.acquire(blocking=False)
        res = routes_apply._run_job(job, env2.cfg)
        assert res.get("annullato") is True, res
        n = created()
        assert 7 <= n <= 8, n  # si ferma al file successivo, non alla fine del lotto
        assert res["link"] == n and res["copie"] == 0, (res, n)
        assert not list(env2.out.rglob("*.fotoscan-tmp"))
        assert routes_apply._run_lock.acquire(blocking=False)  # lock rilasciato
        routes_apply._run_lock.release()

        assert routes_apply._run_lock.acquire(blocking=False)
        res2 = routes_apply._run_job(FakeJob(), env2.cfg)
        assert res2["gia_presenti"] == n and res2["link"] == 40 - n and res2["errori"] == 0, res2
        names = [p.name for p in _files(env2.out)]
        assert len(names) == 40 and len(set(names)) == 40, len(names)
    finally:
        env2.close()


def test_failed_copy_leaves_no_partial_file(env: Env) -> None:
    env.cfg.link_mode = "copy"
    orig_copy2 = shutil.copy2
    calls = {"n": 0}

    def flaky(src, dst, *a, **k):  # il primo file si interrompe a meta'
        calls["n"] += 1
        if calls["n"] == 1:
            Path(dst).write_bytes(b"parziale")
            raise OSError(28, "Disco pieno")
        return orig_copy2(src, dst, *a, **k)

    with patched(shutil, "copy2", flaky):
        st, r = call(env, "POST", "/api/apply/run", {"confirm": True})
        job = wait_job(env, r["job_id"])
    # _make_env: 3 file buoni (il primo copiato si interrompe) + 1 mancante
    assert job["result"]["errori"] == 2 and job["result"]["copie"] == 2, job["result"]
    assert not list(env.out.rglob("*.fotoscan-tmp")), "residuo temporaneo"
    names = sorted(p.name for p in _files(env.out))
    assert names == ["IMG_0002.jpg", "IMG_0003.jpg"], names


def test_restart_resumes_without_duplicates(env: Env) -> None:
    """Un job interrotto (server riavviato) lascia file gia' creati: il rilancio li salta."""
    env2 = Env(n_good=10)
    try:
        routes_apply._run_lock.acquire(blocking=False)
        first = routes_apply._run_job(FakeJob(cancel_when=lambda: len(_files(env2.out)) >= 4), env2.cfg)
        assert first.get("annullato") is True
        # simulazione del riavvio: nessun job in memoria, nessun 'fine' di registro per il run interrotto
        routes_apply._run_lock.acquire(blocking=False)
        second = routes_apply._run_job(FakeJob(), env2.cfg)
        assert second["gia_presenti"] + second["link"] == 10, second
        assert len(_files(env2.out)) == 10
    finally:
        env2.close()


def _add_file(env: Env, fid: int, folder: str, name: str, content: bytes, sub: str = "x", hash_: str | None = "auto") -> Path:
    from fotoscan.scanner_metadata import file_hash

    p = env.src / sub / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(content)
    h = file_hash(str(p)) if hash_ == "auto" else hash_
    conn = env.db()
    conn.execute("INSERT INTO files(id,path,name,kind,size,hash,status,date_taken,date_source) "
                 "VALUES(?,?,?,?,?,?,'embedded','2020-06-09T10:00:00','exif')",
                 (fid, str(p), name, "image", len(content), h))
    conn.execute("INSERT INTO plan(file_id,folder,context,score,is_dup,dup_of) VALUES(?,?,?,?,0,NULL)",
                 (fid, folder, None, 0.9))
    conn.commit()
    conn.close()
    return p


def _apply(env: Env, **kw) -> dict:
    from fotoscan import organizer

    conn = env.db()
    try:
        return organizer.apply_plan(conn, env.cfg, apply=True, **kw)
    finally:
        conn.close()


def test_safe_name_reserved_and_long(env: Env) -> None:
    from fotoscan import organizer

    assert organizer.safe_name("CON") == "CON_" and organizer.safe_name("nul.txt") == "nul_.txt"
    assert organizer.safe_name("COM1.jpg") == "COM1_.jpg" and organizer.safe_name("console.jpg") == "console.jpg"
    n = organizer.safe_name("a" * 300 + ".jpg")
    assert len(n) <= organizer.MAX_COMPONENT and n.endswith(".jpg"), n
    assert organizer.safe_folder("Aux/ok/" + "b" * 300).split("/")[0] == "Aux_"


def test_alt_name_with_different_content_is_never_counted_as_present(env: Env) -> None:
    """nome e nome_<id> gia' occupati da file DIVERSI: il file si scrive con un terzo nome, non e' 'gia' presente'."""
    folder = "2020/06 - Alt"
    a = _add_file(env, 20, folder, "dup.jpg", b"AAAA" * 20, sub="a")
    (env.out / folder).mkdir(parents=True)
    (env.out / folder / "dup.jpg").write_bytes(b"altro contenuto 1")
    (env.out / folder / "dup_20.jpg").write_bytes(b"altro contenuto 2")
    env.cfg.link_mode = "copy"
    res = _apply(env)
    assert res["errori"] == 1, res  # l'unico errore e' il file mancante della fixture
    assert (env.out / folder / "dup_20_2.jpg").read_bytes() == a.read_bytes()
    assert (env.out / folder / "dup.jpg").read_bytes() == b"altro contenuto 1"
    assert (env.out / folder / "dup_20.jpg").read_bytes() == b"altro contenuto 2"
    res2 = _apply(env)  # riapplicare non crea altre copie
    assert res2["gia_presenti"] >= 1 and not (env.out / folder / "dup_20_3.jpg").exists(), res2


def test_missing_hash_does_not_multiply_copies_on_rerun(env: Env) -> None:
    folder = "2020/06 - NoHash"
    _add_file(env, 30, folder, "nohash.jpg", b"contenuto" * 9, hash_=None)
    env.cfg.link_mode = "copy"
    _apply(env)
    _apply(env)
    names = sorted(p.name for p in (env.out / folder).iterdir())
    assert names == ["nohash.jpg"], names


def test_reserved_name_is_written_safely(env: Env) -> None:
    folder = "2020/06 - Riservati"
    _add_file(env, 40, folder, "NUL.jpg", b"riservato" * 5)
    res = _apply(env)
    assert res["errori"] == 1 and (env.out / folder / "NUL_.jpg").exists(), res  # 1 = file mancante della fixture


def test_stale_tmp_is_cleaned_and_dest_never_overwritten(env: Env) -> None:
    from fotoscan import organizer

    folder = "2020/06 - Tmp"
    p = _add_file(env, 50, folder, "t.jpg", b"tmp" * 30)
    stale = env.out / folder / ("vecchio.jpg" + organizer.TMP_SUFFIX)
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"parziale")
    env.cfg.link_mode = "copy"
    _apply(env)
    assert not stale.exists()
    dest = env.out / folder / "esiste.jpg"
    dest.write_bytes(b"non toccare")
    try:
        organizer._copy_atomic(str(p), dest)
        raise AssertionError("avrebbe dovuto rifiutare di sovrascrivere")
    except FileExistsError:
        pass
    assert dest.read_bytes() == b"non toccare" and not list(dest.parent.glob("*" + organizer.TMP_SUFFIX))


def test_disk_full_stops_after_three_errors(env: Env) -> None:
    import errno

    from fotoscan import organizer

    folder = "2020/06 - Pieno"
    for i in range(60, 70):
        _add_file(env, i, folder, f"f{i}.jpg", f"pieno {i}".encode() * 8)
    env.cfg.link_mode = "copy"
    calls = []

    def boom(src, dest):
        calls.append(src)
        raise OSError(errno.ENOSPC, "No space left on device")

    with patched(organizer, "_copy_atomic", boom):
        res = _apply(env)
    assert res["errori"] == 3 and res.get("spazio_esaurito") == 1 and len(calls) == 3, res


def test_hardlink_unsupported_falls_back_to_copy_in_preview(env: Env) -> None:
    from fotoscan import organizer

    with patched(organizer, "can_hardlink", lambda *a, **k: False):
        st, p = call(env, "GET", "/api/apply/preview")
    assert st == 200 and p["hardlink_supported"] is False and p["link"] == 0 and p["copies"] == 4, p
    assert any("non supporta i collegamenti" in w for w in p["warnings"]), p["warnings"]
    assert p["extra_bytes"] > 0


def test_run_refused_while_plan_rebuilds(env: Env) -> None:
    with patched(jobs, "running_job", lambda name: object() if name == "piano" else None):
        pass  # (start_job_unique legge _jobs: simulato sotto)
    job = jobs.Job(id="piano-finto", name="piano")
    with jobs._jlock:
        jobs._jobs[job.id] = job
    try:
        st, r = call(env, "POST", "/api/apply/run", {"confirm": True})
        assert st == 409, (st, r)
        assert not routes_apply._run_lock.locked(), "il lock dell'apply non deve restare preso"
    finally:
        job.state = "done"
        with jobs._jlock:
            jobs._jobs.pop(job.id, None)


def test_suggest_destination_on_posix_is_not_root(env: Env) -> None:
    st, r = call(env, "GET", "/api/apply/suggest")
    assert st == 200 and r["path"] and (os.name == "nt" or not r["path"].startswith("/PhotoScan")), r


TESTS = [test_preview_hardlink_same_volume, test_preview_copy_mode_reports_bytes,
         test_preview_low_space_warns, test_preview_other_volume_uses_copy, test_fmt_bytes_matches_frontend_units,
         test_source_from_catalog_when_not_in_config,
         test_suggest_is_same_volume_and_creates_nothing, test_destination_creates_empty_folder_and_saves,
         test_destination_rejects_bad_paths, test_destination_not_writable,
         test_output_inside_source_rejected, test_run_requires_explicit_confirm,
         test_run_then_rerun_does_not_duplicate, test_copy_mode_creates_independent_files,
         test_name_collision_and_special_names, test_live_counters_and_log,
         test_log_is_readable_and_tail_works, test_log_missing_when_never_run,
         test_second_run_while_running_is_409, test_run_rejects_plan_changed_after_preview,
         test_cancel_is_prompt_and_rerun_resumes,
         test_failed_copy_leaves_no_partial_file, test_restart_resumes_without_duplicates,
         test_safe_name_reserved_and_long, test_alt_name_with_different_content_is_never_counted_as_present,
         test_missing_hash_does_not_multiply_copies_on_rerun, test_reserved_name_is_written_safely,
         test_stale_tmp_is_cleaned_and_dest_never_overwritten, test_disk_full_stops_after_three_errors,
         test_hardlink_unsupported_falls_back_to_copy_in_preview, test_run_refused_while_plan_rebuilds,
         test_suggest_destination_on_posix_is_not_root]


if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            env = _make_env()
            try:
                t(env)
            finally:
                env.close()
            print(f"OK   {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print("tutti i test superati" if not failed else f"{failed} test falliti")
    sys.exit(1 if failed else 0)
