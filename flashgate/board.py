"""Board profile loading: one yaml per board, everything declarative."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml


class BoardError(Exception):
    pass


@dataclass
class Board:
    name: str
    mcu: str
    description: str
    firmware_dir: Path
    configure_command: str
    build_command: str
    artifact: Path
    flash_connect: str
    flash_address: str
    usb_vid: int
    usb_pids: tuple[int, ...]
    baudrate: int
    banner_regex: str
    banner_timeout_s: float
    error_patterns: tuple[str, ...]
    yaml_path: Path

    def head_sha(self) -> str | None:
        """Short git sha the banner should carry (build-time generated)."""
        try:
            out = subprocess.run(
                ["git", "rev-parse", "--short=7", "HEAD"],
                cwd=self.firmware_dir, capture_output=True, text=True, timeout=15,
                check=True,
            )
            return out.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            return None


def load_board(yaml_path: Path) -> Board:
    yaml_path = yaml_path.resolve()
    try:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BoardError(f"cannot load board profile {yaml_path}: {exc}") from exc

    fw = raw.get("firmware") or {}
    flash = raw.get("flash") or {}
    ser = raw.get("serial") or {}

    base = yaml_path.parent
    try:
        board = Board(
            name=raw["board"],
            mcu=raw.get("mcu", ""),
            description=raw.get("description", ""),
            firmware_dir=(base / fw["dir"]).resolve(),
            configure_command=fw.get("configure", ""),
            build_command=fw["build"],
            artifact=(base / fw["dir"] / fw["artifact"]).resolve(),
            flash_connect=flash.get("connect", "port=SWD"),
            flash_address=str(flash.get("address", "0x08000000")),
            usb_vid=int(str(ser.get("vid", "0x1A86")), 0),
            usb_pids=tuple(int(str(p), 0) for p in ser.get("pids", [])),
            baudrate=int(ser.get("baudrate", 115200)),
            banner_regex=ser["banner_regex"],
            banner_timeout_s=float(ser.get("banner_timeout_s", 15)),
            error_patterns=tuple(ser.get("error_patterns", [])),
            yaml_path=yaml_path,
        )
    except KeyError as exc:
        raise BoardError(f"board profile {yaml_path.name} missing key: {exc}") from exc

    if not board.firmware_dir.is_dir():
        raise BoardError(f"firmware dir does not exist: {board.firmware_dir}")
    return board


def default_board_path() -> Path | None:
    """First yaml in <repo>/boards — the repo layout default."""
    boards_dir = Path(__file__).resolve().parent.parent / "boards"
    if boards_dir.is_dir():
        for candidate in sorted(boards_dir.glob("*.yaml")):
            return candidate
    return None
