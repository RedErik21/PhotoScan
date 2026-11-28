"""Duplicati: gruppi dalla tabella plan (is_dup / dup_of), scambio del master, riepilogo.

Un gruppo = un master + tutti i suoi duplicati (is_dup=1 identico per hash, is_dup=2 quasi uguale).
Lo scambio del master aggiorna plan (is_dup, dup_of, folder), scrive file_override per le cartelle
e ricorda i ruoli in `dup_keep`. Se il piano viene ricalcolato, build_plan perde i ruoli: reapply()
(chiamato da ogni lettura) li ripristina, ma solo se il gruppo e le cartelle sono ancora quelli
scritti. Nessun file su disco viene toccato.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db as dbm
from ..organizer import DUP_FOLDER, SIMILI_FOLDER, move_files
from . import deps, jobs, routes_cleanup

router = APIRouter(prefix="/api", tags=["duplicati"])

_MODIF = re.compile(r"^(?P<base>.+?)[-_ ](modificato|modificata)$", re.IGNORECASE)
Roles = dict[int, tuple[int, int | None]]  # file_id -> (is_dup, dup_of)

_KEEP_SCHEMA = """
CREATE TABLE IF NOT EXISTS dup_keep(
  file_id INTEGER PRIMARY KEY,
  is_dup INTEGER NOT NULL, dup_of INTEGER,
  cluster TEXT NOT NULL,        -- id di tutto il gruppo, ordinati e separati da virgola
  expect_folder TEXT,           -- file_override atteso per il file (NULL = nessuno)
  prev_folder TEXT,             -- file_override che il file aveva PRIMA dello scambio (NULL = nessuno)
  touched INTEGER DEFAULT 0     -- 1 = l'override di questo file l'ha scritto lo scambio (da annullare se non vale piu')
)"""
JOB_PLAN = "piano"


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(_KEEP_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dup_keep)")}
    for col, spec in (("prev_folder", "TEXT"), ("touched", "INTEGER DEFAULT 0")):  # cataloghi creati prima
        if col not in cols:
            conn.execute(f"ALTER TABLE dup_keep ADD COLUMN {col} {spec}")
    conn.commit()


def _ensure_idle() -> None:
    """Le modifiche ai gruppi non convivono con il ricalcolo del piano (che riscrive tutta la tabella plan)."""
    if jobs.running_job(JOB_PLAN) is not None:
        raise HTTPException(409, "Il piano e' in ricalcolo: riprova tra un momento.")


def _csv(ids) -> str:
    return ",".join(str(i) for i in sorted(ids))


def _clusters(roles: Roles) -> dict[int, set[int]]:
    """master -> insieme di id (master incluso) per ogni gruppo con almeno un duplicato."""
    out: dict[int, set[int]] = {}
    for fid, (d, of) in roles.items():
        if d and of is not None:
            out.setdefault(of, {of}).add(fid)
    return out


def _group_key(raw: Roles, raw_cl: dict[int, set[int]], fid: int) -> str:
    """Chiave del gruppo GREZZO (quello del piano, senza scambi) a cui appartiene `fid`."""
    root = raw[fid][1] or fid
    return _csv(raw_cl.get(root, {root}))


def _native_key(conn: sqlite3.Connection, raw: Roles, mem: set[int], root: int) -> str:
    """Chiave da scrivere in dup_keep.cluster. La tabella plan contiene gia' gli scambi precedenti, quindi se un
    membro ha una riga dup_keep se ne riusa la chiave (quella del gruppo nativo, come lo ricalcola build_plan)."""
    q = ",".join("?" * len(mem))
    row = conn.execute(f"SELECT cluster FROM dup_keep WHERE file_id IN ({q}) LIMIT 1", sorted(mem)).fetchone()
    if row:
        return row["cluster"]
    return _group_key(raw, _clusters(raw), root)


def _prev_touched(conn: sqlite3.Connection, fid: int, overrides: dict, touched_now: bool) -> tuple[str | None, int]:
    """(prev_folder, touched) da scrivere in dup_keep. Se uno scambio precedente aveva gia' scritto l'override del
    file, l'override 'originale' resta quello di prima di quel primo scambio."""
    old = conn.execute("SELECT prev_folder, touched FROM dup_keep WHERE file_id=?", (fid,)).fetchone()
    if old is not None and old["touched"]:
        return old["prev_folder"], 1
    return overrides.get(fid), 1 if touched_now else 0


def _validity(raw: Roles, keeps, folders: dict[int, str]) -> dict[str, bool]:
    """Un gruppo scambiato vale solo se e' lo stesso di quando lo scambio e' stato fatto (stessi membri)
    e nessun file e' stato spostato a mano da allora."""
    raw_cl = _clusters(raw)
    valid: dict[str, bool] = {}
    for k in keeps:
        fid = k["file_id"]
        ok = fid in raw and _group_key(raw, raw_cl, fid) == k["cluster"] and folders.get(fid) == k["expect_folder"]
        valid[k["cluster"]] = valid.get(k["cluster"], True) and ok
    return valid


def _load(conn: sqlite3.Connection) -> tuple[Roles, Roles, dict[int, set[int]]]:
    """(ruoli nel piano, ruoli effettivi dopo gli scambi, gruppi effettivi)."""
    raw: Roles = {r["file_id"]: (int(r["is_dup"]), r["dup_of"])
                  for r in conn.execute("SELECT file_id, COALESCE(is_dup,0) AS is_dup, dup_of FROM plan")}
    eff = dict(raw)
    keeps = conn.execute("SELECT file_id, is_dup, dup_of, cluster, expect_folder FROM dup_keep").fetchall()
    if keeps:
        folders = {r["file_id"]: r["folder"] for r in conn.execute("SELECT file_id, folder FROM file_override")}
        valid = _validity(raw, keeps, folders)
        for k in keeps:
            if valid[k["cluster"]] and k["file_id"] in raw:
                eff[k["file_id"]] = (int(k["is_dup"]), k["dup_of"])
    return raw, eff, _clusters(eff)


def release_stale(conn: sqlite3.Connection, raw: Roles) -> int:
    """Chiamata da build_plan PRIMA di applicare gli override, con i ruoli grezzi del piano nuovo.
    Per ogni scambio non piu' valido (gruppo cambiato, file spostato a mano o sparito) toglie le righe dup_keep
    e ripristina gli override che lo scambio aveva scritto (quelli che l'utente ha poi cambiato restano)."""
    _ensure(conn)
    keeps = conn.execute("SELECT file_id, cluster, expect_folder, prev_folder, touched FROM dup_keep").fetchall()
    if not keeps:
        return 0
    folders = {r["file_id"]: r["folder"] for r in conn.execute("SELECT file_id, folder FROM file_override")}
    valid = _validity(raw, keeps, folders)
    stale = [k for k in keeps if not valid[k["cluster"]]]
    for k in stale:
        fid = k["file_id"]
        if k["touched"] and k["expect_folder"] is not None and folders.get(fid) == k["expect_folder"]:
            if k["prev_folder"] is not None:
                conn.execute("UPDATE file_override SET folder=? WHERE file_id=?", (k["prev_folder"], fid))
            else:
                conn.execute("DELETE FROM file_override WHERE file_id=?", (fid,))
        conn.execute("DELETE FROM dup_keep WHERE file_id=?", (fid,))
    if stale:
        conn.commit()
    return len(stale)


def _write_back(conn: sqlite3.Connection, raw: Roles, eff: Roles) -> int:
    diff = [(fid, d, of) for fid, (d, of) in eff.items() if (d, of) != raw[fid]]
    if diff:
        conn.executemany("UPDATE plan SET is_dup=?, dup_of=? WHERE file_id=?", [(d, of, fid) for fid, d, of in diff])
        conn.commit()
    return len(diff)


def reapply(conn: sqlite3.Connection) -> int:
    """Riporta in plan i ruoli scambiati (dopo un ricalcolo del piano). Idempotente.
    Va chiamata anche in fondo a organizer.build_plan per coerenza immediata con la galleria."""
    _ensure(conn)
    raw, eff, _ = _load(conn)
    return _write_back(conn, raw, eff)


def _files_by_id(conn: sqlite3.Connection, ids) -> dict[int, dict]:
    ids = sorted(ids)
    out: dict[int, dict] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        for r in conn.execute(deps.SELECT_FILE + f"WHERE f.id IN ({q})", chunk):
            out[r["id"]] = deps.file_to_dict(r)
    return out


def _vectors(conn: sqlite3.Connection, ids) -> dict[int, np.ndarray]:
    ids = sorted(ids)
    out: dict[int, np.ndarray] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT file_id, vec FROM embeddings WHERE file_id IN ({q})", chunk):
            v = dbm.from_blob(r["vec"])
            out[r["file_id"]] = v / (np.linalg.norm(v) + 1e-9)
    return out


def _is_name_variant(member: dict, master: dict) -> bool:
    """Stesso schema della regola 'nome' di organizer: 'X-modificato' accanto a 'X' nella stessa cartella."""
    m = _MODIF.match(Path(member["name"]).stem)
    same_dir = os.path.normcase(str(Path(member["path"]).parent)) == os.path.normcase(str(Path(master["path"]).parent))
    return bool(m) and same_dir and m.group("base").lower() == Path(master["name"]).stem.lower()


def _member_similarity(root: int, m: dict, vecs: dict) -> float | None:
    """Coseno tra il master e il membro. I duplicati esatti valgono 1 per definizione."""
    if m["is_dup"] == 1:
        return 1.0
    if m["is_dup"] == 2 and root in vecs and m["id"] in vecs:
        return round(float(vecs[root] @ vecs[m["id"]]), 3)
    return None


def _group_view(root: int, mem: set[int], eff: Roles, files: dict, vecs: dict) -> dict:
    master = dict(files[root], is_dup=0, dup_of=None)
    members = []
    for x in sorted(mem - {root}):
        d = dict(files[x], is_dup=eff[x][0], dup_of=root)
        d["similarity"] = _member_similarity(root, d, vecs)  # per ogni foto, non il minimo del gruppo
        members.append(d)
    # similarita' del gruppo = minimo tra i quasi-uguali (is_dup=2)
    sims = [m["similarity"] for m in members if m["is_dup"] == 2 and m["similarity"] is not None]
    if any(m["is_dup"] == 1 for m in members):
        reason = "hash"
    elif members and all(_is_name_variant(m, master) for m in members):
        reason = "nome"
    else:
        reason = "immagine"
    similarity = round(min(sims), 3) if sims else (1.0 if reason == "hash" else None)
    return {"master": master, "members": members, "reason": reason, "similarity": similarity}


# ------------------------------------------------------------------ API
@router.get("/duplicates")
def list_duplicates(
    type: str = Query("exact"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    conn: sqlite3.Connection = Depends(deps.get_db),
):
    if type not in ("exact", "similar"):
        raise HTTPException(400, "tipo non valido: usa 'exact' o 'similar'")
    _ensure(conn)
    raw, eff, clusters = _load(conn)
    if jobs.running_job(JOB_PLAN) is None:  # durante il ricalcolo il piano si sta riscrivendo: solo lettura
        _write_back(conn, raw, eff)
    want = 1 if type == "exact" else 2
    roots = sorted(r for r, mem in clusters.items() if any(eff[x][0] == want for x in mem if x != r))
    page = roots[offset:offset + limit]
    ids = set().union(*(clusters[r] for r in page)) if page else set()
    files = _files_by_id(conn, ids)
    vecs = _vectors(conn, ids)
    groups = [_group_view(r, clusters[r], eff, files, vecs) for r in page]
    return {"total": len(roots), "groups": groups}


class KeepBody(BaseModel):
    master_id: int
    member_ids: list[int] = Field(default_factory=list, max_length=20000)


@router.post("/duplicates/keep")
def keep_master(body: KeepBody, conn: sqlite3.Connection = Depends(deps.get_db)):
    """Sceglie `master_id` come nuovo originale del gruppo. `member_ids` deve elencare tutti gli
    altri membri del gruppo (il vecchio master incluso): e' un controllo contro gruppi cambiati."""
    cfg = deps.get_cfg()
    _ensure_idle()
    _ensure(conn)
    raw, eff, clusters = _load(conn)
    root = next((r for r, mem in clusters.items() if body.master_id in mem), None)
    if root is None:
        raise HTTPException(404, "il file non fa parte di un gruppo di duplicati")
    members = clusters[root]
    if body.master_id == root:
        return {"changed": False, "master_id": root, "members": sorted(members - {root})}
    if set(body.member_ids) | {body.master_id} != members:
        raise HTTPException(409, "il gruppo e' cambiato: aggiorna la pagina e riprova")
    if cfg.dup_mode == "skip":
        raise HTTPException(409, "con 'salta i duplicati' non e' possibile scegliere un altro originale")
    row = conn.execute("SELECT folder FROM plan WHERE file_id=?", (root,)).fetchone()
    if not row or not row["folder"]:
        raise HTTPException(409, "cartella dell'originale non disponibile: ricalcola il piano")

    overrides = {r["file_id"]: r["folder"] for r in conn.execute("SELECT file_id, folder FROM file_override")}
    _swap(conn, eff, root, members, body.master_id, row["folder"], overrides, _native_key(conn, raw, members, root))
    return {"changed": True, "master_id": body.master_id, "members": sorted(members - {body.master_id})}


def _swap(conn: sqlite3.Connection, eff: Roles, root: int, members: set[int], master_id: int,
          keep_folder: str, overrides: dict, cluster: str | None = None) -> None:
    """Scrive lo scambio del master (plan, file_override, dup_keep) e aggiorna `eff` in memoria.
    I controlli (gruppo, cartelle, dup_mode) sono gia' stati fatti dal chiamante."""
    new_kind = eff[master_id][0] or 1
    demoted_folder = DUP_FOLDER if new_kind == 1 else SIMILI_FOLDER
    move_files(conn, [master_id], keep_folder)  # file_override: il nuovo master torna nella sua cartella
    move_files(conn, [root], demoted_folder)    # il vecchio master diventa duplicato

    expect = {x: overrides.get(x) for x in members}
    expect.update({master_id: keep_folder, root: demoted_folder})
    cluster = cluster or _csv(members)  # chiave del gruppo NATIVO: resta valida al ricalcolo del piano
    for x in members:
        if x == master_id:
            d, of = 0, None
        elif x == root:
            d, of = new_kind, master_id
        else:
            d, of = (eff[x][0] or 1), master_id
        conn.execute("UPDATE plan SET is_dup=?, dup_of=? WHERE file_id=?", (d, of, x))
        conn.execute(
            "INSERT OR REPLACE INTO dup_keep(file_id,is_dup,dup_of,cluster,expect_folder,prev_folder,touched) "
            "VALUES(?,?,?,?,?,?,?)",
            (x, d, of, cluster, expect[x], *_prev_touched(conn, x, overrides, x in (master_id, root))),
        )
        eff[x] = (d, of)
    conn.commit()


def _master_available(conn: sqlite3.Connection, fid: int) -> bool:
    """L'originale di un gruppo esiste ancora sul disco e non e' segnalato come mancante/illeggibile."""
    row = conn.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
    if row is None or not os.path.exists(row["path"]):
        return False
    return conn.execute("SELECT 1 FROM broken WHERE file_id=?", (fid,)).fetchone() is None


class BulkBody(BaseModel):
    dry_run: bool = False


class KeepManyBody(BaseModel):
    file_ids: list[int] = Field(default_factory=list, max_length=20000)


class TrashCopiesBody(BaseModel):
    type: str = "exact"
    dry_run: bool = False
    confirm: bool = False


@router.post("/duplicates/keep-many")
def keep_many(body: KeepManyBody, conn: sqlite3.Connection = Depends(deps.get_db)):
    """Segna uno o piu' file (anche tutto un gruppo) come foto normali da conservare (is_dup=0, dup_of=NULL)."""
    _ensure_idle()
    _ensure(conn)
    raw, eff, clusters = _load(conn)
    _write_back(conn, raw, eff)
    want_keep = {int(x) for x in body.file_ids}
    if not want_keep:
        raise HTTPException(400, "Nessun file indicato.")
    folders = {r["file_id"]: r["folder"] for r in conn.execute("SELECT file_id, folder FROM plan")}
    overrides = {r["file_id"]: r["folder"] for r in conn.execute("SELECT file_id, folder FROM file_override")}

    kept_count = 0
    groups_updated = 0
    for root, mem in clusters.items():
        hits = mem & want_keep
        if not hits:
            continue
        keep_folder = folders.get(root)
        to_promote = [fid for fid in hits if eff[fid][0] != 0]
        if not to_promote:
            continue
        groups_updated += 1
        kept_count += len(to_promote)
        if keep_folder:
            move_files(conn, to_promote, keep_folder)
        expect = {x: overrides.get(x) for x in mem}
        if keep_folder:
            for fid in to_promote:
                expect[fid] = keep_folder
        cluster = _native_key(conn, raw, mem, root)
        for x in mem:
            if x in to_promote:
                d, of = 0, None
            else:
                d, of = eff[x]
            conn.execute("UPDATE plan SET is_dup=?, dup_of=? WHERE file_id=?", (d, of, x))
            conn.execute(
                "INSERT OR REPLACE INTO dup_keep(file_id,is_dup,dup_of,cluster,expect_folder,prev_folder,touched) "
                "VALUES(?,?,?,?,?,?,?)",
                (x, d, of, cluster, expect[x], *_prev_touched(conn, x, overrides, bool(x in to_promote and keep_folder))),
            )
            eff[x] = (d, of)
    if kept_count:
        conn.commit()
    return {"kept": kept_count, "groups_updated": groups_updated}


@router.post("/duplicates/trash-copies")
def trash_all_copies(body: TrashCopiesBody, conn: sqlite3.Connection = Depends(deps.get_db)):
    """Sposta nel Cestino tutte le copie extra del tipo scelto ('exact' o 'similar'), tenendo gli originali.
    I gruppi il cui originale non e' piu' sul disco (o e' illeggibile) vengono SALTATI: cestinare le copie
    farebbe perdere l'unico file rimasto."""
    if body.type not in ("exact", "similar"):
        raise HTTPException(400, "tipo non valido: usa 'exact' o 'similar'")
    _ensure_idle()
    _ensure(conn)
    raw, eff, clusters = _load(conn)
    _write_back(conn, raw, eff)
    want = 1 if body.type == "exact" else 2
    sizes = {r["id"]: int(r["size"] or 0) for r in conn.execute("SELECT id, size FROM files")}
    copy_ids: list[int] = []
    groups = skipped = 0
    for r, mem in clusters.items():
        extra = [x for x in sorted(mem) if x != r and eff[x][0] == want]
        if extra:
            if not _master_available(conn, r):
                skipped += 1
                continue
            groups += 1
            copy_ids.extend(extra)
    total_bytes = sum(sizes.get(x, 0) for x in copy_ids)
    if body.dry_run:
        return {"groups": groups, "files": len(copy_ids), "bytes": total_bytes, "skipped_groups": skipped,
                "dry_run": True}
    if not body.confirm:
        raise HTTPException(400, "Serve la conferma esplicita per spostare le copie nel Cestino.")
    if not copy_ids:
        return {"trashed": 0, "freed_bytes": 0, "errors": [], "groups": 0, "missing": 0, "skipped_groups": skipped}
    res = routes_cleanup.trash_file_ids(conn, copy_ids)
    res["groups"] = groups
    res["skipped_groups"] = skipped
    return res


@router.post("/duplicates/keep-largest")
def keep_largest_all(body: BulkBody, conn: sqlite3.Connection = Depends(deps.get_db)):
    """In ogni gruppo la foto piu' grande diventa l'originale (a parita' resta quella attuale).
    Stessa logica di keep_master, in una sola chiamata. Con dry_run=true conta soltanto."""
    cfg = deps.get_cfg()
    _ensure_idle()
    _ensure(conn)
    raw, eff, clusters = _load(conn)
    _write_back(conn, raw, eff)
    if cfg.dup_mode == "skip":
        raise HTTPException(409, "con 'salta i duplicati' non e' possibile scegliere un altro originale")
    sizes = {r["id"]: r["size"] or 0 for r in conn.execute("SELECT id, size FROM files")}
    folders = {r["file_id"]: r["folder"] for r in conn.execute("SELECT file_id, folder FROM plan")}
    todo = []
    for root, mem in clusters.items():
        best = max(mem, key=lambda x: (sizes.get(x, 0), x == root, -x))
        if best != root and folders.get(root):
            todo.append((root, mem, best))
    if body.dry_run:
        return {"groups": len(clusters), "changed": len(todo), "dry_run": True}
    overrides = {r["file_id"]: r["folder"] for r in conn.execute("SELECT file_id, folder FROM file_override")}
    for root, mem, best in todo:
        _swap(conn, eff, root, mem, best, folders[root], overrides, _native_key(conn, raw, mem, root))
    return {"groups": len(clusters), "changed": len(todo), "dry_run": False}


class ReviewBody(BaseModel):
    value: bool


@router.post("/duplicates/reviewed")
def set_reviewed(body: ReviewBody, conn: sqlite3.Connection = Depends(deps.get_db)):
    """Segna (o riapre) il controllo dei duplicati: scelta dell'utente, non si azzera da sola."""
    dbm.set_meta(conn, "dup_reviewed", "1" if body.value else "0")
    return {"reviewed": body.value}


@router.get("/duplicates/summary")
def duplicates_summary(conn: sqlite3.Connection = Depends(deps.get_db)):
    _ensure(conn)
    raw, eff, clusters = _load(conn)
    if jobs.running_job(JOB_PLAN) is None:
        _write_back(conn, raw, eff)
    sizes = {r["id"]: r["size"] or 0 for r in conn.execute("SELECT id, size FROM files")}
    out: dict = {}
    for key, want in (("exact", 1), ("similar", 2)):
        groups = files = total = 0
        for r, mem in clusters.items():
            extra = [x for x in mem if x != r and eff[x][0] == want]
            if extra:
                groups += 1
                files += len(extra)
                total += sum(sizes.get(x, 0) for x in extra)
        out[key] = {"groups": groups, "files": files, "bytes": total}
    out["reviewed"] = dbm.get_meta(conn, "dup_reviewed", "0") == "1"
    return out
