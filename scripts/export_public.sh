#!/bin/sh
# Builds the public tree in <target-dir>, then audits it with scripts/oss_audit.py.
#   scripts/export_public.sh <target-dir>                copy every tracked or not-ignored file (working-tree version)
#                                                        minus scripts/oss-exclude.txt, then audit; exit 1 on any hit
#   scripts/export_public.sh --no-exclude <target-dir>   audit-only copy of everything, ignoring oss-exclude.txt.
#                                                        Use it to list residual hits; never publish this copy.
# The target must be missing or empty and must lie outside this repository. All checks run before anything is
# created. Nothing is overwritten or deleted. This script does not run git init: the public repository is created
# from the copy.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
no_exclude=""
if [ "${1:-}" = "--no-exclude" ]; then no_exclude=1; shift; fi
if [ $# -ne 1 ]; then
  echo "usage: scripts/export_public.sh [--no-exclude] <target-dir>" >&2
  exit 2
fi
exclude_file="$here/scripts/oss-exclude.txt"
[ -n "$no_exclude" ] && exclude_file=""
exec python3 - "$here" "$1" "$exclude_file" <<'PY'
import os
import shutil
import subprocess
import sys

here = os.path.realpath(sys.argv[1])
target = os.path.realpath(sys.argv[2])
exclude_file = sys.argv[3]

# 1. checks, before anything is created
if os.path.commonpath([target, here]) == here:
    sys.exit(f"target lies inside the repository: {target}")
if os.path.lexists(target):
    if not os.path.isdir(target):
        sys.exit(f"target exists and is not a folder: {target}")
    if os.listdir(target):
        sys.exit(f"target is not empty, refusing to write into it: {target}")

excluded_paths = []
if exclude_file:
    with open(exclude_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                excluded_paths.append(line)


def is_excluded(rel):
    for e in excluded_paths:
        if rel == e or (e.endswith("/") and rel.startswith(e)):
            return True
    return False


listing = subprocess.run(["git", "-C", here, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, check=True).stdout.decode("utf-8")
names = sorted({n for n in listing.split("\0") if n})

# 2. copy
os.makedirs(target, exist_ok=True)
copied = skipped = 0
for rel in names:
    if is_excluded(rel):
        skipped += 1
        continue
    src = os.path.join(here, rel)
    if os.path.islink(src):
        print(f"skipped symbolic link: {rel}", file=sys.stderr)
        continue
    if not os.path.isfile(src):
        print(f"skipped (listed by git but missing in the working tree): {rel}", file=sys.stderr)
        continue
    dst = os.path.join(target, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)          # copies the mode bits too, so executables stay executable
    copied += 1
print(f"copied {copied} files to {target}; excluded {skipped} by {exclude_file or 'no exclude list (--no-exclude)'}")

# 3. audit the copy (the private denylist is read from the repository's .oss-private, never from the copy)
audit = os.path.join(here, "scripts", "oss_audit.py")
sys.exit(subprocess.run([sys.executable, audit, target]).returncode)
PY
