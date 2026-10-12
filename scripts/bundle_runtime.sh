#!/bin/sh
# Puts a relocatable Python runtime, the locked dependencies and the mse package inside MySearch.app, so the app runs the
# search service by itself: no clone, no uv, no virtual environment on the user's Mac.
#
#   scripts/bundle_runtime.sh install <MySearch.app>     fill <app>/Contents/Resources/{runtime,mse}
#   scripts/bundle_runtime.sh sign    <MySearch.app> [--identity <name|->] [--hardened] [--entitlements <file>]
#                                                        sign every Mach-O file of the runtime (before the app is signed)
#   scripts/bundle_runtime.sh check   <MySearch.app>     relocation audit (otool, rpaths, strings); exit 1 on a hard finding
#
# Layout it makes (Contents/Resources/):
#   runtime/bin/python3          python-build-standalone CPython (statically linked; finds its library by its own location)
#   runtime/bin/mse              wrapper: python3 -m mse "$@"  (the path agents_setup writes into MCP configs)
#   runtime/lib/python3.X/       the standard library, and in site-packages/ the packages locked in uv.lock
#   runtime/RUNTIME.json         what it was built from (Python version, sha256 of uv.lock, date); no paths, no user names
#   mse/                         the package, minus the files scripts/oss-exclude.txt keeps private
#   LICENSE, THIRD_PARTY_NOTICES.md, Licenses/   the project licence, the notices and the GPL/LGPL texts (licenses/); always
#                                refreshed from the repository, even when the runtime is already in place
# A .pth file in site-packages puts Contents/Resources on the path by a relative entry, so `import mse` works for any
# invocation of the runtime's Python, wherever the app lives. Byte code is compiled here (unchecked hash, file names that
# say /Applications/MySearch.app/...), so the signed bundle is never written to.
#
# Network use, build time only: uv downloads a standalone CPython (about 24 MB) and the locked wheels from PyPI, and the
# one git dependency (mlx-vlm) from GitHub. Both are cached under MSE_RUNTIME_CACHE (default build/runtime-cache, git-ignored).
#   MSE_RUNTIME_PYTHON   the Python to bundle (default 3.13; uv.lock allows 3.12 and 3.13)
#   MSE_RUNTIME_CACHE    where uv keeps the downloaded Python and the environment it syncs
# It never deletes. An existing runtime with the same uv.lock is left as it is (only mse/ is refreshed); one built from a
# different lock is refused: build into a new folder.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
tools="$here/scripts/bundle_tools.py"

say()  { printf '    %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,12p' "$0" >&2; exit 2; }
[ $# -ge 2 ] || usage
cmd="$1"; app="$2"; shift 2
[ -d "$app/Contents" ] || die "$app is not an app bundle"
app="$(cd "$app" && pwd -P)"
res="$app/Contents/Resources"
rt="$res/runtime"

install_runtime() {
  [ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || die "the runtime is built on Apple Silicon (the models are MLX models)"
  command -v uv >/dev/null 2>&1 || die "uv is not installed (brew install uv). It is needed to build the runtime, not to run the app."
  [ -f "$here/uv.lock" ] || die "uv.lock not found in $here"
  command -v rsync >/dev/null 2>&1 || die "rsync not found"
  command -v python3 >/dev/null 2>&1 || die "python3 not found (build-time helper scripts)"
  mkdir -p "$res"

  lock_sha="$(shasum -a 256 "$here/uv.lock" | cut -d' ' -f1)"
  want="${MSE_RUNTIME_PYTHON:-3.13}"
  cache="${MSE_RUNTIME_CACHE:-$here/build/runtime-cache}"
  mkdir -p "$cache"
  cache="$(cd "$cache" && pwd -P)"
  pyver=""

  if [ -e "$rt" ]; then
    grep -q "$lock_sha" "$rt/RUNTIME.json" 2>/dev/null \
      || die "$rt exists but was not built from this uv.lock. Build into a new folder; nothing is replaced."
    say "runtime already in place (same uv.lock); leaving it as it is"
    pyver="$(ls "$rt/lib" | sed -n 's/^python\(3\.[0-9]*\)$/\1/p' | head -n 1)"
  else
    echo "==> standalone Python $want (uv, cached in $cache/python)"
    export UV_PYTHON_INSTALL_DIR="$cache/python"
    uv python install "$want" 2>&1 | sed 's/^/    /'
    py="$(uv python find --managed-python "$want")"
    [ -x "$py" ] || die "uv did not report a managed Python $want"
    pbs="$(cd "$(dirname "$py")/.." && pwd -P)"
    pyver="$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    pyfull="$("$py" -c 'import platform; print(platform.python_version())')"
    say "Python $pyfull at $(basename "$pbs")"

    echo "==> locked dependencies (uv sync --frozen, from uv.lock)"
    ( cd "$here" && UV_PROJECT_ENVIRONMENT="$cache/venv" uv sync --frozen --no-dev --no-install-project --python "$py" 2>&1 | sed 's/^/    /' | tail -n 5 )
    site_src="$cache/venv/lib/python$pyver/site-packages"
    [ -d "$site_src" ] || die "uv sync did not make $site_src"

    echo "==> copying the runtime into the app"
    mkdir -p "$rt"
    # the interpreter and the standard library; left out: headers, man pages, Tcl/Tk and IDLE, the test suite, pip, and
    # libpython (the interpreter is statically linked; libpython3.X.dylib is only for programs that embed Python)
    rsync -a \
      --exclude '/include/' --exclude '/share/' --exclude '/BUILD' \
      --exclude '/bin/idle3*' --exclude '/bin/pip*' --exclude '/bin/pydoc3*' --exclude '/bin/*-config' \
      --exclude '/lib/*.dylib' --exclude '/lib/itcl*' --exclude '/lib/tcl*' --exclude '/lib/tk*' --exclude '/lib/thread*' \
      --exclude '/lib/pkgconfig/' \
      --exclude "/lib/python$pyver/test/" --exclude "/lib/python$pyver/idlelib/" --exclude "/lib/python$pyver/tkinter/" \
      --exclude "/lib/python$pyver/turtledemo/" --exclude "/lib/python$pyver/ensurepip/" \
      --exclude "/lib/python$pyver/config-*/" --exclude "/lib/python$pyver/lib-dynload/_tkinter*" \
      --exclude "/lib/python$pyver/site-packages/*" --exclude '__pycache__/' \
      "$pbs/" "$rt/"
    mkdir -p "$rt/lib/python$pyver/site-packages"
    rsync -a --exclude '__pycache__/' --exclude '_virtualenv.*' --exclude '_editable_impl_*' --exclude '*.pyc' \
      "$site_src/" "$rt/lib/python$pyver/site-packages/"
    # `import mse` for any invocation of this Python: a relative .pth entry, resolved against site-packages (-> Resources)
    printf '%s\n' '../../../..' > "$rt/lib/python$pyver/site-packages/mysearch_app.pth"
    # the command line the app and agents use; resolves its own symlinks, so a link in /usr/local/bin works too
    cat > "$rt/bin/mse" <<'WRAP'
#!/bin/sh
# MySearch command line, run by the Python inside MySearch.app. Works from wherever the app is installed.
self="$0"
while [ -L "$self" ]; do
  t="$(readlink "$self")"
  case "$t" in /*) self="$t" ;; *) self="$(dirname "$self")/$t" ;; esac
done
here="$(cd "$(dirname "$self")" && pwd -P)"
export PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
exec "$here/python3" -m mse "$@"
WRAP
    chmod 755 "$rt/bin/mse"

    echo "==> making the Mach-O files relocatable (install_name_tool)"
    fixlog="$(python3 "$tools" fix "$rt" --site "$rt/lib/python$pyver/site-packages")"
    printf '%s\n' "$fixlog" | sed 's/^/    /' | tail -n 12
    case "$fixlog" in *FAILED*) die "install_name_tool failed on a file (see above)" ;; esac

    # uv writes the folder it installed Python into (here: the cache) into the standard library's sysconfig data; the bundle
    # should name its own place instead
    scrublog="$(python3 "$tools" scrub "$rt" --replace "$pbs=/Applications/MySearch.app/Contents/Resources/runtime" \
      --replace "$(dirname "$pbs")/cpython-$pyver-macos-aarch64-none=/Applications/MySearch.app/Contents/Resources/runtime")"
    [ -z "$scrublog" ] || printf '%s\n' "$scrublog" | sed 's/^/    /'

    cat > "$rt/RUNTIME.json" <<EOF
{
  "python": "$pyfull",
  "python_build": "python-build-standalone via uv $(uv --version | cut -d' ' -f2)",
  "uv_lock_sha256": "$lock_sha",
  "mse": "$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$here/mse/__init__.py")",
  "built": "$(date -u +%Y-%m-%d)"
}
EOF
  fi
  [ -n "$pyver" ] || die "cannot tell the Python version of $rt"

  echo "==> the mse package"
  set --
  if [ -f "$here/scripts/oss-exclude.txt" ]; then
    # files under mse/ that the public tree leaves out (scripts/oss-exclude.txt) are not shipped in the app either
    while IFS= read -r line; do
      case "$line" in
        mse/*) set -- "$@" --exclude "/${line#mse/}" ;;
      esac
    done < "$here/scripts/oss-exclude.txt"
  fi
  mkdir -p "$res/mse"
  rsync -a --exclude '__pycache__/' --exclude '*.pyc' "$@" "$here/mse/" "$res/mse/"
  # Licence files. Always copied over what is there (a rebuild into an existing folder must not keep stale notices), and a
  # missing source is an error: a bundled build without them is not allowed to exist.
  echo "==> licence files"
  [ -f "$here/LICENSE" ] && [ -f "$here/THIRD_PARTY_NOTICES.md" ] || die "LICENSE or THIRD_PARTY_NOTICES.md is missing in $here"
  [ -f "$here/licenses/GPL-3.0.txt" ] || die "$here/licenses is missing or incomplete (see licenses/README.md)"
  cp "$here/LICENSE" "$res/LICENSE"
  cp "$here/THIRD_PARTY_NOTICES.md" "$res/THIRD_PARTY_NOTICES.md"
  mkdir -p "$res/Licenses"
  rsync -a "$here/licenses/" "$res/Licenses/"      # texts of the GPL/LGPL code inside the wheels; no --delete
  say "LICENSE, THIRD_PARTY_NOTICES.md and Licenses/ ($(ls "$here/licenses" | wc -l | tr -d ' ') files)"

  echo "==> byte code (so the signed bundle is never written to)"
  # file names inside the byte code say where the app is meant to live, not where it was built
  clog="$(mktemp "${TMPDIR:-/tmp}/mse-compile.XXXXXX")"     # outside the bundle; left where it is
  # -B: this Python writes no byte code of its own (with -I it would ignore PYTHONDONTWRITEBYTECODE and leave files that
  # carry the build path); -f rewrites any that exist
  "$rt/bin/python3" -I -B -m compileall -q -f -j 0 --invalidation-mode unchecked-hash \
    -s "$res" -p "/Applications/MySearch.app/Contents/Resources" "$rt/lib/python$pyver" "$res/mse" \
    > "$clog" 2>&1 || say "some files did not compile (test data or vendored code that is never imported); log: $clog"

  xattr -cr "$res" 2>/dev/null || true
  du -sh "$rt" "$res/mse" | sed 's/^/    /'
}

sign_runtime() {
  identity="-"; hardened=""; ents=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --identity) shift; identity="$1" ;;
      --hardened) hardened=1 ;;
      --entitlements) shift; ents="$1" ;;
      *) die "unknown option $1" ;;
    esac
    shift
  done
  [ -d "$rt" ] || { say "no bundled runtime in $app; nothing to sign"; return 0; }
  if [ -n "$hardened" ]; then flags="--options runtime --timestamp"; else flags="--timestamp=none"; fi
  n="$(python3 "$tools" machos "$rt" | wc -l | tr -d ' ')"
  say "signing $n Mach-O files of the runtime (identity: $identity${hardened:+, hardened runtime, secure timestamp})"
  # shellcheck disable=SC2086
  siglog="$(mktemp "${TMPDIR:-/tmp}/mse-codesign.XXXXXX")"
  python3 "$tools" machos --null "$rt" | xargs -0 -P 8 -n 16 codesign --force --sign "$identity" $flags >/dev/null 2>"$siglog" \
    || { cat "$siglog" >&2; die "codesign failed on a Mach-O file of the runtime"; }
  # the interpreter is the one executable of the runtime: it gets its own identifier and, when asked, the entitlements
  py="$(ls "$rt"/bin/python3.[0-9]* | head -n 1)"
  # shellcheck disable=SC2086
  codesign --force --sign "$identity" $flags --identifier com.mysearch.app.python ${ents:+--entitlements "$ents"} "$py" >/dev/null 2>"$siglog" \
    || { cat "$siglog" >&2; die "codesign failed on the interpreter (a bad entitlements file is rejected here)"; }
  codesign --verify --strict "$py"
  python3 "$tools" machos --null "$rt" | xargs -0 -n 1 -P 8 codesign --verify 2>/dev/null \
    || die "a Mach-O file of the runtime failed codesign --verify"
  say "signature check passed"
}

check_runtime() {
  [ -d "$rt" ] || die "$app has no bundled runtime"
  # the licence files: present and identical to the repository's
  for f in LICENSE THIRD_PARTY_NOTICES.md; do
    cmp -s "$here/$f" "$res/$f" || die "$res/$f is missing or differs from $f in the repository"
  done
  for f in "$here"/licenses/*; do
    [ -f "$f" ] || continue
    cmp -s "$f" "$res/Licenses/$(basename "$f")" || die "$res/Licenses/$(basename "$f") is missing or differs from licenses/$(basename "$f")"
  done
  say "licence files: LICENSE, THIRD_PARTY_NOTICES.md and Licenses/ match the repository"
  cache="${MSE_RUNTIME_CACHE:-$here/build/runtime-cache}"
  python3 "$tools" audit "$app" --forbid "$here" --forbid "$app" --forbid "$(dirname "$app")" --forbid "$cache"
}

case "$cmd" in
  install) install_runtime ;;
  sign)    sign_runtime "$@" ;;
  check)   check_runtime ;;
  *) usage ;;
esac
