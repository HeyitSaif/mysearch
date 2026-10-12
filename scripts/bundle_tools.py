#!/usr/bin/env python3
"""Build-time helpers for the bundled Python runtime (scripts/bundle_runtime.sh). Standard library only.

    bundle_tools.py machos [--null] <root>       list every Mach-O file under <root> (by magic number, symlinks skipped)
    bundle_tools.py fix <root> [--site <dir>]    make the Mach-O files under <root> relocatable with install_name_tool:
                                                 delete absolute run paths (rpaths) that point outside the bundle, and turn
                                                 placeholder install names (/DLC/<pkg>/...) into @loader_path / @rpath ones
    bundle_tools.py scrub <root> --replace FROM=TO ...  replace build-machine paths in text files (not Mach-O)
    bundle_tools.py audit <app> [--forbid STR]...  relocation audit of a built app (exit 1 on a hard finding)

The audit sorts what it finds into three groups, because a Python bundle carries other people's build leftovers:

  HARD   a reference to THIS machine or THIS build: the build user's home folder, the repository folder, the app's output
         folder, the temp folder of the build, any --forbid string. Also a load command that cannot work on another Mac: a
         dependency that is not a system library or @-relative, or a run path that is an absolute folder. Exit status 1.
  INFO   strings that a third-party wheel or the standalone Python carries from its own builders (a CI runner's home folder,
         a Homebrew prefix in a default config path, a compiler's source paths). They sit in code or debug data and are
         never opened; they do not refer to this machine. Counted per package, not hidden.
"""

from __future__ import annotations

import argparse
import collections
import getpass
import os
import re
import subprocess
import sys

MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
         b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"}
SYSTEM_PREFIXES = ("/usr/lib/", "/System/Library/")
RELATIVE_PREFIXES = ("@rpath/", "@loader_path/", "@executable_path/")
PLACEHOLDER = "/DLC/"          # what delocate leaves as the install name of the libraries it copies into a wheel


def is_macho(path: str) -> bool:
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return False
        with open(path, "rb") as f:
            return f.read(4) in MAGIC
    except OSError:
        return False


def machos(root: str) -> list[str]:
    out = []
    for d, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(d, name)
            if is_macho(p):
                out.append(p)
    return sorted(out)


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def _blocks(args: list[str], path: str) -> list[list[str]]:
    """otool prints a header line ("<path>:" or "<path> (architecture x):") per architecture, then that slice's lines."""
    blocks: list[list[str]] = []
    for line in _run(["otool", *args, path]).splitlines():
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")) and line.rstrip().endswith(":") and (line.startswith(path) or " (architecture " in line):
            blocks.append([])
        elif blocks:
            blocks[-1].append(line.strip())
    return blocks


def install_ids(path: str) -> list[str]:
    """The file's own install name(s) (LC_ID_DYLIB, one per architecture slice); empty for a program or a plug-in without one."""
    out = []
    for b in _blocks(["-D"], path):
        out += b[:1]
    return out


def install_id(path: str) -> str | None:
    ids = install_ids(path)
    return ids[0] if ids else None


def dependencies(path: str) -> list[str]:
    """The libraries the file loads (all architecture slices), without its own install name."""
    own = set(install_ids(path))
    deps: list[str] = []
    for b in _blocks(["-L"], path):
        for l in b:
            dep = l.split(" (")[0]
            if dep not in own and dep not in deps:
                deps.append(dep)
    return deps


def rpaths(path: str) -> list[str]:
    out, lines = [], _run(["otool", "-l", path]).splitlines()
    for i, line in enumerate(lines):
        if "cmd LC_RPATH" in line and i + 2 < len(lines):
            m = re.match(r"\s*path (.+?) \(offset \d+\)", lines[i + 2])
            if m and m.group(1) not in out:
                out.append(m.group(1))
    return out


# ------------------------------------------------------------------------------------------------ fix

def fix(root: str, site: str | None = None) -> list[str]:
    """Edit load commands in place. Returns one line per change. The caller signs the files again afterwards."""
    # Both folders as real paths, once, at the boundary: a placeholder dependency is rewritten as @loader_path plus the
    # relative path from the file to the target, and that path is only short when the file (walked under root) and the
    # target (built under site) are measured in the same form. With root given through a symlink (macOS's $TMPDIR is
    # /var/folders -> /private/var/folders) and site resolved, the relative path climbed to / and back and no longer
    # fitted the load command's padding, so install_name_tool refused it.
    root = os.path.realpath(root)
    site = os.path.realpath(site or root)
    changes = []
    for p in machos(root):
        rel = os.path.relpath(p, root)
        for rp in rpaths(p):
            if not rp.startswith("@"):
                # an absolute run path names a folder of the machine that built the wheel; dyld skips a missing one, and on
                # another Mac it is missing, so removing it changes nothing but the reference
                r = subprocess.run(["install_name_tool", "-delete_rpath", rp, p], capture_output=True, text=True)
                changes.append(f"{rel}: delete rpath {rp}" if r.returncode == 0 else f"{rel}: FAILED delete rpath {rp}: {r.stderr.strip()}")
        own = install_id(p)
        if own and own.startswith(PLACEHOLDER):
            r = subprocess.run(["install_name_tool", "-id", "@rpath/" + os.path.basename(own), p], capture_output=True, text=True)
            changes.append(f"{rel}: id {own} -> @rpath/{os.path.basename(own)}" if r.returncode == 0
                           else f"{rel}: FAILED id {own}: {r.stderr.strip()}")
        for dep in dependencies(p):
            if dep.startswith(PLACEHOLDER):
                target = os.path.join(site, dep[len(PLACEHOLDER):])
                if os.path.exists(target):
                    new = "@loader_path/" + os.path.relpath(target, os.path.dirname(p))
                    r = subprocess.run(["install_name_tool", "-change", dep, new, p], capture_output=True, text=True)
                    changes.append(f"{rel}: {dep} -> {new}" if r.returncode == 0 else f"{rel}: FAILED {dep}: {r.stderr.strip()}")
    return changes


# ------------------------------------------------------------------------------------------------ scrub

def scrub(root: str, replacements: list[tuple[str, str]]) -> list[str]:
    """Replace build-machine paths in text files under root (never in Mach-O files, which are not edited this way).
    uv writes the folder it installed Python into into the standard library's _sysconfigdata_*.py; nothing reads it
    at run time, but the bundle should not carry it. Returns one line per file changed or per Mach-O file left alone."""
    out = []
    for d, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(d, name)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            try:
                with open(p, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            hit = [(a, b) for a, b in replacements if a.encode() in data]
            if not hit:
                continue
            rel = os.path.relpath(p, root)
            if data[:4] in MAGIC:
                out.append(f"{rel}: contains a build path but is a Mach-O file; left alone")
                continue
            for a, b in hit:
                data = data.replace(a.encode(), b.encode())
            with open(p, "wb") as f:
                f.write(data)
            out.append(f"{rel}: build paths replaced")
    return out


# ------------------------------------------------------------------------------------------------ audit

def _forbidden(extra: list[str]) -> list[str]:
    """Strings that identify this machine or this build. Built from the environment of the run: nothing is written down."""
    out: list[str] = []
    home = os.path.realpath(os.path.expanduser("~"))
    user = getpass.getuser()
    for s in (home, os.path.expanduser("~"), f"/Users/{user}", f"/home/{user}"):
        if s and s not in out:
            out.append(s)
    for key in ("TMPDIR",):
        v = os.environ.get(key)
        if v and len(v) > 8:
            out.append(os.path.realpath(v).rstrip("/"))
    try:
        t = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True).stdout.strip().rstrip("/")
        if t and len(t) > 8:
            out.append(os.path.realpath(t))
            out.append(t)
    except OSError:
        pass
    out += [e for e in extra if e]
    seen, uniq = set(), []
    for s in out:
        if s not in seen and len(s) > 3:
            seen.add(s)
            uniq.append(s)
    return uniq


# generic leftovers of someone else's build: reported, never a failure
INFO_PATTERNS = {
    "a CI runner or builder home (/Users/...)": re.compile(rb"/Users/[A-Za-z0-9_.\-]+"),
    "a Homebrew prefix (/opt/homebrew...)": re.compile(rb"/opt/homebrew[A-Za-z0-9_./@\-]*"),
    "a build temp folder (/private/tmp, /var/folders)": re.compile(rb"/(?:private/tmp|var/folders)/[A-Za-z0-9_./\-]+"),
}


def _top(rel: str) -> str:
    parts = rel.split(os.sep)
    # lib/python3.13/site-packages/<pkg>/... -> <pkg>; the standalone interpreter's own files -> python
    if "site-packages" in parts:
        i = parts.index("site-packages")
        return parts[i + 1].split("-")[0].split(".")[0] if i + 1 < len(parts) else "site-packages"
    return "python (standard library, interpreter)" if parts and parts[0] == "runtime" else parts[0]


def audit(app: str, forbid: list[str]) -> int:
    app = os.path.realpath(app)
    res = os.path.join(app, "Contents", "Resources")
    scan_root = res if os.path.isdir(res) else app
    runtime = os.path.join(res, "runtime")
    hard: list[str] = []
    info: dict[str, collections.Counter] = {k: collections.Counter() for k in INFO_PATTERNS}
    forbid_all = _forbidden(forbid)
    forbid_b = [(s, s.encode()) for s in forbid_all]

    # 1. load commands of every Mach-O in the app
    files = machos(app)
    bad_deps = bad_rpaths = bad_ids = 0
    unresolvable: collections.Counter = collections.Counter()
    for p in files:
        rel = os.path.relpath(p, app)
        for dep in dependencies(p):
            if dep.startswith(SYSTEM_PREFIXES + RELATIVE_PREFIXES):
                continue
            if not dep.startswith("/"):
                unresolvable[_top(os.path.relpath(p, res) if p.startswith(res) else rel)] += 1      # see below
            else:
                bad_deps += 1
                hard.append(f"dependency {dep}  in {rel}")
        for rp in rpaths(p):
            if not rp.startswith(("@",) + SYSTEM_PREFIXES):
                bad_rpaths += 1
                hard.append(f"absolute rpath {rp}  in {rel}")
        for own in install_ids(p):
            if own.startswith("/") and not own.startswith(SYSTEM_PREFIXES):
                bad_ids += 1
                hard.append(f"install name {own}  in {rel}")
            elif not own.startswith(("/", "@")):
                unresolvable[_top(os.path.relpath(p, res) if p.startswith(res) else rel)] += 1

    # 2. symlinks that leave the bundle or are absolute
    bad_links = 0
    for d, dirs, names in os.walk(app):
        for n in dirs + names:
            p = os.path.join(d, n)
            if os.path.islink(p):
                t = os.readlink(p)
                if os.path.isabs(t) or not os.path.realpath(p).startswith(app + os.sep):
                    bad_links += 1
                    hard.append(f"symlink {os.path.relpath(p, app)} -> {t}")

    # 3. strings in every file under Resources and MacOS
    nfiles = 0
    for root in (scan_root, os.path.join(app, "Contents", "MacOS")):
        for d, _dirs, names in os.walk(root):
            for n in names:
                p = os.path.join(d, n)
                if os.path.islink(p):
                    continue
                try:
                    with open(p, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                nfiles += 1
                rel = os.path.relpath(p, app)
                for s, b in forbid_b:
                    if b in data:
                        hard.append(f"build-machine string {s!r}  in {rel}")
                for label, rx in INFO_PATTERNS.items():
                    for m in rx.finditer(data):
                        info[label][_top(os.path.relpath(p, res) if p.startswith(res) else rel)] += 1

    print(f"relocation audit of {os.path.basename(app)}")
    print(f"  Mach-O files checked: {len(files)}; other files scanned for strings: {nfiles}")
    print(f"  load commands: {bad_deps} bad dependencies, {bad_rpaths} absolute rpaths, {bad_ids} absolute install names")
    print(f"  symlinks that are absolute or leave the bundle: {bad_links}")
    print(f"  strings that identify this machine or this build ({len(forbid_all)} patterns, built from the environment): "
          f"{sum(1 for h in hard if h.startswith('build-machine'))}")
    if unresolvable:
        # an install name or dependency that is neither absolute nor @-relative (a build-tree path such as bazel-out/...):
        # it names nothing on any Mac, so it cannot point at this one; dyld only follows such a name when it is needed
        print(f"  info: install names that are build-tree placeholders, not paths: {sum(unresolvable.values())} in "
              f"{len(unresolvable)} packages ({', '.join(f'{k} {v}' for k, v in unresolvable.most_common(6))})")
    for label, counter in info.items():
        if counter:
            total = sum(counter.values())
            top = ", ".join(f"{k} {v}" for k, v in counter.most_common(6))
            print(f"  info: {label}: {total} in {len(counter)} packages ({top})")
    if hard:
        print(f"\nHARD findings ({len(hard)}); first 40:")
        for h in hard[:40]:
            print("  " + h)
        return 1
    print("  result: OK (no hard finding)")
    return 0


# ------------------------------------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("machos")
    a.add_argument("root")
    a.add_argument("--null", action="store_true", help="separate with NUL, for xargs -0")
    f = sub.add_parser("fix")
    f.add_argument("root")
    f.add_argument("--site", help="the folder /DLC/ stands for (the site-packages folder); default: root")
    c = sub.add_parser("scrub")
    c.add_argument("root")
    c.add_argument("--replace", action="append", default=[], metavar="FROM=TO", help="repeatable")
    u = sub.add_parser("audit")
    u.add_argument("app")
    u.add_argument("--forbid", action="append", default=[], help="a string that must not appear anywhere (repeatable)")
    args = ap.parse_args(argv)
    if args.cmd == "machos":
        found = machos(args.root)
        sep = "\0" if args.null else "\n"
        sys.stdout.write("".join(p + sep for p in found))
        return 0
    if args.cmd == "fix":
        for line in fix(args.root, args.site):
            print(line)
        return 0
    if args.cmd == "scrub":
        for line in scrub(args.root, [tuple(r.split("=", 1)) for r in args.replace]):
            print(line)
        return 0
    return audit(args.app, args.forbid)


if __name__ == "__main__":
    sys.exit(main())
