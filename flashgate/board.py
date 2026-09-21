"""Board profile loading: one yaml per board, everything declarative."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from .gatestate import DEFAULT_WATCH


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
    flash_adapter: str
    openocd_target: str
    openocd_interface: str
    serial_port: str
    usb_vid: int
    usb_pids: tuple[int, ...]
    baudrate: int
    banner_regex: str
    banner_timeout_s: float
    error_patterns: tuple[str, ...]
    watch_globs: tuple[str, ...]
    evidence_mode: str
    sig_address: int
    sig_size: int
    coverage_notes: tuple[str, ...]
    yaml_path: Path

    def head_sha(self) -> str | None:
        """Firmware identity the banner should carry: short HEAD sha plus
        '-dirty' when the working tree differs from HEAD (same algorithm as
        cmake/firmware_identity.cmake, so the comparison is meaningful)."""
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "--short=7", "HEAD"],
                cwd=self.firmware_dir, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=15, check=True,
            ).stdout.strip()
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.firmware_dir, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=15, check=True,
            ).stdout.strip()
            if status:
                sha += "-dirty"
            return sha
        except (subprocess.SubprocessError, OSError):
            return None


def load_board(yaml_path: Path) -> Board:
    yaml_path = yaml_path.resolve()
    try:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BoardError(f"cannot load board profile {yaml_path}: {exc}") from exc
    if not isinstance(raw, dict):
        # Empty file parses to None; a list/scalar document is equally
        # malformed — both must surface as BoardError so the Stop hook's
        # "gate misconfigured" branch (not a traceback) handles them.
        raise BoardError(
            f"board profile {yaml_path} is not a mapping "
            f"(got {type(raw).__name__})")

    fw = raw.get("firmware") or {}
    flash = raw.get("flash") or {}
    import re as _re
    flash_adapter = str(flash.get("adapter", "cubeprogrammer")).lower()
    if flash_adapter not in ("cubeprogrammer", "openocd", "fake"):
        raise BoardError(
            f"board profile {yaml_path.name}: flash.adapter must be one of "
            f"cubeprogrammer|openocd|fake, got {flash_adapter!r}")
    # Tcl-injection surface (adversarial review F1/F2): flash.address is a
    # VALUE inside an OpenOCD command line where ';' separates commands
    # and '}' escapes braces; target/interface strings reach -f scripts.
    # These are enumerations/paths, not free text — whitelist them.
    address = str(flash.get("address", "0x08000000"))
    if not _re.fullmatch(r"0x[0-9a-fA-F]{1,10}", address):
        raise BoardError(
            f"board profile {yaml_path.name}: flash.address must be "
            f"0x-prefixed hex (got {address!r})")
    _SAFE_CFG = _re.compile(r"[A-Za-z0-9_./+-]+")
    for key in ("openocd_target", "openocd_interface"):
        value = str(flash.get(key, ""))
        if value and not _SAFE_CFG.fullmatch(value):
            raise BoardError(
                f"board profile {yaml_path.name}: flash.{key} may only "
                f"contain [A-Za-z0-9_./+-] (got {value!r})")
    if (flash_adapter == "fake"
            and os.environ.get("FLASHGATE_ALLOW_FAKE") != "1"):
        raise BoardError(
            f"board profile {yaml_path.name}: flash.adapter 'fake' is a "
            "test adapter — set FLASHGATE_ALLOW_FAKE=1 to use it "
            "(a fake flash never writes hardware and can green-light a "
            "board that was never programmed)")
    ser = raw.get("serial") or {}
    ev = raw.get("evidence") or {}
    evidence_mode = str(ev.get("mode", "auto")).lower()
    if evidence_mode not in ("auto", "uart", "swd"):
        # A typo like 'uat' used to fall through to the UART path silently —
        # the gate would then vouch for evidence it was never configured to
        # collect. Unknown enum values are load-time errors.
        raise BoardError(
            f"board profile {yaml_path.name}: evidence.mode must be one of "
            f"auto|uart|swd, got {evidence_mode!r}")
    base = yaml_path.parent
    sig = ev.get("signature") or {}
    banner = str(ser.get("banner") or ser.get("banner_regex") or "")
    if not banner:
        raise BoardError(f"board profile {yaml_path.name} missing key: 'banner'")
    # Pre-compile at load (adversarial review F1 config-raiser): a broken
    # regex used to survive until mid-verify, crashing the boot step with
    # a bare re.error instead of surfacing as a load-time config error.
    from .probes import compile_pattern          # lazy: probes pulls pyserial
    try:
        compile_pattern(banner, anchor=False)
    except re.error as exc:
        raise BoardError(
            f"board profile {yaml_path.name}: banner pattern does not "
            f"compile: {exc}") from exc
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
            flash_adapter=flash_adapter,
            openocd_target=str(flash.get("openocd_target", "")),
            openocd_interface=str(flash.get("openocd_interface", "")),
            flash_address=str(flash.get("address", "0x08000000")),
            serial_port=str(ser.get("port", "") or ""),
            usb_vid=int(str(ser.get("vid", "0x1A86")), 0),
            usb_pids=tuple(int(str(p), 0) for p in ser.get("pids", [])),
            baudrate=int(ser.get("baudrate", 115200)),
            banner_regex=banner,
            banner_timeout_s=float(ser.get("banner_timeout_s", 15)),
            error_patterns=tuple(ser.get("error_patterns", [])),
            watch_globs=tuple((raw.get("gate") or {}).get("watch", DEFAULT_WATCH)),
            evidence_mode=evidence_mode,
            sig_address=int(str(sig.get("address", "0x2001FF00")), 0),
            sig_size=int(sig.get("size", 64)),
            coverage_notes=tuple(
                str(x) for x in ((raw.get("coverage") or {}).get("notes")
                                 or [])),
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
