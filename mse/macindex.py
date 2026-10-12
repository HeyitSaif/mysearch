"""The whole-Mac file-name index: an SQLite database of every file and folder name under the configured roots
(default: the home folder), crawled by the native helper and kept current from FSEvents.

Privacy is enforced HERE, in Python, on every path before it is stored and again before it is returned. The helper
prunes the same rules while it walks (a speed-up, not a gate). Never indexed, not even as a name: everything that
matches ignore.SECRET_PATTERNS, credential and key stores, the agents' own state, browser profiles, Mail and
Messages, the system and cache folders, and whatever `mac_index.exclude` lists. Whole-Mac results are for the app
and the web UI: the MCP server has no tool for them (mse/mcp.py) and the CLI refuses them under MSE_LOCK.
"""

import collections
import hashlib
import io
import json
import os
import queue
import re
import sqlite3
import stat as S
import subprocess
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

from . import config, filequery, frecency, gitignore, paths
from .ignore import KEYNOTE_MIN_BYTES, SECRET_PATTERNS, compile_pattern, is_key_name, secret_rules_without_key
from .textutil import fold

TRI_NAME = "name_tri"
SCHEMA_VERSION = 2                     # 2: detail=none trigram index (name_tri) + tri_stats; 1: the first index (names)
LEGACY_SCHEMA = 1
RULES_VERSION = 3                      # bump when the built-in privacy rules change: forces a re-crawl and a purge
BATCH = 2000
SMALL_BATCH = 400                      # rows in a write that is not worth loading the whole vocabulary for
CRAWL_DELAY_S = 20                     # after the service starts, before the first crawl begins
MUST_SCAN = 0x1 | 0x2 | 0x4            # FSEvents: MustScanSubDirs, UserDropped, KernelDropped
F_CREATED, F_REMOVED, F_RENAMED, F_ISDIR = 0x100, 0x200, 0x800, 0x20000

# Never indexed, not configurable. Paths below are relative to the home folder unless they start with "/".
HARD_HOME = [
    ".ssh", ".gnupg", ".aws", ".config/gcloud", ".kube", ".azure", ".config/gh", ".config/op", ".docker",
    ".codex", ".gemini",
    "Library/Keychains", "Library/Mail", "Library/Messages", "Library/Safari", "Library/Cookies", "Library/Accounts",
    "Library/IdentityServices", "Library/Containers", "Library/Group Containers", "Library/Caches", "Library/Logs",
    "Library/Application Support/Google/Chrome", "Library/Application Support/BraveSoftware",
    "Library/Application Support/Microsoft Edge", "Library/Application Support/Arc",
    "Library/Application Support/Firefox", "Library/Application Support/Vivaldi",
    "Library/Application Support/com.operasoftware.Opera", "Library/Application Support/Chromium",
    "Library/Application Support/Zen", "Library/Application Support/MySearch",
    ".Trash",
]
HARD_ABS = ["/System", "/private/var", "/Library/Caches", "/Library/Keychains",
            "/Library/Application Support/com.apple.TCC", "/dev", "/cores", "/Volumes", "/Network",
            "/.Spotlight-V100", "/.fseventsd", "/.Trashes", "/.vol", "/Users/Shared/Previously Relocated Items"]
# Credential-ish names anywhere, on top of ignore.SECRET_PATTERNS.
HARD_PATTERNS = [".claude*", ".codex", "codex-*", ".netrc", ".git-credentials", ".npmrc", ".pypirc",
                 "id_dsa*", "*.gpg", "*.asc", "known_hosts"]
# Noise folders anywhere (also pruned by name).
NOISE_DIRS = ["node_modules", ".git", ".hg", ".svn", ".venv", "venv", "DerivedData", ".Trash", ".Trashes",
              "__pycache__", ".Spotlight-V100", ".fseventsd", ".DocumentRevisions-V100", ".TemporaryItems", ".cache",
              ".npm", ".gradle", ".tox", ".mypy_cache", ".pytest_cache", "site-packages", "Backups.backupdb",
              ".MobileBackups", "com.apple.TimeMachine.localsnapshots", ".next", ".turbo"]
# Configurable defaults (mac_index.exclude). Take an entry out to index it.
DEFAULT_EXCLUDE = config.DEFAULTS["mac_index"]["exclude"]
# Folders macOS protects with a consent dialog (TCC): skipped without Full Disk Access unless already in use.
PROTECTED_HOME = ["Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures", "Library/Mobile Documents"]

# The settings of the name search that the shipped defaults (config.DEFAULTS) do not list. Every key can be changed with
# POST /api/settings {"mac_index": {...}}; config.json keeps only what was changed.
EXTRA_DEFAULTS = {
    "smart_case": True,            # a word typed with a capital AND a small letter (Report) is matched with its case
    "frecency": True,              # files opened from the results rank a little higher, see mse/frecency.py
    "respect_gitignore": False,    # leave out what the .gitignore / .ignore files of a git repository ignore
    "match_folder_names": False,   # when the names alone come up short, words may also be found in the folder path
    "regex_budget_ms": 150,        # time a pattern with no literal text may scan before it returns what it has (partial)
}
DEFAULT_CONFIG = {**config.DEFAULTS["mac_index"], **EXTRA_DEFAULTS}


def home() -> str:
    return str(Path.home())


# ------------------------------------------------------------------ thread QoS

QOS_USER_INTERACTIVE, QOS_DEFAULT = 0x21, 0x15
_QOS_LIB: list = []


def _qos_lib():
    """libSystem's QoS calls through ctypes, or None (not macOS, or the symbols are missing)."""
    if not _QOS_LIB:
        lib = None
        if sys.platform == "darwin":
            try:
                import ctypes
                lib = ctypes.CDLL(None)
                lib.pthread_set_qos_class_self_np.argtypes = [ctypes.c_uint, ctypes.c_int]
                lib.pthread_set_qos_class_self_np.restype = ctypes.c_int
                lib.pthread_get_qos_class_np.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint),
                                                         ctypes.POINTER(ctypes.c_int)]
                lib.pthread_get_qos_class_np.restype = ctypes.c_int
                lib.pthread_self.restype = ctypes.c_void_p
            except (OSError, AttributeError):
                lib = None
        _QOS_LIB.append(lib)
    return _QOS_LIB[0]


def thread_qos() -> int | None:
    """The QoS class of the calling thread (0 = unspecified), or None when it cannot be read."""
    lib = _qos_lib()
    if lib is None:
        return None
    import ctypes
    cls, rel = ctypes.c_uint(0), ctypes.c_int(0)
    return int(cls.value) if lib.pthread_get_qos_class_np(lib.pthread_self(), ctypes.byref(cls), ctypes.byref(rel)) == 0 else None


@contextmanager
def interactive_qos():
    """Run the block at user-interactive QoS on the calling thread only (setpriority would change the whole process),
    then put the thread back: a keep-alive connection thread goes on to serve other routes. A no-op where it cannot work."""
    lib = _qos_lib()
    old = None
    if lib is not None:
        try:
            import ctypes
            cls, rel = ctypes.c_uint(0), ctypes.c_int(0)
            if lib.pthread_get_qos_class_np(lib.pthread_self(), ctypes.byref(cls), ctypes.byref(rel)) == 0:
                old = (cls.value or QOS_DEFAULT, rel.value)
            if lib.pthread_set_qos_class_self_np(QOS_USER_INTERACTIVE, 0) != 0:
                old = None
        except Exception:
            old = None
    try:
        yield
    finally:
        if old is not None:
            try:
                lib.pthread_set_qos_class_self_np(old[0], min(0, old[1]))
            except Exception:
                pass


def db_path() -> Path:
    return paths.DATA / "macindex.db"


def helper_path() -> Path:
    return Path(os.environ.get("MSE_HELPER") or paths.HELPER)


_CFG: tuple = (None, None)


def _config() -> dict:
    """config.load(), re-read only when the file changed (searches call this on every request)."""
    global _CFG
    try:
        st = paths.CONFIG.stat()
        key = (st.st_mtime_ns, st.st_size, str(paths.CONFIG))
    except OSError:
        key = None
    if key is None or _CFG[0] != key:
        _CFG = (key, config.load())
    return _CFG[1]


_FEATURES: dict = {}


def helper_features() -> set[str]:
    """What the installed helper can do: an older build (before the file-name index) has neither `crawl` nor
    `watch --latency`, and must not be asked for them. Cached per binary."""
    h = helper_path()
    try:
        key = (str(h), h.stat().st_mtime_ns)
    except OSError:
        return set()
    if key not in _FEATURES:
        try:
            out = subprocess.run([str(h), "features", "-"], capture_output=True, text=True, timeout=10).stdout
            d = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
            _FEATURES[key] = {k for k, v in d.items() if v is True}
        except (OSError, ValueError, subprocess.SubprocessError, IndexError):
            _FEATURES[key] = set()
    return _FEATURES[key]


def settings() -> dict:
    return {**DEFAULT_CONFIG, **(_config().get("mac_index") or {})}


def check_settings(v: dict) -> dict:
    """Validate a settings change (config.update_settings): a ValueError becomes a 400. Returns the change as it is
    stored: roots as absolute paths, exclude entries in their canonical form (see normalize_exclude), both without
    duplicates. A list is replaced as a whole, so one bad entry (say an old hand-written one) fails the whole change;
    the message names it."""
    if not isinstance(v, dict):
        raise ValueError("mac_index must be an object")
    allowed = set(DEFAULT_CONFIG)
    for k in v:
        if k not in allowed:
            raise ValueError(f"cannot change mac_index.{k}")
    if "enabled" in v and not isinstance(v["enabled"], bool):
        raise ValueError("mac_index.enabled must be true or false")
    for k in ("roots", "exclude"):
        if k in v and (not isinstance(v[k], list) or not all(isinstance(x, str) and x.strip() for x in v[k])):
            raise ValueError(f"mac_index.{k} must be a list of paths")
    if "roots" in v:
        if not v["roots"]:
            raise ValueError("mac_index.roots needs at least one folder (or switch enabled off)")
        v = {**v, "roots": normalize_roots(v["roots"])}
    if "exclude" in v:
        v = {**v, "exclude": normalize_excludes(v["exclude"])}
    for k in ("smart_case", "frecency", "respect_gitignore", "match_folder_names"):
        if k in v and not isinstance(v[k], bool):
            raise ValueError(f"mac_index.{k} must be true or false")
    if "regex_budget_ms" in v and not (isinstance(v["regex_budget_ms"], (int, float)) and not isinstance(v["regex_budget_ms"], bool)
                                       and 20 <= v["regex_budget_ms"] <= 5000):
        raise ValueError("mac_index.regex_budget_ms must be a number of milliseconds between 20 and 5000")
    if "skip_protected" in v and v["skip_protected"] not in ("auto", "always", "never"):
        raise ValueError("mac_index.skip_protected must be auto, always or never")
    if "rescan_every_h" in v and not (isinstance(v["rescan_every_h"], (int, float)) and v["rescan_every_h"] >= 0):
        raise ValueError("mac_index.rescan_every_h must be a number of hours (0 = never)")
    return v


_WILD = re.compile(r"[*?\[]")


def _has_dotdot(p: str) -> bool:
    return ".." in p.replace("\\", "/").split("/")


def _collapse(p: str) -> str:
    """normpath, with the POSIX double leading slash collapsed ("//x" -> "/x")."""
    p = os.path.normpath(p)
    return "/" + p.lstrip("/") if p.startswith("/") else p


def _covers(base: str, p: str) -> bool:
    """p is base itself or lies below it (case-insensitive, as the volume is)."""
    b, q = base.rstrip("/").lower(), p.rstrip("/").lower()
    return q == b or q.startswith(b + "/")


def _short(p: str, h: str) -> str:
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


def normalize_exclude(entry: str, h: str | None = None) -> str:
    """One mac_index.exclude entry -> the form that is stored, or ValueError.

    * A folder is an absolute path or starts with `~/`. It is stored as `~/Sub/` (under the home folder) or `/Sub/`,
      with a trailing slash, and covers everything below it. `..` is refused, `~user` is refused, and so are `/` and
      the home folder or any folder above it: only `/` actually hides everything, but an exclusion that contains the
      home folder does nothing while the home folder is a root (a root inside an exclusion is still searched) and
      hides all of it otherwise, so neither can be what anyone wants.
    * A name or glob without a slash (`node_modules`, `*.tmp`, `**/build`) matches that name anywhere and is kept as
      typed. A relative entry with a slash (`Documents/Old`) would never match anything: refused.
    * A glob with a full path (`~/Documents/*/Drafts`) is kept as a pattern.
    An exclusion can only add to the built-in rules, never take one away (nothing starting with `!` or `#`)."""
    h = (h or home()).rstrip("/")
    e = entry.strip()
    if not e:
        raise ValueError("an excluded folder cannot be empty")
    if any(ord(ch) < 32 for ch in e):
        raise ValueError(f"{e!r}: control characters are not allowed in an excluded folder")
    if e[0] in "!#":
        raise ValueError(f"{e}: an entry cannot start with ! or #. Exclusions only add to the built-in rules; "
                         "nothing here can switch one of those off")
    if _has_dotdot(e):
        raise ValueError(f"{e}: '..' is not allowed; give the full path of the folder")
    if e.startswith("~"):
        if e != "~" and not e.startswith("~/"):
            raise ValueError(f"{e}: only ~ and ~/Folder are understood, not another user's home; give the full path")
        full = h + e[1:]
    elif e.startswith("/"):
        full = e
    else:
        body = e[3:] if e.startswith("**/") else e
        if "/" in body.rstrip("/"):
            raise ValueError(f"{e}: a folder must be a full path (/Users/alex/Folder) or start with ~/. "
                             "A bare name or pattern such as node_modules or *.tmp matches anywhere")
        return e
    full = _collapse(full)
    if _WILD.search(full):
        return _short(full, h)
    if full == "/":
        raise ValueError("/ cannot be excluded: that would hide every file from the whole-Mac search. "
                         "Remove folders from the roots instead, or switch the whole-Mac search off")
    if _covers(full, h):
        raise ValueError(f"{_short(full, h) if full != h else '~'}: this contains your home folder. Excluding it does "
                         "nothing while your home folder is a search root, and hides all of it otherwise. "
                         "Exclude folders inside it, or change the roots")
    return _short(full, h).rstrip("/") + "/"


def exclude_key(entry: str, h: str | None = None) -> str:
    """What makes two exclude entries the same: the expanded folder, lower-cased, without a trailing slash; for a
    pattern the pattern itself, lower-cased."""
    h = (h or home()).rstrip("/")
    e = entry.strip()
    if e.startswith("~") and (e == "~" or e.startswith("~/")):
        e = h + e[1:]
    if e.startswith("/"):
        e = _collapse(e)
    return (e.rstrip("/") or "/").lower()


def normalize_excludes(entries: list[str], h: str | None = None) -> list[str]:
    out, seen = [], set()
    for e in entries:
        s = normalize_exclude(e, h)
        k = exclude_key(s, h)
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out


def normalize_roots(roots: list[str], h: str | None = None) -> list[str]:
    """Roots as absolute paths without duplicates. A root inside a folder that is never indexed (credentials, keys,
    Mail, browser profiles, the system) is refused instead of being dropped silently at crawl time."""
    h = (h or home()).rstrip("/")
    hard = Excluder({"exclude": []}, h=h)
    out, seen = [], set()
    for x in roots:
        if _has_dotdot(x):
            raise ValueError(f"mac_index.roots: {x}: '..' is not allowed; give the full path")
        r = x.strip()
        r = h + r[1:] if r == "~" or r.startswith("~/") else r
        if not r.startswith("/"):
            raise ValueError(f"mac_index.roots must be absolute paths: {x}")
        r = _collapse(r)
        if not hard.root_allowed(r):
            raise ValueError(f"mac_index.roots: {_short(r, h)} is inside a location that is never indexed "
                             "(credentials, keys, Mail, browser profiles, caches or system folders) and cannot be a root")
        if r.lower() not in seen:
            seen.add(r.lower())
            out.append(r)
    return out


def edit_paths(current: list[str], action: str, items: list[str], h: str | None = None,
               roots: bool = False) -> tuple[list[str], list[str], list[str]]:
    """Add (`add`) or take out (`rm`) entries of mac_index.exclude, or of mac_index.roots (roots=True). Returns
    (the new list, the entries that changed it, the ones that did not: already there / not in the list). The new
    list still goes through check_settings (config.update_settings), so a bad entry is refused there."""
    h = (h or home()).rstrip("/")

    def key(x: str) -> str:
        if roots:
            x = h + x[1:] if x == "~" or x.startswith("~/") else x
            return _collapse(x).lower() if x.startswith("/") else x.lower()
        return exclude_key(x, h)

    have = {key(x): x for x in current}
    new, changed, same = list(current), [], []
    for it in items:
        k = key(it)
        if action == "add":
            if k in have:
                same.append(it)
            else:
                new.append(it)
                have[k] = it
                changed.append(it)
        else:
            if k in have:
                new = [x for x in new if key(x) != k]
                have.pop(k)
                changed.append(it)
            else:
                same.append(it)
    return new, changed, same


QOS_UTILITY = 0x11


@contextmanager
def background_qos():
    """The calling thread at utility QoS for the block (long housekeeping must not compete with a typed search)."""
    lib = _qos_lib()
    ok = False
    if lib is not None:
        try:
            ok = lib.pthread_set_qos_class_self_np(QOS_UTILITY, 0) == 0
        except Exception:
            ok = False
    try:
        yield
    finally:
        if ok:
            try:
                lib.pthread_set_qos_class_self_np(QOS_DEFAULT, 0)
            except Exception:
                pass


# ------------------------------------------------------------------ the privacy rules

def _expand(p: str, h: str) -> str:
    p = p.strip()
    if p.startswith("~"):
        p = h + p[1:]
    return os.path.normpath(p) if p.startswith("/") else p


def full_disk_access() -> bool | None:
    """True / False / None (cannot tell). Opening a file only Full Disk Access may open never shows a dialog."""
    for p in (os.path.join(home(), "Library/Application Support/com.apple.TCC/TCC.db"),
              "/Library/Application Support/com.apple.TCC/TCC.db"):
        try:
            os.close(os.open(p, os.O_RDONLY))
            return True
        except PermissionError:
            return False
        except OSError:
            continue
    return None


def protected_skips(mode: str, fda: bool | None, collection_paths: list[str], h: str | None = None) -> list[str]:
    """The consent-protected folders the crawl must not touch (absolute paths). auto: all of them, unless Full Disk
    Access is on, and except a folder that already holds an enabled collection (the service reads it today)."""
    h = h or home()
    if mode == "never" or (mode == "auto" and fda):
        return []
    out = []
    for rel in PROTECTED_HOME:
        p = os.path.join(h, rel)
        if mode == "auto" and any(c == p or c.startswith(p + "/") for c in collection_paths):
            continue
        out.append(p)
    return out


def keynote_verdict(path: str) -> bool | None:
    """The built-in *.key rule for one path, the same rule as indexer._secret_excluded:
    True = blocked: inside a package, a .key that is a private key (a small file), or a .key that is gone (nothing
    can say what it was). False = a Keynote deck: a folder named *.key, or a regular file over KEYNOTE_MIN_BYTES,
    decided by lstat, nothing read. None = no .key name is involved. The other rules still apply to a deck."""
    parts = path.split("/")
    if any(is_key_name(p) for p in parts[:-1]):
        return True
    if not is_key_name(parts[-1]):
        return None
    try:
        st = os.lstat(path)
    except OSError:
        return True
    if S.S_ISDIR(st.st_mode):
        return False
    return not (S.S_ISREG(st.st_mode) and st.st_size > KEYNOTE_MIN_BYTES)


def gone_keynote(path: str) -> bool:
    """A *.key path, outside any package, that no longer exists. Its event must still reach the index, which removes the
    rows under it (a deck it holds). Nothing else is let through: the rules still block everything else."""
    parts = path.split("/")
    if not is_key_name(parts[-1]) or any(is_key_name(p) for p in parts[:-1]):
        return False
    return not os.path.lexists(path)


class Excluder:
    """blocked(path) -> True for anything that must not be indexed or shown. Built from the built-in rules, the
    secret patterns and the settings. A configurable exclusion (mac_index.exclude, the protected folders) does not
    apply inside an explicit root that sits within it (root ~/Library/CloudStorage inside the excluded ~/Library);
    the hard rules apply everywhere. The built-in *.key pattern is the one exception: a Keynote deck is let through
    (keynote_verdict), a private key is not. A *.key that the user configured is blocked as before."""

    def __init__(self, cfg: dict | None = None, h: str | None = None, protected: list[str] | None = None,
                 roots: list[str] | None = None):
        self.home = h or home()
        cfg = {**DEFAULT_CONFIG, **(cfg or {})}
        self.hard = [os.path.normpath(os.path.join(self.home, p)) for p in HARD_HOME] + HARD_ABS
        self.soft: list[str] = []
        self.patterns: list[str] = list(SECRET_PATTERNS) + HARD_PATTERNS
        configured: list[str] = []
        for e in cfg.get("exclude") or []:
            e = _expand(e, self.home)
            if e.startswith("/") and not re.search(r"[*?\[]", e):
                self.soft.append(e.rstrip("/") or "/")
            else:
                self.patterns.append(e)
                configured.append(e)
        self.soft += [p.rstrip("/") for p in (protected or [])]
        self.noise = {n.lower() for n in NOISE_DIRS}
        carve = []
        for r in roots or []:
            r = r.rstrip("/")
            if any(r.startswith(p + "/") or r == p for p in self.soft):
                carve.append(r.lower())
        self._carve_set = set(carve)
        self._carve = tuple(c + "/" for c in carve)
        pre = [p.lower() for p in self.hard]
        self._hset, self._htuple = set(pre), tuple(p.rstrip("/") + "/" for p in pre)
        soft = [p.lower() for p in self.soft]
        self._sset, self._stuple = set(soft), tuple(p.rstrip("/") + "/" for p in soft)
        # every rule but the built-in *.key one (keynote_verdict decides that per path)
        rx = [compile_pattern(p).pattern for p in secret_rules_without_key() + HARD_PATTERNS + configured]
        # re.S: the rules' `.*` must run across a newline too, or one folder named "a\nb" would switch every rule below it off
        self._rx = re.compile("|".join(f"(?:{r})" for r in rx), re.I | re.S) if rx else None
        # every built-in pattern is about one name; a configured pattern with a "/" is about a path: no shortcut then
        self._name_only = not any("/" in (p[3:] if p.startswith("**/") else p).strip("/") for p in self.patterns)
        # Directories whose own chain is verified not blocked (see blocked()). Only clean verdicts are kept: a blocked or
        # unknown directory always takes the full check, so a carved-out root below a blocked folder is still let through.
        self._clean_dirs: set[str] = set()

    CLEAN_DIRS_MAX = 50000

    def blocked(self, path: str, soft: bool = True) -> bool:
        lp = path.lower()
        if ".key" in lp and keynote_verdict(path):
            return True
        if not (soft and self._name_only):
            return self._blocked_full(path, lp, soft)
        parent, _, name = path.rpartition("/")
        if parent in self._clean_dirs:
            # The folder chain is known to be clean: only the entry's own name is new. A name that is itself a rule
            # (an exact hard/soft entry, a noise folder) takes the full check.
            if lp in self._hset or lp in self._sset or name.lower() in self.noise:
                return self._blocked_full(path, lp, soft)
            return bool(self._rx and self._rx.search(name))
        if self._blocked_full(path, lp, soft):
            return True
        if parent and not self._blocked_full(parent, parent.lower(), soft):
            if len(self._clean_dirs) >= self.CLEAN_DIRS_MAX:
                self._clean_dirs.clear()
            self._clean_dirs.add(parent)
        return False

    def blocked_uncached(self, path: str, soft: bool = True) -> bool:
        """The verdict without any cache: the reference blocked() must agree with (tests, audits)."""
        lp = path.lower()
        return bool((".key" in lp and keynote_verdict(path)) or self._blocked_full(path, lp, soft))

    def _blocked_full(self, path: str, lp: str, soft: bool) -> bool:
        if lp in self._hset or lp.startswith(self._htuple):
            return True
        if not self.noise.isdisjoint(lp.split("/")):
            return True
        if self._rx and self._rx.search(path.lstrip("/")):
            return True
        if soft and (lp in self._sset or lp.startswith(self._stuple)):
            return not (lp in self._carve_set or lp.startswith(self._carve))
        return False

    def root_allowed(self, root: str) -> bool:
        # The given form and the real location are both checked, here, once, at the boundary every root passes
        # (normalize_roots for a settings change, the crawl for the configured list). The hard rules name real folders
        # (/private/var, /Volumes, ...) while a root can be typed through a symlink (/var is /private/var on macOS):
        # without the second check a root written as /var/... slipped past a rule written for /private/var/....
        r = root.rstrip("/") or "/"
        return not (self.blocked(r, soft=False) or self.blocked(os.path.realpath(r), soft=False))

    def helper_args(self) -> list[str]:
        args = []
        for p in self.hard + self.soft:
            args += ["--skip-prefix", p]
        for n in NOISE_DIRS:
            args += ["--skip-dir", n]
        # --skip-name is a name glob and cannot test a size, so "*.key" stays a skip here and the crawl never lists a
        # Keynote deck. keynote_verdict lets a deck through only where Python checks a path: live events, walk_python.
        for pat in self.patterns:
            pat = pat.rstrip("/")
            if pat.startswith("**/"):
                pat = pat[3:]
            if pat and "/" not in pat:
                args += ["--skip-name", pat]
        for ext in sorted(filequery.PACKAGE_EXTS):
            args += ["--no-descend", ext]
        return args

    def describe(self) -> list[str]:
        h = self.home

        def short(p):
            return "~" + p[len(h):] if p.startswith(h + "/") else p
        return ([short(p) + "/" for p in self.hard] + [short(p) + "/" for p in self.soft]
                + [n + "/" for n in NOISE_DIRS] + list(self.patterns))


def rules_hash(excl: Excluder, roots: list[str]) -> str:
    blob = json.dumps([RULES_VERSION, sorted(excl.hard), sorted(excl.soft), sorted(excl.patterns), sorted(roots)])
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ what the settings screens show

# The built-in rules, grouped for display (read-only): first matching prefix wins, the last group takes the rest.
# Built from HARD_HOME / HARD_ABS / HARD_PATTERNS / NOISE_DIRS / SECRET_PATTERNS themselves, so a new rule appears
# there without anyone touching the screens.
_HARD_GROUPS = [
    ("Keys, tokens and credentials",
     (".ssh", ".gnupg", ".aws", ".config/gcloud", ".kube", ".azure", ".config/gh", ".config/op", ".docker",
      "Library/Keychains", "/Library/Keychains",
      "/Library/Application Support/com.apple.TCC")),
    ("AI tool state", (".codex", ".gemini")),
    ("Mail, Messages and accounts", ("Library/Mail", "Library/Messages", "Library/Safari", "Library/Cookies",
                                     "Library/Accounts", "Library/IdentityServices")),
    ("Browser profiles", ("Library/Application Support/Google/Chrome", "Library/Application Support/BraveSoftware",
                          "Library/Application Support/Microsoft Edge", "Library/Application Support/Arc",
                          "Library/Application Support/Firefox", "Library/Application Support/Vivaldi",
                          "Library/Application Support/com.operasoftware.Opera",
                          "Library/Application Support/Chromium", "Library/Application Support/Zen")),
    ("App data, caches and logs", ("Library/Containers", "Library/Group Containers", "Library/Caches",
                                   "Library/Logs", "Library/Application Support/MySearch", "/Library/Caches")),
    ("Trash", (".Trash",)),
    ("System folders and disks", ("/",)),
]


def hard_groups(h: str | None = None) -> list[dict]:
    """The rules that can never be switched off, grouped: [{label, items}], items shown with ~ for the home folder."""
    h = (h or home()).rstrip("/")
    groups: dict[str, list[str]] = {label: [] for label, _ in _HARD_GROUPS}
    other = "Other private locations"

    def put(rel: str, shown: str) -> None:
        for label, prefixes in _HARD_GROUPS:
            if any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in prefixes):
                groups[label].append(shown)
                return
        groups.setdefault(other, []).append(shown)

    for rel in HARD_HOME:
        put(rel, f"~/{rel}/")
    for p in HARD_ABS:
        put(p, p + "/")
    out = [{"label": label, "items": groups[label]} for label, _ in _HARD_GROUPS if groups[label]]
    if groups.get(other):
        out.append({"label": other, "items": groups[other]})
    out.append({"label": "Secret and key file names, anywhere", "items": list(SECRET_PATTERNS) + HARD_PATTERNS})
    out.append({"label": "Build, cache and tool folders, anywhere", "items": [n + "/" for n in NOISE_DIRS]})
    return out


def exclude_entries(cfg: dict | None = None, h: str | None = None) -> list[dict]:
    """The configured exclusions, in their stored order and exactly as stored (`entry`), with what each one means:
    {entry, kind: folder|pattern, path (the folder, ~ expanded; None for a pattern), note?, invalid?}. A note says
    when an entry does nothing: a folder that is itself a root (a root inside an exclusion is still searched) or one a
    built-in rule covers already. `invalid` is why check_settings would refuse the entry today (an old hand-written
    one): such an entry blocks every full-list change until it is taken out."""
    cfg = {**DEFAULT_CONFIG, **(cfg if cfg is not None else settings())}
    h = (h or home()).rstrip("/")
    roots = [(_expand(r, h).rstrip("/") or "/") for r in cfg.get("roots") or []]
    hard = Excluder({"exclude": []}, h=h)
    out = []
    for e in cfg.get("exclude") or []:
        x = _expand(e, h)
        try:
            normalize_exclude(e, h)
            bad = None
        except ValueError as err:
            bad = str(err)
        if not x.startswith("/") or _WILD.search(x):
            out.append({"entry": e, "kind": "pattern", "path": None, **({"invalid": bad} if bad else {})})
            continue
        p = x.rstrip("/") or "/"
        item = {"entry": e, "kind": "folder", "path": p, **({"invalid": bad} if bad else {})}
        if not bad and any(p.lower() == r.lower() for r in roots):
            item["note"] = "No effect: this folder is a search root. Remove it from the roots instead."
        elif not bad and hard.blocked(p, soft=False):
            item["note"] = "Already excluded by a built-in rule."
        out.append(item)
    return out


# ------------------------------------------------------------------ rows

_TOK = re.compile(r"[^\W_]+", re.U)
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+")


def name_tokens(name: str) -> set[str]:
    """Words of a name for the typo vocabulary: split on punctuation and camelCase, 4+ letters, folded."""
    out = set()
    for t in _TOK.findall(name):
        f = fold(t)
        if 4 <= len(f) <= 32 and not f.isdigit():
            out.add(f)
        if t != t.lower() and t != t.upper():
            for p in _CAMEL.findall(t):
                fp = fold(p)
                if 4 <= len(fp) <= 32 and not fp.isdigit():
                    out.add(fp)
    return out


def make_row(path: str, is_dir: bool, size: int, mtime: int, ctime: int, gen: int) -> tuple:
    name = path.rsplit("/", 1)[-1]
    ext = ""
    if "." in name.strip("."):
        ext = name.rpartition(".")[2].lower()
        if len(ext) > 12 or (is_dir and ext not in filequery.PACKAGE_EXTS):
            ext = ""
    parent = path.rsplit("/", 1)[0] or "/"
    return (path, fold(name), parent, int(is_dir), int(size or 0), int(mtime or 0), int(ctime or 0), ext,
            filequery.kind_of(ext, is_dir), path.count("/"), gen)


def row_from_stat(path: str, st: os.stat_result, gen: int) -> tuple:
    d = S.S_ISDIR(st.st_mode)
    return make_row(path, d, 0 if d else st.st_size, int(st.st_mtime),
                    int(getattr(st, "st_birthtime", st.st_ctime)), gen)


# ------------------------------------------------------------------ the database

# Tables and the path index (the upsert needs it) always exist; the rest is dropped during the first load and built
# once at the end, which is several times faster than maintaining it row by row (see Store.begin_bulk).
BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT NOT NULL, nf TEXT NOT NULL, parent_id INTEGER, is_dir INTEGER NOT NULL,
  size INTEGER NOT NULL DEFAULT 0, mtime INTEGER NOT NULL DEFAULT 0, ctime INTEGER NOT NULL DEFAULT 0,
  ext TEXT NOT NULL DEFAULT '', kind INTEGER NOT NULL, depth INTEGER NOT NULL, gen INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS files_path ON files(path);
CREATE TABLE IF NOT EXISTS vocab(tok TEXT PRIMARY KEY, rtok TEXT NOT NULL) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS vocab_r ON vocab(rtok);
"""
# Schema 2. The name index keeps no positions (detail=none: about a third of the size of the first index) and no column
# sizes (columnsize=0: nothing here ranks by bm25), so it answers "which names contain this 3-letter piece" and nothing
# else; filequery verifies the whole word with instr(). tri_stats holds how many names contain each piece; it only
# steers the query plan (which pieces to ask for first), so a stale count costs speed and never a result.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS name_tri USING fts5(nf, content='files', content_rowid='id', tokenize='trigram',
  detail=none, columnsize=0);
CREATE VIRTUAL TABLE IF NOT EXISTS name_tri_v USING fts5vocab(name_tri, 'row');
CREATE TABLE IF NOT EXISTS tri_stats(tri TEXT PRIMARY KEY, n INTEGER NOT NULL) WITHOUT ROWID;
"""
# Schema 1, still read (and kept current) until Store.migrate_fts has built the new index.
LEGACY_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS names USING fts5(nf, content='files', content_rowid='id', tokenize='trigram');
"""
INDEXES = """
CREATE INDEX IF NOT EXISTS files_nf ON files(nf);
CREATE INDEX IF NOT EXISTS files_parent ON files(parent_id);
CREATE INDEX IF NOT EXISTS files_ext ON files(ext, mtime);
CREATE INDEX IF NOT EXISTS files_kind ON files(kind, mtime);
CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime);
CREATE INDEX IF NOT EXISTS files_size ON files(size) WHERE is_dir = 0;
"""
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
  INSERT INTO name_tri(rowid, nf) VALUES (new.id, new.nf); END;
CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
  INSERT INTO name_tri(name_tri, rowid, nf) VALUES ('delete', old.id, old.nf); END;
"""
LEGACY_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
  INSERT INTO names(rowid, nf) VALUES (new.id, new.nf); END;
CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
  INSERT INTO names(names, rowid, nf) VALUES ('delete', old.id, old.nf); END;
"""
SECONDARY_INDEXES = ["files_nf", "files_parent", "files_ext", "files_kind", "files_mtime", "files_size"]

STAT_MIN = 8                           # pieces in fewer names than this are not listed in tri_stats (they read as rare)
WATCH_LATENCY_S = 0.05                 # FSEvents coalescing in the helper: a created file is reported ~20 ms later (was 0.3 s)
FLUSH_COALESCE_S = 0.01                # after the first event of a burst, wait this long for the rest before applying
FLUSH_MIN_GAP_S = 0.05                 # at least this long between two applies; 4x the apply time when that is longer
LEGACY_GRACE_S = 60                    # after the upgrade, before the first index's table is dropped
STATS_EVERY_MIN = 5000                 # rows changed before the counts are recounted (or 2% of the index, if more)

# tri_stats is rebuilt from the name index's own vocabulary, a slice of the key space at a time (about 40 ms each on
# two million names), so it never needs one long write.
STAT_BOUNDS = [""] + list("0123456789abcdefghijklmnopqrstuvwxyz") + ["\u0080"]
STAT_SLICES = [(STAT_BOUNDS[i], STAT_BOUNDS[i + 1]) for i in range(len(STAT_BOUNDS) - 1)] + [(STAT_BOUNDS[-1], None)]

UPSERT = ("INSERT INTO files(path, nf, parent_id, is_dir, size, mtime, ctime, ext, kind, depth, gen)"
          " VALUES (?, ?, (SELECT id FROM files WHERE path = ?), ?, ?, ?, ?, ?, ?, ?, ?)"
          " ON CONFLICT(path) DO UPDATE SET is_dir = excluded.is_dir, size = excluded.size, mtime = excluded.mtime,"
          " ctime = excluded.ctime, ext = excluded.ext, kind = excluded.kind, gen = excluded.gen")


INSERT_NEW = ("INSERT INTO files(path, nf, parent_id, is_dir, size, mtime, ctime, ext, kind, depth, gen)"
              " VALUES (?, ?, (SELECT id FROM files WHERE path = ?), ?, ?, ?, ?, ?, ?, ?, ?)"
              " ON CONFLICT(path) DO NOTHING")
UPDATE_CHANGED = ("UPDATE files SET is_dir = ?, size = ?, mtime = ?, ctime = ?, ext = ?, kind = ? WHERE path = ?"
                  " AND (is_dir != ? OR size != ? OR mtime != ? OR ext != ? OR kind != ?)")


def _statements(script: str) -> list[str]:
    """The statements of a DDL script made of statements that each end in ';' (no trigger bodies with inner ';' ...
    triggers are split on their closing 'END;')."""
    out, cur = [], []
    for line in script.strip().splitlines():
        cur.append(line)
        if line.rstrip().endswith("END;") or (line.rstrip().endswith(";") and not any(
                l.lstrip().upper().startswith("CREATE TRIGGER") for l in cur)):
            out.append("\n".join(cur).rstrip().rstrip(";"))
            cur = []
    if cur:
        out.append("\n".join(cur).rstrip().rstrip(";"))
    return [x for x in out if x.strip()]


def open_db(path: Path | str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=30)
        conn.execute("PRAGMA query_only = 1")
    else:
        conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=60)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -65536")
    conn.execute("PRAGMA mmap_size = 2147483648")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


class Store:
    """The writer (one lock) and per-thread read-only connections of macindex.db."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else db_path()
        self.wlock = threading.RLock()
        self._w: sqlite3.Connection | None = None
        self._pool: queue.LifoQueue = queue.LifoQueue()
        self._pool_n = 0
        self._vocab: set[str] | None = None
        self._count = (0.0, 0)
        self._bulk = False
        self._bulk_read = (0.0, False)
        self._fts_cache = (0.0, "names")
        self._dirty = 0                                  # rows changed since tri_stats was last recounted (a hint)
        self._stats_built = False                        # tri_stats recounted by this process (an empty one is then normal)
        self._legacy_left = False                        # schema 2 with the first index still on disk (dropped later)
        self.upgrade: dict = {"state": "idle"}           # the index upgrade, for /api/files/status

    # ---- connections
    def writer(self) -> sqlite3.Connection:
        with self.wlock:
            if self._w is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                w = open_db(self.path)
                try:
                    self._init_writer(w)
                except BaseException:
                    w.close()
                    raise
                self._w = w
            return self._w

    @staticmethod
    def _has_table(w: sqlite3.Connection, name: str) -> bool:
        return w.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None

    def _init_writer(self, w: sqlite3.Connection) -> None:
        """Open an existing or new database in whatever schema it has. Schema 2 gets the trigram index and its triggers.
        Schema 1 (an index from before the speed work) is left exactly as it is and keeps being maintained and searched
        until migrate_fts() has built the new index; a schema-1 database that never finished its first load holds no
        index at all and simply becomes schema 2."""
        w.execute("PRAGMA wal_autocheckpoint = 2000")
        w.executescript(BASE_SCHEMA)
        row = w.execute("SELECT v FROM meta WHERE k = 'schema'").fetchone()
        v = row[0] if row else None
        bulk = w.execute("SELECT v FROM meta WHERE k = 'bulk'").fetchone()
        self._bulk = bool(bulk and bulk[0] == "1")       # (a load interrupted in bulk mode stays in it until it ends)
        legacy_table = self._has_table(w, "names")
        if v is None:
            v = str(LEGACY_SCHEMA) if legacy_table else str(SCHEMA_VERSION)    # decided by the tables, not by a missing row
            w.execute("INSERT INTO meta(k, v) VALUES ('schema', ?)", (v,))
        elif v == str(LEGACY_SCHEMA) and self._bulk:
            w.execute("DROP TABLE IF EXISTS names")
            legacy_table = False
            v = str(SCHEMA_VERSION)
            w.execute("UPDATE meta SET v = ? WHERE k = 'schema'", (v,))
        if v == str(SCHEMA_VERSION):
            w.executescript(FTS_SCHEMA)
            if not self._bulk:
                w.executescript(INDEXES + TRIGGERS)
            self._legacy_left = legacy_table
            self._fts_cache = (time.time(), "name_tri")
        elif v == str(LEGACY_SCHEMA):
            w.executescript(LEGACY_FTS_SCHEMA)
            if not self._bulk:
                w.executescript(INDEXES + LEGACY_TRIGGERS)
            self._fts_cache = (time.time(), "names")
        else:
            raise RuntimeError(f"macindex.db has schema {v}, expected {SCHEMA_VERSION}")

    @contextmanager
    def read(self):
        """A read-only connection from a small pool (None while there is no database yet)."""
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            conn = None
            if self.path.exists() and self._pool_n < 16:
                try:
                    conn = open_db(self.path, readonly=True)
                    self._pool_n += 1
                except sqlite3.Error:
                    conn = None
        try:
            yield conn
        finally:
            if conn is not None:
                self._pool.put(conn)

    def reader(self) -> sqlite3.Connection | None:
        """A private read-only connection (the caller closes it); None while there is no database."""
        if not self.path.exists():
            return None
        try:
            return open_db(self.path, readonly=True)
        except sqlite3.Error:
            return None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def close(self) -> None:
        with self.wlock:
            if self._w is not None:
                self._w.close()
                self._w = None
        while True:
            try:
                self._pool.get_nowait().close()
                self._pool_n -= 1
            except queue.Empty:
                break

    # ---- meta
    def get_meta(self, k: str, default=None):
        """Read through the reader pool, never the writer's lock: status polls must not wait for a long write (the
        index build at the end of the first load holds that lock for tens of seconds)."""
        with self.read() as c:
            if c is None:
                return default
            try:
                r = c.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
            except sqlite3.Error:
                return default
        return r[0] if r else default

    def set_meta(self, k: str, v) -> None:
        with self.wlock:
            self.writer().execute("INSERT INTO meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                                  (k, str(v)))

    def next_gen(self) -> int:
        with self.wlock:
            g = int(self.get_meta("gen", 0)) + 1
            self.set_meta("gen", g)
            return g

    # ---- bulk load (the first crawl)
    def in_bulk(self) -> bool:
        """True while the first load runs: the name index is not built yet, so searches answer nothing."""
        t, v = self._bulk_read
        if time.time() - t > 1.5:
            with self.read() as c:
                try:
                    r = c.execute("SELECT v FROM meta WHERE k = 'bulk'").fetchone() if c is not None else None
                except sqlite3.Error:
                    r = None
            v = bool(r and r[0] == "1")
            self._bulk_read = (time.time(), v)
        return v

    def begin_bulk(self) -> None:
        with self.wlock:
            w = self.writer()
            if self._schema_of(w) < 2 and w.execute("SELECT count(*) FROM files").fetchone()[0] == 0:
                # an empty schema-1 database about to be loaded: nothing to keep, so it simply becomes schema 2
                w.execute("DROP TABLE IF EXISTS names")
                w.execute("UPDATE meta SET v = ? WHERE k = 'schema'", (str(SCHEMA_VERSION),))
                for stmt in _statements(FTS_SCHEMA):
                    w.execute(stmt)
                self._legacy_left = False
                self._fts_cache = (time.time(), "name_tri")
            w.execute("BEGIN")
            w.execute("DROP TRIGGER IF EXISTS files_ai")
            w.execute("DROP TRIGGER IF EXISTS files_ad")
            for ix in SECONDARY_INDEXES:
                w.execute(f"DROP INDEX IF EXISTS {ix}")
            w.execute("INSERT INTO meta(k, v) VALUES ('bulk', '1') ON CONFLICT(k) DO UPDATE SET v = '1'")
            w.execute("COMMIT")
            self._bulk, self._bulk_read = True, (time.time(), True)

    def end_bulk(self) -> None:
        """Build the indexes and the trigram index in one pass, then switch the triggers back on."""
        with self.wlock:
            w = self.writer()
            w.executescript(INDEXES)
            if self._schema_of(w) >= 2:
                w.execute("INSERT INTO name_tri(name_tri) VALUES ('rebuild')")
                w.execute("INSERT INTO name_tri(name_tri) VALUES ('optimize')")
                self.refresh_stats()
                w.executescript(TRIGGERS)
            else:                                                 # a schema-1 database that was not emptied first
                w.execute("INSERT INTO names(names) VALUES ('rebuild')")
                w.executescript(LEGACY_TRIGGERS)
            w.execute("INSERT INTO meta(k, v) VALUES ('bulk', '0') ON CONFLICT(k) DO UPDATE SET v = '0'")
            w.execute("ANALYZE")
            self._bulk, self._bulk_read = False, (time.time(), False)

    # ---- the name index: which table answers, how it is counted, how an old one is replaced
    def schema_version(self) -> int:
        v = self.get_meta("schema")
        return int(v) if v and v.isdigit() else SCHEMA_VERSION

    def fts_name(self) -> str:
        """The table to ask for names: name_tri (schema 2) or, until migrate_fts has run, the first index."""
        t, v = self._fts_cache
        if time.time() - t > 1.0:
            v = "name_tri" if self.schema_version() >= 2 else "names"
            self._fts_cache = (time.time(), v)
        return v

    def fts_pending(self) -> bool:
        """True for a finished schema-1 index that migrate_fts() should replace."""
        return (self.path.exists() and self.get_meta("schema") == str(LEGACY_SCHEMA) and not self.in_bulk())

    def refresh_stats(self, sl: int | None = None) -> int:
        """Recount tri_stats from the name index's own vocabulary: all of it, or one slice of the key space (STAT_SLICES).
        Pieces held by fewer than STAT_MIN names are left out: a missing piece reads as rare, which is all the planner
        needs to know. Readers see the old counts or the new ones, never half of each. Returns the rows now in the table."""
        with self.wlock:
            w = self.writer()
            if self._schema_of(w) < 2:
                return 0
            w.execute("BEGIN IMMEDIATE")
            try:
                if sl is None:
                    w.execute("DELETE FROM tri_stats")
                    w.execute("INSERT INTO tri_stats SELECT term, doc FROM name_tri_v WHERE doc >= ?", (STAT_MIN,))
                else:
                    lo, hi = STAT_SLICES[sl % len(STAT_SLICES)]
                    if hi is None:
                        w.execute("DELETE FROM tri_stats WHERE tri >= ?", (lo,))
                        w.execute("INSERT INTO tri_stats SELECT term, doc FROM name_tri_v WHERE term >= ? AND doc >= ?",
                                  (lo, STAT_MIN))
                    else:
                        w.execute("DELETE FROM tri_stats WHERE tri >= ? AND tri < ?", (lo, hi))
                        w.execute("INSERT INTO tri_stats SELECT term, doc FROM name_tri_v WHERE term >= ? AND term < ?"
                                  " AND doc >= ?", (lo, hi, STAT_MIN))
                w.execute("COMMIT")
            except BaseException:
                w.execute("ROLLBACK")
                raise
            if sl is None:
                self._dirty = 0
                self._stats_built = True
            return w.execute("SELECT count(*) FROM tri_stats").fetchone()[0]

    @staticmethod
    def _schema_of(w: sqlite3.Connection) -> int:
        r = w.execute("SELECT v FROM meta WHERE k = 'schema'").fetchone()
        return int(r[0]) if r and str(r[0]).isdigit() else SCHEMA_VERSION

    def stats_due(self, entries: int | None = None) -> bool:
        """Enough rows changed since the last recount to be worth another (or there is no recount at all yet)."""
        n = self.count() if entries is None else entries
        if n <= 0 or self.schema_version() < 2:
            return False
        if self._dirty >= max(STATS_EVERY_MIN, n // 50):
            return True
        if self._stats_built:
            return False
        with self.read() as c:                              # no counts at all (a failed first recount): make them
            try:
                return c is not None and c.execute("SELECT 1 FROM tri_stats LIMIT 1").fetchone() is None
            except sqlite3.Error:
                return False

    def migrate_fts(self, chunk: int = 50000, on_progress=None) -> dict:
        """Replace the schema-1 name index with the schema-2 one without ever hiding the index: the old table keeps
        answering searches (readers are not blocked by the writer) until the last statement, which switches the triggers,
        the schema flag and the counts in one transaction. Until then the new table is built in chunks of `chunk` rows by
        the one thread that holds the write lock, so no row can change underneath it. A crash before the switch leaves
        schema 1 and a half-built new table that the next call drops and starts over; nothing is lost, the index is only
        derived data. Returns {migrated, entries, seconds}. The old table is dropped later (drop_legacy_fts)."""
        t0 = time.time()
        with self.wlock:
            w = self.writer()
            if self._schema_of(w) >= 2:
                return {"migrated": False, "reason": "already current", "entries": 0, "seconds": 0.0}
            self.upgrade = {"state": "running", "phase": "preparing", "fraction": 0.0, "started": t0, "entries": 0}
            try:
                for t in ("tri_stats", "name_tri_v", "name_tri"):
                    w.execute(f"DROP TABLE IF EXISTS {t}")              # a half-built table of an earlier attempt
                for stmt in _statements(FTS_SCHEMA):
                    w.execute(stmt)
                lo, hi = w.execute("SELECT min(id), max(id) FROM files").fetchone()
                entries = w.execute("SELECT count(*) FROM files").fetchone()[0]
                self.upgrade.update(phase="building the new name index", entries=entries)
                done = 0
                if lo is not None:
                    pos = lo
                    while pos <= hi:
                        end = pos + chunk
                        w.execute("BEGIN IMMEDIATE")
                        try:
                            done += w.execute("INSERT INTO name_tri(rowid, nf) SELECT id, nf FROM files WHERE id >= ?"
                                              " AND id < ? ORDER BY id", (pos, end)).rowcount
                            w.execute("COMMIT")
                        except BaseException:
                            w.execute("ROLLBACK")
                            raise
                        pos = end
                        frac = min(1.0, (pos - lo) / max(1, hi - lo + 1))
                        self.upgrade.update(fraction=round(0.9 * frac, 3), done=done)
                        if on_progress:
                            on_progress(self.upgrade)
                # Built in chunks the index is many segments, and every later small write would merge some of them
                # (a 100-file batch took 20-100 ms instead of 3). One merge into a single segment is cheap (~1 s on
                # two million names).
                self.upgrade.update(phase="merging the new index", fraction=0.9)
                w.execute("INSERT INTO name_tri(name_tri) VALUES ('optimize')")
                self.upgrade.update(phase="counting the pieces", fraction=0.95)
                w.execute("BEGIN IMMEDIATE")
                try:
                    w.execute("INSERT INTO tri_stats SELECT term, doc FROM name_tri_v WHERE doc >= ?", (STAT_MIN,))
                    self.upgrade.update(phase="switching over", fraction=0.98)
                    w.execute("DROP TRIGGER IF EXISTS files_ai")
                    w.execute("DROP TRIGGER IF EXISTS files_ad")
                    for stmt in _statements(TRIGGERS):
                        w.execute(stmt)
                    w.execute("INSERT INTO meta(k, v) VALUES ('schema', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                              (str(SCHEMA_VERSION),))
                    w.execute("COMMIT")
                except BaseException:
                    w.execute("ROLLBACK")
                    raise
            except BaseException as e:
                self.upgrade = {"state": "failed", "error": f"{type(e).__name__}: {e}", "started": t0,
                                "elapsed_s": round(time.time() - t0, 1)}
                raise
            self._fts_cache = (time.time(), "name_tri")
            self._legacy_left = True
            self._dirty = 0
            self.upgrade = {"state": "done", "entries": entries, "elapsed_s": round(time.time() - t0, 1),
                            "finished": time.time()}
            try:
                w.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass
            return {"migrated": True, "entries": entries, "seconds": round(time.time() - t0, 2)}

    def optimize_names(self) -> bool:
        """Merge the name index into one segment (after a big change such as a crawl; about a second per two million
        names). Searches keep answering while it runs."""
        with self.wlock:
            w = self.writer()
            if self._schema_of(w) < 2:
                return False
            w.execute("INSERT INTO name_tri(name_tri) VALUES ('optimize')")
            return True

    def drop_legacy_fts(self) -> bool:
        """Drop the schema-1 table once nothing reads it any more (schema 2 and a grace period after the switch).
        Its pages go to the free list for the new index to reuse; the file does not shrink by itself."""
        with self.wlock:
            w = self.writer()
            if self._schema_of(w) < 2 or not self._has_table(w, "names"):
                self._legacy_left = False
                return False
            w.execute("DROP TABLE IF EXISTS names")
            self._legacy_left = False
            return True

    # ---- writes
    def upsert(self, rows: list[tuple], lean: bool | None = None) -> None:
        """rows: make_row() tuples. One transaction; the vocabulary of new name words is kept up to date. `lean`: do not
        load the index's whole vocabulary to find the new words (INSERT OR IGNORE finds them in the table instead): right
        for live changes, wrong for a crawl, which tests millions of words. Default: lean for a small batch."""
        if not rows:
            return
        with self.wlock:
            c = self.writer()
            new = []
            if lean is None:
                lean = len(rows) <= SMALL_BATCH
            if self._vocab is None and lean and not self._bulk:
                # a few changed files on a quiet index (FSEvents): the first one must not load the whole vocabulary of
                # the index just to find out which words are new. INSERT OR IGNORE below does the same job.
                new = sorted({(t, t[::-1]) for r in rows for t in name_tokens(r[0].rsplit("/", 1)[-1])})
            else:
                if self._vocab is None:
                    self._vocab = {r[0] for r in c.execute("SELECT tok FROM vocab")}
                for r in rows:
                    for t in name_tokens(r[0].rsplit("/", 1)[-1]):
                        if t not in self._vocab:
                            self._vocab.add(t)
                            new.append((t, t[::-1]))
            self._dirty += len(rows)
            c.execute("BEGIN")
            try:
                if self._bulk:                           # first load: every row is new, no secondary indexes to churn
                    c.executemany(UPSERT, rows)
                else:
                    # a re-crawl re-sees every row: new ones are inserted, the rest only get their (unindexed) gen
                    # stamped, and size/time/kind are rewritten only for rows that really changed -- SET on an
                    # indexed column would re-write its index entries even for the same value
                    c.executemany(INSERT_NEW, rows)
                    c.executemany("UPDATE files SET gen = ? WHERE path = ? AND gen != ?",
                                  [(r[10], r[0], r[10]) for r in rows])
                    c.executemany(UPDATE_CHANGED, [(r[3], r[4], r[5], r[6], r[7], r[8], r[0], r[3], r[4], r[5], r[7], r[8])
                                                   for r in rows])
                if new:
                    c.executemany("INSERT OR IGNORE INTO vocab(tok, rtok) VALUES (?, ?)", new)
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    def delete_tree(self, path: str) -> int:
        """The entry and everything below it."""
        lo, hi = filequery._path_range(path)
        with self.wlock:
            c = self.writer()
            c.execute("BEGIN")
            n = c.execute("DELETE FROM files WHERE path = ? OR (path >= ? AND path < ?)", (path, lo, hi)).rowcount
            c.execute("COMMIT")
            self._dirty += n
        return n

    def sweep(self, root: str, gen: int, chunk: int = 5000) -> int:
        """Delete what a finished crawl of `root` did not see again (gen older than the crawl's)."""
        lo, hi = filequery._path_range(root)
        total = 0
        while True:
            with self.wlock:
                c = self.writer()
                c.execute("BEGIN")
                # a crawl never lists a Keynote deck (--skip-name *.key), so the decks that live events added are not swept
                n = c.execute("DELETE FROM files WHERE id IN (SELECT id FROM files WHERE path >= ? AND path < ?"
                              " AND gen < ? AND path NOT LIKE '%.key' LIMIT ?)", (lo, hi, gen, chunk)).rowcount
                c.execute("COMMIT")
                self._dirty += n
            total += n
            if n < chunk:
                break
            time.sleep(0.01)
        return total

    def sweep_outside(self, roots: list[str], chunk: int = 5000) -> int:
        """Delete every row that is not under one of the current roots (a root was removed from the settings)."""
        if not roots:
            return 0
        conds, params = [], []
        for r in roots:
            lo, hi = filequery._path_range(r)
            conds.append("(path = ? OR (path >= ? AND path < ?))")
            params += [r.rstrip("/") or "/", lo, hi]
        total = 0
        while True:
            with self.wlock:
                c = self.writer()
                c.execute("BEGIN")
                n = c.execute(f"DELETE FROM files WHERE id IN (SELECT id FROM files WHERE NOT ({' OR '.join(conds)})"
                              f" LIMIT {chunk})", params).rowcount
                c.execute("COMMIT")
                self._dirty += n
            total += n
            if n < chunk:
                return total
            time.sleep(0.01)

    def purge(self, excl: Excluder, chunk: int = 5000) -> int:
        """Delete every stored path the rules now block (rules changed, or a fake feed slipped one in)."""
        n = 0
        with self.wlock:
            c = self.writer()
            for p in excl.hard + excl.soft:
                n += self.delete_tree(p)
            rows = c.execute("SELECT id, path FROM files").fetchall() if self._small() else []
            bad = [r[0] for r in rows if excl.blocked(r[1])]
            for i in range(0, len(bad), 900):
                part = bad[i:i + 900]
                c.execute("BEGIN")
                c.execute(f"DELETE FROM files WHERE id IN ({','.join('?' * len(part))})", part)
                c.execute("COMMIT")
            n += len(bad)
        return n

    def _small(self) -> bool:
        return self.count() < 400000

    def count(self, fresh: bool = False) -> int:
        t, n = self._count
        if fresh or time.time() - t > 30 or t == 0.0:
            with self.read() as r:
                if r is not None:
                    n = r.execute("SELECT count(*) FROM files").fetchone()[0]
                    self._count = (time.time(), n)
        return n


# ------------------------------------------------------------------ walking (when the helper is missing)

def walk_python(root: str, excl: Excluder, max_depth: int = 0):
    """The helper's crawl in Python (slower): the same rows, the same rules, symlinks not followed, one volume."""
    try:
        dev = os.lstat(root).st_dev
    except OSError:
        return
    todo = collections.deque([(root, 0)])
    while todo:
        d, depth = todo.popleft()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not (S.S_ISREG(st.st_mode) or S.S_ISDIR(st.st_mode) or S.S_ISLNK(st.st_mode)):
                    continue
                if excl.blocked(e.path):
                    continue
                is_dir = S.S_ISDIR(st.st_mode)
                yield {"path": e.path, "name": e.name, "is_dir": is_dir, "size": 0 if is_dir else st.st_size,
                       "mtime": int(st.st_mtime), "ctime": int(getattr(st, "st_birthtime", st.st_ctime))}
                if is_dir and st.st_dev == dev and not getattr(st, "st_flags", 0) & 0x40000000 \
                        and (not max_depth or depth + 1 < max_depth):
                    ext = e.name.rpartition(".")[2].lower() if "." in e.name else ""
                    if ext not in filequery.PACKAGE_EXTS and ext != "key":      # a deck is one entry, not walked into
                        todo.append((e.path, depth + 1))


def helper_rows(root: str, excl: Excluder, max_depth: int = 0, proc_holder: list | None = None, stats: dict | None = None):
    """Rows from `msehelper crawl`. The helper's own lines {"done": …} / {"denied": …} go to `stats`."""
    args = [str(helper_path()), "crawl", root, "--max-depth", str(max_depth)] + excl.helper_args()
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    if proc_holder is not None:
        proc_holder.append(proc)
    reader = io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace")
    done = False
    try:
        for line in reader:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if "path" in d:
                yield d
            elif stats is not None:
                if d.get("done"):
                    stats.update({k: d.get(k, 0) for k in ("entries", "dirs", "denied", "skipped")})
                elif "denied" in d:
                    stats.setdefault("denied_paths", []).append(d["denied"])
            if d.get("done"):
                done = True
        if not done:                          # never mistake a failed walk for an empty folder (the sweep would delete)
            raise RuntimeError(f"the helper's crawl of {root} did not finish")
    finally:
        reader.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()


# ------------------------------------------------------------------ ingest

def ingest(store: Store, rows, gen: int, excl: Excluder, on_batch=None, stop: threading.Event | None = None,
           throttle: float = 0.0, lean: bool | None = None) -> int:
    """Store a stream of crawl rows ({"path", "is_dir", "size", "mtime", "ctime"} dicts). Anything the rules block
    is dropped here, whatever produced it. Returns the number of rows stored."""
    batch, n = [], 0
    for d in rows:
        if stop is not None and stop.is_set():
            break
        p = d.get("path")
        if not isinstance(p, str) or not p.startswith("/") or excl.blocked(p):
            continue
        batch.append(make_row(p, bool(d.get("is_dir")), d.get("size") or 0, d.get("mtime") or 0, d.get("ctime") or 0, gen))
        if len(batch) >= BATCH:
            t = time.time()
            store.upsert(batch, lean=lean)
            n += len(batch)
            batch = []
            if on_batch:
                on_batch(n)
            if throttle:
                time.sleep((time.time() - t) * throttle)
    if batch:
        store.upsert(batch, lean=lean)
        n += len(batch)
        if on_batch:
            on_batch(n)
    return n


def apply_events(store: Store, events: dict[str, int], excl: Excluder, scan=None) -> dict:
    """FSEvents -> index. events: {path: OR of flags}. A path that is gone is deleted (with everything below);
    one that exists is upserted; a new or renamed folder is scanned with `scan(path, gen)` (its contents);
    MustScanSubDirs / dropped events rescan the folder."""
    out = {"upserted": 0, "deleted": 0, "scanned": 0}
    gen = store.next_gen()
    rows, scans = [], []
    for path, flags in events.items():
        path = path.rstrip("/") or "/"
        if excl.blocked(path) and not gone_keynote(path):
            continue
        if flags & MUST_SCAN:
            scans.append(path)
            continue
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            out["deleted"] += store.delete_tree(path)
            continue
        except OSError:
            continue
        rows.append(row_from_stat(path, st, gen))
        if S.S_ISDIR(st.st_mode) and flags & (F_CREATED | F_RENAMED):
            scans.append(path)
    store.upsert(rows, lean=True)
    out["upserted"] = len(rows)
    for path in scans:
        if scan is not None and os.path.isdir(path):
            out["scanned"] += scan(path, gen)
    return out


# ------------------------------------------------------------------ the manager (crawl, watch, status)

class MacIndex:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self.started = False
        self.stop_flag = threading.Event()
        self.crawl_stop = threading.Event()
        self.crawling = False
        self.progress: dict | None = None
        self.last_update = 0.0
        self.last_error: str | None = None
        self.force = False
        self.retry_at = 0.0
        self.pending: dict[str, int] = {}
        self.plock = threading.Lock()
        self.watcher: subprocess.Popen | None = None
        self.watch_roots: tuple = ()
        self.events_seen = 0
        self.applies = 0                                  # batches of events applied so far
        self._wake = threading.Event()                    # set by feed_event: something is waiting in `pending`
        self.crawl_thread: threading.Thread | None = None
        self.migrating = False
        self.frecency = frecency.Frecency(self.store.path.parent / "frecency.db")      # opened only when something is recorded or ranked
        self._gitignore: tuple | None = None              # (the Excluder it was made for, the matcher)
        self.t0 = time.time()
        self._excl: tuple | None = None
        self._fda: tuple = (0.0, None)

    # ---- rules
    def excluder(self) -> Excluder:
        cfg = settings()
        colls = [c["path"] for c in _config()["collections"] if c.get("enabled", True)]
        fda = self.fda()
        key = (json.dumps(cfg, sort_keys=True, default=str), tuple(colls), fda)
        if self._excl is None or self._excl[0] != key:
            skips = protected_skips(cfg.get("skip_protected", "auto"), fda, colls)
            roots = [_expand(r, home()) for r in cfg["roots"]]
            self._excl = (key, Excluder(cfg, protected=skips, roots=roots), skips)
        return self._excl[1]

    def fda(self) -> bool | None:
        t, v = self._fda
        if time.time() - t > 60:
            v = full_disk_access()
            self._fda = (time.time(), v)
        return v

    def roots(self) -> list[str]:
        out = []
        for r in settings()["roots"]:
            r = _expand(r, home())
            if os.path.isdir(r) and r not in out and self.excluder().root_allowed(r):
                out.append(r)
        return out

    # ---- lifecycle
    def start(self) -> None:
        """Called by the service (never at import time): supervises the crawl and the FSEvents watcher."""
        if self.started:
            return
        self.started = True
        for fn, name in ((self._supervise, "macindex"), (self._flush_loop, "macindex-events")):
            threading.Thread(target=fn, daemon=True, name=name).start()

    def stop(self) -> None:
        self.stop_flag.set()
        self._wake.set()
        self.crawl_stop.set()
        self._stop_watcher()
        self.frecency.close()

    def _log(self, msg: str) -> None:
        try:
            paths.LOGS.mkdir(parents=True, exist_ok=True)
            with open(paths.LOGS / "daemon.log", "a") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} macindex: {msg}\n")
        except OSError:
            pass

    def _supervise(self) -> None:
        while not self.stop_flag.is_set():
            try:
                cfg = settings()
                if not cfg["enabled"]:
                    self.crawl_stop.set()
                    self._stop_watcher()
                else:
                    self._watch(cfg)
                    self._maintain()
                    if (not self.crawling and not self.migrating and time.time() - self.t0 > CRAWL_DELAY_S
                            and self.crawl_due(cfg)):
                        self.start_crawl()
            except Exception:
                self.last_error = traceback.format_exc().splitlines()[-1]
                self._log(traceback.format_exc())
            self.stop_flag.wait(10)

    def crawl_due(self, cfg: dict) -> bool:
        if time.time() < self.retry_at:
            return False                                  # the last crawl failed: wait before trying again
        if self.force:
            return True
        try:
            state = json.loads(self.store.get_meta("crawl") or "{}")
        except ValueError:
            state = {}
        roots = self.roots()
        if not roots:
            return False
        if state.get("rules") != rules_hash(self.excluder(), roots):
            return True
        if not state.get("finished"):
            return True                                   # unfinished: resume
        every = float(cfg.get("rescan_every_h") or 0)
        return bool(every) and time.time() - state["finished"] > every * 3600

    # ---- the name index itself: the one-time upgrade, the counts that steer the query plan
    def _maintain(self) -> None:
        """One supervisor tick of housekeeping, never while a crawl or the upgrade holds the writer: replace an index of
        the first schema (in a thread of its own, the old one keeps answering), drop the old table a minute after the
        switch, and recount tri_stats a slice at a time once enough rows have changed."""
        st = self.store
        if self.crawling or self.migrating or not st.path.exists():
            return
        st.writer()                           # opens it: a leftover first index is noticed there
        if st.fts_pending():
            if time.time() < self.retry_at:
                return                                        # the last attempt failed: not again at once
            self.migrating = True
            threading.Thread(target=self._migrate_main, daemon=True, name="macindex-upgrade").start()
            return
        if st._legacy_left:
            up = st.upgrade
            if up.get("state") != "done" or time.time() - up.get("finished", 0) > LEGACY_GRACE_S:
                st.drop_legacy_fts()
                self._log("dropped the first name index (its pages are free for the new one)")
        elif st.stats_due():
            for i in range(len(STAT_SLICES)):
                if self.stop_flag.is_set() or self.crawling:
                    break
                st.refresh_stats(i)
                self.stop_flag.wait(0.05)
            st._dirty = 0

    def _migrate_main(self) -> None:
        try:
            def alive(_p):
                if self.stop_flag.is_set():
                    raise InterruptedError("the service is stopping")
            with background_qos():
                r = self.store.migrate_fts(on_progress=alive)
            self._log(f"name index upgraded: {r}")
            self.last_update = time.time()
        except InterruptedError:
            self._log("name index upgrade interrupted; it starts over next time (the old index kept working)")
        except Exception:
            self.last_error = traceback.format_exc().splitlines()[-1]
            self._log(traceback.format_exc())
            self.retry_at = time.time() + 300
        finally:
            self.migrating = False

    # ---- crawl
    def start_crawl(self, force: bool = False) -> dict:
        if not settings()["enabled"]:
            return {"started": False, "reason": "disabled"}
        if self.crawling:
            return {"started": False, "reason": "already crawling"}
        if force:
            self.force = True
        self.crawl_stop.clear()
        self.crawling = True
        self.progress = {"phase": "starting", "units_done": 0, "units_total": 0, "entries": 0, "fraction": 0.0,
                         "current": None, "started_at": time.time()}
        self.crawl_thread = threading.Thread(target=self._crawl_main, daemon=True, name="macindex-crawl")
        self.crawl_thread.start()
        return {"started": True}

    def _units(self, roots: list[str], excl: Excluder) -> list[dict]:
        units = []
        for root in roots:
            units.append({"path": root, "depth": 1, "root": root})        # the root's own entries
            try:
                with os.scandir(root) as it:
                    kids = sorted(e.path for e in it if e.is_dir(follow_symlinks=False))
            except OSError:
                kids = []
            units += [{"path": k, "depth": 0, "root": root} for k in kids if not excl.blocked(k)]
        return units

    def _crawl_main(self) -> None:
        store = self.store
        try:
            roots = self.roots()
            excl = self.excluder()
            rhash = rules_hash(excl, roots)
            try:
                state = json.loads(store.get_meta("crawl") or "{}")
            except ValueError:
                state = {}
            resume = (state.get("rules") == rhash and not state.get("finished") and not self.force and state.get("gen"))
            if resume:
                gen, done = int(state["gen"]), set(state.get("done") or [])
            else:
                if state.get("rules") not in (None, rhash):
                    n = store.purge(excl) + store.sweep_outside(roots)   # nothing the new rules block, or outside the roots, stays
                    self._log(f"rules changed: purged {n} rows")
                gen, done = store.next_gen(), set()
                state = {"gen": gen, "rules": rhash, "roots": roots, "started": time.time(), "done": [], "finished": None}
                store.set_meta("crawl", json.dumps(state))
                if store.count(fresh=True) == 0:
                    store.begin_bulk()                         # the first load: build the indexes once, at the end
            self.force = False
            units = self._units(roots, excl)
            total = len(units)
            self.progress.update(phase="crawling", units_total=total, units_done=len(done))
            use_helper = "crawl" in helper_features()
            denied = int(state.get("denied") or 0)
            entries = int(store.count(fresh=True))
            counted = [0]
            for u in units:
                if self.crawl_stop.is_set() or self.stop_flag.is_set():
                    raise InterruptedError
                key = f"{u['path']}|{u['depth']}"
                if key in done:
                    continue
                self.progress.update(current=u["path"])
                r_excl = excl
                stats: dict = {}
                if u["depth"] == 1:                                 # the root itself is an entry, too
                    try:
                        store.upsert([row_from_stat(u["root"], os.lstat(u["root"]), gen)])
                    except OSError:
                        pass
                rows = (helper_rows(u["path"], r_excl, u["depth"], stats=stats) if use_helper
                        else walk_python(u["path"], r_excl, u["depth"]))
                base = counted[0]

                def on_batch(n, base=base):
                    counted[0] = base + n
                    self.progress.update(entries=entries + counted[0])
                    self.last_update = time.time()
                ingest(store, rows, gen, r_excl, on_batch=on_batch, stop=self.crawl_stop, throttle=0.5)
                if self.crawl_stop.is_set():
                    raise InterruptedError
                denied += int(stats.get("denied") or 0)
                done.add(key)
                state["done"] = sorted(done)
                state["denied"] = denied
                store.set_meta("crawl", json.dumps(state))
                self.progress.update(units_done=len(done), fraction=round(len(done) / max(1, total), 3))
            if store.in_bulk() or store._bulk:
                self.progress.update(phase="building the name index")
                store.end_bulk()
            self.progress.update(phase="cleaning up")
            for root in roots:
                store.sweep(root, gen)
            state["finished"] = time.time()
            store.set_meta("crawl", json.dumps(state))
            self.last_update = time.time()
            store.count(fresh=True)
            try:
                if store.schema_version() >= 2:
                    store.optimize_names()                     # a crawl rewrote a lot: one segment again
                    store.refresh_stats()                      # and the counts that steer the query plan
            except sqlite3.Error:
                pass
        except InterruptedError:
            self._log("crawl stopped; it resumes later")
        except Exception:
            self.last_error = traceback.format_exc().splitlines()[-1]
            self.retry_at = time.time() + 300
            self._log(traceback.format_exc())
        finally:
            self.crawling = False
            self.progress = None

    # ---- FSEvents
    def _watch(self, cfg: dict) -> None:
        roots = tuple(self.roots())
        alive = self.watcher is not None and self.watcher.poll() is None
        if roots != self.watch_roots or (roots and not alive):
            self._stop_watcher()
            self.watch_roots = roots
            if roots and helper_path().exists():
                feats = helper_features()
                fast = ["--latency", str(WATCH_LATENCY_S)] if "watch_latency" in feats else []
                if "watch_nodefer" in feats:             # the first change after a quiet spell is reported at once
                    fast += ["--no-defer"]
                if "watch_exclude" in feats:             # keep the churn of ~/Library etc. out of the event stream
                    for p in self.watch_excludes(list(roots)):
                        fast += ["--exclude", p]
                self.watcher = subprocess.Popen([str(helper_path()), "watch", *roots, *fast],
                                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.DEVNULL, text=True)
                threading.Thread(target=self._read_events, args=(self.watcher,), daemon=True, name="macindex-fsevents").start()

    def watch_excludes(self, roots: list[str]) -> list[str]:
        """At most 8 existing folders inside the watched roots that FSEvents should not report at all: what the
        rules exclude and apps churn through (~/Library first). Python still checks every event it does get."""
        excl = self.excluder()
        out = []
        for p in excl.soft + excl.hard:
            if p in out or not os.path.isdir(p) or p.lower() in excl._carve_set:
                continue
            if any(p.lower().startswith(c) for c in excl._carve) or any(c.startswith(p.lower() + "/") for c in excl._carve_set):
                continue                                   # a root sits inside it: its events are wanted
            if any(p.startswith(r.rstrip("/") + "/") for r in roots):
                out.append(p)
        return out[:8]

    def _stop_watcher(self) -> None:
        p, self.watcher = self.watcher, None
        if p and p.poll() is None:
            try:
                p.stdin.close()
                p.wait(timeout=3)
            except Exception:
                p.kill()
        if p and p.stdout:
            try:
                p.stdout.close()
            except OSError:
                pass

    def _read_events(self, proc) -> None:
        excl = self.excluder()
        n = 0
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if "path" not in ev:
                continue
            n += 1
            if n % 5000 == 0:
                excl = self.excluder()
            self.feed_event(ev["path"], int(ev.get("flags", 0)), excl)

    def feed_event(self, path: str, flags: int, excl: Excluder | None = None) -> None:
        excl = excl or self.excluder()
        p = path.rstrip("/") or "/"
        if excl.blocked(p) and not gone_keynote(p):
            return                                                    # FSEvents reports ~/.ssh too: dropped here
        with self.plock:
            self.pending[path] = self.pending.get(path, 0) | flags
            self.events_seen += 1
        self._wake.set()

    def _flush_loop(self) -> None:
        """Apply what FSEvents reported. Event driven: nothing runs while nothing changes (no polling), the first event of a
        burst starts a FLUSH_COALESCE_S window for the rest of it, and the next apply waits at least FLUSH_MIN_GAP_S, or four
        times as long as the last one took. So one new file is searchable ~25 ms after the helper reports it, and a burst
        of thousands (a build, a checkout) costs a few transactions, not one per event, at under a fifth of a core."""
        while not self.stop_flag.is_set():
            if not self._wake.wait(1.0):                  # woken by feed_event; the timeout only lets a stop request through
                continue
            if self.stop_flag.wait(FLUSH_COALESCE_S):
                return
            self._wake.clear()
            with self.plock:
                batch, self.pending = self.pending, {}
            if not batch:
                continue
            t = time.perf_counter()
            try:
                self.apply(batch)
                self.applies += 1
            except Exception:
                self.last_error = traceback.format_exc().splitlines()[-1]
                self._log(traceback.format_exc())
            if self.stop_flag.wait(min(2.0, max(FLUSH_MIN_GAP_S, 4 * (time.perf_counter() - t)))):
                return

    def apply(self, batch: dict[str, int]) -> dict:
        excl = self.excluder()
        helper = "crawl" in helper_features()

        def scan(path: str, gen: int) -> int:
            r_excl = excl
            rows = helper_rows(path, r_excl) if helper else walk_python(path, r_excl)
            n = ingest(self.store, rows, gen, r_excl, lean=True)       # live change: no whole-vocabulary load
            self.store.sweep(path, gen)
            return n
        out = apply_events(self.store, batch, excl, scan)
        self.last_update = time.time()
        return out

    # ---- queries
    def searchable(self) -> bool:
        """False until the first crawl has put names in the index: while it builds the index in bulk, and before it has
        started at all (an empty database is "not indexed yet", never "no matches")."""
        if self.store.in_bulk():
            return False
        try:
            if json.loads(self.store.get_meta("crawl") or "{}").get("finished"):
                return True
        except (ValueError, sqlite3.Error):
            pass
        return self.store.count() > 0

    def search(self, query: str, limit: int = filequery.DEFAULT_LIMIT, scope_roots: list[str] | None = None,
               collections: list[dict] | None = None, offset: int = 0, paged: bool = False) -> dict:
        t0 = time.time()
        cfg = settings()
        q = filequery.parse_query(query, home=home(), smart_case=bool(cfg["smart_case"]))
        excl = self.excluder()
        allow = lambda p: not excl.blocked(p)                  # noqa: E731
        if cfg["respect_gitignore"]:
            gi = self.gitignore(excl)
            allow = lambda p: not excl.blocked(p) and gi.keep(p)     # noqa: E731  (the ignore files of a blocked path are never read)
        hints = self.frecency.for_query(query) if cfg["frecency"] and not os.environ.get("MSE_LOCK") else None
        kw = dict(allow=allow, budget_ms=cfg["regex_budget_ms"], offset=offset, paged=paged,
                  folder_words=bool(cfg["match_folder_names"]), hints=hints)
        fts = self.store.fts_name()                            # these two read the meta table through the pool: ask them
        ready = self.searchable()                              # before this search holds a pooled connection of its own
        with self.store.read() as conn:
            if conn is None or not ready:                      # no index yet / still being built: not "no matches"
                res = {"items": [], "total": 0, "truncated": False, "query": q.public(), "searchable": False}
            else:
                try:
                    res = filequery.search(conn, q, limit, scope_roots, fts=fts, **kw)
                except sqlite3.OperationalError as e:
                    if "no such table" not in str(e):
                        raise
                    self.store._fts_cache = (0.0, "")           # the index was switched under us: ask again which one
                    res = filequery.search(conn, q, limit, scope_roots, fts=self.store.fts_name(), **kw)
                res["searchable"] = True
        roots = sorted(((c["path"].rstrip("/"), c["name"]) for c in collections or []), key=lambda x: -len(x[0]))
        if roots:
            for it in res["items"]:
                for r, name in roots:
                    if it["path"] == r or it["path"].startswith(r + "/"):
                        it["collection"] = name
                        break
        res["ms"] = round((time.time() - t0) * 1000, 1)
        return res

    def gitignore(self, excl: Excluder) -> "gitignore.GitIgnore":
        """The ignore-file matcher for these rules (remade when the rules change: it must not read a newly blocked folder)."""
        g = self._gitignore
        if g is None or g[0] is not excl:
            g = self._gitignore = (excl, gitignore.GitIgnore(blocked=excl.blocked))
        return g[1]

    def opened(self, path, query: str | None = None) -> dict:
        """The UI opened `path` from the results (after typing `query`): remember it for the ranking. Refuses what the
        search itself would never show: a relative or odd path, anything the privacy rules block, anything outside the
        roots, anything not in the name index. -> {recorded, reason?}; ValueError for a bad request."""
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or len(path) > 4096 or _has_dotdot(path):
            raise ValueError("path must be an absolute path")
        if query is not None and not isinstance(query, str):
            raise ValueError("query must be text")
        if not settings()["frecency"] or os.environ.get("MSE_LOCK"):
            return {"recorded": False, "reason": "switched off (mac_index.frecency)"}
        p = _collapse(path)
        if self.excluder().blocked(p):
            raise ValueError("that path is not searchable")
        if not any(_covers(r, p) for r in self.roots()):
            raise ValueError("that path is outside the searched folders")
        with self.store.read() as conn:
            row = conn.execute("SELECT id FROM files WHERE path = ?", (p,)).fetchone() if conn is not None else None
        if row is None:
            return {"recorded": False, "reason": "not in the name index"}
        return {"recorded": bool(self.frecency.record(p, query, row[0]))}

    def name_index_status(self) -> dict:
        """What the name index is and how an upgrade of it is going: {schema, table, upgrade: {state: idle|running|done|failed,
        phase?, fraction?, elapsed_s?, entries?, error?}, legacy_table, stats_rows?}. While state is running the old index
        keeps answering, and the new one takes over in one step."""
        st = self.store
        up = dict(st.upgrade)
        if up.get("state") == "running":
            up["elapsed_s"] = round(time.time() - up.pop("started", time.time()), 1)
        up.pop("started", None)
        out = {"schema": st.schema_version() if st.path.exists() else SCHEMA_VERSION,
               "table": st.fts_name() if st.path.exists() else TRI_NAME, "upgrade": up,
               "upgrade_pending": bool(st.path.exists() and st.fts_pending()),
               "legacy_table": bool(st._legacy_left), "changed_since_recount": st._dirty}
        return out

    def status(self) -> dict:
        cfg = settings()
        excl = self.excluder()
        try:
            state = json.loads(self.store.get_meta("crawl") or "{}") if self.store.path.exists() else {}
        except (ValueError, sqlite3.Error):
            state = {}
        entries = self.store.count() if self.store.path.exists() else 0
        prog = dict(self.progress) if self.progress else None
        if prog:
            prog["elapsed_s"] = round(time.time() - prog.pop("started_at", time.time()))
        skipped = self._excl[2] if self._excl else []
        return {
            "enabled": cfg["enabled"], "roots": self.roots() if cfg["enabled"] else list(cfg["roots"]),
            "entries": entries, "crawling": self.crawling, "progress": prog,
            "last_update": self.last_update or (self.store.path.stat().st_mtime if self.store.path.exists() else None),
            "full_disk_access": self.fda(), "excluded": excl.describe(),
            "state": ("disabled" if not cfg["enabled"] else "crawling" if self.crawling
                      else "ready" if state.get("finished") else "waiting" if not self.started else "not crawled"),
            "last_crawl": state.get("finished"), "helper_present": helper_path().exists(),
            "watching": bool(self.watcher and self.watcher.poll() is None), "events": self.events_seen,
            "skipped_protected": skipped, "denied": state.get("denied", 0), "error": self.last_error,
            "searchable": self.searchable(), "helper_features": sorted(helper_features()) if helper_path().exists() else [],
            "helper_outdated": helper_path().exists() and "crawl" not in helper_features(),
            # for the settings screens: the interpreter macOS must trust with Full Disk Access (the file it resolves
            # to is the one the privacy list takes), the configured exclusions with what each means, and the
            # built-in rules that cannot be switched off
            "python_path": sys.executable, "python_real_path": os.path.realpath(sys.executable),
            "exclude_entries": exclude_entries(cfg), "hard_excluded": hard_groups(),
        }


_MANAGER: MacIndex | None = None
_MLOCK = threading.Lock()


def manager() -> MacIndex:
    global _MANAGER
    with _MLOCK:
        if _MANAGER is None or _MANAGER.store.path != db_path():
            _MANAGER = MacIndex()
        return _MANAGER
