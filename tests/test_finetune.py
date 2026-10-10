"""The re-ranker fine-tune (mse/finetune.py): collected data format, gate, promotion rule, query-grouped split,
registry and rollback. No model is loaded and no service is called: searches and favourites are mocked.
Run: .venv/bin/python -m unittest discover -s tests
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import config, feedback, finetune  # noqa: E402


def cand(path, rank, copies=()):
    return {"path": path, "copies": list(copies), "docid": None, "rank": rank, "text": f"{path} (passage)"}


CANDS = [cand("a.md", 1), cand("b.md", 2), cand("fav.md", 3, copies=["fav.digest.md"]), cand("c.md", 4),
         cand("d.md", 5), cand("other_fav.md", 6), cand("e.md", 7)]


class Groups(unittest.TestCase):
    def test_positive_and_hard_negatives(self):
        g = finetune.make_group("q", CANDS, "fav.md", ["fav.md"], k=7)
        self.assertEqual(g["pos"], {"path": "fav.md", "rank": 3, "text": "fav.md (passage)"})
        # everything above the positive first (best first), then the nearest below
        self.assertEqual([n["rank"] for n in g["negs"]], [1, 2, 4, 5, 6, 7])
        for n in g["negs"]:
            self.assertEqual(set(n), {"path", "rank", "text", "hidden"})

    def test_hidden_first_other_favourites_never_negative_and_k(self):
        g = finetune.make_group("q", CANDS, "fav.md", ["fav.md", "other_fav.md"], hidden={"d.md"}, k=3)
        self.assertEqual([n["path"] for n in g["negs"]], ["d.md", "a.md", "b.md"])
        self.assertTrue(g["negs"][0]["hidden"])
        g = finetune.make_group("q", CANDS, "fav.md", ["fav.md", "other_fav.md"], k=10)
        self.assertNotIn("other_fav.md", [n["path"] for n in g["negs"]])

    def test_positive_found_through_a_folded_version(self):
        g = finetune.make_group("q", CANDS, "FAV.digest.md", ["FAV.digest.md"])
        self.assertEqual(g["pos"]["path"], "fav.md")

    def test_not_retrieved_or_no_rival(self):
        self.assertIsNone(finetune.make_group("q", CANDS, "missing.md", ["missing.md"]))
        self.assertIsNone(finetune.make_group("q", [cand("fav.md", 1)], "fav.md", ["fav.md"]))


class Gate(unittest.TestCase):
    def test_gate(self):
        self.assertEqual(finetune.gate(42, 30), (False, "skipped: 42/300"))
        self.assertEqual(finetune.gate(299, 150), (False, "skipped: 299/300"))
        self.assertEqual(finetune.gate(300, 99), (False, "skipped: 99/100 queries"))
        ok, msg = finetune.gate(300, 100)
        self.assertTrue(ok)
        self.assertEqual(finetune.gate(5, 2, min_examples=5, min_queries=2)[0], True)

    def test_run_skips_below_the_gate_and_without_new_favourites(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(finetune, "counts", return_value={"favourites": 42, "queries": 30}):
            r = finetune.run(dict(finetune.DEFAULTS), root=Path(root))
            self.assertEqual((r["action"], r["reason"]), ("skipped", "skipped: 42/300"))
            self.assertEqual(finetune.history(Path(root))[-1]["reason"], "skipped: 42/300")
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(finetune, "counts", return_value={"favourites": 310, "queries": 120}):
            finetune.log_event({"kind": "train", "id": "x", "favourites": 300}, Path(root))
            r = finetune.run(dict(finetune.DEFAULTS), root=Path(root))
            self.assertEqual(r["action"], "skipped")
            self.assertIn("10 new favourites", r["reason"])


class PromotionRule(unittest.TestCase):
    def sets(self, fav, alpha, beta, n_fav=40):
        return {"favourites": {"kind": "favourites", "n": n_fav, "mrr": fav},
                "gold_a": {"kind": "gold", "n": 28, "mrr": alpha}, "gold_b": {"kind": "gold", "n": 28, "mrr": beta}}

    def test_promotes_when_favourites_gain_and_gold_holds(self):
        ok, why = finetune.decide(self.sets(0.50, 0.90, 0.85), self.sets(0.52, 0.895, 0.85), latency_ratio=1.05)
        self.assertTrue(ok, why)

    def test_refuses_small_gain(self):
        ok, why = finetune.decide(self.sets(0.50, 0.90, 0.85), self.sets(0.519, 0.90, 0.85))
        self.assertFalse(ok)
        self.assertIn("favourites MRR", why[0])

    def test_refuses_gold_loss(self):
        ok, why = finetune.decide(self.sets(0.50, 0.90, 0.85), self.sets(0.60, 0.889, 0.85))
        self.assertFalse(ok)
        self.assertTrue(why[0].startswith("gold_a MRR -0.011"))
        self.assertTrue(finetune.decide(self.sets(0.50, 0.90, 0.85), self.sets(0.60, 0.891, 0.85))[0])
        # the measured dry-run case: favourites +0.113, gold_b +0.015, gold_a -0.049
        ok, why = finetune.decide(self.sets(0.479, 0.855, 0.845, 70), self.sets(0.592, 0.806, 0.860, 70))
        self.assertEqual((ok, why), (False, ["gold_a MRR -0.049 (may lose at most 0.01)"]))

    def test_thresholds_are_parameters(self):
        b, c = self.sets(0.50, 0.90, 0.85), self.sets(0.51, 0.88, 0.85)
        self.assertFalse(finetune.decide(b, c)[0])
        self.assertTrue(finetune.decide(b, c, fav_min_gain=0.01, gold_max_loss=0.02)[0])

    def test_refuses_what_it_cannot_measure(self):
        ok, why = finetune.decide(self.sets(0.5, 0.9, 0.85, n_fav=5), self.sets(0.9, 0.9, 0.85, n_fav=5))
        self.assertFalse(ok)
        self.assertIn("held-out favourite questions", why[0])
        b = self.sets(0.5, 0.9, 0.85)
        b["gold_b"]["n"] = 0
        self.assertFalse(finetune.decide(b, self.sets(0.6, 0.9, 0.85))[0])
        self.assertFalse(finetune.decide({"gold_a": b["gold_a"]}, {"gold_a": b["gold_a"]})[0])

    def test_latency(self):
        b, c = self.sets(0.5, 0.9, 0.85), self.sets(0.6, 0.9, 0.85)
        self.assertTrue(finetune.decide(b, c, latency_ratio=1.10)[0])
        ok, why = finetune.decide(b, c, latency_ratio=1.12)
        self.assertFalse(ok)
        self.assertIn("slower", why[0])


class Split(unittest.TestCase):
    def test_stable_and_about_twenty_percent(self):
        qs = [f"question number {i}" for i in range(5000)]
        sides = [finetune.split_of(q) for q in qs]
        self.assertEqual(sides, [finetune.split_of(q) for q in qs])
        share = sides.count("test") / len(sides)
        self.assertTrue(0.18 < share < 0.22, share)
        self.assertEqual({finetune.split_of(q, 0.0) for q in qs[:200]}, {"train"})

    def test_collect_keeps_a_query_on_one_side_and_never_trains_on_gold(self):
        favs = [{"q": f"Query {i}", "expect": [f"f{i}.md", f"g{i}.md"], "collection": "c1"} for i in range(60)]
        favs.append({"q": "Gold question", "expect": ["gold.md"], "collection": "c1"})
        auto = [{"q": "query 3", "expect": ["x.md"], "collection": "c1"},
                {"q": "an auto question", "expect": ["y.md"], "collection": "c1"}]

        def fake_candidates(q, coll, window):
            i = q.split()[-1]
            return [cand(f"f{i}.md", 1), cand(f"g{i}.md", 2), cand("noise1.md", 3), cand("noise2.md", 4),
                    cand("x.md", 5), cand("y.md", 6)], q.lower()

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(feedback, "gold", return_value=favs), \
                mock.patch.object(feedback, "list_items", return_value=[]), \
                mock.patch.object(finetune, "candidates", side_effect=fake_candidates), \
                mock.patch.object(finetune, "gold_queries", return_value={"gold question"}), \
                mock.patch("mse.learn.load_auto_gold", return_value=auto), \
                mock.patch.object(config, "load", return_value={"collections": [{"name": "c1", "enabled": True}]}):
            s = {**finetune.DEFAULTS, "include_auto": True}
            r = finetune.collect(s, out=Path(d) / "examples.jsonl")
            lines = finetune._read_jsonl(Path(d) / "examples.jsonl")
            summary = json.loads((Path(d) / "collect.json").read_text())
        self.assertEqual(len(lines), r["groups"])
        self.assertNotIn("gold question", {g["norm"] for g in lines})
        self.assertEqual(r["stats"]["dropped: same question as a gold set"], 1)
        side: dict[str, set] = {}
        for g in lines:
            side.setdefault(g["norm"], set()).add(g["split"])
            self.assertTrue({"query", "raw_query", "norm", "collection", "source", "weight", "split", "pos",
                             "negs"} <= set(g))
            self.assertEqual(g["collection"], "c1")
            self.assertNotIn(g["pos"]["path"], {n["path"] for n in g["negs"]})
            if g["source"] == "favourite":                 # the query's other favourite is never a negative
                self.assertFalse({n["path"] for n in g["negs"]} & set(next(
                    f["expect"] for f in favs if f["q"] == g["raw_query"])))
        self.assertTrue(all(len(v) == 1 for v in side.values()), "a query on both sides")
        test_q = {n for n, v in side.items() if v == {"test"}}
        self.assertTrue(test_q, "no held-out query in 60")
        self.assertEqual({h["q"].lower() for h in summary["heldout"]}, test_q)
        autos = [g for g in lines if g["source"] == "auto"]
        self.assertTrue(all(g["split"] == "train" and g["weight"] == s["auto_weight"] for g in autos))
        if "query 3" in test_q:
            self.assertNotIn("query 3", {g["norm"] for g in autos})
        # passages are only in the working file, never in what is shown or kept with an adapter
        self.assertNotIn("passage", json.dumps(summary))


class Registry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def adapter(self, aid, promote=None):
        p = self.root / aid
        p.mkdir()
        (p / "adapters.safetensors").write_bytes(b"x")
        if promote is not None:
            (p / "metrics.json").write_text(json.dumps({"promote": promote, "reasons": [] if promote else ["no"]}))

    def test_promote_rollback_only_move_the_pointer(self):
        self.adapter("2025-03-01_0300", True)
        self.adapter("2025-03-02_0300", False)
        self.adapter("2025-03-03_0300", True)
        (self.root / "2025-03-04_0300").mkdir()                       # a run without weights is not an adapter
        self.assertEqual(finetune.adapters(self.root), ["2025-03-01_0300", "2025-03-02_0300", "2025-03-03_0300"])
        self.assertIsNone(finetune.active_id(self.root))
        with self.assertRaises(ValueError):
            finetune.promote("2025-03-02_0300", root=self.root)       # its evaluation refused it
        self.assertEqual(finetune.promote("2025-03-01_0300", root=self.root)["to"], "2025-03-01_0300")
        self.assertEqual(finetune.promote(root=self.root)["to"], "2025-03-03_0300")   # newest that passed
        self.assertEqual(finetune.active_id(self.root), "2025-03-03_0300")
        self.assertEqual(finetune.rollback(root=self.root)["to"], "2025-03-01_0300")
        self.assertEqual(finetune.active_id(self.root), "2025-03-01_0300")
        self.assertEqual(finetune.rollback(root=self.root)["to"], None)
        self.assertEqual((self.root / "CURRENT").read_text().strip(), "none")
        self.assertIsNone(finetune.active_id(self.root))
        with self.assertRaises(ValueError):
            finetune.rollback(root=self.root)
        finetune.rollback("2025-03-03_0300", root=self.root)          # explicit target
        self.assertEqual(finetune.active_id(self.root), "2025-03-03_0300")
        self.assertEqual(finetune.promote("2025-03-02_0300", root=self.root, force=True)["forced"], True)
        with self.assertRaises(KeyError):
            finetune.rollback("2025-03-09_0300", root=self.root)
        # nothing is ever deleted
        self.assertEqual(len(list(self.root.iterdir())), 6)
        kinds = [e["kind"] for e in finetune.history(self.root)]
        self.assertEqual(kinds, ["promote", "promote", "rollback", "rollback", "rollback", "promote"])

    def test_dry_run_changes_nothing(self):
        self.adapter("2025-03-01_0300", True)
        self.assertEqual(finetune.promote(root=self.root, dry_run=True)["action"], "dry-run")
        self.assertFalse((self.root / "CURRENT").exists())
        self.assertEqual(finetune.history(self.root), [])

    def test_new_dirs_never_collide(self):
        t = 1791400000.0
        a = finetune.new_adapter_dir(self.root, t)
        b = finetune.new_adapter_dir(self.root, t)
        self.assertNotEqual(a, b)
        self.assertTrue(b.name.startswith(a.name) and b.name.endswith("_2"))

    def test_status_line(self):
        with mock.patch.object(finetune, "counts", return_value={"favourites": 42, "queries": 30,
                                                                 "per_collection": {}}):
            st = finetune.status(dict(finetune.DEFAULTS), root=self.root)
        self.assertEqual(st["gate"], "skipped: 42/300")
        self.assertEqual(st["line"], "Learning: 42 / 300 favourites until the first fine-tune · base model")

    def test_worker_finds_the_adapter_named_in_current(self):
        try:
            from mse.worker import Reranker
        except ImportError:
            self.skipTest("mlx not installed")
        with mock.patch("mse.paths.MODELS", self.root.parent), mock.patch.object(Reranker, "ADAPTERS", self.root.name):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MSE_RERANK_ADAPTER", None)
                self.assertIsNone(Reranker.find_adapter())                   # no CURRENT: base model
                (self.root / "CURRENT").write_text("2025-03-01_0300\n")
                self.assertEqual(Reranker.find_adapter(), self.root / "2025-03-01_0300")
                (self.root / "CURRENT").write_text("none\n")
                self.assertIsNone(Reranker.find_adapter())
                os.environ["MSE_RERANK_ADAPTER"] = "/some/dir"
                self.assertEqual(Reranker.find_adapter(), Path("/some/dir"))
                os.environ["MSE_RERANK_ADAPTER"] = "off"
                self.assertIsNone(Reranker.find_adapter())


class Candidates(unittest.TestCase):
    def test_exact_reranker_text_in_retrieval_order(self):
        res = {"text": "supplier invoice", "results": [
            {"path": "a.pdf", "docid": "1", "copies": [{"path": "a.digest.md"}], "_rr": "A (a.pdf)\npassage"},
            {"path": "b.md", "docid": "2", "copies": [], "_rr": "B (b.md)"}]}
        with mock.patch("mse.search.search", return_value=res) as s:
            out, q = finetune.candidates("supplier invoice type:pdf", "c1", 10)
        opts = s.call_args.kwargs["opts"]
        self.assertEqual(opts, {"feedback": False, "rerank": False, "keep_rr": True})
        self.assertEqual(s.call_args.kwargs["collections"], ["c1"])
        self.assertEqual(q, "supplier invoice")                 # the parsed text the re-ranker receives
        self.assertEqual([(c["path"], c["rank"], c["copies"], c["text"]) for c in out],
                         [("a.pdf", 1, ["a.digest.md"], "A (a.pdf)\npassage"), ("b.md", 2, [], "B (b.md)")])

    def test_search_without_passages_is_an_error(self):
        with mock.patch("mse.search.search", return_value={"text": "q", "results": [{"path": "a", "copies": []}]}):
            with self.assertRaises(RuntimeError):
                finetune.candidates("q", "c1")


class FakeWorker:
    def __init__(self, alive=True, started_at=0.0):
        self.alive, self.started_at = alive, started_at


class FakePool:
    def __init__(self, worker):
        self.workers = {"rerank": worker}
        self.calls, self.stopped = [], []

    def rerank(self, query, docs):
        self.calls.append((query, list(docs)))
        return [0.5] * len(docs)

    def stop(self, mode, wait=10):
        self.stopped.append(mode)
        self.workers[mode].alive = False


class RerankCache(unittest.TestCase):
    def setUp(self):
        from mse import rerank
        self.rerank = rerank
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        rerank._cache.clear()
        env = mock.patch.dict(os.environ)
        env.start()
        os.environ.pop("MSE_RERANK_ADAPTER", None)
        self.addCleanup(env.stop)
        self.addCleanup(rerank._cache.clear)
        self.addCleanup(self.tmp.cleanup)

    def test_scores_are_cached_per_adapter(self):
        fp = FakePool(FakeWorker(alive=False))
        with mock.patch.object(self.rerank, "pool", return_value=fp), \
                mock.patch.object(finetune, "registry_dir", return_value=self.root):
            self.rerank._scores("q", ["d1", "d2"])
            self.rerank._scores("q", ["d1", "d2"])
            self.assertEqual(len(fp.calls), 1)                 # base model: cached
            self.assertEqual(self.rerank.adapter_id(), "base")
            (self.root / "CURRENT").write_text("2025-03-08_0300\n")
            self.assertEqual(self.rerank.adapter_id(), "2025-03-08_0300")
            self.rerank._scores("q", ["d1", "d2"])
            self.assertEqual(len(fp.calls), 2)                 # another adapter: scored again
            self.rerank._scores("q", ["d1"])
            self.assertEqual(len(fp.calls), 2)
            os.environ["MSE_RERANK_ADAPTER"] = "none"
            self.assertEqual(self.rerank.adapter_id(), "none")

    def test_worker_restarts_when_current_moved_after_it_started(self):
        (self.root / "CURRENT").write_text("2025-03-08_0300\n")
        moved = (self.root / "CURRENT").stat().st_mtime
        with mock.patch.object(finetune, "registry_dir", return_value=self.root):
            fp = FakePool(FakeWorker(alive=True, started_at=moved + 5))
            with mock.patch.object(self.rerank, "pool", return_value=fp):
                self.rerank._fresh_worker()
            self.assertEqual(fp.stopped, [])                   # started after the pointer moved: keep it
            fp = FakePool(FakeWorker(alive=True, started_at=moved - 5))
            with mock.patch.object(self.rerank, "pool", return_value=fp):
                self.rerank._fresh_worker()
                self.rerank._cache[("x", "q", "d")] = 1.0
                self.rerank.reset()
            self.assertEqual(fp.stopped, ["rerank", "rerank"])
            self.assertEqual(len(self.rerank._cache), 0)


class FakeProc:
    def __init__(self, out=b"", code=0, polls=0):
        self.stdout = mock.Mock(read=mock.Mock(return_value=out))
        self.returncode, self._polls, self.terminated = code, polls, False

    def poll(self):
        if self.terminated:
            return -15
        if self._polls > 0:
            self._polls -= 1
            return None
        return self.returncode

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return -15

    def kill(self):
        self.terminated = True


class NightlyJob(unittest.TestCase):
    def setUp(self):
        from mse import jobs
        self.jobs = jobs
        self.real_window = jobs.in_night_window
        self.cfg = {"schedule": {"nightly_at": "02:30", "only_on_ac_power": True}, "finetune": {}}
        self.logged = []
        for p in (mock.patch.object(finetune, "log_event", side_effect=lambda e, root=None: self.logged.append(e)),
                  mock.patch.object(jobs.time, "sleep"),
                  mock.patch.object(jobs, "in_night_window", return_value=True),
                  mock.patch.object(jobs, "on_ac_power", return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def counts(self, n, q):
        return mock.patch.object(finetune, "counts", return_value={"favourites": n, "queries": q,
                                                                   "per_collection": {}})

    def test_below_the_gate_no_process(self):
        with self.counts(42, 30), mock.patch("subprocess.Popen") as popen:
            r = self.jobs.finetune_job(self.cfg)
        popen.assert_not_called()
        self.assertEqual(r, {"action": "skipped", "reason": "skipped: 42/300"})
        self.assertEqual(self.logged[-1]["reason"], "skipped: 42/300")
        cfg = {**self.cfg, "finetune": {"enabled": False}}
        with self.counts(400, 200), mock.patch("subprocess.Popen") as popen:
            self.assertEqual(self.jobs.finetune_job(cfg)["reason"], "disabled")
        popen.assert_not_called()

    def test_battery_and_night_window(self):
        with self.counts(400, 200), mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(self.jobs, "on_ac_power", return_value=False):
            self.assertEqual(self.jobs.finetune_job(self.cfg)["reason"], "skipped: on battery")
        with self.counts(400, 200), mock.patch("subprocess.Popen") as popen2, \
                mock.patch.object(self.jobs, "in_night_window", return_value=False):
            self.assertEqual(self.jobs.finetune_job(self.cfg)["reason"], "skipped: outside the night window")
        popen.assert_not_called()
        popen2.assert_not_called()

    def test_promotion_restarts_the_reranker(self):
        out = json.dumps({"kind": "run", "action": "promoted", "id": "2025-03-08_0300"}).encode()
        with self.counts(400, 200), mock.patch("subprocess.Popen", return_value=FakeProc(out, polls=2)) as popen, \
                mock.patch("mse.rerank.reset") as reset:
            r = self.jobs.finetune_job(self.cfg)
        self.assertEqual(r["action"], "promoted")
        reset.assert_called_once()
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[:3], ["/usr/bin/nice", "-n", "10"])                 # low priority
        self.assertEqual(cmd[-4:], ["-m", "mse.finetune", "run", "--json"])      # its own process
        with self.counts(400, 200), mock.patch("subprocess.Popen", return_value=FakeProc(b"", code=1)), \
                mock.patch("mse.rerank.reset") as reset:
            r = self.jobs.finetune_job(self.cfg)
        self.assertEqual(r["action"], "failed")
        reset.assert_not_called()

    def test_cancel_and_battery_stop_the_process(self):
        proc = FakeProc(polls=100)
        with self.counts(400, 200), mock.patch("subprocess.Popen", return_value=proc):
            r = self.jobs.finetune_job(self.cfg, cancel=lambda: True)
        self.assertEqual(r, {"action": "stopped", "reason": "cancelled"})
        self.assertTrue(proc.terminated)
        self.assertEqual(self.logged[-1]["action"], "stopped")

    def test_night_window(self):
        from datetime import datetime
        cfg = {"schedule": {"nightly_at": "02:30"}}
        in_night_window = self.real_window
        self.assertTrue(in_night_window(cfg, datetime(2025, 3, 8, 3, 0)))
        self.assertTrue(in_night_window(cfg, datetime(2025, 3, 8, 7, 29)))
        self.assertFalse(in_night_window(cfg, datetime(2025, 3, 8, 7, 31)))
        self.assertFalse(in_night_window(cfg, datetime(2025, 3, 8, 2, 29)))
        self.assertTrue(in_night_window({"schedule": {"nightly_at": "23:00"}}, datetime(2025, 3, 9, 1, 0)))


class Settings(unittest.TestCase):
    def test_config_defaults_match_the_module(self):
        for k, v in config.DEFAULTS["finetune"].items():
            self.assertEqual(finetune.DEFAULTS[k], v, k)
        self.assertEqual(finetune.DEFAULTS["gold_max_loss"], 0.01)
        s = finetune.settings({"finetune": {"min_examples": 50, "collections": ["acme"]}})
        self.assertEqual((s["min_examples"], s["collections"], s["min_queries"]), (50, ["acme"], 100))


if __name__ == "__main__":
    unittest.main()
