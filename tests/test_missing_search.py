"""Search never shows a file the indexer has marked missing (files.missing_since), on any path: keyword, vector-only
(binary and exact), filtered, get by docid or path, list_files and favourites. A database that no writer has opened
since the upgrade (no column) still works. Only temporary folders and a temporary data folder are used.
Run: .venv/bin/python -m unittest -v tests.test_missing_search"""

import os
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

import numpy as np  # noqa: E402
import sqlite_vec  # noqa: E402

from mse import config, feedback, indexer, search, store  # noqa: E402

DIMS = store.DIMS


def _unit(axis: int) -> np.ndarray:
    v = np.zeros(DIMS, dtype=np.float32)
    v[axis] = 1.0
    return v


def _collection(folder: Path) -> dict:
    return {"name": "t" + uuid.uuid4().hex[:10], "path": str(folder), "enabled": True, "media": {},
            "exclude": [], "metadata_only": []}


def _insert_file(conn, folder: Path, coll: str, rel: str, text: str, vec: np.ndarray, missing: bool) -> int:
    (folder / rel).write_text(text)
    cur = conn.execute(
        "INSERT INTO files(path, name, ext, kind, size, mtime, docid, indexed_at, mode, state, missing_since)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (rel, rel, "txt", "text", len(text), time.time(), indexer.docid(coll, rel), time.time(), "full", "done",
         time.time() - 3600 if missing else None))
    fid = cur.lastrowid
    store.add_items(conn, fid, rel, [{"modality": "text", "text": text}])
    item = conn.execute("SELECT id FROM items WHERE file_id=?", (fid,)).fetchone()[0]
    store.store_vectors(conn, [(item, "text", vec)])
    return fid


def _config_for(coll: dict) -> dict:
    cfg = config.load()
    cfg["collections"] = [coll]
    return cfg


class SearchHidesMissingFiles(unittest.TestCase):
    """Two files share the word 'zebra'. The second one is missing and matches better, so the top-k cut is what would
    show it first if it were not filtered before the cut."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="mse-srch-"))
        self.coll = _collection(self.folder)
        self.name = self.coll["name"]
        conn = store.connect(self.name)
        self.present = _insert_file(conn, self.folder, self.name, "present.txt", "zebra crossing",
                                    _unit(0), missing=False)
        self.gone = _insert_file(conn, self.folder, self.name, "gone.txt",
                                 "zebra zebra zebra stripes zebra", _unit(1), missing=True)
        conn.commit()
        conn.close()
        cfg = _config_for(self.coll)
        self.patch = mock.patch.object(config, "load", lambda: cfg)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def _paths(self, res) -> list[str]:
        return [r["path"] for r in res["results"]]

    def test_keyword_search_does_not_return_a_missing_file(self):
        res = search.search("zebra", collections=[self.name], mode="keyword", limit=10)
        self.assertEqual(self._paths(res), ["present.txt"])

    def test_keyword_top_k_still_fills_when_the_best_match_is_missing(self):
        res = search.search("zebra", collections=[self.name], mode="keyword", limit=1)
        self.assertEqual(self._paths(res), ["present.txt"])

    def test_filtered_keyword_search_excludes_a_missing_file_too(self):
        res = search.search("zebra", collections=[self.name], mode="keyword", filters={"type": ["text"]}, limit=10)
        self.assertEqual(self._paths(res), ["present.txt"])

    def test_vector_only_search_does_not_return_a_missing_file(self):
        # the query points at the missing file's vector: without the filter it would win
        res = search.search("", collections=[self.name], mode="semantic", qvec=_unit(1), limit=10)
        self.assertNotIn("gone.txt", self._paths(res))
        self.assertIn("present.txt", self._paths(res))

    def test_vector_only_search_exact_mode_does_not_return_a_missing_file(self):
        res = search.search("", collections=[self.name], mode="semantic", qvec=_unit(1), limit=10,
                            opts={"vector_mode": "exact"})
        self.assertNotIn("gone.txt", self._paths(res))

    def test_vector_only_top_k_still_fills(self):
        res = search.search("", collections=[self.name], mode="semantic", qvec=_unit(1), limit=1)
        self.assertEqual(self._paths(res), ["present.txt"])

    def test_get_by_path_and_by_docid_refuses_a_missing_file(self):
        self.assertIsNotNone(search.get_document(self.name, "present.txt"))
        self.assertIsNone(search.get_document(self.name, "gone.txt"))
        self.assertIsNone(search.get_document(self.name, "#" + indexer.docid(self.name, "gone.txt")))
        self.assertIsNone(search.find_file(self.name, "gone.txt"))

    def test_list_files_omits_a_missing_file(self):
        names = [f["path"] for f in search.list_files(self.name, "", 50)]
        self.assertEqual(names, ["present.txt"])

    def test_a_missing_file_is_shown_again_once_it_is_seen(self):
        conn = store.connect(self.name)
        conn.execute("UPDATE files SET missing_since=NULL WHERE id=?", (self.gone,))
        conn.commit()
        conn.close()
        res = search.search("zebra", collections=[self.name], mode="keyword", limit=10)
        self.assertEqual(set(self._paths(res)), {"present.txt", "gone.txt"})

    def test_a_favourite_loader_skips_a_missing_file_and_keeps_a_present_one(self):
        coll = config.load()["collections"][0]
        row = {"path": "gone.txt", "docid": indexer.docid(self.name, "gone.txt")}
        self.assertIsNone(feedback._load_file(coll, row, None, None))
        ok_row = {"path": "present.txt", "docid": indexer.docid(self.name, "present.txt")}
        self.assertIsNotNone(feedback._load_file(coll, ok_row, None, None))


class FavouritesAndMissingFiles(unittest.TestCase):
    """A real favourite (feedback.add) on a file that then goes missing, and a favourite added to a file already missing."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="mse-fav-"))
        self.coll = _collection(self.folder)
        self.name = self.coll["name"]
        conn = store.connect(self.name)
        _insert_file(conn, self.folder, self.name, "present.txt", "zebra crossing", _unit(0), missing=False)
        _insert_file(conn, self.folder, self.name, "gone.txt", "zebra zebra zebra stripes", _unit(1), missing=False)
        conn.commit()
        conn.close()
        cfg = _config_for(self.coll)
        self.patches = [mock.patch.object(config, "load", lambda: cfg),
                        mock.patch.object(search, "embed_query", lambda q: _unit(0))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        for ref in ("present.txt", "gone.txt"):              # the favourites this test added, nothing else
            try:
                feedback.remove(query="zebra", collection=self.name, ref=ref)
            except (KeyError, ValueError):
                pass

    def _mark_missing(self, rel: str):
        conn = store.connect(self.name)
        conn.execute("UPDATE files SET missing_since=? WHERE path=?", (time.time() - 3600, rel))
        conn.commit()
        conn.close()

    def _search(self):
        return search.search("zebra", collections=[self.name], mode="keyword", limit=10)

    def test_a_favourite_on_a_present_file_pins_it_to_the_top(self):
        feedback.add("zebra", self.name, "present.txt", "best")
        res = self._search()
        self.assertEqual(res["results"][0]["path"], "present.txt")
        self.assertNotIn("error", res["feedback"])

    def test_a_favourite_whose_file_goes_missing_is_not_shown_and_breaks_nothing(self):
        feedback.add("zebra", self.name, "gone.txt", "best")
        self._mark_missing("gone.txt")
        res = self._search()
        self.assertNotIn("gone.txt", [r["path"] for r in res["results"]])
        self.assertIn("present.txt", [r["path"] for r in res["results"]])
        self.assertNotIn("error", res["feedback"])

    def test_adding_a_favourite_to_a_missing_file_fails_cleanly(self):
        self._mark_missing("gone.txt")
        with self.assertRaises(KeyError):
            feedback.add("zebra", self.name, "gone.txt", "best")


class OldDatabaseWithoutTheColumn(unittest.TestCase):
    """A database built from the schema alone: no missing_since, no hash column. Read-only connections never run the
    migration, so search must cope with it (the column is looked up, not assumed)."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="mse-old-"))
        self.coll = _collection(self.folder)
        self.name = self.coll["name"]
        raw = sqlite3.connect(store.db_path(self.name))
        raw.enable_load_extension(True)
        sqlite_vec.load(raw)
        raw.enable_load_extension(False)
        raw.executescript(store.SCHEMA)
        (self.folder / "old.txt").write_text("zebra notebook")
        cur = raw.execute("INSERT INTO files(path, name, ext, kind, size, mtime, docid, mode, state)"
                          " VALUES ('old.txt','old.txt','txt','text',14,?,?,'full','done')",
                          (time.time(), indexer.docid(self.name, "old.txt")))
        store.add_items(raw, cur.lastrowid, "old.txt", [{"modality": "text", "text": "zebra notebook"}])
        item = raw.execute("SELECT id FROM items WHERE file_id=?", (cur.lastrowid,)).fetchone()[0]
        store.store_vectors(raw, [(item, "text", _unit(0))])
        raw.commit()
        raw.close()
        cfg = _config_for(self.coll)
        self.patch = mock.patch.object(config, "load", lambda: cfg)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_the_column_is_absent_and_the_search_still_works(self):
        conn = store.connect(self.name, readonly=True)
        try:
            self.assertFalse(search._has_missing_col(conn))
        finally:
            conn.close()
        res = search.search("zebra", collections=[self.name], mode="keyword", limit=5)
        self.assertEqual([r["path"] for r in res["results"]], ["old.txt"])

    def test_vector_search_and_get_and_list_work_without_the_column(self):
        res = search.search("", collections=[self.name], mode="semantic", qvec=_unit(0), limit=5)
        self.assertEqual([r["path"] for r in res["results"]], ["old.txt"])
        self.assertIsNotNone(search.get_document(self.name, "old.txt"))
        self.assertEqual([f["path"] for f in search.list_files(self.name, "", 10)], ["old.txt"])


class ColumnCheckIsPerCall(unittest.TestCase):
    """The column is looked up on each call, so a database that gains the column later is filtered from then on."""

    def test_the_filter_appears_once_the_column_exists(self):
        folder = Path(tempfile.mkdtemp(prefix="mse-late-"))
        coll = _collection(folder)
        name = coll["name"]
        raw = sqlite3.connect(store.db_path(name))
        raw.enable_load_extension(True)
        sqlite_vec.load(raw)
        raw.enable_load_extension(False)
        raw.executescript(store.SCHEMA)
        raw.commit()
        raw.close()
        ro = store.connect(name, readonly=True)
        try:
            self.assertEqual(search._present(ro, "f."), "")
        finally:
            ro.close()
        store.connect(name).close()                    # a writer runs the additive migration
        ro = store.connect(name, readonly=True)
        try:
            self.assertEqual(search._present(ro, "f."), " AND f.missing_since IS NULL")
        finally:
            ro.close()


if __name__ == "__main__":
    unittest.main()
