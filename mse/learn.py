"""Learning from use: after every night sync, measure search quality, re-tune the ranking, apply it only if safe.

  1. Known-item questions are built from the index (DATA/auto_gold.json): for up to 150 recently indexed files
     per collection, the cleaned title / first heading / file name becomes a question whose answer is that file,
     so the evaluation grows with the corpus.
  2. The current settings are scored on four sets: every gold set (evaluate.discover_gold_files), the favourites
     (feedback application off) and the known-item questions. One line per run goes to DATA/quality.jsonl.
  3. The tune grid runs (evaluate.tune: fusion weights, hub penalty, re-ranker gate and weight).
  4. The best setting is applied only when evaluate.guardrails() has no objection; otherwise the current settings
     stay and the line records why. Applied lines keep the previous settings: `mse tune --rollback` restores them.
"""

import json
import re
import time
import unicodedata
from pathlib import Path

from . import config, evaluate, paths, store
from .textutil import STOP, fold

AUTO_PER_COLLECTION = 150
MAX_WORDS = 8
MAX_DOCS_PER_QUESTION = 2      # a question several documents answer equally is ambiguous, not a known item
GENERIC_WORDS = {"page", "seite", "digest", "copy", "kopie", "untitled", "unbenannt", "scan", "img", "image", "file",
                 "datei", "document", "dokument", "doc", "new", "neu", "final", "draft", "entwurf", "version", "en",
                 "de", "md", "pdf", "eml", "txt", "html", "pre", "cleanup"}
_EXT = re.compile(r"\.(pdf|md|eml|txt|docx?|xlsx?|pptx?|odt|rtf|html?|csv|json|jpe?g|png|heic|m4a|mp3|mp4|mov|wav)\b", re.I)
_DATE = re.compile(r"^(?:(?:19|20)\d\d(?:[-_.]\d{1,2}){0,2}|\d{1,2}\.\d{1,2}\.\d{2,4}|Q[1-4]|KW\d{1,2}|W\d{1,2})$", re.I)


def auto_gold_path() -> Path:
    return paths.DATA / "auto_gold.json"


def quality_path() -> Path:
    return paths.DATA / "quality.jsonl"


# ------------------------------------------------------------------ 1. known-item questions

def clean_query(s: str, from_name: bool = False) -> str:
    """A title or file name as a question: no extensions, dates, IDs, amounts, hashes or filler words."""
    s = _EXT.sub(" ", unicodedata.normalize("NFC", s or ""))
    if from_name:
        s = re.sub(r"([a-zäöüß])([A-ZÄÖÜ])", r"\1 \2", s)          # camelCase in file names
    s = re.sub(r"[_—–›|/\\()\[\]{}:;,\"“”„+=#]+", " ", s)
    out = []
    for tok in re.findall(r"[^\W_][\w.&'’-]*", s):
        tok = tok.strip(".-'’&")
        if not tok or _DATE.match(tok):
            continue
        digits = sum(ch.isdigit() for ch in tok)
        if digits >= 3 or (digits and 2 * digits >= len(tok)) or re.fullmatch(r"[0-9a-f]{8,}", tok, re.I):
            continue                                                   # IDs, invoice numbers, amounts, hashes
        if fold(tok) in GENERIC_WORDS:
            continue
        out.append(tok)
    return " ".join(out[:MAX_WORDS])


def meaningful_words(q: str) -> list[str]:
    return [w for w in q.split() if len(w) >= 3 and w.lower() not in STOP and any(ch.isalpha() for ch in w)]


def _doc_key(path: str) -> str:
    from .search import group_key
    return group_key(path, None) or "p:" + path


def build_auto_gold(collections: list[str] | None = None, per_collection: int = AUTO_PER_COLLECTION,
                    save: bool = True) -> dict:
    """Known-item questions from the most recently indexed (and embedded) files of every enabled collection."""
    from .search import GENERIC_STEMS, HUB_NAMES, _rep_rank
    cfg = config.load()
    names = collections or [c["name"] for c in cfg["collections"] if c.get("enabled", True)]
    queries, per = [], {}
    for name in names:
        try:
            conn = store.connect(name, readonly=True)
        except FileNotFoundError:
            continue
        try:
            files = conn.execute(
                "SELECT f.id, f.path, f.name, f.title, f.indexed_at FROM files f WHERE f.mode != 'meta'"
                " AND COALESCE(f.state, '') != 'error'"
                " AND EXISTS (SELECT 1 FROM items i WHERE i.file_id = f.id AND i.embedded = 1)"
                " ORDER BY f.indexed_at DESC, f.id DESC").fetchall()
            # first heading of each file (SQLite takes the bare column from the MIN(ord) row)
            heads = {r[0]: r[1] for r in conn.execute(
                "SELECT file_id, heading, MIN(ord) FROM items WHERE heading IS NOT NULL AND heading != ''"
                " GROUP BY file_id")}
        finally:
            conn.close()
        cands: dict[int, list[tuple[str, str]]] = {}
        docs: dict[str, set] = {}            # question -> documents it could mean
        holders: dict[str, list] = {}        # question -> files that produced it
        for f in files:
            stem = re.sub(r"\.digest\.md$", "", f["name"], flags=re.I)
            stem = re.sub(r"(\.[A-Za-z0-9]{1,5})+$", "", stem)
            if HUB_NAMES.search(f["path"]) or re.sub(r"[^a-z0-9]+", "", fold(stem)) in GENERIC_STEMS:
                continue
            head = (heads.get(f["id"]) or "").split(" › ")[0]
            if re.fullmatch(r"(page|seite|slide|folie)\s*\d+", head.strip(), re.I):
                head = ""
            opts = []
            for src, text, from_name in (("title", f["title"], False), ("heading", head, False), ("name", stem, True)):
                q = clean_query(text or "", from_name)
                if len(meaningful_words(q)) >= 2 and q.lower() not in (o.lower() for _, o in opts):
                    opts.append((src, q))
            cands[f["id"]] = opts
            for _, q in opts:
                k = fold(q)
                docs.setdefault(k, set()).add(_doc_key(f["path"]))
                holders.setdefault(k, []).append(f["path"])
        seen, n = set(), 0
        for f in files:
            if n >= per_collection:
                break
            if _rep_rank(f["path"]) >= 2:    # backups and superseded copies are answers, not question sources
                continue
            pick = next(((src, q) for src, q in cands.get(f["id"], []) if len(docs[fold(q)]) <= MAX_DOCS_PER_QUESTION),
                        None)
            if not pick or fold(pick[1]) in seen:
                continue
            seen.add(fold(pick[1]))
            queries.append({"q": pick[1], "expect": sorted(set(holders[fold(pick[1])])), "collection": name,
                            "source": pick[0], "file": f["path"]})
            n += 1
        per[name] = n
    out = {"built": time.strftime("%Y-%m-%dT%H:%M:%S"), "per_collection": per, "queries": queries}
    if save:
        tmp = auto_gold_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
        tmp.replace(auto_gold_path())
    return out


def load_auto_gold() -> list[dict]:
    try:
        return json.loads(auto_gold_path().read_text())["queries"]
    except (OSError, ValueError, KeyError):
        return []


# ------------------------------------------------------------------ 2. quality log

def append(entry: dict) -> None:
    paths.ensure_dirs()
    with open(quality_path(), "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def history(n: int | None = None) -> list[dict]:
    try:
        lines = quality_path().read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out[-n:] if n else out


def _entry(kind: str, t: dict, started: float) -> dict:
    base, cand = t["base"], t["cand"]
    return {
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind,
        "sets": evaluate.summary(base), "n": base["n"], "weighted_mrr": round(base["weighted_mrr"], 4),
        "settings": t["current"], "rerank": t["rerank"],
        "tune": {"evaluated": t["evaluated"], "candidate": t["candidate"], "changed": t["changed"],
                 "sets": evaluate.summary(cand), "weighted_mrr": round(cand["weighted_mrr"], 4)},
        "duration_s": round(time.time() - started),
    }


def _apply(settings: dict) -> None:
    config.update_settings({"search": evaluate.config_patch(settings)})


# ------------------------------------------------------------------ 3./4. the nightly run

def run(progress=None, cancel=None, apply: bool = True, collections: list[str] | None = None,
        threads: int = evaluate.THREADS) -> dict:
    """Build the known-item set, score the current settings, tune, apply when the guardrails allow it."""
    started = time.time()
    if progress:
        progress("known-item questions", 0, 0, "")
    auto = build_auto_gold(collections)
    sets = evaluate.load_sets(collections)
    t = evaluate.tune(sets, progress=progress, cancel=cancel, threads=threads)
    entry = _entry("learn", t, started)
    entry["auto_gold"] = {"built": auto["built"], "questions": len(auto["queries"])}
    if t["reasons"]:
        entry.update(action="kept", reason="; ".join(t["reasons"]))
    elif not apply:
        entry.update(action="dry-run", reason="would apply: all guardrails pass")
    else:
        _apply(t["candidate"])
        entry.update(action="applied", reason="all guardrails pass", previous=t["current"], applied=t["candidate"])
    append(entry)
    return {"action": entry["action"], "reason": entry["reason"], "questions": entry["n"],
            "weighted_mrr": entry["weighted_mrr"], "candidate_weighted_mrr": entry["tune"]["weighted_mrr"],
            "evaluated": t["evaluated"], "duration_s": entry["duration_s"]}


def record_manual(t: dict) -> dict:
    """`mse tune --apply`: apply the tuned setting by hand (guardrail objections are recorded, not enforced)."""
    entry = _entry("tune", t, time.time())
    _apply(t["candidate"])
    entry.update(action="applied", reason="by hand (mse tune --apply)" + (
        "; guardrails objected: " + "; ".join(t["reasons"]) if t["reasons"] else ""),
        previous=t["current"], applied=t["candidate"])
    append(entry)
    return entry


def rollback() -> dict:
    """Undo the most recent applied change that has not been undone yet (repeat to go further back)."""
    skip, target = 0, None
    for e in reversed(history()):
        if e.get("action") == "rolled back":
            skip += 1
        elif e.get("action") == "applied" and e.get("previous"):
            if skip:
                skip -= 1
                continue
            target = e
            break
    if not target:
        raise ValueError("nothing to roll back: quality.jsonl has no applied change left")
    cur = evaluate.current_settings()
    _apply(target["previous"])
    entry = {"when": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": "rollback", "action": "rolled back",
             "reason": f"undoes the change of {target['when']}", "settings": target["previous"], "previous": cur,
             "undoes": target["when"]}
    append(entry)
    return entry
