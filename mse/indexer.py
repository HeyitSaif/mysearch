"""The indexing pipeline for one collection: scan → light extraction → heavy extraction → embedding → vocabulary."""

import bisect
import hashlib
import json
import os
import re
import shutil
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed, wait as wait_futures
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISDIR, S_ISREG

import numpy as np

from . import config, docmeta, extract, gitignore, paths, readers, store
from . import progress as liveprog
from .embedder import QUERY, doc_prompt, pool
from .ignore import (BUNDLE_DIR_EXT, CACHE_DIR, CACHE_PARENT, DEFAULT_EXCLUDES, FLOOR_DIRS, KEYNOTE_MIN_BYTES,
                     MARKER_EXTRA, PROJECT_MARKERS, SECRET_PATTERNS,
                     Matcher, content_skipped, effective_prune, is_key_name, secret_rules_without_key)
from .textutil import STOP, date_from_path, name_tokens

EXTRACT_VERSION = "2"     # bump when extraction/chunking changes: every file is re-read at the next scan
TEXT_MODALITIES = ("title", "text", "ocr", "transcript")
MEDIA_MODALITIES = ("image", "page", "audio", "video")
SF_DATALESS = 0x40000000      # st_flags: the content is in the cloud and not on this Mac (macOS File Provider)
MISSING_GRACE_H = 24          # a vanished file's rows stay this long before a later pass may remove them
NO_EMBED_F = " AND NOT " + store.name_only_unembeddable("f.")   # name-only code and binaries are never embedded
RULE_BATCH = 2000            # rows removed per transaction when a rule now excludes them
NAMES_BATCH = 500            # name-only rows written per transaction in the names-first pass
# The pure-Python readers, which run in a reader process with a time limit (readers.py). The others keep their own:
# PDF, image, audio and video go through helper and ffmpeg subprocesses that already have timeouts.
GUARDED_KINDS = frozenset({"text", "office", "email", "other"})
# A guarded kind that waits on a subprocess of its own gets that wait plus a margin, so its read is never cut short:
# iWork's preview helper waits up to 300 s, bsdtar 120 s, textutil 90 s. Other kinds get the configured reader limit.
READ_LIMIT_FLOOR_S = {"office": 330.0, "other": 150.0}
_lstat = os.lstat             # a seam for tests: a stat that reports the dataless flag


class Cancelled(Exception):
    pass


def docid(coll: str, rel: str) -> str:
    return hashlib.sha1(f"{coll}:{rel}".encode()).hexdigest()[:8]


def readable_path(rel: str) -> str:
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", rel)
    return re.sub(r"[_]+", " ", stem.replace("/", " / "))




def _base_patterns(coll: dict, settings: dict) -> list[str]:
    """The collection's exclusion patterns, apart from the secrets: DEFAULT_EXCLUDES (the floor HEAD always applied), the
    always-pruned folder names the setting allows, indexing.extra_excludes and the collection's own exclude list."""
    prune = effective_prune(settings.get("prune_dirs"))
    named = [n + "/" for n, rule in prune.items() if rule == "always" and n not in FLOOR_DIRS]
    return list(DEFAULT_EXCLUDES) + named + list(settings.get("extra_excludes") or []) + list(coll.get("exclude") or [])


def matchers(coll: dict, settings: dict | None = None) -> tuple[Matcher, Matcher]:
    settings = _settings() if settings is None else settings
    return Matcher(SECRET_PATTERNS + _base_patterns(coll, settings)), \
        Matcher(coll.get("metadata_only") or [])


def _flag(coll: dict, key: str, settings: dict, default: bool) -> bool:
    """A per-collection override (None inherits) of a global indexing setting."""
    v = coll.get(key)
    return bool(settings.get(key, default) if v is None else v)


_SECRET_ONLY = Matcher(SECRET_PATTERNS)


class _RepoIgnores(gitignore.GitIgnore):
    """gitignore.GitIgnore plus the repository's .git/info/exclude, at the lowest precedence, as git applies it. That one
    file is read inside .git, and only where .git is a folder (a worktree or a submodule keeps .git as a file)."""

    def __init__(self):
        super().__init__(blocked=_SECRET_ONLY.match)     # Matcher.match returns a bool
        self._info: dict[str, tuple[float, list]] = {}

    def _load(self, folder: str):
        rules = super()._load(folder)
        if not os.path.isdir(folder + "/.git"):
            return rules
        now = self._clock()
        hit = self._info.get(folder)
        if hit and now - hit[0] < gitignore.RECHECK_S:
            return hit[1]
        f = folder + "/.git/info/exclude"
        info = [] if self._blocked(f) else gitignore._read_rules(f)
        combined = info + rules                       # the folder's own ignore files override info/exclude
        self._info[folder] = (now, combined)
        return combined


def _bundle_name(name: str) -> bool:
    """A folder that is one entry and is never walked into: an app, a framework, an Xcode archive, a bundle package."""
    return "." in name and name.rsplit(".", 1)[1].lower() in BUNDLE_DIR_EXT


class _Scope:
    """One collection's rules for one pass. The full walk, the event path (index_paths, index_one), the survey behind
    `mse add` and the removal of rows that a rule now excludes all ask the same three questions, so none of them can
    disagree about a path:

    - name_reason: rules that read the path's text alone (a secret or exclusion pattern, an always-pruned folder name, a
      folder under a bundle or a Keynote package, Library/Caches). A row they exclude is removed at once, file on disk
      or not: the walk could never have listed it.
    - ancestor_reason: rules that read the folders above a path. A project or virtual-environment marker beside a folder
      named build, dist, env ..., and a nested repository (indexing.skip_nested_repos). The marker or the .git entry
      must be on disk; the row's own file need not be.
    - own_reason: rules decided by the file itself: the ignore files of its repository (gitignore) and a small Keynote
      `.key` file. Only asked of a path that is on disk.
    """

    def __init__(self, coll: dict, settings: dict | None = None):
        settings = _settings() if settings is None else settings
        self.coll = coll
        self.root = Path(coll["path"])
        self.root_s = str(self.root)
        self.prune = effective_prune(settings.get("prune_dirs"))
        self.exclude, self.meta_only = matchers(coll, settings)
        self.nokey = _nokey_matcher(coll, settings)
        self.max_bytes = int(coll.get("max_file_mb", 200)) * 1024 * 1024
        self.index_code = _flag(coll, "index_code", settings, False)
        self.respect_git = _flag(coll, "respect_gitignore", settings, True)
        self.nested_on = _flag(coll, "skip_nested_repos", settings, True)
        self.root_repo = os.path.lexists(self.root_s + "/.git")     # the collection itself is a repository
        self.pruned = Counter()          # folders pruned by name
        self.pruned_nested = Counter()   # nested repositories pruned, by kind: clone, worktree
        self.gitignored = 0              # entries a git ignore file leaves out (the walk does not enter them)
        self.bundles = 0                 # bundles listed as one name-only entry
        self._entries_cache: dict[str, set] = {}
        self._nested: dict[str, str | None] = {}
        self._ig = None

    # -- folder markers
    def _entries(self, folder: str) -> set:
        hit = self._entries_cache.get(folder)
        if hit is None:
            try:
                hit = {e.name for e in os.scandir(folder)}
            except OSError:
                hit = set()
            if len(self._entries_cache) > 50000:
                self._entries_cache.clear()
            self._entries_cache[folder] = hit
        return hit

    def _marker(self, folder: str, name: str) -> bool:
        ents = self._entries(folder)
        if ents & (PROJECT_MARKERS | MARKER_EXTRA.get(name, frozenset())):
            return True
        return any(e.endswith(".xcodeproj") for e in ents)

    def prune_hit(self, parent: str, name: str) -> bool:
        """Whether the folder `name` inside `parent` (an absolute path) is pruned, by its name and its markers."""
        if name == CACHE_DIR and os.path.basename(parent) == CACHE_PARENT:
            return True
        rule = self.prune.get(name)
        if rule is None:
            return False
        if rule == "always":
            return True
        if rule == "pyvenv":
            return os.path.isfile(os.path.join(parent, name, "pyvenv.cfg"))
        return self._marker(parent, name)

    # -- nested repositories
    def _inside_root(self, path: str) -> bool:
        try:
            base = os.path.realpath(self.root_s)
            real = os.path.realpath(path)
        except OSError:
            return False
        return real == base or real.startswith(base + os.sep)

    def nested_kind(self, full: str) -> str | None:
        """'clone' for a folder with its own .git folder, 'worktree' for a linked git worktree (a .git FILE that names
        <repository>/.git/worktrees/<name>), else None. A clone is pruned only when the collection root is itself a
        repository: a folder of separate projects is kept. A worktree is pruned when the collection root is a repository,
        or when the repository it was made from lies inside the collection (its checkout duplicates content already
        indexed here). Decided from the .git entry alone, and cached per folder."""
        if not self.nested_on:
            return None
        if full in self._nested:
            return self._nested[full]
        kind = None
        try:
            st = _lstat(full + "/.git")
        except OSError:
            st = None
        if st is not None:
            if S_ISDIR(st.st_mode) and self.root_repo:
                kind = "clone"
            elif S_ISREG(st.st_mode):
                main = gitignore.linked_worktree_repo(full + "/.git")
                if main is not None and (self.root_repo or self._inside_root(main)):
                    kind = "worktree"
        if len(self._nested) > 50000:
            self._nested.clear()
        self._nested[full] = kind
        return kind

    # -- git
    def git_ignored(self, full: str, is_dir: bool) -> bool:
        if not self.respect_git:
            return False
        if self._ig is None:
            self._ig = _RepoIgnores()
        return self._ig.keep(full, is_dir) is False

    def skip_git(self, full: str, is_dir: bool) -> bool:
        """A path a git ignore file leaves out: the walk does not enter it, and the rows it holds are removed by rule
        (own_reason). Counted."""
        if not self.git_ignored(full, is_dir):
            return False
        self.gitignored += 1
        return True

    # -- the three questions
    def name_reason(self, rel: str) -> str | None:
        parts = rel.split("/")
        for i, name in enumerate(parts[:-1]):
            if is_key_name(name) or _bundle_name(name):          # a Keynote package or a bundle is never walked into
                return "rule"
            parent = parts[i - 1] if i else os.path.basename(self.root_s)
            if name == CACHE_DIR and parent == CACHE_PARENT:
                return "rule"
            if self.prune.get(name) == "always":
                return "rule"
        if self.nokey.match(rel):
            return "rule"
        return None

    def ancestor_reason(self, rel: str, self_dir: bool = False) -> str | None:
        chain = rel.split("/") if self_dir else rel.split("/")[:-1]
        parent = self.root_s
        for name in chain:
            full = parent + "/" + name
            if self.prune_hit(parent, name):
                return "rule"
            if self.nested_kind(full):
                return "nested"
            parent = full
        return None

    def own_reason(self, rel: str, is_dir: bool) -> str | None:
        if self.git_ignored(os.path.join(self.root_s, rel), is_dir):
            return "gitignored"
        if _secret_excluded(rel, self.exclude, self.nokey, self.root):
            return "rule"
        return None

    def reason(self, rel: str, is_dir: bool = False) -> str | None:
        """Why the collection's rules keep this path out (rule, nested, gitignored), or None when it may be indexed."""
        return self.name_reason(rel) or self.ancestor_reason(rel, is_dir) or self.own_reason(rel, is_dir)

    def eligible(self, rel: str, is_dir: bool) -> bool:
        return self.reason(rel, is_dir) is None

    def walk_entries(self, start: str = "", errors: list | None = None, unreadable: set | None = None,
                     dataless: set | None = None):
        """(rel, size, mtime, ino) for each path the rules allow, under `start` (a folder relative to the collection
        root, "" for the root). Folders are pruned before the walk enters them. A Keynote package and a bundle are one
        entry each and are never walked into. Inside .git nothing is read, except .git/info/exclude."""
        errors = [] if errors is None else errors
        top = self.root_s + ("/" + start if start else "")

        def onerror(e):
            errors.append(str(e))
            if unreadable is not None:
                unreadable.add(_rel_folder(self.root, getattr(e, "filename", None)))

        for dirpath, dirnames, filenames in os.walk(top, onerror=onerror, followlinks=False):
            self._entries_cache[dirpath] = set(dirnames) | set(filenames)     # this folder's markers, free of charge
            reldir = os.path.relpath(dirpath, self.root_s)
            reldir = "" if reldir == "." else reldir + "/"
            keep, decks, bundles = [], [], []
            for d in dirnames:
                rel = reldir + d
                full = os.path.join(dirpath, d)
                if os.path.islink(full):
                    continue
                if is_key_name(d) and not _secret_excluded(rel, self.exclude, self.nokey, self.root):
                    decks.append(rel)                 # a Keynote package: one entry, and nothing inside it is walked
                    continue
                if _bundle_name(d) and not self.exclude.match(rel + "/"):
                    bundles.append(rel)               # an app, framework or archive bundle: one name-only entry
                    continue
                if self.prune_hit(dirpath, d):
                    self.pruned[d] += 1
                    continue
                kind = self.nested_kind(full)
                if kind:
                    self.pruned_nested[kind] += 1
                    continue
                if self.exclude.match(rel + "/"):
                    continue
                if self.skip_git(full, True):
                    continue
                keep.append(d)
            dirnames[:] = sorted(keep)
            for rel in decks:
                got = _deck_stats(self.root, rel, dataless)
                if got is None:
                    errors.append(f"cannot read {rel}")
                    if unreadable is not None:
                        unreadable.add(reldir.rstrip("/"))
                    continue
                yield rel, got[0], got[1], got[2]
            for rel in bundles:
                full = os.path.join(self.root_s, rel)
                try:
                    st = _lstat(full)
                except OSError as e:
                    errors.append(str(e))
                    continue
                if S_ISDIR(st.st_mode) and not self.skip_git(full, True):
                    self.bundles += 1
                    yield rel, 0, st.st_mtime, st.st_ino
            for f in filenames:
                rel = reldir + f
                if self.exclude.match(rel) and not _keynote_candidate(rel, self.nokey):
                    continue
                full = os.path.join(dirpath, f)
                try:
                    st = _lstat(full)
                except OSError as e:
                    errors.append(str(e))
                    if unreadable is not None:
                        unreadable.add(reldir.rstrip("/"))      # the file sits in this folder: its rows are kept
                    continue
                if not S_ISREG(st.st_mode):
                    continue                                    # a folder, a symlink or a special file
                if self.exclude.match(rel) and st.st_size <= KEYNOTE_MIN_BYTES:
                    continue                                    # a .key this small is a private key: a secret
                if self.skip_git(full, False):
                    continue
                if dataless is not None and _flag_dataless(st):
                    dataless.add(rel)
                yield rel, st.st_size, st.st_mtime, st.st_ino


def _kind_of_rel(rel: str) -> str:
    """A file's kind from its path: code (by extension, or by the name of a lockfile or bundle), else the extension's."""
    if extract.is_code_name(rel.rsplit("/", 1)[-1]):
        return "code"
    return extract.kind_of(_ext_of(rel))


def _name_prep(rel: str) -> dict:
    """The row of a name-only file: its path is the whole entry, nothing is read."""
    return {"ext": _ext_of(rel), "kind": _kind_of_rel(rel), "title": None, "lang": None, "pages": None, "heavy": None,
            "items": [], "error": None, "hash": None}


def _remove_rule_rows(conn, name: str, items: list[tuple[dict, str]], progress=None, cancel=None) -> Counter:
    """Rows a rule now excludes, each with its reason (nested, gitignored, rule). Their files are still on disk or the
    rule reads the path alone, so nothing is lost and there is no grace period and no mass-vanish count. Removed in
    batches of RULE_BATCH, each committed, so a search or an event read can run between them. A stop leaves the rest for
    the next pass. Returns the count per reason."""
    done: Counter = Counter()
    for i in range(0, len(items), RULE_BATCH):
        if cancel and cancel():
            break
        chunk = items[i:i + RULE_BATCH]
        _remove_rows(conn, name, [r for r, _why in chunk])
        conn.commit()
        for _r, why in chunk:
            done[why] += 1
        if progress:
            progress("scanning", sum(done.values()), len(items), "rules")
        time.sleep(0.005)
    return done


def _classify(scope: "_Scope", root: Path, rows: list[dict]) -> tuple[list[tuple[dict, str]], list[dict]]:
    """Split rows the walk did not list into those a rule excludes (with the reason) and the rest, which are missing.
    The path-only rules are asked first and need no disk read. The rules that read the folders above (markers, nested
    repositories) need no read of the row's own file. The rules decided by the file itself (gitignore, a small Keynote
    .key file) need the file on disk: a file that is gone is never removed by them, it waits in the missing path."""
    rule: list[tuple[dict, str]] = []
    gone: list[dict] = []
    for v in rows:
        why = scope.name_reason(v["path"]) or scope.ancestor_reason(v["path"])
        if why is None:
            try:
                st = _lstat(root / v["path"])
            except OSError:
                gone.append(v)
                continue
            why = scope.own_reason(v["path"], S_ISDIR(st.st_mode))
        if why:
            rule.append((v, why))
        else:
            gone.append(v)
    return rule, gone


def _names_first(conn, coll: dict, items: list, progress=None, cancel=None) -> int:
    """The names pass of a first scan: a row for every new file, from its path alone, before any file is read. Each file
    is searchable by name at once; the light pass that follows gives it its content."""
    done = 0
    for i in range(0, len(items), NAMES_BATCH):
        if cancel and cancel():
            break
        for w in items[i:i + NAMES_BATCH]:
            _store_light(conn, coll, w.rel, w.size, w.mtime, "meta", w.ino, _name_prep(w.rel))
            done += 1
        conn.commit()
        if progress:
            progress("scanning", done, len(items), "names")
    return done


def survey(coll: dict) -> dict:
    """What a first pass over this folder would do, from stat calls alone: nothing is read or written. For `mse add`."""
    scope = _Scope(coll)
    errs: list[str] = []
    entries = name_only = code_only = content_only = 0
    for rel, size, _mtime, _ino in scope.walk_entries(errors=errs):
        entries += 1
        if _mode(scope.meta_only, rel, size, scope.max_bytes, code_meta=not scope.index_code) == "meta":
            name_only += 1
            if not scope.index_code and _kind_of_rel(rel) == "code":
                code_only += 1
            elif content_skipped(rel, size):
                content_only += 1
    return {"entries": entries, "name_only": name_only, "code_name_only": code_only,
            "content_name_only": content_only, "pruned_folders": sum(scope.pruned.values()),
            "pruned": dict(scope.pruned.most_common(12)), "gitignored": scope.gitignored,
            "bundles": scope.bundles, "walk_errors": errs[:3]}



def walk(coll: dict, errors: list):
    """(rel, size, mtime) for every file the collection's rules allow."""
    for rel, size, mtime, _ino in _walk(coll, errors):
        yield rel, size, mtime


def _rel_folder(root: Path, p) -> str:
    """A folder in the form its rows use: relative to the root, no trailing slash, "" for the root itself. A path that
    is missing or lies outside the root counts as the root, the safe side."""
    rel = os.path.relpath(str(p), str(root)) if p else "."
    return "" if rel in (".", os.pardir) or rel.startswith(os.pardir + os.sep) else rel


def _under_unreadable(rel: str, unreadable: set[str]) -> bool:
    """True when a row's path lies inside a folder the walk could not read ("" is the root, which holds every row)."""
    return any(d == "" or rel.startswith(d + "/") for d in unreadable)


def _root_readable(root: Path) -> bool:
    try:
        os.scandir(root).close()
        return True
    except OSError:
        return False



def _nokey_matcher(coll: dict, settings: dict | None = None) -> Matcher:
    """The collection's rules without the *.key pattern: what still excludes a Keynote deck, whatever its size."""
    settings = _settings() if settings is None else settings
    return Matcher(secret_rules_without_key() + _base_patterns(coll, settings))


def _flag_dataless(st) -> bool:
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)


def _dataless(p) -> bool:
    """True when a file's content is in the cloud. Only the stat flags are read: no byte of the file is touched."""
    try:
        return _flag_dataless(_lstat(p))
    except OSError:
        return False


def _icloud_placeholder(rel: str) -> bool:
    """A legacy iCloud placeholder, '.<name>.icloud', stands in for a file not on this Mac: never read."""
    name = rel.rsplit("/", 1)[-1]
    return name.startswith(".") and name.endswith(".icloud") and len(name) > len(".icloud") + 1


def _keynote_candidate(rel: str, nokey: Matcher) -> bool:
    """Whether the only rule that excludes a path can be the *.key pattern: its own name ends in .key, no folder above
    it does, and no other rule excludes it."""
    parts = rel.split("/")
    return is_key_name(parts[-1]) and not any(is_key_name(x) for x in parts[:-1]) and not nokey.match(rel)


def _secret_excluded(rel: str, exclude: Matcher, nokey: Matcher, root: Path) -> bool:
    """Whether the collection's rules keep a path out. Only the *.key pattern is relaxed, and only for a Keynote deck: a
    folder named *.key (its package, indexed as one entry) or a regular file over KEYNOTE_MIN_BYTES. A .key of that size
    or less is a private key. Decided from stat alone; no content is read."""
    if not exclude.match(rel):
        return False
    if not _keynote_candidate(rel, nokey):
        return True
    try:
        st = _lstat(root / rel)
    except OSError:
        return True
    if S_ISDIR(st.st_mode):
        return False
    return not (S_ISREG(st.st_mode) and st.st_size > KEYNOTE_MIN_BYTES)


def _deck_stats(root: Path, rel: str, dataless: set | None) -> tuple[int, float, int] | None:
    """Size, newest date and inode of a Keynote package, from stat calls only. None when part of it cannot be read. A
    package with a member in the cloud is recorded in `dataless`."""
    top = root / rel
    try:
        st = _lstat(top)
    except OSError:
        return None
    size, mtime, errs, flagged = 0, st.st_mtime, [], False
    for dp, dn, fn in os.walk(top, onerror=errs.append, followlinks=False):
        for name in dn + fn:
            try:
                m = _lstat(os.path.join(dp, name))
            except OSError:
                errs.append(name)
                continue
            mtime = max(mtime, m.st_mtime)
            flagged = flagged or _flag_dataless(m)
            if S_ISREG(m.st_mode):
                size += m.st_size
    if errs:
        return None
    if flagged and dataless is not None:
        dataless.add(rel)
    return size, mtime, st.st_ino



def _walk(coll: dict, errors: list, unreadable: set | None = None, dataless: set | None = None):
    """(rel, size, mtime, ino) for each path the collection's rules allow (see _Scope.walk_entries)."""
    yield from _Scope(coll).walk_entries(errors=errors, unreadable=unreadable, dataless=dataless)


# ------------------------------------------------------------------ settings, failures, crash markers

FAILURE_LIMIT = 3            # a file that failed this many times in a row is skipped until its mtime or its extractor changes
XVER_PER_SCAN = 25           # extractor-version upgrades re-read per scan (daytime); the nightly run takes many more
XVER_PER_NIGHT = 2000
FILE_TIMEOUT_S = 420         # wall-clock limit for reading one file (the helper calls keep their own, shorter limits)
COMMIT_EVERY = 200           # files per transaction ...
COMMIT_SECONDS = 2.0         # ... or this long, whichever comes first
HASH_BACKFILL = 300          # old rows without a content hash hashed per scan (the nightly run does more)
HASH_BACKFILL_NIGHT = 5000
DOCMETA_BACKFILL = 500       # files without document facts (older database, new extractor version, OCR finished) done per scan ...
DOCMETA_BACKFILL_NIGHT = 20000   # ... and per night run (mse/docmeta.py)
INFLIGHT = "scan_inflight.json"
STOPPING = False             # set by the job manager's freeze() on a graceful stop: no more in-flight markers are written


class _Overdue(Exception):
    """A file's read ran longer than its limit. Its thread stays busy (a Python thread cannot be stopped)."""


@dataclass
class _Item:
    rel: str
    size: int
    mtime: float
    mode: str                   # full | meta (metadata-only: the name is the whole entry, nothing is read)
    ino: int | None
    reason: str                 # new | changed | forced | xver
    old: dict | None = None     # the file's row, when it has one
    move: dict | None = None    # set when this file has the same content as a row that vanished in this pass


def _settings() -> dict:
    try:
        return config.load().get("indexing") or {}
    except Exception:
        return {}


def _media(coll: dict) -> dict:
    """The media rules for one collection: its own media dict with the global defaults filled in where it says null
    (config.effective_media). What light() and heavy() are given."""
    return config.effective_media(coll)


# ------------------------------------------------------------------ the idle gate of the heavy pass and the embed loop
#
# Outside a manual "Sync now" and the night run, OCR, page renders, picture and media work and embedding batches run only
# while jobs.heavy_work_allowed(priority) says so (indexing.priority: low | balanced | always; "always" is never asked).
# When it says no, the stage pauses in short steps, keeps checking, stays cancellable, and `mse status` shows why.

PAUSE_STEP_S = 2.0                 # the longest one sleep while paused: a cancel or a change of state is seen within this
_PAUSE_LABEL = {"user_active": "user active", "on_battery": "on battery", "disabled": "disabled"}


def _priority(priority: str | None) -> str:
    """The priority that applies: the one the caller passed, else indexing.priority, else balanced."""
    if priority in config.PRIORITIES:
        return priority
    p = _settings().get("priority", "balanced")
    return p if p in config.PRIORITIES else "balanced"


def heavy_work_allowed(priority: str) -> tuple[bool, str | None]:
    """(allowed, reason) from jobs.heavy_work_allowed when this tree has it; "always" and a tree without the function
    mean allowed. The reason is None, "user_active", "on_battery" or "disabled"."""
    if priority == "always":
        return True, None
    from . import jobs
    fn = getattr(jobs, "heavy_work_allowed", None)
    if fn is None:
        return True, None
    try:
        allowed, reason = fn(priority, enabled=True)
    except Exception:
        return True, None
    return bool(allowed), (None if allowed else reason)


def _wait_for_heavy_work(name: str, priority: str, cancel, progress, stage: str, done: int, total: int) -> float:
    """Block until heavy work is allowed under `priority`. Sleeps in steps of at most PAUSE_STEP_S, re-checks after each,
    raises Cancelled as soon as `cancel` says so, and reports the pause (and its end) to the progress registry and the
    job's progress callback, so `mse status` reads e.g. `paused (user active)`. Returns the seconds paused."""
    if priority == "always":
        return 0.0
    paused_at = None
    shown = None
    while True:
        allowed, reason = heavy_work_allowed(priority)
        if allowed:
            if paused_at is not None:
                liveprog.pause(name, None)
                if progress:
                    progress(stage, done, total, "")
            return 0.0 if paused_at is None else time.monotonic() - paused_at
        if cancel and cancel():
            liveprog.pause(name, None)
            raise Cancelled()
        if paused_at is None:
            paused_at = time.monotonic()
        if reason != shown:
            shown = reason
            label = _PAUSE_LABEL.get(reason or "", reason or "waiting")
            liveprog.pause(name, label)
            if progress:
                progress(stage, done, total, f"paused ({label})")
        time.sleep(PAUSE_STEP_S)


def _worker_count() -> int:
    default = max(2, min(4, (os.cpu_count() or 4) // 2))
    try:
        return max(1, int(_settings().get("workers", default)))
    except (TypeError, ValueError):
        return default


def _ext_of(rel: str) -> str:
    return Path(rel).suffix.lower().lstrip(".")


def _ext_version(ext: str) -> int:
    """The extractor version for an extension (extract.EXTRACTOR_VERSION; missing means 1, the current one)."""
    return int(getattr(extract, "EXTRACTOR_VERSION", {}).get(ext, 1) or 1)


def _version_key(ext: str) -> str:
    """Which extraction code applies to an extension. A failure is only retried when this key changes."""
    return f"{EXTRACT_VERSION}.{_ext_version(ext)}"



def _mode(meta_only: Matcher, rel: str, size: int, max_bytes: int, dataless: bool = False,
          code_meta: bool = False) -> str:
    """full: the content is read and indexed. meta: the name is the whole entry; nothing is read. A file in the cloud, an
    iCloud placeholder, a metadata-only rule, an over-size file, a disk image or a large log is meta, and so is code when
    indexing.index_code is off (code_meta)."""
    if dataless or _icloud_placeholder(rel) or meta_only.match(rel) or size > max_bytes:
        return "meta"
    if content_skipped(rel, size) or (code_meta and _kind_of_rel(rel) == "code"):
        return "meta"
    return "full"


def _record_failure(conn, rel: str, mtime: float, error: str) -> None:
    row = conn.execute("SELECT count, mtime_at_failure FROM failures WHERE path=?", (rel,)).fetchone()
    same = row is not None and row["mtime_at_failure"] is not None and abs(row["mtime_at_failure"] - mtime) < 1e-3
    count = (row["count"] + 1) if same else 1
    conn.execute("INSERT OR REPLACE INTO failures(path, count, last_error, last_try, mtime_at_failure, version)"
                 " VALUES (?,?,?,?,?,?)", (rel, count, error[:300], time.time(), mtime, _version_key(_ext_of(rel))))


def _struck_out(conn, rel: str, mtime: float) -> bool:
    row = conn.execute("SELECT count, mtime_at_failure, version FROM failures WHERE path=?", (rel,)).fetchone()
    return bool(row is not None and row["count"] >= FAILURE_LIMIT and row["mtime_at_failure"] is not None
                and abs(row["mtime_at_failure"] - mtime) < 1e-3 and row["version"] == _version_key(_ext_of(rel)))


def _clear_strikes(conn, rel: str) -> None:
    conn.execute("UPDATE failures SET count=0 WHERE path=? AND count>0", (rel,))


def _inflight_path(name: str) -> Path:
    return paths.TMP / name / INFLIGHT


def _mark_inflight(name: str, items: list) -> None:
    """Crash guard: the files being read right now. Written before each read, cleared when the scan stops cleanly."""
    f = _inflight_path(name)
    try:
        if items and not STOPPING:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(items))
        else:
            f.unlink(missing_ok=True)
    except OSError:
        pass


def _recover_inflight(conn, name: str) -> int:
    """A marker still on disk means the last run was killed while these files were being read. Each of them gets a
    strike, so a file that kills the process is not retried at every start."""
    f = _inflight_path(name)
    if not f.exists():
        return 0
    try:
        items = json.loads(f.read_text())
    except (OSError, ValueError):
        items = []
    n = 0
    for it in items if isinstance(items, list) else []:
        try:
            rel, mtime = it
            _record_failure(conn, rel, float(mtime), "interrupted: the service stopped while this file was being read")
            n += 1
        except (TypeError, ValueError):
            continue
    f.unlink(missing_ok=True)
    return n


def _load_resume(conn) -> dict | None:
    """The checkpoint of a forced re-read that did not finish: files up to `after` were already re-read."""
    raw = store.get_meta(conn, "scan_resume") or ""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("ver") == EXTRACT_VERSION else None


def _rows_under(conn, rel: str) -> list[dict]:
    """Rows for a path and everything below it (a file, or a whole folder)."""
    esc = rel.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return [dict(r) for r in conn.execute(
        "SELECT id, path, size, mtime, mode, inode, hash, missing_since FROM files WHERE path=? OR path LIKE ? ESCAPE '\\'",
        (rel, esc + "/%"))]


# ------------------------------------------------------------------ light stage

def file_hash(p: Path, limit: int = 512 * 1024 * 1024) -> str | None:
    """Content hash used to recognise identical copies (results show them once). None for a file whose content is in the
    cloud: it is never read, because reading it would download it."""
    try:
        if _dataless(p):
            return None
        if p.stat().st_size > limit:
            return None
        h = hashlib.blake2b(digest_size=16)
        with open(p, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def _new_reader(coll: dict, cancel=None) -> readers.ReaderPool:
    """The reader pool of one pass. Processes start on first need and stop with the pass."""
    def stop() -> bool:
        return STOPPING or bool(cancel and cancel())
    limit = float(_settings().get("reader_timeout_s", readers.READER_TIMEOUT_S))
    return readers.ReaderPool(size=_worker_count(), timeout_s=limit, should_stop=stop)


def _prepare(coll: dict, rel: str, mode: str, starts: dict | None = None, reader=None) -> dict:
    """The worker half of indexing one file: extraction and the content hash. It touches no database.
    A metadata-only file is never opened: its name is the whole index entry. A file whose content is in the cloud is
    not opened either; it is deferred, so the read never starts a download. The pure-Python readers run in a reader
    process when `reader` is given (a read that runs past its limit is killed and counts as a failure)."""
    if starts is not None:
        starts[rel] = time.monotonic()
    p = Path(coll["path"]) / rel
    ext = _ext_of(rel)
    kind = _kind_of_rel(rel)
    read_kind = "text" if kind == "code" else kind      # code is read as text, when it is read at all (index_code)
    out = {"ext": ext, "kind": kind, "title": None, "lang": None, "pages": None, "heavy": None, "items": [],
           "error": None, "hash": None}
    if mode == "full":
        if _dataless(p):
            return {**out, "deferred": True}
        media = _media(coll)
        try:
            if reader is not None and read_kind in GUARDED_KINDS:
                try:
                    res = reader.run(p, read_kind, ext, media, max(reader.timeout_s, READ_LIMIT_FLOOR_S.get(read_kind, 0.0)))
                except readers.ReaderUnavailable as e:
                    # no reader process can run here: read in-process, as before, and say so once per pass
                    if not reader.reported:
                        reader.reported = True
                        liveprog.error(coll["name"], f"reader processes unavailable, reading in-process: {e}")
                    res = extract.light(p, read_kind, ext, media)
            else:
                res = extract.light(p, read_kind, ext, media)
            out.update(title=res["title"], lang=res["lang"], pages=res["pages"], heavy=res["heavy"],
                       items=res["items"])
        except readers.Stopped:
            raise Cancelled()
        except readers.ReaderError as e:                 # includes ReaderTimeout: its text says the limit that applied
            out["error"] = str(e)[:400]
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"[:400]
        out["hash"] = file_hash(p) or ""
    return out



def _failed_prep(rel: str, error: str) -> dict:
    ext = _ext_of(rel)
    return {"ext": ext, "kind": _kind_of_rel(rel), "title": None, "lang": None, "pages": None, "heavy": None,
            "items": [], "error": error, "hash": None}


def _store_light(conn, coll: dict, rel: str, size: int, mtime: float, mode: str, ino: int | None, prep: dict) -> dict:
    """The main-thread half: one file's rows, written into the open transaction (the caller commits in batches). A file
    that the cloud had not given back when its read began is not stored: its row keeps what it had, and the next pass
    sees the file again. A stored file is no longer missing."""
    if prep.get("deferred"):
        return {"id": None, "items": 0, "heavy": False, "error": None, "deferred": True}
    name = coll["name"]
    p = Path(coll["path"]) / rel
    row = conn.execute("SELECT id FROM files WHERE path=?", (rel,)).fetchone()
    if row:
        fid = row["id"]
    else:
        fid = conn.execute("INSERT INTO files(path, name, docid) VALUES (?,?,?)",
                           (rel, p.name, docid(name, rel))).lastrowid
    ext, error, title, heavy = prep["ext"], prep["error"], prep["title"], prep["heavy"]
    title_text = readable_path(rel) + (f" — {title}" if title else "")
    items = [{"modality": "title", "text": title_text}] + prep["items"]
    store.replace_items(conn, fid, name_tokens(rel), items)
    extract.cleanup_work(name, fid)
    thumb = paths.THUMBS / name / f"{fid}.jpg"
    if thumb.exists():
        thumb.unlink()
    full = mode == "full"
    conn.execute(
        "UPDATE files SET name=?, ext=?, kind=?, size=?, mtime=?, mode=?, state=?, heavy=?, error=?, title=?,"
        " doc_date=?, lang=?, pages=?, thumb=0, duration=NULL, indexed_at=?, heavy_at=NULL, hash=?, inode=?, xver=?,"
        " missing_since=NULL WHERE id=?",
        (p.name, ext, prep["kind"], size, mtime, mode, "error" if error else "text",
         json.dumps(heavy) if heavy else None, error, title,
         date_from_path(rel) or time.strftime("%Y-%m-%d", time.localtime(mtime)), prep["lang"], prep["pages"],
         time.time(), (prep["hash"] or "") if full else None, ino, _ext_version(ext) if full else None, fid))
    docmeta.store_light(conn, fid, rel, prep, full, error)      # never raises: a classifier problem must not cost the scan
    if error:
        _record_failure(conn, rel, mtime, error)
        liveprog.error(name, f"{p.name}: {error}")
    else:
        _clear_strikes(conn, rel)
    return {"id": fid, "items": len(items), "heavy": bool(heavy), "error": error}


def index_file_light(conn, coll: dict, rel: str, size: int, mtime: float, mode: str, ino: int | None = None,
                     reader=None) -> dict:
    if reader is not None:
        return _store_light(conn, coll, rel, size, mtime, mode, ino, _prepare(coll, rel, mode, None, reader))
    with _new_reader(coll) as own:
        return _store_light(conn, coll, rel, size, mtime, mode, ino, _prepare(coll, rel, mode, None, own))


def _match_moves(items: list[_Item], gone: list[dict], hasher) -> dict[str, dict]:
    """Pairs new files with rows that vanished in the same pass, one to one. A pair must agree on extension, size and
    mode. The inode is only a hint (inodes are reused and differ across remounts):
    - a metadata-only file pairs on inode + size + mtime + extension. Nothing is read, and no content is carried;
    - a full-mode file pairs only when its content hash agrees with the vanished row's hash, so no content is ever
      carried over on a hint alone. Among equal hashes, the same inode and mtime are preferred (copies share a hash).
    Returns {rel: row}. The paired rows are the ones the caller keeps."""
    pairs: dict[str, dict] = {}
    used: set[int] = set()
    by_ino: dict[int, list[dict]] = {}
    for g in gone:
        if g["inode"] is not None and g["mode"] == "meta":
            by_ino.setdefault(g["inode"], []).append(g)
    rest: list[_Item] = []
    for it in items:
        hit = None
        if it.mode == "meta" and it.ino is not None:
            hit = next((g for g in by_ino.get(it.ino, []) if g["id"] not in used and g["size"] == it.size
                        and abs((g["mtime"] or 0) - it.mtime) < 1e-3 and _ext_of(g["path"]) == _ext_of(it.rel)), None)
        if hit is not None:
            pairs[it.rel] = hit
            used.add(hit["id"])
        else:
            rest.append(it)
    by_size: dict[int, list[dict]] = {}
    for g in gone:
        if g["id"] not in used and g["mode"] == "full" and g["hash"] and g["size"] is not None:
            by_size.setdefault(g["size"], []).append(g)
    need = [it for it in rest if it.mode == "full" and it.size in by_size]
    if need:
        for it, h in zip(need, hasher(need)):
            if not h:
                continue
            cands = [g for g in by_size[it.size] if g["id"] not in used and g["hash"] == h
                     and _ext_of(g["path"]) == _ext_of(it.rel)]
            cands.sort(key=lambda g: (g["inode"] != it.ino, abs((g["mtime"] or 0) - it.mtime) >= 1e-3))
            if cands:
                pairs[it.rel] = cands[0]
                used.add(cands[0]["id"])
    return pairs


def _repoint(conn, coll: dict, row: dict, it: _Item) -> int:
    """A file moved or was renamed, so the same row now carries the new path. Its id stays, and with it the text,
    OCR, transcripts, thumbnail and vectors. Also rewritten: the files row's path, name, size, mtime, inode and date;
    the title item (it holds the readable path, so it is embedded again, one short text); and the FTS name column of
    every item (contentless FTS: delete each rowid, then insert it again). The docid is kept, so a favourite that
    matched by docid still finds the file."""
    fid = row["id"]
    conn.execute("DELETE FROM failures WHERE path=?", (it.rel,))        # the strikes follow the file to its new path
    conn.execute("UPDATE failures SET path=? WHERE path=?", (it.rel, row["path"]))
    title = conn.execute("SELECT title FROM files WHERE id=?", (fid,)).fetchone()["title"]
    title_text = readable_path(it.rel) + (f" — {title}" if title else "")
    name_text = name_tokens(it.rel)
    conn.execute("UPDATE files SET path=?, name=?, size=?, mtime=?, inode=?, doc_date=?, missing_since=NULL WHERE id=?",
                 (it.rel, Path(it.rel).name, it.size, it.mtime, it.ino,
                  date_from_path(it.rel) or time.strftime("%Y-%m-%d", time.localtime(it.mtime)), fid))
    for r in conn.execute("SELECT id, modality, heading, text FROM items WHERE file_id=?", (fid,)).fetchall():
        text = r["text"]
        if r["modality"] == "title":
            text = title_text
            conn.execute("UPDATE items SET text=?, embedded=0 WHERE id=?", (text, r["id"]))
            conn.execute("DELETE FROM vectors WHERE item_id=?", (r["id"],))
            conn.execute("DELETE FROM vbin WHERE item_id=?", (r["id"],))
        conn.execute("DELETE FROM fts WHERE rowid=?", (r["id"],))
        conn.execute("INSERT INTO fts(rowid, name, heading, text) VALUES (?,?,?,?)",
                     (r["id"], name_text, r["heading"] or "", text or ""))
    return fid


def _await(fut, rel: str, starts: dict, limit: float, cancel):
    """The result of one read. Raises _Overdue when it has run longer than `limit`."""
    while not fut.done():
        # a stop is checked first: a read cut short by a stop is not a timeout, so it never counts as a strike
        if cancel and cancel():
            raise Cancelled()
        t = starts.get(rel)
        if t is not None and time.monotonic() - t > limit:
            raise _Overdue()
        wait_futures([fut], timeout=0.25)
    return fut.result()


# The mass-vanish guard. A cloud drive that is offline for a weekend lists its folder empty: every row under it is then
# marked missing at once, and the first pass after the grace period would remove them all. A removal is held while the
# collection, or the top-level folder it sits in, has most of its rows missing. Override with
# indexing.mass_missing_ratio and indexing.mass_missing_min; a ratio above 1 turns the guard off.
MASS_MISSING_RATIO = 0.30     # the share of rows that must be missing ...
MASS_MISSING_MIN = 50         # ... and the number of rows, before any removal is held


def _mass_guard(settings: dict) -> tuple[float, int]:
    try:
        ratio = float(settings.get("mass_missing_ratio", MASS_MISSING_RATIO))
    except (TypeError, ValueError):
        ratio = MASS_MISSING_RATIO
    try:
        minimum = int(settings.get("mass_missing_min", MASS_MISSING_MIN))
    except (TypeError, ValueError):
        minimum = MASS_MISSING_MIN
    return ratio, minimum


def _top_folder(rel: str) -> str:
    return rel.split("/", 1)[0] if "/" in rel else ""


def _held_by_mass_guard(conn, due: list[dict], ratio: float, minimum: int) -> tuple[set[int], str]:
    """The removals the mass-vanish guard holds, as file ids, and a warning ("" when nothing is held).

    Counts are over the whole collection: the rows missing now (those already marked, plus any this pass marked) against
    all rows. Over the threshold, every due removal is held. Otherwise each top-level folder is judged on its own, so a
    folder that went offline is held without holding the removals elsewhere. The counts run only when a removal is due,
    so an ordinary pass pays nothing for the guard."""
    def over(n: int, m: int) -> bool:
        return m >= minimum and m >= ratio * n

    total, missing = conn.execute("SELECT COUNT(*), COUNT(missing_since) FROM files").fetchone()
    if over(total, missing):
        return {g["id"] for g in due}, (
            f"{missing:,} of {total:,} indexed files are missing ({missing / max(total, 1):.0%}), over the threshold: "
            "nothing was removed this pass. Rows stay hidden from search and are removed once the files are back, "
            "or once the count drops under the threshold")
    # per top-level folder, for the folders the due removals sit in (a range on the path index, not a scan of the
    # table). Root-level files have no folder of their own: the collection-wide count above is their guard.
    groups = {}
    for top in {_top_folder(g["path"]) for g in due} - {""}:
        groups[top] = conn.execute("SELECT COUNT(*), COUNT(missing_since) FROM files WHERE path >= ? AND path < ?",
                                   (top + "/", top + "0")).fetchone()
    held_tops = {t for t, (n, m) in groups.items() if over(n, m)}
    if not held_tops:
        return set(), ""
    ids = {g["id"] for g in due if _top_folder(g["path"]) in held_tops}
    named = sorted(f"{t} ({groups[t][1]:,} of {groups[t][0]:,} missing)" for t in held_tops)
    return ids, ("removals held in folder(s) over the threshold, nothing removed from them this pass: "
                 + ", ".join(named[:3]) + (f" and {len(named) - 3} more" if len(named) > 3 else ""))


def _note_held(name: str, warning: str) -> None:
    """A held removal counts as an error in the progress registry (mse status shows the count and the message) and is
    written to the daemon log."""
    liveprog.error(name, f"mass-vanish guard: {warning}")
    try:
        paths.LOGS.mkdir(parents=True, exist_ok=True)
        with (paths.LOGS / "daemon.log").open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {name}: mass-vanish guard: {warning}\n")
    except OSError:
        pass


def _remove_rows(conn, name: str, rows: list[dict]) -> int:
    """The one removal routine: a file's items, vectors and keyword entries (store.delete_items), its document facts, its
    row, its failure record, its work files and its thumbnail. Every removal from the index goes through here: the rule
    removals, the guarded passes (_settle_missing) and forget_missing. Batched: one statement per 500 files, because one
    statement per file scans the vector table every time (50 files: 2.6 s one by one, 0.3 s batched). The caller commits.
    Nothing on disk outside the index is touched."""
    for i in range(0, len(rows), 500):
        part = rows[i:i + 500]
        fids = [g["id"] for g in part]
        q = ",".join("?" * len(fids))
        store.delete_items(conn, [r[0] for r in conn.execute(f"SELECT id FROM items WHERE file_id IN ({q})", fids)])
        conn.execute(f"DELETE FROM doc_meta WHERE file_id IN ({q})", fids)
        conn.execute(f"DELETE FROM files WHERE id IN ({q})", fids)
        conn.executemany("DELETE FROM failures WHERE path=?", [(g["path"],) for g in part])
    for g in rows:
        extract.cleanup_work(name, g["id"])
        (paths.THUMBS / name / f"{g['id']}.jpg").unlink(missing_ok=True)
    return len(rows)


def _settle_missing(conn, name: str, rows: list[dict], now: float, grace_s: float,
                    guard: tuple[float, int] | None = None) -> dict:
    """Rows whose file a pass did not see. The first time, the row is marked (missing_since) and kept; nothing is lost
    to a folder that lists empty while it is offline. It is removed only when a later pass, at least grace_s after the
    mark, still does not see the file, and the mass-vanish guard does not hold it (_held_by_mass_guard). A held row stays
    marked, hidden from search, and is removed by a later pass once the count is back under the threshold. All removals
    go through here. Returns {"marked", "kept", "removed", "held", "warning"}; kept includes held."""
    if guard is None:
        guard = _mass_guard(_settings())
    marked = kept = 0
    due = []
    for g in rows:
        if g["missing_since"] is None:
            conn.execute("UPDATE files SET missing_since=? WHERE id=?", (now, g["id"]))
            marked += 1
        elif now - g["missing_since"] >= grace_s:
            due.append(g)
        else:
            kept += 1
    held_ids, warning = _held_by_mass_guard(conn, due, *guard) if due else (set(), "")
    to_remove = [g for g in due if g["id"] not in held_ids]
    held = len(due) - len(to_remove)
    kept += held
    removed = _remove_rows(conn, name, to_remove)
    if held:
        warning += (f" (held {held:,} missing files; if you deleted them on purpose, run "
                    f"`mse forget-missing {name}`)")
        _note_held(name, warning)
    return {"marked": marked, "kept": kept, "removed": removed, "held": held,
            "warning": warning if held else ""}


def _missing_grace_s(settings: dict) -> float:
    try:
        return max(0.0, float(settings.get("missing_grace_h", MISSING_GRACE_H))) * 3600.0
    except (TypeError, ValueError):
        return MISSING_GRACE_H * 3600.0


def _has_missing_col(conn) -> bool:
    """Whether this database has files.missing_since (schema 3). A read-only connection never runs the migration, so a
    database no writer has opened since the upgrade lacks the column, and no row in it can be missing then."""
    return any(r[1] == "missing_since" for r in conn.execute("PRAGMA table_info(files)"))


def _present(conn, alias: str = "") -> str:
    """' AND <alias>missing_since IS NULL' for a query over files: only rows whose file is there. '' without the column
    (the same approach as search._present)."""
    return f" AND {alias}missing_since IS NULL" if _has_missing_col(conn) else ""


def held_missing(conn, now: float | None = None) -> int:
    """How many missing rows the mass-vanish guard holds right now: the rows past their grace period that a removal pass
    would hold (the test _settle_missing makes). Read-only. Zero without the column, or when nothing is due."""
    if not _has_missing_col(conn):
        return 0
    s = _settings()
    now = time.time() if now is None else now
    due = [dict(r) for r in conn.execute(
        "SELECT id, path FROM files WHERE missing_since IS NOT NULL AND missing_since <= ?",
        (now - _missing_grace_s(s),))]
    if not due:
        return 0
    ids, _ = _held_by_mass_guard(conn, due, *_mass_guard(s))
    return len(ids)


def _clean_prefix(p: str | None) -> str:
    """A folder in the form the rows use: relative to the collection, no empty or '.' parts. '' is the whole collection."""
    p = (p or "").strip()
    if p.startswith("/"):
        raise ValueError("path must be relative to the collection")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if ".." in parts:
        raise ValueError("path must stay inside the collection")
    return "/".join(parts)


def _missing_rows(conn, prefix: str = "", as_of: float | None = None) -> list[dict]:
    """The rows marked missing, under a folder or in the whole collection, in path order. With as_of, only the rows marked
    at or before that time: a row marked after a listing was not on it, so it stays."""
    if not _has_missing_col(conn):
        return []
    q, args = "SELECT id, path, missing_since FROM files WHERE missing_since IS NOT NULL", []
    if as_of is not None:
        q += " AND missing_since <= ?"
        args.append(as_of)
    if prefix:
        esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        q += " AND (path = ? OR path LIKE ? ESCAPE '\\')"
        args += [prefix, esc + "/%"]
    return [dict(r) for r in conn.execute(q + " ORDER BY path", args)]


def _forget_favourites(name: str, rels: set[str]) -> int:
    """Favourites and hidden results (feedback.db) of the files forget_missing removed. Only those: the guarded passes
    keep them, so a file that comes back finds its favourite again."""
    from . import feedback
    n = 0
    for f in feedback.list_items():
        if f["collection"] == name and f["path"] in rels:
            n += feedback.remove(id=f["id"])
    return n


def forget_missing(name: str, path: str = "", dry_run: bool = True, as_of: float | None = None) -> dict:
    """Remove from the index the rows of files that are gone from disk, under `path` or in the whole collection. Only rows
    marked missing are touched, never a present one, and nothing on disk is touched.

    A dry run lists them (count, the first 20 paths) and returns `as_of`, the time of the listing. A removal needs that
    `as_of` and removes only the rows marked at or before it, so a folder that goes offline between the listing and the
    confirmation is not removed unseen. The removal is _remove_rows, the routine the guarded passes use, and the favourites
    of the removed files go too. The mass-vanish guard does not apply: the removal was confirmed."""
    prefix = _clean_prefix(path)
    if not dry_run and as_of is None:
        raise ValueError("a removal needs as_of from a dry run of the same scope")
    listed_at = time.time()
    out = {"collection": name, "path": prefix, "dry_run": dry_run, "count": 0, "paths": [], "removed": 0,
           "favourites_removed": 0, "as_of": listed_at if dry_run else float(as_of)}
    if not store.db_path(name).exists():
        return out
    if dry_run:
        conn = store.connect(name, readonly=True)
        try:
            rows = _missing_rows(conn, prefix)
        finally:
            conn.close()
    else:
        with store.write_lock(name):
            conn = store.connect(name)
            try:
                rows = _missing_rows(conn, prefix, float(as_of))
                out["removed"] = _remove_rows(conn, name, rows)
                conn.commit()
            finally:
                conn.close()
        if rows:
            try:
                out["favourites_removed"] = _forget_favourites(name, {r["path"] for r in rows})
            except Exception as e:       # the rows are gone already: say what did not clean
                out["favourites_error"] = f"{type(e).__name__}: {e}"
    out["count"] = len(rows)
    out["paths"] = [r["path"] for r in rows[:20]]
    return out


def scan(coll: dict, progress=None, cancel=None, night: bool = False) -> dict:
    """Detect new, changed and removed files and read the changes.

    - Unchanged files are never opened: size, mtime and mode decide. The inode is only a rename hint.
    - Reads run in a bounded thread pool, each with a wall-clock limit. The pure-Python readers run in reader processes
      that are killed at reader_timeout_s (readers.py). Only this thread writes the database, in transactions of
      COMMIT_EVERY files or COMMIT_SECONDS, whichever comes first.
    - A file that vanished and a new file with the same content (same inode, or same hash, size and extension)
      are the same file: the existing row is re-pointed and nothing is read again.
    - A row whose file this pass did not see is marked missing (missing_since) and kept. It is removed only when a later
      pass, at least missing_grace_h hours after the mark, still misses the file. A pass that sees it clears the mark.
    - A file whose content is in the cloud is listed by name and dates and never read. An existing row whose size and
      date did not change keeps its content.
    - A file that failed FAILURE_LIMIT times is skipped until its mtime or its extractor version changes.
    - A folder the walk could not read keeps its rows (counted as kept_unreadable): nothing under it is removed. A
      missing or unreadable root removes nothing at all.
    - Resumable: a forced re-read (new extractor) that stopped continues after its last committed file.
    """
    name = coll["name"]
    settings = _settings()
    workers = _worker_count()
    limit_s = float(settings.get("file_timeout_s", FILE_TIMEOUT_S))
    xver_budget = int(settings.get("xver_per_night" if night else "xver_per_scan",
                                   XVER_PER_NIGHT if night else XVER_PER_SCAN))
    hash_budget = HASH_BACKFILL_NIGHT if night else HASH_BACKFILL
    grace_s = _missing_grace_s(settings)
    guard = _mass_guard(settings)
    t0 = time.time()
    now = t0
    errors: list[str] = []
    seen: set[str] = set()
    unreadable: set[str] = set()       # folders the walk could not read ("" is the root): their rows are not removed
    dataless_rels: set[str] = set()    # files whose content is in the cloud: listed and kept, never read
    reappeared: list[int] = []         # rows the walk saw again after they had been marked missing
    work: list[_Item] = []
    gone_rows: list[dict] = []
    counts = {"errors": 0, "moved": 0, "done": 0, "deferred": 0}
    state = {"overdue": [], "since": 0, "committed": 0, "last_commit": time.monotonic(), "last_rel": None}
    stalled = cancelled = False
    removed = skipped = xver_deferred = interrupted = kept_unreadable = 0
    removed_by_rule = names_first = downgraded_code = downgraded_other = kinds_fixed = 0
    by_rule: Counter = Counter()       # rows removed by a rule, by reason: nested, gitignored, rule
    kept_missing = missing_marked = dataless_kept = 0
    docmeta_done = 0
    held_mass = 0                      # removals the mass-vanish guard held in this pass (see _held_by_mass_guard)
    held_warning = ""
    resume_after = None
    ex = None
    reader = None
    if progress:
        progress("scanning", 0, 0, "")
    with store.write_lock(name):
        conn = store.connect(name)
        try:
            interrupted = _recover_inflight(conn, name)
            if store.get_meta(conn, "meta_hashes_cleared") != "1":
                # metadata-only files are never read, so a content hash kept from before must not group them with copies
                conn.execute("UPDATE files SET hash=NULL WHERE mode='meta' AND hash IS NOT NULL")
                store.set_meta(conn, "meta_hashes_cleared", "1")
            conn.commit()
            known = {r["path"]: dict(r) for r in conn.execute(
                "SELECT id, path, size, mtime, mode, inode, hash, xver, kind, missing_since FROM files")}
            scope = _Scope(coll)
            kind_fix: list[tuple[str, int]] = []   # rows whose kind is now "code", updated in place (no re-read)
            version_stale = store.get_meta(conn, "extract_version") != EXTRACT_VERSION
            resume = _load_resume(conn) if version_stale else None
            resume_after = resume.get("after") if resume else None
            xver_cands: list[_Item] = []
            for rel, size, mtime, ino in scope.walk_entries(errors=errors, unreadable=unreadable,
                                                            dataless=dataless_rels):
                seen.add(rel)
                is_dl = rel in dataless_rels
                mode = _mode(scope.meta_only, rel, size, scope.max_bytes, dataless=is_dl,
                             code_meta=not scope.index_code)
                old = known.get(rel)
                if old is not None and old["missing_since"] is not None:
                    reappeared.append(old["id"])
                if is_dl and old is not None and old["size"] == size and abs((old["mtime"] or 0) - mtime) < 1e-3:
                    dataless_kept += 1                # the cloud evicted it: its row keeps its content, nothing is read
                    continue
                if old is None:
                    reason = "new"
                elif old["size"] != size or abs((old["mtime"] or 0) - mtime) > 1e-3 or old["mode"] != mode:
                    reason = "changed"
                elif version_stale and (resume_after is None or rel > resume_after):
                    reason = "forced"
                elif mode == "full" and old["mode"] == "full" and _ext_version(_ext_of(rel)) > (old["xver"] or 1):
                    reason = "xver"
                else:
                    if old is not None and old["kind"] != _kind_of_rel(rel):
                        kind_fix.append((_kind_of_rel(rel), old["id"]))
                    continue
                if old is not None and old["mtime"] == -1:
                    _clear_strikes(conn, rel)           # Retry sets mtime to -1: an explicit request resets the strikes
                elif _struck_out(conn, rel, mtime):
                    skipped += 1
                    continue
                item = _Item(rel, size, mtime, mode, ino, reason, old)
                if reason == "changed" and mode == "meta" and old is not None and old["mode"] == "full":
                    if _kind_of_rel(rel) == "code":
                        downgraded_code += 1               # index_code is off: the content goes, the name stays
                    else:
                        downgraded_other += 1
                (xver_cands if reason == "xver" else work).append(item)
            if kind_fix:
                conn.executemany("UPDATE files SET kind=? WHERE id=?", kind_fix)
                kinds_fixed = len(kind_fix)
            if reappeared:
                conn.executemany("UPDATE files SET missing_since=NULL WHERE id=?", [(i,) for i in reappeared])
            xver_cands.sort(key=lambda w: w.rel)
            xver_deferred = max(0, len(xver_cands) - xver_budget)
            work += xver_cands[:xver_budget]
            work.sort(key=lambda w: w.rel)
            if errors and not seen and not known:
                raise PermissionError(
                    f"cannot read {coll['path']}: {errors[0]} — allow MySearch's Python in System Settings › "
                    "Privacy & Security › Full Disk Access (or Files and Folders)")
            root = Path(coll["path"])
            reader = _new_reader(coll, cancel)
            ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="index")
            # A row the walk did not see is marked missing, and removed only after the grace period (_settle_missing),
            # unless the walk could not read the folder it sits in. A missing or unreadable root removes nothing at all.
            unseen = [v for k, v in known.items() if k not in seen]
            rule_rows: list[tuple[dict, str]] = []
            if not _root_readable(root):
                errors.insert(0, "root unreadable; nothing removed")
                gone_rows = []
                kept_unreadable = len(unseen)
            else:
                readable = [v for v in unseen if not _under_unreadable(v["path"], unreadable)]
                kept_unreadable = len(unseen) - len(readable)
                # a row a rule excludes is removed at once; the rest is missing (grace period, mass-vanish guard)
                rule_rows, gone_rows = _classify(scope, root, readable)
            if rule_rows:
                by_rule = _remove_rule_rows(conn, name, rule_rows, progress, cancel)
                removed_by_rule = sum(by_rule.values())
            new_items = [w for w in work if w.reason == "new"]
            pairs = {}
            if new_items and gone_rows:
                pairs = _match_moves(new_items, gone_rows,
                                     lambda its: list(ex.map(lambda w: file_hash(root / w.rel), its)))
            for w in new_items:
                w.move = pairs.get(w.rel)
            used = {g["id"] for g in pairs.values()}
            settled = _settle_missing(
                conn, name, [g for g in gone_rows if g["id"] not in used], now, grace_s, guard)
            missing_marked, kept_in_grace, removed = settled["marked"], settled["kept"], settled["removed"]
            held_mass, held_warning = settled["held"], settled["warning"]
            kept_missing = missing_marked + kept_in_grace
            conn.commit()
            names_first = _names_first(conn, coll, [w for w in work if w.reason == "new" and w.move is None],
                                       progress, cancel)

            total = len(work)
            if progress:
                progress("extract", 0, total, "")
            liveprog.set_pending(name, light=sum(1 for w in work if w.move is None))
            window = max(2, 2 * workers)
            starts: dict[str, float] = {}
            pending: deque = deque()

            def inflight_mark():
                _mark_inflight(name, [[x.rel, x.mtime] for x, f in pending if f is not None])

            def commit(after_rel):
                if version_stale and after_rel is not None:
                    store.set_meta(conn, "scan_resume", json.dumps({"after": after_rel, "ver": EXTRACT_VERSION}))
                conn.commit()
                state["since"], state["last_commit"] = 0, time.monotonic()
                state["committed"] += 1
                if state["committed"] % 10 == 0:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

            def consume():
                w, fut = pending.popleft()
                if w.move is not None:
                    _repoint(conn, coll, w.move, w)
                    counts["moved"] += 1
                else:
                    try:
                        prep = _await(fut, w.rel, starts, limit_s, cancel)
                    except _Overdue:
                        state["overdue"].append(fut)
                        prep = _failed_prep(w.rel, f"timed out after {int(limit_s)} s")
                    except Cancelled:
                        raise
                    except Exception as e:
                        prep = _failed_prep(w.rel, f"{type(e).__name__}: {e}"[:400])
                    r = _store_light(conn, coll, w.rel, w.size, w.mtime, w.mode, w.ino, prep)
                    counts["errors"] += bool(r["error"])
                    counts["deferred"] += bool(r.get("deferred"))
                    inflight_mark()
                counts["done"] += 1
                state["since"] += 1
                state["last_rel"] = w.rel
                if progress and (counts["done"] % 5 == 0 or counts["done"] == total):
                    progress("extract", counts["done"], total, w.rel)
                if state["since"] >= COMMIT_EVERY or time.monotonic() - state["last_commit"] >= COMMIT_SECONDS:
                    commit(w.rel)

            def stuck_now() -> int:              # reads past their limit that are still running (their threads are busy)
                return sum(1 for f in state["overdue"] if not f.done())

            def drain(limit: int):
                while pending and len(pending) >= limit and stuck_now() < workers:
                    consume()

            try:
                for w in work:
                    if stuck_now() >= workers:
                        break
                    if cancel and cancel():
                        raise Cancelled()
                    if w.move is not None:
                        pending.append((w, None))
                    else:
                        pending.append((w, ex.submit(_prepare, coll, w.rel, w.mode, starts, reader)))
                        inflight_mark()
                    drain(window)
                drain(1)
                stalled = stuck_now() >= workers
            except Cancelled:
                cancelled = True
                commit(state["last_rel"])
                raise
            except BaseException:
                conn.rollback()
                raise
            if stalled:
                # every worker is stuck on a file that will not finish: stop here, the rest waits for the next run
                commit(state["last_rel"])
            else:
                if version_stale:
                    store.set_meta(conn, "extract_version", EXTRACT_VERSION)
                store.set_meta(conn, "scan_resume", "")
                rows = conn.execute("SELECT id, path FROM files WHERE hash IS NULL AND mode='full' LIMIT ?",
                                    (hash_budget,)).fetchall()
                for i in range(0, len(rows), 200):
                    if cancel and cancel():
                        break
                    # a file in the cloud is never read to hash it: its row waits for a pass that finds it local
                    part = [r for r in rows[i:i + 200] if not _dataless(root / r["path"])]
                    hashes = list(ex.map(lambda r: file_hash(root / r["path"]) or "", part))
                    conn.executemany("UPDATE files SET hash=? WHERE id=?", [(h, r["id"]) for h, r in zip(hashes, part)])
                    conn.commit()
                try:        # document facts for what has none yet; a failure here never fails the scan
                    docmeta_done = docmeta.backfill(conn, DOCMETA_BACKFILL_NIGHT if night else DOCMETA_BACKFILL, cancel)
                except Exception as e:
                    liveprog.error(name, f"document facts: {type(e).__name__}: {e}")
                store.set_meta(conn, "last_scan", time.strftime("%Y-%m-%d %H:%M:%S"))
                conn.commit()
                if state["committed"] >= 10:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            if ex is not None:
                ex.shutdown(wait=not (stalled or cancelled or any(not f.done() for f in state["overdue"])),
                            cancel_futures=True)
            if reader is not None:
                reader.close()                # a clean stop kills nothing; a read still running is killed here
            _mark_inflight(name, [])              # a clean stop: nothing is in flight any more
            conn.close()
    stats = {
        "new_or_changed": sum(1 for w in work if w.reason in ("new", "changed")),
        "removed": removed,
        "kept_missing": kept_missing,
        "held_mass_missing": held_mass,
        "mass_missing_warning": held_warning,
        "missing_marked": missing_marked,
        "kept_unreadable": kept_unreadable,
        "dataless": len(dataless_rels),
        "dataless_kept": dataless_kept,
        "deferred": counts["deferred"],
        "errors": counts["errors"],
        "files_seen": len(seen),
        "moved": counts["moved"],
        "forced": sum(1 for w in work if w.reason == "forced"),
        "xver": sum(1 for w in work if w.reason == "xver"),
        "xver_deferred": xver_deferred,
        "skipped_failed": skipped,
        "docmeta": docmeta_done,
        "interrupted": interrupted,
        "resumed": resume_after is not None,
        "stalled": stalled,
        "removed_by_rule": removed_by_rule,
        "removed_nested_repo": by_rule["nested"],
        "removed_gitignored": by_rule["gitignored"],
        "names_first": names_first,
        "downgraded_code": downgraded_code,
        "downgraded_other": downgraded_other,
        "kinds_fixed": kinds_fixed,
        "pruned": dict(scope.pruned.most_common(12)),
        "gitignored": scope.gitignored,
        "pruned_nested": dict(scope.pruned_nested),
        "bundles": scope.bundles,
    }
    stats["seconds"] = round(time.time() - t0, 1)
    stats["walk_errors"] = errors[:5]
    return stats



def index_paths(coll: dict, rels: list[str], progress=None, cancel=None) -> dict:
    """Re-index exactly these paths (from file events): new or changed files, and removals. A folder is walked, and
    whatever vanished inside it is marked missing and removed only after the grace period (_settle_missing). A file that
    moved is re-pointed, not read again. A file whose content is in the cloud is listed, never read. A path that a rule
    now excludes (a pruned build folder, a secret, a nested repository, a gitignored path) loses its rows at once, as in
    a scan (see _classify)."""
    name = coll["name"]
    root = Path(coll["path"])
    scope = _Scope(coll)
    max_bytes = scope.max_bytes
    files: dict[str, tuple | None] = {}
    dataless: set[str] = set()     # paths whose content is in the cloud: listed and kept, never read
    sweep: list[str] = []          # folders read in full: what is indexed under them but no longer listed is removed or marked
    unknown: list[str] = []        # paths that cannot be read (permission, offline share): their rows are never marked
    drop: list[str] = []           # paths a rule now excludes: their rows are removed
    for rel in (r.strip("/") for r in rels):
        if not rel:                                   # the collection root itself is a full scan, never a path job
            continue
        p = root / rel
        try:
            lst = _lstat(p)
        except FileNotFoundError:
            files[rel] = None                         # gone: a file, or a whole folder
            continue
        except OSError:
            unknown.append(rel)                       # cannot tell whether it is there: keep what is indexed
            continue
        is_dir = S_ISDIR(lst.st_mode)
        if scope.reason(rel, is_dir):
            drop.append(rel)
            continue
        if is_dir and is_key_name(p.name):
            got = _deck_stats(root, rel, dataless)    # a Keynote package is one entry, not a folder to sweep
            if got is None:
                unknown.append(rel)
            else:
                files[rel] = got
        elif is_dir and _bundle_name(p.name):
            files[rel] = (0, lst.st_mtime, lst.st_ino)  # a bundle is one name-only entry
        elif is_dir:
            errs: list[str] = []
            sub_dataless: set[str] = set()
            walked = list(scope.walk_entries(rel, errs, None, sub_dataless))
            if errs:
                unknown.append(rel)                   # an unreadable part inside: nothing under it may be removed
            else:
                sweep.append(rel)
            for r2, size, mtime, ino in walked:       # root-relative, under the collection's own rules
                files[r2] = (size, mtime, ino)
                if r2 in sub_dataless:
                    dataless.add(r2)
        elif S_ISREG(lst.st_mode):
            files[rel] = (lst.st_size, lst.st_mtime, lst.st_ino)
            if _flag_dataless(lst):
                dataless.add(rel)
        else:
            files[rel] = None                         # a symlink or a special file: never indexed, so nothing to keep

    def _unknown(path: str) -> bool:
        return any(path == u or path.startswith(u + "/") for u in unknown)

    now = time.time()
    settings_now = _settings()
    grace_s = _missing_grace_s(settings_now)
    guard = _mass_guard(settings_now)
    changed: list[int] = []
    moved_ids: list[int] = []
    moved = skipped = removed = marked = kept_in_grace = dataless_kept = 0
    by_rule: Counter = Counter()
    held = 0
    held_warning = ""
    with store.write_lock(name):
        conn = store.connect(name)
        try:
            # the rows the walk does not list now: under a dropped path, under a missing path, or under a swept folder
            # that no longer holds them (and that could be read). A row a rule excludes goes at once; the rest is missing.
            cand: dict[int, dict] = {}
            for rel in drop:
                for g in _rows_under(conn, rel):
                    cand[g["id"]] = g
            for rel, st in files.items():
                if st is None:
                    for g in _rows_under(conn, rel):
                        cand[g["id"]] = g
            for d in sweep:
                for g in _rows_under(conn, d):
                    if g["path"] not in files and not _unknown(g["path"]):
                        cand[g["id"]] = g
            rule_rows, gone_rows = _classify(scope, root, list(cand.values()))
            if rule_rows:
                by_rule.update(_remove_rule_rows(conn, name, rule_rows, progress, cancel))
            gone: dict[int, dict] = {g["id"]: g for g in gone_rows}
            items: list[_Item] = []
            for rel, st in sorted(files.items()):
                if st is None:
                    continue
                size, mtime, ino = st
                is_dl = rel in dataless
                mode = _mode(scope.meta_only, rel, size, max_bytes, dataless=is_dl, code_meta=not scope.index_code)
                row = conn.execute("SELECT id, size, mtime, mode, missing_since FROM files WHERE path=?",
                                   (rel,)).fetchone()
                if row is not None and row["missing_since"] is not None:
                    conn.execute("UPDATE files SET missing_since=NULL WHERE id=?", (row["id"],))
                if is_dl and row is not None and row["size"] == size and abs((row["mtime"] or 0) - mtime) < 1e-3:
                    dataless_kept += 1                # the cloud evicted it: its row keeps its content
                    continue
                if row and row["size"] == size and abs((row["mtime"] or 0) - mtime) < 1e-3 and row["mode"] == mode:
                    continue
                if row is not None and row["mtime"] == -1:
                    _clear_strikes(conn, rel)
                elif _struck_out(conn, rel, mtime):
                    skipped += 1
                    continue
                items.append(_Item(rel, size, mtime, mode, ino, "new" if row is None else "changed"))
            pairs = {}
            if gone and any(w.reason == "new" for w in items):
                pairs = _match_moves([w for w in items if w.reason == "new"], list(gone.values()),
                                     lambda its: [file_hash(root / w.rel) for w in its])
            used = {g["id"] for g in pairs.values()}
            settled = _settle_missing(
                conn, name, [g for g in gone.values() if g["id"] not in used], now, grace_s, guard)
            marked, kept_in_grace, removed = settled["marked"], settled["kept"], settled["removed"]
            held, held_warning = settled["held"], settled["warning"]
            with _new_reader(coll, cancel) as reader:
                for it in items:
                    if cancel and cancel():
                        break
                    if it.rel in pairs:
                        moved_ids.append(_repoint(conn, coll, pairs[it.rel], it))
                        moved += 1
                        continue
                    r = index_file_light(conn, coll, it.rel, it.size, it.mtime, it.mode, it.ino, reader)
                    if r["id"] is not None:
                        changed.append(r["id"])
                    if progress:
                        progress("extract", len(changed), len(items), it.rel)
            conn.commit()
        finally:
            conn.close()
    return {"changed": len(changed) + moved, "removed": removed, "moved": moved, "skipped_failed": skipped,
            "kept_missing": marked + kept_in_grace, "missing_marked": marked, "dataless_kept": dataless_kept,
            "held_mass_missing": held, "mass_missing_warning": held_warning,
            "removed_by_rule": sum(by_rule.values()), "removed_nested_repo": by_rule["nested"],
            "removed_gitignored": by_rule["gitignored"],
            "ids": changed + moved_ids}


def heavy_pending_ids(name: str, ids: list[int]) -> list[int]:
    conn = store.connect(name)
    q = ",".join("?" * len(ids))
    out = [r[0] for r in conn.execute(
        f"SELECT id FROM files WHERE heavy IS NOT NULL AND mode='full' AND id IN ({q}){_present(conn)}", ids)]
    conn.close()
    return out



def index_one(coll: dict, rel: str) -> dict:
    """Re-index a single file fully (light + heavy + embed). A file that is gone is marked missing, not removed at once,
    as in a scan. A file in the cloud is indexed by its name only."""
    root = Path(coll["path"])
    p = root / rel
    scope = _Scope(coll)
    if _secret_excluded(rel, scope.exclude, scope.nokey, root) or not scope.eligible(rel, False):
        raise ValueError("this path is excluded by the collection's rules")
    with store.write_lock(coll["name"]):
        conn = store.connect(coll["name"])
        try:
            try:
                st = _lstat(p)
            except OSError:
                st = None
            if st is None:
                rows = [dict(r) for r in conn.execute("SELECT id, path, missing_since FROM files WHERE path=?", (rel,))]
                settled = _settle_missing(conn, coll["name"], rows, time.time(), _missing_grace_s(_settings()))
                conn.commit()
                return {"removed": bool(settled["removed"]), "kept_missing": bool(settled["marked"] or settled["kept"]),
                        "held_mass_missing": settled["held"]}
            dl = _flag_dataless(st)
            mode = _mode(scope.meta_only, rel, st.st_size, scope.max_bytes, dataless=dl,
                         code_meta=not scope.index_code)
            row = conn.execute("SELECT id, size, mtime FROM files WHERE path=?", (rel,)).fetchone()
            if dl and row is not None and row["size"] == st.st_size and abs((row["mtime"] or 0) - st.st_mtime) < 1e-3:
                conn.commit()
                return {"id": row["id"], "kept_dataless": True}
            r = index_file_light(conn, coll, rel, st.st_size, st.st_mtime, mode, st.st_ino)
            conn.commit()
        finally:
            conn.close()
    if r["id"] is None:                  # in the cloud when its read began: nothing stored, the next scan sees it again
        return r
    heavy(coll, only_ids=[r["id"]], priority="always")          # an explicit request for one file: never paused
    embed(coll, only_file_ids=[r["id"]], priority="always")
    return r


def maintain(name: str) -> None:
    """After a job: flush the write-ahead log and refresh the planner statistics. Never creates a database."""
    if not store.db_path(name).exists():
        return
    with store.write_lock(name):
        conn = store.connect(name)
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            conn.execute("PRAGMA optimize")
        finally:
            conn.close()


def optimize_fts(name: str) -> None:
    """The night run: merge the keyword index's b-trees, so search stays fast after many small updates."""
    if not store.db_path(name).exists():
        return
    with store.write_lock(name):
        conn = store.connect(name)
        try:
            conn.execute("INSERT INTO fts(fts) VALUES('optimize')")
            conn.commit()
        finally:
            conn.close()


# ------------------------------------------------------------------ heavy stage

MEDIA_KINDS = ("audio", "video")
DOC_HEAVY_WORKERS = 3     # OCR / page rendering run in helper processes (Neural Engine + CPU): parallel helps


def heavy(coll: dict, progress=None, cancel=None, only_ids: list[int] | None = None,
          kinds: tuple | None = None, exclude_kinds: tuple | None = None, workers: int = 1,
          priority: str | None = None) -> dict:
    """OCR, page images, pictures, audio/video. Smallest files first so most of the index is complete early;
    documents can run several at a time, audio/video one at a time (Whisper shares the GPU). A file whose content is in
    the cloud is left pending, never downloaded, and counted as deferred. Before each file the idle gate is asked
    (_wait_for_heavy_work; `priority` None = indexing.priority; "always" = never paused, what Sync now and the night
    run pass)."""
    name = coll["name"]
    root = Path(coll["path"])
    media = _media(coll)
    prio = _priority(priority)
    conn = store.connect(name)
    # a file marked missing is not read: its pending work waits, and resumes once missing_since is cleared
    q = "SELECT id, path, kind, heavy, size FROM files WHERE heavy IS NOT NULL AND mode='full'" + _present(conn)
    rows = [dict(r) for r in conn.execute(q)]
    conn.close()
    if only_ids is not None:
        rows = [r for r in rows if r["id"] in only_ids]
    if kinds:
        rows = [r for r in rows if r["kind"] in kinds]
    if exclude_kinds:
        rows = [r for r in rows if r["kind"] not in exclude_kinds]
    rows.sort(key=lambda r: r["size"] or 0)
    stage = "audio & video" if kinds == MEDIA_KINDS else "ocr & pages"
    counts = {"done": 0, "failed": 0, "n": 0, "deferred": 0, "paused_s": 0.0}
    lock = threading.Lock()

    def one(r):
        if cancel and cancel():
            return
        paused = _wait_for_heavy_work(name, prio, cancel, progress, stage, counts["n"], len(rows))
        if paused:
            with lock:
                counts["paused_s"] += paused
        if _dataless(root / r["path"]):
            with lock:
                counts["deferred"] += 1
                counts["n"] += 1
            return
        try:
            res = extract.heavy(root / r["path"], r["kind"], json.loads(r["heavy"]), name, r["id"], media)
            err = None
        except Exception as e:
            res, err = {"items": [], "thumb": False, "duration": None}, f"{type(e).__name__}: {e}"[:400]
        _store_heavy(name, r, res, err)
        if err:
            liveprog.error(name, f"{Path(r['path']).name}: {err}")
        with lock:
            counts["done" if err is None else "failed"] += 1
            counts["n"] += 1
            if progress:
                progress(stage, counts["n"], len(rows), r["path"])

    if workers > 1 and len(rows) > 1:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="heavy") as ex:
            futures = [ex.submit(one, r) for r in rows]
            for f in as_completed(futures):
                f.result()
                if cancel and cancel():
                    ex.shutdown(wait=False, cancel_futures=True)
                    raise Cancelled()
    else:
        for r in rows:
            if cancel and cancel():
                raise Cancelled()
            one(r)
    if rows:
        with store.write_lock(name):
            conn = store.connect(name)
            store.set_meta(conn, "last_heavy", time.strftime("%Y-%m-%d %H:%M:%S"))
            conn.commit()
            conn.close()
    return {"files": len(rows), "done": counts["done"], "failed": counts["failed"], "deferred": counts["deferred"],
            "paused_s": round(counts["paused_s"], 2)}


def _store_heavy(name: str, r: dict, res: dict, err: str | None) -> None:
    with store.write_lock(name):
        conn = store.connect(name)
        exists = conn.execute("SELECT 1 FROM files WHERE id=?", (r["id"],)).fetchone()
        if exists:
            store.replace_items(conn, r["id"], name_tokens(r["path"]), res["items"],
                                modalities={"ocr", "image", "page", "audio", "video", "transcript"})
            # only the spec this read was for is cleared: a mark written while the read was running (mark_reprocess)
            # stays queued, because the stored spec no longer matches the one the read started from
            conn.execute("UPDATE files SET heavy=NULL, heavy_at=?, thumb=?, duration=COALESCE(?, duration),"
                         " error=COALESCE(?, error), state=CASE WHEN ? IS NULL THEN state ELSE 'error' END"
                         " WHERE id=? AND heavy IS ?",
                         (time.time(), int(bool(res["thumb"])), res["duration"], err, err, r["id"], r.get("heavy")))
            conn.commit()
        conn.close()


# File kinds that mark_reprocess may queue again: OCR for pdf and image, transcription for audio and video.
REPROCESS_TYPES = ("pdf", "image", "audio", "video")


def _reprocess_spec(conn, r: dict, media: dict | None = None) -> dict | None:
    """The heavy spec that redoes what a file already has, or None when there is nothing to redo. A PDF is redone on the
    pages that have OCR text and on the pages that have a page image, so the replacement touches those and no others.
    A picture is OCRed again whatever media.image_ocr says (the request is for OCR); a video follows media.video_mode."""
    if r["kind"] == "pdf":
        ocr = [x[0] for x in conn.execute(
            "SELECT DISTINCT page FROM items WHERE file_id=? AND modality='ocr' AND page IS NOT NULL ORDER BY page",
            (r["id"],))]
        visual = [x[0] for x in conn.execute(
            "SELECT DISTINCT page FROM items WHERE file_id=? AND modality='page' AND page IS NOT NULL ORDER BY page",
            (r["id"],))]
        if not ocr and not visual:
            return None
        spec = {"thumb": True}
        if ocr:
            spec["ocr"] = ocr
        if visual:
            spec["visual"] = visual
        return spec
    if r["kind"] == "image":
        return {"image": True, "ocr": True, "thumb": True}
    if r["kind"] == "audio":
        return {"audio": True}
    if r["kind"] == "video":
        spec = {"video": True, "thumb": True}
        if (media or {}).get("video_mode") == "poster":
            spec["poster"] = True
        return spec
    return None


def mark_reprocess(name: str, types, prefix: str | None = None, now: float | None = None) -> dict:
    """Queue OCR again (pdf, image) or transcription again (audio, video) for files that already have it. Nothing
    now: the file's heavy spec is set, which is what heavy() reads, so the next heavy run does the work. The old OCR,
    transcripts and images stay in place until heavy() replaces them by modality. A file already queued is left as it
    is. `prefix` limits the marks to one folder of the collection (a relative path)."""
    kinds = tuple(sorted({t for t in types if t in REPROCESS_TYPES}))
    by_type = {k: 0 for k in kinds}
    already_queued = nothing_to_redo = 0
    if not kinds:
        return {"marked": 0, "by_type": by_type, "already_queued": 0, "nothing_to_redo": 0}
    now = time.time() if now is None else now
    try:
        media = _media(config.get_collection(config.load(), name) or {})
    except Exception:
        media = _media({})
    with store.write_lock(name):
        conn = store.connect(name)
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, path, kind, heavy FROM files WHERE mode='full' AND kind IN ("
                + ",".join("?" * len(kinds)) + ")" + _present(conn), kinds)]
            for r in rows:
                if prefix and not (r["path"] == prefix or r["path"].startswith(prefix + "/")):
                    continue
                if r["heavy"] is not None:
                    already_queued += 1
                    continue
                spec = _reprocess_spec(conn, r, media)
                if spec is None:
                    nothing_to_redo += 1
                    continue
                spec["mark"] = now                  # a distinct spec per mark: a read that was running cannot clear it
                conn.execute("UPDATE files SET heavy=?, heavy_at=NULL WHERE id=?", (json.dumps(spec), r["id"]))
                by_type[r["kind"]] += 1
            conn.commit()
        finally:
            conn.close()
    return {"marked": sum(by_type.values()), "by_type": by_type, "already_queued": already_queued,
            "nothing_to_redo": nothing_to_redo}


# ------------------------------------------------------------------ embedding stage

def _stem(name: str | None) -> str:
    return re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name or "").replace("_", " ")


def item_prompt(r: dict) -> str:
    """The exact text a chunk is embedded as (also the cache key)."""
    if r["modality"] == "title":
        return doc_prompt(None, r["text"])
    head = r["heading"] if r["heading"] and not r["heading"].startswith("Page ") else None
    return doc_prompt(r["title"] or _stem(r["name"]), (head + "\n" if head else "") + (r["text"] or ""))


def prompt_key(prompt: str) -> str:
    return hashlib.blake2b(prompt.encode("utf-8"), digest_size=16).hexdigest()


def backfill_vcache(conn) -> None:
    """One-time: seed the vector cache from vectors computed before the cache existed."""
    if store.get_meta(conn, "vcache_backfilled") == "1":
        return
    rows = conn.execute(
        "SELECT i.modality, i.heading, i.text, f.title, f.name, v.v FROM items i JOIN files f ON f.id = i.file_id"
        f" JOIN vectors v ON v.item_id = i.id WHERE i.modality IN {TEXT_MODALITIES}")
    batch = []
    for r in rows:
        batch.append((prompt_key(item_prompt(dict(r))), r["v"]))
        if len(batch) >= 2000:
            conn.executemany("INSERT OR IGNORE INTO vcache(key, v) VALUES (?, ?)", batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT OR IGNORE INTO vcache(key, v) VALUES (?, ?)", batch)
    store.set_meta(conn, "vcache_backfilled", "1")



def pending_count(name: str, media: bool | None = None) -> int:
    conn = store.connect(name)
    q = "SELECT COUNT(*) FROM items WHERE embedded=0" + store.embed_skip_items(conn)
    if media is True:
        q += f" AND modality IN {MEDIA_MODALITIES}"
    elif media is False:
        q += f" AND modality IN {TEXT_MODALITIES}"
    n = conn.execute(q).fetchone()[0]
    conn.close()
    return n


def embed(coll: dict, progress=None, cancel=None, media: bool = True, only_file_ids: list[int] | None = None,
          batch_priority: bool = False, priority: str | None = None) -> dict:
    """Vectors for every pending item: cached prompts first (no model), then text in batches of similar length, then the
    media items. Before each model batch the idle gate is asked (_wait_for_heavy_work; `priority` None =
    indexing.priority, "always" = never paused)."""
    name = coll["name"]
    prio = _priority(priority)
    paused_s = 0.0
    p = pool()
    p.workers["text"].batch_priority = p.workers["full"].batch_priority = batch_priority
    conn = store.connect(name)
    flt = ""
    if only_file_ids:
        flt = f" AND i.file_id IN ({','.join(str(int(x)) for x in only_file_ids)})"
    text_rows = [dict(r) for r in conn.execute(
        "SELECT i.id, i.modality, i.heading, i.text, f.title, f.name FROM items i JOIN files f ON f.id=i.file_id"
        f" WHERE i.embedded=0 AND i.modality IN {TEXT_MODALITIES}{flt}{_present(conn, 'f.')}{NO_EMBED_F} ORDER BY i.id")]
    media_rows = [dict(r) for r in conn.execute(
        "SELECT i.id, i.file_id, i.modality, i.media, f.title, f.name FROM items i JOIN files f ON f.id=i.file_id"
        f" WHERE i.embedded=0 AND i.modality IN {MEDIA_MODALITIES}{flt}{_present(conn, 'f.')}{NO_EMBED_F}"
        " ORDER BY i.file_id, i.id")] if media else []
    conn.close()
    total = len(text_rows) + len(media_rows)
    done = failed = reused = 0
    for r in text_rows:
        r["prompt"] = item_prompt(r)
        r["key"] = prompt_key(r["prompt"])
    group = lambda r: "text" if r["modality"] != "title" else "title"
    # Identical inputs (copies of a file, re-extracted chunks) reuse their cached vector.
    with store.write_lock(name):
        conn = store.connect(name)
        backfill_vcache(conn)
        hits = {}
        keys = [r["key"] for r in text_rows]
        for i in range(0, len(keys), 900):
            part = keys[i:i + 900]
            hits.update(conn.execute(f"SELECT key, v FROM vcache WHERE key IN ({','.join('?' * len(part))})", part))
        cached = [r for r in text_rows if r["key"] in hits]
        for i in range(0, len(cached), 500):
            store.store_vectors(conn, [(r["id"], group(r), store.from_f16(hits[r["key"]])) for r in cached[i:i + 500]])
        conn.commit()
        conn.close()
    reused = len(cached)
    done += reused
    text_rows = [r for r in text_rows if r["key"] not in hits]
    # Similar lengths in one batch: padding to the longest item is what makes mixed batches slow.
    text_rows.sort(key=lambda r: len(r["prompt"]))
    B = 32
    for i in range(0, len(text_rows), B):
        if cancel and cancel():
            raise Cancelled()
        paused_s += _wait_for_heavy_work(name, prio, cancel, progress, "embedding text", done, total)
        part = text_rows[i:i + B]
        vecs = p.embed_texts([r["prompt"] for r in part], batch=B if len(part[-1]["prompt"]) < 600 else 16)
        with store.write_lock(name):
            conn = store.connect(name)
            store.store_vectors(conn, [(r["id"], group(r), v) for r, v in zip(part, vecs)])
            conn.executemany("INSERT OR REPLACE INTO vcache(key, v) VALUES (?, ?)",
                             [(r["key"], store.to_f16(v)) for r, v in zip(part, vecs)])
            conn.commit()
            conn.close()
        done += len(part)
        if progress:
            progress("embedding text", done, total, "")
    MB = 4
    touched_files = set()
    for i in range(0, len(media_rows), MB):
        if cancel and cancel():
            raise Cancelled()
        paused_s += _wait_for_heavy_work(name, prio, cancel, progress, "embedding media", done, total)
        part = [r for r in media_rows[i:i + MB]]
        ok_rows, bad_rows = [], []
        reqs = []
        for r in part:
            if r["media"] and os.path.exists(r["media"]):
                reqs.append(r)
            else:
                bad_rows.append(r)
        if reqs:
            items = [{"type": "image" if r["modality"] in ("image", "page") else r["modality"],
                      "path": r["media"], "title": r["title"] or _stem(r["name"])} for r in reqs]
            try:
                vecs, oks = p.embed_media(items)
            except Exception:
                vecs, oks = None, [False] * len(reqs)
            for r, ok, k in zip(reqs, oks, range(len(reqs))):
                (ok_rows.append((r, vecs[k])) if ok and vecs is not None and np.isfinite(vecs[k]).all()
                 else bad_rows.append(r))
        with store.write_lock(name):
            conn = store.connect(name)
            group = {"image": "visual", "page": "visual", "video": "visual", "audio": "audio"}
            store.store_vectors(conn, [(r["id"], group[r["modality"]], v) for r, v in ok_rows])
            for r in bad_rows:
                conn.execute("UPDATE items SET embedded=-1 WHERE id=?", (r["id"],))
            conn.commit()
            conn.close()
        for r in part:
            touched_files.add(r["file_id"])
        done += len(part)
        failed += len(bad_rows)
        if progress:
            progress("embedding media", done, total, "")
    for fid in touched_files:
        extract.cleanup_work(name, fid)
    if total:
        with store.write_lock(name):
            conn = store.connect(name)
            store.set_meta(conn, "last_embed", time.strftime("%Y-%m-%d %H:%M:%S"))
            conn.commit()
            conn.close()
    return {"embedded": done - failed, "reused": reused, "failed": failed, "paused_s": round(paused_s, 2)}


# ------------------------------------------------------------------ vocabulary (cross-language bridge)

_TERM_OK = re.compile(r"^[a-z][a-z\-]{3,30}$")


def build_vocab(coll: dict, progress=None, cancel=None, max_terms: int = 25000) -> dict:
    """Embed the collection's own vocabulary so a query can be bridged to the words documents actually use."""
    name = coll["name"]
    conn = store.connect(name)
    rows = conn.execute("SELECT term, doc FROM fts_terms WHERE doc >= 2 ORDER BY doc DESC LIMIT ?",
                        (max_terms * 2,)).fetchall()
    terms = {}
    for t, df in rows:
        if _TERM_OK.match(t) and t not in STOP and not re.search(r"(.)\1\1", t):
            terms[t] = df
        if len(terms) >= max_terms:
            break
    have = {r[0]: r[1] for r in conn.execute("SELECT term, id FROM vocab")}
    conn.close()
    new = [t for t in terms if t not in have]
    gone = [have[t] for t in have if t not in terms]
    p = pool()
    B = 128
    for i in range(0, len(new), B):
        if cancel and cancel():
            raise Cancelled()
        part = new[i:i + B]
        vecs = p.embed_texts([QUERY + t for t in part], batch=B)
        with store.write_lock(name):
            conn = store.connect(name)
            for t, v in zip(part, vecs):
                tid = conn.execute("INSERT OR REPLACE INTO vocab(term, df) VALUES (?,?)", (t, terms[t])).lastrowid
                conn.execute("DELETE FROM vvocab WHERE id=?", (tid,))
                conn.execute("INSERT INTO vvocab(id, embedding) VALUES (?, ?)", (tid, np.asarray(v, np.float32).tobytes()))
            conn.commit()
            conn.close()
        if progress:
            progress("vocabulary", min(i + B, len(new)), len(new), "")
    if gone:
        with store.write_lock(name):
            conn = store.connect(name)
            for k in range(0, len(gone), 500):
                part = gone[k:k + 500]
                q = ",".join("?" * len(part))
                conn.execute(f"DELETE FROM vocab WHERE id IN ({q})", part)
                conn.execute(f"DELETE FROM vvocab WHERE id IN ({q})", part)
            conn.commit()
            conn.close()
    return {"terms": len(terms), "added": len(new), "removed": len(gone)}


def drop_collection_data(name: str) -> str:
    """Move a removed collection's derived data to trash (never touches the indexed folder)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = paths.TRASH / f"{name}-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        f = paths.COLLECTIONS / f"{name}.db{suffix}"
        if f.exists():
            shutil.move(str(f), dest / f.name)
    for d in (paths.THUMBS / name, paths.TMP / name):
        if d.exists():
            shutil.move(str(d), dest / d.name)
    return str(dest)
