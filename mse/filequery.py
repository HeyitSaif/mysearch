"""File-name search over the whole-Mac index (mse/macindex.py): the query language, the SQL plan and the ranking.

The language is fsearch's, plus a few additions, plus some of fff's matching rules (https://github.com/dmtrKovalenko/fff,
MIT; ideas only, no code):

    invoice acme           every word must be in the name; words are fuzzy (substring; a typo is forgiven, see below)
    'exact                 the exact text, no typo tolerance          "two words"   the same, with a space
    ^prefix  suffix$       the name starts / ends with it             ^whole$       the whole name
    !word                  the name must not contain it               !ext:tmp  !type:image  !path:cache
    *.pdf  report-*.docx   a glob on the whole name (`*` any run, `?` one character, once a `*` is there); `*.pdf` is
                           ext:pdf; globs ignore case. A token with no `*`, or one quoted, 'exact or anchored with ^ / $,
                           is plain text (`a[1]` and `{x}` are always plain text)
    **/invoices/*          a glob with a `/` is matched against the whole path: `*` and `?` stay inside one folder name,
    src/**/*.py            `**/` is any number of folders (also none), a pattern may start at any folder unless it starts
    ~/Work/**              with `/` or `~`, and a trailing `/**` means everything below that folder
    !*.log  !**/old/**     a glob with a bang excludes what it matches (`!*.log` is !ext:log)
    ext:pdf,docx           extension(s)                               type:image    kind:folder|file|image|pdf|doc|
                                                                      sheet|slides|audio|video|code|archive|app|email|font
    size:>5mb  size:<1kb   size:1mb..2gb  size:=0   (1 KB = 1024 bytes; files only)
    mtime:<7d              modified in the last 7 days (also 5h 30min 2w 3mo 1y)
    mtime:>30d             modified more than 30 days ago
    mtime:>2026-01-01      on or after that day  (<2026-01-01: before that day; 2025-03 = that month; 2026 = that year)
    mtime:2026-01..2026-03 a range of days or months
    path:work/notes        the full path contains it                  docs/readme   = path:docs/ + the name readme
    re:^IMG_\\d+            regular expression on the name (case-insensitive)
    in:~/Documents         under that folder ("/" or "~" first; otherwise in: names a MySearch folder or group)
    limit:50               at most 50 results

Smart case: a word typed with a capital AND a small letter (`Report`, `iPhone`, `Café`) is matched with its case; a word in
small letters (`report`) or all capitals (`PDF`, `README`) matches any case. The all-capitals exception is deliberate: a
shouted word is a caps-lock accident or an acronym, and either way the file is named in whatever case it has. A word that
is matched with its case gets no typo tolerance. The setting mac_index.smart_case turns the rule off.

Typo tolerance: a word of 5 or more letters finds names that contain a word one edit away (insertion, deletion,
substitution or swapped neighbours: `recieve` finds `receive`). A word of 4 letters forgives swapped neighbours only
(`mian` finds `main`). Shorter words are literal. Typos are looked for only when the literal search finds fewer
results than asked for, and those results rank last.

Ranking: exact name (with or without extension) > prefix > word boundary > substring > typo; ties by how much of the
name the words cover, recency and folder depth; folders get a small boost on exact matches. As in fff, a match in the file
name is worth more than a match elsewhere: the whole file name is the query +40 %, the query is inside the file name +16 %
(at most 30 points); both are increasing in the score, so they never reorder name matches among themselves. With
mac_index.match_folder_names on, a word that is not in a name may be in the folder path when the names alone come up short
(two or more words); such a row ranks below every name match and gets none of the bonuses. Files you opened from the
results rank a little higher, and first for exactly the query you opened them for (mse/frecency.py). With
mac_index.respect_gitignore on, what the .gitignore / .ignore files of a git repository ignore is left out
(mse/gitignore.py).

A pattern with no literal text in it (`re:^.{3}$`, `??*.t?t`) cannot ask the name index anything: the names are read in
filter order and tested here, until enough match or mac_index.regex_budget_ms (150) is used up, and the answer then says
`partial`. Such a scan returns the first matches it finds in that order, not the best-scored of everything.
"""

import math
import re
import sqlite3
import time
import unicodedata
from datetime import date, datetime, timedelta

from . import gitignore as _gi
from .textutil import fold, fold_map

KINDS = ["folder", "image", "pdf", "doc", "sheet", "slides", "audio", "video", "code", "archive", "app", "email",
         "font", "other"]
KIND_ID = {k: i for i, k in enumerate(KINDS)}
_EXT_KIND = {
    "image": "jpg jpeg png gif heic heif webp tif tiff bmp svg raw cr2 cr3 nef arw dng psd ai ico icns avif",
    "pdf": "pdf",
    "doc": "doc docx odt rtf pages txt md markdown tex epub mobi log",
    "sheet": "xls xlsx xlsm xlsb csv tsv ods numbers",
    "slides": "ppt pptx pps ppsx key odp",
    "audio": "mp3 m4a wav aac flac aif aiff ogg opus wma caf",
    "video": "mp4 mov mkv avi webm m4v wmv flv mpg mpeg",
    "code": "py js mjs cjs ts tsx jsx swift java kt kts c h cc cpp cxx hpp m mm rs go rb php sh zsh bash fish json "
            "yaml yml toml xml html htm css scss sass less sql ipynb lua pl r cs dart vue svelte gradle make "
            "cmake ini conf plist",
    "archive": "zip tar gz tgz bz2 xz 7z rar dmg iso pkg zst",
    "app": "app",
    "email": "eml emlx msg mbox",
    "font": "ttf otf woff woff2 ttc",
}
EXT_KIND = {e: k for k, exts in _EXT_KIND.items() for e in exts.split()}
PACKAGE_EXTS = {"app", "framework", "bundle", "photoslibrary", "musiclibrary", "tvlibrary", "imovielibrary",
                "fcpbundle", "xcarchive", "plugin", "kext", "prefpane", "qlgenerator", "saver", "appex", "xpc"}

TYPE_WORDS = {
    "folder": "folder", "folders": "folder", "dir": "folder", "dirs": "folder", "directory": "folder",
    "file": "file", "files": "file",
    "image": "image", "images": "image", "img": "image", "photo": "image", "photos": "image", "picture": "image",
    "pdf": "pdf", "pdfs": "pdf",
    "doc": "doc", "docs": "doc", "document": "doc", "documents": "doc", "text": "doc", "word": "doc",
    "sheet": "sheet", "sheets": "sheet", "spreadsheet": "sheet", "excel": "sheet",
    "slide": "slides", "slides": "slides", "presentation": "slides", "deck": "slides",
    "audio": "audio", "music": "audio", "sound": "audio",
    "video": "video", "videos": "video", "movie": "video",
    "code": "code", "source": "code", "script": "code",
    "archive": "archive", "archives": "archive", "zip": "archive",
    "app": "app", "apps": "app", "application": "app",
    "email": "email", "mail": "email", "font": "font", "fonts": "font", "other": "other",
}


def kind_of(ext: str, is_dir: bool) -> int:
    if is_dir:
        return KIND_ID["app"] if ext == "app" else KIND_ID["folder"]
    return KIND_ID[EXT_KIND.get(ext, "other")]


FILE_KEYS = {"ext", "type", "kind", "size", "mtime", "path", "re", "limit", "in"}
MAX_LIMIT = 1000
DEFAULT_LIMIT = 200
_UNITS = {"b": 1, "k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2, "g": 1024 ** 3, "gb": 1024 ** 3,
          "t": 1024 ** 4, "tb": 1024 ** 4}
_DURATIONS = {"min": 60, "mi": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "mo": 30 * 86400, "y": 365 * 86400}


class QueryError(ValueError):
    """A query the engine cannot run (bad regex, bad size or date): a 400 at the API."""


# ------------------------------------------------------------------ parsing

def _tokens(q: str) -> list[str]:
    out, i, n = [], 0, len(q)
    while i < n:
        while i < n and q[i].isspace():
            i += 1
        if i >= n:
            break
        j, inq = i, False
        while j < n:
            ch = q[j]
            if ch == '"':
                inq = not inq
            elif ch.isspace() and not inq:
                break
            j += 1
        out.append(q[i:j])
        i = j
    return out


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    return v.lstrip('"')


def is_folder_value(v: str) -> bool:
    """`in:` values that start with ~ or / are folder paths; anything else names a MySearch folder or group."""
    return v.strip().startswith(("~", "/"))


def _word_boundary(nf: str, i: int) -> bool:
    return i == 0 or not nf[i - 1].isalnum()


def _bytes(v: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*", v.lower())
    if not m or (m.group(2) and m.group(2) not in _UNITS):
        raise QueryError(f"cannot read the size '{v}' (use e.g. 500kb, 5mb, 2gb)")
    return int(float(m.group(1)) * _UNITS[m.group(2) or "b"])


def parse_size(v: str) -> dict:
    v = v.strip()
    if ".." in v:
        a, b = v.split("..", 1)
        out = {}
        if a.strip():
            out["min"] = _bytes(a)
        if b.strip():
            out["max"] = _bytes(b)
        return out
    m = re.fullmatch(r"(<=|>=|<|>|=)?\s*(.+)", v)
    op, num = m.group(1) or "=", _bytes(m.group(2))
    return {">": {"min": num + 1}, ">=": {"min": num}, "<": {"max": num - 1}, "<=": {"max": num},
            "=": {"min": num, "max": num}}[op]


def _day_range(v: str) -> tuple[float, float] | None:
    """'2026-01-05' / '2026-01' / '2026' -> [start, end) in local time; also today, yesterday."""
    v = v.strip().lower()
    today = date.today()
    if v in ("today", "yesterday"):
        d = today - timedelta(days=1 if v == "yesterday" else 0)
        a = datetime(d.year, d.month, d.day)
        return a.timestamp(), (a + timedelta(days=1)).timestamp()
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?", v)
    if not m:
        return None
    y, mo, d = int(m.group(1)), m.group(2), m.group(3)
    try:
        if d:
            a = datetime(y, int(mo), int(d))
            b = a + timedelta(days=1)
        elif mo:
            a = datetime(y, int(mo), 1)
            b = datetime(y + (int(mo) == 12), int(mo) % 12 + 1, 1)
        else:
            a, b = datetime(y, 1, 1), datetime(y + 1, 1, 1)
    except ValueError:
        raise QueryError(f"not a date: '{v}'")
    return a.timestamp(), b.timestamp()


def _age(v: str) -> float | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(min|mi|h|d|w|mo|y)", v.strip().lower())
    return float(m.group(1)) * _DURATIONS[m.group(2)] if m else None


def parse_mtime(v: str, now: float) -> dict:
    """-> {"min": epoch?, "max": epoch?} (inclusive bounds on the modification time)."""
    v = v.strip()
    if ".." in v:
        a, b = (x.strip() for x in v.split("..", 1))
        out = {}
        if a:
            r = _day_range(a)
            if not r:
                raise QueryError(f"cannot read the date '{a}'")
            out["min"] = r[0]
        if b:
            r = _day_range(b)
            if not r:
                raise QueryError(f"cannot read the date '{b}'")
            out["max"] = r[1] - 1
        return out
    m = re.fullmatch(r"(<=|>=|<|>|=)?\s*(.+)", v)
    op, val = m.group(1) or "=", m.group(2).strip()
    age = _age(val)
    if age is not None:                          # a duration: "<7d" is newer than 7 days, ">7d" older
        if op in ("<", "<="):
            return {"min": now - age}
        if op in (">", ">="):
            return {"max": now - age}
        return {"min": now - age}
    r = _day_range(val)
    if r is None:
        raise QueryError(f"cannot read the time '{val}' (use 7d, 2w, 2026-01-01, 2025-03 …)")
    a, b = r
    return {">": {"min": a}, ">=": {"min": a}, "<": {"max": a - 1}, "<=": {"max": b - 1},
            "=": {"min": a, "max": b - 1}}[op]


def is_mixed_case(raw: str) -> bool:
    """The smart-case rule: a word typed with a capital AND a small letter (`Report`, `iPhone`, `Café`) is matched with
    its case; all small (`report`) or all capitals (`PDF`, `README`) letters match any case."""
    return any(c.isupper() for c in raw) and any(c.islower() for c in raw)


def nfc(s: str) -> str:
    return s if s.isascii() else unicodedata.normalize("NFC", s)


def cs_match(name: str, raw: str, mode: str) -> bool:
    """Case-sensitive test of one word against a displayed name (Unicode-normalised: a name stored decomposed still
    matches a word typed composed). mode: sub (contains), pre, suf, whole."""
    n, w = nfc(name), nfc(raw)
    if mode == "pre":
        return n.startswith(w)
    if mode == "suf":
        return n.endswith(w)
    if mode == "whole":
        return n == w
    return w in n


_CS_MODE = {"prefix": "pre", "suffix": "suf", "whole": "whole"}


class Word:
    __slots__ = ("raw", "text", "mode", "neg", "cs")

    def __init__(self, raw: str, mode: str, neg: bool, cs: bool = False):
        self.raw, self.mode, self.neg, self.cs = raw, mode, neg, cs
        self.text = fold(raw)

    def public(self) -> dict:
        d = {"text": self.raw, "mode": self.mode, "not": self.neg}
        if self.cs:
            d["case"] = "sensitive"
        return d


# ------------------------------------------------------------------ globs (`*.pdf`, `report-*.docx`, `**/invoices/*`)

def has_wildcard(tok: str) -> bool:
    return "*" in tok


MAX_GLOB_CHARS, MAX_GLOB_STARS = 200, 6


def glob_segments(pat: str) -> list:
    """A path glob as segments for gitignore's matcher (wildcards matched with a bounded algorithm, no regular expression).
    `*` and `?` stay inside one folder or file name, `**/` is any number of folders (also none), a pattern may start at any
    folder unless it starts with `/`, a trailing `/` or `/**` means everything below."""
    anchored = pat.startswith("/")
    body = pat.lstrip("/")
    below = body.endswith("/")
    parts = [p for p in body.split("/") if p]
    segs: list = []
    for k, p in enumerate(parts):
        if p == "**":
            segs.append(_gi.DSTAR1 if k == len(parts) - 1 and len(parts) > 1 else _gi.DSTAR)
        else:
            segs.append(_gi._tokens(p, plain=True))
    if below:
        segs.append(_gi.DSTAR1)
    if not anchored:
        segs.insert(0, _gi.DSTAR)
    return segs


def glob_sql(pat: str) -> str:
    """The same name glob as SQLite's GLOB understands it, over the folded name: `[` is kept literal."""
    return fold(pat).replace("[", "[[]")


def glob_literals(pat: str) -> list[str]:
    """Runs of plain characters of 3 or more in a name glob, folded: what a name must contain, whatever the wildcards."""
    out = []
    for r in re.split(r"[*?]+", pat):
        f = fold(r)
        if len(f) >= 3 and "/" not in f:
            out.append(f)
    return out


class Glob:
    __slots__ = ("raw", "kind", "neg", "sql", "literals", "hint")

    def __init__(self, raw: str, neg: bool, home: str):
        self.raw, self.neg = raw, neg
        if len(raw) > MAX_GLOB_CHARS or len(re.findall(r"\*+", raw)) > MAX_GLOB_STARS:
            raise QueryError(f"the glob '{raw[:40]}' is too long or has too many wildcards (at most {MAX_GLOB_STARS} `*`)")
        if "/" in raw:                                  # a path glob: matched against the whole path
            self.kind = "path"
            self.sql = home + raw[1:] if raw.startswith("~/") else raw          # what mse_glob receives
            self.literals = []
            # a piece of the path (plain ASCII, as typed) that every match contains, to pre-filter with LIKE
            runs = [r for r in re.split(r"[*?/]+", raw.lstrip("~")) if len(r) >= 3 and r.isascii()]
            self.hint = max(runs, key=len) if runs else None
        else:                                           # a name glob: matched against the whole name
            self.kind = "name"
            self.sql = glob_sql(raw)
            self.literals = glob_literals(raw)
            self.hint = None

    def public(self) -> str:
        return self.raw


class Query:
    def __init__(self):
        self.words: list[Word] = []
        self.ext: list[str] = []
        self.kinds: list[str] = []
        self.not_ext: list[str] = []
        self.not_kinds: list[str] = []
        self.path: list[str] = []
        self.not_path: list[str] = []
        self.regex: list[str] = []
        self.folders: list[str] = []         # absolute folder paths from in:~/… or in:/…
        self.scope_names: list[str] = []     # in:<collection or group> left in the query (the server resolves them)
        self.size: dict | None = None
        self.mtime: dict | None = None
        self.limit: int | None = None
        self.globs: list[Glob] = []          # `*.log`, `report-*.docx`, `**/invoices/*` (negated: !*.log)

    def public(self) -> dict:
        f = {}
        for key, val in (("ext", self.ext), ("kind", self.kinds), ("path", self.path), ("re", self.regex),
                         ("in", self.folders), ("size", self.size), ("mtime", self.mtime), ("limit", self.limit)):
            if val:
                f[key] = val
        if any(not g.neg for g in self.globs):
            f["glob"] = [g.public() for g in self.globs if not g.neg]
        nots = {k: v for k, v in (("ext", self.not_ext), ("kind", self.not_kinds), ("path", self.not_path),
                                  ("glob", [g.public() for g in self.globs if g.neg])) if v}
        if nots:
            f["not"] = nots
        return {"words": [w.public() for w in self.words], "filters": f}

    @property
    def needs_functions(self) -> bool:
        """Does the SQL of this query call mse_cs / mse_glob (see register_functions)?"""
        return any(w.cs for w in self.words) or any(g.kind == "path" for g in self.globs)


def _glob_token(out: Query, t: str, neg: bool, home: str) -> bool:
    """A bare token with a `*` in it is a glob, unless it is quoted, an 'exact word or anchored with ^ / $ (those stay
    literal text). `*.pdf` is the extension filter; anything else becomes a Glob. -> True when the token was consumed."""
    if "*" not in t or t.startswith(("'", '"', "^")) or t.endswith(("$", '$"')):
        return False
    m = re.fullmatch(r"\*\.([A-Za-z0-9]{1,12})", t)
    if m:
        (out.not_ext if neg else out.ext).append(m.group(1).lower())
        return True
    if t.strip("*/") == "":                   # `*` or `**` alone: everything, which is no constraint
        return True
    out.globs.append(Glob(t, neg, home))
    return True


def parse_query(q: str, now: float | None = None, home: str | None = None, smart_case: bool = True) -> Query:
    import os
    now = time.time() if now is None else now
    home = (home or os.path.expanduser("~")).rstrip("/")
    out = Query()
    for tok in _tokens(q or ""):
        neg = tok.startswith("!") and len(tok) > 1
        t = tok[1:] if neg else tok
        m = re.match(r"^([A-Za-z_]+):(.*)$", t, re.S)
        if m and m.group(1).lower() in FILE_KEYS and m.group(2):
            _filter(out, m.group(1).lower(), _unquote(m.group(2)), neg, now, home)
            continue
        if _glob_token(out, t, neg, home):
            continue
        mode = "fuzzy"
        if t.startswith("'"):
            mode, t = "exact", t[1:]
        pre = suf = False
        if t.startswith("^"):
            pre, t = True, t[1:]
        if t.endswith("$") and len(t) > 1:
            suf, t = True, t[:-1]
        elif t.endswith('$"') and len(t) > 2:
            suf, t = True, t[:-2] + '"'
        quoted = t.startswith('"')
        t = _unquote(t)
        if not t:
            continue
        if pre and suf:
            mode = "whole"
        elif pre:
            mode = "prefix"
        elif suf:
            mode = "suffix"
        elif quoted:
            mode = "exact"
        if "/" in t and not neg and mode in ("fuzzy", "exact"):      # docs/readme: a path part and a name part
            d, _, name = t.rpartition("/")
            if d:
                out.path.append(d.lower() + "/")
            t = name
            if not t:
                continue
        if mode == "suffix" and not neg and re.fullmatch(r"\.[a-z0-9]{1,8}", t.lower()):
            out.ext.append(t.lower().lstrip("."))               # ".pdf$" is the extension pdf: the extension index answers it
            continue
        out.words.append(Word(t, mode, neg, cs=smart_case and is_mixed_case(t)))
    return out


def _filter(out: Query, key: str, value: str, neg: bool, now: float, home: str) -> None:
    import os
    if key == "ext":
        vals = [v.strip().lower().lstrip(".*") for v in re.split(r"[,;|]", value) if v.strip()]
        (out.not_ext if neg else out.ext).extend(vals)
    elif key in ("type", "kind"):
        for v in re.split(r"[,;|]", value):
            v = v.strip().lower()
            if not v:
                continue
            k = TYPE_WORDS.get(v)
            if k is None:                         # an unknown word is an extension: type:swift
                (out.not_ext if neg else out.ext).append(v.lstrip("."))
            else:
                (out.not_kinds if neg else out.kinds).append(k)
    elif key == "size":
        out.size = parse_size(value)
    elif key == "mtime":
        out.mtime = parse_mtime(value, now)
    elif key == "path":
        (out.not_path if neg else out.path).append(value.lower())
    elif key == "re":
        if len(value) > 200:
            raise QueryError("regular expression too long")
        try:
            re.compile(value)
        except re.error as e:
            raise QueryError(f"bad regular expression: {e}")
        # nested quantifiers like (a+)+ can take forever on a long name, and Python cannot interrupt a regex
        if re.search(r"\([^()]*[+*][^()]*\)\s*(?:[+*]|\{\d*,\d*\})", value) or re.search(r"\(\?[^)]*\)[+*]{2}", value):
            raise QueryError("that regular expression could run forever on a long name; simplify the repeated groups")
        out.regex.append(value)
    elif key == "limit":
        if not value.isdigit():
            raise QueryError("limit: takes a number")
        out.limit = max(1, min(MAX_LIMIT, int(value)))
    elif key == "in":
        for v in (re.split(r",(?=[~/])", value) if is_folder_value(value) else value.split(",")):
            v = v.strip()
            if not v:
                continue
            if is_folder_value(v):
                out.folders.append(os.path.normpath(os.path.expanduser(v) if v.startswith("~") else v))
            else:
                out.scope_names.append(v)


# ------------------------------------------------------------------ typo variants

def within_one(a: str, b: str) -> bool:
    """Damerau distance <= 1 (insert, delete, substitute, swap neighbours)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = 0
    while i < la and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def swaps(w: str) -> list[str]:
    return [w[:i] + w[i + 1] + w[i] + w[i + 2:] for i in range(len(w) - 1) if w[i] != w[i + 1]]


def typo_ok(word: Word) -> bool:
    return word.mode == "fuzzy" and not word.neg and len(word.text) >= 4 and not word.text.isdigit()


def typo_variants(conn: sqlite3.Connection, w: str, cap: int = 12) -> list[str]:
    """Strings that, found in a name, mean `w` with one typo: the swapped-neighbour forms and (5+ letters) the
    known name words within one edit. Looked up in the vocab table by prefix / suffix halves, which one edit
    cannot spoil both of."""
    out = [s for s in swaps(w) if len(s) >= 3]
    if len(w) >= 5:
        k = max(2, len(w) // 2)
        found = []
        try:
            for lo, hi, col in ((w[:k], w[:k] + "\U0010ffff", "tok"), (w[-k:][::-1], w[-k:][::-1] + "\U0010ffff", "rtok")):
                rows = conn.execute(f"SELECT tok FROM vocab WHERE {col} >= ? AND {col} < ? AND length(tok) BETWEEN ? AND ?"
                                    " LIMIT 30000", (lo, hi, len(w) - 1, len(w) + 1))
                found += [r[0] for r in rows if r[0] != w and within_one(w, r[0])]
        except sqlite3.OperationalError:
            found = []
        for t in found:
            if t not in out:
                out.append(t)
    return out[:cap]


# ------------------------------------------------------------------ SQL functions

_GLOB_CACHE: dict[str, list] = {}


def _udf_glob(path, pattern):
    segs = _GLOB_CACHE.get(pattern)
    if segs is None:
        if len(_GLOB_CACHE) > 256:
            _GLOB_CACHE.clear()
        segs = _GLOB_CACHE[pattern] = glob_segments(pattern)
    return 1 if _gi._segs_match(segs, (path or "").split("/")[1:]) else 0


def _udf_cs(path, raw, mode):
    return 1 if cs_match((path or "").rpartition("/")[2], raw, mode) else 0


def _udf_path_has(path, word):
    """Is the folder part of the path (the name left out) holding this folded word?"""
    return 1 if word in fold((path or "").rpartition("/")[0]) else 0


def register_functions(conn: sqlite3.Connection) -> None:
    """mse_glob(path, glob), mse_cs(path, word, mode) and mse_path_has(path, word): the tests SQL cannot do on its own.
    Registered on the connection that runs a query that needs them (a pooled connection is used by one search at a time)."""
    conn.create_function("mse_glob", 2, _udf_glob, deterministic=True)
    conn.create_function("mse_cs", 3, _udf_cs, deterministic=True)
    conn.create_function("mse_path_has", 2, _udf_path_has, deterministic=True)


# ------------------------------------------------------------------ SQL plan

COLS = "f.id, f.path, f.nf, f.is_dir, f.size, f.mtime, f.ctime, f.ext, f.kind, f.depth"
INNER_CAP = 4000       # candidates one trigram query looks at (the retry looks at DEEP_CAP)
DEEP_CAP = 80000
SHORT_INNER = 4000
SCAN_CAP = 300000
TYPO_WINDOW, TYPO_CAP = 6000, 600


def _fts_term(t: str) -> str:
    return '"' + t.replace('"', '""') + '"'


def _hi(p: str) -> str:
    return p + "\U0010ffff"


def _path_range(folder: str) -> tuple[str, str]:
    f = folder.rstrip("/")
    return f + "/", f + "0"            # "0" is the character after "/": every path below the folder


def _like(s: str) -> str:
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _where(q: Query, scope_roots: list[str] | None) -> tuple[list[str], list]:
    cl, pr = [], []
    if q.ext:
        cl.append(f"f.ext IN ({','.join('?' * len(q.ext))})")
        pr += q.ext
    if q.not_ext:
        cl.append(f"f.ext NOT IN ({','.join('?' * len(q.not_ext))})")
        pr += q.not_ext
    for neg, kinds in ((False, q.kinds), (True, q.not_kinds)):
        if not kinds:
            continue
        ids = [KIND_ID[k] for k in kinds if k in KIND_ID]
        files = "file" in kinds
        parts = []
        if ids:
            parts.append(f"f.kind IN ({','.join('?' * len(ids))})")
            pr += ids
        if files:
            parts.append("f.is_dir = 0")
        if parts:
            cl.append(("NOT " if neg else "") + "(" + " OR ".join(parts) + ")")
    if q.size:
        cl.append("f.is_dir = 0")
        if "min" in q.size:
            cl.append("f.size >= ?")
            pr.append(q.size["min"])
        if "max" in q.size:
            cl.append("f.size <= ?")
            pr.append(q.size["max"])
    if q.mtime:
        if "min" in q.mtime:
            cl.append("f.mtime >= ?")
            pr.append(int(q.mtime["min"]))
        if "max" in q.mtime:
            cl.append("f.mtime <= ?")
            pr.append(int(q.mtime["max"]))
    for p in q.path:
        cl.append("f.path LIKE ? ESCAPE '\\'")
        pr.append(_like(p))
    for p in q.not_path:
        cl.append("f.path NOT LIKE ? ESCAPE '\\'")
        pr.append(_like(p))
    for group in (q.folders, scope_roots):
        if group:
            cl.append("(" + " OR ".join("(f.path >= ? AND f.path < ?)" for _ in group) + ")")
            for g in group:
                pr += list(_path_range(g))
    for g in q.globs:
        if g.kind == "name":
            cl.append(("NOT " if g.neg else "") + "(f.nf GLOB ?)")
            pr.append(g.sql)
        else:
            if g.hint and not g.neg:
                cl.append("f.path LIKE ? ESCAPE '\\'")             # cheap superset first, the exact regex second
                pr.append(_like(g.hint))
            cl.append(("NOT " if g.neg else "") + "mse_glob(f.path, ?)")
            pr.append(g.sql)
    for w in q.words:
        t = w.text
        if w.cs:                                  # the case rule: the folded text finds candidates, the typed text decides
            mode = "sub" if w.neg else _CS_MODE.get(w.mode, "sub")
            cl.append(("NOT " if w.neg else "") + "mse_cs(f.path, ?, ?)")
            pr += [w.raw, mode]
            if w.neg:
                continue
        if w.neg:
            cl.append("instr(f.nf, ?) = 0")
            pr.append(t)
        elif w.mode == "prefix":
            cl.append("substr(f.nf, 1, ?) = ?")
            pr += [len(t), t]
        elif w.mode == "suffix":
            cl.append("substr(f.nf, ?) = ?")
            pr += [-len(t), t]
        elif w.mode == "whole":
            cl.append("f.nf = ?")
            pr.append(t)
        elif len(t) < 3:
            cl.append("instr(f.nf, ?) > 0")
            pr.append(t)
    return cl, pr


def _needle(q: Query) -> str | None:
    cands = [w.text for w in q.words if not w.neg and len(w.text) >= 3 and w.mode in ("fuzzy", "exact", "suffix")]
    return max(cands, key=len) if cands else None


_TIER_SQL = ("CASE WHEN f.nf = ? OR substr(f.nf, 1, ?) = ? || '.' THEN 4 WHEN substr(f.nf, 1, ?) = ? THEN 3"
             " WHEN instr(f.nf, ' ' || ?) > 0 OR instr(f.nf, '_' || ?) > 0 OR instr(f.nf, '-' || ?) > 0"
             " OR instr(f.nf, '.' || ?) > 0 THEN 2 ELSE 1 END")


def _tier_order(needle) -> tuple[str, list]:
    """ORDER BY fragment (with its parameters): the needle's tier first, then shallow, then recent."""
    if not needle:
        return "f.depth ASC, f.mtime DESC", []
    n = len(needle)
    return _TIER_SQL + " DESC, f.depth ASC, f.mtime DESC", [needle, n + 1, needle, n, needle, needle, needle, needle, needle]


# ------------------------------------------------------------------ the trigram plan
#
# Schema 2 keeps, per name, only the set of its 3-letter pieces (macindex.FTS_SCHEMA). To find the names that contain a
# word the plan asks the index for names that contain the word's rarest pieces (three of them; every name that contains the
# word contains all of its pieces, so this never loses a match), looks at the first `inner` names in index order that
# really contain the word (instr() inside the window, so the window holds exactly the names the first index's phrase
# query would have returned), and lets the outer query filter and order them. Which pieces are rare comes from tri_stats;
# a stale or missing count changes the speed, never the answer.

TRI_TABLE, LEGACY_TABLE = "name_tri", "names"
TRI_PER_NEEDLE = 3
# A word whose rarest piece is in more names than INNER_CAP but no more than ALL_BOUND is looked at whole instead of
# through the first INNER_CAP-name window. 0 = off: the old index only ever looked at that window, so any value changes
# which rows a very common word offers. With 8000 the top 20 differ from the old engine's for 3 of 235 benchmark queries
# (output, receipt, scripts), each time closer to a full scan's top 20 and never further, so it stays off until the owner
# decides that closer is worth different.
ALL_BOUND = 0


def detect_fts(conn: sqlite3.Connection) -> str:
    """name_tri when the connection's database has the schema-2 index, else the first index. A database whose meta table
    still says schema 1 is read through the first index even if a half-built name_tri is lying around (an upgrade that
    was interrupted): the schema flag is switched in the same transaction that completes the new index."""
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name IN (?, ?)", (TRI_TABLE, LEGACY_TABLE))}
    except sqlite3.Error:
        return LEGACY_TABLE
    try:
        row = conn.execute("SELECT v FROM meta WHERE k = 'schema'").fetchone()
    except sqlite3.Error:
        row = None                                  # no meta table: a bare test database, trust the tables
    if row is not None and str(row[0]).strip() == "1" and LEGACY_TABLE in have:
        return LEGACY_TABLE
    return TRI_TABLE if TRI_TABLE in have else LEGACY_TABLE


def tri_pieces(s: str) -> list[str]:
    """The distinct 3-character pieces of s, in order of appearance."""
    seen, out = set(), []
    for i in range(len(s) - 2):
        t = s[i:i + 3]
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def stat_counts(conn: sqlite3.Connection, pieces) -> dict[str, int]:
    """{piece: names that contain it} for the pieces tri_stats lists (the others are rare, or the table is not there)."""
    pieces = list(dict.fromkeys(pieces))
    out: dict[str, int] = {}
    try:
        for i in range(0, len(pieces), 400):
            part = pieces[i:i + 400]
            out.update(conn.execute(f"SELECT tri, n FROM tri_stats WHERE tri IN ({','.join('?' * len(part))})", part))
    except sqlite3.OperationalError:
        return {}
    return out


def pick_pieces(pieces: list[str], counts: dict[str, int], k: int = TRI_PER_NEEDLE) -> list[str]:
    """The k rarest pieces, or (with no counts at all) k spread over the word."""
    if len(pieces) <= k:
        return pieces
    if not counts:
        idx = sorted({round(i * (len(pieces) - 1) / (k - 1)) for i in range(k)})
        return [pieces[i] for i in idx]
    return [p for _, _, p in sorted((counts.get(p, 0), i, p) for i, p in enumerate(pieces))[:k]]


# A plan is a tree: ("n", text, cs_raw) = a name containing `text` (cs_raw: the word as typed when the case rule applies),
# ("and", [plans]), ("or", [plans]).

def plan_pieces(node) -> list[str]:
    if node[0] == "n":
        return tri_pieces(node[1])
    return [p for c in node[1] for p in plan_pieces(c)]


def plan_render(node, counts: dict[str, int], per: int = TRI_PER_NEEDLE):
    """-> (MATCH expression, verification SQL, its parameters, rarest piece count or None when nothing is known).
    The MATCH expression is only 3-letter terms: with detail=none a longer quoted string would be a phrase, which the
    index cannot answer."""
    if node[0] == "n":
        text, cs = node[1], node[2] if len(node) > 2 else None
        pieces = tri_pieces(text)
        chosen = pick_pieces(pieces, counts, per)
        expr = "(" + " AND ".join(_fts_term(t) for t in chosen) + ")"
        ver, par = "instr(f.nf, ?) > 0", [text]
        if cs:
            ver += " AND mse_cs(f.path, ?, 'sub')"
            par.append(cs)
        est = min((counts.get(t, 0) for t in pieces), default=0) if counts else None
        return expr, "(" + ver + ")", par, est
    parts = [plan_render(c, counts, per) for c in node[1]]
    op = " AND " if node[0] == "and" else " OR "
    ests = [p[3] for p in parts]
    if None in ests:
        est = None
    else:
        est = min(ests) if node[0] == "and" else sum(ests)
    return ("(" + op.join(p[0] for p in parts) + ")", "(" + op.join(p[1] for p in parts) + ")",
            [x for p in parts for x in p[2]], est)


def plan_phrases(node) -> str:
    """The first index's MATCH expression for a plan (phrases, which detail=full can answer)."""
    if node[0] == "n":
        return _fts_term(node[1])
    op = " AND " if node[0] == "and" else " OR "
    return "(" + op.join(plan_phrases(c) for c in node[1]) + ")"


def _run_fts(conn, q: Query, scope_roots, plan, needle, inner: int, cap: int, table: str = TRI_TABLE,
             extra: tuple[list[str], list] | None = None) -> list[tuple]:
    """The index's first `inner` candidates (index order), filtered, then the best `cap` by the needle's tier, depth and
    recency. Work is bounded by `inner`, however common the term; when filters leave too few of the first candidates,
    one retry looks much deeper. `extra`: more (clauses, parameters) for the outer filter."""
    cl, pr = _where(q, scope_roots)
    if extra:
        cl, pr = cl + extra[0], pr + extra[1]
    where = (" WHERE " + " AND ".join(cl)) if cl else ""
    order, oparams = _tier_order(needle)
    if table == LEGACY_TABLE:
        expr, ver, vpar = plan_phrases(plan), None, []
        win = "SELECT rowid FROM names WHERE names MATCH ? LIMIT ?"
        sql = (f"SELECT * FROM (SELECT {COLS} FROM ({win}) AS m JOIN files f ON f.id = m.rowid{where}) AS f "
               f"ORDER BY {order} LIMIT {cap}")
        probe = "SELECT count(*) FROM (SELECT rowid FROM names WHERE names MATCH ? LIMIT ?)"
    else:
        counts = stat_counts(conn, plan_pieces(plan))
        expr, ver, vpar, est = plan_render(plan, counts, TRI_PER_NEEDLE)
        if est is not None and inner == INNER_CAP and ALL_BOUND and INNER_CAP < est <= ALL_BOUND:
            inner = ALL_BOUND
        win = (f"SELECT {COLS} FROM {TRI_TABLE} CROSS JOIN files AS f ON f.id = {TRI_TABLE}.rowid "
               f"WHERE {TRI_TABLE} MATCH ? AND {ver} LIMIT ?")
        sql = f"SELECT * FROM (SELECT {COLS} FROM ({win}) AS f{where}) AS f ORDER BY {order} LIMIT {cap}"
        probe = (f"SELECT count(*) FROM (SELECT 1 FROM {TRI_TABLE} CROSS JOIN files AS f ON f.id = {TRI_TABLE}.rowid "
                 f"WHERE {TRI_TABLE} MATCH ? AND {ver} LIMIT ?)")
    head = [expr] + vpar
    found = conn.execute(sql, head + [inner] + pr + oparams).fetchall()
    if cl and len(found) < cap and inner < DEEP_CAP:
        looked = conn.execute(probe, head + [inner]).fetchone()[0]
        if looked >= inner:                       # the first candidates were not all of them: look deeper
            found = conn.execute(sql, head + [DEEP_CAP] + pr + oparams).fetchall()
    return found


def _run_ids(conn, q: Query, scope_roots, ids) -> list[tuple]:
    """The rows with these ids that pass the query's filters (a file offered because it was opened for this query)."""
    ids = list(ids)[:200]
    if not ids:
        return []
    cl, pr = _where(q, scope_roots)
    where = " AND ".join([f"f.id IN ({','.join('?' * len(ids))})"] + cl)
    return conn.execute(f"SELECT {COLS} FROM files f WHERE {where}", ids + pr).fetchall()


def _run_range(conn, q: Query, scope_roots, lo: str, hi: str, inner: int, cap: int | None = None,
               needle: str | None = None) -> list[tuple]:
    """Names in the index range [lo, hi) (a prefix, or an exact name), filtered; the best `cap` of the first `inner`."""
    cl, pr = _where(q, scope_roots)
    order, oparams = _tier_order(needle)
    sql = (f"SELECT * FROM (SELECT {COLS} FROM files f WHERE f.nf >= ? AND f.nf < ?" + "".join(" AND " + c for c in cl)
           + f" LIMIT {inner}) AS f ORDER BY {order} LIMIT {cap or inner}")
    return conn.execute(sql, [lo, hi] + pr + oparams).fetchall()


def _run_filters(conn, q: Query, scope_roots, cap: int) -> list[tuple]:
    """No usable word: list by the best index (extension / kind / size / time / folder)."""
    cl, pr = _where(q, scope_roots)
    where = (" WHERE " + " AND ".join(cl)) if cl else ""
    if q.size:
        order = "f.size DESC"
    elif q.ext or q.kinds or q.mtime or not (q.folders or scope_roots):
        order = "f.mtime DESC"
    else:
        order = "f.depth ASC, f.mtime DESC"
    rows = conn.execute(f"SELECT {COLS} FROM files f{where} ORDER BY {order} LIMIT {cap}", pr).fetchall()
    return rows


SCAN_BUDGET_MS = 150   # time a scan for a pattern with no literal text may take before it returns what it has
SCAN_CHUNK = 2000


def _scan(conn, q: Query, scope_roots, regs, stop_at: int, budget_s: float) -> tuple[list[tuple], bool, bool]:
    """A pattern with nothing literal in it (`re:^.{3}$`, `re:\\d{4}-\\d{2}`) cannot ask the name index anything, so the
    names are read in the order the filters would list them and tested here. Only (id, path) leave SQLite, in chunks of
    SCAN_CHUNK, so memory stays flat however many names are read; the full rows are fetched for the matches only. The
    scan stops at `stop_at` matches, or when `budget_s` is used up: the matches found so far are returned and the
    third value says so (`partial`). A hard stop inside SQLite (progress handler) covers a sort that never yields.
    -> (rows, stopped early, ran out of time)"""
    cl, pr = _where(q, scope_roots)
    where = (" WHERE " + " AND ".join(cl)) if cl else ""
    if q.size:
        order = "f.size DESC"
    elif q.ext or q.kinds or q.mtime or not (q.folders or scope_roots):
        order = "f.mtime DESC"
    else:
        order = "f.depth ASC, f.mtime DESC"
    t0 = time.perf_counter()
    deadline = t0 + budget_s
    hard = t0 + max(budget_s * 3, 0.5)
    ids: list[int] = []
    early = timed_out = False
    cur = None
    conn.set_progress_handler(lambda: 1 if time.perf_counter() > hard else 0, 20000)
    try:
        cur = conn.execute(f"SELECT f.id, f.path FROM files f{where} ORDER BY {order}", pr)
        while True:
            chunk = cur.fetchmany(SCAN_CHUNK)
            if not chunk:
                break
            for rid, path in chunk:
                name = path.rpartition("/")[2]
                if all(rx.search(name) for rx in regs):
                    ids.append(rid)
            if len(ids) >= stop_at:
                early = True
                break
            if time.perf_counter() > deadline:
                early = timed_out = True
                break
    except sqlite3.OperationalError as e:
        if "interrupt" not in str(e).lower():
            raise
        early = timed_out = True
    finally:
        conn.set_progress_handler(None, 0)
        if cur is not None:
            cur.close()
    rows: list[tuple] = []
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        rows += conn.execute(f"SELECT {COLS} FROM files f WHERE f.id IN ({','.join('?' * len(part))})", part).fetchall()
    return rows, early, timed_out


def _regex_literal(pattern: str) -> str | None:
    """The longest run of plain characters (3 or more) that a match must contain, or None."""
    if "|" in pattern:
        return None
    runs, cur, i = [], "", 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            if i + 1 < len(pattern) and pattern[i + 1] in ".^$*+?{}[]()|\\/-":
                cur += pattern[i + 1]
            else:
                runs.append(cur)
                cur = ""
            i += 2
            continue
        if c == "[":
            runs.append(cur)
            cur = ""
            j = pattern.find("]", i + 1)
            i = (j + 1) if j != -1 else len(pattern)
            continue
        if c == "(":                     # a group may be optional or repeated (`(abc)*def`): nothing inside it is required
            runs.append(cur)
            cur, depth, i = "", 1, i + 1
            while i < len(pattern) and depth:
                ch = pattern[i]
                if ch == "\\":
                    i += 2
                    continue
                if ch == "[":
                    j = pattern.find("]", i + 1)
                    i = (j + 1) if j != -1 else len(pattern)
                    continue
                depth += (ch == "(") - (ch == ")")
                i += 1
            continue
        if c == "{":                     # {3} {2,5}: the whole quantifier is skipped, the character before it may repeat
            cur = cur[:-1]
            runs.append(cur)
            cur = ""
            j = pattern.find("}", i + 1)
            i = (j + 1) if j != -1 else len(pattern)
            continue
        if c in "*?":
            cur = cur[:-1]               # the character before a quantifier is optional
            runs.append(cur)
            cur = ""
        elif c in ".^$+)":
            runs.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    runs.append(cur)
    runs = [r for r in runs if len(r) >= 3]
    return max(runs, key=len).lower() if runs else None


# ------------------------------------------------------------------ ranking

def _tier_of(nf: str, w: Word) -> tuple[float, tuple[int, int] | None]:
    """(tier 0 = no match .. 5 = exact name, span in nf) for one positive word."""
    t = w.text
    if w.mode == "whole":
        return (5, (0, len(nf))) if nf == t else (0, None)
    if w.mode == "prefix":
        if not nf.startswith(t):
            return 0, None
        return (5 if nf == t else 4), (0, len(t))
    if w.mode == "suffix":
        if not nf.endswith(t):
            return 0, None
        return 3, (len(nf) - len(t), len(nf))
    i = nf.find(t)
    if i < 0:
        return 0, None
    stem = nf.rsplit(".", 1)[0] if "." in nf.strip(".") else nf
    if nf == t or stem == t:
        return 5, (i, i + len(t))
    if i == 0:
        return 4, (0, len(t))
    j = i
    while j != -1 and not _word_boundary(nf, j):          # a later occurrence may sit on a boundary
        j = nf.find(t, j + 1)
    if j != -1:
        return 3, (j, j + len(t))
    return 2, (i, i + len(t))


PATH_TIER = 1.5          # a word found only in the folder path: above a typo (1), below any name match (2 and up)
NAME_EXACT_BONUS = 0.40  # fff's weights: the whole file name is the query: +40 % ...
NAME_BONUS = 0.16        # ... the query is inside the file name: +16 %, at most NAME_BONUS_CAP points
NAME_BONUS_CAP = 30


def score_row(row: tuple, q: Query, alts: dict[int, list[str]], now: float, path_ok: bool = False):
    """-> (score, spans in nf, typo?) or None when the row does not match the words. `path_ok`: a word that is not in the
    name may be in the folder path (it then counts as PATH_TIER, and the row gets none of the name bonuses, so a name match
    always ranks above a path-only match)."""
    nf = row[2]
    tiers, spans, typo, cover, via_path = [], [], False, 0, False
    name = pf = None
    for k, w in enumerate(q.words):
        if w.cs:                                       # the case rule, on the name as displayed
            if name is None:
                name = row[1].rpartition("/")[2]
            hit = cs_match(name, w.raw, "sub" if w.neg else _CS_MODE.get(w.mode, "sub"))
            if w.neg:
                if hit:
                    return None
                continue
            if not hit:
                return None
        elif w.neg:
            if w.text in nf:
                return None
            continue
        tier, span = _tier_of(nf, w)
        if tier == 0 and typo_ok(w):
            for a in alts.get(k, ()):
                i = nf.find(a)
                if i >= 0:
                    tier, span, typo = 1, (i, i + len(a)), True
                    break
        if tier == 0 and path_ok and not w.cs and w.mode in ("fuzzy", "exact"):
            if pf is None:
                pf = fold(row[1].rpartition("/")[0])
            if w.text in pf:
                tier, span, via_path = PATH_TIER, None, True
        if tier == 0:
            return None
        tiers.append(tier)
        if span is not None:
            spans.append(span)
            cover += span[1] - span[0]
    age_days = max(0.0, (now - (row[5] or 0)) / 86400)
    if not tiers:                                 # only filters (ext:pdf, size:>1gb, !word …): newest and shallowest first
        return 100 + 10 * math.exp(-age_days / 180) - min(row[9], 40) * 0.8, [], False
    tier = min(tiers) + 0.15 * (sum(tiers) / len(tiers) - min(tiers))
    score = (tier * 100 + 40 * min(1.0, cover / max(1, len(nf))) + 10 * math.exp(-age_days / 180)
             - min(row[9], 40) * 0.8 + (15 if row[3] and min(tiers) >= 5 else 0))
    if not typo and not via_path:
        # A name match is worth more than the same words anywhere in the path: the whole file name +40 %, otherwise +16 %
        # (a few points at most). Both are increasing in the score, and the exact class started above the other
        # class's ceiling, so the order of name matches among themselves is unchanged.
        score = score * (1 + NAME_EXACT_BONUS) if min(tiers) >= 5 else score + min(score * NAME_BONUS, NAME_BONUS_CAP)
    return score, spans, typo


def _original_spans(name: str, nf: str, spans: list[tuple[int, int]]) -> list[list[int]]:
    """Spans in the folded name -> spans in the displayed name (they differ for non-ASCII names)."""
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    if name.isascii() and len(name) == len(nf):
        return merged
    folded, idx = fold_map(name)
    if folded != nf or not idx:
        return merged
    out = []
    for a, b in merged:
        if a < len(idx) and b - 1 < len(idx):
            end = idx[b - 1] + 1
            while end < len(name) and unicodedata.combining(name[end]):     # keep the accents of the last letter
                end += 1
            out.append([idx[a], end])
    return out


# ------------------------------------------------------------------ the search

def search(conn: sqlite3.Connection, q: Query, limit: int | None = None, scope_roots: list[str] | None = None,
           now: float | None = None, allow=None, fts: str | None = None, budget_ms: float | None = None,
           offset: int = 0, paged: bool = False, folder_words: bool = False, hints=None) -> dict:
    """-> {"items": [...], "total", "truncated", "query"} (+ "partial": true when a scan ran out of time; + "offset",
    "limit", "has_more", "next_offset" when `paged`). `allow(path) -> bool` is the privacy gate: a row it refuses is never
    returned, whatever the index holds. scope_roots: folders the results must be under (scope=indexed). `total` counts the
    matches among the candidates the plan looked at (`truncated` says the plan stopped early).

    Pages. The candidates and their order are planned for the window max(offset + limit, DEFAULT_LIMIT), so for any
    offset + limit up to DEFAULT_LIMIT the list is the same list: items(limit=50) is the first 50 of items(limit=200), and
    items(offset=50, limit=150) the rest of it. `offset` skips rows the gate lets through, not rows it refuses.
    folder_words: a word that is not in a name may be in the folder path when the name search comes up short (see
    score_row). hints: the opened-files ranking (mse/frecency.py), or None."""
    now = time.time() if now is None else now
    table = fts or detect_fts(conn)
    if q.needs_functions or folder_words:
        register_functions(conn)
    limit = q.limit or min(MAX_LIMIT, int(limit or DEFAULT_LIMIT))
    offset = max(0, int(offset or 0))
    limit = min(limit, max(0, MAX_LIMIT - offset))
    want = offset + limit
    window = max(want, DEFAULT_LIMIT)
    cap = min(1500, window + max(100, window // 2))   # candidates scored in Python; SQL has ordered them best first
    rows: dict[int, tuple] = {}
    truncated = partial = False

    def take(found):
        for r in found:
            rows.setdefault(r[0], r)

    positive = [w for w in q.words if not w.neg]
    fts_words = [w for w in positive if len(w.text) >= 3 and w.mode in ("fuzzy", "exact", "suffix")]
    needle = _needle(q)
    regex_lit = _regex_literal(q.regex[0]) if (not fts_words and q.regex) else None
    if regex_lit:
        regex_lit = fold(regex_lit)               # the index holds folded names: a literal "café" is looked for as "cafe"
        regex_lit = regex_lit if len(regex_lit) >= 3 else None
    glob_lits = [lit for g in q.globs if g.kind == "name" and not g.neg for lit in g.literals]

    def leaf(w):
        return ("n", w.text, w.raw if w.cs else None)

    if limit <= 0:
        pass
    elif fts_words or regex_lit or glob_lits:
        plan = ("and", [leaf(w) for w in fts_words] + ([("n", regex_lit)] if regex_lit and not fts_words else [])
                + [("n", lit) for lit in glob_lits])
        found = _run_fts(conn, q, scope_roots, plan, needle, INNER_CAP, cap, table)
        truncated = len(found) >= cap
        take(found)
        if truncated and needle and any(w.text == needle and w.mode != "suffix" for w in fts_words):
            take(_run_range(conn, q, scope_roots, needle, needle + "\U0010ffff", 1500, 120, needle))   # exact / prefix
    else:
        lead = [w for w in positive if w.mode in ("prefix", "whole")] or \
               [w for w in positive if w.mode in ("fuzzy", "exact") and 0 < len(w.text) < 3]
        if lead:
            found = _run_range(conn, q, scope_roots, lead[0].text, _hi(lead[0].text), SHORT_INNER, cap, lead[0].text)
            truncated = len(found) >= cap
            take(found)
        elif q.regex or (q.globs and not (q.ext or q.kinds or q.size or q.mtime or q.folders or q.path)):
            found, early, timed_out = _scan(conn, q, scope_roots, [re.compile(r_, re.I) for r_ in q.regex],
                                            max(cap * 3, 600), (SCAN_BUDGET_MS if budget_ms is None else budget_ms) / 1000)
            truncated = early
            partial = timed_out
            take(found)
        elif (q.ext or q.kinds or q.size or q.mtime or q.folders or q.path or scope_roots or q.not_ext
              or q.not_kinds or q.globs):
            found = _run_filters(conn, q, scope_roots, cap)
            truncated = len(found) >= cap
            take(found)

    if hints is not None and getattr(hints, "ids", None) and limit > 0:
        # files opened for exactly this query before: offered even if the plan's window did not reach them. The id is
        # only a hint; the row must still have the path whose keyed hash was stored, and it passes the same filters.
        for r in _run_ids(conn, q, scope_roots, hints.ids):
            if hints.key(r[1]) == hints.ids[r[0]]:
                rows.setdefault(r[0], r)

    regs = [re.compile(r, re.I) for r in q.regex]
    path_ok = False

    def rank(alts):
        out = []
        for r in rows.values():
            sc = score_row(r, q, alts, now, path_ok)
            if sc is None or (regs and not all(rx.search(r[1].rsplit("/", 1)[-1]) for rx in regs)):
                continue
            score = sc[0] + (hints.bonus(r[1]) if hints is not None else 0.0)
            out.append((score, r, sc[1], sc[2]))
        return out

    alts: dict[int, list[str]] = {}
    scored = rank({}) if limit > 0 else []
    if limit > 0 and len(scored) < want and fts_words:
        alts = {k: typo_variants(conn, w.text) for k, w in enumerate(q.words) if typo_ok(w)}
        for k, alt in alts.items():
            alt = [a for a in alt if len(a) >= 3]
            if not alt:
                continue
            plan = ("and", [("or", [("n", a) for a in alt]) if j == k else leaf(v)
                            for j, v in enumerate(q.words) if not v.neg and len(v.text) >= 3
                            and v.mode in ("fuzzy", "exact", "suffix")])
            take(_run_fts(conn, q, scope_roots, plan, None, TYPO_WINDOW, TYPO_CAP, table))
        if alts:
            scored = rank(alts)
    if limit > 0 and folder_words and len(scored) < want:
        # The name search came up short: names that hold one of the words, in a folder whose path holds the others.
        plain = [w for w in fts_words if w.mode in ("fuzzy", "exact") and not w.cs]
        if len(plain) >= 2:
            for anchor in plain[:3]:
                others = [w for w in plain if w is not anchor]
                ex_cl = ["(instr(f.nf, ?) > 0 OR mse_path_has(f.path, ?))" for _ in others]
                ex_pr = [x for w in others for x in (w.text, w.text)]
                take(_run_fts(conn, q, scope_roots, ("and", [leaf(anchor)]), needle, INNER_CAP, cap, table,
                              extra=(ex_cl, ex_pr)))
            path_ok = True
            scored = rank(alts)
    scored.sort(key=lambda x: (-x[0], x[1][9], -(x[1][5] or 0), x[1][1]))
    items, total, skipped, stop = [], len(scored), 0, len(scored)
    for idx, (score, r, spans, typo) in enumerate(scored):
        if len(items) >= limit:
            stop = idx
            break
        if allow is not None and not allow(r[1]):
            total -= 1                         # refused by the privacy gate: never shown, not counted
            continue
        if skipped < offset:
            skipped += 1                       # on an earlier page
            continue
        path = r[1]
        name = path.rsplit("/", 1)[-1]
        items.append({
            "path": path, "name": name, "parent": path.rsplit("/", 1)[0] or "/", "is_dir": bool(r[3]),
            "kind": KINDS[r[8]] if 0 <= r[8] < len(KINDS) else "other", "ext": r[7],
            "size": 0 if r[3] else r[4], "mtime": r[5], "ctime": r[6], "score": round(score, 2),
            "match": _original_spans(name, r[2], spans),
        })
    out = {"items": items, "total": total, "truncated": bool(truncated), "query": q.public()}
    if partial:
        out["partial"] = True          # a scan ran out of time (SCAN_BUDGET_MS): these are the matches found so far
    if paged:
        more = any(allow is None or allow(r[1]) for _, r, _, _ in scored[stop:]) if stop < len(scored) else False
        out.update(offset=offset, limit=limit, has_more=bool(more), next_offset=(offset + len(items)) if more else None)
    return out
