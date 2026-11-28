"""NAS (fotoscan/nas.py): invio verificato, ricezione senza sovrascritture, pulizia con conferma.

Nessun NAS reale: il NAS e' una cartella temporanea. Il Cestino di Windows non si tocca nei test
(la pulizia reale e' simulata; su Linux il comando del Cestino non e' disponibile).
Esecuzione: `python -m pytest tests\\test_nas.py` oppure `python tests\\test_nas.py`.
"""
from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="fs_nas_test_"))
os.environ["FOTOSCAN_DB"] = str(_TMP / "catalogo.db")  # mai il catalogo vero
os.environ["FOTOSCAN_CACHE"] = str(_TMP / "cache")

from fotoscan import db as dbm  # noqa: E402
from fotoscan import nas  # noqa: E402
from fotoscan.config import Config  # noqa: E402


@dataclass
class FakeJob:
    cancel_requested: bool = False
    stage: str = ""
    done: int = 0
    total: int = 0
    log: list = field(default_factory=list)

    def update(self, stage=None, done=None, total=None, message=None):
        if stage is not None:
            self.stage = stage
        if done is not None:
            self.done = done
        if total is not None:
            self.total = total


def _cfg(tmp: Path, source: Path, nas_dir: Path) -> Config:
    return Config(source_dir=str(source), nas_dir=str(nas_dir), cache_dir=str(tmp / "cache"),
                  db_path=str(tmp / "catalogo.db"), nas_layout="as_is")


def _add_file(conn, path: Path, data: bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    st = path.stat()
    with conn:
        cur = conn.execute(
            "INSERT INTO files(path, name, kind, size, mtime, hash) VALUES(?,?,?,?,?,?)",
            (str(path), path.name, "image", st.st_size, st.st_mtime, nas.file_hash(str(path))),
        )
    return int(cur.lastrowid)


def _fresh(name: str):
    base = Path(tempfile.mkdtemp(prefix=f"nas_{name}_"))
    source = base / "PC" / "foto"
    nas_dir = base / "NAS" / "foto"
    nas_dir.mkdir(parents=True)
    cfg = _cfg(base, source, nas_dir)
    conn = dbm.connect(cfg.db_path)
    return base, source, nas_dir, cfg, conn


def test_send_copies_and_verifies() -> None:
    base, source, nas_dir, cfg, conn = _fresh("send")
    _add_file(conn, source / "2020" / "a.jpg", b"AAAA")
    _add_file(conn, source / "b.jpg", b"BBBBBB")
    res = nas.send_job(FakeJob(), conn, cfg, "as_is")
    assert res["copiati"] == 2 and res["errori"] == 0 and res["conflitti"] == 0, res
    assert (nas_dir / "2020" / "a.jpg").read_bytes() == b"AAAA"
    assert conn.execute("SELECT COUNT(*) FROM nas_copies").fetchone()[0] == 2
    # seconda passata: niente da copiare, i file gia' presenti vengono verificati con l'hash
    res2 = nas.send_job(FakeJob(), conn, cfg, "as_is")
    assert res2["copiati"] == 0 and res2["verificati"] == 2, res2


def test_send_never_overwrites_conflict() -> None:
    base, source, nas_dir, cfg, conn = _fresh("conflict")
    _add_file(conn, source / "c.jpg", b"LOCALE-LUNGO")
    (nas_dir / "c.jpg").write_bytes(b"altro")  # stesso nome, contenuto diverso sul NAS
    res = nas.send_job(FakeJob(), conn, cfg, "as_is")
    assert res["conflitti"] == 1 and res["copiati"] == 0, res
    assert (nas_dir / "c.jpg").read_bytes() == b"altro"  # intatto
    assert conn.execute("SELECT COUNT(*) FROM nas_copies").fetchone()[0] == 0  # nessuna copia verificata


def test_copy_verified_rejects_bad_hash_and_leaves_no_partial() -> None:
    base, source, nas_dir, cfg, conn = _fresh("hash")
    src = source / "d.jpg"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"DATI")
    dst = nas_dir / "d.jpg"
    with mock.patch.object(nas, "file_hash", return_value="sbagliato"):
        try:
            nas.copy_verified(str(src), dst, "giusto")
        except OSError:
            pass
        else:
            raise AssertionError("hash errato accettato")
    assert not dst.exists()
    assert not (nas_dir / "d.jpg.fotoscan-tmp").exists()


def test_receive_does_not_overwrite() -> None:
    base, source, nas_dir, cfg, conn = _fresh("recv")
    (nas_dir / "2021").mkdir()
    (nas_dir / "2021" / "x.jpg").write_bytes(b"NUOVO")
    (nas_dir / "2021" / "y.jpg").write_bytes(b"UGUALE")
    dest = base / "ricevute"
    (dest / "2021").mkdir(parents=True)
    (dest / "2021" / "y.jpg").write_bytes(b"UGUALE")
    (dest / "2021" / "x.jpg").write_bytes(b"DIVERSO")  # conflitto: non si tocca
    res = nas.receive_job(FakeJob(), cfg, str(dest))
    assert res["ricevuti"] == 0 and res["gia_presenti"] == 1 and res["conflitti"] == 1, res
    assert (dest / "2021" / "x.jpg").read_bytes() == b"DIVERSO"


def test_cleanup_requires_verified_unchanged_copies() -> None:
    base, source, nas_dir, cfg, conn = _fresh("cleanup")
    fid = _add_file(conn, source / "e.jpg", b"EEEE")
    nas.send_job(FakeJob(), conn, cfg, "as_is")
    assert nas.cleanup_preview(conn)["count"] == 1
    # il file locale viene modificato dopo la verifica: non e' piu' candidato
    p = source / "e.jpg"
    p.write_bytes(b"EEEE-cambiato")
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 10))
    assert nas.cleanup_preview(conn)["count"] == 0
    assert fid  # usato sopra


def test_cleanup_rejects_stale_confirmation() -> None:
    base, source, nas_dir, cfg, conn = _fresh("stale")
    _add_file(conn, source / "f.jpg", b"FFFF")
    nas.send_job(FakeJob(), conn, cfg, "as_is")
    try:
        nas.cleanup_job(FakeJob(), conn, cfg, expected_count=5)
    except nas.NasError:
        pass
    else:
        raise AssertionError("conferma con numero errato accettata")


def test_cleanup_moves_nothing_when_copy_changed_on_nas() -> None:
    base, source, nas_dir, cfg, conn = _fresh("nascopy")
    _add_file(conn, source / "g.jpg", b"GGGG")
    nas.send_job(FakeJob(), conn, cfg, "as_is")
    (nas_dir / "g.jpg").write_bytes(b"corrotto")  # la copia sul NAS non e' piu' valida
    with mock.patch.object(nas, "send_to_recycle") as rec:
        res = nas.cleanup_job(FakeJob(), conn, cfg, expected_count=1)
    rec.assert_not_called()
    assert res["spostati"] == 0 and res["saltati"] == 1, res


def test_cleanup_moves_verified_file_to_recycle() -> None:
    base, source, nas_dir, cfg, conn = _fresh("recycle")
    _add_file(conn, source / "h.jpg", b"HHHH")
    nas.send_job(FakeJob(), conn, cfg, "as_is")
    with mock.patch.object(nas, "send_to_recycle") as rec:
        res = nas.cleanup_job(FakeJob(), conn, cfg, expected_count=1)
    rec.assert_called_once()
    assert res["spostati"] == 1, res
    assert conn.execute("SELECT COUNT(*) FROM nas_copies").fetchone()[0] == 0


def test_by_album_layout() -> None:
    base, source, nas_dir, cfg, conn = _fresh("album")
    fid = _add_file(conn, source / "k.jpg", b"KKKK")
    with conn:
        conn.execute("INSERT INTO albums(id, name, created) VALUES(1, 'Vacanze: Roma', '2026-01-01')")
        conn.execute("INSERT INTO album_items(album_id, file_id, added) VALUES(1, ?, '2026-01-01')", (fid,))
    items = nas.plan_send(conn, cfg, "by_album")
    assert items[0]["rel"] == "Vacanze_ Roma/k.jpg", items[0]["rel"]


def test_by_album_disambiguates_duplicate_filenames() -> None:
    base, source, nas_dir, cfg, conn = _fresh("album_dup")
    _add_file(conn, source / "a" / "IMG_0001.jpg", b"PRIMA")
    fid2 = _add_file(conn, source / "b" / "IMG_0001.jpg", b"SECONDA")
    items = nas.plan_send(conn, cfg, "by_album")
    rels = [i["rel"] for i in items]
    assert rels == ["_senza_album/IMG_0001.jpg", f"_senza_album/IMG_0001_{fid2}.jpg"], rels
    res = nas.send_job(FakeJob(), conn, cfg, "by_album")
    assert res["copiati"] == 2 and res["conflitti"] == 0, res


def test_send_checks_free_space_before_starting() -> None:
    base, source, nas_dir, cfg, conn = _fresh("space_send")
    _add_file(conn, source / "big.jpg", b"X" * 100)
    fake_usage = mock.Mock(total=1000, used=950, free=50)
    with mock.patch.object(nas.shutil, "disk_usage", return_value=fake_usage):
        try:
            nas.send_job(FakeJob(), conn, cfg, "as_is")
        except nas.NasError as e:
            assert "Spazio insufficiente" in str(e)
        else:
            raise AssertionError("doveva rifiutare per spazio insufficiente sul NAS")


def test_receive_counts_only_missing_files_for_free_space() -> None:
    base, source, nas_dir, cfg, conn = _fresh("space_recv")
    (nas_dir / "old.jpg").write_bytes(b"A" * 200)
    (nas_dir / "new.jpg").write_bytes(b"B" * 20)
    dest = base / "dest"
    dest.mkdir()
    (dest / "old.jpg").write_bytes(b"A" * 200)  # gia' presente sul PC
    fake_usage = mock.Mock(total=1000, used=950, free=50)  # 50 > 20 (mancanti), ma < 220 (totali)
    with mock.patch.object(nas.shutil, "disk_usage", return_value=fake_usage):
        res = nas.receive_job(FakeJob(), cfg, str(dest))
    assert res["ricevuti"] == 1 and res["gia_presenti"] == 1, res


def test_send_incremental_skips_rehashing_verified_copies() -> None:
    base, source, nas_dir, cfg, conn = _fresh("inc")
    _add_file(conn, source / "a.jpg", b"AAAA")
    nas.send_job(FakeJob(), conn, cfg, "as_is")
    with mock.patch.object(nas, "file_hash", side_effect=AssertionError("non doveva ricalcolare l'hash")):
        res = nas.send_job(FakeJob(), conn, cfg, "as_is")
    assert res["copiati"] == 0 and res["verificati"] == 1, res


def test_cleanup_removes_orphaned_file_from_catalog() -> None:
    base, source, nas_dir, cfg, conn = _fresh("orphan")
    _add_file(conn, source / "h.jpg", b"HHHH")
    nas.send_job(FakeJob(), conn, cfg, "as_is")
    with mock.patch.object(nas, "send_to_recycle"):
        res = nas.cleanup_job(FakeJob(), conn, cfg, expected_count=1)
    assert res["spostati"] == 1
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM nas_copies").fetchone()[0] == 0


def test_overlap_detection_rejects_nested_paths() -> None:
    from fotoscan.server.routes_nas import _overlap
    assert _overlap(r"C:\Foto", r"C:\Foto")
    assert _overlap(r"C:\Foto\NAS", r"C:\Foto")
    assert _overlap(r"C:\Foto", r"C:\Foto\NAS")
    assert not _overlap(r"C:\Foto", r"C:\Foto2")


def test_by_album_filters_selected_albums_and_unsorted() -> None:
    base, source, nas_dir, cfg, conn = _fresh("album_filter")
    f1 = _add_file(conn, source / "a.jpg", b"AAAA")
    f2 = _add_file(conn, source / "b.jpg", b"BBBB")
    f3 = _add_file(conn, source / "c.jpg", b"CCCC")  # senza album
    with conn:
        conn.execute("INSERT INTO albums(id, name, created) VALUES(1, 'Mare', '2026-01-01')")
        conn.execute("INSERT INTO albums(id, name, created) VALUES(2, 'Montagna', '2026-01-01')")
        conn.execute("INSERT INTO album_items(album_id, file_id, added) VALUES(1, ?, '2026-01-01')", (f1,))
        # f2 e' sia in Mare che in Montagna: se scelgo solo Montagna deve finire in Montagna
        conn.execute("INSERT INTO album_items(album_id, file_id, added) VALUES(1, ?, '2026-01-01')", (f2,))
        conn.execute("INSERT INTO album_items(album_id, file_id, added) VALUES(2, ?, '2026-01-01')", (f2,))
    assert len(nas.list_albums(conn)) == 2
    items_only_2 = nas.plan_send(conn, cfg, "by_album", album_ids=[2], include_unsorted=False)
    assert [i["rel"] for i in items_only_2] == ["Montagna/b.jpg"], items_only_2
    items_2_plus_unsorted = nas.plan_send(conn, cfg, "by_album", album_ids=[2], include_unsorted=True)
    assert [i["rel"] for i in items_2_plus_unsorted] == ["Montagna/b.jpg", "_senza_album/c.jpg"], items_2_plus_unsorted
    res = nas.send_job(FakeJob(), conn, cfg, "by_album", album_ids=[2], include_unsorted=False)
    assert res["copiati"] == 1 and (nas_dir / "Montagna" / "b.jpg").exists() and not (nas_dir / "Mare").exists()



def test_send_reuses_catalog_hash_when_unchanged() -> None:
    base, source, nas_dir, cfg, conn = _fresh("reuse")
    _add_file(conn, source / "r.jpg", b"RRRR")  # _add_file salva l'hash nel catalogo con size+mtime
    real = nas.file_hash
    calls: list[str] = []

    def spy(path):
        calls.append(str(path))
        return real(path)
    with mock.patch.object(nas, "file_hash", side_effect=spy):
        res = nas.send_job(FakeJob(), conn, cfg, "as_is")
    assert res["copiati"] == 1, res
    assert not any(c.endswith("r.jpg") and "NAS" not in c for c in calls), calls  # sorgente non riletta
    assert any(c.endswith(nas.TMP_SUFFIX) for c in calls), calls  # la copia sul NAS si verifica sempre


def test_receive_size_mismatch_is_conflict_without_hashing() -> None:
    base, source, nas_dir, cfg, conn = _fresh("recvsize")
    (nas_dir / "z.jpg").write_bytes(b"LUNGO-SUL-NAS")
    dest = base / "ricevute"
    dest.mkdir()
    (dest / "z.jpg").write_bytes(b"corto")
    with mock.patch.object(nas, "file_hash", side_effect=AssertionError("non deve leggere")):
        res = nas.receive_job(FakeJob(), cfg, str(dest))
    assert res["conflitti"] == 1 and res["errori"] == 0, res
    assert (dest / "z.jpg").read_bytes() == b"corto"


def test_copy_verified_never_overwrites_late_file() -> None:
    base, source, nas_dir, cfg, conn = _fresh("late")
    src = source / "l.jpg"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"NUOVO")
    dst = nas_dir / "l.jpg"
    real_copy = nas.shutil.copyfile

    def copy_then_race(a, b):
        real_copy(a, b)
        dst.write_bytes(b"ARRIVATO-PRIMA")  # un altro processo crea la destinazione durante la copia
    with mock.patch.object(nas.shutil, "copyfile", side_effect=copy_then_race):
        try:
            nas.copy_verified(str(src), dst, nas.file_hash(str(src)))
        except FileExistsError:
            pass
        else:
            raise AssertionError("destinazione sovrascritta")
    assert dst.read_bytes() == b"ARRIVATO-PRIMA"
    assert not list(nas_dir.glob("*" + nas.TMP_SUFFIX))

def test_nas_equal_to_source_is_refused() -> None:
    base, source, nas_dir, cfg, conn = _fresh("stessa")
    _add_file(conn, source / "a.jpg", b"AAAA")
    cfg.nas_dir = str(source)  # il "NAS" e' la cartella sorgente: il backup sarebbe il file stesso
    try:
        nas.plan_send(conn, cfg, "as_is")
    except nas.NasError:
        pass
    else:
        raise AssertionError("NAS uguale alla sorgente accettato")


def test_plan_skips_files_already_inside_nas_dir() -> None:
    base, source, nas_dir, cfg, conn = _fresh("dentro")
    _add_file(conn, source / "a.jpg", b"AAAA")
    # sorgente = cartella madre del NAS: i file gia' nel NAS non si copiano su se stessi
    cfg.source_dir = str(base)
    cfg.nas_dir = str(nas_dir)
    _add_file(conn, nas_dir / "gia.jpg", b"GIA")
    names = {i["name"] for i in nas.plan_send(conn, cfg, "as_is")}
    assert "gia.jpg" not in names and "a.jpg" in names, names


def test_cleanup_never_trashes_a_file_that_is_its_own_backup() -> None:
    base, source, nas_dir, cfg, conn = _fresh("stesso")
    p = source / "s.jpg"
    fid = _add_file(conn, p, b"SSSS")
    st = p.stat()
    with conn:  # copia "verificata" che in realta' punta al file locale stesso
        conn.execute("INSERT INTO nas_copies(file_id, nas_path, local_size, local_mtime, hash, verified) VALUES(?,?,?,?,?,?)",
                     (fid, str(p), st.st_size, st.st_mtime, nas.file_hash(str(p)), "x"))
    with mock.patch.object(nas, "send_to_recycle") as rec:
        res = nas.cleanup_job(FakeJob(), conn, cfg, expected_count=1)
    rec.assert_not_called()
    assert res["spostati"] == 0 and res["saltati"] == 1, res
    assert p.exists()


def test_cleanup_confirmation_compares_file_ids_not_only_count() -> None:
    base, source, nas_dir, cfg, conn = _fresh("ids")
    fid = _add_file(conn, source / "i.jpg", b"IIII")
    nas.send_job(FakeJob(), conn, cfg, "as_is")
    prev = nas.cleanup_preview(conn)
    assert prev["ids"] == [fid], prev
    try:  # stesso numero (1) ma un altro file: va rifiutato
        nas.cleanup_job(FakeJob(), conn, cfg, expected_count=1, expected_ids=[fid + 100])
    except nas.NasError:
        pass
    else:
        raise AssertionError("conferma con id diversi accettata")
    with mock.patch.object(nas, "send_to_recycle") as rec:
        res = nas.cleanup_job(FakeJob(), conn, cfg, expected_count=1, expected_ids=[fid])
    rec.assert_called_once()
    assert res["spostati"] == 1, res


def test_cleanup_calls_faces_forget_when_available() -> None:
    from fotoscan import faces

    base, source, nas_dir, cfg, conn = _fresh("volti")
    fid = _add_file(conn, source / "v.jpg", b"VVVV")
    nas.send_job(FakeJob(), conn, cfg, "as_is")
    calls = []
    with mock.patch.object(nas, "send_to_recycle"), \
            mock.patch.object(faces, "forget_files", lambda c, ids, cfg=None: calls.append(list(ids)), create=True):
        nas.cleanup_job(FakeJob(), conn, cfg, expected_count=1)
    assert calls == [[fid]], calls


def test_guarded_times_out() -> None:
    import time

    t0 = time.monotonic()
    try:
        nas._guarded(lambda: time.sleep(3), 0.2)
    except nas.NasTimeout:
        pass
    else:
        raise AssertionError("nessun timeout")
    assert time.monotonic() - t0 < 2
    assert nas._guarded(lambda: 42, 1.0) == 42


def test_send_stops_after_repeated_nas_stalls() -> None:
    import time

    base, source, nas_dir, cfg, conn = _fresh("stallo")
    for i in range(6):
        _add_file(conn, source / f"s{i}.jpg", b"S" * (i + 1))
    with mock.patch.object(nas, "_io_timeout", lambda size: 0.2), \
            mock.patch.object(nas, "copy_verified", side_effect=lambda *a: time.sleep(2)):
        res = nas.send_job(FakeJob(), conn, cfg, "as_is")
    assert res["errori"] == nas.STALL_LIMIT and res.get("interrotto"), res
    assert res["copiati"] == 0


def test_send_removes_fresh_copy_when_local_file_changed() -> None:
    base, source, nas_dir, cfg, conn = _fresh("cambiato")
    _add_file(conn, source / "c.jpg", b"CCCC")
    with mock.patch.object(nas, "_record_copy", side_effect=OSError("il file locale e' cambiato durante la copia")):
        res = nas.send_job(FakeJob(), conn, cfg, "as_is")
    assert res["errori"] == 1 and res["copiati"] == 0, res
    assert not (nas_dir / "c.jpg").exists(), "copia vecchia lasciata sul NAS"


def test_nas_jobs_are_mutually_exclusive() -> None:
    import threading

    from fastapi import HTTPException

    from fotoscan.server import jobs, routes_nas

    gate = threading.Event()
    j = routes_nas._start_exclusive(routes_nas.CLEANUP, lambda job: gate.wait(5))
    try:
        for name in (routes_nas.SEND, routes_nas.RECEIVE, routes_nas.CLEANUP):
            try:
                routes_nas._start_exclusive(name, lambda job: None)
            except HTTPException as e:
                assert e.status_code == 409
            else:
                raise AssertionError(f"{name} avviato durante la pulizia")
    finally:
        gate.set()
        import time
        for _ in range(50):
            if not jobs.running_job(routes_nas.CLEANUP):
                break
            time.sleep(0.1)
    assert j["name"] == routes_nas.CLEANUP


def test_require_nas_refuses_unmounted_network_mount() -> None:
    base, source, nas_dir, cfg, conn = _fresh("smontato")
    with mock.patch.object(nas.sourcefs, "network_mount_missing", return_value=True):
        try:
            nas.require_nas(cfg)
        except nas.NasError as e:
            assert "montata" in str(e)
        else:
            raise AssertionError("mount di rete mancante accettato")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
