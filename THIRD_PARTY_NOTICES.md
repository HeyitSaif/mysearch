# Third-party notices

MySearch's own code is MIT-licensed (see [LICENSE](LICENSE)). Other code and data keep their own licences, listed here.

## Main Python dependencies

Licences as declared in each installed package's metadata (the project's `.venv`, built from `uv.lock`).

| Package | Licence | Role |
|---|---|---|
| mlx, mlx-metal, mlx-lm, mlx-vlm | MIT | Apple MLX runtime; language and vision models on the GPU |
| mlx-audio | MIT | audio models |
| transformers | Apache-2.0 | model code and tokenizers |
| tokenizers, safetensors, huggingface_hub, hf-xet, sentencepiece | Apache-2.0 | model files and tokenization |
| sqlite-vec | MIT or Apache-2.0 (dual) | vector search inside SQLite |
| numpy | BSD-3-Clause (its bundled parts carry 0BSD, MIT, Zlib and CC0 notices) | arrays |
| pillow | MIT-CMU (HPND) | image decoding and rendering |
| opencv-python | Apache 2.0 for OpenCV (the wheel's own `LICENSE.txt`, for the packaging project, is MIT); the macOS arm64 wheel also carries 99 native libraries under their own licences, among them a **GPL** build of FFmpeg 7.1.1 with x264, x265, Xvid, vid.stab and Rubber Band: see "GPL components in the self-contained DMG" below | image handling (video frames) |
| regex | Apache-2.0 AND CNRI-Python | text matching |
| scipy | BSD-3-Clause (the macOS wheel also bundles libgcc_s and libgfortran, GPL-3.0-or-later with the GCC Runtime Library Exception 3.1, and libquadmath, LGPL-2.1-or-later: see "Native libraries inside wheels" below) | numerics |
| fastapi, starlette, uvicorn, httpx, pydantic | MIT or BSD-3-Clause | transitive (pulled in by other packages; the daemon itself is stdlib-only) |

## Notable transitive components

* **certifi** (MPL-2.0) and **tqdm** (MPL-2.0 AND MIT): used unmodified. MPL-2.0 applies per file, so changes to those
  files would have to stay under MPL-2.0 and be made available.
* **sounddevice** (MIT) bundles PortAudio, which is MIT-licensed.

## Model weights (not in this repository)

No model weights are in this repository or in any build. Each user downloads the models, and the download is governed by
the model's own licence:

* **EmbeddingGemma** (Google): Apache-2.0.
* **Qwen3** models (Qwen3-Reranker-0.6B, Qwen3-4B-Instruct-2507): Apache-2.0.
* **Whisper large-v3-turbo** (OpenAI): MIT (OpenAI's original); the MLX repack's model card says Apache-2.0.
* **Cloud models for Ask** are not downloaded. Their use is governed by the terms of the provider you choose.

Check the model card of each model you download before you use or redistribute it.

## Bundled code

### FinderFlow

Parts of the Mac app are adapted from FinderFlow, https://github.com/Gtarafdar/FinderFlow, tag v1.5.2:

* `app/Launchers.swift`: installed-app and CLI detection, the open-in helpers, shell and AppleScript quoting
  (`FinderFlow/QuickActionsToolbar.swift`, `FinderFlow/FolderCreationService.swift`).
* `app/FinderExtension/FinderSyncExtension.swift`: the Finder Sync extension (`FinderFlowExtension/FinderSyncExtension.swift`).
* `app/Tags.swift`: the colour tag tables, the toggle rule and the tag writer (`FinderFlow/FileItem.swift`,
  `FinderFlow/FolderCreationService.swift`).
* `app/FileOps.swift`: the unique-name rule (`FinderFlow/FolderCreationService.swift`, uniqueURL).
* `app/CodeEditorFile.swift`: the list of text and code extensions (`FinderFlow/CodeEditorView.swift`, `TextFileDetector`).
* `app/MarkdownViewWindow.swift` and `app/MarkdownViewRenderer.swift`: the Markdown reader and its renderer
  (`FinderFlow/MarkdownReaderView.swift`, `MarkdownToHTML`).

`app/CodeEditorWindow.swift` is FinderFlow-inspired: FinderFlow's code editor window is its behaviour reference.

The Mac app's command palette and dual panes follow ideas from **Shuffle** (ideas only, no code copied).

```
MIT License

Copyright (c) 2026 Gobinda Tarafdar

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## GPL components in the self-contained DMG

This section applies to a disk image built with `scripts/build_dmg.sh --bundle-runtime` (the self-contained DMG). A plain
build has no bundled runtime and contains none of the code described here. Everything below was checked on 2026-10-10
against the wheels locked in `uv.lock`.

**What is in it.** The macOS arm64 wheel of opencv-python 5.0.0.93 (pulled in by mlx-vlm, which reads video frames with
it) was built against the FFmpeg that Homebrew had installed on its maintainers' build machine, and carries that FFmpeg with
the libraries it loads: 99 files in `cv2/.dylibs`. That FFmpeg is a **GPL build**. The wheel's own `LICENSE-3RD-PARTY.txt`
prints the LGPL-2.1 for it and does not mention x264, x265, Xvid, vid.stab or Rubber Band; for this wheel that is wrong.
The last two rows are GCC runtime libraries of the scipy wheel, not of opencv-python.

| Component | Version | Licence | Where | How version and licence were established |
|---|---|---|---|---|
| FFmpeg: libavutil, libavcodec, libavformat, libavfilter, libavdevice, libswscale, libswresample, libpostproc | 7.1.1 (library versions 59.39.100, 61.19.101, 61.7.100, 10.4.100, 61.3.100, 8.3.100, 5.3.100, 58.3.100) | **GPL-3.0-or-later**: configured with `--enable-gpl --enable-version3` | `cv2/.dylibs/libav*`, `libsw*`, `libpostproc*` | libavutil prints "libavutil license: GPL version 3 or later" and "FFmpeg version 7.1.1"; all eight library versions equal the `version.h` files of the 7.1.1 release |
| x264 | r3108, commit `31e19f92f00c7003fa115047ce50978bc98c3a0d` | GPL-2.0-or-later (headers: version 2 "or (at your option) any later version"; a commercial licence is also sold) | `libx264.164.dylib`, loaded by libavcodec | the library holds the version string " r3108 31e19f9" and the banner "x264 - core %d%s - H.264/MPEG-4 AVC codec" (core 164 is in the file name); the commit has 3108 commits in its history |
| x265 | 4.1, one commit after the tag (changeset `1d117be`) | GPL-2.0-or-later (same wording; commercial licence also offered) | `libx265.215.dylib`, loaded by libavcodec | the library reports "4.1+1-1d117be", which is what `x265Version.txt` in `x265_4.1.tar.gz` yields |
| Xvid (xvidcore) | 1.3.7 | GPL-2.0-or-later | **statically linked into libavcodec**; no file of its own | libavcodec contains "xvid-1.3.7" (from `src/xvid.c`) and "XviD%04d%c"; there is no libxvidcore file and no undefined xvid symbol |
| vid.stab | 1.1.1 | GPL-2.0-or-later | `libvidstab.1.2.dylib`, loaded by libavfilter | a vid.stab 1.1.1 build (Apple clang, arm64) exports exactly the 183 symbols of this library; a 1.1.2 build exports a different set |
| Rubber Band | 4.0.0 | GPL-2.0-or-later (a commercial licence is also offered) | `librubberband.3.dylib`, loaded by libavfilter | it has the `RubberBandLiveShifter` classes, which 3.3.0 lacks, and library version 3.0.0, which is what the 4.0.0 `meson.build` sets; 4.0.0 is the newest release |
| GCC runtime: libgcc_s, libgfortran | 13.4.0 (inferred, see below) | GPL-3.0-or-later with the GCC Runtime Library Exception 3.1 | `scipy/.dylibs/libgcc_s.1.1.dylib`, `libgfortran.5.dylib` (scipy wheel, not opencv-python) | scipy's `LICENSE.txt`; the version is not a string in the libraries but is named by the run paths of three scipy extension modules (`/opt/homebrew/Cellar/gcc@13/13.4.0/lib/gcc/13`) |
| GCC runtime: libquadmath | 13.4.0 (inferred, same evidence) | LGPL-2.1-or-later | `scipy/.dylibs/libquadmath.0.dylib` | scipy's `LICENSE.txt` |

The licence column follows the file headers of the sources (read in the upstream tarballs). FFmpeg's own `LICENSE.md`
(kept as `Licenses/FFmpeg-7.1.1-LICENSE.md`) says libx264, libx265, libxvid, libvidstab and librubberband are GPL libraries
that make a build GPL, and that `--enable-version3` raises it to version 3, which FFmpeg needs for libvmaf, OpenCORE AMR, gmp
and aribb24. Homebrew's formulas for x264 and x265 said "GPL-2.0-only" until 2025-06-27 and "GPL-2.0-or-later" since; the
headers are the authority. The licence texts are in `Contents/Resources/Licenses/` (`GPL-3.0.txt`, `GPL-2.0.txt`).

**Not modified by MySearch; how it was built.** MySearch does not change any of this code. Its build rewrites the install
names and run paths in the Mach-O load commands of these files (`scripts/bundle_tools.py fix`) and signs each file again
(`scripts/bundle_runtime.sh sign`); the compiled code is not edited. The wheel's maintainers built the libraries with
Homebrew: the configure line inside libavcodec is `--prefix=/opt/homebrew/Cellar/ffmpeg/7.1.1_3 --enable-shared ...
--enable-gpl --enable-version3 ...`, the same arguments as Homebrew's `ffmpeg` formula, revision 3. That formula applies two
patches to FFmpeg 7.1.1 (support for svt-av1 3.0; `av_stream_get_first_dts` for QtWebEngine), and Homebrew's x265 formula
applies two CMake patches (policy lines only). All four patches are in the source archive. The libraries are linked
dynamically, with one exception: that build links Xvid statically into libavcodec. `cv2/cv2.abi3.so` (OpenCV 5.0.0)
loads libavcodec, libavformat, libavutil, libswscale and libavdevice directly (`@loader_path/.dylibs`). The libraries are
separate files, so a user can replace one with a modified build and sign the app again (`codesign --force --sign -` on the
app, which ends a Developer ID signature, as any change to a signed app does).

**Why this FFmpeg and not an LGPL one.** The wheel is the release of opencv-python tag 93 (commit
`b83046cda41133f1bf2e73e99dba16a1248f103a`; its OpenCV submodule is commit `40738fb16ceddb5fb3fea747585f7ce6abb0605b`, OpenCV
tag 5.0.0). Its macOS arm64 job, `.github/workflows/build_wheels_macos_m1.yml`, runs `setup.py bdist_wheel` and `delocate-wheel`
with `MACOSX_DEPLOYMENT_TARGET=13.0` (the wheel tag `macosx_13_0`) and never uses `travis_config.sh`, whose
`generate_ffmpeg_formula` would have removed `--enable-gpl` and the GPL libraries. So the build machine's stock Homebrew FFmpeg
was linked and copied in. `cv2.getBuildInformation()` reports `Darwin 22.6.0 arm64` (macOS 13) and the same FFmpeg versions.
The versions above come from the libraries themselves, not from a Homebrew snapshot: the wheel's other libraries (glib 2.84.3,
OpenEXR 3.3.4, gnutls 3.8.9) are not those of one day's homebrew-core.

**The combined work, and the terms.** `cv2.abi3.so` is Apache-2.0 code that loads these GPL libraries; together they form one
combined work, and this DMG conveys that combined work under the GNU General Public License, version 3 or later. Apache-2.0
and MySearch's own MIT licence are both compatible with GPL-3.0, so MySearch's source code remains MIT-licensed and
mlx-vlm (MIT), which calls cv2, is unaffected. The other Python packages in the bundle are separate works with their own
licences, listed in the sections below; this notice does not claim the DMG as a whole is GPL-licensed.

**Source.** `MySearch-<version>-sources.tar.gz`, attached to the same GitHub release as `MySearch-<version>.dmg`
(https://github.com/HeyitSaif/mysearch/releases; the release notes name it), holds the source of the six GPL components of
the opencv-python wheel, of `cv2.abi3.so` and of the GCC runtime libraries of the scipy wheel; the source of the sixteen LGPL
and MPL-2.0 libraries of the two wheels whose version is established and the source of Nettle, both 3.10.1 and 3.10.2 with their
signature files (see "LGPL and MPL-2.0 libraries in the same wheel" below); the build scripts: FFmpeg 7.1.1 with its two patches; x264 at the commit above; x265 4.1
with its two patches; Xvid 1.3.7; vid.stab 1.1.1; Rubber Band 4.0.0; the six Homebrew formulas that built them (homebrew-core
commit `ecfd2879128b7a2f4b0d0bf53969426263c28584`); the OpenCV 5.0.0 source and the opencv-python tag 93 scripts; GCC 13.4.0
with Homebrew's patch for it and the `gcc@13` formula; a `README.txt` that records how each version was established;
the licence texts of `licenses/`, `MANIFEST.tsv` and `SHA256SUMS`. It is made by `scripts/fetch_gpl_sources.sh`, which downloads each item from its upstream
location and refuses one whose sha256 or commit differs from the pin. The upstream locations of the GPL components:

* FFmpeg: https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz
* x264: https://code.videolan.org/videolan/x264.git, commit `31e19f92f00c7003fa115047ce50978bc98c3a0d`
* x265: https://bitbucket.org/multicoreware/x265_git/downloads/x265_4.1.tar.gz
* Xvid: https://downloads.xvid.com/downloads/xvidcore-1.3.7.tar.bz2
* vid.stab: https://github.com/georgmartius/vid.stab/archive/refs/tags/v1.1.1.tar.gz
* Rubber Band: https://breakfastquay.com/files/releases/rubberband-4.0.0.tar.bz2
* OpenCV: https://github.com/opencv/opencv.git, tag 5.0.0; opencv-python: https://github.com/opencv/opencv-python.git, tag 93
* Homebrew formulas: https://github.com/Homebrew/homebrew-core/tree/ecfd2879128b7a2f4b0d0bf53969426263c28584/Formula
* GCC: https://ftp.gnu.org/gnu/gcc/gcc-13.4.0/gcc-13.4.0.tar.xz, and Homebrew's patch
  https://github.com/Homebrew/homebrew-core/blob/d17f3939dc55c4a42a6e5833a8353d0948bbd416/Patches/gcc/gcc-13.4.0.diff

**Written offer.** As an extra assurance beside the archive, the maintainer of MySearch (HeyitSaif,
https://github.com/HeyitSaif/mysearch) will give anyone who has a copy of the self-contained DMG the source of the GPL
components above and of every other library in `cv2/.dylibs` and `scipy/.dylibs`: the permissively licensed ones, whose sources are public (the LGPL and MPL-2.0 libraries, nettle included,
are in the archive). The offer is made as a download, free of charge, or on a physical medium for the cost of making and sending it. This
offer is valid for three years from the date of the release that carried that copy of the DMG, and for as long as that
version is offered for download. To use it, open an issue at https://github.com/HeyitSaif/mysearch/issues; the archive
named above is the normal answer.

**Check it yourself.** In the app, the libraries are in `Contents/Resources/runtime/lib/python3.13/site-packages/cv2/.dylibs`:

```
strings -a libavutil.59.39.100.dylib | grep -E 'libavutil license|FFmpeg version|--enable-gpl'
strings -a libx264.164.dylib  | grep -E 'x264 - core| r3108'
strings -a libx265.215.dylib  | grep -E '^4\.1\+'
strings -a libavcodec.61.19.101.dylib | grep -E 'xvid-1|XviD%04d'
otool -L libavcodec.61.19.101.dylib | grep -c -E 'libx264|libx265'          # 2: both are loaded as separate files
```

The GCC version is in the run paths of the scipy wheel as downloaded (`otool -l scipy/linalg/_fblas.cpython-313-darwin.so |
grep -A2 LC_RPATH`). The DMG build deletes those run paths (see "What the build changes afterwards"), so the bundle itself no
longer shows it.

### LGPL and MPL-2.0 libraries in the same wheel

Checked on 2026-10-11 against the wheel's own files, the Homebrew formulas and the upstream tarballs. Each version is established
by the library itself: a function that reports it, a string in the file, or a build path in the binary. The licence is the one
in the upstream tarball's licence file (the source headers where they differ). Each library's source is in the sources
archive, folder `lgpl/<library>/`, with its Homebrew recipe in `homebrew-formulas/` and any patch; the archive's `SHA256SUMS`
gives every file's sha256. The written offer above stays in force as well. The licences follow the upstream files, not
Homebrew's declarations, where the two differ: aribb24 (its source headers say 2.1 or later, its COPYING file holds the
LGPL-3.0 text, Homebrew says LGPL-3.0-only), libunistring (LGPL-3.0-or-later or GPL-2.0-or-later, as its README says; Homebrew
said GPL-2.0-only), and graphite2 (its COPYING names the MPL without a version; Homebrew said MPL-1.1 or later).

| Library file | Licence (upstream file) | Version | Evidence | Source in the archive |
|---|---|---|---|---|
| `libbluray.2` | LGPL-2.1-or-later | 1.3.4 | `bd_get_version` | `lgpl/libbluray/libbluray-1.3.4.tar.bz2` |
| `libgnutls.30` | LGPL-2.1-or-later | 3.8.9 | `gnutls_check_version`; Cellar path `gnutls/3.8.9` | `lgpl/gnutls/gnutls-3.8.9.tar.xz` |
| `libgmp.10` | LGPL-3.0-or-later or GPL-2.0-or-later | 6.3.0 | `__gmp_version` | `lgpl/gmp/gmp-6.3.0.tar.xz` |
| `libidn2.0` | GPL-2.0-or-later or LGPL-3.0-or-later (dual, as its README says) | 2.3.8 | `idn2_check_version` | `lgpl/libidn2/libidn2-2.3.8.tar.gz` |
| `libunistring.5` | LGPL-3.0-or-later or GPL-2.0-or-later (its README: "LGPLv3+ or GPLv2+") | 1.3 | `_libunistring_version` = 0x10300; `version.sh` | `lgpl/libunistring/libunistring-1.3.tar.gz` |
| `libintl.8` (gettext 0.25) | LGPL-2.1-or-later (`intl/COPYING.LIB`) | 0.25.0 | `libintl_version` = 0x1900; `LIBINTL_VERSION 0x001900` in the source; Cellar path `gettext/0.25` | `lgpl/gettext/gettext-0.25.tar.gz` (the whole gettext project) |
| `libtasn1.6` | LGPL-2.1-or-later | 4.20.0 | `asn1_check_version` | `lgpl/libtasn1/libtasn1-4.20.0.tar.gz` |
| `libmp3lame.0` | LGPL-2.0-or-later | 3.100 | `get_lame_version`; `LAME_MINOR_VERSION 100` | `lgpl/lame/lame-3.100.tar.gz` |
| `libsoxr.0.1.2` | LGPL-2.1-or-later | 0.1.3 | `soxr_version` = `libsoxr-0.1.3`; Homebrew patch `arm64_defines.patch` | `lgpl/soxr/soxr-0.1.3-Source.tar.xz`, `lgpl/soxr/patches/` |
| `libssh.4.10.1` | LGPL-2.1-or-later | 0.11.1 | `ssh_version(0)`; build path `libssh-0.11.1`; compiled 2024-09-01 (build path `libssh-20240901`) | `lgpl/libssh/libssh-0.11.1.tar.xz` |
| `libaribb24.0` | LGPL-2.1-or-later per the source headers (see above) | 1.0.4 | formula history only: 1.0.4 in every revision since 2023-08; the binary has no version string; its 15 exported `arib_*` functions are all declared in the 1.0.4 source | `lgpl/aribb24/aribb24-v1.0.4.tar.bz2` |
| `libfribidi.0` | LGPL-2.1-or-later | 1.0.16 | `fribidi_version_info` | `lgpl/fribidi/fribidi-1.0.16.tar.xz` |
| `libglib-2.0.0` | LGPL-2.1-or-later | 2.84.3 | `glib_major_version`, `glib_minor_version`, `glib_micro_version`; build path `glib/2.84.3`; Homebrew patch `hardcoded-paths.diff` | `lgpl/glib/glib-2.84.3.tar.xz`, `lgpl/glib/patches/` |
| `libgraphite2.3.2.1` | LGPL-2.1-or-later (COPYING: "version 2.1 ... or (at your option) any later version"; it also offers the MPL, version not stated, and GPL-2.0-or-later) | 1.3.14 | `gr_engine_version` | `lgpl/graphite2/graphite2-1.3.14.tgz` |
| `libzmq.5` | MPL-2.0 | 4.3.5 | `zmq_version`; the binary exports no draft-API function, so it was built without `--enable-drafts` (recipe: the formula of 2025-03-20) | `lgpl/zeromq/zeromq-4.3.5.tar.gz` |
| `libsrt.1.5.4` | MPL-2.0 | 1.5.4 | `srt_getversion`; build path `srt-1.5.4`; compiled 2024-11-11 (build path `srt-20241111`) | `lgpl/srt/srt-1.5.4.tar.gz` |
| `libnettle.8.10`, `libhogweed.6.10` (one library, nettle) | GPL-2.0-or-later or LGPL-3.0-or-later | 3.10 as reported; both 3.10.1 and 3.10.2 are in the archive (see below) | `nettle_version_major` and `nettle_version_minor` give 3.10 only | `lgpl/nettle/nettle-3.10.1.tar.gz`, `lgpl/nettle/nettle-3.10.2.tar.gz` and their `.sig` files |

The MPL-2.0 licence text is in the zeromq and srt tarballs (their `LICENSE` files). The canonical text is also `licenses/MPL-2.0.txt`, which a self-contained build copies to `Licenses/` with the other licence texts.

**Nettle: the binary reports version 3.10; both 3.10.1 and 3.10.2 sources are included because the exact patch release could not be determined from the binary.** The binaries report 3.10 and nothing more. The
Homebrew formula was 3.10.1 until 2025-06-26 and 3.10.2 from then on, and the FFmpeg window (2025-05-27 to 2025-08-14)
contains both. The two tarballs differ only in `getopt.c`, `getopt.h` and two PowerPC assembly files, which the arm64 library
does not use (a reading of the diff, not a build comparison). Each candidate's sha256 is in its Homebrew formula (the formulas were not fetched again): 3.10.1
`b0fcdd7fc0cdea6e80dcf1dd85ba794af0d5b4a57e26397eee3bc193272d9132`, 3.10.2
`fe9ff51cb1f2abb5e65a6b8c10a92da0ab5ab6eaf26e7fc2b675c45f1fb519b5`. On 2026-10-11 both tarballs were downloaded from
ftp.gnu.org and their sha256 values matched these. The archive holds both tarballs and their `.sig` files. The signatures
name key ID F3599FF828C67298, which was not in the keyring used for this check, so they are not verified here: check them
with gpg and the Nettle release key. No Nettle recipe is in the archive.

**What points to 3.10.1, and why that is not proof.** The other libraries' version constraints fit one window if all of the
wheel's libraries came from one `brew install`: glib 2.84.3 exists in the formula from 2025-06-13; libssh 0.11.1 was replaced
in the formula on 2025-06-24; gettext 0.25, zeromq without drafts and gnutls 3.8.9 all still held before their own changes of
2025-07-02, 2025-07-04 and 2025-07-09. So the install would fall between 2025-06-13 and 2025-06-23, before the formula of
nettle 3.10.2 (2025-06-26), which points to 3.10.1. This assumes one install, which is not checked: a bottle's compile date
(libssh 2024-09-01, srt 2024-11-11) shows when CI built it, and an unchanged bottle is poured again on a later install, so it
says nothing about the install date either way. Both tarballs are shipped, so the archive does not depend on that choice.
Comparing the binary's code with the arm64 bottles of 3.10.1 and 3.10.2 would settle the question; the bottles are
binaries, so that comparison is the owner's call and is outside the source download.

**libquadmath** (LGPL-2.1-or-later, scipy) has its source inside the GCC 13.4.0 tarball of the GPL section; its version is
the inferred GCC version above.

**Pins without a version the binary states.** aribb24 1.0.4 rests on the Homebrew formula history alone. The formula held
1.0.4 in every revision since 2023-08, so no formula revision inside the FFmpeg window differs. That is evidence, not a
binary check. The zeromq recipe is the formula of 2025-03-20: the binary exports none of the draft-API functions of the 4.3.5 header
(those of its `ZMQ_BUILD_DRAFT_API` block), so the build predates `zeromq: enable drafts` (2025-07-04).

**Patches in the archive.** soxr's `arm64_defines.patch` (in the libsoxr formula at every revision from 2023-09-13 to
2026-09-10) and GLib's `hardcoded-paths.diff` (in the GLib 2.84.3 formula of 2025-06-13) apply with `patch -p1` to their
tarballs. The srt patch `7962936829` is not in the archive: it entered the srt formula on 2025-05-12, after the srt bottle of
this wheel was built (2024-11-11).

**Tags.** aribb24 v1.0.4 is commit `a41b904d66ed991ace32a9827f629db3d438b47b`, and srt v1.5.4 is commit
`a8c6b65520f814c5bd8f801be48c33ceece7c4a6`. Their tarballs come from the hosts' generated archive endpoints; the sha256
pins are what the script checks, and the dry run checks the tag commits.

### The remaining libraries of the wheel

The other 69 files of `cv2/.dylibs` are permissively licensed, as Homebrew declares them (not checked against each
project). BSD-2-Clause: libaom, libdav1d, librav1e, libavif, libopenjp2, librist, libsamplerate, libarchive, libleptonica,
liblz4 (BSD-2-Clause-Patent: libvmaf). BSD-3-Clause: libSvtAv1Enc, OpenEXR (libOpenEXR, libOpenEXRCore, libIex, libIlmThread),
Imath, libjxl (libjxl, libjxl_cms, libjxl_threads), libogg, libvorbis, libvorbisenc, libtheoradec, libtheoraenc, libspeex,
libvpx, libwebp, libwebpmux, libsharpyuv, libsnappy, libp11-kit, libpcre2-8, libopus. MIT: libX11, libXau, libXdmcp, libxcb
(libxcb, libxcb-shape, libxcb-shm, libxcb-xfixes), libbrotli (common, dec, enc), libcjson, libdeflate, libfontconfig, libgif,
libharfbuzz, liblcms2. Apache-2.0: libhwy, libmbedcrypto, libopencore-amrnb, libopencore-amrwb, libtesseract, libcrypto and
libssl (OpenSSL 3). ISC: libass, libsodium. Zlib: libSDL2, libunibreak. Others: libfreetype (FTL), libpng16 (libpng-2.0),
libtiff (the libtiff licence), libjpeg (IJG, Zlib and BSD-3-Clause), libzstd (BSD-3-Clause, with GPL-2.0-only as an alternative
for part of it), liblzma (0BSD for the library), libb2 (CC0-1.0), libzimg (WTFPL).

## Bundled runtime (DMG built with --bundle-runtime)

A DMG built with `scripts/build_dmg.sh --bundle-runtime` carries its own Python inside
`MySearch.app/Contents/Resources/runtime`. It is a python-build-standalone CPython (fetched by uv; 3.13 by default, and
`uv.lock` allows 3.12 and 3.13) plus the non-development packages of `uv.lock` for macOS on Apple silicon, installed with
`uv sync --frozen --no-dev --no-install-project`. The `mse` package is copied next to it. Every build of the app, bundled
or not, carries `LICENSE` and this file in `Contents/Resources`; a bundled build also carries the licence texts of the
GPL/LGPL code described above in `Contents/Resources/Licenses/`. They are copied again on every build, so a rebuild into
an existing folder never keeps stale ones.

**How this list was made.** Versions are the ones pinned in `uv.lock`. Licences are read from the `METADATA` files of the
wheels installed from that lock in the project's development environment (macOS, Apple silicon, CPython 3.13). The
installed version equals the locked version for all 59 packages. The table below was made in that environment, not in a
built bundle. The facts in "GPL components in the self-contained DMG" were also checked in a built bundle on 2026-10-10: its
`cv2/.dylibs` has the same 99 files, the strings quoted there are byte-identical to those of the development environment's
files, and `Licenses/` and this file were present (`scripts/bundle_runtime.sh check` compares them with the repository).

* A plain entry in the table is the package's `License-Expression` field.
* "(License header)" is its `License` field, used when there is no expression and the field is a short licence name.
* "(classifier)" is its `License :: OSI Approved ::` classifier, used when neither of the two fields can be used (for
  scipy the `License` field holds a full licence text; the name comes from the classifier).
* Strings are quoted as the packages give them. No package lacks a declaration, so none is marked "unknown".

**Licence texts.** A wheel keeps its licence texts in its `.dist-info` directory (in `licenses/` or at its top level).
The copy step into the app leaves out only `__pycache__` folders, `*.pyc`, `_virtualenv.*` and `_editable_impl_*`. A dry
run of that filter over the development environment dropped nothing from any `.dist-info` or `.dylibs` folder, so the
texts and the native libraries are copied as they are. The build syncs its own cache environment from `uv.lock`, not the
development environment; the development environment matches the lock exactly (59 of 59 versions), so it stands in for
it. **Exception:** sentencepiece, sqlite-vec and tokenizers ship no licence text in their wheels (the last column below).

**What the build changes afterwards.** These steps do not target licence files, but they do change some bundled files:

* `bundle_tools.py fix` rewrites the load commands of the Mach-O files (`.so`, `.dylib`): it deletes absolute run paths and
  turns placeholder install names such as `/DLC/...` into `@rpath/...` or `@loader_path/...` names. The code in those files
  is not edited.
* The signing step signs every Mach-O file of the runtime again.
* `bundle_tools.py scrub` replaces the build machine's Python folder path in text files that contain it.
* `compileall` adds `.pyc` files.

For the interpreter's own tree the copy step leaves out headers, man pages, Tcl/Tk and `tkinter`, IDLE, the standard
library's test suite, `pip` and `ensurepip`, `turtledemo` and `libpython`. Tcl/Tk is therefore not distributed.

| Package | Version | Licence (as declared) | Licence file in .dist-info |
|---|---|---|---|
| annotated-doc | 0.0.5 | MIT | yes |
| annotated-types | 0.8.0 | MIT | yes |
| anyio | 4.15.1 | MIT | yes |
| certifi | 2026.7.22 | MPL-2.0 (License header) | yes |
| cffi | 2.1.1 | MIT-0 | yes |
| charset-normalizer | 3.5.2 | MIT (License header) | yes |
| click | 8.5.0 | BSD-3-Clause | yes |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | yes |
| fastapi | 0.142.2 | MIT | yes |
| filelock | 4.0.12 | MIT | yes |
| fsspec | 2026.9.0 | BSD-3-Clause | yes |
| h11 | 0.16.0 | MIT (License header) | yes |
| hf-xet | 1.7.0 | Apache-2.0 | yes |
| httpcore | 1.0.9 | BSD-3-Clause | yes |
| httpx | 0.28.1 | BSD-3-Clause (License header) | yes |
| huggingface-hub | 1.33.0 | Apache-2.0 (License header) | yes |
| idna | 3.20 | BSD-3-Clause | yes |
| jinja2 | 3.1.6 | BSD License (classifier) | yes |
| llguidance | 1.9.1 | MIT | yes |
| markdown-it-py | 4.2.0 | MIT License (classifier) | yes |
| markupsafe | 3.0.4 | BSD-3-Clause | yes |
| mdurl | 0.1.2 | MIT License (classifier) | yes |
| miniaudio | 1.71 | MIT (License header) | yes |
| mlx | 0.32.3 | MIT (License header) | yes |
| mlx-audio | 0.5.8 | MIT | yes |
| mlx-lm | 0.32.0 | MIT | yes |
| mlx-metal | 0.32.3 | MIT (License header) | yes |
| mlx-vlm | 0.7.7 (git `fec3f50`) | MIT (License header) | yes |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | yes |
| opencv-python | 5.0.0.93 | Apache 2.0 (License header) | yes |
| opentelemetry-api | 1.45.1 | Apache-2.0 | yes |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | yes |
| pillow | 12.3.0 | MIT-CMU | yes |
| protobuf | 7.36.2 | 3-Clause BSD License (License header) | yes |
| pycparser | 3.0 | BSD-3-Clause | yes |
| pydantic | 2.13.5 | MIT | yes |
| pydantic-core | 2.46.5 | MIT | yes |
| pygments | 2.21.0 | BSD-2-Clause | yes |
| python-multipart | 0.0.32 | Apache-2.0 | yes |
| pyyaml | 6.0.3 | MIT (License header) | yes |
| regex | 2026.9.29 | Apache-2.0 AND CNRI-Python | yes |
| requests | 2.34.2 | Apache-2.0 (License header) | yes |
| rich | 15.0.0 | MIT (License header) | yes |
| safetensors | 0.8.0 | Apache Software License (classifier) | yes |
| scipy | 1.18.1 | BSD License (classifier) | yes |
| sentencepiece | 0.2.2 | Apache-2.0 | no |
| shellingham | 1.5.4 | ISC License (License header) | yes |
| sounddevice | 0.5.6 | MIT | yes |
| sqlite-vec | 0.1.9 | MIT License, Apache License, Version 2.0 (License header) | no |
| starlette | 1.7.0 | BSD-3-Clause | yes |
| tokenizers | 0.23.2 | Apache Software License (classifier) | no |
| tqdm | 4.70.1 | MPL-2.0 AND MIT (License header) | yes |
| transformers | 5.19.0 | Apache 2.0 License (License header) | yes |
| typer | 0.27.3 | MIT | yes |
| typing-extensions | 4.16.0 | PSF-2.0 | yes |
| typing-inspection | 0.4.4 | MIT | yes |
| urllib3 | 2.8.0 | MIT | yes |
| uvicorn | 0.54.0 | BSD-3-Clause | yes |
| websockets | 17.2 | BSD-3-Clause | yes |

### Unknown or not verified

* **No package is unknown.** All 59 declare a licence: 35 by `License-Expression`, 18 by the `License` field, 6 by
  classifier.
* **Classifier only.** jinja2 (BSD License), markdown-it-py (MIT License), mdurl (MIT License), safetensors (Apache Software
  License), scipy (BSD License) and tokenizers (Apache Software License) declare nothing else. A classifier does not give
  the Apache version or the number of BSD clauses. scipy's licence text is a three-clause BSD notice, read in the wheel;
  for the other five the exact variant is not verified here.
* **No licence text in the wheel:** sentencepiece (declares Apache-2.0), sqlite-vec and tokenizers. The tokenizers
  `.dist-info` holds only a CycloneDX SBOM of its Rust crates. The app therefore carries no licence text for these three.
  The standard texts are at https://www.apache.org/licenses/LICENSE-2.0 and https://opensource.org/licenses/MIT.
* **sqlite-vec:** the metadata reads "MIT License, Apache License, Version 2.0" and does not say whether the two are
  alternatives or both apply. The "dual" wording in the main table above is not stated in the installed metadata: to be
  confirmed.
* **regex:** declares "Apache-2.0 AND CNRI-Python". Its `LICENSE.txt` says the `re`-derived parts are "licensed under CNRI's
  Python 1.6 license" and does not include that text. The Free Software Foundation lists the licences of Python 1.6b1
  through 2.1 as incompatible with the GNU GPL (governing law of Virginia). regex does not load or link any of the GPL
  libraries; it is a separate work in the bundle, which this notice does not call GPL-licensed. Whether its CNRI part has
  consequences for conveying the DMG alongside the GPL combined work is not verified here.
* **pillow:** the "(HPND)" note in the main table is not in Pillow's metadata or licence file, which name MIT-CMU.
* **opencv-python:** the `License` field says "Apache 2.0". The `LICENSE.txt` in its `.dist-info` (identical to
  `cv2/LICENSE.txt`) is an MIT licence, that of the opencv-python packaging project. OpenCV itself is Apache License 2.0
  according to `LICENSE-3RD-PARTY.txt`, which sits in the same place. FFmpeg and the other bundled libraries are covered
  in "GPL components in the self-contained DMG".
* **Rust extension modules** (cryptography, hf-xet, llguidance, pydantic-core, safetensors, tokenizers) include Rust crates
  statically. cryptography, hf-xet, pydantic-core, safetensors and tokenizers ship CycloneDX SBOMs in
  `.dist-info/sboms/` (`*.cyclonedx.json`) that give a licence for every listed crate (mostly MIT, Apache-2.0 or both).
  The hf-xet SBOM includes two MPL-2.0 crates (colored and option-ext); MySearch does not modify them. llguidance ships
  no SBOM, so the licences of its crates are not verified.
* **cryptography** also embeds OpenSSL statically. The binary (`hazmat/bindings/_rust.abi3.so`) reports "OpenSSL 4.0.3 29 Sep
  2026" and the wheel's `sbom.json` records the same version without a licence entry. OpenSSL's `LICENSE.txt` at the upstream
  tag `openssl-4.0.3` is the Apache License 2.0 (read 2026-10-10), which is compatible with GPL-3.0; the wheel ships that text
  as `LICENSE.APACHE` in its `.dist-info`.

### Native libraries inside wheels

The lists below are those of the cp313 macOS arm64 wheels. No wheel bundles libc++; the extension modules load the system
copy in `/usr/lib`.

* **opencv-python** (`cv2/cv2.abi3.so` and 99 libraries in `cv2/.dylibs`). Sources:
  https://github.com/opencv/opencv-python and https://github.com/opencv/opencv. The licences of the 99 libraries are set out in
  "GPL components in the self-contained DMG" above: six GPL components (FFmpeg and five others, one of them linked
  statically into libavcodec), 18 files of 17 LGPL or MPL-2.0 libraries (libnettle and libhogweed are two files of one library) and 69 permissively licensed files.
  * The wheel's own `LICENSE-3RD-PARTY.txt` says FFmpeg is redistributed, prints the LGPL-2.1, and names libbluray, libgnutls,
    libnettle, libhogweed, libintl, libmp3lame, libp11, librtmp (not in this wheel), libsoxr and libtasn1. It does not mention
    x264, x265, Xvid, vid.stab or Rubber Band, and says nothing about many of the other libraries.
  * Versions of the FFmpeg libraries, from the file names (and equal to the FFmpeg 7.1.1 release): libavcodec 61.19.101,
    libavformat 61.7.100, libavutil 59.39.100, libavdevice 61.3.100, libavfilter 10.4.100, libswscale 8.3.100, libswresample
    5.3.100, libpostproc 58.3.100.
  * They are dynamically linked: `cv2.abi3.so` loads them through `@loader_path/.dylibs/`, and libavcodec loads libx264, libx265
    and its other codec libraries the same way (`otool -L`); Xvid is the exception, compiled into libavcodec. MySearch does
    not edit their code. The DMG build rewrites their install names and run paths and signs them again, as described below.
  * opencv-python is pulled in by mlx-vlm (`uv.lock`), which uses `cv2.VideoCapture` to read video frames. The `mse`
    package itself does not import cv2.
* **pillow** (`PIL/.dylibs`, 18 libraries). Pillow's licence file and its CycloneDX SBOM list the licences of the bundled
  libraries, for example FreeType (FTL), HarfBuzz (MIT), libjpeg-turbo (IJG and BSD-3-Clause), libtiff (the libtiff
  licence), libwebp (BSD-3-Clause), OpenJPEG (BSD-2-Clause), libavif (BSD-2-Clause), Little CMS 2 (MIT), libxcb (X11) and
  zlib (Zlib). The SBOM also lists libimagequant (GPL-3.0-or-later) and FriBiDi (LGPL-2.1-or-later) as optional
  components. Neither is in this wheel: there is no libimagequant or libfribidi file in `PIL/.dylibs`, and `_imaging`
  contains no `liq_` symbols. libXau is in neither the licence file nor the SBOM: to be confirmed.
* **scipy** (`scipy/.dylibs`: libgcc_s.1.1, libgfortran.5, libquadmath.0). scipy's own `LICENSE.txt` documents them:
  libgfortran and libgcc as GPL-3.0-or-later WITH GCC-exception-3.1, libquadmath as LGPL-2.1-or-later. The texts are in
  `Licenses/` (`GPL-3.0.txt`, `GCC-Runtime-Library-Exception-3.1.txt`, `LGPL-2.1.txt`). The three files are redistributed
  unmodified, as scipy's wheel ships them (the DMG build changes only their install names and signs them again). Only one
  scipy extension, `scipy/odr/__odrpack` (the ODRPACK Fortran code), loads libgfortran; libgfortran loads libquadmath and
  libgcc_s. **Source version: GCC 13.4.0, inferred, not read from the libraries.** They carry no GCC version string. The wheel
  is `scipy-1.18.1-cp313-cp313-macosx_14_0_arm64`; scipy's tag v1.18.1 (`.github/workflows/wheels.yml`) builds that variant
  (Accelerate) with the build machine's preinstalled `gfortran-13` (Homebrew's `gcc@13`) and copies the runtime libraries
  into the wheel with `delocate`. Three scipy extension modules (for example `linalg/_fblas`) keep the compiler's library
  folders in their run paths in the wheel as downloaded: `/opt/homebrew/Cellar/gcc@13/13.4.0/lib/gcc/13`, which is where
  Homebrew keeps `libgfortran.5.dylib`, and two folders below it. That names version 13.4.0, the only version Homebrew's
  `gcc@13` has had since 2025-06-10. Homebrew builds it from `gcc-13.4.0.tar.xz` plus one patch (the Darwin maintainer's
  branch with Apple Silicon support). The source archive holds both and the formula.
* **numpy:** 19 extension modules and no `.dylibs` folder. Its `LICENSE.txt` and the files it lists cover the bundled parts.
* **mlx** and **mlx-metal:** `mlx/lib/libmlx.dylib`, `libjaccl.dylib` and `mlx.metallib`; both declare MIT.
* **sqlite-vec:** `sqlite_vec/vec0.dylib`, a loadable SQLite extension, with no licence file (see above).
* **sounddevice:** `_sounddevice_data/portaudio-binaries/libportaudio.dylib` (x86_64 and arm64). The README in that folder
  says "PortAudio by Ross Bencina and Phil Burk, MIT License". The same folder also holds six Windows DLLs. The app
  copies them along; macOS cannot load them. Three are built with ASIO support (`*-asio.dll`), and the README names
  Steinberg Media Technologies GmbH for the ASIO interface without stating a licence for those builds: to be confirmed.

### CPython

CPython is licensed under the Python Software Foundation License, version 2. The runtime keeps its licence text as
`lib/python3.X/LICENSE.txt` (the copy step does not exclude it). That wording was read in a python-build-standalone
3.13.15 tree installed by uv on the build machine, because this repository holds no built runtime. The file says Python
software and documentation are licensed under the PSF License Version 2, and that examples, recipes and other code in the
documentation are dual licensed under it and the Zero-Clause BSD licence. The exact Python version of a build is recorded
in `runtime/RUNTIME.json` inside the app.

The python-build-standalone distribution builds in further third-party components, and its own distribution lists their
licences. That list is not in this repository. The tree checked has no `PYTHON.json` and no licence list for the
interpreter's components; its only other licence files belong to pip, which the bundle leaves out, and its `share/` holds
only man pages. Those component licences are therefore **not verified** here.

### Packages excluded from the macOS bundle

`uv.lock` names 61 packages. 59 are bundled. The other two are:

* **mysearch** 0.1.3, this project (MIT). It is not installed into the runtime (`--no-install-project`); the `mse` package is
  copied separately. The development environment has 60 `.dist-info` directories because it holds an editable install of
  it next to the 59.
* **colorama** 0.4.6. tqdm and typer require it only under the marker `sys_platform == 'win32'`, so it is not installed on
  macOS.

There are no development-only packages to drop: `pyproject.toml` has no dev dependency group and `uv.lock` records no
dev dependencies for the project. Resolving the lock for Python 3.12 gives the same 59 packages as for 3.13: the one
marker that differs, `python_full_version < '3.13'` on starlette's typing-extensions edge, changes nothing because other
packages require typing-extensions without a marker.
