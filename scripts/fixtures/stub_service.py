#!/usr/bin/env python3
"""A stub of the MySearch service for the golden snapshots (scripts/snapshot_check.sh). Standard library only.

    stub_service.py --home <fixture home> --token <token> [--port 0]

It answers the GET routes that the snapshot states call, using only the fixture folder: the file-name search walks the
fixture's home, and the collections, status and Ask answers are fixed. Nothing here reads the Mac or the real service.

  * a request without the token (header X-MSE-Token, or ?t= on the page) gets 401;
  * a route the app asks for that this stub does not know is logged to stderr as "unknown route ..." and gets 404,
    so the check can fail when the app starts asking for something new;
  * the routes that the app is designed to see as "not installed" (the Ask providers list) get 404 without a log line.

The Ask states use two more fixed answers: GET /api/tree (the folder picker) and POST /api/ask (a finished answer as
application/x-ndjson, with fixed timings so the picture does not change). The name search pages as the service does:
with `limit` or `offset` in the request the answer also carries offset, limit, has_more and next_offset; without them it
is unchanged. POST /api/files/opened (a result the user opened) answers {"ok": true} and keeps nothing.

GET /api/settings answers the settings the app reads (search.bridge) with no "wholedisk" key, so the whole-disk UI stays
hidden. With --disk the stub also answers the whole-disk contract with fictional data: the "wholedisk" key of /api/settings,
GET /api/disk/status (fixed counts) and GET /api/disk/search (a fixed list, paged by limit and offset, filtered by kind).
POST /api/disk/rescan answers {"started": true} and POST /api/settings the settings, only with --disk; without it they are
unknown routes. The key sets and value domains are the service's own (tests/test_wholedisk_contract.py checks that).

The first line on stdout is PORT=<n>, once the server is listening. The server runs until it is stopped.
"""

import argparse
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from datetime import datetime, timezone
import json

NOT_INSTALLED = {"/api/ask/providers"}

# --disk: the whole-disk search, all fictional. The folders are under the placeholder home /Users/alex, the files are invented,
# and the snippets and labels are written for this check only.
# The keys and value domains are the service's (mse/wholedisk.py: settings(), Manager.status(), Manager.search());
# tests/test_wholedisk_contract.py compares this stub with the real service field by field.
DISK_SETTINGS = {"enabled": True, "roots": ["/Users/alex"], "extra_excludes": ["node_modules"], "priority": "balanced",
                 "images": True, "documents": True, "ocr_images": True, "max_text_kb": 8}
DISK_STATUS = {"enabled": True, "roots": ["/Users/alex"], "files_seen": 48213, "files_described": 31902,
               "images_labelled": 9127, "pdfs_described": 4411, "docs_described": 18102, "videos_described": 262,
               "failed": 0, "pending": 16311, "state": "describing", "paused_reason": None, "rate_per_s": 14.2,
               "eta_s": 1140, "last_error": None, "priority": "balanced", "last_sync": 1755165600.0,
               "last_reconcile": 1755162000.0, "name_index_ready": True}


def _epoch(year, month, day, hour=10, minute=0):
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp())


DISK_RESULTS = [
    {"path": "/Users/alex/Documents/Studio/zebra-notes.md", "name": "zebra-notes.md", "kind": "doc", "size": 4096,
     "mtime": _epoch(2025, 6, 21, 12), "snippet": "", "labels": [], "score": 0.99, "why": "name"},
    {"path": "/Users/alex/Pictures/Safari/zebra-crossing-2025.jpg", "name": "zebra-crossing-2025.jpg", "kind": "image",
     "size": 2481933, "mtime": _epoch(2025, 8, 14, 10, 2), "snippet": "", "labels": ["zebra", "animal", "grass"],
     "score": 0.91, "why": "label"},
    {"path": "/Users/alex/Pictures/Safari/zebra-savanna-wide.png", "name": "zebra-savanna-wide.png", "kind": "image",
     "size": 1904772, "mtime": _epoch(2025, 8, 14, 10, 4), "snippet": "", "labels": ["zebra", "savanna", "grass"],
     "score": 0.88, "why": "label"},
    {"path": "/Users/alex/Documents/Studio/Reports/Q2-field-notes.pdf", "name": "Q2-field-notes.pdf", "kind": "pdf",
     "size": 884230, "mtime": _epoch(2025, 7, 2, 9, 15),
     "snippet": "Field notes from the reserve: a zebra herd crossed the track at dusk; counts are provisional.",
     "labels": [], "score": 0.68, "why": "text"},
    {"path": "/Users/alex/Pictures/Savanna/IMG_2231.jpg", "name": "IMG_2231.jpg", "kind": "image", "size": 3120450,
     "mtime": _epoch(2025, 8, 16, 17, 40), "snippet": "", "labels": ["savanna", "grass", "tree"], "score": 0.74, "why": "label"},
    {"path": "/Users/alex/Documents/Angebote/Angebot_Gartenpflege_2026.pdf", "name": "Angebot_Gartenpflege_2026.pdf", "kind": "pdf",
     "size": 512007, "mtime": _epoch(2026, 3, 3, 8, 30),
     "snippet": "Angebot für die Gartenpflege der Anlage: Zahlungsziel 14 Tage, Größe der Fläche 420 m².",
     "labels": [], "score": 0.61, "why": "text"},
]


SHOWN_HOME = "/Users/alex"       # the placeholder the public audit allows in examples
KINDS = {".pdf": "PDF document", ".md": "Markdown", ".txt": "Plain text", ".png": "PNG image", ".swift": "Swift source"}
PAGE = (b"<!doctype html><meta charset=\"utf-8\"><title>Fixture</title>"
        b"<p>Fictional fixture page for the snapshot check.</p>")

# --docs: the document-understanding answers (search results with meta, facets, timeline, saved searches). Every name is
# invented: Acme-style companies, fictional invoice numbers, fictional folders under the fixture's collection "work".
DOC_COLLECTION = "work"
DOC_RESULTS = [
    # doctype, counterparty, doc date, amount, currency, reference, folder file, title, snippet
    ("invoice", "Acme Logistics GmbH", "2025-03-14", 1234.50, "EUR", "INV-2025-0412",
     "Invoices/2025/2025-03_invoice_acme-logistics.pdf", "Invoice INV-2025-0412",
     "Acme Logistics GmbH bills <mark>Acme</mark> Studio for freight, net 30 days."),
    ("invoice", "Acme Logistics GmbH", "2025-06-02", 580.00, "EUR", "INV-2025-0877",
     "Invoices/2025/2025-06_invoice_acme-logistics.pdf", "Invoice INV-2025-0877",
     "Second freight invoice from <mark>Acme</mark> Logistics, partial delivery."),
    ("receipt", "Globex Office Supplies", "2025-02-08", 42.90, "EUR", "",
     "Receipts/2025/2025-02_receipt_globex.pdf", "Receipt Globex Office Supplies",
     "Paper, toner and pens, paid by card at the counter."),
    ("invoice", "Contoso Print Ltd", "2025-05-19", 1890.00, "GBP", "CP-7781",
     "Invoices/2025/2025-05_invoice_contoso-print.pdf", "Invoice CP-7781",
     "Brochure print run, 2,000 copies, delivered to the studio."),
    ("bank_statement", "", "2025-04-30", None, "", "",
     "Statements/2025/2025-04_statement_bank.pdf", "Account statement April 2025",
     "Monthly statement: opening and closing balance, card payments listed by date."),
    ("contract", "Fabrikam Consulting", "2025-01-10", None, "", "",
     "Contracts/2025/2025-01_contract_fabrikam.pdf", "Consulting framework agreement",
     "Fabrikam Consulting provides advisory services; payment terms are net 14."),
    ("invoice", "Tailspin Cloud Services", "2025-07-01", 96.00, "USD", "TS-2025-31",
     "Invoices/2025/2025-07_invoice_tailspin.pdf", "Invoice TS-2025-31",
     "Hosting for July, billed in US dollars."),
    ("invoice", "Acme Logistics GmbH", "2024-11-20", 3500.00, "CHF", "INV-2024-1190",
     "Invoices/2024/2024-11_invoice_acme-logistics-zurich.pdf", "Invoice INV-2024-1190",
     "Zurich leg of the 2024 shipping programme, settled in francs."),
    (None, None, None, None, None, None,
     "Notes/2025-03-kickoff-acme.md", "Meeting notes: Acme kickoff",
     "Notes from the kickoff with <mark>Acme</mark>: scope, owners, next steps."),
]
DOC_TYPE_ORDER = ["invoice", "receipt", "bank_statement", "contract", "letter", "quote", "reminder", "payslip", "tax_document"]
DOC_TIMELINE = {       # per type: a count for each month of 2025 (January first); receipts have two gap months
    "invoice": [3, 2, 4, 1, 3, 2, 2, 1],
    "receipt": [2, 0, 1, 3, 0, 1, 4, 2],
    "bank_statement": [1, 1, 1, 1, 1, 1, 1, 1],
}
DOC_SAVED = [
    {"id": "f3a1c9e2", "name": "Acme invoices 2025", "query": "party:acme type:invoice docdate:2025",
     "collection": DOC_COLLECTION, "created_at": "2025-08-01T09:00:00Z", "updated_at": "2025-08-01T09:00:00Z"},
    {"id": "7b20d4aa", "name": "Receipts over 40 EUR", "query": "type:receipt amount:>40 currency:EUR",
     "collection": DOC_COLLECTION, "created_at": "2025-08-03T09:00:00Z", "updated_at": "2025-08-03T09:00:00Z"},
]


# Ask (all states): GET /api/tree answers the folder picker from this fixed fictional tree (collection -> folder path ->
# [(name, has subfolders)]); POST /api/ask answers every question with the same short answer, in the event order of
# mse/ask.py, with fixed timings.
ASK_TREE = {
    "work": {
        "": [("Clients", True), ("Contracts", False), ("Invoices", True), ("Notes", False), ("Projects", False)],
        "Clients": [("Acme Logistics", True), ("Contoso Print", False), ("Fabrikam Consulting", False),
                    ("Globex Office Supplies", False), ("Tailspin Cloud Services", False)],
        "Clients/Acme Logistics": [("2024", False), ("2025", False)],
    },
    "notes": {"": [("Meetings", False), ("Plans", False)]},
}
ASK_MODEL = "fixture-local-small"
ASK_SOURCES = [
    {"n": 1, "docid": "#a1b2c3", "collection": "work", "path": "Contracts/2025/2025-01_contract_acme-logistics.pdf",
     "title": "Freight services agreement, Acme Logistics", "page": 2, "ts": None,
     "snippet": "Payment is due within 30 days of the invoice date. Late payments accrue interest at 1 % per month."},
    {"n": 2, "docid": "#d4e5f6", "collection": "work", "path": "Invoices/2025/2025-03_invoice_acme-logistics.pdf",
     "title": "Invoice INV-2025-0412", "page": 1, "ts": None,
     "snippet": "Acme Logistics GmbH bills Acme Studio for freight, net 30 days."},
]
ASK_TOKENS = ["Payment is due within 30 days of the invoice date [1]. ",
              "Late payments accrue interest at 1 % per month [1]. ",
              "The March invoice repeats the net 30 days term [2]."]


# The most rows a paged answer reaches in all (limit + offset), as mse/filequery.py MAX_LIMIT.
PAGE_MAX_LIMIT = 1000


def _fold(s):
    return (s or "").lower()


def _doc_filters(q):
    """The document filters and free words of a query, the way the app writes them (type:/doctype:, party:, currency:,
    amount:, docdate:). Quoted values are unquoted. Only what the fixture needs; the real parser is in mse/search.py."""
    import shlex
    try:
        tokens = shlex.split(q)
    except ValueError:
        tokens = q.split()
    filters, words = {}, []
    for t in tokens:
        key, sep, value = t.partition(":")
        if sep and key.lower() in ("type", "doctype", "party", "currency", "amount", "docdate", "after", "before",
                                   "include", "exclude", "in", "kind", "ext", "path"):
            filters.setdefault(key.lower(), []).append(value)
        else:
            words.append(t)
    return filters, words


def _amount_ok(amount, spec):
    if amount is None:
        return False
    try:
        # the app writes ">=N" for a custom "From N" amount and "<=N" for "To N"; ">N", "<N" and "=N" also arrive
        for op, test in ((">=", lambda a, n: a >= n), ("<=", lambda a, n: a <= n),
                         (">", lambda a, n: a > n), ("<", lambda a, n: a < n), ("=", lambda a, n: a == n)):
            if spec.startswith(op):
                return test(amount, float(spec[len(op):]))
        if ".." in spec:
            lo, hi = spec.split("..", 1)
            return float(lo or 0) <= amount <= float(hi or "inf")
        return amount == float(spec)
    except ValueError:
        return False  # an unparseable amount matches nothing instead of crashing the stub


class Stub:
    def __init__(self, home, token, docs=False, disk=False):
        self.home = os.path.realpath(home)
        self.token = token
        self.docs = docs
        self.disk = disk
        self.index = self._walk()
        self.lock = threading.Lock()
        self.unknown = []

    def shown(self, path):
        """The path the stub reports: the fixture's home is shown as the placeholder /Users/alex, so the scratch folder
        the fixture lives in never reaches a picture. Paths outside the home are never reported."""
        rel = os.path.relpath(path, self.home)
        return SHOWN_HOME + ("" if rel == "." else "/" + rel)

    def _walk(self):
        """Every file and folder under the fixture home, sorted, as the search answers describe them."""
        records = []
        for dirpath, dirnames, filenames in os.walk(self.home):
            dirnames.sort()
            filenames.sort()
            for name, is_dir in [(d, True) for d in dirnames] + [(f, False) for f in filenames]:
                path = os.path.join(dirpath, name)
                st = os.stat(path)
                ext = os.path.splitext(name)[1].lower()
                records.append({
                    "path": self.shown(path),
                    "name": name,
                    "parent": self.shown(dirpath),
                    "is_dir": is_dir,
                    "kind": "Folder" if is_dir else KINDS.get(ext, "Document"),
                    "size": 0 if is_dir else st.st_size,
                    "mtime": int(st.st_mtime),
                    "collection": None,
                })
        return records

    def search(self, q, limit, offset=0, paged=False):
        needle = q.lower()
        hits = []
        for rec in self.index:
            low = rec["name"].lower()
            at = low.find(needle) if needle else -1
            if at < 0:
                continue
            item = dict(rec)
            item["score"] = 1.0
            item["match"] = [[at, at + len(needle)]]
            hits.append(item)
        if not paged:
            return {"searchable": True, "items": hits[:limit], "total": len(hits), "ms": 4}
        # A paged request (`limit` or `offset` in the request) is answered as mse/server.py does: the window of the list
        # that starts at `offset`, and offset, limit, has_more and next_offset (null when there is no more).
        limit = max(1, min(PAGE_MAX_LIMIT, limit))
        offset = max(0, offset)
        limit = min(limit, max(0, PAGE_MAX_LIMIT - offset))
        page = hits[offset:offset + limit]
        more = offset + len(page) < len(hits)
        return {"searchable": True, "items": page, "total": len(hits), "ms": 4, "offset": offset, "limit": limit,
                "has_more": more, "next_offset": offset + len(page) if more else None}

    def collections(self):
        work = self.shown(os.path.join(self.home, "Documents", "Studio"))
        notes = self.shown(os.path.join(self.home, "Documents", "Notes"))
        # --docs only: one fictional group (the notes folder), so the Indexed sidebar has a group row to show (groups-sidebar).
        groups = [{"name": "reference", "label": "Reference", "collections": ["notes"], "aliases": ["ref"],
                   "hidden": False, "files": 2}] if self.docs else []
        return {"collections": [
            {"name": "work", "path": work, "enabled": True, "exists": True, "stats": {"files": 9}},
            {"name": "notes", "path": notes, "enabled": True, "exists": True, "stats": {"files": 2}},
        ], "groups": groups}

    def answer(self, route, query):
        """(status, body) for a GET route; body is a dict, or None for a 404 with the not-found text."""
        if route == "/api/status":
            return 200, {"version": "0.1.2", "uptime_s": 3600,
                         "ask": {"available": True, "enabled": True, "route": "local",
                                 "model": "fixture-local-small", "local_model": "fixture-local-small",
                                 "online": False, "local_fallback": True}}
        if route == "/api/collections":
            return 200, self.collections()
        if route == "/api/files/search":
            q = (query.get("q") or [""])[0]
            limit = int((query.get("limit") or ["200"])[0])
            offset = int((query.get("offset") or ["0"])[0])
            return 200, self.search(q, limit, offset, paged=("limit" in query or "offset" in query))
        if route == "/api/search":
            if self.docs:
                return 200, self.doc_search((query.get("q") or [""])[0])
            return 200, {"results": [], "total": 0, "ms": 3}
        if route == "/api/saved":
            return 200, {"saved": DOC_SAVED if self.docs else []}
        if route == "/api/docs/facets":
            return 200, self.doc_facets() if self.docs else {
                "collections": [], "files": 0, "classified": 0, "unclassified": 0, "undated": 0,
                "doctypes": [], "counterparties": [], "months": [], "currencies": []}
        if route == "/api/docs/timeline":
            types = [t for v in query.get("type", []) for t in v.split(",") if t]
            return 200, self.doc_timeline(types) if self.docs else {
                "collections": [], "types": types, "granularity": "month", "from": None, "to": None,
                "first": None, "last": None, "buckets": [], "gaps": [], "total": 0, "undated": 0}
        if route == "/api/files/status":
            return 200, {"enabled": True, "crawling": False, "searchable": True, "progress": None}
        if route == "/api/index/progress":
            return 200, {"folders": {}}
        if route == "/api/ask/local-models":
            return 200, {"current": "fixture-local-small",
                         "models": [{"name": "fixture-local-small", "size_mb": 1200}]}
        if route == "/api/tree":
            return 200, self.tree((query.get("c") or [""])[0], (query.get("path") or [""])[0])
        if route == "/api/settings":
            body = {"search": {"bridge": True}}
            if self.disk:
                body["wholedisk"] = dict(DISK_SETTINGS)
            return 200, body
        if route == "/api/disk/status" and self.disk:
            return 200, dict(DISK_STATUS)
        if route == "/api/disk/search" and self.disk:
            q = (query.get("q") or [""])[0]
            limit = int((query.get("limit") or ["25"])[0])
            offset = int((query.get("offset") or ["0"])[0])
            return 200, self.disk_search(q, limit, offset, (query.get("kind") or [""])[0])
        return None, None

    def disk_search(self, q, limit, offset, kind):
        """One page of the fictional whole-disk results. Any non-empty text finds the whole list (the picture needs the
        rows, not a ranking), filtered by kind when one is asked; has_more and next_offset page it as the service does."""
        rows = [] if not q.strip() else [r for r in DISK_RESULTS if not kind or r["kind"] == kind]
        page = rows[offset:offset + limit]
        more = offset + limit < len(rows)
        words = [[w.lower()] for w in q.split() if w.strip()]
        return {"results": page, "has_more": more, "next_offset": offset + len(page) if more else None, "took_ms": 6,
                "query": {"words": words, "kinds": [kind] if kind else []}}

    def tree(self, collection, path):
        """/api/tree: the subfolders of a folder of the collection, from the fixed fictional ASK_TREE (an unknown
        collection or folder has none). The fixture's own folders are not walked: the picker needs two levels."""
        name, rel = collection.lower(), path.strip("/")
        rows = ASK_TREE.get(name, {}).get(rel, [])
        return {"collection": name, "path": rel,
                "folders": [{"name": n, "path": (rel + "/" if rel else "") + n, "children": c} for n, c in rows]}

    def ask_events(self, body):
        """POST /api/ask: the NDJSON of one finished answer, in the order mse/ask.py yields it (steps, sources, the model
        step, tokens, done). The question and the scope come from the request; the timings are fixed."""
        q = str(body.get("q") or "").strip()
        scope = " + ".join(body.get("collections") or []) or "All"
        answer = "".join(ASK_TOKENS)
        events = [
            {"type": "step", "kind": "search", "text": "Searching %s for \u201c%s\u201d" % (scope, q)},
            {"type": "step", "kind": "search", "text": "Found 3 results", "count": 3, "ms": 14},
            {"type": "step", "kind": "read", "text": "Reading 2 passages from 2 documents", "count": 2},
            {"type": "sources", "sources": ASK_SOURCES},
            {"type": "step", "kind": "model", "text": "Asking the local model %s" % ASK_MODEL},
        ] + [{"type": "token", "text": t} for t in ASK_TOKENS] + [
            {"type": "done", "citations": [1, 2], "model": ASK_MODEL, "provider": "local", "where": "local",
             "redacted": 0, "timings": {"ttft_ms": 420, "total_ms": 3100}, "answer": answer, "answered": True,
             "dropped_citations": 0},
        ]
        return ("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n").encode("utf-8")

    def doc_search(self, q):
        """/api/search with --docs: the fictional documents that match the query's document filters and words, each with
        its meta (null for a note), the facets of the answer and the filters it echoes back."""
        filters, words = _doc_filters(q)
        types = [v for key in ("type", "doctype") for v in filters.get(key, []) if v in DOC_TYPE_ORDER or v == "other"]
        parties = [_fold(v) for v in filters.get("party", [])]
        currencies = [v.upper() for v in filters.get("currency", [])]
        dates = filters.get("docdate", [])
        amounts = filters.get("amount", [])
        items = []
        for row in DOC_RESULTS:
            doctype, cp, date, amount, cur, ref, rel, title, snippet = row
            haystack = _fold(" ".join([os.path.basename(rel), title, cp or "", snippet.replace("<mark>", "").replace("</mark>", "")]))
            if types and doctype not in types:
                continue
            if parties and not any(p in _fold(cp) for p in parties):
                continue
            if currencies and cur not in currencies:
                continue
            if dates and not any((d.split("..", 1)[0] <= date <= (d.split("..", 1)[1] or "9999") if ".." in d
                                  else date.startswith(d)) for d in dates):
                continue
            if amounts and not all(_amount_ok(amount, a) for a in amounts):
                continue
            if words and not all(_fold(w) in haystack for w in words):
                continue
            if not (types or parties or currencies or dates or amounts or words):
                continue
            meta = None if doctype is None else {
                "doctype": doctype, "confidence": 0.92, "doc_date": date, "amount": amount,
                "currency": cur or None, "counterparty": cp or None, "reference": ref or None}
            items.append({
                "abs_path": self.shown(os.path.join(self.home, "Documents", "Studio", rel)), "path": rel,
                "name": os.path.basename(rel), "title": title,
                "kind": "md" if rel.endswith(".md") else "pdf", "date": date, "size": 48211,
                "collection": DOC_COLLECTION, "hits": [{"snippet": snippet}], "why": ["keyword"],
                "relevance": 92 - 3 * len(items), "meta": meta})
        kinds, years = {}, {}
        for it in items:
            kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
            if it["date"]:     # an undated note (date None) is listed, but has no year to count
                years[it["date"][:4]] = years.get(it["date"][:4], 0) + 1
        doctypes = {}
        for it in items:
            if it["meta"]:
                doctypes[it["meta"]["doctype"]] = doctypes.get(it["meta"]["doctype"], 0) + 1
        return {"results": items, "total": len(items), "ms": 7, "browse": not words,
                "facets": {"doctype": doctypes, "kind": kinds, "year": years, "collection": {DOC_COLLECTION: len(items)}},
                "filters": {k: v for k, v in filters.items()}, "bridge": {}}

    def doc_facets(self):
        """/api/docs/facets with --docs: the counts of the fictional collection (the same numbers as the timeline)."""
        return {
            "collections": [{"name": DOC_COLLECTION, "files": 42}], "files": 42, "classified": 39, "unclassified": 3,
            "undated": 1,
            "doctypes": [{"doctype": "invoice", "count": 18}, {"doctype": "receipt", "count": 13},
                         {"doctype": "bank_statement", "count": 8}, {"doctype": "contract", "count": 3},
                         {"doctype": "letter", "count": 5}, {"doctype": "quote", "count": 2},
                         {"doctype": "tax_document", "count": 2}, {"doctype": "reminder", "count": 1}],
            "counterparties": [{"name": "Acme Logistics GmbH", "count": 6}],
            "months": [], "currencies": [{"currency": "EUR", "count": 25}, {"currency": "USD", "count": 4}]}

    def doc_timeline(self, types):
        """/api/docs/timeline with --docs: 2025-01 to 2025-08, one bucket per month, with the gap months of the type."""
        chosen = [t for t in types if t in DOC_TIMELINE] or list(DOC_TIMELINE)
        buckets, gaps = [], []
        for i in range(8):
            month = "2025-%02d" % (i + 1)
            n = sum(DOC_TIMELINE[t][i] for t in chosen)
            amounts = {}
            if n:
                amounts["EUR"] = round(n * 118.25, 2)
                if "invoice" in chosen and i % 2:
                    amounts["USD"] = round(96.0 * (i % 3 + 1), 2)
            buckets.append({"month": month, "count": n, "amounts": amounts,
                            "with_amount": 0 if chosen == ["bank_statement"] else n,
                            "query": ("type:%s " % ",".join(types) if types else "") + "docdate:" + month})
            if n == 0:
                gaps.append(month)
        return {"collections": [DOC_COLLECTION], "types": types, "granularity": "month", "from": "2025-01",
                "to": "2025-08", "first": "2025-01", "last": "2025-08", "buckets": buckets, "gaps": gaps,
                "total": sum(b["count"] for b in buckets), "undated": 1}

    def log_unknown(self, method, route):
        with self.lock:
            self.unknown.append(route)
        sys.stderr.write("stub: unknown route %s %s\n" % (method, route))
        sys.stderr.flush()


def make_handler(stub):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MySearchStub/1"

        def log_message(self, fmt, *args):      # keep the log to the explicit lines only
            pass

        def _send(self, status, body, content_type="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            route = parts.path
            if route == "/":
                if query.get("t", [""])[0] != stub.token:
                    return self._send(401, {"error": "unauthorized"})
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if self.headers.get("X-MSE-Token", "") != stub.token:
                return self._send(401, {"error": "unauthorized"})
            status, body = stub.answer(route, query)
            if status is not None:
                return self._send(status, body)
            if route not in NOT_INSTALLED:
                stub.log_unknown("GET", route)
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if urlsplit(self.path).path == "/api/ask":     # a question only reads: it gets the fixed answer of ask_events
                if self.headers.get("X-MSE-Token", "") != stub.token:
                    return self._send(401, {"error": "unauthorized"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}") if length > 0 else {}
                except (ValueError, OSError):
                    body = {}
                return self._send(200, stub.ask_events(body if isinstance(body, dict) else {}),
                                  "application/x-ndjson")
            if urlsplit(self.path).path == "/api/files/opened":    # a result the user opened: nothing is kept, the answer is fixed
                if self.headers.get("X-MSE-Token", "") != stub.token:
                    return self._send(401, {"error": "unauthorized"})
                try:
                    self.rfile.read(int(self.headers.get("Content-Length") or 0))
                except (ValueError, OSError):
                    pass
                return self._send(200, {"ok": True})
            if stub.disk and urlsplit(self.path).path in ("/api/disk/rescan", "/api/settings"):
                if self.headers.get("X-MSE-Token", "") != stub.token:
                    return self._send(401, {"error": "unauthorized"})
                try:
                    self.rfile.read(int(self.headers.get("Content-Length") or 0))
                except (ValueError, OSError):
                    pass
                # the service's own answers: {"started": true} for a rescan, the settings (as GET sends them) for a save
                if urlsplit(self.path).path == "/api/disk/rescan":
                    return self._send(200, {"started": True})
                return self._send(200, {"search": {"bridge": True}, "wholedisk": dict(DISK_SETTINGS)})
            # the snapshot states never write to the service; a POST is always unknown here
            stub.log_unknown("POST", urlsplit(self.path).path)
            return self._send(404, {"error": "not found"})

    return Handler


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--home", required=True, help="the fixture's home folder (make_snapshot_tree.sh)")
    ap.add_argument("--token", required=True, help="the token the app sends as X-MSE-Token")
    ap.add_argument("--port", type=int, default=0, help="0 picks a free port")
    ap.add_argument("--docs", action="store_true",
                    help="answer the document routes (search results with meta, facets, timeline, saved searches) with fictional data")
    ap.add_argument("--disk", action="store_true",
                    help="answer the whole-disk routes (the wholedisk settings, /api/disk/status, /api/disk/search) with fictional data")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.home):
        sys.stderr.write("stub: no such folder: %s\n" % args.home)
        return 2
    stub = Stub(args.home, args.token, docs=args.docs, disk=args.disk)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(stub))
    server.daemon_threads = True
    print("PORT=%d" % server.server_address[1], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
