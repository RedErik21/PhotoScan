"""NAS: backup verificato del catalogo, ricezione dal NAS e pulizia sicura del PC.

Regole (non negoziabili):
- Nessuna copia sovrascrive un file esistente. Se la destinazione esiste ma non coincide, e' un conflitto.
- Una copia e' valida solo se l'hash della destinazione, riletta dal disco, coincide con quello della sorgente.
- Dal PC si elimina solo un file la cui copia sul NAS e' verificata adesso (hash riletto), e solo dopo
  una conferma esplicita con il numero di file mostrato. I file vanno nel Cestino di Windows.
- Ogni azione viene scritta in cache/nas_log.txt.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import sqlite3
import uuid
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from .organizer import safe_name
from .scanner_metadata import _fs, file_hash

LAYOUTS = ("as_is", "by_album")
UNSORTED = "_senza_album"
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".dng", ".mp4", ".mov", ".3gp", ".m4v"}


class NasError(RuntimeError):
    """Errore di configurazione o di stato del NAS, con messaggio leggibile in italiano."""


def space(path: str) -> dict:
    """Spazio libero e totale del disco del NAS. reachable=False se la cartella non risponde."""
    out = {"configured": bool(path), "reachable": False, "free_bytes": None, "total_bytes": None}
    if not path:
        return out
    try:
        if Path(path).is_dir():
            usage = shutil.disk_usage(_fs(path))
            out.update(reachable=True, free_bytes=usage.free, total_bytes=usage.total)
    except OSError:
        pass
    return out


def require_nas(cfg) -> Path:
    """Cartella NAS configurata e raggiungibile, altrimenti NasError."""
    if not cfg.nas_dir:
        raise NasError("Nessuna cartella NAS impostata.")
    root = Path(cfg.nas_dir)
    if not root.is_dir():
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
        if not _under(r["path"], source):
            continue
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


def copy_verified(src: str, dst: Path, expected_hash: str) -> None:
    """Copia src in dst senza sovrascrivere. Il file finale esiste solo se il suo hash e' quello atteso."""
    if dst.exists():
        raise FileExistsError(str(dst))
    dst.parent.mkdir(parents=True, exist_ok=True)
    # nome temporaneo univoco: due copie verso lo stesso file non usano lo stesso temporaneo
    tmp = dst.with_name(f"{dst.name}.{uuid.uuid4().hex[:10]}{TMP_SUFFIX}")
    try:
        shutil.copyfile(_fs(src), _fs(str(tmp)))
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
        free = shutil.disk_usage(_fs(str(require_nas(cfg)))).free
        if needed > free:
            raise NasError("Spazio insufficiente sul NAS per completare l'invio.")
    known_copies = {
        int(r["file_id"]): (r["nas_path"], r["local_size"], r["local_mtime"])
        for r in conn.execute("SELECT file_id, nas_path, local_size, local_mtime FROM nas_copies")
    }
    todo = [i for i in items if i["state"] in ("da_copiare", "presente")]
    res = {"totale": len(items), "copiati": 0, "verificati": 0, "conflitti": 0, "errori": 0, "annullato": False}
    job.update(stage="invio al NAS", done=0, total=len(todo))
    for n, it in enumerate(todo, 1):
        if job.cancel_requested:
            res["annullato"] = True
            break
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
            if it["state"] == "da_copiare":
                copy_verified(it["local"], Path(it["target"]), local_hash)
                res["copiati"] += 1
            elif file_hash(it["target"]) == local_hash:
                res["verificati"] += 1
            else:
                res["conflitti"] += 1
                _log(cfg, "conflitto", it["target"])
                job.update(done=n)
                continue
            _record_copy(conn, it["file_id"], it["target"], it["local"], local_hash, it["size"], it.get("mtime"))
            _log(cfg, "copia_verificata" if it["state"] == "da_copiare" else "presente_verificato", it["target"])
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
    dest_root = Path(dest)
    if not dest:
        raise NasError("Scegli la cartella dove ricevere i file.")
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
            nas_hash = file_hash(full)
            if local_size is not None or target.exists():
                if file_hash(str(target)) == nas_hash:
                    res["gia_presenti"] += 1
                else:
                    res["conflitti"] += 1
                    _log(cfg, "conflitto_ricezione", str(target))
                job.update(done=n)
                continue
            copy_verified(full, target, nas_hash)
            res["ricevuti"] += 1
            _log(cfg, "ricevuto", str(target))
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
            "sample": [c["name"] for c in cands[:12]]}


def _is_remote_path(path: str) -> bool:
    """Condivisione di rete o unita' mappata: li' il Cestino non esiste e Windows cancellerebbe davvero."""
    if path.startswith("\\\\"):
        return True
    drive = os.path.splitdrive(path)[0]
    if not drive:
        return False
    return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE


def send_to_recycle(path: str) -> None:
    """Sposta un file nel Cestino di Windows (recuperabile). Solo Windows, solo dischi locali."""
    if os.name != "nt":
        raise NasError("Lo spostamento nel Cestino e' disponibile solo su Windows.")
    full = os.path.abspath(path)
    if _is_remote_path(full):
        raise NasError("Il file e' su un disco di rete: li' il Cestino non esiste, non lo elimino.")

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
    # pFrom e' un elenco di percorsi chiuso da DUE caratteri nulli: buffer esplicito, non affidato a ctypes
    buf = ctypes.create_unicode_buffer(full + "\0\0")
    op = SHFILEOPSTRUCTW(hwnd=None, wFunc=FO_DELETE, pFrom=ctypes.cast(buf, wintypes.LPCWSTR),
                         pTo=None, fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
                         fAnyOperationsAborted=False, hNameMappings=None, lpszProgressTitle=None)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code != 0 or op.fAnyOperationsAborted:
        raise OSError(f"spostamento nel Cestino non riuscito (codice {code})")
    if os.path.lexists(full):
        raise OSError("spostamento nel Cestino non riuscito: il file e' ancora al suo posto")


def cleanup_job(job, conn: sqlite3.Connection, cfg, expected_count: int) -> dict:
    """Sposta nel Cestino i file verificati. Rifiuta se l'elenco e' cambiato dall'anteprima."""
    cands = cleanup_candidates(conn)
    if len(cands) != expected_count:
        raise NasError(f"L'elenco e' cambiato dall'anteprima ({len(cands)} file ora, {expected_count} confermati). Ricontrolla.")
    res = {"spostati": 0, "saltati": 0, "errori": 0, "annullato": False}
    job.update(stage="spostamento nel Cestino", done=0, total=len(cands))
    for n, c in enumerate(cands, 1):
        if job.cancel_requested:
            res["annullato"] = True
            break
        try:
            # riverifica adesso: la copia sul NAS si rilegge sempre (hash); il file locale si rilegge solo se
            # size o mtime sono cambiati dalla verifica, altrimenti l'hash registrato e' ancora il suo
            try:
                lst = os.stat(_fs(c["local"]))
                local_same = lst.st_size == c["size"] and lst.st_mtime == c["mtime"]
            except OSError:
                local_same = None
            if not os.path.isfile(_fs(c["target"])) or file_hash(c["target"]) != c["hash"]:
                res["saltati"] += 1
                _log(cfg, "saltato_copia_non_valida", c["target"])
            elif local_same is None or (not local_same and file_hash(c["local"]) != c["hash"]):
                res["saltati"] += 1
                _log(cfg, "saltato_locale_cambiato", c["local"])
            else:
                send_to_recycle(c["local"])
                with conn:
                    conn.execute("DELETE FROM nas_copies WHERE file_id=?", (c["file_id"],))
                    conn.execute("DELETE FROM files WHERE id=?", (c["file_id"],))
                res["spostati"] += 1
                _log(cfg, "cestino", c["local"])
        except (OSError, NasError) as e:
            res["errori"] += 1
            _log(cfg, "errore_cestino", f"{c['local']}: {e}")
        job.update(done=n)
    return res
