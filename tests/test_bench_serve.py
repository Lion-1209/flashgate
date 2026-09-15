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
