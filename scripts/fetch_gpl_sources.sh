#!/bin/sh
# Collects the source of the copyleft code inside the self-contained MySearch disk image (the one built with
# scripts/build_dmg.sh --bundle-runtime): the GPL components, the LGPL and MPL-2.0 libraries of the same wheels, and the
# Homebrew recipes and patches of those builds. Packs it as MySearch-<version>-sources.tar.gz, to be attached to the same
# GitHub release as the image. Plain images (no bundled runtime) contain none of this code and need no such file.
#
#   scripts/fetch_gpl_sources.sh <out-dir> [--dry-run]
#
#   --dry-run   prints every item (what it is, where it comes from, the pin that is checked, the size the server reports)
#               and downloads nothing; the folder is not created. It also checks that each host answers and that the
#               tags that name the pinned commits still point at them.
#   <out-dir>   must not exist or must be empty. Nothing is ever deleted or overwritten. The work folder <out-dir>/work
#               (git checkouts, partial downloads) stays behind; it is not part of the archive.
#
# What it fetches (all pinned; a download whose sha256 differs, or a commit that differs, stops the script):
#   GPL: FFmpeg 7.1.1 and Homebrew's two patches, x265 4.1 and Homebrew's two patches, x264 r3108 (commit 31e19f92), Xvid 1.3.7,
#   vid.stab 1.1.1, Rubber Band 4.0.0, the six Homebrew formulas that built them (homebrew-core ecfd2879), the OpenCV
#   source (5.0.0, commit 40738fb1) and the opencv-python build scripts (tag 93, commit b83046cd) that produced the macOS
#   arm64 wheel 5.0.0.93 locked in uv.lock; and, for the scipy wheel locked in the same file, GCC 13.4.0 with Homebrew's
#   Darwin patch and formula (libgcc_s, libgfortran, libquadmath).
#   LGPL and MPL-2.0: the sixteen libraries of the opencv-python wheel whose version is established (gnutls, gmp, libidn2,
#   libunistring, gettext's libintl, libtasn1, lame, soxr, libssh, libbluray, aribb24, fribidi, glib, graphite2, libzmq and
#   libsrt), with the two Homebrew patches and the Homebrew recipes of those builds. Nettle (libnettle, libhogweed,
#   GPL-2.0-or-later or LGPL-3.0-or-later): both upstream tarballs, 3.10.1 and 3.10.2, with their signature files, because
#   the binary reports version 3.10 only. No Nettle recipe is fetched.
#   How each pin was established is written into README.txt inside the archive.
# It does NOT fetch: the permissively licensed libraries of the wheels (their sources are public at their upstream projects;
# the written offer in THIRD_PARTY_NOTICES.md covers them) and Apple's system libraries; see "What is not in this archive"
# in that README.
# Network use: read-only downloads of source code (curl, git), about 267 MB. Needs curl, git, shasum, tar, xz support in tar and gzip.
set -eu
here="$(cd "$(dirname "$0")/.." && pwd)"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,30p' "$0" >&2; exit 2; }

dry=""
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry=1 ;;
    -h|--help) usage ;;
    -*) die "unknown option: $1 (accepted: --dry-run)" ;;
    *) [ -z "$out" ] || usage; out="$1" ;;
  esac
  shift
done
[ -n "$out" ] || usage
for t in curl git shasum tar gzip; do command -v "$t" >/dev/null 2>&1 || die "$t not found"; done

ver="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$here/mse/__init__.py" | head -n 1)"
[ -n "$ver" ] || die "cannot read the version from mse/__init__.py"
name="MySearch-$ver-sources"

HC=ecfd2879128b7a2f4b0d0bf53969426263c28584   # homebrew-core: the last commit before ffmpeg went from 7.1.1_3 to 7.1.1_4
HCORE="https://raw.githubusercontent.com/Homebrew/homebrew-core"
HF="$HCORE/$HC/Formula"
# the formula is named gcc, an at sign and 13 (written in pieces: the public-release audit reads that name as an e-mail address)
AT="@"
GCC13="gcc${AT}13.rb"

# kind | path inside the archive | URL or repository | pin (sha256 for "file", full commit id for "git") | approx. MB | note
# git items are fetched by commit id (shallow), archived with git archive and gzip -n, so the same commit always gives the
# same archive. Their sha256 is recorded, not pinned: the pin is the commit.
# Notes never contain a vertical bar. "formula as of" means the Homebrew formula at that homebrew-core commit; the URL, the
# sha256 and the patches named in the note are the ones of the pinned version at that commit.
ITEMS="
file|ffmpeg/ffmpeg-7.1.1.tar.xz|https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz|733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1|11|FFmpeg 7.1.1 (libavcodec 61.19.101 ... libpostproc 58.3.100), the release the libraries report
file|ffmpeg/ffmpeg-7.1.1.tar.xz.asc|https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz.asc|a52e92620b266ea341191a01b42a191e01c15a9f56e99b173582181781f5bc75|0|FFmpeg release signature (check with gpg and the FFmpeg release key)
file|ffmpeg/patches/ffmpeg-7.1.1-svt-av1-3.0-backport-d1ed5c06.patch|https://github.com/FFmpeg/FFmpeg/commit/d1ed5c06e3edc5f2b5f3664c80121fa55b0baa95.patch?full_index=1|0eb23ab90c0e5904590731dd3b81c86a4127785bc2b367267d77723990fb94a2|0|Homebrew patch 1 for FFmpeg: support for svt-av1 3.0
file|ffmpeg/patches/ffmpeg-7.1.1-add-av_stream_get_first_dts-for-chromium-arch-5670ccd8.patch|https://gitlab.archlinux.org/archlinux/packaging/packages/ffmpeg/-/raw/5670ccd86d3b816f49ebc18cab878125eca2f81f/add-av_stream_get_first_dts-for-chromium.patch|57e26caced5a1382cb639235f9555fc50e45e7bf8333f7c9ae3d49b3241d3f77|0|Homebrew patch 2 for FFmpeg: av_stream_get_first_dts for QtWebEngine
file|x265/x265_4.1.tar.gz|https://bitbucket.org/multicoreware/x265_git/downloads/x265_4.1.tar.gz|a31699c6a89806b74b0151e5e6a7df65de4b49050482fe5ebf8a4379d7af8f29|2|x265 4.1 (x265Version.txt: tag 4.1, distance 1, changeset 1d117be = the library's own \"4.1+1-1d117be\")
file|x265/patches/x265-4.1-cmake-policy-b354c009.patch|https://api.bitbucket.org/2.0/repositories/multicoreware/x265_git/diff/b354c009a60bcd6d7fc04014e200a1ee9c45c167|f7d3ce261c4b0cd461b55ad00de38ffa6a7cc2fa13ae6f034b3e46d8bb3cb6a8|0|Homebrew patch 1 for x265 (CMake policy lines only)
file|x265/patches/x265-4.1-cmake-policy-51ae8e92.patch|https://api.bitbucket.org/2.0/repositories/multicoreware/x265_git/diff/51ae8e922bcc4586ad4710812072289af91492a8|56c78f60cbaac61a44cb6e9889ece3380f9b60d32a4b704e274d9a636a16379d|0|Homebrew patch 2 for x265 (CMake policy lines only)
git|x264/x264-r3108-31e19f92.tar.gz|https://code.videolan.org/videolan/x264.git|31e19f92f00c7003fa115047ce50978bc98c3a0d|1|x264 r3108 (3108 commits, the library reports \"r3108 31e19f9\"); tree 898c5c5b3851a8736e44fcb69ccd2b29fd84a48f
file|xvid/xvidcore-1.3.7.tar.bz2|https://downloads.xvid.com/downloads/xvidcore-1.3.7.tar.bz2|aeeaae952d4db395249839a3bd03841d6844843f5a4f84c271ff88f7aa1acff7|1|Xvid 1.3.7, linked statically into libavcodec (its string \"xvid-1.3.7\" is in the library)
file|vidstab/vid.stab-1.1.1.tar.gz|https://github.com/georgmartius/vid.stab/archive/refs/tags/v1.1.1.tar.gz|9001b6df73933555e56deac19a0f225aae152abbc0e97dc70034814a1943f3d4|0|vid.stab 1.1.1 (libvidstab.1.2.dylib; its exported symbols equal a 1.1.1 build, not 1.1.2)
file|rubberband/rubberband-4.0.0.tar.bz2|https://breakfastquay.com/files/releases/rubberband-4.0.0.tar.bz2|af050313ee63bc18b35b2e064e5dce05b276aaf6d1aa2b8a82ced1fe2f8028e9|1|Rubber Band 4.0.0 (librubberband.3.dylib; library version 3.0.0, has RubberBandLiveShifter)
file|homebrew-formulas/ffmpeg.rb|$HF/f/ffmpeg.rb|562782f559db343cb8c628c5c30e1ef79dc823bb930819e3adf37c0cef1d53d3|0|Homebrew formula ffmpeg, revision 3 (version 7.1.1_3): the configure line and the patch list
file|homebrew-formulas/x264.rb|$HF/x/x264.rb|ec18743492727165307ea9db6c69ea4a4cd3a3aa72a0634bca2bb45630f38d03|0|Homebrew formula x264 r3108
file|homebrew-formulas/x265.rb|$HF/x/x265.rb|17000fc3ac24cff14ed9d3712fa5e6408efd8a3f072f72906cc64bd3d28fc513|0|Homebrew formula x265 4.1
file|homebrew-formulas/xvid.rb|$HF/x/xvid.rb|65715b77bf812d1b931dcb904ca6cf9f2e6742f715c2af2c24e53ab5d72e3e40|0|Homebrew formula xvid 1.3.7
file|homebrew-formulas/libvidstab.rb|$HF/lib/libvidstab.rb|0dca08908e5c58d2174f0013a7ce362ba95684b376aeb6a71f4a3e758e97c9c4|0|Homebrew formula libvidstab 1.1.1
file|homebrew-formulas/rubberband.rb|$HF/r/rubberband.rb|52e24ac53881a2cd6acd93891bdaf01b25e4163a536b65afe8694ff2048fb93b|0|Homebrew formula rubberband 4.0.0
git|opencv-python/opencv-python-93.tar.gz|https://github.com/opencv/opencv-python.git|b83046cda41133f1bf2e73e99dba16a1248f103a|1|opencv-python tag 93 (wheel 5.0.0.93): .github/workflows/build_wheels_macos_m1.yml, setup.py, pyproject.toml, scripts
git|opencv-python/multibuild-4546f4d4.tar.gz|https://github.com/multi-build/multibuild.git|4546f4d4820ffd0104b872c3b6187260b3e64edc|1|multibuild submodule of opencv-python tag 93 (initialised by the macOS job, not called by its build commands)
git|opencv/opencv-5.0.0-40738fb1.tar.gz|https://github.com/opencv/opencv.git|40738fb16ceddb5fb3fea747585f7ce6abb0605b|81|OpenCV 5.0.0 (the submodule of opencv-python tag 93; tag 5.0.0): the source of cv2.abi3.so that loads the GPL libraries
file|gcc/gcc-13.4.0.tar.xz|https://ftp.gnu.org/gnu/gcc/gcc-13.4.0/gcc-13.4.0.tar.xz|9c4ce6dbb040568fdc545588ac03c5cbc95a8dbf0c7aa490170843afb59ca8f5|89|GCC 13.4.0: libgcc, libgfortran and libquadmath, the runtime libraries inside the scipy wheel (Homebrew gcc@13 13.4.0, per the rpaths of its extension modules); libquadmath (LGPL-2.1-or-later) is in this tarball
file|gcc/patches/gcc-13.4.0-darwin-homebrew.diff|$HCORE/d17f3939dc55c4a42a6e5833a8353d0948bbd416/Patches/gcc/gcc-13.4.0.diff|60b22ae7f5f78b41e12c51d8c6e99ba933a7e124454fe8cdbff7200505167949|1|Homebrew's patch for GCC 13.4.0 (the Darwin maintainer's branch, with Apple Silicon support)
file|homebrew-formulas/gcc-13.rb|$HCORE/d17f3939dc55c4a42a6e5833a8353d0948bbd416/Formula/g/$GCC13|1ef04d2c64f88e549a8c924dfd49e1b474220d1795547b02936786e40d3b44f9|0|Homebrew formula gcc@13 (13.4.0) as of 2026-07-29, the last state before the scipy 1.18.1 wheel was uploaded
file|lgpl/libbluray/libbluray-1.3.4.tar.bz2|https://download.videolan.org/videolan/libbluray/1.3.4/libbluray-1.3.4.tar.bz2|478ffd68a0f5dde8ef6ca989b7f035b5a0a22c599142e5cd3ff7b03bbebe5f2b|1|libbluray 1.3.4 (bd_get_version 1.3.4; libbluray.2.dylib): LGPL-2.1-or-later
file|homebrew-formulas/libbluray.rb|$HCORE/b6c80205eaaca07cc8405a40e430e0cd25fc5ae0/Formula/lib/libbluray.rb|628a461d9a2da5cce74ca6b9bf00e374eb62e1fcd870b3b4ff10d8fec1018d14|0|formula as of 2025-03-20 (1.3.4, no patch); unchanged to the end of the FFmpeg window
file|lgpl/gnutls/gnutls-3.8.9.tar.xz|https://www.gnupg.org/ftp/gcrypt/gnutls/v3.8/gnutls-3.8.9.tar.xz|69e113d802d1670c4d5ac1b99040b1f2d5c7c05daec5003813c049b5184820ed|7|GnuTLS 3.8.9 (gnutls_check_version 3.8.9): libgnutls.30.dylib, LGPL-2.1-or-later
file|homebrew-formulas/gnutls.rb|$HCORE/ed7c0e5f0ef3580e7c2f3c67412274d21ef069b9/Formula/g/gnutls.rb|41c4486a5e8cbcdc8a1122604d27ed2989393838f6a05114065ce3bd9f489dca|0|formula as of 2025-03-23 (3.8.9, no patch); moved to 3.8.10 on 2025-07-09, the 3.8.9 pins are unchanged
file|lgpl/gmp/gmp-6.3.0.tar.xz|https://ftp.gnu.org/gnu/gmp/gmp-6.3.0.tar.xz|a3c2b80201b89e68616f4ad30bc66aee4927c3ce50e33929ca819d5c43538898|2|GMP 6.3.0 (__gmp_version 6.3.0): libgmp.10.dylib, LGPL-3.0-or-later or GPL-2.0-or-later
file|homebrew-formulas/gmp.rb|$HCORE/b21c5920d2232116d2c358e9fd5ae6d27e5ec68e/Formula/g/gmp.rb|81d1133fd6c33be299d5edee7b4c58156ff7016f3166d587acc538cfd25860ba|0|formula as of 2025-03-19 (6.3.0, no patch); unchanged to the end of the FFmpeg window
file|lgpl/libidn2/libidn2-2.3.8.tar.gz|https://ftp.gnu.org/gnu/libidn/libidn2-2.3.8.tar.gz|f557911bf6171621e1f72ff35f5b1825bb35b52ed45325dcdee931e5d3c0787a|2|libidn2 2.3.8 (idn2_check_version 2.3.8): libidn2.0.dylib, GPL-2.0-or-later or LGPL-3.0-or-later (its README)
file|homebrew-formulas/libidn2.rb|$HCORE/aada4f076c7ace031e319840d8f929d47cecda9f/Formula/lib/libidn2.rb|9942882a67485da0d1aa64c441d0d17ffe453712acc46ed3e9f310cfe9d4decd|0|formula as of 2025-03-19 (2.3.8, no patch); unchanged to the end of the FFmpeg window
file|lgpl/libunistring/libunistring-1.3.tar.gz|https://ftp.gnu.org/gnu/libunistring/libunistring-1.3.tar.gz|8ea8ccf86c09dd801c8cac19878e804e54f707cf69884371130d20bde68386b7|5|libunistring 1.3 (_libunistring_version 0x10300; version.sh VERSION_NUMBER=1.3): libunistring.5.dylib, LGPL-3.0-or-later or GPL-2.0-or-later (its README)
file|homebrew-formulas/libunistring.rb|$HCORE/4ad4a2d2faf0c657b42b526b3657477be59ff8df/Formula/lib/libunistring.rb|d43d08b0e1e12cecc3862e5642a3a2808e25641a45a68fdd052cffbb0c98d278|0|formula as of 2025-03-19 (1.3, no patch); unchanged to the end of the FFmpeg window
file|lgpl/gettext/gettext-0.25.tar.gz|https://ftp.gnu.org/gnu/gettext/gettext-0.25.tar.gz|aee02dab79d9138fdcc7226b67ec985121bce6007edebe30d0e39d42f69a340e|31|gettext 0.25.0 for libintl (libintl_version 0x1900; LIBINTL_VERSION 0x001900 in the source): libintl.8.dylib, LGPL-2.1-or-later (intl/COPYING.LIB); the tarball is the whole gettext project
file|homebrew-formulas/gettext.rb|$HCORE/9a340c18147e7f531184d25a7579645684e890ff/Formula/g/gettext.rb|e34c5ffa5081b0e70dc57ee91ca39f09c878ed3920b63c68f7fab6c2799e8f0a|0|formula as of 2025-05-08 (0.25, no patch); moved to 0.25.1 on 2025-07-02, the 0.25 pin is unchanged
file|lgpl/libtasn1/libtasn1-4.20.0.tar.gz|https://ftp.gnu.org/gnu/libtasn1/libtasn1-4.20.0.tar.gz|92e0e3bd4c02d4aeee76036b2ddd83f0c732ba4cda5cb71d583272b23587a76c|2|libtasn1 4.20.0 (asn1_check_version 4.20.0): libtasn1.6.dylib, LGPL-2.1-or-later
file|homebrew-formulas/libtasn1.rb|$HCORE/1ff6026d2616cbc3360a4cb92f8d26dd051f68b2/Formula/lib/libtasn1.rb|e08ebf070491c155e6cd817879ca3ef9379355157dc296867cf56d8ca6d996d7|0|formula as of 2025-03-19 (4.20.0, no patch); unchanged to the end of the FFmpeg window
file|lgpl/lame/lame-3.100.tar.gz|https://downloads.sourceforge.net/project/lame/lame/3.100/lame-3.100.tar.gz|ddfe36cab873794038ae2c1210557ad34857a4b6bdc515785d1da9e175b1da1e|2|LAME 3.100 (get_lame_version 3.100): libmp3lame.0.dylib, LGPL-2.0-or-later
file|homebrew-formulas/lame.rb|$HCORE/e8c27fb0b078f479404e7950ff1f28d86182d5d8/Formula/l/lame.rb|b86eef7828e22d383b6b9c30643ea8ab83a10c5587c16b5f87d0a4fdb4518228|0|formula as of 2025-03-19 (3.100, no patch); unchanged to the end of the FFmpeg window
file|lgpl/soxr/soxr-0.1.3-Source.tar.xz|https://downloads.sourceforge.net/project/soxr/soxr-0.1.3-Source.tar.xz|b111c15fdc8c029989330ff559184198c161100a59312f5dc19ddeb9b5a15889|0|libsoxr 0.1.3 (soxr_version libsoxr-0.1.3): libsoxr.0.1.2.dylib, LGPL-2.1-or-later
file|lgpl/soxr/patches/soxr-arm64_defines.patch|https://raw.githubusercontent.com/Homebrew/formula-patches/76868b36263be42440501d3692fd3a258f507d82/libsoxr/arm64_defines.patch|9df5737a21b9ce70cc136c302e195fad9f9f6c14418566ad021f14bb34bb022c|0|Homebrew patch for libsoxr 0.1.3; in the libsoxr formula at every revision from 2023-09-13 to 2026-09-10; applies with patch -p1
file|homebrew-formulas/libsoxr.rb|$HCORE/1e47f4def3b4df9a5cf89489d35e25048a3c6c3c/Formula/lib/libsoxr.rb|fd271615474170ca3297a0fa41c3f401288de95d4f1ee6c3e7bec80e58191648|0|formula as of 2025-03-19 (0.1.3 with the arm64_defines patch); unchanged to the end of the FFmpeg window
file|lgpl/libssh/libssh-0.11.1.tar.xz|https://www.libssh.org/files/0.11/libssh-0.11.1.tar.xz|14b7dcc72e91e08151c58b981a7b570ab2663f630e7d2837645d5a9c612c1b79|1|libssh 0.11.1 (ssh_version 0.11.1; build path libssh-0.11.1 in the binary): libssh.4.10.1.dylib, LGPL-2.1-or-later
file|homebrew-formulas/libssh.rb|$HCORE/15751083ec9e20c080d6c80d9e07f74e517bbb11/Formula/lib/libssh.rb|88ec66e7ccb8baf0fcc3200f0989981aa4666b99f2509d9314f88d9adeafb5db|0|compiled 2024-09-01 (build path libssh-20240901 in the binary); formula 0.11.1, no patch
file|lgpl/aribb24/aribb24-v1.0.4.tar.bz2|https://code.videolan.org/jeeb/aribb24/-/archive/v1.0.4/aribb24-v1.0.4.tar.bz2|88b58dd760609372701087e25557ada9f7c6d973306c017067c5dcaf9e2c9710|0|aribb24 1.0.4 (formula history only; the binary has no version string): libaribb24.0.dylib, LGPL-2.1-or-later per the source headers (its COPYING holds the LGPL-3.0 text)
file|homebrew-formulas/aribb24.rb|$HCORE/9142e63df3d20867635b529f1da0889a1caa225e/Formula/a/aribb24.rb|9c8d2a63fb26c59e92e0827b8ac4cd9cfb3a228aa23e10df85776ee8d19bf725|0|formula as of 2025-03-19 (1.0.4 in every revision since 2023-08; no patch)
file|lgpl/fribidi/fribidi-1.0.16.tar.xz|https://github.com/fribidi/fribidi/releases/download/v1.0.16/fribidi-1.0.16.tar.xz|1b1cde5b235d40479e91be2f0e88a309e3214c8ab470ec8a2744d82a5a9ea05c|1|FriBidi 1.0.16 (fribidi_version_info 1.0.16): libfribidi.0.dylib, LGPL-2.1-or-later
file|homebrew-formulas/fribidi.rb|$HCORE/a65e05e2ed7bbcd376f58109eb6a2a35d13183cb/Formula/f/fribidi.rb|dee3d4b95955353c853931abaff1793fbc2d01e575c6d255a2e5570fee3f5d83|0|formula as of 2025-03-19 (1.0.16, no patch); unchanged to the end of the FFmpeg window
file|lgpl/glib/glib-2.84.3.tar.xz|https://download.gnome.org/sources/glib/2.84/glib-2.84.3.tar.xz|aa4f87c3225bf57ca85f320888f7484901a17934ca37023c3bd8435a72db863e|6|GLib 2.84.3 (glib_*_version 2.84.3; build path glib/2.84.3 in the binary): libglib-2.0.0.dylib, LGPL-2.1-or-later
file|lgpl/glib/patches/glib-hardcoded-paths.diff|https://raw.githubusercontent.com/Homebrew/formula-patches/b46d8deae6983110b4e39bb2971bcbd10bb59716/glib/hardcoded-paths.diff|d846efd0bf62918350da94f850db33b0f8727fece9bfaf8164566e3094e80c97|0|Homebrew patch for GLib 2.84.3; in the formula of 2.84.3 (commit 5192b699); applies with patch -p1
file|homebrew-formulas/glib.rb|$HCORE/5192b699b21fa3ef2a0cb70f47f2438e019b19a6/Formula/g/glib.rb|966c866d2adc70478aff4e8a5e17245e9b3185b9a5dd2a6b9e7076053e9e5e80|0|formula of 2.84.3 (2025-06-13), with the hardcoded-paths patch; moved to 2.84.4 on 2025-08-08
file|lgpl/graphite2/graphite2-1.3.14.tgz|https://github.com/silnrsi/graphite/releases/download/1.3.14/graphite2-1.3.14.tgz|f99d1c13aa5fa296898a181dff9b82fb25f6cc0933dbaa7a475d8109bd54209d|7|Graphite2 1.3.14 (gr_engine_version 1.3.14): libgraphite2.3.2.1.dylib, LGPL-2.1-or-later (COPYING: version 2.1 or any later version; it also offers MPL, no version stated, and GPL-2.0-or-later)
file|homebrew-formulas/graphite2.rb|$HCORE/3add0f55a4e288dce7afd6d0fee306bbff6722c0/Formula/g/graphite2.rb|2d5ff2b5418aa442ff0b0967dfe03381c29bce332880c5c2603a29e22407ad04|0|formula as of 2025-03-19 (1.3.14, no patch); unchanged to the end of the FFmpeg window
file|lgpl/zeromq/zeromq-4.3.5.tar.gz|https://github.com/zeromq/libzmq/releases/download/v4.3.5/zeromq-4.3.5.tar.gz|6653ef5910f17954861fe72332e68b03ca6e4d9c7160eb3a8de5a5a913bfab43|3|libzmq 4.3.5 (zmq_version 4.3.5): libzmq.5.dylib, MPL-2.0; built without the draft API (none of its draft functions is exported)
file|homebrew-formulas/zeromq.rb|$HCORE/69c4f9c09f1d20277b479b67eb84cc8c5f6a36eb/Formula/z/zeromq.rb|151f97af21f13134a8588ca2e63562b8ed8c215bc773ef835746aaa5289e573c|0|formula as of 2025-03-20 (4.3.5_1, configured without --enable-drafts, as the binary shows); moved to zeromq: enable drafts on 2025-07-04, the 4.3.5 pins are unchanged
file|lgpl/srt/srt-1.5.4.tar.gz|https://github.com/Haivision/srt/archive/refs/tags/v1.5.4.tar.gz|d0a8b600fe1b4eaaf6277530e3cfc8f15b8ce4035f16af4a5eb5d4b123640cdd|2|SRT 1.5.4 (srt_getversion 1.5.4; build path srt-1.5.4 in the binary): libsrt.1.5.4.dylib, MPL-2.0
file|homebrew-formulas/srt.rb|$HCORE/561a7a3f6d52776d50de407d23cb2cf44fdfd141/Formula/s/srt.rb|4b4eb56cc87eaa40bedb61faf4956165327adf4c71d0ecbf48539b406dbd2984|0|compiled 2024-11-11 (build path srt-20241111 in the binary); formula 1.5.4 with no patch (patch 7962936829 was added on 2025-05-12 and is not in this build)
file|lgpl/nettle/nettle-3.10.1.tar.gz|https://ftp.gnu.org/gnu/nettle/nettle-3.10.1.tar.gz|b0fcdd7fc0cdea6e80dcf1dd85ba794af0d5b4a57e26397eee3bc193272d9132|3|Nettle 3.10.1 (libnettle.8.10 and libhogweed.6.10): the binary reports version 3.10 only, so both patch releases are included; GPL-2.0-or-later or LGPL-3.0-or-later; the sha256 is the one THIRD_PARTY_NOTICES.md gives for the Homebrew formula of 3.10.1
file|lgpl/nettle/nettle-3.10.1.tar.gz.sig|https://ftp.gnu.org/gnu/nettle/nettle-3.10.1.tar.gz.sig|7542775e1fe0404ee7807c8669b105e18c417e4cb3d5e8aa808dafb246b9ec23|0|OpenPGP signature of nettle-3.10.1.tar.gz, issuer key ID F3599FF828C67298: check with gpg and the Nettle release key (not verified when the pin was set)
file|lgpl/nettle/nettle-3.10.2.tar.gz|https://ftp.gnu.org/gnu/nettle/nettle-3.10.2.tar.gz|fe9ff51cb1f2abb5e65a6b8c10a92da0ab5ab6eaf26e7fc2b675c45f1fb519b5|3|Nettle 3.10.2 (the same library; the binary reports version 3.10 only, so both patch releases are included); the sha256 is the one THIRD_PARTY_NOTICES.md gives for the Homebrew formula of 3.10.2
file|lgpl/nettle/nettle-3.10.2.tar.gz.sig|https://ftp.gnu.org/gnu/nettle/nettle-3.10.2.tar.gz.sig|9cc1319ede378148f4c729ad889e3c0bd461d974ea590916f3480b93257a1d0b|0|OpenPGP signature of nettle-3.10.2.tar.gz, issuer key ID F3599FF828C67298: check with gpg and the Nettle release key (not verified when the pin was set)
"

# refs that name a pinned commit (checked in --dry-run and again after the fetch): repository | ref
REFS="
https://github.com/opencv/opencv-python.git|refs/tags/93^{}|b83046cda41133f1bf2e73e99dba16a1248f103a
https://github.com/opencv/opencv.git|refs/tags/5.0.0^{}|40738fb16ceddb5fb3fea747585f7ce6abb0605b
https://code.videolan.org/jeeb/aribb24.git|refs/tags/v1.0.4^{}|a41b904d66ed991ace32a9827f629db3d438b47b
https://github.com/Haivision/srt.git|refs/tags/v1.5.4|a8c6b65520f814c5bd8f801be48c33ceece7c4a6
"

sha256_of() { shasum -a 256 "$1" | cut -d' ' -f1; }

# Every pin above describes one wheel: opencv-python 5.0.0.93 for macOS arm64. If uv.lock locks another, the libraries inside
# it may be other versions (or LGPL ones), and nothing here may be reused without establishing them again (docs/RELEASING.md).
WHEEL_LINE='opencv_python-5.0.0.93-cp37-abi3-macosx_13_0_arm64.whl", hash = "sha256:198a75138241810206a17c829dbcc40a7cb1841cda538ca86cbbfc6c7d95f898"'
SCIPY_LINE='scipy-1.18.1-cp313-cp313-macosx_14_0_arm64.whl", hash = "sha256:ea324d9dd34c38bfb9bec8ca4d1b407db97dbb74029f566b8e322b1b6fe56fe6"'
grep -qF "$SCIPY_LINE" "$here/uv.lock" 2>/dev/null \
  || die "uv.lock does not lock the scipy 1.18.1 macOS arm64 wheel (sha256 ea324d9d...) whose GCC runtime these pins describe. Establish its libraries first: docs/RELEASING.md, \"When a dependency changes\"."
grep -qF "$WHEEL_LINE" "$here/uv.lock" 2>/dev/null \
  || die "uv.lock does not lock the opencv-python 5.0.0.93 macOS arm64 wheel (sha256 198a7513...) that these pins describe. Establish the libraries of the new wheel first: docs/RELEASING.md, \"When a dependency changes\"."

# ---------------------------------------------------------------------------------------------------------- dry run
if [ -n "$dry" ]; then
  say "Dry run for $name: nothing is downloaded or written."
  say ""
  printf '%s\n' "$ITEMS" | while IFS='|' read -r kind path src pin mb note; do
    [ -n "$kind" ] || continue
    printf '%-4s %s\n' "$kind" "$path"
    printf '     from  %s\n' "$src"
    printf '     pin   %s %s\n' "$([ "$kind" = file ] && echo sha256 || echo commit)" "$pin"
    printf '     what  %s\n' "$note"
    if [ "$kind" = file ]; then
      # HEAD first; some hosts refuse it (403) or report no size, then a one-byte range request answers the same question
      hdr="$(curl -sIL --max-time 40 --connect-timeout 15 -A 'MySearch-sources/1' "$src" </dev/null 2>/dev/null | tr -d '\r' || true)"
      code="$(printf '%s\n' "$hdr" | awk 'toupper($1) ~ /^HTTP/ {c=$2} END {print c}')"
      len="$(printf '%s\n' "$hdr" | awk 'tolower($1) == "content-length:" {l=$2} END {print l}')"
      if [ "${code:-}" != 200 ] || [ -z "${len:-}" ] || [ "${len:-0}" = 0 ]; then
        hdr="$(curl -sL -r 0-0 -D - -o /dev/null --max-time 40 --connect-timeout 15 --max-filesize 20000000 -A 'MySearch-sources/1' "$src" </dev/null 2>/dev/null | tr -d '\r' || true)"
        code="$(printf '%s\n' "$hdr" | awk 'toupper($1) ~ /^HTTP/ {c=$2} END {print c}')"
        len="$(printf '%s\n' "$hdr" | awk 'tolower($1) == "content-range:" {split($3, a, "/"); l=a[2]}  END {print l}')"
        [ "${code:-}" = 206 ] && code=200
      fi
      printf '     host  HTTP %s, %s\n' "${code:-no answer}" "$([ -n "${len:-}" ] && [ "${len:-0}" != 0 ] && echo "$len bytes" || echo 'size not reported (about '"$mb"' MB)')"
      [ "${code:-}" = 200 ] || printf '     WARNING: expected HTTP 200\n'
    else
      if git ls-remote "$src" HEAD </dev/null >/dev/null 2>&1; then printf '     host  repository answers (git ls-remote)\n'; else printf '     WARNING: repository does not answer\n'; fi
      printf '     size  about %s MB as a .tar.gz\n' "$mb"
    fi
  done
  say ""
  say "Tags that must still point at the pinned commits:"
  printf '%s\n' "$REFS" | while IFS='|' read -r repo ref want; do
    [ -n "$repo" ] || continue
    got="$(git ls-remote "$repo" "$ref" </dev/null 2>/dev/null | cut -f1 | head -n 1)"
    if [ "$got" = "$want" ]; then printf '  ok       %s %s -> %s\n' "$repo" "$ref" "$got"; else printf '  MISMATCH %s %s -> %s (pinned %s)\n' "$repo" "$ref" "${got:-nothing}" "$want"; fi
  done
  say ""
  est="$(printf '%s\n' "$ITEMS" | awk -F'|' 'NF >= 6 {s += $5} END {print s}')"
  say "Estimated total: about $est MB (the archive $name.tar.gz will be about the same size, plus a few KB of licences and notes)."
  say "Real run: scripts/fetch_gpl_sources.sh <new-or-empty-folder>"
  exit 0
fi

# ---------------------------------------------------------------------------------------------------------- real run
if [ -e "$out" ]; then
  [ -d "$out" ] || die "$out exists and is not a folder"
  [ -z "$(ls -A "$out")" ] || die "$out is not empty. Use a new folder; nothing is overwritten."
fi
mkdir -p "$out"
out="$(cd "$out" && pwd -P)"
root="$out/$name"
work="$out/work"
mkdir -p "$root" "$work"
[ -d "$here/licenses" ] || die "$here/licenses is missing"

: > "$work/manifest.tsv"
record() { # path source pin kind
  f="$root/$1"
  printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$(wc -c < "$f" | tr -d ' ')" "$(sha256_of "$f")" "$3" "$2" >> "$work/manifest.tsv"
}

printf '%s\n' "$ITEMS" > "$work/items.txt"
while IFS='|' read -r kind path src pin mb note <&3; do
  [ -n "$kind" ] || continue
  dest="$root/$path"
  mkdir -p "$(dirname "$dest")"
  if [ "$kind" = file ]; then
    say "==> $path"
    curl -fsSL --retry 3 --connect-timeout 20 --max-time 900 -A 'MySearch-sources/1' -o "$dest.part" "$src" </dev/null \
      || die "download failed: $src (a partial file $dest.part may remain; it is safe to leave)"
    got="$(sha256_of "$dest.part")"
    [ "$got" = "$pin" ] || die "sha256 mismatch for $src: got $got, pinned $pin (the file is left as $dest.part)"
    mv "$dest.part" "$dest"
    say "    sha256 ok, $(wc -c < "$dest" | tr -d ' ') bytes"
    record "$path" "$src" "sha256:$pin"
  else
    say "==> $path (git, commit $pin)"
    g="$work/git-$(basename "$path" .tar.gz)"
    mkdir -p "$g"
    git -C "$g" init -q
    git -C "$g" remote add origin "$src"
    GIT_TERMINAL_PROMPT=0 git -C "$g" fetch -q --depth 1 origin "$pin" </dev/null || die "git fetch of $pin from $src failed"
    got="$(git -C "$g" rev-parse 'FETCH_HEAD^{commit}')"
    [ "$got" = "$pin" ] || die "commit mismatch for $src: got $got, pinned $pin"
    tree="$(git -C "$g" rev-parse 'FETCH_HEAD^{tree}')"
    prefix="$(basename "$path" .tar.gz)"
    git -C "$g" archive --format=tar --prefix="$prefix/" FETCH_HEAD | gzip -n -9 > "$dest.part"
    mv "$dest.part" "$dest"
    say "    commit ok, tree $tree, $(wc -c < "$dest" | tr -d ' ') bytes"
    record "$path" "$src" "commit:$pin tree:$tree"
  fi
done 3< "$work/items.txt"

# sanity checks on what was fetched (not only that the hashes match what was pinned)
tar -xOzf "$root/opencv/opencv-5.0.0-40738fb1.tar.gz" opencv-5.0.0-40738fb1/modules/core/include/opencv2/core/version.hpp 2>/dev/null \
  | grep -E 'define CV_VERSION_(MAJOR|MINOR|REVISION) ' | tr -s ' ' | tr '\n' ';' | grep -q 'CV_VERSION_MAJOR 5;.*CV_VERSION_MINOR 0;.*CV_VERSION_REVISION 0;' \
  || die "the OpenCV source is not 5.0.0"
tar -xOzf "$root/opencv-python/opencv-python-93.tar.gz" opencv-python-93/.github/workflows/build_wheels_macos_m1.yml 2>/dev/null | grep -q 'macOS ARM64' \
  || die "the opencv-python archive has no macOS ARM64 workflow"
tar -xOJf "$root/ffmpeg/ffmpeg-7.1.1.tar.xz" ffmpeg-7.1.1/RELEASE 2>/dev/null | grep -qx '7.1.1' || die "the FFmpeg archive is not 7.1.1"
tar -xOzf "$root/x265/x265_4.1.tar.gz" x265_4.1/x265Version.txt 2>/dev/null | grep -q 'repositorychangeset: 1d117be' || die "x265 is not 1d117be"
tar -xOqJf "$root/gcc/gcc-13.4.0.tar.xz" gcc-13.4.0/gcc/BASE-VER 2>/dev/null | grep -qx '13.4.0' || die "the GCC archive is not 13.4.0"
# the LGPL and MPL-2.0 sources: each one's own build file or version header must say the pinned version (grep -E on one member)
member_has() { # archive (relative to root) | member | extended regular expression
  tar -xOf "$root/$1" "$2" 2>/dev/null | grep -qE "$3" || die "$1: $2 does not match $3"
}
member_has lgpl/libbluray/libbluray-1.3.4.tar.bz2 libbluray-1.3.4/configure.ac '^m4_define\(\[bluray_micro\], 4\)'
member_has lgpl/gnutls/gnutls-3.8.9.tar.xz gnutls-3.8.9/configure.ac 'AC_INIT\(\[GnuTLS\],\[3\.8\.9\]'
member_has lgpl/gmp/gmp-6.3.0.tar.xz gmp-6.3.0/gmp-h.in '#define __GNU_MP_VERSION_MINOR[[:space:]]+3[[:space:]]*$'
member_has lgpl/libidn2/libidn2-2.3.8.tar.gz libidn2-2.3.8/.tarball-version '^2\.3\.8$'
member_has lgpl/libunistring/libunistring-1.3.tar.gz libunistring-1.3/version.sh '^VERSION_NUMBER=1\.3$'
member_has lgpl/gettext/gettext-0.25.tar.gz gettext-0.25/gettext-runtime/intl/libgnuintl.in.h '#define LIBINTL_VERSION[[:space:]]+0x001900'
member_has lgpl/libtasn1/libtasn1-4.20.0.tar.gz libtasn1-4.20.0/.tarball-version '^4\.20\.0$'
member_has lgpl/lame/lame-3.100.tar.gz lame-3.100/libmp3lame/version.h 'LAME_MINOR_VERSION[[:space:]]+100'
member_has lgpl/soxr/soxr-0.1.3-Source.tar.xz soxr-0.1.3-Source/CMakeLists.txt 'set \(PROJECT_VERSION_PATCH 3\)'
member_has lgpl/libssh/libssh-0.11.1.tar.xz libssh-0.11.1/CMakeLists.txt 'project\(libssh VERSION 0\.11\.1'
member_has lgpl/aribb24/aribb24-v1.0.4.tar.bz2 aribb24-v1.0.4/configure.ac 'AC_INIT\(\[aribb24\], \[1\.0\.4\]'
member_has lgpl/fribidi/fribidi-1.0.16.tar.xz fribidi-1.0.16/meson.build "version: '1\.0\.16'"
member_has lgpl/glib/glib-2.84.3.tar.xz glib-2.84.3/meson.build "version : '2\.84\.3'"
member_has lgpl/graphite2/graphite2-1.3.14.tgz graphite2-1.3.14/ChangeLog '1\.3\.14'
member_has lgpl/zeromq/zeromq-4.3.5.tar.gz zeromq-4.3.5/include/zmq.h '#define ZMQ_VERSION_PATCH[[:space:]]+5'
member_has lgpl/srt/srt-1.5.4.tar.gz srt-1.5.4/CMakeLists.txt 'set \(SRT_VERSION 1\.5\.4\)'
member_has lgpl/nettle/nettle-3.10.1.tar.gz nettle-3.10.1/configure.ac 'AC_INIT\(\[nettle\], \[3\.10\.1\]'
member_has lgpl/nettle/nettle-3.10.2.tar.gz nettle-3.10.2/configure.ac 'AC_INIT\(\[nettle\], \[3\.10\.2\]'
say "==> sanity checks passed (OpenCV 5.0.0, macOS ARM64 workflow, FFmpeg 7.1.1, x265 changeset 1d117be, GCC 13.4.0; 16 LGPL and MPL-2.0 sources and Nettle 3.10.1 and 3.10.2 at their pinned versions)"

# the licence texts of this repository, byte for byte
mkdir -p "$root/licenses"
for f in "$here"/licenses/*; do
  [ -f "$f" ] || continue
  cp "$f" "$root/licenses/$(basename "$f")"
  record "licenses/$(basename "$f")" "this repository, licenses/" "copy"
done

# ------------------------------------------------------------------------------------------------ README.txt in the archive
cat > "$root/README.txt" <<EOF
MySearch $ver: source of the copyleft code in the self-contained disk image
==========================================================================

This archive belongs to the release that carries MySearch-$ver.dmg built with scripts/build_dmg.sh --bundle-runtime.
That image contains free-software libraries that it did not build itself. They arrive inside the macOS arm64 wheel of
opencv-python 5.0.0.93 (cv2/.dylibs of the bundled Python) and inside the scipy 1.18.1 wheel (scipy/.dylibs). Most are
GNU GPL, LGPL or MPL-2.0 licensed. This archive holds the source code of the GPL components, of the OpenCV module that
loads them, of the GCC runtime, and of the LGPL and MPL-2.0 libraries whose version is established (below), with the Homebrew
recipes and patches of those builds. It does not hold the other permissively licensed libraries of the wheels. It holds nettle
as well, both 3.10.1 and 3.10.2 (see below).
MySearch's own code is MIT-licensed and public. THIRD_PARTY_NOTICES.md inside the app (MySearch.app/Contents/Resources) gives
the full account, the written offer and the licence texts (Resources/Licenses). MySearch does not modify any of these
libraries; its build only rewrites their install names and signs them again.

GPL components in the image
---------------------------
  FFmpeg 7.1.1            libavutil 59.39.100, libavcodec 61.19.101, libavformat 61.7.100, libavfilter 10.4.100,
                          libavdevice 61.3.100, libswscale 8.3.100, libswresample 5.3.100, libpostproc 58.3.100.
                          Configured with --enable-gpl --enable-version3: "GPL version 3 or later".
                          Source: ffmpeg/ffmpeg-7.1.1.tar.xz plus ffmpeg/patches/*.patch (applied by Homebrew's formula).
  x264 r3108              libx264.164.dylib. GPL version 2 or later. Source: x264/ (commit 31e19f92f00c...).
  x265 4.1+1-1d117be      libx265.215.dylib. GPL version 2 or later. Source: x265/ (x265_4.1.tar.gz plus two CMake patches).
  Xvid 1.3.7              statically linked into libavcodec (not a separate file). GPL version 2 or later. Source: xvid/.
  vid.stab 1.1.1          libvidstab.1.2.dylib, loaded by libavfilter. GPL version 2 or later. Source: vidstab/.
  Rubber Band 4.0.0       librubberband.3.dylib, loaded by libavfilter. GPL version 2 or later. Source: rubberband/.
  OpenCV 5.0.0            cv2/cv2.abi3.so (Apache-2.0) loads libavcodec, libavformat, libavutil, libswscale and libavdevice
                          directly, so it is the other half of the combined work. Source: opencv/ and opencv-python/.
  GCC 13.4.0 runtime      libgcc_s.1.1.dylib and libgfortran.5.dylib (GPL version 3 or later with the GCC Runtime Library
                          Exception 3.1) and libquadmath.0.dylib (LGPL version 2.1 or later), in scipy/.dylibs.
                          Source: gcc/gcc-13.4.0.tar.xz plus gcc/patches/gcc-13.4.0-darwin-homebrew.diff (Homebrew's patch).
                          The libquadmath source is inside that tarball.

LGPL and MPL-2.0 libraries in the image
---------------------------------------
Licence as stated in the source's own licence file (the source headers where they differ from the file); the evidence for
the version is in brackets. Each source is in lgpl/<library>/ of this archive.
  libbluray 1.3.4          libbluray.2.dylib. LGPL-2.1-or-later.                  [bd_get_version: 1.3.4]
  GnuTLS 3.8.9             libgnutls.30.dylib. LGPL-2.1-or-later.                 [gnutls_check_version: 3.8.9]
  GMP 6.3.0                libgmp.10.dylib. LGPL-3.0-or-later or GPL-2.0-or-later. [__gmp_version: 6.3.0]
  libidn2 2.3.8            libidn2.0.dylib. GPL-2.0-or-later or LGPL-3.0-or-later (dual; the README). [idn2_check_version: 2.3.8]
  libunistring 1.3         libunistring.5.dylib. LGPL-3.0-or-later or GPL-2.0-or-later (the README). [_libunistring_version: 0x10300]
  gettext 0.25 (libintl)   libintl.8.dylib. LGPL-2.1-or-later (intl/COPYING.LIB). [libintl_version: 0x1900]
  libtasn1 4.20.0          libtasn1.6.dylib. LGPL-2.1-or-later.                   [asn1_check_version: 4.20.0]
  LAME 3.100               libmp3lame.0.dylib. LGPL-2.0-or-later.                 [get_lame_version: 3.100]
  soxr 0.1.3               libsoxr.0.1.2.dylib. LGPL-2.1-or-later.                [soxr_version: libsoxr-0.1.3]
  libssh 0.11.1            libssh.4.10.1.dylib. LGPL-2.1-or-later.                [ssh_version: 0.11.1]
  aribb24 1.0.4            libaribb24.0.dylib. LGPL-2.1-or-later per the source headers; its COPYING holds the LGPL-3.0 text.
                                                                                  [formula history only; no version in the binary]
  FriBidi 1.0.16           libfribidi.0.dylib. LGPL-2.1-or-later.                 [fribidi_version_info: 1.0.16]
  GLib 2.84.3              libglib-2.0.0.dylib. LGPL-2.1-or-later.                [glib_*_version: 2.84.3]
  Graphite2 1.3.14         libgraphite2.3.2.1.dylib. LGPL-2.1-or-later (COPYING: version 2.1 or any later version); its
                           COPYING also offers MPL (no version stated) and
                           GPL-2.0-or-later.                                       [gr_engine_version: 1.3.14]
  libzmq 4.3.5             libzmq.5.dylib. MPL-2.0.                               [zmq_version: 4.3.5]
  libsrt 1.5.4             libsrt.1.5.4.dylib. MPL-2.0.                           [srt_getversion: 1.5.4]
  nettle                   libnettle.8.10.dylib and libhogweed.6.10.dylib. GPL-2.0-or-later or LGPL-3.0-or-later.
                           Source: lgpl/nettle/, both 3.10.1 and 3.10.2. Note: the binary reports version 3.10; both 3.10.1 and 3.10.2 sources are included because the exact patch release could not be determined from the binary.
  libquadmath              scipy/.dylibs. LGPL-2.1-or-later. Its source is the GCC tarball above.

The MPL-2.0 licence text (for libzmq and libsrt) is the file MPL-2.0.txt in the licenses folder of this archive, the
canonical Mozilla text. Its sha256 is listed in licenses/README.md.

How the versions were established (reproducible with the commands in THIRD_PARTY_NOTICES.md)
-----------------------------------------------------------------------------------------
  * Runtime: each library was loaded and asked for its own version through its exported functions or variables (the
    evidence in brackets above). Where a library has no version function, the version comes from a build path or from the
    Homebrew formula history (aribb24 only).
  * Source: each tarball's own build file or version header was read and gives the same version. The check is repeated by
    the sanity checks of the script.
  * Recipe: homebrew-formulas/<name>.rb is the Homebrew formula at the homebrew-core commit named in its MANIFEST.tsv line.
    It carries the same url and sha256 as the tarball. Build paths that name a date (libssh 2024-09-01, srt 2024-11-11) fix
    the recipe to that bottle's commit. For the others, the note says the commit and whether the formula moved on later
    in the FFmpeg window (2025-05-27 to 2025-08-14).
  * Patches: soxr's arm64_defines.patch and glib's hardcoded-paths.diff apply with patch -p1 to their tarballs. The soxr
    patch is in every libsoxr formula revision from 2023-09-13 to 2026-09-10. The srt patch 7962936829 is not in this
    archive: it entered the srt formula on 2025-05-12, after the srt bottle of this build was made (2024-11-11).
  * zeromq: the binary exports none of the draft-API functions of the 4.3.5 header (the poller, ctx ext, socket monitor
    versioned and other draft names), so it was built without --enable-drafts. The recipe is the formula of 2025-03-20
    (4.3.5_1), which has no drafts flag.
  * Nettle: the binary reports version 3.10; both 3.10.1 and 3.10.2 sources are included because the exact patch release could not be determined from the binary.
    The Homebrew formula was 3.10.1 until 2025-06-26 and 3.10.2 after that date,
    and the FFmpeg window contains both. The sha256 of the two tarballs as downloaded on 2026-10-11 equal the values that
    THIRD_PARTY_NOTICES.md gives for the two Homebrew formulas (the formulas were not fetched again). The .sig files are
    archived with sha256 pins. Their issuer key ID is F3599FF828C67298, which was not in the keyring used, so the
    signatures are not verified here: check them with gpg and the Nettle release key.
  * Tags: aribb24 v1.0.4 is commit a41b904d66ed991ace32a9827f629db3d438b47b and srt v1.5.4 is commit
    a8c6b65520f814c5bd8f801be48c33ceece7c4a6. Their tarballs come from the hosts' archive endpoints, which are generated
    on request; the sha256 pins are what the script checks, and the dry run checks the tag commits.
  * The GPL libraries: the wheel's Homebrew FFmpeg 7.1.1_3 is the one in the formula window 2025-05-27 to 2025-08-14. The
    libraries' own version strings, not the formulas, decided the GPL versions above. The build dates in the libssh and srt
    binaries (2024-09-01, 2024-11-11) show when CI compiled those bottles, not when the wheel's machine installed them.
  * homebrew-formulas/ of the GPL components are the formulas at homebrew-core commit $HC, the last commit before ffmpeg
    moved from revision 3 to 4. Between the commit that published the 7.1.1_3 bottles (a671b0c7) and that one, ffmpeg.rb is
    unchanged and x264.rb and x265.rb changed only in licence metadata.
  * GCC runtime: the wheel is scipy-1.18.1-cp313-cp313-macosx_14_0_arm64.whl (sha256
    ea324d9dd34c38bfb9bec8ca4d1b407db97dbb74029f566b8e322b1b6fe56fe6, locked in uv.lock). Its build workflow at tag v1.18.1
    builds that variant (Accelerate) with the runner's preinstalled gfortran-13, from Homebrew's gcc@13. The library files
    carry no GCC version string, but three scipy extension modules (for example linalg/_fblas) keep the compiler's library
    folders in their run paths: /opt/homebrew/Cellar/gcc@13/13.4.0/lib/gcc/13 (and two folders below it). That names GCC
    13.4.0 (Homebrew gcc@13 has been 13.4.0 since 2025-06-10). This is an inference from the run paths, not a version string
    inside libgfortran, libgcc_s or libquadmath. Homebrew builds gcc@13 from gcc-13.4.0.tar.xz plus the patch in gcc/patches.
  * Rubber Band: the library carries the RubberBandLiveShifter classes, which exist from release 4.0.0 on (v3.3.0 has no such
    header), and its library version 3.0.0 equals rubberband_dynamic_library_version of the 4.0.0 meson.build. 4.0.0 is
    the newest upstream release (checked 2026-10-10).

What is not in this archive
---------------------------
  * Nettle recipes: the Homebrew formulas of nettle 3.10.1 and 3.10.2 are not in this archive (they were not fetched).
    The sha256 values of the two tarballs are in MANIFEST.tsv and SHA256SUMS, and the Homebrew formula values are in
    THIRD_PARTY_NOTICES.md.
  * Homebrew itself, Apple's compiler and the macOS SDK (general-purpose tools and system libraries, not part of the work).
  * The other permissively licensed libraries of the opencv-python wheel (the MIT, BSD, Apache-2.0, zlib, ISC and similar
    ones listed in THIRD_PARTY_NOTICES.md). Their sources are public at their upstream projects; the written offer covers them.
  * The Python packages of uv.lock other than opencv-python: each with its source on PyPI (the exact files and hashes are in
    uv.lock); MySearch's own source is MIT-licensed and public.

Checking this archive
---------------------
  SHA256SUMS lists every file below this folder; MANIFEST.tsv adds, for each, its size, where it came from and the pin that
  was checked (a sha256, or a commit and tree id for the git archives).  cd into this folder and run: shasum -a 256 -c SHA256SUMS
EOF
record "README.txt" "generated by scripts/fetch_gpl_sources.sh" "generated"

# ------------------------------------------------------------------------------------------------ manifest and archive
{ printf 'path\tbytes\tsha256\tpin\tsource\n'; sort "$work/manifest.tsv"; } > "$root/MANIFEST.tsv"
( cd "$root" && find . -type f ! -name SHA256SUMS | sed 's|^\./||' | LC_ALL=C sort | while IFS= read -r f; do printf '%s  %s\n' "$(sha256_of "$f")" "$f"; done ) > "$root/SHA256SUMS"
( cd "$root" && shasum -a 256 -c SHA256SUMS >/dev/null ) || die "SHA256SUMS does not verify"

tarflags=""
for fl in --no-mac-metadata --no-xattrs; do
  if COPYFILE_DISABLE=1 tar $fl -cf /dev/null --files-from /dev/null 2>/dev/null; then tarflags="$tarflags $fl"; fi
done
archive="$out/$name.tar.gz"
# no owner names, no AppleDouble files, no local paths
# shellcheck disable=SC2086
COPYFILE_DISABLE=1 tar $tarflags --uid 0 --gid 0 --uname root --gname wheel -cf - -C "$out" "$name" | gzip -n -9 > "$archive.part"
mv "$archive.part" "$archive"
tar -tzf "$archive" >/dev/null || die "the archive does not read back"

say ""
say "Archive:  $archive"
abytes="$(wc -c < "$archive" | tr -d ' ')"
say "Size:     $abytes bytes ($(awk -v b="$abytes" 'BEGIN {printf "%.0f", b / 1000000}') MB)"
say "sha256:   $(sha256_of "$archive")"
say "Contents: $(tar -tzf "$archive" | grep -vc '/$') files; per-file pins in $name/MANIFEST.tsv"
say "Attach $name.tar.gz to the GitHub release next to MySearch-$ver.dmg. The folder $work can be removed by hand once the archive is made."
