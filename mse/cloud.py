"""Cloud models for Ask: one streaming call to the Claude API (Messages), or to an OpenAI-compatible endpoint, either
Chat Completions (DeepSeek, OpenRouter, LM Studio, Ollama ...) or Responses (a local router, ...).

Standard library only: the daemon loads no SDK and no ML code. Every stream yields the same messages as the local
worker does, {"token": "..."} many times and then one {"done": True, "model_ttft_ms", "prompt_tokens", "tokens",
"tok_s", "stop_reason"}, so ask.py treats local and cloud answers alike. Failures raise CloudError; `network=True`
means the host could not be reached (no connection, DNS, timeout), which ask.py treats as being offline.

Safety:
  - the key goes only in a header (x-api-key for Claude, a bearer token otherwise), never in a URL; every error
    message is scrubbed of it before it leaves this module
  - redirects are refused, so a key is never replayed to another host
  - a key is never sent over plain http except to this Mac (providers.can_send_key decides before a call)
  - loopback endpoints (a local router, LM Studio, Ollama) bypass any HTTP proxy from the environment
"""

import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterable, Iterator
from urllib.parse import urlsplit

from . import __version__

ANTHROPIC_VERSION = "2023-06-01"
FALLBACK_BETA = "server-side-fallback-2026-07-01"        # with "fallbacks": "default" (Claude API only)
# models that take the server-side refusal fallback ("default" form): a classifier decline is retried on the model
# Anthropic recommends for that category instead of ending the answer
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
ERROR_BODY_BYTES = 65536


class CloudError(RuntimeError):
    """A cloud call failed. status: the HTTP status when there was one; network: the host was not reachable."""

    def __init__(self, message: str, status: int | None = None, network: bool = False):
        super().__init__(message)
        self.status = status
        self.network = network


# ------------------------------------------------------------------ helpers

def scrub(text: str, secrets: Iterable[str | None] = ()) -> str:
    """`text` without any of `secrets`."""
    out = str(text or "")
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "[key]")
    return out


def is_loopback(host: str | None) -> bool:
    h = (host or "").strip("[]").lower()
    return h in ("localhost", "127.0.0.1", "::1") or h.endswith(".localhost") or h.startswith("127.")


def supports_effort(model: str) -> bool:
    """Claude models that take output_config.effort: every 5.x model, Opus 4.5-4.8 and Sonnet 4.6."""
    m = re.match(r"^claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d+))?", model or "")
    if not m:
        return False
    family, major, minor = m.group(1), int(m.group(2)), int(m.group(3) or 0)
    if major >= 5:
        return True
    return major == 4 and ((family == "opus" and 5 <= minor <= 8) or (family == "sonnet" and minor == 6))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                                   # urllib then raises HTTPError(3xx): the key stays here


def _error_text(e: urllib.error.HTTPError) -> str:
    try:
        raw = e.read(ERROR_BODY_BYTES) or b""
    except Exception:
        raw = b""
    text = raw.decode("utf-8", "replace").strip()
    try:
        d = json.loads(text)
    except ValueError:
        return re.sub(r"\s+", " ", text)[:200] or (e.reason if isinstance(e.reason, str) else "")
    err = d.get("error") if isinstance(d, dict) else None
    if isinstance(err, dict):
        return " ".join(str(x) for x in (err.get("type"), err.get("message")) if x)[:300]
    if isinstance(err, str):
        return err[:300]
    if isinstance(d, dict) and d.get("message"):
        return str(d["message"])[:300]
    return text[:200]


def _open(url: str, body: dict | None, headers: dict, timeout: float, secrets=()):
    """POST `body` (GET when None) -> the open response. CloudError on any failure, scrubbed of `secrets`."""
    host = urlsplit(url).hostname
    handlers: list = [_NoRedirect()]
    if is_loopback(host):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"User-Agent": f"MySearch/{__version__}", **headers})
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        what = "redirect refused" if 300 <= e.code < 400 else _error_text(e)
        raise CloudError(scrub(f"HTTP {e.code}: {what}".strip(), secrets), status=e.code) from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise CloudError(scrub(f"cannot reach {host}: {reason}", secrets), network=True) from None


# ------------------------------------------------------------------ server-sent events

def sse(lines: Iterable) -> Iterator[tuple[str | None, str]]:
    """Server-sent events from an iterable of lines (bytes or str) -> (event name or None, data) per event.
    Comment lines (": keep-alive") are skipped; several data lines join with a newline."""
    event, data = None, []
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        line = line.rstrip("\r\n")
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = None, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    if data:
        yield event, "\n".join(data)


def _json(data: str) -> dict | None:
    try:
        d = json.loads(data)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def anthropic_events(events: Iterable[tuple[str | None, str]]) -> Iterator[dict]:
    """Claude Messages stream -> tokens (text deltas only: thinking, signatures and fallback blocks are skipped),
    then done. A refusal or an error event raises CloudError."""
    usage: dict = {}
    stop = None
    finished = False
    for name, data in events:
        d = _json(data)
        if d is None:
            continue
        t = d.get("type") or name
        if t == "message_start":
            usage.update((d.get("message") or {}).get("usage") or {})
        elif t == "content_block_delta":
            delta = d.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                yield {"token": delta["text"]}
        elif t == "message_delta":
            stop = (d.get("delta") or {}).get("stop_reason") or stop
            usage.update(d.get("usage") or {})
        elif t == "error":
            e = d.get("error") or {}
            raise CloudError(" ".join(str(x) for x in (e.get("type"), e.get("message")) if x) or "stream error",
                             status=529 if e.get("type") == "overloaded_error" else None)
        elif t == "message_stop":
            finished = True
            break
    if stop == "refusal":
        raise CloudError("the model declined to answer (refusal)")
    if not finished and stop is None:
        raise CloudError("the stream ended before the answer was complete", network=True)
    yield {"done": True, "stop_reason": stop, "prompt_tokens": usage.get("input_tokens"),
           "tokens": usage.get("output_tokens")}


def chat_events(events: Iterable[tuple[str | None, str]]) -> Iterator[dict]:
    """OpenAI Chat Completions stream -> tokens (delta.content; reasoning_content is skipped), then done."""
    usage: dict = {}
    finish, finished = None, False
    for _, data in events:
        if data.strip() == "[DONE]":
            finished = True
            break
        d = _json(data)
        if d is None:
            continue
        if d.get("error"):
            e = d["error"]
            raise CloudError(str(e.get("message") if isinstance(e, dict) else e)[:300] or "stream error")
        for ch in d.get("choices") or []:
            c = (ch.get("delta") or {}).get("content")
            if isinstance(c, str) and c:
                yield {"token": c}
            finish = ch.get("finish_reason") or finish
        if isinstance(d.get("usage"), dict):
            usage = d["usage"]
    if not finished and finish is None:
        raise CloudError("the stream ended before the answer was complete", network=True)
    yield {"done": True, "stop_reason": finish, "prompt_tokens": usage.get("prompt_tokens"),
           "tokens": usage.get("completion_tokens")}


def responses_events(events: Iterable[tuple[str | None, str]]) -> Iterator[dict]:
    """OpenAI Responses stream (a local router speaks it) -> tokens (response.output_text.delta), then done.
    response.failed and error events raise; response.incomplete ends the answer where it stopped."""
    for name, data in events:
        d = _json(data)
        if d is None:
            continue                                   # "[DONE]" and other non-JSON lines
        t = d.get("type") or name
        if t == "response.output_text.delta":
            if isinstance(d.get("delta"), str) and d["delta"]:
                yield {"token": d["delta"]}
        elif t in ("response.completed", "response.incomplete"):
            r = d.get("response") or {}
            usage = r.get("usage") or {}
            stop = "completed" if t == "response.completed" else \
                f"incomplete: {(r.get('incomplete_details') or {}).get('reason') or 'unknown'}"
            yield {"done": True, "stop_reason": stop, "prompt_tokens": usage.get("input_tokens"),
                   "tokens": usage.get("output_tokens")}
            return
        elif t in ("response.failed", "error"):
            err = (d.get("response") or {}).get("error") or d.get("error") or d
            msg = err.get("message") if isinstance(err, dict) else err
            code = err.get("code") if isinstance(err, dict) else None
            raise CloudError(" ".join(str(x) for x in (code, msg) if x)[:300] or "the response failed")
    raise CloudError("the stream ended before the response was complete", network=True)


PARSERS: dict[str, Callable] = {"anthropic": anthropic_events, "openai-chat": chat_events,
                                "openai-responses": responses_events}


# ------------------------------------------------------------------ requests

def build_request(p: dict, key: str | None, model: str, messages: list[dict], max_tokens: int,
                  temperature: float | None = None, effort: str | None = None) -> tuple[str, dict, dict]:
    """(url, body, headers) for one streaming answer. `messages`: [{"role": "system" | "user" | "assistant", ...}]."""
    base = p["base_url"].rstrip("/")
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] in ("user", "assistant")]
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if p["kind"] == "anthropic":
        # no temperature / top_p: current Claude models reject sampling parameters; thinking stays adaptive (Opus 5.5
        # cannot turn it off) and `effort` keeps it short
        body: dict = {"model": model, "max_tokens": int(max_tokens), "stream": True, "messages": turns}
        if system:
            body["system"] = system
        if effort and supports_effort(model):
            body["output_config"] = {"effort": effort}
        if model in FALLBACK_MODELS and urlsplit(base).hostname == "api.anthropic.com":
            body["fallbacks"] = "default"
            headers["anthropic-beta"] = FALLBACK_BETA
        headers["anthropic-version"] = ANTHROPIC_VERSION
        if key:
            headers["x-api-key"] = key
        return base + "/messages", body, headers
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if p["kind"] == "openai-chat":
        body = {"model": model, "stream": True, "max_tokens": int(max_tokens),
                "messages": ([{"role": "system", "content": system}] if system else []) + turns}
        if temperature is not None:
            body["temperature"] = float(temperature)
        return base + "/chat/completions", body, headers
    if p["kind"] == "openai-responses":
        body = {"model": model, "stream": True, "store": False, "max_output_tokens": int(max_tokens), "input": turns}
        if system:
            body["instructions"] = system
        return base + "/responses", body, headers
    raise CloudError(f"unknown provider kind {p['kind']!r}")


def stream(p: dict, key: str | None, model: str, messages: list[dict], max_tokens: int = 4096,
           temperature: float | None = None, effort: str | None = None, timeout: float = 60) -> Iterator[dict]:
    """One streaming answer from provider `p` -> {"token"} ... {"done"}. CloudError (scrubbed of the key) on failure.
    Closing the generator early closes the connection, which ends the generation upstream."""
    url, body, headers = build_request(p, key, model, messages, max_tokens, temperature, effort)
    parse = PARSERS[p["kind"]]
    t0 = time.time()
    try:
        resp = _open(url, body, headers, timeout, secrets=[key])
    except CloudError as e:
        if not (e.status == 400 and "fallbacks" in body and "fallback" in str(e).lower()):
            raise
        # the refusal fallback is a beta: should this account or model reject it, ask once more without it
        body = {k: v for k, v in body.items() if k != "fallbacks"}
        headers = {k: v for k, v in headers.items() if k != "anthropic-beta"}
        resp = _open(url, body, headers, timeout, secrets=[key])
    first, n = None, 0
    try:
        for msg in parse(sse(resp)):
            if "token" in msg:
                if first is None:
                    first = time.time()
                n += 1
                yield msg
            elif msg.get("done"):
                t1 = time.time()
                tokens = msg.get("tokens") or n
                gen_s = t1 - (first or t1)
                yield {"done": True, "stop_reason": msg.get("stop_reason"), "prompt_tokens": msg.get("prompt_tokens"),
                       "tokens": tokens, "model_ttft_ms": round(((first or t1) - t0) * 1000),
                       "tok_s": round(tokens / gen_s, 1) if gen_s > 0.05 and tokens else None}
                return
    except CloudError as e:
        raise CloudError(scrub(str(e), [key]), status=e.status, network=e.network) from None
    except (socket.timeout, TimeoutError) as e:
        raise CloudError(f"no data from {urlsplit(url).hostname} for {timeout:.0f} s", network=True) from e
    except (OSError, http.client.HTTPException) as e:
        raise CloudError(scrub(f"connection to {urlsplit(url).hostname} failed: {type(e).__name__}", [key]),
                         network=True) from None
    finally:
        try:
            resp.close()
        except Exception:
            pass


def generator(p: dict, key: str | None, model: str, s: dict) -> Callable:
    """A gen_fn for ask.py, same call shape as the local worker's: gen(messages, max_tokens=, temperature=, top_p=).
    max_tokens is raised to cloud_max_tokens (thinking models spend tokens before the answer; the prompt keeps the
    answer short), and top_p is not sent."""
    def gen(messages, max_tokens=300, temperature=None, top_p=None):
        return stream(p, key, model, messages, max_tokens=max(int(s.get("cloud_max_tokens") or 4096), int(max_tokens)),
                      temperature=temperature, effort=s.get("cloud_effort"),
                      timeout=float(s.get("cloud_timeout_s") or 60))
    return gen


MODEL_PAGES = 50                       # Anthropic's list is paged; a safety stop, not a limit anyone reaches


def _get_json(url: str, headers: dict, timeout: float, key: str | None) -> dict:
    resp = _open(url, None, headers, timeout, secrets=[key])
    try:
        raw = resp.read(8 * 1024 * 1024)
    except OSError as e:
        raise CloudError(f"reading the model list failed: {type(e).__name__}", network=True) from None
    finally:
        resp.close()
    d = _json(raw.decode("utf-8", "replace"))
    if d is None:
        raise CloudError("the models endpoint did not return JSON")
    return d


def _model_items(d: dict) -> list:
    return d.get("data") if isinstance(d.get("data"), list) else d.get("models") if isinstance(d.get("models"), list) \
        else []


def list_models_detail(p: dict, key: str | None, timeout: float = 15) -> list[dict]:
    """Every model the provider's models endpoint lists, in its order -> [{"id", "name"?, "owned_by"?, "created"?}].
    Claude API: GET /v1/models, followed page by page (`has_more`, `after_id` = the page's `last_id`) until the end.
    OpenAI-compatible endpoints (some answer {"data": [{id, object, owned_by}], "models": [...]}) are
    one page. Never invents an id: an endpoint that lists nothing is an error, not an empty list."""
    base = p["base_url"].rstrip("/")
    headers = {"Accept": "application/json"}
    items: list = []
    if p["kind"] == "anthropic":
        headers["anthropic-version"] = ANTHROPIC_VERSION
        if key:
            headers["x-api-key"] = key
        after = None
        for _ in range(MODEL_PAGES):
            url = base + "/models?limit=1000" + (f"&after_id={urllib.parse.quote(after, safe='')}" if after else "")
            d = _get_json(url, headers, timeout, key)
            page = _model_items(d)
            items += page
            last = d.get("last_id") or (page[-1].get("id") if page and isinstance(page[-1], dict) else None)
            if not d.get("has_more") or not last or last == after:
                break
            after = last
    else:
        if key:
            headers["Authorization"] = f"Bearer {key}"
        items = _model_items(_get_json(base + "/models", headers, timeout, key))
    out, seen = [], set()
    for it in items:
        if isinstance(it, dict):
            mid = it.get("id") or it.get("slug") or it.get("name")
            name = it.get("display_name") or it.get("displayName")
            owner = it.get("owned_by")
            created = it.get("created_at") if isinstance(it.get("created_at"), str) else None
        else:
            mid, name, owner, created = it, None, None, None
        if not isinstance(mid, str) or not mid.strip() or mid.strip() in seen:
            continue
        mid = mid.strip()
        seen.add(mid)
        row: dict = {"id": mid}
        if isinstance(name, str) and name.strip() and name.strip() != mid:
            row["name"] = name.strip()[:80]
        if isinstance(owner, str) and owner.strip():
            row["owned_by"] = owner.strip()[:40]
        if created:
            row["created"] = created[:10]
        out.append(row)
    if not out:
        raise CloudError("the provider's models endpoint listed no models")
    return out


def list_models(p: dict, key: str | None, timeout: float = 15) -> list[str]:
    """The provider's model ids (list_models_detail without the details)."""
    return [m["id"] for m in list_models_detail(p, key, timeout)]
