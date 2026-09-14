"""Console serial: resolve the USB-TTL adapter, wait for the boot banner."""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports

ENV_PORT = "FLASHGATE_SERIAL_PORT"


@dataclass
class BannerResult:
    matched: bool
    groups: dict[str, str] | None
    transcript: str
    error_hit: str | None
    matched_line: str = ""        # the exact banner line (m.group(0)); the
                                  # transcript tail may contain LATER output too


def resolve_console_port(
    explicit_port: str,
    vid: int,
    pids: tuple[int, ...],
) -> tuple[str | None, str]:
    """Layered port resolution. Returns (device, why) so doctor can explain.

    1. explicit port (yaml `serial.port` or env FLASHGATE_SERIAL_PORT)
    2. VID/PID hint — matches common USB-TTL bridges (CH340/CP210x/FT232...)
    3. sole serial port on the machine
    The banner regex remains the final identity proof in every case: a wrong
    port simply never matches and verify times out with a clear error.
    """
    explicit = (os.environ.get(ENV_PORT) or explicit_port or "").strip()
    if explicit:
        return explicit, "explicit (config/env override)"

    ports = list_ports.comports()
    for p in ports:
        if p.vid == vid and (not pids or p.pid in pids):
            return p.device, f"VID/PID hint {p.vid:04X}:{p.pid:04X} ({p.description})"
    if len(ports) == 1:
        return ports[0].device, f"sole serial port ({ports[0].description})"

    if not ports:
        return None, "no serial ports on this machine — plug in the USB-TTL adapter"
    names = ", ".join(f"{p.device} ({p.description})" for p in ports)
    return None, f"ambiguous: multiple serial ports [{names}] — set serial.port or {ENV_PORT}"


# Windows COM ports are exclusive and their handle release is ASYNC: right
# after the previous process exits (e.g. a verify followed by the Stop
# hook's own verify — seen live in the 2026-09-14 Claude Code session),
# reopening can fail with access-denied for a sub-second driver window.
# Retrying briefly turns that transient into a non-event; a genuinely
# held port (serial monitor, concurrent verify) exhausts the attempts and
# the raised error says what to do about it.
_OPEN_ATTEMPTS = 5
_OPEN_RETRY_DELAY_S = 0.5


def _is_access_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(mark in text for mark in (
        "permissionerror", "access is denied", "errno 13",
        "win error 5", "being used by another process", "拒绝访问",
    ))


def open_flush(device: str, baudrate: int,
               attempts: int = _OPEN_ATTEMPTS,
               retry_delay_s: float = _OPEN_RETRY_DELAY_S) -> serial.Serial:
    """Open and drain stale output (a previous firmware's banner).

    verify() keeps this connection open across the flash step on purpose:
    the banner emitted right at the `--start` reset then sits in the OS
    buffer instead of being lost to a close/reopen race.

    Access-denied failures are retried for ~attempts × retry_delay_s: the
    driver's async handle teardown after a just-exited process must not
    surface as CAPABILITY_UNAVAILABLE. Other errors (bad port name, no
    such device) raise immediately — retrying those is pure latency."""
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            conn = serial.Serial(device, baudrate, timeout=0.2)
            conn.reset_input_buffer()
            conn.reset_output_buffer()
            return conn
        except (serial.SerialException, PermissionError, OSError) as exc:
            if not _is_access_error(exc):
                raise
            last_exc = exc
            if attempt < attempts:
                time.sleep(retry_delay_s)
    waited = (attempts - 1) * retry_delay_s
    raise serial.SerialException(
        f"{device} is held by another process after {attempts} attempts "
        f"(~{waited:.0f}s): {last_exc}. Close any serial monitor "
        "(串口助手/putty/VSCode serial monitor) or concurrent flashgate/verify "
        "run holding it. If a verify JUST finished, its handle may still be "
        "closing — simply retry. On Linux, 'Permission denied' can also mean "
        "your user lacks dialout access (add yourself to the dialout group "
        "or fix the udev rule).")


def wait_on(
    conn: serial.Serial,
    banner_regex: str,
    error_patterns: tuple[str, ...],
    timeout_s: float,
    echo: bool = True,
) -> BannerResult:
    """Read from an open connection until banner / error pattern / timeout."""
    from .probes import compile_pattern

    pattern = compile_pattern(banner_regex, anchor=False)
    deadline = time.monotonic() + timeout_s
    transcript = ""

    while time.monotonic() < deadline:
        chunk = conn.read(512)
        if chunk:
            text = chunk.decode("utf-8", errors="replace")
            transcript += text
            if echo:
                sys.stdout.write(text)
                sys.stdout.flush()
            m = pattern.search(transcript)
            if m:
                return BannerResult(True, m.groupdict(), transcript, None,
                                    m.group(0))
            for err in error_patterns:
                if err in transcript:
                    return BannerResult(False, None, transcript, err)

    return BannerResult(False, None, transcript, None)


def console_forever(device: str, baudrate: int) -> None:
    # open_flush (not a bare Serial): a held/tearing-down port gets the
    # retry window and the actionable message, and cmd_console maps the
    # failure onto EXIT_ENV instead of a raw traceback (runtime audit G).
    conn = open_flush(device, baudrate)
    try:
        while True:
            chunk = conn.read(512)
            if chunk:
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                sys.stdout.flush()
    finally:
        conn.close()
