"""The whole-disk search (mse/wholedisk.py): the query language (type words, stop words, EN<->DE concepts), the ranking
(labels before names before text, AND before OR), paging, the `why` of each result, the API contract shape, and the
routes (GET /api/disk/status, GET /api/disk/search, POST /api/disk/rescan). Runs on temporary trees and fake descriptions,
as test_wholedisk.py does; nothing here reads the owner's files."""

import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import config, server, wholedisk as wd  # noqa: E402
from tests.test_wholedisk import BASE, OLD, _configure, _crawl, _manager, _root, _write, isolate  # noqa: E402

RESULT_KEYS = {"path", "name", "kind", "size", "mtime", "snippet", "labels", "score", "why"}
STATUS_KEYS = {"enabled", "roots", "files_seen", "files_described", "images_labelled", "pdfs_described", "pending",
               "state", "paused_reason", "rate_per_s", "eta_s", "last_error", "priority"}


def _describer(labels_by_name):
    """Labels per file name (the stand-in for the helper); files not listed get no labels."""
    def describe(path, dkind, ext, cfg):
        labels = labels_by_name.get(os.path.basename(path), [])
        return {"text": "", "title": "", "labels": [(lab, 0.9) for lab in labels], "lang": "", "lines": 0,
                "ocr": False}
    return describe


class ParserTests(unittest.TestCase):
    def test_type_words_become_a_kind_and_are_not_searched(self):
        p = wd.parse("image of a zebra")
        self.assertEqual(p.groups, [("zebra",)])
        self.assertEqual(p.kinds, frozenset({"image"}))

    def test_german_type_words(self):
        self.assertEqual(wd.parse("Bild Hund").kinds, frozenset({"image"}))
        self.assertEqual(wd.parse("Fotos vom Strand").kinds, frozenset({"image"}))
        self.assertIn("sand", wd.parse("Fotos vom Strand").groups[0])

    def test_pdf_and_document_words(self):
        self.assertEqual(wd.parse("pdf").kinds, frozenset({"pdf"}))
        self.assertEqual(wd.parse("pdf document").kinds, frozenset({"pdf", "doc"}))
        self.assertEqual(wd.parse("Dokument").kinds, frozenset({"doc"}))

    def test_stop_words_only_leave_nothing_to_search(self):
        p = wd.parse("the file of a")
        self.assertEqual(p.groups, [])
        self.assertEqual(p.kinds, frozenset())

    def test_english_and_german_concepts_meet(self):
        en = wd.parse("invoice")
        de = wd.parse("Rechnung")
        self.assertEqual(en.groups, de.groups)
        self.assertIn("invoice", en.groups[0])
        self.assertIn("rechnung", en.groups[0])

    def test_folding_makes_case_and_accents_equal(self):
        self.assertEqual(wd.parse("Hund").groups, wd.parse("hund").groups)
        self.assertEqual(wd.parse("Strände").groups, wd.parse("strande").groups)

    def test_two_content_words_are_two_groups(self):
        p = wd.parse("Rechnung Telekom")
        self.assertEqual(len(p.groups), 2)
        self.assertIn("telekom", p.groups[1])

    def test_match_expression_quotes_each_alternative(self):
        expr = wd._match_expr([("dog", "hund")], "and")
        self.assertEqual(expr, '("dog"* OR "hund"*)')
        self.assertEqual(wd._match_expr([("a",), ("b",)], "or"), '("a"*) OR ("b"*)')


class RankingAndPagingTests(unittest.TestCase):
    def setUp(self):
        isolate(self.addCleanup)
        self.root = _root()
        _configure(self.root)
        t = self.root
        _write(f"{t}/Documents/photos/zebra_a.jpg", b"\xff\xd8 a")
        _write(f"{t}/Documents/photos/zebra_b.jpg", b"\xff\xd8 b")
        _write(f"{t}/Documents/photos/animal_c.jpg", b"\xff\xd8 c")
        _write(f"{t}/Documents/book/zebra_book.txt", "A book about zebra stripes and their herds.\n")
        _write(f"{t}/Documents/a.txt", "Telekom invoice 4711 for the line.\n")
        _write(f"{t}/Documents/b.txt", "An invoice from acme, no carrier named here.\n")
        _crawl(t)
        self.m = _manager(describer=_describer({"zebra_a.jpg": ["zebra", "animal"],
                                                 "zebra_b.jpg": ["zebra", "animal"],
                                                 "animal_c.jpg": ["animal", "dog"]}))
        self.m.sync(full=True)
        self.m.describe_pending()

    def test_a_label_outranks_a_mention_in_text(self):
        res = self.m.search("zebra", limit=10)
        names = [os.path.basename(r["path"]) for r in res["results"]]
        self.assertIn("zebra_a.jpg", names)
        self.assertLess(names.index("zebra_a.jpg"), names.index("zebra_book.txt"))
        self.assertEqual(res["results"][0]["why"], "label")

    def test_all_words_first_then_any_word_to_fill(self):
        res = self.m.search("invoice telekom", limit=10)
        names = [os.path.basename(r["path"]) for r in res["results"]]
        self.assertEqual(names[0], "a.txt")                       # both words
        self.assertIn("b.txt", names)                             # one word: fills the page after the AND hits

    def test_kind_filter_keeps_only_that_kind(self):
        res = self.m.search("zebra", kind="doc", limit=10)
        self.assertTrue(res["results"])
        self.assertTrue(all(r["kind"] == "doc" for r in res["results"]))
        self.assertEqual(self.m.search("zebra", kind="video", limit=10)["results"], [])

    def test_paging_has_more_and_no_repeats(self):
        p1 = self.m.search("animal", limit=2, offset=0)
        self.assertEqual(len(p1["results"]), 2)
        self.assertTrue(p1["has_more"])
        self.assertEqual(p1["next_offset"], 2)
        p2 = self.m.search("animal", limit=2, offset=p1["next_offset"])
        paths1 = {r["path"] for r in p1["results"]}
        paths2 = {r["path"] for r in p2["results"]}
        self.assertFalse(paths1 & paths2)
        full = self.m.search("animal", limit=50)
        self.assertFalse(full["has_more"])
        self.assertIsNone(full["next_offset"])

    def test_why_names_the_match(self):
        by_name = self.m.search("book", limit=5)
        self.assertEqual(by_name["results"][0]["why"], "name")
        by_text = self.m.search("herds", limit=5)
        self.assertEqual(by_text["results"][0]["why"], "text")
        self.assertIn("herds", by_text["results"][0]["snippet"])

    def test_empty_and_stop_word_queries_return_nothing(self):
        self.assertEqual(self.m.search("", limit=5)["results"], [])
        self.assertEqual(self.m.search("the of a", limit=5)["results"], [])

    def test_kind_only_query_lists_newest_first(self):
        res = self.m.search("pdf", limit=5)
        self.assertEqual(res["results"], [])                      # no PDFs in this tree
        res = self.m.search("bild", limit=5)
        self.assertTrue(res["results"])
        self.assertTrue(all(r["kind"] == "image" for r in res["results"]))

    def test_invalid_kind_and_limits_are_handled(self):
        with self.assertRaises(ValueError):
            self.m.search("zebra", kind="spreadsheet")
        res = self.m.search("zebra", limit=10_000)
        self.assertLessEqual(len(res["results"]), 100)
        res = self.m.search("zebra", limit=-5, offset=-3)
        self.assertGreaterEqual(len(res["results"]), 0)


class ContractTests(unittest.TestCase):
    def setUp(self):
        isolate(self.addCleanup)
        self.root = _root()
        _configure(self.root)
        _write(f"{self.root}/Documents/photos/zebra.jpg", b"\xff\xd8 z")
        _write(f"{self.root}/Documents/notes.txt", "zebra in a note\n")
        _crawl(self.root)
        self.m = _manager(describer=_describer({"zebra.jpg": ["zebra"]}))
        self.m.sync(full=True)
        self.m.describe_pending()

    def test_status_has_the_contract_fields(self):
        st = self.m.status()
        self.assertTrue(STATUS_KEYS <= set(st), STATUS_KEYS - set(st))
        self.assertIn(st["state"], {"idle", "scanning", "describing", "paused"})
        self.assertIsInstance(st["files_seen"], int)
        self.assertIsInstance(st["roots"], list)

    def test_disabled_reports_paused_with_reason_disabled(self):
        config.update_settings({"wholedisk": {"enabled": False}})
        try:
            st = self.m.status()
            self.assertEqual((st["state"], st["paused_reason"]), ("paused", "disabled"))
        finally:
            config.update_settings({"wholedisk": {"enabled": True}})

    def test_result_items_have_the_contract_fields(self):
        res = self.m.search("zebra", limit=5)
        self.assertTrue(res["results"])
        for r in res["results"]:
            self.assertTrue(RESULT_KEYS <= set(r), RESULT_KEYS - set(r))
            self.assertIn(r["why"], {"name", "label", "text", "vector"})
            self.assertIn(r["kind"], wd.KINDS)
            self.assertTrue(all(isinstance(x, str) for x in r["labels"]))
            self.assertIsInstance(r["score"], float)
        self.assertIsInstance(res["took_ms"], float)
        self.assertIsInstance(res["has_more"], bool)
        self.assertIn("words", res["query"])

    def test_the_routes_exist_and_the_mcp_server_does_not_expose_them(self):
        for name in ("get_api_disk_status", "get_api_disk_search", "post_api_disk_rescan"):
            self.assertTrue(callable(getattr(server.Handler, name, None)), name)
        from mse import mcp
        with open(mcp.__file__, encoding="utf-8") as f:
            self.assertNotIn("wholedisk", f.read())

    def test_rescan_is_accepted_and_runs_a_full_merge(self):
        self.assertEqual(self.m.rescan(), {"started": True})
        self.assertTrue(self.m._rescan)

    def test_settings_keys_are_in_the_read_back_of_the_settings_route(self):
        import inspect
        src = inspect.getsource(server.Handler.get_api_settings)
        self.assertIn('"wholedisk"', src)


class ImmediateExclusionTests(unittest.TestCase):
    """A result is checked against the privacy rules at search time: a folder excluded now disappears now."""

    def test_privacy_rule_changed_after_indexing_hides_the_result(self):
        isolate(self.addCleanup)
        root = _root()
        _configure(root)
        _write(f"{root}/Documents/photos/zebra.jpg", b"\xff\xd8 z")
        _crawl(root)
        m = _manager(describer=_describer({"zebra.jpg": ["zebra"]}))
        m.sync(full=True)
        m.describe_pending()
        self.assertTrue(m.search("zebra", limit=5)["results"])
        config.update_settings({"wholedisk": {"extra_excludes": ["photos"]}})
        try:
            self.assertEqual(m.search("zebra", limit=5)["results"], [])
        finally:
            config.update_settings({"wholedisk": {"extra_excludes": []}})



class CliTests(unittest.TestCase):
    """`mse disk` with the service marked down: it reads the index itself, and refuses under MSE_LOCK. No port is used."""

    def setUp(self):
        isolate(self.addCleanup)
        self.root = _root()
        _configure(self.root)
        _write(f"{self.root}/Documents/photos/zebra.jpg", b"\xff\xd8 z")
        _crawl(self.root)
        self.db = os.path.join(tempfile.mkdtemp(prefix="wd-cli-", dir=BASE), "wholedisk.db")

    def _args(self, action, words=(), **kw):
        import argparse
        base = dict(action=action, words=list(words), kind=None, limit=20, offset=None, json=False)
        return argparse.Namespace(**{**base, **kw})

    def test_status_and_search_offline(self):
        import io
        from contextlib import redirect_stdout
        from mse import cli
        m = _manager(describer=_describer({"zebra.jpg": ["zebra"]}))
        m.sync(full=True)
        m.describe_pending()
        with mock.patch.object(wd, "manager", return_value=m), mock.patch.object(cli, "daemon_up", return_value=False):
            out = io.StringIO()
            with redirect_stdout(out):
                cli.cmd_disk(self._args("status"))
            self.assertIn("whole-disk index", out.getvalue())
            out = io.StringIO()
            with redirect_stdout(out):
                cli.cmd_disk(self._args("search", ["image", "of", "a", "zebra"]))
            self.assertIn("zebra.jpg", out.getvalue())
            with self.assertRaises(SystemExit):
                cli.cmd_disk(self._args("search", []))

    def test_refused_under_mse_lock(self):
        from mse import cli
        with mock.patch.object(cli, "_files_locked", return_value=SimpleNamespace(name="work")):
            with self.assertRaises(SystemExit) as ctx:
                cli.cmd_disk(self._args("status"))
            self.assertIn("MSE_LOCK", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
