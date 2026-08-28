# flashgate

**中文说明见下方** | The agent can't claim the firmware works — until the board says so.

flashgate is a hardware-in-the-loop verification gate for coding agents (Claude Code, Codex, coderio, …).
It closes the loop that every coding agent is missing on firmware work:

```
build → flash over ST-Link → board boots → banner over serial → exit code
```

The firmware prints a boot banner carrying its **git sha + build time**:

```
FLASHGATE-BOOT board=apollo-h743 git=9508c78 build=2026-08-28T07:16:24Z rtos=FreeRTOS
```

`flashgate verify` proves the sha on the wire matches the repo HEAD. If the board
didn't boot — dead loop, HardFault, wrong build — the exit code says so, and the
upcoming Stop hook (M3) uses exactly that to block premature "done" claims.

## Install

```bash
pip install -e .          # inside this repo; needs Python 3.11+
```

Requires: STM32CubeProgrammer CLI (auto-discovered from STM32Cube bundles),
CMake + Ninja + arm-none-eabi-gcc (bundles work), an ST-Link, and the board's
USB-serial cable.

## Usage

```bash
flashgate doctor            # ST-Link / serial / toolchain sanity
flashgate build             # firmware build only
flashgate flash             # write + verify + start
flashgate console           # live serial monitor
flashgate verify            # THE gate: build → flash → banner → sha
echo $?                     # consume the verdict
```

## Exit codes (the contract)

| code | meaning |
|---|---|
| 0 | verified — board booted the firmware built from HEAD |
| 1 | build failed |
| 2 | flash failed |
| 3 | no boot banner within timeout (dead loop? HardFault?) |
| 4 | boot error string seen on serial |
| 5 | banner sha ≠ repo HEAD |
| 6 | environment error (no ST-Link / serial / tools) |

## Board profiles

One yaml per board under `boards/`. Everything the gate needs is declarative —
build command, artifact, flash address, USB VID/PID of the serial bridge, banner
regex, timeout, error patterns. First profile: `boards/apollo-h743.yaml`
(ALIENTEK Apollo STM32H743, console on USART1 via onboard CH340 — note the RGB
LCD is hardware-mutually-exclusive with the console on this board, PA10 conflict).

## Roadmap

- **M1** (this release): build → flash → banner → sha loop ✔
- **M2**: serial probe protocol (`led0?` → state + real CCR readback) for
  claim-proportional functional verification
- **M3**: Claude Code Stop hook — unverified `.c/.h` changes block "done"
- **M4**: MCP server (serial stream + flash + board state for any agent)

## License

MIT
