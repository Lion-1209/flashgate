"""flashgate CLI: doctor / build / flash / verify / console.

Exit-code contract (the M3 Stop hook enforces these):
  0 verified | 1 build failed | 2 flash failed | 3 no banner (timeout)
  4 boot error string | 5 identity mismatch (git sha / board name, or
  the pre-start signature wipe failed / its readback ANSWERED wrongly
  — identity untrustworthy; a readback that cannot run at all is 6)
  6 environment error (incl. probes required but console unavailable)
  7 functional probe failed
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
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
from . import backends, flasher, gatestate, probes as probe_mod, records, serialmon, swdsig, verifylock
from .sttools import augmented_env

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


def _board_backend(board: Board) -> backends.DebugBackend:
    """The debug backend a board profile selects (Phase 2)."""
    if board.flash_adapter == "openocd":
        return backends.OpenOcdBackend(
            mcu=board.mcu,
            target=board.openocd_target or None,
            interface=board.openocd_interface or None)
    return backends.get_backend(board.flash_adapter)


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

    backend = _board_backend(board)
    exe = backend.available()
    if exe:
        print(_green(f"  backend    : {backend.name} ({exe})"))
    else:
        problems.append(f"backend {backend.name!r} executable not found")
        print(_red(f"  backend    : {backend.name} — EXECUTABLE NOT FOUND"))

    if exe:
        listing = backend.discover()
        if backend.probe_detected(listing):
            print(_green("  probe      : detected"))
        else:
            problems.append("no debug probe detected (check USB, power, driver)")
            print(_red("  probe      : none detected"))

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
            board.flash_connect, board.sig_address, board.sig_size, timeout_s=2.0,
            read_fn=backend.read_mem)
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


def _build(board: Board, j: records.VerifyJournal | None = None) -> int:
    print(_cyan(f"[build] {board.build_command}  (in {board.firmware_dir})"))
    build_ninja = board.firmware_dir / "build" / "Debug" / "build.ninja"
    if not build_ninja.is_file() and board.configure_command:
        print(_cyan(f"[configure] {board.configure_command}"))
        code, out = _run(board.configure_command, board.firmware_dir)
        if code != 0:
            if j:
                j.check("build", "failed", f"configure failed (exit {code})")
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
            if j:
                j.check("build", "failed", out[-300:].strip())
            return EXIT_BUILD
    warnings = [ln for ln in out.splitlines() if "warning:" in ln]
    tail = [ln for ln in out.splitlines() if ln.startswith(("[", "Memory region", "FLASH", "text"))][-4:]
    for ln in tail:
        print(f"  {ln}")
    print(_green(f"[build] OK in {elapsed:.1f}s, {len(warnings)} warning(s)"))
    if not board.artifact.is_file():
        print(_red(f"[build] artifact missing after build: {board.artifact}"))
        if j:
            j.check("build", "failed", f"artifact missing after build: {board.artifact}")
        return EXIT_BUILD
    if j:
        j.check("build", "passed", f"OK in {elapsed:.1f}s, {len(warnings)} warning(s)",
                duration_ms=int(elapsed * 1000))
    return EXIT_OK


def cmd_build(board: Board) -> int:
    return _build(board)


def cmd_flash(board: Board) -> int:
    backend = _board_backend(board)
    print(_cyan(f"[flash] {board.artifact.name} @ {board.flash_address} via "
                f"{backend.name}:{board.flash_connect}"))
    result = backend.flash(board.artifact, board.flash_connect, board.flash_address)
    if not result.ok:
        print(_red("[flash] FAILED"))
        print(result.detail[-1200:])
        return EXIT_FLASH
    print(_green("[flash] OK (written, verified, started)"))
    return EXIT_OK


def _console_port(board: Board) -> tuple[str | None, str]:
    return serialmon.resolve_console_port(board.serial_port, board.usb_vid, board.usb_pids)


def _run_probes(board: Board, names: list[str] | None, conn,
                j: records.VerifyJournal | None = None) -> int:
    """Run named probes (or all) on an open console connection. The string
    'all' selects every defined probe (used by --all-probes / the Stop hook)."""
    try:
        available = probe_mod.load_probes(board.yaml_path)
    except (OSError, yaml.YAMLError, probe_mod.ProbeError) as exc:
        print(_red(f"[probe] cannot load probes: {exc}"))
        if j:
            j.check("probes", "failed", f"cannot load probes: {exc}")
        return EXIT_ENV
    if not available:
        print(_red(f"[probe] no probes defined in {board.yaml_path.name}"))
        if j:
            j.check("probes", "failed", "no probes defined in the board profile")
        return EXIT_ENV

    selected = list(available) if names is None else names
    for name in selected:
        if name not in available:
            print(_red(f"[probe] unknown probe {name!r}; available: {list(available)}"))
            if j:
                j.check(f"probe:{name}", "failed",
                        f"unknown probe; available: {list(available)}")
            return EXIT_ENV

    for name in selected:
        probe = available[name]
        print(_cyan(f"[probe] {name}: {probe.description}"))
        result = probe_mod.run_probe(conn, probe)
        if not result.ok:
            print(_red(f"[probe] FAIL — {result.detail}"))
            if j:
                j.check(f"probe:{name}", "failed", result.detail)
                j.add_evidence("probe-transcript", name, result.detail)
            return EXIT_PROBE_FAIL
        print(_green(f"[probe] {name}: PASS ({len(probe.steps)} steps)"))
        if j:
            j.check(f"probe:{name}", "passed", f"{len(probe.steps)} steps")
    return EXIT_OK


def _probe_plan(board: Board, probe_names: list[str] | None) -> list[str]:
    """Planned per-probe check names — what the record EXPECTS to see run."""
    if probe_names is None:
        return []
    if probe_names == ["all"]:
        try:
            return [f"probe:{n}" for n in probe_mod.load_probes(board.yaml_path)]
        except Exception:
            return ["probes"]          # load failure itself gets recorded
    return [f"probe:{n}" for n in probe_names]


def _write_verify_record(board: Board, j: records.VerifyJournal, rc: int) -> None:
    """Persist the run's evidence record. Strictly auxiliary: a failure to
    write must never change the verification outcome."""
    try:
        fingerprint = j.fingerprint or gatestate.tree_fingerprint(
            board.firmware_dir, board.yaml_path)
        firmware: dict = {
            "dir": str(board.firmware_dir),
            "git_sha": board.head_sha(),
            "tree_fingerprint": fingerprint,
            "artifact": str(board.artifact),
        }
        if board.artifact.is_file():
            digest, size = records.sha256_file(board.artifact)
            firmware["artifact_sha256"] = digest
            firmware["artifact_bytes"] = size
        j.note("firmware", firmware)
        profile_sha, _ = records.sha256_file(board.yaml_path,
                                             cap=gatestate._HASH_CAP_BYTES)
        record = j.to_record(board, rc, records.status_word(rc),
                             summary=_exit_summary(rc))
        record["coverage"] = records.build_coverage(board, record)
        record["board"]["profile_sha256"] = profile_sha
        path = records.write_record(record, board.firmware_dir, fingerprint)
        print(_cyan(f"[record] {path.name}  ({records.summarize_record(record)})"))
    except (OSError, ValueError) as exc:
        print(_yellow(f"[record] could not persist verify record: {exc}"))


def _exit_summary(rc: int) -> str:
    return {
        0: "board confirmed the firmware",
        1: "build failed", 2: "flash failed",
        3: "board stayed silent (no boot evidence)",
        4: "boot error string on console",
        5: "on-board identity != repo state, or identity untrustworthy "
           "(signature wipe failed, or its readback ANSWERED wrongly)",
        6: "environment error — a required check could not run",
        7: "functional probe failed",
    }.get(rc, f"exit {rc}")


def _verify_lock_wait() -> float:
    """Bench-lock wait budget; $FLASHGATE_VERIFY_LOCK_WAIT overrides (seconds).

    Only finite non-negative floats are honored: nan would time out every
    verify instantly, inf would wait forever — both silently break the
    bounded-wait contract, so they fall back to the default with a warning."""
    raw = os.environ.get("FLASHGATE_VERIFY_LOCK_WAIT")
    if raw is None:
        return verifylock.DEFAULT_WAIT_S
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not math.isfinite(value) or value < 0:
        print(_yellow(f"[verify] ignoring invalid FLASHGATE_VERIFY_LOCK_WAIT={raw!r} "
                      "(expected non-negative seconds as a float)"))
        return verifylock.DEFAULT_WAIT_S
    return value


def cmd_verify(board: Board, probe_names: list[str] | None,
               evidence: str | None = None) -> int:
    """One hardware verify at a time per bench (Stop-hook stacking fix):
    a second verify — another hook level, a manual run racing the hook —
    waits for the lock instead of fighting for the console port, then
    times out to a truthful exit 6 rather than a misleading serial error."""
    wait_s = _verify_lock_wait()
    t0 = time.monotonic()
    lock = verifylock.BenchLock(board.firmware_dir)
    try:
        lock.acquire(wait_s)
    except verifylock.VerifyLockTimeout:
        waited = time.monotonic() - t0
        print(_red(f"[verify] bench busy: another verify is still holding the "
                   f"bench lock (waited {waited:.0f}s of the {wait_s:.1f}s "
                   "budget) — retry when it finishes, or raise "
                   "FLASHGATE_VERIFY_LOCK_WAIT"))
        # Forensics only: identify the tree the verdict WOULD have applied
        # to. Any failure here must not change the outcome.
        fingerprint = ""
        try:
            fingerprint = gatestate.tree_fingerprint(board.firmware_dir,
                                                     board.yaml_path)
        except Exception:
            pass
        j = records.VerifyJournal([], mode="bench-busy",
                                  probe_names=probe_names,
                                  fingerprint=fingerprint)
        if fingerprint:
            j.note("firmware", {"dir": str(board.firmware_dir),
                                "git_sha": board.head_sha(),
                                "tree_fingerprint": fingerprint})
        j.check("verify", "skipped",
                f"bench lock held by another verify; waited {waited:.1f}s")
        try:
            record = j.to_record(board, EXIT_ENV, records.status_word(EXIT_ENV),
                                 summary="bench busy: another verify holds the lock")
            records.write_record(record, board.firmware_dir, fingerprint)
            print(_cyan(f"[record] {record['record_id']}.json  "
                        f"({records.summarize_record(record)})"))
        except (OSError, ValueError) as exc:
            print(_yellow(f"[record] could not persist verify record: {exc}"))
        return EXIT_ENV
    except OSError as exc:
        # Lock SETUP failed only (unwritable state dir): fail as an env
        # error — record writing needs the same dir, so no record either.
        # Errors raised by the verify body below are NOT caught here.
        print(_red(f"[verify] cannot set up the bench lock: {exc}"))
        return EXIT_ENV
    try:
        return _cmd_verify(board, probe_names, evidence)
    finally:
        lock.release()


def _cmd_verify(board: Board, probe_names: list[str] | None,
                evidence: str | None = None) -> int:
    mode = (evidence or board.evidence_mode or "auto").lower()
    if mode not in ("auto", "uart", "swd"):
        print(_red(f"[verify] invalid evidence mode {mode!r} (expected auto|uart|swd)"))
        return EXIT_ENV
    if mode == "auto":
        # A console-port SCAN failure (pyserial enumerating COM ports can
        # raise) must not escape as a bare traceback — auto falls back to
        # the swd evidence path, which enforces its own console rules when
        # probes are explicitly requested.
        try:
            has_console = _console_port(board)[0] is not None
        except OSError as exc:
            print(_yellow(f"[verify] console port scan failed ({exc}); "
                          "evidence mode falls back to swd"))
            has_console = False
        mode = "uart" if has_console else "swd"

    plan = (["console", "build", "flash", "boot", "identity"] if mode == "uart"
            else ["build", "flash", "boot", "identity"])
    plan += _probe_plan(board, probe_names)
    # PRE-run identity: the same fingerprint the Stop hook decided on. A
    # post-run computation would diverge whenever the build drops
    # untracked-unignored artifacts (adversarial review F8).
    try:
        fingerprint = gatestate.tree_fingerprint(board.firmware_dir,
                                                 board.yaml_path)
    except Exception:
        fingerprint = ""
    j = records.VerifyJournal(plan, mode=mode, probe_names=probe_names,
                              fingerprint=fingerprint)

    # Crash net (adversarial review F1): an exception escaping a verify
    # path used to skip the record entirely AND leak rc=1 (misread as
    # BUILD_FAILED). Worst crashes leave evidence and keep the contract.
    import traceback
    try:
        if mode == "swd":
            rc = _verify_swd(board, probe_names, j)
        else:
            rc = _verify_uart(board, probe_names, j)
    except Exception as exc:                  # KeyboardInterrupt passes through
        traceback.print_exc()
        print(_red(f"[verify] aborted: {type(exc).__name__}: {exc}"))
        j.check("verify", "failed",
                f"aborted: {type(exc).__name__}: {exc}")
        rc = EXIT_ENV
    _write_verify_record(board, j, rc)
    return rc


def _verify_swd(board: Board, probe_names: list[str] | None,
                j: records.VerifyJournal | None = None) -> int:
    """Boot gate through the ST-Link alone: fixed-address RAM signature.
    No serial cable needed. Probes explicitly requested via --probe /
    --all-probes (as the Stop hook does) MUST run: if the console is
    missing or unusable, verify FAILS (exit 6) — a check that didn't
    happen must never count as a pass.

    `j` (the evidence journal) is optional: direct callers without one
    get a throwaway — records are only persisted by cmd_verify."""
    if j is None:
        j = records.VerifyJournal([], mode="swd", probe_names=probe_names)
    print(_cyan(f"[verify] {board.name}: build -> flash -> SWD signature"))
    rc = _build(board, j)
    if rc != EXIT_OK:
        j.ensure_failed("build", f"exit {rc}")
        return rc
    j.ensure_passed("build")

    # Flash WITHOUT starting, wipe the stale signature, then start: RAM is
    # not cleared by reset, so a surviving old-boot signature would lie.
    backend = _board_backend(board)
    result = backend.flash(board.artifact, board.flash_connect, board.flash_address,
                           start=False)
    if not result.ok:
        print(_red("[flash] FAILED"))
        print(result.detail[-1200:])
        j.check("flash", "failed", result.detail[-300:].strip())
        return EXIT_FLASH
    if not backend.write32(board.flash_connect, 0, board.sig_address):
        # Fail closed: an unwiped stale signature could pass identity for
        # a build that never booted. Withholding the start keeps the board
        # from running firmware whose identity we could no longer vouch
        # for, and exit 5 closes the identity gate (a warning here used
        # to leave a false-pass window open).
        print(_red("[verify] SIGNATURE WIPE FAILED — a stale old-boot signature "
                   "could lie about identity; failing closed (start withheld)"))
        j.check("flash", "failed",
                "signature wipe failed — start withheld "
                "(stale-identity false-pass window)")
        return EXIT_SHA_MISMATCH
    # Read the wiped word back: a backend can report success while the
    # write never landed (silent lie), which would leave the stale-identity
    # window open. An unverifiable wipe fails closed exactly like a failed
    # one — "probably wiped" is not evidence. The semantic axis (N0
    # follow-up, adversarial M1): a readback that ANSWERED wrongly
    # (nonzero, or a short/empty dump with rc 0) is an identity verdict
    # -> exit 5; a readback that could not RUN at all (OSError spawn,
    # SwdError tool failure, None return) is an environment failure
    # -> exit 6 — never a pass either way, start withheld both ways.
    try:
        wiped = backend.read_mem(board.flash_connect, board.sig_address, 4)
    except (swdsig.SwdError, OSError) as exc:
        print(_red(f"[verify] SIGNATURE WIPE READBACK could not run: "
                   f"{type(exc).__name__}: {exc} — failing closed "
                   "(start withheld)"))
        j.check("flash", "failed",
                f"readback could not run ({type(exc).__name__}: "
                f"{exc}) — start withheld")
        return EXIT_ENV
    if wiped is None:
        # the tool answered nothing at all — environment, not identity
        print(_red("[verify] SIGNATURE WIPE READBACK could not run: "
                   "backend returned no data — failing closed "
                   "(start withheld)"))
        j.check("flash", "failed",
                "readback could not run (no data) — start withheld")
        return EXIT_ENV
    if len(wiped) != 4:
        reason = (f"the post-wipe readback returned {len(wiped)} byte(s) "
                  f"instead of 4")
    elif any(wiped):
        reason = ("the post-wipe readback is not zero (write reported "
                  "success but the memory disagrees)")
    else:
        reason = ""
    if reason:
        print(_red(f"[verify] SIGNATURE WIPE NOT CONFIRMED — {reason}; "
                   "failing closed (start withheld)"))
        j.check("flash", "failed",
                f"signature wipe not confirmed by readback ({reason}) — "
                "start withheld")
        return EXIT_SHA_MISMATCH
    if not backend.start_app(board.flash_connect):
        print(_red("[flash] FAILED to start the application"))
        j.check("flash", "failed", "start_app failed")
        return EXIT_FLASH
    j.check("flash", "passed", "written+verified, signature wiped (readback "
            "confirmed), started")

    print(_cyan(f"[verify] polling signature @ {board.sig_address:#010x} via {board.flash_connect}"))
    info, err = swdsig.wait_for_signature(
        board.flash_connect, board.sig_address, board.sig_size,
        timeout_s=board.banner_timeout_s, read_fn=backend.read_mem)
    if info is None:
        if "not supported" in err:
            print(_red(f"[verify] SIGNATURE LAYOUT MISMATCH: {err}"))
            j.check("boot", "failed", f"signature layout mismatch: {err}")
            return EXIT_ENV
        print(_red(f"[verify] TIMEOUT: board never published its SWD signature ({err})"))
        j.check("boot", "failed", f"timeout: no signature ({err})")
        return EXIT_BANNER_TIMEOUT

    print(_green(f"[verify] signature OK: git={info['git']} build={info['build']} "
                 f"flags={info['flags']:#x}"))
    j.check("boot", "passed",
            f"signature: git={info['git']} build={info['build']} flags={info['flags']:#x}")
    j.add_evidence("swd-signature", f"RAM@{board.sig_address:#010x}",
                   " ".join(f"{k}={v}" for k, v in sorted(info.items())))

    expected = board.head_sha()
    if expected and info["git"] != expected:
        print(_red(f"[verify] SHA MISMATCH: board runs {info['git']}, repo HEAD is {expected} "
                   "(rebuild after committing?)"))
        j.check("identity", "failed",
                f"board git={info['git']} != repo HEAD {expected}")
        return EXIT_SHA_MISMATCH
    j.check("identity", "passed", f"git={info['git']} matches repo HEAD")

    if probe_names is not None:
        port, why = _console_port(board)
        if port is None:
            print(_red("[verify] probes were explicitly requested but no console serial "
                       f"is available ({why}) — failing rather than passing unverified "
                       "(drop --probe/--all-probes, or connect the console UART)"))
            for name in plan_probes(j):
                j.check(name, "skipped", f"console unavailable: {why}")
            return EXIT_ENV
        try:
            conn = serialmon.open_flush(port, board.baudrate)
        except serial.SerialException as exc:
            print(_red(f"[verify] probes were explicitly requested but {port} cannot be "
                       f"opened ({exc}) — failing rather than passing unverified"))
            for name in plan_probes(j):
                j.check(name, "skipped", f"console open failed: {exc}")
            return EXIT_ENV
        try:
            prc = _run_probes(board, None if probe_names == ["all"] else probe_names,
                              conn, j)
            if prc == EXIT_OK:
                for name in plan_probes(j):
                    j.ensure_passed(name)
            return prc
        finally:
            conn.close()

    print(_green(f"[verify] PASS — the board's RAM itself confirms the firmware booted "
                 f"(git={info['git']}), no serial cable involved"))
    return EXIT_OK


def plan_probes(j: records.VerifyJournal) -> list[str]:
    return [n for n in j.plan if n.startswith("probe:") or n == "probes"]


def _check_banner_identity(board: Board, info: dict,
                           j: records.VerifyJournal | None = None) -> int | None:
    """Compare the banner's identity fields against the board profile.
    Returns an exit code on contradiction, None when consistent.

    Strictness (design-doc Phase 0): a field the banner pattern PROMISES
    (a named group) must actually be present and non-empty in the match —
    a legacy regex with an optional group could match a banner that omits
    `git=`, and the old code silently skipped the sha comparison."""
    promised = probe_mod.compile_pattern(board.banner_regex, anchor=False).groupindex
    for field in ("board", "git"):
        if field in promised and not info.get(field):
            print(_red(f"[verify] IDENTITY INCOMPLETE: banner matched but the "
                       f"promised field {field!r} is empty/missing"))
            if j:
                j.check("identity", "failed",
                        f"banner promised {field!r} but the match lacks it")
            return EXIT_SHA_MISMATCH
    got_board = info.get("board")
    if got_board and got_board != board.name:
        print(_red(f"[verify] BOARD MISMATCH: banner says board={got_board}, "
                   f"profile expects {board.name} — the verdict would not be "
                   "about the board you configured"))
        if j:
            j.check("identity", "failed",
                    f"banner board={got_board} != profile {board.name}")
        return EXIT_SHA_MISMATCH
    expected = board.head_sha()
    got = info.get("git")
    if expected and got and expected != got:
        print(_red(f"[verify] SHA MISMATCH: board runs {got}, repo HEAD is {expected} "
                   "(rebuild after committing?)"))
        if j:
            j.check("identity", "failed",
                    f"banner git={got} != repo HEAD {expected}")
        return EXIT_SHA_MISMATCH
    if j:
        j.check("identity", "passed",
                f"board={info.get('board')} git={got or '(not promised)'}")
    return None


def _banner_line(transcript: str) -> str:
    lines = [ln for ln in transcript.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _verify_uart(board: Board, probe_names: list[str] | None,
                 j: records.VerifyJournal | None = None) -> int:
    if j is None:
        j = records.VerifyJournal([], mode="uart", probe_names=probe_names)
    all_probes = probe_names == ["all"]
    title = "[verify] {b}: build -> flash -> boot banner" + (" -> probes" if probe_names is not None else "")
    print(_cyan(title.format(b=board.name)))

    port, why = _console_port(board)
    if port is None:
        print(_red(f"[verify] console serial port unresolved — {why}"))
        j.check("console", "failed", f"serial port unresolved: {why}")
        return EXIT_ENV
    try:
        conn = serialmon.open_flush(port, board.baudrate)
    except serial.SerialException as exc:
        print(_red(f"[verify] cannot open {port}: {exc} — close any serial terminal "
                   "(串口助手/putty/VSCode serial monitor) holding the port, then retry"))
        j.check("console", "failed", f"cannot open {port}: {exc}")
        return EXIT_ENV
    j.check("console", "passed", f"{port} @ {board.baudrate} [{why}]")

    try:
        rc = _build(board, j)
        if rc != EXIT_OK:
            j.ensure_failed("build", f"exit {rc}")
            return rc
        j.ensure_passed("build")

        rc = cmd_flash(board)
        if rc != EXIT_OK:
            j.check("flash", "failed", f"exit {rc} (see log)")
            return rc
        j.check("flash", "passed", "written, verified, started")

        print(_cyan(f"[verify] waiting for boot banner on {port} "
                    f"(timeout {board.banner_timeout_s:.0f}s)"))
        banner = serialmon.wait_on(
            conn, board.banner_regex, board.error_patterns, board.banner_timeout_s
        )

        if banner.error_hit:
            print(_red(f"[verify] BOOT ERROR: error pattern {banner.error_hit!r} in output"))
            j.check("boot", "failed", f"error pattern {banner.error_hit!r} on console")
            j.add_evidence("console-tail", port, banner.transcript[-1000:])
            return EXIT_BOOT_ERROR
        if not banner.matched:
            print(_red("[verify] TIMEOUT: board never printed the FLASHGATE-BOOT banner"))
            print("  last serial output:")
            for ln in banner.transcript.splitlines()[-5:]:
                print(f"    | {ln}")
            j.check("boot", "failed", "timeout: no banner within "
                    f"{board.banner_timeout_s:.0f}s")
            j.add_evidence("console-tail", port, banner.transcript[-1000:])
            return EXIT_BANNER_TIMEOUT

        info = banner.groups or {}
        banner_line = banner.matched_line or _banner_line(banner.transcript)
        print(_green(f"[verify] banner OK: board={info.get('board')} git={info.get('git')} "
                     f"build={info.get('build')} rtos={info.get('rtos')}"))
        j.check("boot", "passed", "banner: " + banner_line)
        j.add_evidence("uart-banner", port, banner_line)

        mismatch = _check_banner_identity(board, info, j)
        if mismatch is not None:
            return mismatch

        if probe_names is not None:
            return _run_probes(board, None if all_probes else probe_names, conn, j)

        print(_green(f"[verify] PASS — the board itself confirms the firmware booted "
                     f"(git={info.get('git')})"))
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
    except serial.SerialException as exc:
        print(_red(f"[console] {exc}"))
        return EXIT_ENV
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
    p_verify.add_argument("--json", action="store_true",
                          help="print this run's evidence record as JSON on "
                               "stdout (human logs move to stderr)")
    p_probe = sub.add_parser("probe", help="run probes against running firmware")
    p_probe.add_argument("names", nargs="*", metavar="NAME",
                         help="probe names (default: all defined in the board profile)")
    sub.add_parser("console", help="live serial monitor")
    p_bench = sub.add_parser(
        "bench-serve", help="expose this bench over device-connect (optional extra: flashgate[bench])")
    p_bench.add_argument("--device-id", default=None,
                         help="device-connect id (default: flashgate-bench-<board>)")
    p_bench.add_argument("--stop", action="store_true",
                         help="signal the running bench-serve for this "
                              "board to stop (drains, then exits) instead "
                              "of starting a new one")

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
            if getattr(args, "json", False):
                # stdout is the machine channel: human logs go to stderr
                with contextlib.redirect_stdout(sys.stderr):
                    rc = cmd_verify(board, names, getattr(args, "evidence", None))
                last = records.current_last()
                if last is not None and Path(last["fw_dir"]) == board.firmware_dir:
                    # ensure_ascii (default): the JSON must survive ANY
                    # consumer codepage — a cp936 pipe meeting U+FFFD from
                    # serial noise used to raise UnicodeEncodeError, wipe
                    # stdout and exit 1, which scripts misread as BUILD
                    # FAILED (adversarial review R1).
                    print(json.dumps(last["record"]))
                else:
                    print(json.dumps({"error": "no record written",
                                      "exit_code": rc}))
                return rc
            return cmd_verify(board, names, getattr(args, "evidence", None))
        if args.cmd == "probe":
            return cmd_probe(board, args.names or None)
        if args.cmd == "bench-serve":
            from .bench_serve import serve, stop_bench
            if args.stop:
                try:
                    stopping = stop_bench(board.firmware_dir)
                except ValueError as exc:   # bad FLASHGATE_BENCH_LOCK_PORT_BASE
                    print(_red(f"[bench-serve] {exc}"))
                    return 2
                if stopping is True:
                    print("[bench-serve] stop signalled — the server drains "
                          "its in-flight operation, then exits")
                    return 0
                if stopping == "draining":
                    print("[bench-serve] the lock is held by a listener "
                          "that is not answering — almost certainly a "
                          "bench-serve already draining after an earlier "
                          "--stop. Wait for it to exit; if nothing ever "
                          "exits, look for a silent third-party service "
                          "(netstat) or move the lock range via "
                          "FLASHGATE_BENCH_LOCK_PORT_BASE.")
                    return 0
                print("[bench-serve] no bench-serve is holding the lock for "
                      f"{board.firmware_dir} on the current lock-port base "
                      "(FLASHGATE_BENCH_LOCK_PORT_BASE) — if the server was "
                      "started with a different base, stop it from a shell "
                      "with that same base set")
                return 2
            return serve(board, args.device_id)
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
