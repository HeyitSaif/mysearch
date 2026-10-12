#!/bin/sh
# Golden snapshots of the main views, rendered from a fictional fixture: no real folder, volume, collection, recent or
# service is ever read. Off-screen only (the app runs as a prohibited process, see app/Snapshot.swift).
#   scripts/snapshot_check.sh                  build the app into a new scratch folder, render every state, compare with tests/golden
#   scripts/snapshot_check.sh <state> ...      the same for the named states only (a state name from the list below)
#   scripts/snapshot_check.sh --update <state> ...   replace tests/golden/<state>.png for the named states (look at them first);
#                                              with no state name, every state is replaced. compare_snapshots.py drops the
#                                              metadata chunks (iCCP, eXIf, text, tIME) on the way
#   scripts/snapshot_check.sh --dark <state> ...    render the named states in dark mode into the scratch folder only; nothing is
#                                              compared and no golden is touched (look at the renders before you keep anything)
#   scripts/snapshot_check.sh --size WxH [--dark] [<state> ...]   render at another size (WxH = digits x digits, for example
#                                              900x760) into the scratch folder only: light into <scratch>/out, with --dark into
#                                              <scratch>/dark. Nothing is compared and no golden is touched, and --size cannot
#                                              be combined with --update (the goldens are 1280x760). Any --size, 1280x760 too,
#                                              is a scratch render. Without --size the size is 1280x760, as before
# Three stub services answer the states (the third, started with --disk, answers the whole-disk states too): the first serves the plain states (no documents, no saved searches, the document
# routes empty; the Ask answers and the folder tree are fixed), the second (--docs) serves the fictional document results,
# facets, timeline and saved searches used by the document states. Each run works in a new folder from mktemp (printed at
# the end) and never deletes anything.
# The scratch build has its own bundle identifier, so its preferences never mix with an installed MySearch.
# Needs swiftc and the project's .venv (Pillow).
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
update=""
dark=""
only=""
size="1280x760"
size_given=""
usage() { echo "usage: scripts/snapshot_check.sh [--update | --dark] [--size WxH] [<state> ...]" >&2; exit 2; }
bad_size() { echo "bad --size: use WxH, digits x digits (for example --size 900x760)" >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --update) update=1 ;;
    --dark) dark=1 ;;
    --size)
      [ $# -ge 2 ] || bad_size
      size="$2"; size_given=1
      shift ;;
    --*) usage ;;
    *) only="$only $1" ;;
  esac
  shift
done
if [ -n "$size_given" ]; then
  case "$size" in   # digits, one x, digits: nothing else
    *[!0-9x]*|""|x*|*x|*x*x*) bad_size ;;
    *x*) ;;
    *) bad_size ;;
  esac
fi
[ -z "$update" ] || [ -z "$dark" ] || usage
[ -z "$update" ] || [ -z "$size_given" ] || {
  echo "--size cannot be combined with --update: the goldens are 1280x760, and --size renders into the scratch folder only" >&2
  exit 2
}

# the plain states use the plain stub; the document states (they read search results, facets, the timeline or saved
# searches) use the stub with --docs. The first six plain and the first three document states are the original nine;
# search-mac-filters is a whole-Mac search (file names only) and the ask-* states use the Ask answers that both stubs
# serve: GET /api/tree for the folder picker and POST /api/ask for a finished answer.
default_states="columns-deep columns-file-then-folder list-documents icons-documents search-mac ask search-mac-filters ask-empty ask-done ask-picker"
docs_states="search-indexed-docs saved-sidebar timeline search-indexed-filters search-preview search-indexed-chips search-indexed-more search-empty groups-sidebar"
# the whole-disk states use the stub with --disk (the wholedisk settings, /api/disk/status, /api/disk/search, and every route of
# the plain stub too: one render has one port). Their settings values are injected, but the pane's own status poll is answered.
disk_states="quick-search-disk search-mac-disk settings-wholedisk settings-folders-deep"
known=" $default_states $docs_states $disk_states "
for s in $only; do
  case "$known" in *" $s "*) ;; *) echo "unknown state: $s (known: $default_states $docs_states $disk_states)" >&2; exit 2 ;; esac
done
pick() {  # pick <list>: the states of the list that were asked for (all of them when none were named)
  if [ -z "$only" ]; then echo "$1"; return; fi
  out=""
  for s in $1; do case " $only " in *" $s "*) out="$out $s" ;; esac; done
  echo "$out"
}
selected_default="$(pick "$default_states")"
selected_docs="$(pick "$docs_states")"
selected_disk="$(pick "$disk_states")"
states="$selected_default $selected_docs $selected_disk"
[ -n "$(echo $states)" ] || { echo "no states selected" >&2; exit 2; }

py="$here/.venv/bin/python"
[ -x "$py" ] || { echo "no $py: run uv sync --frozen first" >&2; exit 2; }

scratch="$(mktemp -d "${TMPDIR:-/tmp}/mse-snapshot.XXXXXX")"
mkdir "$scratch/out" "$scratch/tmp" "$scratch/dark"
echo "scratch: $scratch"

# 1. the app, built into the scratch folder, with its own bundle identifier
MSE_APP_DIR="$scratch/app" "$here/scripts/build_app.sh" >/dev/null
app="$scratch/app/MySearch.app"
plutil -replace CFBundleIdentifier -string com.mysearch.snapshot-fixture "$app/Contents/Info.plist"
codesign --force --sign - "$app" >/dev/null 2>&1

# 2. the fictional tree, two stub services on free ports, and a token that only this run knows
fixture="$scratch/fixture"
"$here/scripts/fixtures/make_snapshot_tree.sh" "$fixture" >/dev/null
token="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
python3 "$here/scripts/fixtures/stub_service.py" --home "$fixture/home" --token "$token" \
  >"$scratch/stub.out" 2>"$scratch/stub.log" &
stub_pid=$!
python3 "$here/scripts/fixtures/stub_service.py" --home "$fixture/home" --token "$token" --docs \
  >"$scratch/stub-docs.out" 2>"$scratch/stub-docs.log" &
stub_docs_pid=$!
python3 "$here/scripts/fixtures/stub_service.py" --home "$fixture/home" --token "$token" --disk \
  >"$scratch/stub-disk.out" 2>"$scratch/stub-disk.log" &
stub_disk_pid=$!
trap 'kill "$stub_pid" "$stub_docs_pid" "$stub_disk_pid" 2>/dev/null || true' EXIT
port=""; port_docs=""; port_disk=""
for _ in $(seq 1 100); do
  port="$(sed -n 's/^PORT=//p' "$scratch/stub.out" | head -n 1)"
  port_docs="$(sed -n 's/^PORT=//p' "$scratch/stub-docs.out" | head -n 1)"
  port_disk="$(sed -n 's/^PORT=//p' "$scratch/stub-disk.out" | head -n 1)"
  [ -n "$port" ] && [ -n "$port_docs" ] && [ -n "$port_disk" ] && break
  sleep 0.1
done
[ -n "$port" ] || { echo "the stub service did not start (see $scratch/stub.log)" >&2; exit 1; }
[ -n "$port_docs" ] || { echo "the document stub did not start (see $scratch/stub-docs.log)" >&2; exit 1; }
[ -n "$port_disk" ] || { echo "the whole-disk stub did not start (see $scratch/stub-disk.log)" >&2; exit 1; }

# 3. render each state, off-screen, at $size (1280x760 unless --size), with the fixture and the stub that answers it
render() {  # render <state> <stub port> <output folder> [--dark]
  s="$1"; p="$2"; dir="$3"; mode="${4:-}"
  env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin \
    CFFIXED_USER_HOME="$fixture/home" TMPDIR="$scratch/tmp/" LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 TZ=UTC \
    MSE_API_BASE="http://127.0.0.1:$p" MSE_API_TOKEN="$token" \
    "$app/Contents/MacOS/MySearch" --snapshot "$dir/$s.png" --size "$size" --state "$s" $mode \
      --fixture "$fixture" >"$dir/$s.stdout" 2>"$dir/$s.stderr"
}
failed_render=""
for s in $selected_default; do
  if [ -n "$dark" ]; then render "$s" "$port" "$scratch/dark" --dark || failed_render="$failed_render $s"
  else render "$s" "$port" "$scratch/out" || failed_render="$failed_render $s"; fi
done
for s in $selected_docs; do
  if [ -n "$dark" ]; then render "$s" "$port_docs" "$scratch/dark" --dark || failed_render="$failed_render $s"
  else render "$s" "$port_docs" "$scratch/out" || failed_render="$failed_render $s"; fi
done
for s in $selected_disk; do
  if [ -n "$dark" ]; then render "$s" "$port_disk" "$scratch/dark" --dark || failed_render="$failed_render $s"
  else render "$s" "$port_disk" "$scratch/out" || failed_render="$failed_render $s"; fi
done
if [ -n "$failed_render" ]; then
  echo "render failed:$failed_render (see $scratch/out/*.stderr and $scratch/dark/*.stderr)" >&2
  exit 1
fi

# 4. the stubs must have been asked for nothing they do not know
for log in "$scratch/stub.log" "$scratch/stub-docs.log" "$scratch/stub-disk.log"; do
  if [ -s "$log" ]; then
    echo "the app asked a stub for routes it does not answer ($(basename "$log")):" >&2
    sort "$log" | uniq -c >&2
    exit 1
  fi
done

# 5. dark renders, and renders at a chosen size, are kept in the scratch folder, not compared
at=""
[ -z "$size_given" ] || at=" at $size"
if [ -n "$dark" ]; then
  echo "dark renders$at kept in: $scratch/dark"
  exit 0
fi
if [ -n "$size_given" ]; then
  echo "renders$at kept in: $scratch/out (not compared)"
  exit 0
fi

# 6. compare with the goldens (or replace them)
set +e
if [ -n "$update" ]; then
  "$py" "$here/scripts/fixtures/compare_snapshots.py" "$scratch/out" "$here/tests/golden" --update $states
  status=$?
else
  "$py" "$here/scripts/fixtures/compare_snapshots.py" "$scratch/out" "$here/tests/golden" $states
  status=$?
fi
set -e
echo "renders kept in: $scratch/out"
exit "$status"
