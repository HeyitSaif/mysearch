"""Managing what the whole-Mac name search skips: the validation of mac_index.exclude and mac_index.roots, the
settings API, the status the settings screens read, and `mse files exclude|roots add|rm|list`.

Everything runs against a temporary MySearch home; no crawl is started, no helper is used and the real disk is not
walked. Run: .venv/bin/python -m unittest discover -s tests"""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import cli, config, ignore, macindex as mi  # noqa: E402

H = "/Users/alex"
TMP = os.path.realpath(os.environ.get("TMPDIR") or "/tmp")


def mkdtemp(prefix="mse-mx-"):
    return tempfile.mkdtemp(prefix=prefix, dir=TMP)


class Settings:
    """Puts mac_index back as it was (the home of the whole test run is shared)."""

    def save(self):
        self._saved = {k: list(v) if isinstance(v, list) else v for k, v in config.load()["mac_index"].items()}

    def restore(self):
        config.update_settings({"mac_index": self._saved})


# ====================================================================== one exclude entry

class NormalizeExclude(unittest.TestCase):
    def n(self, e):
        return mi.normalize_exclude(e, H)

    def test_folders_get_one_canonical_form(self):
        self.assertEqual(self.n("~/Library"), "~/Library/")
        self.assertEqual(self.n("~/Library/"), "~/Library/")
        self.assertEqual(self.n(f"{H}/Library//Caches/"), "~/Library/Caches/")
        self.assertEqual(self.n("  ~/Documents/Projects/Alpha/  "), "~/Documents/Projects/Alpha/")
        self.assertEqual(self.n("/Volumes/Backup/Old"), "/Volumes/Backup/Old/")
        self.assertEqual(self.n("//tmp//x/./y"), "/tmp/x/y/")

    def test_names_and_globs_are_kept_as_typed(self):
        for p in ("node_modules", "*.tmp", "**/build", "Backups", "~/Documents/*/Drafts"):
            self.assertEqual(self.n(p), p)
        self.assertEqual(self.n(f"{H}/Documents/*/Drafts"), "~/Documents/*/Drafts")

    def test_refused(self):
        cases = {
            "": "cannot be empty", "   ": "cannot be empty", "a\nb": "control characters",
            "..": "'..'", "../x": "'..'", "~/a/../b": "'..'", "/Users/alex/../y": "'..'", "**/../x": "'..'",
            "Documents/Old": "full path", "sub/dir/": "full path", "./x": "full path",
            "~bob/x": "another user", "!~/.ssh": "cannot start with", "#note": "cannot start with",
        }
        for entry, why in cases.items():
            with self.assertRaises(ValueError, msg=repr(entry)) as cm:
                self.n(entry)
            self.assertIn(why, str(cm.exception), repr(entry))

    def test_slash_and_the_home_folder_are_refused_with_the_real_reason(self):
        with self.assertRaises(ValueError) as cm:
            self.n("/")
        self.assertIn("hide every file", str(cm.exception))
        for e in ("~", "~/", H, H + "/", "/Users", "/Users/"):
            with self.assertRaises(ValueError, msg=e) as cm:
                self.n(e)
            self.assertIn("contains your home folder", str(cm.exception), e)
            self.assertIn("does nothing while your home folder is a search root", str(cm.exception), e)
        self.assertEqual(self.n("/Users/alex2"), "/Users/alex2/")          # a sibling of the home folder is fine
        self.assertEqual(self.n("/Users/alex2/x"), "/Users/alex2/x/")  # and so is a name that merely starts alike

    def test_a_root_inside_an_exclusion_stays_valid(self):
        """~/Library/ containing the root ~/Library/CloudStorage is the documented case, not an error."""
        self.assertEqual(mi.normalize_excludes(["~/Library/"], H), ["~/Library/"])
        self.assertEqual(mi.normalize_roots(["~/Library/CloudStorage"], H), [f"{H}/Library/CloudStorage"])

    def test_duplicates_collapse_whatever_the_spelling(self):
        got = mi.normalize_excludes(["~/Library", f"{H}/library/", "*.tmp", "*.TMP", "~/Other"], H)
        self.assertEqual(got, ["~/Library/", "*.tmp", "~/Other/"])

    def test_key_ignores_case_slash_and_spelling_of_home(self):
        k = mi.exclude_key
        self.assertEqual(k("~/Library/", H), k(f"{H}/library", H))
        self.assertNotEqual(k("~/Library/", H), k("~/Library2/", H))


class NormalizeRoots(unittest.TestCase):
    def test_accepts_and_expands(self):
        self.assertEqual(mi.normalize_roots(["~", "/tmp/x/", "~/Work"], H), [H, "/tmp/x", f"{H}/Work"])
        self.assertEqual(mi.normalize_roots(["/tmp/x", "/TMP/X"], H), ["/tmp/x"])

    def test_refused(self):
        for r, why in (("relative/dir", "absolute"), ("/tmp/a/../b", "'..'"), ("~/.ssh", "never indexed"),
                       (f"{H}/Library/Mail/x", "never indexed"), ("/Volumes/Backup", "never indexed"),
                       ("/System/Library", "never indexed"), ("/tmp/proj/node_modules/x", "never indexed")):
            with self.assertRaises(ValueError, msg=r) as cm:
                mi.normalize_roots([r], H)
            self.assertIn(why, str(cm.exception), r)


class EditPaths(unittest.TestCase):
    def test_add_rm_and_the_no_ops(self):
        cur = ["~/Library/"]
        new, changed, same = mi.edit_paths(cur, "add", ["~/Music", "/Users/alex/library"], H)
        self.assertEqual((new, changed, same), (["~/Library/", "~/Music"], ["~/Music"], ["/Users/alex/library"]))
        new, changed, same = mi.edit_paths(new, "rm", ["/Users/alex/Music/", "~/Nope"], H)
        self.assertEqual((new, changed, same), (["~/Library/"], ["/Users/alex/Music/"], ["~/Nope"]))
        self.assertEqual(cur, ["~/Library/"])                                  # the input list is not touched

    def test_roots(self):
        new, changed, _ = mi.edit_paths([H, "/tmp/a"], "add", ["/tmp/b", "/TMP/A"], H, roots=True)
        self.assertEqual((new, changed), ([H, "/tmp/a", "/tmp/b"], ["/tmp/b"]))
        new, changed, same = mi.edit_paths(new, "rm", ["/tmp/a/", "/tmp/zzz"], H, roots=True)
        self.assertEqual((new, changed, same), ([H, "/tmp/b"], ["/tmp/a/"], ["/tmp/zzz"]))


# ====================================================================== the rules that cannot be changed

class HardRulesStayHard(unittest.TestCase):
    def test_the_settings_cannot_take_a_built_in_rule_away(self):
        for entry in ("!~/.ssh/", "!.env", "#~/.ssh"):
            with self.assertRaises(ValueError, msg=entry):
                mi.check_settings({"exclude": [entry]})
        everything_off = mi.Excluder({"exclude": [], "roots": [H, f"{H}/.ssh"]}, h=H, roots=[H, f"{H}/.ssh"])
        for p in (f"{H}/.ssh/id_rsa", f"{H}/Library/Mail/x", f"{H}/proj/.env", f"{H}/proj/my_secret.txt",
                  f"{H}/proj/node_modules/a.js", "/System/x", f"{H}/Documents/Private/Credentials_Store/a"):
            self.assertTrue(everything_off.blocked(p), p)
        self.assertFalse(everything_off.root_allowed(f"{H}/.ssh"))

    def test_excluding_something_already_built_in_is_harmless_and_flagged(self):
        got = mi.check_settings({"exclude": ["~/.ssh/", "~/Music/"]})["exclude"]
        self.assertEqual(got, ["~/.ssh/", "~/Music/"])
        entries = mi.exclude_entries({"roots": ["/tmp/r"], "exclude": got}, h=os.path.expanduser("~"))
        self.assertIn("built-in", entries[0]["note"])
        self.assertNotIn("note", entries[1])

    def test_hard_groups_list_every_rule_once(self):
        groups = mi.hard_groups(H)
        flat = [i for g in groups for i in g["items"]]
        self.assertTrue(all(g["items"] for g in groups))
        self.assertEqual(len(flat), len(set(flat)))
        want = ([f"~/{r}/" for r in mi.HARD_HOME] + [p + "/" for p in mi.HARD_ABS] + list(ignore.SECRET_PATTERNS)
                + mi.HARD_PATTERNS + [n + "/" for n in mi.NOISE_DIRS])
        self.assertEqual(sorted(flat), sorted(set(want)) if len(want) != len(set(want)) else sorted(want))
        by = {g["label"]: g["items"] for g in groups}
        self.assertIn("~/.ssh/", by["Keys, tokens and credentials"])
        self.assertIn("~/Library/Mail/", by["Mail, Messages and accounts"])
        self.assertIn("~/Library/Application Support/Google/Chrome/", by["Browser profiles"])
        self.assertIn("/System/", by["System folders and disks"])
        self.assertIn("*.pem", by["Secret and key file names, anywhere"])
        self.assertIn("node_modules/", by["Build, cache and tool folders, anywhere"])

    def test_exclude_entries_explain_themselves(self):
        h = os.path.expanduser("~")
        got = mi.exclude_entries({"roots": [h, f"{h}/Docs"], "exclude": ["~/Library/", f"{h}/Docs/", "*.tmp", "~/a/*/b"]})
        self.assertEqual([(e["entry"], e["kind"]) for e in got],
                         [("~/Library/", "folder"), (f"{h}/Docs/", "folder"), ("*.tmp", "pattern"), ("~/a/*/b", "pattern")])
        self.assertEqual(got[0]["path"], f"{h}/Library")
        self.assertNotIn("note", got[0])
        self.assertIn("search root", got[1]["note"])                       # an exclusion equal to a root does nothing
        self.assertIsNone(got[2]["path"])
        self.assertFalse(any("invalid" in e for e in got))

    def test_an_old_entry_the_rules_now_refuse_is_flagged(self):
        h = os.path.expanduser("~")
        got = mi.exclude_entries({"roots": [h], "exclude": ["Documents/Old", "/", "~/ok/", "x/../y"]})
        self.assertEqual([("invalid" in e) for e in got], [True, True, False, True])
        self.assertIn("full path", got[0]["invalid"])
        self.assertIn("hide every file", got[1]["invalid"])
        self.assertEqual(got[0]["entry"], "Documents/Old")                     # shown exactly as stored, so it can be removed


class ExcluderUsesTheNormalisedForm(unittest.TestCase):
    def test_an_added_folder_hides_names_below_it_and_only_there(self):
        cfg = {"exclude": mi.check_settings({"exclude": ["~/Music", "*.tmp"]})["exclude"]}
        e = mi.Excluder(cfg, h=H)
        self.assertTrue(e.blocked(f"{H}/Music/song.mp3"))
        self.assertTrue(e.blocked(f"{H}/Music"))
        self.assertTrue(e.blocked(f"{H}/proj/a.tmp"))
        self.assertFalse(e.blocked(f"{H}/Music2/song.mp3"))
        self.assertFalse(e.blocked(f"{H}/Documents/a.md"))
        self.assertIn(("--skip-prefix", f"{H}/Music"), list(zip(e.helper_args()[::2], e.helper_args()[1::2])))

    def test_a_root_inside_an_excluded_folder_is_still_searched(self):
        e = mi.Excluder({"exclude": ["~/Library/"]}, h=H, roots=[H, f"{H}/Library/CloudStorage"])
        self.assertFalse(e.blocked(f"{H}/Library/CloudStorage/a.docx"))
        self.assertTrue(e.blocked(f"{H}/Library/Preferences/a.plist"))


class CheckSettings(unittest.TestCase):
    def test_exclude_and_roots_go_through_one_validation(self):
        got = mi.check_settings({"exclude": ["~/Library", "~/Library/"], "roots": ["/tmp/x/", "/tmp/x"]})
        self.assertEqual(got["exclude"], ["~/Library/"])
        self.assertEqual(got["roots"], ["/tmp/x"])
        for bad in ({"exclude": ["/"]}, {"exclude": [os.path.expanduser("~")]}, {"exclude": ["a/b"]},
                    {"exclude": ["~/x/../y"]}, {"exclude": [""]}, {"exclude": "x"}, {"exclude": [3]},
                    {"roots": ["~/.ssh"]}, {"roots": ["/tmp/a/.."]}, {"roots": []}):
            with self.assertRaises(ValueError, msg=str(bad)):
                mi.check_settings(bad)

    def test_skip_protected_keeps_its_three_values(self):
        for v in ("auto", "always", "never"):
            self.assertEqual(mi.check_settings({"skip_protected": v}), {"skip_protected": v})
        with self.assertRaises(ValueError):
            mi.check_settings({"skip_protected": "sometimes"})

    def test_the_message_names_the_bad_entry(self):
        with self.assertRaises(ValueError) as cm:
            mi.check_settings({"exclude": ["~/Library/", "old/folder"]})
        self.assertIn("old/folder", str(cm.exception))


# ====================================================================== the API

class Api(unittest.TestCase):
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

    def setUp(self):
        self.s = Settings()
        self.s.save()
        mi.manager()._excl = None

    def tearDown(self):
        self.s.restore()
        mi.manager()._excl = None

    def req(self, method, path, body=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                   data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-MSE-Token": config.load()["token"], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_posting_the_full_list_replaces_and_normalises(self):
        code, res = self.req("POST", "/api/settings", {"mac_index": {"exclude": ["~/Library", "~/Music/", "*.tmp"]}})
        self.assertEqual(code, 200)
        self.assertEqual(res["mac_index"]["exclude"], ["~/Library/", "~/Music/", "*.tmp"])
        self.assertEqual(config.load()["mac_index"]["exclude"], ["~/Library/", "~/Music/", "*.tmp"])
        code, res = self.req("GET", "/api/settings")
        self.assertEqual(res["mac_index"]["exclude"], ["~/Library/", "~/Music/", "*.tmp"])
        code, res = self.req("POST", "/api/settings", {"mac_index": {"exclude": ["~/Library/"]}})          # remove two
        self.assertEqual(res["mac_index"]["exclude"], ["~/Library/"])

    def test_refusals_are_400_with_a_readable_message_and_change_nothing(self):
        before = config.load()["mac_index"]
        for body, needle in (({"exclude": ["/"]}, "hide every file"), ({"exclude": ["~"]}, "home folder"),
                             ({"exclude": [os.path.expanduser("~") + "/"]}, "home folder"),
                             ({"exclude": ["Documents/Old"]}, "full path"), ({"exclude": ["~/a/../b"]}, "'..'"),
                             ({"exclude": ["!~/.ssh"]}, "cannot start with"), ({"exclude": ["~/ok/", ""]}, "mac_index.exclude"),
                             ({"roots": ["~/.ssh"]}, "never indexed"), ({"roots": ["/tmp/x/.."]}, "'..'"),
                             ({"roots": ["relative"]}, "absolute"), ({"skip_protected": "x"}, "skip_protected")):
            code, err = self.req("POST", "/api/settings", {"mac_index": body})
            self.assertEqual(code, 400, body)
            self.assertIn(needle, err["error"], body)
        self.assertEqual(config.load()["mac_index"], before)

    def test_update_settings_is_the_same_path(self):
        with self.assertRaises(ValueError):
            config.update_settings({"mac_index": {"exclude": ["/"]}})
        cfg = config.update_settings({"mac_index": {"exclude": ["/tmp//zz/"]}})
        self.assertEqual(cfg["mac_index"]["exclude"], ["/tmp/zz/"])

    def test_status_has_what_the_settings_screens_need(self):
        self.req("POST", "/api/settings", {"mac_index": {"exclude": ["~/Library/", "~/Music/", "node_modules"]}})
        code, st = self.req("GET", "/api/files/status")
        self.assertEqual(code, 200)
        self.assertEqual(st["python_path"], sys.executable)
        self.assertEqual(st["python_real_path"], os.path.realpath(sys.executable))
        self.assertEqual([e["entry"] for e in st["exclude_entries"]], ["~/Library/", "~/Music/", "node_modules"])
        self.assertEqual(st["exclude_entries"][1]["path"], os.path.expanduser("~/Music"))
        self.assertEqual(st["exclude_entries"][2]["kind"], "pattern")
        labels = [g["label"] for g in st["hard_excluded"]]
        self.assertIn("Keys, tokens and credentials", labels)
        self.assertTrue(all(g["items"] for g in st["hard_excluded"]))
        self.assertTrue(any("~/.ssh/" in g["items"] for g in st["hard_excluded"]))
        self.assertTrue(any("~/Music/" in x for x in st["excluded"]))        # the existing flat list follows the setting

    def test_the_built_in_rules_are_not_in_the_editable_list_and_survive_any_edit(self):
        self.req("POST", "/api/settings", {"mac_index": {"exclude": []}})
        code, st = self.req("GET", "/api/files/status")
        self.assertEqual(st["exclude_entries"], [])
        self.assertTrue(any(p.endswith(".ssh/") for p in st["excluded"]))
        self.assertTrue(any(p.endswith("Library/Mail/") for p in st["excluded"]))
        self.assertIn("**/*credential*", st["excluded"])

    def test_skip_protected_toggle_round_trips_and_always_is_not_rewritten(self):
        for v in ("never", "auto", "always"):
            code, res = self.req("POST", "/api/settings", {"mac_index": {"skip_protected": v}})
            self.assertEqual((code, res["mac_index"]["skip_protected"]), (200, v))
        code, res = self.req("POST", "/api/settings", {"mac_index": {"exclude": ["~/Music/"]}})   # another edit leaves it
        self.assertEqual(res["mac_index"]["skip_protected"], "always")

    def test_a_changed_rule_set_makes_the_crawler_run_again(self):
        m = mi.manager()
        cfg = mi.settings()
        h0 = mi.rules_hash(m.excluder(), m.roots())
        self.req("POST", "/api/settings", {"mac_index": {"exclude": cfg["exclude"] + ["~/Zzz-not-there/"]}})
        m._excl = None
        self.assertNotEqual(mi.rules_hash(m.excluder(), m.roots()), h0)


# ====================================================================== the command line

def ns(*words, **kw):
    return Namespace(words=list(words), scope="mac", collection=None, limit=5, force=False, json=kw.get("json", False),
                     paths=False, all=kw.get("all", False))


class Cli(unittest.TestCase):
    def setUp(self):
        self.s = Settings()
        self.s.save()
        self.work = Path(mkdtemp()).resolve()
        (self.work / "proj").mkdir()
        p = mock.patch.object(cli, "daemon_up", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.s.restore()
        mi.manager()._excl = None

    def run_cli(self, *words, env=None, **kw):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}), redirect_stdout(out), redirect_stderr(err):
            try:
                cli.cmd_files(ns(*words, **kw))
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def exclude(self):
        return config.load()["mac_index"]["exclude"]

    def test_exclude_add_list_rm(self):
        folder = str(self.work / "proj")
        code, out, _ = self.run_cli("exclude", "add", folder, "node_modules2", "*.bak")
        self.assertEqual(code, 0)
        self.assertEqual(self.exclude()[-3:], [folder + "/", "node_modules2", "*.bak"])
        self.assertIn("added exclusion: " + folder + "/", out)
        self.assertIn("service is not running", out)
        code, out, _ = self.run_cli("exclude", "add", folder + "/")                    # again: nothing changes
        self.assertEqual(code, 0)
        self.assertIn("already in the list", out)
        self.assertEqual(self.exclude().count(folder + "/"), 1)
        code, out, _ = self.run_cli("exclude", "list")
        self.assertIn(folder, out)
        self.assertIn("*.bak", out)
        self.assertIn("Always excluded", out)
        self.assertNotIn("~/.ssh/", out)
        code, out, _ = self.run_cli("exclude", "list", all=True)
        self.assertIn("~/.ssh/", out)
        self.assertIn("Keys, tokens and credentials", out)
        code, out, _ = self.run_cli("exclude", "rm", folder, "*.bak", "never-added")
        self.assertEqual(code, 0)
        self.assertEqual(self.exclude(), self.s._saved["exclude"] + ["node_modules2"])
        self.assertIn("removed", out)
        self.assertIn("not in the list: never-added", out)

    def test_exclude_list_json(self):
        code, out, _ = self.run_cli("exclude", "list", json=True)
        d = json.loads(out)
        self.assertEqual([e["entry"] for e in d["exclude"]], self.exclude())
        self.assertTrue(any(g["label"] == "Keys, tokens and credentials" for g in d["hard_excluded"]))

    def test_the_same_refusals_as_the_api(self):
        before = self.exclude()
        for arg, needle in (("/", "hide every file"), ("~", "home folder"), ("~/x/../y", "'..'"), ("~bob/x", "another user"),
                            ("!node_modules", "cannot start with"), ("!~/.ssh", "cannot start with"),
                            ("#~/x", "cannot start with")):
            code, _, _ = self.run_cli("exclude", "add", arg)
            self.assertIn("error:", str(code), arg)
            self.assertIn(needle, str(code), arg)
        self.assertEqual(self.exclude(), before)

    def test_a_relative_folder_is_made_absolute_against_the_current_folder(self):
        self.assertEqual(cli._cli_folder("proj", True), os.path.abspath("proj"))
        self.assertEqual(cli._cli_folder("sub/dir", False), os.path.abspath("sub/dir"))
        self.assertEqual(cli._cli_folder(".", False), os.path.abspath("."))
        self.assertEqual(cli._cli_folder("node_modules", False), "node_modules")          # a name stays a name
        self.assertEqual(cli._cli_folder("*.tmp", False), "*.tmp")
        self.assertEqual(cli._cli_folder("~/x", True), "~/x")
        cwd = os.getcwd()
        try:
            os.chdir(self.work)
            code, out, _ = self.run_cli("exclude", "add", "./proj")
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0)
        self.assertIn(str(self.work / "proj") + "/", self.exclude())

    def test_roots_add_rm_list(self):
        extra = str(self.work / "proj")
        code, out, _ = self.run_cli("roots", "add", extra)
        self.assertEqual(code, 0)
        self.assertEqual(config.load()["mac_index"]["roots"][-1], extra)
        self.assertIn("added root: " + extra, out)
        code, out, _ = self.run_cli("roots", "list")
        self.assertIn(extra, out)
        code, out, _ = self.run_cli("roots", "rm", extra)
        self.assertEqual(config.load()["mac_index"]["roots"], self.s._saved["roots"])
        code, _, _ = self.run_cli("roots", "add", str(self.work / "missing"))
        self.assertIn("not a folder", str(code))
        code, _, _ = self.run_cli("roots", "add", os.path.expanduser("~/.ssh"))
        self.assertTrue("never indexed" in str(code) or "not a folder" in str(code))
        only = config.load()["mac_index"]["roots"]
        code, _, _ = self.run_cli("roots", "rm", *only)
        self.assertIn("at least one folder", str(code))
        self.assertEqual(config.load()["mac_index"]["roots"], only)

    def test_roots_refuses_a_hard_blocked_folder_even_when_it_exists(self):
        ssh = os.path.join(self.work, ".ssh")
        os.mkdir(ssh)
        code, _, _ = self.run_cli("roots", "add", ssh)
        self.assertIn("never indexed", str(code))                                    # (a .ssh name is blocked anywhere)
        self.assertNotIn(ssh, config.load()["mac_index"]["roots"])

    def test_locked_agent_sessions_cannot_manage_it(self):
        inside = Path(mkdtemp(prefix="mse-mx-lock-")).resolve()
        config.add_collection(str(inside), "mx-locked")
        try:
            env = {"MSE_LOCK": "1", "MSE_CWD": str(inside)}
            for words in (("exclude", "add", "~/Music"), ("exclude", "list"), ("roots", "add", str(inside)),
                          ("roots", "rm", "/tmp/x")):
                before = config.load()["mac_index"]
                code, _, _ = self.run_cli(*words, env=env)
                self.assertIn("locked", str(code), words)
                self.assertEqual(config.load()["mac_index"], before)
        finally:
            config.remove_collection("mx-locked")

    def test_words_that_are_only_a_search_stay_a_search(self):
        seen = []

        def fake_search(q, scope="mac", names=None, limit=None):
            seen.append(q)
            return {"items": [], "total": 0, "ms": 1.0, "scope": scope, "query": {}, "truncated": False, "searchable": True}
        with mock.patch("mse.server.files_search", side_effect=fake_search):
            self.run_cli("exclude")
            self.run_cli("roots", "list", "of", "things")
            self.run_cli("search", "exclude", "add")
            self.run_cli("roots")
        self.assertEqual(seen, ["exclude", "roots list of things", "exclude add", "roots"])

    def test_add_without_a_folder_is_a_usage_error(self):
        code, _, _ = self.run_cli("exclude", "add")
        self.assertIn("usage", str(code))


if __name__ == "__main__":
    unittest.main()
