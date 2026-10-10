"""Folder groups / multi-folder scope, and connecting agents to a folder (mse/agents_setup.py).

Everything runs in temporary folders and a temporary HOME: no real agent config is read or written.
"""

import io
import json
import os
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

os.environ["MSE_HOME"] = tempfile.mkdtemp(prefix="mse-test-")

from mse import agents_setup, cli, config, indexer, mcp, search  # noqa: E402
from mse.ignore import DEFAULT_EXCLUDES, SECRET_PATTERNS, Matcher  # noqa: E402

FAKE = "sk-live-FAKE-0123456789"          # stands in for an API key; must never appear in any output
FAKE_TOK = "ghp_FAKEFAKEFAKE"


def _mkdir(prefix):
    return Path(tempfile.mkdtemp(prefix=prefix)).resolve()


def _one_insertion(before: str, after: str) -> str:
    """Assert `after` is `before` with one contiguous chunk inserted (every other byte identical); return it."""
    i = 0
    while i < min(len(before), len(after)) and before[i] == after[i]:
        i += 1
    j = 0
    while j < min(len(before), len(after)) - i and before[-1 - j] == after[-1 - j]:
        j += 1
    assert before[:i] + before[len(before) - j:] == before, "before is not a prefix + suffix"
    assert after[:i] + after[len(after) - j:] == before, "more than one region changed"
    return after[i:len(after) - j]


# ====================================================================== groups and scope

class Groups(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dirs = {n: _mkdir(f"mse-g-{n}-") for n in ("g-one", "g-two", "g-three", "g-local")}
        for n, d in cls.dirs.items():
            config.add_collection(str(d), n, **({"in_global": False} if n == "g-local" else {}))

    def tearDown(self):
        for g in list(config.load()["groups"]):
            if g["name"].startswith(("g", "work", "team")):
                config.remove_group(g["name"])

    def test_create_derives_slug_dedupes_and_updates(self):
        g = config.save_group("Work Stuff", ["g-one", "G-ONE", "g-two", " g-two "])
        self.assertEqual(g, {"name": "work-stuff", "label": "Work Stuff", "collections": ["g-one", "g-two"]})
        g = config.save_group("Work Stuff", "g-three")                     # same slug: an update
        self.assertEqual(g["collections"], ["g-three"])
        g = config.save_group("Renamed", ["g-one"], name="work-stuff")      # explicit name keeps the group
        self.assertEqual([x["label"] for x in config.load()["groups"] if x["name"] == "work-stuff"], ["Renamed"])

    def test_validation(self):
        for args, msg in ((("", []), "needs a label"),
                          (("Work", []), "at least one collection"),
                          (("Work", ["nope"]), "unknown collection: nope"),
                          (("All", ["g-one"]), "reserved"),
                          (("x", ["g-one"], "g-one"), "already the name of a collection"),
                          (("x", ["g-one"], "Not A Slug"), "must be a slug")):
            with self.assertRaises(ValueError) as e:
                config.save_group(*args)
            self.assertIn(msg, str(e.exception))
        config.save_group("Team", ["g-one"])
        with self.assertRaises(ValueError) as e:
            config.save_group("Nested", ["team"])
        self.assertIn("groups cannot contain groups", str(e.exception))
        with self.assertRaises(ValueError):                                  # a folder may not take a group's name
            config.add_collection(str(_mkdir("mse-g-clash-")), "team")
        with self.assertRaises(ValueError):
            config.add_collection(str(_mkdir("mse-g-clash-")), "all")

    def test_removing_a_collection_empties_groups_but_keeps_them(self):
        d = _mkdir("mse-g-gone-")
        config.add_collection(str(d), "g-gone")
        config.save_group("gx", ["g-gone"])
        config.save_group("gy", ["g-one", "g-gone"])
        config.remove_collection("g-gone")
        groups = {g["name"]: g["collections"] for g in config.load()["groups"]}
        self.assertEqual(groups["gx"], [])
        self.assertEqual(groups["gy"], ["g-one"])
        self.assertEqual(config.resolve_scope(config.load(), "gx")["collections"], [])

    def test_resolve_scope(self):
        cfg = config.load()
        config.save_group("Work", ["g-two", "g-local"])                     # g-local is not in global search
        cfg = config.load()
        everything = config.global_names(cfg)
        self.assertNotIn("g-local", everything)
        self.assertEqual(config.resolve_scope(cfg, None), {"collections": everything, "label": "All", "tokens": [],
                                                            "unknown": []})
        self.assertEqual(config.resolve_scope(cfg, "ALL")["collections"], everything)
        r = config.resolve_scope(cfg, "work")
        self.assertEqual((r["collections"], r["label"]), (["g-two", "g-local"], "Work"))   # in_global ignored
        r = config.resolve_scope(cfg, "g-one, g-three")
        self.assertEqual((r["collections"], r["label"]), (["g-one", "g-three"], "g-one + g-three"))
        r = config.resolve_scope(cfg, ["g-two,work", "g-one"])                # duplicates removed, order kept
        self.assertEqual((r["collections"], r["label"]), (["g-two", "g-local", "g-one"], "g-two + Work + g-one"))
        with self.assertRaises(ValueError) as e:
            config.resolve_scope(cfg, "g-one,nope,nada")
        self.assertIn("unknown collections or groups: nope, nada", str(e.exception))
        self.assertEqual(config.resolve_scope(cfg, "g-one,nope", strict=False)["collections"], ["g-one"])

    def test_search_scope(self):
        config.save_group("Work", ["g-two", "g-local"])
        config.save_group("gempty", ["g-three"])
        config.remove_collection("g-three")
        try:
            res = search.search("invoice", collections=["work"], mode="keyword")
            self.assertEqual(res["scope"], {"collections": ["g-two", "g-local"], "label": "Work"})
            res = search.search("invoice", collections=["g-one", "work", "g-one"], mode="keyword")
            self.assertEqual(sorted(res["collections"]), ["g-local", "g-one", "g-two"])
            res = search.search("invoice", mode="keyword")
            self.assertEqual(res["scope"]["label"], "All")
            self.assertNotIn("g-local", res["collections"])
            res = search.search("invoice", collections=["gempty"], mode="keyword")    # empty group: nothing, not All
            self.assertEqual(res["collections"], [])
            self.assertEqual(res["results"], [])
        finally:
            config.add_collection(str(self.dirs["g-three"]), "g-three")


# ====================================================================== MCP: groups and the lock

class FakeClient:
    def __init__(self, resolves_to="grp-a"):
        self.calls, self.resolves_to = [], resolves_to

    def search(self, q, colls, limit, filters, mode, precise=False):
        self.calls.append(("search", colls))
        return {"query": q, "collections": ["grp-a", "grp-b"], "results": [], "bridge": {},
                "scope": {"collections": ["grp-a", "grp-b"], "label": "Pair"}}

    def get(self, coll, ref, max_chars, page):
        self.calls.append(("get", coll, ref))
        if coll != self.resolves_to:
            return None
        return {"path": ref, "docid": "abcd1234", "collection": coll, "kind": "text", "text": "body"}

    def similar(self, coll, ref, limit, scope=None):
        self.calls.append(("similar", coll, ref, scope))
        return {"query": "q", "collections": [], "results": [], "bridge": {}, "collection": self.resolves_to}

    def collections(self):
        return [{"name": n, "path": f"/x/{n}", "stats": {"files": 2}} for n in ("grp-a", "grp-b")]

    def groups(self):
        return [{"name": "pair", "label": "Pair", "collections": ["grp-a", "grp-b"], "files": 4}]

    def mark_best(self, query, coll, ref):
        self.calls.append(("mark_best", coll, ref))
        return {"ok": True, "id": 3}

    def sync(self, coll, path):
        self.calls.append(("sync", coll, path))
        return {"jobs": []}


def call(name, args, client, lock=None, cwd=None):
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    r = mcp.handle(req, client, lock.cwd if lock else cwd, lock)["result"]
    return r["isError"], r["content"][0]["text"]


class McpScope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a, cls.b = _mkdir("mse-grp-a-"), _mkdir("mse-grp-b-")
        config.add_collection(str(cls.a), "grp-a")
        config.add_collection(str(cls.b), "grp-b")
        config.save_group("Pair", ["grp-a", "grp-b"])
        config.save_group("Solo", ["grp-a"])

    @classmethod
    def tearDownClass(cls):
        for g in ("pair", "solo"):
            config.remove_group(g)

    def test_unlocked_accepts_groups_and_lists(self):
        cl = FakeClient()
        self.assertFalse(call("search", {"query": "x", "collection": "pair"}, cl)[0])
        self.assertFalse(call("search", {"query": "x", "collection": "grp-a, grp-b"}, cl)[0])
        err, text = call("search", {"query": "x", "collection": "Pair"}, cl)
        self.assertIn('in Pair (grp-a, grp-b)', text)
        self.assertEqual([c for c in cl.calls], [("search", ["pair"]), ("search", ["grp-a", "grp-b"]),
                                                 ("search", ["pair"])])
        err, text = call("search", {"query": "x", "collection": "grp-a,nope"}, cl)
        self.assertTrue(err)
        self.assertIn("unknown collection or group: nope", text)

    def test_get_similar_sync_mark_best_with_a_group(self):
        cl = FakeClient(resolves_to="grp-b")
        err, text = call("get", {"ref": "a.md", "collection": "pair"}, cl)
        self.assertFalse(err)
        self.assertIn("body", text)
        self.assertEqual(cl.calls, [("get", "grp-a", "a.md"), ("get", "grp-b", "a.md")])   # members, in order
        cl.calls.clear()
        call("similar", {"ref": "a.md", "collection": "pair"}, cl)
        call("similar", {"ref": "a.md", "collection": "all"}, cl)
        call("sync", {"collection": "pair"}, cl)
        call("mark_best", {"query": "x", "ref": "a.md", "collection": "pair"}, cl)
        self.assertEqual(cl.calls, [("similar", None, "a.md", ["pair"]), ("similar", None, "a.md", ["all"]),
                                    ("sync", "pair", None), ("mark_best", None, "a.md")])

    def test_collections_tool_lists_groups_unless_locked(self):
        err, text = call("collections", {}, FakeClient())
        self.assertIn("- pair (Pair): grp-a, grp-b, 4 files", text)
        lock = mcp.Lock(str(self.a))
        err, text = call("collections", {}, FakeClient(), lock)
        self.assertNotIn("pair", text)

    def test_lock_allows_only_scopes_of_the_locked_collection(self):
        lock = mcp.Lock(str(self.a))
        refusal = "This session is limited to the 'grp-a' collection by this folder's MCP config."
        for spec in ("solo", "grp-a,grp-a", "Solo, grp-a", "GRP-A"):
            cl = FakeClient()
            err, text = call("search", {"query": "x", "collection": spec}, cl, lock)
            self.assertFalse(err, spec)
            self.assertEqual(cl.calls, [("search", ["grp-a"])], spec)        # forced to the locked collection
        for spec in ("pair", "grp-a,grp-b", "all", "solo,all", "nope", "grp-a,nope"):
            cl = FakeClient()
            for tool, args in (("search", {"query": "x"}), ("get", {"ref": "a.md"}), ("similar", {"ref": "a.md"}),
                               ("sync", {})):
                err, text = call(tool, {**args, "collection": spec}, cl, lock)
                self.assertTrue(err, (tool, spec))
                self.assertEqual(text, refusal)
            self.assertEqual(cl.calls, [])


# ====================================================================== agents_setup

def _write(p: Path, text: str, mode=0o644):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    os.chmod(p, mode)


MCP_JSON = """{
    "mcpServers": {
        "github": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_TOKEN": "%s"}
        },
        "example": {"command": "/opt/example", "env": {"API_KEY": "%s"}}
    },
    "other": [1, 2.50, true, null]
}
""" % (FAKE_TOK, FAKE)

CURSOR_JSON = '{"mcpServers":{"example":{"url":"http://x","headers":{"Authorization":"Bearer %s"}}}}' % FAKE

SETTINGS_JSON = """{
  "permissions": {
    "allow": ["Bash(ls:*)"]
  },
  "enabledMcpjsonServers": [
    "github"
  ]
}
"""

CODEX_TOML = """# project codex config
model = "o3"

[mcp_servers.example]
command = "/opt/example"
env = { API_KEY = "%s" }
""" % FAKE

OPENCODE_JSON = """{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "example": {"type": "remote", "url": "http://x", "headers": {"k": "%s"}}
  }
}
""" % FAKE


class AgentsFolder(unittest.TestCase):
    def setUp(self):
        self.home = _mkdir("mse-home-")
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()
        self.root = _mkdir("mse-agents-")
        self.name = config.slugify(self.root.name)
        config.add_collection(str(self.root), self.name)
        self.files = {".mcp.json": MCP_JSON, ".cursor/mcp.json": CURSOR_JSON,
                      ".claude/settings.local.json": SETTINGS_JSON, ".codex/config.toml": CODEX_TOML,
                      "opencode.json": OPENCODE_JSON}

    def seed(self):
        """Existing agent configs with other servers and fake keys, in different formatting styles."""
        for rel, text in self.files.items():
            _write(self.root / rel, text, 0o644 if rel != ".mcp.json" else 0o640)

    def tearDown(self):
        config.remove_collection(self.name)
        self.env.stop()

    def read(self, rel):
        return (self.root / rel).read_text()

    def test_install_merges_minimally_and_round_trips(self):
        self.seed()
        res = agents_setup.install(self.name)
        actions = {Path(r["file"]).relative_to(self.root).as_posix(): r["action"] for r in res}
        self.assertEqual(actions, {".mcp.json": "added", ".claude/settings.local.json": "added",
                                   ".cursor/mcp.json": "added", ".codex/config.toml": "added",
                                   ".grok/config.toml": "added", "opencode.json": "added"})
        self.assertNotIn(FAKE, json.dumps(res))
        # every other byte of each existing file is untouched: the edit is one inserted chunk without secrets
        for rel, before in self.files.items():
            chunk = _one_insertion(before, self.read(rel))
            self.assertIn("mysearch", chunk, rel)
            self.assertNotIn("FAKE", chunk, rel)
        # and the other servers / keys parse to exactly the same values
        after = json.loads(self.read(".mcp.json"))
        orig = json.loads(MCP_JSON)
        self.assertEqual(after["other"], orig["other"])
        self.assertEqual({k: v for k, v in after["mcpServers"].items() if k != "mysearch"}, orig["mcpServers"])
        entry = after["mcpServers"]["mysearch"]
        self.assertEqual(entry, {"command": agents_setup.mse_bin(), "args": ["mcp"],
                                 "env": {"MSE_CWD": str(self.root), "MSE_LOCK": "1"}})
        self.assertTrue(Path(entry["command"]).is_absolute() and entry["command"].endswith("/mse"))
        self.assertEqual(json.loads(self.read(".claude/settings.local.json"))["enabledMcpjsonServers"],
                         ["github", "mysearch"])
        oc = json.loads(self.read("opencode.json"))["mcp"]["mysearch"]
        self.assertEqual(oc, {"type": "local", "command": [agents_setup.mse_bin(), "mcp"],
                              "environment": {"MSE_CWD": str(self.root), "MSE_LOCK": "1"}, "enabled": True})
        import tomllib
        for rel in (".codex/config.toml", ".grok/config.toml"):
            t = tomllib.loads(self.read(rel))
            self.assertEqual(t["mcp_servers"]["mysearch"]["env"], {"MSE_CWD": str(self.root), "MSE_LOCK": "1"})
        # modes: kept for existing files, 0600 for new ones
        self.assertEqual(stat.S_IMODE((self.root / ".mcp.json").stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE((self.root / "opencode.json").stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((self.root / ".grok/config.toml").stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.root.rglob("*.mse-tmp")], [])

        # idempotent
        snapshot = {rel: self.read(rel) for rel in list(self.files) + [".grok/config.toml"]}
        self.assertEqual({r["action"] for r in agents_setup.install(self.name)}, {"present"})
        self.assertEqual({rel: self.read(rel) for rel in snapshot}, snapshot)

        # status: connected everywhere; Codex is not a trusted project in this (temporary) home
        st = {r["id"]: r for r in agents_setup.status(self.name)}
        self.assertEqual({k: v["state"] for k, v in st.items()},
                         {k: "connected" for k in ("claude", "claude-approval", "cursor", "codex", "grok", "opencode")})
        self.assertIn("not trusted", st["codex"]["note"])
        self.assertNotIn(FAKE, json.dumps(st))

        # remove: every pre-existing file is byte-identical to the original; the created one stays (emptied)
        res = agents_setup.remove(self.name)
        self.assertEqual({r["action"] for r in res}, {"removed"})
        for rel, before in self.files.items():
            self.assertEqual(self.read(rel), before, rel)
        self.assertTrue((self.root / ".grok/config.toml").exists())
        self.assertNotIn("mysearch", self.read(".grok/config.toml"))
        self.assertEqual({r["action"] for r in agents_setup.remove(self.name)}, {"absent"})
        self.assertEqual({r["state"] for r in agents_setup.status(self.name)}, {"missing"})

    def test_fresh_folder_round_trip(self):
        self.assertEqual({r["action"] for r in agents_setup.install(self.name)}, {"added"})
        for rel in (".mcp.json", ".cursor/mcp.json"):
            self.assertEqual(stat.S_IMODE((self.root / rel).stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.read("opencode.json"))["$schema"], agents_setup.OPENCODE_SCHEMA)
        agents_setup.remove(self.name)
        for rel in (".mcp.json", ".cursor/mcp.json", "opencode.json", ".claude/settings.local.json",
                    ".codex/config.toml", ".grok/config.toml"):
            self.assertTrue((self.root / rel).exists(), rel)                 # never deleted, even when empty
            self.assertNotIn('"mysearch"', self.read(rel))

    def test_entries_pinned_elsewhere_are_left_alone_by_remove(self):
        other = {"mcpServers": {"mysearch": {"command": "/x/mse", "args": ["mcp"],
                                             "env": {"MSE_CWD": "/somewhere/else", "MSE_LOCK": "1"}}}}
        _write(self.root / ".cursor/mcp.json", json.dumps(other))
        before = self.read(".cursor/mcp.json")
        self.assertEqual({r["id"]: r["state"] for r in agents_setup.status(self.name)}["cursor"], "other")
        res = {r["id"]: r["action"] for r in agents_setup.remove(self.name)}
        self.assertEqual(res["cursor"], "kept")
        self.assertEqual(self.read(".cursor/mcp.json"), before)
        res = {r["id"]: r["action"] for r in agents_setup.install(self.name)}   # install points it here
        self.assertEqual(res["cursor"], "updated")
        self.assertEqual(json.loads(self.read(".cursor/mcp.json"))["mcpServers"]["mysearch"]["env"]["MSE_CWD"],
                         str(self.root))

    def test_extra_keys_in_our_entry_are_kept(self):
        _write(self.root / ".mcp.json", json.dumps({"mcpServers": {"mysearch": {
            "command": "/old/mse", "args": ["mcp"], "timeout": 30, "env": {"MSE_CWD": str(self.root), "X": "1"}}}},
            indent=2))
        res = {r["id"]: r["action"] for r in agents_setup.install(self.name)}
        self.assertEqual(res["claude"], "updated")
        e = json.loads(self.read(".mcp.json"))["mcpServers"]["mysearch"]
        self.assertEqual(e["timeout"], 30)
        self.assertEqual(e["env"], {"MSE_CWD": str(self.root), "X": "1", "MSE_LOCK": "1"})
        self.assertEqual(e["command"], agents_setup.mse_bin())

    def test_unreadable_files_are_left_untouched(self):
        jsonc = '{\n  // comments are not JSON\n  "mcp": {}\n}\n'
        _write(self.root / "opencode.json", jsonc)
        _write(self.root / ".codex/config.toml", "this is = = not toml\n")
        res = {r["id"]: r for r in agents_setup.install(self.name)}
        self.assertEqual(res["opencode"]["action"], "skipped")
        self.assertEqual(res["codex"]["action"], "skipped")
        self.assertEqual(self.read("opencode.json"), jsonc)
        self.assertEqual(self.read(".codex/config.toml"), "this is = = not toml\n")
        st = {r["id"]: r["state"] for r in agents_setup.status(self.name)}
        self.assertEqual((st["opencode"], st["codex"], st["claude"]), ("unreadable", "unreadable", "connected"))

    def test_symlinked_config_is_written_through(self):
        target = self.root / "real-cursor.json"
        _write(target, CURSOR_JSON)
        (self.root / ".cursor").mkdir()
        (self.root / ".cursor/mcp.json").symlink_to(target)
        agents_setup.install(self.name)
        self.assertTrue((self.root / ".cursor/mcp.json").is_symlink())
        self.assertIn("mysearch", json.loads(target.read_text())["mcpServers"])

    def test_codex_trust_is_read_from_home(self):
        _write(self.home / ".codex/config.toml",
               f'api_key = "{FAKE}"\n\n[projects."{self.root}"]\ntrust_level = "trusted"\n')
        self.assertEqual(agents_setup.codex_trust(self.root), "trusted")
        agents_setup.install(self.name)
        st = {r["id"]: r for r in agents_setup.status(self.name)}
        self.assertIn("trusted Codex project", st["codex"]["note"])
        self.assertNotIn(FAKE, json.dumps(st))

    def test_agent_files_are_never_indexed(self):
        agents_setup.install(self.name)
        m = Matcher(SECRET_PATTERNS + DEFAULT_EXCLUDES)
        for rel in (".mcp.json", ".claude/settings.local.json", ".cursor/mcp.json", ".codex/config.toml",
                    ".grok/config.toml", "opencode.json", "sub/opencode.json", "sub/.mcp.json"):
            self.assertTrue(m.match(rel), rel)
        (self.root / "note.md").write_text("hello")
        coll = config.get_collection(config.load(), self.name)
        walked = [rel for rel, _, _ in indexer.walk(coll, [])]
        self.assertEqual(walked, ["note.md"])

    def test_cli_prints_no_secrets(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["agents", "install", self.name])
            cli.main(["agents", "status", self.name])
            cli.main(["agents", "status", self.name, "--json"])
            cli.main(["agents", "remove", self.name])
        text = out.getvalue()
        self.assertIn(".mcp.json", text)
        self.assertIn("connected", text)
        for secret in (FAKE, FAKE_TOK, "FAKE"):
            self.assertNotIn(secret, text)

    def test_remove_by_path_after_the_collection_is_gone(self):
        agents_setup.install(self.name)
        with self.assertRaises(ValueError):
            agents_setup.install(str(self.root / "sub-not-a-collection"))
        res = agents_setup.remove(str(self.root))           # a folder path works too
        self.assertEqual({r["action"] for r in res}, {"removed"})


class AgentsGlobal(unittest.TestCase):
    def setUp(self):
        self.home = _mkdir("mse-ghome-")
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()
        self.desktop = self.home / "Library/Application Support/Claude/claude_desktop_config.json"
        _write(self.desktop, json.dumps({"mcpServers": {"x": {"command": "y", "env": {"K": FAKE}}},
                                         "globalShortcut": "Alt+Space"}, indent=2) + "\n")
        self.codex = self.home / ".codex/config.toml"
        _write(self.codex, f'model = "o3"\napi_key = "{FAKE}"\n\n[projects."/a"]\ntrust_level = "trusted"\n', 0o600)
        (self.home / ".cursor").mkdir()                  # Cursor installed, no mcp.json yet; Grok and Antigravity absent

    def tearDown(self):
        self.env.stop()

    def test_install_status_remove(self):
        before = {p: p.read_text() for p in (self.desktop, self.codex)}
        res = {r["id"]: r["action"] for r in agents_setup.install_global()}
        self.assertEqual(res, {"claude-desktop": "added", "cursor": "added", "grok": "skipped", "codex": "added",
                               "antigravity": "skipped"})
        self.assertFalse((self.home / ".grok").exists())
        self.assertFalse((self.home / ".gemini").exists())
        e = json.loads(self.desktop.read_text())["mcpServers"]["mysearch"]
        self.assertEqual(e, {"command": agents_setup.mse_bin(), "args": ["mcp"]})             # no env
        self.assertEqual(stat.S_IMODE(self.codex.stat().st_mode), 0o600)
        for p, text in before.items():
            self.assertNotIn("FAKE", _one_insertion(text, p.read_text()))
        st = {r["id"]: r["state"] for r in agents_setup.status_global()}
        self.assertEqual(st, {"claude-desktop": "connected", "cursor": "connected", "grok": "missing",
                              "codex": "connected", "antigravity": "missing"})
        self.assertEqual({r["action"] for r in agents_setup.install_global()} - {"skipped"}, {"present"})
        agents_setup.remove_global()
        for p, text in before.items():
            self.assertEqual(p.read_text(), text)
        self.assertNotIn(FAKE, json.dumps(agents_setup.status_global()))


# ====================================================================== HTTP API

class _Jobs:
    def __init__(self):
        self.submitted = []

    def submit(self, kind, collection=None, path=None, reason="manual", paths=None):
        self.submitted.append((kind, collection, path))
        return {"jobs": [{"kind": kind, "collection": collection, "path": path}]}

    def status(self):
        return {"current": None, "queued": []}


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from mse import server
        cls.server_mod = server
        cls.old_jobs = server.JOBS
        server.JOBS = cls.jobs = _Jobs()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.token = config.load()["token"]
        cls.home = _mkdir("mse-apihome-")
        cls.env = mock.patch.dict(os.environ, {"HOME": str(cls.home)})
        cls.env.start()
        cls.d1, cls.d2 = _mkdir("mse-api-one-"), _mkdir("mse-api-two-")
        config.add_collection(str(cls.d1), "api-one")
        config.add_collection(str(cls.d2), "api-two")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server_mod.JOBS = cls.old_jobs
        cls.env.stop()
        for g in config.load()["groups"]:
            if g["name"].startswith("api"):
                config.remove_group(g["name"])

    def req(self, method, path, body=None, params=None):
        url = f"http://127.0.0.1:{self.port}{path}" + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-MSE-Token": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_groups_crud_and_search_scope(self):
        code, g = self.req("POST", "/api/groups", {"label": "API Work", "collections": ["api-one", "api-two"]})
        self.assertEqual((code, g), (200, {"name": "api-work", "label": "API Work",
                                           "collections": ["api-one", "api-two"], "files": 0,
                                           "aliases": [], "hidden": False}))
        code, g = self.req("POST", "/api/groups", {"name": "api-work", "label": "API Work", "collections": ["api-two"]})
        self.assertEqual(g["collections"], ["api-two"])
        code, err = self.req("POST", "/api/groups", {"label": "api-bad", "collections": ["nope"]})
        self.assertEqual(code, 400)
        self.assertIn("unknown collection: nope", err["error"])
        code, body = self.req("GET", "/api/groups")
        self.assertIn("api-work", [x["name"] for x in body["groups"]])
        code, body = self.req("GET", "/api/collections")
        self.assertIn("api-work", [x["name"] for x in body["groups"]])
        self.assertIn("api-one", [x["name"] for x in body["collections"]])

        code, res = self.req("GET", "/api/search", params={"q": "invoice", "mode": "keyword", "c": "api-work,api-one"})
        self.assertEqual(code, 200)
        self.assertEqual(res["scope"], {"collections": ["api-one", "api-two"], "label": "API Work + api-one"})
        code, res = self.req("GET", "/api/search", params={"q": "invoice", "mode": "keyword", "c": "all"})
        self.assertEqual(res["scope"]["label"], "All")
        code, err = self.req("GET", "/api/search", params={"q": "x", "c": "api-one,ghost"})
        self.assertEqual(code, 400)
        self.assertIn("ghost", err["error"])
        code, err = self.req("GET", "/api/similar", params={"ref": "#00000000", "in": "ghost"})
        self.assertEqual(code, 400)

        code, body = self.req("POST", "/api/sync", {"collection": "api-work"})
        self.assertEqual(self.jobs.submitted[-1], ("sync", "api-two", None))
        self.assertEqual(self.req("POST", "/api/groups/api-work/remove", {}), (200, {"ok": True}))
        self.assertEqual(self.req("POST", "/api/groups/api-work/remove", {})[0], 404)

    def test_settings_and_status_additions(self):
        code, s = self.req("GET", "/api/settings")
        self.assertEqual(set(s["agents"]), {"redact", "default_scope"})
        self.assertIn(".mcp.json", s["secret_patterns"])
        self.assertEqual(s["search"]["hub_penalty"], 0.6)
        code, st = self.req("GET", "/api/status")
        self.assertTrue(st["mse_bin"].endswith("/mse") and Path(st["mse_bin"]).is_absolute())
        from mse import paths
        self.assertEqual(st["data_dir"], str(paths.DATA))

    def test_agents_endpoints(self):
        code, body = self.req("GET", "/api/collections/api-one/agents")
        self.assertEqual(code, 200)
        self.assertEqual({r["state"] for r in body["agents"]}, {"missing"})
        self.assertEqual(self.req("POST", "/api/collections/api-one/agents", {})[0], 400)
        self.assertEqual(self.req("GET", "/api/collections/ghost/agents")[0], 404)
        code, body = self.req("POST", "/api/collections/api-one/agents", {"install": True})
        self.assertEqual({f["action"] for f in body["files"]}, {"added"})
        self.assertTrue(all({"file", "action"} <= set(f) for f in body["files"]))
        code, body = self.req("GET", "/api/collections/api-one/agents")
        self.assertEqual({r["state"] for r in body["agents"]}, {"connected"})
        code, body = self.req("POST", "/api/collections/api-one/agents", {"install": False})
        self.assertEqual({f["action"] for f in body["files"]}, {"removed"})
        code, body = self.req("GET", "/api/agents/global")
        self.assertEqual({r["state"] for r in body["agents"]}, {"missing"})       # nothing installed in this HOME
        code, body = self.req("POST", "/api/agents/global", {"install": True})
        self.assertEqual({f["action"] for f in body["files"]}, {"skipped"})

    def test_add_with_agents_and_remove_with_disconnect(self):
        d = _mkdir("mse-api-three-")
        code, body = self.req("POST", "/api/collections", {"path": str(d), "name": "api-three", "connect_agents": True})
        self.assertEqual(code, 200)
        self.assertEqual({f["action"] for f in body["agents"]["files"]}, {"added"})
        self.assertTrue((d / ".mcp.json").exists())
        config.save_group("api-g3", ["api-three", "api-one"])
        code, body = self.req("POST", "/api/collections/api-three/remove", {"disconnect_agents": True})
        self.assertEqual(code, 200)
        self.assertEqual({f["action"] for f in body["agents"]["files"]}, {"removed"})
        self.assertNotIn("mysearch", json.loads((d / ".mcp.json").read_text())["mcpServers"])
        self.assertEqual(config.get_group(config.load(), "api-g3")["collections"], ["api-one"])


if __name__ == "__main__":
    unittest.main()
