"""Docs contract: the CI recipe doc must stay honest.

Every fenced yaml block in docs/ci-recipes.md must parse, and every
flashgate command it shows must use subcommands/flags that actually
exist — a recipe whose commands don't run is worse than no recipe.
"""

import re
from pathlib import Path

import yaml

DOC = Path(__file__).resolve().parent.parent / "docs" / "ci-recipes.md"

_KNOWN_SUBCOMMANDS = {
    "doctor", "build", "flash", "verify", "probe", "console", "bench-serve",
}
_KNOWN_FLAGS = {
    "--board", "--all-probes", "--probe", "--json", "--evidence",
    "--device-id", "--stop", "--version",
}


def _fenced_yaml_blocks(text: str) -> list[str]:
    blocks, current = [], None
    for line in text.splitlines():
        if line.strip() == "```yaml":
            current = []
        elif line.strip() == "```" and current is not None:
            blocks.append("\n".join(current))
            current = None
        elif current is not None:
            current.append(line)
    return blocks


def _fenced_code_blocks(text: str) -> list[str]:
    """All fenced code blocks, any language tag (yaml/bash/...)."""
    blocks, current = [], None
    for line in text.splitlines():
        if current is None:
            if line.startswith("```"):
                current = []
        else:
            if line.startswith("```"):
                blocks.append("\n".join(current))
                current = None
            else:
                current.append(line)
    return blocks


def _flashgate_command_lines(text: str) -> list[str]:
    """flashgate commands shown inside fenced code blocks only — prose
    mentions of the tool name are not commands."""
    hits = []
    for block in _fenced_code_blocks(text):
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.endswith("\\"):
                stripped = stripped[:-1].strip()
            if "flashgate " in stripped and not stripped.startswith("#"):
                hits.append(stripped)
    return hits


def test_yaml_blocks_parse():
    text = DOC.read_text(encoding="utf-8")
    blocks = _fenced_yaml_blocks(text)
    assert blocks, "the recipe doc must contain at least one yaml block"
    for i, block in enumerate(blocks):
        parsed = yaml.safe_load(block)
        assert isinstance(parsed, dict), f"block {i} is not a mapping"


def test_flashgate_commands_use_real_surface():
    text = DOC.read_text(encoding="utf-8")
    commands = _flashgate_command_lines(text)
    assert commands, "the recipe doc must show at least one flashgate command"
    for cmd in commands:
        # strip env-var prefixes and redirections: FLASHGATE_X=1 flashgate ... > f
        body = cmd.split(" flashgate ", 1)[1]
        body = body.split(">", 1)[0].strip()
        tokens = body.split()
        assert tokens, cmd
        # global flags (--board) may precede the subcommand; find it anywhere
        subs = [t for t in tokens if t in _KNOWN_SUBCOMMANDS]
        assert len(subs) == 1, f"expected exactly one subcommand in: {cmd}"
        for tok in tokens:
            if tok.startswith("-"):
                flag = tok.split("=", 1)[0]
                assert flag in _KNOWN_FLAGS, f"unknown flag {flag!r} in: {cmd}"


def test_referenced_local_docs_exist():
    text = DOC.read_text(encoding="utf-8")
    for ref in re.findall(r"\]\(([^)]+)\)", text):
        if ref.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = DOC.parent / ref
        assert target.is_file(), f"doc references missing file: {ref}"
