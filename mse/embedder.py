"""Client side of the model workers (embedding, speech, re-ranking, Ask): starts each on demand, stops it when idle."""

import base64
import ctypes
import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from . import config, paths

DIMS = 768
QUERY = "task: search result | query: "


def footprint_mb(pid: int, peak: bool = False) -> float | None:
    """Physical memory of a process as Activity Monitor counts it (phys_footprint). `ps` RSS misses the Metal
    buffers MLX keeps model weights in (a loaded 4B model shows ~0.4 GB RSS but ~2.5 GB footprint)."""
    try:
        buf = (ctypes.c_uint64 * 48)()          # rusage_info_v4: 16-byte uuid, then uint64 fields
        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
        if lib.proc_pid_rusage(int(pid), 4, ctypes.byref(buf)) != 0:
            return None
        v = buf[2 + 28] if peak else buf[2 + 7]  # ri_lifetime_max_phys_footprint / ri_phys_footprint
        return round(v / 2**20, 1)
    except Exception:
        return None


def doc_prompt(title: str | None, text: str) -> str:
    t = (title or "none").replace("|", " ").strip()[:200] or "none"
    return f"title: {t} | text: {text}"


class Worker:
    def __init__(self, mode: str):
        self.mode = mode
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.last_used = 0.0
        self.started_at = 0.0
        self._seq = 0
        self.batch_priority = False
        self.info: dict = {}         # what the worker reported when it started (reranker: its LoRA adapter)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _start(self):
        cfg = config.load()
        if self.mode == "ask":
            from . import ask
            s = ask.settings(cfg)
            if not s["enabled"]:
                raise ask.AskDisabled("ask_disabled")       # switched off: never load the model
            if not ask.available(s):
                raise RuntimeError(f"ask model not installed: {s['local_model']}")
        paths.LOGS.mkdir(parents=True, exist_ok=True)
        log = open(paths.LOGS / "worker.log", "ab")
        env = dict(os.environ, HF_HUB_OFFLINE="1", PYTHONUNBUFFERED="1")
        model = cfg["model"]["path"]
        text_model = cfg["model"].get("text_path")
        if self.mode == "text" and text_model and os.path.isdir(text_model):
            model = text_model      # text encoder only: a quarter of the weights, identical vectors
        cmd = [sys.executable, "-m", "mse.worker", "--mode", self.mode, "--model", model]
        if self.mode == "rerank":
            rr = cfg["model"].get("reranker")
            if not (rr and os.path.isdir(rr)):
                raise RuntimeError("reranker model not installed")
            cmd = [sys.executable, "-m", "mse.worker", "--mode", "rerank", "--model", rr]
        if self.mode == "asr":
            asr = cfg["model"].get("asr") or {}
            if not (asr.get("path") and os.path.isdir(asr["path"])):
                raise RuntimeError("speech model not installed")
            cmd = [sys.executable, "-m", "mse.worker", "--mode", "asr", "--model", asr["path"],
                   "--processor", asr["processor"]]
        if self.mode == "ask":
            cmd = [sys.executable, "-m", "mse.worker", "--mode", "ask", "--model",
                   str(Path(s["local_model"]).expanduser())]      # the local model; s["model"] is the cloud one
        if self.batch_priority:
            cmd = ["/usr/bin/nice", "-n", "10"] + cmd
        t = time.time()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                     cwd=str(paths.PROJECT), env=env)
        ready = self._readline(timeout=180)
        if not ready or not ready.get("ready"):
            self.stop()
            raise RuntimeError(f"{self.mode} worker failed to start — see ~/Library/Logs/MySearch/worker.log")
        self.info = {k: ready[k] for k in ("adapter", "adapter_error") if k in ready}
        self.started_at = time.time()
        self.load_ms = round((self.started_at - t) * 1000)

    def _readline(self, timeout: float) -> dict | None:
        assert self.proc and self.proc.stdout
        r, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not r:
            return None
        line = self.proc.stdout.readline()
        return json.loads(line) if line else None

    def call(self, payload: dict, timeout: float = 600) -> dict:
        with self.lock:
            if not self.alive:
                self._start()
            self._seq += 1
            payload = dict(payload, id=self._seq)
            self.proc.stdin.write((json.dumps(payload) + "\n").encode())
            self.proc.stdin.flush()
            reply = self._readline(timeout)
            self.last_used = time.time()
            if reply is None:
                self.stop()
                raise RuntimeError("embedding worker timed out")
            if "error" in reply:
                raise RuntimeError(reply["error"])
            return reply

    def stream(self, payload: dict, first_timeout: float = 300, timeout: float = 60):
        """Send one request and yield every reply line for it until the one carrying "done".
        The worker lock is held throughout. If the consumer stops early (client gone), the worker is told to
        cancel and the rest of its output is drained, so the next request starts clean."""
        with self.lock:
            fresh = not self.alive
            if fresh:
                self._start()
            self._seq += 1
            rid = self._seq
            self.proc.stdin.write((json.dumps(dict(payload, id=rid)) + "\n").encode())
            self.proc.stdin.flush()
            reader = _Lines(self.proc.stdout.fileno())
            done = False
            try:
                wait = first_timeout
                while True:
                    msg = reader.next(wait)
                    if msg is None:
                        raise RuntimeError(f"{self.mode} worker timed out")
                    if msg.get("id") != rid:
                        continue
                    if "error" in msg:
                        done = True
                        raise RuntimeError(msg["error"])
                    if msg.get("done"):
                        done = True
                        if fresh:
                            msg["load_ms"] = getattr(self, "load_ms", None)
                        yield msg
                        return
                    yield msg
                    wait = timeout
            except GeneratorExit:
                if not done:
                    self._cancel(reader, rid)
                raise
            except Exception:
                if not done:
                    self.stop()
                raise
            finally:
                self.last_used = time.time()

    def _cancel(self, reader: "_Lines", rid: int):
        try:
            self.proc.stdin.write(b'{"op": "cancel"}\n')
            self.proc.stdin.flush()
            end = time.time() + 15
            while time.time() < end:
                msg = reader.next(max(0.1, end - time.time()))
                if msg is None:
                    break
                if msg.get("id") == rid and (msg.get("done") or "error" in msg):
                    return
        except Exception:
            pass
        self.stop()                                      # could not resynchronise: start fresh next time

    def stop(self, wait: float = 5):
        """Ask the worker to quit and wait `wait` seconds; a worker still busy after that is killed."""
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.stdin.write(b'{"op": "quit"}\n')
                p.stdin.flush()
                p.wait(timeout=wait)
            except Exception:
                p.kill()

    def rss_mb(self) -> float | None:
        if not self.alive:
            return None
        try:
            out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(self.proc.pid)])
            return round(int(out) / 1024, 1)
        except Exception:
            return None

    def footprint_mb(self) -> float | None:
        return footprint_mb(self.proc.pid) if self.alive else None


class _Lines:
    """JSON lines from a pipe, read straight from the file descriptor: a buffered reader would hide lines that
    already arrived from select(), which matters when a worker streams many short lines."""

    def __init__(self, fd: int):
        self.fd = fd
        self.buf = b""

    def next(self, timeout: float) -> dict | None:
        end = time.time() + timeout
        while b"\n" not in self.buf:
            left = end - time.time()
            if left <= 0:
                return None
            r, _, _ = select.select([self.fd], [], [], left)
            if not r:
                return None
            chunk = os.read(self.fd, 65536)
            if not chunk:
                raise RuntimeError("worker exited")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line) if line.strip() else self.next(max(0.0, end - time.time()))


class Pool:
    def __init__(self):
        self.workers = {"text": Worker("text"), "full": Worker("full"), "asr": Worker("asr"),
                        "rerank": Worker("rerank"), "ask": Worker("ask")}
        self._reaper = threading.Thread(target=self._reap, daemon=True, name="worker-reaper")
        self._reaper.start()

    def _reap(self):
        while True:
            time.sleep(30)
            try:
                self.reap_once()
            except Exception:
                pass

    def reap_once(self):
        """Stop workers idle longer than their limit; the Ask worker also stops as soon as Ask is switched off."""
        from . import ask
        cfg = config.load()
        idle = cfg["worker"]["idle_unload_min"] * 60
        a = ask.settings(cfg)
        for mode, w in self.workers.items():
            if not w.alive or w.lock.locked():
                continue
            if mode == "ask" and not a["enabled"]:
                w.stop()
            elif time.time() - w.last_used > (float(a["idle_unload_min"]) * 60 if mode == "ask" else idle):
                w.stop()

    def stop(self, mode: str, wait: float = 10):
        """Unload one worker now (stop("ask") when Ask is switched off). A request in progress gets `wait`
        seconds to finish; after that the worker is stopped anyway and that request ends with an error."""
        w = self.workers[mode]
        got = w.lock.acquire(timeout=wait)
        try:
            w.stop()
        finally:
            if got:
                w.lock.release()

    def _decode(self, reply: dict, n: int) -> tuple[np.ndarray, list[bool]]:
        v = np.frombuffer(base64.b64decode(reply["vecs"]), dtype=np.float32).reshape(n, DIMS)
        return v, reply.get("ok", [True] * n)

    def embed_texts(self, texts: list[str], batch: int | None = None) -> np.ndarray:
        if not texts:
            return np.zeros((0, DIMS), dtype=np.float32)
        w = self.workers["full"] if self.workers["full"].alive else self.workers["text"]
        batch = batch or config.load()["worker"]["batch_text"]
        out = []
        for i in range(0, len(texts), batch):
            part = texts[i:i + batch]
            v, _ = self._decode(w.call({"op": "text", "texts": part}), len(part))
            out.append(v)
        return np.concatenate(out)

    def embed_query(self, q: str) -> np.ndarray:
        return self.embed_texts([QUERY + q])[0]

    def embed_media(self, items: list[dict]) -> tuple[np.ndarray, list[bool]]:
        w = self.workers["full"]
        reply = w.call({"op": "media", "items": items}, timeout=1800)
        return self._decode(reply, len(items))

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        return self.workers["rerank"].call({"op": "rerank", "query": query, "docs": docs}, timeout=120)["scores"]

    def transcribe(self, path: str) -> dict:
        return self.workers["asr"].call({"op": "transcribe", "path": path}, timeout=3 * 3600)

    def ask_prepare(self):
        """Load the Ask model if it is not loaded yet (called while the search for a question runs)."""
        w = self.workers["ask"]
        with w.lock:
            if not w.alive:
                w._start()
                w.last_used = time.time()

    def ask_stream(self, messages: list[dict], max_tokens: int = 300, temperature: float = 0.1,
                   top_p: float = 0.9):
        """Stream an answer: yields {"token": "..."} dicts, then one {"done": True, ...} with timings.
        Raises ask.AskDisabled (before loading anything) while Ask is switched off."""
        from . import ask
        if not ask.settings()["enabled"]:
            raise ask.AskDisabled("ask_disabled")
        return self.workers["ask"].stream({"op": "ask", "messages": messages, "max_tokens": max_tokens,
                                           "temperature": temperature, "top_p": top_p})

    def has_asr(self) -> bool:
        asr = config.load()["model"].get("asr") or {}
        return bool(asr.get("path")) and os.path.isdir(asr["path"])

    def status(self) -> dict:
        return {m: {"running": w.alive, "rss_mb": w.rss_mb(),
                    "idle_s": round(time.time() - w.last_used) if w.alive else None,
                    **({"adapter": w.info.get("adapter")} if m == "rerank" and w.info else {})}
                for m, w in self.workers.items()}

    def stop_all(self, wait: float = 5):
        """Stop every worker at once. Each gets `wait` seconds to quit, and a worker still busy after that is killed.
        All of them wait together, so the whole stop costs `wait` seconds, not that much per busy worker."""
        threads = [threading.Thread(target=w.stop, args=(wait,), daemon=True) for w in self.workers.values()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(wait + 1)


_pool: Pool | None = None
_pool_lock = threading.Lock()


def pool() -> Pool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = Pool()
        return _pool
