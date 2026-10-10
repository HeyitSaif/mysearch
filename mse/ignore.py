"""gitignore-style path patterns (subset): `*`, `**`, `?`, `[..]`, trailing `/`, anchoring by inner `/`.

Matching is case-insensitive (macOS file systems are) and a pattern that matches a
directory also matches everything below it.
"""

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


@lru_cache(maxsize=4096)
def compile_pattern(pattern: str) -> re.Pattern:
    p = pattern.strip()
    p = p.rstrip("/") if p.endswith("/") else p
    anchored = p.startswith("/") or "/" in p.rstrip("/")
    p = p.lstrip("/")
    body = _translate(p)
    prefix = "^" if anchored else "^(?:.*/)?"
    return re.compile(prefix + body + "(?:/.*)?$", re.IGNORECASE)


class Matcher:
    def __init__(self, patterns):
        self.patterns = [p for p in (patterns or []) if p and not p.strip().startswith("#")]
        self._rx = [compile_pattern(p) for p in self.patterns]

    def match(self, relpath: str) -> bool:
        return any(rx.match(relpath) for rx in self._rx)

    def __bool__(self):
        return bool(self._rx)
