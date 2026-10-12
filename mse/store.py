"""One SQLite database per collection: files, items (chunks / pages / media segments),
FTS5 keyword index, float16 vectors for exact re-scoring and a 1-bit sqlite-vec index for fast search."""

import sqlite3
import threading
import time

import numpy as np
import sqlite_vec

from . import paths
from .ignore import NO_EMBED_EXT

DIMS = 768
SCHEMA_VERSION = "4"      # 4: doc_meta; 3: files.missing_since; 2: files.xver, files.inode, failures (all additive, _migrate)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  name TEXT, ext TEXT, kind TEXT,
  size INTEGER, mtime REAL,
  mode TEXT DEFAULT 'full',
  state TEXT DEFAULT 'new',
  heavy TEXT,
  error TEXT,
  title TEXT, doc_date TEXT, lang TEXT,
  pages INTEGER, duration REAL,
  thumb INTEGER DEFAULT 0,
  docid TEXT,
  indexed_at REAL, heavy_at REAL
);
CREATE INDEX IF NOT EXISTS files_heavy ON files(heavy) WHERE heavy IS NOT NULL;
CREATE INDEX IF NOT EXISTS files_docid ON files(docid);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL,
  ord INTEGER,
  modality TEXT,
  page INTEGER, t0 REAL, t1 REAL,
  heading TEXT,
  text TEXT,
  media TEXT,
  embedded INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_file ON items(file_id);
CREATE INDEX IF NOT EXISTS items_pending ON items(embedded) WHERE embedded = 0;
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  name, heading, text, content='', contentless_delete=1,
  tokenize='unicode61 remove_diacritics 2', prefix='3'
);
CREATE TABLE IF NOT EXISTS vectors(item_id INTEGER PRIMARY KEY, modality TEXT, v BLOB);
CREATE VIRTUAL TABLE IF NOT EXISTS vbin USING vec0(item_id INTEGER PRIMARY KEY, embedding bit[{DIMS}], modality TEXT);
CREATE TABLE IF NOT EXISTS vocab(id INTEGER PRIMARY KEY, term TEXT UNIQUE, df INTEGER);
CREATE VIRTUAL TABLE IF NOT EXISTS vvocab USING vec0(id INTEGER PRIMARY KEY, embedding float[{DIMS}] distance_metric=cosine);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_terms USING fts5vocab(fts, 'row');
CREATE TABLE IF NOT EXISTS failures(
  path TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0, last_error TEXT, last_try REAL,
  mtime_at_failure REAL, version TEXT
);
-- schema 4 (additive; created on the first writable open of an older database, existing tables and indexes untouched):
-- what a document is and its main facts (mse/docmeta.py). One row per file; doctype NULL = looked at, nothing to classify.
CREATE TABLE IF NOT EXISTS doc_meta(
  file_id INTEGER PRIMARY KEY,
  doctype TEXT, confidence REAL, doc_date TEXT, amount REAL, currency TEXT,
  counterparty TEXT, reference TEXT, lang TEXT, extractor_version INTEGER, extracted_at REAL
);
CREATE INDEX IF NOT EXISTS doc_meta_doctype ON doc_meta(doctype);
CREATE INDEX IF NOT EXISTS doc_meta_date ON doc_meta(doc_date);
"""

_write_locks: dict[str, threading.Lock] = {}
_wl_guard = threading.Lock()


def write_lock(name: str) -> threading.Lock:
    with _wl_guard:
        return _write_locks.setdefault(name, threading.Lock())


def db_path(name: str):
    return paths.COLLECTIONS / f"{name}.db"


def connect(name: str, readonly: bool = False) -> sqlite3.Connection:
    path = db_path(name)
    if readonly:
        if not path.exists():
            raise FileNotFoundError(path)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    else:
        paths.COLLECTIONS.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA busy_timeout=30000")
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
    return conn


def _migrate(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    if "hash" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN hash TEXT")
    # schema 2, in place and without re-extraction: xver NULL means extractor version 1, which is current for every
    # existing row; inode NULL means unknown (it is only a rename hint, never a change test)
    if "xver" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN xver INTEGER")
    if "inode" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN inode INTEGER")
    # schema 3, additive: when a pass last failed to see a file, the time it was first missing (NULL = present, which is
    # every existing row). The row is removed only after a later pass, a grace period on (see indexer._settle_missing)
    if "missing_since" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN missing_since REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS files_missing ON files(missing_since) WHERE missing_since IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS files_hash ON files(hash)")
    # prompt -> vector cache: identical chunks (copies, re-extraction) are never embedded twice
    conn.execute("CREATE TABLE IF NOT EXISTS vcache(key TEXT PRIMARY KEY, v BLOB)")
    # written only when it changes: connections are opened for many small reads, and a write here would queue behind a scan
    if get_meta(conn, "schema") != SCHEMA_VERSION:
        conn.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (SCHEMA_VERSION,))
    conn.commit()


# ---------------------------------------------------------------- vectors

def to_f16(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float16).tobytes()


def from_f16(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float16).astype(np.float32)


def to_bits(v: np.ndarray) -> bytes:
    return np.packbits(np.asarray(v) > 0).tobytes()


# ---------------------------------------------------------------- writes

def delete_items(conn, item_ids: list[int]) -> None:
    for i in range(0, len(item_ids), 500):
        part = item_ids[i:i + 500]
        q = ",".join("?" * len(part))
        conn.execute(f"DELETE FROM fts WHERE rowid IN ({q})", part)
        conn.execute(f"DELETE FROM vectors WHERE item_id IN ({q})", part)
        conn.execute(f"DELETE FROM vbin WHERE item_id IN ({q})", part)
        conn.execute(f"DELETE FROM items WHERE id IN ({q})", part)


def delete_file(conn, file_id: int) -> None:
    ids = [r[0] for r in conn.execute("SELECT id FROM items WHERE file_id=?", (file_id,))]
    delete_items(conn, ids)
    conn.execute("DELETE FROM doc_meta WHERE file_id=?", (file_id,))      # what was derived from the file goes with it
    conn.execute("DELETE FROM files WHERE id=?", (file_id,))


def add_items(conn, file_id: int, name_text: str, items: list[dict], start_ord: int = 0) -> int:
    n = 0
    for k, it in enumerate(items):
        cur = conn.execute(
            "INSERT INTO items(file_id, ord, modality, page, t0, t1, heading, text, media, embedded)"
            " VALUES (?,?,?,?,?,?,?,?,?,0)",
            (file_id, start_ord + k, it["modality"], it.get("page"), it.get("t0"), it.get("t1"),
             it.get("heading"), it.get("text"), it.get("media")),
        )
        conn.execute("INSERT INTO fts(rowid, name, heading, text) VALUES (?,?,?,?)",
                     (cur.lastrowid, name_text, it.get("heading") or "", it.get("text") or ""))
        n += 1
    return n


def replace_items(conn, file_id: int, name_text: str, items: list[dict], modalities: set[str] | None = None) -> int:
    """Replace a file's items (optionally only those of the given modalities)."""
    if modalities:
        q = ",".join("?" * len(modalities))
        ids = [r[0] for r in conn.execute(f"SELECT id FROM items WHERE file_id=? AND modality IN ({q})",
                                          (file_id, *modalities))]
        start = (conn.execute("SELECT COALESCE(MAX(ord),-1)+1 FROM items WHERE file_id=?", (file_id,)).fetchone()[0])
    else:
        ids = [r[0] for r in conn.execute("SELECT id FROM items WHERE file_id=?", (file_id,))]
        start = 0
    delete_items(conn, ids)
    return add_items(conn, file_id, name_text, items, start)


def store_vectors(conn, rows: list[tuple[int, str, np.ndarray]]) -> None:
    for item_id, modality, v in rows:
        v = np.asarray(v, dtype=np.float32)
        conn.execute("INSERT OR REPLACE INTO vectors(item_id, modality, v) VALUES (?,?,?)", (item_id, modality, to_f16(v)))
        conn.execute("DELETE FROM vbin WHERE item_id=?", (item_id,))
        conn.execute("INSERT INTO vbin(item_id, embedding, modality) VALUES (?, vec_bit(?), ?)",
                     (item_id, to_bits(v), modality))
        conn.execute("UPDATE items SET embedded=1, media=NULL WHERE id=?", (item_id,))


def set_meta(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))


def get_meta(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def has_column(conn, table: str, column: str) -> bool:
    """Whether a table has a column. A read-only connection never runs _migrate, so a database that no writer has opened
    since an upgrade may lack a newer column; callers treat that as 'nothing there'."""
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def name_only_unembeddable(alias: str = "", with_ext: bool = True) -> str:
    """The rule for a file whose items are never embedded: a name-only code file (its kind is code), and a name-only
    binary, font or data file (NO_EMBED_EXT). Its name stays searchable through the keyword index. `alias` is the files
    table's alias in the query ("f." or ""). with_ext=False for a database that predates the ext column: code only.
    The one predicate for embed(), pending_count, the progress counts and stats."""
    if not with_ext:
        return f"({alias}mode='meta' AND {alias}kind='code')"
    exts = ",".join("'" + e + "'" for e in sorted(NO_EMBED_EXT))
    return f"({alias}mode='meta' AND ({alias}kind='code' OR {alias}ext IN ({exts})))"


def embed_skip_items(conn) -> str:
    """' AND file_id NOT IN (...)' for a query over items: the items of files that are never embedded, a file marked
    missing (when the column exists) and every file name_only_unembeddable() names. Same answer everywhere."""
    skip = name_only_unembeddable(with_ext=has_column(conn, "files", "ext"))
    if has_column(conn, "files", "missing_since"):
        skip = "missing_since IS NOT NULL OR " + skip
    return f" AND file_id NOT IN (SELECT id FROM files WHERE {skip})"


def stats(conn) -> dict:
    # Pending work is the work that will run: a file marked missing (missing_since) is skipped by the heavy and embed
    # stages, and so is a name-only code or binary file (name_only_unembeddable). Neither counts, or the pending numbers
    # would never reach zero.
    if has_column(conn, "files", "missing_since"):
        heavy = "SUM(heavy IS NOT NULL AND missing_since IS NULL)"
    else:
        heavy = "SUM(heavy IS NOT NULL)"
    embed = "SUM(embedded=0" + embed_skip_items(conn) + ")"
    f = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0), SUM(mode='meta'), SUM(state='error'),"
                     f" {heavy} FROM files").fetchone()
    i = conn.execute(f"SELECT COUNT(*), {embed} FROM items").fetchone()
    by_mod = {r[0]: r[1] for r in conn.execute("SELECT modality, COUNT(*) FROM items GROUP BY modality")}
    by_kind = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM files GROUP BY kind")}
    return {
        "files": f[0], "bytes": f[1], "metadata_only": f[2] or 0, "errors": f[3] or 0,
        "heavy_pending": f[4] or 0, "items": i[0], "embed_pending": i[1] or 0,
        "modalities": by_mod, "kinds": by_kind,
        "last_scan": get_meta(conn, "last_scan"), "last_heavy": get_meta(conn, "last_heavy"),
        "last_embed": get_meta(conn, "last_embed"),
    }


def now() -> float:
    return time.time()
