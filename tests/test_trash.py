"""Cestino del sistema (fotoscan/trash.py): mai una cancellazione definitiva.

Si usano solo file creati in cartelle temporanee. Su Windows un test manda davvero un file temporaneo nel Cestino
(se il Cestino del disco e' disattivato l'operazione deve FALLIRE lasciando il file: e' accettato come esito).
Esecuzione: `.\\.venv\\Scripts\\python.exe tests\\test_trash.py`.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fotoscan import trash  # noqa: E402


def _tmpfile(data: bytes = b"prova") -> Path:
    d = Path(tempfile.mkdtemp(prefix="trash_test_"))
    p = d / "x.jpg"
    p.write_bytes(data)
    return p


def test_missing_file_and_folder_are_refused() -> None:
    d = Path(tempfile.mkdtemp(prefix="trash_test_"))
    for target in (d / "non_esiste.jpg", d):
        try:
            trash.send_to_trash(str(target))
        except trash.TrashError as e:
            assert isinstance(e, OSError)
        else:
            raise AssertionError(f"accettato: {target}")
    assert d.exists()


def test_network_paths_are_refused_without_touching_the_file() -> None:
    p = _tmpfile()
    with mock.patch("fotoscan.sourcefs.is_network", return_value=True):
        try:
            trash.send_to_trash(str(p))
        except trash.TrashError as e:
            assert "rete" in str(e)
        else:
            raise AssertionError("percorso di rete accettato")
    assert p.exists()


def test_file_still_in_place_is_an_error() -> None:
    p = _tmpfile()
    target = "_trash_windows" if os.name == "nt" else "_trash_linux"
    with mock.patch.object(trash, target, lambda full: None):  # "successo" dichiarato ma il file e' rimasto
        try:
            trash.send_to_trash(str(p))
        except trash.TrashError as e:
            assert "ancora al suo posto" in str(e)
        else:
            raise AssertionError("file rimasto ma nessun errore")
    assert p.exists()


def test_windows_fallback_checks_recycle_bin_before_deleting() -> None:
    if os.name != "nt":
        return
    p = _tmpfile(b"x" * 1000)
    with mock.patch.object(trash, "_trash_ifileoperation", side_effect=trash._ComUnavailable("test")):
        with mock.patch.object(trash, "_bin_settings", return_value=(False, None)):   # Cestino disattivato
            try:
                trash.send_to_trash(str(p))
            except trash.TrashError as e:
                assert "disattivato" in str(e)
            else:
                raise AssertionError("Cestino disattivato ignorato")
        assert p.exists()
        with mock.patch.object(trash, "_bin_settings", return_value=(True, 10)):      # capienza di 10 byte
            try:
                trash.send_to_trash(str(p))
            except trash.TrashError as e:
                assert "capienza" in str(e)
            else:
                raise AssertionError("file troppo grande accettato")
        assert p.exists()


def test_windows_real_recycle_or_clean_failure() -> None:
    if os.name != "nt":
        return
    p = _tmpfile(b"cestino di prova")
    try:
        trash.send_to_trash(str(p))
    except trash.TrashError:
        assert p.exists(), "errore dichiarato ma il file e' sparito: cancellazione definitiva!"
    else:
        assert not p.exists()


def test_linux_xdg_trash_moves_with_trashinfo() -> None:
    if not sys.platform.startswith("linux"):
        return
    data = Path(tempfile.mkdtemp(prefix="xdg_data_"))
    p = _tmpfile(b"ciao")
    with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data)}), mock.patch("shutil.which", return_value=None):
        trash.send_to_trash(str(p))
        q = _tmpfile(b"altro")
        os.rename(q, p)  # stesso nome di nuovo: nel Cestino diventa x.jpg.1
        trash.send_to_trash(str(p))
    files = sorted(f.name for f in (data / "Trash" / "files").iterdir()) if (data / "Trash" / "files").exists() else []
    if files:  # (su un altro volume del temporaneo il Cestino e' quello del disco: gia' coperto da trash.py)
        assert files == ["x.jpg", "x.jpg.1"], files
        info = (data / "Trash" / "info" / "x.jpg.trashinfo").read_text(encoding="utf-8")
        assert info.startswith("[Trash Info]\nPath=") and "DeletionDate=" in info
    assert not p.exists()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("OK test_trash")
