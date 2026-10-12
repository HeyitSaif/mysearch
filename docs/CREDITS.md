# Credits and references

MySearch uses, or took ideas from, the projects, models, papers and platforms below; each entry says how.

## Inspiration

- [fsearch](https://github.com/noahdunnagan/fsearch) by noahdunnagan. MIT. `mse/filequery.py` follows its file-name query language (`ext:`, `size:`, `type:`, `path:`) and adds operators of its own.
- [fff](https://github.com/dmtrKovalenko/fff) by Dmitriy Kovalenko. MIT. Ideas only, no code: smart case, ranking a match in the file name above a match in the folder path (+40 % when the name is the query, +16 % capped inside it), glob constraints with `!` exclusion, a recency-and-frequency score for opened files with a boost for the file last opened for the same query (`mse/frecency.py`), and honouring ignore files in git repositories (`mse/gitignore.py`). `mse/filequery.py`, `mse/frecency.py` and `mse/gitignore.py` are written from those descriptions; the name index and its query plan are MySearch's own.
- [Everything](https://www.voidtools.com/) by voidtools. The README names it as the model for instant file-name search on the Mac (`README.md`).
- [Shuffle](https://www.shuffleapp.co/) by Shuffle. Only its ideas were taken, no code: the command palette (`app/CommandPalette.swift`) and the dual panes (`app/SplitPanes.swift`).

## Code adapted from

- [FinderFlow](https://github.com/Gtarafdar/FinderFlow) by Gobinda Tarafdar. MIT, tag v1.5.2. Several Mac app files are adapted from its code (app launching, colour tags, the Finder Sync extension, the unique-name rule, the list of code file types, the Markdown reader), and its code editor window is the behaviour reference for `app/CodeEditorWindow.swift`; each file is listed in `THIRD_PARTY_NOTICES.md`.

## Models

MySearch does not redistribute model weights; `mse setup models` downloads pinned revisions from Hugging Face, and each model's own licence applies.

- [EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2) by Google. Apache-2.0. The default embedding model is the 8-bit MLX conversion by mlx-community ([embeddinggemma-2-8bit](https://huggingface.co/mlx-community/embeddinggemma-2-8bit), Apache-2.0), which places text, images, PDF pages, audio and video in one vector space (`mse/paths.py`, `mse/setup_models.py`).
- [Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) by the Qwen team. Apache-2.0. The second-stage re-ranker for close calls (`mse/rerank.py`), in an 8-bit MLX build that is an unpublished hand conversion of these weights and that `mse setup models` does not install; the LoRA fine-tune under Research trains adapters on it.
- [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507) by the Qwen team. Apache-2.0. The default local Ask model is the 4-bit MLX conversion by mlx-community ([Qwen3-4B-Instruct-2507-4bit](https://huggingface.co/mlx-community/Qwen3-4B-Instruct-2507-4bit), Apache-2.0), used only when a local answer is chosen (`mse/ask.py`, `mse/setup_models.py`).
- [Qwen3.5-4B](https://huggingface.co/Qwen/Qwen3.5-4B) by the Qwen team. Apache-2.0. An optional alternative local Ask model, chosen in Settings > Ask, using the 4-bit MLX conversion by mlx-community ([Qwen3.5-4B-MLX-4bit](https://huggingface.co/mlx-community/Qwen3.5-4B-MLX-4bit), Apache-2.0); it was the measured runner-up to Qwen3-4B-Instruct-2507 (`docs/ask-models.md`).
- [Whisper large-v3-turbo](https://huggingface.co/openai/whisper-large-v3-turbo) by OpenAI. MIT (the MLX repack's card says Apache-2.0). Speech-to-text with timestamps for audio and video transcripts, where MySearch loads the 8-bit MLX conversion ([whisper-large-v3-turbo-8bit](https://huggingface.co/mlx-community/whisper-large-v3-turbo-8bit)) and takes its processor and tokenizer files from the original (`mse/worker.py`, `mse/setup_models.py`).

## Models evaluated for Ask

These candidates were compared on their model cards, licences, sizes and published hallucination rates, and only the two Qwen models above were measured (`docs/ask-models.md`).

- [Gemma 4 E2B-it](https://huggingface.co/google/gemma-4-E2B-it) by Google. Apache-2.0. Compared, not run: its 4-bit MLX build was over the memory budget and the "mobile" QAT build was not tested; the [MLX quantization report](https://huggingface.co/mlx-community/gemma-4-e2b-4bit/discussions/1), a community discussion, is cited for the 4-bit caveat.
- [Ministral 3 3B Instruct 2512](https://huggingface.co/mistralai/Ministral-3-3B-Instruct-2512) by Mistral AI. Apache-2.0. Compared, not run; its published hallucination rate (24.2 %) is higher than Qwen3-4B's (5.7 %).
- [Granite 4.2 3B](https://huggingface.co/ibm-granite/granite-4.2-3b) by IBM. Apache-2.0. Compared, not run; the size figure comes from IBM's own MLX build ([granite-4.2-3b-q4-mlx](https://huggingface.co/ibm-granite/granite-4.2-3b-q4-mlx), Apache-2.0).
- [LFM2-1.2B-RAG](https://huggingface.co/LiquidAI/LFM2-1.2B-RAG) by Liquid AI. LFM Open License (the Hub tags it "other"). Compared, not run; ruled out by its licence, and because the RAG model has no 4-bit MLX build.

## Research

- [Reciprocal rank fusion outperforms Condorcet and individual rank learning methods](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf), Cormack, Clarke & Büttcher, SIGIR 2009. Search merges keyword and meaning results with weighted reciprocal rank fusion, with `RRF_K = 60` (`mse/search.py`).
- [LoRA: Low-Rank Adaptation of Large Language Models](https://arxiv.org/abs/2106.09685), Hu et al., 2021. The re-ranker fine-tune trains rank-8 LoRA adapters on the attention q and v projections of the last 16 layers of the frozen 8-bit model (`mse/finetune.py`).
- [QLoRA: Efficient Finetuning of Quantized LLMs](https://arxiv.org/abs/2305.14314), Dettmers et al., 2023. The module docstring calls the re-ranker fine-tune QLoRA, adapters on a quantized base, though MySearch's base is 8-bit MLX rather than the paper's 4-bit NF4 setup (`mse/finetune.py`).
- [Robust Speech Recognition via Large-Scale Weak Supervision](https://arxiv.org/abs/2212.04356), Radford et al., 2022. Describes the original Whisper; large-v3-turbo is a later release of that family, which MySearch uses for transcripts (`mse/worker.py`).
- [Qwen3 Embedding: Advancing Text Embedding and Reranking Through Foundation Models](https://arxiv.org/abs/2506.05176), Qwen team, 2025. The report covers the Qwen3 reranker family; MySearch uses its 0.6B reranker only (`mse/rerank.py`).
- [EmbeddingGemma: Powerful and Lightweight Text Representations](https://arxiv.org/abs/2509.20354), Google, 2025. Describes the EmbeddingGemma family; the paper covers text embeddings only, while MySearch uses EmbeddingGemma 2 for text and the other media listed above.
- [Vectara hallucination leaderboard (HHEM)](https://github.com/vectara/hallucination-leaderboard), Vectara. Apache-2.0. The hallucination rates quoted in `docs/ask-models.md` come from it; MySearch does not run it.
- [Gemma 4](https://huggingface.co/blog/gemma4), Hugging Face blog. Cited in `docs/ask-models.md` for the Gemma 4 facts used in the comparison; no Gemma 4 model is shipped or run.
- [Mistral 3](https://mistral.ai/news/mistral-3/), Mistral AI, release article. Cited in `docs/ask-models.md` for the Ministral 3 facts used in the comparison; no Mistral model is shipped or run.

## Libraries and platforms

The main Python dependencies and their licences are listed in `THIRD_PARTY_NOTICES.md`.

- [MLX](https://github.com/ml-explore/mlx), ml-explore (Apple). MIT. Runs the embedding, re-ranker, speech and answer models on the Mac GPU (`mse/worker.py`).
- [mlx-lm](https://github.com/ml-explore/mlx-lm), ml-explore. MIT. Loads the Qwen answer and re-ranker models and trains the re-ranker LoRA adapters (`mse/worker.py`, `mse/finetune.py`).
- [mlx-vlm](https://github.com/Blaizzy/mlx-vlm), Blaizzy. MIT. Loads the EmbeddingGemma 2 model; `pyproject.toml` pins it to a commit.
- [mlx-audio](https://github.com/Blaizzy/mlx-audio), Blaizzy. MIT. Loads Whisper for speech-to-text (`mse/worker.py`).
- [Transformers](https://github.com/huggingface/transformers), Hugging Face. Apache-2.0. Loads the Whisper processor from its files (`mse/worker.py`).
- [huggingface_hub](https://github.com/huggingface/huggingface_hub), Hugging Face. Apache-2.0. Downloads the pinned model revisions for `mse setup models` (`mse/setup_models.py`).
- [sqlite-vec](https://github.com/asg017/sqlite-vec), asg017. MIT or Apache-2.0. Provides the 1-bit vector index that makes meaning search fast, inside SQLite (`mse/store.py`).
- [SQLite FTS5](https://www.sqlite.org/fts5.html), SQLite project. Public domain. Provides the keyword index (`mse/store.py`, `mse/textutil.py`).
- [NumPy](https://numpy.org/), NumPy developers. BSD-3-Clause. Array arithmetic for embeddings, search and indexing (`mse/embedder.py`, `mse/search.py`, `mse/indexer.py`).
- [ffmpeg](https://ffmpeg.org/), FFmpeg developers. Optional external tool, not bundled: when it is installed, MySearch calls it to take video frames, extract audio and detect silences for transcripts (`mse/extract.py`).
- [uv](https://docs.astral.sh/uv/), Astral. Creates the Python environment and runs `mse` (`scripts/setup.sh`).
- [Model Context Protocol](https://modelcontextprotocol.io/), MCP project. The `mse` MCP server implements protocol version 2025-06-18 itself, without an SDK (`mse/mcp.py`).
- [Raycast API](https://developers.raycast.com/), Raycast. The Raycast extension is built on `@raycast/api` (`integrations/raycast/package.json`).
- [Claude API](https://docs.anthropic.com/), Anthropic. Optional cloud model for Ask, called through the Messages API with `claude-opus-5-5` as the default model (`mse/cloud.py`, `mse/config.py`).
- [Vision: VNRecognizeTextRequest](https://developer.apple.com/documentation/vision/vnrecognizetextrequest), Apple. Text recognition for scanned PDF pages and pictures (`helper/msehelper.swift`).
- [PDFKit](https://developer.apple.com/documentation/pdfkit), Apple. Reads PDF text page by page, and displays PDFs in the Ask view (`helper/msehelper.swift`, `app/AskView.swift`).
- [QLThumbnailGenerator](https://developer.apple.com/documentation/quicklookthumbnailing/qlthumbnailgenerator), Apple. Creates file thumbnails in the icon and column views (`app/IconView.swift`, `app/ColumnView.swift`).
- [File System Events](https://developer.apple.com/documentation/coreservices/file_system_events), Apple. Reports changed files for live indexing (`helper/msehelper.swift`, `mse/watcher.py`).

## Roadmap inspiration

- [paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) by the paperless-ngx project. Its document types and correspondents are ideas for planned document understanding, not yet implemented.

If we missed a credit, please open an issue.
