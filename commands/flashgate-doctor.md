---
description: Check hardware prerequisites (ST-Link, console serial, toolchain) and export a sendable report
---

Check whether flashgate can see everything it needs on this machine:

```bash
flashgate doctor ${FLASHGATE_BOARD:+--board "$FLASHGATE_BOARD"}
```

Explain any red line to the user: missing ST-Link (USB/power/driver),
unresolved console port (adapter unplugged, or vid/pid hint in the board
profile doesn't match — set `serial.port` or `$FLASHGATE_SERIAL_PORT`),
missing toolchain (STM32Cube bundles or PATH). The `on-board` line, when
present, is the firmware identity read live from RAM over the debug
probe — it tells the user which build the board is running right now.

When the user needs to hand the checkup to someone else (vendor support, a
colleague), export it instead of describing it line by line:

```bash
flashgate doctor --export checkup.md       # markdown page (JSON if .json)
flashgate doctor --export checkup.md --redact   # scrub local paths/host/user
```

`--redact` replaces the workstation's paths, hostname and username with
`<PATH>` / `<HOME>` / `<HOST>` / `<USER>` placeholders, keeping only file
basenames — support still sees which file, not the directory layout.
Paths with spaces (`C:\Program Files\...`) and UNC paths
(`\\server\share\...`) keep only the basename too. An export that cannot
be written (missing directory, permissions) exits 6 with the reason
rather than leaving the user believing a report exists. The checkup
covers only the listed checks; it is not a functional verification of the
firmware.

Write the export OUTSIDE the firmware repo (or into its already-ignored
`.flashgate/` dir): an untracked report inside a watched tree changes the
tree fingerprint and invalidates the Stop hook's cached PASS.
