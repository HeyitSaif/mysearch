"""The lighter heavy pass: no page image for a PDF with a text layer (media.pdf_page_images), OCR only for pictures that
likely hold text (media.image_ocr, the helper's probe), one poster frame per video on request (media.video_mode), the
idle gate of the heavy pass and the embed loop (indexing.priority, jobs.heavy_work_allowed), the settings' validation,
honest pending counts, and that a file indexed under the old rules keeps its rows untouched on the next scan.

The native helper is mocked: each test asserts which helper commands a file triggers. One test runs the real helper on
generated pictures when MSE_TEST_HELPER names a built msehelper (e.g. scripts/build_helper.sh <scratch>).
Only temporary folders and a temporary data folder are used; no real index, folder or helper is touched.
Run: .venv/bin/python -m unittest -v tests.test_light_pipeline"""

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("MSE_HOME", tempfile.mkdtemp(prefix="mse-test-"))

import numpy as np  # noqa: E402

from mse import config, extract, indexer, jobs, paths, store  # noqa: E402
from mse import progress as liveprog  # noqa: E402

TEXT_PAGE = "A page of ordinary body text, long enough to count as a usable text layer for this test. " * 3


def _coll(root, name=None, **kw):
    return config._merge(config.COLLECTION_DEFAULTS, {"name": name or ("t" + uuid.uuid4().hex[:10]), "path": str(root), **kw})


def _media(**kw) -> dict:
    """The media rules a collection would get: the defaults, with `kw` overriding."""
    return config.effective_media({"media": kw})


class _FakeHelper:
    """Stands in for extract.helper: answers like the native helper would, records every command, writes the files a
    command promises (renders, the embedding copy, the thumbnail). PDFs are told apart by name: text.pdf has a text
    layer on every page, scan.pdf has none, mixed.pdf has one on page 1 only."""

    def __init__(self, text_likely: bool = True, probe_supported: bool = True):
        self.calls: list[list[str]] = []
        self.text_likely = text_likely
        self.probe_supported = probe_supported

    def commands(self) -> list[str]:
        return [c[0] for c in self.calls]

    def __call__(self, *args, timeout=300):
        args = [str(a) for a in args]
        self.calls.append(args)
        cmd, file = args[0], Path(args[1])

        def opt(name):
            return args[args.index(name) + 1] if name in args else None

        if cmd == "pdf":
            if file.name == "scan.pdf":
                pages = [("", 0.0), ("", 0.0)]
            elif file.name == "mixed.pdf":
                pages = [(TEXT_PAGE, None), ("", 0.0)]
            else:
                pages = [(TEXT_PAGE, None)] * 3
            items = [{"page": i + 1, "text": t, **({"coverage": c} if c is not None else {})}
                     for i, (t, c) in enumerate(pages)]
            return {"pages": len(items), "encrypted": False, "items": items, "meta": {}}
        if cmd == "render":
            out = Path(opt("--out"))
            out.mkdir(parents=True, exist_ok=True)
            items = []
            for p in opt("--pages").split(","):
                f = out / f"page-{p}.jpg"
                f.write_bytes(b"jpg")
                items.append({"page": int(p), "file": str(f), "w": 10, "h": 10})
            return {"items": items}
        if cmd == "ocr":
            pages = [int(p) for p in (opt("--pages") or "1").split(",")]
            return {"items": [{"page": p, "text": f"OCR text of page {p}, read by the fake helper", "lines": 1,
                               "confidence": 0.9} for p in pages]}
        if cmd == "image":
            Path(opt("--out")).write_bytes(b"jpg")
            res = {"file": opt("--out"), "w": 10, "h": 10}
            if opt("--thumb"):
                Path(opt("--thumb")).write_bytes(b"jpg")
                res["thumb"] = opt("--thumb")
            return res
        if cmd == "probe":
            if not self.probe_supported:
                raise RuntimeError("unknown command probe")
            return {"text_likely": self.text_likely, "lines": int(self.text_likely), "confidence": 0.5}
        raise RuntimeError(f"unknown command {cmd}")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="light-pipe-"))
        extract._probe_supported = None
        config.update_settings({"media": dict(config.DEFAULTS["media"]), "indexing": {"priority": "balanced"}})

    def tearDown(self):
        extract._probe_supported = None

    def file(self, name: str, data: bytes = b"%PDF-1.4 fictional\n") -> Path:
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def light(self, p: Path, kind: str, media: dict, fake: _FakeHelper | None = None) -> dict:
        fake = fake or _FakeHelper()
        with mock.patch.object(extract, "helper", fake):
            return extract.light(p, kind, p.suffix.lstrip("."), media)

    def heavy(self, p: Path, kind: str, spec: dict, media: dict, fake: _FakeHelper) -> dict:
        with mock.patch.object(extract, "helper", fake):
            return extract.heavy(p, kind, spec, "c" + uuid.uuid4().hex[:8], 1, media)


# ---------------------------------------------------------------- 1. PDFs: page images only for scanned pages

class PdfPageImages(_Base):
    def test_a_text_pdf_gets_no_page_image_and_keeps_its_thumbnail(self):
        p = self.file("text.pdf")
        res = self.light(p, "pdf", _media())
        self.assertEqual(len(res["items"]), 3)                       # the text layer, page by page
        self.assertEqual(res["heavy"], {"thumb": True})               # no visual, no ocr
        fake = _FakeHelper()
        out = self.heavy(p, "pdf", res["heavy"], _media(), fake)
        self.assertEqual(fake.commands(), ["render"])                 # the thumbnail only ...
        self.assertIn("420", fake.calls[0])                           # ... at thumbnail size
        self.assertNotIn("1280", fake.calls[0])
        self.assertEqual(out["items"], [])                            # nothing to embed from the heavy pass
        self.assertTrue(out["thumb"])

    def test_all_is_the_old_behaviour_page_one_rendered_and_embedded(self):
        p = self.file("text.pdf")
        res = self.light(p, "pdf", _media(pdf_page_images="all"))
        self.assertEqual(res["heavy"], {"thumb": True, "visual": [1]})
        fake = _FakeHelper()
        out = self.heavy(p, "pdf", res["heavy"], _media(pdf_page_images="all"), fake)
        self.assertEqual(fake.commands(), ["render", "render"])
        sizes = [c[c.index("--max-px") + 1] for c in fake.calls]
        self.assertEqual(sorted(sizes), ["1280", "420"])              # the page image and the thumbnail
        self.assertEqual([it["modality"] for it in out["items"]], ["page"])

    def test_none_renders_no_page_image_even_for_a_scan(self):
        p = self.file("scan.pdf")
        res = self.light(p, "pdf", _media(pdf_page_images="none"))
        self.assertEqual(res["heavy"], {"thumb": True, "ocr": [1, 2]})

    def test_a_scanned_pdf_keeps_ocr_and_its_page_images(self):
        p = self.file("scan.pdf")
        res = self.light(p, "pdf", _media())
        self.assertEqual(res["items"], [])
        self.assertEqual(res["heavy"], {"thumb": True, "ocr": [1, 2], "visual": [1, 2]})
        fake = _FakeHelper()
        out = self.heavy(p, "pdf", res["heavy"], _media(), fake)
        self.assertEqual(fake.commands(), ["ocr", "render", "render"])
        self.assertEqual(sorted(it["modality"] for it in out["items"]), ["ocr", "ocr", "page", "page"])

    def test_scan_page_images_off_means_ocr_text_only(self):
        p = self.file("scan.pdf")
        media = _media(scan_page_images=False)
        res = self.light(p, "pdf", media)
        self.assertEqual(res["heavy"], {"thumb": True, "ocr": [1, 2]})
        fake = _FakeHelper()
        out = self.heavy(p, "pdf", res["heavy"], media, fake)
        self.assertEqual(fake.commands(), ["ocr", "render"])          # OCR and the thumbnail, no page image
        self.assertEqual([it["modality"] for it in out["items"]], ["ocr", "ocr"])

    def test_a_mixed_pdf_renders_only_its_scanned_page_not_page_one(self):
        p = self.file("mixed.pdf")
        res = self.light(p, "pdf", _media())
        self.assertEqual(res["heavy"], {"thumb": True, "ocr": [2], "visual": [2]})
        self.assertEqual(self.light(p, "pdf", _media(pdf_page_images="all"))["heavy"],
                         {"thumb": True, "ocr": [2], "visual": [1, 2]})

    def test_the_old_pdf_pages_off_policy_still_means_no_page_image(self):
        p = self.file("scan.pdf")
        res = self.light(p, "pdf", _media(pdf_pages="off"))
        self.assertEqual(res["heavy"], {"thumb": True, "ocr": [1, 2]})

    def test_page_images_rule_on_its_own(self):
        self.assertEqual(extract.page_images([1, 3, 5], [3, 5], {}), [3, 5])
        self.assertEqual(extract.page_images([1, 3, 5], [3, 5], {"pdf_page_images": "all"}), [1, 3, 5])
        self.assertEqual(extract.page_images([1, 3, 5], [3, 5], {"pdf_page_images": "all", "scan_page_images": False}),
                         [1])
        self.assertEqual(extract.page_images([1, 3, 5], [3, 5], {"pdf_page_images": "none"}), [])
        self.assertEqual(extract.page_images([1, 3, 5], [3, 5], {"scan_page_images": False}), [])


# ---------------------------------------------------------------- 2. pictures: OCR only when the probe finds text

class ImageOcr(_Base):
    def test_the_spec_carries_the_mode(self):
        p = self.file("pic.png", b"png")
        self.assertEqual(self.light(p, "image", _media())["heavy"], {"image": True, "ocr": "auto", "thumb": True})
        self.assertEqual(self.light(p, "image", _media(image_ocr="always"))["heavy"]["ocr"], True)
        self.assertEqual(self.light(p, "image", _media(image_ocr="never"))["heavy"]["ocr"], False)

    def test_auto_skips_ocr_when_the_probe_finds_no_text(self):
        p = self.file("pic.png", b"png")
        fake = _FakeHelper(text_likely=False)
        out = self.heavy(p, "image", {"image": True, "ocr": "auto", "thumb": True}, _media(), fake)
        self.assertEqual(fake.commands(), ["image", "probe"])
        self.assertIn("768", fake.calls[1])                           # the probe's downscale (extract.PROBE_MAX_PX)
        self.assertEqual([it["modality"] for it in out["items"]], ["image"])   # the embedding stays
        self.assertTrue(out["thumb"])

    def test_auto_runs_ocr_when_the_probe_finds_text(self):
        p = self.file("pic.png", b"png")
        fake = _FakeHelper(text_likely=True)
        out = self.heavy(p, "image", {"image": True, "ocr": "auto", "thumb": True}, _media(), fake)
        self.assertEqual(fake.commands(), ["image", "probe", "ocr"])
        self.assertEqual([it["modality"] for it in out["items"]], ["image", "ocr"])

    def test_always_and_never_do_not_probe(self):
        p = self.file("pic.png", b"png")
        fake = _FakeHelper(text_likely=False)
        self.heavy(p, "image", {"image": True, "ocr": True, "thumb": True}, _media(), fake)
        self.assertEqual(fake.commands(), ["image", "ocr"])          # the old behaviour, whatever the probe would say
        fake = _FakeHelper(text_likely=True)
        out = self.heavy(p, "image", {"image": True, "ocr": False, "thumb": True}, _media(), fake)
        self.assertEqual(fake.commands(), ["image"])
        self.assertEqual([it["modality"] for it in out["items"]], ["image"])

    def test_an_older_helper_without_probe_means_ocr_as_before_and_is_asked_once(self):
        p = self.file("pic.png", b"png")
        fake = _FakeHelper(probe_supported=False)
        self.heavy(p, "image", {"image": True, "ocr": "auto", "thumb": True}, _media(), fake)
        self.assertEqual(fake.commands(), ["image", "probe", "ocr"])
        self.heavy(p, "image", {"image": True, "ocr": "auto", "thumb": True}, _media(), fake)
        self.assertEqual(fake.commands()[3:], ["image", "ocr"])      # the second picture: no probe call, OCR as before
        self.assertIs(extract._probe_supported, False)

    def test_an_old_spec_with_ocr_true_is_still_ocred_in_full(self):
        """A picture light-indexed before this change carries ocr: true; it keeps the old behaviour."""
        p = self.file("pic.png", b"png")
        fake = _FakeHelper(text_likely=False)
        self.heavy(p, "image", json.loads('{"image": true, "ocr": true, "thumb": true}'), _media(), fake)
        self.assertEqual(fake.commands(), ["image", "ocr"])

    def test_reprocess_marks_ocr_a_picture_again_in_full(self):
        self.assertEqual(indexer._reprocess_spec(None, {"kind": "image", "id": 1}, _media()),
                         {"image": True, "ocr": True, "thumb": True})


# ---------------------------------------------------------------- 3. video: segments (default) or one poster frame

class _FakeFfmpeg:
    """Stands in for extract.run: records every ffmpeg command and writes the output file it names."""

    def __init__(self):
        self.cmds: list[list[str]] = []

    def __call__(self, cmd, timeout=120):
        self.cmds.append([str(c) for c in cmd])
        out = Path(cmd[-1])
        if cmd[0] == "ffmpeg" and out.suffix in (".mp4", ".wav", ".jpg"):
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x" * 4000)
        return mock.Mock(returncode=0, stdout=b"", stderr=b"")

    def segments(self):
        return [c for c in self.cmds if "fps=1,scale=448:-2" in c]

    def posters(self):
        return [c for c in self.cmds if "-frames:v" in c and c[-1].endswith("poster.jpg")]

    def wavs(self):
        return [c for c in self.cmds if c[-1].endswith(".wav")]


class VideoMode(_Base):
    def _run(self, spec, media):
        p = self.file("clip.mp4", b"mp4")
        ff = _FakeFfmpeg()
        with mock.patch.object(extract, "run", ff), \
                mock.patch.object(extract, "_ffprobe_duration", return_value=(120.0, True, True)), \
                mock.patch.object(extract.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(extract, "transcript_items", return_value=[{"modality": "transcript", "t0": 0.0,
                                                                              "t1": 10.0, "text": "spoken words"}]):
            out = extract.heavy(p, "video", spec, "c" + uuid.uuid4().hex[:8], 1, media)
        return ff, out

    def test_the_spec_carries_the_mode(self):
        p = self.file("clip.mp4", b"mp4")
        self.assertEqual(self.light(p, "video", _media())["heavy"], {"video": True, "thumb": True})
        self.assertEqual(self.light(p, "video", _media(video_mode="poster"))["heavy"],
                         {"video": True, "thumb": True, "poster": True})
        self.assertEqual(indexer._reprocess_spec(None, {"kind": "video", "id": 1}, _media(video_mode="poster")),
                         {"video": True, "thumb": True, "poster": True})
        self.assertEqual(indexer._reprocess_spec(None, {"kind": "video", "id": 1}, _media()),
                         {"video": True, "thumb": True})

    def test_segments_is_the_old_behaviour(self):
        ff, out = self._run({"video": True, "thumb": True}, _media())
        self.assertEqual(len(ff.segments()), 4)                       # 120 s in 30 s clips
        self.assertEqual(ff.posters(), [])
        self.assertEqual(len(ff.wavs()), 2)                           # the audio, 60 s each, unchanged
        mods = [it["modality"] for it in out["items"]]
        self.assertEqual(mods.count("video"), 4)
        self.assertEqual(mods.count("audio"), 2)
        self.assertEqual(mods.count("transcript"), 1)
        self.assertTrue(out["thumb"])

    def test_poster_is_one_frame_at_ten_percent_plus_the_transcript(self):
        ff, out = self._run({"video": True, "thumb": True, "poster": True}, _media(video_mode="poster"))
        self.assertEqual(ff.segments(), [])
        self.assertEqual(len(ff.posters()), 1)
        poster = ff.posters()[0]
        self.assertEqual(poster[poster.index("-ss") + 1], "12.0")      # 10 % of 120 s
        self.assertEqual(len(ff.wavs()), 2)                           # audio unchanged
        mods = [it["modality"] for it in out["items"]]
        self.assertEqual(mods.count("video"), 0)
        self.assertEqual(mods.count("image"), 1)
        self.assertEqual(mods.count("audio"), 2)
        self.assertEqual(mods.count("transcript"), 1)
        frame = next(it for it in out["items"] if it["modality"] == "image")
        self.assertEqual((frame["t0"], frame["t1"]), (12.0, 12.0))
        self.assertTrue(Path(frame["media"]).exists())
        self.assertTrue(out["thumb"])


# ---------------------------------------------------------------- 4. the idle gate

class _Gate:
    """A stand-in for jobs.heavy_work_allowed: answers from a script, then (True, None) for ever."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple] = []

    def __call__(self, priority, enabled=True):
        self.calls.append((priority, enabled))
        return self.answers.pop(0) if self.answers else (True, None)


def _patched_gate(gate):
    return mock.patch.object(jobs, "heavy_work_allowed", gate, create=True)


class IdleGate(_Base):
    def setUp(self):
        super().setUp()
        self._step = mock.patch.object(indexer, "PAUSE_STEP_S", 0.02)
        self._step.start()

    def tearDown(self):
        self._step.stop()
        super().tearDown()

    def test_a_pause_is_reported_then_cleared_and_the_wait_is_returned(self):
        name = "g" + uuid.uuid4().hex[:8]
        gate = _Gate((False, "user_active"), (False, "user_active"), (False, "on_battery"), (True, None))
        seen = []

        def progress(stage, done, total, msg):
            seen.append((stage, done, total, msg, liveprog.REGISTRY._folders[name].paused))

        liveprog.begin(name, "scan")
        with _patched_gate(gate):
            waited = indexer._wait_for_heavy_work(name, "balanced", None, progress, "ocr & pages", 2, 5)
        self.assertGreater(waited, 0.0)
        self.assertEqual([c[0] for c in gate.calls], ["balanced"] * 4)
        self.assertEqual(seen, [("ocr & pages", 2, 5, "paused (user active)", "user active"),
                                ("ocr & pages", 2, 5, "paused (on battery)", "on battery"),
                                ("ocr & pages", 2, 5, "", None)])
        self.assertIsNone(liveprog.REGISTRY._folders[name].paused)
        liveprog.end(name, "done")

    def test_mse_status_shows_the_pause(self):
        name = "g" + uuid.uuid4().hex[:8]
        liveprog.begin(name, "sync")
        liveprog.stage(name, "ocr & pages", 1, 4, "a.pdf")
        liveprog.pause(name, "user active")
        snap = liveprog.snapshot([name])["folders"][name]
        self.assertEqual(snap["paused"], "user active")
        line = liveprog.format_line(name, snap)
        self.assertIn("OCR and pictures 1/4", line)
        self.assertIn("paused (user active)", line)
        liveprog.pause(name, None)
        self.assertIsNone(liveprog.snapshot([name])["folders"][name]["paused"])
        liveprog.end(name, "done")
        self.assertIsNone(liveprog.snapshot([name])["folders"][name]["paused"])

    def test_a_cancel_during_the_pause_raises_cancelled(self):
        name = "g" + uuid.uuid4().hex[:8]
        gate = _Gate(*([(False, "user_active")] * 50))
        hits = {"n": 0}

        def cancel():
            hits["n"] += 1
            return hits["n"] >= 3

        with _patched_gate(gate), self.assertRaises(indexer.Cancelled):
            indexer._wait_for_heavy_work(name, "low", cancel, None, "embedding text", 0, 1)
        self.assertLessEqual(len(gate.calls), 4)                       # it did not sleep the script out
        self.assertIsNone(liveprog.REGISTRY._folders[name].paused)

    def test_always_never_asks_and_a_tree_without_the_function_is_never_paused(self):
        gate = _Gate((False, "user_active"))
        with _patched_gate(gate):
            self.assertEqual(indexer._wait_for_heavy_work("x", "always", None, None, "ocr & pages", 0, 1), 0.0)
        self.assertEqual(gate.calls, [])
        self.assertEqual(indexer.heavy_work_allowed("always"), (True, None))
        with mock.patch.object(jobs, "heavy_work_allowed", None, create=True):
            self.assertEqual(indexer.heavy_work_allowed("low"), (True, None))
            self.assertEqual(indexer._wait_for_heavy_work("x", "low", None, None, "ocr & pages", 0, 1), 0.0)

    def test_the_priority_comes_from_the_setting_unless_the_caller_passes_one(self):
        self.assertEqual(indexer._priority(None), "balanced")
        config.update_settings({"indexing": {"priority": "low"}})
        self.assertEqual(indexer._priority(None), "low")
        self.assertEqual(indexer._priority("always"), "always")
        self.assertEqual(indexer._priority("bogus"), "low")           # an unknown value falls back to the setting

    def _collection_with_a_pending_picture(self):
        root = self.tmp / "v"
        root.mkdir()
        (root / "pic.png").write_bytes(b"png")
        name = "t" + uuid.uuid4().hex[:10]
        coll = _coll(root, name=name)
        with mock.patch.object(extract, "helper", _FakeHelper()):
            indexer.scan(coll)
        return coll

    def test_the_heavy_pass_asks_the_gate_before_each_file(self):
        coll = self._collection_with_a_pending_picture()
        gate = _Gate((False, "user_active"), (True, None))
        fake = _FakeHelper(text_likely=False)
        with _patched_gate(gate), mock.patch.object(extract, "helper", fake):
            res = indexer.heavy(coll)                                  # priority None: the setting, balanced
        self.assertEqual(res["done"], 1)
        self.assertGreater(res["paused_s"], 0.0)
        self.assertEqual([c[0] for c in gate.calls], ["balanced", "balanced"])
        self.assertEqual(fake.commands(), ["image", "probe"])

    def test_sync_now_passes_always_and_is_never_gated(self):
        coll = self._collection_with_a_pending_picture()
        gate = _Gate((False, "user_active"))
        with _patched_gate(gate), mock.patch.object(extract, "helper", _FakeHelper(text_likely=False)):
            res = indexer.heavy(coll, priority="always")
        self.assertEqual((res["done"], res["paused_s"]), (1, 0.0))
        self.assertEqual(gate.calls, [])

    def test_the_embed_loop_asks_the_gate_before_each_model_batch(self):
        coll = self._collection_with_a_pending_picture()
        gate = _Gate((False, "on_battery"), (True, None))

        class Pool:
            class W:
                batch_priority = False
            workers = {"text": W(), "full": W()}

            def embed_texts(self, texts, batch=None):
                v = np.zeros((len(texts), 768), dtype=np.float32)
                v[:, 0] = 1.0
                return v

            def embed_media(self, items):
                return np.zeros((0, 768), dtype=np.float32), []

        with _patched_gate(gate), mock.patch.object(indexer, "pool", lambda: Pool()):
            res = indexer.embed(coll, media=False)
        self.assertEqual(res["embedded"], 1)                            # the title item of the picture
        self.assertGreater(res["paused_s"], 0.0)
        self.assertEqual([c[0] for c in gate.calls], ["balanced", "balanced"])
        gate = _Gate((False, "on_battery"))
        with _patched_gate(gate), mock.patch.object(indexer, "pool", lambda: Pool()):
            indexer.embed(coll, media=False, priority="always")
        self.assertEqual(gate.calls, [])


# ---------------------------------------------------------------- 5. settings and their validation

class SettingsValidation(_Base):
    def test_the_defaults(self):
        self.assertEqual(config.DEFAULTS["media"], {"pdf_page_images": "scanned_only", "scan_page_images": True,
                                                    "image_ocr": "auto", "video_mode": "segments"})
        self.assertEqual(config.DEFAULTS["indexing"]["priority"], "balanced")
        for k in config.MEDIA_CHOICES:
            self.assertIsNone(config.COLLECTION_DEFAULTS["media"][k])  # null: inherit
        m = config.effective_media(_coll(self.tmp))
        self.assertEqual({k: m[k] for k in config.MEDIA_CHOICES}, config.DEFAULTS["media"])
        self.assertEqual(m["pdf_pages"], "smart")                     # the collection's own keys are untouched

    def test_indexing_priority(self):
        for v in ("low", "balanced", "always"):
            self.assertEqual(config.check_indexing({"priority": v}), {"priority": v})
        for v in ("fast", True, 1, None):
            with self.assertRaises(ValueError):
                config.check_indexing({"priority": v})
        config.update_settings({"indexing": {"priority": "low"}})
        self.assertEqual(config.load()["indexing"]["priority"], "low")

    def test_global_media_keys(self):
        cfg = config.update_settings({"media": {"pdf_page_images": "all", "scan_page_images": False,
                                                "image_ocr": "never", "video_mode": "poster"}})
        self.assertEqual(cfg["media"], {"pdf_page_images": "all", "scan_page_images": False, "image_ocr": "never",
                                        "video_mode": "poster"})
        for bad in ({"pdf_page_images": "sometimes"}, {"scan_page_images": "yes"}, {"image_ocr": True},
                    {"video_mode": "clips"}, {"pdf_page_images": None}, {"images": True}, {"nonsense": 1}):
            with self.assertRaises(ValueError):
                config.update_settings({"media": bad})
        with self.assertRaises(ValueError):
            config.update_settings({"media": "all"})

    def test_per_collection_override_and_null_inherits(self):
        root = self.tmp / "w"
        root.mkdir()
        name = "t" + uuid.uuid4().hex[:10]
        config.add_collection(str(root), name)
        config.update_settings({"media": {"image_ocr": "never"}})
        coll = config.get_collection(config.load(), name)
        self.assertEqual(config.effective_media(coll)["image_ocr"], "never")          # inherited
        config.update_collection(name, {"media": {"image_ocr": "always", "pdf_page_images": "all"}})
        coll = config.get_collection(config.load(), name)
        self.assertEqual(coll["media"]["image_ocr"], "always")
        self.assertEqual(config.effective_media(coll)["image_ocr"], "always")         # the override wins
        self.assertEqual(config.effective_media(coll)["pdf_page_images"], "all")
        self.assertEqual(config.effective_media(coll)["video_mode"], "segments")      # still inherited
        config.update_collection(name, {"media": {"image_ocr": None}})
        coll = config.get_collection(config.load(), name)
        self.assertIsNone(coll["media"]["image_ocr"])
        self.assertEqual(config.effective_media(coll)["image_ocr"], "never")          # back to the global value
        config.update_collection(name, {"media": {"images": False, "max_pdf_pages": 12}})   # the old keys, as before
        coll = config.get_collection(config.load(), name)
        self.assertEqual((coll["media"]["images"], coll["media"]["max_pdf_pages"]), (False, 12))
        for bad in ({"video_mode": "clips"}, {"scan_page_images": "yes"}, {"image_ocr": 1}, {"pdf_page_images": ""}):
            with self.assertRaises(ValueError):
                config.update_collection(name, {"media": bad})
        with self.assertRaises(ValueError):
            config.update_collection(name, {"media": []})
        self.assertEqual(config.get_collection(config.load(), name)["media"]["image_ocr"], None)   # nothing changed

    def test_effective_media_ignores_a_bad_global_value(self):
        self.assertEqual(config.effective_media({"media": {}}, {"media": {"image_ocr": "bogus"}})["image_ocr"],
                         "auto")
        self.assertEqual(config.effective_media({"media": {}}, {"media": {"image_ocr": "never"}})["image_ocr"],
                         "never")


# ---------------------------------------------------------------- 6. counts, and no re-indexing of what is already there

def _db(name):
    conn = store.connect(name, readonly=True)
    try:
        files = {r["path"]: dict(r) for r in conn.execute(
            "SELECT id, path, indexed_at, heavy, heavy_at, state FROM files")}
        items = [tuple(r) for r in conn.execute(
            "SELECT id, file_id, modality, page, embedded FROM items ORDER BY id")]
        st = store.stats(conn)
    finally:
        conn.close()
    return files, items, st


class CountsAndNoReindex(_Base):
    def _folder(self):
        root = self.tmp / "v"
        root.mkdir()
        (root / "text.pdf").write_bytes(b"%PDF-1.4 fictional text document\n")
        (root / "pic.png").write_bytes(b"png")
        return root

    def test_pending_counts_are_what_will_run(self):
        root = self._folder()
        name = "t" + uuid.uuid4().hex[:10]
        coll = _coll(root, name=name)
        fake = _FakeHelper(text_likely=False)
        with mock.patch.object(extract, "helper", fake):
            indexer.scan(coll)
            files, items, st = _db(name)
            self.assertEqual(json.loads(files["text.pdf"]["heavy"]), {"thumb": True})
            self.assertEqual(json.loads(files["pic.png"]["heavy"]), {"image": True, "ocr": "auto", "thumb": True})
            self.assertEqual(st["heavy_pending"], 2)                   # the thumbnail, and the picture
            self.assertEqual(indexer.pending_count(name, media=True), 0)
            res = indexer.heavy(coll, priority="always")
        self.assertEqual((res["files"], res["done"]), (2, 2))
        files, items, st = _db(name)
        self.assertEqual(st["heavy_pending"], 0)
        mods = sorted(m for _i, _f, m, _p, _e in items)
        self.assertEqual(mods, ["image", "text", "text", "text", "title", "title"])   # no page item, no OCR item
        self.assertEqual(indexer.pending_count(name, media=True), 1)                     # the picture's embedding
        self.assertEqual(st["embed_pending"], 6)
        self.assertEqual(fake.commands().count("ocr"), 0)
        self.assertEqual([c for c in fake.calls if c[0] == "render" and "1280" in c], [])

    def test_a_file_indexed_under_the_old_rules_is_not_touched_by_the_new_ones(self):
        root = self._folder()
        name = "t" + uuid.uuid4().hex[:10]
        old = _coll(root, name=name, media={"pdf_page_images": "all", "image_ocr": "always"})
        fake = _FakeHelper(text_likely=False)
        with mock.patch.object(extract, "helper", fake):
            indexer.scan(old)
            indexer.heavy(old, priority="always")
        before_files, before_items, _st = _db(name)
        self.assertEqual(sorted(m for _i, _f, m, _p, _e in before_items),
                         ["image", "ocr", "page", "text", "text", "text", "title", "title"])  # the old, fuller rows
        self.assertTrue(all(f["heavy"] is None for f in before_files.values()))

        new = _coll(root, name=name)                                   # the new defaults: scanned_only, auto
        fake2 = _FakeHelper(text_likely=False)
        with mock.patch.object(extract, "helper", fake2):
            stats = indexer.scan(new)
            res = indexer.heavy(new, priority="always")
        self.assertEqual((stats["new_or_changed"], stats["forced"], stats["xver"]), (0, 0, 0))
        self.assertEqual(res["files"], 0)                              # nothing pending: nothing re-processed
        self.assertEqual(fake2.calls, [])                              # the helper was never called
        after_files, after_items, _st = _db(name)
        self.assertEqual(after_files, before_files)                    # same ids, same indexed_at, same heavy_at
        self.assertEqual(after_items, before_items)                    # the page image and the OCR text stay

        (root / "text.pdf").write_bytes(b"%PDF-1.4 fictional text document, edited\n")   # a changed file ...
        os.utime(root / "text.pdf", (1_700_000_000, 1_700_000_000))
        with mock.patch.object(extract, "helper", fake2):
            stats = indexer.scan(new)
            indexer.heavy(new, priority="always")
        self.assertEqual(stats["new_or_changed"], 1)                   # ... is read again, under the new rules
        files, items, _st = _db(name)
        self.assertEqual(files["pic.png"], after_files["pic.png"])      # the picture: still untouched
        pdf_mods = sorted(m for _i, f, m, _p, _e in items if f == files["text.pdf"]["id"])
        self.assertEqual(pdf_mods, ["text", "text", "text", "title"])  # no page image any more
        self.assertEqual([c for c in fake2.calls if c[0] == "render" and "1280" in c], [])


# ---------------------------------------------------------------- 7. the real helper, when one is given

@unittest.skipUnless(os.environ.get("MSE_TEST_HELPER"), "set MSE_TEST_HELPER to a built msehelper to run")
class RealHelperProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="light-pipe-real-"))
        extract._probe_supported = None

    def tearDown(self):
        extract._probe_supported = None

    def _pictures(self):
        from PIL import Image, ImageDraw, ImageFont
        text = Image.new("RGB", (900, 500), "white")
        d = ImageDraw.Draw(text)
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 44)
        for i, line in enumerate(("INVOICE 2026-0042", "Fictional Widgets GmbH", "Total 1.234,56 EUR")):
            d.text((40, 60 + i * 110), line, fill="black", font=font)
        text.save(self.tmp / "text.png")
        Image.new("RGB", (800, 600), (200, 30, 30)).save(self.tmp / "solid.png")
        caption = Image.new("RGB", (1600, 1200), (230, 230, 240))
        ImageDraw.Draw(caption).text((60, 1120), "Figure 3: fictional caption text", fill="black",
                                     font=ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 26))
        caption.save(self.tmp / "caption.png")

    def test_the_probe_tells_text_from_no_text_and_gates_the_full_ocr(self):
        helper = Path(os.environ["MSE_TEST_HELPER"])
        self.assertTrue(helper.exists(), helper)
        self._pictures()
        with mock.patch.object(paths, "HELPER", helper):
            self.assertTrue(extract._image_has_text(self.tmp / "text.png"))
            self.assertTrue(extract._image_has_text(self.tmp / "caption.png"))
            self.assertFalse(extract._image_has_text(self.tmp / "solid.png"))
            self.assertIs(extract._probe_supported, True)
            work = self.tmp / "work"
            work.mkdir()
            items, thumb = extract._image_heavy(self.tmp / "solid.png", work, self.tmp / "t1.jpg", "auto", {})
            self.assertEqual([it["modality"] for it in items], ["image"])
            self.assertTrue(thumb)
            items, thumb = extract._image_heavy(self.tmp / "text.png", work, self.tmp / "t2.jpg", "auto", {})
            self.assertEqual([it["modality"] for it in items], ["image", "ocr"])
            self.assertIn("Fictional Widgets", items[1]["text"])
            # every existing command answers as before
            self.assertIn("crawl", extract.helper("features", "-"))
            with self.assertRaises(RuntimeError):
                extract.helper("bogus", "x")


if __name__ == "__main__":
    unittest.main()
