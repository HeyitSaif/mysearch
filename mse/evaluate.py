"""Retrieval evaluation on gold sets (hit@1/3/10, MRR@10) and tuning of the ranking settings.

    mse eval eval/gold_example.json -c work --configs hybrid,keyword,semantic,no-bridge,rerank
    mse eval --feedback                    # favourites as questions, with feedback application OFF
    mse tune [-c work] [--apply | --rollback]

Evaluation always runs with feedback application switched off ({"feedback": False}), so favourites measure
the engine instead of grading themselves; the "favourites" config switches it back on for comparison.
"""

import itertools
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, paths, search, store

CONFIGS = {
    "hybrid": {"mode": "hybrid"},
    "keyword": {"mode": "keyword"},
    "semantic": {"mode": "semantic"},
    "no-bridge": {"mode": "hybrid", "opts": {"bridge": False}},
    "no-collapse": {"mode": "hybrid", "opts": {"collapse": False}},
    "rerank": {"mode": "hybrid", "opts": {"rerank": True, "force_rerank": True}},
    "exact-vectors": {"mode": "hybrid", "opts": {"vector_mode": "exact"}},
    "favourites": {"mode": "hybrid", "opts": {"feedback": True}},
}


def discover_gold_files() -> list[Path]:
    """The hold-out gold sets: every *.json in the data folder's eval/ (the user's own sets), then every *.json in
    the repository's eval/ except gold_example.json. A set is named by its file stem. When a stem exists in both
    places the data folder's file is used and the other is skipped, so no set is counted twice. A missing folder
    contributes nothing. Called at each use, so a new file is picked up without a restart of any code path."""
    out, seen = [], set()
    for folder in (paths.DATA / "eval", paths.PROJECT / "eval"):
        try:
            found = sorted(folder.glob("*.json"))
        except OSError:
            continue
        for f in found:
            if not f.is_file() or f.stem in seen:
                continue
            if folder == paths.PROJECT / "eval" and f.name == "gold_example.json":
                continue
            seen.add(f.stem)
            out.append(f)
    return out


FUSION = ("keyword", "translated", "name")                 # search.DEFAULT_WEIGHTS keys that are tuned
KNOBS = FUSION + ("hub_penalty", "rerank_gate", "rerank_weight")
HUB_PENALTY = 0.6          # search.py's default when config.json has none
AUTO_WEIGHT = 0.5          # an automatic known-item question counts half a hand-written or favourite one
MIN_GAIN = 0.01            # guardrails (see guardrails())
MAX_GOLD_LOSS = 0.01
THREADS = 4


def rank_of(results: list[dict], expect: list[str]) -> int | None:
    exp = [e.lower() for e in expect]
    for i, r in enumerate(results, 1):
        paths_ = [r["path"]] + [c["path"] for c in r.get("copies", [])] + [c["path"] for c in r.get("companions", [])]
        if any(e in p.lower() for p in paths_ for e in exp):
            return i
    return None


def run(gold, collections: list[str] | None, configs: list[str], verbose: bool = False) -> dict:
    """`gold`: a gold file or a list of {"q", "expect"[, "collection"]}. A question that names its collection is
    searched there unless `collections` is given."""
    gold = json.loads(Path(gold).read_text())["queries"] if isinstance(gold, (str, Path)) else gold
    out = {}
    for name in configs:
        cfg = CONFIGS[name]
        ranks, misses, rr_ms, rr_used, t = [], [], [], 0, time.time()
        for g in gold:
            scope = collections or ([g["collection"]] if g.get("collection") else None)
            res = search.search(g["q"], collections=scope, limit=10, mode=cfg["mode"],
                                opts={"feedback": False, **(cfg.get("opts") or {})})
            r = rank_of(res["results"], g["expect"])
            ranks.append(r)
            if "rerank_ms" in res["timings"]:
                rr_ms.append(res["timings"]["rerank_ms"])
                rr_used += any(x.get("rerank") is not None for x in res["results"])
            if r is None or r > 3:
                misses.append((g["q"], r, [x["path"].rsplit("/", 1)[-1][:60] for x in res["results"][:3]]))
        n = len(ranks) or 1
        out[name] = {
            "hit@1": round(sum(1 for r in ranks if r == 1) / n, 3),
            "hit@3": round(sum(1 for r in ranks if r and r <= 3) / n, 3),
            "hit@10": round(sum(1 for r in ranks if r) / n, 3),
            "mrr@10": round(sum(1 / r for r in ranks if r) / n, 3),
            "ms_per_query": round((time.time() - t) * 1000 / n),
            "misses": misses if verbose else len(misses),
        }
        if rr_ms:   # time inside the reranker per query (the first call includes loading the model)
            out[name].update(rerank_ms=round(sum(rr_ms) / n), rerank_ms_max=max(rr_ms), reranked=rr_used)
    return out


# ------------------------------------------------------------------ settings

def current_settings(cfg: dict | None = None) -> dict:
    from . import rerank
    s = (cfg or config.load())["search"]
    w = {**search.DEFAULT_WEIGHTS, **(s.get("weights") or {})}
    return {**{k: w[k] for k in FUSION}, "hub_penalty": s.get("hub_penalty", HUB_PENALTY),
            "rerank_gate": s.get("rerank_gate", rerank.GATE), "rerank_weight": s.get("rerank_weight", rerank.W_RETRIEVAL)}


def config_patch(settings: dict) -> dict:
    """The config.json "search" fragment for a settings dict."""
    return {"weights": {k: settings[k] for k in FUSION}, "hub_penalty": settings["hub_penalty"],
            "rerank_gate": settings["rerank_gate"], "rerank_weight": settings["rerank_weight"]}


def search_opts(settings: dict, rerank: bool) -> dict:
    return {"weights": {k: settings[k] for k in FUSION}, "hub_penalty": settings["hub_penalty"],
            "rerank_gate": settings["rerank_gate"], "rerank_weight": settings["rerank_weight"],
            "feedback": False, "rerank": rerank, "force_rerank": rerank}


# ------------------------------------------------------------------ question sets

def _scope_paths(scope: list[str] | None, cache: dict) -> list[str]:
    cfg = config.load()
    names = scope or [c["name"] for c in cfg["collections"] if c.get("enabled", True) and c.get("in_global", True)]
    out = []
    for n in names:
        if n not in cache:
            try:
                conn = store.connect(n, readonly=True)
                try:
                    cache[n] = [r[0].lower() for r in conn.execute("SELECT path FROM files")]
                finally:
                    conn.close()
            except FileNotFoundError:
                cache[n] = []
        out += cache[n]
    return out


def load_sets(collections: list[str] | None = None, gold_files=None, favourites: bool = True,
              auto: bool = True) -> dict:
    """name -> {"kind": gold|favourites|auto, "queries": [(question, scope)], "skipped": n}. Questions whose
    expected file is not indexed in the searched collections cannot be answered there and are skipped."""
    from . import feedback, learn
    raw = [(Path(f).stem, "gold", json.loads(Path(f).read_text())["queries"])
           for f in (gold_files or discover_gold_files()) if Path(f).exists()]
    if favourites:
        raw.append(("favourites", "favourites", feedback.gold()))
    if auto:
        raw.append(("auto", "auto", learn.load_auto_gold()))
    cache, sets = {}, {}
    for name, kind, queries in raw:
        s = sets[name] = {"kind": kind, "queries": [], "skipped": 0}
        for g in queries:
            scope = collections or ([g["collection"]] if g.get("collection") else None)
            exp = [e.lower() for e in g["expect"]]
            if any(e in p for p in _scope_paths(scope, cache) for e in exp):
                s["queries"].append((g, scope))
            else:
                s["skipped"] += 1
    return sets


# ------------------------------------------------------------------ scoring

def _metrics(ranks: list) -> dict:
    n = len(ranks)
    if not n:
        return {"n": 0, "hit@1": 0.0, "hit@3": 0.0, "hit@10": 0.0, "mrr": 0.0}
    return {"n": n, "hit@1": sum(r == 1 for r in ranks) / n, "hit@3": sum(bool(r) and r <= 3 for r in ranks) / n,
            "hit@10": sum(bool(r) for r in ranks) / n, "mrr": sum(1 / r for r in ranks if r) / n}


def score(sets: dict, settings: dict, rerank: bool = False, threads: int = THREADS) -> dict:
    """Every question of every set searched with `settings`: per-set metrics, per-question ranks, weighted MRR."""
    opts = search_opts(settings, rerank)
    jobs = [(name, g, scope) for name, s in sets.items() for g, scope in s["queries"]]

    def one(job):
        _, g, scope = job
        res = search.search(g["q"], collections=scope, limit=10, mode="hybrid", opts=dict(opts))
        return rank_of(res["results"], g["expect"])

    with ThreadPoolExecutor(max(1, threads)) as ex:
        ranks = list(ex.map(one, jobs))
    out, num, den = {}, 0.0, 0.0
    for name, s in sets.items():
        rs = [r for (n, _, _), r in zip(jobs, ranks) if n == name]
        out[name] = {**_metrics(rs), "kind": s["kind"], "skipped": s.get("skipped", 0), "ranks": rs,
                     "questions": [g["q"] for g, _ in s["queries"]]}
        w = AUTO_WEIGHT if s["kind"] == "auto" else 1.0
        num += w * sum(1 / r for r in rs if r)
        den += w * len(rs)
    hit1 = sum((AUTO_WEIGHT if sets[n]["kind"] == "auto" else 1.0) * (r == 1) for (n, _, _), r in zip(jobs, ranks))
    return {"sets": out, "n": len(jobs), "weighted_mrr": num / den if den else 0.0,
            "weighted_hit1": hit1 / den if den else 0.0, "settings": dict(settings), "rerank": rerank}


def summary(scored: dict) -> dict:
    """What goes into quality.jsonl: rounded per-set metrics, no per-question detail."""
    return {name: {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items() if k not in ("ranks", "questions")}
            for name, m in scored["sets"].items()}


# ------------------------------------------------------------------ guardrails

def safety(base: dict, cand: dict) -> list[str]:
    """Regressions that rule a candidate out, whatever it gains elsewhere."""
    out = []
    for name, b in base["sets"].items():
        c = cand["sets"].get(name)
        if not c or not b["n"]:
            continue
        if b["kind"] == "gold":
            if c["mrr"] < b["mrr"] - MAX_GOLD_LOSS - 1e-9:
                out.append(f"{name} MRR {c['mrr'] - b['mrr']:+.3f} (may lose at most {MAX_GOLD_LOSS})")
            lost = [q for q, rb, rc in zip(b["questions"], b["ranks"], c["ranks"]) if rb == 1 and rc != 1]
            if lost:
                out.append(f"{name} loses the right file at rank 1 on {len(lost)} question(s), e.g. \"{lost[0]}\"")
        elif b["kind"] == "favourites" and c["mrr"] < b["mrr"] - 1e-9:
            out.append(f"favourites MRR {c['mrr'] - b['mrr']:+.3f} (must not get worse)")
    return out


def guardrails(base: dict, cand: dict) -> list[str]:
    """Why `cand` must not replace `base` (empty: it may). All must hold:
    weighted MRR over all sets +MIN_GAIN or more; no hand-written gold set loses more than MAX_GOLD_LOSS MRR
    or the rank-1 hit of any of its questions; the favourites set does not get worse."""
    out = []
    gain = cand["weighted_mrr"] - base["weighted_mrr"]
    if gain < MIN_GAIN - 1e-9:
        out.append(f"weighted MRR {gain:+.3f} (needs at least +{MIN_GAIN})")
    return out + safety(base, cand)


# ------------------------------------------------------------------ tuning

def _levels(v: float, step: float, lo: float, hi: float) -> list[float]:
    return sorted({round(min(hi, max(lo, v + d)), 3) for d in (-step, 0.0, step)})


def _dist(a: dict, b: dict) -> float:
    return sum(abs(a[k] - b[k]) for k in KNOBS)


def _pick(ref: dict, scored: list[dict], cur: dict) -> dict:
    """Best of `scored` that passes the safety checks against `ref`; ties go to the setting closest to `cur`."""
    ok = [s for s in scored if not safety(ref, s)] or [ref]
    return max(ok, key=lambda s: (round(s["weighted_mrr"], 9), round(s["weighted_hit1"], 9), -_dist(s["settings"], cur)))


def tune(sets: dict, progress=None, cancel=None, threads: int = THREADS) -> dict:
    """Staged grid search, at most 39 settings:
      1. keyword x translated x name weights, 3 levels each (current ±0.2 / ±0.15), re-ranker off   27
      2. hub_penalty at current ±0.15 with the weights picked in 1                                    2
      3. only when the re-ranker is enabled: rerank_gate (±0.1) x rerank_weight (±0.25), re-ranker on 9
    Each stage keeps the best setting that does not regress a gold set or the favourites. The pick is then
    compared with the current settings as people get them today (re-ranker on when enabled)."""
    cur = current_settings()
    rerank_on = bool(config.load()["search"].get("rerank_enabled", False))
    memo: dict = {}
    total = 1 + 26 + 2 + (1 + 8 if rerank_on else 0)     # upper bound; shared points are evaluated once

    def ev(settings: dict, rr: bool) -> dict:
        key = (tuple(round(settings[k], 3) for k in KNOBS), rr)
        if key not in memo:
            if cancel and cancel():
                from .indexer import Cancelled
                raise Cancelled()
            memo[key] = score(sets, settings, rr, threads)
            if progress:
                progress("tune", len(memo), total, f"{len(memo)}/{total} settings")
        return memo[key]

    base = ev(cur, rerank_on)
    ref = ev(cur, False)
    grid1 = [{**cur, "keyword": a, "translated": b, "name": c}
             for a, b, c in itertools.product(_levels(cur["keyword"], 0.2, 0.05, 2.0),
                                              _levels(cur["translated"], 0.2, 0.05, 2.0),
                                              _levels(cur["name"], 0.15, 0.05, 2.0))]
    pick = _pick(ref, [ev(s, False) for s in grid1], cur)["settings"]
    grid2 = [{**pick, "hub_penalty": h} for h in _levels(cur["hub_penalty"], 0.15, 0.2, 1.0)]
    pick = _pick(ref, [ev(s, False) for s in grid2], cur)["settings"]
    if rerank_on:
        grid3 = [{**pick, "rerank_gate": g, "rerank_weight": w}
                 for g, w in itertools.product(_levels(cur["rerank_gate"], 0.1, 0.5, 1.0),
                                               _levels(cur["rerank_weight"], 0.25, 0.25, 2.0))]
        pick = _pick(base, [ev(s, True) for s in grid3], cur)["settings"]
    cand = ev(pick, rerank_on)
    same = _dist(pick, cur) < 1e-9
    reasons = ["the current settings are already the best on the grid"] if same else guardrails(base, cand)
    table = sorted(memo.values(), key=lambda s: -s["weighted_mrr"])
    for s in table:
        s["safe"] = not safety(base if s["rerank"] == rerank_on else ref, s)
    return {"current": cur, "candidate": pick, "base": base, "cand": cand, "changed": not same, "reasons": reasons,
            "evaluated": len(memo), "rerank": rerank_on, "table": table}
