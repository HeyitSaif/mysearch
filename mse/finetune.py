"""Learning the re-ranker from the user's favourites: a small LoRA fine-tune of Qwen3-Reranker-0.6B on this Mac.

    python -m mse.finetune status | collect | train | eval [ID] | promote [ID] | rollback [ID] | run
                           [--dry-run] [--min N] [--min-queries N] [--force] [--json]

  collect   Every favourite ('best' in feedback.db) is searched again with feedback application and re-ranking off.
            The favourite is the positive; the hard negatives are the documents retrieval ranked above it, then those
            just below it (files hidden for that query first). Each document is represented by exactly the text the
            re-ranker reads (search.py `_rr`, kept with opts keep_rr), with the query text it receives.
            Optional: learn.py's known-item questions at a lower weight. Every example carries its collection.
            Queries are split 80/20 by a stable hash of the normalised query, so a query is never on both sides;
            a query that equals a question of any gold set (evaluate.discover_gold_files) is never used: the
            gold sets are the hold-out.
  train     QLoRA through mlx_lm.tuner on the 8-bit model: rank-8 adapters on the attention q/v projections of the
            last 16 layers. Loss: listwise softmax over (1 positive + k negatives) of the yes-minus-no logit the
            worker scores with. Low priority, on AC power only, within a time and memory budget.
  eval      Base vs candidate through mse.evaluate (the live "rerank" configuration: gate, weights) on the held-out
            favourites and on every gold set (evaluate.discover_gold_files), plus the re-rank latency of both models.
            Promotion: favourites MRR +0.02 or more AND no gold set loses more than 0.01 MRR AND latency +10% at
            most (all three are settings).
  promote / rollback   switch the CURRENT pointer only; adapters are kept forever.
  run       the nightly sequence: gate -> collect -> train -> eval -> promote when allowed.

Registry: <data>/models/reranker-lora/
    CURRENT              the active adapter id (absent or "none": the base model); read by worker.Reranker
    history.jsonl        every run, training, evaluation and pointer move (append-only)
    <YYYY-MM-DD_HHMM>/   adapters.safetensors + adapter_config.json (mlx_lm format), training.json, split.json and
                         heldout.json (queries and paths only, never document text), metrics.json
Working data: <data>/finetune/ (examples.jsonl with the passages, collect.json; rebuilt by every collect).

status(), counts() and gate() are cheap and safe inside the daemon. collect() needs the embedding worker; train()
and evaluate_adapter() load MLX, so the daemon runs them as a subprocess: `python -m mse.finetune run --json`.
"""

import argparse
import hashlib
import json
import math
import os
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import config, paths

LORA_DIR = "reranker-lora"          # = worker.Reranker.ADAPTERS
SPLIT_SALT = "mse-finetune-1"


def _gold_files() -> list:
    """The gold sets (evaluate.discover_gold_files); imported here because evaluate imports the search stack."""
    from . import evaluate as ev
    return ev.discover_gold_files()


DEFAULTS = {
    "enabled": True,                # the nightly step (jobs.py); nothing trains below the gate anyway
    # gate
    "min_examples": 300,            # favourites ('best' marks)
    "min_queries": 100,             # ... over this many distinct queries
    "min_new": 25,                  # a new training run needs this many favourites more than the last one
    "collections": None,            # None: one shared adapter from every collection; or a list of names
    # promotion
    "fav_min_gain": 0.02,           # held-out favourites MRR must rise by at least this
    "gold_max_loss": 0.01,          # no gold set may lose more MRR than this (= evaluate.MAX_GOLD_LOSS)
    "max_latency_increase": 0.10,   # median re-rank time may grow by at most 10 %
    "min_heldout": 20,              # held-out favourite questions needed to judge at all
    "gold_collections": None,       # where the gold sets are searched (None: every "Include in All" collection)
    # data
    "test_share": 0.2,
    "negatives": 7,                 # hard negatives per positive
    "window": 10,                   # positives must be among the first `window` results (rerank.TOP reads 10)
    "include_auto": False,          # learn.py's known-item questions as extra training data
    "auto_weight": 0.3,
    "doc_tokens": 128,              # = worker.Reranker.DOC_TOKENS
    # model and optimisation (measured on the M4 Pro: q/v on 16 layers +4 % latency, all projections +34 %)
    "rank": 8, "scale": 20.0, "dropout": 0.05, "num_layers": 16,
    "keys": ["self_attn.q_proj", "self_attn.v_proj"],
    "lr": 2e-5, "warmup": 0.1, "batch_groups": 2, "epochs": 2, "steps": None, "min_steps": 100, "max_steps": 400,
    "anchor": 0.0,                  # weight of KL(base || adapter) per group: keeps the ranking near the base model
    "seed": 7,
    # budget
    "max_minutes": 15, "max_memory_gb": 6.0, "require_ac": True,
}


def settings(cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else config.load()
    return {**DEFAULTS, **(cfg.get("finetune") or {})}


def registry_dir() -> Path:
    return paths.MODELS / LORA_DIR


def work_dir() -> Path:
    return paths.DATA / "finetune"


def examples_path() -> Path:
    return work_dir() / "examples.jsonl"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _read_json(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


def _write_json(p: Path, data) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


def _read_jsonl(p: Path) -> list[dict]:
    try:
        lines = Path(p).read_text().split("\n")      # not splitlines(): passages may hold U+2028 and friends
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _norm(q: str) -> str:
    from .feedback import normalize
    return normalize(q)


def _allow(s: dict):
    colls = s.get("collections")
    return (lambda c: True) if not colls else (lambda c: c in colls)


# ------------------------------------------------------------------ gate and split (pure)

def gate(n_favourites: int, n_queries: int, min_examples: int = 300, min_queries: int = 100) -> tuple[bool, str]:
    """Enough favourites to learn from? (ok, message); below the gate the message is "skipped: N/300"."""
    if n_favourites < min_examples:
        return False, f"skipped: {n_favourites}/{min_examples}"
    if n_queries < min_queries:
        return False, f"skipped: {n_queries}/{min_queries} queries"
    return True, f"ready: {n_favourites} favourites over {n_queries} queries"


def split_of(norm: str, test_share: float = 0.2, salt: str = SPLIT_SALT) -> str:
    """'train' or 'test' for a normalised query: a stable hash, so every example of one query lands on one side,
    and a query keeps its side from night to night."""
    h = int(hashlib.sha256(f"{salt}\n{norm}".encode()).hexdigest()[:8], 16)
    return "test" if h % 10000 < round(test_share * 10000) else "train"


def gold_queries(files=None) -> set[str]:
    """The normalised questions of the hold-out gold sets: never trained on."""
    out = set()
    for f in files or _gold_files():
        data = _read_json(f, {}) or {}
        out |= {_norm(g["q"]) for g in data.get("queries", []) if g.get("q")}
    return out


def counts(s: dict | None = None) -> dict:
    """Favourites ('best' marks) and distinct queries in the collections the fine-tune learns from."""
    from . import feedback
    s = s or settings()
    allow = _allow(s)
    favs = [g for g in feedback.gold() if allow(g["collection"])]
    per = Counter()
    for g in favs:
        per[g["collection"]] += len(g["expect"])
    return {"favourites": sum(per.values()), "queries": len({_norm(g["q"]) for g in favs}),
            "per_collection": dict(per)}


# ------------------------------------------------------------------ collect

def candidates(query: str, collection: str, window: int = 10) -> tuple[list[dict], str]:
    """The documents retrieval hands the re-ranker for `query` in one collection (feedback application and
    re-ranking off), in retrieval order, each with the text the re-ranker reads (search opts keep_rr: the "_rr"
    field), and the query text it receives (the parsed query, without inline filters)."""
    from . import search
    res = search.search(query, collections=[collection], limit=max(window, 10), mode="hybrid",
                        opts={"feedback": False, "rerank": False, "keep_rr": True})
    rows = res["results"][:window]
    if rows and not any(r.get("_rr") for r in rows):
        raise RuntimeError("search returned no re-ranker passages (search.py opts keep_rr)")
    out = [{"path": r["path"], "copies": [c["path"] for c in r.get("copies") or []], "docid": r.get("docid"),
            "rank": i + 1, "text": r["_rr"]} for i, r in enumerate(rows) if r.get("_rr")]
    return out, res.get("text") or query


def _paths_of(c: dict) -> set[str]:
    return {c["path"].lower(), *(p.lower() for p in c.get("copies") or [])}


def make_group(query: str, cands: list[dict], pos_path: str, expect: list[str], hidden: set[str] | None = None,
               k: int = 7) -> dict | None:
    """One training group: the positive and up to k hard negatives from the candidate list. Negatives: files hidden
    for this query, then everything ranked above the positive (best first), then the nearest below it. Other
    favourites of the same query are never negatives. None when the positive was not retrieved or has no rival."""
    exp = {e.lower() for e in expect}
    hide = {h.lower() for h in hidden or ()}
    pos = next((c for c in cands if pos_path.lower() in _paths_of(c)), None)
    if pos is None:
        return None
    others = [c for c in cands if c is not pos and not (_paths_of(c) & exp)]
    hid = [c for c in others if _paths_of(c) & hide]
    above = [c for c in others if c["rank"] < pos["rank"] and c not in hid]
    below = [c for c in others if c["rank"] > pos["rank"] and c not in hid]
    negs = (hid + above + below)[:k]
    if not negs:
        return None
    return {"query": query, "pos": {"path": pos["path"], "rank": pos["rank"], "text": pos["text"]},
            "negs": [{"path": c["path"], "rank": c["rank"], "text": c["text"], "hidden": c in hid} for c in negs]}


def collect(s: dict | None = None, save: bool = True, progress=None, out: Path | None = None) -> dict:
    """Favourites -> training groups (written to <data>/finetune/examples.jsonl) and the held-out questions."""
    from . import feedback, learn
    s = s or settings()
    allow = _allow(s)
    favs = [g for g in feedback.gold() if allow(g["collection"])]
    hidden: dict[tuple, set] = defaultdict(set)
    for it in feedback.list_items():
        if it["kind"] == "hide" and allow(it["collection"]):
            hidden[(_norm(it["query"]), it["collection"])].add(it["path"])
    guard = gold_queries()
    questions = [(g, "favourite", 1.0) for g in favs]
    if s["include_auto"]:
        questions += [(g, "auto", float(s["auto_weight"])) for g in learn.load_auto_gold() if allow(g.get("collection"))]
    test_norms = {n for n in (_norm(g["q"]) for g in favs) if split_of(n, s["test_share"]) == "test"}
    cfg_names = {c["name"] for c in config.load()["collections"] if c.get("enabled", True)}

    groups, heldout, st = [], [], Counter()
    for i, (g, source, w) in enumerate(questions):
        if progress:
            progress("collect", i, len(questions), "")
        norm = _norm(g["q"])
        if not norm:
            continue
        if norm in guard:
            st["dropped: same question as a gold set"] += 1
            continue
        split = split_of(norm, s["test_share"]) if source == "favourite" else "train"
        if source != "favourite" and norm in test_norms:
            st["dropped: known-item question equals a held-out favourite"] += 1
            continue
        if g.get("collection") not in cfg_names:
            st["dropped: collection not indexed"] += 1
            continue
        if source == "favourite" and split == "test":
            heldout.append({"q": g["q"], "expect": list(g["expect"]), "collection": g["collection"]})
        try:
            cands, qtext = candidates(g["q"], g["collection"], s["window"])
        except Exception as e:      # one failing search must not stop the collection
            st[f"search failed: {type(e).__name__}"] += 1
            continue
        for pos_path in g["expect"]:
            grp = make_group(qtext, cands, pos_path, g["expect"], hidden.get((norm, g["collection"])), s["negatives"])
            if grp is None:
                st[f"skipped: favourite not in the first {s['window']} results, or no rival"] += 1
                continue
            grp.update(raw_query=g["q"], norm=norm, collection=g["collection"], source=source, weight=w, split=split)
            groups.append(grp)
            st[f"groups: {source} / {split}"] += 1
    if progress:
        progress("collect", len(questions), len(questions), "")
    summary = {"when": _now(), "groups": len(groups), "train": sum(g["split"] == "train" for g in groups),
               "test": sum(g["split"] == "test" for g in groups), "heldout_questions": len(heldout),
               "per_collection": dict(Counter(g["collection"] for g in groups)),
               "stats": dict(st), **{k: v for k, v in counts(s).items() if k != "per_collection"}}
    if save:
        p = Path(out) if out else examples_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(p.parent, 0o700)
        tmp = p.with_name(p.name + ".tmp")
        with open(tmp, "w") as f:
            for g in groups:
                f.write(json.dumps(g, ensure_ascii=False) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        _write_json(p.with_name("collect.json"), {**summary, "heldout": heldout})
    return {**summary, "heldout": heldout, "examples": groups}


# ------------------------------------------------------------------ train

def _on_ac() -> bool:
    try:
        from .jobs import on_ac_power
    except Exception:
        return True
    return on_ac_power()


def _low_priority() -> None:
    try:
        if os.nice(0) < 10:
            os.nice(10 - os.nice(0))
    except OSError:
        pass


def plan_steps(n_groups: int, s: dict) -> int:
    if s.get("steps"):
        return int(s["steps"])
    want = math.ceil(float(s["epochs"]) * n_groups / max(1, int(s["batch_groups"])))
    return max(int(s["min_steps"]), min(int(s["max_steps"]), want))


def _schedule(lr: float, steps: int, warmup: float):
    w = max(1, int(warmup * steps))

    def f(step):
        import mlx.core as mx
        step = step.astype(mx.float32) if hasattr(step, "astype") else float(step)
        warm = lr * (step + 1) / w
        prog = (step - w) / max(1, steps - w)
        cos = lr * (0.1 + 0.9 * 0.5 * (1 + mx.cos(math.pi * mx.minimum(mx.maximum(prog, 0.0), 1.0))))
        return mx.where(step < w, warm, cos)
    return f


def _pack(batch: list[tuple], pad: int):
    """Groups [(head, [pos_seq, neg_seq...], weight, base log-probs | None)] -> padded ids, last-token index,
    (G, D) row map, mask, weights, (G, D) base log-probs (0 where unknown)."""
    import mlx.core as mx
    rows, last, gidx, valid, w, base = [], [], [], [], [], []
    dmax = max(len(b[1]) for b in batch)
    for head, seqs, wt, lp in batch:
        idx = []
        for sq in seqs:
            idx.append(len(rows))
            rows.append(head + sq)
            last.append(len(head) + len(sq) - 1)
        gidx.append(idx + [idx[0]] * (dmax - len(idx)))
        valid.append([True] * len(idx) + [False] * (dmax - len(idx)))
        w.append(wt)
        base.append((list(lp) if lp is not None else [0.0] * len(idx)) + [0.0] * (dmax - len(idx)))
    width = max(len(r) for r in rows)
    ids = mx.array([r + [pad] * (width - len(r)) for r in rows])
    return (ids, mx.array(last), mx.array(gidx), mx.array(valid), mx.array(w, dtype=mx.float32),
            mx.array(base, dtype=mx.float32))


def _group_logp(rr, ids, last, gidx, valid):
    """Each group's log-softmax over its documents' yes-minus-no margins: the listwise distribution."""
    import mlx.core as mx
    h = rr.model.model(ids)
    sc = rr.margin(h[mx.arange(ids.shape[0]), last])
    S = mx.where(valid, sc[gidx], -1e9)
    return S - mx.logsumexp(S, axis=1, keepdims=True)


def _listwise(rr, groups: list[dict]) -> dict:
    """Diagnostic: the positive's rank among its own candidates when the model alone orders them (MRR, hit@1),
    next to the retrieval order. Scored through Reranker.score, i.e. the shared-prefix inference path."""
    rr_ranks, ret_ranks = [], []
    for g in groups:
        sc = rr.score(g["query"], [g["pos"]["text"]] + [n["text"] for n in g["negs"]])
        rr_ranks.append(1 + sum(x > sc[0] for x in sc[1:]))
        ret_ranks.append(1 + sum(n["rank"] < g["pos"]["rank"] for n in g["negs"]))
    n = len(groups) or 1
    return {"n": len(groups), "mrr": round(sum(1 / r for r in rr_ranks) / n, 4),
            "hit@1": round(sum(r == 1 for r in rr_ranks) / n, 4),
            "retrieval_mrr": round(sum(1 / r for r in ret_ranks) / n, 4)}


def new_adapter_dir(root: Path | None = None, when: float | None = None) -> Path:
    root = Path(root or registry_dir())
    stamp = time.strftime("%Y-%m-%d_%H%M", time.localtime(when or time.time()))
    p, n = root / stamp, 2
    while p.exists():
        p, n = root / f"{stamp}_{n}", n + 1
    p.mkdir(parents=True)
    return p


def train(s: dict | None = None, examples: Path | None = None, root: Path | None = None, dry_run: bool = False,
          progress=None, say=None) -> dict:
    """Train a candidate adapter on the 'train' groups. Returns {"status": "trained", "id", ...} or a skip/abort."""
    s = s or settings()
    say = say or (lambda *a: None)
    src = Path(examples or examples_path())
    allg = _read_jsonl(src)
    groups = [g for g in allg if g.get("split") == "train"]
    test = [g for g in allg if g.get("split") == "test"]
    if not groups:
        return {"status": f"skipped: no training groups in {src.name} (run collect first)"}
    steps = plan_steps(len(groups), s)
    lora = {"rank": int(s["rank"]), "scale": float(s["scale"]), "dropout": float(s["dropout"]),
            "keys": list(s["keys"])}
    plan = {"train_groups": len(groups), "test_groups": len(test), "steps": steps,
            "batch_groups": int(s["batch_groups"]), "lora": lora, "num_layers": int(s["num_layers"]), "lr": s["lr"],
            "per_collection": dict(Counter(g["collection"] for g in groups)),
            "sources": dict(Counter(g["source"] for g in groups))}
    if dry_run:
        return {"status": "dry-run", **plan}
    if s["require_ac"] and not _on_ac():
        return {"status": "skipped: on battery"}
    _low_priority()

    import mlx.core as mx
    import mlx.nn as nn
    import mlx.optimizers as optim
    from mlx.utils import tree_flatten
    from mlx_lm.tuner.utils import linear_to_lora_layers

    from .worker import Reranker

    t_start = time.time()
    budget = float(s["max_memory_gb"]) * 1e9
    mx.random.seed(int(s["seed"]))
    rnd = random.Random(int(s["seed"]))
    try:
        mx.set_memory_limit(int(budget))
    except Exception:
        pass
    mx.reset_peak_memory()
    base_model = Path(config.load()["model"]["reranker"])
    rr = Reranker(base_model, adapter=False)
    model = rr.model
    model.freeze()
    linear_to_lora_layers(model, int(s["num_layers"]), lora)
    n_params = sum(v.size for _, v in tree_flatten(model.trainable_parameters()))
    model.eval()
    sample = rnd.sample(groups, min(50, len(groups)))
    before = {"test": _listwise(rr, test), "train_sample": _listwise(rr, sample)}     # LoRA B = 0: the base model
    say(f"base model, candidates only: held-out MRR {before['test']['mrr']}, train sample {before['train_sample']['mrr']}")

    doc_tokens = int(s["doc_tokens"])
    anchor = float(s.get("anchor") or 0.0)
    enc = []
    for g in groups:
        head, seqs = rr.encode(g["query"], [g["pos"]["text"]] + [n["text"] for n in g["negs"]], doc_tokens)
        enc.append((head, seqs, float(g.get("weight", 1.0)), None))
    if anchor:      # the base model's own listwise distribution per group (LoRA B is still 0)
        t_a = time.time()
        for i in range(0, len(enc), 4):
            part = enc[i:i + 4]
            ids, last, gidx, valid, _, _ = _pack(part, rr.pad)
            lp = _group_logp(rr, ids, last, gidx, valid)
            mx.eval(lp)
            for j, row in enumerate(lp.tolist()):
                h_, s_, w_, _ = part[j]
                enc[i + j] = (h_, s_, w_, row[:len(s_)])
        say(f"base distributions for the anchor in {time.time() - t_a:.0f}s")

    def loss_fn(m, ids, last, gidx, valid, w, base):
        logp = _group_logp(rr, ids, last, gidx, valid)
        per = -logp[:, 0]                                  # listwise cross-entropy: -log softmax(positive)
        if anchor:                                         # + anchor x KL(base || model) over the same documents
            q = mx.where(valid, mx.exp(base), 0.0)
            per = per + anchor * (q * (base - logp)).sum(axis=1)
        return (per * w).sum() / w.sum()

    opt = optim.Adam(learning_rate=_schedule(float(s["lr"]), steps, float(s["warmup"])))
    step_fn = nn.value_and_grad(model, loss_fn)
    model.train()
    curve, run_loss, order, status = [], None, [], "trained"
    t_train = time.time()
    for step in range(1, steps + 1):
        batch = []
        while len(batch) < int(s["batch_groups"]):
            if not order:
                order = list(range(len(enc)))
                rnd.shuffle(order)
            batch.append(enc[order.pop()])
        loss, grads = step_fn(model, *_pack(batch, rr.pad))
        opt.update(model, grads)
        mx.eval(model.trainable_parameters(), opt.state, loss)
        lv = float(loss.item())
        run_loss = lv if run_loss is None else 0.9 * run_loss + 0.1 * lv
        if step % 10 == 0 or step == steps:
            curve.append([step, round(run_loss, 4)])
            if progress:
                progress("train", step, steps, f"loss {run_loss:.3f}")
        if step % 50 == 0:
            say(f"step {step}/{steps} loss {run_loss:.3f} {time.time() - t_train:.0f}s "
                f"peak {mx.get_peak_memory() / 1e9:.2f} GB")
        if time.time() - t_start > float(s["max_minutes"]) * 60:
            status = f"aborted: over the {s['max_minutes']}-minute budget at step {step}/{steps}"
        elif mx.get_peak_memory() > budget:
            status = f"aborted: over the {s['max_memory_gb']} GB memory budget at step {step}/{steps}"
        elif s["require_ac"] and step % 25 == 0 and not _on_ac():
            status = f"aborted: on battery at step {step}/{steps}"
        if status != "trained":
            break
    train_s = time.time() - t_train
    peak = mx.get_peak_memory() / 1e9
    if status != "trained":
        return {"status": status, "train_s": round(train_s, 1), "peak_memory_gb": round(peak, 2), **plan}
    model.eval()
    after = {"test": _listwise(rr, test), "train_sample": _listwise(rr, sample)}
    say(f"trained, candidates only: held-out MRR {after['test']['mrr']}, train sample {after['train_sample']['mrr']}")

    out = new_adapter_dir(root)
    mx.save_safetensors(str(out / "adapters.safetensors"), dict(tree_flatten(model.trainable_parameters())))
    _write_json(out / "adapter_config.json", {"fine_tune_type": "lora", "num_layers": int(s["num_layers"]),
                                              "lora_parameters": lora, "base_model": str(base_model),
                                              "trained_by": "mse.finetune"})
    collected = _read_json(src.with_name("collect.json"), {}) or {}
    info = {"id": out.name, "created": _now(), "status": status, "base_model": str(base_model),
            "examples": str(src), "favourites": collected.get("favourites"), "queries": collected.get("queries"),
            "settings": {k: s[k] for k in ("rank", "scale", "dropout", "num_layers", "keys", "lr", "warmup",
                                           "batch_groups", "epochs", "anchor", "negatives", "window", "test_share",
                                           "include_auto", "auto_weight", "doc_tokens", "collections", "seed")},
            **plan, "trainable_params": int(n_params), "loss": curve, "train_s": round(train_s, 1),
            "total_s": round(time.time() - t_start, 1), "peak_memory_gb": round(peak, 2),
            "listwise": {"base": before, "trained": after}}
    _write_json(out / "training.json", info)
    _write_json(out / "split.json", {sp: [{"query": g["raw_query"], "collection": g["collection"], "source": g["source"],
                                           "pos": g["pos"]["path"], "negs": [n["path"] for n in g["negs"]]}
                                          for g in allg if g.get("split") == sp] for sp in ("train", "test")})
    _write_json(out / "heldout.json", collected.get("heldout") or [])
    log_event({"kind": "train", "id": out.name, "status": status, "favourites": collected.get("favourites"),
               "queries": collected.get("queries"), "train_groups": len(groups), "steps": steps,
               "train_s": info["train_s"], "peak_memory_gb": info["peak_memory_gb"],
               "listwise_test": [before["test"]["mrr"], after["test"]["mrr"]]}, root)
    return {"status": "trained", "id": out.name, "dir": str(out), "train_s": info["train_s"],
            "total_s": info["total_s"], "peak_memory_gb": info["peak_memory_gb"], "trainable_params": int(n_params),
            "listwise": info["listwise"], **plan}


# ------------------------------------------------------------------ evaluate

def decide(base: dict, cand: dict, fav_min_gain: float = 0.02, gold_max_loss: float = 0.01,
           min_heldout: int = 20, latency_ratio: float | None = None,
           max_latency_increase: float = 0.10) -> tuple[bool, list[str]]:
    """The promotion rule. `base` / `cand`: set name -> {"kind": "favourites"|"gold", "n", "mrr"}.
    Promote only when the held-out favourites gain at least fav_min_gain MRR, no gold set loses more than
    gold_max_loss, every set could be measured, and re-ranking is at most max_latency_increase slower.
    Returns (promote, reasons why not)."""
    out = []
    favs = [n for n, m in base.items() if m.get("kind") == "favourites"]
    golds = [n for n, m in base.items() if m.get("kind") == "gold"]
    if not favs:
        out.append("no held-out favourites to measure")
    for n in favs:
        b, c = base[n], cand.get(n) or {}
        if b.get("n", 0) < min_heldout:
            out.append(f"only {b.get('n', 0)} held-out favourite questions (needs {min_heldout})")
            continue
        gain = c.get("mrr", 0.0) - b.get("mrr", 0.0)
        if gain < fav_min_gain - 1e-9:
            out.append(f"favourites MRR {gain:+.3f} (needs +{fav_min_gain})")
    if not golds:
        out.append("no gold set to guard against regressions")
    for n in golds:
        b, c = base[n], cand.get(n) or {}
        if not b.get("n"):
            out.append(f"{n} could not be measured (no answerable questions)")
            continue
        loss = b.get("mrr", 0.0) - c.get("mrr", 0.0)
        if loss > gold_max_loss + 1e-9:
            out.append(f"{n} MRR {-loss:+.3f} (may lose at most {gold_max_loss})")
    if latency_ratio is not None and latency_ratio > 1 + max_latency_increase + 1e-9:
        out.append(f"re-ranking {100 * (latency_ratio - 1):+.0f}% slower (at most +{100 * max_latency_increase:.0f}%)")
    return not out, out


def _use_adapter(adir: Path | None) -> None:
    """Point this process's re-rank worker at an adapter (None: the base model): restart it, forget cached scores."""
    os.environ["MSE_RERANK_ADAPTER"] = str(adir) if adir else "none"
    from . import rerank
    from .embedder import pool
    pool().workers["rerank"].stop()
    cache = getattr(rerank, "_cache", None)
    if hasattr(cache, "clear"):
        cache.clear()


def latency(adir: Path, groups: list[dict], rounds: int = 3, docs: int = 10) -> dict:
    """Median re-rank time of the base model and the adapter on the same queries and passages, interleaved call by
    call so background load hits both alike. Loads MLX in this process."""
    from .worker import Reranker
    base_model = Path(config.load()["model"]["reranker"])
    a, b = Reranker(base_model, adapter=False), Reranker(base_model, adapter=adir)
    if not b.adapter:
        raise RuntimeError(f"adapter did not load: {b.adapter_error}")
    jobs = [(g["query"], ([g["pos"]["text"]] + [n["text"] for n in g["negs"]])[:docs]) for g in groups[:20]]
    if not jobs:
        return {}
    for m in (a, b):
        m.score(*jobs[0])                                   # warm up
    ta, tb = [], []
    for _ in range(rounds):
        for q, d in jobs:
            for m, t in ((a, ta), (b, tb)):
                t0 = time.perf_counter()
                m.score(q, d)
                t.append(time.perf_counter() - t0)
    med = lambda xs: sorted(xs)[len(xs) // 2]
    return {"calls": len(ta), "base_ms": round(1000 * med(ta), 1), "adapter_ms": round(1000 * med(tb), 1),
            "ratio": round(med(tb) / med(ta), 3)}


def evaluate_adapter(adapter_id: str | None = None, s: dict | None = None, root: Path | None = None,
                     gold_files=None, examples: Path | None = None, threads: int = 4, record: bool = True,
                     say=None) -> dict:
    """Base vs candidate on the held-out favourites and the gold sets (search with the live re-rank settings),
    plus latency; writes the verdict to the adapter's metrics.json."""
    from . import evaluate as ev
    s = s or settings()
    say = say or (lambda *a: None)
    root = Path(root or registry_dir())
    aid = adapter_id or latest_id(root)
    if not aid:
        raise ValueError("no adapter to evaluate")
    adir = root / aid
    heldout = _read_json(adir / "heldout.json", []) or []
    sets = ev.load_sets(s["gold_collections"], gold_files or _gold_files(), favourites=False, auto=False)
    known = {c["name"] for c in config.load()["collections"] if c.get("enabled", True)}
    sets["favourites"] = {"kind": "favourites", "skipped": sum(g["collection"] not in known for g in heldout),
                          "queries": [(g, [g["collection"]]) for g in heldout if g["collection"] in known]}
    knobs = ev.current_settings()
    prev = os.environ.get("MSE_RERANK_ADAPTER")
    t0 = time.time()
    try:
        _use_adapter(None)
        base = ev.score(sets, knobs, rerank=True, threads=threads)
        say(f"base scored in {time.time() - t0:.0f}s")
        _use_adapter(adir)
        cand = ev.score(sets, knobs, rerank=True, threads=threads)
        say(f"candidate scored in {time.time() - t0:.0f}s")
    finally:
        if prev is None:
            os.environ.pop("MSE_RERANK_ADAPTER", None)
        else:
            os.environ["MSE_RERANK_ADAPTER"] = prev
        from .embedder import pool
        pool().workers["rerank"].stop()
    test = [g for g in _read_jsonl(examples or examples_path()) if g.get("split") == "test"] or \
        [g for g in _read_jsonl(examples or examples_path()) if g.get("split") == "train"]
    lat = latency(adir, test) if test else {}
    b, c = ev.summary(base), ev.summary(cand)
    ok, reasons = decide(b, c, s["fav_min_gain"], s["gold_max_loss"], s["min_heldout"], lat.get("ratio"),
                         s["max_latency_increase"])
    changed = [{"set": name, "q": q, "base": rb, "adapter": rc}
               for name in base["sets"] for q, rb, rc in zip(base["sets"][name]["questions"], base["sets"][name]["ranks"],
                                                               cand["sets"][name]["ranks"]) if rb != rc]
    result = {"id": aid, "when": _now(), "promote": ok, "reasons": reasons, "base": b, "adapter": c,
              "delta_mrr": {n: round(c[n]["mrr"] - b[n]["mrr"], 4) for n in b if n in c},
              "latency": lat, "changed": changed, "eval_s": round(time.time() - t0),
              "thresholds": {k: s[k] for k in ("fav_min_gain", "gold_max_loss", "min_heldout", "max_latency_increase")},
              "rerank_settings": {k: knobs[k] for k in ("rerank_gate", "rerank_weight")}}
    if record:
        old = _read_json(adir / "metrics.json")
        if old:
            result["earlier"] = (old.pop("earlier", None) or []) + [old]
        _write_json(adir / "metrics.json", result)
        log_event({"kind": "eval", "id": aid, "promote": ok, "reasons": reasons, "delta_mrr": result["delta_mrr"],
                   "latency_ratio": lat.get("ratio")}, root)
    return result


# ------------------------------------------------------------------ registry

def log_event(entry: dict, root: Path | None = None) -> None:
    root = Path(root or registry_dir())
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "history.jsonl", "a") as f:
        f.write(json.dumps({"when": _now(), **entry}, ensure_ascii=False) + "\n")


def history(root: Path | None = None) -> list[dict]:
    return _read_jsonl(Path(root or registry_dir()) / "history.jsonl")


def adapters(root: Path | None = None) -> list[str]:
    """Every trained adapter id, oldest first (directories holding adapters.safetensors)."""
    root = Path(root or registry_dir())
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and (p / "adapters.safetensors").is_file())


def latest_id(root: Path | None = None) -> str | None:
    ids = adapters(root)
    return ids[-1] if ids else None


def active_id(root: Path | None = None) -> str | None:
    """The adapter named in CURRENT (None: the base model)."""
    try:
        v = (Path(root or registry_dir()) / "CURRENT").read_text().strip()
    except OSError:
        return None
    return None if v.lower() in ("", "none", "off", "base") else v


def _set_current(root: Path, aid: str | None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "CURRENT.tmp"
    tmp.write_text((aid or "none") + "\n")
    os.replace(tmp, root / "CURRENT")


def promote(adapter_id: str | None = None, root: Path | None = None, force: bool = False,
            dry_run: bool = False) -> dict:
    """Make an adapter the active one (only the CURRENT pointer moves). Without an id: the newest adapter whose
    evaluation allowed promotion. An adapter whose evaluation refused it needs force=True."""
    root = Path(root or registry_dir())
    ids = adapters(root)
    if adapter_id is None:
        ok = [i for i in ids if (_read_json(root / i / "metrics.json", {}) or {}).get("promote")]
        if not ok:
            raise ValueError("no evaluated adapter that passed the promotion rule")
        adapter_id = ok[-1]
    if adapter_id not in ids:
        raise KeyError(f"no adapter {adapter_id}")
    m = _read_json(root / adapter_id / "metrics.json", {}) or {}
    if not force and not m.get("promote"):
        why = "; ".join(m.get("reasons") or ["not evaluated yet"])
        raise ValueError(f"{adapter_id} did not pass the promotion rule ({why}); use force to promote anyway")
    prev = active_id(root)
    entry = {"kind": "promote", "action": "promoted", "from": prev, "to": adapter_id, "forced": bool(force and not m.get("promote"))}
    if prev == adapter_id:
        return {**entry, "action": "unchanged"}
    if dry_run:
        return {**entry, "action": "dry-run"}
    _set_current(root, adapter_id)
    log_event(entry, root)
    return entry


def rollback(adapter_id: str | None = None, root: Path | None = None, dry_run: bool = False) -> dict:
    """Move CURRENT back: to `adapter_id` ("none" = the base model) if given, else undo the most recent promotion
    that has not been undone yet (repeat to go further back)."""
    root = Path(root or registry_dir())
    prev = active_id(root)
    if adapter_id is not None:
        target = None if adapter_id.lower() in ("none", "base", "off") else adapter_id
        if target and target not in adapters(root):
            raise KeyError(f"no adapter {target}")
        undoes = None
    else:
        skip, found = 0, None
        for e in reversed(history(root)):
            if e.get("kind") == "rollback" and e.get("undoes"):
                skip += 1
            elif e.get("kind") == "promote":
                if skip:
                    skip -= 1
                    continue
                found = e
                break
        if not found:
            raise ValueError("nothing to roll back: no promotion left to undo")
        target, undoes = found.get("from"), found.get("when")
    entry = {"kind": "rollback", "action": "rolled back", "from": prev, "to": target, "undoes": undoes}
    if dry_run:
        return {**entry, "action": "dry-run"}
    _set_current(root, target)
    log_event(entry, root)
    return entry


# ------------------------------------------------------------------ status and the nightly run

def status(s: dict | None = None, root: Path | None = None) -> dict:
    s = s or settings()
    root = Path(root or registry_dir())
    try:
        c = counts(s)
    except Exception as e:
        c = {"favourites": 0, "queries": 0, "per_collection": {}, "error": str(e)}
    ok, msg = gate(c["favourites"], c["queries"], s["min_examples"], s["min_queries"])
    hist = history(root)
    last = next((e for e in reversed(hist) if e.get("kind") == "run"), None)
    act = active_id(root)
    m = (_read_json(root / act / "metrics.json", {}) or {}) if act else {}
    metrics = {"base": m.get("base"), "adapter": m.get("adapter"), "delta_mrr": m.get("delta_mrr"),
               "latency": m.get("latency")} if m else None
    if not ok and c["favourites"] < s["min_examples"]:
        line = f"Learning: {c['favourites']} / {s['min_examples']} favourites until the first fine-tune"
    elif not ok:
        line = f"Learning: {c['queries']} / {s['min_queries']} different searches until the first fine-tune"
    else:
        line = f"Learning: {c['favourites']} favourites over {c['queries']} searches"
    if last:
        line += f" · last run {last['when'][:16].replace('T', ' ')} {last.get('action', '')}".rstrip()
    line += f" · active adapter {act}" if act else " · base model"
    return {"enabled": bool(s["enabled"]), **c, "min_examples": s["min_examples"], "min_queries": s["min_queries"],
            "gate": msg, "ready": ok, "active": act, "active_metrics": metrics, "last_run": last,
            "adapters": adapters(root), "line": line}


def run(s: dict | None = None, force: bool = False, dry_run: bool = False, root: Path | None = None,
        progress=None, say=None) -> dict:
    """gate -> collect -> train -> evaluate -> promote when the rule allows. One history line per call."""
    s = s or settings()
    say = say or (lambda *a: None)
    root = Path(root or registry_dir())
    t0 = time.time()
    c = counts(s)
    entry = {"kind": "run", "favourites": c["favourites"], "queries": c["queries"], "dry_run": dry_run}

    def done(action, reason, **more):
        entry.update(action=action, reason=reason, duration_s=round(time.time() - t0), **more)
        if not dry_run:
            log_event(entry, root)
        return entry

    ok, msg = gate(c["favourites"], c["queries"], s["min_examples"], s["min_queries"])
    if not ok and not force:
        return done("skipped", msg)
    last = next((e for e in reversed(history(root)) if e.get("kind") == "train" and e.get("favourites") is not None),
                None)
    if last and not force and c["favourites"] - last["favourites"] < int(s["min_new"]):
        return done("skipped", f"skipped: {c['favourites'] - last['favourites']} new favourites since {last['id']} "
                               f"(needs {s['min_new']})")
    if s["require_ac"] and not _on_ac():
        return done("skipped", "skipped: on battery")
    col = collect(s, save=not dry_run, progress=progress)
    say(f"collected {col['groups']} groups ({col['train']} train / {col['test']} test), "
        f"{col['heldout_questions']} held-out questions")
    if dry_run:            # nothing was saved: plan from the groups just collected
        tr = {"status": "dry-run", "train_groups": col["train"], "test_groups": col["test"],
              "steps": plan_steps(col["train"], s) if col["train"] else 0}
        return done("dry-run", "would collect, train, evaluate", collect={k: v for k, v in col.items()
                                                                            if k not in ("examples", "heldout")},
                    train=tr)
    tr = train(s, root=root, progress=progress, say=say)
    if tr["status"] != "trained":
        return done("failed" if tr["status"].startswith("aborted") else "skipped", tr["status"])
    ev = evaluate_adapter(tr["id"], s, root=root, say=say)
    if not ev["promote"]:
        return done("kept", "; ".join(ev["reasons"]), id=tr["id"], delta_mrr=ev["delta_mrr"])
    p = promote(tr["id"], root=root)
    return done("promoted", "all promotion rules pass", id=tr["id"], previous=p.get("from"), delta_mrr=ev["delta_mrr"])


# ------------------------------------------------------------------ CLI

def _safe(text: str) -> str:
    """Queries and paths printed here go through the identifier mask (never document text)."""
    from .redact import redact
    return redact(text)


def _show_examples(groups: list[dict], n: int = 5) -> None:
    for g in groups[:n]:
        print(f"  [{g['collection']}/{g['split']}] \"{_safe(g['raw_query'])}\"")
        print(f"     + #{g['pos']['rank']} {_safe(g['pos']['path'])}")
        for x in g["negs"][:3]:
            print(f"     - #{x['rank']} {_safe(x['path'])}")
        if len(g["negs"]) > 3:
            print(f"     - … {len(g['negs']) - 3} more")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m mse.finetune", description="LoRA fine-tune of the re-ranker")
    ap.add_argument("cmd", choices=["status", "collect", "train", "eval", "promote", "rollback", "run"])
    ap.add_argument("id", nargs="?", help="adapter id (eval, promote, rollback; rollback 'none' = base model)")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    ap.add_argument("--min", type=int, help="favourites needed (gate), default 300")
    ap.add_argument("--min-queries", type=int, help="distinct queries needed (gate), default 100")
    ap.add_argument("--steps", type=int, help="training steps (default: 2 epochs, 100-400)")
    ap.add_argument("--force", action="store_true", help="run: ignore the gate; promote: ignore a refusal")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    s = settings()
    if a.min is not None:
        s["min_examples"] = a.min
    if a.min_queries is not None:
        s["min_queries"] = a.min_queries
    if a.steps:
        s["steps"] = a.steps
    say = (lambda *x: print(*x, file=sys.stderr, flush=True)) if a.json else (lambda *x: print(*x, flush=True))

    def emit(obj, text=None):
        out = json.dumps(obj, ensure_ascii=False, indent=1, default=str) if a.json or text is None else text
        if not sys.stdout.isatty() and config.load().get("agents", {}).get("redact", True):
            out = _safe(out)        # piped (agents, the nightly job): queries with identifiers come out masked
        print(out)

    try:
        if a.cmd == "status":
            st = status(s)
            emit(st, f"{st['line']}\n  gate: {st['gate']} · adapters: {len(st['adapters'])}"
                     + (f" · last run: {st['last_run'].get('reason')}" if st.get("last_run") else ""))
        elif a.cmd == "collect":
            ok, msg = gate(*[counts(s)[k] for k in ("favourites", "queries")], s["min_examples"], s["min_queries"])
            r = collect(s, save=not a.dry_run)
            if a.json:
                emit({k: v for k, v in r.items() if k not in ("examples", "heldout")})
            else:
                print(f"{r['favourites']} favourites over {r['queries']} queries ({msg})")
                print(f"{r['groups']} groups: {r['train']} train, {r['test']} test · "
                      f"{r['heldout_questions']} held-out questions" + (" · not saved (dry run)" if a.dry_run else ""))
                for k, v in sorted(r["stats"].items()):
                    print(f"  {k}: {v}")
                _show_examples(r["examples"])
        elif a.cmd == "train":
            c = counts(s)
            ok, msg = gate(c["favourites"], c["queries"], s["min_examples"], s["min_queries"])
            if not ok and not a.force:
                emit({"status": msg}, msg)
                return
            emit(train(s, dry_run=a.dry_run, say=say))
        elif a.cmd == "eval":
            r = evaluate_adapter(a.id, s, record=not a.dry_run, say=say)
            emit(r if a.json else {k: v for k, v in r.items() if k not in ("changed", "earlier")})
        elif a.cmd == "promote":
            emit(promote(a.id, force=a.force, dry_run=a.dry_run))
        elif a.cmd == "rollback":
            emit(rollback(a.id, dry_run=a.dry_run))
        elif a.cmd == "run":
            emit(run(s, force=a.force, dry_run=a.dry_run, say=say))
    except (ValueError, KeyError) as e:
        sys.exit(f"error: {e}")
    finally:                # this process's own embedding / re-rank workers (collect, eval) stop with it
        emb = sys.modules.get("mse.embedder")
        if emb is not None and getattr(emb, "_pool", None) is not None:
            emb._pool.stop_all()


if __name__ == "__main__":
    main()
