# flashgate

flashgate answers one specific question: the firmware you just built — does
it actually run on the board?

It automates the whole chain: compile, flash over ST-Link, wait for the
board to report its own identity, then drive the actual feature over the
console and assert on hardware register readbacks. Any broken step ends
with a non-zero exit code and a reason.

```
build → flash over ST-Link → board boots → evidence → probes → exit code
```

A remote agent driving the bench over the network (Wi-Fi laptop → wired
bench, OpenOCD backend) — real output:

```
$ flashgate --board apollo-openocd.yaml verify --all-probes
[verify] apollo-h743: build -> flash -> boot banner -> probes
[build] OK in 1.3s, 0 warning(s)
[flash] Apollo.bin @ 0x08000000 via openocd:port=SWD
[flash] OK (written, verified, started)
FLASHGATE-BOOT board=apollo-h743 git=e87efd8-dirty build=2026-09-16T02:48:14Z rtos=FreeRTOS
[probe] led-demo: LED state-machine set/readback + PWM CCR sanity
    step 1: led-demo> led0 breath
    board: OK led0 state=BREATH
    step 2: led-demo> led0?
    board: OK led0 state=BREATH ccr=691          <- hardware register readback
    ...
[probe] led-demo: PASS (5 steps)
[record] 20260916T024814701-0-035e45ea.json  (succeeded exit=0 [6/6 checks])
```

[中文说明](#中文说明) | [使用说明（推荐先读这份）](docs/GUIDE.md)

## How the board testifies

Two evidence channels, picked per bench (`evidence.mode`: uart / swd / auto):

The firmware prints its identity over the console UART, and also publishes
a 64-byte signature (magic + git sha + build time + CRC32) at a fixed RAM
address that the host reads through the ST-Link alone — no serial cable
needed for boot verification:

```
FLASHGATE-BOOT board=apollo-h743 git=2c58bd3 build=2026-08-28T07:26:35Z rtos=FreeRTOS
```

Both carry the git sha plus `-dirty` when the tree differs from HEAD, so a
passing verify proves the board is running a build of your current HEAD
(`-dirty` marks uncommitted changes; it does not fingerprint their
content — the Stop hook's tree fingerprint covers that side: tracked diffs
in full, untracked files and the board profile by path + size + first
4 MiB, plus the flashgate version itself, so tightening probe
expectations or upgrading the tool invalidates a cached PASS. Files
ignored by git and nested git repositories are outside the gate). Functional probes then send real commands and assert on the answers,
including register readbacks (TIM3 CCR), not firmware self-reports:

```yaml
- send: "led0?"
  expect: "OK led0 state={state} ccr={ccr:d}"   # mirror of the firmware's printf
  assert: "state == BREATH and ccr <= 1000"
```

## Quick start

```bash
pip install -e .                 # Python 3.11+
pip install -e ".[mcp]"          # optional MCP server
pip install -e ".[bench]"        # optional remote bench (device-connect)

flashgate doctor                 # ST-Link / serial / toolchain sanity
flashgate verify --all-probes    # build → flash → evidence → sha → probes
echo $?                          # 0 = the board confirms it works
```

Requires an ST-Link; a USB-TTL adapter on the console UART adds probes.
The repo ships a complete buildable example for the ALIENTEK Apollo
STM32H743 (`examples/apollo-h743/`) with the Stop hook pre-wired, so a
fresh clone verifies out of the box once wired up.

## Exit codes

| code | meaning |
|---|---|
| 0 | verified |
| 1 | build failed |
| 2 | flash failed |
| 3 | board stayed silent (no banner / no signature within timeout) |
| 4 | error string seen on serial (HardFault, assertion) |
| 5 | on-board identity ≠ repo state (git sha or board name) — or the identity evidence channel could not be reset: a failed pre-start signature wipe, or a readback that ANSWERED wrongly — nonzero, or a truncated rc-0 dump (the wipe is read back and verified; a backend silently lying about it is caught) — fails closed (exit 5, start withheld); a readback that cannot RUN at all is an environment failure (exit 6). A surviving old-boot signature must never count as a pass |
| 6 | environment error (no ST-Link / serial / tools) — including probes explicitly required via `--probe`/`--all-probes` but the console UART is unavailable: a check that cannot run never counts as a pass |
| 7 | functional probe failed |

## Contributing

Board profiles, probes, fixes and docs are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md) for the setup and contribution terms.
For a release gate that does not depend on the Stop hook (the board signs
off in CI before a tag publishes), see the official GitHub Actions
recipes in [docs/ci-recipes.md](docs/ci-recipes.md).

## The Stop hook

`hooks/flashgate_stop.py` is a Claude Code Stop hook (any harness
implementing the same hook contract works). When the agent tries to finish after
touching watched firmware files, the hook fingerprints the tree (plus the
board profile — editing verification semantics must not reuse a cached
green) and allows instantly if that exact state already passed hardware
verify (~0.7 s cached). Otherwise it runs the full verify on the real
board and blocks the stop on failure, feeding the agent the board's
testimony:

```
[flashgate] BLOCKED (attempt 1/2): firmware changes are not verified on hardware (verify rc=7).
[flashgate] last verify output:
    step 1: led-demo> led0 breath
    board: OK led0 state=OFF          ← readback exposes the silent no-op
```

The same broken tree is blocked at most twice, then released with a loud
warning — the session can never wedge, and a failure is never silently
swallowed.

One verify at a time per bench: concurrent verifies (a user-level and a
session-level Stop hook firing on the same stop, or a manual run racing
the hook) serialize on an OS-owned lock file under the firmware's
`.flashgate/` instead of fighting over the console serial port — the
second one waits (default 300 s, `FLASHGATE_VERIFY_LOCK_WAIT`), and a
timed-out wait is a truthful exit 6 ("bench busy"), never a misleading
serial error. Register the hook at ONE level only: in the common case
stacking now just costs a redundant second verify; on a very slow bench
it can still exhaust the hook's own subprocess timeout, so it is a
crutch, not a supported setup.

## Verification records

Every `verify` — passing or failing — leaves an evidence record at
`<firmware>/.flashgate/records/<ts>-<exit>-<fingerprint8>.json`: the
per-check verdicts (`build`/`flash`/`boot`/`identity`/`probe:*`, with
`skipped` meaning *never executed*, never a silent absence), the raw
evidence behind them (banner line, SWD signature fields, console tails,
failing probe transcripts), the artifact sha256, and the exact
tree+profile+tool-version identity the verdict applies to. The MCP
`verify` tool returns the same record inline as `data.record`. Format:
[docs/record-schema.md](docs/record-schema.md). Keep `.flashgate/` in your
firmware repo's `.gitignore` (the shipped example does), or every record
write churns the fingerprint. A record that cannot be written is a
warning, never a changed exit code — and a crashed run still leaves one.
The newest ~500 records are kept (best-effort pruning). `flashgate verify
--json` prints this run's record as pure-ASCII JSON on stdout (human logs
move to stderr) for scripting — safe on any console codepage.

## Remote bench (device-connect, Stage 2)

Expose this bench to any agent on the network over Arm's device-connect
protocol — four named RPCs over a `BenchDriver` core, nothing else (no
arbitrary shell, no path arguments; the board a bench serves is fixed by
its profile):

```bash
pip install "flashgate[bench]"          # optional extra -> device-connect-edge
DEVICE_CONNECT_ALLOW_INSECURE=true DEVICE_CONNECT_DISCOVERY_MODE=d2d   flashgate --board boards/apollo-h743.yaml bench-serve
```

Remote callers discover `device_type=flashgate-bench` and use
`describe_bench` / `start_verify` / `get_operation` / `cancel_operation`,
plus a `verify_completed` event per operation at its terminal state (an
operation drained during server shutdown does not emit one — polling
remains the authoritative contract).
`start_verify` returns an `op_id` immediately (single verify at a time —
a client retrying over a flaky network can never trigger a second
flash); poll `get_operation` to the terminal snapshot, which carries the
exit code and the run's full evidence record. Stop the server without
hunting PIDs: `flashgate --board <profile> bench-serve --stop` (it
drains the in-flight operation, then exits).

**The red line for every consumer**: the mesh wraps any normally-delivered
reply as `success: true` — including a busy bench answering
`{"error": "busy"}`. Judge verification ONLY by the operation payload
(`state` / `exit_code` / the record's checks), never by transport
success. Acceptance on the Apollo bench: the same firmware input
produces identical local and remote domain results (tree fingerprint
and per-check verdicts equal; `artifact_sha256` differs by design — it
identifies the BUILD, which embeds its timestamp, while the tree
fingerprint identifies the SOURCE).

**Deployment notes** (from the first real cross-host test, wired host
+ Wi-Fi laptop, 2026-09-15):

- Multicast discovery does not cross all wired↔wireless routers. If
  discovery finds nothing across machines but `ping` works, switch to
  unicast: on the bench host set `ZENOH_LISTEN=tcp/0.0.0.0:7447` before
  `bench-serve`; on the client set `MESSAGING_URLS=tcp/<host-ip>:7447`.
- Open UDP 7446 (multicast scouting) or TCP 7447 (unicast) inbound on
  the host's firewall; a Wi-Fi client set to "Public network" blocks the
  announcements it needs to receive.
- **One bench-serve per bench**: a second instance for the same
  firmware dir is refused at startup (a loopback lock socket, released
  automatically when the process dies). Before the lock existed, two
  same-named servers split-brained the mesh and raced each other for
  the board's serial port.

**Third-party licensing**: the `bench` extra depends on
[device-connect-edge](https://github.com/arm/device-connect)
(Apache-2.0) as a pip dependency only — flashgate stays MIT, and no
device-connect source is vendored into this repository (fixes go
upstream as PRs). Commercial distributions that bundle the dependency
must include its LICENSE and NOTICE files.

**Security posture, stated plainly**: D2D mode is zero-authentication —
anyone on the same LAN can discover the bench and `start_verify`, which
FLASHES THE BOARD. Descriptions and records also carry local paths.
Fine for a home bench; for anything shared, put the mesh behind
authenticated infrastructure (device-connect server mode) — Stage 4
work.

## Install as a Claude Code plugin

The repo doubles as a Claude Code plugin: the Stop hook, an MCP server,
a board-integration skill and /flashgate commands, all at once:

```
pip install git+https://github.com/Lion-1209/flashgate        # CLI + hook runtime
pip install "flashgate[mcp] @ git+https://github.com/Lion-1209/flashgate"  # + MCP
```

Then inside Claude Code:

```
/plugin marketplace add Lion-1209/flashgate
/plugin install flashgate@flashgate
```

The hook needs to know your board profile: set `FLASHGATE_BOARD` to your
board yaml (or a repo `boards/` default is used when present). Without a
profile the gate stays idle and says so.

## MCP server

```json
{ "mcpServers": { "flashgate": {
    "command": "flashgate-mcp",
    "args": ["--board", "/path/to/boards/apollo-h743.yaml"] } } }
```

board_info, doctor, build, flash, verify, probe, console_send,
console_read. Any MCP-capable agent can drive the board directly. Every tool returns a structured result envelope (status, stable code, summary, CLI exit_code, log) — never a bare log to parse. mcp 1.x
and 2.x supported.

## Demos

Real-hardware recordings, indexed in [demo/](demo/README.md): doctor,
green-path verify, boot-timeout catch, probe catching a silent no-op,
Stop-hook escalation — plus the full session of a real agent (Claude
Code harness; recorded with GLM-5.2 behind the Anthropic-compatible
endpoint) getting blocked, diagnosing the firmware↔profile contract,
fixing both sides, and passing on hardware ([24 MB GIF, release asset](https://github.com/Lion-1209/flashgate/releases/download/v0.3.0/5-agent-blocked.gif)).

## Debug backends (Phase 2)

The verify pipeline is backend-agnostic: `flash.adapter:` in the board
profile picks the probe tool — `cubeprogrammer` (default, the original
implementation), `openocd` (same ST-Link, no ST toolchain needed, and
the standard route to Linux/ARM64 bench hosts like a Raspberry Pi), or
`fake` (scripted, for tests). Validated on the same Apollo board:
swapping to `openocd` is a one-line profile change — verify stays green
with identical records, zero upper-layer edits, and runs slightly
faster. OpenOCD is discovered via PATH or `$OPENOCD_BIN`; the target
script maps from the MCU family (`STM32H7*` -> `stm32h7x`, override
with `flash.openocd_target`).

## Board profiles

One yaml per board (`boards/`): build command, artifact, flash address
(`flash.adapter:` picks the debug backend), serial adapter hints,
banner template, probes, watch globs. The console-side
USB adapter is a property of your bench, not the board — port resolution
goes explicit `serial.port` / `FLASHGATE_SERIAL_PORT`, then VID/PID hint,
then the sole serial port, with the banner match as the final identity
proof. See the [guide](docs/GUIDE.md#6-固件怎么对接-flashgate) for the
firmware-side integration recipe (three levels, with code) and the
full profile field reference.

## Status

Boot gate, probe gate, Stop hook, MCP server, SWD signature channel,
verification records (per-check evidence with artifact hashes), the
remote bench over device-connect (validated cross-host), and a second
debug backend (OpenOCD, validated on the same board) — all on real
hardware. Windows-first; Linux/macOS untested.

## License

MIT

---

# 中文说明

flashgate 回答一个很具体的问题：刚编译出来的固件，烧到板子上到底能
不能跑。它把整条链路自动化：编译、ST-Link 烧录、等板子报告身份、经
串口实际调用功能并断言，任何一步断了就以非零退出码结束。

完整的使用说明在 [docs/GUIDE.md](docs/GUIDE.md)：接线、安装、第一次
验证、给自己的板子写档案、探针写法、Stop hook 和 MCP 的配置、排错。

要点：

- 板子有两种方式自证：串口 banner，或者在固定 RAM 地址发布 64 字节
  签名（后者只靠 ST-Link 就能验证，不用串口线）
- 版本身份带 `-dirty` 语义，验证通过意味着板上跑的就是当前工作区
- 探针下真命令、断言硬件寄存器读回值；响应报的是实际状态不是回声，
  静默失效的设置第一步就会露馅
- 每次验证（无论成败）都留一份证据记录：逐项检查结论、板子的原话、
  产物哈希——事后可审计"当时验证的是什么、板子说了什么"
- `bench-serve` 可以把整个台架挂上局域网：远程 agent 用 device-connect
  协议发现它、请求真机验证、拿回带证据的结论（跨机实测通过）
- Stop hook 挂进 Claude Code：agent 改了固件没过真机验证就说"完成"，
  会被拦下并收到板子的失败证词；同一棵坏树最多拦两次，之后放行但
  打警告，会话不会被卡死
- 仓库带完整的示例固件（examples/apollo-h743），接好线 clone 下来
  就能跑通第一次验证

```bash
pip install -e ".[mcp]"
flashgate doctor                  # 体检
flashgate verify --all-probes     # 完整验证
echo $?
```

六个真机演示 GIF 在 [demo/](demo/)，包括一段完整的 Claude Code 会话：
agent 改固件、被拦、自己定位到固件与板卡档案的契约不一致、两侧改齐、
真机通过后放行。

MIT License
