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


def open_flush(device: str, baudrate: int) -> serial.Serial:
    """Open and drain stale output (a previous firmware's banner).

    verify() keeps this connection open across the flash step on purpose:
    the banner emitted right at the `--start` reset then sits in the OS
    buffer instead of being lost to a close/reopen race.
    """
    conn = serial.Serial(device, baudrate, timeout=0.2)
    conn.reset_input_buffer()
    conn.reset_output_buffer()
    return conn


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
                return BannerResult(True, m.groupdict(), transcript, None)
            for err in error_patterns:
                if err in transcript:
                    return BannerResult(False, None, transcript, err)

    return BannerResult(False, None, transcript, None)


def console_forever(device: str, baudrate: int) -> None:
    with serial.Serial(device, baudrate, timeout=0.2) as conn:
        while True:
            chunk = conn.read(512)
            if chunk:
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                sys.stdout.flush()
