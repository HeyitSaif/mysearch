"""Embedding worker process (EmbeddingGemma 2 on MLX).

Started on demand by the daemon and stopped when idle, so the model only occupies memory while
it is needed. Protocol: one JSON object per line on stdin, one JSON reply per line on stdout.

  {"id": 1, "op": "text",  "texts": ["task: search result | query: ..."]}
  {"id": 2, "op": "media", "items": [{"type": "image", "path": "...", "title": "..."}]}
  -> {"id": 1, "vecs": "<base64 float32, n x 768>", "ok": [true, ...]}

--mode text loads only the text encoder (270M params); --mode full adds the vision and audio towers.
All modes share one vector space.

--mode ask loads a small instruct model (mlx-lm) for Ask mode and streams several lines per request:
  {"id": 3, "op": "ask", "messages": [...], "max_tokens": 300, "temperature": 0.1, "top_p": 0.9}
  -> {"id": 3, "token": "..."} ... then {"id": 3, "done": true, "prompt_tokens", "tokens", "model_ttft_ms", ...}
  A {"op": "cancel"} line sent while it streams stops that answer early (checked between tokens).
"""

import argparse
import base64
import json
import os
import select
import sys
import time
import traceback
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

import warnings  # noqa: E402

warnings.filterwarnings("ignore")

import mlx.core as mx  # noqa: E402
import numpy as np  # noqa: E402

DIMS = 768


def log(*a):
    print(time.strftime("%H:%M:%S"), "[worker]", *a, file=sys.stderr, flush=True)


class Transcriber:
    """Whisper large-v3-turbo (8-bit MLX) — speech to text with timestamps.

    One generate() call transcribes a whole file. mlx_audio detects the language once, from the first 30 s window,
    and keeps it for the rest of the file. condition_on_previous_text=False stops a wrong passage from being repeated
    into the next window. Each segment carries the model's own quality figures (avg_logprob, no_speech_prob,
    compression_ratio), which extract.py uses to drop hallucinations. hallucination_silence_threshold is not passed:
    the model applies it only together with word_timestamps, which is not used.
    """

    def __init__(self, model_path: Path, processor_path: Path):
        from mlx_audio.stt import load
        from transformers import WhisperProcessor

        t = time.time()
        mx.set_cache_limit(256 * 1024 * 1024)
        self.model = load(str(model_path))
        self.model._processor = WhisperProcessor.from_pretrained(str(processor_path))
        log(f"loaded whisper in {time.time() - t:.1f}s")

    def transcribe(self, path: str) -> dict:
        out = self.model.generate(path, condition_on_previous_text=False, no_speech_threshold=0.6,
                                  logprob_threshold=-1.0, compression_ratio_threshold=2.4)

        def num(v):
            return None if v is None else float(v)

        segs = [{"start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip(),
                 "avg_logprob": num(s.get("avg_logprob")), "no_speech_prob": num(s.get("no_speech_prob")),
                 "compression_ratio": num(s.get("compression_ratio"))}
                for s in (getattr(out, "segments", None) or []) if (s.get("text") or "").strip()]
        mx.clear_cache()
        return {"language": getattr(out, "language", None), "segments": segs}


class Reranker:
    """Qwen3-Reranker-0.6B (8-bit MLX): reads query and document together, answers yes/no.

    All documents of one query go through the model as one batch. The shared head (system prompt, instruction,
    query) is computed once and its key/value cache copied to every row; documents are right-padded, which the
    causal mask keeps away from the real tokens, so each row scores exactly as it would alone (to bf16 rounding).
    Only the yes/no rows of the output layer are evaluated, in float32, at each document's last token.
    Cost is linear in documents x DOC_TOKENS: 10 documents x 128 tokens take ~0.25-0.5 s on an M4 Pro.

    Learned adapter (mse/finetune.py): when <data>/models/reranker-lora/CURRENT names an adapter, its LoRA layers
    are loaded on top of the 8-bit model (mlx_lm adapter_path). They only change linear projections, so the
    shared-prefix cache works unchanged; rank-8 q/v adapters on 16 layers add ~4% latency. MSE_RERANK_ADAPTER
    overrides the pointer (an adapter id or directory, or "none"). An adapter that fails to load is logged and the
    base model is used instead: a bad adapter never breaks search. No CURRENT file: the base model, as before.
    """

    PREFIX = ("<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the "
              "Instruct provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n<|im_start|>user\n")
    SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    INSTRUCT = "Given a search query over a person's documents, judge whether the document is what they are looking for"
    DOC_TOKENS = 128    # per document (title, path, start of the best passage); 96-640 all measured alike
    BATCH = 16
    ADAPTERS = "reranker-lora"      # <data>/models/<ADAPTERS>/CURRENT and /<id>/ (finetune.py's registry)

    def __init__(self, model_path: Path, adapter=None):
        """`adapter`: None = the active one (MSE_RERANK_ADAPTER, else CURRENT); False = none; or a directory."""
        from mlx_lm import load

        t = time.time()
        mx.set_cache_limit(128 * 1024 * 1024)
        self.adapter, self.adapter_error, self.model = None, None, None
        adir = self.find_adapter() if adapter is None else (Path(adapter) if adapter else None)
        if adir is not None:
            try:
                if not (adir / "adapters.safetensors").is_file():
                    raise FileNotFoundError(f"no adapter at {adir}")
                self.model, self.tok = load(str(model_path), adapter_path=str(adir))
                self.adapter = adir.name
            except Exception as e:
                self.model, self.adapter_error = None, f"{type(e).__name__}: {e}"
                log(f"reranker adapter not loaded, using the base model: {self.adapter_error}")
        if self.model is None:
            self.model, self.tok = load(str(model_path))
        self.yes = self.tok.convert_tokens_to_ids("yes")
        self.no = self.tok.convert_tokens_to_ids("no")
        self.pre = self.tok.encode(self.PREFIX, add_special_tokens=False)
        self.suf = self.tok.encode(self.SUFFIX, add_special_tokens=False)
        self.pad = self.tok.pad_token_id if self.tok.pad_token_id is not None else self.tok.eos_token_id
        # Tied input/output embeddings (Qwen3-0.6B): the two output rows we need, dequantized once.
        self.lm_head = getattr(self.model, "lm_head", None)
        self.yes_no = None if self.lm_head is not None else \
            self.model.model.embed_tokens(mx.array([self.no, self.yes])).astype(mx.float32)
        log(f"loaded reranker in {time.time() - t:.1f}s" + (f" with adapter {self.adapter}" if self.adapter else ""))

    @classmethod
    def find_adapter(cls) -> Path | None:
        """The adapter directory to load: MSE_RERANK_ADAPTER if set (id, directory, or none/off/base), else the id
        named in <data>/models/reranker-lora/CURRENT. None: the base model."""
        from mse import paths

        root = paths.MODELS / cls.ADAPTERS
        want = os.environ.get("MSE_RERANK_ADAPTER")
        if want is None:
            try:
                want = (root / "CURRENT").read_text()
            except OSError:
                return None
        want = want.strip()
        if want.lower() in ("", "none", "off", "base"):
            return None
        p = Path(want).expanduser()
        return p if p.is_absolute() else root / want

    def info(self) -> dict:
        """For the worker's ping / ready reply: which adapter is active (None: the base model)."""
        return {"adapter": self.adapter, "adapter_error": self.adapter_error}

    def _yes_logits(self, h: mx.array) -> mx.array:
        """(B, hidden) last-token states -> (B, 2) logits for [no, yes]."""
        if self.yes_no is not None:
            return h.astype(mx.float32) @ self.yes_no.T
        return self.lm_head(h)[:, mx.array([self.no, self.yes])].astype(mx.float32)

    def margin(self, h: mx.array) -> mx.array:
        """(B, hidden) last-token states -> (B,) logit(yes) - logit(no). score() reports its sigmoid (the softmax
        probability of "yes"); finetune.py trains on it directly."""
        lg = self._yes_logits(h)
        return lg[:, 1] - lg[:, 0]

    def encode(self, query: str, docs: list[str], doc_tokens: int = DOC_TOKENS) -> tuple[list[int], list[list[int]]]:
        """Token ids: the shared head (system prompt, instruction, query) and per document its first `doc_tokens`
        tokens plus the answer prompt. score() and the fine-tune use this one function, so they see the same input."""
        head = self.pre + self.tok.encode(f"<Instruct>: {self.INSTRUCT}\n<Query>: {query}\n<Document>:",
                                          add_special_tokens=False)
        return head, [self.tok.encode(" " + d, add_special_tokens=False)[:doc_tokens] + self.suf for d in docs]

    def score(self, query: str, docs: list[str], doc_tokens: int = DOC_TOKENS) -> list[float]:
        from mlx_lm.models.cache import make_prompt_cache

        if not docs:
            return []
        head, seqs = self.encode(query, docs, doc_tokens)
        base = make_prompt_cache(self.model)
        self.model.model(mx.array([head]), cache=base)
        kv = [(c.keys[..., :c.offset, :], c.values[..., :c.offset, :], c.offset) for c in base]
        out = [0.0] * len(seqs)
        order = sorted(range(len(seqs)), key=lambda i: len(seqs[i]))    # similar lengths batch together
        for s in range(0, len(order), self.BATCH):
            part = order[s:s + self.BATCH]
            n, width = len(part), max(len(seqs[i]) for i in part)
            cache = make_prompt_cache(self.model)
            for c, (k, v, off) in zip(cache, kv):
                c.keys, c.values, c.offset = mx.repeat(k, n, axis=0), mx.repeat(v, n, axis=0), off
            ids = mx.array([seqs[i] + [self.pad] * (width - len(seqs[i])) for i in part])
            h = self.model.model(ids, cache=cache)
            last = h[mx.arange(n), mx.array([len(seqs[i]) - 1 for i in part])]
            p = mx.softmax(self._yes_logits(last), axis=-1)[:, 1]
            for i, v in zip(part, p.tolist()):
                out[i] = float(v)
        mx.clear_cache()
        return out


class Embedder:
    def __init__(self, model_path: Path, mode: str):
        from mlx_vlm.utils import load_config

        self.mode = mode
        t = time.time()
        if mode == "text":
            from mlx_vlm.embedding_loader import load_embedding_model
            from tokenizers import Tokenizer

            # Small GPU cache and the raw Rust tokenizer: the transformers wrapper alone costs ~575 MB
            # for Gemma's 262k vocabulary, the raw tokenizer ~260 MB with identical token ids.
            mx.set_cache_limit(128 * 1024 * 1024)
            cfg = load_config(model_path)
            cfg["audio_config"] = None
            cfg["vision_config"] = None
            self.model = load_embedding_model(model_path, config=cfg)
            self.tok = Tokenizer.from_file(str(model_path / "tokenizer.json"))
            self.tok.enable_truncation(2048)
            self.tok.enable_padding(pad_id=0, pad_token="<pad>", direction="right")
            self.processor = None
        else:
            from mlx_vlm import load

            mx.set_cache_limit(512 * 1024 * 1024)
            self.model, self.processor = load(str(model_path))
            self.tokenizer = self.processor.tokenizer
            self.tok = None
        log(f"loaded {mode} model in {time.time() - t:.1f}s")

    def text(self, texts: list[str]) -> np.ndarray:
        if self.tok is not None:
            enc = self.tok.encode_batch(texts)
            inp = {"input_ids": np.array([e.ids for e in enc], dtype=np.int32),
                   "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int32)}
        else:
            inp = self.tokenizer(texts, padding=True, truncation=True, max_length=2048, return_tensors="np")
        e = self.model(**{k: mx.array(v) for k, v in inp.items()}).text_embeds
        mx.eval(e)
        return np.array(e, dtype=np.float32)

    def _conv(self, item: dict) -> list:
        title = (item.get("title") or "none").replace("|", " ")[:200]
        return [
            {"role": "system", "content": f"title: {title} | text: "},
            {"role": "user", "content": [{"type": item["type"], "url": item["path"]}]},
        ]

    def media(self, items: list[dict]) -> tuple[np.ndarray, list[bool]]:
        if self.processor is None:
            raise RuntimeError("media needs --mode full")
        out = np.full((len(items), DIMS), np.nan, dtype=np.float32)
        ok = [False] * len(items)

        def run(idx):
            inputs = self.processor.apply_chat_template([self._conv(items[i]) for i in idx], tokenize=True,
                                                        return_dict=True, return_tensors="mlx")
            e = self.model(**inputs).text_embeds
            mx.eval(e)
            return np.array(e, dtype=np.float32)

        idx = list(range(len(items)))
        try:
            out[idx] = run(idx)
            ok = [True] * len(items)
        except Exception:
            for i in idx:
                try:
                    out[i] = run([i])[0]
                    ok[i] = True
                except Exception as e:
                    log(f"media failed: {items[i].get('path')}: {e}")
        mx.clear_cache()
        return out, ok


class Generator:
    """Small instruct model for Ask mode (mlx-lm, 4-bit). Streams the answer token by token.

    Thinking is switched off (enable_thinking=False; templates without that switch ignore it). Kernels are
    compiled by a one-token warm-up at load, so the first real question does not pay for it.
    """

    MAX_PROMPT_TOKENS = 8192
    PREFILL_STEP = 512      # prompt read in 512-token steps: peak memory on a 2.1k-token prompt 3.2 -> 3.0 GB
                            # (Qwen3-4B) and 4.0 -> 3.3 GB (Qwen3.5-4B), for 8-14% slower prefill

    def __init__(self, model_path: Path):
        from mlx_lm import load

        t = time.time()
        mx.set_cache_limit(256 * 1024 * 1024)
        self.model, self.tok = load(str(model_path))
        self.generate([{"role": "user", "content": "Hi"}], max_tokens=1, temperature=0.0, top_p=1.0)
        log(f"loaded ask model in {time.time() - t:.1f}s")

    def prompt(self, messages: list[dict]) -> list[int]:
        try:
            text = self.tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False,
                                                enable_thinking=False)
        except TypeError:
            text = self.tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
        return self.tok.encode(text, add_special_tokens=False)     # the template already has any BOS

    def generate(self, messages: list[dict], max_tokens: int = 300, temperature: float = 0.1, top_p: float = 0.9,
                 emit=None, cancelled=None) -> dict:
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler

        ids = self.prompt(messages)
        if len(ids) > self.MAX_PROMPT_TOKENS:
            raise ValueError(f"prompt too long: {len(ids)} tokens")
        mx.reset_peak_memory()
        t = time.time()
        first, r, finish = None, None, None
        for r in stream_generate(self.model, self.tok, prompt=ids, max_tokens=max_tokens,
                                 sampler=make_sampler(temp=temperature, top_p=top_p),
                                 prefill_step_size=self.PREFILL_STEP):
            if first is None:
                first = time.time()
            if r.text and emit:
                emit({"token": r.text})
            if cancelled and cancelled():
                finish = "cancelled"
                break
        end = time.time()
        mx.clear_cache()
        return {"done": True, "prompt_tokens": len(ids), "tokens": r.generation_tokens if r else 0,
                "model_ttft_ms": round(((first or end) - t) * 1000), "gen_ms": round((end - (first or end)) * 1000),
                "tok_s": round(r.generation_tps, 1) if r else None, "prompt_tps": round(r.prompt_tps) if r else None,
                "peak_mb": round(r.peak_memory * 1000) if r else None,
                "finish": finish or (r.finish_reason if r else None)}


def _stdin_op() -> str | None:
    """A cancel/quit sent while a request is streaming (checked between tokens, never blocks)."""
    r, _, _ = select.select([sys.stdin], [], [], 0)
    if not r:
        return None
    line = sys.stdin.readline()
    if not line:
        return "quit"
    try:
        op = json.loads(line).get("op")
    except Exception:
        return None
    return op if op in ("cancel", "quit") else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--mode", choices=["text", "full", "asr", "rerank", "ask"], default="text")
    ap.add_argument("--processor")
    a = ap.parse_args()
    if a.mode == "asr":
        emb = Transcriber(Path(a.model), Path(a.processor))
    elif a.mode == "rerank":
        emb = Reranker(Path(a.model))
    elif a.mode == "ask":
        emb = Generator(Path(a.model))
    else:
        emb = Embedder(Path(a.model), a.mode)
    info = emb.info() if hasattr(emb, "info") else {}      # reranker: which LoRA adapter is active
    sys.stdout.write(json.dumps({"ready": True, "mode": a.mode, "pid": os.getpid(), **info}) + "\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        rid = req.get("id")
        stop_after = False
        try:
            if req["op"] == "quit":
                break
            if req["op"] == "cancel":
                continue                    # arrived after its request had already finished
            if req["op"] == "ping":
                reply = {"id": rid, "pong": True, **info}
            elif req["op"] == "ask":
                seen = {}

                def emit(m, rid=rid):
                    sys.stdout.write(json.dumps({"id": rid, **m}) + "\n")
                    sys.stdout.flush()

                def cancelled(seen=seen):
                    op = _stdin_op()
                    if op:
                        seen["op"] = op
                    return bool(op)

                reply = {"id": rid, **emb.generate(req["messages"], int(req.get("max_tokens", 300)),
                                                   float(req.get("temperature", 0.1)), float(req.get("top_p", 0.9)),
                                                   emit, cancelled)}
                stop_after = seen.get("op") == "quit"
            elif req["op"] == "text":
                v = emb.text(req["texts"])
                reply = {"id": rid, "vecs": base64.b64encode(v.tobytes()).decode(), "ok": [True] * len(v)}
            elif req["op"] == "media":
                v, ok = emb.media(req["items"])
                reply = {"id": rid, "vecs": base64.b64encode(v.tobytes()).decode(), "ok": ok}
            elif req["op"] == "rerank":
                reply = {"id": rid, "scores": emb.score(req["query"], req["docs"])}
            elif req["op"] == "transcribe":
                reply = {"id": rid, **emb.transcribe(req["path"])}
            else:
                reply = {"id": rid, "error": f"unknown op {req['op']}"}
        except Exception as e:
            log(traceback.format_exc())
            reply = {"id": rid, "error": str(e)}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
        if stop_after:
            break


if __name__ == "__main__":
    main()
