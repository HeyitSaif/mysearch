"""Reader processes (mse/readers.py): a read past its limit is killed and reported as a failure, a stop ends a read at once,
a failed read keeps its message, and the pure-Python readers give the same result in a child process as in-process.

The slow and failing readers below are test-only: a child process imports this module to find them by name. Only
temporary files are read. Run: .venv/bin/python -m unittest -v tests.test_indexing_safety_readers"""

import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import extract, indexer, readers, store  # noqa: E402

SLOW = "tests.test_indexing_safety_readers:slow_light"
BOOM = "tests.test_indexing_safety_readers:boom_light"
GRANDCHILD = "tests.test_indexing_safety_readers:grandchild_light"
NOISY = "tests.test_indexing_safety_readers:noisy_light"
EMPTY = {"title": None, "lang": None, "pages": None, "items": [], "heavy": None}


def slow_light(path, kind, ext, media):
    """Test-only reader: a file whose name contains 'slow' hangs, every other file gets an empty result."""
    if "slow" in Path(path).name:
        time.sleep(60)
    return dict(EMPTY)


def boom_light(path, kind, ext, media):
    """Test-only reader: every read fails with the same error."""
    raise ValueError("boom")


def grandchild_light(path, kind, ext, media):
    """Test-only reader: starts a long subprocess, as textutil or the helper does, records its pid, then hangs."""
    import subprocess
    child = subprocess.Popen(["/bin/sleep", "600"])
    Path(str(path) + ".pid").write_text(str(child.pid))
    time.sleep(600)
    return dict(EMPTY)


def noisy_light(path, kind, ext, media):
    """Test-only reader: writes straight to file descriptor 1, which is the reply channel unless it is kept private."""
    os.write(1, b"not a reply " * 8)
    return dict(EMPTY)


def _folder(files: dict[str, bytes | str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="mse-readers-"))
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, str):
            p.write_text(body, encoding="utf-8")
        else:
            p.write_bytes(body)
    return root


class ResultsMatchInProcess(unittest.TestCase):
    """The indexer must store the same thing whether a pure-Python reader ran in-process or in a reader process."""

    def setUp(self):
        self.root = _folder({
            "note.md": "# Title\n\nSome words about a budget.\n\n## Part\n\nMore words.\n",
            "page.html": "<html><head><title>Plan</title></head><body><h1>Heading</h1><p>Body text here.</p></body></html>",
            "mail.eml": "From: a@example.test\nTo: b@example.test\nSubject: Hello\n\nA short message body.\n",
            "log.txt": "plain text line one\nplain text line two\n",
            "bundle.zip": b"",
        })
        import zipfile
        with zipfile.ZipFile(self.root / "bundle.zip", "w") as z:
            z.writestr("inside/readme.txt", "member text")
            z.writestr("inside/data.csv", "a,b\n1,2\n")

    def test_each_reader_gives_the_same_result_in_a_child_process(self):
        with readers.ReaderPool(size=2, timeout_s=60) as pool:
            for rel in ("note.md", "page.html", "mail.eml", "log.txt", "bundle.zip"):
                p = self.root / rel
                ext = p.suffix.lstrip(".").lower()
                kind = extract.kind_of(ext)
                expected = extract.light(p, kind, ext, {})
                got = pool.run(p, kind, ext, {})
                self.assertEqual(got, expected, rel)

    def test_prepare_gives_the_same_row_data_with_and_without_the_pool(self):
        coll = {"name": "t" + uuid.uuid4().hex[:10], "path": str(self.root), "media": {}}
        with readers.ReaderPool(size=1, timeout_s=60) as pool:
            for rel in ("note.md", "mail.eml", "log.txt"):
                self.assertEqual(indexer._prepare(coll, rel, "full"),
                                 indexer._prepare(coll, rel, "full", None, pool), rel)


class ReaderLimits(unittest.TestCase):

    def test_a_read_past_its_limit_is_killed_and_the_next_read_still_works(self):
        root = _folder({"slow.txt": "x", "fast.txt": "y"})
        pool = readers.ReaderPool(size=1, timeout_s=1.0, target=SLOW)
        try:
            started = time.monotonic()
            with self.assertRaises(readers.ReaderTimeout):
                pool.run(root / "slow.txt", "text", "txt", {})
            self.assertLess(time.monotonic() - started, 15)
            self.assertEqual(pool._live, 0)                       # the killed process is not counted any more
            got = pool.run(root / "fast.txt", "text", "txt", {})  # a replacement process answers
            self.assertEqual(got, EMPTY)
        finally:
            pool.close()

    def test_a_killed_read_takes_its_own_subprocesses_with_it(self):
        root = _folder({"hang.txt": "x"})
        pool = readers.ReaderPool(size=1, timeout_s=4, target=GRANDCHILD)
        try:
            with self.assertRaises(readers.ReaderTimeout):
                pool.run(root / "hang.txt", "text", "txt", {})
        finally:
            pool.close()
        pid = int((root / "hang.txt.pid").read_text())
        gone = False
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                gone = True
                break
            time.sleep(0.1)
        self.assertTrue(gone, "the subprocess of a killed read is still running")

    def test_a_read_cannot_write_into_the_reply_channel(self):
        root = _folder({"any.txt": "x"})
        with readers.ReaderPool(size=1, timeout_s=60, target=NOISY) as pool:
            self.assertEqual(pool.run(root / "any.txt", "text", "txt", {}), EMPTY)

    def test_a_guarded_kind_gets_the_limit_of_its_own_subprocesses(self):
        class Recorder:
            timeout_s = 120.0
            reported = False

            def __init__(self):
                self.limits = []

            def run(self, p, kind, ext, media, timeout_s=None):
                self.limits.append(timeout_s)
                return dict(EMPTY)

        coll = {"name": "t" + uuid.uuid4().hex[:10], "path": tempfile.mkdtemp(prefix="mse-limit-"), "media": {}}
        rec = Recorder()
        indexer._prepare(coll, "book.docx", "full", None, rec)     # office: waits on a helper of up to 300 s
        indexer._prepare(coll, "note.txt", "full", None, rec)      # text: the configured limit
        self.assertEqual(rec.limits, [330.0, 120.0])

    def test_a_stop_ends_a_read_within_a_few_seconds(self):
        root = _folder({"slow.txt": "x"})
        stop = threading.Event()
        pool = readers.ReaderPool(size=1, timeout_s=60, target=SLOW, should_stop=stop.is_set)
        timer = threading.Timer(0.8, stop.set)
        timer.start()
        try:
            started = time.monotonic()
            with self.assertRaises(readers.Stopped):
                pool.run(root / "slow.txt", "text", "txt", {})
            self.assertLess(time.monotonic() - started, 8)
        finally:
            timer.cancel()
            pool.close()

    def test_a_failed_read_reports_the_same_text_as_in_process(self):
        root = _folder({"any.txt": "x"})
        with readers.ReaderPool(size=1, timeout_s=60, target=BOOM) as pool:
            with self.assertRaises(readers.ReaderError) as ctx:
                pool.run(root / "any.txt", "text", "txt", {})
        self.assertEqual(str(ctx.exception), "ValueError: boom")
        self.assertNotIsInstance(ctx.exception, readers.ReaderTimeout)

    def test_an_interpreter_that_cannot_answer_falls_back_in_process_without_a_strike(self):
        root = _folder({"note.txt": "fallback words"})
        coll = {"name": "t" + uuid.uuid4().hex[:10], "path": str(root), "media": {}}
        expected = indexer._prepare(coll, "note.txt", "full")
        with mock.patch.object(readers, "_PYTHON", "/usr/bin/false"):      # exits at once: never answers the ping
            pool = readers.ReaderPool(size=1, timeout_s=60)
            try:
                got = indexer._prepare(coll, "note.txt", "full", None, pool)
            finally:
                pool.close()
        self.assertIsNone(got["error"])                          # no strike: the file is not at fault
        self.assertEqual(got, expected)                          # and it is read, in-process, to the same result
        self.assertTrue(pool.reported)
        self.assertIsNotNone(pool.broken)

    def test_close_kills_the_idle_processes(self):
        root = _folder({"fast.txt": "y"})
        pool = readers.ReaderPool(size=2, timeout_s=60)
        pool.run(root / "fast.txt", "text", "txt", {})
        self.assertEqual(pool._live, 1)
        pool.close()
        self.assertEqual(pool._live, 0)
        with self.assertRaises(readers.Stopped):
            pool.run(root / "fast.txt", "text", "txt", {})

    def test_a_scan_records_a_timed_out_read_as_a_strike_and_indexes_the_rest(self):
        folder = _folder({"slow.txt": "never read", "fine.txt": "read me"})
        coll = {"name": "t" + uuid.uuid4().hex[:10], "path": str(folder), "media": {}, "exclude": [],
                "metadata_only": []}

        def pool_for_scan(c, cancel=None):
            return readers.ReaderPool(size=2, timeout_s=1, target=SLOW, should_stop=lambda: False)

        with mock.patch.object(indexer, "_new_reader", pool_for_scan):
            s = indexer.scan(coll)
        conn = store.connect(coll["name"], readonly=True)
        try:
            strike = conn.execute("SELECT count, last_error FROM failures WHERE path='slow.txt'").fetchone()
            fine = conn.execute("SELECT state FROM files WHERE path='fine.txt'").fetchone()
        finally:
            conn.close()
        self.assertEqual(strike["count"], 1)
        self.assertEqual(strike["last_error"], "timed out after 1 s")
        self.assertEqual(fine["state"], "text")
        self.assertEqual(s["errors"], 1)


if __name__ == "__main__":
    unittest.main()
