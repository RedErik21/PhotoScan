"""Test degli aggiornamenti (fotoscan/updater.py e /api/update): versioni, scelta della Release, download
con zip finto, installazione su una cartella finta con copia di riserva e ripristino.

Nessuna connessione: fetch_releases e fetch_zip sono sostituiti. Config, catalogo e cache in una cartella
temporanea; l'installazione scrive solo in una radice finta, mai nel progetto vero.
Esecuzione: `.\\.venv\\Scripts\\python.exe tests\\test_updater.py`
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="updater_test_"))
os.environ["FOTOSCAN_CONFIG"] = str(_TMP / "config.json")
os.environ["FOTOSCAN_DB"] = str(_TMP / "catalogo.db")
os.environ["FOTOSCAN_CACHE"] = str(_TMP / "cache")
os.environ["FOTOSCAN_NETLOG"] = str(_TMP / "rete.jsonl")
os.environ["FOTOSCAN_UPDATE_REPO"] = "esempio/fotoscan"
(_TMP / "config.json").write_text(json.dumps({"source_dir": str(_TMP)}), encoding="utf-8")

from fotoscan import updater  # noqa: E402
from fotoscan.version import __version__  # noqa: E402

NEW = "9.9.9"


def _release(tag, prerelease=False, draft=False):
    return {"tag_name": tag, "name": f"PhotoScan {tag}", "body": f"Novità di {tag}", "prerelease": prerelease,
            "draft": draft, "published_at": "2026-11-24T10:00:00Z", "html_url": f"https://github.com/x/{tag}",
            "zipball_url": f"https://api.github.com/repos/esempio/fotoscan/zipball/{tag}"}


def _zip_bytes(version=NEW, extra: dict | None = None, top="esempio-fotoscan-abc123", drop=()) -> bytes:
    files = {
        "fotoscan/__init__.py": "",
        "fotoscan/version.py": f'__version__ = "{version}"\nUPDATE_REPO = ""\n',
        "fotoscan/server/app.py": "# nuovo\n",
        "frontend/index.html": "<!doctype html>nuovo",
        "requirements.txt": "fastapi\nnuova-libreria\n",
        "README.md": "nuovo readme",
        "config.json": '{"rubato": true}',          # mai installato: dato dell'utente
        "catalogo.db": "x",                          # idem
    }
    files.update(extra or {})
    for name in drop:
        files.pop(name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(f"{top}/{name}" if top else name, text)
    return buf.getvalue()


def _fake_fetch_zip(data: bytes):
    def fetch(url, dest, progress=None, cancelled=None):
        dest.write_bytes(data)
        if progress:
            progress(len(data), len(data))
    return fetch


def _fake_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="updater_root_", dir=_TMP))
    (root / "fotoscan" / "server").mkdir(parents=True)
    (root / "fotoscan" / "__init__.py").write_text("", encoding="utf-8")
    (root / "fotoscan" / "version.py").write_text('__version__ = "2.0.0"\n', encoding="utf-8")
    (root / "fotoscan" / "server" / "app.py").write_text("# vecchio\n", encoding="utf-8")
    (root / "fotoscan" / "server" / "routes_vecchia.py").write_text("router = None\n", encoding="utf-8")
    (root / "frontend").mkdir()
    (root / "frontend" / "index.html").write_text("vecchio", encoding="utf-8")
    (root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (root / "config.json").write_text('{"mio": 1}', encoding="utf-8")
    (root / "catalogo.db").write_text("catalogo vero", encoding="utf-8")
    (root / "cache").mkdir()
    (root / "cache" / "miniatura.jpg").write_text("m", encoding="utf-8")
    return root


# ------------------------------------------------------------------ versioni e scelta della Release
def test_versions():
    pv = updater.parse_version
    assert pv("v2.1.0") > pv("2.0.9")
    assert pv("2.1.0") > pv("2.1.0-beta.3") > pv("2.1.0-beta.2") > pv("2.0.0")
    assert pv("2.10.0") > pv("2.9.9")
    assert pv("v3") == pv("3.0.0")
    assert updater.is_newer("2.0.1", "2.0.0") and not updater.is_newer("2.0.0", "2.0.0")
    print("ok versions")


def test_pick_release():
    rels = [_release("v2.1.0"), _release("v2.2.0-beta.1", prerelease=True), _release("v3.0.0", draft=True),
            _release("v2.0.5"), {"bad": 1}, "spazzatura"]
    assert updater._pick_release(rels, prerelease=False)["tag_name"] == "v2.1.0"
    assert updater._pick_release(rels, prerelease=True)["tag_name"] == "v2.2.0-beta.1"
    assert updater._pick_release([], prerelease=True) is None
    print("ok pick_release")


def test_check_saves_state_and_errors():
    cache = _TMP / "cache_check"
    orig = updater.fetch_releases
    try:
        updater.fetch_releases = lambda repo: [_release("v0.0.1"), _release(f"v{NEW}")]
        st = updater.check(cache)
        assert st["available"] and st["latest"]["version"] == NEW and st["error"] is None, st
        assert updater.read_state(cache)["latest"]["version"] == NEW

        updater.fetch_releases = lambda repo: [_release("v0.0.1")]
        st = updater.check(cache)
        assert not st["available"] and st["latest"]["version"] == "0.0.1"

        def boom(repo):
            raise TimeoutError("lento")
        updater.fetch_releases = boom
        st = updater.check(cache)
        assert not st["available"] and "non raggiungibile" in st["error"], st
    finally:
        updater.fetch_releases = orig
    print("ok check")


def test_check_without_repo_makes_no_request():
    from fotoscan import version
    orig_repo, orig_fetch = os.environ.pop("FOTOSCAN_UPDATE_REPO"), updater.fetch_releases
    orig_default = version.UPDATE_REPO
    try:
        version.UPDATE_REPO = ""  # repository di default non impostato: nessuna richiesta
        def never(repo):
            raise AssertionError("nessuna richiesta senza repository")
        updater.fetch_releases = never
        st = updater.check(_TMP / "cache_norepo")
        assert "non configurati" in st["error"]
    finally:
        version.UPDATE_REPO = orig_default
        os.environ["FOTOSCAN_UPDATE_REPO"] = orig_repo
        updater.fetch_releases = orig_fetch
    print("ok check_without_repo")


# ------------------------------------------------------------------ download
def test_download_stages_and_verifies():
    cache = _TMP / "cache_dl"
    info = updater._release_info(_release(f"v{NEW}"))
    orig = updater.fetch_zip
    try:
        updater.fetch_zip = _fake_fetch_zip(_zip_bytes())
        seen = []
        p = updater.download(cache, info, progress=lambda d, t: seen.append((d, t)))
        assert p["version"] == NEW and not p["approved"] and seen
        staged = Path(p["dir"])
        assert (staged / "fotoscan" / "version.py").is_file() and (staged / "frontend" / "index.html").is_file()
        assert updater.read_pending(cache)["version"] == NEW
        assert not list((cache / "updates" / "download").glob("*.zip")), "lo zip va cancellato dopo l'estrazione"

        # pacchetto senza PhotoScan dentro
        updater.fetch_zip = _fake_fetch_zip(_zip_bytes(drop=("frontend/index.html",)))
        try:
            updater.download(_TMP / "cache_dl2", info)
            raise AssertionError("pacchetto incompleto accettato")
        except updater.UpdateError as e:
            assert "non contiene PhotoScan" in str(e)

        # versione non piu' recente
        updater.fetch_zip = _fake_fetch_zip(_zip_bytes(version="0.0.1"))
        try:
            updater.download(_TMP / "cache_dl3", info)
            raise AssertionError("versione vecchia accettata")
        except updater.UpdateError as e:
            assert "non più recente" in str(e)
        assert updater.read_pending(_TMP / "cache_dl3") is None
    finally:
        updater.fetch_zip = orig
    print("ok download")


def test_zip_slip_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("top/fotoscan/__init__.py", "")
        zf.writestr("top/../../fuori.txt", "male")
    dest = _TMP / "slip"
    dest.mkdir()
    try:
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
            updater._safe_extract(zf, dest)
        raise AssertionError("percorso fuori dalla cartella accettato")
    except updater.UpdateError:
        pass
    assert not (_TMP / "fuori.txt").exists()
    print("ok zip_slip")


# ------------------------------------------------------------------ installazione
def _stage(cache: Path, data: bytes | None = None) -> None:
    orig = updater.fetch_zip
    try:
        updater.fetch_zip = _fake_fetch_zip(data or _zip_bytes())
        updater.download(cache, updater._release_info(_release(f"v{NEW}")))
    finally:
        updater.fetch_zip = orig


def test_apply_requires_approval_and_keeps_user_data():
    root, cache = _fake_root(), _TMP / "cache_apply"
    _stage(cache)
    assert updater.apply_pending(root, cache) is None, "senza approvazione non si installa"
    updater.set_approved(cache, True)
    res = updater.apply_pending(root, cache)
    assert res["from"] == "2.0.0" and res["to"] == NEW and res["requirements_changed"], res
    assert updater.read_version(root) == NEW
    assert (root / "fotoscan" / "server" / "app.py").read_text(encoding="utf-8") == "# nuovo\n"
    assert not (root / "fotoscan" / "server" / "routes_vecchia.py").exists(), "i file tolti devono sparire"
    assert (root / "README.md").read_text(encoding="utf-8") == "nuovo readme"
    # dati dell'utente intatti
    assert (root / "config.json").read_text(encoding="utf-8") == '{"mio": 1}'
    assert (root / "catalogo.db").read_text(encoding="utf-8") == "catalogo vero"
    assert (root / "cache" / "miniatura.jpg").exists()
    # copia di riserva, pending consumato, esito salvato
    backup = Path(res["backup"])
    assert (backup / "fotoscan" / "server" / "routes_vecchia.py").exists()
    assert updater.read_pending(cache) is None
    assert updater.read_last_install(cache)["to"] == NEW
    print("ok apply")


def test_apply_force_and_git_checkout():
    root, cache = _fake_root(), _TMP / "cache_git"
    (root / ".git").mkdir()
    _stage(cache)
    assert updater.apply_pending(root, cache, force=True) is None, "un clone git non va sovrascritto"
    assert updater.read_version(root) == "2.0.0"
    (root / ".git").rmdir()
    assert updater.apply_pending(root, cache, force=True)["to"] == NEW, "force = installazione automatica"
    print("ok apply_force_git")


def test_apply_failure_restores_previous_version():
    root, cache = _fake_root(), _TMP / "cache_fail"
    _stage(cache)
    updater.set_approved(cache, True)
    orig = updater._replace_dir
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # la prima cartella e' gia' sostituita: il ripristino deve rimetterla
            raise OSError("disco pieno")
        orig(src, dst)
    updater._replace_dir = flaky
    try:
        updater.apply_pending(root, cache)
        raise AssertionError("errore non segnalato")
    except updater.UpdateError as e:
        assert "2.0.0" in str(e)
    finally:
        updater._replace_dir = orig
    assert updater.read_version(root) == "2.0.0"
    assert (root / "fotoscan" / "server" / "app.py").read_text(encoding="utf-8") == "# vecchio\n"
    assert (root / "fotoscan" / "server" / "routes_vecchia.py").exists()
    assert (root / "frontend" / "index.html").read_text(encoding="utf-8") == "vecchio"
    assert (root / "requirements.txt").read_text(encoding="utf-8") == "fastapi\n"
    assert not (root / "README.md").exists(), "le voci nuove vanno tolte nel ripristino"
    assert updater.read_pending(cache) is not None, "l'aggiornamento resta pronto per un nuovo tentativo"
    print("ok apply_failure_restore")


# ------------------------------------------------------------------ API
def test_api_flow():
    from fotoscan.server import jobs, routes_status, routes_update

    orig = (updater.fetch_releases, updater.fetch_zip, updater.is_git_checkout)
    updater.fetch_releases = lambda repo: [_release(f"v{NEW}")]
    updater.fetch_zip = _fake_fetch_zip(_zip_bytes())
    updater.is_git_checkout = lambda root: False
    try:
        st = routes_update.update_status()
        assert st["current"] == __version__ and st["configured"] and st["check"] is None
        assert st["settings"] == {"update_check_on_start": True, "update_auto_download": False,
                                  "update_auto_install": False, "update_prerelease": False}

        st = routes_update.update_check()
        assert st["check"]["available"] and st["check"]["latest"]["version"] == NEW and st["pending"] is None

        job_id = routes_update.update_download()["job_id"]
        end = time.time() + 20
        while jobs.get_job(job_id).state == "running" and time.time() < end:
            time.sleep(0.05)
        assert jobs.get_job(job_id).state == "done", jobs.get_job(job_id).error
        st = routes_update.update_status()
        assert st["pending"]["version"] == NEW and not st["pending"]["approved"]

        st = routes_update.update_install()
        assert st["pending"]["approved"]

        st = routes_update.update_discard()
        assert st["pending"] is None

        # impostazioni: validate come le altre
        ok, errors = routes_status._validate({"update_auto_install": True, "update_prerelease": "si"})
        assert ok == {"update_auto_install": True} and "update_prerelease" in errors
    finally:
        updater.fetch_releases, updater.fetch_zip, updater.is_git_checkout = orig
    print("ok api_flow")



# ------------------------------------------------------------------ eseguibile (PhotoScan.exe)
def _exe_zip(path: Path, version: str) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("PhotoScan/PhotoScan.exe", b"exe nuovo " + version.encode())
        zf.writestr("PhotoScan/_internal/base.dll", b"dll " + version.encode())


def test_exe_assets_selection():
    assets = [
        {"name": "PhotoScan-0.2.0-windows.zip.002", "browser_download_url": "u2", "size": 5},
        {"name": "PhotoScan-0.2.0-windows.zip.001", "browser_download_url": "u1", "size": 9},
        {"name": "PhotoScan-0.1.9-windows.zip", "browser_download_url": "old", "size": 1},
        {"name": "PhotoScan-0.2.0-windows.zip", "browser_download_url": "single", "size": 3},
        {"name": "fotoscan-source.zip", "browser_download_url": "src", "size": 2},
    ]
    got = updater._exe_assets(assets, "0.2.0")
    assert [a["url"] for a in got] == ["single", "u1", "u2"], [a["url"] for a in got]
    assert updater._exe_assets(assets[2:3], "0.2.0") == [], "versione diversa non deve valere"
    assert [a["url"] for a in updater._exe_assets(assets[:2], "0.2.0")] == ["u1", "u2"], "parti in ordine"
    print("ok exe_assets_selection")


def test_exe_download_and_install():
    import time as _t
    cache = _TMP / "cache_exe"
    app = _TMP / "app_exe" / "PhotoScan"
    (app / "_internal").mkdir(parents=True)
    (app / "PhotoScan.exe").write_bytes(b"exe vecchio")
    (app / "_internal" / "base.dll").write_bytes(b"dll vecchio")
    (app / "config_utente.txt").write_text("resta", encoding="utf-8")  # non e' nella cartella: non deve sparire
    info = {"version": "0.2.0", "tag": "v0.2.0", "name": "x", "notes": "",
            "exe_assets": [{"name": "PhotoScan-0.2.0-windows.zip", "url": "u", "size": 1}]}
    orig_parts = updater.fetch_parts

    def fake_parts(parts, dest, progress=None, cancelled=None):
        _exe_zip(dest, "0.2.0")

    updater.fetch_parts = fake_parts
    try:
        pending = updater.download(cache, info, approved=True, exe=True)
    finally:
        updater.fetch_parts = orig_parts
    assert pending["kind"] == "exe" and (Path(pending["dir"]) / "PhotoScan.exe").is_file()
    assert updater.start_exe_update(cache, app, no_launch=True, process_name="NessunProcessoPhotoScanXYZ")
    end = _t.time() + 60
    while not (cache / "updates" / "installed.json").exists() and _t.time() < end:
        _t.sleep(0.2)
    installed = json.loads((cache / "updates" / "installed.json").read_text(encoding="utf-8-sig"))
    assert installed["to"] == "0.2.0", installed
    assert (app / "PhotoScan.exe").read_bytes() == b"exe nuovo 0.2.0"
    assert (app / "_internal" / "base.dll").read_bytes() == b"dll 0.2.0"
    backup = Path(installed["backup"])
    assert (backup / "PhotoScan.exe").read_bytes() == b"exe vecchio", "copia di riserva del vecchio"
    assert updater.read_pending(cache) is None, "pending consumato"
    print("ok exe_download_and_install")



def test_exe_rollback_when_package_broken():
    import time as _t
    cache = _TMP / "cache_exe_rb"
    app = _TMP / "app_exe_rb" / "PhotoScan"
    (app / "_internal").mkdir(parents=True)
    (app / "PhotoScan.exe").write_bytes(b"exe vecchio")
    staged = cache / "updates" / "staging" / "exe-rotto"
    staged.mkdir(parents=True)
    (staged / "_internal").mkdir()  # manca PhotoScan.exe: pacchetto rotto
    updater._write_json(cache / "updates" / "pending.json",
                        {"version": "0.3.0", "dir": str(staged), "kind": "exe", "approved": True})
    assert updater.start_exe_update(cache, app, no_launch=True, process_name="NessunProcessoPhotoScanXYZ")
    log = cache / "updates" / "apply-exe.log"
    end = _t.time() + 60
    while not (log.exists() and "ripristino" in log.read_text(encoding="utf-8-sig")) and _t.time() < end:
        _t.sleep(0.2)
    assert "ripristino" in log.read_text(encoding="utf-8-sig"), "lo script doveva segnalare il ripristino"
    assert (app / "PhotoScan.exe").read_bytes() == b"exe vecchio", "la versione vecchia torna al suo posto"
    assert not (cache / "updates" / "installed.json").exists(), "nessuna installazione registrata"
    assert updater.read_pending(cache) is None, "pacchetto rotto scartato"
    print("ok exe_rollback")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"tutti i {len(tests)} test passati")
