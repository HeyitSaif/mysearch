"""PNG privacy for the public tree: no device colour profile, no Exif or text metadata in any publishable PNG, and removing
that metadata never changes a pixel. Standard library, Pillow and numpy (project dependencies). Writes only to temp folders.
Run: .venv/bin/python -m unittest tests.test_png_privacy -v"""

import contextlib
import hashlib
import importlib.util
import io
import os
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
GOLDENS = ("ask", "columns-deep", "columns-file-then-folder", "icons-documents", "list-documents", "search-mac")
PUBLIC_ICONS = ("app/AppIcon-1024.png", "integrations/raycast/assets/icon.png")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = _load("oss_audit", "scripts/oss_audit.py")
snap = _load("compare_snapshots", "scripts/fixtures/compare_snapshots.py")


def publishable_pngs():
    """The PNGs the public tree can contain: git's listing of tracked and not-ignored files, the same listing that
    export_public.sh copies. Without git, a walk that skips .git, .venv and build output."""
    try:
        listing = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                                 capture_output=True, check=True).stdout.decode("utf-8")
        names = [n for n in listing.split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in (".git", ".venv", "build", "node_modules", "dist", "__pycache__")]
            names += [os.path.relpath(os.path.join(dirpath, f), ROOT).replace(os.sep, "/") for f in filenames]
    return sorted(n for n in names if n.lower().endswith(".png") and (ROOT / n).is_file())


def _chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def make_png(extra=()):
    """A 16 x 12 RGBA PNG built by hand. The extra chunks, already encoded, go between IHDR and IDAT."""
    w, h = 16, 12
    rows = b"".join(b"\x00" + b"".join(bytes(((x * 16) & 255, (y * 20) & 255, (x * y) & 255, 255 - x)) for x in range(w))
                    for y in range(h))
    return (PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + b"".join(extra) + _chunk(b"IDAT", zlib.compress(rows)) + _chunk(b"IEND", b""))


def metadata_chunks():
    """One chunk of each type the filter must drop, with invented contents."""
    return [
        _chunk(b"iCCP", b"invented-profile\x00\x00" + zlib.compress(bytes(64))),
        _chunk(b"eXIf", b"MM\x00*\x00\x00\x00\x08" + bytes(8)),
        _chunk(b"tEXt", b"Comment\x00invented note"),
        _chunk(b"zTXt", b"Comment\x00\x00" + zlib.compress(b"invented note")),
        _chunk(b"iTXt", b"Comment\x00\x00\x00\x00\x00invented note"),
        _chunk(b"tIME", struct.pack(">HBBBBB", 2026, 10, 10, 12, 0, 0)),
    ]


def kept_chunks():
    return [_chunk(b"pHYs", struct.pack(">IIB", 2835, 2835, 1)), _chunk(b"sRGB", b"\x00")]


def raw_chunks(data):
    """(type, whole chunk bytes including length and CRC) in file order."""
    out, i = [], 8
    while i < len(data):
        length = struct.unpack(">I", data[i:i + 4])[0]
        end = i + 12 + length
        out.append((data[i + 4:i + 8], data[i:end]))
        i = end
    return out


def idat_stream(data):
    return b"".join(raw[8:-4] for kind, raw in raw_chunks(data) if kind == b"IDAT")


def rgba(data):
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGBA"))


class RepoPngTests(unittest.TestCase):
    def test_listing_covers_the_public_images(self):
        names = publishable_pngs()
        for g in GOLDENS:
            self.assertIn("tests/golden/%s.png" % g, names)
        for icon in PUBLIC_ICONS:
            self.assertIn(icon, names)

    def test_every_publishable_png_passes_the_chunk_rule(self):
        for rel in publishable_pngs():
            with self.subTest(png=rel):
                self.assertEqual(audit.png_problems(rel, (ROOT / rel).read_bytes()), [])

    def test_goldens_carry_no_private_chunks_and_keep_their_pixels(self):
        for g in GOLDENS:
            path = ROOT / "tests" / "golden" / (g + ".png")
            with self.subTest(golden=g):
                data = path.read_bytes()
                kinds = {k for k, _ in audit.png_chunks(data)}
                self.assertIn(b"IDAT", kinds)
                self.assertEqual(kinds & set(snap.PRIVATE_CHUNKS), set())
                self.assertEqual(snap.strip_png_chunks(data), data)     # already stripped: nothing to change

    def test_public_icons_pass(self):
        for rel in PUBLIC_ICONS:
            with self.subTest(icon=rel):
                self.assertEqual(audit.png_problems(rel, (ROOT / rel).read_bytes()), [])

    def test_raycast_exif_is_pinned_to_its_exact_bytes(self):
        """The one allowed eXIf is allowed for the exact file only: the same chunk in a changed file is a hit."""
        rel = "integrations/raycast/assets/icon.png"
        changed = make_png(extra=[metadata_chunks()[1]])
        problems = audit.png_problems(rel, changed)
        self.assertTrue(any("allowlisted file whose contents changed" in p for p in problems), problems)
        self.assertEqual(audit.PNG_EXIF_ALLOWED[rel], hashlib.sha256((ROOT / rel).read_bytes()).hexdigest())


class ChunkRuleTests(unittest.TestCase):
    def test_each_forbidden_chunk_is_a_hit(self):
        for kind, chunk in zip((b"iCCP", b"eXIf", b"tEXt", b"zTXt", b"iTXt"), metadata_chunks()):
            with self.subTest(chunk=kind):
                problems = audit.png_problems("tests/golden/invented.png", make_png(extra=[chunk]))
                self.assertTrue(any(kind.decode() in p for p in problems), problems)

    def test_exif_outside_the_allowlist_is_a_hit(self):
        problems = audit.png_problems("tests/golden/invented.png", make_png(extra=[metadata_chunks()[1]]))
        self.assertEqual(len(problems), 1)
        self.assertIn("eXIf: Exif metadata, strip it", problems[0])

    def test_srgb_and_physical_size_pass(self):
        self.assertEqual(audit.png_problems("tests/golden/invented.png", make_png(extra=kept_chunks())), [])

    def test_damaged_files_fail_closed(self):
        good = make_png(extra=metadata_chunks())
        truncated = good[:-5]
        bad_crc = bytearray(good)
        bad_crc[16] ^= 0x01                                  # first byte of the IHDR body
        trailing = good + b"\x00\x00\x00\x00"
        for name, data in (("truncated", truncated), ("bad CRC", bytes(bad_crc)), ("trailing", trailing),
                           ("not a PNG", b"invented text, not a picture")):
            with self.subTest(case=name):
                problems = audit.png_problems("tests/golden/invented.png", data)
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith("[PNG]"), problems)

    def test_audit_exit_status_follows_the_png_rule(self):
        with tempfile.TemporaryDirectory(prefix="pngprivacy-audit-") as tmp:
            good, bad = os.path.join(tmp, "good"), os.path.join(tmp, "bad")
            os.makedirs(good)
            os.makedirs(bad)
            with open(os.path.join(good, "ok.png"), "wb") as fh:
                fh.write(make_png(extra=kept_chunks()))
            with open(os.path.join(bad, "bad.png"), "wb") as fh:
                fh.write(make_png(extra=[metadata_chunks()[0]]))
            private = os.path.join(tmp, "private")               # no denylist here: only the PNG rule is under test
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(audit.audit(Path(good), Path(private)), 0)
                self.assertEqual(audit.audit(Path(bad), Path(private)), 1)


class StripTests(unittest.TestCase):
    def test_strip_drops_only_the_private_chunks_and_copies_the_rest_byte_for_byte(self):
        data = make_png(extra=metadata_chunks() + kept_chunks())
        stripped = snap.strip_png_chunks(data)
        before, after = raw_chunks(data), raw_chunks(stripped)
        self.assertEqual([k for k, _ in after], [k for k, _ in before if k not in snap.PRIVATE_CHUNKS])
        self.assertEqual([raw for k, raw in before if k not in snap.PRIVATE_CHUNKS], [raw for _, raw in after])
        self.assertEqual(audit.png_problems("tests/golden/invented.png", stripped), [])   # every CRC still valid
        self.assertEqual(idat_stream(stripped), idat_stream(data))
        self.assertEqual(rgba(stripped).shape, rgba(data).shape)
        self.assertTrue(np.array_equal(rgba(stripped), rgba(data)))

    def test_strip_is_a_no_op_on_a_clean_png(self):
        clean = make_png(extra=kept_chunks())
        self.assertEqual(snap.strip_png_chunks(clean), clean)

    def test_strip_refuses_damaged_input(self):
        good = make_png(extra=metadata_chunks())
        bad_crc = bytearray(good)
        bad_crc[16] ^= 0x01
        for name, data in (("bad CRC", bytes(bad_crc)), ("truncated", good[:-5]), ("trailing", good + b"\x00"),
                           ("not a PNG", b"invented text")):
            with self.subTest(case=name), self.assertRaises(ValueError):
                snap.strip_png_chunks(data)

    def test_save_golden_writes_a_stripped_pixel_identical_file(self):
        with tempfile.TemporaryDirectory(prefix="pngprivacy-save-") as tmp:
            render = os.path.join(tmp, "render.png")
            golden = os.path.join(tmp, "golden", "state.png")
            original = make_png(extra=metadata_chunks() + kept_chunks())
            with open(render, "wb") as fh:
                fh.write(original)
            snap.save_golden(render, golden)
            with open(golden, "rb") as fh:
                saved = fh.read()
            kinds = {k for k, _ in audit.png_chunks(saved)}
            self.assertEqual(kinds & set(snap.PRIVATE_CHUNKS), set())
            self.assertTrue(np.array_equal(rgba(saved), rgba(original)))

    def test_save_golden_writes_nothing_when_pixels_would_change(self):
        with tempfile.TemporaryDirectory(prefix="pngprivacy-refuse-") as tmp:
            render = os.path.join(tmp, "render.png")
            golden = os.path.join(tmp, "golden.png")
            with open(render, "wb") as fh:
                fh.write(make_png(extra=metadata_chunks()))
            with mock.patch.object(snap, "same_pixels", return_value=False):
                with self.assertRaises(RuntimeError):
                    snap.save_golden(render, golden)
            self.assertFalse(os.path.exists(golden))

    def test_update_mode_writes_goldens_through_the_same_function(self):
        with tempfile.TemporaryDirectory(prefix="pngprivacy-update-") as tmp:
            render_dir, golden_dir = os.path.join(tmp, "render"), os.path.join(tmp, "golden")
            os.makedirs(render_dir)
            with open(os.path.join(render_dir, "state.png"), "wb") as fh:
                fh.write(make_png(extra=metadata_chunks() + kept_chunks()))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(snap.main([render_dir, golden_dir, "--update", "state"]), 0)
            with open(os.path.join(golden_dir, "state.png"), "rb") as fh:
                kinds = {k for k, _ in audit.png_chunks(fh.read())}
            self.assertEqual(kinds & set(snap.PRIVATE_CHUNKS), set())


if __name__ == "__main__":
    unittest.main()
