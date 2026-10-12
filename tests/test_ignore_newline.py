"""Names with control characters (a newline, a carriage return, a tab) must never switch an exclusion off.

Before the fix a rule's `.` did not match a newline, so one folder named "a\\nb" hid everything below it from `.env`,
`Credentials/`, the metadata-only rules and the Keynote check (mse/ignore.py compile_pattern now uses re.DOTALL, as
mse/macindex.py does). This file checks the rules under such folders; compares every pattern with a verbatim copy of
the old matcher (the parity oracle: the new one may exclude more, never less); walks real folders with the indexer;
and checks the `mse files --offset` option and its help text. The daemon is never reached: its calls are mocked.
Run: .venv/bin/python -m unittest tests.test_ignore_newline   (temp folders follow TMPDIR; nothing is deleted)
"""

import io
import itertools
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import cli, filequery, indexer, server  # noqa: E402
from mse.ignore import DEFAULT_EXCLUDES, SECRET_PATTERNS, Matcher, compile_pattern  # noqa: E402

NAMES = ["a\nb", "x\r", "tab\tname", "top\n", "\nlead", "cr\r\nlf"]


# ---------------------------------------------------------------- the parity oracle: the matcher before the fix
# Copied from HEAD mse/ignore.py, logic verbatim (no cache). Not derived from the new code, so a later change to the
# module cannot move the reference with it.

def _old_translate(glob: str) -> str:
    out, i, n = [], 0, len(glob)
    while i < n:
        c = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("/**", i) and i + 3 == n:
            out.append("(?:/.*)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = glob.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                out.append("[" + glob[i + 1:j].replace("\\", "\\\\") + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _old_compile(pattern: str) -> re.Pattern:
    p = pattern.strip()
    p = p.rstrip("/") if p.endswith("/") else p
    anchored = p.startswith("/") or "/" in p.rstrip("/")
    p = p.lstrip("/")
    body = _old_translate(p)
    prefix = "^" if anchored else "^(?:.*/)?"
    return re.compile(prefix + body + "(?:/.*)?$", re.IGNORECASE)


class RulesBelowControlFolders(unittest.TestCase):
    def test_secret_rules_apply_below_each_such_folder(self):
        rules = Matcher(SECRET_PATTERNS + DEFAULT_EXCLUDES)
        for name in NAMES:
            for rel in (".env", "id_rsa", "Credentials/x.txt", "Keychains/login.keychain", "deck.key/Index.apxl",
                        "node_modules/x.js", ".git/config"):
                with self.subTest(folder=name, rel=rel):
                    self.assertTrue(rules.match(f"{name}/{rel}"))

    def test_metadata_only_rule_applies_above_and_inside_such_folders(self):
        meta = Matcher(["Payroll/", "NOTES.md"])
        for name in NAMES:
            with self.subTest(folder=name):
                self.assertTrue(meta.match(f"{name}/Payroll/payslip.pdf"))     # the rule folder below the odd name
                self.assertTrue(meta.match(f"Payroll/{name}/payslip.pdf"))     # the odd name inside the rule folder
                self.assertTrue(meta.match(f"{name}/Payroll/"))                # the folder itself
                self.assertTrue(meta.match(f"{name}/NOTES.md"))
                self.assertFalse(meta.match(f"{name}/Invoices/x.pdf"))         # unrelated: not excluded

    def test_double_star_patterns_cross_such_folders(self):
        for name in NAMES:
            with self.subTest(folder=name):
                self.assertTrue(Matcher(["**/invoices/*"]).match(f"{name}/invoices/a.pdf"))
                self.assertTrue(Matcher(["src/**/*.py"]).match(f"src/{name}/deep/x.py"))
                self.assertTrue(Matcher(["Vault/**"]).match(f"Vault/{name}/x"))
                self.assertTrue(Matcher(["**/*credential*"]).match(f"{name}/a/creds_credential.txt"))

    def test_directory_form_matches_the_folder_itself(self):
        for name in NAMES:
            with self.subTest(folder=name):
                self.assertTrue(Matcher(["**/*credential*/"]).match(f"{name}/Credentials/"))
                self.assertTrue(Matcher(["Credentials/"]).match(f"{name}/Credentials/"))

    def test_a_name_ending_in_a_newline_is_still_excluded(self):
        # `$` takes one trailing newline, so ".env\n" stays a ".env". `\Z` would include it: the reason to keep `$`.
        self.assertTrue(Matcher([".env"]).match(".env\n"))
        self.assertTrue(Matcher(["*.key"]).match("deck.key\n"))
        self.assertTrue(Matcher(SECRET_PATTERNS).match("Credentials\n/x.txt"))

    def test_macos_custom_icon_file_is_excluded_and_plain_icon_still_is(self):
        # DEFAULT_EXCLUDES has "Icon\r" (the file macOS writes). The trimmed "Icon" stays excluded too.
        rules = Matcher(DEFAULT_EXCLUDES)
        self.assertTrue(rules.match("photo/Icon\r"))
        self.assertTrue(rules.match("photo/Icon"))
        self.assertFalse(rules.match("photo/Icons"))
        self.assertFalse(rules.match("photo/Icon\rx"))


ALPHABET = ("a", "Credentials", ".env", "Payroll", "invoices", "x.key", "a\nb", "x\r", "tab\tname", "top\n",
            "Icon\r", "Icon", "NOTES.md", ".git", "src", "*.py")
PARITY_PATTERNS = sorted(set(SECRET_PATTERNS + DEFAULT_EXCLUDES + [
    "Payroll/", "NOTES.md", "**/invoices/*", "src/**/*.py", "invoices/**", "/a/**", "a?b", "[ab]*", "*.pdf",
    "*\n", "Icon\r", "x\r/**", "Credentials/"]))
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _generated_paths():
    """Every path of 1 to 3 names from ALPHABET, as a file and as a folder (trailing slash)."""
    for depth in (1, 2, 3):
        for combo in itertools.product(ALPHABET, repeat=depth):
            p = "/".join(combo)
            yield p
            yield p + "/"


class ParityWithTheOldMatcher(unittest.TestCase):
    def test_the_new_matcher_only_adds_exclusions(self):
        paths = list(_generated_paths())
        lost, changed, gained = [], [], set()
        for pat in PARITY_PATTERNS:
            old, new = _old_compile(pat), Matcher([pat])
            for p in paths:
                o, n = bool(old.match(p)), bool(new.match(p))
                if o and not n:
                    lost.append((pat, p))                 # the one thing that must never happen
                if n and not o:
                    gained.add((pat, p))
                if o != n and not CONTROL.search(p):
                    changed.append((pat, p))              # a path with no control character must not change verdict
        self.assertEqual(lost, [], "a previously excluded path became included")
        self.assertEqual(changed, [], "a path without control characters changed verdict")
        # not vacuous: the holes that were open are closed by this change
        for case in [("Payroll/", "a\nb/Payroll/"), ("Payroll/", "Payroll/a\nb/"), ("**/*credential*/", "a\nb/Credentials/"),
                     ("**/.env", "a\nb/.env"), ("Icon\r", "Icon\r")]:
            self.assertIn(case, gained, case)

    def test_patterns_without_surrounding_space_compile_to_the_same_source(self):
        # macindex.py embeds compile_pattern(p).pattern in its own rule; an ordinary pattern must not change text
        for pat in PARITY_PATTERNS:
            if pat.strip(" ") == pat.strip():
                with self.subTest(pattern=pat):
                    self.assertEqual(compile_pattern(pat).pattern, _old_compile(pat).pattern)

    def test_the_oracle_really_has_the_old_hole(self):
        self.assertFalse(_old_compile(".env").match("a\nb/.env"))


class WalkOfRealFolders(unittest.TestCase):
    def test_the_walk_and_the_secret_check_hold_under_control_character_folders(self):
        root = Path(tempfile.mkdtemp(prefix="mse-nl-"))       # under TMPDIR; nothing is removed afterwards
        files = {
            "a\nb/.env": b"K=1", "a\nb/Credentials/x.txt": b"c", "top\n/server.key": b"-----BEGIN",
            "x\r/id_rsa": b"k", "a\nb/keep.txt": b"ok", "tab\tname/notes.txt": b"ok",
            "tab\tname/Payroll/payslip.pdf": b"p", "top\n/deck.key/Index.apxl": b"<xml/>",
        }
        for rel, data in files.items():
            full = root / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(data)
        coll = {"name": "nl-test", "path": str(root), "exclude": [], "metadata_only": ["Payroll/"]}
        errors: list = []
        walked = {rel for rel, *_ in indexer._walk(coll, errors)}
        for gone in ("a\nb/.env", "a\nb/Credentials/x.txt", "top\n/server.key", "x\r/id_rsa", "top\n/deck.key/Index.apxl"):
            self.assertNotIn(gone, walked, gone)
        for kept in ("a\nb/keep.txt", "tab\tname/notes.txt", "top\n/deck.key"):   # a Keynote package is one entry
            self.assertIn(kept, walked, kept)
        exclude, meta = indexer.matchers(coll)
        self.assertTrue(meta.match("tab\tname/Payroll/payslip.pdf"))
        self.assertTrue(indexer._secret_excluded("a\nb/.env", exclude, indexer._nokey_matcher(coll), root))


PAGE = {"items": [], "total": 45, "ms": 3, "scope": "mac", "searchable": True, "truncated": False,
        "has_more": True, "next_offset": 60, "offset": 40, "limit": 20}


class FilesOffsetAndHelp(unittest.TestCase):
    def _offline(self, argv, page):
        out = io.StringIO()
        with mock.patch.object(cli, "daemon_up", return_value=False), \
                mock.patch.object(cli, "_files_locked", return_value=None), \
                mock.patch.object(server, "files_search", return_value=dict(page)) as fs, redirect_stdout(out):
            cli.main(argv)
        return fs, out.getvalue()

    def test_offline_offset_reaches_the_server_in_the_paged_shape(self):
        fs, text = self._offline(["files", "invoice", "--offset", "40", "-n", "20"], PAGE)
        args, kw = fs.call_args
        self.assertEqual((args[0], args[3], args[4]), ("invoice", 20, 40))
        self.assertTrue(kw.get("paged"))
        self.assertIn("more: mse files invoice -n 20 --offset 60", text)

    def test_without_offset_the_offline_call_is_unchanged_and_no_hint_is_printed(self):
        # the call keeps its four arguments: an older test double of files_search takes only those
        fs, text = self._offline(["files", "invoice"], {**PAGE, "has_more": False, "next_offset": None})
        args, kw = fs.call_args
        self.assertEqual(len(args), 4)
        self.assertEqual(kw, {})
        self.assertNotIn("more: mse files", text)

    def test_daemon_path_sends_offset_and_limit(self):
        client = mock.Mock()
        client._call.return_value = dict(PAGE)
        with mock.patch.object(cli, "daemon_up", return_value=True), mock.patch.object(cli, "client", return_value=client), \
                mock.patch.object(cli, "_files_locked", return_value=None), redirect_stdout(io.StringIO()):
            cli.main(["files", "invoice", "--offset", "40", "-n", "20"])
        _method, path, params = client._call.call_args[0]
        self.assertEqual(path, "/api/files/search")
        self.assertEqual((params["offset"], params["limit"]), (40, 20))

    def test_daemon_path_without_offset_sends_limit_only(self):
        client = mock.Mock()
        client._call.return_value = dict(PAGE)
        with mock.patch.object(cli, "daemon_up", return_value=True), mock.patch.object(cli, "client", return_value=client), \
                mock.patch.object(cli, "_files_locked", return_value=None), redirect_stdout(io.StringIO()):
            cli.main(["files", "invoice"])
        params = client._call.call_args[0][2]
        self.assertNotIn("offset", params)
        self.assertEqual(params["limit"], 20)

    def test_negative_offset_is_refused_before_any_search(self):
        with mock.patch.object(cli, "daemon_up", return_value=False), mock.patch.object(cli, "_files_locked", return_value=None), \
                mock.patch.object(server, "files_search") as fs:
            with self.assertRaises(SystemExit) as cm:
                cli.main(["files", "invoice", "--offset", "-1"])
        self.assertIn("--offset must not be negative", str(cm.exception.code))
        fs.assert_not_called()

    def test_help_documents_the_query_rules_and_offset(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            cli.main(["files", "--help"])
        self.assertEqual(cm.exception.code, 0)
        text = out.getvalue()
        for needle in ("Smart case:", "Ranking:", "file name", "report-*.docx", "**/invoices/*", "!*.log",
                       "mac_index.smart_case", "--offset OFFSET", "exclude:"):
            self.assertIn(needle, text)


class HelpIsFilequeryWording(unittest.TestCase):
    def test_every_rule_chunk_is_copied_from_the_filequery_docstring(self):
        doc = " ".join((filequery.__doc__ or "").split())
        chunks = cli._files_rule_chunks()
        self.assertEqual(len(chunks), 3, "the rule sections moved in mse/filequery.py: update cli._files_rule_chunks")
        for ch in chunks:
            self.assertIn(" ".join(ch.split()), doc)


if __name__ == "__main__":
    unittest.main()
