"""Files you open from the whole-Mac name search rank a little higher next time, and the file you always open for a
query comes first for exactly that query.

The idea (a recency-and-frequency score per file, and a boost for "the file opened last time for this same query") is
fff's (https://github.com/dmtrKovalenko/fff, MIT); this is a small independent implementation for MySearch.

What is kept, on this Mac only, in `frecency.db` next to the name index (the index can be deleted and rebuilt without
losing it; deleting this file forgets all of it):

* per opened file: a keyed hash of its path (never the path), how often, when last, and a score that halves every 14 days;
* per (query, file) pair: a keyed hash of the normalised query text and of the path, a count, the time, and the file's id
  in the name index (an id is only a hint: it is used again only if the row at that id still hashes to the same path).

The hash key is random and lives in the same file, so the hashes mean nothing outside this install. Both tables are capped
(CAP_FILES, CAP_PAIRS); the least valuable rows go first. A boost only reorders rows that already passed the privacy gate
and the query's filters. Who can see the effect: the ranking is part of the order of every file-name search the service
answers, `mse files` included (a session locked with MSE_LOCK that runs the command line without the service gets plain
order). The stored data itself cannot be read through the MCP server or the command line; they have no way to list, read
or change it, and the service only changes it on `POST /api/files/opened` and `/api/files/frecency/clear`.
"""

import hashlib
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import paths

HALF_LIFE_S = 14 * 86400
CAP_FILES = 4000
CAP_PAIRS = 8000
FRECENCY_MAX = 40.0          # points a file opened often and lately can gain (a tier is 100 points)
HISTORY_TOP = 1000.0         # the file opened most for exactly this query: first among everything that matches
HISTORY_OTHER = 100.0        # other files opened for exactly this query
MAX_QUERY_CHARS = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS opened(h BLOB PRIMARY KEY, n INTEGER NOT NULL, score REAL NOT NULL, last INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS history(qh BLOB NOT NULL, h BLOB NOT NULL, n INTEGER NOT NULL, last INTEGER NOT NULL,
  fid INTEGER, PRIMARY KEY(qh, h)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS history_last ON history(last);
"""


def normalise_query(q: str | None) -> str:
    """What "the same query" means: lower case, single spaces, no leading or trailing blanks, at most 200 characters."""
    return re.sub(r"\s+", " ", (q or "").strip().lower())[:MAX_QUERY_CHARS]


def decayed(score: float, last: float, now: float) -> float:
    return score * 0.5 ** (max(0.0, now - last) / HALF_LIFE_S)


class Prepared:
    """What search() needs for one query: the files to offer (id -> hash they must still have), the key function for
    those hashes and the bonus of a path. Built by Frecency.for_query; reads memory only."""

    def __init__(self, fr: "Frecency", qh: bytes | None, ids: dict, hist: dict, now: float):
        self._fr, self.ids, self._hist, self._now = fr, ids, hist, now
        top = max(hist.items(), key=lambda kv: (kv[1][0], kv[1][1]))[0] if hist else None
        self._top = top

    def key(self, path: str) -> bytes:
        return self._fr.hash_path(path)

    def bonus(self, path: str) -> float:
        h = self._fr.hash_path(path)
        b = 0.0
        row = self._fr._files.get(h)
        if row is not None:
            eff = decayed(row[0], row[1], self._now)
            b += FRECENCY_MAX * (1.0 - math.exp(-eff / 2.0))
        if self._hist and h in self._hist:
            b += HISTORY_TOP if h == self._top else HISTORY_OTHER
        return b


class Frecency:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else paths.DATA / "frecency.db"
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._salt: bytes | None = None
        self._files: dict[bytes, tuple[float, int]] = {}                   # hash -> (score, last)
        self._hist: dict[bytes, dict[bytes, tuple[int, int, int | None]]] = {}   # query hash -> {hash: (n, last, fid)}

    # ---- storage
    def _db(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                new = not self.path.exists()
                c = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False, timeout=30)
                if new:
                    try:
                        os.chmod(self.path, 0o600)
                    except OSError:
                        pass
                c.execute("PRAGMA journal_mode = WAL")
                c.execute("PRAGMA synchronous = NORMAL")
                c.executescript(SCHEMA)
                row = c.execute("SELECT v FROM meta WHERE k = 'salt'").fetchone()
                if row:
                    self._salt = bytes.fromhex(row[0])
                else:
                    self._salt = os.urandom(16)
                    c.execute("INSERT INTO meta(k, v) VALUES ('salt', ?)", (self._salt.hex(),))
                self._files = {bytes(h): (s, l) for h, s, l in c.execute("SELECT h, score, last FROM opened")}
                self._hist = {}
                for qh, h, n, last, fid in c.execute("SELECT qh, h, n, last, fid FROM history"):
                    self._hist.setdefault(bytes(qh), {})[bytes(h)] = (n, last, fid)
                self._conn = c
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ---- hashes
    def _h(self, domain: bytes, text: str) -> bytes:
        if self._salt is None:
            self._db()
        return hashlib.blake2b(domain + text.encode("utf-8", "surrogatepass"), digest_size=8, key=self._salt).digest()

    def hash_path(self, path: str) -> bytes:
        return self._h(b"p:", path)

    def hash_query(self, query: str | None) -> bytes | None:
        n = normalise_query(query)
        return self._h(b"q:", n) if n else None

    # ---- writing
    def record(self, path: str, query: str | None = None, file_id: int | None = None, now: float | None = None) -> bool:
        """One open of `path`, optionally for `query`. The caller has checked that the path may be recorded."""
        now = int(time.time() if now is None else now)
        with self._lock:
            c = self._db()
            h = self.hash_path(path)
            score, last = self._files.get(h, (0.0, now))
            score = decayed(score, last, now) + 1.0
            qh = self.hash_query(query)
            c.execute("BEGIN")
            try:
                c.execute("INSERT INTO opened(h, n, score, last) VALUES (?, 1, ?, ?) ON CONFLICT(h) DO UPDATE SET "
                          "n = n + 1, score = excluded.score, last = excluded.last", (h, score, now))
                if qh is not None:
                    c.execute("INSERT INTO history(qh, h, n, last, fid) VALUES (?, ?, 1, ?, ?) ON CONFLICT(qh, h) DO "
                              "UPDATE SET n = n + 1, last = excluded.last, fid = excluded.fid", (qh, h, now, file_id))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            self._files[h] = (score, now)                  # memory follows the file only once the write is safe
            if qh is not None:
                old = self._hist.setdefault(qh, {}).get(h)
                self._hist[qh][h] = ((old[0] + 1) if old else 1, now, file_id)
            self._trim(now)
            return True

    def _trim(self, now: float) -> None:
        """Keep the tables within their caps: the rows with the lowest decayed score (files) or the oldest (pairs) go."""
        c = self._conn
        if len(self._files) > CAP_FILES:
            keep = sorted(self._files.items(), key=lambda kv: decayed(kv[1][0], kv[1][1], now), reverse=True)
            drop = [h for h, _ in keep[int(CAP_FILES * 0.9):]]
            c.execute("BEGIN")
            c.executemany("DELETE FROM opened WHERE h = ?", [(h,) for h in drop])
            c.execute("COMMIT")
            for h in drop:
                self._files.pop(h, None)
        total = sum(len(v) for v in self._hist.values())
        if total > CAP_PAIRS:
            pairs = sorted(((v[1], qh, h) for qh, d in self._hist.items() for h, v in d.items()))
            drop = pairs[: total - int(CAP_PAIRS * 0.9)]
            c.execute("BEGIN")
            c.executemany("DELETE FROM history WHERE qh = ? AND h = ?", [(qh, h) for _, qh, h in drop])
            c.execute("COMMIT")
            for _, qh, h in drop:
                self._hist.get(qh, {}).pop(h, None)
            self._hist = {k: v for k, v in self._hist.items() if v}

    def clear(self) -> None:
        with self._lock:
            c = self._db()
            c.execute("BEGIN")
            c.execute("DELETE FROM opened")
            c.execute("DELETE FROM history")
            c.execute("COMMIT")
            self._files, self._hist = {}, {}

    # ---- reading
    def for_query(self, query: str | None, now: float | None = None) -> Prepared | None:
        """None when there is nothing to rank by (no file was ever opened here): the common case costs nothing."""
        with self._lock:
            if not self.path.exists() and self._conn is None:
                return None
            self._db()
            if not self._files:
                return None
            now = time.time() if now is None else now
            qh = self.hash_query(query)
            hist = dict(self._hist.get(qh, {})) if qh is not None else {}
            ids = {fid: h for h, (n, last, fid) in hist.items() if fid is not None}
            return Prepared(self, qh, ids, {h: (n, last) for h, (n, last, fid) in hist.items()}, now)

    def stats(self) -> dict:
        with self._lock:
            if self._conn is None and not self.path.exists():
                return {"files": 0, "pairs": 0, "cap_files": CAP_FILES, "cap_pairs": CAP_PAIRS}
            self._db()
            return {"files": len(self._files), "pairs": sum(len(v) for v in self._hist.values()),
                    "cap_files": CAP_FILES, "cap_pairs": CAP_PAIRS}
