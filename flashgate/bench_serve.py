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
import socket
import zlib
from pathlib import Path
from typing import Optional

from .bench import BenchBusyError, BenchDriver
from .board import Board


def _lock_port(fw_dir: Path) -> int:
    return 17500 + (zlib.crc32(
        str(fw_dir.resolve()).lower().encode("utf-8")) % 1000)


def acquire_bench_lock(fw_dir: Path) -> socket.socket:
    """OS-level single-instance lock per bench, auto-released when the
    process dies (a bound socket cannot outlive its process).

    Why: two bench-serves for the SAME firmware dir register the same
    device_id on the mesh and split-brain every RPC — the losing process
    races the winner for the board's serial port and surfaces as
    access-denied (seen live in the first cross-host test, 2026-09-15).
    Different boards (different fw dirs) lock different ports and may
    share a host."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", _lock_port(fw_dir)))
        s.listen(1)
        return s
    except OSError:
        s.close()
        raise RuntimeError(
            f"another bench-serve for {fw_dir} appears to be running on "
            f"this machine — one bench-serve per bench (the running one "
            f"owns lock port {_lock_port(fw_dir)}; kill it first)")


def _build_driver(bench: BenchDriver):
    from device_connect_edge.drivers import DeviceDriver, rpc

    class FlashGateBenchDriver(DeviceDriver):
        device_type = "flashgate-bench"

        def __init__(self, bench: BenchDriver):
            super().__init__()          # base builds _functions_cache etc.
            self._bench = bench

        @rpc()
        async def describe_bench(self) -> dict:
            """Board identity, probe list, busy state of this bench."""
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
    except RuntimeError as exc:
        print(f"[bench-serve] {exc}")
        return 2

    bench = BenchDriver(board)
    driver_cls = _build_driver(bench)
    ident = device_id or f"flashgate-bench-{board.name}"
    print(f"[bench-serve] {ident}: {board.name} ({board.mcu}) — "
          f"four RPCs over device-connect; Ctrl+C to stop")
    try:
        asyncio.run(DeviceRuntime(driver=driver_cls(bench),
                                  device_id=ident).run())
    except KeyboardInterrupt:
        # Drain before exit (bench.py's deployment constraint): a daemon
        # worker killed mid-flash leaves CubeProgrammer orphaned and the
        # run without its evidence record.
        current = bench.describe().get("current_operation")
        if current:
            print(f"\n[bench-serve] draining {current} before stop...")
            done = bench.wait(current, timeout_s=120)
            print(f"[bench-serve] drained: {done['state']} "
                  f"exit={done.get('exit_code')}")
        print("[bench-serve] stopped")
    except Exception as exc:              # e.g. invalid device_id (F5)
        print(f"[bench-serve] failed: {type(exc).__name__}: {exc}")
        return 2
    return 0
