"""The indexing pipeline for one collection: scan → light extraction → heavy extraction → embedding → vocabulary."""

import hashlib
import json
import os
import re
import shutil
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed, wait as wait_futures
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISDIR, S_ISREG

import numpy as np

from . import config, extract, paths, store
from . import progress as liveprog
from .embedder import QUERY, doc_prompt, pool
from .ignore import DEFAULT_EXCLUDES, SECRET_PATTERNS, Matcher
from .textutil import STOP, date_from_path, name_tokens

EXTRACT_VERSION = "2"     # bump when extraction/chunking changes: every file is re-read at the next scan
TEXT_MODALITIES = ("title", "text", "ocr", "transcript")
MEDIA_MODALITIES = ("image", "page", "audio", "video")


class Cancelled(Exception):
    pass


def docid(coll: str, rel: str) -> str:
    return hashlib.sha1(f"{coll}:{rel}".encode()).hexdigest()[:8]


def readable_path(rel: str) -> str:
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", rel)
    return re.sub(r"[_]+", " ", stem.replace("/", " / "))


def matchers(coll: dict) -> tuple[Matcher, Matcher]:
    return Matcher(SECRET_PATTERNS + DEFAULT_EXCLUDES + list(coll.get("exclude") or [])), \
        Matcher(coll.get("metadata_only") or [])


def walk(coll: dict, errors: list):
    """(rel, size, mtime) for every file the collection's rules allow."""
    for rel, size, mtime, _ino in _walk(coll, errors):
        yield rel, size, mtime


def _rel_folder(root: Path, p) -> str:
    """A folder in the form its rows use: relative to the root, no trailing slash, "" for the root itself. A path that
    is missing or lies outside the root counts as the root, the safe side."""
    rel = os.path.relpath(str(p), str(root)) if p else "."
    return "" if rel in (".", os.pardir) or rel.startswith(os.pardir + os.sep) else rel


def _under_unreadable(rel: str, unreadable: set[str]) -> bool:
    """True when a row's path lies inside a folder the walk could not read ("" is the root, which holds every row)."""
    return any(d == "" or rel.startswith(d + "/") for d in unreadable)


def _root_readable(root: Path) -> bool:
    try:
        os.scandir(root).close()
        return True
    except OSError:
        return False


def _walk(coll: dict, errors: list, unreadable: set | None = None):
    """Yields (rel, size, mtime, ino). When `unreadable` is given, the folders the walk could not read are added to it
    (relative, "" for the root): their rows must not be removed, because the walk never saw what is in them."""
    root = Path(coll["path"])
    exclude, _ = matchers(coll)

    def onerror(e):
        errors.append(str(e))
        if unreadable is not None:
            unreadable.add(_rel_folder(root, getattr(e, "filename", None)))

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror, followlinks=False):
        reldir = os.path.relpath(dirpath, root)
        reldir = "" if reldir == "." else reldir + "/"
        keep = []
        for d in dirnames:
            rel = reldir + d
            if not exclude.match(rel + "/") and not os.path.islink(os.path.join(dirpath, d)):
                keep.append(d)
        dirnames[:] = sorted(keep)
        for f in filenames:
            rel = reldir + f
            if exclude.match(rel):
                continue
            full = os.path.join(dirpath, f)
            try:
                st = os.lstat(full)
            except OSError as e:
                errors.append(str(e))
                if unreadable is not None:
                    unreadable.add(reldir.rstrip("/"))      # the file sits in this folder: its rows are kept
                continue
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            yield rel, st.st_size, st.st_mtime, st.st_ino


# ------------------------------------------------------------------ settings, failures, crash markers

FAILURE_LIMIT = 3            # a file that failed this many times in a row is skipped until its mtime or its extractor changes
XVER_PER_SCAN = 25           # extractor-version upgrades re-read per scan (daytime); the nightly run takes many more
XVER_PER_NIGHT = 2000
FILE_TIMEOUT_S = 420         # wall-clock limit for reading one file (the helper calls keep their own, shorter limits)
COMMIT_EVERY = 200           # files per transaction ...
COMMIT_SECONDS = 2.0         # ... or this long, whichever comes first
HASH_BACKFILL = 300          # old rows without a content hash hashed per scan (the nightly run does more)
HASH_BACKFILL_NIGHT = 5000
INFLIGHT = "scan_inflight.json"
STOPPING = False             # set by the job manager's freeze() on a graceful stop: no more in-flight markers are written


class _Overdue(Exception):
    """A file's read ran longer than its limit. Its thread stays busy (a Python thread cannot be stopped)."""


@dataclass
class _Item:
    rel: str
    size: int
    mtime: float
    mode: str                   # full | meta (metadata-only: the name is the whole entry, nothing is read)
    ino: int | None
    reason: str                 # new | changed | forced | xver
    old: dict | None = None     # the file's row, when it has one
    move: dict | None = None    # set when this file has the same content as a row that vanished in this pass


def _settings() -> dict:
    try:
        return config.load().get("indexing") or {}
    except Exception:
        return {}


def _worker_count() -> int:
    default = max(2, min(4, (os.cpu_count() or 4) // 2))
    try:
        return max(1, int(_settings().get("workers", default)))
    except (TypeError, ValueError):
        return default


def _ext_of(rel: str) -> str:
    return Path(rel).suffix.lower().lstrip(".")


def _ext_version(ext: str) -> int:
    """The extractor version for an extension (extract.EXTRACTOR_VERSION; missing means 1, the current one)."""
    return int(getattr(extract, "EXTRACTOR_VERSION", {}).get(ext, 1) or 1)


def _version_key(ext: str) -> str:
    """Which extraction code applies to an extension. A failure is only retried when this key changes."""
    return f"{EXTRACT_VERSION}.{_ext_version(ext)}"


def _mode(meta_only: Matcher, rel: str, size: int, max_bytes: int) -> str:
    return "meta" if (meta_only.match(rel) or size > max_bytes) else "full"


def _record_failure(conn, rel: str, mtime: float, error: str) -> None:
    row = conn.execute("SELECT count, mtime_at_failure FROM failures WHERE path=?", (rel,)).fetchone()
    same = row is not None and row["mtime_at_failure"] is not None and abs(row["mtime_at_failure"] - mtime) < 1e-3
    count = (row["count"] + 1) if same else 1
    conn.execute("INSERT OR REPLACE INTO failures(path, count, last_error, last_try, mtime_at_failure, version)"
                 " VALUES (?,?,?,?,?,?)", (rel, count, error[:300], time.time(), mtime, _version_key(_ext_of(rel))))


def _struck_out(conn, rel: str, mtime: float) -> bool:
    row = conn.execute("SELECT count, mtime_at_failure, version FROM failures WHERE path=?", (rel,)).fetchone()
    return bool(row is not None and row["count"] >= FAILURE_LIMIT and row["mtime_at_failure"] is not None
                and abs(row["mtime_at_failure"] - mtime) < 1e-3 and row["version"] == _version_key(_ext_of(rel)))


def _clear_strikes(conn, rel: str) -> None:
    conn.execute("UPDATE failures SET count=0 WHERE path=? AND count>0", (rel,))


def _inflight_path(name: str) -> Path:
    return paths.TMP / name / INFLIGHT


def _mark_inflight(name: str, items: list) -> None:
    """Crash guard: the files being read right now. Written before each read, cleared when the scan stops cleanly."""
    f = _inflight_path(name)
    try:
        if items and not STOPPING:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(items))
        else:
            f.unlink(missing_ok=True)
    except OSError:
        pass


def _recover_inflight(conn, name: str) -> int:
    """A marker still on disk means the last run was killed while these files were being read. Each of them gets a
    strike, so a file that kills the process is not retried at every start."""
    f = _inflight_path(name)
    if not f.exists():
        return 0
    try:
        items = json.loads(f.read_text())
    except (OSError, ValueError):
        items = []
    n = 0
    for it in items if isinstance(items, list) else []:
        try:
            rel, mtime = it
            _record_failure(conn, rel, float(mtime), "interrupted: the service stopped while this file was being read")
            n += 1
        except (TypeError, ValueError):
            continue
    f.unlink(missing_ok=True)
    return n


def _load_resume(conn) -> dict | None:
    """The checkpoint of a forced re-read that did not finish: files up to `after` were already re-read."""
    raw = store.get_meta(conn, "scan_resume") or ""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("ver") == EXTRACT_VERSION else None


def _rows_under(conn, rel: str) -> list[dict]:
    """Rows for a path and everything below it (a file, or a whole folder)."""
    esc = rel.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return [dict(r) for r in conn.execute(
        "SELECT id, path, size, mtime, mode, inode, hash FROM files WHERE path=? OR path LIKE ? ESCAPE '\\'",
        (rel, esc + "/%"))]


# ------------------------------------------------------------------ light stage

def file_hash(p: Path, limit: int = 512 * 1024 * 1024) -> str | None:
    """Content hash used to recognise identical copies (results show them once)."""
    try:
        if p.stat().st_size > limit:
            return None
        h = hashlib.blake2b(digest_size=16)
        with open(p, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def _prepare(coll: dict, rel: str, mode: str, starts: dict | None = None) -> dict:
    """The worker half of indexing one file: extraction and the content hash. It touches no database.
    A metadata-only file is never opened: its name is the whole index entry."""
    if starts is not None:
        starts[rel] = time.monotonic()
    p = Path(coll["path"]) / rel
    ext = _ext_of(rel)
    kind = extract.kind_of(ext)
    out = {"ext": ext, "kind": kind, "title": None, "lang": None, "pages": None, "heavy": None, "items": [],
           "error": None, "hash": None}
    if mode == "full":
        try:
            res = extract.light(p, kind, ext, coll.get("media") or {})
            out.update(title=res["title"], lang=res["lang"], pages=res["pages"], heavy=res["heavy"],
                       items=res["items"])
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"[:400]
        out["hash"] = file_hash(p) or ""
    return out


def _failed_prep(rel: str, error: str) -> dict:
    ext = _ext_of(rel)
    return {"ext": ext, "kind": extract.kind_of(ext), "title": None, "lang": None, "pages": None, "heavy": None,
            "items": [], "error": error, "hash": None}


def _store_light(conn, coll: dict, rel: str, size: int, mtime: float, mode: str, ino: int | None, prep: dict) -> dict:
    """The main-thread half: one file's rows, written into the open transaction (the caller commits in batches)."""
    name = coll["name"]
    p = Path(coll["path"]) / rel
    row = conn.execute("SELECT id FROM files WHERE path=?", (rel,)).fetchone()
    if row:
        fid = row["id"]
    else:
        fid = conn.execute("INSERT INTO files(path, name, docid) VALUES (?,?,?)",
                           (rel, p.name, docid(name, rel))).lastrowid
    ext, error, title, heavy = prep["ext"], prep["error"], prep["title"], prep["heavy"]
    title_text = readable_path(rel) + (f" — {title}" if title else "")
    items = [{"modality": "title", "text": title_text}] + prep["items"]
    store.replace_items(conn, fid, name_tokens(rel), items)
    extract.cleanup_work(name, fid)
    thumb = paths.THUMBS / name / f"{fid}.jpg"
    if thumb.exists():
        thumb.unlink()
    full = mode == "full"
    conn.execute(
        "UPDATE files SET name=?, ext=?, kind=?, size=?, mtime=?, mode=?, state=?, heavy=?, error=?, title=?,"
        " doc_date=?, lang=?, pages=?, thumb=0, duration=NULL, indexed_at=?, heavy_at=NULL, hash=?, inode=?, xver=?"
        " WHERE id=?",
        (p.name, ext, prep["kind"], size, mtime, mode, "error" if error else "text",
         json.dumps(heavy) if heavy else None, error, title,
         date_from_path(rel) or time.strftime("%Y-%m-%d", time.localtime(mtime)), prep["lang"], prep["pages"],
         time.time(), (prep["hash"] or "") if full else None, ino, _ext_version(ext) if full else None, fid))
    if error:
        _record_failure(conn, rel, mtime, error)
        liveprog.error(name, f"{p.name}: {error}")
    else:
        _clear_strikes(conn, rel)
    return {"id": fid, "items": len(items), "heavy": bool(heavy), "error": error}


def index_file_light(conn, coll: dict, rel: str, size: int, mtime: float, mode: str, ino: int | None = None) -> dict:
    return _store_light(conn, coll, rel, size, mtime, mode, ino, _prepare(coll, rel, mode))


def _match_moves(items: list[_Item], gone: list[dict], hasher) -> dict[str, dict]:
    """Pairs new files with rows that vanished in the same pass, one to one. A pair must agree on extension, size and
    mode. The inode is only a hint (inodes are reused and differ across remounts):
    - a metadata-only file pairs on inode + size + mtime + extension. Nothing is read, and no content is carried;
    - a full-mode file pairs only when its content hash agrees with the vanished row's hash, so no content is ever
      carried over on a hint alone. Among equal hashes, the same inode and mtime are preferred (copies share a hash).
    Returns {rel: row}. The paired rows are the ones the caller keeps."""
    pairs: dict[str, dict] = {}
    used: set[int] = set()
    by_ino: dict[int, list[dict]] = {}
    for g in gone:
        if g["inode"] is not None and g["mode"] == "meta":
            by_ino.setdefault(g["inode"], []).append(g)
    rest: list[_Item] = []
    for it in items:
        hit = None
        if it.mode == "meta" and it.ino is not None:
            hit = next((g for g in by_ino.get(it.ino, []) if g["id"] not in used and g["size"] == it.size
                        and abs((g["mtime"] or 0) - it.mtime) < 1e-3 and _ext_of(g["path"]) == _ext_of(it.rel)), None)
        if hit is not None:
            pairs[it.rel] = hit
            used.add(hit["id"])
        else:
            rest.append(it)
    by_size: dict[int, list[dict]] = {}
    for g in gone:
        if g["id"] not in used and g["mode"] == "full" and g["hash"] and g["size"] is not None:
            by_size.setdefault(g["size"], []).append(g)
    need = [it for it in rest if it.mode == "full" and it.size in by_size]
    if need:
        for it, h in zip(need, hasher(need)):
            if not h:
                continue
            cands = [g for g in by_size[it.size] if g["id"] not in used and g["hash"] == h
                     and _ext_of(g["path"]) == _ext_of(it.rel)]
            cands.sort(key=lambda g: (g["inode"] != it.ino, abs((g["mtime"] or 0) - it.mtime) >= 1e-3))
            if cands:
                pairs[it.rel] = cands[0]
                used.add(cands[0]["id"])
    return pairs


def _repoint(conn, coll: dict, row: dict, it: _Item) -> int:
    """A file moved or was renamed, so the same row now carries the new path. Its id stays, and with it the text,
    OCR, transcripts, thumbnail and vectors. Also rewritten: the files row's path, name, size, mtime, inode and date;
    the title item (it holds the readable path, so it is embedded again, one short text); and the FTS name column of
    every item (contentless FTS: delete each rowid, then insert it again). The docid is kept, so a favourite that
    matched by docid still finds the file."""
    fid = row["id"]
    conn.execute("DELETE FROM failures WHERE path=?", (it.rel,))        # the strikes follow the file to its new path
    conn.execute("UPDATE failures SET path=? WHERE path=?", (it.rel, row["path"]))
    title = conn.execute("SELECT title FROM files WHERE id=?", (fid,)).fetchone()["title"]
    title_text = readable_path(it.rel) + (f" — {title}" if title else "")
    name_text = name_tokens(it.rel)
    conn.execute("UPDATE files SET path=?, name=?, size=?, mtime=?, inode=?, doc_date=? WHERE id=?",
                 (it.rel, Path(it.rel).name, it.size, it.mtime, it.ino,
                  date_from_path(it.rel) or time.strftime("%Y-%m-%d", time.localtime(it.mtime)), fid))
    for r in conn.execute("SELECT id, modality, heading, text FROM items WHERE file_id=?", (fid,)).fetchall():
        text = r["text"]
        if r["modality"] == "title":
            text = title_text
            conn.execute("UPDATE items SET text=?, embedded=0 WHERE id=?", (text, r["id"]))
            conn.execute("DELETE FROM vectors WHERE item_id=?", (r["id"],))
            conn.execute("DELETE FROM vbin WHERE item_id=?", (r["id"],))
        conn.execute("DELETE FROM fts WHERE rowid=?", (r["id"],))
        conn.execute("INSERT INTO fts(rowid, name, heading, text) VALUES (?,?,?,?)",
                     (r["id"], name_text, r["heading"] or "", text or ""))
    return fid


def _await(fut, rel: str, starts: dict, limit: float, cancel):
    """The result of one read. Raises _Overdue when it has run longer than `limit`."""
    while not fut.done():
        # a stop is checked first: a read cut short by a stop is not a timeout, so it never counts as a strike
        if cancel and cancel():
            raise Cancelled()
        t = starts.get(rel)
        if t is not None and time.monotonic() - t > limit:
            raise _Overdue()
        wait_futures([fut], timeout=0.25)
    return fut.result()


def scan(coll: dict, progress=None, cancel=None, night: bool = False) -> dict:
    """Detect new, changed and removed files and read the changes.

    - Unchanged files are never opened: size, mtime and mode decide. The inode is only a rename hint.
    - Reads run in a bounded thread pool, each with a wall-clock limit. Only this thread writes the database, in
      transactions of COMMIT_EVERY files or COMMIT_SECONDS, whichever comes first.
    - A file that vanished and a new file with the same content (same inode, or same hash, size and extension)
      are the same file: the existing row is re-pointed and nothing is read again.
    - A file that failed FAILURE_LIMIT times is skipped until its mtime or its extractor version changes.
    - A folder the walk could not read keeps its rows (counted as kept_unreadable): nothing under it is removed. A
      missing or unreadable root removes nothing at all.
    - Resumable: a forced re-read (new extractor) that stopped continues after its last committed file.
    """
    name = coll["name"]
    _, meta_only = matchers(coll)
    max_bytes = int(coll.get("max_file_mb", 200)) * 1024 * 1024
    settings = _settings()
    workers = _worker_count()
    limit_s = float(settings.get("file_timeout_s", FILE_TIMEOUT_S))
    xver_budget = int(settings.get("xver_per_night" if night else "xver_per_scan",
                                   XVER_PER_NIGHT if night else XVER_PER_SCAN))
    hash_budget = HASH_BACKFILL_NIGHT if night else HASH_BACKFILL
    t0 = time.time()
    errors: list[str] = []
    seen: set[str] = set()
    unreadable: set[str] = set()       # folders the walk could not read ("" is the root): their rows are not removed
    work: list[_Item] = []
    gone_rows: list[dict] = []
    counts = {"errors": 0, "moved": 0, "done": 0}
    state = {"overdue": [], "since": 0, "committed": 0, "last_commit": time.monotonic(), "last_rel": None}
    stalled = cancelled = False
    removed = skipped = xver_deferred = interrupted = kept_unreadable = 0
    resume_after = None
    ex = None
    if progress:
        progress("scanning", 0, 0, "")
    with store.write_lock(name):
        conn = store.connect(name)
        try:
            interrupted = _recover_inflight(conn, name)
            if store.get_meta(conn, "meta_hashes_cleared") != "1":
                # metadata-only files are never read, so a content hash kept from before must not group them with copies
                conn.execute("UPDATE files SET hash=NULL WHERE mode='meta' AND hash IS NOT NULL")
                store.set_meta(conn, "meta_hashes_cleared", "1")
            conn.commit()
            known = {r["path"]: dict(r) for r in conn.execute(
                "SELECT id, path, size, mtime, mode, inode, hash, xver FROM files")}
            version_stale = store.get_meta(conn, "extract_version") != EXTRACT_VERSION
            resume = _load_resume(conn) if version_stale else None
            resume_after = resume.get("after") if resume else None
            xver_cands: list[_Item] = []
            for rel, size, mtime, ino in _walk(coll, errors, unreadable):
                seen.add(rel)
                mode = _mode(meta_only, rel, size, max_bytes)
                old = known.get(rel)
                if old is None:
                    reason = "new"
                elif old["size"] != size or abs((old["mtime"] or 0) - mtime) > 1e-3 or old["mode"] != mode:
                    reason = "changed"
                elif version_stale and (resume_after is None or rel > resume_after):
                    reason = "forced"
                elif mode == "full" and old["mode"] == "full" and _ext_version(_ext_of(rel)) > (old["xver"] or 1):
                    reason = "xver"
                else:
                    continue
                if old is not None and old["mtime"] == -1:
                    _clear_strikes(conn, rel)           # Retry sets mtime to -1: an explicit request resets the strikes
                elif _struck_out(conn, rel, mtime):
                    skipped += 1
                    continue
                item = _Item(rel, size, mtime, mode, ino, reason, old)
                (xver_cands if reason == "xver" else work).append(item)
            xver_cands.sort(key=lambda w: w.rel)
            xver_deferred = max(0, len(xver_cands) - xver_budget)
            work += xver_cands[:xver_budget]
            work.sort(key=lambda w: w.rel)
            if errors and not seen and not known:
                raise PermissionError(
                    f"cannot read {coll['path']}: {errors[0]} — allow MySearch's Python in System Settings › "
                    "Privacy & Security › Full Disk Access (or Files and Folders)")
            root = Path(coll["path"])
            ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="index")
            # A row the walk did not see is removed, unless the walk could not read the folder it sits in. A missing or
            # unreadable root removes nothing at all: that is "cannot tell", not "every file was deleted".
            unseen = [v for k, v in known.items() if k not in seen]
            if not _root_readable(root):
                errors.insert(0, "root unreadable; nothing removed")
                gone_rows = []
            else:
                gone_rows = [v for v in unseen if not _under_unreadable(v["path"], unreadable)]
            kept_unreadable = len(unseen) - len(gone_rows)
            new_items = [w for w in work if w.reason == "new"]
            pairs = {}
            if new_items and gone_rows:
                pairs = _match_moves(new_items, gone_rows,
                                     lambda its: list(ex.map(lambda w: file_hash(root / w.rel), its)))
            for w in new_items:
                w.move = pairs.get(w.rel)
            used = {g["id"] for g in pairs.values()}
            for g in gone_rows:
                if g["id"] in used:
                    continue
                store.delete_file(conn, g["id"])
                conn.execute("DELETE FROM failures WHERE path=?", (g["path"],))
                extract.cleanup_work(name, g["id"])
                (paths.THUMBS / name / f"{g['id']}.jpg").unlink(missing_ok=True)
                removed += 1
            conn.commit()

            total = len(work)
            if progress:
                progress("extract", 0, total, "")
            liveprog.set_pending(name, light=sum(1 for w in work if w.move is None))
            window = max(2, 2 * workers)
            starts: dict[str, float] = {}
            pending: deque = deque()

            def inflight_mark():
                _mark_inflight(name, [[x.rel, x.mtime] for x, f in pending if f is not None])

            def commit(after_rel):
                if version_stale and after_rel is not None:
                    store.set_meta(conn, "scan_resume", json.dumps({"after": after_rel, "ver": EXTRACT_VERSION}))
                conn.commit()
                state["since"], state["last_commit"] = 0, time.monotonic()
                state["committed"] += 1
                if state["committed"] % 10 == 0:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

            def consume():
                w, fut = pending.popleft()
                if w.move is not None:
                    _repoint(conn, coll, w.move, w)
                    counts["moved"] += 1
                else:
                    try:
                        prep = _await(fut, w.rel, starts, limit_s, cancel)
                    except _Overdue:
                        state["overdue"].append(fut)
                        prep = _failed_prep(w.rel, f"timed out after {int(limit_s)} s")
                    except Cancelled:
                        raise
                    except Exception as e:
                        prep = _failed_prep(w.rel, f"{type(e).__name__}: {e}"[:400])
                    r = _store_light(conn, coll, w.rel, w.size, w.mtime, w.mode, w.ino, prep)
                    counts["errors"] += bool(r["error"])
                    inflight_mark()
                counts["done"] += 1
                state["since"] += 1
                state["last_rel"] = w.rel
                if progress and (counts["done"] % 5 == 0 or counts["done"] == total):
                    progress("extract", counts["done"], total, w.rel)
                if state["since"] >= COMMIT_EVERY or time.monotonic() - state["last_commit"] >= COMMIT_SECONDS:
                    commit(w.rel)

            def stuck_now() -> int:              # reads past their limit that are still running (their threads are busy)
                return sum(1 for f in state["overdue"] if not f.done())

            def drain(limit: int):
                while pending and len(pending) >= limit and stuck_now() < workers:
                    consume()

            try:
                for w in work:
                    if stuck_now() >= workers:
                        break
                    if cancel and cancel():
                        raise Cancelled()
                    if w.move is not None:
                        pending.append((w, None))
                    else:
                        pending.append((w, ex.submit(_prepare, coll, w.rel, w.mode, starts)))
                        inflight_mark()
                    drain(window)
                drain(1)
                stalled = stuck_now() >= workers
            except Cancelled:
                cancelled = True
                commit(state["last_rel"])
                raise
            except BaseException:
                conn.rollback()
                raise
            if stalled:
                # every worker is stuck on a file that will not finish: stop here, the rest waits for the next run
                commit(state["last_rel"])
            else:
                if version_stale:
                    store.set_meta(conn, "extract_version", EXTRACT_VERSION)
                store.set_meta(conn, "scan_resume", "")
                rows = conn.execute("SELECT id, path FROM files WHERE hash IS NULL AND mode='full' LIMIT ?",
                                    (hash_budget,)).fetchall()
                for i in range(0, len(rows), 200):
                    if cancel and cancel():
                        break
                    part = rows[i:i + 200]
                    hashes = list(ex.map(lambda r: file_hash(root / r["path"]) or "", part))
                    conn.executemany("UPDATE files SET hash=? WHERE id=?", [(h, r["id"]) for h, r in zip(hashes, part)])
                    conn.commit()
                store.set_meta(conn, "last_scan", time.strftime("%Y-%m-%d %H:%M:%S"))
                conn.commit()
                if state["committed"] >= 10:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            if ex is not None:
                ex.shutdown(wait=not (stalled or cancelled or any(not f.done() for f in state["overdue"])),
                            cancel_futures=True)
            _mark_inflight(name, [])              # a clean stop: nothing is in flight any more
            conn.close()
    stats = {
        "new_or_changed": sum(1 for w in work if w.reason in ("new", "changed")),
        "removed": removed,
        "kept_unreadable": kept_unreadable,
        "errors": counts["errors"],
        "files_seen": len(seen),
        "moved": counts["moved"],
        "forced": sum(1 for w in work if w.reason == "forced"),
        "xver": sum(1 for w in work if w.reason == "xver"),
        "xver_deferred": xver_deferred,
        "skipped_failed": skipped,
        "interrupted": interrupted,
        "resumed": resume_after is not None,
        "stalled": stalled,
    }
    stats["seconds"] = round(time.time() - t0, 1)
    stats["walk_errors"] = errors[:5]
    return stats


def index_paths(coll: dict, rels: list[str], progress=None, cancel=None) -> dict:
    """Re-index exactly these paths (from file events): new or changed files, and removals. A folder is walked, and
    whatever vanished inside it is removed. A file that moved is re-pointed, not read again."""
    name = coll["name"]
    root = Path(coll["path"])
    exclude, meta_only = matchers(coll)
    max_bytes = int(coll.get("max_file_mb", 200)) * 1024 * 1024
    files: dict[str, tuple | None] = {}
    sweep: list[str] = []          # folders read in full: what is indexed under them but no longer on disk is removed
    unknown: list[str] = []        # paths that could not be read (permission, offline share): their rows are never removed
    for rel in (r.strip("/") for r in rels):
        if not rel:                                   # the collection root itself is a full scan, never a path job
            continue
        if exclude.match(rel) or exclude.match(rel + "/"):
            continue
        p = root / rel
        try:
            lst = os.lstat(p)
        except FileNotFoundError:
            files[rel] = None                         # gone: a file, or a whole folder
            continue
        except OSError:
            unknown.append(rel)                       # cannot tell whether it is there: keep what is indexed
            continue
        if S_ISDIR(lst.st_mode):
            errs: list[str] = []
            walked = list(_walk({"path": str(p), "exclude": []}, errs))
            if errs:
                unknown.append(rel)                   # an unreadable part inside: nothing under it may be removed
            else:
                sweep.append(rel)
            for r2, size, mtime, ino in walked:
                full = f"{rel}/{r2}"
                if not exclude.match(full):           # collection rules are relative to its root
                    files[full] = (size, mtime, ino)
        elif S_ISREG(lst.st_mode):
            files[rel] = (lst.st_size, lst.st_mtime, lst.st_ino)
        else:
            files[rel] = None                         # a symlink or a special file: never indexed, so nothing to keep

    def _unknown(path: str) -> bool:
        return any(path == u or path.startswith(u + "/") for u in unknown)

    changed: list[int] = []
    moved_ids: list[int] = []
    moved = removed = skipped = 0
    with store.write_lock(name):
        conn = store.connect(name)
        try:
            gone: dict[int, dict] = {}
            for rel, st in files.items():
                if st is None:
                    for g in _rows_under(conn, rel):
                        gone[g["id"]] = g
            for d in sweep:
                for g in _rows_under(conn, d):
                    if g["path"] not in files and not _unknown(g["path"]):   # gone from disk, and it could be read
                        gone[g["id"]] = g
            items: list[_Item] = []
            for rel, st in sorted(files.items()):
                if st is None:
                    continue
                size, mtime, ino = st
                mode = _mode(meta_only, rel, size, max_bytes)
                row = conn.execute("SELECT id, size, mtime, mode FROM files WHERE path=?", (rel,)).fetchone()
                if row and row["size"] == size and abs((row["mtime"] or 0) - mtime) < 1e-3 and row["mode"] == mode:
                    continue
                if row is not None and row["mtime"] == -1:
                    _clear_strikes(conn, rel)
                elif _struck_out(conn, rel, mtime):
                    skipped += 1
                    continue
                items.append(_Item(rel, size, mtime, mode, ino, "new" if row is None else "changed"))
            pairs = {}
            if gone and any(w.reason == "new" for w in items):
                pairs = _match_moves([w for w in items if w.reason == "new"], list(gone.values()),
                                     lambda its: [file_hash(root / w.rel) for w in its])
            used = {g["id"] for g in pairs.values()}
            for g in gone.values():
                if g["id"] in used:
                    continue
                store.delete_file(conn, g["id"])
                conn.execute("DELETE FROM failures WHERE path=?", (g["path"],))
                extract.cleanup_work(name, g["id"])
                (paths.THUMBS / name / f"{g['id']}.jpg").unlink(missing_ok=True)
                removed += 1
            for it in items:
                if cancel and cancel():
                    break
                if it.rel in pairs:
                    moved_ids.append(_repoint(conn, coll, pairs[it.rel], it))
                    moved += 1
                    continue
                changed.append(index_file_light(conn, coll, it.rel, it.size, it.mtime, it.mode, it.ino)["id"])
                if progress:
                    progress("extract", len(changed), len(items), it.rel)
            conn.commit()
        finally:
            conn.close()
    return {"changed": len(changed) + moved, "removed": removed, "moved": moved, "skipped_failed": skipped,
            "ids": changed + moved_ids}


def heavy_pending_ids(name: str, ids: list[int]) -> list[int]:
    conn = store.connect(name)
    q = ",".join("?" * len(ids))
    out = [r[0] for r in conn.execute(f"SELECT id FROM files WHERE heavy IS NOT NULL AND mode='full' AND id IN ({q})", ids)]
    conn.close()
    return out


def index_one(coll: dict, rel: str) -> dict:
    """Re-index a single file fully (light + heavy + embed)."""
    p = Path(coll["path"]) / rel
    exclude, meta_only = matchers(coll)
    if exclude.match(rel):
        raise ValueError("this path is excluded by the collection's rules")
    max_bytes = int(coll.get("max_file_mb", 200)) * 1024 * 1024
    with store.write_lock(coll["name"]):
        conn = store.connect(coll["name"])
        try:
            if not p.exists():
                row = conn.execute("SELECT id FROM files WHERE path=?", (rel,)).fetchone()
                if row:
                    store.delete_file(conn, row["id"])
                conn.commit()
                return {"removed": True}
            st = p.stat()
            mode = _mode(meta_only, rel, st.st_size, max_bytes)
            r = index_file_light(conn, coll, rel, st.st_size, st.st_mtime, mode, st.st_ino)
            conn.commit()
        finally:
            conn.close()
    heavy(coll, only_ids=[r["id"]])
    embed(coll, only_file_ids=[r["id"]])
    return r


def maintain(name: str) -> None:
    """After a job: flush the write-ahead log and refresh the planner statistics. Never creates a database."""
    if not store.db_path(name).exists():
        return
    with store.write_lock(name):
        conn = store.connect(name)
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            conn.execute("PRAGMA optimize")
        finally:
            conn.close()


def optimize_fts(name: str) -> None:
    """The night run: merge the keyword index's b-trees, so search stays fast after many small updates."""
    if not store.db_path(name).exists():
        return
    with store.write_lock(name):
        conn = store.connect(name)
        try:
            conn.execute("INSERT INTO fts(fts) VALUES('optimize')")
            conn.commit()
        finally:
            conn.close()


# ------------------------------------------------------------------ heavy stage

MEDIA_KINDS = ("audio", "video")
DOC_HEAVY_WORKERS = 3     # OCR / page rendering run in helper processes (Neural Engine + CPU): parallel helps


def heavy(coll: dict, progress=None, cancel=None, only_ids: list[int] | None = None,
          kinds: tuple | None = None, exclude_kinds: tuple | None = None, workers: int = 1) -> dict:
    """OCR, page images, pictures, audio/video. Smallest files first so most of the index is complete early;
    documents can run several at a time, audio/video one at a time (Whisper shares the GPU)."""
    name = coll["name"]
    root = Path(coll["path"])
    conn = store.connect(name)
    q = "SELECT id, path, kind, heavy, size FROM files WHERE heavy IS NOT NULL AND mode='full'"
    rows = [dict(r) for r in conn.execute(q)]
    conn.close()
    if only_ids is not None:
        rows = [r for r in rows if r["id"] in only_ids]
    if kinds:
        rows = [r for r in rows if r["kind"] in kinds]
    if exclude_kinds:
        rows = [r for r in rows if r["kind"] not in exclude_kinds]
    rows.sort(key=lambda r: r["size"] or 0)
    stage = "audio & video" if kinds == MEDIA_KINDS else "ocr & pages"
    counts = {"done": 0, "failed": 0, "n": 0}
    lock = threading.Lock()

    def one(r):
        if cancel and cancel():
            return
        try:
            res = extract.heavy(root / r["path"], r["kind"], json.loads(r["heavy"]), name, r["id"],
                                coll.get("media") or {})
            err = None
        except Exception as e:
            res, err = {"items": [], "thumb": False, "duration": None}, f"{type(e).__name__}: {e}"[:400]
        _store_heavy(name, r, res, err)
        if err:
            liveprog.error(name, f"{Path(r['path']).name}: {err}")
        with lock:
            counts["done" if err is None else "failed"] += 1
            counts["n"] += 1
            if progress:
                progress(stage, counts["n"], len(rows), r["path"])

    if workers > 1 and len(rows) > 1:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="heavy") as ex:
            futures = [ex.submit(one, r) for r in rows]
            for f in as_completed(futures):
                f.result()
                if cancel and cancel():
                    ex.shutdown(wait=False, cancel_futures=True)
                    raise Cancelled()
    else:
        for r in rows:
            if cancel and cancel():
                raise Cancelled()
            one(r)
    if rows:
        with store.write_lock(name):
            conn = store.connect(name)
            store.set_meta(conn, "last_heavy", time.strftime("%Y-%m-%d %H:%M:%S"))
            conn.commit()
            conn.close()
    return {"files": len(rows), "done": counts["done"], "failed": counts["failed"]}


def _store_heavy(name: str, r: dict, res: dict, err: str | None) -> None:
    with store.write_lock(name):
        conn = store.connect(name)
        exists = conn.execute("SELECT 1 FROM files WHERE id=?", (r["id"],)).fetchone()
        if exists:
            store.replace_items(conn, r["id"], name_tokens(r["path"]), res["items"],
                                modalities={"ocr", "image", "page", "audio", "video", "transcript"})
            conn.execute("UPDATE files SET heavy=NULL, heavy_at=?, thumb=?, duration=COALESCE(?, duration),"
                         " error=COALESCE(?, error), state=CASE WHEN ? IS NULL THEN state ELSE 'error' END"
                         " WHERE id=?", (time.time(), int(bool(res["thumb"])), res["duration"], err, err, r["id"]))
            conn.commit()
        conn.close()


# ------------------------------------------------------------------ embedding stage

def _stem(name: str | None) -> str:
    return re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name or "").replace("_", " ")


def item_prompt(r: dict) -> str:
    """The exact text a chunk is embedded as (also the cache key)."""
    if r["modality"] == "title":
        return doc_prompt(None, r["text"])
    head = r["heading"] if r["heading"] and not r["heading"].startswith("Page ") else None
    return doc_prompt(r["title"] or _stem(r["name"]), (head + "\n" if head else "") + (r["text"] or ""))


def prompt_key(prompt: str) -> str:
    return hashlib.blake2b(prompt.encode("utf-8"), digest_size=16).hexdigest()


def backfill_vcache(conn) -> None:
    """One-time: seed the vector cache from vectors computed before the cache existed."""
    if store.get_meta(conn, "vcache_backfilled") == "1":
        return
    rows = conn.execute(
        "SELECT i.modality, i.heading, i.text, f.title, f.name, v.v FROM items i JOIN files f ON f.id = i.file_id"
        f" JOIN vectors v ON v.item_id = i.id WHERE i.modality IN {TEXT_MODALITIES}")
    batch = []
    for r in rows:
        batch.append((prompt_key(item_prompt(dict(r))), r["v"]))
        if len(batch) >= 2000:
            conn.executemany("INSERT OR IGNORE INTO vcache(key, v) VALUES (?, ?)", batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT OR IGNORE INTO vcache(key, v) VALUES (?, ?)", batch)
    store.set_meta(conn, "vcache_backfilled", "1")


def pending_count(name: str, media: bool | None = None) -> int:
    conn = store.connect(name)
    q = "SELECT COUNT(*) FROM items WHERE embedded=0"
    if media is True:
        q += f" AND modality IN {MEDIA_MODALITIES}"
    elif media is False:
        q += f" AND modality IN {TEXT_MODALITIES}"
    n = conn.execute(q).fetchone()[0]
    conn.close()
    return n


def embed(coll: dict, progress=None, cancel=None, media: bool = True, only_file_ids: list[int] | None = None,
          batch_priority: bool = False) -> dict:
    name = coll["name"]
    p = pool()
    p.workers["text"].batch_priority = p.workers["full"].batch_priority = batch_priority
    conn = store.connect(name)
    flt = ""
    if only_file_ids:
        flt = f" AND i.file_id IN ({','.join(str(int(x)) for x in only_file_ids)})"
    text_rows = [dict(r) for r in conn.execute(
        "SELECT i.id, i.modality, i.heading, i.text, f.title, f.name FROM items i JOIN files f ON f.id=i.file_id"
        f" WHERE i.embedded=0 AND i.modality IN {TEXT_MODALITIES}{flt} ORDER BY i.id")]
    media_rows = [dict(r) for r in conn.execute(
        "SELECT i.id, i.file_id, i.modality, i.media, f.title, f.name FROM items i JOIN files f ON f.id=i.file_id"
        f" WHERE i.embedded=0 AND i.modality IN {MEDIA_MODALITIES}{flt} ORDER BY i.file_id, i.id")] if media else []
    conn.close()
    total = len(text_rows) + len(media_rows)
    done = failed = reused = 0
    for r in text_rows:
        r["prompt"] = item_prompt(r)
        r["key"] = prompt_key(r["prompt"])
    group = lambda r: "text" if r["modality"] != "title" else "title"
    # Identical inputs (copies of a file, re-extracted chunks) reuse their cached vector.
    with store.write_lock(name):
        conn = store.connect(name)
        backfill_vcache(conn)
        hits = {}
        keys = [r["key"] for r in text_rows]
        for i in range(0, len(keys), 900):
            part = keys[i:i + 900]
            hits.update(conn.execute(f"SELECT key, v FROM vcache WHERE key IN ({','.join('?' * len(part))})", part))
        cached = [r for r in text_rows if r["key"] in hits]
        for i in range(0, len(cached), 500):
            store.store_vectors(conn, [(r["id"], group(r), store.from_f16(hits[r["key"]])) for r in cached[i:i + 500]])
        conn.commit()
        conn.close()
    reused = len(cached)
    done += reused
    text_rows = [r for r in text_rows if r["key"] not in hits]
    # Similar lengths in one batch: padding to the longest item is what makes mixed batches slow.
    text_rows.sort(key=lambda r: len(r["prompt"]))
    B = 32
    for i in range(0, len(text_rows), B):
        if cancel and cancel():
            raise Cancelled()
        part = text_rows[i:i + B]
        vecs = p.embed_texts([r["prompt"] for r in part], batch=B if len(part[-1]["prompt"]) < 600 else 16)
        with store.write_lock(name):
            conn = store.connect(name)
            store.store_vectors(conn, [(r["id"], group(r), v) for r, v in zip(part, vecs)])
            conn.executemany("INSERT OR REPLACE INTO vcache(key, v) VALUES (?, ?)",
                             [(r["key"], store.to_f16(v)) for r, v in zip(part, vecs)])
            conn.commit()
            conn.close()
        done += len(part)
        if progress:
            progress("embedding text", done, total, "")
    MB = 4
    touched_files = set()
    for i in range(0, len(media_rows), MB):
        if cancel and cancel():
            raise Cancelled()
        part = [r for r in media_rows[i:i + MB]]
        ok_rows, bad_rows = [], []
        reqs = []
        for r in part:
            if r["media"] and os.path.exists(r["media"]):
                reqs.append(r)
            else:
                bad_rows.append(r)
        if reqs:
            items = [{"type": "image" if r["modality"] in ("image", "page") else r["modality"],
                      "path": r["media"], "title": r["title"] or _stem(r["name"])} for r in reqs]
            try:
                vecs, oks = p.embed_media(items)
            except Exception:
                vecs, oks = None, [False] * len(reqs)
            for r, ok, k in zip(reqs, oks, range(len(reqs))):
                (ok_rows.append((r, vecs[k])) if ok and vecs is not None and np.isfinite(vecs[k]).all()
                 else bad_rows.append(r))
        with store.write_lock(name):
            conn = store.connect(name)
            group = {"image": "visual", "page": "visual", "video": "visual", "audio": "audio"}
            store.store_vectors(conn, [(r["id"], group[r["modality"]], v) for r, v in ok_rows])
            for r in bad_rows:
                conn.execute("UPDATE items SET embedded=-1 WHERE id=?", (r["id"],))
            conn.commit()
            conn.close()
        for r in part:
            touched_files.add(r["file_id"])
        done += len(part)
        failed += len(bad_rows)
        if progress:
            progress("embedding media", done, total, "")
    for fid in touched_files:
        extract.cleanup_work(name, fid)
    if total:
        with store.write_lock(name):
            conn = store.connect(name)
            store.set_meta(conn, "last_embed", time.strftime("%Y-%m-%d %H:%M:%S"))
            conn.commit()
            conn.close()
    return {"embedded": done - failed, "reused": reused, "failed": failed}


# ------------------------------------------------------------------ vocabulary (cross-language bridge)

_TERM_OK = re.compile(r"^[a-z][a-z\-]{3,30}$")


def build_vocab(coll: dict, progress=None, cancel=None, max_terms: int = 25000) -> dict:
    """Embed the collection's own vocabulary so a query can be bridged to the words documents actually use."""
    name = coll["name"]
    conn = store.connect(name)
    rows = conn.execute("SELECT term, doc FROM fts_terms WHERE doc >= 2 ORDER BY doc DESC LIMIT ?",
                        (max_terms * 2,)).fetchall()
    terms = {}
    for t, df in rows:
        if _TERM_OK.match(t) and t not in STOP and not re.search(r"(.)\1\1", t):
            terms[t] = df
        if len(terms) >= max_terms:
            break
    have = {r[0]: r[1] for r in conn.execute("SELECT term, id FROM vocab")}
    conn.close()
    new = [t for t in terms if t not in have]
    gone = [have[t] for t in have if t not in terms]
    p = pool()
    B = 128
    for i in range(0, len(new), B):
        if cancel and cancel():
            raise Cancelled()
        part = new[i:i + B]
        vecs = p.embed_texts([QUERY + t for t in part], batch=B)
        with store.write_lock(name):
            conn = store.connect(name)
            for t, v in zip(part, vecs):
                tid = conn.execute("INSERT OR REPLACE INTO vocab(term, df) VALUES (?,?)", (t, terms[t])).lastrowid
                conn.execute("DELETE FROM vvocab WHERE id=?", (tid,))
                conn.execute("INSERT INTO vvocab(id, embedding) VALUES (?, ?)", (tid, np.asarray(v, np.float32).tobytes()))
            conn.commit()
            conn.close()
        if progress:
            progress("vocabulary", min(i + B, len(new)), len(new), "")
    if gone:
        with store.write_lock(name):
            conn = store.connect(name)
            for k in range(0, len(gone), 500):
                part = gone[k:k + 500]
                q = ",".join("?" * len(part))
                conn.execute(f"DELETE FROM vocab WHERE id IN ({q})", part)
                conn.execute(f"DELETE FROM vvocab WHERE id IN ({q})", part)
            conn.commit()
            conn.close()
    return {"terms": len(terms), "added": len(new), "removed": len(gone)}


def drop_collection_data(name: str) -> str:
    """Move a removed collection's derived data to trash (never touches the indexed folder)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = paths.TRASH / f"{name}-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        f = paths.COLLECTIONS / f"{name}.db{suffix}"
        if f.exists():
            shutil.move(str(f), dest / f.name)
    for d in (paths.THUMBS / name, paths.TMP / name):
        if d.exists():
            shutil.move(str(d), dest / d.name)
    return str(dest)
