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
    monkeypatch.setattr(cli, "_build", lambda board: cli.EXIT_OK)
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
        def run_probes(board, names, conn):
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
        def fake_swd(b, p):
            seen["mode"] = "swd"; return cli.EXIT_OK
        def fake_uart(b, p):
            seen["mode"] = "uart"; return cli.EXIT_OK
        monkeypatch.setattr(cli, "_verify_swd", fake_swd)
        monkeypatch.setattr(cli, "_verify_uart", fake_uart)
        assert cli.cmd_verify(board, None, None) == cli.EXIT_OK
        assert seen["mode"] == "swd"

    def test_auto_picks_uart_with_serial(self, tmp_path, monkeypatch):
        board = make_board(tmp_path)
        seen = {}
        monkeypatch.setattr(cli, "_console_port", lambda b: ("COM3", "hint"))
        def fake_swd(b, p):
            seen["mode"] = "swd"; return cli.EXIT_OK
        def fake_uart(b, p):
            seen["mode"] = "uart"; return cli.EXIT_OK
        monkeypatch.setattr(cli, "_verify_swd", fake_swd)
        monkeypatch.setattr(cli, "_verify_uart", fake_uart)
        assert cli.cmd_verify(board, None, None) == cli.EXIT_OK
        assert seen["mode"] == "uart"


class TestEnvelopeCoupling:
    def test_exit_env_maps_to_incomplete_never_succeeded(self):
        # Belt-and-suspenders (mutation finding M5): the constant every
        # fail-closed guard asserts must map to "incomplete" in the MCP
        # envelope. If the mapping ever flips to "succeeded", a check that
        # could not run would count as a pass.
        from flashgate import results
        assert results.from_exit(cli.EXIT_ENV, "x").status == "incomplete"
        assert results.from_exit(cli.EXIT_PROBE_FAIL, "x").status == "failed"
