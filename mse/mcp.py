"""MCP server (JSON-RPC 2.0) for agents. stdio transport via `mse mcp`, HTTP transport at POST /mcp.

Scope rule: an agent searches the collection that contains its working directory unless it asks for
another collection or for "all". A session working in one vault does not get results from another by
accident. `collection` also takes a saved group ("work") or a comma list ("work,notes").

Lock (stdio only): with MSE_LOCK=1 in the server's environment, the session may use ONLY the collection that
contains MSE_CWD (or the working directory). Other collections, "all", groups or lists naming anything else, and
paths outside it are refused (a group or list containing only the locked collection is fine), and
when the folder is in no collection at all every tool call fails (fail closed). The HTTP /mcp endpoint has no
working directory and is not locked.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, config

PROTOCOL = "2025-06-18"

INSTRUCTIONS = (
    "MySearch is a local search engine over the user's indexed folders (documents, PDFs incl. scanned pages, "
    "Office files, e-mails, images, audio and video). Use `search` first, with natural language or exact terms; "
    "English queries also find German documents. Then `get` a result by its #docid or path to read its text. "
    "Results default to the collection of your working directory; pass collection='all' only when the user wants "
    "everything, or a group name / comma list (e.g. 'work,notes') to search several folders together "
    "(`collections` lists the folders and groups). Some files are metadata-only by the user's rules: their contents are deliberately not indexed. "
    "Results marked ★ favourite are files the user picked as the right answer for that query. When the user "
    "confirms which document answered their question, record it with `mark_best` so it comes first next time. "
    "If the user has switched Ask on, `ask` returns a short answer with [n] citations written from the top results "
    "(by a cloud model while the Mac is online and the user allows it, otherwise by a local model); pass the "
    "earlier turns as `history` for a follow-up; check what matters with `get`."
)

TOOLS = [
    {
        "name": "search",
        "title": "Search indexed folders",
        "description": "Hybrid keyword + meaning search across documents, PDF pages, images, audio and video. "
                       "Returns ranked files with #docid, path, why it matched, page/time and a snippet. "
                       "Inline filters also work in the query: type:pdf, after:2025-03, before:2025-04, path:\"Invoices\". "
                       "A scope shortcut overrides `collection`: a folder, group or alias name before a colon at the "
                       "start ('work: invoice', 'a,b: invoice') or in:<name> anywhere ('invoice in:work').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for, in any language"},
                "collection": {"type": "string", "description": "Collection name, a group name, a comma list ('work,notes') or 'all'. Default: the collection containing the working directory"},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                "type": {"type": "string", "description": "pdf, text, office, email, image, audio, video, or a file extension"},
                "after": {"type": "string", "description": "Only documents dated on/after YYYY-MM[-DD]"},
                "before": {"type": "string", "description": "Only documents dated before YYYY-MM[-DD]"},
                "path": {"type": "string", "description": "Only paths starting with this prefix (relative to the collection)"},
                "include": {"type": "string", "description": "Files to include, comma-separated globs like VS Code: '*.jpg', 'Invoices/, *.pdf'"},
                "exclude": {"type": "string", "description": "Files to exclude, comma-separated globs: '*.md, *.eml, backups/'"},
                "mode": {"type": "string", "enum": ["hybrid", "keyword", "semantic"], "default": "hybrid"},
                "precise": {"type": "boolean", "default": True,
                            "description": "Re-check close calls with a cross-encoder (default on; ~0.3-0.5 s when it runs)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get",
        "title": "Read a document",
        "description": "Return the extracted text of one file (by #docid from search results, relative path, or absolute path).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "#docid, relative path or absolute path"},
                "collection": {"type": "string"},
                "page": {"type": "integer", "description": "Only this page (PDFs, slides)"},
                "max_chars": {"type": "integer", "default": 12000},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "multi_get",
        "title": "Read several documents",
        "description": "Return the text of several files at once (list of #docids or paths), each truncated to max_chars.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refs": {"type": "array", "items": {"type": "string"}},
                "collection": {"type": "string"},
                "max_chars": {"type": "integer", "default": 4000},
            },
            "required": ["refs"],
        },
    },
    {
        "name": "similar",
        "title": "Find similar documents",
        "description": "Files closest in meaning to a given file (#docid or path); for images and scans, visually similar ones.",
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string"},
                           "collection": {"type": "string", "description": "Where to look: the file's collection (default), a group, a comma list or 'all'"},
                           "limit": {"type": "integer", "default": 10}},
            "required": ["ref"],
        },
    },
    {
        "name": "collections",
        "title": "List collections",
        "description": "The indexed folders, what they contain (descriptions, path contexts), index health, and the saved groups of folders that can be searched together.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "mark_best",
        "title": "Mark the right result",
        "description": "Record that a file is the right answer for a query, so it is listed first the next time "
                       "this query is searched (and ranks higher for very similar ones). Call it only after the user "
                       "has confirmed which document was the right one; pass their query as they searched it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query, as searched"},
                "ref": {"type": "string", "description": "#docid from the search results, relative or absolute path"},
                "collection": {"type": "string", "description": "Collection of the file (default: the one containing the working directory, then any)"},
            },
            "required": ["query", "ref"],
        },
    },
    {
        "name": "sync",
        "title": "Re-index",
        "description": "Queue re-indexing: a whole collection (new/changed files, OCR, media, embeddings) or a single file path.",
        "inputSchema": {
            "type": "object",
            "properties": {"collection": {"type": "string", "description": "A collection, a group, a comma list or 'all' (default: all)"},
                           "path": {"type": "string"}},
        },
    },
    {
        "name": "ask",
        "title": "Ask a question about the documents",
        "description": "A short answer (2-5 sentences) written from the top search results only, with numbered "
                       "citations [n] that map to #docid, path and page. The user's Ask setting chooses the model: a "
                       "large cloud model while the Mac is online (personal identifiers masked before they leave), "
                       "the local model when offline or for folders kept local. Says so plainly when the documents "
                       "do not answer. Personal identifiers are masked in the answer. Needs Ask switched on in "
                       "MySearch Settings; when it is off, or when you need the full text, use `search` and `get`. "
                       "Verify important facts with `get` on the cited #docid.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question, in English or German"},
                "collection": {"type": "string", "description": "Collection name, a group name, a comma list or 'all'. Default: the collection containing the working directory. One answer reads one collection unless 'all' is given"},
                "history": {"type": "array", "maxItems": 3,
                            "description": "For a follow-up question: the earlier turns of this conversation, oldest "
                                           "first, at most 3, each {q: the question, a: the answer you got}. It "
                                           "shapes what is searched, never where",
                            "items": {"type": "object", "properties": {"q": {"type": "string"}, "a": {"type": "string"}},
                                      "required": ["q", "a"]}},
            },
            "required": ["question"],
        },
    },
]


# ---------------------------------------------------------------- backends

class LocalClient:
    """Inside the daemon: call the modules directly."""

    def search(self, q, colls, limit, filters, mode, precise=False):
        from . import search
        return search.search(q, collections=colls, limit=limit, filters=filters, mode=mode,
                             opts={"rerank": True} if precise else None)

    def get(self, coll, ref, max_chars, page):
        from . import search
        return search.get_document(coll, ref, max_chars=max_chars, page=page)

    def similar(self, coll, ref, limit, scope=None):
        from . import search
        return search.similar(coll, ref, scope, limit=limit)

    def collections(self):
        from .server import collections_summary
        return collections_summary()

    def groups(self):
        from .server import groups_summary
        return groups_summary()

    def mark_best(self, query, coll, ref):
        from . import feedback
        return {"ok": True, "id": feedback.add(query, coll, ref, "best")}

    def ask(self, q, colls, agent_safe=True, history=None):
        """Ask events (mse/ask.py); raises ask.AskDisabled while Ask is off. The route is the user's setting."""
        from . import ask
        return ask.ask(q, collections=colls, agent_safe=agent_safe, history=history)

    def sync(self, coll, path):
        from . import server
        if path and path.startswith("/"):
            c = config.collection_for_path(config.load(), path)
            if not c:
                raise ValueError("path is not inside any collection")
            from pathlib import Path
            coll, path = c["name"], str(Path(path).resolve().relative_to(c["path"]))
        return server.submit_scoped("file" if path else "sync", coll, path)


class HttpClient:
    """`mse mcp` (stdio) talks to the running daemon so the model stays loaded in one place."""

    def __init__(self):
        cfg = config.load()
        self.base = f"http://127.0.0.1:{os.environ.get('MSE_PORT') or cfg['port']}"   # MSE_PORT: a test daemon
        self.token = cfg["token"]

    def _call(self, method, path, params=None, body=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"X-MSE-Token": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(json.loads(e.read() or b"{}").get("error") or str(e))
        except urllib.error.URLError:
            raise RuntimeError("the MySearch daemon is not running — start it with `mse service install` or `mse serve`")

    def search(self, q, colls, limit, filters, mode, precise=False):
        # shortcuts=0: the callers (call_tool, the CLI) have taken the scope shortcut out of q and checked it
        # (under MSE_LOCK too); the service must not read another one in what is left
        params = {"q": q, "limit": limit, "mode": mode, "shortcuts": "0"}
        if precise:
            params["precise"] = "1"
        if colls:
            params["c"] = ",".join(colls)
        params.update({k: v[0] for k, v in filters.items()})
        return self._call("GET", "/api/search", params)

    def get(self, coll, ref, max_chars, page):
        params = {"ref": ref, "max": max_chars}
        if coll:
            params["c"] = coll
        if page:
            params["page"] = page
        try:
            return self._call("GET", "/api/doc", params)
        except RuntimeError as e:
            if "not found" in str(e):
                return None
            raise

    def similar(self, coll, ref, limit, scope=None):
        params = {"ref": ref, "limit": limit}
        if coll:
            params["c"] = coll
        if scope:
            params["in"] = ",".join(scope)
        return self._call("GET", "/api/similar", params)

    def collections(self):
        return self._call("GET", "/api/collections")["collections"]

    def groups(self):
        return self._call("GET", "/api/groups")["groups"]

    def mark_best(self, query, coll, ref):
        return self._call("POST", "/api/feedback", body={"query": query, "collection": coll, "path": ref, "kind": "best"})

    def sync(self, coll, path):
        return self._call("POST", "/api/sync", body={"collection": coll, "path": path})

    def ask(self, q, colls, agent_safe=True, history=None, route=None):
        """Ask events streamed from POST /api/ask (NDJSON). RuntimeError("ask_disabled") while Ask is off.
        history: earlier turns [{"q", "a"}] for a follow-up (`mse ask --follow-up`, the MCP tool's `history`);
        route: "cloud" | "local" for this question (`mse ask --cloud / --local`); agents never send one."""
        body = {"q": q, "collections": colls or [], "agent_safe": agent_safe, "shortcuts": False}   # as in search()
        if history:
            body["history"] = history
        if route:
            body["route"] = route
        req = urllib.request.Request(self.base + "/api/ask", data=json.dumps(body).encode(), method="POST",
                                     headers={"X-MSE-Token": self.token, "Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            raise RuntimeError(json.loads(e.read() or b"{}").get("error") or str(e))
        except urllib.error.URLError:
            raise RuntimeError("the MySearch daemon is not running — start it with `mse service install` or `mse serve`")
        with r:
            for line in r:
                if line.strip():
                    yield json.loads(line)


# ---------------------------------------------------------------- formatting

def _plain(snip: str) -> str:
    return re.sub(r"</?mark>", "**", snip or "").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">") \
        .replace("&quot;", '"').replace("&#x27;", "'")


def _fmt_time(t):
    if t is None:
        return ""
    t = int(t)
    return f"{t // 60}:{t % 60:02d}"


def _agent_safe(text: str) -> str:
    if config.load().get("agents", {}).get("redact", True):
        from .redact import redact
        return redact(text)
    return text


def format_results(res: dict) -> str:
    where = ', '.join(res['collections']) or 'nothing'
    label = (res.get("scope") or {}).get("label")
    if label and label not in (where, "All"):
        where = f"{label} ({where})"
    lines = [f"{len(res['results'])} results for \"{res['query']}\" in {where}"]
    bridge = sorted({t for v in res.get("bridge", {}).values() for t in v})
    if bridge:
        lines.append("also searched (translation bridge): " + ", ".join(bridge))
    for n, r in enumerate(res["results"], 1):
        meta = [r["kind"]]
        if r.get("date"):
            meta.append(r["date"])
        if r.get("pages"):
            meta.append(f"{r['pages']} pages")
        if r.get("metadata_only"):
            meta.append("METADATA-ONLY")
        shown = r["path"]
        if r.get("metadata_only") and config.load().get("agents", {}).get("redact", True):
            from .redact import redact_path
            shown = redact_path(shown)
        fav = {"query": "  ★ favourite", "similar": "  ★ favourite (similar query)"}.get(r.get("favorite"), "")
        lines.append(f"\n{n}. [{r['collection']}] {shown}  #{r['docid']}  ({', '.join(meta)}; matched: {', '.join(r['why'])}){fav}")
        for h in r["hits"][:2]:
            where = ""
            if h.get("page"):
                where = f"p.{h['page']} "
            if h.get("t0") is not None:
                where = f"{_fmt_time(h['t0'])}–{_fmt_time(h['t1'])} "
            if h.get("snippet"):
                lines.append(f"   {where}{_plain(h['snippet'])}")
            elif h["modality"] in ("image", "page", "video", "audio"):
                lines.append(f"   {where}[{h['modality']} match]")
        if r.get("context"):
            lines.append(f"   context: {r['context'][:200]}")
        if r.get("copies"):
            lines.append(f"   same document also at: {', '.join(c['path'] for c in r['copies'][:4])}")
    return _agent_safe("\n".join(lines))


def format_answer(events: list[dict]) -> str:
    """Ask events -> the answer text plus the sources it cites, identifiers masked."""
    err = next((e.get("message") for e in events if e.get("type") == "error"), None)
    if err:
        return f"Ask failed: {err}"
    sources = next((e.get("sources") or [] for e in events if e.get("type") == "sources"), [])
    done = next((e for e in events if e.get("type") == "done"), {})
    answer = done.get("answer") or "".join(e.get("text", "") for e in events if e.get("type") == "token")
    lines = [_agent_safe(answer.strip())]
    cited = done.get("citations") or []
    if cited:
        lines += ["", "Sources:"]
        for s in sources:
            if s["n"] in cited:
                where = f", page {s['page']}" if s.get("page") else (f", at {s['ts']}" if s.get("ts") else "")
                lines.append(_agent_safe(f"[{s['n']}] {s['docid']} {s['collection']}/{s['path']}{where}"))
    elif done.get("answered") is False:
        lines.append("(no source in the indexed documents answers this)")
    if done.get("where") == "cloud":
        who = f"the cloud model {done.get('model')} ({done.get('provider')})"
    else:
        who = f"the local model {done.get('model')}"
    lines.append(f"\n(written by {who}; read the cited #docid with `get` to verify)")
    return "\n".join(lines)


def format_doc(d: dict) -> str:
    shown = d["path"]
    if d.get("metadata_only") and config.load().get("agents", {}).get("redact", True):
        from .redact import redact_path
        shown = redact_path(shown)
    head = f"# {shown}  (#{d['docid']}, {d['collection']}, {d['kind']}"
    head += f", {d['doc_date']})" if d.get("doc_date") else ")"
    if d.get("metadata_only"):
        return head + "\n\n" + d.get("note", "metadata only")
    body = d.get("text") or "(no text extracted)"
    if d.get("truncated"):
        body += "\n\n[truncated — pass a larger max_chars or a page number]"
    return _agent_safe(head + "\n" + (f"context: {d['context']}\n" if d.get("context") else "") + "\n" + body)


# ---------------------------------------------------------------- dispatch

def default_collection(cwd: str | None) -> str | None:
    if not cwd:
        return None
    c = config.collection_for_path(config.load(), cwd)
    return c["name"] if c else None


class Lock:
    """MSE_LOCK: this session may only use the collection containing its folder; none at all if there is none."""

    def __init__(self, cwd: str | None):
        self.cwd = cwd
        self.name = default_collection(cwd)
        self.error = None if self.name else (
            f"MSE_LOCK is set, but this folder ({cwd or 'unknown'}) is not inside any indexed MySearch collection, "
            "so this session may not search anything. Add the folder (`mse add <folder>`) or remove MSE_LOCK "
            "from this folder's MCP config.")

    @property
    def refusal(self) -> str:
        return f"This session is limited to the '{self.name}' collection by this folder's MCP config."

    def check(self, args: dict) -> dict:
        """The arguments with the collection forced to the locked one; PermissionError for anything else."""
        if self.error:
            raise PermissionError(self.error)
        coll = args.get("collection")
        if coll and not self.only_mine(coll):
            raise PermissionError(self.refusal)
        for ref in [args.get("ref"), args.get("path")] + list(args.get("refs") or []):
            if isinstance(ref, str) and ref.strip().startswith(("/", "~")):
                c = config.collection_for_path(config.load(), ref.strip())
                if not c or c["name"] != self.name:
                    raise PermissionError(f"{ref.strip()} is outside the '{self.name}' collection. {self.refusal}")
        return {**args, "collection": self.name}

    def allows(self, coll_name: str | None) -> bool:
        return coll_name == self.name

    def check_folders(self, text) -> None:
        """A folder path in a query (in:~/…, in:/…) must lie inside the locked collection."""
        c = config.get_collection(config.load(), self.name) if self.name else None
        for f in config.folder_tokens(text if isinstance(text, str) else ""):
            if not c or not (f == c["path"].rstrip("/") or f.startswith(c["path"].rstrip("/") + "/")):
                raise PermissionError(f"{f} is outside the '{self.name}' collection. {self.refusal}")

    def only_mine(self, spec) -> bool:
        """A collection, group or comma list that names nothing but the locked collection ("all" never does).
        Aliases count as what they name."""
        cfg = config.load()
        for t in config.scope_tokens(spec):
            t = config.canonical_name(cfg, t) or t
            if t == self.name:
                continue
            if t == "all" or config.get_collection(cfg, t):
                return False
            g = config.get_group(cfg, t)
            if not g or [m for m in g["collections"] if config.get_collection(cfg, m)] != [self.name]:
                return False
        return True


def _scope_arg(coll) -> tuple[str | None, list[str] | None]:
    """The `collection` argument -> (one collection or "all" or None, the names when it is a group / list / mix).
    Unknown names -> ValueError."""
    tokens = config.scope_tokens(coll)
    if not tokens:
        return None, None
    cfg = config.load()
    if tokens == ["all"] or len(tokens) == 1 and config.get_collection(cfg, tokens[0]):
        return tokens[0], None
    config.resolve_scope(cfg, tokens)
    return None, tokens


def _members(multi: list[str]) -> list[str]:
    return config.resolve_scope(config.load(), multi)["collections"]


def _get_in(client, coll, multi, ref, max_chars, page):
    if multi:
        for name in _members(multi):
            d = client.get(name, ref, max_chars, page)
            if d:
                return d
        return None
    return client.get(coll if coll != "all" else None, ref, max_chars, page)


def lock_from_env(cwd: str | None) -> Lock | None:
    if os.environ.get("MSE_LOCK", "").strip().lower() in ("1", "true", "yes", "on"):
        return Lock(cwd)
    return None


def tools_for(lock: Lock | None) -> list[dict]:
    """TOOLS, with every `collection` parameter describing the lock when there is one."""
    if lock is None:
        return TOOLS
    note = (f"Only '{lock.name}' is allowed (the default): this session is limited to it by this folder's MCP config."
            if lock.name else "This session is locked and its folder is in no collection: every call fails.")
    out = []
    for t in TOOLS:
        t = json.loads(json.dumps(t))
        props = t["inputSchema"].get("properties", {})
        if "collection" in props:
            props["collection"]["description"] = note
        if t["name"] == "collections":
            t["description"] = f"The collection this session is limited to ('{lock.name}'), its contents and index health." \
                if lock.name else note
        out.append(t)
    return out


def _shortcut(name: str, args: dict, lock: Lock | None) -> dict:
    """search / ask: a scope shortcut in the query ("work: invoice", "invoice in:work", config.scope_shortcut)
    replaces the `collection` argument and goes through the same lock check. Under the lock an unknown name is
    refused like any other (the error would list the other collections). The query loses the shortcut, and the
    clients tell the service not to look for another one in what is left."""
    key = "query" if name == "search" else "question"
    if not isinstance(args.get(key), str):
        return args
    if lock is not None:
        lock.check_folders(args[key])
    s = config.scope_shortcut(args[key], config.load())
    if s["unknown"]:
        if lock is not None:
            raise PermissionError(lock.refusal)
        config.scope_shortcut(args[key], config.load(), strict=True)       # ValueError naming the known ones
    if s["tokens"] is None:
        return args
    args = {**args, key: s["rest"], "collection": ",".join(s["tokens"])}
    return lock.check(args) if lock is not None else args


def _confined(lock: Lock | None, items) -> None:
    """Under the lock, nothing from another collection leaves, whatever the backend returned."""
    if lock is not None and any(isinstance(x, dict) and x.get("collection") not in (None, lock.name) for x in items or []):
        raise PermissionError(lock.refusal)


def call_tool(name: str, args: dict, client, cwd: str | None, lock: Lock | None = None) -> str:
    if lock is not None:
        args = lock.check(args)
    if name in ("search", "ask"):
        args = _shortcut(name, args, lock)
    coll, multi = _scope_arg(args.get("collection"))     # multi: a group, a comma list or a mix of them
    if name == "search":
        if coll == "all":
            colls = None
        elif multi:
            colls = multi
        elif coll:
            colls = [coll]
        else:
            d = default_collection(cwd)
            colls = [d] if d else None
        filters = {k: [args[k]] for k in ("type", "after", "before", "path", "include", "exclude") if args.get(k)}
        res = client.search(args["query"], colls, int(args.get("limit") or 10), filters, args.get("mode") or "hybrid",
                            bool(args.get("precise", True)))
        _confined(lock, res.get("results"))
        return format_results(res)
    if name == "get":
        d = _get_in(client, coll, multi, args["ref"], int(args.get("max_chars") or 12000), args.get("page"))
        if d and lock is not None and not lock.allows(d.get("collection")):
            d = None                                        # resolved outside the locked collection
        return format_doc(d) if d else f"not found: {args['ref']}" + (f" in '{lock.name}'" if lock else "")
    if name == "similar":
        if multi or coll == "all":                         # look in the group / list / everything
            res = client.similar(None, args["ref"], int(args.get("limit") or 10), scope=multi or ["all"])
        else:
            res = client.similar(coll, args["ref"], int(args.get("limit") or 10))
        if res and lock is not None and not lock.allows(res.get("collection")):
            res = None
        return format_results(res) if res else f"not found: {args['ref']}" + (f" in '{lock.name}'" if lock else "")
    if name == "multi_get":
        out = []
        for ref in args.get("refs", [])[:20]:
            d = _get_in(client, coll, multi, ref, int(args.get("max_chars") or 4000), None)
            if d and lock is not None and not lock.allows(d.get("collection")):
                d = None
            out.append(format_doc(d) if d else f"not found: {ref}")
        return "\n\n---\n\n".join(out)
    if name == "collections":
        lines = []
        for c in client.collections():
            if lock is not None and not lock.allows(c["name"]):
                continue
            s = c.get("stats") or {}
            also = f" (also: {', '.join(c['aliases'])})" if c.get("aliases") else ""
            lines.append(f"- {c['name']}{also}: {c['path']} — {c.get('description') or ''}\n  files {s.get('files', 0)}, "
                         f"pending embeddings {s.get('embed_pending', 0)}, pending OCR/media {s.get('heavy_pending', 0)}, "
                         f"in global search: {c.get('in_global')}")
            for prefix, ctx in (c.get("contexts") or {}).items():
                lines.append(f"    {prefix}: {ctx}")
        if lock is not None:
            lines.append(f"\nThis session is locked to '{lock.name}' by this folder's MCP config (MSE_LOCK): "
                         "other collections and 'all' are refused.")
            return "\n".join(lines)
        groups = []
        try:
            groups = client.groups() if hasattr(client, "groups") else []
        except Exception:
            pass
        if groups:
            lines.append("\nGroups (search them together with collection='<group>', or name folders as a comma list 'a,b'):")
            for g in groups:
                also = f", also: {', '.join(g['aliases'])}" if g.get("aliases") else ""
                lines.append(f"- {g['name']} ({g['label']}{also}): {', '.join(g['collections']) or 'empty'}, {g.get('files', 0)} files")
        lines.append("\nIn a query, 'name: words' at the start or in:name anywhere picks the folder or group.")
        d = default_collection(cwd)
        lines.append(f"\nDefault scope for this session: {d or 'all collections'}")
        return "\n".join(lines)
    if name == "mark_best":
        ref = args["ref"].strip()
        target = None if (coll == "all" or multi) else coll or default_collection(cwd)
        try:
            r = client.mark_best(args["query"], target, ref)
        except (KeyError, RuntimeError) as e:
            if lock is not None or coll or not target or "not found" not in str(e):
                raise
            r = client.mark_best(args["query"], None, ref)        # not in this folder's collection: look everywhere
        return (f"Recorded {ref} as the best result for \"{args['query']}\" (feedback #{r['id']}). "
                "It will be listed first when this query is searched again.")
    if name == "sync":
        return json.dumps(client.sync(",".join(multi) if multi else coll, args.get("path")))
    if name == "ask":                      # under MSE_LOCK, lock.check above has already forced the collection
        if coll == "all":
            colls = ["all"]
        elif multi:
            colls = multi
        elif coll:
            colls = [coll]
        else:
            d = default_collection(cwd)
            colls = [d] if d else None
        from .ask import validate_history            # malformed history -> ValueError -> an error result
        history = validate_history(args.get("history"))
        try:
            events = list(client.ask(args["question"], colls, agent_safe=True,
                                     **({"history": history} if history else {})))
        except Exception as e:
            if type(e).__name__ == "AskDisabled" or "ask_disabled" in str(e):
                return ("Ask is switched off in MySearch (Settings → Ask), so no answer was generated. "
                        "Use `search` and `get` instead.")
            raise
        _confined(lock, [s for e in events if e.get("type") == "sources" for s in e.get("sources") or []])
        return format_answer(events)
    raise ValueError(f"unknown tool {name}")


def handle(req: dict, client, cwd: str | None, lock: Lock | None = None) -> dict | None:
    method, rid = req.get("method"), req.get("id")
    if rid is None:            # notification
        return None
    try:
        if method == "initialize":
            ver = (req.get("params") or {}).get("protocolVersion") or PROTOCOL
            result = {"protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "mysearch", "title": "MySearch", "version": __version__},
                      "instructions": INSTRUCTIONS + (
                          "" if lock is None else f" This session is limited to the '{lock.name}' collection."
                          if lock.name else " " + lock.error)}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tools_for(lock)}
        elif method == "tools/call":
            p = req.get("params") or {}
            try:
                text = call_tool(p.get("name"), p.get("arguments") or {}, client, cwd, lock)
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            except PermissionError as e:
                result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
            except Exception as e:
                result = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
        elif method in ("resources/list", "prompts/list"):
            result = {method.split("/")[0]: []}
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}}


def run_stdio() -> None:
    client = HttpClient()
    cwd = os.environ.get("MSE_CWD") or os.getcwd()
    lock = lock_from_env(cwd)               # computed once: the folder's collection, or fail closed
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(req, client, cwd, lock)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
