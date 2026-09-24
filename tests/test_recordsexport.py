"""N3: records export — the evidence archive as a sendable package.

Acceptance from the product plan: one command produces a file a user can
hand to vendor support; the redacted variant must contain ZERO local
paths (an automated grep, not a promise); the coverage block — what this
verdict proves and what it does not — travels with the export; and a
record that cannot be read is reported, never silently dropped.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

from flashgate import recordsexport
from flashgate import records as records_mod


def _record(record_id="20260924T010101000-0-aaaabbbb", exit_code=0,
            *, profile=None, artifact=None, banner=None, probes=("all",)):
    from pathlib import Path as _P
    home = str(_P.home())
    return {
        "schema_version": "1.1",
        "kind": "flashgate.verify",
        "record_id": record_id,
        "tool": {"name": "flashgate", "version": "0.9.1",
                 "python": "3.11.0", "platform": "win32"},
        "board": {"name": "apollo-h743", "mcu": "STM32H743IIT6",
                  "profile": profile or (home + r"\boards\apollo-h743.yaml"),
                  "profile_sha256": "0" * 64},
        "firmware": {"dir": home + r"\fw",
                     "git_sha": "4c8340e-dirty",
                     "tree_fingerprint": "f" * 64,
                     "artifact": artifact or (home + r"\fw\build\Apollo.bin"),
                     "artifact_sha256": "a" * 64, "artifact_bytes": 42988},
        "run": {"mode": "uart", "probes": list(probes), "exit_code": exit_code,
                "status": "succeeded" if exit_code == 0 else "failed",
                "summary": "board confirmed the firmware",
                "started_at": "2026-09-24T01:01:01.000+00:00",
                "finished_at": "2026-09-24T01:01:15.000+00:00",
                "duration_ms": 14000},
        "checks": [
            {"name": "console", "status": "passed", "detail": "COM3 @ 115200"},
            {"name": "build", "status": "passed", "detail": "OK in 1.5s"},
            {"name": "flash", "status": "passed", "detail": "written"},
            {"name": "boot", "status": "passed",
             "detail": "banner: FLASHGATE-BOOT board=apollo-h743 git=4c8340e-dirty"},
            {"name": "identity", "status": "passed",
             "detail": "board=apollo-h743 git=4c8340e-dirty"},
            {"name": "probe:led-demo", "status": "passed", "detail": "5 steps"},
        ],
        "evidence": [
            {"kind": "uart-banner", "source": "COM3",
             "content": banner or ("FLASHGATE-BOOT board=apollo-h743 "
                                   "git=4c8340e-dirty rtos=FreeRTOS")},
        ],
        "coverage": {
            "statement": "the listed checks held on THIS board and bench "
                         "for THIS tree — nothing beyond the list",
            "verified": ["console", "build", "flash", "boot", "identity",
                         "probe:led-demo"],
            "not_verified": ["physical effects of probed features",
                             "any peripheral or behavior outside the "
                             "listed probes"],
            "profile_notes": ["LED visibility is not independently observed"],
        },
    }


@pytest.fixture
def fw(tmp_path):
    d = tmp_path / "fw"
    (records_mod.records_dir(d)).mkdir(parents=True)
    return d


def _write(fw, record, name=None, age_s=0.0):
    """Write one record; `age_s` back-dates its mtime so ordering tests
    do not depend on two writes landing in the same filesystem tick."""
    path = records_mod.records_dir(fw) / (name or record["record_id"] + ".json")
    path.write_text(json.dumps(record), encoding="utf-8")
    when = time.time() - age_s
    os.utime(path, (when, when))
    return path


class TestSelection:
    def test_latest_is_the_default_and_picks_the_newest(self, fw):
        _write(fw, _record("20260924T010101000-0-old"), "old.json",
               age_s=60)
        _write(fw, _record("20260924T020202000-0-new"), "new.json")
        paths = recordsexport.select_records(fw)
        assert [p.name for p in paths] == ["new.json"]
        loaded, unreadable = recordsexport.load_selected(fw)
        assert len(loaded) == 1 and loaded[0]["record_id"].endswith("new")
        assert unreadable == []

    def test_all_selects_every_retained_record(self, fw):
        for i in range(3):
            _write(fw, _record(f"20260924T01010{i}000-0-r{i}"), f"r{i}.json")
        loaded, _ = recordsexport.load_selected(fw, all_records=True)
        assert len(loaded) == 3

    def test_unreadable_record_is_reported_not_dropped(self, fw):
        _write(fw, _record("20260924T010101000-0-good"), "good.json")
        (records_mod.records_dir(fw) / "bad.json").write_text("{not json",
                                                              encoding="utf-8")
        loaded, unreadable = recordsexport.load_selected(fw, all_records=True)
        assert len(loaded) == 1
        assert unreadable == ["bad.json"]
        bundle, unreadable2 = recordsexport.build_export(fw, True, False)
        assert unreadable2 == ["bad.json"]
        assert bundle["export"]["unreadable"] == ["bad.json"]
        assert "bad.json" in recordsexport.render_markdown(bundle)

    def test_empty_archive_is_an_error_not_an_empty_file(self, fw):
        with pytest.raises(recordsexport.ExportError):
            recordsexport.export_records(fw, fw / "out.md")


class TestRedaction:
    """The acceptance bar: the redacted export greps clean."""

    def _leaks(self, text):
        import getpass
        import platform
        return [name for name, pat in {
            "drive-path": r"[A-Za-z]:[\\/]",
            "drive-letter": r"\b[A-Za-z]:",
            "home": re.escape(str(Path.home())),
            "user": re.escape(getpass.getuser()),
            "host": re.escape(platform.node()),
        }.items() if re.search(pat, text)]

    def test_markdown_redacted_has_zero_local_paths(self, fw, tmp_path):
        _write(fw, _record())
        out = recordsexport.export_records(fw, tmp_path / "r.md",
                                           redact=True)
        text = out.read_text(encoding="utf-8")
        assert self._leaks(text) == [], self._leaks(text)
        # basenames survive, behind the real placeholder: support still
        # sees WHICH file, and a mutation that swaps the placeholder for
        # an invented directory name cannot pass (mutation round, N3)
        assert "\<PATH\>/apollo-h743.yaml" in text
        assert "\<PATH\>/Apollo.bin" in text
    def test_json_redacted_has_zero_local_paths(self, fw, tmp_path):
        _write(fw, _record())
        out = recordsexport.export_records(fw, tmp_path / "r.json",
                                           redact=True)
        text = out.read_text(encoding="utf-8")
        assert self._leaks(text) == [], self._leaks(text)
        bundle = json.loads(text)
        assert bundle["export"]["redacted"] is True
        assert "apollo-h743.yaml" in text

    def test_as_is_export_keeps_the_paths_and_says_so(self, fw, tmp_path):
        _write(fw, _record())
        out = recordsexport.export_records(fw, tmp_path / "plain.md")
        text = out.read_text(encoding="utf-8")
        assert "未脱敏" in text
        assert str(Path.home()) in text or ":" in text

    def test_all_records_redacted_greps_clean(self, fw, tmp_path):
        for i in range(4):
            _write(fw, _record(f"20260924T01010{i}000-0-r{i}"), f"r{i}.json")
        out = recordsexport.export_records(fw, tmp_path / "all.md",
                                           all_records=True, redact=True)
        assert self._leaks(out.read_text(encoding="utf-8")) == []


class TestContent:
    def test_coverage_block_travels_with_the_export(self, fw, tmp_path):
        _write(fw, _record())
        out = recordsexport.export_records(fw, tmp_path / "r.md")
        text = out.read_text(encoding="utf-8")
        assert "coverage" in text
        assert "nothing beyond the list" in text
        assert "physical effects of probed features" in text
        assert "LED visibility is not independently observed" in text

    def test_evidence_travels_with_the_export(self, fw, tmp_path):
        _write(fw, _record(banner="FLASHGATE-BOOT board=apollo-h743 git=deadbee"))
        out = recordsexport.export_records(fw, tmp_path / "r.md")
        text = out.read_text(encoding="utf-8")
        assert "FLASHGATE-BOOT board=apollo-h743 git=deadbee" in text
        assert "uart-banner" in text

    def test_failed_record_exports_with_its_checks(self, fw, tmp_path):
        rec = _record("20260924T010101000-7-aaaabbbb", exit_code=7)
        rec["checks"][-1] = {"name": "probe:led-demo", "status": "failed",
                             "detail": "timeout waiting for /OK led0 "
                                       "state=BREATH/, last response: "
                                       "'OK led0 state=OFF'"}
        rec["coverage"]["failed_checks"] = ["probe:led-demo"]
        _write(fw, rec)
        out = recordsexport.export_records(fw, tmp_path / "r.md")
        text = out.read_text(encoding="utf-8")
        assert "exit 7" in text
        assert "OK led0 state=OFF" in text
        assert "失败项" in text

    def test_markdown_cannot_be_forged_by_record_fields(self, fw, tmp_path):
        rec = _record()
        rec["board"]["name"] = "[click](http://evil.example)"
        rec["board"]["mcu"] = "<img src=x onerror=alert(1)>"
        rec["checks"][0]["detail"] = "COM3\n| FAKE | PASSED | all good |"
        rec["evidence"][0]["content"] = "banner\n| INJECT | PASS |\n## forged"
        # the four numeric fields: the only ones the renderer used to
        # interpolate raw, so a tampered record could forge a PASS row
        # and a heading in markdown while JSON stayed inert (adversarial
        # M-1). Normal records carry ints; a record file is writable.
        rec["run"]["exit_code"] = "0 | 伪造 | x\n| ghost | 0 | also passed |"
        rec["run"]["duration_ms"] = "1\n## FORGED-HEADING"
        rec["firmware"]["artifact_bytes"] = "42988\n| ghost | 0 |"
        _write(fw, rec)
        out = recordsexport.export_records(fw, tmp_path / "r.md")
        text = out.read_text(encoding="utf-8")
        rows = [ln for ln in text.splitlines()
                if ln.startswith("| ") and "---" not in ln]
        # summary header + summary row + checks header + one row per check
        # — no forged "ghost" row from any injected field
        assert len(rows) == 3 + len(rec["checks"])
        # the injected text stays INSIDE its cell (escaped); it
        # does not open a row of its own
        assert not any(ln.startswith("| ghost")
                       for ln in text.splitlines())
        assert r"\| ghost \| 0 \|" in text
        for pattern in (r"(?<!\\)\[click\]\(http://evil",
                        r"(?<!\\)<img"):
            assert re.search(pattern, text) is None, pattern
        # evidence content is NOT rewritten (it is the board's own words)
        # but it stays contained inside its indented code block: nothing
        # it carries can open a line at column 0
        assert "\n    | INJECT | PASS |" in text
        assert not any(ln.startswith("## forged")
                       for ln in text.splitlines())
        assert not any(ln.startswith("| INJECT")
                       for ln in text.splitlines())

    def test_pipe_in_a_detail_is_escaped_not_truncated(self, fw, tmp_path):
        # L-2: the pipe escape is what keeps a detail (a COM error line,
        # a probe transcript) inside its own cell instead of being
        # truncated by the table renderer
        rec = _record()
        rec["checks"][0]["detail"] = "COM3 | FAKE | PASSED | all good |"
        _write(fw, rec)
        text = recordsexport.export_records(
            fw, tmp_path / "r.md").read_text(encoding="utf-8")
        row = next(ln for ln in text.splitlines()
                   if ln.startswith("| console |"))
        assert "\| FAKE \| PASSED \|" in row

    def test_evidence_sits_in_a_real_code_block(self, fw, tmp_path):
        # runtime audit (N3): the evidence used to hang off a LIST ITEM
        # with a 4-space indent. Inside a list item an indented code
        # block needs the content column plus four (six after "- "), so
        # four spaces was only a lazy paragraph continuation and the
        # content rendered as inline markdown — a hostile firmware could
        # forge a heading or a clickable link inside the export. The
        # block must therefore follow a blank line at TOP level.
        rec = _record()
        rec["evidence"][0]["content"] = "## Forged Heading\n[x](http://evil.example)"
        _write(fw, rec)
        text = recordsexport.export_records(
            fw, tmp_path / "r.md").read_text(encoding="utf-8")
        lines = text.splitlines()
        idx = next(i for i, ln in enumerate(lines)
                   if ln.startswith("    ## Forged Heading"))
        assert lines[idx - 1] == "", "evidence block must start after a blank line"
        header = lines[idx - 2]
        assert not header.lstrip().startswith("- "), \
            f"evidence must not hang off a list item: {header!r}"
        assert header.startswith("**") and header.endswith("**")
        # the whole block is indented: nothing in it can open a line at
        # column 0
        block = []
        for ln in lines[idx:]:
            if ln.strip() and not ln.startswith("    "):
                break
            block.append(ln)
        assert all(ln.startswith("    ") for ln in block if ln.strip())

class TestRenderers:
    def test_table_lists_newest_first_and_caps(self, fw):
        for i in range(25):
            _write(fw, _record(f"20260924T01010{i:02d}00-0-r{i}"),
                   f"r{i:02d}.json")
        text = recordsexport.render_table(fw, limit=20)
        assert "25 retained" in text and "newest 20 shown" in text
        assert "and 5 older" in text
        # the cap is EXECUTED, not just announced: the header used to be
        # computed from the full list, so removing the slice still
        # claimed "newest 20 shown" while rendering all 25
        # (mutation round, N3). Record rows are the 2-space-indented ones
        # (their detail lines are indented deeper).
        rows = [ln for ln in text.splitlines()
                if ln.startswith("  ") and not ln.startswith("   ")
                and not ln.lstrip().startswith("…")]
        assert len(rows) == 20, f"cap not applied: {len(rows)} rows rendered"
        assert rows[0].strip().endswith("r24")
        assert rows[-1].strip().endswith("r05")

    def test_table_on_empty_archive_tells_you_what_to_do(self, fw):
        text = recordsexport.render_table(fw)
        assert "no verification records" in text
        assert "flashgate verify" in text

    def test_json_bundle_is_self_describing(self, fw):
        _write(fw, _record())
        bundle, _ = recordsexport.build_export(fw, False, False)
        assert bundle["export"]["selector"] == "latest"
        assert bundle["export"]["record_count"] == 1
        assert bundle["export"]["redacted"] is False
        assert bundle["records"][0]["coverage"]["statement"]


class TestCli:
    def test_list_and_export(self, fw, tmp_path, capsys, monkeypatch):
        from flashgate import cli
        from tests.test_cli import make_board
        board = make_board(tmp_path)
        monkeypatch.setattr(board, "firmware_dir", fw)
        _write(fw, _record())
        assert cli.main(["--board", str(board.yaml_path), "records"]) == 0
        assert "verification records" in capsys.readouterr().out

        out = tmp_path / "r.md"
        assert cli.main(["--board", str(board.yaml_path), "records",
                         "--export", str(out)]) == 0
        assert out.is_file()

    def test_redact_without_export_warns(self, fw, tmp_path, capsys,
                                         monkeypatch):
        # L-1: the table view prints the records directory path, so a
        # silent --redact would look like it protected that output
        from flashgate import cli
        from tests.test_cli import make_board
        board = make_board(tmp_path)
        monkeypatch.setattr(board, "firmware_dir", fw)
        _write(fw, _record())
        rc = cli.main(["--board", str(board.yaml_path), "records",
                       "--redact"])
        assert rc == 0
        assert "--redact has no effect without --export" in             capsys.readouterr().out

    def test_export_failure_is_exit_6(self, fw, tmp_path, capsys, monkeypatch):
        from flashgate import cli
        from tests.test_cli import make_board
        board = make_board(tmp_path)
        monkeypatch.setattr(board, "firmware_dir", fw)
        rc = cli.main(["--board", str(board.yaml_path), "records",
                       "--export", str(tmp_path / "no-such-dir" / "r.md")])
        assert rc == cli.EXIT_ENV
        assert "could not export" in capsys.readouterr().out

    def test_empty_archive_is_exit_6(self, fw, tmp_path, capsys, monkeypatch):
        from flashgate import cli
        from tests.test_cli import make_board
        board = make_board(tmp_path)
        monkeypatch.setattr(board, "firmware_dir", fw)
        rc = cli.main(["--board", str(board.yaml_path), "records",
                       "--export", str(tmp_path / "r.md")])
        assert rc == cli.EXIT_ENV
        assert "could not export" in capsys.readouterr().out

    @pytest.mark.skipif(sys.platform != "win32",
                        reason="NUL is a Windows device name")
    def test_device_name_is_not_a_silent_success(self, fw, tmp_path, capsys,
                                                 monkeypatch):
        from flashgate import cli
        from tests.test_cli import make_board
        board = make_board(tmp_path)
        monkeypatch.setattr(board, "firmware_dir", fw)
        _write(fw, _record())
        rc = cli.main(["--board", str(board.yaml_path), "records",
                       "--export", "NUL"])
        assert rc == cli.EXIT_ENV
        assert "could not export" in capsys.readouterr().out
