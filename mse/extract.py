"""Turn files into searchable items.

light()            cheap, runs during the day: text formats, Office and OpenDocument, e-mail (single messages and
                   mbox), e-books, notebooks, archive member lists, iWork previews, the PDF text layer.
heavy()            expensive, runs at night or on "Sync now": OCR, PDF page images, picture OCR, audio/video segments.
transcript_items() speech to text for audio and video; long silences are cut out before Whisper runs.

EXTRACTOR_VERSION names the extensions whose extraction changed or is newly supported. The indexer re-reads only
those files, lazily. Anything absent is version 1. PDF and image files are not listed: the OCR changes are opt-in,
because a bump would re-OCR the whole library. Audio and video are not listed for the same reason: a bump would
re-transcribe the library without being asked.
"""

import email
import email.policy
import html
import html.parser
import json
import mailbox
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
import wave
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from . import paths
from .textutil import detect_lang

TEXT_EXT = {
    "md", "markdown", "txt", "text", "rst", "org", "csv", "tsv", "json", "jsonl", "ndjson", "yaml", "yml", "toml",
    "ini", "cfg", "conf", "xml", "html", "htm", "xhtml", "py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "sh", "zsh",
    "bash", "sql", "swift", "go", "rs", "java", "kt", "c", "h", "cpp", "hpp", "cs", "rb", "php", "r", "tex", "bib",
    "vtt", "srt", "log", "ics", "vcf", "css", "scss", "mdx", "ipynb",
}
TEXTUTIL_EXT = {"doc", "docx", "docm", "dotx", "rtf", "rtfd", "odt", "wordml", "webarchive"}
SHEET_EXT = {"xlsx", "xlsm"}
SLIDE_EXT = {"pptx", "pptm"}
OPENDOC_EXT = {"ods", "odp", "odg"}             # odt stays with textutil (TEXTUTIL_EXT)
IWORK_EXT = {"pages", "numbers", "key"}         # Apple iWork: a zip file or a package folder
EPUB_EXT = {"epub"}
MBOX_EXT = {"mbox"}
ARCHIVE_EXT = {"zip", "tar", "tgz", "tbz2", "txz", "7z", "rar"}
TAR_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz")   # the extension is gz/bz2/xz here: recognised by name
IMAGE_EXT = {"jpg", "jpeg", "png", "heic", "heif", "webp", "gif", "tif", "tiff", "bmp"}
AUDIO_EXT = {"mp3", "m4a", "wav", "aac", "flac", "ogg", "oga", "aif", "aiff", "caf", "opus"}
VIDEO_EXT = {"mp4", "mov", "m4v", "webm", "mkv", "avi"}

# extension -> extractor version, for every extension whose extraction changed or is newly supported.
EXTRACTOR_VERSION: dict[str, int] = {ext: 2 for ext in (
    "epub", "ods", "odp", "odg", "pages", "numbers", "key", "mbox", "eml", "ipynb",
    "zip", "tar", "tgz", "tbz2", "txz", "7z", "rar", "gz", "bz2", "xz",
)}

MAX_TEXT = 3_000_000          # characters kept per file
SCANNED_PAGE_CHARS = 40       # a PDF page with fewer characters than this has no usable text layer
SCANNED_PAGE_COVERAGE = 0.05  # ... and a page whose text lines cover less than this share of the page is mostly image
LOW_TEXT_CHARS = 600          # the helper measures coverage only for pages with fewer characters than this
MBOX_MAX_MESSAGES = 2000
ARCHIVE_MAX_MEMBERS = 5000
NOTEBOOK_OUTPUT_CHARS = 4000  # per cell output
NS_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
NS_DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
NS_PRESENTATION = "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"
NS_DC = "http://purl.org/dc/elements/1.1/"

# Transcripts (see transcript_items)
TRANSCRIPT_SILENCE_DB = -40        # below this level a stretch counts as silent ...
TRANSCRIPT_SILENCE_MIN_S = 8.0     # ... and only silences this long are cut out
TRANSCRIPT_KEEP_PAD_S = 0.4        # silence kept on each side of a cut, so no word is clipped
PASSAGE_SOFT_S = 30.0              # a passage ends at a sentence end once it is this long ...
PASSAGE_HARD_S = 60.0              # ... and never runs past this
PASSAGE_MAX_CHARS = 1100


def kind_of(ext: str) -> str:
    ext = ext.lower()
    if ext == "pdf":
        return "pdf"
    if ext in TEXT_EXT or ext in EPUB_EXT:
        return "text"
    if ext in TEXTUTIL_EXT or ext in SHEET_EXT or ext in SLIDE_EXT or ext in OPENDOC_EXT or ext in IWORK_EXT:
        return "office"
    if ext == "eml" or ext in MBOX_EXT:
        return "email"
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    return "other"


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def helper(*args: str, timeout: int = 300) -> dict:
    if not paths.HELPER.exists():
        raise RuntimeError("native helper missing — run scripts/build_helper.sh")
    p = run([str(paths.HELPER), *args], timeout=timeout)
    try:
        out = json.loads(p.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except Exception:
        raise RuntimeError(f"helper failed: {p.stderr.decode('utf-8', 'replace')[:300]}")
    if "error" in out:
        raise RuntimeError(out["error"])
    return out


def _private_tmp(prefix: str) -> Path:
    """A new empty folder under the data directory's tmp, for one file's temporary copies."""
    paths.TMP.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(paths.TMP)))


# ------------------------------------------------------------------ chunking

def chunk_text(text: str, target: int = 1100, hard: int = 1600, overlap: int = 150) -> list[str]:
    text = text.strip()
    if len(text) <= hard:
        return [text] if text else []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for p in paras:
        if len(p) <= hard:
            pieces.append(p)
            continue
        sents = re.split(r"(?<=[.!?:;])\s+|\n", p)
        buf = ""
        for s in sents:
            while len(s) > hard:
                pieces.append(s[:hard])
                s = s[hard - overlap:]
            if len(buf) + len(s) + 1 > target and buf:
                pieces.append(buf)
                buf = s          # overlap is added once, between chunks, below
            else:
                buf = (buf + " " + s).strip()
        if buf:
            pieces.append(buf)
    chunks, buf = [], ""
    for p in pieces:
        if len(buf) + len(p) + 2 > target and buf:
            chunks.append(buf)
            tail = buf[-overlap:]
            buf = (tail[tail.find(" ") + 1:] + "\n\n" + p) if overlap and len(p) < target else p
        else:
            buf = (buf + "\n\n" + p).strip()
    if buf:
        chunks.append(buf)
    return chunks


def markdown_sections(text: str) -> list[dict]:
    """Split markdown by headings; each section keeps its heading trail."""
    out, trail, buf = [], [], []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            out.append({"heading": " › ".join(trail[-3:]) or None, "text": body})
        buf.clear()

    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush()
            level = len(m.group(1))
            trail[:] = trail[:level - 1] + [m.group(2).strip().strip("#").strip()]
        else:
            buf.append(line)
    flush()
    return out


def sections_to_items(sections: list[dict]) -> list[dict]:
    items = []
    for s in sections:
        for c in chunk_text(s["text"]):
            items.append({"modality": s.get("modality", "text"), "text": c, "heading": s.get("heading"),
                          "page": s.get("page")})
    return items


# ------------------------------------------------------------------ readers

def decode_bytes(raw: bytes, limit: int = MAX_TEXT) -> str:
    for enc in ("utf-8", "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else None, "cp1252", "latin-1"):
        if not enc:
            continue
        try:
            return raw.decode(enc)[:limit]
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")[:limit]


def read_text_file(p: Path, limit: int = MAX_TEXT) -> str:
    return decode_bytes(p.read_bytes()[: limit * 2], limit)


class _HTMLText(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(s: str) -> str:
    p = _HTMLText()
    try:
        p.feed(s)
    except Exception:
        return re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", html.unescape("".join(p.parts)))


def _xml_texts(data: bytes, tag_suffix: str) -> list[str]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    return [e.text for e in root.iter() if e.tag.endswith(tag_suffix) and e.text]


def read_xlsx(p: Path) -> list[dict]:
    sections = []
    with zipfile.ZipFile(p) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root:
                shared.append("".join(t.text or "" for t in si.iter() if t.tag.endswith("}t")))
        sheet_names = {}
        if "xl/workbook.xml" in names:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            for i, s in enumerate([e for e in wb.iter() if e.tag.endswith("}sheet")], 1):
                sheet_names[i] = s.get("name")
        sheets = sorted((n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        budget = 200_000
        for n in sheets:
            idx = int(re.findall(r"\d+", n)[-1])
            rows = []
            root = ET.fromstring(z.read(n))
            for row in root.iter():
                if not row.tag.endswith("}row"):
                    continue
                cells = []
                for c in row:
                    if not c.tag.endswith("}c"):
                        continue
                    t = c.get("t")
                    v = next((x.text for x in c if x.tag.endswith("}v")), None)
                    if t == "s" and v is not None and v.isdigit() and int(v) < len(shared):
                        v = shared[int(v)]
                    elif t == "inlineStr":
                        v = "".join(x.text or "" for x in c.iter() if x.tag.endswith("}t"))
                    if v not in (None, ""):
                        cells.append(str(v))
                if cells:
                    rows.append(" | ".join(cells))
                    budget -= len(cells)
                if budget <= 0:
                    break
            if rows:
                sections.append({"heading": f"Sheet: {sheet_names.get(idx, idx)}", "text": "\n".join(rows)})
            if budget <= 0:
                break
    return sections


def read_pptx(p: Path) -> list[dict]:
    sections = []
    with zipfile.ZipFile(p) as z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        for n in slides:
            no = int(re.findall(r"\d+", n)[-1])
            text = "\n".join(_xml_texts(z.read(n), "}t"))
            if text.strip():
                sections.append({"heading": f"Slide {no}", "text": text, "page": no})
    return sections


def read_docx_zip(p: Path) -> str:
    with zipfile.ZipFile(p) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras = []
    for para in root.iter():
        if para.tag.endswith("}p"):
            t = "".join(x.text or "" for x in para.iter() if x.tag.endswith("}t"))
            if t.strip():
                paras.append(t)
    return "\n\n".join(paras)


def read_textutil(p: Path) -> str:
    r = run(["/usr/bin/textutil", "-convert", "txt", "-stdout", "--", str(p)], timeout=90)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[:200] or "textutil failed")
    return r.stdout.decode("utf-8", "replace")[:MAX_TEXT]


# ------------------------------------------------------------------ e-mail

def _part_text(part) -> str:
    try:
        content = part.get_content()
    except Exception:
        return ""
    if part.get_content_type() == "text/html":
        return html_to_text(str(content))
    return str(content)


def _message_body(msg) -> str:
    """The message's own body: its preferred text part (plain, else html converted). Only if that is empty, any
    other text part that is not an attachment. A forwarded message's text is never taken as this message's body."""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    text = _part_text(part) if part is not None else ""
    if text.strip():
        return text
    for part in msg.walk():
        if part is msg or part.get_content_maintype() != "text" or part.get_filename():
            continue
        text = _part_text(part)
        if text.strip():
            return text
    return ""


def message_text(msg) -> tuple[str, str]:
    """(subject, searchable text) of one e-mail: the headers, the names of every attachment (also those inside a
    forwarded message) and the body, cut to MAX_TEXT."""
    head = []
    for h in ("Subject", "From", "To", "Cc", "Date"):
        if msg.get(h):
            head.append(f"{h}: {msg.get(h)}")
    names: list[str] = []
    for part in msg.walk():
        fn = part.get_filename()
        if not fn or fn in names:
            continue
        if part.get_content_maintype() == "image" and part.get_content_disposition() == "inline":
            continue                                # signature logos and other inline pictures are not attachments
        names.append(fn)
    if names:
        head.append("Attachments: " + ", ".join(names[:200]))
    body = _message_body(msg)
    return str(msg.get("Subject") or ""), "\n".join(head) + "\n\n" + body[:MAX_TEXT]


def read_eml(p: Path) -> tuple[str, list[dict]]:
    msg = email.message_from_bytes(p.read_bytes(), policy=email.policy.default)
    subject, text = message_text(msg)
    return subject, [{"heading": None, "text": text}]


def read_mbox(p: Path) -> list[dict]:
    """One section per message (heading = subject), in file order. Stops at MBOX_MAX_MESSAGES or MAX_TEXT.
    mailbox opens a read-only file read-only, so a mailbox on a read-only volume still works."""
    sections, total = [], 0
    box = mailbox.mbox(str(p), create=False)
    try:
        for n, key in enumerate(box.keys()):
            if n >= MBOX_MAX_MESSAGES or total >= MAX_TEXT:
                break
            msg = email.message_from_bytes(box.get_bytes(key), policy=email.policy.default)
            subject, text = message_text(msg)
            sections.append({"heading": subject or None, "text": text})
            total += len(text)
    finally:
        box.close()
    return sections


# ------------------------------------------------------------------ OpenDocument

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _odf_title(z: zipfile.ZipFile) -> str | None:
    try:
        root = ET.fromstring(z.read("meta.xml"))
    except (KeyError, ET.ParseError):
        return None
    for el in root.iter(f"{{{NS_DC}}}title"):
        if el.text and el.text.strip():
            return el.text.strip()[:200]
    return None


def _ods_sections(root) -> list[dict]:
    """Rows per sheet, shaped like read_xlsx. A repeated row or column is read once and never expanded
    (number-rows-repeated can stand for a million rows)."""
    sections, budget = [], 200_000
    for table in root.iter(f"{{{NS_TABLE}}}table"):
        name = table.get(f"{{{NS_TABLE}}}name") or f"Sheet {len(sections) + 1}"
        rows = []
        for row in table.iter(f"{{{NS_TABLE}}}table-row"):
            cells = []
            for cell in row:
                if _local(cell.tag) in ("table-cell", "covered-table-cell"):
                    text = "".join(cell.itertext()).strip()
                    if text:
                        cells.append(text)
            if cells:
                rows.append(" | ".join(cells))
                budget -= len(cells)
            if budget <= 0:
                break
        if rows:
            sections.append({"heading": f"Sheet: {name}", "text": "\n".join(rows)})
        if budget <= 0:
            break
    return sections


def _page_text(el, skip: frozenset = frozenset()) -> str:
    """Text of a slide or drawing page with paragraph breaks. Child elements whose tag is in `skip` are left out."""
    parts: list[str] = []

    def walk(e):
        if e.text:
            parts.append(e.text)
        for child in e:
            if child.tag in skip:
                continue
            walk(child)
            if child.tail:
                parts.append(child.tail)
        if _local(e.tag) in ("p", "h"):
            parts.append("\n")

    walk(el)
    text = re.sub(r"[ \t]+\n", "\n", "".join(parts))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _odf_pages(root, ext: str) -> list[dict]:
    """ODP: one section per slide, with its speaker notes after the slide text. ODG: one per drawing page."""
    notes_tag = f"{{{NS_PRESENTATION}}}notes"
    sections = []
    for n, page in enumerate(root.iter(f"{{{NS_DRAW}}}page"), 1):
        text = _page_text(page, frozenset({notes_tag}))
        notes = [c for c in page if c.tag == notes_tag]
        if notes:
            note_text = _page_text(notes[0])
            if note_text:
                text = (text + "\n\nSpeaker notes:\n" + note_text).strip()
        if text:
            heading = f"Slide {n}" if ext == "odp" else f"Page {n}"
            sections.append({"heading": heading, "text": text, "page": n})
    return sections


def read_opendocument(p: Path, ext: str) -> tuple[str | None, int | None, list[dict]]:
    """ODS, ODP and ODG: the text comes from content.xml, the title from meta.xml. Returns (title, pages, sections)."""
    with zipfile.ZipFile(p) as z:
        title = _odf_title(z)
        root = ET.fromstring(z.read("content.xml"))
    if ext == "ods":
        return title, None, _ods_sections(root)
    slides = _odf_pages(root, ext)
    return title, len(slides) or None, slides


# ------------------------------------------------------------------ e-books and notebooks

_XHTML_HEADING = re.compile(r"<h[1-3][^>]*>(.*?)</h[1-3]>", re.I | re.S)
_XHTML_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _markup_heading(s: str) -> str | None:
    for rx in (_XHTML_HEADING, _XHTML_TITLE):
        m = rx.search(s)
        if m:
            text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip()
            if text:
                return text[:200]
    return None


def read_epub(p: Path) -> tuple[str | None, list[dict]]:
    """Chapters in spine order: container.xml names the package document, its itemrefs give the order. Each chapter
    is headed by its first h1-h3 (or its <title>). Parts listed in encryption.xml are skipped, never decoded."""
    sections, total = [], 0
    with zipfile.ZipFile(p) as z:
        names = set(z.namelist())
        opf_path = None
        if "META-INF/container.xml" in names:
            for el in ET.fromstring(z.read("META-INF/container.xml")).iter():
                if _local(el.tag) == "rootfile" and el.get("full-path"):
                    opf_path = el.get("full-path")
                    break
        encrypted = set()
        if "META-INF/encryption.xml" in names:
            for el in ET.fromstring(z.read("META-INF/encryption.xml")).iter():
                if _local(el.tag) == "CipherReference" and el.get("URI"):
                    encrypted.add(posixpath.normpath(unquote(el.get("URI"))))
        title, spine = None, []
        if opf_path and opf_path in names:
            opf = ET.fromstring(z.read(opf_path))
            for el in opf.iter():
                if title is None and _local(el.tag) == "title" and el.text and el.text.strip():
                    title = el.text.strip()[:200]
            base = posixpath.dirname(opf_path)
            manifest = {el.get("id"): el.get("href") for el in opf.iter()
                        if _local(el.tag) == "item" and el.get("id") and el.get("href")}
            for el in opf.iter():
                if _local(el.tag) == "itemref" and el.get("idref") in manifest:
                    spine.append(posixpath.normpath(posixpath.join(base, unquote(manifest[el.get("idref")]))))
        if not spine:
            spine = sorted(n for n in names if n.lower().endswith((".xhtml", ".html", ".htm")))
        for name in spine:
            if name in encrypted or name not in names:
                continue
            markup = decode_bytes(z.read(name))
            body = html_to_text(markup).strip()
            if not body:
                continue
            sections.append({"heading": _markup_heading(markup), "text": body})
            total += len(body)
            if total >= MAX_TEXT:
                break
    return title, sections


def _joined(value) -> str:
    if isinstance(value, list):
        return "".join(str(x) for x in value)
    return str(value) if value else ""


def read_notebook(p: Path) -> tuple[str | None, list[dict]]:
    """Jupyter notebook: markdown and code cells, plus the plain-text outputs (streams, text/plain). Images and HTML
    outputs are left out. The whole file is parsed (a notebook with embedded images is larger than any text cut).
    Raises ValueError when the file is not a notebook."""
    nb = json.loads(p.read_bytes())
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        raise ValueError("not a notebook")
    title, sections, total = None, [], 0
    for i, cell in enumerate(nb["cells"], 1):
        if not isinstance(cell, dict):
            continue
        source = _joined(cell.get("source"))
        kind = cell.get("cell_type")
        if kind == "markdown":
            if title is None:
                m = re.search(r"^#\s+(.+)$", source, re.M)
                if m:
                    title = m.group(1).strip()[:200]
            parts = [source]
        elif kind == "code":
            parts = [source]
            for out in cell.get("outputs") or []:
                if not isinstance(out, dict):
                    continue
                if out.get("output_type") == "stream":
                    parts.append(_joined(out.get("text"))[:NOTEBOOK_OUTPUT_CHARS])
                elif out.get("output_type") in ("execute_result", "display_data"):
                    data = out.get("data") if isinstance(out.get("data"), dict) else {}
                    parts.append(_joined(data.get("text/plain"))[:NOTEBOOK_OUTPUT_CHARS])
        else:
            continue
        body = "\n\n".join(x for x in parts if x.strip())
        if body:
            sections.append({"heading": f"Cell {i}", "text": body})
            total += len(body)
            if total >= MAX_TEXT:
                break
    return title, sections


# ------------------------------------------------------------------ archives (member names only)

def _archive_kind(p: Path, ext: str) -> str | None:
    """'zip', 'tar' or 'libarchive' for an archive whose member names can be listed; None otherwise."""
    if p.name.lower().endswith(TAR_SUFFIXES):
        return "tar"
    if ext == "zip":
        return "zip"
    if ext in ("tar", "tgz", "tbz2", "txz"):
        return "tar"
    if ext in ("7z", "rar"):
        return "libarchive"
    return None


def list_archive(p: Path, kind: str) -> list[str]:
    """Member names only; nothing is extracted to disk. zip and tar use the stdlib. 7z, rar and the rest go through
    the system bsdtar (libarchive), which lists them."""
    if kind == "zip":
        with zipfile.ZipFile(p) as z:
            return z.namelist()[:ARCHIVE_MAX_MEMBERS]
    if kind == "tar":
        names = []
        with tarfile.open(p, "r:*") as t:
            for member in t:
                names.append(member.name)
                if len(names) >= ARCHIVE_MAX_MEMBERS:
                    break
        return names
    tool = "/usr/bin/bsdtar"
    if not Path(tool).exists():
        return []
    r = run([tool, "-tf", str(p)], timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[:200] or "bsdtar failed")
    return r.stdout.decode("utf-8", "replace").splitlines()[:ARCHIVE_MAX_MEMBERS]


def archive_sections(p: Path, kind: str) -> list[dict]:
    names = list_archive(p, kind)
    if not names:
        return []
    return [{"heading": f"Archive contents ({len(names)} entries)", "text": "\n".join(names)}]


# ------------------------------------------------------------------ iWork (.pages, .numbers, .key)

class _Package:
    """The members of a zip-format file or of a package folder. Names are found without regard to case."""

    def __init__(self, p: Path):
        self.root = p
        self._zip = None if p.is_dir() else zipfile.ZipFile(p)
        if self._zip is not None:
            self.names = self._zip.namelist()
        else:
            self.names = [q.relative_to(p).as_posix() for q in p.rglob("*") if q.is_file()]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._zip is not None:
            self._zip.close()

    def find(self, want: str) -> str | None:
        low = want.lower()
        return next((n for n in self.names if n.lower() == low), None)

    def extract(self, name: str, dest: Path) -> Path:
        data = self._zip.read(name) if self._zip is not None else (self.root / name).read_bytes()
        dest.write_bytes(data)
        return dest


def _read_iwork(p: Path, media: dict) -> tuple[str | None, int | None, list[dict], dict]:
    """iWork documents keep a QuickLook preview: a PDF of the whole document (its text layer and scanned pages get the
    PDF treatment) or, in older files, only a JPEG (OCRed in heavy()). A file that merely has the extension, neither
    a zip file nor a package, gives metadata only. The preview PDF is copied to a private temporary folder for the
    light pass and removed again; heavy() copies it again from the document."""
    if not (p.is_dir() or zipfile.is_zipfile(p)):
        return None, None, [], {}
    with _Package(p) as pk:
        preview = pk.find("QuickLook/Preview.pdf")
        if preview:
            tmp = _private_tmp("iwork-")
            try:
                title, pages, sections, heavy = _pdf_light(pk.extract(preview, tmp / "preview.pdf"), media)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            return title, pages, sections, ({**heavy, "preview": preview} if heavy else {})
        # The JPEG preview is the higher-resolution one; the QuickLook thumbnail is only a small picture of page 1.
        image = pk.find("preview.jpg") or pk.find("QuickLook/Thumbnail.jpg")
        if image and media.get("images", True):
            return None, None, [], {"image_member": image, "ocr_image": True, "thumb": True}
    return None, None, [], {}


# ------------------------------------------------------------------ PDF

def _page_is_scanned(text: str, coverage: float | None) -> bool:
    """Does this page need OCR? The text layer counts as missing when a page has fewer than SCANNED_PAGE_CHARS
    characters. A page with a header or footer and a scanned body passes that test, so the helper also reports the
    share of the page that text lines cover (`coverage`, only for pages with few characters): below
    SCANNED_PAGE_COVERAGE the text is only a thin layer over an image, and the page is OCRed too. Pages with more text
    are never treated as scanned. Without `coverage` (an older helper) only the character rule applies."""
    chars = len(re.sub(r"\s", "", text))
    if chars < SCANNED_PAGE_CHARS:
        return True
    return coverage is not None and chars < LOW_TEXT_CHARS and coverage < SCANNED_PAGE_COVERAGE


def _pdf_light(p: Path, media: dict) -> tuple[str | None, int | None, list[dict], dict]:
    info = helper("pdf", str(p), "--max-pages", "1500", timeout=300)
    pages = info.get("pages")
    title = None
    meta_title = (info.get("meta") or {}).get("Title") or ""
    if meta_title and not re.match(r"(?i)^(untitled|microsoft word|document|scan|\s*$)", meta_title):
        title = meta_title.strip()[:200]
    if info.get("encrypted"):
        return title, pages, [], {}
    sections, scanned = [], []
    for it in info.get("items", []):
        text = it.get("text") or ""
        # Text on a page is kept from SCANNED_PAGE_CHARS characters up. Below that (page numbers, Bates numbers on a
        # scan) it is not worth an item of its own; such a page is OCRed instead, when it is flagged below.
        if len(re.sub(r"\s", "", text)) >= SCANNED_PAGE_CHARS:
            sections.append({"heading": f"Page {it['page']}", "text": text, "page": it["page"]})
        if _page_is_scanned(text, it.get("coverage")):
            scanned.append(it["page"])
    policy = media.get("pdf_pages", "smart")
    cap = int(media.get("max_pdf_pages", 30))
    if policy == "all":
        visual = list(range(1, min(pages or 0, cap) + 1))
    elif policy == "smart":
        visual = sorted(set([1] + scanned))[:cap] if pages else []
    else:
        visual = []
    heavy = {"thumb": True}
    if scanned:
        heavy["ocr"] = scanned[:max(cap, 100)]
    if visual:
        heavy["visual"] = visual
    return title, pages, sections, heavy


# ------------------------------------------------------------------ light

def light(p: Path, kind: str, ext: str, media: dict) -> dict:
    """Cheap extraction. Returns {title, lang, pages, items, heavy}."""
    title, pages, sections, heavy = None, None, [], {}
    if kind == "text":
        if ext in EPUB_EXT:
            title, sections = read_epub(p)
        elif ext == "ipynb":
            try:
                title, sections = read_notebook(p)
            except ValueError:
                sections = [{"heading": None, "text": read_text_file(p)}]
        else:
            text = read_text_file(p)
            if ext in ("html", "htm", "xhtml"):
                text = html_to_text(text)
            if ext in ("md", "markdown", "mdx"):
                sections = markdown_sections(text)
                m = re.search(r"^#\s+(.+)$", text, re.M)
                title = m.group(1).strip() if m else None
            else:
                sections = [{"heading": None, "text": text}]
    elif kind == "office":
        if ext in IWORK_EXT:
            title, pages, sections, heavy = _read_iwork(p, media)
        elif ext in OPENDOC_EXT:
            title, pages, sections = read_opendocument(p, ext)
        elif ext in SHEET_EXT:
            sections = read_xlsx(p)
        elif ext in SLIDE_EXT:
            sections = read_pptx(p)
            pages = len(sections) or None
        else:
            try:
                text = read_textutil(p)
            except Exception:
                text = read_docx_zip(p) if ext in ("docx", "docm") else ""
            sections = [{"heading": None, "text": text}]
    elif kind == "email":
        if ext in MBOX_EXT:
            sections = read_mbox(p)
        else:
            title, sections = read_eml(p)
    elif kind == "pdf":
        title, pages, sections, heavy = _pdf_light(p, media)
    elif kind == "image" and media.get("images", True):
        heavy = {"image": True, "ocr": True, "thumb": True}
    elif kind == "audio" and media.get("audio", True):
        heavy = {"audio": True}
    elif kind == "video" and media.get("video", True):
        heavy = {"video": True, "thumb": True}
    elif kind == "other":
        arch = _archive_kind(p, ext)
        if arch:
            sections = archive_sections(p, arch)

    items = sections_to_items(sections)
    sample = " ".join(i["text"] for i in items[:6])
    return {"title": title, "lang": detect_lang(sample) if sample else None, "pages": pages,
            "items": items, "heavy": heavy or None}


# ------------------------------------------------------------------ heavy

def _ocr_args(media: dict) -> list[str]:
    """Languages for Vision, when the collection sets them (media.ocr_langs). Unset: automatic detection."""
    langs = media.get("ocr_langs")
    if isinstance(langs, (list, tuple)):
        langs = ",".join(langs)
    return ["--langs", str(langs)] if langs else []


def _pdf_heavy(pdf: Path, spec: dict, work: Path, thumb_file: Path, media: dict) -> tuple[list[dict], bool]:
    items, thumb = [], False
    if spec.get("ocr"):
        pages = [int(x) for x in spec["ocr"]]
        res = helper("ocr", str(pdf), "--pages", ",".join(map(str, pages)), *_ocr_args(media),
                     timeout=min(7200, max(900, 30 * len(pages))))
        for it in res.get("items", []):
            text = it.get("text") or ""
            if len(text.strip()) >= 3:
                for c in chunk_text(text):
                    items.append({"modality": "ocr", "text": c, "page": it["page"],
                                  "heading": f"Page {it['page']} (OCR)"})
    if spec.get("visual"):
        res = helper("render", str(pdf), "--pages", ",".join(map(str, spec["visual"])), "--out", str(work),
                     "--max-px", "1280", timeout=600)
        for it in res.get("items", []):
            items.append({"modality": "page", "page": it["page"], "media": it["file"], "heading": f"Page {it['page']}"})
    if spec.get("thumb"):
        res = helper("render", str(pdf), "--pages", "1", "--out", str(work / "thumb"), "--max-px", "420", timeout=120)
        if res.get("items"):
            shutil.move(res["items"][0]["file"], thumb_file)
            thumb = True
    return items, thumb


def _image_heavy(src: Path, work: Path, thumb_file: Path, ocr: bool, media: dict) -> tuple[list[dict], bool]:
    out = work / "image.jpg"
    res = helper("image", str(src), "--out", str(out), "--max-px", "1024", "--thumb", str(thumb_file),
                 "--thumb-px", "420", timeout=120)
    items = [{"modality": "image", "media": str(out)}]
    if ocr:
        text = (helper("ocr", str(src), *_ocr_args(media), timeout=300).get("items") or [{}])[0].get("text") or ""
        if len(text.strip()) >= 8:
            for c in chunk_text(text):
                items.append({"modality": "ocr", "text": c, "heading": "Text in image (OCR)"})
    return items, bool(res.get("thumb"))


def _ffprobe_duration(p: Path) -> tuple[float | None, bool, bool]:
    if not shutil.which("ffprobe"):
        return None, False, False
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(p)], 60)
    try:
        d = json.loads(r.stdout)
        types = {s.get("codec_type") for s in d.get("streams", [])}
        return float(d["format"]["duration"]), "video" in types, "audio" in types
    except Exception:
        return None, False, False


def heavy(p: Path, kind: str, spec: dict, coll: str, file_id: int, media: dict) -> dict:
    """Expensive extraction. Returns {items, thumb, duration}; media items carry a temp file in 'media'."""
    work = paths.TMP / coll / str(file_id)
    work.mkdir(parents=True, exist_ok=True)
    thumbs = paths.THUMBS / coll
    thumbs.mkdir(parents=True, exist_ok=True)
    thumb_file = thumbs / f"{file_id}.jpg"
    items, thumb, duration = [], False, None

    if kind == "pdf":
        items, thumb = _pdf_heavy(p, spec, work, thumb_file, media)

    elif kind == "office" and (spec.get("preview") or spec.get("image_member")):
        with _Package(p) as pk:
            if spec.get("preview") and pk.find(spec["preview"]):
                pdf = pk.extract(pk.find(spec["preview"]), work / "preview.pdf")
                items, thumb = _pdf_heavy(pdf, spec, work, thumb_file, media)
            elif spec.get("image_member") and pk.find(spec["image_member"]):
                member = pk.find(spec["image_member"])
                img = pk.extract(member, work / f"member{Path(member).suffix.lower()}")
                items, thumb = _image_heavy(img, work, thumb_file, bool(spec.get("ocr_image")), media)

    elif kind == "image":
        items, thumb = _image_heavy(p, work, thumb_file, bool(spec.get("ocr")), media)

    elif kind in ("audio", "video"):
        duration, has_video, has_audio = _ffprobe_duration(p)
        if duration and shutil.which("ffmpeg"):
            limit = min(duration, float(media.get("max_media_minutes", 60)) * 60)
            if kind == "video" and has_video:
                step = 30.0
                t = 0.0
                while t < limit:
                    seg = work / f"v-{int(t)}.mp4"
                    r = run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-t", str(step), "-i", str(p), "-an",
                             "-vf", "fps=1,scale=448:-2", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                             str(seg)], 300)
                    if r.returncode == 0 and seg.exists() and seg.stat().st_size > 0:
                        items.append({"modality": "video", "t0": t, "t1": min(t + step, duration), "media": str(seg)})
                    t += step
                r = run(["ffmpeg", "-v", "error", "-y", "-ss", str(min(1.0, duration / 2)), "-i", str(p),
                         "-frames:v", "1", "-vf", "scale=420:-2", str(thumb_file)], 120)
                thumb = r.returncode == 0 and thumb_file.exists()
            if has_audio:
                step = 60.0
                t = 0.0
                while t < limit:
                    seg = work / f"a-{int(t)}.wav"
                    r = run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-t", str(step), "-i", str(p), "-vn",
                             "-ac", "1", "-ar", "16000", str(seg)], 300)
                    if r.returncode == 0 and seg.exists() and seg.stat().st_size > 2000:
                        items.append({"modality": "audio", "t0": t, "t1": min(t + step, duration), "media": str(seg)})
                    t += step
                if media.get("transcribe", True):
                    items += transcript_items(p, work, limit)
    return {"items": items, "thumb": thumb, "duration": duration}


# ------------------------------------------------------------------ transcripts

def _wav_seconds(wav: Path) -> float:
    with wave.open(str(wav), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 16000)


_SILENCE_START = re.compile(r"silence_start: (-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end: (-?\d+(?:\.\d+)?)")


def _silence_cuts(wav: Path, total: float) -> list[tuple[float, float]]:
    """Silent stretches of at least TRANSCRIPT_SILENCE_MIN_S seconds, from ffmpeg's silencedetect (one audio pass over
    the speech WAV). silencedetect reports at the info level, so the log level is not lowered. A silence without an
    end runs to the end of the file."""
    r = run(["ffmpeg", "-nostats", "-i", str(wav), "-af",
             f"silencedetect=noise={TRANSCRIPT_SILENCE_DB}dB:d={TRANSCRIPT_SILENCE_MIN_S}", "-f", "null", "-"], 600)
    if r.returncode != 0:
        raise RuntimeError("silencedetect failed")
    log = r.stderr.decode("utf-8", "replace")
    starts = [max(0.0, float(x)) for x in _SILENCE_START.findall(log)]
    ends = [float(x) for x in _SILENCE_END.findall(log)]
    cuts = []
    for i, s in enumerate(starts):
        e = min(ends[i] if i < len(ends) else total, total)
        if e > s:
            cuts.append((min(s, total), e))
    return cuts


def _speech_only(wav: Path, work: Path) -> tuple[Path, list[tuple[float, float]]] | None:
    """A copy of the speech WAV without its long silences, and the kept stretches of the original as (start, end) in
    seconds, in order. Only the 16 kHz mono 16-bit WAV written by transcript_items is read (frame offsets are exact).
    None when the copy would save less than 20% and less than a minute: the caller then transcribes the full WAV."""
    with wave.open(str(wav), "rb") as src:
        if src.getnchannels() != 1 or src.getsampwidth() != 2:
            return None
        rate, frames = src.getframerate(), src.getnframes()
        total = frames / float(rate)
        keep: list[tuple[float, float]] = []
        cur = 0.0
        for s, e in _silence_cuts(wav, total):
            a, b = s + TRANSCRIPT_KEEP_PAD_S, e - TRANSCRIPT_KEEP_PAD_S
            if b <= a:
                continue
            if a > cur:
                keep.append((cur, a))
            cur = max(cur, b)
        if total > cur:
            keep.append((cur, total))
        saved = total - sum(y - x for x, y in keep)
        if saved < max(60.0, 0.2 * total):
            return None
        out = work / "speech-cut.wav"
        with wave.open(str(out), "wb") as dst:
            dst.setnchannels(1)
            dst.setsampwidth(2)
            dst.setframerate(rate)
            for x, y in keep:
                f0, f1 = int(round(x * rate)), min(frames, int(round(y * rate)))
                if f1 > f0:
                    src.setpos(f0)
                    dst.writeframes(src.readframes(f1 - f0))
    return out, keep


def _timeline(keep: list[tuple[float, float]]) -> list[tuple[float, float, float]]:
    """(output start, original start, length) per kept stretch: maps a Whisper time back to the original file."""
    out, spans = 0.0, []
    for a, b in keep:
        spans.append((out, a, b - a))
        out += b - a
    return spans


def _source_time(t: float, spans: list[tuple[float, float, float]]) -> tuple[float, int]:
    """(original time, index of the stretch) for a time in the speech-only audio."""
    for i, (o, src, n) in enumerate(spans):
        if t < o + n:
            return src + max(0.0, t - o), i
    o, src, n = spans[-1]
    return src + n, len(spans) - 1


_CREDIT_PHRASES = ("amara org", "untertitelung", "untertitel von", "untertitel im auftrag", "subtitles by",
                   "subtitled by")
_SIGNOFF = {"thank you", "thanks", "vielen dank", "danke", "bye", "you"}


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def _is_signoff(n: str) -> bool:
    return n in _SIGNOFF or n.startswith(("thanks for watching", "thank you for watching", "thank you very much"))


def _is_artefact(seg: dict) -> bool:
    """Whisper's output for silence or noise, not speech. Non-speech tags such as [Music] and subtitle credits are
    dropped always. A sign-off such as 'Thank you.' is dropped only when the model itself judged the segment unlikely
    to be speech (no_speech_prob or avg_logprob), because a real speaker may say it."""
    raw = seg["text"].strip()
    n = _norm(raw)
    if not n:
        return True
    words = n.split()
    if len(words) <= 6 and raw[:1] in "[(*♪" and raw[-1:] in "])*♪":
        return True
    if len(words) <= 15 and any(ph in n for ph in _CREDIT_PHRASES):
        return True
    avg = seg.get("avg_logprob")
    unlikely = (seg.get("no_speech_prob") or 0) > 0.5 or (avg is not None and avg < -1.0)
    return unlikely and _is_signoff(n)


def _clean_segments(segs: list[dict]) -> list[dict]:
    """Drops the hallucinations: a line repeated three or more times in a row is kept once, artefacts are dropped."""
    out, i = [], 0
    while i < len(segs):
        n = _norm(segs[i].get("text") or "")
        j = i
        if n:
            while j + 1 < len(segs) and _norm(segs[j + 1].get("text") or "") == n:
                j += 1
        run_len = j - i + 1
        for k in (range(i, i + 1) if run_len >= 3 else range(i, j + 1)):
            if not _is_artefact(segs[k]):
                out.append(segs[k])
        i = j + 1
    return out


def _passage(texts, t0, t1) -> dict:
    mmss = lambda t: f"{int(t) // 60}:{int(t) % 60:02d}"
    return {"modality": "transcript", "t0": round(t0, 1), "t1": round(t1, 1), "text": " ".join(texts),
            "heading": f"Transcript {mmss(t0)}–{mmss(t1)}"}


def _passages(segs: list[dict]) -> list[dict]:
    """Groups segments into passages of roughly 30-60 s. A passage ends at a sentence end once it is 30 s long, never
    runs past 60 s or PASSAGE_MAX_CHARS, and never spans a cut (a change of stretch)."""
    items, cur, t0, t1, blk = [], [], 0.0, 0.0, None
    for s in segs:
        if cur:
            span = t1 - t0
            last = cur[-1].strip()
            ends_sentence = bool(last) and last[-1] in ".!?…"
            if (s["block"] != blk or span >= PASSAGE_HARD_S or sum(len(x) for x in cur) >= PASSAGE_MAX_CHARS
                    or (span >= PASSAGE_SOFT_S and ends_sentence)):
                items.append(_passage(cur, t0, t1))
                cur = []
        if not cur:
            t0, blk = s["start"], s["block"]
        cur.append(s["text"])
        t1 = s["end"]
    if cur:
        items.append(_passage(cur, t0, t1))
    return items


def transcript_items(p: Path, work: Path, limit_s: float) -> list[dict]:
    """Whisper transcript in passages of roughly 30-60 s, each with its time range in the original file.

    Long silences are cut out first (fewer seconds for Whisper to read, and the language is detected on speech).
    One Whisper call per file: the language is detected once and kept for the whole file. Times are mapped back to
    the original timeline. Any failure in the cut falls back to the full WAV."""
    from .embedder import pool
    if not pool().has_asr():
        return []
    wav = work / "speech.wav"
    r = run(["ffmpeg", "-v", "error", "-y", "-t", str(limit_s), "-i", str(p), "-vn", "-ac", "1", "-ar", "16000",
             str(wav)], 900)
    if r.returncode != 0 or not wav.exists():
        return []
    try:
        cut = _speech_only(wav, work)
    except Exception:
        cut = None
    if cut:
        asr_wav, keep = cut
    else:
        try:
            seconds = _wav_seconds(wav)
        except Exception:
            seconds = limit_s          # a WAV the stdlib cannot read: the timeline is then the requested length
        asr_wav, keep = wav, [(0.0, seconds)]
    spans = _timeline(keep)
    res = pool().transcribe(str(asr_wav))
    segs = []
    for s in _clean_segments(res.get("segments") or []):
        a, blk = _source_time(s["start"], spans)
        b, _ = _source_time(max(s["start"], s["end"] - 0.01), spans)
        segs.append({**s, "start": a, "end": max(a, b), "block": blk})
    items = _passages(segs)
    for it in items:
        it["lang"] = res.get("language")
    return items


def cleanup_work(coll: str, file_id: int) -> None:
    shutil.rmtree(paths.TMP / coll / str(file_id), ignore_errors=True)
