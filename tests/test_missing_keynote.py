"""Keynote decks in the whole-Mac name index (macindex), the watcher's live routing and the Ask folder tree. A deck is a
folder named *.key, or a file over KEYNOTE_MIN_BYTES; a smaller .key is a private key and stays excluded everywhere. Every
other secret rule still applies to a deck. Only temporary folders and a temporary data folder are used.
Run: .venv/bin/python -m unittest -v tests.test_missing_keynote"""

import os
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import config, indexer, macindex, tree, watcher  # noqa: E402
from tests import tmpdirs  # noqa: E402


def _home() -> Path:
    """A fake home folder with a deck, a private key, a big .key, a deck the secret rules exclude, and a note."""
    # a fake home must be somewhere the rules would index (the default macOS $TMPDIR is not; tests/tmpdirs.py)
    home = Path(tmpdirs.mkdtemp(prefix="mse-kn-home-", indexable=True))
    work = home / "Work"
    work.mkdir()
    (work / "Talk.key").mkdir()
    (work / "Talk.key" / "Index.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    (work / "Talk.key" / "Sub").mkdir()
    (work / "Small.key").write_bytes(b"-----BEGIN PRIVATE KEY-----\n" + b"x" * 100)
    (work / "Exact.key").write_bytes(b"k" * (64 * 1024))                    # exactly the limit: still a secret
    (work / "Big.key").write_bytes(b"z" * (70 * 1024))                      # over the limit: a deck file
    (work / "secret-plans.key").mkdir()                                     # **/*secret* excludes it
    (work / "notes.txt").write_text("hello")
    return home


def _coll(home: Path) -> dict:
    return {"name": "t" + uuid.uuid4().hex[:10], "path": str(home), "enabled": True, "media": {},
            "exclude": [], "metadata_only": []}


class KeynoteInTheMacIndex(unittest.TestCase):
    def setUp(self):
        self.home = _home()
        self.work = self.home / "Work"
        self.excl = macindex.Excluder(h=str(self.home))

    def p(self, rel: str) -> str:
        return str(self.work / rel) if rel else str(self.work)

    def test_a_deck_folder_is_not_blocked(self):
        self.assertFalse(self.excl.blocked(self.p("Talk.key")))

    def test_a_deck_file_over_the_limit_is_not_blocked(self):
        self.assertFalse(self.excl.blocked(self.p("Big.key")))

    def test_what_is_inside_a_deck_is_blocked(self):
        self.assertTrue(self.excl.blocked(self.p("Talk.key/Index.zip")))
        self.assertTrue(self.excl.blocked(self.p("Talk.key/Sub")))

    def test_a_private_key_and_a_key_at_the_limit_stay_blocked(self):
        self.assertTrue(self.excl.blocked(self.p("Small.key")))
        self.assertTrue(self.excl.blocked(self.p("Exact.key")))

    def test_other_secret_rules_still_block_a_deck(self):
        self.assertTrue(self.excl.blocked(self.p("secret-plans.key")))

    def test_a_gone_key_is_blocked_for_ingest_but_let_through_for_removal(self):
        gone = self.p("Removed.key")
        self.assertTrue(self.excl.blocked(gone))
        self.assertTrue(macindex.gone_keynote(gone))
        self.assertFalse(macindex.gone_keynote(self.p("notes.txt")))
        self.assertFalse(macindex.gone_keynote(self.p("Talk.key/Index.zip")))

    def test_a_key_the_user_configured_still_blocks_decks(self):
        excl = macindex.Excluder(cfg={"exclude": ["*.key"]}, h=str(self.home))
        self.assertTrue(excl.blocked(self.p("Talk.key")))

    def test_the_crawl_still_skips_key_names(self):
        args = self.excl.helper_args()
        pairs = list(zip(args, args[1:]))
        self.assertIn(("--skip-name", "*.key"), pairs)

    def test_the_rule_is_decided_per_path_not_by_the_pattern_list(self):
        self.assertIn("*.key", self.excl.patterns)               # the built-in rule is still listed (crawl, purge hash)
        self.assertIsNotNone(macindex.keynote_verdict(self.p("Talk.key")))
        self.assertIsNone(macindex.keynote_verdict(self.p("notes.txt")))


class KeynoteRemovalInTheIndex(unittest.TestCase):
    """A sweep does not delete decks the crawl cannot see, and a deleted deck's event removes its rows."""

    def setUp(self):
        self.home = _home()
        self.work = self.home / "Work"
        self.store = macindex.Store(Path(tempfile.mkdtemp(prefix="mse-kn-db-")) / "m.db")
        self.excl = macindex.Excluder(h=str(self.home))

    def _names(self) -> set[str]:
        c = sqlite3.connect(self.store.path)
        try:
            return {Path(r[0]).name for r in c.execute("SELECT path FROM files")}
        finally:
            c.close()

    def test_a_sweep_keeps_decks_and_removes_the_rest(self):
        self.store.upsert([macindex.make_row(str(self.work / "notes.txt"), False, 5, 1, 1, 1),
                           macindex.make_row(str(self.work / "Talk.key"), True, 0, 1, 1, 1)])
        self.store.sweep(str(self.work), 2)                       # a crawl that did not list either
        self.assertEqual(self._names(), {"Talk.key"})

    def test_a_deleted_deck_removes_its_rows_through_apply_events(self):
        self.store.upsert([macindex.make_row(str(self.work / "Talk.key"), True, 0, 1, 1, 1)])
        self.assertEqual(self._names(), {"Talk.key"})
        shutil.rmtree(self.work / "Talk.key")
        res = macindex.apply_events(self.store, {str(self.work / "Talk.key"): macindex.F_REMOVED}, self.excl)
        self.assertEqual(res["deleted"], 1)
        self.assertEqual(self._names(), set())

    def test_an_existing_small_key_is_never_stored(self):
        res = macindex.apply_events(self.store, {str(self.work / "Small.key"): 0}, self.excl)
        self.assertEqual(res["upserted"], 0)
        self.assertEqual(self._names(), set())


class KeynoteInTheWatcher(unittest.TestCase):
    def setUp(self):
        self.home = _home()
        coll = _coll(self.home)
        self.root = Path(coll["path"])
        self.exclude, _ = indexer.matchers(coll)
        self.nokey = indexer._nokey_matcher(coll)

    def target(self, rel: str):
        return watcher.job_target(self.root, rel, self.exclude, self.nokey)

    def test_an_existing_deck_folder_is_queued(self):
        self.assertEqual(self.target("Work/Talk.key"), "Work/Talk.key")

    def test_an_existing_deck_file_over_the_limit_is_queued(self):
        self.assertEqual(self.target("Work/Big.key"), "Work/Big.key")

    def test_a_private_key_is_dropped(self):
        self.assertIsNone(self.target("Work/Small.key"))
        self.assertIsNone(self.target("Work/Exact.key"))

    def test_a_change_inside_a_deck_goes_to_the_deck(self):
        self.assertEqual(self.target("Work/Talk.key/Index.zip"), "Work/Talk.key")
        self.assertEqual(self.target("Work/Talk.key/Sub/deep.txt"), "Work/Talk.key")

    def test_a_change_inside_an_excluded_deck_is_still_dropped(self):
        self.assertIsNone(self.target("Work/secret-plans.key/x.txt"))

    def test_other_secret_rules_still_drop_a_deck(self):
        self.assertIsNone(self.target("Work/secret-plans.key"))

    def test_a_deleted_deck_sends_its_parent_folder_so_the_sweep_marks_it(self):
        held = lambda rel: True                                 # the index holds a row for it: a deck
        self.assertEqual(watcher.job_target(self.root, "Work/Removed.key", self.exclude, self.nokey, held), "Work")

    def test_a_deleted_root_level_deck_asks_for_a_full_scan(self):
        held = lambda rel: True
        self.assertEqual(watcher.job_target(self.root, "Removed.key", self.exclude, self.nokey, held), "")

    def test_a_deleted_key_the_index_does_not_hold_causes_no_work(self):
        nothing = lambda rel: False                             # a rotated private key: no row, nothing to sweep
        self.assertIsNone(watcher.job_target(self.root, "Work/Rotated.key", self.exclude, self.nokey, nothing))
        self.assertIsNone(watcher.job_target(self.root, "Rotated.key", self.exclude, self.nokey, nothing))

    def test_ordinary_files_and_hidden_or_excluded_paths_are_unchanged(self):
        self.assertEqual(self.target("Work/notes.txt"), "Work/notes.txt")
        self.assertIsNone(self.target("Work/.hidden"))
        self.assertIsNone(self.target("node_modules/x.js"))


class KeynoteInTheTree(unittest.TestCase):
    """GET /api/tree (the Ask folder picker): a deck shows as one folder, and nothing inside it is listed."""

    def setUp(self):
        self.home = _home()
        self.coll = _coll(self.home)
        cfg = config.load()
        cfg["collections"] = [self.coll]
        self.cfg = cfg

    def _folders(self, rel: str) -> list[str]:
        excl = macindex.Excluder(h=str(self.home))

        class _Mgr:
            def excluder(self):
                return excl

        with mock.patch.object(config, "load", lambda: self.cfg), \
                mock.patch.object(macindex, "manager", lambda: _Mgr()):
            return [f["name"] for f in tree.folders(self.coll["name"], rel)["folders"]]

    def test_a_deck_is_listed_as_a_folder(self):
        self.assertIn("Talk.key", self._folders("Work"))

    def test_a_secret_named_deck_is_not_listed(self):
        self.assertNotIn("secret-plans.key", self._folders("Work"))

    def test_nothing_inside_a_deck_is_listed(self):
        self.assertEqual(self._folders("Work/Talk.key"), [])


if __name__ == "__main__":
    unittest.main()
