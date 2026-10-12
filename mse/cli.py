"""`mse` — command line for MySearch. Talks to the background daemon when it is running."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from . import __version__, config, paths

TTY = sys.stdout.isatty()


def c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if TTY else s


def client():
    from .mcp import HttpClient
    return HttpClient()


def daemon_up() -> bool:
    try:
        client()._call("GET", "/api/status")
        return True
    except Exception:
        return False


def human_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def agent_safe(text: str) -> str:
    """Interactive terminal: everything. Piped (agents, scripts): personal identifiers masked."""
    if not TTY and config.load().get("agents", {}).get("redact", True):
        from .redact import redact
        return redact(text)
    return text


def marks(s: str) -> str:
    s = re.sub(r"<mark>(.*?)</mark>", lambda m: c("1;33", m.group(1)), s or "")
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#x27;", "'")


# ------------------------------------------------------------------ commands

def cmd_search(a):
    filters = {k: [getattr(a, k)] for k in ("type", "after", "before", "path", "include", "exclude") if getattr(a, k)}
    # "w: invoice" / "invoice in:work" in the query overrides -c; -c takes collections, groups, aliases and "all",
    # comma separated; unknown names -> error
    tokens, query = config.split_scope(a.query, config.load())
    colls = tokens or _scope(a.collection)
    if daemon_up():
        res = client().search(query, colls, a.limit, filters, a.mode, not a.fast)
    else:
        from . import search
        res = search.search(query, collections=colls, limit=a.limit, filters=filters, mode=a.mode,
                            opts=None if a.fast else {"rerank": True})
    if a.json:
        out = json.dumps(res, ensure_ascii=False, indent=2)
        print(agent_safe(out))
        return
    if a.files:
        for r in res["results"]:
            print(r["abs_path"])
        return
    bridge = sorted({t for v in res.get("bridge", {}).values() for t in v})
    where = ", ".join(res["collections"])
    label = (res.get("scope") or {}).get("label")
    if label and label != where:
        where = f"{label} ({where})"
    print(c("2", f"{len(res['results'])} results · {res['timings']['total_ms']} ms · {where}"
            + (f" · also searched: {', '.join(bridge)}" if bridge else "")))
    for n, r in enumerate(res["results"], 1):
        meta = [r["kind"], r.get("date") or ""]
        if r.get("pages"):
            meta.append(f"{r['pages']}p")
        fav = {"query": c("1;33", "  ★ favourite"), "similar": c("33", "  ★ favourite (similar query)")}.get(
            r.get("favorite"), "")
        print(f"\n{c('1', str(n) + '.')} {c('1;36', r['path'])}  {c('2', '#' + r['docid'])}{fav}")
        print(c("2", f"   {r['collection']} · {' · '.join(m for m in meta if m)} · {', '.join(r['why'])} · {r['relevance']}%"))
        for h in r["hits"][:2]:
            where = f"p.{h['page']} " if h.get("page") else ""
            if h.get("t0") is not None:
                where = f"{int(h['t0'])//60}:{int(h['t0'])%60:02d} "
            if h.get("snippet"):
                print(f"   {c('2', where)}{agent_safe(marks(h['snippet']))}")
            elif h["modality"] in ("image", "page", "video", "audio"):
                print(c("2", f"   {where}[{h['modality']} match]"))


ASK_LAST = "ask_last.json"      # the last `mse ask` turns and scope, for --follow-up (data dir tmp/, mode 0600)


def _ask_last() -> dict | None:
    from .ask import MAX_HISTORY
    try:
        d = json.loads((paths.TMP / ASK_LAST).read_text())
    except (OSError, ValueError):
        return None
    turns = [t for t in d.get("turns") or [] if isinstance(t, dict) and isinstance(t.get("q"), str)
             and t["q"].strip() and isinstance(t.get("a"), str)]
    return {"turns": turns[-MAX_HISTORY:], "collections": d.get("collections")} if turns else None


def _ask_remember(turns: list[dict], colls) -> None:
    from .ask import MAX_HISTORY
    try:
        paths.TMP.mkdir(parents=True, exist_ok=True)
        f = paths.TMP / ASK_LAST
        tmp = f.with_name(ASK_LAST + ".tmp")
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
            json.dump({"turns": turns[-MAX_HISTORY:], "collections": colls, "at": round(time.time())}, fh,
                      ensure_ascii=False)
        os.replace(tmp, f)
    except OSError:
        pass                                  # a convenience: never fail an answer over it


def cmd_ask(a):
    """A short answer with citations from the top results (Ask must be switched on). Piped: identifiers masked.
    --follow-up sends the earlier `mse ask` turns (at most 3) as history and keeps their scope unless -c is given."""
    tokens, a.question = config.split_scope(a.question, config.load())     # "w: wann …" overrides -c
    colls = tokens or _scope(a.collection)
    history = []
    if getattr(a, "follow_up", False):
        last = _ask_last()
        if not last:
            sys.exit("no earlier question to follow up — ask one first (without --follow-up)")
        history = last["turns"]
        if colls is None:
            colls = last["collections"]
    if colls is None and not history:         # like agents: the folder you are in, else everything
        here = config.collection_for_path(config.load(), os.getcwd())
        colls = [here["name"]] if here else None
    if not daemon_up():
        sys.exit("the MySearch daemon is not running — Ask needs it: mse service install (or mse serve)")
    safe = not TTY and config.load().get("agents", {}).get("redact", True)
    route = "local" if getattr(a, "local", False) else "cloud" if getattr(a, "cloud", False) else None
    sources, done, parts = [], {}, []
    try:
        for ev in client().ask(a.question, colls, agent_safe=safe, **({"history": history} if history else {}),
                               **({"route": route} if route else {})):
            if ev.get("type") == "sources":
                sources = ev.get("sources") or []
            elif ev.get("type") == "token":
                parts.append(ev["text"])
                if TTY and not a.json:
                    print(ev["text"], end="", flush=True)
            elif ev.get("type") == "done":
                done = ev
            elif ev.get("type") == "error":
                sys.exit(f"ask failed: {ev.get('message')}")
    except RuntimeError as e:
        sys.exit("Ask is switched off — turn it on in MySearch Settings (or POST /api/settings "
                 '{"ask": {"enabled": true}})' if "ask_disabled" in str(e) else str(e))
    answer = (done.get("answer") or "".join(parts)).strip()
    cited = done.get("citations") or []
    if done:
        _ask_remember(history + [{"q": a.question, "a": answer}], colls)     # for the next --follow-up
    if a.json:
        print(agent_safe(json.dumps({"question": a.question, "answer": answer, "answered": done.get("answered"),
                                     "citations": cited, "sources": sources, "model": done.get("model"),
                                     "provider": done.get("provider"), "where": done.get("where"),
                                     "fallback_reason": done.get("fallback_reason"), "redacted": done.get("redacted"),
                                     "timings": done.get("timings"), "search_query": done.get("search_query"),
                                     "rewrite": done.get("rewrite"), "rewrite_fallback": done.get("rewrite_fallback")},
                                    ensure_ascii=False, indent=2)))
        return
    print() if TTY else print(agent_safe(answer))
    for s in sources:
        if s["n"] in cited:
            where = f" p.{s['page']}" if s.get("page") else (f" {s['ts']}" if s.get("ts") else "")
            print(c("2", f"[{s['n']}] ") + agent_safe(f"{s['collection']}/{s['path']}{where}") + c("2", f"  {s['docid']}"))
    t = done.get("timings") or {}
    if TTY and t:
        by = f"{done.get('model')} · cloud ({done.get('provider')})" if done.get("where") == "cloud" else \
            f"{done.get('model')} · local" + (f" ({done['fallback_reason']})" if done.get("fallback_reason") else "")
        masked = f" · {done['redacted']} identifiers masked" if done.get("redacted") else ""
        print(c("2", f"{by} · first word {t.get('ttft_ms')} ms · total {t.get('total_ms')} ms{masked}"))
    if TTY and done.get("rewrite") not in (None, "none"):
        print(c("2", f"searched for: {done.get('search_query')} ({done.get('rewrite')})"))


def _read_secret(pid: str, from_env: bool) -> str:
    """A key from the environment (--from-env), a hidden prompt (terminal) or one line on stdin (piped or redirected):
    never a command-line argument, so it shows neither in the process list nor in the shell history."""
    from . import providers
    if from_env:
        name = providers.env_name(pid)
        v = os.environ.get(name, "")
        if not v.strip():
            sys.exit(f"error: {name} is not set in this shell")
        return v
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass(f"Key for {pid} (typing stays hidden, Enter to finish): ")
    return sys.stdin.readline()


def _print_providers(v: dict):
    online = {True: "online", False: "offline"}.get(v.get("online"), "not checked")
    local_only = v["route"] == "local"
    fb = " · local fallback on" if (v.get("local_fallback") or v["route"] == "auto") and not local_only else ""
    sel = "the local model (route local)" if local_only else f"{v['provider']} {v['model']}"
    print(f"route {v['route']}{fb} · selected: {sel} · {online}")
    if not local_only and not any(p["id"] == v["provider"] and p["has_key"] for p in v["providers"]):
        print(c("33", f"  the selected provider has no key; choose a model: mse providers use <id> <model>"))
    for p in v["providers"]:
        mark = c("1;32", "*") if p["id"] == v["provider"] else " "
        src = p.get("key_source")
        key = (f"key: from {p['key_file']}" if src == "file" and p.get("key_file") else "key ✓" if p["has_key"]
               else "no key" if p["needs_key"] else "no key needed")
        fix = f"mse providers connect {p['id']}" if p.get("key_file") else f"mse providers set-key {p['id']}"
        st = {"ok": c("32", "ok"), "no_key": c("33", f"→ {fix}"),
              "offline": c("33", "offline" + (f": {p['hint']}" if p.get("hint") else "")),
              "error": c("31", "error")}.get(p["status"], p["status"])
        print(f"{mark} {c('1;36', p['id']):14s} {p['label']}  {c('2', p['kind'] + ' · ' + p['base_url'] + ' · ' + key)}"
              f"  {st}" + (c("2", "  built in") if p["builtin"] else ""))
        if p.get("error"):
            print(c("31", f"    {p['error']}"))
        if p["models"]:
            shown = [m + (" ⚠" if config.trains_on_inputs(m, p) else "") for m in p["models"][:12]]
            when = f" · listed {time.strftime('%Y-%m-%d %H:%M', time.localtime(p['models_at']))}" if p.get("models_at") else ""
            print(c("2", f"    {len(p['models'])} models{when}: " + ", ".join(shown) + (" …" if len(p["models"]) > 12 else "")))
    if any(config.trains_on_inputs(m, p) for p in v["providers"] for m in p["models"]):
        print(c("2", f"  ⚠ {config.TRAINING_WARNING}; `mse providers use <id> <model> --allow-training` confirms a model"))


def _print_models(ids, meta=None, provider=None):
    """A model list grouped by the prefix before the first '/' (deepseek/, anthropic/ …); ⚠ marks the models that
    the provider's training_models patterns match: its provider may use their inputs for training."""
    meta = meta or {}
    groups: dict = {}
    for m in ids:
        groups.setdefault(m.split("/", 1)[0] if "/" in m else "", []).append(m)
    for g, items in groups.items():
        if g or len(groups) > 1:
            print(c("1", f"  {g or 'other'}/" if g else "  (no prefix)"))
        for m in items:
            name = (meta.get(m) or {}).get("name")
            flag = c("33", "  ⚠ " + config.TRAINING_WARNING) if config.trains_on_inputs(m, provider) else ""
            print(f"    {m}" + (c("2", f"  {name}") if name else "") + flag)


def cmd_providers(a):
    """Cloud models for Ask. Goes through the daemon when it runs (so it uses a new key at once); otherwise works
    in this process, where an environment variable (ANTHROPIC_API_KEY, MSE_KEY_<ID>) also counts as a key. Keys are read with a hidden prompt or from stdin and never printed."""
    from . import providers
    act = a.paction or "list"
    up = daemon_up()
    api = client() if up else None
    known = config.provider_entries(config.load())
    pid = getattr(a, "id", None)
    if pid and act not in ("add", "use") and pid not in known and not (act == "models" and pid == "local"):
        sys.exit(f"error: no provider named '{pid}' (known: {', '.join(known)})")
    if act == "list":
        v = api._call("GET", "/api/ask/providers") if up else providers.list_view(allow_env=True)
        print(json.dumps(v, ensure_ascii=False, indent=2)) if getattr(a, "json", False) else _print_providers(v)
    elif act == "set-key":
        key = providers.clean_key(_read_secret(pid, a.from_env))
        if up:
            api._call("POST", "/api/ask/providers", body={"id": pid, "key": key})
        else:
            providers.set_key(pid, key)
        print(f"stored the key for {pid} in the login Keychain (service {providers.SERVICE}, account {pid})")
    elif act == "forget-key":
        if up:
            api._call("POST", "/api/ask/providers", body={"id": pid, "forget_key": True})
        else:
            providers.forget_key(pid)
        print(f"removed the key for {pid} from the Keychain")
    elif act == "test":
        r = api._call("POST", f"/api/ask/providers/{pid}/test", body={"model": a.model} if a.model else {}) if up \
            else providers.test(pid, a.model, allow_env=True)
        if not r["ok"]:
            sys.exit(f"{pid} {r.get('model')}: failed after {r.get('ms')} ms: {r.get('error')}")
        print(f"{pid} {r['model']}: ok ({r['ms']} ms)")
    elif act == "models":
        if pid == "local":
            v = api._call("GET", "/api/ask/local-models") if up else __import__("mse.ask", fromlist=["x"]).local_models()
            print(f"installed answer models (current: {v['current']}):")
            for m in v["models"]:
                print(f"  {'*' if m['current'] else ' '} {m['name']}  ({m['size_mb']} MB)")
            print("choose one: mse providers use local <name>")
            return
        if getattr(a, "cached", False):
            p = known[pid]
            r = {"fetched": len(p["models"]), "models": p["models"], "at": p.get("models_at")}
            print(f"{pid}: {r['fetched']} models (stored list)")
        else:
            r = api._call("POST", f"/api/ask/providers/{pid}/models", body={}) if up \
                else providers.refresh_models(pid, allow_env=True)
            print(f"{pid}: {r['fetched']} models fetched ({r['ms']} ms)")
        now = config.provider_entries(config.load())[pid]
        _print_models(r["models"], now.get("models_meta"), now)
    elif act == "connect":
        if not known[pid].get("key_file"):
            sys.exit(f"error: '{pid}' has no key file: mse providers add {pid} --key-file PATH")
        r = api._call("POST", f"/api/ask/providers/{pid}/connect", body={}) if up else providers.connect(pid, allow_env=True)
        print(f"connected {pid}: its key from {r['file']} is in the login Keychain "
              f"(service {providers.SERVICE}, account {pid}); this step is optional, MySearch also reads that file itself")
        if r.get("models_error"):
            print(f"but its models could not be listed: {r['models_error']}")
        else:
            print(f"{r['fetched']} models listed; see them with `mse providers models {pid} --cached`")
    elif act == "use":
        if pid == "local":
            if a.model:           # the model that answers on this Mac
                change = {"local_model": a.model, **({"route": a.route} if a.route else {})}
            else:
                change = {"route": "local"}
        else:
            if pid not in known:
                sys.exit(f"error: no provider named '{pid}' (known: {', '.join(known)}, or 'local')")
            change = {"provider": pid, **({"model": a.model} if a.model else {}),
                      **({"route": a.route} if a.route else {})}
            if a.allow_training:
                if not a.model:
                    sys.exit("error: --allow-training confirms one model: mse providers use <id> <model> --allow-training")
                now = (api._call("GET", "/api/ask/providers") if up else providers.list_view(allow_env=True))
                change["allow_training_models"] = sorted(set(now.get("allow_training_models") or []) | {a.model})
                print(c("33", f"⚠ {a.model}: {config.TRAINING_WARNING} (its inputs and outputs may be used for training)"))
        r = api._call("POST", "/api/settings", body={"ask": change}) if up else \
            {"ask": __import__("mse.ask", fromlist=["x"]).settings(config.update_settings({"ask": change}))}
        s = r["ask"]
        print(f"Ask: route {s['route']} · cloud model {s['provider']} {s['model']} · local model {Path(s['local_model']).name}")
    elif act == "add":
        if not a.base_url and pid not in known:
            sys.exit("error: --base-url is required for a new provider")
        body = {"id": pid, **({"kind": a.kind} if a.kind else {} if pid in known else {"kind": "openai-chat"}),
                **({"base_url": a.base_url} if a.base_url else {}), **({"label": a.label} if a.label else {}),
                **({"models": a.model_ids} if a.model_ids else {}),
                **({"key_file": a.key_file} if a.key_file is not None else {}),
                **({"training_models": a.training_models} if a.training_models else {}),
                **({"auto_models": True} if a.auto_models else {})}
        if a.key:
            body["key"] = providers.clean_key(_read_secret(pid, False))
        r = api._call("POST", "/api/ask/providers", body=body) if up else providers.save(body)
        print(f"{'updated' if pid in known else 'added'} {r['id']} ({r['kind']} · {r['base_url']}) · "
              + ("key ✓" if r["has_key"] else "no key" + (f": mse providers set-key {pid}" if r["needs_key"] else "")))
    elif act == "remove":
        r = api._call("POST", f"/api/ask/providers/{pid}/remove", body={"forget_key": a.forget_key}) if up \
            else providers.remove(pid, forget=a.forget_key)
        print(f"removed {pid}" + (" and its key" if r.get("key_forgotten") else "")
              + f" · Ask uses {r['provider']} {r['model']}")


def cmd_similar(a):
    from .mcp import format_results
    ref = str(Path(a.ref).expanduser().resolve()) if a.ref.startswith(("/", "~")) else a.ref
    own, scope = config.scope_tokens(a.collection), _scope(a.within)
    coll = own[0] if len(own) == 1 and config.get_collection(config.load(), own[0]) else None
    if own and not coll:                      # a group / list / all given as -c: look there
        scope = scope or _scope(own)
    res = client().similar(coll, ref, a.limit, scope=scope) if daemon_up() else \
        __import__("mse.search", fromlist=["x"]).similar(coll, ref, scope, limit=a.limit)
    if not res:
        sys.exit(f"not found: {a.ref}")
    print(agent_safe(json.dumps(res, ensure_ascii=False, indent=2)) if a.json else format_results(res))


def cmd_eval(a):
    from . import evaluate, feedback, learn
    colls = _expand(a.collection)
    gold = json.loads(Path(a.gold).read_text())["queries"] if a.gold else []
    if a.feedback:
        gold += feedback.gold()          # every query with a favourite; evaluated with feedback application off
    if a.auto:
        gold += learn.load_auto_gold()
    if not gold:
        sys.exit("nothing to evaluate: give a gold file, --feedback and/or --auto")
    res = evaluate.run(gold, colls, a.configs.split(","), verbose=a.verbose)
    print(c("2", f"{len(gold)} questions · feedback application off (config 'favourites' turns it on)"))
    print(f"{'config':14s} {'hit@1':>6s} {'hit@3':>6s} {'hit@10':>7s} {'MRR':>6s} {'ms/q':>6s}")
    for name, r in res.items():
        print(f"{name:14s} {r['hit@1']:6.3f} {r['hit@3']:6.3f} {r['hit@10']:7.3f} {r['mrr@10']:6.3f} {r['ms_per_query']:6d}")
        if a.verbose:
            for q, rank, top in r["misses"]:
                print(c("2", f"     miss {rank}: {q}  → {top}"))


def _progress(stage, done, total, msg):
    print(f"\r{stage}: {msg or ''}" + " " * 20, end="", file=sys.stderr, flush=True)


def _ref(r: str) -> str:
    return str(Path(r).expanduser().resolve()) if r.startswith(("/", "~")) else r


def cmd_fav(a):
    """Mark a file as the right answer (or noise, --hide) for a query, or take the mark back (--remove)."""
    from . import feedback
    ref, up = _ref(a.ref), daemon_up()
    if a.remove:
        body = {"query": a.query, "collection": a.collection, "path": ref}
        n = client()._call("POST", "/api/feedback/remove", body=body)["removed"] if up else \
            feedback.remove(query=a.query, collection=a.collection, ref=ref)
        print(f"removed {n} mark(s) for \"{a.query}\"")
        return
    kind = "hide" if a.hide else "best"
    try:
        fid = client()._call("POST", "/api/feedback", body={"query": a.query, "collection": a.collection,
                                                             "path": ref, "kind": kind})["id"] if up else \
            feedback.add(a.query, a.collection, ref, kind)
    except KeyError as e:
        sys.exit(f"error: {e.args[0] if e.args else e}")
    what = "hidden from" if a.hide else "★ marked as the best result for"
    print(f"{a.ref} {what} \"{a.query}\"  (feedback #{fid})")


def cmd_favs(a):
    from . import feedback
    items = client()._call("GET", "/api/feedback", {"q": a.q} if a.q else None)["items"] if daemon_up() else \
        feedback.list_items(a.q)
    if a.json:
        print(agent_safe(json.dumps(items, ensure_ascii=False, indent=2)))
        return
    if not items:
        print("no favourites yet: `mse fav \"<query>\" <#docid|path>`")
        return
    for it in items:
        mark = c("1;33", "★ best") if it["kind"] == "best" else c("2", "⊘ hide")
        used = f"used {it['uses']}×" + (f", last {time.strftime('%Y-%m-%d', time.localtime(it['last_used']))}"
                                         if it.get("last_used") else "")
        print(agent_safe(f"{c('2', '#' + str(it['id'])):>5} {mark}  \"{it['query']}\"  →  {c('1;36', it['path'])}"
                         f"  {c('2', '[' + it['collection'] + '] #' + (it['docid'] or '?') + ' · ' + used)}"))


def _settings_line(label: str, s: dict, scored: dict | None, names: list[str]) -> str:
    cols = f"{label:10s} {s['keyword']:7.2f} {s['translated']:10.2f} {s['name']:5.2f} {s['hub_penalty']:5.2f}" \
           f" {s['rerank_gate']:5.2f} {s['rerank_weight']:5.2f}"
    if scored:
        cols += f" {scored['weighted_mrr']:6.3f}" + "".join(
            f" {scored['sets'][n]['mrr']:>{max(7, len(n))}.3f}" if scored["sets"][n]["n"] else f" {'—':>{max(7, len(n))}}"
            for n in names)
    return cols


def cmd_tune(a):
    from . import evaluate, learn
    if a.rollback:
        e = learn.rollback()
        print(f"rolled back: {e['reason']}")
        print("now: " + ", ".join(f"{k} {v}" for k, v in e["settings"].items()))
        return
    colls = _expand(a.collection)
    sets = evaluate.load_sets(colls, gold_files=a.gold or None)
    names = [n for n in sets if sets[n]["queries"]]
    parts = [f"{n} {len(s['queries'])}" + (f" ({s['skipped']} not indexed here)" if s["skipped"] else "")
             for n, s in sets.items()]
    print(f"tuning on {sum(len(s['queries']) for s in sets.values())} questions: " + " · ".join(parts))
    t0 = time.time()
    t = evaluate.tune(sets, progress=_progress)
    print("\r" + " " * 60 + "\r", end="", file=sys.stderr)
    print(c("2", f"{t['evaluated']} settings evaluated in {round(time.time() - t0)} s · re-ranker "
                 f"{'on (gate and weight tuned)' if t['rerank'] else 'off in config (its knobs are not tuned)'}"
                 f" · feedback application off"))
    head = f"{'':10s} {'keyword':>7s} {'translated':>10s} {'name':>5s} {'hub':>5s} {'gate':>5s} {'rr-w':>5s} {'wMRR':>6s}" \
           + "".join(f" {n:>{max(7, len(n))}s}" for n in names)
    print("\n" + c("1", head) + c("2", "   (per set: MRR@10)"))
    print(_settings_line("current", t["current"], t["base"], names))
    print(_settings_line("best", t["candidate"], t["cand"], names))
    print(c("2", "\ntop settings by weighted MRR (✓ passes the gold/favourites safety checks):"))
    for sc in t["table"][:a.top]:
        mark = "✓" if sc["safe"] else "✗"
        print(c("2", _settings_line(f"  {mark}{' rr' if sc['rerank'] else ''}", sc["settings"], sc, names)))
    if not t["changed"]:
        print("\nverdict: the current settings are already the best on this grid")
        return
    ok = not t["reasons"]
    print("\nverdict: " + ("the guardrails allow this change" if ok else "the nightly job would keep the current "
                                                                         "settings — " + "; ".join(t["reasons"])))
    if a.apply:
        learn.record_manual(t)
        print(c("1;32", "applied to config (search.weights, hub_penalty, rerank_gate, rerank_weight);"
                        " undo with `mse tune --rollback`"))
    else:
        print(c("2", "nothing written: `mse tune --apply` writes the best settings to config"))


def cmd_learn(a):
    from . import learn
    colls = _expand(a.collection)
    r = learn.run(progress=_progress, apply=not a.dry_run, collections=colls)
    print("\r" + " " * 60 + "\r", end="", file=sys.stderr)
    _print_quality(learn.history(1), detail=True)
    print(f"\n{r['action']}: {r['reason']}  ({r['evaluated']} settings, {r['duration_s']} s)")


def _print_quality(entries: list[dict], detail: bool = False):
    names = []
    for e in entries:
        names += [n for n in (e.get("sets") or {}) if n not in names]
    print(c("1", f"{'when':16s} {'kind':8s} {'action':11s} {'n':>4s} {'wMRR':>6s}"
                 + "".join(f" {n:>{max(13, len(n))}s}" for n in names)) + c("2", "   (per set: MRR / hit@1)"))
    for e in entries:
        sets = e.get("sets") or {}
        cols = "".join(f" {(str(round(sets[n]['mrr'], 3)) + ' / ' + str(round(sets[n]['hit@1'], 3))) if sets.get(n, {}).get('n') else '—':>{max(13, len(n))}s}"
                       for n in names)
        wm = f"{e['weighted_mrr']:6.3f}" if e.get("weighted_mrr") is not None else f"{'':6s}"
        print(f"{e['when'][:16].replace('T', ' '):16s} {e.get('kind', ''):8s} {e.get('action', ''):11s}"
              f" {e.get('n', '') or '':>4} {wm}{cols}")
        if detail or e.get("action") in ("applied", "rolled back"):
            tn = e.get("tune") or {}
            if tn:
                print(c("2", f"{'':16s} tuned: {tn.get('evaluated')} settings, best wMRR {tn.get('weighted_mrr')}"
                             + ("" if tn.get("changed") else " (= current)")))
            if e.get("reason"):
                print(c("2", f"{'':16s} {e['reason']}"))
            if e.get("previous") and e.get("action") in ("applied", "rolled back"):
                new = e.get("applied") or e.get("settings") or {}
                diff = [f"{k} {e['previous'][k]} → {new[k]}" for k in new if e["previous"].get(k) != new.get(k)]
                print(c("2", f"{'':16s} changed: {', '.join(diff) or 'nothing'}"))


def cmd_quality(a):
    from . import evaluate, learn
    entries = learn.history(a.n)
    if a.json:
        print(json.dumps({"entries": entries, "current": evaluate.current_settings()}, ensure_ascii=False, indent=2))
        return
    if not entries:
        print("no quality history yet: `mse learn` (or wait for the night run)")
        return
    _print_quality(entries)
    cur = evaluate.current_settings()
    print(c("2", "\nnow: " + ", ".join(f"{k} {v}" for k, v in cur.items())))


def cmd_finetune(a):
    """Re-ranker learning from favourites (finetune.py). status reads files only; run trains in this process
    (a few minutes, on AC power) — the night job does the same in its own low-priority process."""
    from . import finetune
    finetune.main([a.action] + ([a.id] if a.id else []) + (["--dry-run"] if a.dry_run else [])
                  + (["--force"] if a.force else []) + (["--json"] if a.json else []))


def cmd_get(a):
    from .mcp import format_doc
    d = client().get(a.collection, a.ref, a.max, a.page) if daemon_up() else __import__("mse.search", fromlist=["x"]).get_document(a.collection, a.ref, a.max, a.page)
    if not d:
        sys.exit(f"not found: {a.ref}")
    print(agent_safe(json.dumps(d, ensure_ascii=False, indent=2)) if a.json else format_doc(d))


def cmd_ls(a):
    from . import search
    for f in search.list_files(a.collection, a.prefix or "", a.limit):
        tag = c("2", " [metadata only]") if f["mode"] == "meta" else ""
        print(f"{f['path']}  {c('2', '#' + f['docid'])}{tag}")


def cmd_list(a):
    from .server import collections_summary
    for col in collections_summary():
        s = col.get("stats") or {}
        flag = "" if col["in_global"] else c("2", " (not in global search)")
        print(f"{c('1;36', col['name'])}{flag}  {col['path']}")
        if col.get("aliases"):
            print(c("2", f"   aliases: {', '.join(col['aliases'])}"))
        if col.get("description"):
            print(c("2", f"   {col['description']}"))
        if s:
            print(c("2", f"   {s['files']} files · {human_size(s['bytes'])} · {s['items']} items · "
                         f"{s['embed_pending']} to embed · {s['heavy_pending']} awaiting OCR/media · last scan {s['last_scan']}"))
    groups = config.load().get("groups") or []
    if groups:
        print(c("2", "groups: " + " · ".join(
            f"{g['name']} ({', '.join(g['collections']) or 'empty'}"
            + (f"; aliases: {', '.join(config.aliases_of(g))}" if config.aliases_of(g) else "") + ")" for g in groups)))


def cmd_add(a):
    coll = config.add_collection(a.path, a.name, **({"description": a.description} if a.description else {}),
                                 **({"in_global": False} if a.no_global else {}))
    print(f"added {coll['name']} → {coll['path']}")
    if daemon_up():
        client()._call("POST", "/api/sync", body={"collection": coll["name"], "kind": "scan"})
        print("searchable by name now; contents indexing in background (OCR, page images and media run tonight "
              "or on `mse sync`)")
    else:
        print("the service is not running: start it and the folder is indexed by its next scan "
              "(or run `mse sync " + coll["name"] + "` then)")
    from . import indexer
    print(_survey_text(indexer.survey(coll)))
    if a.agents:
        from . import agents_setup
        print("agents (locked to this folder's collection):")
        _print_files(agents_setup.install(coll["name"]), Path(coll["path"]))


def _survey_text(s: dict) -> str:
    """The counts `mse add` prints: what the first pass skips, and what it keeps by name only."""
    lines = [f"  {s['entries']:,} files and folders found; {s['name_only']:,} are searchable by name only"]
    if s["pruned_folders"]:
        top = ", ".join(f"{n} {c:,}" for n, c in s["pruned"].items())
        lines.append(f"  {s['pruned_folders']:,} folders skipped by rule ({top})")
    if s["gitignored"]:
        lines.append(f"  {s['gitignored']:,} entries left out by .gitignore")
    if s["code_name_only"]:
        lines.append(f"  {s['code_name_only']:,} code files by name (indexing.index_code is off)")
    if s["content_name_only"]:
        lines.append(f"  {s['content_name_only']:,} disk images, large logs and databases by name")
    if s["bundles"]:
        lines.append(f"  {s['bundles']:,} app bundles by name")
    return "\n".join(lines)


def cmd_remove(a):
    from . import agents_setup
    root = Path(config.get_collection(config.load(), a.name)["path"]) if config.get_collection(config.load(), a.name) \
        else None
    if a.agents and root:                     # before the collection goes (works with any running daemon)
        print("agents:")
        _print_files(agents_setup.remove(a.name), root)
    elif root and any(r["state"] == "connected" for r in agents_setup.status(a.name)):
        print(c("33", f"note: agent configs in {root} still start MySearch locked to this folder; they will fail "
                      f"closed. Take them back with `mse agents remove {root}`."))
    if daemon_up():
        r = client()._call("POST", f"/api/collections/{a.name}/remove", body={})
    else:
        from . import indexer
        config.remove_collection(a.name)
        r = {"index_moved_to": indexer.drop_collection_data(a.name)}
    print(f"removed {a.name}; its index was moved to {r['index_moved_to']} (your files are untouched)")


def cmd_set(a):
    changes = {}
    for kv in a.settings:
        k, _, v = kv.partition("=")
        if v.lower() in ("true", "false"):
            v = v.lower() == "true"
        changes[k] = v
    if a.exclude:
        coll = config.get_collection(config.load(), a.name)
        changes["exclude"] = coll["exclude"] + a.exclude
    if a.metadata_only:
        coll = config.get_collection(config.load(), a.name)
        changes["metadata_only"] = coll["metadata_only"] + a.metadata_only
    coll = config.update_collection(a.name, changes)
    print(json.dumps({k: coll[k] for k in changes}, indent=2, ensure_ascii=False))


def cmd_context(a):
    coll = config.get_collection(config.load(), a.name)
    ctx = dict(coll.get("contexts") or {})
    if a.action == "add":
        ctx[a.prefix] = a.text
    elif a.action == "rm":
        ctx.pop(a.prefix, None)
    if a.action != "list":
        config.update_collection(a.name, {"contexts": ctx})
    for k, v in ctx.items():
        print(f"{k}: {v}")


def cmd_reprocess(a):
    """`mse sync <collection> --reocr | --retranscribe`: queue OCR (pdf, image) or transcription (audio, video) again for
    the files that already have it. Nothing is read here: the marks wait for the night run, and --now queues a sync."""
    from . import indexer
    tokens = config.scope_tokens(a.collection)
    names = config.resolve_scope(config.load(), tokens)["collections"] if tokens and tokens != ["all"] else []
    if len(names) != 1:
        sys.exit("name one collection with --reocr or --retranscribe (not 'all', a group or a list)")
    name = names[0]
    coll = config.get_collection(config.load(), name)
    if not coll:
        sys.exit(f"no collection named {name}")
    known_types = {"pdf", "image", "audio", "video"}
    wanted = {t.strip() for t in a.types.split(",") if t.strip()} if a.types else known_types
    if wanted - known_types:
        sys.exit(f"--types: unknown {', '.join(sorted(wanted - known_types))} (use pdf, image, audio, video)")
    allowed = ({"pdf", "image"} if a.reocr else set()) | ({"audio", "video"} if a.retranscribe else set())
    types = wanted & allowed
    if not types:
        sys.exit("nothing to do: --reocr redoes pdf and image, --retranscribe redoes audio and video")
    prefix = None
    if a.path:
        root = Path(coll["path"]).resolve()
        try:
            rel = Path(a.path).expanduser().resolve().relative_to(root).as_posix()
        except ValueError:
            sys.exit(f"--path must be a folder inside {name}")
        prefix = None if rel == "." else rel
    res = indexer.mark_reprocess(name, types, prefix)
    counts = ", ".join(f"{k} {v}" for k, v in sorted(res["by_type"].items())) or "none"
    print(f"marked {res['marked']} file(s) in {name} ({counts}); {res['already_queued']} already queued, "
          f"{res['nothing_to_redo']} with nothing to redo. Nothing was read.")
    if not a.now:
        print("the night run does the work; add --now to queue a sync instead")
        return
    if res["marked"] == 0:
        print("nothing new to queue")
        return
    if not daemon_up():
        sys.exit("marked, but the daemon is not running: start it, then run the same command with --now")
    client()._call("POST", "/api/sync", body={"collection": name, "kind": "sync"})
    print(f"queued a sync of {name}; the marked files are redone with it")


def cmd_forget_missing(a):
    """`mse forget-missing <collection> [--path <folder>] [--dry-run] [--yes]`: remove from the index the files that are gone
    from disk and marked missing, with their text, vectors, thumbnails and favourites. Files on disk are never touched and
    present files are never removed. The list is printed first; removal needs a typed yes, or --yes. It writes the index
    directly, as `mse sync --reocr` does, so it works while the daemon runs an older version."""
    from . import indexer
    coll = config.get_collection(config.load(), a.collection)
    if not coll:
        sys.exit(f"no collection named {a.collection}")
    name = coll["name"]
    prefix = ""
    if a.path:
        root = Path(coll["path"]).resolve()
        try:
            rel = Path(a.path).expanduser().resolve().relative_to(root).as_posix()
        except ValueError:
            sys.exit(f"--path must be a folder inside {name} (absolute, or relative to the current folder)")
        prefix = "" if rel == "." else rel
    where = f" under {prefix}" if prefix else ""
    listed = indexer.forget_missing(name, prefix, dry_run=True)
    n = listed["count"]
    if n == 0:
        print(f"nothing to forget in {name}{where}: no missing files there")
        return
    print(f"{n:,} missing file(s) in {name}{where} would leave the index (rows, text, vectors, thumbnails, favourites):")
    for p in listed["paths"]:
        print(f"  {p}")
    if n > len(listed["paths"]):
        print(f"  ... and {n - len(listed['paths']):,} more")
    if a.dry_run:
        print("dry run: nothing was removed")
        return
    if not a.yes:
        try:
            answer = input(f"Remove these {n:,} rows from the index? Type yes to continue: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            sys.exit("not removed: type yes, or pass --yes")
    done = indexer.forget_missing(name, prefix, dry_run=False, as_of=listed["as_of"])
    print(f"removed {done['removed']:,} row(s) from {name}; favourites removed: {done['favourites_removed']}. "
          "Nothing on disk was touched.")
    if done.get("favourites_error"):
        print(f"favourites not all cleaned: {done['favourites_error']}")


def cmd_sync(a):
    if a.reocr or a.retranscribe:
        return cmd_reprocess(a)
    tokens = config.scope_tokens(a.collection)
    # a group / comma list syncs each member; "all" (or nothing) syncs every enabled collection
    names = [None] if not tokens or tokens == ["all"] or a.path else \
        config.resolve_scope(config.load(), tokens)["collections"]
    if a.path and tokens:
        names = [tokens[0]]
    if not daemon_up():
        sys.exit("the daemon is not running: `mse service install` (or `mse serve` in a terminal)")
    jobs = []
    for name in names:
        body = {"collection": name, "kind": a.kind}
        if a.path:
            body["path"] = str(Path(a.path).expanduser().resolve())
        jobs += [j for j in client()._call("POST", "/api/sync", body=body)["jobs"] if j not in jobs]
    r = {"jobs": jobs}
    print("queued:", ", ".join(f"{j['kind']} {j['collection'] or 'all'}" + (f" {j['path']}" if j.get("path") else "")
                              for j in r["jobs"]) or "nothing (empty group)")
    if a.wait:
        while True:
            st = client()._call("GET", "/api/status")["jobs"]
            cur = st["current"]
            if not cur and not st["queued"]:
                break
            if cur:
                print(f"\r{cur['kind']} {cur['collection']}: {cur.get('stage', '')} {cur.get('done', '')}/{cur.get('total', '')}   ",
                      end="", flush=True)
            time.sleep(2)
        print("\ndone")


def cmd_status(a):
    from . import service
    print(f"service: {service.status()}")
    if not daemon_up():
        print("daemon: not reachable")
        return
    st = client()._call("GET", "/api/status")
    print(f"daemon: pid {st['pid']} · {st['daemon_rss_mb']} MB · up {st['uptime_s']}s · model {st['model']}")
    for m, w in st["workers"].items():
        print(f"worker {m}: " + (f"running · {w['rss_mb']} MB · idle {w['idle_s']}s" if w["running"] else "stopped"))
    ask = st.get("ask") or {}
    if ask:
        state = "on" if ask.get("enabled") else "off"
        online = {True: "online", False: "offline"}.get(ask.get("online"), "not checked yet")
        nxt = f" → next answer: {ask.get('where_next')} ({ask.get('next_model')})" if ask.get("enabled") else ""
        fb = " · local fallback on" if ask.get("local_fallback") else ""
        print(f"ask: {state} · route {ask.get('route')}{fb} · cloud {ask.get('provider')} {ask.get('model')} ({online})"
              + nxt)
        if ask.get("next_problem") and ask.get("where_next") == "cloud":
            print(f"ask problem: the cloud model cannot answer now ({ask['next_problem']}); the question would end with an "
                  f"error, not load the local model. Choose a model: mse providers use <id> <model>")
        loaded = f" · loaded · {ask.get('ram_mb')} MB" if ask.get("loaded") else ""
        print(f"ask local model: {ask.get('local_model')}" + ("" if ask.get("available") else " (not installed)")
              + loaded)
    if "reranker_adapter" in st:
        print(f"re-ranker: {'adapter ' + st['reranker_adapter'] if st['reranker_adapter'] else 'base model'}")
    j = st["jobs"]
    if j["current"]:
        cur = j["current"]
        print(f"job: {cur['kind']} {cur['collection']} · {cur.get('stage', '')} {cur.get('done', '')}/{cur.get('total', '')}")
    print(f"queued: {len(j['queued'])} · next scan in {j['next_scan_in_s']}s · nightly at {j['nightly']['at']} "
          f"(last {j['nightly']['last_run']})")
    from .progress import format_line       # one line per folder: stage, done/total, ETA, errors, pending work
    for folder, entry in ((st.get("progress") or {}).get("folders") or {}).items():
        print(format_line(folder, entry))


def cmd_serve(a):
    from .server import serve
    serve(a.port)


def cmd_mcp(a):
    from .mcp import run_stdio
    run_stdio()


def cmd_ui(a):
    app = Path.home() / "Applications" / "MySearch.app"
    if app.exists():
        subprocess.run(["open", str(app)])
    else:
        # the web UI is served only with the token: the page URL carries it as t (the same way the app opens it)
        cfg = config.load()
        page = f"http://127.0.0.1:{cfg['port']}/?" + urlencode({"t": cfg["token"]})
        subprocess.run(["open", page])


def cmd_service(a):
    from . import service
    if a.action == "install":
        print("installed:", service.install())
        if "--daemon" in service.plist()["ProgramArguments"]:
            print("runs via MySearch.app (grant Full Disk Access to that app)")
    elif a.action == "uninstall":
        service.uninstall()
        print("uninstalled")
    elif a.action == "restart":
        print("restarted" if service.restart() else "restarted, but the daemon did not answer within 20 s (mse service status; log: ~/Library/Logs/MySearch/)")
    print(service.status())


def cmd_doctor(a):
    cfg = config.load()
    ok = lambda b: c("32", "ok") if b else c("31", "MISSING")
    print(f"data dir      {paths.DATA}")
    print(f"model         {ok(Path(cfg['model']['path']).exists())}  {cfg['model']['path']}")
    print(f"helper        {ok(paths.HELPER.exists())}  {paths.HELPER}")
    import shutil
    print(f"ffmpeg        {ok(bool(shutil.which('ffmpeg')))}  (audio/video)")
    for col in cfg["collections"]:
        readable = os.access(col["path"], os.R_OK)
        try:
            next(os.scandir(col["path"]))
            listable = True
        except Exception:
            listable = False
        print(f"collection    {col['name']}: {ok(readable and listable)}  {col['path']}")
    mi = cfg.get("mac_index") or {}
    if mi.get("enabled", True):
        from . import macindex
        feats = macindex.helper_features() if paths.HELPER.exists() else set()
        print(f"file names    {ok('crawl' in feats)}  whole-Mac name index: helper "
              f"{'can crawl' if 'crawl' in feats else 'is missing or too old (scripts/build_helper.sh)'}; roots {', '.join(mi.get('roots') or [])}")
    print(f"daemon        {ok(daemon_up())}  http://127.0.0.1:{cfg['port']}/")
    from . import setup_models
    for line in setup_models.report_lines():     # models (from the manifest), app, service, free disk, Full Disk Access
        print(line)


def cmd_setup(a):
    from . import setup_models
    if a.saction == "check":
        if a.json:
            print(json.dumps(setup_models.check_rows(), ensure_ascii=False, indent=2))
        else:
            print("\n".join(setup_models.report_lines()))
        return
    if a.events:     # the app's setup assistant starts this: one JSON line per event instead of text (setup_models.install_events)
        sys.exit(setup_models.install_events("full" if a.full else "minimal"))
    setup_models.install("full" if a.full else "minimal", dry_run=a.dry_run)


# ------------------------------------------------------------------ groups, scopes and agent connections

def _files_locked():
    """MSE_LOCK (an agent session limited to one folder): the collection it may search, or sys.exit when none."""
    from . import mcp
    lock = mcp.lock_from_env(os.environ.get("MSE_CWD") or os.getcwd())
    if lock is not None and lock.error:
        sys.exit(lock.error)
    return lock


def _cli_folder(x: str, roots: bool) -> str:
    """A folder typed on the command line -> what mac_index takes. `~` and absolute paths (and globs) go through as
    typed; a relative one (., ./x, sub/dir) is made absolute against the current folder, as `mse add` does. A bare word
    without a slash stays a name pattern for an exclusion (node_modules, *.tmp); a root is always a path. The settings
    validation (macindex.check_settings) has the last word either way."""
    x = x.strip()
    if x.startswith(("~", "/", "**/", "!", "#")):          # ("!" and "#" go to the validation, which refuses them)
        return x
    if roots or x in (".", "..") or "/" in x.rstrip("/"):
        return os.path.abspath(x)
    return x


def _files_manage(a, what: str, sub: str, args: list[str]):
    """mse files exclude add|rm|list <folder>...   mse files roots add|rm|list <folder>...
    Edits mac_index.exclude / mac_index.roots through config.update_settings, the same validation as
    POST /api/settings; the service reads the file itself, so nothing has to be running."""
    from . import macindex
    roots = what == "roots"
    key = "roots" if roots else "exclude"
    mi = {**macindex.DEFAULT_CONFIG, **(config.load().get("mac_index") or {})}
    if sub == "list":
        rows = ([{"entry": r, "kind": "folder", "path": r} for r in mi["roots"]] if roots
                else macindex.exclude_entries(mi))
        groups = macindex.hard_groups()
        if a.json:
            print(json.dumps({key: rows, **({} if roots else {"hard_excluded": groups})}, ensure_ascii=False, indent=2))
            return
        if roots:
            print("Searched by the whole-Mac name search:" if rows else "no roots")
        else:
            print("Excluded by you:" if rows else "nothing excluded by you")
        for r in rows:
            print(f"  {r['path'] or r['entry']}" + (c("2", f"   ({r['kind']})") if r["kind"] == "pattern" else "")
                  + (c("33", f"   {r['note']}") if r.get("note") else ""))
        if not roots:
            n = sum(len(g["items"]) for g in groups)
            print(c("2", f"Always excluded (built in, cannot be switched off): {n} rules in {len(groups)} groups"
                         + ("" if getattr(a, "all", False) else "; `mse files exclude list --all` shows them")))
            if getattr(a, "all", False):
                for g in groups:
                    print(f"  {g['label']}")
                    print(c("2", "    " + "  ".join(g["items"])))
        return
    if not args:
        sys.exit(f"usage: mse files {what} {sub} <folder>...")
    items = [_cli_folder(x, roots) for x in args]
    if roots and sub == "add":
        for x in items:
            if not os.path.isdir(os.path.expanduser(x)):
                sys.exit(f"error: not a folder: {x}")
    new, changed, same = macindex.edit_paths(mi[key], sub, items, roots=roots)
    if changed:
        try:
            saved = config.update_settings({"mac_index": {key: new}})["mac_index"][key]
        except ValueError as e:
            sys.exit(f"error: {e}")
    else:
        saved = mi[key]
    norm = macindex.normalize_roots if roots else macindex.normalize_excludes
    for x in changed:
        print(("added " if sub == "add" else "removed ") + ("root" if roots else "exclusion") + ": "
              + (norm([x])[0] if sub == "add" else x))
    for x in same:
        print(c("33", f"{'already in the list' if sub == 'add' else 'not in the list'}: {x}"))
    if sub == "add" and not roots:
        for x in changed:
            if "/" in x and not any(ch in x for ch in "*?[") and not os.path.exists(os.path.expanduser(x)):
                print(c("33", f"note: {x} does not exist right now; the exclusion applies if it appears later"))
    if changed:
        left = ("" if roots else " Names in skipped folders leave the results at once.")
        print(c("2", f"{len(saved)} {'roots' if roots else 'exclusions'} now."
                + (f" The service notices the change within seconds and re-indexes in the background.{left} "
                   "`mse files crawl` starts the re-index now."
                   if daemon_up() else " The service is not running; the change applies when it starts.")))


FILES_EXCLUDE_ROOTS = ("exclude: folders the name search skips (a full path, ~/Folder, or a name such as node_modules).\n"
                       "The built-in privacy rules cannot be changed. roots: where the search looks.")


def _files_rule_chunks() -> list[str]:
    """The query rules of `mse files --help`, copied from the module docstring of mse/filequery.py, where they are
    written down. Read at run time so the help cannot drift from it; tests/test_ignore_newline.py checks that each chunk
    is still in that docstring. Chunks: the query language table (globs and !exclude included), smart case, ranking."""
    from . import filequery
    doc = filequery.__doc__ or ""

    def para(start: str) -> str:
        i = doc.find(start)
        if i < 0:
            return ""
        j = doc.find("\n\n", i)
        return (doc[i:] if j < 0 else doc[i:j]).rstrip()

    return [ch for ch in (para("    invoice acme"), para("Smart case:"), para("Ranking:")) if ch]


def _files_query_rules() -> str:
    return "\n\n".join(_files_rule_chunks() or ["See mse/filequery.py for the query rules."]) + "\n\n" + FILES_EXCLUDE_ROOTS


def cmd_files(a):
    """File-name search over the whole Mac (instant, names only), its status, or a re-crawl.
    mse files "<query>" [--scope mac|indexed] [-c folders] [--offset N] [--json]   mse files status   mse files crawl [--force]
    mse files exclude add|rm|list <folder>...   mse files roots add|rm|list <folder>...
    Piped output masks personal identifiers like every agent-facing output. In a session locked with MSE_LOCK only
    --scope indexed in the locked folder works. `mse files search exclude add` searches for those words."""
    words = a.words
    action = "search"
    if words and words[0] in ("status", "crawl", "search") and (len(words) == 1 or words[0] == "search"):
        action, words = words[0], words[1:]
    elif (len(words) >= 2 and words[0] in ("exclude", "roots") and words[1] in ("add", "rm", "list")
          and (words[1] != "list" or len(words) == 2)):
        action = words[0]
    lock = _files_locked()
    if lock is not None and action != "search":
        sys.exit(f"this session is locked to '{lock.name}': only `mse files <query> --scope indexed` works")
    if action in ("exclude", "roots"):
        return _files_manage(a, words[0], words[1], words[2:])
    up = daemon_up()
    if action == "status":
        st = client()._call("GET", "/api/files/status") if up else None
        if st is None:
            from . import macindex
            st = macindex.manager().status()
        if a.json:
            print(json.dumps(st, ensure_ascii=False, indent=2))
            return
        fda = {True: "granted", False: "not granted (Desktop/Downloads are skipped)", None: "unknown"}[st["full_disk_access"]]
        print(f"whole-Mac file names: {st['state']} · {st['entries']:,} entries · roots {', '.join(st['roots'])}")
        if st.get("progress"):
            p = st["progress"]
            print(f"  crawling: {p.get('units_done')}/{p.get('units_total')} folders · {p.get('entries', 0):,} entries"
                  f" · {p.get('elapsed_s')} s · {p.get('current') or ''}")
        helper = ("missing (scripts/build_helper.sh)" if not st.get("helper_present") else
                  "OLD, without the crawler (scripts/build_helper.sh, then restart the service)" if st.get("helper_outdated")
                  else "ok")
        print(f"  watching changes: {'yes' if st.get('watching') else 'no'} · helper: {helper} · Full Disk Access: {fda}")
        if not st.get("searchable", True):
            print("  the name index is still being built: searches answer nothing until the first crawl has finished")
        if st.get("skipped_protected"):
            print("  skipped (macOS asks consent for these): " + ", ".join(st["skipped_protected"]))
            if st.get("full_disk_access") is False and st.get("python_real_path"):
                print("  to index them: System Settings > Privacy & Security > Full Disk Access > + > "
                      + st["python_real_path"] + "   (the program the service runs under)")
        if st.get("error"):
            print(c("31", f"  last error: {st['error']}"))
        return
    if action == "crawl":
        if not up:
            from . import service
            up = service.wait_ready(20)      # a service that was just (re)started is still loading
        if not up:
            sys.exit("the MySearch daemon is not running — it owns the index: mse service install (or mse serve)")
        print(json.dumps(client()._call("POST", "/api/files/crawl", body={"force": bool(a.force)})))
        return
    query = " ".join(words).strip()
    if not query:
        sys.exit("usage: mse files \"<query>\" [--scope mac|indexed] [-c folders] [--offset N] [--json] | status | crawl [--force]")
    offset = getattr(a, "offset", None)                 # None: not given (a namespace built by a caller may leave it out)
    if offset is not None and offset < 0:
        sys.exit("--offset must not be negative")
    scope, names = a.scope, config.scope_tokens(a.collection)
    if lock is not None:
        if scope == "mac":
            sys.exit(f"whole-Mac search is not available in a session locked to '{lock.name}' (MSE_LOCK); use --scope indexed")
        try:
            lock.check_folders(query)
            if names and not lock.only_mine(names):
                raise PermissionError(lock.refusal)
        except PermissionError as e:
            sys.exit(str(e))
        scope, names = "indexed", [lock.name]
    params = {"q": query, "scope": scope, "limit": a.limit}     # the daemon answers in the paged shape: it gets limit
    if offset is not None:
        params["offset"] = offset
    if names:
        params["c"] = ",".join(names)
    try:
        if up:
            res = client()._call("GET", "/api/files/search", params)
        else:
            from . import server
            if offset is None:
                res = server.files_search(query, scope, names, a.limit)
            else:       # paged, so has_more and next_offset come back; without --offset the call stays as it was
                res = server.files_search(query, scope, names, a.limit, offset, paged=True)
    except (ValueError, RuntimeError) as e:
        sys.exit(f"error: {e}")
    if a.json:
        print(agent_safe(json.dumps(res, ensure_ascii=False, indent=2)))
        return
    if a.paths:
        for it in res["items"]:
            print(agent_safe(it["path"]))
        return
    if res.get("searchable") is False:
        print(c("33", "the file-name index is not ready yet (first crawl in progress or not started): mse files status"))
    print(c("2", f"{len(res['items'])} of {res['total']}{'+' if res.get('truncated') else ''} · {res['ms']} ms · {res['scope']}"))
    for it in res["items"]:
        size = "" if it["is_dir"] else human_size(it["size"])
        when = time.strftime("%Y-%m-%d", time.localtime(it["mtime"])) if it.get("mtime") else ""
        mark = c("1;34", it["path"] + "/") if it["is_dir"] else c("1", it["path"])
        print(agent_safe(f"{mark}  {c('2', ' · '.join(x for x in (it['kind'], size, when, it.get('collection') or '') if x))}"))
    if res.get("has_more"):
        import shlex
        same = [f"-n {a.limit}"] + ([f"--scope {scope}"] if scope != "mac" else []) + ([f"-c {a.collection}"] if a.collection else [])
        print(agent_safe(c("2", f"more: mse files {shlex.quote(query)} {' '.join(same)} --offset {res['next_offset']}")))


def _scope(spec) -> list[str] | None:
    """-c / --in: collection names, group names, aliases and "all", comma separated -> validated names, aliases
    replaced by what they name (None = All)."""
    tokens = config.scope_tokens(spec)
    if not tokens:
        return None
    cfg = config.load()
    config.resolve_scope(cfg, tokens)                    # unknown names -> ValueError -> "error: ..."
    return list(dict.fromkeys(config.canonical_name(cfg, t) or t for t in tokens))


def _expand(spec) -> list[str] | None:
    """The same, expanded to collection names (for eval / tune / learn, which open collection databases)."""
    return config.resolve_scope(config.load(), spec)["collections"] if config.scope_tokens(spec) else None


def cmd_groups(a):
    from .server import groups_summary
    groups = groups_summary()
    if a.json:
        print(json.dumps(groups, ensure_ascii=False, indent=2))
        return
    if not groups:
        print("no groups yet: `mse group add work notes projects --label Work`")
        return
    for g in groups:
        extra = (f"  aliases: {', '.join(g['aliases'])}" if g.get("aliases") else "") + \
                ("  (no tab: mse group show " + g["name"] + ")" if g.get("hidden") else "")
        print(f"{c('1;36', g['name'])}  {g['label']}  {c('2', ' + '.join(g['collections']) or 'empty')}"
              f"  {c('2', str(g['files']) + ' files')}{c('2', extra)}")


def cmd_group(a):
    cfg = config.load()
    if a.gaction == "rm":
        try:
            config.remove_group(a.name)
        except KeyError as e:
            sys.exit(f"error: {e.args[0]}")
        print(f"removed group {a.name} (its folders and their indexes are untouched)")
        return
    if a.gaction == "list":
        return cmd_groups(a)
    if a.gaction in ("hide", "show"):              # the group's tab in the app; it still works by name and alias
        if not config.get_group(cfg, a.name):
            sys.exit(f"error: no group named '{a.name}'")
        config.save_group(None, None, a.name, hidden=a.gaction == "hide")
        print(f"group {a.name}: " + ("no tab of its own now (still searchable by name, alias and the scope menu)"
                                     if a.gaction == "hide" else "shown as a tab"))
        return
    name = config.slugify(a.name)
    existing = config.get_group(cfg, name)
    if a.gaction == "add" and existing:
        sys.exit(f"error: group '{name}' exists; change it with `mse group set {name} <collection>...`")
    if a.gaction == "set" and not existing:
        sys.exit(f"error: no group named '{name}'; create it with `mse group add {name} <collection>...`")
    label = a.label or (existing["label"] if existing else a.name)
    g = config.save_group(label, config.scope_tokens(a.collections), name)
    print(f"{'updated' if existing else 'added'} group {g['name']} ({g['label']}): {' + '.join(g['collections'])}")
    print(c("2", f"search it: mse search \"…\" -c {g['name']}"))


def cmd_alias(a):
    """Short names for folders and groups, for -c and the search shortcut ("w: invoice", "in:w")."""
    if a.aaction == "add":
        r = config.add_alias(a.alias, a.target)
        print(f"{r['alias']} → {r['kind']} {r['name']} (aliases: {', '.join(r['aliases'])})")
        print(c("2", f"search it: mse search \"{r['alias']}: …\"  or  mse search \"…\" -c {r['alias']}"))
        return
    if a.aaction == "rm":
        r = config.remove_alias(a.alias)
        print(f"removed {r['alias']} from the {r['kind']} {r['name']}")
        return
    cfg = config.load()
    rows = [("folder", e["name"], config.aliases_of(e)) for e in cfg["collections"]] + \
           [("group", e["name"], config.aliases_of(e)) for e in cfg.get("groups") or []]
    if a.json:
        print(json.dumps([{"kind": k, "name": n, "aliases": al} for k, n, al in rows], ensure_ascii=False, indent=2))
        return
    if not any(al for _, _, al in rows):
        print("no aliases yet: `mse alias add w work`")
        return
    for k, n, al in rows:
        if al:
            print(f"{c('1;36', ', '.join(al))}  → {k} {n}")


def _print_files(rows: list[dict], root: Path | None = None):
    for r in rows:
        f = r["file"]
        if root and f.startswith(str(root) + "/"):
            f = f[len(str(root)) + 1:]
        colour = {"added": "32", "updated": "32", "removed": "32", "skipped": "33", "kept": "33"}.get(r["action"], "2")
        print(f"  {c(colour, r['action']):8s} {r['agent']:24s} {f}" + (c("2", f"  — {r['note']}") if r.get("note") else ""))


def _print_status(rows: list[dict], root: Path | None = None):
    for r in rows:
        f = r["file"]
        if root and f.startswith(str(root) + "/"):
            f = f[len(str(root)) + 1:]
        mark = {"connected": c("32", "✓ connected"), "missing": c("2", "· missing"), "other": c("33", "≠ other"),
                "unreadable": c("31", "! unreadable")}[r["state"]]
        print(f"  {mark:12s} {r['agent']:24s} {f}" + (c("2", f"  — {r['note']}") if r.get("note") else ""))


def cmd_agents(a):
    """Which agents (Claude Code, Cursor, Codex, Grok, OpenCode, Claude Desktop, Antigravity) start MySearch where.
    Prints file names, server names and states only: these files can hold other servers' API keys."""
    from . import agents_setup
    cfg = config.load()
    if a.action == "status":
        out = {}
        targets = [a.target] if a.target else ([] if a.glob else [col["name"] for col in cfg["collections"]])
        for t in targets:
            root, _ = agents_setup.folder_of(t)
            out[t] = {"folder": str(root), "agents": agents_setup.status(t)}
        if a.glob or not a.target:
            out["global"] = {"agents": agents_setup.status_global()}
        if a.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return
        for t, v in out.items():
            print(c("1;36", t) + (c("2", f"  {v['folder']}") if v.get("folder") else c("2", "  user-level configs")))
            _print_status(v["agents"], Path(v["folder"]) if v.get("folder") else None)
        print(c("2", f"command: {agents_setup.mse_bin()} mcp"))
        return
    if a.glob:
        rows = agents_setup.install_global() if a.action == "install" else agents_setup.remove_global()
        print(c("1;36", "global") + c("2", "  user-level configs (no folder lock: the server scopes itself to the "
                                           "folder the agent works in)"))
        _print_files(rows)
        return
    if a.action == "install" and a.all:
        names = [col["name"] for col in cfg["collections"]]
    elif a.target:
        names = [a.target]
    else:
        sys.exit("error: name a collection (or a folder path), or pass --all / --global")
    for t in names:
        root, _ = agents_setup.folder_of(t)
        print(c("1;36", t) + c("2", f"  {root}"))
        _print_files(agents_setup.install(t) if a.action == "install" else agents_setup.remove(t), root)


def _doc_collections(spec) -> list[str]:
    """-c for `mse docs`: collections, groups, aliases and 'all' -> collection names (none = All)."""
    return config.resolve_scope(config.load(), _scope(spec))["collections"]


def _amounts(d: dict) -> str:
    return "  ".join(f"{v:,.2f} {c}" for c, v in d.items())


def cmd_docs(a):
    """`mse docs facets|timeline|meta|reindex`: what the documents are (type, date, amount, counterparty), read from the
    index. facets / timeline / meta read the databases directly; reindex goes through the daemon when it is running."""
    from . import docmeta, search, store
    if a.daction == "facets":
        res = docmeta.facets(_doc_collections(a.collection), a.type)
        if a.json:
            print(agent_safe(json.dumps(res, ensure_ascii=False, indent=2)))
            return
        print(c("2", f"{res['classified']:,} of {res['files']:,} files classified · {res['undated']:,} without a document date"))
        print(c("1", "types"))
        for r in res["doctypes"]:
            print(f"  {r['doctype']:15s} {r['count']:>6,}")
        print(c("1", "most frequent counterparties"))
        for r in res["counterparties"][:10]:
            print(f"  {agent_safe(r['name']):40s} {r['count']:>6,}")
        print(c("1", "per month"))
        for r in res["months"][-12:]:
            print(f"  {r['month']}  {r['count']:>6,}")
        return
    if a.daction == "timeline":
        res = docmeta.timeline(_doc_collections(a.collection), a.type, a.from_, a.to)
        if a.json:
            print(agent_safe(json.dumps(res, ensure_ascii=False, indent=2)))
            return
        what = ", ".join(res["types"]) or "all documents"
        print(c("2", f"{what} · {res['from'] or '-'} to {res['to'] or '-'} · {res['total']:,} dated, {res['undated']:,} undated"))
        for b in res["buckets"]:
            mark = c("33", "  none") if b["count"] == 0 else f"  {b['count']:>4,}  {_amounts(b['amounts'])}"
            print(f"  {b['month']}{mark}")
        if res["gaps"]:
            print(c("1", f"months without: {', '.join(res['gaps'])}"))
        return
    if a.daction == "meta":
        found = search.find_file(a.collection, a.ref)
        if not found:
            sys.exit(f"error: no file {a.ref}")
        coll, f = found
        conn = store.connect(coll["name"], readonly=True)
        try:
            row = docmeta.get(conn, f["id"])
        finally:
            conn.close()
        out = {"docid": f["docid"], "path": f["path"], "collection": coll["name"],
               "classified": bool(row and row.get("doctype")), "meta": row}
        print(agent_safe(json.dumps(out, ensure_ascii=False, indent=2)))
        return
    # reindex
    coll = config.get_collection(config.load(), a.collection)
    if not coll:
        sys.exit(f"error: no collection named {a.collection}")
    name = coll["name"]
    if daemon_up():
        info = client()._call("POST", "/api/docs/reindex", body={"collection": name})
        print(f"redoing the document facts of {name} in the daemon (batches of 200; the index stays usable)")
        while info.get("state") == "running":
            time.sleep(2)
            info = client()._call("GET", "/api/docs/reindex", {"collection": name})
            print(f"\r  {info.get('done', 0):,}/{info.get('total', 0):,}   ", end="", flush=True)
        print()
    else:
        info = docmeta.reindex(name, progress=lambda d, t: print(f"\r  {d:,}/{t:,}   ", end="", flush=True))
        print()
    if info.get("state") == "error":
        sys.exit(f"error: {info.get('error')}")
    print(f"{info.get('state')}: {info.get('done', 0):,} of {info.get('total', 0):,} files")


def _saved(ref: str) -> dict:
    """A saved search by id or by exact (case-insensitive) name."""
    items = config.saved_searches()
    hit = next((s for s in items if s["id"] == ref), None) or next((s for s in items if s["name"].lower() == ref.lower()), None)
    if not hit:
        sys.exit(f"error: no saved search '{ref}' (see `mse saved list`)")
    return hit


def cmd_saved(a):
    """`mse saved list|add|rm|run`: queries kept by name. rm removes the definition only, never a file."""
    if a.saction == "add":
        s = config.add_saved(a.name, a.query, a.collection)
        print(f"saved {s['id']}: {s['name']} = {s['query']}" + (f"  [{s['collection']}]" if s["collection"] else ""))
        print(c("2", f"run it: mse saved run {s['id']}"))
        return
    if a.saction == "rm":
        s = _saved(a.ref)
        config.remove_saved(s["id"])
        print(f"removed the saved search '{s['name']}' (no file or index was touched)")
        return
    if a.saction == "run":
        s = _saved(a.ref)
        ns = argparse.Namespace(query=s["query"], collection=s["collection"], limit=a.limit, mode="hybrid", type=None, after=None,
                                before=None, path=None, include=None, exclude=None, fast=a.fast, json=a.json, files=a.files)
        return cmd_search(ns)
    items = config.saved_searches()
    if a.json:
        print(agent_safe(json.dumps(items, ensure_ascii=False, indent=2)))
        return
    if not items:
        print('no saved searches yet: `mse saved add "Big invoices" "type:invoice amount:>500"`')
        return
    for s in items:
        print(f"{c('1;36', s['id'])}  {s['name']}  {c('2', s['query'])}" + (c("2", f"  [{s['collection']}]") if s["collection"] else ""))


def cmd_disk(a):
    """The whole-disk content index (mse/wholedisk.py): files found by what is IN them, e.g. "image of a zebra".
    mse disk status [--json] | mse disk search "<query>" [--kind image|pdf|doc|video|audio|code|other] [-n N] [--offset N] [--json]
    | mse disk rescan. Piped output masks personal identifiers like every agent-facing output. Not available in a session
    locked with MSE_LOCK (an agent session limited to one folder)."""
    import shlex
    from . import wholedisk
    lock = _files_locked()
    if lock is not None:
        sys.exit(f"whole-disk search is not available in a session locked to '{lock.name}' (MSE_LOCK)")
    up = daemon_up()
    if a.action == "status":
        st = client()._call("GET", "/api/disk/status") if up else wholedisk.manager().status()
        if a.json:
            print(json.dumps(st, ensure_ascii=False, indent=2))
            return
        print(f"whole-disk index: {st['state']}" + (f" ({st['paused_reason']})" if st.get("paused_reason") else "")
              + f" · {st['files_seen']:,} entries · {st['files_described']:,} described · {st['pending']:,} waiting")
        print(f"  {st['images_labelled']:,} pictures labelled · {st['pdfs_described']:,} PDFs read · roots {', '.join(st['roots'])}"
              + (f" · {st['rate_per_s']} per s" if st.get("rate_per_s") else "")
              + (f" · about {st['eta_s'] // 60} min left" if st.get("eta_s") else ""))
        if st.get("last_error"):
            print(c("33", f"  last error: {agent_safe(str(st['last_error']))}"))
        return
    if a.action == "rescan":
        if not up:
            sys.exit("the service is not running: start it, then run mse disk rescan")
        print(json.dumps(client()._call("POST", "/api/disk/rescan"), ensure_ascii=False))
        return
    query = " ".join(a.words).strip()
    if not query:
        sys.exit('usage: mse disk search "<query>" [--kind image|pdf|doc|video|audio|code|other] [-n N] [--offset N] [--json]')
    params = {"q": query, "limit": a.limit, "offset": a.offset or 0}
    if a.kind:
        params["kind"] = a.kind
    try:
        if up:
            res = client()._call("GET", "/api/disk/search", params)
        else:
            res = wholedisk.manager().search(query, a.kind, a.limit, a.offset or 0)
    except (ValueError, RuntimeError) as e:
        sys.exit(f"error: {e}")
    if a.json:
        print(agent_safe(json.dumps(res, ensure_ascii=False, indent=2)))
        return
    understood = " ".join("/".join(g) for g in res["query"]["words"]) or "no content words"
    kinds = ",".join(res["query"]["kinds"])
    print(c("2", f"{len(res['results'])} results · {res['took_ms']} ms · {understood}" + (f" · kind {kinds}" if kinds else "")))
    for it in res["results"]:
        when = time.strftime("%Y-%m-%d", time.localtime(it["mtime"])) if it.get("mtime") else ""
        size = human_size(it["size"]) if it.get("size") else ""
        meta = " · ".join(x for x in (it["kind"], size, when, ", ".join(it.get("labels") or [])[:80], it["why"]) if x)
        print(agent_safe(f"{c('1', it['path'])}  {c('2', meta)}"))
        if it.get("snippet"):
            print(agent_safe("    " + c("2", it["snippet"][:200])))
    if res.get("has_more"):
        print(agent_safe(c("2", f"more: mse disk search {shlex.quote(query)} -n {a.limit} --offset {res['next_offset']}")))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mse", description="MySearch — local multimodal search over your folders")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", aliases=["s"], help="search (hybrid by default)",
                       epilog="Shortcut: 'work: invoice', 'w,notes: invoice' or 'invoice in:work' in the query "
                              "picks the folder, group or alias and overrides -c.")
    s.add_argument("query")
    s.add_argument("-c", "--collection", help="collections, groups and/or aliases, comma separated (work,notes or projects), or 'all'")
    s.add_argument("-n", "--limit", type=int, default=10)
    s.add_argument("--mode", choices=["hybrid", "keyword", "semantic"], default="hybrid")
    s.add_argument("--type")
    s.add_argument("--after")
    s.add_argument("--before")
    s.add_argument("--path")
    s.add_argument("-i", "--include", help="files to include, comma-separated globs: '*.jpg' or 'Invoices/, *.pdf'")
    s.add_argument("-x", "--exclude", help="files to exclude, comma-separated globs: '*.md, *.eml'")
    s.add_argument("--fast", action="store_true", help="skip re-ranking close calls (default: re-rank)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--files", action="store_true", help="print only absolute paths")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("files", help="find files by NAME anywhere on the Mac, instantly: mse files \"<query>\" | status | crawl | "
                                     "exclude add|rm|list <folder> | roots add|rm|list <folder>",
                       epilog=_files_query_rules(), formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument("words", nargs="*", help="the query, or: status | crawl | search <query> | exclude|roots add|rm|list <folder>...")
    s.add_argument("--all", action="store_true", help="exclude list: also show the built-in rules")
    s.add_argument("--scope", choices=["mac", "indexed"], default="mac",
                   help="mac: every file under the configured roots (default); indexed: only your indexed folders")
    s.add_argument("-c", "--collection", help="with --scope indexed: folders, groups or aliases, comma separated")
    s.add_argument("-n", "--limit", type=int, default=20, help="results per page (default 20)")
    s.add_argument("--offset", type=int, default=None,
                   help="skip the first N results: the next page. A page with more results ends with the command for it")
    s.add_argument("--force", action="store_true", help="crawl: start over instead of resuming")
    s.add_argument("--json", action="store_true")
    s.add_argument("--paths", action="store_true", help="print only paths")
    s.set_defaults(fn=cmd_files)

    s = sub.add_parser("disk", help="files by what is IN them, across the Mac: mse disk search \"image of a zebra\" | "
                                    "status | rescan")
    s.add_argument("action", nargs="?", default="status", choices=["status", "search", "rescan"])
    s.add_argument("words", nargs="*", help="the query for search, e.g. \"Rechnung Telekom\" or \"photo of a beach\"")
    s.add_argument("--kind", choices=["image", "pdf", "doc", "video", "audio", "code", "other"],
                   help="only this kind of file (image, pdf, doc, video, audio, code, other)")
    s.add_argument("-n", "--limit", type=int, default=20, help="results per page (default 20)")
    s.add_argument("--offset", type=int, default=None, help="skip the first N results: the next page")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_disk)

    s = sub.add_parser("ask", help="a short answer with citations from your documents (Ask must be switched on)")
    s.add_argument("question")
    s.add_argument("-c", "--collection", help="collections and/or groups, comma separated, or 'all' "
                                              "(default: the folder you are in, else all; one answer reads one folder unless 'all')")
    s.add_argument("--follow-up", action="store_true",
                   help="a follow-up to the last `mse ask` (its last 3 turns go along; same scope unless -c)")
    where = s.add_mutually_exclusive_group()
    where.add_argument("--local", action="store_true", help="this question: the local model only (nothing leaves the Mac). Without it the local model is never loaded")
    where.add_argument("--cloud", action="store_true",
                       help="this question: the cloud model only (no local fallback; folders kept local stay local)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("providers", help="cloud models for Ask: list | set-key | connect | forget-key | test | use | "
                                         "models | add | remove")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_providers, paction=None)
    ps = s.add_subparsers(dest="paction")
    g = ps.add_parser("list", help="providers, keys (stored or not, never shown), status and models")
    g.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    g.set_defaults(fn=cmd_providers)
    g = ps.add_parser("set-key", help="store a provider's key in the Keychain (hidden prompt, or one line on stdin)")
    g.add_argument("id")
    g.add_argument("--from-env", action="store_true",
                   help="take it from ANTHROPIC_API_KEY / MSE_KEY_<ID> in this shell")
    g.set_defaults(fn=cmd_providers)
    g = ps.add_parser("forget-key", help="delete a provider's key from the Keychain")
    g.add_argument("id")
    g.set_defaults(fn=cmd_providers)
    g = ps.add_parser("test", help="one tiny request to check the key and the model (may cost a fraction of a cent)")
    g.add_argument("id")
    g.add_argument("--model", help="test this model instead of the active / first one")
    g.set_defaults(fn=cmd_providers)
    g = ps.add_parser("use", help="make a provider and model the active cloud model; `use local` = route Local only, "
                                  "`use local <model>` = the model that answers on this Mac")
    g.add_argument("id")
    g.add_argument("model", nargs="?")
    g.add_argument("--route", choices=["auto", "cloud", "local"])
    g.add_argument("--allow-training", action="store_true",
                   help="confirm a model its provider may use inputs of for training (see training_models; check its terms); refused otherwise")
    g.set_defaults(fn=cmd_providers)
    g = ps.add_parser("models", help="list every model a provider offers (fetches the list from its models endpoint; "
                                     "`models local`: the answer models installed on this Mac)")
    g.add_argument("id")
    g.add_argument("--cached", action="store_true", help="print the stored list without asking the provider")
    g.set_defaults(fn=cmd_providers)
    g = ps.add_parser("connect", help="a provider with a key file: read the key from that file, store it in the "
                                      "Keychain and list its models")
    g.add_argument("id")
    g.set_defaults(fn=cmd_providers)
    g = ps.add_parser("add", help="add or change an OpenAI-compatible provider (DeepSeek, OpenRouter, Ollama, a local "
                                  "router …)")
    g.add_argument("id")
    g.add_argument("--kind", choices=["openai-chat", "openai-responses"], default=None,
                   help="openai-chat (Chat Completions, the default for a new provider) or openai-responses")
    g.add_argument("--base-url", help="e.g. https://api.deepseek.com/v1 or http://127.0.0.1:11434/v1 "
                                      "(required for a new provider)")
    g.add_argument("--label")
    g.add_argument("--model", dest="model_ids", action="append", help="a model id (repeat for several)")
    g.add_argument("--key", action="store_true", help="then ask for its key (hidden) and store it in the Keychain")
    g.add_argument("--key-file", metavar="PATH", default=None,
                   help="read the key from this file when a call needs it (e.g. ~/.config/myrouter/key); "
                        "'' removes it. `mse providers connect <id>` copies it into the Keychain")
    g.add_argument("--training-models", metavar="PATTERN", action="append",
                   help="model ids (glob: * and ?) whose provider may use inputs for training, e.g. '*-train'; "
                        "repeat for several. Such a model needs --allow-training once")
    g.add_argument("--auto-models", action="store_true",
                   help="list the provider's models by itself the first time it is looked at")
    g.set_defaults(fn=cmd_providers)
    g = ps.add_parser("remove", help="remove a custom provider")
    g.add_argument("id")
    g.add_argument("--forget-key", action="store_true", help="also delete its key from the Keychain")
    g.set_defaults(fn=cmd_providers)

    s = sub.add_parser("similar", help="documents similar to a file (#docid or path)")
    s.add_argument("ref")
    s.add_argument("-c", "--collection", help="the file's collection; a group / comma list / 'all' = look there")
    s.add_argument("--in", dest="within", help="where to look: collections, groups or 'all' (default: the file's collection)")
    s.add_argument("-n", "--limit", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_similar)

    s = sub.add_parser("eval", help="score search quality on a gold set")
    s.add_argument("gold", nargs="?")
    s.add_argument("-c", "--collection", help="collections and/or groups, comma separated")
    s.add_argument("--configs", default="hybrid,keyword,semantic,no-bridge,no-collapse")
    s.add_argument("--feedback", action="store_true", help="add every query with a favourite as a question")
    s.add_argument("--auto", action="store_true", help="add the known-item questions of the last learn run")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_eval)

    s = sub.add_parser("tune", help="grid-search the ranking settings on all gold sets (writes only with --apply)")
    s.add_argument("-c", "--collection")
    s.add_argument("--gold", action="append", help="gold file(s) instead of the gold sets (eval/, see eval/README.md)")
    s.add_argument("--apply", action="store_true", help="write the best settings to config")
    s.add_argument("--rollback", action="store_true", help="restore the settings before the last applied change")
    s.add_argument("--top", type=int, default=5)
    s.set_defaults(fn=cmd_tune)

    s = sub.add_parser("learn", help="measure quality, re-tune, apply if the guardrails allow (the nightly job, now)")
    s.add_argument("-c", "--collection")
    s.add_argument("--dry-run", action="store_true", help="measure and tune, but never apply")
    s.set_defaults(fn=cmd_learn)

    s = sub.add_parser("quality", help="search-quality history (learn runs, tunes, rollbacks)")
    s.add_argument("-n", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_quality)

    s = sub.add_parser("finetune", help="re-ranker learning from favourites: status | run | rollback [id|none]")
    s.add_argument("action", choices=["status", "run", "rollback", "promote", "eval"])
    s.add_argument("id", nargs="?", help="adapter id (promote, rollback, eval); rollback 'none' = base model")
    s.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    s.add_argument("--force", action="store_true", help="run: ignore the gate; promote: ignore a refusal")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_finetune)

    s = sub.add_parser("fav", help="mark the right file for a query: mse fav \"<query>\" <#docid|path>")
    s.add_argument("query")
    s.add_argument("ref", help="#docid, relative or absolute path")
    s.add_argument("-c", "--collection")
    s.add_argument("--hide", action="store_true", help="hide this file for this query instead")
    s.add_argument("--remove", action="store_true", help="take the mark back")
    s.set_defaults(fn=cmd_fav)

    s = sub.add_parser("favs", help="list favourites and hidden results")
    s.add_argument("q", nargs="?", help="filter on query or path")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_favs)

    s = sub.add_parser("get", help="print a document's extracted text (#docid or path)")
    s.add_argument("ref")
    s.add_argument("-c", "--collection")
    s.add_argument("--page", type=int)
    s.add_argument("--max", type=int, default=20000)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_get)

    s = sub.add_parser("ls", help="list indexed files of a collection")
    s.add_argument("collection")
    s.add_argument("prefix", nargs="?")
    s.add_argument("-n", "--limit", type=int, default=200)
    s.set_defaults(fn=cmd_ls)

    sub.add_parser("list", help="list collections").set_defaults(fn=cmd_list)

    s = sub.add_parser("add", help="add a folder")
    s.add_argument("path")
    s.add_argument("--name")
    s.add_argument("--description")
    s.add_argument("--no-global", action="store_true", help="only searchable when picked explicitly")
    s.add_argument("--agents", action="store_true",
                   help="also connect the folder's agents (Claude Code, Cursor, Codex, Grok, OpenCode), locked to it")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("remove", help="remove a folder from the index (files are never touched)")
    s.add_argument("name")
    s.add_argument("--agents", action="store_true", help="also take the mysearch entries out of the folder's agent configs")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("set", help="change a collection: mse set NAME in_global=false description=...")
    s.add_argument("name")
    s.add_argument("settings", nargs="*")
    s.add_argument("--exclude", action="append", help="add an exclude pattern")
    s.add_argument("--metadata-only", action="append", help="add a metadata-only pattern")
    s.set_defaults(fn=cmd_set)

    s = sub.add_parser("context", help="describe parts of a collection for agents")
    s.add_argument("action", choices=["list", "add", "rm"])
    s.add_argument("name")
    s.add_argument("prefix", nargs="?")
    s.add_argument("text", nargs="?")
    s.set_defaults(fn=cmd_context)

    s = sub.add_parser("sync", help="index now (default: everything incl. OCR, media, embeddings)")
    s.add_argument("collection", nargs="?", help="a collection, a group, a comma list or 'all' (default: all)")
    s.add_argument("--path", help="one file (with --reocr / --retranscribe: a folder inside the collection)")
    s.add_argument("--kind", choices=["scan", "sync", "embed", "vocab", "learn"], default="sync")
    s.add_argument("--wait", action="store_true")
    s.add_argument("--reocr", action="store_true",
                   help="redo OCR on the PDFs and images that already have it (marks only; the night run does the work)")
    s.add_argument("--retranscribe", action="store_true",
                   help="redo transcripts of the audio and video that already have them (marks only)")
    s.add_argument("--types", help="pdf,image,audio,video: narrow what --reocr / --retranscribe redo")
    s.add_argument("--now", action="store_true",
                   help="with --reocr / --retranscribe: queue a sync now instead of waiting for the night run")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("forget-missing", help="remove from the index the files that are gone from disk (held by the "
                       "mass-vanish guard): mse forget-missing <collection> [--path <folder>] [--dry-run] [--yes]")
    s.add_argument("collection", help="the collection to clean")
    s.add_argument("--path", help="only under this folder (absolute, or relative to the current folder; it may be gone)")
    s.add_argument("--dry-run", action="store_true", help="list what would be removed; remove nothing")
    s.add_argument("--yes", action="store_true", help="remove without asking (the list is still printed first)")
    s.set_defaults(fn=cmd_forget_missing)

    sub.add_parser("status", help="daemon, workers, jobs").set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="check the installation").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("setup", help="models for a fresh install: setup models [--minimal|--full] [--dry-run] | setup check")
    ss = s.add_subparsers(dest="saction", required=True)
    g = ss.add_parser("models", help="download the models of a tier; installed folders are skipped")
    t = g.add_mutually_exclusive_group()
    t.add_argument("--minimal", action="store_true", help="text search (default)")
    t.add_argument("--full", action="store_true", help="also speech to text and the local Ask model")
    g.add_argument("--dry-run", action="store_true", help="print the plan; download and change nothing")
    g.add_argument("--events", action="store_true", help=argparse.SUPPRESS)
    g = ss.add_parser("check", help="which models are installed, the app, the service, free disk (read-only)")
    g.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("serve", help="run the daemon in the foreground")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("mcp", help="MCP server on stdio (for Claude Code and other agents)").set_defaults(fn=cmd_mcp)
    sub.add_parser("ui", help="open the app").set_defaults(fn=cmd_ui)
    s = sub.add_parser("service", help="background service (launchd)")
    s.add_argument("action", choices=["install", "uninstall", "restart", "status"])
    s.set_defaults(fn=cmd_service)

    # ---- groups of folders searched together, and agent connections
    s = sub.add_parser("groups", help="list groups of folders (search one with -c <group>)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_groups)
    s = sub.add_parser("group", help="mse group add|set <name> <collection>... [--label] | mse group rm <name>")
    gs = s.add_subparsers(dest="gaction", required=True)
    for act, hlp in (("add", "create a group"), ("set", "replace a group's folders (and --label)")):
        g = gs.add_parser(act, help=hlp)
        g.add_argument("name", help="group name (a slug; a free text name is slugified and kept as the label)")
        g.add_argument("collections", nargs="+", help="collection names (spaces or commas)")
        g.add_argument("--label", help="display name, e.g. Work")
        g.set_defaults(fn=cmd_group)
    g = gs.add_parser("rm", help="delete a group (its folders are untouched)")
    g.add_argument("name")
    g.set_defaults(fn=cmd_group)
    g = gs.add_parser("list", help="same as `mse groups`")
    g.add_argument("--json", action="store_true")
    g.set_defaults(fn=cmd_group)
    for act, hlp in (("hide", "no tab in the app (it still works by name, alias and in the scope menu)"),
                     ("show", "show the group as a tab again")):
        g = gs.add_parser(act, help=hlp)
        g.add_argument("name")
        g.set_defaults(fn=cmd_group)

    s = sub.add_parser("alias", help="short names: mse alias add w work | mse alias rm w | mse alias list")
    als = s.add_subparsers(dest="aaction", required=True)
    g = als.add_parser("add", help="give a folder or group a short name (for -c and 'w: …' in a search)")
    g.add_argument("alias")
    g.add_argument("target", help="the folder (collection) or group it stands for")
    g.set_defaults(fn=cmd_alias)
    g = als.add_parser("rm", help="take a short name back")
    g.add_argument("alias")
    g.set_defaults(fn=cmd_alias)
    g = als.add_parser("list", help="every alias and what it stands for")
    g.add_argument("--json", action="store_true")
    g.set_defaults(fn=cmd_alias)
    s = sub.add_parser("aliases", help="same as `mse alias list`")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_alias, aaction="list")

    s = sub.add_parser("agents", help="connect agents to folders: status | install <coll>|--all|--global | remove <coll>")
    s.add_argument("action", choices=["status", "install", "remove"])
    s.add_argument("target", nargs="?", help="collection name (or, for status/remove, a folder path)")
    s.add_argument("--all", action="store_true", help="install: every collection")
    s.add_argument("--global", dest="glob", action="store_true",
                   help="user-level configs: Claude Desktop, ~/.cursor, ~/.grok, ~/.codex, Antigravity")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_agents)

    s = sub.add_parser("docs", help="what documents are: facets | timeline | meta | reindex (type, date, amount, counterparty)")
    ds = s.add_subparsers(dest="daction", required=True)
    g = ds.add_parser("facets", help="counts per document type, top counterparties, documents per month")
    g.add_argument("-c", "--collection", help="collections, groups, aliases, or 'all' (default)")
    g.add_argument("--type", help="only these document types, comma separated (invoice,receipt)")
    g.add_argument("--json", action="store_true")
    g.set_defaults(fn=cmd_docs)
    g = ds.add_parser("timeline", help="documents per month with amount sums, and the months with none")
    g.add_argument("-c", "--collection", help="collections, groups, aliases, or 'all' (default)")
    g.add_argument("--type", help="document types, comma separated: mse docs timeline --type receipt")
    g.add_argument("--from", dest="from_", help="first month: 2025 or 2025-03 (default: the first month with a document)")
    g.add_argument("--to", help="last month: 2025 or 2025-12 (default: the last month with a document)")
    g.add_argument("--json", action="store_true")
    g.set_defaults(fn=cmd_docs)
    g = ds.add_parser("meta", help="the stored facts of one document (#docid or path)")
    g.add_argument("ref")
    g.add_argument("-c", "--collection")
    g.set_defaults(fn=cmd_docs)
    g = ds.add_parser("reindex", help="redo the document facts of a collection (after an update of the rules, or to start over)")
    g.add_argument("collection")
    g.set_defaults(fn=cmd_docs)

    s = sub.add_parser("saved", help="saved searches: list | add NAME QUERY | rm ID | run ID")
    ss_ = s.add_subparsers(dest="saction", required=True)
    g = ss_.add_parser("list", help="every saved search")
    g.add_argument("--json", action="store_true")
    g.set_defaults(fn=cmd_saved)
    g = ss_.add_parser("add", help='keep a query by name: mse saved add "Big invoices" "type:invoice amount:>500" -c work')
    g.add_argument("name")
    g.add_argument("query")
    g.add_argument("-c", "--collection", help="the folder, group or alias it searches (default: All)")
    g.set_defaults(fn=cmd_saved)
    g = ss_.add_parser("rm", help="remove a saved search (the definition only; no file is touched)")
    g.add_argument("ref", help="its id or name")
    g.set_defaults(fn=cmd_saved)
    g = ss_.add_parser("run", help="run a saved search")
    g.add_argument("ref", help="its id or name")
    g.add_argument("-n", "--limit", type=int, default=10)
    g.add_argument("--fast", action="store_true")
    g.add_argument("--json", action="store_true")
    g.add_argument("--files", action="store_true", help="print only absolute paths")
    g.set_defaults(fn=cmd_saved)
    s.set_defaults(fn=cmd_saved, saction="list", json=False)

    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except (KeyError, ValueError, RuntimeError) as e:
        sys.exit(f"error: {e}")
