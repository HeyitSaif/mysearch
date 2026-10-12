"""Reader processes for the pure-Python readers (text, Office and zip files, mail, archive listings).

A Python thread cannot be stopped, so a read that hangs used to hold an indexing thread for ever. These readers run in
small child processes instead, and a child that runs past its time limit is killed and replaced.

A child is a plain `python -m mse.readers`. It is not a multiprocessing child: multiprocessing's "spawn" start method
re-runs the parent's main module inside the child, and mse/__main__.py starts the CLI without a guard. The protocol is
length-prefixed pickles: a request on the child's stdin, a reply on its stdout. A child's stray prints go to stderr.
"""

import importlib
import os
import pickle
import queue
import select
import signal
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path

READER_TIMEOUT_S = 120            # wall-clock limit for one file's read (indexing.reader_timeout_s overrides it)
LIGHT_TARGET = "mse.readers:light_target"
PING_TARGET = "mse.readers:ping"
PING_TIMEOUT_S = 30               # a new process must answer a ping this fast, or the pool is unavailable
_HEADER = 8                       # bytes of length prefix before each pickle
_ROOT = Path(__file__).resolve().parent.parent     # the directory that holds the mse package
_PYTHON = sys.executable          # the interpreter that runs the daemon (the project venv under launchd)
_POOLS: "weakref.WeakSet[ReaderPool]" = weakref.WeakSet()
_POOLS_LOCK = threading.Lock()     # the set changes from scan threads and is read by a freeze


class ReaderError(Exception):
    """The read failed. str(error) is the message the index records, the same text the in-process reader gives."""


class ReaderTimeout(ReaderError):
    """The read ran longer than the pool's limit. Its process was killed."""


class ReaderUnavailable(ReaderError):
    """No reader process can run here (it does not start, or does not answer a ping). This is not a fault of the file:
    the caller reads the file in-process instead, and no strike is recorded."""


class Stopped(Exception):
    """The pool was closed, or a stop was asked, during a read. Its process was killed; it is not a strike."""


def light_target(path, kind, ext, media):
    """The default read: the same call the indexer makes in-process, so the result is identical."""
    from . import extract
    return extract.light(Path(path), kind, ext, media)


def ping():
    """Answered by every new process before its first read: proves the module imports and the protocol works."""
    return "pong"


def _resolve(spec: str):
    module, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module), attr)


def _child_main() -> None:
    """The reader process: answer requests until stdin closes or a None request arrives. Runs in the child only."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)      # a Ctrl-C stops the read from the daemon side, not here
    # The protocol gets private descriptors. Requests come in on a copy of stdin; fd 0 and fd 1 are then pointed at
    # /dev/null and at stderr, so a subprocess that a read starts can neither read the request pipe nor write into
    # the reply pipe.
    inp = os.fdopen(os.dup(0), "rb")
    out = os.fdopen(os.dup(1), "wb")
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 0)
    os.dup2(2, 1)
    os.close(null)
    sys.stdout = sys.stderr
    while True:
        head = inp.read(_HEADER)
        if len(head) < _HEADER:
            return
        request = pickle.loads(inp.read(int.from_bytes(head, "big")))
        if request is None:
            return
        target, args = request
        try:
            data = pickle.dumps(("ok", _resolve(target)(*args)))
        except BaseException as e:                    # a failed read is reported as the in-process reader would
            data = pickle.dumps(("err", f"{type(e).__name__}: {e}"[:400]))
        out.write(len(data).to_bytes(_HEADER, "big") + data)
        out.flush()


class _Slot:
    """One child process and its pipes."""

    def __init__(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(x for x in (str(_ROOT), env.get("PYTHONPATH", "")) if x)
        # start_new_session: the child leads its own process group, so kill() takes the subprocesses a read started
        # (textutil, bsdtar, the helper) with it. A bare kill would leave them running with no time limit.
        self.proc = subprocess.Popen([_PYTHON, "-m", "mse.readers"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, cwd=str(_ROOT), env=env, start_new_session=True)
        self.fd = self.proc.stdout.fileno()

    def send(self, obj) -> None:
        data = pickle.dumps(obj)
        self.proc.stdin.write(len(data).to_bytes(_HEADER, "big") + data)
        self.proc.stdin.flush()

    def kill(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)  # the child and everything it started
        except OSError:
            pass
        try:
            self.proc.kill()
        except OSError:
            pass
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


class ReaderPool:
    """Up to `size` reader processes, started only when a read needs one. Thread-safe: a read borrows one process for
    its duration. A process that is killed (timeout, stop, crash) is not reused, and the next read starts a new one.

    `should_stop` is a callable that returns True when the pass is being stopped; a read in progress then ends at once,
    and its process is killed. The pool is also closed by close(), which kills every process it holds."""

    def __init__(self, size: int = 2, timeout_s: float = READER_TIMEOUT_S, target: str = LIGHT_TARGET,
                 should_stop=None):
        self.size = max(1, int(size))
        self.timeout_s = float(timeout_s)
        self.target = target
        self.should_stop = should_stop
        self._idle: queue.Queue = queue.Queue()
        self._live = 0
        self._lock = threading.Lock()
        self._closed = False
        self.broken: str | None = None      # set when no process can run: the rest of the pass reads in-process
        self.reported = False               # the caller logs the fallback once per pool
        with _POOLS_LOCK:
            _POOLS.add(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _stopping(self) -> bool:
        return self._closed or bool(self.should_stop and self.should_stop())

    def _borrow(self) -> _Slot:
        while True:
            if self._stopping():
                raise Stopped()
            if self.broken is not None:
                raise ReaderUnavailable(self.broken)
            try:
                slot = self._idle.get_nowait()
            except queue.Empty:
                slot = None
            if slot is not None:
                if slot.proc.poll() is None:
                    return slot
                slot.kill()                       # it died while idle: never hand a dead process to a read
                with self._lock:
                    self._live -= 1
                continue
            with self._lock:                      # the lock is not held while a process starts: close() must never wait on it
                if self._closed:
                    raise Stopped()
                reserved = self._live < self.size
                if reserved:
                    self._live += 1
            if reserved:
                try:
                    return self._start()          # ReaderUnavailable when no process can answer a ping
                except BaseException:
                    with self._lock:
                        self._live -= 1
                    raise
            try:
                return self._idle.get(timeout=0.2)
            except queue.Empty:
                continue

    def _start(self) -> _Slot:
        """A new process, proven by a ping. Any failure to get one makes the pool unavailable for this pass: the reads
        then run in-process, so a broken interpreter cannot strike every file as failed."""
        slot = None
        try:
            slot = _Slot()
            slot.send((PING_TARGET, ()))
            tag, payload = self._await_reply(slot, time.monotonic() + PING_TIMEOUT_S, PING_TIMEOUT_S)
            if tag != "ok" or payload != "pong":
                raise ReaderError(f"no answer to a ping: {payload}")
            return slot
        except Stopped:
            if slot is not None:
                slot.kill()
            raise
        except (OSError, ValueError, EOFError, pickle.UnpicklingError, ReaderError) as e:
            if slot is not None:
                slot.kill()
            self.broken = f"{type(e).__name__}: {e}"[:300]
            raise ReaderUnavailable(self.broken)

    def _release(self, slot: _Slot, healthy: bool) -> None:
        with self._lock:
            if healthy and not self._closed:
                self._idle.put(slot)
                return
            self._live -= 1
        slot.kill()

    def _await_reply(self, slot: _Slot, deadline: float, limit_s: float):
        """Waits for one reply, polling so that a stop or the deadline is noticed within a quarter second."""
        def read_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                ready, _, _ = select.select([slot.fd], [], [], 0.25)
                if not ready:
                    if self._stopping():
                        raise Stopped()
                    if slot.proc.poll() is not None:
                        raise ReaderError("reader process exited")
                    if time.monotonic() >= deadline:
                        raise ReaderTimeout(f"timed out after {int(limit_s)} s")
                    continue
                chunk = os.read(slot.fd, n - len(buf))
                if not chunk:
                    raise ReaderError("reader process closed its output")
                buf += chunk
            return bytes(buf)

        n = int.from_bytes(read_exact(_HEADER), "big")
        return pickle.loads(read_exact(n))

    def run(self, path, kind: str, ext: str, media: dict, timeout_s: float | None = None) -> dict:
        """The light reader's result for one file, computed in a reader process. `timeout_s` overrides the pool's limit
        for this read (a kind that waits on a subprocess of its own needs more than the pure-Python readers).

        Raises ReaderTimeout after the limit (the process and what it started are killed), Stopped when a stop was asked
        (the process is killed; this is not a failure of the file), and ReaderError when the read itself failed."""
        limit = self.timeout_s if timeout_s is None else float(timeout_s)
        slot = self._borrow()
        healthy = False
        try:
            try:
                slot.send((self.target, (str(path), kind, ext, media)))
            except (OSError, ValueError) as e:
                raise ReaderError(f"reader process unavailable: {e}")
            tag, payload = self._await_reply(slot, time.monotonic() + limit, limit)
            healthy = True                 # the process answered; a read that failed does not cost it
        except (EOFError, pickle.UnpicklingError) as e:
            raise ReaderError(f"reader process failed: {e}")
        finally:
            self._release(slot, healthy)
        if tag == "ok":
            return payload
        raise ReaderError(payload)

    def close(self) -> None:
        """Kills the idle processes now. A read still in progress notices within a quarter second and kills its own."""
        with self._lock:
            self._closed = True
            idle = []
            while True:
                try:
                    idle.append(self._idle.get_nowait())
                except queue.Empty:
                    break
            self._live -= len(idle)
        for slot in idle:
            slot.kill()


def close_all() -> None:
    """Kills the reader processes of every pool (a fast stop). Reads that wait on them end at once."""
    with _POOLS_LOCK:
        pools = list(_POOLS)
    for pool in pools:
        pool.close()


if __name__ == "__main__":
    _child_main()
