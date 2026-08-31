# flashgate

**The agent can't claim the firmware works — until the board says so.**
[中文说明](#中文说明) · **[使用说明 / User Guide](docs/GUIDE.md)**（中文，从接线到接 agent 的完整上手）

flashgate is a hardware-in-the-loop verification gate for coding agents
(Claude Code, Codex, coderio, …). It closes the loop every coding agent is
missing on firmware work:

```
build → flash over ST-Link → board boots → evidence → exit code
```

Boot evidence comes over **either** channel, whichever your bench has:

- **UART banner** — the firmware prints its identity over the console
- **SWD signature** — the firmware publishes a 64-byte identity (magic +
  git sha + build time + CRC32) at a fixed RAM address; the host reads it
  through the **ST-Link alone**. No serial cable needed.

Both carry the **git sha + build time** (plus `-dirty` when the tree
differs from HEAD, so the proof covers exactly what you're about to flash):

```
FLASHGATE-BOOT board=apollo-h743 git=2c58bd3 build=2026-08-28T07:26:35Z rtos=FreeRTOS
```

`flashgate verify` then drives the **actual feature** over the console and
asserts on hardware register readbacks — and a Stop hook turns the verdict
into law: unverified firmware changes block the agent's "done".

## The three layers (all validated on real hardware)

| Layer | Proof | Since |
|---|---|---|
| Boot gate | the board prints its identity; sha must match the tree | M1 |
| Probe gate | send real commands, assert on TIM3 CCR register readback | M2 |
| Stop hook | unverified `.c/.h` changes block the agent's "done" (exit 2) | M3 |
| MCP server | any agent can flash/probe/read the board over the standard protocol | M4 |

## Install

```bash
pip install -e .                 # Python 3.11+
pip install -e ".[mcp]"          # + MCP server (flashgate-mcp)
```

Requires: STM32CubeProgrammer CLI (auto-discovered from STM32Cube bundles),
CMake + Ninja + arm-none-eabi-gcc (bundles work), an ST-Link, and a
USB-TTL adapter wired to the board's console UART.

## Usage

```bash
flashgate doctor            # ST-Link / serial / toolchain sanity
flashgate build             # firmware build only
flashgate flash             # write + verify + start (auto-retry on USB flakes)
flashgate verify            # THE gate: build → flash → evidence → sha
flashgate verify --evidence swd   # SWD signature only — no serial cable
flashgate verify --all-probes    # + functional probes (claim-proportional)
flashgate probe [NAME...]   # probes against already-running firmware
flashgate console           # live serial monitor
echo $?                     # consume the verdict
```

## Exit codes (the contract)

| code | meaning |
|---|---|
| 0 | verified — board booted the firmware built from this tree |
| 1 | build failed |
| 2 | flash failed |
| 3 | no boot banner within timeout (dead loop? HardFault?) |
| 4 | boot error string seen on serial |
| 5 | banner sha ≠ repo HEAD |
| 6 | environment error (no ST-Link / serial / tools) |
| 7 | functional probe failed |

## The Stop hook: the gate becomes a law

`hooks/flashgate_stop.py` is a Claude Code **Stop hook** (the contract works
in any compatible harness, coderio included). When the agent tries to end
its turn after touching watched firmware files (`gate.watch` in the board
profile), the hook:

1. Fingerprints the firmware tree (HEAD + full diff + untracked list)
2. **Instantly allows** if this exact tree state already passed hardware
   verify (0.7 s cached path)
3. Otherwise runs `flashgate verify --all-probes` on the real board
4. **Blocks (exit 2)** on failure, feeding the agent the board's testimony
   and the exact command to run
5. Escalation (coderio VerifyGate semantics): the same broken tree is
   blocked at most twice, then released with a loud warning — never wedges
   your session, never silently gives up

Real transcript, sabotaged firmware (`led_set_state` silently does nothing —
compiles clean, board boots, only the readback catches it):

```
[flashgate] watched firmware changed (1 file(s)) — running hardware verify...
[flashgate] BLOCKED (attempt 1/2): firmware changes are not verified on hardware (verify rc=7).
[flashgate] Run `flashgate --board boards/apollo-h743.yaml verify --all-probes`,
            fix the firmware until it exits 0, then finish.
[flashgate] last verify output:
    step 1: led-demo> led0 breath
    board: OK led0 state=OFF          ← the readback exposes the silent no-op
```

Install into your firmware project's `.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "python /path/to/flashgate/hooks/flashgate_stop.py --board /path/to/boards/apollo-h743.yaml",
        "timeout": 600
      }]
    }]
  }
}
```

## MCP server (M4)

```json
{ "mcpServers": { "flashgate": {
    "command": "flashgate-mcp",
    "args": ["--board", "/path/to/boards/apollo-h743.yaml"] } } }
```

Tools: `board_info`, `doctor`, `build`, `flash`, `verify`, `probe`,
`console_send`, `console_read` — any MCP-capable agent can now flash the
board, run probes, and read the console directly. Supports mcp 1.x and 2.x.

```
tools/call console_send {"line": "ping"}
→ OK pong git=c65c453-dirty build=2026-08-28T08:06:26Z
```

## Demos (real hardware, real agent)

| Scene | What you see |
|---|---|
| [0 — doctor](demo/0-doctor.gif) | hardware prerequisites check, all green |
| [1 — verify green path](demo/1-verify-green.gif) | one command: build → flash → banner → probes, exit 0 |
| [2 — boot timeout](demo/2-boot-timeout.gif) | dead-loop firmware compiles & flashes, board stays silent 15s, exit 3 |
| [3 — probe catches silent no-op](demo/3-probe-fail.gif) | `led_set_state` returns early; readback says `state=OFF`, exit 7 |
| [4 — Stop hook law](demo/4-stop-hook.gif) | blocked ×2, released with warning, then verified-green allowed |
| [5 — real Claude Code agent blocked](https://github.com/Lion-1209/flashgate/releases/download/v0.2.0/5-agent-blocked.gif) | full interactive session: agent edits firmware, gets blocked, discovers the firmware↔profile contract, fixes both sides, verifies on hardware |

![probe catches a silent no-op](demo/3-probe-fail.gif)

## Board profiles

One yaml per board under `boards/`. Everything the gate needs is
declarative — build command, artifact, flash address, console adapter
hints, banner regex, probes, watch globs. First profile:
`boards/apollo-h743.yaml` (ALIENTEK Apollo STM32H743 core board; console on
the USART1 header via any USB-TTL adapter) — **complete buildable example
firmware included under `examples/apollo-h743/`**, Stop hook pre-wired
with repo-relative paths. Note: on this board the RGB LCD
is hardware-mutually-exclusive with the console (LTDC_B1 shares PA10 with
USART1_RX).

The console-side adapter is a property of your bench, not the board: port
resolution is explicit `serial.port` / env `FLASHGATE_SERIAL_PORT` >
VID/PID hint > sole serial port, and the banner regex is the final identity
proof regardless of which adapter answered.

## Roadmap

- **M1**: build → flash → banner → sha loop ✔
- **M2**: serial probe protocol (`led0?` → state + real CCR readback) ✔
- **M3**: Claude Code Stop hook — unverified changes block "done" ✔
- **M4**: MCP server ✔ · demo GIFs ✔ · SWD-signature evidence channel
  (verify without a serial cable) ✔

## License

MIT
---

# 中文说明

**agent 不能说固件能跑——除非板子亲口说。**

📖 **完整使用说明见 [docs/GUIDE.md](docs/GUIDE.md)**：硬件接线、安装、五分钟上手、
自定义板卡档案、探针编写、Stop hook / MCP 安装、排坑表。

flashgate 是给 coding agent（Claude Code / Codex / coderio……）用的**硬件在环验证门**，补上所有 coding agent 在固件开发里都缺的那一环：编译通过不等于能用，agent 说"完成"不算完成，**板子自己说了才算**。

## 工作原理

```
编译 → ST-Link 烧录 → 板子启动 → 串口打出身份横幅 → 退出码
```

- **启动门（M1）**：固件开机第一句通过串口喊出自己的 git sha 和构建时间（工作区有未提交改动会带 `-dirty` 后缀），工具核对"板上跑的确实是当前这份代码"
- **探针门（M2）**：通过串口真刀真枪用功能——下发 `led0 breath`，再读回**定时器 CCR 硬件寄存器当前值**，断言状态与占空比。寄存器不会撒谎
- **执法门（M3）**：Claude Code Stop hook——agent 改了 `.c/.h` 想结束会话？没过硬件验证就拦截（exit 2），把板子的证词喂回给 agent；同一棵坏树最多拦两次，之后放行但大声警告（coderio 四道门的逐级拦截语义，永不卡死会话、永不静默放水）
- **MCP server（M4）**：任何支持 MCP 的 agent 都能直接烧录、探针、读串口

## 安装与使用

```bash
pip install -e ".[mcp]"      # Python 3.11+，含 MCP server

flashgate doctor             # 体检：ST-Link / 串口 / 工具链
flashgate verify --all-probes  # 完整闭环：编译→烧录→横幅→sha→功能探针
echo $?                      # 0 = 板子亲自证明能跑；非 0 = 不能跑，附原因
```

退出码契约：`0` 验证通过 / `1` 构建失败 / `2` 烧录失败 / `3` 无横幅（死循环？HardFault？）/ `4` 启动报错 / `5` sha 不一致 / `6` 环境问题 / `7` 探针失败。

## 板卡档案

一块板一个 yaml（`boards/apollo-h743.yaml`），构建命令、烧录地址、串口适配提示、横幅正则、探针、监控通配符全部声明式——换板子只换档案。串口转接模块（CH340/CP210x/FT232…）是工作台属性不是板子属性，端口解析支持显式指定 / 芯片提示匹配 / 唯一串口兜底三级。

## 已验证的真实拦截案例

`led_set_state` 被塞入静默 `return`——编译通过、板子正常启动、ping 都通（最阴险的一类 bug），探针第一步就抓住：

```
电脑 → "led0 breath"
板子 → "OK led0 state=OFF"     ← 读回值（非回声）暴露设置未生效
exit 7，Stop hook 拦截 agent 的"完成"声明
```

## 路线图

M1 启动门 ✔ · M2 探针门 ✔ · M3 执法 Stop hook ✔ · M4 MCP server ✔ ·
SWD 签名副通道（免串口线验证）✔

## 双证据通道

串口线不在也没关系：固件在 DTCM 固定地址发布 64 字节签名（magic +
git sha + 构建时间 + CRC32），flashgate 通过 ST-Link 直接读物理 RAM 验证
（`verify --evidence swd`）；烧录后先擦除旧签名再启动——RAM 不随复位清零，
陈旧签名会说谎。串口在场时自动走 uart 通道并附带功能探针（`auto` 模式）。

## 演示视频（全部真机实录）

六个场景 GIF 见 [demo/](demo/) 目录（doctor 体检 / 绿路径 / 启动超时拦截 /
探针抓静默失效 / Stop hook 执法），以及压轴的
[真 Claude Code agent 被门拦下的完整会话](https://github.com/Lion-1209/flashgate/releases/download/v0.2.0/5-agent-blocked.gif)——
agent 改固件想收工，被板子证词拦回，自己分析出固件与板卡档案的契约关系，
两侧改齐、真机验证通过后才被放行。

MIT License
