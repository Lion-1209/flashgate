"""flashgate MCP server: expose the hardware gate to any MCP-capable agent.

Run via `flashgate-mcp` (stdio transport). Requires the optional extra:

    pip install "flashgate[mcp]"

Wiring (.mcp.json, Claude Code compatible):

    {"mcpServers": {"flashgate": {
        "command": "flashgate-mcp",
        "args": ["--board", "/path/to/boards/apollo-h743.yaml"]}}}

Tools: board_info, doctor, build, flash, verify, probe, console_send,
console_read. Every tool returns plain text (ANSI stripped) — the same
output the CLI prints, plus the flashgate exit-code contract.
"""

from __future__ import annotations

import io
import re
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer as _Server   # mcp 2.x
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP as _Server   # mcp 1.x
    except ImportError as exc:  # pragma: no cover - friendly extra hint
        raise SystemExit(
            "flashgate MCP server needs the optional dependency: "
            'pip install "flashgate[mcp]"'
        ) from exc

from . import __version__, flasher, probes as probe_mod, serialmon
from .board import Board, BoardError, default_board_path, load_board
from . import cli as cli_mod

mcp = _Server(f"flashgate {__version__}")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BOARD_ARG: list[str] = []          # set from argv by main()


def _board(board: str | None = None) -> Board:
    """Resolve the board profile: tool arg > server --board arg > default."""
    source = board or (_BOARD_ARG[0] if _BOARD_ARG else None)
    path = Path(source) if source else default_board_path()
    if path is None or not Path(path).is_file():
        raise BoardError(f"board profile not found: {source or 'boards/*.yaml'}")
    return load_board(Path(path))


def _capture(fn, *args) -> str:
    """Run a CLI command function, return its printed output, ANSI-stripped."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(*args)
    text = _ANSI.sub("", buf.getvalue()).strip()
    return f"exit code: {rc}\n{text}"


def _err(exc: Exception) -> str:
    return f"error: {exc}"


@mcp.tool()
def board_info(board: str | None = None) -> str:
    """Show the active board profile: firmware dir, artifact, watch globs,
    banner contract, and the functional probes it defines."""
    try:
        b = _board(board)
    except BoardError as exc:
        return _err(exc)
    lines = [
        f"board       : {b.name} ({b.mcu})",
        f"description : {b.description}",
        f"firmware    : {b.firmware_dir}",
        f"artifact    : {b.artifact}",
        f"flash       : {b.flash_connect} @ {b.flash_address}",
        f"banner      : {b.banner_regex}",
        f"gate.watch  : {', '.join(b.watch_globs)}",
    ]
    try:
        names = list(probe_mod.load_probes(b.yaml_path))
        lines.append(f"probes      : {', '.join(names) if names else '(none)'}")
    except (OSError, ValueError) as exc:
        lines.append(f"probes      : (unloadable: {exc})")
    return "\n".join(lines)


@mcp.tool()
def doctor(board: str | None = None) -> str:
    """Check hardware prerequisites: ST-Link probe, console serial port,
    toolchain. Run this first when anything else fails (exit code 6)."""
    try:
        return _capture(cli_mod.cmd_doctor, _board(board))
    except BoardError as exc:
        return _err(exc)


@mcp.tool()
def build(board: str | None = None) -> str:
    """Build the firmware (incremental). Exit codes: 0 ok, 1 build failed."""
    try:
        return _capture(cli_mod.cmd_build, _board(board))
    except BoardError as exc:
        return _err(exc)


@mcp.tool()
def flash(board: str | None = None) -> str:
    """Flash + verify + start via ST-Link (with auto-retry). Exit codes:
    0 ok, 2 flash failed, 6 environment error."""
    try:
        return _capture(cli_mod.cmd_flash, _board(board))
    except BoardError as exc:
        return _err(exc)


@mcp.tool()
def verify(board: str | None = None) -> str:
    """The full gate: build -> flash -> boot banner -> sha check -> all
    functional probes. Exit 0 means the BOARD ITSELF confirms the firmware
    works. 1 build, 2 flash, 3 no banner, 4 boot error, 5 sha mismatch,
    6 env, 7 probe failure."""
    try:
        return _capture(cli_mod.cmd_verify, _board(board), ["all"])
    except BoardError as exc:
        return _err(exc)


@mcp.tool()
def probe(names: list[str] | None = None, board: str | None = None) -> str:
    """Run functional probes against the ALREADY RUNNING firmware (no
    rebuild/reflash). names=None runs every probe. Exit 7 = probe failed."""
    try:
        b = _board(board)
    except BoardError as exc:
        return _err(exc)
    port, why = serialmon.resolve_console_port(b.serial_port, b.usb_vid, b.usb_pids)
    if port is None:
        return f"error: console serial unresolved — {why}"
    try:
        conn = serialmon.open_flush(port, b.baudrate)
    except Exception as exc:  # pyserial SerialException
        return f"error: cannot open {port}: {exc}"
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_mod._run_probes(b, names, conn)
        return f"exit code: {rc}\n{_ANSI.sub('', buf.getvalue()).strip()}"
    finally:
        conn.close()


@mcp.tool()
def console_send(line: str, wait_s: float = 1.0, board: str | None = None) -> str:
    """Send ONE line to the firmware console (e.g. 'led0?' or 'selftest')
    and return the response lines received within wait_s seconds."""
    try:
        b = _board(board)
    except BoardError as exc:
        return _err(exc)
    port, why = serialmon.resolve_console_port(b.serial_port, b.usb_vid, b.usb_pids)
    if port is None:
        return f"error: console serial unresolved — {why}"
    try:
        conn = serialmon.open_flush(port, b.baudrate)
    except Exception as exc:
        return f"error: cannot open {port}: {exc}"
    try:
        conn.write((line + "\r\n").encode())
        deadline = time.monotonic() + max(0.1, wait_s)
        out = ""
        while time.monotonic() < deadline:
            out += conn.read(256).decode("utf-8", errors="replace")
        return out.strip() or "(no response)"
    finally:
        conn.close()


@mcp.tool()
def console_read(seconds: float = 2.0, board: str | None = None) -> str:
    """Read whatever the firmware prints on the console for N seconds
    (banner, self-test output, fault dumps)."""
    try:
        b = _board(board)
    except BoardError as exc:
        return _err(exc)
    port, why = serialmon.resolve_console_port(b.serial_port, b.usb_vid, b.usb_pids)
    if port is None:
        return f"error: console serial unresolved — {why}"
    try:
        conn = serialmon.open_flush(port, b.baudrate)
    except Exception as exc:
        return f"error: cannot open {port}: {exc}"
    try:
        deadline = time.monotonic() + max(0.1, seconds)
        out = ""
        while time.monotonic() < deadline:
            out += conn.read(256).decode("utf-8", errors="replace")
        return out.strip() or "(silence)"
    finally:
        conn.close()


def main() -> None:
    args = sys.argv[1:]
    if "--board" in args:
        i = args.index("--board")
        if i + 1 < len(args):
            _BOARD_ARG.append(args[i + 1])
    mcp.run()


if __name__ == "__main__":
    main()
