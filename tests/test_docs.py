"""Docs contract: the CI recipe doc must stay honest.

Every fenced yaml block in docs/ci-recipes.md must parse, and every
flashgate command it shows must use subcommands/flags that actually
exist — a recipe whose commands don't run is worse than no recipe.
"""

import re
import subprocess
import sys
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


def _cli_surface() -> tuple[set, set]:
    """(subcommands, flags) parsed from the REAL `flashgate --help` output.
    The docs whitelist must stay a subset of this — a whitelist referencing
    a renamed/removed flag guards against a dead surface (M4)."""
    repo = Path(__file__).resolve().parent.parent

    def help_of(*args):
        out = subprocess.run([sys.executable, "-m", "flashgate", *args, "--help"],
                             capture_output=True, text=True, timeout=120,
                             cwd=str(repo))
        assert out.returncode == 0, out.stderr
        return out.stdout

    top = help_of()
    m = re.search(r"\{([^}]+)\}", top)
    subs = set(m.group(1).split(",")) if m else set()
    flags = {f for f in re.findall(r"\[(--[\w-]+)[\] =]", top)}
    for s in sorted(subs):
        if s.startswith("-"):
            continue
        flags |= {f for f in re.findall(r"\[(--[\w-]+)[\] =]", help_of(s))}
    return subs, flags


class TestContractSurfaces:
    """The exit-code contract lives in many copies (README table, cli
    module docstring, slash-command, board profile comment, results.py
    mappings, cli constants). A new/renumbered code must move ALL of them
    together — this is the guard that makes silent desync fail loudly."""

    CODES = set(range(8))

    @staticmethod
    def _readme_exit_codes() -> set:
        text = (DOC.parent.parent / "README.md").read_text(encoding="utf-8")
        section = text.split("## Exit codes", 1)[1]
        rows = section.split("##", 1)[0]
        return {int(c) for c in re.findall(r"^\|\s*(\d)\s*\|", rows, re.M)}

    def test_readme_exit_table_is_exactly_0_to_7(self):
        assert self._readme_exit_codes() == self.CODES

    def test_results_mappings_cover_exactly_0_to_7(self):
        from flashgate import results
        assert set(results._EXIT_CODE) == self.CODES
        assert set(results._EXIT_STATUS) == self.CODES

    def test_cli_exit_constants_are_0_to_7(self):
        from flashgate import cli
        values = [cli.EXIT_OK, cli.EXIT_BUILD, cli.EXIT_FLASH,
                  cli.EXIT_BANNER_TIMEOUT, cli.EXIT_BOOT_ERROR,
                  cli.EXIT_SHA_MISMATCH, cli.EXIT_ENV, cli.EXIT_PROBE_FAIL]
        assert values == list(range(8))
        named = [n for n in dir(cli) if n.startswith("EXIT_")]
        assert len(named) == len(self.CODES), (
            f"EXIT_* constants {sorted(named)} drifted from the contract "
            "— add the new one to this test on purpose, never silently")

    def test_cli_module_docstring_lists_every_code(self):
        # line-start or "| "-anchored only: prose like "within 3 seconds"
        # must not count as documenting code 3 (adversarial L-1)
        from flashgate import cli
        found = {int(c) for c in re.findall(r"(?:^\s*|\| )([0-7]) [a-z]",
                                            cli.__doc__ or "", re.M)}
        assert self.CODES <= found, f"docstring missing codes: {self.CODES - found}"

    def test_slash_command_lists_every_code(self):
        text = (DOC.parent.parent / "commands" / "flashgate-verify.md")\
            .read_text(encoding="utf-8")
        found = set()
        for group in re.findall(r"exit ([\d/ ]+):", text):
            found |= {int(c) for c in re.findall(r"\d", group)}
        assert self.CODES <= found, f"slash-command missing: {self.CODES - found}"

    def test_guide_exit_table_lists_every_code(self):
        # the GUIDE §5 table is the second FULL contract copy (Chinese) —
        # an unguarded desync here misleads readers exactly like README's
        text = (DOC.parent / "GUIDE.md").read_text(encoding="utf-8")
        section = text.split("## 5. 退出码", 1)[1].split("##", 1)[0]
        rows = {int(c) for c in re.findall(r"^\|\s*(\d)\s*\|", section, re.M)}
        assert rows == self.CODES, f"GUIDE exit table codes drifted: {rows}"

    def test_ci_recipes_inline_copy_covers_1_to_7(self):
        # ci-recipes.md carries a compact copy ("1 构建 / 2 烧录 / …") —
        # guard its presence and coverage (adversarial M-1, 8th copy)
        text = DOC.read_text(encoding="utf-8")
        m = re.search(r"（1 构建.*?探针失败）", text, re.S)
        assert m, "ci-recipes inline exit-code copy is gone or reworded"
        found = {int(c) for c in re.findall(r"(\d) [^/）]+", m.group(0))}
        assert {1, 2, 3, 4, 5, 6, 7} <= found, found

    def test_board_profile_comment_lists_every_code(self):
        text = (DOC.parent.parent / "boards" / "apollo-h743.yaml")\
            .read_text(encoding="utf-8")
        found = {int(c) for c in re.findall(r"(?<![\w])([0-7])\s*=", text)}
        assert self.CODES <= found, f"board profile comment missing: {self.CODES - found}"


    def test_record_schema_documents_coverage_block(self):
        # N1: the coverage block is part of the record contract — the
        # schema doc must keep documenting its keys (desync pin)
        text = (DOC.parent / "record-schema.md").read_text(encoding="utf-8")
        for key in ("coverage", "verified", "not_verified", "profile_notes",
                    "coverage.notes"):
            assert key in text, f"record-schema.md lost the coverage key {key!r}"
        assert '"1.1"' in text

    def test_coverage_trigger_wording_pinned(self):
        # the three probe states must stay documented — a branch landing
        # in code but not in the contract docs slipped through twice
        # (audit rounds caught it both times); pin the phrases
        text = (DOC.parent / "record-schema.md").read_text(encoding="utf-8")
        for phrase in ("no probes were run",
                       "planned but never executed",
                       "ran but none passed"):
            assert phrase in text, (
                f"record-schema.md lost the coverage trigger phrase "
                f"{phrase!r} — sync the docs with the code branches")
        guide = (DOC.parent / "GUIDE.md").read_text(encoding="utf-8")
        assert "已计划未执行" in guide and "无一通过" in guide

    def test_readme_and_guide_describe_coverage_block(self):
        # audit L-2: the README/GUIDE coverage paragraphs had no drift
        # guard — if they stop describing the block, this pin goes red
        readme = (DOC.parent.parent / "README.md").read_text(encoding="utf-8")
        assert "coverage" in readme and "what this PASS proves" in readme
        guide = (DOC.parent / "GUIDE.md").read_text(encoding="utf-8")
        assert "coverage" in guide and "没验证什么" in guide

    def test_record_schema_mentions_the_contract_range(self):
        # the 9th copy: record-schema.md describes exit_code as "the CLI
        # 0-7 contract" — a range-only copy, but if the contract ever
        # grows this phrase must move with it (N0-4)
        text = (DOC.parent / "record-schema.md").read_text(encoding="utf-8")
        m = re.search(r"exit_code.{0,80}?([0-9])-([0-9]) contract", text)
        assert m, "record-schema.md lost its exit-code contract phrase"
        assert (int(m.group(1)), int(m.group(2))) == (0, 7)

    def test_demo_md_stays_non_contractual(self):
        # DEMO.md is a narrative transcript archive, ADJUDICATED out of
        # the contract-surface set (N0-5). If someone ever turns it into
        # a full enumerated exit-code copy, it must join
        # TestContractSurfaces instead of silently drifting.
        text = (DOC.parent.parent / "demo" / "README.md")            .read_text(encoding="utf-8")
        codes = {int(c) for c in re.findall(r"exit(?:\s*code)?\s*([0-7])",
                                            text, re.I)}
        assert codes != self.CODES, (
            "demo/README.md now enumerates every exit code — either wire "
            "it into TestContractSurfaces or trim it back to narrative")


class TestWhitelistSyncedWithRealCli:
    def test_whitelists_reference_only_real_surface(self):
        subs, flags = _cli_surface()
        assert subs, "failed to parse subcommands from --help"
        stale_subs = _KNOWN_SUBCOMMANDS - subs
        assert not stale_subs, f"whitelist has removed subcommands: {stale_subs}"
        stale_flags = _KNOWN_FLAGS - flags
        assert not stale_flags, f"whitelist has removed flags: {stale_flags}"
