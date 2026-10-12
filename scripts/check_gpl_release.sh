#!/bin/sh
# Release gate: fails when a self-contained disk image (built with --bundle-runtime) sits in a release folder without the
# source archive (GPL, LGPL and MPL-2.0) that has to travel with it. Plain images (no bundled runtime) need no archive.
#
#   scripts/check_gpl_release.sh <release-folder> [--plain | --bundled] [--quick]
#
# For every *.dmg in the folder it decides whether the image bundles the runtime:
#   * <image>.dmg.bundled-runtime next to it (build_dmg.sh writes it) means yes, and names the archive that is required;
#   * --bundled   treats every image in the folder as bundled (the archive is MySearch-<version>-sources.tar.gz, with the
#                 version read from mse/__init__.py);
#   * --plain     says every image is a plain build: nothing is required;
#   * with neither, an image without that file is an error (the script cannot tell, and does not guess).
# It then checks the archive that scripts/fetch_gpl_sources.sh makes: it reads back, has the expected top folder and the
# files that matter (README.txt, MANIFEST.tsv, SHA256SUMS, the GPL texts, FFmpeg, x264, x265, Xvid, vid.stab, Rubber Band,
# OpenCV, opencv-python, GCC, the LGPL and MPL-2.0 sources), and every file listed in SHA256SUMS has the recorded sha256
# (--quick skips that last check).
# It reads files and writes nothing. Exit 0 = the release folder is complete; 1 = something is missing or wrong.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"
say() { printf '%s\n' "$*"; }
usage() { sed -n '2,17p' "$0" >&2; exit 2; }

dir=""; mode=""; quick=""
while [ $# -gt 0 ]; do
  case "$1" in
    --plain) mode=plain ;;
    --bundled) mode=bundled ;;
    --quick) quick=1 ;;
    -h|--help) usage ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) [ -z "$dir" ] || usage; dir="$1" ;;
  esac
  shift
done
[ -n "$dir" ] && [ -d "$dir" ] || { echo "error: give the release folder" >&2; usage; }
dir="$(cd "$dir" && pwd)"
ver="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$here/mse/__init__.py" | head -n 1)"
fail=0
bad() { say "FAIL  $*"; fail=1; }
ok() { say "ok    $*"; }

n=0
for img in "$dir"/*.dmg; do
  [ -e "$img" ] || continue
  n=$((n + 1))
  base="$(basename "$img")"
  marker="$img.bundled-runtime"
  need=""
  if [ "$mode" = plain ]; then
    ok "$base: declared plain (--plain); no source archive needed"
    continue
  elif [ -f "$marker" ]; then
    need="$(sed -n 's/^requires=//p' "$marker" | head -n 1)"
    [ -n "$need" ] || { bad "$base: $base.bundled-runtime has no requires= line"; continue; }
  elif [ "$mode" = bundled ]; then
    need="MySearch-$ver-sources.tar.gz"
  else
    bad "$base: no $base.bundled-runtime next to it, so I cannot tell whether it bundles the runtime. Say --plain or --bundled."
    continue
  fi
  arch="$dir/$need"
  if [ ! -f "$arch" ]; then
    bad "$base bundles the runtime, but $need is not in $dir. Make it with scripts/fetch_gpl_sources.sh and attach it to the same release."
    continue
  fi
  top="${need%.tar.gz}"
  list="$(tar -tzf "$arch" 2>/dev/null)" || { bad "$need does not read back as a .tar.gz"; continue; }
  miss=""
  for m in README.txt MANIFEST.tsv SHA256SUMS licenses/GPL-3.0.txt licenses/GPL-2.0.txt \
           ffmpeg/ffmpeg-7.1.1.tar.xz x264/x264-r3108-31e19f92.tar.gz x265/x265_4.1.tar.gz xvid/xvidcore-1.3.7.tar.bz2 \
           vidstab/vid.stab-1.1.1.tar.gz rubberband/rubberband-4.0.0.tar.bz2 opencv/opencv-5.0.0-40738fb1.tar.gz \
           opencv-python/opencv-python-93.tar.gz homebrew-formulas/ffmpeg.rb gcc/gcc-13.4.0.tar.xz \
           lgpl/libbluray/libbluray-1.3.4.tar.bz2 lgpl/gnutls/gnutls-3.8.9.tar.xz lgpl/gmp/gmp-6.3.0.tar.xz \
           lgpl/libidn2/libidn2-2.3.8.tar.gz lgpl/libunistring/libunistring-1.3.tar.gz lgpl/gettext/gettext-0.25.tar.gz \
           lgpl/libtasn1/libtasn1-4.20.0.tar.gz lgpl/lame/lame-3.100.tar.gz lgpl/soxr/soxr-0.1.3-Source.tar.xz \
           lgpl/libssh/libssh-0.11.1.tar.xz lgpl/aribb24/aribb24-v1.0.4.tar.bz2 lgpl/fribidi/fribidi-1.0.16.tar.xz \
           lgpl/glib/glib-2.84.3.tar.xz lgpl/graphite2/graphite2-1.3.14.tgz lgpl/zeromq/zeromq-4.3.5.tar.gz \
           lgpl/srt/srt-1.5.4.tar.gz lgpl/nettle/nettle-3.10.1.tar.gz lgpl/nettle/nettle-3.10.2.tar.gz \
           licenses/MPL-2.0.txt; do
    printf '%s\n' "$list" | grep -qx "$top/$m" || miss="$miss $m"
  done
  if [ -n "$miss" ]; then bad "$need is missing:$miss"; continue; fi
  if [ -z "$quick" ]; then
    sums="$(tar -xOzf "$arch" "$top/SHA256SUMS")" || { bad "$need: cannot read SHA256SUMS"; continue; }
    cnt=0; wrong=""
    while IFS= read -r line <&3; do
      want="${line%%  *}"; f="${line#*  }"
      [ -n "$f" ] || continue
      got="$(tar -xOzf "$arch" "$top/$f" 2>/dev/null | shasum -a 256 | cut -d' ' -f1)"
      [ "$got" = "$want" ] || wrong="$wrong $f"
      cnt=$((cnt + 1))
    done 3<<SUMS
$sums
SUMS
    if [ -n "$wrong" ]; then bad "$need: sha256 differs from SHA256SUMS for:$wrong"; continue; fi
    ok "$base: $need present, $cnt files match SHA256SUMS"
  else
    ok "$base: $need present with its expected files (sha256 check skipped by --quick)"
  fi
done
if [ "$n" -eq 0 ]; then bad "no .dmg in $dir"; fi
[ "$fail" -eq 0 ] && say "release folder complete" || say "release folder NOT complete"
exit "$fail"
