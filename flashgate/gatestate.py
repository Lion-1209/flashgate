"""Gate state: fingerprint the firmware tree, remember the last PASS.

The Stop hook only re-runs the (slow) hardware verify when the firmware tree
actually changed since the last passing run. The fingerprint covers HEAD,
the full diff against HEAD, and the untracked-file list — so any content
change to a watched file produces a new fingerprint.

Escalation: after MAX_CONSECUTIVE_BLOCKS failed blocks for the SAME
fingerprint, the gate releases with a warning instead of blocking forever
(coderio VerifyGate semantics: never wedge the session, never silently
give up).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

STATE_DIR = ".flashgate"
STATE_FILE = "state.json"
MAX_CONSECUTIVE_BLOCKS = 2

DEFAULT_WATCH = [
    "*.c", "*.h", "*.s", "*.ld", "*.ioc",
    "CMakeLists.txt", "*.cmake", "cmake/*",
]


def _git(args: list[str], cwd: Path) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=20, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def tree_fingerprint(fw_dir: Path) -> str:
    """Content-identity of the working tree (not just HEAD: dirty trees with
    different edits must NOT share a fingerprint)."""
    head = _git(["rev-parse", "HEAD"], fw_dir)
    diff = _git(["diff", "HEAD"], fw_dir)
    status = _git(["status", "--porcelain"], fw_dir)
    blob = "\x00".join((head, diff, status))
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()


def watched_paths(fw_dir: Path, patterns: list[str]) -> list[str]:
    """Working-tree changes that match the watch globs (gate triggers)."""
    status = _git(["status", "--porcelain"], fw_dir)
    watched: list[str] = []
    for line in status.splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:                     # rename: judge by the new name
            path = path.split(" -> ")[1]
        if path and any(fnmatch(path, pat) for pat in patterns):
            watched.append(path)
    return watched


def state_path(fw_dir: Path) -> Path:
    return fw_dir / STATE_DIR / STATE_FILE


def load_state(fw_dir: Path) -> dict:
    try:
        return json.loads(state_path(fw_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(fw_dir: Path, **fields) -> None:
    path = state_path(fw_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated": datetime.now(timezone.utc).isoformat(), **fields}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
