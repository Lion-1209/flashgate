"""Compatibility matrix (N4): schema validation + cross-checks against
the board profile and real verify records.

The matrix is the long-term responsibility surface for a board package:
hardware revisions, adapters, console wiring, firmware/toolchain
versions, hosts, checks with their failure boundaries. Anything not
measured must say `untested` — the template makes the honest state
expressible and the validator makes omissions loud.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

import pytest

DOCS = Path(__file__).resolve().parent.parent / "docs"
TEMPLATE = DOCS / "compat-template.yaml"
APOLLO = DOCS / "compat-apollo-h743.yaml"

_REQUIRED = {
    "board", "mcu", "hardware_revisions", "unverified_revisions",
    "flash", "console", "firmware", "hosts", "checks",
    "coverage_caveats", "records_dir",
}
_FLASH_REQUIRED = {"adapters", "probe", "connect", "address"}
_HOST_REQUIRED = {"os"}                    # status: untested also allowed
_CHECK_REQUIRED = {"name", "proves", "failure_boundary"}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and "matrix" in data, \
        f"{path.name}: must be a mapping with a top-level 'matrix'"
    return data["matrix"]


class TestTemplate:
    def test_template_parses_with_every_required_key(self):
        m = _load(TEMPLATE)
        missing = _REQUIRED - set(m)
        assert not missing, f"template lost required keys: {missing}"
        assert _FLASH_REQUIRED <= set(m["flash"])
        assert m["checks"], "template must show the checks shape"

    def test_template_placeholders_are_angle_bracketed(self):
        # <placeholders> mark what a new board must fill; the template
        # itself must not carry one real board's data
        text = TEMPLATE.read_text(encoding="utf-8")
        assert "<板卡名" in text and "<YYYY-MM-DD>" in text


class TestApolloExample:
    def test_apollo_matrix_validates(self):
        m = _load(APOLLO)
        missing = _REQUIRED - set(m)
        assert not missing
        assert m["board"] == "apollo-h743"
        assert m["console"]["baudrate"] == 115200

    def test_every_adapter_and_revision_carry_dates(self):
        m = _load(APOLLO)
        for a in m["flash"]["adapters"]:
            assert _DATE.match(str(a["verified"])), a
        for r in m["hardware_revisions"]:
            assert _DATE.match(str(r["verified"])), r

    def test_untested_host_is_labeled(self):
        m = _load(APOLLO)
        untested = [h for h in m["hosts"] if h.get("status") == "untested"]
        assert untested, "the Linux row must stay honestly untested"

    def test_checks_have_failure_boundaries(self):
        m = _load(APOLLO)
        assert 8 <= len(m["checks"]) <= 12, \
            "the product plan calls for 8-12 concrete acceptance checks"
        for c in m["checks"]:
            assert c.get("proves") and c.get("failure_boundary"), c

    def test_cross_check_with_board_profile(self):
        # the matrix must not contradict the machine-checked profile
        from flashgate.board import load_board
        b = load_board(DOCS.parent / "boards" / "apollo-h743.yaml")
        m = _load(APOLLO)
        assert m["board"] == b.name
        assert int(m["console"]["baudrate"]) == b.baudrate
        assert m["flash"]["address"] == b.flash_address

    def test_coverage_caveats_match_profile(self):
        from flashgate.board import load_board
        b = load_board(DOCS.parent / "boards" / "apollo-h743.yaml")
        m = _load(APOLLO)
        for note in b.coverage_notes:
            assert any(note[:30] == c[:30] for c in m["coverage_caveats"]), \
                f"profile coverage note missing from matrix: {note[:40]}"

    def test_last_verify_record_exists(self):
        m = _load(APOLLO)
        rec = (DOCS.parent / m["records_dir"] / m["last_verify_record"])
        assert rec.is_file(), \
            f"last_verify_record {m['last_verify_record']} not on disk"
