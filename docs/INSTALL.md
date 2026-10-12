# Installing MySearch

MySearch is a local search engine for your own folders. It runs on your Mac, indexes files in place and never sends
file contents anywhere. This guide covers a fresh install on an Apple Silicon Mac, two ways:

- **[From the disk image](#install-from-the-disk-image)**: drag the app to Applications. No clone, no Python, no Terminal.
- **[From a clone](#install-from-a-clone-developers)**: `scripts/setup.sh`, for people who want to read, run and change the code.

## Install from the disk image

The disk image (`MySearch.dmg`, made by `scripts/build_dmg.sh --bundle-runtime`) carries its own Python runtime and every
package MySearch needs, so nothing else has to be installed. The one thing downloaded afterwards is the model, by the app,
when you press the button.

**Needs:** an Apple Silicon Mac (M1 or later) on macOS 14 or later; about 0.8 GB for the app (large because the Python
packages are inside it), then about 1.3 GB for the minimal model set (about 4.4 GB for the full set). ffmpeg is optional
and only matters for audio and video (`brew install ffmpeg`).

1. **Open the disk image and drag MySearch to Applications.** Open it from Applications, not from the disk image: the
   background service is started from wherever the app is when you set it up, and a disk image goes away when you eject it.
   The app refuses to set the service up from a disk image or from a quarantined copy and says so.
2. **First open.** A notarized build opens normally. A build that is only ad-hoc signed (made by someone without an Apple
   Developer ID) is blocked by Gatekeeper on other Macs: right-click the app, choose Open, then Open again; or System
   Settings > Privacy & Security > Open Anyway.
3. **The Setup Assistant opens by itself** (there is no folder yet and no model). Five steps:
   - **Welcome:** press *Start MySearch*. That sets up the background service (a login item that runs the search service
     and the indexing). macOS shows a "Background item added" notice: that is this. It starts when you log in.
   - **Folders:** *Add Folder…* (the usual folder picker), or one click for Documents or Desktop. Your files are only read,
     never changed.
   - **Models:** *Minimal* (text search, plus pictures and PDF pages; about 1.27 GB) or *Full* (adds speech to text and a
     small local model for Ask; about 4.4 GB). Nothing is downloaded until you press *Download*. The bar shows bytes, speed
     and time left; *Stop* keeps what was fetched and *Resume Download* continues. When the models are in, MySearch starts
     indexing the folders you added.
   - **Full Disk Access:** explains why macOS keeps Desktop, Documents, Downloads and Mail private, and opens System
     Settings > Privacy & Security > Full Disk Access. You switch MySearch on there yourself. It is optional if you only
     index folders macOS does not protect.
   - **Done:** a summary. *Open MySearch* closes the assistant.
4. **Open the assistant again** any time: Settings > Mac App > *Open Setup Assistant…*. *Skip Setup* (or closing the
   window) stops it from opening by itself; nothing else changes.

**Licences.** The app's notices are in Settings > About > *Licences…* (the file is `MySearch.app/Contents/Resources/THIRD_PARTY_NOTICES.md`).
The disk image bundles GPL-licensed code (FFmpeg and others, inside the opencv-python package); the notices say which, and
where its source is.

**What goes online, and only this:** the model download you start in step 3 (Hugging Face); a once-a-day look at the latest
GitHub release (Settings > Mac App > *Check for updates*, on by default, off with one switch; see the README); and cloud
answers only if you turn Ask on and choose a cloud model. Indexing, searching and the local models never go online, and
the background service itself runs with Hugging Face access switched off: a download runs in a separate short-lived process
that exists only while you are downloading.

**The command line.** The app carries `mse`: `/Applications/MySearch.app/Contents/Resources/runtime/bin/mse`. Put a link on
your path if you want it in Terminal (`ln -s /Applications/MySearch.app/Contents/Resources/runtime/bin/mse /usr/local/bin/mse`);
the settings that connect AI agents write this full path, so they work without it.

**Moving or replacing the app.** The background service starts the app from the place it was in when you pressed *Start
MySearch*. Keep one copy. If you move the app, open it, go to the assistant's first step (Settings > Mac App > *Open Setup
Assistant…*) and press *Start MySearch* again. After you replace the app with a newer one, restart the service so it runs the new code:
`/Applications/MySearch.app/Contents/Resources/runtime/bin/mse service restart` (or log out and in).

**Updating.** Download the new disk image and replace the app. MySearch tells you when a release is newer
(*Update available — 0.x.y* in the menu-bar menu and in Settings > Mac App) and opens its release page; it never downloads or
installs anything itself. With Homebrew (once a cask exists, see `packaging/homebrew/`): `brew upgrade --cask mysearch`.

**Uninstalling the disk-image install.** In order, by hand:

1. Quit MySearch (menu bar icon > Quit MySearch), then run `/Applications/MySearch.app/Contents/Resources/runtime/bin/mse service uninstall`
   (stops the service and removes its login item).
2. Move `/Applications/MySearch.app` to the Trash. Remove MySearch from Full Disk Access in System Settings.
3. Optional, and not undoable: the data folder `~/Library/Application Support/MySearch/` (indexes, config, models), the logs in
   `~/Library/Logs/MySearch/`, and `~/Library/Preferences/com.mysearch.app.plist`.

## Install from a clone (developers)

### Requirements

| What | Needed for |
|---|---|
| Apple Silicon (M1 or later) | the models are MLX models, which run on the Mac's GPU |
| macOS 14 or later | the app and the background service |
| About 2.5 GB free for the minimal set, about 5 GB for the full set, plus room for your indexes | the models (about 1.3 GB minimal, about 4.4 GB full), the Python environment (about 0.56 GB, measured on the reference Mac; uv's own download cache is extra and was not measured), the app (about 10 MB) |
| Xcode Command Line Tools (`xcode-select --install`) | the Swift helper and the app; they also provide `git` |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) (`brew install uv`) | the Python environment and the `mse` command |
| ffmpeg (optional, `brew install ffmpeg`) | audio and video: frames and sound |

The setup never installs uv, Homebrew or ffmpeg for you. It tells you what is missing and stops.

### One-command setup

```sh
git clone <repository-url> ~/Developer/mysearch
cd ~/Developer/mysearch
scripts/setup.sh
```

The script asks two things: which model set to install (minimal is the default) and which folders to index. It asks
once more before copying the app into `/Applications`, and the default answer is no. To run it without questions:

```sh
scripts/setup.sh --minimal --yes     # text search; no app copy; no folders added
scripts/setup.sh --full --yes        # adds speech to text and the local Ask model
scripts/setup.sh --no-app            # command line and background service only
```

Each step is skipped when it is already done, so running the script again is safe. It never deletes anything, never
uses `sudo`, and never pipes a download into a shell. Anything you must delete or move yourself is printed for you.

What it does, in order:

1. checks Apple Silicon, macOS 14+, the Command Line Tools and uv;
2. `uv sync --frozen`, which installs the Python packages pinned in `uv.lock`;
3. shows the model plan (`mse setup models --dry-run`), then downloads the set you chose;
4. builds the native helper into `~/Library/Application Support/MySearch/bin/`, unless it is already there;
5. builds `build/MySearch.app`, ad-hoc signed, unless it is already built;
6. offers to copy the app to `/Applications` (y/N). It refuses to replace an existing app;
7. installs the background service (`mse service install`), unless it is installed;
8. asks for folders to add (`mse add`), one per line, until an empty line.

## Manual steps

Everything the script does, by hand, from the clone:

```sh
uv sync --frozen
uv run mse setup models --dry-run --minimal     # the plan; changes nothing
uv run mse setup models --minimal               # or --full
scripts/build_helper.sh                         # into the data folder (default location)
scripts/build_app.sh                            # into build/MySearch.app
ditto build/MySearch.app /Applications/MySearch.app   # optional; move any old copy to the Trash first
uv run mse service install
uv run mse add ~/Documents/Notes                # any folder you want searched
uv run mse doctor
```

## Permissions to grant yourself

These cannot be granted by a script:

- **Full Disk Access.** System Settings > Privacy & Security > Full Disk Access, switch on MySearch. Without it, macOS
  blocks the indexer from Desktop, Documents, Downloads, Mail and similar folders. If the app is not installed in
  `/Applications`, the background service runs the project's Python instead, and that Python needs the grant (the
  setup and `mse doctor` print which one).
- **Finder extension (optional).** System Settings > General > Login Items & Extensions > Finder, switch on MySearch.
- **Raycast (optional).** The extension is in `integrations/raycast`. Follow its README.
- **Agents (optional).** `uv run mse agents install <folder>` writes that folder's agent configuration (the MCP files
  for Claude Code, Cursor and others). `uv run mse agents install --global` also writes user-level configs (Claude
  Desktop, `~/.cursor`, `~/.grok`, `~/.codex`, Antigravity), so run it only if you want them.

Keep the clone where it is. The background service runs from it. Moving or deleting the clone stops the service until
you run `uv run mse service install` again from the new place.

## Models and licences

Model weights are never shipped with this repository. The setup downloads them from public Hugging Face repositories,
at the revisions below, and keeps them in `~/Library/Application Support/MySearch/models/`.

Checked on 2026-10-10 against the Hugging Face API (read-only) and the source repository's model cards.

| Folder | What it does | Tier | Source (revision) | Download | Licence | Gated |
|---|---|---|---|---|---|---|
| `embeddinggemma-2-8bit` | embeddings: text search, and images, PDF pages, audio and video | minimal | `mlx-community/embeddinggemma-2-8bit` (`7505ef2f`) | about 1.27 GB | Apache-2.0 | no |
| `whisper-large-v3-turbo-8bit` | speech to text with timestamps | full | `mlx-community/whisper-large-v3-turbo-8bit` (`62103fc2`), plus processor and tokenizer files from `openai/whisper-large-v3-turbo` (`41f01f3f`) | about 0.87 GB | MIT (OpenAI's original); the MLX repo's card says Apache-2.0 | no |
| `qwen3-4b-instruct-2507-4bit` | local answers for Ask, used only when you choose a local answer | full | `mlx-community/Qwen3-4B-Instruct-2507-4bit` (`50d42775`) | about 2.28 GB | Apache-2.0 | no |
| `qwen3-reranker-0.6b-8bit` | re-ranks close calls | full | original `Qwen/Qwen3-Reranker-0.6B` (`e61197ed`), converted to 8 bits by hand; the setup cannot download it yet | (copy the folder from a Mac that has it) | Apache-2.0 | no |
| `embeddinggemma-2-8bit-text` | a text-only copy of the embedding model, smaller and quicker for text search | optional | unknown; no public source found | not downloaded | same licence as its source model (Apache-2.0); conversion recipe not published | no |
| `qwen3.5-4b-4bit` | an alternative local answer model | optional | `mlx-community/Qwen3.5-4B-MLX-4bit` (`32f3e8ec`) | about 3.06 GB | Apache-2.0 | no |

Notes on the table:

- **Licence of EmbeddingGemma.** The model card and the Hugging Face licence tag say Apache-2.0, not the Gemma Terms of
  Use. This guide follows the card. If Hugging Face ever asks you to accept terms for a model, open its page while
  signed in, accept them, and run `uv run hf auth login`. The setup prints the same instruction when a download is refused.
- **Login.** Only the Hugging Face CLI stores a token (`uv run hf auth login`). MySearch never stores one.
- **Why minimal downloads the full embedding folder.** The text-only copy has no public source, so the setup cannot
  fetch it. The full folder contains the text encoder too, and the text worker loads it with the vision and audio parts
  switched off (`mse/worker.py`, `Embedder`). Search therefore works from the full folder. The reference install still
  uses the text-only copy, so this path has not yet been run on a fresh Mac.
- **Re-ranker.** Without it, search still works. The re-ranker only reorders close calls, and its failure is recorded in
  the timings (`rerank_error`). `uv run mse setup check` says whether it is installed.
- **Not part of any install.** `reranker-lora` is a locally trained adapter for the re-ranker. It applies only
  on top of the 8-bit re-ranker and is never downloaded.
- If you have an `hf/` folder in the models folder from earlier manual downloads, the setup does not use it and does not remove it.

Check the set at any time:

```sh
uv run mse setup check           # every model, the app, the service, free disk
uv run mse setup check --json    # the same, with licence, source and tier for each model
```

## Building the app and the disk image yourself

- **Ad-hoc build (no Apple Developer account).** `scripts/build_app.sh` builds `build/MySearch.app`, signed ad hoc. It
  runs on the Mac that built it and uses this clone's `.venv` for the service, as before. `scripts/build_app.sh --install`
  builds into `~/Applications`; the background service uses only `/Applications`, so copy the app there before you run
  `mse service install`.
- **With its own Python: `--bundle-runtime`.** `scripts/build_app.sh --bundle-runtime` (or `scripts/build_dmg.sh
  --bundle-runtime`) also puts a relocatable CPython (python-build-standalone, fetched with `uv python install`), the
  packages locked in `uv.lock` (`uv sync --frozen`) and the `mse` package inside `MySearch.app/Contents/Resources/`
  (`scripts/bundle_runtime.sh`). The result is the app of the disk-image install above, about 0.8 GB. Build-time network use:
  the CPython download (about 24 MB), the wheels from PyPI and the one git dependency (mlx-vlm) from GitHub; they are cached
  in `MSE_RUNTIME_CACHE` (default `build/runtime-cache`). `scripts/bundle_runtime.sh check <app>` audits the result: every
  `otool -L` dependency and rpath of every Mach-O file, symlinks, and strings that name the build machine.
  Which Python the app starts (`MySearch --daemon`), in this order: the project in `config.json` (`service.project`) when it has a
  `.venv` (a clone, the developer's setup), then the bundled runtime, then the clone the app was built in.
  **Licences.** Every build, bundled or not, carries `LICENSE` and `THIRD_PARTY_NOTICES.md` in `Contents/Resources`. A bundled
  build also carries the GPL, LGPL and GCC-exception texts in `Contents/Resources/Licenses/` (from `licenses/`), because the
  opencv-python wheel inside it bundles a GPL build of FFmpeg with x264, x265, Xvid, vid.stab and Rubber Band. **A release
  of a bundled image must also attach `MySearch-<version>-sources.tar.gz`, the source of the GPL, LGPL and MPL-2.0 libraries in it, to the same GitHub
  release** (`scripts/fetch_gpl_sources.sh`, checked by `scripts/check_gpl_release.sh`); a plain image needs no such file.
  See [RELEASING.md](RELEASING.md).
- **Disk image.** `scripts/build_dmg.sh` writes `build/MySearch.dmg`. `--adhoc` makes a test image. A normal build signs
  with the identity in the `MSE_SIGN_IDENTITY` environment variable: set it to yours (`security find-identity -v -p
  codesigning` lists them). Signing is inside-out: every Mach-O file of the runtime, the helper, the Finder extension, then
  the app.
- **Distributing to other people** needs a Developer ID signature and notarisation. macOS blocks unnotarised apps and
  disk images downloaded from elsewhere. See *Notarizing* below. For a bundled image it also needs the GPL source archive
  (see *Licences* above and [RELEASING.md](RELEASING.md)).

### Notarizing

`scripts/notarize.sh` is a scaffold for that. **It has not been run.** If you have a Developer ID certificate, read it first and run it on that Mac. It needs
two environment variables and refuses to run without them: `MSE_DEVELOPER_ID` (the `Developer ID Application: …` codesign
identity) and `MSE_NOTARY_PROFILE` (a `notarytool` profile you made once with `xcrun notarytool store-credentials`; the
script never sees or stores a credential). It builds and signs the app with the hardened runtime and a secure timestamp,
has Apple notarize the app, staples it, packs it into `MySearch-<version>.dmg`, signs, notarizes and staples that, then
prints the sha256 for the Homebrew cask.

The hardened runtime is what notarization requires, and it needs two entitlement files (`scripts/entitlements/`):

| File, applied to | Entitlement | Why |
|---|---|---|
| `MySearch.entitlements`, the app | `com.apple.security.automation.apple-events` | "Open in…" starts Terminal through Apple Events; the hardened runtime blocks that without it. |
| `python-runtime.entitlements`, the bundled `python3.X` | `com.apple.security.cs.disable-library-validation` | Library validation makes a hardened process refuse libraries signed by another team. Every Mach-O file of the runtime is signed with the app's identity, so a Developer ID build should pass without this key (not tried). It is set because an ad-hoc signed runtime does not import numpy without it, and as the safety net for a wheel library that could not be re-signed. |

Not set, on purpose: `allow-jit` and `allow-unsigned-executable-memory`. Add one only if a notarized build fails at run time
with a code-signing or JIT error. Entitlements count on the main executable of a process, so only the interpreter carries
them; the extension modules and libraries it loads are signed without.

What has been checked, on this Mac, with an **ad-hoc** signature and the hardened runtime switched on for the interpreter:
the entitlement files are accepted by `codesign` (the signing tools parse them strictly: a comment must not contain two
hyphens in a row); with `disable-library-validation` the interpreter imports numpy, scipy, cv2, PIL, mlx, mlx_vlm, mlx_lm,
transformers, cryptography and sqlite-vec, runs an MLX matmul and an 8-bit quantized matmul on the GPU, a ctypes callback, a
cffi callback and an sqlite-vec nearest-neighbour query; without it, the import of numpy fails ("different Team IDs");
`allow-jit` alone does not help. What has **not** been checked: any of this with a Developer ID signature, and whether
Apple's service accepts the whole bundle. That is known only after the first real run; if it rejects a file,
`xcrun notarytool log <id> --keychain-profile <profile>` names it.

## Updating

For the disk-image install see *Updating* under "Install from the disk image". For a clone:

```sh
cd ~/Developer/mysearch
git pull
uv sync --frozen
uv run mse setup models --minimal        # or --full: installed models are skipped
```

If the helper changed, run `scripts/build_helper.sh`. A rebuilt helper makes macOS ask again for the Keychain items of
cloud providers: allow it, or store the keys again with `uv run mse providers set-key <provider>`.

If the app changed: move the old `/Applications/MySearch.app` to the Trash yourself, run `scripts/build_app.sh`, copy
`build/MySearch.app` to `/Applications`, then run `uv run mse service install`.

After you replace the app, check two things in System Settings: Full Disk Access for MySearch, and the Finder extension.
macOS may treat a rebuilt ad-hoc app as a new program and drop either grant. This is not yet confirmed on this project's
builds; `uv run mse setup check` shows which program the service runs.

Restart the service at any time with `uv run mse service restart`.

## Uninstalling

For the disk-image install see its own section above. For a clone, nothing here runs these commands for you; run them yourself,
in this order:

```sh
cd ~/Developer/mysearch
uv run mse service uninstall                  # stops the service and removes its LaunchAgent
uv run mse providers forget-key <provider>    # once for each cloud provider you stored a key for
```

Then:

1. Move `/Applications/MySearch.app` (and `~/Applications/MySearch.app`, if present) to the Trash in Finder.
2. Remove MySearch from System Settings > Privacy & Security > Full Disk Access, and switch off its Finder extension.
3. Delete the data folder `~/Library/Application Support/MySearch/` (indexes, config, models, the download cache). Its
   location follows the `MSE_HOME` environment variable if you set one.
4. Delete the LaunchAgent file `~/Library/LaunchAgents/com.mysearch.daemon.plist`, if `mse service uninstall` left it.
5. Delete the logs in `~/Library/Logs/MySearch/`.
6. Delete the clone folder, `~/Developer/mysearch`.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `uv: command not found` | Install uv (`brew install uv`), then run the setup again. |
| Command Line Tools missing | `xcode-select --install`, wait, then run the setup again. |
| A model download says *not found, or needs terms accepted and a login* | Hugging Face gives the same answer for both. Open the model page (the URL is printed), accept the terms if it asks, then `uv run hf auth login`. |
| `not enough free disk space` | Free the amount the message names, plus 1 GB, then run the setup again. |
| `incomplete folder ... not touched` | The folder exists without its weights or files. Move it out of the models folder in Finder, then run the setup again. |
| A download stopped | Run the same command again. Finished files are kept and the download resumes. |
| `mse doctor` says the service is not installed | `uv run mse service install`. Logs: `~/Library/Logs/MySearch/launchd.log` and `worker.log`. |
| Files in Desktop, Documents, Downloads or Mail are missing from search | Grant Full Disk Access (see above). |
| Images, PDF pages or recordings are not found | Images and pages need the embedding model, which the minimal set includes. Audio and video need ffmpeg (`brew install ffmpeg`), and speech needs the full set. |
| Search answers do not reorder close calls | The re-ranker is not installed. See the table above. |
| Keychain prompts after a helper rebuild | Allow the helper once, or store the provider keys again (see Updating). |
| The app does not open on another Mac | It is ad-hoc signed, so it is for the Mac that built it. Distribution needs notarisation (see above). |
| The assistant says the service is not running, or the app was moved | Open Settings > Mac App > *Open Setup Assistant…* and press *Start MySearch* (it installs the login item for the app where it is now). The log is `~/Library/Logs/MySearch/launchd.log`. |
| The first open from the disk image is slow | macOS checks a new app the first time it runs, and this one holds about 20,000 files. On the build Mac the first start from a freshly mounted image took longer than 8 seconds; the next one took about a second. Copy the app to Applications first and open it from there. |
| The assistant refuses to start the service ("disk image", "temporary copy") | Drag MySearch to Applications and open it from there. |
| A model download failed or stopped | Press *Try Again* or *Resume Download*: finished files are kept. The reason is shown; the log is `~/Library/Logs/MySearch/setup-models.log`. |
| "Update available" never shows, or you want no update check | Settings > Mac App > *Check for updates*. It is one request a day to api.github.com; *Check Now* looks once. |

If something is not listed here, run `uv run mse doctor` and keep its output with the first error line.
