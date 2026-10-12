"""The document filters of the MCP `search` tool (doctype, amount_min / amount_max, currency, party, docdate_from /
docdate_to): each argument becomes the inline filter the query language reads, nothing else changes, bad input is refused
before a query is written, and the filters really narrow a search over a small index. Fictional documents only; temporary
folders and a temporary data folder.
Run: .venv/bin/python -m unittest -v tests.test_mcp_filters"""

import os
import re
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import config, docmeta, mcp, search, store  # noqa: E402
from tests.test_docmeta_index import M, add_doc  # noqa: E402


class Recorder:
    """A client that remembers the query it was given and answers with no results."""

    def __init__(self):
        self.calls = []

    def search(self, q, colls, limit, filters, mode, precise=False):
        self.calls.append({"q": q, "colls": colls, "limit": limit, "filters": filters, "mode": mode, "precise": precise})
        return {"query": q, "collections": colls or [], "results": [], "bridge": {}}


def final_q(args, query="invoice", lock=None, cwd=None):
    """The query a client receives for a `search` call."""
    cl = Recorder()
    mcp.call_tool("search", {"query": query, **args}, cl, cwd, lock)
    return cl.calls[-1]["q"]


def parsed(q):
    """What the query language makes of q: (words, filters)."""
    text, filters, _ = search.parse(q)
    return text, filters


class Tokens(unittest.TestCase):
    def test_every_argument_maps_to_its_inline_filter(self):
        cases = [
            ({"doctype": ["invoice"]}, "doctype:invoice"),
            ({"doctype": ["invoice", "receipt"]}, "doctype:invoice doctype:receipt"),
            ({"doctype": ["bank_statement"]}, "doctype:bank_statement"),
            ({"amount_min": 100}, "amount:>=100.00"),
            ({"amount_max": 250.5}, "amount:<=250.50"),
            ({"amount_min": 100, "amount_max": 250.5}, "amount:>=100.00 amount:<=250.50"),
            ({"currency": "eur"}, "currency:EUR"),
            ({"party": "Acme"}, "party:Acme"),
            ({"party": "Acme Parts GmbH"}, 'party:"Acme Parts GmbH"'),
            ({"docdate_from": "2025-01", "docdate_to": "2025-06"}, "docdate:2025-01..2025-06"),
            ({"docdate_from": "2025-03-14"}, "docdate:2025-03-14.."),
            ({"docdate_to": "2025"}, "docdate:..2025"),
            ({"docdate_from": 2025, "docdate_to": 2025}, "docdate:2025..2025"),
        ]
        for args, tokens in cases:
            self.assertEqual(final_q(args), f"invoice {tokens}", args)

    def test_all_arguments_together_keep_the_order(self):
        q = final_q({"doctype": ["receipt"], "amount_min": 5, "amount_max": 50, "currency": "usd", "party": "Cafe",
                     "docdate_from": "2025-04", "docdate_to": "2025-05-31"})
        self.assertEqual(q, "invoice doctype:receipt amount:>=5.00 amount:<=50.00 currency:USD party:Cafe docdate:2025-04..2025-05-31")

    def test_no_new_argument_leaves_the_query_byte_identical(self):
        for query in ("invoice", "", "  spaced   out  ", 'type:pdf "unbalanced', "trailing backslash \\", "doctype:receipt party:x",
                      "amount:>500 in:work", "Ünïcode ✓"):
            self.assertEqual(mcp._with_doc_filters(query, {"query": query}), query, repr(query))
            self.assertIs(mcp._with_doc_filters(query, {"query": query, "limit": 5, "type": "pdf"}), query)
        # arguments that clients send for "not set" count as not given; 0 is a value
        empty = {"doctype": [], "party": "", "currency": None, "docdate_from": "", "amount_min": None, "amount_max": ""}
        self.assertEqual(final_q(empty), "invoice")
        self.assertEqual(final_q({"amount_min": 0}), "invoice amount:>=0.00")

    def test_empty_query_with_filters_is_just_the_filters(self):
        self.assertEqual(final_q({"doctype": ["invoice"]}, query=""), "doctype:invoice")
        self.assertEqual(final_q({"doctype": ["invoice"]}, query="   "), "doctype:invoice")
        self.assertEqual(final_q({"doctype": ["invoice"]}, query="  lead"), "  lead doctype:invoice")

    def test_every_advertised_kind_is_a_kind_the_query_language_accepts(self):
        for kind in mcp.DOCTYPES:
            q = final_q({"doctype": [kind]})
            self.assertEqual(docmeta.doctype_values(parsed(q)[1]), {kind}, kind)
        self.assertEqual(mcp.DOCTYPES, docmeta.DOCTYPES)

    def test_doctype_forms(self):
        self.assertEqual(final_q({"doctype": "invoice"}), "invoice doctype:invoice")                    # one kind as a string
        self.assertEqual(final_q({"doctype": ["Invoice", " receipt ", "invoice"]}), "invoice doctype:invoice doctype:receipt")
        self.assertEqual(final_q({"doctype": "invoice,quote"}), "invoice doctype:invoice doctype:quote")
        self.assertEqual(final_q({"doctype": ["other"]}), "invoice doctype:other")
        for bad in (["bill"], ["invoice", "nope"], [3], {"a": 1}, 7, "tax-document"):
            with self.assertRaises(ValueError, msg=bad):
                final_q({"doctype": bad})

    def test_currency_is_validated(self):
        for bad in ("EU", "EURO", "E1R", "€", " ", 3, ["EUR"]):
            with self.assertRaises(ValueError, msg=bad):
                final_q({"currency": bad})
        self.assertEqual(final_q({"currency": " Chf "}), "invoice currency:CHF")

    def test_amounts_are_validated_and_written_with_two_decimals(self):
        for bad in (True, False, float("nan"), float("inf"), float("-inf"), -1, -0.01, 1e12, 5e15, "abc", [1], {}):
            for key in ("amount_min", "amount_max"):
                with self.assertRaises(ValueError, msg=(key, bad)):
                    final_q({key: bad})
        with self.assertRaises(ValueError):
            final_q({"amount_min": 500, "amount_max": 499.99})
        self.assertEqual(final_q({"amount_min": 500, "amount_max": 500}), "invoice amount:>=500.00 amount:<=500.00")
        self.assertEqual(final_q({"amount_min": "12.5"}), "invoice amount:>=12.50")                      # a numeric string is fine
        self.assertEqual(final_q({"amount_min": 1500000}), "invoice amount:>=1500000.00")                # never 1.5e+06
        self.assertEqual(final_q({"amount_min": 999999999999}), "invoice amount:>=999999999999.00")
        self.assertEqual(final_q({"amount_min": 1.234}), "invoice amount:>=1.23")                        # never read as 1234

    def test_written_amounts_read_back_exactly(self):
        for v in (0, 0.01, 0.1, 1, 1.234, 1.005, 8, 40, 99.99, 100, 999.99, 1000, 1234.5, 12345.67, 1500000, 999999999999):
            token = mcp._doc_filter_pairs({"amount_min": v})[0][1]
            lo, lo_inc, hi, hi_inc = docmeta.parse_amount(token)
            self.assertEqual((lo, lo_inc, hi), (round(float(v), 2), True, None), (v, token))
            token = mcp._doc_filter_pairs({"amount_max": v})[0][1]
            self.assertEqual(docmeta.parse_amount(token), (None, True, round(float(v), 2), True), (v, token))

    def test_dates_are_validated(self):
        for bad in ("2025-13", "2025-00", "2025-02-30", "2025-04-31", "2025-1", "25-01", "2025/01", "2025-01-01T00", "March 2025",
                    "0000", "2025-03-14.."):
            for key in ("docdate_from", "docdate_to"):
                with self.assertRaises(ValueError, msg=(key, bad)):
                    final_q({key: bad})
        for bad in (True, 2025.5, ["2025"], {}):
            with self.assertRaises(ValueError, msg=bad):
                final_q({"docdate_from": bad})
        with self.assertRaises(ValueError):
            final_q({"docdate_from": "2025-07", "docdate_to": "2025-06"})
        with self.assertRaises(ValueError):
            final_q({"docdate_from": "2026", "docdate_to": "2025-12-31"})
        with self.assertRaises(ValueError):
            final_q({"docdate_from": "2025-06-20", "docdate_to": "2025-06-10"})      # same month, the day order decides
        self.assertEqual(final_q({"docdate_from": "2024-02-29"}), "invoice docdate:2024-02-29..")        # a leap day is a real day
        with self.assertRaises(ValueError):
            final_q({"docdate_from": "2025-02-29"})
        self.assertEqual(final_q({"docdate_from": "2025-06-15", "docdate_to": "2025-06"}), "invoice docdate:2025-06-15..2025-06")

    def test_party_is_validated(self):
        for bad in ('Acme "Parts"', "Acme\\Parts", "Acme\nParts", "Acme\tParts", "Acme\x00", "Smith, Jones", "x" * 121, ["Acme"], 5):
            with self.assertRaises(ValueError, msg=bad):
                final_q({"party": bad})
        with self.assertRaises(ValueError):
            final_q({"party": "   "})                                                                   # blank is not "no party"
        self.assertEqual(final_q({"party": "  Acme  "}), "invoice party:Acme")
        self.assertEqual(final_q({"party": "O'Brien & Sons"}), "invoice party:\"O'Brien & Sons\"")
        self.assertEqual(final_q({"party": "O'Brien"}), "invoice party:\"O'Brien\"")                     # the quote is quoted
        self.assertEqual(final_q({"party": "Müller & Söhne"}), 'invoice party:"Müller & Söhne"')
        self.assertEqual(final_q({"party": "x" * 120}), "invoice party:" + "x" * 120)

    def test_party_with_spaces_needs_a_query_that_tokenizes(self):
        # an unbalanced quote or a trailing backslash in the user's own query would swallow the quoted party
        for query in ('say "hello', "it's", "ends with \\"):
            with self.assertRaises(ValueError, msg=query) as cm:
                final_q({"party": "Acme Parts"}, query=query)
            self.assertIn("cannot be added", str(cm.exception))
        # a party without spaces does not need quoting, so the plain-whitespace fallback of the parser reads it right
        self.assertEqual(final_q({"party": "Acme"}, query='say "hello'), 'say "hello party:Acme')
        text, filters = parsed(final_q({"party": "Acme"}, query='say "hello'))
        self.assertEqual(filters, {"party": ["Acme"]})
        # ... but not behind a backslash that escapes the separator
        with self.assertRaises(ValueError):
            final_q({"doctype": ["invoice"]}, query="ends with \\")
        # a balanced query is fine
        self.assertEqual(final_q({"party": "Acme Parts"}, query='"exact phrase" more'),
                         '"exact phrase" more party:"Acme Parts"')

    def test_a_party_cannot_smuggle_a_filter_or_a_scope(self):
        q = final_q({"party": "Acme in:/etc type:pdf"}, query="invoice")
        text, filters = parsed(q)
        self.assertEqual(filters, {"party": ["Acme in:/etc type:pdf"]})
        self.assertEqual(text, "invoice")
        self.assertEqual(config.folder_tokens(q), ["/etc"])          # the raw-text scan WOULD see it, so it must never run on q
        # the scope shortcut and the folder check run on the user's query, before the tokens exist
        cl = Recorder()
        lock = mcp.Lock(str(_Lock.root / "sub"))
        mcp.call_tool("search", {"query": "invoice", "party": "Acme in:/etc"}, cl, None, lock)
        self.assertEqual(cl.calls[0]["colls"], [lock.name])
        self.assertEqual(cl.calls[0]["filters"], {})

    def test_lock_and_scope_shortcut_apply_to_the_user_query_only(self):
        lock = mcp.Lock(str(_Lock.root / "sub"))
        cl = Recorder()
        mcp.call_tool("search", {"query": "invoice", "doctype": ["invoice"], "party": "Acme Parts"}, cl, None, lock)
        self.assertEqual(cl.calls[0]["colls"], [lock.name])
        self.assertEqual(cl.calls[0]["q"], 'invoice doctype:invoice party:"Acme Parts"')
        with self.assertRaises(PermissionError):
            mcp.call_tool("search", {"query": "invoice", "doctype": ["invoice"], "collection": "all"}, cl, None, lock)
        with self.assertRaises(PermissionError):
            mcp.call_tool("search", {"query": "all: invoice", "doctype": ["invoice"]}, cl, None, lock)
        self.assertEqual(len(cl.calls), 1)                          # nothing reached the backend
        # unlocked: the shortcut is taken out of q first, then the tokens are added to what is left
        name = "mf" + uuid.uuid4().hex[:8]
        folder = Path(tempfile.mkdtemp(prefix="mse-mcp-filters-"))
        config.add_collection(str(folder), name)
        self.addCleanup(config.remove_collection, name)
        cl = Recorder()
        mcp.call_tool("search", {"query": f"{name}: invoice", "doctype": ["receipt"]}, cl, None)
        self.assertEqual((cl.calls[0]["q"], cl.calls[0]["colls"]), ("invoice doctype:receipt", [name]))

    def test_the_tokens_are_what_the_query_language_reads(self):
        args = {"doctype": ["invoice", "receipt"], "amount_min": 5, "amount_max": 50.5, "currency": "eur", "party": "Cafe Beispiel",
                "docdate_from": "2025-04", "docdate_to": "2025-06"}
        text, filters = parsed(final_q(args, query="lunch"))
        self.assertEqual(text, "lunch")
        self.assertEqual(filters, {"doctype": ["invoice", "receipt"], "amount": [">=5.00", "<=50.50"], "currency": ["EUR"],
                                   "party": ["Cafe Beispiel"], "docdate": ["2025-04..2025-06"]})
        # and a predicate can be built from them
        self.assertIsNotNone(search.meta_filter(filters))

    def test_docdate_to_is_inclusive(self):
        for start, stop, lo, hi in (("2025-01", "2025-06", "2025-01-01", "2025-07-01"),
                                    ("2025-06", "2025-06", "2025-06-01", "2025-07-01"),
                                    ("2025", "2025", "2025-01-01", "2026-01-01"),
                                    ("2025-12", "2025-12", "2025-12-01", "2026-01-01"),
                                    ("2025-03-14", "2025-03-14", "2025-03-14", "2025-03-15"),
                                    ("2025-02-01", "2025-02-28", "2025-02-01", "2025-03-01")):
            value = mcp._doc_filter_pairs({"docdate_from": start, "docdate_to": stop})[0][1]
            self.assertEqual(docmeta.parse_docdate(value), (lo, hi), value)
        self.assertEqual(docmeta.parse_docdate(mcp._doc_filter_pairs({"docdate_from": "2025-03"})[0][1]), ("2025-03-01", None))
        self.assertEqual(docmeta.parse_docdate(mcp._doc_filter_pairs({"docdate_to": "2025-03"})[0][1]), (None, "2025-04-01"))

    def test_repeated_filters_combine_as_documented(self):
        filters = parsed(final_q({"doctype": ["invoice", "receipt"], "amount_min": 10, "amount_max": 20}))[1]
        self.assertEqual(docmeta.doctype_values(filters), {"invoice", "receipt"})                      # doctype: alternatives
        ok = search.meta_filter(filters)
        row = lambda dt, amount: {"dm_doctype": dt, "dm_amount": amount, "dm_currency": "EUR", "dm_counterparty": "x",
                                  "dm_doc_date": "2025-03-14"}
        self.assertTrue(ok(row("invoice", 10.0)) and ok(row("receipt", 20.0)) and ok(row("invoice", 15.0)))
        self.assertFalse(ok(row("invoice", 9.99)) or ok(row("receipt", 20.01)) or ok(row("quote", 15.0)))   # both amount limits hold


class HttpPath(unittest.TestCase):
    """HttpClient sends q to /api/search and drops every other key it does not know: the tokens must be in q."""

    def client(self):
        c = mcp.HttpClient.__new__(mcp.HttpClient)
        c.base, c.token = "http://127.0.0.1:9", "t"
        return c

    def test_http_client_sends_the_final_query(self):
        seen = []

        def fake_call(self, method, path, params=None, body=None):
            seen.append((method, path, dict(params or {})))
            return {"query": params["q"], "collections": [], "results": [], "bridge": {}}

        with mock.patch.object(mcp.HttpClient, "_call", fake_call):
            mcp.call_tool("search", {"query": "invoice", "doctype": ["invoice"], "amount_min": 100, "currency": "eur",
                                     "party": "Acme Parts", "docdate_from": "2025-01", "docdate_to": "2025-06"},
                          self.client(), None)
            mcp.call_tool("search", {"query": "plain"}, self.client(), None)
        method, path, params = seen[0]
        self.assertEqual((method, path), ("GET", "/api/search"))
        self.assertEqual(params["q"], 'invoice doctype:invoice amount:>=100.00 currency:EUR party:"Acme Parts" docdate:2025-01..2025-06')
        self.assertEqual(params["shortcuts"], "0")
        self.assertEqual(set(params) - {"q", "limit", "mode", "shortcuts", "precise"}, set())      # nothing the server would drop
        self.assertEqual(seen[1][2]["q"], "plain")
        # the tokens survive the URL and come out of the daemon's parser as they went in
        from urllib.parse import parse_qs, urlencode
        back = parse_qs(urlencode(params, doseq=True))["q"][0]
        self.assertEqual(back, params["q"])
        self.assertEqual(parsed(back)[1]["party"], ["Acme Parts"])


class _Lock:
    root = None


def setUpModule():
    _Lock.root = Path(tempfile.mkdtemp(prefix="mse-mcp-flock-")).resolve()
    (_Lock.root / "sub").mkdir()
    _Lock.name = "mfl" + uuid.uuid4().hex[:8]
    config.add_collection(str(_Lock.root), _Lock.name)


def tearDownModule():
    config.remove_collection(_Lock.name)


class EndToEnd(unittest.TestCase):
    """The filters over a real (tiny) index, through LocalClient and the query language."""

    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="mse-mcp-e2e-"))
        cls.name = "me" + uuid.uuid4().hex[:8]
        config.add_collection(str(cls.folder), cls.name)
        conn = store.connect(cls.name)
        for rel, text, meta in (
                ("acme_mar.pdf", "alpha", M("invoice", "2025-03-14", 900.0, "EUR", "Acme Parts GmbH")),
                ("muster_apr.pdf", "alpha", M("invoice", "2025-04-02", 40.0, "EUR", "Muster AG")),
                ("cafe_apr.pdf", "alpha", M("receipt", "2025-04-03", 8.0, "EUR", "Cafe Beispiel")),
                ("acme_quote_jun.pdf", "alpha", M("quote", "2025-06-30", 1500.5, "USD", "Acme Parts GmbH")),
                ("cafe_jul.pdf", "alpha", M("receipt", "2025-07-01", 12.0, "EUR", "Cafe Beispiel")),
                ("brien_jun.pdf", "alpha", M("invoice", "2025-06-15", 40.0, "EUR", "O'Brien & Sons")),
                ("misc.pdf", "alpha", M("other")),
                ("unclassified.pdf", "alpha", None)):
            add_doc(conn, cls.folder, cls.name, rel, text, meta)
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        config.remove_collection(cls.name)

    def found(self, args, query=""):
        out = mcp.call_tool("search", {"query": query, "collection": self.name, "mode": "keyword", "precise": False, "limit": 50, **args},
                            mcp.LocalClient(), None)
        return sorted(re.findall(r"\] (\S+\.pdf)", out))

    def test_filters_narrow_the_results(self):
        e = self.assertEqual
        e(self.found({"doctype": ["invoice"]}), ["acme_mar.pdf", "brien_jun.pdf", "muster_apr.pdf"])
        e(self.found({"doctype": ["invoice", "receipt"]}), ["acme_mar.pdf", "brien_jun.pdf", "cafe_apr.pdf", "cafe_jul.pdf", "muster_apr.pdf"])
        e(self.found({"amount_min": 40, "amount_max": 900}), ["acme_mar.pdf", "brien_jun.pdf", "muster_apr.pdf"])
        e(self.found({"amount_min": 41, "amount_max": 899.99}), [])
        e(self.found({"amount_min": 1000}), ["acme_quote_jun.pdf"])
        e(self.found({"currency": "usd"}), ["acme_quote_jun.pdf"])
        e(self.found({"party": "cafe"}), ["cafe_apr.pdf", "cafe_jul.pdf"])
        e(self.found({"party": "Acme Parts"}), ["acme_mar.pdf", "acme_quote_jun.pdf"])
        e(self.found({"party": "O'Brien & Sons"}), ["brien_jun.pdf"])
        e(self.found({"doctype": ["invoice"], "party": "Acme Parts", "currency": "EUR"}), ["acme_mar.pdf"])

    def test_docdate_to_includes_its_whole_period(self):
        e = self.assertEqual
        e(self.found({"docdate_from": "2025-04", "docdate_to": "2025-06"}), ["acme_quote_jun.pdf", "brien_jun.pdf", "cafe_apr.pdf", "muster_apr.pdf"])
        e(self.found({"docdate_from": "2025-06", "docdate_to": "2025-06"}), ["acme_quote_jun.pdf", "brien_jun.pdf"])
        e(self.found({"docdate_to": "2025-03"}), ["acme_mar.pdf"])
        e(self.found({"docdate_from": "2025-07"}), ["cafe_jul.pdf"])
        e(self.found({"docdate_from": "2025-04-03", "docdate_to": "2025-04-03"}), ["cafe_apr.pdf"])
        e(self.found({"docdate_from": "2025", "docdate_to": "2025", "doctype": ["quote"]}), ["acme_quote_jun.pdf"])
        e(self.found({"docdate_from": "2026"}), [])

    def test_unclassified_documents_never_match_a_document_filter(self):
        self.assertNotIn("unclassified.pdf", self.found({"docdate_from": "2000"}))
        self.assertEqual(self.found({"doctype": ["other"]}), ["misc.pdf"])          # `other` is a kind of its own, not "everything else"

    def test_words_and_filters_combine(self):
        self.assertEqual(self.found({"doctype": ["receipt"]}, query="alpha"), ["cafe_apr.pdf", "cafe_jul.pdf"])


if __name__ == "__main__":
    unittest.main()
