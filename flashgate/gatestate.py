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

from . import __version__ as _FGATE_VERSION

STATE_DIR = ".flashgate"
STATE_FILE = "state.json"
MAX_CONSECUTIVE_BLOCKS = 2

DEFAULT_WATCH = [
    "*.c", "*.h", "*.s", "*.ld", "*.ioc",
    "CMakeLists.txt", "*.cmake", "cmake/*",
]


def _git(args: list[str], cwd: Path) -> str:
    # errors="surrogateescape": `ls-files -z` emits raw UTF-8 path bytes and
    # text=True alone would decode with the ANSI code page (cp936 on a
    # Chinese Windows) — a non-ASCII path then kills the reader thread,
    # stdout becomes None, and the Stop hook crashes to exit 1, which the
    # hook contract treats as non-blocking: the gate would fail OPEN.
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="surrogateescape",
            timeout=20, check=True,
        )
        return proc.stdout or ""
    except (subprocess.SubprocessError, OSError):
        return ""


_HASH_CAP_BYTES = 4 * 1024 * 1024   # per file: length + first 4 MiB


def _is_state_dir_status(line: str) -> bool:
    """Is this a `git status --porcelain` line about the gate's own
    .flashgate/ dir (untracked, at any depth)?"""
    if not line.startswith("?? "):
        return False
    path = line[3:].strip().strip('"').rstrip("/")
    return path == ".flashgate" or path.endswith("/.flashgate")


def _untracked_files(fw_dir: Path) -> list[str]:
    """Untracked files, recursively, gitignore-respecting, no quoting.

    `git status --porcelain` collapses a new directory to '?? dir/' (hiding
    every file inside) and C-escapes non-ASCII paths under core.quotepath —
    both let content edits slip past a content digest. `ls-files
    --others --exclude-standard -z` lists real paths, NUL-separated."""
    out = _git(["ls-files", "--others", "--exclude-standard", "-z"], fw_dir)
    return sorted(p for p in out.split("\x00") if p)


def _untracked_content_digest(fw_dir: Path) -> str:
    """Digest over the CONTENT of untracked files.

    Found by the 2026-09-13 external review: git diff ignores untracked
    files and git status lists only their paths, so editing an untracked
    source file after a cached PASS kept the fingerprint unchanged — the
    Stop hook reused the stale green. Hashing path + size + content closes
    it. The gate's own state dir is excluded: save_state() rewrites a
    timestamp there, and letting it feed the fingerprint would invalidate
    every cached PASS."""
    h = hashlib.sha256()
    for path in _untracked_files(fw_dir):
        if path == ".flashgate" or path.startswith(".flashgate/"):
            continue
        f = fw_dir / path
        try:
            size = f.stat().st_size
            h.update(f"{path}\x00{size}\x00".encode("utf-8", errors="replace"))
            with f.open("rb") as fh:               # chunked: never OOM on a
                remaining = min(size, _HASH_CAP_BYTES)  # stray huge file
                while remaining > 0:
                    chunk = fh.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    h.update(chunk)
                    remaining -= len(chunk)
        except OSError:
            h.update(f"{path}\x00<unreadable>\x00".encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _profile_digest(profile: Path) -> str:
    """sha256 over the board profile's bytes (same 4 MiB cap as untracked
    files). Identity is content-only: two profiles with identical bytes
    define identical verification semantics; any location-dependent field
    resolves through firmware_dir, which is fingerprinted separately."""
    try:
        h = hashlib.sha256()
        with profile.open("rb") as fh:
            remaining = _HASH_CAP_BYTES
            while remaining > 0:
                chunk = fh.read(min(1 << 20, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
        return h.hexdigest()
    except OSError:
        return "<unreadable>"


def tree_fingerprint(fw_dir: Path, profile: Path | None = None) -> str:
    """Content-identity of the working tree AND the verification semantics.

    Not just HEAD: dirty trees with different edits must NOT share a
    fingerprint — including edits to untracked files (hashed explicitly)
    and edits to the board profile. The profile defines what "PASS"
    means (probe expectations, build command), so tightening or loosening
    it after a cached PASS must invalidate that green, not reuse it —
    same stale-PASS class as the untracked-content hole (2026-09-13
    external review)."""
    head = _git(["rev-parse", "HEAD"], fw_dir)
    diff = _git(["diff", "HEAD"], fw_dir)
    status = _git(["status", "--porcelain"], fw_dir)
    # The gate's own state (PASS cache, verify records) lives under
    # .flashgate/ and must not feed the identity: the first-ever write
    # would flip `git status` from nothing to an untracked line and every
    # later fingerprint stays shifted — one wasted re-verify per repo.
    # Path-segment match, not prefix: with the firmware dir as a SUBDIR of
    # a larger repo, status emits repo-root-relative paths
    # ('?? fw/.flashgate/'), which a plain startswith('?? .flashgate')
    # would miss (runtime audit, 2026-09-14).
    status = "\n".join(
        ln for ln in status.splitlines() if not _is_state_dir_status(ln))
    untracked = _untracked_content_digest(fw_dir)
    profile_part = _profile_digest(profile) if profile is not None else "<no-profile>"
    # The verifying tool itself defines what PASS means (probe semantics,
    # evidence parsing). Adversarial-review finding F2: a flashgate upgrade
    # could loosen assertions while the tree+profile stayed identical and
    # the cached green would survive. Version joins the identity, so an
    # upgrade invalidates the cache once — fail-safe direction.
    blob = "\x00".join((head, diff, status, untracked, profile_part,
                        f"flashgate={_FGATE_VERSION}"))
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()


def watched_paths(fw_dir: Path, patterns: list[str]) -> list[str]:
    """Working-tree changes that match the watch globs (gate triggers)."""
    status = _git(["status", "--porcelain"], fw_dir)
    watched: list[str] = []
    for line in status.splitlines():
        if line.startswith("?? "):
            continue     # untracked: handled below, dir-collapsed in status
        path = line[3:].strip().strip('"')
        if " -> " in path:                     # rename: judge by the new name
            path = path.split(" -> ")[1]
        if path and any(fnmatch(path, pat) for pat in patterns):
            watched.append(path)
    # Untracked files come from ls-files: a new directory collapses to a
    # single '?? dir/' status line that no *.c glob can match, which used
    # to mean new files in new dirs never even triggered the gate.
    for path in _untracked_files(fw_dir):
        if any(fnmatch(path, pat) for pat in patterns):
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
