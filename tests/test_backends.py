"""Debug-backend adapters (Phase 2): registry, targeting, memory-parse,
profile selection, and the read_fn injection that decouples swdsig from
CubeProgrammer."""

import pytest

from flashgate import backends


class TestRegistry:
    def test_known_and_default(self):
        assert backends.known_backends() == ["cubeprogrammer", "fake", "openocd"]
        assert backends.get_backend("cubeprogrammer").name == "cubeprogrammer"

    def test_unknown_rejected(self):
        with pytest.raises(ValueError, match="unknown debug backend"):
            backends.get_backend("jlink-someday")


class TestTargetMapping:
    def test_mcu_prefix_map(self):
        assert backends.openocd_target_for("STM32H743IIT6") == "stm32h7x"
        assert backends.openocd_target_for("stm32f407") == "stm32f4x"
        assert backends.openocd_target_for("LPC55S69") is None


class TestReadMemParsing:
    def test_mdw_words_without_0x_prefix(self, monkeypatch):
        b = backends.OpenOcdBackend(mcu="STM32H743IIT6")
        out = ("Info : blah\n"
               "0x2001ff00: f1a5c0de 01000100 37323463 \n"
               "0x2001ff10: 2d656337 \n"
               "shutdown command invoked\n")
        monkeypatch.setattr(b, "_run", lambda cmds: (0, out))
        blob = b.read_mem("x", 0x2001FF00, 8)
        assert blob == bytes.fromhex("dec0a5f1" "00010001")

    def test_failed_run_returns_none(self, monkeypatch):
        b = backends.OpenOcdBackend(mcu="STM32H743IIT6")
        monkeypatch.setattr(b, "_run", lambda cmds: (1, "boom"))
        assert b.read_mem("x", 0, 64) is None

    def test_unparsed_output_returns_none(self, monkeypatch):
        b = backends.OpenOcdBackend(mcu="STM32H743IIT6")
        monkeypatch.setattr(b, "_run", lambda cmds: (0, "no memory lines"))
        assert b.read_mem("x", 0, 64) is None

    def test_no_target_mapping_reports_clearly(self):
        b = backends.OpenOcdBackend(mcu="LPC55S69")
        rc, out = b._run(["init"])
        assert rc == -1 and "openocd_target" in out


class TestSwdsigReadInjection:
    def test_wait_uses_injected_reader(self):
        from flashgate import swdsig
        import struct
        import zlib
        calls = []

        # valid v1 signature: magic+ver+flags(8) | git@0x08(16) |
        # build@0x18(24) | crc32 of first 0x30 bytes @0x30
        buf = bytearray(64)
        struct.pack_into("<IHH", buf, 0, swdsig.SIG_MAGIC, 1, 0)
        buf[0x08:0x0F] = b"c4277ce"
        buf[0x18:0x2C] = b"2026-01-01T00:00:00Z"
        struct.pack_into("<I", buf, 0x30,
                         zlib.crc32(bytes(buf[:0x30])) & 0xFFFFFFFF)
        blob = bytes(buf)

        def fake_read(connect, address, size):
            calls.append((address, size))
            return blob[:size]

        info, err = swdsig.wait_for_signature(
            "c", 0x2001FF00, 64, timeout_s=1.0, read_fn=fake_read)
        assert calls, "injected reader must be used"
        assert info is not None and info["version"] == 1


class TestFakeBackend:
    def test_scripts_the_pipeline(self):
        b = backends.FakeBackend(flash_ok=False)
        assert b.flash("x", "c", "0x08000000").ok is False
        assert b.write32("c", 0, 0x2001FF00) is True
        assert b.read_mem("c", 0, 64) is None
        assert b.calls[0].startswith("flash:")


class TestFakeBackendPipeline:
    """The design doc §14 simulated-integration list, driven end-to-end
    through cmd_verify with a scripted fake backend: flash failure,
    signature timeout, stale identity, happy path — no probe attached."""

    @pytest.fixture(autouse=True)
    def _allow_fake(self, monkeypatch):
        monkeypatch.setenv("FLASHGATE_ALLOW_FAKE", "1")

    @staticmethod
    def _board(tmp_path, extra=""):
        from flashgate.board import load_board
        fw = tmp_path / "fw"
        fw.mkdir(exist_ok=True)
        p = tmp_path / "b.yaml"
        p.write_text(
            "board: t\nmcu: STM32H743\nfirmware:\n  dir: fw\n"
            "  build: ninja\n  artifact: fw.bin\n"
            f"flash:\n  adapter: fake\n{extra}"
            "evidence:\n  mode: swd\n"
            "coverage:\n  notes: [chain intact]\n"
            "serial:\n  banner: 'BOOT {git}'\n"
            "  banner_timeout_s: 0.3\n", encoding="utf-8")
        (fw / "fw.bin").write_bytes(b"\x00" * 16)
        return load_board(p)

    @staticmethod
    def _sig(git: bytes = b"aaaaaaa"):
        import struct
        import zlib
        from flashgate import swdsig as sg
        buf = bytearray(64)
        struct.pack_into("<IHH", buf, 0, sg.SIG_MAGIC, 1, 0)
        buf[0x08:0x08 + len(git)] = git
        struct.pack_into("<I", buf, 0x30,
                         zlib.crc32(bytes(buf[:0x30])) & 0xFFFFFFFF)
        return bytes(buf)

    def _run(self, tmp_path, fake, monkeypatch, extra=""):
        from flashgate import cli
        board = self._board(tmp_path, extra)
        monkeypatch.setattr(cli, "_build", lambda b, j=None: cli.EXIT_OK)
        monkeypatch.setattr(cli, "_board_backend", lambda b: fake)
        return cli.cmd_verify(board, None)

    def test_flash_failure_exit_2(self, tmp_path, monkeypatch):
        from flashgate import backends, cli
        rc = self._run(tmp_path, backends.FakeBackend(flash_ok=False),
                       monkeypatch)
        assert rc == cli.EXIT_FLASH

    def test_signature_timeout_exit_3(self, tmp_path, monkeypatch):
        from flashgate import backends, cli
        rc = self._run(tmp_path, backends.FakeBackend(signature=None),
                       monkeypatch)
        assert rc == cli.EXIT_BANNER_TIMEOUT

    def test_stale_identity_exit_5(self, tmp_path, monkeypatch):
        from flashgate import backends, cli
        from flashgate.board import Board
        fake = backends.FakeBackend(signature=self._sig(b"bbbbbbb"))
        monkeypatch.setattr(Board, "head_sha",
                            lambda self: "aaaaaaa-dirty")
        rc = self._run(tmp_path, fake, monkeypatch)
        assert rc == cli.EXIT_SHA_MISMATCH

    def test_happy_path_exit_0(self, tmp_path, monkeypatch):
        from flashgate import backends, cli
        from flashgate.board import Board
        fake = backends.FakeBackend(
            signature=self._sig(b"aaaaaaa-dirty"))   # matches head_sha
        monkeypatch.setattr(Board, "head_sha", lambda self: "aaaaaaa-dirty")
        rc = self._run(tmp_path, fake, monkeypatch)
        assert rc == cli.EXIT_OK

    def test_record_carries_coverage_block(self, tmp_path, monkeypatch):
        # N1: the record's own boundary statement — verified lists what
        # held, not_verified carries the standing blind spots
        from flashgate import backends, cli, records
        from flashgate.board import Board
        fake = backends.FakeBackend(
            signature=self._sig(b"aaaaaaa-dirty"))
        monkeypatch.setattr(Board, "head_sha", lambda self: "aaaaaaa-dirty")
        self._run(tmp_path, fake, monkeypatch)
        rec = records.latest_record(self._board(tmp_path).firmware_dir)
        cov = rec["coverage"]
        assert cov["verified"] and "boot" in cov["verified"]
        assert any("physical" in x for x in cov["not_verified"])
        assert "statement" in cov and "profile_notes" in cov
        # the REAL loader chain (mutation gap E, adversarial H1): the
        # note must ARRIVE in the record — key-presence alone lets the
        # board->record half break silently
        assert cov["profile_notes"] == ["chain intact"]

    def test_wipe_lie_caught_by_readback_exit_5(self, tmp_path, monkeypatch):
        # L1: the backend reports a successful wipe but the memory still
        # holds the old magic — the readback must catch the silent lie.
        from flashgate import backends, cli
        fake = backends.FakeBackend(signature=self._sig(b"aaaaaaa-dirty"),
                                    wipe_lies=True)
        rc = self._run(tmp_path, fake, monkeypatch)
        assert rc == cli.EXIT_SHA_MISMATCH
        from flashgate import records
        rec = records.latest_record(self._board(tmp_path).firmware_dir)
        by_name = {c["name"]: c for c in rec["checks"]}
        assert by_name["flash"]["status"] == "failed"
        assert "readback" in by_name["flash"]["detail"]
        assert any(c.startswith("read:") for c in fake.calls)

    def test_wipe_readback_unreadable_is_env_failure(self, tmp_path,
                                                     monkeypatch):
        # readback returning NO DATA at all is an environment failure
        # (exit 6, "could not run"), not an identity verdict — the tool
        # answered nothing (N0 follow-up, adversarial M1 semantic axis)
        from flashgate import backends, cli
        fake = backends.FakeBackend(signature=None, wipe_lies=True)
        rc = self._run(tmp_path, fake, monkeypatch)
        assert rc == cli.EXIT_ENV

    def test_wipe_readback_tool_error_is_env_failure(self, tmp_path,
                                                     monkeypatch):
        # SwdError from the read tool (CLI missing / read failed) is an
        # environment failure too — all three SwdError raise sites are
        # "could not run", none is an identity verdict (adversarial M1)
        from flashgate import backends, cli, swdsig

        class NoTool(backends.FakeBackend):
            def read_mem(self, connect, address, size):
                if size == 4:
                    raise swdsig.SwdError("CubeProgrammer read failed")
                return super().read_mem(connect, address, size)
        fake = NoTool(signature=self._sig(b"aaaaaaa-dirty"))
        rc = self._run(tmp_path, fake, monkeypatch)
        assert rc == cli.EXIT_ENV
        # record-level anchors are the load-bearing assertions: the
        # crash-net also yields exit 6, only the named check tells the
        # explicit branch from an aborted run (N0 v2, audit finding)
        assert fake.calls.count("start_app") == 0
        from flashgate import records
        rec = records.latest_record(self._board(tmp_path).firmware_dir)
        by = {c["name"]: c for c in rec["checks"]}
        assert by["flash"]["status"] == "failed"
        assert "could not run" in by["flash"]["detail"]
        assert "SwdError" in by["flash"]["detail"]

    def test_wipe_readback_empty_or_short_fails_closed(self, tmp_path,
                                                       monkeypatch):
        # a tool exiting 0 with a truncated/empty dump is the same silent
        # lie as a nonzero read — not confirmation (adversarial M1).
        from flashgate import backends, cli

        class TruncRead(backends.FakeBackend):
            def read_mem(self, connect, address, size):
                if size == 4:
                    return self.trunc
                return super().read_mem(connect, address, size)

        for trunc in (b"", b"\x00\x00"):
            fake = TruncRead(signature=self._sig(b"aaaaaaa-dirty"))
            fake.trunc = trunc
            rc = self._run(tmp_path, fake, monkeypatch)
            assert rc == cli.EXIT_SHA_MISMATCH, trunc

    def test_explicit_wipe_failure_exit_5(self, tmp_path, monkeypatch):
        # L4: the fake can now express an outright wipe failure.
        from flashgate import backends, cli
        fake = backends.FakeBackend(signature=self._sig(b"aaaaaaa-dirty"),
                                    wipe_ok=False)
        rc = self._run(tmp_path, fake, monkeypatch)
        assert rc == cli.EXIT_SHA_MISMATCH
        assert not any(c.startswith("read:") for c in fake.calls)

    def test_happy_path_performs_and_passes_readback(self, tmp_path, monkeypatch):
        from flashgate import backends, cli
        from flashgate.board import Board
        fake = backends.FakeBackend(
            signature=self._sig(b"aaaaaaa-dirty"))
        monkeypatch.setattr(Board, "head_sha", lambda self: "aaaaaaa-dirty")
        rc = self._run(tmp_path, fake, monkeypatch)
        assert rc == cli.EXIT_OK
        reads = [c for c in fake.calls if c.startswith("read:")]
        assert any(c.endswith("+4") for c in reads), "4-byte readback happened"
        from flashgate import records
        rec = records.latest_record(self._board(tmp_path).firmware_dir)
        by_name = {c["name"]: c for c in rec["checks"]}
        assert by_name["flash"]["status"] == "passed"
        assert "readback confirmed" in by_name["flash"]["detail"]
        assert fake._wiped_at is None          # cleared by start_app

    def test_fake_adapter_selectable_from_profile(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLASHGATE_ALLOW_FAKE", "1")
        board = self._board(tmp_path)
        assert board.flash_adapter == "fake"

    def test_fake_requires_explicit_opt_in(self, tmp_path, monkeypatch):
        # F4: without the env gate, a fake profile must not even load —
        # a simulated flash can green-light a board never programmed.
        import pytest as _pytest
        from flashgate.board import BoardError
        monkeypatch.delenv("FLASHGATE_ALLOW_FAKE", raising=False)
        with _pytest.raises(BoardError, match="FLASHGATE_ALLOW_FAKE"):
            self._board(tmp_path)

    def test_unknown_adapter_rejected_at_load(self, tmp_path):
        import pytest
        from flashgate.board import BoardError
        with pytest.raises(BoardError, match="flash.adapter"):
            self._board(tmp_path, extra="  adapter: jlink\n")


class TestPhase2AuditFixes:
    """Runtime-audit gaps: openocd_target wiring, per-backend probe
    detection, doctor's on-board read through the backend."""

    def test_openocd_target_override_wired_from_profile(self, tmp_path):
        from flashgate.board import load_board
        fw = tmp_path / "fw"; fw.mkdir()
        p = tmp_path / "b.yaml"
        p.write_text(
            "board: t\nmcu: STM32H743\nfirmware:\n  dir: fw\n"
            "  build: ninja\n  artifact: fw.bin\n"
            "flash:\n  adapter: openocd\n"
            "  openocd_target: my_custom_target\n"
            "evidence:\n  mode: swd\nserial:\n  banner: 'B {git}'\n",
            encoding="utf-8")
        board = load_board(p)
        assert board.openocd_target == "my_custom_target"
        from flashgate import backends
        b = backends.OpenOcdBackend(
            mcu=board.mcu, target=board.openocd_target or None)
        assert b._target == "my_custom_target"

    def test_probe_detection_markers(self):
        cp = backends.CubeProgrammerBackend()
        assert cp.probe_detected("--- ST-LINK SN : 56FF ---") is True
        assert cp.probe_detected("(CubeProgrammer CLI not found)") is False
        oc = backends.OpenOcdBackend(mcu="STM32H743")
        assert oc.probe_detected("Info : STLINK V2 (API v2) VID:PID 0483:3748") is True
        assert oc.probe_detected("SWD DPIDR 0x6ba02477") is True
        assert oc.probe_detected("(openocd probe listing unavailable: x)") is False
        fake = backends.FakeBackend()
        assert fake.probe_detected("(fake backend)") is True
        assert fake.probe_detected("") is False

    def test_board_backend_passes_overrides(self, tmp_path, monkeypatch):
        from flashgate.board import load_board
        from flashgate import cli
        fw = tmp_path / "fw"; fw.mkdir()
        p = tmp_path / "b.yaml"
        p.write_text(
            "board: t\nmcu: STM32H743\nfirmware:\n  dir: fw\n"
            "  build: ninja\n  artifact: fw.bin\n"
            "flash:\n  adapter: openocd\n"
            "  openocd_interface: interface/cmsis-dap.cfg\n"
            "evidence:\n  mode: swd\nserial:\n  banner: 'B {git}'\n",
            encoding="utf-8")
        board = load_board(p)
        b = cli._board_backend(board)
        assert isinstance(b, backends.OpenOcdBackend)
        assert b._interface == "interface/cmsis-dap.cfg"
        assert b._target == "stm32h7x"          # MCU mapping still applies


class TestInjectionAndDelegation:
    """Q4/Q6 mutation survivors + F1/F2 injection rejections."""

    def test_program_command_is_forward_slashed_and_braced(self, monkeypatch):
        import os
        import pathlib
        b = backends.OpenOcdBackend(mcu="STM32H743")
        seen = {}
        monkeypatch.setattr(b, "_run",
                            lambda cmds: (seen.update(cmds=cmds) or (0, "")))
        monkeypatch.setattr(backends.Path, "is_file",
                            lambda self: True, raising=False)
        if os.name == "nt":
            fake_bin = pathlib.PureWindowsPath(r"E:\dir\na me\App.bin")
            expected = "program {E:/dir/na me/App.bin} 0x08000000 verify"
        else:
            # POSIX: Path() never sees backslashes; assert the braced,
            # space-safe form of a real POSIX path instead
            fake_bin = pathlib.Path("/tmp/dir/na me/App.bin")
            expected = "program {/tmp/dir/na me/App.bin} 0x08000000 verify"
        b.flash(fake_bin, "c", "0x08000000")
        assert seen["cmds"][0] == expected

    def test_brace_in_artifact_path_refused(self, tmp_path):
        evil = tmp_path / "we}ird.bin"
        evil.write_bytes(b"x")
        b = backends.OpenOcdBackend(mcu="STM32H743")
        res = b.flash(evil, "c", "0x08000000")
        assert res.ok is False and "cannot be passed" in res.detail

    def test_address_with_tcl_separator_rejected_at_load(self, tmp_path):
        import pytest as _pytest
        from flashgate.board import BoardError, load_board
        fw = tmp_path / "fw"; fw.mkdir()
        p = tmp_path / "b.yaml"
        p.write_text("board: t\nmcu: STM32H743\nfirmware:\n  dir: fw\n"
                     "  build: ninja\n  artifact: fw.bin\n"
                     'flash:\n  address: "0x08000000; shutdown"\n'
                     "serial:\n  banner: 'B {git}'\n", encoding="utf-8")
        with _pytest.raises(BoardError, match="flash.address"):
            load_board(p)

    def test_openocd_target_with_brace_rejected_at_load(self, tmp_path):
        import pytest as _pytest
        from flashgate.board import BoardError, load_board
        fw = tmp_path / "fw"; fw.mkdir()
        p = tmp_path / "b.yaml"
        p.write_text("board: t\nmcu: STM32H743\nfirmware:\n  dir: fw\n"
                     "  build: ninja\n  artifact: fw.bin\n"
                     "flash:\n  adapter: openocd\n"
                     "  openocd_target: 'x}; echo PWNED; #'\n"
                     "serial:\n  banner: 'B {git}'\n", encoding="utf-8")
        with _pytest.raises(BoardError, match="openocd_target"):
            load_board(p)

    def test_cubeprogrammer_read_mem_delegates_in_order(self, monkeypatch):
        from flashgate import swdsig as swsig
        seen = {}
        monkeypatch.setattr(swsig, "read_ram",
                            lambda c, a, s: seen.update(c=c, a=a, s=s) or b"")
        b = backends.CubeProgrammerBackend()
        b.read_mem("port=SWD", 0x2001FF00, 64)
        assert seen == {"c": "port=SWD", "a": 0x2001FF00, "s": 64}

    def test_interface_short_name_gets_prefix(self, monkeypatch):
        b = backends.OpenOcdBackend(mcu="STM32H743",
                                    interface="cmsis-dap")
        seen = {}
        monkeypatch.setattr(b, "_run",
                            lambda cmds: (seen.update(cmds=cmds) or (0, "")))
        b.write32("c", 0, 0x2001FF00)
        argv = seen["cmds"]  # not full argv; assert via prefixing behavior
        assert b._interface == "cmsis-dap"
        # prefixing happens in _run's cmd construction; assert through flash
        import flashgate.backends as bm
        src = open(bm.__file__, encoding="utf-8").read()
        assert 'f"interface/{i}.cfg"' in src



class TestReadbackOSErrorIsEnvFailure:
    def test_readback_spawn_failure_exit_6_start_withheld(self, tmp_path,
                                                          monkeypatch):
        # the read could not RUN (tool spawn OSError) — environment
        # failure exit 6 with the check named, never a pass, start
        # withheld (N0-6: was an accidental crash-net 6 before)
        from flashgate import backends, cli
        _P = TestFakeBackendPipeline

        class NoTool(backends.FakeBackend):
            def read_mem(self, connect, address, size):
                if size == 4:
                    raise OSError("spawn failed")
                return super().read_mem(connect, address, size)
        monkeypatch.setenv("FLASHGATE_ALLOW_FAKE", "1")
        board = _P._board(tmp_path)
        fake = NoTool(signature=_P._sig(b"aaaaaaa-dirty"))
        monkeypatch.setattr(cli, "_build", lambda b, j=None: cli.EXIT_OK)
        monkeypatch.setattr(cli, "_board_backend", lambda b: fake)
        assert cli.cmd_verify(board, None) == cli.EXIT_ENV
        assert fake.calls.count("start_app") == 0
        from flashgate import records
        rec = records.latest_record(board.firmware_dir)
        by = {c["name"]: c for c in rec["checks"]}
        assert by["flash"]["status"] == "failed"
        assert "could not run" in by["flash"]["detail"]
