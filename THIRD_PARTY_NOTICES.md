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
| opencv-python | Apache-2.0 (wrapper and bundled OpenCV) | image handling |
| regex | Apache-2.0 AND CNRI-Python | text matching |
| scipy | BSD-3-Clause | numerics |
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
