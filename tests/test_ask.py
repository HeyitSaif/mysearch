"""Ask mode: passage packing, citation checking, redaction for agents, the off-switch and follow-up questions
(history). No model is loaded: the worker pool and the search are replaced by fakes.
Run: .venv/bin/python -m unittest discover -s tests"""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import ask, config, embedder, paths  # noqa: E402

# Fictitious values only: a textbook example IBAN and made-up document numbers.
IBAN = "DE89 3704 0044 0532 0130 00"


def result(path, docid, hits=(), relevance=100, meta=False, collection="unit", date="2025-04-09", title=None):
    return {"collection": collection, "path": path, "docid": docid, "title": title or path.rsplit("/", 1)[-1],
            "name": path.rsplit("/", 1)[-1], "date": date, "relevance": relevance, "metadata_only": meta,
            "hits": list(hits), "copies": []}


def chunk(path, docid, text, page=None, t0=None, heading=None):
    return {"path": path, "docid": docid, "modality": "text", "page": page, "t0": t0, "t1": None,
            "heading": heading, "text": text}


CHUNKS = {
    "a/supplier.md": [chunk("a/supplier.md", "aaaa0001", "Unrelated introduction about team lunches and holiday planning."),
                    chunk("a/supplier.md", "aaaa0001", "The supplier invoice 1234567001 of 250,00 EUR was paid on "
                          "9 April 2025 from a business account.")],
    "b/office.pdf": [chunk("b/office.pdf", "bbbb0002", "Bürobedarf, Acme Supplies: Beleg vom 17.06.2025.", page=1),
                    chunk("b/office.pdf", "bbbb0002", "Gesamtbetrag 18,90 EUR gebucht.", page=2)],
    "c/identity.pdf": [chunk("c/identity.pdf", "cccc0003", "SECRET CONTENT THAT MUST NEVER BE SENT")],
    "d/bank.md": [chunk("d/bank.md", "dddd0004", f"Payment to the supplier from IBAN {IBAN} confirmed.")],
}


def fake_chunks(r):
    if r.get("metadata_only"):
        raise AssertionError("metadata-only files must not be read")
    return CHUNKS[r["path"]]


def fake_search(results, bridge=None):
    calls = []

    def search(q, collections, filters, limit):
        calls.append((q, collections, filters, limit))
        return {"results": results, "bridge": bridge or {}}
    search.calls = calls
    return search


def fake_gen(tokens, record=None):
    def gen(messages, **params):
        if record is not None:
            record.append({"messages": messages, **params})
        for t in tokens:
            yield {"token": t}
        yield {"done": True, "model_ttft_ms": 5, "tokens": len(tokens), "tok_s": 50.0, "prompt_tokens": 100}
    return gen


ENABLED = {"ask": {"enabled": True, "route": "local"}}


class Passages(unittest.TestCase):
    def test_best_chunk_per_document_and_numbering(self):
        res = [result("a/supplier.md", "aaaa0001", hits=[{"snippet": "The supplier <mark>invoice</mark> 1234567001 of "
                                                                   "250,00 EUR was <mark>paid</mark> on 9 April"}]),
               result("b/office.pdf", "bbbb0002", hits=[{"page": 2, "snippet": None}])]
        ps = ask.build_passages("when was the supplier invoice paid", res, fake_chunks, max_passages=2)
        self.assertEqual([p["n"] for p in ps], [1, 2])
        self.assertIn("250,00 EUR", ps[0]["text"])               # the hit's chunk, not the introduction
        self.assertEqual(ps[0]["docid"], "#aaaa0001")
        self.assertEqual((ps[1]["page"], ps[1]["path"]), (2, "b/office.pdf"))
        self.assertIn("page 2", ps[1]["header"])

    def test_metadata_only_never_read(self):
        res = [result("c/identity.pdf", "cccc0003", meta=True), result("a/supplier.md", "aaaa0001")]
        ps = ask.build_passages("supplier invoice", res, fake_chunks)
        self.assertTrue(all(p["path"] != "c/identity.pdf" for p in ps))
        self.assertNotIn("SECRET", json.dumps(ps))

    def test_budget_and_limit(self):
        long_text = "supplier " * 2000
        many = {f"x/{i}.md": [chunk(f"x/{i}.md", f"{i:08x}", long_text)] for i in range(20)}
        res = [result(p, f"{i:08x}") for i, p in enumerate(many)]
        ps = ask.build_passages("supplier", res, lambda r: many[r["path"]], max_passages=8, context_tokens=1500)
        self.assertLessEqual(len(ps), 8)
        total = sum(ask.estimate_tokens(p["header"] + "\n" + p["text"]) + 4 for p in ps)
        self.assertLessEqual(total, 1500)
        self.assertTrue(all(len(p["text"]) <= ask.PASSAGE_CHARS + 2 for p in ps))

    def test_second_passage_for_top_document_when_room_is_left(self):
        res = [result("b/office.pdf", "bbbb0002")]
        ps = ask.build_passages("Beleg Gesamtbetrag", res, fake_chunks, max_passages=8)
        self.assertEqual(len(ps), 2)
        self.assertEqual({p["page"] for p in ps}, {1, 2})

    def test_low_relevance_results_skipped(self):
        res = [result("a/supplier.md", "aaaa0001"), result("b/office.pdf", "bbbb0002", relevance=5)]
        ps = ask.build_passages("supplier", res, fake_chunks, min_relevance=20)
        self.assertTrue(all(p["path"] == "a/supplier.md" for p in ps))

    def test_digits_cost_more_than_letters(self):
        self.assertGreater(ask.estimate_tokens("1234567001 250,00"), ask.estimate_tokens("supplier invoice"))
        self.assertAlmostEqual(ask.estimate_tokens("abcd"), 4 * 0.29)

    def test_collections_do_not_mix_unless_all(self):
        res = [result("a/supplier.md", "aaaa0001", collection="acme"),
               result("d/bank.md", "dddd0004", collection="notes"),
               result("b/office.pdf", "bbbb0002", collection="acme")]
        self.assertEqual({r["collection"] for r in ask.one_collection(res, None)}, {"acme"})
        self.assertEqual({r["collection"] for r in ask.one_collection(res, ["acme", "notes"])},
                         {"acme"})
        self.assertEqual({r["collection"] for r in ask.one_collection(res, ["all"])}, {"acme", "notes"})
        rec = []
        list(ask.ask("supplier?", search_fn=fake_search(res), chunks_fn=fake_chunks, gen_fn=fake_gen(["ok [1]."], rec),
                     cfg=ENABLED))
        self.assertNotIn("d/bank.md", rec[0]["messages"][1]["content"])

    def test_transcript_timestamp(self):
        self.assertEqual(ask._ts(190.4), "00:03:10")
        self.assertIsNone(ask._ts(None))


class Citations(unittest.TestCase):
    def test_invalid_numbers_dropped_and_ranges_expanded(self):
        text, cited, dropped = ask.clean_citations("Paid on 9 April [1]. Registered [2, 9]. Fee [7]. Both [1-2].",
                                                   {1, 2})
        self.assertEqual(text, "Paid on 9 April [1]. Registered [2]. Fee. Both [1][2].")
        self.assertEqual(cited, [1, 2])
        self.assertEqual(dropped, 2)

    def test_stream_holds_split_markers(self):
        s = ask.CitationStream({1, 2})
        out = "".join(s.feed(t) for t in [" Paid", " on 14 Mar", " [", "1", "]", ". See [", "4", "].", " A [note]", " [2"])
        out += s.flush()
        self.assertEqual(out, "Paid on 14 Mar [1]. See. A [note]")       # the cut-off "[2" is dropped
        self.assertEqual(s.cited, [1])
        self.assertEqual(s.dropped, 1)

    def test_stream_canonicalises_lists(self):
        s = ask.CitationStream({1, 3})
        out = s.feed("Both [1, 3] agree [3 ,1].") + s.flush()
        self.assertEqual(out, "Both [1][3] agree [3][1].")

    def test_think_block_swallowed(self):
        s = ask.CitationStream({1})
        out = "".join(s.feed(t) for t in ["<think>", "plan", "</think>", "\n\nAnswer [1]."]) + s.flush()
        self.assertEqual(out, "Answer [1].")

    def test_not_found(self):
        self.assertTrue(ask.is_not_found("The documents don't answer this."))
        self.assertTrue(ask.is_not_found("Die Dokumente beantworten diese Frage nicht."))
        self.assertFalse(ask.is_not_found("The invoice was paid on 9 April [1].", [1]))
        # paraphrased refusals without any citation (seen from Qwen3.5-4B in the benchmark)
        self.assertTrue(ask.is_not_found("The provided documents do not state the number of followers."))
        self.assertTrue(ask.is_not_found("Die Dokumente enthalten keine Informationen darüber, welche Farbe es hat."))
        # an answer that cites something is an answer, even if it mentions what the documents lack
        self.assertFalse(ask.is_not_found("It was filed [1]. The documents do not give the exact time.", [1]))


class Flow(unittest.TestCase):
    def run_ask(self, tokens, results, agent_safe=False, record=None, q="When was the supplier invoice paid?"):
        return list(ask.ask(q, agent_safe=agent_safe, search_fn=fake_search(results), chunks_fn=fake_chunks,
                            gen_fn=fake_gen(tokens, record), cfg=ENABLED))

    def test_events_follow_the_contract(self):
        ev = self.run_ask(["It was paid", " on 9 April 2025", " [1]", " [5]."],
                          [result("a/supplier.md", "aaaa0001"), result("b/office.pdf", "bbbb0002")])
        self.assertEqual([e["type"] for e in ev], ["sources", "token", "token", "token", "token", "done"])
        src = ev[0]["sources"]
        self.assertEqual(set(src[0]), {"n", "docid", "collection", "path", "title", "page", "ts", "snippet"})
        self.assertTrue(src[0]["docid"].startswith("#"))
        self.assertNotIn("<mark>", src[0]["snippet"])
        done = ev[-1]
        self.assertEqual(done["citations"], [1])
        self.assertEqual(done["answer"], "It was paid on 9 April 2025 [1].")
        self.assertTrue(done["answered"])
        self.assertIn("ttft_ms", done["timings"])
        self.assertIn("total_ms", done["timings"])
        self.assertNotIn("[5]", "".join(e.get("text", "") for e in ev))

    def test_prompt_rules(self):
        rec = []
        self.run_ask(["x"], [result("a/supplier.md", "aaaa0001")], record=rec, q="Wann wurde die Lieferrechnung bezahlt?")
        system, user = rec[0]["messages"][0]["content"], rec[0]["messages"][1]["content"]
        self.assertIn("Write in German", system)
        self.assertIn(ask.NOT_FOUND["de"], system)
        self.assertIn("[1] ", user)
        self.assertTrue(user.rstrip().endswith("citing the passages as [n]."))
        self.assertEqual(rec[0]["max_tokens"], ask.ASK_DEFAULTS["max_tokens"])

    def test_no_passages_answers_without_the_model(self):
        rec = []
        ev = self.run_ask(["never"], [], record=rec)
        self.assertEqual(rec, [])
        self.assertEqual(ev[-1]["answer"], ask.NOT_FOUND["en"])
        self.assertFalse(ev[-1]["answered"])

    def test_agent_safe_masks_identifiers_everywhere(self):
        rec = []
        ev = self.run_ask([f"Paid from {IBAN} [1]."], [result("d/bank.md", "dddd0004")], agent_safe=True, record=rec)
        self.assertNotIn("3704 0044", json.dumps(rec[0]["messages"]))     # the model never sees it
        self.assertNotIn("3704 0044", json.dumps(ev[0]))                  # nor do the sources
        self.assertNotIn("3704 0044", ev[-1]["answer"])                  # nor the final answer

    def test_owner_sees_everything(self):
        rec = []
        self.run_ask(["ok [1]."], [result("d/bank.md", "dddd0004")], record=rec)
        self.assertIn("3704 0044", json.dumps(rec[0]["messages"]))

    def test_gen_error_becomes_error_event(self):
        def broken(messages, **p):
            raise RuntimeError("worker failed")
            yield  # pragma: no cover
        ev = list(ask.ask("supplier?", search_fn=fake_search([result("a/supplier.md", "aaaa0001")]), chunks_fn=fake_chunks,
                          gen_fn=broken, cfg=ENABLED))
        self.assertEqual(ev[-1]["type"], "error")


class OffSwitch(unittest.TestCase):
    def test_disabled_raises_before_anything_runs(self):
        search = fake_search([result("a/supplier.md", "aaaa0001")])
        gen_calls = []
        with self.assertRaises(ask.AskDisabled):
            ask.ask("supplier?", search_fn=search, chunks_fn=fake_chunks, gen_fn=lambda *a, **k: gen_calls.append(1),
                    cfg={"ask": {"enabled": False}})
        self.assertEqual(search.calls, [])
        self.assertEqual(gen_calls, [])

    def test_default_is_off(self):
        self.assertFalse(ask.settings({})["enabled"])
        self.assertFalse(ask.settings(config.load())["enabled"])

    def test_pool_never_spawns_while_disabled(self):
        p = embedder.Pool.__new__(embedder.Pool)               # no reaper thread
        p.workers = {"ask": embedder.Worker("ask")}
        with mock.patch("subprocess.Popen", side_effect=AssertionError("worker spawned")) as popen, \
                mock.patch.object(ask, "settings", return_value={**ask.ASK_DEFAULTS, "enabled": False}):
            with self.assertRaises(ask.AskDisabled):
                p.ask_stream([{"role": "user", "content": "x"}])
            with self.assertRaises(ask.AskDisabled):
                p.ask_prepare()
            popen.assert_not_called()
        self.assertFalse(p.workers["ask"].alive)

    def test_switching_off_unloads(self):
        p = embedder.Pool.__new__(embedder.Pool)
        w = embedder.Worker("ask")
        w.proc = mock.Mock()
        w.proc.poll.return_value = None
        p.workers = {"ask": w}
        with mock.patch.object(ask, "settings", return_value={**ask.ASK_DEFAULTS, "enabled": False}):
            p.reap_once()                                       # the reaper notices Ask is off
        self.assertIsNone(w.proc)

    def test_explicit_stop(self):
        p = embedder.Pool.__new__(embedder.Pool)
        w = embedder.Worker("ask")
        proc = mock.Mock()
        proc.poll.return_value = None
        w.proc = proc
        p.workers = {"ask": w}
        p.stop("ask")
        self.assertIsNone(w.proc)
        proc.stdin.write.assert_called()                        # asked to quit (killed if it does not)

    def test_status_block(self):
        st = ask.status()
        self.assertEqual(set(st), STATUS_KEYS)
        self.assertFalse(st["loaded"])


STATUS_KEYS = {"available", "enabled", "loaded", "local_model", "ram_mb", "route", "provider", "model", "online",
               "where_next", "next_model", "training", "training_confirmed", "local_fallback", "next_problem"}


class Defaults(unittest.TestCase):
    def test_model_lives_in_the_models_folder(self):
        self.assertTrue(ask.ASK_DEFAULTS["local_model"].startswith(str(paths.MODELS)))
        self.assertEqual(ask.model_name({"local_model": "/x/y/qwen"}), "qwen")
        # before cloud models, "model" named the local model's folder: such a value still means the local model
        s = ask.settings({"ask": {"model": "/x/y/qwen"}})
        self.assertEqual((s["local_model"], ask.model_name(s)), ("/x/y/qwen", "qwen"))
        self.assertEqual(s["model"], config.ASK_CLOUD_DEFAULTS["model"])


# ---------------------------------------------------------------- phase 2: settings, HTTP API, MCP tool, CLI

class _ConfigRestore(unittest.TestCase):
    """Each test leaves config.json exactly as it found it (other test modules share the temporary home)."""

    def setUp(self):
        config.load()
        self._saved = paths.CONFIG.read_text()

    def tearDown(self):
        paths.CONFIG.write_text(self._saved)


class SettingsSwitch(_ConfigRestore):
    def test_off_by_default_and_switchable(self):
        self.assertEqual(config.load()["ask"], {"enabled": False})
        fake = mock.Mock()
        with mock.patch.object(embedder, "_pool", fake):
            config.update_settings({"ask": {"enabled": True}})
            fake.stop.assert_not_called()
            self.assertTrue(ask.settings()["enabled"])
            config.update_settings({"ask": {"enabled": False}})
            fake.stop.assert_called_once_with("ask")            # switching off unloads at once
        self.assertFalse(ask.settings()["enabled"])

    def test_no_pool_no_problem(self):
        with mock.patch.object(embedder, "_pool", None):
            config.update_settings({"ask": {"enabled": False}})

    def test_enabled_must_be_boolean(self):
        with self.assertRaises(ValueError):
            config.update_settings({"ask": {"enabled": "yes"}})

    def test_other_knobs_override_defaults(self):
        with mock.patch.object(embedder, "_pool", None):
            config.update_settings({"ask": {"max_tokens": 200}})
        self.assertEqual(ask.settings()["max_tokens"], 200)
        self.assertEqual(ask.settings()["passages"], ask.ASK_DEFAULTS["passages"])


class HttpApi(_ConfigRestore):
    """POST /api/ask on an in-process test server (port 0, temporary home); the model is never loaded."""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        from mse import server
        cls.server_mod = server
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def post(self, path, body):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
                                     method="POST", headers={"X-MSE-Token": config.load()["token"],
                                                             "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.headers.get("Content-Type"), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read().decode()

    def test_409_while_disabled(self):
        code, _, body = self.post("/api/ask", {"q": "supplier?"})
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(body), {"error": "ask_disabled"})

    def test_streams_ndjson_events(self):
        seen = {}

        def fake_ask(q, collections=None, filters=None, agent_safe=False):
            seen.update(q=q, collections=collections, filters=filters, agent_safe=agent_safe)
            return iter([{"type": "sources", "sources": []}, {"type": "token", "text": "Paid [1]."},
                         {"type": "done", "citations": [1], "model": "m", "timings": {"ttft_ms": 1, "total_ms": 2}}])
        with mock.patch.object(ask, "ask", side_effect=fake_ask):
            code, ctype, body = self.post("/api/ask", {"q": "supplier?", "filters": {"type": "pdf", "after": ["2025-03"]}})
        self.assertEqual(code, 200)
        self.assertTrue(ctype.startswith("application/x-ndjson"))
        lines = [json.loads(x) for x in body.splitlines() if x]
        self.assertEqual([e["type"] for e in lines], ["sources", "token", "done"])
        self.assertEqual(seen["filters"], {"type": ["pdf"], "after": ["2025-03"]})
        self.assertFalse(seen["agent_safe"])

    def test_unknown_scope_is_400(self):
        config.update_settings({"ask": {"enabled": True}})
        code, _, body = self.post("/api/ask", {"q": "x", "collections": ["no-such-folder"]})
        self.assertEqual(code, 400)

    def test_status_and_settings_carry_ask(self):
        st = self.server_mod._ask_status()
        self.assertEqual(set(st) - {"error"}, STATUS_KEYS)
        with mock.patch.object(embedder, "_pool", None):
            code, _, body = self.post("/api/settings", {"ask": {"enabled": True}})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ask"]["enabled"])


class McpTool(_ConfigRestore):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="mse-ask-coll-")
        os.makedirs(os.path.join(self.root, "sub"))
        self.coll = config.add_collection(self.root, "asktest")["name"]
        self.other_root = tempfile.mkdtemp(prefix="mse-ask-other-")
        self.other = config.add_collection(self.other_root, "askother")["name"]

    def client(self, events=None, error=None):
        c = mock.Mock()

        def ask_fn(q, colls, agent_safe=True):
            c.seen = (q, colls, agent_safe)
            if error:
                raise error
            return iter(events or [
                {"type": "sources", "sources": [{"n": 1, "docid": "#aaaa0001", "collection": self.coll,
                                                 "path": "a/supplier.md", "title": "supplier", "page": 2, "ts": None,
                                                 "snippet": "x"}]},
                {"type": "token", "text": "Paid [1]."},
                {"type": "done", "citations": [1], "model": "qwen", "answer": "Paid [1].", "answered": True,
                 "timings": {}}])
        c.ask = ask_fn
        return c

    def test_tool_listed(self):
        from mse import mcp
        self.assertIn("ask", [t["name"] for t in mcp.TOOLS])

    def test_defaults_to_the_working_folder_and_masks(self):
        from mse import mcp
        c = self.client()
        out = mcp.call_tool("ask", {"question": "supplier?"}, c, os.path.join(self.root, "sub"))
        self.assertEqual(c.seen, ("supplier?", [self.coll], True))
        self.assertIn("Paid [1].", out)
        self.assertIn("[1] #aaaa0001 asktest/a/supplier.md, page 2", out)

    def test_all_and_groups_pass_through(self):
        from mse import mcp
        c = self.client()
        mcp.call_tool("ask", {"question": "q", "collection": "all"}, c, None)
        self.assertEqual(c.seen[1], ["all"])
        mcp.call_tool("ask", {"question": "q", "collection": f"{self.coll},{self.other}"}, c, None)
        self.assertEqual(c.seen[1], [self.coll, self.other])

    def test_lock_forces_the_locked_collection_and_refuses_others(self):
        from mse import mcp
        lock = mcp.Lock(os.path.join(self.root, "sub"))
        c = self.client()
        mcp.call_tool("ask", {"question": "q"}, c, os.path.join(self.root, "sub"), lock)
        self.assertEqual(c.seen[1], [self.coll])
        for spec in ("all", self.other, f"{self.coll},{self.other}"):
            with self.assertRaises(PermissionError):
                mcp.call_tool("ask", {"question": "q", "collection": spec}, c, os.path.join(self.root, "sub"), lock)
        reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "ask", "arguments": {"question": "q", "collection": "all"}}},
                           c, os.path.join(self.root, "sub"), lock)
        self.assertTrue(reply["result"]["isError"])
        self.assertIn(lock.refusal, reply["result"]["content"][0]["text"])

    def test_disabled_is_a_plain_message(self):
        from mse import mcp
        for err in (ask.AskDisabled("ask_disabled"), RuntimeError("ask_disabled")):
            out = mcp.call_tool("ask", {"question": "q"}, self.client(error=err), None)
            self.assertIn("Ask is switched off", out)

    def test_not_found_and_error_formatting(self):
        from mse import mcp
        out = mcp.format_answer([{"type": "sources", "sources": []},
                                 {"type": "done", "citations": [], "answer": ask.NOT_FOUND["en"], "answered": False,
                                  "model": "qwen"}])
        self.assertIn(ask.NOT_FOUND["en"], out)
        self.assertIn("no source", out)
        self.assertEqual(mcp.format_answer([{"type": "error", "message": "boom"}]), "Ask failed: boom")


class Cli(_ConfigRestore):
    def run_cli(self, events, argv, tty=False):
        import contextlib
        import io
        from mse import cli
        fake = mock.Mock()
        fake.ask = mock.Mock(return_value=iter(events))
        out = io.StringIO()
        with mock.patch.object(cli, "daemon_up", return_value=True), \
                mock.patch.object(cli, "client", return_value=fake), \
                mock.patch.object(cli, "TTY", tty), contextlib.redirect_stdout(out):
            cli.main(argv)
        return fake, out.getvalue()

    EVENTS = [{"type": "sources", "sources": [{"n": 1, "docid": "#dddd0004", "collection": "c", "path": "d/bank.md",
                                               "title": "bank", "page": None, "ts": None, "snippet": ""}]},
              {"type": "token", "text": f"Paid from {IBAN} [1]."},
              {"type": "done", "citations": [1], "model": "qwen", "answer": f"Paid from {IBAN} [1].",
               "answered": True, "timings": {"ttft_ms": 1, "total_ms": 2}}]

    def test_piped_output_is_masked_and_asks_agent_safe(self):
        fake, out = self.run_cli(self.EVENTS, ["ask", "who was paid?", "-c", "all"])
        self.assertEqual(fake.ask.call_args.args[1], ["all"])
        self.assertTrue(fake.ask.call_args.kwargs["agent_safe"])
        self.assertNotIn("3704 0044", out)
        self.assertIn("[1] ", out)
        self.assertIn("d/bank.md", out)

    def test_json(self):
        _, out = self.run_cli(self.EVENTS, ["ask", "who was paid?", "--json", "-c", "all"])
        d = json.loads(out)
        self.assertEqual(d["citations"], [1])
        self.assertTrue(d["answered"])
        self.assertNotIn("3704 0044", out)


# ---------------------------------------------------------------- follow-up questions (history)

SUPPLIER_Q = "How much did the supplier charge for the courier?"
SUPPLIER_A = ("The supplier charged 350,00 EUR in two invoices, 1234567001 and 1234567002 [1][2]. Both are dated "
              "11 July 2025 [1].")
LAPTOP_Q = "When does the warranty on the laptop end?"
REWRITE = "When was the supplier invoice for the courier paid?"
HEURISTIC = "and when was it paid? supplier charge courier"
NO_REWRITE = {"ask": {"enabled": True, "route": "local", "rewrite_model": False}}


def turn(q, a="It was paid [1]."):
    return {"q": q, "a": a}


def gen_by_role(rewrite_tokens, answer_tokens, record=None, rewrite_delay=0.0):
    """A fake model that tells the rewrite call (by its system prompt) from the answer call."""
    def gen(messages, **params):
        rewrite = messages[0]["content"] == ask.REWRITE_SYSTEM
        if record is not None:
            record.append({"kind": "rewrite" if rewrite else "answer", "messages": messages, **params})
        if rewrite and rewrite_delay:
            time.sleep(rewrite_delay)
        for t in (rewrite_tokens if rewrite else answer_tokens):
            yield {"token": t}
        yield {"done": True}
    return gen


def follow_up(q, history, gen, cfg=None, results=None, agent_safe=False, collections=None):
    search = fake_search(results if results is not None else [result("a/supplier.md", "aaaa0001")])
    ev = list(ask.ask(q, collections=collections, agent_safe=agent_safe, search_fn=search, chunks_fn=fake_chunks,
                      gen_fn=gen, cfg=cfg or ENABLED, history=history))
    return ev, search


def answer_call(record):
    return next(r for r in record if r["kind"] == "answer")


class FollowUpHeuristic(unittest.TestCase):
    def test_pronoun_follow_up_carries_the_previous_subject(self):
        self.assertTrue(ask.is_follow_up("and when was it paid?"))
        self.assertEqual(ask.heuristic_query("and when was it paid?", [turn(SUPPLIER_Q)]), HEURISTIC)

    def test_new_entity_is_added_and_the_topic_kept(self):
        got = ask.heuristic_query("what about the printer?", [turn(LAPTOP_Q)])
        self.assertTrue(got.startswith("what about the printer?"))
        for term in ("warranty", "laptop", "end"):
            self.assertIn(term, got)
        self.assertIn("warranty", ask.heuristic_query("printer?", [turn(LAPTOP_Q)]))     # a bare fragment, too

    def test_german_follow_ups(self):
        h = [turn("Wann wurde die Lieferantenrechnung bezahlt?")]
        self.assertIn("Lieferantenrechnung", ask.heuristic_query("Und wie viel hat sie gekostet?", h))
        self.assertIn("Lieferantenrechnung", ask.heuristic_query("Wer hat bezahlt?", h))

    def test_standalone_questions_are_searched_as_they_are(self):
        for q in ("Who is Erika Mustermann?", "Welche Farbe hat der USB-Stick?", "When does the warranty on the laptop end?",
                  "Which bank account was used for the hosting invoice payment?", "Was kostet die Lieferantenrechnung?"):
            self.assertFalse(ask.is_follow_up(q), q)
            self.assertEqual(ask.heuristic_query(q, [turn(SUPPLIER_Q)]), q)

    def test_a_chain_keeps_its_subject(self):
        got = ask.heuristic_query("from which account?", [turn(SUPPLIER_Q), turn("and when was it paid?")])
        self.assertIn("supplier", got)
        self.assertIn("paid", got)

    def test_carried_terms_are_capped(self):
        long_q = "Compare the supplier invoice, bank charges and the rent"
        got = ask.heuristic_query("and who paid?", [turn(long_q)])
        self.assertEqual(len(got.split()) - len("and who paid?".split()), ask.CARRY_TERMS)

    def test_a_new_date_or_number_replaces_the_old_one(self):
        got = ask.heuristic_query("and in April?", [turn("Which invoices did Acme send in March 2025?")])
        self.assertIn("Acme", got)
        self.assertNotIn("March", got)               # carried, it would become a contradicting date filter
        self.assertNotIn("2025", got)
        got = ask.heuristic_query("and 1234567002?", [turn("What did invoice 1234567001 cost?")])
        self.assertNotIn("1234567001", got)
        self.assertIn("invoice", got)
        got = ask.heuristic_query("and from Acme?", [turn("Which invoices came in last month?")])
        self.assertIn("last month", got)              # no date of its own: the earlier one still applies

    def test_language_of_a_bare_follow_up_comes_from_the_conversation(self):
        self.assertEqual(ask.language("printer?", [turn("Wann endet die Garantie für den Laptop?")]), "de")
        self.assertEqual(ask.language("and when?", [turn("Wann endet die Garantie für den Laptop?")]), "en")
        self.assertEqual(ask.language("printer?"), "en")


class FollowUpRewrite(unittest.TestCase):
    def test_clean_rewrite(self):
        h = [turn(SUPPLIER_Q, SUPPLIER_A)]
        q = "and when was it paid?"
        ok = {"Standalone question: " + REWRITE: REWRITE,
              f'"{REWRITE}"\nThe question asks about the payment date.': REWRITE,
              "<think>x</think>" + REWRITE: REWRITE,
              "When was invoice 1234567001 paid?": "When was invoice 1234567001 paid?",   # a number it was told
              REWRITE + " in:personal": REWRITE}                    # scope never comes from the model
        for raw, want in ok.items():
            self.assertEqual(ask.clean_rewrite(raw, q, h), want, raw)
        for bad in ("", None, "It was paid on 9 April 2025 [1].", "When was invoice 4711 paid?",
                    "Hello! How can I help you today?", ask.NOT_FOUND["en"], "x" * 400):
            self.assertIsNone(ask.clean_rewrite(bad, q, h), bad)
        # a follow-up that names something new must keep it
        self.assertIsNone(ask.clean_rewrite("When does the warranty on the laptop end?", "what about the USB printer?",
                                            [turn(LAPTOP_Q)]))
        self.assertEqual(ask.clean_rewrite("When does the USB printer warranty end?", "what about the USB printer?",
                                           [turn(LAPTOP_Q)]), "When does the USB printer warranty end?")

    def test_model_rewrite_is_searched_and_reported(self):
        rec = []
        ev, search = follow_up("and when was it paid?", [turn(SUPPLIER_Q, SUPPLIER_A)],
                               gen_by_role([REWRITE[:20], REWRITE[20:]], ["Paid on 9 April 2025 [1]."], rec))
        self.assertEqual(search.calls[0][0], REWRITE)
        rw = rec[0]
        self.assertEqual((rw["kind"], rw["max_tokens"], rw["temperature"]), ("rewrite", ask.REWRITE_TOKENS, 0.0))
        self.assertLessEqual(ask.REWRITE_TOKENS, 40)
        self.assertIn("Last question: and when was it paid?", rw["messages"][1]["content"])
        self.assertNotIn("[1]", rw["messages"][1]["content"])     # earlier markers never reach the rewrite
        done = ev[-1]
        self.assertEqual((done["search_query"], done["rewrite"], done["history_used"]), (REWRITE, "model", 1))
        self.assertIn("rewrite_ms", done["timings"])
        self.assertIn(f"(In context: {REWRITE})", answer_call(rec)["messages"][1]["content"])

    def test_request_filters_survive_a_rewrite(self):
        _, search = follow_up("and when was it paid? type:pdf", [turn(SUPPLIER_Q)],
                              gen_by_role([REWRITE + " after:2020"], ["ok [1]."]))
        self.assertEqual(search.calls[0][0], REWRITE + " type:pdf")   # the request's filter kept, the invented one not

    def test_slow_rewrite_falls_back_to_the_heuristic(self):
        cfg = {"ask": {"enabled": True, "route": "local", "rewrite_budget_ms": 50}}
        t = time.time()
        ev, search = follow_up("and when was it paid?", [turn(SUPPLIER_Q)],
                               gen_by_role([REWRITE], ["Paid [1]."], rewrite_delay=0.6), cfg=cfg)
        self.assertLess(time.time() - t, 0.5)                      # the request did not wait for it
        self.assertEqual(search.calls[0][0], HEURISTIC)
        done = ev[-1]
        self.assertEqual((done["rewrite"], done["rewrite_fallback"]), ("heuristic", "timeout"))
        self.assertEqual(done["answer"], "Paid [1].")

    def test_unusable_or_failing_rewrite_falls_back(self):
        ev, search = follow_up("and when was it paid?", [turn(SUPPLIER_Q)],
                               gen_by_role(["It was paid on 9 April [1]."], ["Paid [1]."]))
        self.assertEqual(search.calls[0][0], HEURISTIC)
        self.assertEqual(ev[-1]["rewrite_fallback"], "unusable")

        def broken(messages, **p):
            if messages[0]["content"] == ask.REWRITE_SYSTEM:
                raise RuntimeError("worker failed")
            return fake_gen(["Paid [1]."])(messages, **p)
        ev, search = follow_up("and when was it paid?", [turn(SUPPLIER_Q)], broken)
        self.assertEqual((search.calls[0][0], ev[-1]["rewrite_fallback"]), (HEURISTIC, "error"))
        self.assertEqual(ev[-1]["answer"], "Paid [1].")

    def test_standalone_question_never_calls_the_rewrite(self):
        rec = []
        ev, search = follow_up("Welche Farbe hat der USB-Stick?", [turn(SUPPLIER_Q)], gen_by_role(["x"], ["ok [1]."], rec))
        self.assertEqual([r["kind"] for r in rec], ["answer"])
        self.assertEqual(search.calls[0][0], "Welche Farbe hat der USB-Stick?")
        self.assertEqual(ev[-1]["rewrite"], "none")

    def test_switch_off_and_cold_model_use_the_heuristic(self):
        rec = []
        ev, search = follow_up("and when was it paid?", [turn(SUPPLIER_Q)], gen_by_role(["x"], ["ok [1]."], rec),
                               cfg=NO_REWRITE)
        self.assertEqual([r["kind"] for r in rec], ["answer"])
        self.assertEqual((search.calls[0][0], ev[-1]["rewrite_fallback"]), (HEURISTIC, "off"))
        sq = ask.search_query("and when was it paid?", [turn(SUPPLIER_Q)], ask.settings(ENABLED), gen_fn=None)
        self.assertEqual((sq["query"], sq["fallback"]), (HEURISTIC, "model_not_loaded"))

    def test_no_history_changes_nothing(self):
        rec = []
        ev, search = follow_up("and when was it paid?", None, gen_by_role(["x"], ["ok [1]."], rec))
        self.assertEqual([r["kind"] for r in rec], ["answer"])
        self.assertEqual(search.calls[0][0], "and when was it paid?")
        self.assertEqual((ev[-1]["search_query"], ev[-1]["rewrite"]), ("and when was it paid?", "none"))
        self.assertNotIn("history_used", ev[-1])


class FollowUpPrompt(unittest.TestCase):
    def test_first_question_prompt_is_unchanged(self):
        ps = [{"n": 1, "header": "h", "text": "t"}]
        msgs = ask.build_messages("When?", ps, "en")
        self.assertEqual(msgs[0]["content"], ask.SYSTEM.format(not_found=ask.NOT_FOUND["en"], language="English"))
        self.assertEqual(msgs[1]["content"], "Passages:\n\n[1] h\nt\n\nQuestion: When?\n\n"
                                             "Answer in English, citing the passages as [n].")

    def test_prompt_holds_the_compact_conversation(self):
        rec = []
        long_a = SUPPLIER_A + " " + " ".join(f"word{i}" for i in range(100))
        follow_up("and when was it paid?", [turn(SUPPLIER_Q, long_a)], gen_by_role([], ["ok [1]."], rec), cfg=NO_REWRITE)
        system, user = (m["content"] for m in answer_call(rec)["messages"])
        self.assertIn(ask.HISTORY_RULE, system)
        self.assertTrue(system.rstrip().endswith("Write in English."))
        # passages, then the conversation, then the question, then the instruction
        order = [user.index(x) for x in ("Passages:", ask.CONVO_HEAD, "Question: and when was it paid?",
                                         "Answer in English")]
        self.assertEqual(order, sorted(order))
        convo = user[user.index(ask.CONVO_HEAD):user.index("\n\nQuestion:")]
        self.assertIn("Q: " + SUPPLIER_Q, convo)
        self.assertIn("A: The supplier charged 350,00 EUR in two invoices, 1234567001 and 1234567002.", convo)
        self.assertNotRegex(convo, r"\[\d+\]")                     # earlier markers pointed at earlier passages
        a_line = next(x for x in convo.splitlines() if x.startswith("A: "))
        self.assertEqual(len(a_line[3:].split()), ask.HISTORY_WORDS + 1)      # 60 words and "…"
        self.assertTrue(a_line.endswith("…"))

    def test_citations_point_only_at_the_current_passages(self):
        h = [turn(SUPPLIER_Q, "See [1][2][3][4][5] and [7].")]
        ev, _ = follow_up("and when was it paid?", h, gen_by_role([], ["Paid on 9 April [1].", " As said [4]",
                                                                      " and [2]."]), cfg=NO_REWRITE)
        self.assertEqual([s["n"] for s in ev[0]["sources"]], [1])
        done = ev[-1]
        self.assertEqual(done["citations"], [1])
        self.assertEqual(done["dropped_citations"], 2)
        self.assertEqual(done["answer"], "Paid on 9 April [1]. As said and.")

    def test_history_is_trimmed_first_and_the_total_stays_in_budget(self):
        h = [turn(f"OLDEST question {'alpha ' * 300}", "beta " * 400),
             turn(f"MIDDLE question {'gamma ' * 300}", "delta " * 400),
             turn(f"NEWEST question about the supplier {'eps ' * 300}", "zeta " * 400)]
        rec = []
        cfg = {"ask": {"enabled": True, "route": "local", "rewrite_model": False, "context_tokens": 1200}}
        with mock.patch.object(ask, "build_passages", wraps=ask.build_passages) as bp:
            follow_up("and when was it paid?", h, gen_by_role([], ["ok [1]."], rec), cfg=cfg)
        user = answer_call(rec)["messages"][1]["content"]
        self.assertNotIn("OLDEST", user)
        self.assertIn("NEWEST", user)
        passage_budget = bp.call_args.args[4]
        convo = user[user.index(ask.CONVO_HEAD):user.index("\n\nQuestion:")]
        hist = ask.estimate_tokens(convo) + ask.estimate_tokens(ask.HISTORY_RULE) + 8
        self.assertLessEqual(hist, 1200 // 4 + 1)                    # the conversation's share
        self.assertGreaterEqual(passage_budget, 1200 - 1200 // 4)    # the passages keep the rest
        self.assertLessEqual(passage_budget + hist, 1200 + 1)

    def test_fit_history(self):
        turns = ask.compact_history([turn("one " * 70, "two " * 70), turn("three " * 70, "four " * 70)])
        fit = ask.fit_history(turns, 120)
        self.assertEqual(len(fit), 1)
        self.assertTrue(fit[0]["q"].startswith("three"))
        self.assertLessEqual(ask.history_cost(fit), 120)
        self.assertEqual(ask.fit_history(turns, 5), [])
        self.assertEqual(ask.fit_history(turns, 10_000), turns)

    def test_bare_follow_up_is_answered_in_the_conversation_language(self):
        rec = []
        follow_up("printer?", [turn("Wann endet die Garantie für den Laptop?")], gen_by_role([], ["x [1]."], rec),
                  cfg=NO_REWRITE)
        self.assertIn("Write in German", answer_call(rec)["messages"][0]["content"])


class FollowUpScopeAndRedaction(_ConfigRestore):
    def test_history_never_chooses_the_scope(self):
        root = tempfile.mkdtemp(prefix="mse-ask-fu-")
        coll = config.add_collection(root, "fuscope")["name"]
        h = [turn(f"{coll}: when was the supplier invoice paid in:{coll} type:pdf")]
        rec = []
        res = [result("a/supplier.md", "aaaa0001", collection="unit"), result("d/bank.md", "dddd0004", collection="other")]
        ev, search = follow_up("and the office fee?", h, gen_by_role([REWRITE + f" in:{coll}"], ["ok [1]."], rec),
                               results=res)
        q, colls, filters, _ = search.calls[0]
        self.assertIsNone(colls)                                     # the request's scope: none given, so none
        self.assertIsNone(filters)
        self.assertNotIn(coll, q)
        self.assertNotIn("type:pdf", q)
        for r in rec:                                                # neither model sees the shortcut or filters
            self.assertNotIn(coll, json.dumps(r["messages"]))
            self.assertNotIn("type:pdf", json.dumps(r["messages"]))
        self.assertEqual({s["collection"] for s in ev[0]["sources"]}, {"unit"})   # still one collection
        _, search = follow_up("and the office fee?", h, gen_by_role([], ["ok [1]."]), cfg=NO_REWRITE,
                              collections=["unit"])
        self.assertEqual(search.calls[0][1], ["unit"])
        self.assertNotIn(coll, search.calls[0][0])

    def test_agent_safe_masks_the_history_everywhere(self):
        h = [turn(f"Which payment came from IBAN {IBAN}?", f"It came from {IBAN} [1].")]
        rec = []
        ev, search = follow_up("and when was it paid?", h,
                               gen_by_role([f"When was the payment from {IBAN} paid?"], ["Paid [1]."], rec),
                               agent_safe=True)
        self.assertEqual([r["kind"] for r in rec], ["rewrite", "answer"])
        self.assertNotIn("3704 0044", json.dumps([r["messages"] for r in rec]))   # neither prompt
        self.assertNotIn("3704 0044", search.calls[0][0])            # nor the search built from the history
        self.assertNotIn("3704 0044", json.dumps(ev))                # nor any event (search_query, sources, answer)
        self.assertEqual(ev[-1]["rewrite_fallback"], "unusable")     # a number the masked conversation never said
        self.assertNotIn("3000", ev[-1]["search_query"])             # nor the visible tail of the mask

    def test_owner_sees_the_history(self):
        h = [turn(f"Which payment came from IBAN {IBAN}?", f"It came from {IBAN} [1].")]
        rec = []
        follow_up("and when was it paid?", h, gen_by_role([], ["ok [1]."], rec), cfg=NO_REWRITE)
        self.assertIn("3704 0044", json.dumps(answer_call(rec)["messages"]))


class HistoryValidation(unittest.TestCase):
    def test_validate(self):
        self.assertEqual(ask.validate_history(None), [])
        self.assertEqual(ask.validate_history([]), [])
        got = ask.validate_history([{"q": " x " * 1000, "a": "y" * 2500, "id": 7}])
        self.assertEqual(set(got[0]), {"q", "a"})
        self.assertEqual((len(got[0]["q"]), len(got[0]["a"])), (ask.HISTORY_CHARS, ask.HISTORY_CHARS))
        for bad in ("x", {"q": "x", "a": "y"}, [turn("x")] * 4, ["x"], [{"q": "", "a": ""}], [{"q": 1, "a": ""}],
                    [{"q": "x"}], [{"q": "x", "a": None}]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ask.validate_history(bad)

    def test_bad_history_fails_before_anything_runs(self):
        search = fake_search([result("a/supplier.md", "aaaa0001")])
        with self.assertRaises(ValueError):
            ask.ask("and?", search_fn=search, chunks_fn=fake_chunks, gen_fn=fake_gen(["x"]), cfg=ENABLED,
                    history=[turn("x")] * 4)
        self.assertEqual(search.calls, [])


class HttpHistory(_ConfigRestore):
    """POST /api/ask with history (in-process test server, as in HttpApi)."""
    setUpClass = HttpApi.__dict__["setUpClass"]                   # the raw classmethods: cls = this class
    tearDownClass = HttpApi.__dict__["tearDownClass"]
    post = HttpApi.post

    def test_malformed_history_is_400_before_the_switch(self):
        for history in ("x", [{}], [{"q": 1, "a": ""}], [{"q": "x", "a": None}], [turn("x")] * 4):
            code, _, body = self.post("/api/ask", {"q": "and when?", "history": history})
            self.assertEqual(code, 400, history)
            self.assertIn("history", json.loads(body)["error"])
        code, _, body = self.post("/api/ask", {"q": "and when?", "history": [turn("x")]})
        self.assertEqual((code, json.loads(body)), (409, {"error": "ask_disabled"}))     # valid, but Ask is off

    def test_history_passes_through_cut_to_size(self):
        seen = {}

        def fake_ask(q, collections=None, filters=None, agent_safe=False, history=None):
            seen.update(q=q, history=history)
            return iter([{"type": "done", "citations": [], "answer": "x", "search_query": "s", "rewrite": "model"}])
        with mock.patch.object(ask, "ask", side_effect=fake_ask):
            code, _, body = self.post("/api/ask", {"q": "and when?", "history": [{"q": "x" * 2500, "a": "y", "n": 1}]})
        self.assertEqual(code, 200)
        self.assertEqual(seen["history"], [{"q": "x" * ask.HISTORY_CHARS, "a": "y"}])
        self.assertEqual(json.loads(body.splitlines()[-1])["search_query"], "s")

    def test_without_history_the_call_is_unchanged(self):
        def old_signature(q, collections=None, filters=None, agent_safe=False):   # as other fakes are written
            return iter([{"type": "done", "citations": [], "answer": "x"}])
        with mock.patch.object(ask, "ask", side_effect=old_signature):
            for body in ({"q": "x"}, {"q": "x", "history": []}, {"q": "x", "history": None}):
                self.assertEqual(self.post("/api/ask", body)[0], 200, body)


class CliFollowUp(_ConfigRestore):
    def setUp(self):
        super().setUp()
        self.memory = paths.TMP / "ask_last.json"
        self.memory.unlink(missing_ok=True)

    def tearDown(self):
        self.memory.unlink(missing_ok=True)
        super().tearDown()

    def run_cli(self, argv, answer):
        import contextlib
        import io
        from mse import cli
        events = [{"type": "sources", "sources": []}, {"type": "token", "text": answer},
                  {"type": "done", "citations": [], "model": "qwen", "answer": answer, "answered": True,
                   "timings": {}, "search_query": "sq", "rewrite": "heuristic"}]
        fake = mock.Mock()
        fake.ask = mock.Mock(return_value=iter(events))
        out = io.StringIO()
        with mock.patch.object(cli, "daemon_up", return_value=True), \
                mock.patch.object(cli, "client", return_value=fake), \
                mock.patch.object(cli, "TTY", False), contextlib.redirect_stdout(out):
            cli.main(argv)
        return fake.ask.call_args, out.getvalue()

    def test_follow_up_sends_the_earlier_turns_and_keeps_their_scope(self):
        call, _ = self.run_cli(["ask", "How much did the supplier charge?", "-c", "all"], "350,00 EUR.")
        self.assertNotIn("history", call.kwargs)
        self.assertEqual(self.memory.stat().st_mode & 0o777, 0o600)
        call, out = self.run_cli(["ask", "and when was it paid?", "--follow-up", "--json"], "On 9 April.")
        self.assertEqual(call.kwargs["history"], [{"q": "How much did the supplier charge?", "a": "350,00 EUR."}])
        self.assertEqual(call.args[1], ["all"])                      # the conversation's scope
        self.assertEqual(json.loads(out)["search_query"], "sq")
        for i in range(3):
            call, _ = self.run_cli(["ask", f"follow-up {i}?", "--follow-up"], f"answer {i}.")
        self.assertEqual([t["q"] for t in call.kwargs["history"]],
                         ["and when was it paid?", "follow-up 0?", "follow-up 1?"])     # the last 3 turns
        call, _ = self.run_cli(["ask", "A new topic?"], "x.")        # no --follow-up: a new conversation
        self.assertNotIn("history", call.kwargs)
        call, _ = self.run_cli(["ask", "and?", "--follow-up"], "y.")
        self.assertEqual(call.kwargs["history"], [{"q": "A new topic?", "a": "x."}])

    def test_follow_up_without_an_earlier_question(self):
        with self.assertRaises(SystemExit) as e:
            self.run_cli(["ask", "and when?", "--follow-up"], "x")
        self.assertIn("no earlier question", str(e.exception))


if __name__ == "__main__":
    unittest.main()
