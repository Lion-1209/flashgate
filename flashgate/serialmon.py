"""Console serial: find the USB-serial bridge, wait for the boot banner."""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports


@dataclass
class BannerResult:
    matched: bool
    groups: dict[str, str] | None
    transcript: str
    error_hit: str | None


def find_console_port(vid: int, pids: tuple[int, ...]) -> str | None:
    for port in list_ports.comports():
        if port.vid == vid and (not pids or port.pid in pids):
            return port.device
    return None


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
    pattern = re.compile(banner_regex)
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
