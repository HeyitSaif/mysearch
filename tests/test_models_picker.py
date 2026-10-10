"""Model selection for Ask: the real Claude model list (paged), a custom provider whose key is read from a key file
(read at call time, one-click connect, automatic model list, offline hint, never sent to another machine over http), a
second custom provider with its own key, the training guard (a provider's training_models patterns), the local model
picker, the CLI. The providers here are fictional ("localrouter", "acme"). Mocked HTTP only (fake servers on 127.0.0.1);
the Keychain is an in-memory fake; the key file is a fake file under a temporary HOME and its content must never appear
in any output.
Run: .venv/bin/python -m unittest discover -s tests"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:                       # run as a package (pytest) or as top-level modules (unittest discover -s tests)
    from . import test_cloud as tc
except ImportError:
    import test_cloud as tc
from mse import ask, cloud, config, paths, providers
from mse.cloud import CloudError

JSON = {"Content-Type": "application/json"}
TRAIN = tc.TRAIN


def models_page(ids, has_more=False, last_id=None, names=True):
    return json.dumps({"data": [{"type": "model", "id": i, **({"display_name": f"Name of {i}", "created_at": "2025-03-01T00:00:00Z"}
                                                              if names else {})} for i in ids],
                       "has_more": has_more, "first_id": ids[0] if ids else None, "last_id": last_id or (ids[-1] if ids else None)})


class HttpCase(tc.Base):
    """The real HTTP handler on a free port; call() sends the usual token."""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
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


class CliCase(tc.Base):
    def run_cli(self, argv, up=True, stdin=None):
        from mse import cli
        fake = mock.Mock()
        fake._call = mock.Mock(side_effect=lambda method, path, params=None, body=None: {"id": "anthropic"})
        out = io.StringIO()
        with mock.patch.object(cli, "daemon_up", return_value=up), mock.patch.object(cli, "client", return_value=fake), \
                mock.patch.object(cli, "TTY", False), contextlib.redirect_stdout(out), \
                mock.patch("sys.stdin", stdin or io.StringIO("")):
            cli.main(argv)
        return fake, out.getvalue()


class Anthropic(tc.Base):
    def setUp(self):
        super().setUp()
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.close()
        super().tearDown()

    def api(self, reply):
        s = tc.FakeAPI(reply)
        self.servers.append(s)
        config.save({**config.load(), "ask": {"providers": {"anthropic": {"base_url": s.base}}}})
        return s

    def test_every_page_of_the_models_api_is_followed(self):
        def reply(path, body):
            if "after_id=claude-b" in path:
                return 200, JSON, [models_page(["claude-c", "claude-haiku-5-5"])]
            if "after_id=" in path:
                return 500, JSON, ["{}"]
            return 200, JSON, [models_page(["claude-a", "claude-b"], has_more=True)]
        api = self.api(reply)
        self.kc.set("anthropic", tc.SECRET)
        r = providers.refresh_models("anthropic")
        self.assertEqual(r["models"], ["claude-a", "claude-b", "claude-c", "claude-haiku-5-5"])
        self.assertEqual(r["fetched"], 4)
        self.assertEqual([q["path"] for q in api.requests],
                         ["/v1/models?limit=1000", "/v1/models?limit=1000&after_id=claude-b"])
        self.assertEqual(api.requests[0]["headers"]["x-api-key"], tc.SECRET)
        self.assertEqual(api.requests[0]["headers"]["anthropic-version"], "2023-06-01")
        p = providers.get("anthropic")
        self.assertEqual(p["models"], r["models"])                    # exactly what the API listed
        self.assertEqual(p["models_meta"]["claude-haiku-5-5"]["name"], "Name of claude-haiku-5-5")
        self.assertEqual(p["models_meta"]["claude-a"]["created"], "2025-03-01")
        self.assertIsInstance(p["models_at"], float)
        row = providers.list_view()["providers"][0]
        self.assertEqual((row["models_at"], row["model_info"]["claude-a"]["name"]), (p["models_at"], "Name of claude-a"))
        self.assertNotIn(tc.SECRET, paths.CONFIG.read_text())

    def test_a_page_that_repeats_its_cursor_ends_the_walk(self):
        api = self.api(lambda path, body: (200, JSON, [models_page(["m1", "m2"], has_more=True)]))
        self.kc.set("anthropic", tc.SECRET)
        self.assertEqual(providers.refresh_models("anthropic")["models"], ["m1", "m2"])
        self.assertEqual(len(api.requests), 2)

    def test_the_active_model_is_not_added_to_the_listed_ones(self):
        self.api(lambda path, body: (200, JSON, [models_page(["claude-sonnet-5-5"])]))
        self.kc.set("anthropic", tc.SECRET)
        self.assertEqual(providers.get("anthropic")["models"][0], "claude-opus-5-5")
        self.assertEqual(providers.refresh_models("anthropic")["models"], ["claude-sonnet-5-5"])
        self.assertEqual(ask.settings()["model"], "claude-opus-5-5")   # still the chosen one, the picker shows it anyway

    def test_an_empty_or_failing_list_keeps_the_old_one(self):
        self.api(lambda path, body: (200, JSON, [models_page([])]))
        self.kc.set("anthropic", tc.SECRET)
        before = providers.get("anthropic")["models"]
        with self.assertRaises(CloudError):
            providers.refresh_models("anthropic")
        self.assertEqual(providers.get("anthropic")["models"], before)
        self.servers[0].reply = lambda path, body: (401, JSON, [json.dumps({"error": {"type": "authentication_error",
                                                                                     "message": "invalid x-api-key"}})])
        with self.assertRaises(CloudError) as cm:
            providers.refresh_models("anthropic")
        self.assertNotIn(tc.SECRET, str(cm.exception))

    def test_builtin_claude_ids_are_the_documented_current_ones(self):
        self.assertEqual(config.BUILTIN_PROVIDERS["anthropic"]["models"],
                         ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"])


class KeyFileCase(HttpCase):
    """A temporary HOME holding a fake key file for the fictional provider "localrouter" (~/.config/localrouter/key; a
    real key file is never read), and fake servers that answer for it."""
    IDS = ["acme/acme-large", "acme/acme-small", TRAIN, "other-lab/thing-1", "acme/acme-large"]
    LISTED = ["acme/acme-large", "acme/acme-small", TRAIN, "other-lab/thing-1"]            # once each, in the server's order

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.secret_file = self.home / ".config" / "localrouter" / "key"
        self.secret_file.parent.mkdir(parents=True)
        self.secret_file.write_text(tc.FILE_KEY + "\n")
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()
        self.servers = []

    def tearDown(self):
        self.env.stop()
        for s in self.servers:
            s.close()
        self.tmp.cleanup()
        super().tearDown()

    def router(self, ids=None, status=200, base_url=None, **entry):
        ids = ids or self.IDS
        body = {"object": "list", "data": [{"id": i, "object": "model", "owned_by": i.split("/")[0]} for i in ids],
                "models": [{"slug": i, "display_name": i} for i in ids]}
        s = tc.FakeAPI(lambda path, b: (status, JSON, [json.dumps(body)]) if path.startswith("/v1/models")
                       else (404, JSON, ["{}"]))
        self.servers.append(s)
        config.save({**config.load(), "ask": {"providers": {"localrouter": {
            "kind": "openai-responses", "label": "Local Router", "base_url": base_url or s.base, "models": [],
            "key_file": "~/.config/localrouter/key", "training_models": ["*-train"], "auto_models": True, **entry}}}})
        return s


class KeyFile(KeyFileCase):
    def test_models_are_parsed_from_the_providers_list_with_the_bearer_key(self):
        api = self.router()
        self.kc.set("localrouter", tc.FILE_KEY)
        r = providers.refresh_models("localrouter")
        self.assertEqual(r["models"], self.LISTED)
        self.assertEqual(api.requests[0]["path"], "/v1/models")
        self.assertEqual(api.requests[0]["headers"]["authorization"], f"Bearer {tc.FILE_KEY}")
        self.assertEqual(providers.get("localrouter")["models_meta"]["other-lab/thing-1"], {"owned_by": "other-lab"})

    def test_connect_reads_the_key_file_stores_it_and_never_shows_it(self):
        api = self.router()
        r = providers.connect("localrouter")
        self.assertEqual(self.kc.items["localrouter"], tc.FILE_KEY)
        self.assertEqual((r["connected"], r["fetched"], r["file"]), (True, 4, "~/.config/localrouter/key"))
        self.assertEqual(api.requests[0]["headers"]["authorization"], f"Bearer {tc.FILE_KEY}")
        for blob in (json.dumps(r), paths.CONFIG.read_text(), json.dumps(providers.list_view()), json.dumps(ask.settings())):
            self.assertNotIn(tc.FILE_KEY, blob)
            self.assertNotIn(tc.FILE_KEY[:10], blob)

    def test_connect_with_the_server_not_running_still_stores_the_key(self):
        s = tc.FakeAPI(lambda p, b: (200, JSON, ["{}"]))
        base = s.base
        s.close()
        self.router(base_url=base)
        r = providers.connect("localrouter")
        self.assertTrue(r["connected"])
        self.assertIn("models_error", r)
        self.assertNotIn(tc.FILE_KEY, json.dumps(r))
        self.assertEqual(self.kc.items["localrouter"], tc.FILE_KEY)

    def test_connect_errors_do_not_leak_the_file(self):
        self.router()
        self.secret_file.write_text("short secret xyz\n")
        with self.assertRaises(ValueError) as cm:
            providers.connect("localrouter")
        self.assertNotIn("short", str(cm.exception))
        self.assertNotIn("localrouter", self.kc.items)
        self.secret_file.unlink()
        with self.assertRaises(ValueError) as cm:
            providers.connect("localrouter")
        self.assertIn("no key file", str(cm.exception))
        providers.save({"id": "plain", "kind": "openai-chat", "base_url": "http://127.0.0.1:59998/v1"})
        with self.assertRaises(ValueError) as cm:
            providers.connect("plain")
        self.assertIn("no key_file", str(cm.exception))
        with self.assertRaises(providers.UnknownProvider):
            providers.connect("nope")

    def test_connect_refuses_to_store_a_key_for_a_plain_http_remote_server(self):
        self.router(base_url="http://remote.example/v1")
        with self.assertRaises(ValueError) as cm:
            providers.connect("localrouter")
        self.assertIn("https", str(cm.exception))
        self.assertEqual(self.kc.items, {})

    def test_the_key_file_path_may_be_absolute_or_start_with_a_tilde(self):
        self.router()
        self.assertEqual(providers.key_file_path(providers.get("localrouter")), self.secret_file)
        providers.save({"id": "localrouter", "key_file": str(self.secret_file)})
        self.assertEqual(providers.key_source("localrouter"), "file")
        self.assertEqual(providers.get_key("localrouter"), tc.FILE_KEY)

    def test_connect_over_http(self):
        self.router()
        code, body = self.call("POST", "/api/ask/providers/localrouter/connect", {})
        self.assertEqual(code, 200, body)
        d = json.loads(body)
        self.assertEqual((d["ok"], d["connected"], d["fetched"]), (True, True, 4))
        self.assertNotIn(tc.FILE_KEY, body)
        code, body = self.call("POST", "/api/ask/providers/anthropic/connect", {})        # no key_file
        self.assertEqual(code, 400)
        self.assertIn("no key_file", body)
        self.secret_file.unlink()
        code, body = self.call("POST", "/api/ask/providers/localrouter/connect", {})
        self.assertEqual(code, 400)
        self.assertNotIn(tc.FILE_KEY, body)
        self.assertEqual(self.call("POST", "/api/ask/providers/nope/connect", {})[0], 404)


class KeyFileSetup(KeyFileCase):
    """A provider with a key_file needs no key from the user: the file is read when a call needs it."""
    K2 = "Second" + "x" * 30 + "Key_9876543210"

    def row(self):
        return next(p for p in providers.list_view()["providers"] if p["id"] == "localrouter")

    def blobs(self):
        _, api = self.call("GET", "/api/ask/providers")
        _, st = self.call("GET", "/api/status")
        _, se = self.call("GET", "/api/settings")
        _, cli = None, CliCase.run_cli(self, ["providers"], up=False)[1]
        return [api, st, se, cli, json.dumps(providers.list_view()), json.dumps(ask.settings()), paths.CONFIG.read_text()]

    def assertNoSecret(self, *keys):
        for blob in self.blobs():
            for k in keys or (tc.FILE_KEY,):
                self.assertNotIn(k, blob)
                self.assertNotIn(k[:10], blob)

    def test_resolution_order_keychain_then_key_file_then_environment_then_none(self):
        self.router()
        self.assertEqual((providers.get_key("localrouter"), providers.key_source("localrouter")), (tc.FILE_KEY, "file"))
        self.kc.set("localrouter", self.K2)
        providers.forget_cache()
        self.assertEqual((providers.get_key("localrouter"), providers.key_source("localrouter")), (self.K2, "keychain"))
        self.assertTrue(providers.has_key("localrouter"))
        self.assertTrue(providers.forget_key("localrouter"))
        self.assertEqual((providers.get_key("localrouter"), providers.key_source("localrouter")), (tc.FILE_KEY, "file"))
        with mock.patch.dict(os.environ, {"MSE_KEY_LOCALROUTER": "env" + self.K2}):
            self.assertEqual(providers.get_key("localrouter", allow_env=True), tc.FILE_KEY)    # the file beats the environment
            self.secret_file.unlink()
            providers.forget_cache()
            self.assertEqual((providers.get_key("localrouter", allow_env=True), providers.key_source("localrouter", True)),
                             ("env" + self.K2, "env"))
            self.assertIsNone(providers.get_key("localrouter"))        # the service never reads the environment
        self.assertEqual((providers.get_key("localrouter"), providers.key_source("localrouter"),
                          providers.has_key("localrouter")), (None, "none", False))

    def test_a_key_file_belongs_to_its_provider_only(self):
        self.router()
        self.assertEqual(providers.key_source("anthropic"), "none")
        self.assertIsNone(providers.get_key("anthropic"))
        providers.save({"id": "other", "kind": "openai-chat", "base_url": "http://127.0.0.1:59998/v1"})
        self.assertEqual(providers.key_source("other"), "none")

    def test_status_reports_the_source_and_ok(self):
        self.router()
        r = self.row()
        self.assertEqual((r["has_key"], r["key_source"], r["status"]), (True, "file", "ok"))
        self.assertEqual((r["key_file"], r["training_models"], r["needs_key"]), ("~/.config/localrouter/key", ["*-train"], True))
        self.assertNotIn("hint", r)
        code, body = self.call("GET", "/api/ask/providers")
        d = {p["id"]: p for p in json.loads(body)["providers"]}
        self.assertEqual((d["localrouter"]["has_key"], d["localrouter"]["key_source"], d["localrouter"]["status"]),
                         (True, "file", "ok"))
        self.assertEqual((d["anthropic"]["key_source"], d["anthropic"]["status"]), ("none", "no_key"))
        self.kc.set("localrouter", self.K2)
        providers.forget_cache()
        self.assertEqual(self.row()["key_source"], "keychain")

    def test_a_changed_file_is_read_again_and_an_unchanged_one_is_not(self):
        self.router()
        real = providers.clean_key
        with mock.patch.object(providers, "clean_key", side_effect=real) as spy:
            for _ in range(3):
                self.assertEqual(providers.get_key("localrouter"), tc.FILE_KEY)
                providers.has_key("localrouter")
            self.assertEqual(spy.call_count, 1)                          # read once, then served from memory
            st = os.stat(self.secret_file)
            self.secret_file.write_text(self.K2 + "\n")                  # the key was rotated
            os.utime(self.secret_file, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
            self.assertEqual(providers.get_key("localrouter"), self.K2)
            self.assertEqual(spy.call_count, 2)
            self.assertEqual(providers.get_key("localrouter"), self.K2)
            self.assertEqual(spy.call_count, 2)
        self.secret_file.write_text(self.K2 + "x\n")                    # same mtime sets are not needed: size changed
        self.assertEqual(providers.get_key("localrouter"), self.K2 + "x")

    def test_the_key_is_sent_as_the_bearer_token_and_never_shown(self):
        api = self.router()
        r = providers.refresh_models("localrouter")
        self.assertEqual(r["fetched"], 4)
        self.assertEqual(api.requests[0]["headers"]["authorization"], f"Bearer {tc.FILE_KEY}")
        self.assertNotIn("localrouter", self.kc.items)                  # nothing was copied to the Keychain
        self.assertNoSecret()
        self.assertNotIn(tc.FILE_KEY, json.dumps(r))

    def test_target_uses_the_key_file_and_a_removed_file_means_no_key(self):
        api = self.router()
        s = {**ask.settings(), "provider": "localrouter", "model": "acme/acme-large"}
        t, why = providers.target(s)
        self.assertIsNone(why)
        self.assertEqual((t["provider"], t["kind"]), ("localrouter", "openai-responses"))
        self.secret_file.unlink()
        providers.forget_cache()
        self.assertEqual(providers.target(s)[1], "no_key")
        self.assertIn("key file", providers.why_message("no_key", "localrouter"))
        self.assertNotIn(tc.FILE_KEY, providers.why_message("no_key", "localrouter"))
        self.assertEqual(api.requests, [])

    def test_a_missing_or_invalid_file_is_no_key(self):
        self.router()
        self.secret_file.write_text("not a key at all\n")
        r = self.row()
        self.assertEqual((r["has_key"], r["key_source"], r["status"]), (False, "none", "no_key"))
        self.assertNotIn("not a key", json.dumps(r))
        self.secret_file.unlink()
        r = self.row()
        self.assertEqual((r["has_key"], r["key_source"], r["status"]), (False, "none", "no_key"))
        self.assertEqual(self.call("GET", "/api/ask/providers")[0], 200)
        self.assertEqual(self.row()["models_at"], None)                 # no list fetched without a key

    def test_a_directory_is_no_key_and_only_the_first_4096_bytes_are_read(self):
        self.router()
        folder = self.home / "a-folder"
        folder.mkdir()
        providers.save({"id": "localrouter", "key_file": str(folder)})
        self.assertEqual(providers.key_source("localrouter"), "none")
        providers.save({"id": "localrouter", "key_file": "~/.config/localrouter/key"})
        self.secret_file.write_text("k" * 5000)
        self.assertEqual(providers.get_key("localrouter"), "k" * providers.KEY_FILE_BYTES)

    def test_a_server_that_is_not_running_is_offline_with_a_hint(self):
        self.router()
        self.up = False
        r = self.row()
        self.assertEqual((r["has_key"], r["key_source"], r["status"]), (True, "file", "offline"))
        self.assertTrue(r["hint"].startswith("Start the server on this Mac (127.0.0.1:"), r["hint"])
        self.assertEqual(self.probed[0][0], "127.0.0.1")
        _, out = CliCase.run_cli(self, ["providers"], up=False)
        self.assertIn("key: from ~/.config/localrouter/key", out)
        self.assertIn("offline: Start the server on this Mac", out)
        self.assertNoSecret()
        self.up = True
        providers.forget_cache()
        self.assertEqual(self.row()["status"], "ok")
        providers.save({"id": "remote", "kind": "openai-chat", "base_url": "https://api.acme.example/v1", "key": "k" * 12})
        self.up = False
        providers.forget_cache()
        remote = next(p for p in providers.list_view()["providers"] if p["id"] == "remote")
        self.assertEqual(remote["status"], "offline")
        self.assertNotIn("hint", remote)                                # nothing the user can start

    def test_the_cli_says_where_the_key_comes_from(self):
        self.router()
        _, out = CliCase.run_cli(self, ["providers"], up=False)
        line = next(x for x in out.splitlines() if "localrouter" in x and "Local Router" in x)
        self.assertIn("key: from ~/.config/localrouter/key", line)
        self.assertNotIn("no key", line)
        self.assertNoSecret()
        _, out = CliCase.run_cli(self, ["providers", "list", "--json"], up=False)
        self.assertEqual(next(p for p in json.loads(out)["providers"] if p["id"] == "localrouter")["key_source"], "file")
        self.secret_file.unlink()
        _, out = CliCase.run_cli(self, ["providers"], up=False)
        self.assertIn("mse providers connect localrouter", out)

    def test_the_model_list_loads_by_itself_once_and_is_cached(self):
        api = self.router()
        d = json.loads(self.call("GET", "/api/ask/providers")[1])
        r = next(p for p in d["providers"] if p["id"] == "localrouter")
        self.assertEqual(r["models"], self.LISTED)
        self.assertIsNotNone(r["models_at"])
        self.assertEqual(len([q for q in api.requests if q["path"] == "/v1/models"]), 1)
        self.call("GET", "/api/ask/providers")
        self.call("GET", "/api/ask/providers")
        self.assertEqual(len([q for q in api.requests if q["path"] == "/v1/models"]), 1)     # cached, not fetched again
        self.assertEqual(api.requests[0]["headers"]["authorization"], f"Bearer {tc.FILE_KEY}")
        self.assertNoSecret()
        self.assertIn("other-lab/thing-1", providers.get("localrouter")["models"])           # stored in config.json

    def test_without_auto_models_no_list_is_fetched_by_itself(self):
        api = self.router(auto_models=False)
        self.row()
        self.row()
        self.assertEqual(api.requests, [])
        self.assertEqual(self.row()["auto_models"], False)

    def test_no_list_is_fetched_while_the_server_is_offline_or_has_no_key(self):
        api = self.router()
        self.up = False
        self.row()
        self.assertEqual(api.requests, [])
        self.up = True
        providers.forget_cache()
        self.secret_file.unlink()
        self.row()
        self.assertEqual(api.requests, [])

    def test_a_failing_list_is_retried_only_after_a_while(self):
        api = self.router(status=500)
        self.row()
        self.row()
        self.assertEqual(len(api.requests), 1)
        self.assertIsNone(providers.get("localrouter").get("models_at"))
        providers._autolist["localrouter"] -= providers.AUTOLIST_RETRY_S + 1
        self.row()
        self.assertEqual(len(api.requests), 2)
        self.assertNoSecret()

    def test_the_key_is_never_sent_over_plain_http_to_another_machine(self):
        self.router(base_url="http://remote.example/v1")
        providers.forget_cache()
        self.assertEqual(providers.get_key("localrouter"), tc.FILE_KEY)           # it is read ...
        s = {**ask.settings(), "provider": "localrouter", "model": "acme/acme-large"}
        self.assertEqual(providers.target(s)[1], "insecure_url")                  # ... but a call is refused
        with self.assertRaises(ValueError) as cm:                                  # and such a provider cannot even be saved
            providers.save({"id": "x2", "kind": "openai-chat", "base_url": "http://remote.example/v1",
                            "key_file": str(self.secret_file)})
        self.assertIn("https", str(cm.exception))
        self.assertNotIn('"x2"', paths.CONFIG.read_text())      # quoted: the random token may contain x2

    def test_over_https_the_key_file_is_used_for_that_provider_only(self):
        self.router(base_url="https://api.acme.example/v1")
        s = {**ask.settings(), "provider": "localrouter", "model": "acme/acme-large"}
        t, why = providers.target(s)
        self.assertIsNone(why)
        self.assertEqual(t["base_url"], "https://api.acme.example/v1")
        s2 = {**s, "provider": "anthropic", "model": "claude-opus-5-5"}
        self.assertEqual(providers.target(s2)[1], "no_key")                        # the file is not Anthropic's key

    def test_an_unreadable_keychain_does_not_block_the_key_file(self):
        self.router()

        class Broken(tc.FakeKeychain):
            def get(self, account):
                raise providers.KeychainError("locked")
        providers.keychain = Broken()
        providers.forget_cache()
        self.assertEqual(providers.get_key("localrouter"), tc.FILE_KEY)
        with self.assertRaises(providers.KeychainError):
            providers.get_key("anthropic")

    def test_a_rebuilt_helper_that_may_not_open_the_old_item_falls_back_to_the_key_file(self):
        """After ./scripts/build_helper.sh the old Keychain items no longer trust the binary: a read without UI fails
        (OSStatus -25308) instead of asking. The daemon passes --no-ui, takes that as "unreadable" and, for a provider
        with a key_file, uses that file; no dialog, no exception for the request."""
        self.router()
        real = providers.HelperKeychain()
        denied = mock.Mock(stdout=b'{"error": "keychain: User interaction is not allowed. (OSStatus -25308)"}\n')
        seen = []

        def run(cmd, **kw):
            seen.append(cmd)
            return denied
        providers.keychain = real
        providers.forget_cache()
        with mock.patch.object(paths, "HELPER", paths.CONFIG), mock.patch("subprocess.run", side_effect=run):
            self.assertEqual(providers.get_key("localrouter"), tc.FILE_KEY)
            with self.assertRaisesRegex(providers.KeychainError, "25308"):
                providers.get_key("anthropic")
        self.assertTrue(seen and all("--no-ui" in c for c in seen))

    def test_connect_still_stores_the_key_and_then_the_keychain_wins(self):
        self.router()
        providers.connect("localrouter")
        self.assertEqual(self.kc.items["localrouter"], tc.FILE_KEY)
        self.secret_file.write_text(self.K2 + "\n")
        providers.forget_cache()
        self.assertEqual((providers.get_key("localrouter"), providers.key_source("localrouter")), (tc.FILE_KEY, "keychain"))

    def test_tests_never_reach_a_real_key_file(self):
        self.router()
        path = providers.key_file_path(providers.get("localrouter"))
        self.assertEqual(path, self.secret_file)
        self.assertTrue(str(path).startswith(self.tmp.name))


class OneStepSelection(tc.Base):
    """The model menu sets provider + model + route in one POST /api/settings."""

    def test_a_cloud_model_sets_provider_model_and_route_together(self):
        self.add_router()
        config.update_settings({"ask": {"route": "local"}})
        s = ask.settings(config.update_settings({"ask": {"provider": "localrouter", "model": "acme-small", "route": "cloud"}}))
        self.assertEqual((s["route"], s["provider"], s["model"]), ("cloud", "localrouter", "acme-small"))
        self.assertEqual(ask.route_mode(s), ("cloud", False))                     # and no silent local fallback

    def test_a_local_model_sets_route_local_and_the_model(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "qwen3.5-4b-4bit"
            d.mkdir()
            (d / "config.json").write_text('{"architectures": ["Qwen3_5ForConditionalGeneration"]}')
            (d / "model.safetensors").write_bytes(b"0")
            with mock.patch.object(paths, "MODELS", Path(t)), mock.patch.object(config, "_unload_ask"):
                s = ask.settings(config.update_settings({"ask": {"route": "local", "local_model": "qwen3.5-4b-4bit"}}))
        self.assertEqual((s["route"], Path(s["local_model"]).name), ("local", "qwen3.5-4b-4bit"))
        self.assertEqual(ask.route_mode(s), ("local", False))

    def test_a_marked_model_in_the_same_step_is_refused_whole_until_confirmed(self):
        self.add_router()
        before = ask.settings()
        with self.assertRaises(ValueError) as cm:
            config.update_settings({"ask": {"provider": "localrouter", "model": TRAIN, "route": "cloud"}})
        self.assertIn("training_confirmation_required", str(cm.exception))
        after = ask.settings()
        self.assertEqual((after["provider"], after["model"], after["route"]), (before["provider"], before["model"], before["route"]))
        s = ask.settings(config.update_settings({"ask": {"provider": "localrouter", "model": TRAIN, "route": "cloud",
                                                         "allow_training_models": [TRAIN]}}))
        self.assertEqual((s["provider"], s["model"], s["route"]), ("localrouter", TRAIN, "cloud"))

    def test_local_fallback_is_a_boolean_and_off_by_default(self):
        self.assertIs(ask.settings()["local_fallback"], False)
        self.assertEqual(ask.settings()["route"], "cloud")
        with self.assertRaises(ValueError):
            config.update_settings({"ask": {"local_fallback": "yes"}})
        self.assertIs(ask.settings(config.update_settings({"ask": {"local_fallback": True}}))["local_fallback"], True)
        self.assertEqual(ask.route_mode(ask.settings()), ("cloud", True))
        config.update_settings({"ask": {"route": "auto"}})                          # the old name keeps working
        self.assertEqual(ask.route_mode(ask.settings()), ("cloud", True))


class MenuGroups(unittest.TestCase):
    """The grouping of a provider's models by id prefix lives in the page (mse/ui/app.js familyOf); run it in node."""

    def test_family_of_groups_by_prefix(self):
        import re
        import shutil
        import subprocess
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        src = (paths.UI / "app.js").read_text()
        m = re.search(r"const FAMILY_NAMES = .*?\nfunction familyOf\(id\) \{.*?\n\}\n", src, re.S)
        if not m:
            self.skipTest("this interface copy has no model menu yet (install the new UI)")
        ids = ["gpt-5.5", "openai/gpt-5.5", "deepseek/deepseek-chat", "anthropic/claude-opus-4.8", "example-api/grok-4.5",
               "acme/kimi-k3", "widget/qwen3.7-max", "acme/acme-large-train", "weird-lab/thing-1"]
        prog = m.group(0) + "console.log(JSON.stringify(%s.map(familyOf)));" % json.dumps(ids)
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), ["GPT", "GPT", "DeepSeek", "Claude", "Example", "Acme", "Widget", "Acme", "Weird-lab"])

    def test_the_page_matches_training_models_like_the_service_does(self):
        """globRe / trainsOnInputs in mse/ui/app.js and config.trains_on_inputs agree on the same patterns and ids."""
        import re
        import shutil
        import subprocess
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        src = (paths.UI / "app.js").read_text()
        m = re.search(r"const globRe = .*?\nconst trainsOnInputs = \(model, provider\) => \{.*?\n\};\n", src, re.S)
        self.assertTrue(m, "globRe / trainsOnInputs not found in app.js")
        cases = [(["*-train"], "acme-large-train"), (["*-train"], "ACME-Large-TRAIN"), (["*-train"], "acme-large"),
                 (["*-train"], "acme-train-large"), (["acme/*-train"], "acme/x-train"), (["acme-?"], "acme-x"),
                 (["acme-?"], "acme-xy"), (["a.b"], "aXb"), (["a.b"], "a.b"), (["*"], "anything"), ([], "x"),
                 (["x(1)*"], "x(1)y"), (["[ab]"], "[ab]"), (["[ab]"], "a")]
        prog = ("const providerRow = () => null;\n" + m.group(0)
                + "console.log(JSON.stringify(%s.map(([g, id]) => trainsOnInputs(id, {training_models: g}))));" % json.dumps(cases))
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [config.trains_on_inputs(id, {"training_models": g}) for g, id in cases])


class ResponsesProvider(tc.Base):
    """A second fictional custom provider, "acme": Responses wire, its own key in the Keychain, no key file."""

    def setUp(self):
        super().setUp()
        providers.save({"id": "acme", "kind": "openai-responses", "base_url": "https://api.acme.example/v1",
                        "label": "Acme", "models": ["acme-large", "acme-xl-train"]})

    def test_it_is_a_custom_responses_provider_with_its_own_key(self):
        p = providers.get("acme")
        self.assertEqual((p["kind"], p["base_url"], p["builtin"]), ("openai-responses", "https://api.acme.example/v1", False))
        self.assertEqual(providers.env_name("acme"), "MSE_KEY_ACME")
        self.assertEqual([r["status"] for r in providers.list_view()["providers"] if r["id"] == "acme"], ["no_key"])
        self.kc.set("acme", "M" * 40)
        self.assertEqual(providers.get_key("acme"), "M" * 40)
        self.assertEqual(providers.key_source("acme"), "keychain")
        self.assertIsNone(providers.get_key("anthropic"))
        url, body, headers = cloud.build_request(p, "M" * 40, "acme-large", [{"role": "system", "content": "s"},
                                                                          {"role": "user", "content": "q"}], 500)
        self.assertEqual((url, body["model"], body["instructions"]), ("https://api.acme.example/v1/responses", "acme-large", "s"))
        self.assertEqual(headers["Authorization"], "Bearer " + "M" * 40)

    def test_a_model_id_can_be_added_and_a_pattern_marks_it(self):
        row = providers.save({"id": "acme", "models": ["acme-large", "acme-2-train"]})
        self.assertEqual(row["models"], ["acme-large", "acme-2-train"])
        self.assertFalse(config.trains_on_inputs("acme-2-train", providers.get("acme")))      # no patterns yet
        row = providers.save({"id": "acme", "training_models": ["*-train"]})
        self.assertEqual(row["training_models"], ["*-train"])
        self.assertTrue(config.trains_on_inputs("acme-2-train", providers.get("acme")))
        self.assertFalse(config.trains_on_inputs("acme-large", providers.get("acme")))

    def test_it_answers_through_the_responses_stream(self):
        api = tc.FakeAPI(lambda path, body: (200, tc.SSE, tc.responses_sse(["Paid", " [1]."])))
        try:
            self.kc.set("acme", "M" * 40)
            cfg = {"ask": {"enabled": True, "provider": "acme", "model": "acme-large",
                           "providers": {"acme": {"kind": "openai-responses", "base_url": api.base}}}}
            d = tc.done(tc.run(cfg=cfg))
            self.assertEqual((d["where"], d["provider"], d["model"], d["answer"]), ("cloud", "acme", "acme-large", "Paid [1]."))
            self.assertNotIn("training", d)
            self.assertEqual(api.requests[0]["headers"]["authorization"], "Bearer " + "M" * 40)
        finally:
            api.close()


class TrainingPatterns(unittest.TestCase):
    """config.trains_on_inputs: a provider's training_models globs (* any characters, ? one), case-insensitive."""

    def test_globs(self):
        t = config.trains_on_inputs
        p = {"training_models": ["*-train", "acme/x-?", "Exact"]}
        self.assertTrue(t("acme-large-train", p))
        self.assertTrue(t("ACME-LARGE-TRAIN", p))
        self.assertTrue(t("lab/deep/acme-train", p))                # * also crosses a slash
        self.assertTrue(t("acme/x-1", p))
        self.assertTrue(t("exact", p))
        self.assertFalse(t("acme/x-12", p))
        self.assertFalse(t("acme-large-train-2", p))
        self.assertFalse(t("acme-large", p))
        self.assertFalse(t("exact-and-more", p))

    def test_regex_characters_in_a_pattern_are_literal(self):
        t = config.trains_on_inputs
        self.assertTrue(t("a.b", {"training_models": ["a.b"]}))
        self.assertFalse(t("aXb", {"training_models": ["a.b"]}))
        self.assertTrue(t("x(1)y", {"training_models": ["x(1)*"]}))
        self.assertTrue(t("[ab]", {"training_models": ["[ab]"]}))
        self.assertFalse(t("a", {"training_models": ["[ab]"]}))

    def test_no_provider_no_patterns_no_warning(self):
        t = config.trains_on_inputs
        for p in (None, {}, {"training_models": []}, {"training_models": None}, {"training_models": "*"}, "x", 5):
            self.assertFalse(t("acme-large-train", p), repr(p))
        for m in (None, "", 5):
            self.assertFalse(t(m, {"training_models": ["*"]}), repr(m))

    def test_validation(self):
        self.assertEqual(config.check_training_models(" a*, b? ,a*,"), ["a*", "b?"])
        self.assertEqual(config.check_training_models(["x"]), ["x"])
        self.assertEqual(config.check_training_models([]), [])
        for bad in (5, [1], {"a": 1}, ["a" * 201], ["a\nb"], [str(i) for i in range(21)]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config.check_training_models(bad)
        self.assertIsNone(config.check_key_file(None))
        self.assertIsNone(config.check_key_file(""))
        self.assertEqual(config.check_key_file(" ~/k "), "~/k")
        self.assertEqual(config.check_key_file("/etc/k"), "/etc/k")
        for bad in ("k", "./k", "~other/k", 5, "~/k\n", "/" + "a" * 500):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config.check_key_file(bad)


class TrainingGuard(tc.Base):
    def setUp(self):
        super().setUp()
        self.add_router()

    def test_a_marked_model_needs_a_confirmation_before_it_can_be_selected(self):
        for change in ({"provider": "localrouter", "model": TRAIN}, {"provider": "localrouter", "model": "ACME-XL-TRAIN"}):
            with self.assertRaises(ValueError) as cm:
                config.update_settings({"ask": change})
            self.assertIn("training_confirmation_required", str(cm.exception))
            self.assertIn(config.TRAINING_WARNING, str(cm.exception))
        self.assertEqual(ask.settings()["model"], "claude-opus-5-5")
        # an ordinary model, and every other setting, are not asked about
        config.update_settings({"ask": {"provider": "localrouter", "model": "acme-large"}})
        config.update_settings({"ask": {"route": "cloud", "cloud_redact": False}})
        # the pattern belongs to the provider: the same id at a provider without patterns is not marked
        config.update_settings({"ask": {"provider": "anthropic", "model": TRAIN}})

    def test_the_confirmation_is_stored_once_per_model_id(self):
        s = ask.settings(config.update_settings({"ask": {"provider": "localrouter", "model": TRAIN,
                                                         "allow_training_models": [TRAIN]}}))
        self.assertEqual((s["model"], s["allow_training_models"]), (TRAIN, [TRAIN]))
        self.assertIn('"allow_training_models"', paths.CONFIG.read_text())
        with self.assertRaises(ValueError):             # another marked id is a new question
            config.update_settings({"ask": {"model": "acme-small-train"}})
        config.update_settings({"ask": {"model": "acme-small-train",
                                        "allow_training_models": [TRAIN, "acme-small-train"]}})

    def test_the_confirmation_list_is_validated(self):
        for bad in ("x", [1], [["a"]], ["a" * 300], ["x"] * 0 + ["a\nb"]):
            with self.assertRaises(ValueError):
                config.update_settings({"ask": {"allow_training_models": bad}})
        s = ask.settings(config.update_settings({"ask": {"allow_training_models": [" a ", "a", "b", ""]}}))
        self.assertEqual(s["allow_training_models"], ["a", "b"])

    def test_a_hand_edited_config_cannot_bypass_the_guard(self):
        api = tc.FakeAPI(lambda path, body: (200, tc.SSE, tc.responses_sse(["Paid", " [1]."])))
        try:
            ask_cfg = {"enabled": True, "provider": "localrouter", "model": TRAIN,
                       "providers": {"localrouter": self.router_def(api.base)}}
            local = tc.gen(["local [1]."])
            ev = tc.run(cfg={"ask": ask_cfg}, gen_fn=local)                     # default: an error, not a local answer
            self.assertEqual(ev[-1]["type"], "error")
            d = tc.done(tc.run(cfg={"ask": {**ask_cfg, "local_fallback": True}}, gen_fn=local))
            self.assertEqual((d["where"], d["fallback_reason"]), ("local", "training_unconfirmed"))
            self.assertEqual(api.requests, [])                                  # nothing was sent
            ev = tc.run(cfg={"ask": ask_cfg}, gen_fn=local, route="cloud")
            self.assertEqual(ev[-1]["type"], "error")
            self.assertIn("may use inputs for training", ev[-1]["message"])
            self.assertEqual(api.requests, [])
            ok = {**ask_cfg, "allow_training_models": [TRAIN]}
            d = tc.done(tc.run(cfg={"ask": ok}, gen_fn=local))
            self.assertEqual((d["where"], d["training"]), ("cloud", True))      # the answer carries the warning flag
            self.assertEqual(len(api.requests), 1)
            self.assertEqual(api.requests[0]["headers"]["authorization"], f"Bearer {tc.FILE_KEY}")     # the key file's key
            # the same model id at a provider without patterns is not guarded
            plain = {**ask_cfg, "providers": {"localrouter": {**self.router_def(api.base), "training_models": []}}}
            d = tc.done(tc.run(cfg={"ask": plain}, gen_fn=local))
            self.assertEqual(d["where"], "cloud")
            self.assertNotIn("training", d)
        finally:
            api.close()

    def test_folders_that_keep_questions_local_stay_local_even_when_confirmed(self):
        api = tc.FakeAPI(lambda path, body: (200, tc.SSE, tc.responses_sse(["x"])))
        try:
            cfg = {"ask": {"enabled": True, "provider": "localrouter", "model": TRAIN, "allow_training_models": [TRAIN],
                           "providers": {"localrouter": self.router_def(api.base)}},
                   "collections": [{"name": "unit", "path": "/tmp/unit", "ask_cloud": False}]}
            d = tc.done(tc.run(cfg=cfg, collections=["unit"]))
            self.assertEqual((d["where"], d["fallback_reason"]), ("local", "ask_cloud_off"))
            self.assertEqual(api.requests, [])
        finally:
            api.close()

    def test_status_says_when_the_chosen_model_trains(self):
        base = {"ask": {"enabled": True, "provider": "localrouter", "providers": {"localrouter": self.router_def()}}}
        s = {**ask.settings(base), "model": TRAIN, "allow_training_models": []}
        with mock.patch.object(ask, "settings", return_value=s), mock.patch("mse.embedder.pool") as pool:
            pool.return_value.workers = {}
            st = ask.status()
        self.assertEqual((st["training"], st["training_confirmed"]), (True, False))
        s["model"] = "acme-large"
        with mock.patch.object(ask, "settings", return_value=s), mock.patch("mse.embedder.pool") as pool:
            pool.return_value.workers = {}
            self.assertFalse(ask.status()["training"])


class ApiSettings(HttpCase):
    """The settings and local-model endpoints over HTTP."""

    def test_api_validation_without_any_ui(self):
        self.add_router()
        code, body = self.call("POST", "/api/settings", {"ask": {"provider": "localrouter", "model": TRAIN}})
        self.assertEqual(code, 400)
        self.assertIn("training_confirmation_required", body)
        self.assertEqual(self.call("POST", "/api/settings", {"ask": {"allow_training_models": "yes"}})[0], 400)
        self.assertEqual(self.call("POST", "/api/settings", {"ask": {"local_model": "nonesuch"}})[0], 400)
        self.assertEqual(self.call("POST", "/api/settings", {"ask": {"local_model": "/etc"}})[0], 400)
        code, body = self.call("POST", "/api/settings", {"ask": {"provider": "localrouter", "model": TRAIN,
                                                                  "allow_training_models": [TRAIN]}})
        self.assertEqual(code, 200)
        a = json.loads(body)["ask"]
        self.assertEqual((a["model"], a["allow_training_models"]), (TRAIN, [TRAIN]))
        v = json.loads(self.call("GET", "/api/ask/providers")[1])
        self.assertEqual((v["allow_training_models"], v["training_warning"]), ([TRAIN], config.TRAINING_WARNING))
        row = next(p for p in v["providers"] if p["id"] == "localrouter")
        self.assertEqual(row["training_models"], ["*-train"])
        st = json.loads(self.call("GET", "/api/status")[1])["ask"]
        self.assertTrue(st["training"])
        self.assertEqual(self.call("POST", "/api/ask/providers/localrouter/frobnicate", {})[0], 404)

    def test_the_new_fields_over_http(self):
        code, body = self.call("POST", "/api/ask/providers", {
            "id": "localrouter", "kind": "openai-responses", "base_url": tc.ROUTER_URL, "key_file": "relative"})
        self.assertEqual(code, 400)
        self.assertIn("absolute path", body)
        code, body = self.call("POST", "/api/ask/providers", {
            "id": "localrouter", "kind": "openai-responses", "base_url": tc.ROUTER_URL, "key_file": "~/k",
            "training_models": ["*-train"], "auto_models": True})
        self.assertEqual(code, 200, body)
        d = json.loads(body)
        self.assertEqual((d["key_file"], d["training_models"], d["auto_models"], d["has_key"]), ("~/k", ["*-train"], True, False))


class LocalModels(tc.Base):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.object(paths, "MODELS", self.root)
        self.patch.start()

        def model(name, arch, weights=True):
            d = self.root / name
            d.mkdir()
            (d / "config.json").write_text(json.dumps({"architectures": [arch]} if arch else {}))
            if weights:
                (d / "model.safetensors").write_bytes(b"0" * 2_097_152)
        model("qwen3-4b-instruct-2507-4bit", "Qwen3ForCausalLM")
        model("qwen3.5-4b-4bit", "Qwen3_5ForConditionalGeneration")
        model("qwen3-reranker-0.6b-8bit", "Qwen3ForCausalLM")           # another role
        model("embeddinggemma-2-8bit", "EmbeddingGemma2Model")
        model("whisper-large-v3-turbo-8bit", None)
        model("half-downloaded-4bit", "Qwen3ForCausalLM", weights=False)
        (self.root / "hf").mkdir()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        super().tearDown()

    def test_detects_installed_answer_models_by_their_files(self):
        self.assertEqual([m["name"] for m in config.local_chat_models()], ["qwen3-4b-instruct-2507-4bit", "qwen3.5-4b-4bit"])
        v = ask.local_models({**ask.settings(), "local_model": str(self.root / "qwen3.5-4b-4bit")})
        self.assertEqual(v["current"], "qwen3.5-4b-4bit")
        self.assertEqual([(m["name"], m["size_mb"], m["current"]) for m in v["models"]],
                         [("qwen3-4b-instruct-2507-4bit", 2, False), ("qwen3.5-4b-4bit", 2, True)])

    def test_choosing_one_sets_it_and_unloads_the_worker(self):
        with mock.patch.object(config, "_unload_ask") as unload:
            config.update_settings({"ask": {"enabled": True}})          # (switching Ask off unloads, as before)
            unload.reset_mock()
            s = ask.settings(config.update_settings({"ask": {"local_model": "qwen3.5-4b-4bit"}}))
            self.assertEqual(s["local_model"], str(self.root / "qwen3.5-4b-4bit"))
            self.assertEqual(ask.model_name(s), "qwen3.5-4b-4bit")
            unload.assert_called_once()
            unload.reset_mock()
            config.update_settings({"ask": {"local_model": str(self.root / "qwen3.5-4b-4bit")}})     # same: no unload
            config.update_settings({"ask": {"route": "local"}})
            unload.assert_not_called()
        for bad in ("qwen3-reranker-0.6b-8bit", "embeddinggemma-2-8bit", "half-downloaded-4bit", "../x", "", 5,
                    "/Users/alex2/else/qwen3.5-4b-4bit"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config.update_settings({"ask": {"local_model": bad}})

    def test_the_local_route_is_untouched(self):
        s = ask.settings(config.update_settings({"ask": {"route": "local"}}))
        self.assertEqual(ask.next_route({**s, "enabled": True}, block=False)["where"], "local")


class Cli(CliCase):
    """`mse providers …` for the new commands (the daemon is faked, or absent with up=False)."""

    def setUp(self):
        super().setUp()
        self.add_router()

    def test_use_refuses_a_marked_model_until_it_is_confirmed(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()) as err:
            self.run_cli(["providers", "use", "localrouter", TRAIN], up=False)
        self.assertIn(config.TRAINING_WARNING, str(cm.exception.code))
        self.assertEqual(ask.settings()["model"], "claude-opus-5-5")
        _, out = self.run_cli(["providers", "use", "localrouter", TRAIN, "--allow-training"], up=False)
        self.assertIn(config.TRAINING_WARNING, out)
        s = ask.settings()
        self.assertEqual((s["provider"], s["model"], s["allow_training_models"]), ("localrouter", TRAIN, [TRAIN]))
        with self.assertRaises(SystemExit):
            self.run_cli(["providers", "use", "localrouter", "--allow-training"], up=False)

    def test_use_through_the_daemon_sends_the_confirmation_with_the_model(self):
        from mse import cli
        fake_calls = []

        def call(method, path, params=None, body=None):
            fake_calls.append((method, path, body))
            if path == "/api/ask/providers":
                return {"allow_training_models": ["old-one-train"], "providers": []}
            return {"ask": {"route": "auto", "provider": "localrouter", "model": TRAIN, "local_model": "/x/qwen3-4b"}}
        fake = mock.Mock(_call=mock.Mock(side_effect=call))
        out = io.StringIO()
        with mock.patch.object(cli, "daemon_up", return_value=True), mock.patch.object(cli, "client", return_value=fake), \
                mock.patch.object(cli, "TTY", False), contextlib.redirect_stdout(out):
            cli.main(["providers", "use", "localrouter", TRAIN, "--allow-training"])
        self.assertEqual(fake_calls[-1], ("POST", "/api/settings", {"ask": {
            "provider": "localrouter", "model": TRAIN, "allow_training_models": [TRAIN, "old-one-train"]}}))

    def test_connect_prints_no_key(self):
        s = tc.FakeAPI(lambda path, b: (200, JSON, [json.dumps({"data": [{"id": "acme/a"}, {"id": "other-lab/b"}]})]))
        try:
            providers.save({"id": "localrouter", "base_url": s.base})
            _, out = self.run_cli(["providers", "connect", "localrouter"], up=False)
        finally:
            s.close()
        self.assertEqual(self.kc.items["localrouter"], tc.FILE_KEY)
        self.assertNotIn(tc.FILE_KEY, out)
        self.assertIn("2 models", out)
        self.assertIn(self.key_file, out)                              # the path of the file, not its content
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            self.run_cli(["providers", "connect", "anthropic"], up=False)     # no key_file
        self.assertIn("no key file", str(cm.exception.code))

    def test_add_with_a_key_file_and_training_patterns_are_stored(self):
        _, out = self.run_cli(["providers", "add", "second", "--kind", "openai-responses", "--base-url", "http://127.0.0.1:59996/v1",
                               "--key-file", "~/keys/second", "--training-models", "*-train", "--training-models", "x?",
                               "--auto-models", "--model", "a-model"], up=False)
        self.assertIn("added second", out)
        raw = json.loads(paths.CONFIG.read_text())["ask"]["providers"]["second"]
        self.assertEqual(raw, {"kind": "openai-responses", "label": "second", "base_url": "http://127.0.0.1:59996/v1",
                               "models": ["a-model"], "key_file": "~/keys/second", "training_models": ["*-train", "x?"],
                               "auto_models": True})
        self.assertEqual(providers.get("second")["needs_key"] if "needs_key" in providers.get("second") else
                         providers.needs_key("second", providers.get("second")), True)       # a key_file says a key is needed

    def test_models_prints_the_whole_list_grouped_by_prefix_with_the_warning(self):
        from mse import cli
        listing = {"fetched": 4, "ms": 12, "models": ["acme/acme-large", "acme/acme-small", "acme/acme-xl-train",
                                                       "other-lab/thing-1"]}
        fake = mock.Mock(_call=mock.Mock(return_value=listing))
        out = io.StringIO()
        with mock.patch.object(cli, "daemon_up", return_value=True), mock.patch.object(cli, "client", return_value=fake), \
                mock.patch.object(cli, "TTY", False), contextlib.redirect_stdout(out):
            cli.main(["providers", "models", "localrouter"])
        text = out.getvalue()
        self.assertIn("4 models fetched", text)
        for line in ("acme/", "other-lab/", "acme/acme-small", "other-lab/thing-1"):
            self.assertIn(line, text)
        self.assertEqual(text.count(config.TRAINING_WARNING), 1)       # only the model the provider's pattern marks

    def test_the_list_marks_the_models_the_patterns_match(self):
        providers.save({"id": "localrouter", "models": ["acme-large", TRAIN]})
        _, out = self.run_cli(["providers"], up=False)
        self.assertIn(f"{TRAIN} ⚠", out)
        self.assertNotIn("acme-large ⚠", out)
        self.assertIn("--allow-training", out)

    def test_use_local_with_a_model_chooses_the_model_that_answers_here(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            d = root / "qwen3.5-4b-4bit"
            d.mkdir()
            (d / "config.json").write_text('{"architectures": ["Qwen3_5ForConditionalGeneration"]}')
            (d / "model.safetensors").write_bytes(b"0")
            with mock.patch.object(paths, "MODELS", root), mock.patch.object(config, "_unload_ask"):
                _, out = self.run_cli(["providers", "use", "local", "qwen3.5-4b-4bit"], up=False)
                self.assertIn("local model qwen3.5-4b-4bit", out)
                self.assertEqual(ask.settings()["route"], "cloud")                # the route is untouched
                _, out = self.run_cli(["providers", "models", "local"], up=False)
                self.assertIn("* qwen3.5-4b-4bit", out)
                _, out = self.run_cli(["providers", "use", "local"], up=False)
                self.assertEqual(ask.settings()["route"], "local")
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                    self.run_cli(["providers", "use", "local", "nope"], up=False)


if __name__ == "__main__":
    unittest.main()
