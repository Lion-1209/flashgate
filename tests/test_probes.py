"""Probe parsing and the assert mini-evaluator."""

from pathlib import Path

import pytest

from flashgate.probes import ProbeError, eval_assert, load_probes


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
