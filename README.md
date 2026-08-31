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
passing verify proves the board is running exactly the code you're looking
at. Functional probes then send real commands and assert on the answers,
including register readbacks (TIM3 CCR), not firmware self-reports.

## Quick start

```bash
pip install -e .                 # Python 3.11+
pip install -e ".[mcp]"          # optional MCP server

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
| 5 | on-board identity ≠ repo state |
| 6 | environment error (no ST-Link / serial / tools) |
| 7 | functional probe failed |

## The Stop hook

`hooks/flashgate_stop.py` is a Claude Code Stop hook (any harness
implementing the same hook contract works). When the agent tries to finish after
touching watched firmware files, the hook fingerprints the tree and allows
instantly if that exact state already passed hardware verify (~0.7 s
cached). Otherwise it runs the full verify on the real board and blocks
the stop on failure, feeding the agent the board's testimony:

```
[flashgate] BLOCKED (attempt 1/2): firmware changes are not verified on hardware (verify rc=7).
[flashgate] last verify output:
    step 1: led-demo> led0 breath
    board: OK led0 state=OFF          ← readback exposes the silent no-op
```

The same broken tree is blocked at most twice, then released with a loud
warning — the session can never wedge, and a failure is never silently
swallowed.

## MCP server

```json
{ "mcpServers": { "flashgate": {
    "command": "flashgate-mcp",
    "args": ["--board", "/path/to/boards/apollo-h743.yaml"] } } }
```

board_info, doctor, build, flash, verify, probe, console_send,
console_read. Any MCP-capable agent can drive the board directly. mcp 1.x
and 2.x supported.

## Demos

Real-hardware recordings, indexed in [demo/](demo/README.md): doctor,
green-path verify, boot-timeout catch, probe catching a silent no-op,
Stop-hook escalation — plus the full session of a real Claude Code agent
getting blocked, diagnosing the firmware↔profile contract, fixing both
sides, and passing on hardware ([24 MB GIF, release asset](https://github.com/Lion-1209/flashgate/releases/download/v0.3.0/5-agent-blocked.gif)).

## Board profiles

One yaml per board (`boards/`): build command, artifact, flash address,
serial adapter hints, banner regex, probes, watch globs. The console-side
USB adapter is a property of your bench, not the board — port resolution
goes explicit `serial.port` / `FLASHGATE_SERIAL_PORT`, then VID/PID hint,
then the sole serial port, with the banner regex as the final identity
proof. See the [guide](docs/GUIDE.md#6-固件怎么对接-flashgate) for the
firmware-side integration recipe (three levels, with code) and the
full profile field reference.

## Status

Boot gate, probe gate, Stop hook, MCP server, SWD signature channel — all
implemented and validated on real hardware. Windows-first; Linux/macOS
untested.

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
