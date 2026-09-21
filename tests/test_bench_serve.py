"""bench-serve front-end RPCs (L1, network-free): the four wrappers over
BenchDriver. The device-connect transport is NOT exercised here — these
pin the semantics the shell must not dilute (including the busy-as-
payload red line: transport success, structured busy error)."""

import asyncio
import random

import pytest

pytest.importorskip("device_connect_edge",
                    reason="bench extra not installed")

from flashgate import records
from flashgate.bench import BenchDriver, DuplicateBenchError
from flashgate.bench_serve import _build_driver
from tests.test_cli import make_board


@pytest.fixture(autouse=True)
def _random_lock_port_base(monkeypatch):
    """A host process squatting the default 17500-18499 lock range made
    these tests flaky (~15% per full run, measured with a static squatter
    on 18432): move the whole range to a random base per run. Per-bench
    determinism is preserved — same fw_dir still maps to the same port
    under whatever base is active."""
    monkeypatch.setenv("FLASHGATE_BENCH_LOCK_PORT_BASE",
                       str(random.randrange(20000, 40000)))


def _make(tmp_path, verify_fn):
    board = make_board(tmp_path)
    bench = BenchDriver(board, verify_fn=verify_fn)      # ONE driver only
    return _build_driver(bench)(bench), board


class TestFourRpcs:
    def test_describe_bench_shape(self, tmp_path):
        (tmp_path / "d").mkdir()
        drv, board = _make(tmp_path / "d", lambda b, n: 0)
        info = asyncio.run(drv.describe_bench())
        assert info["bench"] == "test-board"
        assert info["busy"] is False
        assert "verify" in info["capabilities"]

    def test_start_verify_returns_running_op(self, tmp_path):
        (tmp_path / "d").mkdir()
        import threading
        release = threading.Event()

        def blocker(b, n):
            release.wait(10)
            return 0

        drv, board = _make(tmp_path / "d", blocker)
        op = asyncio.run(drv.start_verify(["all"]))
        assert op["state"] == "running" and op["op_id"]

        # busy while executing: a PAYLOAD error, never an exception —
        # the mesh would wrap any normal reply in success:true anyway
        busy = asyncio.run(drv.start_verify())
        assert busy["error"] == "busy"
        assert op["op_id"] in busy["detail"]

        release.set()
        done = asyncio.run(drv.get_operation(op["op_id"]))
        while done["state"] == "running":
            done = asyncio.run(drv.get_operation(op["op_id"]))
        assert done["state"] == "succeeded"

    def test_unknown_operation_is_payload_error(self, tmp_path):
        (tmp_path / "d").mkdir()
        drv, board = _make(tmp_path / "d", lambda b, n: 0)
        res = asyncio.run(drv.get_operation("op-nope"))
        assert res["error"] == "unknown_operation"

    def test_cancel_passthrough(self, tmp_path):
        (tmp_path / "d").mkdir()
        drv, board = _make(tmp_path / "d", lambda b, n: 0)
        res = asyncio.run(drv.cancel_operation("op-whatever"))
        assert res == {"op_id": "op-whatever", "cancelled": False}


class TestMissingExtra:
    def test_serve_without_edge_prints_hint(self, tmp_path, monkeypatch, capsys):
        import flashgate.bench_serve as bs
        board = make_board(tmp_path)
        monkeypatch.setitem(__import__("sys").modules, "device_connect_edge", None)
        rc = bs.serve(board)
        assert rc == 2
        assert "flashgate[bench]" in capsys.readouterr().out


class TestStartVerifyArgumentGuard:
    """Adversarial F1/F2/F4: the advertised schema must be an ARRAY, and
    malformed probes must be rejected BEFORE any hardware is touched."""

    def test_advertised_schema_is_array(self, tmp_path):
        (tmp_path / "d").mkdir()
        board = make_board(tmp_path / "d")
        bench = BenchDriver(board, verify_fn=lambda b, n: 0)
        drv = _build_driver(bench)(bench)
        start = next(f for f in drv.functions if f.name == "start_verify")
        probes_schema = start.parameters["properties"]["probes"]
        assert probes_schema["type"] == "array", \
            "discovery must advertise probes as an array, not a string"

    def test_probes_passthrough_not_silently_dropped(self, tmp_path):
        (tmp_path / "d").mkdir()
        seen = {}

        def recorder(b, names):
            seen["names"] = names
            return 0

        board = make_board(tmp_path / "d")
        bench = BenchDriver(board, verify_fn=recorder)
        drv = _build_driver(bench)(bench)
        asyncio.run(drv.start_verify(["p1", "p2"]))
        deadline = __import__("time").time() + 5
        while "names" not in seen and __import__("time").time() < deadline:
            __import__("time").sleep(0.01)
        assert seen["names"] == ["p1", "p2"]

    def test_malformed_probes_rejected_without_hardware(self, tmp_path):
        (tmp_path / "d").mkdir()
        touched = {"n": 0}

        def never(b, n):
            touched["n"] += 1
            return 0

        board = make_board(tmp_path / "d")
        bench = BenchDriver(board, verify_fn=never)
        drv = _build_driver(bench)(bench)
        for bad in ("all", 42, {"a": 1}, [1, 2]):
            res = asyncio.run(drv.start_verify(bad))
            assert res["error"] == "invalid_argument", bad
        assert touched["n"] == 0, "malformed args must not reach hardware"


class TestSingleInstanceLock:
    """v0.7.1 field lesson: two bench-serves on one bench split-brain the
    mesh under one device_id and race for the serial port. The lock is a
    bound loopback socket — exclusive while alive, auto-released on
    process death (no stale lock files to clean up)."""

    def test_second_lock_same_fw_dir_rejected(self, tmp_path):
        import socket
        import pytest
        from flashgate import bench_serve as bs
        lock = bs.acquire_bench_lock(tmp_path)
        listener = bs._start_lock_listener(lock, interrupt=lambda: None)
        try:
            with pytest.raises(RuntimeError, match="one bench-serve per bench"):
                bs.acquire_bench_lock(tmp_path)
        finally:
            # Linux portability (CI ubuntu leg): a plain close() does NOT
            # wake a thread blocked in accept(), so the port stays bound
            # and the re-acquire below would see a live listener. shutdown
            # wakes the accept on both platforms; join waits it out.
            try:
                lock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            lock.close()
            listener.join(timeout=5)
        # released: acquirable again
        again = bs.acquire_bench_lock(tmp_path)
        again.close()

    def test_different_fw_dirs_lock_independently(self, tmp_path):
        from flashgate.bench_serve import acquire_bench_lock
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        la = acquire_bench_lock(tmp_path / "a")
        lb = acquire_bench_lock(tmp_path / "b")
        la.close()
        lb.close()

    def test_serve_refuses_when_lock_held(self, tmp_path, monkeypatch, capsys):
        from flashgate import bench_serve as bs
        from tests.test_cli import make_board
        board = make_board(tmp_path)
        lock = bs.acquire_bench_lock(board.firmware_dir)
        bs._start_lock_listener(lock, interrupt=lambda: None)
        try:
            rc = bs.serve(board)
            assert rc == 2
            assert "one bench-serve per bench" in capsys.readouterr().out
        finally:
            lock.close()


class TestLockDiagnostics:
    """The bind-failure message must blame the RIGHT holder: a real
    bench-serve lock (answers the probe) gets the stop hint; an
    unrelated squatter gets the honest unrelated-process message."""

    def test_real_bench_lock_keeps_stop_hint(self, tmp_path):
        import pytest
        from flashgate import bench_serve as bs
        lock = bs.acquire_bench_lock(tmp_path)
        bs._start_lock_listener(lock, interrupt=lambda: None)
        try:
            with pytest.raises(RuntimeError,
                               match="bench-serve --stop"):
                bs.acquire_bench_lock(tmp_path)
        finally:
            lock.close()

    def test_squatter_named_as_unrelated_process(self, tmp_path):
        # a REAL stranger: a local service that answers something the
        # lock listener never would (deterministic on every platform —
        # the bind-only shape is nondeterministic on Windows loopback:
        # deferred-refuse vs reset-at-recv)
        import socket as pysocket
        import threading
        import pytest
        from flashgate import bench_serve as bs
        port = bs._lock_port(tmp_path)
        stranger = pysocket.socket()
        stranger.bind(("127.0.0.1", port))
        stranger.listen(1)

        def answer_garbage():
            try:
                conn, _ = stranger.accept()
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                conn.close()
            except OSError:
                pass
        th = threading.Thread(target=answer_garbage, daemon=True)
        th.start()
        try:
            with pytest.raises(RuntimeError,
                               match="UNRELATED local process"):
                bs.acquire_bench_lock(tmp_path)
        finally:
            stranger.close()

    def test_silent_holder_named_as_draining(self, tmp_path):
        # listening but never accepting/answering: the exact shape of a
        # bench-serve DRAINING after --stop (listener thread returned,
        # socket still bound) — must NOT be blamed as an unrelated
        # process (stage-7 L-1, fixed 2026-09-21)
        import socket as pysocket
        import pytest
        from flashgate import bench_serve as bs
        port = bs._lock_port(tmp_path)
        silent = pysocket.socket()
        silent.bind(("127.0.0.1", port))
        silent.listen(1)          # kernel backlog accepts; nobody replies
        try:
            with pytest.raises(RuntimeError,
                               match="DRAINING after --stop"):
                bs.acquire_bench_lock(tmp_path)
        finally:
            silent.close()

    def test_port_base_default_and_override(self, tmp_path, monkeypatch):
        from flashgate import bench_serve as bs
        monkeypatch.delenv("FLASHGATE_BENCH_LOCK_PORT_BASE", raising=False)
        assert 17500 <= bs._lock_port(tmp_path) < 18500
        monkeypatch.setenv("FLASHGATE_BENCH_LOCK_PORT_BASE", "30000")
        assert 30000 <= bs._lock_port(tmp_path) < 31000

    def test_same_fw_dir_still_deterministic_per_base(self, tmp_path, monkeypatch):
        from flashgate import bench_serve as bs
        monkeypatch.setenv("FLASHGATE_BENCH_LOCK_PORT_BASE", "30000")
        assert bs._lock_port(tmp_path) == bs._lock_port(tmp_path)

    def test_port_base_out_of_range_or_garbage_rejected(self, tmp_path,
                                                        monkeypatch):
        # without the range check a base like 70000 surfaces as a raw
        # OverflowError from bind() — not an OSError, so the honest
        # diagnostics never fire (mutation round, 2026-09-21)
        import pytest
        from flashgate import bench_serve as bs
        for bad in ("-1", "0", "1023", "63536", "70000", "abc"):
            monkeypatch.setenv("FLASHGATE_BENCH_LOCK_PORT_BASE", bad)
            with pytest.raises(ValueError, match="must be a port number"):
                bs._lock_port(tmp_path)


class TestStopChannel:
    def test_stop_signals_live_listener(self, tmp_path):
        from pathlib import Path as P
        from flashgate.bench_serve import (_start_lock_listener,
                                           acquire_bench_lock, stop_bench)
        signalled = []
        # interrupt is called with NO arguments (like _thread.interrupt_main)
        lock = acquire_bench_lock(tmp_path)
        try:
            thread = _start_lock_listener(
                lock, interrupt=lambda: signalled.append("FIRED"))
            assert stop_bench(tmp_path) is True
            thread.join(timeout=3)
            assert not thread.is_alive()
            assert signalled == ["FIRED"]   # the stop signal fired once
        finally:
            lock.close()

    def test_stop_without_holder_is_false(self, tmp_path):
        from flashgate.bench_serve import stop_bench
        (tmp_path / "free").mkdir(exist_ok=True)
        assert stop_bench(tmp_path / "free") is False

    def test_cli_stop_without_server_exit_2(self, tmp_path, monkeypatch, capsys):
        from flashgate import cli
        board = make_board(tmp_path)
        rc = cli.main(["--board", str(board.yaml_path), "bench-serve", "--stop"])
        assert rc == 2
        assert "no bench-serve" in capsys.readouterr().out


class TestCompletedEventWiring:
    def test_operation_done_noop_without_loop(self, tmp_path):
        board = make_board(tmp_path)
        bench = BenchDriver(board, verify_fn=lambda b, n: 0)
        drv = _build_driver(bench)(bench)
        drv._operation_done({"op_id": "x", "state": "succeeded"})  # no crash

    def test_loop_captured_on_first_rpc(self, tmp_path):
        board = make_board(tmp_path)
        bench = BenchDriver(board, verify_fn=lambda b, n: 0)
        drv = _build_driver(bench)(bench)
        assert drv._loop is None
        asyncio.run(drv.describe_bench())
        assert drv._loop is not None


class TestEventAndStopGuards:
    """Mutation P6/P8: the loop guard must actually skip scheduling, and
    stop_bench must not claim success without an ok ack."""

    def test_no_scheduling_without_loop(self, tmp_path, monkeypatch):
        board = make_board(tmp_path)
        bench = BenchDriver(board, verify_fn=lambda b, n: 0)
        drv = _build_driver(bench)(bench)
        calls = []
        monkeypatch.attr_target = None
        import flashgate.bench_serve as bs
        monkeypatch.setattr(
            bs.asyncio, "run_coroutine_threadsafe",
            lambda coro, loop: calls.append(loop))
        drv._operation_done({"op_id": "x", "state": "succeeded"})
        assert calls == []                 # loop None: nothing scheduled
        import threading
        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        try:
            drv._loop = loop               # as _remember_loop would
            drv._operation_done({"op_id": "y", "state": "failed"})
            assert len(calls) == 1 and calls[0] is loop
            loop.call_soon_threadsafe(loop.stop)
        finally:
            t.join(2)
            loop.close()

    def test_stop_bench_false_without_ok_ack(self, tmp_path):
        # an impostor ANSWERS, but not the protocol's ok — not signalled
        import socket as pysocket
        import threading
        from flashgate.bench_serve import _lock_port, stop_bench
        impostor = pysocket.socket()
        impostor.bind(("127.0.0.1", _lock_port(tmp_path)))
        impostor.listen(1)

        def answer_not_ok():
            try:
                conn, _ = impostor.accept()
                conn.recv(64)
                conn.sendall(b"nope\n")
                conn.close()
            except OSError:
                pass
        th = threading.Thread(target=answer_not_ok, daemon=True)
        th.start()
        try:
            assert stop_bench(tmp_path) is False
        finally:
            impostor.close()

    def test_stop_bench_draining_receipt_for_silent_holder(self, tmp_path):
        # a holder that connects-but-never-answers = the draining shape:
        # the receipt must say so, not claim "no bench-serve" (N0-3)
        import socket as pysocket
        from flashgate.bench_serve import _lock_port, stop_bench
        silent = pysocket.socket()
        silent.bind(("127.0.0.1", _lock_port(tmp_path)))
        silent.listen(1)
        try:
            assert stop_bench(tmp_path) == "draining"
        finally:
            silent.close()


class TestDrainBudget:
    """The --stop drain must outlast a verify QUEUED on the bench lock:
    a flat 120 s abandoned exactly that op (adversarial M2) — no terminal
    snapshot for a client told a verify was running."""

    def test_budget_covers_lock_wait_plus_verify(self, monkeypatch):
        from flashgate import bench_serve, verifylock
        monkeypatch.delenv("FLASHGATE_VERIFY_LOCK_WAIT", raising=False)
        assert bench_serve._drain_timeout_s() == \
            verifylock.DEFAULT_WAIT_S + bench_serve._VERIFY_BUDGET_S
        monkeypatch.setenv("FLASHGATE_VERIFY_LOCK_WAIT", "400")
        assert bench_serve._drain_timeout_s() == 880.0

    def test_drain_passes_scaled_budget_to_wait(self, monkeypatch, capsys):
        from flashgate import bench_serve
        seen = {}

        class FakeBench:
            def describe(self):
                return {"current_operation": "op-1"}

            def wait(self, op_id, timeout_s):
                seen["op"], seen["t"] = op_id, timeout_s
                return {"state": "succeeded", "exit_code": 0}
        monkeypatch.setenv("FLASHGATE_VERIFY_LOCK_WAIT", "400")
        bench_serve._drain_current(FakeBench())
        assert seen == {"op": "op-1", "t": 880.0}
        assert "drained: succeeded" in capsys.readouterr().out

    def test_drain_noop_without_current(self):
        from flashgate import bench_serve

        class FakeBench:
            def describe(self):
                return {}

            def wait(self, *a, **k):
                raise AssertionError("no wait without an in-flight op")
        bench_serve._drain_current(FakeBench())

    def test_drain_budget_exhaustion_says_so_honestly(self, monkeypatch, capsys):
        # a still-running op after the budget is NOT "drained" — the print
        # must not pretend it was (adversarial M2 residual)
        from flashgate import bench_serve

        class SlowBench:
            def describe(self):
                return {"current_operation": "op-slow"}

            def wait(self, op_id, timeout_s):
                return {"state": "running"}
        bench_serve._drain_current(SlowBench())
        out = capsys.readouterr().out
        assert "drain budget exhausted" in out
        assert "WITHOUT a terminal snapshot" in out
        assert "drained:" not in out

    def test_lock_socket_reuseaddr_is_platform_conditional(self, tmp_path):
        # POSIX: probe connections die into TIME_WAIT holding the lock port
        # as local port; without SO_REUSEADDR a quick re-acquire (bench
        # restart) hits EADDRINUSE (CI ubuntu leg, 2026-09-21). Windows:
        # SO_REUSEADDR would behave like SO_REUSEPORT and allow binding
        # over a LIVE listener — the lock would be void, so it must be OFF
        # there (caught locally, same day).
        import os as _os
        import socket
        from flashgate import bench_serve as bs
        lock = bs.acquire_bench_lock(tmp_path)
        try:
            got = lock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
            assert got == (0 if _os.name == "nt" else 1)
        finally:
            lock.close()


class TestN0DebtGuards:
    """2026-09-21 N0 debt-pack guards: socket-leak-free acquire, the
    draining receipt for a second --stop."""

    def test_bad_base_raises_before_any_socket_is_created(self, tmp_path,
                                                          monkeypatch):
        # the port is computed BEFORE the socket exists: a ValueError for
        # a bad base can no longer leak an unclosed socket (N0-1)
        import pytest
        from flashgate import bench_serve as bs
        made = []
        real_socket = bs.socket.socket

        class CountingSocket(real_socket):
            def __init__(self, *a, **kw):
                made.append(self)
                super().__init__(*a, **kw)
        monkeypatch.setattr(bs.socket, "socket", CountingSocket)
        monkeypatch.setenv("FLASHGATE_BENCH_LOCK_PORT_BASE", "abc")
        with pytest.raises(ValueError, match="must be a port number"):
            bs.acquire_bench_lock(tmp_path)
        assert not made, "no socket may exist when the base is invalid"

