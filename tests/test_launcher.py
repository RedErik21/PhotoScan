"""Test del launcher PhotoScan-Setup: scelta runtime/modello, parti della Release, ricomposizione, config."""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "launcher"))

import setup_photoscan as sp  # noqa: E402


def _hw(gpu=None, vram=0, ram=16.0):
    return sp.Hardware(cores=8, ram_gb=ram, gpu_name=gpu, vram_mb=vram, free_disk_gb=100.0)


def test_recommend_tiers():
    assert sp.recommend(_hw("RTX", 8000, 32)) == ("gpu", "so400m")
    assert sp.recommend(_hw("RTX", 6000, 16)) == ("gpu", "so400m")
    assert sp.recommend(_hw("GTX", 4000, 8)) == ("gpu", "large")
    assert sp.recommend(_hw("GTX", 2000, 16)) == ("cpu", "base")  # VRAM piccola: meglio CPU + base
    assert sp.recommend(_hw(None, 0, 32)) == ("cpu", "base")      # nessuna NVIDIA
    assert sp.recommend(_hw("RTX", 8000, 8)) == ("gpu", "large")  # RAM bassa: niente SO400M


def test_space_warning():
    hw = sp.Hardware(4, 16, None, 0, free_disk_gb=2.0)
    assert sp.space_warning(hw, "cpu", "base") is not None
    assert sp.space_warning(_hw(), "cpu", "base") is None


def test_package_parts_selects_runtime_and_order():
    assets = [
        {"name": "PhotoScan-0.1.4-windows.zip.002", "browser_download_url": "u2", "size": 2},
        {"name": "PhotoScan-0.1.4-windows.zip.001", "browser_download_url": "u1", "size": 1},
        {"name": "PhotoScan-0.1.4-windows-cpu.zip", "browser_download_url": "c", "size": 9},
        {"name": "PhotoScan-0.1.3-windows.zip", "browser_download_url": "old", "size": 9},
        {"name": "PhotoScan-0.1.4-windows.zip.checksums", "browser_download_url": "x", "size": 1},
    ]
    gpu = sp.package_parts(assets, "0.1.4", "gpu")
    assert [p["name"] for p in gpu] == ["PhotoScan-0.1.4-windows.zip.001", "PhotoScan-0.1.4-windows.zip.002"]
    cpu = sp.package_parts(assets, "0.1.4", "cpu")
    assert [p["name"] for p in cpu] == ["PhotoScan-0.1.4-windows-cpu.zip"]


def test_download_reassembles_parts_and_extracts():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src"
        src.mkdir()
        # zip di prova, poi spezzato in due parti come farà la build
        zsrc = tmp / "full.zip"
        with zipfile.ZipFile(zsrc, "w") as z:
            z.writestr("PhotoScan/PhotoScan.exe", b"MZ" + b"x" * 5000)
            z.writestr("PhotoScan/_internal/lib.dll", b"y" * 3000)
        data = zsrc.read_bytes()
        half = len(data) // 2
        parts = []
        for i, chunk in enumerate([data[:half], data[half:]], 1):
            p = src / f"PhotoScan-9.9.9-windows.zip.{i:03d}"
            p.write_bytes(chunk)
            parts.append({"name": p.name, "url": p.as_uri(), "size": len(chunk)})
        got = []
        zip_path = sp.download_parts(parts, tmp / "dl", lambda f, t: got.append(f))
        assert zip_path.read_bytes() == data
        assert not any((tmp / "dl" / p["name"]).exists() for p in parts)  # parti rimosse
        app = tmp / "app"
        sp.extract(zip_path, app)
        assert (app / "PhotoScan" / "PhotoScan.exe").exists()
        # reinstallazione: sostituisce la versione precedente
        sp.extract(zip_path, app)
        assert (app / "PhotoScan" / "_internal" / "lib.dll").exists()


def test_download_detects_truncated_part():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        p = tmp / "PhotoScan-9.9.9-windows.zip"
        p.write_bytes(b"abc")
        parts = [{"name": p.name, "url": p.as_uri(), "size": 10}]
        try:
            sp.download_parts(parts, tmp / "dl")
        except RuntimeError as e:
            assert "incompleta" in str(e)
        else:
            raise AssertionError("parte incompleta non rilevata")


def test_write_config_keeps_other_keys_and_sets_model():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.json"
        cfg.write_text(json.dumps({"keep": 1, "model_name": "old"}), encoding="utf-8")
        sp.write_config(cfg, "google/siglip2-base-patch16-224")
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data == {"keep": 1, "model_name": "google/siglip2-base-patch16-224"}


def test_install_marker_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert sp.installed_state(root) is None
        sp.write_install_marker(root, "0.1.4", "gpu", "so400m")
        assert sp.installed_state(root) == {"version": "0.1.4", "runtime": "gpu", "model": "so400m"}


# ------------------------------------------------------------------ nomi, parti complete, versioni
def test_incomplete_parts_and_new_names():
    mk = lambda name: {"name": name, "browser_download_url": "u-" + name, "size": 1}
    assert sp.package_parts([mk("PhotoScan-1.0-windows.zip.001"), mk("PhotoScan-1.0-windows.zip.003")], "1.0", "gpu") == []
    assert [p["name"] for p in sp.app_parts([mk("PhotoScan-app-1.0-windows.zip.002"), mk("PhotoScan-app-1.0-windows.zip.001")], "1.0")] == [
        "PhotoScan-app-1.0-windows.zip.001", "PhotoScan-app-1.0-windows.zip.002"]
    # i pacchetti nuovi non si confondono con quelli completi
    assert sp.package_parts([mk("PhotoScan-app-1.0-windows.zip"), mk("PhotoScan-runtime-1-windows.zip")], "1.0", "gpu") == []


def test_version_key_and_pick_release():
    assert sp.version_key("0.1.10") > sp.version_key("0.1.9")
    assert sp.version_key("1.0.0") > sp.version_key("1.0.0-rc.1") > sp.version_key("1.0.0-beta.2")
    rels = [{"tag_name": "v0.1.9"}, {"tag_name": "v0.1.10"}, {"tag_name": "v0.2.0-beta.1", "prerelease": True}]
    assert sp.pick_release(rels)["tag_name"] == "v0.1.10", "la piu' recente STABILE, numeri non testo"
    assert sp.pick_release(rels, "0.1.9")["tag_name"] == "v0.1.9"
    assert sp.pick_release(rels, "0.2.0-beta.1")["tag_name"] == "v0.2.0-beta.1"
    try:
        sp.pick_release(rels, "7.7.7")
    except RuntimeError as e:
        assert "non è tra le Release" in str(e)
    else:
        raise AssertionError("versione inesistente accettata")


def test_runtime_index_and_plan():
    def asset(name):
        return {"name": name, "browser_download_url": "https://x/" + name, "size": 5}
    new = {"tag_name": "v0.1.12", "assets": [asset("SHA256SUMS"), asset("release-manifest.json"), asset("PhotoScan-app-0.1.12-windows.zip"),
                                              asset("PhotoScan-0.1.12-windows.zip.001"), asset("PhotoScan-0.1.12-windows.zip.002")]}
    old = {"tag_name": "v0.1.11", "assets": [asset("SHA256SUMS"), asset("PhotoScan-runtime-1-windows.zip.001"), asset("PhotoScan-runtime-1-windows.zip.002")]}
    idx = sp.runtime_index([new, old])
    assert list(idx["1"]) == ["gpu"] and len(idx["1"]["gpu"]["parts"]) == 2 and idx["1"]["gpu"]["sums_url"].endswith("SHA256SUMS")
    orig = sp._get
    sp._get = lambda url, accept="", timeout=30: json.dumps({"runtime_version": "1"}).encode()
    try:
        plan = sp.plan_install(new, [new, old], "gpu")
        assert plan["kind"] == "light" and plan["runtime_version"] == "1" and plan["app"]
        # runtime cpu mai pubblicato -> pacchetto completo (che per cpu non c'e': errore chiaro)
        try:
            sp.plan_install(new, [new, old], "cpu")
        except RuntimeError as e:
            assert "«cpu»" in str(e)
        else:
            raise AssertionError("manca il pacchetto cpu e non e' stato segnalato")
        # Release vecchia senza app leggera: pacchetto completo
        plan = sp.plan_install(dict(new, assets=[a for a in new["assets"] if "app" not in a["name"]]), [new, old], "gpu")
        assert plan["kind"] == "full"
    finally:
        sp._get = orig


# ------------------------------------------------------------------ download e estrazione sicuri
def test_download_checks_sha256_and_retries_resume():
    import hashlib
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data = b"abc" * 4000
        f = tmp / "PhotoScan-9.9.9-windows.zip"
        f.write_bytes(data)
        good = [{"name": f.name, "url": f.as_uri(), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]
        assert sp.download_parts(good, tmp / "dl").read_bytes() == data
        bad = [dict(good[0], sha256="0" * 64)]
        try:
            sp.download_parts(bad, tmp / "dl2")
        except RuntimeError as e:
            assert "SHA-256" in str(e)
        else:
            raise AssertionError("impronta sbagliata non rilevata")
        # sums: nomi mancanti = errore, assenza di SHA256SUMS = ammessa
        assert sp.attach_sums([{"name": "a", "url": "u", "size": 1}], None)[0].get("sha256") is None
        try:
            sp.attach_sums([{"name": "a", "url": "u", "size": 1}], {"b": "0" * 64})
        except RuntimeError as e:
            assert "non verificabile" in str(e)
        else:
            raise AssertionError("file fuori da SHA256SUMS accettato")


def test_extract_keeps_old_app_when_folder_is_in_use():
    """Setup sopra a PhotoScan aperto: la vecchia cartella non va distrutta a meta' (voce 3)."""
    if sys.platform != "win32":
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        zsrc = tmp / "new.zip"
        with zipfile.ZipFile(zsrc, "w") as z:
            z.writestr("PhotoScan/PhotoScan.exe", b"nuovo")
        app = tmp / "app"
        (app / "PhotoScan").mkdir(parents=True)
        (app / "PhotoScan" / "PhotoScan.exe").write_bytes(b"vecchio")
        (app / "PhotoScan" / "altro.dll").write_bytes(b"dll")
        with open(app / "PhotoScan" / "PhotoScan.exe", "rb"):        # file in uso: la cartella non si puo' spostare
            try:
                sp.extract(zsrc, app)
            except RuntimeError as e:
                assert "Chiudi PhotoScan" in str(e), e
            else:
                raise AssertionError("sostituzione riuscita con un file in uso?")
        assert (app / "PhotoScan" / "PhotoScan.exe").read_bytes() == b"vecchio"
        assert (app / "PhotoScan" / "altro.dll").exists(), "la vecchia installazione deve restare intera"
        assert not (tmp / "app.new").exists(), "estrazione parziale rimasta"
        sp.extract(zsrc, app)                                         # senza blocchi: sostituisce
        assert (app / "PhotoScan" / "PhotoScan.exe").read_bytes() == b"nuovo" and not (tmp / "app.old").exists()


def test_extract_rejects_path_traversal():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        z = tmp / "evil.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("../fuori.txt", b"x")
        try:
            sp.extract(z, tmp / "app")
        except RuntimeError as e:
            assert "sospetto" in str(e)
        else:
            raise AssertionError("zip con percorso fuori cartella accettato")
        assert not (tmp / "fuori.txt").exists()


def test_extract_runtime_atomic_and_complete():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        good = tmp / "good.zip"
        with zipfile.ZipFile(good, "w") as z:
            z.writestr("runtime/site/torch/__init__.py", b"x")
            z.writestr("runtime/runtime.json", b"{}")
        dest = tmp / "runtime" / "1-gpu"
        dest.parent.mkdir()
        sp.extract_runtime(good, dest)
        assert (dest / "site" / "torch" / "__init__.py").is_file() and (dest / "runtime.json").is_file()
        assert not list(dest.parent.glob(".staging-*"))
        bad = tmp / "bad.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("runtime/site/torch/__init__.py", b"x")             # manca runtime.json
        try:
            sp.extract_runtime(bad, dest)
        except RuntimeError as e:
            assert "incompleto" in str(e)
        else:
            raise AssertionError("runtime incompleto accettato")
        assert (dest / "runtime.json").is_file(), "il runtime buono resta"
        assert not list(dest.parent.glob(".staging-*"))


# ------------------------------------------------------------------ collegamenti con apostrofi e accenti (voce 18)
def test_shortcut_with_apostrophe_and_accents():
    if sys.platform != "win32":
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        weird = tmp / "D'Angelo è Andrea Stella"
        weird.mkdir()
        exe = weird / "PhotoScan.exe"
        exe.write_bytes(b"MZ")
        lnk = weird / "PhotoScan.lnk"
        assert sp.create_shortcut(lnk, exe) is True and lnk.exists()
        assert sp.create_shortcut(weird / "no" / "x.lnk", exe) is False, "errore reale -> False, non un successo finto"


# ------------------------------------------------------------------ App installate (registro finto)
class FakeKey:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeReg:
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ, REG_DWORD, KEY_WRITE = 1, 4, 0x20006

    def __init__(self):
        self.keys: dict[str, dict] = {}

    def CreateKeyEx(self, hive, path, reserved, access):
        assert hive == "HKCU", "solo HKCU: nessun amministratore"
        self.keys.setdefault(path, {})
        return FakeKey(self, path)

    def SetValueEx(self, key, name, reserved, typ, value):
        key.store.keys[key.path][name] = (typ, value)

    def DeleteKey(self, hive, path):
        if path not in self.keys:
            raise FileNotFoundError(path)
        del self.keys[path]


def test_register_and_unregister_uninstall_entry():
    import uninstall_photoscan as up
    reg = FakeReg()
    root = Path("C:/Users/x/AppData/Local/PhotoScan")
    sp.register_uninstall(root, "0.1.11", root / "app" / "PhotoScan" / "PhotoScan.exe",
                          '"C:/u/PhotoScan-Uninstall.exe" --uninstall', 3 * 1024 * 1024 * 1024, reg=reg)
    vals = reg.keys[sp.UNINSTALL_KEY]
    assert sp.UNINSTALL_KEY == up.UNINSTALL_KEY and sp.UNINSTALL_KEY.startswith("Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall")
    for name in ("DisplayName", "DisplayVersion", "Publisher", "DisplayIcon", "InstallLocation", "UninstallString",
                 "EstimatedSize", "NoModify", "NoRepair"):
        assert name in vals, name
    assert vals["DisplayVersion"][1] == "0.1.11" and vals["NoModify"] == (4, 1) and vals["EstimatedSize"][0] == 4
    assert vals["UninstallString"][1].endswith("--uninstall")
    assert up.unregister_uninstall(reg) is True and sp.UNINSTALL_KEY not in reg.keys
    assert up.unregister_uninstall(reg) is True, "gia' assente: non e' un errore"


# ------------------------------------------------------------------ disinstallazione
def _fake_install(base: Path) -> tuple[Path, Path]:
    """Cartella dati finta + una cartella di foto FUORI e una DENTRO (nel caso peggiore)."""
    root = base / "Local" / "PhotoScan"
    (root / "app" / "PhotoScan").mkdir(parents=True)
    (root / "app" / "PhotoScan" / "PhotoScan.exe").write_bytes(b"x" * 100)
    (root / "runtime" / "1-gpu" / "site").mkdir(parents=True)
    (root / "runtime" / "1-gpu" / "site" / "torch.py").write_bytes(b"y" * 200)
    (root / "uninstall").mkdir()
    (root / "uninstall" / "PhotoScan-Uninstall.exe").write_bytes(b"u")
    (root / "install.json").write_text("{}", encoding="utf-8")
    (root / "cache" / "thumbs").mkdir(parents=True)
    (root / "cache" / "thumbs" / "a.jpg").write_bytes(b"t" * 50)
    (root / "catalogo.db").write_bytes(b"db" * 20)
    photos_out = base / "Foto"
    photos_out.mkdir()
    (photos_out / "ricordo.jpg").write_bytes(b"foto")
    (root / "config.json").write_text(json.dumps({"source_dir": str(photos_out)}), encoding="utf-8")
    return root, photos_out


def test_uninstall_keeps_data_by_default_and_never_touches_photos():
    import uninstall_photoscan as up
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root, photos = _fake_install(tmp)
        lnk = tmp / "Desktop" / "PhotoScan.lnk"
        lnk.parent.mkdir()
        lnk.write_bytes(b"lnk")
        reg = FakeReg()
        reg.keys[up.UNINSTALL_KEY] = {"DisplayName": "PhotoScan"}
        plan = up.build_plan(root, delete_data=False)
        labels = [i["label"] for i in plan["items"]]
        assert "Programma" in labels and not any(l.startswith("Dati:") for l in labels), "senza richiesta i dati non sono nel piano"
        assert plan["total"] >= 300
        res = up.uninstall(root, delete_data=False, silent=True, reg=reg, shortcuts=[lnk])
        assert res["done"], res
        assert not (root / "app").exists() and not (root / "runtime").exists() and not (root / "uninstall").exists()
        assert not lnk.exists() and up.UNINSTALL_KEY not in reg.keys
        assert (root / "catalogo.db").exists() and (root / "cache" / "thumbs" / "a.jpg").exists() and (root / "config.json").exists(), \
            "i dati si conservano di default"
        assert (photos / "ricordo.jpg").read_bytes() == b"foto"


def test_uninstall_delete_data_skips_photo_folders_inside_root():
    import uninstall_photoscan as up
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root, photos = _fake_install(tmp)
        inside = root / "MieFoto"
        inside.mkdir()
        (inside / "a.jpg").write_bytes(b"1")
        (root / "config.json").write_text(json.dumps({"source_dir": str(inside), "output_dir": str(photos)}), encoding="utf-8")
        plan = up.build_plan(root, delete_data=True)
        assert str(inside) in plan["skipped"], "una cartella foto dentro la cartella dati non si cancella"
        res = up.uninstall(root, delete_data=True, silent=True, reg=FakeReg(), shortcuts=[])
        assert res["done"] and res["delete_data"]
        assert not (root / "cache").exists() and not (root / "catalogo.db").exists() and not (root / "app").exists()
        assert (inside / "a.jpg").exists() and (photos / "ricordo.jpg").exists(), "le foto non si toccano"
        assert root.exists(), "la cartella resta se contiene ancora foto"


def test_uninstall_refuses_unsafe_roots():
    import uninstall_photoscan as up
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        other = tmp / "Documenti"
        other.mkdir()
        (other / "f.txt").write_text("1", encoding="utf-8")
        res = up.uninstall(other, delete_data=True, silent=True, reg=FakeReg(), shortcuts=[])
        assert not res["done"] and (other / "f.txt").exists()
        assert not up.is_safe_root(Path.home()) and not up.is_safe_root(Path(Path.home().anchor))
        assert up.is_safe_root(tmp / "PhotoScan")


def test_silent_install_flow_light_and_full():
    """install() con Release finte: app leggera + runtime (scaricato una volta), poi pacchetto completo di una Release
    vecchia scelta con --version. Nessuna scrittura nel registro vero."""
    import hashlib
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src"
        src.mkdir()

        def make(name, entries):
            p = src / name
            with zipfile.ZipFile(p, "w") as z:
                for k, v in entries.items():
                    z.writestr(k, v)
            return p

        app_zip = make("PhotoScan-app-0.2.0-windows.zip", {"PhotoScan/PhotoScan.exe": b"app", "PhotoScan/_internal/x.dll": b"d"})
        rt_zip = make("PhotoScan-runtime-1-windows.zip", {"runtime/runtime.json": b"{}", "runtime/site/torch/__init__.py": b"t"})
        old_zip = make("PhotoScan-0.1.0-windows.zip", {"PhotoScan/PhotoScan.exe": b"old", "PhotoScan/_internal/x.dll": b"o"})
        manifest = src / "release-manifest.json"
        manifest.write_text(json.dumps({"runtime_version": "1"}), encoding="utf-8")
        files = [app_zip, rt_zip, old_zip, manifest]
        sums = src / "SHA256SUMS"
        sums.write_text("".join(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}\n" for f in files), encoding="utf-8")
        asset = lambda f: {"name": f.name, "browser_download_url": f.as_uri(), "size": f.stat().st_size}
        rel_new = {"tag_name": "v0.2.0", "assets": [asset(sums), asset(manifest), asset(app_zip), asset(rt_zip)]}
        rel_old = {"tag_name": "v0.1.0", "assets": [asset(sums), asset(old_zip)]}
        saved = {k: getattr(sp, k) for k in ("list_releases", "create_desktop_shortcut", "install_uninstaller", "register_uninstall",
                                             "running_app_processes")}
        calls = []
        sp.list_releases = lambda repo=sp.REPO: [rel_new, rel_old]
        sp.create_desktop_shortcut = lambda exe: [tmp / "x.lnk"]
        sp.install_uninstaller = lambda root: '"u.exe" --uninstall'
        sp.register_uninstall = lambda *a, **k: calls.append(a)
        sp.running_app_processes = lambda app: []
        orig_get = sp._get
        sp._get = lambda url, accept="", timeout=30: Path(urllib_path(url)).read_bytes()
        try:
            root = tmp / "PhotoScan"
            exe = sp.install(root, "gpu", "base")
            assert exe == root / "app" / "PhotoScan" / "PhotoScan.exe" and exe.read_bytes() == b"app"
            assert (root / "runtime" / "1-gpu" / "runtime.json").is_file()
            assert sp.installed_state(root)["version"] == "0.2.0" and calls, "voce in «App installate» registrata"
            assert not (root / "download").exists(), "download temporanei rimasti"
            # versione scelta, piu' vecchia: pacchetto completo (Release senza app leggera)
            exe = sp.install(root, "gpu", "base", version="0.1.0")
            assert exe.read_bytes() == b"old" and sp.installed_state(root)["version"] == "0.1.0"
            # SHA256SUMS che non corrisponde -> installazione rifiutata
            sums.write_text(f"{'0' * 64}  {old_zip.name}\n", encoding="utf-8")
            try:
                sp.install(tmp / "Altro" / "PhotoScan", "gpu", "base", version="0.1.0")
            except RuntimeError as e:
                assert "SHA-256" in str(e), e
            else:
                raise AssertionError("pacchetto con impronta sbagliata installato")
            assert not (tmp / "Altro" / "PhotoScan" / "app").exists()
        finally:
            for k, v in saved.items():
                setattr(sp, k, v)
            sp._get = orig_get


def urllib_path(url: str) -> str:
    from urllib.parse import unquote, urlparse
    from urllib.request import url2pathname
    return url2pathname(unquote(urlparse(url).path))


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"tutti i {len(tests)} test passati")
