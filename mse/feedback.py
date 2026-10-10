"""Search feedback: the files a person marked as the right answer ("best") or as noise ("hide") for a query.

Stored in <data>/feedback.db, one row per (normalised query, collection, path); marking again updates the row.
Applied inside search.search() after results are grouped and before re-ranking and the limit cut:

  same query     every 'best' file comes first, in the order it was marked. A file retrieval missed is loaded
                 from the collection index, but only if it still exists and passes the active filters and scope.
                 Every 'hide' file is removed.
  similar query  'best' files that are already among the results get a score bonus that grows with how close the
                 two queries are (cosine of their query embeddings >= SIMILAR). They are not forced first or
                 injected, and 'hide' does not carry over.

Files are identified by (collection, path), falling back to docid; a result also matches through the other
versions folded into it ("copies"). Feedback never fails a search: search.py catches anything raised here.
"""

import re
import sqlite3
import time
import unicodedata
from pathlib import Path

import numpy as np

from . import config, paths, store
from .textutil import fold, snippet

KINDS = ("best", "hide")
DIMS = 768

# Query-to-query cosine of the EmbeddingGemma query embeddings, measured on a private 28-question gold set
# (2026-10-07). The example names are stand-ins; the numbers are measured:
#   rewordings of one question   0.94-0.99  ("Acme invoice" ~ "invoice from Acme" 0.991, ~ "acme invoices" 0.941,
#                                            "courier invoice" ~ "courier bill" 0.946)
#   different questions          up to 0.889 ("Acme invoice" ~ "Acme credit note" 0.889; the 28 gold-set questions
#                                            among themselves: max 0.888, p99 0.80)
#   EN<->DE translations         0.75-0.86, indistinguishable from different questions, so they get no boost.
# 0.88 would let "Acme credit note" inherit the favourite of "Acme invoice"; 0.90 separates the two groups.
SIMILAR = 0.90
SIM_FULL = 0.97                    # at and above this similarity the bonus is at its maximum
BONUS_MIN, BONUS_MAX = 0.15, 0.50  # bonus as a fraction of the best remaining score in this result list

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY,
  query TEXT NOT NULL,
  query_norm TEXT NOT NULL,
  qvec BLOB,
  collection TEXT NOT NULL,
  path TEXT NOT NULL,
  docid TEXT,
  kind TEXT NOT NULL CHECK(kind IN ('best','hide')),
  created REAL,
  uses INTEGER DEFAULT 0,
  last_used REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS feedback_key ON feedback(query_norm, collection, path);
"""

FIELDS = ("id", "query", "collection", "path", "docid", "kind", "created", "uses", "last_used")


def db_path() -> Path:
    return paths.DATA / "feedback.db"


def _connect(create: bool = False) -> sqlite3.Connection | None:
    p = db_path()
    if not create and not p.exists():
        return None
    if create:
        paths.ensure_dirs()
    conn = sqlite3.connect(p, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if create:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        try:
            p.chmod(0o600)
        except OSError:
            pass
    return conn


# ---------------------------------------------------------------- queries

def _norm_text(text: str) -> str:
    s = fold(text or "")
    s = re.sub(r"[\"'“”„‘’`?!,;:()\[\]{}<>]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize(query: str) -> str:
    """What makes two queries 'the same': lowercase, accents folded, whitespace collapsed, inline filters
    (type:pdf, after:…) and date phrases ("last month") removed — those stay filters at search time — and the
    scope shortcut too ("w: invoice" is the query "invoice", searched in w)."""
    from .search import parse
    return _norm_text(parse(_unscoped(query))[0])


def _unscoped(query: str) -> str:
    """The query without its scope shortcut, as the search that shows the results sees it (config.strip_scope)."""
    try:
        return config.strip_scope(query or "")
    except Exception:            # a broken config must not cost a favourite
        return query or ""


def _embed(query: str) -> bytes | None:
    """The query's embedding as search.search() computes it (cached), stored as normalised float16."""
    from . import search
    text = search.parse(query)[0]
    try:
        v = np.asarray(search.embed_query(text), dtype=np.float32)
    except Exception:
        return None                       # model unavailable: the row still works for the exact query
    if v.shape != (DIMS,):
        return None
    return store.to_f16(v / (np.linalg.norm(v) + 1e-9))


def resolve(collection: str | None, ref: str) -> tuple[dict, dict]:
    """(collection config, file row) for a #docid, relative or absolute path. Also tries the other Unicode
    normalisation, since macOS paths and typed text disagree about 'ä'."""
    from .search import find_file
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("no file given")
    for r in dict.fromkeys([ref, unicodedata.normalize("NFC", ref), unicodedata.normalize("NFD", ref)]):
        try:
            found = find_file(collection or None, r)
        except (ValueError, OSError):
            found = None
        if found:
            return found
    raise KeyError(f"file not found: {ref}" + (f" in {collection}" if collection else ""))


# ---------------------------------------------------------------- storage

def add(query: str, collection: str | None, ref: str, kind: str = "best") -> int:
    """Record that `ref` is the right answer ('best') or noise ('hide') for `query`. Returns the row id;
    marking the same file for the same query again updates that row."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    norm = normalize(query)
    query = _unscoped(query)            # kept as the search saw it: "w: invoice" is the query "invoice"
    if not norm:
        raise ValueError("the query has no search words")
    coll, f = resolve(collection, ref)
    qvec = _embed(query)
    now = time.time()
    conn = _connect(create=True)
    try:
        old = conn.execute("SELECT id, kind FROM feedback WHERE query_norm=? AND collection=? AND path=?",
                           (norm, coll["name"], f["path"])).fetchone()
        if old:
            # a changed verdict counts as a new mark (it moves to the end of the pinned order)
            conn.execute("UPDATE feedback SET qvec=COALESCE(qvec, ?), docid=?, kind=?,"
                         " created=CASE WHEN kind=? THEN created ELSE ? END WHERE id=?",
                         (qvec, f["docid"], kind, kind, now, old["id"]))
            fid = old["id"]
        else:
            fid = conn.execute("INSERT INTO feedback(query, query_norm, qvec, collection, path, docid, kind, created,"
                               " uses, last_used) VALUES (?,?,?,?,?,?,?,?,0,NULL)",
                               (query.strip(), norm, qvec, coll["name"], f["path"], f["docid"], kind, now)).lastrowid
        conn.commit()
        return fid
    finally:
        conn.close()


def remove(id: int | None = None, query: str | None = None, collection: str | None = None,
           ref: str | None = None) -> int:
    """Delete one row by id, or the rows for (query, file). Works for files that are gone from the index."""
    conn = _connect()
    if conn is None:
        return 0
    try:
        if id is not None:
            n = conn.execute("DELETE FROM feedback WHERE id=?", (int(id),)).rowcount
        else:
            norm = normalize(query or "")
            if not norm or not ref:
                raise ValueError("give an id, or a query and a path")
            ref = ref.strip()
            refs, docid = {ref, unicodedata.normalize("NFC", ref), unicodedata.normalize("NFD", ref)}, None
            if ref.startswith("#"):
                docid = ref[1:]
            try:
                c, f = resolve(collection, ref)
                refs.add(f["path"])
                docid, collection = f["docid"], collection or c["name"]
            except (KeyError, ValueError):
                pass
            q = ",".join("?" * len(refs))
            sql = f"DELETE FROM feedback WHERE query_norm=? AND (path IN ({q}) OR (? IS NOT NULL AND docid=?))"
            args = [norm, *refs, docid, docid]
            if collection:
                sql += " AND collection=?"
                args.append(collection)
            n = conn.execute(sql, args).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def list_items(q: str | None = None) -> list[dict]:
    """All feedback, newest first; `q` filters on the query or the path."""
    conn = _connect()
    if conn is None:
        return []
    try:
        sql, args = f"SELECT {', '.join(FIELDS)} FROM feedback", []
        if q and q.strip():
            sql += " WHERE query_norm LIKE ? OR lower(path) LIKE ?"
            args = [f"%{_norm_text(q)}%", f"%{q.strip().lower()}%"]
        return [dict(r) for r in conn.execute(sql + " ORDER BY created DESC, id DESC", args)]
    finally:
        conn.close()


def gold() -> list[dict]:
    """Every query with 'best' files as an evaluation question: query -> the files marked for it."""
    conn = _connect()
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT query, query_norm, collection, path FROM feedback WHERE kind='best'"
                            " ORDER BY created, id").fetchall()
    finally:
        conn.close()
    out: dict[tuple, dict] = {}
    for r in rows:
        g = out.setdefault((r["query_norm"], r["collection"]),
                           {"q": r["query"], "expect": [], "collection": r["collection"], "source": "favourite"})
        g["expect"].append(r["path"])
    return list(out.values())


# ---------------------------------------------------------------- applying feedback to a result list

def _matches(res: dict, coll: str, path: str, docid: str | None) -> bool:
    if res["collection"] != coll:
        return False
    versions = [(res["path"], res.get("docid"))] + [(c["path"], c.get("docid")) for c in res.get("copies") or []]
    return any(p == path for p, _ in versions) or bool(docid) and any(d == docid for _, d in versions)


def _load_file(coll: dict, row, ok, exclude) -> dict | None:
    """The file behind a feedback row, if it is still indexed, still on disk and allowed by the filters."""
    try:
        conn = store.connect(coll["name"], readonly=True)
    except FileNotFoundError:
        return None
    try:
        f = conn.execute("SELECT * FROM files WHERE path=?", (row["path"],)).fetchone()
        if f is None and row["docid"]:
            f = conn.execute("SELECT * FROM files WHERE docid=?", (row["docid"],)).fetchone()
        if f is None:
            return None
        f = dict(f)
        f["_collection"] = coll["name"]
        if not (Path(coll["path"]) / f["path"]).exists():
            return None
        if ok is not None and not ok(f):
            return None
        if exclude and (coll["name"], f["path"]) in exclude:
            return None
        f["_items"] = [dict(r) for r in conn.execute(
            "SELECT modality, page, t0, t1, heading, text FROM items WHERE file_id=? AND text IS NOT NULL"
            " AND modality IN ('text','ocr','transcript','title') ORDER BY ord LIMIT 300", (f["id"],))]
        return f
    finally:
        conn.close()


def _file_fields(coll: dict, f: dict) -> dict:
    from .search import context_for
    return {"collection": coll["name"], "path": f["path"], "abs_path": str(Path(coll["path"]) / f["path"]),
            "name": f["name"], "ext": f["ext"], "kind": f["kind"], "title": f["title"], "docid": f["docid"],
            "date": f["doc_date"], "size": f["size"], "pages": f["pages"], "duration": f["duration"],
            "thumb": bool(f["thumb"]), "metadata_only": f["mode"] == "meta", "lang": f["lang"],
            "context": context_for(coll, f["path"])}


def _injected(coll: dict, f: dict, terms: list[str]) -> dict:
    """A result entry for a favourite that retrieval did not find, with its best-matching passage."""
    keys = [fold(t) for t in terms or [] if len(t) >= 2]
    best = max(f["_items"], key=lambda it: sum(k in fold(it["text"]) for k in keys), default=None)
    hits = []
    if best and f["mode"] != "meta":
        hits.append({"modality": best["modality"], "page": best["page"], "t0": best["t0"], "t1": best["t1"],
                     "heading": best["heading"], "why": ["favourite"], "similarity": None,
                     "snippet": snippet(best["text"], terms or [])})
    return {**_file_fields(coll, f), "score": 0.0, "similarity": None, "why": ["favourite"], "hits": hits,
            "copies": []}


def _promote(res: dict, coll: dict, f: dict) -> dict:
    """Show the marked file as the result's representative when it is another version of the same document."""
    if res["path"] == f["path"]:
        return res
    new = {**res, **_file_fields(coll, f)}
    new["copies"] = [{"path": res["path"], "docid": res["docid"], "kind": res["kind"]}] + \
        [c for c in res.get("copies") or [] if c["path"] != f["path"]]
    hits = []
    for h in res.get("hits") or []:
        h = dict(h)
        src = h.pop("from", res["path"])
        if src != f["path"]:
            h["from"] = src
        hits.append(h)
    new["hits"] = hits
    return new


def apply(text: str, qvec, results: list[dict], scope: dict, ok=None, exclude: set | None = None,
          terms: list[str] | None = None, collapse: bool = True) -> tuple[list[dict], dict]:
    """Apply stored feedback to grouped results (sorted by score). `scope` maps collection name -> its config,
    `ok` is the active file filter (None when there is none). Returns (results, info)."""
    from .search import group_key
    info = {"applied": 0, "pinned": 0, "injected": 0, "hidden": 0, "boosted": 0}
    norm = _norm_text(text)
    conn = _connect() if norm else None
    if conn is None:
        return results, info
    try:
        exact = [r for r in conn.execute(
            "SELECT id, collection, path, docid, kind FROM feedback WHERE query_norm=? ORDER BY created, id", (norm,))
            if r["collection"] in scope]
        near = []
        if qvec is not None:
            near = [r for r in conn.execute(
                "SELECT id, collection, path, docid, qvec FROM feedback WHERE kind='best' AND qvec IS NOT NULL"
                " AND query_norm != ?", (norm,)) if r["collection"] in scope and len(r["qvec"]) == 2 * DIMS]
    finally:
        conn.close()
    if not exact and not near:
        return results, info

    used: set[int] = set()
    rest = list(results)
    # same query, hide: drop the document (every version of it)
    for row in (r for r in exact if r["kind"] == "hide"):
        keep = [x for x in rest if not _matches(x, row["collection"], row["path"], row["docid"])]
        if len(keep) < len(rest):
            info["hidden"] += len(rest) - len(keep)
            used.add(row["id"])
        rest = keep
    # same query, best: first, in the order marked
    pinned: list[dict] = []
    for row in (r for r in exact if r["kind"] == "best"):
        coll, args = scope[row["collection"]], (row["collection"], row["path"], row["docid"])
        if any(_matches(p, *args) for p in pinned):
            used.add(row["id"])
            continue
        i = next((i for i, x in enumerate(rest) if _matches(x, *args)), None)
        if i is not None:
            if not Path(rest[i]["abs_path"]).exists():     # deleted, index not caught up yet: no pin
                continue
            res = rest.pop(i)
            if res["path"] != row["path"]:               # marked file is a folded version: show it
                f = _load_file(coll, row, None, None)
                res = _promote(res, coll, f) if f else res
        else:
            f = _load_file(coll, row, ok, exclude)
            if f is None:                                  # gone, filtered out, or outside the scope
                continue
            gk = group_key(f["path"], f.get("hash")) if collapse else None
            j = next((j for j, x in enumerate(rest) if gk and x["collection"] == row["collection"]
                      and group_key(x["path"], None) == gk), None)
            if j is not None:                              # another version of it was retrieved
                res = _promote(rest.pop(j), coll, f)
            else:
                res = _injected(coll, f, terms or [])
                info["injected"] += 1
        res["favorite"] = "query"
        pinned.append(res)
        used.add(row["id"])
    # similar query, best: a bonus for files that were retrieved anyway
    bonus: dict[int, float] = {}
    if near and rest:
        qv = np.asarray(qvec, dtype=np.float32)
        qv = qv / (np.linalg.norm(qv) + 1e-9)
        Q = np.stack([store.from_f16(r["qvec"]) for r in near])
        sims = Q @ qv / (np.linalg.norm(Q, axis=1) + 1e-9)
        top = max(x["score"] for x in rest)
        for row, s in zip(near, sims):
            if s < SIMILAR:
                continue
            i = next((i for i, x in enumerate(rest) if _matches(x, row["collection"], row["path"], row["docid"])), None)
            if i is None:
                continue
            w = min(1.0, (float(s) - SIMILAR) / (SIM_FULL - SIMILAR))
            bonus[i] = max(bonus.get(i, 0.0), top * (BONUS_MIN + (BONUS_MAX - BONUS_MIN) * w))
            used.add(row["id"])
    for x in rest:
        x["favorite"] = None
    for i, b in bonus.items():
        rest[i]["score"] += b
        rest[i]["favorite"] = "similar"
    info["boosted"] = len(bonus)
    if bonus:
        rest.sort(key=lambda x: -x["score"])
    if pinned:
        # pinned results share the top score, so scores stay monotone and their relevance reads 100
        top = max([rest[0]["score"] if rest else 0.0] + [p.get("score") or 0.0 for p in pinned]) or 1.0
        for p in pinned:
            p["score"] = top
    info["pinned"] = len(pinned)
    info["applied"] = len(used)
    if used:
        _touch(used)
    return pinned + rest, info


def _touch(ids: set[int]) -> None:
    """Count the searches in which a row took effect. Best effort: never slows down or fails a search."""
    try:
        conn = sqlite3.connect(db_path(), timeout=0.5)
        try:
            q = ",".join("?" * len(ids))
            conn.execute(f"UPDATE feedback SET uses = uses + 1, last_used = ? WHERE id IN ({q})", (time.time(), *ids))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass
