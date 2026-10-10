# Ask mode: which models

Since 2026-10-07 Ask writes its answers with a **cloud model while the Mac is online** and with the **local model**
below when it is offline (README → "Cloud models for Ask"). The cloud part is described at the end of this file
("Cloud models"); everything up to there is about the local model.

## The local model

Researched and measured on 2026-10-07 (M4 Pro, 48 GB, mlx-lm 0.32.0 / mlx 0.32.3, no upgrade needed).

**Default: `qwen3-4b-instruct-2507-4bit`** (mlx-community conversion of Qwen/Qwen3-4B-Instruct-2507, Apache-2.0,
2.1 GB on disk). Runner-up, measured: `qwen3.5-4b-4bit`. Both live in `~/Library/Application Support/MySearch/models/`.

## What the model has to do

Answer in 2–5 sentences from 5–8 numbered passages (about 2.4–3.3k prompt tokens), German or English, put `[n]`
after each claim, and say plainly when the passages do not answer. It must fit in about 3 GB at 4 bit, carry a
permissive licence, and load with the installed mlx-lm without `trust_remote_code`.

## Candidates (October 2026)

Sizes and licences come from the Hugging Face Hub API (`model_info`), queried 2026-10-07. "Halluc." is Vectara's
summarisation hallucination rate (HHEM leaderboard, updated 2026-09-22); "–" means the model is not listed there.

| Model | Released | Licence | 4-bit MLX on disk | Notes | Halluc. |
|---|---|---|---|---|---|
| **Qwen3-4B-Instruct-2507** | 2025 ("2507") | Apache-2.0 | 2.28 GB | non-thinking only; `qwen3` in mlx-lm | 5.7 % (Qwen3-4B) |
| Qwen3.5-4B | Feb 2026 | Apache-2.0 | 3.06 GB (incl. vision tower, which mlx-lm drops) | thinking by default (`enable_thinking=False` turns it off); 201 languages; IFEval 89.8, MMMLU 76.1 (card) | – (larger Qwen3.5 models: 10.5–12.1 %) |
| Qwen3.5-2B | Feb 2026 | Apache-2.0 | 1.75 GB | fast fallback, not measured | – |
| Qwen3-1.7B | 2025 | Apache-2.0 | 0.98 GB | fast fallback, not measured | – |
| Gemma 4 E2B-it | Apr 2026 | Apache-2.0 | 3.58 GB (QAT "mobile" build 2.43 GB) | 2.3B effective / 5.1B with embeddings; MMMLU 67.4 (card); early MLX 4-bit builds of Gemma 4 E-models were reported to emit garbage because the per-layer embeddings were quantized | – (Gemma-3-4B 6.4 %) |
| Gemma 4 E4B-it | Apr 2026 | Apache-2.0 | 5.18 GB (QAT mobile 3.49 GB) | over the memory budget; MMMLU 76.6 (card) | – |
| Granite 4.2 3B | Aug 2026 | Apache-2.0 | 2.07 GB (IBM's own MLX build) | German listed; thinking by default; IFBench 74.3, MMLU-ProX lite 27.8 (card) | – |
| Ministral 3 3B Instruct 2512 | Dec 2025 | Apache-2.0 | 2.78 GB | multilingual incl. German | 24.2 % |
| Phi-4-mini-instruct | – | MIT | 2.18 GB | repo ships custom `.py` modelling code | 23.5 % |
| Llama 3.2 3B Instruct | – | Llama 3.2 licence (not Apache/MIT) | 1.82 GB | licence rules it out | – |
| SmolLM3-3B | Jul 2025 | Apache-2.0 | 1.75 GB | older; not shortlisted | – |
| LFM2-1.2B-RAG / LFM2.5-2.6B | 2025 / Aug 2026 | LFM Open License (not Apache/MIT) | RAG model: BF16 only (2.35 GB); LFM2.5-2.6B: 1.60 GB | RAG-specialised, but licence and no 4-bit MLX build of the RAG model | – |

Finalists downloaded (5.0 GB together; disk had 14–20 GB free): Qwen3-4B-Instruct-2507 (best hallucination record
in its size, smallest) and Qwen3.5-4B (newest small Qwen, best published multilingual and instruction scores).
Gemma 4 E2B was the next candidate; its 4-bit build is over budget and the "mobile" QAT build was not tested.

## Benchmark (measured)

24 questions on collection `work`: 19 answerable, taken from two private, hand-written gold sets (not published) and
rephrased as questions (10 EN / 9 DE, each with its expected document), plus 5 unanswerable (2 EN / 3 DE).
The question and document names in this file are stand-ins; the measurements come from the private sets.
Retrieval: the live daemon's `/api/search?precise=1` (top 10). Passage text: the same read-only chunk reader the
daemon uses (`ask.db_chunks`). Both models got identical passages. The expected document reached the passages for
17 of 19 questions (the misses are retrieval, not the model).

| | Qwen3-4B-Instruct-2507 | Qwen3.5-4B |
|---|---|---|
| Expected document cited (of 17 where it was in the passages) | **15/17** | 14/17 |
| Answers with at least one citation (of 19) | **19/19** | 17/19 |
| Answers with zero invalid citations | 23/23 | 23/23 |
| Unanswerable: exact "not in the documents" sentence (of 4 that reached the model) | **4/4** | 0/4 (refused, but in its own words) |
| False refusals on answerable questions | 0 | 0 |
| Bullet lists / bold despite the rules | 0 | 2 |
| Hit the 300-token limit | 1 | 2 |
| Median answer length | **60 words** | 90 words |
| Steady memory, loaded (phys_footprint) | **2.46 GB** | 2.69 GB |
| Peak memory while answering (lifetime phys_footprint) | **3.76 GB** | 3.93 GB |
| `ps` RSS (misleading: Metal buffers not counted) | 0.7 GB | 0.1 GB |
| Load, incl. warm-up | 3.3–3.7 s | 6.5 s |
| Generation, 40-token prompt, no other GPU job seen | **87 tok/s** | 75 tok/s |
| Median model TTFT on ~2.8k-token prompts | 5.7 s* | 5.7 s* |
| Median generation on those prompts | 15.6 tok/s* | 18.0 tok/s* |

\* Measured while another process kept the GPU at 99 % utilisation (a LoRA training/eval run). These are not
representative. Uncontended TTFT on a real 3k-token prompt has not been measured yet; see "To re-measure".

Both models were wrong in the same places. Two answers followed an older dated record over a newer one. The prompt now tells the model to
prefer the most recent passage when passages disagree and to say that older records differ. That rule is not
re-measured yet.

**Why Qwen3-4B-Instruct-2507:** it follows the format the app depends on. It uses the exact refusal sentence (so
`answered: false` is reliable), cites in every answer, stays short, and never uses lists. It is also the smaller and
faster model, and it has the best published hallucination rate in its class. Qwen3.5-4B read one document better
(a long supply contract), but it dropped citations and the refusal sentence often enough to show in a 24-question
run.

## Memory and speed settings (in `mse/worker.py`, `Generator`)

- `prefill_step_size=512`. On a 2.1k-token prompt the MLX peak went from 3.2 to 3.0 GB (Qwen3-4B) and from 4.0 to
  3.3 GB (Qwen3.5-4B). Prefill was 8–14 % slower.
- 8-bit KV cache: no lower peak on these prompt lengths, so it is not used.
- Token budget: Qwen tokenizers spend one token per digit, so a flat 3 chars/token under-counted number-heavy records
  by up to 54 %. Least-squares fits on 182 real passages (one per tokenizer) had 6–8 % mean error; the shipped
  `ask.estimate_tokens` takes the larger coefficient of the two fits: letters × 0.29 + digits × 1.5 + other symbols × 0.5.
- Memory is read as phys_footprint (`embedder.footprint_mb`, libproc), which matches `/usr/bin/footprint` exactly.

## Follow-up questions (multi-turn)

Added 2026-10-07. A chat client sends `history: [{q, a}]` (the last turns, oldest first, at most 3). Nothing here
was measured on the GPU. The choices below come from reasoning and from the figures above.

**What is searched.** A follow-up ("and when was it paid?", "what about the printer?", "Und wer hat bezahlt?") has too
few words of its own to find the right documents, so it is searched for what it means:

1. `ask.is_follow_up` decides whether the question leans on the conversation. It does when the question opens like
   a continuation ("and", "what about", "und", "was ist mit"), points back ("it", "they", "es", "dort") with at most
   four subject words, or is very short and names no subject of its own. A question that stands on its own ("Welche
   Farbe hat der USB-Stick?") is searched as it is, and no model call is made.
2. **Heuristic (always available, free):** append up to 5 key terms of the previous question, itself resolved the same
   way, so a chain keeps its subject. A follow-up that names its own date or number does not inherit the old one,
   because `search.parse` would turn a carried "March 2025" into a date filter that contradicts "and in April?". It is
   additive: "what about the printer?" after "When does the warranty on the laptop end?" searches
   `what about the printer? warranty laptop end`, and the answer model, which sees the conversation, picks the printer
   passages.
3. **Model rewrite (used when it can be done cheaply):** one greedy generation (temperature 0, ≤ 40 tokens) turns
   the follow-up into a standalone question ("When does the warranty of the printer end?"). It runs only when
   the question is a follow-up **and** the Ask model is already loaded and idle. Its output is used only if it passes
   `ask.clean_rewrite`. That check rejects an empty or over-long line, an answer instead of a question (`[n]`
   markers, the not-found sentence), a number the conversation never mentioned, a dropped number or acronym of the
   new question, and a line that shares no key term with the conversation. Otherwise the heuristic query is used.
   `done.rewrite` / `rewrite_fallback` say which path ran and why (`off`, `model_not_loaded`, `timeout`, `error`,
   `unusable`).

**Why the model rewrite is on by default.**

- **Quality.** The heuristic can only add words. It cannot swap one subject for another or turn "he" into a person's
  name. In a multi-folder archive that matters, because one folder's documents must not answer for another's, and most
  follow-ups are exactly such swaps and pronouns. A 4B instruct model rewrites a question reliably enough for this. The guards above catch
  the typical small-model failures (answering instead of rewriting, inventing a number).
- **Cost, estimated rather than measured.** The rewrite prompt is short: the rules, every earlier question, the latest
  answer at 40 words and older answers at 15. That is about 310 estimated tokens for one turn and 460 for three
  number-heavy turns (`ask.estimate_tokens`, which over-counts on purpose). The output is 15–30 tokens. Measured
  above: 87 tok/s generation on a free GPU; about 490 tok/s prompt reading (2.8k tokens in 5.7 s) while another
  process held the GPU at 99 %. On a busy GPU that gives about 0.6–0.9 s of prompt reading plus 1–2 s of writing,
  which is over budget, so the heuristic is used. On a free GPU, prompt reading should be faster than the busy-GPU
  rate (not yet measured) and writing takes about 0.2–0.35 s, so the expected total is under about 1.2 s.
- **Bounded worst case.** The model is used only while already loaded (never a 3.5 s load in front of the search),
  and only for follow-ups. The request waits at most `rewrite_budget_ms` (1,500) for it; after that the heuristic
  query is searched at once, and the abandoned generation is cancelled at its next token. In a chat the model is
  nearly always warm for the follow-up, because the previous answer loaded it less than 5 minutes earlier.
- **Off switch.** `"ask": {"rewrite_model": false}` in `config.json` keeps the heuristic only.

**What the prompt gets.** The earlier turns, compacted, sit between the passages and the question: questions and
answers at most 60 words each, answers without their `[n]` markers (they pointed at that turn's passages), and an
extra rule that the conversation is not a source. After a model rewrite the question line also carries
"(In context: …)". The conversation comes out of the same `context_tokens` budget. It may use at most 600 tokens
(a quarter of the budget at most); the oldest turns go first, then the last answer shrinks. The passages get the
rest, so the total prompt stays within the budget. A first question's prompt is byte for byte the benchmarked one, except the first sentence of the system prompt, reworded on 2026-10-10 (the figures above were measured on the earlier wording).
Citations are checked against the current passages only, as before.

**Unchanged:** the scope comes from the request alone. A scope shortcut, `in:` or other inline filters in an earlier
question are dropped, never carried. One answer reads one collection unless the scope is `all`. With `agent_safe`
the history is masked before anything uses it, and `search_query` is masked too.

**Known limits of the heuristic:** German capitalises every noun, so a short German question naming a company by a
plain word ("Gibt es eine Rechnung von Acme?") can look like a follow-up and get earlier terms added (extra
recall, some noise). A topic switch phrased as a fragment ("Invoices?") is treated as a follow-up.

## To re-measure (the user runs this; nothing in the repo runs it automatically)

With nothing else using the GPU and Ask switched on, ask the same question twice (the first one also loads the
model) and read the timings from the `done` event:

```
mse ask "When does the warranty of the Acme laptop end?" -c work --json | jq .timings
```

`ttft_ms` is from the request to the first word (search included), `model_ttft_ms` the model's own prompt reading,
`tok_s` the writing speed.

The follow-up rewrite: right after an answer (model warm), ask a follow-up and read how long the rewrite took and
what was searched:

```
mse ask --follow-up "and when was it paid?" --json | jq '{rewrite, rewrite_fallback, search_query, ms: .timings.rewrite_ms}'
```

`rewrite: "heuristic"` with `rewrite_fallback: "timeout"` means it took longer than `rewrite_budget_ms`. Not yet known for long prompts on a free GPU: 87 tok/s was measured on a short prompt,
15–20 tok/s on ~3k-token prompts with the GPU fully busy elsewhere; a free GPU should land in between.

## Cloud models

Added 2026-10-07: the latest larger models whenever the Mac is online, the local model only
offline, custom models, and the same behaviour for agents. **Nothing in this section was measured against a live API**
(the tests use local fake servers); the choices come from the API documentation and from the requirements.

**Default: Claude Opus 5.5 (`claude-opus-5-5`) on the Claude API.** The current Opus at $4 / $20 per million tokens.
An Ask prompt is about 3,000–3,500 input tokens and an answer 60–150 output tokens plus a little thinking, so one
answer costs roughly 1.5–2 cents; a follow-up rewrite adds a fraction of a cent. Request shape (`mse/cloud.py`):

- streaming Messages API, system prompt as `system`, one user turn: exactly the prompt the local model gets, so
  citations, the not-found sentence and the history rules are unchanged;
- `output_config.effort: "low"` (`cloud_effort`): Opus 5.5 always thinks and its default effort is `medium`; a 2–5
  sentence cited answer does not need more, and low effort keeps the first word quick. Raise it in `config.json` if
  answers to hard questions seem thin;
- no `temperature` / `top_p`: current Claude models reject sampling parameters;
- `max_tokens` 8192 (`cloud_max_tokens`): thinking counts against it, the 300-token cap of the local model would cut
  answers off. The prompt keeps the answer itself short;
- `fallbacks: "default"` with beta `server-side-fallback-2026-07-01` (Claude API only, for Opus 5.5 / 5, Sonnet 5.5 and
  Fable 5.1): should a safety classifier decline, the API retries on the model Anthropic recommends for that category.
  If the API ever answers 400 to the parameter, the request is repeated once without it;
- a `refusal` stop with no text, an empty answer, or any failure before the first word counts as a failure before
  the first token: under route `auto` the local model answers instead.

**Local routers and key files.** A server on this Mac that speaks the OpenAI Responses API and keeps its key in a file
is an ordinary custom provider (`kind: "openai-responses"`) with a `key_file`. By default MySearch reads that file when
a request needs it and keeps no copy of the key; **Connect** or `mse providers set-key` store one in the Keychain only
when asked. Such servers often use namespaced model ids (`vendor/model-name`; `GET /v1/models` lists the ones they
publish, and `auto_models` fetches the list once by itself). Requests go to `/v1/responses` with `stream: true`,
`store: false`, `instructions` (the system prompt), `input` (the user turn) and `max_output_tokens`, and authenticate with
the key as a bearer token, so the key never appears in a URL. Reasoning models behind a router take longer to their first
word; the follow-up rewrite then usually misses its 1.5 s budget and the heuristic search query is used.

**Custom providers.** Any OpenAI-compatible endpoint: Chat Completions (`/chat/completions`, streaming,
`temperature` 0.1, `reasoning_content` is ignored) or Responses. A `<think>…</think>` block at the start of an answer is
dropped by the citation filter, as for local models.

**Privacy.** With `cloud_redact` the passages and the earlier turns are masked (`mse/redact.py`) before they leave;
`done.redacted` counts what was masked. A folder with `ask_cloud: false` never sends anything; a scope that includes it
answers locally. Metadata-only files are never read.

**To check (the user runs this; it calls the API and costs a little):**

```
mse providers test anthropic
mse ask "When does the warranty of the Acme laptop end?" -c work --json \
  | jq '{where, provider, model, redacted, fallback_reason, timings}'
```

`timings.ttft_ms` is from the question to the first word (search included), `model_ttft_ms` the cloud model's own wait
(network, prompt reading, thinking), `tok_s` its writing speed.

## Sources

- Claude API (models, effort, refusal fallbacks, streaming events): the `claude-api` reference, cached 2026-10-06
- Vectara hallucination leaderboard (updated 2026-09-22): https://github.com/vectara/hallucination-leaderboard
- Qwen3.5-4B model card: https://huggingface.co/Qwen/Qwen3.5-4B
- MLX builds: https://huggingface.co/mlx-community/Qwen3-4B-Instruct-2507-4bit, https://huggingface.co/mlx-community/Qwen3.5-4B-MLX-4bit
- Gemma 4: https://huggingface.co/blog/gemma4, https://huggingface.co/google/gemma-4-E2B-it,
  MLX quantization report: https://huggingface.co/mlx-community/gemma-4-e2b-4bit/discussions/1
- Granite 4.2: https://huggingface.co/ibm-granite/granite-4.2-3b, https://huggingface.co/ibm-granite/granite-4.2-3b-q4-mlx
- Ministral 3: https://huggingface.co/mistralai/Ministral-3-3B-Instruct-2512, https://mistral.ai/news/mistral-3/
- LFM2-1.2B-RAG: https://huggingface.co/LiquidAI/LFM2-1.2B-RAG
- Sizes, licences, release dates: Hugging Face Hub API, 2026-10-07
