"""A small, safe matcher for `.gitignore` and `.ignore` files, for `mac_index.respect_gitignore` (off by default).

When the setting is on, a name result inside a git repository is left out if the repository's own ignore files ignore it
(build folders, generated files, ...). The ignore files are read when a result needs them, never stored, never shown, and
only those of a folder that already passed the privacy gate; the name index itself is not changed, so switching the setting
off brings the files back at once.

What is understood (gitignore(5)): blank lines and `#` comments, `!` to re-include, a trailing `/` for folders only, a
`/` at the start or in the middle to anchor a pattern to its ignore file's folder, `*` `?` `[a-z]` `[!a-z]`, `**/x`,
`x/**` and `a/**/b`, `\\` escapes, trailing spaces. A file below an ignored folder stays ignored whatever a later rule says
(as git does). Deeper ignore files override shallower ones, `.ignore` overrides `.gitignore` in the same folder. The nearest
`.git` (folder or file) above a result is its repository's root. Matching is case-insensitive, as git is on a default Mac.
Not read: `.git/info/exclude`, the global excludes file.

The matcher is bounded on purpose, so a hostile ignore file cannot make a search run for long: ignore files over 128 KB
are skipped, at most 2000 rules and 500 characters per rule are read, and there is no regular expression anywhere. A name
is matched against a rule with the classic wildcard algorithm (a pattern with N characters against a name of M: at most
N x M steps), and `**` is matched on whole folders (at most patterns x folders states). Written from the file format only.
"""

import os
import stat
import time

MAX_FILE_BYTES = 128 * 1024
MAX_RULES = 2000
MAX_PATTERN = 500
MAX_DEPTH = 60
RECHECK_S = 5.0
CACHE_MAX = 6000
NAMES = (".gitignore", ".ignore")          # later names override earlier ones in the same folder

# tokens of one path segment: ("c", char) ("?",) ("*",) ("[", frozenset(chars), ((lo, hi), ...), negated)
DSTAR = "**"          # a whole segment: any number of folders (also none)
DSTAR1 = "**+"        # a trailing `/**`: one or more (it matches what is inside, not the folder itself)


class Rule:
    __slots__ = ("segs", "neg", "dir_only", "anchored")

    def __init__(self, segs, neg, dir_only, anchored):
        self.segs, self.neg, self.dir_only, self.anchored = segs, neg, dir_only, anchored


def _tokens(seg: str, plain: bool = False) -> list:
    """One path segment as tokens. plain=True (the query language's globs): only `*` and `?` are wildcards, every other
    character, `[` and `\\` included, is itself."""
    out, i, n = [], 0, len(seg)
    while i < n:
        c = seg[i]
        if plain and c not in "*?":
            out.append(("c", c.lower()))
            i += 1
        elif c == "\\" and i + 1 < n:
            out.append(("c", seg[i + 1].lower()))
            i += 2
        elif c == "*":
            while i < n and seg[i] == "*":
                i += 1
            out.append(("*",))
        elif c == "?":
            out.append(("?",))
            i += 1
        elif c == "[":
            j = i + 1
            neg = j < n and seg[j] in "!^"
            if neg:
                j += 1
            k = j + 1 if j < n and seg[j] == "]" else j          # a `]` right after `[` or `[!` is a member
            close = seg.find("]", k)
            if close == -1:
                out.append(("c", "["))
                i += 1
                continue
            chars, ranges, b = set(), [], seg[j:close].lower()
            m = 0
            while m < len(b):
                if m + 2 < len(b) and b[m + 1] == "-":
                    ranges.append((b[m], b[m + 2]))
                    m += 3
                else:
                    chars.add(b[m])
                    m += 1
            out.append(("[", frozenset(chars), tuple(ranges), neg))
            i = close + 1
        else:
            out.append(("c", c.lower()))
            i += 1
    return out


def _tok_ok(tok, ch: str) -> bool:
    kind = tok[0]
    if kind == "c":
        return tok[1] == ch
    if kind == "?":
        return True
    hit = ch in tok[1] or any(lo <= ch <= hi for lo, hi in tok[2])
    return hit != tok[3]


def _wild(tokens: list, text: str) -> bool:
    """Wildcard match of one segment: `*` any run, `?` one character. One saved restart point, so at most len x len steps."""
    t = p = 0
    star = mark = -1
    n, m = len(text), len(tokens)
    while t < n:
        if p < m and tokens[p][0] == "*":
            star, mark = p, t
            p += 1
        elif p < m and tokens[p][0] != "*" and _tok_ok(tokens[p], text[t]):
            p += 1
            t += 1
        elif star != -1:
            mark += 1
            t, p = mark, star + 1
        else:
            return False
    while p < m and tokens[p][0] == "*":
        p += 1
    return p == m


def _segs_match(segs: list, comps: list[str]) -> bool:
    """Do the pattern's segments match the path's folder and file names? `**` segments take any number of folders."""
    memo: dict[tuple[int, int], bool] = {}

    def m(i: int, j: int) -> bool:
        key = (i, j)
        if key in memo:
            return memo[key]
        if i == len(segs):
            r = j == len(comps)
        elif segs[i] == DSTAR:
            r = m(i + 1, j) or (j < len(comps) and m(i, j + 1))
        elif segs[i] == DSTAR1:
            r = j < len(comps)                     # a trailing `/**`: anything inside (at least one more name)
        elif j < len(comps) and _wild(segs[i], comps[j].lower()):
            r = m(i + 1, j + 1)
        else:
            r = False
        memo[key] = r
        return r
    return m(0, 0)


def parse(text: str) -> list[Rule]:
    rules: list[Rule] = []
    for line in text.splitlines():
        if len(rules) >= MAX_RULES:
            break
        if not line or line.startswith("#"):
            continue
        stripped = line.rstrip(" ")
        if stripped.endswith("\\") and len(stripped) < len(line):
            stripped += " "                         # an escaped trailing space is part of the name
        line = stripped
        if not line or len(line) > MAX_PATTERN:
            continue
        neg = line.startswith("!")
        if neg:
            line = line[1:]
        elif line.startswith("\\!") or line.startswith("\\#"):
            line = line[1:]
        dir_only = line.endswith("/")
        if dir_only:
            line = line.rstrip("/")
        if not line:
            continue
        anchored = "/" in line
        parts = [p for p in line.lstrip("/").split("/")]
        segs: list = []
        for k, p in enumerate(parts):
            if p == "**":
                segs.append(DSTAR1 if k == len(parts) - 1 and len(parts) > 1 else DSTAR)
            elif p:
                segs.append(_tokens(p))
        if not segs:
            continue
        if not anchored and segs[0] in (DSTAR, DSTAR1):    # `**` alone
            segs, anchored = [DSTAR1], True
        rules.append(Rule(segs, neg, dir_only, anchored))
    return rules


def _decide(rules: list[Rule], comps: list[str], is_dir: bool):
    """True = ignored, False = re-included, None = no rule says. The last matching rule wins."""
    verdict = None
    last = comps[-1:]
    for r in rules:
        if r.dir_only and not is_dir:
            continue
        if (_segs_match(r.segs, comps) if r.anchored else _segs_match(r.segs, last)):
            verdict = not r.neg
    return verdict


def _read_rules(path: str) -> list[Rule]:
    """The rules in one ignore file, or none. Only a regular file is read, and never through a symbolic link (one could
    point at a file the privacy rules block) or a pipe (opening it would wait forever)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return []
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
            return []
        data = b""
        while len(data) < MAX_FILE_BYTES:
            chunk = os.read(fd, MAX_FILE_BYTES - len(data))
            if not chunk:
                break
            data += chunk
        return parse(data.decode("utf-8", "replace"))
    except OSError:
        return []
    finally:
        os.close(fd)


class GitIgnore:
    def __init__(self, blocked=None, clock=time.monotonic):
        """blocked(path) -> True for a path that must not be read (the privacy rules); clock: for tests."""
        self._blocked = blocked
        self._clock = clock
        self._files: dict[str, tuple[float, list[Rule]]] = {}      # folder -> (checked, rules of its ignore files)
        self._root: dict[str, tuple[float, str | None]] = {}       # folder -> (checked, repo root or None)
        self._dirs: dict[str, tuple[float, bool]] = {}             # folder -> (checked, ignored?)

    # ---- rules of one folder
    def _load(self, folder: str) -> list[Rule]:
        now = self._clock()
        hit = self._files.get(folder)
        if hit and now - hit[0] < RECHECK_S:
            return hit[1]
        rules: list[Rule] = []
        for name in NAMES:
            f = folder + "/" + name
            if self._blocked is not None and self._blocked(f):
                continue
            rules += _read_rules(f)
        if len(self._files) >= CACHE_MAX:
            self._files.clear()
        self._files[folder] = (now, rules)
        return rules

    def _repo(self, folder: str) -> str | None:
        now = self._clock()
        hit = self._root.get(folder)
        if hit and now - hit[0] < 30.0:
            return hit[1]
        d, found, seen = folder, None, []
        for _ in range(MAX_DEPTH):
            seen.append(d)
            if os.path.lexists(d + "/.git"):
                found = d
                break
            parent = os.path.dirname(d)
            if parent == d or not parent:
                break
            d = parent
        if len(self._root) >= CACHE_MAX:
            self._root.clear()
        for x in seen:                                  # every folder on the way up has the same repository (or none)
            self._root[x] = (now, found)
        return found

    # ---- one path
    def _sets(self, root: str, folder: str) -> list[tuple[int, list[Rule]]]:
        """(depth of the ignore file's folder below the repo root, its rules) for root, ..., folder."""
        out, cur = [], root
        rules = self._load(cur)
        if rules:
            out.append((0, rules))
        if folder != root:
            for depth, comp in enumerate(folder[len(root) + 1:].split("/"), 1):
                cur += "/" + comp
                rules = self._load(cur)
                if rules:
                    out.append((depth, rules))
        return out

    def _verdict(self, root: str, path: str, is_dir: bool):
        comps = path[len(root) + 1:].split("/")
        verdict = None
        for depth, rules in self._sets(root, path.rpartition("/")[0]):
            v = _decide(rules, comps[depth:], is_dir)
            if v is not None:
                verdict = v
        return verdict

    def _ignored_dir(self, root: str, folder: str) -> bool:
        """Is this folder, or one above it below the repo root, ignored? Remembered for a few seconds."""
        if len(folder) <= len(root):
            return False
        now = self._clock()
        hit = self._dirs.get(folder)
        if hit and now - hit[0] < RECHECK_S:
            return hit[1]
        res = self._ignored_dir(root, folder.rpartition("/")[0]) or self._verdict(root, folder, True) is True
        if len(self._dirs) >= CACHE_MAX:
            self._dirs.clear()
        self._dirs[folder] = (now, res)
        return res

    def keep(self, path: str, is_dir: bool | None = None) -> bool:
        """False when the ignore files of the git repository that holds `path` ignore it."""
        folder = path.rpartition("/")[0]
        if not folder:
            return True
        root = self._repo(folder)
        if root is None or len(path) <= len(root) + 1:
            return True
        if self._ignored_dir(root, folder):
            return False
        if is_dir is None:
            dir_rules = any(r.dir_only for _, rules in self._sets(root, folder) for r in rules)
            is_dir = os.path.isdir(path) if dir_rules else False
        return self._verdict(root, path, is_dir) is not True


def linked_worktree_repo(dotgit: str) -> str | None:
    """For a `.git` FILE that belongs to a linked worktree, the `.git` folder of the repository it was made from (the
    path of that folder as the file names it, normalised); None for anything else: a folder, a submodule's `.git` file,
    an unreadable or oversized file. The file is read the way ignore files are (bounded, never through a symbolic link)."""
    try:
        fd = os.open(dotgit, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 4096:
            return None
        text = os.read(fd, 4096).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)
    first = text.strip().splitlines()[0] if text.strip() else ""
    if not first.startswith("gitdir:"):
        return None
    target = first[len("gitdir:"):].strip()
    if not target:
        return None
    if not os.path.isabs(target):                       # git may write a path relative to the worktree folder
        target = os.path.join(os.path.dirname(dotgit), target)
    target = os.path.normpath(target)
    # a linked worktree's gitdir is <repo>/.git/worktrees/<name>; a submodule's is <repo>/.git/modules/<name>
    if os.path.basename(os.path.dirname(target)) != "worktrees":
        return None
    dotgit_main = os.path.dirname(os.path.dirname(target))
    return dotgit_main if os.path.basename(dotgit_main) == ".git" else None
