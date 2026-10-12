"""MCP server (JSON-RPC 2.0) for agents. stdio transport via `mse mcp`, HTTP transport at POST /mcp.

Scope rule: an agent searches the collection that contains its working directory unless it asks for
another collection or for "all". A session working in one vault does not get results from another by
accident. `collection` also takes a saved group ("work") or a comma list ("work,notes").

Lock (stdio only): with MSE_LOCK=1 in the server's environment, the session may use ONLY the collection that
contains MSE_CWD (or the working directory). Other collections, "all", groups or lists naming anything else, and
paths outside it are refused (a group or list containing only the locked collection is fine), and
when the folder is in no collection at all every tool call fails (fail closed). The HTTP /mcp endpoint has no
working directory and is not locked.

Besides the tools the server offers resources (the collection list, saved searches, single documents by #docid) and three
ready-made prompts. They are thin wrappers over the tools below, so the lock, the confinement check and the masking of
personal identifiers apply to them unchanged. Every new string that comes from a document or from the user's
configuration (a party name, a reference, a title, a saved search) goes through `_guard`, which follows the same
`agents.redact` switch as `_agent_safe`.
"""

import datetime
import json
import math
import os
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, config

PROTOCOL = "2025-06-18"                                       # offered when the client asks for a version we do not know
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")    # tools, resources and prompts exist in all three
# Mirrors docmeta.DOCTYPES (kept here so starting the stdio server does not import docmeta; tests/test_mcp_schema.py
# fails when the two differ).
DOCTYPES = ("invoice", "receipt", "bank_statement", "contract", "letter", "quote", "reminder", "payslip",
            "tax_document", "other")
DATE_PATTERN = r"^\d{4}(-\d{2}(-\d{2})?)?$"                  # 2025, 2025-03, 2025-03-14 (the calendar is checked on top)

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
    "earlier turns as `history` for a follow-up; check what matters with `get`. "
    "`search` can also filter by what a document is (`doctype`, `amount_min` / `amount_max`, `currency`, `party`, "
    "`docdate_from` / `docdate_to`); a result may carry a `meta:` line (kind, date, amount, party, reference) and a "
    "`cite:` line (title and page, or time in a recording) to quote when you name a source. `timeline` counts the "
    "documents of a kind per month and lists the months with none; `facets` counts kinds, parties, months and currencies. "
    "The collection list and saved searches are resources, and the prompts `find-document`, `monthly-summary` and "
    "`missing-receipts` are ready-made workflows."
)

TOOLS = [
    {
        "name": "search",
        "title": "Search indexed folders",
        "description": "Hybrid keyword + meaning search across documents, PDF pages, images, audio and video. "
                       "Returns ranked files with #docid, path, why it matched, page/time and a snippet. "
                       "Inline filters also work in the query: type:pdf, after:2025-03, before:2025-04, path:\"Invoices\". "
                       "A scope shortcut overrides `collection`: a folder, group or alias name before a colon at the "
                       "start ('work: invoice', 'a,b: invoice') or in:<name> anywhere ('invoice in:work'). "
                       "Document filters narrow the results to what a document is: `doctype` (any of the listed kinds), "
                       "`amount_min` / `amount_max` (both limits apply, in the document's own currency, two decimals), "
                       "`currency`, `party` (part of the sender or counterparty name, one name) and `docdate_from` / "
                       "`docdate_to` (the date inside the document; the end is inclusive: to 2025-06 includes all of June). "
                       "They are added to the query as the inline filters doctype:, amount:, currency:, party: and docdate:, "
                       "all conditions must hold. With only filters, pass an empty `query` to list the matching documents, "
                       "newest first. A result may add a `meta:` line (kind, date, amount, party, reference) and a `cite:` "
                       "line (title and page, or time in a recording): quote it when you name a source.",
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
                "doctype": {"type": "array", "items": {"type": "string", "enum": list(DOCTYPES)},
                            "description": "Only these kinds of document; several are alternatives (invoice or receipt). "
                                           "Documents that are not classified yet never match"},
                "amount_min": {"type": "number", "minimum": 0, "maximum": 999999999999,
                               "description": "Only documents whose amount is at least this (0 to 999999999999, two decimals; "
                                              "negative amounts cannot be searched)"},
                "amount_max": {"type": "number", "minimum": 0, "maximum": 999999999999,
                               "description": "Only documents whose amount is at most this"},
                "currency": {"type": "string", "pattern": "^[A-Za-z]{3}$",
                             "description": "ISO currency code of the amount, e.g. EUR"},
                "party": {"type": "string",
                          "description": "Part of the sender or counterparty name, at most 120 characters. One name: no comma, "
                                         "double quote or backslash"},
                "docdate_from": {"type": "string", "pattern": DATE_PATTERN,
                                 "description": "Only documents dated on/after this day, month or year inside the document: "
                                                "YYYY, YYYY-MM or YYYY-MM-DD"},
                "docdate_to": {"type": "string", "pattern": DATE_PATTERN,
                               "description": "Only documents dated up to and including this day, month or year: "
                                              "YYYY, YYYY-MM or YYYY-MM-DD"},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True},
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
                "page": {"type": "integer", "minimum": 1,
                         "description": "Only this page (PDFs, slides). Pages count from 1, as the page numbers in search results do"},
                "max_chars": {"type": "integer", "default": 12000},
            },
            "required": ["ref"],
        },
        "annotations": {"readOnlyHint": True},
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
        "annotations": {"readOnlyHint": True},
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
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "collections",
        "title": "List collections",
        "description": "The indexed folders, what they contain (descriptions, path contexts), index health, and the saved groups of folders that can be searched together.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
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
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
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
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
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
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "timeline",
        "title": "Documents per month",
        "description": "How many documents of a kind (invoices, receipts ...) exist per month, with the amounts per currency, "
                       "and the months in the range that have none (`gaps`): the way to answer \"which months have no "
                       "receipts\". Counts only documents with a document date inside them; the others are reported as "
                       "undated. Then open a month with `search` (empty `query`, the same `doctype`, `docdate_from` and "
                       "`docdate_to` set to the month).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string", "description": "Collection name, a group name, a comma list or 'all'. Default: the collection containing the working directory"},
                "type": {"type": "array", "items": {"type": "string", "enum": list(DOCTYPES)},
                         "description": "Only these kinds of document (one kind may also be given as a plain string). Default: every kind"},
                "from": {"type": "string", "pattern": DATE_PATTERN,
                         "description": "First month: YYYY-MM (YYYY or YYYY-MM-DD also work). Default: the first month that has a document"},
                "to": {"type": "string", "pattern": DATE_PATTERN,
                       "description": "Last month, inclusive: YYYY-MM (YYYY or YYYY-MM-DD also work). Default: the last month that has a document"},
            },
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "facets",
        "title": "What the documents are",
        "description": "Counts of the classified documents: per kind, the most frequent parties (sender or counterparty), "
                       "per month and per currency, plus how many files are not classified and how many documents have no "
                       "date. Covers every month; use `timeline` for a date range.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string", "description": "Collection name, a group name, a comma list or 'all'. Default: the collection containing the working directory"},
                "type": {"type": "array", "items": {"type": "string", "enum": list(DOCTYPES)},
                         "description": "Only these kinds of document (one kind may also be given as a plain string). Default: every kind"},
            },
        },
        "annotations": {"readOnlyHint": True},
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

    def timeline(self, colls, types, date_from, date_to):
        """Documents per month (mse/docmeta.py). colls: names, groups or "all"; None = every collection."""
        from . import docmeta
        names = config.resolve_scope(config.load(), colls or None)["collections"]
        return docmeta.timeline(names, types or None, date_from, date_to)

    def facets(self, colls, types):
        from . import docmeta
        names = config.resolve_scope(config.load(), colls or None)["collections"]
        return docmeta.facets(names, types or None)

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

    def timeline(self, colls, types, date_from, date_to):
        params = {}
        if colls:
            params["c"] = ",".join(colls)
        if types:
            params["type"] = list(types)
        if date_from:
            params["from"] = date_from
        if date_to:
            params["to"] = date_to
        return self._call("GET", "/api/docs/timeline", params)

    def facets(self, colls, types):
        params = {}
        if colls:
            params["c"] = ",".join(colls)
        if types:
            params["type"] = list(types)
        return self._call("GET", "/api/docs/facets", params)

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


def _redact_on() -> bool:
    """The same switch as _agent_safe: config `agents.redact`, on by default."""
    return bool(config.load().get("agents", {}).get("redact", True))


# IBAN-shaped runs in any letter case, with spaces or hyphens between the groups; _iban_like decides if one really is.
_RX_IBAN_ANY = re.compile(r"\b[A-Za-z]{2}\d{2}(?:[ -]?[A-Za-z0-9]{4}){2,7}(?:[ -]?[A-Za-z0-9]{1,4})?\b")
_RX_CARD_RUN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")        # 13-19 digits, grouped or not; _luhn decides


def _guard(value, reference: bool = False, on: bool | None = None):
    """A string that comes from a document or from the user's settings (party, reference, title, saved search ...), for an
    agent. Switch off (`agents.redact` false): returned unchanged. On: redact() first; then an IBAN-shaped or a Luhn-valid
    run (the docmeta checks the extractor uses), then every token with six or more digits in a row becomes "[id]" (the
    pattern redact_path uses for identity files: redact() alone leaves a bare ID such as a passport-style number alone).
    reference=True: a reference the extractor's own safety test (docmeta._safe_reference) rejects is masked whole.
    on: the state of the switch when the caller has just read it (a long output asks once, not once per value)."""
    if value is None:
        return None
    text = str(value)
    if not text or not (_redact_on() if on is None else on):
        return text
    from .redact import MASK, _RX_PATH_ID, redact
    if reference:
        from . import docmeta
        if not docmeta._safe_reference(text, text):
            return MASK
    text = redact(text) or ""
    if _RX_IBAN_ANY.search(text) or _RX_CARD_RUN.search(text):
        from . import docmeta
        text = _RX_IBAN_ANY.sub(lambda m: MASK if docmeta._iban_like(re.sub(r"[ -]", "", m.group(0))) else m.group(0), text)
        text = _RX_CARD_RUN.sub(lambda m: MASK if docmeta._luhn(re.sub(r"\D", "", m.group(0))) else m.group(0), text)
    return _RX_PATH_ID.sub("[id]", text)


def _cite_time(t) -> str:
    """Seconds -> m:ss, or h:mm:ss from one hour on (the hit lines of format_results keep their own m:ss)."""
    t = max(0, int(t))
    h, rest = divmod(t, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _cite_title(r: dict, on: bool | None = None) -> str | None:
    """What a citation calls the file: its title, else its name. A metadata-only file never shows its title (it can hold the
    holder's name): its name, with long numbers masked while masking is on."""
    on = _redact_on() if on is None else on
    name = r.get("name") or (r.get("path") or "").rsplit("/", 1)[-1]
    if r.get("metadata_only"):
        if not name:
            return None
        if on:
            from .redact import redact_path
            name = redact_path(name)
        return _guard(name, on=on)
    title = r.get("title") or name
    return _guard(title, on=on) if title else None


def result_cite(r: dict, on: bool | None = None) -> tuple[int | None, str | None]:
    """(page, cite) of a search result. page: the page of the first hit that has one (the extractor counts from 1, so it is
    what a reader sees). cite: "<title> p.<page>", or for a recording "<title> @ <m:ss | h:mm:ss>" from the first hit with a
    time; None when no hit says where in the file the match is."""
    hits = [h for h in r.get("hits") or [] if isinstance(h, dict)]
    for h in hits:
        if h.get("page"):
            title = _cite_title(r, on)
            return h["page"], (f"{title} p.{h['page']}" if title else None)
    for h in hits:
        if h.get("t0") is not None:
            title = _cite_title(r, on)
            return None, (f"{title} @ {_cite_time(h['t0'])}" if title else None)
    return None, None


def _meta_line(meta, on: bool | None = None) -> str | None:
    """"   meta: invoice · 2025-03-14 · 1234.00 EUR · party <name> · ref <reference>" for a result that carries document
    facts (search results `meta`); parts that are unknown are left out. Every value goes through _guard."""
    if not isinstance(meta, dict) or not meta:
        return None
    on = _redact_on() if on is None else on
    parts = []
    if meta.get("doctype"):
        parts.append(_guard(meta["doctype"], on=on))
    if meta.get("doc_date"):
        parts.append(_guard(meta["doc_date"], on=on))
    if meta.get("amount") is not None:
        try:
            amount = f"{float(meta['amount']):.2f}"
        except (TypeError, ValueError):
            amount = None
        if amount:
            parts.append(f"{amount} {_guard(meta['currency'], on=on)}" if meta.get("currency") else amount)
    if meta.get("counterparty"):
        parts.append("party " + _guard(meta["counterparty"], on=on))
    if meta.get("reference"):
        parts.append("ref " + _guard(meta["reference"], reference=True, on=on))
    return "   meta: " + " · ".join(parts) if parts else None


def format_results(res: dict) -> str:
    on = _redact_on()
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
        facts = _meta_line(r.get("meta"), on)
        if facts:
            lines.append(facts)
        _, cite = result_cite(r, on)
        if cite:
            lines.append(f"   cite: {cite}")
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


def format_doc(d: dict, page: int | None = None) -> str:
    """One document as text. page: the page the caller asked for (counted from 1): the head then says "page N" and a `cite:`
    line gives the title and the page; a page without extracted text says so instead of printing an empty body."""
    shown = d["path"]
    if d.get("metadata_only") and config.load().get("agents", {}).get("redact", True):
        from .redact import redact_path
        shown = redact_path(shown)
    head = f"# {shown}  (#{d['docid']}, {d['collection']}, {d['kind']}"
    if d.get("metadata_only"):
        return head + (f", {d['doc_date']})" if d.get("doc_date") else ")") + "\n\n" + d.get("note", "metadata only")
    head += (f", {d['doc_date']}" if d.get("doc_date") else "") + (f", page {page}" if page else "") + ")"
    text = d.get("text")
    cite = ""
    if page and text:
        title = _cite_title(d)
        if title:
            cite = f"cite: {title} p.{page}\n"
    if text:
        body = text
    elif page:
        body = f"page {page} has no extracted text"
        if d.get("pages") and page > d["pages"]:
            body += f" (the document has {d['pages']} pages)"
    else:
        body = "(no text extracted)"
    if d.get("truncated"):
        body += "\n\n[truncated — pass a larger max_chars or a page number]"
    return _agent_safe(head + "\n" + cite + (f"context: {d['context']}\n" if d.get("context") else "") + "\n" + body)


def _lines_cap(items: list, cap: int, what: str) -> list[str]:
    return [str(x) for x in items[:cap]] + ([f"... and {len(items) - cap} more {what}"] if len(items) > cap else [])


def format_timeline(res: dict) -> str:
    """docmeta.timeline() -> text: a line per month (count, amounts per currency), the gaps, first and last month, undated."""
    on = _redact_on()
    g = lambda v: _guard(v, on=on)
    types = [str(t) for t in res.get("types") or []]
    where = ", ".join(res.get("collections") or []) or "nothing"
    head = f"Documents per month in {where} ({', '.join(types) if types else 'all kinds'})"
    if res.get("from") and res.get("to"):
        head += f", {g(res['from'])} to {g(res['to'])}"
    lines = [head, f"{res.get('total', 0)} documents on the timeline; {res.get('undated', 0)} classified documents have no "
                   "document date and are not placed on it"]
    if res.get("first"):
        lines.append(f"first month with a document: {g(res['first'])}, last: {g(res['last'])}")
    gaps = [g(m) for m in res.get("gaps") or []]
    lines.append("months with no document: " + (", ".join(_lines_cap(gaps, 240, "months")) if gaps else "none"))
    buckets = res.get("buckets") or []
    if buckets:
        lines.append("")
    for b in buckets[:240]:
        n = b.get("count", 0)
        line = f"{g(b.get('month'))}  {n} document{'s' if n != 1 else ''}"
        amounts = " + ".join(f"{g(cur)} {v:,.2f}" for cur, v in sorted((b.get("amounts") or {}).items()))
        if amounts:
            line += f"  {amounts}"
            if 0 < b.get("with_amount", 0) < n:
                line += f" (amount found on {b['with_amount']} of {n})"
        elif n == 0:
            line += "  (gap)"
        lines.append(line)
    if len(buckets) > 240:
        lines.append(f"... {len(buckets) - 240} more months; narrow the range with `from` and `to`")
    if buckets:
        example = next((b["month"] for b in buckets if b.get("count")), buckets[0]["month"])
        kind = f' and doctype ["{types[0]}"]' if len(types) == 1 else (" and the same doctype" if types else "")
        lines.append(f'\nhint: list a month\'s documents with `search`: empty query, docdate_from and docdate_to set to that '
                     f'month (e.g. "{g(example)}"){kind}.')
    else:
        lines.append("\nhint: no classified document with a document date was found in this range; `facets` shows how many "
                     "files are classified (classification runs after indexing).")
    return _agent_safe("\n".join(lines))


def format_facets(res: dict) -> str:
    """docmeta.facets() -> text: kinds, top parties, months, currencies and the classified / unclassified / undated counts."""
    on = _redact_on()
    g = lambda v: _guard(v, on=on)
    where = ", ".join(res.get("collections") or []) or "nothing"
    lines = [f"Document facts in {where}",
             f"files {res.get('files', 0)}, classified {res.get('classified', 0)}, not classified {res.get('unclassified', 0)}, "
             f"classified without a document date {res.get('undated', 0)}"]

    def counted(rows, key):
        return ", ".join(f"{g(r[key])} {r['count']}" for r in rows) or "none"
    lines.append("document kinds: " + counted(res.get("doctypes") or [], "doctype"))
    parties = res.get("counterparties") or []
    lines.append("top parties: " + (", ".join(f"{g(p.get('name'))} ({p.get('count', 0)})" for p in parties) or "none"))
    months = res.get("months") or []
    shown = months[-36:]
    lines.append("months: " + (", ".join(f"{g(m['month'])} ({m['count']})" for m in shown) if shown else "none")
                 + (f" ... and {len(months) - len(shown)} earlier months" if len(months) > len(shown) else ""))
    lines.append("currencies: " + counted(res.get("currencies") or [], "currency"))
    lines.append("\nhint: `timeline` shows these per month and lists the months with none; `search` takes the same kinds as "
                 "`doctype`.")
    return _agent_safe("\n".join(lines))


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


# ---------------------------------------------------------------- arguments: document filters, pages, dates

_DATE_RX = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def _given(v) -> bool:
    """An optional argument counts as given unless it is absent, an empty string or an empty list (clients send those for
    "not set"; 0 is a value)."""
    return v is not None and v != "" and v != []


def _check_date(label: str, v) -> str:
    """YYYY, YYYY-MM or YYYY-MM-DD that is a real calendar date (the query language does not check the month or the day of a
    range's start)."""
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        raise ValueError(f"{label} must be text such as 2025, 2025-03 or 2025-03-14")
    text = str(v).strip()
    m = _DATE_RX.match(text)
    if not m:
        raise ValueError(f"{label} '{text}': use YYYY, YYYY-MM or YYYY-MM-DD")
    try:
        datetime.date(int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1))
    except ValueError:
        raise ValueError(f"{label} '{text}' is not a calendar date") from None
    return text


def _month_key(v: str, end: bool) -> tuple[int, int]:
    return int(v[:4]), int(v[5:7]) if len(v) >= 7 else (12 if end else 1)


def _amount_text(label: str, v) -> str:
    """A bound as the query language reads it: two decimals, so parse_number never mistakes 1.234 for 1234. Negative amounts
    and 1e12 or more cannot be written in the query language."""
    if isinstance(v, bool):
        raise ValueError(f"{label} must be a number")
    try:
        f = float(v.strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number") from None
    if not math.isfinite(f) or f < 0 or f >= 1e12:
        raise ValueError(f"{label} must be a number from 0 to 999999999999 (negative amounts cannot be searched)")
    return f"{f:.2f}"


def _doctype_list(label: str, v) -> list[str]:
    """A list of document types (one may be given as a plain string, or comma-separated): each one of DOCTYPES."""
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        raise ValueError(f"{label} must be a list of document types ({', '.join(DOCTYPES)})")
    out = []
    for item in items:
        for part in item.split(","):
            t = part.strip().lower()
            if not t:
                continue
            if t not in DOCTYPES:
                raise ValueError(f"{label}: '{part.strip()}' is not a document type (use {', '.join(DOCTYPES)})")
            if t not in out:
                out.append(t)
    return out


def _doc_filter_pairs(args: dict) -> list[tuple[str, str]]:
    """The document-filter arguments of `search` -> (key, value) pairs of the inline filters (doctype:, amount:, currency:,
    party:, docdate:). Everything is validated here, before it is written into a query. Repeated doctype: filters are
    alternatives; repeated amount: filters must all hold (so >=min and <=max make a range); docdate: is one range whose end is
    inclusive (docdate:2025-01..2025-06 includes June)."""
    pairs: list[tuple[str, str]] = []
    if _given(args.get("doctype")):
        pairs += [("doctype", t) for t in _doctype_list("doctype", args["doctype"])]
    lo = _amount_text("amount_min", args["amount_min"]) if _given(args.get("amount_min")) else None
    hi = _amount_text("amount_max", args["amount_max"]) if _given(args.get("amount_max")) else None
    if lo is not None and hi is not None and float(lo) > float(hi):
        raise ValueError("amount_min is greater than amount_max")
    if lo is not None:
        pairs.append(("amount", ">=" + lo))
    if hi is not None:
        pairs.append(("amount", "<=" + hi))
    cur = args.get("currency")
    if _given(cur):
        if not isinstance(cur, str) or not re.fullmatch(r"[A-Za-z]{3}", cur.strip()):
            raise ValueError("currency must be a three-letter code such as EUR")
        pairs.append(("currency", cur.strip().upper()))
    party = args.get("party")
    if _given(party):
        if not isinstance(party, str):
            raise ValueError("party must be text")
        party = party.strip()
        if not party:
            raise ValueError("party is empty")
        if len(party) > 120:
            raise ValueError("party is longer than 120 characters")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in party):
            raise ValueError("party must not contain control characters or line breaks")
        if re.search(r'[",\\]', party):
            raise ValueError('party must be one name without a comma, double quote or backslash '
                             '(the query language reads a comma as "or")')
        pairs.append(("party", party))
    a, b = args.get("docdate_from"), args.get("docdate_to")
    if _given(a) or _given(b):
        start = _check_date("docdate_from", a) if _given(a) else ""
        stop = _check_date("docdate_to", b) if _given(b) else ""
        if start and stop and _month_key(start, False) > _month_key(stop, True):
            raise ValueError("docdate_from is after docdate_to")
        value = f"{start}..{stop}"
        from . import docmeta
        docmeta.parse_docdate(value)                 # the range must really parse and must not end before it starts
        pairs.append(("docdate", value))
    return pairs


def _tokens(q: str) -> list[str]:
    """How search.parse() cuts a query into tokens: like a shell, and by plain whitespace when the quoting is broken."""
    try:
        return shlex.split(q)
    except ValueError:
        return q.split()


def _with_doc_filters(q, args: dict):
    """The query with the document-filter arguments added as inline filters; q itself, byte for byte, when none is given.
    The filters travel inside q because that is the one thing both backends pass on (LocalClient hands q to search.search,
    HttpClient sends it as q to /api/search, which drops any other key it does not know). A party with spaces is quoted, so
    the result is checked: the appended tokens must come out of the tokenizer exactly as written. A query with an unbalanced
    quote or a trailing backslash would swallow them, and is refused instead."""
    pairs = _doc_filter_pairs(args)
    if not pairs:
        return q
    if not isinstance(q, str):
        raise ValueError("query must be text")
    parts = []
    for key, value in pairs:
        quoted = key == "party" and re.search(r"[\s'\"\\]", value)
        parts.append(f'{key}:"{value}"' if quoted else f"{key}:{value}")
    base = q.rstrip()
    final = (base + " " if base else "") + " ".join(parts)
    want = [f"{key}:{value}" for key, value in pairs]
    if _tokens(final)[-len(want):] != want:
        raise ValueError("the document filters cannot be added to this query because it has an unbalanced quote or ends in "
                         "a backslash; fix the query, or leave out a party that has spaces or quotes")
    return final


def _page_arg(v) -> int | None:
    """get's `page`: counted from 1, as the page numbers in search results are. 0 or less is an error (not "no page")."""
    if v is None or v == "":
        return None
    n = None
    if not isinstance(v, bool) and (not isinstance(v, float) or v.is_integer()):
        try:
            n = int(v)
        except (TypeError, ValueError):
            n = None
    if n is None or n < 1:
        raise ValueError("page must be a whole number from 1 (pages count from 1)")
    return n


def _colls(coll, multi, cwd) -> list[str] | None:
    """The scope arguments -> the names to run over: None = every collection, else the collection / group / list given, else
    the collection that contains the working directory."""
    if coll == "all":
        return None
    if multi:
        return multi
    if coll:
        return [coll]
    d = default_collection(cwd)
    return [d] if d else None


def _confine_names(lock: Lock | None, res: dict) -> None:
    if lock is not None and any(c != lock.name for c in res.get("collections") or []):
        raise PermissionError(lock.refusal)


def _docs_view(name: str, args: dict, coll, multi, client, cwd, lock: Lock | None) -> str:
    """timeline / facets: the document facts of one scope. Under the lock the scope is the locked collection and nothing
    else, whatever was asked, and what comes back is checked once more."""
    types = _doctype_list("type", args["type"]) if _given(args.get("type")) else []
    colls = [lock.name] if lock is not None else _colls(coll, multi, cwd)
    if name == "facets":
        res = client.facets(colls, types)
        _confine_names(lock, res)
        return format_facets(res)
    start = _check_date("from", args["from"]) if _given(args.get("from")) else None
    stop = _check_date("to", args["to"]) if _given(args.get("to")) else None
    if start and stop and _month_key(start, False) > _month_key(stop, True):
        raise ValueError("from is after to")
    res = client.timeline(colls, types, start, stop)
    _confine_names(lock, res)
    return format_timeline(res)


def _collections_text(client, cwd, lock: Lock | None) -> str:
    """The `collections` tool's text (also the resource mysearch://collections)."""
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
        q = _with_doc_filters(args["query"], args)       # after the lock check and the scope shortcut: see its docstring
        res = client.search(q, colls, int(args.get("limit") or 10), filters, args.get("mode") or "hybrid",
                            bool(args.get("precise", True)))
        _confined(lock, res.get("results"))
        return format_results(res)
    if name == "get":
        page = _page_arg(args.get("page"))
        d = _get_in(client, coll, multi, args["ref"], int(args.get("max_chars") or 12000), page)
        if d and lock is not None and not lock.allows(d.get("collection")):
            d = None                                        # resolved outside the locked collection
        return format_doc(d, page) if d else f"not found: {args['ref']}" + (f" in '{lock.name}'" if lock else "")
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
        return _collections_text(client, cwd, lock)
    if name in ("timeline", "facets"):
        return _docs_view(name, args, coll, multi, client, cwd, lock)
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


# ---------------------------------------------------------------- resources and prompts

class RpcError(Exception):
    """A JSON-RPC error with its own code: -32002 resource not found, -32602 invalid params."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


URI_COLLECTIONS = "mysearch://collections"
URI_SAVED = "mysearch://saved/"
URI_DOC = "mysearch://doc/"
_SAVED_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DOCID = re.compile(r"[0-9a-f]{8}")

RESOURCE_TEMPLATES = [
    {"uriTemplate": URI_SAVED + "{id}", "name": "saved-search", "title": "Saved search",
     "description": "A search the user saved in MySearch: its name, query and scope, and its results now. The ids are listed "
                    "by resources/list.", "mimeType": "text/plain"},
    {"uriTemplate": URI_DOC + "{docid}", "name": "document", "title": "Document by #docid",
     "description": "The extracted text of one document, by the 8-character #docid that search results show (without the "
                    "#), as the `get` tool returns it.", "mimeType": "text/plain"},
]


def _saved_allowed(saved: dict, lock: Lock | None) -> bool:
    """Whether this session may see a saved search. Unlocked: all of them. Locked: only those whose scope (a scope shortcut in
    the query wins over the collection field, as when the app runs it) is nothing but the locked collection. One that names
    no scope means every collection, so it is hidden, and so is one with an unknown name or a folder outside the lock."""
    if lock is None:
        return True
    if lock.error:
        return False
    try:
        lock.check_folders(saved["query"])
    except PermissionError:
        return False
    found = config.scope_shortcut(saved["query"], config.load())
    if found["unknown"]:
        return False
    tokens = found["tokens"] or config.scope_tokens(saved.get("collection"))
    return bool(tokens) and lock.only_mine(",".join(tokens))


def _saved_args(saved: dict, lock: Lock | None) -> dict:
    """The `search` arguments that run a saved search. A scope shortcut in its query is left to call_tool; otherwise its
    collection field is the scope, and none means every collection (as in the app), which a locked session never gets."""
    args = {"query": saved["query"]}
    if not config.scope_shortcut(saved["query"], config.load())["tokens"]:
        coll = saved.get("collection") or (None if lock is not None else "all")
        if coll:
            args["collection"] = coll
    return args


def _guard_echo(text: str) -> str:
    """The first line of a results text repeats the query ('N results for "<query>" in <where>'). For a saved search that query
    is the user's, not the agent's own, so its identifiers are masked like any other value from the user's settings."""
    first, nl, rest = text.partition("\n")
    head, sep, tail = first.rpartition('" in ')
    pre, quote, query = head.partition('"')
    if not (sep and quote):
        return text
    return pre + quote + _guard(query) + sep + tail + nl + rest


def list_resources(lock: Lock | None) -> list[dict]:
    on = _redact_on()
    out = [{"uri": URI_COLLECTIONS, "name": "collections", "title": "Indexed folders",
            "description": "The indexed folders, what they contain and the saved groups of folders (what the `collections` "
                           "tool returns).", "mimeType": "text/plain"}]
    for saved in config.saved_searches():
        if not _SAVED_ID.fullmatch(saved["id"]) or not _saved_allowed(saved, lock):
            continue
        out.append({"uri": URI_SAVED + saved["id"], "name": "saved-" + saved["id"], "title": _guard(saved["name"], on=on),
                    "description": ("Saved search: " + _guard(saved["query"], on=on))[:200], "mimeType": "text/plain"})
    return out


def read_resource(uri, client, cwd: str | None, lock: Lock | None) -> dict:
    """resources/read: a thin wrapper over call_tool, so the lock, the confinement check and the masking apply unchanged.
    A resource outside the lock answers exactly like one that does not exist (-32002)."""
    if not isinstance(uri, str) or not uri:
        raise RpcError(-32602, "uri is required")
    missing = RpcError(-32002, f"resource not found: {uri}")
    try:
        if uri == URI_COLLECTIONS:
            text = call_tool("collections", {}, client, cwd, lock)
        elif uri.startswith(URI_SAVED):
            sid = uri[len(URI_SAVED):]
            saved = config.get_saved(sid) if _SAVED_ID.fullmatch(sid) else None
            if saved is None or not _saved_allowed(saved, lock):
                raise missing
            head = [f"# Saved search: {_guard(saved['name'])}", f"query: {_guard(saved['query'])}",
                    "collection: " + (saved.get("collection") or "(none: every collection)"), ""]
            try:
                body = _guard_echo(call_tool("search", _saved_args(saved, lock), client, cwd, lock))
            except ValueError as e:                  # e.g. a collection the saved search names no longer exists
                body = f"(this saved search could not be run: {e})"
            text = "\n".join(head) + "\n" + body
        elif uri.startswith(URI_DOC):
            docid = uri[len(URI_DOC):]
            if not _DOCID.fullmatch(docid):
                raise RpcError(-32602, "invalid docid: expected 8 lower-case hexadecimal characters (mysearch://doc/<docid>)")
            text = call_tool("get", {"ref": "#" + docid, "max_chars": 20000}, client, cwd, lock)
            if text.startswith("not found: "):
                raise missing
        else:
            raise missing
    except PermissionError as e:
        if lock is not None and lock.error:
            raise RpcError(-32002, str(e)) from None        # nothing is allowed at all: say why
        raise missing from None
    return {"contents": [{"uri": uri, "mimeType": "text/plain", "text": text}]}


_ARG_COLLECTION = {"name": "collection", "required": False,
                   "description": "Collection, group or alias to look in (default: the collection of the working directory)"}
PROMPTS = [
    {"name": "find-document", "title": "Find a document",
     "description": "Find one document from a description, optionally narrowed by kind and date, and report it with its citation.",
     "arguments": [{"name": "description", "required": True, "description": "What the document is about, in your own words"},
                   {"name": "type", "required": False, "description": "Kind of document: " + ", ".join(DOCTYPES)},
                   {"name": "date", "required": False, "description": "Year, month or day it is dated: YYYY, YYYY-MM or YYYY-MM-DD"}]},
    {"name": "monthly-summary", "title": "Summarise a month",
     "description": "Summarise the documents dated in one month: how many, of which kinds, the amounts and the main parties.",
     "arguments": [_ARG_COLLECTION, {"name": "month", "required": True, "description": "The month: YYYY-MM"}]},
    {"name": "missing-receipts", "title": "Months without receipts",
     "description": "List the months of a year in which no receipt was found.",
     "arguments": [_ARG_COLLECTION, {"name": "year", "required": True, "description": "The year: YYYY"}]},
]


def _prompt_scope(vals: dict, lock: Lock | None) -> tuple[str, str]:
    """(the collection argument, the sentence part that passes it to the tools). Under the lock only the locked collection."""
    coll = vals.get("collection", "")
    if coll and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._,:-]{0,99}", coll):
        raise ValueError("collection: use a collection, group or alias name, a comma list or 'all'")
    if lock is not None:
        if lock.error:
            raise ValueError(lock.error)
        if coll and not lock.only_mine(coll):
            raise ValueError(f"{lock.refusal} Leave `collection` out or use '{lock.name}'.")
    return coll, (f' and `collection` "{coll}"' if coll else "")


def _prompt_find_document(vals: dict, lock: Lock | None) -> str:
    desc = re.sub(r"\s+", " ", vals["description"])[:500]
    kinds = _doctype_list("type", vals["type"]) if vals.get("type") else []
    if len(kinds) > 1:
        raise ValueError("type: give one kind of document")
    date = _check_date("date", vals["date"]) if vals.get("date") else None
    filters = (f', `doctype` ["{kinds[0]}"]' if kinds else "") + (f', `docdate_from` and `docdate_to` "{date}"' if date else "")
    return (f"Find the document described below in the user's indexed folders.\n\nDescription: {desc}\n\n"
            f"1. Call `search` with a short query made of the most distinctive words of the description (names, numbers, "
            f"places){filters}. English queries also find German documents.\n"
            "2. If nothing fits, relax one filter at a time (first the date, then the kind) and search again.\n"
            "3. Open the best candidates with `get` (by #docid) and check each one against the description.\n"
            "4. Answer with the path, the #docid and the `cite:` line of the best match, and say how sure you are. If nothing "
            "matches, say so and list what you tried. Do not guess.")


def _prompt_monthly_summary(vals: dict, lock: Lock | None) -> str:
    month = _check_date("month", vals["month"])
    if len(month) != 7:
        raise ValueError("month: use YYYY-MM")
    _, scope = _prompt_scope(vals, lock)
    return (f"Summarise the documents dated {month} in the user's indexed folders.\n\n"
            f'1. Call `timeline` with `from` and `to` set to "{month}"{scope}. Note the number of documents and the amounts per '
            "currency. A month with no documents is a result, not an error.\n"
            f'2. Call `search` with an empty `query`, `docdate_from` and `docdate_to` set to "{month}"{scope}, and `limit` 50. '
            "Each result's `meta:` line gives its kind, date, amount, party and reference. Repeat with `doctype` set to one "
            "kind when there are more than 50 documents.\n"
            f"3. Call `facets`{scope.replace(' and ', ' with ', 1)} once to see the usual kinds, parties and currencies; it "
            "covers every month and takes no date range, so use it only to put this month in context.\n"
            "4. Write a short summary: documents per kind, totals per currency, the main parties and anything unusual. Use only "
            "what the tools returned, name documents with their `cite:` line, and say which figures are incomplete (documents "
            "without an amount or a date are not counted).")


def _prompt_missing_receipts(vals: dict, lock: Lock | None) -> str:
    year = _check_date("year", vals["year"])
    if len(year) != 4:
        raise ValueError("year: use YYYY")
    _, scope = _prompt_scope(vals, lock)
    return (f"Find the months of {year} in which the user's indexed folders hold no receipt.\n\n"
            f'1. Call `timeline` with `type` ["receipt"], `from` "{year}-01" and `to` "{year}-12"{scope}.\n'
            '2. Report the months listed after "months with no document:" (the gaps) and, for contrast, the months that do '
            "have receipts with their counts.\n"
            "3. Say how many classified receipts have no document date (the undated figure): they cannot be placed in a month, "
            "so when it is more than a few the list of gaps may be incomplete.\n"
            f"4. Before you call a gap empty, check it with `search` (a plain query such as \"receipt\", `docdate_from` and "
            f"`docdate_to` set to that month{scope}).")


_PROMPT_BUILDERS = {"find-document": _prompt_find_document, "monthly-summary": _prompt_monthly_summary,
                    "missing-receipts": _prompt_missing_receipts}


def get_prompt(name, arguments, lock: Lock | None) -> dict:
    """prompts/get. Missing or malformed arguments, an unknown prompt and a collection the lock does not allow are -32602."""
    spec = next((p for p in PROMPTS if p["name"] == name), None) if isinstance(name, str) else None
    if spec is None:
        raise RpcError(-32602, f"unknown prompt: {name}. Available: {', '.join(p['name'] for p in PROMPTS)}")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise RpcError(-32602, "arguments must be an object")
    unknown = sorted(set(arguments) - {a["name"] for a in spec["arguments"]})
    if unknown:
        raise RpcError(-32602, f"unknown argument {', '.join(unknown)} for prompt {name}")
    vals = {}
    for key, v in arguments.items():
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            raise RpcError(-32602, f"argument {key} must be text")
        if str(v).strip():
            vals[key] = str(v).strip()
    for a in spec["arguments"]:
        if a["required"] and a["name"] not in vals:
            raise RpcError(-32602, f"missing required argument: {a['name']}")
    try:
        text = _PROMPT_BUILDERS[name](vals, lock)
    except ValueError as e:
        raise RpcError(-32602, str(e)) from None
    return {"description": spec["description"], "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}


def handle(req: dict, client, cwd: str | None, lock: Lock | None = None) -> dict | None:
    method, rid = req.get("method"), req.get("id")
    if rid is None:            # notification
        return None
    params = req.get("params")
    params = params if isinstance(params, dict) else {}
    try:
        if method == "initialize":
            asked = params.get("protocolVersion")
            ver = asked if asked in SUPPORTED_PROTOCOLS else PROTOCOL     # a version we do not know gets ours
            result = {"protocolVersion": ver,
                      "capabilities": {"tools": {"listChanged": False},
                                       "resources": {"subscribe": False, "listChanged": False},
                                       "prompts": {"listChanged": False}},
                      "serverInfo": {"name": "mysearch", "title": "MySearch", "version": __version__},
                      "instructions": INSTRUCTIONS + (
                          "" if lock is None else f" This session is limited to the '{lock.name}' collection."
                          if lock.name else " " + lock.error)}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tools_for(lock)}
        elif method == "tools/call":
            p = params
            try:
                text = call_tool(p.get("name"), p.get("arguments") or {}, client, cwd, lock)
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            except PermissionError as e:
                result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
            except Exception as e:
                result = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
        elif method == "resources/list":
            result = {"resources": list_resources(lock)}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": RESOURCE_TEMPLATES}
        elif method == "resources/read":
            result = read_resource(params.get("uri"), client, cwd, lock)
        elif method == "prompts/list":
            result = {"prompts": PROMPTS}
        elif method == "prompts/get":
            result = get_prompt(params.get("name"), params.get("arguments"), lock)
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    except RpcError as e:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": e.message}}
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
