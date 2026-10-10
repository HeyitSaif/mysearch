"""Second-stage ordering with a cross-encoder (Qwen3-Reranker-0.6B), used as a tie-breaker for close calls.

What was measured on the 28-question gold set (`mse eval ... --configs hybrid,rerank`) and shaped this:
- The reranker's probabilities saturate: most plausible files score 0.95-0.999, so mixing the raw probability
  into the retrieval score mostly adds noise (that blend was *worse* than plain hybrid: MRR 0.878 vs 0.896).
  Its order carries the signal: sorted by the reranker alone, the right file comes first about as often as
  with hybrid search, but on different queries. So the two orders are fused by rank, the reranker winning ties.
- When the runner-up scores below GATE x the leader, hybrid search was right on every gold query; the model is
  not called then (about half of all queries), which also halves the average cost.
- Only the first TOP candidates are read; the rest keep their order.
"""

import threading
from collections import OrderedDict

from . import config
from .embedder import pool

TOP = 10            # candidates the cross-encoder reads
GATE = 0.8          # re-rank only when results[1].score >= GATE * results[0].score   (config search.rerank_gate)
K = 10              # rank-fusion smoothing
W_RETRIEVAL = 0.75  # weight of the retrieval rank against the reranker rank (1.0): the reranker breaks ties
                    # (config search.rerank_weight; both are tuned by `mse tune` / the nightly learn job)

_cache: OrderedDict[tuple[str, str, str], float] = OrderedDict()   # (adapter, query, document text) -> score
_lock = threading.Lock()
CACHE_SIZE = 2048


def adapter_id() -> str:
    """The re-ranker adapter in effect (finetune.py's CURRENT pointer; MSE_RERANK_ADAPTER overrides it), "base"
    without one. Part of every cache key, so a promotion or rollback never serves the other model's scores."""
    import os
    from .finetune import active_id
    return os.environ.get("MSE_RERANK_ADAPTER") or active_id() or "base"


def _fresh_worker() -> None:
    """Restart the re-rank worker when CURRENT moved after it loaded (a promotion or rollback by the night job or
    from the CLI in another process): the next call starts it with the adapter now in effect."""
    from .finetune import registry_dir
    w = pool().workers["rerank"]
    if not w.alive:
        return
    try:
        moved = (registry_dir() / "CURRENT").stat().st_mtime
    except OSError:
        return
    if moved > w.started_at:
        pool().stop("rerank")


def reset() -> None:
    """After the active adapter changed: stop the worker (it reloads on the next call) and forget all scores."""
    pool().stop("rerank")
    with _lock:
        _cache.clear()


def _scores(query: str, docs: list[str]) -> list[float]:
    """Reranker scores, from memory where this query has already read this text with this adapter (paging,
    filter changes)."""
    _fresh_worker()
    ad = adapter_id()
    with _lock:
        have = {d: _cache[(ad, query, d)] for d in docs if (ad, query, d) in _cache}
    todo = list(dict.fromkeys(d for d in docs if d not in have))
    if todo:
        got = dict(zip(todo, pool().rerank(query, todo)))
        have.update(got)
        with _lock:
            for d, s in got.items():
                _cache[(ad, query, d)] = s
            while len(_cache) > CACHE_SIZE:
                _cache.popitem(last=False)
    return [have[d] for d in docs]


def rerank(query: str, results: list[dict], settings: dict | None = None) -> list[dict]:
    """`settings`: the search settings to read GATE / W_RETRIEVAL from (default: config.json's "search")."""
    knobs = settings if settings is not None else config.load()["search"]
    gate, w_retrieval = float(knobs.get("rerank_gate", GATE)), float(knobs.get("rerank_weight", W_RETRIEVAL))
    for r in results:
        r["rerank"] = None
    n = min(TOP, len(results))
    if n < 2 or results[1]["score"] < gate * (results[0]["score"] or 1.0):
        return results                                   # retrieval is decisive: keep its order
    idx = [i for i in range(n) if results[i].get("_rr")]
    if len(idx) < 2:
        return results
    s = dict(zip(idx, _scores(query, [results[i]["_rr"] for i in idx])))
    rr_rank = {i: r for r, i in enumerate(sorted(idx, key=lambda i: -s[i]))}
    fused = {i: w_retrieval / (K + i) + 1 / (K + rr_rank.get(i, i)) for i in range(n)}
    order = sorted(range(n), key=lambda i: -fused[i])
    scores = [results[i]["score"] for i in range(n)]     # already descending
    head = []
    for pos, i in enumerate(order):
        r = results[i]
        r["rerank"] = round(s[i], 3) if i in s else None
        r["score"] = scores[pos]                         # keep scores monotone so relevance stays 0-100
        head.append(r)
    return head + results[n:]
