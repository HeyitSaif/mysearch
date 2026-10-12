"""The setup assistant's backend: the model manifest as the app reads it, the download job (run by a child process; here a
fake one that prints events), the child side that turns huggingface_hub's byte counts into events, and the /api/setup/*
routes with the same token rule as every other route. No network is used and no model is read or written: MODELS is a
temporary folder, the child is a fake, and Hugging Face is never imported except for its progress-bar class.
Run: .venv/bin/python -m unittest discover -s tests -t ."""

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import config, paths, server, setup_models  # noqa: E402
from mse.setup_models import DownloadJob, SetupRefused  # noqa: E402

BIG_DISK = type("U", (), {"free": 500 * 1_000_000_000, "total": 1_000_000_000_000, "used": 1})()

# what the real child prints, in the order it prints it (setup_models.install_events); stdout carries nothing else
FAKE_OK = r'''
import json, os, sys, time
def ev(**k): print(json.dumps(k), flush=True)
ev(event="plan", total_bytes=1000, models=[{"folder": "embeddinggemma-2-8bit", "size_bytes": 1000}])
ev(event="model", folder="embeddinggemma-2-8bit", state="start")
print("a stray line from a library", flush=True)
ev(event="bytes", folder="embeddinggemma-2-8bit", done_bytes=250)
time.sleep(0.15)
ev(event="bytes", folder="embeddinggemma-2-8bit", done_bytes=400)
time.sleep(0.15)
ev(event="model", folder="embeddinggemma-2-8bit", state="installed")
ev(event="bytes", folder="embeddinggemma-2-8bit", done_bytes=1000)
ev(event="done")
'''
FAKE_ERROR = r'''
import json
print(json.dumps({"event": "error", "message": "embeddinggemma-2-8bit: not found, or it needs its terms accepted"}), flush=True)
raise SystemExit(1)
'''
FAKE_CRASH = r'''
raise SystemExit(3)
'''
FAKE_SLOW = r'''
import json, time
print(json.dumps({"event": "model", "folder": "embeddinggemma-2-8bit", "state": "start"}), flush=True)
time.sleep(60)
'''


def fake(code):
    return lambda tier: ([sys.executable, "-c", code, tier], dict(os.environ))


def wait_for(job, states, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = job.view()
        if v["state"] in states:
            return v
        time.sleep(0.02)
    raise AssertionError(f"job still {job.view()['state']}, wanted {states}")


class Isolated(unittest.TestCase):
    """MODELS and LOGS in a temporary folder; a roomy disk; a job of its own."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mse-setup-"))
        self.models = self.tmp / "models"
        self.models.mkdir()
        self.logs = self.tmp / "logs"
        for p in (mock.patch.object(paths, "MODELS", self.models), mock.patch.object(paths, "LOGS", self.logs),
                  mock.patch.object(paths, "DATA", self.tmp),
                  mock.patch("mse.setup_models.shutil.disk_usage", return_value=BIG_DISK)):
            p.start()
            self.addCleanup(p.stop)
        self.job = DownloadJob()
        p = mock.patch.object(setup_models, "JOB", self.job)
        p.start()
        self.addCleanup(p.stop)
        # other tests (and mse modules) leave HF_HUB_OFFLINE set in this process; the child side refuses to run with it
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HF_HUB_OFFLINE", None)

    def install_fake_model(self, folder="embeddinggemma-2-8bit", weights=True):
        d = self.models / folder
        d.mkdir()
        (d / "config.json").write_text("{}")
        (d / "tokenizer.json").write_text("{}")
        if weights:
            (d / "model.safetensors").write_bytes(b"x" * 16)


class ManifestView(Isolated):
    def test_every_model_is_listed_with_status_and_sizes(self):
        v = setup_models.models_view()
        by = {m["folder"]: m for m in v["models"]}
        self.assertEqual(by["embeddinggemma-2-8bit"]["status"], "missing")
        self.assertEqual(by["embeddinggemma-2-8bit"]["download_bytes"], 1_266_584_562)
        self.assertEqual(by["embeddinggemma-2-8bit"]["download_human"], "1.27 GB")
        self.assertTrue(by["embeddinggemma-2-8bit"]["installable"])
        self.assertFalse(by["qwen3-reranker-0.6b-8bit"]["installable"])
        self.assertIn("licence", by["embeddinggemma-2-8bit"])
        self.assertEqual(v["job"]["state"], "idle")
        self.assertEqual(v["free_bytes"], BIG_DISK.free)

    def test_the_minimal_tier_is_one_model_and_full_adds_the_rest(self):
        t = setup_models.models_view()["tiers"]
        self.assertEqual(t["minimal"]["todo"], ["embeddinggemma-2-8bit"])
        self.assertEqual(t["minimal"]["need_bytes"], 1_266_584_562)
        self.assertEqual(set(t["full"]["todo"]), {"embeddinggemma-2-8bit", "whisper-large-v3-turbo-8bit", "qwen3-4b-instruct-2507-4bit"})
        self.assertEqual(t["full"]["manual"], ["qwen3-reranker-0.6b-8bit"])     # no public source: never downloaded
        self.assertFalse(t["minimal"]["ready"])

    def test_an_installed_model_is_not_planned_again(self):
        self.install_fake_model()
        t = setup_models.models_view()["tiers"]
        self.assertEqual(t["minimal"]["todo"], [])
        self.assertTrue(t["minimal"]["ready"])
        self.assertEqual(t["minimal"]["present"], ["embeddinggemma-2-8bit"])
        self.assertNotIn("embeddinggemma-2-8bit", t["full"]["todo"])

    def test_a_folder_without_weights_blocks_and_is_never_touched(self):
        self.install_fake_model(weights=False)
        t = setup_models.models_view()["tiers"]["minimal"]
        self.assertEqual(t["blocked"], ["embeddinggemma-2-8bit"])
        self.assertFalse(t["ready"])
        self.assertTrue((self.models / "embeddinggemma-2-8bit" / "config.json").exists())

    def test_a_resumable_partial_folder_counts_what_it_holds(self):
        part = self.models / ".embeddinggemma-2-8bit.partial"
        part.mkdir()
        (part / "model.safetensors").write_bytes(b"x" * 4096)
        t = setup_models.plan("minimal")
        self.assertEqual(len(t["todo"]), 1)
        self.assertLess(t["need_bytes"], 1_266_584_562)      # the partial bytes are not fetched again

    def test_status_summary(self):
        with mock.patch("mse.macindex.full_disk_access", return_value=False):
            s = setup_models.setup_status()
        self.assertTrue(s["needs_setup"])                     # no folder, no model
        self.assertFalse(s["minimal_models_ready"])
        self.assertEqual(s["full_disk_access"], "not_granted")
        self.assertEqual(s["job"]["state"], "idle")
        self.install_fake_model()
        cfg = dict(config.load(), collections=[{"name": "notes"}])
        with mock.patch("mse.macindex.full_disk_access", return_value=None), mock.patch("mse.config.load", return_value=cfg):
            s = setup_models.setup_status()
        self.assertFalse(s["needs_setup"])
        self.assertEqual(s["collections"], 1)
        self.assertTrue(s["minimal_models_ready"])
        self.assertEqual(s["full_disk_access"], "unknown")
        self.assertFalse(s["bundled"])                        # the test tree is not inside an .app
        with mock.patch("mse.macindex.full_disk_access", return_value=True), mock.patch("mse.config.load", return_value={"collections": []}):
            self.assertEqual(setup_models.setup_status()["full_disk_access"], "granted")
            self.assertTrue(setup_models.setup_status()["needs_setup"])      # a folder is still missing


class AppBundle(unittest.TestCase):
    def test_the_package_inside_an_app_is_recognised(self):
        with mock.patch.object(paths, "PROJECT", Path("/Applications/MySearch.app/Contents/Resources")):
            self.assertEqual(setup_models.app_bundle(), Path("/Applications/MySearch.app"))
        for not_app in ("/Users/alex/Developer/mysearch", "/Applications/MySearch.app/Contents", "/x/Resources"):
            with mock.patch.object(paths, "PROJECT", Path(not_app)):
                self.assertIsNone(setup_models.app_bundle())


class JobLifecycle(Isolated):
    def test_a_download_runs_to_done_and_reports_progress(self):
        self.job.command_factory = fake(FAKE_OK)
        done = threading.Event()
        seen = []
        v = self.job.start("minimal", on_done=lambda st: (seen.append(st), done.set()))
        self.assertTrue(v["started"])
        self.assertEqual(v["state"], "running")
        self.assertEqual(v["models"], [{"folder": "embeddinggemma-2-8bit", "size_bytes": 1_266_584_562, "state": "pending"}])
        mid = wait_for(self.job, ("running",))
        self.assertEqual(mid["tier"], "minimal")
        deadline = time.monotonic() + 10
        while self.job.view()["done_bytes"] < 400 and time.monotonic() < deadline:
            time.sleep(0.02)
        v = self.job.view()
        self.assertEqual(v["total_bytes"], 1000)               # the child's plan replaced the manifest estimate
        self.assertGreaterEqual(v["done_bytes"], 250)
        wait_for(self.job, ("done",))
        self.assertTrue(done.wait(5))
        self.assertEqual(seen, ["done"])
        v = self.job.view()
        self.assertEqual((v["percent"], v["done_bytes"], v["error"]), (100.0, 1000, None))
        self.assertEqual([m["state"] for m in v["models"]], ["installed"])

    def test_an_error_event_ends_in_error_with_its_sentence(self):
        self.job.command_factory = fake(FAKE_ERROR)
        self.job.start("minimal")
        v = wait_for(self.job, ("error",))
        self.assertIn("terms accepted", v["error"])
        self.assertIn("resume", v["message"])

    def test_a_crash_without_events_still_ends_in_error(self):
        self.job.command_factory = fake(FAKE_CRASH)
        self.job.start("minimal")
        v = wait_for(self.job, ("error",))
        self.assertIn("status 3", v["error"])

    def test_cancel_stops_the_child_and_keeps_the_partial_folder(self):
        part = self.models / ".embeddinggemma-2-8bit.partial"
        part.mkdir()
        (part / "keep.bin").write_bytes(b"x")
        self.job.command_factory = fake(FAKE_SLOW)
        seen = []
        self.job.start("minimal", on_done=seen.append)
        wait_for(self.job, ("running",))
        t0 = time.monotonic()
        self.job.cancel()
        v = wait_for(self.job, ("cancelled",))
        self.assertLess(time.monotonic() - t0, 10)
        self.assertIn("resume", v["message"])
        self.assertTrue((part / "keep.bin").exists())          # nothing is deleted
        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(seen, ["cancelled"])

    def test_a_second_start_while_running_returns_the_running_job(self):
        self.job.command_factory = fake(FAKE_SLOW)
        self.job.start("minimal")
        wait_for(self.job, ("running",))
        again = self.job.start("minimal")
        self.assertFalse(again["started"])
        self.assertEqual(again["state"], "running")
        self.job.cancel()
        wait_for(self.job, ("cancelled",))

    def test_nothing_to_download_does_not_start_a_process(self):
        self.install_fake_model()
        called = []
        self.job.command_factory = lambda tier: called.append(tier) or ([sys.executable, "-c", "pass"], dict(os.environ))
        v = self.job.start("minimal")
        self.assertFalse(v["started"])
        self.assertEqual(v["state"], "done")
        self.assertEqual(called, [])

    def test_refusals_are_sentences_for_the_app(self):
        with self.assertRaises(SetupRefused) as cm:
            self.job.start("everything")
        self.assertIn("unknown tier", str(cm.exception))
        with self.assertRaises(SetupRefused):
            setup_models.start_download(None)
        self.install_fake_model(weights=False)
        with self.assertRaises(SetupRefused) as cm:
            self.job.start("minimal")
        self.assertIn("without its weights", str(cm.exception))
        self.assertIn("embeddinggemma-2-8bit", str(cm.exception))
        self.assertIsInstance(SetupRefused("x"), ValueError)   # the server turns ValueError into a 400

    def test_not_enough_disk_is_refused_before_anything_starts(self):
        called = []
        self.job.command_factory = lambda tier: called.append(tier) or ([sys.executable, "-c", "pass"], dict(os.environ))
        small = type("U", (), {"free": 1_000_000_000})()
        with mock.patch("mse.setup_models.shutil.disk_usage", return_value=small):
            with self.assertRaises(SetupRefused) as cm:
                self.job.start("minimal")
        self.assertIn("not enough free disk space", str(cm.exception))
        self.assertEqual(called, [])

    def test_the_child_runs_the_documented_command_without_the_offline_switch(self):
        with mock.patch.dict(os.environ, {"HF_HUB_OFFLINE": "1", "KEEP_ME": "yes"}):
            cmd, env = setup_models._child_command("full")
        self.assertEqual(cmd[1:], ["-m", "mse", "setup", "models", "--full", "--events"])
        self.assertNotIn("HF_HUB_OFFLINE", env)               # the service is offline; the download it was asked for is not
        self.assertEqual(env["KEEP_ME"], "yes")
        self.assertEqual(env["HF_HUB_DISABLE_TELEMETRY"], "1")
        self.assertEqual(env["HF_HUB_DISABLE_IMPLICIT_TOKEN"], "1")      # no stored account token goes along: the models are public


class ChildSide(Isolated):
    """setup_models.install_events: what the child prints. _download is replaced; the byte counts are driven by hand."""

    def run_child(self, download, tier="minimal"):
        events = []
        with mock.patch.object(setup_models, "_download", side_effect=download):
            rc = setup_models.install_events(tier, emit=events.append, watch_parent=False)
        return rc, events

    def test_the_events_of_a_good_run(self):
        def download(m, out, tqdm_class=None, on_part=None):
            on_part(1)
            transfer, written = (tqdm_class(total=0, initial=0, unit="B", name="t"), tqdm_class(total=0, initial=0, unit="B", name="r"))
            written.update(300)
            transfer.update(200)
            time.sleep(1.3)                                    # long enough for the 0.5 s reporter to speak
            written.update(300)

        rc, ev = self.run_child(download)
        kinds = [e["event"] for e in ev]
        self.assertEqual(rc, 0)
        self.assertEqual(kinds[0], "plan")
        self.assertEqual(ev[0]["total_bytes"], 1_266_584_562)
        self.assertEqual(kinds[1], "model")
        self.assertEqual(ev[1], {"event": "model", "folder": "embeddinggemma-2-8bit", "state": "start"})
        live = [e for e in ev if e["event"] == "bytes" and e["model_bytes"] < 1_266_584_562]
        self.assertTrue(live and max(e["model_bytes"] for e in live) in (300, 600))     # the larger of the two bars
        self.assertEqual(kinds[-3:], ["bytes", "model", "done"])
        self.assertEqual(ev[-2], {"event": "model", "folder": "embeddinggemma-2-8bit", "state": "installed"})
        self.assertEqual(ev[-3]["done_bytes"], 1_266_584_562)

    def test_a_failed_download_prints_one_error_and_returns_1(self):
        def download(m, out, tqdm_class=None, on_part=None):
            raise RuntimeError("embeddinggemma-2-8bit: Hugging Face refused the request (503). Re-run the same command to resume.")

        rc, ev = self.run_child(download)
        self.assertEqual(rc, 1)
        self.assertEqual(ev[-1]["event"], "error")
        self.assertIn("503", ev[-1]["message"])
        self.assertNotIn("Re-run", ev[-1]["message"])            # written for the terminal; the app has a button
        self.assertIn("Try again", ev[-1]["message"])
        self.assertNotIn("done", [e["event"] for e in ev])

    def test_a_refusal_before_anything_starts(self):
        self.install_fake_model(weights=False)
        rc, ev = self.run_child(lambda *a, **k: None)
        self.assertEqual(rc, 1)
        self.assertEqual([e["event"] for e in ev], ["error"])
        self.assertIn("incomplete folder", ev[0]["message"])
        events = []
        self.assertEqual(setup_models.install_events("nonsense", emit=events.append, watch_parent=False), 1)
        self.assertIn("unknown tier", events[0]["message"])

    def test_nothing_to_do_still_ends_with_done(self):
        self.install_fake_model()
        rc, ev = self.run_child(lambda *a, **k: self.fail("must not download"))
        self.assertEqual((rc, [e["event"] for e in ev]), (0, ["done"]))

    def test_offline_environment_is_refused_with_a_sentence(self):
        with mock.patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}):
            rc, ev = self.run_child(lambda *a, **k: self.fail("must not download"))
        self.assertEqual(rc, 1)
        self.assertIn("HF_HUB_OFFLINE", ev[0]["message"])


class ByteSink(unittest.TestCase):
    def test_parts_of_one_model_add_up_and_a_bar_never_prints(self):
        sink = setup_models._ByteSink()
        Bar = sink.tqdm_class()
        self.assertFalse(sink.seen())
        sink.begin_part(1)
        transfer, written = Bar(total=0, initial=0, unit="B", unit_scale=True, desc="Downloading bytes", name="a"), \
            Bar(total=0, initial=0, unit="B", unit_scale=True, desc="Reconstructing", name="b")
        files = Bar([1, 2, 3], desc="Fetching 3 files", unit="it", name="c")       # the file counter is not a byte bar
        self.assertEqual(list(files), [1, 2, 3])
        transfer.update(80)
        written.update(100)
        self.assertEqual(sink.bytes(), 100)                  # the larger of the two bars
        sink.begin_part(2)                                    # a second repo of the same model
        Bar(total=0, initial=0, unit="B", name="a").update(40)
        self.assertEqual(sink.bytes(), 140)
        self.assertTrue(sink.seen())
        self.assertTrue(written.disable)                      # nothing is drawn


class AgainstTheLibrary(unittest.TestCase):
    """The progress sink is handed to huggingface_hub, which calls its own helpers on the bars. A bar that lacks an attribute those
    helpers read would crash the first real download, so they are called here the way _snapshot_download calls them (huggingface_hub
    1.33, the version in uv.lock). No network."""

    def test_the_sink_survives_the_library_s_own_calls(self):
        import logging
        from huggingface_hub.utils._xet_progress_reporting import (XET_BYTES_BAR_FORMAT, XET_TRANSFER_BAR_FORMAT, _finish_transfer_bar,
                                                                   _set_aggregate_rate_postfix, _update_transfer_bar)
        from huggingface_hub.utils.tqdm import _create_progress_bar
        sink = setup_models._ByteSink()
        Bar = sink.tqdm_class()
        sink.begin_part(1)
        kw = dict(log_level=logging.WARNING, total=0, initial=0, unit="B", unit_scale=True)
        transfer = _create_progress_bar(cls=Bar, name="huggingface_hub.snapshot_download.transfer", desc="Downloading bytes",
                                        bar_format=XET_TRANSFER_BAR_FORMAT, **kw)
        written = _create_progress_bar(cls=Bar, name="huggingface_hub.snapshot_download", desc="Reconstructing (incomplete total...)",
                                       bar_format=XET_BYTES_BAR_FORMAT, **kw)
        for bar in (written, transfer):                          # what _AggregatedTqdm does for every file
            bar.total = (bar.total or 0) + 1000
        written.refresh()
        written.update(300)
        _update_transfer_bar(transfer, 250)
        _set_aggregate_rate_postfix(written)
        _set_aggregate_rate_postfix(transfer)
        written.set_postfix_str("x", refresh=False)
        written.update(100)
        _finish_transfer_bar(transfer)
        transfer.set_description_str("Download complete")
        written.set_description("Reconstruction complete")
        self.assertEqual(sink.bytes(), 400)                      # the larger of the two bars
        self.assertTrue(sink.seen())

    def test_hf_thread_map_accepts_the_class_for_the_file_counter(self):
        from huggingface_hub.utils.tqdm import hf_thread_map
        Bar = setup_models._ByteSink().tqdm_class()
        out = hf_thread_map(lambda x: x * 2, [1, 2, 3], desc="Fetching 3 files", max_workers=2, tqdm_class=Bar)
        self.assertEqual(list(out), [2, 4, 6])


class Running(Isolated):
    """The routes, through the real request handler, with the token rule of every /api/ route."""

    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.token = config.load()["token"]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        p = mock.patch("mse.macindex.full_disk_access", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def call(self, method, path, body=None, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            headers = {"X-MSE-Token": self.token} if token else {}
            data = None
            if body is not None:
                data = json.dumps(body)
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            r = conn.getresponse()
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
        finally:
            conn.close()

    def test_every_setup_route_needs_the_token(self):
        for method, path in (("GET", "/api/setup/status"), ("GET", "/api/setup/models"), ("POST", "/api/setup/models"),
                             ("POST", "/api/setup/models/cancel")):
            with self.subTest(route=f"{method} {path}"):
                code, body = self.call(method, path, {} if method == "POST" else None, token=False)
                self.assertEqual(code, 401)
                self.assertIn("token", body["error"])

    def test_status_and_models_shapes(self):
        code, s = self.call("GET", "/api/setup/status")
        self.assertEqual(code, 200)
        for key in ("version", "collections", "minimal_models_ready", "full_models_ready", "needs_setup", "full_disk_access", "bundled", "job"):
            self.assertIn(key, s)
        code, m = self.call("GET", "/api/setup/models")
        self.assertEqual(code, 200)
        self.assertEqual(set(m["tiers"]), {"minimal", "full"})
        self.assertEqual(m["job"]["state"], "idle")
        self.assertEqual(len(m["models"]), len(setup_models.MANIFEST))

    def test_a_bad_tier_is_a_400_with_the_sentence(self):
        for body in ({"tier": "huge"}, {}, {"tier": 5}):
            with self.subTest(body=body):
                code, e = self.call("POST", "/api/setup/models", body)
                self.assertEqual(code, 400)
                self.assertTrue(e["error"])

    def test_start_progress_and_the_sync_after_the_models_arrive(self):
        self.job.command_factory = fake(FAKE_OK)
        jobs = mock.Mock()
        with mock.patch.object(server, "JOBS", jobs):
            code, v = self.call("POST", "/api/setup/models", {"tier": "minimal"})
            self.assertEqual((code, v["started"], v["tier"]), (200, True, "minimal"))
            wait_for(self.job, ("done",))
            deadline = time.monotonic() + 5
            while not jobs.submit.called and time.monotonic() < deadline:
                time.sleep(0.02)
        jobs.submit.assert_called_once_with("sync", None, None, reason="models-installed")
        code, m = self.call("GET", "/api/setup/models")
        self.assertEqual((m["job"]["state"], m["job"]["percent"]), ("done", 100.0))

    def test_cancel_over_http(self):
        self.job.command_factory = fake(FAKE_SLOW)
        self.assertEqual(self.call("POST", "/api/setup/models", {"tier": "full"})[0], 200)
        wait_for(self.job, ("running",))
        code, v = self.call("POST", "/api/setup/models/cancel", {})
        self.assertEqual(code, 200)
        wait_for(self.job, ("cancelled",))
        self.assertEqual(self.call("GET", "/api/setup/models")[1]["job"]["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
