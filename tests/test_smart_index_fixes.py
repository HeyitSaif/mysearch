"""Smart indexing fixes: nested git repositories pruned (and their rows removed by rule), rows a rule excludes removed at
once while genuinely vanished rows keep the grace period and the mass-vanish guard, binaries and fonts name-only, and one
predicate for name-only files that are never embedded, shared by embed() and every pending count."""

import os
import sqlite3
import tempfile
import time
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

import numpy as np  # noqa: E402

from mse import config, extract, gitignore, indexer, store  # noqa: E402
from mse import progress as liveprog  # noqa: E402
from mse.ignore import BINARY_EXT, content_skipped  # noqa: E402

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
        return {r["path"]: dict(r) for r in conn.execute("SELECT path, mode, kind, ext, missing_since FROM files")}
    finally:
        conn.close()


def _backdate(name, hours):
    """Age every missing mark by `hours`, so the grace period has passed (the clock is not changed)."""
    conn = store.connect(name)
    conn.execute("UPDATE files SET missing_since=? WHERE missing_since IS NOT NULL", (time.time() - hours * 3600,))
    conn.commit()
    conn.close()


def _repo_root(root):
    """A collection root that is itself a git repository (a .git folder)."""
    (root / ".git" / "info").mkdir(parents=True, exist_ok=True)


def _linked_worktree(folder, gitdir):
    """A linked git worktree: a .git FILE naming the repository's worktree folder."""
    _write(Path(folder) / ".git", f"gitdir: {gitdir}\n")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="smart-fix-"))
        config.update_settings({"indexing": {"respect_gitignore": True, "index_code": False, "extra_excludes": [],
                                             "prune_dirs": None, "skip_nested_repos": True}})

    def walk(self, root, settings=None, **coll_kw):
        scope = indexer._Scope(_coll(root, **coll_kw), settings=settings)
        return sorted(r for r, _s, _m, _i in scope.walk_entries()), scope


# ---------------------------------------------------------------- 1. nested repositories

class NestedRepos(_Base):
    def test_worktrees_and_clones_inside_a_repository_root_are_pruned(self):
        root = self.tmp / "r"
        _repo_root(root)
        _linked_worktree(root / "worktrees" / "a", str(root / ".git" / "worktrees" / "a"))
        _write(root / "worktrees" / "a" / "src" / "app.md")
        _write(root / "vendor" / "other-repo" / ".git" / "HEAD", "ref: refs/heads/main\n")
        _write(root / "vendor" / "other-repo" / "README.md")
        _write(root / "keep.md")
        files, scope = self.walk(root)
        self.assertEqual(files, ["keep.md"])
        self.assertEqual(dict(scope.pruned_nested), {"worktree": 1, "clone": 1})

    def test_a_folder_of_separate_clones_is_kept_when_the_root_is_not_a_repository(self):
        root = self.tmp / "r"                                    # no .git at the root: these are separate projects
        for proj in ("frontend", "backend"):
            _write(root / proj / ".git" / "HEAD", "ref: refs/heads/main\n")
            _write(root / proj / "src" / "main.md")
        files, scope = self.walk(root)
        self.assertEqual(files, ["backend/src/main.md", "frontend/src/main.md"])
        self.assertEqual(dict(scope.pruned_nested), {})

    def test_a_linked_worktree_of_a_repository_inside_a_plain_root_is_pruned_and_an_outside_one_is_kept(self):
        root = self.tmp / "r"
        _write(root / "frontend" / ".git" / "HEAD", "ref: refs/heads/main\n")          # the clone, inside
        _linked_worktree(root / "worktrees" / "inside", str(root / "frontend" / ".git" / "worktrees" / "inside"))
        _write(root / "worktrees" / "inside" / "page.md")
        outside = self.tmp / "elsewhere" / ".git" / "worktrees" / "out"             # its repository is not in here
        _linked_worktree(root / "worktrees" / "outside", str(outside))
        _write(root / "worktrees" / "outside" / "page.md")
        files, _scope = self.walk(root)
        self.assertEqual(files, ["worktrees/outside/page.md"])

    def test_a_submodule_style_git_file_is_not_a_linked_worktree(self):
        root = self.tmp / "r"
        _repo_root(root)
        _write(root / "sub" / ".git", "gitdir: ../.git/modules/sub\n")       # a submodule, not a worktree
        _write(root / "sub" / "doc.md")
        files, _scope = self.walk(root)
        self.assertEqual(files, ["sub/doc.md"])

    def test_the_setting_and_the_collection_override_turn_it_off(self):
        root = self.tmp / "r"
        _repo_root(root)
        _write(root / "vendor" / "other-repo" / ".git" / "HEAD", "x\n")
        _write(root / "vendor" / "other-repo" / "README.md")
        files, _ = self.walk(root, settings={"skip_nested_repos": False, "prune_dirs": None, "extra_excludes": []})
        self.assertEqual(files, ["vendor/other-repo/README.md"])
        files, _ = self.walk(root, settings={"skip_nested_repos": True, "prune_dirs": None, "extra_excludes": []},
                             skip_nested_repos=False)
        self.assertEqual(files, ["vendor/other-repo/README.md"])
        files, _ = self.walk(root, settings={"skip_nested_repos": False, "prune_dirs": None, "extra_excludes": []},
                             skip_nested_repos=None)
        self.assertEqual(files, ["vendor/other-repo/README.md"])
        files, _ = self.walk(root, settings={"skip_nested_repos": True, "prune_dirs": None, "extra_excludes": []},
                             skip_nested_repos=None)
        self.assertEqual(files, [])

    def test_the_collection_root_itself_is_never_pruned(self):
        root = self.tmp / "r"
        _linked_worktree(root, str(self.tmp / "main" / ".git" / "worktrees" / "r"))      # the root is a worktree itself
        _write(root / "doc.md")
        files, _ = self.walk(root)
        self.assertEqual(files, ["doc.md"])

    def test_rows_under_a_nested_repo_are_removed_by_rule_not_marked_missing(self):
        root = self.tmp / "r"
        _repo_root(root)
        _linked_worktree(root / "worktrees" / "a", str(root / ".git" / "worktrees" / "a"))
        for i in range(5):
            _write(root / "worktrees" / "a" / f"f{i}.md", f"n{i}\n")
        _write(root / "keep.md", "keep\n")
        name = "t" + uuid.uuid4().hex[:10]
        config.update_settings({"indexing": {"skip_nested_repos": False}})          # rows get created first
        first = indexer.scan(_coll(root, name=name))
        self.assertEqual(first["files_seen"], 6)              # the five files and keep.md; the hidden .git file is excluded
        config.update_settings({"indexing": {"skip_nested_repos": True}})
        second = indexer.scan(_coll(root, name=name))
        self.assertEqual(second["removed_nested_repo"], 5)
        self.assertEqual(second["removed_by_rule"], 5)
        self.assertEqual(second["missing_marked"], 0)
        self.assertEqual(second["held_mass_missing"], 0)
        self.assertEqual(sorted(_rows(name)), ["keep.md"])
        self.assertTrue((root / "worktrees" / "a" / "f0.md").exists())         # the files on disk are never touched

    def test_nested_rows_whose_repository_folder_is_gone_wait_for_the_grace_period(self):
        # the rule reads the .git entry on disk; a worktree folder that is gone cannot be read, so its rows are missing
        root = self.tmp / "r"
        _repo_root(root)
        _linked_worktree(root / "worktrees" / "a", str(root / ".git" / "worktrees" / "a"))
        for i in range(4):
            _write(root / "worktrees" / "a" / f"f{i}.md", f"n{i}\n")
        name = "t" + uuid.uuid4().hex[:10]
        config.update_settings({"indexing": {"skip_nested_repos": False}})
        indexer.scan(_coll(root, name=name))
        config.update_settings({"indexing": {"skip_nested_repos": True}})
        os.rename(root / "worktrees" / "a", self.tmp / "moved_a")              # gone from the collection
        stats = indexer.scan(_coll(root, name=name))
        self.assertEqual(stats["removed_nested_repo"], 0)
        self.assertEqual(stats["missing_marked"], 4)

    def test_event_paths_under_a_nested_repo_create_no_rows(self):
        root = self.tmp / "r"
        _repo_root(root)
        _linked_worktree(root / "worktrees" / "a", str(root / ".git" / "worktrees" / "a"))
        _write(root / "worktrees" / "a" / "new.md")
        _write(root / "keep.md")
        name = "t" + uuid.uuid4().hex[:10]
        res = indexer.index_paths(_coll(root, name=name), ["worktrees/a/new.md", "worktrees/a"])
        self.assertEqual(res["changed"], 0)
        self.assertEqual(_rows(name), {})

    def test_the_setting_default_is_on(self):
        self.assertTrue(config.DEFAULTS["indexing"]["skip_nested_repos"])
        self.assertIsNone(config.COLLECTION_DEFAULTS["skip_nested_repos"])


# ---------------------------------------------------------------- 2. rule removal versus the missing path

class RuleVersusMissing(_Base):
    def test_already_missing_rows_under_an_excluded_path_are_cleared_on_the_next_scan(self):
        root = self.tmp / "r"
        name = "t" + uuid.uuid4().hex[:10]
        for i in range(30):
            _write(root / "worktrees" / f"w{i % 3}" / f"f{i}.md", f"{i}\n")
        _write(root / "keep.md")
        indexer.scan(_coll(root, name=name))
        os.rename(root / "worktrees", self.tmp / "gone_worktrees")              # the folder is deleted
        first = indexer.scan(_coll(root, name=name))
        self.assertEqual(first["missing_marked"], 30)
        self.assertEqual(sum(1 for r in _rows(name).values() if r["missing_since"]), 30)
        config.update_settings({"indexing": {"extra_excludes": ["worktrees/"]}})  # the owner's exclude entry
        second = indexer.scan(_coll(root, name=name))
        self.assertEqual(second["removed_by_rule"], 30)
        self.assertEqual(second["missing_marked"], 0)
        self.assertEqual(second["held_mass_missing"], 0)
        self.assertEqual(sorted(_rows(name)), ["keep.md"])

    def test_gitignored_rows_are_removed_by_rule_while_the_file_stays_on_disk(self):
        root = self.tmp / "r"
        _repo_root(root)
        name = "t" + uuid.uuid4().hex[:10]
        _write(root / "x.log", "log\n")
        _write(root / "keep.md", "keep\n")
        config.update_settings({"indexing": {"respect_gitignore": False}})
        indexer.scan(_coll(root, name=name))
        self.assertIn("x.log", _rows(name))
        _write(root / ".gitignore", "*.log\n")
        config.update_settings({"indexing": {"respect_gitignore": True}})
        stats = indexer.scan(_coll(root, name=name))
        self.assertEqual(stats["removed_gitignored"], 1)
        self.assertEqual(stats["removed_by_rule"], 1)
        self.assertEqual(sorted(_rows(name)), ["keep.md"])
        self.assertTrue((root / "x.log").exists())

    def test_a_gitignored_file_that_is_gone_still_waits_for_the_grace_period(self):
        root = self.tmp / "r"
        _repo_root(root)
        name = "t" + uuid.uuid4().hex[:10]
        _write(root / "x.log", "log\n")
        _write(root / "keep.md", "keep\n")
        config.update_settings({"indexing": {"respect_gitignore": False}})
        indexer.scan(_coll(root, name=name))
        _write(root / ".gitignore", "*.log\n")
        os.rename(root / "x.log", self.tmp / "x_moved.log")                     # gone: the rule cannot be read on it
        config.update_settings({"indexing": {"respect_gitignore": True}})
        stats = indexer.scan(_coll(root, name=name))
        self.assertEqual(stats["removed_gitignored"], 0)
        self.assertEqual(stats["missing_marked"], 1)

    def test_genuinely_vanished_files_go_through_grace_and_then_the_mass_vanish_guard(self):
        root = self.tmp / "r"
        name = "t" + uuid.uuid4().hex[:10]
        for i in range(60):
            _write(root / "docs" / f"d{i}.md", f"doc {i}\n")
        indexer.scan(_coll(root, name=name))
        os.rename(root / "docs", self.tmp / "vanished_docs")                    # all 60 vanish from the collection
        first = indexer.scan(_coll(root, name=name))
        self.assertEqual((first["missing_marked"], first["removed"], first["removed_by_rule"]), (60, 0, 0))
        _backdate(name, 25)                                                    # the grace period has passed
        second = indexer.scan(_coll(root, name=name))
        self.assertEqual(second["removed"], 0)                                 # the guard holds all of them
        self.assertEqual(second["held_mass_missing"], 60)
        self.assertIn("missing", second["mass_missing_warning"])
        self.assertEqual(len(_rows(name)), 60)

    def test_a_small_vanish_below_the_threshold_is_removed_after_the_grace_period(self):
        root = self.tmp / "r"
        name = "t" + uuid.uuid4().hex[:10]
        for i in range(60):
            _write(root / "docs" / f"d{i}.md", f"doc {i}\n")
        indexer.scan(_coll(root, name=name))
        for i in range(3):
            os.rename(root / "docs" / f"d{i}.md", self.tmp / f"moved_{i}.md")
        indexer.scan(_coll(root, name=name))
        _backdate(name, 25)
        stats = indexer.scan(_coll(root, name=name))
        self.assertEqual(stats["removed"], 3)
        self.assertEqual(stats["held_mass_missing"], 0)
        self.assertEqual(len(_rows(name)), 57)

    def test_removals_are_batched_and_counted_by_reason(self):
        root = self.tmp / "r"
        _repo_root(root)
        name = "t" + uuid.uuid4().hex[:10]
        _linked_worktree(root / "worktrees" / "a", str(root / ".git" / "worktrees" / "a"))
        for i in range(5):
            _write(root / "worktrees" / "a" / f"f{i}.md")
        for i in range(2):
            _write(root / "gen" / f"g{i}.draft")                # *.tmp is excluded by default, so use another name
        _write(root / ".gitignore", "*.draft\n")
        _write(root / "keep.md")
        config.update_settings({"indexing": {"skip_nested_repos": False, "respect_gitignore": False}})
        indexer.scan(_coll(root, name=name))
        config.update_settings({"indexing": {"skip_nested_repos": True, "respect_gitignore": True}})
        with mock.patch.object(indexer, "RULE_BATCH", 3):
            stats = indexer.scan(_coll(root, name=name))
        self.assertEqual(stats["removed_nested_repo"], 5)
        self.assertEqual(stats["removed_gitignored"], 2)
        self.assertEqual(stats["removed_by_rule"], 7)
        self.assertEqual(sorted(_rows(name)), ["keep.md"])


# ---------------------------------------------------------------- 3. binaries, fonts and library junk

class BinariesByName(_Base):
    def test_every_binary_type_is_name_only_and_is_never_read(self):
        root = self.tmp / "w"
        names = ["lib/libfoo.a", "lib/foo.obj", "lib/foo.lib", "bin/tool.exe", "bin/lib.dll",
                 "app/dist/x.jar", "app/web.war", "app/mod.wasm", "app/addon.node", "app/plugin.bundle",
                 "app/App.ipa", "app/App.apk", "app/App.aab", "app/game.pak", "app/data.bin",
                 "fonts/a.ttf", "fonts/b.otf", "fonts/c.ttc", "fonts/d.woff", "fonts/e.woff2", "fonts/f.eot",
                 "app/LIB.DLL"]
        for n in names:
            _write(root / n, "zebra\n")
        for n in ("app/App.apk", "app/App.ipa", "app/x.jar", "app/mod_archive.jar"):
            with zipfile.ZipFile(root / n, "w") as z:
                z.writestr("inside.txt", "zebra inside a zip\n")
        _write(root / "notes.md", "plain notes\n")
        name = "t" + uuid.uuid4().hex[:10]
        seen = []
        real = extract.light

        def spy(p, kind, ext, media):
            seen.append(Path(p).name)
            return real(p, kind, ext, media)

        with mock.patch.object(extract, "light", side_effect=spy):
            indexer.scan(_coll(root, name=name))
        rows = _rows(name)
        for n in names:
            with self.subTest(file=n):
                self.assertEqual(rows[n]["mode"], "meta")
                self.assertNotIn(Path(n).name, seen)
        self.assertEqual(rows["notes.md"]["mode"], "full")
        conn = store.connect(name, readonly=True)
        bodies = conn.execute("SELECT f.path, i.modality FROM items i JOIN files f ON f.id=i.file_id "
                              "WHERE i.modality != 'title'").fetchall()
        conn.close()
        self.assertEqual([r[0] for r in bodies], ["notes.md"])

    def test_compiled_objects_excluded_by_default_leave_no_row_at_all(self):
        root = self.tmp / "w"
        for n in ("lib/a.o", "lib/b.so", "lib/c.dylib", "app/M.class", "app/m.pyc", "app/m.pyo"):
            _write(root / n, "zebra\n")
        _write(root / "keep.md", "keep\n")
        name = "t" + uuid.uuid4().hex[:10]
        indexer.scan(_coll(root, name=name))
        self.assertEqual(sorted(_rows(name)), ["keep.md"])

    def test_frameworks_and_bundles_are_one_name_only_entry_each(self):
        root = self.tmp / "w"
        _write(root / "Kit.framework" / "Versions" / "A" / "Kit", "zebra\n")
        _write(root / "Kit.framework" / "Headers" / "k.h", "zebra\n")
        _write(root / "Kit.xcframework" / "ios" / "Info.plist", "zebra\n")
        _write(root / "Build.xcarchive" / "Info.plist", "zebra\n")
        _write(root / "Plugin.bundle" / "Contents" / "MacOS" / "x", "zebra\n")
        _write(root / "notes.md", "n\n")
        name = "t" + uuid.uuid4().hex[:10]
        indexer.scan(_coll(root, name=name))
        rows = _rows(name)
        self.assertEqual(sorted(rows), ["Build.xcarchive", "Kit.framework", "Kit.xcframework", "Plugin.bundle", "notes.md"])
        for n in ("Build.xcarchive", "Kit.framework", "Kit.xcframework", "Plugin.bundle"):
            self.assertEqual(rows[n]["mode"], "meta", n)

    def test_library_caches_are_pruned_and_the_rest_of_library_is_kept(self):
        root = self.tmp / "w"
        _write(root / "Library" / "Caches" / "com.app" / "cache.db", "zebra\n")
        _write(root / "Library" / "Caches" / "deep" / "x.md", "zebra\n")
        _write(root / "Library" / "notes.md", "keep\n")
        _write(root / "Caches" / "top.md", "keep: not under Library\n")
        files, scope = self.walk(root)
        self.assertEqual(files, ["Caches/top.md", "Library/notes.md"])
        self.assertEqual(scope.pruned["Caches"], 1)

    def test_a_dat_file_is_name_only_above_one_megabyte_and_read_below_it(self):
        root = self.tmp / "w"
        _write(root / "small.dat", "zebra small\n")
        with open(root / "big.dat", "wb") as f:
            f.truncate(2 * MB)
        name = "t" + uuid.uuid4().hex[:10]
        indexer.scan(_coll(root, name=name))
        rows = _rows(name)
        self.assertEqual(rows["small.dat"]["mode"], "full")
        self.assertEqual(rows["big.dat"]["mode"], "meta")

    def test_the_content_skip_rule_covers_every_binary_type(self):
        for ext in sorted(BINARY_EXT):
            with self.subTest(ext=ext):
                self.assertTrue(content_skipped(f"a/b.{ext}", 10), ext)
        self.assertTrue(content_skipped("Kit.framework", 0))
        self.assertTrue(content_skipped("Kit.xcframework", 0))
        self.assertFalse(content_skipped("a/b.dat", 10))
        self.assertTrue(content_skipped("a/b.dat", 2 * MB))
        self.assertFalse(content_skipped("notes.md", 10))

    def test_the_ancestor_bundle_rule_removes_old_rows_inside_a_framework(self):
        root = self.tmp / "w"
        _write(root / "Kit.framework" / "Headers" / "k.h", "zebra\n")
        _write(root / "notes.md", "n\n")
        name = "t" + uuid.uuid4().hex[:10]
        indexer.scan(_coll(root, name=name))
        conn = store.connect(name)
        conn.execute("INSERT INTO files(path, name, docid, ext, kind, size, mtime, mode, state) "
                     "VALUES ('Kit.framework/Headers/k.h', 'k.h', 'deadbeef', 'h', 'other', 10, 1.0, 'full', 'text')")
        conn.commit()
        conn.close()
        stats = indexer.scan(_coll(root, name=name))
        self.assertEqual(stats["removed_by_rule"], 1)
        self.assertEqual(stats["missing_marked"], 0)
        self.assertEqual(sorted(_rows(name)), ["Kit.framework", "notes.md"])


# ---------------------------------------------------------------- 4. name-only files are never embedded, and counted alike

class _FakeWorker:
    batch_priority = False


class _FakePool:
    """Stands in for the embedding worker: records each text it is asked to embed, returns unit vectors."""

    def __init__(self):
        self.workers = {"text": _FakeWorker(), "full": _FakeWorker()}
        self.texts: list[str] = []

    def embed_texts(self, texts, batch=None):
        self.texts += list(texts)
        v = np.zeros((len(texts), 768), dtype=np.float32)
        v[:, 0] = 1.0
        return v

    def embed_media(self, items):
        return np.zeros((0, 768), dtype=np.float32), []


class EmbedCounts(_Base):
    def _setup(self):
        root = self.tmp / "w"
        _write(root / "app.py", "def zebra_function():\n    return 1\n")
        _write(root / "fonts" / "Glyph.ttf", "zebra font bytes\n")
        _write(root / "data.bin", "zebra binary\n")
        _write(root / "notes.md", "some notes about zebras\n")
        _write(root / "guide.txt", "a plain text guide\n")
        name = "t" + uuid.uuid4().hex[:10]
        indexer.scan(_coll(root, name=name))
        return root, name

    def test_name_only_code_and_binaries_are_never_embedded_and_all_counts_agree(self):
        root, name = self._setup()
        coll = _coll(root, name=name)
        conn = store.connect(name, readonly=True)
        expected = conn.execute(
            "SELECT COUNT(*) FROM items i JOIN files f ON f.id=i.file_id WHERE i.embedded=0 "
            "AND i.modality IN ('title','text','ocr','transcript') "
            "AND NOT (f.mode='meta' AND (f.kind='code' OR f.ext IN ('bin','ttf','dat')))").fetchone()[0]
        conn.close()
        self.assertEqual(expected, 4)                       # notes.md and guide.txt (title and text), not code or binaries
        self.assertEqual(indexer.pending_count(name, media=False), expected)
        conn = store.connect(name, readonly=True)
        self.assertEqual(store.stats(conn)["embed_pending"], expected)
        conn.close()
        liveprog.REGISTRY._counts.clear()                  # the counts are cached for a few seconds
        self.assertEqual(liveprog.REGISTRY.snapshot([name])["folders"][name]["pending"]["embed"], expected)

        fake = _FakePool()
        with mock.patch.object(indexer, "pool", lambda: fake):
            res = indexer.embed(coll)
        self.assertEqual(res["embedded"], expected)
        self.assertTrue(fake.texts)
        for text in fake.texts:
            self.assertNotIn("zebra_function", text)     # the code body is never sent
            self.assertNotIn("zebra font", text)
            self.assertNotIn("zebra binary", text)
        conn = store.connect(name, readonly=True)
        still = conn.execute(
            "SELECT f.path FROM items i JOIN files f ON f.id=i.file_id WHERE i.embedded=0 "
            "AND f.path IN ('app.py','fonts/Glyph.ttf','data.bin')").fetchall()
        conn.close()
        self.assertEqual(len(still), 3)                    # nothing for these three gets a vector
        self.assertEqual(indexer.pending_count(name, media=False), 0)
        conn = store.connect(name, readonly=True)
        self.assertEqual(store.stats(conn)["embed_pending"], 0)
        conn.close()
        liveprog.REGISTRY._counts.clear()
        self.assertEqual(liveprog.REGISTRY.snapshot([name])["folders"][name]["pending"]["embed"], 0)

    def test_the_embed_skip_is_the_same_whether_or_not_the_ext_column_exists(self):
        raw = sqlite3.connect(":memory:")
        raw.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, mode TEXT, kind TEXT);"
                          "CREATE TABLE items(id INTEGER PRIMARY KEY, file_id INTEGER, embedded INTEGER);"
                          "INSERT INTO files VALUES (1,'meta','code'), (2,'full','text');"
                          "INSERT INTO items VALUES (1,1,0), (2,2,0);")
        sql = store.embed_skip_items(raw)
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM items WHERE embedded=0" + sql).fetchone()[0], 1)


# ---------------------------------------------------------------- 5. settings

class Settings(_Base):
    def test_the_setting_is_a_boolean(self):
        self.assertTrue(config.check_indexing({"skip_nested_repos": False})["skip_nested_repos"] is False)
        with self.assertRaises(ValueError):
            config.check_indexing({"skip_nested_repos": "yes"})

    def test_the_per_collection_override_is_validated_and_null_inherits(self):
        root = self.tmp / "w"
        root.mkdir(parents=True, exist_ok=True)
        name = "t" + uuid.uuid4().hex[:10]
        config.add_collection(str(root), name)
        config.update_collection(name, {"skip_nested_repos": False})
        self.assertIs(config.get_collection(config.load(), name)["skip_nested_repos"], False)
        config.update_collection(name, {"skip_nested_repos": None})
        self.assertIsNone(config.get_collection(config.load(), name)["skip_nested_repos"])
        with self.assertRaises(ValueError):
            config.update_collection(name, {"skip_nested_repos": "maybe"})


if __name__ == "__main__":
    unittest.main()
