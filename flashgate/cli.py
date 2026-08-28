"""flashgate CLI: doctor / build / flash / verify / console.

Exit-code contract (the M3 Stop hook enforces these):
  0 verified | 1 build failed | 2 flash failed | 3 no banner (timeout)
  4 boot error string | 5 git sha mismatch | 6 environment error
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

import serial
import yaml

from . import __version__
from .board import Board, BoardError, default_board_path, load_board
from . import flasher, probes as probe_mod, serialmon
from .sttools import augmented_env, find_cubeprogrammer

EXIT_OK = 0
EXIT_BUILD = 1
EXIT_FLASH = 2
EXIT_BANNER_TIMEOUT = 3
EXIT_BOOT_ERROR = 4
EXIT_SHA_MISMATCH = 5
EXIT_ENV = 6
EXIT_PROBE_FAIL = 7

BUILD_TIMEOUT_S = 300


def _green(text: str) -> str:  return f"\033[32m{text}\033[0m"
def _red(text: str) -> str:    return f"\033[31m{text}\033[0m"
def _yellow(text: str) -> str: return f"\033[33m{text}\033[0m"
def _cyan(text: str) -> str:   return f"\033[36m{text}\033[0m"


def _resolve_board(args: argparse.Namespace) -> Board:
    path = Path(args.board) if args.board else default_board_path()
    if path is None or not Path(path).is_file():
        hint = args.board or "boards/*.yaml"
        raise BoardError(f"board profile not found: {hint}")
    return load_board(Path(path))


def _run(cmd: str, cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, shell=True, cwd=cwd, capture_output=True, text=True,
        timeout=BUILD_TIMEOUT_S, env=augmented_env(),
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def cmd_doctor(board: Board) -> int:
    print(_cyan(f"flashgate doctor — {board.name} ({board.mcu})"))
    print(f"  firmware   : {board.firmware_dir}")
    print(f"  artifact   : {board.artifact}")
    problems: list[str] = []

    cli = find_cubeprogrammer()
    if cli:
        print(_green(f"  programmer : {cli}"))
    else:
        problems.append("STM32CubeProgrammer CLI not found")
        print(_red("  programmer : NOT FOUND"))

    if cli:
        listing = flasher.list_stlink()
        sn_lines = [ln.strip() for ln in listing.splitlines() if "ST-LINK SN" in ln]
        if sn_lines:
            print(_green(f"  ST-Link    : {sn_lines[0]}"))
        else:
            problems.append("no ST-Link probe detected (check USB, power, driver)")
            print(_red("  ST-Link    : none detected"))

    port, why = serialmon.resolve_console_port(board.serial_port, board.usb_vid, board.usb_pids)
    if port:
        print(_green(f"  console    : {port} @ {board.baudrate}  [{why}]"))
    else:
        problems.append(f"console serial port unresolved: {why}")
        print(_red(f"  console    : UNRESOLVED — {why}"))

    env = augmented_env()
    for tool in ("cmake", "ninja", "arm-none-eabi-gcc"):
        found = shutil.which(tool, path=env.get("PATH"))
        if found:
            print(_green(f"  {tool:<10} : {found}"))
        else:
            problems.append(f"{tool} not found in PATH or ST bundles")
            print(_red(f"  {tool:<10} : NOT FOUND"))

    sha = board.head_sha()
    print(f"  HEAD sha   : {sha or 'unknown'}")

    if problems:
        print(_yellow("  issues:"))
        for p in problems:
            print(_yellow(f"    - {p}"))
        return EXIT_ENV
    print(_green("  all prerequisites OK"))
    return EXIT_OK


def _build(board: Board) -> int:
    print(_cyan(f"[build] {board.build_command}  (in {board.firmware_dir})"))
    build_ninja = board.firmware_dir / "build" / "Debug" / "build.ninja"
    if not build_ninja.is_file() and board.configure_command:
        print(_cyan(f"[configure] {board.configure_command}"))
        code, out = _run(board.configure_command, board.firmware_dir)
        if code != 0:
            print(_red(out[-2000:]))
            return EXIT_BUILD

    t0 = time.monotonic()
    code, out = _run(board.build_command, board.firmware_dir)
    elapsed = time.monotonic() - t0
    if code != 0:
        print(_red(f"[build] FAILED (exit {code})"))
        print(out[-2000:])
        return EXIT_BUILD
    warnings = [ln for ln in out.splitlines() if "warning:" in ln]
    tail = [ln for ln in out.splitlines() if ln.startswith(("[", "Memory region", "FLASH", "text"))][-4:]
    for ln in tail:
        print(f"  {ln}")
    print(_green(f"[build] OK in {elapsed:.1f}s, {len(warnings)} warning(s)"))
    if not board.artifact.is_file():
        print(_red(f"[build] artifact missing after build: {board.artifact}"))
        return EXIT_BUILD
    return EXIT_OK


def cmd_build(board: Board) -> int:
    return _build(board)


def cmd_flash(board: Board) -> int:
    print(_cyan(f"[flash] {board.artifact.name} @ {board.flash_address} via {board.flash_connect}"))
    result = flasher.flash(board.artifact, board.flash_connect, board.flash_address)
    if not result.ok:
        print(_red("[flash] FAILED"))
        print(result.detail[-1200:])
        return EXIT_FLASH
    print(_green("[flash] OK (written, verified, started)"))
    return EXIT_OK


def _console_port(board: Board) -> tuple[str | None, str]:
    return serialmon.resolve_console_port(board.serial_port, board.usb_vid, board.usb_pids)


def _run_probes(board: Board, names: list[str] | None, conn) -> int:
    """Run named probes (or all) on an open console connection. The string
    'all' selects every defined probe (used by --all-probes / the Stop hook)."""
    try:
        available = probe_mod.load_probes(board.yaml_path)
    except (OSError, yaml.YAMLError, probe_mod.ProbeError) as exc:
        print(_red(f"[probe] cannot load probes: {exc}"))
        return EXIT_ENV
    if not available:
        print(_red(f"[probe] no probes defined in {board.yaml_path.name}"))
        return EXIT_ENV

    selected = list(available) if names is None else names
    for name in selected:
        if name not in available:
            print(_red(f"[probe] unknown probe {name!r}; available: {list(available)}"))
            return EXIT_ENV

    for name in selected:
        probe = available[name]
        print(_cyan(f"[probe] {name}: {probe.description}"))
        result = probe_mod.run_probe(conn, probe)
        if not result.ok:
            print(_red(f"[probe] FAIL — {result.detail}"))
            return EXIT_PROBE_FAIL
        print(_green(f"[probe] {name}: PASS ({len(probe.steps)} steps)"))
    return EXIT_OK


def cmd_verify(board: Board, probe_names: list[str] | None) -> int:
    all_probes = probe_names == ["all"]
    title = "[verify] {b}: build -> flash -> boot banner" + (" -> probes" if probe_names is not None else "")
    print(_cyan(title.format(b=board.name)))

    port, why = _console_port(board)
    if port is None:
        print(_red(f"[verify] console serial port unresolved — {why}"))
        return EXIT_ENV
    try:
        conn = serialmon.open_flush(port, board.baudrate)
    except serial.SerialException as exc:
        print(_red(f"[verify] cannot open {port}: {exc} — close any serial terminal "
                   "(串口助手/putty/VSCode serial monitor) holding the port, then retry"))
        return EXIT_ENV

    try:
        rc = _build(board)
        if rc != EXIT_OK:
            return rc

        rc = cmd_flash(board)
        if rc != EXIT_OK:
            return rc

        print(_cyan(f"[verify] waiting for boot banner on {port} "
                    f"(timeout {board.banner_timeout_s:.0f}s)"))
        banner = serialmon.wait_on(
            conn, board.banner_regex, board.error_patterns, board.banner_timeout_s
        )

        if banner.error_hit:
            print(_red(f"[verify] BOOT ERROR: error pattern {banner.error_hit!r} in output"))
            return EXIT_BOOT_ERROR
        if not banner.matched:
            print(_red("[verify] TIMEOUT: board never printed the FLASHGATE-BOOT banner"))
            print("  last serial output:")
            for ln in banner.transcript.splitlines()[-5:]:
                print(f"    | {ln}")
            return EXIT_BANNER_TIMEOUT

        info = banner.groups or {}
        print(_green(f"[verify] banner OK: board={info.get('board')} git={info.get('git')} "
                     f"build={info.get('build')} rtos={info.get('rtos')}"))

        expected = board.head_sha()
        got = info.get("git")
        if expected and got and expected != got:
            print(_red(f"[verify] SHA MISMATCH: board runs {got}, repo HEAD is {expected} "
                       "(rebuild after committing?)"))
            return EXIT_SHA_MISMATCH

        if probe_names is not None:
            return _run_probes(board, None if all_probes else probe_names, conn)

        print(_green(f"[verify] PASS — the board itself confirms the firmware booted "
                     f"(git={got})"))
        return EXIT_OK
    finally:
        conn.close()


def cmd_probe(board: Board, names: list[str] | None) -> int:
    """Standalone probe run against already-running firmware (no build/flash)."""
    port, why = _console_port(board)
    if port is None:
        print(_red(f"[probe] console serial port unresolved — {why}"))
        return EXIT_ENV
    try:
        conn = serialmon.open_flush(port, board.baudrate)
    except serial.SerialException as exc:
        print(_red(f"[probe] cannot open {port}: {exc}"))
        return EXIT_ENV
    try:
        return _run_probes(board, names, conn)
    finally:
        conn.close()


def cmd_console(board: Board) -> int:
    port, why = _console_port(board)
    if port is None:
        print(_red(f"console serial port unresolved — {why}"))
        return EXIT_ENV
    print(_cyan(f"[console] {port} @ {board.baudrate} — Ctrl+C to exit"))
    try:
        serialmon.console_forever(port, board.baudrate)
    except KeyboardInterrupt:
        print()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flashgate",
        description="Hardware-in-the-loop verification gate: the agent can't claim "
                    "firmware works until the board says so.",
    )
    parser.add_argument("--board", help="path to a board profile yaml (default: boards/*.yaml)")
    parser.add_argument("--version", action="version", version=f"flashgate {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check ST-Link, console serial, toolchain")
    sub.add_parser("build", help="build the firmware")
    sub.add_parser("flash", help="flash + verify + start via ST-Link")
    p_verify = sub.add_parser("verify", help="full loop: build -> flash -> banner -> sha")
    p_verify.add_argument("--probe", action="append", metavar="NAME",
                          help="run functional probes after banner (repeatable)")
    p_verify.add_argument("--all-probes", action="store_true",
                          help="run every probe defined in the board profile")
    p_probe = sub.add_parser("probe", help="run probes against running firmware")
    p_probe.add_argument("names", nargs="*", metavar="NAME",
                         help="probe names (default: all defined in the board profile)")
    sub.add_parser("console", help="live serial monitor")

    args = parser.parse_args(argv)
    try:
        board = _resolve_board(args)
    except BoardError as exc:
        print(_red(str(exc)))
        return EXIT_ENV

    try:
        if args.cmd == "verify":
            names: list[str] | None = args.probe
            if args.all_probes:
                names = ["all"]
            return cmd_verify(board, names)
        if args.cmd == "probe":
            return cmd_probe(board, args.names or None)
        simple = {
            "doctor": cmd_doctor, "build": cmd_build,
            "flash": cmd_flash, "console": cmd_console,
        }
        return simple[args.cmd](board)
    except KeyboardInterrupt:
        print()
        return EXIT_ENV


if __name__ == "__main__":
    sys.exit(main())
