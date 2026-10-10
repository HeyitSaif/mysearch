"""Hybrid multimodal search.

For every collection in scope:
  keyword   — FTS5 BM25 over chunk text, file names and headings (diacritic-insensitive)
  bridge    — the query embedded and matched against the collection's own vocabulary, so an English
              query also looks for the German words the documents use (and vice versa)
  meaning   — EmbeddingGemma 2 vectors: text chunks, titles, images / PDF pages / video, audio,
              found with a 1-bit index and re-scored exactly with float16 vectors
Lists are merged with reciprocal-rank fusion, then grouped by file.
"""

import re
import shlex
import threading
import time
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

import numpy as np

import json

from . import config, store
from .embedder import pool
from .ignore import Matcher
from .textutil import detect_lang, fold, fts_query, query_terms, snippet

RRF_K = 60
# List weights for reciprocal-rank fusion (tuned on the gold sets in eval/; see `mse eval`)
DEFAULT_WEIGHTS = {"keyword": 0.6, "translated": 0.6, "meaning": 1.0, "name": 0.55, "visual": 0.75, "audio": 0.6}
HUB_NAMES = re.compile(r"(^|/)(summary|readme|index|manifest|document_map|todo|changelog|overview)[^/]*$|_roadmap", re.I)
VECTOR_GROUPS = {          # group -> (list weight, label)
    "text": (1.0, "meaning"),
    "title": (0.55, "name"),
    "visual": (0.75, "visual"),
    "audio": (0.6, "audio"),
}
KIND_ALIASES = {"doc": "office", "docs": "office", "word": "office", "excel": "office", "sheet": "office",
                "mail": "email", "photo": "image", "picture": "image", "images": "image", "pic": "image",
                "movie": "video", "sound": "audio", "recording": "audio", "note": "text", "md": "text"}

_qcache: OrderedDict[str, np.ndarray] = OrderedDict()
_qlock = threading.Lock()


def embed_query(q: str) -> np.ndarray:
    with _qlock:
        if q in _qcache:
            _qcache.move_to_end(q)
            return _qcache[q]
    v = pool().embed_query(q)
    v = v / (np.linalg.norm(v) + 1e-9)
    with _qlock:
        _qcache[q] = v
        while len(_qcache) > 256:
            _qcache.popitem(last=False)
    return v


# ------------------------------------------------------------------ query parsing

FILTER_KEYS = config.FILTER_KEYS   # reserved there: never the name or alias of a folder or group


MONTHS = {}
for _i, _names in enumerate([("january", "januar", "jan", "jän"), ("february", "februar", "feb"),
                             ("march", "märz", "maerz", "mar", "mär"), ("april", "apr"), ("may", "mai"),
                             ("june", "juni", "jun"), ("july", "juli", "jul"), ("august", "aug"),
                             ("september", "sept", "sep"), ("october", "oktober", "oct", "okt"),
                             ("november", "nov"), ("december", "dezember", "dec", "dez")], 1):
    for _n in _names:
        MONTHS[_n] = _i
_MONTH_RX = "|".join(sorted(MONTHS, key=len, reverse=True))
_PREP = r"(?:in|from|during|since|of|im|vom|aus|seit|von)"


def date_phrases(text: str, today=None) -> tuple[str, str | None, str | None]:
    """'invoices from September', 'Rechnungen letzten Monat', 'notes last week' -> (remaining text, after, before)."""
    from datetime import date, timedelta
    today = today or date.today()
    t = text
    after = before = None

    def month_range(y, m):
        a = date(y, m, 1)
        b = date(y + (m == 12), m % 12 + 1, 1)
        return a.isoformat(), b.isoformat()

    rel = [
        (r"\b(today|heute)\b", lambda: (today.isoformat(), (today + timedelta(1)).isoformat())),
        (r"\b(yesterday|gestern)\b", lambda: ((today - timedelta(1)).isoformat(), today.isoformat())),
        (r"\b(this week|diese woche)\b", lambda: ((today - timedelta(today.weekday())).isoformat(), None)),
        (r"\b(last week|letzte woche|vorige woche)\b",
         lambda: ((today - timedelta(today.weekday() + 7)).isoformat(), (today - timedelta(today.weekday())).isoformat())),
        (r"\b(this month|diesen monat|dieser monat)\b", lambda: month_range(today.year, today.month)),
        (r"\b(last month|letzten monat|letzter monat|vormonat|vorigen monat)\b",
         lambda: month_range(today.year - (today.month == 1), (today.month - 2) % 12 + 1)),
        (r"\b(this year|dieses jahr|diesem jahr)\b", lambda: (f"{today.year}-01-01", None)),
        (r"\b(last year|letztes jahr|letzten jahr|vorjahr)\b", lambda: (f"{today.year - 1}-01-01", f"{today.year}-01-01")),
    ]
    for rx, fn in rel:
        m = re.search(rx, t, re.I)
        if m:
            after, before = fn()
            t = t[:m.start()] + t[m.end():]
            break
    if after is None:
        m = re.search(rf"\b{_PREP}?\s*({_MONTH_RX})\.?\s+(20\d\d)\b", t, re.I) or \
            re.search(rf"\b{_PREP}\s+({_MONTH_RX})\b\.?", t, re.I)
        if m:
            mon = MONTHS[m.group(1).lower()]
            year = int(m.group(2)) if m.lastindex and m.lastindex >= 2 else \
                (today.year if mon <= today.month else today.year - 1)
            after, before = month_range(year, mon)
            t = t[:m.start()] + t[m.end():]
        else:
            m = re.search(rf"\b(?:{_PREP}|im jahr|in year)\s+(20\d\d|19\d\d)\b", t, re.I)
            if m:
                y = int(m.group(1))
                after, before = f"{y}-01-01", f"{y + 1}-01-01"
                t = t[:m.start()] + t[m.end():]
    t = re.sub(r"\s+", " ", t).strip()
    if after is None or not re.search(r"\w", t):
        return text, None, None          # a date alone is content, not a filter
    return t, after, before


def parse(q: str) -> tuple[str, dict, list[str]]:
    filters, phrases, words = {}, [], []
    try:
        tokens = shlex.split(q)
    except ValueError:
        tokens = q.split()
    for t in tokens:
        m = re.match(r"^(\w+):(.+)$", t)
        if m and m.group(1).lower() in FILTER_KEYS:
            filters.setdefault(m.group(1).lower(), []).append(m.group(2))
            continue
        if " " in t:
            phrases.append(t)
        words.append(t)
    text = " ".join(words).strip()
    if "after" not in filters and "before" not in filters:
        text2, after, before = date_phrases(text)
        if after:
            text = text2
            filters["after"] = [after]
            if before:
                filters["before"] = [before]
    return text, filters, phrases


def folder_values(values) -> tuple[list[str], list[str]]:
    """The values of in: filters -> (folder paths, MySearch folder/group names). A value that starts with ~ or / is a
    folder path; anything else names a collection, group or alias (config.scope_shortcut handles those)."""
    import os
    folders, names = [], []
    for v in values:
        v = str(v)
        parts = re.split(r",(?=[~/])", v) if v.strip().startswith(("~", "/")) else v.split(",")
        for part in parts:
            part = part.strip()
            if part.startswith(("~", "/")):
                folders.append(os.path.normpath(os.path.expanduser(part)))
            elif part:
                names.append(part)
    return folders, names


def folder_prefixes(folders: list[str], root: str) -> list[str] | None:
    """For one collection: None = none of the folders touches it; [] = no restriction (a folder contains the whole
    collection); otherwise the collection-relative prefixes (with a trailing slash) a path must start with."""
    root = root.rstrip("/")
    out = []
    for f in folders:
        f = f.rstrip("/") or "/"
        if root == f or root.startswith(f.rstrip("/") + "/"):
            return []
        if f.startswith(root + "/"):
            out.append(f[len(root) + 1:] + "/")
    return out or None


def file_filter(filters: dict):
    kinds = {KIND_ALIASES.get(k.lower(), k.lower()) for k in filters.get("type", []) + filters.get("kind", [])}
    exts = {e.lower().lstrip(".") for e in filters.get("ext", [])}
    for k in list(kinds):
        if k not in ("pdf", "text", "office", "email", "image", "audio", "video", "other"):
            exts.add(k)
            kinds.discard(k)
    after = (filters.get("after") or [None])[0]
    before = (filters.get("before") or [None])[0]
    prefixes = [p.lower().strip("/") for p in filters.get("path", [])]
    # _under: groups of collection-relative folder prefixes (each ending in "/"); a path must be in every group, in at
    # least one prefix of it. Set by in:~/… folders and by Ask's subfolder scope -- unlike path, never a bare startswith
    under = [[p.lower() for p in g] for g in filters.get("_under", []) if g]
    split = lambda key: [_glob(p) for v in filters.get(key, []) for p in str(v).split(",") if p.strip()]
    inc, exc = Matcher(split("include")), Matcher(split("exclude"))

    def ok(f) -> bool:
        if kinds and f["kind"] not in kinds and not (exts and f["ext"] in exts):
            return False
        if exts and not kinds and f["ext"] not in exts:
            return False
        if after and (f["doc_date"] or "") < after:
            return False
        if before and (f["doc_date"] or "9999") >= before:
            return False
        if prefixes and not any(f["path"].lower().startswith(p) for p in prefixes):
            return False
        if under:
            low = f["path"].lower()
            if not all(any(low.startswith(p) for p in g) for g in under):
                return False
        if inc and not inc.match(f["path"]):
            return False
        if exc and exc.match(f["path"]):
            return False
        return True

    active = bool(kinds or exts or after or before or prefixes or under or inc or exc)
    return ok, active


def _glob(p: str) -> str:
    """Search filters accept what people type: '.md' and 'md' mean '*.md'; real globs and paths pass through."""
    p = p.strip()
    if re.fullmatch(r"\.?[A-Za-z0-9]{1,8}", p) and (p.startswith(".") or p.lower() in EXT_WORDS):
        return "*." + p.lstrip(".")
    return p


EXT_WORDS = {"md", "txt", "pdf", "json", "csv", "eml", "html", "xlsx", "docx", "pptx", "jpg", "jpeg", "png", "heic",
             "mp3", "m4a", "mp4", "mov", "wav", "py", "js", "ts", "yaml", "yml", "xml", "doc", "xls", "zip"}


def allowed_files(conn, ok) -> set[int]:
    """File ids that pass the filters — applied inside retrieval so narrow filters still fill the result list."""
    return {r["id"] for r in conn.execute("SELECT id, path, kind, ext, doc_date FROM files") if ok(r)}


# ------------------------------------------------------------------ retrieval per collection

def _keyword(conn, fq: str, k: int, allowed: set[int] | None = None) -> list[int]:
    try:
        if allowed is None:
            return [r[0] for r in conn.execute(
                "SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts, 4.0, 2.0, 1.0) LIMIT ?", (fq, k))]
        return [r[0] for r in conn.execute(
            "SELECT fts.rowid FROM fts JOIN items i ON i.id = fts.rowid WHERE fts MATCH ?"
            " AND i.file_id IN (SELECT value FROM json_each(?)) ORDER BY bm25(fts, 4.0, 2.0, 1.0) LIMIT ?",
            (fq, json.dumps(sorted(allowed)), k))]
    except Exception:
        return []


EXACT_FILTER_FILES = 2000   # at or below this many allowed files, search their vectors exactly
VEC_KNN_MAX = 4096          # sqlite-vec's upper limit for k in one KNN query


def _vectors(conn, qv: np.ndarray, group: str, k: int, mode: str, oversample: int,
             allowed: set[int] | None = None) -> list[tuple[int, float]]:
    if allowed is not None and len(allowed) <= EXACT_FILTER_FILES:
        rows = conn.execute(
            "SELECT v.item_id, v.v FROM vectors v JOIN items i ON i.id = v.item_id WHERE v.modality = ?"
            " AND i.file_id IN (SELECT value FROM json_each(?))", (group, json.dumps(sorted(allowed)))).fetchall()
    elif mode == "exact":
        rows = conn.execute("SELECT item_id, v FROM vectors WHERE modality=?", (group,)).fetchall()
    else:
        want = min(k * oversample * (4 if allowed is not None else 1), VEC_KNN_MAX)
        cand = [r[0] for r in conn.execute(
            "SELECT item_id FROM vbin WHERE embedding MATCH vec_bit(?) AND k = ? AND modality = ?",
            (store.to_bits(qv), want, group))]
        if not cand:
            return []
        rows = []
        for i in range(0, len(cand), 900):
            part = cand[i:i + 900]
            q = ",".join("?" * len(part))
            if allowed is not None:
                rows += [(a, b) for a, b, fid in conn.execute(
                    f"SELECT v.item_id, v.v, i.file_id FROM vectors v JOIN items i ON i.id = v.item_id"
                    f" WHERE v.item_id IN ({q})", part) if fid in allowed]
            else:
                rows += conn.execute(f"SELECT item_id, v FROM vectors WHERE item_id IN ({q})", part).fetchall()
    if not rows:
        return []
    ids = np.array([r[0] for r in rows])
    M = np.stack([store.from_f16(r[1]) for r in rows])
    sims = M @ qv / (np.linalg.norm(M, axis=1) + 1e-9)
    order = np.argsort(-sims)[:k]
    return [(int(ids[i]), float(sims[i])) for i in order]


def bridge_terms(conn, qv: np.ndarray, q_terms: list[str], k: int = 12, min_sim: float = 0.62) -> list[tuple[str, float]]:
    try:
        rows = conn.execute(
            "SELECT v.term, d.distance FROM vvocab d JOIN vocab v ON v.id = d.id"
            " WHERE d.embedding MATCH ? AND k = ? ORDER BY d.distance", (np.asarray(qv, np.float32).tobytes(), k)).fetchall()
    except Exception:
        return []
    qf = [fold(t) for t in q_terms]
    out, stems = [], []
    for term, dist in rows:
        sim = 1 - dist
        if sim < min_sim:
            continue
        if any(term == t or term.startswith(t[:5]) and len(t) >= 5 or t.startswith(term) for t in qf):
            continue
        # one word form per stem: "lieferbestaetigung(ung)(en)" are the same bridge
        stem = term[:max(6, min(len(term) - 2, 12))]
        if any(stem.startswith(s) or s.startswith(stem) for s in stems):
            continue
        stems.append(stem)
        out.append((term, round(sim, 3)))
    return out[:5]


GENERIC_STEMS = {"summary", "readme", "index", "manifest", "status", "timeline", "notes", "todo", "claude", "agents",
                 "brain", "overview", "client", "changelog", "threads", "registry", "skill", "contents"}


def group_key(path: str, content_hash: str | None) -> str | None:
    """Files that are one document: identical bytes, or the same long, specific name in another format/place
    (X.eml, X.md, X.pdf + X.pdf.digest.md, backup copies)."""
    name = path.rsplit("/", 1)[-1]
    stem = re.sub(r"\.digest\.md$", "", name, flags=re.I)
    stem = re.sub(r"(\.[A-Za-z0-9]{1,5})+$", "", stem)
    s = re.sub(r"[^a-z0-9]+", "", fold(stem))
    if len(s) >= 18 and s not in GENERIC_STEMS:
        return "n:" + s
    if content_hash:
        return "h:" + content_hash
    return None


def _rep_rank(path: str) -> int:
    """Lower is a better representative: originals over digests, live folders over backups/raw mail copies."""
    p = path.lower()
    return (2 * ("backup" in p or "_superseded" in p or "_stubs" in p)
            + ("_original_emails" in p) + (p.endswith(".digest.md")) + p.endswith(".eml"))


def search(q: str, collections: list[str] | None = None, limit: int | None = None, mode: str = "hybrid",
           filters: dict | None = None, qvec: np.ndarray | None = None, opts: dict | None = None,
           exclude: set | None = None) -> dict:
    t_start = time.time()
    cfg = config.load()
    scfg = cfg["search"]
    opts = {"bridge": True, "collapse": True, **(opts or {})}
    if not scfg.get("rerank_enabled", False) and not (opts or {}).get("force_rerank"):
        opts["rerank"] = False
    limit = int(limit or scfg["limit"])
    text, parsed_filters, phrases = parse(q or "")
    filters = {**parsed_filters, **(filters or {})}
    # The scope shortcut ("w: invoice", "in:work") is applied by the callers (server, CLI, MCP: see
    # config.scope_shortcut), so the MCP lock checks it. An in: filter left here only counts when no scope is given.
    folders, names = folder_values(filters.get("in") or [])
    if names:
        collections = collections or names
    # scope: collection names, group names and "all", mixed; None/empty = All (lenient: unknown names are skipped,
    # the API layer rejects them before calling this). A group searches exactly its members.
    scope_info = config.resolve_scope(cfg, collections, strict=False)
    scope = [c for c in cfg["collections"] if c.get("enabled", True) and c["name"] in scope_info["collections"]]
    # in:~/Folder narrows the scope; it never widens it. Each collection gets the relative prefix(es) it is checked with.
    unders: dict[str, list[str]] = {}
    if folders:
        kept = []
        for c in scope:
            rel = folder_prefixes(folders, c["path"])
            if rel is None:
                continue
            kept.append(c)
            unders[c["name"]] = rel
        scope = kept
    terms = query_terms(text)
    lang = detect_lang(text) if text else None
    ok, filtered = file_filter(filters)
    oks = {}
    for c in scope:
        okc, fc = file_filter({**filters, "_under": list(filters.get("_under") or []) + [unders[c["name"]]]}) \
            if unders.get(c["name"]) else (ok, filtered)
        oks[c["name"]] = (okc, fc)
    filtered_any = any(fc for _, fc in oks.values())

    def ok_any(f) -> bool:                    # the feedback code does not know the collection: it tags the row
        okc, fc = oks.get(f.get("_collection") if isinstance(f, dict) else None, (ok, filtered))
        return okc(f) if fc else True
    want_vectors = mode in ("hybrid", "semantic") and (text or qvec is not None)
    timings = {}
    t = time.time()
    if want_vectors and qvec is None:
        try:
            qvec = embed_query(text)
        except Exception as e:
            qvec, timings["embed_error"] = None, str(e)
    timings["embed_ms"] = round((time.time() - t) * 1000)
    k = limit * 3          # filters are applied inside retrieval, so no 10x over-fetch is needed
    fused: dict[tuple[str, int], float] = defaultdict(float)
    why: dict[tuple[str, int], set] = defaultdict(set)
    sims: dict[tuple[str, int], float] = {}
    bridged: dict[str, list] = {}

    W = {**DEFAULT_WEIGHTS, **(scfg.get("weights") or {}), **(opts.get("weights") or {})}

    def add(coll, ranked, weight, label, bonus=0.0):
        weight = W.get(label, weight)
        for r, iid in enumerate(ranked):
            key = (coll, iid)
            fused[key] += weight / (RRF_K + r + 1) + (bonus if r == 0 else 0)
            why[key].add(label)

    t = time.time()
    for c in scope:
        try:
            conn = store.connect(c["name"], readonly=True)
        except FileNotFoundError:
            continue
        try:
            okc, filtered_c = oks[c["name"]]
            allowed = allowed_files(conn, okc) if filtered_c else None
            if allowed is not None and not allowed:
                continue
            if mode in ("hybrid", "keyword"):
                fq = fts_query(terms, phrases)
                if fq:
                    add(c["name"], _keyword(conn, fq, k, allowed), 1.0, "keyword", bonus=0.004)
            if qvec is not None:
                if (opts["bridge"] and cfg["translation"].get("enabled") and cfg["translation"].get("corpus_bridge")
                        and terms):
                    bt = bridge_terms(conn, qvec, terms)
                    if bt:
                        bridged[c["name"]] = bt
                        fq2 = fts_query([b for b, _ in bt])
                        add(c["name"], _keyword(conn, fq2, k, allowed), 0.6, "translated")
                vmode = opts.get("vector_mode") or scfg.get("vector_mode", "binary")
                for group, (w, label) in VECTOR_GROUPS.items():
                    hits = _vectors(conn, qvec, group, k, vmode, scfg.get("oversample", 16), allowed)
                    for iid, s in hits:
                        sims[(c["name"], iid)] = max(sims.get((c["name"], iid), -1), s)
                    add(c["name"], [i for i, _ in hits], w, label, bonus=0.004 if group == "text" else 0)
        finally:
            conn.close()
    timings["retrieve_ms"] = round((time.time() - t) * 1000)

    # ---- group by file
    t = time.time()
    by_coll: dict[str, list[int]] = defaultdict(list)
    for (coll, iid) in fused:
        by_coll[coll].append(iid)
    files: dict[tuple[str, int], dict] = {}
    all_terms = terms + [b for v in bridged.values() for b, _ in v]
    ccfg = {c["name"]: c for c in scope}
    for coll, iids in by_coll.items():
        conn = store.connect(coll, readonly=True)
        has_hash = any(r[1] == "hash" for r in conn.execute("PRAGMA table_info(files)"))
        try:
            for i in range(0, len(iids), 900):
                part = iids[i:i + 900]
                rows = conn.execute(
                    "SELECT i.id, i.file_id, i.modality, i.page, i.t0, i.t1, i.heading, i.text,"
                    " f.path, f.name, f.ext, f.kind, f.size, f.mtime, f.doc_date, f.title, f.pages, f.duration,"
                    f" f.thumb, f.mode, f.docid, f.lang, {'f.hash' if has_hash else 'NULL'} AS hash"
                    " FROM items i JOIN files f ON f.id = i.file_id"
                    f" WHERE i.id IN ({','.join('?' * len(part))})", part).fetchall()
                for r in rows:
                    okc, filtered_c = oks[coll]
                    if filtered_c and not okc(r):
                        continue
                    if exclude and (coll, r["path"]) in exclude:
                        continue
                    key = (coll, r["file_id"])
                    score = fused[(coll, r["id"])]
                    f = files.get(key)
                    if not f:
                        f = files[key] = {"coll": coll, "row": r, "items": []}
                    f["items"].append((score, r, why[(coll, r["id"])], sims.get((coll, r["id"]))))
        finally:
            conn.close()

    # one entry per file
    entries = []
    for (coll, fid), f in files.items():
        f["items"].sort(key=lambda x: -x[0])
        scores = [x[0] for x in f["items"]]
        f["score"] = scores[0] + 0.3 * sum(scores[1:2]) + 0.1 * sum(scores[2:3])
        if HUB_NAMES.search(f["row"]["path"]):
            f["score"] *= opts.get("hub_penalty", scfg.get("hub_penalty", 0.6))
        entries.append(f)

    # one entry per document: copies, other formats and digests fold into the best representative
    groups: dict = {}
    for f in entries:
        gk = group_key(f["row"]["path"], f["row"]["hash"]) if opts["collapse"] else None
        groups.setdefault((f["coll"], gk) if gk else (f["coll"], "f", f["row"]["file_id"]), []).append(f)
    results = []
    for members in groups.values():
        members.sort(key=lambda m: -m["score"])
        best = members[0]
        rep = min(members, key=lambda m: (_rep_rank(m["row"]["path"]), -m["score"]))
        if rep["score"] < 0.6 * best["score"]:
            rep = best
        gscore = best["score"] + 0.15 * sum(m["score"] for m in members[1:2])
        items = sorted((x for m in members for x in m["items"]), key=lambda x: -x[0])
        r, coll = rep["row"], rep["coll"]
        root = ccfg[coll]["path"]
        hits, seen_snips = [], set()
        for score, it, w, sim in items:
            h = {"modality": it["modality"], "page": it["page"], "t0": it["t0"], "t1": it["t1"],
                 "heading": it["heading"], "why": sorted(w), "similarity": round(sim, 3) if sim is not None else None}
            if it["path"] != r["path"]:
                h["from"] = it["path"]
            if it["modality"] in ("text", "ocr", "title", "transcript") and it["text"]:
                h["snippet"] = snippet(it["text"], all_terms)
                if h["snippet"] in seen_snips:
                    continue
                seen_snips.add(h["snippet"])
            hits.append(h)
            if len(hits) == 3:
                break
        why_all = sorted({w for x in items for w in x[2]})
        best_sim = max([x[3] for x in items if x[3] is not None], default=None)
        copies = [{"path": m["row"]["path"], "docid": m["row"]["docid"], "kind": m["row"]["kind"]}
                  for m in sorted(members, key=lambda m: _rep_rank(m["row"]["path"])) if m is not rep]
        results.append({
            "collection": coll, "path": r["path"], "abs_path": str(Path(root) / r["path"]), "name": r["name"],
            "ext": r["ext"], "kind": r["kind"], "title": r["title"], "docid": r["docid"], "date": r["doc_date"],
            "size": r["size"], "pages": r["pages"], "duration": r["duration"], "thumb": bool(r["thumb"]),
            "metadata_only": r["mode"] == "meta", "lang": r["lang"], "score": gscore,
            "similarity": round(best_sim, 3) if best_sim is not None else None,
            "why": why_all, "hits": hits, "copies": copies, "context": context_for(ccfg[coll], r["path"]),
            "_rr": _rerank_text(r, items),
        })
    results.sort(key=lambda x: -x["score"])
    results, fb = _apply_feedback(text, qvec, results, ccfg, ok_any if filtered_any else None, exclude, all_terms, opts)
    pin = fb.get("pinned", 0)      # favourites for this exact query stay on top: re-ranking only sees the rest
    if opts.get("rerank") and text and results[pin:]:
        t_r = time.time()
        try:
            from .rerank import rerank
            knobs = {**scfg, **{k: opts[k] for k in ("rerank_gate", "rerank_weight") if k in opts}}
            n = max(limit, 20)
            results = results[:pin] + rerank(text, results[pin:pin + n], knobs) + results[pin + n:]
            timings["rerank_ms"] = round((time.time() - t_r) * 1000)
        except Exception as e:
            timings["rerank_error"] = str(e)
    # facets over every reasonable match (not just the page shown): folder, type, year
    floor = (results[0]["score"] * 0.2) if results else 0
    matched = [x for x in results if x["score"] >= floor]
    facets = {
        "collection": dict(Counter(x["collection"] for x in matched).most_common()),
        "kind": dict(Counter(x["kind"] for x in matched).most_common()),
        "year": dict(sorted(Counter((x["date"] or "")[:4] for x in matched if x.get("date")).items(), reverse=True)),
    }
    total = len(matched)
    results = results[:limit]
    for x in results:
        if not opts.get("keep_rr"):      # finetune.py collects the exact text the re-ranker reads
            x.pop("_rr", None)
        x.pop("_blend", None)
        x.setdefault("favorite", None)
    top = results[0]["score"] if results else 1
    for x in results:
        x["relevance"] = round(100 * x["score"] / top)
        x["score"] = round(x["score"], 5)
    timings["group_ms"] = round((time.time() - t) * 1000)
    timings["total_ms"] = round((time.time() - t_start) * 1000)
    return {
        "query": q, "text": text, "lang": lang, "mode": mode,
        "filters": {k: v for k, v in filters.items() if not k.startswith("_")},     # (_under is internal)
        "collections": [c["name"] for c in scope],
        "scope": {"collections": [c["name"] for c in scope], "label": scope_info["label"]},
        "bridge": {c: [b for b, _ in v] for c, v in bridged.items()},
        "results": results, "timings": timings, "facets": facets, "total": total, "feedback": fb,
    }


def _apply_feedback(text, qvec, results, scope, ok, exclude, terms, opts) -> tuple[list[dict], dict]:
    """The user's favourites and hidden results (mse/feedback.py); opts {"feedback": False} switches them off.
    Never fails a search."""
    if not (text and opts.get("feedback", True)):
        return results, {"applied": 0}
    try:
        from . import feedback
        return feedback.apply(text, qvec, results, scope, ok, exclude, terms, collapse=opts.get("collapse", True))
    except Exception as e:
        for x in results:
            x["favorite"] = None
        return results, {"applied": 0, "error": f"{type(e).__name__}: {e}"}


def _rerank_text(row, items) -> str | None:
    """What the reranker reads: the file's name and its best-matching passages (plain text)."""
    texts, seen = [], set()
    for _, it, _, _ in items:
        t = (it["text"] or "").strip()
        if it["modality"] in ("text", "ocr", "transcript") and t and t[:80] not in seen:
            seen.add(t[:80])
            texts.append(t[:700])
        if len(texts) == 2:
            break
    head = f"{row['title'] or row['name']} ({row['path']})"
    return head + ("\n" + "\n…\n".join(texts) if texts else "")


def context_for(coll: dict, rel: str) -> str | None:
    parts = []
    if coll.get("description"):
        parts.append(coll["description"])
    best = None
    for prefix, ctx in (coll.get("contexts") or {}).items():
        if rel.lower().startswith(prefix.lower().strip("/")) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, ctx)
    if best:
        parts.append(best[1])
    return " ".join(parts) or None


# ------------------------------------------------------------------ documents

def find_file(coll_name: str | None, ref: str) -> tuple[dict, dict] | None:
    """Resolve a path (relative or absolute) or a #docid to (collection, file row)."""
    cfg = config.load()
    ref = ref.strip()
    cands = [config.get_collection(cfg, coll_name)] if coll_name else cfg["collections"]
    if ref.startswith("/"):
        c = config.collection_for_path(cfg, ref)
        if not c:
            return None
        cands, ref = [c], str(Path(ref).resolve().relative_to(c["path"]))
    for c in cands:
        if not c:
            continue
        try:
            conn = store.connect(c["name"], readonly=True)
        except FileNotFoundError:
            continue
        try:
            if ref.startswith("#") or re.fullmatch(r"[0-9a-f]{8}", ref):
                r = conn.execute("SELECT * FROM files WHERE docid=?", (ref.lstrip("#"),)).fetchone()
            else:
                r = conn.execute("SELECT * FROM files WHERE path=?", (ref,)).fetchone()
            if r:
                return c, dict(r)
        finally:
            conn.close()
    return None


def get_document(coll_name: str | None, ref: str, max_chars: int = 20000, page: int | None = None) -> dict | None:
    found = find_file(coll_name, ref)
    if not found:
        return None
    c, f = found
    out = {k: f[k] for k in ("path", "name", "kind", "ext", "size", "doc_date", "title", "pages", "duration", "docid",
                             "lang", "state", "error")}
    out.update(collection=c["name"], abs_path=str(Path(c["path"]) / f["path"]), context=context_for(c, f["path"]),
               metadata_only=f["mode"] == "meta")
    if f["mode"] == "meta":
        out["text"] = None
        out["note"] = "metadata-only by this collection's rules: contents are not indexed"
        return out
    conn = store.connect(c["name"], readonly=True)
    try:
        q = "SELECT modality, page, heading, text FROM items WHERE file_id=? AND modality IN ('text','ocr','transcript')"
        args = [f["id"]]
        if page:
            q += " AND page=?"
            args.append(page)
        rows = conn.execute(q + " ORDER BY ord", args).fetchall()
    finally:
        conn.close()
    parts, last_head = [], None
    for r in rows:
        head = r["heading"]
        if head and head != last_head:
            parts.append(f"\n## {head}\n")
            last_head = head
        parts.append(r["text"] or "")
    text = "\n".join(parts).strip()
    out["truncated"] = len(text) > max_chars
    out["text"] = text[:max_chars]
    return out


def list_files(coll_name: str, prefix: str = "", limit: int = 200) -> list[dict]:
    conn = store.connect(coll_name, readonly=True)
    try:
        rows = conn.execute("SELECT path, kind, size, doc_date, docid, mode FROM files WHERE path LIKE ? ESCAPE '\\'"
                            " ORDER BY path LIMIT ?", (prefix.replace("%", "\\%").replace("_", "\\_") + "%", limit))
        return [dict(r) for r in rows]
    finally:
        conn.close()


def similar(coll_name: str | None, ref: str, collections: list[str] | None = None, limit: int = 20) -> dict | None:
    """Documents nearest to a given file: its picture for images/scans, the mean of its text otherwise.
    `collections` (names, groups, "all") is where to look; default: the file's own collection."""
    found = None
    if coll_name is None and collections:               # the file itself: first inside the scope, then anywhere
        for name in config.resolve_scope(config.load(), collections, strict=False)["collections"]:
            if (found := find_file(name, ref)):
                break
    found = found or find_file(coll_name, ref)
    if not found:
        return None
    c, f = found
    conn = store.connect(c["name"], readonly=True)
    try:
        rows = conn.execute("SELECT v.modality, v.v FROM vectors v JOIN items i ON i.id = v.item_id"
                            " WHERE i.file_id = ? ORDER BY i.ord", (f["id"],)).fetchall()
    finally:
        conn.close()
    by = defaultdict(list)
    for mod, v in rows:
        by[mod].append(store.from_f16(v))
    pick = ("visual", "text", "title") if f["kind"] in ("image", "video") else ("text", "visual", "audio", "title")
    vecs = next((by[m] for m in pick if by.get(m)), None)
    if not vecs:
        return {"similar_to": f["path"], "results": [], "note": "this file has no embeddings yet"}
    qv = np.mean(vecs[:12], axis=0)
    qv = qv / (np.linalg.norm(qv) + 1e-9)
    res = search("", collections=collections or [c["name"]], mode="semantic", qvec=qv, limit=limit + 5,
                 exclude={(c["name"], f["path"])})
    gk = group_key(f["path"], f.get("hash"))
    res["results"] = [r for r in res["results"]
                      if not (gk and r["collection"] == c["name"] and group_key(r["path"], None) == gk)][:limit]
    res.update(similar_to=f["path"], collection=c["name"], query=f"similar to {f['name']}")
    return res
