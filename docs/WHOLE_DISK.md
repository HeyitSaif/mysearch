# Whole-disk content search

Find a file by what is **in** it, anywhere in the folders you choose, without indexing those folders deeply.
"image of a zebra" finds the zebra photos; "Rechnung Telekom" finds the invoice whose text says so. The code is
`mse/wholedisk.py`; the Vision and PDF work runs in the native helper (`msehelper describe`).

## Two tiers

| | Whole-disk light | Deep folders |
|---|---|---|
| Where | every folder under the whole-disk roots (your home folder by default) | the folders you add to the index (Settings → Folders, the sidebar's **Deep indexed** list) |
| What is read | the name, the folder, up to 10 Vision labels of a picture or a video's poster frame, the first pages of a PDF, the first kilobytes of a document (8 KB by default) | all the text of every file, scans by OCR, audio and video transcripts, tables, embeddings, English↔German matching, document facts (type, date, amount, counterparty) |
| How it ranks | one FTS5 table, BM25, labels and names first | keyword + meaning (vectors) + re-ranking, favourites, learning from what you open |
| When it runs | in the background, only when the Mac is idle (see **When it runs**) | live for changed files, heavy work in the night run |
| Cost per file | milliseconds | seconds for a scan or a recording |

A file in a deep folder is in both tiers: one search covers both, and the Quick Search panel lists the whole-disk hits
under **On this Mac**, after the file names and before **In your folders**. The main window's Mac mode shows them as
**Contents on this Mac**.

## What it indexes

Every file the whole-Mac name index (`mse/macindex.py`) lists under the roots is an entry with its name and folder.
These kinds also get a description, made in the background:

| Kind | Extensions | Description |
|---|---|---|
| image | jpg, png, heic, gif, tiff, webp … (the name index's image list) | Vision classification labels (1,303 identifiers, English, with their German equivalents added so "Hund" finds "dog"); pictures that look like text (a screenshot, a scan, a sign) are read with fast OCR |
| pdf | pdf | the text layer of the first two pages; a scan with no text layer is read by fast OCR of page 1; encrypted files are reported, not opened |
| doc | txt, md, csv, json (up to 1 MB), html, docx, odt, rtf, doc, eml, the QuickLook preview of .pages | the first `max_text_kb` kilobytes of text and the title or first heading |
| video | mp4, mov, m4v | labels of the poster frame at 10 % of the duration; nothing else is decoded |

Everything else (code, audio, archives, fonts, binaries, disk images) is found **by name only**. Size limits keep one
huge file from costing a read: 40 MB for a picture, 80 MB for a PDF, 20 MB for a document, 2 GB for a video; a file
over its limit stays searchable by name.

## What it never touches

The candidate list is the name index, so **everything the name index excludes never reaches this index**: credential
stores and key folders (`~/.ssh`, `~/.gnupg`, `~/.aws` …), secret file names anywhere (`.env`, `*.pem`, `*.key` unless it
is a Keynote deck, `id_rsa*`, `*.p12`, folders named `*credential*`, `*secret*`, `*password*` …), Keychains, Mail,
Messages, browser profiles, the system and cache folders, and every `mac_index.exclude` entry (`~/Library/` by default).

On top of that the whole-disk index skips:

- hidden files and folders (any path component starting with `.`),
- the insides of packages (`.app`, `.framework`, `.photoslibrary`, and the insides of `.pages`, `.numbers`, `.key`),
- build, dependency and cache folders (the same `prune_dirs` rules the deep index uses: `node_modules`, `.venv`,
  `DerivedData`, `target`, `build` next to a project marker …),
- nested git repositories,
- whatever `wholedisk.extra_excludes` names,
- iCloud placeholders (dataless files) and symbolic links — never read, never followed,
- a download still being written (`.crdownload`, `.part`, `.download`).

**Every path is checked again before it is described and again before a result is shown**, so a folder excluded a
moment ago disappears from the results at once, and a file deleted since the last look is never listed.

Nothing leaves the Mac. The index is one SQLite file, `wholedisk.db`, in the MySearch data folder; delete it and it is
rebuilt from the name index. The routes are not exposed through MCP, and `mse disk` refuses to run in a session locked
to one folder (`MSE_LOCK`).

## When it runs

The describer is one background thread. Before **every file** it asks the shared gate (`jobs.heavy_work_allowed`):

| `priority` | Runs when | Helper QoS |
|---|---|---|
| `low` (default) | on AC power, after 60 s without keyboard or mouse input | background |
| `balanced` | on AC power after 5 s without input, or on battery after 60 s without input | utility |

Both stop within one file when you come back (input within 5 s), and within one file when the switch is turned off.
The helper process starts once per batch of 16 files and is killed if it stays silent for 30 s; the file then counts as
failed and is tried again when it changes or after a rescan (three tries at most). Without the helper binary, pictures,
PDFs and videos simply wait, name-only, and the documents are still read.

Change tracking reads the name index and never writes there: every minute the rows added or touched recently are
checked, every hour (and after a settings change or a rescan) the two indexes are merged in path order, which also
drops the rows of deleted or newly excluded files.

## Settings

The `wholedisk` block of `config.json`, also Settings → **Whole-disk Search** in the app and `GET/POST /api/settings`.
The app always saves the whole block.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | the switch; off, nothing is read and the status says `paused` / `disabled` |
| `roots` | `["~"]` | the folders to look in; absolute, no duplicates, never empty |
| `extra_excludes` | `[]` | up to 200 gitignore-style patterns (no `!` negation), on top of the rules above |
| `priority` | `"low"` | `low` or `balanced`, see **When it runs** |
| `images` | `true` | describe pictures and videos |
| `documents` | `true` | describe PDFs and documents |
| `ocr_images` | `true` | read the text of pictures that look like text |
| `max_text_kb` | `8` | kilobytes of text kept per file, 1 to 64 |

A bad value is refused with a 400 and a sentence (`wholedisk.max_text_kb must be a whole number of kilobytes, 1 to 64`).

## CLI

```
mse disk status [--json]
mse disk search "<query>" [--kind image|pdf|doc|video|audio|code|other] [-n N] [--offset N] [--json]
mse disk rescan
```

```
$ mse disk status
whole-disk index: describing · 48,213 entries · 31,902 described · 16,311 waiting
  9,127 pictures labelled · 4,411 PDFs read · roots /Users/alex · 14.2 per s · about 19 min left

$ mse disk search "photo of a beach" -n 3
3 results · 6.1 ms · beach/coast/sand/sea/shore/… · kind image
/Users/alex/Pictures/Summer/IMG_4471.jpg  image · 2.4 MB · 2025-08-14 · beach, sand, sea, sky · label
/Users/alex/Pictures/Summer/IMG_4472.jpg  image · 2.3 MB · 2025-08-14 · beach, people, water · label
/Users/alex/Documents/Studio/strand-notizen.md  doc · 2 KB · 2025-06-21 · text
    Notizen vom Strand: die Flut kam um 17 Uhr, der Sand war noch warm.
```

Type words (`image`, `photo`, `pdf`, `document`, `Bild`, `Foto`, `video`) become a kind filter; stop words are dropped;
common labels are expanded English↔German (`Hund` finds `dog`). Piped output masks personal identifiers like every
agent-facing output. Without the service running, `status` and `search` read the index directly.

## API

| Route | Answer |
|---|---|
| `GET /api/disk/status` | `enabled, roots, files_seen, files_described, images_labelled, pdfs_described, docs_described, videos_described, failed, pending, state (idle · scanning · describing · paused), paused_reason (null · user_active · on_battery · disabled), rate_per_s, eta_s, last_error, priority, last_sync, last_reconcile, name_index_ready` |
| `GET /api/disk/search?q=&limit=20&offset=0&kind=` | `results: [{path, name, kind, size, mtime, snippet, labels, score, why (name · label · text · vector)}], has_more, next_offset, took_ms, query: {words, kinds}` |
| `POST /api/disk/rescan` | `{started: true}` — a full merge at the next turn and another try for the failed files |

The snippet is plain text (the text around the first matching word, or the labels). `tests/test_wholedisk_contract.py`
starts the real service and checks these key sets, and that the app's fixture stub (`scripts/fixtures/stub_service.py
--disk`) answers the same shape.

## How to add a deep folder

A folder you search often, or whose scans, recordings and tables you want read in full, belongs in the deep tier:

- Settings → **Folders** → **Deep-index a folder…** (the heading of the pane), or drop the folder onto the pane;
- the sidebar: the **+** in the **Deep indexed** header → **Deep-index a Folder…**, or right-click a folder in the
  sidebar → **Deep-index this Folder**;
- the main window in Mac mode: right-click a folder result → **Deep-index this Folder**;
- from the command line, the same `mse` command that adds a folder to the index (README → CLI); the app sends
  `POST /api/collections {"path": …}`.

The folder keeps its whole-disk entries too, so one search still covers both tiers. The deep index obeys the folder's
own rules (`.gitignore`, code files, excludes; README → Rules per folder).
