"""Matching rules of the whole-Mac name search, ported from fff's ideas: smart case, file name above folder path, glob
constraints, opened-files ranking, .gitignore awareness; and the pages and the scan budget of the speed work.
Everything runs on temporary folders and temporary databases.
Run: .venv/bin/python -m unittest tests.test_files_rules"""

import json
import os
import random
import re
import tempfile
import threading
import time
import unicodedata
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import filequery as fq, frecency, gitignore, macindex as mi  # noqa: E402
from tests.test_files_speed import HOME, NOW, Idx, corpus, fake, mkdtemp  # noqa: E402


# ====================================================================== smart case

class SmartCase(unittest.TestCase):
    ROWS = [fake(f"{HOME}/a/report.txt"), fake(f"{HOME}/a/Report.txt"), fake(f"{HOME}/a/REPORT.txt"),
            fake(f"{HOME}/a/annual-Report-2025.pdf"), fake(f"{HOME}/a/iPhone notes.md"), fake(f"{HOME}/a/iphone old.md"),
            fake(f"{HOME}/a/README.md"), fake(f"{HOME}/a/readme.txt"), fake(f"{HOME}/a/Draft.doc"), fake(f"{HOME}/a/draft.doc.bak")]

    @classmethod
    def setUpClass(cls):
        cls.idx = Idx(cls.ROWS)

    def names(self, q, **kw):
        return sorted(self.idx.names(q, **kw))

    def test_the_rule(self):
        self.assertTrue(fq.is_mixed_case("Report"))
        self.assertTrue(fq.is_mixed_case("iPhone"))
        self.assertTrue(fq.is_mixed_case("Café"))
        for w in ("report", "REPORT", "PDF", "A", "2025", "re-port", "ÜBERSICHT", "über"):
            self.assertFalse(fq.is_mixed_case(w), w)

    def test_all_small_letters_match_any_case(self):
        self.assertEqual(self.names("report"), ["REPORT.txt", "Report.txt", "annual-Report-2025.pdf", "report.txt"])
        self.assertEqual(self.names("readme"), ["README.md", "readme.txt"])

    def test_a_word_with_a_capital_and_a_small_letter_is_matched_with_its_case(self):
        self.assertEqual(self.names("Report"), ["Report.txt", "annual-Report-2025.pdf"])
        self.assertEqual(self.names("iPhone"), ["iPhone notes.md"])
        self.assertEqual(self.names("Draft"), ["Draft.doc"])

    def test_all_capitals_stay_case_insensitive(self):
        self.assertEqual(self.names("REPORT"), ["REPORT.txt", "Report.txt", "annual-Report-2025.pdf", "report.txt"])
        self.assertEqual(self.names("README"), ["README.md", "readme.txt"])

    def test_the_other_word_modes_follow_the_rule(self):
        self.assertEqual(self.names("^Report"), ["Report.txt"])
        self.assertEqual(self.names("^report"), ["REPORT.txt", "Report.txt", "report.txt"])
        self.assertEqual(self.names("Draft$"), [])
        self.assertEqual(self.names("^Draft.doc$"), ["Draft.doc"])
        self.assertEqual(self.names("'Report"), ["Report.txt", "annual-Report-2025.pdf"])
        self.assertEqual(self.names("Report 2025"), ["annual-Report-2025.pdf"])

    def test_a_negated_word_with_a_capital_excludes_only_that_case(self):
        self.assertEqual(self.names("draft !Draft"), ["draft.doc.bak"])
        self.assertEqual(self.names("draft !draft"), [])

    def test_typos_are_looked_for_in_any_case_but_only_for_words_without_the_rule(self):
        i = Idx([fake(f"{HOME}/a/receive.txt")])
        self.assertEqual(i.names("recieve"), ["receive.txt"])
        self.assertEqual(i.names("Recieve"), [])                          # typed with its case: no guessing

    def test_composed_and_decomposed_names_agree(self):
        nfd = unicodedata.normalize("NFD", "Café Menü.docx")
        i = Idx([fake(f"{HOME}/a/{nfd}"), fake(f"{HOME}/a/cafe latte.txt")])
        self.assertEqual(i.names("Café"), [nfd])
        self.assertEqual(i.names(unicodedata.normalize("NFD", "Café")), [nfd])
        self.assertEqual(sorted(i.names("cafe")), sorted([nfd, "cafe latte.txt"]))

    def test_the_case_test_runs_inside_the_candidate_window(self):
        """A word more common in lower case than the window is wide must still find every capitalised name."""
        rows = [fake(f"{HOME}/a/report_{i}.txt") for i in range(300)] + [fake(f"{HOME}/z/Report_{i}.txt") for i in range(5)]
        i = Idx(rows)
        with mock.patch.object(fq, "INNER_CAP", 40), mock.patch.object(fq, "DEEP_CAP", 40):
            got = i.names("Report", limit=100)
        self.assertEqual(sorted(got), [f"Report_{k}.txt" for k in range(5)])

    def test_the_setting_turns_it_off(self):
        q = fq.parse_query("Report", now=NOW, home=HOME, smart_case=False)
        self.assertFalse(q.words[0].cs)
        with self.idx.store.read() as c:
            names = sorted(i["name"] for i in fq.search(c, q, 50, now=NOW, fts=self.idx.store.fts_name())["items"])
        self.assertEqual(names, ["REPORT.txt", "Report.txt", "annual-Report-2025.pdf", "report.txt"])

    def test_the_query_description_says_which_words_are_case_sensitive(self):
        pub = fq.parse_query("Report pdf", now=NOW, home=HOME).public()
        self.assertEqual(pub["words"][0], {"text": "Report", "mode": "fuzzy", "not": False, "case": "sensitive"})
        self.assertEqual(pub["words"][1], {"text": "pdf", "mode": "fuzzy", "not": False})


# ====================================================================== globs

class Globs(unittest.TestCase):
    ROWS = [fake(f"{HOME}/Work/Clients/Acme/invoices/2025-01.pdf"), fake(f"{HOME}/Work/Clients/Acme/invoices/2025-02.pdf"),
            fake(f"{HOME}/Work/Clients/Acme/invoices/old/2024-12.pdf"), fake(f"{HOME}/Work/Clients/Acme/report-q1.docx"),
            fake(f"{HOME}/Work/Clients/Acme/report-q2.docx"), fake(f"{HOME}/Work/Clients/Acme/report.docx"),
            fake(f"{HOME}/Work/src/app/main.py"), fake(f"{HOME}/Work/src/app/util/helpers.py"), fake(f"{HOME}/Work/src/tests/test_main.py"),
            fake(f"{HOME}/Work/docs/readme.md"), fake(f"{HOME}/Work/docs/guide/intro.md"), fake(f"{HOME}/Work/server.log"),
            fake(f"{HOME}/Work/debug.LOG"), fake(f"{HOME}/Work/notes*star.txt"), fake(f"{HOME}/Work/a[1].txt"),
            fake(f"{HOME}/Work/ab.txt"), fake(f"{HOME}/Work/invoices", is_dir=True), fake(f"{HOME}/Work/x.tar.gz"),
            fake(f"{HOME}/Pictures/old/invoices/scan.png")]

    @classmethod
    def setUpClass(cls):
        cls.idx = Idx(cls.ROWS)

    def rel(self, q, **kw):
        return sorted(p.replace(HOME + "/", "") for p in self.idx.paths(q, limit=100, **kw))

    def test_star_dot_extension_is_the_extension_filter(self):
        self.assertEqual(self.rel("*.pdf"), self.rel("ext:pdf"))
        self.assertEqual(self.rel("*.PDF"), self.rel("ext:pdf"))
        q = fq.parse_query("*.pdf !*.log", now=NOW, home=HOME)
        self.assertEqual((q.ext, q.not_ext, q.globs), (["pdf"], ["log"], []))
        self.assertEqual(self.rel("report !*.docx"), [])

    def test_a_name_glob_matches_the_whole_name(self):
        self.assertEqual(self.rel("report-*.docx"), ["Work/Clients/Acme/report-q1.docx", "Work/Clients/Acme/report-q2.docx"])
        self.assertEqual(self.rel("*port*.docx"), ["Work/Clients/Acme/report-q1.docx", "Work/Clients/Acme/report-q2.docx",
                                                    "Work/Clients/Acme/report.docx"])
        self.assertEqual(self.rel("test_*.py"), ["Work/src/tests/test_main.py"])
        self.assertEqual(self.rel("*.tar.gz"), ["Work/x.tar.gz"])               # more than an extension
        self.assertEqual(self.rel("m*n.py"), ["Work/src/app/main.py"])
        self.assertEqual(self.rel("a?*.txt"), ["Work/a[1].txt", "Work/ab.txt"])        # `?` is a wildcard once the token has a `*`
        self.assertEqual(self.rel("a?.txt"), [])                                       # without a `*` it is plain text

    def test_globs_ignore_case(self):
        self.assertEqual(self.rel("debug*"), ["Work/debug.LOG"])
        self.assertEqual(self.rel("DEBUG*"), ["Work/debug.LOG"])
        self.assertEqual(self.rel("*.log"), ["Work/debug.LOG", "Work/server.log"])

    def test_a_path_glob_is_matched_against_the_whole_path(self):
        self.assertEqual(self.rel("**/invoices/*"), ["Pictures/old/invoices/scan.png", "Work/Clients/Acme/invoices/2025-01.pdf",
                                                    "Work/Clients/Acme/invoices/2025-02.pdf"])
        self.assertEqual(self.rel("invoices/*"), self.rel("**/invoices/*"))        # may start at any folder
        self.assertEqual(self.rel("**/invoices/**"), ["Pictures/old/invoices/scan.png", "Work/Clients/Acme/invoices/2025-01.pdf",
                                                      "Work/Clients/Acme/invoices/2025-02.pdf",
                                                      "Work/Clients/Acme/invoices/old/2024-12.pdf"])
        self.assertEqual(self.rel("src/**/*.py"), ["Work/src/app/main.py", "Work/src/app/util/helpers.py", "Work/src/tests/test_main.py"])
        self.assertEqual(self.rel("src/*.py"), [])                                 # one level only: nothing directly in src/
        self.assertEqual(self.rel("app/*.py"), ["Work/src/app/main.py"])
        self.assertEqual(self.rel("docs/*.md"), ["Work/docs/readme.md"])
        self.assertEqual(self.rel("docs/**/*.md"), ["Work/docs/guide/intro.md", "Work/docs/readme.md"])
        self.assertEqual(self.rel(f"{HOME}/Pictures/**/*.png"), ["Pictures/old/invoices/scan.png"])
        self.assertEqual(self.rel("~/Pictures/**/*.png"), self.rel(f"{HOME}/Pictures/**/*.png"))
        self.assertEqual(self.rel("Acme/"), self.rel("Acme/**"))                    # everything below a folder

    def test_a_path_glob_does_not_match_inside_a_name(self):
        self.assertEqual(self.rel("nvoices/*"), [])                                # needs whole folder names
        self.assertEqual(self.rel("*/invoices"), ["Work/invoices"])

    def test_excluding_with_a_bang(self):
        every_pdf = self.rel("ext:pdf")
        self.assertEqual(self.rel("ext:pdf !**/old/**"), [p for p in every_pdf if "/old/" not in p])
        self.assertEqual(self.rel("report !report-*"), ["Work/Clients/Acme/report.docx"])
        self.assertEqual(self.rel("ext:py !test_*"), ["Work/src/app/main.py", "Work/src/app/util/helpers.py"])
        self.assertEqual(self.rel("ext:md !docs/guide/*"), ["Work/docs/readme.md"])

    def test_words_and_filters_combine_with_globs(self):
        self.assertEqual(self.rel(f"report *.docx in:{HOME}/Work/Clients"), self.rel("report ext:docx"))
        self.assertEqual(self.rel(f"*.docx in:{HOME}/Pictures"), [])
        self.assertEqual(self.rel("2025 **/invoices/*"), ["Work/Clients/Acme/invoices/2025-01.pdf", "Work/Clients/Acme/invoices/2025-02.pdf"])
        self.assertEqual(self.rel("size:<10mb mtime:<400d report-*.docx"), self.rel("report-*.docx"))

    def test_quoted_exact_anchored_and_bracketed_text_stays_literal(self):
        self.assertEqual(self.rel("'notes*star"), ["Work/notes*star.txt"])
        self.assertEqual(self.rel('"notes*star"'), ["Work/notes*star.txt"])
        self.assertEqual(self.rel("a[1]"), ["Work/a[1].txt"])                      # [ is not a class
        self.assertEqual(self.rel("a[1].*"), ["Work/a[1].txt"])                    # not even next to a star
        self.assertEqual(self.rel("^notes*"), ["Work/notes*star.txt"])             # ^ keeps the old prefix syntax: the * is text
        self.assertEqual(self.rel("^notes?"), [])

    def test_a_lone_star_is_no_constraint_and_queries_without_one_are_unchanged(self):
        self.assertEqual(self.rel("report *"), self.rel("report"))
        self.assertEqual(fq.parse_query("report ext:pdf", now=NOW, home=HOME).globs, [])

    def test_a_glob_without_text_scans_in_budget_and_still_filters_exactly(self):
        got = self.rel("??*.t?t")                       # nothing literal to ask the index for
        self.assertEqual(got, ["Work/a[1].txt", "Work/ab.txt", "Work/notes*star.txt"])
        res = self.idx.search("??*.t?t")
        self.assertNotIn("partial", res)

    def test_the_query_description_lists_the_globs(self):
        pub = fq.parse_query("report *.docx !**/old/** src/*.py", now=NOW, home=HOME).public()
        self.assertEqual(pub["filters"]["ext"], ["docx"])
        self.assertEqual(pub["filters"]["glob"], ["src/*.py"])
        self.assertEqual(pub["filters"]["not"]["glob"], ["**/old/**"])

    def test_matches_a_brute_force_reference_on_random_names(self):
        """Name globs through the index agree with fnmatch-style matching written independently here."""
        r = random.Random(11)
        names = ["".join(r.choice("abcxyz-_.") for _ in range(r.randint(3, 9))) + r.choice([".txt", ".md", ""]) for _ in range(400)]
        i = Idx([fake(f"{HOME}/n/{n}_{k}") for k, n in enumerate(names)])
        pats = ["a*", "*z", "a*z", "*ab*", "x?a*", "*-*_*", "*.txt", "ab*.md", "?b*", "*a?b*", "a*b*c*"]
        for pat in pats:
            rx = re.compile("^" + "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pat) + "$", re.S)
            want = sorted(f"{n}_{k}" for k, n in enumerate(names) if rx.match(f"{n}_{k}"))
            got = sorted(x["name"] for x in i.search(pat, limit=1000)["items"])
            self.assertEqual(got, want, pat)


class GlobSafety(unittest.TestCase):
    def test_wildcards_are_bounded_and_the_error_is_a_query_error(self):
        with self.assertRaises(fq.QueryError):
            fq.parse_query("a*a*a*a*a*a*a*b", now=NOW, home=HOME)
        with self.assertRaises(fq.QueryError):
            fq.parse_query("x" * 300 + "*", now=NOW, home=HOME)
        fq.parse_query("a*a*a*a*a*a*b", now=NOW, home=HOME)                       # six is fine

    def test_a_path_glob_cannot_be_made_to_run_long(self):
        names = [f"{HOME}/" + "a" * 40 + f"/{'a' * 30}{i}.txt" for i in range(300)]
        i = Idx([fake(p) for p in names])
        t = time.time()
        self.assertEqual(i.names("**/a*a*a*a*a/zz*", limit=50), [])
        self.assertLess(time.time() - t, 2.0)

    def test_the_literal_that_folds_shorter_than_three_does_not_break_the_query(self):
        i = Idx([fake(f"{HOME}/a/\u00e1\u00e9 notes.txt"), fake(f"{HOME}/a/plain.txt")])
        decomposed = "a\u0301e\u0301*"                                          # what pasting "áé*" from Finder gives
        self.assertEqual(i.names(decomposed), ["áé notes.txt"])                  # no error (the literal "ae" is two letters)
        self.assertEqual(i.names("\u00e1\u00e9*"), ["áé notes.txt"])
        self.assertEqual(i.names("*\u00e1\u00e9*s.txt"), ["áé notes.txt"])

    def test_the_path_glob_hint_never_hides_a_non_ascii_folder(self):
        i = Idx([fake(f"{HOME}/x/Café/menu.txt"), fake(f"{HOME}/x/ÄPFEL/obst.txt"), fake(f"{HOME}/x/plain/menu.txt")])
        self.assertEqual(i.names("**/Café/*"), ["menu.txt"])
        self.assertEqual(i.names("**/ÄPFEL/*"), ["obst.txt"])
        self.assertEqual(i.names("**/ÄPFEL/*"), ["obst.txt"])

    def test_folder_words_see_accented_folders(self):
        i = Idx([fake(f"{HOME}/Clients/Café Acme/report.txt"), fake(f"{HOME}/Clients/Äpfel/notes_obst.txt")])
        self.assertEqual(i.names("report cafe", folder_words=True), ["report.txt"])
        self.assertEqual(i.names("notes apfel", folder_words=True), ["notes_obst.txt"])

    def test_regex_literals_ignore_what_a_group_may_skip(self):
        for pat, want in ((r"(?:report)_final", "_final"), (r"^(abc)*def$", "def"), (r"foo(bar)baz", "foo"),
                          (r"x(\))yyy", "yyy"), (r"a(b(c)d)efg", "efg"), (r"report(_old)?\.pdf$", "report"),
                          (r"^[a-f0-9]{32}\.ts$", ".ts"), (r"^.{3}$", None), (r"(foo|bar)", None)):
            self.assertEqual(fq._regex_literal(pat), want, pat)
        i = Idx([fake(f"{HOME}/a/report_final.pdf"), fake(f"{HOME}/a/def.txt")])
        self.assertEqual(i.names(r"re:(?:report)_final"), ["report_final.pdf"])
        self.assertEqual(i.names(r"re:^(abc)*def\."), ["def.txt"])


# ====================================================================== file name above folder path

class NameAbovePath(unittest.TestCase):
    def old_score(self, row, q, now):
        """The score before the name bonuses existed (the formula of the first version), for the order test."""
        sc = fq.score_row(row, q, {}, now)
        nf = row[2]
        tiers, cover = [], 0
        for w in q.words:
            if w.neg:
                continue
            t, span = fq._tier_of(nf, w)
            tiers.append(t)
            cover += span[1] - span[0]
        age = max(0.0, (now - (row[5] or 0)) / 86400)
        tier = min(tiers) + 0.15 * (sum(tiers) / len(tiers) - min(tiers))
        return (tier * 100 + 40 * min(1.0, cover / max(1, len(nf))) + 10 * fq.math.exp(-age / 180)
                - min(row[9], 40) * 0.8 + (15 if row[3] and min(tiers) >= 5 else 0)), sc[0]

    def test_the_bonuses_do_not_reorder_name_matches(self):
        rows = []
        r = random.Random(5)
        for i in range(600):
            d = HOME + "".join("/" + r.choice("abcdef") for _ in range(r.randint(1, 6)))
            stem = r.choice(["report", "reports", "my_report", "report_final", "Report2", "xreportx", "re-port", "annual report"])
            rows.append(fake(f"{d}/{stem}{r.choice(['', '.txt', '.pdf'])}", is_dir=r.random() < 0.1, age_days=r.randint(0, 2000)))
        idx = Idx(rows)
        with idx.store.read() as c:
            raw = c.execute(f"SELECT {fq.COLS} FROM files f").fetchall()
        for qtext in ("report", "reports", "re", "my_report", "annual report"):
            q = fq.parse_query(qtext, now=NOW, home=HOME)
            pairs = []
            for row in raw:
                sc = fq.score_row(row, q, {}, NOW)
                if sc is not None:
                    old = self.old_score(row, q, NOW)[0]
                    pairs.append((old, sc[0]))
            self.assertGreater(len(pairs), 10, qtext)
            by_old = sorted(pairs, key=lambda p: p[0])
            by_new = sorted(pairs, key=lambda p: p[1])
            self.assertEqual([p[0] for p in by_old], [p[0] for p in by_new], qtext)     # same order, strictly
            self.assertTrue(all(b[1] > a[1] for a, b in zip(by_old, by_old[1:]) if b[0] > a[0]), qtext)

    def test_the_weights(self):
        i = Idx([fake(f"{HOME}/a/main.py"), fake(f"{HOME}/a/the_main_thing.py")])
        with i.store.read() as c:
            rows = {r[1].rsplit("/", 1)[-1]: r for r in c.execute(f"SELECT {fq.COLS} FROM files f")}
        q = fq.parse_query("main", now=NOW, home=HOME)
        exact, inside = rows["main.py"], rows["the_main_thing.py"]
        base_e, new_e = self.old_score(exact, q, NOW)
        base_i, new_i = self.old_score(inside, q, NOW)
        self.assertAlmostEqual(new_e, base_e * 1.40, places=6)                       # the whole name: +40 %
        self.assertAlmostEqual(new_i, base_i + min(base_i * 0.16, 30), places=6)     # inside the name: +16 %, at most 30 points

    def test_a_typo_match_gets_no_bonus_and_stays_below_every_literal_match(self):
        i = Idx([fake(f"{HOME}/a/receive.txt"), fake(f"{HOME}/a/recieve_notes_with_long_name_so_it_is_a_substring_x.txt", age_days=900),
                 fake(f"{HOME}/a/x/y/z/w/v/u/t/recieve_deep_old_a_substring_zz.txt", age_days=3000)])
        res = i.search("recieve")["items"]
        self.assertEqual(res[-1]["name"], "receive.txt")
        self.assertLess(res[-1]["score"], min(r["score"] for r in res[:-1]))

    def folder_index(self):
        return Idx([fake(f"{HOME}/Clients/Acme/invoice-2025.pdf"), fake(f"{HOME}/Clients/Acme/notes.txt"),
                    fake(f"{HOME}/Clients/Other/invoice-acme-2024.pdf"), fake(f"{HOME}/Clients/Other/invoice-misc.pdf"),
                    fake(f"{HOME}/Clients/acme-invoice-copy.pdf")])

    def test_folder_words_are_opt_in(self):
        i = self.folder_index()
        self.assertEqual(sorted(i.names("acme invoice")), ["acme-invoice-copy.pdf", "invoice-acme-2024.pdf"])
        self.assertEqual(sorted(i.names("acme invoice", folder_words=False)), ["acme-invoice-copy.pdf", "invoice-acme-2024.pdf"])

    def test_a_word_in_the_folder_path_counts_after_every_name_match(self):
        i = self.folder_index()
        res = i.search("acme invoice", folder_words=True)["items"]
        self.assertEqual([r["path"].replace(HOME, "") for r in res][2:], ["/Clients/Acme/invoice-2025.pdf"])
        self.assertEqual(sorted(r["name"] for r in res[:2]), ["acme-invoice-copy.pdf", "invoice-acme-2024.pdf"])
        self.assertLess(res[2]["score"], min(r["score"] for r in res[:2]))
        # only the word that is in the path loses its highlight; nothing outside the name is highlighted
        self.assertEqual(res[2]["match"], [[0, 7]])
        # one word never goes to the folder path: there would be nothing to anchor the search on
        self.assertEqual(sorted(i.names("acme", folder_words=True)), ["acme-invoice-copy.pdf", "invoice-acme-2024.pdf"])

    def test_folder_words_still_obey_the_privacy_gate(self):
        i = Idx([fake(f"{HOME}/Clients/Acme/invoice-2025.pdf")], cfg={"exclude": ["~/Clients/Acme/"]})
        self.assertEqual(i.names("acme invoice", folder_words=True), [])

    def test_folder_words_only_run_when_names_come_up_short(self):
        i = self.folder_index()
        calls = []
        orig = fq._run_fts
        with mock.patch.object(fq, "_run_fts", side_effect=lambda *a, **k: (calls.append(k.get("extra")), orig(*a, **k))[1]):
            i.search("acme invoice", limit=2, folder_words=True)                # two name matches fill the limit
        self.assertTrue(all(c is None for c in calls))


# ====================================================================== pages

class Pages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = Idx(corpus(3000, seed=9))

    def paths(self, q, **kw):
        return self.idx.paths(q, **kw)

    def test_a_first_page_is_the_start_of_the_full_list(self):
        big = 0
        for q in ("report", "invoice", "co", "main", "ext:pdf", "photo", "r", "budget draft", "path:work", "*.md", "config", "re:^(r|c)"):
            full = self.paths(q, limit=200)
            big += len(full) > 60
            self.assertEqual(self.paths(q, limit=50), full[:50], q)
            self.assertEqual(self.paths(q, limit=20), full[:20], q)
            self.assertEqual(self.paths(q, limit=1), full[:1], q)
            res = self.idx.search(q, limit=150, offset=50)
            self.assertEqual([i["path"] for i in res["items"]], full[50:200], q)
            res = self.idx.search(q, limit=30, offset=40)
            self.assertEqual([i["path"] for i in res["items"]], full[40:70], q)
        self.assertGreaterEqual(big, 6)                       # most of these really have more than one page

    def test_has_more_and_next_offset(self):
        full = self.paths("report", limit=200)
        res = self.idx.search("report", limit=50, paged=True)
        self.assertEqual((res["offset"], res["limit"], res["has_more"], res["next_offset"]), (0, 50, True, 50))
        res = self.idx.search("report", limit=50, offset=len(full) - 10, paged=True)
        self.assertEqual((len(res["items"]), res["has_more"], res["next_offset"]), (10, False, None))
        res = self.idx.search("report", limit=50, offset=len(full) + 100, paged=True)
        self.assertEqual((res["items"], res["has_more"]), ([], False))
        res = self.idx.search("report", limit=len(full), paged=True)
        self.assertEqual((len(res["items"]), res["has_more"]), (len(full), False))

    def test_the_page_keys_appear_only_when_asked_for(self):
        plain = self.idx.search("report")
        self.assertEqual(set(plain), {"items", "total", "truncated", "query"})
        self.assertTrue({"offset", "limit", "has_more", "next_offset"} <= set(self.idx.search("report", paged=True)))

    def test_offset_skips_rows_the_gate_lets_through_not_rows_it_refuses(self):
        rows = [fake(f"{HOME}/a/rep_{i:02d}.txt") for i in range(30)]
        i = Idx(rows)
        blocked = {f"{HOME}/a/rep_{k:02d}.txt" for k in (1, 2, 3, 4, 5)}
        full = [p for p in i.paths("rep", limit=100)]
        gate = lambda p: p not in blocked          # noqa: E731
        q = fq.parse_query("rep", now=NOW, home=HOME)
        with i.store.read() as c:
            seen = fq.search(c, q, 100, now=NOW, allow=gate, fts=i.store.fts_name())["items"]
            first = fq.search(c, q, 10, now=NOW, allow=gate, fts=i.store.fts_name(), paged=True)
            second = fq.search(c, q, 10, now=NOW, allow=gate, fts=i.store.fts_name(), offset=10)
        self.assertEqual(len(seen), 25)
        self.assertEqual([x["path"] for x in first["items"]] + [x["path"] for x in second["items"]], [x["path"] for x in seen][:20])
        self.assertFalse(set(x["path"] for x in first["items"]) & blocked)

    def test_a_deep_page_is_planned_again_but_stays_inside_the_limit(self):
        res = self.idx.search("report", limit=300, offset=300, paged=True)
        self.assertLessEqual(len(res["items"]), 300)
        res = self.idx.search("report", limit=1000, offset=900, paged=True)
        self.assertLessEqual(len(res["items"]), 100)               # offset + limit never passes MAX_LIMIT


# ====================================================================== the scan budget

class ScanBudget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = [fake(f"{HOME}/d{i % 40}/{'file' if i % 7 else 'abc'}_{i}.txt", age_days=i % 400) for i in range(6000)]
        rows += [fake(f"{HOME}/z/{n}.txt") for n in ("abc", "xyz", "ab12", "q")]
        cls.idx = Idx(rows)

    def search(self, q, **kw):
        return self.idx.search(q, limit=50, **kw)

    def test_a_pattern_with_no_literal_scans_and_every_hit_matches(self):
        res = self.search(r"re:^[a-z]{3}\.[a-z]{3}$")
        self.assertEqual(sorted(i["name"] for i in res["items"]), ["abc.txt", "xyz.txt"])
        self.assertNotIn("partial", res)
        for pat in (r"re:^.{4}\.[a-z]{3}$", r"re:\d\d\.[a-z]{3}$", r"re:^(abc|q)\."):
            res = self.search(pat)
            rx = re.compile(pat[3:], re.I)
            self.assertTrue(res["items"], pat)
            self.assertTrue(all(rx.search(i["name"]) for i in res["items"]), pat)

    def test_the_time_budget_returns_what_it_has_and_says_so(self):
        res = self.search(r"re:^f.{3}_\d*7\d\.[a-z]{3}$", budget_ms=0)      # few matches per chunk: only the clock can stop it
        self.assertTrue(res["partial"])
        self.assertTrue(res["truncated"])
        rx = re.compile(r"^f.{3}_\d*7\d\.[a-z]{3}$", re.I)
        self.assertTrue(res["items"])
        self.assertTrue(all(rx.search(i["name"]) for i in res["items"]))
        self.assertLessEqual(res["total"], fq.SCAN_CHUNK + 600)         # one chunk past the deadline at most

    def test_it_stops_early_at_enough_matches(self):
        res = self.search(r"re:^.{4}_\d+\.[a-z]{3}$")
        self.assertTrue(res["truncated"])
        self.assertNotIn("partial", res)
        self.assertEqual(len(res["items"]), 50)

    def test_only_two_columns_leave_sqlite_and_in_chunks(self):
        seen = []
        real = fq._scan

        class Spy:
            def __init__(self, cur):
                self.cur = cur

            def fetchmany(self, n):
                seen.append(n)
                return self.cur.fetchmany(n)

            def close(self):
                self.cur.close()

        with self.idx.store.read() as c:
            class Conn:
                def __init__(self, c):
                    self.c = c

                def execute(self, sql, params=()):
                    cur = self.c.execute(sql, params)
                    if sql.startswith("SELECT f.id, f.path FROM files f"):
                        seen.append(("cols", len(cur.description)))
                        return Spy(cur)
                    return cur

                def __getattr__(self, name):
                    return getattr(self.c, name)
            q = fq.parse_query(r"re:^.{4}\.[a-z]{3}$", now=NOW, home=HOME)
            fq.search(Conn(c), q, 50, now=NOW, fts=self.idx.store.fts_name())
        self.assertIn(("cols", 2), seen)
        self.assertTrue(all(n == fq.SCAN_CHUNK for n in seen if isinstance(n, int)))

    def test_the_scan_never_returns_what_the_gate_refuses(self):
        i = Idx([fake(f"{HOME}/a/ab12.txt"), fake(f"{HOME}/a/cd34.txt")])
        q = fq.parse_query(r"re:^[a-z]{2}\d\d\.[a-z]{3}$", now=NOW, home=HOME)
        with i.store.read() as c:
            res = fq.search(c, q, 50, now=NOW, allow=lambda p: "ab12" not in p, fts=i.store.fts_name())
        self.assertEqual([x["name"] for x in res["items"]], ["cd34.txt"])

    def test_the_regex_is_tried_on_the_displayed_name_not_the_folded_one(self):
        i = Idx([fake(f"{HOME}/a/Café.txt"), fake(f"{HOME}/a/Cafe.txt")])
        self.assertEqual(i.names("re:^Café\\.[a-z]{3}$"), ["Café.txt"])               # the folded name would say "cafe"
        self.assertEqual(sorted(i.names(r"re:^caf.\.[a-z]{3}$")), ["Cafe.txt", "Café.txt"])

    def test_a_budget_setting_is_checked(self):
        self.assertEqual(mi.check_settings({"regex_budget_ms": 200}), {"regex_budget_ms": 200})
        for bad in (0, 5, 99999, "fast", True, None):
            with self.assertRaises(ValueError, msg=bad):
                mi.check_settings({"regex_budget_ms": bad})
        for k in ("smart_case", "frecency", "respect_gitignore", "match_folder_names"):
            self.assertEqual(mi.check_settings({k: True}), {k: True})
            with self.assertRaises(ValueError):
                mi.check_settings({k: "yes"})


# ====================================================================== opened files (frecency)

class FrecencyStore(unittest.TestCase):
    def fr(self, **kw):
        f = frecency.Frecency(Path(mkdtemp(prefix="mse-sp-fr-")) / "frecency.db", **kw)
        self.addCleanup(f.close)
        return f

    def test_nothing_recorded_means_nothing_to_rank_by_and_no_file(self):
        fr = self.fr()
        self.assertIsNone(fr.for_query("report"))
        self.assertFalse(fr.path.exists())                  # searching alone never creates the database
        self.assertEqual(fr.stats()["files"], 0)

    def test_score_halves_every_fourteen_days_and_grows_with_each_open(self):
        fr = self.fr()
        t0 = 1_700_000_000
        fr.record("/Users/alex/x.txt", None, 1, now=t0)
        p = fr.for_query("x", now=t0)
        self.assertAlmostEqual(p.bonus("/Users/alex/x.txt"), frecency.FRECENCY_MAX * (1 - 2.718281828 ** -0.5), places=2)
        p14 = fr.for_query("x", now=t0 + 14 * 86400)
        self.assertAlmostEqual(p14.bonus("/Users/alex/x.txt"), frecency.FRECENCY_MAX * (1 - 2.718281828 ** -0.25), places=2)
        fr.record("/Users/alex/x.txt", None, 1, now=t0)
        fr.record("/Users/alex/x.txt", None, 1, now=t0)
        self.assertGreater(fr.for_query("x", now=t0).bonus("/Users/alex/x.txt"), 25)
        self.assertLessEqual(fr.for_query("x", now=t0).bonus("/Users/alex/x.txt"), frecency.FRECENCY_MAX)
        self.assertEqual(fr.for_query("x", now=t0).bonus("/Users/alex/never-opened.txt"), 0.0)
        old = fr.for_query("x", now=t0 + 400 * 86400).bonus("/Users/alex/x.txt")
        self.assertLess(old, 0.1)                           # a year later it is gone for practical purposes

    def test_the_same_query_puts_its_file_first_and_other_queries_do_not(self):
        fr = self.fr()
        fr.record("/Users/alex/b.txt", "Rep  Final ", 7, now=1_700_000_000)
        fr.record("/Users/alex/c.txt", "rep final", 8, now=1_700_000_100)
        fr.record("/Users/alex/c.txt", "rep final", 8, now=1_700_000_200)
        p = fr.for_query("REP final", now=1_700_000_300)          # normalised: case and blanks do not matter
        self.assertEqual(sorted(p.ids), [7, 8])
        self.assertGreater(p.bonus("/Users/alex/c.txt"), frecency.HISTORY_TOP)
        self.assertGreater(p.bonus("/Users/alex/b.txt"), frecency.HISTORY_OTHER)
        self.assertLess(p.bonus("/Users/alex/b.txt"), frecency.HISTORY_TOP)
        q = fr.for_query("rep", now=1_700_000_300)                # another query: only the open count counts
        self.assertEqual(q.ids, {})
        self.assertLess(q.bonus("/Users/alex/c.txt"), frecency.FRECENCY_MAX + 1)

    def test_the_file_holds_hashes_and_never_a_path_or_a_query(self):
        fr = self.fr()
        secret_path, secret_query = "/Users/alex/Documents/divorce-lawyer-notes.txt", "divorce lawyer"
        fr.record(secret_path, secret_query, 3)
        fr.close()
        blob = b"".join(f.read_bytes() for f in fr.path.parent.iterdir() if f.name.startswith("frecency.db"))
        for needle in (b"divorce", b"lawyer", b"alex", b"Documents", b"notes.txt"):
            self.assertNotIn(needle, blob)
        self.assertEqual(oct(fr.path.stat().st_mode & 0o777), "0o600")
        # the hashes are keyed per install: another file of hashes would not know this one
        other = self.fr()
        self.assertNotEqual(other.hash_path(secret_path), frecency.Frecency(fr.path).hash_path(secret_path))
        self.assertEqual(frecency.Frecency(fr.path).hash_path(secret_path), frecency.Frecency(fr.path).hash_path(secret_path))

    def test_survives_a_restart(self):
        fr = self.fr()
        fr.record("/Users/alex/x.txt", "x", 4, now=1_700_000_000)
        fr.close()
        again = frecency.Frecency(fr.path)
        self.assertGreater(again.for_query("x", now=1_700_000_000).bonus("/Users/alex/x.txt"), 100)
        self.assertEqual(again.stats()["files"], 1)

    def test_both_tables_are_capped_and_the_least_valuable_rows_go_first(self):
        fr = self.fr()
        t0 = 1_700_000_000
        with mock.patch.object(frecency, "CAP_FILES", 50), mock.patch.object(frecency, "CAP_PAIRS", 80):
            fr.record("/Users/alex/keep-often.txt", "keep", 1, now=t0)
            for _ in range(5):
                fr.record("/Users/alex/keep-often.txt", "keep", 1, now=t0)
            for i in range(200):
                fr.record(f"/Users/alex/once-{i}.txt", f"query {i}", i + 10, now=t0 + i)
        st = fr.stats()
        self.assertLessEqual(st["files"], 50)
        self.assertLessEqual(st["pairs"], 80)
        self.assertGreater(fr.for_query("keep", now=t0 + 300).bonus("/Users/alex/keep-often.txt"), 30)    # the valuable one stayed
        self.assertEqual(fr.for_query("x", now=t0 + 300).bonus("/Users/alex/once-0.txt"), 0.0)             # the old single opens went
        again = frecency.Frecency(fr.path)
        self.assertEqual((again.stats()["files"], again.stats()["pairs"]), (st["files"], st["pairs"]))   # and on disk too

    def test_clear_forgets_everything(self):
        fr = self.fr()
        fr.record("/Users/alex/x.txt", "x", 1)
        fr.clear()
        self.assertEqual((fr.stats()["files"], fr.stats()["pairs"]), (0, 0))
        self.assertIsNone(fr.for_query("x"))
        self.assertEqual(frecency.Frecency(fr.path).stats()["files"], 0)

    def test_query_text_is_normalised_and_bounded(self):
        self.assertEqual(frecency.normalise_query("  Invoice   ACME \n"), "invoice acme")
        self.assertEqual(len(frecency.normalise_query("x" * 5000)), frecency.MAX_QUERY_CHARS)
        self.assertEqual(frecency.normalise_query(None), "")


class FrecencyRanking(unittest.TestCase):
    ROWS = [fake(f"{HOME}/a/report_final.pdf", age_days=40), fake(f"{HOME}/a/report_draft.pdf", age_days=40),
            fake(f"{HOME}/a/report.pdf", age_days=40), fake(f"{HOME}/a/the_report_old.pdf", age_days=40),
            fake(f"{HOME}/a/xreportx.pdf", age_days=40)]

    def setUp(self):
        self.idx = Idx(self.ROWS)
        self.fr = frecency.Frecency(Path(self.idx.dir) / "frecency.db")
        self.addCleanup(self.fr.close)
        self.addCleanup(self.idx.store.close)

    def ids(self):
        with self.idx.store.read() as c:
            return {r[1].rsplit("/", 1)[-1]: r[0] for r in c.execute("SELECT id, path FROM files")}

    def names(self, q, **kw):
        return self.idx.names(q, hints=self.fr.for_query(q), **kw)

    def open(self, name, q=None):
        self.fr.record(f"{HOME}/a/{name}", q, self.ids()[name])

    def test_with_nothing_opened_the_order_is_the_plain_order(self):
        plain = self.idx.names("report")
        self.assertEqual(self.names("report"), plain)

    def test_an_opened_file_moves_up_within_its_tier_but_not_over_a_better_match(self):
        plain = self.idx.names("report")
        self.assertEqual(plain[0], "report.pdf")
        self.open("xreportx.pdf")
        self.open("xreportx.pdf")
        ranked = self.names("report")
        self.assertEqual(ranked[0], "report.pdf")                        # the exact name is still first
        self.assertLessEqual(ranked.index("xreportx.pdf"), plain.index("xreportx.pdf"))        # never down
        self.assertGreaterEqual(ranked.index("xreportx.pdf"), 3)         # the substring tier is a tier below the others

    def test_the_file_opened_for_exactly_this_query_comes_first(self):
        self.open("xreportx.pdf", "report")
        self.assertEqual(self.names("report")[0], "xreportx.pdf")
        self.assertNotEqual(self.names("repor")[0], "xreportx.pdf")        # another query: no history boost
        self.assertEqual(self.names("REPORT ")[0], "xreportx.pdf")         # the same query, written differently

    def test_the_history_offers_a_file_the_window_did_not_reach(self):
        rows = [fake(f"{HOME}/b/report_{i}.txt") for i in range(200)] + [fake(f"{HOME}/z/zz_report_last.txt")]
        idx = Idx(rows)
        fr = frecency.Frecency(Path(idx.dir) / "frecency.db")
        with idx.store.read() as c:
            fid = c.execute("SELECT id FROM files WHERE path LIKE '%zz_report_last.txt'").fetchone()[0]
        fr.record(f"{HOME}/z/zz_report_last.txt", "report", fid)
        with mock.patch.object(fq, "INNER_CAP", 20), mock.patch.object(fq, "DEEP_CAP", 20), mock.patch.object(fq, "TYPO_WINDOW", 20):
            plain = idx.names("report", limit=300)
            ranked = idx.names("report", limit=300, hints=fr.for_query("report"))
        self.assertNotIn("zz_report_last.txt", plain)
        self.assertEqual(ranked[0], "zz_report_last.txt")

    def test_a_stale_id_is_not_trusted(self):
        """The id came from the index of that day. If the row at that id is another file now, nothing is offered."""
        rows = [fake(f"{HOME}/b/report_{i}.txt") for i in range(200)] + [fake(f"{HOME}/z/zz_report_last.txt")]
        idx = Idx(rows)
        fr = frecency.Frecency(Path(idx.dir) / "frecency.db")
        with idx.store.read() as c:
            other = c.execute("SELECT id FROM files WHERE path LIKE '%report_5.txt'").fetchone()[0]
        fr.record(f"{HOME}/z/zz_report_last.txt", "report", other)             # the id now belongs to report_5.txt
        with mock.patch.object(fq, "INNER_CAP", 20), mock.patch.object(fq, "DEEP_CAP", 20), mock.patch.object(fq, "TYPO_WINDOW", 20):
            ranked = idx.names("report", limit=300, hints=fr.for_query("report"))
        self.assertNotIn("zz_report_last.txt", ranked)

    def test_an_opened_file_that_is_blocked_or_filtered_out_never_comes_back(self):
        self.open("report_final.pdf", "report")
        q = fq.parse_query("report", now=NOW, home=HOME)
        with self.idx.store.read() as c:
            res = fq.search(c, q, 50, now=NOW, allow=lambda p: "report_final" not in p, fts=self.idx.store.fts_name(),
                            hints=self.fr.for_query("report"))
            self.assertNotIn("report_final.pdf", [i["name"] for i in res["items"]])
            q2 = fq.parse_query("report ext:txt", now=NOW, home=HOME)
            res = fq.search(c, q2, 50, now=NOW, fts=self.idx.store.fts_name(), hints=self.fr.for_query("report ext:txt"))
            self.assertEqual(res["items"], [])
            q3 = fq.parse_query("zzzz", now=NOW, home=HOME)                 # and a file that does not match the words stays out
            res = fq.search(c, q3, 50, now=NOW, fts=self.idx.store.fts_name(), hints=self.fr.for_query("zzzz"))
            self.assertEqual(res["items"], [])


class OpenedThroughTheManager(unittest.TestCase):
    def setUp(self):
        self.root = Path(mkdtemp(prefix="mse-sp-open-")).resolve()
        for rel in ("docs/plan.md", "docs/plan b.md", ".ssh/id_rsa", "docs/.env"):
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        self.m = mi.MacIndex(mi.Store(Path(mkdtemp(prefix="mse-sp-db-")) / "m.db"))
        self.cfg = {**mi.DEFAULT_CONFIG, "roots": [str(self.root)], "exclude": []}
        self.patch = mock.patch.object(mi, "settings", side_effect=lambda: dict(self.cfg))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.m.stop)
        self.addCleanup(self.m.store.close)
        excl = self.m.excluder()
        rows = [fake(str(self.root / "docs/plan.md")), fake(str(self.root / "docs/plan b.md"))]
        mi.ingest(self.m.store, iter(rows), self.m.store.next_gen(), excl)

    def test_a_shown_file_is_recorded_and_ranks_first_for_its_query(self):
        res = self.m.opened(str(self.root / "docs/plan b.md"), "plan")
        self.assertEqual(res, {"recorded": True})
        self.assertEqual(self.m.search("plan")["items"][0]["name"], "plan b.md")
        self.assertEqual(self.m.frecency.stats()["files"], 1)

    def test_what_the_search_would_never_show_is_refused(self):
        for bad in ("relative/path.txt", "", None, 5, "/a/../b", "\x00", "/" + "x" * 5000):
            with self.assertRaises(ValueError, msg=bad):
                self.m.opened(bad)
        with self.assertRaises(ValueError):
            self.m.opened(str(self.root / ".ssh/id_rsa"))                    # blocked by the built-in rules
        with self.assertRaises(ValueError):
            self.m.opened(str(self.root / "docs/.env"))
        with self.assertRaises(ValueError):
            self.m.opened("/tmp/definitely/outside.txt")                      # not under a searched folder
        with self.assertRaises(ValueError):
            self.m.opened(str(self.root / "docs/plan.md"), 5)
        self.assertEqual(self.m.frecency.stats()["files"], 0)
        self.assertEqual(self.m.opened(str(self.root / "docs/not-indexed.md")), {"recorded": False, "reason": "not in the name index"})

    def test_a_locked_session_in_this_process_gets_plain_order_and_records_nothing(self):
        self.m.opened(str(self.root / "docs/plan b.md"), "plan")
        with mock.patch.dict(os.environ, {"MSE_LOCK": "1"}):
            self.assertEqual(self.m.opened(str(self.root / "docs/plan.md"), "plan")["recorded"], False)
            locked = [i["name"] for i in self.m.search("plan")["items"]]
        self.assertEqual(self.m.frecency.stats()["files"], 1)
        self.assertEqual(locked, ["plan.md", "plan b.md"])                        # the plain order, not the ranked one
        self.assertEqual(self.m.search("plan")["items"][0]["name"], "plan b.md")

    def test_the_setting_switches_it_off_both_ways(self):
        self.m.opened(str(self.root / "docs/plan b.md"), "plan")
        self.cfg["frecency"] = False
        self.assertEqual(self.m.opened(str(self.root / "docs/plan.md"))["recorded"], False)
        self.assertEqual(self.m.frecency.stats()["files"], 1)                  # nothing new
        plain = [i["name"] for i in self.m.search("plan")["items"]]
        self.cfg["frecency"] = True
        self.assertEqual(self.m.search("plan")["items"][0]["name"], "plan b.md")
        self.cfg["frecency"] = False
        self.assertEqual([i["name"] for i in self.m.search("plan")["items"]], plain)


# ====================================================================== .gitignore

class GitIgnoreMatcher(unittest.TestCase):
    def repo(self, files: dict[str, str]):
        root = Path(mkdtemp(prefix="mse-sp-git-")).resolve()
        (root / ".git").mkdir()
        for rel, text in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return root

    def keeps(self, root, gi, *rels, dirs=()):
        return {r: gi.keep(str(root / r), is_dir=(r in dirs)) for r in rels}

    def test_the_gitignore_man_page_cases(self):
        root = self.repo({".gitignore": "# comment\n\n*.log\n!keep.log\nbuild/\n/top.txt\ndocs/*.tmp\n**/cache\nout/**\n"
                                        "a/**/z\n*.py[co]\nfoo?.txt\n\\#hash\n[!a-c]x.md\n",
                          "src/main.py": "", "src/x.log": "", "src/keep.log": "", "build/o.js": "", "src/build": "",
                          "top.txt": "", "src/top.txt": "", "docs/a.tmp": "", "docs/sub/a.tmp": "", "src/cache/f": "",
                          "out/f": "", "out/inner/f": "", "a/z": "", "a/b/c/z": "", "a/b/zz": "", "m.pyc": "", "m.pyo": "",
                          "m.pyx": "", "foo1.txt": "", "foo12.txt": "", "#hash": "", "dx.md": "", "ax.md": ""})
        gi = gitignore.GitIgnore()
        got = self.keeps(root, gi, "src/main.py", "src/x.log", "src/keep.log", "build/o.js", "src/build", "top.txt", "src/top.txt",
                         "docs/a.tmp", "docs/sub/a.tmp", "src/cache/f", "out/f", "out/inner/f", "a/z", "a/b/c/z", "a/b/zz",
                         "m.pyc", "m.pyo", "m.pyx", "foo1.txt", "foo12.txt", "#hash", "dx.md", "ax.md")
        want = {"src/main.py": True, "src/x.log": False, "src/keep.log": True, "build/o.js": False, "src/build": True,
                "top.txt": False, "src/top.txt": True, "docs/a.tmp": False, "docs/sub/a.tmp": True, "src/cache/f": False,
                "out/f": False, "out/inner/f": False, "a/z": False, "a/b/c/z": False, "a/b/zz": True, "m.pyc": False,
                "m.pyo": False, "m.pyx": True, "foo1.txt": False, "foo12.txt": True, "#hash": False, "dx.md": False, "ax.md": True}
        self.assertEqual(got, want)

    def test_a_folder_only_rule_does_not_hit_a_file_of_that_name(self):
        root = self.repo({".gitignore": "build/\n", "src/build": "", "build/o.js": ""})
        gi = gitignore.GitIgnore()
        self.assertTrue(gi.keep(str(root / "src/build"), is_dir=False))
        self.assertFalse(gi.keep(str(root / "src/build"), is_dir=True))
        self.assertFalse(gi.keep(str(root / "build"), is_dir=True))
        self.assertFalse(gi.keep(str(root / "build/o.js")))
        (root / "lib" / "build").mkdir(parents=True)
        self.assertFalse(gitignore.GitIgnore().keep(str(root / "lib/build")))      # a folder of that name: decided by looking at the disk

    def test_nothing_below_an_ignored_folder_can_be_re_included(self):
        root = self.repo({".gitignore": "vendor/\n!vendor/keep.txt\n", "vendor/keep.txt": ""})
        self.assertFalse(gitignore.GitIgnore().keep(str(root / "vendor/keep.txt")))
        root = self.repo({".gitignore": "vendor/*\n!vendor/keep.txt\n", "vendor/keep.txt": "", "vendor/other.txt": ""})
        gi = gitignore.GitIgnore()
        self.assertTrue(gi.keep(str(root / "vendor/keep.txt")))                    # here the folder itself is not ignored
        self.assertFalse(gi.keep(str(root / "vendor/other.txt")))

    def test_deeper_files_and_dot_ignore_override(self):
        root = self.repo({".gitignore": "*.txt\n", "docs/.gitignore": "!readme.txt\n", "a.txt": "", "docs/readme.txt": "",
                          "docs/other.txt": "", "both/.gitignore": "x.txt\n", "both/.ignore": "!x.txt\n", "both/x.txt": ""})
        gi = gitignore.GitIgnore()
        self.assertFalse(gi.keep(str(root / "a.txt")))
        self.assertTrue(gi.keep(str(root / "docs/readme.txt")))
        self.assertFalse(gi.keep(str(root / "docs/other.txt")))
        self.assertTrue(gi.keep(str(root / "both/x.txt")))                         # .ignore wins over .gitignore

    def test_matching_ignores_case_and_a_folder_with_no_repository_is_never_filtered(self):
        root = self.repo({".gitignore": "*.LOG\nBuild/\n", "a.log": "", "build/x": ""})
        gi = gitignore.GitIgnore()
        self.assertFalse(gi.keep(str(root / "a.log")))
        self.assertFalse(gi.keep(str(root / "build/x")))
        plain = Path(mkdtemp(prefix="mse-sp-plain-")).resolve()
        (plain / ".gitignore").write_text("*\n")
        (plain / "f.txt").write_text("x")
        self.assertTrue(gitignore.GitIgnore().keep(str(plain / "f.txt")))          # no .git above: not a repository

    def test_a_git_file_marks_a_repository_too_and_the_nearest_one_counts(self):
        outer = self.repo({".gitignore": "*.log\n"})
        inner = outer / "sub"
        inner.mkdir()
        (inner / ".git").write_text("gitdir: ../.git/modules/sub\n")              # a submodule / worktree
        (inner / ".gitignore").write_text("*.tmp\n")
        (inner / "a.log").write_text("x")
        (inner / "a.tmp").write_text("x")
        gi = gitignore.GitIgnore()
        self.assertTrue(gi.keep(str(inner / "a.log")))                            # the inner repository has its own rules
        self.assertFalse(gi.keep(str(inner / "a.tmp")))

    def test_edits_are_noticed_after_the_recheck_time(self):
        clock = [0.0]
        root = self.repo({".gitignore": "*.txt\n", "a.txt": ""})
        gi = gitignore.GitIgnore(clock=lambda: clock[0])
        self.assertFalse(gi.keep(str(root / "a.txt")))
        (root / ".gitignore").write_text("*.md\n")
        self.assertFalse(gi.keep(str(root / "a.txt")))                            # remembered
        clock[0] += gitignore.RECHECK_S + 1
        self.assertTrue(gi.keep(str(root / "a.txt")))

    def test_the_privacy_rules_decide_what_may_be_read(self):
        root = self.repo({".gitignore": "*.txt\n", "a.txt": ""})
        seen = []
        gi = gitignore.GitIgnore(blocked=lambda p: (seen.append(p), True)[1])
        self.assertTrue(gi.keep(str(root / "a.txt")))                              # an ignore file it may not read is no rules
        self.assertTrue(seen and all(p.endswith(("/.gitignore", "/.ignore")) for p in seen))

    def test_an_ignore_file_that_is_a_link_or_a_pipe_is_not_read(self):
        outside = Path(mkdtemp(prefix="mse-sp-linktarget-"))
        (outside / "rules").write_text("*.txt\n")
        root = self.repo({"a.txt": "", "sub/b.txt": ""})
        (root / ".gitignore").symlink_to(outside / "rules")                       # points at a file it must not follow
        os.mkfifo(str(root / "sub" / ".gitignore"))                                # reading it would wait for ever
        gi = gitignore.GitIgnore()
        t = time.time()
        self.assertTrue(gi.keep(str(root / "a.txt")))
        self.assertTrue(gi.keep(str(root / "sub" / "b.txt")))
        self.assertLess(time.time() - t, 2.0)

    def test_hostile_files_are_bounded(self):
        big = "x\n" * 70000                                                         # over 128 KB
        evil = "*a" * 200 + "b\n"
        many = "\n".join(f"f{i}*" for i in range(5000))
        root = self.repo({".gitignore": evil + many, "sub/.gitignore": big + "*.txt\n", "aaaa.txt": "", "sub/f.txt": ""})
        name = "a" * 250
        (root / name).write_text("x")
        t = time.time()
        gi = gitignore.GitIgnore()
        for rel in (name, "aaaa.txt", "sub/f.txt", "f1.txt", "f4999x"):
            gi.keep(str(root / rel))
        self.assertLess(time.time() - t, 2.0)
        self.assertTrue(gi.keep(str(root / "sub/f.txt")))                         # the oversize file is skipped, not half-read
        self.assertLessEqual(len(gitignore.parse(many)), gitignore.MAX_RULES)
        self.assertEqual(gitignore.parse("x" * 600), [])


class GitIgnoreInTheSearch(unittest.TestCase):
    def setUp(self):
        self.root = Path(mkdtemp(prefix="mse-sp-gitsearch-")).resolve()
        (self.root / "proj" / ".git").mkdir(parents=True)
        (self.root / "proj" / ".gitignore").write_text("build/\n*.generated.js\n")
        for rel in ("proj/src/outfile.js", "proj/build/outfile.js", "proj/src/outfile.generated.js", "other/outfile.js"):
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        self.m = mi.MacIndex(mi.Store(Path(mkdtemp(prefix="mse-sp-db-")) / "m.db"))
        self.cfg = {**mi.DEFAULT_CONFIG, "roots": [str(self.root)], "exclude": []}
        self.patch = mock.patch.object(mi, "settings", side_effect=lambda: dict(self.cfg))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.m.stop)
        self.addCleanup(self.m.store.close)
        rows = [fake(str(self.root / rel)) for rel in ("proj/src/outfile.js", "proj/build/outfile.js", "proj/src/outfile.generated.js",
                                                       "other/outfile.js")]
        mi.ingest(self.m.store, iter(rows), self.m.store.next_gen(), self.m.excluder())

    def paths(self):
        return sorted(i["path"].replace(str(self.root) + "/", "") for i in self.m.search("outfile")["items"])

    def test_off_by_default_and_on_by_the_setting(self):
        self.assertFalse(mi.DEFAULT_CONFIG["respect_gitignore"])
        everything = ["other/outfile.js", "proj/build/outfile.js", "proj/src/outfile.generated.js", "proj/src/outfile.js"]
        self.assertEqual(self.paths(), everything)
        self.cfg["respect_gitignore"] = True
        self.assertEqual(self.paths(), ["other/outfile.js", "proj/src/outfile.js"])
        self.cfg["respect_gitignore"] = False
        self.assertEqual(self.paths(), everything)                                  # nothing was removed from the index

    def test_ignore_files_in_a_blocked_folder_are_never_read(self):
        secret = self.root / "proj" / "secrets"
        secret.mkdir()
        (secret / ".gitignore").write_text("*\n")
        (secret / "plain.js").write_text("x")
        self.cfg["respect_gitignore"] = True
        reads = []
        real = open
        with mock.patch("builtins.open", side_effect=lambda f, *a, **k: (reads.append(str(f)), real(f, *a, **k))[1]):
            self.m.search("outfile")
        self.assertFalse([r for r in reads if "/secrets/" in r])


# ====================================================================== privacy parity

class PrivacyParity(unittest.TestCase):
    """Whatever the new plans, rules and boosts do, nothing the privacy rules block is ever returned, and nothing they let
    through goes missing for that reason. Secret paths are put into the database behind the ingest gate's back (as a
    stale index, a restored backup or a rule added later would) and then every kind of query is run."""

    SECRET = [f"{HOME}/.ssh/id_rsa_work", f"{HOME}/.ssh/config_work", f"{HOME}/.aws/credentials_work", f"{HOME}/proj/.env_work",
              f"{HOME}/proj/server_work.pem", f"{HOME}/proj/secret_work_notes.txt", f"{HOME}/proj/passwords_work.txt",
              f"{HOME}/Library/Keychains/login_work.keychain", f"{HOME}/Library/Mail/V10/work_message.emlx",
              f"{HOME}/proj/node_modules/work/index.js", f"{HOME}/proj/.git/work_config", f"{HOME}/.Trash/work_old.txt",
              f"{HOME}/Library/Application Support/Google/Chrome/Default/work_Cookies", "/System/Library/work_x", "/private/var/db/work_x",
              f"{HOME}/proj/credentials_work.json", f"{HOME}/proj/recovery-codes-work.zip", f"{HOME}/.claude/work_settings.json"]
    FINE = [f"{HOME}/Documents/work_plan.md", f"{HOME}/Documents/Work Report.pdf", f"{HOME}/proj/src/work_app.py",
            f"{HOME}/Desktop/work_photo.jpg", f"{HOME}/proj/readme_work.md", f"{HOME}/Documents/Acme/work_invoice.pdf"]

    def setUp(self):
        self.root = HOME
        self.m = mi.MacIndex(mi.Store(Path(mkdtemp(prefix="mse-sp-priv-")) / "m.db"))
        self.cfg = {**mi.DEFAULT_CONFIG, "roots": [HOME], "exclude": []}
        self.patch = mock.patch.object(mi, "settings", side_effect=lambda: dict(self.cfg))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.m.stop)
        self.addCleanup(self.m.store.close)
        mi.ingest(self.m.store, iter([fake(p) for p in self.FINE]), self.m.store.next_gen(), self.m.excluder())
        w = self.m.store.writer()
        for p in self.SECRET:                                # in behind the gate's back
            name = p.rsplit("/", 1)[-1]
            w.execute("INSERT INTO files(path, nf, parent_id, is_dir, kind, depth, ext, mtime) VALUES (?, ?, NULL, 0, 13, ?, '', ?)",
                      (p, mi.fold(name), p.count("/"), NOW))
        self.excl = mi.Excluder({"exclude": []}, h=HOME)
        with mock.patch.object(mi.MacIndex, "roots", return_value=[HOME]):
            pass

    def queries(self):
        words = ["work", "id_rsa", "Cookies", "credentials", "passwords", "secret", "keychain", "work_x", "pem", "env", "emlx", "settings"]
        shapes = []
        for w in words:
            shapes += [w, w.capitalize(), f"^{w}", f"{w}$", f"'{w}", f"{w}*", f"*{w}*", f"*{w}", f"{w} ext:txt", f"{w} !zzz",
                       f"re:{w}", f"path:{w}", f"{w[:2]}", f"{w[:-1]}x", f"work {w}", f"{w} work"]
        shapes += ["*.pem", "*.json", "**/.ssh/*", "**/node_modules/**", "work*", "??*.txt", "re:^.{4}_work", r"re:\w+_work",
                   "ext:pem", "ext:emlx", "type:file", "size:<1gb", "mtime:<30d", f"in:{HOME}/.ssh", f"in:{HOME}/Library", "path:.git/",
                   "path:Library/Mail", "*", "work_*", "*_work*"]
        return shapes

    def blocked_results(self, q, **kw):
        res = self.m.search(q, 300, **kw)
        return [i["path"] for i in res["items"] if self.m.excluder().blocked_uncached(i["path"])], res

    def test_no_query_shape_returns_a_blocked_path(self):
        for flags in ({}, {"match_folder_names": True}, {"respect_gitignore": True}, {"smart_case": False},
                      {"match_folder_names": True, "respect_gitignore": True, "frecency": False}):
            self.cfg = {**mi.DEFAULT_CONFIG, "roots": [HOME], "exclude": [], **flags}
            for q in self.queries():
                bad, res = self.blocked_results(q)
                self.assertEqual(bad, [], (q, flags))
                bad, res = self.blocked_results(q, offset=20, paged=True)
                self.assertEqual(bad, [], (q, "page", flags))

    def test_what_is_allowed_is_still_found(self):
        self.cfg["match_folder_names"] = True
        for q, want in (("work_plan", "work_plan.md"), ("Work Report", "Work Report.pdf"), ("*_photo.jpg", "work_photo.jpg"),
                        ("**/Acme/*", "work_invoice.pdf"), ("re:^readme_w.{3}\\.md$", "readme_work.md"), ("work_app.py", "work_app.py"),
                        ("acme invoice", "work_invoice.pdf")):
            names = [i["name"] for i in self.m.search(q, 300)["items"]]
            self.assertIn(want, names, q)

    def test_a_blocked_path_in_the_history_is_never_offered(self):
        w = self.m.store.writer()
        fid = w.execute("SELECT id FROM files WHERE path = ?", (f"{HOME}/proj/secret_work_notes.txt",)).fetchone()[0]
        self.m.frecency.record(f"{HOME}/proj/secret_work_notes.txt", "secret", fid)        # recorded before a rule existed
        self.m.frecency.record(f"{HOME}/Documents/work_plan.md", "secret", None)
        for q in ("secret", "work", "secret_work"):
            bad, res = self.blocked_results(q)
            self.assertEqual(bad, [], q)

    def test_the_scan_and_the_glob_scan_read_the_blocked_rows_but_return_none_of_them(self):
        for q in (r"re:^\w+_work", "??*_work*", "*_work.*"):
            res = self.m.search(q, 300)
            names = [i["name"] for i in res["items"]]
            self.assertIn("work_plan.md" if "plan" in " ".join(names) else names[0], names + ["work_plan.md"])
            self.assertEqual([i["path"] for i in res["items"] if self.m.excluder().blocked_uncached(i["path"])], [], q)


class ManySearchesAtOnce(unittest.TestCase):
    def test_no_search_is_told_the_index_is_missing_because_other_searches_hold_the_pool(self):
        i = Idx(corpus(800))
        m = mi.MacIndex(i.store)
        cfg = {**mi.DEFAULT_CONFIG, "roots": [HOME], "exclude": []}
        bad, lock = [], threading.Lock()
        with mock.patch.object(mi, "settings", side_effect=lambda: dict(cfg)):
            def work(k):
                for n in range(25):
                    res = m.search("report" if (k + n) % 2 else "invoice", 20)
                    if not res.get("searchable"):
                        with lock:
                            bad.append(res)
            threads = [threading.Thread(target=work, args=(k,)) for k in range(14)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual(bad, [])


# ====================================================================== the HTTP API

class FilesApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from mse import config, server
        cls.config = config
        cls.root = Path(mkdtemp(prefix="mse-sp-api-")).resolve()
        cls.m = mi.MacIndex(mi.Store(Path(mkdtemp(prefix="mse-sp-db-")) / "m.db"))
        cls.cfg = {**mi.DEFAULT_CONFIG, "roots": [str(cls.root)], "exclude": []}
        cls.patches = [mock.patch.object(mi, "settings", side_effect=lambda: dict(cls.cfg)),
                       mock.patch.object(mi, "manager", return_value=cls.m)]
        for p in cls.patches:
            p.start()
        (cls.root / "d").mkdir()
        rows = [fake(str(cls.root / "d" / f"item_{i:03d}.txt"), age_days=i) for i in range(120)] + [fake(str(cls.root / "d/.env"))]
        for r in rows[:5]:
            Path(r["path"]).write_text("x")
        mi.ingest(cls.m.store, iter(rows), cls.m.store.next_gen(), cls.m.excluder())
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in cls.patches:
            p.stop()
        cls.m.stop()
        cls.m.store.close()

    def req(self, method, path, body=None, params=None):
        url = f"http://127.0.0.1:{self.port}{path}" + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-MSE-Token": self.config.load()["token"], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_the_default_answer_keeps_its_shape_and_a_request_with_a_limit_gains_the_page_keys(self):
        code, res = self.req("GET", "/api/files/search", params={"q": "item"})
        self.assertEqual(set(res), {"items", "total", "ms", "scope", "query", "truncated", "searchable"})
        code, res = self.req("GET", "/api/files/search", params={"q": "item", "limit": 50})
        self.assertEqual((code, len(res["items"]), res["offset"], res["limit"], res["has_more"], res["next_offset"]),
                         (200, 50, 0, 50, True, 50))
        code, rest = self.req("GET", "/api/files/search", params={"q": "item", "limit": 150, "offset": 50})
        self.assertEqual((len(rest["items"]), rest["has_more"], rest["next_offset"]), (70, False, None))
        code, whole = self.req("GET", "/api/files/search", params={"q": "item", "limit": 200})
        self.assertEqual([i["path"] for i in res["items"] + rest["items"]], [i["path"] for i in whole["items"]])
        code, only = self.req("GET", "/api/files/search", params={"q": "item", "offset": 100})
        self.assertEqual((len(only["items"]), only["offset"]), (20, 100))

    def test_bad_limits_and_offsets_are_400(self):
        for params in ({"q": "item", "offset": -1}, {"q": "item", "offset": "x"}, {"q": "item", "limit": "x"}):
            code, err = self.req("GET", "/api/files/search", params=params)
            self.assertEqual(code, 400, params)

    def test_opened_records_and_the_next_search_ranks_by_it(self):
        target = str(self.root / "d" / "item_003.txt")
        code, res = self.req("POST", "/api/files/opened", {"path": target, "query": "item"})
        self.assertEqual((code, res), (200, {"recorded": True}))
        code, res = self.req("GET", "/api/files/search", params={"q": "item"})
        self.assertEqual(res["items"][0]["path"], target)
        code, st = self.req("GET", "/api/files/frecency")
        self.assertEqual((code, st["enabled"], st["files"], st["pairs"]), (200, True, 1, 1))
        self.assertEqual({"cap_files", "cap_pairs"} <= set(st), True)
        code, res = self.req("POST", "/api/files/frecency/clear", {})
        self.assertEqual((code, res), (200, {"cleared": True}))
        code, res = self.req("GET", "/api/files/search", params={"q": "item"})
        self.assertNotEqual(res["items"][0]["path"], target)

    def test_opened_refuses_what_the_search_would_not_show(self):
        for body in ({"path": "relative.txt"}, {"path": str(self.root / "d/.env")}, {"path": "/etc/hosts"}, {}, {"path": 5},
                     {"path": str(self.root / "d/item_001.txt"), "query": 7}):
            code, err = self.req("POST", "/api/files/opened", body)
            self.assertEqual(code, 400, body)
            self.assertIn("error", err)
        code, st = self.req("GET", "/api/files/frecency")
        self.assertEqual(st["files"], 0)

    def test_a_body_that_is_not_an_object_is_a_400_not_a_500(self):
        for body in ([], "x", 5, [1, 2]):
            code, err = self.req("POST", "/api/files/opened", body)
            self.assertEqual(code, 400, body)

    def test_the_new_routes_need_the_token(self):
        for method, path in (("POST", "/api/files/opened"), ("GET", "/api/files/frecency"), ("POST", "/api/files/frecency/clear")):
            r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, data=b"{}" if method == "POST" else None)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(r, timeout=10)
            self.assertEqual(cm.exception.code, 401)

    def test_status_says_how_the_name_index_is(self):
        code, st = self.req("GET", "/api/files/status")
        info = st["name_index"]
        self.assertEqual((info["schema"], info["table"], info["upgrade"]["state"], info["upgrade_pending"]), (2, "name_tri", "idle", False))
        self.assertIn("full_disk_access", st)                                       # the rest of the status is still there

    def test_the_settings_accept_the_new_keys_and_refuse_wrong_ones(self):
        code, res = self.req("POST", "/api/settings", {"mac_index": {"respect_gitignore": "yes"}})
        self.assertEqual(code, 400)
        code, res = self.req("POST", "/api/settings", {"mac_index": {"regex_budget_ms": 1}})
        self.assertEqual(code, 400)
        code, res = self.req("POST", "/api/settings", {"mac_index": {"smart_case": False, "regex_budget_ms": 250}})
        self.assertEqual(code, 200)
        self.assertEqual((res["mac_index"]["smart_case"], res["mac_index"]["regex_budget_ms"]), (False, 250))
        code, res = self.req("POST", "/api/settings", {"mac_index": {"smart_case": True, "regex_budget_ms": 150}})
        self.assertEqual(code, 200)


if __name__ == "__main__":
    unittest.main()
