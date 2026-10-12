"""The whole-disk routes of the REAL service, and the fixture stub that stands in for it in the snapshot checks.

The service runs as its own process (`python -m mse serve --port <free port>`), with HOME, TMPDIR and MSE_HOME under one
temporary sandbox: HOME is a fictional home folder built here (a few documents, a picture, and files the privacy rules
must keep out), MSE_HOME its data folder beside it. Nothing of the owner's is read, and the port is never 8737. The
helper is not installed in the sandbox, so pictures wait name-only and the documents are read by the service's own
readers: the real code, no stand-in describer.

Checked: the exact key sets of GET /api/disk/status, GET /api/disk/search (and of every result), POST /api/disk/rescan,
and of the "wholedisk" block of GET/POST /api/settings with its validation errors; the privacy rules on a live tree; and
that scripts/fixtures/stub_service.py --disk answers the same shape field by field (the service is the truth). The stop
is checked too: SIGTERM, exit code 0, port released.

Run: .venv/bin/python -m unittest tests.test_wholedisk_contract"""

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import wholedisk as wd  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FORBIDDEN_PORT = 8737                       # the owner's live service: never touched by a test

STATUS_KEYS = {"enabled", "roots", "files_seen", "files_described", "images_labelled", "pdfs_described",
               "docs_described", "videos_described", "failed", "pending", "state", "paused_reason", "rate_per_s",
               "eta_s", "last_error", "priority", "last_sync", "last_reconcile", "name_index_ready"}
SEARCH_KEYS = {"results", "has_more", "next_offset", "took_ms", "query"}
RESULT_KEYS = {"path", "name", "kind", "size", "mtime", "snippet", "labels", "score", "why"}
QUERY_KEYS = {"words", "kinds"}
RESCAN_KEYS = {"started"}
SETTINGS_KEYS = set(wd.SETTINGS_KEYS)
STATES = {"idle", "scanning", "describing", "paused"}
PAUSED = {None, "user_active", "on_battery", "disabled"}
WHYS = {"name", "label", "text", "vector"}

# The fictional home. Legitimate entries first; then files the rules must keep out, each carrying the word "kiwi".
ENTRIES = {
    "Documents/Safari/zebra-notes.md": "# Zebra notes\n\nA zebra herd crossed the track at dusk. Counts are provisional.\n",
    "Documents/safari-day-two.txt": "More zebra sightings near the waterhole, two foals among them.\n",
    "Documents/invoice-acme.txt": "Invoice from Acme Tools, number 4471, due in 14 days.\n",
    "Pictures/holiday.jpg": b"\xff\xd8\xff\xe0 not really a picture",
}
NEVER = {
    "Documents/.env": "KIWI_TOKEN=kiwi\n",                              # a secret file name
    "Documents/keys/id_rsa": "kiwi private key\n",                      # a private key
    "Documents/cert.pem": "kiwi certificate\n",                         # *.pem
    "Documents/kiwi-credentials/notes.txt": "kiwi\n",                   # a *credential* folder
    ".quiet/kiwi-note.md": "kiwi hidden\n",                             # a hidden folder
    "Library/Preferences/kiwi.txt": "kiwi in Library\n",                # ~/Library (mac_index.exclude)
    "Library/Keychains/kiwi.keychain-db": "kiwi keychain\n",            # Keychains (hard rule)
    "Documents/Deck.key/Index/kiwi.iwa": "kiwi inside a package\n",     # the insides of a .key package
    ".ssh/kiwi_config": "kiwi\n",                                       # ~/.ssh (hard rule)
}

PREBUILD = r"""
import json, sys, time
from mse import config, macindex as mi, wholedisk as wd
root = sys.argv[1]
config.update_settings({"mac_index": {"roots": [root], "skip_protected": "never"}})
config.update_settings({"wholedisk": {"roots": [root], "priority": "low"}})
store = mi.Store()
excl = mi.manager().excluder()
rows = list(mi.walk_python(root, excl))
gen = store.next_gen()
mi.ingest(store, iter(rows), gen, excl)
store.sweep(root, gen)
store.set_meta("crawl", json.dumps({"gen": gen, "finished": time.time(), "rules": "contract-test", "roots": [root],
                                    "done": []}))
m = wd.Manager()
print(json.dumps({"sync": m.sync(full=True), "describe": m.describe_pending(gate=False)}))
"""


def _free_port() -> int:
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if port != FORBIDDEN_PORT:
            return port


def _port_open(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


class Http:
    def __init__(self, port: int, token: str):
        self.port, self.token = port, token

    def call(self, method: str, path: str, body=None, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            headers = {"X-MSE-Token": self.token} if token else {}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            r = conn.getresponse()
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, raw
        finally:
            conn.close()

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body if body is not None else {}, **kw)


class RealService(unittest.TestCase):
    """One sandboxed service for the class; every test reads it. Settings tests restore what they change."""

    @classmethod
    def setUpClass(cls):
        assert FORBIDDEN_PORT != 0
        cls.sandbox = Path(tempfile.mkdtemp(prefix="mse-contract-"))
        cls.home = cls.sandbox / "home"
        cls.mse_home = cls.sandbox / "msehome"
        cls.tmp = cls.sandbox / "tmp"
        for d in (cls.home, cls.mse_home, cls.tmp):
            d.mkdir(parents=True)
        old = time.time() - 7 * 86400                       # older than MIN_AGE_S, so nothing waits
        for rel, data in {**ENTRIES, **NEVER}.items():
            p = cls.home / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data if isinstance(data, bytes) else data.encode())
            os.utime(p, (old, old))
        cls.env = {k: v for k, v in os.environ.items()
                   if k not in ("MSE_HELPER", "MSE_LOCK", "MSE_CWD", "MSE_SUPERVISED", "MSE_UI_DIR")}
        cls.env.update({"HOME": str(cls.home), "TMPDIR": str(cls.tmp), "MSE_HOME": str(cls.mse_home),
                        "PYTHONDONTWRITEBYTECODE": "1"})
        pre = subprocess.run([sys.executable, "-c", PREBUILD, str(cls.home)], cwd=str(REPO), env=cls.env,
                             capture_output=True, text=True, timeout=120)
        assert pre.returncode == 0, pre.stderr[-2000:]
        cls.prebuild = json.loads(pre.stdout.strip().splitlines()[-1])
        cls.token = json.loads((cls.mse_home / "config.json").read_text())["token"]
        cls.port = _free_port()
        cls.log = open(cls.sandbox / "service.log", "w")
        cls.proc = subprocess.Popen([sys.executable, "-m", "mse", "serve", "--port", str(cls.port)], cwd=str(REPO),
                                    env=cls.env, stdout=cls.log, stderr=subprocess.STDOUT)
        cls.http = Http(cls.port, cls.token)
        deadline = time.monotonic() + 60
        while True:
            try:
                code, _ = cls.http.get("/api/disk/status")
                if code == 200:
                    break
            except OSError:
                pass
            if cls.proc.poll() is not None or time.monotonic() > deadline:
                cls.log.flush()
                raise AssertionError(f"the service did not come up:\n{(cls.sandbox / 'service.log').read_text()[-3000:]}")
            time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        if cls.proc.poll() is None:
            cls.proc.send_signal(signal.SIGTERM)
            try:
                cls.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
        cls.log.close()

    # ---- the status

    def test_status_has_exactly_the_contract_keys_and_domains(self):
        code, st = self.http.get("/api/disk/status")
        self.assertEqual(code, 200)
        self.assertEqual(set(st), STATUS_KEYS)
        self.assertIs(st["enabled"], True)
        self.assertEqual(st["roots"], [str(self.home)])
        self.assertIn(st["state"], STATES)
        self.assertIn(st["paused_reason"], PAUSED)
        self.assertIn(st["priority"], wd.PRIORITIES)
        self.assertIsInstance(st["name_index_ready"], bool)
        for k in ("files_seen", "files_described", "images_labelled", "pdfs_described", "docs_described",
                  "videos_described", "failed", "pending"):
            self.assertIsInstance(st[k], int, k)
        self.assertIsInstance(st["rate_per_s"], (int, float))
        self.assertTrue(st["eta_s"] is None or isinstance(st["eta_s"], int))

    def test_only_the_legitimate_files_are_entries(self):
        """The privacy rules on a live tree: the secrets, the hidden folder, ~/Library, the Keychains folder, ~/.ssh and
        the insides of a package are not entries, so files_seen is the count of the legitimate files alone."""
        code, st = self.http.get("/api/disk/status")
        self.assertEqual(code, 200)
        self.assertEqual(st["files_seen"], len(ENTRIES), st)
        self.assertEqual(st["docs_described"], 3)                   # the three text documents, read by the service's readers
        self.assertEqual(st["images_labelled"], 0)                  # no helper in the sandbox: the picture waits, name-only
        for q in ("kiwi", "id_rsa", "cert", "env", "keychain", "credentials"):
            code, res = self.http.get(f"/api/disk/search?q={q}")
            self.assertEqual(code, 200)
            self.assertEqual([r["path"] for r in res["results"]], [], q)

    # ---- the search

    def test_search_has_exactly_the_contract_keys_and_domains(self):
        code, res = self.http.get("/api/disk/search?q=zebra&limit=5")
        self.assertEqual(code, 200)
        self.assertEqual(set(res), SEARCH_KEYS)
        self.assertEqual(set(res["query"]), QUERY_KEYS)
        self.assertEqual(res["query"]["words"], [["zebra"]])
        self.assertEqual(res["query"]["kinds"], [])
        self.assertGreaterEqual(len(res["results"]), 2, res)
        self.assertIsInstance(res["took_ms"], (int, float))
        self.assertIs(res["has_more"], False)
        self.assertIsNone(res["next_offset"])
        for r in res["results"]:
            self.assertEqual(set(r), RESULT_KEYS, r)
            self.assertIn(r["kind"], wd.KINDS)
            self.assertIn(r["why"], WHYS)
            self.assertIsInstance(r["labels"], list)
            self.assertIsInstance(r["snippet"], str)
            self.assertNotIn("<mark>", r["snippet"])                # plain text; the apps highlight themselves
            self.assertIsInstance(r["size"], int)
            self.assertIsInstance(r["mtime"], int)
            self.assertIsInstance(r["score"], (int, float))
            self.assertTrue(r["path"].startswith(str(self.home) + "/"))
        names = {r["name"] for r in res["results"]}
        self.assertEqual(names, {"zebra-notes.md", "safari-day-two.txt"})
        by_name = {r["name"]: r for r in res["results"]}
        self.assertEqual(by_name["zebra-notes.md"]["why"], "name")
        self.assertEqual(by_name["safari-day-two.txt"]["why"], "text")
        self.assertIn("zebra", by_name["safari-day-two.txt"]["snippet"].lower())

    def test_paging_and_the_kind_filter(self):
        code, one = self.http.get("/api/disk/search?q=zebra&limit=1")
        self.assertEqual(code, 200)
        self.assertEqual(len(one["results"]), 1)
        self.assertIs(one["has_more"], True)
        self.assertEqual(one["next_offset"], 1)
        code, two = self.http.get("/api/disk/search?q=zebra&limit=1&offset=1")
        self.assertEqual(code, 200)
        self.assertEqual(len(two["results"]), 1)
        self.assertNotEqual(one["results"][0]["path"], two["results"][0]["path"])
        self.assertIs(two["has_more"], False)
        code, docs = self.http.get("/api/disk/search?q=zebra&kind=doc")
        self.assertEqual(code, 200)
        self.assertEqual({r["kind"] for r in docs["results"]}, {"doc"})
        code, imgs = self.http.get("/api/disk/search?q=holiday&kind=image")
        self.assertEqual(code, 200)
        self.assertEqual([r["name"] for r in imgs["results"]], ["holiday.jpg"])
        self.assertEqual(imgs["results"][0]["why"], "name")
        code, typed = self.http.get("/api/disk/search?q=photo+holiday")
        self.assertEqual(code, 200)
        self.assertEqual(typed["query"]["kinds"], ["image"])      # the type word became the kind filter

    def test_bad_search_parameters_are_a_400(self):
        for path in ("/api/disk/search?q=zebra&kind=bogus", "/api/disk/search?q=zebra&limit=abc",
                     "/api/disk/search?q=zebra&offset=x"):
            code, body = self.http.get(path)
            self.assertEqual(code, 400, path)
            self.assertIn("error", body)

    def test_the_routes_need_the_token(self):
        for method, path in (("GET", "/api/disk/status"), ("GET", "/api/disk/search?q=zebra"), ("POST", "/api/disk/rescan")):
            code, _ = self.http.call(method, path, {} if method == "POST" else None, token=False)
            self.assertEqual(code, 401, path)

    # ---- the rescan

    def test_rescan_answers_started(self):
        code, body = self.http.post("/api/disk/rescan")
        self.assertEqual(code, 200)
        self.assertEqual(set(body), RESCAN_KEYS)
        self.assertIs(body["started"], True)

    # ---- the settings

    def test_settings_round_trip(self):
        code, cfg = self.http.get("/api/settings")
        self.assertEqual(code, 200)
        self.assertIn("wholedisk", cfg)
        self.assertEqual(set(cfg["wholedisk"]), SETTINGS_KEYS)
        before = dict(cfg["wholedisk"])
        self.assertEqual(before["roots"], [str(self.home)])
        self.assertEqual(before["max_text_kb"], 8)
        try:
            code, reply = self.http.post("/api/settings", {"wholedisk": {"priority": "balanced", "max_text_kb": 16,
                                                                           "extra_excludes": ["Pictures", "*.tmp"]}})
            self.assertEqual(code, 200, reply)
            self.assertEqual(set(reply["wholedisk"]), SETTINGS_KEYS)
            self.assertEqual(reply["wholedisk"]["priority"], "balanced")
            self.assertEqual(reply["wholedisk"]["max_text_kb"], 16)
            self.assertEqual(reply["wholedisk"]["extra_excludes"], ["Pictures", "*.tmp"])
            code, again = self.http.get("/api/settings")
            self.assertEqual(again["wholedisk"], reply["wholedisk"])  # stored, not just echoed
            code, st = self.http.get("/api/disk/status")
            self.assertEqual(st["priority"], "balanced")
            code, imgs = self.http.get("/api/disk/search?q=holiday")
            self.assertEqual(imgs["results"], [])                     # the new exclude hides the picture at once
        finally:
            code, restored = self.http.post("/api/settings", {"wholedisk": before})
            self.assertEqual(code, 200, restored)
        code, cfg = self.http.get("/api/settings")
        self.assertEqual(cfg["wholedisk"], before)

    def test_bad_settings_are_refused_and_change_nothing(self):
        code, cfg = self.http.get("/api/settings")
        before = cfg["wholedisk"]
        bad = [{"priority": "high"}, {"max_text_kb": 0}, {"max_text_kb": 65}, {"max_text_kb": "8"},
               {"max_text_kb": True}, {"roots": []}, {"roots": "x"}, {"roots": ["relative/folder"]},
               {"extra_excludes": ["!keep"]}, {"extra_excludes": ["x"] * 201}, {"extra_excludes": "x"},
               {"enabled": "yes"}, {"images": 1}, {"nonsense": True}, "not an object"]
        for change in bad:
            code, body = self.http.post("/api/settings", {"wholedisk": change})
            self.assertEqual(code, 400, change)
            self.assertIn("wholedisk", body["error"])
        code, cfg = self.http.get("/api/settings")
        self.assertEqual(cfg["wholedisk"], before)

    def test_roots_are_normalised(self):
        code, cfg = self.http.get("/api/settings")
        before = cfg["wholedisk"]
        try:
            code, reply = self.http.post("/api/settings", {"wholedisk": {"roots": [str(self.home) + "/", "~/Documents",
                                                                                     str(self.home)]}})
            self.assertEqual(code, 200, reply)
            self.assertEqual(reply["wholedisk"]["roots"], [str(self.home), str(self.home / "Documents")])
        finally:
            self.http.post("/api/settings", {"wholedisk": before})

    # ---- the fixture stub: the same shape, field by field

    def test_the_fixture_stub_answers_the_same_shape(self):
        stub = subprocess.Popen([sys.executable, str(REPO / "scripts/fixtures/stub_service.py"), "--home", str(self.home),
                                 "--token", "stub-token", "--disk"], cwd=str(REPO), env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = stub.stdout.readline()
            self.assertTrue(line.startswith("PORT="), line)
            s = Http(int(line.strip().split("=", 1)[1]), "stub-token")
            # status
            code, st_s = s.get("/api/disk/status")
            code2, st_r = self.http.get("/api/disk/status")
            self.assertEqual((code, code2), (200, 200))
            self.assertEqual(set(st_s), set(st_r))
            self.assertIn(st_s["state"], STATES)
            self.assertIn(st_s["paused_reason"], PAUSED)
            self.assertIn(st_s["priority"], wd.PRIORITIES)
            for k, v in st_r.items():
                if v is not None and st_s[k] is not None:
                    self.assertIsInstance(st_s[k], type(v) if not isinstance(v, bool) else bool, k)
            # search: the top level, the query, every result
            code, se_s = s.get("/api/disk/search?q=zebra&limit=25")
            code2, se_r = self.http.get("/api/disk/search?q=zebra")
            self.assertEqual((code, code2), (200, 200))
            self.assertEqual(set(se_s), set(se_r))
            self.assertEqual(set(se_s["query"]), set(se_r["query"]))
            self.assertTrue(se_s["results"])
            for r in se_s["results"]:
                self.assertEqual(set(r), RESULT_KEYS, r)
                self.assertIn(r["kind"], wd.KINDS, r)
                self.assertIn(r["why"], WHYS, r)
                self.assertNotIn("<mark>", r["snippet"])
                self.assertTrue(all(isinstance(x, str) for x in r["labels"]))
                self.assertIsInstance(r["size"], int)
                self.assertIsInstance(r["mtime"], int)
            # paging as the service pages
            code, one = s.get("/api/disk/search?q=zebra&limit=1")
            self.assertEqual((one["has_more"], one["next_offset"]), (True, 1))
            code, imgs = s.get("/api/disk/search?q=zebra&kind=image")
            self.assertEqual({r["kind"] for r in imgs["results"]}, {"image"})
            # settings: the block's keys, and values the service's validation accepts
            code, cfg_s = s.get("/api/settings")
            self.assertEqual(set(cfg_s["wholedisk"]), SETTINGS_KEYS)
            wd.check_settings(cfg_s["wholedisk"])                  # raises ValueError on a value the service refuses
            code, saved = s.post("/api/settings", {"wholedisk": cfg_s["wholedisk"]})
            self.assertEqual(code, 200)
            self.assertEqual(set(saved["wholedisk"]), SETTINGS_KEYS)
            # rescan
            code, rs = s.post("/api/disk/rescan")
            self.assertEqual(code, 200)
            self.assertEqual(rs, {"started": True})
        finally:
            stub.terminate()
            try:
                stub.wait(timeout=10)
            except subprocess.TimeoutExpired:
                stub.kill()


class Stop(unittest.TestCase):
    """A second, short-lived service: SIGTERM ends it with exit code 0 and releases the port. The crash loops of earlier
    versions came from this path (server_close, the bounded freeze), so the whole-disk thread must not hold it up."""

    def test_sigterm_exits_zero_and_frees_the_port(self):
        sandbox = Path(tempfile.mkdtemp(prefix="mse-contract-stop-"))
        home, mse_home, tmp = sandbox / "home", sandbox / "msehome", sandbox / "tmp"
        for d in (home / "Documents", mse_home, tmp):
            d.mkdir(parents=True)
        (home / "Documents" / "note.txt").write_text("a note about a zebra\n")
        env = {k: v for k, v in os.environ.items()
               if k not in ("MSE_HELPER", "MSE_LOCK", "MSE_CWD", "MSE_SUPERVISED", "MSE_UI_DIR")}
        env.update({"HOME": str(home), "TMPDIR": str(tmp), "MSE_HOME": str(mse_home), "PYTHONDONTWRITEBYTECODE": "1"})
        pre = subprocess.run([sys.executable, "-c", PREBUILD, str(home)], cwd=str(REPO), env=env, capture_output=True,
                             text=True, timeout=120)
        self.assertEqual(pre.returncode, 0, pre.stderr[-2000:])
        token = json.loads((mse_home / "config.json").read_text())["token"]
        port = _free_port()
        with open(sandbox / "service.log", "w") as log:
            proc = subprocess.Popen([sys.executable, "-m", "mse", "serve", "--port", str(port)], cwd=str(REPO), env=env,
                                    stdout=log, stderr=subprocess.STDOUT)
            try:
                h = Http(port, token)
                deadline = time.monotonic() + 60
                while True:
                    try:
                        if h.get("/api/disk/status")[0] == 200:
                            break
                    except OSError:
                        pass
                    self.assertIsNone(proc.poll(), (sandbox / "service.log").read_text()[-2000:])
                    self.assertLess(time.monotonic(), deadline, "the service did not come up")
                    time.sleep(0.2)
                h.post("/api/disk/rescan")                         # the thread has work queued when the stop arrives
                t0 = time.monotonic()
                proc.send_signal(signal.SIGTERM)
                code = proc.wait(timeout=30)
                self.assertEqual(code, 0, (sandbox / "service.log").read_text()[-2000:])
                self.assertLess(time.monotonic() - t0, 25)
                self.assertFalse(_port_open(port))
            finally:
                if proc.poll() is None:
                    proc.kill()
        # no failure in the service's own code (the stdlib server prints a harmless traceback for a connection that sent
        # nothing, which a port probe is)
        log_text = (sandbox / "service.log").read_text()
        self.assertNotIn(str(REPO / "mse"), log_text, log_text[-3000:])


if __name__ == "__main__":
    unittest.main()
