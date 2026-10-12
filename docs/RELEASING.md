# Releasing MySearch

This page is about what a release must carry. How the app and the disk image are built and signed is in
[INSTALL.md](INSTALL.md), "Building the app and the disk image yourself".

## The rule

There are two kinds of disk image, and only one of them needs more than the `.dmg`:

| Image | Built with | Contains copyleft code? | What the GitHub release must carry |
|---|---|---|---|
| **Plain** | `scripts/build_dmg.sh` (no `--bundle-runtime`) | No. The app uses the Python of a clone. | the `.dmg` |
| **Self-contained** | `scripts/build_dmg.sh --bundle-runtime`, or `scripts/notarize.sh --bundle-runtime` | **Yes.** The opencv-python wheel inside it carries GPL builds (FFmpeg 7.1.1, x264, x265, Xvid, vid.stab and Rubber Band) and LGPL and MPL-2.0 libraries. | the `.dmg` **and** `MySearch-<version>-sources.tar.gz`, attached to the **same** release |

A self-contained image is published with its source archive, never without it. The Homebrew cask in `packaging/homebrew/`
installs the self-contained image, so its release follows the same rule. Every build, plain or not, carries `LICENSE` and
`THIRD_PARTY_NOTICES.md` in `Contents/Resources`; a self-contained build also carries `Contents/Resources/Licenses/`
(the GPL, LGPL, GCC-exception and MPL-2.0 texts from `licenses/`). The notices contain the written offer for the source, the list of
the GPL components ("GPL components in the self-contained DMG") and the list of the LGPL and MPL-2.0 libraries with their
versions and sources ("LGPL and MPL-2.0 libraries in the same wheel"): [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Steps for a self-contained release

1. Build the image. `scripts/build_dmg.sh --bundle-runtime --versioned ...` or `scripts/notarize.sh --bundle-runtime`. Both
   print a reminder about the source archive, and `build_dmg.sh` writes `MySearch-<version>.dmg.bundled-runtime` next to the
   image. That file tells the check in step 4 which archive the image needs.
2. Make the source archive: `scripts/fetch_gpl_sources.sh <new folder>`. It needs the network, refuses a folder that is not
   empty and deletes nothing. It checks every download against a pinned sha256 or commit and stops on a difference, then runs
   sanity checks on the unpacked versions. The result is `<folder>/MySearch-<version>-sources.tar.gz`: about 259 MB, holding
   about 262 MB of files (the GPL components with GCC, OpenCV and the opencv-python scripts about 187 MB; the sixteen LGPL and
   MPL-2.0 sources about 69 MB; the two Nettle tarballs and their signatures about 5 MB; the recipes, licence texts and notes the rest). `--dry-run` shows the plan and the sizes without downloading. The folder `<folder>/work` (git checkouts and
   partial downloads) is left over and can be removed by hand.
3. Put the image and the archive in one folder.
4. Check the folder: `scripts/check_gpl_release.sh <folder>`. It fails when an image that bundles the runtime has no archive
   next to it, when the archive lacks one of its expected files, or when a file differs from `SHA256SUMS`. It reads and writes
   nothing else. For an image built some other way: `--bundled` (treat every image in the folder as self-contained) or
   `--plain` (say nothing is needed). Without the sidecar file and without either flag it refuses to guess.
5. Create the GitHub release `v<version>` and attach both files. Write the archive's name into the release notes, next to the
   download: `MySearch-<version>-sources.tar.gz` is the source of the GPL, LGPL and MPL-2.0 libraries in the disk image. Link
   `THIRD_PARTY_NOTICES.md` as well, because the GNU GPL asks for clear directions next to the object code, and the notices
   inside the app are not next to the download. The check script cannot see the release notes, so this step is yours.
6. Keep the archive downloadable for as long as the release offers that image, and for at least three years after the last
   release that did (the written offer in the notices promises that).

## When a dependency changes

The pins in `scripts/fetch_gpl_sources.sh` and the facts in the notices describe **one wheel**: opencv-python 5.0.0.93 for
macOS arm64 (sha256 `198a7513...`, locked in `uv.lock`), with the scipy 1.18.1 wheel for the GCC runtime. The script stops if
`uv.lock` locks anything else. A new wheel may carry other library versions, other copyleft components, or an LGPL FFmpeg.
Before releasing with one:

* list `cv2/.dylibs` of the new wheel. Read each library's version from the library itself (its version function, or a
  version string with `strings -a`), not from a Homebrew formula alone. Homebrew bottles differ in date: the build paths
  inside a binary (for example `libssh-20240901` or `srt-20241111`) show when its bottle was compiled, and the formula of that
  date is the recipe;
* find how that wheel's macOS job got its FFmpeg (the workflow files at the opencv-python tag) and the build configuration
  string inside libavutil (`strings -a libavutil.*.dylib | grep -e --enable`);
* for each LGPL or MPL-2.0 library: find its tarball and the sha256 of its Homebrew formula at the same version, read the
  tarball's own version file, and test any Homebrew patch with `patch -p1 --dry-run` against the tarball;
* update the pins and the sanity checks in the script, the table in `THIRD_PARTY_NOTICES.md` and the README text the script
  writes;
* if scipy, cryptography or another wheel starts to bundle GPL or LGPL code, add it to the notices and to the archive.

The licence texts in `licenses/` are stored byte for byte, with their source and sha256 in `licenses/README.md`. Replace one
only by downloading it again from the source named there and updating the sha256.

## Open items before a self-contained release (as of 2026-10-11)

* **Nettle is in the archive as both 3.10.1 and 3.10.2, with no recipe, and its signatures are not checked.** The binary
  reports 3.10 only, so both tarballs and their `.sig` files are pinned in `scripts/fetch_gpl_sources.sh` (the notices give the
  reasoning). Their sha256 values match the Homebrew formula values recorded in the notices, but the formulas were not fetched
  again. Follow-ups: check the signatures with gpg and the Nettle release key (the issuer key ID is F3599FF828C67298); add
  the nettle members and `licenses/MPL-2.0.txt` to the expected list in `scripts/check_gpl_release.sh`, which does not name
  them yet; and, if wanted, add the nettle Homebrew formulas to `homebrew-formulas/`. The arm64 bottles could still settle
  which patch release the binary came from; downloading them is the owner's call.
* **The GCC release behind scipy's libgcc_s, libgfortran and libquadmath is inferred, not read from the libraries.** They carry
  no version string. The archive holds GCC 13.4.0 because three scipy extension modules, as downloaded, have run paths into
  `/opt/homebrew/Cellar/gcc@13/13.4.0/lib/gcc/13` (the DMG build deletes them). That is strong evidence, not proof. To settle
  it, read the build log of the scipy v1.18.1 wheels job on GitHub Actions (macos-14, arm64, Accelerate), which prints the
  compiler it used, or compare `libgfortran.5.dylib` with a Homebrew `gcc@13` 13.4.0 bottle.
* **The MPL-2.0 licence text is in `licenses/` (added 2026-10-11).** `licenses/MPL-2.0.txt` is the canonical Mozilla text. It is
  pinned by sha256 in the table of `licenses/README.md` and in `VERBATIM_LICENCE_TEXTS` in `scripts/oss_audit.py`, because the
  private word list flags its text. The zeromq and srt tarballs keep their own `LICENSE` files in the archive.
* **The permissively licensed libraries of the opencv-python and scipy wheels are covered by the written offer, not by the
  archive.** The FFmpeg build here links many of them hard (libvpx, opus, dav1d and others), and the GNU GPL counts "shared
  libraries and dynamically linked subprograms that the work is specifically designed to require" into the Corresponding
  Source. The archive takes the LGPL and MPL-2.0 sources; the offer is the cheaper reading for the permissive ones.
* **The offer's contact is the GitHub issue tracker.** The repository publishes no mailbox. A release that is offered to
  people who cannot use GitHub should add one.
* **regex declares a CNRI Python licence** that the Free Software Foundation lists, for some versions, as GPL-incompatible. It is
  not linked to the GPL libraries and the notices do not call the DMG as a whole GPL; a lawyer should confirm that reading.
* **The notices state a legal position** (the DMG conveys the cv2 combined work under GPL-3.0-or-later; the other packages
  are separate works). It was written by an engineer from the licences' texts. Have it read before the first public release.
