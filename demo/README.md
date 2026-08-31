# Demos

Six real-hardware recordings. Five are in this directory; the sixth is
too large for git and ships as a release asset.

| # | File | What you see |
|---|---|---|
| 0 | [0-doctor.gif](0-doctor.gif) | prerequisite check — ST-Link, console, toolchain |
| 1 | [1-verify-green.gif](1-verify-green.gif) | one command: build → flash → boot banner → probes, exit 0 |
| 2 | [2-boot-timeout.gif](2-boot-timeout.gif) | dead-loop firmware compiles and flashes, board stays silent, exit 3 |
| 3 | [3-probe-fail.gif](3-probe-fail.gif) | silently-broken setter caught by the readback, exit 7 |
| 4 | [4-stop-hook.gif](4-stop-hook.gif) | Stop hook blocks twice, releases with a warning, then allows after a green verify |
| 5 | [5-agent-blocked.gif](https://github.com/Lion-1209/flashgate/releases/download/v0.3.0/5-agent-blocked.gif) | **the main one** — a full Claude Code session: the agent edits firmware, gets blocked by the board's testimony, diagnoses the firmware↔profile contract mismatch, fixes both sides, and is only allowed to finish after a passing hardware verify (24 MB, full quality, release asset) |

The inline one worth embedding anywhere:

![probe catches a silent no-op](3-probe-fail.gif)
