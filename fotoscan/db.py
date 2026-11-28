"""Catalogo SQLite (catalogo.db): metadati, embedding, volti, piano e override."""
from __future__ import annotations

import sqlite3
import threading
import zlib
from collections import Counter

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,              -- image | video
  size INTEGER, mtime REAL,
  hash TEXT,
  date_taken TEXT,                 -- ISO YYYY-MM-DDTHH:MM:SS
  date_source TEXT,                -- exif | filename | video | mtime
  data_incerta INTEGER DEFAULT 0,
  lat REAL, lon REAL,
  width INTEGER, height INTEGER, duration REAL,
  status TEXT DEFAULT 'new',       -- new | meta | embedded | error
  error TEXT
);
CREATE INDEX IF NOT EXISTS ix_files_status ON files(status);
CREATE INDEX IF NOT EXISTS ix_files_hash ON files(hash);

CREATE TABLE IF NOT EXISTS embeddings(
  file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS label_cache(
  prompt TEXT PRIMARY KEY, vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS persons(
  id INTEGER PRIMARY KEY,
  name TEXT,
  created TEXT,
  centroid BLOB,
  n INTEGER DEFAULT 0,
  cover_face_id INTEGER,
  cover_locked INTEGER DEFAULT 0,
  hidden INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS faces(
  id INTEGER PRIMARY KEY,
  file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
  bbox TEXT, emb BLOB, cluster INTEGER,
  person_id INTEGER REFERENCES persons(id) ON DELETE SET NULL,
  locked INTEGER DEFAULT 0,
  excluded INTEGER DEFAULT 0,
  det_score REAL,
  quality REAL,
  landmarks TEXT
);
CREATE INDEX IF NOT EXISTS ix_faces_file ON faces(file_id);
CREATE TABLE IF NOT EXISTS faces_done(file_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS favorites(file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE, added TEXT);
CREATE TABLE IF NOT EXISTS albums(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, created TEXT, cover_file_id INTEGER
);
CREATE TABLE IF NOT EXISTS album_items(
  album_id INTEGER REFERENCES albums(id) ON DELETE CASCADE,
  file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
  added TEXT, PRIMARY KEY(album_id, file_id)
);
CREATE INDEX IF NOT EXISTS ix_files_date ON files(date_taken);
CREATE INDEX IF NOT EXISTS ix_files_status_date ON files(status, date_taken);
CREATE TABLE IF NOT EXISTS plan(
  file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  folder TEXT,                     -- NULL = non copiare
  context TEXT, score REAL,
  is_dup INTEGER DEFAULT 0, dup_of INTEGER
);
CREATE INDEX IF NOT EXISTS ix_plan_folder ON plan(folder);
CREATE INDEX IF NOT EXISTS ix_plan_dup_of ON plan(dup_of);
CREATE INDEX IF NOT EXISTS ix_album_items_file ON album_items(file_id);
CREATE TABLE IF NOT EXISTS rename_map(old TEXT PRIMARY KEY, new TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS file_override(file_id INTEGER PRIMARY KEY, folder TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
-- etichette da esempi (vettore medio di un gruppo di foto) e suggerimenti di nuovi temi
CREATE TABLE IF NOT EXISTS visual_labels(
  name TEXT PRIMARY KEY, vec BLOB NOT NULL, thr REAL NOT NULL, n INTEGER, sample TEXT, created TEXT
);
CREATE TABLE IF NOT EXISTS label_suggestions(
  id INTEGER PRIMARY KEY, name TEXT, alt TEXT, vec BLOB NOT NULL, thr REAL, n INTEGER, days INTEGER,
  months INTEGER, sample TEXT, created TEXT
);
CREATE TABLE IF NOT EXISTS label_dismissed(id INTEGER PRIMARY KEY, vec BLOB NOT NULL);
-- unioni di persone annullabili: payload = stato precedente (persone assorbite, destinazione, volti spostati)
CREATE TABLE IF NOT EXISTS merge_log(
  id INTEGER PRIMARY KEY, created TEXT, payload TEXT NOT NULL, undone INTEGER DEFAULT 0
);
-- coppie di persone segnalate come «Diverse» (a < b): non riproposte come suggerimento
CREATE TABLE IF NOT EXISTS persons_not_same(
  a INTEGER REFERENCES persons(id) ON DELETE CASCADE,
  b INTEGER REFERENCES persons(id) ON DELETE CASCADE,
  created TEXT,
  PRIMARY KEY(a, b), CHECK(a < b)
);
-- copie sul NAS verificate con hash: il file locale puo' essere spostato nel Cestino solo se la copia risulta valida
CREATE TABLE IF NOT EXISTS nas_copies(
  file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  nas_path TEXT NOT NULL, local_size INTEGER, local_mtime REAL, hash TEXT NOT NULL, verified TEXT NOT NULL
);
-- file non utilizzabili scoperti durante l'uso: 'mancante' (non piu' sul disco) | 'illeggibile' (anteprima impossibile)
CREATE TABLE IF NOT EXISTS broken(
  file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  reason TEXT NOT NULL, checked TEXT
);
"""


MIGRATION_REV = 2  # da incrementare quando cambia _migrate
# Versione dello schema scritta in PRAGMA user_version dopo SCHEMA + migrazioni: le connessioni successive
# (una per richiesta HTTP) la leggono dall'intestazione del file e saltano executescript e migrazioni.
# Un catalogo nuovo, sostituito o creato da una versione diversa ha un altro valore e viene aggiornato.
SCHEMA_VERSION = (zlib.crc32(SCHEMA.encode("utf-8")) ^ MIGRATION_REV) & 0x7FFFFFFF or 1


_upgrade_lock = threading.Lock()  # aggiornamento dello schema: una connessione alla volta (richieste parallele all'avvio)


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        with _upgrade_lock:
            # un'altra richiesta puo' aver gia' aggiornato il file mentre si aspettava il lock
            if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                conn.execute("PRAGMA journal_mode=WAL")  # persistente nel file: basta impostarlo una volta
                conn.executescript(SCHEMA)
                _migrate(conn)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Colonne e tabelle aggiunte dopo la prima versione dello schema (CREATE TABLE IF NOT EXISTS non le aggiunge)."""
    changed = False
    cols = {r[1] for r in conn.execute("PRAGMA table_info(plan)")}
    if "gkind" not in cols:  # tipo del gruppo: event | varie | context (NULL = ricavato dal nome)
        conn.execute("ALTER TABLE plan ADD COLUMN gkind TEXT")
        changed = True
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(persons)")}
    if "cover_locked" not in pcols:
        conn.execute("ALTER TABLE persons ADD COLUMN cover_locked INTEGER DEFAULT 0")
        changed = True
    fcols = {r[1] for r in conn.execute("PRAGMA table_info(faces)")}
    for col, spec in (
        ("person_id", "INTEGER REFERENCES persons(id) ON DELETE SET NULL"),
        ("locked", "INTEGER DEFAULT 0"),
        ("excluded", "INTEGER DEFAULT 0"),
        ("det_score", "REAL"),
        ("quality", "REAL"),
        ("landmarks", "TEXT"),
    ):
        if col not in fcols:
            conn.execute(f"ALTER TABLE faces ADD COLUMN {col} {spec}")
            changed = True
    conn.execute("CREATE INDEX IF NOT EXISTS ix_faces_file ON faces(file_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_faces_person ON faces(person_id)")
    try:  # nomi album unici (senza maiuscole); se esistono gia' doppioni il vincolo non si crea e resta il controllo nella route
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_albums_name ON albums(name COLLATE NOCASE)")
    except sqlite3.IntegrityError:
        pass
    # Migrazione una tantum dei vecchi cluster greedy -> tabella persons
    if conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0:
        legacy = conn.execute(
            "SELECT cluster, COUNT(*) AS c, MIN(id) AS cover_id FROM faces "
            "WHERE cluster IS NOT NULL AND cluster >= 0 AND person_id IS NULL GROUP BY cluster ORDER BY cluster"
        ).fetchall()
        if legacy:
            from datetime import datetime
            now = datetime.now().isoformat(timespec="seconds")
            for r in legacy:
                cur = conn.execute(
                    "INSERT INTO persons(name, created, n, cover_face_id, hidden) VALUES(NULL,?,?,?,0)",
                    (now, int(r["c"]), int(r["cover_id"])),
                )
                conn.execute("UPDATE faces SET person_id=? WHERE cluster=? AND person_id IS NULL",
                             (cur.lastrowid, r["cluster"]))
            changed = True
    if changed:
        conn.commit()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def load_embeddings(conn: sqlite3.Connection) -> tuple[np.ndarray, np.ndarray]:
    """(ids[n], matrice[n,d]) normalizzata float32."""
    rows = conn.execute("SELECT file_id, vec FROM embeddings ORDER BY file_id").fetchall()
    if not rows:
        return np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.float32)
    # Catalogo con embedding di modelli diversi (cambio modello a meta'): si tengono quelli della dimensione piu'
    # comune, invece di far fallire np.stack con un errore.
    dims = Counter(len(r["vec"]) for r in rows)
    if len(dims) > 1:
        keep = dims.most_common(1)[0][0]
        rows = [r for r in rows if len(r["vec"]) == keep]
    ids = np.fromiter((r["file_id"] for r in rows), dtype=np.int64, count=len(rows))
    mat = np.stack([from_blob(r["vec"]) for r in rows])
    return ids, mat


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    # una sola scansione di files per tutti i contatori (prima: 9 COUNT separati)
    r = conn.execute(
        "SELECT COUNT(*),"
        " COALESCE(SUM(kind='image'),0), COALESCE(SUM(kind='video'),0),"
        " COALESCE(SUM(status='new'),0), COALESCE(SUM(status IN ('meta','embedded')),0),"
        " COALESCE(SUM(status='embedded'),0), COALESCE(SUM(status='error'),0),"
        " COALESCE(SUM(date_source IS NOT NULL AND date_source!='exif' AND kind='image'),0),"
        " COALESCE(SUM(data_incerta=1),0) FROM files"
    ).fetchone()
    dup = conn.execute(
        "SELECT COALESCE(SUM(c-1),0) FROM (SELECT COUNT(*) c FROM files WHERE hash IS NOT NULL GROUP BY hash HAVING c>1)"
    ).fetchone()[0]
    keys = ("totale", "foto", "video", "nuovi", "con_metadati", "indicizzati", "errori", "senza_data_exif", "data_incerta")
    out = {k: int(v) for k, v in zip(keys, r)}
    out["duplicati"] = int(dup)
    return out
