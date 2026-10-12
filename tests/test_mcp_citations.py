"""Citations and document facts in the MCP text: the `cite:` and `meta:` lines of search results, and `get` with a page.
The old lines stay exactly as they were; new lines are only added. Fictional documents only.
Run: .venv/bin/python -m unittest -v tests.test_mcp_citations"""

import copy
import os
import tempfile
import unittest

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import mcp  # noqa: E402


def hit(page=None, t0=None, snippet="text of the match", modality="text"):
    return {"modality": modality, "page": page, "t0": t0, "t1": None if t0 is None else t0 + 5, "snippet": snippet}


def result(path="Invoices/acme.pdf", **kw):
    r = {"collection": "c1", "path": path, "name": path.rsplit("/", 1)[-1], "kind": "pdf", "title": "Acme invoice March",
         "docid": "abcd1234", "date": "2025-03-14", "pages": 3, "metadata_only": False, "why": ["keyword"], "hits": [],
         "copies": [], "context": None, "favorite": None, "meta": None}
    r.update(kw)
    return r


def render(*results, **kw):
    return mcp.format_results({"query": "q", "collections": ["c1"], "results": list(results), "bridge": {}, **kw})


class FakeClient:
    def __init__(self, doc=None):
        self.doc, self.calls = doc, []

    def get(self, coll, ref, max_chars, page):
        self.calls.append((coll, ref, max_chars, page))
        return copy.deepcopy(self.doc) if self.doc else None


def doc(**kw):
    d = {"path": "Reports/q3.pdf", "docid": "abcd1234", "collection": "c1", "kind": "pdf", "doc_date": "2025-09-30",
         "title": "Quarterly report", "name": "q3.pdf", "pages": 4, "text": "Page two text.", "truncated": False,
         "metadata_only": False}
    d.update(kw)
    return d


class Cite(unittest.TestCase):
    def test_a_paged_hit_has_a_page_and_a_cite(self):
        r = result(hits=[hit(page=2), hit(page=3)])
        self.assertEqual(mcp.result_cite(r), (2, "Acme invoice March p.2"))
        self.assertIn("\n   cite: Acme invoice March p.2\n", render(r))

    def test_the_first_hit_with_a_page_wins_even_after_hits_without_one(self):
        r = result(hits=[hit(), hit(page=7), hit(page=2)])
        self.assertEqual(mcp.result_cite(r)[0], 7)

    def test_a_media_hit_cites_the_time(self):
        r = result("Media/call.m4a", kind="audio", title="Supplier call", pages=None, hits=[hit(t0=75.0, modality="transcript")])
        self.assertEqual(mcp.result_cite(r), (None, "Supplier call @ 1:15"))
        self.assertIn("   cite: Supplier call @ 1:15", render(r))

    def test_time_is_h_mm_ss_beyond_one_hour(self):
        for seconds, shown in ((0, "0:00"), (5, "0:05"), (59.9, "0:59"), (60, "1:00"), (3599, "59:59"), (3600, "1:00:00"),
                               (3661, "1:01:01"), (45296, "12:34:56")):
            self.assertEqual(mcp._cite_time(seconds), shown)
        r = result("Media/long.m4a", kind="audio", pages=None, hits=[hit(t0=4000.0)])
        self.assertEqual(mcp.result_cite(r)[1], "Acme invoice March @ 1:06:40")
        # the existing hit lines keep their own m:ss
        self.assertIn("66:40–66:45", render(result(hits=[hit(t0=4000.0)])))

    def test_a_page_beats_a_time(self):
        r = result(hits=[hit(t0=10.0), hit(page=4)])
        self.assertEqual(mcp.result_cite(r), (4, "Acme invoice March p.4"))

    def test_a_hit_without_page_or_time_has_neither(self):
        for hits in ([], [hit()], [hit(page=0)], [hit(snippet=None, modality="image")]):
            r = result(hits=hits)
            self.assertEqual(mcp.result_cite(r), (None, None), hits)
            self.assertNotIn("cite:", render(r))

    def test_the_title_falls_back_to_the_name_and_then_the_path(self):
        self.assertEqual(mcp.result_cite(result(title=None, hits=[hit(page=1)]))[1], "acme.pdf p.1")
        self.assertEqual(mcp.result_cite(result(title="", hits=[hit(page=1)]))[1], "acme.pdf p.1")
        bare = {"collection": "c1", "path": "x/y/plain.md", "docid": "aaaa0000", "kind": "text", "why": [], "hits": [hit(page=2)]}
        self.assertEqual(mcp.result_cite(bare)[1], "plain.md p.2")                        # results from a minimal backend

    def test_a_metadata_only_file_never_shows_its_title(self):
        r = result("Identity/scan_1234567890.pdf", metadata_only=True, title="Passport of Jane Example",
                   name="scan_1234567890.pdf", hits=[hit(page=1)])
        text = render(r)
        self.assertNotIn("Jane Example", text)
        self.assertIn("cite: scan_[id].pdf p.1", text)
        # with masking off the name is shown as it is, still not the title
        with _redaction(False):
            text = render(r)
        self.assertNotIn("Jane Example", text)
        self.assertIn("cite: scan_1234567890.pdf p.1", text)

    def test_old_lines_are_unchanged_and_new_ones_come_from_the_cite_and_meta_only(self):
        r1 = result(hits=[hit(page=2, snippet="alpha **beta**")], meta={"doctype": "invoice", "doc_date": "2025-03-14", "amount": 12.5,
                                                                      "currency": "EUR", "counterparty": "Acme", "reference": "INV-1"})
        r2 = result("Notes/n.md", kind="text", docid="bbbb1111", hits=[hit(snippet="plain")])
        new = render(r1, r2).split("\n")
        old = [l for l in new if not l.startswith(("   cite:", "   meta:"))]
        self.assertEqual(len(new) - len(old), 2)
        self.assertEqual(old[0], '2 results for "q" in c1')
        self.assertEqual(old[2], "1. [c1] Invoices/acme.pdf  #abcd1234  (pdf, 2025-03-14, 3 pages; matched: keyword)")
        self.assertEqual(old[3], "   p.2 alpha **beta**")


class Meta(unittest.TestCase):
    def test_the_meta_line(self):
        meta = {"doctype": "invoice", "confidence": 0.9, "doc_date": "2025-03-14", "amount": 1234.0, "currency": "EUR",
                "counterparty": "Acme Parts GmbH", "reference": "INV-2025-0042"}
        text = render(result(meta=meta, hits=[hit(page=1)]))
        self.assertIn("   meta: invoice · 2025-03-14 · 1234.00 EUR · party Acme Parts GmbH · ref INV-2025-0042\n", text)
        # the meta line comes right after the result's own line, before the cite and the hits
        lines = text.split("\n")
        i = next(n for n, l in enumerate(lines) if l.startswith("1. "))
        self.assertTrue(lines[i + 1].startswith("   meta:") and lines[i + 2].startswith("   cite:") and lines[i + 3].startswith("   p.1"))

    def test_unknown_parts_are_left_out_and_no_meta_means_no_line(self):
        self.assertIn("   meta: receipt\n", render(result(meta={"doctype": "receipt"})) + "\n")
        self.assertIn("   meta: receipt · 8.00\n", render(result(meta={"doctype": "receipt", "amount": 8, "currency": None})) + "\n")
        self.assertIn("2025-05-01 · party Cafe", render(result(meta={"doctype": None, "doc_date": "2025-05-01", "counterparty": "Cafe"})))
        for meta in (None, {}, {"doctype": None, "amount": None, "counterparty": None}, "junk"):
            self.assertNotIn("meta:", render(result(meta=meta)))
        self.assertNotIn("meta:", render({k: v for k, v in result().items() if k != "meta"}))       # a backend with no meta key
        self.assertIn("meta: invoice", render(result(meta={"doctype": "invoice", "amount": "n/a", "currency": "EUR"})))


class _redaction:
    """Switch config agents.redact for a block (and back)."""

    def __init__(self, on):
        self.on = on

    def __enter__(self):
        from mse import config
        self.was = config.load()["agents"]["redact"]
        config.update_settings({"agents": {"redact": self.on}})

    def __exit__(self, *exc):
        from mse import config
        config.update_settings({"agents": {"redact": self.was}})


class GetPage(unittest.TestCase):
    def get(self, d, **args):
        cl = FakeClient(d)
        out = mcp.call_tool("get", {"ref": "#abcd1234", **args}, cl, None)
        return out, cl

    def test_without_a_page_the_output_is_the_old_one(self):
        out, cl = self.get(doc())
        self.assertEqual(out, "# Reports/q3.pdf  (#abcd1234, c1, pdf, 2025-09-30)\n\nPage two text.")
        self.assertEqual(cl.calls, [(None, "#abcd1234", 12000, None)])
        out, _ = self.get(doc(text="", doc_date=None))
        self.assertEqual(out, "# Reports/q3.pdf  (#abcd1234, c1, pdf)\n\n(no text extracted)")
        out, _ = self.get(doc(text=None, truncated=False, context="Company reports"))
        self.assertEqual(out, "# Reports/q3.pdf  (#abcd1234, c1, pdf, 2025-09-30)\ncontext: Company reports\n\n(no text extracted)")

    def test_with_a_page_the_head_names_it_and_a_cite_line_follows(self):
        out, cl = self.get(doc(), page=2)
        self.assertEqual(out, "# Reports/q3.pdf  (#abcd1234, c1, pdf, 2025-09-30, page 2)\n"
                              "cite: Quarterly report p.2\n\nPage two text.")
        self.assertEqual(cl.calls[0][3], 2)
        out, _ = self.get(doc(doc_date=None, context="Company reports", title=None), page="3")        # a numeric string is fine
        self.assertEqual(out, "# Reports/q3.pdf  (#abcd1234, c1, pdf, page 3)\ncite: q3.pdf p.3\ncontext: Company reports\n\nPage two text.")

    def test_a_page_without_text_says_so(self):
        out, _ = self.get(doc(text=""), page=2)
        self.assertEqual(out, "# Reports/q3.pdf  (#abcd1234, c1, pdf, 2025-09-30, page 2)\n\npage 2 has no extracted text")
        self.assertNotIn("cite:", out)
        out, _ = self.get(doc(text=None), page=9)
        self.assertTrue(out.endswith("page 9 has no extracted text (the document has 4 pages)"), out)
        out, _ = self.get(doc(text="", pages=None), page=9)
        self.assertTrue(out.endswith("page 9 has no extracted text"), out)

    def test_truncation_note_survives(self):
        out, _ = self.get(doc(truncated=True), page=1)
        self.assertTrue(out.endswith("[truncated — pass a larger max_chars or a page number]"))

    def test_page_zero_or_less_is_an_error_not_no_page(self):
        for bad in (0, -1, -100, "0", "-3", "two", "2.5", 1.5, True, False, [], {}):
            cl = FakeClient(doc())
            with self.assertRaises(ValueError, msg=bad):
                mcp.call_tool("get", {"ref": "#abcd1234", "page": bad}, cl, None)
            self.assertEqual(cl.calls, [])
        # through the protocol it is an error result, as for any bad argument
        r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get", "arguments": {"ref": "x", "page": 0}}},
                       FakeClient(doc()), None)["result"]
        self.assertTrue(r["isError"])
        self.assertIn("page must be a whole number from 1", r["content"][0]["text"])
        # empty and null mean "no page"
        for ok in (None, ""):
            out, cl = self.get(doc(), page=ok)
            self.assertEqual(cl.calls[0][3], None)
            self.assertNotIn("page ", out.split("\n")[0])
        self.assertEqual(self.get(doc(), page=2.0)[1].calls[0][3], 2)

    def test_a_metadata_only_file_gets_no_cite(self):
        out, _ = self.get(doc(metadata_only=True, note="metadata-only by this collection's rules", text=None), page=1)
        self.assertEqual(out, "# Reports/q3.pdf  (#abcd1234, c1, pdf, 2025-09-30)\n\nmetadata-only by this collection's rules")

    def test_the_page_reaches_the_http_client(self):
        from unittest import mock
        seen = []

        def fake_call(self, method, path, params=None, body=None):
            seen.append(dict(params))
            return doc()

        c = mcp.HttpClient.__new__(mcp.HttpClient)
        c.base, c.token = "http://127.0.0.1:9", "t"
        with mock.patch.object(mcp.HttpClient, "_call", fake_call):
            mcp.call_tool("get", {"ref": "#abcd1234", "page": 3}, c, None)
            mcp.call_tool("get", {"ref": "#abcd1234"}, c, None)
        self.assertEqual(seen[0]["page"], 3)
        self.assertNotIn("page", seen[1])


if __name__ == "__main__":
    unittest.main()
