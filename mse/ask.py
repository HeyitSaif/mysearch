"""Ask mode: a short answer to a question, grounded only in the top search results, with [n] citations.

  question -> search (precise) -> best chunk(s) per document -> numbered passages within a token budget
           -> a model streams the answer: a cloud model (mse/providers.py, mse/cloud.py) while online, or the small
              local instruct model (mlx-lm, its own worker process)
           -> every [n] is checked against the passage set; markers that point nowhere are dropped

Where the answer is written (plan_route): route "cloud" (the default) uses the active cloud provider and model and
NEVER the local model: when the cloud cannot answer, the question ends with an error that says how to choose another
model or switch to Local. The local model answers only when the user asked for it: route "local", the chat's or
question's Local switch (`mse ask --local`), a folder with "ask_cloud": false in scope, or the option
ask.local_fallback ("Use the local model when no cloud model is reachable", off by default; the old route "auto" is
cloud + local_fallback). Nothing preloads it: not switching Ask on, not starting the app. With cloud_redact (default) personal identifiers are masked in the passages and the history before they leave.
The local model is never loaded for an answer the cloud writes.

Metadata-only files never contribute text (their contents are not indexed, and Ask respects that).
The local model is loaded only while Ask is switched on and an answer needs it, and unloaded after
`idle_unload_min` idle minutes or immediately when Ask is switched off (embedder.Pool.stop("ask")).

Events (POST /api/ask streams them as NDJSON, one object per line):
  {"type": "sources", "sources": [{"n", "docid": "#…", "collection", "path", "title", "page", "ts", "snippet"}]}
  {"type": "token", "text": "…"}                  many; the text carries [n] markers (always canonical: [1][3])
  {"type": "done", "citations": [1, 3], "model": "…", "provider": "anthropic" | … | "local",
   "where": "cloud" | "local", "training"?: true (a model its provider's training_models marks: the provider may use inputs for training),
   "fallback_reason"?: "…", "redacted": n, "timings": {"ttft_ms", "total_ms", …},
   "answer": "…", "answered": bool, "dropped_citations": n,
   "search_query": "…", "rewrite": "none" | "heuristic" | "model", "rewrite_fallback"?: "…", "history_used"?: n}
      answer = the full cleaned text; answered = False for "not in the documents"; model = the model that wrote it;
      fallback_reason (only when the local model answered although the route allowed the cloud): "offline",
      "no_key", "ask_cloud_off", "unknown_provider", "no_model", "insecure_url", "training_unconfirmed", "keychain: …" or "error: …" (the
      cloud call failed before its first token); redacted = identifiers masked in the prompt the model received
  {"type": "error", "message": "…"}

Follow-up questions (multi-turn): `history` = the last turns [{"q", "a"}], oldest first, at most 3. A follow-up
("and when was it paid?") is searched for what it means (search_query: a heuristic, or the loaded model in one short
generation), and the turns, compacted, go into the prompt so the model can resolve references. History never changes
the scope and is never a source: citations refer only to the current passages.
"""

import contextlib
import contextvars
import html
import re
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Iterator

from . import config, paths
from .redact import MASK
from .textutil import STOP, WORD, detect_lang, fold, query_terms, snippet

DEFAULT_MODEL = "qwen3-4b-instruct-2507-4bit"

ASK_DEFAULTS = {
    "enabled": False,            # off: no model is called or loaded
    **config.ASK_CLOUD_DEFAULTS,  # route, provider, model (the cloud model), cloud_redact, cloud_* knobs
    "local_model": str(paths.MODELS / DEFAULT_MODEL),
    "max_tokens": 300,           # answers are at most 5 sentences; 300 tokens is a hard stop, not a target
    "passages": 8,               # numbered passages sent to the model
    "context_tokens": 3000,      # budget for the passages (the prompt adds ~250 tokens of rules)
    "results": 10,               # search results considered
    "min_relevance": 20,         # results below this (0-100, relative to the top hit) are not read
    "idle_unload_min": 5,        # the ask worker unloads after this many idle minutes
    "temperature": 0.1,          # low: grounded answers should not wander
    "top_p": 0.9,
    "rewrite_model": True,       # follow-ups: the loaded model rewrites the search query (<= 40 tokens, temperature 0)
    "rewrite_budget_ms": 1500,   # ... within this time; otherwise, or while the model is not loaded, the heuristic query
}

PASSAGE_CHARS = 1200         # one passage at most (index chunks are <= 1600 characters)
SNIPPET_CHARS = 220
TEXT_MODALITIES = ("text", "ocr", "transcript")

NOT_FOUND = {
    "en": "The documents don't answer this.",
    "de": "Die Dokumente beantworten diese Frage nicht.",
}
LANG_NAME = {"en": "English", "de": "German"}

SYSTEM = """You answer questions about the user's own documents.
Rules:
- Use only the numbered passages. No outside knowledge, no guessing.
- After every sentence that states a fact, put the number of the passage it comes from in square brackets, like [2] or [1][3].
- When passages disagree, prefer the most recent one (dates are in the passage headers) and say that older records differ.
- If the passages contain nothing that answers the question, reply with exactly this sentence and nothing else: {not_found}
  Never add that sentence to an answer.
- If they answer only part of the question, give that part and say what is missing.
- Be brief: at most 5 short sentences. No headings, no bullet lists, no list of sources at the end.
- Write in {language}."""

# Added to SYSTEM (before its last line) only when the question follows earlier turns, so a first question's prompt
# stays exactly the one that was benchmarked.
HISTORY_RULE = ("- The conversation so far only tells you what the question refers to. It is not a source: take every "
                "fact from the numbered passages and cite only their numbers.")
CONVO_HEAD = "Conversation so far (only to understand the question; it is not a source, never cite it):"


def estimate_tokens(text: str) -> float:
    """Prompt tokens for `text` without loading a tokenizer in the daemon. Qwen tokenizers spend one token per
    digit, so number-heavy records cost far more than prose: a flat chars/token ratio under-estimated real
    passages by up to 54%. Least-squares fits on 182 passages of one collection had 6-8% mean error (one fit per
    tokenizer, Qwen3 and Qwen3.5); these coefficients are the larger of the two fits."""
    letters = sum(1 for ch in text if ch.isalpha())
    digits = sum(1 for ch in text if ch.isdigit())
    other = sum(1 for ch in text if not ch.isalnum() and not ch.isspace())
    return 0.29 * letters + 1.5 * digits + 0.5 * other


class AskDisabled(RuntimeError):
    """Ask is switched off: the server answers 409 {"error": "ask_disabled"}."""


# ------------------------------------------------------------------ settings

def settings(cfg: dict | None = None) -> dict:
    """The "ask" settings with their defaults; "providers" is the merged provider view (built-ins + custom), never
    a key. Before cloud models, "model" named the local model's folder: such a path is read as local_model."""
    cfg = cfg if cfg is not None else config.load()
    raw = dict(cfg.get("ask") or {})
    m = raw.get("model")
    if isinstance(m, str) and m.strip().startswith(("/", "~")):
        raw.setdefault("local_model", m)
        raw.pop("model")
    s = {**ASK_DEFAULTS, **raw}
    s["providers"] = config.provider_entries({"ask": raw})
    return s


def model_name(s: dict | None = None) -> str:
    """The local model's name (its folder)."""
    s = s or settings()
    return Path(s.get("local_model") or ASK_DEFAULTS["local_model"]).expanduser().name


def available(s: dict | None = None) -> bool:
    """The local model's files exist (config.json + weights)."""
    s = s or settings()
    p = Path(s.get("local_model") or ASK_DEFAULTS["local_model"]).expanduser()
    return (p / "config.json").is_file() and any(p.glob("*.safetensors"))


def next_route(s: dict, block: bool = False) -> dict:
    """Where the next question would be answered (no folder rule applied): {"where", "online", "model", "problem"?}.
    problem (why the cloud cannot answer now: "no_key", "offline", "unknown_provider"): with local_fallback the local
    model answers; without it the question would end with an error, and "where" stays "cloud".
    block=False never waits for the online check (GET /api/status)."""
    from . import providers
    local = {"where": "local", "online": None, "model": model_name(s)}
    mode, fallback = route_mode(s)
    if not s["enabled"] or mode == "local":
        return local
    p = (s.get("providers") or {}).get(s.get("provider"))
    if not p:
        return {**local, "problem": "unknown_provider"} if fallback else \
            {"where": "cloud", "online": None, "model": s["model"], "problem": "unknown_provider"}
    usable = providers.has_key(s["provider"], p=p) or not providers.needs_key(s["provider"], p)
    up = providers.online(p["base_url"], block=block) if usable else None
    problem = "no_key" if not usable else "offline" if up is False else None
    if problem and fallback:
        return {**local, "online": up, "problem": problem}
    return {"where": "cloud", "online": up, "model": s["model"], **({"problem": problem} if problem else {})}


def status() -> dict:
    """The "ask" block of GET /api/status. available / loaded / ram_mb / local_model describe the local model;
    route / provider / model / online describe the cloud side; where_next / next_model say who answers next."""
    from .embedder import pool
    s = settings()
    w = pool().workers.get("ask")
    loaded = bool(w and w.alive)
    nxt = next_route(s)
    return {"available": available(s), "enabled": bool(s["enabled"]), "loaded": loaded, "local_model": model_name(s),
            "ram_mb": w.footprint_mb() if loaded else None, "route": s["route"], "provider": s["provider"],
            "model": s["model"], "online": nxt["online"], "where_next": nxt["where"], "next_model": nxt["model"],
            "local_fallback": route_mode(s)[1], "next_problem": nxt.get("problem"),
            "training": config.trains_on_inputs(s["model"], (s.get("providers") or {}).get(s["provider"])),
            "training_confirmed": s["model"] in (s.get("allow_training_models") or [])}


def local_models(s: dict | None = None) -> dict:
    """GET /api/ask/local-models: the answer models installed on this Mac (detected by their files, nothing loaded)
    -> {"current": name, "models": [{"name", "size_mb", "current"}]}. Choose one with
    POST /api/settings {"ask": {"local_model": name}}; the running Ask worker is unloaded, so the next local answer
    uses it."""
    s = s or settings()
    cur = model_name(s)
    return {"current": cur, "models": [{"name": m["name"], "size_mb": m["size_mb"], "current": m["name"] == cur}
                                       for m in config.local_chat_models()]}


# ------------------------------------------------------------------ passages

def _plain(text: str) -> str:
    """Prose for the model: markdown/table syntax out, whitespace collapsed (same cleaning as search snippets)."""
    t = re.sub(r"^\s*\|?[-:| ]{3,}\|?\s*$", " ", text or "", flags=re.M)
    t = re.sub(r"\s*\|\s*(\|\s*)*", " · ", t)
    t = re.sub(r"\*\*|__|`+|^#+\s*|^\s*[-*]\s+\[[ x]\]\s*", "", t, flags=re.M)
    t = re.sub(r"\[([^\]]{1,120})\]\([^)]{1,300}\)", r"\1", t)
    return re.sub(r"\s+", " ", t).strip(" ·")


def _fragments(hit_snippet: str | None) -> list[str]:
    """The pieces of a search-hit snippet, folded, for finding the chunk that snippet was cut from."""
    if not hit_snippet:
        return []
    t = html.unescape(re.sub(r"</?mark>", "", hit_snippet))
    return [fold(p.strip()) for p in t.split("…") if len(p.strip()) >= 20]


def _ts(t0: float | None) -> str | None:
    if t0 is None:
        return None
    s = int(t0)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _window(text: str, keys: list[str], width: int) -> str:
    """At most `width` characters of `text`, around the densest cluster of query terms."""
    if len(text) <= width:
        return text
    folded = fold(text)
    pos = sorted(m.start() for k in keys for m in re.finditer(r"(?<!\w)" + re.escape(k), folded))
    start = 0
    if pos:
        best, best_n = pos[0], 0
        for p in pos:
            n = sum(1 for q in pos if p <= q < p + width)
            if n > best_n:
                best, best_n = p, n
        start = max(0, min(best - width // 5, len(text) - width))
        sp = text.find(" ", start)
        start = sp + 1 if 0 <= sp < start + 25 else start
    piece = text[start:start + width]
    return ("…" if start > 0 else "") + piece.strip() + ("…" if start + width < len(text) else "")


def db_chunks(result: dict, connect: Callable | None = None) -> list[dict]:
    """The indexed text chunks of a search result: its representative file plus any version a hit came from.
    Metadata-only files return nothing. `connect(collection)` opens the collection read-only."""
    from . import store
    connect = connect or (lambda name: store.connect(name, readonly=True))
    wanted = [result["path"]] + [h["from"] for h in result.get("hits") or [] if h.get("from")]
    wanted = list(dict.fromkeys(wanted))[:3]
    out = []
    conn = connect(result["collection"])
    try:
        for path in wanted:
            f = conn.execute("SELECT id, mode, docid FROM files WHERE path = ?", (path,)).fetchone()
            if not f or f[1] == "meta":
                continue
            rows = conn.execute(
                "SELECT modality, page, t0, t1, heading, text FROM items WHERE file_id = ? AND modality IN"
                f" ({','.join('?' * len(TEXT_MODALITIES))}) AND text IS NOT NULL ORDER BY ord",
                (f[0], *TEXT_MODALITIES)).fetchall()
            for r in rows:
                out.append({"path": path, "docid": f[2], "modality": r[0], "page": r[1], "t0": r[2], "t1": r[3],
                            "heading": r[4], "text": r[5]})
    finally:
        conn.close()
    return out


def _score_chunks(chunks: list[dict], hits: list[dict], keys: list[str]) -> list[tuple[float, int]]:
    """(score, index) per chunk, best first. The search engine's own best hits (fused keyword + meaning scores)
    count most; query-term coverage breaks ties and covers chunks the hits did not show."""
    frags = [(i, _fragments(h.get("snippet"))) for i, h in enumerate(hits)]
    scored = []
    for ci, c in enumerate(chunks):
        body = fold(_plain(c["text"]))
        s = 0.0
        for rank, fr in frags:
            h = hits[rank]
            if fr and any(f[:80] in body for f in fr) and (not h.get("from") or h["from"] == c["path"]):
                s += 3.0 / (1 + rank)
            elif h.get("page") and h.get("page") == c.get("page") and not fr:
                s += 1.0 / (1 + rank)
        if keys:
            s += sum(1 for k in keys if re.search(r"(?<!\w)" + re.escape(k), body)) / len(keys)
        scored.append((s, ci))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored


def build_passages(question: str, results: list[dict], chunks_fn: Callable[[dict], list[dict]],
                   max_passages: int = 8, context_tokens: int = 3000, min_relevance: float = 0,
                   extra_terms: Iterable[str] = ()) -> list[dict]:
    """Best-matching chunk(s) per document, in result order, within the budget, numbered 1..n.
    First one passage per document; leftover room goes to second passages of the top documents."""
    keys = sorted({fold(t) for t in list(query_terms(question)) + list(extra_terms) if len(t) >= 3},
                  key=len, reverse=True)
    budget = float(context_tokens)
    ranked = []                                   # per document: (result, chunks, [(score, idx)])
    for r in results:
        if r.get("metadata_only") or (r.get("relevance") is not None and r["relevance"] < min_relevance):
            continue
        try:
            chunks = chunks_fn(r)
        except Exception:
            chunks = []
        if chunks:
            ranked.append((r, chunks, _score_chunks(chunks, r.get("hits") or [], keys)))
    picked: list[tuple[int, int, dict]] = []      # (document order, pass, passage)
    used = 0.0

    def take(order: int, r: dict, c: dict, pass_no: int) -> bool:
        nonlocal used
        text = _window(_plain(c["text"]), keys, PASSAGE_CHARS)
        head = _header(r, c)
        cost = estimate_tokens(head + "\n" + text) + 4
        if used + cost > budget:
            return False
        used += cost
        picked.append((order, pass_no, {"result": r, "chunk": c, "text": text, "header": head}))
        return True

    for order, (r, chunks, sc) in enumerate(ranked):
        if len(picked) >= max_passages:
            break
        take(order, r, chunks[sc[0][1]], 0)
    for order, (r, chunks, sc) in enumerate(ranked):
        if len(picked) >= max_passages:
            break
        if len(sc) > 1 and sc[1][0] > 0 and any(p[0] == order for p in picked):
            take(order, r, chunks[sc[1][1]], 1)
    picked.sort(key=lambda x: (x[0], x[1]))
    out = []
    for n, (_, _, p) in enumerate(picked, 1):
        r, c = p["result"], p["chunk"]
        out.append({
            "n": n, "docid": "#" + str(c.get("docid") or r.get("docid") or "").lstrip("#"),
            "collection": r["collection"], "path": c.get("path") or r["path"], "title": r.get("title") or r.get("name"),
            "page": c.get("page"), "ts": _ts(c.get("t0")), "t0": c.get("t0"), "date": r.get("date"),
            "header": p["header"], "text": p["text"],
        })
    return out


def _header(r: dict, c: dict) -> str:
    title = r.get("title") or r.get("name") or ""
    where = c.get("path") or r["path"]
    bits = []
    if c.get("page"):
        bits.append(f"page {c['page']}")
    if c.get("t0") is not None:
        bits.append(f"at {_ts(c['t0'])}")
    if r.get("date"):
        bits.append(str(r["date"]))
    if c.get("heading"):
        bits.append(f"section: {c['heading']}")
    return f"{title} — {where}" + (f" ({', '.join(bits)})" if bits else "")


# ------------------------------------------------------------------ prompt

def language(question: str, history: Iterable[dict] = ()) -> str:
    """The answer language: the question's; a follow-up too short to tell ("Acme?") takes the latest earlier
    question's; English when neither says."""
    lang = detect_lang(question)
    for t in reversed(list(history)):
        if lang in NOT_FOUND:
            break
        lang = detect_lang(t["q"])
    return lang if lang in NOT_FOUND else "en"


def history_block(turns: list[dict]) -> str:
    lines = [CONVO_HEAD]
    for t in turns:
        lines.append(f"Q: {t['q']}")
        if t.get("a"):
            lines.append(f"A: {t['a']}")
    return "\n".join(lines)


def build_messages(question: str, passages: list[dict], lang: str, history: list[dict] | None = None,
                   meaning: str | None = None) -> list[dict]:
    """history: compacted earlier turns (compact_history / fit_history), placed between the passages and the
    question; meaning: the model's standalone rewrite of a follow-up, shown under the question."""
    blocks = [f"[{p['n']}] {p['header']}\n{p['text']}" for p in passages]
    user = "Passages:\n\n" + "\n\n".join(blocks)
    if history:
        user += "\n\n" + history_block(history)
    user += f"\n\nQuestion: {question.strip()}"
    if history and meaning and fold(meaning.strip()) != fold(question.strip()):
        user += f"\n(In context: {meaning.strip()})"
    user += f"\n\nAnswer in {LANG_NAME[lang]}, citing the passages as [n]."
    system = SYSTEM.format(not_found=NOT_FOUND[lang], language=LANG_NAME[lang])
    if history:
        head, last = system.rsplit("\n", 1)
        system = f"{head}\n{HISTORY_RULE}\n{last}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ------------------------------------------------------------------ citations

_CITE = re.compile(r"\[\s*(\d{1,3}(?:\s*(?:[,;]|–|-|und|and)\s*\d{1,3})*)\s*\]")
_CITE_PART = re.compile(r"^\[[\d\s,;–\-]*(?:u|un|und|a|an|and)?[\d\s,;–\-]*$")


def _numbers(inner: str) -> list[int]:
    out = []
    for part in re.split(r"\s*(?:[,;]|und|and)\s*", inner.strip()):
        m = re.fullmatch(r"(\d{1,3})\s*[–-]\s*(\d{1,3})", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out += list(range(a, b + 1)) if 0 < b - a < 10 else [a, b]
        elif part.isdigit():
            out.append(int(part))
    return out


def clean_citations(text: str, valid: set[int]) -> tuple[str, list[int], int]:
    """Rewrite every [n] / [n, m] / [n-m] marker as canonical [n][m] keeping only numbers in `valid`.
    Returns (text, cited numbers in order of first appearance, number of invalid references dropped)."""
    cited, dropped = [], 0

    def sub(m):
        nonlocal dropped
        nums = _numbers(m.group(1))
        keep = [n for n in dict.fromkeys(nums) if n in valid]
        dropped += len(nums) - len(keep)
        for n in keep:
            if n not in cited:
                cited.append(n)
        return "".join(f"[{n}]" for n in keep)

    out = _CITE.sub(sub, text)
    out = re.sub(r"[ \t]+([.,;:!?])", r"\1", out)          # a dropped marker leaves no space before punctuation
    return re.sub(r"[ \t]{2,}", " ", out), cited, dropped


class CitationStream:
    """Cleans citations while tokens stream: text that might be the start of a [n] marker is held back until
    the marker closes (or turns out to be ordinary text), so the client never sees a marker that points nowhere.
    Also swallows a leading <think>…</think> block, should a model emit one."""

    HOLD = 32

    def __init__(self, valid: set[int]):
        self.valid = valid
        self.buf = ""
        self.ws = ""                 # whitespace held back in front of a possible marker
        self.started = False
        self.thinking = False
        self.out: list[str] = []
        self.cited: list[int] = []
        self.dropped = 0

    def _emit(self, s: str) -> str:
        if not self.started:
            s = s.lstrip()
            if not s:
                return ""
            self.started = True
        self.out.append(s)
        return s

    def feed(self, text: str) -> str:
        self.buf += text
        if not self.started and not self.thinking and self.buf.lstrip().startswith("<think>"):
            self.thinking = True
        if self.thinking:
            end = self.buf.find("</think>")
            if end < 0:
                return ""
            self.buf, self.thinking = self.buf[end + len("</think>"):], False
        emitted = []
        while self.buf:
            i = self.buf.find("[")
            if i < 0:
                emitted.append(self._emit(self.ws + self.buf))
                self.ws, self.buf = "", ""
                break
            if i > 0:                                       # text before the bracket; its trailing space waits
                pre = self.buf[:i]
                core = pre.rstrip(" \t")
                if core:
                    emitted.append(self._emit(self.ws + core))
                    self.ws = ""
                self.ws += pre[len(core):]
                self.buf = self.buf[i:]
            j = self.buf.find("]")
            if j < 0:
                if len(self.buf) <= self.HOLD and _CITE_PART.match(self.buf):
                    break                                   # could still become a citation: wait
                emitted.append(self._emit(self.ws + self.buf[0]))      # an ordinary bracket
                self.ws, self.buf = "", self.buf[1:]
                continue
            piece = self.buf[:j + 1]
            if _CITE.fullmatch(piece):
                cleaned, cited, dropped = clean_citations(piece, self.valid)
                self.dropped += dropped
                self.cited += [n for n in cited if n not in self.cited]
                if cleaned:
                    emitted.append(self._emit(self.ws + cleaned))
                self.ws, self.buf = "", self.buf[j + 1:]    # a dropped marker takes its space with it
            else:
                emitted.append(self._emit(self.ws + self.buf[0]))
                self.ws, self.buf = "", self.buf[1:]
        return "".join(e for e in emitted if e)

    def flush(self) -> str:
        rest, self.buf, ws, self.ws = self.buf, "", self.ws, ""
        if self.thinking or (rest and _CITE_PART.match(rest)):
            return ""                                       # an unfinished marker (answer cut off): drop it
        return self._emit(ws + rest) if (ws + rest) else ""

    def text(self) -> str:
        t, _, _ = clean_citations("".join(self.out), self.valid)
        return t.strip()


_REFUSAL = re.compile(r"\b(documents?|passages?|records?|dokumente\w*|unterlagen|passagen)\b[^.]{0,80}?"
                      r"\b(do(?:es)?\s+not|don't|doesn't|contain no|no information|keine|nicht)\b", re.I)


def is_not_found(answer: str, cited: Iterable[int] = ()) -> bool:
    """The model said the passages do not answer the question: the exact sentence it was told to use, or, with
    no citation at all, a paraphrase of it ("The provided documents do not state ...", "Die Dokumente enthalten
    keine ...") as other small models write it."""
    a = fold(answer).strip()
    if not a or any(a.startswith(fold(nf).rstrip(".")) for nf in NOT_FOUND.values()):
        return True
    return not list(cited) and bool(_REFUSAL.search(answer))


# ------------------------------------------------------------------ follow-up questions
#
# A chat client sends the last turns with each question. They change what is searched (a follow-up is searched for
# what it means) and what the prompt shows (the turns, compacted). They never change where the search runs, and the
# answer cites only the current passages.

MAX_HISTORY = 3              # turns a client may send, oldest first
HISTORY_CHARS = 2000         # each q / a as received; longer strings are cut, not refused
HISTORY_WORDS = 60           # each q / a in the answer prompt
HISTORY_TOKENS = 600         # the conversation's part of context_tokens (at most a quarter); passages get the rest
REWRITE_WORDS = 40           # each q / a in the rewrite prompt (short: it is read before the search can start)
REWRITE_TOKENS = 40          # the rewritten question is one short line
CARRY_TERMS = 5              # heuristic: key terms carried over from the previous question


def validate_history(history) -> list[dict]:
    """The API's `history`: absent, null, [] or a list of at most MAX_HISTORY {"q": str, "a": str}, oldest first.
    Other keys are ignored; strings are cut to HISTORY_CHARS. Anything else -> ValueError (a 400 at the API)."""
    if history is None:
        return []
    if not isinstance(history, list):
        raise ValueError("history must be a list of {q, a} objects, oldest first")
    if len(history) > MAX_HISTORY:
        raise ValueError(f"history takes at most {MAX_HISTORY} turns (the most recent, oldest first)")
    out = []
    for i, t in enumerate(history):
        if not isinstance(t, dict):
            raise ValueError(f"history[{i}] must be an object with string q and a")
        q, a = t.get("q"), t.get("a")
        if not isinstance(q, str) or not q.strip():
            raise ValueError(f"history[{i}].q must be a non-empty string")
        if not isinstance(a, str):
            raise ValueError(f"history[{i}].a must be a string")
        out.append({"q": q.strip()[:HISTORY_CHARS], "a": a.strip()[:HISTORY_CHARS]})
    return out


def _folded(words: str) -> frozenset:
    return frozenset(fold(w) for w in words.split())


# question and helper words that say nothing about the subject (textutil.STOP lacks some German ones: wann, wurde)
_QWORDS = _folded("""what when where which who whom whose why how much many does did do is are was were has have
    had can could would should will shall
    wann wie was wer wen wem wessen wo woher wohin wofür wozu womit woran warum weshalb wieso welche welcher welches
    welchem welchen wieviel wieviele viel viele wurde wurden worden ist sind war waren hat haben hatte hatten gibt gab
    kann konnte muss musste soll sollte eigentlich genau bitte denn mal noch schon""")
# words that point back to something said earlier
_ANAPHORA = _folded("""it its itself they them their theirs this that these those he him his she her hers then
    same former latter one ones such
    es sie ihn ihm ihr ihre ihren ihrem ihrer ihres er dies diese dieser dieses diesem diesen dort damals dafür
    davon dazu darüber daran darauf dabei damit dagegen danach davor dasselbe dieselbe derselbe denselben demselben
    selbe selben jene jener jenes deren dessen""")
# a question that opens like this continues the conversation ("and when …", "what about …", "und für …")
_LEAD = re.compile(r"^\W*(?:and|but|also|or|plus|what about|how about|what of|same for|und|aber|auch|oder|sowie|"
                   r"was ist mit|wie ist es mit|wie sieht es (?:mit|bei)|wie steht es (?:mit|um)|dasselbe fur|"
                   r"das gleiche fur|gilt das (?:auch )?fur)\b")
_FILTER_TOKEN = re.compile(r"(?<!\S)(\w+):(\"[^\"]*\"|\S+)")
_MASKED_IBAN = re.compile(r"\b[A-Z]{2}•+\s?[A-Z0-9]{1,4}\b")     # redact._iban's form: "DE•••• 3000"


def _split_filters(q: str) -> tuple[str, list[str]]:
    """(q without its inline filters, the filters) — "type:pdf", "after:2025-03" … as search.parse reads them."""
    found = []

    def cut(m):
        if m.group(1).lower() in config.FILTER_KEYS:
            found.append(m.group(0))
            return " "
        return m.group(0)
    rest = _FILTER_TOKEN.sub(cut, q or "")
    return re.sub(r"\s+", " ", rest).strip(), found


def _history_text(text: str, keep_mask: bool = False) -> str:
    """An earlier question without its scope shortcut and inline filters: they applied to that turn only, and
    history never chooses where the search runs. Masked identifiers are dropped too, unless kept for the prompt."""
    try:
        text = config.strip_scope(text)
    except Exception:
        pass
    text = _split_filters(text)[0]
    if keep_mask:
        return text
    text = _MASKED_IBAN.sub(" ", text.replace(MASK, " "))
    return re.sub(r"\s+", " ", text).strip()


def _strip_markers(text: str) -> str:
    """An earlier answer as context: no [n] markers (they pointed at that turn's passages), no markdown."""
    t = re.sub(r"<think>.*?(?:</think>|$)", " ", text or "", flags=re.S)
    t = re.sub(r"[ \t]+([.,;:!?])", r"\1", _CITE.sub("", t))
    return _plain(t)


def _trim_words(text: str, n: int) -> str:
    w = (text or "").split()
    return " ".join(w[:n]) + (" …" if len(w) > n else "")


def compact_history(history: list[dict], words: int = HISTORY_WORDS) -> list[dict]:
    """The turns as the answer prompt shows them: each question and answer cut to `words` words, markers removed."""
    out = []
    for t in history:
        q = _trim_words(_plain(_history_text(t["q"], keep_mask=True)), words)
        if q:
            out.append({"q": q, "a": _trim_words(_strip_markers(t.get("a") or ""), words)})
    return out


def history_cost(turns: list[dict], meaning: str | None = None) -> float:
    """Prompt tokens the conversation adds: its block, the extra rule and the "In context" line."""
    if not turns:
        return 0.0
    return (estimate_tokens(history_block(turns)) + estimate_tokens(HISTORY_RULE) + 8
            + (estimate_tokens(meaning) + 6 if meaning else 0))


def fit_history(turns: list[dict], budget: float, meaning: str | None = None) -> list[dict]:
    """Trim the conversation to `budget` tokens: oldest turns go first, then the last answer and question shrink."""
    turns = [dict(t) for t in turns]
    while turns and history_cost(turns, meaning) > budget:
        if len(turns) > 1:
            turns.pop(0)
            continue
        t = turns[0]
        a, q = _trim_words(t["a"], 15), _trim_words(t["q"], 15)
        if a != t["a"]:
            t["a"] = a
        elif q != t["q"]:
            t["q"] = q
        else:                                   # as short as it gets and still too long: no conversation
            turns.pop(0)
    return turns


def _content_terms(text: str) -> list[str]:
    """The words of a question that name its subject (no stop, question or pointing words), in order."""
    return [t for t in query_terms(text) if fold(t) not in STOP and fold(t) not in _QWORDS
            and fold(t) not in _ANAPHORA]


def _has_anaphora(text: str) -> bool:
    words = [fold(w) for w in re.findall(r"\w+", text)]
    return any(w in _ANAPHORA and not (w == "es" and i and words[i - 1] in ("gibt", "gab", "geht"))
               for i, w in enumerate(words))


def _names_its_subject(text: str, lang: str) -> bool:
    """A number, an acronym (USB, EU) or a proper name: in English a capitalised word inside the sentence; in
    German, where every noun is capitalised, a long compound ("Lieferantenrechnung")."""
    for i, t in enumerate(WORD.findall(text)):
        f = fold(t)
        if f in STOP or f in _QWORDS or f in _ANAPHORA:
            continue
        if any(ch.isdigit() for ch in t) or sum(ch.isupper() for ch in t) >= 2:
            return True
        if t[:1].isupper() and ((lang == "de" and len(t) >= 10) or (lang != "de" and i > 0)):
            return True
    return False


def is_follow_up(q: str) -> bool:
    """Does `q` lean on the conversation? Yes when it opens like a continuation ("and …", "what about …", "und …")
    or points back ("it", "they", "dort"), with at most four subject words; or when it is very short (at most two
    subject words) and either a bare fragment ("the printer?") or names no subject of its own ("Who paid?").
    Longer questions carry enough words to be searched as they are."""
    terms = _content_terms(q)
    if len(terms) > 4:
        return False
    if _LEAD.match(fold(q)) or _has_anaphora(q):
        return True
    words = [fold(w) for w in re.findall(r"\w+", q)]
    fragment = len(words) <= 3 and not any(w in _QWORDS for w in words)
    return len(terms) <= 2 and (fragment or not _names_its_subject(q, detect_lang(q)))


_DATE_WORDS = _folded("""today heute yesterday gestern week weeks woche wochen month months monat monate year years
    jahr jahre vorjahr vormonat quarter quartal last letzte letzten letzter letztes vorige vorigen""")


def _dateish(term: str) -> bool:
    """A month, a year or a relative-date word: search.parse turns these into a date filter."""
    from .search import MONTHS
    f = fold(term)
    return f in MONTHS or f in _DATE_WORDS or bool(re.fullmatch(r"(?:19|20)\d\d", f))


def _carry(q: str, prev: str) -> str:
    if not is_follow_up(q):
        return q
    terms = query_terms(q)
    have, extra = {fold(t) for t in terms}, []
    # a follow-up that names its own date or number replaces the earlier one ("and in April?"): carried, the old
    # date would become a date filter that contradicts the new one
    new_date = any(_dateish(t) for t in terms)
    new_number = any(ch.isdigit() for t in terms for ch in t)
    for t in _content_terms(prev):
        if (new_date and _dateish(t)) or (new_number and any(ch.isdigit() for ch in t)):
            continue
        if fold(t) not in have:
            have.add(fold(t))
            extra.append(t)
            if len(extra) >= CARRY_TERMS:
                break
    return f"{q} {' '.join(extra)}" if extra else q


def heuristic_query(q: str, history: list[dict]) -> str:
    """The search query for `q` without a model: a follow-up gets up to CARRY_TERMS key terms of the previous
    question appended, that question itself resolved the same way (so a chain of follow-ups keeps its subject).
    Purely additive: "what about the printer?" after a laptop warranty question searches
    "what about the printer? warranty laptop end", and the answer model, which sees the conversation, picks the
    printer passages."""
    prev = None
    for t in history:
        tq = _history_text(t["q"])
        prev = _carry(tq, prev) if prev else tq
    return _carry(q, prev) if prev else q


REWRITE_SYSTEM = """You rewrite the last question of a conversation about the user's documents as one standalone search question.
Rules:
- Replace words that point back (it, they, that, there, the same, es, sie, das, dort, dasselbe) with what they refer to.
- If the last question names something new ("what about X?", "und X?"), ask the earlier question about X instead.
- Keep names, numbers and dates exactly as written. Add nothing that is not in the conversation.
- If the last question already stands on its own, repeat it unchanged.
- Do not answer it. Write in the language of the last question.
- Reply with the question only, on one line."""


def rewrite_messages(q: str, history: list[dict]) -> list[dict]:
    """Kept short, because its reading time delays the search: every earlier question, the latest answer at
    REWRITE_WORDS words and older answers at 15 (references are nearly always to the latest exchange)."""
    lines = []
    for i, t in enumerate(history):
        lines.append("Q: " + _trim_words(_history_text(t["q"]), REWRITE_WORDS))
        a = _trim_words(_MASKED_IBAN.sub(" ", _strip_markers(t.get("a") or "").replace(MASK, " ")),
                        REWRITE_WORDS if i == len(history) - 1 else 15)
        if a:
            lines.append("A: " + a)
    user = ("Conversation:\n" + "\n".join(lines) + f"\n\nLast question: {_split_filters(q)[0]}\n\n"
            "Standalone question:")
    return [{"role": "system", "content": REWRITE_SYSTEM}, {"role": "user", "content": user}]


_REWRITE_LABEL = re.compile(r"^(?:standalone(?: search)? question|rewritten(?: question)?|search question|question|"
                            r"query|eigenständige frage|suchfrage|frage)\s*:\s*", re.I)


def clean_rewrite(text: str | None, q: str, history: list[dict]) -> str | None:
    """The model's rewrite as a search question, or None when it is unusable: empty or too long, an answer instead of
    a question (citation markers, the not-found sentence), a number that is not in the conversation, a number or
    acronym of the new question left out, or not one key term in common with the conversation."""
    if not text:
        return None
    t = re.sub(r"<think>.*?(?:</think>|$)", " ", text, flags=re.S)
    line = next((x.strip() for x in t.splitlines() if x.strip()), "")
    line = _REWRITE_LABEL.sub("", line).strip().strip("\"'“”„«»*`").strip()
    line = _split_filters(line.replace(MASK, " "))[0]           # filters and scope come from the request, never the model
    if not line or len(line) > 300 or _CITE.search(line):
        return None
    got = fold(line)
    if any(got.startswith(fold(nf).rstrip(".")) for nf in NOT_FOUND.values()):
        return None
    q_core = _split_filters(q)[0]
    said = fold(" ".join([q_core] + [f"{h['q']} {h.get('a') or ''}" for h in history]))
    known = [re.sub(r"\D", "", n) for n in re.findall(r"\d[\d.,/-]*", said)]
    if any(not any(re.sub(r"\D", "", n) in k for k in known) for n in re.findall(r"\d[\d.,/-]*", line)):
        return None                                             # a number the conversation never mentioned
    for term in query_terms(q_core):
        if (any(ch.isdigit() for ch in term) or sum(ch.isupper() for ch in term) >= 2) and \
                not re.search(r"(?<!\w)" + re.escape(fold(term)) + r"(?!\w)", got):
            return None                                         # dropped a number or acronym of the new question
    pool = _content_terms(q_core) + [x for h in history for x in _content_terms(_history_text(h["q"]))]
    if pool and not any(re.search(r"(?<!\w)" + re.escape(fold(x)), got) for x in pool):
        return None
    return line


def run_rewrite(messages: list[dict], gen_fn: Callable, budget_s: float) -> tuple[str | None, str | None]:
    """One short greedy generation, read in a helper thread so the request never waits longer than `budget_s`.
    -> (text, None), or (None, "timeout" | "error"). An abandoned generation is cancelled at its next token (the
    worker then drains it); the answer, which needs the same worker, waits for that, never for a load: the caller
    only rewrites while the model is already loaded."""
    out: list[str] = []
    box: dict = {}
    finished, abandon = threading.Event(), threading.Event()

    def run():
        g = None
        try:
            g = gen_fn(messages, max_tokens=REWRITE_TOKENS, temperature=0.0, top_p=1.0)
            for msg in g:
                if msg.get("done") or abandon.is_set():
                    break
                out.append(msg.get("token") or "")
                if "\n" in "".join(out).strip():
                    break                                       # one line is all that is used
        except Exception as e:
            box["error"] = e
        finally:
            close = getattr(g, "close", None)
            if close:
                try:
                    close()                                     # Worker.stream cancels an unfinished generation
                except Exception:
                    pass
            finished.set()

    threading.Thread(target=run, daemon=True, name="ask-rewrite").start()
    if not finished.wait(budget_s):
        abandon.set()
        return None, "timeout"
    if "error" in box:
        return None, "error"
    return "".join(out), None


def search_query(q: str, history: list[dict], s: dict, gen_fn: Callable | None = None,
                 model_history: list[dict] | None = None) -> dict:
    """What to search for `q` after `history` -> {"query", "rewrite": "none" | "heuristic" | "model",
    "meaning"? (the model's rewrite), "fallback"? (why the model's rewrite was not used), "rewrite_ms"?}.
    Only follow-ups (is_follow_up) are rewritten. `gen_fn`: the model that rewrites (the loaded local model or the
    cloud model), or None when there is none. `model_history`: the history as that model may see it (masked before a
    cloud call); the heuristic always reads `history`."""
    if not history or not is_follow_up(q):
        return {"query": q, "rewrite": "none"}
    heur = heuristic_query(q, history)
    out = {"query": heur, "rewrite": "heuristic" if heur != q else "none"}
    if not s.get("rewrite_model", True):
        return {**out, "fallback": "off"}
    if gen_fn is None:
        return {**out, "fallback": "model_not_loaded"}
    mh = history if model_history is None else model_history
    t = time.time()
    text, why = run_rewrite(rewrite_messages(q, mh), gen_fn, float(s.get("rewrite_budget_ms", 1500)) / 1000)
    ms = round((time.time() - t) * 1000)
    line = clean_rewrite(text, q, mh) if why is None else None
    if line is None:
        return {**out, "fallback": why or "unusable", "rewrite_ms": ms}
    return {"query": " ".join([line] + _split_filters(q)[1]), "rewrite": "model", "meaning": line, "rewrite_ms": ms}


# ------------------------------------------------------------------ the whole flow

_STEPS: contextvars.ContextVar = contextvars.ContextVar("ask_steps", default=False)


@contextlib.contextmanager
def step_events():
    """Inside this block, ask() yields step events (see its docstring)."""
    token = _STEPS.set(True)
    try:
        yield
    finally:
        _STEPS.reset(token)


def ask(q: str, collections: list[str] | None = None, filters: dict | None = None, agent_safe: bool = False,
        search_fn: Callable | None = None, chunks_fn: Callable | None = None, gen_fn: Callable | None = None,
        cfg: dict | None = None, history: list[dict] | None = None, route: str | None = None,
        cloud_fn: Callable | None = None, online_fn: Callable | None = None, steps: bool | None = None) -> Iterator[dict]:
    """Checks the switch first (raises AskDisabled before anything streams), then returns the event generator.
    `agent_safe`: identifiers are masked in the passages and the history before the model sees them, in the sources,
    in the answer and in search_query.
    `history`: the last turns [{"q", "a"}], oldest first, at most MAX_HISTORY (validate_history: ValueError otherwise).
    `route`: this question's "auto" | "cloud" | "local" instead of the configured one (ValueError otherwise).
    `search_fn(q, collections, filters, limit)`, `chunks_fn(result)`, `gen_fn(messages, **params)` (the local model),
    `cloud_fn(messages, **params)` (the cloud model, instead of the active provider) and `online_fn()` are injectable
    (tests, benchmarks); the defaults are the in-process search, the read-only index, the ask worker, the active
    provider with its Keychain key and the cached reachability check. An injected gen_fn also writes the follow-up
    rewrite when the answer is local (it counts as loaded).
    `steps`: also yield {"type": "step", "kind": "rewrite" | "search" | "read" | "model", "text", "count"?, "ms"?}
    events as the work happens (the app's transcript: Searching …, Found 12 results, Reading 6 passages from 4
    documents, Asking <model>). Default off, so direct callers and MCP get the plain stream; the HTTP API switches
    them on for the call with `with step_events():` (a context variable, so the call's signature stays the same)."""
    s = settings(cfg)
    if not s["enabled"]:
        raise AskDisabled("ask_disabled")
    if not (q or "").strip():
        raise ValueError("empty question")
    if route is not None and route not in config.ASK_ROUTES:
        raise ValueError(f"route must be one of: {', '.join(config.ASK_ROUTES)}")
    turns = validate_history(history)
    return _run(q.strip(), collections, filters, agent_safe, s, search_fn, chunks_fn, gen_fn, turns,
                route=route, cloud_fn=cloud_fn, online_fn=online_fn, cfg=cfg,
                steps=_STEPS.get() if steps is None else steps)


# ------------------------------------------------------------------ cloud or local

def cloud_allowed(collections: list[str] | None, cfg: dict | None = None) -> tuple[bool, str | None]:
    """May passages from this scope go to a cloud model? No when any folder in it has "ask_cloud": false
    -> (allowed, the first folder that keeps its questions local)."""
    full = cfg if cfg is not None and "collections" in cfg else config.load()
    for name in config.resolve_scope(full, collections, strict=False)["collections"]:
        c = config.get_collection(full, name)
        if c is not None and c.get("ask_cloud", True) is False:
            return False, name
    return True, None


def route_mode(s: dict, route: str | None = None) -> tuple[str, bool]:
    """(mode, fallback): "cloud" | "local", and may the local model answer when the cloud cannot. A question's own
    route wins: "local" -> local; "cloud" -> cloud, never falling back; "auto" -> cloud with fallback. Otherwise the
    configured route: "local"; "auto" (old name) -> cloud with fallback; "cloud" -> cloud, fallback = local_fallback."""
    if route in ("local", "cloud", "auto"):
        return ("local", False) if route == "local" else ("cloud", route == "auto")
    configured = s.get("route") or "cloud"
    if configured == "local":
        return "local", False
    return "cloud", configured == "auto" or bool(s.get("local_fallback"))


def no_cloud_message(pid: str, why: str) -> str:
    return (f"No cloud model is reachable ({pid}: {why}). Switch this chat to Local to answer on this Mac, "
            "or choose a model: mse providers use <id> <model> (Settings › Ask has the model menu).")


def plan_route(s: dict, route: str | None, collections: list[str] | None, cfg: dict | None = None,
               cloud_fn: Callable | None = None, online_fn: Callable | None = None) -> dict:
    """Where this question is answered, decided before the search -> {"where": "cloud" | "local" | "error",
    "provider", "model", "fallback": may a cloud failure before the first token fall back to local,
    "reason"?: why the local model answers although the route allowed the cloud, "message"? (where "error"),
    "gen"?, "base_url"? (where "cloud")}. Never loads the local model and never calls a model."""
    from . import providers
    mode, fallback = route_mode(s, route)
    base = {"provider": s["provider"], "model": s["model"], "fallback": fallback}
    if mode == "local":
        return {**base, "where": "local", "reason": None}
    ok, _ = cloud_allowed(collections, cfg)
    if not ok:                                       # the folder's rule beats the route (an explicit choice of the user)
        return {**base, "where": "local", "reason": "ask_cloud_off"}
    if cloud_fn is not None:
        target, why = {"gen": cloud_fn, "base_url": None, "model": s["model"]}, None
    else:
        target, why = providers.target(s)
    if why:
        if fallback:
            return {**base, "where": "local", "reason": why}
        return {**base, "where": "error", "message": no_cloud_message(s["provider"], providers.why_message(why, s["provider"]))}
    if fallback:
        up = online_fn() if online_fn is not None else providers.online(target["base_url"])
        if not up:
            return {**base, "where": "local", "reason": "offline"}
    return {**base, "where": "cloud", "gen": target["gen"], "base_url": target["base_url"], "model": target["model"]}


def _mask_turns(turns: list[dict]) -> tuple[list[dict], int]:
    from .redact import redact_count
    out, n = [], 0
    for t in turns:
        q, a = redact_count(t.get("q") or ""), redact_count(t.get("a") or "")
        out.append({**t, "q": q[0], "a": a[0]})
        n += q[1] + a[1]
    return out, n


def _mask_passages(passages: list[dict]) -> tuple[list[dict], int]:
    from .redact import redact_count
    out, n = [], 0
    for p in passages:
        text, header = redact_count(p["text"]), redact_count(p["header"])
        out.append({**p, "text": text[0], "header": header[0]})
        n += text[1] + header[1]
    return out, n


def one_collection(results: list[dict], collections: list[str] | None) -> list[dict]:
    """Collections never mix in one prompt unless the scope is explicitly "all": otherwise only the results from
    the collection of the best result are read (e.g. documents from two folders stay apart)."""
    if not results or (collections and "all" in collections):
        return results
    top = results[0]["collection"]
    return [r for r in results if r["collection"] == top]


def _default_search(q, collections, filters, limit):
    from . import search
    return search.search(q, collections=collections, filters=filters, limit=limit, opts={"rerank": True})


def _default_gen(messages, **params):
    from .embedder import pool
    return pool().ask_stream(messages, **params)


def _model_warm() -> bool:
    """The Ask model is loaded and idle: a follow-up rewrite costs a short generation, never a load."""
    try:
        from .embedder import pool
        w = pool().workers.get("ask")
        return bool(w and w.alive and not w.lock.locked())
    except Exception:
        return False


def _passages_cloud_ok(passages: list[dict], cfg: dict | None) -> bool:
    full = cfg if cfg is not None and "collections" in cfg else config.load()
    for name in {p["collection"] for p in passages}:
        c = config.get_collection(full, name)
        if c is not None and c.get("ask_cloud", True) is False:
            return False
    return True


def _step(kind: str, text: str, **extra) -> dict:
    return {"type": "step", "kind": kind, "text": text, **{k: v for k, v in extra.items() if v is not None}}


def _scope_text(collections, filters, cfg) -> str:
    """"work › Invoices": the scope's label and the subfolder(s) the question is limited to."""
    full = cfg if cfg is not None and "collections" in cfg else config.load()
    label = config.resolve_scope(full, collections, strict=False)["label"]
    crumbs = []
    for p in (filters or {}).get("path") or []:
        parts = [x for x in str(p).strip("/").split("/") if x]
        if parts:
            crumbs.append(" › ".join(parts))
    for g in (filters or {}).get("_under") or []:
        crumbs.append(", ".join(" › ".join(x for x in str(p).strip("/").split("/") if x) for p in g))
    for p in (filters or {}).get("in") or []:
        if str(p).strip().startswith(("~", "/")):
            crumbs.append(" › ".join(x for x in str(p).replace("~", "Home").strip("/").split("/") if x))
    return " › ".join([label] + ([", ".join(crumbs)] if crumbs else []))


def _model_text(who: dict) -> str:
    if who.get("where") == "local":
        return f"Asking the local model {who.get('model')}"
    model = str(who.get("model") or "")
    return f"Asking {model if '/' in model else (str(who.get('provider')) + '/' + model)}"


def _run(q, collections, filters, agent_safe, s, search_fn, chunks_fn, gen_fn, history=(), route=None,
         cloud_fn=None, online_fn=None, cfg=None, steps=False) -> Iterator[dict]:
    from . import providers
    from .cloud import CloudError
    from .redact import redact, redact_count
    t0 = time.time()
    timings: dict = {}
    gen_fn = gen_fn or _default_gen
    masked = 0                          # identifiers masked in what the model reads (agent_safe; cloud_redact below)
    if agent_safe:
        history, masked = _mask_turns(list(history))
    else:
        history = list(history)
    plan = plan_route(s, route, collections, cfg, cloud_fn, online_fn)    # cloud or local, before anything loads
    if plan["where"] == "error":
        yield {"type": "error", "message": plan["message"]}
        return
    cloud = plan["where"] == "cloud"
    default = gen_fn is _default_gen
    warm = not default or (bool(history) and _model_warm())    # checked before _prepare could take the lock
    if default and not warm and not cloud:   # load the local model while the search runs (no-op when loaded),
        threading.Thread(target=_prepare, daemon=True, name="ask-prepare").start()    # never for a cloud answer
    if cloud:                           # a follow-up is rewritten by the cloud model; it sees the history masked
        seen = _mask_turns(history)[0] if s.get("cloud_redact", True) else history
        sq = search_query(q, history, s, plan["gen"], model_history=seen)
    else:
        sq = search_query(q, history, s, gen_fn if warm else None)  # a follow-up is searched for what it means
    if "rewrite_ms" in sq:
        timings["rewrite_ms"] = sq["rewrite_ms"]
    query = sq["query"]
    shown = (lambda t: redact(t)) if agent_safe else (lambda t: t)       # the transcript is masked like the sources
    if steps and sq["rewrite"] != "none":
        yield _step("rewrite", shown(f"Reading the follow-up as: {query}"), ms=sq.get("rewrite_ms"))
    turns = compact_history(history)
    if turns:                           # the conversation is trimmed first; the passages get what is left
        turns = fit_history(turns, min(HISTORY_TOKENS, int(s["context_tokens"]) // 4), sq.get("meaning"))
    extra = {"search_query": redact(query) if agent_safe else query, "rewrite": sq["rewrite"],
             **({"rewrite_fallback": sq["fallback"]} if sq.get("fallback") else {}),
             **({"history_used": len(turns)} if history else {})}
    t_search = time.time()
    if steps:
        yield _step("search", shown(f"Searching {_scope_text(collections, filters, cfg)} for “{query}”"))
    try:
        res = (search_fn or _default_search)(query, collections, filters, int(s["results"]))
    except Exception as e:
        yield {"type": "error", "message": f"search failed: {type(e).__name__}: {e}"}
        return
    timings["search_ms"] = round((time.time() - t_search) * 1000)
    if steps:
        n_found = len(res.get("results") or [])
        yield _step("search", f"Found {n_found} result{'' if n_found == 1 else 's'}", count=n_found,
                    ms=timings["search_ms"])
    bridge = [t for v in (res.get("bridge") or {}).values() for t in v]
    budget = int(s["context_tokens"]) - history_cost(turns, sq.get("meaning"))
    passages = build_passages(query, one_collection(res.get("results") or [], collections), chunks_fn or db_chunks,
                              int(s["passages"]), budget, float(s["min_relevance"]), bridge)
    if agent_safe:
        for p in passages:
            (p["text"], a), (p["header"], b) = redact_count(p["text"]), redact_count(p["header"])
            masked += a + b
            p["title"], p["path"] = redact(p["title"]), redact(p["path"])
    reason = plan.get("reason")
    if cloud and not _passages_cloud_ok(passages, cfg):     # every passage's folder must allow the cloud, too
        cloud, reason = False, "ask_cloud_off"
    lang = language(q, history)
    if steps and passages:
        docs = len({(p["collection"], p["path"]) for p in passages})
        yield _step("read", f"Reading {len(passages)} passage{'' if len(passages) == 1 else 's'} from {docs} "
                            f"document{'' if docs == 1 else 's'}", count=len(passages))
    yield {"type": "sources", "sources": [
        {"n": p["n"], "docid": p["docid"], "collection": p["collection"], "path": p["path"], "title": p["title"],
         "page": p["page"], "ts": p["ts"],
         "snippet": html.unescape(re.sub(r"</?mark>", "", snippet(p["text"], query_terms(query), SNIPPET_CHARS)))}
        for p in passages]}
    local_who = {"model": model_name(s), "provider": "local", "where": "local"}
    cloud_who = {"model": plan["model"], "provider": plan["provider"], "where": "cloud",
                 **({"training": True} if config.trains_on_inputs(plan["model"], (s.get("providers") or {}).get(plan["provider"]))
                    else {})}                      # the provider may use inputs for training
    if not passages:                    # nothing to read: say so without calling or waking a model
        text = NOT_FOUND[lang]
        who = cloud_who if cloud else local_who
        timings.update(ttft_ms=round((time.time() - t0) * 1000), total_ms=round((time.time() - t0) * 1000))
        yield {"type": "token", "text": text}
        yield {"type": "done", "citations": [], **who,
               **({"fallback_reason": reason} if who["where"] == "local" and reason else {}), "redacted": masked,
               "timings": timings, "answer": text, "answered": False, **extra}
        return
    valid = {p["n"] for p in passages}
    attempts = (["cloud", "local"] if plan["fallback"] else ["cloud"]) if cloud else ["local"]
    for i, where in enumerate(attempts):
        if where == "cloud":
            ps, ts, sent_masked = passages, turns, masked
            if s.get("cloud_redact", True):            # identifiers stay on this Mac
                (ps, n1), (ts, n2) = _mask_passages(passages), _mask_turns(turns)
                sent_masked += n1 + n2
            messages, gen, who = build_messages(q, ps, lang, ts, sq.get("meaning")), plan["gen"], cloud_who
        else:
            messages, gen, who, sent_masked = build_messages(q, passages, lang, turns, sq.get("meaning")), gen_fn, \
                local_who, masked
        stream = CitationStream(valid)
        first, final, sent, g = None, {}, False, None
        if steps:
            yield _step("model", _model_text(who))
        try:
            g = gen(messages, max_tokens=int(s["max_tokens"]), temperature=float(s["temperature"]),
                    top_p=float(s["top_p"]))
            for msg in g:
                if msg.get("done"):
                    final = msg
                    break
                out = stream.feed(msg.get("token") or "")
                if out:
                    first, sent = first or time.time(), True
                    yield {"type": "token", "text": out}
            tail = stream.flush()
            if tail:
                first, sent = first or time.time(), True
                yield {"type": "token", "text": tail}
            if where == "cloud" and not sent:
                raise CloudError(f"no answer text (stop reason: {final.get('stop_reason') or 'none'})")
        except Exception as e:
            if where == "cloud":
                providers.note_failure(plan["provider"], plan.get("base_url"), e)
            if not sent and i + 1 < len(attempts):   # nothing shown yet: the local model answers instead
                reason = f"error: {e}"[:200]
                continue
            if where == "cloud" and not sent:
                yield {"type": "error", "message": no_cloud_message(plan["provider"], f"{type(e).__name__}: {e}")}
            else:
                yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
            return
        finally:
            close = getattr(g, "close", None)
            if close:
                close()                         # cancels a generation still running (client gone)
        if where == "cloud":
            providers.note_success(plan["provider"], plan.get("base_url"))
        break
    answer = stream.text()
    if agent_safe:
        answer = redact(answer)
    timings.update(ttft_ms=round(((first or time.time()) - t0) * 1000), total_ms=round((time.time() - t0) * 1000))
    for k in ("model_ttft_ms", "load_ms", "prompt_tokens", "tokens", "tok_s"):
        if final.get(k) is not None:
            timings[k] = final[k]
    yield {"type": "done", "citations": [n for n in stream.cited if f"[{n}]" in answer], **who,
           **({"fallback_reason": reason} if who["where"] == "local" and reason else {}), "redacted": sent_masked,
           "timings": timings, "answer": answer, "answered": not is_not_found(answer, stream.cited),
           "dropped_citations": stream.dropped, **extra}


def _prepare():
    try:
        from .embedder import pool
        pool().ask_prepare()
    except Exception:
        pass        # the real request reports the error
