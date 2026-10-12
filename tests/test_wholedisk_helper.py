"""The whole-disk describer on real inputs through the real msehelper. Runs when MSE_TEST_HELPER points at a built binary
(scripts/build_helper.sh <dir>); otherwise every test is skipped. The inputs are made here: a generated PNG, a PDF from
textutil (macOS), a page of it rendered by msehelper, and a poster frame from ffmpeg when it is installed."""

import json
import os
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
import zlib

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

from mse import wholedisk as wd  # noqa: E402
from tests.test_wholedisk import BASE, OLD, _configure, _crawl, _manager, _root, _write, isolate  # noqa: E402

HELPER = os.environ.get("MSE_TEST_HELPER", "")
HAVE_HELPER = bool(HELPER) and os.path.exists(HELPER)


def _png(path: str, w: int = 96, h: int = 96) -> str:
    """A small PNG: a red square on a blue ground, written by hand (no image library needed)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            red = 200 if 24 <= x < 72 and 24 <= y < 72 else 30
            row += bytes((red, 40, 180 if red == 30 else 20))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    return path


def _text_pdf(text: str, folder: str) -> str:
    """A one-page PDF whose text layer is `text` (Helvetica, ASCII), written by hand with a correct cross-reference table.
    macOS textutil cannot write PDF, so the test writes one itself."""
    os.makedirs(folder, exist_ok=True)
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")  # noqa: E731
    body = " ".join(f"({esc(line)}) Tj 0 -24 Td" for line in text.splitlines())
    stream = f"BT /F1 18 Tf 72 720 Td {body} ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path = os.path.join(folder, "invoice.pdf")
    with open(path, "wb") as f:
        f.write(bytes(out))
    return path


@unittest.skipUnless(HAVE_HELPER, "MSE_TEST_HELPER is not set to a built msehelper")
class RealHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        isolate(cls.addClassCleanup)
        cls._old_helper = os.environ.get("MSE_HELPER")
        os.environ["MSE_HELPER"] = HELPER
        cls.work = os.path.realpath(tempfile.mkdtemp(prefix="wd-helper-", dir=BASE))

    @classmethod
    def tearDownClass(cls):
        if cls._old_helper is None:
            os.environ.pop("MSE_HELPER", None)
        else:
            os.environ["MSE_HELPER"] = cls._old_helper

    def _describe(self, items, *opts):
        payload = "".join(json.dumps(i) + "\n" for i in items)
        out = subprocess.run([HELPER, "describe", "--stdin", *opts], input=payload.encode(), capture_output=True,
                             timeout=120)
        return [json.loads(line) for line in out.stdout.decode().splitlines() if line.strip()]

    def test_probe_answers_describe_true(self):
        out = subprocess.run([HELPER, "describe", "--probe"], capture_output=True, timeout=30)
        self.assertEqual(json.loads(out.stdout.decode().strip().splitlines()[-1]), {"describe": True, "version": 1})

    def test_the_existing_features_answer_is_unchanged(self):
        out = subprocess.run([HELPER, "features", "-"], capture_output=True, timeout=30)
        d = json.loads(out.stdout.decode().strip().splitlines()[-1])
        self.assertNotIn("describe", d)                          # describe is probed, not advertised here

    def test_an_image_gets_labels_one_line_per_input(self):
        img = _png(os.path.join(self.work, "square.png"))
        res = self._describe([{"id": 1, "path": img, "kind": "image"},
                              {"id": 2, "path": os.path.join(self.work, "missing.jpg"), "kind": "image"},
                              {"id": "x", "path": img, "kind": "unknown"}])
        self.assertEqual([r["id"] for r in res], [1, 2, "x"])
        self.assertTrue(res[0]["ok"])
        self.assertIsInstance(res[0]["labels"], list)
        self.assertFalse(res[1]["ok"])
        self.assertEqual(res[1]["error"], "missing")
        self.assertFalse(res[2]["ok"])

    def test_bad_input_line_is_answered_not_fatal(self):
        out = subprocess.run([HELPER, "describe", "--stdin"], input=b"not json\n", capture_output=True, timeout=30)
        self.assertEqual(json.loads(out.stdout.decode().strip())["ok"], False)

    def test_a_pdf_text_layer_is_read(self):
        pdf = _text_pdf("Rechnung Nr. 4711 der Telekom Deutschland GmbH.\n", os.path.join(self.work, "pdf1"))
        res = self._describe([{"id": 1, "path": pdf, "kind": "pdf"}])[0]
        self.assertTrue(res["ok"], res)
        self.assertIn("Telekom", res["text"])
        self.assertEqual(res["ocr"], False)
        self.assertEqual(res["lang"], "de")

    def test_a_page_read_by_ocr_gives_words(self):
        pdf = _text_pdf("Rechnung Telekom Anzahlung\n", os.path.join(self.work, "ocr1"))
        render_dir = os.path.join(self.work, "ocr1", "render")
        subprocess.run([HELPER, "render", pdf, "--pages", "1", "--out", render_dir, "--max-px", "1400"],
                       capture_output=True, timeout=120, check=True)
        jpg = os.path.join(render_dir, "page-1.jpg")
        res = self._describe([{"id": 1, "path": jpg, "kind": "image"}], "--ocr", "always", "--text-px", "1400")[0]
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["ocr"])
        words = res["text"].lower()
        self.assertTrue(any(w in words for w in ("rechnung", "telekom", "anzahlung")), res["text"][:200])

    def test_ocr_off_never_reads_text(self):
        img = _png(os.path.join(self.work, "square2.png"))
        res = self._describe([{"id": 1, "path": img, "kind": "image"}], "--ocr", "off")[0]
        self.assertFalse(res["ocr"])
        self.assertEqual(res["text"], "")

    def test_a_video_poster_frame_is_labelled(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg is not installed")
        mp4 = os.path.join(self.work, "clip.mp4")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=5",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", mp4], check=True, capture_output=True, timeout=120)
        res = self._describe([{"id": 1, "path": mp4, "kind": "video"}])[0]
        self.assertTrue(res["ok"], res)
        self.assertIsInstance(res["labels"], list)

    def test_a_pages_document_is_read_through_its_preview(self):
        root = _root()
        _configure(root)
        pdf = _text_pdf("Kuendigung des Vertrags zum Monatsende\n", os.path.join(self.work, "pages1"))
        pkg = os.path.join(root, "Documents", "Brief.pages")
        os.makedirs(os.path.join(pkg, "QuickLook"), exist_ok=True)
        shutil.copyfile(pdf, os.path.join(pkg, "QuickLook", "Preview.pdf"))
        os.utime(pkg, (OLD, OLD))
        _write(f"{root}/Documents/plain.txt", "Nothing special here.\n")
        _crawl(root)
        m = _manager()                                           # the real describer, the real helper
        m.sync(full=True)
        out = m.describe_pending(limit=None)
        self.assertEqual(out["failed"], 0, m.last_error)
        res = m.search("Kuendigung", limit=5)
        self.assertTrue(any(r["path"].endswith("Brief.pages") for r in res["results"]), res)

    def test_the_manager_describes_a_tree_with_the_real_helper(self):
        root = _root()
        _configure(root)
        _png(os.path.join(root, "Documents", "square.png"))
        os.utime(os.path.join(root, "Documents", "square.png"), (OLD, OLD))
        _write(f"{root}/Documents/memo.txt", "Die Zusammenarbeit beginnt im Oktober.\n")
        _crawl(root)
        m = _manager()
        m.sync(full=True)
        out = m.describe_pending(limit=None)
        self.assertEqual(out["failed"], 0, m.last_error)
        c = m.store.writer()
        self.assertEqual(c.execute("SELECT count(*) FROM files WHERE state = ?", (wd.ST_DONE,)).fetchone()[0], 2)
        self.assertTrue(m.search("Zusammenarbeit", limit=5)["results"])


if __name__ == "__main__":
    unittest.main()
