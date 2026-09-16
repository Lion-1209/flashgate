"""Debug backends: the adapter layer between the verify pipeline and the
physical debug probe (design doc §5.6, Phase 2).

A backend owns EVERYTHING probe-specific: discovery, flashing, memory
access. The verify pipeline — and everything above it: records, bench,
MCP — is backend-agnostic. Swapping CubeProgrammer for OpenOCD is a
one-line board-profile change (`flash.adapter:`) and zero upper-layer
edits; that substitution property is the Phase 2 acceptance criterion.

Semantics every backend must honor (they are the gate's, not the
adapter's):
- flash(start=False) writes and verifies but does NOT run: the caller
  wipes the boot signature in between, then start_app() — so a stale
  RAM signature from a previous boot can never lie about identity.
- write32/read_mem are the wipe/poll primitives behind that contract.
- The `connect` string is opaque and backend-specific (CubeProgrammer
  syntax like "port=SWD"; ignored by OpenOCD, which targets via its own
  config derived from the board's MCU).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from . import flasher, swdsig
from .sttools import augmented_env, find_cubeprogrammer

# OpenOCD target scripts by MCU family prefix. Explicit override lives in
# the board profile (`flash.openocd_target`) for anything unmapped.
_OPENOCD_TARGETS: tuple[tuple[str, str], ...] = (
    ("STM32H7", "stm32h7x"),
    ("STM32F7", "stm32f7x"),
    ("STM32F4", "stm32f4x"),
    ("STM32F1", "stm32f1x"),
    ("STM32L4", "stm32l4x"),
    ("STM32G0", "stm32g0x"),
    ("STM32G4", "stm32g4x"),
    ("STM32U5", "stm32u5x"),
    ("STM32WB", "stm32wbx"),
    ("STM32WL", "stm32wlx"),
)
_OPENOCD_TIMEOUT_S = 60


def openocd_target_for(mcu: str) -> str | None:
    mcu = (mcu or "").upper()
    for prefix, target in _OPENOCD_TARGETS:
        if mcu.startswith(prefix):
            return target
    return None


def find_openocd() -> Path | None:
    """openocd.exe via PATH, $OPENOCD_BIN, or the xpack layout."""
    exe = "openocd.exe" if shutil.os.name == "nt" else "openocd"
    found = shutil.which(exe, path=augmented_env().get("PATH", ""))
    if found:
        return Path(found)
    import os
    for env_name in ("OPENOCD_BIN", "OPENOCD_HOME"):
        base = os.environ.get(env_name)
        if base:
            cand = Path(base)
            if cand.is_file():
                return cand
            hit = cand / "bin" / exe
            if hit.is_file():
                return hit
    return None


class DebugBackend(ABC):
    """The probe-facing surface the verify pipeline depends on."""

    name: str = "abstract"

    @abstractmethod
    def available(self) -> str | None:
        """Usable executable path (doctor shows it), or None."""

    @abstractmethod
    def discover(self) -> str:
        """Human-readable listing of attached probes (doctor)."""

    @abstractmethod
    def flash(self, bin_path: Path, connect: str, address: str,
              start: bool = True) -> flasher.FlashResult: ...

    @abstractmethod
    def write32(self, connect: str, value: int, address: int) -> bool: ...

    @abstractmethod
    def start_app(self, connect: str) -> bool: ...

    @abstractmethod
    def read_mem(self, connect: str, address: int, size: int) -> bytes | None: ...

    def probe_detected(self, discover_output: str) -> bool:
        """Did discover() actually see a probe? Backend-specific markers —
        each backend knows its own tool's output format."""
        return bool(discover_output.strip())


class CubeProgrammerBackend(DebugBackend):
    """STM32CubeProgrammer CLI over ST-Link — the original implementation,
    unchanged semantics, now behind the interface."""

    name = "cubeprogrammer"

    def available(self) -> str | None:
        cli = find_cubeprogrammer()
        return str(cli) if cli else None

    def discover(self) -> str:
        return flasher.list_stlink()

    def flash(self, bin_path, connect, address, start=True):
        return flasher.flash(bin_path, connect, address, start=start)

    def write32(self, connect, value, address) -> bool:
        return flasher.write32(connect, value, address)

    def start_app(self, connect) -> bool:
        return flasher.start_app(connect)

    def read_mem(self, connect, address, size):
        return swdsig.read_ram(connect, address, size)

    def probe_detected(self, discover_output: str) -> bool:
        return "ST-LINK SN" in discover_output


class OpenOcdBackend(DebugBackend):
    """OpenOCD over ST-Link (or any adapter OpenOCD speaks). The second
    flash implementation that validates the abstraction — and the
    standard route to Linux/ARM64 bench hosts (e.g. Raspberry Pi), where
    CubeProgrammer is the awkward dependency.

    `connect` is ignored (it carries CubeProgrammer syntax); targeting
    comes from the board's MCU mapped to an OpenOCD target script, or an
    explicit `flash.openocd_target` in the profile. Interface defaults
    to ST-Link; override with `flash.openocd_interface`.
    """

    name = "openocd"

    def __init__(self, mcu: str = "",
                 interface: str = "interface/stlink-dap.cfg",
                 target: str | None = None):
        self._interface = interface or "interface/stlink-dap.cfg"
        self._target = target or openocd_target_for(mcu)

    def available(self) -> str | None:
        exe = find_openocd()
        return str(exe) if exe else None

    def _run(self, commands: list[str]) -> tuple[int, str]:
        exe = find_openocd()
        if self._target is None:
            return -1, ("no OpenOCD target mapping for this MCU — set "
                        "flash.openocd_target in the board profile")
        if exe is None:
            return -1, "openocd not found (install it or set OPENOCD_BIN)"
        cmd = [str(exe)]
        # xpack layout keeps scripts outside bin/ — pass the search dir
        # explicitly so `-f target/...` resolves regardless of build.
        scripts = exe.parent.parent / "openocd" / "scripts"
        if scripts.is_dir():
            cmd += ["-s", str(scripts)]
        # target names map into the target/ script dir; profile overrides
        # may carry a path or extension already
        t = self._target
        target_cfg = t if ("/" in t or t.endswith(".cfg")) else f"target/{t}.cfg"
        i = self._interface
        interface_cfg = i if ("/" in i or i.endswith(".cfg")) else f"interface/{i}.cfg"
        cmd += ["-c", "adapter speed 4000",
                "-f", interface_cfg, "-f", target_cfg,
                # mdw/mww/reset need an initialized session; `program`
                # inits internally and tolerates the explicit one.
                "-c", "init"]
        for c in commands:
            cmd += ["-c", c]
        cmd += ["-c", "shutdown"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_OPENOCD_TIMEOUT_S,
                encoding="utf-8", errors="replace",
                # Neutral cwd (F3): OpenOCD resolves relative -f paths
                # against ITS cwd first — running from the gated repo
                # would let a checked-in target/*.cfg shadow the stock
                # scripts.
                cwd=str(exe.parent),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return -1, str(exc)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def discover(self) -> str:
        rc, out = self._run(["adapter list"])
        if rc == 0:
            return out.strip()
        return f"(openocd probe listing unavailable: {out[-300:]})"

    def probe_detected(self, discover_output: str) -> bool:
        # a connected ST-Link shows up as VID:PID 0483:... plus a DPIDR
        # line once init reaches the target
        return ("VID:PID 0483:" in discover_output
                or "DPIDR" in discover_output)

    def flash(self, bin_path: Path, connect: str, address: str,
              start: bool = True) -> flasher.FlashResult:
        if not Path(bin_path).is_file():
            return flasher.FlashResult(False, f"artifact not found: {bin_path}")
        # Tcl eats backslashes as escapes — hand OpenOCD forward slashes,
        # braced so nothing else gets substituted. Braces have NO escape
        # mechanism: a path containing '{' or '}' cannot be passed safely
        # and is refused outright (F2).
        tcl_bin = str(Path(bin_path)).replace("\\", "/")
        if "{" in tcl_bin or "}" in tcl_bin:
            return flasher.FlashResult(
                False, f"artifact path contains '{{' or '}}' — cannot be "
                f"passed to OpenOCD safely: {tcl_bin}")
        steps = [f"program {{{tcl_bin}}} {address} verify"]
        if start:
            steps.append("reset run")
        rc, out = self._run(steps)
        # OpenOCD prints '** Programming Finished **' on success
        ok = rc == 0 and "** Programming Finished **" in out
        return flasher.FlashResult(ok, out.strip()[-1200:])

    def write32(self, connect: str, value: int, address: int) -> bool:
        rc, out = self._run([f"mww 0x{address:08x} 0x{value:08x}"])
        return rc == 0

    def start_app(self, connect: str) -> bool:
        rc, out = self._run(["reset run"])
        return rc == 0

    def read_mem(self, connect: str, address: int, size: int) -> bytes | None:
        """mdw-based read; returns None on any failure (poll semantics)."""
        words = (size + 3) // 4
        rc, out = self._run([f"mdw 0x{address:08x} {words}"])
        if rc != 0:
            return None
        got: list[int] = []
        # OpenOCD mdw format: "0x2001ff00: f1a5c0de 00010001 ..." —
        # the ADDRESS carries 0x, the words do not.
        for m in re.finditer(r"0x[0-9a-fA-F]+:\s+((?:[0-9a-fA-F]{8}\s*)+)",
                             out):
            for w in re.finditer(r"[0-9a-fA-F]{8}", m.group(1)):
                got.append(int(w.group(0), 16))
        if len(got) < words:
            return None
        blob = b"".join(w.to_bytes(4, "little") for w in got[:words])
        return blob[:size]


class FakeBackend(DebugBackend):
    """CI adapter: no probe, no hardware — scripts decide what happens
    (design doc §14: build fail / flash timeout / stale identity / probe
    assertion fail, all in-process)."""

    name = "fake"

    def __init__(self, *, flash_ok: bool = True, signature: bytes | None = None):
        self.flash_ok = flash_ok
        self.signature = signature
        self.calls: list[str] = []

    def available(self) -> str | None:
        return "fake"

    def discover(self) -> str:
        return "(fake backend)"

    def flash(self, bin_path, connect, address, start=True):
        self.calls.append(f"flash:{bin_path}@{address}:start={start}")
        return flasher.FlashResult(self.flash_ok, "fake flash")

    def write32(self, connect, value, address):
        self.calls.append(f"write32:{address:#x}={value:#x}")
        return True

    def start_app(self, connect):
        self.calls.append("start_app")
        return True

    def read_mem(self, connect, address, size):
        self.calls.append(f"read:{address:#x}+{size}")
        return self.signature[:size] if self.signature else None


_BACKENDS: dict[str, type] = {
    CubeProgrammerBackend.name: CubeProgrammerBackend,
    OpenOcdBackend.name: OpenOcdBackend,
    FakeBackend.name: FakeBackend,
}


def known_backends() -> list[str]:
    return sorted(_BACKENDS)


def get_backend(name: str, **kwargs) -> DebugBackend:
    try:
        return _BACKENDS[name](**kwargs)
    except KeyError:
        raise ValueError(
            f"unknown debug backend {name!r}; known: {known_backends()}") from None
