"""Background jobs: one queue, one worker thread, plus a scheduler for periodic scans and the nightly run."""

import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timedelta

from . import config, indexer, paths, readers
from . import progress as liveprog

STATE = paths.DATA / "state.json"
QUEUE = paths.DATA / "queue.json"


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _atomic_write(path, text: str) -> None:
    """Write through a temporary file, so a crash in the middle cannot leave a half-written queue or state file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _save_state(s: dict) -> None:
    _atomic_write(STATE, json.dumps(s, indent=2))


def on_ac_power() -> bool:
    try:
        return "AC Power" in subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return True


def idle_seconds() -> float:
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r'"HIDIdleTime" = (\d+)', out)
        return int(m.group(1)) / 1e9 if m else 0.0
    except Exception:
        return 0.0


NIGHT_HOURS = 5                  # the night window: from schedule.nightly_at, this many hours


def in_night_window(cfg: dict, now: datetime | None = None) -> bool:
    hh, mm = map(int, cfg["schedule"].get("nightly_at", "02:30").split(":"))
    now = now or datetime.now()
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < start:
        start -= timedelta(days=1)
    return now - start < timedelta(hours=NIGHT_HOURS)


def finetune_job(cfg: dict, nightly: bool = True, progress=None, cancel=None) -> dict:
    """The re-ranker fine-tune (finetune.py). The gate is checked here (cheap, no model); the work runs in its own
    low-priority process (`python -m mse.finetune run --json`: collect, train, evaluate, promote when it helps),
    on AC power only and, for the night run, inside the night window. It is stopped when the job is cancelled,
    the Mac goes on battery, or it overruns. A promotion restarts this daemon's re-rank worker (rerank.reset)."""
    from . import finetune, rerank
    s = finetune.settings(cfg)
    c = finetune.counts(s)
    entry = {"kind": "run", "favourites": c["favourites"], "queries": c["queries"], "dry_run": False}

    def skip(reason):
        finetune.log_event({**entry, "action": "skipped", "reason": reason, "duration_s": 0})
        return {"action": "skipped", "reason": reason}

    if not s.get("enabled", True):
        return {"action": "skipped", "reason": "disabled"}
    ok, msg = finetune.gate(c["favourites"], c["queries"], s["min_examples"], s["min_queries"])
    if not ok:
        return skip(msg)
    ac = cfg["schedule"].get("only_on_ac_power", True) or s.get("require_ac", True)
    if ac and not on_ac_power():
        return skip("skipped: on battery")
    if nightly and not in_night_window(cfg):
        return skip("skipped: outside the night window")
    if progress:
        progress("finetune", 0, 0, "collect, train, evaluate (log: finetune.log)")
    paths.LOGS.mkdir(parents=True, exist_ok=True)
    with open(paths.LOGS / "finetune.log", "ab") as log:
        p = subprocess.Popen(["/usr/bin/nice", "-n", "10", sys.executable, "-m", "mse.finetune", "run", "--json"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log, cwd=str(paths.PROJECT),
                             env=dict(os.environ, HF_HUB_OFFLINE="1", PYTHONUNBUFFERED="1"))
    t0, checked, why = time.time(), time.time(), None
    limit = (float(s.get("max_minutes", 15)) + 15) * 60          # + collect and evaluation
    while p.poll() is None:
        if cancel and cancel():
            why = "cancelled"
        elif time.time() - t0 > limit:
            why = f"stopped after {round(limit / 60)} minutes"
        elif ac and time.time() - checked > 30:
            checked = time.time()
            if not on_ac_power():
                why = "stopped: on battery"
        if why:
            p.terminate()
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                p.kill()
            finetune.log_event({**entry, "action": "stopped", "reason": why, "duration_s": round(time.time() - t0)})
            return {"action": "stopped", "reason": why}
        time.sleep(2)
    try:
        out = json.loads(p.stdout.read() or b"{}")
    except ValueError:
        out = {}
    if not out:
        out = {"action": "failed", "reason": f"exit code {p.returncode}: see {paths.LOGS / 'finetune.log'}"}
    if out.get("action") == "promoted":
        rerank.reset()
    return out


class JobManager:
    KINDS = ("scan", "sync", "embed", "file", "vocab", "paths", "learn", "finetune")
    GLOBAL = ("learn", "finetune")          # one job for all collections (collection None)

    def __init__(self):
        self.queue: deque[dict] = deque()
        self.current: dict | None = None
        self.history: deque[dict] = deque(maxlen=30)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self._cancel = False
        self._seq = 0
        self._frozen = False             # set by freeze(): from then on nothing is written and no queued job starts
        self._idle = threading.Event()   # set while no job is running
        self._idle.set()
        self.state = _load_state()
        self._restore()
        threading.Thread(target=self._loop, daemon=True, name="jobs").start()
        threading.Thread(target=self._scheduler, daemon=True, name="scheduler").start()

    # -------------------------------------------------- public

    def submit(self, kind: str, collection: str | None = None, path: str | None = None, reason: str = "manual",
               paths: list[str] | None = None) -> dict:
        if kind not in self.KINDS:
            raise ValueError(kind)
        cfg = config.load()
        if kind in self.GLOBAL:
            collection, path = None, None
            names = [None]
        else:
            names = [collection] if collection else [c["name"] for c in cfg["collections"] if c.get("enabled", True)]
        if collection and not config.get_collection(cfg, collection):
            raise KeyError(collection)
        jobs = []
        with self.lock:
            for n in names:
                if kind == "paths":
                    dup = next((j for j in self.queue if j["kind"] == "paths" and j["collection"] == n), None)
                    if dup:
                        dup["paths"] = sorted(set(dup["paths"]) | set(paths or []))
                        jobs.append(dup)
                        continue
                dup = next((j for j in self.queue if j["kind"] == kind and j["collection"] == n
                            and j.get("path") == path and kind != "paths"), None)
                if dup:
                    jobs.append(dup)
                    continue
                self._seq += 1
                j = {"id": self._seq, "kind": kind, "collection": n, "path": path, "reason": reason,
                     "status": "queued", "queued_at": time.time()}
                if kind == "paths":
                    j["paths"] = sorted(set(paths or []))
                self.queue.append(j)
                jobs.append(j)
        self.wake.set()
        self._persist()
        return {"jobs": [self._public(j) for j in jobs]}

    def cancel(self) -> None:
        with self.lock:
            self.queue.clear()
            self._cancel = True
        self._persist()

    def status(self) -> dict:
        with self.lock:
            return {
                "current": self._public(self.current) if self.current else None,
                "queued": [self._public(j) for j in self.queue],
                "recent": [self._public(j) for j in list(self.history)[-10:]][::-1],
                "next_scan_in_s": self._next_scan_in(),
                "nightly": self._nightly_info(),
                "watcher": self.watcher.status() if getattr(self, "watcher", None) else None,
            }

    # -------------------------------------------------- persistence: queued and interrupted work survives restarts

    def freeze(self) -> None:
        """Start a graceful stop. It does not wait (wait_idle does, with a bound). The queue is saved with the running
        job still in it, so the next start resumes that job; nothing else is written and no queued job starts; the
        running job is asked to stop at its next cancel check; and the in-flight markers are cleared, so a stop is
        never counted as a crash by the next start."""
        with self.lock:
            data = self._queue_data()
            self._frozen = True
            self._cancel = True
            try:
                _atomic_write(QUEUE, json.dumps(data))
            except OSError:
                pass
        indexer.STOPPING = True          # set before the markers are cleared: no read writes one again after this
        readers.close_all()              # idle reader processes die now; a read in progress ends within a quarter second
        self._clear_inflight()

    def wait_idle(self, timeout: float) -> bool:
        """Wait at most `timeout` seconds for the running job to stop at its next cancel check (it commits what it has
        first). Returns whether it did. The in-flight markers are cleared once more afterwards, for a read that was
        still starting when freeze() ran."""
        idle = self._idle.wait(timeout)
        self._clear_inflight()
        return idle

    @staticmethod
    def _clear_inflight() -> None:
        try:
            for c in config.load()["collections"]:
                indexer._mark_inflight(c["name"], [])
        except Exception:
            pass

    def _queue_data(self) -> list[dict]:
        """The running job and the queue, as queue.json keeps them. Call with self.lock held."""
        keep = ("kind", "collection", "path", "paths", "reason")
        items = ([self.current] if self.current and self.current.get("status") == "running" else []) + list(self.queue)
        return [{k: j[k] for k in keep if k in j} for j in items]

    def _persist(self) -> None:
        # Written under the lock, so a write cannot land after freeze() has saved its snapshot.
        with self.lock:
            if getattr(self, "_frozen", False):          # getattr: a manager built without __init__ (tests) has no flag
                return
            try:
                _atomic_write(QUEUE, json.dumps(self._queue_data()))
            except OSError:
                pass

    def _restore(self) -> None:
        try:
            data = json.loads(QUEUE.read_text())
        except Exception:
            return
        for j in data:
            try:
                reason = j.get("reason") or "manual"
                self.submit(j["kind"], j.get("collection"), j.get("path"),
                            reason=reason if reason.startswith("resumed") else f"resumed ({reason})",
                            paths=j.get("paths"))
            except Exception:
                pass

    # -------------------------------------------------- internals

    @staticmethod
    def _public(j):
        if not j:
            return None
        out = {k: v for k, v in j.items() if not k.startswith("_")}
        if "paths" in out:
            out["paths"] = out["paths"][:20] + ([f"… {len(j['paths']) - 20} more"] if len(j["paths"]) > 20 else [])
        return out

    def _progress(self, job):
        scoped = job.get("collection") if job.get("kind") not in self.GLOBAL else None

        def cb(stage, done, total, msg):
            job.update(stage=stage, done=done, total=total, message=msg[-120:] if msg else "")
            if scoped:
                liveprog.stage(scoped, stage, done, total, msg or "")
        return cb

    def _loop(self):
        while True:
            self.wake.wait(5)
            self.wake.clear()
            while True:
                with self.lock:
                    if not self.queue or self._frozen:       # a stop: no queued job starts
                        break
                    job = self.queue.popleft()
                    self.current = job
                    self._cancel = False
                    self._idle.clear()
                job.update(status="running", started_at=time.time())
                scoped = job.get("collection") if job["kind"] not in self.GLOBAL else None
                if scoped:
                    liveprog.begin(scoped, job["kind"])
                self._persist()
                try:
                    job["result"] = self._run(job)
                    job["status"] = "done"
                except indexer.Cancelled:
                    job["status"] = "cancelled"
                except Exception as e:
                    job["status"] = "error"
                    job["error"] = f"{type(e).__name__}: {e}"
                    (paths.LOGS / "daemon.log").open("a").write(traceback.format_exc())
                job["finished_at"] = time.time()
                if scoped:
                    liveprog.end(scoped, job["status"])
                    try:
                        indexer.maintain(scoped)          # WAL checkpoint and planner statistics after the job
                    except Exception:
                        (paths.LOGS / "daemon.log").open("a").write(traceback.format_exc())
                with self.lock:
                    self.history.append(job)
                    self.current = None
                    self._idle.set()
                self._persist()

    def _run(self, job):
        if job["kind"] == "learn":       # measure quality, re-tune, apply when the guardrails allow (learn.py)
            from . import learn
            return learn.run(progress=self._progress(job), cancel=lambda: self._cancel)
        if job["kind"] == "finetune":    # the re-ranker learns from favourites (finetune.py), in its own process
            return finetune_job(config.load(), job["reason"] == "nightly", self._progress(job), lambda: self._cancel)
        cfg = config.load()
        coll = config.get_collection(cfg, job["collection"])
        if not coll:
            raise KeyError(job["collection"])
        prog = self._progress(job)
        cancel = lambda: self._cancel
        kind = job["kind"]
        night = "nightly" in (job.get("reason") or "")      # a restored job says "resumed (nightly)"
        if kind == "file":
            return indexer.index_one(coll, job["path"])
        if kind == "paths":
            res = indexer.index_paths(coll, job["paths"], prog, cancel)
            ids = res.pop("ids")
            if ids:
                heavy_ids = indexer.heavy_pending_ids(coll["name"], ids)
                if heavy_ids and len(heavy_ids) <= cfg["schedule"].get("live_heavy_limit", 5) and on_ac_power():
                    res["heavy"] = indexer.heavy(coll, prog, cancel, only_ids=heavy_ids)
                res["embed"] = indexer.embed(coll, prog, cancel, media=bool(res.get("heavy")), only_file_ids=ids)
            return res
        out = {}
        if kind in ("scan", "sync"):
            out["scan"] = indexer.scan(coll, prog, cancel, night=night)
        if kind == "sync":
            # quick wins first: documents (3 at a time, smallest first), then make them searchable,
            # and only then the long audio/video work (shortest first). A sync is "Sync now" or the night run: its
            # heavy and embed stages pass priority "always" and are never paused by the idle gate (indexing.priority).
            out["heavy_docs"] = indexer.heavy(coll, prog, cancel, exclude_kinds=indexer.MEDIA_KINDS,
                                              workers=indexer.DOC_HEAVY_WORKERS, priority="always")
            out["embed"] = indexer.embed(coll, prog, cancel, media=True, batch_priority=night, priority="always")
            out["heavy_media"] = indexer.heavy(coll, prog, cancel, kinds=indexer.MEDIA_KINDS, priority="always")
            out["embed_media"] = indexer.embed(coll, prog, cancel, media=True, batch_priority=night,
                                               priority="always")
            if night:
                prog("optimizing", 0, 0, "")
                indexer.optimize_fts(coll["name"])        # the night run merges the keyword index once
                out["fts_optimized"] = True
        if kind == "embed":
            out["embed"] = indexer.embed(coll, prog, cancel, media=True, batch_priority=night)
        if kind == "scan":
            limit = cfg["schedule"]["live_embed_limit"]
            pending = indexer.pending_count(coll["name"], media=False)
            if 0 < pending <= limit:
                out["embed"] = indexer.embed(coll, prog, cancel, media=False)
            else:
                out["embed"] = {"deferred": pending}
        if kind in ("sync", "vocab"):
            out["vocab"] = indexer.build_vocab(coll, prog, cancel)
        return out

    def _next_scan_in(self):
        every = config.load()["schedule"]["scan_every_min"] * 60
        last = self.state.get("last_periodic_scan", 0)
        return max(0, round(last + every - time.time()))

    def _nightly_due(self, cfg) -> tuple[bool, str]:
        sch = cfg["schedule"]
        if not sch.get("nightly_enabled", True):
            return False, "disabled"
        hh, mm = map(int, sch.get("nightly_at", "02:30").split(":"))
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < target:
            target -= timedelta(days=1)          # the most recent scheduled time
        last = self.state.get("last_nightly_at", 0)
        if last >= target.timestamp():
            return False, "done"
        in_window = now - target < timedelta(hours=5)
        if not in_window and idle_seconds() < 20 * 60:
            return False, "waiting for the machine to be idle"
        if sch.get("only_on_ac_power", True) and not on_ac_power():
            return False, "waiting for AC power"
        return True, "due"

    def _nightly_info(self):
        sch = config.load()["schedule"]
        last = self.state.get("last_nightly_at")
        return {"at": sch.get("nightly_at"), "enabled": sch.get("nightly_enabled"),
                "last_run": datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M") if last else None}

    def _scheduler(self):
        from .watcher import Watcher
        self.watcher = Watcher(self)
        time.sleep(20)
        while True:
            try:
                cfg = config.load()
                busy = bool(self.current or self.queue)
                due, _ = self._nightly_due(cfg)
                if due and not busy:
                    self.state["last_nightly_at"] = time.time()
                    _save_state(self.state)
                    self.submit("sync", reason="nightly")
                    if cfg["schedule"].get("learn_nightly", True):
                        self.submit("learn", reason="nightly")     # queued behind the syncs: runs when they finish
                    if (cfg.get("finetune") or {}).get("enabled", True):
                        self.submit("finetune", reason="nightly")  # behind learn: gate, train, evaluate, promote
                elif not busy and self._next_scan_in() == 0 and cfg["collections"]:
                    self.state["last_periodic_scan"] = time.time()
                    _save_state(self.state)
                    self.submit("scan", reason="periodic")
            except Exception:
                (paths.LOGS / "daemon.log").open("a").write(traceback.format_exc())
            time.sleep(30)


# ---------------------------------------------------------------- background windows (mse/wholedisk.py)
# The describer asks for the idle time and the power source before every file. Each reading starts a subprocess, so
# the answer is kept for a few seconds: a gate that is checked often is not a load of its own.
_READINGS: dict[str, tuple[float, object]] = {}


def _cached(key: str, max_age: float, fn):
    now = time.monotonic()
    hit = _READINGS.get(key)
    if hit and now - hit[0] < max_age:
        return hit[1]
    value = fn()
    _READINGS[key] = (now, value)
    return value


def idle_seconds_cached(max_age: float = 1.0) -> float:
    """jobs.idle_seconds(), asked again only after max_age seconds."""
    return float(_cached("idle", max_age, idle_seconds))


def on_ac_power_cached(max_age: float = 10.0) -> bool:
    """jobs.on_ac_power(), asked again only after max_age seconds."""
    return bool(_cached("ac", max_age, on_ac_power))


# ---------------------------------------------------------------- one gate for heavy background work
# The whole-disk describer (mse/wholedisk.py) and the deep-folder indexing ask the same question before each heavy step:
# may it run now, given the power source and the user's input? These thresholds are the only definition of the answer.
HEAVY_IDLE_LOW_S = 60.0       # low: on AC power after this long without input; balanced: or on battery after this long
HEAVY_IDLE_ACTIVE_S = 5.0     # balanced: nothing heavy while the user typed or clicked within this many seconds
HEAVY_PRIORITIES = ("low", "balanced")


def heavy_work_allowed(priority: str, enabled: bool = True) -> tuple[bool, str | None]:
    """May a heavy background step run now? Returns (allowed, paused_reason); the reason is None when allowed, otherwise
    "disabled" (enabled is False: the caller's switch), "user_active" (the user typed or clicked too recently), or
    "on_battery" (on battery, and not idle long enough). low: AC power and HEAVY_IDLE_LOW_S without input. balanced: at
    least HEAVY_IDLE_ACTIVE_S without input, and either AC power or HEAVY_IDLE_LOW_S without input. It reads only the
    power source and the idle time (both cached for a few seconds); it holds no state of any feature. An unknown
    priority raises ValueError."""
    if priority not in HEAVY_PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(HEAVY_PRIORITIES)}, not {priority!r}")
    if not enabled:
        return False, "disabled"
    ac = on_ac_power_cached()
    idle = idle_seconds_cached()
    if priority == "low":
        if not ac:
            return False, "on_battery"
        if idle < HEAVY_IDLE_LOW_S:
            return False, "user_active"
        return True, None
    if idle < HEAVY_IDLE_ACTIVE_S:
        return False, "user_active"
    if ac or idle >= HEAVY_IDLE_LOW_S:
        return True, None
    return False, "on_battery"
