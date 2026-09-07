"""SWD signature evidence: read the firmware's fixed-address boot identity
through the ST-Link alone — no serial cable required.

The firmware publishes a 64-byte struct at SIGRAM (end of AXI SRAM):
magic written LAST, CRC32 over the first 48 bytes. A debugger read that
races the boot-time writes fails the CRC and is simply retried.
"""

from __future__ import annotations

import struct
import tempfile
import time
import zlib
from pathlib import Path

from .flasher import FLASH_ATTEMPTS  # reuse attempt count semantics
from .sttools import augmented_env, find_cubeprogrammer

SIG_MAGIC = 0xF1A5C0DE
SIG_VERSION = 1                 # the only signature layout this tool decodes
READ_TIMEOUT_S = 30


class SwdError(Exception):
    pass


class SignatureLayoutError(SwdError):
    """CRC-valid signature whose layout version this tool cannot decode.

    Waiting longer cannot fix a firmware/tool generation mismatch, so
    callers should fail fast instead of polling to the timeout."""


def read_ram(connect: str, address: int, size: int) -> bytes | None:
    """One CubeProgrammer memory read (HotPlug: does not halt the core)."""
    cli = find_cubeprogrammer()
    if cli is None:
        raise SwdError("STM32CubeProgrammer CLI not found")
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "sig.bin"
        cmd = [str(cli), "--connect", connect, "--read", hex(address), str(size), str(out)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=READ_TIMEOUT_S,
                env=augmented_env(), encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise SwdError("CubeProgrammer read timed out") from exc
        if proc.returncode != 0 or not out.is_file():
            output = (proc.stdout or "") + (proc.stderr or "")
            raise SwdError(f"RAM read failed: {output.strip()[-300:]}")
        return out.read_bytes()[:size]


def parse_signature(buf: bytes) -> dict | None:
    """Validate magic, layout version and CRC; decode the identity.

    None = not (yet) valid (retry). SignatureLayoutError = a complete,
    CRC-valid signature from a layout generation this tool refuses to
    interpret — treating it as evidence would be a lie."""
    if len(buf) < 64:
        return None
    magic, version, flags = struct.unpack_from("<IHH", buf, 0)
    if magic != SIG_MAGIC:
        return None
    crc_field = struct.unpack_from("<I", buf, 0x30)[0]
    if zlib.crc32(buf[:0x30]) & 0xFFFFFFFF != crc_field:
        return None                       # raced the boot-time write: retry
    if version != SIG_VERSION:
        raise SignatureLayoutError(
            f"signature layout version {version} not supported "
            f"(this tool decodes version {SIG_VERSION} only — "
            "update flashgate or the firmware's bsp_signature)")
    git = buf[0x08:0x18].split(b"\x00", 1)[0].decode("ascii", errors="replace")
    build = buf[0x18:0x30].split(b"\x00", 1)[0].decode("ascii", errors="replace")
    return {"version": version, "flags": flags, "git": git, "build": build}


def wait_for_signature(
    connect: str, address: int, size: int = 64, timeout_s: float = 15.0
) -> tuple[dict | None, str]:
    """Poll the signature until valid or timeout. Returns (info, last_error).

    A SignatureLayoutError aborts the poll immediately (returned as
    (None, message)): an alien layout version is a permanent firmware/tool
    mismatch, not something retrying can resolve."""
    deadline = time.monotonic() + timeout_s
    last = "no valid signature yet"
    while time.monotonic() < deadline:
        try:
            buf = read_ram(connect, address, size)
        except SwdError as exc:
            last = str(exc)
            time.sleep(1.0)
            continue
        try:
            info = parse_signature(buf or b"")
        except SignatureLayoutError as exc:
            return None, str(exc)
        if info is not None:
            return info, ""
        time.sleep(0.5)
    return None, last
