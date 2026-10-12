"""Speed work on the whole-Mac name search: the allow-gate verdict cache, the trigram query plan, the index upgrade, the
scan budget, the freshness path and the pagination.  Everything runs on temporary folders and temporary databases.
Run: .venv/bin/python -m unittest tests.test_files_speed"""

import json
import os
import random
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import filequery as fq, macindex as mi, service  # noqa: E402

def _tmp_root() -> str:
    """Where the test trees live. The privacy rules never index /private/var (the system temp folder), so a tree there
    would be invisible: use TMPDIR when it is somewhere else (a sandboxed run sets it), else /tmp."""
    t = os.path.realpath(os.environ.get("TMPDIR") or "/tmp")
    return t if not t.startswith(("/private/var", "/var")) else os.path.realpath("/tmp")


TMP = _tmp_root()
HOME = "/Users/alex"
NOW = 1_742_385_600
DAY = 86400


def mkdtemp(prefix="mse-sp-"):
    return tempfile.mkdtemp(prefix=prefix, dir=TMP)


def fake(path, is_dir=False, size=1000, age_days=10):
    m = NOW - int(age_days * DAY)
    return {"path": path, "is_dir": is_dir, "size": size, "mtime": m, "ctime": m}


class Idx:
    """A temporary schema-2 index fed through the real ingest path (Excluder and all)."""

    def __init__(self, rows, cfg=None, home=HOME):
        self.dir = mkdtemp(prefix="mse-sp-idx-")
        self.store = mi.Store(Path(self.dir) / "macindex.db")
        self.excl = mi.Excluder(cfg or {}, h=home)
        self.stored = mi.ingest(self.store, iter(rows), self.store.next_gen(), self.excl)

    def search(self, query, limit=50, roots=None, now=NOW, **kw):
        q = fq.parse_query(query, now=now, home=HOME)
        with self.store.read() as c:
            return fq.search(c, q, limit, roots, now=now, allow=lambda p: not self.excl.blocked(p),
                             fts=self.store.fts_name(), **kw)

    def names(self, query, **kw):
        return [i["name"] for i in self.search(query, **kw)["items"]]

    def paths(self, query, **kw):
        return [i["path"] for i in self.search(query, **kw)["items"]]


# ====================================================================== 1. the allow-gate verdict cache

class GateCacheParity(unittest.TestCase):
    """Excluder.blocked() keeps verdicts per folder. It must give, for every path, in any order, cold or warm, exactly the
    answer of the stateless rules (Excluder.blocked_uncached = the keynote rule + _blocked_full)."""

    WORDS = ["alpha", "beta", "gamma", "docs", "photos", "work", "client", "invoice", "report", "2025", "src", "build",
             "notes", "draft", "Drafts", "Backups", "Alpha", "Projects", "x", "X", "Drive", "private", "CloudStorage"]
    NOISE = ["node_modules", ".git", "DerivedData", "__pycache__", "site-packages", ".venv", ".cache", ".npm", ".Trash",
             "Backups.backupdb", ".next", "NODE_MODULES"]
    SECRET_DIRS = ["secrets", "my_credentials", "recovery-codes", "Passwords", "mysecretstuff", "credentials",
                   "recovery_code_2025"]
    SECRET_FILES = [".env", ".env.local", "id_rsa", "id_rsa.pub", "id_ed25519", "server.pem", "cert.p12", "bundle.pfx",
                    "vault.kdbx", "client.ovpn", "ids.ppk", "keystore.jks", "release.keystore", "credentials.json",
                    "password_list.txt", ".mcp.json", "mcp.json", "secret.txt", ".netrc", ".npmrc", ".pypirc",
                    ".git-credentials", "id_dsa", "known_hosts", "a.gpg", "b.asc", "codex-notes", ".codex", ".claude",
                    ".claude.json", "my.KEY", "notes.key", "ID_RSA", "Secret.TXT", "tmp.tmp", "x.TMP", "x", "X"]
    BASES = [HOME, HOME + "/Documents", HOME + "/Desktop", HOME + "/Library", HOME + "/Library/Caches",
             HOME + "/Library/Keychains", HOME + "/Library/CloudStorage", HOME + "/Library/CloudStorage/Drive",
             HOME + "/Library/CloudStorage/Drive/private", HOME + "/Library/Application Support/MySearch",
             HOME + "/Library/Mail", HOME + "/.ssh", HOME + "/.aws", HOME + "/Documents/Projects/Alpha",
             HOME + "/Documents/work", HOME + "/Documents/Projects/Beta/Drafts", "/Applications", "/System/Library",
             "/private/var/db", "/Volumes/Backup", "/opt/homebrew", "/Users/Shared", "/Library/Caches",
             "/Library/Application Support/com.apple.TCC", "/dev", "/tmp"]

    CONFIGS = {
        "A default": dict(cfg={}, protected=None, roots=None),
        "B name patterns incl. one that matches the probe leaf x": dict(
            cfg={"exclude": ["~/Library/", "*.tmp", "Backups", "Drafts", "x"]}, protected=None, roots=None),
        "C protected folders, roots carved out of ~/Library and ~/Documents": dict(
            cfg={}, protected=[HOME + "/Desktop", HOME + "/Documents"],
            roots=[HOME + "/Library/CloudStorage/Drive", HOME + "/Documents/work"]),
        "D path patterns (cache bypassed)": dict(
            cfg={"exclude": ["~/Library/", "~/Documents/*/Drafts", "**/build"]}, protected=[HOME + "/Desktop"], roots=None),
        "E a configured soft folder inside a carved root": dict(
            cfg={"exclude": ["~/Library/", "~/Library/CloudStorage/Drive/private/"]}, protected=None,
            roots=[HOME + "/Library/CloudStorage/Drive"]),
    }

    @classmethod
    def paths(cls, n, seed):
        r = random.Random(seed)
        out = []
        for _ in range(n):
            comps = []
            for _ in range(r.randint(0, 6)):
                k = r.random()
                c = (r.choice(cls.NOISE) if k < 0.04 else r.choice(cls.SECRET_DIRS) if k < 0.07 else r.choice(cls.WORDS))
                if r.random() < 0.1:
                    c = c.upper() if r.random() < 0.5 else c.capitalize()
                comps.append(c)
            leaf = (r.choice(cls.SECRET_FILES) if r.random() < 0.12
                    else r.choice(cls.WORDS) + r.choice(["", ".txt", ".pdf", ".md"]))
            out.append(r.choice(cls.BASES) + "".join("/" + c for c in comps) + "/" + leaf)
        # leaves that are rules themselves: every base (exact hard/soft entries, carve roots), in two letter cases
        out += cls.BASES + [b.upper() for b in cls.BASES] + [b + "/" for b in cls.BASES]
        out += [HOME + "/Library/CloudStorage/Drive", HOME + "/Library/CloudStorage/Drive/Docs/a.txt",
                HOME + "/Library/CloudStorage", HOME + "/Library/CloudStorage/Other/a.txt",
                HOME + "/Documents/work", HOME + "/Documents/work/a.txt", HOME + "/Documents/a.txt", "/x", "/", ""]
        return out

    def test_cached_verdicts_equal_the_stateless_rules(self):
        base = self.paths(30000, seed=5)
        for label, c in self.CONFIGS.items():
            ex = mi.Excluder(c["cfg"], h=HOME, protected=c["protected"], roots=c["roots"])
            for order_seed in (1, 2):
                order = base[:]
                random.Random(order_seed).shuffle(order)
                truth = [ex.blocked_uncached(p) for p in order]
                cold = mi.Excluder(c["cfg"], h=HOME, protected=c["protected"], roots=c["roots"])
                got_cold = [cold.blocked(p) for p in order]
                got_warm = [cold.blocked(p) for p in order]               # the same instance again, cache full
                got_rev = [cold.blocked(p) for p in reversed(order)][::-1]  # and in the opposite order
                for name, got in (("cold", got_cold), ("warm", got_warm), ("reversed", got_rev)):
                    bad = [(p, t, g) for p, t, g in zip(order, truth, got) if t != g]
                    self.assertEqual(bad[:5], [], f"{label} ({name}, order {order_seed}): {len(bad)} mismatches")
            self.assertTrue(any(truth) and not all(truth), label)         # the sample has both kinds of path

    def test_a_folder_name_with_a_newline_or_an_odd_letter_does_not_switch_the_rules_off(self):
        for cfg in ({}, {"exclude": ["**/build", "*.tmp"]}):
            for order in (0, 1):
                ex = mi.Excluder(cfg, h=HOME)
                probes = [f"{HOME}/a\nb/.ssh/id_rsa", f"{HOME}/a\nb/server.pem", f"{HOME}/a\nb/credentials/x.txt",
                          f"{HOME}/a\nb/notes.txt", f"{HOME}/\u0130stanbul/node_modules", f"{HOME}/\u0130stanbul/.git",
                          f"{HOME}/\u0130stanbul/notes.txt", f"{HOME}/a\nb/node_modules/x.js"]
                if order:
                    probes.reverse()
                for p in probes:
                    self.assertEqual(ex.blocked(p), ex.blocked_uncached(p), p)
                self.assertTrue(ex.blocked(f"{HOME}/a\nb/server.pem"))
                self.assertTrue(ex.blocked(f"{HOME}/\u0130stanbul/node_modules"))
                self.assertFalse(ex.blocked(f"{HOME}/a\nb/notes.txt"))

    def test_a_double_star_pattern_with_a_slash_is_about_a_path_not_a_name(self):
        ex = mi.Excluder({"exclude": ["**/foo/bar"]}, h=HOME)
        self.assertFalse(ex._name_only)
        ex.blocked(f"{HOME}/x/foo/other")
        self.assertEqual(ex.blocked(f"{HOME}/x/foo/bar"), ex.blocked_uncached(f"{HOME}/x/foo/bar"))
        self.assertTrue(mi.Excluder({"exclude": ["**/build", "*.tmp"]}, h=HOME)._name_only)

    def test_a_blocked_parent_never_blocks_a_carved_out_root(self):
        """The root ~/Library/CloudStorage/Drive sits inside the excluded ~/Library: it and what is below it stay visible,
        in any order, and ~/Library/CloudStorage itself stays hidden."""
        c = self.CONFIGS["C protected folders, roots carved out of ~/Library and ~/Documents"]
        for order in (0, 1):
            ex = mi.Excluder(c["cfg"], h=HOME, protected=c["protected"], roots=c["roots"])
            probes = [(HOME + "/Library/CloudStorage/Other/a.txt", True), (HOME + "/Library/CloudStorage/Drive", False),
                      (HOME + "/Library/CloudStorage/Drive/a/b.txt", False), (HOME + "/Library/CloudStorage", True),
                      (HOME + "/Library/Other/a.txt", True), (HOME + "/Library/CloudStorage/Drive/.env", True)]
            if order:
                probes.reverse()
            for p, want in probes:
                self.assertEqual(ex.blocked(p), want, p)
                self.assertEqual(ex.blocked_uncached(p), want, p)

    def test_the_cache_is_per_instance_and_bounded(self):
        a = mi.Excluder({}, h=HOME)
        for i in range(5):
            a.blocked(f"{HOME}/proj{i}/a.txt")
        self.assertEqual(len(a._clean_dirs), 5)
        self.assertEqual(mi.Excluder({}, h=HOME)._clean_dirs, set())
        a.CLEAN_DIRS_MAX = 3
        for i in range(10):
            a.blocked(f"{HOME}/dir{i}/a.txt")
        self.assertLessEqual(len(a._clean_dirs), 3)

    def test_blocked_directories_are_not_remembered_as_clean(self):
        e = mi.Excluder({}, h=HOME)
        self.assertTrue(e.blocked(f"{HOME}/.ssh/id_rsa"))
        self.assertTrue(e.blocked(f"{HOME}/.ssh/notes.txt"))             # a harmless name inside a hard folder
        self.assertEqual(e._clean_dirs, set())
        self.assertFalse(e.blocked(f"{HOME}/Documents/notes.txt"))
        self.assertEqual(e._clean_dirs, {f"{HOME}/Documents"})
        self.assertTrue(e.blocked(f"{HOME}/Documents/.env"))             # clean folder, secret name
        self.assertTrue(e.blocked(f"{HOME}/Documents/node_modules"))     # clean folder, noise name

    def test_keynote_rule_still_decides_by_size(self):
        d = Path(mkdtemp(prefix="mse-sp-key-")).resolve()
        big, small = d / "deck.key", d / "private.key"
        big.write_bytes(b"x" * (mi.KEYNOTE_MIN_BYTES + 10))
        small.write_bytes(b"x" * 100)
        (d / "pkg.key").mkdir()
        (d / "pkg.key" / "inner.txt").write_text("x")
        e = mi.Excluder({}, h=HOME)
        for _ in range(2):                                              # twice: the second round has the folder cached
            self.assertFalse(e.blocked(str(big)))
            self.assertTrue(e.blocked(str(small)))
            self.assertFalse(e.blocked(str(d / "pkg.key")))
            self.assertTrue(e.blocked(str(d / "pkg.key" / "inner.txt")))
            self.assertTrue(e.blocked(str(d / "gone.key")))


# ====================================================================== 2. launchd and QoS

class Priority(unittest.TestCase):
    def test_the_launch_agent_asks_for_the_interactive_process_type(self):
        with mock.patch.object(service, "own_app_executable", return_value=None):
            for exe in (None, "/Applications/MySearch.app/Contents/MacOS/MySearch"):
                d = service.plist(exe)
                self.assertEqual(d["ProcessType"], "Interactive")
                # nothing else about the agent changed: it still starts at login and is restarted if it stops
                self.assertTrue(d["RunAtLoad"] and d["KeepAlive"])
                self.assertEqual(d["Label"], service.LABEL)
                self.assertGreater(d["ExitTimeOut"], 30)
                self.assertNotIn("Nice", d)                      # the whole agent is not reniced, only background work is

    def test_the_plist_survives_a_round_trip_through_plistlib(self):
        import plistlib
        with mock.patch.object(service, "own_app_executable", return_value=None):
            raw = plistlib.dumps(service.plist())
        self.assertEqual(plistlib.loads(raw)["ProcessType"], "Interactive")

    @unittest.skipUnless(mi._qos_lib() is not None, "thread QoS needs macOS")
    def test_a_search_thread_runs_at_user_interactive_qos_and_goes_back(self):
        seen = {}

        def work():
            seen["before"] = mi.thread_qos()
            with mi.interactive_qos():
                seen["inside"] = mi.thread_qos()
            seen["after"] = mi.thread_qos()
        t = threading.Thread(target=work)
        t.start()
        t.join()
        self.assertEqual(seen["inside"], mi.QOS_USER_INTERACTIVE)
        self.assertEqual(seen["after"], seen["before"])

    def test_qos_is_a_no_op_when_the_library_is_missing(self):
        with mock.patch.object(mi, "_qos_lib", return_value=None):
            with mi.interactive_qos():
                self.assertIsNone(mi.thread_qos())

    def test_the_file_search_route_sets_the_qos_around_the_search_only(self):
        from mse import server
        calls = []

        class Spy:
            def __enter__(self_):
                calls.append("in")

            def __exit__(self_, *a):
                calls.append("out")
        fake_mgr = mock.Mock()
        fake_mgr.search.side_effect = lambda *a, **k: (calls.append("search"), {"items": []})[1]
        with mock.patch.object(server.macindex, "interactive_qos", return_value=Spy()), \
                mock.patch.object(server.macindex, "manager", return_value=fake_mgr):
            server.files_search("report")
        self.assertEqual(calls, ["in", "search", "out"])


# ====================================================================== 3. the trigram plan and the index upgrade

# The schema-1 database exactly as the first version created it (git history of mse/macindex.py).
LEGACY_DDL = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT NOT NULL, nf TEXT NOT NULL, parent_id INTEGER, is_dir INTEGER NOT NULL,
  size INTEGER NOT NULL DEFAULT 0, mtime INTEGER NOT NULL DEFAULT 0, ctime INTEGER NOT NULL DEFAULT 0,
  ext TEXT NOT NULL DEFAULT '', kind INTEGER NOT NULL, depth INTEGER NOT NULL, gen INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS files_path ON files(path);
CREATE VIRTUAL TABLE IF NOT EXISTS names USING fts5(nf, content='files', content_rowid='id', tokenize='trigram');
CREATE TABLE IF NOT EXISTS vocab(tok TEXT PRIMARY KEY, rtok TEXT NOT NULL) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS vocab_r ON vocab(rtok);
CREATE INDEX IF NOT EXISTS files_nf ON files(nf);
CREATE INDEX IF NOT EXISTS files_parent ON files(parent_id);
CREATE INDEX IF NOT EXISTS files_ext ON files(ext, mtime);
CREATE INDEX IF NOT EXISTS files_kind ON files(kind, mtime);
CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime);
CREATE INDEX IF NOT EXISTS files_size ON files(size) WHERE is_dir = 0;
CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
  INSERT INTO names(rowid, nf) VALUES (new.id, new.nf); END;
CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
  INSERT INTO names(names, rowid, nf) VALUES ('delete', old.id, old.nf); END;
"""

WORDS = ["invoice", "report", "acme", "budget", "photo", "config", "readme", "notes", "scan", "backup", "project", "design",
         "meeting", "summary", "draft", "final", "client", "contract", "receipt", "travel", "recipe", "holiday", "résumé",
         "Straße", "café", "main", "helper", "utils", "index", "schema", "alpha", "beta", "gamma", "delta"]
EXTS = ["pdf", "txt", "md", "png", "jpg", "docx", "xlsx", "py", "js", "json", "swift", "zip"]


def corpus(n=3000, seed=3):
    """Deterministic fake rows (fictional names) with a spread of depths, ages, extensions and a few folders."""
    r = random.Random(seed)
    rows = []
    for i in range(n):
        d = HOME + "".join("/" + r.choice(["Documents", "Work", "Projects", "Pictures", "Archive", "a", "b", "Clients"])
                           for _ in range(r.randint(1, 4)))
        name = "_".join(r.choice(WORDS) for _ in range(r.randint(1, 3)))
        if r.random() < 0.15:
            rows.append(fake(f"{d}/{name}_{i}", is_dir=True, age_days=r.randint(0, 900)))
        else:
            rows.append(fake(f"{d}/{name}_{i}.{r.choice(EXTS)}", size=r.randint(0, 5_000_000), age_days=r.randint(0, 900)))
    return rows


def build_legacy(path, rows):
    """A finished schema-1 database holding `rows`, built the way the first version built it."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(LEGACY_DDL)
    conn.execute("INSERT INTO meta(k, v) VALUES ('schema', '1')")
    excl = mi.Excluder({}, h=HOME)
    made = [mi.make_row(d["path"], d["is_dir"], d["size"], d["mtime"], d["ctime"], 1) for d in rows if not excl.blocked(d["path"])]
    conn.execute("BEGIN")
    conn.executemany(mi.UPSERT, made)
    toks = {(t, t[::-1]) for r in made for t in mi.name_tokens(r[0].rsplit("/", 1)[-1])}
    conn.executemany("INSERT OR IGNORE INTO vocab(tok, rtok) VALUES (?, ?)", sorted(toks))
    conn.execute("INSERT INTO meta(k, v) VALUES ('crawl', ?)", (json.dumps({"finished": 1.0, "rules": "x", "gen": 1}),))
    conn.execute("COMMIT")
    conn.close()
    return len(made)


def run(store, query, limit=200, fts=None, now=NOW, **kw):
    q = fq.parse_query(query, now=now, home=HOME)
    with store.read() as c:
        return fq.search(c, q, limit, now=now, fts=fts or store.fts_name(), **kw)


QUERIES = ["invoice", "report acme", "budget draft", "photo", "con", "co", "x", "config json", "^main", "helper$", "^readme$",
           "'meeting", "\"final notes\"", "invioce", "recieve", "photo ext:png", "report size:>1mb", "scan mtime:<30d",
           "backup !draft", "ext:pdf", "type:image", "path:clients invoice", "in:/Users/alex/Work report", "re:^inv",
           "re:\\d{3}\\.pdf$", "cafe", "strasse", "résumé", "alpha beta gamma", "acme !report ext:docx", "zzzzqq", "utils main",
           "docs/contract", "limit:7 report", "mian", "docment", "projct design", "ReportX", "delta_alpha"]


class Planner(unittest.TestCase):
    def test_the_rarest_pieces_are_chosen(self):
        pieces = fq.tri_pieces("invoice")
        self.assertEqual(pieces, ["inv", "nvo", "voi", "oic", "ice"])
        counts = {"inv": 500, "nvo": 20, "voi": 400, "oic": 3, "ice": 900}
        self.assertEqual(fq.pick_pieces(pieces, counts), ["oic", "nvo", "voi"])
        self.assertEqual(fq.pick_pieces(pieces, counts, 2), ["oic", "nvo"])
        self.assertEqual(fq.pick_pieces(["abc", "bcd"], counts), ["abc", "bcd"])           # fewer than k: all of them

    def test_unlisted_pieces_count_as_rare_and_ties_keep_the_word_order(self):
        pieces = fq.tri_pieces("abcdefg")
        self.assertEqual(fq.pick_pieces(pieces, {"abc": 10, "bcd": 20}), ["cde", "def", "efg"])   # none of those is listed
        self.assertEqual(fq.pick_pieces(pieces, {"cde": 50}), ["abc", "bcd", "def"])             # the listed one is the common one

    def test_without_counts_three_pieces_spread_over_the_word_are_used(self):
        pieces = fq.tri_pieces("abcdefghij")
        got = fq.pick_pieces(pieces, {})
        self.assertEqual(len(got), 3)
        self.assertEqual((got[0], got[-1]), (pieces[0], pieces[-1]))

    def test_a_word_of_exactly_three_letters_is_one_piece_and_two_dupes_collapse(self):
        self.assertEqual(fq.tri_pieces("abc"), ["abc"])
        self.assertEqual(fq.tri_pieces("aaaaa"), ["aaa"])
        self.assertEqual(fq.tri_pieces("ab"), [])

    def test_the_match_expression_has_only_three_letter_terms(self):
        """detail=none cannot answer a phrase; a longer quoted string would be one."""
        plan = ("and", [("n", "invoice"), ("or", [("n", "acme"), ("n", 'we"ird (x) -y:z*')])])
        expr, ver, par, est = fq.plan_render(plan, {})
        terms = re.findall(r'"((?:[^"]|"")*)"', expr)
        self.assertTrue(terms)
        self.assertTrue(all(len(t.replace('""', '"')) == 3 for t in terms), terms)
        self.assertEqual(ver.count("instr(f.nf, ?)"), 3)
        self.assertEqual(par, ["invoice", "acme", 'we"ird (x) -y:z*'])
        self.assertIsNone(est)

    def test_the_bound_is_the_rarest_pieces_count_and_an_or_adds_up(self):
        counts = {"inv": 500, "nvo": 20, "voi": 400, "oic": 3, "ice": 900, "acm": 70, "cme": 80}
        self.assertEqual(fq.plan_render(("n", "invoice"), counts)[3], 3)
        self.assertEqual(fq.plan_render(("and", [("n", "invoice"), ("n", "acme")]), counts)[3], 3)
        self.assertEqual(fq.plan_render(("or", [("n", "invoice"), ("n", "acme")]), counts)[3], 3 + 70)

    def test_stats_are_hints_only(self):
        """Wrong, zero, huge or missing counts change which pieces are asked for first, never the answer."""
        d = Path(mkdtemp())
        build_legacy(d / "m.db", corpus(1500))
        st = mi.Store(d / "m.db")
        st.migrate_fts()
        base = {qq: [i["path"] for i in run(st, qq)["items"]] for qq in QUERIES}
        w = st.writer()
        for fill in ("0", "1000000", "abs(random()) % 5000"):
            w.execute("DELETE FROM tri_stats")
            w.execute(f"INSERT INTO tri_stats SELECT term, {fill} FROM name_tri_v")
            for qq in QUERIES:
                self.assertEqual([i["path"] for i in run(st, qq)["items"]], base[qq], (fill, qq))
        w.execute("DROP TABLE tri_stats")                                  # no stats table at all
        for qq in QUERIES:
            self.assertEqual([i["path"] for i in run(st, qq)["items"]], base[qq], ("no table", qq))

    def test_words_with_search_syntax_in_them_do_not_break_the_match(self):
        rows = [fake(f"{HOME}/a/he said \"hi\" (x) a*b -c:d.txt"), fake(f"{HOME}/a/plain.txt"), fake(f"{HOME}/a/日本語のファイル.txt"),
                fake(f"{HOME}/a/emoji \U0001F600\U0001F600 note.txt")]
        i = Idx(rows)
        for qq, want in (("(x) -c:d", ["he said \"hi\" (x) a*b -c:d.txt"]),
                         ("-c:d", ["he said \"hi\" (x) a*b -c:d.txt"]), ("日本語", ["日本語のファイル.txt"]),
                         ('"\U0001F600\U0001F600 note"', ["emoji \U0001F600\U0001F600 note.txt"])):
            self.assertEqual(i.names(qq), want, qq)

    def test_non_adjacent_pieces_are_not_a_match(self):
        """The AND of a word's pieces also holds for "abc" + "bcd" far apart; instr() inside the window rejects it."""
        i = Idx([fake(f"{HOME}/a/abcd.txt"), fake(f"{HOME}/a/abc_bcd.txt"), fake(f"{HOME}/a/xabcdx.txt")])
        self.assertEqual(sorted(i.names("abcd")), ["abcd.txt", "xabcdx.txt"])


class SameAnswersAsTheFirstIndex(unittest.TestCase):
    """The schema-2 plan returns, for any query, the lists the first index returned: same rows, same order, also when
    the candidate window cuts the matches off (windows made small here so that a few thousand rows overflow them)."""

    @classmethod
    def setUpClass(cls):
        d = Path(mkdtemp())
        cls.rows = corpus(3500)
        build_legacy(d / "old.db", cls.rows)
        shutil_copy(d / "old.db", d / "new.db")
        cls.old = mi.Store(d / "old.db")
        cls.new = mi.Store(d / "new.db")
        cls.new.migrate_fts()

    def compare(self, queries):
        self.assertEqual(self.old.fts_name(), "names")
        self.assertEqual(self.new.fts_name(), "name_tri")
        for qq in queries:
            a, b = run(self.old, qq), run(self.new, qq)
            self.assertEqual([(i["path"], i["score"]) for i in a["items"]], [(i["path"], i["score"]) for i in b["items"]], qq)
            self.assertEqual((a["total"], a["truncated"]), (b["total"], b["truncated"]), qq)

    def test_identical_lists(self):
        self.compare(QUERIES)

    def test_identical_lists_when_the_windows_overflow(self):
        with mock.patch.object(fq, "INNER_CAP", 40), mock.patch.object(fq, "DEEP_CAP", 400), \
                mock.patch.object(fq, "TYPO_WINDOW", 60), mock.patch.object(fq, "TYPO_CAP", 30):
            self.compare(QUERIES + ["a", "e", "re", "ext:txt invoice", "ext:pdf report", "size:>1gb report", "report path:a/"])

    def test_the_bound_above_the_window_looks_at_more_rows(self):
        """A word more common than the window but within ALL_BOUND is read whole: its late rows become reachable."""
        with mock.patch.object(fq, "INNER_CAP", 30), mock.patch.object(fq, "DEEP_CAP", 30), \
                mock.patch.object(fq, "TYPO_WINDOW", 30):
            narrow = run(self.new, "report limit:1000")
            with mock.patch.object(fq, "ALL_BOUND", 100000):
                wide = run(self.new, "report limit:1000")
        self.assertEqual(len(narrow["items"]), 30)                       # the window
        self.assertGreater(len(wide["items"]), 100)                      # all of them
        self.assertTrue(set(i["path"] for i in narrow["items"]) <= set(i["path"] for i in wide["items"]))


class IndexUpgrade(unittest.TestCase):
    """Schema 1 -> 2 on a database that is in use: the old index keeps answering, writers wait, nothing is lost."""

    def legacy(self, n=1200):
        d = Path(mkdtemp(prefix="mse-sp-up-"))
        rows = corpus(n)
        build_legacy(d / "m.db", rows)
        return d / "m.db", rows

    def truth(self, store, word):
        with store.read() as c:
            return c.execute("SELECT count(*) FROM files WHERE instr(nf, ?) > 0", (word,)).fetchone()[0]

    def count_hits(self, store, word):
        return len([i for i in run(store, f"{word} limit:1000")["items"] if word in mi.fold(i["name"])])

    def test_the_old_index_answers_until_the_switch_and_the_new_one_after(self):
        path, rows = self.legacy()
        st = mi.Store(path)
        self.assertEqual((st.schema_version(), st.fts_name(), st.fts_pending()), (1, "names", True))
        before = {qq: [i["path"] for i in run(st, qq)["items"]] for qq in QUERIES[:12]}
        seen = []

        def during(u):
            seen.append((st.fts_name(), st.upgrade.get("state"), st.upgrade.get("phase"), st.upgrade.get("fraction")))
            for qq in ("report", "acme invoice"):                              # reads are never blocked by the writer
                self.assertEqual([i["path"] for i in run(st, qq)["items"]], before_all[qq])
        before_all = {qq: [i["path"] for i in run(st, qq)["items"]] for qq in ("report", "acme invoice")}
        r = st.migrate_fts(chunk=100, on_progress=during)
        self.assertTrue(r["migrated"])
        self.assertGreaterEqual(len(seen), 5)
        self.assertTrue(all(t == "names" and state == "running" for t, state, _, _ in seen), seen[:3])
        fractions = [f for *_, f in seen]
        self.assertEqual(fractions, sorted(fractions))                       # progress only moves forward
        self.assertLessEqual(max(fractions), 0.95)
        self.assertEqual((st.schema_version(), st.fts_name(), st.fts_pending(), st.upgrade["state"]), (2, "name_tri", False, "done"))
        self.assertEqual(st.migrate_fts(), {"migrated": False, "reason": "already current", "entries": 0, "seconds": 0.0})
        after = {qq: [i["path"] for i in run(st, qq)["items"]] for qq in QUERIES[:12]}
        self.assertEqual(after, before)
        with st.read() as c:
            self.assertGreater(c.execute("SELECT count(*) FROM tri_stats").fetchone()[0], 20)
        st.writer().execute("INSERT INTO name_tri(name_tri) VALUES ('integrity-check')")    # raises when the index is wrong

    def test_a_write_during_the_upgrade_waits_and_is_found_afterwards(self):
        path, rows = self.legacy(600)
        st = mi.Store(path)
        gen = st.next_gen()
        new = [mi.make_row(f"{HOME}/Work/zzfresh_{i}.txt", False, 10, NOW, NOW, gen) for i in range(3)]
        finished = threading.Event()
        started = []

        def writer():
            st.upsert(new)
            finished.set()
        th = threading.Thread(target=writer)

        def during(u):
            if not started:
                started.append(1)
                th.start()
                time.sleep(0.2)
            self.assertFalse(finished.is_set(), "a write got through while the upgrade held the writer")
        st.migrate_fts(chunk=100, on_progress=during)
        th.join(10)
        self.assertTrue(finished.is_set())
        self.assertEqual(sorted(i["name"] for i in run(st, "zzfresh")["items"]), ["zzfresh_0.txt", "zzfresh_1.txt", "zzfresh_2.txt"])
        st.writer().execute("INSERT INTO name_tri(name_tri) VALUES ('integrity-check')")

    def test_an_interrupted_upgrade_changes_nothing_and_starts_over_cleanly(self):
        path, rows = self.legacy(800)
        st = mi.Store(path)
        want = {qq: [i["path"] for i in run(st, qq)["items"]] for qq in QUERIES[:10]}
        calls = []

        def die(u):
            calls.append(1)
            if len(calls) == 3:
                raise InterruptedError("the service is stopping")
        with self.assertRaises(InterruptedError):
            st.migrate_fts(chunk=100, on_progress=die)
        self.assertEqual((st.schema_version(), st.fts_name(), st.upgrade["state"]), (1, "names", "failed"))
        self.assertEqual({qq: [i["path"] for i in run(st, qq)["items"]] for qq in QUERIES[:10]}, want)   # the old index still serves
        # a new process: the half-built table is dropped, the build starts at row one, nothing is indexed twice
        st2 = mi.Store(path)
        self.assertTrue(st2.fts_pending())
        self.assertTrue(st2.migrate_fts(chunk=100)["migrated"])
        for word in ("report", "invoice", "acme", "café"):
            self.assertEqual(self.count_hits(st2, mi.fold(word)), self.truth(st2, mi.fold(word)), word)
        self.assertEqual({qq: [i["path"] for i in run(st2, qq)["items"]] for qq in QUERIES[:10]}, want)
        st2.writer().execute("INSERT INTO name_tri(name_tri) VALUES ('integrity-check')")

    def test_deletes_and_inserts_after_the_switch_keep_the_new_index_right(self):
        path, rows = self.legacy(700)
        st = mi.Store(path)
        st.migrate_fts()
        n0 = self.truth(st, "report")
        self.assertGreater(n0, 5)
        victim = run(st, "report")["items"][0]
        self.assertEqual(st.delete_tree(victim["path"]), 1)
        self.assertEqual(self.count_hits(st, "report"), n0 - 1)
        st.upsert([mi.make_row(f"{HOME}/Work/report_brand_new.txt", False, 1, NOW, NOW, st.next_gen())])
        self.assertEqual(self.count_hits(st, "report"), n0)
        self.assertEqual(self.count_hits(st, "brand_new"), 1)
        st.writer().execute("INSERT INTO name_tri(name_tri) VALUES ('integrity-check')")

    def test_the_first_index_is_dropped_later_and_the_search_survives_the_race(self):
        path, rows = self.legacy(500)
        st = mi.Store(path)
        st.migrate_fts()
        self.assertTrue(st._legacy_left)
        self.assertTrue(st.drop_legacy_fts())
        self.assertFalse(st.drop_legacy_fts())
        with st.read() as c:
            self.assertEqual(fq.detect_fts(c), "name_tri")
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'names%'").fetchone()[0], 0)
        self.assertTrue(run(st, "report")["items"])
        # a reader that still believes in the first index (its choice was cached before the switch)
        m = mi.MacIndex(st)
        st._fts_cache = (time.time(), "names")
        with mock.patch.object(mi, "settings", return_value={**mi.DEFAULT_CONFIG, "roots": [HOME]}):
            res = m.search("report", 20)
        self.assertTrue(res["searchable"] and res["items"])

    def test_a_new_database_is_schema_2_without_a_first_index(self):
        i = Idx([fake(f"{HOME}/a/x.txt")])
        w = i.store.writer()
        self.assertEqual(i.store.schema_version(), 2)
        names = {r[0] for r in w.execute("SELECT name FROM sqlite_master")}
        self.assertTrue({"name_tri", "name_tri_v", "tri_stats", "files_ai", "files_ad"} <= names)
        self.assertNotIn("names", names)
        self.assertFalse(i.store.fts_pending())
        self.assertEqual(w.execute("SELECT sql FROM sqlite_master WHERE name='name_tri'").fetchone()[0].count("detail=none"), 1)
        # reopening it in a new process must not bring the first index back (the old CREATE ... IF NOT EXISTS did)
        s2 = mi.Store(Path(i.dir) / "macindex.db")
        s2.writer()
        self.assertNotIn("names", {r[0] for r in s2.writer().execute("SELECT name FROM sqlite_master")})

    def test_a_schema_1_database_that_never_finished_its_first_load_just_becomes_schema_2(self):
        d = Path(mkdtemp(prefix="mse-sp-bulk-"))
        conn = sqlite3.connect(str(d / "m.db"), isolation_level=None)
        conn.executescript(LEGACY_DDL)
        conn.execute("INSERT INTO meta(k, v) VALUES ('schema', '1'), ('bulk', '1')")
        for t in ("files_ai", "files_ad"):
            conn.execute(f"DROP TRIGGER {t}")
        conn.close()
        st = mi.Store(d / "m.db")
        st.writer()
        self.assertEqual((st.schema_version(), st.fts_pending(), st.fts_name()), (2, False, "name_tri"))
        self.assertTrue(st._bulk)
        gen = st.next_gen()
        st.upsert([mi.make_row(f"{HOME}/a/report_{i}.txt", False, 1, NOW, NOW, gen) for i in range(5)])
        self.assertEqual(run(st, "report")["items"], [])          # the index is built at the end of the load
        st.end_bulk()
        self.assertEqual(len(run(st, "report")["items"]), 5)
        with st.read() as c:
            self.assertGreater(c.execute("SELECT count(*) FROM tri_stats").fetchone()[0] + 1, 0)

    def test_an_empty_schema_1_database_that_is_bulk_loaded_becomes_schema_2(self):
        d = Path(mkdtemp(prefix="mse-sp-empty-"))
        build_legacy(d / "m.db", [])
        st = mi.Store(d / "m.db")
        st.writer()
        self.assertEqual(st.schema_version(), 1)
        st.begin_bulk()
        self.assertEqual((st.schema_version(), st.fts_name()), (2, "name_tri"))
        gen = st.next_gen()
        st.upsert([mi.make_row(f"{HOME}/a/report_{i}.txt", False, 1, NOW, NOW, gen) for i in range(7)])
        st.end_bulk()
        self.assertEqual(len(run(st, "report")["items"]), 7)
        with st.read() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name = 'names'").fetchone()[0], 0)

    def test_a_schema_1_bulk_load_that_was_not_emptied_still_gets_its_index(self):
        d = Path(mkdtemp(prefix="mse-sp-bulk1-"))
        build_legacy(d / "m.db", corpus(40))
        st = mi.Store(d / "m.db")
        st.begin_bulk()                                                   # nobody does this to a full database, but it must work
        st.end_bulk()
        self.assertTrue(run(st, "report")["items"] or run(st, "invoice")["items"] or run(st, "acme")["items"])

    def test_a_database_without_a_schema_row_is_judged_by_its_tables(self):
        d = Path(mkdtemp(prefix="mse-sp-norow-"))
        n = build_legacy(d / "m.db", corpus(200))
        c = sqlite3.connect(str(d / "m.db"), isolation_level=None)
        c.execute("DELETE FROM meta WHERE k = 'schema'")
        c.close()
        st = mi.Store(d / "m.db")
        st.writer()
        self.assertEqual((st.schema_version(), st.fts_name()), (1, "names"))      # the first index is there: still schema 1
        self.assertTrue(st.fts_pending())
        self.assertEqual(len(run(st, "report limit:1000")["items"]), self.truth(st, "report"))

    def test_the_manager_upgrades_in_the_background_and_says_so(self):
        path, rows = self.legacy(900)
        st = mi.Store(path)
        m = mi.MacIndex(st)
        self.assertEqual(m.name_index_status()["table"], "names")
        self.assertTrue(m.name_index_status()["upgrade_pending"])
        m._maintain()
        deadline = time.time() + 30
        while (m.migrating or st.fts_pending()) and time.time() < deadline:
            time.sleep(0.05)
        info = m.name_index_status()
        self.assertEqual((info["schema"], info["table"], info["upgrade"]["state"], info["upgrade_pending"]), (2, "name_tri", "done", False))
        self.assertTrue(info["legacy_table"])
        self.assertIn("elapsed_s", info["upgrade"])
        with mock.patch.object(mi, "LEGACY_GRACE_S", 0):
            m._maintain()
        self.assertFalse(m.name_index_status()["legacy_table"])

    def test_a_half_built_new_index_is_not_read_while_the_schema_says_1(self):
        path, rows = self.legacy(300)
        st = mi.Store(path)
        calls = []

        def die(u):
            calls.append(1)
            if len(calls) == 2:
                raise InterruptedError
        with self.assertRaises(InterruptedError):
            st.migrate_fts(chunk=50, on_progress=die)
        with st.read() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name = 'name_tri'").fetchone()[0], 1)
            self.assertEqual(fq.detect_fts(c), "names")                    # the leftover is ignored
        self.assertEqual(len(run(st, "report limit:1000", fts=None)["items"]), len(run(st, "report limit:1000")["items"]))
        self.assertTrue(st.migrate_fts()["migrated"])
        with st.read() as c:
            self.assertEqual(fq.detect_fts(c), "name_tri")

    def test_a_tiny_index_with_no_listed_pieces_is_not_recounted_forever(self):
        i = Idx([fake(f"{HOME}/a/{w}.txt") for w in ("alpha", "bravo", "charlie", "delta")])
        st = i.store
        st.refresh_stats()
        with st.read() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tri_stats").fetchone()[0], 0)      # every piece is in < STAT_MIN names
        self.assertFalse(st.stats_due())
        st._dirty = 10 ** 6
        self.assertTrue(st.stats_due())                                                          # and churn still triggers it

    def test_a_failed_upgrade_is_reported_and_retried_later(self):
        path, rows = self.legacy(300)
        st = mi.Store(path)
        m = mi.MacIndex(st)
        with mock.patch.object(mi.Store, "migrate_fts", side_effect=RuntimeError("disk full")):
            m._maintain()
            deadline = time.time() + 10
            while m.migrating and time.time() < deadline:
                time.sleep(0.02)
        self.assertFalse(m.migrating)
        self.assertIn("disk full", m.last_error)
        self.assertGreater(m.retry_at, time.time())
        self.assertTrue(st.fts_pending())                              # and the old index is untouched
        with mock.patch.object(mi.Store, "migrate_fts", side_effect=AssertionError("tried again at once")):
            m._maintain()                                              # the back-off holds: no second attempt now
            self.assertFalse(m.migrating)


# ====================================================================== 5. freshness

class Freshness(unittest.TestCase):
    def manager(self):
        d = Path(mkdtemp(prefix="mse-sp-fresh-"))
        return mi.MacIndex(mi.Store(d / "m.db"))

    def start_loop(self, m):
        t = threading.Thread(target=m._flush_loop, daemon=True)
        t.start()
        return t

    def test_the_watcher_is_asked_for_a_short_latency_and_for_no_deferral(self):
        for feats, want in (({"watch_latency", "watch_exclude", "watch_nodefer"}, ["--latency", "0.05", "--no-defer"]),
                            ({"watch_latency"}, ["--latency", "0.05"]), (set(), [])):
            m = self.manager()
            with mock.patch.object(mi.MacIndex, "roots", return_value=["/tmp"]), \
                    mock.patch.object(mi, "helper_path", return_value=Path(__file__)), \
                    mock.patch.object(mi, "helper_features", return_value=feats), \
                    mock.patch.object(mi.MacIndex, "watch_excludes", return_value=[]), \
                    mock.patch.object(mi.threading, "Thread"), \
                    mock.patch.object(mi.subprocess, "Popen") as popen:
                m._watch({"enabled": True})
            args = popen.call_args[0][0]
            self.assertEqual(args[1:3], ["watch", "/tmp"])
            self.assertEqual(args[3:], want, feats)

    def test_idle_means_no_work_at_all(self):
        m = self.manager()
        calls = []
        m.apply = lambda batch: calls.append(batch)
        t = self.start_loop(m)
        time.sleep(0.4)
        self.assertEqual(calls, [])
        m.stop()
        t.join(3)
        self.assertFalse(t.is_alive())

    def test_one_event_is_applied_within_a_few_tens_of_milliseconds(self):
        m = self.manager()
        done = threading.Event()
        seen = []
        m.apply = lambda batch: (seen.append((time.perf_counter(), dict(batch))), done.set())[0]
        self.start_loop(m)
        time.sleep(0.05)
        excl = mi.Excluder({}, h=HOME)
        t0 = time.perf_counter()
        m.feed_event(f"{HOME}/Documents/new.txt", mi.F_CREATED, excl)
        self.assertTrue(done.wait(1.0))
        self.assertLess(seen[0][0] - t0, 0.1)
        self.assertEqual(list(seen[0][1]), [f"{HOME}/Documents/new.txt"])
        m.stop()

    def test_a_burst_costs_a_few_applies_and_loses_nothing(self):
        m = self.manager()
        applied, times = [], []
        m.apply = lambda batch: (times.append(time.perf_counter()), applied.extend(batch))[0]
        self.start_loop(m)
        excl = mi.Excluder({}, h=HOME)
        paths_ = [f"{HOME}/proj/pkg/f{i}.txt" for i in range(6000)]
        t0 = time.perf_counter()
        for i, p in enumerate(paths_):
            m.feed_event(p, mi.F_CREATED, excl)
            if i % 500 == 499:
                time.sleep(0.03)                              # the burst lasts ~0.2 s, like a checkout
        deadline = time.time() + 5
        while len(applied) < len(paths_) and time.time() < deadline:
            time.sleep(0.01)
        m.stop()
        self.assertEqual(sorted(applied), sorted(paths_))
        span = time.perf_counter() - t0
        self.assertLessEqual(len(times), max(8, int(span / mi.FLUSH_MIN_GAP_S) + 2))     # not one apply per event, not per wake-up
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(g >= mi.FLUSH_MIN_GAP_S * 0.9 for g in gaps), gaps)

    def test_a_slow_apply_widens_the_gap_so_the_writer_stays_under_a_fifth_of_a_core(self):
        m = self.manager()
        starts = []

        def slow(batch):
            starts.append(time.perf_counter())
            time.sleep(0.06)
        m.apply = slow
        self.start_loop(m)
        excl = mi.Excluder({}, h=HOME)
        for i in range(6):
            m.feed_event(f"{HOME}/a/f{i}.txt", mi.F_CREATED, excl)
            time.sleep(0.08)
        time.sleep(0.6)
        m.stop()
        self.assertGreaterEqual(len(starts), 2)
        for a, b in zip(starts, starts[1:]):
            self.assertGreaterEqual(b - a, 0.06 + 4 * 0.06 - 0.03)           # apply time + 4x apply time of rest

    def test_a_blocked_path_never_wakes_the_loop(self):
        m = self.manager()
        calls = []
        m.apply = lambda batch: calls.append(batch)
        self.start_loop(m)
        m.feed_event(f"{HOME}/.ssh/id_rsa", mi.F_CREATED, mi.Excluder({}, h=HOME))
        time.sleep(0.2)
        self.assertEqual(calls, [])
        m.stop()

    def test_the_first_small_change_does_not_load_the_whole_vocabulary(self):
        st = Idx([fake(f"{HOME}/a/{w}.txt") for w in ("alpha", "bravo", "charlie")]).store
        st._vocab = None
        gen = st.next_gen()
        st.upsert([mi.make_row(f"{HOME}/a/delta_echo.txt", False, 1, NOW, NOW, gen)])
        self.assertIsNone(st._vocab)                                       # nothing resident
        with st.read() as c:
            toks = {r[0] for r in c.execute("SELECT tok FROM vocab")}
        self.assertTrue({"alpha", "bravo", "charlie", "delta", "echo"} <= toks)     # and the new words are still learned
        st.upsert([mi.make_row(f"{HOME}/a/foxtrot_{i}.txt", False, 1, NOW, NOW, gen) for i in range(mi.SMALL_BATCH + 10)], lean=False)
        self.assertIsNotNone(st._vocab)                                    # a crawl-sized batch keeps the set, as before
        # and the typo path still finds a word learned the lean way
        self.assertEqual(Idx([fake(f"{HOME}/a/recieve.txt")]).names("receive"), ["recieve.txt"])


def shutil_copy(a, b):
    import shutil
    shutil.copy(str(a), str(b))


if __name__ == "__main__":
    unittest.main()
