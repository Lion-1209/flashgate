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
status, stable code, summary, CLI exit_code, data, evidence, warnings,
policy) —
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

from . import __version__, flasher, probes as probe_mod, records, serialmon
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
    """Discover what hardware you are working with: the active board
    profile.

    Call this FIRST in any session — it tells you the board identity
    (name, MCU), where the firmware builds (firmware_dir, artifact), how
    it flashes (flash connection), and — most importantly — which
    functional probes exist (the "probes" list holds the names the probe
    tool accepts). Read-only; touches neither host nor board.

    Result: data carries the full profile; warnings flag an unloadable
    probes section (fix the board YAML before relying on probes)."""
    try:
        b = _board(board)
    except BoardError as exc:
        return results.failure(str(exc), code=results.PROFILE_NOT_FOUND,
                               policy=P_READ)
    probes: list[str] = []
    warnings: list[str] = []
    try:
        probes = list(probe_mod.load_probes(b.yaml_path))
    except (OSError, ValueError, probe_mod.ProbeError) as exc:
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
    """Diagnose the bench: is the ST-Link probe visible, is the console
    serial port resolvable, is the cross toolchain installed?

    Run this before anything else when a board operation fails — and
    ALWAYS when a tool returns status "incomplete" or code
    CAPABILITY_UNAVAILABLE; the report names the exact missing link.
    Read-only. Findings are listed in data.log under "issues:"; an empty
    problems list with exit_code 0 means every prerequisite is healthy."""
    try:
        rc, log = _capture(cli_mod.cmd_doctor, _board(board))
    except BoardError as exc:
        return results.failure(str(exc), code=results.PROFILE_NOT_FOUND,
                               policy=P_READ)
    return _cli_result("doctor", rc, log, P_READ)


@_register(annotations=_ann(idempotentHint=True), structured=True)
@_safe(P_BUILD)
def build(board: str | None = None) -> results.Result:
    """Compile the board's firmware (incremental — recompiles only what
    changed). No hardware is touched.

    Mostly useful on its own for a fast failure loop; verify runs the
    same build internally, so you don't need this before verify. Result:
    succeeded + exit_code 0 = clean build; failed + BUILD_FAILED = the
    compiler output in data.log names the offending file and line. The
    binary lands at the profile's artifact path, ready for flash."""
    try:
        rc, log = _capture(cli_mod.cmd_build, _board(board))
    except BoardError as exc:
        return results.failure(str(exc), code=results.PROFILE_NOT_FOUND,
                               policy=P_BUILD)
    return _cli_result("build", rc, log, P_BUILD)


@_register(annotations=_ann(destructiveHint=True), structured=True)
@_safe(P_FLASH)
def flash(board: str | None = None) -> results.Result:
    """Write the built firmware to the board's flash over ST-Link,
    verify the write, then start the application. DESTRUCTIVE: whatever
    runs on the MCU is replaced.

    Flashes the artifact from the last build — call build first if
    sources changed since. Tries up to 3 times on transient probe
    errors. Any failure — including a missing ST-Link or CubeProgrammer —
    surfaces as failed + FLASH_FAILED, exit_code 2; data.log carries the
    programmer output. Run doctor to tell an environment problem (no
    probe / no tools) apart from a write failure."""
    try:
        rc, log = _capture(cli_mod.cmd_flash, _board(board))
    except BoardError as exc:
        return results.failure(str(exc), code=results.PROFILE_NOT_FOUND,
                               policy=P_FLASH)
    return _cli_result("flash", rc, log, P_FLASH)


@_register(annotations=_ann(destructiveHint=True), structured=True)
@_safe(P_VERIFY)
def verify(board: str | None = None) -> results.Result:
    """The hardware gate — the authoritative answer to "does this
    firmware actually work on the board?". status=succeeded means the
    BOARD ITSELF booted the exact tree you are on (its reported git sha
    matches the working tree) and every functional probe passed. Trust no
    other signal; a clean build is not a pass.

    What it does: rebuild the tree, flash over ST-Link, start the app,
    then require boot evidence proving WHICH build is running (UART
    banner; or, without a serial cable, an SWD RAM signature — wiped
    first, best-effort, so a stale one cannot lie), then run every
    defined probe, asserting on the board's answers — including live
    register readbacks where the board provides them. DESTRUCTIVE:
    rewrites the board's flash.

    The failure code names the broken stage — BUILD_FAILED (compile),
    FLASH_FAILED (write/start), BOOT_EVIDENCE_TIMEOUT (board silent),
    BOOT_ERROR (fault string on serial), IDENTITY_MISMATCH (board runs a
    different tree), CAPABILITY_UNAVAILABLE (a required check could not
    run — most often probes needing the console UART, which is missing or
    held by another program), PROBE_FAILED (a functional assertion did
    not hold). Full transcript in data.log; data.record carries the
    persisted evidence record of this run (per-check verdicts, banner /
    signature / probe evidence, artifact sha256) and data.record_dir is
    where the JSON audit trail accumulates."""
    try:
        board_obj = _board(board)
    except BoardError as exc:
        return results.failure(str(exc), code=results.PROFILE_NOT_FOUND,
                               policy=P_VERIFY)
    t0 = time.monotonic()
    rc, log = _capture(cli_mod.cmd_verify, board_obj, ["all"])
    result = _cli_result("verify", rc, log, P_VERIFY)
    # Attach THIS run's record — the in-process registry, never a blind
    # mtime pick (adversarial review F3: a failed run whose own record
    # write broke must not carry some earlier run's green record).
    try:
        last = records.LAST
        if (last is not None and last["at"] >= t0
                and Path(last["fw_dir"]) == board_obj.firmware_dir):
            result.data["record"] = last["record"]
            result.data["record_dir"] = str(records.records_dir(board_obj.firmware_dir))
    except (OSError, ValueError, KeyError, TypeError):   # evidence, not the gate
        pass
    return result


@_register(annotations=_ann(readOnlyHint=False), structured=True)
@_safe(P_PROBE)
def probe(names: list[str] | None = None, board: str | None = None) -> results.Result:
    """Exercise the firmware ALREADY RUNNING on the board with its
    defined probes: each probe sends console commands and asserts on the
    board's answers — including live register readbacks (e.g. the timer
    value actually driving an LED). No rebuild, no reflash.

    names picks specific probes (valid names are in board_info's
    "probes" list); omit it or pass [] to run all. Probes need the
    console UART: without it the result is incomplete +
    CAPABILITY_UNAVAILABLE — never a pass. failed + PROBE_FAILED means an
    assertion did not hold; data.log shows the exact step, the board's
    last response, and which value was off."""
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
    """Talk to the firmware directly: send ONE command line on the
    console UART and collect its answer.

    Example: line "led0?" — the firmware replies with its LED state and
    the live PWM register value (a hardware readback, not a cached
    variable). The command set is board-specific — board_info and the
    board's probe definitions show the known commands; unknown commands
    typically answer "ERR unknown-cmd". The reply lands in
    data.response within wait_s seconds; silence is reported as
    "(no response)" in the summary + a warning — check wiring before
    concluding the firmware is dead."""
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
    """Listen passively: capture everything the firmware prints on the
    console for the given number of seconds, sending nothing.

    Use it to watch boot banners, self-test prints, or fault dumps
    (HardFault traces appear here). Output lands in data.text; a fully
    silent window is reported as "(console silent)" in the summary —
    which for a just-reset board usually means wiring or baud-rate
    trouble rather than a healthy quiet firmware. Read-only."""
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
