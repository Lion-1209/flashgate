"""flashgate CLI: doctor / build / flash / verify / console.

Exit-code contract (the M3 Stop hook enforces these):
  0 verified | 1 build failed | 2 flash failed | 3 no banner (timeout)
  4 boot error string | 5 git sha mismatch | 6 environment error
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import serial
import yaml

from . import __version__
from .board import Board, BoardError, default_board_path, load_board
from . import flasher, probes as probe_mod, serialmon, swdsig
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


def _foreign_missing_tool(output: str) -> str | None:
    """Locale-independent detection of 'tool not on PATH' failures: cmd.exe
    errors quote the tool name ('cube-cmake' ...), regardless of language."""
    for name in re.findall(r"'([^'\r\n]{2,64})'", output):
        if " " in name or "/" in name or "\\" in name:
            continue
        if not shutil.which(name, path=augmented_env().get("PATH", "")):
            return name
    return None


def _colors_enabled() -> bool:
    """Colors only when stdout is an interactive terminal that can render
    ANSI — legacy PowerShell/conhost would print raw escape codes, and
    piped/logged output shouldn't carry them either."""
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            ENABLE_VT = 0x0004                            # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            if not (mode.value & ENABLE_VT):
                kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT)
            return True
        except OSError:
            return False
    return True


_COLOR = _colors_enabled()


def _green(text: str) -> str:  return f"\033[32m{text}\033[0m" if _COLOR else text
def _red(text: str) -> str:    return f"\033[31m{text}\033[0m" if _COLOR else text
def _yellow(text: str) -> str: return f"\033[33m{text}\033[0m" if _COLOR else text
def _cyan(text: str) -> str:   return f"\033[36m{text}\033[0m" if _COLOR else text


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

    # Live SWD signature: what the board is running RIGHT NOW, no serial needed
    try:
        info, _ = swdsig.wait_for_signature(
            board.flash_connect, board.sig_address, board.sig_size, timeout_s=2.0)
        if info:
            print(_green(f"  on-board   : git={info['git']} build={info['build']} (SWD signature)"))
        else:
            print(_yellow("  on-board   : no SWD signature (old firmware?)"))
    except swdsig.SwdError as exc:
        print(_yellow(f"  on-board   : SWD read unavailable ({exc})"))

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
        # A foreign configure (e.g. the VSCode STM32 extension records its own
        # tool names like 'cube-cmake' into build.ninja) breaks builds outside
        # that environment. Self-heal: reconfigure with our cmake, rebuild.
        foreign = _foreign_missing_tool(out)
        if foreign and board.configure_command:
            print(_yellow(f"[build] {foreign!r} (recorded by a foreign configure) "
                          "not on PATH — reconfiguring with ST-bundle cmake"))
            ccode, _ = _run(board.configure_command, board.firmware_dir)
            if ccode == 0:
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


def cmd_verify(board: Board, probe_names: list[str] | None, evidence: str | None = None) -> int:
    mode = (evidence or board.evidence_mode or "auto").lower()
    if mode == "auto":
        mode = "uart" if _console_port(board)[0] else "swd"

    if mode == "swd":
        return _verify_swd(board, probe_names)
    return _verify_uart(board, probe_names)


def _verify_swd(board: Board, probe_names: list[str] | None) -> int:
    """Boot gate through the ST-Link alone: fixed-address RAM signature.
    No serial cable needed; functional probes are skipped (they need the
    console) unless a probe run is explicitly requested and a port exists."""
    print(_cyan(f"[verify] {board.name}: build -> flash -> SWD signature"))
    rc = _build(board)
    if rc != EXIT_OK:
        return rc

    # Flash WITHOUT starting, wipe the stale signature, then start: RAM is
    # not cleared by reset, so a surviving old-boot signature would lie.
    result = flasher.flash(board.artifact, board.flash_connect, board.flash_address,
                           start=False)
    if not result.ok:
        print(_red("[flash] FAILED"))
        print(result.detail[-1200:])
        return EXIT_FLASH
    if not flasher.write32(board.flash_connect, 0, board.sig_address):
        print(_yellow("[verify] warning: could not wipe the old signature "
                      "(stale-identity false-pass window)"))
    if not flasher.start_app(board.flash_connect):
        print(_red("[flash] FAILED to start the application"))
        return EXIT_FLASH

    print(_cyan(f"[verify] polling signature @ {board.sig_address:#010x} via {board.flash_connect}"))
    info, err = swdsig.wait_for_signature(
        board.flash_connect, board.sig_address, board.sig_size,
        timeout_s=board.banner_timeout_s)
    if info is None:
        print(_red(f"[verify] TIMEOUT: board never published its SWD signature ({err})"))
        return EXIT_BANNER_TIMEOUT

    print(_green(f"[verify] signature OK: git={info['git']} build={info['build']} "
                 f"flags={info['flags']:#x}"))

    expected = board.head_sha()
    if expected and info["git"] != expected:
        print(_red(f"[verify] SHA MISMATCH: board runs {info['git']}, repo HEAD is {expected} "
                   "(rebuild after committing?)"))
        return EXIT_SHA_MISMATCH

    if probe_names is not None:
        port, _ = _console_port(board)
        if port is None:
            print(_yellow("[verify] probes skipped: no console serial in swd mode"))
        else:
            try:
                conn = serialmon.open_flush(port, board.baudrate)
            except serial.SerialException as exc:
                print(_yellow(f"[verify] probes skipped: cannot open {port} ({exc})"))
                return EXIT_OK
            try:
                return _run_probes(board, None if probe_names == ["all"] else probe_names, conn)
            finally:
                conn.close()
        return EXIT_OK

    print(_green(f"[verify] PASS — the board's RAM itself confirms the firmware booted "
                 f"(git={info['git']}), no serial cable involved"))
    return EXIT_OK


def _verify_uart(board: Board, probe_names: list[str] | None) -> int:
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
    p_verify.add_argument("--evidence", choices=["uart", "swd", "auto"],
                          help="boot-evidence channel (default: board profile evidence.mode)")
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
            return cmd_verify(board, names, getattr(args, "evidence", None))
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
