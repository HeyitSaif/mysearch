"""MSE_LOCK: a stdio MCP session limited to the collection of its folder, failing closed outside any collection."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["MSE_HOME"] = tempfile.mkdtemp(prefix="mse-test-")

from mse import config, mcp  # noqa: E402


class FakeClient:
    """Records calls; resolves refs to whatever collection a test says."""

    def __init__(self, resolves_to: str):
        self.calls, self.resolves_to = [], resolves_to

    def _res(self, coll):
        return {"query": "q", "collections": [coll] if coll else ["a", "b"], "results": [], "bridge": {},
                "collection": self.resolves_to}

    def search(self, q, colls, limit, filters, mode, precise=False):
        self.calls.append(("search", colls))
        return self._res(colls[0] if colls else None)

    def get(self, coll, ref, max_chars, page):
        self.calls.append(("get", coll, ref))
        return {"path": ref, "docid": "abcd1234", "collection": self.resolves_to, "kind": "text", "text": "body"}

    def similar(self, coll, ref, limit):
        self.calls.append(("similar", coll, ref))
        return self._res(coll)

    def collections(self):
        return [{"name": n, "path": f"/x/{n}", "stats": {}} for n in ("lock-a", "lock-b")]

    def mark_best(self, query, coll, ref):
        self.calls.append(("mark_best", coll, ref))
        if coll is None:
            return {"ok": True, "id": 7}
        raise RuntimeError("file not found: " + ref)

    def sync(self, coll, path):
        self.calls.append(("sync", coll, path))
        return {"jobs": []}


def call(name, args, client, lock):
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    r = mcp.handle(req, client, lock.cwd if lock else None, lock)["result"]
    return r["isError"], r["content"][0]["text"]


class McpLock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root_a = Path(tempfile.mkdtemp(prefix="mse-lock-a-"))
        cls.root_b = Path(tempfile.mkdtemp(prefix="mse-lock-b-"))
        (cls.root_a / "sub").mkdir()
        config.add_collection(str(cls.root_a), "lock-a")
        config.add_collection(str(cls.root_b), "lock-b")
        cls.outside = Path(tempfile.mkdtemp(prefix="mse-lock-none-"))

    def lock(self):
        return mcp.Lock(str(self.root_a / "sub"))

    def test_lock_from_env(self):
        with mock.patch.dict(os.environ, {"MSE_LOCK": "1"}):
            self.assertEqual(mcp.lock_from_env(str(self.root_a)).name, "lock-a")
        with mock.patch.dict(os.environ, {"MSE_LOCK": "true"}):
            self.assertIsNotNone(mcp.lock_from_env(str(self.root_a)))
        with mock.patch.dict(os.environ, {"MSE_LOCK": "0"}):
            self.assertIsNone(mcp.lock_from_env(str(self.root_a)))
        env = {k: v for k, v in os.environ.items() if k != "MSE_LOCK"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(mcp.lock_from_env(str(self.root_a)))

    def test_missing_collection_means_locked(self):
        lock, cl = self.lock(), FakeClient("lock-a")
        for name, args in (("search", {"query": "x"}), ("get", {"ref": "#abcd1234"}), ("multi_get", {"refs": ["a.md"]}),
                           ("similar", {"ref": "a.md"}), ("sync", {})):
            err, text = call(name, args, cl, lock)
            self.assertFalse(err, (name, text))
        self.assertEqual(cl.calls, [("search", ["lock-a"]), ("get", "lock-a", "#abcd1234"), ("get", "lock-a", "a.md"),
                                    ("similar", "lock-a", "a.md"), ("sync", "lock-a", None)])
        self.assertFalse(call("search", {"query": "x", "collection": "lock-a"}, cl, lock)[0])

    def test_other_collections_and_all_are_refused(self):
        lock, cl = self.lock(), FakeClient("lock-a")
        msg = "This session is limited to the 'lock-a' collection by this folder's MCP config."
        for name, args in (("search", {"query": "x", "collection": "all"}),
                           ("search", {"query": "x", "collection": "lock-b"}),
                           ("get", {"ref": "a.md", "collection": "lock-b"}),
                           ("similar", {"ref": "a.md", "collection": "all"}),
                           ("mark_best", {"query": "x", "ref": "a.md", "collection": "all"}),
                           ("sync", {"collection": "lock-b"}),
                           ("ask", {"question": "x", "collection": "lock-b"})):     # any tool, also future ones
            err, text = call(name, args, cl, lock)
            self.assertTrue(err, name)
            self.assertEqual(text, msg)
        self.assertEqual(cl.calls, [])                     # nothing reached the backend

    def test_paths_outside_the_collection_are_refused(self):
        lock, cl = self.lock(), FakeClient("lock-a")
        for name, args in (("get", {"ref": str(self.root_b / "x.md")}), ("mark_best", {"query": "x", "ref": str(self.root_b / "x.md")}),
                           ("multi_get", {"refs": ["ok.md", str(self.outside / "y.md")]}),
                           ("sync", {"path": str(self.root_b / "x.md")})):
            err, text = call(name, args, cl, lock)
            self.assertTrue(err, name)
            self.assertIn("is outside the 'lock-a' collection", text)
        self.assertFalse(call("get", {"ref": str(self.root_a / "sub" / "z.md")}, cl, lock)[0])

    def test_get_only_resolves_inside_the_lock(self):
        lock = self.lock()
        err, text = call("get", {"ref": "#abcd1234"}, FakeClient("lock-b"), lock)   # backend found it elsewhere
        self.assertFalse(err)
        self.assertEqual(text, "not found: #abcd1234 in 'lock-a'")
        self.assertIn("not found", call("multi_get", {"refs": ["a.md"]}, FakeClient("lock-b"), lock)[1])
        self.assertIn("not found", call("similar", {"ref": "a.md"}, FakeClient("lock-b"), lock)[1])

    def test_mark_best_never_looks_elsewhere(self):
        lock, cl = self.lock(), FakeClient("lock-a")
        err, text = call("mark_best", {"query": "x", "ref": "#ffff0000"}, cl, lock)
        self.assertTrue(err)
        self.assertIn("not found", text)
        self.assertEqual(cl.calls, [("mark_best", "lock-a", "#ffff0000")])      # no retry with collection None
        # an unlocked session in the same folder keeps the fallback to all collections
        cl2 = FakeClient("lock-a")
        text = mcp.call_tool("mark_best", {"query": "x", "ref": "#ffff0000"}, cl2, str(self.root_a / "sub"))
        self.assertIn("feedback #7", text)
        self.assertEqual(cl2.calls, [("mark_best", "lock-a", "#ffff0000"), ("mark_best", None, "#ffff0000")])

    def test_collections_lists_only_the_locked_one(self):
        err, text = call("collections", {}, FakeClient("lock-a"), self.lock())
        self.assertFalse(err)
        self.assertIn("- lock-a:", text)
        self.assertNotIn("lock-b", text)
        self.assertIn("locked to 'lock-a'", text)

    def test_tools_list_describes_the_lock(self):
        lock = self.lock()
        tools = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, FakeClient("lock-a"), lock.cwd, lock)
        for t in tools["result"]["tools"]:
            coll = t["inputSchema"].get("properties", {}).get("collection")
            if coll:
                self.assertIn("Only 'lock-a' is allowed", coll["description"])
        self.assertNotIn("Only", mcp.TOOLS[0]["inputSchema"]["properties"]["collection"]["description"])  # not mutated
        init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, None, lock.cwd, lock)
        self.assertIn("limited to the 'lock-a' collection", init["result"]["instructions"])

    def test_fail_closed_outside_any_collection(self):
        lock, cl = mcp.Lock(str(self.outside)), FakeClient("lock-a")
        self.assertIsNone(lock.name)
        for name, args in (("search", {"query": "x"}), ("get", {"ref": "#abcd1234"}), ("collections", {}),
                           ("mark_best", {"query": "x", "ref": "a.md"}), ("sync", {}), ("similar", {"ref": "a"}),
                           ("multi_get", {"refs": ["a"]})):
            err, text = call(name, args, cl, lock)
            self.assertTrue(err, name)
            self.assertIn("MSE_LOCK is set, but this folder", text)
            self.assertIn("is not inside any indexed MySearch collection", text)
        self.assertEqual(cl.calls, [])
        tools = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, cl, lock.cwd, lock)["result"]["tools"]
        self.assertIn("every call fails", tools[0]["inputSchema"]["properties"]["collection"]["description"])

    def test_unlocked_sessions_unchanged(self):
        cl = FakeClient("lock-b")
        self.assertFalse(call("search", {"query": "x", "collection": "all"}, cl, None)[0])
        self.assertFalse(call("get", {"ref": "a.md", "collection": "lock-b"}, cl, None)[0])
        self.assertEqual(cl.calls, [("search", None), ("get", "lock-b", "a.md")])


if __name__ == "__main__":
    unittest.main()
