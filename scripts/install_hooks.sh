#!/bin/sh
# Installs the project's git hooks (scripts/hooks/*) into this clone's hooks folder as symlinks, so a later change to a hook
# in the repository is picked up without reinstalling. Works in a worktree too (git rev-parse --git-path hooks).
#   scripts/install_hooks.sh
# A hook that is already installed is left alone. A different file of the same name is never overwritten or removed: it is
# reported, with the command to move it aside yourself. Nothing is deleted. Run it again after moving the clone.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
cd "$here"
hooks_dir="$(git rev-parse --git-path hooks)"
case "$hooks_dir" in /*) ;; *) hooks_dir="$here/$hooks_dir" ;; esac
mkdir -p "$hooks_dir"
status=0
for src in "$here"/scripts/hooks/*; do
  [ -f "$src" ] || continue
  name="$(basename "$src")"
  dst="$hooks_dir/$name"
  chmod +x "$src"
  if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
    echo "hooks: $name already installed ($dst -> $src)"
  elif [ -e "$dst" ] || [ -L "$dst" ]; then
    echo "hooks: $name NOT installed: $dst already exists and is not this project's hook." >&2
    echo "       Move it aside yourself, then run scripts/install_hooks.sh again:  mv '$dst' '$dst.local'" >&2
    status=1
  else
    ln -s "$src" "$dst"
    echo "hooks: $name installed ($dst -> $src)"
  fi
done
echo "hooks: bypass once with MSE_SKIP_HOOKS=1 git push ...  (the hook prints that it was bypassed)"
exit "$status"
