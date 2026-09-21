"""Verification records: one JSON per verify run — the audit trail.

A record is what turns "the gate said PASS" into evidence someone can
audit later: which tool version ran, which tree and artifact it verified,
what the board actually said, and the per-check verdicts. Records live
under ``<firmware>/.flashgate/records/`` (the state dir is already
outside the tree fingerprint, so writing a record never invalidates the
cached PASS it may describe).

Record writing is strictly auxiliary: a failure to persist must never
change the verification outcome (the CLI wraps this in try/except).
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__

if TYPE_CHECKING:                     # stdlib-only at runtime (CLI dep);
    from .board import Board          # Board is annotation-only

RECORDS_SCHEMA_VERSION = "1.1"   # 1.1: coverage block (what this PASS proves)
RECORDS_DIRNAME = ".flashgate/records"

# Retention: records are small (a few KB each), but a bench left running
# for months would grow the dir without bound (architecture doc open
# question #2). Oldest files are pruned past the cap; the newest PASS
# and the running audit window are always preserved well inside it.
MAX_RECORDS = 500

# In-process registry of the record written by the LAST write_record call.
# The MCP server (which runs cmd_verify in-process) uses it to attach THIS
# run's record — never a stale one picked blindly by mtime.
# THREAD-LOCAL since 2026-09-21: every consumer (bench worker thread, MCP
# tool thread) writes and reads on the SAME thread, so per-thread storage
# makes cross-run attachment structurally impossible — a concurrent
# verify's record can never land in another run's read window (the
# business plan's record-association race, closed at the storage layer).
import threading as _threading

_local = _threading.local()


def current_last() -> dict | None:
    """This thread's most recent write_record result (path/record/fw_dir).

    None in a thread that never wrote one — a fresh consumer thread
    cannot inherit another thread's record by accident."""
    return getattr(_local, "last", None)

_EVIDENCE_MAX_CHARS = 4000        # a banner is one line; transcripts get tails

# Envelope status per CLI exit code. Kept in sync with results._EXIT_STATUS
# (this module must stay stdlib-only: the CLI depends on it, and pydantic
# is an MCP-extra dependency). tests/test_records.py pins the two tables
# together; tests/test_docs.py pins the key domain to exactly 0-7.
_EXIT_STATUS_WORD: dict[int, str] = {
    0: "succeeded", 1: "failed", 2: "failed", 3: "timed_out",
    4: "failed", 5: "failed", 6: "incomplete", 7: "failed",
}


def status_word(exit_code: int) -> str:
    return _EXIT_STATUS_WORD.get(exit_code, "failed")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_file(path: Path, cap: int = 64 * 1024 * 1024) -> tuple[str, int]:
    """(sha256, size) of a file; content capped like the fingerprint."""
    h = hashlib.sha256()
    size = path.stat().st_size
    remaining = min(size, cap)
    with path.open("rb") as fh:
        while remaining > 0:
            chunk = fh.read(min(1 << 20, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest(), size


class VerifyJournal:
    """Collects per-check verdicts and evidence while a verify runs.

    The caller passes the PLAN (the checks this run intends to make);
    steps that never execute — because an earlier step failed — are
    serialized as ``skipped`` with the reason. A check that ran and did
    not hold is ``failed``; ``passed`` means it executed and held. This
    is the record-level echo of the gate's core rule: not-executed is
    never a pass.
    """

    def __init__(self, plan: list[str], *, mode: str,
                 probe_names: list[str] | None,
                 fingerprint: str = ""):
        self.plan = list(plan)
        self.mode = mode
        self.probe_names = list(probe_names) if probe_names is not None else None
        self.fingerprint = fingerprint    # pre-run tree identity (see F8)
        self.started_at = _utcnow_iso()
        self._t0 = time.monotonic()
        self.checks: dict[str, dict] = {}
        self.evidence: list[dict] = []
        self.meta: dict[str, object] = {}

    def check(self, name: str, status: str, detail: str = "",
              duration_ms: int | None = None) -> None:
        """Record a verdict for a planned step.

        status: "passed" | "failed" | "skipped" (explicitly not run, with
        reason). Re-recording a name overwrites — the LAST verdict wins
        (e.g. a retry inside one verify run).
        """
        if status not in ("passed", "failed", "skipped"):
            raise ValueError(f"invalid check status: {status!r}")
        entry: dict = {"name": name, "status": status}
        if detail:
            entry["detail"] = detail[:500]
        if duration_ms is not None:
            entry["duration_ms"] = duration_ms
        self.checks[name] = entry

    def ensure_failed(self, name: str, detail: str = "") -> None:
        """Safety net: a step whose runner returned failure but recorded
        no verdict (stubbed or exotic path) still reads as failed in the
        record — never silently skipped."""
        if name not in self.checks:
            self.check(name, "failed", detail)

    def ensure_passed(self, name: str, detail: str = "") -> None:
        """Mirror of ensure_failed: a step that returned success has
        demonstrably executed — if it recorded no richer verdict, still
        mark it passed rather than letting it read as skipped."""
        if name not in self.checks:
            self.check(name, "passed", detail)

    def add_evidence(self, kind: str, source: str, content: str) -> None:
        """Raw material backing a verdict: the banner line, the signature
        hex, a console tail, a probe transcript."""
        self.evidence.append({
            "kind": kind,
            "source": source,
            "content": content[:_EVIDENCE_MAX_CHARS],
        })

    def note(self, key: str, value: object) -> None:
        self.meta[key] = value

    def to_record(self, board: "Board", exit_code: int,
                  status_word: str, summary: str) -> dict:
        finished = _utcnow_iso()
        duration_ms = int((time.monotonic() - self._t0) * 1000)

        checks: list[dict] = []
        recorded_failed = False
        for step in self.plan:
            entry = self.checks.get(step)
            if entry is None:
                reason = ("earlier step failed — not executed"
                          if recorded_failed else "not executed")
                checks.append({"name": step, "status": "skipped",
                               "detail": reason})
                continue
            if entry["status"] == "failed":
                recorded_failed = True
            checks.append(entry)
        # Defensive: checks recorded outside the plan still belong in the
        # record (e.g. the console-resolve step of a UART run).
        for name, entry in self.checks.items():
            if name not in self.plan:
                checks.append(entry)

        record: dict = {
            "schema_version": RECORDS_SCHEMA_VERSION,
            "kind": "flashgate.verify",
            "record_id": None,          # filled by write_record (needs fp)
            "tool": {
                "name": "flashgate",
                "version": __version__,
                "python": platform.python_version(),
                "platform": sys.platform,
            },
            "board": {
                "name": board.name,
                "mcu": board.mcu,
                "profile": str(board.yaml_path),
            },
            "run": {
                "mode": self.mode,
                "probes": self.probe_names,
                "exit_code": exit_code,
                "status": status_word,
                "summary": summary[:300],
                "started_at": self.started_at,
                "finished_at": finished,
                "duration_ms": duration_ms,
            },
            "checks": checks,
            "evidence": self.evidence,
        }
        record.update(self.meta)        # firmware.*, board extras, ...
        return record


_PHYSICAL_EFFECTS_CAVEAT = (
    "physical effects of probed features (register/console readbacks are "
    "the firmware's own software observations, not independent physical "
    "measurements)")

_NOT_VERIFIED_BASE = [
    _PHYSICAL_EFFECTS_CAVEAT,
    "any peripheral or behavior outside the listed probes",
    "environmental conditions (temperature/voltage margins, timing under "
    "load)",
]


def build_coverage(board, record: dict) -> dict:
    """The 'what this PASS proves' statement (T1 red line: a green record
    must never read as 'all functionality passed').

    Auto-derived from the record's own checks — verified lists what HELD,
    not_verified names the standing blind spots every flashgate verdict
    carries, and profile_notes lets a board add bench-specific caveats
    (coverage.notes in the yaml)."""
    checks = record.get("checks", [])
    passed = [c.get("name") for c in checks if c.get("status") == "passed"]
    failed = [c.get("name") for c in checks if c.get("status") == "failed"]
    skipped = [c.get("name") for c in checks if c.get("status") == "skipped"]
    probe_names_all = [c.get("name") for c in checks
                       if (c.get("name") or "").startswith("probe:")]
    probes_passed = [n for n in probe_names_all if n in passed]
    not_verified = list(_NOT_VERIFIED_BASE)
    if not probe_names_all:
        not_verified.insert(
            0, "functional behavior entirely — no probes were run; this "
            "record proves boot identity only")
    elif not probes_passed:
        # probes RAN but none passed — "no probes ran" would be a lie
        # contradicted by failed_checks in the same block (adversarial M2)
        not_verified.insert(
            0, "functional behavior — probes ran but none passed (see "
            "failed_checks); this record proves nothing functional")
    cov: dict = {
        "statement": "the listed checks held on THIS board and bench for "
                     "THIS tree — nothing beyond the list",
        "verified": [n for n in passed if n],
        "not_verified": not_verified,
        "profile_notes": list(getattr(board, "coverage_notes", ()) or ()),
    }
    if failed:
        cov["failed_checks"] = [n for n in failed if n]
    if skipped:
        cov["skipped_checks"] = [n for n in skipped if n]
    return cov


def records_dir(fw_dir: Path) -> Path:
    return fw_dir / RECORDS_DIRNAME


def write_record(record: dict, fw_dir: Path, fingerprint: str = "") -> Path:
    """Persist one record; returns its path. Millisecond timestamp plus an
    existence-checked suffix: two runs can land in the same second with the
    same verdict and fingerprint (adversarial review F2) — silent overwrite
    would destroy audit history."""
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}"
    exit_code = record.get("run", {}).get("exit_code", -1)
    fp8 = (fingerprint or "")[:8] or "nofp"
    directory = records_dir(fw_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{ts}-{exit_code}-{fp8}"
    path = directory / f"{stem}.json"
    n = 1
    while path.exists():
        path = directory / f"{stem}-{n}.json"
        n += 1
    record["record_id"] = path.stem
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    _local.last = {"path": path, "record": record, "fw_dir": fw_dir,
                   "at": time.monotonic()}
    _prune(fw_dir, exempt=path)
    return path


def _prune(fw_dir: Path, keep: int | None = None,
           exempt: Path | None = None) -> None:
    """Keep only the newest `keep` records (mtime order). Best-effort:
    a file that cannot be deleted is skipped — retention must never
    break record writing. `keep=None` resolves MAX_RECORDS at call time
    (a plain default arg would freeze the constant at import)."""
    if keep is None:
        keep = MAX_RECORDS
    directory = records_dir(fw_dir)
    try:
        # Secondary key = filename (its timestamp prefix): rapid writes
        # land on the same mtime tick and a glob-order tiebreak could
        # judge the JUST-WRITTEN file as oldest (flake found by the
        # mutation round's baseline runs).
        candidates = sorted(directory.glob("*.json"),
                             key=lambda p: (p.stat().st_mtime, p.name))
        excess = candidates[:-keep] if keep else []
        if exempt is not None:
            # After an NTP clock rollback every EXISTING file looks
            # "newer" than this one — the just-written record may land in
            # the excess list; drop it from the DELETION list only (it
            # still counts toward the cap, so steady state stays exact).
            excess = [f for f in excess if f != exempt]
    except OSError:
        return
    for stale in excess:
        try:
            stale.unlink()
        except OSError:
            pass


def latest_record(fw_dir: Path) -> dict | None:
    """Most recent record (by mtime), or None when none exist yet."""
    directory = records_dir(fw_dir)
    if not directory.is_dir():
        return None
    candidates = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        return None
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def summarize_record(record: dict) -> str:
    """One-line human digest for CLI/MCP surfaces."""
    run = record.get("run", {})
    checks = record.get("checks", [])
    passed = sum(1 for c in checks if c.get("status") == "passed")
    total = len(checks)
    return (f"{run.get('status', '?')} exit={run.get('exit_code')} "
            f"[{passed}/{total} checks] {run.get('summary', '')}".strip())
