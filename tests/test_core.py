"""Unit tests for the parts that must never silently break. Run: .venv/bin/python -m unittest -v"""

import os
import tempfile
import unittest
from datetime import date

os.environ["MSE_HOME"] = tempfile.mkdtemp(prefix="mse-test-")

import numpy as np  # noqa: E402

from mse import config, store  # noqa: E402
from mse.extract import chunk_text, markdown_sections  # noqa: E402
from mse.ignore import DEFAULT_EXCLUDES, SECRET_PATTERNS, Matcher  # noqa: E402
from mse.redact import redact, redact_path  # noqa: E402
from mse.search import date_phrases, file_filter, group_key, parse  # noqa: E402
from mse.textutil import detect_lang, fts_query, snippet  # noqa: E402


# A folder preset written out for these tests (a stand-in; a user's own presets live in their own data folder).
ARCHIVE_PRESET = {
    "exclude": ["_staging/", "_tmp/", "tools/", "scripts/", "*.zip"],
    "metadata_only": ["01_Payroll/", "03_Contracts/", "_extracted/archive/",
                      "_extracted/documents/", "NOTES.md"],
}


class PrivacyRules(unittest.TestCase):
    def setUp(self):
        self.ex = Matcher(SECRET_PATTERNS + DEFAULT_EXCLUDES + ARCHIVE_PRESET["exclude"])
        self.meta = Matcher(ARCHIVE_PRESET["metadata_only"])

    def test_credentials_never_indexed(self):
        for p in ("Credentials_Store/ssh/id_ed25519", "_extracted/recovery-codes/codes.txt",
                  "recovery-codes-20250301.zip", "a/.env", "certs/server.key", "x/ACME_Credentials/c.pfx",
                  ".claude/settings.json"):
            self.assertTrue(self.ex.match(p), p)

    def test_metadata_only_is_name_only(self):
        for p in ("01_Payroll/payslip.pdf", "03_Contracts/2023/form.pdf", "NOTES.md"):
            self.assertFalse(self.ex.match(p), p)
            self.assertTrue(self.meta.match(p), p)

    def test_normal_documents_indexed(self):
        for p in ("02_Projects/STATUS.md", "06_Payroll/payslip_2025-01.pdf", "00_Notes/minutes.md"):
            self.assertFalse(self.ex.match(p) or self.meta.match(p), p)


class Chunking(unittest.TestCase):
    def test_no_duplicated_text_inside_a_chunk(self):
        sentences = [f"Sentence number {i} talks about invoice {i * 7} and payment terms." for i in range(400)]
        text = " ".join(sentences)
        for c in chunk_text(text):
            self.assertLessEqual(len(c), 1700)
            for s in sentences:
                self.assertLessEqual(c.count(s), 1, "a sentence repeats inside one chunk")

    def test_markdown_headings_become_context(self):
        secs = markdown_sections("# Title\nintro\n## Costs\nsupplier 250,00 EUR\n### Office\nfee 100,00 EUR")
        self.assertEqual(secs[-1]["heading"], "Title › Costs › Office")


class Text(unittest.TestCase):
    def test_snippet_strips_markdown_and_marks_terms(self):
        s = snippet("| **Bürobedarf** | 18,90 EUR | `Acme Supplies` |", ["burobedarf"])
        self.assertIn("<mark>Bürobedarf</mark>", s)
        self.assertNotIn("**", s)
        self.assertNotIn("|", s)

    def test_fts_query_folds_umlauts_and_prefixes(self):
        self.assertEqual(fts_query(["Bürobedarf"]), '"burobedarf"*')

    def test_language(self):
        self.assertEqual(detect_lang("Die Rechnung des Lieferanten ist nächste Woche fällig"), "de")
        self.assertEqual(detect_lang("The invoice for the company is due next week"), "en")


class Dates(unittest.TestCase):
    T = date(2025, 3, 19)

    def test_month_names(self):
        self.assertEqual(date_phrases("invoices from February", self.T), ("invoices", "2025-02-01", "2025-03-01"))
        self.assertEqual(date_phrases("Rechnungen im Dezember", self.T)[1:], ("2024-12-01", "2025-01-01"))
        self.assertEqual(date_phrases("notes march 2025", self.T)[1:], ("2025-03-01", "2025-04-01"))

    def test_relative(self):
        self.assertEqual(date_phrases("receipts last month", self.T), ("receipts", "2025-02-01", "2025-03-01"))
        self.assertEqual(date_phrases("Belege letzte Woche", self.T)[1:], ("2025-03-10", "2025-03-17"))

    def test_date_alone_is_content(self):
        self.assertEqual(date_phrases("September", self.T), ("September", None, None))

    def test_parse_sets_filters(self):
        text, filters, _ = parse("Acme invoice last month type:pdf")
        self.assertEqual(text, "Acme invoice")
        self.assertEqual(filters["type"], ["pdf"])
        self.assertIn("after", filters)


class IncludeExclude(unittest.TestCase):
    def test_extension_shorthand(self):
        ok, _ = file_filter({"exclude": [".json, .md"]})
        row = lambda p: {"path": p, "kind": "text", "ext": p.rsplit(".", 1)[-1], "doc_date": None}
        self.assertFalse(ok(row("a/notes.md")))
        self.assertFalse(ok(row("data/x.json")))
        self.assertTrue(ok(row("a/invoice.pdf")))
        ok2, _ = file_filter({"include": ["pdf"]})
        self.assertTrue(ok2(row("a/invoice.pdf")))
        self.assertFalse(ok2(row("a/notes.md")))

    def test_globs(self):
        ok, active = file_filter({"include": ["*.jpg, *.png"], "exclude": ["backups/"]})
        row = lambda p: {"path": p, "kind": "image", "ext": p.rsplit(".", 1)[-1], "doc_date": "2025-03-01"}
        self.assertTrue(active)
        self.assertTrue(ok(row("a/b/photo.jpg")))
        self.assertFalse(ok(row("a/notes.md")))
        self.assertFalse(ok(row("backups/photo.jpg")))


class Redaction(unittest.TestCase):
    def test_personal_identifiers_masked(self):
        self.assertNotIn("3704", redact("IBAN DE89 3704 0044 0532 0130 00"))
        self.assertIn("[redacted]", redact("Passport No.: AB1234567"))
        self.assertIn("[redacted]", redact("Aufenthaltstitel Nr. Y12A3B4C5"))
        self.assertIn("[redacted]", redact("UAE ID 784-1970-1234567-1"))
        self.assertIn("[redacted]", redact("Geburtsdatum: 01.01.1970"))
        self.assertEqual(redact_path("01_Records/Passport_AB1234567.pdf"), "01_Records/Passport_[id].pdf")

    def test_company_identifiers_kept(self):
        for t in ("Reg 123456 B", "Steuer-Nr. 12/345/67890", "USt-IdNr. DE123456789", "Invoice 1234567001"):
            self.assertEqual(redact(t), t)


class Grouping(unittest.TestCase):
    def test_versions_of_one_document(self):
        a = group_key("02_Mail/2025-03-04_Acme_Office_Receipt_0000-0001_10EUR.md", None)
        b = group_key("02_Mail/originals/2025-03-04_Acme_Office_Receipt_0000-0001_10EUR.eml", None)
        c = group_key("x/2025-06-17_Acme_Supplies_Lieferantenrechnung_Buerobedarf_18.90EUR.pdf.digest.md", None)
        d = group_key("y/2025-06-17_Acme_Supplies_Lieferantenrechnung_Buerobedarf_18.90EUR.pdf", None)
        self.assertEqual(a, b)
        self.assertEqual(c, d)

    def test_generic_names_not_merged(self):
        self.assertNotEqual(group_key("a/SUMMARY.md", "h1"), group_key("b/SUMMARY.md", "h2"))


class Store(unittest.TestCase):
    def test_roundtrip(self):
        conn = store.connect("unit")
        fid = conn.execute("INSERT INTO files(path, name) VALUES ('a/x.md', 'x.md')").lastrowid
        store.add_items(conn, fid, "a x md", [{"modality": "text", "text": "Lieferantenrechnung Bürobedarf"},
                                               {"modality": "text", "text": "supplier invoice"}])
        ids = [r[0] for r in conn.execute("SELECT id FROM items ORDER BY id")]
        v = np.random.default_rng(1).normal(size=(2, 768)).astype(np.float32)
        store.store_vectors(conn, [(ids[0], "text", v[0]), (ids[1], "text", v[1])])
        hits = conn.execute("SELECT rowid FROM fts WHERE fts MATCH '\"burobedarf\"*'").fetchall()
        self.assertEqual([h[0] for h in hits], [ids[0]])
        knn = conn.execute("SELECT item_id FROM vbin WHERE embedding MATCH vec_bit(?) AND k = 1 AND modality = 'text'",
                           (store.to_bits(v[1]),)).fetchall()
        self.assertEqual(knn[0][0], ids[1])
        store.delete_file(conn, fid)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0], 0)



class HighlightAlignment(unittest.TestCase):
    def test_marks_land_on_the_word_after_eszett(self):
        s = snippet("Mit freundlichen Grüßen Ihr Acme-Team, invoice für order", ["acme", "invoice"])
        self.assertIn("<mark>Acme</mark>", s)
        self.assertIn("<mark>invoice</mark>", s)


if __name__ == "__main__":
    unittest.main()
