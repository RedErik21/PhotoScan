"""NAS: backup verificato del catalogo, ricezione dal NAS e pulizia sicura del PC.

Regole (non negoziabili):
- Nessuna copia sovrascrive un file esistente. Se la destinazione esiste ma non coincide, e' un conflitto.
- Una copia e' valida solo se l'hash della destinazione, riletta dal disco, coincide con quello della sorgente.
- Dal PC si elimina solo un file la cui copia sul NAS e' verificata adesso (hash riletto), e solo dopo
  una conferma esplicita con il numero di file mostrato. I file vanno nel Cestino del sistema (fotoscan/trash.py).
- Ogni azione viene scritta in cache/nas_log.txt.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime
from functools import partial
from pathlib import Path

from . import sourcefs, trash
from .organizer import safe_name
from .scanner_metadata import _fs, file_hash

LAYOUTS = ("as_is", "by_album")
UNSORTED = "_senza_album"
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".dng", ".mp4", ".mov", ".3gp", ".m4v"}


class NasError(RuntimeError):
    """Errore di configurazione o di stato del NAS, con messaggio leggibile in italiano."""


NET_PROBE_TIMEOUT = 8.0   # secondi: una condivisione bloccata non deve tenere appeso il server
STALL_LIMIT = 3           # operazioni di rete consecutive in timeout dopo cui il job si ferma


class NasTimeout(OSError):
    """Il NAS non ha risposto entro il tempo previsto."""


def _guarded(fn, timeout: float):
    """Esegue fn() in un thread e aspetta al massimo `timeout` secondi: su SMB bloccato stat/scandir/copy possono
    restare appesi per minuti. Se scade solleva NasTimeout (il thread resta in background, e' un daemon)."""
    box: dict = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001 - rilanciata nel thread chiamante
            box["e"] = e
        finally:
            done.set()

    threading.Thread(target=run, daemon=True, name="nas-io").start()
    if not done.wait(timeout):
        raise NasTimeout("il NAS non risponde (tempo scaduto)")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _io_timeout(size: int | None) -> float:
    """Tempo massimo per copiare o rileggere un file: base fissa + dimensione a 1 MB/s (minimo accettabile)."""
    return 120.0 + (size or 0) / (1024 * 1024)


def space(path: str) -> dict:
    """Spazio libero e totale del disco del NAS. reachable=False se la cartella non risponde."""
    out = {"configured": bool(path), "reachable": False, "free_bytes": None, "total_bytes": None}
    if not path:
        return out
    try:
        def probe():
            if Path(path).is_dir() and not sourcefs.network_mount_missing(path):
                return shutil.disk_usage(_fs(path))
            return None
        usage = _guarded(probe, NET_PROBE_TIMEOUT)
        if usage is not None:
            out.update(reachable=True, free_bytes=usage.free, total_bytes=usage.total)
    except OSError:
        pass
    return out


def require_nas(cfg) -> Path:
    """Cartella NAS configurata e raggiungibile, altrimenti NasError."""
    if not cfg.nas_dir:
        raise NasError("Nessuna cartella NAS impostata.")
    root = Path(cfg.nas_dir)
    try:
        ok = _guarded(lambda: root.is_dir(), NET_PROBE_TIMEOUT)
        missing = ok and _guarded(lambda: sourcefs.network_mount_missing(cfg.nas_dir), NET_PROBE_TIMEOUT)
    except OSError:
        ok, missing = False, False
    if missing:
        raise NasError(f"Il NAS non e' collegato (la condivisione non e' montata): {cfg.nas_dir}. "
                       "Montala e riprova: senza, i file finirebbero sul disco del PC.")
    if not ok:
        raise NasError(f"Il NAS non risponde: {cfg.nas_dir}. Controlla che sia acceso e connesso.")
    return root


def _log(cfg, action: str, detail: str) -> None:
    try:
        with open(Path(cfg.cache_dir) / "nas_log.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{action}\t{detail}\n")
    except OSError:
        pass  # il registro non deve bloccare il lavoro


def _albums_by_file(conn: sqlite3.Connection) -> dict[int, list[tuple[int, str]]]:
    """Album (id, nome) di ogni file ordinati per id album."""
    out: dict[int, list[tuple[int, str]]] = {}
    for r in conn.execute("SELECT ai.file_id, a.id, a.name FROM album_items ai JOIN albums a ON a.id = ai.album_id ORDER BY a.id"):
        out.setdefault(int(r["file_id"]), []).append((int(r["id"]), r["name"]))
    return out


def list_albums(conn: sqlite3.Connection) -> list[dict]:
    """Elenco degli album con numero di file nel catalogo (per il filtro selettivo nella UI NAS)."""
    rows = conn.execute(
        "SELECT a.id, a.name, COUNT(DISTINCT ai.file_id) AS n "
        "FROM albums a JOIN album_items ai ON ai.album_id = a.id "
        "GROUP BY a.id HAVING n > 0 ORDER BY a.name COLLATE NOCASE, a.id"
    ).fetchall()
    return [{"id": int(r["id"]), "name": r["name"], "count": int(r["n"])} for r in rows]


def _under(path: str, root: str) -> bool:
    p, r = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return p.startswith(r.rstrip("\\/") + os.sep)


def _same_folder(a: str, b: str) -> bool:
    """Le due cartelle sono la stessa (anche con nomi di accesso diversi: lettera/UNC, junction, maiuscole)."""
    sep = "\\/"
    if os.path.normcase(os.path.abspath(a)).rstrip(sep) == os.path.normcase(os.path.abspath(b)).rstrip(sep):
        return True
    try:
        return os.path.samefile(_fs(a), _fs(b))
    except OSError:
        return False


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(_fs(a), _fs(b))
    except OSError:
        return False


def target_rel(row, layout: str, source: str, albums: dict[int, list[tuple[int, str]]],
               album_ids: set[int] | None = None) -> str:
    """Percorso relativo sul NAS per un file del catalogo, secondo il layout scelto."""
    if layout == "by_album":
        file_albums = albums.get(int(row["id"]), [])
        if album_ids is not None:
            matched = [name for aid, name in file_albums if aid in album_ids]
            folder = matched[0] if matched else UNSORTED
        else:
            folder = file_albums[0][1] if file_albums else UNSORTED
        return f"{safe_name(folder)}/{row['name']}"
    return os.path.relpath(row["path"], source).replace("\\", "/")


def plan_send(conn: sqlite3.Connection, cfg, layout: str,
              album_ids: list[int] | None = None, include_unsorted: bool = True) -> list[dict]:
    """Per ogni file del catalogo sotto la cartella sorgente: destinazione sul NAS e stato.

    da_copiare: non presente sul NAS. presente: stesso nome e dimensione (hash verificato nel job).
    conflitto: sul NAS c'e' un file con lo stesso nome ma dimensione diversa (non viene toccato).
    In modalita' by_album, album_ids (se fornito) limita l'invio agli album indicati.
    """
    if layout not in LAYOUTS:
        raise NasError("Struttura NAS non valida.")
    root = require_nas(cfg)
    source = cfg.source_dir
    if not source:
        raise NasError("Nessuna cartella sorgente impostata.")
    if _same_folder(str(root), source):
        raise NasError("La cartella NAS coincide con la cartella sorgente: l'invio non avrebbe senso e la pulizia "
                       "eliminerebbe l'unico esemplare. Scegli una cartella NAS diversa.")
    albums = _albums_by_file(conn)
    allowed_albums = {int(x) for x in album_ids} if album_ids is not None else None
    items = []
    used_rels: set[str] = set()
    listing = _DirListing()
    rows = conn.execute(
        "SELECT f.id, f.path, f.name, f.size, f.mtime, f.hash, n.nas_path, n.local_size, n.local_mtime, "
        "n.hash AS nas_hash FROM files f LEFT JOIN nas_copies n ON n.file_id = f.id ORDER BY f.id"
    )
    for r in rows:
        if not _under(r["path"], source) or _under(r["path"], str(root)):
            continue  # fuori dalla sorgente, o gia' dentro la cartella del NAS (sarebbe copia su se stessa)
        if layout == "by_album":
            file_albs = albums.get(int(r["id"]), [])
            if allowed_albums is not None:
                in_allowed = any(aid in allowed_albums for aid, _ in file_albs)
                if not in_allowed and (file_albs or not include_unsorted):
                    continue
            elif not include_unsorted and not file_albs:
                continue
        try:
            st = os.stat(_fs(r["path"]))
        except OSError:
            continue
        if not os.path.isfile(_fs(r["path"])):
            continue
        rel = target_rel(r, layout, source, albums, allowed_albums)
        if layout == "by_album" and rel.lower() in used_rels:
            folder, fname = rel.rsplit("/", 1) if "/" in rel else ("", rel)
            stem, ext = os.path.splitext(fname)
            rel = f"{folder}/{stem}_{int(r['id'])}{ext}" if folder else f"{stem}_{int(r['id'])}{ext}"
        used_rels.add(rel.lower())
        target = root / rel
        exists, nas_size = listing.lookup(target)  # niente stat per file sulla condivisione di rete
        if not exists:
            state = "da_copiare"
        else:
            state = "presente" if nas_size == st.st_size else "conflitto"
        items.append({"file_id": int(r["id"]), "local": r["path"], "name": r["name"], "rel": rel,
                      "target": str(target), "state": state, "size": int(st.st_size),
                      "mtime": st.st_mtime, "known_hash": _known_hash(r, st)})
    return items


class _DirListing:
    """Contenuto delle cartelle del NAS letto con un solo scandir per cartella: sulla condivisione di rete
    ogni exists()/stat() e' un giro di rete, con migliaia di file il piano aspettava minuti."""

    def __init__(self) -> None:
        self._dirs: dict[str, dict[str, int | None] | None] = {}

    def _entries(self, folder: Path) -> dict[str, int | None] | None:
        k = os.path.normcase(str(folder))
        if k not in self._dirs:
            out: dict[str, int | None] | None = {}
            try:
                with os.scandir(_fs(str(folder))) as it:
                    for e in it:
                        try:
                            out[os.path.normcase(e.name)] = e.stat().st_size if e.is_file() else None
                        except OSError:
                            out[os.path.normcase(e.name)] = None
            except FileNotFoundError:
                out = None  # cartella assente sul NAS: tutto da copiare
            except OSError:
                out = _Unknown()  # cartella illeggibile: si ricade sullo stat del singolo file
            self._dirs[k] = out
        return self._dirs[k]

    def lookup(self, path: Path) -> tuple[bool, int | None]:
        """(esiste, dimensione; None se non e' un file normale)."""
        ent = self._entries(path.parent)
        if ent is None:
            return False, None
        if isinstance(ent, _Unknown):
            try:
                st = path.stat()
            except OSError:
                return path.exists(), None
            return True, st.st_size if path.is_file() else None
        k = os.path.normcase(path.name)
        if k not in ent:
            return False, None
        return True, ent[k]


class _Unknown(dict):
    pass


def _known_hash(row, st: os.stat_result) -> str | None:
    """Hash gia' calcolato del file locale (copia NAS verificata o catalogo), se size e mtime non sono
    cambiati da allora. Lo stesso file_hash: rileggerlo darebbe lo stesso valore."""
    if row["nas_hash"] and row["local_size"] == st.st_size and row["local_mtime"] == st.st_mtime:
        return row["nas_hash"]
    if row["hash"] and row["size"] == st.st_size and row["mtime"] == st.st_mtime:
        return row["hash"]
    return None


TMP_SUFFIX = ".fotoscan-tmp"


def _publish_no_overwrite(tmp: Path, dst: Path) -> None:
    """Rende definitivo tmp come dst; FileExistsError se dst e' comparso nel frattempo (mai sovrascritto)."""
    if os.name == "nt":
        os.rename(_fs(str(tmp)), _fs(str(dst)))  # su Windows rename non sovrascrive mai
        return
    try:
        os.link(_fs(str(tmp)), _fs(str(dst)))  # atomico e senza sovrascrittura
    except FileExistsError:
        raise
    except OSError:  # condivisioni senza hard link
        if os.path.lexists(_fs(str(dst))):
            raise FileExistsError(str(dst)) from None
        os.rename(_fs(str(tmp)), _fs(str(dst)))
        return
    os.unlink(_fs(str(tmp)))


def _fsync(path: Path) -> None:
    """Forza la scrittura sul NAS prima di riletterla per l'hash (la rilettura non deve venire dalla cache locale)."""
    try:
        with open(_fs(str(path)), "r+b") as f:
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass  # non tutte le condivisioni lo permettono: l'hash successivo resta la verifica


def copy_verified(src: str, dst: Path, expected_hash: str) -> None:
    """Copia src in dst senza sovrascrivere. Il file finale esiste solo se il suo hash e' quello atteso."""
    if dst.exists():
        raise FileExistsError(str(dst))
    dst.parent.mkdir(parents=True, exist_ok=True)
    # nome temporaneo univoco: due copie verso lo stesso file non usano lo stesso temporaneo
    tmp = dst.with_name(f"{dst.name}.{uuid.uuid4().hex[:10]}{TMP_SUFFIX}")
    try:
        shutil.copyfile(_fs(src), _fs(str(tmp)))
        _fsync(tmp)
        if file_hash(str(tmp)) != expected_hash:
            raise OSError("verifica fallita: la copia non coincide con l'originale")
        _publish_no_overwrite(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _record_copy(conn: sqlite3.Connection, file_id: int, target: str, local: str, hash_: str,
                 size: int | None = None, mtime: float | None = None) -> None:
    """Registra la copia verificata. size/mtime: lo stat letto prima dell'hash; se il file e' cambiato
    nel frattempo la copia non si registra (la pulizia non deve fidarsi di un hash vecchio)."""
    st = os.stat(_fs(local))
    if size is not None and (st.st_size != size or st.st_mtime != mtime):
        raise OSError("il file locale e' cambiato durante la copia")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO nas_copies(file_id, nas_path, local_size, local_mtime, hash, verified) VALUES(?,?,?,?,?,?)",
            (file_id, target, st.st_size, st.st_mtime, hash_, datetime.now().isoformat(timespec="seconds")),
        )


def send_job(job, conn: sqlite3.Connection, cfg, layout: str, verify_all: bool = False,
             album_ids: list[int] | None = None, include_unsorted: bool = True) -> dict:
    """Invia al NAS i file da_copiare e verifica quelli presenti. Ritorna i contatori."""
    items = plan_send(conn, cfg, layout, album_ids=album_ids, include_unsorted=include_unsorted)
    needed = sum(i["size"] for i in items if i["state"] == "da_copiare")
    if needed > 0:
        root = require_nas(cfg)
        try:
            free = _guarded(partial(shutil.disk_usage, _fs(str(root))), NET_PROBE_TIMEOUT).free
        except OSError as e:
            raise NasError(f"Il NAS non risponde: {e}") from e
        if needed > free:
            raise NasError("Spazio insufficiente sul NAS per completare l'invio.")
    known_copies = {
        int(r["file_id"]): (r["nas_path"], r["local_size"], r["local_mtime"])
        for r in conn.execute("SELECT file_id, nas_path, local_size, local_mtime FROM nas_copies")
    }
    todo = [i for i in items if i["state"] in ("da_copiare", "presente")]
    res = {"totale": len(items), "copiati": 0, "verificati": 0, "conflitti": 0, "errori": 0, "annullato": False}
    job.update(stage="invio al NAS", done=0, total=len(todo))
    stalls = 0
    for n, it in enumerate(todo, 1):
        if job.cancel_requested:
            res["annullato"] = True
            break
        copied_now = False
        try:
            if it["state"] == "presente" and not verify_all:
                rec = known_copies.get(it["file_id"])
                if rec is not None:
                    rec_path, rec_size, rec_mtime = rec
                    st = os.stat(_fs(it["local"]))
                    if rec_path == it["target"] and st.st_size == rec_size and st.st_mtime == rec_mtime:
                        res["verificati"] += 1
                        job.update(done=n)
                        continue
            local_hash = it.get("known_hash") or file_hash(it["local"])  # riuso se size+mtime coincidono
            limit = _io_timeout(it["size"])
            if it["state"] == "da_copiare":
                _guarded(partial(copy_verified, it["local"], Path(it["target"]), local_hash), limit)
                res["copiati"] += 1
                copied_now = True
            elif _guarded(partial(file_hash, it["target"]), limit) == local_hash:
                res["verificati"] += 1
            else:
                res["conflitti"] += 1
                _log(cfg, "conflitto", it["target"])
                job.update(done=n)
                continue
            try:
                _record_copy(conn, it["file_id"], it["target"], it["local"], local_hash, it["size"], it.get("mtime"))
            except OSError:
                if copied_now:  # il file locale e' cambiato: la copia appena creata e' di un contenuto vecchio
                    res["copiati"] -= 1
                    try:
                        os.unlink(_fs(it["target"]))
                    except OSError:
                        pass
                raise
            stalls = 0
            _log(cfg, "copia_verificata" if it["state"] == "da_copiare" else "presente_verificato", it["target"])
        except NasTimeout as e:
            res["errori"] += 1
            stalls += 1
            _log(cfg, "errore", f"{it['local']} -> {it['target']}: {e}")
            if stalls >= STALL_LIMIT:
                res["interrotto"] = "Il NAS non risponde: invio interrotto. Controlla la connessione e riprova."
                _log(cfg, "errore", res["interrotto"])
                break
        except (OSError, FileExistsError) as e:
            res["errori"] += 1
            _log(cfg, "errore", f"{it['local']} -> {it['target']}: {e}")
        job.update(done=n)
    for it in items:
        if it["state"] == "conflitto":
            res["conflitti"] += 1
    return res


def receive_job(job, cfg, dest: str) -> dict:
    """Riceve dal NAS i file che non esistono ancora nella cartella di destinazione. Mai sovrascritti."""
    root = require_nas(cfg)
    if not dest:
        raise NasError("Scegli la cartella dove ricevere i file.")
    dest_root = Path(dest)
    files = _walk_media(_fs(str(root)))  # (percorso, relativo, dimensione): uno scandir per cartella
    # Calcola lo spazio necessario solo per i file non ancora presenti in destinazione
    missing_bytes = sum(size for _f, rel, size in files if not (dest_root / rel).exists())
    probe = dest_root
    while not probe.exists() and probe != probe.parent:  # la cartella puo' non esistere ancora
        probe = probe.parent
    if missing_bytes > shutil.disk_usage(_fs(str(probe))).free:
        raise NasError("Spazio insufficiente sulla cartella di destinazione per ricevere i file del NAS.")
    res = {"totale": len(files), "ricevuti": 0, "gia_presenti": 0, "conflitti": 0, "errori": 0, "annullato": False}
    job.update(stage="ricezione dal NAS", done=0, total=len(files))
    stalls = 0
    for n, (full, rel, size) in enumerate(files, 1):
        if job.cancel_requested:
            res["annullato"] = True
            break
        target = dest_root / rel
        try:
            try:
                local_size = os.stat(_fs(str(target))).st_size
            except FileNotFoundError:
                local_size = None
            if local_size is not None and local_size != size:
                # dimensioni diverse: conflitto certo, senza leggere i due file
                res["conflitti"] += 1
                _log(cfg, "conflitto_ricezione", str(target))
                job.update(done=n)
                continue
            limit = _io_timeout(size)
            nas_hash = _guarded(partial(file_hash, full), limit)
            if local_size is not None or target.exists():
                if file_hash(str(target)) == nas_hash:
                    res["gia_presenti"] += 1
                else:
                    res["conflitti"] += 1
                    _log(cfg, "conflitto_ricezione", str(target))
                job.update(done=n)
                continue
            _guarded(partial(copy_verified, full, target, nas_hash), limit)
            res["ricevuti"] += 1
            stalls = 0
            _log(cfg, "ricevuto", str(target))
        except NasTimeout as e:
            res["errori"] += 1
            stalls += 1
            _log(cfg, "errore_ricezione", f"{full}: {e}")
            if stalls >= STALL_LIMIT:
                res["interrotto"] = "Il NAS non risponde: ricezione interrotta. Controlla la connessione e riprova."
                _log(cfg, "errore_ricezione", res["interrotto"])
                break
        except (OSError, FileExistsError) as e:
            res["errori"] += 1
            _log(cfg, "errore_ricezione", f"{full}: {e}")
        job.update(done=n)
    return res


def _walk_media(root: str) -> list[tuple[str, str, int]]:
    """(percorso, relativo con '/', dimensione) dei media sotto root, nell'ordine di os.walk.
    Un solo scandir per cartella: nome, tipo e dimensione insieme (niente getsize per file sulla rete)."""
    out: list[tuple[str, str, int]] = []

    def visit(d: str) -> None:
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            return
        subdirs = []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    subdirs.append(e.path)
                elif e.is_file() and os.path.splitext(e.name)[1].lower() in MEDIA_EXT:
                    out.append((e.path, os.path.relpath(e.path, root).replace("\\", "/"), e.stat().st_size))
            except OSError:
                continue
        for sd in subdirs:
            visit(sd)

    visit(root)
    return out


def cleanup_candidates(conn: sqlite3.Connection) -> list[dict]:
    """File con copia NAS verificata e non modificati dopo la verifica."""
    out = []
    for r in conn.execute(
        "SELECT n.file_id, n.nas_path, n.hash, n.local_size, n.local_mtime, f.name, f.path "
        "FROM nas_copies n JOIN files f ON f.id = n.file_id"
    ):
        try:  # confronta il file reale sul disco (non il catalogo, che puo' essere vecchio)
            st = os.stat(_fs(r["path"]))
        except OSError:
            continue
        if not os.path.isfile(_fs(r["path"])) or st.st_size != r["local_size"] or st.st_mtime != r["local_mtime"]:
            continue
        out.append({"file_id": int(r["file_id"]), "name": r["name"], "local": r["path"],
                    "target": r["nas_path"], "hash": r["hash"], "bytes": int(st.st_size),
                    "size": r["local_size"], "mtime": r["local_mtime"]})
    return out


def cleanup_preview(conn: sqlite3.Connection) -> dict:
    cands = cleanup_candidates(conn)
    return {"count": len(cands), "bytes": sum(c["bytes"] for c in cands),
            "sample": [c["name"] for c in cands[:12]], "ids": [c["file_id"] for c in cands]}


def _is_remote_path(path: str) -> bool:
    """Condivisione di rete o unita' mappata: li' il Cestino non esiste e il sistema cancellerebbe davvero."""
    return sourcefs.is_network(path)


def send_to_recycle(path: str) -> None:
    """Sposta un file nel Cestino del sistema (recuperabile). Fallisce, senza cancellare, se non e' possibile:
    disco di rete, Cestino disattivato o troppo piccolo (vedi fotoscan/trash.py)."""
    trash.send_to_trash(path)


def _forget_faces(conn: sqlite3.Connection, cfg, file_ids: list[int]) -> None:
    """Toglie volti e persone legati ai file che stanno per uscire dal catalogo. Da chiamare PRIMA del DELETE su
    `files`, dentro la stessa transazione e senza commit (faces.forget_files)."""
    try:
        from . import faces
    except Exception:  # noqa: BLE001 - modulo volti non importabile: nulla da ripulire
        return
    forget = getattr(faces, "forget_files", None)
    if forget is not None:
        forget(conn, file_ids, cfg)


def cleanup_job(job, conn: sqlite3.Connection, cfg, expected_count: int,
                expected_ids: list[int] | None = None) -> dict:
    """Sposta nel Cestino i file verificati. Rifiuta se l'elenco e' cambiato dall'anteprima
    (stessi file_id, non solo lo stesso numero)."""
    cands = cleanup_candidates(conn)
    if expected_ids is not None:
        if {c["file_id"] for c in cands} != {int(x) for x in expected_ids}:
            raise NasError(f"L'elenco e' cambiato dall'anteprima ({len(cands)} file ora). Ricontrolla.")
    elif len(cands) != expected_count:
        raise NasError(f"L'elenco e' cambiato dall'anteprima ({len(cands)} file ora, {expected_count} confermati). Ricontrolla.")
    res = {"spostati": 0, "saltati": 0, "errori": 0, "annullato": False}
    job.update(stage="spostamento nel Cestino", done=0, total=len(cands))
    stalls = 0
    for n, c in enumerate(cands, 1):
        if job.cancel_requested:
            res["annullato"] = True
            break
        try:
            if _same_file(c["local"], c["target"]):  # il "backup" e' il file stesso: eliminarlo = perderlo
                res["saltati"] += 1
                _log(cfg, "saltato_stesso_file", c["local"])
                job.update(done=n)
                continue
            # riverifica adesso: la copia sul NAS si rilegge sempre (hash); il file locale si rilegge solo se
            # size o mtime sono cambiati dalla verifica, altrimenti l'hash registrato e' ancora il suo
            try:
                lst = os.stat(_fs(c["local"]))
                local_same = lst.st_size == c["size"] and lst.st_mtime == c["mtime"]
            except OSError:
                local_same = None
            nas_ok = _guarded(partial(_nas_copy_valid, c["target"], c["hash"]), _io_timeout(c["bytes"]))
            stalls = 0
            if not nas_ok:
                res["saltati"] += 1
                _log(cfg, "saltato_copia_non_valida", c["target"])
            elif local_same is None or (not local_same and file_hash(c["local"]) != c["hash"]):
                res["saltati"] += 1
                _log(cfg, "saltato_locale_cambiato", c["local"])
            else:
                send_to_recycle(c["local"])
                with conn:
                    _forget_faces(conn, cfg, [c["file_id"]])
                    conn.execute("DELETE FROM nas_copies WHERE file_id=?", (c["file_id"],))
                    conn.execute("DELETE FROM files WHERE id=?", (c["file_id"],))
                res["spostati"] += 1
                _log(cfg, "cestino", c["local"])
        except NasTimeout as e:
            res["errori"] += 1
            stalls += 1
            _log(cfg, "errore_cestino", f"{c['local']}: {e}")
            if stalls >= STALL_LIMIT:
                res["interrotto"] = "Il NAS non risponde: pulizia interrotta. Nessun altro file e' stato eliminato."
                _log(cfg, "errore_cestino", res["interrotto"])
                break
        except (OSError, NasError) as e:
            res["errori"] += 1
            _log(cfg, "errore_cestino", f"{c['local']}: {e}")
        job.update(done=n)
    return res


def _nas_copy_valid(target: str, expected_hash: str) -> bool:
    return os.path.isfile(_fs(target)) and file_hash(target) == expected_hash
