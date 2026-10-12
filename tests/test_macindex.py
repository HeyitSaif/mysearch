"""The whole-Mac file-name search: query language, ranking, typo tolerance, the privacy rules, FSEvents, the API,
Ask's step events, the MCP/CLI boundary and the folder tree.

Everything runs against a temporary MySearch home with a fake crawl stream or a temporary folder tree: the real disk
is never crawled and the helper is only used when MSE_TEST_HELPER points at a built binary (see HelperCrawl).
Run: .venv/bin/python -m unittest discover -s tests"""

import io
import json
import os
import tempfile
import threading
import time
import unicodedata
import unittest
import urllib.error
import urllib.parse
import urllib.request
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import ask, cli, config, filequery as fq, macindex as mi, mcp, search  # noqa: E402
from tests import tmpdirs  # noqa: E402

# The system temp folder (/private/var/folders) is private to the rules on purpose, so test trees live under $TMPDIR
# only when the rules allow it and under /tmp otherwise (tests/tmpdirs.py). Always the real path.
TMP = tmpdirs.indexable_tmp()


def mkdtemp(prefix="mse-mi-"):
    return tmpdirs.mkdtemp(prefix=prefix, indexable=True)


HOME = "/Users/alex"
NOW = 1_742_385_600            # a fixed "now" (2025-03-19 12:00 UTC) for ranking tests
DAY = 86400


def fake(path, is_dir=False, size=1000, age_days=10, ctime=None):
    m = NOW - int(age_days * DAY)
    return {"path": path, "is_dir": is_dir, "size": size, "mtime": m, "ctime": ctime or m}


class Idx:
    """A temporary index fed through the real ingest path (Excluder and all)."""

    def __init__(self, rows, cfg=None, home=HOME):
        self.dir = mkdtemp(prefix="mse-macidx-")
        self.store = mi.Store(Path(self.dir) / "macindex.db")
        self.excl = mi.Excluder(cfg or {}, h=home)
        self.stored = mi.ingest(self.store, iter(rows), self.store.next_gen(), self.excl)

    def search(self, query, limit=50, roots=None, now=NOW):
        q = fq.parse_query(query, now=now, home=HOME)
        with self.store.read() as c:
            return fq.search(c, q, limit, roots, now=now, allow=lambda p: not self.excl.blocked(p))

    def names(self, query, **kw):
        return [i["name"] for i in self.search(query, **kw)["items"]]

    def paths(self, query, **kw):
        return [i["path"] for i in self.search(query, **kw)["items"]]


# ====================================================================== the query language

class Parsing(unittest.TestCase):
    def parse(self, q):
        return fq.parse_query(q, now=NOW, home=HOME)

    def test_word_modes(self):
        q = self.parse("""invoice 'exact ^start end$ ^whole$ !skip "two words" '""")
        got = [(w.raw, w.mode, w.neg) for w in q.words]
        self.assertEqual(got, [("invoice", "fuzzy", False), ("exact", "exact", False), ("start", "prefix", False),
                               ("end", "suffix", False), ("whole", "whole", False), ("skip", "fuzzy", True),
                               ("two words", "exact", False)])

    def test_filters(self):
        q = self.parse("ext:PDF,.docx type:image kind:folder !ext:tmp !type:video path:Projects/Alpha limit:25 re:^IMG_\\d+")
        self.assertEqual(q.ext, ["pdf", "docx"])
        self.assertEqual(q.kinds, ["image", "folder"])
        self.assertEqual((q.not_ext, q.not_kinds), (["tmp"], ["video"]))
        self.assertEqual((q.path, q.limit, q.regex), (["projects/alpha"], 25, ["^IMG_\\d+"]))
        self.assertEqual(self.parse("type:swift").ext, ["swift"])              # an unknown type word is an extension
        self.assertEqual(self.parse("limit:99999").limit, fq.MAX_LIMIT)
        pub = self.parse("ext:pdf report").public()
        self.assertEqual(pub, {"words": [{"text": "report", "mode": "fuzzy", "not": False}], "filters": {"ext": ["pdf"]}})

    def test_size(self):
        self.assertEqual(fq.parse_size(">5mb"), {"min": 5 * 1024 ** 2 + 1})
        self.assertEqual(fq.parse_size("<=1kb"), {"max": 1024})
        self.assertEqual(fq.parse_size("1mb..2gb"), {"min": 1024 ** 2, "max": 2 * 1024 ** 3})
        self.assertEqual(fq.parse_size("=0"), {"min": 0, "max": 0})
        self.assertEqual(fq.parse_size(">1.5k"), {"min": 1537})
        with self.assertRaises(fq.QueryError):
            fq.parse_size(">12 parsecs")

    def test_mtime_direction(self):
        # a duration: "<7d" is NEWER than 7 days, ">7d" is OLDER
        self.assertEqual(fq.parse_mtime("<7d", NOW), {"min": NOW - 7 * DAY})
        self.assertEqual(fq.parse_mtime(">30d", NOW), {"max": NOW - 30 * DAY})
        self.assertEqual(fq.parse_mtime("<2h", NOW), {"min": NOW - 7200})
        # a date: ">" is on/after the day, "<" is before it
        after = fq.parse_mtime(">2025-01-01", NOW)
        before = fq.parse_mtime("<2025-01-01", NOW)
        self.assertIn("min", after)
        self.assertNotIn("max", after)
        self.assertEqual(before["max"], after["min"] - 1)
        month = fq.parse_mtime("2025-03", NOW)
        self.assertLess(month["max"] - month["min"], 31 * DAY)
        self.assertGreater(month["max"] - month["min"], 28 * DAY - 1)
        rng = fq.parse_mtime("2025-01..2025-03", NOW)
        self.assertGreater(rng["max"] - rng["min"], 88 * DAY)
        with self.assertRaises(fq.QueryError):
            fq.parse_mtime("<someday", NOW)

    def test_bad_regex_and_limit(self):
        for bad in ("re:(", "limit:many", "size:>x", "mtime:>never", "re:(a+)+$", "re:(.*)*x", "re:(\\w+){2,}z"):
            with self.assertRaises(fq.QueryError, msg=bad):
                self.parse(bad)

    def test_in_disambiguation(self):
        """A value starting with ~ or / is a FOLDER; anything else names a MySearch folder or group."""
        q = self.parse("report in:~/Documents/Projects in:/Volumes/x in:work")
        self.assertEqual(q.folders, [os.path.join(os.path.expanduser("~"), "Documents/Projects"), "/Volumes/x"])
        self.assertEqual(q.scope_names, ["work"])
        self.assertEqual([w.raw for w in q.words], ["report"])
        q = self.parse('in:"/tmp/with space/x" in:~/a,~/b')
        self.assertEqual(q.folders[0], "/tmp/with space/x")
        self.assertEqual(len(q.folders), 3)
        self.assertEqual(fq.is_folder_value("~/x"), True)
        self.assertEqual(fq.is_folder_value("/x"), True)
        self.assertEqual(fq.is_folder_value("work"), False)

    def test_path_part_of_a_word(self):
        q = self.parse("docs/readme")
        self.assertEqual((q.path, [w.raw for w in q.words]), (["docs/"], ["readme"]))


class ScopeShortcuts(unittest.TestCase):
    """config.scope_shortcut keeps its behaviour and leaves folder paths alone."""

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(mkdtemp(prefix="mse-mi-sc-")).resolve()
        if not config.get_collection(config.load(), "mi-work"):
            config.add_collection(str(cls.dir), "mi-work")

    @classmethod
    def tearDownClass(cls):
        config.remove_collection("mi-work")

    def test_name_prefix_and_in_name_still_scope(self):
        s = config.scope_shortcut("mi-work: invoice", config.load(), strict=True)
        self.assertEqual((s["tokens"], s["rest"]), (["mi-work"], "invoice"))
        s = config.scope_shortcut("invoice in:mi-work", config.load(), strict=True)
        self.assertEqual((s["tokens"], s["rest"]), (["mi-work"], "invoice"))

    def test_folder_paths_are_not_scope_names(self):
        cfg = config.load()
        for q in ("invoice in:~/Documents", "invoice in:/Users/alex/Documents", 'invoice in:"~/My Folder/x y"',
                  'invoice in:"/Users/alex y"'):
            s = config.scope_shortcut(q, cfg, strict=True)          # strict: no 400 for an "unknown" name
            self.assertIsNone(s["tokens"], q)
            self.assertEqual((s["rest"], s["unknown"]), (q, []))
        s = config.scope_shortcut('invoice in:"~/My Folder" in:mi-work', cfg, strict=True)
        self.assertEqual((s["tokens"], s["rest"]), (["mi-work"], 'invoice in:"~/My Folder"'))
        with self.assertRaises(ValueError):                           # a real unknown name is still an error
            config.scope_shortcut("invoice in:ghost", cfg, strict=True)

    def test_folder_tokens(self):
        self.assertEqual(config.folder_tokens('a in:~/x in:"/tmp/a b" in:work in:/q/'),
                         [os.path.expanduser("~/x"), "/tmp/a b", "/q"])


# ====================================================================== ranking and typos

class Ranking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = Idx([
            fake(f"{HOME}/Documents/main.py"),
            fake(f"{HOME}/Documents/Mainframe Notes.txt"),
            fake(f"{HOME}/Documents/the_main_report.pdf"),
            fake(f"{HOME}/Documents/domain.txt"),
            fake(f"{HOME}/Documents/remaining-tasks.md"),
            fake(f"{HOME}/Documents/Projects/Main", is_dir=True),
        ])

    def test_order_exact_prefix_boundary_substring(self):
        names = self.idx.names("main")
        self.assertEqual(names[:2], ["Main", "main.py"])                  # exact (folder boosted), then exact stem
        self.assertEqual(names[2], "Mainframe Notes.txt")                 # prefix
        self.assertEqual(names[3], "the_main_report.pdf")                 # word boundary
        self.assertEqual(set(names[4:]), {"domain.txt", "remaining-tasks.md"})   # substring

    def test_folder_boost_only_on_exact(self):
        i = Idx([fake(f"{HOME}/a/report.txt"), fake(f"{HOME}/b/report", is_dir=True),
                 fake(f"{HOME}/a/reports.txt"), fake(f"{HOME}/b/reports", is_dir=True)])
        names = i.names("report")
        self.assertEqual(names[0], "report")                              # exact folder beats exact-stem file
        self.assertEqual(set(names[:2]), {"report", "report.txt"})

    def test_recency_and_depth_break_ties(self):
        i = Idx([fake(f"{HOME}/old/notes_a.txt", age_days=900), fake(f"{HOME}/new/notes_b.txt", age_days=2),
                 fake(f"{HOME}/x/y/z/w/notes_c.txt", age_days=2)])
        names = i.names("notes")
        self.assertEqual(names, ["notes_b.txt", "notes_c.txt", "notes_a.txt"])   # recent first, then shallow, then old

    def test_match_spans_map_to_the_displayed_name(self):
        it = self.idx.search("main")["items"][1]
        self.assertEqual((it["name"], it["match"]), ("main.py", [[0, 4]]))
        it = [x for x in self.idx.search("main")["items"] if x["name"] == "the_main_report.pdf"][0]
        self.assertEqual(it["match"], [[4, 8]])

    def test_non_ascii_names_nfd_and_nfc(self):
        nfd = unicodedata.normalize("NFD", "Café Menü Übersicht.docx")
        i = Idx([fake(f"{HOME}/a/{nfd}"), fake(f"{HOME}/a/Straße.txt")])
        for q in ("cafe menu", "Café", "café", "übersicht", "UBERSICHT"):
            self.assertEqual(i.names(q), [nfd], q)
        it = i.search("café")["items"][0]
        a, b = it["match"][0]
        self.assertEqual(unicodedata.normalize("NFC", it["name"][a:b]), "Café")      # spans index the original name
        self.assertEqual(i.names("strasse"), ["Straße.txt"])

    def test_all_words_must_match(self):
        i = Idx([fake(f"{HOME}/a/invoice_acme_2025.pdf"), fake(f"{HOME}/a/invoice_other.pdf")])
        self.assertEqual(i.names("invoice acme"), ["invoice_acme_2025.pdf"])
        self.assertEqual(i.names("acme invoice 2025"), ["invoice_acme_2025.pdf"])
        self.assertEqual(i.names("invoice !acme"), ["invoice_other.pdf"])


class Typos(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = Idx([fake(f"{HOME}/a/main.py"), fake(f"{HOME}/a/receive.txt"), fake(f"{HOME}/a/Invoice_2025.pdf"),
                       fake(f"{HOME}/a/document.docx"), fake(f"{HOME}/a/accommodation.md"), fake(f"{HOME}/a/acme_supplies.key.txt"),
                       fake(f"{HOME}/a/mail.txt")])

    def test_swapped_neighbours_four_letters(self):
        self.assertIn("main.py", self.idx.names("mian"))                  # the example: 4 letters, swapped
        self.assertNotIn("mail.txt", self.idx.names("mian"))              # but no other edit at 4 letters

    def test_one_typo_for_five_letters_or_more(self):
        self.assertEqual(self.idx.names("recieve"), ["receive.txt"])      # swap
        self.assertEqual(self.idx.names("invioce"), ["Invoice_2025.pdf"]) # swap
        self.assertEqual(self.idx.names("docment"), ["document.docx"])    # deletion
        self.assertEqual(self.idx.names("documment"), ["document.docx"])  # insertion
        self.assertEqual(self.idx.names("documant"), ["document.docx"])   # substitution
        self.assertEqual(self.idx.names("supplias"), ["acme_supplies.key.txt"])  # substitution

    def test_two_typos_find_nothing(self):
        self.assertEqual(self.idx.names("dcoumnet"), [])
        self.assertEqual(self.idx.names("recevied"), [])

    def test_typo_results_rank_after_literal_ones(self):
        i = Idx([fake(f"{HOME}/a/recieve_notes.txt"), fake(f"{HOME}/a/receive.txt")])
        res = i.search("recieve")["items"]
        self.assertEqual([r["name"] for r in res], ["recieve_notes.txt", "receive.txt"])
        i2 = Idx([fake(f"{HOME}/a/mainly.txt"), fake(f"{HOME}/a/main.py")])
        self.assertEqual(i2.names("mian")[0] in ("main.py", "mainly.txt"), True)

    def test_exact_mode_and_short_words_have_no_typos(self):
        self.assertEqual(self.idx.names("'recieve"), [])
        self.assertEqual(self.idx.names("\"recieve\""), [])
        self.assertEqual(self.idx.names("mnai"), [])                      # 4 letters: swaps only, and this is not a swap of any name

    def test_within_one(self):
        for a, b, want in (("abc", "abc", True), ("abcd", "abdc", True), ("abcde", "abcxe", True),
                           ("abcde", "abde", True), ("abcde", "abcdef", True), ("abcde", "abxye", False),
                           ("abcde", "ab", False)):
            self.assertEqual(fq.within_one(a, b), want, (a, b))


# ====================================================================== filters

class Filters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = Idx([
            fake(f"{HOME}/Docs/report.pdf", size=5 * 1024 ** 2, age_days=3),
            fake(f"{HOME}/Docs/report_old.pdf", size=2 * 1024, age_days=400),
            fake(f"{HOME}/Docs/photo.jpg", size=900_000, age_days=5),
            fake(f"{HOME}/Docs/notes.md", size=300, age_days=60),
            fake(f"{HOME}/Docs/IMG_0042.png", size=2_000_000, age_days=1),
            fake(f"{HOME}/Docs/song.mp3", size=4_000_000, age_days=30),
            fake(f"{HOME}/Docs/Projects", is_dir=True, age_days=3),
            fake(f"{HOME}/Work/deep/sub/folder/report_final.docx", size=10, age_days=3),
            fake(f"{HOME}/Docs/tool.swift", size=50, age_days=2),
            fake(f"{HOME}/Apps/Tool.app", is_dir=True, age_days=20),
        ])

    def test_ext_and_type(self):
        self.assertEqual(sorted(self.idx.names("ext:pdf")), ["report.pdf", "report_old.pdf"])
        self.assertEqual(sorted(self.idx.names("ext:pdf,md")), ["notes.md", "report.pdf", "report_old.pdf"])
        self.assertEqual(sorted(self.idx.names("type:image")), ["IMG_0042.png", "photo.jpg"])
        self.assertEqual(self.idx.names("kind:folder"), ["Projects"])
        self.assertEqual(self.idx.names("type:audio"), ["song.mp3"])
        self.assertEqual(self.idx.names("type:app"), ["Tool.app"])
        self.assertEqual(self.idx.names("type:code"), ["tool.swift"])
        self.assertEqual(self.idx.names("type:swift"), ["tool.swift"])
        self.assertEqual(self.idx.names("report !ext:pdf"), ["report_final.docx"])
        self.assertEqual(self.idx.names("report !type:doc"), self.idx.names("report !ext:docx"))
        self.assertNotIn("Projects", self.idx.names("type:file"))

    def test_size(self):
        self.assertEqual(self.idx.names("size:>4mb"), ["report.pdf"])
        self.assertEqual(sorted(self.idx.names("size:<1kb")), ["notes.md", "report_final.docx", "tool.swift"])
        self.assertEqual(self.idx.names("report size:1kb..1mb"), ["report_old.pdf"])
        self.assertNotIn("Projects", self.idx.names("size:<10mb"))            # folders have no size

    def test_mtime_both_forms(self):
        self.assertEqual(sorted(self.idx.names("mtime:<7d type:file")),
                         sorted(["report.pdf", "photo.jpg", "IMG_0042.png", "report_final.docx", "tool.swift"]))
        self.assertEqual(self.idx.names("mtime:>300d"), ["report_old.pdf"])
        day = time.strftime("%Y-%m-%d", time.localtime(NOW - 100 * DAY))
        self.assertEqual(self.idx.names(f"mtime:>{day} report"), ["report.pdf", "report_final.docx"])
        self.assertEqual(self.idx.names(f"mtime:<{day} report"), ["report_old.pdf"])

    def test_path_and_folder(self):
        self.assertEqual(self.idx.names("path:work/deep"), ["report_final.docx"])
        self.assertEqual(self.idx.names("report path:docs/ !path:old"), ["report.pdf"])
        self.assertEqual(sorted(self.idx.names(f"report in:{HOME}/Docs")), ["report.pdf", "report_old.pdf"])
        self.assertEqual(self.idx.names(f"in:{HOME}/Work type:file"), ["report_final.docx"])
        self.assertEqual(self.idx.names("in:/nowhere report"), [])
        self.assertEqual(self.idx.names("docs/report ext:pdf"), ["report.pdf", "report_old.pdf"])

    def test_in_survives_inserts_after_the_crawl(self):
        i = Idx([fake(f"{HOME}/Work/deep/report_final.docx"), fake(f"{HOME}/Docs/report.pdf")])
        mi.ingest(i.store, iter([fake(f"{HOME}/Work/deep/new_report.pdf")]), i.store.next_gen(), i.excl)
        self.assertEqual(sorted(i.names(f"report in:{HOME}/Work")), ["new_report.pdf", "report_final.docx"])

    def test_regex(self):
        self.assertEqual(self.idx.names(r"re:^IMG_\d+\.png$"), ["IMG_0042.png"])
        self.assertEqual(sorted(self.idx.names(r"re:report(_old)?\.pdf$")), ["report.pdf", "report_old.pdf"])
        self.assertEqual(self.idx.names(r"re:^IMG_ ext:png"), ["IMG_0042.png"])
        self.assertEqual(self.idx.names(r"re:^zzz"), [])

    def test_prefix_suffix_whole_and_exact(self):
        self.assertEqual(sorted(self.idx.names("^report_")), ["report_final.docx", "report_old.pdf"])
        self.assertEqual(sorted(self.idx.names("old.pdf$")), ["report_old.pdf"])
        self.assertEqual(self.idx.names("^notes.md$"), ["notes.md"])
        self.assertEqual(self.idx.names("'port_f"), ["report_final.docx"])
        self.assertEqual(self.idx.names("\"report old\""), [])

    def test_short_words_and_limits(self):
        self.assertEqual(sorted(self.idx.names("im")), ["IMG_0042.png"])          # 1-2 letters: names that start with it
        self.assertEqual(len(self.idx.names("report", limit=2)), 2)
        self.assertEqual(len(self.idx.names("report limit:1")), 1)
        res = self.idx.search("report")
        self.assertEqual(res["total"], 3)

    def test_empty_query_and_item_shape(self):
        self.assertEqual(self.idx.search("")["items"], [])
        it = self.idx.search("^report.pdf$")["items"][0]
        self.assertEqual(set(it), {"path", "name", "parent", "is_dir", "kind", "ext", "size", "mtime", "ctime", "score", "match"})
        self.assertEqual((it["parent"], it["kind"], it["ext"], it["is_dir"]), (f"{HOME}/Docs", "pdf", "pdf", False))

    def test_scope_roots_restrict(self):
        self.assertEqual(sorted(self.idx.names("report", roots=[f"{HOME}/Work"])), ["report_final.docx"])
        self.assertEqual(self.idx.names("report", roots=["/elsewhere"]), [])


# ====================================================================== privacy

SECRET_FEED = [
    f"{HOME}/.env", f"{HOME}/proj/.env.production", f"{HOME}/proj/server.pem", f"{HOME}/proj/api.key",
    f"{HOME}/proj/cert.p12", f"{HOME}/proj/bundle.pfx", f"{HOME}/proj/release.keystore", f"{HOME}/.ssh/id_ed25519",
    f"{HOME}/.ssh/config", f"{HOME}/.gnupg/pubring.kbx", f"{HOME}/.aws/credentials", f"{HOME}/.config/gcloud/creds.db",
    f"{HOME}/Library/Keychains/login.keychain-db", f"{HOME}/.codex/state.sqlite", f"{HOME}/.codex/auth/key",
    f"{HOME}/.local/share/codex-agent/state",
    f"{HOME}/.claude/settings.json", f"{HOME}/.claude.json", f"{HOME}/.claude-mem/db",
    f"{HOME}/Library/Application Support/Google/Chrome/Default/Cookies",
    f"{HOME}/Library/Application Support/Firefox/Profiles/x/cookies.sqlite", f"{HOME}/Library/Mail/V10/x.emlx",
    f"{HOME}/Library/Messages/chat.db", f"{HOME}/Documents/Private/Credentials_Store/codes.txt",
    f"{HOME}/Documents/Projects/Alpha/repo/main.py", f"{HOME}/proj/node_modules/x/index.js", f"{HOME}/proj/.git/config",
    f"{HOME}/proj/.venv/bin/python", f"{HOME}/Library/Caches/x/y", "/System/Library/x", "/private/var/db/x",
    "/Library/Caches/x", f"{HOME}/proj/my_secret_notes.txt", f"{HOME}/proj/passwords.txt", f"{HOME}/proj/credentials.json",
    f"{HOME}/proj/recovery-codes-20250301.zip", f"{HOME}/proj/mcp.json", f"{HOME}/.mcp.json", f"{HOME}/.netrc",
    f"{HOME}/.Trash/old.txt", f"{HOME}/Library/Safari/History.db", f"{HOME}/proj/id_rsa.pub",
]
FINE_FEED = [f"{HOME}/Documents/plan.md", f"{HOME}/proj/src/app.py", f"{HOME}/Desktop/photo.jpg", f"{HOME}/proj/readme.md"]
# The editable privacy rules these tests run with: the shipped default plus one folder of the test's own.
ALPHA_RULES = {"exclude": ["~/Documents/Projects/Alpha/", "~/Library/"]}


class Privacy(unittest.TestCase):
    def test_secret_names_are_never_stored(self):
        i = Idx([fake(p) for p in SECRET_FEED + FINE_FEED], cfg=ALPHA_RULES)
        with i.store.read() as c:
            stored = [r[0] for r in c.execute("SELECT path FROM files")]
        self.assertEqual(sorted(stored), sorted(FINE_FEED))
        self.assertEqual(i.stored, len(FINE_FEED))
        for needle in ("env", "pem", "ssh", "id_rsa", "keychain", "credentials", "cookies", "secret", "password", "claude"):
            self.assertEqual(i.search(needle)["items"], [], needle)

    def test_nothing_slips_in_through_a_parent_cache(self):
        """The fast path remembers a verified parent; a blocked sibling must still be refused, in every order."""
        e = mi.Excluder({}, h=HOME)
        order = [f"{HOME}/proj/a.txt", f"{HOME}/proj/.env", f"{HOME}/proj/b.txt", f"{HOME}/proj/node_modules",
                 f"{HOME}/proj/c.pem", f"{HOME}/proj/credentials.txt", f"{HOME}/proj/.ssh", f"{HOME}/proj/ok.txt"]
        got = [e.blocked(p) for p in order]
        fresh = [mi.Excluder({}, h=HOME).blocked(p) for p in order]
        self.assertEqual(got, fresh)
        self.assertEqual(got, [False, True, False, True, True, True, True, False])
        for p in SECRET_FEED + FINE_FEED:                                    # same answer, whatever came before
            self.assertEqual(e.blocked(p), mi.Excluder({}, h=HOME).blocked(p), p)

    def test_the_gate_applies_again_at_query_time(self):
        i = Idx([fake(f"{HOME}/Documents/plan.md")])
        w = i.store.writer()                                # a row that got into the file some other way
        w.execute("INSERT INTO files(path, nf, parent_id, is_dir, kind, depth, ext) VALUES (?, 'id_rsa', NULL, 0, 13, 4, '')",
                  (f"{HOME}/.ssh/id_rsa",))
        self.assertEqual(i.names("id_rsa"), [])
        w.execute("INSERT INTO files(path, nf, parent_id, is_dir, kind, depth, ext) VALUES (?, 'plan b.md', NULL, 0, 3, 4, 'md')",
                  (f"{HOME}/Documents/plan b.md",))
        self.assertEqual(i.names("plan b"), ["plan b.md"])

    def test_purge_removes_what_the_rules_now_block(self):
        i = Idx([fake(f"{HOME}/Documents/plan.md"), fake(f"{HOME}/Documents/Secret Garden/map.md"),
                 fake(f"{HOME}/Documents/Projects/Alpha/code.py"), fake(f"{HOME}/Documents/Projects/Beta/code.py")],
                cfg={"exclude": []})
        self.assertEqual(sorted(i.names("code")), ["code.py", "code.py"])
        tight = mi.Excluder({"exclude": ["~/Documents/Projects/Alpha/"]}, h=HOME)
        self.assertGreaterEqual(i.store.purge(tight), 1)
        self.assertEqual(i.paths("code"), [f"{HOME}/Documents/Projects/Beta/code.py"])

    def test_configurable_exclusions_and_roots_inside_them(self):
        cfg = {"exclude": ["~/Documents/Projects/Alpha/", "~/Library/"]}
        e = mi.Excluder(cfg, h=HOME)
        self.assertTrue(e.blocked(f"{HOME}/Documents/Projects/Alpha/x"))
        self.assertTrue(e.blocked(f"{HOME}/Library/Preferences/x.plist"))
        self.assertFalse(e.blocked(f"{HOME}/Documents/Projects/Beta/x"))
        self.assertFalse(mi.Excluder({"exclude": []}, h=HOME).blocked(f"{HOME}/Documents/Projects/Alpha/x"))      # configurable
        self.assertTrue(mi.Excluder({"exclude": []}, h=HOME).blocked(f"{HOME}/.ssh/x"))                     # not configurable
        # an explicit root inside an excluded folder is crawled; a hard-blocked root never is
        e2 = mi.Excluder(cfg, h=HOME, roots=[HOME, f"{HOME}/Library/CloudStorage"])
        self.assertFalse(e2.blocked(f"{HOME}/Library/CloudStorage/OneDrive/a.docx"))
        self.assertTrue(e2.blocked(f"{HOME}/Library/Preferences/x.plist"))
        self.assertTrue(e2.blocked(f"{HOME}/Library/Mail/x"))
        self.assertFalse(e2.root_allowed(f"{HOME}/.ssh"))
        self.assertTrue(e2.root_allowed(f"{HOME}/Library/CloudStorage"))

    def test_helper_gets_the_same_rules_as_arguments(self):
        args = mi.Excluder(ALPHA_RULES, h=HOME).helper_args()
        pairs = list(zip(args[::2], args[1::2]))
        self.assertIn(("--skip-prefix", f"{HOME}/.ssh"), pairs)
        self.assertIn(("--skip-prefix", f"{HOME}/Documents/Projects/Alpha"), pairs)
        self.assertIn(("--skip-dir", "node_modules"), pairs)
        self.assertIn(("--skip-name", ".env"), pairs)
        self.assertIn(("--skip-name", "*credential*"), pairs)
        self.assertIn(("--skip-name", ".claude*"), pairs)
        self.assertIn(("--no-descend", "app"), pairs)

    def test_protected_folders_policy(self):
        h = HOME
        skips = mi.protected_skips("auto", False, [f"{h}/Documents/acme"], h)
        self.assertIn(f"{h}/Downloads", skips)
        self.assertIn(f"{h}/Desktop", skips)
        self.assertNotIn(f"{h}/Documents", skips)                 # a collection already lives there: access exists
        self.assertEqual(mi.protected_skips("auto", True, [], h), [])          # Full Disk Access: nothing to protect
        self.assertEqual(mi.protected_skips("never", False, [], h), [])
        self.assertIn(f"{h}/Documents", mi.protected_skips("always", True, [f"{h}/Documents/x"], h))
        e = mi.Excluder({}, h=h, protected=skips)
        self.assertTrue(e.blocked(f"{h}/Downloads/a.pdf"))
        self.assertFalse(e.blocked(f"{h}/Documents/acme/a.pdf"))

    def test_full_disk_access_detection_never_prompts_and_reports_none_or_bool(self):
        self.assertIn(mi.full_disk_access(), (True, False, None))
        with mock.patch("os.open", side_effect=PermissionError):
            self.assertIs(mi.full_disk_access(), False)
        with mock.patch("os.open", side_effect=FileNotFoundError):
            self.assertIsNone(mi.full_disk_access())
        with mock.patch("os.open", return_value=99), mock.patch("os.close") as close:
            self.assertIs(mi.full_disk_access(), True)
            close.assert_called_once_with(99)

    def test_settings_validation(self):
        for bad in ({"enabled": "yes"}, {"roots": []}, {"roots": ["relative/path"]}, {"roots": "x"}, {"exclude": [1]},
                    {"skip_protected": "maybe"}, {"rescan_every_h": -1}, {"nope": 1}):
            with self.assertRaises(ValueError, msg=str(bad)):
                mi.check_settings(bad)
        self.assertEqual(mi.check_settings({"roots": ["~/x"]})["roots"], [os.path.expanduser("~/x")])
        with self.assertRaises(ValueError):
            config.update_settings({"mac_index": {"enabled": "no"}})


# ====================================================================== FSEvents and the crawl

class Events(unittest.TestCase):
    def setUp(self):
        self.root = Path(mkdtemp(prefix="mse-mi-ev-")).resolve()
        self.home = str(self.root)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "a.txt").write_text("x")
        self.store = mi.Store(Path(mkdtemp(prefix="mse-mi-evdb-")) / "m.db")
        self.excl = mi.Excluder({"exclude": []}, h=self.home)
        mi.ingest(self.store, mi.walk_python(self.home, self.excl), self.store.next_gen(), self.excl)

    def names(self, q):
        with self.store.read() as c:
            return [i["name"] for i in fq.search(c, fq.parse_query(q))["items"]]

    def apply(self, events):
        def scan(path, gen):
            n = mi.ingest(self.store, mi.walk_python(path, self.excl), gen, self.excl)
            self.store.sweep(path, gen)
            return n
        return mi.apply_events(self.store, events, self.excl, scan)

    def test_walk_python_found_the_tree(self):
        self.assertEqual(sorted(self.names("a.txt")), ["a.txt"])
        self.assertEqual(self.names("docs type:folder"), ["docs"])

    def test_created_modified_removed(self):
        new = self.root / "docs" / "fresh_report.txt"
        new.write_text("hello")
        out = self.apply({str(new): mi.F_CREATED})
        self.assertEqual(out["upserted"], 1)
        self.assertEqual(self.names("fresh_report"), ["fresh_report.txt"])
        new.write_text("hello world, longer")
        self.apply({str(new): 0x1000})
        with self.store.read() as c:
            self.assertEqual(c.execute("SELECT size FROM files WHERE path = ?", (str(new),)).fetchone()[0], len("hello world, longer"))
        new.unlink()
        self.assertEqual(self.apply({str(new): mi.F_REMOVED})["deleted"], 1)
        self.assertEqual(self.names("fresh_report"), [])

    def test_new_and_moved_in_folders_are_scanned_and_removed_folders_vanish(self):
        d = self.root / "incoming" / "deep"
        d.mkdir(parents=True)
        (d / "inside_file.txt").write_text("x")
        self.apply({str(self.root / "incoming"): mi.F_CREATED | mi.F_ISDIR})
        self.assertEqual(self.names("inside_file"), ["inside_file.txt"])
        import shutil
        shutil.rmtree(self.root / "incoming")
        self.apply({str(self.root / "incoming"): mi.F_REMOVED | mi.F_ISDIR})
        self.assertEqual(self.names("inside_file"), [])
        self.assertEqual(self.names("deep"), [])

    def test_rename_removes_the_old_name_and_adds_the_new(self):
        old, new = self.root / "docs" / "a.txt", self.root / "docs" / "renamed_doc.txt"
        old.rename(new)
        self.apply({str(old): mi.F_RENAMED, str(new): mi.F_RENAMED})
        self.assertEqual(self.names("a.txt"), [])
        self.assertEqual(self.names("renamed_doc"), ["renamed_doc.txt"])

    def test_must_scan_rescans_the_folder_and_drops_vanished_files(self):
        (self.root / "docs" / "b.txt").write_text("x")
        (self.root / "docs" / "a.txt").unlink()
        self.apply({str(self.root / "docs"): mi.MUST_SCAN})
        self.assertEqual(self.names("b.txt"), ["b.txt"])
        self.assertEqual(self.names("^a.txt$"), [])

    def test_events_for_private_paths_are_dropped(self):
        ssh = self.root / ".ssh"
        ssh.mkdir()
        (ssh / "id_ed25519").write_text("k")
        env = self.root / "docs" / ".env"
        env.write_text("A=1")
        out = self.apply({str(ssh / "id_ed25519"): mi.F_CREATED, str(ssh): mi.F_CREATED | mi.F_ISDIR, str(env): mi.F_CREATED})
        self.assertEqual(out["upserted"], 0)
        self.assertEqual(self.names("id_ed25519"), [])
        self.assertEqual(self.names(".env"), [])

    def test_manager_queue_drops_blocked_paths_before_they_wait(self):
        m = mi.MacIndex(self.store)
        m.feed_event(str(self.root / ".ssh" / "id_rsa"), mi.F_CREATED, self.excl)
        m.feed_event(str(self.root / "docs" / "c.txt"), mi.F_CREATED, self.excl)
        m.feed_event(str(self.root / "docs" / "c.txt"), 0x1000, self.excl)
        self.assertEqual(list(m.pending), [str(self.root / "docs" / "c.txt")])
        self.assertEqual(m.pending[str(self.root / "docs" / "c.txt")], mi.F_CREATED | 0x1000)

    def test_changes_are_visible_within_a_second(self):
        m = mi.MacIndex(self.store)
        with mock.patch.object(mi.MacIndex, "excluder", return_value=self.excl):
            threading.Thread(target=m._flush_loop, daemon=True).start()
            f = self.root / "docs" / "instant_find.txt"
            f.write_text("x")
            t0 = time.time()
            m.feed_event(str(f), mi.F_CREATED, self.excl)
            while time.time() - t0 < 3 and not self.names("instant_find"):
                time.sleep(0.02)
            m.stop_flag.set()
        self.assertLess(time.time() - t0, 1.0)
        self.assertEqual(self.names("instant_find"), ["instant_find.txt"])


class CrawlManager(unittest.TestCase):
    """A full crawl of a temporary tree through the manager (Python walker: the helper is not needed)."""

    def setUp(self):
        self.root = Path(mkdtemp(prefix="mse-mi-crawl-")).resolve()
        for rel in ("a/one.txt", "a/b/two.txt", "c/three.txt", "top.txt", ".ssh/id_rsa", "c/node_modules/x/y.js"):
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        self.store = mi.Store(Path(mkdtemp(prefix="mse-mi-crawldb-")) / "m.db")
        self.cfg = {**mi.DEFAULT_CONFIG, "roots": [str(self.root)], "exclude": [], "skip_protected": "never"}
        self.m = mi.MacIndex(self.store)
        self.patches = [mock.patch.object(mi, "settings", lambda: self.cfg),
                        mock.patch.object(mi, "helper_path", lambda: Path("/nonexistent/msehelper")),
                        mock.patch.object(mi.MacIndex, "fda", lambda s: True)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def crawl(self, force=False):
        self.m.force = force
        self.m.crawling = True
        self.m.progress = {"phase": "x", "units_done": 0, "units_total": 0, "entries": 0, "fraction": 0, "current": None,
                           "started_at": time.time()}
        self.m._crawl_main()

    def names(self):
        with self.store.read() as c:
            return sorted(r[0].replace(str(self.root), "") for r in c.execute("SELECT path FROM files"))

    def test_crawl_indexes_the_tree_and_skips_private_and_noise(self):
        self.crawl()
        self.assertEqual(self.names(), ["", "/a", "/a/b", "/a/b/two.txt", "/a/one.txt", "/c", "/c/three.txt", "/top.txt"])
        st = json.loads(self.store.get_meta("crawl"))
        self.assertTrue(st["finished"])
        self.assertFalse(self.m.crawling)

    def test_an_empty_index_before_the_first_crawl_is_not_searchable(self):
        """Between the service start and the first crawl (~20 s) nothing is indexed: "searchable: false", never an empty
        "no matches" the app would show for minutes."""
        r = self.m.search("three")
        self.assertFalse(r["searchable"])
        self.assertFalse(self.m.status()["searchable"])
        self.crawl()
        self.assertTrue(self.m.search("three")["searchable"])
        self.assertTrue(self.m.status()["searchable"])

    def test_first_crawl_is_a_bulk_load_that_builds_the_index_at_the_end(self):
        seen = []
        real = mi.ingest

        def spy(store, rows, gen, excl, **kw):
            seen.append((store.in_bulk(), self.m.search("three")["items"], self.m.status()["searchable"]))
            return real(store, rows, gen, excl, **kw)
        with mock.patch.object(mi, "ingest", spy):
            self.crawl()
        self.assertTrue(seen)
        self.assertTrue(all(b and items == [] and not ok for b, items, ok in seen))     # nothing to search mid-load
        self.assertFalse(self.store.in_bulk())
        self.assertEqual([i["name"] for i in self.m.search("three")["items"]], ["three.txt"])
        with self.store.read() as c:
            kinds = {(r[0], r[1]) for r in c.execute("SELECT type, name FROM sqlite_master")}
        self.assertIn(("trigger", "files_ai"), kinds)
        self.assertIn(("index", "files_nf"), kinds)
        # from now on a new file is searchable at once (the triggers keep the trigram index current)
        (self.root / "c" / "later_arrival.txt").write_text("x")
        mi.apply_events(self.store, {str(self.root / "c" / "later_arrival.txt"): mi.F_CREATED}, self.m.excluder())
        self.assertEqual([i["name"] for i in self.m.search("later_arrival")["items"]], ["later_arrival.txt"])

    def test_a_load_interrupted_in_bulk_mode_stays_in_it_after_a_restart(self):
        self.store.begin_bulk()
        self.store.close()
        again = mi.Store(self.store.path)
        again.writer()
        self.assertTrue(again.in_bulk())
        with again.read() as c:
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type IN ('index', 'trigger')")}
        self.assertNotIn("files_nf", names)
        self.assertNotIn("files_ai", names)
        again.end_bulk()
        self.assertFalse(again.in_bulk())

    def test_status_never_waits_for_a_long_write(self):
        self.crawl()
        done = []
        with self.store.wlock:                                  # e.g. the index build at the end of the first load
            def poll():
                self.m.status()
                self.m.crawl_due(self.cfg)
                done.append(time.time())
            t0 = time.time()
            th = threading.Thread(target=poll)
            th.start()
            th.join(2.0)
            finished = not th.is_alive()
        th.join()
        self.assertTrue(finished)
        self.assertLess(done[0] - t0, 0.5)

    def test_rows_of_a_removed_root_are_deleted(self):
        other = Path(mkdtemp(prefix="mse-mi-crawl-b-")).resolve()
        (other / "elsewhere.txt").write_text("x")
        self.cfg = {**self.cfg, "roots": [str(self.root), str(other)]}
        self.m._excl = None
        self.crawl()
        with self.store.read() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM files WHERE path LIKE ?", (str(other) + "%",)).fetchone()[0], 2)
        self.cfg = {**self.cfg, "roots": [str(self.root)]}
        self.m._excl = None
        self.assertTrue(self.m.crawl_due(self.cfg))             # the roots are part of the rules hash
        self.crawl()
        with self.store.read() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM files WHERE path LIKE ?", (str(other) + "%",)).fetchone()[0], 0)
            self.assertGreater(c.execute("SELECT count(*) FROM files WHERE path LIKE ?", (str(self.root) + "%",)).fetchone()[0], 5)
        self.assertEqual([i["name"] for i in self.m.search("elsewhere")["items"]], [])

    def test_a_recrawl_updates_what_changed_and_keeps_the_indexes_right(self):
        self.crawl()
        f = self.root / "a" / "one.txt"
        f.write_text("now much longer than before " * 40)
        os.utime(f, (NOW, NOW))
        self.crawl(force=True)
        with self.store.read() as c:
            size, mtime = c.execute("SELECT size, mtime FROM files WHERE path = ?", (str(f),)).fetchone()
        self.assertEqual((size, mtime), (len("now much longer than before " * 40), NOW))
        q = fq.parse_query("one size:>1kb")
        with self.store.read() as c:
            self.assertEqual([i["name"] for i in fq.search(c, q)["items"]], ["one.txt"])
            gens = {r[0] for r in c.execute("SELECT gen FROM files WHERE path LIKE ?", (str(self.root) + "/a%",))}
        self.assertEqual(len(gens), 1)                           # every row was re-stamped by the second crawl

    def test_watch_excludes_are_existing_folders_inside_the_roots_and_at_most_eight(self):
        (self.root / "Library").mkdir()
        (self.root / ".ssh").mkdir(exist_ok=True)
        self.cfg = {**self.cfg, "exclude": [str(self.root / "Library")]}
        self.m._excl = None
        with mock.patch.object(mi.Excluder, "__init__", wraps=None) if False else mock.patch("os.path.isdir", os.path.isdir):
            ex = self.m.watch_excludes([str(self.root)])
        self.assertEqual(ex[0], str(self.root / "Library"))       # the soft exclusion first
        self.assertTrue(all(p.startswith(str(self.root) + "/") and os.path.isdir(p) for p in ex))
        self.assertLessEqual(len(ex), 8)
        # a root inside an excluded folder is wanted, not excluded
        self.cfg = {**self.cfg, "roots": [str(self.root), str(self.root / "Library" / "CloudStorage")]}
        (self.root / "Library" / "CloudStorage").mkdir()
        self.m._excl = None
        self.assertNotIn(str(self.root / "Library"), self.m.watch_excludes([str(self.root), str(self.root / "Library" / "CloudStorage")]))

    def test_second_crawl_sweeps_deleted_files_and_picks_up_new_ones(self):
        self.crawl()
        (self.root / "a" / "one.txt").unlink()
        (self.root / "c" / "four.txt").write_text("x")
        (self.root / "toplevel_new.txt").write_text("x")
        self.crawl(force=True)
        names = self.names()
        self.assertNotIn("/a/one.txt", names)
        self.assertIn("/c/four.txt", names)
        self.assertIn("/toplevel_new.txt", names)

    def test_interrupted_crawl_resumes_and_does_not_redo_finished_units(self):
        calls = []
        real = mi.walk_python

        def spy(path, excl, max_depth=0):
            calls.append((path, max_depth))
            return real(path, excl, max_depth)
        with mock.patch.object(mi, "walk_python", spy):
            stop = {"n": 0}

            real_ingest = mi.ingest

            def stopping(store, rows, gen, excl, **kw):
                stop["n"] += 1
                if stop["n"] == 3:
                    self.m.crawl_stop.set()
                return real_ingest(store, rows, gen, excl, **kw)
            with mock.patch.object(mi, "ingest", stopping):
                self.crawl()
            self.assertIsNone(json.loads(self.store.get_meta("crawl"))["finished"])
            done_first = len(json.loads(self.store.get_meta("crawl"))["done"])
            self.assertGreaterEqual(done_first, 2)
            n_first = len(calls)
            self.m.crawl_stop.clear()
            self.crawl()
        self.assertTrue(json.loads(self.store.get_meta("crawl"))["finished"])
        self.assertLess(len(calls) - n_first, n_first + 4)                     # only the unfinished units ran again
        self.assertIn("/c/three.txt", self.names())

    def fake_helper(self, body):
        h = Path(mkdtemp(prefix="mse-mi-fakehelper-")) / "msehelper"
        h.write_text("#!/bin/sh\n" + body)
        h.chmod(0o755)
        return h

    def test_an_old_helper_without_crawl_is_not_asked_to_crawl(self):
        old = self.fake_helper("echo '{\"error\": \"unknown command\"}'; exit 1\n")
        with mock.patch.object(mi, "helper_path", lambda: old):
            self.assertEqual(mi.helper_features(), set())
            self.crawl()                                          # falls back to the Python walker
        self.assertIn("/c/three.txt", self.names())

    def test_a_crawl_that_dies_midway_never_sweeps_and_backs_off(self):
        self.crawl()
        before = self.names()
        broken = self.fake_helper('case "$1" in features) echo \'{"crawl": true}\';; '
                                  'crawl) echo \'{"path": "' + str(self.root) + '/a/one.txt", "is_dir": false}\'; exit 1;; esac\n')
        with mock.patch.object(mi, "helper_path", lambda: broken):
            self.assertEqual(mi.helper_features(), {"crawl"})
            self.crawl(force=True)
        self.assertEqual(self.names(), before)                    # nothing deleted because a walk failed
        self.assertIn("did not finish", self.m.last_error or "")
        self.assertFalse(self.m.crawl_due(self.cfg))              # and it waits before trying again
        self.m.retry_at = 0

    def test_rules_change_forces_a_new_crawl_and_a_purge(self):
        self.crawl()
        self.assertFalse(self.m.crawl_due(self.cfg) and json.loads(self.store.get_meta("crawl")).get("finished") is None)
        self.cfg = {**self.cfg, "exclude": [str(self.root / "c")]}
        self.m._excl = None
        self.assertTrue(self.m.crawl_due(self.cfg))
        self.crawl()
        self.assertNotIn("/c/three.txt", self.names())

    def test_status_shape(self):
        self.crawl()
        st = self.m.status()
        self.assertEqual(set(st), {"enabled", "roots", "entries", "crawling", "progress", "last_update", "full_disk_access",
                                   "excluded", "state", "last_crawl", "helper_present", "watching", "events",
                                   "skipped_protected", "denied", "error", "searchable", "helper_features",
                                   "helper_outdated", "python_path", "python_real_path", "exclude_entries",
                                   "hard_excluded"})
        self.assertEqual((st["enabled"], st["roots"], st["crawling"], st["progress"], st["state"]),
                         (True, [str(self.root)], False, None, "ready"))
        self.assertEqual(st["entries"], 8)
        self.assertIs(st["full_disk_access"], True)
        self.assertIn("~/.ssh/", st["excluded"])
        self.assertIn("node_modules/", st["excluded"])
        self.assertIn("**/*credential*", st["excluded"])

    def test_disabled_does_not_start(self):
        self.cfg = {**self.cfg, "enabled": False}
        self.assertEqual(self.m.start_crawl(), {"started": False, "reason": "disabled"})


@unittest.skipUnless(os.environ.get("MSE_TEST_HELPER") and os.path.exists(os.environ.get("MSE_TEST_HELPER", "")),
                     "set MSE_TEST_HELPER to a built msehelper to test the native crawl and FSEvents on a temp folder")
class HelperCrawl(unittest.TestCase):
    def test_native_crawl_matches_the_python_walker_and_prunes(self):
        root = Path(mkdtemp(prefix="mse-mi-nat-")).resolve()
        for rel in ("a/one.txt", "a/b/two.txt", ".ssh/id_rsa", "x/node_modules/y.js", "x/my_secret.txt", "x/ok.txt", "p.app/Contents/z.txt"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("hello")
        (root / "link").symlink_to(root / "a")
        excl = mi.Excluder({"exclude": []}, h=str(root))
        with mock.patch.object(mi, "helper_path", lambda: Path(os.environ["MSE_TEST_HELPER"])):
            stats = {}
            native = {d["path"].replace(str(root), ""): d for d in mi.helper_rows(str(root), excl, stats=stats)}
        # what the helper lets through, minus the secrets the Python side removes anyway
        kept = {p for p in native if not excl.blocked(str(root) + p)}
        py = {d["path"].replace(str(root), "") for d in mi.walk_python(str(root), excl)}
        self.assertEqual(kept, py)
        self.assertNotIn("/.ssh", native)                      # pruned before listing: not even the folder
        self.assertNotIn("/x/node_modules", native)
        self.assertNotIn("/x/my_secret.txt", native)
        self.assertIn("/p.app", native)
        self.assertNotIn("/p.app/Contents", native)            # bundles are not entered
        self.assertFalse(native["/link"]["is_dir"])            # symlinks are listed, not followed
        self.assertNotIn("/link/one.txt", native)
        self.assertEqual(native["/a/one.txt"]["size"], 5)
        self.assertEqual(stats.get("entries"), len(native))
        # a shallow listing
        with mock.patch.object(mi, "helper_path", lambda: Path(os.environ["MSE_TEST_HELPER"])):
            shallow = {d["path"].replace(str(root), "") for d in mi.helper_rows(str(root), excl, max_depth=1)}
        self.assertIn("/a", shallow)
        self.assertNotIn("/a/one.txt", shallow)

    def test_watch_exclude_keeps_a_folder_out_of_the_event_stream(self):
        import subprocess
        root = Path(mkdtemp(prefix="mse-mi-wx-")).resolve()
        (root / "noisy").mkdir()
        (root / "quiet").mkdir()
        proc = subprocess.Popen([os.environ["MSE_TEST_HELPER"], "watch", str(root), "--latency", "0.2",
                                 "--exclude", str(root / "noisy")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            first = json.loads(proc.stdout.readline())
            self.assertEqual(first["excluded"], [str(root / "noisy")])
            time.sleep(0.8)
            (root / "noisy" / "churn.txt").write_text("x")
            (root / "quiet" / "signal.txt").write_text("x")
            paths_ = []
            t0 = time.time()
            import select
            while time.time() - t0 < 2.5:
                if select.select([proc.stdout], [], [], 0.2)[0]:
                    paths_.append(json.loads(proc.stdout.readline())["path"])
            self.assertTrue(any(p.endswith("signal.txt") for p in paths_), paths_)
            self.assertFalse(any("/noisy/" in p for p in paths_), paths_)      # (the folder itself may still be reported)
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)
            proc.stdout.close()

    def test_fsevents_reach_the_index_within_a_second(self):
        root = Path(mkdtemp(prefix="mse-mi-fse-")).resolve()
        store = mi.Store(Path(mkdtemp(prefix="mse-mi-fsedb-")) / "m.db")
        excl = mi.Excluder({"exclude": []}, h=str(root))
        m = mi.MacIndex(store)
        cfg = {**mi.DEFAULT_CONFIG, "roots": [str(root)], "exclude": [], "skip_protected": "never"}
        with mock.patch.object(mi, "settings", lambda: cfg), \
                mock.patch.object(mi, "helper_path", lambda: Path(os.environ["MSE_TEST_HELPER"])), \
                mock.patch.object(mi.MacIndex, "fda", lambda s: True):
            threading.Thread(target=m._flush_loop, daemon=True).start()
            m._watch(cfg)
            time.sleep(1.0)                                    # the stream starts "since now"
            f = root / "watched_new_file.txt"
            f.write_text("x")
            t0 = time.time()
            found = False
            while time.time() - t0 < 4 and not found:
                with store.read() as c:
                    found = bool(c and fq.search(c, fq.parse_query("watched_new_file"))["items"])
                time.sleep(0.05)
            dt = time.time() - t0
            m.stop()
        self.assertTrue(found)
        self.assertLess(dt, 2.0)
        print(f"\n  FSEvents -> index latency: {dt * 1000:.0f} ms")


# ====================================================================== the HTTP API

class _Jobs:
    def submit(self, kind, collection=None, path=None, reason="manual"):
        return {"jobs": []}

    def status(self):
        return {"current": None, "queued": []}


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from mse import server
        cls.server_mod, cls.old_jobs = server, server.JOBS
        server.JOBS = _Jobs()
        cls.work = Path(mkdtemp(prefix="mse-mi-api-work-")).resolve()
        cls.other = Path(mkdtemp(prefix="mse-mi-api-other-")).resolve()
        for rel in ("Clients/Acme/contract.pdf", "Clients/Acme/notes.md", "Clients/old/contract_2020.pdf",
                    ".git/config", "Clients/Acme/.env"):
            p = cls.work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        (cls.work / "Clients" / "link").symlink_to(cls.work / "Clients" / "Acme")
        for n, d in (("mi-api-work", cls.work), ("mi-api-other", cls.other)):
            if not config.get_collection(config.load(), n):
                config.add_collection(str(d), n)
        config.update_collection("mi-api-work", {"aliases": ["miw"], "exclude": ["Clients/old/"]})
        config.save_group("MI Group", ["mi-api-work"], "mi-group")
        # the privacy rules' editable part is set here, explicitly, so these tests do not depend on the shipped default
        cls.saved_exclude = config.load()["mac_index"]["exclude"]
        cls.mac_rules = ALPHA_RULES
        config.update_settings({"mac_index": cls.mac_rules})
        cls.mgr = mi.manager()
        cls.mgr._excl = None
        rows = [fake(str(cls.work / "Clients/Acme/contract.pdf")), fake(str(cls.work / "Clients/Acme/notes.md")),
                fake(str(cls.work / "Clients/old/contract_2020.pdf")), fake(str(cls.other / "contract_other.pdf")),
                fake(str(cls.work / "Clients/Acme"), is_dir=True), fake(f"{os.path.expanduser('~')}/Documents/Projects/Alpha/contract.txt"),
                fake("/tmp/elsewhere/contract_elsewhere.pdf")]
        cls.rows_stored = mi.ingest(cls.mgr.store, iter(rows), cls.mgr.store.next_gen(), mi.Excluder(cls.mac_rules, h=os.path.expanduser("~")))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server_mod.JOBS = cls.old_jobs
        config.remove_group("mi-group")
        for n in ("mi-api-work", "mi-api-other"):
            config.remove_collection(n)

    def req(self, method, path, body=None, params=None):
        url = f"http://127.0.0.1:{self.port}{path}" + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-MSE-Token": config.load()["token"], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_search_shape_and_defaults(self):
        code, res = self.req("GET", "/api/files/search", params={"q": "contract"})
        self.assertEqual(code, 200)
        self.assertEqual(set(res), {"items", "total", "ms", "scope", "query", "truncated", "searchable"})
        self.assertTrue(res["searchable"])
        self.assertEqual((res["scope"], res["total"]), ("mac", 4))
        it = res["items"][0]
        self.assertEqual(set(it) - {"collection"}, {"path", "name", "parent", "is_dir", "kind", "ext", "size", "mtime", "ctime",
                                                    "score", "match"})
        self.assertEqual(res["query"], {"words": [{"text": "contract", "mode": "fuzzy", "not": False}], "filters": {}})
        self.assertIsInstance(res["ms"], float)
        names = {x["path"]: x.get("collection") for x in res["items"]}
        self.assertEqual(names[str(self.work / "Clients/Acme/contract.pdf")], "mi-api-work")
        self.assertIsNone(names["/tmp/elsewhere/contract_elsewhere.pdf"])
        self.assertFalse(any("/Projects/Alpha/" in p for p in names))                
        self.assertNotIn(str(self.work / ".git/config"), names)

    def test_scope_indexed_restricts_to_collection_folders(self):
        code, res = self.req("GET", "/api/files/search", params={"q": "contract", "scope": "indexed"})
        paths_ = {x["path"] for x in res["items"]}
        self.assertEqual(res["scope"], "indexed")
        self.assertNotIn("/tmp/elsewhere/contract_elsewhere.pdf", paths_)
        self.assertIn(str(self.other / "contract_other.pdf"), paths_)
        code, res = self.req("GET", "/api/files/search", params={"q": "contract", "scope": "indexed", "c": "mi-api-other"})
        self.assertEqual([x["name"] for x in res["items"]], ["contract_other.pdf"])
        code, res = self.req("GET", "/api/files/search", params={"q": "contract", "scope": "indexed", "c": "mi-group"})
        self.assertEqual({x["collection"] for x in res["items"]}, {"mi-api-work"})
        code, res = self.req("GET", "/api/files/search", params={"q": "contract", "scope": "indexed", "c": "miw"})   # an alias
        self.assertEqual({x["collection"] for x in res["items"]}, {"mi-api-work"})

    def test_scope_shortcut_in_the_query(self):
        code, res = self.req("GET", "/api/files/search", params={"q": "mi-api-other: contract"})
        self.assertEqual((res["scope"], [x["name"] for x in res["items"]]), ("indexed", ["contract_other.pdf"]))
        self.assertEqual(res["shortcut"], {"names": ["mi-api-other"], "tokens": ["mi-api-other"]})
        code, res = self.req("GET", "/api/files/search", params={"q": "contract in:miw"})
        self.assertEqual({x["collection"] for x in res["items"]}, {"mi-api-work"})

    def test_folder_path_in_the_query(self):
        q = f'contract in:"{self.work}/Clients"'
        code, res = self.req("GET", "/api/files/search", params={"q": q})
        self.assertEqual(code, 200)
        self.assertEqual(sorted(x["name"] for x in res["items"]), ["contract.pdf", "contract_2020.pdf"])
        self.assertEqual(res["scope"], "mac")
        code, res = self.req("GET", "/api/files/search", params={"q": "contract in:~/Documents/Projects/Alpha"})
        self.assertEqual(res["items"], [])                                       # and the folder stays private

    def test_errors_are_400(self):
        for params in ({"q": "x", "scope": "everywhere"}, {"q": "re:("}, {"q": "size:>x"}, {"q": "x in:ghost"},
                       {"q": "x", "scope": "indexed", "c": "ghost"}):
            code, err = self.req("GET", "/api/files/search", params=params)
            self.assertEqual(code, 400, params)
            self.assertIn("error", err)

    def test_limit(self):
        code, res = self.req("GET", "/api/files/search", params={"q": "contract", "limit": 2})
        self.assertEqual((len(res["items"]), res["total"]), (2, 4))

    def test_status_shape(self):
        code, st = self.req("GET", "/api/files/status")
        self.assertEqual(code, 200)
        for k in ("enabled", "roots", "entries", "crawling", "progress", "last_update", "full_disk_access", "excluded"):
            self.assertIn(k, st)
        self.assertIsInstance(st["enabled"], bool)
        self.assertIsInstance(st["roots"], list)
        self.assertIsInstance(st["entries"], int)
        self.assertIn(st["full_disk_access"], (True, False, None))
        self.assertTrue(any(p.endswith(".ssh/") for p in st["excluded"]))
        self.assertTrue(any("Projects/Alpha" in p for p in st["excluded"]))

    def test_crawl_endpoint_starts_a_crawl(self):
        with mock.patch.object(mi.MacIndex, "start_crawl", return_value={"started": True}) as start:
            code, res = self.req("POST", "/api/files/crawl", {"force": True})
        self.assertEqual((code, res), (200, {"started": True}))
        start.assert_called_once_with(force=True)

    def test_settings_roundtrip(self):
        code, res = self.req("POST", "/api/settings", {"mac_index": {"rescan_every_h": 12}})
        self.assertEqual(code, 200)
        self.assertEqual(config.load()["mac_index"]["rescan_every_h"], 12)
        code, err = self.req("POST", "/api/settings", {"mac_index": {"enabled": "no"}})
        self.assertEqual(code, 400)
        config.update_settings({"mac_index": {"rescan_every_h": 24}})

    # ---- /api/tree

    def test_tree_lists_immediate_subfolders_and_hides_excluded(self):
        code, res = self.req("GET", "/api/tree", params={"c": "mi-api-work"})
        self.assertEqual(code, 200)
        self.assertEqual(res, {"collection": "mi-api-work", "path": "",
                               "folders": [{"name": "Clients", "path": "Clients", "children": True}]})
        code, res = self.req("GET", "/api/tree", params={"c": "mi-api-work", "path": "Clients"})
        names = [f["name"] for f in res["folders"]]
        self.assertEqual(names, ["Acme"])                       # old/ is excluded by the rules; link is a symlink
        self.assertEqual(res["folders"][0], {"name": "Acme", "path": "Clients/Acme", "children": False})
        code, res = self.req("GET", "/api/tree", params={"c": "miw", "path": "/Clients/"})       # alias, slashes tolerated
        self.assertEqual((code, res["path"]), (200, "Clients"))

    def test_tree_refuses_escapes_excluded_folders_and_links(self):
        for path in ("..", "Clients/../..", "../x", "/etc", "Clients/old", ".git", "Clients/link", "nope"):
            code, res = self.req("GET", "/api/tree", params={"c": "mi-api-work", "path": path})
            self.assertIn(code, (400, 404), path)
            self.assertNotIn("folders", res, path)
        code, _ = self.req("GET", "/api/tree", params={"c": "ghost"})
        self.assertEqual(code, 404)

    def test_tree_never_lists_private_names(self):
        (self.work / "Clients" / "my_secret_stuff").mkdir(exist_ok=True)
        (self.work / "Clients" / ".hidden").mkdir(exist_ok=True)
        code, res = self.req("GET", "/api/tree", params={"c": "mi-api-work", "path": "Clients"})
        self.assertEqual([f["name"] for f in res["folders"]], ["Acme"])


# ====================================================================== hybrid search with a folder path

class HybridFolderFilter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from mse import indexer
        cls.root = Path(mkdtemp(prefix="mse-mi-hy-")).resolve()
        for rel in ("alpha/invoice one.txt", "beta/invoice two.txt", "beta/sub/invoice three.txt"):
            p = cls.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("invoice " + rel)
        cls.coll = config.add_collection(str(cls.root), "mi-hybrid")
        indexer.scan(cls.coll)

    @classmethod
    def tearDownClass(cls):
        config.remove_collection("mi-hybrid")

    def names(self, q, **kw):
        res = search.search(q, mode="keyword", collections=["mi-hybrid"], **kw)
        return sorted(r["path"] for r in res["results"])

    def test_folder_path_filters_like_a_path_prefix(self):
        self.assertEqual(len(self.names("invoice")), 3)
        self.assertEqual(self.names(f"invoice in:{self.root}/beta"), ["beta/invoice two.txt", "beta/sub/invoice three.txt"])
        self.assertEqual(self.names(f'invoice in:"{self.root}/beta/sub"'), ["beta/sub/invoice three.txt"])
        self.assertEqual(self.names(f"invoice in:{self.root}"), self.names("invoice"))               # the whole collection
        self.assertEqual(self.names(f"invoice in:{self.root.parent}"), self.names("invoice"))       # a parent folder
        self.assertEqual(self.names("invoice in:/nowhere/else"), [])                                 # never widens
        self.assertEqual(self.names(f"invoice in:{self.root}/alpha", filters={"path": ["beta"]}), [])   # path filters AND with it

    def test_whole_folders_only_for_asks_subfolder_scope(self):
        """Asking inside 'Acme' must not read 'Acme_old' (nor 'Acme' read 'Acme Group')."""
        for rel in ("Acme/plan invoice.txt", "Acme_old/plan invoice.txt", "Acme/deep/plan invoice.txt"):
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("plan invoice " + rel)                    # different bytes: copies would fold into one result
        from mse import indexer
        indexer.scan(self.coll)
        every = self.names("plan")
        self.assertEqual(len(every), 3)
        under = lambda *rel: {"_under": [list(rel)]}
        self.assertEqual(self.names("plan", filters=under("Acme/")), ["Acme/deep/plan invoice.txt", "Acme/plan invoice.txt"])
        self.assertEqual(self.names("plan", filters=under("acme/deep/")), ["Acme/deep/plan invoice.txt"])
        self.assertEqual(self.names("plan", filters=under("Acme/", "Acme_old/")), every)
        # the old bare-prefix behaviour of path: is unchanged
        self.assertEqual(len(self.names("plan", filters={"path": ["Acme"]})), 3)
        # and it ANDs with an in: folder
        self.assertEqual(self.names(f"plan in:{self.root}/Acme/deep", filters=under("Acme/")),
                         ["Acme/deep/plan invoice.txt"])
        res = search.search("plan", mode="keyword", collections=["mi-hybrid"], filters=under("Acme/"))
        text = json.dumps(res, default=str)
        self.assertNotIn("_under", text)                          # internals never reach the result JSON
        self.assertNotIn("_collection", text)

    def test_api_search_takes_a_folder_in_the_query_string_or_the_query(self):
        from http.server import ThreadingHTTPServer
        from mse import server
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def get(**params):
                url = f"http://127.0.0.1:{httpd.server_address[1]}/api/search?" + urllib.parse.urlencode(params, doseq=True)
                r = urllib.request.Request(url, headers={"X-MSE-Token": config.load()["token"]})
                with urllib.request.urlopen(r, timeout=30) as resp:
                    return json.loads(resp.read())
            a = get(q="invoice", c="mi-hybrid", mode="keyword", **{"in": f"{self.root}/beta"})
            b = get(q=f'invoice in:"{self.root}/beta"', c="mi-hybrid", mode="keyword")
            self.assertEqual([r["path"] for r in a["results"]], [r["path"] for r in b["results"]])
            self.assertTrue(a["results"] and all(r["path"].startswith("beta/") for r in a["results"]))
            self.assertNotIn("_under", json.dumps(a["filters"]))
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_names_still_pick_collections(self):
        res = search.search("invoice in:mi-hybrid", mode="keyword", collections=None)
        self.assertEqual(res["collections"], ["mi-hybrid"])


# ====================================================================== Ask: step events and the subfolder scope

ENABLED = {**config.DEFAULTS, "ask": {"enabled": True, "route": "local"}}


def _result(path, docid):
    return {"collection": "unit", "path": path, "docid": docid, "title": path, "name": path, "date": "2025-04-09",
            "relevance": 100, "metadata_only": False, "hits": [], "copies": []}


def _chunks(r):
    return [{"path": r["path"], "docid": r["docid"], "modality": "text", "page": None, "t0": None, "t1": None,
             "heading": None, "text": "The supplier invoice was paid on 9 April 2025."}]


def _gen(tokens):
    def g(messages, **params):
        for t in tokens:
            yield {"token": t}
        yield {"done": True}
    return g


class AskSteps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(mkdtemp(prefix="mse-mi-ask-"))
        config.add_collection(str(cls.dir), "mi-ask")

    @classmethod
    def tearDownClass(cls):
        config.remove_collection("mi-ask")

    def run_ask(self, q="When was the invoice paid?", results=None, steps=True, filters=None, agent_safe=False,
                collections=None, **kw):
        results = [_result("a/n.md", "aaaa0001"), _result("a/m.md", "aaaa0002"), _result("b/c.md", "bbbb0003")] \
            if results is None else results
        search_fn = lambda query, colls, flt, limit: {"results": results, "bridge": {}}
        return list(ask.ask(q, collections=collections, filters=filters, agent_safe=agent_safe, search_fn=search_fn,
                            chunks_fn=_chunks, gen_fn=_gen(["Paid [1]."]),
                            cfg={**ENABLED, "collections": config.load()["collections"]}, steps=steps, **kw))

    def test_steps_come_as_the_work_happens(self):
        ev = self.run_ask()
        kinds = [(e["type"], e.get("kind")) for e in ev]
        self.assertEqual(kinds[:6], [("step", "search"), ("step", "search"), ("step", "read"), ("sources", None),
                                     ("step", "model"), ("token", None)])
        self.assertEqual(kinds[-1], ("done", None))
        steps = [e for e in ev if e["type"] == "step"]
        self.assertEqual(steps[0]["text"], "Searching All for “When was the invoice paid?”")
        self.assertEqual((steps[1]["text"], steps[1]["count"]), ("Found 3 results", 3))
        self.assertIsInstance(steps[1]["ms"], int)
        self.assertEqual((steps[2]["text"], steps[2]["count"]), ("Reading 3 passages from 3 documents", 3))
        self.assertTrue(steps[3]["text"].startswith("Asking the local model"))
        for s in steps:
            self.assertEqual(set(s) - {"count", "ms"}, {"type", "kind", "text"})

    def test_off_by_default_so_plain_streams_are_unchanged(self):
        ev = self.run_ask(steps=False)
        self.assertEqual([e["type"] for e in ev], ["sources", "token", "done"])

    def test_scope_and_subfolder_in_the_search_step(self):
        ev = self.run_ask(collections=["mi-ask"], filters={"_under": [["Acme Group/"]]})
        self.assertEqual(ev[0]["text"], "Searching mi-ask › Acme Group for “When was the invoice paid?”")
        ev = self.run_ask(filters={"path": ["Acme Group/04_Docs"]})
        self.assertEqual(ev[0]["text"], "Searching All › Acme Group › 04_Docs for “When was the invoice paid?”")
        ev = self.run_ask(filters={"in": ["~/Documents/Projects/Alpha"]})
        self.assertIn("Home › Documents › Projects › Alpha", ev[0]["text"])

    def test_one_result_and_no_result_wording_and_no_model_step_without_passages(self):
        ev = self.run_ask(results=[_result("a/n.md", "aaaa0001")])
        steps = [e["text"] for e in ev if e["type"] == "step"]
        self.assertEqual(steps[1], "Found 1 result")
        self.assertEqual(steps[2], "Reading 1 passage from 1 document")
        ev = self.run_ask(results=[])
        self.assertEqual([(e["kind"], e["text"]) for e in ev if e["type"] == "step"],
                         [("search", "Searching All for “When was the invoice paid?”"), ("search", "Found 0 results")])
        self.assertEqual(ev[-1]["answered"], False)

    def test_model_step_names_provider_and_model(self):
        who = ask._model_text
        self.assertEqual(who({"where": "cloud", "provider": "acme", "model": "acme-large-train"}),
                         "Asking acme/acme-large-train")
        self.assertEqual(who({"where": "cloud", "provider": "localrouter", "model": "deepseek/deepseek-chat"}),
                         "Asking deepseek/deepseek-chat")
        self.assertEqual(who({"where": "local", "provider": "local", "model": "qwen"}), "Asking the local model qwen")

    def test_cloud_route_names_the_cloud_model(self):
        cfg = {**config.DEFAULTS, "ask": {"enabled": True, "route": "cloud", "provider": "acme",
                                           "model": "acme-large-train",
                                           "allow_training_models": ["acme-large-train"]}}

        def cloud(messages, **p):
            yield {"token": "Paid [1]."}
            yield {"done": True}
        search_fn = lambda query, colls, flt, limit: {"results": [_result("a/n.md", "aaaa0001")], "bridge": {}}
        ev = list(ask.ask("When was it paid?", search_fn=search_fn, chunks_fn=_chunks, cloud_fn=cloud, cfg=cfg, steps=True))
        model = [e for e in ev if e.get("kind") == "model"]
        self.assertEqual(len(model), 1)
        self.assertTrue(model[0]["text"].startswith("Asking "), model[0]["text"])
        self.assertIn("acme-large-train", model[0]["text"])

    def test_agent_safe_masks_the_query_in_the_transcript(self):
        iban = "DE89 3704 0044 0532 0130 00"
        ev = self.run_ask(q=f"Was {iban} the account?", agent_safe=True)
        self.assertNotIn("3704 0044", json.dumps([e for e in ev if e["type"] == "step"]))
        ev = self.run_ask(q=f"Was {iban} the account?", agent_safe=False)
        self.assertIn("3704 0044", ev[0]["text"])

    def test_rewrite_step_for_a_follow_up(self):
        ev = self.run_ask(q="and the office?", history=[{"q": "When was the supplier invoice paid?", "a": "On 9 April."}])
        kinds = [e["kind"] for e in ev if e["type"] == "step"]
        self.assertEqual(kinds[0], "rewrite")
        self.assertEqual(kinds[1:], ["search", "search", "read", "model"])


class AskHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from mse import server
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def post(self, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/ask", data=json.dumps(body).encode(), method="POST",
                                     headers={"X-MSE-Token": config.load()["token"], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_path_becomes_a_filter_and_steps_are_on(self):
        seen = {}

        def fake_ask(q, collections=None, filters=None, agent_safe=False, **kw):
            seen.update(filters=filters, steps=ask._STEPS.get())
            return iter([{"type": "step", "kind": "search", "text": "Searching All for “x”"},
                         {"type": "sources", "sources": []}, {"type": "token", "text": "ok"},
                         {"type": "done", "citations": [], "model": "m", "timings": {}}])
        with mock.patch.object(ask, "ask", side_effect=fake_ask):
            code, body = self.post({"q": "x", "path": "Acme Group/", "filters": {"type": "pdf"}})
            self.assertEqual(code, 200)
            self.assertEqual(seen["filters"], {"type": ["pdf"], "_under": [["Acme Group/"]]})
            self.assertTrue(seen["steps"])
            self.assertEqual(json.loads(body.splitlines()[0])["type"], "step")
            self.post({"q": "x", "path": ["~/Documents/Projects/Beta"]})
            self.assertEqual(seen["filters"], {"in": ["~/Documents/Projects/Beta"]})
            code, body = self.post({"q": "x", "path": "../etc"})
            self.assertEqual(code, 400)
            # the web UI's spelling: filters.path is the same whole-folder scope as a top-level path
            self.post({"q": "x", "filters": {"path": "Acme Group/04_Docs"}})
            self.assertEqual(seen["filters"], {"_under": [["Acme Group/04_Docs/"]]})
            self.post({"q": "x", "path": "A", "filters": {"path": ["B/C"], "type": "pdf"}})
            self.assertEqual(seen["filters"], {"type": ["pdf"], "_under": [["A/", "B/C/"]]})
            code, _ = self.post({"q": "x", "filters": {"path": "../etc"}})
            self.assertEqual(code, 400)


# ====================================================================== the agent boundary

class AgentBoundary(unittest.TestCase):
    def test_mcp_has_no_whole_mac_tool_and_no_route_to_it(self):
        names = {t["name"] for t in mcp.TOOLS}
        self.assertEqual(names, {"search", "get", "multi_get", "similar", "collections", "mark_best", "sync", "ask",
                                 "timeline", "facets"})
        self.assertFalse([n for n in names if "file" in n and n not in ("multi_get",)])
        src = Path(mcp.__file__).read_text()
        for forbidden in ("macindex", "filequery", "/api/files", "files_search"):
            self.assertNotIn(forbidden, src)
        for client in (mcp.HttpClient, mcp.LocalClient):
            self.assertFalse([m for m in dir(client) if "mac" in m.lower() or m.startswith("files")])
        self.assertEqual({t["name"] for t in mcp.tools_for(mcp.Lock("/tmp"))}, names)

    def test_locked_session_refuses_folders_outside_its_collection(self):
        inside = Path(mkdtemp(prefix="mse-mi-lock-")).resolve()
        config.add_collection(str(inside), "mi-locked")
        try:
            lock = mcp.Lock(str(inside))
            client = mock.Mock()
            client.search.return_value = {"query": "x", "collections": ["mi-locked"], "results": [], "bridge": {},
                                          "scope": {"label": "mi-locked"}, "timings": {"total_ms": 1}}
            call = lambda q: mcp.call_tool("search", {"query": q}, client, str(inside), lock)
            call(f"report in:{inside}/sub")
            call(f'report in:"{inside}/sub folder"')
            for outside in ("/etc", "~/Documents", str(inside) + "-sibling", "/"):
                with self.assertRaises(PermissionError, msg=outside):
                    call(f"report in:{outside}")
            with self.assertRaises(PermissionError):
                call('report in:"~/My Documents"')
            with self.assertRaises(PermissionError):
                mcp.call_tool("ask", {"question": "x in:/etc"}, client, str(inside), lock)
        finally:
            config.remove_collection("mi-locked")

    def run_cli(self, argv_ns, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}), redirect_stdout(out), redirect_stderr(err):
            try:
                cli.cmd_files(argv_ns)
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def ns(self, *words, **kw):
        return Namespace(words=list(words), scope=kw.get("scope", "mac"), collection=kw.get("collection"), limit=5,
                         force=False, json=kw.get("json", False), paths=False)

    def test_cli_refuses_whole_mac_under_mse_lock(self):
        inside = Path(mkdtemp(prefix="mse-mi-cli-")).resolve()
        config.add_collection(str(inside), "mi-cli")
        try:
            env = {"MSE_LOCK": "1", "MSE_CWD": str(inside)}
            code, out, err = self.run_cli(self.ns("report"), env)
            self.assertNotEqual(code, 0)
            self.assertIn("not available", str(code))
            code, _, _ = self.run_cli(self.ns("status"), env)
            self.assertIn("locked", str(code))
            code, _, _ = self.run_cli(self.ns("crawl"), env)
            self.assertIn("locked", str(code))
            code, _, _ = self.run_cli(self.ns("report in:/etc", scope="indexed"), env)
            self.assertIn("outside", str(code))
            # indexed search of its own collection works, and is pinned to it whatever -c says
            with mock.patch.object(cli, "daemon_up", return_value=False):
                code, out, _ = self.run_cli(self.ns("zzzz", scope="indexed", collection="mi-cli", json=True), env)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["scope"], "indexed")
            code, _, _ = self.run_cli(self.ns("report", scope="indexed", collection="all"), env)
            self.assertNotEqual(code, 0)
        finally:
            config.remove_collection("mi-cli")

    def test_piped_cli_output_masks_identifiers_like_every_agent_output(self):
        iban = "DE89 3704 0044 0532 0130 00"
        res = {"items": [{"path": f"/x/{iban}.pdf", "name": f"{iban}.pdf", "parent": "/x", "is_dir": False, "kind": "pdf",
                          "ext": "pdf", "size": 1, "mtime": NOW, "ctime": NOW, "score": 1, "match": []}],
               "total": 1, "ms": 1.0, "scope": "mac", "query": {"words": [], "filters": {}}, "truncated": False}
        with mock.patch.object(cli, "daemon_up", return_value=True), mock.patch.object(cli, "client") as cl, \
                mock.patch.object(cli, "TTY", False):
            cl.return_value._call.return_value = res
            code, out, _ = self.run_cli(self.ns("pdf", json=True))
        self.assertEqual(code, 0)
        self.assertNotIn("3704 0044", out)
        self.assertIn("•••", out)

    def test_cli_status_and_search_fall_back_to_the_database_without_the_daemon(self):
        with mock.patch.object(cli, "daemon_up", return_value=False):
            code, out, _ = self.run_cli(self.ns("status", ))
            self.assertEqual(code, 0)
            self.assertIn("whole-Mac file names", out)
            code, out, _ = self.run_cli(self.ns("search", "nothing_matches_this_xyz", json=True))
            self.assertEqual(json.loads(out)["items"], [])
            code, _, _ = self.run_cli(self.ns("crawl"))
            self.assertIn("daemon is not running", str(code))


if __name__ == "__main__":
    unittest.main()
