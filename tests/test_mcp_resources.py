"""MCP protocol surface beyond the tools: version negotiation and capabilities, tool annotations, resources (the collection
list, saved searches, documents by #docid), resource templates, prompts, and the error codes. Everything runs through
mcp.handle, with a fake client and, for the last class, a real tiny index through LocalClient. A locked session sees only
its own collection's saved searches and documents, and a refused resource answers exactly like a missing one.
Run: .venv/bin/python -m unittest -v tests.test_mcp_resources"""

import json
import os
import re
import tempfile
import unittest
import uuid
from pathlib import Path

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import config, indexer, mcp, store  # noqa: E402
from tests.test_docmeta_index import M, add_doc  # noqa: E402

TAG = uuid.uuid4().hex[:8]
A, B = f"mra-{TAG}", f"mrb-{TAG}"
G_A, G_AB = f"mrga-{TAG}", f"mrgab-{TAG}"
DOCID_A, DOCID_B = "aaaa1111", "bbbb2222"
S = {}                                   # saved searches by role


class Fake:
    """Records calls. Documents live in a collection each; searches answer with no results."""

    def __init__(self):
        self.calls = []

    def search(self, q, colls, limit, filters, mode, precise=False):
        self.calls.append(("search", q, colls))
        return {"query": q, "collections": colls or [], "results": [], "bridge": {}}

    def get(self, coll, ref, max_chars, page):
        self.calls.append(("get", coll, ref))
        for docid, home in ((DOCID_A, A), (DOCID_B, B)):
            if ref == "#" + docid and coll in (None, home):
                return {"path": f"{home}.pdf", "docid": docid, "collection": home, "kind": "pdf", "text": f"text of {home}",
                        "title": f"Title {home}", "pages": 1}
        return None

    def collections(self):
        return [{"name": n, "path": f"/x/{n}", "stats": {"files": 1}} for n in (A, B)]

    def groups(self):
        return [{"name": G_AB, "label": "Both", "collections": [A, B], "files": 2}]


def setUpModule():
    roots = {n: Path(tempfile.mkdtemp(prefix="mse-mcp-res-")).resolve() for n in (A, B)}
    (roots[A] / "sub").mkdir()
    for n, r in roots.items():
        config.add_collection(str(r), n)
    config.save_group("A only", [A], name=G_A)
    config.save_group("A and B", [A, B], name=G_AB)
    for role, (name, query, coll) in {
            "a": ("Search in A", "invoice", A),
            "b": ("Search in B", "invoice", B),
            "all": ("Search everywhere", "invoice", None),
            "short_a": ("Shortcut to A", f"{A}: invoice", None),
            "short_b_coll_a": ("Shortcut beats field", f"{B}: invoice", A),
            "group_a": ("Group of A", "invoice", G_A),
            "group_ab": ("Group of A and B", "invoice", G_AB),
            "folder_out": ("Folder outside", "invoice in:/etc", A),
            "filtered": ("Invoices over 500", "doctype:invoice amount:>500", A)}.items():
        S[role] = config.add_saved(name, query, coll)
    setUpModule.roots = roots


def tearDownModule():
    for s in S.values():
        config.remove_saved(s["id"])
    for g in (G_A, G_AB):
        config.remove_group(g)
    for n in (A, B):
        config.remove_collection(n)


def lock_a():
    return mcp.Lock(str(setUpModule.roots[A] / "sub"))


def rpc(method, params=None, client=None, lock=None, cwd=None):
    req = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        req["params"] = params
    return mcp.handle(req, client or Fake(), lock.cwd if lock else cwd, lock)


def result(method, params=None, **kw):
    r = rpc(method, params, **kw)
    assert "error" not in r, r
    return r["result"]


def error(method, params=None, **kw):
    r = rpc(method, params, **kw)
    assert "result" not in r, r
    return r["error"]


class Protocol(unittest.TestCase):
    def test_a_known_version_is_echoed_and_anything_else_gets_ours(self):
        for v in mcp.SUPPORTED_PROTOCOLS:
            self.assertEqual(result("initialize", {"protocolVersion": v})["protocolVersion"], v)
        for v in ("2099-01-01", "2025-06-19", "", None, 7, ["2025-06-18"], {"a": 1}):
            self.assertEqual(result("initialize", {"protocolVersion": v})["protocolVersion"], mcp.PROTOCOL, v)
        self.assertEqual(result("initialize")["protocolVersion"], mcp.PROTOCOL)
        self.assertEqual(mcp.PROTOCOL, "2025-06-18")
        self.assertEqual(mcp.SUPPORTED_PROTOCOLS, ("2025-06-18", "2025-03-26", "2024-11-05"))

    def test_capabilities_claim_only_what_exists(self):
        init = result("initialize", {"protocolVersion": "2025-03-26"})
        self.assertEqual(init["capabilities"], {"tools": {"listChanged": False},
                                                "resources": {"subscribe": False, "listChanged": False},
                                                "prompts": {"listChanged": False}})
        self.assertEqual(init["serverInfo"]["name"], "mysearch")
        self.assertEqual(error("resources/subscribe", {"uri": mcp.URI_COLLECTIONS})["code"], -32601)
        self.assertEqual(error("completion/complete", {})["code"], -32601)
        self.assertEqual(error("nope/nope")["code"], -32601)
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, Fake(), None))

    def test_tool_annotations(self):
        tools = {t["name"]: t for t in result("tools/list")["tools"]}
        self.assertEqual(set(tools), {"search", "get", "multi_get", "similar", "collections", "mark_best", "sync", "ask", "timeline", "facets"})
        for name in ("search", "get", "multi_get", "similar", "collections", "ask", "timeline", "facets"):
            self.assertIs(tools[name]["annotations"]["readOnlyHint"], True, name)
        for name in ("mark_best", "sync"):
            self.assertIs(tools[name]["annotations"]["readOnlyHint"], False, name)
            self.assertIs(tools[name]["annotations"]["destructiveHint"], False, name)
        locked = {t["name"]: t for t in result("tools/list", lock=lock_a())["tools"]}
        self.assertEqual({n: t["annotations"] for n, t in locked.items()}, {n: t["annotations"] for n, t in tools.items()})


class Resources(unittest.TestCase):
    def uris(self, **kw):
        return {r["uri"]: r for r in result("resources/list", **kw)["resources"]}

    def test_list_unlocked(self):
        got = self.uris()
        self.assertEqual(got[mcp.URI_COLLECTIONS]["mimeType"], "text/plain")
        for role, s in S.items():
            r = got[mcp.URI_SAVED + s["id"]]
            self.assertEqual(r["name"], "saved-" + s["id"])
            self.assertEqual(r["title"], s["name"])
            self.assertEqual(r["mimeType"], "text/plain")
            self.assertTrue(r["description"].startswith("Saved search: "))
        for r in got.values():
            self.assertTrue({"uri", "name", "title", "description", "mimeType"} <= set(r))

    def test_list_locked_hides_every_saved_search_that_is_not_only_this_collection(self):
        got = self.uris(lock=lock_a())
        shown = {role for role, s in S.items() if mcp.URI_SAVED + s["id"] in got}
        self.assertEqual(shown, {"a", "short_a", "group_a", "filtered"})
        self.assertIn(mcp.URI_COLLECTIONS, got)
        # a lock whose folder is in no collection: nothing is allowed, so only the (explaining) collections resource is listed
        nowhere = mcp.Lock(tempfile.mkdtemp(prefix="mse-mcp-none-"))
        self.assertEqual(set(self.uris(lock=nowhere)), {mcp.URI_COLLECTIONS})

    def test_templates(self):
        t = result("resources/templates/list")["resourceTemplates"]
        self.assertEqual([x["uriTemplate"] for x in t], ["mysearch://saved/{id}", "mysearch://doc/{docid}"])
        for x in t:
            self.assertTrue(all(x.get(k) for k in ("name", "title", "description")))
            self.assertEqual(x["mimeType"], "text/plain")

    def read(self, uri, **kw):
        return result("resources/read", {"uri": uri}, **kw)["contents"]

    def test_read_collections_is_the_tool_text(self):
        for lock in (None, lock_a()):
            cl = Fake()
            tool = mcp.call_tool("collections", {}, cl, lock.cwd if lock else None, lock)
            contents = self.read(mcp.URI_COLLECTIONS, client=cl, lock=lock)
            self.assertEqual(contents, [{"uri": mcp.URI_COLLECTIONS, "mimeType": "text/plain", "text": tool}])
        locked = self.read(mcp.URI_COLLECTIONS, lock=lock_a())[0]["text"]
        self.assertIn(A, locked)
        self.assertNotIn(B, locked)
        nowhere = mcp.Lock(tempfile.mkdtemp(prefix="mse-mcp-none-"))
        e = error("resources/read", {"uri": mcp.URI_COLLECTIONS}, lock=nowhere)
        self.assertEqual(e["code"], -32002)
        self.assertIn("is not inside any indexed MySearch collection", e["message"])

    def test_read_saved_runs_the_query_through_the_search_tool(self):
        cl = Fake()
        c = self.read(mcp.URI_SAVED + S["a"]["id"], client=cl)[0]
        self.assertEqual(c["uri"], mcp.URI_SAVED + S["a"]["id"])
        self.assertTrue(c["text"].startswith(f"# Saved search: Search in A\nquery: invoice\ncollection: {A}\n\n0 results"))
        self.assertEqual(cl.calls, [("search", "invoice", [A])])
        cl = Fake()
        self.read(mcp.URI_SAVED + S["all"]["id"], client=cl)
        self.assertEqual(cl.calls, [("search", "invoice", None)])                  # no scope saved: every collection, as in the app
        cl = Fake()
        text = self.read(mcp.URI_SAVED + S["all"]["id"], client=cl)[0]["text"]
        self.assertIn("collection: (none: every collection)", text)
        cl = Fake()
        self.read(mcp.URI_SAVED + S["short_b_coll_a"]["id"], client=cl)
        self.assertEqual(cl.calls, [("search", "invoice", [B])])                   # the shortcut wins over the field
        cl = Fake()
        self.read(mcp.URI_SAVED + S["group_ab"]["id"], client=cl)
        self.assertEqual(cl.calls, [("search", "invoice", [G_AB])])
        cl = Fake()
        self.read(mcp.URI_SAVED + S["filtered"]["id"], client=cl)
        self.assertEqual(cl.calls, [("search", "doctype:invoice amount:>500", [A])])

    def test_locked_read_of_a_saved_search_outside_the_lock_is_a_missing_resource(self):
        lock = lock_a()
        missing = error("resources/read", {"uri": mcp.URI_SAVED + "00000000"}, lock=lock)
        self.assertEqual(missing["code"], -32002)
        for role in ("b", "all", "short_b_coll_a", "group_ab", "folder_out"):
            cl = Fake()
            uri = mcp.URI_SAVED + S[role]["id"]
            e = error("resources/read", {"uri": uri}, client=cl, lock=lock)
            self.assertEqual(e, {"code": -32002, "message": f"resource not found: {uri}"}, role)
            self.assertEqual(e["message"], missing["message"].replace("00000000", S[role]["id"]))      # exactly like a missing one
            self.assertEqual(cl.calls, [], role)                                                            # nothing reached the backend
        for role in ("a", "short_a", "group_a", "filtered"):
            cl = Fake()
            self.assertIn("# Saved search:", self.read(mcp.URI_SAVED + S[role]["id"], client=cl, lock=lock)[0]["text"])
            self.assertEqual([c[2] for c in cl.calls], [[A]], role)
        # the same saved searches are readable when nothing is locked
        for role in ("b", "all", "group_ab", "short_b_coll_a"):
            self.assertIn("# Saved search:", self.read(mcp.URI_SAVED + S[role]["id"])[0]["text"], role)

    def test_a_saved_search_whose_collection_is_gone_still_reads_and_a_lock_does_not_list_it(self):
        name = "mrx" + uuid.uuid4().hex[:8]
        config.add_collection(tempfile.mkdtemp(prefix="mse-mcp-gone-"), name)
        saved = config.add_saved("Will break", "invoice", name)
        self.addCleanup(config.remove_saved, saved["id"])
        config.remove_collection(name)
        uri = mcp.URI_SAVED + saved["id"]
        self.assertIn(uri, self.uris())
        text = self.read(uri)[0]["text"]
        self.assertTrue(text.startswith(f"# Saved search: Will break\nquery: invoice\ncollection: {name}\n\n(this saved search could not be run: "), text)
        self.assertNotIn(uri, self.uris(lock=lock_a()))
        self.assertEqual(error("resources/read", {"uri": uri}, lock=lock_a())["code"], -32002)

    def test_read_doc(self):
        cl = Fake()
        c = self.read(mcp.URI_DOC + DOCID_A, client=cl)[0]
        self.assertEqual(c["mimeType"], "text/plain")
        self.assertEqual(c["text"], mcp.format_doc(cl.get(None, "#" + DOCID_A, 20000, None)))
        self.assertEqual(cl.calls[0], ("get", None, "#" + DOCID_A))                  # unlocked: every collection; never a path
        self.assertIn(f"text of {B}", self.read(mcp.URI_DOC + DOCID_B)[0]["text"])

    def test_locked_read_of_a_document_outside_the_lock_is_a_missing_resource(self):
        lock = lock_a()
        cl = Fake()
        self.assertIn(f"text of {A}", self.read(mcp.URI_DOC + DOCID_A, client=cl, lock=lock)[0]["text"])
        self.assertEqual(cl.calls[0], ("get", A, "#" + DOCID_A))
        for docid in (DOCID_B, "cccc3333"):                                            # another collection's, and none at all
            e = error("resources/read", {"uri": mcp.URI_DOC + docid}, lock=lock)
            self.assertEqual(e, {"code": -32002, "message": f"resource not found: {mcp.URI_DOC + docid}"})

    def test_invalid_docids_and_unknown_uris(self):
        for tail in ("ABCD1234", "abcd123", "abcd12345", "abcd123g", "../etc/passwd", "abcd1234/x", "abcd1234?x=1", "#abcd1234", "",
                     "abcd1234 ", "%61bcd1234", "/etc/passwd"):
            cl = Fake()
            e = error("resources/read", {"uri": mcp.URI_DOC + tail}, client=cl)
            self.assertEqual(e["code"], -32602, tail)
            self.assertIn("invalid docid", e["message"])
            self.assertEqual(cl.calls, [], tail)
        for uri in ("mysearch://nope", "mysearch://collections/", "mysearch://collections?x", "mysearch://saved/", "mysearch://saved/..",
                    "mysearch://saved/" + "a" * 65, "mysearch://doc", "MYSEARCH://collections", "file:///etc/passwd", "http://x/y",
                    "/etc/passwd", "collections"):
            e = error("resources/read", {"uri": uri})
            self.assertEqual(e, {"code": -32002, "message": f"resource not found: {uri}"}, uri)
        for params in (None, {}, {"uri": ""}, {"uri": None}, {"uri": 5}, {"uri": ["mysearch://collections"]}):
            self.assertEqual(error("resources/read", params)["code"], -32602, params)


class Prompts(unittest.TestCase):
    def get(self, name, arguments=None, **kw):
        p = {"name": name}
        if arguments is not None:
            p["arguments"] = arguments
        return result("prompts/get", p, **kw)

    def test_list(self):
        prompts = result("prompts/list")["prompts"]
        self.assertEqual([p["name"] for p in prompts], ["find-document", "monthly-summary", "missing-receipts"])
        args = {p["name"]: {a["name"]: a["required"] for a in p["arguments"]} for p in prompts}
        self.assertEqual(args["find-document"], {"description": True, "type": False, "date": False})
        self.assertEqual(args["monthly-summary"], {"collection": False, "month": True})
        self.assertEqual(args["missing-receipts"], {"collection": False, "year": True})
        for p in prompts:
            self.assertTrue(p["title"] and p["description"])
            for a in p["arguments"]:
                self.assertTrue(a["description"])

    def message(self, got):
        self.assertEqual(set(got), {"description", "messages"})
        (m,) = got["messages"]
        self.assertEqual((m["role"], m["content"]["type"]), ("user", "text"))
        return m["content"]["text"]

    def test_find_document(self):
        text = self.message(self.get("find-document", {"description": "the March invoice from the roofing company"}))
        self.assertIn("Description: the March invoice from the roofing company", text)
        for tool in ("`search`", "`get`", "`cite:`"):
            self.assertIn(tool, text)
        self.assertNotIn("`doctype`", text)
        self.assertNotIn("docdate_from", text)
        text = self.message(self.get("find-document", {"description": "x", "type": "Receipt", "date": "2025-03"}))
        self.assertIn('`doctype` ["receipt"]', text)
        self.assertIn('`docdate_from` and `docdate_to` "2025-03"', text)

    def test_monthly_summary(self):
        text = self.message(self.get("monthly-summary", {"month": "2025-03"}))
        self.assertIn('`timeline` with `from` and `to` set to "2025-03"', text)
        self.assertIn("`facets`", text)
        self.assertIn('`docdate_from` and `docdate_to` set to "2025-03"', text)
        self.assertLess(text.index("`timeline`"), text.index("`search`"))
        self.assertLess(text.index("`search`"), text.index("`facets`"))
        self.assertNotIn("`collection`", text)                                              # none given, none named
        text = self.message(self.get("monthly-summary", {"month": "2025-03", "collection": "work,notes"}))
        self.assertIn('and `collection` "work,notes"', text)
        self.assertIn('`facets` with `collection` "work,notes"', text)

    def test_missing_receipts(self):
        text = self.message(self.get("missing-receipts", {"year": 2025, "collection": "work"}))                # a number is accepted
        self.assertIn('`timeline` with `type` ["receipt"], `from` "2025-01" and `to` "2025-12" and `collection` "work"', text)
        self.assertIn("months with no document:", text)
        self.assertIn("undated", text)

    def test_texts_are_generic(self):
        for name, args in (("find-document", {"description": "d"}), ("monthly-summary", {"month": "2025-01"}),
                           ("missing-receipts", {"year": "2025"})):
            text = self.message(self.get(name, args))
            self.assertIsNone(re.search(r"@|/Users|https?://|\bIBAN\b", text), text)

    def test_arguments_are_validated(self):
        bad = [("nope", {}), (None, {}), (5, {}), ("find-document", {}), ("find-document", {"description": "   "}),
               ("find-document", {"description": "x", "type": "bill"}), ("find-document", {"description": "x", "type": "invoice,receipt"}),
               ("find-document", {"description": "x", "date": "2025-13"}), ("find-document", {"description": "x", "date": "last week"}),
               ("find-document", {"description": "x", "extra": "1"}), ("find-document", {"description": ["x"]}),
               ("find-document", {"description": True}), ("find-document", "oops"),
               ("monthly-summary", {}), ("monthly-summary", {"month": "2025"}), ("monthly-summary", {"month": "2025-13"}),
               ("monthly-summary", {"month": "March 2025"}), ("monthly-summary", {"month": "2025-03-14"}),
               ("monthly-summary", {"month": "2025-03", "collection": 'a" b'}), ("monthly-summary", {"month": "2025-03", "collection": "x\ny"}),
               ("missing-receipts", {}), ("missing-receipts", {"year": "25"}), ("missing-receipts", {"year": "2025-01"}),
               ("missing-receipts", {"year": "20x5"}), ("missing-receipts", {"year": "2025", "month": "01"})]
        for name, args in bad:
            e = error("prompts/get", {"name": name, "arguments": args})
            self.assertEqual(e["code"], -32602, (name, args))
        self.assertEqual(error("prompts/get", {})["code"], -32602)
        self.assertEqual(error("prompts/get")["code"], -32602)
        self.assertIn("missing required argument: month", error("prompts/get", {"name": "monthly-summary", "arguments": {}})["message"])
        self.assertIn("monthly-summary", error("prompts/get", {"name": "nope"})["message"])           # names what exists

    def test_a_locked_session_may_only_name_its_own_collection(self):
        lock = lock_a()
        for name, args in (("monthly-summary", {"month": "2025-03"}), ("missing-receipts", {"year": "2025"})):
            ok = self.message(self.get(name, {**args, "collection": A}, lock=lock))
            self.assertIn(f'"{A}"', ok)
            self.assertIn(f'"{G_A}"', self.message(self.get(name, {**args, "collection": G_A}, lock=lock)))     # a group of only A is fine
            self.get(name, args, lock=lock)                                                              # none given: the default scope
            for other in (B, "all", f"{A},{B}", G_AB):
                e = error("prompts/get", {"name": name, "arguments": {**args, "collection": other}}, lock=lock)
                self.assertEqual(e["code"], -32602, other)
                self.assertIn(f"limited to the '{A}' collection", e["message"])
                self.assertIn(f"use '{A}'", e["message"])                                                # says what is allowed
        nowhere = mcp.Lock(tempfile.mkdtemp(prefix="mse-mcp-none-"))
        e = error("prompts/get", {"name": "monthly-summary", "arguments": {"month": "2025-03"}}, lock=nowhere)
        self.assertEqual(e["code"], -32602)
        self.assertIn("is not inside any indexed MySearch collection", e["message"])
        # find-document names no collection, so the lock has nothing to refuse
        self.get("find-document", {"description": "x"}, lock=lock)
        self.assertEqual(len(result("prompts/list", lock=lock)["prompts"]), 3)


class RealIndex(unittest.TestCase):
    """The same methods through LocalClient, which is what POST /mcp uses."""

    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="mse-mcp-real-")).resolve()
        cls.name = "mrr" + uuid.uuid4().hex[:8]
        config.add_collection(str(cls.folder), cls.name)
        conn = store.connect(cls.name)
        add_doc(conn, cls.folder, cls.name, "inv.pdf", "invoice alpha", M("invoice", "2025-03-14", 900.0, "EUR", "Acme Parts GmbH"))
        conn.commit()
        conn.close()
        cls.docid = indexer.docid(cls.name, "inv.pdf")
        cls.saved = config.add_saved("Real saved", "doctype:invoice", cls.name)
        cls.other = Path(tempfile.mkdtemp(prefix="mse-mcp-real2-")).resolve()
        cls.other_name = "mrs" + uuid.uuid4().hex[:8]
        config.add_collection(str(cls.other), cls.other_name)

    @classmethod
    def tearDownClass(cls):
        config.remove_saved(cls.saved["id"])
        config.remove_collection(cls.name)
        config.remove_collection(cls.other_name)

    def test_resources_and_prompts_work_with_the_local_client(self):
        cl = mcp.LocalClient()
        listed = {r["uri"] for r in result("resources/list", client=cl)["resources"]}
        self.assertIn(mcp.URI_SAVED + self.saved["id"], listed)
        text = result("resources/read", {"uri": mcp.URI_SAVED + self.saved["id"]}, client=cl)["contents"][0]["text"]
        self.assertIn("1 results for", text)
        self.assertIn("inv.pdf", text)
        self.assertIn("meta: invoice · 2025-03-14 · 900.00 EUR · party Acme Parts GmbH", text)
        doc = result("resources/read", {"uri": mcp.URI_DOC + self.docid}, client=cl)["contents"][0]["text"]
        self.assertTrue(doc.startswith(f"# inv.pdf  (#{self.docid}, {self.name}, pdf"))
        self.assertIn("invoice alpha", doc)
        self.assertEqual(error("resources/read", {"uri": mcp.URI_DOC + "0badf00d"}, client=cl)["code"], -32002)
        # locked to the other collection: this document and saved search do not exist for that session
        lock = mcp.Lock(str(self.other))
        self.assertEqual(error("resources/read", {"uri": mcp.URI_DOC + self.docid}, client=cl, lock=lock)["code"], -32002)
        self.assertEqual(error("resources/read", {"uri": mcp.URI_SAVED + self.saved["id"]}, client=cl, lock=lock)["code"], -32002)
        self.assertNotIn(mcp.URI_SAVED + self.saved["id"], {r["uri"] for r in result("resources/list", client=cl, lock=lock)["resources"]})
        self.assertEqual(len(result("prompts/list", client=cl, lock=lock)["prompts"]), 3)
        json.dumps(rpc("prompts/get", {"name": "missing-receipts", "arguments": {"year": "2025"}}, client=cl))    # serialisable


if __name__ == "__main__":
    unittest.main()
