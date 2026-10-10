"""The MySearch daemon: JSON API, the web UI, MCP over HTTP, background jobs. Listens on 127.0.0.1 only."""

import errno
import hmac
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, config, indexer, macindex, paths, search, store, tree
from . import progress as liveprog
from .embedder import pool
from .jobs import JobManager

JOBS: JobManager | None = None
STARTED = time.time()


def log(msg: str) -> None:
    paths.LOGS.mkdir(parents=True, exist_ok=True)
    with open(paths.LOGS / "daemon.log", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def rss_mb(pid: int | None = None) -> float | None:
    try:
        out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid or os.getpid())])
        return round(int(out) / 1024, 1)
    except Exception:
        return None


def collections_summary() -> list[dict]:
    cfg = config.load()
    out = []
    for c in cfg["collections"]:
        info = {k: c.get(k) for k in ("name", "path", "description", "in_global", "enabled", "exclude",
                                       "metadata_only", "contexts", "media", "created", "ask_cloud")}
        info["aliases"] = config.aliases_of(c)
        info["exists"] = Path(c["path"]).is_dir()
        try:
            conn = store.connect(c["name"], readonly=True)
            info["stats"] = store.stats(conn)
            conn.close()
        except FileNotFoundError:
            info["stats"] = None
        out.append(info)
    return out


# ---------------------------------------------------------------- groups and search scope (see config.resolve_scope)

def groups_summary(colls: list[dict] | None = None) -> list[dict]:
    """[{name, label, collections, files, aliases, hidden}]; files = the members' file counts added up (an empty
    group shows 0); hidden = no tab of its own (it still works by name, alias and in the scope menu)."""
    cfg = config.load()
    if colls is None:
        colls = collections_summary() if cfg.get("groups") else []
    files = {c["name"]: ((c.get("stats") or {}).get("files") or 0) for c in colls}
    return [{"name": g["name"], "label": g["label"], "collections": list(g["collections"]),
             "files": sum(files.get(m, 0) for m in g["collections"]),
             "aliases": config.aliases_of(g), "hidden": bool(g.get("hidden"))} for g in cfg.get("groups") or []]


def scope_param(values) -> list[str] | None:
    """A scope from the API (c=work,notes&c=journal): its names, validated (unknown -> ValueError, a 400),
    or None for All. Groups and "all" are expanded later, inside search.search()."""
    tokens = config.scope_tokens(values)
    if not tokens:
        return None
    config.resolve_scope(config.load(), tokens)
    return tokens


def query_scope(q: str, values, shortcuts: bool = True) -> tuple[str, list[str] | None, dict | None]:
    """Where a search or a question runs. A scope shortcut in the query ("w: invoice", "invoice in:work", see
    config.scope_shortcut) overrides `values` (the tab, dropdown or `c`); unknown names in either are a 400.
    -> (the query without the shortcut, the scope names or None for All, {"names", "tokens"} when one was used).
    shortcuts=False (the CLI and MCP clients, which split the query themselves) takes the query as it is."""
    if shortcuts and q:
        s = config.scope_shortcut(q, config.load(), strict=True)
        if s["tokens"]:
            return s["rest"], s["tokens"], {"names": s["names"], "tokens": s["tokens"]}
    return q, scope_param(values), None


def _flag(v, default: bool = True) -> bool:
    """A yes/no API parameter: "0", "false", "no", "off" and False are no; absent is the default."""
    if v is None:
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off")


def submit_scoped(kind: str, spec, path: str | None = None, reason: str = "manual") -> dict:
    """Queue a job for a collection, a group, a comma list or "all" (= every enabled collection, as for no scope)."""
    tokens = config.scope_tokens(spec)
    cfg = config.load()
    if path or len(tokens) == 1 and config.get_collection(cfg, tokens[0]):
        return JOBS.submit(kind, tokens[0] if tokens else None, path, reason=reason)
    if not tokens or tokens == ["all"]:
        return JOBS.submit(kind, None, None, reason=reason)
    jobs = []
    for name in config.resolve_scope(cfg, tokens)["collections"]:
        jobs += [j for j in JOBS.submit(kind, name, None, reason=reason)["jobs"] if j not in jobs]
    return {"jobs": jobs}


def files_search(q: str, scope: str = "mac", names=None, limit=None) -> dict:
    """The whole-Mac file-name search (GET /api/files/search, `mse files`): see Handler.get_api_files_search.
    ValueError (a 400 at the API) for an unknown scope or folder name and for a bad query."""
    from .filequery import DEFAULT_LIMIT, MAX_LIMIT
    cfg = config.load()
    if scope not in ("mac", "indexed"):
        raise ValueError("scope must be mac or indexed")
    names = config.scope_tokens(names)
    s = config.scope_shortcut(q, cfg, strict=True)
    shortcut = None
    if s["tokens"]:
        q, names, scope = s["rest"], s["tokens"], "indexed"
        shortcut = {"names": s["names"], "tokens": s["tokens"]}
    enabled = [c for c in cfg["collections"] if c.get("enabled", True)]
    roots = None
    if scope == "indexed":
        if not names or "all" in names:
            chosen = [c["name"] for c in enabled]
        else:
            chosen = config.resolve_scope(cfg, names, strict=True)["collections"]
        roots = [c["path"] for c in enabled if c["name"] in chosen] or ["/nonexistent-mysearch-scope"]
    n = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    res = macindex.manager().search(q, n, roots, enabled)
    res["scope"] = scope
    if shortcut:
        res["shortcut"] = shortcut
    return res


def ui_version() -> str:
    """Changes whenever an interface file changes; open windows reload themselves when it does."""
    return str(int(max(f.stat().st_mtime for f in paths.UI.iterdir() if f.is_file())))


def status() -> dict:
    cfg = config.load()
    return {
        "version": __version__, "uptime_s": round(time.time() - STARTED), "pid": os.getpid(),
        "daemon_rss_mb": rss_mb(), "workers": pool().status(), "jobs": JOBS.status() if JOBS else None,
        "model": cfg["model"]["path"].rsplit("/", 1)[-1], "model_present": Path(cfg["model"]["path"]).exists(),
        "helper_present": paths.HELPER.exists(), "port": cfg["port"], "ui_version": ui_version(),
        "idle_unload_min": cfg["worker"]["idle_unload_min"],
        "rerank_enabled": cfg["search"].get("rerank_enabled", False),
        "asr_present": Path((cfg["model"].get("asr") or {}).get("path", "")).is_dir(),
        "mse_bin": _mse_bin(), "data_dir": str(paths.DATA),
        "ask": _ask_status(),
        "reranker_adapter": _reranker_adapter(),
        "progress": liveprog.snapshot(),
    }


def _reranker_adapter() -> str | None:
    """The learned re-ranker adapter in effect (finetune.py's CURRENT pointer); None: the base model."""
    try:
        from .finetune import active_id
        return active_id()
    except Exception:
        return None


def _ask_status() -> dict:
    """{"available", "enabled", "loaded", "local_model", "ram_mb", "route", "provider", "model", "online",
    "where_next", "next_model"} for the Settings toggle and the answer panel (ask.status)."""
    try:
        from . import ask
        return ask.status()
    except Exception as e:
        return {"available": False, "enabled": False, "loaded": False, "local_model": None, "ram_mb": None,
                "route": None, "provider": None, "model": None, "online": None, "where_next": "local",
                "next_model": None, "error": f"{type(e).__name__}: {e}"}


def _mse_bin() -> str:
    from .agents_setup import mse_bin
    return mse_bin()


# Files from the indexed folders (/api/file, /api/thumb). A browser that shows one of them as a document can run its
# script in this origin, where the token lives. So only the kinds the web UI shows are sent as themselves: PDF (its
# iframe preview), images, audio and video. Everything else, SVG and XHTML included, is sent as plain text.
# The sandbox directive is left off PDFs: it turns off the plugin that draws the PDF inside the preview frame. On an
# image, audio or video response the policy has no effect on how the element shows or plays it.
FILE_CSP = "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'"


def served_type(path: Path) -> str:
    """The Content-Type for a file from an indexed folder: its own type for the kinds above, else text/plain."""
    ctype = mimetypes.guess_type(str(path))[0] or ""
    if ctype == "application/pdf" or ctype.startswith(("audio/", "video/")):
        return ctype
    if ctype.startswith("image/") and ctype != "image/svg+xml":
        return ctype
    return "text/plain; charset=utf-8"


def file_headers(ctype: str, extra: dict | None = None) -> dict:
    """The extra headers for a file response: the CSP, except on a PDF. nosniff is already on every response (_send)."""
    headers = dict(extra or {})
    if ctype != "application/pdf":
        headers["Content-Security-Policy"] = FILE_CSP
    return headers


class Handler(BaseHTTPRequestHandler):
    server_version = "MySearch"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # no access log: queries are private
        pass

    # ---------------------------------------------------------- plumbing

    def _send(self, code: int, body: bytes, ctype: str = "application/json", headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode())

    def _err(self, code: int, msg: str):
        self._json({"error": msg}, code)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 50 * 1024 * 1024:
            raise ValueError("request too large")
        return self.rfile.read(n) if n else b""

    def _jbody(self) -> dict:
        b = self._body()
        return json.loads(b) if b else {}

    def _allowed_host(self) -> bool:
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")

    def _authed(self, qs: dict) -> bool:
        """The token, from the X-MSE-Token header or the t query parameter. Compared in constant time, as bytes, so a
        missing or non-ASCII value is simply a refusal (401), and a service with no token configured refuses everything."""
        tok = config.load().get("token")
        if not tok:
            return False
        key = tok.encode("utf-8")
        supplied = (self.headers.get("X-MSE-Token"), (qs.get("t") or [None])[0])
        return any(s is not None and hmac.compare_digest(s.encode("utf-8"), key) for s in supplied)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = self.server.server_address[1]
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    # ---------------------------------------------------------- routing

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str):
        try:
            if not self._allowed_host():
                return self._err(403, "forbidden host")
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            p = u.path
            if method == "GET" and (p == "/" or p == "/index.html" or p.startswith("/settings")):
                # the page embeds the token (the web UI and Quick Search read it from there), so it is served only
                # to a request that already carries it: the t query parameter (what `mse ui` and the app send) or the header
                if not self._authed(qs):
                    return self._page_denied()
                return self._index()
            if method == "GET" and p.startswith("/static/"):          # the page's CSS, scripts and icon: no token in them
                return self._static(p[len("/static/"):])
            if p == "/favicon.ico":
                return self._static("icon.svg")
            if not (p.startswith("/api/") or p == "/mcp"):
                return self._err(404, "not found")
            if not self._authed(qs) or not self._origin_ok():
                return self._err(401, "missing or wrong token")
            handler = getattr(self, f"{method.lower()}_{p.strip('/').replace('/', '_').replace('-', '_')}", None)
            if handler is None:
                m = re.match(r"^/api/groups/([\w\-]+)/remove$", p)
                if m and method == "POST":
                    return self._group_remove(m.group(1))
                m = re.match(r"^/api/ask/providers/([\w\-]+)/(remove|models|test|connect)$", p)
                if m and method == "POST":
                    return self._provider_action(m.group(1), m.group(2))
                m = re.match(r"^/api/collections/([\w\-]+)/agents$", p)
                if m:
                    return self._agents_action(m.group(1), method)
                m = re.match(r"^/api/collections/([\w\-]+)(?:/(remove|update))?$", p)
                if m and method == "POST":
                    return self._collection_action(m.group(1), m.group(2) or "update")
                return self._err(404, "no such endpoint")
            return handler(qs)
        except (KeyError, ValueError, PermissionError) as e:
            return self._err(400, f"{type(e).__name__}: {e}")
        except BrokenPipeError:
            return
        except Exception as e:
            log(traceback.format_exc())
            return self._err(500, f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------- UI

    def _index(self):
        html = (paths.UI / "index.html").read_text()
        cfg = config.load()
        html = html.replace("{{TOKEN}}", cfg["token"]).replace("{{VERSION}}", __version__) \
                   .replace("{{UI}}", ui_version())
        # Referrer-Policy: the token is in this page's URL, so no link out of the page may send that URL on
        self._send(200, html.encode(), "text/html; charset=utf-8",
                   {"Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:;"
                                               " style-src 'self' 'unsafe-inline'; script-src 'self'; frame-src 'self';"
                                               " object-src 'self'; frame-ancestors 'none'",
                    "Referrer-Policy": "no-referrer"})

    def _page_denied(self):
        """A page asked for without a valid token. A small static note that says how to open MySearch; nothing in it
        comes from the request, and it carries no token."""
        body = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>MySearch</title>"
                "<style>body{font:16px -apple-system,system-ui,sans-serif;margin:0;min-height:100vh;display:grid;"
                "place-items:center;background:#E7EBEF;color:#445061}"
                "@media (prefers-color-scheme: dark){body{background:#10141A;color:#AAB4C0}}"
                "p{margin:0;padding:0 16px;text-align:center}code{font:14px ui-monospace,Menlo,monospace}</style></head>"
                "<body><p>Open MySearch from the app or run <code>mse ui</code>.</p></body></html>")
        self._send(401, body.encode(), "text/html; charset=utf-8",
                   {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                    "Referrer-Policy": "no-referrer"})

    def _static(self, rel: str):
        root = paths.UI.resolve()
        f = (root / rel).resolve()
        # is_relative_to, not a string prefix: a sibling folder such as ui_old/ starts with the same characters
        if not f.is_relative_to(root) or not f.is_file():
            return self._err(404, "not found")
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype)

    # ---------------------------------------------------------- read API

    def get_api_status(self, qs):
        self._json(status())

    def get_api_index_progress(self, qs):
        """Per-folder indexing progress (stage, done/total, rate, ETA, errors, pending work). Read-only and cheap."""
        self._json(liveprog.snapshot())

    def get_api_search(self, qs):
        # c: collections, groups, aliases and "all", mixed; a shortcut in q ("w: invoice", "in:work") overrides it;
        # unknown names -> 400. shortcuts=0: take q as it is (clients that split it themselves).
        q, colls, shortcut = query_scope((qs.get("q") or [""])[0], qs.get("c", []),
                                         _flag((qs.get("shortcuts") or [None])[0]))
        filters = {}
        for key in ("type", "after", "before", "path", "ext", "include", "exclude", "in"):
            if qs.get(key) and qs[key][0]:                # in=~/Folder|/Folder: a folder path (see search.folder_values)
                filters[key] = qs[key]
        opts = {"rerank": True} if (qs.get("precise") or ["0"])[0] in ("1", "true") else None
        res = search.search(q, collections=colls, limit=int((qs.get("limit") or [0])[0]) or None,
                            mode=(qs.get("mode") or ["hybrid"])[0], filters=filters, opts=opts)
        if shortcut:
            res["scope"]["shortcut"] = shortcut
        self._json(res)

    def get_api_similar(self, qs):
        """c = the file's collection; in = where to look for similar files (collections, groups, "all"; default:
        the file's collection). A group or list given as c is taken as the scope and the file is looked up in it."""
        colls = scope_param(qs.get("in", []))
        own = config.scope_tokens(qs.get("c", []))
        coll = None
        if len(own) == 1 and config.get_collection(config.load(), own[0]):
            coll = own[0]
        elif own:
            colls = colls or scope_param(own)
        res = search.similar(coll, (qs.get("path") or qs.get("ref") or [""])[0], colls,
                             int((qs.get("limit") or [20])[0]))
        if res is None:
            return self._err(404, "file not found")
        self._json(res)

    def post_api_test_rule(self, qs):
        """How many indexed files a set of patterns would match (rule tester in Settings)."""
        from .ignore import Matcher
        b = self._jbody()
        pats = [p.strip() for p in b.get("patterns", []) if p.strip() and not p.strip().startswith("#")]
        m = Matcher(pats)
        conn = store.connect(b["collection"], readonly=True)
        try:
            rows = [r[0] for r in conn.execute("SELECT path FROM files ORDER BY path")]
        finally:
            conn.close()
        hits = [p for p in rows if m.match(p)] if pats else []
        self._json({"matches": len(hits), "of": len(rows), "sample": hits[:8]})

    def get_api_health(self, qs):
        name = qs["c"][0]
        conn = store.connect(name, readonly=True)
        try:
            errors = [dict(r) for r in conn.execute(
                "SELECT path, error FROM files WHERE state='error' ORDER BY path LIMIT 200")]
            failed = [r[0] for r in conn.execute(
                "SELECT DISTINCT f.path FROM items i JOIN files f ON f.id = i.file_id WHERE i.embedded = -1"
                " ORDER BY f.path LIMIT 200")]
            name_only = [r[0] for r in conn.execute("SELECT path FROM files WHERE mode='meta' ORDER BY path LIMIT 200")]
            counts = conn.execute("SELECT SUM(state='error'), SUM(mode='meta') FROM files").fetchone()
        finally:
            conn.close()
        self._json({"errors": errors, "failed_media": failed, "name_only": name_only,
                    "error_count": counts[0] or 0, "name_only_count": counts[1] or 0})

    def post_api_retry(self, qs):
        """Re-read files that failed (extraction errors, media that could not be embedded)."""
        name = self._jbody()["collection"]
        with store.write_lock(name):
            conn = store.connect(name)
            n = conn.execute("UPDATE files SET mtime = -1 WHERE state = 'error' OR id IN"
                             " (SELECT file_id FROM items WHERE embedded = -1)").rowcount
            conn.commit()
            conn.close()
        self._json({"retrying": n, **JOBS.submit("sync", name, reason="retry")})

    def get_api_collections(self, qs):
        colls = collections_summary()
        self._json({"collections": colls, "groups": groups_summary(colls)})

    def get_api_groups(self, qs):
        self._json({"groups": groups_summary()})

    def post_api_groups(self, qs):
        """{name?, label, collections, aliases?, hidden?} -> the group {name, label, collections, files, aliases,
        hidden}; creates or updates. On an update, a key left out keeps its value ({name, hidden: true} only hides
        the tab). Invalid names or aliases -> 400 with the reason."""
        b = self._jbody()
        g = config.save_group(b.get("label"), b.get("collections"), b.get("name"), aliases=b.get("aliases"),
                              hidden=b.get("hidden"))
        self._json(next(x for x in groups_summary() if x["name"] == g["name"]))

    def _group_remove(self, name: str):
        try:
            config.remove_group(name)
        except KeyError:
            return self._err(404, f"no group named '{name}'")
        self._json({"ok": True})

    def _agents_action(self, name: str, method: str):
        """GET -> {collection, folder, mse_bin, agents: [status rows]}; POST {install: true|false} -> {files}."""
        from . import agents_setup
        c = config.get_collection(config.load(), name)
        if not c:
            return self._err(404, f"no collection named '{name}'")
        if method == "GET":
            return self._json({"collection": name, "folder": c["path"], "mse_bin": agents_setup.mse_bin(),
                               "agents": agents_setup.status(name)})
        install = self._jbody().get("install")
        if not isinstance(install, bool):
            raise ValueError('send {"install": true} to connect the agents or {"install": false} to disconnect them')
        self._json({"files": agents_setup.install(name) if install else agents_setup.remove(name)})

    def get_api_agents_global(self, qs):
        from . import agents_setup
        self._json({"mse_bin": agents_setup.mse_bin(), "agents": agents_setup.status_global()})

    def post_api_agents_global(self, qs):
        from . import agents_setup
        install = self._jbody().get("install")
        if not isinstance(install, bool):
            raise ValueError('send {"install": true} or {"install": false}')
        self._json({"files": agents_setup.install_global() if install else agents_setup.remove_global()})

    def get_api_doc(self, qs):
        ref = (qs.get("ref") or qs.get("path") or [""])[0]
        doc = search.get_document((qs.get("c") or [None])[0], ref,
                                  max_chars=int((qs.get("max") or [20000])[0]),
                                  page=int(qs["page"][0]) if qs.get("page") else None)
        if not doc:
            return self._err(404, "document not found")
        self._json(doc)

    def get_api_ls(self, qs):
        self._json({"files": search.list_files(qs["c"][0], (qs.get("prefix") or [""])[0],
                                                int((qs.get("limit") or [200])[0]))})

    def get_api_settings(self, qs):
        cfg = config.load()
        from .ignore import SECRET_PATTERNS
        from . import ask
        self._json({**{k: cfg[k] for k in ("schedule", "worker", "search", "translation", "model", "port", "agents",
                                             "mac_index")},
                    "secret_patterns": list(SECRET_PATTERNS), "ask": ask.settings(cfg)})

    def _resolve_file(self, qs) -> tuple[dict, dict]:
        found = search.find_file((qs.get("c") or [None])[0], (qs.get("path") or qs.get("ref") or [""])[0])
        if not found:
            raise KeyError("file not found in any collection")
        return found

    def get_api_thumb(self, qs):
        c, f = self._resolve_file(qs)
        t = paths.THUMBS / c["name"] / f"{f['id']}.jpg"
        if f["mode"] == "meta" or not t.exists():
            return self._err(404, "no thumbnail")
        self._send(200, t.read_bytes(), "image/jpeg", file_headers("image/jpeg", {"Cache-Control": "max-age=3600"}))

    def get_api_file(self, qs):
        c, f = self._resolve_file(qs)
        if f["mode"] == "meta":
            return self._err(403, "metadata-only file: open it in Finder instead")
        full = Path(c["path"]) / f["path"]
        if not full.is_file():
            return self._err(404, "file is gone")
        size = full.stat().st_size
        ctype = served_type(full)               # active types (html, xhtml, xml, svg, ...) come back as text/plain
        rng = self.headers.get("Range")
        with open(full, "rb") as fh:
            if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
                start = int(m.group(1) or 0)
                end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1, start + 8 * 1024 * 1024)
                fh.seek(start)
                data = fh.read(end - start + 1)
                return self._send(206, data, ctype, file_headers(ctype, {"Content-Range": f"bytes {start}-{end}/{size}",
                                                                         "Accept-Ranges": "bytes",
                                                                         "Content-Disposition": "inline"}))
            if size > 200 * 1024 * 1024:
                return self._err(413, "file too large to preview")
            self._send(200, fh.read(), ctype, file_headers(ctype, {"Accept-Ranges": "bytes",
                                                                   "Content-Disposition": "inline"}))

    # ---------------------------------------------------------- whole-Mac file names (mse/macindex.py, mse/filequery.py)

    def get_api_files_search(self, qs):
        """?q=&limit=200&scope=mac|indexed&c=<collections/groups> -> {items: [{path, name, parent, is_dir, kind, ext,
        size, mtime, score, match: [[start, end], …], collection?}], total, ms, scope, query: {words, filters},
        truncated}. scope=mac (default) searches every file name under the configured roots; scope=indexed only the
        folders of the given (or all enabled) collections and groups. A scope shortcut in q ("w: invoice", "in:work")
        narrows to those folders; in:~/Folder or in:/Folder is a folder path. Errors in the query language -> 400."""
        self._json(files_search((qs.get("q") or [""])[0], (qs.get("scope") or ["mac"])[0] or "mac",
                                qs.get("c", []), (qs.get("limit") or [None])[0]))

    def get_api_files_status(self, qs):
        self._json(macindex.manager().status())

    def post_api_files_crawl(self, qs):
        """{force?: true} -> {started, reason?}. A full re-crawl in the background (nice'd, resumable)."""
        b = self._jbody()
        self._json(macindex.manager().start_crawl(force=bool(b.get("force"))))

    def get_api_tree(self, qs):
        """?c=<collection>&path=<relative folder> -> {collection, path, folders: [{name, path, children}]}."""
        try:
            self._json(tree.folders((qs.get("c") or [""])[0], (qs.get("path") or [""])[0]))
        except tree.NotFound as e:
            self._err(404, str(e.args[0] if e.args else e))

    # ---------------------------------------------------------- write API

    def post_api_open(self, qs):
        b = self._jbody()
        c, f = search.find_file(b.get("collection"), b.get("path", "")) or (None, None)
        if not f:
            return self._err(404, "file not found")
        full = str(Path(c["path"]) / f["path"])
        subprocess.Popen(["/usr/bin/open", "-R", full] if b.get("reveal") else ["/usr/bin/open", full])
        self._json({"ok": True})

    def post_api_pick_folder(self, qs):
        r = subprocess.run(["/usr/bin/osascript", "-e",
                            'POSIX path of (choose folder with prompt "Choose a folder for MySearch to index")'],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return self._json({"path": None})
        self._json({"path": r.stdout.strip().rstrip("/")})

    def post_api_collections(self, qs):
        b = self._jbody()
        coll = config.add_collection(b["path"], b.get("name"), **{k: b[k] for k in ("description", "in_global")
                                                                    if k in b})
        JOBS.submit("scan", coll["name"], reason="added")
        out = {"collection": coll}
        if b.get("connect_agents"):                       # write the mysearch entry into the folder's agent configs
            from . import agents_setup
            try:
                out["agents"] = {"files": agents_setup.install(coll["name"])}
            except Exception as e:                        # the folder is added either way
                out["agents_error"] = f"{type(e).__name__}: {e}"
        self._json(out)

    def _collection_action(self, name: str, action: str):
        if action == "remove":
            b = self._jbody()
            agents = None
            if b.get("disconnect_agents"):                # before the collection goes: its folder is needed
                from . import agents_setup
                agents = {"files": agents_setup.remove(name)}
            coll = config.remove_collection(name)
            trash = indexer.drop_collection_data(name)
            return self._json({"removed": coll["name"], "index_moved_to": trash,
                               "note": "only the search index was moved aside; your files are untouched",
                               **({"agents": agents} if agents else {})})
        coll = config.update_collection(name, self._jbody())
        self._json({"collection": coll})

    def post_api_sync(self, qs):
        b = self._jbody()
        kind = b.get("kind", "sync")
        coll, path = b.get("collection"), b.get("path")
        if path and path.startswith("/"):
            c = config.collection_for_path(config.load(), path)
            if not c:
                return self._err(400, "path is not inside any collection")
            coll, path = c["name"], str(Path(path).resolve().relative_to(c["path"]))
        if path:
            kind = "file"
        self._json(submit_scoped(kind, coll, path))    # collection, group, comma list or "all"

    def post_api_cancel(self, qs):
        JOBS.cancel()
        self._json({"ok": True})

    def post_api_settings(self, qs):
        cfg = config.update_settings(self._jbody())    # {"ask": {"enabled": false}} also unloads the Ask model
        from . import ask
        self._json({**{k: cfg[k] for k in ("schedule", "worker", "search", "translation", "agents", "mac_index")},
                    "ask": ask.settings(cfg)})

    # ---------------------------------------------------------- feedback (favourites) and quality

    def get_api_feedback(self, qs):
        from . import feedback
        self._json({"items": feedback.list_items((qs.get("q") or [""])[0] or None)})

    def post_api_feedback(self, qs):
        """{query, collection, path, kind: best|hide} -> {ok, id}; marking the same file again updates the row."""
        from . import feedback
        b = self._jbody()
        if not (b.get("query") or "").strip() or not (b.get("path") or "").strip():
            raise ValueError("query and path are required")
        try:
            fid = feedback.add(b["query"], b.get("collection"), b["path"], b.get("kind") or "best")
        except KeyError as e:
            return self._err(404, str(e.args[0] if e.args else e))
        self._json({"ok": True, "id": fid})

    def post_api_feedback_remove(self, qs):
        """{id} or {query, collection, path} -> {ok, removed}."""
        from . import feedback
        b = self._jbody()
        if b.get("id") is not None:
            n = feedback.remove(id=int(b["id"]))
        else:
            n = feedback.remove(query=b.get("query"), collection=b.get("collection"), ref=b.get("path"))
        self._json({"ok": True, "removed": n})

    def get_api_quality(self, qs):
        """Search-quality history (the nightly learn runs, manual tunes, rollbacks) and the settings in effect."""
        from . import evaluate, learn
        try:
            from . import finetune
            ft = finetune.status()
        except Exception as e:
            ft = {"error": f"{type(e).__name__}: {e}"}
        self._json({"entries": learn.history(60), "current": evaluate.current_settings(), "finetune": ft})

    def get_api_finetune(self, qs):
        """Re-ranker learning from favourites (finetune.py): favourites against the gate, the last run, the active
        adapter and what its evaluation measured, plus recent history. Read-only: training is the night job's
        (or `mse finetune run`)."""
        from . import finetune
        self._json({**finetune.status(), "history": finetune.history()[-30:]})

    def post_api_search_image(self, qs):
        data = self._body()
        if not data:
            return self._err(400, "send the image bytes as the request body")
        f = paths.TMP / f"query-{os.getpid()}-{threading.get_ident()}.img"
        f.write_bytes(data)
        try:
            jpg = f.with_suffix(".jpg")
            from .extract import helper
            helper("image", str(f), "--out", str(jpg), "--max-px", "1024")
            vecs, ok = pool().embed_media([{"type": "image", "path": str(jpg), "title": "none"}])
            if not ok[0]:
                return self._err(400, "could not read that image")
            import numpy as np
            qv = vecs[0] / (np.linalg.norm(vecs[0]) + 1e-9)
            colls = scope_param(qs.get("c", []))
            self._json(search.search("", collections=colls, mode="semantic", qvec=qv,
                                     limit=int((qs.get("limit") or [0])[0]) or None))
        finally:
            f.unlink(missing_ok=True)
            f.with_suffix(".jpg").unlink(missing_ok=True)

    def post_api_ask(self, qs):
        """{q, collections?, filters?, agent_safe?} -> application/x-ndjson, one Ask event per line (mse/ask.py):
        sources, token..., then done (or error). 409 {"error": "ask_disabled"} while Ask is switched off.
        `collections` takes what `c` takes on /api/search (collections, groups, aliases, "all"); a scope shortcut in
        q ("w: when was …") overrides it, unless {"shortcuts": false}; `filters` takes its filter keys. The
        sources event carries the scope it read: {"collections", "label", "shortcut"?}.
        `history` (follow-ups): [{"q", "a"}], the most recent turns, oldest first, at most 3, strings cut to 2,000
        characters; malformed -> 400. It shapes the search query and the prompt, never the scope.
        `route` ("auto" | "cloud" | "local"): this question only, instead of the configured route; anything else -> 400.
        done carries provider, model, where ("cloud" | "local"), fallback_reason? and redacted (mse/ask.py)."""
        from . import ask
        b = self._jbody()
        q, colls, shortcut = query_scope(b.get("q") or "", b.get("collections") or [], _flag(b.get("shortcuts")))
        history = ask.validate_history(b.get("history"))          # ValueError -> 400, before the 409 check
        route = b.get("route")
        if route is not None and route not in config.ASK_ROUTES:
            raise ValueError(f"route must be one of: {', '.join(config.ASK_ROUTES)}")
        filters = {}
        for key in ("type", "after", "before", "ext", "include", "exclude"):
            v = (b.get("filters") or {}).get(key)
            if v:
                filters[key] = [str(x) for x in v] if isinstance(v, list) else [str(v)]
        # A subfolder scope: relative to the collection, or a folder path. Two spellings mean the same thing and both
        # are read (the web UI sends `filters.path`, API clients may send a top-level `path`); either way it is a
        # whole-folder scope ("Acme" is not "Acme_old"), not a name prefix.
        sub = []
        for src in (b.get("path"), (b.get("filters") or {}).get("path")):
            if src:
                sub += [str(i) for i in src] if isinstance(src, list) else [str(src)]
        if sub:
            rel = []
            for x in sub:
                x = x.strip()
                if not x:
                    continue
                if ".." in x.replace("\\", "/").split("/"):
                    raise ValueError("path must stay inside the folder (no '..')")
                if x.startswith(("~", "/")):
                    filters.setdefault("in", []).append(x)
                else:                           # whole folders only: "Acme" is not "Acme_old"
                    rel.append(x.replace("\\", "/").strip("/") + "/")
            if rel:
                filters["_under"] = [rel]
        try:
            with ask.step_events():          # the transcript: Searching …, Found n results, Reading …, Asking <model>
                events = ask.ask(q, collections=colls, filters=filters or None, agent_safe=bool(b.get("agent_safe")),
                                 **({"history": history} if history else {}), **({"route": route} if route else {}))
        except ask.AskDisabled:
            return self._json({"error": "ask_disabled"}, 409)
        sc = config.resolve_scope(config.load(), colls, strict=False)
        scope = {"collections": sc["collections"], "label": sc["label"], **({"shortcut": shortcut} if shortcut else {})}
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")            # no Content-Length: the body ends when the socket closes
        self.end_headers()
        self.close_connection = True
        try:
            for ev in events:
                if ev.get("type") == "sources":
                    ev = {**ev, "scope": scope}
                self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass                                            # client gone: closing the generator cancels generation
        except Exception as e:                              # headers are out: report in-band, never a second response
            log(traceback.format_exc())
            try:
                self.wfile.write((json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}) + "\n").encode())
            except Exception:
                pass
        finally:
            close = getattr(events, "close", None)
            if close:
                close()                                     # stops a generation still in progress

    # ---------------------------------------------------------- Ask providers (mse/providers.py). Never a key out.

    def get_api_ask_providers(self, qs):
        """{route, provider, model, online, providers: [{id, label, kind, base_url, builtin, has_key, needs_key,
        models, status: ok | no_key | offline | error, error?}]}."""
        from . import providers
        self._json(providers.list_view())

    def get_api_ask_local_models(self, qs):
        """{current, models: [{name, size_mb, current}]}: the answer models installed in the models folder."""
        from . import ask
        self._json(ask.local_models())

    def post_api_ask_providers(self, qs):
        """{id, label?, kind?, base_url?, key?, models?, forget_key?, key_file?, training_models?, auto_models?} -> the
        provider's row. Creates a custom provider or updates one (a built-in keeps its kind); a key goes to the Keychain
        and is never returned."""
        from . import providers
        try:
            self._json(providers.save(self._jbody()))
        except providers.KeychainError as e:
            self._err(503, f"keychain: {e}")

    def _provider_action(self, pid: str, action: str):
        """POST …/<id>/remove {forget_key?} | …/<id>/models | …/<id>/connect (a provider with a key_file: this process
        reads the key file, stores the key in the Keychain, lists the models; the key is never returned) |
        …/<id>/test {model?} (test: one tiny request, 200 with {ok, model, ms, error?} whatever the outcome)."""
        from . import providers
        from .cloud import CloudError
        b = self._jbody()
        try:
            if action == "remove":
                return self._json(providers.remove(pid, forget=b.get("forget_key") is True))
            if action == "models":
                return self._json(providers.refresh_models(pid))
            if action == "connect":
                return self._json({"ok": True, **providers.connect(pid)})
            return self._json(providers.test(pid, b.get("model") or None))
        except providers.UnknownProvider:
            return self._err(404, f"no provider named '{pid}'")
        except providers.KeychainError as e:
            return self._err(503, f"keychain: {e}")
        except CloudError as e:
            return self._err(502, str(e))

    def post_mcp(self, qs):
        from .mcp import LocalClient, handle
        req = self._jbody()
        cwd = self.headers.get("X-MSE-Cwd")
        reply = handle(req, LocalClient(), cwd)
        if reply is None:
            return self._send(202, b"", "application/json")
        self._json(reply)


BIND_RETRY_S = 30.0           # a previous instance may still hold the port while it shuts down (restart, kickstart -k)
BIND_RETRY_EVERY_S = 0.5
SUPERVISOR_POLL_S = 1.0       # a supervised service checks every this often that its supervisor is still its parent
SHUTDOWN_WAIT_S = 5.0         # a stop waits this long for the running job, and this long for the model workers


def _bind(port: int, window_s: float = BIND_RETRY_S) -> ThreadingHTTPServer:
    """Bind 127.0.0.1:<port>. Only "address in use" is retried, every BIND_RETRY_EVERY_S, for up to window_s; any other
    error is raised at once. The retry is logged once."""
    deadline = time.monotonic() + window_s
    waited = False
    while True:
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as e:
            if e.errno != errno.EADDRINUSE or time.monotonic() >= deadline:
                raise
            if not waited:
                log(f"port {port} is still held by the previous instance; retrying for up to {int(window_s)} s")
                waited = True
            time.sleep(BIND_RETRY_EVERY_S)
            continue
        if waited:
            log(f"port {port} is free; bound")
        return httpd


def _watch_supervisor(on_gone) -> None:
    """Started only when the app's supervisor launched this process (MSE_SUPERVISED=1). If the supervisor dies without
    stopping us, this process is re-parented (to launchd or a subreaper) and on_gone(new_parent_pid) is called."""
    parent = os.getppid()

    def watch():
        while True:
            time.sleep(SUPERVISOR_POLL_S)
            if os.getppid() != parent:
                on_gone(os.getppid())
                return

    threading.Thread(target=watch, daemon=True, name="supervisor-watch").start()


def _stop_services() -> None:
    """Run after the port is released. Each step has a bound, and none waits for a file read to finish: a read still
    running after SHUTDOWN_WAIT_S is left behind, and the next start reads that file again."""
    for label, step in (("crawl", lambda: macindex.manager().stop()),
                        ("jobs", lambda: JOBS.wait_idle(SHUTDOWN_WAIT_S)),
                        ("model workers", lambda: pool().stop_all(SHUTDOWN_WAIT_S))):
        try:
            step()
        except Exception:
            log(f"stopping {label} failed:\n{traceback.format_exc()}")


def serve(port: int | None = None) -> None:
    global JOBS
    paths.ensure_dirs()
    cfg = config.load()
    port = port or cfg["port"]
    # popped, so the model workers and helpers this process starts do not inherit the variable
    supervised = os.environ.pop("MSE_SUPERVISED", None) == "1"
    if supervised and os.getppid() == 1:
        log("started by a supervisor that has already exited; not starting")
        raise SystemExit(75)
    httpd = _bind(port)              # first: nothing starts while another instance still holds the port
    httpd.daemon_threads = True      # request threads are daemons, so server_close() does not wait for them
    JOBS = JobManager()
    macindex.manager().start()        # whole-Mac file names: the first crawl starts a little later, in the background
    stopping = threading.Event()

    def stop(reason: str) -> None:
        """The one way a stop starts. It runs on its own thread, because shutdown() must not be called from the thread
        inside serve_forever(), which is the main thread (the signal handler runs there). The order matters: the queue
        and the in-flight markers are saved before the socket is closed, so the next instance reads a settled state."""
        if stopping.is_set():
            return
        stopping.set()

        def run():
            log(f"stopping ({reason})")
            try:
                JOBS.freeze()
            finally:
                httpd.shutdown()          # always: a failed freeze must not leave the service ignoring its stop

        threading.Thread(target=run, daemon=True, name="stop").start()

    signal.signal(signal.SIGTERM, lambda *_: stop("SIGTERM"))
    signal.signal(signal.SIGINT, lambda *_: stop("SIGINT"))
    if supervised:
        _watch_supervisor(lambda ppid: stop(f"supervisor gone; parent is now pid {ppid}"))
    log(f"MySearch {__version__} listening on http://127.0.0.1:{port} (pid {os.getpid()})")
    print(f"MySearch listening on http://127.0.0.1:{port}", file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()         # the port is free from here on
    _stop_services()
    log("stopped")
    sys.stdout.flush()
    sys.stderr.flush()
    # Exit now rather than at interpreter shutdown: that joins every ThreadPoolExecutor thread, so one read still running
    # would keep this process alive for up to FILE_TIMEOUT_S. Nothing else needs flushing: the state is saved above.
    os._exit(0)
