"""The MCP tools `timeline` and `facets`: arguments and their validation, the scope (default folder, groups, `all`, the lock),
the text an agent reads, the HTTP client's requests, and a real tiny index through LocalClient. Fictional documents only.
Run: .venv/bin/python -m unittest -v tests.test_mcp_timeline"""

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import config, mcp, store  # noqa: E402
from tests.test_docmeta_index import M, add_doc  # noqa: E402

TL = {"collections": ["c1"], "types": ["receipt"], "granularity": "month", "from": "2025-01", "to": "2025-04", "first": "2025-01",
      "last": "2025-04", "total": 3, "undated": 2, "gaps": ["2025-02", "2025-03"],
      "buckets": [{"month": "2025-01", "count": 2, "amounts": {"EUR": 12.5, "USD": 1234.5}, "with_amount": 2, "query": "type:receipt docdate:2025-01"},
                  {"month": "2025-02", "count": 0, "amounts": {}, "with_amount": 0, "query": "type:receipt docdate:2025-02"},
                  {"month": "2025-03", "count": 0, "amounts": {}, "with_amount": 0, "query": "type:receipt docdate:2025-03"},
                  {"month": "2025-04", "count": 1, "amounts": {"EUR": 8.0}, "with_amount": 0, "query": "type:receipt docdate:2025-04"}]}
FC = {"collections": ["c1"], "files": 12, "classified": 7, "unclassified": 5, "undated": 1,
      "doctypes": [{"doctype": "invoice", "count": 4}, {"doctype": "receipt", "count": 3}],
      "counterparties": [{"name": "Acme Parts GmbH", "count": 3}, {"name": "Cafe Beispiel", "count": 2}],
      "months": [{"month": "2025-03", "count": 4}, {"month": "2025-04", "count": 2}], "currencies": [{"currency": "EUR", "count": 6}]}


class Fake:
    """Answers timeline / facets with canned data for whatever collections it is asked about."""

    def __init__(self, foreign=None):
        self.calls, self.foreign = [], foreign

    def _names(self, colls):
        return list(colls) if colls else ["c1", "c2"]

    def timeline(self, colls, types, date_from, date_to):
        self.calls.append(("timeline", colls, types, date_from, date_to))
        return {**TL, "collections": self.foreign or self._names(colls)}

    def facets(self, colls, types):
        self.calls.append(("facets", colls, types))
        return {**FC, "collections": self.foreign or self._names(colls)}


class _World:
    root = None


def setUpModule():
    _World.root = Path(tempfile.mkdtemp(prefix="mse-mcp-tl-")).resolve()
    (_World.root / "sub").mkdir()
    _World.name = "mt" + uuid.uuid4().hex[:8]
    config.add_collection(str(_World.root), _World.name)
    _World.root2 = Path(tempfile.mkdtemp(prefix="mse-mcp-tl2-")).resolve()
    _World.name2 = "mu" + uuid.uuid4().hex[:8]
    config.add_collection(str(_World.root2), _World.name2)


def tearDownModule():
    config.remove_collection(_World.name)
    config.remove_collection(_World.name2)


def lock():
    return mcp.Lock(str(_World.root / "sub"))


class Arguments(unittest.TestCase):
    def test_default_scope_is_the_collection_of_the_working_directory(self):
        cl = Fake()
        mcp.call_tool("timeline", {}, cl, str(_World.root / "sub"))
        mcp.call_tool("facets", {}, cl, str(_World.root / "sub"))
        self.assertEqual(cl.calls, [("timeline", [_World.name], [], None, None), ("facets", [_World.name], [])])
        cl = Fake()
        mcp.call_tool("timeline", {}, cl, None)                              # no folder, no collection: every collection
        self.assertEqual(cl.calls, [("timeline", None, [], None, None)])

    def test_scope_arguments(self):
        cl = Fake()
        mcp.call_tool("timeline", {"collection": "all"}, cl, str(_World.root))
        mcp.call_tool("timeline", {"collection": f"{_World.name}, {_World.name}"}, cl, None)
        mcp.call_tool("facets", {"collection": _World.name}, cl, None)
        self.assertEqual([c[1] for c in cl.calls], [None, [_World.name], [_World.name]])
        with self.assertRaises(ValueError):
            mcp.call_tool("timeline", {"collection": "no-such-folder"}, cl, None)

    def test_types_from_and_to(self):
        cl = Fake()
        mcp.call_tool("timeline", {"type": ["Receipt", "invoice", "receipt"], "from": "2025-01", "to": "2025-04-30"}, cl, None)
        mcp.call_tool("timeline", {"type": "receipt", "from": "2025", "to": 2026}, cl, None)
        mcp.call_tool("facets", {"type": "quote,letter"}, cl, None)
        mcp.call_tool("timeline", {"type": [], "from": "", "to": None}, cl, None)
        self.assertEqual(cl.calls, [("timeline", None, ["receipt", "invoice"], "2025-01", "2025-04-30"),
                                    ("timeline", None, ["receipt"], "2025", "2026"),
                                    ("facets", None, ["quote", "letter"]),
                                    ("timeline", None, [], None, None)])

    def test_bad_arguments_are_refused_before_the_backend_is_asked(self):
        bad = [("timeline", {"type": ["bill"]}), ("facets", {"type": ["bill"]}), ("timeline", {"type": [3]}), ("timeline", {"type": 3}),
               ("timeline", {"from": "2025-13"}), ("timeline", {"to": "2025-02-30"}), ("timeline", {"from": "March"}),
               ("timeline", {"from": True}), ("timeline", {"from": "2025-07", "to": "2025-06"}), ("timeline", {"from": "2026", "to": "2025"})]
        for name, args in bad:
            cl = Fake()
            with self.assertRaises(ValueError, msg=(name, args)):
                mcp.call_tool(name, args, cl, None)
            self.assertEqual(cl.calls, [], (name, args))
        mcp.call_tool("timeline", {"from": "2025-06", "to": "2025-06"}, Fake(), None)          # one month is a range


class Lock(unittest.TestCase):
    def test_a_locked_session_gets_its_own_collection_and_nothing_wider(self):
        for name in ("timeline", "facets"):
            cl = Fake()
            mcp.call_tool(name, {}, cl, None, lock())
            mcp.call_tool(name, {"collection": _World.name}, cl, None, lock())
            self.assertEqual([c[1] for c in cl.calls], [[_World.name], [_World.name]])
            for collection in ("all", "c2", f"{_World.name},c2", "ghost"):
                with self.assertRaises(PermissionError, msg=(name, collection)):
                    mcp.call_tool(name, {"collection": collection}, cl, None, lock())
            self.assertEqual(len(cl.calls), 2)                                                  # nothing else was asked
        nowhere = mcp.Lock(tempfile.mkdtemp(prefix="mse-mcp-none-"))
        cl = Fake()
        with self.assertRaises(PermissionError):
            mcp.call_tool("timeline", {}, cl, None, nowhere)
        self.assertEqual(cl.calls, [])

    def test_whatever_a_backend_returns_is_confined(self):
        for name in ("timeline", "facets"):
            with self.assertRaises(PermissionError):
                mcp.call_tool(name, {}, Fake(foreign=["c2"]), None, lock())
            with self.assertRaises(PermissionError):
                mcp.call_tool(name, {}, Fake(foreign=[_World.name, "c2"]), None, lock())
            mcp.call_tool(name, {}, Fake(foreign=[_World.name]), None, lock())

    def test_the_collection_parameter_describes_the_lock(self):
        tools = {t["name"]: t for t in mcp.tools_for(lock())}
        for name in ("timeline", "facets"):
            self.assertIn(f"Only '{_World.name}' is allowed", tools[name]["inputSchema"]["properties"]["collection"]["description"])
        self.assertNotIn("Only", mcp.TOOLS[8]["inputSchema"]["properties"]["collection"]["description"])


class Text(unittest.TestCase):
    def test_timeline_text(self):
        text = mcp.call_tool("timeline", {"type": ["receipt"]}, Fake(), None)
        lines = text.split("\n")
        self.assertEqual(lines[0], "Documents per month in c1, c2 (receipt), 2025-01 to 2025-04")
        self.assertIn("3 documents on the timeline; 2 classified documents have no document date and are not placed on it", lines)
        self.assertIn("first month with a document: 2025-01, last: 2025-04", lines)
        self.assertIn("months with no document: 2025-02, 2025-03", lines)
        self.assertIn("2025-01  2 documents  EUR 12.50 + USD 1,234.50", lines)
        self.assertIn("2025-02  0 documents  (gap)", lines)
        self.assertIn("2025-04  1 document  EUR 8.00", lines)
        self.assertTrue(any(l.startswith("hint: list a month's documents with `search`") and 'doctype ["receipt"]' in l for l in lines))

    def test_amounts_found_on_some_documents_only(self):
        tl = {**TL, "buckets": [{**TL["buckets"][0], "with_amount": 1}]}
        text = mcp.format_timeline(tl)
        self.assertIn("EUR 12.50 + USD 1,234.50 (amount found on 1 of 2)", text)

    def test_nothing_on_the_timeline(self):
        text = mcp.format_timeline({"collections": ["c1"], "types": [], "from": None, "to": None, "first": None, "last": None,
                                    "buckets": [], "gaps": [], "total": 0, "undated": 3})
        self.assertIn("Documents per month in c1 (all kinds)", text)
        self.assertIn("0 documents on the timeline; 3 classified documents have no document date", text)
        self.assertIn("months with no document: none", text)
        self.assertIn("no classified document with a document date was found", text)

    def test_long_ranges_are_cut(self):
        months = [f"{2000 + i // 12}-{i % 12 + 1:02d}" for i in range(300)]
        tl = {**TL, "buckets": [{"month": m, "count": 0, "amounts": {}, "with_amount": 0} for m in months], "gaps": months, "total": 0,
              "first": None, "last": None, "from": months[0], "to": months[-1]}
        text = mcp.format_timeline(tl)
        self.assertIn("... and 60 more months", text)
        self.assertIn("... 60 more months; narrow the range with `from` and `to`", text)
        self.assertNotIn(months[-1] + "  0 documents", text)

    def test_facets_text(self):
        text = mcp.call_tool("facets", {}, Fake(), None)
        lines = text.split("\n")
        self.assertEqual(lines[0], "Document facts in c1, c2")
        self.assertEqual(lines[1], "files 12, classified 7, not classified 5, classified without a document date 1")
        self.assertIn("document kinds: invoice 4, receipt 3", lines)
        self.assertIn("top parties: Acme Parts GmbH (3), Cafe Beispiel (2)", lines)
        self.assertIn("months: 2025-03 (4), 2025-04 (2)", lines)
        self.assertIn("currencies: EUR 6", lines)
        empty = mcp.format_facets({"collections": [], "files": 0, "classified": 0, "unclassified": 0, "undated": 0, "doctypes": [],
                                   "counterparties": [], "months": [], "currencies": []})
        self.assertIn("Document facts in nothing", empty)
        self.assertIn("top parties: none", empty)
        many = {**FC, "months": [{"month": f"{2000 + i // 12}-{i % 12 + 1:02d}", "count": 1} for i in range(40)]}
        self.assertIn("... and 4 earlier months", mcp.format_facets(many))


class Http(unittest.TestCase):
    def client(self):
        c = mcp.HttpClient.__new__(mcp.HttpClient)
        c.base, c.token = "http://127.0.0.1:9", "t"
        return c

    def test_requests(self):
        seen = []

        def fake_call(self, method, path, params=None, body=None):
            seen.append((method, path, dict(params or {})))
            return TL if "timeline" in path else FC

        with mock.patch.object(mcp.HttpClient, "_call", fake_call):
            c = self.client()
            mcp.call_tool("timeline", {"collection": f"{_World.name},{_World.name2}", "type": ["receipt", "invoice"], "from": "2025-01",
                                       "to": "2025-06"}, c, None)
            mcp.call_tool("timeline", {}, c, None)
            mcp.call_tool("facets", {"collection": "all", "type": "quote"}, c, None)
            mcp.call_tool("facets", {}, c, str(_World.root))
        self.assertEqual(seen[0], ("GET", "/api/docs/timeline", {"c": f"{_World.name},{_World.name2}", "type": ["receipt", "invoice"], "from": "2025-01", "to": "2025-06"}))
        self.assertEqual(seen[1], ("GET", "/api/docs/timeline", {}))
        self.assertEqual(seen[2], ("GET", "/api/docs/facets", {"type": ["quote"]}))
        self.assertEqual(seen[3], ("GET", "/api/docs/facets", {"c": _World.name}))
        from urllib.parse import parse_qs, urlencode
        self.assertEqual(parse_qs(urlencode(seen[0][2], doseq=True))["type"], ["receipt", "invoice"])     # what the daemon reads


class RealIndex(unittest.TestCase):
    """The same tools over a real tiny index, through LocalClient (the daemon's /mcp route)."""

    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="mse-mcp-tl-real-")).resolve()
        cls.name = "mtr" + uuid.uuid4().hex[:8]
        config.add_collection(str(cls.folder), cls.name)
        conn = store.connect(cls.name)
        for rel, meta in (("r1.pdf", M("receipt", "2025-01-10", 5.0, "EUR", "Cafe Beispiel")),
                          ("r2.pdf", M("receipt", "2025-04-20", 7.5, "EUR", "Cafe Beispiel")),
                          ("r3.pdf", M("receipt", None, 3.0, "EUR", "Cafe Beispiel")),
                          ("i1.pdf", M("invoice", "2025-03-14", 900.0, "EUR", "Acme Parts GmbH")),
                          ("plain.pdf", None)):
            add_doc(conn, cls.folder, cls.name, rel, "text " + rel, meta)
        conn.commit()
        conn.close()
        cls.other = Path(tempfile.mkdtemp(prefix="mse-mcp-tl-other-")).resolve()
        cls.other_name = "mto" + uuid.uuid4().hex[:8]
        config.add_collection(str(cls.other), cls.other_name)

    @classmethod
    def tearDownClass(cls):
        config.remove_collection(cls.name)
        config.remove_collection(cls.other_name)

    def test_timeline(self):
        text = mcp.call_tool("timeline", {"collection": self.name, "type": ["receipt"]}, mcp.LocalClient(), None)
        self.assertIn(f"Documents per month in {self.name} (receipt), 2025-01 to 2025-04", text)
        self.assertIn("2 documents on the timeline; 1 classified documents have no document date", text)
        self.assertIn("months with no document: 2025-02, 2025-03", text)
        self.assertIn("2025-01  1 document  EUR 5.00", text)
        self.assertIn("2025-03  0 documents  (gap)", text)
        # a range: the year is padded with its empty months
        text = mcp.call_tool("timeline", {"collection": self.name, "type": "receipt", "from": "2025-01", "to": "2025-06"}, mcp.LocalClient(), None)
        self.assertIn("months with no document: 2025-02, 2025-03, 2025-05, 2025-06", text)
        # every kind
        text = mcp.call_tool("timeline", {"collection": self.name}, mcp.LocalClient(), None)
        self.assertIn("2025-03  1 document  EUR 900.00", text)

    def test_facets(self):
        text = mcp.call_tool("facets", {"collection": self.name}, mcp.LocalClient(), None)
        self.assertIn("files 5, classified 4, not classified 1, classified without a document date 1", text)
        self.assertIn("document kinds: receipt 3, invoice 1", text)
        self.assertIn("top parties: Cafe Beispiel (3), Acme Parts GmbH (1)", text)
        self.assertIn("currencies: EUR 4", text)

    def test_lock_to_another_collection_sees_nothing_of_this_one(self):
        lock_other = mcp.Lock(str(self.other))
        for name in ("timeline", "facets"):
            text = mcp.call_tool(name, {}, mcp.LocalClient(), None, lock_other)
            self.assertIn(self.other_name, text)
            self.assertNotIn(self.name, text)
            self.assertNotIn("Cafe Beispiel", text)
            with self.assertRaises(PermissionError):
                mcp.call_tool(name, {"collection": self.name}, mcp.LocalClient(), None, lock_other)
        # through the protocol
        r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "facets", "arguments": {"collection": "all"}}},
                       mcp.LocalClient(), None, lock_other)["result"]
        self.assertTrue(r["isError"])
        self.assertIn(f"limited to the '{self.other_name}' collection", r["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
