"""The MCP tools over HTTP: HttpClient (what `mse mcp` uses) against a real daemon started in this process on a free port with a
temporary data folder, and the daemon's own `POST /mcp` route. The same calls through LocalClient must give the same text. This is
the test that catches a filter the daemon silently drops. Fictional documents only; never the real port or data folder.
Run: .venv/bin/python -m unittest -v tests.test_mcp_http"""

import http.client
import json
import os
import tempfile
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import config, indexer, mcp, server, store  # noqa: E402
from tests.test_docmeta_index import M, add_doc  # noqa: E402


class Daemon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="mse-mcp-http-")).resolve()
        cls.name = "mh" + uuid.uuid4().hex[:8]
        config.add_collection(str(cls.folder), cls.name)
        conn = store.connect(cls.name)
        for rel, meta in (("acme_mar.pdf", M("invoice", "2025-03-14", 900.0, "EUR", "Acme Parts GmbH")),
                          ("muster_apr.pdf", M("invoice", "2025-04-02", 40.0, "EUR", "Muster AG")),
                          ("cafe_apr.pdf", M("receipt", "2025-04-03", 8.0, "EUR", "Cafe Beispiel")),
                          ("quote_jun.pdf", M("quote", "2025-06-30", 1500.5, "USD", "Acme Parts GmbH")),
                          ("cafe_jul.pdf", M("receipt", "2025-07-01", 12.0, "EUR", "Cafe Beispiel"))):
            add_doc(conn, cls.folder, cls.name, rel, "alpha", meta)
        fid = add_doc(conn, cls.folder, cls.name, "report.pdf", "front page", M("letter", "2025-09-30", None, None, "Muster AG"))
        store.add_items(conn, fid, "report.pdf", [{"modality": "text", "text": "second page zebra", "page": 2},
                                                  {"modality": "text", "text": "third page", "page": 3}], start_ord=1)
        conn.execute("UPDATE files SET title='Quarterly report', pages=3 WHERE id=?", (fid,))
        fid = add_doc(conn, cls.folder, cls.name, "call.m4a", "supplier call", None, kind="audio")
        store.add_items(conn, fid, "call.m4a", [{"modality": "transcript", "text": "we agreed on the giraffe price", "t0": 4000.0, "t1": 4010.0}],
                        start_ord=1)
        conn.commit()
        conn.close()
        cls.report_id = indexer.docid(cls.name, "report.pdf")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.env = mock.patch.dict(os.environ, {"MSE_PORT": str(cls.port)})
        cls.env.start()
        cls.http = mcp.HttpClient()
        cls.local = mcp.LocalClient()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        config.remove_collection(cls.name)

    def both(self, tool, args):
        """The same call through the daemon and in-process; the text must be the same."""
        args = {"collection": self.name, **args}
        a = mcp.call_tool(tool, args, self.http, None)
        b = mcp.call_tool(tool, args, self.local, None)
        self.assertEqual(a, b, (tool, args))
        return a

    def files(self, args, query=""):
        text = self.both("search", {"query": query, "mode": "keyword", "precise": False, "limit": 50, **args})
        import re
        return sorted(re.findall(r"\] (\S+\.(?:pdf|m4a))", text))

    def test_the_document_filters_reach_the_daemon(self):
        e = self.assertEqual
        e(self.files({"doctype": ["invoice"]}), ["acme_mar.pdf", "muster_apr.pdf"])
        e(self.files({"doctype": ["invoice", "receipt"], "party": "Cafe"}), ["cafe_apr.pdf", "cafe_jul.pdf"])
        e(self.files({"party": "Acme Parts"}), ["acme_mar.pdf", "quote_jun.pdf"])
        e(self.files({"amount_min": 40, "amount_max": 900}), ["acme_mar.pdf", "muster_apr.pdf"])
        e(self.files({"currency": "usd"}), ["quote_jun.pdf"])
        e(self.files({"docdate_from": "2025-04", "docdate_to": "2025-06"}), ["cafe_apr.pdf", "muster_apr.pdf", "quote_jun.pdf"])
        e(self.files({"doctype": ["receipt"]}, query="alpha"), ["cafe_apr.pdf", "cafe_jul.pdf"])
        e(self.files({"docdate_from": "2025", "docdate_to": "2025", "doctype": ["letter"]}), ["report.pdf"])

    def test_meta_and_cite_lines_over_http(self):
        text = self.both("search", {"query": "zebra", "mode": "keyword", "precise": False})
        self.assertIn("   meta: letter · 2025-09-30 · party Muster AG\n", text)
        self.assertIn("   cite: Quarterly report p.2\n", text)
        text = self.both("search", {"query": "giraffe", "mode": "keyword", "precise": False})
        self.assertIn("   cite: call.m4a @ 1:06:40\n", text)
        self.assertNotIn("meta:", text)                                          # not classified: no facts, no line

    def test_get_with_a_page_over_http(self):
        text = self.both("get", {"ref": "#" + self.report_id, "page": 2})
        self.assertTrue(text.startswith(f"# report.pdf  (#{self.report_id}, {self.name}, pdf"), text)
        self.assertIn(", page 2)\ncite: Quarterly report p.2\n", text)
        self.assertTrue(text.endswith("second page zebra"))
        text = self.both("get", {"ref": "#" + self.report_id, "page": 9})
        self.assertTrue(text.endswith("page 9 has no extracted text (the document has 3 pages)"), text)
        self.assertIn("front page", self.both("get", {"ref": "#" + self.report_id}))
        with self.assertRaises(ValueError):
            mcp.call_tool("get", {"ref": "#" + self.report_id, "page": 0}, self.http, None)

    def test_timeline_and_facets_over_http(self):
        text = self.both("timeline", {"type": ["receipt"], "from": "2025-01", "to": "2025-08"})
        self.assertIn("months with no document: 2025-01, 2025-02, 2025-03, 2025-05, 2025-06, 2025-08", text)
        self.assertIn("2025-04  1 document  EUR 8.00", text)
        text = self.both("facets", {})
        self.assertIn("document kinds: invoice 2, receipt 2, letter 1, quote 1", text)
        self.assertIn("top parties: ", text)
        self.assertIn("Acme Parts GmbH (2)", text)
        with self.assertRaises(RuntimeError) as cm:                              # the daemon's 400 comes back as an error
            mcp.call_tool("timeline", {"collection": self.name, "from": "1800", "to": "2099"}, self.http, None)
        self.assertIn("100 years", str(cm.exception))

    def test_resources_over_http(self):
        saved = config.add_saved("Cafe receipts", "doctype:receipt party:cafe", self.name)
        self.addCleanup(config.remove_saved, saved["id"])
        for client in (self.http, self.local):
            doc = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": mcp.URI_DOC + self.report_id}},
                             client, None)["result"]["contents"][0]["text"]
            self.assertIn("front page", doc)
            text = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": mcp.URI_SAVED + saved["id"]}},
                              client, None)["result"]["contents"][0]["text"]
            self.assertIn("# Saved search: Cafe receipts\nquery: doctype:receipt party:cafe\n", text)
            self.assertIn("2 results for", text)
            self.assertIn("cafe_apr.pdf", text)
        other = Path(tempfile.mkdtemp(prefix="mse-mcp-http2-")).resolve()
        other_name = "mi" + uuid.uuid4().hex[:8]
        config.add_collection(str(other), other_name)
        self.addCleanup(config.remove_collection, other_name)
        lock = mcp.Lock(str(other))
        r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": mcp.URI_DOC + self.report_id}},
                       self.http, None, lock)
        self.assertEqual(r["error"]["code"], -32002)

    def post(self, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request("POST", "/mcp", json.dumps(body), {"X-MSE-Token": config.load()["token"], "Content-Type": "application/json"})
            r = conn.getresponse()
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
        finally:
            conn.close()

    def test_the_daemons_own_mcp_route(self):
        code, r = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual((code, r["result"]["protocolVersion"]), (200, "2024-11-05"))
        self.assertEqual(set(r["result"]["capabilities"]), {"tools", "resources", "prompts"})
        code, r = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(r["result"]["tools"]), 10)
        code, r = self.post({"jsonrpc": "2.0", "id": 3, "method": "prompts/get",
                             "params": {"name": "monthly-summary", "arguments": {"month": "2025-04", "collection": self.name}}})
        self.assertIn("2025-04", r["result"]["messages"][0]["content"]["text"])
        code, r = self.post({"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "mysearch://nope"}})
        self.assertEqual(r["error"]["code"], -32002)
        code, r = self.post({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "search", "arguments": {
            "query": "", "collection": self.name, "doctype": ["quote"], "party": "Acme Parts", "mode": "keyword", "precise": False}}})
        self.assertFalse(r["result"]["isError"])
        self.assertIn("quote_jun.pdf", r["result"]["content"][0]["text"])
        self.assertNotIn("acme_mar.pdf", r["result"]["content"][0]["text"])
        code, r = self.post({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "timeline", "arguments": {
            "collection": self.name, "type": "invoice"}}})
        text = r["result"]["content"][0]["text"]
        self.assertIn("2025-03  1 document  EUR 900.00", text)
        self.assertIn("months with no document: none", text)


if __name__ == "__main__":
    unittest.main()
