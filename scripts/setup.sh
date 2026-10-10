#!/bin/bash
# Sets MySearch up on a fresh Apple Silicon Mac in one command. Run it from the clone:  scripts/setup.sh
#   --minimal | --full   the model set: minimal = text search; full adds speech to text and the local Ask model
#   --yes                no questions: default answers (minimal models, no app copy, no folders added)
#   --no-app             do not build or copy MySearch.app (command line and background service only)
# Safe to run again: finished steps are skipped. It never deletes, never uses sudo, and never pipes a download into a shell.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

ASSUME_YES=0
NO_APP=0
TIER=""
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
    --no-app) NO_APP=1 ;;
    --minimal|--full)
      new="${arg#--}"
      if [ -n "$TIER" ] && [ "$TIER" != "$new" ]; then echo "error: choose --minimal or --full, not both" >&2; exit 2; fi
      TIER="$new" ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "error: unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

interactive() { [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; }

# yes_no "question" y|n : true for yes. Without a terminal (or with --yes) the default answer is taken.
yes_no() {
  local question="$1" default="$2" answer=""
  if ! interactive; then
    [ "$default" = "y" ]
    return
  fi
  local hint="y/N"
  [ "$default" = "y" ] && hint="Y/n"
  printf '    %s [%s] ' "$question" "$hint"
  read -r answer || answer=""
  answer="${answer:-$default}"
  case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

say "Checking this Mac"
[ "$(uname -s)" = "Darwin" ] || die "MySearch runs on macOS."
[ "$(uname -m)" = "arm64" ] || die "Apple Silicon (arm64) is required; this Mac reports $(uname -m). The models are MLX models."
macos="$(sw_vers -productVersion)"
[ "${macos%%.*}" -ge 14 ] || die "macOS 14 or later is required; this Mac runs $macos."
xcode-select -p >/dev/null 2>&1 || die "The Xcode Command Line Tools are missing. Run 'xcode-select --install', wait for it to finish, then run this script again."
command -v swiftc >/dev/null 2>&1 || die "swiftc not found. Run 'xcode-select --install' and try again."
command -v git >/dev/null 2>&1 || die "git not found (it comes with the Command Line Tools)."
note "macOS $macos on Apple Silicon, Command Line Tools: ok"
if ! command -v uv >/dev/null 2>&1; then
  cat >&2 <<'EOF'
error: uv is not installed. This script does not install software for you. Install uv yourself, then run it again:
  brew install uv
  or the official instructions: https://docs.astral.sh/uv/getting-started/installation/
EOF
  exit 1
fi
note "uv: $(uv --version)"
if command -v ffmpeg >/dev/null 2>&1; then
  note "ffmpeg: ok (audio and video)"
else
  note "ffmpeg: not installed. It is needed only for audio and video: brew install ffmpeg"
fi

say "Python packages (uv sync --frozen)"
uv sync --frozen

say "Models"
if [ -z "$TIER" ]; then
  if interactive; then
    printf '    minimal = text search.  full = adds speech to text and the local Ask model.\n'
    printf '    Model set? [minimal] '
    read -r TIER || TIER=""
    TIER="$(printf '%s' "${TIER:-minimal}" | tr '[:upper:]' '[:lower:]')"
  else
    TIER="minimal"
  fi
fi
case "$TIER" in minimal|full) ;; *) die "unknown model set: $TIER (use minimal or full)" ;; esac
note "plan for the $TIER set (nothing is changed by the plan):"
uv run mse setup models --dry-run "--$TIER"
uv run mse setup models "--$TIER"

say "Native helper"
HELPER="$HOME/Library/Application Support/MySearch/bin/msehelper"
if [ -x "$HELPER" ]; then
  note "already built: ~/Library/Application Support/MySearch/bin/msehelper (skipped; to rebuild, run scripts/build_helper.sh yourself)"
else
  scripts/build_helper.sh
fi

say "App"
APP_BUILD="$REPO/build/MySearch.app"
if [ "$NO_APP" -eq 1 ]; then
  note "skipped (--no-app)"
elif [ -d "$APP_BUILD" ]; then
  note "already built: build/MySearch.app (skipped; to rebuild, move it to the Trash yourself and run scripts/build_app.sh)"
else
  scripts/build_app.sh
  if codesign --verify --strict "$APP_BUILD" >/dev/null 2>&1; then
    note "signed ad hoc, the signature verifies"
  else
    warn "the app's signature did not verify; see scripts/build_app.sh"
  fi
fi

if [ "$NO_APP" -eq 0 ] && [ -d "$APP_BUILD" ]; then
  if [ -e /Applications/MySearch.app ]; then
    note "/Applications/MySearch.app already exists and is not replaced. To update it, move the old app to the Trash yourself, then copy build/MySearch.app there."
  elif [ ! -w /Applications ]; then
    note "/Applications is not writable by this user. Drag build/MySearch.app into Applications in Finder."
  elif yes_no "Copy build/MySearch.app to /Applications now?" "n"; then
    ditto "$APP_BUILD" /Applications/MySearch.app
    note "copied to /Applications/MySearch.app"
  else
    note "not copied. The background service runs Python directly until the app is in /Applications and you run: uv run mse service install"
  fi
fi

say "Background service (launchd)"
# The app's daemon reads config.json (service.project) before it starts. Writing the defaults and the token now means
# the first start works; a fresh config.json is otherwise written only by the first mse command.
uv run python -c 'from mse import config; config.load()'
service_state="$(uv run mse service status 2>/dev/null || true)"
case "$service_state" in
  installed*) note "already installed: $service_state (skipped). After moving the app, run: uv run mse service install" ;;
  *) uv run mse service install ;;
esac

say "Folders to search"
if interactive; then
  note "Add the folders MySearch should index. An empty line finishes."
  while true; do
    printf '    Folder: '
    read -r folder || folder=""
    [ -n "$folder" ] || break
    folder="${folder/#\~/$HOME}"
    if [ ! -d "$folder" ]; then
      warn "not a folder: $folder"
      continue
    fi
    uv run mse add "$folder" || warn "could not add $folder (see the message above)"
  done
else
  note "skipped (no terminal, or --yes). Add folders with: uv run mse add <folder>"
fi

say "Done"
# the same line `mse doctor` prints, so the note always names the program the installed service really runs
fda_line="$(uv run mse setup check 2>/dev/null | sed -n 's/^full disk *//p' || true)"
cat <<EOF
    Do these yourself, once:
    1. ${fda_line:-Full Disk Access: System Settings > Privacy & Security > Full Disk Access (see uv run mse doctor)}
       Without it, folders macOS protects (Desktop, Documents, Downloads, Mail) are skipped.
    2. Finder extension (optional): System Settings > General > Login Items & Extensions > Finder, switch on MySearch.
    3. Agents (optional): connect a folder to your agents with: uv run mse agents install <folder>
       Raycast (optional): integrations/raycast, see docs/INSTALL.md.

    Check everything with: uv run mse doctor
    Keep this clone where it is: the background service runs from it.
EOF
