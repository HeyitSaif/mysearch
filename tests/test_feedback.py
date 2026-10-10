"""Favourites (mse/feedback.py) and learning (mse/learn.py, evaluate.tune / guardrails).

Builds a small collection in a temporary MSE_HOME with a deterministic fake query embedding, so no model is loaded.
Run: .venv/bin/python -m unittest discover -s tests
"""

import hashlib
import json
import os
import tempfile
import unittest
import unicodedata
from pathlib import Path
from unittest import mock

os.environ["MSE_HOME"] = tempfile.mkdtemp(prefix="mse-test-")

import numpy as np  # noqa: E402

from mse import config, evaluate, feedback, learn, paths, search, store  # noqa: E402
from mse.textutil import name_tokens  # noqa: E402

DIMS = 768


def unit(v):
    return v / np.linalg.norm(v)


def _hash_vec(text: str) -> np.ndarray:
    """A fixed pseudo-random vector per text: seeded from the md5 of the text itself (lower-cased)."""
    key = text.lower()
    seed = int(hashlib.md5(key.encode()).hexdigest()[:8], 16)
    return unit(np.random.default_rng(seed).normal(size=DIMS)).astype(np.float32)


_rng = np.random.default_rng(42)
_ACME = unit(_rng.normal(size=DIMS))
_noise1, _noise2 = unit(_rng.normal(size=DIMS)), unit(_rng.normal(size=DIMS))
QUERY_VECS = {
    "acme invoice": _ACME,
    "invoice from acme": unit(_ACME + 0.25 * _noise1),     # cosine ~0.97: a rewording
    "acme credit note": unit(_ACME + 0.60 * _noise2),      # cosine ~0.86: another question
}


def fake_embed(text: str) -> np.ndarray:
    v = QUERY_VECS.get(text.strip().lower())
    return (v if v is not None else _hash_vec(text)).astype(np.float32)


FILES = {
    "invoices/2025-03-11_Acme_Rechnung_1234567890_100.00EUR.pdf": ("pdf", "Rechnung", "Acme Elektronik invoice Rechnung for battery cells, 100,00 EUR"),
    "notes/acme_order_notes.md": ("text", "Acme order notes", "Notes: we ordered battery cells from Acme, the invoice follows"),
    "letters/supplier_invoice.md": ("text", "Supplier invoice", "Kunde Rechnung for the printer repair, 250,00 EUR"),
    "misc/zebra_manual.md": ("text", "Zebra crossing manual", "A manual about zebra crossings and traffic lights"),
    "misc/gone.md": ("text", "Gone file", "A zebra note that will be deleted"),
}
COLL = "fbtest"


def build_collection(name: str, files: dict) -> Path:
    root = Path(tempfile.mkdtemp(prefix=f"mse-{name}-"))
    for rel, (_, _, text) in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    config.add_collection(str(root), name)
    conn = store.connect(name)
    for n, (rel, (kind, title, text)) in enumerate(files.items()):
        fid = conn.execute(
            "INSERT INTO files(path, name, ext, kind, mode, state, title, doc_date, docid, indexed_at)"
            " VALUES (?,?,?,?, 'full', 'done', ?, NULL, ?, ?)",
            (rel, rel.rsplit("/", 1)[-1], rel.rsplit(".", 1)[-1], kind, title,
             hashlib.sha1(f"{name}:{rel}".encode()).hexdigest()[:8], 1000.0 + n)).lastrowid
        store.add_items(conn, fid, name_tokens(rel), [{"modality": "text", "text": text}])
        iid = conn.execute("SELECT id FROM items WHERE file_id=?", (fid,)).fetchone()[0]
        store.store_vectors(conn, [(iid, "text", _hash_vec(text))])
    conn.commit()
    conn.close()
    return Path(config.get_collection(config.load(), name)["path"])


_patch = mock.patch.object(search, "embed_query", fake_embed)
ROOT: Path


def setUpModule():
    global ROOT
    _patch.start()
    ROOT = build_collection(COLL, FILES)


def tearDownModule():
    _patch.stop()


def clear_feedback():
    for it in feedback.list_items():
        feedback.remove(id=it["id"])


def run(q, mode="keyword", **kw):
    return search.search(q, collections=[COLL], mode=mode, limit=10, **kw)


def paths_of(res):
    return [r["path"] for r in res["results"]]


PDF, NOTES, SUPPLIER, ZEBRA, GONE = list(FILES)


class Storage(unittest.TestCase):
    def setUp(self):
        clear_feedback()

    def test_normalised_query(self):
        self.assertEqual(feedback.normalize("  Acme   INVOICE?  type:pdf "), "acme invoice")
        self.assertEqual(feedback.normalize("Bürobedarf Kunde"), "burobedarf kunde")
        self.assertEqual(feedback.normalize("type:pdf"), "")

    def test_roundtrip_and_duplicates(self):
        fid = feedback.add("Acme invoice", COLL, PDF, "best")
        items = feedback.list_items()
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual({k: it[k] for k in ("id", "query", "collection", "path", "kind", "uses")},
                         {"id": fid, "query": "Acme invoice", "collection": COLL, "path": PDF, "kind": "best", "uses": 0})
        self.assertEqual(set(it), set(feedback.FIELDS))
        self.assertEqual(len(it["docid"]), 8)
        # the same file for the same (normalised) query updates the row, by path or by #docid
        self.assertEqual(feedback.add("acme  Invoice", COLL, "#" + it["docid"], "hide"), fid)
        self.assertEqual([i["kind"] for i in feedback.list_items()], ["hide"])
        conn = feedback._connect()
        qv = conn.execute("SELECT qvec FROM feedback WHERE id=?", (fid,)).fetchone()[0]
        conn.close()
        self.assertEqual(len(qv), 2 * DIMS)                       # 768 float16
        self.assertAlmostEqual(float(np.linalg.norm(store.from_f16(qv))), 1.0, places=2)
        # absolute path and NFD spelling resolve to the same file
        self.assertEqual(feedback.add("acme invoice", None, str(ROOT / PDF), "best"), fid)
        self.assertEqual(feedback.add("acme invoice", COLL, unicodedata.normalize("NFD", PDF)), fid)
        self.assertEqual(feedback.list_items("acme")[0]["id"], fid)
        self.assertEqual(feedback.list_items("nothing-like-this"), [])
        self.assertEqual(feedback.remove(query="Acme invoice", collection=COLL, ref=PDF), 1)
        fid2 = feedback.add("acme invoice", COLL, NOTES)
        self.assertEqual(feedback.remove(id=fid2), 1)
        self.assertEqual(feedback.list_items(), [])

    def test_errors(self):
        with self.assertRaises(KeyError):
            feedback.add("acme invoice", COLL, "no/such/file.pdf")
        with self.assertRaises(ValueError):
            feedback.add("acme invoice", COLL, PDF, "love")
        with self.assertRaises(ValueError):
            feedback.add("type:pdf", COLL, PDF)

    def test_gold_from_favourites(self):
        feedback.add("Acme invoice", COLL, PDF)
        feedback.add("acme invoice", COLL, NOTES)
        feedback.add("zebra", COLL, ZEBRA, "hide")
        self.assertEqual(feedback.gold(), [{"q": "Acme invoice", "expect": [PDF, NOTES], "collection": COLL,
                                            "source": "favourite"}])


class Applying(unittest.TestCase):
    def setUp(self):
        clear_feedback()

    def test_exact_query_pins_in_marked_order(self):
        before = run("acme invoice")
        self.assertEqual(before["feedback"]["applied"], 0)
        self.assertTrue(all(r["favorite"] is None for r in before["results"]))
        feedback.add("acme invoice", COLL, SUPPLIER)       # marked first
        feedback.add("acme invoice", COLL, NOTES)
        res = run("Acme   Invoice")
        self.assertEqual(paths_of(res)[:2], [SUPPLIER, NOTES])
        self.assertEqual([r["favorite"] for r in res["results"][:2]], ["query", "query"])
        self.assertEqual(res["feedback"]["applied"], 2)
        self.assertEqual(res["results"][0]["relevance"], 100)
        scores = [r["score"] for r in res["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(len(set(paths_of(res))), len(res["results"]))   # nothing listed twice
        self.assertEqual(feedback.list_items()[0]["uses"], 1)
        off = run("acme invoice", opts={"feedback": False})
        self.assertEqual(off["feedback"], {"applied": 0})
        self.assertNotEqual(paths_of(off)[0], SUPPLIER)

    def test_injection_respects_existence_filters_and_scope(self):
        feedback.add("acme invoice", COLL, ZEBRA)        # not a keyword match: retrieval misses it
        res = run("acme invoice")
        self.assertEqual(paths_of(res)[0], ZEBRA)
        self.assertEqual(res["results"][0]["why"], ["favourite"])
        self.assertEqual(res["feedback"]["injected"], 1)
        self.assertNotIn(ZEBRA, paths_of(run("acme invoice", filters={"path": ["invoices"]})))
        self.assertNotIn(ZEBRA, paths_of(run("acme invoice", filters={"type": ["pdf"]})))
        self.assertEqual(search.search("acme invoice", collections=["other-coll"], mode="keyword")["results"], [])

    def test_hide(self):
        self.assertIn(PDF, paths_of(run("acme invoice")))
        feedback.add("acme invoice", COLL, PDF, "hide")
        res = run("acme invoice")
        self.assertNotIn(PDF, paths_of(res))
        self.assertEqual(res["feedback"]["hidden"], 1)
        # a hide does not carry over to similar queries
        self.assertIn(PDF, paths_of(run("invoice from acme", mode="hybrid")))

    def test_similar_query_boost(self):
        q = "invoice from acme"
        base = {r["path"]: r for r in run(q, mode="hybrid", opts={"feedback": False})["results"]}
        target = NOTES if base[NOTES]["score"] < base[PDF]["score"] else PDF
        feedback.add("acme invoice", COLL, target)
        feedback.add("acme invoice", COLL, ZEBRA)
        res = run(q, mode="hybrid", filters={"exclude": ["misc/"]})
        got = {r["path"]: r for r in res["results"]}
        self.assertEqual(got[target]["favorite"], "similar")
        self.assertGreater(got[target]["score"], base[target]["score"])
        self.assertNotIn(ZEBRA, got)                       # boosted only if retrieved: never injected
        self.assertEqual(res["feedback"]["boosted"], 1)
        # a different question (cosine ~0.86) gets nothing
        other = run("acme credit note", mode="hybrid")
        self.assertTrue(all(r["favorite"] is None for r in other["results"]))
        # keyword mode has no query vector: exact matches only
        self.assertTrue(all(r["favorite"] is None for r in run(q)["results"]))

    def test_missing_file_is_ignored(self):
        feedback.add("zebra note", COLL, GONE)
        self.assertEqual(paths_of(run("zebra note"))[0], GONE)
        (ROOT / GONE).rename(ROOT / "misc" / "gone.moved")      # gone from disk, still in the index
        try:
            res = run("zebra note")
            self.assertTrue(all(r["favorite"] is None for r in res["results"]))
            self.assertEqual(res["feedback"]["pinned"], 0)
            feedback.add("acme invoice", COLL, ZEBRA)
            conn = store.connect(COLL)                          # and one that left the index entirely
            conn.execute("UPDATE files SET path='misc/renamed.md' WHERE path=?", (ZEBRA,))
            conn.commit()
            conn.close()
            res = run("acme invoice")
            self.assertNotIn(ZEBRA, paths_of(res))
        finally:
            (ROOT / "misc" / "gone.moved").rename(ROOT / GONE)
            conn = store.connect(COLL)
            conn.execute("UPDATE files SET path=? WHERE path='misc/renamed.md'", (ZEBRA,))
            conn.commit()
            conn.close()

    def test_feedback_errors_never_fail_a_search(self):
        with mock.patch.object(feedback, "apply", side_effect=RuntimeError("db broken")):
            res = run("acme invoice")
        self.assertTrue(res["results"])
        self.assertIn("db broken", res["feedback"]["error"])
        self.assertTrue(all(r["favorite"] is None for r in res["results"]))

    def test_reranker_cannot_move_a_pinned_favourite(self):
        from mse import rerank
        feedback.add("acme invoice", COLL, SUPPLIER)
        calls = []

        def scores(query, docs):                           # the cross-encoder prefers the last candidates
            calls.append(len(docs))
            return [float(i) for i in range(len(docs))]

        with mock.patch.object(rerank, "_scores", scores):
            res = run("acme invoice", mode="hybrid", opts={"rerank": True, "rerank_gate": 0.0})
        self.assertTrue(calls)
        self.assertEqual(paths_of(res)[0], SUPPLIER)
        self.assertNotIn("rerank", res["results"][0])        # the pinned result was not handed to the reranker


class KnownItems(unittest.TestCase):
    def test_clean_query(self):
        self.assertEqual(learn.clean_query("2025-06-17_Acme_Supplies_Lieferantenrechnung_Buerobedarf_18.90EUR", True),
                         "Acme Supplies Lieferantenrechnung Buerobedarf")
        self.assertEqual(learn.clean_query("2025-03-11_Acme_Rechnung_1234567890", True), "Acme Rechnung")
        self.assertEqual(learn.clean_query(unicodedata.normalize("NFD", "Nutzungsbestätigung Acme")),
                         "Nutzungsbestätigung Acme")
        self.assertEqual(learn.clean_query("Acme_Group_Price_List.pdf — digest"), "Acme Group Price List")

    def test_builder(self):
        files = {
            "a/2025-03-01_Acme_Supplies_Auftragsbestaetigung.pdf": ("pdf", "Acme Supplies Auftragsbestätigung", "x"),
            "a/SUMMARY.md": ("text", "Summary of the folder", "x"),                      # generic: skipped
            "a/2025-03-02_Rechnung_123456.pdf": ("pdf", "Rechnung", "x"),                # 1 word, name has 1 word
            "b/Acme_Office_Contract.pdf": ("pdf", "Acme Group", "x"),           # title shared by 3 docs
            "b/Supplier_Invoice_Scan.pdf": ("pdf", "Acme Group", "x"),
            "b/Delivery_Letter_Scan.pdf": ("pdf", "Acme Group", "x"),
            "backups/Acme_Supplies_Auftragsbestaetigung.pdf": ("pdf", "Acme Supplies Auftragsbestätigung", "x"),
            "c/identity_card.pdf": ("pdf", "Identity card scan", "x"),                   # metadata-only
        }
        build_collection("kitest", files)
        conn = store.connect("kitest")
        conn.execute("UPDATE files SET mode='meta' WHERE path='c/identity_card.pdf'")
        conn.commit()
        conn.close()
        g = learn.build_auto_gold(["kitest"], save=False)
        by_file = {q["file"]: q for q in g["queries"]}
        self.assertNotIn("a/SUMMARY.md", by_file)
        self.assertNotIn("a/2025-03-02_Rechnung_123456.pdf", by_file)
        self.assertNotIn("c/identity_card.pdf", by_file)
        self.assertNotIn("backups/Acme_Supplies_Auftragsbestaetigung.pdf", by_file)   # a copy, not a source
        supplies = by_file["a/2025-03-01_Acme_Supplies_Auftragsbestaetigung.pdf"]
        self.assertEqual((supplies["q"], supplies["source"]), ("Acme Supplies Auftragsbestätigung", "title"))
        self.assertEqual(len(supplies["expect"]), 2)                                     # the backup also counts
        # an ambiguous title falls back to the file name, dates/IDs stripped
        self.assertEqual(by_file["b/Acme_Office_Contract.pdf"]["q"], "Acme Office Contract")
        self.assertEqual(by_file["b/Acme_Office_Contract.pdf"]["source"], "name")
        self.assertTrue(all(len(learn.meaningful_words(q["q"])) >= 2 for q in g["queries"]))
        self.assertEqual(len(learn.build_auto_gold(["kitest"], per_collection=2, save=False)["queries"]), 2)
        learn.build_auto_gold(["kitest"])
        self.assertEqual(len(learn.load_auto_gold()), len(g["queries"]))


def scored(sets: dict, wmrr: float) -> dict:
    """A score() result from {name: (kind, ranks)}."""
    out = {}
    for name, (kind, ranks) in sets.items():
        out[name] = {**evaluate._metrics(ranks), "kind": kind, "skipped": 0, "ranks": ranks,
                     "questions": [f"{name} q{i}" for i in range(len(ranks))]}
    return {"sets": out, "weighted_mrr": wmrr, "weighted_hit1": 0.0, "n": 0, "settings": {}, "rerank": False}


class Guardrails(unittest.TestCase):
    BASE = {"gold_a": ("gold", [1, 1, 2, 3]), "gold_b": ("gold", [1, 2]),
            "favourites": ("favourites", [1, 2]), "auto": ("auto", [1, 1, 4])}

    def cand(self, wmrr, **sets):
        return scored({**self.BASE, **sets}, wmrr)

    def test_applies_when_everything_holds(self):
        base = scored(self.BASE, 0.700)
        self.assertEqual(evaluate.guardrails(base, self.cand(0.712, auto=("auto", [1, 1, 1]))), [])

    def test_refuses_small_gain(self):
        reasons = evaluate.guardrails(scored(self.BASE, 0.700), self.cand(0.705))
        self.assertEqual(len(reasons), 1)
        self.assertIn("weighted MRR +0.005", reasons[0])

    def test_refuses_gold_mrr_loss(self):
        reasons = evaluate.guardrails(scored(self.BASE, 0.7), self.cand(0.8, gold_a=("gold", [1, 1, 3, 3])))
        self.assertTrue(any("gold_a MRR" in r for r in reasons), reasons)

    def test_refuses_any_lost_rank1_hit(self):
        # gold_b keeps its MRR (1, 2 -> 2, 1) but its first question loses the right file at rank 1
        reasons = evaluate.guardrails(scored(self.BASE, 0.7), self.cand(0.8, gold_b=("gold", [2, 1])))
        self.assertEqual(len(reasons), 1)
        self.assertIn("gold_b loses the right file at rank 1", reasons[0])
        self.assertIn("gold_b q0", reasons[0])

    def test_refuses_worse_favourites(self):
        reasons = evaluate.guardrails(scored(self.BASE, 0.7), self.cand(0.8, favourites=("favourites", [1, 3])))
        self.assertTrue(any(r.startswith("favourites MRR") for r in reasons), reasons)

    def test_auto_set_may_move(self):
        base = scored(self.BASE, 0.7)
        self.assertEqual(evaluate.guardrails(base, self.cand(0.75, auto=("auto", [2, 2, None]))), [])


class LearnRun(unittest.TestCase):
    def setUp(self):
        self.saved = json.loads(paths.CONFIG.read_text())
        learn.quality_path().unlink(missing_ok=True)

    def tearDown(self):
        config.save(self.saved)

    def fake_tune(self, reasons, gain=0.02):
        cur = evaluate.current_settings()
        cand = {**cur, "keyword": round(cur["keyword"] + 0.2, 3), "hub_penalty": round(cur["hub_penalty"] + 0.15, 3)}
        base = scored(Guardrails.BASE, 0.70)
        better = scored(Guardrails.BASE, 0.70 + gain)

        def tune(sets, progress=None, cancel=None, threads=None):
            return {"current": cur, "candidate": cand, "base": base, "cand": better, "changed": True,
                    "reasons": reasons, "evaluated": 30, "rerank": False, "table": []}
        return cur, cand, tune

    def test_applies_records_and_rolls_back(self):
        cur, cand, tune = self.fake_tune([])
        with mock.patch.object(evaluate, "tune", tune):
            r = learn.run(collections=[COLL])
        self.assertEqual(r["action"], "applied")
        self.assertEqual(evaluate.current_settings(), cand)
        e = learn.history()[-1]
        self.assertEqual((e["action"], e["previous"], e["applied"]), ("applied", cur, cand))
        self.assertEqual(set(e["sets"]["gold_a"]), {"n", "hit@1", "hit@3", "hit@10", "mrr", "kind", "skipped"})
        self.assertTrue(learn.auto_gold_path().exists())
        rb = learn.rollback()
        self.assertEqual(rb["action"], "rolled back")
        self.assertEqual(evaluate.current_settings(), cur)
        with self.assertRaises(ValueError):                   # nothing left to undo
            learn.rollback()

    def test_refuses_and_keeps_settings(self):
        cur, _, tune = self.fake_tune(["weighted MRR +0.004 (needs at least +0.01)"], gain=0.004)
        with mock.patch.object(evaluate, "tune", tune):
            r = learn.run(collections=[COLL])
        self.assertEqual(r["action"], "kept")
        self.assertIn("needs at least", r["reason"])
        self.assertEqual(evaluate.current_settings(), cur)
        self.assertNotIn("previous", learn.history()[-1])

    def test_real_tune_on_a_small_set(self):
        gold = Path(tempfile.mkdtemp()) / "gold_small.json"
        gold.write_text(json.dumps({"queries": [{"q": "acme invoice", "expect": ["Acme_Rechnung"]},
                                                {"q": "supplier printer repair invoice", "expect": ["supplier_invoice"]},
                                                {"q": "somewhere else", "expect": ["not/in/this/collection"]}]}))
        sets = evaluate.load_sets([COLL], gold_files=[gold], favourites=False, auto=False)
        self.assertEqual((len(sets["gold_small"]["queries"]), sets["gold_small"]["skipped"]), (2, 1))
        from mse import rerank
        cfg = config.load()
        cfg["search"]["rerank_enabled"] = True                  # stage 3 (re-ranker knobs) runs too
        config.save(cfg)
        with mock.patch.object(rerank, "_scores", lambda q, docs: [float(len(d) % 7) for d in docs]):
            t = evaluate.tune(sets, threads=2)
        self.assertTrue(t["rerank"])
        self.assertLessEqual(t["evaluated"], 39)
        self.assertGreater(t["evaluated"], 29)
        self.assertEqual(set(t["candidate"]), set(evaluate.KNOBS))
        self.assertEqual(t["base"]["sets"]["gold_small"]["n"], 2)
        self.assertTrue(all("safe" in s for s in t["table"]))


class LearnJob(unittest.TestCase):
    def test_learn_is_one_global_job_that_runs_learn(self):
        import threading
        from collections import deque
        from mse.jobs import JobManager
        jm = JobManager.__new__(JobManager)              # no threads, no watcher
        jm.queue, jm.lock, jm.wake, jm._seq, jm.current, jm._cancel = deque(), threading.Lock(), threading.Event(), 0, None, False
        jm.submit("learn", reason="nightly")
        jm.submit("learn", "fbtest")                      # deduplicated, and never per collection
        self.assertEqual([(j["kind"], j["collection"]) for j in jm.queue], [("learn", None)])
        with mock.patch.object(learn, "run", return_value={"action": "kept"}) as run_:
            self.assertEqual(jm._run(jm.queue[0]), {"action": "kept"})
        self.assertTrue(callable(run_.call_args.kwargs["cancel"]))
        self.assertTrue(config.load()["schedule"]["learn_nightly"])


if __name__ == "__main__":
    unittest.main()
