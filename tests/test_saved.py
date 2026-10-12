"""Saved searches: create, list, change, remove and run, through the configuration, the HTTP API and the command line. A
saved search is only a query kept under a name: removing one never touches a file or an index. Temporary folders and a
temporary data folder; the HTTP service is started on a free port (never the real one).
Run: .venv/bin/python -m unittest -v tests.test_saved"""

import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import cli, config, paths, search, server, store  # noqa: E402
from tests.test_docmeta_index import M, add_doc  # noqa: E402


class _Base(unittest.TestCase):
    """A real collection (config entry + database in the temporary data folder) with three documents, and clean-up of
    everything this test adds to the shared configuration."""

    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="mse-saved-"))
        cls.name = "sv" + uuid.uuid4().hex[:8]
        config.add_collection(str(cls.folder), cls.name)
        conn = store.connect(cls.name)
        add_doc(conn, cls.folder, cls.name, "a.pdf", "invoice alpha", M("invoice", "2025-03-14", 900.0, "EUR", "Acme Parts GmbH"))
        add_doc(conn, cls.folder, cls.name, "b.pdf", "invoice beta", M("invoice", "2025-04-02", 40.0, "EUR", "Muster AG"))
        add_doc(conn, cls.folder, cls.name, "c.pdf", "receipt gamma", M("receipt", "2025-04-03", 8.0, "EUR", "Cafe Beispiel"))
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        config.remove_collection(cls.name)

    def tearDown(self):
        for s in config.saved_searches():
            config.remove_saved(s["id"])


class SavedInConfig(_Base):
    def test_add_list_get(self):
        s = config.add_saved("Big invoices", "type:invoice amount:>500", self.name)
        self.assertEqual({k: s[k] for k in ("name", "query", "collection")}, {"name": "Big invoices", "query": "type:invoice amount:>500",
                                                                              "collection": self.name})
        self.assertRegex(s["id"], r"^[0-9a-f]{8}$")
        self.assertEqual(config.saved_searches(), [s])
        self.assertEqual(config.get_saved(s["id"]), s)
        self.assertIsNone(config.get_saved("nope"))
        raw = json.loads(paths.CONFIG.read_text())               # persisted in config.json under saved_searches
        self.assertEqual([x["id"] for x in raw["saved_searches"]], [s["id"]])

    def test_collection_is_optional_and_normalised(self):
        s = config.add_saved("Everything", "type:receipt")
        self.assertIsNone(s["collection"])
        s2 = config.add_saved("Two", "type:receipt", f" {self.name.upper()} , {self.name} ")
        self.assertEqual(s2["collection"], self.name)

    def test_validation(self):
        for name, query, coll in (("", "type:invoice", None), ("  ", "type:invoice", None), ("x", "", None), ("x", "   ", None),
                                  ("x", "a" * 2001, None), ("n" * 121, "type:invoice", None), ("x", "amount:>abc", None),
                                  ("x", "docdate:march", None), ("x", "doctype:spaceship", None), ("x", "currency:bitcoins", None),
                                  ("x", "type:invoice", "no-such-folder"), (None, "type:invoice", None), ("x", None, None)):
            with self.subTest(name=name[:10] if name else name, query=(query or "")[:20], coll=coll):
                with self.assertRaises(ValueError):
                    config.add_saved(name, query, coll)
        self.assertEqual(config.saved_searches(), [])

    def test_names_are_unique_ignoring_case(self):
        config.add_saved("Big invoices", "type:invoice")
        with self.assertRaises(ValueError):
            config.add_saved("big INVOICES", "type:receipt")

    def test_update(self):
        s = config.add_saved("Big invoices", "type:invoice amount:>500", self.name)
        time.sleep(0.01)
        u = config.update_saved(s["id"], {"name": "Large invoices", "query": "type:invoice amount:>800"})
        self.assertEqual((u["name"], u["query"], u["collection"], u["id"]), ("Large invoices", "type:invoice amount:>800", self.name, s["id"]))
        self.assertEqual(u["created_at"], s["created_at"])
        self.assertGreater(u["updated_at"], s["updated_at"])
        self.assertIsNone(config.update_saved(s["id"], {"collection": None})["collection"])     # back to "everywhere"
        with self.assertRaises(KeyError):
            config.update_saved("deadbeef", {"name": "x"})
        with self.assertRaises(ValueError):
            config.update_saved(s["id"], {"query": "amount:>abc"})
        with self.assertRaises(ValueError):
            config.update_saved(s["id"], {"id": "other"})
        other = config.add_saved("Other", "type:receipt")
        with self.assertRaises(ValueError):
            config.update_saved(other["id"], {"name": "large invoices"})
        self.assertEqual(config.get_saved(s["id"])["name"], "Large invoices")                  # a refused change changed nothing

    def test_remove_only_removes_the_definition(self):
        s = config.add_saved("Big invoices", "type:invoice amount:>500", self.name)
        files_before = sorted(p.name for p in self.folder.iterdir())
        conn = store.connect(self.name, readonly=True)
        rows_before = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], conn.execute("SELECT COUNT(*) FROM doc_meta").fetchone()[0]
        conn.close()
        self.assertEqual(config.remove_saved(s["id"])["id"], s["id"])
        self.assertEqual(config.saved_searches(), [])
        with self.assertRaises(KeyError):
            config.remove_saved(s["id"])
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), files_before)
        conn = store.connect(self.name, readonly=True)
        self.assertEqual((conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], conn.execute("SELECT COUNT(*) FROM doc_meta").fetchone()[0]),
                         rows_before)
        conn.close()

    def test_at_most_two_hundred(self):
        with mock.patch.object(config, "SAVED_MAX", 2):
            config.add_saved("a", "type:invoice")
            config.add_saved("b", "type:invoice")
            with self.assertRaises(ValueError):
                config.add_saved("c", "type:invoice")

    def test_a_damaged_entry_in_the_file_is_ignored(self):
        cfg = config.load()
        cfg["saved_searches"] = [{"id": "1"}, "junk", {"id": "2", "name": "ok", "query": "type:invoice"}]
        config.save(cfg)
        try:
            self.assertEqual([s["id"] for s in config.saved_searches()], ["2"])
        finally:
            cfg["saved_searches"] = []
            config.save(cfg)

    def test_new_filter_words_are_reserved_names(self):
        for word in ("amount", "currency", "party", "docdate", "doctype"):
            self.assertIn(word, config.FILTER_KEYS)
            with self.assertRaises(ValueError):
                config.add_collection(str(Path(tempfile.mkdtemp(prefix="mse-saved-"))), word)

    def test_run(self):
        s = config.add_saved("Big invoices", "type:invoice amount:>500", self.name)
        res = search.run_saved(s)
        self.assertEqual([Path(r["path"]).name for r in res["results"]], ["a.pdf"])
        self.assertEqual(res["results"][0]["meta"]["amount"], 900.0)
        # a scope shortcut in the query wins over the collection field
        s2 = config.add_saved("Shortcut", f"{self.name}: type:receipt", None)
        self.assertEqual([Path(r["path"]).name for r in search.run_saved(s2)["results"]], ["c.pdf"])
        # a collection that has been removed since: an error, not an empty list
        gone = dict(s, collection="vanished-folder")
        with self.assertRaises(ValueError):
            search.run_saved(gone)


class Http(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)      # a free port; never the service's own
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.token = config.load()["token"]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        super().tearDownClass()

    def call(self, method, path, body=None, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            headers = {"X-MSE-Token": self.token} if token else {}
            data = None
            if body is not None:
                data = json.dumps(body)
                headers["Content-Type"] = "application/json"
            conn.request(method, path, data, headers)
            r = conn.getresponse()
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
        finally:
            conn.close()

    def test_crud(self):
        code, body = self.call("GET", "/api/saved")
        self.assertEqual((code, body), (200, {"saved": []}))
        code, s = self.call("POST", "/api/saved", {"name": "Big invoices", "query": "type:invoice amount:>500", "collection": self.name})
        self.assertEqual(code, 200)
        self.assertEqual((s["name"], s["collection"]), ("Big invoices", self.name))
        sid = s["id"]
        self.assertEqual(self.call("GET", "/api/saved")[1]["saved"][0]["id"], sid)
        self.assertEqual(self.call("GET", f"/api/saved/{sid}"), (200, s))
        code, u = self.call("PATCH", f"/api/saved/{sid}", {"name": "Large invoices"})
        self.assertEqual((code, u["name"], u["query"]), (200, "Large invoices", "type:invoice amount:>500"))
        files_before = sorted(p.name for p in self.folder.iterdir())
        self.assertEqual(self.call("DELETE", f"/api/saved/{sid}"), (200, {"removed": sid}))
        self.assertEqual(self.call("GET", "/api/saved")[1], {"saved": []})
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), files_before)         # only the definition went

    def test_errors_say_what_is_wrong(self):
        code, body = self.call("POST", "/api/saved", {"name": "x", "query": "amount:>abc"})
        self.assertEqual(code, 400)
        self.assertIn("amount", body["error"])
        code, body = self.call("POST", "/api/saved", {"name": "", "query": "type:invoice"})
        self.assertEqual(code, 400)
        code, body = self.call("POST", "/api/saved", {"name": "x", "query": "type:invoice", "collection": "nonesuch"})
        self.assertEqual(code, 400)
        self.assertIn("nonesuch", body["error"])
        for method in ("GET", "PATCH", "DELETE"):
            self.assertEqual(self.call(method, "/api/saved/deadbeef", {} if method == "PATCH" else None)[0], 404)
        s = config.add_saved("A", "type:invoice")
        self.assertEqual(self.call("PATCH", f"/api/saved/{s['id']}", {"query": "docdate:x"})[0], 400)
        self.assertEqual(self.call("PATCH", f"/api/saved/{s['id']}", {"color": "red"})[0], 400)
        self.assertEqual(self.call("POST", f"/api/saved/{s['id']}", {})[0], 405)
        self.assertEqual(self.call("DELETE", f"/api/saved/{s['id']}/run")[0], 405)

    def test_run(self):
        s = config.add_saved("Big invoices", "type:invoice amount:>500", self.name)
        code, res = self.call("GET", f"/api/saved/{s['id']}/run?limit=5")
        self.assertEqual(code, 200)
        self.assertEqual([Path(r["path"]).name for r in res["results"]], ["a.pdf"])
        self.assertEqual(res["saved"]["id"], s["id"])
        self.assertEqual(res["filters"], {"type": ["invoice"], "amount": [">500"]})

    def test_all_of_it_needs_the_token(self):
        for method, path in (("GET", "/api/saved"), ("POST", "/api/saved"), ("PATCH", "/api/saved/deadbeef"),
                             ("DELETE", "/api/saved/deadbeef"), ("GET", "/api/saved/deadbeef/run")):
            self.assertEqual(self.call(method, path, None, token=False)[0], 401, (method, path))

    def test_a_foreign_origin_is_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("DELETE", "/api/saved/deadbeef", headers={"X-MSE-Token": self.token, "Origin": "http://evil.example"})
        r = conn.getresponse()
        r.read()
        self.assertEqual(r.status, 401)
        conn.close()


class CommandLine(_Base):
    def run_cli(self, *argv):
        out = io.StringIO()
        with mock.patch.object(cli, "daemon_up", lambda: False), contextlib.redirect_stdout(out):
            cli.main(list(argv))
        return out.getvalue()

    def test_list_add_run_rm(self):
        self.assertIn("no saved searches yet", self.run_cli("saved", "list"))
        out = self.run_cli("saved", "add", "Big invoices", "type:invoice amount:>500", "-c", self.name)
        self.assertIn("Big invoices", out)
        s = config.saved_searches()[0]
        listing = self.run_cli("saved", "list")
        self.assertIn(s["id"], listing)
        self.assertIn("type:invoice amount:>500", listing)
        self.assertEqual(json.loads(self.run_cli("saved", "list", "--json"))[0]["id"], s["id"])
        res = json.loads(self.run_cli("saved", "run", s["id"], "--json", "--fast"))
        self.assertEqual([Path(r["path"]).name for r in res["results"]], ["a.pdf"])
        self.assertEqual(self.run_cli("saved", "run", "big INVOICES", "--files", "--fast").strip(), str(self.folder / "a.pdf"))   # by name
        out = self.run_cli("saved", "rm", "Big invoices")
        self.assertIn("no file or index was touched", out)
        self.assertEqual(config.saved_searches(), [])
        self.assertTrue((self.folder / "a.pdf").exists())

    def test_errors_exit_with_a_sentence(self):
        with self.assertRaises(SystemExit) as e:
            self.run_cli("saved", "rm", "nothing-by-that-name")
        self.assertIn("no saved search", str(e.exception))
        with self.assertRaises(SystemExit) as e:
            self.run_cli("saved", "add", "x", "amount:>abc")
        self.assertIn("amount", str(e.exception))

    def test_docs_commands(self):
        facets = json.loads(self.run_cli("docs", "facets", "-c", self.name, "--json"))
        self.assertEqual({d["doctype"]: d["count"] for d in facets["doctypes"]}, {"invoice": 2, "receipt": 1})
        self.assertIn("invoice", self.run_cli("docs", "facets", "-c", self.name))
        tl = json.loads(self.run_cli("docs", "timeline", "-c", self.name, "--type", "invoice", "--from", "2025-02", "--to", "2025-05", "--json"))
        self.assertEqual(tl["gaps"], ["2025-02", "2025-05"])
        text = self.run_cli("docs", "timeline", "-c", self.name, "--type", "invoice", "--from", "2025-02", "--to", "2025-05")
        self.assertIn("months without: 2025-02, 2025-05", text)
        meta = json.loads(self.run_cli("docs", "meta", "a.pdf", "-c", self.name))
        self.assertEqual((meta["classified"], meta["meta"]["amount"]), (True, 900.0))

    def test_docs_reindex_redoes_the_facts_of_a_collection(self):
        from mse import indexer
        folder = Path(tempfile.mkdtemp(prefix="mse-saved-"))
        name = "ri" + uuid.uuid4().hex[:8]
        coll = config.add_collection(str(folder), name)
        try:
            (folder / "Rechnung_RE-1.txt").write_text("Rechnung Nr. RE-1\nRechnungsdatum 14.03.2025\nGesamtbetrag 119,00 EUR\nZahlungsziel 14 Tage")
            indexer.scan(coll)
            conn = store.connect(name)
            conn.execute("UPDATE doc_meta SET amount = -1")
            conn.commit()
            conn.close()
            self.assertIn("done", self.run_cli("docs", "reindex", name))
            conn = store.connect(name, readonly=True)
            self.assertEqual(conn.execute("SELECT amount FROM doc_meta").fetchone()[0], 119.0)
            conn.close()
            with self.assertRaises(SystemExit):
                self.run_cli("docs", "reindex", "no-such-collection")
        finally:
            config.remove_collection(name)


if __name__ == "__main__":
    unittest.main()
