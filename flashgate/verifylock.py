"""Cross-process verify lock: one hardware verify at a time per bench.

Real-world incident (2026-09): a user-level and a session-level Stop hook
registered in the same harness both fired on ONE stop event and raced for
the bench — two concurrent verifies fighting over the console serial port
and the debug probe, with the loser surfacing a misleading environment
failure (rc=6, "cannot open COM3") for firmware that was perfectly fine.
The same collision hits any manual ``flashgate verify`` started while the
hook is verifying.

The fix is mutual exclusion at the one place every verify passes through:
``cmd_verify``. A second verify WAITS for the bench (blocking, bounded by
a finite wait budget) instead of fighting it. The lock is an OS-level
byte-range lock on a file under the firmware dir's ``.flashgate/`` state
dir, so it never feeds the tree fingerprint, and the OS releases it when
the holding process dies — a crashed verify cannot leave a stale lock
behind.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

LOCK_RELPATH = ".flashgate/verify.lock"
DEFAULT_WAIT_S = 300.0        # < the Stop hook's 480s verify timeout budget
_POLL_S = 0.2


class VerifyLockTimeout(Exception):
    """Another verify held the bench lock past the wait budget."""


def lock_path(fw_dir: Path) -> Path:
    return fw_dir / LOCK_RELPATH


def _try_lock(fh: IO) -> bool:
    """One non-blocking attempt; False means 'held elsewhere right now'."""
    if os.name == "nt":
        import msvcrt
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    else:
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


def _unlock(fh: IO) -> None:
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass          # the handle is closing anyway; the OS reclaims it
    else:
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


class BenchLock:
    """Explicit acquire/release bench lock.

    The CLI uses this form so lock-SETUP errors (unwritable state dir)
    stay strictly separated from errors raised by the verify body —
    mislabeling one as the other is exactly the misleading-failure class
    this module exists to remove.
    """

    def __init__(self, fw_dir: Path):
        self.path = lock_path(fw_dir)
        self._fh: IO | None = None

    def acquire(self, wait_s: float = DEFAULT_WAIT_S) -> "BenchLock":
        """Block up to ``wait_s`` seconds for a concurrent verify to
        finish, then raise VerifyLockTimeout (the caller decides the
        failure mode — for the CLI that is exit 6 with a truthful reason,
        never a silent pass). ``wait_s=0`` means try once and give up."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # touch + "r+b": every contender shares one file; the lock lives
        # in the OS's byte-range state, not the file's content.
        self.path.touch(exist_ok=True)
        fh = self.path.open("r+b")
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            if _try_lock(fh):
                self._fh = fh
                return self
            if time.monotonic() >= deadline:
                fh.close()
                raise VerifyLockTimeout(
                    f"another verify has held {self.path} for over {wait_s:.1f}s")
            time.sleep(_POLL_S)

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        _unlock(fh)
        fh.close()

    def __enter__(self) -> "BenchLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


@contextmanager
def verify_lock(fw_dir: Path, wait_s: float = DEFAULT_WAIT_S) -> Iterator[Path]:
    """Context-manager form of BenchLock (yields the lock file path)."""
    lock = BenchLock(fw_dir)
    lock.acquire(wait_s)
    try:
        yield lock.path
    finally:
        lock.release()
