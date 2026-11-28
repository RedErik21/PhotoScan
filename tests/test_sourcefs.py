"""Sorgente di rete (fotoscan/sourcefs.py): riallineamento del catalogo e NAS irraggiungibile.

Nessun NAS reale: le cartelle sono temporanee e la rete viene simulata.
Esecuzione: `python -m pytest tests\\test_sourcefs.py` oppure `python tests\\test_sourcefs.py`.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="fs_sourcefs_test_"))
os.environ["FOTOSCAN_DB"] = str(_TMP / "catalogo.db")  # mai il catalogo vero
os.environ["FOTOSCAN_CACHE"] = str(_TMP / "cache")

from fotoscan import db as dbm  # noqa: E402
from fotoscan import scanner_metadata as sm  # noqa: E402
from fotoscan import sourcefs  # noqa: E402
from fotoscan.config import Config  # noqa: E402


def _make_tree(root: Path, names: list[str]) -> None:
    for i, n in enumerate(names):
        p = root / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(bytes([i + 1]) * (100 + i))


def _connect(name: str):
    return dbm.connect(str(_TMP / f"{name}.db"))


def test_norm_root_keeps_drive_root_and_strips_trailing_sep():
    base = os.path.abspath(os.sep)
    assert sourcefs.norm_root(base) == base
    assert sourcefs.norm_root(os.path.join(str(_TMP), "x") + os.sep) == os.path.join(str(_TMP), "x")


def test_local_path_is_not_network():
    assert sourcefs.is_network(str(_TMP)) is False


def test_rebase_after_root_rename_avoids_reindex():
    names = ["2020/a.jpg", "2020/b.jpg", "c.jpg"]
    old = _TMP / "vecchia" / "foto"
    new = _TMP / "nuova" / "foto"
    _make_tree(old, names)
    conn = _connect("rebase")
    cfg = Config(source_dir=str(old))
    assert sm.scan_into_db(conn, cfg) == len(names)

    # stessa cartella, altro nome di accesso (copia con mtime conservato)
    for n in names:
        (new / n).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old / n, new / n)
    cfg2 = Config(source_dir=str(new))
    assert sm.scan_into_db(conn, cfg2) == 0  # niente da rielaborare: percorsi riallineati
    paths = [r["path"] for r in conn.execute("SELECT path FROM files")]
    assert len(paths) == len(names)
    assert all(p.startswith(sourcefs.norm_root(str(new))) for p in paths), paths
    import json
    rb = json.loads(dbm.get_meta(conn, "last_rebase") or "{}")
    assert rb.get("moved") == len(names)
    assert rb.get("from") == sourcefs.norm_root(str(old))
    assert rb.get("to") == sourcefs.norm_root(str(new))
    conn.close()


def test_rebase_refused_for_different_folder():
    old = _TMP / "diversa_a"
    other = _TMP / "diversa_b"
    _make_tree(old, ["x.jpg", "y.jpg"])
    _make_tree(other, ["altro.jpg"])
    conn = _connect("refuse")
    sm.scan_into_db(conn, Config(source_dir=str(old)))
    assert sourcefs.rebase_catalog(conn, str(old), str(other)) == 0
    paths = [r["path"] for r in conn.execute("SELECT path FROM files")]
    assert all(p.startswith(sourcefs.norm_root(str(old))) for p in paths)
    conn.close()


def test_network_scan_fails_cleanly_when_share_unreachable():
    src = _TMP / "nas_giu"
    _make_tree(src, ["a.jpg"])
    conn = _connect("nas_down")
    with mock.patch.object(sourcefs, "is_network", return_value=True), \
         mock.patch.object(sourcefs, "reachable", return_value=False):
        try:
            sm.scan_into_db(conn, Config(source_dir=str(src)))
        except FileNotFoundError as e:
            assert "non risponde" in str(e)
        else:
            raise AssertionError("doveva fallire: NAS irraggiungibile")
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0  # catalogo intatto
    conn.close()


def test_permission_errors_do_not_fail_network_scan():
    src = _TMP / "nas_permessi"
    _make_tree(src, ["a.jpg"])
    denied = OSError(13, "Permesso negato", str(src / "chiusa"))
    conn = _connect("perm")
    with mock.patch.object(sourcefs, "is_network", return_value=True), \
         mock.patch.object(sourcefs, "reachable", return_value=True), \
         mock.patch.object(sm.os, "walk", side_effect=lambda top, onerror=None: _walk_with_error(top, onerror, denied)):
        assert sm.scan_into_db(conn, Config(source_dir=str(src))) == 1
    conn.close()


_REAL_WALK = os.walk


def _walk_with_error(top, onerror, err):
    if onerror is not None:
        onerror(err)
    yield from _REAL_WALK(top)


def test_rebase_refuses_when_new_root_has_only_part_of_the_files():
    names = [f"d/{i}.jpg" for i in range(20)]
    old = _TMP / "completa" / "foto"
    new = _TMP / "parziale" / "foto"
    _make_tree(old, names)
    conn = _connect("rebase_parziale")
    assert sm.scan_into_db(conn, Config(source_dir=str(old))) == len(names)
    for n in names[:14]:  # solo il 70%: riscrivere tutti i percorsi farebbe "sparire" gli altri 6 file
        (new / n).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old / n, new / n)
    assert sourcefs.rebase_catalog(conn, str(old), str(new)) == 0
    paths = [r["path"] for r in conn.execute("SELECT path FROM files")]
    assert all(p.startswith(sourcefs.norm_root(str(old))) for p in paths), paths
    for n in names[14:]:  # ora ci sono tutti
        (new / n).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old / n, new / n)
    assert sourcefs.rebase_catalog(conn, str(old), str(new)) == len(names)
    conn.close()


def test_network_mount_missing_detects_unmounted_fstab_share():
    import io

    fstab = "# nas\n//nas/foto /mnt/nas cifs defaults 0 0\n/dev/sda1 / ext4 defaults 0 1\n"
    real_open = open

    def fake_open(path, *a, **k):
        return io.StringIO(fstab) if str(path) == "/etc/fstab" else real_open(path, *a, **k)

    with mock.patch.object(sourcefs.sys, "platform", "linux"), mock.patch("builtins.open", fake_open):
        with mock.patch.object(sourcefs.os.path, "ismount", return_value=False):
            assert sourcefs.network_mount_missing("/mnt/nas") is True
            assert sourcefs.network_mount_missing("/home/utente/foto") is False
        with mock.patch.object(sourcefs.os.path, "ismount", return_value=True):
            assert sourcefs.network_mount_missing("/mnt/nas") is False
    assert sourcefs.network_mount_missing(str(_TMP)) is False  # percorso locale (e su Windows sempre False)


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("OK  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
