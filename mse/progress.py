"""Live indexing progress per folder: a thread-safe, in-memory registry.

The indexer and the job queue report what they are doing (stage, done, total, the file being read, errors). The
service shows it as `progress` in /api/status and at GET /api/index/progress; `mse status` prints one line per folder.

Reading the registry never walks a folder and never writes to a collection database. Pending counts come from
read-only connections and are cached for a few seconds, because the app polls every 1.5 seconds.
"""

import os
import threading
import time
from collections import deque

from . import config, store

# the stages a folder can be in (idle = no job running for it)
STAGES = ("idle", "scanning", "extracting", "ocr", "transcribing", "embedding", "vocab", "optimizing")
# the stage names the indexer and the job queue report -> the stages above
_STAGE_OF = {
    "scanning": "scanning", "extract": "extracting", "ocr & pages": "ocr", "ocr & media": "ocr",
    "audio & video": "transcribing", "embedding text": "embedding", "embedding media": "embedding",
    "vocabulary": "vocab", "optimizing": "optimizing",
}
_LABEL = {
    "idle": "idle", "scanning": "checking files", "extracting": "reading files", "ocr": "OCR and pictures",
    "transcribing": "transcribing", "embedding": "embedding", "vocab": "building vocabulary",
    "optimizing": "optimizing",
}
_START_STAGE = {"embed": "embedding", "vocab": "vocab", "file": "extracting", "paths": "extracting"}
EWMA = 0.3            # weight of the newest rate sample (files per second)
ERRORS_KEEP = 5       # the most recent error messages kept per folder
COUNT_TTL_S = 4.0     # pending counts are read from the database at most this often


class _Folder:
    def __init__(self):
        self.job = None           # kind of the running job, None when idle
        self.status = None        # how the last job ended: done | error | cancelled
        self.stage = "idle"
        self.done = 0
        self.total = 0
        self.current = ""
        self.rate = None
        self.last_t = None
        self.last_done = 0
        self.started_at = None
        self.finished_at = None
        self.error_count = 0
        self.errors = deque(maxlen=ERRORS_KEEP)
        self.light = 0            # files the running scan still has to read


class Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self._folders: dict[str, _Folder] = {}
        self._counts: dict[str, tuple[float, dict]] = {}

    def _folder(self, name: str) -> _Folder:
        f = self._folders.get(name)
        if f is None:
            f = self._folders[name] = _Folder()
        return f

    def begin(self, name: str, kind: str) -> None:
        with self._lock:
            f = self._folder(name)
            f.job, f.status = kind, None
            f.stage = _START_STAGE.get(kind, "scanning")
            f.done = f.total = f.light = 0
            f.current, f.rate = "", None
            f.last_t, f.last_done = None, 0      # the first stage() call sets the baseline (embed's first `done` includes reused work)
            f.started_at, f.finished_at = time.time(), None
            f.error_count = 0
            f.errors.clear()

    def stage(self, name: str, stage: str, done: int = 0, total: int = 0, current: str = "") -> None:
        now = time.monotonic()
        with self._lock:
            f = self._folder(name)
            st = _STAGE_OF.get(stage, stage)
            if st != f.stage or f.last_t is None:
                # a new stage starts its own rate: its first `done` may already include reused work
                f.stage, f.rate = st, None
            elif done >= f.last_done and now > f.last_t:
                inst = (done - f.last_done) / (now - f.last_t)
                f.rate = inst if f.rate is None else EWMA * inst + (1 - EWMA) * f.rate
            f.last_t, f.last_done = now, done
            f.done, f.total = done, total
            f.current = os.path.basename(current or "")

    def set_pending(self, name: str, light: int | None = None) -> None:
        if light is None:
            return
        with self._lock:
            self._folder(name).light = max(0, int(light))

    def error(self, name: str, message: str) -> None:
        with self._lock:
            f = self._folder(name)
            f.error_count += 1
            f.errors.append(" ".join(str(message).split())[:200])

    def end(self, name: str, status: str) -> None:
        with self._lock:
            f = self._folder(name)
            f.job, f.status = None, status
            f.stage = "idle"
            f.done = f.total = f.light = 0
            f.current, f.rate = "", None
            f.finished_at = time.time()

    def _db_counts(self, name: str) -> dict:
        """Items waiting for OCR/media and for embedding, from a read-only connection (cached)."""
        now = time.monotonic()
        with self._lock:
            cached = self._counts.get(name)
        if cached and now - cached[0] < COUNT_TTL_S:
            return cached[1]
        counts = {"heavy": 0, "embed": 0}
        try:
            if store.db_path(name).exists():
                conn = store.connect(name, readonly=True)
                try:
                    counts["heavy"] = conn.execute(
                        "SELECT COUNT(*) FROM files WHERE heavy IS NOT NULL AND mode='full'").fetchone()[0]
                    counts["embed"] = conn.execute("SELECT COUNT(*) FROM items WHERE embedded=0").fetchone()[0]
                finally:
                    conn.close()
        except Exception:
            counts = {"heavy": 0, "embed": 0}          # busy or unreadable right now: show zero this time
        with self._lock:
            self._counts[name] = (now, counts)
        return counts

    def snapshot(self, names: list[str] | None = None) -> dict:
        if names is None:
            try:
                names = [c["name"] for c in config.load()["collections"]]
            except Exception:
                names = []
        out = {}
        for name in names:
            with self._lock:
                f = self._folders.get(name) or _Folder()
                job, status, stage = f.job, f.status, f.stage
                done, total, current, rate = f.done, f.total, f.current, f.rate
                error_count, recent = f.error_count, list(f.errors)
                started_at, finished_at = f.started_at, f.finished_at
            pending = self._db_counts(name)
            busy = job is not None
            eta = round((total - done) / rate) if busy and rate and total > done else None
            out[name] = {
                "job": job,
                "stage": stage if busy else "idle",
                "status": status,
                "done": done if busy else 0,
                "total": total if busy else 0,
                "percent": round(100.0 * done / total, 1) if busy and total else None,
                "current": current if busy else "",
                "rate": round(rate, 2) if busy and rate else None,
                "eta_s": eta,
                "errors": {"count": error_count, "recent": recent},
                # files still to read in this run: what is left of the extracting stage, counting down
                "pending": {"light": max(0, total - done) if busy and stage == "extracting" else 0,
                            "heavy": pending["heavy"], "embed": pending["embed"]},
                "started_at": started_at,
                "finished_at": finished_at,
            }
        return {"active": any(v["job"] for v in out.values()), "folders": out, "updated_at": time.time()}


def format_eta(seconds: int) -> str:
    if seconds < 90:
        return f"{max(1, round(seconds))} s"
    if seconds < 3600:
        return f"{round(seconds / 60)} min"
    return f"{seconds // 3600} h {round((seconds % 3600) / 60)} min"


def format_line(name: str, e: dict) -> str:
    """One line for `mse status`, e.g. `work          embedding 1,240/3,100 (40%)  ~2 min  3 errors`."""
    pend = e.get("pending") or {}
    errs = (e.get("errors") or {}).get("count") or 0
    tail = f"  {errs} error{'s' if errs != 1 else ''}" if errs else ""
    if e.get("job") is None:
        bits = []
        if pend.get("embed"):
            bits.append(f"{pend['embed']:,} to embed")
        if pend.get("heavy"):
            bits.append(f"{pend['heavy']:,} for OCR/media")
        return f"{name:<14}" + "idle" + (" · " + " · ".join(bits) if bits else "") + tail
    line = f"{name:<14}" + _LABEL.get(e.get("stage"), e.get("stage") or "working")
    if e.get("total"):
        pct = f" ({e['percent']:.0f}%)" if e.get("percent") is not None else ""
        line += f" {e['done']:,}/{e['total']:,}{pct}"
        if e.get("eta_s") is not None:
            line += f"  ~{format_eta(e['eta_s'])}"
    return line + tail


REGISTRY = Registry()
begin = REGISTRY.begin
stage = REGISTRY.stage
set_pending = REGISTRY.set_pending
error = REGISTRY.error
end = REGISTRY.end
snapshot = REGISTRY.snapshot
