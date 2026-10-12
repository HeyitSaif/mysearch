"""Indexing safety: a vanished file's rows are kept for a grace period, a file whose content is in the cloud is never read,
a Keynote deck is told apart from a private key, and --reocr / --retranscribe mark files without reading them.

Only temporary folders and a temporary data folder are used; no real index or real folder is touched.
Run: .venv/bin/python -m unittest -v tests.test_indexing_safety"""

import argparse
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import cli, config, extract, indexer, store  # noqa: E402
from mse.ignore import DEFAULT_EXCLUDES, SECRET_PATTERNS, Matcher  # noqa: E402


class _Flagged:
    """A stat result that carries the macOS dataless flag (st_flags); every other field is the real stat's."""

    def __init__(self, st, flags: int):
        self._st = st
        self.st_flags = flags

    def __getattr__(self, name):
        return getattr(self._st, name)


def _cloud(in_cloud: set[str]):
    """Patches the stat seam so the given absolute paths report the dataless flag, as a File Provider placeholder does."""
    real = os.lstat

    def fake(p):
        st = real(p)
        return _Flagged(st, indexer.SF_DATALESS) if str(p) in in_cloud else st

    return mock.patch.object(indexer, "_lstat", fake)


def _coll(folder: Path) -> dict:
    return {"name": "t" + uuid.uuid4().hex[:10], "path": str(folder), "media": {}, "exclude": [], "metadata_only": []}


def _row(coll: dict, rel: str):
    conn = store.connect(coll["name"], readonly=True)
    try:
        return conn.execute("SELECT * FROM files WHERE path=?", (rel,)).fetchone()
    finally:
        conn.close()


def _items(coll: dict, rel: str, modality: str) -> int:
    conn = store.connect(coll["name"], readonly=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM items i JOIN files f ON f.id = i.file_id"
                            " WHERE f.path=? AND i.modality=?", (rel, modality)).fetchone()[0]
    finally:
        conn.close()


def _age_mark(coll: dict, rel: str, seconds: float) -> None:
    conn = store.connect(coll["name"])
    conn.execute("UPDATE files SET missing_since = missing_since - ? WHERE path=?", (seconds, rel))
    conn.commit()
    conn.close()


class MissingRows(unittest.TestCase):
    """missing_since: a row is marked on the first pass that does not see its file, and removed only by a later pass
    at least the grace period after the mark. A pass that sees the file again clears the mark."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="mse-missing-"))
        self.away = Path(tempfile.mkdtemp(prefix="mse-away-"))      # outside the collection: a vanished file goes here
        self.coll = _coll(self.folder)
        (self.folder / "note.txt").write_text("alpha beta gamma")
        self.first = indexer.scan(self.coll)

    def test_first_scan_indexes_the_file(self):
        self.assertIsNotNone(_row(self.coll, "note.txt"))
        self.assertIsNone(_row(self.coll, "note.txt")["missing_since"])
        self.assertEqual(self.first["kept_missing"], 0)

    def test_vanished_file_is_marked_kept_then_removed_after_the_grace_period(self):
        os.rename(self.folder / "note.txt", self.away / "note.txt")
        s1 = indexer.scan(self.coll)
        self.assertEqual((s1["removed"], s1["kept_missing"], s1["missing_marked"]), (0, 1, 1))
        self.assertIsNotNone(_row(self.coll, "note.txt")["missing_since"])
        s2 = indexer.scan(self.coll)                       # a later pass, but inside the grace period
        self.assertEqual((s2["removed"], s2["kept_missing"], s2["missing_marked"]), (0, 1, 0))
        self.assertIsNotNone(_row(self.coll, "note.txt"))
        _age_mark(self.coll, "note.txt", 25 * 3600)        # the mark is now more than 24 h old
        s3 = indexer.scan(self.coll)
        self.assertEqual(s3["removed"], 1)
        self.assertIsNone(_row(self.coll, "note.txt"))

    def test_a_folder_that_lists_empty_loses_no_rows_at_once(self):
        os.rename(self.folder / "note.txt", self.away / "note.txt")      # the folder now lists empty
        s = indexer.scan(self.coll)
        self.assertEqual(s["removed"], 0)
        self.assertEqual(s["kept_missing"], 1)
        self.assertIsNotNone(_row(self.coll, "note.txt"))

    def test_a_file_that_reappears_clears_the_mark(self):
        os.rename(self.folder / "note.txt", self.away / "note.txt")
        indexer.scan(self.coll)
        self.assertIsNotNone(_row(self.coll, "note.txt")["missing_since"])
        os.rename(self.away / "note.txt", self.folder / "note.txt")
        s = indexer.scan(self.coll)
        self.assertIsNone(_row(self.coll, "note.txt")["missing_since"])
        self.assertEqual((s["removed"], s["kept_missing"]), (0, 0))

    def test_the_grace_period_is_configurable(self):
        os.rename(self.folder / "note.txt", self.away / "note.txt")
        with mock.patch.object(indexer, "_settings", lambda: {"missing_grace_h": 0}):
            s1 = indexer.scan(self.coll)
            s2 = indexer.scan(self.coll)
        self.assertEqual((s1["removed"], s1["missing_marked"]), (0, 1))
        self.assertEqual(s2["removed"], 1)

    def test_a_row_is_marked_by_the_pass_that_sees_it_missing_and_not_before(self):
        self.assertIsNone(_row(self.coll, "note.txt")["missing_since"])
        (self.folder / "other.txt").write_text("delta")
        s = indexer.scan(self.coll)
        self.assertEqual(s["missing_marked"], 0)
        self.assertIsNone(_row(self.coll, "note.txt")["missing_since"])


class DatalessFiles(unittest.TestCase):
    """A file whose content is in the cloud is listed by its name and dates. Nothing reads it, so nothing downloads it."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="mse-cloud-"))
        self.coll = _coll(self.folder)
        self.f = self.folder / "report.txt"
        self.f.write_text("xylophone sentence")

    def test_a_new_cloud_file_is_listed_by_name_and_its_content_is_not_indexed(self):
        with _cloud({str(self.f)}):
            s = indexer.scan(self.coll)
        row = _row(self.coll, "report.txt")
        self.assertEqual(row["mode"], "meta")
        self.assertEqual(_items(self.coll, "report.txt", "text"), 0)
        self.assertEqual(s["dataless"], 1)

    def test_the_read_itself_never_starts_for_a_cloud_file(self):
        with _cloud({str(self.f)}), mock.patch.object(extract, "light", side_effect=AssertionError("read")):
            prep = indexer._prepare(self.coll, "report.txt", "full")
        self.assertTrue(prep.get("deferred"))
        self.assertIsNone(prep["hash"])

    def test_a_file_that_is_materialised_is_read_on_the_next_pass(self):
        with _cloud({str(self.f)}):
            indexer.scan(self.coll)
        self.assertEqual(_row(self.coll, "report.txt")["mode"], "meta")
        indexer.scan(self.coll)                              # the flag is gone: the file is local now
        self.assertEqual(_row(self.coll, "report.txt")["mode"], "full")
        self.assertGreaterEqual(_items(self.coll, "report.txt", "text"), 1)

    def test_an_evicted_full_row_keeps_its_content(self):
        indexer.scan(self.coll)                              # local: indexed in full
        self.assertGreaterEqual(_items(self.coll, "report.txt", "text"), 1)
        with _cloud({str(self.f)}):
            s = indexer.scan(self.coll)                      # evicted: same size and date
        self.assertEqual(_row(self.coll, "report.txt")["mode"], "full")
        self.assertGreaterEqual(_items(self.coll, "report.txt", "text"), 1)
        self.assertEqual(s["dataless_kept"], 1)

    def test_an_icloud_placeholder_is_never_read_and_never_indexed(self):
        # DEFAULT_EXCLUDES' ".*/" rule drops every dot-named entry (it is compiled without its slash), so a legacy
        # placeholder is never walked. The _mode guard is the second line: even if one were, it is metadata-only.
        ph = self.folder / ".notes.docx.icloud"
        ph.write_bytes(b"bplist00 placeholder")
        with mock.patch.object(extract, "light", side_effect=AssertionError("read")):
            indexer.scan(self.coll)
        self.assertIsNone(_row(self.coll, ".notes.docx.icloud"))
        self.assertEqual(indexer._mode(indexer.matchers(self.coll)[1], ".notes.docx.icloud", 9, 10 ** 9), "meta")

    def test_a_cloud_file_hash_is_never_computed(self):
        with _cloud({str(self.f)}):
            self.assertIsNone(indexer.file_hash(self.f))


class KeynoteRules(unittest.TestCase):
    """A .key that is a package folder, or a file over 64 KB, is a Keynote deck. A .key of 64 KB or less is a private key.
    Every other secret rule still applies, to a deck as well."""

    def setUp(self):
        r = Path(tempfile.mkdtemp(prefix="mse-keynote-"))
        self.root = r
        (r / "small.key").write_bytes(b"-----BEGIN PRIVATE KEY-----\n" + b"x" * 100)
        (r / "exact.key").write_bytes(b"k" * (64 * 1024))                     # exactly the limit: still a secret
        (r / "large.key").write_bytes(b"z" * (64 * 1024 + 1))                 # one byte over: a Keynote file
        deck = r / "Talk.key"
        (deck / "QuickLook").mkdir(parents=True)
        (deck / "QuickLook" / "Preview.pdf").write_bytes(b"%PDF-1.4 x")
        (deck / "Index.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        (r / "notes").mkdir()
        (r / "notes" / "Old.key").mkdir()                                    # a deck inside an ordinary folder
        (r / "notes" / "Old.key" / "Index.zip").write_bytes(b"PK")
        (r / "secret-plans.key").mkdir()                                      # **/*secret* excludes it
        (r / "secret-plans.key" / "x.txt").write_text("x")
        (r / "my_credentials_deck.key").mkdir()                               # **/*credential* excludes it
        (r / "my_credentials_deck.key" / "x.txt").write_text("x")
        (r / "id_rsa_backup.key").write_bytes(b"k" * (70 * 1024))             # id_rsa* excludes it at any size
        self.coll = _coll(r)
        self.rels = {rel for rel, *_ in indexer._walk(self.coll, [])}

    def test_a_small_key_file_stays_a_secret(self):
        self.assertNotIn("small.key", self.rels)
        self.assertNotIn("exact.key", self.rels)

    def test_a_key_file_over_the_limit_is_indexed_as_keynote(self):
        self.assertIn("large.key", self.rels)

    def test_a_deck_folder_is_one_entry_and_nothing_inside_it_is_walked(self):
        self.assertIn("Talk.key", self.rels)
        self.assertNotIn("Talk.key/QuickLook/Preview.pdf", self.rels)
        self.assertNotIn("Talk.key/Index.zip", self.rels)

    def test_a_deck_inside_an_ordinary_folder_is_kept(self):
        self.assertIn("notes/Old.key", self.rels)
        self.assertNotIn("notes/Old.key/Index.zip", self.rels)

    def test_other_secret_rules_still_exclude_a_deck(self):
        for rel in ("secret-plans.key", "my_credentials_deck.key", "id_rsa_backup.key"):
            self.assertNotIn(rel, self.rels, rel)

    def test_the_secret_matcher_itself_is_unchanged(self):
        m = Matcher(SECRET_PATTERNS + DEFAULT_EXCLUDES)
        self.assertTrue(m.match("certs/server.key"))
        self.assertTrue(m.match("Talk.key/Index.zip"))

    def test_the_size_rule_is_a_stat_decision_at_the_boundary(self):
        nokey = indexer._nokey_matcher(self.coll)
        exclude, _ = indexer.matchers(self.coll)
        self.assertTrue(indexer._secret_excluded("exact.key", exclude, nokey, self.root))
        self.assertFalse(indexer._secret_excluded("large.key", exclude, nokey, self.root))


class WatcherPathsAndHeavy(unittest.TestCase):
    """The event path (index_paths, index_one for a missing file) and the heavy stage follow the same rules as a scan."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="mse-watch-"))
        self.away = Path(tempfile.mkdtemp(prefix="mse-away-"))
        self.coll = _coll(self.folder)
        (self.folder / "a.txt").write_text("alpha words")
        indexer.scan(self.coll)

    def test_index_paths_marks_a_removed_file_instead_of_removing_it(self):
        os.rename(self.folder / "a.txt", self.away / "a.txt")
        res = indexer.index_paths(self.coll, ["a.txt"])
        self.assertEqual((res["removed"], res["missing_marked"]), (0, 1))
        self.assertIsNotNone(_row(self.coll, "a.txt")["missing_since"])

    def test_index_paths_indexes_a_new_file_and_lists_a_cloud_one_by_name(self):
        (self.folder / "b.txt").write_text("beta words")
        res = indexer.index_paths(self.coll, ["b.txt"])
        self.assertEqual(res["changed"], 1)
        self.assertEqual(_row(self.coll, "b.txt")["mode"], "full")
        c = self.folder / "c.txt"
        c.write_text("gamma words")
        with _cloud({str(c)}):
            indexer.index_paths(self.coll, ["c.txt"])
        self.assertEqual(_row(self.coll, "c.txt")["mode"], "meta")

    def test_index_paths_keeps_a_keynote_deck_as_one_entry(self):
        deck = self.folder / "Talk.key"
        (deck / "QuickLook").mkdir(parents=True)
        (deck / "QuickLook" / "Preview.pdf").write_bytes(b"%PDF-1.4 x")
        indexer.index_paths(self.coll, ["Talk.key"])
        self.assertIsNotNone(_row(self.coll, "Talk.key"))
        self.assertIsNone(_row(self.coll, "Talk.key/QuickLook/Preview.pdf"))

    def test_index_one_marks_a_missing_file_and_returns_before_any_embedding(self):
        os.rename(self.folder / "a.txt", self.away / "a.txt")
        res = indexer.index_one(self.coll, "a.txt")
        self.assertEqual((res["removed"], res["kept_missing"]), (False, True))
        self.assertIsNotNone(_row(self.coll, "a.txt")["missing_since"])

    def test_heavy_leaves_a_cloud_file_pending_and_reads_nothing(self):
        conn = store.connect(self.coll["name"])
        conn.execute("UPDATE files SET heavy=? WHERE path='a.txt'", ('{"audio": true}',))
        conn.commit()
        conn.close()
        with _cloud({str(self.folder / "a.txt")}), \
                mock.patch.object(indexer.extract, "heavy", side_effect=AssertionError("read")):
            res = indexer.heavy(self.coll)
        self.assertEqual(res["deferred"], 1)
        row = _row(self.coll, "a.txt")
        self.assertEqual(json.loads(row["heavy"]), {"audio": True})


class ReprocessMarks(unittest.TestCase):
    """--reocr and --retranscribe set the heavy spec that heavy() reads. They read no file, keep the old OCR and
    transcripts in place, skip what is already queued, and never touch a file that has nothing to redo."""

    def setUp(self):
        self.coll = {"name": "t" + uuid.uuid4().hex[:10], "path": tempfile.mkdtemp(prefix="mse-reocr-")}
        conn = store.connect(self.coll["name"])
        self.fid = {}
        for path, kind, mode, heavy in (
                ("scan.pdf", "pdf", "full", None),
                ("sub/scan2.pdf", "pdf", "full", None),
                ("textonly.pdf", "pdf", "full", None),
                ("queued.pdf", "pdf", "full", '{"thumb": true}'),
                ("pic.jpg", "image", "full", None),
                ("meta.jpg", "image", "meta", None),
                ("song.m4a", "audio", "full", None),
                ("clip.mp4", "video", "full", None),
                ("note.txt", "text", "full", None)):
            cur = conn.execute("INSERT INTO files(path, name, ext, kind, mode, heavy, docid) VALUES (?,?,?,?,?,?,?)",
                               (path, Path(path).name, Path(path).suffix.lstrip("."), kind, mode, heavy, "d" + path[:6]))
            self.fid[path] = cur.lastrowid
        for rel, modality, page in (("scan.pdf", "ocr", 2), ("scan.pdf", "ocr", 5), ("scan.pdf", "page", 1),
                                    ("sub/scan2.pdf", "ocr", 3), ("pic.jpg", "ocr", None),
                                    ("song.m4a", "transcript", None)):
            conn.execute("INSERT INTO items(file_id, ord, modality, page, text, embedded) VALUES (?,?,?,?,?,1)",
                         (self.fid[rel], 0, modality, page, "old " + modality))
        conn.commit()
        conn.close()

    def _items_total(self) -> int:
        conn = store.connect(self.coll["name"], readonly=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        finally:
            conn.close()

    def _heavy(self, rel: str):
        conn = store.connect(self.coll["name"], readonly=True)
        try:
            v = conn.execute("SELECT heavy FROM files WHERE path=?", (rel,)).fetchone()[0]
            return json.loads(v) if v else None
        finally:
            conn.close()

    def test_marks_every_type_and_reports_the_counts(self):
        res = indexer.mark_reprocess(self.coll["name"], {"pdf", "image", "audio", "video"})
        self.assertEqual(res["by_type"], {"audio": 1, "image": 1, "pdf": 2, "video": 1})
        self.assertEqual(res["marked"], 5)
        self.assertEqual(res["nothing_to_redo"], 1)       # textonly.pdf has no OCR and no page image
        self.assertEqual(res["already_queued"], 1)        # queued.pdf is already waiting
        self.assertIsNone(self._heavy("meta.jpg"))        # metadata-only: never marked
        self.assertIsNone(self._heavy("note.txt"))        # text is not a re-OCR type

    def test_a_pdf_is_redone_on_exactly_the_pages_it_has(self):
        indexer.mark_reprocess(self.coll["name"], {"pdf"})
        spec = self._heavy("scan.pdf")
        self.assertEqual(spec["ocr"], [2, 5])
        self.assertEqual(spec["visual"], [1])
        self.assertIn("mark", spec)

    def test_old_ocr_and_transcripts_stay_in_place(self):
        before = self._items_total()
        indexer.mark_reprocess(self.coll["name"], {"pdf", "image", "audio", "video"})
        self.assertEqual(self._items_total(), before)

    def test_the_folder_prefix_limits_the_marks(self):
        res = indexer.mark_reprocess(self.coll["name"], {"pdf"}, prefix="sub")
        self.assertEqual(res["by_type"], {"pdf": 1})
        self.assertIsNotNone(self._heavy("sub/scan2.pdf"))
        self.assertIsNone(self._heavy("scan.pdf"))

    def test_only_the_named_types_are_marked(self):
        res = indexer.mark_reprocess(self.coll["name"], {"audio"})
        self.assertEqual(res["by_type"], {"audio": 1})
        self.assertIsNone(self._heavy("scan.pdf"))

    def test_a_mark_written_during_a_read_is_not_cleared_by_it(self):
        indexer.mark_reprocess(self.coll["name"], {"pdf"})
        started = self._heavy("scan.pdf")
        r = {"id": self.fid["scan.pdf"], "path": "scan.pdf", "heavy": json.dumps(started)}
        newer = dict(started, mark=started["mark"] + 60)          # a second mark, written while the read ran
        conn = store.connect(self.coll["name"])
        conn.execute("UPDATE files SET heavy=? WHERE id=?", (json.dumps(newer), self.fid["scan.pdf"]))
        conn.commit()
        conn.close()
        indexer._store_heavy(self.coll["name"], r, {"items": [], "thumb": False, "duration": None}, None)
        self.assertEqual(self._heavy("scan.pdf")["mark"], newer["mark"])     # still queued

    def test_a_read_clears_its_own_spec(self):
        indexer.mark_reprocess(self.coll["name"], {"pdf"})
        current = self._heavy("scan.pdf")
        r = {"id": self.fid["scan.pdf"], "path": "scan.pdf", "heavy": json.dumps(current)}
        indexer._store_heavy(self.coll["name"], r, {"items": [], "thumb": False, "duration": None}, None)
        self.assertIsNone(self._heavy("scan.pdf"))

    def test_the_cli_marks_without_a_daemon_and_reads_nothing(self):
        cfg = {"collections": [self.coll]}
        args = argparse.Namespace(collection=self.coll["name"], path=None, reocr=True, retranscribe=False,
                                  types=None, now=False, kind="sync", wait=False)
        with mock.patch.object(config, "load", return_value=cfg), \
                mock.patch.object(config, "scope_tokens", return_value=[self.coll["name"]]), \
                mock.patch.object(config, "resolve_scope", return_value={"collections": [self.coll["name"]]}), \
                mock.patch.object(config, "get_collection", return_value=self.coll), \
                mock.patch.object(cli, "daemon_up", side_effect=AssertionError("no daemon needed")), \
                mock.patch.object(extract, "heavy", side_effect=AssertionError("nothing is read")):
            cli.cmd_sync(args)
        self.assertIsNotNone(self._heavy("scan.pdf"))
        self.assertIsNotNone(self._heavy("pic.jpg"))
        self.assertIsNone(self._heavy("song.m4a"))        # --reocr does not touch audio


if __name__ == "__main__":
    unittest.main()
