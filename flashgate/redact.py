"""Shared redaction for anything a user might send outward: replace the
local machine's identity markers (home dir, hostname, username) with
stable placeholders. Used by `doctor --export --redact` here and by the
records export (N3) — one scrubber, no divergent copies.

Two rules the patterns encode:
  * an absolute path keeps only its basename — support still sees WHICH
    file (cmake.EXE, the profile yaml) without learning the directory
    layout. Intermediate segments may contain spaces ("C:\\Program
    Files\\..."); the BASENAME may not, so a path never swallows the
    prose that follows it. The trade is deliberate: a space-tolerant
    intermediate class can also swallow prose that itself contains a
    backslash ("C:\\a\\c.exe and \\d\\e.exe" -> "<PATH>/e.exe"). That
    direction is safe — over-redaction loses context, under-redaction
    loses the workstation's layout, and only one of those is a leak.
  * identity markers (hostname, username) only replace whole tokens —
    a bench named "apollo" must not turn the board name into
    "<HOST>-h743", and a user named "ninja" must not rename the ninja
    check (adversarial L-2).

Known boundary (for N3): `redact_json_document` scrubs string VALUES,
not dict KEYS — records must never put a path in a key.
"""

from __future__ import annotations

import getpass
import json
import platform
import re
from functools import lru_cache
from pathlib import Path

_PLACEHOLDERS = {
    "home": "<HOME>",
    "hostname": "<HOST>",
    "user": "<USER>",
    "path": "<PATH>",
}

# A path segment. `_SEG_WS` allows spaces (C:\Program Files\... is where
# ST tools install by default — a space-free class redacted only
# "C:\" and leaked every directory after the first space, adversarial
# H1); `_SEG` is the strict form used for the BASENAME, so a match stops
# at the first space after the last separator and never eats the
# sentence following the path. Angle brackets stay legal inside a
# segment: an earlier pass may already have replaced the username with
# <USER>, and a class excluding it made the whole path unmatchable.
_SEG_WS = r"[^\\/:*?\"|\r\n]+"
_SEG = r"[^\\/:*?\"|\r\n\s]+"
# Drive paths, backslash / forward-slash / JSON-escaped. The single
# letter before the colon cannot be a URL scheme ('https:' is five
# letters), so URLs survive verbatim. (Composed by concatenation, not
# f-strings: a raw f-string literal full of backslashes is a SyntaxError
# before 3.12.)
_WIN_PATH = re.compile(r"[A-Za-z]:\\(?:" + _SEG_WS + r"\\)*(" + _SEG + ")")
_WIN_PATH_ESC = re.compile(r"[A-Za-z]:\\\\(?:" + _SEG_WS + r"\\\\)*(" + _SEG + ")")
_WIN_PATH_FWD = re.compile(r"[A-Za-z]:/(?:" + _SEG_WS + r"/)*(" + _SEG + ")")
# UNC paths (\\server\share\cmake.EXE) — a bench whose toolchain sits on
# a network share leaks the server and share names just like a drive
# path leaks the workstation layout (adversarial M-2).
_UNC_PATH = re.compile(r"\\\\(?:" + _SEG_WS + r"\\)*(" + _SEG + ")")
# A root-relative Windows path with no drive letter (\Windows\System32\
# drivers\x.sys) leaks the layout exactly like a drive path does
# (adversarial M-A). The leading backslash is required, so ordinary
# prose and drive paths (already collapsed above) are untouched.
_ROOT_PATH = re.compile(r"\\(?:" + _SEG_WS + r"\\)+(" + _SEG + ")")
# Any posix absolute path with 2+ segments. Segments stay space-free
# here on purpose: unlike Windows ("Program Files" is the default),
# spaced posix paths are rare, and a space-tolerant class swallows the
# prose between two absolute paths in a console transcript. The leading
# lookbehind keeps http:// and friends intact (the SECOND slash of a
# '://' must not start a match either).
_POSIX_PATH = re.compile(r"(?<![:\w/])/(?:[\w.@+-]+/)+([\w.@+-]+)")

# Whole-token identity markers: a hyphen or dot next to the token means
# it is part of a longer identifier (apollo-h743, my.host), not the
# hostname — replacing those mangles fields without protecting anything.
# IGNORECASE: Windows hostnames and paths are case-insensitive, so
# 'desktop-u7kpaip' is the same identity (adversarial L-A).
_MARKER = r"(?<![\w.-]){name}(?![\w.-])"


@lru_cache(maxsize=4)
def _home_patterns(home: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    r"""(home tree, bare home), separator-agnostic.

    Replacing only the home PREFIX is not enough: what survived was
    "\AppData\Local\stm32cube\bundles\...\cmake.EXE" — the workstation's
    layout, minus the drive letter (found on the first real redacted
    doctor report, 2026-09-24). The tree collapses to its basename like
    any other absolute path; separators match loosely so a forward-slash
    home ("C:/Users/me") redacts like its backslash twin."""
    parts = [re.escape(p) for p in re.split(r"[\\/]", home) if p]
    sep = r"[\\/]"
    body = sep.join(parts)
    return (re.compile(body + rf"(?:{sep}{_SEG_WS})*{sep}({_SEG})"),
            re.compile(body + r"(?!\w)"))


def _current_user() -> str:
    """getpass.getuser(), or "" where it cannot answer.

    On a slim image with no USER/LOGNAME env it falls through to the
    `pwd` module, which does not exist on Windows — ModuleNotFoundError
    escaped redact_text and crashed `doctor --export --redact` with
    exit 1 (= "build failed", a lie) on the repo's own python:3.12-slim
    Dockerfile (adversarial H-3). A scrubber that cannot find the
    username must skip it, never take the tool down."""
    try:
        return getpass.getuser() or ""
    except (ImportError, OSError, KeyError):
        return ""


def redact_text(text: str) -> str:
    """Scrub one string: home directory, hostname, username, and
    absolute paths (basename kept, location dropped)."""
    home = str(Path.home())
    if home and len(home) > 3:            # never redact "/" or "C:\\"
        tree, bare = _home_patterns(home)
        text = tree.sub(_PLACEHOLDERS["path"] + r"/\1", text)
        text = bare.sub(_PLACEHOLDERS["home"], text)
        # posix home shorthand that may appear in logs
        user = _current_user()
        if user:
            text = re.sub(r"(?<=/)" + re.escape(user),
                          _PLACEHOLDERS["user"], text)
    host = platform.node()
    if host and len(host) > 2:
        text = re.sub(_MARKER.format(name=re.escape(host)),
                      _PLACEHOLDERS["hostname"], text, flags=re.IGNORECASE)
    user = _current_user()
    if user and len(user) > 2:
        text = re.sub(_MARKER.format(name=re.escape(user)),
                      _PLACEHOLDERS["user"], text, flags=re.IGNORECASE)
    for pattern in (_WIN_PATH, _WIN_PATH_ESC, _WIN_PATH_FWD, _UNC_PATH,
                    _ROOT_PATH, _POSIX_PATH):
        text = pattern.sub(_PLACEHOLDERS["path"] + r"/\1", text)
    return text


def redact_json_document(json_text: str) -> str:
    """Redact a serialized JSON document: decode it, scrub every string
    value (recursively), re-serialize. Structure and non-string values
    are untouched by construction — no regex over escaped JSON. KEYS are
    structural and not scrubbed; callers must not put paths in keys."""
    data = json.loads(json_text)

    def _walk(node):
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if isinstance(node, str):
            return redact_text(node)
        return node

    return json.dumps(_walk(data), indent=2, ensure_ascii=False)
