"""Unified result envelope for flashgate MCP tools (design doc §7).

Every MCP tool returns a Result: machine-decidable status, a stable code,
a human summary, the CLI exit code as a compatibility layer, and the
payload. "Command succeeded" and "hardware verified" are different things;
`status` speaks only in evidence terms.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

Status = Literal["succeeded", "failed", "incomplete", "cancelled", "timed_out"]

# Stable machine codes (design doc §7.2). Extensible; never repurpose one.
OK = "OK"
BUILD_FAILED = "BUILD_FAILED"
FLASH_FAILED = "FLASH_FAILED"
RESET_FAILED = "RESET_FAILED"
BOOT_EVIDENCE_TIMEOUT = "BOOT_EVIDENCE_TIMEOUT"
BOOT_ERROR = "BOOT_ERROR"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
PROBE_FAILED = "PROBE_FAILED"
CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
TRANSPORT_ERROR = "TRANSPORT_ERROR"
ADAPTER_ERROR = "ADAPTER_ERROR"
INTERNAL_ERROR = "INTERNAL_ERROR"
PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"      # board yaml missing/unloadable
INVALID_ARGUMENT = "INVALID_ARGUMENT"

# CLI exit-code contract (board yaml comment) -> envelope semantics.
# 6 = env/prereq: a required step COULD NOT RUN -> "incomplete", never "passed".
_EXIT_STATUS: dict[int, Status] = {
    0: "succeeded", 1: "failed", 2: "failed", 3: "timed_out",
    4: "failed", 5: "failed", 6: "incomplete", 7: "failed",
}
_EXIT_CODE: dict[int, str] = {
    0: OK, 1: BUILD_FAILED, 2: FLASH_FAILED, 3: BOOT_EVIDENCE_TIMEOUT,
    4: BOOT_ERROR, 5: IDENTITY_MISMATCH, 6: CAPABILITY_UNAVAILABLE,
    7: PROBE_FAILED,
}


class Result(BaseModel):
    """The envelope every flashgate MCP tool returns."""

    schema_version: str = SCHEMA_VERSION
    status: Status
    code: str
    summary: str
    exit_code: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    policy: dict[str, Any] = Field(default_factory=dict)


def ok(summary: str, *, data: dict | None = None,
       policy: dict | None = None) -> Result:
    return Result(status="succeeded", code=OK, summary=summary,
                  data=data or {}, policy=policy or {})


def failure(summary: str, *, code: str = INTERNAL_ERROR,
            status: Status = "failed", data: dict | None = None,
            policy: dict | None = None) -> Result:
    return Result(status=status, code=code, summary=summary,
                  data=data or {}, policy=policy or {})


def from_exit(rc: int, summary: str, *, log: str | None = None,
              data: dict | None = None,
              policy: dict | None = None) -> Result:
    """Envelope from the CLI exit-code contract (the compat layer)."""
    payload = dict(data or {})
    if log:
        payload["log"] = log
    return Result(
        status=_EXIT_STATUS.get(rc, "failed"),
        code=_EXIT_CODE.get(rc, INTERNAL_ERROR),
        summary=summary,
        exit_code=rc,
        data=payload,
        policy=policy or {},
    )
