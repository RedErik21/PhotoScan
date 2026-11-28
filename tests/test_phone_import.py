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


def _place_all(staging: Path, dest: Path, res) -> None:
    """Piazza tutti i file dello staging (come fa copy_folders, un file alla volta)."""
    for src in sorted(p for p in staging.rglob("*") if p.is_file()):
        phone_mtp._place(src, staging, dest, res)


def test_move_into_dest_rules() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="phone_test_"))
    staging = tmp / "staging"
    dest = tmp / "dest"
    dest.mkdir()

    # nuovo file con struttura
    _write(staging / "DCIM" / "Camera" / "IMG_1.jpg", b"aaaa")
    # stesso nome e STESSO contenuto -> duplicato, saltato
    _write(staging / "DCIM" / "Camera" / "IMG_2.jpg", b"bbbb")
    _write(dest / "DCIM" / "Camera" / "IMG_2.jpg", b"bbbb")
    # stesso nome e stessa dimensione ma contenuto DIVERSO -> foto diversa: tenuta, rinominata
    _write(staging / "DCIM" / "Camera" / "IMG_4.jpg", b"dddd")
    _write(dest / "DCIM" / "Camera" / "IMG_4.jpg", b"xxxx")
    # stesso nome, dimensione diversa -> rinominato
    _write(staging / "DCIM" / "Camera" / "IMG_3.jpg", b"cccccc")
    _write(dest / "DCIM" / "Camera" / "IMG_3.jpg", b"c")
    # non multimediale -> ignorato
    _write(staging / "DCIM" / "Camera" / "note.txt", b"x")

    res = phone_mtp.ImportResult()
    _place_all(staging, dest, res)

    assert res.new_files == 3, res
    assert res.skipped_existing == 1, res
    assert (dest / "DCIM" / "Camera" / "IMG_1.jpg").read_bytes() == b"aaaa"
    assert (dest / "DCIM" / "Camera" / "IMG_3_2.jpg").read_bytes() == b"cccccc"
    assert (dest / "DCIM" / "Camera" / "IMG_3.jpg").read_bytes() == b"c"  # originale intatto
    assert (dest / "DCIM" / "Camera" / "IMG_4.jpg").read_bytes() == b"xxxx"  # mai sovrascritto
    assert (dest / "DCIM" / "Camera" / "IMG_4_2.jpg").read_bytes() == b"dddd"  # la foto diversa non va persa
    assert not (dest / "DCIM" / "Camera" / "note.txt").exists()
    assert not list(dest.rglob("*" + phone_mtp.TMP_SUFFIX)), "file temporaneo lasciato"


def test_reimport_of_renamed_variant_is_not_duplicated() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="phone_var_"))
    staging, dest = tmp / "staging", tmp / "dest"
    _write(dest / "IMG.jpg", b"prima")
    _write(dest / "IMG_2.jpg", b"seconda")   # importata la volta scorsa con un numero nel nome
    _write(staging / "IMG.jpg", b"seconda")   # stessa foto, riportata dal telefono
    res = phone_mtp.ImportResult()
    _place_all(staging, dest, res)
    assert res.new_files == 0 and res.skipped_existing == 1, res
    assert sorted(p.name for p in dest.iterdir()) == ["IMG.jpg", "IMG_2.jpg"]


def _fake_run(messages, staging_files):
    """Sostituisce phone_mtp._run: scrive i file nello staging e restituisce i messaggi dello script."""
    def run(args, timeout=None, cancelled=None):
        staging = Path(args["staging"])
        for rel, data in staging_files.items():
            _write(staging / rel, data)
        for m in messages:
            if isinstance(m, Exception):
                raise m
            yield m
    return run


def _copy_with(messages, staging_files, cancelled=None):
    tmp = Path(tempfile.mkdtemp(prefix="phone_copy_"))
    dest = tmp / "dest"
    orig = phone_mtp._run
    phone_mtp._run = _fake_run(messages, staging_files)
    try:
        res = phone_mtp.copy_folders("P", "S", [["DCIM"]], dest, cancelled=cancelled)
    finally:
        phone_mtp._run = orig
    return dest, res


def test_partial_files_never_reach_destination() -> None:
    msgs = [
        {"type": "total", "n": 3, "bytes": 12, "skipped": 0},
        {"type": "file", "i": 1, "rel": "DCIM/ok.jpg", "ok": True, "bytes": 4, "size": 4},
        # timeout: lo script riporta l'errore ma il file a meta' e' rimasto nello staging
        {"type": "file", "i": 2, "rel": "DCIM/meta.mp4", "ok": False, "bytes": 0, "size": 8, "error": "timeout nella copia"},
        # dichiarato ok ma piu' corto di quanto atteso
        {"type": "file", "i": 3, "rel": "DCIM/corto.jpg", "ok": True, "bytes": 2, "size": 6},
        {"type": "done"},
    ]
    files = {"DCIM/ok.jpg": b"1234", "DCIM/meta.mp4": b"12", "DCIM/corto.jpg": b"12"}
    dest, res = _copy_with(msgs, files)
    assert sorted(p.name for p in dest.rglob("*") if p.is_file()) == ["ok.jpg"], list(dest.rglob("*"))
    assert res.copied == 1 and res.new_files == 1 and len(res.errors) == 2, res
    assert not (dest / phone_mtp.STAGING_NAME).exists()


def test_cancel_discards_file_in_flight() -> None:
    msgs = [
        {"type": "total", "n": 2, "bytes": 8, "skipped": 0},
        {"type": "file", "i": 1, "rel": "DCIM/a.jpg", "ok": True, "bytes": 4, "size": 4},
        {"type": "file", "i": 2, "rel": "DCIM/b.jpg", "ok": True, "bytes": 4, "size": 4},
    ]
    files = {"DCIM/a.jpg": b"1234", "DCIM/b.jpg": b"5678", "DCIM/c.mp4": b"12"}  # c: copia in corso, non dichiarata
    seen = {"n": 0}

    def cancelled():
        seen["n"] += 1
        return seen["n"] > 2   # annullato dopo il primo file

    dest, res = _copy_with(msgs, files, cancelled=cancelled)
    names = sorted(p.name for p in dest.rglob("*") if p.is_file())
    assert names == ["a.jpg"], names


def test_phone_unplugged_midway_keeps_done_files_and_reports() -> None:
    msgs = [
        {"type": "total", "n": 2, "bytes": 8, "skipped": 0},
        {"type": "file", "i": 1, "rel": "DCIM/a.jpg", "ok": True, "bytes": 4, "size": 4},
        phone_mtp.PhoneError("Telefono scollegato o bloccato: importazione interrotta."),
    ]
    dest, res = _copy_with(msgs, {"DCIM/a.jpg": b"1234", "DCIM/b.jpg": b"56"})
    assert sorted(p.name for p in dest.rglob("*") if p.is_file()) == ["a.jpg"]
    assert "scollegato" in res.aborted and res.new_files == 1, res


def test_args_go_through_a_file_not_the_environment() -> None:
    """Su Windows una variabile d'ambiente e' limitata a 32 KB: la lista dei file da saltare puo' essere piu' lunga."""
    import io
    import json as _json

    if sys.platform != "win32":
        return
    captured = {}

    class FakeProc:
        stdout, stderr = io.StringIO(""), io.StringIO("")

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    def fake_popen(cmd, **kw):
        env = kw["env"]
        captured["file"] = env.get("FOTOSCAN_PHONE_ARGS_FILE")
        captured["no_env"] = "FOTOSCAN_PHONE_ARGS" not in env
        captured["data"] = _json.loads(Path(captured["file"]).read_text(encoding="utf-8"))
        return FakeProc()

    skip = [f"DCIM/Camera/IMG_{i:06d}_con_nome_lungo.jpg" for i in range(1500)]   # circa 70 KB di JSON
    orig = phone_mtp.subprocess.Popen
    phone_mtp.subprocess.Popen = fake_popen
    try:
        list(phone_mtp._run({"action": "copy", "skip": skip}))
    finally:
        phone_mtp.subprocess.Popen = orig
    assert captured["no_env"] and captured["data"]["skip"] == skip
    assert not Path(captured["file"]).exists(), "file degli argomenti non rimosso"


def test_run_times_out_on_silent_phone() -> None:
    import io

    if sys.platform != "win32":
        return

    class Hang(io.StringIO):
        def __iter__(self):
            import time
            time.sleep(5)
            return iter(())

    class FakeProc:
        stdout, stderr = Hang(""), io.StringIO("")

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return None

        def kill(self):
            pass

    orig = phone_mtp.subprocess.Popen
    phone_mtp.subprocess.Popen = lambda *a, **k: FakeProc()
    try:
        try:
            list(phone_mtp._run({"action": "list"}, timeout=1.0))
        except phone_mtp.PhoneError as e:
            assert "non risponde" in str(e)
        else:
            raise AssertionError("nessun timeout")
    finally:
        phone_mtp.subprocess.Popen = orig


def test_dest_must_be_absolute() -> None:
    from fastapi import HTTPException

    from fotoscan.server import routes_phone as rp

    for bad in ("foto/telefono", "relativo"):
        try:
            rp._check_dest(bad)
        except HTTPException as e:
            assert e.status_code == 400
        else:
            raise AssertionError(f"destinazione relativa accettata: {bad}")
    assert rp._check_dest(str(Path(tempfile.mkdtemp(prefix="phone_dest_")) / "x"))


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
    test_reimport_of_renamed_variant_is_not_duplicated()
    test_partial_files_never_reach_destination()
    test_cancel_discards_file_in_flight()
    test_phone_unplugged_midway_keeps_done_files_and_reports()
    test_args_go_through_a_file_not_the_environment()
    test_run_times_out_on_silent_phone()
    test_dest_must_be_absolute()
    test_classify_nome_e_dimensione()
    test_storico_importazioni()
    test_scelta_cartelle_senza_duplicati()
    test_anteprima_lista_completa()
    print("OK test_phone_import")
