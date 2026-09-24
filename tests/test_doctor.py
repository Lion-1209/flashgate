"""N2: doctor --export — the environment checkup as a sendable report.

The acceptance bar from the product plan: one command produces a file a
user can hand to support; on a toolchain-less machine the all-red page
must still be readable (each problem with its fix hint); the redacted
variant must not leak the workstation's identity or directory layout.
"""

import json
import re
import sys

import pytest

from flashgate import doctor as doctor_mod
from flashgate import redact as redact_mod


@pytest.fixture
def board(tmp_path):
    from tests.test_cli import make_board
    return make_board(tmp_path)


def _patch_env(monkeypatch, *, backend_ok=True, probe_ok=True,
               console="COM3", tools=True):
    from types import SimpleNamespace
    from flashgate import cli

    backend = SimpleNamespace(
        name="fake", available=lambda: "fake.exe" if backend_ok else None,
        discover=lambda: "(probe)" if probe_ok else "",
        probe_detected=lambda out: bool(out),
        read_mem=lambda *a: None)
    monkeypatch.setattr(cli.backends, "get_backend", lambda n, **k: backend)
    monkeypatch.setattr(
        cli.serialmon, "resolve_console_port",
        lambda *a: ((console, "test hint") if console else (None, "no port")))
    if tools:
        monkeypatch.setattr(doctor_mod.shutil, "which",
                            lambda t, path=None: f"/opt/tools/{t}")
    else:
        monkeypatch.setattr(doctor_mod.shutil, "which",
                            lambda t, path=None: None)
    monkeypatch.setattr(doctor_mod.serialmon, "resolve_console_port",
                        cli.serialmon.resolve_console_port)
    # Patch the construction point every surface now shares
    # (backends.backend_for_board) rather than get_backend: an openocd
    # profile no longer goes through get_backend at all, so patching
    # only that left the fixture silently building a real backend
    # (runtime audit S6).
    monkeypatch.setattr(doctor_mod.backends, "backend_for_board",
                        lambda b: backend)
    from flashgate.board import Board
    monkeypatch.setattr(Board, "head_sha", lambda self: "abc1234")
    # the on-board read goes through the fake backend (returns None)


class TestCollect:
    def test_all_green(self, board, monkeypatch):
        _patch_env(monkeypatch)
        rep = doctor_mod.collect_report(board)
        assert rep["all_ok"] and rep["problems"] == []
        names = [c["name"] for c in rep["checks"]]
        assert {"backend", "probe", "console", "cmake", "ninja",
                "arm-none-eabi-gcc", "head-sha", "on-board"} <= set(names)

    def test_all_red_names_problems_with_hints(self, board, monkeypatch):
        _patch_env(monkeypatch, backend_ok=False, probe_ok=False,
                   console=None, tools=False)
        rep = doctor_mod.collect_report(board)
        assert not rep["all_ok"]
        # backend missing -> the probe check is skipped (original
        # doctor semantics): backend+console+3 tools
        assert len(rep["problems"]) == 5
        assert not any(c["name"] == "probe" for c in rep["checks"])
        hinted = [c for c in rep["checks"] if c["ok"] is False]
        assert all(c["hint"] for c in hinted), \
            "every failed check must carry a fix hint"


class TestRenderers:
    def test_markdown_cannot_be_forged_by_injected_values(self, board,
                                                           monkeypatch):
        # adversarial M-1: serial.port and the board name are
        # unvalidated profile strings, and the on-board signature is
        # decoded from firmware RAM — either can carry a newline or a
        # pipe. The report is evidence handed to a third party, so it
        # must not be able to grow a fake verdict row.
        from flashgate import cli
        _patch_env(monkeypatch)
        monkeypatch.setattr(
            cli.serialmon, "resolve_console_port",
            lambda *a: ("COM3\n| FAKE | PASSED | all good |", "test"))
        rep = doctor_mod.collect_report(board)
        md = doctor_mod.render_markdown(rep)
        table_rows = [ln for ln in md.splitlines()
                      if ln.startswith("| ") and "---" not in ln]
        # header + one row per check, and no injected row between them
        assert len(table_rows) == 1 + len(rep["checks"])
        assert "| FAKE | PASSED |" not in md
        assert "FAKE" in md            # the value is still shown, escaped
        # a newline in a heading field cannot open a fake section
        rep2 = json.loads(json.dumps(rep))
        rep2["board"]["name"] = "apollo\n## fakesection\n\n**ALL PASSED**"
        md2 = doctor_mod.render_markdown(rep2)
        assert not any(ln.startswith("## fakesection")
                       for ln in md2.splitlines()), \
            "an injected newline opened a fake section heading"
        assert "ALL PASSED" in md2        # escaped into the title line
        # link/image/HTML injection: a clickable "[x](http://evil)" in a
        # forwarded report is a phishing vector even though it cannot
        # forge the verdict (adversarial M-B; the mutation round found
        # the escaping itself unguarded)
        rep3 = json.loads(json.dumps(rep))
        rep3["board"]["name"] = "[click](http://evil.example)"
        rep3["board"]["mcu"] = "<img src=x onerror=alert(1)>"
        rep3["checks"][0]["detail"] = "see [x](http://e.co) and <b>bold</b>"
        md3 = doctor_mod.render_markdown(rep3)
        # no UNESCAPED link/image/HTML survives (the escape backslash is
        # what makes markdown render it literally)
        for pattern in (r"(?<!\\)\[click\]\(http://evil",
                        r"(?<!\\)<img", r"(?<!\\)\[x\]\(http://e\.co\)",
                        r"(?<!\\)<b>"):
            assert re.search(pattern, md3) is None, pattern
        assert "\\[click\\]" in md3 and "\\<img" in md3   # escaped, not deleted
        assert "click" in md3 and "bold" in md3   # still readable, escaped

    def test_markdown_problems_section_carries_hints(self, board,
                                                     monkeypatch):
        # adversarial M4 survivor: the hints existed only in the console
        # copy; dropping them from the export page left the tests green
        _patch_env(monkeypatch, backend_ok=True, probe_ok=False,
                   console=None, tools=False)
        md = doctor_mod.render_markdown(
            doctor_mod.collect_report(board))
        section = md.split("## 问题与建议修复", 1)[1]
        lines = [ln for ln in section.splitlines()
                 if ln.startswith("- ")]
        assert lines, "no problem lines"
        assert all("——" in ln for ln in lines), \
            f"a problem line lost its hint: {lines}"

    def test_markdown_all_red_is_readable(self, board, monkeypatch):
        # backend PRESENT so the probe check runs and fails: the classic
        # "no toolchain-less machine left unreadable" acceptance case
        _patch_env(monkeypatch, backend_ok=True, probe_ok=False,
                   console=None, tools=False)
        md = doctor_mod.render_markdown(
            doctor_mod.collect_report(board))
        assert "PROBLEM(S) FOUND" in md
        for c in ("backend", "probe", "console", "cmake"):
            assert c in md
        assert "## 问题与建议修复" in md and "❌" in md

    def test_markdown_all_green(self, board, monkeypatch):
        _patch_env(monkeypatch)
        md = doctor_mod.render_markdown(doctor_mod.collect_report(board))
        assert "ALL CHECKS PASSED" in md and "不代表固件功能验证" in md

    def test_json_roundtrip(self, board, monkeypatch):
        _patch_env(monkeypatch)
        rep = doctor_mod.collect_report(board)
        again = json.loads(doctor_mod.render_json(rep))
        assert again["board"]["name"] == rep["board"]["name"]
        assert again["checks"] == rep["checks"]


class TestRedact:
    def test_forward_slash_drive_paths(self):
        # what cmake/ninja logs and hand-written yaml actually carry
        out = redact_mod.redact_text(
            r"exe E:/999-Git/tools/xpack/bin/cmake.EXE and C:/u/me/ninja.EXE")
        assert "E:" not in out and "C:" not in out
        assert "<PATH>/cmake.EXE" in out and "<PATH>/ninja.EXE" in out

    def test_home_tree_collapses_to_basename(self):
        # the first real redacted doctor report leaked everything BELOW
        # the home prefix: '<HOME>\AppData\Local\...\cmake.EXE' — the
        # workstation's layout minus the drive letter
        from pathlib import Path
        home = str(Path.home())
        out = redact_mod.redact_text(
            home + r"\AppData\Local\stm32cube\bundles\cmake\bin\cmake.EXE")
        assert out == "<PATH>/cmake.EXE"

    def test_bare_home_keeps_the_home_placeholder(self):
        from pathlib import Path
        home = str(Path.home())
        assert redact_mod.redact_text(f"dir {home}") == "dir <HOME>"

    def test_username_inside_a_path_outside_home(self):
        # the username pass runs BEFORE the path pass; the path patterns
        # must still match a segment that already carries a placeholder
        import getpass
        user = getpass.getuser()
        out = redact_mod.redact_text(rf"E:\work\{user}\fw.bin")
        assert user not in out
        assert "<PATH>/fw.bin" in out

    def test_forward_slash_home_gets_the_home_placeholder(self):
        # runtime audit F-C: 'C:/Users/me' used to fall through to the
        # path pass and read '<PATH>/<USER>' — no leak, but inconsistent
        from pathlib import Path
        fwd = str(Path.home()).replace("\\", "/")
        assert redact_mod.redact_text(f"dir {fwd}") == "dir <HOME>"

    def test_home_prefix_name_collision_is_not_eaten(self):
        # 'C:\Users\me2' is a DIFFERENT directory; the bare-home pattern
        # must not swallow its first characters and leave '<HOME>2\...'
        from pathlib import Path
        home = str(Path.home())
        sibling = home + "2"
        out = redact_mod.redact_text(sibling + r"\fw.bin")
        assert "<HOME>" not in out           # no half-eaten placeholder
        assert "<HOME>2" not in out
        assert home not in out               # the real home path is gone

    def test_root_relative_windows_paths_are_collapsed(self):
        # adversarial M-A: '\Windows\System32\drivers\x.sys' has no drive
        # letter, so no drive/UNC pattern matched it — the whole layout
        # survived verbatim
        bs = chr(92)
        out = redact_mod.redact_text(
            bs + "Windows" + bs + "System32" + bs + "drivers" + bs + "x.sys")
        assert out == "<PATH>/x.sys"
        out = redact_mod.redact_text(
            bs + "Program Files" + bs + "App" + bs + "config.ini")
        assert out == "<PATH>/config.ini"

    def test_identity_markers_match_case_insensitively(self):
        # adversarial L-A: Windows hostnames/paths are case-insensitive
        import platform
        host = platform.node()
        assert host and len(host) > 2
        out = redact_mod.redact_text(host.lower() + " and " + host.upper())
        assert out == "<HOST> and <HOST>"

    def test_home_patterns_survive_a_posix_home(self, monkeypatch):
        # The CI's ubuntu legs run with a POSIX home. Building the home
        # pattern by splitting on separators dropped the leading '/', so
        # '/home/alice' matched the path's TAIL: 'dir /<HOME>' and
        # '/<PATH>/fw.bin'. Windows-only thinking, caught only by a
        # non-Windows runner.
        from pathlib import Path
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: "/home/alice"))
        assert redact_mod.redact_text("dir /home/alice") == "dir <HOME>"
        assert redact_mod.redact_text(
            "/home/alice/build/fw.bin") == "<PATH>/fw.bin"
        assert redact_mod.redact_text(
            "/home/alice/AppData/x/cmake.EXE") == "<PATH>/cmake.EXE"
        # a sibling directory is not half-eaten either
        out = redact_mod.redact_text("/home/alice2/y.bin")
        assert "<HOME>" not in out and out.endswith("y.bin")

    def test_urls_survive_the_forward_slash_pattern(self):
        # 'https:' is five letters — the drive pattern must not eat it
        out = redact_mod.redact_text(
            "see https://a.b/c/d then E:/tools/x/cmake")
        assert "https://a.b/c/d" in out
        assert "<PATH>/cmake" in out and "E:" not in out

    def test_paths_lose_location_keep_basename(self):
        out = redact_mod.redact_text(
            r"profile E:\some\where\boards\a.yaml exe C:\u\me\t\cmake.EXE")
        assert "E:" not in out and "C:" not in out
        assert "<PATH>/a.yaml" in out and "<PATH>/cmake.EXE" in out

    def test_posix_home_paths(self):
        out = redact_mod.redact_text("/home/alice/fw/build/fw.bin")
        assert "alice" not in out and "<PATH>/fw.bin" in out

    def test_json_document_structure_survives(self):
        doc = json.dumps({"p": r"C:\\Users\\me\\x\\y.bin", "n": 3})
        out = redact_mod.redact_json_document(doc)
        parsed = json.loads(out)
        assert parsed["n"] == 3 and "C:" not in parsed["p"]

    def test_spaced_path_segments_do_not_leak_the_rest(self):
        # adversarial H1: 'C:\Program Files\...' is where ST tools
        # install by default — a space-free segment class redacted only
        # 'C:\' and published every directory after the first space
        out = redact_mod.redact_text(
            r"C:\Program Files\CMake\bin\cmake.EXE")
        assert out == "<PATH>/cmake.EXE"
        out = redact_mod.redact_text(r"C:\Users\me\My Docs\a.yaml")
        assert out == "<PATH>/a.yaml"

    def test_unc_paths_lose_server_and_share(self):
        # adversarial M-2: a bench with the toolchain on a network share
        out = redact_mod.redact_text(r"\\server\share\cmake.EXE")
        assert out == "<PATH>/cmake.EXE"
        assert "server" not in out and "share" not in out

    def test_identity_markers_are_whole_tokens(self):
        # adversarial L-2: a bench named 'apollo' must not turn the board
        # name into '<HOST>-h743', and a user named 'ninja' must not
        # rename the ninja check
        monkey_host = "apollo"
        orig_node = redact_mod.platform.node
        redact_mod.platform.node = lambda: monkey_host
        try:
            out = redact_mod.redact_text("board apollo-h743 on host apollo")
        finally:
            redact_mod.platform.node = orig_node
        assert out == "board apollo-h743 on host <HOST>"

    def test_username_only_replaces_whole_tokens(self):
        import getpass
        user = getpass.getuser()
        assert user and len(user) > 2, "test needs a real username"
        glued = f"x{user}y and {user}2"
        out = redact_mod.redact_text(glued)
        assert glued in out, "a username inside a longer token was replaced"
        assert redact_mod.redact_text(f"user {user} ok") == "user <USER> ok"

    def test_missing_username_environment_does_not_crash(self,
                                                         monkeypatch):
        # adversarial H-3: getpass.getuser() falls through to the pwd
        # module on a slim image with no USER env — ModuleNotFoundError
        # crashed doctor --export --redact with exit 1 (= build failed)
        def _boom():
            raise ImportError("No module named 'pwd'")
        monkeypatch.setattr(redact_mod.getpass, "getuser", _boom)
        out = redact_mod.redact_text(r"C:\Tools\cmake.EXE")
        assert out == "<PATH>/cmake.EXE"

    def test_harmless_strings_untouched(self):
            "COM3 @ 115200 [ok]"

    def test_urls_survive(self):
        # the posix-path scrubber must not start inside '://' —
        # https://a.b/c/d has to come through verbatim (N2 round bug)
        out = redact_mod.redact_text(
            "see https://a.b/c/d and http://x/y/z then /opt/t/cmake")
        assert "https://a.b/c/d" in out and "http://x/y/z" in out
        assert "<PATH>/cmake" in out


class TestOnBoardRead:
    """The diagnostic tool must never traceback on its own subject, and
    must not misname a generation gap as 'old firmware'."""

    def test_read_error_names_the_host_not_the_firmware(self, board,
                                                        monkeypatch):
        # adversarial L-4: on a toolchain-less machine the old wording
        # asserted "old firmware" about a board it never managed to read
        _patch_env(monkeypatch)
        monkeypatch.setattr(
            doctor_mod.swdsig, "wait_for_signature",
            lambda *a, **k: (None, "STM32CubeProgrammer CLI not found"))
        rep = doctor_mod.collect_report(board)
        note = [c for c in rep["checks"]
                if c["name"] == "on-board"][0]["detail"]
        assert "SWD read unavailable" in note
        assert "old firmware" not in note
        assert "CubeProgrammer" in note

    def test_oserror_during_read_is_an_environment_fact(self, board,
                                                        monkeypatch):
        _patch_env(monkeypatch)

        def _boom(*a, **k):
            raise OSError("probe yanked mid-read")

        monkeypatch.setattr(doctor_mod.swdsig, "wait_for_signature", _boom)
        rep = doctor_mod.collect_report(board)
        ob = [c for c in rep["checks"] if c["name"] == "on-board"][0]
        assert ob["ok"] is None and "SWD read unavailable" in ob["detail"]
        assert "probe yanked" in ob["detail"]

    def test_layout_mismatch_is_not_reported_as_old_firmware(self, board,
                                                             monkeypatch):
        _patch_env(monkeypatch)
        monkeypatch.setattr(
            doctor_mod.swdsig, "wait_for_signature",
            lambda *a, **k: (None, "signature layout version 2 not "
                                  "supported (this tool decodes version 1 "
                                  "only — update flashgate or the "
                                  "firmware's bsp_signature)"))
        rep = doctor_mod.collect_report(board)
        note = [c for c in rep["checks"]
                if c["name"] == "on-board"][0]["detail"]
        assert "layout" in note and "old firmware" not in note

    def test_openocd_tool_missing_is_not_reported_as_old_firmware(
            self, tmp_path, monkeypatch):
        # runtime audit S1: the H2 fix (doctor builds the backend the
        # same way verify does) exposed that OpenOcdBackend.read_mem
        # swallowed a tool failure into None, so an openocd bench on a
        # machine without openocd was told to reflash its firmware
        from flashgate import backends
        from flashgate.board import load_board
        fw = tmp_path / "fw"
        fw.mkdir()
        p = tmp_path / "oc.yaml"
        p.write_text(
            "board: oc\nmcu: STM32H743\nfirmware:\n  dir: fw\n"
            "  build: ninja\n  artifact: fw.bin\n"
            "flash:\n  adapter: openocd\n  openocd_target: stm32h7x\n"
            "evidence:\n  mode: swd\nserial:\n  banner: 'B {git}'\n",
            encoding="utf-8")
        monkeypatch.setattr(backends, "find_openocd", lambda: None)
        rep = doctor_mod.collect_report(load_board(p))
        note = [c for c in rep["checks"]
                if c["name"] == "on-board"][0]["detail"]
        assert "old firmware" not in note, note
        assert "openocd" in note.lower(), note

    def test_no_signature_still_says_old_firmware(self, board, monkeypatch):
        monkeypatch.setattr(doctor_mod.swdsig, "wait_for_signature",
                            lambda *a, **k: (None, "no valid signature yet"))
        rep = doctor_mod.collect_report(board)
        note = [c for c in rep["checks"]
                if c["name"] == "on-board"][0]["detail"]
        assert "old firmware" in note


class TestBackendWiring:
    """Adversarial H2: doctor must build the backend the SAME way the
    verify pipeline does. get_backend(adapter) alone drops the profile's
    mcu/target/interface, so an openocd bench got target=None and every
    discovery call returned 'no target mapping' without running the
    tool — doctor then told the user to check a probe that was fine."""

    def _openocd_board(self, tmp_path):
        from flashgate.board import load_board
        fw = tmp_path / "fw"
        fw.mkdir(exist_ok=True)
        p = tmp_path / "oc.yaml"
        p.write_text(
            "board: oc\nmcu: STM32H743\nfirmware:\n  dir: fw\n"
            "  build: ninja\n  artifact: fw.bin\n"
            "flash:\n  adapter: openocd\n"
            "  openocd_target: my_custom_target\n"
            "  openocd_interface: interface/cmsis-dap.cfg\n"
            "evidence:\n  mode: swd\nserial:\n  banner: 'B {git}'\n",
            encoding="utf-8")
        return load_board(p)

    def test_profile_target_reaches_the_tool(self, tmp_path, monkeypatch):
        from flashgate import backends
        holder = {}
        real_for_board = backends.backend_for_board

        def _spy_for_board(board):
            b = real_for_board(board)
            holder["backend"] = b
            return b

        monkeypatch.setattr(backends, "backend_for_board", _spy_for_board)
        argv = []
        real_run = backends.subprocess.run

        def _spy_run(cmd, **kw):
            argv.append([str(c) for c in cmd])
            return real_run(cmd, **kw)

        monkeypatch.setattr(backends.subprocess, "run", _spy_run)
        monkeypatch.setattr(backends, "find_openocd",
                            lambda: tmp_path / "openocd.exe")
        (tmp_path / "openocd.exe").write_text("not a real binary",
                                              encoding="utf-8")
        doctor_mod.collect_report(self._openocd_board(tmp_path))
        # the constructed backend carries the profile's overrides...
        backend = holder["backend"]
        assert backend._target == "my_custom_target"
        assert backend._interface == "interface/cmsis-dap.cfg"
        # ...and the tool is really invoked with that target script
        assert argv, "openocd was never invoked"
        assert any("my_custom_target" in " ".join(c) for c in argv), argv

    def test_probe_failure_keeps_the_listing_as_evidence(self, tmp_path,
                                                         monkeypatch):
        # adversarial M-3: 'none detected' plus 'check the cable' when
        # the TOOL is what failed sends people to re-seat healthy USB
        from flashgate import backends
        from types import SimpleNamespace

        class _Broken(SimpleNamespace):
            name = "openocd"
            available = lambda self: "/fake/openocd"
            discover = lambda self: "(openocd probe listing unavailable: target script not found)"
            probe_detected = staticmethod(
                lambda out: "VID:PID 0483:" in out or "DPIDR" in out)
            read_mem = lambda self, *a: None

        monkeypatch.setattr(backends, "backend_for_board",
                            lambda b: _Broken())
        rep = doctor_mod.collect_report(self._openocd_board(tmp_path))
        probe = [c for c in rep["checks"] if c["name"] == "probe"][0]
        assert probe["ok"] is False
        assert "listing" in probe["detail"]
        assert "target script not found" in probe["detail"]
        # the listing is user-visible content: it must not be able to
        # forge a table row either (adversarial L-E)
        md = doctor_mod.render_markdown(rep)
        rows = [ln for ln in md.splitlines()
                if ln.startswith("| ") and "---" not in ln]
        assert len(rows) == 1 + len(rep["checks"])


class TestExportCli:
    def test_export_markdown_and_json(self, board, monkeypatch, tmp_path):
        from flashgate import cli
        _patch_env(monkeypatch)
        md_path = tmp_path / "report.md"
        rc = cli.main(["--board", str(board.yaml_path), "doctor",
                       "--export", str(md_path)])
        assert rc == 0 and md_path.is_file()
        assert "环境体检单" in md_path.read_text(encoding="utf-8")

        json_path = tmp_path / "report.json"
        rc = cli.main(["--board", str(board.yaml_path), "doctor",
                       "--export", str(json_path)])
        assert rc == 0
        rep = json.loads(json_path.read_text(encoding="utf-8"))
        assert rep["all_ok"]

    def test_export_redacted_has_no_workstation_paths(
            self, board, monkeypatch, tmp_path):
        from flashgate import cli
        _patch_env(monkeypatch)
        monkeypatch.setattr(doctor_mod.shutil, "which",
                            lambda t, path=None: f"/opt/tools/{t}")
        out_path = tmp_path / "r.json"
        rc = cli.main(["--board", str(board.yaml_path), "doctor",
                       "--export", str(out_path), "--redact"])
        assert rc == 0
        text = out_path.read_text(encoding="utf-8")
        assert "/opt/tools" not in text          # location gone
        assert "cmake" in text                    # basename survives

    def test_exit_code_follows_problems(self, board, monkeypatch, tmp_path):
        from flashgate import cli
        _patch_env(monkeypatch, backend_ok=True, probe_ok=False,
                   console=None, tools=False)
        out_path = tmp_path / "red.md"
        rc = cli.main(["--board", str(board.yaml_path), "doctor",
                       "--export", str(out_path)])
        assert rc == cli.EXIT_ENV
        assert "PROBLEM(S) FOUND" in out_path.read_text(encoding="utf-8")

    def test_unwritable_export_target_is_a_loud_exit_6(self, board,
                                                       monkeypatch, tmp_path,
                                                       capsys):
        # a caller gating on rc=0 must not believe support has a report
        # that was never written
        from flashgate import cli
        _patch_env(monkeypatch)
        missing = tmp_path / "no-such-dir" / "r.md"
        rc = cli.main(["--board", str(board.yaml_path), "doctor",
                       "--export", str(missing)])
        assert rc == cli.EXIT_ENV
        assert not missing.exists()
        assert "could not export" in capsys.readouterr().out

    def test_redact_without_export_warns_and_keeps_verdict(self, board,
                                                           monkeypatch,
                                                           tmp_path, capsys):
        from flashgate import cli
        _patch_env(monkeypatch)
        rc = cli.main(["--board", str(board.yaml_path), "doctor",
                       "--redact"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "--redact has no effect without --export" in out

    def test_json_suffix_is_case_insensitive(self, board, monkeypatch,
                                             tmp_path):
        # adversarial M4 survivor: dropping .lower() left '.JSON' going
        # out as a markdown page with a json filename
        import json as _json
        from flashgate import cli
        _patch_env(monkeypatch)
        out = tmp_path / "r.JSON"
        rc = cli.main(["--board", str(board.yaml_path), "doctor",
                       "--export", str(out)])
        assert rc == 0
        _json.loads(out.read_text(encoding="utf-8"))

    @pytest.mark.skipif(sys.platform != "win32",
                        reason="NUL/CON are Windows device names")
    def test_windows_device_name_is_not_a_silent_success(
            self, board, monkeypatch, tmp_path, capsys):
        # adversarial M-5: writing to NUL succeeds and produces no file,
        # so the old code printed 'exported' and exited 0
        from flashgate import cli
        _patch_env(monkeypatch)
        rc = cli.main(["--board", str(board.yaml_path), "doctor",
                       "--export", "NUL"])
        assert rc == cli.EXIT_ENV
        assert "could not export" in capsys.readouterr().out

    def test_stat_failure_after_write_is_an_export_error(self, board,
                                                         monkeypatch,
                                                         tmp_path):
        # adversarial L-C: a file vanishing between write and the
        # post-write check must surface as ExportError (exit 6), not as
        # a bare OSError traceback with exit 1 (= "build failed")
        import pathlib
        _patch_env(monkeypatch)
        rep = doctor_mod.collect_report(board)
        monkeypatch.setattr(pathlib.Path, "is_file", lambda self: True)

        def _boom(self):
            raise OSError("file vanished between write and check")

        monkeypatch.setattr(pathlib.Path, "stat", _boom)
        with pytest.raises(doctor_mod.ExportError):
            doctor_mod.export_report(rep, tmp_path / "r.md")

    def test_export_error_raised_for_unwritable_path(self, board,
                                                     monkeypatch, tmp_path):
        _patch_env(monkeypatch)
        rep = doctor_mod.collect_report(board)
        with pytest.raises(doctor_mod.ExportError):
            doctor_mod.export_report(rep, tmp_path / "nope" / "r.json")

    def test_console_issues_name_every_red_check_with_its_hint(
            self, board, monkeypatch, tmp_path, capsys):
        # runtime audit F-A: the console issues list used to print the
        # bare detail, so three missing tools produced three IDENTICAL
        # lines and the console carried no fix advice at all
        from flashgate import cli
        _patch_env(monkeypatch, backend_ok=True, probe_ok=False,
                   console=None, tools=False)
        rc = cli.main(["--board", str(board.yaml_path), "doctor"])
        assert rc == cli.EXIT_ENV
        out = capsys.readouterr().out
        issues = [ln for ln in out.splitlines() if ln.strip().startswith("- ")]
        assert issues, "no issues listed"
        # every red check is named (probe + console + 3 tools here)
        for name in ("probe", "console", "cmake", "ninja",
                     "arm-none-eabi-gcc"):
            assert any(ln.strip().startswith(f"- {name}:") for ln in issues), \
                f"console issues lost the check name for {name}"
        assert len(set(issues)) == len(issues), \
            "console issues collapsed distinct checks into one string"
        # and every red line carries its fix hint
        assert all("\u2014" in ln for ln in issues), \
            f"a red console line lost its hint: {issues}"

    def test_console_issues_carry_the_backend_hint(self, board, monkeypatch,
                                                   capsys):
        from flashgate import cli
        _patch_env(monkeypatch, backend_ok=False)
        rc = cli.main(["--board", str(board.yaml_path), "doctor"])
        assert rc == cli.EXIT_ENV
        out = capsys.readouterr().out
        assert "- backend:" in out and "docs/GUIDE.md" in out

    def test_empty_export_argument_is_not_silently_ignored(
            self, board, monkeypatch, tmp_path, capsys):
        # runtime audit F-B: `--export ''` fell through the truthiness
        # check as "no export requested" and exited 0 with nothing written
        from flashgate import cli
        _patch_env(monkeypatch)
        rc = cli.main(["--board", str(board.yaml_path), "doctor",
                       "--export", ""])
        assert rc == cli.EXIT_ENV
        assert "could not export" in capsys.readouterr().out
