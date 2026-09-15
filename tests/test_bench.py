"""Bench driver core (L0): operation state machine, single-flight,
cancellation semantics, record association — all injectable, no network,
no hardware.

The device-connect front-end (L1) is a shell over BenchDriver; these
tests pin the semantics that shell must not dilute.
"""

import threading

import pytest

from flashgate import records
from flashgate.bench import BenchBusyError, BenchDriver
from tests.test_cli import make_board


def _write_record_for(board, exit_code=0):
    j = records.VerifyJournal(["build"], mode="swd", probe_names=None)
    j.check("build", "passed" if exit_code == 0 else "failed")
    rec = j.to_record(board, exit_code, records.status_word(exit_code),
                      "stubbed run")
    records.write_record(rec, board.firmware_dir, fingerprint="dd" * 32)
    return rec


class TestDescribe:
    def test_describe_is_side_effect_free(self, tmp_path):
        board = make_board(tmp_path)
        d = BenchDriver(board, verify_fn=lambda b, n: 0)
        info = d.describe()
        assert info["bench"] == "test-board"
        assert info["flashgate"]
        assert info["busy"] is False
        assert info["current_operation"] is None
        assert "verify" in info["capabilities"]
        # repeated describe must not mutate anything
        assert d.describe() == info


class TestOperationLifecycle:
    def test_success_path(self, tmp_path):
        board = make_board(tmp_path)
        d = BenchDriver(board, verify_fn=lambda b, n: 0)
        op = d.start_verify()
        # (state may already be terminal for an instant verify — the
        # "running" observation is pinned deterministically in
        # TestSingleFlight with a blocking executor)
        done = d.wait(op["op_id"], timeout_s=5)
        assert done["state"] == "succeeded"
        assert done["exit_code"] == 0
        assert done["status"] == "succeeded"
        assert done["finished_at"] and done["started_at"]

    def test_failure_path_exit_7(self, tmp_path):
        board = make_board(tmp_path)
        d = BenchDriver(board, verify_fn=lambda b, n: 7)
        op = d.start_verify()
        done = d.wait(op["op_id"], timeout_s=5)
        assert done["state"] == "failed"
        assert done["exit_code"] == 7 and done["status"] == "failed"

    def test_executor_crash_is_a_failed_operation(self, tmp_path):
        board = make_board(tmp_path)

        def boom(b, n):
            raise RuntimeError("bench infra bug")

        d = BenchDriver(board, verify_fn=boom)
        op = d.start_verify()
        done = d.wait(op["op_id"], timeout_s=5)
        assert done["state"] == "failed"
        assert "RuntimeError" in done["error"]
        assert done["exit_code"] is None

    def test_record_attached_from_this_run(self, tmp_path):
        board = make_board(tmp_path)

        def fake(b, n):
            _write_record_for(b, exit_code=0)
            return 0

        d = BenchDriver(board, verify_fn=fake)
        done = d.wait(d.start_verify()["op_id"], timeout_s=5)
        assert done["record"] is not None
        assert done["record"]["run"]["exit_code"] == 0
        assert done["summary"] == "stubbed run"

    def test_no_stale_record_attached(self, tmp_path):
        # F3 lesson: a run that wrote no record must NOT inherit an older
        # run's record sitting on disk.
        board = make_board(tmp_path)
        _write_record_for(board, exit_code=0)      # stale, from "before"
        records.LAST = None
        d = BenchDriver(board, verify_fn=lambda b, n: 7)
        done = d.wait(d.start_verify()["op_id"], timeout_s=5)
        assert done["state"] == "failed"
        assert done["record"] is None

    def test_operation_ids_unique(self, tmp_path):
        board = make_board(tmp_path)
        d = BenchDriver(board, verify_fn=lambda b, n: 0)
        ids = {d.wait(d.start_verify()["op_id"], 5)["op_id"]
               for _ in range(3)}
        assert len(ids) == 3

    def test_unknown_operation(self, tmp_path):
        board = make_board(tmp_path)
        d = BenchDriver(board, verify_fn=lambda b, n: 0)
        assert d.get_operation("op-nope") is None
        assert d.wait("op-nope", 1) is None
        assert d.cancel_operation("op-nope") is False


class TestSingleFlight:
    def test_second_start_while_executing_is_busy(self, tmp_path):
        board = make_board(tmp_path)
        release = threading.Event()

        def blocker(b, n):
            release.wait(10)
            return 0

        d = BenchDriver(board, verify_fn=blocker)
        first = d.start_verify()
        with pytest.raises(BenchBusyError) as ei:
            d.start_verify()
        assert first["op_id"] in str(ei.value)
        assert d.describe()["busy"] is True
        assert d.describe()["current_operation"] == first["op_id"]
        release.set()
        assert d.wait(first["op_id"], 10)["state"] == "succeeded"
        # bench is free again
        assert d.describe()["busy"] is False
        second = d.start_verify()
        assert d.wait(second["op_id"], 10)["state"] == "succeeded"


class TestCancel:
    def test_cancel_while_executing_is_advisory(self, tmp_path):
        board = make_board(tmp_path)
        release = threading.Event()

        def blocker(b, n):
            release.wait(10)
            _write_record_for(b, exit_code=0)
            return 0

        d = BenchDriver(board, verify_fn=blocker)
        op = d.start_verify()
        assert d.cancel_operation(op["op_id"]) is False   # executing: no
        done = d.get_operation(op["op_id"])
        assert done["cancel_requested"] is True           # ...but recorded
        release.set()
        done = d.wait(op["op_id"], 10)
        # hardware was never torn down: the run completes with its REAL verdict
        assert done["state"] == "succeeded"
        assert done["cancel_requested"] is True

    def test_cancel_after_terminal_is_false(self, tmp_path):
        board = make_board(tmp_path)
        d = BenchDriver(board, verify_fn=lambda b, n: 0)
        done = d.wait(d.start_verify()["op_id"], 5)
        assert d.cancel_operation(done["op_id"]) is False

    def test_cancel_before_execution_cancels(self, tmp_path):
        board = make_board(tmp_path)
        d = BenchDriver(board, verify_fn=lambda b, n: 0)
        # The pre-start window (operation created, worker not yet running)
        # exercised directly on the internal state machine: the window is
        # microseconds wide in real flow, its SEMANTICS are what matters.
        from flashgate.bench import _Operation
        with d._lock:
            op = _Operation(op_id="op-manual-001", created_at="now")
            d._ops[op.op_id] = op
            d._current = op
        assert d.cancel_operation("op-manual-001") is True
        assert d.get_operation("op-manual-001")["state"] == "cancelled"
        # a cancelled op never runs and the bench accepts a new verify
        done = d.wait(d.start_verify()["op_id"], 5)
        assert done["state"] == "succeeded"


class TestSameTickStaleRecord:
    """Runtime-audit FINDING-1: Windows time.monotonic() ticks at 15.6 ms;
    the old `at >= _t0` compare let a same-tick stale record attach to a
    run that wrote nothing. Identity against the pre-run snapshot is
    immune — deterministic on every platform and clock."""

    def test_immediate_retry_after_write_does_not_inherit(self, tmp_path):
        board = make_board(tmp_path)
        calls = {"n": 0}

        def first_writes_then_crashes(b, n):
            calls["n"] += 1
            if calls["n"] == 1:
                _write_record_for(b, exit_code=0)
                return 0
            raise RuntimeError("infra bug before any record")

        d = BenchDriver(board, verify_fn=first_writes_then_crashes)
        first = d.wait(d.start_verify()["op_id"], 5)
        assert first["record"] is not None
        # no sleep: the second op's snapshot may land in the SAME tick
        second = d.wait(d.start_verify()["op_id"], 5)
        assert second["state"] == "failed"
        assert second["record"] is None, \
            "a run that wrote nothing must not inherit the previous green"
        assert "previous" not in (second["summary"] or "")

    def test_second_driver_same_firmware_dir_rejected(self, tmp_path):
        # Adversarial F1: two drivers on one fw_dir would interleave on
        # the board and could cross-attach green evidence — banned at
        # construction.
        import pytest
        from flashgate.bench import DuplicateBenchError
        board = make_board(tmp_path)
        BenchDriver(board, verify_fn=lambda b, n: 0)
        with pytest.raises(DuplicateBenchError):
            BenchDriver(board, verify_fn=lambda b, n: 0)

    def test_two_benches_distinct_dirs_independent_ids(self, tmp_path):
        (tmp_path / "x").mkdir()
        (tmp_path / "y").mkdir()
        bx = make_board(tmp_path / "x")
        by = make_board(tmp_path / "y")

        def make(board_obj, rc):
            def fn(b, n):
                _write_record_for(b, exit_code=rc)
                return rc
            return fn

        dx = BenchDriver(bx, verify_fn=make(bx, 0))
        dy = BenchDriver(by, verify_fn=make(by, 7))
        fx = dx.wait(dx.start_verify()["op_id"], 5)
        fy = dy.wait(dy.start_verify()["op_id"], 5)
        assert fx["record"]["run"]["exit_code"] == 0
        assert fy["record"]["run"]["exit_code"] == 7
        assert fx["op_id"] != fy["op_id"]      # F5: process-wide sequence


class TestRecordSourceAndIsolation:
    """Mutation-round N3/N7: the fw_dir guard is load-bearing (a record
    written by ANOTHER bench in the same process must not attach), and
    operation snapshots must be copies, not live state."""

    def test_record_from_other_firmware_dir_not_attached(self, tmp_path):
        (tmp_path / "b").mkdir()
        (tmp_path / "a").mkdir()
        board_b = make_board(tmp_path / "b")
        board_a = make_board(tmp_path / "a")
        release = threading.Event()

        def blocking_no_write(b, n):
            release.wait(10)
            return 7

        d_b = BenchDriver(board_b, verify_fn=blocking_no_write)
        op = d_b.start_verify()               # snapshots _pre_last NOW

        # while b's op executes, a DIFFERENT bench writes a record
        def writes_a(b, n):
            _write_record_for(b, exit_code=0)
            return 0

        d_a = BenchDriver(board_a, verify_fn=writes_a)
        d_a.wait(d_a.start_verify()["op_id"], 5)
        assert records.LAST is not None       # registry holds A's record

        release.set()
        done = d_b.wait(op["op_id"], 10)
        assert done["state"] == "failed"
        assert done["record"] is None, \
            "a record from another firmware dir must not attach"

    def test_snapshots_are_copies_not_live_state(self, tmp_path):
        board = make_board(tmp_path)
        release = threading.Event()

        def blocker(b, n):
            release.wait(10)
            return 0

        d = BenchDriver(board, verify_fn=blocker)
        op_id = d.start_verify()["op_id"]
        view1 = d.get_operation(op_id)
        view1["state"] = "hacked"             # caller mutates its view
        view1["record"] = {"forged": True}
        view2 = d.get_operation(op_id)
        assert view2["state"] == "running"    # internal state untouched
        assert view2["record"] is None
        release.set()
        assert d.wait(op_id, 10)["state"] == "succeeded"


class TestHardening:
    """Adversarial-round F2/F3/F8 guards."""

    def test_systemexit_in_executor_does_not_wedge_bench(self, tmp_path):
        board = make_board(tmp_path)
        calls = {"n": 0}

        def quit_once(b, n):
            calls["n"] += 1
            if calls["n"] == 1:
                raise SystemExit(2)
            return 0

        d = BenchDriver(board, verify_fn=quit_once)
        done = d.wait(d.start_verify()["op_id"], 5)
        assert done["state"] == "failed"
        assert "SystemExit" in done["error"]
        # not wedged: the bench ACCEPTS a new verify which then succeeds
        done2 = d.wait(d.start_verify()["op_id"], 5)
        assert done2["state"] == "succeeded"

    def test_nested_record_mutation_isolated(self, tmp_path):
        board = make_board(tmp_path)

        def writes(b, n):
            _write_record_for(b, exit_code=0)
            return 0

        d = BenchDriver(board, verify_fn=writes)
        done = d.wait(d.start_verify()["op_id"], 5)
        assert done["record"] is not None
        done["record"]["run"]["exit_code"] = 99        # nested mutation
        again = d.get_operation(done["op_id"])
        assert again["record"]["run"]["exit_code"] == 0
        import flashgate.records as rec_mod
        assert rec_mod.LAST["record"]["run"]["exit_code"] == 0

    def test_describe_reports_probes_error(self, tmp_path):
        from flashgate.board import BoardError
        board = make_board(tmp_path)
        import flashgate.bench as bench_mod

        def broken_load(path):
            raise RuntimeError("bad probes yaml")

        orig = bench_mod.probe_mod.load_probes
        bench_mod.probe_mod.load_probes = broken_load
        try:
            info = BenchDriver(board, verify_fn=lambda b, n: 0).describe()
        finally:
            bench_mod.probe_mod.load_probes = orig
        assert info["probes"] == []
        assert "bad probes yaml" in info["probes_error"]


class TestCompletionCallback:
    def test_notified_on_success_and_failure(self, tmp_path):
        board = make_board(tmp_path)
        events = []
        d = BenchDriver(board, verify_fn=lambda b, n: len(events))
        d.set_on_complete(lambda snap: events.append((snap["op_id"], snap["state"])))
        d.wait(d.start_verify()["op_id"], 5)
        d.wait(d.start_verify()["op_id"], 5)
        assert [s for _, s in events] == ["succeeded", "failed"]

    def test_notified_on_pre_start_cancel(self, tmp_path):
        from flashgate.bench import _Operation
        board = make_board(tmp_path)
        events = []
        d = BenchDriver(board, verify_fn=lambda b, n: 0)
        d.set_on_complete(lambda snap: events.append(snap["state"]))
        with d._lock:
            op = _Operation(op_id="op-x", created_at="now")
            d._ops[op.op_id] = op
            d._current = op
        assert d.cancel_operation("op-x") is True
        assert events == ["cancelled"]

    def test_callback_exception_swallowed(self, tmp_path):
        board = make_board(tmp_path)

        def boom(snap):
            raise RuntimeError("callback bug")

        d = BenchDriver(board, verify_fn=lambda b, n: 0)
        d.set_on_complete(boom)
        done = d.wait(d.start_verify()["op_id"], 5)
        assert done["state"] == "succeeded"          # bench unaffected


class TestCallbackGuard:
    """Mutation P5: a callback exception must not even surface as an
    unhandled-thread-exception warning."""

    @pytest.mark.filterwarnings(
        "error::pytest.PytestUnhandledThreadExceptionWarning")
    def test_callback_exception_no_thread_warning(self, tmp_path):
        board = make_board(tmp_path)

        def boom(snap):
            raise RuntimeError("callback bug")

        d = BenchDriver(board, verify_fn=lambda b, n: 0)
        d.set_on_complete(boom)
        assert d.wait(d.start_verify()["op_id"], 5)["state"] == "succeeded"
