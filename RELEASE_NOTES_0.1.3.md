MySearch 0.1.3 is a local-first search engine for your Mac. It searches your documents, scans, e-mail, audio and video by keyword and by meaning, and comes with a Finder-style app, instant whole-Mac file-name search, and an MCP server for AI agents. Everything runs on Apple Silicon.

To see how it works inside, read [How MySearch works](https://github.com/HeyitSaif/mysearch/blob/main/docs/TECHNICAL.md). Credits for the research, models and projects it builds on are in [docs/CREDITS.md](https://github.com/HeyitSaif/mysearch/blob/main/docs/CREDITS.md).

### What is new in 0.1.3

- **Whole-disk content search.** Find a file by what is *in* it, anywhere under the folders you choose, without indexing those folders deeply: "image of a zebra" finds the zebra photos, "Rechnung Telekom" the invoice whose text says so. Pictures get Vision labels (English and German), PDFs their first pages, documents their first kilobytes, videos a poster frame. It runs only while the Mac is idle and never reaches anything the name index excludes. Settings › Whole-disk Search, `mse disk status | search | rescan`, `GET /api/disk/search`. The whole story: [docs/WHOLE_DISK.md](https://github.com/HeyitSaif/mysearch/blob/main/docs/WHOLE_DISK.md).
- **Smart folder indexing.** A new folder is indexed sensibly from the first pass: `node_modules`, caches, virtual environments and build output next to a project marker are skipped, `.gitignore` and `.ignore` files are honoured, nested repositories and linked git worktrees are left out, and source code, binaries, fonts, disk images and large logs are searchable by name only. Every file is searchable by name at once; its content follows. Settings › Indexing, with per-folder overrides.
- **Native ⌥Space quick search.** The Quick Search panel is now native: it opens over any app, lists file names, the whole-disk hits under **On this Mac** and the matches in your folders under **In your folders**, with Quick Look, keyboard navigation and paging.
- **Lighter deep indexing.** A PDF with a text layer no longer gets a page image; a picture is OCRed only when a cheap probe says it holds text; a video can be indexed as one poster frame instead of 30-second clips; and heavy work (OCR, page renders, embeddings) waits while you use the Mac or run on battery (`indexing.priority`). Files already indexed are never redone.
- **Faster file-name search.** A smaller trigram name index (about a third of the size), a query plan that asks for the rarest pieces first, and fff-style rules: smart case (`Report` matches its case, `report` any), globs (`*.pdf`, `**/invoices/*`, `!**/old/**`), a match in the file name above one in the folder path, and files you open rank higher next time. The upgrade from the first index runs in the background; searching never stops. See [Find files by name](https://github.com/HeyitSaif/mysearch/blob/main/README.md#find-files-by-name-whole-mac).
- **Classic UI switchable off.** The old web interface is off by default and comes back with one switch in Settings › Advanced.
- **Shortcut recorder.** Change the Quick Search shortcut in Settings › Mac App: press the keys you want; combinations that belong to macOS or to MySearch itself are refused with a reason.

Also in this release: document facts (type, date, amount, counterparty) with filters such as `type:invoice amount:>500 party:acme docdate:2025`, a timeline of documents per month, saved searches, a setup assistant for first run, a once-a-day update check (Settings › Mac App, off with one switch), national ID masking for 20+ countries in agent output, and MCP resources and prompts for agents. Details in the [README](https://github.com/HeyitSaif/mysearch/blob/main/README.md).

### Install

1. You need an Apple Silicon Mac with macOS 14 or later, the Xcode Command Line Tools, and [uv](https://docs.astral.sh/uv/).
2. Clone this repository and run `./scripts/setup.sh`. It downloads the models, builds the helper and installs the background service. See [docs/INSTALL.md](https://github.com/HeyitSaif/mysearch/blob/main/docs/INSTALL.md).
3. Open `MySearch-0.1.3.dmg` and drag MySearch to Applications.
4. This build is **ad-hoc signed and not notarized**. On first launch macOS blocks it: open System Settings → Privacy & Security and click **Open Anyway**.
5. Grant **Full Disk Access** to MySearch.app. Ad-hoc builds need it granted again after each update.

This image does not bundle its own Python runtime, so it carries no GPL libraries and needs no source archive ([docs/RELEASING.md](https://github.com/HeyitSaif/mysearch/blob/main/docs/RELEASING.md)). Third-party notices: [THIRD_PARTY_NOTICES.md](https://github.com/HeyitSaif/mysearch/blob/main/THIRD_PARTY_NOTICES.md).

### Upgrading from 0.1.2

Replace the app and restart the service (`mse service restart`, or log out and in). The whole-Mac name index is upgraded in the background the first time the service runs; the old index keeps answering meanwhile. Document facts for files indexed before 0.1.3 are filled in over the next scans.

SHA-256 (MySearch-0.1.3.dmg): `314f0cbe08fb7f2d42ae723912d4d5be00c7a896b56b164af65ce7448077d720`
