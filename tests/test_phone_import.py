"""Test dell'importazione dal telefono: spostamento dallo staging alla destinazione (senza telefono reale).

Verifica: file nuovi spostati con struttura, duplicati (stesso nome e dimensione) saltati, conflitti di nome
con dimensione diversa rinominati (_2), file non multimediali ignorati, sorgente mai toccata.
Esecuzione: `.\\.venv\\Scripts\\python.exe tests\\test_phone_import.py` (oppure pytest).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fotoscan import phone_mtp  # noqa: E402


def _write(p: Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_move_into_dest_rules() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="phone_test_"))
    staging = tmp / "staging"
    dest = tmp / "dest"
    dest.mkdir()

    # nuovo file con struttura
    _write(staging / "DCIM" / "Camera" / "IMG_1.jpg", b"aaaa")
    # gia' presente, stessa dimensione -> saltato
    _write(staging / "DCIM" / "Camera" / "IMG_2.jpg", b"bbbb")
    _write(dest / "DCIM" / "Camera" / "IMG_2.jpg", b"xxxx")
    # stesso nome, dimensione diversa -> rinominato
    _write(staging / "DCIM" / "Camera" / "IMG_3.jpg", b"cccccc")
    _write(dest / "DCIM" / "Camera" / "IMG_3.jpg", b"c")
    # non multimediale -> ignorato
    _write(staging / "DCIM" / "Camera" / "note.txt", b"x")

    res = phone_mtp.ImportResult()
    phone_mtp._move_into_dest(staging, dest, res)

    assert res.new_files == 2, res
    assert res.skipped_existing == 1, res
    assert (dest / "DCIM" / "Camera" / "IMG_1.jpg").read_bytes() == b"aaaa"
    assert (dest / "DCIM" / "Camera" / "IMG_3_2.jpg").read_bytes() == b"cccccc"
    assert (dest / "DCIM" / "Camera" / "IMG_3.jpg").read_bytes() == b"c"  # originale intatto
    assert (dest / "DCIM" / "Camera" / "IMG_2.jpg").read_bytes() == b"xxxx"  # mai sovrascritto
    assert not (dest / "DCIM" / "Camera" / "note.txt").exists()


def test_classify_nome_e_dimensione() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="phone_cls_"))
    dest = tmp / "dest"
    _write(dest / "DCIM" / "Camera" / "IMG_1.jpg", b"1234")      # gia' in destinazione, dimensione 4
    _write(dest / "altro.txt", b"zz")                              # non multimediale: ignorato
    catalog = {phone_mtp._key("IMG_9.jpg", 9)}                     # gia' nell'archivio
    _, dest_keys = phone_mtp.existing_keys(_FakeConn(catalog), dest)
    # _FakeConn: il catalogo viene letto dalla query; qui basta verificare le chiavi di destinazione
    assert phone_mtp._key("IMG_1.jpg", 4) in dest_keys
    assert all(k[0] != "altro.txt" for k in dest_keys)

    items = [
        {"rel": "DCIM/Camera/IMG_1.jpg", "name": "IMG_1.jpg", "size": 4},   # destinazione
        {"rel": "DCIM/Camera/IMG_9.jpg", "name": "IMG_9.jpg", "size": 9},   # archivio
        {"rel": "DCIM/Camera/IMG_5.jpg", "name": "IMG_5.jpg", "size": 5},   # nuovo
        {"rel": "DCIM/Camera/IMG_7.jpg", "name": "img_1.JPG", "size": 4},   # stesso nome/dimensione, maiuscole diverse
        {"rel": "DCIM/Camera/NOSIZE.jpg", "name": "NOSIZE.jpg", "size": -1},  # dimensione ignota: si copia
    ]
    out = phone_mtp.classify(items, catalog, dest_keys)
    known = {k["rel"]: k["reason"] for k in out["known"]}
    assert known == {"DCIM/Camera/IMG_1.jpg": "destinazione", "DCIM/Camera/IMG_9.jpg": "archivio",
                     "DCIM/Camera/IMG_7.jpg": "destinazione"}, known
    assert [i["rel"] for i in out["new"]] == ["DCIM/Camera/IMG_5.jpg", "DCIM/Camera/NOSIZE.jpg"]


class _FakeConn:
    """Imita la sola query di existing_keys: SELECT name, size FROM files ..."""

    def __init__(self, keys: set):
        self._rows = [(name, size) for name, size in ((k[0], k[1]) for k in keys)]

    def execute(self, _sql):
        return iter(self._rows)


def test_storico_importazioni() -> None:
    """Lo storico conserva tutti gli scarichi (piu' recente per prima) e l'ultimo resta in KEY."""
    from fotoscan import db as dbm, phone_log

    tmp = Path(tempfile.mkdtemp(prefix="phone_hist_"))
    conn = dbm.connect(str(tmp / "catalogo.db"))
    try:
        phone_log.record_import(conn, "Pixel", 5, 6, when="2026-11-23T08:00:00", state="done", folders=["DCIM/Camera"])
        phone_log.record_import(conn, "Pixel", 0, 2, when="2026-11-23T09:00:00", state="cancelled")
        phone_log.record_import(conn, "Pixel", 0, 0, when="2026-11-23T10:00:00", state="error", error="boom")
        hist = phone_log.history(conn)
        assert [r.when for r in hist] == ["2026-11-23T10:00:00", "2026-11-23T09:00:00", "2026-11-23T08:00:00"], hist
        assert [r.state for r in hist] == ["error", "cancelled", "done"]
        assert hist[2].folders == ["DCIM/Camera"] and hist[2].new_files == 5
        assert phone_log.last_import(conn).when == "2026-11-23T10:00:00"
    finally:
        conn.close()


def test_scelta_cartelle_senza_duplicati() -> None:
    """Una sottocartella gia' inclusa nella cartella scelta viene tolta (niente conteggi/copie doppi)."""
    from fotoscan.server import routes_phone as rp

    out = rp._dedupe_folders([["DCIM", "Camera"], ["DCIM"], ["Pictures"], ["DCIM", "Camera", "x"]])
    assert sorted(out) == [["DCIM"], ["Pictures"]], out
    assert rp._dedupe_folders([["A", "B"], ["A", "C"]]) == [["A", "B"], ["A", "C"]]
    device, storage, folders = rp._check_selection({"device": "P", "storage": "S", "folders": [["DCIM"], ["DCIM", "x"]]})
    assert folders == [["DCIM"]], folders


def test_anteprima_lista_completa() -> None:
    """L'anteprima restituisce tutti i file gia' presenti (il client li rimanda come `skip`): nessun limite a 5000."""
    from fotoscan.server import routes_phone as rp

    n = 5300
    items = [{"rel": f"DCIM/Camera/IMG_{i}.jpg", "name": f"IMG_{i}.jpg", "size": 10} for i in range(n)]
    known = [{**it, "reason": "archivio"} for it in items]

    class _Conn:
        def close(self):
            pass

    orig = (rp.phone_mtp.scan_folders, rp.phone_mtp.existing_keys, rp.phone_mtp.classify, rp.deps.open_db)
    try:
        rp.phone_mtp.scan_folders = lambda *a, **k: items
        rp.phone_mtp.existing_keys = lambda conn, dest: (set(), set())
        rp.phone_mtp.classify = lambda it, cat, dk: {"new": [], "known": known}
        rp.deps.open_db = lambda: _Conn()
        res = rp.scan({"device": "P", "storage": "S", "folders": [["DCIM"]], "dest": "C:/tmp/x"})
    finally:
        rp.phone_mtp.scan_folders, rp.phone_mtp.existing_keys, rp.phone_mtp.classify, rp.deps.open_db = orig
    assert res["known"] == n and len(res["known_items"]) == n, (res["known"], len(res["known_items"]))


if __name__ == "__main__":
    test_move_into_dest_rules()
    test_classify_nome_e_dimensione()
    test_storico_importazioni()
    test_scelta_cartelle_senza_duplicati()
    test_anteprima_lista_completa()
    print("OK test_phone_import")
