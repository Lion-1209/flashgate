"""cli-level fail-closed guards: a check that cannot run must fail, never pass.

Regression coverage for the false-green found in 0.4.1: --all-probes with
no console serial in swd mode used to print a yellow "skipped" and return 0.
"""

from types import SimpleNamespace

import pytest
import serial

from flashgate import cli
from flashgate.board import Board, load_board


BODY = """
board: test-board
mcu: STM32F999
description: unit test fixture
firmware:
  dir: fw
  build: ninja -C build
  artifact: build/fw.bin
serial:
  baudrate: 9600
  banner: 'BOOT board={board} git={git}'
"""


def make_board(tmp_path) -> Board:
    fw = tmp_path / "fw"
    fw.mkdir(exist_ok=True)
    p = tmp_path / "board.yaml"
    p.write_text(BODY, encoding="utf-8")
    return load_board(p)


@pytest.fixture
def happy_swd(monkeypatch):
    """Everything up to (but excluding) the probe stage succeeds: build,
    flash, wipe, start, and a valid v1 signature answers immediately."""
    monkeypatch.setattr(cli, "_build", lambda board, j=None: cli.EXIT_OK)
    monkeypatch.setattr(cli.flasher, "flash",
                        lambda *a, **k: SimpleNamespace(ok=True, detail=""))
    monkeypatch.setattr(cli.flasher, "write32", lambda *a, **k: True)
    monkeypatch.setattr(cli.flasher, "start_app", lambda *a, **k: True)
    monkeypatch.setattr(cli.swdsig, "wait_for_signature",
                        lambda *a, **k: ({"version": 1, "flags": 0,
                                          "git": "x", "build": "y"}, ""))
    monkeypatch.setattr(Board, "head_sha", lambda self: None)


class TestSwdProbeGuard:
    def test_probes_required_no_serial_fails(self, tmp_path, happy_swd, monkeypatch):
        board = make_board(tmp_path)
        monkeypatch.setattr(cli, "_console_port", lambda b: (None, "no serial found"))
        assert cli._verify_swd(board, ["all"]) == cli.EXIT_ENV

    def test_probes_required_port_unopenable_fails(self, tmp_path, happy_swd, monkeypatch):
        board = make_board(tmp_path)
        monkeypatch.setattr(cli, "_console_port", lambda b: ("COM3", "hint"))

        def boom(*a, **k):
            raise serial.SerialException("access denied")
        monkeypatch.setattr(cli.serialmon, "open_flush", boom)
        assert cli._verify_swd(board, ["all"]) == cli.EXIT_ENV

    def test_probes_required_run_when_serial_ok(self, tmp_path, happy_swd, monkeypatch):
        board = make_board(tmp_path)
        monkeypatch.setattr(cli, "_console_port", lambda b: ("COM3", "hint"))
        monkeypatch.setattr(cli.serialmon, "open_flush",
                            lambda *a, **k: SimpleNamespace(close=lambda: None))
        seen = {}
        def run_probes(board, names, conn, j=None):
            seen["names"] = names
            return cli.EXIT_OK
        monkeypatch.setattr(cli, "_run_probes", run_probes)
        assert cli._verify_swd(board, ["all"]) == cli.EXIT_OK
        assert seen["names"] is None                # "all" expanded, not literal

    def test_no_probes_requested_still_passes_without_serial(self, tmp_path, happy_swd, monkeypatch):
        board = make_board(tmp_path)
        monkeypatch.setattr(cli, "_console_port", lambda b: (None, "no serial"))
        assert cli._verify_swd(board, None) == cli.EXIT_OK


class TestSignatureErrors:
    def test_layout_mismatch_fails_fast_as_env_error(self, tmp_path, happy_swd, monkeypatch):
        board = make_board(tmp_path)
        monkeypatch.setattr(
            cli.swdsig, "wait_for_signature",
            lambda *a, **k: (None, "signature layout version 3 not supported "
                                   "(this tool decodes version 1 only)"))
        assert cli._verify_swd(board, None) == cli.EXIT_ENV

    def test_plain_timeout_still_exit_3(self, tmp_path, happy_swd, monkeypatch):
        board = make_board(tmp_path)
        monkeypatch.setattr(cli.swdsig, "wait_for_signature",
                            lambda *a, **k: (None, "no valid signature yet"))
        assert cli._verify_swd(board, None) == cli.EXIT_BANNER_TIMEOUT


class TestBannerIdentity:
    def test_board_mismatch_detected(self, tmp_path, monkeypatch):
        board = make_board(tmp_path)
        monkeypatch.setattr(Board, "head_sha", lambda self: None)
        rc = cli._check_banner_identity(board, {"board": "other-board", "git": "x"})
        assert rc == cli.EXIT_SHA_MISMATCH

    def test_sha_mismatch_detected(self, tmp_path, monkeypatch):
        board = make_board(tmp_path)
        monkeypatch.setattr(Board, "head_sha", lambda self: "abc1234")
        rc = cli._check_banner_identity(board, {"board": "test-board", "git": "deadbee"})
        assert rc == cli.EXIT_SHA_MISMATCH

    def test_consistent_identity_passes(self, tmp_path, monkeypatch):
        board = make_board(tmp_path)
        monkeypatch.setattr(Board, "head_sha", lambda self: "abc1234")
        assert cli._check_banner_identity(
            board, {"board": "test-board", "git": "abc1234"}) is None


class TestAutoRouting:
    def test_auto_picks_swd_without_serial(self, tmp_path, monkeypatch):
        board = make_board(tmp_path)
        seen = {}
        monkeypatch.setattr(cli, "_console_port", lambda b: (None, "none"))
        def fake_swd(b, p, j=None):
            seen["mode"] = "swd"; return cli.EXIT_OK
        def fake_uart(b, p, j=None):
            seen["mode"] = "uart"; return cli.EXIT_OK
        monkeypatch.setattr(cli, "_verify_swd", fake_swd)
        monkeypatch.setattr(cli, "_verify_uart", fake_uart)
        assert cli.cmd_verify(board, None, None) == cli.EXIT_OK
        assert seen["mode"] == "swd"

    def test_auto_picks_uart_with_serial(self, tmp_path, monkeypatch):
        board = make_board(tmp_path)
        seen = {}
        monkeypatch.setattr(cli, "_console_port", lambda b: ("COM3", "hint"))
        def fake_swd(b, p, j=None):
            seen["mode"] = "swd"; return cli.EXIT_OK
        def fake_uart(b, p, j=None):
            seen["mode"] = "uart"; return cli.EXIT_OK
        monkeypatch.setattr(cli, "_verify_swd", fake_swd)
        monkeypatch.setattr(cli, "_verify_uart", fake_uart)
        assert cli.cmd_verify(board, None, None) == cli.EXIT_OK
        assert seen["mode"] == "uart"


class TestUartPassPath:
    def test_plain_uart_verify_passes_without_probe_flags(self, tmp_path, monkeypatch, capsys):
        # Regression from 0.4.2: the identity-check refactor left a NameError
        # on the uart PASS print (git={got} referenced a moved local) — verify
        # crashed AFTER everything had passed, exit 1 (misread as build fail).
        board = make_board(tmp_path)
        monkeypatch.setattr(cli, "_console_port",
                            lambda b: ("COM77", "pinned"))   # host serial topology must not matter
        monkeypatch.setattr(cli, "_build", lambda b, j=None: cli.EXIT_OK)
        monkeypatch.setattr(cli, "cmd_flash", lambda b: cli.EXIT_OK)
        monkeypatch.setattr(cli.serialmon, "open_flush",
                            lambda *a, **k: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(cli.serialmon, "wait_on",
                            lambda *a, **k: SimpleNamespace(
                                matched=True, error_hit=None, transcript="",
                                matched_line="BOOT board=test-board git=abc1234",
                                groups={"board": "test-board", "git": "abc1234"}))
        monkeypatch.setattr(Board, "head_sha", lambda self: "abc1234")
        assert cli._verify_uart(board, None) == cli.EXIT_OK
        assert "PASS" in capsys.readouterr().out


class TestVerifyRecords:
    """Stage 1: cmd_verify persists an evidence record on every run —
    failed runs included, with unexecuted steps marked skipped."""

    def test_failed_build_writes_record_with_skipped_tail(self, tmp_path, monkeypatch):
        board = make_board(tmp_path)
        monkeypatch.setattr(cli, "_build", lambda b, j=None: cli.EXIT_BUILD)
        monkeypatch.setattr(cli, "_console_port", lambda b: (None, "no serial"))
        assert cli.cmd_verify(board, None) == cli.EXIT_BUILD   # auto -> swd
        from flashgate import records
        rec = records.latest_record(board.firmware_dir)
        assert rec is not None, "a FAILED run must still leave a record"
        assert rec["run"]["exit_code"] == 1 and rec["run"]["status"] == "failed"
        by_name = {c["name"]: c for c in rec["checks"]}
        assert by_name["build"]["status"] == "failed"
        for later in ("flash", "boot", "identity"):
            assert by_name[later]["status"] == "skipped"
        assert rec["firmware"]["tree_fingerprint"]
        assert rec["tool"]["name"] == "flashgate"

    def test_passing_swd_run_records_full_chain(self, tmp_path, monkeypatch,
                                                happy_swd):
        board = make_board(tmp_path)
        # probes requested -> a console MUST exist, or the fail-closed
        # guard rightly fails the run (exit 6)
        monkeypatch.setattr(cli, "_console_port", lambda b: ("COMX", "stub"))
        monkeypatch.setattr(cli.serialmon, "open_flush",
                            lambda *a, **k: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(cli, "_run_probes",
                            lambda b, names, conn, j=None: cli.EXIT_OK)
        monkeypatch.setattr(
            cli.probe_mod, "load_probes", lambda p: {"led-demo": None})
        assert cli.cmd_verify(board, ["all"], "swd") == cli.EXIT_OK
        from flashgate import records
        rec = records.latest_record(board.firmware_dir)
        by_name = {c["name"]: c for c in rec["checks"]}
        for step in ("build", "flash", "boot", "identity", "probe:led-demo"):
            assert by_name[step]["status"] == "passed", step
        kinds = [e["kind"] for e in rec["evidence"]]
        assert "swd-signature" in kinds
        assert rec["run"]["status"] == "succeeded"

    def test_record_write_failure_never_changes_exit(self, tmp_path, monkeypatch,
                                                     happy_swd):
        board = make_board(tmp_path)
        monkeypatch.setattr(cli, "_console_port", lambda b: (None, "no serial"))
        from flashgate import records as rec_mod
        monkeypatch.setattr(rec_mod, "write_record",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        assert cli.cmd_verify(board, None) == cli.EXIT_OK   # record is auxiliary


class TestStrictEvidenceMode:
    def test_unknown_mode_rejected_at_load(self, tmp_path):
        body = BODY + "evidence:\n  mode: 'uat'\n"      # typo of 'uart'
        fw = tmp_path / "fw"
        fw.mkdir(exist_ok=True)
        p = tmp_path / "board.yaml"
        p.write_text(body, encoding="utf-8")
        from flashgate.board import BoardError
        with pytest.raises(BoardError):
            load_board(p)

    def test_valid_modes_load(self, tmp_path):
        from flashgate.board import BoardError
        for mode in ("auto", "uart", "swd", "AUTO"):
            body = BODY + f"evidence:\n  mode: '{mode}'\n"
            fw = tmp_path / "fw"
            fw.mkdir(exist_ok=True)
            p = tmp_path / f"b-{mode}.yaml"
            p.write_text(body, encoding="utf-8")
            try:
                b = load_board(p)
                assert b.evidence_mode == mode.lower()
            except BoardError:
                pytest.fail(f"mode {mode!r} must load")


class TestStrictBannerIdentity:
    def test_promised_field_missing_fails(self, tmp_path, monkeypatch):
        # Legacy regex with an OPTIONAL git group can match a banner that
        # omits git= — the old code silently skipped the sha comparison.
        board = make_board(tmp_path)
        monkeypatch.setattr(Board, "head_sha", lambda self: "abc1234")
        rc = cli._check_banner_identity(board, {"board": "test-board"})
        assert rc == cli.EXIT_SHA_MISMATCH

    def test_banner_without_git_promise_still_ok(self, tmp_path, monkeypatch):
        # A pattern that never promised `git` cannot be blamed for lacking it.
        board = make_board(tmp_path)
        board.banner_regex = r"BOOT board=(?P<board>\S+)"   # no git group
        monkeypatch.setattr(Board, "head_sha", lambda self: "abc1234")
        assert cli._check_banner_identity(board, {"board": "test-board"}) is None


class TestUartBuildFailRecord:
    """uart-side ensure_failed net (its swd twin is covered above)."""

    def test_uart_build_fail_marks_build_failed_not_skipped(self, tmp_path, monkeypatch):
        board = make_board(tmp_path)
        monkeypatch.setattr(cli, "_console_port", lambda b: ("COMX", "stub"))
        monkeypatch.setattr(cli.serialmon, "open_flush",
                            lambda *a, **k: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(cli, "_build", lambda b, j=None: cli.EXIT_BUILD)
        assert cli.cmd_verify(board, None, "uart") == cli.EXIT_BUILD
        from flashgate import records
        rec = records.latest_record(board.firmware_dir)
        by_name = {c["name"]: c for c in rec["checks"]}
        assert by_name["console"]["status"] == "passed"
        assert by_name["build"]["status"] == "failed"
        assert by_name["identity"]["status"] == "skipped"
