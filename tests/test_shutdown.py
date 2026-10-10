"""The stop path of the background service: a new instance binds while the old one is still shutting down, a stop is
bounded and starts no queued job, a stop is never reported as a read timeout (so never as a crash strike), and the
LaunchAgent waits longer than the supervisor does. No model is loaded and no real folder is read.
Run: .venv/bin/python -m unittest discover -s tests"""

import errno
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import indexer, jobs, paths, server, service  # noqa: E402


def _holder():
    """A listening socket on a free port, held by the test. Returns (socket, port)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


class BindRetry(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(server, "log")          # keep the test out of the real log
        patch.start()
        self.addCleanup(patch.stop)

    def test_binds_once_the_previous_instance_has_let_go_of_the_port(self):
        held, port = _holder()
        threading.Timer(1.0, held.close).start()
        t0 = time.monotonic()
        httpd = server._bind(port, window_s=10)
        try:
            self.assertLess(time.monotonic() - t0, 8)
            self.assertEqual(httpd.server_address[1], port)
        finally:
            httpd.server_close()

    def test_gives_up_after_the_window_with_address_in_use(self):
        held, port = _holder()
        self.addCleanup(held.close)
        t0 = time.monotonic()
        with self.assertRaises(OSError) as cm:
            server._bind(port, window_s=0.6)
        self.assertEqual(cm.exception.errno, errno.EADDRINUSE)
        self.assertLess(time.monotonic() - t0, 3)

    def test_other_bind_errors_are_not_retried(self):
        with mock.patch.object(server, "ThreadingHTTPServer", side_effect=OSError(errno.EACCES, "denied")):
            t0 = time.monotonic()
            with self.assertRaises(OSError):
                server._bind(1, window_s=10)
            self.assertLess(time.monotonic() - t0, 1)


class BoundedStop(unittest.TestCase):
    def setUp(self):
        paths.ensure_dirs()
        jobs.QUEUE.write_text("[]")           # a new JobManager restores whatever the previous test left queued
        patch = mock.patch.object(jobs.JobManager, "_scheduler", lambda self: None)   # no nightly or periodic runs
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(setattr, indexer, "STOPPING", indexer.STOPPING)

    def test_freeze_returns_at_once_and_wait_idle_is_bounded(self):
        jm = jobs.JobManager()
        started, release = threading.Event(), threading.Event()

        def stuck(job):                       # a job that never reaches a cancel check
            started.set()
            release.wait(30)
            return {}

        jm._run = stuck
        jm.submit("learn")
        self.assertTrue(started.wait(5))
        t0 = time.monotonic()
        jm.freeze()
        self.assertLess(time.monotonic() - t0, 1)
        t0 = time.monotonic()
        self.assertFalse(jm.wait_idle(0.5))
        self.assertLess(time.monotonic() - t0, 2)
        release.set()

    def test_a_stop_starts_no_queued_job_and_keeps_the_queue(self):
        jm = jobs.JobManager()
        ran, started = [], threading.Event()

        def run(job):                         # a job that stops when it is cancelled, as real jobs do
            ran.append(job["kind"])
            started.set()
            while not jm._cancel:
                time.sleep(0.01)
            raise indexer.Cancelled()

        jm._run = run
        jm.submit("learn")
        jm.submit("finetune")
        self.assertTrue(started.wait(5))
        jm.freeze()
        self.assertTrue(jm.wait_idle(5))
        time.sleep(0.2)                       # give the job loop the chance to start the next job, if it would
        self.assertEqual(ran, ["learn"])
        saved = json.loads(jobs.QUEUE.read_text())
        self.assertEqual([j["kind"] for j in saved], ["learn", "finetune"])   # resumed by the next start


class StopIsNotAStrike(unittest.TestCase):
    def test_a_read_cut_short_by_a_stop_is_not_reported_as_a_timeout(self):
        fut = Future()                        # never completes
        starts = {"slow.pdf": time.monotonic() - 10_000}      # far past any time limit
        with self.assertRaises(indexer.Cancelled):
            indexer._await(fut, "slow.pdf", starts, 1.0, lambda: True)


class LaunchAgent(unittest.TestCase):
    def test_launchd_waits_longer_than_the_supervisor_does(self):
        p = service.plist("/nonexistent/MySearch.app/Contents/MacOS/MySearch")
        self.assertGreater(p["ExitTimeOut"], 30)          # the supervisor waits up to 30 s for its child
        self.assertEqual(p["ThrottleInterval"], 15)


if __name__ == "__main__":
    unittest.main()
