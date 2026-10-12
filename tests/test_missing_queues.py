"""Missing files outside the search path. A file marked missing (missing_since) is not work: the OCR and media queue, the
embedding queue and the pending counts skip it, and its pending work resumes when it is back. `forget-missing` removes
only the rows marked missing in a scope, with their text, vectors and the favourites of those files, and only what a
listing showed. The route takes the token like the rest of the API. No model is loaded: the embedder and the readers
are replaced by fakes, and every folder is temporary.
Run: .venv/bin/python -m unittest -v tests.test_missing_queues"""

import argparse
import contextlib
import http.client
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import types
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import numpy as np
import sqlite_vec

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import cli, config, feedback, indexer, server, store  # noqa: E402
from mse import progress as liveprog  # noqa: E402

DAY = 24 * 3600
GUARD = (0.30, 50)          # the defaults: 30 % of the rows and at least 50 files


def _name() -> str:
    return "q" + uuid.uuid4().hex[:10]


def _folder() -> Path:
    return Path(tempfile.mkdtemp(prefix="mse-q-"))


def _add(conn, folder: Path, rel: str, *, age=None, heavy=None, text=None, kind="pdf") -> int:
    """One file row. age: seconds since it was marked missing (None = present, and the file is written to the folder).
    text: one unembedded text item for the row."""
    now = time.time()
    if age is None:
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("present")
    fid = conn.execute(
        "INSERT INTO files(path, name, kind, ext, size, mtime, mode, state, heavy, missing_since)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rel, rel.rsplit("/", 1)[-1], kind, kind, 1, now, "full", "done", heavy,
         None if age is None else now - age)).lastrowid
    if text is not None:
        store.add_items(conn, fid, rel, [{"modality": "text", "text": text}])
    conn.commit()
    return fid


def _rows(conn) -> list[str]:
    return sorted(r[0] for r in conn.execute("SELECT path FROM files"))


class FakePool:
    """Stands in for embedder.pool(): 768-dimensional vectors, no model."""

    def __init__(self):
        self.workers = {"text": types.SimpleNamespace(batch_priority=False),
                        "full": types.SimpleNamespace(batch_priority=False)}

    def embed_texts(self, prompts, batch=32):
        v = np.zeros((len(prompts), store.DIMS), dtype=np.float32)
        v[:, 0] = 1.0
        return v

    def embed_media(self, items):
        raise AssertionError("no media in these tests")


class QueuesSkipMissing(unittest.TestCase):
    def setUp(self):
        self.name = _name()
        self.folder = _folder()
        self.coll = {"name": self.name, "path": str(self.folder), "media": {}}
        self.conn = store.connect(self.name)

    def tearDown(self):
        self.conn.close()

    def test_the_heavy_queue_skips_missing_rows_and_resumes_when_they_are_back(self):
        c = self.conn
        present = [_add(c, self.folder, f"live/p{i}.pdf", heavy='{"ocr": [1]}') for i in range(2)]
        missing = [_add(c, self.folder, f"gone/m{i}.pdf", age=60, heavy='{"ocr": [1]}') for i in range(3)]
        seen = []

        def fake_heavy(path, kind, spec, name, fid, media):
            seen.append(Path(path).name)
            return {"items": [], "thumb": False, "duration": None}

        with mock.patch.object(indexer.extract, "heavy", side_effect=fake_heavy):
            # the pending ids leave the missing rows out, and the run reads only the two present rows
            self.assertEqual(sorted(indexer.heavy_pending_ids(self.name, present + missing)), sorted(present))
            self.assertEqual(indexer.heavy(self.coll)["files"], 2)
            self.assertEqual(sorted(seen), ["p0.pdf", "p1.pdf"])
            # the missing rows keep their pending work (the spec stays on the row) ...
            self.assertEqual(c.execute("SELECT COUNT(*) FROM files WHERE heavy IS NOT NULL").fetchone()[0], 3)

            # ... and when the files come back, that work resumes; the present rows are already done
            c.execute("UPDATE files SET missing_since=NULL WHERE id IN ({})".format(",".join("?" * 3)), missing)
            c.commit()
            seen.clear()
            self.assertEqual(sorted(indexer.heavy_pending_ids(self.name, missing)), sorted(missing))
            self.assertEqual(indexer.heavy(self.coll)["files"], 3)
            self.assertEqual(sorted(seen), ["m0.pdf", "m1.pdf", "m2.pdf"])

    def test_the_embed_queue_skips_missing_rows_and_resumes_when_they_are_back(self):
        c = self.conn
        present = _add(c, self.folder, "live/p.txt", text="alpha one", kind="text")
        missing = _add(c, self.folder, "gone/m.txt", age=60, text="beta two", kind="text")

        def embedded(fid):
            return c.execute("SELECT embedded FROM items WHERE file_id=?", (fid,)).fetchone()[0]

        with mock.patch.object(indexer, "pool", return_value=FakePool()):
            indexer.embed(self.coll, media=False)
            self.assertEqual((embedded(present), embedded(missing)), (1, 0))
            c.execute("UPDATE files SET missing_since=NULL WHERE id=?", (missing,))
            c.commit()
            indexer.embed(self.coll, media=False)
            self.assertEqual(embedded(missing), 1)

    def test_the_pending_counts_leave_missing_rows_out(self):
        c = self.conn
        for i in range(2):
            _add(c, self.folder, f"live/p{i}.pdf", heavy='{"ocr": [1]}', text=f"present {i}")
        for i in range(3):
            _add(c, self.folder, f"gone/m{i}.pdf", age=60, heavy='{"ocr": [1]}', text=f"missing {i}")
        counts = liveprog.Registry()._db_counts(self.name)
        # the present counts are not zero, so a query that failed would not pass this as "nothing missing"
        self.assertEqual((counts["heavy"], counts["embed"], counts["held"]), (2, 2, 0))
        self.assertEqual(indexer.pending_count(self.name), 2)
        self.assertEqual(indexer.pending_count(self.name, media=False), 2)

    def test_mark_reprocess_does_not_mark_a_missing_file(self):
        c = self.conn
        present = _add(c, self.folder, "live/p.pdf")
        missing = _add(c, self.folder, "gone/m.pdf", age=60)
        for fid in (present, missing):
            store.add_items(c, fid, "x", [{"modality": "ocr", "page": 1, "text": "scanned words"}])
        c.commit()
        res = indexer.mark_reprocess(self.name, ["pdf"], now=1000.0)
        self.assertEqual(res["by_type"], {"pdf": 1})
        self.assertIsNotNone(c.execute("SELECT heavy FROM files WHERE id=?", (present,)).fetchone()[0])
        self.assertIsNone(c.execute("SELECT heavy FROM files WHERE id=?", (missing,)).fetchone()[0])


class OldDatabaseWithoutTheColumn(unittest.TestCase):
    """A database that no writer has opened since files.missing_since was added: read paths must treat no row as missing."""

    def setUp(self):
        self.name = _name()
        store.paths.COLLECTIONS.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(store.db_path(self.name))
        c.enable_load_extension(True)
        sqlite_vec.load(c)
        c.enable_load_extension(False)
        c.executescript(store.SCHEMA)                 # the schema before schema 3: no missing_since
        c.execute("INSERT INTO files(path, name, kind, size, mtime, mode, state, heavy)"
                  " VALUES ('a.pdf', 'a.pdf', 'pdf', 1, 0, 'full', 'done', '{\"ocr\": [1]}')")
        c.execute("INSERT INTO files(path, name, kind, size, mtime, mode, state, heavy)"
                  " VALUES ('b.pdf', 'b.pdf', 'pdf', 1, 0, 'full', 'done', NULL)")
        c.execute("INSERT INTO items(file_id, ord, modality, text, embedded) VALUES (1, 0, 'text', 'x', 0)")
        c.commit()
        c.close()

    def test_the_read_only_counts_work_and_nothing_is_missing(self):
        counts = liveprog.Registry()._db_counts(self.name)
        self.assertEqual((counts["heavy"], counts["embed"], counts["held"]), (1, 1, 0))

    def test_held_is_zero_and_the_forget_removes_nothing(self):
        conn = store.connect(self.name, readonly=True)
        try:
            self.assertEqual(indexer.held_missing(conn), 0)
        finally:
            conn.close()
        res = indexer.forget_missing(self.name, dry_run=True)
        self.assertEqual((res["count"], res["paths"]), (0, []))
        self.assertEqual(indexer.forget_missing(self.name, dry_run=False, as_of=time.time())["removed"], 0)


class HeldCountAndWarning(unittest.TestCase):
    def setUp(self):
        self.name = _name()
        self.folder = _folder()
        self.conn = store.connect(self.name)
        for i in range(200):
            _add(self.conn, self.folder, f"live/f{i}.txt", kind="text")
        for i in range(120):
            _add(self.conn, self.folder, f"cloud/f{i}.txt", age=2 * DAY, kind="text")   # past the grace period

    def tearDown(self):
        self.conn.close()

    def test_status_counts_the_missing_rows_the_guard_holds(self):
        self.assertEqual(liveprog.Registry()._db_counts(self.name)["held"], 120)
        snap = liveprog.Registry().snapshot([self.name])
        self.assertEqual(snap["folders"][self.name]["pending"]["held"], 120)
        line = liveprog.format_line(self.name, snap["folders"][self.name])
        self.assertEqual(line, f"{self.name:<14}idle · 120 missing (held)")

    def test_a_missing_row_inside_the_grace_period_is_not_held(self):
        conn = self.conn
        conn.execute("UPDATE files SET missing_since=? WHERE path LIKE 'cloud/%'", (time.time() - 3600,))
        conn.commit()
        self.assertEqual(indexer.held_missing(conn), 0)

    def test_the_warning_names_the_forget_command(self):
        rows = [dict(r) for r in self.conn.execute("SELECT id, path, missing_since FROM files WHERE missing_since IS NOT NULL")]
        res = indexer._settle_missing(self.conn, self.name, rows, time.time(), DAY, guard=GUARD)
        self.assertEqual((res["removed"], res["held"]), (0, 120))
        self.assertIn("over the threshold", res["warning"])
        self.assertIn(f"held 120 missing files; if you deleted them on purpose, run `mse forget-missing {self.name}`",
                      res["warning"])


class ForgetMissing(unittest.TestCase):
    def setUp(self):
        self.name = _name()
        self.folder = _folder()
        self.conn = store.connect(self.name)

    def tearDown(self):
        self.conn.close()

    def _seed(self, missing: list[str], present: list[str] = ("live/x.txt",)):
        for rel in present:
            _add(self.conn, self.folder, rel, kind="text", text=f"text of {rel}")
        for rel in missing:
            _add(self.conn, self.folder, rel, age=60, kind="text", text=f"text of {rel}")

    def test_a_dry_run_lists_the_missing_rows_and_removes_nothing(self):
        self._seed(["gone/a.txt", "gone/b.txt", "old/c.txt"])
        res = indexer.forget_missing(self.name, dry_run=True)
        self.assertEqual((res["count"], res["removed"], res["dry_run"]), (3, 0, True))
        self.assertEqual(res["paths"], ["gone/a.txt", "gone/b.txt", "old/c.txt"])
        self.assertIsInstance(res["as_of"], float)
        self.assertEqual(len(_rows(self.conn)), 4)

    def test_the_listing_is_capped_at_twenty_paths(self):
        self._seed([f"gone/f{i:02d}.txt" for i in range(25)])
        res = indexer.forget_missing(self.name, dry_run=True)
        self.assertEqual((res["count"], len(res["paths"])), (25, 20))

    def test_a_scope_takes_its_folder_only_and_never_a_look_alike(self):
        self._seed(["gone/a.txt", "gone/sub/c.txt", "gone2/b.txt", "a_b/x.txt", "aXb/y.txt"])
        listed = indexer.forget_missing(self.name, "gone", dry_run=True)
        self.assertEqual(listed["paths"], ["gone/a.txt", "gone/sub/c.txt"])
        res = indexer.forget_missing(self.name, "gone", dry_run=False, as_of=listed["as_of"])
        self.assertEqual(res["removed"], 2)
        self.assertIn("gone2/b.txt", _rows(self.conn))
        # the underscore is a literal in a path, not a wildcard
        under = indexer.forget_missing(self.name, "a_b", dry_run=True)
        self.assertEqual(under["paths"], ["a_b/x.txt"])

    def test_present_rows_and_files_on_disk_are_never_touched(self):
        self._seed(["gone/a.txt"])
        # a row marked missing whose file is in fact on disk (the mark is stale): its row goes, the file stays
        stale = self.folder / "stale.txt"
        stale.write_text("still here")
        _add(self.conn, self.folder, "stale.txt", age=60, kind="text")
        listed = indexer.forget_missing(self.name, dry_run=True)
        res = indexer.forget_missing(self.name, dry_run=False, as_of=listed["as_of"])
        self.assertEqual(res["removed"], 2)
        self.assertEqual(_rows(self.conn), ["live/x.txt"])
        self.assertTrue((self.folder / "live" / "x.txt").exists())
        self.assertTrue(stale.exists(), "nothing on disk is removed")
        self.assertEqual(stale.read_text(), "still here")

    def test_a_removal_needs_as_of_and_removes_only_what_was_listed(self):
        self._seed(["gone/a.txt", "gone/b.txt"])
        listed = indexer.forget_missing(self.name, dry_run=True)
        # a folder that goes offline after the listing: its rows were marked after it, so they stay
        _add(self.conn, self.folder, "late/z.txt", age=-30, kind="text")
        with self.assertRaises(ValueError):
            indexer.forget_missing(self.name, dry_run=False)
        res = indexer.forget_missing(self.name, dry_run=False, as_of=listed["as_of"])
        self.assertEqual(res["removed"], 2)
        self.assertEqual(_rows(self.conn), ["late/z.txt", "live/x.txt"])

    def test_the_removal_cleans_text_vectors_and_favourites_of_those_files_only(self):
        self._seed(["gone/a.txt"])
        fid = self.conn.execute("SELECT id FROM files WHERE path='gone/a.txt'").fetchone()[0]
        item = self.conn.execute("SELECT id FROM items WHERE file_id=?", (fid,)).fetchone()[0]
        store.store_vectors(self.conn, [(item, "text", np.ones(store.DIMS, dtype=np.float32))])
        self.conn.commit()
        query_gone = f"gone file {uuid.uuid4().hex}"
        query_live = f"live file {uuid.uuid4().hex}"
        other = _name()
        for q, coll, path in ((query_gone, self.name, "gone/a.txt"), (query_live, self.name, "live/x.txt"),
                              (query_gone, other, "gone/a.txt")):
            # rows are written directly: feedback.add would embed the query with the model
            fc = feedback._connect(create=True)
            try:
                fc.execute("INSERT INTO feedback(query, query_norm, qvec, collection, path, docid, kind, created, uses,"
                           " last_used) VALUES (?,?,NULL,?,?,NULL,'best',?,0,NULL)",
                           (q, feedback.normalize(q), coll, path, time.time()))
                fc.commit()
            finally:
                fc.close()
        listed = indexer.forget_missing(self.name, dry_run=True)
        res = indexer.forget_missing(self.name, dry_run=False, as_of=listed["as_of"])
        self.assertEqual((res["removed"], res["favourites_removed"]), (1, 1))
        self.assertNotIn("favourites_error", res)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM items WHERE file_id=?", (fid,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM vectors WHERE item_id=?", (item,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM vbin WHERE item_id=?", (item,)).fetchone()[0], 0)
        left = [f for f in feedback.list_items() if f["collection"] in (self.name, other) and f["query"] in (query_gone, query_live)]
        self.assertEqual(sorted((f["collection"], f["path"]) for f in left),
                         sorted([(self.name, "live/x.txt"), (other, "gone/a.txt")]))

    def test_a_missing_collection_is_a_quiet_zero(self):
        res = indexer.forget_missing(_name(), dry_run=True)
        self.assertEqual((res["count"], res["paths"]), (0, []))

    def test_a_path_that_leaves_the_collection_is_refused(self):
        self._seed(["gone/a.txt"])
        for bad in ("../x", "gone/../../x", "/abs/path"):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                indexer.forget_missing(self.name, bad, dry_run=True)


class ForgetMissingCli(unittest.TestCase):
    """`mse forget-missing` as the person runs it: the list first, then removal only on a yes."""

    def setUp(self):
        self.name = _name()
        self.folder = _folder()
        self.coll = {"name": self.name, "path": str(self.folder), "media": {}}
        self.conn = store.connect(self.name)
        _add(self.conn, self.folder, "live/x.txt", kind="text")
        _add(self.conn, self.folder, "gone/a.txt", age=60, kind="text")
        _add(self.conn, self.folder, "gone/b.txt", age=60, kind="text")
        patcher = mock.patch.object(config, "get_collection",
                                    side_effect=lambda cfg, n: self.coll if n == self.name else None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.conn.close()

    def run_cli(self, *, path="", dry_run=False, yes=False, answer=None):
        a = argparse.Namespace(collection=self.name, path=path, dry_run=dry_run, yes=yes)
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            if answer is None:
                ask = stack.enter_context(mock.patch("builtins.input", side_effect=EOFError))
            else:
                ask = stack.enter_context(mock.patch("builtins.input", return_value=answer))
            try:
                cli.cmd_forget_missing(a)
                code = None
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), ask

    def test_without_a_yes_at_the_prompt_nothing_is_removed(self):
        code, out, _ = self.run_cli()                          # the prompt gets end of input: a refusal
        self.assertIsNotNone(code)
        self.assertIn("not removed", str(code))
        self.assertIn("gone/a.txt", out)
        self.assertEqual(len(_rows(self.conn)), 3)

    def test_any_answer_but_yes_refuses(self):
        code, _, _ = self.run_cli(answer="y")
        self.assertIsNotNone(code)
        self.assertEqual(len(_rows(self.conn)), 3)

    def test_a_typed_yes_removes_only_the_missing_rows(self):
        code, out, _ = self.run_cli(answer="yes")
        self.assertIsNone(code)
        self.assertIn("removed 2 row(s)", out)
        self.assertEqual(_rows(self.conn), ["live/x.txt"])

    def test_yes_flag_removes_without_asking(self):
        code, out, ask = self.run_cli(yes=True)
        self.assertIsNone(code)
        ask.assert_not_called()
        self.assertEqual(_rows(self.conn), ["live/x.txt"])

    def test_a_dry_run_never_removes_even_with_yes(self):
        code, out, ask = self.run_cli(dry_run=True, yes=True)
        self.assertIsNone(code)
        ask.assert_not_called()
        self.assertIn("dry run: nothing was removed", out)
        self.assertEqual(len(_rows(self.conn)), 3)

    def test_a_scope_limits_the_removal_to_that_folder(self):
        code, _, _ = self.run_cli(path=str(self.folder / "gone"), yes=True)
        self.assertIsNone(code)
        self.assertEqual(_rows(self.conn), ["live/x.txt"])

    def test_a_path_outside_the_collection_is_refused(self):
        code, _, _ = self.run_cli(path="/etc", yes=True)
        self.assertIn("must be a folder inside", str(code))
        self.assertEqual(len(_rows(self.conn)), 3)

    def test_nothing_to_forget_says_so(self):
        self.conn.execute("UPDATE files SET missing_since=NULL")
        self.conn.commit()
        code, out, ask = self.run_cli(yes=True)
        self.assertIsNone(code)
        self.assertIn("nothing to forget", out)
        ask.assert_not_called()


class ForgetRoute(unittest.TestCase):
    """POST /api/collections/<name>/forget-missing: behind the token, a dry run unless told otherwise."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.token = config.load()["token"]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.name = _name()
        self.folder = _folder()
        self.coll = {"name": self.name, "path": str(self.folder), "media": {}}
        self.conn = store.connect(self.name)
        _add(self.conn, self.folder, "live/x.txt", kind="text")
        _add(self.conn, self.folder, "gone/a.txt", age=60, kind="text")
        patcher = mock.patch.object(config, "get_collection",
                                    side_effect=lambda cfg, n: self.coll if n == self.name else None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.conn.close)

    def post(self, body=None, token=True, origin=None, path=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-MSE-Token"] = self.token
        if origin:
            headers["Origin"] = origin
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request("POST", path or f"/api/collections/{self.name}/forget-missing",
                         body=None if body is None else json.dumps(body), headers=headers)
            r = conn.getresponse()
            data = r.read()
            return r.status, json.loads(data or b"{}")
        finally:
            conn.close()

    def test_the_token_is_needed(self):
        for kwargs in ({"token": False}, {"origin": "http://evil.example"}):
            with self.subTest(**kwargs):
                status, _ = self.post({"dry_run": False, "as_of": time.time()}, **kwargs)
                self.assertEqual(status, 401)
        self.assertEqual(len(_rows(self.conn)), 2)

    def test_a_post_with_the_token_is_a_dry_run_by_default(self):
        status, body = self.post({})
        self.assertEqual(status, 200)
        self.assertEqual((body["count"], body["removed"], body["dry_run"]), (1, 0, True))
        self.assertEqual(body["paths"], ["gone/a.txt"])
        self.assertEqual(len(_rows(self.conn)), 2)

    def test_a_removal_needs_the_as_of_of_its_dry_run(self):
        status, body = self.post({"dry_run": False})
        self.assertEqual(status, 400)
        self.assertIn("as_of", body["error"])
        self.assertEqual(len(_rows(self.conn)), 2)

    def test_a_removal_with_as_of_removes_only_the_listed_rows(self):
        _, listed = self.post({})
        status, body = self.post({"dry_run": False, "as_of": listed["as_of"]})
        self.assertEqual(status, 200)
        self.assertEqual((body["removed"], body["dry_run"]), (1, False))
        self.assertEqual(_rows(self.conn), ["live/x.txt"])

    def test_bad_values_are_refused(self):
        bad = [{"dry_run": "no"}, {"dry_run": 0}, {"path": "../x"}, {"path": "/abs/x"}, {"path": 5},
               {"dry_run": False, "as_of": "now"}, {"dry_run": False, "as_of": True}]
        for body in bad:
            with self.subTest(body=body):
                status, _ = self.post(body)
                self.assertEqual(status, 400)
        self.assertEqual(len(_rows(self.conn)), 2)

    def test_an_unknown_collection_is_404(self):
        status, _ = self.post({}, path="/api/collections/no-such-collection/forget-missing")
        self.assertEqual(status, 404)

    def test_get_is_not_a_route(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request("GET", f"/api/collections/{self.name}/forget-missing",
                         headers={"X-MSE-Token": self.token})
            self.assertEqual(conn.getresponse().status, 404)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
