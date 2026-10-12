"""The HTTP side of document facts: /api/docs/facets, /api/docs/timeline, /api/docs/<docid>/meta, /api/docs/reindex, the
facts in /api/search results, and the docmeta settings. The service is started on a free port (never the real one);
temporary folders and a temporary data folder.
Run: .venv/bin/python -m unittest -v tests.test_docmeta_api"""

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlencode

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import config, docmeta, indexer, server, store  # noqa: E402
from tests.test_docmeta_index import M, add_doc  # noqa: E402


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="mse-docapi-"))
        cls.name = "da" + uuid.uuid4().hex[:8]
        cls.coll = config.add_collection(str(cls.folder), cls.name)
        conn = store.connect(cls.name)
        cls.ids = {}
        for rel, text, meta in (
                ("a.pdf", "invoice alpha", M("invoice", "2025-03-14", 900.0, "EUR", "Acme Parts GmbH", "INV-1")),
                ("b.pdf", "invoice beta", M("invoice", "2025-05-02", 40.0, "EUR", "Muster AG")),
                ("r1.pdf", "receipt one", M("receipt", "2025-01-10", 5.0, "EUR", "Cafe Beispiel")),
                ("r2.pdf", "receipt two", M("receipt", "2025-04-20", 7.0, "EUR", "Cafe Beispiel")),
                ("plain.pdf", "nothing", None)):
            add_doc(conn, cls.folder, cls.name, rel, text, meta)
        for r in conn.execute("SELECT path, docid FROM files"):
            cls.ids[r["path"]] = r["docid"]
        conn.commit()
        conn.close()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.token = config.load()["token"]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        config.remove_collection(cls.name)

    def call(self, method, path, body=None, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            headers = {"X-MSE-Token": self.token} if token else {}
            data = json.dumps(body) if body is not None else None
            conn.request(method, path, data, headers)
            r = conn.getresponse()
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
        finally:
            conn.close()

    def get(self, path, **params):
        return self.call("GET", path + ("?" + urlencode(params) if params else ""))

    # -- facets
    def test_facets(self):
        code, f = self.get("/api/docs/facets", collection=self.name)
        self.assertEqual(code, 200)
        self.assertEqual({d["doctype"]: d["count"] for d in f["doctypes"]}, {"invoice": 2, "receipt": 2})
        self.assertEqual((f["files"], f["classified"], f["unclassified"]), (5, 4, 1))
        self.assertEqual({p["name"]: p["count"] for p in f["counterparties"]}["Cafe Beispiel"], 2)
        self.assertEqual([m["month"] for m in f["months"]], ["2025-01", "2025-03", "2025-04", "2025-05"])
        code, f = self.get("/api/docs/facets", c=self.name, type="receipt")                # c= works too, and type narrows
        self.assertEqual([d["doctype"] for d in f["doctypes"]], ["receipt"])

    def test_facets_of_everything_when_no_collection_is_named(self):
        code, f = self.get("/api/docs/facets")
        self.assertEqual(code, 200)
        self.assertGreaterEqual(f["classified"], 4)

    def test_bad_arguments_are_a_400_with_a_sentence(self):
        for path, params in (("/api/docs/facets", {"collection": "no-such-folder"}), ("/api/docs/facets", {"type": "spaceship"}),
                             ("/api/docs/timeline", {"collection": self.name, "from": "march"}),
                             ("/api/docs/timeline", {"collection": self.name, "from": "2025-13"}),
                             ("/api/docs/timeline", {"collection": self.name, "from": "1800", "to": "2099"})):
            with self.subTest(path=path, params=params):
                code, body = self.get(path, **params)
                self.assertEqual(code, 400)
                self.assertTrue(body["error"])

    # -- timeline
    def test_timeline_with_gaps(self):
        code, t = self.get("/api/docs/timeline", collection=self.name, type="receipt", **{"from": "2025-01", "to": "2025-05"})
        self.assertEqual(code, 200)
        self.assertEqual([(b["month"], b["count"]) for b in t["buckets"]],
                         [("2025-01", 1), ("2025-02", 0), ("2025-03", 0), ("2025-04", 1), ("2025-05", 0)])
        self.assertEqual(t["gaps"], ["2025-02", "2025-03", "2025-05"])
        self.assertEqual(t["buckets"][0]["amounts"], {"EUR": 5.0})
        self.assertEqual((t["from"], t["to"], t["first"], t["last"], t["undated"]), ("2025-01", "2025-05", "2025-01", "2025-04", 0))

    def test_timeline_default_range_is_first_to_last_document(self):
        code, t = self.get("/api/docs/timeline", collection=self.name, type="invoice")
        self.assertEqual((t["from"], t["to"]), ("2025-03", "2025-05"))
        self.assertEqual(t["gaps"], ["2025-04"])
        code, t = self.get("/api/docs/timeline", collection=self.name, type="receipt,invoice")
        self.assertEqual(t["types"], ["receipt", "invoice"])

    # -- one document
    def test_meta_of_one_document(self):
        code, m = self.get(f"/api/docs/{self.ids['a.pdf']}/meta")
        self.assertEqual(code, 200)
        self.assertEqual((m["path"], m["collection"], m["classified"]), ("a.pdf", self.name, True))
        self.assertEqual({k: m["meta"][k] for k in ("doctype", "doc_date", "amount", "currency", "counterparty", "reference")},
                         {"doctype": "invoice", "doc_date": "2025-03-14", "amount": 900.0, "currency": "EUR",
                          "counterparty": "Acme Parts GmbH", "reference": "INV-1"})
        self.assertEqual(m["meta"]["extractor_version"], docmeta.EXTRACTOR_VERSION)
        self.assertIn("confidence", m["meta"])
        self.assertEqual(self.get(f"/api/docs/{self.ids['a.pdf']}/meta", c=self.name)[0], 200)

    def test_meta_of_a_document_without_facts_and_of_none(self):
        code, m = self.get(f"/api/docs/{self.ids['plain.pdf']}/meta")
        self.assertEqual((code, m["classified"], m["meta"]), (200, False, None))
        self.assertEqual(self.get("/api/docs/deadbeef/meta")[0], 404)
        self.assertEqual(self.get(f"/api/docs/{self.ids['a.pdf']}/meta", c="other-folder")[0], 404)

    def test_everything_needs_the_token(self):
        for path in ("/api/docs/facets", "/api/docs/timeline", f"/api/docs/{self.ids['a.pdf']}/meta", "/api/docs/reindex"):
            self.assertEqual(self.call("GET", path, token=False)[0], 401, path)
        self.assertEqual(self.call("POST", "/api/docs/reindex", None, token=False)[0], 401)

    # -- search carries the facts
    def test_search_filters_through_the_api(self):
        q = quote("type:invoice amount:>500 currency:EUR")
        code, res = self.call("GET", f"/api/search?q={q}&c={self.name}&mode=keyword")
        self.assertEqual(code, 200)
        self.assertEqual([Path(r["path"]).name for r in res["results"]], ["a.pdf"])
        self.assertEqual(res["results"][0]["meta"], {"doctype": "invoice", "confidence": 0.9, "doc_date": "2025-03-14", "amount": 900.0,
                                                     "currency": "EUR", "counterparty": "Acme Parts GmbH", "reference": "INV-1"})
        self.assertTrue(res["browse"])
        code, res = self.call("GET", f"/api/search?q={quote('invoices over 20 EUR in 2025')}&c={self.name}&mode=keyword")
        self.assertEqual(sorted(Path(r["path"]).name for r in res["results"]), ["a.pdf", "b.pdf"])
        self.assertEqual(res["filters"]["docdate"], ["2025"])
        code, body = self.call("GET", f"/api/search?q={quote('type:invoice amount:>x')}&c={self.name}&mode=keyword")
        self.assertEqual(code, 400)

    def test_doc_endpoint_carries_meta(self):
        code, d = self.get("/api/doc", c=self.name, ref=self.ids["a.pdf"])
        self.assertEqual((code, d["meta"]["doctype"]), (200, "invoice"))

    # -- reindex
    def test_reindex_runs_in_the_background(self):
        name = "ri" + uuid.uuid4().hex[:8]
        folder = Path(tempfile.mkdtemp(prefix="mse-docapi-"))
        coll = config.add_collection(str(folder), name)
        try:
            (folder / "Rechnung.txt").write_text("Rechnung Nr. RE-7\nRechnungsdatum 14.03.2025\nGesamtbetrag 50,00 EUR\nZahlungsziel 14 Tage")
            indexer.scan(coll)
            conn = store.connect(name)
            conn.execute("UPDATE doc_meta SET amount = -1")
            conn.commit()
            conn.close()
            code, info = self.call("POST", "/api/docs/reindex", {"collection": name})
            self.assertEqual(code, 200)
            self.assertIn(info["state"], ("running", "done"))
            for _ in range(100):
                code, info = self.get("/api/docs/reindex", collection=name)
                if info["state"] != "running":
                    break
                time.sleep(0.1)
            self.assertEqual(info["state"], "done")
            self.assertEqual((info["done"], info["total"]), (1, 1))
            conn = store.connect(name, readonly=True)
            self.assertEqual(conn.execute("SELECT amount FROM doc_meta").fetchone()[0], 50.0)
            conn.close()
            self.assertEqual(self.get("/api/docs/reindex", collection="never-run")[1], {"state": "idle"})
            self.assertEqual(self.call("POST", "/api/docs/reindex", {"collection": "no-such-folder"})[0], 400)
        finally:
            config.remove_collection(name)

    def test_one_reindex_at_a_time_per_collection(self):
        first = docmeta.REINDEX["x-busy"] = {"state": "running", "done": 0, "total": 0}
        try:
            self.assertIs(docmeta.start_reindex("x-busy"), first)         # a running one is returned, none is started
        finally:
            docmeta.REINDEX.pop("x-busy", None)


class Settings(unittest.TestCase):
    def tearDown(self):
        cfg = config.load()
        cfg["docmeta"] = {"enabled": True, "own_names": [], "extra_org_suffixes": [], "extra_keywords": {}}
        config.save(cfg)
        docmeta.reset_settings_cache()

    def test_defaults(self):
        d = config.DEFAULTS["docmeta"]
        self.assertEqual((d["enabled"], d["own_names"], d["extra_org_suffixes"], d["extra_keywords"]), (True, [], [], {}))
        self.assertEqual(config.DEFAULTS["saved_searches"], [])

    def test_settings_reach_the_indexer(self):
        config.update_settings({"docmeta": {"own_names": ["Muster AG", " muster ag ", ""], "extra_org_suffixes": "Ltda, SpA2",
                                            "extra_keywords": {"tax_document": ["Hundesteuer", "hundesteuer"]}}})
        st = docmeta.settings()
        self.assertEqual(st["own_names"], ("Muster AG",))
        self.assertEqual(st["extra_org_suffixes"], ("Ltda", "SpA2"))
        self.assertEqual(st["extra_keywords"], {"tax_document": ["Hundesteuer"]})
        self.assertTrue(st["enabled"])
        config.update_settings({"docmeta": {"enabled": False}})
        self.assertFalse(docmeta.settings()["enabled"])

    def test_validation(self):
        for bad in ({"enabled": "yes"}, {"own_names": [1]}, {"own_names": [f"name {i}" for i in range(51)]}, {"extra_org_suffixes": {"a": 1}},
                    {"extra_keywords": ["a"]}, {"extra_keywords": {"other": ["x"]}}, {"extra_keywords": {"spaceship": ["x"]}},
                    {"extra_keywords": {"invoice": "x"}}, {"extra_keywords": {"invoice": [3]}}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    config.update_settings({"docmeta": bad})

    def test_extra_keywords_are_used_by_the_classifier_of_the_indexer(self):
        folder = Path(tempfile.mkdtemp(prefix="mse-docapi-"))
        coll = {"name": "t" + uuid.uuid4().hex[:10], "path": str(folder), "enabled": True, "media": {}, "exclude": [], "metadata_only": []}
        (folder / "x.txt").write_text("Hundesteuer 2025\nHundemarke Nr. 55\nSteuerzeichen fuer Hundehalter")
        indexer.scan(coll)
        conn = store.connect(coll["name"], readonly=True)
        self.assertEqual(conn.execute("SELECT doctype FROM doc_meta").fetchone()[0], "other")
        conn.close()
        config.update_settings({"docmeta": {"extra_keywords": {"tax_document": ["hundesteuer", "hundemarke", "steuerzeichen", "hundehalter"]}}})
        self.assertGreater(docmeta.reindex(coll["name"])["total"], 0)
        conn = store.connect(coll["name"], readonly=True)
        self.assertEqual(conn.execute("SELECT doctype FROM doc_meta").fetchone()[0], "tax_document")
        conn.close()


if __name__ == "__main__":
    unittest.main()
