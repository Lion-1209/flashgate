"""MCP contract tests: every tool returns the unified Result envelope,
stable codes map from the CLI exit contract, and structuredContent carries
the envelope on mcp 2.x. Programmatic call_tool — no transport needed."""

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

from flashgate import mcp_server as srv
from flashgate import results


class TestExitMapping:
    def test_all_eight_exit_codes_map(self):
        expect = {
            0: ("succeeded", results.OK),
            1: ("failed", results.BUILD_FAILED),
            2: ("failed", results.FLASH_FAILED),
            3: ("timed_out", results.BOOT_EVIDENCE_TIMEOUT),
            4: ("failed", results.BOOT_ERROR),
            5: ("failed", results.IDENTITY_MISMATCH),
            6: ("incomplete", results.CAPABILITY_UNAVAILABLE),
            7: ("failed", results.PROBE_FAILED),
        }
        for rc, (status, code) in expect.items():
            r = results.from_exit(rc, "s")
            assert r.status == status and r.code == code and r.exit_code == rc

    def test_unknown_exit_code_is_internal_error(self):
        r = results.from_exit(99, "s")
        assert r.status == "failed" and r.code == results.INTERNAL_ERROR

    def test_log_rides_in_data(self):
        r = results.from_exit(0, "s", log="line1\nline2")
        assert r.data["log"] == "line1\nline2"

    def test_json_round_trip(self):
        r = results.from_exit(7, "probe broke", log="x")
        parsed = json.loads(r.model_dump_json())
        assert parsed["code"] == results.PROBE_FAILED


class TestRegisteredTools:
    TOOLS = {"board_info", "doctor", "build", "flash", "verify",
             "probe", "console_send", "console_read"}

    def test_all_tools_registered(self):
        async def go():
            return {t.name for t in await srv.mcp.list_tools()}
        assert asyncio.run(go()) == self.TOOLS

    def test_annotations_attached_when_supported(self):
        async def go():
            return {t.name: t.annotations for t in await srv.mcp.list_tools()}
        anns = asyncio.run(go())
        if srv.ToolAnnotations is None:                 # pre-1.9 SDK
            pytest.skip("SDK lacks ToolAnnotations")
        assert anns["board_info"].read_only_hint is True
        assert anns["flash"].destructive_hint is True
        assert anns["verify"].destructive_hint is True
        assert anns["console_read"].read_only_hint is True

    def test_every_tool_is_envelope_typed(self):
        async def go():
            out = {}
            for t in await srv.mcp.list_tools():
                out[t.name] = (t.output_schema or {}).get("properties", {})
            return out
        props = asyncio.run(go())
        for name in self.TOOLS:
            assert {"status", "code", "summary", "schema_version"} <= set(props[name]), name


class TestBoardInfoTool:
    def test_default_profile_returns_envelope(self):
        async def go():
            return await srv.mcp.call_tool("board_info", {})
        res = asyncio.run(go())
        sc = res.structured_content
        assert res.is_error is False
        assert sc["schema_version"] == results.SCHEMA_VERSION
        assert sc["status"] == "succeeded"
        assert sc["code"] == results.OK
        assert sc["policy"] == {"risk": "R0"}
        assert isinstance(sc["data"]["probes"], list)

    def test_missing_profile_is_structured_failure(self):
        async def go():
            return await srv.mcp.call_tool(
                "board_info", {"board": "Z:/definitely/missing.yaml"})
        res = asyncio.run(go())
        sc = res.structured_content
        assert sc["status"] == "failed"
        assert sc["code"] == results.PROFILE_NOT_FOUND
        assert res.is_error is False        # domain errors are envelopes, not MCP errors


class TestProbeTool:
    def test_unresolved_serial_is_incomplete_never_pass(self, monkeypatch):
        monkeypatch.setattr(
            srv.serialmon, "resolve_console_port",
            lambda serial_port, vid, pids: (None, "no serial found"))
        async def go():
            return await srv.mcp.call_tool("probe", {})
        res = asyncio.run(go())
        sc = res.structured_content
        assert sc["status"] == "incomplete"
        assert sc["code"] == results.CAPABILITY_UNAVAILABLE

    def test_unopenable_serial_is_incomplete(self, tmp_path, monkeypatch):
        import serial as serial_mod

        board_yaml = tmp_path / "board.yaml"
        board_yaml.write_text(
            "board: t\nmcu: m\ndescription: d\nfirmware:\n  dir: fw\n"
            "  build: ninja -C build\n  artifact: build/fw.bin\nserial:\n"
            "  baudrate: 9600\n  banner: 'BOOT {git}'\n", encoding="utf-8")
        (tmp_path / "fw").mkdir()
        monkeypatch.setattr(
            srv.serialmon, "resolve_console_port",
            lambda serial_port, vid, pids: ("COMX", "hint"))

        def boom(*a, **k):
            raise serial_mod.SerialException("access denied")
        monkeypatch.setattr(srv.serialmon, "open_flush", boom)
        async def go():
            return await srv.mcp.call_tool("probe", {"board": str(board_yaml)})
        res = asyncio.run(go())
        sc = res.structured_content
        assert sc["status"] == "incomplete"
        assert sc["code"] == results.TRANSPORT_ERROR
        assert "COMX" in sc["summary"]


class TestProbeEdgeCases:
    """Regressions from the adversarial review of 0.5.0."""

    @staticmethod
    def _board_yaml(tmp_path):
        yaml = tmp_path / "board.yaml"
        yaml.write_text(
            "board: t\nmcu: m\ndescription: d\nfirmware:\n  dir: fw\n"
            "  build: ninja -C build\n  artifact: build/fw.bin\nserial:\n"
            "  baudrate: 9600\n  banner: 'BOOT {git}'\n", encoding="utf-8")
        (tmp_path / "fw").mkdir()
        return yaml

    def test_empty_names_means_all_like_cli(self, tmp_path, monkeypatch):
        # F1 regression: probe(names=[]) used to run ZERO probes and return
        # succeeded — a false pass at the heart of the gate's invariant.
        # [] must behave exactly like None ("all"), as the CLI does.
        yaml = self._board_yaml(tmp_path)
        monkeypatch.setattr(srv.serialmon, "resolve_console_port",
                            lambda sp, vid, pids: ("COMX", "hint"))
        monkeypatch.setattr(srv.serialmon, "open_flush",
                            lambda *a, **k: SimpleNamespace(close=lambda: None))
        called = {}
        def fake_run(b, names, conn):
            called["names"] = names
            return 0
        monkeypatch.setattr(srv.cli_mod, "_run_probes", fake_run)
        res = asyncio.run(srv.mcp.call_tool(
            "probe", {"board": str(yaml), "names": []}))
        assert res.structured_content["status"] == "succeeded"
        assert called["names"] is None              # expanded to "all"

    def test_unknown_probe_name_is_invalid_argument(self, tmp_path, monkeypatch):
        # F3: a typo in probe names is an argument error, not a missing
        # capability — the agent should fix the name, not hunt the serial.
        yaml = self._board_yaml(tmp_path)
        monkeypatch.setattr(srv.serialmon, "resolve_console_port",
                            lambda sp, vid, pids: (None, "n/a"))
        res = asyncio.run(srv.mcp.call_tool(
            "probe", {"board": str(yaml), "names": ["typo"]}))
        sc = res.structured_content
        assert sc["status"] == "failed"
        assert sc["code"] == results.INVALID_ARGUMENT
        assert "typo" in sc["summary"]


class TestCatchAllGuard:
    def test_internal_exception_becomes_envelope(self, monkeypatch):
        # F2: no exception may escape a tool into an MCP protocol error;
        # the envelope contract survives even a crash inside the CLI layer.
        def boom(b):
            raise RuntimeError("kaboom")
        monkeypatch.setattr(srv.cli_mod, "cmd_doctor", boom)
        res = asyncio.run(srv.mcp.call_tool("doctor", {}))
        sc = res.structured_content
        assert res.is_error is False
        assert sc["status"] == "failed"
        assert sc["code"] == results.INTERNAL_ERROR
        assert "kaboom" in sc["summary"]
