# MySearch

A private search engine for your own folders, running entirely on this Mac. It searches documents, PDFs (including scanned pages), Office files, e-mails, images, audio and video. English queries also find German documents. You use it from a native app, the CLI or AI agents (MCP). It runs in the background, indexes changes within seconds, and does heavy work at night.

## Why I built this

I run a small consulting business across two countries. My records are in English and German: contracts, invoices, tax letters, scanned letters, e-mails and meeting recordings. After losing a large part of my files, I rebuilt the archive from cloud copies and old exports. It came back as thousands of scattered files: duplicates, scans with no text layer, e-mail exports. Finding the right document became the real problem. Spotlight and Finder find file names and some text, but not "the letter where the tax office asked for a document" when the scan says it in German. Cloud search would have meant uploading private financial and legal records.

Before that, my documents had only a keyword-only index: it matched the words I typed. When I saw EmbeddingGemma 2, a small Apache-2.0 model from Google that places text, images, audio and video in one vector space, I saw the chance to build this properly. It runs on the Mac's GPU through MLX, in 8-bit, at about 1.2 GB, so a search that understands meaning no longer has to leave the machine.

So I built a search engine that:

- runs entirely on my Mac;
- searches by meaning and keyword, and English questions find German documents;
- reads scans (OCR), Office files, e-mails, audio and video;
- finds any file on the Mac by name, instantly, like Everything on Windows;
- feels like Finder.

I also wanted AI agents (Claude Code, Cursor, Codex) to search my documents through MCP, with personal identifiers masked; search never leaves the machine (the optional Ask feature can use a cloud model and is off by default). Apple Silicon makes this practical: embedding, re-ranking and speech models run on the GPU. Freelancers, small businesses and anyone with a paper trail in two languages have the same problem, so I open-sourced it: local search should be something you can read, run and change. It is for Mac users with private documents, often in more than one language, who would rather not upload them.

I wrote about why and how I built it on my blog: [EmbeddingGemma 2 and local search](https://www.heyitsaif.com/blog/embeddinggemma-2-local-search).

## Quick start

Run [`./scripts/setup.sh`](scripts/setup.sh), then follow [docs/INSTALL.md](docs/INSTALL.md).

To see how it works inside, read [How MySearch works](docs/TECHNICAL.md).

## What it does

| | |
|---|---|
| Search | Keyword (SQLite FTS5, umlaut-insensitive) + meaning (EmbeddingGemma 2) + an EN↔DE bridge (each folder's own vocabulary, embedded), merged with weighted reciprocal-rank fusion |
| Files | Markdown/text/code, PDF (text layer + Apple Vision OCR for scans + page images), Word/RTF/ODT (`textutil`), Excel, PowerPoint, e-mail (.eml), images (OCR + picture embedding), audio and video (Whisper transcripts with timestamps + audio/frame embeddings) |
| Results | One entry per document: identical copies, `.eml`/`.md` versions, digests and backups fold into "N versions". Highlighted snippets; page or timestamp jump; thumbnails |
| Scope | One folder, several (`work,notes`), a saved **group** (`work` = `notes` + `projects`, works like a folder tab) or All (every folder marked Include in All). Groups search exactly their members, also folders left out of All. Short **aliases** (`w`) and a **shortcut in the query** (`w: invoice`, `invoice in:work`): see [Choosing where to search](#choosing-where-to-search) |
| Filters | Type, date, **files to include / exclude** (comma-separated globs, like VS Code: `*.jpg`, `*.md, backups/`), plain-language dates ("last month", "im September", "invoices from March 2025") |
| More | Find similar (by meaning, or visually for images), search by dropping an image, a cross-encoder that re-checks near-ties (agents/CLI by default, `--fast` / `precise:false` to skip; the app shows results instantly and re-orders a moment later) |
| Freshness | macOS FSEvents → changed files indexed ~3 s after they settle; up to 5 new scans/photos get OCR + picture embedding at once; everything else in the night run (02:30, AC power) or on "Sync now" |
| Favourites | Mark the right file for a query (several allowed) and it comes first next time; hide noise for a query; very similar queries (cosine ≥ 0.90) give favourites a score boost. `mse fav`, the app, or the MCP tool `mark_best` |
| Learning | After every night sync: known-item questions are built from the index, quality is measured on four sets (the gold sets in `eval/`, favourites, known items) into `quality.jsonl`, the ranking weights and re-ranker knobs are re-tuned, and applied only if the guardrails pass (weighted MRR +0.01, no hand-written gold set loses >0.01 MRR or any rank-1 hit, favourites not worse). `mse tune --rollback` undoes a change |
| Ask (optional) | A short answer (2–5 sentences) to a question, written from the top search results only, each claim followed by a numbered citation that opens the document at its page or timestamp; says plainly when the documents don't answer. **While online a large cloud model writes it** (Claude by default, or any model behind an OpenAI-compatible endpoint; personal identifiers masked before anything leaves), **offline the local model does**. Folders can be kept local. Metadata-only files are never read, and one answer reads one folder unless you ask across `all`. **Off by default**: while off nothing is called or loaded. Settings → Ask, `mse ask`, `mse providers`, MCP tool `ask` (identifiers masked). See [Cloud models for Ask](#cloud-models-for-ask) and [docs/ask-models.md](docs/ask-models.md) |
| File names | **Find a file by its name anywhere on the Mac, instantly** (fsearch-style): `mse files "invoice 2026"`, `/api/files/search`. A name index of the whole home folder, kept current by FSEvents (~0.3 s), typo-tolerant, with `ext:` `type:` `size:` `mtime:` `path:` `re:` `in:` filters; p50 about 5 ms in-process (9 ms end to end at `limit` 200) over 2 million entries. Names only, never contents, and never for agents. See [Find files by name](#find-files-by-name-whole-mac) |
| Agents | MCP tools `search`, `get`, `multi_get`, `similar`, `collections`, `mark_best`, `sync`, `ask`; agents search the folder they work in unless they ask for `all`, a group or a list (or only that folder, with `MSE_LOCK=1`); personal identifiers are masked in everything sent to agents. `mse agents install <folder>` connects Claude Code, Cursor, Codex, Grok and OpenCode to a folder in one step |

## Models (all local, all converted here from official weights)

| Role | Model | Size | Loaded when |
|---|---|---|---|
| Embeddings (text, image, audio, video) | Google EmbeddingGemma 2, 8-bit MLX | 1.2 GB (text-only copy 320 MB) | searching (text copy); night run (full) |
| Speech to text | Whisper large-v3-turbo, 8-bit MLX | 829 MB | night run / new recordings |
| Re-ranking (close calls) | Qwen3-Reranker-0.6B, 8-bit MLX | 615 MB | when the top results are near-ties: agents and CLI by default, the app refines its list a moment after showing it |
| Answers (Ask), offline | Qwen3-4B-Instruct-2507, 4-bit MLX | 2.1 GB on disk; ~2.5 GB loaded, up to ~3.8 GB while answering | only while Ask is switched on and an answer is written locally: with the first such question (~3.5 s), stopped after 5 idle minutes or at once when switched off. Never loaded for an answer a cloud model writes |

Each model runs in its own worker process, started on demand and stopped after 10 idle minutes (Ask: 5). The background service itself never loads ML code. Ask's cloud models (not local, not in this table) are described in [Cloud models for Ask](#cloud-models-for-ask).

## Measured on this Mac (M4 Pro)

**Search quality** on 28 questions about a private folder of a few hundred business documents (EN/DE), run with `mse eval` on a hand-written gold set that is not published:

| Setting | Right file first | In top 3 | In top 10 | MRR |
|---|---|---|---|---|
| First version (equal weights) | 0.68 | 0.89 | 0.96 | 0.81 |
| **Current default** (tuned weights, overview files ×0.6, versions grouped) | **0.82** | **0.96** | **1.00** | **0.90** |
| Meaning only | 0.82 | 0.93 | 0.96 | 0.87 |
| Keywords only | 0.57 | 0.64 | 0.79 | 0.63 |
| **+ re-ranker** (close calls only, rank fusion, 10 docs × 128 tokens) | **0.89** | **1.00** | **1.00** | **0.95** (+0.25–0.45 s when it runs, cached for repeats) |
| first re-ranker attempt (raw-probability blend, 20 docs × 640 tokens) | 0.79 | 0.96 | 1.00 | 0.88 (≈3 s/query, replaced) |

**Engine and resources:**

| What | Result |
|---|---|
| 8-bit vs BF16 embeddings | cosine 0.9996, 99.6% identical top-10 |
| 1-bit search + 16× exact re-scoring vs exact | 99% identical top-10, ~10× faster |
| Embedding cache | re-embedding an unchanged folder: 6 s instead of 320 s |
| Live indexing | new/changed/renamed/deleted files reflected in ~3.6 s |
| Memory | service 50–170 MB; text model ~0.8 GB while loaded; re-ranker ~0.9 GB while loaded; 1.5–2 GB during heavy work |
| CPU | ~0% idle; the GPU does the model work, the CPU stays at 30–50% of one core while embedding |

## Layout

| Path | What |
|---|---|
| `mse/` | Python package. `server.py` (HTTP + UI + MCP), `macindex.py` + `filequery.py` (whole-Mac file names: privacy rules, crawl, FSEvents, query language, ranking), `tree.py` (folder tree for the Ask picker), `jobs.py` (queue persisted across restarts, scheduler), `watcher.py` (FSEvents), `indexer.py`, `extract.py`, `search.py`, `worker.py` (MLX), `ask.py` (Ask: passages, prompt, cloud-or-local route), `providers.py` (Ask's cloud providers, Keychain keys, online check), `cloud.py` (streaming Claude / Chat Completions / Responses clients, standard library only), `redact.py`, `mcp.py`, `cli.py`, `agents_setup.py` (agent MCP configs per folder) |
| `mse/ui/` | The app interface: vanilla HTML/CSS/JS, no build step, no npm |
| `helper/msehelper.swift` | Native helper: PDF text, page rendering, Vision OCR, image normalisation, FSEvents, the `crawl` walker (getattrlistbulk) for the file-name index, Keychain items for Ask's provider keys |
| `app/` | The native Mac app (AppKit/SwiftUI, Finder-style list, column and icon views; the main window is `app/MainWindowController.swift`), global hotkey ⌃⌥Space |
| `eval/` | Gold sets for `mse eval` |
| `tests/` | `.venv/bin/python -m unittest discover -s tests` |

Data lives in `~/Library/Application Support/MySearch/` (folder mode 0700):
- `config.json` (settings, folders, groups), `queue.json`, `state.json`
- one database per folder in `collections/<name>.db`
- `macindex.db` (the whole-Mac file-name index, see below; derived data, safe to move aside)
- `models/`, `thumbs/`, `tmp/`
- `trash/` (indexes of removed folders)
- `feedback.db` (favourites and hidden results), `quality.jsonl` (search-quality history), `auto_gold.json` (known-item questions)

Logs go to `~/Library/Logs/MySearch/`. Indexed folders are only ever read.

## Rules per folder

When a folder is added, its rules can start from a presets file, `presets.json` in the data folder, keyed by the
folder's name. The format is in [docs/presets.example.json](docs/presets.example.json). Without the file nothing is preset.

- **Skip these paths:** never read or listed.
- **File names only:** contents are never read.
- **Ask in the cloud** (`ask_cloud`, on by default): off keeps every Ask question whose scope includes this folder on the local model, whatever the route (`mse set <folder> ask_cloud=false`, or the folder card).
- **Always skipped everywhere:** credentials and secrets (`*credential*`, `*recovery*code*` and `*secret*` folders and files, `Keychains`, `.ssh`, `.gnupg`, `.env`, keys, certificates, `*password*`), hidden folders, and the agent configs `mse agents install` writes (`.mcp.json`, `.cursor/`, `.claude/`, `.codex/`, `.grok/`, `opencode.json`).
- **Include in All:** whether the folder takes part in global search.
- **Contexts:** descriptions of sub-folders that agents receive with results.
- **Media switches:** images, PDF page images, audio, video, transcription.

## Choosing where to search

| Where | How |
|---|---|
| App | The tabs (All, groups, folders); ⌘-click tabs to search several folders; **Folders ▾** next to the tabs opens a menu with a search, every group (pick one) and every folder (tick several; All / None). Quick Search: the ▾ button or ⌘F |
| Raycast | The dropdown next to the search bar; groups show what you can type for them ("Work · work, w") |
| CLI / API / agents | `-c`, `c=`, the MCP `collection` argument: names, groups, aliases and `all`, comma separated |
| **Anywhere** | a shortcut in the query itself, which overrides the tab, the dropdown or `-c` for that search |

The shortcut:

```
work: invoice                 the folder or group "work" (its name or an alias), then a colon and a space
w,notes: invoice         several at once
w:"exact phrase"             the quotes stay: the rest is your query exactly as you would type it
invoice in:work               in:<name> anywhere in the query (also in:w,notes and in:"work")
all: invoice                  everything marked Include in All, whatever tab is selected
```

- Names are case-insensitive and count only when they are known: `Note: the meeting` and `Re: Rechnung` stay ordinary text. An `in:` naming nothing known is an error (the app and Raycast wait while you type it).
- The search filters `type after before path ext include exclude in kind has` and `all` are reserved: no folder, group or alias can be called that.
- **Aliases**: in Settings › Folders (a folder's *Short names*, a group's editor), or `mse alias add w work`, `mse alias rm w`, `mse aliases`. Each alias is unique across every folder name, group name and alias.
- **Groups without a tab**: switch *Tab* off in Settings › Folders › Groups (or `mse group hide work` / `show`). The group stays in the Folders ▾ menu and in Raycast, and works by name and alias.
- **Folders**: *In All* and *On* can be switched on each folder card, also while it is closed.
- In the app, a recognised shortcut is marked in the search field and shown as a chip (`in Work ×`; click it to take the shortcut out); typing `wo` and then `:` (or `in:`) suggests the names that fit, ⇥ or ↵ takes one.
- For agents the shortcut goes through the same lock as `collection`: under `MSE_LOCK`, `notes: x` is refused like `collection: notes`.

## CLI

```
mse search "what did we spend on software last quarter"             # all folders marked Include in All
mse search "receipt" -i "*.jpg,*.png" -c work             # only pictures
mse search "requirements document" -c work,notes                      # two folders at once
mse search "requirements document" -c work                            # a saved group (mix freely: -c work,notes)
mse group add work notes projects --label Work | mse group set work notes | mse group rm work | mse groups
mse search "w: requirements document" | mse search "invoice in:work"    # the shortcut: overrides -c
mse alias add w work | mse alias rm w | mse aliases           # short names for folders and groups
mse group hide work | mse group show work                    # no tab in the app (still searchable)
mse search "invoice" -x "*.eml,*.md" --json                # exclude raw mails and notes
mse search "invoices last month" --fast                   # plain-language date; skip re-ranking
mse ask "Wann wurde die Rechnung bezahlt?" -c work [--json]   # cited answer (Ask switched on)
mse ask --follow-up "Und wie viel hat sie gekostet?"      # a follow-up to the last mse ask (same scope unless -c)
mse ask "…" --local | --cloud                             # this question only: never leave the Mac | cloud, no fallback
mse providers                                             # cloud providers, keys (stored or not), status, models
mse providers set-key anthropic                           # hidden prompt → Keychain (or: … < file, --from-env)
mse providers use deepseek deepseek-chat                 # the active cloud model | mse providers use local
mse providers test anthropic | models deepseek            # one tiny request | refresh the model list
mse providers add deepseek --base-url https://api.deepseek.com/v1 --model deepseek-chat --key
mse providers add myrouter --kind openai-responses --base-url http://127.0.0.1:8080/v1 --key-file ~/.config/myrouter/key
mse providers remove deepseek --forget-key | forget-key anthropic
mse similar '#4319a060'                                   # documents like this one
mse get '#4319a060' --page 2
mse add ~/Documents/Foo | mse remove foo | mse list
mse set notes in_global=false --metadata-only "scans/*.jpg"
mse sync work --wait | mse sync notes | mse sync --path ~/Documents/x.pdf
mse similar '#4319a060' --in work                         # look for similar files across a group
mse eval eval/gold_example.json -c work --configs hybrid,semantic,rerank -v
mse fav "Acme invoice" '#87e569f1' | mse fav "x" '#…' --hide | --remove ; mse favs
mse eval --feedback --configs hybrid,favourites          # favourites as questions, feedback application off
mse tune [--apply | --rollback] | mse learn [--dry-run] | mse quality
mse status | mse doctor | mse ui | mse service install|uninstall|restart|status
mse add ~/Documents/Foo --agents | mse remove foo --agents   # also connect / disconnect the folder's agents
mse agents status [foo | --global] | mse agents install foo|--all|--global | mse agents remove foo|<path>
```

`-c` (and `--in`, the MCP `collection` argument, the API's `c` / `in`) takes collection names, group names, aliases and `all`, mixed and comma separated. A shortcut in the query (`w: …`, `in:w`) overrides it; the API applies it unless `shortcuts=0` (`{"shortcuts": false}` for `/api/ask`), and reports it in the response's `scope.shortcut`. Groups expand to their members, duplicates are removed, unknown names are an error. In `eval`, `tune` and `learn` a group expands to its folders; in `sync`, a group syncs each member and `all` means every folder.

When the CLI's output is piped (agents, scripts), personal identifiers are masked. In an interactive terminal you see everything.

## Ask

Switch it on in Settings → Ask (or `POST /api/settings {"ask": {"enabled": true}}`; `false` unloads the local model at
once). A question runs a precise search, takes the best-matching passage(s) of the top documents (up to 8, about 3,000
tokens, metadata-only files never), and streams an answer with `[n]` after each claim: from a cloud model while online
(see [Cloud models for Ask](#cloud-models-for-ask)), else from Qwen3-4B-Instruct-2507 on this Mac. Citations that point
at no passage are removed before they are shown.

- **App:** the **Search | Ask** switch next to the logo (⌘J) opens Ask, a chat: your questions on the right, answers
  streaming on the left with `[n]` chips that open the passage in the preview at its page or timestamp, the sources
  under each answer, Stop / Copy / Retry, **New chat** and a list of recent chats (kept on this Mac, the last 30).
  Follow-ups send the last three turns as `history`; a chat started with a shortcut (`w: …`) keeps reading there.
  The tabs and Folders ▾ choose where it reads. While Ask is off, the view has a one-click **Turn on Ask** button.
  From a search, ⌥↵ (or **Ask about this**) asks it in a new chat. Quick Search: ⌥↵ answers in the panel; ⇥ or ⌘O
  continues that answer as a chat in the main window.
- **API:** `POST /api/ask {q, collections?, filters?, history?, route?}` streams NDJSON: `sources`, then `token`s, then
  `done` (`citations`, `model`, `provider`, `where`, `fallback_reason?`, `redacted`, `timings`, `answer`, `answered`,
  `search_query`, `rewrite`) or `error`; `409 {"error": "ask_disabled"}` while off. `GET /api/status` → `ask:
  {available, enabled, loaded, local_model, ram_mb, route, provider, model, online, where_next, next_model, training, training_confirmed}`.
- **Follow-up questions:** `history: [{q, a}]` = the last turns, oldest first, at most 3 (each string cut to 2,000
  characters; anything malformed is a 400). A follow-up such as "and when was it paid?" or "what about the printer?" is
  searched for what it means: the loaded model rewrites it in one short generation (≤ 40 tokens, at most 1.5 s), or,
  when the model is not loaded, too slow or its output unusable, earlier key terms are added to it. `done.search_query`
  is what was searched; `done.rewrite` says how (`none`, `heuristic`, `model`). The earlier turns go into the prompt
  compactly (answers ≤ 60 words, without their `[n]`), taken from the same 3,000-token budget; citations refer only to
  the current passages. History never changes the scope.
- **CLI / agents:** `mse ask "…"` (masked when piped), `mse ask --follow-up "…"`, and the MCP tool `ask` (masked;
  default scope = the working folder; under `MSE_LOCK` only that folder; `history: [{q, a}]` for follow-ups). Agents get
  the same route as the app: the cloud model while online, the local one offline. Grok Bot and the other agents
  connected with `mse agents install` need nothing new.
- **Model choice and measurements:** [docs/ask-models.md](docs/ask-models.md). Settings that can be overridden in
  `config.json` under `"ask"`: `route` (cloud), `local_fallback` (false), `provider` (anthropic), `model` (claude-opus-5-5), `cloud_redact`
  (true), `cloud_max_tokens` (8192), `cloud_effort` (low), `cloud_timeout_s` (60), `allow_training_models` (none), `local_model` (the Qwen folder),
  `max_tokens` (300), `passages` (8), `context_tokens` (3000), `idle_unload_min` (5), `temperature` (0.1),
  `rewrite_model` (true), `rewrite_budget_ms` (1500). Before cloud models, `model` held the local model's folder; such a
  path is still read as `local_model`.

## Cloud models for Ask

**Where an answer is written** (`ask.route`, Settings → Ask, `mse providers use`, or per question `--local` / `--cloud`
and `route` in `/api/ask`). **The model on this Mac loads only when you ask for it.** Not by turning Ask on, not by
starting the app, not by a cloud failure:

| Route | What answers | Cloud model cannot answer |
|---|---|---|
| `cloud` (default) | the model chosen in the menu | an `error` event: "No cloud model is reachable (…). Switch this chat to Local to answer on this Mac, or choose a model: `mse providers use <id> <model>`" |
| `cloud` + `local_fallback: true` ("Use the local model when no cloud model is reachable", off by default; the old route `auto` is read as this) | the model chosen in the menu | the local model answers instead, silently (`done.fallback_reason`) |
| `local` | the local model | – |

The local model also answers when the chat's **Local** switch is on, with `mse ask --local`, and for a folder with
`ask_cloud: false` in scope (shown as the reason). The Ask view has one **model menu** next to the message box (and at the
top of Settings › Ask): it lists only providers you have connected (a key, a key file, or a server that needs none)
grouped by provider and, where a provider's ids carry a prefix such as `deepseek/…`, by family, with the local models last; choosing a cloud
model sets provider, model and route cloud in one step, choosing a local model sets route local. There is no banner above
the thread; with nothing connected a quiet line says "No cloud model connected · Set up in Settings › Ask".

"Online" is a TCP connect to the active provider's host (1.5 s at most, never an API call), remembered for 60 s; a
failed cloud call marks it offline at once, so the next question goes straight to the local model. With no key stored,
a cloud route is an error (or, with `local_fallback`, a local answer with `fallback_reason: "no_key"`). The local model is
never loaded for an answer the cloud writes.
A follow-up question is rewritten by the cloud model when the answer goes there (one short call, 1.5 s budget, else the
heuristic). Every answer says who wrote it: `done.where`, `done.provider`, `done.model`.

**What leaves the Mac, and what never does**

- With `cloud_redact` (default on), personal identifiers (IBANs, passport / ID / permit / tax / social-security
  numbers, dates of birth, MRZ lines; `mse/redact.py`) are masked in the passages and the earlier turns before the call;
  `done.redacted` counts them. The question itself is sent as typed. The app still shows you the full sources.
- A folder with **`ask_cloud` off** keeps every question whose scope includes it on the local model, whatever the route
  (`mse set notes ask_cloud=false`, `POST /api/collections/<name> {"ask_cloud": false}`). Asking across `all` while
  such a folder is in All is local, too.
- Metadata-only files are never read, so never sent. Agents' answers stay masked as before.
- Keys travel only in a request header, never in a URL; redirects are refused; a key is sent over https, or over http
  only to this Mac.

**Providers**

| Id | Kind | Endpoint | Key |
|---|---|---|---|
| `anthropic` (built in) | Claude API, Messages, streaming | `https://api.anthropic.com/v1` | an Anthropic API key |
| custom | `openai-chat` (Chat Completions) or `openai-responses` | any OpenAI-compatible base URL: DeepSeek (`https://api.deepseek.com/v1`), OpenRouter (`https://openrouter.ai/api/v1`), LM Studio (`http://127.0.0.1:1234/v1`), Ollama (`http://127.0.0.1:11434/v1`), a router on this Mac (`http://127.0.0.1:8080/v1`) | its API key, in the Keychain or in a key file; none needed on this Mac or the local network |

A provider is an entry under `ask` → `providers` in `config.json` (add one with `mse providers add`, the Add provider
button, or `POST /api/ask/providers`). Besides `kind`, `label`, `base_url` and `models`, every provider takes these
optional fields (the built-in `anthropic` too):

| Field | What it does |
|---|---|
| `key_file` | Path of a local file that holds the API key (`/abs/path` or `~/…`; one line of printable characters). The key is read when a call needs it, kept in memory only and read again when the file changes (so a rotated key is followed), and never copied into `config.json`, logs, API answers or the Keychain. Like any key it is sent only to this provider's `base_url`, over https or to this Mac. A key in the Keychain wins over the file. A provider with a `key_file` needs a key (it is "not ready" while the file is missing). `mse providers connect <id>` / **Connect** copies the file's key into the Keychain once and lists the models. |
| `training_models` | Glob patterns (`*` any characters, `?` one, case-insensitive) of the model ids whose provider may use inputs for training, e.g. `["*-train"]`. See the training warning below. |
| `auto_models` | `true`: the model list is fetched once by itself the first time the provider is looked at (a key is at hand and the server answers), so the picker holds every model it offers without **Refresh list**. A failed attempt is repeated after a minute. |

A provider on this Mac that is not running shows `status: "offline"` with the hint "Start the server on this Mac
(host:port)".

**Model lists and the picker.** Each provider card (Settings › Ask) and the Ask view's cloud card have a searchable model
picker with every model the provider lists, grouped where that helps (by id prefix: `deepseek/`, `anthropic/` …;
Claude by family) and **Use a model id…** for any other id. **Refresh list** asks the provider for
all it offers and shows the count and the time: for Claude it follows `GET /v1/models` page by page with your key and
offers exactly what the API returns (no id is added from memory; before the first refresh the picker shows the four
current ids: `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`, `claude-fable-5-1`); for an OpenAI-compatible
provider it reads `GET /models` (`data[].id`). `mse providers models <id>` prints the full list (fetched; `--cached` prints the stored
one), `mse providers models local` the answer models installed on this Mac. For Claude the default is `claude-opus-5-5` at `effort: low` (short cited answers), with Anthropic's
server-side refusal fallback switched on (`fallbacks: "default"`) and dropped automatically should the API refuse it.

**Keys: the macOS Keychain only.** Each key is a generic password in your login Keychain, service
`com.mysearch.provider`, account = the provider id (Keychain Access shows it as "MySearch Ask: <id>"). The native helper
writes and reads it and takes it on stdin, so a key is never in a process list, `config.json`, a log, an API response
or the app. The service reads a key only to make a call, without Keychain dialogs. After rebuilding the helper
(`./scripts/build_helper.sh`) macOS no longer recognises the new binary for the old items: store the keys again (or
allow access when asked). For the CLI alone, `ANTHROPIC_API_KEY` and `MSE_KEY_<ID>` (the provider id upper-cased,
non-letters as `_`) work as a fallback when the service is not running; the background service never reads the
environment.

- **Claude key:** Settings → Ask → Claude → paste the key, or in a terminal `mse providers set-key anthropic` and paste
  at the hidden prompt (or `mse providers set-key anthropic --from-env` to copy `ANTHROPIC_API_KEY` from your shell).
  Then `mse providers test anthropic`.
- **A key file: nothing to paste.** For a server on this Mac that keeps its key in a file, give the provider a
  `key_file`: `mse providers add myrouter --kind openai-responses --base-url http://127.0.0.1:8080/v1 --key-file
  ~/.config/myrouter/key --auto-models`. With no key in the Keychain, MySearch reads that file when a request needs it
  (see the table above). `GET /api/ask/providers` says where a key comes from (`key_source`: `keychain` | `file` |
  `none`; a key in the Keychain wins) and returns the configured `key_file` path, never its content. The card shows
  "Key read from its file"; "Use a different key…" remains for odd setups. `mse providers` prints "key: from
  <key_file>". **Connect** (Settings › Ask, or `mse providers connect myrouter`) does one more thing: the *service*
  reads the file, stores the key in the Keychain through the helper (where it then wins, so run `forget-key` to go
  back to the file), never returns, logs or prints it, and lists the provider's models.
- **Custom provider:** `mse providers add deepseek --base-url https://api.deepseek.com/v1 --model deepseek-chat --key`
  (asks for the key), then `mse providers use deepseek deepseek-chat`. Local servers need no key:
  `mse providers add ollama --base-url http://127.0.0.1:11434/v1 --model qwen3:8b`.

**Models the provider may train on: "The provider may use inputs for training — check the provider's terms".** Some
providers offer a cheaper tier whose inputs and outputs they may use for training. Mark those model ids with the
provider's `training_models` patterns (`mse providers add myrouter --training-models '*-train'`; the pattern belongs to
that provider, so the same id at another provider is not marked). The picker marks matching models with a warning badge, choosing one asks
for a confirmation once, and the id is stored in `ask.allow_training_models`; the API (`POST /api/settings`) and
`mse providers use <id> <model>` refuse such a model until it is confirmed (`--allow-training`, or the list sent along).
A model of that kind that is not confirmed never answers (even when `config.json` is edited by hand: the question is
answered locally, or is an error under route Cloud). Answers written by one carry `done.training: true` and the badge.
**Folders with `ask_cloud: false` stay local regardless**, confirmed or not.

**The model on this Mac.** Every way to keep an answer local is still there: Settings › Ask route (Auto / Cloud only /
Local only), the Auto / Local switch above a chat's question, `mse ask --local`, the folder's *Allow cloud answers*
switch, and the automatic offline fallback. Settings › Ask also has a **Model on this Mac** picker: it lists the MLX
answer models in `<data>/models` (detected by their `config.json` and weights, e.g. `qwen3-4b-instruct-2507-4bit`,
`qwen3.5-4b-4bit`; embeddings, re-ranker and speech models are left out), sets `ask.local_model` and unloads the running
Ask worker, so the next local answer uses it. CLI: `mse providers use local <model>` (`mse providers use local` alone =
route Local only). `GET /api/ask/local-models` -> `{current, models: [{name, size_mb, current}]}`. The ~2.5 GB of memory
is named only where the local model will answer.

**API** (all behind the usual token; never returns a key or any part of one):

```
GET  /api/ask/providers                 {route, provider, model, online, providers: [{id, label, kind, base_url,
                                          builtin, has_key, key_source, key_file, training_models, auto_models, needs_key,
                                          models, status: ok|no_key|offline|error, error?, hint?}]}
POST /api/ask/providers                 {id, label?, kind?, base_url?, key?, models?, forget_key?, key_file?,
                                          training_models?, auto_models?} -> the row
POST /api/ask/providers/<id>/remove     {forget_key?}   custom providers only
POST /api/ask/providers/<id>/models     refresh the model list from the provider (every page) -> {models, fetched, at, ms}
POST /api/ask/providers/<id>/connect   a provider with a key_file: the service reads the file, stores the key in the Keychain, lists the models
GET  /api/ask/local-models              installed local answer models
POST /api/settings                      also {"ask": {local_model, allow_training_models}}
POST /api/ask/providers/<id>/test       {model?} -> {ok, model, ms, error?}   one tiny request
POST /api/settings                      {"ask": {route, provider, model, cloud_redact}}
```

## Find files by name (whole Mac)

The collections above search what is *inside* your folders. This is the other half: **where is the file called …?**,
anywhere on the Mac, in a few milliseconds, before you narrow it down to your indexed folders. It only ever indexes
**names, sizes and dates** (never contents) in `macindex.db`, a SQLite file with an FTS5 trigram index.

### Using it

    mse files "invoice acme"               # every file under your home folder (--scope mac, the default)
    mse files "mian ext:py" --json         # typos forgiven: finds main.py
    mse files "report type:pdf mtime:<7d size:>1mb in:~/Documents"
    mse files "report" --scope indexed -c work     # only the folders of the work group
    mse files status                       # entries, crawl progress, Full Disk Access
    mse files crawl [--force]              # start a crawl now (the service also does it, see below)
    mse files exclude add ~/Music node_modules   # skip a folder / a name anywhere; also: rm, list [--all]
    mse files roots list                   # where it looks; also: add <folder>, rm <folder>

Query language (fsearch's, plus a few more; the full text is at the top of `mse/filequery.py`):

| Write | Means |
|---|---|
| `invoice 2026` | every word is in the name; words are fuzzy: substrings match, and **a word of 5+ letters forgives one typo** (insert, delete, substitute, swap: `recieve` finds `receive`), **a word of 4 letters forgives swapped neighbours** (`mian` finds `main`); shorter words are literal. Typos are looked for only when the literal search finds fewer results than asked for, and rank last |
| `'exact` · `"two words"` | exactly that text, no typos |
| `^prefix` · `suffix$` · `^whole$` | starts with · ends with · is the whole name (`.pdf$` is `ext:pdf`) |
| `!word` · `!ext:tmp` · `!type:image` · `!path:cache` | must not match |
| `ext:pdf,docx` | extensions |
| `type:` / `kind:` | `folder file image pdf doc sheet slides audio video code archive app email font other`; an unknown word is an extension (`type:swift`) |
| `size:>5mb` `size:<1kb` `size:1mb..2gb` `size:=0` | file sizes (1 KB = 1024 bytes; folders have none) |
| `mtime:<7d` | modified in the last 7 days (`5h` `30min` `2w` `3mo` `1y`); **`mtime:>30d` is older than 30 days** |
| `mtime:>2026-01-01` | on or after that day; **`mtime:<2026-01-01` is before it**; `2025-03` is that month, `2026` that year, `2026-01..2026-03` a range, `today`, `yesterday` |
| `path:work/notes` · `docs/readme` | the full path contains it · `docs/readme` is `path:docs/` plus the name `readme` |
| `re:^IMG_\d+` | a regular expression on the name (repeated groups like `(a+)+` are refused) |
| `in:~/Documents` · `in:"/Users/alex/My Folder"` | under that folder. **A value starting with `~` or `/` is a folder path; any other value names a MySearch folder, group or alias** (`in:work`, and `work: invoice` at the start, behave as in [Choosing where to search](#choosing-where-to-search) and switch to `--scope indexed`) |
| `limit:50` | at most 50 results |

Ranking: exact name (with or without extension) > prefix > word boundary > substring > typo; ties by how much of the
name your words cover, recency and folder depth; a folder gets a small boost on an exact match. One- and two-letter
queries match name prefixes only.

### API (the app and the web UI use these)

`GET /api/files/search?q=&limit=200&scope=mac|indexed&c=<collections/groups>` ->

    {"items": [{"path", "name", "parent", "is_dir", "kind", "ext", "size", "mtime", "ctime", "score",
                "match": [[start, end], ...], "collection"?}],
     "total", "ms", "scope": "mac" | "indexed", "truncated": bool,
     "query": {"words": [{"text", "mode": "fuzzy|exact|prefix|suffix|whole", "not": bool}], "filters": {...}},
     "shortcut"?: {"names", "tokens"}}

`match` is character ranges in `name` (the displayed name, accents and all) to highlight. `kind` is one of the type
words above; `mtime` / `ctime` are Unix seconds (`ctime` is the creation time). `collection` is set when the file is in
an enabled indexed folder. `total` counts the matches among the candidates the plan looked at (`truncated: true` says it
stopped early on a very common term). **`searchable: false` means the name index does not exist yet** (the service
has just started, or the first crawl is still loading): the list is empty because nothing is indexed, not because
nothing matches, and `GET /api/files/status` has the progress (`progress.fraction`, 0 to 1). `scope=indexed` restricts to the folders of the given (or all enabled) collections
and groups. Errors in the query (bad regex, bad size or date, unknown folder name after `in:`) are `400 {"error"}`.

`GET /api/files/status` -> `{"enabled", "roots", "entries", "crawling", "progress": null | {"phase", "units_done",
"units_total", "entries", "fraction", "current", "elapsed_s"}, "last_update", "full_disk_access": true|false|null,
"excluded": [patterns], "state": "disabled|crawling|ready|not crawled|waiting", "searchable", "last_crawl",
"helper_present", "watching", "events", "skipped_protected", "denied", "error", "python_path", "python_real_path",
"exclude_entries": [{"entry", "kind": "folder|pattern", "path", "note"?, "invalid"?}], "hard_excluded": [{"label",
"items"}]}` (the last three are for the settings screens, see "Managing what it skips"). `POST /api/files/crawl
{"force"?}` -> `{"started", "reason"?}`.

`GET /api/tree?c=<collection>&path=<relative folder>` -> `{"collection", "path", "folders": [{"name", "path",
"children": bool}]}`: the immediate subfolders of an indexed folder for the Ask folder picker. Folders the collection's
rules exclude (and anything the privacy rules below block) are never listed; `..`, symbolic links and unknown folders
are refused (400/404). `POST /api/ask` accepts `"path"` (a folder relative to the collection, or a `~/…` / `/…` folder; also read as
`filters.path`, which is how the web UI sends it, and always a whole-folder scope, never a name prefix)
next to `collections` to ask inside one subfolder, and its NDJSON stream now also carries
`{"type": "step", "kind": "rewrite|search|read|model", "text", "count"?, "ms"?}` events as the work happens
("Searching work › Invoices for …", "Found 12 results", "Reading 6 passages from 4 documents",
"Asking deepseek/deepseek-chat"); everything else in the stream is unchanged.

### Privacy (binding)

Enforced in Python on every path before it is stored and again before it is returned; the helper prunes the same
rules while it walks only to go faster.

* **Never indexed, not even as a name, and not configurable:** everything matching `ignore.SECRET_PATTERNS` (`.env`,
  keys, certificates, `*credential*`, `*secret*`, `*password*`, recovery codes …),
  `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config/gcloud`, `~/.kube`, `~/.azure`, `~/.docker`, `~/Library/Keychains`,
  `~/.codex` and any `codex-*` folder, `~/.claude*`, browser profiles (Chrome, Brave, Edge, Arc, Firefox, Vivaldi, Opera,
  Chromium, Safari), Mail, Messages and `~/Library/Containers` / `Group Containers`, plus system and cache noise:
  `/System`, `/private/var`, `/Library/Caches`, caches and logs, `node_modules`, `.git`, `.venv`, `DerivedData`,
  Trash, Time Machine.
* **Excluded by default, yours to change** (`mac_index.exclude`): `~/Library/`. A root listed in `mac_index.roots` inside an excluded folder (say `~/Library/CloudStorage`) is
  crawled; the hard rules above still apply.
* **Settings** (`config.json` → `mac_index`, or `POST /api/settings {"mac_index": {...}}`): `enabled` (true),
  `roots` (your home folder by default), `exclude`, `skip_protected` (`auto` | `always` | `never`), `rescan_every_h` (24).
  How to change `roots` and `exclude` without editing the file: "Managing what it skips" below.
* **Never for agents:** the MCP server has no tool for it and no route to it; a locked MCP session (`MSE_LOCK=1`)
  refuses an `in:` folder outside its collection; `mse files --scope mac` (and `status`, `crawl`, `exclude`, `roots`) refuse under
  `MSE_LOCK`, which only allows `--scope indexed` in the locked folder. Piped `mse files` output (a script or an agent
  calling the CLI) is allowed and masks personal identifiers like every other CLI output; the app and web UI see everything.
* The crawl does not follow symbolic links, stays on the boot volume, does not enter iCloud/File Provider placeholder
  folders (`SF_DATALESS`), and skips folders it may not open without asking.

### Managing what it skips

Three places, one list (`mac_index.exclude`) and one validation (`macindex.check_settings`, a 400 with a readable
sentence on a refusal):

* **Web UI, Settings › Whole-Mac search** (also the app's ⌘, window): the state of the index, **Where it looks** (the
  roots: Add folder…, Remove), **Skipped folders** (Add folder… or type a path or name; Remove), a read-only **Always
  skipped** list of the built-in rules above (it comes from the service, so it cannot drift), and **Folders macOS
  protects** (the `skip_protected` switch: on = `auto`, off = `never`; a value of `always` set in the file shows as on and
  is kept until you switch it) with the Full Disk Access how-to and the interpreter path to add. **Rescan now** starts a
  full re-crawl.
* **CLI:** `mse files exclude add|rm|list <folder-or-name>...` and `mse files roots add|rm|list <folder>...`
  (`exclude list --all` also prints the built-in rules; `--json`). They write the same settings through
  `config.update_settings`, so they work with the service stopped; a relative folder is made absolute against the current
  folder (`./proj`); a bare word such as `node_modules` stays a name pattern. Refused under `MSE_LOCK`.
  `mse files search exclude add` searches for those words.
* **Mac app:** right-click a folder in the list, column or icon view: **Exclude from whole-Mac search** or **Include in
  whole-Mac search** (a folder already under an excluded parent or a built-in rule shows a disabled note instead).

What an entry can be, and what is refused (the same everywhere):

* A **folder**: an absolute path or `~/Folder`, stored as `~/Folder/` or `/Folder/`; it covers everything below it.
  Spelling variants and case collapse into one entry.
* A **name or glob without a slash** (`node_modules`, `*.tmp`, `**/build`), kept as typed, matching that name anywhere.
  A glob with a full path (`~/Documents/*/Drafts`) is kept as a pattern.
* **Refused:** `..`; `~user`; a relative entry with a slash (`Documents/Old`, which would never match anything); an entry
  starting with `!` or `#` (exclusions only add to the built-in rules, nothing here switches one off); an empty entry or
  one with control characters; **`/`** (the only entry that really hides every file); and **the home folder or any folder
  above it**. Excluding the home folder does nothing while it is a root, because a root inside an excluded folder is still
  searched, and hides all of it otherwise: neither is what anyone means, so it is refused with that explanation; exclude
  folders inside it, or change the roots. An entry equal to a root is accepted but has no effect (the screens say so);
  an entry a built-in rule already covers is harmless (also flagged).
* **Roots** must be absolute, without `..`, and not inside a place that is never indexed (`~/.ssh`, `/Volumes`, Mail, a
  `node_modules` folder …): refused instead of being dropped silently when the crawl starts.
* A list is replaced as a whole, so an old hand-written entry that the rules now refuse blocks every later change until
  it is taken out; the message names it, the status marks it `invalid`, and removing it in the web UI removes all such
  entries together.

**When it applies.** Names under a newly skipped folder leave the results at once (every result is checked against the
current rules). The service notices a changed rule set or root list within about 10 seconds and re-crawls in the
background (a crawl already running finishes with the old rules and one more starts with the new ones, so several changes
in a row cost one extra crawl, not one each); that is what brings a folder back after Remove. **Rescan now** / `mse files crawl` just starts it without the wait.

### Full Disk Access and the consent dialogs

macOS asks consent before a process first reads Desktop, Documents, Downloads, Movies, Music, Pictures or iCloud Drive.
With `skip_protected: auto` (the default) the crawl **does not touch those folders** unless Full Disk Access is on or an
indexed folder already lives in one (the service already reads it), so a first crawl shows no dialog; `status` lists
what it skipped (`skipped_protected`) and `full_disk_access` says whether the grant exists (detected by opening a file
only Full Disk Access may open: nothing is prompted or changed). To include them, grant Full Disk Access yourself:
System Settings → Privacy & Security → Full Disk Access → "+" → press ⌘⇧G and enter the **binary the service really
runs**: the launchd job starts `.venv/bin/python`, a symlink to
`/opt/homebrew/Cellar/python@3.13/<version>/Frameworks/Python.framework/Versions/3.13/bin/python3.13` (check with
`python3 -c "import os; print(os.path.realpath('.venv/bin/python'))"`; Homebrew changes the version folder on upgrade).
The helper the service starts inherits that grant. Or set `skip_protected: never` and answer the one-time dialogs.

### Measured (synthetic 2.0 M-entry index with realistic names, M4 Pro, warm cache, not a live disk)

* **Query latency**, in-process, mixed set of 75 queries (1-2 characters, common substrings, rare words, two words,
  typos, exact/prefix/suffix, filters, `in:`, regex, negation): `limit` 50 p50 **4.9 ms**, p95 10.9, p99 19; `limit` 20
  p50 4.3; `limit` 200 p50 6.8 ms, p95 13. (Earlier, noisier runs on the same index gave p50 6.0 ms.)
* **End to end** through the HTTP handler on loopback with the JSON decoded by the client, `limit` 200 (the API
  default, 59 KB bodies): p50 **9.4 ms**, p95 15.5, max 22; `limit` 50: p50 7.3 ms.
* **While a crawl is writing** in the same process (ingest throttled to half speed): `limit` 200 p50 13.4 ms, p95 26;
  `limit` 50 p50 10.5 ms, p95 24, worst 72 ms. The first search of a rare shape after a restart is slower (cold pages).
* Size: about 1 GB for 2 M entries.
* **Crawl speed.** The helper walked 17.9 k entries of this project in 0.5 s (about 35 k entries/s with disk I/O
  throttled, 80 k entries/s of CPU time): an estimate from one warm run, not a disk benchmark. Storing them (Python:
  rules check, name folding, SQLite): the first load is a bulk load, ~12 k entries/s plus ~40 s to build the indexes at
  the end, so **2 M entries take about 4-6 minutes in the background** (nothing is searchable until it ends). A later
  re-crawl costs ~24 k entries/s when nothing changed (about 90 s for 2 M, run daily at most) and ~5 k entries/s in the
  worst case where every row changed. FSEvents changes reach the index in ~0.3 s (measured with the real helper).

### Install (after you approve it)

    ./scripts/build_helper.sh            # replaces the installed helper (re-store Ask's cloud keys afterwards: see Rebuild)
    .venv/bin/mse service install        # or: launchctl kickstart -k gui/$(id -u)/com.mysearch.daemon
    mse files status                     # the first crawl starts ~20 s after the service is up; watch "progress"
    mse files "readme"                   # searchable when the first crawl has finished (state: ready)

Switch it off with `mac_index.enabled: false`; delete `macindex.db` (it is only derived data) to start over.

## Connecting agents

**One folder, every agent:** `mse agents install <collection>` (or `mse add <folder> --agents`, or the app's folder settings) writes a `mysearch` server entry locked to that folder (`MSE_CWD=<folder>`, `MSE_LOCK=1`) into:

| File in the folder | Agent |
|---|---|
| `.mcp.json` | Claude Code (Grok CLI reads it too) |
| `.claude/settings.local.json` | Claude Code's approval of that server (`enabledMcpjsonServers`) |
| `.cursor/mcp.json` | Cursor, and the Grok Bot app (it reads Cursor's config) |
| `.codex/config.toml` | Codex, only when the folder is a trusted Codex project (`mse agents status` says whether it is) |
| `.grok/config.toml` | Grok CLI (project scope) |
| `opencode.json` | OpenCode |

`mse agents install --global` adds an unlocked entry (no env: the server scopes itself to the folder the agent works in) to Claude Desktop, `~/.cursor/mcp.json`, `~/.grok/config.toml`, `~/.codex/config.toml` and Antigravity (`~/.gemini/config/mcp_config.json`), for the apps that are installed.

How the files are edited: only the `mysearch` entry changes. Everything else in the file stays byte for byte (install then remove gives back the original file). Each edit is re-parsed and checked before an atomic write; the file mode is kept (new files are 0600) and symlinks are written through. A file that is not plain JSON / valid TOML is reported and left alone. Nothing is ever deleted, and only file names, server names and states are printed, because these files can hold other servers' API keys. `mse agents remove` takes back only the entries that point at that folder; `mse remove <folder> --agents` does it before the folder leaves the index (otherwise its agents fail closed under the lock).

By hand:

- **Claude Code:** registered for all projects.
  ```
  claude mcp add --scope user mysearch -- /path/to/mysearch/.venv/bin/mse mcp
  ```
- **Claude Desktop, Cursor, Codex and other MCP clients (stdio):**
  ```json
  "mysearch": { "command": "/path/to/mysearch/.venv/bin/mse", "args": ["mcp"] }
  ```
- **HTTP clients:** `POST http://127.0.0.1:8737/mcp` with header `X-MSE-Token: <token from config.json>`.
- **Shell-only agents:** `mse search --json "…"` and `mse get '#docid'`.
- **Per-folder lock (stdio):** env `MSE_CWD=<folder>` sets the default collection; adding `MSE_LOCK=1` limits the session to that collection only (other collections, `all`, groups or lists naming anything else, and outside paths are refused; a group or list containing only that collection is fine; a folder in no collection fails every call).

## Raycast

`integrations/raycast/` is a private Raycast extension: Search Files (list with preview), Search Images (thumbnail grid),
Sync Now, and a MySearch Status menu-bar item. It reads the port and token from `config.json` each time, so no token is
stored in the extension.

    cd integrations/raycast
    nvm use                              # Node from .nvmrc; the Raycast CLI needs >= 22.22.2
    npm ci --ignore-scripts              # exact versions; .npmrc also disables install scripts
    npm run build && npx ray develop     # Ctrl-C once the commands appear in Raycast

Hotkey: Raycast Settings → Extensions → MySearch → Search Files → Record Hotkey. Keys in Search Files: ↵ open · ⌘↵ show in
Finder · ⌘Y Quick Look · ⌘⇧C copy citation for AI · ⌃⌘C copy path · ⌘⇧F find similar · ⌘. favourite · ⌘T type filter.
Where to search: the dropdown, or type `work: invoice` / `invoice in:work` (a folder, group or alias), which overrides the
dropdown; the title bar and the first section then say where it ran ("in Work").

## Mac app features

The native app (`MySearch.app`) is a folder browser and a search window in one. Everything below is in the File, Edit,
View, Go and Help menus, and in the Settings window.

* **File operations, Trash first.** ⌘C, ⌘X and ⌘V copy, move and paste files; ⌘D duplicates; ⇧⌘N makes a folder; Return
  renames. ⌘⌫ moves to the Trash, and ⌥⌘⌫ (Delete Immediately) is the only way to delete for good, behind a confirmation
  whose default button is Cancel. ⌘Z and ⇧⌘Z undo and redo every operation (copy, move, duplicate, rename, new folder,
  alias, compress, extract, tags). Nothing is ever overwritten: a taken name gets " 2", " 3" and so on, as in Finder.
  Several items compress into one `Archive.zip` (or `Archive 2.zip`); Extract handles zip, tar, gz, tgz, bz2 and xz.
* **Colour tags.** The seven Finder colours, set from the context menu. Tags are written in macOS's own format, so Finder
  shows them. A dot appears after each name in the list, column, icon and search views, and the **Tags** section of the
  sidebar lists every file on the Mac that carries one colour.
* **Editors.** ⌘⌥O opens the selected Markdown, text or code file in the editor (Markdown opens as a reader). The code
  editor has a tab per file, a status bar, and ⌘S. File › New Text File… creates a file only when you confirm the name.
  Quitting asks before it discards unsaved edits.
* **Open in…** opens the folder in Terminal, iTerm, VS Code, Cursor, Claude Code or Codex, showing only what is installed.
  See [Open in… and the Finder extension](#open-in-and-the-finder-extension) below.
* **Finder extension.** A "MySearch" submenu in Finder's right-click menu. Switch it on after the app is installed and
  opened once: System Settings › General › Login Items & Extensions › Finder (on macOS 14: Privacy & Security › Extensions
  › Added Extensions), and tick MySearch.
* **Command palette (⌘P).** One field for files and folders, the app's commands (New Tab, New Window, New Folder, Split Pane,
  Filter This Folder, Show or Hide Hidden Files, Open in Terminal, VS Code or Cursor, Rescan Mac, the view and mode switches,
  Settings, Sync Now), a path field that starts with `/` or `~/`, and "Ask MySearch" for any text.
* **Tabs and windows.** ⌘T opens a tab at the current folder and ⌘N a separate window. Tabs can be dragged out into a window
  and back. The open tabs are saved and reopened at launch.
* **Split panes (⌘⌥\).** The folder area splits into two panes, each with its own history and view style. ⇥ moves the focus
  between them; F5 copies the selection to the other pane and F6 moves it.
* **Filter this folder (/ or ⌘⌥F).** A field above the list, icons or columns narrows the folder to the names that match.
  Matching ignores case and accents, and a typo in a longer word is forgiven. Esc clears it.
* **Sidebar sources.** Favorites (pinned folders), Recents, Indexed (collections and groups), Locations (cloud drives,
  mounted volumes, with eject) and Tags. Sections remember whether they are open.
* **Search.** The scope button (⌘⌥F) chooses Whole Mac, All indexed, a group or folders, and the sidebar follows it. The
  results have a **File names** section and a **Contents** section that collapses. ⌃1, ⌃2 and ⌃3 jump to the names, the
  contents and the filters. ⌘↵ shows a result in its folder, and ⌘O opens it.
* **Keyboard shortcuts.** Help › Keyboard Shortcuts lists every key above.

Credits: the command palette and the dual panes follow ideas from **Shuffle** (ideas only, no code copied). Parts of the app-opening code, the colour tags and the Finder extension are adapted from **FinderFlow** (MIT,
(c) 2026 Gobinda Tarafdar, tag v1.5.2). The full notice is in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Install the app (DMG)

`MySearch.app` can run the background service itself, so macOS Full Disk Access is granted to **MySearch.app** instead of
Homebrew's Python. With the app installed in `/Applications`, `mse service install` writes a LaunchAgent that starts
`/Applications/MySearch.app/Contents/MacOS/MySearch --daemon`; that supervisor (no window, no hot keys) runs
`<project>/.venv/bin/python -m mse serve` as its child, forwards SIGTERM/SIGINT so the queue is saved, and exits with the
child's status (launchd restarts it). `msehelper` is bundled in the app, so the file crawl belongs to the app too. The project
folder comes from `service.project` in `~/Library/Application Support/MySearch/config.json` (without it, the app uses the clone it was built in, else `~/Developer/mysearch`, the folder docs/INSTALL.md tells you to clone to).
Without the app, `mse service install` keeps the old Python LaunchAgent.

1. **Build and sign:** `cd /path/to/mysearch && ./scripts/build_dmg.sh`. Click "Always Allow" if macOS asks whether codesign may use the key. The signing identity comes from `MSE_SIGN_IDENTITY`, `MSE_SIGN_SHA1` and `MSE_TEAM_ID` (or `scripts/sign.local.env`, see `scripts/sign.local.env.example`); without them the build is ad-hoc.
   (`--adhoc --out <folder>` makes an ad-hoc test build with no Keychain prompt.)
2. **Install:** open `build/MySearch.dmg` and drag MySearch to Applications.
3. **Grant access BEFORE switching the service:** System Settings > Privacy & Security > Full Disk Access > + > `/Applications/MySearch.app`, switched on.
4. **Quit the old app** (MySearch menu > Quit). Then switch the service and rescan: `.venv/bin/mse service install && .venv/bin/mse files crawl --force`. macOS shows a "Background item added" notice.
5. **Move the old copy to the Trash**, so two apps don't fight over the hotkeys: `mv ~/Applications/MySearch.app ~/.Trash/`.
6. **Open `/Applications/MySearch.app`.** Then run `mse files status`, which should say Full Disk Access: granted.

`mse service restart` now waits (up to 20 s) until the daemon answers, and `mse files crawl` waits briefly for a daemon that is
still starting. The bundled helper is a different binary from the one in the data folder: store Ask's cloud keys again
(`mse providers set-key <id>`) if the Keychain asks.

## Open in… and the Finder extension

**Open in…** opens the selected (or current) folder in Terminal, iTerm, VS Code, Cursor, Claude Code or Codex, showing only
what is installed. It is in the toolbar ("Open in…"), in File › Open in…, and, for context menus, through `Launchers.menuItems(for:)` (the views add it). Terminal,
iTerm, VS Code and Cursor open through the system; Claude Code (and Codex without its app) start in a new Terminal window with
`cd '<folder>' && <tool>`; the first use asks macOS for permission to control Terminal. Settings › Mac App › Open in… lists what was
found and has a **Refresh** button (detection is cached for 6 hours).

**Finder extension** (`MySearch.app/Contents/PlugIns/MySearchFinder.appex`): a "MySearch" submenu in Finder's right-click menu
with *Search in MySearch here*, *Ask MySearch about this folder*, *Open in MySearch*, *Copy Path* and *Open in Terminal*. It is
sandboxed and only sends `mysearch://search|ask|open|terminal?path=…` links to the app, which checks the path and does the work.
It watches the home folder (Desktop, Documents, Downloads) and the indexed folders. To switch it on, open the installed app once,
then **System Settings › General › Login Items & Extensions › Finder** (on macOS 14: **Privacy & Security › Extensions › Added
Extensions**) and tick **MySearch**. If it does not appear, run `pluginkit -a /Applications/MySearch.app/Contents/PlugIns/MySearchFinder.appex`
and `killall Finder`. Source: `app/FinderExtension/`; parts adapted from FinderFlow (MIT), see `THIRD_PARTY_NOTICES.md`.

## Rebuild

```
uv sync --frozen
./scripts/build_helper.sh && ./scripts/build_app.sh
.venv/bin/mse service install
```

`./scripts/build_helper.sh <dir>` builds the helper into another folder (to try a build without replacing the installed
one). A rebuilt helper is a new binary to the Keychain: store Ask's provider keys again afterwards (`mse providers set-key`).

## Credits

MySearch builds on open models, research and other people's projects: fsearch and Everything for instant file-name search, FinderFlow (MIT) for adapted file-operation code, EmbeddingGemma 2, Qwen3 and Whisper for the models, MLX and sqlite-vec for the engine, and reciprocal rank fusion and LoRA from the research literature. The full list, with links and what each is used for, is in [docs/CREDITS.md](docs/CREDITS.md).

## Licence

The code is MIT-licensed, see [LICENSE](LICENSE). Model weights are not in this repository and are never shipped with it:
you download them yourself, under their own terms. EmbeddingGemma: Apache-2.0. The Qwen3 models: Apache-2.0.
Whisper: MIT. Third-party code and the licences of the main dependencies: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Distributing the app

The DMG that `scripts/build_dmg.sh` makes is for the Mac that built it. A build for other people needs a
Developer ID certificate (`MSE_SIGN_IDENTITY`, `MSE_SIGN_SHA1`, `MSE_TEAM_ID`) and notarization by Apple, which the
script does not do. An ad-hoc build (the default when no signing values are set) runs only on the machine that built it.
