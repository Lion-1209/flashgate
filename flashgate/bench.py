"""Bench driver core (Stage 2, level L0): the operation manager behind a
remote bench's RPC surface — network-free and hardware-free to test.

The device-connect front-end (L1) will be a thin shell over this module;
the verify pipeline itself is untouched (architecture §9). What this
module owns:

- operation-lite state machine: running -> succeeded | failed | cancelled
- SINGLE-FLIGHT: one verify at a time per bench. A second start_verify
  while one is executing raises BenchBusyError — this is also the
  anti-replay protection (a client retrying start_verify over a flaky
  network can never start a second flash; GPT6 report §8.1 red line).
- cancellation that never tears down hardware mid-run: an operation not
  yet executing is cancelled outright; once executing, cancel is
  advisory (recorded on the operation) and the run completes with its
  real verdict — a flash/erase is never aborted halfway.
- record association by the in-process write registry (records.LAST),
  never an mtime pick (the F3 lesson from the 0.6.0 adversarial review).

Threading: cmd_verify runs in a daemon thread. Its console prints go to
the server's stdout (visible in the bench terminal — deliberate); the
machine-readable surface is the operation snapshot and the attached
evidence record.

Deployment constraints for the L1 front-end: ONE driver per firmware
dir per process (enforced at construction); on server shutdown, drain
the current operation (`wait(op_id, timeout)`) — a daemon worker killed
mid-flash leaves CubeProgrammer running as an orphan and the operation
terminal-state-less.
"""

from __future__ import annotations

import copy
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import count
from typing import Callable

from . import __version__, records
from . import cli as cli_mod
from . import probes as probe_mod
from .board import Board

VerifyFn = Callable[[Board, "list[str] | None"], int]


class BenchBusyError(RuntimeError):
    """Another verify is executing on this bench (single-flight)."""


class DuplicateBenchError(RuntimeError):
    """A driver for this firmware dir already exists in this process.
    One driver per firmware dir per process — otherwise two drivers'
    verifies interleave on the same board and the record registry
    (process-global) can attach one bench's green evidence to another
    bench's failed operation (adversarial review F1)."""


# Process-wide driver registry, keyed by normcased firmware dir.
_DRIVERS: dict[str, "BenchDriver"] = {}
_REGISTRY_LOCK = threading.Lock()
# Process-wide operation counter: op ids must not collide across driver
# instances (same millisecond + per-instance counter did — F5).
_OP_SEQ = count(1)

MAX_OPERATION_HISTORY = 256          # completed ops kept for polling


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class _Operation:
    op_id: str
    created_at: str
    state: str = "running"        # running | succeeded | failed | cancelled
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    status_word: str | None = None
    summary: str = ""
    cancel_requested: bool = False
    error: str = ""
    record: dict | None = None
    # The registry state BEFORE this operation could have written anything
    # (runtime-audit FINDING-1): attaching uses IDENTITY against this
    # snapshot — immune to clock resolution, unlike a monotonic compare,
    # which Windows' 15.6 ms GetTickCount64 ticks let through a stale
    # same-tick record onto a failed run.
    _pre_last: object | None = field(default=None, repr=False, compare=False)

    def snapshot(self) -> dict:
        return {
            "op_id": self.op_id, "state": self.state,
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "exit_code": self.exit_code,
            "status": self.status_word, "summary": self.summary,
            "cancel_requested": self.cancel_requested,
            "error": self.error,
            # deep copy: a caller mutating its view must never reach the
            # internal state or the process-global records registry (F3)
            "record": copy.deepcopy(self.record),
        }


class BenchDriver:
    """Single-board bench: owns one in-flight verify and its history."""

    def __init__(self, board: Board, verify_fn: VerifyFn | None = None,
                 on_complete: "Callable[[dict], None] | None" = None):
        self._board = board
        self._verify_fn = verify_fn or (
            lambda b, names: cli_mod.cmd_verify(b, names))
        self._on_complete = on_complete    # auxiliary: see set_on_complete
        self._lock = threading.Lock()
        self._ops: dict[str, _Operation] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._current: _Operation | None = None
        key = os.path.normcase(str(board.firmware_dir.resolve()))
        with _REGISTRY_LOCK:
            existing = _DRIVERS.get(key)
            if existing is not None:
                raise DuplicateBenchError(
                    f"a BenchDriver for {board.firmware_dir} already exists "
                    f"in this process — one driver per firmware dir "
                    f"(single-flight and record association both assume it)")
            _DRIVERS[key] = self

    def set_on_complete(self, cb: "Callable[[dict], None] | None") -> None:
        """Register a best-effort completion callback: invoked once with
        the terminal snapshot when an operation reaches succeeded/failed/
        cancelled. Exceptions in the callback are swallowed —
        notification must never affect the operation or the bench (the
        RPC front-end uses this to emit verify_completed events)."""
        self._on_complete = cb

    def _notify(self, op: _Operation) -> None:
        if self._on_complete is None:
            return
        try:
            self._on_complete(op.snapshot())
        except Exception:
            pass

    # ------------------------------------------------------ describe_bench
    def describe(self) -> dict:
        probes_error = ""
        try:
            probe_names = list(probe_mod.load_probes(self._board.yaml_path))
        except Exception as exc:
            probe_names = []
            probes_error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            busy = (self._current is not None
                    and self._current.state == "running")
            current = self._current.op_id if busy else None
        return {
            "bench": self._board.name,
            "mcu": self._board.mcu,
            "profile": str(self._board.yaml_path),
            "flashgate": __version__,
            "probes": probe_names,
            "probes_error": probes_error,
            "capabilities": ["verify"],
            "busy": busy,
            "current_operation": current,
        }

    # ------------------------------------------------------ start_verify
    def start_verify(self, probes: "list[str] | None" = None) -> dict:
        with self._lock:
            if (self._current is not None
                    and self._current.state == "running"):
                raise BenchBusyError(
                    f"verify {self._current.op_id!r} is executing on this "
                    f"bench — poll it with get_operation; single verify at "
                    f"a time (no replayed flashes)")
            seq = next(_OP_SEQ)
            now = datetime.now(timezone.utc)
            ts = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}"
            op = _Operation(op_id=f"op-{ts}-{seq:05d}",
                            created_at=_iso(),
                            _pre_last=records.current_last())
            self._ops[op.op_id] = op
            self._current = op
            while len(self._ops) > MAX_OPERATION_HISTORY:   # oldest out
                oldest = next(iter(self._ops))
                self._ops.pop(oldest, None)
                self._threads.pop(oldest, None)
        thread = threading.Thread(target=self._execute, args=(op, probes),
                                  daemon=True, name=f"flashgate-{op.op_id}")
        self._threads[op.op_id] = thread
        thread.start()
        return op.snapshot()

    # ------------------------------------------------------ get_operation
    def get_operation(self, op_id: str) -> dict | None:
        with self._lock:
            op = self._ops.get(op_id)
            return op.snapshot() if op is not None else None

    # ------------------------------------------------------ cancel_operation
    def cancel_operation(self, op_id: str) -> bool:
        """True = the operation is now cancelled. False = it cannot be:
        unknown id, already terminal, or EXECUTING — hardware is never
        torn down mid-run; the request is recorded as advisory and the
        run completes with its real verdict."""
        with self._lock:
            op = self._ops.get(op_id)
            if op is None or op.state != "running":
                return False
            if op.started_at is None:
                # Created but the worker never picked it up: clean cancel.
                op.state = "cancelled"
                op.finished_at = _iso()
                op.summary = "cancelled before execution"
                cancelled = True
            else:
                cancelled = False
                op.cancel_requested = True
        if cancelled:
            self._notify(op)                 # outside the lock: a custom
            return True                      # callback may query the bench
        return False

    # ------------------------------------------------------ wait (helper)
    def wait(self, op_id: str, timeout_s: float = 600.0) -> dict | None:
        """Block until the operation's worker exits (tests / shutdown)."""
        thread = self._threads.get(op_id)
        if thread is None:
            return self.get_operation(op_id)
        thread.join(timeout_s)
        return self.get_operation(op_id)

    # ------------------------------------------------------ internals
    def _execute(self, op: _Operation, probes: "list[str] | None") -> None:
        with self._lock:
            if op.state == "cancelled":        # cancelled before start
                return
            op.started_at = _iso()
        try:
            rc = self._verify_fn(self._board, probes)
            with self._lock:
                op.exit_code = rc
                op.status_word = records.status_word(rc)
                op.state = "succeeded" if rc == 0 else "failed"
        except BaseException as exc:    # incl. SystemExit/CancelledError:
            with self._lock:            # a wedged "running" op would brick
                op.state = "failed"     # the bench forever (F2)
                op.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._attach_record(op)
            with self._lock:
                op.finished_at = _iso()
                if op.state == "running":    # worker exited sans verdict
                    op.state = "failed"
                    op.error = ((op.error + " | ") if op.error else "") + \
                        "worker exited without a verdict"
            self._notify(op)

    def _attach_record(self, op: _Operation) -> None:
        """Attach THIS run's evidence record via the in-process registry.

        Identity against the pre-run snapshot: a run that wrote nothing
        new (LAST unchanged) carries no record — never a stale one, even
        when the previous write landed in the same clock tick (FINDING-1).
        No mtime fallback (F3): missing evidence stays honestly missing."""
        last = records.current_last()
        if last is None or last is op._pre_last:
            return
        try:
            if str(last.get("fw_dir")) == str(self._board.firmware_dir):
                op.record = last["record"]
                if not op.summary:
                    op.summary = last["record"].get("run", {}).get(
                        "summary", "")
        except (KeyError, TypeError, ValueError):
            pass
