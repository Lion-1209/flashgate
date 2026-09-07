"""flashgate MCP server: expose the hardware gate to any MCP-capable agent.

Run via `flashgate-mcp` (stdio transport). Requires the optional extra:

    pip install "flashgate[mcp]"

Wiring (.mcp.json, Claude Code compatible):

    {"mcpServers": {"flashgate": {
        "command": "flashgate-mcp",
        "args": ["--board", "/path/to/boards/apollo-h743.yaml"]}}}

Tools: board_info, doctor, build, flash, verify, probe, console_send,
console_read.

Every tool returns a `flashgate.results.Result` envelope (schema_version,
status, stable code, summary, CLI exit_code, data, warnings, policy) —
never a bare log the model has to parse. With mcp 2.x the envelope is
emitted as MCP structuredContent (registered via structured_output=True)
plus a JSON text block; on the mcp 1.x fallback the JSON text is still
produced. The text log of the underlying CLI run travels in data["log"].
Native ToolAnnotations (readOnly/destructive hints) are attached when the
installed SDK supports them.
"""

from __future__ import annotations

import functools
import inspect
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

try:
    from mcp.types import ToolAnnotations
except ImportError:                                         # pre-1.9 SDK
    ToolAnnotations = None                                  # type: ignore[assignment]

from . import __version__, flasher, probes as probe_mod, serialmon
from . import results
from .board import Board, BoardError, default_board_path, load_board
from . import cli as cli_mod

mcp = _Server(f"flashgate {__version__}")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BOARD_ARG: list[str] = []          # set from argv by main()

# Policy metadata (design doc §5.5 risk ladder), carried in every envelope.
P_READ = {"risk": "R0"}                 # read-only, no device state change
P_PROBE = {"risk": "R1"}                # drives device state, recoverable
P_BUILD = {"risk": "R0"}                # host-side only
P_FLASH = {"risk": "R2"}                # modifies the device
P_VERIFY = {"risk": "R2"}               # includes a flash
P_CONSOLE_SEND = {"risk": "R1"}         # open-world line into the firmware


def _board(board: str | None = None) -> Board:
    """Resolve the board profile: tool arg > server --board arg > default."""
    source = board or (_BOARD_ARG[0] if _BOARD_ARG else None)
    path = Path(source) if source else default_board_path()
    if path is None or not Path(path).is_file():
        raise BoardError(f"board profile not found: {source or 'boards/*.yaml'}")
    return load_board(Path(path))


def _capture(fn, *args) -> tuple[int, str]:
    """Run a CLI command function, return (exit code, printed output ANSI-stripped)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(*args)
    return rc, _ANSI.sub("", buf.getvalue()).strip()


def _cli_result(label: str, rc: int, log: str, policy: dict) -> results.Result:
    """Envelope from a CLI run: the log rides in data['log'], the summary is
    the log's first meaningful line so a model can triage without reading it."""
    first = next((ln for ln in log.splitlines() if ln.strip()), "")
    summary = f"{label}: {first[:140]}" if first else f"{label}: exit {rc}"
    return results.from_exit(rc, summary, log=log, policy=policy)


def _serial_error(exc: Exception, port: str | None, policy: dict) -> results.Result:
    return results.failure(
        f"cannot open {port}: {exc}" if port else str(exc),
        code=results.TRANSPORT_ERROR, status="incomplete", policy=policy)


def _register(annotations=None, structured: bool = False):
    """@tool decorator tolerant of mcp 2.x and 1.x: forwards annotations /
    structured_output only when the installed SDK's decorator accepts them."""
    def deco(fn):
        params = inspect.signature(mcp.tool).parameters
        kwargs = {}
        if annotations is not None and "annotations" in params:
            kwargs["annotations"] = annotations
        if structured and "structured_output" in params:
            kwargs["structured_output"] = True
        return mcp.tool(**kwargs)(fn)
    return deco


def _ann(**kwargs):
    return ToolAnnotations(**kwargs) if ToolAnnotations is not None else None


def _safe(policy: dict):
    """Last-resort guard: an exception escaping a tool must surface as an
    INTERNAL_ERROR envelope, never as an MCP protocol error — the
    envelope contract (design doc §5.1) has no room for bare crashes."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:            # noqa: BLE001 - by design
                return results.failure(
                    f"{type(exc).__name__}: {exc}",
                    code=results.INTERNAL_ERROR, policy=policy)
        return wrapper
    return deco


@_register(annotations=_ann(readOnlyHint=True), structured=True)
@_safe(P_READ)
def board_info(board: str | None = None) -> results.Result:
    """Show the active board profile: firmware dir, artifact, watch globs,
    banner contract, and the functional probes it defines."""
    try:
        b = _board(board)
    except BoardError as exc:
        return results.failure(str(exc), code=results.PROFILE_NOT_FOUND,
                               policy=P_READ)
    probes: list[str] = []
    warnings: list[str] = []
    try:
        probes = list(probe_mod.load_probes(b.yaml_path))
    except (OSError, ValueError) as exc:
        warnings.append(f"probes unloadable: {exc}")
    r = results.ok(
        f"{b.name} ({b.mcu}) — {len(probes)} probe(s) defined",
        data={
            "board": b.name,
            "mcu": b.mcu,
            "description": b.description,
            "firmware_dir": str(b.firmware_dir),
            "artifact": str(b.artifact),
            "flash": f"{b.flash_connect} @ {b.flash_address}",
            "banner": b.banner_regex,
            "gate_watch": list(b.watch_globs),
            "probes": probes,
        },
        policy=P_READ)
    r.warnings.extend(warnings)
    return r


@_register(annotations=_ann(readOnlyHint=True), structured=True)
@_safe(P_READ)
def doctor(board: str | None = None) -> results.Result:
    """Check hardware prerequisites: ST-Link probe, console serial port,
    toolchain. Run this first when anything else fails (exit code 6)."""
    try:
        rc, log = _capture(cli_mod.cmd_doctor, _board(board))
    except BoardError as exc:
        return results.failure(str(exc), code=results.PROFILE_NOT_FOUND,
                               policy=P_READ)
    return _cli_result("doctor", rc, log, P_READ)


@_register(annotations=_ann(idempotentHint=True), structured=True)
@_safe(P_BUILD)
def build(board: str | None = None) -> results.Result:
    """Build the firmware (incremental). Fails with code BUILD_FAILED."""
    try:
        rc, log = _capture(cli_mod.cmd_build, _board(board))
    except BoardError as exc:
        return results.failure(str(exc), code=results.PROFILE_NOT_FOUND,
                               policy=P_BUILD)
    return _cli_result("build", rc, log, P_BUILD)


@_register(annotations=_ann(destructiveHint=True), structured=True)
@_safe(P_FLASH)
def flash(board: str | None = None) -> results.Result:
    """Flash + verify + start via ST-Link (with auto-retry). Destructive:
    replaces the firmware running on the board."""
    try:
        rc, log = _capture(cli_mod.cmd_flash, _board(board))
    except BoardError as exc:
        return results.failure(str(exc), code=results.PROFILE_NOT_FOUND,
                               policy=P_FLASH)
    return _cli_result("flash", rc, log, P_FLASH)


@_register(annotations=_ann(destructiveHint=True), structured=True)
@_safe(P_VERIFY)
def verify(board: str | None = None) -> results.Result:
    """The full gate: build -> flash -> boot evidence -> identity check ->
    all functional probes. status=succeeded means the BOARD ITSELF confirms
    the firmware works. Failure codes: BUILD_FAILED, FLASH_FAILED,
    BOOT_EVIDENCE_TIMEOUT, BOOT_ERROR, IDENTITY_MISMATCH,
    CAPABILITY_UNAVAILABLE (console needed by probes but unavailable),
    PROBE_FAILED."""
    try:
        rc, log = _capture(cli_mod.cmd_verify, _board(board), ["all"])
    except BoardError as exc:
        return results.failure(str(exc), code=results.PROFILE_NOT_FOUND,
                               policy=P_VERIFY)
    return _cli_result("verify", rc, log, P_VERIFY)


@_register(annotations=_ann(readOnlyHint=False), structured=True)
@_safe(P_PROBE)
def probe(names: list[str] | None = None, board: str | None = None) -> results.Result:
    """Run functional probes against the ALREADY RUNNING firmware (no
    rebuild/reflash). names=None or [] runs every probe (same semantics as
    the CLI). The probe stage needs the console UART; without it the result
    is incomplete / CAPABILITY_UNAVAILABLE — never a pass."""
    try:
        b = _board(board)
    except BoardError as exc:
        return results.failure(str(exc), code=results.PROFILE_NOT_FOUND,
                               policy=P_PROBE)
    try:
        available = list(probe_mod.load_probes(b.yaml_path))
    except (OSError, ValueError, probe_mod.ProbeError) as exc:
        return results.failure(f"cannot load probes: {exc}",
                               code=results.CAPABILITY_UNAVAILABLE,
                               status="incomplete", policy=P_PROBE)
    requested = names or None                     # [] means "all", like the CLI
    if requested is not None:
        unknown = [n for n in requested if n not in available]
        if unknown:
            return results.failure(
                f"unknown probe {unknown[0]!r}; available: {available}",
                code=results.INVALID_ARGUMENT, policy=P_PROBE)
    port, why = serialmon.resolve_console_port(b.serial_port, b.usb_vid, b.usb_pids)
    if port is None:
        return results.failure(
            f"console serial unresolved — {why}",
            code=results.CAPABILITY_UNAVAILABLE, status="incomplete",
            policy=P_PROBE)
    try:
        conn = serialmon.open_flush(port, b.baudrate)
    except Exception as exc:  # pyserial SerialException
        return _serial_error(exc, port, P_PROBE)
    try:
        rc, log = _capture(cli_mod._run_probes, b, requested, conn)
    finally:
        conn.close()
    return _cli_result("probe", rc, log, P_PROBE)


@_register(annotations=_ann(readOnlyHint=False, openWorldHint=True), structured=True)
@_safe(P_CONSOLE_SEND)
def console_send(line: str, wait_s: float = 1.0, board: str | None = None) -> results.Result:
    """Send ONE line to the firmware console (e.g. 'led0?' or 'selftest')
    and return the response lines received within wait_s seconds."""
    try:
        b = _board(board)
    except BoardError as exc:
        return results.failure(str(exc), code=results.PROFILE_NOT_FOUND,
                               policy=P_CONSOLE_SEND)
    port, why = serialmon.resolve_console_port(b.serial_port, b.usb_vid, b.usb_pids)
    if port is None:
        return results.failure(
            f"console serial unresolved — {why}",
            code=results.CAPABILITY_UNAVAILABLE, status="incomplete",
            policy=P_CONSOLE_SEND)
    try:
        conn = serialmon.open_flush(port, b.baudrate)
    except Exception as exc:  # pyserial SerialException
        return _serial_error(exc, port, P_CONSOLE_SEND)
    try:
        conn.write((line + "\r\n").encode())
        deadline = time.monotonic() + max(0.1, wait_s)
        out = ""
        while time.monotonic() < deadline:
            out += conn.read(256).decode("utf-8", errors="replace")
    finally:
        conn.close()
    r = results.ok(f"sent {line!r}" + (" (no response)" if not out.strip() else ""),
                   data={"sent": line, "wait_s": wait_s,
                         "response": out.strip()},
                   policy=P_CONSOLE_SEND)
    if not out.strip():
        r.warnings.append("no response within wait_s")
    return r


@_register(annotations=_ann(readOnlyHint=True), structured=True)
@_safe(P_READ)
def console_read(seconds: float = 2.0, board: str | None = None) -> results.Result:
    """Read whatever the firmware prints on the console for N seconds
    (banner, self-test output, fault dumps)."""
    try:
        b = _board(board)
    except BoardError as exc:
        return results.failure(str(exc), code=results.PROFILE_NOT_FOUND,
                               policy=P_READ)
    port, why = serialmon.resolve_console_port(b.serial_port, b.usb_vid, b.usb_pids)
    if port is None:
        return results.failure(
            f"console serial unresolved — {why}",
            code=results.CAPABILITY_UNAVAILABLE, status="incomplete",
            policy=P_READ)
    try:
        conn = serialmon.open_flush(port, b.baudrate)
    except Exception as exc:  # pyserial SerialException
        return _serial_error(exc, port, P_READ)
    try:
        deadline = time.monotonic() + max(0.1, seconds)
        out = ""
        while time.monotonic() < deadline:
            out += conn.read(256).decode("utf-8", errors="replace")
    finally:
        conn.close()
    r = results.ok(f"read {seconds:.1f}s of console output"
                   + (" (console silent)" if not out.strip() else ""),
                   data={"text": out.strip()}, policy=P_READ)
    if not out.strip():
        r.warnings.append("console silent for the whole window")
    return r


def main() -> None:
    args = sys.argv[1:]
    if "--board" in args:
        i = args.index("--board")
        if i + 1 < len(args):
            _BOARD_ARG.append(args[i + 1])
    mcp.run()


if __name__ == "__main__":
    main()
