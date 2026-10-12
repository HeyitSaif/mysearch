#!/bin/sh
# Builds the public tree, then audits it with scripts/oss_audit.py. Three modes:
#   scripts/export_public.sh <target-dir>                      copy every tracked or not-ignored file (working-tree version)
#                                                              minus scripts/oss-exclude.txt, then audit; exit 1 on any hit
#   scripts/export_public.sh --no-exclude <target-dir>         audit-only copy of everything, ignoring oss-exclude.txt.
#                                                              Use it to list residual hits; never publish this copy.
#   scripts/export_public.sh --sync [--dry-run] <clean-repo>   export to a NEW staging folder under $MSE_SYNC_STAGE_ROOT (default: the system temp folder),
#                                                              audit it (abort on any hit), then bring the clean repository's
#                                                              working tree up to the stage: added and changed files are copied
#                                                              in. Removed files are never deleted here; the exact git rm
#                                                              commands are printed for the owner. Nothing is committed or
#                                                              pushed, and <clean-repo>/.git is only read. --dry-run prints the
#                                                              plan and copies nothing.
# The export target must be missing or empty and must lie outside this repository. All checks run before anything is
# created. Nothing is overwritten or deleted. This script does not run git init: the public repository is created
# from the copy.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
mode=export
no_exclude=""
dry_run=""
usage() {
  echo "usage: scripts/export_public.sh [--no-exclude] <target-dir>" >&2
  echo "       scripts/export_public.sh --sync [--dry-run] <clean-repo>" >&2
  exit 2
}
while [ $# -gt 0 ]; do
  case "$1" in
    --no-exclude) no_exclude=1 ;;
    --sync) mode=sync ;;
    --dry-run) dry_run=1 ;;
    -*) usage ;;
    *) break ;;
  esac
  shift
done
[ $# -eq 1 ] || usage
if [ "$mode" = sync ] && [ -n "$no_exclude" ]; then echo "--sync cannot be combined with --no-exclude" >&2; exit 2; fi
if [ "$mode" = export ] && [ -n "$dry_run" ]; then echo "--dry-run goes with --sync" >&2; exit 2; fi
exclude_file="$here/scripts/oss-exclude.txt"
[ -n "$no_exclude" ] && exclude_file=""
exec python3 - "$here" "$1" "$exclude_file" "$mode" "$dry_run" <<'PY'
import filecmp
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time

here = os.path.realpath(sys.argv[1])
target = os.path.realpath(sys.argv[2])
exclude_file = sys.argv[3]
mode = sys.argv[4]
dry_run = sys.argv[5] == "1"

STAGE_ROOT = os.environ.get("MSE_SYNC_STAGE_ROOT") or os.path.join(tempfile.gettempdir(), "mysearch-sync-%d" % os.getuid())   # sync mode: every staging folder is created here, as a new folder
SKIP_TOP = (".git", "build")                  # sync mode: top-level names that are never compared or copied
NOREPLY = "@users.noreply.github.com"


def read_excludes(path):
    out = []
    if path:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line)
    return out


def is_excluded(rel, excluded):
    for e in excluded:
        if rel == e or (e.endswith("/") and rel.startswith(e)):
            return True
    return False


def git(repo, *args):
    """Read-only git: optional locks are off, so the index and the refs are never refreshed. Fails loudly."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, env=env, check=True).stdout.decode("utf-8")


def git_or_empty(repo, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    done = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, env=env)
    return done.stdout.decode("utf-8") if done.returncode == 0 else ""


def export_into(dest, excluded):
    listing = git(here, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    names = sorted({n for n in listing.split("\0") if n})
    os.makedirs(dest, exist_ok=True)
    copied = skipped = 0
    for rel in names:
        if is_excluded(rel, excluded):
            skipped += 1
            continue
        src = os.path.join(here, rel)
        if os.path.islink(src):
            print(f"skipped symbolic link: {rel}", file=sys.stderr)
            continue
        if not os.path.isfile(src):
            print(f"skipped (listed by git but missing in the working tree): {rel}", file=sys.stderr)
            continue
        out = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copy2(src, out)          # copies the mode bits too, so executables stay executable
        copied += 1
    return copied, skipped


def run_audit(folder):
    # the private denylist is read from this repository's .oss-private, never from the copy
    audit = os.path.join(here, "scripts", "oss_audit.py")
    return subprocess.run([sys.executable, audit, folder]).returncode


def mode_bits(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def path_has_symlink(root, rel):
    """True when any component of root/rel (below root) is a symbolic link, so nothing may be written through it."""
    cur = root
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return True
    return False


def top_skipped(rel):
    return rel.split("/", 1)[0] in SKIP_TOP


# ---------------------------------------------------------------- export (default and --no-exclude)
if mode == "export":
    if os.path.commonpath([target, here]) == here:
        sys.exit(f"target lies inside the repository: {target}")
    if os.path.lexists(target):
        if not os.path.isdir(target):
            sys.exit(f"target exists and is not a folder: {target}")
        if os.listdir(target):
            sys.exit(f"target is not empty, refusing to write into it: {target}")
    copied, skipped = export_into(target, read_excludes(exclude_file))
    print(f"copied {copied} files to {target}; excluded {skipped} by {exclude_file or 'no exclude list (--no-exclude)'}")
    sys.exit(run_audit(target))

# ---------------------------------------------------------------- sync
if not os.path.isdir(target):
    sys.exit(f"not a folder: {target}")
if not os.path.exists(os.path.join(target, ".git")):
    sys.exit(f"not a git repository (no .git there): {target}")
if os.path.commonpath([target, here]) == here or os.path.commonpath([here, target]) == target:
    sys.exit("the clean repository and the private repository must not contain each other")

excluded = read_excludes(exclude_file)
stamp = time.strftime("%Y%m%d-%H%M%S")
stage = os.path.join(STAGE_ROOT, f"mysearch-sync-{stamp}")
os.makedirs(STAGE_ROOT, exist_ok=True)
if os.path.lexists(stage):
    sys.exit(f"staging folder already exists, refusing to reuse it: {stage}")
os.mkdir(stage)

# 1. export to the new stage, then audit it; a hit aborts before anything is copied into the clean repository
copied, skipped = export_into(stage, excluded)
print(f"staging folder: {stage} ({copied} files, {skipped} excluded by {exclude_file})")
if run_audit(stage) != 0:
    sys.exit(f"audit of the staging folder found hits; nothing was copied. Staging folder kept for inspection: {stage}")
print("audit of the staging folder: 0 hits")


def stage_files():
    found = set()
    for dirpath, _dirnames, filenames in os.walk(stage):
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), stage).replace(os.sep, "/")
            if not top_skipped(rel):
                found.add(rel)
    return found


def clean_listing():
    """Tracked and untracked-but-not-ignored files of the clean repository, as git lists them (never os.walk)."""
    tracked = {n for n in git(target, "ls-files", "-z", "--cached").split("\0") if n and not top_skipped(n)}
    others = {n for n in git(target, "ls-files", "-z", "--others", "--exclude-standard").split("\0") if n and not top_skipped(n)}
    return tracked, others


wanted = stage_files()
tracked, untracked = clean_listing()

added, changed, unsafe = [], [], []
for rel in sorted(wanted):
    src = os.path.join(stage, rel)
    dst = os.path.join(target, rel)
    if path_has_symlink(target, rel):
        unsafe.append(rel)
        continue
    if not os.path.lexists(dst):
        added.append(rel)
    elif not os.path.isfile(dst):
        unsafe.append(rel)              # a folder or a special file where the stage has a file
    elif not filecmp.cmp(src, dst, shallow=False):
        changed.append((rel, "content"))
    elif mode_bits(src) != mode_bits(dst):
        changed.append((rel, "mode"))

removed = sorted(rel for rel in tracked if rel not in wanted)        # tracked in the clean repository, gone from the stage
extra = sorted(rel for rel in untracked if rel not in wanted)        # untracked, not ignored, not in the stage: left alone

if unsafe:
    print("refusing: these paths are symbolic links, or not files, in the clean repository (nothing copied):")
    for rel in unsafe:
        print(f"  {rel}")
    sys.exit(1)

print(f"plan against {target}: {len(wanted)} files in the stage")
print(f"added ({len(added)}):")
for rel in added:
    print(f"  {rel}")
print(f"changed ({len(changed)}):")
for rel, why in changed:
    print(f"  {rel}  [{why}]")
print(f"removed ({len(removed)}): never deleted by this script; run these yourself if you agree:")
for rel in removed:
    print(f"  git -C {shlex.quote(target)} rm -- {shlex.quote(rel)}")
print(f"untracked, not in the stage ({len(extra)}): left as they are:")
for rel in extra:
    print(f"  {rel}")

if dry_run:
    print("dry run: nothing was copied and nothing was verified; the staging folder is kept for inspection")
    sys.exit(0)

# 2. copy the added and changed files in (never a removal)
for rel, _why in [(r, "added") for r in added] + changed:
    src = os.path.join(stage, rel)
    dst = os.path.join(target, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
print(f"copied {len(added) + len(changed)} files into {target}")

# 3. verify: the clean working tree equals the stage, apart from the removals still pending
problems = []
for rel in sorted(wanted):
    src = os.path.join(stage, rel)
    dst = os.path.join(target, rel)
    if path_has_symlink(target, rel) or not os.path.isfile(dst) or os.path.islink(dst):
        problems.append(f"not a plain file after the copy: {rel}")
    elif not filecmp.cmp(src, dst, shallow=False) or mode_bits(src) != mode_bits(dst):
        problems.append(f"differs from the stage after the copy: {rel}")
tracked_after, untracked_after = clean_listing()
listed_after = tracked_after | untracked_after
for rel in sorted(wanted - listed_after):
    problems.append(f"in the stage but not listed by git in the clean repository (ignored there?): {rel}")
pending = set(removed) | set(extra)
for rel in sorted(listed_after - wanted - pending):
    problems.append(f"in the clean repository but neither in the stage nor in the plan: {rel}")
if problems:
    print("verification FAILED:")
    for p in problems:
        print(f"  {p}")
    sys.exit(1)
print(f"verified: the clean working tree equals the stage ({len(wanted)} files); {len(removed)} removal(s) and "
      f"{len(extra)} untracked file(s) are still pending, as listed above")

# 4. the commit line, with the clean repository's own identity; printed only when that identity is the no-reply one
email = git_or_empty(target, "config", "--get", "user.email").strip()
name = git_or_empty(target, "config", "--get", "user.name").strip()
if email.endswith(NOREPLY):
    message = "Sync from the development repository"
    print(f"suggested commit (author {name} <{email}>, from that repository's config; run the git rm lines first, if any):")
    print(f"  git -C {shlex.quote(target)} add -A && git -C {shlex.quote(target)} commit -m {shlex.quote(message)}")
else:
    print(f"no commit line printed: user.email of {target} is not a GitHub no-reply address ({email or 'unset'})")
sys.exit(0)
PY
