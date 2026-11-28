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
        {"name": "PhotoScan-0.2.0-windows-cpu.zip.001", "browser_download_url": "cpu1", "size": 4},
        {"name": "PhotoScan-0.2.0-x86_64.AppImage", "browser_download_url": "appimg", "size": 7},
        {"name": "fotoscan-source.zip", "browser_download_url": "src", "size": 2},
    ]
    got = updater._exe_assets(assets, "0.2.0", platform="win32", runtime="gpu")
    assert [a["url"] for a in got] == ["u1", "u2"], [a["url"] for a in got]
    got_cpu = updater._exe_assets(assets, "0.2.0", platform="win32", runtime="cpu")
    assert [a["url"] for a in got_cpu] == ["cpu1"], got_cpu
    got_linux = updater._exe_assets(assets, "0.2.0", platform="linux")
    assert [a["url"] for a in got_linux] == ["appimg"], got_linux
    assert updater._exe_assets(assets[2:3], "0.2.0", platform="win32", runtime="gpu") == [], "versione diversa non deve valere"
    assert [a["url"] for a in updater._exe_assets(assets[:2], "0.2.0", platform="win32", runtime="gpu")] == ["u1", "u2"], "parti in ordine"
    print("ok exe_assets_selection")


def test_appimage_install_replaces_running_file():
    """Linux: l'AppImage scaricato sostituisce quello in uso ($APPIMAGE), con copia di riserva."""
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "cache"
        target = Path(td) / "PhotoScan-old.AppImage"
        target.write_bytes(b"old")
        new = updater.updates_dir(cache) / "download" / "PhotoScan-0.2.0-x86_64.AppImage"
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_bytes(b"new")
        updater._write_json(updater.updates_dir(cache) / "pending.json",
                            {"version": "0.2.0", "kind": "appimage", "approved": True, "dir": str(new)})
        old_env = os.environ.get("APPIMAGE")
        os.environ["APPIMAGE"] = str(target)
        try:
            assert updater.start_exe_update(cache, Path(td)) is True
        finally:
            if old_env is None:
                os.environ.pop("APPIMAGE", None)
            else:
                os.environ["APPIMAGE"] = old_env
        assert target.read_bytes() == b"new", "AppImage non sostituito"
        backups = list((updater.updates_dir(cache) / "backup-appimage").glob("*.AppImage"))
        assert backups and backups[0].read_bytes() == b"old", "copia di riserva mancante"
        assert updater.read_pending(cache) is None, "pending non rimosso"
    print("ok appimage_install_replaces_running_file")


def test_exe_download_and_install():
    if sys.platform != "win32":
        print("skip exe_download_and_install (solo Windows: su Linux il pacchetto e' un AppImage)")
        return
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
    if sys.platform != "win32":
        print("skip exe_rollback (solo Windows: il ripristino usa lo script PowerShell)")
        return
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
    while not (log.exists() and "danneggiato" in log.read_text(encoding="utf-8-sig")) and _t.time() < end:
        _t.sleep(0.2)
    assert "danneggiato" in log.read_text(encoding="utf-8-sig"), "lo script doveva segnalare il pacchetto danneggiato"
    assert (app / "PhotoScan.exe").read_bytes() == b"exe vecchio", "la versione vecchia torna al suo posto"
    assert not (cache / "updates" / "installed.json").exists(), "nessuna installazione registrata"
    assert updater.read_pending(cache) is None, "pacchetto rotto scartato"
    failure = updater.read_last_failure(cache)
    assert failure and failure["to"] == "0.3.0" and "danneggiato" in failure["message"], failure
    print("ok exe_rollback")


def test_exe_install_with_app_folder_as_cwd():
    """Regressione: il processo che lancia lo script aveva la cartella del programma come directory corrente;
    lo spostamento falliva con «elemento in uso». Lo script ora parte fuori da quella cartella."""
    if sys.platform != "win32":
        print("skip exe_install_cwd (solo Windows)")
        return
    import time as _t
    cache = _TMP / "cache_exe_cwd"
    app = _TMP / "app_exe_cwd" / "PhotoScan"
    (app / "_internal").mkdir(parents=True)
    (app / "PhotoScan.exe").write_bytes(b"exe vecchio")
    staged = cache / "updates" / "staging" / "exe-cwd"
    (staged / "_internal").mkdir(parents=True)
    (staged / "PhotoScan.exe").write_bytes(b"exe nuovo")
    updater._write_json(cache / "updates" / "pending.json",
                        {"version": "0.4.0", "dir": str(staged), "kind": "exe", "approved": True})
    old_cwd = os.getcwd()
    os.chdir(app)
    try:
        assert updater.start_exe_update(cache, app, no_launch=True, process_name="NessunProcessoPhotoScanXYZ")
    finally:
        os.chdir(old_cwd)
    end = _t.time() + 60
    while not (cache / "updates" / "installed.json").exists() and _t.time() < end:
        _t.sleep(0.2)
    log = (cache / "updates" / "apply-exe.log").read_text(encoding="utf-8-sig")
    assert (cache / "updates" / "installed.json").exists(), log
    assert (app / "PhotoScan.exe").read_bytes() == b"exe nuovo", log
    print("ok exe_install_cwd")


# ------------------------------------------------------------------ versioni (voci 25), parti complete, nomi degli asset
def test_versions_four_numbers_and_labels():
    pv = updater.parse_version
    assert pv("0.1.10") > pv("0.1.9"), "0.1.10 e' piu' recente di 0.1.9 (numeri, non testo)"
    assert pv("0.1.10.1") > pv("0.1.10") and pv("0.1.10.0") == pv("0.1.10")
    assert pv("1.0.0") > pv("1.0.0-rc.1") > pv("1.0.0-beta.2") > pv("1.0.0-beta.1") > pv("1.0.0-alpha.9")
    assert pv("v0.1.11") > pv("0.1.10") and updater.is_newer("0.1.11", "0.1.10")
    assert not updater.is_newer("0.1.9", "0.1.10")
    print("ok versions_four_numbers_and_labels")


def test_incomplete_parts_are_not_offered():
    """Release ancora in caricamento: parti con un buco (.001, .003) o senza la prima non producono uno zip troncato."""
    mk = lambda n: {"name": f"PhotoScan-0.2.0-windows.zip.{n:03d}", "browser_download_url": f"u{n}", "size": 1}
    assert updater._exe_assets([mk(1), mk(3)], "0.2.0", platform="win32", runtime="gpu") == []
    assert updater._exe_assets([mk(2), mk(3)], "0.2.0", platform="win32", runtime="gpu") == []
    assert [a["url"] for a in updater._exe_assets([mk(2), mk(1), mk(3)], "0.2.0", platform="win32", runtime="gpu")] == ["u1", "u2", "u3"]
    single = {"name": "PhotoScan-0.2.0-windows.zip", "browser_download_url": "s", "size": 1}
    assert [a["url"] for a in updater._exe_assets([single], "0.2.0", platform="win32", runtime="gpu")] == ["s"]
    print("ok incomplete_parts_are_not_offered")


def test_new_asset_names_do_not_match_old_regexes():
    """Gli updater <= 0.1.10 e il Setup vecchio usano queste espressioni: non devono vedere gli asset nuovi, altrimenti
    scaricherebbero un'app leggera credendola completa."""
    import re
    old_exe = re.compile(r"^PhotoScan-(?P<v>\d[\w.\-]*?)-windows\.zip(?:\.(?P<n>\d{3}))?$")
    old_cpu = re.compile(r"^PhotoScan-(?P<v>\d[\w.\-]*?)-windows-cpu\.zip(?:\.(?P<n>\d{3}))?$")
    old_setup = re.compile(r"^PhotoScan-(?P<v>\d[\w.\-]*?)-(?P<rt>windows|windows-cpu)\.zip(?:\.(?P<n>\d{3}))?$")
    for name in ("PhotoScan-app-0.1.11-windows.zip", "PhotoScan-app-0.1.11-windows.zip.001",
                 "PhotoScan-runtime-1-windows.zip.002", "PhotoScan-runtime-1-windows-cpu.zip",
                 "PhotoScan-Setup-0.1.11.exe", "SHA256SUMS", "release-manifest.json"):
        for rx in (old_exe, old_cpu, old_setup):
            assert not rx.match(name), (name, rx.pattern)
    for name in ("PhotoScan-0.1.11-windows.zip.001", "PhotoScan-0.1.11-windows-cpu.zip.002"):
        assert old_setup.match(name), name
    # e i nuovi nomi si riconoscono
    assets = [{"name": n, "browser_download_url": "u-" + n, "size": 5} for n in (
        "PhotoScan-app-0.1.11-windows.zip.002", "PhotoScan-app-0.1.11-windows.zip.001")]
    assert [a["name"] for a in updater._app_assets(assets, "0.1.11")][0].endswith(".001")
    print("ok new_asset_names_do_not_match_old_regexes")


def test_runtime_index_finds_older_release():
    """Il runtime si pubblica solo quando cambia: l'updater lo cerca in tutte le Release."""
    def asset(name, size=3):
        return {"name": name, "browser_download_url": "https://x/" + name, "size": size}
    newest = {"tag_name": "v0.1.12", "assets": [asset("SHA256SUMS"), asset("PhotoScan-app-0.1.12-windows.zip")]}
    older = {"tag_name": "v0.1.11", "assets": [asset("SHA256SUMS"),
                                               asset("PhotoScan-runtime-1-windows.zip.001"), asset("PhotoScan-runtime-1-windows.zip.002"),
                                               asset("PhotoScan-runtime-1-windows-cpu.zip")]}
    partial = {"tag_name": "v0.1.9", "assets": [asset("PhotoScan-runtime-2-windows.zip.002")]}   # incompleto
    draft = {"tag_name": "v0.2.0", "draft": True, "assets": [asset("PhotoScan-runtime-3-windows.zip")]}
    idx = updater._runtime_index([newest, older, partial, draft])
    assert set(idx) == {"1"}, idx
    assert [p["name"] for p in idx["1"]["gpu"]["parts"]] == ["PhotoScan-runtime-1-windows.zip.001", "PhotoScan-runtime-1-windows.zip.002"]
    assert [p["name"] for p in idx["1"]["cpu"]["parts"]] == ["PhotoScan-runtime-1-windows-cpu.zip"]
    assert idx["1"]["gpu"]["sums_url"].endswith("SHA256SUMS")
    info = updater._release_info({**newest, "name": "x", "body": ""}, [newest, older])
    assert info["runtime_assets"]["1"]["cpu"]["parts"], "il runtime della Release vecchia e' nell'info della nuova"
    print("ok runtime_index_finds_older_release")


# ------------------------------------------------------------------ download: SHA256SUMS, parti, avanzi
def _write_parts(td: Path, data: bytes, n: int = 2) -> list[dict]:
    step = -(-len(data) // n)
    parts = []
    for i in range(n):
        chunk = data[i * step:(i + 1) * step]
        f = td / f"PhotoScan-app-9.9.9-windows.zip.{i + 1:03d}"
        f.write_bytes(chunk)
        parts.append({"name": f.name, "url": f.as_uri(), "size": len(chunk)})
    return parts


def test_fetch_parts_verifies_sha256_and_size_and_cleans_part_file():
    import hashlib
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data = os.urandom(5000)
        parts = _write_parts(td, data)
        ok_parts = [dict(p, sha256=hashlib.sha256((td / p["name"]).read_bytes()).hexdigest()) for p in parts]
        dest = td / "out" / "pkg.zip"
        dest.parent.mkdir()
        updater.fetch_parts(ok_parts, dest)
        assert dest.read_bytes() == data and not dest.with_name(dest.name + ".part").exists()

        bad = [dict(ok_parts[0], sha256="0" * 64), ok_parts[1]]
        dest2 = td / "out" / "bad.zip"
        try:
            updater.fetch_parts(bad, dest2)
        except updater.UpdateError as e:
            assert "SHA-256" in str(e), e
        else:
            raise AssertionError("impronta sbagliata non rilevata")
        assert not dest2.exists() and not dest2.with_name(dest2.name + ".part").exists(), ".part lasciato dopo l'errore"

        short = [dict(ok_parts[0], size=ok_parts[0]["size"] + 10), ok_parts[1]]
        try:
            updater.fetch_parts(short, td / "out" / "short.zip")
        except updater.UpdateError as e:
            assert "incompleta" in str(e), e
        else:
            raise AssertionError("parte troncata non rilevata")

        cancel = [False]
        try:
            updater.fetch_parts(ok_parts, td / "out" / "c.zip", cancelled=lambda: True)
        except updater.UpdateError as e:
            assert "annullato" in str(e).lower()
        assert not (td / "out" / "c.zip.part").exists()
    print("ok fetch_parts_verifies")


def test_sums_parse_and_attach():
    h = "a" * 64
    sums = updater.parse_sums(f"{h}  PhotoScan-app-1-windows.zip.001\n{'b' * 64} *file con spazi.zip\nriga rotta\n")
    assert sums == {"PhotoScan-app-1-windows.zip.001": h, "file con spazi.zip": "b" * 64}
    parts = [{"name": "PhotoScan-app-1-windows.zip.001", "url": "u", "size": 1}]
    assert updater._attach_sums(parts, sums)[0]["sha256"] == h
    assert "sha256" not in updater._attach_sums(parts, None)[0], "Release senza SHA256SUMS: ammessa"
    try:
        updater._attach_sums([{"name": "altro.zip", "url": "u", "size": 1}], sums)
    except updater.UpdateError as e:
        assert "non verificabile" in str(e)
    else:
        raise AssertionError("asset fuori da SHA256SUMS accettato")
    print("ok sums_parse_and_attach")


def test_exe_download_with_sums_rejects_tampered_part():
    """Windows: lo zip dell'app scaricato con SHA256SUMS sbagliato non arriva in staging."""
    if sys.platform != "win32":
        print("skip exe_download_with_sums (solo Windows)")
        return
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        z = td / "app.zip"
        _exe_zip(z, "0.2.0")
        parts = _write_parts(td, z.read_bytes(), 1)
        info = {"version": "0.2.0", "tag": "v0.2.0", "name": "x", "notes": "", "sums_url": "https://x/SHA256SUMS",
                "exe_assets": parts}
        orig = updater._fetch_text, updater.sys.platform
        updater._fetch_text = lambda url: f"{'0' * 64}  {parts[0]['name']}\n"
        updater.sys.platform = "win32"
        try:
            try:
                updater.download(td / "cache", info, exe=True)
            except updater.UpdateError as e:
                assert "SHA-256" in str(e), e
            else:
                raise AssertionError("pacchetto con impronta sbagliata accettato")
            assert not list((td / "cache" / "updates" / "staging").glob("*")), "staging sporco"
        finally:
            updater._fetch_text, updater.sys.platform = orig
    print("ok exe_download_with_sums_rejects_tampered_part")


# ------------------------------------------------------------------ runtime separato
def _make_runtime(dest: Path, rv="1", flavor="cpu", python=None, site_files=("torch/__init__.py", "torch/lib/x.dll")) -> Path:
    from fotoscan import runtime
    (dest / "site").mkdir(parents=True)
    for rel in site_files:
        f = dest / "site" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * 10)
    (dest / "runtime.json").write_text(json.dumps({
        "runtime_version": rv, "python": python or runtime.python_tag(), "flavor": flavor, "platform": sys.platform,
        "tree_sha256": runtime.tree_hash(dest / "site")}), encoding="utf-8")
    return dest


def test_requirements_runtime_hash_matches_version_file():
    """Se cambia requirements-runtime.txt va alzata RUNTIME_VERSION (e aggiornato l'hash): altrimenti i PC non
    scaricherebbero il runtime nuovo."""
    from fotoscan import runtime, runtime_version
    h = runtime.requirements_hash(ROOT / "requirements-runtime.txt")
    assert h == runtime_version.REQUIREMENTS_SHA256, (
        f"requirements-runtime.txt e' cambiato: alza RUNTIME_VERSION in fotoscan/runtime_version.py e metti "
        f"REQUIREMENTS_SHA256 = {h!r}")
    pins = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if "==" in line and not line.startswith("#"):
            name, ver = line.split("==", 1)
            pins[name.strip().lower()] = ver.strip()
    for line in (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8").splitlines():
        if "==" in line and not line.startswith("#"):
            name, ver = line.split("==", 1)
            if name.strip().lower() in pins:
                assert pins[name.strip().lower()] == ver.strip(), f"{name}: versione diversa fra i due requirements"
    print("ok requirements_runtime_hash_matches_version_file")


def test_requirements_are_pinned():
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            assert "==" in s, f"requirements.txt: {s!r} non e' bloccato con =="
    print("ok requirements_are_pinned")


def test_runtime_check_dir_and_find():
    from fotoscan import runtime
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        good = _make_runtime(td / "good")
        assert runtime.check_dir(good, "1") == (True, "")
        assert not runtime.check_dir(good, "2")[0], "versione del runtime diversa"
        bad_py = _make_runtime(td / "badpy", python="2.7")
        assert "Python" in runtime.check_dir(bad_py, "1")[1]
        assert not runtime.check_dir(td / "vuota", "1")[0]
        # sito mancante
        nosite = _make_runtime(td / "nosite")
        shutil_rmtree(nosite / "site")
        assert not runtime.check_dir(nosite, "1")[0]
        # tree_hash cambia se manca un file (estrazione incompleta)
        h1 = runtime.tree_hash(good / "site")
        (good / "site" / "torch" / "lib" / "x.dll").unlink()
        assert runtime.tree_hash(good / "site") != h1
    print("ok runtime_check_dir_and_find")


def shutil_rmtree(p):
    import shutil
    shutil.rmtree(p)


def test_runtime_activate_and_adopt_bundled():
    from fotoscan import runtime
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ext = td / "ext"
        bundle = _make_runtime(td / "bundle", rv=runtime.required_version(), flavor="gpu")
        old_env = {k: os.environ.get(k) for k in ("FOTOSCAN_RUNTIME_DIR", "FOTOSCAN_RUNTIME_BUNDLE", "FOTOSCAN_RUNTIME")}
        os.environ["FOTOSCAN_RUNTIME_DIR"] = str(ext)
        os.environ["FOTOSCAN_RUNTIME_BUNDLE"] = str(bundle)
        try:
            assert runtime.installed(runtime.required_version(), "gpu") == bundle
            assert runtime.installed(runtime.required_version(), "cpu") is None
            adopted = runtime.adopt_bundled(runtime.required_version(), "gpu")
            assert adopted == ext / f"{runtime.required_version()}-gpu" and (adopted / "site" / "torch" / "__init__.py").is_file()
            assert runtime.adopt_bundled(runtime.required_version(), "cpu") is None, "tipo diverso: non si adotta"
            # prune: tiene solo le versioni richieste e toglie gli avanzi
            (ext / ".staging-9-gpu").mkdir()
            (ext / "0-gpu").mkdir()
            removed = runtime.prune({runtime.required_version()})
            assert sorted(removed) == [".staging-9-gpu", "0-gpu"], removed
            assert adopted.is_dir()
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    print("ok runtime_activate_and_adopt_bundled")


def _runtime_zip(path: Path, rv: str, flavor: str, broken=False) -> None:
    from fotoscan import runtime
    with tempfile.TemporaryDirectory() as td:
        src = _make_runtime(Path(td) / "r", rv=rv, flavor=flavor)
        with zipfile.ZipFile(path, "w") as zf:
            for f in sorted(src.rglob("*")):
                if f.is_file() and not (broken and f.name == "x.dll"):
                    zf.write(f, Path("runtime") / f.relative_to(src))


def test_install_runtime_atomic_and_verified():
    from fotoscan import runtime
    rv = runtime.required_version()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        old_dir = os.environ.get("FOTOSCAN_RUNTIME_DIR")
        os.environ["FOTOSCAN_RUNTIME_DIR"] = str(td / "rt")
        orig_parts, orig_text = updater.fetch_parts, updater._fetch_text
        try:
            # runtime valido
            def good_parts(parts, dest, progress=None, cancelled=None):
                _runtime_zip(dest, rv, "cpu")
            updater.fetch_parts = good_parts
            entry = {"parts": [{"name": f"PhotoScan-runtime-{rv}-windows-cpu.zip", "url": "u", "size": 1}], "sums_url": None}
            final = updater.install_runtime(td / "cache", rv, "cpu", entry)
            assert final == td / "rt" / f"{rv}-cpu" and runtime.check_dir(final, rv)[0]
            assert not list((td / "rt").glob(".staging-*")), "appoggio rimasto"
            # un runtime incompleto (file mancante rispetto all'impronta) viene rifiutato e il vecchio resta
            def broken_parts(parts, dest, progress=None, cancelled=None):
                _runtime_zip(dest, rv, "cpu", broken=True)
            updater.fetch_parts = broken_parts
            before = (final / "site" / "torch" / "lib" / "x.dll").read_bytes()
            try:
                updater.install_runtime(td / "cache", rv, "cpu", entry)
            except updater.UpdateError as e:
                assert "incompleto" in str(e), e
            else:
                raise AssertionError("runtime incompleto accettato")
            assert (final / "site" / "torch" / "lib" / "x.dll").read_bytes() == before, "il runtime in uso non va toccato"
            assert not list((td / "rt").glob(".staging-*"))
            # tipo sbagliato (gpu al posto di cpu)
            def wrong_parts(parts, dest, progress=None, cancelled=None):
                _runtime_zip(dest, rv, "gpu")
            updater.fetch_parts = wrong_parts
            try:
                updater.install_runtime(td / "cache", rv, "cpu", entry)
            except updater.UpdateError as e:
                assert "non è utilizzabile" in str(e), e
            else:
                raise AssertionError("runtime del tipo sbagliato accettato")
        finally:
            updater.fetch_parts, updater._fetch_text = orig_parts, orig_text
            if old_dir is None:
                os.environ.pop("FOTOSCAN_RUNTIME_DIR", None)
            else:
                os.environ["FOTOSCAN_RUNTIME_DIR"] = old_dir
    print("ok install_runtime_atomic_and_verified")


def test_light_download_fetches_runtime_only_when_missing():
    """Aggiornamento normale: solo l'app leggera. Il runtime si scarica solo se la versione richiesta non e' gia' qui."""
    if sys.platform != "win32":
        print("skip light_download (solo Windows)")
        return
    from fotoscan import runtime
    rv = runtime.required_version()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        old_dir = os.environ.get("FOTOSCAN_RUNTIME_DIR")
        os.environ["FOTOSCAN_RUNTIME_DIR"] = str(td / "rt")
        orig = updater.fetch_parts, updater._fetch_text, updater._detect_windows_runtime
        calls = []

        def fake_parts(parts, dest, progress=None, cancelled=None):
            calls.append(parts[0]["name"])
            if "runtime" in parts[0]["name"]:
                _runtime_zip(dest, rv, "cpu")
            else:
                _exe_zip(dest, "0.2.0")

        updater.fetch_parts = fake_parts
        updater._fetch_text = lambda url: json.dumps({"version": "0.2.0", "runtime_version": rv})
        updater._detect_windows_runtime = lambda: "cpu"
        info = {"version": "0.2.0", "tag": "v0.2.0", "name": "x", "notes": "", "sums_url": None,
                "manifest_url": "https://x/release-manifest.json",
                "app_assets": [{"name": "PhotoScan-app-0.2.0-windows.zip", "url": "u", "size": 1}],
                "exe_assets": [{"name": "PhotoScan-0.2.0-windows-cpu.zip", "url": "full", "size": 1}],
                "runtime_assets": {rv: {"cpu": {"parts": [{"name": f"PhotoScan-runtime-{rv}-windows-cpu.zip", "url": "r", "size": 1}],
                                                "sums_url": None}}}}
        try:
            p = updater.download(td / "cache", info, exe=True)
            assert p["light"] and p["runtime_version"] == rv and p["kind"] == "exe"
            assert calls == [f"PhotoScan-runtime-{rv}-windows-cpu.zip", "PhotoScan-app-0.2.0-windows.zip"], calls
            assert runtime.check_dir(td / "rt" / f"{rv}-cpu", rv)[0]
            # secondo aggiornamento, stesso runtime: niente runtime da scaricare
            calls.clear()
            updater.discard_pending(td / "cache")
            p = updater.download(td / "cache", info, exe=True)
            assert calls == ["PhotoScan-app-0.2.0-windows.zip"], calls
            # runtime non pubblicato -> ripiego sul pacchetto completo
            calls.clear()
            updater.discard_pending(td / "cache")
            shutil_rmtree(td / "rt" / f"{rv}-cpu")
            p = updater.download(td / "cache", dict(info, runtime_assets={}), exe=True)
            assert calls == ["PhotoScan-0.2.0-windows-cpu.zip"] and not p.get("light"), (calls, p)
        finally:
            updater.fetch_parts, updater._fetch_text, updater._detect_windows_runtime = orig
            if old_dir is None:
                os.environ.pop("FOTOSCAN_RUNTIME_DIR", None)
            else:
                os.environ["FOTOSCAN_RUNTIME_DIR"] = old_dir
    print("ok light_download_fetches_runtime_only_when_missing")


# ------------------------------------------------------------------ avanzi, installazione automatica, AppImage
def test_discard_pending_removes_appimage_file_and_part_files():
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "cache"
        d = updater.updates_dir(cache) / "download"
        d.mkdir(parents=True)
        app = d / "PhotoScan-9-x86_64.AppImage"
        app.write_bytes(b"x")
        (d / "pkg.zip.part").write_bytes(b"y")
        updater._write_json(updater.updates_dir(cache) / "pending.json", {"version": "9", "kind": "appimage", "dir": str(app)})
        updater.discard_pending(cache)
        assert not app.exists() and not (d / "pkg.zip.part").exists()
    print("ok discard_pending_removes_appimage_file_and_part_files")


def test_auto_install_uses_force_without_approval():
    """Installazione automatica: il pacchetto scaricato si installa anche se non e' stato approvato a mano."""
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "cache"
        target = Path(td) / "bin" / "PhotoScan.AppImage"
        target.parent.mkdir()
        target.write_bytes(b"old")
        new = updater.updates_dir(cache) / "download" / "PhotoScan-0.2.0-x86_64.AppImage"
        new.parent.mkdir(parents=True)
        new.write_bytes(b"new")
        pend = {"version": "0.2.0", "kind": "appimage", "approved": False, "dir": str(new)}
        updater._write_json(updater.updates_dir(cache) / "pending.json", pend)
        old_env = os.environ.get("APPIMAGE")
        os.environ["APPIMAGE"] = str(target)
        try:
            assert updater.start_exe_update(cache, Path(td)) is False and target.read_bytes() == b"old"
            assert updater.start_exe_update(cache, Path(td), force=True) is True
        finally:
            if old_env is None:
                os.environ.pop("APPIMAGE", None)
            else:
                os.environ["APPIMAGE"] = old_env
        assert target.read_bytes() == b"new"
        assert not list(target.parent.glob("*.new")), "file d'appoggio rimasto"
    print("ok auto_install_uses_force_without_approval")


def test_appimage_install_failure_is_reported_not_silent():
    if sys.platform == "win32":
        print("skip appimage_install_failure (percorsi POSIX)")
        return
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "cache"
        new = updater.updates_dir(cache) / "download" / "x.AppImage"
        new.parent.mkdir(parents=True)
        new.write_bytes(b"new")
        updater._write_json(updater.updates_dir(cache) / "pending.json",
                            {"version": "0.2.0", "kind": "appimage", "approved": True, "dir": str(new)})
        ro = Path(td) / "ro"
        ro.mkdir()
        target = ro / "P.AppImage"
        target.write_bytes(b"old")
        ro.chmod(0o500)
        old_env = os.environ.get("APPIMAGE")
        os.environ["APPIMAGE"] = str(target)
        try:
            if os.access(ro, os.W_OK):
                print("skip (permessi non applicabili: root)")
                return
            assert updater.start_exe_update(cache, Path(td)) is False
            f = updater.read_last_failure(cache)
            assert f and "scrivibile" in f["message"], f
        finally:
            ro.chmod(0o700)
            if old_env is None:
                os.environ.pop("APPIMAGE", None)
            else:
                os.environ["APPIMAGE"] = old_env
    print("ok appimage_install_failure_is_reported_not_silent")


def test_exe_update_refuses_unwritable_program_folder():
    """Program Files e simili: messaggio chiaro invece di 20 secondi di tentativi e un «file in uso» falso."""
    if sys.platform != "win32":
        print("skip unwritable (solo Windows)")
        return
    cache = _TMP / "cache_exe_ro"
    app = _TMP / "app_exe_ro" / "PhotoScan"
    (app / "_internal").mkdir(parents=True)
    (app / "PhotoScan.exe").write_bytes(b"exe")
    staged = cache / "updates" / "staging" / "exe-ro"
    staged.mkdir(parents=True)
    (staged / "PhotoScan.exe").write_bytes(b"n")
    updater._write_json(cache / "updates" / "pending.json", {"version": "0.5.0", "dir": str(staged), "kind": "exe", "approved": True})
    orig = updater._writable_dir
    updater._writable_dir = lambda p: False
    try:
        assert updater.start_exe_update(cache, app, no_launch=True) is False
    finally:
        updater._writable_dir = orig
    f = updater.read_last_failure(cache)
    assert f and "non è modificabile" in f["message"], f
    print("ok exe_update_refuses_unwritable_program_folder")


def test_exe_helper_blocked_is_reported():
    """PowerShell che esce subito senza scrivere il log (criteri di esecuzione): l'utente vede un errore, non il silenzio."""
    if sys.platform != "win32":
        print("skip helper_blocked (solo Windows)")
        return
    cache = _TMP / "cache_exe_blocked"
    app = _TMP / "app_exe_blocked" / "PhotoScan"
    (app / "_internal").mkdir(parents=True)
    (app / "PhotoScan.exe").write_bytes(b"exe")
    staged = cache / "updates" / "staging" / "exe-b"
    staged.mkdir(parents=True)
    (staged / "PhotoScan.exe").write_bytes(b"n")
    updater._write_json(cache / "updates" / "pending.json", {"version": "0.5.0", "dir": str(staged), "kind": "exe", "approved": True})
    orig_popen = updater.subprocess.Popen
    updater.subprocess.Popen = lambda cmd, **kw: orig_popen([sys.executable, "-c", "import sys; sys.exit(1)"], **{
        k: v for k, v in kw.items() if k in ("stdin", "stdout", "stderr", "creationflags", "close_fds")})
    try:
        assert updater.start_exe_update(cache, app, no_launch=True) is False
    finally:
        updater.subprocess.Popen = orig_popen
    f = updater.read_last_failure(cache)
    assert f and "impedito" in f["message"], f
    assert updater.read_pending(cache) is None
    print("ok exe_helper_blocked_is_reported")


# ------------------------------------------------------------------ altre versioni, downgrade
def test_list_releases_and_release_info():
    releases = [
        {**_release("v0.1.11"), "assets": [
            {"name": "PhotoScan-Setup-0.1.11.exe", "browser_download_url": "https://x/Setup-0.1.11.exe", "size": 1},
            {"name": "PhotoScan-0.1.11-windows.zip", "browser_download_url": "https://x/full", "size": 2}]},
        {**_release("v0.1.10"), "assets": [{"name": "PhotoScan-Setup.exe", "browser_download_url": "https://x/Setup.exe", "size": 1}]},
        {**_release("v0.2.0-beta.1", prerelease=True)},
        {**_release("v0.0.9", draft=True)},
    ]
    orig = updater.fetch_releases
    updater.fetch_releases = lambda repo: releases
    try:
        stable = updater.list_releases(_TMP / "cache_lr", prerelease=False)
        assert [r["version"] for r in stable] == ["0.1.11", "0.1.10"], stable
        assert stable[0]["setup_url"].endswith("Setup-0.1.11.exe") and stable[1]["setup_url"].endswith("Setup.exe")
        withpre = updater.list_releases(_TMP / "cache_lr", prerelease=True)
        assert [r["version"] for r in withpre] == ["0.2.0-beta.1", "0.1.11", "0.1.10"]
        info = updater.release_info("0.1.10")
        assert info["version"] == "0.1.10"
        try:
            updater.release_info("9.9.8")
        except updater.UpdateError as e:
            assert "non è tra le Release" in str(e)
        else:
            raise AssertionError("versione inesistente accettata")
    finally:
        updater.fetch_releases = orig
    print("ok list_releases_and_release_info")


def test_backup_catalog_and_downgrade_flow():
    import sqlite3
    from fotoscan.server import jobs, routes_update
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        db = td / "catalogo.db"
        conn = sqlite3.connect(db)
        conn.execute("create table t(x)")
        conn.execute("insert into t values (42)")
        conn.commit()
        conn.close()
        bak = updater.backup_catalog(td / "cache", db, "0.1.11")
        assert bak and bak.name == "backup-0.1.11.db"
        c2 = sqlite3.connect(bak)
        try:
            assert c2.execute("select x from t").fetchone() == (42,)
        finally:
            c2.close()
        assert updater.backup_catalog(td / "cache", td / "manca.db", "1") is None

    # flusso API: versione piu' vecchia = conferma obbligatoria, backup del catalogo, pending con downgrade
    older = "0.0.1"
    orig = (updater.fetch_releases, updater.fetch_zip, updater.is_git_checkout)
    updater.fetch_releases = lambda repo: [_release(f"v{older}"), _release(f"v{NEW}")]
    updater.fetch_zip = _fake_fetch_zip(_zip_bytes(version=older))
    updater.is_git_checkout = lambda root: False
    try:
        from fastapi import HTTPException
        try:
            routes_update.update_install_version({"version": older})
        except HTTPException as e:
            assert e.status_code == 409, e.detail
        else:
            raise AssertionError("downgrade senza conferma accettato")
        try:
            routes_update.update_install_version({"version": __version__})
        except HTTPException as e:
            assert e.status_code == 400
        res = routes_update.update_install_version({"version": older, "confirm_downgrade": True})
        assert res["downgrade"] is True
        end = time.time() + 20
        while jobs.get_job(res["job_id"]).state == "running" and time.time() < end:
            time.sleep(0.05)
        assert jobs.get_job(res["job_id"]).state == "done", jobs.get_job(res["job_id"]).error
        st = routes_update.update_status()
        assert st["pending"]["version"] == older and st["pending"]["downgrade"] is True, st["pending"]
        rels = routes_update.update_releases()
        assert {r["version"] for r in rels["releases"]} == {older, NEW}
        routes_update.update_discard()
    finally:
        updater.fetch_releases, updater.fetch_zip, updater.is_git_checkout = orig
    print("ok backup_catalog_and_downgrade_flow")


def test_uninstall_endpoint_requires_confirm_and_never_touches_photos():
    from fastapi import HTTPException
    from fotoscan import uninstall
    from fotoscan.server import routes_update
    try:
        routes_update.update_uninstall({"delete_data": True})
    except HTTPException as e:
        assert e.status_code == 400
    else:
        raise AssertionError("disinstallazione senza conferma accettata")
    try:
        routes_update.update_uninstall({"confirm": True})     # non frozen: nulla da disinstallare
    except HTTPException as e:
        assert e.status_code == 409
    else:
        raise AssertionError("disinstallazione di una copia non installata accettata")

    # Linux: richiesta + rimozione del collegamento nel menu; i dati restano se non richiesti
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data = td / "share"
        old = {k: os.environ.get(k) for k in ("XDG_DATA_HOME",)}
        os.environ["XDG_DATA_HOME"] = str(data)
        try:
            (data / "applications").mkdir(parents=True)
            (data / "icons").mkdir()
            (data / "applications" / "photoscan.desktop").write_text("x", encoding="utf-8")
            (data / "icons" / "photoscan.png").write_text("x", encoding="utf-8")
            root = data / "PhotoScan"
            (root / "cache").mkdir(parents=True)
            (root / "catalogo.db").write_text("db", encoding="utf-8")
            photos = td / "Foto"
            photos.mkdir()
            (photos / "a.jpg").write_text("foto", encoding="utf-8")
            removed = uninstall.linux_cleanup(root, delete_data=False)
            assert any(r.endswith("photoscan.desktop") for r in removed) and (root / "catalogo.db").exists()
            removed = uninstall.linux_cleanup(root, delete_data=True)
            assert not root.exists() and (photos / "a.jpg").exists(), "le foto non si toccano"
            # una cartella che non e' «PhotoScan» non si cancella mai
            other = data / "Altro"
            other.mkdir()
            (other / "f").write_text("1", encoding="utf-8")
            uninstall.linux_cleanup(other, delete_data=True)
            assert (other / "f").exists()
            assert not uninstall.is_data_root(Path.home()) and not uninstall.is_data_root(Path(Path.home().anchor))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    # richiesta scritta e letta
    with tempfile.TemporaryDirectory() as td:
        uninstall.request(td, delete_data=True)
        assert uninstall.read_request(td)["delete_data"] is True
    print("ok uninstall_endpoint_requires_confirm_and_never_touches_photos")


def test_desktop_helpers_state_ctime_and_lock():
    """Voci 4 e 9: il pid dello stato vale solo se e' ancora lo stesso processo; il blocco di avvio e' esclusivo."""
    from fotoscan import desktop
    me = os.getpid()
    ct = desktop._proc_ctime(me)
    assert ct, "istante di creazione del processo corrente"
    assert desktop._state_pid_valid({"pid": me, "ctime": ct})
    assert not desktop._state_pid_valid({"pid": me, "ctime": ct + 1}), "pid riciclato: ctime diverso"
    assert not desktop._state_pid_valid({"pid": me}), "stato vecchio senza ctime: non si killa nulla"
    assert desktop._proc_ctime(2 ** 22 + 12345) is None
    with tempfile.TemporaryDirectory() as td:
        orig = desktop.LOCK, desktop.CACHE
        desktop.CACHE = Path(td)
        desktop.LOCK = Path(td) / "desktop.lock"
        try:
            with desktop._StartupLock(timeout=5):
                # un secondo tentativo, da un altro processo, non ottiene il blocco finche' il primo lo tiene
                code = ("import sys; sys.path.insert(0, %r); from fotoscan import desktop; from pathlib import Path; import time;"
                        "desktop.CACHE = Path(%r); desktop.LOCK = Path(%r);"
                        "t = time.time()\ntry:\n  l = desktop._StartupLock(timeout=1.5).__enter__()\nexcept Exception as e:\n  print('err', e)\n"
                        "print(round(time.time() - t, 1))") % (str(ROOT), td, str(Path(td) / "desktop.lock"))
                out = __import__("subprocess").run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60).stdout.strip()
                assert float(out.splitlines()[-1]) >= 1.4, f"il secondo avvio non ha aspettato: {out!r}"
        finally:
            desktop.LOCK, desktop.CACHE = orig
    # porta: usa l'ultima, poi la preferita; esclusioni rispettate
    with tempfile.TemporaryDirectory() as td:
        orig_lp = desktop.LAST_PORT
        desktop.LAST_PORT = Path(td) / "p.txt"
        try:
            p1 = desktop.free_port()
            assert isinstance(p1, int) and p1 > 0
            assert desktop.free_port(exclude=(p1,)) != p1
        finally:
            desktop.LAST_PORT = orig_lp
    print("ok desktop_helpers_state_ctime_and_lock")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"tutti i {len(tests)} test passati")
