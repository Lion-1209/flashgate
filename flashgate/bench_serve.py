"""flashgate bench-serve: expose the local bench over device-connect (L1).

A thin shell over BenchDriver (L0) — exactly four named RPCs, nothing
else: no arbitrary shell, no path arguments, no console passthrough.
Which board a bench serves is fixed by the --board profile the server
was started with; remote callers cannot choose it.

Requires the optional extra:

    pip install "flashgate[bench]"        # -> device-connect-edge

D2D mode (no broker, Zenoh multicast scouting) is the default shape for
a bench: set DEVICE_CONNECT_ALLOW_INSECURE=true for local development
and force DEVICE_CONNECT_DISCOVERY_MODE=d2d when no broker URLs exist.

THE RED LINE for every consumer (GPT6 report §4.2): the mesh wraps a
normally-delivered reply as success:true regardless of what the bench
verified. Judge verification ONLY by the operation payload —
state / exit_code / status and the embedded record's checks — never by
transport success alone.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import _thread
import zlib
from pathlib import Path
from typing import Optional

from .bench import BenchBusyError, BenchDriver
from .board import Board


def _lock_port(fw_dir: Path) -> int:
    # The 1000-port span is deterministic per fw_dir (same bench, same port
    # — the single-instance property depends on it). The BASE is overridable
    # so a host process squatting the default range can be sidestepped
    # without changing the per-bench determinism (measured: one full test
    # run makes ~160 lock attempts; a single static squatter hit ~15%).
    raw = os.environ.get("FLASHGATE_BENCH_LOCK_PORT_BASE", "17500")
    try:
        base = int(raw)
    except ValueError:
        base = -1              # fall through to the guided message below
    # 0 would make the OS assign ephemeral ports (the lock silently stops
    # being a lock); <1024 needs privileges on Linux (adversarial H1/L2)
    if base < 1024 or base > 64535 - 1000:
        raise ValueError(
            f"FLASHGATE_BENCH_LOCK_PORT_BASE={raw!r} must be a port number "
            "in [1024, 63535] (the lock span adds up to 1000)")
    return base + (zlib.crc32(
        str(fw_dir.resolve()).lower().encode("utf-8")) % 1000)


def _probe_lock_holder(port: int) -> str:
    """Classify who holds the port: 'bench' (the lock listener answers
    non-stop payloads with 'unknown command'), 'silent' (connect succeeds
    but nobody answers — the exact shape of a bench-serve DRAINING after
    --stop: its listener thread has returned while the socket stays
    bound), or 'none' (refused/other — an unrelated bound-but-not-
    listening squatter). Blame must land on the right holder."""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=2)
    except (TimeoutError, socket.timeout):
        return "silent"   # full backlog: someone's connection is queued —
                          # a listener exists; the drain shape again
    except OSError:
        return "none"
    with s:
        try:
            s.settimeout(2)
            s.sendall(b"flashgate-lock-probe\n")
            reply = s.recv(128)
        except (TimeoutError, socket.timeout):
            return "silent"    # connected, then silence: recv timed out —
                               # the draining shape
        except OSError:
            return "none"      # connected, then reset: bound but not
                               # really listening (Windows loopback
                               # deferred-refuse lands here)
        if reply.strip() == b"unknown command":
            return "bench"
        return "silent" if not reply else "none"


def acquire_bench_lock(fw_dir: Path) -> socket.socket:
    """OS-level single-instance lock per bench, auto-released when the
    process dies (a bound socket cannot outlive its process).

    Why: two bench-serves for the SAME firmware dir register the same
    device_id on the mesh and split-brain every RPC — the losing process
    races the winner for the board's serial port and surfaces as
    access-denied (seen live in the first cross-host test, 2026-09-15).
    Different boards (different fw dirs) lock different ports and may
    share a host."""
    port = _lock_port(fw_dir)      # ValueError (bad base) BEFORE any
    s = socket.socket()            # socket exists — nothing to leak
    try:
        # POSIX only: answered probe connections die into TIME_WAIT with
        # the lock port as their local port — without SO_REUSEADDR a Linux
        # rebind after a quick stop/start hits EADDRINUSE. Windows must NOT
        # set it: its SO_REUSEADDR behaves like SO_REUSEPORT and would let
        # a second bench-serve bind over a LIVE listener, destroying the
        # single-instance lock (caught by the lock tests, 2026-09-21).
        # Windows needs no opt-in — it already allows rebinding over dead
        # remnants by default.
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        s.close()
        holder = _probe_lock_holder(port)
        if holder == "bench":
            raise RuntimeError(
                f"a bench-serve lock listener answers on lock port {port} "
                f"— almost certainly another bench-serve for {fw_dir} "
                "(one bench-serve per bench; stop it with `bench-serve "
                "--stop`, do not kill it mid-verify. Rarely, a DIFFERENT "
                "firmware dir hashed to the same port — stopping would "
                "stop THAT bench)")
        if holder == "silent":
            raise RuntimeError(
                f"lock port {port} is held by a listener that does not "
                "answer — almost certainly a bench-serve DRAINING after "
                "--stop (wait for it to exit; do not kill it mid-drain). "
                "If it never exits, only then look for a stranger")
        raise RuntimeError(
            f"lock port {port} is held by an UNRELATED local process "
            f"(not a bench-serve) — free it (netstat -ano, then find the "
            "PID) or set FLASHGATE_BENCH_LOCK_PORT_BASE to move the lock "
            "range")


def _start_lock_listener(lock: socket.socket,
                         interrupt=None) -> threading.Thread:
    """Accept `stop` commands on the bench lock port. The stopper gets an
    immediate ack; THIS process then exits through serve()'s drain path —
    the operator never has to hunt for a PID (the pain that motivated
    this, see v0.7.1 field notes).

    `interrupt` defaults to interrupting the MAIN thread (where serve()
    runs asyncio.run — the KeyboardInterrupt unwinds through the drain
    path). It is injectable so tests can observe the signal without
    receiving a real KeyboardInterrupt."""
    if interrupt is None:
        interrupt = _thread.interrupt_main
    def _loop() -> None:
        while True:
            try:
                conn, _ = lock.accept()
            except OSError:
                return                       # lock closed: server exiting
            try:
                conn.settimeout(2)           # a silent local connection must
                data = conn.recv(64)         # not wedge the stop channel (R4)
                if data.strip().lower() == b"stop":
                    try:
                        conn.sendall(b"ok: draining and stopping\n")
                    except OSError:
                        pass
                    try:
                        interrupt()
                    except Exception:
                        pass
                    return
                conn.sendall(b"unknown command\n")
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
    t = threading.Thread(target=_loop, daemon=True,
                         name="flashgate-bench-lock")
    t.start()
    return t


def stop_bench(fw_dir: Path) -> bool | str:
    """Signal a running bench-serve for this firmware dir to stop.
    True = signalled (it drains its in-flight operation, then exits);
    False = nobody holds the lock; "draining" = connected (or queued on a
    full backlog) but nobody answered — the draining signature after an
    earlier --stop. A bare False for that state used to claim 'no
    bench-serve' about a server that was still alive (stage-5 L1)."""
    try:
        with socket.create_connection(("127.0.0.1", _lock_port(fw_dir)),
                                      timeout=3) as s:
            s.sendall(b"stop\n")
            reply = s.recv(128).decode("utf-8", errors="replace")
        return reply.startswith("ok")
    except (TimeoutError, socket.timeout):
        return "draining"
    except OSError:
        return False


def _build_driver(bench: BenchDriver):
    from device_connect_edge.drivers import DeviceDriver, emit, rpc

    class FlashGateBenchDriver(DeviceDriver):
        device_type = "flashgate-bench"

        def __init__(self, bench: BenchDriver):
            super().__init__()          # base builds _functions_cache etc.
            self._bench = bench
            self._loop: "asyncio.AbstractEventLoop | None" = None

        @emit()
        async def verify_completed(self, op_id: str, state: str,
                                   exit_code: Optional[int] = None,
                                   status: Optional[str] = None) -> None:
            """Fires once per operation at its terminal state
            (succeeded / failed / cancelled) — polling-free completion
            for remote consumers. Auxiliary: polling get_operation
            remains the authoritative contract."""

        def _operation_done(self, snapshot: dict) -> None:
            # Called from the bench worker THREAD: hop onto the runtime
            # loop. Best-effort — an emit failure never touches the op.
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            payload = {"op_id": snapshot["op_id"], "state": snapshot["state"]}
            # Omit null fields entirely: the SDK's schema converter drops
            # Optional's null, so emitting exit_code=None (cancelled /
            # crashed ops) would produce payload/schema contradictions
            # for validating consumers (adversarial review R2).
            if snapshot.get("exit_code") is not None:
                payload["exit_code"] = snapshot["exit_code"]
            if snapshot.get("status") is not None:
                payload["status"] = snapshot["status"]
            try:
                asyncio.run_coroutine_threadsafe(
                    self.verify_completed(**payload), loop)
            except Exception:
                pass

        def _remember_loop(self) -> None:
            if self._loop is None:
                self._loop = asyncio.get_running_loop()

        @rpc()
        async def describe_bench(self) -> dict:
            """Board identity, probe list, busy state of this bench."""
            self._remember_loop()
            return bench.describe()

        @rpc()
        async def start_verify(self, probes: Optional[list] = None) -> dict:
            """Start one hardware verify; returns the operation snapshot
            (state=running, op_id). Single-flight: a busy bench answers
            {"error": "busy", ...} — a transport success that must NOT be
            read as a verification pass.

            Note: Optional[list] (typing.Union), NOT `list | None` — the
            SDK's schema converter misses PEP 604 unions and would
            advertise probes as a STRING, steering well-behaved callers
            into flashing the board only to die on "unknown probe 'a'"
            (adversarial review F1)."""
            if probes is not None and (
                    not isinstance(probes, list)
                    or not all(isinstance(x, str) for x in probes)):
                # reject BEFORE any hardware is touched (F2): a malformed
                # argument must not cost a build+flash cycle
                return {"error": "invalid_argument",
                        "detail": "probes must be a list of probe names or null"}
            self._remember_loop()
            try:
                return bench.start_verify(probes)
            except BenchBusyError as exc:
                return {"error": "busy", "detail": str(exc)}

        @rpc()
        async def get_operation(self, op_id: str) -> dict:
            """Poll an operation until state is succeeded|failed|cancelled;
            the terminal snapshot carries exit_code/status and the run's
            evidence record (checks + what the board actually said)."""
            op = bench.get_operation(op_id)
            if op is None:
                return {"error": "unknown_operation", "op_id": op_id}
            return op

        @rpc()
        async def cancel_operation(self, op_id: str) -> dict:
            """True = cancelled (only before execution starts). Executing
            operations are never torn down mid-run — the request is
            recorded and the run completes with its REAL verdict."""
            return {"op_id": op_id, "cancelled": bench.cancel_operation(op_id)}

    return FlashGateBenchDriver


def _drain_timeout_s() -> float:
    """An in-flight verify may QUEUE on the bench lock (default 300 s,
    FLASHGATE_VERIFY_LOCK_WAIT) before it even starts building, and a
    hook-shaped verify takes up to ~480 s. A flat 120 s drain abandoned
    exactly the queued op — no terminal snapshot for a client told a
    verify was running (adversarial M2): the budget must cover both.
    Residual, stated honestly: the bench-RPC path has no external cap on
    a verify, and a worst-case LEGAL body (cold build 300 s + flash +
    probes) can brush past 480 s — the drain then gives up and says so
    (see _drain_current) instead of pretending it drained."""
    from . import cli
    return cli._verify_lock_wait() + _VERIFY_BUDGET_S


_VERIFY_BUDGET_S = 480.0        # the Stop hook's VERIFY_TIMEOUT_S shape;
                                # NOT a cap the bench path enforces


def _drain_current(bench) -> None:
    """Serve()'s exit path: wait out the in-flight operation (see
    bench.py's deployment constraint — a daemon worker killed mid-flash
    leaves CubeProgrammer orphaned and the run without its record).
    A second Ctrl+C during the drain is ignored on purpose: killing
    mid-drain recreates exactly that orphan; the printed budget tells
    the user how long the wait can legitimately take."""
    current = bench.describe().get("current_operation")
    if current:
        budget = _drain_timeout_s()
        print(f"\n[bench-serve] draining {current} before stop "
              f"(up to {budget:.0f}s; Ctrl+C is ignored until it "
              "finishes)...")
        done = bench.wait(current, timeout_s=budget)
        state = done.get("state") if done else None
        if state in ("succeeded", "failed", "cancelled"):
            print(f"[bench-serve] drained: {state} "
                  f"exit={done.get('exit_code')}")
        else:
            print(f"[bench-serve] drain budget exhausted after "
                  f"{budget:.0f}s — operation still "
                  f"{state or 'running'}; it ends here WITHOUT a "
                  "terminal snapshot (the bench-RPC path has no "
                  "verify cap; see _drain_timeout_s)")


def serve(board: Board, device_id: str | None = None) -> int:
    """Run the bench server until Ctrl+C. Blocks forever."""
    try:
        from device_connect_edge import DeviceRuntime
    except ImportError:
        print("bench-serve needs the optional dependency: "
              'pip install "flashgate[bench]" (device-connect-edge)')
        return 2

    try:
        lock = acquire_bench_lock(board.firmware_dir)   # held for life
    except (RuntimeError, ValueError) as exc:
        print(f"[bench-serve] {exc}")
        return 2

    _start_lock_listener(lock)
    bench = BenchDriver(board)
    driver = _build_driver(bench)(bench)
    bench.set_on_complete(driver._operation_done)   # verify_completed events
    ident = device_id or f"flashgate-bench-{board.name}"
    print(f"[bench-serve] {ident}: {board.name} ({board.mcu}) — "
          f"four RPCs over device-connect; Ctrl+C or `bench-serve --stop` "
          f"to stop")
    try:
        asyncio.run(DeviceRuntime(driver=driver,
                                  device_id=ident).run())
    except KeyboardInterrupt:
        # Drain before exit (bench.py's deployment constraint): a daemon
        # worker killed mid-flash leaves CubeProgrammer orphaned and the
        # run without its evidence record. SIGINT is ignored while the
        # drain runs: a second Ctrl+C killing the worker mid-flash is the
        # exact accident the drain exists to prevent. (The lock listener
        # thread has already returned after the first stop signal.)
        import signal
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
        _drain_current(bench)
        print("[bench-serve] stopped")
    except Exception as exc:              # e.g. invalid device_id (F5)
        print(f"[bench-serve] failed: {type(exc).__name__}: {exc}")
        return 2
    return 0
