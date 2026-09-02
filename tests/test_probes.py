"""Probe parsing and the assert mini-evaluator."""

from pathlib import Path

import pytest

from flashgate.probes import (
    ProbeError,
    compile_pattern,
    eval_assert,
    load_probes,
)


class TestCompilePattern:
    def test_template_captures_value(self):
        m = compile_pattern("OK bat mv={mv}").search("OK bat mv=3300")
        assert m and m.group("mv") == "3300"

    def test_template_digits_type(self):
        pat = compile_pattern("OK bat mv={mv:d}")
        assert pat.search("OK bat mv=3300")
        assert pat.search("OK bat mv=-") is None       # dash is not a digit

    def test_plain_text_is_exact_line(self):
        pat = compile_pattern("OK demo on")
        assert pat.search("OK demo on")
        assert pat.search("OK demo on extra") is None
        assert pat.search("prefix OK demo on") is None

    def test_template_escapes_regex_chars(self):
        pat = compile_pattern("OK bat (pack1) mv={mv:d}")
        assert pat.search("OK bat (pack1) mv=4100")
        assert pat.search("OK bat pack1 mv=4100") is None

    def test_slash_form_is_raw_regex(self):
        pat = compile_pattern("/^OK led\\d .+$/")
        assert pat.search("OK led0 whatever")

    def test_legacy_named_group_passthrough(self):
        pat = compile_pattern(r"^OK led0 state=(?P<state>\S+)$")
        m = pat.search("OK led0 state=BREATH")
        assert m and m.group("state") == "BREATH"

    def test_unanchored_matches_inside_transcript(self):
        # banner scenario: the line arrives after \r\n, unanchored search
        pat = compile_pattern("BOOT board={board} git={git}", anchor=False)
        m = pat.search("\r\nnoise\r\nBOOT board=b1 git=abc1234 rtos=FreeRTOS\r\n")
        assert m and m.group("board") == "b1" and m.group("git") == "abc1234"


class TestEvalAssert:
    def test_numeric_compare(self):
        assert eval_assert("ccr > 0", {"ccr": "691"})
        assert not eval_assert("ccr > 1000", {"ccr": "1000"})
        assert eval_assert("ccr <= 1000", {"ccr": "1000"})

    def test_string_equality(self):
        assert eval_assert("state == BREATH", {"state": "BREATH"})
        assert not eval_assert("state == BREATH", {"state": "OFF"})
        assert eval_assert("state != OFF", {"state": "BREATH"})

    def test_conjunction(self):
        groups = {"state": "BREATH", "ccr": "512"}
        assert eval_assert("state == BREATH and ccr > 0", groups)
        assert not eval_assert("state == BREATH and ccr > 1000", groups)

    def test_missing_group_is_false(self):
        assert not eval_assert("state == BREATH", {"ccr": "1"})

    def test_negative_numbers(self):
        assert eval_assert("temp > -40", {"temp": "25"})
        assert not eval_assert("temp < -40", {"temp": "25"})


class TestLoadProbes:
    def _yaml(self, tmp_path: Path, body: str) -> Path:
        p = tmp_path / "board.yaml"
        p.write_text(body, encoding="utf-8")
        return p

    def test_full_parse(self, tmp_path):
        p = self._yaml(tmp_path, """
probes:
  led-demo:
    description: LED check
    step_timeout_s: 4
    steps:
      - send: "led0 breath"
        expect: '^OK led0 state=BREATH$'
      - send: "led0?"
        expect: '^OK led0 state=(?P<state>\\S+) ccr=(?P<ccr>\\d+)$'
        assert: "state == BREATH and ccr <= 1000"
""")
        probes = load_probes(p)
        assert list(probes) == ["led-demo"]
        probe = probes["led-demo"]
        assert probe.step_timeout_s == 4.0
        assert len(probe.steps) == 2
        assert probe.steps[0].assert_expr is None
        assert probe.steps[1].assert_expr == "state == BREATH and ccr <= 1000"

    def test_template_syntax_parses_like_legacy(self, tmp_path):
        p = self._yaml(tmp_path, """
probes:
  led-demo:
    steps:
      - send: "led0?"
        expect: "OK led0 state={state} ccr={ccr:d}"
        assert: "state == BREATH and ccr <= 1000"
""")
        step = load_probes(p)["led-demo"].steps[0]
        legacy = compile_pattern(r"^OK led0 state=(?P<state>\S+) ccr=(?P<ccr>\d+)$")
        modern = compile_pattern(step.expect)
        line = "OK led0 state=BREATH ccr=691"
        assert modern.search(line).groupdict() == legacy.search(line).groupdict()

    def test_no_probes_section(self, tmp_path):
        p = self._yaml(tmp_path, "board: x\n")
        assert load_probes(p) == {}

    def test_empty_steps_rejected(self, tmp_path):
        p = self._yaml(tmp_path, "probes:\n  bad: {}\n")
        with pytest.raises(ProbeError):
            load_probes(p)

    def test_missing_send_rejected(self, tmp_path):
        p = self._yaml(tmp_path, "probes:\n  bad:\n    steps:\n      - expect: x\n")
        with pytest.raises((ProbeError, KeyError)):
            load_probes(p)
