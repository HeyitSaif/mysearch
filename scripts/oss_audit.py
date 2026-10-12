#!/usr/bin/env python3
"""Public-release audit (standard library only).

    scripts/oss_audit.py <dir> [--private-dir DIR]

Scans every file under <dir>, its file names included, for things that must not be published:

  * secrets: API keys and tokens (Anthropic, OpenAI-style, Stripe live, GitHub, AWS, Slack), private key blocks;
  * e-mail addresses other than *@example.com, *@users.noreply.github.com and noreply@*;
  * IBAN-shaped numbers, except the published example IBANs;
  * home-folder paths /Users/<name> for any name other than "alex" (the placeholder used in examples);
  * every term in DIR/denylist.txt, the user's private list (default DIR: .oss-private next to this script), except in the
    verbatim licence texts of licenses/ (VERBATIM_LICENCE_TEXTS: exact path and SHA-256, all other checks still apply);
  * PNG chunks: no iCCP (a device colour profile), no eXIf, tEXt, zTXt or iTXt, except the one reviewed file in
    PNG_EXIF_ALLOWED. A PNG that cannot be read to its IEND, or has a bad CRC, is a hit (so a damaged file cannot
    hide a chunk). sRGB and gAMA are fine. The chunk types of every PNG are listed after the binary files.

Matching rules. Before anything is matched, two kinds of text are masked, so that they cannot cause a hit:
content digests (sha1/sha256/sha384/sha512 values, as in lockfiles), the invented fixture values listed in
PUBLIC_EXEMPT, and each phrase in DIR/allowlist.txt, matched case-insensitively. Denylist terms match as case-insensitive substrings of the masked text. Binary files are scanned
through their printable strings (runs of 6 or more printable ASCII characters) and are listed separately.

Exit status: 0 when nothing is found, 1 when anything is found, 2 on a usage error. A missing denylist or allowlist
is reported on stderr and that check is skipped (the secret, e-mail, IBAN and path checks always run).
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
import sys
import zlib
from pathlib import Path

SECRET_RX = [
    ("API key (Anthropic)", re.compile(r"sk-" r"ant-")),
    ("API key (sk-...)", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("API key (Stripe live)", re.compile(r"sk_" r"live_")),
    ("token (GitHub)", re.compile(r"gh" r"p_")),
    ("token (GitHub fine-grained)", re.compile(r"github" r"_pat_")),
    ("key id (AWS)", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("token (Slack)", re.compile(r"xox[abp]-")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY")),
]
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_OK_DOMAINS = ("example.com", "users.noreply.github.com")
# Domains reserved for documentation and testing (RFC 2606 and RFC 6761): no one's real address is under them.
RESERVED_EMAIL_SUFFIXES = (".example.com", ".example.net", ".example.org", ".test", ".example", ".invalid", ".localhost")
HOME_RX = re.compile(r"/Users/(?!alex(?![A-Za-z]))[A-Za-z][A-Za-z0-9_.-]*")     # alex, alex2 ... are placeholders
IBAN_RX = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
IBAN_SPACED_RX = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,4})?\b")
DIGEST_RX = re.compile(r"\b(?:sha(?:1|256|384|512)|md5)[-:][A-Za-z0-9+/=_-]+")
# Published example IBANs (ISO 13616 and bank documentation examples), with spaces removed.
EXAMPLE_IBANS = {"DE89370400440532013000", "GB82WEST12345698765432", "FR1420041010050500013M02606",
                 "NL91ABNA0417164300", "CH9300762011623852957", "ES9121000418450200051332"}
# Invented values the public test suite uses on purpose: they look like credentials or a personal address, but they
# are not secrets. Written in pieces so that this file does not match its own rules.
PUBLIC_EXEMPT = ("sk-" "ant-test-0123456789abcdefSECRETKEY", "gh" "p_FAKEFAKEFAKE", "pw@x" ".example",
                 "/Users/" "Shared",
                 # a test writes this fake key file on purpose (tests/test_indexing_safety.py); the body is "x" * 100
                 'write_bytes(b"-----BEGIN ' 'PRIVATE KEY-----\\n" + b"x" * 100)')
PRINTABLE_RUN = re.compile(rb"[\x20-\x7e]{6,}")
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".icns", ".pdf", ".zip", ".dmg", ".ttf", ".otf", ".woff", ".woff2",
              ".mp3", ".mp4", ".mov", ".wav", ".m4a", ".heic", ".gz", ".tgz", ".xlsx", ".docx", ".pptx", ".db",
              ".sqlite", ".safetensors", ".bin", ".npz", ".pyc", ".so", ".dylib", ".o", ".a"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Chunks a public PNG must not carry. iCCP is a device colour profile; a display profile can carry the display's serial
# number. sRGB and gAMA are not listed, so they pass.
PNG_FORBIDDEN = {
    b"iCCP": "device colour profile, which can carry a display serial number: strip it",
    b"tEXt": "text metadata: strip it",
    b"zTXt": "compressed text metadata: strip it",
    b"iTXt": "international text metadata: strip it",
}
# The one file that may carry an eXIf chunk, by exact path and SHA-256 of its whole contents. Reviewed by reading its
# tag IDs: the eXIf holds only a colour-space flag and the pixel dimensions (512 x 512), with no make, model, time,
# place or serial number. Any other PNG with eXIf is a hit. This file is a hit again once its bytes change: review
# it, then update the digest here.
PNG_EXIF_ALLOWED = {
    "integrations/raycast/assets/icon.png": "4c4d598ee80f7e8f813f9e3e9c0470b5bff89c33093f87f9d8cfd9782c6966e7",
}


# Third-party licence texts that are stored byte for byte (licenses/README.md gives their sources) and cannot be edited.
# Licence texts are exempt from the project's private word list because they are verbatim upstream texts, pinned by
# sha256. By exact path and SHA-256 of the whole file, like PNG_EXIF_ALLOWED: the denylist is not applied to a file whose
# bytes equal the digest. Every other check
# (secrets, e-mail addresses, IBANs, home paths) still applies to it, and a file whose bytes changed is checked in full.
VERBATIM_LICENCE_TEXTS = {
    "licenses/GPL-3.0.txt": "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986",
    "licenses/GPL-2.0.txt": "edaef632cbb643e4e7a221717a6c441a4c1a7c918e6e4d56debc3d8739b233f6",
    "licenses/LGPL-2.1.txt": "20e50fe7aae3e56378ebf0417d9de904f55a0e61e4df315333e632a4d3555d95",
    "licenses/LGPL-3.0.txt": "e3a994d82e644b03a792a930f574002658412f62407f5fee083f2555c5f23118",
    "licenses/GCC-Runtime-Library-Exception-3.1.txt": "9d6b43ce4d8de0c878bf16b54d8e7a10d9bd42b75178153e3af6a815bdc90f74",
    "licenses/FFmpeg-7.1.1-LICENSE.md": "cb48bf09a11f5fb576cddb0431c8f5ed0a60157a9ec942adffc13907cbe083f2",
    "licenses/MPL-2.0.txt": "3f3d9e0024b1921b067d6f7f88deb4a60cbe7a78e76c64e3f1d7fc3b779b9d04",
}


def _read_terms(path: Path) -> list[str] | None:
    if not path.is_file():
        return None
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _mask_allow(text: str, allow_rx) -> str:
    text = DIGEST_RX.sub("<digest>", text)
    if allow_rx is not None:
        text = allow_rx.sub("<allowed>", text)
    return text


def _iban_hits(text: str) -> list[str]:
    hits = []
    for rx in (IBAN_RX, IBAN_SPACED_RX):
        for m in rx.finditer(text):
            if re.sub(r"\s", "", m.group(0)) not in EXAMPLE_IBANS:
                hits.append(m.group(0))
    return hits


def _binary_text(data: bytes, suffix: str) -> bytes:
    """The bytes of a binary file that can hold readable text. A PNG's pixel data is compressed noise that
    matches short words by chance, so for PNG only the text and EXIF chunks are scanned; other binaries are
    scanned whole, through their printable runs."""
    if suffix != ".png" or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data
    out, i = [], 8
    while i + 8 <= len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        kind = data[i + 4:i + 8]
        if kind in (b"tEXt", b"zTXt", b"iTXt", b"eXIf"):
            out.append(data[i + 8:i + 8 + length])
        i += 12 + length
    return b"\n".join(out)


def png_chunks(data: bytes) -> list[tuple[bytes, int]]:
    """(type, length) of every chunk of a PNG, in file order. Raises ValueError unless the file is a complete, well-formed
    PNG (valid signature, every CRC matches, IEND last, nothing after it), so that a damaged file cannot hide a chunk.
    Error messages name chunk types only, never their contents."""
    if data[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG")
    out = []
    i = 8
    while True:
        if i + 12 > len(data):
            raise ValueError("the file ends before IEND")
        length = struct.unpack(">I", data[i:i + 4])[0]
        end = i + 12 + length
        if end > len(data):
            raise ValueError("a chunk runs past the end of the file")
        kind = data[i + 4:i + 8]
        if zlib.crc32(data[i + 4:end - 4]) & 0xFFFFFFFF != struct.unpack(">I", data[end - 4:end])[0]:
            raise ValueError("bad CRC in a %s chunk" % kind.decode("latin-1"))
        out.append((kind, length))
        i = end
        if kind == b"IEND":
            break
    if i != len(data):
        raise ValueError("bytes after IEND")
    return out


def png_problems(rel: str, data: bytes) -> list[str]:
    """Findings for one file that is a PNG (or is named .png). An empty list when it passes."""
    try:
        chunks = png_chunks(data)
    except ValueError as ex:
        return [f"[PNG] cannot be read to its end, so its chunks cannot be checked: {ex}"]
    digest = hashlib.sha256(data).hexdigest()
    problems = []
    for kind, _length in chunks:
        if kind == b"eXIf":
            if rel not in PNG_EXIF_ALLOWED:
                problems.append("[PNG chunk] eXIf: Exif metadata, strip it")
            elif PNG_EXIF_ALLOWED[rel] != digest:
                problems.append("[PNG chunk] eXIf in an allowlisted file whose contents changed: "
                                "review it again, then update PNG_EXIF_ALLOWED")
        elif kind in PNG_FORBIDDEN:
            problems.append(f"[PNG chunk] {kind.decode('latin-1')}: {PNG_FORBIDDEN[kind]}")
    return problems


def png_summary(data: bytes) -> str:
    """The chunk types of a PNG in file order, runs collapsed (IDAT x20). Types only."""
    try:
        chunks = png_chunks(data)
    except ValueError:
        return "unreadable (see the hit above)"
    parts: list[list] = []
    for kind, _length in chunks:
        name = kind.decode("latin-1")
        if parts and parts[-1][0] == name:
            parts[-1][1] += 1
        else:
            parts.append([name, 1])
    return ", ".join(name if count == 1 else f"{name} x{count}" for name, count in parts)


class Auditor:
    def __init__(self, private_dir: Path):
        deny = _read_terms(private_dir / "denylist.txt")
        allow = _read_terms(private_dir / "allowlist.txt")
        if deny is None:
            print(f"warning: {private_dir / 'denylist.txt'} not found: denylist check skipped", file=sys.stderr)
        if allow is None:
            print(f"warning: {private_dir / 'allowlist.txt'} not found: only PUBLIC_EXEMPT applies", file=sys.stderr)
        self.deny = sorted({t.lower() for t in (deny or [])}, key=len, reverse=True)
        exempt = list(allow or []) + list(PUBLIC_EXEMPT)
        self.allow_rx = (re.compile("|".join(re.escape(a) for a in sorted(exempt, key=len, reverse=True)),
                                    re.IGNORECASE) if exempt else None)

    def check(self, text: str, deny: bool = True) -> list[tuple[str, str]]:
        """(category, matched text) pairs found in one line or name. deny=False skips the private denylist only."""
        masked = _mask_allow(text, self.allow_rx)
        found = []
        for name, rx in SECRET_RX:
            for m in rx.finditer(masked):
                found.append((name, m.group(0)))
        for m in EMAIL_RX.finditer(masked):
            addr = m.group(0)
            local, _, domain = addr.lower().partition("@")
            if local == "noreply" or domain in EMAIL_OK_DOMAINS or domain.endswith(RESERVED_EMAIL_SUFFIXES):
                continue
            found.append(("e-mail address", addr))
        for m in HOME_RX.finditer(masked):
            found.append(("home-folder path", m.group(0)))
        for iban in _iban_hits(masked):
            found.append(("IBAN-shaped number", iban))
        low = masked.lower()
        for term in (self.deny if deny else ()):
            if term in low:
                found.append((f"denylist term {term!r}", term))
        return found


def audit(root: Path, private_dir: Path) -> int:
    aud = Auditor(private_dir)
    hits: list[str] = []
    binaries: list[tuple[str, int, str]] = []
    files = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            files += 1
            for cat, term in aud.check(rel):
                hits.append(f"{rel}:0  [file name] {cat}: {term}")
            if os.path.islink(full):
                hits.append(f"{rel}:0  [symlink] symbolic links are not exported; remove or replace it")
                continue
            data = full.read_bytes()
            is_png = data.startswith(PNG_SIGNATURE) or full.suffix.lower() == ".png"
            is_binary = is_png or full.suffix.lower() in BINARY_EXT or b"\x00" in data[:8192]
            if is_binary:
                n = 0
                for run in PRINTABLE_RUN.finditer(_binary_text(data, full.suffix.lower())):
                    text = run.group(0).decode("ascii")
                    for cat, term in aud.check(text):
                        n += 1
                        hits.append(f"{rel}  [binary strings] {cat}: {term}")
                note = ""
                if is_png:
                    for msg in png_problems(rel, data):
                        hits.append(f"{rel}  {msg}")
                    note = png_summary(data)
                binaries.append((rel, n, note))
                continue
            text = data.decode("utf-8", errors="replace")
            verbatim = VERBATIM_LICENCE_TEXTS.get(rel) == hashlib.sha256(data).hexdigest()
            for lineno, line in enumerate(text.splitlines(), 1):
                for cat, term in aud.check(line, deny=not verbatim):
                    excerpt = line.strip()[:120]
                    hits.append(f"{rel}:{lineno}  {cat}: {term}   | {excerpt}")
    for h in hits:
        print(h)
    if binaries:
        print(f"\nbinary files, listed separately ({len(binaries)}; each scanned by its printable strings):")
        for rel, n, note in binaries:
            print(f"  {rel}  hits={n}" + (f"  chunks: {note}" if note else ""))
    print(f"\nscanned {files} files under {root}: {len(hits)} hit(s)")
    return 1 if hits else 0


def main(argv: list[str]) -> int:
    args = list(argv)
    private = Path(__file__).resolve().parent.parent / ".oss-private"
    if "--private-dir" in args:
        i = args.index("--private-dir")
        if i + 1 >= len(args):
            print("--private-dir needs a folder", file=sys.stderr)
            return 2
        private = Path(args[i + 1]).expanduser()
        del args[i:i + 2]
    if len(args) != 1:
        print("usage: oss_audit.py <dir> [--private-dir DIR]", file=sys.stderr)
        return 2
    root = Path(args[0]).expanduser().resolve()
    if not root.is_dir():
        print(f"not a folder: {root}", file=sys.stderr)
        return 2
    return audit(root, private)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
