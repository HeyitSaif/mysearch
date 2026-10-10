"""The HTTP service's access rules: the web UI pages need the token, the static files do not, files from indexed folders
come back with safe types and headers, and the static route refuses path tricks. No model is loaded and no real folder
is read: the index lookup is replaced by a fake that names files in a temporary folder.
Run: .venv/bin/python -m unittest discover -s tests"""

import http.client
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import config, paths, server  # noqa: E402


class Running(unittest.TestCase):
    """An in-process HTTP server on a free port, with the real request handler."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.token = config.load()["token"]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path, headers=None):
        """-> (status, lower-cased headers, body bytes)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request("GET", path, headers=headers or {})
            r = conn.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            conn.close()


class PageAccess(Running):
    """The web UI and Quick Search pages embed the token, so only a request that carries it gets them."""

    def test_pages_refuse_a_request_without_the_token(self):
        for path in ("/", "/index.html", "/settings", "/settings/ask", "/?mini=1"):
            with self.subTest(path=path):
                code, h, body = self.get(path)
                self.assertEqual(code, 401)
                self.assertIn(b"Open MySearch from the app or run <code>mse ui</code>.", body)
                self.assertNotIn(self.token.encode(), body)
                self.assertEqual(h["referrer-policy"], "no-referrer")

    def test_pages_refuse_a_wrong_or_malformed_token(self):
        for path in (f"/?t={self.token[:-1]}", "/?t=", "/?t=nope", f"/?t={self.token}x", "/?t=%C3%A9",
                     "/settings?t=%C3%A9"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 401)
        self.assertEqual(self.get("/", {"X-MSE-Token": ""})[0], 401)
        self.assertEqual(self.get("/", {"X-MSE-Token": "é"})[0], 401)

    def test_a_page_with_the_token_is_served(self):
        for path in (f"/?t={self.token}", f"/?mini=1&t={self.token}"):
            with self.subTest(path=path):
                code, h, body = self.get(path)
                self.assertEqual(code, 200)
                self.assertIn(f'content="{self.token}"'.encode(), body)   # the page reads the token from this meta tag
                self.assertIn("default-src 'self'", h["content-security-policy"])
                self.assertEqual(h["referrer-policy"], "no-referrer")
                self.assertEqual(h["cache-control"], "no-store")

    def test_the_header_opens_a_page_too(self):
        code, _, body = self.get("/", {"X-MSE-Token": self.token})
        self.assertEqual(code, 200)
        self.assertIn(f'content="{self.token}"'.encode(), body)

    def test_api_takes_the_token_in_the_header_or_the_query(self):
        self.assertEqual(self.get("/api/status")[0], 401)
        self.assertEqual(self.get("/api/status", {"X-MSE-Token": self.token})[0], 200)
        self.assertEqual(self.get(f"/api/status?t={self.token}")[0], 200)
        self.assertEqual(self.get("/api/status?t=wrong")[0], 401)

    def test_no_configured_token_refuses_everything(self):
        """A config without a token must not let a request through (None compared with None used to match)."""
        with mock.patch.object(config, "load", return_value={"token": None}):
            self.assertEqual(self.get("/api/status")[0], 401)
            self.assertEqual(self.get("/")[0], 401)


class StaticFiles(Running):
    """/static holds the page's own CSS, scripts and icon: public, and without any token in them."""

    def test_static_files_need_no_token_and_hold_none(self):
        code, _, body = self.get("/static/app.js")
        self.assertEqual(code, 200)
        self.assertNotIn(self.token.encode(), body)
        code, _, body = self.get("/static/index.html")
        self.assertEqual(code, 200)
        self.assertIn(b"{{TOKEN}}", body)            # the template: only the page route puts the token in
        self.assertNotIn(self.token.encode(), body)

    def test_static_refuses_paths_outside_the_ui_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "ui").mkdir()
            (base / "ui_evil").mkdir()
            (base / "ui" / "ok.txt").write_text("fine")
            (base / "ui_evil" / "secret.txt").write_text("not for the web")
            with mock.patch.object(paths, "UI", base / "ui"):
                self.assertEqual(self.get("/static/ok.txt")[0], 200)
                # the sibling folder starts with the same characters as ui/: a string prefix check served it
                self.assertEqual(self.get("/static/../ui_evil/secret.txt")[0], 404)
                self.assertEqual(self.get("/static/../../../../../../etc/hosts")[0], 404)


class FileRoutes(Running):
    """/api/file and /api/thumb: the types and headers they send. The index lookup is a fake over a temporary folder."""

    FILES = {"page.svg": b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
             "page.html": b"<script>alert(1)</script>",
             "page.xhtml": b"<html xmlns='http://www.w3.org/1999/xhtml'></html>",
             "data.xml": b"<x/>",
             "doc.pdf": b"%PDF-1.4 test",
             "photo.jpg": b"\xff\xd8\xff test",
             "clip.mp4": b"\x00\x00\x00\x18ftyp test",
             "notes.md": b"# hello"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.tmp.name) / "folder"
        cls.folder.mkdir()
        for name, data in cls.FILES.items():
            (cls.folder / name).write_bytes(data)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        super().tearDownClass()

    def setUp(self):
        patch = mock.patch.object(server.search, "find_file", side_effect=self.fake_find)
        patch.start()
        self.addCleanup(patch.stop)

    def fake_find(self, collection, ref):
        if ref in self.FILES:
            return {"name": "c1", "path": str(self.folder)}, {"id": 42, "path": ref, "mode": "text"}
        if ref.startswith("meta-"):
            return {"name": "c1", "path": str(self.folder)}, {"id": 43, "path": ref, "mode": "meta"}
        return None

    def file_url(self, name):
        return f"/api/file?c=c1&path={name}&t={self.token}"

    def test_active_types_come_back_as_text_with_the_policy(self):
        for name in ("page.svg", "page.html", "page.xhtml", "data.xml", "notes.md"):
            with self.subTest(name=name):
                code, h, body = self.get(self.file_url(name))
                self.assertEqual(code, 200)
                self.assertTrue(h["content-type"].startswith("text/plain"), h["content-type"])
                self.assertIn("sandbox", h["content-security-policy"])
                self.assertEqual(h["x-content-type-options"], "nosniff")
                self.assertEqual(body, self.FILES[name])

    def test_pdf_images_audio_and_video_keep_their_types(self):
        code, h, body = self.get(self.file_url("doc.pdf"))
        self.assertEqual((code, h["content-type"]), (200, "application/pdf"))
        self.assertNotIn("content-security-policy", h)     # a sandbox would turn off the PDF plugin in the preview frame
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertEqual(body, self.FILES["doc.pdf"])
        code, h, _ = self.get(self.file_url("photo.jpg"))
        self.assertEqual((code, h["content-type"]), (200, "image/jpeg"))
        self.assertIn("sandbox", h["content-security-policy"])
        code, h, _ = self.get(self.file_url("clip.mp4"))
        self.assertEqual((code, h["content-type"]), (200, "video/mp4"))

    def test_range_requests_keep_the_type(self):
        code, h, body = self.get(self.file_url("doc.pdf"), {"Range": "bytes=0-3"})
        self.assertEqual(code, 206)
        self.assertEqual(h["content-type"], "application/pdf")
        self.assertEqual(h["content-range"], f"bytes 0-3/{len(self.FILES['doc.pdf'])}")
        self.assertEqual(body, b"%PDF")

    def test_file_routes_need_the_token(self):
        self.assertEqual(self.get("/api/file?c=c1&path=doc.pdf")[0], 401)
        self.assertEqual(self.get("/api/thumb?c=c1&path=photo.jpg")[0], 401)

    def test_metadata_only_and_unknown_files_are_refused(self):
        self.assertEqual(self.get(self.file_url("meta-secret.txt"))[0], 403)
        self.assertEqual(self.get(self.file_url("missing.txt"))[0], 400)

    def test_thumbnail_is_a_jpeg_with_the_policy(self):
        with tempfile.TemporaryDirectory() as thumbs:
            (Path(thumbs) / "c1").mkdir()
            (Path(thumbs) / "c1" / "42.jpg").write_bytes(b"\xff\xd8\xff thumb")
            with mock.patch.object(paths, "THUMBS", Path(thumbs)):
                code, h, body = self.get(f"/api/thumb?c=c1&path=photo.jpg&t={self.token}")
        self.assertEqual((code, h["content-type"]), (200, "image/jpeg"))
        self.assertIn("sandbox", h["content-security-policy"])
        self.assertEqual(body, b"\xff\xd8\xff thumb")


class MseUi(unittest.TestCase):
    """`mse ui` opens the web UI with the token in the URL. The browser launch is mocked: nothing is opened."""

    def test_mse_ui_opens_the_page_with_the_token(self):
        from urllib.parse import parse_qs, urlparse
        from mse import cli
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(cli.subprocess, "run") as run, \
                mock.patch("pathlib.Path.home", return_value=Path(home)):      # no ~/Applications/MySearch.app here
            cli.cmd_ui(None)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "open")
        url = urlparse(argv[1])
        self.assertEqual((url.hostname, url.path), ("127.0.0.1", "/"))
        self.assertEqual(parse_qs(url.query), {"t": [config.load()["token"]]})


class TypeMapping(unittest.TestCase):
    """Without a server: which Content-Type a file from an indexed folder is sent with."""

    def test_the_kinds_the_web_ui_shows_keep_their_type(self):
        self.assertEqual(server.served_type(Path("a.pdf")), "application/pdf")
        self.assertEqual(server.served_type(Path("a.jpg")), "image/jpeg")
        self.assertEqual(server.served_type(Path("a.mp3")), "audio/mpeg")
        self.assertEqual(server.served_type(Path("a.mov")), "video/quicktime")

    def test_everything_else_is_text(self):
        for name in ("a.svg", "a.html", "a.htm", "a.xhtml", "a.xml", "a.xsl", "a.mhtml", "a.js", "a.zip", "a.unknownext"):
            with self.subTest(name=name):
                self.assertTrue(server.served_type(Path(name)).startswith("text/plain"))

    def test_the_csp_is_left_off_only_for_pdf(self):
        self.assertNotIn("Content-Security-Policy", server.file_headers("application/pdf"))
        self.assertEqual(server.file_headers("image/jpeg")["Content-Security-Policy"], server.FILE_CSP)
        self.assertEqual(server.file_headers("text/plain; charset=utf-8", {"X-A": "1"})["X-A"], "1")


if __name__ == "__main__":
    unittest.main()
