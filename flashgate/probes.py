"""Probe runner: send/expect/assert steps against the console protocol.

A probe is claim-proportional functional verification: the host drives the
board over the console and asserts on what the board reports — including
hardware register readbacks like the PWM CCR value. Any `ERR` line from the
firmware, an expect mismatch, a timeout, or a failed assert fails the probe.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import serial
import yaml


@dataclass
class ProbeStep:
    send: str
    expect: str                      # regex matched against one response line
    assert_expr: str | None = None   # over expect's named groups, e.g. "ccr > 0"


@dataclass
class Probe:
    name: str
    description: str = ""
    step_timeout_s: float = 3.0
    steps: list[ProbeStep] = field(default_factory=list)


@dataclass
class ProbeResult:
    ok: bool
    step_index: int | None = None    # failing step (0-based)
    detail: str = ""


class ProbeError(Exception):
    pass


def load_probes(yaml_path) -> dict[str, Probe]:
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    probes: dict[str, Probe] = {}
    for name, spec in (raw.get("probes") or {}).items():
        probe = Probe(
            name=name,
            description=spec.get("description", ""),
            step_timeout_s=float(spec.get("step_timeout_s", 3.0)),
            steps=[
                ProbeStep(
                    send=str(s["send"]),
                    expect=str(s["expect"]),
                    assert_expr=s.get("assert"),
                )
                for s in spec.get("steps", [])
            ],
        )
        if not probe.steps:
            raise ProbeError(f"probe {name!r} has no steps")
        probes[name] = probe
    return probes


def _clause(groups: dict[str, str], clause: str) -> bool:
    for op in ("==", "!=", ">=", "<=", ">", "<"):
        if op in clause:
            name, _, value = clause.partition(op)
            name, value = name.strip(), value.strip()
            if name not in groups:
                return False
            left, right = groups[name], value
            if left.lstrip("-").isdigit() and right.lstrip("-").isdigit():
                left, right = int(left), int(right)
            return {
                "==": left == right, "!=": left != right,
                ">": left > right,   "<": left < right,
                ">=": left >= right, "<=": left <= right,
            }[op]
    return False


def eval_assert(expr: str, groups: dict[str, str]) -> bool:
    """Tiny conjunction evaluator: `name OP value and name OP value`.
    No eval() — probes are data, not code."""
    return all(_clause(groups, c) for c in expr.split(" and "))


def run_probe(conn: serial.Serial, probe: Probe, echo: bool = True) -> ProbeResult:
    for idx, step in enumerate(probe.steps):
        if echo:
            print(f"    step {idx + 1}: {probe.name}> {step.send}")
        conn.write((step.send + "\r\n").encode())

        deadline = time.monotonic() + probe.step_timeout_s
        buffer = ""
        matched = False
        last_line = ""
        while time.monotonic() < deadline:
            chunk = conn.read(256)
            if chunk:
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip("\r")
                    if not line:
                        continue
                    if echo:
                        print(f"    board: {line}")
                    last_line = line
                    if line.startswith("ERR"):
                        return ProbeResult(False, idx,
                                           f"firmware error: {line!r} at step {idx + 1}")
                    m = re.search(step.expect, line)
                    if m:
                        groups = m.groupdict()
                        if step.assert_expr and not eval_assert(step.assert_expr, groups):
                            return ProbeResult(
                                False, idx,
                                f"assert failed: {step.assert_expr!r} "
                                f"against {groups} (step {idx + 1})")
                        matched = True
                        break
                if matched:
                    break
        if not matched:
            seen = f", last response: {last_line!r}" if last_line else ""
            return ProbeResult(False, idx,
                               f"timeout waiting for /{step.expect}/ after "
                               f"{step.send!r} (step {idx + 1}){seen}")
    return ProbeResult(True)
