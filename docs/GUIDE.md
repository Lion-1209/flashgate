# flashgate 使用说明

> agent 不能说固件能跑——除非板子亲口说。
> 本指南带你从零接好硬件、装好工具，跑通第一次验证，然后把它接到你自己的板子和 agent 上。

- [1. 它解决什么问题](#1-它解决什么问题)
- [2. 硬件准备与接线](#2-硬件准备与接线)
- [3. 软件安装](#3-软件安装)
- [4. 五分钟跑通第一次验证](#4-五分钟跑通第一次验证)
- [5. 核心概念](#5-核心概念)
- [6. 给你自己的板子写档案](#6-给你自己的板子写档案)
- [7. 功能探针](#7-功能探针)
- [8. Stop hook：让执法自动化](#8-stop-hook让执法自动化)
- [9. MCP server](#9-mcp-server)
- [10. 常见问题排查](#10-常见问题排查)
- [11. 已知限制](#11-已知限制)

---

## 1. 它解决什么问题

AI agent（或任何人）写完固件说"完成了，能用"——你信吗？编译通过只说明语法对，
烧到芯片上能不能启动、功能是否真的工作，agent 永远无法自证。

flashgate 把"验证"这个动作从 agent 的嘴转移到物理世界：

```
编译 → ST-Link 烧录 → 板子启动 → 板子自证身份 → 功能探针实测 → 退出码
```

退出码 `0` 意味着**板子本身**确认了固件启动、功能可用；非 0 给出具体失败原因。
Stop hook 可以把这个结论变成强制法律：agent 没过硬件验证就不许说"完成"。

## 2. 硬件准备与接线

| 硬件 | 作用 | 必须 |
|---|---|---|
| 目标板（本指南以阿波罗 STM32H743 核心板为例） | 被验证对象 | ✅ |
| ST-Link V2（含克隆版） | 烧录 + SWD 证据通道 | ✅ |
| USB 转 TTL 模块（CH340/CP2102/FT232 均可） | 串口证据通道 + 功能探针 | 推荐 |
| 杜邦线若干 | 接线 | — |

**接线**（以阿波罗 H743 核心板为例）：

```
ST-Link          核心板
  SWDIO ────────── PA13 (SWDIO)
  SWCLK ────────── PA14 (SWCLK)
  GND   ────────── GND
  3.3V  ────────── 3.3V（板子独立供电可不接）

USB-TTL 模块      核心板
  RXD  ─────────── PA9  (USART1_TX)
  TXD  ─────────── PA10 (USART1_RX)
  GND  ─────────── GND
```

> ⚠️ 阿波罗板注意：H743 的 RGB 液晶（LTDC_B1）与串口（USART1_RX）共用 PA10，
> 硬件互斥——用串口验证时不能同时点 RGB 屏。这是板级/硅片限制，不是软件选择。

没有串口线也能用：`verify --evidence swd` 只依赖 ST-Link（见[第 5 节](#5-核心概念)），
只是功能探针（第 7 节）需要串口。

## 3. 软件安装

**主机要求**：Windows 10/11 + Python 3.11+（当前以 Windows 优先，工具链自动发现
基于 STM32Cube bundles；Linux/macOS 未测试）。

```powershell
git clone https://github.com/Lion-1209/flashgate
cd flashgate
pip install -e .                # 基础 CLI（doctor/verify/flash/probe/console）
pip install -e ".[mcp]"         # 可选：MCP server
```

**工具链**（三选一，按顺序自动发现）：

1. **STM32Cube CLT / VSCode STM32 扩展**（推荐，零配置）——flashgate 自动从
   `%LOCALAPPDATA%\stm32cube\bundles` 发现 CubeProgrammer / CMake / Ninja /
   arm-none-eabi-gcc 的最新版本
2. **标准安装**——独立安装 STM32CubeProgrammer + CMake + Ninja + GCC ARM，
   确保在 PATH 里
3. **固件工程自带**——若固件仓库有自己的构建脚本，在板卡档案里直接写命令即可

**被验证固件的要求**（详见[第 6 节](#6-给你自己的板子写档案)）：
- 可用命令行构建出 `.bin`
- 固件启动后发布"证据"：串口打一行 banner（uart 通道），或往固定 RAM 地址
  写签名（swd 通道，参考 apollo 仓库的 `BSP/Src/bsp_signature.c`）

## 4. 五分钟跑通第一次验证

以自带示例（阿波罗 H743 + apollo 固件）为例：

```powershell
# ① 体检：ST-Link 在线？串口在位？工具链齐全？
flashgate doctor

# ② 完整验证：编译 → 烧录 → 等板子自证 → 校验身份
flashgate verify --all-probes
echo "exit=$LASTEXITCODE"        # Git Bash 用 $?；cmd 用 %errorlevel%

# ③ 想亲眼看板子说什么？
flashgate console                # 实时串口监视，Ctrl+C 退出
```

`verify` 成功时你会看到：

```
FLASHGATE-BOOT board=apollo-h743 git=9508c78 build=2026-08-31T08:00:00Z rtos=FreeRTOS
[probe] led-demo: PASS (5 steps)
[verify] PASS — the board itself confirms the firmware booted (git=9508c78)
```

**故意试试它会拦什么**：在固件 banner 打印前塞个 `while(1){}`（编译照样过），
再跑 `verify`——板子沉默 15 秒后你会得到 `exit=3`。这就是产品的全部意义。

## 5. 核心概念

### 证据通道（evidence channel）

板子向主机自证存活的通路，两种可选、可自动切换：

| 通道 | 原理 | 依赖 | 附带能力 |
|---|---|---|---|
| `uart` | 固件开机经串口打一行 banner（含 git sha + 构建时间） | USB-TTL 线 | 可跑功能探针 |
| `swd` | 固件往**固定 RAM 地址**写 64 字节签名（magic + sha + 时间 + CRC32），主机经 ST-Link 直读物理 RAM | 仅 ST-Link | 无（探针需串口） |
| `auto`（默认） | 串口能解析就用 uart，否则落回 swd | — | 视情况 |

```powershell
flashgate verify --evidence swd     # 强制免串口线验证
```

> swd 通道的两个坑（都已处理，此处供理解）：
> ① **RAM 不随复位清零**——上一版固件的签名会冒充新版。flashgate 在烧录与
> 启动之间先擦掉旧 magic，只有真正的新启动才能重新发布。
> ② 签名必须放在**调试口可见且不被缓存吞掉**的内存——示例固件放在 DTCM 尾部
> （核心本地、架构上永不缓存）。别放 AXI SRAM，除非你确定解决了 D-cache 可见性。

### 身份语义（git sha + `-dirty`）

banner / 签名里的 git sha 是**构建时**从固件仓库读取的；工作区有未提交改动时
带 `-dirty` 后缀。verify 会把它与仓库当前状态比对——不一致 = 你验证的不是
这份代码 = `exit=5`。这保证了"板上跑的就是你眼前这份工作区"。

### 退出码契约

| 码 | 含义 |
|---|---|
| 0 | 验证通过——板子亲证 |
| 1 | 构建失败 |
| 2 | 烧录失败 |
| 3 | 板子没出声（无 banner / 无签名，超时） |
| 4 | 启动报错字符串（HardFault/断言） |
| 5 | 板上固件身份 ≠ 仓库当前状态 |
| 6 | 环境错误（无 ST-Link / 无串口 / 缺工具） |
| 7 | 功能探针失败 |

任何 harness / CI / hook 都只需要消费这一个数字。

## 6. 给你自己的板子写档案

一块板一个 yaml，放 `boards/`。所有字段：

```yaml
board: my-board              # 名字（banner/签名里的 board= 与之对应）
mcu: STM32F407VGTx           # 描述用
description: 一句话

firmware:
  dir: ../my-firmware        # 固件仓库路径（相对本 yaml）
  configure: cmake -B build  # 可选：build 目录不存在时先跑
  build: ninja -C build      # 构建命令（退出码即 L0 门）
  artifact: build/fw.bin     # 烧录产物

flash:
  connect: port=SWD          # CubeProgrammer 连接参数
  address: "0x08000000"      # Flash 基址

serial:                      # uart 证据通道 + 探针载体
  port: ""                   # 显式指定 COM 口；空 = 自动解析
  vid: 0x1A86                # 串口转接芯片提示（CH340）
  pids: [0x7523, 0x5523]
  baudrate: 115200
  banner_regex: 'FLASHGATE-BOOT board=(?P<board>\S+) git=(?P<git>\S+) build=(?P<build>\S+)'
  banner_timeout_s: 15
  error_patterns: ["HardFault", "Assertion"]

evidence:
  mode: auto                 # uart | swd | auto
  signature:
    address: "0x2001FF00"    # 固件签名所在固定地址
    size: 64

probes:                      # 见第 7 节
  my-check: ...

gate:
  watch: ["*.c", "*.h", "*.ld", "*.ioc", "CMakeLists.txt"]   # Stop hook 触发范围
```

**串口转接模块是"工作台属性"不是"板子属性"**：今天用 CH340、明天换 CP2102，
改 `vid/pids` 提示或直接写死 `port: COM3` 都行。端口解析顺序：显式 `port` /
环境变量 `FLASHGATE_SERIAL_PORT` > VID/PID 提示 > 机器上唯一串口兜底。
banner 正则是最终身份判据——选错口只会超时，不会误判通过。

**固件侧要做的两件事**（参考 apollo 仓库）：
1. banner：开机第一行 printf 一个固定格式、含 git sha 的字符串（sha 在构建时
   注入，参考 `cmake/firmware_identity.cmake` 的 build-time 生成方案）
2. （可选，swd 通道）签名：参考 `BSP/Src/bsp_signature.c` 与链接脚本的
   SIGRAM 保留段

## 7. 功能探针

探针 = **声称什么就验证什么**：主机经串口下真命令，断言板子的回答——包括
硬件寄存器读回值。

**固件侧协议**（行式，一命令一响应）：

```
主机 → "led0 breath"          固件 → "OK led0 state=BREATH"     ← 回读值，非回声
主机 → "led0?"                固件 → "OK led0 state=BREATH ccr=691"   ← TIM3 硬件寄存器实时值
```

关键设计：**响应回的是读回来的实际状态，不是请求的回声**——设置静默失效时
板子当场露馅（`exit=7`）。

**探针定义**（板卡档案 `probes:` 节）：

```yaml
probes:
  led-demo:
    description: LED 状态机 set/readback + PWM CCR 校验
    step_timeout_s: 3
    steps:
      - send: "led0 breath"
        expect: '^OK led0 state=BREATH$'
      - send: "led0?"
        expect: '^OK led0 state=(?P<state>\S+) ccr=(?P<ccr>\d+)$'
        assert: "state == BREATH and ccr <= 1000"
```

- `expect`：对**一行响应**做正则匹配；固件回 `ERR` 开头的行立即失败
- `assert`：对 expect 的**命名捕获组**求值，支持 `== != > < >= <=` 与 `and`
  连接（微型安全求值器，不碰 eval）
- 断言写**确定性的量**（状态、范围），别写瞬时值——呼吸中的 ccr 每次读都不同

**运行方式**：

```powershell
flashgate verify --all-probes       # 完整闭环含全部探针
flashgate verify --probe led-demo   # 指定探针（可重复）
flashgate probe                     # 对运行中的固件直接探（不重建不烧录）
```

## 8. Stop hook：让执法自动化

前面的 verify 需要人（或 agent 自觉）去跑。Stop hook 把它挂进 Claude Code 的
生命周期：**agent 改了 watched 文件想结束回合时，hook 自动跑真机验证，
不过就拦（exit 2），把板子的证词喂回给 agent。**

**升级语义**（继承 coderio 四道门）：同一棵坏树最多拦 2 次，第 3 次放行但
大声警告——永不卡死会话，永不静默放水。

**安装（项目级，随仓库分发）**——固件工程 `.claude/settings.json`：

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "python /path/to/flashgate/hooks/flashgate_stop.py --board /path/to/boards/my-board.yaml",
        "timeout": 600
      }]
    }]
  }
}
```

**安装（用户级，本机全局生效）**——同样内容放进 `~/.claude/settings.json`。

> ⚠️ 实测坑：**项目级 hooks 在交互式会话需要逐项目审批**，没弹窗/没批就静默
> 不加载（headless `claude -p` 不受此限）。自己机器推荐用户级；给 OSS 用户
> 用项目级并提醒审批。装好后会话里输入 `/hooks` 应能看到 Stop 下挂着
> flashgate 条目。

**指纹缓存**：树没变时（HEAD + diff + 未跟踪文件指纹一致）0.7 秒静默放行，
高频使用无感。coderio 等兼容 Claude Code hooks 契约的 harness 同样可挂。

## 9. MCP server

让任何支持 MCP 的 agent 直接驱动板子：

```powershell
pip install -e ".[mcp]"
```

`.mcp.json`（Claude Code 兼容格式）：

```json
{ "mcpServers": { "flashgate": {
    "command": "flashgate-mcp",
    "args": ["--board", "/path/to/boards/my-board.yaml"] } } }
```

8 个工具：`board_info` / `doctor` / `build` / `flash` / `verify` / `probe` /
`console_send` / `console_read`。兼容 mcp 1.x 与 2.x。

## 10. 常见问题排查

| 症状 | 原因与解法 |
|---|---|
| `console : UNRESOLVED` | 串口线没插 / 转接芯片提示不符。跑 `flashgate doctor`，多串口时设 `serial.port` 或环境变量 `FLASHGATE_SERIAL_PORT` |
| `could not open port 'COM3': 拒绝访问` | 串口助手占用——**关掉串口助手**再 verify |
| `ST-LINK error (DEV_USB_COMM_ERR)` | 克隆 ST-Link 的 USB 抖动。flashgate 已内置三连自动重试；持续失败就拔插 ST-Link USB |
| `'[cube-cmake] 不是内部或外部命令'` | VSCode STM32 扩展重新 configure 过工程，把它的工具名写进了 build.ninja。flashgate 会自动检测并自愈（用 bundle cmake 重 configure） |
| `exit=3` 但板子明明启动了 | uart：banner 格式与正则不符；swd：固件没发布签名 / 签名地址与档案不一致 |
| `exit=5` | 板上固件身份 ≠ 仓库状态——commit 后没重新构建，或构建后改了代码。重跑 verify（它会重新构建） |
| Stop hook 不触发 | ① 目录不对（必须在挂了 settings.json 的目录启动）② 项目级 hooks 未审批（`/hooks` 检查）③ 会话启动早于配置写入——重启会话 |
| 输出满屏 `[36m` 乱码 | 旧版 PowerShell 5.1 不渲染 ANSI。v0.2.0+ 已自动探测：新开窗口即正常，或设 `NO_COLOR=1` |
| 探针偶发失败 | 断言写了非确定量（呼吸中的瞬时 ccr）。改断言状态与范围 |

## 11. 已知限制

- Windows 优先（ST bundles 自动发现）；Linux/macOS 未测试
- 签名通道的固件侧参考实现针对 STM32H7（DTCM 方案）；移植到其他 MCU 时
  需要选一块"调试口可见且不被缓存"的内存
- swd 证据通道不含功能探针（探针需要串口的双向能力）
- Stop hook 的审批行为随 Claude Code 版本可能变化，以 `/hooks` 实测为准

---

*发现问题或想加你板子的档案？欢迎 issue / PR：https://github.com/Lion-1209/flashgate*
