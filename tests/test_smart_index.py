"""Smart indexing of new folders: pruning before the walk, .gitignore awareness, code by name, huge and binary files
by name, the names-first pass, the migration of existing rows, the event path and the settings."""

import argparse
import io
import os
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import cli, config, extract, indexer, store  # noqa: E402
from mse.ignore import content_skipped, effective_prune  # noqa: E402

MB = 1024 * 1024


def _write(p, text="x\n"):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _coll(root, name=None, **kw):
    return config._merge(config.COLLECTION_DEFAULTS,
                         {"name": name or ("t" + uuid.uuid4().hex[:10]), "path": str(root), **kw})


def _rows(name):
    conn = store.connect(name, readonly=True)
    try:
        return {r["path"]: dict(r) for r in conn.execute("SELECT path, mode, kind, missing_since FROM files")}
    finally:
        conn.close()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="smart-"))
        config.update_settings({"indexing": {"respect_gitignore": True, "index_code": False,
                                             "extra_excludes": [], "prune_dirs": None}})

    def walk(self, root, settings=None, **coll_kw):
        scope = indexer._Scope(_coll(root, **coll_kw), settings=settings)
        return sorted(r for r, _s, _m, _i in scope.walk_entries())


class Pruning(_Base):
    def test_names_that_are_always_pruned_are_never_entered(self):
        root = self.tmp / "w"
        for d in ("node_modules/a", ".next/x", "DerivedData/y", "__pycache__/z", ".venv/q", ".git/objects/o"):
            _write(root / d / "f.txt")
        _write(root / "Podfile")
        _write(root / "Pods" / "Lib" / "f.m")
        _write(root / "keep.md")
        self.assertEqual(self.walk(root), ["Podfile", "keep.md"])

    def test_build_dist_out_target_vendor_are_pruned_only_beside_a_project_marker(self):
        root = self.tmp / "w"
        _write(root / "app" / "package.json", "{}")
        for d in ("build", "dist", "out", "target"):
            _write(root / "app" / d / "bundle.js")
        _write(root / "docs" / "build" / "guide.txt")                    # no marker beside it: kept
        _write(root / "go" / "go.mod", "module x\n")
        _write(root / "go" / "vendor" / "lib.go")
        _write(root / "php" / "composer.json", "{}")
        _write(root / "php" / "vendor" / "x.php")
        _write(root / "plain" / "vendor" / "notes.txt")                  # no marker: kept
        self.assertEqual(self.walk(root), ["app/package.json", "docs/build/guide.txt", "go/go.mod",
                                           "php/composer.json", "plain/vendor/notes.txt"])

    def test_env_is_pruned_only_when_it_holds_pyvenv_cfg(self):
        root = self.tmp / "w"
        _write(root / "tools" / "env" / "pyvenv.cfg", "home = /usr\n")
        _write(root / "tools" / "env" / "lib" / "x.py")
        _write(root / "notes" / "env" / "readme.txt")
        self.assertEqual(self.walk(root), ["notes/env/readme.txt"])

    def test_carthage_needs_a_cartfile(self):
        root = self.tmp / "w"
        _write(root / "history" / "Carthage" / "2019.txt")
        _write(root / "ios" / "Cartfile", "github \"x\"\n")
        _write(root / "ios" / "Carthage" / "Build" / "x.framework")
        self.assertEqual(self.walk(root), ["history/Carthage/2019.txt", "ios/Cartfile"])

    def test_app_bundle_is_one_name_only_entry(self):
        root = self.tmp / "w"
        _write(root / "Tool.app" / "Contents" / "Resources" / "a.txt")
        _write(root / "Tool.app" / "Contents" / "Info.plist")
        _write(root / "doc.txt")
        self.assertEqual(self.walk(root), ["Tool.app", "doc.txt"])

    def test_prune_dirs_setting_adds_and_removes_optional_names_but_never_the_floor(self):
        root = self.tmp / "w"
        _write(root / "x" / "tmp_export" / "a.txt")
        _write(root / "x" / "keep" / "b.txt")
        _write(root / "x" / "dist" / "c.txt")                  # no marker, and dist is not in the list: kept
        _write(root / "node_modules" / "m.js")                 # the floor: always pruned
        self.assertEqual(self.walk(root, settings={"prune_dirs": ["tmp_export", "node_modules"]}),
                         ["x/dist/c.txt", "x/keep/b.txt"])
        self.assertEqual(self.walk(root, settings={"prune_dirs": []}),
                         ["x/dist/c.txt", "x/keep/b.txt", "x/tmp_export/a.txt"])

    def test_effective_prune_keeps_the_floor_whatever_the_setting(self):
        self.assertEqual(effective_prune([])["node_modules"], "always")
        self.assertEqual(effective_prune(["tmp"])["tmp"], "always")
        self.assertNotIn("dist", effective_prune(["tmp"]))


class GitignoreRules(_Base):
    def _repo(self, root):
        (root / ".git" / "info").mkdir(parents=True, exist_ok=True)

    def test_nested_gitignore_with_negation(self):
        root = self.tmp / "r"
        self._repo(root)
        _write(root / ".gitignore", "*.log\n")
        _write(root / "sub" / ".gitignore", "!keep.log\n")
        for f in ("a.log", "sub/keep.log", "sub/other.log", "b.txt"):
            _write(root / f)
        self.assertEqual(self.walk(root), ["b.txt", "sub/keep.log"])   # dot files are never indexed (.*/ floor)

    def test_git_info_exclude_applies_and_a_gitignore_overrides_it(self):
        root = self.tmp / "r"
        self._repo(root)
        _write(root / ".git" / "info" / "exclude", "secret_plan.txt\n*.bak\n")
        _write(root / ".gitignore", "!keep.bak\n")
        for f in ("secret_plan.txt", "x.bak", "keep.bak", "open.txt"):
            _write(root / f)
        self.assertEqual(self.walk(root), ["keep.bak", "open.txt"])

    def test_a_gitignored_path_is_not_walked_whether_or_not_it_has_rows(self):
        # a gitignored path is left out of the walk; rows it already holds are removed by rule (test_smart_index_fixes)
        root = self.tmp / "r"
        self._repo(root)
        _write(root / ".gitignore", "generated/\n*.tmp\n")
        for f in ("generated/old.txt", "generated/new.txt", "x.tmp", "keep.md"):
            _write(root / f)
        self.assertEqual(self.walk(root), ["keep.md"])

    def test_respect_gitignore_false_lists_everything(self):
        root = self.tmp / "r"
        self._repo(root)
        _write(root / ".gitignore", "generated/\n")
        _write(root / "generated" / "new.txt")
        self.assertEqual(self.walk(root, settings={"respect_gitignore": False, "prune_dirs": None,
                                                   "extra_excludes": []}), ["generated/new.txt"])

    def test_a_per_collection_override_beats_the_global_setting(self):
        root = self.tmp / "r"
        self._repo(root)
        _write(root / ".gitignore", "generated/\n")
        _write(root / "generated" / "new.txt")
        self.assertEqual(self.walk(root, respect_gitignore=False), ["generated/new.txt"])


class CodeByName(_Base):
    def test_code_is_its_own_kind_and_lockfiles_are_recognised_by_name(self):
        for ext in ("py", "ts", "css", "map"):
            self.assertEqual(extract.kind_of(ext), "code", ext)
        for ext in ("md", "html", "ipynb", "json", "yaml"):
            self.assertEqual(extract.kind_of(ext), "text", ext)
        self.assertTrue(extract.is_code_name("package-lock.json"))
        self.assertTrue(extract.is_code_name("app.min.js"))
        self.assertFalse(extract.is_code_name("package.json"))

    def test_name_only_by_default_full_when_the_folder_turns_index_code_on(self):
        root = self.tmp / "w"
        _write(root / "app.py", "def f():\n    return 'zebra'\n")
        _write(root / "notes.md", "zebra notes\n")
        name = "t" + uuid.uuid4().hex[:10]
        stats = indexer.scan(_coll(root, name=name))
        rows = _rows(name)
        self.assertEqual((rows["app.py"]["mode"], rows["app.py"]["kind"]), ("meta", "code"))
        self.assertEqual((rows["notes.md"]["mode"], rows["notes.md"]["kind"]), ("full", "text"))
        self.assertEqual(stats["names_first"], 2)
        conn = store.connect(name, readonly=True)
        modalities = [r[0] for r in conn.execute(
            "SELECT i.modality FROM items i JOIN files f ON f.id=i.file_id WHERE f.path='app.py'")]
        conn.close()
        self.assertEqual(modalities, ["title"])
        indexer.scan(_coll(root, name=name, index_code=True))
        rows = _rows(name)
        self.assertEqual((rows["app.py"]["mode"], rows["app.py"]["kind"]), ("full", "code"))

    def test_turning_index_code_off_downgrades_the_row_without_a_reread(self):
        root = self.tmp / "w"
        _write(root / "app.py", "def f():\n    return 'zebra'\n")
        name = "t" + uuid.uuid4().hex[:10]
        indexer.scan(_coll(root, name=name, index_code=True))
        stats = indexer.scan(_coll(root, name=name))
        self.assertEqual(stats["downgraded_code"], 1)
        self.assertEqual(_rows(name)["app.py"]["mode"], "meta")
        conn = store.connect(name, readonly=True)
        n_items = conn.execute("SELECT COUNT(*) FROM items i JOIN files f ON f.id=i.file_id "
                               "WHERE f.path='app.py' AND i.modality != 'title'").fetchone()[0]
        conn.close()
        self.assertEqual(n_items, 0)

    def test_name_only_code_is_never_pending_for_embedding(self):
        root = self.tmp / "w"
        _write(root / "app.py", "def f():\n    return 1\n")
        _write(root / "notes.md", "some notes about zebras\n")
        name = "t" + uuid.uuid4().hex[:10]
        indexer.scan(_coll(root, name=name))
        conn = store.connect(name, readonly=True)
        expected = conn.execute("SELECT COUNT(*) FROM items i JOIN files f ON f.id=i.file_id WHERE i.embedded=0 "
                                "AND i.modality IN ('title','text','ocr','transcript') "
                                "AND NOT (f.kind='code' AND f.mode='meta')").fetchone()[0]
        conn.close()
        self.assertEqual(indexer.pending_count(name, media=False), expected)


class SizeAndBinaries(_Base):
    def test_content_skip_rules(self):
        self.assertTrue(content_skipped("a/b.dmg", 10))
        self.assertTrue(content_skipped("x.iso", 10))
        self.assertTrue(content_skipped("Tool.app", 0))
        self.assertTrue(content_skipped("big.log", 11 * MB))
        self.assertTrue(content_skipped("x.sqlite", 11 * MB))
        self.assertFalse(content_skipped("small.log", 1024))
        self.assertFalse(content_skipped("paper.pdf", 300 * MB))     # the size rule (max_file_mb) is separate

    def test_scan_names_a_disk_image_and_a_huge_log_and_reads_the_rest(self):
        root = self.tmp / "w"
        _write(root / "archive.dmg", "not really a disk image\n")
        with open(root / "big.log", "wb") as f:
            f.truncate(11 * MB)
        _write(root / "small.log", "a line\n")
        _write(root / "readme.md", "hello\n")
        name = "t" + uuid.uuid4().hex[:10]
        indexer.scan(_coll(root, name=name))
        rows = _rows(name)
        self.assertEqual(rows["archive.dmg"]["mode"], "meta")
        self.assertEqual(rows["big.log"]["mode"], "meta")
        self.assertEqual(rows["small.log"]["mode"], "full")
        self.assertEqual(rows["readme.md"]["mode"], "full")


class NamesFirst(_Base):
    def test_every_new_file_has_a_row_before_any_file_is_read(self):
        root = self.tmp / "w"
        for i in range(30):
            _write(root / f"note_{i}.md", f"note {i}\n")
        name = "t" + uuid.uuid4().hex[:10]
        seen = []
        real = indexer._prepare

        def spy(coll, rel, mode, starts=None, reader=None):
            c = store.connect(name, readonly=True)
            seen.append(c.execute("SELECT COUNT(*) FROM files").fetchone()[0])
            c.close()
            return real(coll, rel, mode, starts, reader)

        with mock.patch.object(indexer, "_prepare", spy):
            stats = indexer.scan(_coll(root, name=name))
        self.assertEqual(stats["names_first"], 30)
        self.assertEqual(len(seen), 30)
        self.assertTrue(all(n == 30 for n in seen), seen[:5])

    def test_a_changed_file_is_not_named_again_and_keeps_its_content(self):
        root = self.tmp / "w"
        _write(root / "a.md", "first version\n")
        name = "t" + uuid.uuid4().hex[:10]
        indexer.scan(_coll(root, name=name))
        _write(root / "a.md", "second version, longer than before\n")
        stats = indexer.scan(_coll(root, name=name))
        self.assertEqual(stats["names_first"], 0)
        self.assertEqual(_rows(name)["a.md"]["mode"], "full")


class Migration(_Base):
    def _tree(self, n=60):
        root = self.tmp / "w"
        _write(root / "app" / "package.json", "{}")
        for i in range(n):
            _write(root / "app" / "dist" / f"b{i}.txt", f"bundle {i}\n")
        _write(root / "app" / "real.md", "real\n")
        return root

    def test_rows_a_rule_now_excludes_go_at_once_and_never_count_as_missing(self):
        root = self._tree()
        name = "t" + uuid.uuid4().hex[:10]
        config.update_settings({"indexing": {"prune_dirs": []}})          # the old rules: dist is indexed
        first = indexer.scan(_coll(root, name=name))
        self.assertEqual(first["files_seen"], 62)
        config.update_settings({"indexing": {"prune_dirs": None}})       # the new rules: dist is a project build folder
        second = indexer.scan(_coll(root, name=name))
        self.assertEqual(second["removed_by_rule"], 60)
        self.assertEqual(second["missing_marked"], 0)
        self.assertEqual(second["held_mass_missing"], 0)
        self.assertEqual(second["mass_missing_warning"], "")
        rows = _rows(name)
        self.assertEqual(sorted(rows), ["app/package.json", "app/real.md"])
        self.assertTrue(all(r["missing_since"] is None for r in rows.values()))

    def test_a_user_pattern_that_now_matches_is_removed_by_rule_at_once(self):
        # owner's rule (smart-index fixes): extra_excludes removes the rows it matches; the files stay on disk
        root = self.tmp / "w"
        for i in range(60):
            _write(root / "docs" / f"d{i}.txt", f"doc {i}\n")
        name = "t" + uuid.uuid4().hex[:10]
        indexer.scan(_coll(root, name=name))
        config.update_settings({"indexing": {"extra_excludes": ["*.txt"]}})
        stats = indexer.scan(_coll(root, name=name))
        self.assertEqual(stats["removed_by_rule"], 60)
        self.assertEqual(stats["removed"], 0)
        self.assertEqual(stats["missing_marked"], 0)
        self.assertTrue((root / "docs" / "d0.txt").exists())

    def test_a_folder_name_the_owner_adds_to_prune_dirs_is_removed_by_rule_at_once(self):
        root = self.tmp / "w"
        for i in range(60):
            _write(root / "stash" / f"s{i}.txt", f"stash {i}\n")
        name = "t" + uuid.uuid4().hex[:10]
        indexer.scan(_coll(root, name=name))
        config.update_settings({"indexing": {"prune_dirs": ["stash"]}})
        stats = indexer.scan(_coll(root, name=name))
        self.assertEqual(stats["removed_by_rule"], 60)
        self.assertEqual(stats["missing_marked"], 0)

    def test_rows_under_a_marker_pruned_folder_are_removed_by_rule_even_when_the_file_is_gone(self):
        # the marker (app/package.json) is on disk, so the rule reads the folder above, not the vanished file
        root = self._tree()
        name = "t" + uuid.uuid4().hex[:10]
        config.update_settings({"indexing": {"prune_dirs": []}})
        indexer.scan(_coll(root, name=name))
        config.update_settings({"indexing": {"prune_dirs": None}})
        os.rename(root / "app" / "dist", self.tmp / "outside_dist")          # gone from the collection, not moved in it
        stats = indexer.scan(_coll(root, name=name))
        self.assertEqual(stats["removed_by_rule"], 60)
        self.assertEqual(stats["missing_marked"], 0)
        self.assertEqual(stats["removed"], 0)

    def test_rows_of_files_that_vanished_from_an_unpruned_folder_still_wait_for_the_grace_period(self):
        root = self.tmp / "w"                                # no project marker: the folder is never pruned
        for i in range(60):
            _write(root / "dist" / f"b{i}.txt", f"bundle {i}\n")
        _write(root / "app" / "real.md", "real\n")
        name = "t" + uuid.uuid4().hex[:10]
        indexer.scan(_coll(root, name=name))
        os.rename(root / "dist", self.tmp / "outside_dist")
        stats = indexer.scan(_coll(root, name=name))
        self.assertEqual(stats["removed_by_rule"], 0)
        self.assertEqual(stats["missing_marked"], 60)
        self.assertEqual(stats["removed"], 0)


class EventPaths(_Base):
    def _tree(self):
        root = self.tmp / "w"
        _write(root / "app" / "package.json", "{}")
        _write(root / "app" / "src" / "a.md", "a\n")
        _write(root / "app" / "node_modules" / "x.js", "x\n")
        return root

    def test_an_event_under_a_pruned_build_folder_creates_no_rows(self):
        root = self._tree()
        name = "t" + uuid.uuid4().hex[:10]
        indexer.scan(_coll(root, name=name))
        _write(root / "app" / "dist" / "new.txt", "new\n")
        res = indexer.index_paths(_coll(root, name=name), ["app/dist/new.txt"])
        self.assertEqual(res["changed"], 0)
        self.assertNotIn("app/dist/new.txt", _rows(name))

    def test_an_event_that_drops_existing_rows_under_a_rule_removes_them(self):
        root = self._tree()
        name = "t" + uuid.uuid4().hex[:10]
        config.update_settings({"indexing": {"prune_dirs": []}})
        _write(root / "app" / "dist" / "old.txt", "old\n")
        indexer.scan(_coll(root, name=name))
        config.update_settings({"indexing": {"prune_dirs": None}})
        res = indexer.index_paths(_coll(root, name=name), ["app/dist"])
        self.assertEqual(res["removed_by_rule"], 1)
        self.assertNotIn("app/dist/old.txt", _rows(name))

    def test_a_folder_event_is_walked_with_the_collection_rules(self):
        root = self._tree()
        name = "t" + uuid.uuid4().hex[:10]
        indexer.index_paths(_coll(root, name=name), ["app"])
        rows = _rows(name)
        self.assertIn("app/src/a.md", rows)
        self.assertNotIn("app/node_modules/x.js", rows)

    def test_rows_inside_an_app_bundle_go_at_once(self):
        root = self.tmp / "w"
        _write(root / "Tool.app" / "Contents" / "res.txt", "inside a bundle\n")
        _write(root / "doc.md", "doc\n")
        name = "t" + uuid.uuid4().hex[:10]
        indexer.scan(_coll(root, name=name))
        conn = store.connect(name)                           # an older index (the HEAD walk) held the bundle's files
        conn.execute("INSERT INTO files(path, name, docid, ext, kind, size, mtime, mode, state) "
                     "VALUES ('Tool.app/Contents/res.txt', 'res.txt', 'deadbeef', 'txt', 'text', 10, 1.0, 'full', 'text')")
        conn.commit()
        conn.close()
        stats = indexer.scan(_coll(root, name=name))
        self.assertEqual(stats["removed_by_rule"], 1)
        self.assertEqual(stats["missing_marked"], 0)
        self.assertEqual(sorted(_rows(name)), ["Tool.app", "doc.md"])

    def test_index_one_refuses_a_path_the_rules_exclude(self):
        root = self._tree()
        with self.assertRaises(ValueError):
            indexer.index_one(_coll(root, name="t" + uuid.uuid4().hex[:10]), "app/node_modules/x.js")


class Survey(_Base):
    def test_survey_counts_what_a_first_pass_skips_and_keeps_by_name(self):
        root = self.tmp / "w"
        _write(root / "app" / "package.json", "{}")
        _write(root / "app" / "node_modules" / "x.js")
        _write(root / "app" / "dist" / "a.js")
        _write(root / "app" / "src" / "a.py", "x = 1\n")
        _write(root / "x.dmg", "img\n")
        _write(root / "Tool.app" / "Contents" / "x.txt")
        _write(root / "notes.md", "n\n")
        s = indexer.survey(_coll(root))
        self.assertEqual(s["entries"], 5)
        self.assertEqual(s["name_only"], 3)     # a.py (code), x.dmg and Tool.app (content skipped)
        self.assertEqual(s["code_name_only"], 1)
        self.assertEqual(s["content_name_only"], 2)
        self.assertEqual(s["pruned_folders"], 2)
        self.assertEqual(s["pruned"], {"node_modules": 1, "dist": 1})
        self.assertEqual(s["bundles"], 1)


class Settings(_Base):
    def test_a_valid_block_is_accepted(self):
        out = config.check_indexing({"respect_gitignore": False, "index_code": True,
                                     "extra_excludes": ["*.bak", "Archive"], "prune_dirs": ["tmp_export"],
                                     "workers": 2, "file_timeout_s": 420, "missing_grace_h": 24,
                                     "mass_missing_ratio": 0.3, "mass_missing_min": 50, "xver_per_scan": 25})
        self.assertEqual(out["prune_dirs"], ["tmp_export"])
        self.assertEqual(out["workers"], 2)
        self.assertIsNone(config.check_indexing({"prune_dirs": None})["prune_dirs"])

    def test_invalid_values_are_refused(self):
        bad = [{"respect_gitignore": "yes"}, {"index_code": 1}, {"extra_excludes": "*.bak"},
               {"extra_excludes": ["!keep.txt"]},
               {"prune_dirs": ["a/b"]}, {"prune_dirs": ["*"]}, {"prune_dirs": [".."]}, {"prune_dirs": "build"},
               {"workers": 0}, {"workers": True}, {"file_timeout_s": 1}, {"unknown_key": 1}, {"missing_grace_h": -1}]
        for b in bad:
            with self.subTest(b=b), self.assertRaises(ValueError):
                config.check_indexing(b)

    def test_update_settings_round_trip(self):
        config.update_settings({"indexing": {"index_code": True, "extra_excludes": ["*.bak"]}})
        idx = config.load()["indexing"]
        self.assertTrue(idx["index_code"])
        self.assertEqual(idx["extra_excludes"], ["*.bak"])
        self.assertTrue(idx["respect_gitignore"])            # untouched keys keep their value
        with self.assertRaises(ValueError):
            config.update_settings({"indexing": {"index_code": "yes"}})

    def test_per_collection_override_is_validated_and_null_inherits(self):
        root = self.tmp / "w"
        root.mkdir(parents=True, exist_ok=True)
        name = "t" + uuid.uuid4().hex[:10]
        config.add_collection(str(root), name)
        config.update_collection(name, {"index_code": True, "respect_gitignore": False})
        self.assertTrue(config.get_collection(config.load(), name)["index_code"])
        config.update_collection(name, {"index_code": None})
        self.assertIsNone(config.get_collection(config.load(), name)["index_code"])
        with self.assertRaises(ValueError):
            config.update_collection(name, {"respect_gitignore": "maybe"})

    def test_the_settings_get_block_posts_back_unchanged(self):
        from mse import server
        captured = {}

        class Stub:
            def _json(self, doc):
                captured.update(doc)

        server.Handler.get_api_settings(Stub(), {})
        self.assertIn("indexing_info", captured)
        self.assertNotIn("prune_dirs_effective", captured["indexing"])
        config.update_settings({"indexing": captured["indexing"]})          # must not raise
        self.assertEqual(config.load()["indexing"]["respect_gitignore"], captured["indexing"]["respect_gitignore"])

    def test_an_unset_override_inherits_the_global_value(self):
        self.assertTrue(indexer._flag({"index_code": None}, "index_code", {"index_code": True}, False))
        self.assertFalse(indexer._flag({"index_code": False}, "index_code", {"index_code": True}, False))
        self.assertTrue(indexer._flag({}, "respect_gitignore", {}, True))


class CliAdd(_Base):
    def test_add_prints_the_counts_and_says_when_the_service_is_down(self):
        root = self.tmp / "w"
        _write(root / "app" / "package.json", "{}")
        _write(root / "app" / "node_modules" / "x.js")
        _write(root / "notes.md", "n\n")
        ns = argparse.Namespace(path=str(root), name="t" + uuid.uuid4().hex[:10], description=None,
                                no_global=False, agents=False)
        buf = io.StringIO()
        with mock.patch.object(cli, "daemon_up", return_value=False), redirect_stdout(buf):
            cli.cmd_add(ns)
        out = buf.getvalue()
        self.assertIn("the service is not running", out)
        self.assertIn("folders skipped by rule (node_modules 1)", out)
        self.assertIn("files and folders found", out)


if __name__ == "__main__":
    unittest.main()
