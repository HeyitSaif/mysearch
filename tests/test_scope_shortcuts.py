"""Choosing where to search: the scope shortcut in a query ("w: invoice", "invoice in:work"), aliases for folders
and groups, hidden groups, and the MCP lock seeing a shortcut like any other scope.

Everything runs in temporary folders and a temporary MySearch home.
"""

import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

os.environ["MSE_HOME"] = tempfile.mkdtemp(prefix="mse-test-")

from mse import cli, config, feedback, mcp, search  # noqa: E402


def _mkdir(prefix):
    return Path(tempfile.mkdtemp(prefix=prefix)).resolve()


class _Setup(unittest.TestCase):
    """Folders sc-alpha (alias sa), sc-beta, sc-gamma (not in All); groups sc-team (alpha + beta, alias st) and
    sc-quiet (gamma, hidden)."""

    @classmethod
    def setUpClass(cls):
        cls.dirs = {n: _mkdir(f"mse-{n}-") for n in ("sc-alpha", "sc-beta", "sc-gamma")}
        for n, d in cls.dirs.items():
            if not config.get_collection(config.load(), n):
                config.add_collection(str(d), n, **({"in_global": False} if n == "sc-gamma" else {}))
        config.update_collection("sc-alpha", {"aliases": ["sa"]})
        config.save_group("SC Team", ["sc-alpha", "sc-beta"], "sc-team", aliases=["st"])
        config.save_group("SC Quiet", ["sc-gamma"], "sc-quiet", hidden=True)

    @classmethod
    def tearDownClass(cls):
        for g in list(config.load()["groups"]):
            if g["name"].startswith("sc-"):
                config.remove_group(g["name"])
        for n in cls.dirs:
            config.remove_collection(n)


# ====================================================================== parsing

class Parsing(_Setup):
    def split(self, q):
        return config.split_scope(q, config.load())

    def test_prefix_forms(self):
        cases = {
            "sc-alpha: invoice": (["sc-alpha"], "invoice"),
            "SA: Invoice March": (["sc-alpha"], "Invoice March"),            # aliases and names: any case
            "st: x": (["sc-team"], "x"),                                      # a group's alias
            "Sc-Team: x": (["sc-team"], "x"),
            'sa:"exact phrase"': (["sc-alpha"], '"exact phrase"'),            # the quotes stay: a phrase
            "sa: \"exact phrase\" more": (["sc-alpha"], '"exact phrase" more'),
            "  sa:   spaced  ": (["sc-alpha"], "spaced"),
            "sa:": (["sc-alpha"], ""),
            "all: x": (["all"], "x"),
            "sc-quiet: x": (["sc-quiet"], "x"),                               # hidden groups still work by name
        }
        for q, want in cases.items():
            self.assertEqual(self.split(q), want, q)

    def test_comma_lists(self):
        self.assertEqual(self.split("sa,sc-beta: invoice"), (["sc-alpha", "sc-beta"], "invoice"))
        self.assertEqual(self.split("sa, sc-beta: invoice"), (["sc-alpha", "sc-beta"], "invoice"))
        self.assertEqual(self.split("sa,sc-alpha,SA: x"), (["sc-alpha"], "x"))           # the same folder once
        self.assertEqual(self.split("x in:sa,sc-beta"), (["sc-alpha", "sc-beta"], "x"))

    def test_in_anywhere(self):
        cases = {
            "invoice in:sc-team": (["sc-team"], "invoice"),
            "in:sa invoice": (["sc-alpha"], "invoice"),
            "invoice IN:sa march": (["sc-alpha"], "invoice march"),
            'invoice in:"st"': (["sc-team"], "invoice"),
            "sa: invoice in:sc-beta": (["sc-alpha", "sc-beta"], "invoice"),    # prefix and in: add up
            "a in:sa b in:sc-beta": (["sc-alpha", "sc-beta"], "a b"),
            "type:pdf in:sa after:2025-03": (["sc-alpha"], "type:pdf after:2025-03"),   # filters stay
        }
        for q, want in cases.items():
            self.assertEqual(self.split(q), want, q)

    def test_unknown_names_stay_text(self):
        for q in ("Note: the meeting", "nope,sa: x", "sa:invoice", "10:30 meeting", "Re: Rechnung", "in: sa",
                  "type:pdf invoice", "path: x", "\"sa: quoted\" words", 'meet "with in:sa" there', "in:\"\" x",
                  "", "  two  spaces  "):
            self.assertEqual(self.split(q), (None, q), q)                      # untouched, byte for byte

    def test_unknown_in_names(self):
        with self.assertRaises(ValueError) as e:
            self.split("invoice in:nope")
        self.assertIn("unknown collection or group after in: nope", str(e.exception))
        self.assertIn("known: all, ", str(e.exception))
        with self.assertRaises(ValueError) as e:
            self.split("invoice in:sa,nope,nada")
        self.assertIn("nope, nada", str(e.exception))
        s = config.scope_shortcut("sa: invoice in:nope", config.load())          # lenient: reported, kept as text
        self.assertEqual((s["tokens"], s["rest"], s["unknown"]), (["sc-alpha"], "invoice in:nope", ["nope"]))
        self.assertEqual(config.strip_scope("sa: invoice"), "invoice")

    def test_reserved_names_never_count(self):
        legacy = {"collections": [{"name": "path", "path": "/x"}, {"name": "ok", "path": "/y", "aliases": ["in"]}],
                  "groups": [{"name": "type", "label": "T", "collections": ["ok"]}]}
        self.assertEqual(set(config.shortcut_names(legacy)), {"all", "ok"})
        for q in ("path: x", "type: x", "in: x", "x in:path", "x in:in"):
            s = config.scope_shortcut(q, legacy)
            self.assertIsNone(s["tokens"], q)

    def test_response_scope_and_search_untouched(self):
        # search.search() itself never reads a shortcut: the callers do (so the MCP lock sees it)
        res = search.search("sa: invoice", collections=["sc-beta"], mode="keyword")
        self.assertEqual(res["scope"], {"collections": ["sc-beta"], "label": "sc-beta"})
        self.assertEqual(res["text"], "sa: invoice")
        res = search.search("invoice", collections=["sa"], mode="keyword")      # an alias as a scope name
        self.assertEqual(res["scope"], {"collections": ["sc-alpha"], "label": "sc-alpha"})


# ====================================================================== reserved words and aliases

class ReservedAndAliases(_Setup):
    def tearDown(self):
        for n in ("sc-alpha", "sc-beta"):
            config.update_collection(n, {"aliases": ["sa"] if n == "sc-alpha" else []})
        config.save_group(None, None, "sc-team", aliases=["st"])

    def test_filter_keys_are_reserved(self):
        self.assertIs(search.FILTER_KEYS, config.FILTER_KEYS)
        self.assertTrue({"type", "after", "before", "path", "ext", "include", "exclude", "in", "kind", "has"}
                        <= config.RESERVED_SCOPE_NAMES)
        self.assertIn("all", config.RESERVED_SCOPE_NAMES)
        for word in sorted(config.RESERVED_SCOPE_NAMES):
            with self.assertRaises(ValueError, msg=word) as e:
                config.add_alias(word, "sc-alpha")
            self.assertIn("reserved", str(e.exception))
            with self.assertRaises(ValueError, msg=word) as e:
                config.save_group("x", ["sc-alpha"], word)
            self.assertIn("reserved", str(e.exception))
            with self.assertRaises(ValueError, msg=word) as e:
                config.add_collection(str(_mkdir("mse-sc-res-")), word)
            self.assertIn("reserved", str(e.exception))
        with self.assertRaises(ValueError):
            config.update_collection("sc-beta", {"aliases": ["Exclude"]})
        with self.assertRaises(ValueError):
            config.save_group(None, None, "sc-team", aliases=["st", "before"])
        self.assertEqual(config.aliases_of(config.get_group(config.load(), "sc-team")), ["st"])   # nothing saved

    def test_alias_validation(self):
        r = config.add_alias(" SB ", "SC-BETA")
        self.assertEqual(r, {"alias": "sb", "kind": "folder", "name": "sc-beta", "aliases": ["sb"]})
        self.assertEqual(config.add_alias("sb", "sc-beta")["aliases"], ["sb"])       # again: no change
        bad = {("sa", "sc-beta"): "already a shortcut for the folder 'sc-alpha'",
               ("st", "sc-beta"): "already a shortcut for the group 'sc-team'",
               ("sc-alpha", "sc-beta"): "already the name of a folder",
               ("sc-team", "sc-beta"): "already the name of a group",
               ("s p", "sc-beta"): "must be lowercase letters, digits and single dashes",
               ("sc-beta", "sc-beta"): "already its name",
               ("x", "sa"): "is itself a shortcut",
               ("x", "nowhere"): "no folder or group named 'nowhere'"}
        for (alias, target), msg in bad.items():
            with self.assertRaises(ValueError, msg=alias) as e:
                config.add_alias(alias, target)
            self.assertIn(msg, str(e.exception))
        # names and aliases share one namespace: a new folder or group cannot take an alias
        with self.assertRaises(ValueError) as e:
            config.add_collection(str(_mkdir("mse-sc-clash-")), "sa")
        self.assertIn("already a shortcut", str(e.exception))
        with self.assertRaises(ValueError) as e:
            config.save_group("SB", ["sc-alpha"])
        self.assertIn("already a shortcut", str(e.exception))
        self.assertEqual(config.remove_alias("SB"), {"alias": "sb", "kind": "folder", "name": "sc-beta", "aliases": []})
        self.assertNotIn("aliases", config.get_collection(config.load(), "sc-beta"))   # none: no empty key stored
        with self.assertRaises(KeyError):
            config.remove_alias("sb")

    def test_aliases_work_as_scope_names(self):
        cfg = config.load()
        r = config.resolve_scope(cfg, "sa,st")
        self.assertEqual((r["collections"], r["label"]), (["sc-alpha", "sc-beta"], "sc-alpha + SC Team"))
        self.assertEqual(config.canonical_name(cfg, "st"), "sc-team")
        self.assertIsNone(config.canonical_name(cfg, "nope"))
        self.assertEqual(cli._scope("sa,sc-alpha,st"), ["sc-alpha", "sc-team"])

    def test_group_editor_saves_keep_aliases_and_hidden(self):
        config.save_group("SC Team", ["sc-alpha", "sc-beta"], "sc-team")              # the old editor's request
        g = config.get_group(config.load(), "sc-team")
        self.assertEqual(g["aliases"], ["st"])
        config.save_group(None, None, "sc-team", aliases=[])                          # [] takes them away
        self.assertNotIn("aliases", config.get_group(config.load(), "sc-team"))


class HiddenGroups(_Setup):
    def test_hidden_flag(self):
        g = config.get_group(config.load(), "sc-quiet")
        self.assertIs(g.get("hidden"), True)
        config.save_group(None, None, "sc-quiet", hidden=False)                      # only the flag changes
        g = config.get_group(config.load(), "sc-quiet")
        self.assertEqual(g, {"name": "sc-quiet", "label": "SC Quiet", "collections": ["sc-gamma"]})
        config.save_group(None, None, "sc-quiet", hidden=True)
        with self.assertRaises(ValueError):
            config.save_group(None, None, "sc-quiet", hidden="yes")
        from mse.server import groups_summary
        rows = {x["name"]: x for x in groups_summary()}
        self.assertTrue(rows["sc-quiet"]["hidden"])
        self.assertFalse(rows["sc-team"]["hidden"])
        self.assertEqual(rows["sc-team"]["aliases"], ["st"])
        # a hidden group is searched like any other, by name, in a list or through the shortcut
        self.assertEqual(config.resolve_scope(config.load(), "sc-quiet")["collections"], ["sc-gamma"])
        res = search.search("x", collections=["sc-quiet"], mode="keyword")
        self.assertEqual(res["scope"], {"collections": ["sc-gamma"], "label": "SC Quiet"})


# ====================================================================== HTTP API

class _Jobs:
    def submit(self, kind, collection=None, path=None, reason="manual"):
        return {"jobs": []}

    def status(self):
        return {"current": None, "queued": []}


class Api(_Setup):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from http.server import ThreadingHTTPServer
        from mse import server
        cls.server_mod, cls.old_jobs = server, server.JOBS
        server.JOBS = _Jobs()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server_mod.JOBS = cls.old_jobs
        super().tearDownClass()

    def req(self, method, path, body=None, params=None, raw=False):
        url = f"http://127.0.0.1:{self.port}{path}" + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-MSE-Token": config.load()["token"], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                data = resp.read()
                return resp.status, (data.decode() if raw else json.loads(data))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_collections_lists_aliases_and_hidden(self):
        code, body = self.req("GET", "/api/collections")
        colls = {c["name"]: c for c in body["collections"]}
        self.assertEqual(colls["sc-alpha"]["aliases"], ["sa"])
        self.assertEqual(colls["sc-beta"]["aliases"], [])
        groups = {g["name"]: g for g in body["groups"]}
        self.assertEqual((groups["sc-team"]["aliases"], groups["sc-team"]["hidden"]), (["st"], False))
        self.assertTrue(groups["sc-quiet"]["hidden"])

    def test_search_shortcut_overrides_the_tab(self):
        code, res = self.req("GET", "/api/search", params={"q": "sa: invoice", "c": "sc-beta", "mode": "keyword"})
        self.assertEqual(code, 200)
        self.assertEqual(res["scope"], {"collections": ["sc-alpha"], "label": "sc-alpha",
                                        "shortcut": {"names": ["sa"], "tokens": ["sc-alpha"]}})
        self.assertEqual(res["query"], "invoice")
        code, res = self.req("GET", "/api/search", params={"q": "invoice in:st", "mode": "keyword"})
        self.assertEqual(res["scope"]["label"], "SC Team")
        self.assertEqual(sorted(res["collections"]), ["sc-alpha", "sc-beta"])
        # not a shortcut: the tab stays, the text is the query
        code, res = self.req("GET", "/api/search", params={"q": "Note: the meeting", "c": "sc-beta", "mode": "keyword"})
        self.assertEqual((res["scope"], res["query"]), ({"collections": ["sc-beta"], "label": "sc-beta"}, "Note: the meeting"))
        # clients that split the query themselves switch it off
        code, res = self.req("GET", "/api/search", params={"q": "sa: invoice", "c": "sc-beta", "shortcuts": "0",
                                                          "mode": "keyword"})
        self.assertEqual((res["scope"]["collections"], res["query"]), (["sc-beta"], "sa: invoice"))
        code, err = self.req("GET", "/api/search", params={"q": "invoice in:ghost"})
        self.assertEqual(code, 400)
        self.assertIn("unknown collection or group after in: ghost", err["error"])

    def test_alias_and_group_validation_is_400(self):
        code, err = self.req("POST", "/api/groups", {"label": "SC X", "collections": ["sc-beta"], "aliases": ["type"]})
        self.assertEqual(code, 400)
        self.assertIn("'type' is reserved", err["error"])
        code, err = self.req("POST", "/api/groups", {"name": "sc-team", "aliases": ["sa"]})
        self.assertEqual(code, 400)
        self.assertIn("already a shortcut for the folder 'sc-alpha'", err["error"])
        code, err = self.req("POST", "/api/groups", {"name": "sc-team", "hidden": "yes"})
        self.assertEqual(code, 400)
        code, err = self.req("POST", "/api/collections/sc-beta", {"aliases": ["st"]})
        self.assertEqual(code, 400)
        self.assertIn("already a shortcut for the group 'sc-team'", err["error"])
        code, err = self.req("POST", "/api/collections/sc-beta", {"aliases": ["Has"]})
        self.assertEqual(code, 400)
        # valid: set, then clear; hide and show a group without resending it
        code, body = self.req("POST", "/api/collections/sc-beta", {"aliases": ["SB", "beta2"]})
        self.assertEqual((code, body["collection"]["aliases"]), (200, ["sb", "beta2"]))
        self.assertEqual(self.req("POST", "/api/collections/sc-beta", {"aliases": []})[0], 200)
        code, g = self.req("POST", "/api/groups", {"name": "sc-team", "hidden": True})
        self.assertEqual((code, g["hidden"], g["collections"], g["aliases"]), (200, True, ["sc-alpha", "sc-beta"], ["st"]))
        code, g = self.req("POST", "/api/groups", {"name": "sc-team", "hidden": False})
        self.assertFalse(g["hidden"])

    def test_ask_shortcut(self):
        from mse import ask
        seen = {}

        def fake_ask(q, collections=None, filters=None, agent_safe=False):
            seen.update(q=q, collections=collections)
            return iter([{"type": "sources", "sources": []}, {"type": "done", "citations": [], "answer": "x"}])
        with mock.patch.object(ask, "ask", side_effect=fake_ask):
            code, body = self.req("POST", "/api/ask", {"q": "st: wann wurde bezahlt?", "collections": ["sc-beta"]},
                                  raw=True)
            self.assertEqual(code, 200)
            self.assertEqual(seen, {"q": "wann wurde bezahlt?", "collections": ["sc-team"]})
            sources = json.loads(body.splitlines()[0])
            self.assertEqual(sources["scope"]["label"], "SC Team")
            self.assertEqual(sources["scope"]["shortcut"]["names"], ["st"])
            code, body = self.req("POST", "/api/ask", {"q": "st: x", "collections": ["sc-beta"], "shortcuts": False},
                                  raw=True)
            self.assertEqual(seen, {"q": "st: x", "collections": ["sc-beta"]})
            code, err = self.req("POST", "/api/ask", {"q": "x in:ghost"})
            self.assertEqual(code, 400)


# ====================================================================== MCP: the lock sees the shortcut

class FakeClient:
    def __init__(self, foreign=None):
        self.calls, self.foreign = [], foreign

    def search(self, q, colls, limit, filters, mode, precise=False):
        self.calls.append(("search", q, colls))
        results = [{"collection": self.foreign, "path": "x.md", "docid": "abcd1234", "kind": "text", "why": [],
                    "hits": []}] if self.foreign else []
        return {"query": q, "collections": colls or [], "results": results, "bridge": {}}

    def ask(self, q, colls, agent_safe=True):
        self.calls.append(("ask", q, colls))
        src = [{"n": 1, "docid": "#abcd1234", "collection": self.foreign or (colls or ["?"])[0], "path": "x.md"}]
        return iter([{"type": "sources", "sources": src}, {"type": "done", "citations": [1], "answer": "a [1]",
                                                           "model": "m"}])


def call(name, args, client, lock=None, cwd=None):
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    r = mcp.handle(req, client, lock.cwd if lock else cwd, lock)["result"]
    return r["isError"], r["content"][0]["text"]


class McpShortcut(_Setup):
    REFUSAL = "This session is limited to the 'sc-alpha' collection by this folder's MCP config."

    def lock(self):
        return mcp.Lock(str(self.dirs["sc-alpha"]))

    def test_lock_refuses_other_scopes_named_in_the_query(self):
        lock = self.lock()
        for tool, key in (("search", "query"), ("ask", "question")):
            for q in ("sc-beta: invoice", "SC-BETA: invoice", "invoice in:sc-beta", "all: invoice", "st: invoice",
                      "sa,sc-beta: invoice", "invoice in:sa,sc-beta", "sc-quiet: x", "invoice in:ghost",
                      "sa: invoice in:sc-beta"):
                cl = FakeClient()
                err, text = call(tool, {key: q}, cl, lock)
                self.assertTrue(err, (tool, q))
                self.assertEqual(text, self.REFUSAL, (tool, q))        # the standard refusal, no names listed
                self.assertEqual(cl.calls, [], (tool, q))               # nothing reached the backend

    def test_lock_allows_its_own_collection_and_aliases(self):
        lock = self.lock()
        for q in ("sa: invoice", "sc-alpha: invoice", "invoice in:sa", "Note: invoice"):
            cl = FakeClient()
            err, text = call("search", {"query": q}, cl, lock)
            self.assertFalse(err, (q, text))
            want = q if q.startswith("Note") else "invoice"
            self.assertEqual(cl.calls, [("search", want, ["sc-alpha"])], q)
        cl = FakeClient()
        self.assertFalse(call("search", {"query": "x", "collection": "sa"}, cl, lock)[0])   # alias as the argument
        self.assertEqual(cl.calls, [("search", "x", ["sc-alpha"])])
        cl = FakeClient()
        self.assertFalse(call("ask", {"question": "sa: wann?"}, cl, lock)[0])
        self.assertEqual(cl.calls, [("ask", "wann?", ["sc-alpha"])])

    def test_lock_checks_what_comes_back(self):
        lock = self.lock()
        for tool, args in (("search", {"query": "x"}), ("ask", {"question": "x"})):
            err, text = call(tool, args, FakeClient(foreign="sc-beta"), lock)
            self.assertTrue(err, tool)
            self.assertEqual(text, self.REFUSAL)

    def test_unlocked_shortcut_overrides_the_argument(self):
        cl = FakeClient()
        err, text = call("search", {"query": "st: invoice", "collection": "sc-beta"}, cl)
        self.assertFalse(err, text)
        self.assertEqual(cl.calls, [("search", "invoice", ["sc-team"])])
        err, text = call("search", {"query": "invoice in:ghost"}, FakeClient())
        self.assertTrue(err)
        self.assertIn("unknown collection or group after in: ghost (known: all, ", text)

    def test_http_client_tells_the_service_not_to_split_again(self):
        hc = mcp.HttpClient()
        with mock.patch.object(hc, "_call", return_value={"results": []}) as c:
            hc.search("personal: x", ["sc-alpha"], 5, {}, "hybrid")
        self.assertEqual(c.call_args.args[2]["shortcuts"], "0")
        sent = {}

        def fake_open(req, timeout=None):
            sent.update(json.loads(req.data))
            return io.BytesIO(b'{"type": "done"}\n')
        with mock.patch("urllib.request.urlopen", side_effect=fake_open):
            list(hc.ask("personal: x", ["sc-alpha"]))
        self.assertIs(sent["shortcuts"], False)


# ====================================================================== CLI and favourites

class Cli(_Setup):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main(list(argv))
            except SystemExit as e:
                return out.getvalue(), str(e.code)
        return out.getvalue(), None

    def test_search_shortcut(self):
        seen = []

        def fake_search(q, collections=None, **kw):
            seen.append((q, collections))
            return {"query": q, "collections": [], "results": [], "bridge": {}, "timings": {"total_ms": 1},
                    "scope": {"collections": [], "label": "x"}}
        with mock.patch.object(cli, "daemon_up", return_value=False), mock.patch.object(search, "search", fake_search):
            self.run_cli("search", "sa: invoice", "-c", "sc-beta", "--json")
            self.run_cli("search", "invoice", "-c", "sa")
            self.run_cli("search", "Note: x", "-c", "sc-beta", "--files")
            out, err = self.run_cli("search", "x in:ghost")
        self.assertEqual(seen, [("invoice", ["sc-alpha"]), ("invoice", ["sc-alpha"]), ("Note: x", ["sc-beta"])])
        self.assertIn("unknown collection or group after in: ghost", err)

    def test_alias_and_group_commands(self):
        out, err = self.run_cli("alias", "add", "SB", "sc-beta")
        self.assertIsNone(err)
        self.assertIn("sb → folder sc-beta", out)
        out, _ = self.run_cli("aliases")
        self.assertIn("sb  → folder sc-beta", out)
        self.assertIn("st  → group sc-team", out)
        out, _ = self.run_cli("groups")
        self.assertIn("aliases: st", out)
        self.assertIn("(no tab: mse group show sc-quiet)", out)
        out, _ = self.run_cli("list")
        self.assertIn("aliases: sb", out)
        self.assertIn("aliases: st", out)
        out, err = self.run_cli("alias", "add", "in", "sc-beta")
        self.assertIn("reserved", err)
        out, err = self.run_cli("alias", "rm", "sb")
        self.assertIn("removed sb from the folder sc-beta", out)
        out, err = self.run_cli("alias", "rm", "sb")
        self.assertIn("no folder or group has the alias 'sb'", err)
        self.run_cli("group", "show", "sc-quiet")
        self.assertNotIn("hidden", config.get_group(config.load(), "sc-quiet"))
        self.run_cli("group", "hide", "sc-quiet")
        self.assertTrue(config.get_group(config.load(), "sc-quiet")["hidden"])
        self.run_cli("group", "set", "sc-team", "sc-alpha", "sc-beta")              # keeps the alias
        self.assertEqual(config.get_group(config.load(), "sc-team")["aliases"], ["st"])

    def test_favourites_ignore_the_shortcut(self):
        self.assertEqual(feedback.normalize("sa: Invoice"), "invoice")
        self.assertEqual(feedback.normalize("Invoice in:st"), "invoice")
        self.assertEqual(feedback.normalize("Note: invoice"), "note invoice")


if __name__ == "__main__":
    unittest.main()
