"""Document facts in the index: the doc_meta table and its migration, the indexer hook and the throttled backfill, the search
filters (type / amount / currency / party / docdate, with and without words), the plain-language mapping, facets and the
timeline with its gaps. Fictional documents only; temporary folders and a temporary data folder.
Run: .venv/bin/python -m unittest -v tests.test_docmeta_index"""

import json
import os
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import config, docmeta, indexer, search, store  # noqa: E402
from tests.docmeta_corpus import DEV  # noqa: E402

BY_ID = {d["id"]: d for d in DEV}


def _coll(folder: Path) -> dict:
    return {"name": "t" + uuid.uuid4().hex[:10], "path": str(folder), "enabled": True, "media": {}, "exclude": [],
            "metadata_only": []}


def _cfg(*colls: dict) -> dict:
    cfg = config.load()
    cfg["collections"] = list(colls)
    return cfg


def add_doc(conn, folder: Path, coll: str, rel: str, text: str, meta: dict | None = None, file_date: str = "2025-12-31",
            missing: bool = False, kind: str = "pdf") -> int:
    """A file row with one text chunk and (optionally) its doc_meta row, without running the extractors."""
    (folder / rel).parent.mkdir(parents=True, exist_ok=True)
    (folder / rel).write_text(text)
    cur = conn.execute(
        "INSERT INTO files(path, name, ext, kind, size, mtime, docid, indexed_at, mode, state, doc_date, missing_since)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (rel, Path(rel).name, Path(rel).suffix.lstrip("."), kind, len(text), time.time(), indexer.docid(coll, rel), time.time(),
         "full", "text", file_date, time.time() - 3600 if missing else None))
    fid = cur.lastrowid
    store.add_items(conn, fid, rel, [{"modality": "text", "text": text}])
    if meta is not None:
        docmeta.upsert(conn, fid, meta)
    return fid


def M(doctype, doc_date=None, amount=None, currency=None, counterparty=None, reference=None, lang="en", confidence=0.9) -> dict:
    return {"doctype": doctype, "confidence": confidence, "doc_date": doc_date, "amount": amount, "currency": currency,
            "counterparty": counterparty, "reference": reference, "lang": lang}


# ====================================================================== schema and migration

class Migration(unittest.TestCase):
    def setUp(self):
        self.name = "t" + uuid.uuid4().hex[:10]
        self.folder = Path(tempfile.mkdtemp(prefix="mse-dm-"))

    def test_new_database_has_the_table_its_indexes_and_the_schema_version(self):
        conn = store.connect(self.name)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(doc_meta)")]
        self.assertEqual(cols, ["file_id", "doctype", "confidence", "doc_date", "amount", "currency", "counterparty",
                                "reference", "lang", "extractor_version", "extracted_at"])
        idx = {r[1] for r in conn.execute("PRAGMA index_list(doc_meta)")}
        self.assertTrue({"doc_meta_doctype", "doc_meta_date"} <= idx)
        self.assertEqual(store.get_meta(conn, "schema"), "4")
        conn.close()

    def test_an_old_database_gets_the_table_and_loses_nothing(self):
        conn = store.connect(self.name)
        fid = add_doc(conn, self.folder, self.name, "a.txt", "alpha invoice beta", M("invoice", "2025-03-14", 10.0, "EUR"))
        conn.commit()
        fts_before = conn.execute("SELECT COUNT(*) FROM fts").fetchone()[0]
        items_before = conn.execute("SELECT id, text FROM items ORDER BY id").fetchall()
        # turn it back into a schema-3 database: no doc_meta, no indexes of it
        conn.execute("DROP TABLE doc_meta")
        conn.execute("UPDATE meta SET value='3' WHERE key='schema'")
        conn.commit()
        conn.close()
        ro = store.connect(self.name, readonly=True)           # a reader that no writer has upgraded yet
        self.assertFalse(docmeta.has_table(ro))
        self.assertEqual(search.allowed_files(ro, search.file_filter({"type": ["invoice"]})[0]), set())
        self.assertIsNone(docmeta.get(ro, fid))
        self.assertEqual(docmeta.facets([self.name])["classified"], 0)
        self.assertEqual(docmeta.timeline([self.name])["buckets"], [])
        ro.close()
        conn = store.connect(self.name)                        # the first writable open migrates
        self.assertTrue(docmeta.has_table(conn))
        self.assertEqual(store.get_meta(conn, "schema"), "4")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fts").fetchone()[0], fts_before)       # nothing rebuilt
        self.assertEqual(conn.execute("SELECT id, text FROM items ORDER BY id").fetchall(), items_before)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM doc_meta").fetchone()[0], 0)
        conn.close()

    def test_migration_is_repeatable(self):
        for _ in range(3):
            store.connect(self.name).close()
        conn = store.connect(self.name)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='doc_meta'").fetchone()[0], 1)
        conn.close()

    def test_deleting_a_file_deletes_what_was_derived_from_it(self):
        conn = store.connect(self.name)
        fid = add_doc(conn, self.folder, self.name, "a.txt", "alpha", M("invoice", "2025-03-14"))
        other = add_doc(conn, self.folder, self.name, "b.txt", "beta", M("receipt", "2025-03-15"))
        store.delete_file(conn, fid)
        conn.commit()
        self.assertIsNone(docmeta.get(conn, fid))
        self.assertIsNotNone(docmeta.get(conn, other))
        conn.close()

    def test_pending_counts_skip_files_marked_missing(self):
        conn = store.connect(self.name)
        a = add_doc(conn, self.folder, self.name, "a.txt", "alpha")
        b = add_doc(conn, self.folder, self.name, "b.txt", "beta", missing=True)
        conn.execute("UPDATE files SET heavy='{\"ocr\":[1]}' WHERE id IN (?,?)", (a, b))
        conn.commit()
        st = store.stats(conn)
        self.assertEqual(st["heavy_pending"], 1)       # the missing file's OCR is not pending work
        self.assertEqual(st["embed_pending"], 1)       # nor are its chunks
        self.assertEqual(st["files"], 2)
        conn.close()
        ro = store.connect(self.name, readonly=True)
        self.assertEqual(store.stats(ro)["heavy_pending"], 1)
        ro.close()

    def test_stats_work_on_a_database_without_missing_since(self):
        path = Path(tempfile.mkdtemp(prefix="mse-dm-")) / "old.db"
        raw = sqlite3.connect(path)
        raw.executescript("CREATE TABLE files(id INTEGER PRIMARY KEY, size INTEGER, mode TEXT, state TEXT, heavy TEXT, kind TEXT);"
                          "CREATE TABLE items(id INTEGER PRIMARY KEY, file_id INTEGER, modality TEXT, embedded INTEGER);"
                          "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
                          "INSERT INTO files VALUES (1, 5, 'full', 'text', '{}', 'pdf');"
                          "INSERT INTO items VALUES (1, 1, 'text', 0);")
        raw.row_factory = sqlite3.Row
        st = store.stats(raw)
        self.assertEqual((st["heavy_pending"], st["embed_pending"]), (1, 1))
        raw.close()


# ====================================================================== the indexer

class IndexerHook(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="mse-dm-scan-"))
        self.coll = _coll(self.folder)
        self.name = self.coll["name"]
        docmeta.reset_settings_cache()
        for rel, d in (("Rechnung_RE-2025-0042.txt", "d01"), ("invoice_1042.txt", "d02"), ("umsaetze.csv", "d12"),
                       ("mail_invoice.eml", "d29"), ("meeting.md", "d25")):
            (self.folder / rel).write_text(BY_ID[d]["text"])
        (self.folder / "tool.py").write_text("def f(x):\n    return x + 1\n")
        (self.folder / "empty.txt").write_text("")

    def tearDown(self):
        docmeta.reset_settings_cache()

    def meta(self, rel):
        conn = store.connect(self.name, readonly=True)
        try:
            r = conn.execute("SELECT d.* FROM doc_meta d JOIN files f ON f.id=d.file_id WHERE f.path=?", (rel,)).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    def count(self):
        conn = store.connect(self.name, readonly=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM doc_meta").fetchone()[0]
        finally:
            conn.close()

    def test_scan_writes_the_facts_of_every_eligible_file(self):
        s = indexer.scan(self.coll)
        self.assertEqual(s["errors"], 0)
        inv = self.meta("Rechnung_RE-2025-0042.txt")
        self.assertEqual((inv["doctype"], inv["doc_date"], inv["amount"], inv["currency"], inv["counterparty"], inv["reference"]),
                         ("invoice", "2025-03-14", 255.85, "EUR", "Beispiel Handels GmbH", "RE-2025-0042"))
        self.assertEqual(inv["extractor_version"], docmeta.EXTRACTOR_VERSION)
        self.assertEqual(self.meta("invoice_1042.txt")["amount"], 1320.0)
        self.assertEqual(self.meta("umsaetze.csv")["doctype"], "bank_statement")
        mail = self.meta("mail_invoice.eml")
        self.assertEqual((mail["doctype"], mail["counterparty"], mail["doc_date"]), ("invoice", "Sample Trading Billing", "2025-03-14"))
        self.assertEqual(self.meta("meeting.md")["doctype"], "other")
        self.assertIsNone(self.meta("tool.py"))                       # source code is never classified
        stub = self.meta("empty.txt")                                 # looked at, nothing in it: not classified, not retried
        self.assertIsNotNone(stub)
        self.assertIsNone(stub["doctype"])

    def test_nothing_to_redo_when_nothing_changed(self):
        indexer.scan(self.coll)
        before = self.count()
        s = indexer.scan(self.coll)
        self.assertEqual(s["docmeta"], 0)                              # the stub row of the empty file is not a candidate
        self.assertEqual(self.count(), before)

    def test_a_changed_file_is_read_again(self):
        indexer.scan(self.coll)
        (self.folder / "invoice_1042.txt").write_text(
            "Rechnung Nr. 99\nRechnungsdatum 01.02.2025\nGesamtbetrag CHF 500.00\nZahlungsziel 14 Tage")
        time.sleep(0.02)
        os.utime(self.folder / "invoice_1042.txt", (time.time() + 5, time.time() + 5))
        indexer.scan(self.coll)
        self.assertEqual(self.meta("invoice_1042.txt")["currency"], "CHF")

    def test_a_file_that_turns_metadata_only_loses_its_facts(self):
        indexer.scan(self.coll)
        self.assertIsNotNone(self.meta("Rechnung_RE-2025-0042.txt"))
        self.coll["metadata_only"] = ["Rechnung_RE-2025-0042.txt"]
        indexer.scan(self.coll)
        self.assertIsNone(self.meta("Rechnung_RE-2025-0042.txt"))      # nothing derived from content that is no longer indexed

    def test_removing_a_file_removes_its_facts(self):
        indexer.scan(self.coll)
        n = self.count()
        os.remove(self.folder / "meeting.md")
        with mock.patch.object(indexer, "_settings", lambda: {"missing_grace_h": 0}):
            indexer.scan(self.coll)
            indexer.scan(self.coll)
        self.assertEqual(self.count(), n - 1)

    def test_backfill_for_an_existing_index_is_throttled_and_resumes(self):
        indexer.scan(self.coll)
        conn = store.connect(self.name)
        conn.execute("DELETE FROM doc_meta")                           # an index built before this feature existed
        conn.commit()
        conn.close()
        with mock.patch.object(indexer, "DOCMETA_BACKFILL", 2):
            s1 = indexer.scan(self.coll)
            self.assertEqual((s1["docmeta"], self.count()), (2, 2))
            s2 = indexer.scan(self.coll)
            self.assertEqual((s2["docmeta"], self.count()), (2, 4))
        s3 = indexer.scan(self.coll)                                   # the rest (7 files in all minus tool.py = 6)
        self.assertEqual(self.count(), 6)
        self.assertEqual(s3["docmeta"], 2)

    def test_the_night_run_takes_a_bigger_share(self):
        indexer.scan(self.coll)
        conn = store.connect(self.name)
        conn.execute("DELETE FROM doc_meta")
        conn.commit()
        conn.close()
        with mock.patch.object(indexer, "DOCMETA_BACKFILL", 1), mock.patch.object(indexer, "DOCMETA_BACKFILL_NIGHT", 100):
            indexer.scan(self.coll, night=True)
        self.assertEqual(self.count(), 6)

    def test_a_newer_extractor_version_is_redone_by_the_backfill(self):
        indexer.scan(self.coll)
        conn = store.connect(self.name)
        conn.execute("UPDATE doc_meta SET extractor_version = 0, amount = -1")
        conn.commit()
        conn.close()
        s = indexer.scan(self.coll)
        self.assertGreaterEqual(s["docmeta"], 6)
        self.assertEqual(self.meta("Rechnung_RE-2025-0042.txt")["amount"], 255.85)

    def test_text_from_ocr_that_arrived_after_the_row_is_picked_up(self):
        indexer.scan(self.coll)
        conn = store.connect(self.name)
        fid = conn.execute("SELECT id FROM files WHERE path='empty.txt'").fetchone()[0]
        store.add_items(conn, fid, "empty.txt", [{"modality": "ocr", "text": BY_ID["d07"]["text"]}])
        conn.execute("UPDATE files SET heavy_at=? WHERE id=?", (time.time() + 30, fid))
        conn.commit()
        conn.close()
        indexer.scan(self.coll)
        self.assertEqual(self.meta("empty.txt")["doctype"], "receipt")

    def test_a_file_waiting_for_ocr_is_not_classified_before_the_ocr_ran(self):
        indexer.scan(self.coll)
        conn = store.connect(self.name)
        fid = conn.execute("SELECT id FROM files WHERE path='meeting.md'").fetchone()[0]
        conn.execute("DELETE FROM doc_meta WHERE file_id=?", (fid,))
        conn.execute("UPDATE files SET heavy=? WHERE id=?", (json.dumps({"ocr": [1]}), fid))
        conn.commit()
        conn.close()
        indexer.scan(self.coll)
        self.assertIsNone(self.meta("meeting.md"))

    def test_a_classifier_bug_never_costs_the_scan(self):
        with mock.patch.object(docmeta, "analyse", side_effect=RuntimeError("boom")):
            s = indexer.scan(self.coll)
        self.assertEqual(s["errors"], 0)
        conn = store.connect(self.name, readonly=True)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 7)
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
        conn.close()
        conn = store.connect(self.name, readonly=True)       # the backfill leaves a stub (not classified) so it does not retry forever
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM doc_meta WHERE doctype IS NOT NULL").fetchone()[0], 0)
        conn.close()

    def test_a_backfill_bug_never_fails_the_scan(self):
        indexer.scan(self.coll)
        with mock.patch.object(docmeta, "backfill", side_effect=RuntimeError("boom")):
            s = indexer.scan(self.coll)
        self.assertEqual(s["errors"], 0)

    def test_switched_off_in_the_configuration(self):
        with mock.patch.object(docmeta, "settings", lambda: {"enabled": False, "own_names": (), "extra_org_suffixes": (),
                                                              "extra_keywords": {}}):
            indexer.scan(self.coll)
        self.assertEqual(self.count(), 0)

    def test_own_names_and_extra_forms_from_the_configuration_reach_the_hook(self):
        (self.folder / "dup.txt").write_text("Muster AG\nMusterweg 1\nRechnung Nr. 5\nRechnungsdatum 14.03.2025\nGesamtbetrag 10,00 EUR\n"
                                              "Zahlungsziel 14 Tage")
        with mock.patch.object(docmeta, "settings", lambda: {"enabled": True, "own_names": ("Muster AG",),
                                                              "extra_org_suffixes": (), "extra_keywords": {}}):
            indexer.scan(self.coll)
        self.assertNotEqual(self.meta("dup.txt")["counterparty"], "Muster AG")

    def test_reindex_redoes_everything_in_batches(self):
        indexer.scan(self.coll)
        conn = store.connect(self.name)
        conn.execute("UPDATE doc_meta SET amount = -1, extractor_version = ?", (docmeta.EXTRACTOR_VERSION,))
        conn.commit()
        conn.close()
        seen = []
        info = docmeta.reindex(self.name, progress=lambda d, t: seen.append((d, t)), batch=2)
        self.assertEqual(info["state"], "done")
        self.assertEqual(info["total"], 6)
        self.assertEqual(seen[-1], (6, 6))
        self.assertEqual(len(seen), 3)                                   # three batches of two
        self.assertEqual(self.meta("Rechnung_RE-2025-0042.txt")["amount"], 255.85)

    def test_reindex_can_be_cancelled_between_batches(self):
        indexer.scan(self.coll)
        calls = []
        info = docmeta.reindex(self.name, cancel=lambda: len(calls) >= 1 or calls.append(1), batch=2)
        self.assertEqual(info["state"], "cancelled")


# ====================================================================== search filters

class Filters(unittest.TestCase):
    """Eight documents with facts, found by words, by filters, and by filters alone."""

    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="mse-dm-search-"))
        cls.coll = _coll(cls.folder)
        cls.name = cls.coll["name"]
        conn = store.connect(cls.name)
        add = lambda rel, text, meta, **kw: add_doc(conn, cls.folder, cls.name, rel, text, meta, **kw)
        add("inv_acme_2025-03.pdf", "invoice widgets for Acme", M("invoice", "2025-03-14", 1320.0, "USD", "Acme Parts GmbH", "INV-1"))
        add("inv_acme_2025-09.pdf", "invoice services for Acme", M("invoice", "2025-09-02", 480.0, "EUR", "Acme Parts GmbH", "INV-2"))
        add("inv_muster_2024.pdf", "invoice transport Muster", M("invoice", "2024-07-03", 2356.2, "EUR", "Muster Logistik AG", "2024-0815"))
        add("rec_2025-03.pdf", "receipt coffee beans", M("receipt", "2025-03-14", 7.66, "EUR", "Cafe Beispiel"))
        add("rec_2025-05.pdf", "receipt printer paper", M("receipt", "2025-05-20", 84.0, "EUR", "Office Beispiel"))
        add("contract_2025.pdf", "contract for services widgets", M("contract", "2025-01-12", None, None, "Acme Parts GmbH"))
        add("payslip.pdf", "payslip june", M("payslip", "2025-06", 3050.0, "GBP", "Demo Consulting Ltd"))
        add("notes.md", "invoice notes widgets", M("other", None, None, None, None), kind="text")
        add("unclassified.pdf", "invoice widgets again", None)                  # no row at all
        add("stub.pdf", "invoice widgets stub", M(None, None))                  # looked at, not classified
        add("gone_invoice.pdf", "invoice widgets gone", M("invoice", "2025-04-01", 99.0, "EUR", "Acme Parts GmbH"), missing=True)
        conn.commit()
        conn.close()
        cfg = _cfg(cls.coll)                       # built before the patch: _cfg itself calls config.load
        cls.patch = mock.patch.object(config, "load", lambda: cfg)
        cls.patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()

    def names(self, q, **kw):
        res = search.search(q, collections=[self.name], mode="keyword", limit=kw.pop("limit", 50), **kw)
        return sorted(Path(r["path"]).name for r in res["results"]), res

    # -- the document type
    def test_type_filter_and_its_list_form(self):
        self.assertEqual(self.names("widgets type:invoice")[0], ["inv_acme_2025-03.pdf"])
        self.assertEqual(self.names("invoice type:receipt,invoice")[0],
                         ["inv_acme_2025-03.pdf", "inv_acme_2025-09.pdf", "inv_muster_2024.pdf"])
        self.assertEqual(self.names("paper doctype:receipt")[0], ["rec_2025-05.pdf"])
        self.assertEqual(self.names("coffee type:Quittung")[0], ["rec_2025-03.pdf"])        # German names work

    def test_type_other_is_still_the_file_kind_and_doctype_other_the_document_type(self):
        self.assertEqual(self.names("widgets type:pdf type:invoice")[0], ["inv_acme_2025-03.pdf"])
        self.assertNotIn("notes.md", self.names("widgets type:pdf")[0])
        self.assertEqual(self.names("widgets doctype:other")[0], ["notes.md"])
        self.assertEqual(self.names("widgets type:text")[0], ["notes.md"])

    def test_unclassified_and_stub_files_match_no_document_type(self):
        names = self.names("widgets type:invoice,receipt,contract,payslip,letter,quote")[0]
        self.assertNotIn("unclassified.pdf", names)
        self.assertNotIn("stub.pdf", names)
        self.assertIn("unclassified.pdf", self.names("widgets")[0])        # but a plain search still finds them

    def test_unknown_document_type_is_an_error(self):
        with self.assertRaises(ValueError):
            self.names("widgets doctype:spaceship")

    # -- amounts and currency
    def test_amount_comparisons_and_ranges(self):
        self.assertEqual(self.names("invoice amount:>1000")[0], ["inv_acme_2025-03.pdf", "inv_muster_2024.pdf"])
        self.assertEqual(self.names("invoice amount:<500")[0], ["inv_acme_2025-09.pdf"])
        self.assertEqual(self.names("invoice amount:480")[0], ["inv_acme_2025-09.pdf"])
        self.assertEqual(self.names("receipt amount:5..100")[0], ["rec_2025-03.pdf", "rec_2025-05.pdf"])
        self.assertEqual(self.names("receipt amount:>=84")[0], ["rec_2025-05.pdf"])
        self.assertEqual(self.names("invoice amount:>400 amount:<2000")[0], ["inv_acme_2025-03.pdf", "inv_acme_2025-09.pdf"])
        self.assertEqual(self.names("contract amount:>0")[0], [])        # no amount, no match

    def test_currency(self):
        self.assertEqual(self.names("invoice currency:USD")[0], ["inv_acme_2025-03.pdf"])
        self.assertEqual(self.names("invoice currency:eur")[0], ["inv_acme_2025-09.pdf", "inv_muster_2024.pdf"])
        self.assertEqual(self.names("invoice currency:USD,EUR")[0], ["inv_acme_2025-03.pdf", "inv_acme_2025-09.pdf", "inv_muster_2024.pdf"])
        self.assertEqual(self.names("payslip currency:£")[0], ["payslip.pdf"])
        with self.assertRaises(ValueError):
            self.names("invoice currency:bitcoins")

    def test_party_is_a_case_insensitive_substring(self):
        self.assertEqual(self.names("invoice party:acme")[0], ["inv_acme_2025-03.pdf", "inv_acme_2025-09.pdf"])
        self.assertEqual(self.names("invoice party:MUSTER")[0], ["inv_muster_2024.pdf"])
        self.assertEqual(self.names('invoice party:"parts gmbh"')[0], ["inv_acme_2025-03.pdf", "inv_acme_2025-09.pdf"])
        self.assertEqual(self.names("invoice party:acme,muster")[0],
                         ["inv_acme_2025-03.pdf", "inv_acme_2025-09.pdf", "inv_muster_2024.pdf"])
        self.assertEqual(self.names("coffee party:café")[0], ["rec_2025-03.pdf"])           # accents do not matter

    def test_document_date_year_month_day_and_ranges(self):
        self.assertEqual(self.names("invoice docdate:2025")[0], ["inv_acme_2025-03.pdf", "inv_acme_2025-09.pdf"])
        self.assertEqual(self.names("invoice docdate:2025-03")[0], ["inv_acme_2025-03.pdf"])
        self.assertEqual(self.names("receipt docdate:2025-03-14")[0], ["rec_2025-03.pdf"])
        self.assertEqual(self.names("widgets docdate:2025-01..2025-03")[0], ["contract_2025.pdf", "inv_acme_2025-03.pdf"])
        self.assertEqual(self.names("receipt docdate:2025-04..")[0], ["rec_2025-05.pdf"])
        self.assertEqual(self.names("invoice docdate:..2024-12")[0], ["inv_muster_2024.pdf"])
        self.assertEqual(self.names("payslip docdate:2025-06")[0], ["payslip.pdf"])         # a month-only date
        self.assertEqual(self.names("invoice docdate:2024,2025-09")[0], ["inv_acme_2025-09.pdf", "inv_muster_2024.pdf"])
        for bad in ("docdate:march", "docdate:2025-13", "docdate:2025-06..2025-01"):
            with self.assertRaises(ValueError, msg=bad):
                self.names("invoice " + bad)

    def test_the_file_date_is_not_the_document_date(self):
        # every file was "saved" 2025-12-31; the document dates are what docdate looks at, after: still looks at the file
        self.assertEqual(self.names("invoice docdate:2024")[0], ["inv_muster_2024.pdf"])
        self.assertIn("inv_muster_2024.pdf", self.names("invoice after:2025-12-01")[0])

    def test_combinations(self):
        self.assertEqual(self.names("widgets type:invoice amount:>1000 currency:USD party:acme docdate:2025")[0],
                         ["inv_acme_2025-03.pdf"])
        self.assertEqual(self.names("widgets type:invoice amount:>1000 currency:EUR party:acme")[0], [])

    def test_a_file_marked_missing_never_matches(self):
        self.assertNotIn("gone_invoice.pdf", self.names("widgets type:invoice")[0])
        self.assertNotIn("gone_invoice.pdf", self.names("", filters={"type": ["invoice"]})[0])

    # -- filters without words
    def test_filters_alone_list_the_matching_documents_newest_first(self):
        names, res = self.names("type:invoice")
        self.assertEqual(names, ["inv_acme_2025-03.pdf", "inv_acme_2025-09.pdf", "inv_muster_2024.pdf"])
        self.assertTrue(res["browse"])
        self.assertEqual(res["total"], 3)
        self.assertEqual([Path(r["path"]).name for r in res["results"]],
                         ["inv_acme_2025-09.pdf", "inv_acme_2025-03.pdf", "inv_muster_2024.pdf"])
        self.assertTrue(all(r["relevance"] >= 99 for r in res["results"]))      # alike: the floor never drops older ones
        self.assertEqual(res["facets"]["doctype"], {"invoice": 3})

    def test_total_counts_every_match_not_only_the_page(self):
        res = search.search("type:invoice", collections=[self.name], mode="keyword", limit=1)
        self.assertEqual((len(res["results"]), res["total"]), (1, 3))

    def test_an_empty_query_without_a_document_filter_is_as_before(self):
        res = search.search("", collections=[self.name], mode="keyword")
        self.assertEqual((res["results"], res["browse"]), ([], False))
        res = search.search("type:pdf", collections=[self.name], mode="keyword")      # a file-kind filter alone: also as before
        self.assertEqual((res["results"], res["browse"]), ([], False))

    def test_a_vector_search_never_browses(self):
        import numpy as np
        res = search.search("", collections=[self.name], mode="semantic", qvec=np.ones(store.DIMS, dtype=np.float32),
                            filters={"type": ["invoice"]})
        self.assertFalse(res["browse"])

    # -- plain language
    def test_plain_language_amounts_dates_and_types(self):
        q = "invoices over 500 EUR in 2024"
        text, filters, _ = search.parse(q)
        self.assertEqual((text, filters), ("", {"amount": [">500"], "currency": ["EUR"], "type": ["invoice"], "docdate": ["2024"]}))
        self.assertEqual(self.names(q)[0], ["inv_muster_2024.pdf"])
        self.assertEqual(self.names("receipts under 50 EUR")[0], ["rec_2025-03.pdf"])
        self.assertEqual(self.names("Rechnungen über 1000 $ im Jahr 2025")[0], ["inv_acme_2025-03.pdf"])
        # words besides the filters: not mapped (it would be a question, as Ask sends them), searched as words as before
        self.assertEqual(search.parse("invoices from Acme over 400 EUR")[1], {})
        self.assertEqual(search.parse("Wann wurde die Rechnung über 500 Euro bezahlt?")[1], {})
        self.assertEqual(search.parse("How much was the invoice over 500 EUR in 2025")[1],       # only the old date reading remains
                         {"after": ["2025-01-01"], "before": ["2026-01-01"]})
        self.assertEqual(search.parse("show me the invoices over 500 EUR in 2025")[1],
                         {"amount": [">500"], "currency": ["EUR"], "type": ["invoice"], "docdate": ["2025"]})
        self.assertEqual(self.names("type:invoice party:acme over 400 EUR")[0], ["inv_acme_2025-09.pdf"])   # typed filters and a plain amount combine

    def test_dates_stay_file_dates_unless_a_document_filter_is_on(self):
        self.assertEqual(search.parse("invoices from March 2025")[1:], ({"after": ["2025-03-01"], "before": ["2025-04-01"]}, []))
        self.assertEqual(search.parse("notes in 2025")[1], {"after": ["2025-01-01"], "before": ["2026-01-01"]})
        self.assertEqual(search.parse("type:invoice from March 2025")[1], {"type": ["invoice"], "docdate": ["2025-03"]})
        self.assertEqual(search.parse("type:pdf budget from March 2025")[1],
                         {"type": ["pdf"], "after": ["2025-03-01"], "before": ["2025-04-01"]})
        self.assertEqual(search.parse("type:invoice 2025")[0], "2025")            # a lone year is content, as before

    def test_queries_that_must_not_change(self):
        for q in ("temperature over 40 degrees", "budget over 500", "something at least 5 pages", "rent over 2025", "Rechnung 119,00 EUR",
                  "budget Q3 over 1000 EUR", "when was the invoice over 500 EUR paid"):
            text, filters, phrases = search.parse(q)
            self.assertEqual((text, filters), (q, {}), q)

    # -- results carry the facts
    def test_every_result_carries_meta(self):
        _, res = self.names("widgets")
        by = {Path(r["path"]).name: r for r in res["results"]}
        self.assertEqual(by["inv_acme_2025-03.pdf"]["meta"]["amount"], 1320.0)
        self.assertEqual(by["inv_acme_2025-03.pdf"]["meta"]["currency"], "USD")
        self.assertEqual(by["inv_acme_2025-03.pdf"]["meta"]["counterparty"], "Acme Parts GmbH")
        self.assertEqual(by["inv_acme_2025-03.pdf"]["meta"]["doc_date"], "2025-03-14")
        self.assertEqual(by["inv_acme_2025-03.pdf"]["meta"]["doctype"], "invoice")
        self.assertIsNone(by["unclassified.pdf"]["meta"])
        self.assertIsNone(by["stub.pdf"]["meta"]["doctype"])
        self.assertIn("doctype", res["facets"])

    def test_get_document_carries_meta_too(self):
        d = search.get_document(self.name, "inv_acme_2025-03.pdf")
        self.assertEqual(d["meta"]["doctype"], "invoice")

    def test_the_file_filter_of_a_row_without_the_columns_cannot_be_proven(self):
        ok, active = search.file_filter({"type": ["invoice"]})
        self.assertTrue(active and ok.needs_meta)
        row = {"path": "a.pdf", "kind": "pdf", "ext": "pdf", "doc_date": "2025-01-01"}      # as the favourites code loads it
        self.assertFalse(ok(row))
        self.assertTrue(search.file_filter({"type": ["pdf"]})[0](row))


# ====================================================================== facets and the timeline

class Views(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="mse-dm-views-"))
        cls.coll = _coll(cls.folder)
        cls.name = cls.coll["name"]
        conn = store.connect(cls.name)
        n = 0

        def add(doctype, date_, amount=None, cur=None, party=None, **kw):
            nonlocal n
            n += 1
            add_doc(conn, cls.folder, cls.name, f"f{n}.pdf", f"text {n}", M(doctype, date_, amount, cur, party), **kw)

        add("receipt", "2025-01-10", 10.0, "EUR", "Cafe Beispiel")
        add("receipt", "2025-01-20", 5.5, "EUR", "Cafe Beispiel")
        add("receipt", "2025-03-02", 20.0, "EUR", "Office Beispiel")
        add("receipt", "2025-03-15", 12.0, "USD", "Office Beispiel")
        add("receipt", "2025-06-30", None, None, None)
        add("receipt", None, 9.0, "EUR", "Cafe Beispiel")                  # no document date
        add("invoice", "2025-02-01", 100.0, "EUR", "acme parts gmbh")
        add("invoice", "2025-02-11", 50.0, "EUR", "Acme Parts GmbH")
        add("invoice", "2024-12-31", 70.0, "EUR", "Muster AG")
        add("receipt", "2025-04-01", 1.0, "EUR", "Gone Cafe", missing=True)  # a missing file counts nowhere
        add_doc(conn, cls.folder, cls.name, "plain.pdf", "no facts")
        add_doc(conn, cls.folder, cls.name, "stub.pdf", "stub", M(None))
        conn.commit()
        conn.close()

    def test_facets(self):
        f = docmeta.facets([self.name])
        self.assertEqual(f["files"], 11)                                   # the file marked missing is not counted
        self.assertEqual(f["classified"], 9)
        self.assertEqual(f["unclassified"], 2)
        by = {r["doctype"]: r["count"] for r in f["doctypes"]}
        self.assertEqual(by, {"receipt": 6, "invoice": 3})
        parties = {r["name"].lower(): r["count"] for r in f["counterparties"]}
        self.assertEqual(parties["acme parts gmbh"], 2)                    # one counterparty, however it is spelled
        self.assertEqual(parties["cafe beispiel"], 3)
        self.assertEqual(f["undated"], 1)
        months = {r["month"]: r["count"] for r in f["months"]}
        self.assertEqual(months, {"2024-12": 1, "2025-01": 2, "2025-02": 2, "2025-03": 2, "2025-06": 1})
        self.assertEqual({r["currency"]: r["count"] for r in f["currencies"]}, {"EUR": 7, "USD": 1})

    def test_facets_for_one_type(self):
        f = docmeta.facets([self.name], "invoice")
        self.assertEqual([r["doctype"] for r in f["doctypes"]], ["invoice"])
        self.assertEqual(sum(r["count"] for r in f["months"]), 3)
        with self.assertRaises(ValueError):
            docmeta.facets([self.name], "spaceship")

    def test_timeline_has_the_months_and_the_gaps(self):
        t = docmeta.timeline([self.name], "receipt")
        self.assertEqual((t["from"], t["to"]), ("2025-01", "2025-06"))
        self.assertEqual([b["month"] for b in t["buckets"]], ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"])
        self.assertEqual([b["count"] for b in t["buckets"]], [2, 0, 2, 0, 0, 1])
        self.assertEqual(t["gaps"], ["2025-02", "2025-04", "2025-05"])
        self.assertEqual(t["buckets"][0]["amounts"], {"EUR": 15.5})
        self.assertEqual(t["buckets"][2]["amounts"], {"EUR": 20.0, "USD": 12.0})
        self.assertEqual(t["buckets"][5]["amounts"], {})
        self.assertEqual(t["buckets"][5]["with_amount"], 0)
        self.assertEqual(t["undated"], 1)                                   # counted on its own, never placed on a month
        self.assertEqual(t["total"], 5)
        self.assertEqual(t["buckets"][0]["query"], "type:receipt docdate:2025-01")
        self.assertEqual(t["types"], ["receipt"])

    def test_timeline_range_and_all_types(self):
        t = docmeta.timeline([self.name], None, "2024-11", "2025-02")
        self.assertEqual([b["month"] for b in t["buckets"]], ["2024-11", "2024-12", "2025-01", "2025-02"])
        self.assertEqual(t["gaps"], ["2024-11"])
        t = docmeta.timeline([self.name], ["invoice", "receipt"], "2025", "2025")        # years mean their first and last month
        self.assertEqual((t["from"], t["to"]), ("2025-01", "2025-12"))
        self.assertEqual(len(t["gaps"]), 12 - 4)
        t = docmeta.timeline([self.name], "contract")
        self.assertEqual((t["buckets"], t["gaps"], t["total"]), ([], [], 0))
        for bad in ("2025-13", "march", "25"):
            with self.assertRaises(ValueError, msg=bad):
                docmeta.timeline([self.name], "receipt", bad, None)
        with self.assertRaises(ValueError):
            docmeta.timeline([self.name], "receipt", "1800", "2099")                    # absurd range

    def test_views_of_a_collection_that_does_not_exist_are_empty(self):
        self.assertEqual(docmeta.facets(["nope" + uuid.uuid4().hex])["classified"], 0)
        self.assertEqual(docmeta.timeline(["nope" + uuid.uuid4().hex])["buckets"], [])


if __name__ == "__main__":
    unittest.main()
