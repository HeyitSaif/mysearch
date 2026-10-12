"""The mass-vanish guard: a cloud folder that lists empty for longer than the grace period must not lose its rows on the
first pass after it. Removal is held while the collection, or one top-level folder, is mostly missing; it resumes by
itself once the count is back under the threshold. Only temporary folders and a temporary data folder are used.
Run: .venv/bin/python -m unittest -v tests.test_missing_guard"""

import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import indexer, progress as liveprog, store  # noqa: E402

DAY = 24 * 3600
GUARD = (0.30, 50)          # the defaults: 30 % of the rows and at least 50 files


def _name() -> str:
    return "t" + uuid.uuid4().hex[:10]


def _rows(conn, specs):
    """specs: (path, missing_age_seconds or None for present)."""
    now = time.time()
    for path, age in specs:
        conn.execute("INSERT INTO files(path, name, kind, size, mtime, mode, state, missing_since)"
                     " VALUES (?,?,?,?,?,?,?,?)",
                     (path, path.rsplit("/", 1)[-1], "text", 1, now, "full", "done",
                      None if age is None else now - age))
    conn.commit()


def _gone(conn, prefix: str = "") -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, path, missing_since FROM files WHERE missing_since IS NOT NULL AND path LIKE ?", (prefix + "%",))]


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]


class MassVanishGuard(unittest.TestCase):
    """_settle_missing with the guard, on rows in a database (no folder, no reader)."""

    def setUp(self):
        self.name = _name()
        self.conn = store.connect(self.name)

    def tearDown(self):
        self.conn.close()

    def settle(self, guard=GUARD):
        return indexer._settle_missing(self.conn, self.name, _gone(self.conn), time.time(), DAY, guard=guard)

    def test_a_mass_vanish_above_the_threshold_removes_nothing(self):
        _rows(self.conn, [(f"live/f{i}.txt", None) for i in range(100)]
              + [(f"cloud/f{i}.txt", 2 * DAY) for i in range(100)])          # 100 of 200 missing: 50 %
        res = self.settle()
        self.assertEqual((res["removed"], res["held"]), (0, 100))
        self.assertEqual(_count(self.conn), 200)
        self.assertEqual(len(_gone(self.conn)), 100, "held rows stay marked, not cleared")
        self.assertIn("over the threshold", res["warning"])

    def test_the_ratio_boundary_is_inclusive_and_below_it_removal_proceeds(self):
        # 59 of 200 = 29.5 %, spread over two folders so that neither one is over the threshold on its own
        _rows(self.conn, [(f"live/f{i}.txt", None) for i in range(141)]
              + [(f"cloud_a/f{i}.txt", 2 * DAY) for i in range(30)]
              + [(f"cloud_b/f{i}.txt", 2 * DAY) for i in range(29)])
        res = self.settle()
        self.assertEqual((res["removed"], res["held"]), (59, 0))
        self.assertEqual(_count(self.conn), 141)

    def test_exactly_the_ratio_is_held(self):
        other = _name()
        conn = store.connect(other)
        _rows(conn, [(f"live/f{i}.txt", None) for i in range(140)]
              + [(f"cloud_a/f{i}.txt", 2 * DAY) for i in range(30)]
              + [(f"cloud_b/f{i}.txt", 2 * DAY) for i in range(30)])          # 60 of 200 = 30 %: held
        res = indexer._settle_missing(conn, other, _gone(conn), time.time(), DAY, guard=GUARD)
        self.assertEqual((res["removed"], res["held"]), (0, 60))
        conn.close()

    def test_a_handful_of_vanished_files_is_always_removed(self):
        _rows(self.conn, [(f"live/f{i}.txt", None) for i in range(20)]
              + [(f"gone/f{i}.txt", 2 * DAY) for i in range(10)])             # 33 % of the rows, but only 10 files
        res = self.settle()
        self.assertEqual((res["removed"], res["held"]), (10, 0))

    def test_a_folder_over_the_threshold_is_held_on_its_own(self):
        # the collection as a whole is under 30 % missing; one top-level folder is 60 % missing
        _rows(self.conn, [(f"big/f{i}.txt", None) for i in range(2900)]
              + [(f"cloud/p{i}.txt", None) for i in range(40)]
              + [(f"cloud/m{i}.txt", 2 * DAY) for i in range(60)]
              + [(f"other/f{i}.txt", 2 * DAY) for i in range(3)])
        res = self.settle()
        self.assertEqual((res["removed"], res["held"]), (3, 60))
        self.assertEqual(len(_gone(self.conn, "cloud/")), 60)
        self.assertIn("cloud", res["warning"])

    def test_removal_resumes_once_the_count_is_back_under_the_threshold(self):
        _rows(self.conn, [(f"live/f{i}.txt", None) for i in range(100)]
              + [(f"cloud/f{i}.txt", 2 * DAY) for i in range(100)])
        self.assertEqual(self.settle()["held"], 100)
        # 60 of the cloud files come back: the rows are cleared, 40 remain missing, below the 50-file minimum
        self.conn.execute("UPDATE files SET missing_since=NULL WHERE path IN ({})".format(
            ",".join("?" * 60)), [f"cloud/f{i}.txt" for i in range(60)])
        self.conn.commit()
        res = self.settle()
        self.assertEqual((res["removed"], res["held"]), (40, 0))
        self.assertEqual(_count(self.conn), 160)

    def test_the_guard_can_be_turned_off(self):
        _rows(self.conn, [(f"live/f{i}.txt", None) for i in range(100)]
              + [(f"cloud/f{i}.txt", 2 * DAY) for i in range(100)])
        res = self.settle(guard=(1.01, 50))                                  # a ratio above 1 never holds
        self.assertEqual((res["removed"], res["held"]), (100, 0))

    def test_the_thresholds_come_from_the_indexing_settings(self):
        with mock.patch.object(indexer, "_settings", lambda: {"mass_missing_ratio": 0.9, "mass_missing_min": 5}):
            self.assertEqual(indexer._mass_guard(indexer._settings()), (0.9, 5))
        self.assertEqual(indexer._mass_guard({"mass_missing_ratio": "junk"}), (indexer.MASS_MISSING_RATIO,
                                                                              indexer.MASS_MISSING_MIN))

    def test_the_guard_counts_only_when_a_removal_is_due(self):
        _rows(self.conn, [(f"live/f{i}.txt", None) for i in range(100)]
              + [(f"cloud/f{i}.txt", None) for i in range(100)])
        with mock.patch.object(indexer, "_held_by_mass_guard", side_effect=AssertionError("must not run")):
            res = indexer._settle_missing(self.conn, self.name, [dict(r) for r in self.conn.execute(
                "SELECT id, path, missing_since FROM files WHERE path LIKE 'cloud/%'")], time.time(), DAY, guard=GUARD)
        self.assertEqual((res["marked"], res["removed"], res["held"]), (100, 0, 0))


class MassVanishThroughAPasses(unittest.TestCase):
    """The same guard reached through a full scan and through a path job, on real folders."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="mse-guard-"))
        self.away = Path(tempfile.mkdtemp(prefix="mse-guard-away-"))
        self.coll = {"name": _name(), "path": str(self.folder), "media": {}, "exclude": [], "metadata_only": []}
        for i in range(100):
            (self.folder / f"note{i:03d}.txt").write_text(f"alpha beta {i}")
        self.first = indexer.scan(self.coll)
        self.assertEqual(self.first["files_seen"], 100)

    def _age_marks(self):
        conn = store.connect(self.coll["name"])
        conn.execute("UPDATE files SET missing_since = missing_since - ? WHERE missing_since IS NOT NULL", (2 * DAY,))
        conn.commit()
        conn.close()

    def _rows(self) -> int:
        conn = store.connect(self.coll["name"], readonly=True)
        try:
            return _count(conn)
        finally:
            conn.close()

    def _move(self, names, back: bool) -> None:
        for n in names:
            src, dst = (self.away / n, self.folder / n) if back else (self.folder / n, self.away / n)
            os.rename(src, dst)

    def test_a_folder_offline_past_the_grace_period_is_held_by_a_scan(self):
        names = sorted(p.name for p in self.folder.iterdir())
        self._move(names, back=False)                       # the folder lists empty
        self.assertEqual(indexer.scan(self.coll)["missing_marked"], 100)
        self._age_marks()                                    # more than the grace period later, still empty
        held = indexer.scan(self.coll)
        self.assertEqual((held["removed"], held["held_mass_missing"]), (0, 100))
        self.assertEqual(self._rows(), 100)
        self.assertIn("over the threshold", held["mass_missing_warning"])
        # the warning reaches mse status: a held removal is an error in the progress registry
        snap = liveprog.snapshot([self.coll["name"]])["folders"][self.coll["name"]]
        self.assertGreaterEqual(snap["errors"]["count"], 1)
        self.assertTrue(any("mass-vanish" in m for m in snap["errors"]["recent"]))

    def test_files_coming_back_resume_removal_of_the_rest(self):
        names = sorted(p.name for p in self.folder.iterdir())
        self._move(names, back=False)
        indexer.scan(self.coll)
        self._age_marks()
        self.assertEqual(indexer.scan(self.coll)["held_mass_missing"], 100)
        self._move(names[:70], back=True)                    # 70 come back: 30 remain missing, below the minimum
        res = indexer.scan(self.coll)
        self.assertEqual((res["removed"], res["held_mass_missing"]), (30, 0))
        self.assertEqual(self._rows(), 70)

    def test_a_path_job_is_held_the_same_way(self):
        names = sorted(p.name for p in self.folder.iterdir())
        self._move(names, back=False)
        indexer.index_paths(self.coll, names)                # the watcher's path job for the vanished files
        self._age_marks()
        res = indexer.index_paths(self.coll, names)
        self.assertEqual((res["removed"], res["held_mass_missing"]), (0, 100))
        self.assertEqual(self._rows(), 100)


if __name__ == "__main__":
    unittest.main()
