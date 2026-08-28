"""Flashing via STM32CubeProgrammer CLI (ST-Link)."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .sttools import augmented_env, find_cubeprogrammer

FLASH_TIMEOUT_S = 90
FLASH_ATTEMPTS = 3          # clone ST-Links throw transient DEV_USB_COMM_ERR
RETRY_DELAY_S = 3.0


@dataclass
class FlashResult:
    ok: bool
    detail: str


def _kill_stlinkserver() -> None:
    """A stale stlink-server session can wedge the probe; it restarts on
    demand, so killing it between attempts is safe."""
    subprocess.run(
        ["taskkill", "/F", "/IM", "stlinkserver.exe"],
        capture_output=True, timeout=15,
    )


def _attempt_flash(cli: Path, bin_path: Path, connect: str, address: str) -> tuple[bool, int, str]:
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
        return False, -1, "CubeProgrammer timed out (ST-Link connected? board powered?)"
    except OSError as exc:
        return False, -1, f"failed to run CubeProgrammer: {exc}"

    output = (proc.stdout or "") + (proc.stderr or "")
    # CubeProgrammer mixes return codes; trust the explicit download line.
    if proc.returncode == 0 and "File download complete" in output:
        return True, 0, output.strip()
    return False, proc.returncode, output.strip() or f"exit code {proc.returncode}"


def flash(bin_path: Path, connect: str, address: str) -> FlashResult:
    cli = find_cubeprogrammer()
    if cli is None:
        return FlashResult(False, "STM32CubeProgrammer CLI not found (bundles or PATH)")

    if not bin_path.is_file():
        return FlashResult(False, f"artifact not found: {bin_path}")

    last_detail = ""
    for attempt in range(1, FLASH_ATTEMPTS + 1):
        ok, rc, output = _attempt_flash(cli, bin_path, connect, address)
        if ok:
            return FlashResult(True, output)
        last_detail = f"attempt {attempt}/{FLASH_ATTEMPTS} rc={rc}: {output[-1200:]}"
        if attempt < FLASH_ATTEMPTS:
            _kill_stlinkserver()
            time.sleep(RETRY_DELAY_S)
    return FlashResult(False, last_detail)


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
