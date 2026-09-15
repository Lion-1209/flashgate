"""bench-serve front-end RPCs (L1, network-free): the four wrappers over
BenchDriver. The device-connect transport is NOT exercised here — these
pin the semantics the shell must not dilute (including the busy-as-
payload red line: transport success, structured busy error)."""

import asyncio

import pytest

pytest.importorskip("device_connect_edge",
                    reason="bench extra not installed")

from flashgate import records
from flashgate.bench import BenchDriver, DuplicateBenchError
from flashgate.bench_serve import _build_driver
from tests.test_cli import make_board


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
        import pytest
        from flashgate.bench_serve import acquire_bench_lock
        lock = acquire_bench_lock(tmp_path)
        try:
            with pytest.raises(RuntimeError, match="one bench-serve per bench"):
                acquire_bench_lock(tmp_path)
        finally:
            lock.close()
        # released: acquirable again
        again = acquire_bench_lock(tmp_path)
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
        try:
            rc = bs.serve(board)
            assert rc == 2
            assert "one bench-serve per bench" in capsys.readouterr().out
        finally:
            lock.close()


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
        import socket as pysocket
        from flashgate.bench_serve import _lock_port, stop_bench
        # an impostor holds the port but does not speak the protocol
        impostor = pysocket.socket()
        impostor.bind(("127.0.0.1", _lock_port(tmp_path)))
        impostor.listen(1)
        try:
            assert stop_bench(tmp_path) is False
        finally:
            impostor.close()
