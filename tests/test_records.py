"""Verification records (Stage 1): the audit trail behind every verify run.

A record must exist for FAILED runs too — the interesting ones — and a
step that never executed is `skipped`, never silently absent: the
record-level echo of the gate's core rule.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from flashgate import records


def make_journal():
    j = records.VerifyJournal(
        ["console", "build", "flash", "boot", "identity", "probe:led-demo"],
        mode="uart", probe_names=["all"])
    j.note("firmware", {"git_sha": "abc1234", "tree_fingerprint": "fp" * 32})
    return j


class TestJournal:
    def test_failed_run_marks_later_steps_skipped(self):
        j = make_journal()
        j.check("console", "passed", "COM3")
        j.check("build", "failed", "ninja: error")
        rec = j.to_record(_board_stub(), 1, "failed", "build failed")
        by_name = {c["name"]: c for c in rec["checks"]}
        assert by_name["build"]["status"] == "failed"
        for later in ("flash", "boot", "identity", "probe:led-demo"):
            assert by_name[later]["status"] == "skipped", later
            assert "earlier step failed" in by_name[later]["detail"]

    def test_full_pass_has_no_skipped(self):
        j = make_journal()
        for name in j.plan:
            j.check(name, "passed")
        rec = j.to_record(_board_stub(), 0, "succeeded", "ok")
        assert all(c["status"] == "passed" for c in rec["checks"])
        assert len(rec["checks"]) == len(j.plan)

    def test_off_plan_check_appended_not_dropped(self):
        j = make_journal()
        j.check("probes", "failed", "cannot load probes")
        rec = j.to_record(_board_stub(), 6, "incomplete", "env")
        names = [c["name"] for c in rec["checks"]]
        assert "probes" in names and names.count("probes") == 1

    def test_evidence_and_metadata_survive(self):
        j = make_journal()
        j.add_evidence("uart-banner", "COM3", "FLASHGATE-BOOT board=x git=y")
        rec = j.to_record(_board_stub(), 0, "succeeded", "ok")
        assert rec["evidence"][0]["kind"] == "uart-banner"
        assert "FLASHGATE-BOOT" in rec["evidence"][0]["content"]
        assert rec["firmware"]["git_sha"] == "abc1234"
        assert rec["tool"]["name"] == "flashgate"
        assert rec["run"]["exit_code"] == 0

    def test_invalid_status_rejected(self):
        j = make_journal()
        with pytest.raises(ValueError):
            j.check("build", "green")

    def test_evidence_content_bounded(self):
        j = make_journal()
        j.add_evidence("console-tail", "COM3", "x" * 100_000)
        rec = j.to_record(_board_stub(), 3, "timed_out", "timeout")
        assert len(rec["evidence"][0]["content"]) == records._EVIDENCE_MAX_CHARS


def _board_stub():
    return SimpleNamespace(name="t", mcu="m", yaml_path=Path("b.yaml"))


class TestPersistence:
    def test_write_and_latest_roundtrip(self, tmp_path):
        j = make_journal()
        rec = j.to_record(_board_stub(), 7, "failed", "probe failed")
        path = records.write_record(rec, tmp_path, fingerprint="ab" * 32)
        assert path.parent == tmp_path / ".flashgate" / "records"
        assert "-7-" in path.name
        loaded = records.latest_record(tmp_path)
        assert loaded["run"]["exit_code"] == 7
        assert loaded["record_id"] == path.stem

    def test_latest_none_when_empty(self, tmp_path):
        assert records.latest_record(tmp_path) is None

    def test_records_dir_outside_fingerprint(self, tmp_path, git_repo):
        # The gate's own state writes must not invalidate a cached PASS —
        # records land next to state.json under .flashgate/, which the
        # untracked digest excludes.
        from flashgate import gatestate
        before = gatestate.tree_fingerprint(git_repo)
        j = make_journal()
        records.write_record(j.to_record(_board_stub(), 0, "succeeded", "ok"),
                             git_repo, fingerprint="ff" * 32)
        assert gatestate.tree_fingerprint(git_repo) == before

    def test_sha256_file_matches_hashlib(self, tmp_path):
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"hello flashgate" * 10)
        digest, size = records.sha256_file(blob)
        assert digest == hashlib.sha256(b"hello flashgate" * 10).hexdigest()
        assert size == blob.stat().st_size


class TestStatusWord:
    def test_matches_results_envelope_table(self):
        # records must stay stdlib-only (CLI dep; pydantic is an mcp extra),
        # so the status table is duplicated — this pins the two together.
        results = pytest.importorskip("flashgate.results")
        for rc in range(8):
            assert records.status_word(rc) == results._EXIT_STATUS[rc], rc

    def test_unknown_rc_defaults_failed(self):
        assert records.status_word(99) == "failed"


class TestVersionInFingerprint:
    """Adversarial-review F2: a flashgate upgrade changes verification
    semantics (probe/assert logic) — the fingerprint must not let a cache
    PASS from the OLD tool version survive into the NEW one."""

    def test_version_change_flips_fingerprint(self, git_repo, monkeypatch):
        from flashgate import gatestate
        before = gatestate.tree_fingerprint(git_repo)
        monkeypatch.setattr(gatestate, "_FGATE_VERSION", "999.999.999-test")
        assert gatestate.tree_fingerprint(git_repo) != before
        monkeypatch.setattr(gatestate, "_FGATE_VERSION", "0.0.1")
        assert gatestate.tree_fingerprint(git_repo) != before

    def test_status_line_for_state_dir_excluded(self, git_repo):
        # First-ever .flashgate write must not flip the fingerprint
        # (the '?? .flashgate' status line is filtered out).
        from flashgate import gatestate
        before = gatestate.tree_fingerprint(git_repo)
        (git_repo / ".flashgate").mkdir()
        (git_repo / ".flashgate" / "state.json").write_text("{}", encoding="utf-8")
        assert gatestate.tree_fingerprint(git_repo) == before


class TestStateDirSubdirLayout:
    """Firmware dir as a SUBDIR of a larger repo: status emits
    repo-root-relative paths ('?? fw/.flashgate/') — the filter must
    still keep the gate's own state out of the fingerprint."""

    def test_subdir_state_dir_does_not_flip_fingerprint(self, tmp_path):
        import subprocess as sp
        from flashgate import gatestate
        root = tmp_path / "repo"
        fw = root / "fw"
        fw.mkdir(parents=True)
        (fw / "main.c").write_text("int main(void){return 0;}\n")
        for args in (("init", "-b", "main"), ("config", "user.email", "t@e.c"),
                     ("config", "user.name", "t"), ("add", "-A"),
                     ("commit", "-m", "init")):
            sp.run(["git", *args], cwd=root, capture_output=True, check=True)
        before = gatestate.tree_fingerprint(fw)
        (fw / ".flashgate" / "records").mkdir(parents=True)
        (fw / ".flashgate" / "records" / "r.json").write_text("{}", encoding="utf-8")
        assert gatestate.tree_fingerprint(fw) == before, \
            "status line '?? fw/.flashgate/' must be filtered too"


class TestCrashNetAndUniqueness:
    """Adversarial review F1/F2: crashes must leave a record and keep the
    exit-code contract; same-second writes must not overwrite."""

    def test_broken_banner_regex_rejected_at_load(self, tmp_path):
        import pytest
        from flashgate.board import BoardError, load_board
        fw = tmp_path / "fw"
        fw.mkdir()
        p = tmp_path / "b.yaml"
        p.write_text(
            "board: t\nfirmware:\n  dir: fw\n  build: b\n  artifact: a.bin\n"
            "serial:\n  banner: 'BOOT board=(?P<board>\S+'\n",  # broken regex
            encoding="utf-8")
        with pytest.raises(BoardError, match="banner pattern"):
            load_board(p)

    def test_build_timeout_crash_keeps_contract_and_records(self, tmp_path, monkeypatch):
        import subprocess as sp
        from flashgate import cli
        from tests.test_cli import BODY, make_board
        board = make_board(tmp_path)
        monkeypatch.setattr(cli, "_console_port", lambda b: (None, "no serial"))

        def explode(b, j=None):
            raise sp.TimeoutExpired(cmd="ninja", timeout=300)

        monkeypatch.setattr(cli, "_build", explode)
        rc = cli.cmd_verify(board, None, "swd")
        assert rc == cli.EXIT_ENV                    # 6, not a raw crash 1
        rec = records.latest_record(board.firmware_dir)
        assert rec is not None, "a crashed run must still leave a record"
        assert rec["run"]["exit_code"] == 6
        names = [c["name"] for c in rec["checks"]]
        assert "verify" in names                     # the crash entry
        v = next(c for c in rec["checks"] if c["name"] == "verify")
        assert "TimeoutExpired" in v["detail"]

    def test_serial_crash_mid_verify_keeps_contract(self, tmp_path, monkeypatch):
        import serial as pyserial
        from types import SimpleNamespace
        from flashgate import cli
        from tests.test_cli import make_board
        board = make_board(tmp_path)
        monkeypatch.setattr(cli, "_console_port", lambda b: ("COMX", "stub"))
        monkeypatch.setattr(cli.serialmon, "open_flush",
                            lambda *a, **k: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(cli, "_build", lambda b, j=None: cli.EXIT_OK)
        monkeypatch.setattr(cli, "cmd_flash", lambda b: cli.EXIT_OK)

        def yank(*a, **k):
            raise pyserial.SerialException("device unplugged")

        monkeypatch.setattr(cli.serialmon, "wait_on", yank)
        assert cli.cmd_verify(board, None, "uart") == cli.EXIT_ENV
        assert records.latest_record(board.firmware_dir) is not None

    def test_same_second_writes_do_not_overwrite(self, tmp_path):
        j = records.VerifyJournal(["build"], mode="swd", probe_names=None)
        rec = j.to_record(_board_stub(), 2, "failed", "x")
        p1 = records.write_record(rec, tmp_path, fingerprint="ab" * 32)
        p2 = records.write_record(dict(rec), tmp_path, fingerprint="ab" * 32)
        assert p1 != p2 and p1.exists() and p2.exists()
        import json as _json
        # compare by path content, NOT latest_record's mtime order —
        # same-tick writes make that ordering flaky on some filesystems
        assert _json.loads(p2.read_text(encoding="utf-8"))["record_id"] == p2.stem


class TestRetention:
    """Architecture-doc open question #2: records must not grow unbounded."""

    def test_prune_keeps_newest_cap(self, tmp_path, monkeypatch):
        from flashgate import records as rec
        monkeypatch.setattr(rec, "MAX_RECORDS", 5)
        for i in range(8):
            j = rec.VerifyJournal(["build"], mode="swd", probe_names=None)
            j.check("build", "passed")
            rec.write_record(j.to_record(_board_stub(), i % 8, "failed", "x"),
                             tmp_path, fingerprint=f"{i:064d}")
        files = sorted(rec.records_dir(tmp_path).glob("*.json"))
        assert len(files) == 5
        kept = [json.loads(f.read_text(encoding="utf-8"))["run"]["exit_code"]
                for f in files]
        assert kept == [3, 4, 5, 6, 7]      # the five NEWEST by mtime

    def test_prune_never_breaks_writing(self, tmp_path, monkeypatch):
        import pytest
        from flashgate import records as rec
        monkeypatch.setattr(rec, "MAX_RECORDS", 2)
        for i in range(4):
            j = rec.VerifyJournal(["build"], mode="swd", probe_names=None)
            j.check("build", "passed")
            path = rec.write_record(
                j.to_record(_board_stub(), 0, "succeeded", "x"),
                tmp_path, fingerprint=f"{i:064d}")
            assert path.exists()


class TestPruneGuards:
    """Adversarial-round fixes: the just-written record survives an NTP
    clock rollback (R3); retention stays best-effort, never fatal."""

    def test_just_written_survives_clock_rollback(self, tmp_path, monkeypatch):
        import os
        from datetime import datetime, timedelta
        from flashgate import records as rec
        monkeypatch.setattr(rec, "MAX_RECORDS", 3)
        # three records whose mtimes are ONE HOUR IN THE FUTURE
        for i in range(3):
            j = rec.VerifyJournal(["build"], mode="swd", probe_names=None)
            j.check("build", "passed")
            p = rec.write_record(j.to_record(_board_stub(), 0, "succeeded", "x"),
                                 tmp_path, fingerprint=f"{i:064d}")
            future = datetime.now() + timedelta(hours=1)
            os.utime(p, (future.timestamp(), future.timestamp()))
        # a fresh write under the rolled-back clock must survive its own prune
        j = rec.VerifyJournal(["build"], mode="swd", probe_names=None)
        j.check("build", "passed")
        fresh = rec.write_record(j.to_record(_board_stub(), 7, "failed", "x"),
                                 tmp_path, fingerprint="ff" * 32)
        assert fresh.exists(), "the just-written record must never be pruned"
        # (latest_record sorts by mtime, so the future-stamped files win —
        # correct behavior under clock skew; the invariant is survival)
        import json as _json
        assert _json.loads(fresh.read_text(encoding="utf-8"))["record_id"] == fresh.stem


class TestLastRegistryIsThreadLocal:
    """N0-7: the write registry is thread-local storage. Every consumer
    (bench worker thread, MCP tool thread) writes and reads on its own
    thread, so a concurrent run's record can never land in another
    run's read window — the record-association race the business plan
    named is closed at the storage layer."""

    def test_other_thread_cannot_inherit(self, tmp_path):
        import threading
        from flashgate import records
        fw = tmp_path / "fw"
        fw.mkdir()
        records.write_record({"run": {"exit_code": 0, "summary": "t"}},
                             fw)
        mine = records.current_last()
        assert mine is not None and mine["record"]["run"]["exit_code"] == 0
        seen = {}
        th = threading.Thread(
            target=lambda: seen.update(last=records.current_last()))
        th.start()
        th.join()
        assert seen["last"] is None, "a fresh thread must start empty"


class TestCoverageBlock:
    """N1: every record carries a 'what this PASS proves' statement —
    a green record must never read as 'all functionality passed'."""

    @staticmethod
    def _board_notes(notes):
        from types import SimpleNamespace
        return SimpleNamespace(coverage_notes=tuple(notes))

    def test_green_with_probes(self):
        from flashgate import records
        rec = {"checks": [
            {"name": "build", "status": "passed"},
            {"name": "boot", "status": "passed"},
            {"name": "probe:led-demo", "status": "passed"},
        ]}
        cov = records.build_coverage(self._board_notes(["n1"]), rec)
        assert cov["verified"] == ["build", "boot", "probe:led-demo"]
        assert "n1" in cov["profile_notes"]
        assert not any("no probes ran" in x for x in cov["not_verified"])
        # the physical-effects caveat is ALWAYS present
        assert any("physical" in x for x in cov["not_verified"])
        assert "beyond the list" in cov["statement"]

    def test_no_probes_says_functional_untested(self):
        from flashgate import records
        rec = {"checks": [{"name": "build", "status": "passed"},
                          {"name": "boot", "status": "passed"}]}
        cov = records.build_coverage(self._board_notes([]), rec)
        assert any("no probes were run" in x for x in cov["not_verified"])

    def test_skipped_probes_are_not_claimed_ran(self):
        # audit M-1: probes planned but never executed (build failed
        # first — the Stop hook's most common failure path) must be
        # described as never-executed, not "ran but none passed"
        from flashgate import records
        rec = {"checks": [
            {"name": "console", "status": "passed"},
            {"name": "build", "status": "failed"},
            {"name": "probe:led-demo", "status": "skipped"},
        ]}
        cov = records.build_coverage(self._board_notes([]), rec)
        assert any("never executed" in x for x in cov["not_verified"])
        assert not any("none passed" in x for x in cov["not_verified"])
        assert cov["skipped_checks"] == ["probe:led-demo"]
        # M2 pin (glm3 v3): the never-executed line must not assert a
        # specific cause — "an earlier step failed" is false on the crash
        # path; restoring that clause must go red, presence-only checks
        # let it slip through once already
        assert all("earlier step failed" not in x
                   for x in cov["not_verified"])

    def test_failed_probe_is_not_claimed_unrun(self):
        # adversarial M2: probes that RAN and failed must not be described
        # as "no probes ran" — failed_checks already names them
        from flashgate import records
        rec = {"checks": [{"name": "build", "status": "passed"},
                          {"name": "probe:led-demo", "status": "failed"}]}
        cov = records.build_coverage(self._board_notes([]), rec)
        assert not any("no probes were run" in x for x in cov["not_verified"])
        assert any("none passed" in x for x in cov["not_verified"])


    def test_failed_and_skipped_named(self):
        from flashgate import records
        rec = {"checks": [
            {"name": "build", "status": "passed"},
            {"name": "flash", "status": "failed"},
            {"name": "boot", "status": "skipped"},
        ]}
        cov = records.build_coverage(self._board_notes([]), rec)
        assert cov["failed_checks"] == ["flash"]
        assert cov["skipped_checks"] == ["boot"]
        assert "flash" not in cov["verified"]
