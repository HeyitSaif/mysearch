"""The whole-disk content index (mse/wholedisk.py): scope and exclusions, change tracking against the name index, the
descriptors per kind (media through a stand-in describer, text and office documents through the real readers), the
power and idle gate, the settings, and the search contract (kinds, labels, why, paging, verification on disk).

Every test runs on a temporary tree under /private/tmp (never under the home folder, never the owner's files) with a
temporary MySearch home, the name index built by the real walker (macindex.walk_python) and the real rules. The helper
is not needed here: test_wholedisk_helper.py runs msehelper on real inputs when MSE_TEST_HELPER is set."""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import config, macindex as mi, paths, wholedisk as wd  # noqa: E402

BASE = "/private/tmp" if os.path.isdir("/private/tmp") else "/tmp"   # the system temp folder is hard-excluded by the rules
OLD = int(time.time()) - 7 * 86400                                 # fixtures are older than MIN_AGE_S


def isolate(add_cleanup) -> None:
    """Point the MySearch data folder, its config, its temporary folder and its collections at a fresh folder for this
    test. The run's shared MSE_HOME is left alone: a collection or a crawled tree left in it breaks the other modules."""
    home = Path(tempfile.mkdtemp(prefix="wd-data-", dir=BASE))
    for name, value in (("DATA", home), ("CONFIG", home / "config.json"), ("TMP", home / "tmp"),
                        ("COLLECTIONS", home / "collections")):
        patch = mock.patch.object(paths, name, value)
        patch.start()
        add_cleanup(patch.stop)


def _root() -> str:
    return os.path.realpath(tempfile.mkdtemp(prefix="wd-", dir=BASE))


def _write(path: str, data: bytes | str = "x\n", mtime: int | None = OLD) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as f:
        f.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _crawl(root: str) -> None:
    """The name index for `root`, as the real crawl would leave it: rows from the walker, rules applied, crawl finished."""
    store = mi.Store()
    excl = mi.manager().excluder()
    rows = list(mi.walk_python(root, excl))
    gen = store.next_gen()
    mi.ingest(store, iter(rows), gen, excl)
    store.sweep(root, gen)                                       # what the crawl did not see again is gone
    store.set_meta("crawl", json.dumps({"gen": gen, "finished": time.time(), "rules": "test", "roots": [root],
                                        "done": []}))


def _configure(root: str, **wholedisk_settings) -> None:
    config.update_settings({"mac_index": {"roots": [root], "skip_protected": "never"}})
    config.update_settings({"wholedisk": {"roots": [root], "priority": "low", "extra_excludes": [],
                                          **wholedisk_settings}})


OPEN = object()                                                  # the gate is open: nothing paces the test


def _manager(describer=None, gate=OPEN) -> wd.Manager:
    if gate is OPEN:
        gate = lambda cfg: (True, None)                          # noqa: E731
    return wd.Manager(db=os.path.join(tempfile.mkdtemp(prefix="wd-db-", dir=BASE), "wholedisk.db"),
                      describer=describer, gate=gate)


def _files(m: wd.Manager) -> dict:
    """path -> (name, kind, dkind, state) of every entry."""
    c = m.store.writer()
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in c.execute("SELECT path, name, kind, dkind, state FROM files")}


def _fake_describer(labels=("zebra", "animal"), text=""):
    """Stands in for the helper: every image, PDF and video gets the given labels."""
    calls = []

    def describe(path, dkind, ext, cfg):
        calls.append((path, dkind))
        return {"text": text, "title": "", "labels": [(lab, 0.9) for lab in labels], "lang": "", "lines": 0,
                "ocr": False}
    describe.calls = calls
    return describe


class ScopeAndSyncTests(unittest.TestCase):
    def setUp(self):
        isolate(self.addCleanup)
        self.root = _root()
        t = self.root
        _write(f"{t}/Desktop/notes.txt", "meeting notes about the roof\n")
        _write(f"{t}/Documents/invoice.txt", "Rechnung 4711 Telekom\n")
        _write(f"{t}/Documents/.hidden.txt", "hidden words\n")
        _write(f"{t}/Documents/credentials.txt", "password here\n")
        _write(f"{t}/Documents/.env", "TOKEN=abc\n")
        _write(f"{t}/Documents/id_rsa.txt", "key material\n")
        _write(f"{t}/Documents/server.pem", "-----BEGIN\n")
        _write(f"{t}/Documents/.ssh/config.txt", "Host x\n")
        _write(f"{t}/Documents/app/node_modules/pkg/index.txt", "dependency text\n")
        _write(f"{t}/Documents/app/package.json", "{}\n")
        _write(f"{t}/Documents/app/build/out.txt", "build output\n")
        _write(f"{t}/Documents/lib/build/keep.txt", "kept build folder without marker\n")
        _write(f"{t}/Documents/env/pyvenv.cfg", "home = /usr\n")
        _write(f"{t}/Documents/env/readme.txt", "venv text\n")
        _write(f"{t}/Documents/env2/readme.txt", "kept env2 text\n")
        _write(f"{t}/Documents/proj/.git/HEAD", "ref\n")
        _write(f"{t}/Documents/proj/readme.md", "# repository\n")
        _write(f"{t}/Documents/Notes.pages/QuickLook/Preview.pdf", b"%PDF-1.4 preview")
        _write(f"{t}/Documents/tool.app/Contents/Info.txt", "bundle text\n")
        _write(f"{t}/Documents/font.ttf", b"\x00\x01binary")
        _write(f"{t}/Documents/.cache/x.txt", "cache\n")
        _write(f"{t}/Other/misc.txt", "misc\n")
        _configure(t)
        _crawl(t)
        self.m = _manager(describer=_fake_describer())
        self.m.sync(full=True)
        self.m.describe_pending()

    def names(self):
        return {os.path.relpath(p, self.root): v for p, v in _files(self.m).items()}

    def test_secrets_hidden_folders_and_junk_are_not_entries(self):
        names = self.names()
        for gone in ("Documents/.hidden.txt", "Documents/credentials.txt", "Documents/.env", "Documents/id_rsa.txt",
                     "Documents/server.pem", "Documents/.ssh/config.txt", "Documents/app/node_modules/pkg/index.txt",
                     "Documents/app/build/out.txt", "Documents/env/readme.txt", "Documents/env/pyvenv.cfg",
                     "Documents/proj/readme.md", "Documents/proj/.git/HEAD", "Documents/Notes.pages/QuickLook/Preview.pdf",
                     "Documents/tool.app/Contents/Info.txt", "Documents/.cache/x.txt"):
            self.assertNotIn(gone, names, gone)

    def test_ordinary_files_are_entries(self):
        names = self.names()
        self.assertIn("Desktop/notes.txt", names)
        self.assertIn("Documents/invoice.txt", names)
        self.assertIn("Documents/lib/build/keep.txt", names)     # a build folder with no project marker is kept
        self.assertIn("Documents/env2/readme.txt", names)        # only a folder named env with pyvenv.cfg is pruned
        self.assertIn("Documents/app/package.json", names)

    def test_pages_package_is_an_entry_and_its_contents_are_not(self):
        names = self.names()
        self.assertEqual(names["Documents/Notes.pages"][1], "doc")
        self.assertEqual(names["Documents/Notes.pages"][2], "doc")

    def test_name_only_kinds_are_listed_and_not_described(self):
        names = self.names()
        self.assertEqual(names["Documents/font.ttf"][3], wd.ST_NAME)
        self.assertIsNone(names["Documents/font.ttf"][2])

    def test_priority_folders_come_first(self):
        c = self.m.store.writer()
        prio = dict(c.execute("SELECT name, prio FROM files"))
        self.assertEqual(prio["notes.txt"], 3)                  # Desktop
        self.assertEqual(prio["invoice.txt"], 3)                # Documents
        self.assertEqual(prio["misc.txt"], 1)                   # a folder that is not one of the four

    def test_symlink_is_never_read(self):
        outside = _write(f"{self.root}/elsewhere/real.txt", "TOPSECRETLINK content\n")
        os.symlink(outside, f"{self.root}/Documents/link.txt")
        os.utime(f"{self.root}/Documents/link.txt", (OLD, OLD), follow_symlinks=False)
        _crawl(self.root)
        self.m.sync(full=True)
        self.m.describe_pending()
        rows = self.names()
        self.assertIn("Documents/link.txt", rows)
        self.assertEqual(rows["Documents/link.txt"][3], wd.ST_NAME)
        res = self.m.search("TOPSECRETLINK", limit=10)
        self.assertFalse(any(r["path"].endswith("link.txt") for r in res["results"]))
        self.assertTrue(any(r["path"].endswith("real.txt") for r in res["results"]))

    def test_extra_excludes_hide_a_folder_at_once_and_drop_its_rows(self):
        _write(f"{self.root}/Documents/private/memo.txt", "confidential memorandum about the merger\n")
        _crawl(self.root)
        self.m.sync(full=True)
        self.m.describe_pending()
        self.assertTrue(any(r["path"].endswith("memo.txt") for r in self.m.search("memorandum", limit=5)["results"]))
        config.update_settings({"wholedisk": {"extra_excludes": ["private"]}})
        res = self.m.search("memorandum", limit=5)
        self.assertEqual(res["results"], [])
        self.assertNotIn("Documents/private/memo.txt", self.names())   # the stale row is removed by the search itself

    def test_a_deleted_file_is_not_returned_even_before_the_next_merge(self):
        self.assertTrue(any(r["path"].endswith("invoice.txt") for r in self.m.search("Telekom", limit=5)["results"]))
        os.rename(f"{self.root}/Documents/invoice.txt", f"{self.root}/Documents/invoice.moved")
        res = self.m.search("Telekom", limit=5)
        self.assertFalse(any(r["path"].endswith("invoice.txt") for r in res["results"]))

    def test_fast_look_finds_new_rows_and_the_merge_drops_removed_ones(self):
        _write(f"{self.root}/Desktop/fresh.txt", "freshly written about gardens\n", mtime=None)
        _write(f"{self.root}/Desktop/old.txt", "old\n")
        _crawl(self.root)
        out = self.m.sync()                                     # the rowid look: the new rows are seen at once
        self.assertGreaterEqual(out.get("looked_at", 0), 1)
        self.assertIn("Desktop/fresh.txt", self.names())
        os.rename(f"{self.root}/Desktop/old.txt", f"{self.root}/Desktop/old.moved")
        _crawl(self.root)
        out = self.m.sync(full=True)
        self.assertGreaterEqual(out["removed"], 1)
        self.assertNotIn("Desktop/old.txt", self.names())

    def test_settings_change_triggers_a_full_merge(self):
        cfg = wd.settings()
        self.assertTrue(self.m.sync().get("looked_at") is not None)
        config.update_settings({"wholedisk": {"images": False}})
        out = self.m.sync()
        self.assertTrue(out.get("full"))                       # the settings key changed: a full merge, not a fast look
        config.update_settings({"wholedisk": {"images": cfg["images"]}})

    def test_name_index_not_ready_means_nothing_is_synced(self):
        with mock.patch.object(wd, "_name_index_ready", return_value=False):
            self.assertIn("skipped", self.m.sync(full=True))
        with mock.patch.object(mi.manager(), "crawling", True, create=True):
            self.assertIn("skipped", self.m.sync(full=True))


class DescriptorTests(unittest.TestCase):
    def setUp(self):
        isolate(self.addCleanup)
        self.root = _root()
        _configure(self.root, max_text_kb=8)

    def _entry(self, rel, data, mtime=OLD):
        _write(f"{self.root}/{rel}", data, mtime=mtime)
        _crawl(self.root)
        m = _manager(describer=_fake_describer(labels=("zebra", "animal"), text="zebra text on the page"))
        m.sync(full=True)
        m.describe_pending()
        return m

    def test_text_document_is_read_with_its_headings_and_language(self):
        m = self._entry("Documents/plan.md", "# Projektplan\n\nDie Zusammenarbeit ist nicht einfach, und wir werden es sehen.\n")
        row = m.store.writer().execute("SELECT title, text, lang, state FROM files").fetchone()
        self.assertEqual(row[0], "Projektplan")
        self.assertIn("Zusammenarbeit", row[1])
        self.assertEqual(row[2], "de")
        self.assertEqual(row[3], wd.ST_DONE)

    def test_text_is_capped_at_max_text_kb(self):
        big = ("wort " * 4000)                                  # 20 kB of text
        m = self._entry("Documents/long.txt", big)
        (text,) = m.store.writer().execute("SELECT text FROM files").fetchone()
        self.assertLessEqual(len(text), 8 * 1024)
        self.assertTrue(text.startswith("wort"))

    def test_csv_json_and_html_are_text(self):
        m = self._entry("Documents/table.csv", "name,amount\nTelekom,49.99\n")
        self.assertIn("Telekom", m.store.writer().execute("SELECT text FROM files").fetchone()[0])
        m = self._entry("Documents/page.html", "<html><title>T</title><body><p>Gartenhaus Ausstellung</p></body></html>")
        self.assertIn("Gartenhaus", m.store.writer().execute("SELECT text FROM files").fetchone()[0])

    def test_docx_is_read_from_its_document_part(self):
        path = f"{self.root}/Documents/brief.docx"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               '<w:body><w:p><w:r><w:t>Kuendigung des Vertrags zum Monatsende</w:t></w:r></w:p></w:body></w:document>')
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("word/document.xml", xml)
        os.utime(path, (OLD, OLD))
        _crawl(self.root)
        m = _manager(describer=_fake_describer())
        m.sync(full=True)
        m.describe_pending()
        self.assertIn("Kuendigung", m.store.writer().execute("SELECT text FROM files").fetchone()[0])

    def test_eml_is_read_by_subject_and_body(self):
        m = self._entry("Documents/mail.eml",
                        "Subject: Angebot Gartenpflege\nFrom: a@example.com\nTo: b@example.com\nContent-Type: text/plain\n\n"
                        "Das Angebot liegt bei.\n")
        text, title = m.store.writer().execute("SELECT text, title FROM files").fetchone()
        self.assertIn("Angebot", title)
        self.assertIn("liegt bei", text)

    def test_image_gets_its_labels_and_they_are_searchable(self):
        m = self._entry("Documents/photos/p1.jpg", b"\xff\xd8\xff fake jpeg bytes")
        row = m.store.writer().execute("SELECT labels, state, dkind FROM files").fetchone()
        self.assertEqual(row[1], wd.ST_DONE)
        self.assertEqual(row[2], "image")
        self.assertIn("zebra", [x[0] for x in json.loads(row[0])])
        res = m.search("image of a zebra", limit=3)
        self.assertEqual(res["query"]["kinds"], ["image"])
        self.assertEqual(res["results"][0]["why"], "label")
        self.assertIn("zebra", res["results"][0]["labels"])

    def test_pdf_and_video_go_through_the_describer(self):
        _write(f"{self.root}/Documents/scan.pdf", b"%PDF-1.4 fake")
        _write(f"{self.root}/Documents/clip.mov", b"fake movie bytes")
        _crawl(self.root)
        desc = _fake_describer()
        m = _manager(describer=desc)
        m.sync(full=True)
        m.describe_pending()
        kinds = sorted(k for _p, k in desc.calls)
        self.assertEqual(kinds, ["pdf", "video"])

    def test_a_failure_is_counted_and_waits_for_a_change(self):
        _write(f"{self.root}/Documents/bad.jpg", b"not an image")
        _crawl(self.root)

        def failing(path, dkind, ext, cfg):
            raise wd.DescribeFailed("cannot read image")
        m = _manager(describer=failing)
        m.sync(full=True)
        self.assertEqual(m.describe_pending()["failed"], 1)
        (state, fails) = m.store.writer().execute("SELECT state, fails FROM files").fetchone()
        self.assertEqual((state, fails), (wd.ST_FAILED, 1))
        m.sync(full=True, rescan=True)                         # a rescan tries again while tries are left
        self.assertEqual(m.store.writer().execute("SELECT state FROM files").fetchone()[0], wd.ST_PENDING)
        for _ in range(3):
            m.describe_pending()
            m.sync(full=True, rescan=True)
        self.assertEqual(m.store.writer().execute("SELECT state FROM files").fetchone()[0], wd.ST_FAILED)
        self.assertEqual(m.store.writer().execute("SELECT fails FROM files").fetchone()[0] >= wd.MAX_FAILS, True)

    def test_a_file_too_big_for_its_kind_is_name_only(self):
        big = f"{self.root}/Documents/huge.txt"
        _write(big, "x\n")
        os.truncate(big, wd.MAX_DOC_BYTES + 1)
        _crawl(self.root)
        m = _manager(describer=_fake_describer())
        m.sync(full=True)
        self.assertIsNone(m.store.writer().execute("SELECT dkind FROM files WHERE name = 'huge.txt'").fetchone()[0])

    def test_dataless_file_is_refused_before_any_read(self):
        _write(f"{self.root}/Documents/cloud.jpg", b"\xff\xd8 placeholder")
        _crawl(self.root)
        m = _manager(describer=_fake_describer())
        m.sync(full=True)
        real = os.lstat

        def dataless(path, *a, **k):
            st = real(path, *a, **k)
            if path.endswith("cloud.jpg"):
                return SimpleNamespace(st_mode=st.st_mode, st_size=st.st_size, st_mtime=st.st_mtime,
                                       st_flags=wd.DATALESS)
            return st
        with mock.patch("mse.wholedisk.os.lstat", side_effect=dataless):
            m.describe_pending()
        row = m.store.writer().execute("SELECT state, fails, fail_key FROM files WHERE name = 'cloud.jpg'").fetchone()
        self.assertEqual(row[0], wd.ST_FAILED)
        self.assertIn("dataless", m.last_error)

    def test_pages_preview_is_the_quicklook_pdf(self):
        path = f"{self.root}/Documents/Plan.pages"
        os.makedirs(f"{path}/QuickLook", exist_ok=True)
        with open(f"{path}/QuickLook/Preview.pdf", "wb") as f:
            f.write(b"%PDF-1.4 preview-bytes")
        dest = wd._pages_preview(path)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"%PDF-1.4 preview-bytes")
        zpath = f"{self.root}/Documents/Old.pages"
        with zipfile.ZipFile(zpath, "w") as z:
            z.writestr("QuickLook/Preview.pdf", b"%PDF-1.4 zipped")
        with open(wd._pages_preview(zpath), "rb") as f:
            self.assertEqual(f.read(), b"%PDF-1.4 zipped")


class GateTests(unittest.TestCase):
    def setUp(self):
        isolate(self.addCleanup)
        self.root = _root()
        _configure(self.root)
        self.m = wd.Manager(db=os.path.join(tempfile.mkdtemp(prefix="wd-db-", dir=BASE), "wholedisk.db"))

    def gate(self, priority, ac, idle, enabled=True):
        cfg = {"enabled": enabled, "priority": priority}
        with mock.patch("mse.jobs.on_ac_power_cached", return_value=ac), \
                mock.patch("mse.jobs.idle_seconds_cached", return_value=idle):
            return self.m.gate(cfg)

    def test_low_describes_only_on_ac_power_after_a_minute_idle(self):
        self.assertEqual(self.gate("low", False, 600), (False, "on_battery"))
        self.assertEqual(self.gate("low", True, 10), (False, "user_active"))
        self.assertEqual(self.gate("low", True, 59.9), (False, "user_active"))
        self.assertEqual(self.gate("low", True, 60), (True, None))

    def test_balanced_runs_on_ac_or_after_a_minute_idle_and_stops_on_activity(self):
        self.assertEqual(self.gate("balanced", True, 3), (False, "user_active"))
        self.assertEqual(self.gate("balanced", True, 10), (True, None))
        self.assertEqual(self.gate("balanced", False, 30), (False, "on_battery"))
        self.assertEqual(self.gate("balanced", False, 90), (True, None))
        self.assertEqual(self.gate("balanced", False, 2), (False, "user_active"))

    def test_disabled_is_reported(self):
        self.assertEqual(self.gate("low", True, 600, enabled=False), (False, "disabled"))

    def test_the_describer_stops_at_once_when_the_user_comes_back(self):
        root = _root()
        _configure(root)
        for i in range(6):
            _write(f"{root}/Documents/pic{i}.jpg", b"\xff\xd8 bytes")
        _crawl(root)
        seen = []

        def describer(path, dkind, ext, cfg):
            seen.append(path)
            return {"text": "", "title": "", "labels": [("zebra", 0.9)], "lang": "", "lines": 0, "ocr": False}
        answers = iter([True, True, False])
        m = _manager(describer=describer, gate=lambda cfg: (next(answers, False), "user_active"))
        m.sync(full=True)
        out = m.describe_pending(limit=None)
        self.assertEqual(len(seen), 2)
        self.assertEqual(out["paused_reason"], "user_active")
        self.assertEqual(m.pending(), 4)                        # the rest wait; nothing is lost


class SettingsTests(unittest.TestCase):
    def setUp(self):
        isolate(self.addCleanup)

    def test_valid_settings_are_normalised(self):
        v = wd.check_settings({"roots": ["/private/tmp/a", "/private/tmp/a/"], "priority": "balanced",
                               "max_text_kb": 16, "extra_excludes": ["private", "*.tmp"]})
        self.assertEqual(v["roots"], ["/private/tmp/a"])
        self.assertEqual(v["priority"], "balanced")

    def test_invalid_settings_are_refused(self):
        bad = [{"priority": "fast"}, {"unknown": 1}, {"max_text_kb": 0}, {"max_text_kb": True},
               {"extra_excludes": ["!keep"]}, {"roots": []}, {"roots": ["relative/path"]}, {"images": "yes"},
               {"enabled": 1}]
        for v in bad:
            with self.subTest(v=v), self.assertRaises(ValueError):
                wd.check_settings(v)

    def test_update_settings_accepts_and_stores_the_section(self):
        cfg = config.update_settings({"wholedisk": {"priority": "balanced", "max_text_kb": 12}})
        self.assertEqual(cfg["wholedisk"]["priority"], "balanced")
        self.assertEqual(config.load()["wholedisk"]["max_text_kb"], 12)
        config.update_settings({"wholedisk": {"priority": "low", "max_text_kb": 8}})
        with self.assertRaises(ValueError):
            config.update_settings({"wholedisk": {"priority": "turbo"}})

    def test_the_defaults_match_the_contract(self):
        d = config.DEFAULTS["wholedisk"]
        self.assertEqual({k: d[k] for k in ("enabled", "priority", "images", "documents", "ocr_images", "max_text_kb")},
                         {"enabled": True, "priority": "low", "images": True, "documents": True, "ocr_images": True,
                          "max_text_kb": 8})
        self.assertEqual(len(d["roots"]), 1)                     # the home folder, as it was when the defaults were read
        self.assertTrue(os.path.isabs(d["roots"][0]))
        self.assertEqual(d["extra_excludes"], [])


class PrivacyTests(unittest.TestCase):
    """A secret, a hidden folder or a package inside a root is not an entry and is never described, whatever the
    describer would do with it. ~/Library is checked in test_wholedisk_contract.py, where the home folder is the fixture."""

    def setUp(self):
        isolate(self.addCleanup)
        self.root = _root()
        _configure(self.root)

    def test_secret_names_hidden_folders_and_package_insides_are_never_described(self):
        never = ["Documents/.env", "Documents/.env.local", "Documents/keys/id_rsa", "Documents/keys/id_rsa.pub",
                 "Documents/cert.pem", "Documents/server.key", "Documents/store.p12", "Documents/kiwi-credentials/x.md",
                 "Documents/secrets/plan.md", ".quiet/note.md", "Documents/.hidden.md", "Documents/Deck.key/Index/a.iwa",
                 "Documents/Report.pages/Index/b.iwa", "Documents/Tool.app/Contents/Info.plist",
                 "Documents/Keychains/login.keychain-db", "Documents/site/node_modules/pkg/readme.md"]
        for rel in never:
            _write(f"{self.root}/{rel}", "kiwi\n")
        _write(f"{self.root}/Documents/plain.md", "# Kiwi\n\nA plain note.\n")
        _write(f"{self.root}/Pictures/kiwi.jpg", b"\xff\xd8 bytes")
        _crawl(self.root)
        describer = _fake_describer()
        m = _manager(describer=describer)
        m.sync(full=True)
        m.describe_pending()
        entries = set(_files(m))
        # the .pages package is an entry (a document); its insides are not
        self.assertEqual(entries, {f"{self.root}/Documents/plain.md", f"{self.root}/Pictures/kiwi.jpg",
                                   f"{self.root}/Documents/Report.pages"})
        # the package was just made (its folder's mtime is now), so it waits for MIN_AGE_S: only the picture is described
        self.assertEqual([p for p, _k in describer.calls], [f"{self.root}/Pictures/kiwi.jpg"])
        self.assertEqual({r["name"] for r in m.search("kiwi", limit=50)["results"]}, {"plain.md", "kiwi.jpg"})

    def test_a_file_excluded_after_the_look_is_removed_before_it_is_read(self):
        """extra_excludes set between the look and the describing: the file is checked again and never read."""
        _write(f"{self.root}/Pictures/a.jpg", b"\xff\xd8 a")
        _write(f"{self.root}/Pictures/b.jpg", b"\xff\xd8 b")
        _crawl(self.root)
        describer = _fake_describer()
        m = _manager(describer=describer)
        m.sync(full=True)
        self.assertEqual(m.pending(), 2)
        config.update_settings({"wholedisk": {"extra_excludes": ["a.jpg"]}})
        m.describe_pending()
        self.assertEqual([p for p, _k in describer.calls], [f"{self.root}/Pictures/b.jpg"])
        self.assertNotIn(f"{self.root}/Pictures/a.jpg", _files(m))


class ReviewFixTests(unittest.TestCase):
    """The behaviour fixed in the review: the helper's QoS follows the priority, the switch stops a batch within one file,
    a missing helper leaves pictures waiting (not failing) while documents are read, a helper that dies mid-batch costs one
    file, a search never waits behind the writer, a removed row is counted once, an absent root keeps its rows."""

    def setUp(self):
        isolate(self.addCleanup)
        self.root = _root()
        _configure(self.root)

    def test_helper_qos_follows_the_priority(self):
        m = _manager()
        self.assertIn("utility", m._helper_opts({"ocr_images": True, "priority": "balanced"}))
        self.assertIn("background", m._helper_opts({"ocr_images": True, "priority": "low"}))
        low = m._helper_opts({"ocr_images": False, "priority": "low"})
        self.assertEqual(low[low.index("--qos") + 1], "background")
        self.assertEqual(low[low.index("--ocr") + 1], "off")
        self.assertEqual(wd.HELPER_QOS, {"low": "background", "balanced": "utility"})

    def test_turning_the_switch_off_stops_the_batch_within_one_file(self):
        for i in range(6):
            _write(f"{self.root}/Pictures/p{i}.jpg", b"\xff\xd8 bytes")
        _crawl(self.root)
        seen = []

        def describer(path, dkind, ext, cfg):
            seen.append(path)
            config.update_settings({"wholedisk": {"enabled": False}})   # the user turns it off while this file is read
            return {"text": "", "title": "", "labels": [("zebra", 0.9)], "lang": "", "lines": 0, "ocr": False}
        m = _manager(describer=describer)
        m.sync(full=True)
        out = m.describe_pending(limit=None)
        self.assertEqual(len(seen), 1)
        self.assertEqual(out, {"described": 1, "failed": 0, "paused_reason": "disabled"})
        self.assertEqual(m.pending(), 5)
        self.assertEqual(m.status()["state"], "paused")
        self.assertEqual(m.status()["paused_reason"], "disabled")

    def test_without_the_helper_pictures_wait_and_documents_are_read(self):
        _write(f"{self.root}/Pictures/p.jpg", b"\xff\xd8 bytes")
        _write(f"{self.root}/Documents/note.md", "# Zebra\n\nnotes\n")
        _write(f"{self.root}/Documents/Letter.pages/Index/x.iwa", "x")   # a .pages package needs the helper too
        _crawl(self.root)
        m = _manager(describer=None)                                      # the real path: the helper binary
        with mock.patch.object(wd.macindex, "helper_path", return_value=Path(self.root) / "no-such-helper"):
            self.assertFalse(m.media_ok())
            m.sync(full=True)
            out = m.describe_pending(gate=False)
        self.assertEqual((out["described"], out["failed"]), (1, 0))
        files = _files(m)
        self.assertEqual(files[f"{self.root}/Documents/note.md"][3], wd.ST_DONE)
        self.assertEqual(files[f"{self.root}/Pictures/p.jpg"][3], wd.ST_PENDING)             # waits, not failed
        self.assertEqual(files[f"{self.root}/Documents/Letter.pages"][3], wd.ST_PENDING)
        self.assertIn("helper", m.last_error)
        self.assertEqual(m.pending(), 2)

    def test_a_helper_that_dies_mid_batch_costs_one_file_and_is_started_again(self):
        for i in range(3):
            _write(f"{self.root}/Pictures/p{i}.jpg", b"\xff\xd8 bytes")
        _crawl(self.root)
        # a stand-in msehelper: answers the first line, dies on the second (as a crash inside Vision would)
        answer = json.dumps({"id": 0, "ok": True, "labels": [{"id": "zebra", "c": 0.9}], "text": "", "lines": 0,
                             "ocr": False, "lang": ""})
        prog = Path(self.root) / "fake-helper.py"
        prog.write_text("import sys\nsys.stdin.readline()\nprint(" + repr(answer) + ", flush=True)\n"
                        "sys.stdin.readline()\nsys.exit(3)\n")
        fake = Path(self.root) / "fake-helper.sh"
        fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{prog}" "$@"\n')
        fake.chmod(0o755)
        m = _manager(describer=None)
        with mock.patch.object(wd.macindex, "helper_path", return_value=fake):
            m.sync(full=True)
            out = m.describe_pending(gate=False)
        self.assertEqual((out["described"], out["failed"]), (2, 1))
        states = sorted(v[3] for v in _files(m).values())
        self.assertEqual(states, [wd.ST_DONE, wd.ST_DONE, wd.ST_FAILED])
        self.assertIn("exited", m.last_error)
        row = m.store.writer().execute("SELECT fails FROM files WHERE state = ?", (wd.ST_FAILED,)).fetchone()
        self.assertEqual(row[0], 1)                                       # one try used; a rescan tries again

    def test_a_search_never_waits_behind_the_writer(self):
        _write(f"{self.root}/Documents/zebra-notes.md", "# Zebra\n\nherd\n")
        _write(f"{self.root}/Documents/zebra-two.md", "# Zebra two\n\nherd\n")
        _crawl(self.root)
        m = _manager()
        m.sync(full=True)
        m.describe_pending()
        gone = f"{self.root}/Documents/zebra-two.md"
        os.rename(gone, gone + ".moved")                                 # deleted since the look
        held, release = threading.Event(), threading.Event()

        def hold():                                                       # a merge in progress, on another thread
            with m.store.lock:
                held.set()
                release.wait(10)
        t = threading.Thread(target=hold, daemon=True)
        t.start()
        self.assertTrue(held.wait(5))
        try:
            t0 = time.monotonic()
            res = m.search("zebra", limit=5)
            self.assertLess(time.monotonic() - t0, 2.0)
            self.assertEqual([r["name"] for r in res["results"]], ["zebra-notes.md"])
            self.assertEqual(len(m._stale), 1)                            # remembered, not removed: the writer is busy
        finally:
            release.set()
            t.join(5)
        m._drain_stale()                                                  # the thread's next turn
        self.assertEqual(m._stale, set())
        self.assertNotIn(gone, _files(m))

    def test_a_removed_row_is_counted_once(self):
        _write(f"{self.root}/Documents/a.md", "a\n")
        _write(f"{self.root}/Documents/b.md", "b\n")
        _crawl(self.root)
        m = _manager()
        self.assertEqual(m.sync(full=True)["added"], 2)
        os.rename(f"{self.root}/Documents/a.md", f"{self.root}/Documents/a.moved.bin")
        _crawl(self.root)
        out = m.sync(full=True)
        self.assertEqual((out["added"], out["removed"]), (1, 1))

    def test_an_absent_root_keeps_its_rows(self):
        cfg = {**wd.settings(), "roots": [self.root, "/Volumes/NoSuchDisk"]}
        scope = wd.Scope(cfg, mi.manager().excluder(), lambda p: False)
        self.assertEqual(scope.root_of("/Volumes/NoSuchDisk/Photos/a.jpg"), "/Volumes/NoSuchDisk")
        self.assertEqual(scope.root_of(f"{self.root}/x.txt"), self.root)
        self.assertIsNone(scope.root_of("/Volumes/Other/x.txt"))

    def test_the_fill_ranks_the_rarest_concept_first_and_stops_when_the_page_is_full(self):
        """One rare and one common word: the rows of the common word alone are not ranked once the rare word's rows fill
        the page (the cost that made text-heavy queries slow)."""
        for i in range(30):
            _write(f"{self.root}/Documents/invoice-{i}.md", f"# Invoice {i}\n\nRechnung number {i}\n")
        for i in range(3):
            _write(f"{self.root}/Pictures/zebra-{i}.jpg", b"\xff\xd8 bytes")
        _crawl(self.root)
        m = _manager(describer=_fake_describer(labels=("zebra",)))
        m.sync(full=True)
        m.describe_pending()
        conn = m.store.reader()
        try:
            groups = wd.parse("zebra invoice").groups
            freq = {g: m._doc_freq(conn, g) for g in groups}
            zebra = next(g for g in groups if "zebra" in g)
            invoice = next(g for g in groups if "invoice" in g)
            self.assertEqual(freq[zebra], 3)
            self.assertGreaterEqual(freq[invoice], 30)
            asked = []
            real = m._fts

            def spy(c, expr, kinds, pool):
                asked.append(expr)
                return real(c, expr, kinds, pool)
            with mock.patch.object(m, "_fts", spy):
                scored = m._candidates(conn, groups, (), need=3)
            self.assertEqual(len(asked), 2)                                # the AND, then the rare concept alone
            self.assertIn("zebra", asked[1])
            self.assertNotIn("invoice", asked[1])
            self.assertEqual(len(scored), 3)
        finally:
            conn.close()
        res = m.search("zebra invoice", limit=2)
        self.assertEqual({r["why"] for r in res["results"]}, {"label"})
        self.assertTrue(res["has_more"])


class SymbolGuardTests(unittest.TestCase):
    """wholedisk imports public names from modules other agents are editing. A rename must fail here, loudly."""

    def test_imported_names_exist(self):
        from mse import extract, ignore
        for name in ("AUDIO_EXT", "CODE_EXT", "IMAGE_EXT", "OPENDOC_EXT", "SHEET_EXT", "SLIDE_EXT", "TEXT_EXT",
                     "TEXTUTIL_EXT", "VIDEO_EXT", "decode_bytes", "html_to_text", "markdown_sections", "read_docx_zip",
                     "read_eml", "read_opendocument", "read_textutil"):
            self.assertTrue(hasattr(extract, name), name)
        for name in ("MARKER_EXTRA", "PROJECT_MARKERS", "content_skipped", "effective_prune"):
            self.assertTrue(hasattr(ignore, name), name)
        self.assertIn("node_modules", ignore.effective_prune())


if __name__ == "__main__":
    unittest.main()
