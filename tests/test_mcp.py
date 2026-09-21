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


class TestExitCoupling:
    def test_exit_env_maps_to_incomplete_never_succeeded(self):
        # Belt-and-suspenders (mutation finding M5): the CLI constants the
        # fail-closed guards assert must map to "incomplete"/"failed" in the
        # envelope. If the mapping ever flips to "succeeded", a check that
        # could not run would count as a pass.
        from flashgate import cli
        assert results.from_exit(cli.EXIT_ENV, "x").status == "incomplete"
        assert results.from_exit(cli.EXIT_PROBE_FAIL, "x").status == "failed"


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


class TestBoardInfoProbesWarning:
    def test_no_step_probe_becomes_warning_not_internal_error(self, tmp_path):
        # F2 regression: board_info claimed "warnings flag an unloadable
        # probes section" but ProbeError (probe with no steps) escaped to
        # INTERNAL_ERROR. The except tuple must match the probe tool's.
        yaml = tmp_path / "board.yaml"
        yaml.write_text(
            "board: t\nmcu: m\ndescription: d\nfirmware:\n  dir: fw\n"
            "  build: ninja -C build\n  artifact: build/fw.bin\nserial:\n"
            "  baudrate: 9600\n  banner: 'BOOT {git}'\n"
            "probes:\n  broken: {}\n", encoding="utf-8")
        (tmp_path / "fw").mkdir()
        res = asyncio.run(srv.mcp.call_tool("board_info", {"board": str(yaml)}))
        sc = res.structured_content
        assert sc["status"] == "succeeded"          # profile itself is fine
        assert sc["data"]["probes"] == []
        assert any("probes unloadable" in w for w in sc["warnings"])


class TestVerifyRecord:
    """Stage 1: verify returns the persisted evidence record inline."""

    def test_verify_data_carries_record(self, tmp_path, monkeypatch):
        from flashgate import records as rec_mod
        from flashgate import cli
        yaml = TestProbeEdgeCases._board_yaml(tmp_path)
        board = cli.load_board(yaml)

        def fake_verify(b, names, evidence=None):
            j = rec_mod.VerifyJournal(["build"], mode="swd", probe_names=names)
            j.check("build", "passed")
            rec = j.to_record(b, 0, "succeeded", "stubbed pass")
            rec_mod.write_record(rec, b.firmware_dir, fingerprint="aa" * 32)
            return 0

        monkeypatch.setattr(srv.cli_mod, "cmd_verify", fake_verify)
        res = asyncio.run(srv.mcp.call_tool(
            "verify", {"board": str(yaml)}))
        data = res.structured_content
        assert data["status"] == "succeeded"
        assert data["data"]["record"]["run"]["exit_code"] == 0
        assert data["data"]["record"]["checks"][0]["name"] == "build"
        assert data["data"]["record_dir"].replace("\\", "/").endswith(
            ".flashgate/records")

    def test_verify_survives_record_read_failure(self, tmp_path, monkeypatch):
        yaml = TestProbeEdgeCases._board_yaml(tmp_path)

        def fake_verify(b, names, evidence=None):
            return 7

        monkeypatch.setattr(srv.cli_mod, "cmd_verify", fake_verify)
        monkeypatch.setattr(srv.records, "latest_record",
                            lambda d: (_ for _ in ()).throw(OSError("gone")))
        res = asyncio.run(srv.mcp.call_tool(
            "verify", {"board": str(yaml)}))
        assert res.structured_content["status"] == "failed"
        assert "record" not in res.structured_content["data"]


class TestVerifyRecordIsolation:
    """Adversarial review F3: data.record must be THIS run's record —
    never a stale green one picked by mtime."""

    def test_stale_last_not_attached(self, tmp_path, monkeypatch):
        import time as _time
        from flashgate import records as rec_mod
        from flashgate import cli
        yaml = TestProbeEdgeCases._board_yaml(tmp_path)
        board = cli.load_board(yaml)
        # an OLD run's green record — BOTH in the in-process registry AND
        # on disk, so neither an mtime pick nor a stale LAST may attach it
        j = rec_mod.VerifyJournal(["build"], mode="swd", probe_names=None)
        j.check("build", "passed")
        stale = j.to_record(board, 0, "succeeded", "old green run")
        rec_mod.write_record(stale, board.firmware_dir, fingerprint="cc" * 32)
        rec_mod.LAST = {"record": stale, "fw_dir": board.firmware_dir,
                        "at": _time.monotonic() - 5, "path": None}

        def fake_verify(b, names, evidence=None):
            return 7                      # fails, writes nothing

        monkeypatch.setattr(srv.cli_mod, "cmd_verify", fake_verify)
        res = asyncio.run(srv.mcp.call_tool("verify", {"board": str(yaml)}))
        data = res.structured_content
        assert data["status"] == "failed"
        assert "record" not in data["data"], \
            "a failed run must not carry an older run's record"

    def test_fresh_last_attached(self, tmp_path, monkeypatch):
        from flashgate import records as rec_mod
        from flashgate import cli
        yaml = TestProbeEdgeCases._board_yaml(tmp_path)
        board = cli.load_board(yaml)

        def fake_verify(b, names, evidence=None):
            j = rec_mod.VerifyJournal(["build"], mode="swd", probe_names=names)
            j.check("build", "passed")
            rec = j.to_record(b, 0, "succeeded", "ok")
            rec_mod.write_record(rec, b.firmware_dir, fingerprint="bb" * 32)
            return 0

        monkeypatch.setattr(srv.cli_mod, "cmd_verify", fake_verify)
        res = asyncio.run(srv.mcp.call_tool("verify", {"board": str(yaml)}))
        assert res.structured_content["data"]["record"]["run"]["exit_code"] == 0


class TestToolDefinitionQuality:
    """Directory-facing guards (Glama TDQS dimensions): every parameter of
    every tool carries a real description, the schema's parameter set stays
    in sync with the function signature (a new param without a doc goes
    red), no description re-inflates past the conciseness budget, the
    read-only / destructive hints are backed by the words in the text, the
    verify description keeps every failure code an agent must route on, and
    the mcp 1.x compatibility shim keeps the wraps-chain annotations
    resolved."""

    MAX_DESC = 1600          # verify sits at ~1.5k; the headroom is deliberate
    MIN_PARAM_DOC = 20       # a stub like "the board" is not a description
    VERIFY_CODES = ("BUILD_FAILED", "FLASH_FAILED", "BOOT_EVIDENCE_TIMEOUT",
                    "BOOT_ERROR", "IDENTITY_MISMATCH", "CAPABILITY_UNAVAILABLE",
                    "PROBE_FAILED")

    @staticmethod
    def _tools():
        async def go():
            return {t.name: t for t in await srv.mcp.list_tools()}
        return asyncio.run(go())

    @staticmethod
    def _schema(t):
        # mcp 2.x exposes input_schema, 1.x inputSchema — the guards must
        # run on both, since the shim they partly protect is a 1.x fix.
        return getattr(t, "input_schema", None) or t.inputSchema

    def test_every_parameter_is_described(self):
        for name, t in self._tools().items():
            props = self._schema(t).get("properties", {})
            assert props, name
            for param, spec in props.items():
                doc = (spec.get("description") or "").strip()
                assert len(doc) >= self.MIN_PARAM_DOC, \
                    f"{name}.{param} lacks a real description: {doc!r}"

    def test_schema_params_match_signature(self):
        import inspect
        for name, t in self._tools().items():
            fn = getattr(srv, name)
            want = set(inspect.signature(fn).parameters)
            have = set(self._schema(t).get("properties", {}))
            assert want == have, f"{name}: schema {have} != signature {want}"

    def test_no_description_reinflates(self):
        for name, t in self._tools().items():
            assert len(t.description or "") <= self.MAX_DESC, \
                f"{name} description is {len(t.description)} chars — keep it terse"

    def test_destructive_hint_is_stated(self):
        for name, t in self._tools().items():
            ann = t.annotations
            if ann is not None and getattr(ann, "destructive_hint", False):
                assert "DESTRUCTIVE" in (t.description or ""), \
                    f"{name} mutates the device but never says so"

    def test_readonly_hint_is_stated(self):
        for name, t in self._tools().items():
            ann = t.annotations
            if ann is not None and getattr(ann, "read_only_hint", False):
                low = (t.description or "").lower()
                assert ("read-only" in low or "read only" in low
                        or "no hardware" in low), \
                    f"{name} claims read-only but the description never says so"

    def test_verify_keeps_every_failure_code(self):
        desc = self._tools()["verify"].description or ""
        missing = [c for c in self.VERIFY_CODES if c not in desc]
        assert not missing, f"verify description lost routing codes: {missing}"

    def test_wraps_chain_annotations_resolved(self):
        # mcp 1.x runs issubclass() on the raw annotation; a string left
        # anywhere on the wraps chain (from __future__ import annotations)
        # crashes its import. The _register shim must have resolved them.
        import inspect
        for name in self._tools():
            fn, seen = getattr(srv, name), set()
            while fn is not None and id(fn) not in seen:
                seen.add(id(fn))
                for ann in inspect.signature(fn).parameters.values():
                    assert not isinstance(ann.annotation, str), \
                        f"{name}: unresolved string annotation on the wraps chain"
                fn = getattr(fn, "__wrapped__", None)
