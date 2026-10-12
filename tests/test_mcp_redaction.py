"""Masking of the new MCP output: the document facts of a result (party, reference), the title in a citation, the parties of
`facets`, and the names and queries of saved searches. With `agents.redact` on a raw IBAN, card number or ID-shaped value must
appear nowhere in what an agent receives; with it off every value passes through unchanged. Fictional values only (the
published example IBAN, a clearly fake ID).
Run: .venv/bin/python -m unittest -v tests.test_mcp_redaction"""

import os
import tempfile
import unittest
import uuid
from pathlib import Path

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import config, mcp  # noqa: E402

IBAN = "DE89370400440532013000"
IBAN_SPACED = "DE89 3704 0044 0532 0130 00"
ID = "X1234567Y"
CARD = "4111 1111 1111 1111"            # a published test card number (Luhn-valid)


class Switch:
    """config agents.redact for a test, restored afterwards."""

    @staticmethod
    def set(test, on: bool):
        was = config.load()["agents"]["redact"]
        config.update_settings({"agents": {"redact": on}})
        test.addCleanup(config.update_settings, {"agents": {"redact": was}})


def result(**kw):
    r = {"collection": "c1", "path": "Docs/file.pdf", "name": "file.pdf", "kind": "pdf", "title": "Plain title", "docid": "abcd1234",
         "date": "2025-03-14", "pages": 2, "metadata_only": False, "why": ["keyword"],
         "hits": [{"modality": "text", "page": 2, "t0": None, "t1": None, "snippet": "match"}], "copies": [], "context": None,
         "favorite": None, "meta": None}
    r.update(kw)
    return r


def render(*results):
    return mcp.format_results({"query": "q", "collections": ["c1"], "results": list(results), "bridge": {}})


RAW = (IBAN, IBAN_SPACED, ID, CARD, "1234567")


class Guard(unittest.TestCase):
    def test_masked_when_on(self):
        for value in (IBAN, IBAN_SPACED, IBAN.lower(), "de89 3704 0044 0532 0130 00", "DE89-3704-0044-0532-0130-00", ID, ID.lower(),
                      CARD, "4111-1111-1111-1111", "4111111111111111", "ref 123456789", f"Pay {IBAN} now", f"{ID} GmbH"):
            out = mcp._guard(value)
            for raw in (IBAN, IBAN_SPACED, ID, ID.lower(), CARD, "4111111111111111", "123456789", "3704 0044 0532"):
                self.assertNotIn(raw, out, (value, out))
            self.assertNotEqual(out, value)

    def test_ordinary_text_is_kept(self):
        for value in ("Acme Parts GmbH", "Müller & Söhne", "O'Brien & Sons", "INV-2025-0042", "2025-03-14", "Cafe Beispiel 24h",
                      "12 Main Street", "Invoice March 2025", "EUR", "Q3 report (final) v2", "a b", "RE-12345"):
            self.assertEqual(mcp._guard(value), value, value)
        self.assertIsNone(mcp._guard(None))
        self.assertEqual(mcp._guard(""), "")
        self.assertEqual(mcp._guard(12), "12")

    def test_a_reference_the_extractor_would_not_store_is_masked_whole(self):
        for value in ("A1", "no digits here", IBAN, CARD, "2025", "x" * 40):
            self.assertEqual(mcp._guard(value, reference=True), "[redacted]", value)
        self.assertEqual(mcp._guard("INV-2025-0042", reference=True), "INV-2025-0042")
        self.assertEqual(mcp._guard(ID, reference=True), "[id]")                 # safe as a reference, but ID-shaped

    def test_unchanged_when_off(self):
        Switch.set(self, False)
        for value in (IBAN, IBAN_SPACED, ID, CARD, "x " + IBAN, "A1", "no digits"):
            self.assertEqual(mcp._guard(value), value)
            self.assertEqual(mcp._guard(value, reference=True), value)


class Results(unittest.TestCase):
    META = {"doctype": "invoice", "doc_date": "2025-03-14", "amount": 12.5, "currency": "EUR"}

    def proof(self, text):
        for raw in RAW:
            self.assertNotIn(raw, text)

    def test_party_reference_and_title_never_show_a_raw_identifier(self):
        r1 = result(meta={**self.META, "counterparty": IBAN, "reference": ID}, title=f"Invoice {ID}")
        r2 = result(docid="bbbb1111", meta={**self.META, "counterparty": f"{ID} GmbH", "reference": IBAN}, title=f"Pay {IBAN_SPACED}")
        r3 = result(docid="cccc2222", meta={**self.META, "counterparty": CARD, "reference": CARD}, title=None, name=f"{ID}.pdf")
        text = render(r1, r2, r3)
        self.proof(text)
        self.assertIn("   meta: invoice · 2025-03-14 · 12.50 EUR", text)           # the line is there, only the values are masked
        self.assertIn("   cite: Invoice [id] p.2", text)

    def test_a_metadata_only_citation_is_built_from_the_name_never_the_title(self):
        r = result(metadata_only=True, title=f"Identity of {ID}", name="scan_1234567890.pdf", path="Identity/scan_1234567890.pdf")
        text = render(r)
        self.assertNotIn("Identity of", text)
        self.assertNotIn("1234567890", text)
        self.assertIn("cite: scan_[id].pdf p.2", text)

    def test_get_cite_title_is_masked(self):
        d = {"path": "Reports/q3.pdf", "docid": "abcd1234", "collection": "c1", "kind": "pdf", "title": f"Report {ID}",
             "text": "body", "truncated": False}
        text = mcp.format_doc(d, page=1)
        self.assertNotIn(ID, text)
        self.assertIn("cite: Report [id] p.1", text)

    def test_unchanged_when_off(self):
        Switch.set(self, False)
        r = result(meta={**self.META, "counterparty": IBAN, "reference": ID}, title=f"Invoice {ID}")
        text = render(r)
        self.assertIn(f"party {IBAN} · ref {ID}", text)
        self.assertIn(f"cite: Invoice {ID} p.2", text)
        d = {"path": "r.pdf", "docid": "abcd1234", "collection": "c1", "kind": "pdf", "title": f"Report {ID}", "text": "body"}
        self.assertIn(f"cite: Report {ID} p.1", mcp.format_doc(d, page=1))


class Facets(unittest.TestCase):
    def test_parties_are_masked_in_facets_and_timeline(self):
        res = {"collections": ["c1"], "files": 3, "classified": 3, "unclassified": 0, "undated": 0,
               "doctypes": [{"doctype": "invoice", "count": 3}],
               "counterparties": [{"name": IBAN, "count": 2}, {"name": f"{ID} GmbH", "count": 1}, {"name": "Acme Parts GmbH", "count": 1}],
               "months": [{"month": "2025-03", "count": 3}], "currencies": [{"currency": "EUR", "count": 3}]}
        text = mcp.format_facets(res)
        for raw in RAW:
            self.assertNotIn(raw, text)
        self.assertIn("Acme Parts GmbH (1)", text)
        Switch.set(self, False)
        text = mcp.format_facets(res)
        self.assertIn(f"{IBAN} (2)", text)
        self.assertIn(f"{ID} GmbH (1)", text)


class Resources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="mse-mcp-red-"))
        cls.name = "mr" + uuid.uuid4().hex[:8]
        config.add_collection(str(cls.folder), cls.name)
        cls.saved = config.add_saved(f"Pay {IBAN} {ID}", f"invoice {IBAN} {ID}", cls.name)

    @classmethod
    def tearDownClass(cls):
        config.remove_saved(cls.saved["id"])
        config.remove_collection(cls.name)

    class Client:
        def search(self, q, colls, limit, filters, mode, precise=False):
            return {"query": q, "collections": colls or [], "results": [], "bridge": {}}

    def test_saved_search_name_and_query_are_masked_in_the_list_and_in_the_read(self):
        listed = [r for r in mcp.list_resources(None) if r["uri"].endswith(self.saved["id"])]
        self.assertEqual(len(listed), 1)
        blob = repr(listed)
        for raw in RAW:
            self.assertNotIn(raw, blob)
        out = mcp.read_resource(mcp.URI_SAVED + self.saved["id"], self.Client(), None, None)["contents"][0]["text"]
        for raw in (IBAN, ID):
            self.assertNotIn(raw, out)
        self.assertIn("# Saved search: Pay", out)
        Switch.set(self, False)
        listed = [r for r in mcp.list_resources(None) if r["uri"].endswith(self.saved["id"])]
        self.assertIn(IBAN, repr(listed))
        out = mcp.read_resource(mcp.URI_SAVED + self.saved["id"], self.Client(), None, None)["contents"][0]["text"]
        self.assertIn(f"query: invoice {IBAN} {ID}", out)


if __name__ == "__main__":
    unittest.main()
