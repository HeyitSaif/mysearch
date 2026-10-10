"""Ask with cloud models: route decisions (online / offline / failures before and after the first token), redaction
before a cloud call, folders kept local (ask_cloud=false), keys never echoed, provider CRUD validation, the Anthropic /
Chat Completions / Responses stream parsers, the HTTP client against local fake servers, the API, the MCP lock and the
CLI. Nothing here touches the network or the real Keychain: keys live in an in-memory fake, the online probe is
replaced, and every "cloud" is a fake server on 127.0.0.1.
Run: .venv/bin/python -m unittest discover -s tests"""

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import ask, cloud, config, embedder, mcp, paths, providers  # noqa: E402
from mse.cloud import CloudError  # noqa: E402

IBAN = "DE89 3704 0044 0532 0130 00"           # the textbook example IBAN (fictitious)
IBAN_DIGITS = "3704 0044"
SECRET = "sk-ant-test-0123456789abcdefSECRETKEY"  # a made-up key; it must never appear in any output
FILE_KEY = "F" * 20 + "ileBackedKey_0123456789"      # the content of the key file of the fictional provider "localrouter"
ENV_KEYS = ("ANTHROPIC_API_KEY", "MSE_KEY_LOCALROUTER")
ROUTER_URL = "http://127.0.0.1:59999/v1"            # nothing listens here
TRAIN = "acme-large-train"                      # matched by the fictional provider's training_models ["*-train"]


# ---------------------------------------------------------------- fakes

class FakeKeychain:
    def __init__(self, items=None):
        self.items = dict(items or {})

    def get(self, account):
        return self.items.get(account)

    def has(self, account):
        return account in self.items

    def set(self, account, secret):
        self.items[account] = secret

    def delete(self, account):
        return self.items.pop(account, None) is not None


def result(path, docid, collection="unit"):
    return {"collection": collection, "path": path, "docid": docid, "title": path.rsplit("/", 1)[-1],
            "name": path.rsplit("/", 1)[-1], "date": "2025-04-09", "relevance": 100, "metadata_only": False,
            "hits": [], "copies": []}


CHUNKS = {
    "a/supplier.md": "The supplier invoice 1234567001 of 250,00 EUR was paid on 9 April 2025.",
    "d/bank.md": f"Payment to the supplier from IBAN {IBAN} confirmed.",
}


def chunks(r):
    return [{"path": r["path"], "docid": r["docid"], "modality": "text", "page": None, "t0": None, "t1": None,
             "heading": None, "text": CHUNKS[r["path"]]}]


def search_fn(results):
    calls = []

    def search(q, collections, filters, limit):
        calls.append((q, collections))
        return {"results": results, "bridge": {}}
    search.calls = calls
    return search


def gen(tokens, record=None, fail_at=None, err=None):
    """A fake model: yields `tokens`; raises `err` before token number `fail_at` (len(tokens): after the last)."""
    def g(messages, **params):
        if record is not None:
            record.append({"messages": messages, **params})
        for i, t in enumerate(tokens):
            if fail_at == i:
                raise err
            yield {"token": t}
        if fail_at == len(tokens):
            raise err
        yield {"done": True, "model_ttft_ms": 3, "tokens": len(tokens)}
    return g


ENABLED = {"ask": {"enabled": True}}
FALLBACK = {"ask": {"enabled": True, "local_fallback": True}}      # "Use the local model when no cloud model is reachable"
SUPPLIER = [result("a/supplier.md", "aaaa0001")]
BANK = [result("d/bank.md", "dddd0004")]


def run(results=SUPPLIER, cfg=None, **kw):
    kw.setdefault("gen_fn", gen(["local [1]."]))
    ev = list(ask.ask(kw.pop("q", "When was the supplier invoice paid?"), search_fn=search_fn(results), chunks_fn=chunks,
                      cfg=cfg or ENABLED, **kw))
    return ev


def done(ev):
    return next(e for e in ev if e["type"] == "done")


def text(ev):
    return "".join(e.get("text", "") for e in ev if e["type"] == "token")


class FakeAPI:
    """A local HTTP server standing in for a cloud API. reply(path, body) -> (status, headers, [chunks])."""

    def __init__(self, reply):
        self.requests, self.reply = [], reply
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def handle_one(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                body = json.loads(raw) if raw else None
                outer.requests.append({"method": self.command, "path": self.path,
                                       "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                status, headers, parts = outer.reply(self.path, body)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                for p in parts:
                    self.wfile.write(p.encode() if isinstance(p, str) else p)
                    self.wfile.flush()
                self.close_connection = True

            do_GET = do_POST = handle_one

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


SSE = {"Content-Type": "text/event-stream"}


def anthropic_sse(texts, stop="end_turn"):
    ev = [("message_start", {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 120}}}),
          ("content_block_start", {"type": "content_block_start", "index": 0,
                                   "content_block": {"type": "thinking", "thinking": ""}}),
          ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                   "delta": {"type": "thinking_delta", "thinking": "THINKING"}}),
          ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                   "delta": {"type": "signature_delta", "signature": "sig"}}),
          ("content_block_stop", {"type": "content_block_stop", "index": 0}),
          ("content_block_start", {"type": "content_block_start", "index": 1,
                                   "content_block": {"type": "text", "text": ""}})]
    ev += [("content_block_delta", {"type": "content_block_delta", "index": 1,
                                    "delta": {"type": "text_delta", "text": t}}) for t in texts]
    ev += [("content_block_stop", {"type": "content_block_stop", "index": 1}), ("ping", {"type": "ping"}),
           ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 42}}),
           ("message_stop", {"type": "message_stop"})]
    return [f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in ev]


def chat_sse(texts):
    out = ['data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
           ": keep-alive\n\n",
           'data: {"choices":[{"delta":{"reasoning_content":"THINKING"}}]}\n\n']
    out += [f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}\n\n" for t in texts]
    out += ['data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":9,"completion_tokens":4}}\n\n',
            "data: [DONE]\n\n"]
    return out


def responses_sse(texts):
    ev = [("response.created", {"type": "response.created", "response": {"id": "r1"}}),
          ("response.reasoning_summary_text.delta", {"type": "response.reasoning_summary_text.delta",
                                                     "delta": "THINKING"})]
    ev += [("response.output_text.delta", {"type": "response.output_text.delta", "delta": t}) for t in texts]
    ev += [("response.completed", {"type": "response.completed",
                                   "response": {"usage": {"input_tokens": 11, "output_tokens": 5}}})]
    return [f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in ev] + ["data: [DONE]\n\n"]


class Base(unittest.TestCase):
    """Fake Keychain, no real probe, no key in the environment, config.json restored after each test."""

    def setUp(self):
        config.load()
        self._saved = paths.CONFIG.read_text()
        self._kc, self._probe = providers.keychain, providers.PROBE
        self.kc = providers.keychain = FakeKeychain()
        self.probed = []
        self.up = True

        def probe(host, port, timeout):
            self.probed.append((host, port))
            return self.up
        providers.PROBE = probe
        providers.forget_cache()
        self._key_dir = tempfile.TemporaryDirectory()         # holds the key file of "localrouter" when a test adds it
        self.key_file = os.path.join(self._key_dir.name, "key")
        self._env = mock.patch.dict(os.environ, {k: "" for k in ENV_KEYS})
        self._env.start()

    def router_def(self, base_url=ROUTER_URL, key=FILE_KEY, **extra):
        """The custom provider "localrouter" as config.json holds it (a Responses endpoint on this Mac whose key is read
        from key_file; model ids ending in -train may be trained on). key=None: the key file is not created."""
        if key is not None:
            with open(self.key_file, "w") as f:
                f.write(key + "\n")
        return {"kind": "openai-responses", "label": "Local Router", "base_url": base_url,
                "models": ["acme-large", TRAIN, "acme-small"], "key_file": self.key_file,
                "training_models": ["*-train"], **extra}

    def add_router(self, base_url=ROUTER_URL, key=FILE_KEY, **extra):
        """Save "localrouter" into config.json the way `mse providers add` does -> its row."""
        d = self.router_def(base_url, key, **extra)
        return providers.save({"id": "localrouter", **d})

    def tearDown(self):
        self._env.stop()
        self._key_dir.cleanup()
        providers.keychain, providers.PROBE = self._kc, self._probe
        providers.forget_cache()
        paths.CONFIG.write_text(self._saved)


# ---------------------------------------------------------------- route decisions

class Routes(Base):
    def test_online_auto_answers_in_the_cloud_and_never_loads_the_local_model(self):
        local, remote = [], []
        with mock.patch.object(ask, "_prepare") as prepare, mock.patch.object(ask, "_default_gen") as default:
            ev = list(ask.ask("When was it paid?", search_fn=search_fn(SUPPLIER), chunks_fn=chunks, cfg=ENABLED,
                              cloud_fn=gen(["Paid on 9 April", " [1]."], remote), online_fn=lambda: True))
            prepare.assert_not_called()                       # the 2.5 GB model is not even warmed up
            default.assert_not_called()
        d = done(ev)
        self.assertEqual((d["where"], d["provider"], d["model"]), ("cloud", "anthropic", "claude-opus-5-5"))
        self.assertNotIn("fallback_reason", d)
        self.assertEqual(d["answer"], "Paid on 9 April [1].")
        self.assertEqual(d["citations"], [1])
        self.assertEqual(len(remote), 1)
        self.assertEqual(local, [])
        self.assertEqual([e["type"] for e in ev], ["sources", "token", "token", "done"])   # the same events as local

    def test_offline_with_local_fallback_answers_locally(self):
        remote, local = [], []
        ev = run(cfg=FALLBACK, gen_fn=gen(["local [1]."], local), cloud_fn=gen(["x"], remote), online_fn=lambda: False)
        d = done(ev)
        self.assertEqual((d["where"], d["provider"], d["fallback_reason"]), ("local", "local", "offline"))
        self.assertEqual(remote, [])
        self.assertEqual(len(local), 1)

    def test_cloud_error_before_the_first_token_falls_back_when_the_owner_allowed_it(self):
        local = []
        ev = run(cfg=FALLBACK, gen_fn=gen(["local [1]."], local), online_fn=lambda: True,
                 cloud_fn=gen(["never"], fail_at=0, err=CloudError("HTTP 529: overloaded_error Overloaded", 529)))
        d = done(ev)
        self.assertEqual((d["where"], d["model"]), ("local", ask.model_name(ask.settings(ENABLED))))
        self.assertTrue(d["fallback_reason"].startswith("error: HTTP 529"))
        self.assertEqual(text(ev), "local [1].")
        self.assertNotIn("error", [e["type"] for e in ev])
        self.assertEqual(len(local), 1)

    def test_text_held_back_is_not_a_first_token(self):
        # "[" is held by the citation filter; nothing has reached the client, so falling back is still safe
        ev = run(cfg=FALLBACK, online_fn=lambda: True, cloud_fn=gen(["["], fail_at=1, err=CloudError("connection reset", network=True)))
        self.assertEqual(done(ev)["where"], "local")
        self.assertEqual(text(ev), "local [1].")

    def test_cloud_error_after_the_first_token_is_an_error_event(self):
        local = []
        ev = run(gen_fn=gen(["x"], local), online_fn=lambda: True,
                 cloud_fn=gen(["Paid on", " 9 April"], fail_at=2, err=CloudError("connection reset", network=True)))
        self.assertEqual([e["type"] for e in ev], ["sources", "token", "token", "error"])
        self.assertIn("connection reset", ev[-1]["message"])
        self.assertEqual(local, [])                            # no second answer glued onto the first

    def test_an_empty_cloud_answer_falls_back(self):
        ev = run(cfg=FALLBACK, online_fn=lambda: True, cloud_fn=gen([]))
        self.assertEqual(done(ev)["where"], "local")
        self.assertIn("no answer text", done(ev)["fallback_reason"])

    def test_route_cloud_never_falls_back(self):
        local = []
        ev = run(gen_fn=gen(["x"], local), route="cloud", online_fn=lambda: False,
                 cloud_fn=gen(["x"], fail_at=0, err=CloudError("cannot reach api.anthropic.com", network=True)))
        self.assertEqual(ev[-1]["type"], "error")
        self.assertEqual(local, [])

    def test_route_local_never_calls_the_cloud(self):
        remote = []
        for cfg, route in ((ENABLED, "local"), ({"ask": {"enabled": True, "route": "local"}}, None)):
            ev = run(cfg=cfg, route=route, cloud_fn=gen(["x"], remote), online_fn=lambda: True)
            self.assertEqual(done(ev)["where"], "local")
            self.assertNotIn("fallback_reason", done(ev))
        self.assertEqual(remote, [])

    def test_bad_route_is_refused_before_anything_runs(self):
        s = search_fn(SUPPLIER)
        with self.assertRaises(ValueError):
            ask.ask("x", route="moon", search_fn=s, chunks_fn=chunks, cfg=ENABLED)
        self.assertEqual(s.calls, [])

    def test_no_key_is_an_error_unless_local_fallback_is_on(self):
        ev = run(cfg=FALLBACK)                                  # the real provider path: no key in the fake Keychain
        self.assertEqual((done(ev)["where"], done(ev)["fallback_reason"]), ("local", "no_key"))
        self.assertEqual(self.probed, [])                       # nothing is probed without a key
        for kw in ({}, {"route": "cloud"}):
            ev = run(**kw)                                      # the default: cloud, no silent local answer
            self.assertEqual([e["type"] for e in ev], ["error"])
            self.assertIn("mse providers set-key anthropic", ev[0]["message"])
            self.assertIn("No cloud model is reachable", ev[0]["message"])
            self.assertIn("Switch this chat to Local", ev[0]["message"])
            self.assertIn("mse providers use <id> <model>", ev[0]["message"])

    def test_the_old_route_auto_is_cloud_with_local_fallback(self):
        ev = run(cfg={"ask": {"enabled": True, "route": "auto"}})
        self.assertEqual((done(ev)["where"], done(ev)["fallback_reason"]), ("local", "no_key"))
        ev = run(route="auto")                                  # a question's own "auto" says the same
        self.assertEqual(done(ev)["where"], "local")
        self.assertEqual(ask.route_mode(ask.settings(ENABLED)), ("cloud", False))
        self.assertEqual(ask.route_mode(ask.settings(FALLBACK)), ("cloud", True))
        self.assertEqual(ask.route_mode(ask.settings({"ask": {"route": "local"}})), ("local", False))

    def test_a_cloud_failure_without_fallback_is_an_error_and_never_loads_the_local_model(self):
        with mock.patch.object(ask, "_prepare") as prepare, mock.patch.object(ask, "_default_gen") as default:
            ev = list(ask.ask("When was it paid?", search_fn=search_fn(SUPPLIER), chunks_fn=chunks, cfg=ENABLED,
                              online_fn=lambda: False, cloud_fn=gen(["x"], fail_at=0, err=CloudError("HTTP 529: overloaded", 529))))
            prepare.assert_not_called()
            default.assert_not_called()
        self.assertEqual(ev[-1]["type"], "error")
        self.assertIn("No cloud model is reachable", ev[-1]["message"])
        self.assertIn("HTTP 529", ev[-1]["message"])

    def test_nothing_spawns_the_local_worker_unless_the_owner_chose_local(self):
        """Not switching Ask on, not status, not a failed cloud call, not a follow-up: no ask worker process."""
        from mse import embedder
        spawned = []
        with mock.patch.object(embedder.Worker, "_start", side_effect=lambda *a, **k: spawned.append(1)), \
                mock.patch.object(embedder.subprocess, "Popen", side_effect=lambda *a, **k: spawned.append(2)):
            with mock.patch.object(embedder, "_pool", None):
                config.update_settings({"ask": {"enabled": True}})
            ask.status()
            ask.next_route(ask.settings(), block=True)
            run()                                                              # no key
            run(online_fn=lambda: False, cloud_fn=gen(["x"], fail_at=0, err=CloudError("down", network=True)))
            run(q="and when?", cloud_fn=gen(["Paid [1]."]), online_fn=lambda: True,
                history=[{"q": "When was it paid?", "a": "On 9 April [1]."}])
            self.assertEqual(spawned, [])
            run(cfg={"ask": {"enabled": True, "route": "local"}}, gen_fn=gen(["local [1]."]))   # explicit: the fake generator
        self.assertEqual(spawned, [])

    def test_no_passages_names_who_would_have_answered(self):
        d = done(run(results=[], cloud_fn=gen(["x"]), online_fn=lambda: True))
        self.assertEqual((d["where"], d["answered"]), ("cloud", False))

    def test_follow_up_rewrite_by_the_cloud_model_sees_the_history_masked(self):
        rec = []

        def both(messages, **p):
            rec.append(messages)
            rewrite = messages[0]["content"] == ask.REWRITE_SYSTEM
            yield {"token": "When was the supplier invoice paid?" if rewrite else "Paid [1]."}
            yield {"done": True}
        h = [{"q": f"Which supplier payment came from IBAN {IBAN}?", "a": f"The one from {IBAN} [1]."}]
        ev = run(cloud_fn=both, online_fn=lambda: True, q="and when was it paid?", history=h)
        d = done(ev)
        self.assertEqual((d["where"], d["rewrite"]), ("cloud", "model"))
        self.assertEqual(len(rec), 2)                           # one rewrite, one answer, both in the cloud
        self.assertNotIn(IBAN_DIGITS, json.dumps(rec))


class OnlineCheck(Base):
    def test_probe_is_cached_and_failures_mark_offline(self):
        url = "https://api.anthropic.com/v1"
        self.assertTrue(providers.online(url))
        self.assertTrue(providers.online(url))
        self.assertEqual(self.probed, [("api.anthropic.com", 443)])         # one probe per minute
        providers.note_failure("anthropic", url, CloudError("cannot reach", network=True))
        self.assertFalse(providers.online(url))                            # no wait for the next probe
        providers.note_success("anthropic", url)
        self.assertTrue(providers.online(url))
        self.assertEqual(len(self.probed), 1)

    def test_non_blocking_check_never_waits(self):
        gate = threading.Event()

        def slow(host, port, timeout):
            gate.wait(5)
            return True
        providers.PROBE = slow
        self.assertIsNone(providers.online("https://example.invalid/v1", block=False))   # unknown yet
        gate.set()
        for _ in range(100):
            if providers.online("https://example.invalid/v1", block=False) is not None:
                break
            threading.Event().wait(0.02)
        self.assertTrue(providers.online("https://example.invalid/v1", block=False))

    def test_a_hanging_probe_counts_as_offline_after_the_budget(self):
        providers.PROBE = lambda h, p, t: threading.Event().wait(10) or True
        with mock.patch.object(providers, "ONLINE_TIMEOUT_S", 0.1):
            self.assertFalse(providers.online("https://hangs.invalid/v1"))

    def test_status_says_where_the_next_answer_goes(self):
        with mock.patch.object(embedder, "_pool", None):
            config.update_settings({"ask": {"enabled": True}})
        s = ask.settings()
        nxt = ask.next_route(s, block=True)
        self.assertEqual((nxt["where"], nxt["problem"]), ("cloud", "no_key"))      # default: no silent local answer
        self.assertEqual(ask.next_route({**s, "local_fallback": True}, block=True)["where"], "local")   # no key
        self.kc.set("anthropic", SECRET)
        providers.forget_cache()
        nxt = ask.next_route(ask.settings(), block=True)
        self.assertEqual((nxt["where"], nxt["online"], nxt["model"]), ("cloud", True, "claude-opus-5-5"))
        self.up = False
        providers.forget_cache()
        self.assertEqual(ask.next_route({**ask.settings(), "local_fallback": True}, block=True)["where"], "local")
        nxt = ask.next_route(ask.settings(), block=True)
        self.assertEqual((nxt["where"], nxt["problem"]), ("cloud", "offline"))
        st = ask.status()
        self.assertEqual((st["route"], st["provider"], st["model"], st["local_fallback"], st["next_problem"]),
                         ("cloud", "anthropic", "claude-opus-5-5", False, "offline"))
        self.assertNotIn(SECRET, json.dumps(st))


# ---------------------------------------------------------------- redaction and folders kept local

class Redaction(Base):
    def test_identifiers_are_masked_before_the_cloud_and_counted(self):
        rec = []
        h = [{"q": f"Which payment came from IBAN {IBAN}?", "a": f"It came from {IBAN} [1]."}]
        ev = run(results=BANK, cloud_fn=gen(["Confirmed [1]."], rec), online_fn=lambda: True, history=h,
                 q="Who confirmed the payment to the supplier?")
        sent = json.dumps(rec, ensure_ascii=False)
        self.assertNotIn(IBAN_DIGITS, sent)                    # passages and history, masked
        self.assertIn("DE•••• 3000", sent)
        self.assertGreaterEqual(done(ev)["redacted"], 3)      # the passage, the question and the answer of the turn
        self.assertIn(IBAN_DIGITS, json.dumps(ev[0]))          # the owner's own sources stay complete

    def test_cloud_redact_off_sends_them(self):
        rec = []
        ev = run(results=BANK, cfg={"ask": {"enabled": True, "cloud_redact": False}}, online_fn=lambda: True,
                 cloud_fn=gen(["ok [1]."], rec))
        self.assertIn(IBAN_DIGITS, json.dumps(rec))
        self.assertEqual(done(ev)["redacted"], 0)

    def test_local_answers_for_the_owner_are_not_masked(self):
        rec = []
        ev = run(results=BANK, gen_fn=gen(["ok [1]."], rec), route="local", online_fn=lambda: False, cloud_fn=gen(["x"]))
        self.assertIn(IBAN_DIGITS, json.dumps(rec))
        self.assertEqual(done(ev)["redacted"], 0)

    def test_agent_answers_stay_masked_from_the_cloud_too(self):
        rec = []
        ev = run(results=BANK, agent_safe=True, cloud_fn=gen([f"Paid from {IBAN} [1]."], rec), online_fn=lambda: True)
        self.assertNotIn(IBAN_DIGITS, json.dumps(rec))         # the model never sees it
        self.assertNotIn(IBAN_DIGITS, json.dumps(ev[0]))       # nor do the sources
        self.assertNotIn(IBAN_DIGITS, done(ev)["answer"])      # nor the answer agents read (as for the local model)
        self.assertGreaterEqual(done(ev)["redacted"], 1)


class FoldersKeptLocal(Base):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="mse-cloud-private-")
        self.coll = config.add_collection(self.root, "privatecloud")["name"]
        config.update_collection(self.coll, {"ask_cloud": False})

    def test_ask_cloud_false_forces_local_whatever_the_route(self):
        for route in (None, "cloud", "auto"):
            remote = []
            ev = run(results=[result("a/supplier.md", "aaaa0001", collection=self.coll)], collections=[self.coll],
                     route=route, cloud_fn=gen(["x"], remote), online_fn=lambda: True)
            self.assertEqual((done(ev)["where"], done(ev)["fallback_reason"]), ("local", "ask_cloud_off"), route)
            self.assertEqual(remote, [])

    def test_a_scope_that_includes_it_stays_local(self):
        remote = []
        ev = run(collections=["all"], cloud_fn=gen(["x"], remote), online_fn=lambda: True)     # it is in All
        self.assertEqual(done(ev)["where"], "local")
        self.assertEqual(remote, [])

    def test_passages_from_it_never_leave_even_when_the_scope_looked_fine(self):
        remote = []
        ev = run(results=[result("a/supplier.md", "aaaa0001", collection=self.coll)], collections=["not-configured"],
                 cloud_fn=gen(["x"], remote), online_fn=lambda: True)
        self.assertEqual((done(ev)["where"], done(ev)["fallback_reason"]), ("local", "ask_cloud_off"))
        self.assertEqual(remote, [])

    def test_the_flag_is_validated_and_shown(self):
        with self.assertRaises(ValueError):
            config.update_collection(self.coll, {"ask_cloud": "no"})
        from mse.server import collections_summary
        self.assertIs(next(c for c in collections_summary() if c["name"] == self.coll)["ask_cloud"], False)
        other = config.add_collection(tempfile.mkdtemp(prefix="mse-cloud-open-"), "opencloud")
        self.assertIs(other["ask_cloud"], True)                # default: allowed


# ---------------------------------------------------------------- stream parsers

class Parsers(unittest.TestCase):
    def lines(self, parts):
        return [ln for p in parts for ln in p.splitlines(keepends=True)]

    def test_sse_framing(self):
        got = list(cloud.sse([b"event: a\r\n", b"data: 1\r\n", b"data: 2\r\n", b"\r\n", b": comment\n", b"\n",
                              "data:x\n", "\n", "data: tail"]))
        self.assertEqual(got, [("a", "1\n2"), (None, "x"), (None, "tail")])

    def test_anthropic(self):
        out = list(cloud.anthropic_events(cloud.sse(self.lines(anthropic_sse(["Paid", " [1]."])))))
        self.assertEqual([m["token"] for m in out if "token" in m], ["Paid", " [1]."])          # no thinking text
        self.assertEqual({k: out[-1][k] for k in ("stop_reason", "prompt_tokens", "tokens")},
                         {"stop_reason": "end_turn", "prompt_tokens": 120, "tokens": 42})

    def test_anthropic_refusal_and_errors(self):
        with self.assertRaisesRegex(CloudError, "refusal"):
            list(cloud.anthropic_events(cloud.sse(self.lines(anthropic_sse([], stop="refusal")))))
        err = 'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n'
        with self.assertRaises(CloudError) as e:
            list(cloud.anthropic_events(cloud.sse(self.lines([err]))))
        self.assertEqual(e.exception.status, 529)
        cut = anthropic_sse(["Paid"])[:7]                       # the connection dropped mid-answer
        with self.assertRaises(CloudError) as e:
            list(cloud.anthropic_events(cloud.sse(self.lines(cut))))
        self.assertTrue(e.exception.network)

    def test_chat(self):
        out = list(cloud.chat_events(cloud.sse(self.lines(chat_sse(["Paid", " [1]."])))))
        self.assertEqual([m["token"] for m in out if "token" in m], ["Paid", " [1]."])          # no reasoning_content
        self.assertEqual((out[-1]["stop_reason"], out[-1]["tokens"]), ("stop", 4))
        with self.assertRaisesRegex(CloudError, "rate limited"):
            list(cloud.chat_events(cloud.sse(['data: {"error":{"message":"rate limited"}}\n', "\n"])))

    def test_responses(self):
        out = list(cloud.responses_events(cloud.sse(self.lines(responses_sse(["Paid", " [1]."])))))
        self.assertEqual([m["token"] for m in out if "token" in m], ["Paid", " [1]."])
        self.assertEqual((out[-1]["stop_reason"], out[-1]["prompt_tokens"]), ("completed", 11))
        failed = 'event: response.failed\ndata: {"type":"response.failed","response":{"error":{"code":"provider_not_enabled","message":"hidden"}}}\n\n'
        with self.assertRaisesRegex(CloudError, "provider_not_enabled"):
            list(cloud.responses_events(cloud.sse(self.lines([failed]))))
        inc = 'data: {"type":"response.incomplete","response":{"incomplete_details":{"reason":"max_output_tokens"}}}\n\n'
        self.assertEqual(list(cloud.responses_events(cloud.sse(self.lines([inc]))))[-1]["stop_reason"],
                         "incomplete: max_output_tokens")
        with self.assertRaises(CloudError):
            list(cloud.responses_events(cloud.sse(self.lines(responses_sse(["x"])[:2]))))

    def test_request_shapes(self):
        msgs = [{"role": "system", "content": "rules"}, {"role": "user", "content": "q"}]
        a = {"kind": "anthropic", "base_url": "https://api.anthropic.com/v1"}
        url, body, headers = cloud.build_request(a, "k", "claude-opus-5-5", msgs, 4096, temperature=0.1, effort="low")
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual((body["system"], body["messages"]), ("rules", [{"role": "user", "content": "q"}]))
        self.assertNotIn("temperature", body)                   # current Claude models reject sampling parameters
        self.assertEqual(body["output_config"], {"effort": "low"})
        self.assertEqual((body["fallbacks"], headers["anthropic-beta"]), ("default", cloud.FALLBACK_BETA))
        self.assertEqual((headers["x-api-key"], headers["anthropic-version"]), ("k", "2023-06-01"))
        _, body, _ = cloud.build_request(a, "k", "claude-haiku-4-5", msgs, 100, effort="low")
        self.assertNotIn("output_config", body)
        self.assertNotIn("fallbacks", body)
        _, body, headers = cloud.build_request({**a, "base_url": "http://127.0.0.1:9/v1"}, "k", "claude-opus-5-5",
                                               msgs, 100)
        self.assertNotIn("fallbacks", body)                     # only on the Claude API itself
        r = {"kind": "openai-responses", "base_url": "http://127.0.0.1:59999/v1"}
        url, body, headers = cloud.build_request(r, "rk", "deepseek/deepseek-chat", msgs, 4096, temperature=0.1)
        self.assertEqual(url, "http://127.0.0.1:59999/v1/responses")
        self.assertEqual((body["instructions"], body["store"], body["max_output_tokens"]), ("rules", False, 4096))
        self.assertEqual(headers["Authorization"], "Bearer rk")
        self.assertNotIn("rk", url)
        c = {"kind": "openai-chat", "base_url": "https://api.deepseek.com/v1"}
        url, body, headers = cloud.build_request(c, None, "deepseek-chat", msgs, 100, temperature=0.1)
        self.assertEqual(body["messages"][0], {"role": "system", "content": "rules"})
        self.assertEqual(body["temperature"], 0.1)
        self.assertNotIn("Authorization", headers)

    def test_effort_support(self):
        for m in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1", "claude-opus-4-7",
                  "claude-sonnet-4-6"):
            self.assertTrue(cloud.supports_effort(m), m)
        for m in ("claude-haiku-4-5", "claude-sonnet-4-5", "deepseek-chat", ""):
            self.assertFalse(cloud.supports_effort(m), m)


# ---------------------------------------------------------------- the HTTP client against local fake servers

class Client(Base):
    def setUp(self):
        super().setUp()
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.close()
        super().tearDown()

    def server(self, reply):
        s = FakeAPI(reply)
        self.servers.append(s)
        return s

    def test_anthropic_stream_end_to_end_through_ask(self):
        api = self.server(lambda path, body: (200, SSE, anthropic_sse(["Paid on 9 April", " [1]."])))
        self.kc.set("anthropic", SECRET)
        cfg = {"ask": {"enabled": True, "providers": {"anthropic": {"base_url": api.base}}}}
        ev = run(cfg=cfg, gen_fn=gen(["local"]))
        d = done(ev)
        self.assertEqual((d["where"], d["answer"]), ("cloud", "Paid on 9 April [1]."))
        self.assertEqual(d["timings"]["prompt_tokens"], 120)
        req = api.requests[0]
        self.assertEqual(req["path"], "/v1/messages")
        self.assertEqual(req["headers"]["x-api-key"], SECRET)                 # the key travels in a header only
        self.assertNotIn(SECRET, req["path"])
        self.assertEqual(req["body"]["model"], "claude-opus-5-5")
        self.assertTrue(req["body"]["stream"])
        self.assertGreaterEqual(req["body"]["max_tokens"], 4096)             # room to think before the answer
        self.assertNotIn(SECRET, json.dumps(ev))

    def test_responses_with_bearer(self):
        api = self.server(lambda path, body: (200, SSE, responses_sse(["Bezahlt", " [1]."])))
        p = {"kind": "openai-responses", "base_url": api.base}
        out = list(cloud.stream(p, FILE_KEY, "acme/acme-large", [{"role": "user", "content": "q"}]))
        self.assertEqual("".join(m.get("token", "") for m in out), "Bezahlt [1].")
        self.assertEqual(api.requests[0]["headers"]["authorization"], f"Bearer {FILE_KEY}")
        self.assertEqual(api.requests[0]["path"], "/v1/responses")

    def test_chat_completions(self):
        api = self.server(lambda path, body: (200, SSE, chat_sse(["ok"])))
        out = list(cloud.stream({"kind": "openai-chat", "base_url": api.base}, None, "llama", [
            {"role": "user", "content": "q"}], temperature=0.1))
        self.assertEqual(out[0], {"token": "ok"})
        self.assertNotIn("authorization", api.requests[0]["headers"])        # a local server without a key

    def test_http_errors_are_scrubbed_of_the_key(self):
        api = self.server(lambda path, body: (401, {"Content-Type": "application/json"}, [json.dumps(
            {"type": "error", "error": {"type": "authentication_error", "message": f"invalid x-api-key {SECRET}"}})]))
        with self.assertRaises(CloudError) as e:
            list(cloud.stream({"kind": "anthropic", "base_url": api.base}, SECRET, "claude-opus-5-5",
                              [{"role": "user", "content": "q"}]))
        self.assertEqual(e.exception.status, 401)
        self.assertIn("authentication_error", str(e.exception))
        self.assertNotIn(SECRET, str(e.exception))

    def test_a_rejected_refusal_fallback_is_retried_once_without_it(self):
        def reply(path, body):
            if "fallbacks" in body:
                return 400, {"Content-Type": "application/json"}, [json.dumps(
                    {"type": "error", "error": {"type": "invalid_request_error", "message": "fallbacks: not allowed"}})]
            return 200, SSE, anthropic_sse(["ok"])
        api = self.server(reply)
        real = cloud.build_request

        def with_fallbacks(*a, **k):                          # as it is built for api.anthropic.com
            url, body, headers = real(*a, **k)
            return url, {**body, "fallbacks": "default"}, {**headers, "anthropic-beta": cloud.FALLBACK_BETA}
        with mock.patch.object(cloud, "build_request", with_fallbacks):
            out = list(cloud.stream({"kind": "anthropic", "base_url": api.base}, SECRET, "claude-opus-5-5",
                                    [{"role": "user", "content": "q"}]))
        self.assertEqual(out[0], {"token": "ok"})
        self.assertEqual(["fallbacks" in r["body"] for r in api.requests], [True, False])
        self.assertNotIn("anthropic-beta", api.requests[1]["headers"])

    def test_redirects_are_refused_so_the_key_stays(self):
        other = self.server(lambda path, body: (200, SSE, anthropic_sse(["x"])))
        api = self.server(lambda path, body: (307, {"Location": other.base + "/messages"}, []))
        with self.assertRaisesRegex(CloudError, "redirect refused"):
            list(cloud.stream({"kind": "anthropic", "base_url": api.base}, SECRET, "claude-opus-5-5",
                              [{"role": "user", "content": "q"}]))
        self.assertEqual(other.requests, [])

    def test_unreachable_host_falls_back_and_marks_it_offline(self):
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()                                          # a port nobody listens on
        self.kc.set("anthropic", SECRET)
        cfg = {"ask": {"enabled": True, "providers": {"anthropic": {"base_url": f"http://127.0.0.1:{port}/v1"}}}}
        ev = run(cfg=cfg)                                     # default: an error, never the local model
        self.assertEqual(ev[-1]["type"], "error")
        self.assertIn("cannot reach 127.0.0.1", ev[-1]["message"])
        providers.forget_cache()
        cfg["ask"]["local_fallback"] = True
        d = done(run(cfg=cfg))
        self.assertEqual(d["where"], "local")
        self.assertTrue(d["fallback_reason"].startswith("error: cannot reach 127.0.0.1"))
        self.assertNotIn(SECRET, json.dumps(d))
        d = done(run(cfg=cfg))                                # within the minute: local at once, no new attempt
        self.assertEqual(d["fallback_reason"], "offline")

    def test_list_models_and_test(self):
        def reply(path, body):
            if path.startswith("/v1/models"):
                return 200, {"Content-Type": "application/json"}, [json.dumps(
                    {"data": [{"id": "claude-opus-5-5"}, {"id": "claude-sonnet-5-5"}], "has_more": False})]
            return 200, SSE, anthropic_sse(["OK"])
        api = self.server(reply)
        config.save({**config.load(), "ask": {"providers": {"anthropic": {"base_url": api.base}}}})
        self.kc.set("anthropic", SECRET)
        r = providers.refresh_models("anthropic")
        self.assertEqual(r["models"], ["claude-opus-5-5", "claude-sonnet-5-5"])
        self.assertEqual(providers.get("anthropic")["models"], ["claude-opus-5-5", "claude-sonnet-5-5"])
        self.assertEqual(api.requests[0]["path"], "/v1/models?limit=1000")
        t = providers.test("anthropic")
        self.assertEqual((t["ok"], t["model"]), (True, "claude-opus-5-5"))
        self.assertIsInstance(t["ms"], int)


# ---------------------------------------------------------------- providers: CRUD, keys, validation

class ProviderCrud(Base):
    def test_builtins_and_custom(self):
        v = providers.list_view()
        ids = [p["id"] for p in v["providers"]]
        self.assertEqual(ids, ["anthropic"])                     # the only built-in one; the others are added by the user
        self.assertEqual({p["status"] for p in v["providers"]}, {"no_key"})
        self.assertEqual((v["route"], v["provider"], v["model"], v["online"]), ("cloud", "anthropic", "claude-opus-5-5",
                                                                                None))
        row = providers.save({"id": "ollama", "kind": "openai-chat", "base_url": "http://127.0.0.1:11434/v1/",
                              "models": ["qwen3:8b", "qwen3:8b", " llama3.3 "]})
        self.assertEqual((row["base_url"], row["models"], row["needs_key"], row["status"]),
                         ("http://127.0.0.1:11434/v1", ["qwen3:8b", "llama3.3"], False, "ok"))
        row = providers.save({"id": "deepseek", "kind": "openai-chat", "base_url": "https://api.deepseek.com/v1",
                              "key": SECRET, "label": "DeepSeek"})
        self.assertEqual((row["has_key"], row["label"]), (True, "DeepSeek"))
        self.assertEqual(self.kc.items["deepseek"], SECRET)
        self.assertNotIn(SECRET, paths.CONFIG.read_text())
        self.assertNotIn(SECRET, json.dumps(providers.list_view()))
        self.assertNotIn(SECRET, json.dumps(ask.settings()))

    def test_validation(self):
        bad = [
            ({"id": "Bad Id", "kind": "openai-chat", "base_url": "https://x.example/v1"}, "id must"),
            ({"id": "local", "kind": "openai-chat", "base_url": "https://x.example/v1"}, "id must"),
            ({"id": "x", "base_url": "https://x.example/v1"}, "kind must"),
            ({"id": "x", "kind": "gemini", "base_url": "https://x.example/v1"}, "kind must"),
            ({"id": "x", "kind": "openai-chat"}, "base_url is required"),
            ({"id": "x", "kind": "openai-chat", "base_url": "ftp://x.example"}, "http"),
            ({"id": "x", "kind": "openai-chat", "base_url": "https://user:pw@x.example/v1"}, "credentials"),
            ({"id": "x", "kind": "openai-chat", "base_url": "https://x.example/v1?key=abc"}, "query"),
            ({"id": "x", "kind": "openai-chat", "base_url": "https://x.example/v1", "models": "a\nb"}, "one line"),
            ({"id": "x", "kind": "openai-chat", "base_url": "https://x.example/v1", "models": [1]}, "model ids"),
            ({"id": "x", "kind": "openai-chat", "base_url": "https://x.example/v1", "secret": 1}, "unknown field"),
            ({"id": "x", "kind": "openai-chat", "base_url": "http://x.example/v1", "key": SECRET}, "https"),
            ({"id": "x", "kind": "openai-chat", "base_url": "https://x.example/v1", "key": "two words"}, "one line"),
            ({"id": "anthropic", "kind": "openai-chat"}, "built in"),
            ({"id": "x", "kind": "openai-chat", "base_url": "https://x.example/v1", "key_file": "relative/key"}, "absolute path"),
            ({"id": "x", "kind": "openai-chat", "base_url": "https://x.example/v1", "key_file": 5}, "key_file must"),
            ({"id": "x", "kind": "openai-chat", "base_url": "https://x.example/v1", "key_file": "~/k\nz"}, "key_file must"),
            ({"id": "x", "kind": "openai-chat", "base_url": "https://x.example/v1", "training_models": "a" * 300}, "one line"),
            ({"id": "x", "kind": "openai-chat", "base_url": "https://x.example/v1", "training_models": [1]}, "training_models"),
            ({"id": "x", "kind": "openai-chat", "base_url": "https://x.example/v1", "training_models": ["a"] * 0 + [str(i) for i in range(21)]}, "at most 20"),
            ({"id": "x", "kind": "openai-chat", "base_url": "https://x.example/v1", "auto_models": "yes"}, "auto_models"),
        ]
        for body, msg in bad:
            with self.assertRaises(ValueError, msg=repr(body)) as e:
                providers.save(body)
            self.assertIn(msg, str(e.exception), body)
            self.assertNotIn(SECRET, str(e.exception))
        self.assertEqual(self.kc.items, {})                     # nothing reached the Keychain
        self.assertNotIn('"x"', paths.CONFIG.read_text())

    def test_a_provider_can_carry_a_key_file_training_patterns_and_auto_models(self):
        row = self.add_router()
        self.assertEqual((row["key_file"], row["training_models"], row["auto_models"]), (self.key_file, ["*-train"], False))
        self.assertEqual((row["has_key"], row["key_source"], row["needs_key"], row["builtin"]), (True, "file", True, False))
        raw = json.loads(paths.CONFIG.read_text())["ask"]["providers"]["localrouter"]
        self.assertEqual((raw["key_file"], raw["training_models"]), (self.key_file, ["*-train"]))
        self.assertNotIn(FILE_KEY, paths.CONFIG.read_text())             # the path is stored, never the content
        self.assertEqual(self.kc.items, {})
        row = providers.save({"id": "localrouter", "auto_models": True, "training_models": [" *-train ", "x?", "x?"]})
        self.assertEqual((row["training_models"], row["auto_models"]), (["*-train", "x?"], True))
        row = providers.save({"id": "localrouter", "key_file": "", "training_models": [], "auto_models": False})
        self.assertEqual((row["key_file"], row["training_models"], row["has_key"], row["needs_key"]), (None, [], False, False))
        raw = json.loads(paths.CONFIG.read_text())["ask"]["providers"]["localrouter"]
        self.assertFalse({"key_file", "training_models", "auto_models"} & set(raw))
        self.assertEqual(providers.save({"id": "localrouter", "key_file": "~/keys/lr"})["key_file"], "~/keys/lr")

    def test_a_key_file_of_a_builtin_provider_and_a_malformed_stored_value(self):
        row = providers.save({"id": "anthropic", "key_file": self.key_file, "training_models": ["claude-x*"]})
        self.assertEqual((row["key_file"], row["training_models"]), (self.key_file, ["claude-x*"]))
        cfg = config.load()
        cfg["ask"]["providers"]["anthropic"]["training_models"] = {"not": "a list"}
        cfg["ask"]["providers"]["anthropic"]["key_file"] = "relative"
        config.save(cfg)
        a = providers.get("anthropic")
        self.assertNotIn("training_models", a)                     # a hand-edited bad value is ignored, never fatal
        self.assertNotIn("key_file", a)

    def test_clean_key(self):
        self.assertEqual(providers.clean_key("  abc123  \n"), "abc123")
        for bad in ("", "  ", "two words", "a\nb", 5, "x" * 9000):
            with self.assertRaises(ValueError):
                providers.clean_key(bad)

    def test_builtin_overrides_and_remove(self):
        providers.save({"id": "anthropic", "label": "Claude", "models": ["claude-opus-5-5", "my-model"]})
        a = providers.get("anthropic")
        self.assertEqual((a["kind"], a["label"], a["models"]), ("anthropic", "Claude", ["claude-opus-5-5", "my-model"]))
        with self.assertRaises(ValueError):
            providers.remove("anthropic")
        with self.assertRaises(providers.UnknownProvider):
            providers.remove("nope")
        providers.save({"id": "acme", "kind": "openai-responses", "base_url": "https://acme.example/v1", "key": SECRET,
                        "models": ["acme-large"]})
        with mock.patch.object(embedder, "_pool", None):
            config.update_settings({"ask": {"provider": "acme"}})
        self.assertEqual(ask.settings()["model"], "acme-large")             # a provider switch picks its model
        r = providers.remove("acme", forget=True)
        self.assertEqual((r["provider"], r["model"], r["key_forgotten"]), ("anthropic", "claude-opus-5-5", True))
        self.assertNotIn("acme", self.kc.items)
        providers.save({"id": "anthropic", "key": SECRET})
        providers.save({"id": "anthropic", "forget_key": True})
        self.assertNotIn("anthropic", self.kc.items)

    def test_settings_validation(self):
        bad = [{"route": "moon"}, {"provider": "nope"}, {"model": ""}, {"model": "/Users/alex/model"},
               {"cloud_redact": "yes"}, {"providers": {"x": {}}}]
        for change in bad:
            with self.assertRaises(ValueError, msg=repr(change)):
                config.update_settings({"ask": change})
        self.assertEqual(config.load()["ask"].get("providers"), None)     # defaults are never written into config
        self.add_router()
        with mock.patch.object(embedder, "_pool", None):
            config.update_settings({"ask": {"route": "cloud", "provider": "localrouter"}})
        s = ask.settings()
        self.assertEqual((s["route"], s["provider"], s["model"]), ("cloud", "localrouter", "acme-large"))

    def test_env_fallback_only_when_asked(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": SECRET}):
            self.assertIsNone(providers.get_key("anthropic"))                    # the daemon never reads it
            self.assertEqual(providers.get_key("anthropic", allow_env=True), SECRET)   # the CLI may
            self.assertEqual(providers.target(ask.settings(ENABLED))[1], "no_key")
        self.assertEqual(providers.env_name("my-proxy"), "MSE_KEY_MY_PROXY")

    def test_the_real_keychain_wrapper_without_a_helper(self):
        kc = providers.HelperKeychain()
        with mock.patch.object(paths, "HELPER", paths.DATA / "bin" / "no-such-helper"):
            self.assertIsNone(kc.get("anthropic"))                   # reads: no helper, no keys, no exception
            self.assertFalse(kc.has("anthropic"))
            with self.assertRaises(providers.KeychainError):
                kc.set("anthropic", SECRET)

    def test_the_helper_gets_the_secret_on_stdin_never_in_argv(self):
        kc = providers.HelperKeychain()
        fake = mock.Mock(returncode=0, stdout=b'{"ok": true}\n')
        with mock.patch.object(paths, "HELPER", paths.CONFIG), mock.patch("subprocess.run", return_value=fake) as r:
            kc.set("anthropic", SECRET)
        argv, kwargs = r.call_args.args[0], r.call_args.kwargs
        self.assertEqual(argv[1:], ["keychain", "set", "anthropic"])
        self.assertNotIn(SECRET, " ".join(argv))
        self.assertEqual(kwargs["input"], SECRET.encode())
        with mock.patch.object(paths, "HELPER", paths.CONFIG), \
                mock.patch("subprocess.run", return_value=mock.Mock(stdout=b'{"error": "unknown command keychain"}')):
            with self.assertRaisesRegex(providers.KeychainError, "rebuild"):
                kc.get("anthropic")
        with mock.patch.object(paths, "HELPER", paths.CONFIG), \
                mock.patch("subprocess.run", return_value=mock.Mock(stdout=b'{"found": false}\n')) as r:
            self.assertIsNone(kc.get("anthropic"))
        self.assertIn("--no-ui", r.call_args.args[0])                 # background reads never show a dialog


# ---------------------------------------------------------------- the HTTP API

class Api(Base):
    @classmethod
    def setUpClass(cls):
        from mse import server
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, method, path, body=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-MSE-Token": config.load()["token"], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_crud_over_http_never_returns_a_key(self):
        bodies = []
        code, body = self.call("POST", "/api/ask/providers", {"id": "anthropic", "key": SECRET})
        bodies.append(body)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["has_key"])
        code, body = self.call("POST", "/api/ask/providers", {"id": "deepseek", "kind": "openai-chat",
                                                               "base_url": "https://api.deepseek.com/v1", "key": SECRET,
                                                               "models": ["deepseek-chat"]})
        bodies.append(body)
        self.assertEqual(code, 200)
        for path in ("/api/ask/providers", "/api/settings", "/api/status", "/api/collections"):
            code, body = self.call("GET", path)
            self.assertEqual(code, 200, path)
            bodies.append(body)
        v = json.loads(bodies[2])
        self.assertEqual({p["id"]: p["status"] for p in v["providers"]},
                         {"anthropic": "ok", "deepseek": "ok"})
        self.assertEqual(v["online"], True)
        code, body = self.call("POST", "/api/ask/providers", {"id": "anthropic", "kind": "openai-chat"})
        self.assertEqual(code, 400)
        code, body = self.call("POST", "/api/ask/providers/anthropic/remove", {})
        self.assertEqual(code, 400)
        code, body = self.call("POST", "/api/ask/providers/nope/test", {})
        self.assertEqual(code, 404)
        code, body = self.call("POST", "/api/ask/providers/deepseek/remove", {"forget_key": True})
        self.assertEqual((code, json.loads(body)["key_forgotten"]), (200, True))
        for b in bodies + [body, paths.CONFIG.read_text()]:
            self.assertNotIn(SECRET, b)
            self.assertNotIn(SECRET[:12], b)                     # not even a prefix

    def test_test_endpoint_reports_failures_without_the_key(self):
        api = FakeAPI(lambda path, body: (401, {"Content-Type": "application/json"}, [json.dumps(
            {"error": {"type": "authentication_error", "message": f"bad key {SECRET}"}})]))
        try:
            self.call("POST", "/api/ask/providers", {"id": "anthropic", "key": SECRET, "base_url": api.base})
            code, body = self.call("POST", "/api/ask/providers/anthropic/test", {})
            self.assertEqual(code, 200)
            r = json.loads(body)
            self.assertEqual((r["ok"], r["model"]), (False, "claude-opus-5-5"))
            self.assertIn("401", r["error"])
            self.assertNotIn(SECRET, body)
            code, body = self.call("GET", "/api/ask/providers")
            row = next(p for p in json.loads(body)["providers"] if p["id"] == "anthropic")
            self.assertEqual(row["status"], "error")
            self.assertNotIn(SECRET, body)
            code, body = self.call("POST", "/api/ask/providers/anthropic/models", {})
            self.assertEqual(code, 502)
            self.assertNotIn(SECRET, body)
        finally:
            api.close()

    def test_settings_route_and_ask_route_param(self):
        code, body = self.call("POST", "/api/settings", {"ask": {"route": "local", "model": "claude-sonnet-5-5"}})
        self.assertEqual(code, 200)
        a = json.loads(body)["ask"]
        self.assertEqual((a["route"], a["model"], a["cloud_redact"]), ("local", "claude-sonnet-5-5", True))
        self.assertEqual(self.call("POST", "/api/settings", {"ask": {"route": "sometimes"}})[0], 400)
        self.assertEqual(self.call("POST", "/api/ask", {"q": "x", "route": "moon"})[0], 400)
        seen = {}

        def fake_ask(q, collections=None, filters=None, agent_safe=False, route=None):
            seen["route"] = route
            return iter([{"type": "done", "citations": [], "answer": "x", "where": "local"}])
        with mock.patch.object(ask, "ask", side_effect=fake_ask):
            self.assertEqual(self.call("POST", "/api/ask", {"q": "x", "route": "cloud"})[0], 200)
        self.assertEqual(seen["route"], "cloud")


# ---------------------------------------------------------------- agents: the MCP tool under the lock

CLOUD_EVENTS = lambda coll: [  # noqa: E731
    {"type": "sources", "sources": [{"n": 1, "docid": "#aaaa0001", "collection": coll, "path": "a/supplier.md",
                                     "title": "supplier", "page": None, "ts": None, "snippet": "x"}]},
    {"type": "token", "text": "Paid [1]."},
    {"type": "done", "citations": [1], "model": "claude-opus-5-5", "provider": "anthropic", "where": "cloud",
     "redacted": 0, "answer": "Paid [1].", "answered": True, "timings": {}}]


class McpCloud(Base):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="mse-mcp-cloud-")
        os.makedirs(os.path.join(self.root, "sub"))
        self.coll = config.add_collection(self.root, "mcpcloud")["name"]
        self.other = config.add_collection(tempfile.mkdtemp(prefix="mse-mcp-other-"), "mcpother")["name"]

    def client(self, events):
        c = mock.Mock()

        def ask_fn(q, colls, agent_safe=True, **kw):
            c.seen = (q, colls, agent_safe, kw)
            return iter(events)
        c.ask = ask_fn
        return c

    def test_history_param_passes_and_the_lock_holds(self):
        cwd = os.path.join(self.root, "sub")
        lock = mcp.Lock(cwd)
        tool = next(t for t in mcp.tools_for(lock) if t["name"] == "ask")
        self.assertIn("history", tool["inputSchema"]["properties"])
        c = self.client(CLOUD_EVENTS(self.coll))
        h = [{"q": "How much did the supplier charge?", "a": "350,00 EUR [1]."}]
        out = mcp.call_tool("ask", {"question": "and when was it paid?", "history": h}, c, cwd, lock)
        self.assertEqual(c.seen, ("and when was it paid?", [self.coll], True, {"history": h}))
        self.assertIn("the cloud model claude-opus-5-5 (anthropic)", out)
        for spec in ("all", self.other):
            with self.assertRaises(PermissionError):
                mcp.call_tool("ask", {"question": "q", "collection": spec, "history": h}, c, cwd, lock)
        with self.assertRaises(PermissionError):                 # a shortcut in the question is no way around it
            mcp.call_tool("ask", {"question": f"{self.other}: q", "history": h}, c, cwd, lock)
        leak = self.client(CLOUD_EVENTS(self.other))            # a backend that answered from elsewhere
        with self.assertRaises(PermissionError):
            mcp.call_tool("ask", {"question": "q"}, leak, cwd, lock)
        reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "ask", "arguments": {"question": "q", "history": "not a list"}}}, c, cwd, lock)
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("history", reply["result"]["content"][0]["text"])

    def test_without_history_the_call_is_as_before(self):
        c = self.client(CLOUD_EVENTS(self.coll))
        mcp.call_tool("ask", {"question": "q"}, c, os.path.join(self.root, "sub"))
        self.assertEqual(c.seen[3], {})

    def test_agent_answers_through_the_daemon_stay_masked(self):
        rec = []
        out_events = list(ask.ask("Who paid?", agent_safe=True, search_fn=search_fn(BANK), chunks_fn=chunks,
                                  cfg=ENABLED, cloud_fn=gen([f"Paid from {IBAN} [1]."], rec), online_fn=lambda: True))
        text_out = mcp.format_answer(out_events)
        self.assertNotIn(IBAN_DIGITS, text_out)
        self.assertNotIn(IBAN_DIGITS, json.dumps(rec))
        self.assertIn("cloud model", text_out)


# ---------------------------------------------------------------- the CLI

class Cli(Base):
    def run_cli(self, argv, events=None, up=True, stdin=None):
        from mse import cli
        fake = mock.Mock()
        fake.ask = mock.Mock(return_value=iter(events or [
            {"type": "sources", "sources": []}, {"type": "token", "text": "ok"},
            {"type": "done", "citations": [], "model": "claude-opus-5-5", "provider": "anthropic", "where": "cloud",
             "redacted": 2, "answer": "ok", "answered": True, "timings": {"ttft_ms": 1, "total_ms": 2}}]))
        fake._call = mock.Mock(side_effect=lambda method, path, params=None, body=None: {"id": "anthropic"})
        out = io.StringIO()
        with mock.patch.object(cli, "daemon_up", return_value=up), mock.patch.object(cli, "client", return_value=fake), \
                mock.patch.object(cli, "TTY", False), contextlib.redirect_stdout(out), \
                mock.patch("sys.stdin", stdin or io.StringIO("")):
            cli.main(argv)
        return fake, out.getvalue()

    def test_ask_route_flags(self):
        fake, out = self.run_cli(["ask", "q", "-c", "all", "--json"])
        self.assertNotIn("route", fake.ask.call_args.kwargs)
        d = json.loads(out)
        self.assertEqual((d["where"], d["provider"], d["redacted"]), ("cloud", "anthropic", 2))
        fake, _ = self.run_cli(["ask", "q", "-c", "all", "--local"])
        self.assertEqual(fake.ask.call_args.kwargs["route"], "local")
        fake, _ = self.run_cli(["ask", "q", "-c", "all", "--cloud"])
        self.assertEqual(fake.ask.call_args.kwargs["route"], "cloud")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_cli(["ask", "q", "--local", "--cloud"])

    def test_set_key_reads_stdin_and_never_prints_it(self):
        fake, out = self.run_cli(["providers", "set-key", "anthropic"], stdin=io.StringIO(SECRET + "\n"))
        method, path = fake._call.call_args.args[:2]
        self.assertEqual((method, path), ("POST", "/api/ask/providers"))
        self.assertEqual(fake._call.call_args.kwargs["body"], {"id": "anthropic", "key": SECRET})
        self.assertNotIn(SECRET, out)
        self.assertIn("Keychain", out)
        _, out = self.run_cli(["providers", "set-key", "anthropic"], up=False, stdin=io.StringIO(SECRET + "\n"))
        self.assertEqual(self.kc.items["anthropic"], SECRET)       # daemon down: straight to the Keychain
        self.assertNotIn(SECRET, out)
        with self.assertRaises(SystemExit):
            self.run_cli(["providers", "set-key", "nope"], stdin=io.StringIO(SECRET))

    def test_set_key_from_env(self):
        self.add_router()
        with mock.patch.dict(os.environ, {"MSE_KEY_LOCALROUTER": FILE_KEY + "-env"}):
            self.run_cli(["providers", "set-key", "localrouter", "--from-env"], up=False)
        self.assertEqual(self.kc.items["localrouter"], FILE_KEY + "-env")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_cli(["providers", "set-key", "localrouter", "--from-env"], up=False)    # the variable is empty again

    def test_list_use_and_add_without_the_daemon(self):
        _, out = self.run_cli(["providers"], up=False)
        self.assertIn("anthropic", out)
        self.assertNotIn("localrouter", out)
        _, out = self.run_cli(["providers", "add", "localrouter", "--kind", "openai-responses", "--base-url", ROUTER_URL,
                               "--model", "acme-large", "--key-file", self.key_file, "--training-models", "*-train",
                               "--auto-models"], up=False)
        self.assertIn("added localrouter", out)
        row = providers.get("localrouter")
        self.assertEqual((row["kind"], row["key_file"], row["training_models"], row["auto_models"]),
                         ("openai-responses", self.key_file, ["*-train"], True))
        _, out = self.run_cli(["providers", "add", "localrouter", "--key-file", ""], up=False)       # change one field only
        self.assertIn("updated localrouter", out)
        self.assertEqual((providers.get("localrouter")["base_url"], providers.get("localrouter").get("key_file")), (ROUTER_URL, None))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_cli(["providers", "add", "brandnew"], up=False)                                # a new one needs --base-url
        _, out = self.run_cli(["providers", "use", "localrouter", "acme-small"], up=False)
        self.assertIn("localrouter acme-small", out)
        self.assertEqual(ask.settings()["model"], "acme-small")
        _, out = self.run_cli(["providers", "use", "local"], up=False)
        self.assertEqual(ask.settings()["route"], "local")
        _, out = self.run_cli(["providers", "add", "lmstudio", "--base-url", "http://127.0.0.1:1234/v1",
                               "--model", "qwen3-30b"], up=False)
        self.assertIn("added lmstudio", out)
        self.assertEqual(providers.get("lmstudio")["models"], ["qwen3-30b"])
        _, out = self.run_cli(["providers", "list", "--json"], up=False)
        self.assertEqual([p["id"] for p in json.loads(out)["providers"]], ["anthropic", "localrouter", "lmstudio"])


if __name__ == "__main__":
    unittest.main()
