# Installing MySearch

MySearch is a local search engine for your own folders. It runs on your Mac, indexes files in place and never sends
file contents anywhere. This guide covers a fresh install on an Apple Silicon Mac.

## Requirements

| What | Needed for |
|---|---|
| Apple Silicon (M1 or later) | the models are MLX models, which run on the Mac's GPU |
| macOS 14 or later | the app and the background service |
| About 2.5 GB free for the minimal set, about 5 GB for the full set, plus room for your indexes | the models (about 1.3 GB minimal, about 4.4 GB full), the Python environment (about 0.56 GB, measured on the reference Mac; uv's own download cache is extra and was not measured), the app (about 10 MB) |
| Xcode Command Line Tools (`xcode-select --install`) | the Swift helper and the app; they also provide `git` |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) (`brew install uv`) | the Python environment and the `mse` command |
| ffmpeg (optional, `brew install ffmpeg`) | audio and video: frames and sound |

The setup never installs uv, Homebrew or ffmpeg for you. It tells you what is missing and stops.

## One-command setup

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
  runs on the Mac that built it. `scripts/build_app.sh --install` builds into `~/Applications`; the background service
  uses only `/Applications`, so copy the app there before you run `mse service install`.
- **Disk image.** `scripts/build_dmg.sh` writes `build/MySearch.dmg`. `--adhoc` makes a test image. A normal build signs
  with the identity in the `MSE_SIGN_IDENTITY` environment variable: set it to yours (`security find-identity -v -p
  codesigning` lists them).
- **Distributing to other people** needs a Developer ID signature and notarisation. macOS blocks unnotarised apps and
  disk images downloaded from elsewhere. This repository does not notarise yet: notarise the disk image with Apple's
  `notarytool` and staple the ticket before you hand it out.

## Updating

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

Nothing here runs these commands for you. Run them yourself, in this order:

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

If something is not listed here, run `uv run mse doctor` and keep its output with the first error line.
