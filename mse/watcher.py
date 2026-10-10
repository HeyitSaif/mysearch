"""Live indexing: macOS FSEvents (via the native helper) → re-index just the changed files a few seconds later.

The periodic scan stays as a safety net (missed events, sleep, volumes that were offline).
"""

import json
import os
import subprocess
import threading
import time
import traceback
from collections import defaultdict

from . import config, paths
from .indexer import matchers

SETTLE_S = 3.0          # wait for a burst of writes to finish ...
MAX_WAIT_S = 45.0       # ... but flush anyway after this long: a file written all the time must not hold everything back
BURST_PATHS = 500       # more changed paths than this in one flush: a full scan of the folder instead of a path job
# FSEvents flags (the helper passes them through as they come)
MUST_SCAN_SUBDIRS = 0x1     # the event path is a folder whose subtree must be re-read: a path job walks it and sweeps it
USER_DROPPED = 0x2          # events were lost: the collection gets a full scan
KERNEL_DROPPED = 0x4
EVENT_IDS_WRAPPED = 0x8
ROOT_CHANGED = 0x20         # the watched folder itself moved or was replaced
FULL_RESCAN = USER_DROPPED | KERNEL_DROPPED | EVENT_IDS_WRAPPED | ROOT_CHANGED


class Watcher:
    def __init__(self, jobs):
        self.jobs = jobs
        self.proc: subprocess.Popen | None = None
        self.roots: tuple = ()
        self.pending: dict[str, set] = defaultdict(set)
        self.rescan: set[str] = set()
        self.last_event = 0.0
        self.first_pending = 0.0    # when the current burst of changes started (for MAX_WAIT_S)
        self.lock = threading.Lock()
        self.events_seen = 0
        threading.Thread(target=self._supervise, daemon=True, name="watcher").start()
        threading.Thread(target=self._flush_loop, daemon=True, name="watcher-flush").start()

    def status(self) -> dict:
        return {"running": bool(self.proc and self.proc.poll() is None), "folders": len(self.roots),
                "events": self.events_seen, "pending": sum(len(v) for v in self.pending.values())}

    # ---------------------------------------------------------- helper process

    def _wanted_roots(self) -> tuple:
        cfg = config.load()
        return tuple(sorted(c["path"] for c in cfg["collections"] if c.get("enabled", True) and os.path.isdir(c["path"])))

    def _supervise(self):
        while True:
            try:
                want = self._wanted_roots()
                alive = self.proc and self.proc.poll() is None
                if want != self.roots or (want and not alive):
                    self._stop()
                    self.roots = want
                    if want and paths.HELPER.exists():
                        self.proc = subprocess.Popen([str(paths.HELPER), "watch", *want], stdin=subprocess.PIPE,
                                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
            except Exception:
                (paths.LOGS / "daemon.log").open("a").write(traceback.format_exc())
            time.sleep(15)

    def _stop(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.stdin.close()
                p.wait(timeout=3)
            except Exception:
                p.kill()

    def _read(self, proc):
        cfg = config.load()
        colls = [(c["path"].rstrip("/") + "/", c) for c in cfg["collections"] if c.get("enabled", True)]
        rules = {c["name"]: matchers(c)[0] for _, c in colls}
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "path" not in ev:
                continue
            p, flags = ev["path"], ev.get("flags", 0)
            for prefix, c in colls:
                if p.startswith(prefix) or p + "/" == prefix:
                    rel = p[len(prefix):]
                    with self.lock:
                        if not (self.pending or self.rescan):
                            self.first_pending = time.time()
                        self.events_seen += 1
                        self.last_event = time.time()
                        if flags & FULL_RESCAN or not rel:
                            self.rescan.add(c["name"])
                        elif not rules[c["name"]].match(rel) and not any(
                                part.startswith(".") for part in rel.split("/")):
                            # a file, or a folder (MUST_SCAN_SUBDIRS or a plain folder event): the path job walks a
                            # folder and removes whatever vanished inside it, so no full scan is needed
                            self.pending[c["name"]].add(rel)
                    break

    # ---------------------------------------------------------- hand-off to the job queue

    def _flush_loop(self):
        while True:
            time.sleep(1.0)
            with self.lock:
                now = time.time()
                if not (self.pending or self.rescan):
                    continue
                if now - self.last_event < SETTLE_S and now - self.first_pending < MAX_WAIT_S:
                    continue
                batch, self.pending = self.pending, defaultdict(set)
                rescan, self.rescan = self.rescan, set()
            try:
                for name in rescan:
                    self.jobs.submit("scan", name, reason="watch")
                for name, rels in batch.items():
                    if name not in rescan and rels:
                        if len(rels) > BURST_PATHS:
                            # a big burst (a folder copied in, a sync): the scan pipeline reads it in parallel and commits in batches
                            self.jobs.submit("scan", name, reason="watch")
                        else:
                            self.jobs.submit("paths", name, paths=sorted(rels), reason="watch")
            except Exception:
                (paths.LOGS / "daemon.log").open("a").write(traceback.format_exc())
