# Licence texts

These are the licence texts for the copyleft code that a disk image built with `scripts/build_dmg.sh --bundle-runtime`
carries inside its Python packages (the macOS wheels of opencv-python and scipy). `scripts/bundle_runtime.sh install` copies
this folder to `MySearch.app/Contents/Resources/Licenses/`, next to `LICENSE` and `THIRD_PARTY_NOTICES.md`. A build
without `--bundle-runtime` contains none of that code and does not get the folder.

The texts are stored byte for byte as downloaded (read-only downloads, 2026-10-10). Do not edit them: `sha256` is the
check. `scripts/oss_audit.py` pins the same digests (`VERBATIM_LICENCE_TEXTS`). Licence texts are exempt from the project's
private word list because they are verbatim upstream texts, pinned by sha256; when a text is replaced, change the digest
in this table and in that script. Wheels keep their own licence files in their `.dist-info` folders; those are copied by the runtime build and are not
repeated here.

| File | Licence | Covers | Downloaded from | sha256 |
|---|---|---|---|---|
| `GPL-3.0.txt` | GNU GPL, version 3 | FFmpeg 7.1.1 as built (`--enable-gpl --enable-version3`: "GPL version 3 or later"), and the combined work around `cv2` | https://www.gnu.org/licenses/gpl-3.0.txt | `3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986` |
| `GPL-2.0.txt` | GNU GPL, version 2 | x264, x265, Xvid, vid.stab, Rubber Band (all "version 2 or, at your option, any later version") | https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt (`/licenses/gpl-2.0.txt` redirects here) | `edaef632cbb643e4e7a221717a6c441a4c1a7c918e6e4d56debc3d8739b233f6` |
| `LGPL-2.1.txt` | GNU LGPL, version 2.1 | libquadmath (scipy wheel); the LGPL-2.1-or-later libraries of the opencv-python wheel (see `THIRD_PARTY_NOTICES.md`) | https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt | `20e50fe7aae3e56378ebf0417d9de904f55a0e61e4df315333e632a4d3555d95` |
| `LGPL-3.0.txt` | GNU LGPL, version 3 | the LGPL-3.0 text that the COPYING file of aribb24 holds (its source headers say "version 2.1 or any later version"; see `THIRD_PARTY_NOTICES.md`), and the LGPL-3.0-or-later option of gmp, libunistring and libidn2 | https://www.gnu.org/licenses/lgpl-3.0.txt | `e3a994d82e644b03a792a930f574002658412f62407f5fee083f2555c5f23118` |
| `GCC-Runtime-Library-Exception-3.1.txt` | GCC Runtime Library Exception, version 3.1 | libgcc_s and libgfortran (scipy wheel): "GPL-3.0-or-later WITH GCC-exception-3.1" | `COPYING.RUNTIME` of GCC, tag `releases/gcc-13.4.0`: https://gcc.gnu.org/git/?p=gcc.git;a=blob_plain;f=COPYING.RUNTIME;hb=refs/tags/releases/gcc-13.4.0 | `9d6b43ce4d8de0c878bf16b54d8e7a10d9bd42b75178153e3af6a815bdc90f74` |
| `MPL-2.0.txt` | Mozilla Public License, version 2.0 | libzmq (`libzmq.5`, zeromq 4.3.5) and libsrt (`libsrt.1.5.4`, srt 1.5.4): both MPL-2.0 (see `THIRD_PARTY_NOTICES.md`). Not the MPL-2.0 licences of certifi and tqdm, which stay in their `.dist-info` folders | https://www.mozilla.org/media/MPL/2.0/index.txt (downloaded 2026-10-11) | `3f3d9e0024b1921b067d6f7f88deb4a60cbe7a78e76c64e3f1d7fc3b779b9d04` |
| `FFmpeg-7.1.1-LICENSE.md` | FFmpeg's own licensing statement | which FFmpeg parts and which external libraries make a build GPL (it names libx264, libx265, libxvid, libvidstab and librubberband as GPL libraries that require `--enable-gpl`) | `ffmpeg-7.1.1/LICENSE.md` inside https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz (sha256 `733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1`) | `cb48bf09a11f5fb576cddb0431c8f5ed0a60157a9ec942adffc13907cbe083f2` |

## Notes on the sources

* **GCC Runtime Library Exception.** gnu.org publishes the exception as a web page only
  (https://www.gnu.org/licenses/gcc-exception-3.1.html); the `.txt` address under `/licenses/` answers 404. The text here is the
  `COPYING.RUNTIME` file that ships with GCC. It is byte-identical in the tags `releases/gcc-13.2.0` and `releases/gcc-13.4.0`
  and on `master` (checked 2026-10-10).
* **Upstream copies.** FFmpeg 7.1.1's `COPYING.GPLv2`, `COPYING.GPLv3`, `COPYING.LGPLv2.1` and `COPYING.LGPLv3` differ from the
  gnu.org files only in the Free Software Foundation's postal address and in `http` against `https` in two URLs (compared
  with `diff -w`, 2026-10-10). x264 (commit `31e19f92`), x265 4.1 and Xvid 1.3.7 each ship the GPL-2.0 text as `COPYING` or
  `LICENSE`. The gnu.org versions are kept here as the canonical ones.
* **"Or later".** The file headers of x264 (commit `31e19f92`, `x264.h`, `common/common.c`), x265 4.1 (`source/x265.h`,
  `source/common/common.cpp`), Xvid 1.3.7 (`src/xvid.c`), vid.stab 1.1.1 (`src/motiondetect.c`) and Rubber Band 4.0.0
  (`rubberband/RubberBandStretcher.h`) say the programs are free software under the GNU GPL "either version 2 of the
  License, or (at your option) any later version" (read in the upstream sources on 2026-10-10). x264, x265 and Rubber Band
  also offer a commercial licence separately; MySearch uses none of those. Homebrew's formulas
  for x264 and x265 declared `GPL-2.0-only` until 2025-06-27 and `GPL-2.0-or-later` since (homebrew-core commits
  `aebf7099cccd` and `225d16f20623`). The headers are the authority; the formula metadata was corrected to match them.
* **What is not here.** The Apache-2.0, MIT, BSD and other permissive texts of the Python packages stay in each package's
  `.dist-info` folder inside the runtime, and so do the MPL-2.0 texts of certifi and tqdm. The MPL-2.0 text here is the one
  for libzmq and libsrt. Licences of the libraries inside the opencv-python wheel
  that are not GPL or LGPL are listed by name in `THIRD_PARTY_NOTICES.md`; the wheel itself ships only an LGPL-2.1 text
  for them (see there).
