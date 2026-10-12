"""The whole-disk content index: find a file by what is IN it, across the Mac, without the per-folder pipeline.

"image of a zebra" finds zebra photos; "Rechnung Telekom" finds the invoice whose text says so. Every file the name
index (mse/macindex.py) lists under the whole-disk roots is an entry, with its name and folder. Some kinds also get a
description, made cheaply and in the background:

  image   Vision classification labels (1,303 identifiers) and, for pictures that look like text, a .fast OCR read
  pdf     the text layer of the first two pages; a scan (no text layer) is read by .fast OCR of page 1
  doc     text, markdown, html, csv, json (up to 1 MB), docx, odt, rtf, doc, eml, and the QuickLook preview of .pages
  video   labels of the poster frame at 10 % of the duration (mp4, mov, m4v)

Everything else (code, audio, binaries, fonts, archives, disk images) is by name only. Folders the owner picks keep the
deep, per-collection indexing; their files get a whole-disk entry too, so one search covers both.

Privacy comes from the name index, which is the candidate list: whatever it excludes (credential stores, secrets, keys,
browser profiles, Mail, Messages, the system and cache folders, mac_index.exclude) never reaches this index. On top of
that: hidden files and folders, package contents (.app, .framework, .photoslibrary, the insides of .pages), build,
dependency and cache folders (ignore.PRUNE_DIRS rules), nested git repositories, and wholedisk.extra_excludes. Every path
is checked again before it is described and again before a result is shown, so a folder excluded a moment ago disappears
at once. iCloud placeholders (dataless files) and symbolic links are never read.

Change tracking reads the name index (macindex.db) read-only and never writes there:
  - every minute: the rows added since the last look (rowid above the last one seen) and the rows touched in the last
    hour (mtime) are checked against this index;
  - every hour, after a settings change, and after a rescan: a merge of both indexes in path order, which also drops the
    rows of deleted or newly excluded files. Nothing here runs while the name index is being built or crawled.
The name index is not edited, so no FSEvents hook is needed. The price is that a deletion is noticed within the hour;
until then the search checks every result on disk before showing it, so a deleted file never appears.

The describer runs in one background thread, in small batches, one file at a time, and checks the gate before every
file. `low` (the default) describes only on AC power after 60 s without input; `balanced` describes on AC power, or on
battery after 60 s without input. Both stop at once when the user is active (less than 5 s without input), and within
one file when the switch is turned off. The helper (`msehelper describe`) starts once per batch, so the Vision work runs
where the system schedules it (Neural Engine, GPU or CPU): at background QoS for `low`, at utility QoS for `balanced`.
Without the helper, pictures, PDFs and videos wait (name only) and the documents are still read.

The search is one FTS5 table, contentless (the text lives once, in the files table): name, folder, labels and text,
BM25-weighted with labels and names first. Words of a query are expanded English<->German for common labels, type
words ("image", "photo", "pdf", "document", "Bild", "Foto") become a kind filter, and stop words are dropped. AND
across the concepts first, cut at a pool a little larger than the page; only while the page is short, the rows matching
one concept fill it, rarest concept first (document counts from the fts5vocab table), each ranked and capped on its own,
so a common word is ranked only when it is still needed. Phase 2 (embeddings of the descriptions) has its table
(vectors) and its `why` value ("vector") reserved; nothing writes or reads them yet.
"""

import collections
import json
import os
import re
import select
import sqlite3
import stat as S
import subprocess
import threading
import time
import traceback
import unicodedata
import zipfile
from pathlib import Path

from . import config, filequery, macindex, paths
from .extract import (AUDIO_EXT, CODE_EXT, IMAGE_EXT, OPENDOC_EXT, SHEET_EXT, SLIDE_EXT, TEXT_EXT, TEXTUTIL_EXT,
                      VIDEO_EXT, decode_bytes, html_to_text, markdown_sections, read_docx_zip, read_eml,
                      read_opendocument, read_textutil)
from .ignore import MARKER_EXTRA, PROJECT_MARKERS, content_skipped, effective_prune
from .jobs import HEAVY_IDLE_ACTIVE_S, HEAVY_IDLE_LOW_S, heavy_work_allowed

DB_NAME = "wholedisk.db"
DESCRIBER_VERSION = 1            # bump when a descriptor changes: the described entries are described again
PRIORITIES = ("low", "balanced")
KINDS = ("image", "pdf", "doc", "video", "audio", "code", "other")
IWORK_EXT = frozenset({"pages", "numbers", "key"})   # packages: the package is an entry, its insides are never listed
ST_NAME, ST_PENDING, ST_DONE, ST_FAILED = 0, 1, 2, 3
DATALESS = 0x40000000            # SF_DATALESS: an iCloud placeholder

FAST_SYNC_S = 60                 # the rowid and recent-mtime look at the name index
FULL_SYNC_S = 3600               # the merge in path order: changes, deletions, rule changes
RECENT_S = 3600                  # rows touched this recently are checked on every fast look
MIN_AGE_S = 30                   # a file changed this recently may still be written: it waits
IDLE_LOW_S = HEAVY_IDLE_LOW_S    # the gate's thresholds live in jobs.heavy_work_allowed, shared with other heavy work
IDLE_ACTIVE_S = HEAVY_IDLE_ACTIVE_S
GATE_POLL_S = 2.0                # how often a paused describer looks again
BATCH = 16                       # files per batch; the helper process starts once per batch
PAUSE_BETWEEN_BATCHES_S = {"low": 0.5, "balanced": 0.2}
WATCHDOG_S = 30.0                # no answer from the helper for this long: it is killed and the file counts as failed
MAX_FAILS = 3                    # after this many failures a file waits for its mtime to change (or a rescan)
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MAX_PDF_BYTES = 80 * 1024 * 1024
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
MAX_DOC_BYTES = 20 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_EML_BYTES = 5 * 1024 * 1024
MAX_PREVIEW_BYTES = 50 * 1024 * 1024
SNIPPET_CHARS = 300
RESULT_LABELS = 10
MAX_POOL = 5000                  # the most rows one AND query ranks and returns (stale rows are dropped on verification)
OR_FILL_EXTRA = 50               # a one-concept fill query returns offset + limit + 1 + this many rows at most
STALE_MAX = 20000                # rows a search found gone, waiting for the writer; more are dropped by the next merge anyway
HELPER_QOS = {"low": "background", "balanced": "utility"}
TEXT_DOC_EXT = frozenset({"txt", "text", "md", "markdown", "rst", "org", "csv", "tsv", "json", "yaml", "yml", "xml",
                          "html", "htm", "xhtml", "log", "srt", "vtt"})
OFFICE_DOC_EXT = frozenset({"docx", "odt", "rtf", "doc", "wordml", "webarchive", "eml", "pages"})
VIDEO_DESCRIBED_EXT = frozenset({"mp4", "mov", "m4v"})
SETTINGS_KEYS = ("enabled", "roots", "extra_excludes", "priority", "images", "documents", "ocr_images", "max_text_kb")
DOWNLOAD_EXT = frozenset({"crdownload", "part", "download"})
ROOT_PRIORITY_FOLDERS = frozenset({"Desktop", "Documents", "Downloads", "Pictures"})


# ---------------------------------------------------------------- settings

def settings() -> dict:
    """config wholedisk, merged over the defaults (config.DEFAULTS)."""
    return {**config.DEFAULTS["wholedisk"], **(config.load().get("wholedisk") or {})}


def check_settings(v: dict) -> dict:
    """Validation of a POST /api/settings change for wholedisk: a ValueError becomes a 400. Roots are stored as absolute
    folders without duplicates; extra_excludes are gitignore-style patterns (no "!" negation)."""
    if not isinstance(v, dict):
        raise ValueError("wholedisk must be an object")
    allowed = set(SETTINGS_KEYS)
    for k in v:
        if k not in allowed:
            raise ValueError(f"cannot change wholedisk.{k}")
    for k in ("enabled", "images", "documents", "ocr_images"):
        if k in v and not isinstance(v[k], bool):
            raise ValueError(f"wholedisk.{k} must be true or false")
    if "priority" in v and v["priority"] not in PRIORITIES:
        raise ValueError("wholedisk.priority must be low or balanced")
    if "roots" in v:
        roots = v["roots"]
        if not isinstance(roots, list) or not roots or not all(isinstance(r, str) and r.strip() for r in roots):
            raise ValueError("wholedisk.roots must be a non-empty list of folders")
        out: list[str] = []
        for r in roots:
            p = macindex._expand(r, macindex.home()).rstrip("/") or "/"
            if not p.startswith("/") or "\x00" in p or ".." in p.split("/"):
                raise ValueError(f"wholedisk.roots: {r!r} is not an absolute folder")
            if p not in out:
                out.append(p)
        v = {**v, "roots": out}
    if "extra_excludes" in v:
        ex = v["extra_excludes"]
        if not isinstance(ex, list) or len(ex) > 200:
            raise ValueError("wholedisk.extra_excludes must be a list of up to 200 patterns")
        for pat in ex:
            if not isinstance(pat, str) or not pat.strip() or len(pat) > 300 or "\x00" in pat:
                raise ValueError("wholedisk.extra_excludes: each pattern is text of 1 to 300 characters")
            if pat.strip().startswith("!"):
                raise ValueError("wholedisk.extra_excludes: a '!' negation is not supported")
    if "max_text_kb" in v:
        n = v["max_text_kb"]
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 64:
            raise ValueError("wholedisk.max_text_kb must be a whole number of kilobytes, 1 to 64")
    return v


def db_path() -> Path:
    return paths.DATA / DB_NAME


# ---------------------------------------------------------------- text and query language

_TOK = re.compile(r"[^\W_]+")


def fold(s: str) -> str:
    """Lower case, no accents: the folding of the FTS5 tokenizer (unicode61 remove_diacritics 2)."""
    s = unicodedata.normalize("NFKD", s.casefold())
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _tokens(s: str) -> list[str]:
    return _TOK.findall(fold(s))


_DE_FUNCTION = frozenset({"der", "die", "das", "und", "ist", "nicht", "mit", "ein", "eine", "sie", "auf", "fur", "von",
                          "zu", "dem", "den", "des", "auch", "wir", "werden", "sind", "wird", "bei", "im", "oder"})
_EN_FUNCTION = frozenset({"the", "and", "is", "not", "with", "of", "to", "in", "that", "it", "for", "are", "was", "this",
                          "be", "on", "as", "by", "we", "have", "from", "or", "at", "an", "which"})


def _lang(text: str) -> str:
    """de or en by the commonest function words of the first kilobytes; "" when it cannot tell."""
    words = _tokens(text[:4000])
    de = sum(1 for w in words if w in _DE_FUNCTION)
    en = sum(1 for w in words if w in _EN_FUNCTION)
    if de + en < 5:
        return ""
    return "de" if de > en else "en"


# Words that carry no meaning in a search, English and German (folded).
STOP_WORDS = frozenset(fold(w) for w in (
    "a an the of in on at to for from with by and or my me all any some is are was were be been about this that these "
    "those show find search please give list what which who where when how i it its file files "
    "der die das den dem des ein eine einen einem einer eines und oder von vom mit fur im in auf aus bei zu zum zur nach "
    "uber ist sind war mein meine meiner meinem alle alles zeig zeige finde suche bitte wo wann wie welche welcher datei "
    "dateien "
    # filler of natural sentences ("a tall animal with a very long neck", "zebras standing together in a grassy field")
    "very some many there here into onto while also just looking standing sitting together outdoors outside inside under "
    "over near next one two piece pieces").split())
# Type words become a kind filter, not a search term.
TYPE_WORDS = {fold(w): k for k, words in {
    "image": "image images img imgs picture pictures pic pics photo photos bild bilder foto fotos",
    "pdf": "pdf pdfs",
    "doc": "document documents doc docs dokument dokumente",
    "video": "video videos film filme movie movies",
    "audio": "audio sound sounds",
}.items() for w in words.split()}
# Concepts: a word and its near equivalents. The Vision labels are English; the German words map to them. Only
# single words are expanded.
CONCEPTS = [
    ("zebra",), ("dog", "hund", "hunde"), ("cat", "katze", "katzen"), ("horse", "pferd", "pferde"),
    ("bird", "vogel"), ("elephant", "elefant", "elefanten"), ("giraffe",), ("animal", "tier", "tiere"),
    ("fish", "fisch", "fische"),
    ("beach", "strand", "strande", "sand", "shore", "seashore", "coast", "coastline", "sea", "ocean", "waterfront",
     "water", "lake", "wasser"), ("mountain", "berg", "berge", "gebirge"),
    ("sea", "meer", "ozean"), ("forest", "wald", "walder"), ("tree", "baum", "baume"), ("flower", "blume", "blumen"),
    ("sky", "himmel"), ("snow", "schnee"), ("city", "stadt", "stadte"), ("house", "haus", "hauser"),
    ("food", "essen", "speisen", "mahlzeit"), ("apple", "apfel"), ("car", "auto", "wagen"),
    ("airplane", "flugzeug", "flugzeuge", "plane"), ("boat", "boot", "segelboot"), ("train", "zug", "zuge"),
    ("bicycle", "fahrrad", "fahrrader"), ("bridge", "brucke", "brucken"), ("laptop", "notebook"),
    ("computer", "rechner"), ("phone", "telefon", "handy"), ("chair", "stuhl"), ("book", "buch", "bucher"),
    ("person", "mensch", "menschen", "leute"), ("money", "geld"), ("map", "karte", "karten"),
    ("invoice", "rechnung", "rechnungen", "bill"), ("receipt", "quittung", "beleg", "belege"),
    ("contract", "vertrag", "vertrage"), ("letter", "brief", "briefe"), ("tax", "steuer", "steuern"),
    ("payslip", "gehaltsabrechnung", "lohnabrechnung"), ("sunset", "sonnenuntergang"), ("sign", "schild", "schilder"),
]
_CONCEPT_OF: dict[str, frozenset] = {}
for _c in CONCEPTS:
    _all = frozenset(fold(w) for w in _c)
    for _w in _all:
        _CONCEPT_OF[_w] = _CONCEPT_OF.get(_w, frozenset()) | _all

# The columns of wd_fts, in order: name, folder, labels, text.
BM25_WEIGHTS = "3.0, 0.3, 6.0, 1.0"


class Parsed:
    __slots__ = ("text", "groups", "kinds")

    def __init__(self, text: str, groups: list, kinds: frozenset):
        self.text, self.groups, self.kinds = text, groups, kinds

    def public(self) -> dict:
        return {"words": [list(g) for g in self.groups], "kinds": sorted(self.kinds)}


def _alternatives(t: str) -> frozenset:
    """The words a query word stands for: its concept, else, for an English plural, the singular too ("zebras" finds
    the label "zebra"). A German compound or plural is covered by the concept table, not by a rule."""
    if t in _CONCEPT_OF:
        return _CONCEPT_OF[t]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        single = t[:-1]
        return _CONCEPT_OF.get(single, frozenset({t, single}))
    return frozenset({t})


def parse(q: str) -> Parsed:
    """A query -> the concepts it names (each an OR of alternatives, the groups are ANDed) and the kinds its type words
    name. Stop words and type words never reach the search text. Folding makes "Hund" and "hund" the same word."""
    kinds: set[str] = set()
    groups: list[tuple] = []
    seen: set[tuple] = set()
    for t in _tokens(q or ""):
        if t in TYPE_WORDS:
            kinds.add(TYPE_WORDS[t])
            continue
        if t in STOP_WORDS:
            continue
        alts = tuple(sorted(_alternatives(t)))
        if alts not in seen:
            seen.add(alts)
            groups.append(alts)
    return Parsed(q or "", groups, frozenset(kinds))


def _match_expr(groups: list, mode: str) -> str:
    """The FTS5 expression. Each alternative is a quoted prefix ("zebra"*), so plurals and compound starts match."""
    parts = ["(" + " OR ".join(f'"{a}"*' for a in g) + ")" for g in groups]
    return (" AND " if mode == "and" else " OR ").join(parts)


def _starts(alts, words) -> bool:
    return any(w.startswith(a) for a in alts for w in words)


# ---------------------------------------------------------------- the database

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  folder TEXT NOT NULL,
  ext TEXT NOT NULL,
  kind TEXT NOT NULL,
  dkind TEXT,
  size INTEGER NOT NULL DEFAULT 0,
  mtime INTEGER NOT NULL DEFAULT 0,
  prio INTEGER NOT NULL DEFAULT 1,
  state INTEGER NOT NULL DEFAULT 0,
  fails INTEGER NOT NULL DEFAULT 0,
  desc_key TEXT,
  fail_key TEXT,
  labels TEXT NOT NULL DEFAULT '[]',
  snippet TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  lang TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS files_pend ON files(state, prio, mtime);
CREATE INDEX IF NOT EXISTS files_kind ON files(kind, mtime);
CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime);
-- columns: name, folder, labels, text (the order BM25_WEIGHTS refers to). Contentless: the text lives in files.
CREATE VIRTUAL TABLE IF NOT EXISTS wd_fts USING fts5(name, folder, labels, text, content='', contentless_delete=1,
  tokenize='unicode61 remove_diacritics 2', prefix='2 3');
-- how many rows carry each word (a view of the FTS index, no data of its own): the search ranks rare words first
CREATE VIRTUAL TABLE IF NOT EXISTS wd_vocab USING fts5vocab(wd_fts, 'row');
-- phase 2 (embeddings of the descriptions): the table exists; nothing writes to it yet
CREATE TABLE IF NOT EXISTS vectors(file_id INTEGER PRIMARY KEY, model TEXT NOT NULL, dims INTEGER NOT NULL,
  vec BLOB NOT NULL);
"""


class Store:
    """The whole-disk database. One writer connection, used under `lock`; readers open their own read-only connection."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._w: sqlite3.Connection | None = None

    def writer(self) -> sqlite3.Connection:
        if self._w is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA temp_store=MEMORY")
            c.executescript(SCHEMA)
            self._w = c
        return self._w

    def reader(self) -> sqlite3.Connection | None:
        if not self.path.exists():
            return None
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30, check_same_thread=False)

    def meta(self, k: str, default=None):
        with self.lock:
            row = self.writer().execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, c, k: str, v) -> None:
        c.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (k, json.dumps(v)))


def _fts_set(c, rowid: int, name: str, folder: str, labels: str, text: str) -> None:
    c.execute("DELETE FROM wd_fts WHERE rowid = ?", (rowid,))
    c.execute("INSERT INTO wd_fts(rowid, name, folder, labels, text) VALUES (?, ?, ?, ?, ?)",
              (rowid, name, folder, labels, text))


def _delete_ids(c, ids: list[int]) -> None:
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        c.execute(f"DELETE FROM wd_fts WHERE rowid IN ({q})", chunk)
        c.execute(f"DELETE FROM vectors WHERE file_id IN ({q})", chunk)
        c.execute(f"DELETE FROM files WHERE id IN ({q})", chunk)


# ---------------------------------------------------------------- scope: what is an entry, what is described

def ext_of(name: str) -> str:
    base = name.strip(".")
    return name.rsplit(".", 1)[-1].lower() if "." in base else ""


def kind_of_ext(ext: str) -> str:
    if ext == "pdf":
        return "pdf"
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in CODE_EXT:
        return "code"
    if ext in TEXT_EXT or ext in TEXTUTIL_EXT or ext in OPENDOC_EXT or ext in SHEET_EXT or ext in SLIDE_EXT \
            or ext in ("eml", "pages", "numbers", "epub"):
        return "doc"
    return "other"


def describe_kind(ext: str, is_dir: bool, size: int, name: str, cfg: dict) -> str | None:
    """The description this entry gets (image, pdf, doc, video), or None (name only). Size limits keep one huge file from
    costing a read: a file over its limit stays searchable by name."""
    if ext in DOWNLOAD_EXT:
        return None                                          # a download still being written
    if is_dir:
        return "doc" if (ext == "pages" and cfg["documents"]) else None
    if content_skipped(name, size):
        return None                                          # disk images, virtual machines, big logs and databases
    if ext in IMAGE_EXT:
        return "image" if cfg["images"] and size <= MAX_IMAGE_BYTES else None
    if ext == "pdf":
        return "pdf" if cfg["documents"] and size <= MAX_PDF_BYTES else None
    if ext in VIDEO_DESCRIBED_EXT:
        return "video" if cfg["images"] and size <= MAX_VIDEO_BYTES else None
    if not cfg["documents"]:
        return None
    if ext in TEXT_DOC_EXT:
        return "doc" if size <= (MAX_JSON_BYTES if ext == "json" else MAX_DOC_BYTES) else None
    if ext in OFFICE_DOC_EXT:
        return "doc" if size <= (MAX_EML_BYTES if ext == "eml" else MAX_DOC_BYTES) else None
    return None


class Scope:
    """Decides, for one path, whether it is an entry. The privacy rules are the name index's own (macindex.Excluder);
    the whole-disk roots and extra_excludes narrow them further; the folder rules are mse/ignore.py's. The name index is
    asked, read-only, for the project markers and the Python virtual environments. Git repositories are looked for on
    disk, because the name index never lists a .git folder."""

    def __init__(self, cfg: dict, excl, has_row, prune_names=None):
        self.cfg = cfg
        self.excl = excl
        self.extra = macindex.Excluder({"exclude": list(cfg.get("extra_excludes") or [])}, h=macindex.home()) \
            if cfg.get("extra_excludes") else None
        # a root that is not there now (an unmounted disk) keeps its rows: the name index lists nothing under it, the
        # search checks every result on disk, and dropping the rows would throw away their descriptions
        self.roots = tuple(r.rstrip("/") for r in cfg["roots"])
        self.has_row = has_row
        self.prune = {k.lower(): v for k, v in effective_prune(prune_names).items()}
        self._repo: dict[str, bool] = {}
        self._marker: dict[tuple, bool] = {}

    def root_of(self, path: str) -> str | None:
        for r in self.roots:
            if path == r or path.startswith(r + "/"):
                return r
        return None

    def blocked(self, path: str) -> bool:
        return self.excl.blocked(path) or bool(self.extra and self.extra.blocked(path))

    def _is_repo(self, folder: str) -> bool:
        v = self._repo.get(folder)
        if v is None:
            v = self._repo[folder] = os.path.lexists(folder + "/.git")
        return v

    def _has_marker(self, folder: str, name: str) -> bool:
        key = (folder, name)
        v = self._marker.get(key)
        if v is None:
            markers = list(PROJECT_MARKERS) + list(MARKER_EXTRA.get(name, ()))
            v = self._marker[key] = any(self.has_row(folder + "/" + m) for m in markers)
        return v

    def reason(self, path: str) -> str | None:
        """None when `path` is an entry; otherwise why not (for the status and the tests)."""
        root = self.root_of(path)
        if root is None:
            return "outside"
        if self.blocked(path):
            return "privacy"
        parts = path[len(root) + 1:].split("/")
        if any(p.startswith(".") for p in parts):
            return "hidden"
        folder = root
        for comp in parts[:-1]:
            if ext_of(comp) in filequery.PACKAGE_EXTS or ext_of(comp) in IWORK_EXT:
                return "package"
            rule = self.prune.get(comp.lower())
            if rule == "always":
                return "junk"
            if rule == "project" and self._has_marker(folder, comp):
                return "junk"
            if rule == "pyvenv" and self.has_row(f"{folder}/{comp}/pyvenv.cfg"):
                return "junk"
            here = folder + "/" + comp
            if self._is_repo(here):
                return "repo"
            folder = here
        return None

    def placement(self, path: str) -> tuple[str, int]:
        """(folder relative to the root, priority). Entries in Desktop, Documents, Downloads and Pictures come first."""
        root = self.root_of(path)
        parts = path[len(root) + 1:].split("/")
        folder = "/".join(parts[:-1])
        prio = 3 if (len(parts) > 1 and parts[0] in ROOT_PRIORITY_FOLDERS) else 1
        return folder, prio


def _candidate(path: str, is_dir: bool, size: int, mtime: int, scope: Scope, cfg: dict) -> dict | None:
    """The whole-disk row for one name-index row, or None when it is not an entry. Folders are entries only as the
    packages a .pages document is; every other folder is found by name through the name index."""
    name = path.rsplit("/", 1)[-1]
    ext = ext_of(name)
    if is_dir and ext != "pages":
        return None
    if scope.reason(path) is not None:
        return None
    folder, prio = scope.placement(path)
    size = int(size or 0)
    dkind = describe_kind(ext, is_dir, size, name, cfg)
    return {"path": path, "name": name, "folder": folder, "ext": ext, "kind": kind_of_ext(ext), "dkind": dkind,
            "size": 0 if is_dir else size, "mtime": int(mtime or 0), "prio": prio}


def _desc_key(size: int, mtime: int, cfg: dict) -> str:
    return f"{int(size)}:{int(mtime)}:v{DESCRIBER_VERSION}:{'ocr' if cfg['ocr_images'] else 'noocr'}:{cfg['max_text_kb']}"


def _settings_key(cfg: dict) -> str:
    keep = {k: cfg.get(k) for k in SETTINGS_KEYS}
    mac = macindex.settings()
    keep["mac_roots"] = mac.get("roots")
    keep["mac_exclude"] = mac.get("exclude")
    keep["prune"] = (config.load().get("indexing") or {}).get("prune_dirs")
    return json.dumps(keep, sort_keys=True, default=str)


# ---------------------------------------------------------------- the name index (read-only)

def _name_index_ready() -> bool:
    p = macindex.db_path()
    if not p.exists():
        return False
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
        try:
            row = c.execute("SELECT v FROM meta WHERE k = 'crawl'").fetchone()
        finally:
            c.close()
        return bool(json.loads(row[0]).get("finished")) if row else False
    except (sqlite3.Error, ValueError, TypeError):
        return False


class _NameIndex:
    """A read-only connection to the name index: the candidate lists and the marker checks."""

    def __init__(self):
        self.c = sqlite3.connect(f"file:{macindex.db_path()}?mode=ro", uri=True, timeout=30, check_same_thread=False)

    def has_row(self, path: str) -> bool:
        return self.c.execute("SELECT 1 FROM files WHERE path = ? LIMIT 1", (path,)).fetchone() is not None

    def close(self) -> None:
        self.c.close()


# ---------------------------------------------------------------- the descriptions

class HelperSession:
    """One `msehelper describe --stdin` process for one batch: one JSON line in, one JSON line out. A pause, or the user
    coming back, takes effect between two files. A helper silent for WATCHDOG_S is killed; the next call starts another."""

    def __init__(self, exe: str, opts: list[str]):
        self.exe, self.opts = exe, opts
        self.proc: subprocess.Popen | None = None
        self.buf = b""

    def _start(self) -> None:
        self.proc = subprocess.Popen([self.exe, "describe", "--stdin", *self.opts], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.buf = b""

    def call(self, payload: dict, timeout: float = WATCHDOG_S) -> dict:
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                self.kill()
                raise TimeoutError("the describer stopped answering")
            ready, _, _ = select.select([self.proc.stdout], [], [], left)
            if not ready:
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                self.kill()
                raise RuntimeError("the describer exited")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return json.loads(line.decode("utf-8", "replace"))

    def kill(self) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            p.kill()
            p.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        for f in (p.stdin, p.stdout):
            try:
                f.close()
            except OSError:
                pass

    def close(self) -> None:
        p = self.proc
        if p is None:
            return
        try:
            p.stdin.close()
            p.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            self.kill()
            return
        self.proc = None
        try:
            p.stdout.close()
        except OSError:
            pass


class DescribeFailed(Exception):
    """This file cannot be described now (it is counted, and waits for its size or mtime to change)."""


def _pages_preview(path: str) -> str:
    """The QuickLook PDF of a .pages document, written to one fixed file in the data folder (overwritten each time, so
    nothing is ever deleted). A .pages document is a folder, or in the older form a zip file."""
    paths.TMP.mkdir(parents=True, exist_ok=True)
    dest = paths.TMP / "wholedisk-preview.pdf"
    if os.path.isdir(path):
        src = os.path.join(path, "QuickLook", "Preview.pdf")
        if not os.path.isfile(src) or os.path.getsize(src) > MAX_PREVIEW_BYTES:
            raise DescribeFailed("no preview")
        with open(src, "rb") as f:
            data = f.read()
    else:
        try:
            with zipfile.ZipFile(path) as z:
                info = z.getinfo("QuickLook/Preview.pdf")
                if info.file_size > MAX_PREVIEW_BYTES:
                    raise DescribeFailed("preview too big")
                data = z.read(info)
        except (KeyError, zipfile.BadZipFile, OSError) as e:
            raise DescribeFailed(f"no preview ({type(e).__name__})")
    with open(dest, "wb") as f:
        f.write(data)
    return str(dest)


def _read_text(path: str, limit: int) -> str:
    """The first `limit` characters of a text file, read no further than needed. A read that stopped inside a line is cut
    at the last line end, so a multibyte character is never split."""
    cap = limit * 4 + 16
    with open(path, "rb") as f:
        raw = f.read(cap)
    if len(raw) == cap:
        cut = raw.rfind(b"\n")
        if cut > 0:
            raw = raw[:cut]
    return decode_bytes(raw, limit)


def describe_doc(path: str, ext: str, cfg: dict) -> dict:
    """Text of a document, read in Python with the extract readers, at most max_text_kb of it."""
    limit = int(cfg["max_text_kb"]) * 1024
    title = ""
    if ext == "docx":
        text = read_docx_zip(Path(path))
    elif ext == "odt":
        t, _pages, sections = read_opendocument(Path(path), ext)
        title = t or ""
        text = "\n".join(s["text"] for s in sections)
    elif ext in ("rtf", "doc", "wordml", "webarchive"):
        text = read_textutil(Path(path))
    elif ext == "eml":
        subject, sections = read_eml(Path(path))
        title = subject or ""
        text = "\n".join(s["text"] for s in sections)
    elif ext in ("html", "htm", "xhtml"):
        text = html_to_text(_read_text(path, limit * 4))
    else:
        text = _read_text(path, limit)
        if ext in ("md", "markdown"):
            heads = [m.group(1).strip() for m in re.finditer(r"^#{1,6}\s+(.+)$", text, re.M)]
            title = heads[0] if heads else ""
    text = text[:limit]
    return {"text": text, "title": title[:200], "labels": [], "lang": _lang(text), "lines": 0, "ocr": False}


def describe_media(helper: HelperSession, path: str, dkind: str, ext: str, cfg: dict) -> dict:
    """An image, a PDF, a video, or a .pages preview, through the helper. Returns the descriptor fields."""
    target, want = path, dkind
    if ext == "pages":
        target, want = _pages_preview(path), "pdf"
    payload = {"id": 0, "path": target, "kind": want}
    if want == "image" and not cfg["ocr_images"]:
        payload["ocr"] = "off"
    res = helper.call(payload)
    if not res.get("ok"):
        raise DescribeFailed(str(res.get("error") or "the describer failed"))
    limit = int(cfg["max_text_kb"]) * 1024
    labels = [(str(x.get("id", "")).strip(), float(x.get("c", 0))) for x in res.get("labels") or [] if x.get("id")]
    text = str(res.get("text") or "")[:limit]
    return {"text": text, "title": str(res.get("title") or "")[:200], "labels": labels,
            "lang": str(res.get("lang") or "") or _lang(text), "lines": int(res.get("lines") or 0),
            "ocr": bool(res.get("ocr"))}


def label_words(label_ids: list[str]) -> str:
    """The labels as the FTS column holds them: each label, then the German and English words of its concept, so a query
    in either language matches without any expansion."""
    out: list[str] = []
    seen: set[str] = set()
    for lid in label_ids:
        for w in [fold(lid)] + sorted(_CONCEPT_OF.get(fold(lid), frozenset())):
            if w and w not in seen:
                seen.add(w)
                out.append(w)
    return " ".join(out)


def _snippet(text: str, labels: list) -> str:
    base = " ".join(text.split())
    if not base:
        base = ", ".join(lid for lid, _c in labels[:8])
    return base[:SNIPPET_CHARS]


# ---------------------------------------------------------------- the manager

class Manager:
    """Owns the database, the change tracking and the describer thread. `start()` runs the thread (the service does, once).
    The search and the status work without it; the tests drive `sync()` and `describe_pending()` directly.

    `describer(path, dkind, ext, cfg) -> descriptor dict` replaces the helper for the tests; `gate(cfg) -> (ok, reason)`
    replaces the power and idle gate. Both default to the real ones."""

    def __init__(self, db: Path | None = None, describer=None, gate=None):
        self.store = Store(db or db_path())
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._rescan = False
        self.state = "idle"
        self.paused_reason: str | None = None
        self.last_error: str | None = None
        self.last_sync: float | None = None
        self.last_reconcile: float | None = None
        self.ocr_mode = "gate"                          # "gate" | "always" | "off": the helper's --ocr (measurements set it)
        self._done_at: collections.deque = collections.deque(maxlen=4000)
        self._counts_cache: tuple = (0.0, None)
        self._extra_cache: tuple = (None, None)
        self._stale_lock = threading.Lock()
        self._stale: set[int] = set()                    # rows a search found gone, not yet removed (see _remove_later)
        self._describer = describer
        self._gate_fn = gate

    # ---- lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="wholedisk")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def rescan(self) -> dict:
        """A full merge at the next turn, and another try for the failed files that have tries left."""
        self._rescan = True
        self._wake.set()
        return {"started": True}

    def _log(self, msg: str) -> None:
        try:
            paths.LOGS.mkdir(parents=True, exist_ok=True)
            with open(paths.LOGS / "daemon.log", "a") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} wholedisk: {msg}\n")
        except OSError:
            pass

    # ---- the gate
    def gate(self, cfg: dict) -> tuple[bool, str | None]:
        if self._gate_fn is not None:
            return self._gate_fn(cfg)
        return heavy_work_allowed(cfg["priority"], enabled=bool(cfg["enabled"]))

    # ---- change tracking
    def _scope(self, cfg: dict, ni: _NameIndex) -> Scope:
        prune = (config.load().get("indexing") or {}).get("prune_dirs")
        return Scope(cfg, macindex.manager().excluder(), ni.has_row, prune_names=prune)

    def sync(self, full: bool = False, rescan: bool = False) -> dict:
        """Bring the index in line with the name index: the fast look, or the full merge (also after a settings change,
        and on the first run). Returns the counts, or why it did nothing."""
        cfg = settings()
        if not _name_index_ready():
            return {"skipped": "the name index is not built yet"}
        if macindex.manager().crawling:
            return {"skipped": "the name index is being crawled"}
        ni = _NameIndex()
        try:
            if full or rescan or self.store.meta("last_src_id") is None \
                    or self.store.meta("settings") != _settings_key(cfg):
                return self._reconcile(cfg, ni, rescan=rescan)
            return self._fast(cfg, ni)
        finally:
            ni.close()

    def _fast(self, cfg: dict, ni: _NameIndex) -> dict:
        last_id = int(self.store.meta("last_src_id") or 0)
        since = int(time.time()) - RECENT_S
        scope = self._scope(cfg, ni)
        rows = ni.c.execute(
            "SELECT id, path, is_dir, size, mtime FROM files "
            "WHERE (id > ? OR mtime >= ?) AND (is_dir = 0 OR (is_dir = 1 AND path LIKE '%.pages')) ORDER BY id",
            (last_id, since)).fetchall()
        top, changed = last_id, 0
        with self.store.lock:
            c = self.store.writer()
            c.execute("BEGIN")
            try:
                for rid, path, is_dir, size, mtime in rows:
                    top = max(top, rid)
                    cand = _candidate(path, bool(is_dir), size, mtime, scope, cfg)
                    if cand is not None:
                        changed += self._apply(c, cand, cfg)
                self.store.set_meta(c, "last_src_id", top)
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        self.last_sync = time.time()
        self._counts_cache = (0.0, None)
        return {"looked_at": len(rows), "changed": changed}

    def _apply(self, c, cand: dict, cfg: dict) -> int:
        """Insert or update one entry. Returns 1 when something changed, 0 otherwise. A changed file keeps its old
        description (still searchable) until the new one is made."""
        row = c.execute("SELECT id, name, folder, ext, kind, dkind, size, mtime, prio, state, desc_key, labels "
                        "FROM files WHERE path = ?", (cand["path"],)).fetchone()
        if row is None:
            state = ST_PENDING if cand["dkind"] else ST_NAME
            c.execute("INSERT INTO files(path, name, folder, ext, kind, dkind, size, mtime, prio, state) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (cand["path"], cand["name"], cand["folder"], cand["ext"], cand["kind"], cand["dkind"],
                       cand["size"], cand["mtime"], cand["prio"], state))
            rid = c.execute("SELECT id FROM files WHERE path = ?", (cand["path"],)).fetchone()[0]
            _fts_set(c, rid, cand["name"], cand["folder"], "", "")
            return 1
        rid, name, folder, ext, kind, dkind, size, mtime, prio, state, desc_key, labels_json = row
        meta_same = (name, folder, ext, kind, dkind, size, mtime, prio) == (
            cand["name"], cand["folder"], cand["ext"], cand["kind"], cand["dkind"], cand["size"], cand["mtime"],
            cand["prio"])
        if cand["dkind"] is None:
            if meta_same and state == ST_NAME:
                return 0
            c.execute("UPDATE files SET name=?, folder=?, ext=?, kind=?, dkind=NULL, size=?, mtime=?, prio=?, state=?, "
                      "fails=0, fail_key=NULL, desc_key=NULL, labels='[]', snippet='', text='', title='', lang='' "
                      "WHERE id=?",
                      (cand["name"], cand["folder"], cand["ext"], cand["kind"], cand["size"], cand["mtime"],
                       cand["prio"], ST_NAME, rid))
            _fts_set(c, rid, cand["name"], cand["folder"], "", "")
            return 1
        key = _desc_key(cand["size"], cand["mtime"], cfg)
        if meta_same and (state != ST_DONE or desc_key == key):
            return 0                       # not changed: a pending or failed file keeps its place and its count
        keeps = desc_key == key and state == ST_DONE
        new_state = ST_DONE if keeps else ST_PENDING
        c.execute("UPDATE files SET name=?, folder=?, ext=?, kind=?, dkind=?, size=?, mtime=?, prio=?, state=? "
                  "WHERE id=?",
                  (cand["name"], cand["folder"], cand["ext"], cand["kind"], cand["dkind"], cand["size"],
                   cand["mtime"], cand["prio"], new_state, rid))
        if not keeps:
            c.execute("UPDATE files SET fails=0, fail_key=NULL WHERE id=?", (rid,))
        if (name, folder) != (cand["name"], cand["folder"]):
            old_ids = [x[0] for x in json.loads(labels_json or "[]")]
            text = c.execute("SELECT text FROM files WHERE id = ?", (rid,)).fetchone()[0]
            _fts_set(c, rid, cand["name"], cand["folder"], label_words(old_ids), text)
        return 1

    def _reconcile(self, cfg: dict, ni: _NameIndex, rescan: bool = False) -> dict:
        """The full merge in path order: adds, updates, and deletes what the rules now exclude or the name index no longer
        lists. The name index's rowid high-water mark and the settings key are stored at the end."""
        scope = self._scope(cfg, ni)
        # Both sides are read in path order through their indexes and merged row by row: neither list is held in memory
        # (a two-million-entry name index would be hundreds of megabytes). The read of this index is a snapshot (WAL):
        # the writes below do not move it.
        src = ni.c.execute("SELECT path, id, is_dir, size, mtime FROM files "
                           "WHERE is_dir = 0 OR (is_dir = 1 AND path LIKE '%.pages') ORDER BY path")
        wdc = self.store.reader()
        wd = iter(wdc.execute("SELECT path, id FROM files ORDER BY path")) if wdc is not None else iter(())
        added = changed = removed = 0
        top = 0
        gone: list[int] = []
        try:
            cur = next(wd, None)
            with self.store.lock:
                w = self.store.writer()
                w.execute("BEGIN")
                try:
                    n = 0
                    for path, rid, is_dir, size, mtime in src:
                        n += 1
                        top = max(top, rid)
                        while cur is not None and cur[0] < path:
                            gone.append(cur[1])
                            cur = next(wd, None)
                        cand = _candidate(path, bool(is_dir), size, mtime, scope, cfg)
                        if cur is not None and cur[0] == path:
                            if cand is None:
                                gone.append(cur[1])
                            else:
                                changed += self._apply(w, cand, cfg)
                            cur = next(wd, None)
                        elif cand is not None:
                            added += self._apply(w, cand, cfg)
                        if n % 2000 == 0:
                            _delete_ids(w, gone)
                            removed += len(gone)
                            gone = []
                            w.execute("COMMIT")
                            w.execute("BEGIN")
                    while cur is not None:
                        gone.append(cur[1])
                        cur = next(wd, None)
                    _delete_ids(w, gone)
                    removed += len(gone)
                    self._requeue_failed(w, cfg, rescan)
                    self.store.set_meta(w, "last_src_id", top)
                    self.store.set_meta(w, "settings", _settings_key(cfg))
                    w.execute("COMMIT")
                except Exception:
                    w.execute("ROLLBACK")
                    raise
                self._counts_cache = (0.0, None)
        finally:
            if wdc is not None:
                wdc.close()
        self.last_reconcile = self.last_sync = time.time()
        return {"added": added, "changed": changed, "removed": removed, "full": True}

    def _requeue_failed(self, c, cfg: dict, rescan: bool) -> None:
        """A failed file is tried again when its size or mtime changed, or after a rescan while it has tries left."""
        rows = c.execute("SELECT id, size, mtime, fails, fail_key FROM files WHERE state = ?", (ST_FAILED,)).fetchall()
        for rid, size, mtime, fails, fail_key in rows:
            if fail_key != _desc_key(size, mtime, cfg) or (rescan and fails < MAX_FAILS):
                c.execute("UPDATE files SET state = ? WHERE id = ?", (ST_PENDING, rid))

    # ---- describing
    def _pick(self, n: int, media: bool = True) -> list[tuple]:
        """The next pending entries. media=False (no helper): only the documents Python reads itself; pictures, PDFs,
        videos and .pages previews wait."""
        now = int(time.time())
        with self.store.lock:
            return self.store.writer().execute(
                "SELECT id, path, kind, dkind, ext, size, mtime FROM files "
                "WHERE state = ? AND dkind IS NOT NULL AND mtime <= ? AND (? OR (dkind = 'doc' AND ext != 'pages')) "
                "ORDER BY prio DESC, mtime DESC LIMIT ?",
                (ST_PENDING, now - MIN_AGE_S, 1 if media else 0, n)).fetchall()

    def media_ok(self) -> bool:
        """Pictures, PDFs and videos can be described: a stand-in describer is set, or the helper binary exists."""
        return self._describer is not None or os.path.exists(macindex.helper_path())

    def pending(self) -> int:
        with self.store.lock:
            return int(self.store.writer().execute("SELECT count(*) FROM files WHERE state = ?",
                                                   (ST_PENDING,)).fetchone()[0])

    def describe_pending(self, cfg: dict | None = None, limit: int | None = None, gate: bool = True) -> dict:
        """Describe the pending entries batch by batch: until none is left, the gate closes, or `limit` files are done."""
        cfg = cfg or settings()
        done = failed = 0
        reason = None
        media = self.media_ok()
        if not media:
            self.last_error = "the helper (msehelper) is not installed: pictures, PDFs and videos wait, documents are read"
        while limit is None or done + failed < limit:
            batch = self._pick(BATCH, media=media)
            if not batch:
                break
            progress = 0                     # a batch that changes nothing (every file skipped) ends the run: no spinning
            helper = None
            if self._describer is None:
                helper = HelperSession(str(macindex.helper_path()), self._helper_opts(cfg))
            try:
                for row in batch:
                    if limit is not None and done + failed >= limit:
                        break
                    if self._stop.is_set():                           # the service is stopping: between two files
                        return {"described": done, "failed": failed, "paused_reason": reason}
                    if gate:
                        # the switch is read live: turned off while a batch runs, the describer stops within one file
                        # (cfg is the batch's snapshot, so the gate alone would not see it)
                        if not settings()["enabled"]:
                            return {"described": done, "failed": failed, "paused_reason": "disabled"}
                        ok, reason = self.gate(cfg)
                        if not ok:
                            return {"described": done, "failed": failed, "paused_reason": reason}
                    outcome = self._describe_one(row, cfg, helper)
                    if outcome == "ok":
                        done += 1
                        self._done_at.append(time.time())
                    elif outcome == "failed":
                        failed += 1
                    if outcome != "skipped":
                        progress += 1
            finally:
                if helper is not None:
                    helper.close()
            if progress == 0:
                break
        return {"described": done, "failed": failed, "paused_reason": reason}

    def _helper_opts(self, cfg: dict) -> list[str]:
        ocr = "off" if not cfg["ocr_images"] else self.ocr_mode
        # background QoS runs about three times slower than default; `balanced` was chosen to get through, so it runs at
        # utility; `low` stays at background and gives way to everything
        return ["--max-px", "512", "--text-px", "1280", "--pdf-px", "1200", "--ocr", ocr,
                "--qos", HELPER_QOS.get(cfg.get("priority"), "background"), "--nice", "10"]

    def _describe_one(self, row: tuple, cfg: dict, helper: HelperSession | None) -> str:
        rid, path, kind, dkind, ext, size, mtime = row
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            self._remove([rid])
            return "skipped"
        except OSError as e:
            return self._fail(rid, size, mtime, cfg, f"cannot stat: {e.strerror}")
        if S.S_ISLNK(st.st_mode):
            self._set_name_only(rid)                          # a link is never followed
            return "skipped"
        is_dir = S.S_ISDIR(st.st_mode)                        # a .pages package: its size is not a file size
        if (not is_dir and int(st.st_size) != int(size)) or int(st.st_mtime) != int(mtime):
            return "skipped"                                  # changed since the look: the next sync takes it
        if st.st_flags & DATALESS:
            return self._fail(rid, size, mtime, cfg, "dataless (an iCloud placeholder): not read")
        if self._visible(settings())(path):
            self._remove([rid])                               # excluded since the look (a rule, or extra_excludes): never read
            return "skipped"
        try:
            if dkind == "doc" and ext != "pages":
                desc = describe_doc(path, ext, cfg)                # text and office documents: read in Python
            elif self._describer is not None:
                desc = self._describer(path, dkind, ext, cfg)      # tests: stands in for the helper
            else:
                own = helper if helper is not None else HelperSession(str(macindex.helper_path()), self._helper_opts(cfg))
                try:
                    desc = describe_media(own, path, dkind, ext, cfg)
                finally:
                    if helper is None:
                        own.close()
        except (DescribeFailed, OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError,
                zipfile.BadZipFile, UnicodeError) as e:
            return self._fail(rid, size, mtime, cfg, f"{type(e).__name__}: {str(e)[:200]}")
        self._store_desc(rid, desc, size, mtime, cfg)
        return "ok"

    def _store_desc(self, rid: int, desc: dict, size: int, mtime: int, cfg: dict) -> None:
        labels_json = json.dumps([[lid, round(c, 3)] for lid, c in desc["labels"]], ensure_ascii=False)
        words = label_words([lid for lid, _c in desc["labels"]])
        searchable = desc["text"] + (" " + desc["title"] if desc["title"] else "")
        with self.store.lock:
            c = self.store.writer()
            c.execute("BEGIN")
            try:
                c.execute("UPDATE files SET state=?, fails=0, fail_key=NULL, desc_key=?, labels=?, snippet=?, text=?, "
                          "title=?, lang=? WHERE id=?",
                          (ST_DONE, _desc_key(size, mtime, cfg), labels_json, _snippet(desc["text"], desc["labels"]),
                           desc["text"], desc["title"], desc["lang"], rid))
                name, folder = c.execute("SELECT name, folder FROM files WHERE id = ?", (rid,)).fetchone()
                _fts_set(c, rid, name, folder, words, searchable)
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    def _fail(self, rid: int, size: int, mtime: int, cfg: dict, why: str) -> str:
        with self.store.lock:
            c = self.store.writer()
            c.execute("UPDATE files SET state=?, fails=fails+1, fail_key=? WHERE id=?",
                      (ST_FAILED, _desc_key(size, mtime, cfg), rid))
        self.last_error = why
        return "failed"

    def _set_name_only(self, rid: int) -> None:
        with self.store.lock:
            c = self.store.writer()
            c.execute("BEGIN")
            try:
                c.execute("UPDATE files SET state=?, dkind=NULL, labels='[]', snippet='', text='', title='', lang='' "
                          "WHERE id=?", (ST_NAME, rid))
                name, folder = c.execute("SELECT name, folder FROM files WHERE id = ?", (rid,)).fetchone()
                _fts_set(c, rid, name, folder, "", "")
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    def _remove(self, ids: list[int]) -> None:
        with self.store.lock:
            c = self.store.writer()
            c.execute("BEGIN")
            try:
                _delete_ids(c, ids)
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    def _remove_later(self, ids: list[int]) -> None:
        """Rows a search found gone or newly excluded. Removed now when the writer is free, otherwise at the thread's next
        turn: a search never waits behind a merge, which holds the writer for its whole run."""
        with self._stale_lock:
            if len(self._stale) < STALE_MAX:
                self._stale.update(ids)
        self._drain_stale(blocking=False)

    def _drain_stale(self, blocking: bool = True) -> None:
        if not self._stale or not self.store.lock.acquire(blocking=blocking):
            return
        try:
            with self._stale_lock:
                ids, self._stale = list(self._stale), set()
            if ids:
                self._remove(ids)
        finally:
            self.store.lock.release()

    # ---- the thread
    def _loop(self) -> None:
        next_fast = next_full = 0.0
        while not self._stop.is_set():
            try:
                self._drain_stale()                          # what the searches found gone since the last turn
                cfg = settings()
                if not cfg["enabled"]:
                    self.state, self.paused_reason = "paused", "disabled"
                    self._wait(5)
                    continue
                if not _name_index_ready():
                    self.state = "scanning"
                    self._wait(5)
                    continue
                now = time.time()
                if self._rescan or now >= next_full:
                    rescan, self._rescan = self._rescan, False
                    self.state = "scanning"
                    self.sync(full=True, rescan=rescan)
                    next_full = time.time() + FULL_SYNC_S
                    next_fast = time.time() + FAST_SYNC_S    # the minute looks go on after a merge (not an hour later)
                    continue
                if now >= next_fast:
                    self.state = "scanning"
                    self.sync()
                    next_fast = time.time() + FAST_SYNC_S
                    continue
                ok, reason = self.gate(cfg)
                if not ok:
                    self.paused_reason = reason
                    self.state = "paused" if self.pending() else "idle"
                    self._wait(GATE_POLL_S)
                    continue
                self.paused_reason = None
                if not self._pick(1):
                    self.state = "idle"
                    self._wait(min(15.0, max(1.0, next_fast - time.time())))
                    continue
                self.state = "describing"
                self.describe_pending(cfg, limit=BATCH)
                self._wait(PAUSE_BETWEEN_BATCHES_S.get(cfg["priority"], 0.5))
            except Exception:
                self.last_error = traceback.format_exc().splitlines()[-1]
                self._log(traceback.format_exc())
                self._wait(10)

    def _wait(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    # ---- status
    def counts(self) -> dict:
        ts, val = self._counts_cache
        if val is not None and time.time() - ts < 5:
            return val
        c = self.store.reader()                 # its own read-only connection: a merge in progress does not hold a status up
        if c is None:
            return dict(self._EMPTY)
        try:
            rows = dict(c.execute("SELECT state, count(*) FROM files GROUP BY state").fetchall())
            described = lambda sql: c.execute(sql, (ST_DONE,)).fetchone()[0]          # noqa: E731
            val = {"files_seen": sum(rows.values()),
                   "files_described": rows.get(ST_DONE, 0),
                   "images_labelled": described("SELECT count(*) FROM files WHERE state = ? AND dkind = 'image'"),
                   "pdfs_described": described("SELECT count(*) FROM files WHERE state = ? AND kind = 'pdf'"),
                   "docs_described": described("SELECT count(*) FROM files WHERE state = ? AND dkind = 'doc'"),
                   "videos_described": described("SELECT count(*) FROM files WHERE state = ? AND dkind = 'video'"),
                   "pending": rows.get(ST_PENDING, 0), "failed": rows.get(ST_FAILED, 0)}
        finally:
            c.close()
        self._counts_cache = (time.time(), val)
        return val

    def rate(self) -> float:
        now = time.time()
        while self._done_at and now - self._done_at[0] > 120:
            self._done_at.popleft()
        if len(self._done_at) < 2:
            return 0.0
        return round(len(self._done_at) / max(1.0, now - self._done_at[0]), 2)

    _EMPTY = {"files_seen": 0, "files_described": 0, "images_labelled": 0, "pdfs_described": 0, "docs_described": 0,
              "videos_described": 0, "pending": 0, "failed": 0}

    def status(self) -> dict:
        cfg = settings()
        if not cfg["enabled"]:
            state, reason = "paused", "disabled"
        else:
            state, reason = self.state, self.paused_reason
        try:
            counts = self.counts() if self.store.path.exists() else dict(self._EMPTY)
        except sqlite3.Error as e:
            counts = dict(self._EMPTY)
            self.last_error = str(e)
        pending = counts["pending"]
        rate = self.rate()
        return {"enabled": bool(cfg["enabled"]), "roots": list(cfg["roots"]),
                "files_seen": counts["files_seen"], "files_described": counts["files_described"],
                "images_labelled": counts["images_labelled"], "pdfs_described": counts["pdfs_described"],
                "docs_described": counts["docs_described"], "videos_described": counts["videos_described"],
                "failed": counts["failed"], "pending": pending, "state": state,
                "paused_reason": reason if state == "paused" else None,
                "rate_per_s": rate, "eta_s": round(pending / rate) if rate > 0 and pending else None,
                "last_error": self.last_error, "priority": cfg["priority"],
                "last_sync": self.last_sync, "last_reconcile": self.last_reconcile,
                "name_index_ready": _name_index_ready()}

    # ---- search
    def _visible(self, cfg: dict):
        """A predicate: True when the privacy rules or extra_excludes block the path now."""
        key = tuple(cfg.get("extra_excludes") or [])
        if self._extra_cache[0] != key:
            extra = macindex.Excluder({"exclude": list(key)}, h=macindex.home()) if key else None
            self._extra_cache = (key, extra)
        base = macindex.manager().excluder()
        extra = self._extra_cache[1]
        return lambda p: base.blocked(p) or bool(extra and extra.blocked(p))

    def search(self, q: str, kind: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        t0 = time.perf_counter()
        if kind is not None and kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        limit = max(1, min(100, int(limit)))
        offset = max(0, int(offset))
        need = offset + limit + 1
        parsed = parse(q)
        kinds = (kind,) if kind else tuple(sorted(parsed.kinds))
        conn = self.store.reader()
        if conn is None:
            return {"results": [], "has_more": False, "next_offset": None, "took_ms": 0.0, "query": parsed.public()}
        visible = self._visible(settings())
        verified: list[tuple] = []
        stale: list[int] = []
        try:
            if not parsed.groups:
                if not kinds:
                    return {"results": [], "has_more": False, "next_offset": None,
                            "took_ms": round((time.perf_counter() - t0) * 1000, 1), "query": parsed.public()}
                marks = ",".join("?" * len(kinds))
                scored = [(r[0], 0.0) for r in conn.execute(
                    f"SELECT id FROM files WHERE kind IN ({marks}) ORDER BY mtime DESC LIMIT ?",
                    (*kinds, need * 4 + 20)).fetchall()]
            else:
                scored = self._candidates(conn, parsed.groups, kinds, need)
            fetched = self._rows(conn, [i for i, _s in scored])
            for rid, score in scored:
                if len(verified) >= need:
                    break
                row = fetched.get(rid)
                if row is None:
                    continue
                if visible(row["path"]) or not os.path.lexists(row["path"]):
                    stale.append(rid)
                    continue
                verified.append((row, score))
            page = verified[offset:offset + limit]
            texts = self._texts(conn, [row["id"] for row, _s in page])   # the text of the page's rows only
        finally:
            conn.close()
        if stale:
            self._remove_later(stale)
        has_more = len(verified) > offset + limit
        return {"results": [self._result(row, score, parsed, texts.get(row["id"], "")) for row, score in page],
                "has_more": has_more, "next_offset": offset + limit if has_more else None,
                "took_ms": round((time.perf_counter() - t0) * 1000, 1), "query": parsed.public()}

    def _candidates(self, conn, groups: list, kinds: tuple, need: int) -> list[tuple[int, float]]:
        """The rows worth verifying, best first. First the rows matching every concept, ranked by BM25 and cut at a pool a
        little larger than the page (verification drops stale rows). Only while that leaves the page short, the rows
        matching one concept fill it, rarest concept first: a rare word is cheap to rank and a strong match, the commonest
        word is ranked last and only when still needed. Every query is `ORDER BY bm25 LIMIT`: no query materialises more
        than its cap, and the rows of the whole OR set are never ranked at once."""
        pool = min(MAX_POOL, need * 4 + 20)
        scored = self._fts(conn, _match_expr(groups, "and"), kinds, pool)
        if len(scored) >= need or len(groups) < 2:
            return scored
        seen = {i for i, _s in scored}
        cap = need + OR_FILL_EXTRA
        for g in sorted(groups, key=lambda g: self._doc_freq(conn, g)):
            for rid, score in self._fts(conn, _match_expr([g], "or"), kinds, cap):
                if rid not in seen:
                    seen.add(rid)
                    scored.append((rid, score))
            if len(scored) >= need:
                break
        return scored

    def _doc_freq(self, conn, group: tuple) -> int:
        """How many rows carry any word of the concept (as a prefix, like the match), from the fts5vocab table: one b-tree
        range read per word. Without the table (an index made before it existed) every concept counts the same."""
        total = 0
        try:
            for a in group:
                hi = a[:-1] + chr(ord(a[-1]) + 1)
                total += int(conn.execute("SELECT coalesce(sum(doc), 0) FROM wd_vocab WHERE term >= ? AND term < ?",
                                          (a, hi)).fetchone()[0])
        except (sqlite3.OperationalError, ValueError):
            return 1 << 30
        return total

    def _fts(self, conn, expr: str, kinds: tuple, pool: int) -> list[tuple[int, float]]:
        try:
            if kinds:
                marks = ",".join("?" * len(kinds))
                rows = conn.execute(
                    f"SELECT f.id, bm25(wd_fts, {BM25_WEIGHTS}) AS r FROM wd_fts JOIN files f ON f.id = wd_fts.rowid "
                    f"WHERE wd_fts MATCH ? AND f.kind IN ({marks}) ORDER BY r LIMIT ?", (expr, *kinds, pool)).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT rowid, bm25(wd_fts, {BM25_WEIGHTS}) AS r FROM wd_fts WHERE wd_fts MATCH ? "
                    f"ORDER BY r LIMIT ?", (expr, pool)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(int(i), -float(r)) for i, r in rows]

    def _rows(self, conn, ids: list[int]) -> dict[int, dict]:
        """The candidates' rows without their text (up to max_text_kb each): the text is read for the page's rows only."""
        out: dict[int, dict] = {}
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            if not chunk:
                break
            cur = conn.execute("SELECT id, path, name, kind, size, mtime, labels, snippet FROM files "
                               f"WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            cols = [d[0] for d in cur.description]
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                out[d["id"]] = d
        return out

    def _texts(self, conn, ids: list[int]) -> dict[int, str]:
        if not ids:
            return {}
        return {int(i): t or "" for i, t in conn.execute(
            f"SELECT id, text FROM files WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()}

    def _result(self, row: dict, score: float, parsed: Parsed, text: str = "") -> dict:
        try:
            labels = sorted(((str(a), float(b)) for a, b in json.loads(row["labels"] or "[]")), key=lambda x: -x[1])
        except (ValueError, TypeError):
            labels = []
        why, matched_conf = "name", 0.0
        if parsed.groups:
            label_toks = _tokens(" ".join(lid for lid, _c in labels))
            name_toks = _tokens(row["name"])
            text_toks = _tokens(text)[:4000]
            if any(_starts(g, label_toks) for g in parsed.groups):
                why = "label"
                matched_conf = max((c for lid, c in labels if any(_starts(g, _tokens(lid)) for g in parsed.groups)),
                                   default=0.0)
            elif any(_starts(g, name_toks) for g in parsed.groups):
                why = "name"
            elif any(_starts(g, text_toks) for g in parsed.groups):
                why = "text"
        snippet = row["snippet"] or ""
        if why == "text":
            snippet = _window(text, parsed.groups) or snippet
        return {"path": row["path"], "name": row["name"], "kind": row["kind"], "size": row["size"],
                "mtime": row["mtime"], "snippet": snippet, "labels": [lid for lid, _c in labels][:RESULT_LABELS],
                "score": round(score + 0.01 * matched_conf, 4), "why": why}


def _window(text: str, groups: list) -> str:
    """The text around the first place a query word occurs, plain text of at most SNIPPET_CHARS."""
    low = text.casefold()
    best = None
    for g in groups:
        for a in g:
            i = low.find(a)
            if i >= 0 and (best is None or i < best):
                best = i
    if best is None:
        return ""
    start = max(0, best - 100)
    return " ".join(text[start:start + SNIPPET_CHARS].split())


_MANAGER: Manager | None = None
_MLOCK = threading.Lock()


def manager() -> Manager:
    global _MANAGER
    with _MLOCK:
        if _MANAGER is None or _MANAGER.store.path != db_path():
            _MANAGER = Manager()
        return _MANAGER
