"""gitignore-style path patterns (subset): `*`, `**`, `?`, `[..]`, trailing `/`, anchoring by inner `/`.

Matching is case-insensitive (macOS file systems are) and a pattern that matches a
directory also matches everything below it. A name may hold any character, control characters included (a newline,
a carriage return, a tab): see compile_pattern.
"""

import os
import re
from functools import lru_cache

# Never read, never listed, in any collection.
SECRET_PATTERNS = [
    # folders by name, at any depth: credential stores, recovery codes, secrets, key chains
    "**/*credential*/", "**/*recoverycode*/", "**/*recovery_code*/", "**/*recovery-code*/", "**/*secret*/",
    "**/*recovery*code*",
    ".env", ".env.*", "**/.env", "**/.env.*",
    ".mcp.json", "**/mcp.json",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "*.kdbx", "*.ovpn",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.ppk",
    ".ssh/", ".gnupg/", ".aws/", "Keychains/",
    "**/*credential*", "**/*secret*", "**/*password*",
]

# Skipped by default in every collection (build output, caches, tool state).
DEFAULT_EXCLUDES = [
    ".*/",                      # hidden folders: .git, .venv, .claude, .cursor ...
    ".DS_Store", "._*", "Icon\r",
    "node_modules/", "__pycache__/", "venv/", ".venv/", "site-packages/",
    "*.pyc", "*.pyo", "*.o", "*.so", "*.dylib", "*.class",
    "*.lock", "*.tmp", "*.swp", "~$*",
    "graphify-out/", ".index/", "_chunks/",
    # agent configs that `mse agents install` writes into a folder (they can hold other servers' API keys);
    # .mcp.json / .cursor/mcp.json are secret patterns above, .claude/ .codex/ .grok/ are hidden folders
    "opencode.json", "opencode.jsonc",
]

# Keynote decks end in .key, as private keys do. Only the KEY_PATTERN secret is relaxed for a deck, and only by
# indexer._secret_excluded: a folder named *.key (the package) or a regular file over KEYNOTE_MIN_BYTES. A .key file of
# that size or less is a private key and stays a secret. Every other secret rule still applies to a deck.
KEY_PATTERN = "*.key"
KEYNOTE_MIN_BYTES = 64 * 1024


def secret_rules_without_key() -> list[str]:
    """SECRET_PATTERNS without the *.key pattern: the rules that still exclude a Keynote deck, whatever its size."""
    return [p for p in SECRET_PATTERNS if p != KEY_PATTERN]


def is_key_name(name: str) -> bool:
    return name.lower().endswith(".key")


def _translate(glob: str) -> str:
    out, i, n = [], 0, len(glob)
    while i < n:
        c = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("/**", i) and i + 3 == n:
            out.append("(?:/.*)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = glob.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                out.append("[" + glob[i + 1:j].replace("\\", "\\\\") + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _spellings(pattern: str) -> list[str]:
    """The pattern as it is read: trimmed of surrounding whitespace, and, when the text as typed differs only by a
    control character at either end (the macOS custom-icon file is named "Icon\\r"), that text too. A spelling only ever
    adds matches, so every name the trimmed reading excluded is still excluded."""
    p = pattern.strip()
    raw = pattern.strip(" ")
    return [p] if raw == p else [p, raw]


def _regex_source(p: str) -> str:
    p = p.rstrip("/") if p.endswith("/") else p
    anchored = p.startswith("/") or "/" in p.rstrip("/")
    p = p.lstrip("/")
    body = _translate(p)
    prefix = "^" if anchored else "^(?:.*/)?"
    # `$` also takes one newline at the very end of a name, so ".env\n" is still a ".env". Keep it: `\Z` would
    # drop that name from the exclusions, which is the one thing these rules must never do.
    return prefix + body + "(?:/.*)?$"


@lru_cache(maxsize=4096)
def compile_pattern(pattern: str) -> re.Pattern:
    # re.DOTALL: every `.` above (the `**` and `/**` translations) must match any character, a newline included. Without
    # it a rule switched off under a folder whose name holds a newline ("a\nb/.env", "a\nb/Credentials/x.txt",
    # "01_Payroll/a\nb/payslip.pdf"). macindex.py compiles the joined pattern text with the same flag.
    srcs = [_regex_source(s) for s in _spellings(pattern)]
    src = srcs[0] if len(srcs) == 1 else "|".join(f"(?:{s})" for s in srcs)
    return re.compile(src, re.IGNORECASE | re.DOTALL)


class Matcher:
    def __init__(self, patterns):
        self.patterns = [p for p in (patterns or []) if p and not p.strip().startswith("#")]
        self._rx = [compile_pattern(p) for p in self.patterns]

    def match(self, relpath: str) -> bool:
        return any(rx.match(relpath) for rx in self._rx)

    def __bool__(self):
        return bool(self._rx)


# ---------------------------------------------------------------- folders a collection never enters
# The walk prunes these before it descends into them (indexer._Scope). The value is the rule:
#   "always"  the name alone prunes the folder: dependency, cache and tool-state folders;
#   "project" pruned only when a project marker sits beside it, in the folder that holds it, so a folder named "build"
#             or "dist" that holds the owner's own documents is kept;
#   "pyvenv"  pruned only when it holds pyvenv.cfg (a Python virtual environment called env).
PRUNE_DIRS: dict[str, str] = {
    "node_modules": "always", "__pycache__": "always", ".venv": "always", "venv": "always",
    "site-packages": "always", ".git": "always", ".hg": "always", ".svn": "always",
    ".mypy_cache": "always", ".pytest_cache": "always", ".ruff_cache": "always", ".tox": "always",
    ".next": "always", ".nuxt": "always", ".svelte-kit": "always", ".turbo": "always",
    ".parcel-cache": "always", ".cache": "always", ".gradle": "always", "DerivedData": "always",
    ".build": "always", ".swiftpm": "always", ".idea": "always", ".vscode": "always",
    ".terraform": "always", ".serverless": "always",
    "env": "pyvenv",
    "build": "project", "dist": "project", "out": "project", "target": "project", "vendor": "project",
    "bower_components": "project", "coverage": "project", "Pods": "project", "Carthage": "project",
}
# The folder names HEAD already excluded (DEFAULT_EXCLUDES). No indexing.prune_dirs setting can bring them back.
FLOOR_DIRS = frozenset({"node_modules", "__pycache__", "venv", ".venv", "site-packages", ".git"})
PROJECT_MARKERS = frozenset({"package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod", "pom.xml",
                             "build.gradle", "Package.swift", "Makefile", "CMakeLists.txt", "composer.json"})
# A project marker that is specific to one kind of folder, in addition to PROJECT_MARKERS.
MARKER_EXTRA = {"Pods": frozenset({"Podfile"}), "Carthage": frozenset({"Cartfile"}),
                "bower_components": frozenset({"bower.json"})}
PRUNE_NAME_OK = r"^[A-Za-z0-9._-]{1,100}$"          # a folder name in indexing.prune_dirs: plain, no glob characters


def effective_prune(names=None) -> dict[str, str]:
    """The folder rules the walk applies: the built-in table, or, when indexing.prune_dirs is a list, that list (a name
    outside the table is pruned by name). The FLOOR_DIRS are always pruned."""
    table = dict(PRUNE_DIRS) if names is None else {n: PRUNE_DIRS.get(n, "always") for n in names}
    for n in FLOOR_DIRS:
        table[n] = "always"
    return table


# Never indexed by content: disk images and virtual machines (their names stay searchable).
CONTENT_SKIP_EXT = frozenset({"dmg", "iso", "pkg", "img", "sparseimage", "vmdk", "vdi", "qcow2", "vhd", "vhdx", "ova",
                              "app", "framework", "xcframework"})
# Compiled binaries, library archives, packaged apps and fonts: name only, never read, never embedded. Several are zip
# files (jar, war, ipa, apk, aab), so they must be kept out before any archive sniffing in extract.light.
BINARY_EXT = frozenset({"dylib", "so", "a", "o", "obj", "lib", "dll", "exe", "class", "jar", "war", "pyc", "pyo",
                        "wasm", "node", "bundle", "ipa", "apk", "aab", "pak", "bin", "xcarchive",
                        "ttf", "otf", "ttc", "woff", "woff2", "eot"})
# Folder-shaped bundles: one name-only entry each, never walked into (as .app is).
BUNDLE_DIR_EXT = frozenset({"app", "framework", "xcframework", "xcarchive", "bundle"})
# Data files that are name-only above a size (a small .dat is a plain file and is read as any other).
SIZE_SKIP_EXT = {"dat": 1024 * 1024}
# Logs and databases over this size are not read for their content.
BIG_SKIP_EXT = frozenset({"log", "db", "sqlite", "sqlite3"})
BIG_SKIP_BYTES = 10 * 1024 * 1024
# Name-only files whose items are never embedded (the SQL predicate in store.py uses the same list): source code (by
# kind), and every binary or font. Their names stay searchable through the keyword index.
NO_EMBED_EXT = BINARY_EXT | frozenset({"dat"})
# Caches folders under a Library folder (Library/Caches, and the like) are pruned by name and parent.
CACHE_DIR = "Caches"
CACHE_PARENT = "Library"


def content_skipped(rel: str, size: int) -> bool:
    ext = os.path.splitext(rel)[1].lower().lstrip(".")
    if ext in CONTENT_SKIP_EXT or ext in BINARY_EXT:
        return True
    if ext in SIZE_SKIP_EXT and size > SIZE_SKIP_EXT[ext]:
        return True
    return ext in BIG_SKIP_EXT and size > BIG_SKIP_BYTES
