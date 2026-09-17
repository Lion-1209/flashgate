"""Bench lock (Stop-hook stacking fix): concurrent verifies must wait for
each other instead of racing for the console serial port; a holder's death
must release the lock (OS-owned, never stale); a timed-out wait must fail
as a truthful env error, never a silent pass.
"""

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from flashgate import verifylock

REPO_ROOT = Path(verifylock.__file__).resolve().parent.parent

# Holder runs as a REAL separate process: the lock must work across
# processes (two hook levels = two `flashgate verify` processes), and the
# hard-exit variant proves the OS — not our code — reclaims the lock.
_HOLDER = """
import sys, time
sys.path.insert(0, {repo!r})
from pathlib import Path
from flashgate import verifylock
with verifylock.verify_lock(Path({fw!r}), wait_s=1):
    print("held", flush=True)
    time.sleep({hold})
    sys.stdout.flush()
{tail}
"""

_HARD_EXIT = "import os; os._exit(1)      # die while holding: no unlock runs"


def _spawn_holder(fw: Path, hold: float, hard_exit: bool = False):
    script = _HOLDER.format(repo=str(REPO_ROOT), fw=str(fw), hold=hold,
                            tail=_HARD_EXIT if hard_exit else "")
    return subprocess.Popen([sys.executable, "-c", script],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)


class TestVerifyLock:
    def test_mutual_exclusion_times_out(self, tmp_path):
        fw = tmp_path / "fw"
        fw.mkdir()
        proc = _spawn_holder(fw, hold=2.0)
        try:
            # wait for the holder to actually take the lock
            assert proc.stdout.readline().strip() == "held"
            with pytest.raises(verifylock.VerifyLockTimeout):
                with verifylock.verify_lock(fw, wait_s=0.3):
                    pass
        finally:
            proc.kill()
            proc.wait()

    def test_wait_succeeds_after_holder_releases(self, tmp_path):
        fw = tmp_path / "fw"
        fw.mkdir()
        proc = _spawn_holder(fw, hold=0.5)
        assert proc.stdout.readline().strip() == "held"
        t0 = time.monotonic()
        with verifylock.verify_lock(fw, wait_s=10.0):
            elapsed = time.monotonic() - t0
        assert elapsed < 10.0            # we waited for the holder, then ran
        assert proc.wait(timeout=5) == 0

    def test_holder_death_releases_lock(self, tmp_path):
        fw = tmp_path / "fw"
        fw.mkdir()
        proc = _spawn_holder(fw, hold=0.2, hard_exit=True)
        assert proc.stdout.readline().strip() == "held"
        proc.wait()                       # exited WITHOUT unlocking
        t0 = time.monotonic()
        with verifylock.verify_lock(fw, wait_s=5.0):
            elapsed = time.monotonic() - t0
        assert elapsed < 5.0              # no stale lock survived the death

    def test_same_process_second_handle_blocks(self, tmp_path):
        fw = tmp_path / "fw"
        fw.mkdir()
        with verifylock.verify_lock(fw, wait_s=1.0):
            got = []

            def contender():
                try:
                    with verifylock.verify_lock(fw, wait_s=0.2):
                        got.append(True)
                except verifylock.VerifyLockTimeout:
                    got.append(False)

            t = threading.Thread(target=contender)
            t.start()
            t.join()
        assert got == [False]             # MCP-style concurrent in-process
                                          # verifies serialize too

    def test_lock_file_lives_in_state_dir(self, tmp_path):
        fw = tmp_path / "fw"
        fw.mkdir()
        with verifylock.verify_lock(fw, wait_s=1.0):
            p = verifylock.lock_path(fw)
            assert p.is_file()
            assert p.parent.name == ".flashgate"


class TestCmdVerifyLockIntegration:
    def test_contended_verify_fails_env_with_truthful_record(
            self, tmp_path, monkeypatch):
        from tests.test_cli import make_board
        from flashgate import cli, records

        board = make_board(tmp_path)
        proc = _spawn_holder(board.firmware_dir, hold=1.5)
        try:
            assert proc.stdout.readline().strip() == "held"
            monkeypatch.setenv("FLASHGATE_VERIFY_LOCK_WAIT", "0.2")
            rc = cli.cmd_verify(board, None, "swd")
            assert rc == cli.EXIT_ENV
            rec = records.latest_record(board.firmware_dir)
            assert rec["run"]["exit_code"] == 6
            assert rec["run"]["mode"] == "bench-busy"
            by_name = {c["name"]: c for c in rec["checks"]}
            assert by_name["verify"]["status"] == "skipped"
            assert "bench lock" in by_name["verify"]["detail"]
            # forensics: the record names the tree the verdict would have
            # applied to, even though no check ran
            assert rec["firmware"]["tree_fingerprint"]
        finally:
            proc.kill()
            proc.wait()

    def test_uncontended_verify_unaffected(self, tmp_path, monkeypatch):
        from tests.test_cli import make_board
        from flashgate import cli, records

        board = make_board(tmp_path)
        monkeypatch.setattr(cli, "_build", lambda b, j=None: cli.EXIT_OK)
        monkeypatch.setattr(cli.flasher, "flash",
                            lambda *a, **k: type("R", (), {"ok": True, "detail": ""})())
        monkeypatch.setattr(cli.flasher, "write32", lambda *a, **k: True)
        monkeypatch.setattr(cli.flasher, "start_app", lambda *a, **k: True)
        monkeypatch.setattr(
            cli.swdsig, "wait_for_signature",
            lambda *a, **k: ({"version": 1, "flags": 0, "git": "x",
                              "build": "y"}, ""))
        from flashgate.board import Board
        monkeypatch.setattr(Board, "head_sha", lambda self: None)
        assert cli.cmd_verify(board, None, "swd") == cli.EXIT_OK
        assert verifylock.lock_path(board.firmware_dir).is_file()

    def test_invalid_env_wait_falls_back_to_default(self, monkeypatch, capsys):
        from flashgate import cli
        for bad in ("soon", "-5", "nan", "inf", "1e999"):
            monkeypatch.setenv("FLASHGATE_VERIFY_LOCK_WAIT", bad)
            assert cli._verify_lock_wait() == verifylock.DEFAULT_WAIT_S, bad
            assert "invalid FLASHGATE_VERIFY_LOCK_WAIT" in capsys.readouterr().out

    def test_lock_setup_failure_is_env_error_not_mislabeled(
            self, tmp_path, monkeypatch, capsys):
        """OSError from lock SETUP gets the lock message; a console-scan
        OSError during mode resolution must NOT be relabeled as one."""
        from tests.test_cli import make_board
        from flashgate import cli

        board = make_board(tmp_path)

        def refuse_acquire(self, wait_s=None):
            raise OSError(13, "permission denied")
        monkeypatch.setattr(verifylock.BenchLock, "acquire", refuse_acquire)
        assert cli.cmd_verify(board, None, "swd") == cli.EXIT_ENV
        out = capsys.readouterr().out
        assert "cannot set up the bench lock" in out

    def test_auto_mode_console_scan_failure_falls_back_to_swd(
            self, tmp_path, monkeypatch, capsys):
        from tests.test_cli import make_board
        from flashgate import cli

        board = make_board(tmp_path)

        def boom(b):
            raise OSError(13, "access denied scanning serial ports")
        monkeypatch.setattr(cli, "_console_port", boom)
        seen = {}
        monkeypatch.setattr(cli, "_verify_swd",
                            lambda b, p, j=None: seen.setdefault("m", 1))
        assert cli.cmd_verify(board, None, None) == 1   # auto -> swd path ran
        assert "falls back to swd" in capsys.readouterr().out
