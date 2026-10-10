"""File-name search over the whole-Mac index (mse/macindex.py): the query language, the SQL plan and the ranking.

The language is fsearch's, plus a few additions:

    invoice acme           every word must be in the name; words are fuzzy (substring; a typo is forgiven, see below)
    'exact                 the exact text, no typo tolerance          "two words"   the same, with a space
    ^prefix  suffix$       the name starts / ends with it             ^whole$       the whole name (case-insensitive)
    !word                  the name must not contain it               !ext:tmp  !type:image  !path:cache
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

Typo tolerance: a word of 5 or more letters finds names that contain a word one edit away (insertion, deletion,
substitution or swapped neighbours: `recieve` finds `receive`). A word of 4 letters forgives swapped neighbours only
(`mian` finds `main`). Shorter words are literal. Typos are looked for only when the literal search finds fewer
results than asked for, and those results rank last.

Ranking: exact name (with or without extension) > prefix > word boundary > substring > typo; ties by how much of the
name the words cover, recency and folder depth; folders get a small boost on exact matches.
"""

import math
import re
import sqlite3
import time
import unicodedata
from datetime import date, datetime, timedelta

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


class Word:
    __slots__ = ("raw", "text", "mode", "neg")

    def __init__(self, raw: str, mode: str, neg: bool):
        self.raw, self.mode, self.neg = raw, mode, neg
        self.text = fold(raw)

    def public(self) -> dict:
        return {"text": self.raw, "mode": self.mode, "not": self.neg}


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

    def public(self) -> dict:
        f = {}
        for key, val in (("ext", self.ext), ("kind", self.kinds), ("path", self.path), ("re", self.regex),
                         ("in", self.folders), ("size", self.size), ("mtime", self.mtime), ("limit", self.limit)):
            if val:
                f[key] = val
        nots = {k: v for k, v in (("ext", self.not_ext), ("kind", self.not_kinds), ("path", self.not_path)) if v}
        if nots:
            f["not"] = nots
        return {"words": [w.public() for w in self.words], "filters": f}


def parse_query(q: str, now: float | None = None, home: str | None = None) -> Query:
    import os
    now = time.time() if now is None else now
    home = home or os.path.expanduser("~")
    out = Query()
    for tok in _tokens(q or ""):
        neg = tok.startswith("!") and len(tok) > 1
        t = tok[1:] if neg else tok
        m = re.match(r"^([A-Za-z_]+):(.*)$", t, re.S)
        if m and m.group(1).lower() in FILE_KEYS and m.group(2):
            _filter(out, m.group(1).lower(), _unquote(m.group(2)), neg, now, home)
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
        out.words.append(Word(t, mode, neg))
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


# ------------------------------------------------------------------ SQL plan

COLS = "f.id, f.path, f.nf, f.is_dir, f.size, f.mtime, f.ctime, f.ext, f.kind, f.depth"
INNER_CAP = 4000       # candidates one trigram query looks at (the retry looks at DEEP_CAP)
DEEP_CAP = 80000
SHORT_INNER = 4000
SCAN_CAP = 300000


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
    for w in q.words:
        t = w.text
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


def _run_fts(conn, q: Query, scope_roots, expr: str, needle, inner: int, cap: int) -> list[tuple]:
    """Trigram MATCH -> at most `inner` candidates (index order), filtered, then the best `cap` by the needle's tier,
    depth and recency. Work is bounded by `inner`, however common the term; when filters leave too few of the
    first candidates, one retry looks much deeper."""
    cl, pr = _where(q, scope_roots)
    where = (" WHERE " + " AND ".join(cl)) if cl else ""
    order, oparams = _tier_order(needle)
    sql = (f"SELECT * FROM (SELECT {COLS} FROM (SELECT rowid FROM names WHERE names MATCH ? LIMIT ?) AS m "
           f"JOIN files f ON f.id = m.rowid{where}) AS f ORDER BY {order} LIMIT {cap}")
    found = conn.execute(sql, [expr, inner] + pr + oparams).fetchall()
    if cl and len(found) < cap and inner < DEEP_CAP:
        looked = conn.execute("SELECT count(*) FROM (SELECT rowid FROM names WHERE names MATCH ? LIMIT ?)",
                              [expr, inner]).fetchone()[0]
        if looked >= inner:                       # the first candidates were not all of them: look deeper
            found = conn.execute(sql, [expr, DEEP_CAP] + pr + oparams).fetchall()
    return found


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
        if c in "*?{":
            cur = cur[:-1]               # the character before a quantifier is optional
            runs.append(cur)
            cur = ""
        elif c in ".^$+()":
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


def score_row(row: tuple, q: Query, alts: dict[int, list[str]], now: float):
    """-> (score, spans in nf, typo?) or None when the row does not match the words."""
    nf = row[2]
    tiers, spans, typo, cover = [], [], False, 0
    for k, w in enumerate(q.words):
        if w.neg:
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
        if tier == 0:
            return None
        tiers.append(tier)
        spans.append(span)
        cover += span[1] - span[0]
    age_days = max(0.0, (now - (row[5] or 0)) / 86400)
    if not tiers:                                 # only filters (ext:pdf, size:>1gb, !word …): newest and shallowest first
        return 100 + 10 * math.exp(-age_days / 180) - min(row[9], 40) * 0.8, [], False
    tier = min(tiers) + 0.15 * (sum(tiers) / len(tiers) - min(tiers))
    score = (tier * 100 + 40 * min(1.0, cover / max(1, len(nf))) + 10 * math.exp(-age_days / 180)
             - min(row[9], 40) * 0.8 + (15 if row[3] and min(tiers) >= 5 else 0))
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
           now: float | None = None, allow=None) -> dict:
    """-> {"items": [...], "total", "truncated", "query"}. `allow(path) -> bool` is the privacy gate: a row it refuses
    is never returned, whatever the index holds. scope_roots: folders the results must be under (scope=indexed).
    `total` counts the matches among the candidates the plan looked at (`truncated` says the plan stopped early)."""
    now = time.time() if now is None else now
    limit = q.limit or min(MAX_LIMIT, int(limit or DEFAULT_LIMIT))
    cap = min(1500, limit + max(100, limit // 2))     # candidates scored in Python; SQL has ordered them best first
    rows: dict[int, tuple] = {}
    truncated = False

    def take(found):
        for r in found:
            rows.setdefault(r[0], r)

    positive = [w for w in q.words if not w.neg]
    fts_words = [w for w in positive if len(w.text) >= 3 and w.mode in ("fuzzy", "exact", "suffix")]
    needle = _needle(q)
    regex_lit = _regex_literal(q.regex[0]) if (not fts_words and q.regex) else None

    if fts_words or regex_lit:
        terms = [_fts_term(w.text) for w in fts_words] + ([_fts_term(regex_lit)] if regex_lit else [])
        found = _run_fts(conn, q, scope_roots, " AND ".join(terms), needle, INNER_CAP, cap)
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
        elif q.regex:
            found = _run_filters(conn, q, scope_roots, SCAN_CAP)
            truncated = len(found) >= SCAN_CAP
            take(found)
        elif (q.ext or q.kinds or q.size or q.mtime or q.folders or q.path or scope_roots or q.not_ext
              or q.not_kinds):
            found = _run_filters(conn, q, scope_roots, cap)
            truncated = len(found) >= cap
            take(found)

    regs = [re.compile(r, re.I) for r in q.regex]

    def rank(alts):
        out = []
        for r in rows.values():
            sc = score_row(r, q, alts, now)
            if sc is None or (regs and not all(rx.search(r[1].rsplit("/", 1)[-1]) for rx in regs)):
                continue
            out.append((sc[0], r, sc[1], sc[2]))
        return out

    scored = rank({})
    if len(scored) < limit and fts_words:
        alts = {k: typo_variants(conn, w.text) for k, w in enumerate(q.words) if typo_ok(w)}
        for k, alt in alts.items():
            alt = [a for a in alt if len(a) >= 3]
            if not alt:
                continue
            terms = [("(" + " OR ".join(_fts_term(a) for a in alt) + ")") if j == k else _fts_term(v.text)
                     for j, v in enumerate(q.words) if not v.neg and len(v.text) >= 3
                     and v.mode in ("fuzzy", "exact", "suffix")]
            take(_run_fts(conn, q, scope_roots, " AND ".join(terms), None, 6000, 600))
        if alts:
            scored = rank(alts)
    scored.sort(key=lambda x: (-x[0], x[1][9], -(x[1][5] or 0), x[1][1]))
    items, total = [], len(scored)
    for score, r, spans, typo in scored:
        if len(items) >= limit:
            break
        if allow is not None and not allow(r[1]):
            total -= 1                         # refused by the privacy gate: never shown, not counted
            continue
        if len(items) < limit:
            path = r[1]
            name = path.rsplit("/", 1)[-1]
            items.append({
                "path": path, "name": name, "parent": path.rsplit("/", 1)[0] or "/", "is_dir": bool(r[3]),
                "kind": KINDS[r[8]] if 0 <= r[8] < len(KINDS) else "other", "ext": r[7],
                "size": 0 if r[3] else r[4], "mtime": r[5], "ctime": r[6], "score": round(score, 2),
                "match": _original_spans(name, r[2], spans),
            })
    return {"items": items, "total": total, "truncated": bool(truncated), "query": q.public()}
