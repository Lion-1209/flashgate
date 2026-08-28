"""Flashing via STM32CubeProgrammer CLI (ST-Link)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .sttools import augmented_env, find_cubeprogrammer

FLASH_TIMEOUT_S = 90


@dataclass
class FlashResult:
    ok: bool
    detail: str


def flash(bin_path: Path, connect: str, address: str) -> FlashResult:
    cli = find_cubeprogrammer()
    if cli is None:
        return FlashResult(False, "STM32CubeProgrammer CLI not found (bundles or PATH)")

    if not bin_path.is_file():
        return FlashResult(False, f"artifact not found: {bin_path}")

    cmd = [
        str(cli),
        "--connect", connect,
        "--write", str(bin_path), address,
        "--verify",
        "--start",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FLASH_TIMEOUT_S,
            env=augmented_env(), encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return FlashResult(False, "CubeProgrammer timed out (ST-Link connected? board powered?)")
    except OSError as exc:
        return FlashResult(False, f"failed to run CubeProgrammer: {exc}")

    output = (proc.stdout or "") + (proc.stderr or "")
    # CubeProgrammer mixes return codes; trust the explicit download/verify lines.
    downloaded = "File download complete" in output
    verified = ("Verifying" in output and "verified successfully" in output.lower()) or downloaded
    if proc.returncode == 0 and downloaded and verified:
        return FlashResult(True, output.strip())
    return FlashResult(False, output.strip()[-1500:] or f"exit code {proc.returncode}")


def list_stlink() -> str:
    """Doctor helper: list connected ST-Link probes."""
    cli = find_cubeprogrammer()
    if cli is None:
        return "(CubeProgrammer CLI not found)"
    try:
        proc = subprocess.run(
            [str(cli), "-l"], capture_output=True, text=True, timeout=30,
            env=augmented_env(), encoding="utf-8", errors="replace",
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except (subprocess.SubprocessError, OSError) as exc:
        return f"(probe listing failed: {exc})"
