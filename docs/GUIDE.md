# flashgate 使用说明

flashgate 是一个命令行工具，用来回答一个很具体的问题：刚编译出来的固件，
烧到板子上以后到底能不能跑。

它的工作方式是把整条链路自动化：编译、通过 ST-Link 烧录、等板子自己报告
"我启动了，我是某某版本"、再通过串口实际调用板子上的功能做几个断言。
任何一步出问题，命令以非零退出码结束，并告诉你死在哪一步。

这份说明写给第一次接触的人，从接线讲到把执法钩子挂进 Claude Code。
示例基于正点原子阿波罗 STM32H743 核心板，仓库里带了这份固件的完整源码
（`examples/apollo-h743/`），clone 下来就能编译烧录。

## 目录

1. [硬件准备](#1-硬件准备)
2. [安装](#2-安装)
3. [第一次验证](#3-第一次验证)
4. [两套证据通道](#4-两套证据通道)
5. [退出码](#5-退出码)
6. [固件怎么对接 flashgate](#6-固件怎么对接-flashgate)
7. [功能探针](#7-功能探针)
8. [Stop hook](#8-stop-hook)
9. [MCP server](#9-mcp-server)
10. [排错](#10-排错)
11. [已知限制](#11-已知限制)

## 1. 硬件准备

需要的东西：

- 一块能跑固件的板子（本说明用阿波罗 H743 核心板）
- 一个 ST-Link V2，正品或克隆都行，烧录必须
- 一个 USB 转 TTL 模块，CH340、CP2102、FT232 都可以。这个不是必须的，
  没有它也能做启动验证（见第 4 节的 swd 通道），但功能探针做不了
- 四五根杜邦线

接线。ST-Link 四根：

```
ST-Link SWDIO  -> 板子 PA13
ST-Link SWCLK  -> 板子 PA14
ST-Link GND    -> 板子 GND
ST-Link 3.3V   -> 板子 3.3V（板子自己供电的话这根可以不接）
```

USB 转 TTL 三根，接 USART1：

```
模块 RXD -> 板子 PA9  (USART1_TX)
模块 TXD -> 板子 PA10 (USART1_RX)
模块 GND -> 板子 GND
```

阿波罗这块板有个值得知道的事：H743 的 RGB 液晶接口（LTDC_B1）和
USART1_RX 在芯片内部共用 PA10，两者不能同时用。想在验证的同时点亮
RGB 屏，在这块板上做不到。这是引脚分配决定的，跟 flashgate 无关，
但你在别的板上做档案时也会遇到类似取舍，先想清楚串口和显示谁让路。

## 2. 安装

主机环境目前是 Windows 10/11 加 Python 3.11 以上。Linux 和 macOS 没测过，
工具链发现那部分逻辑要自己适配。

```powershell
git clone https://github.com/Lion-1209/flashgate
cd flashgate
pip install -e .
```

想用 MCP server 的话装可选依赖：

```powershell
pip install -e ".[mcp]"
```

工具链有三种来路，flashgate 按这个顺序找：

1. STM32Cube 的 bundles 目录（装过 STM32Cube CLT 或者 VSCode 的 STM32
   扩展就有），在 `%LOCALAPPDATA%\stm32cube\bundles` 下面。flashgate 会
   扫这里的 CubeProgrammer、CMake、Ninja、arm-none-eabi-gcc，自动选每个
   工具的最新版本。这是推荐路径，装好扩展就什么都不用配。
2. 自己装的独立工具链，在 PATH 里能找到也行。
3. 固件仓库自己带构建脚本。板卡档案里写的构建命令就是一条普通 shell
   命令，你写 `make`、写自家脚本都可以，flashgate 只看退出码。

被验证的固件要满足两个条件：能用命令行编译出 bin 文件；启动时会报告
自己的身份。第二个条件的具体做法在第 4 节和第 6 节讲。

## 3. 第一次验证

先跑体检：

```powershell
flashgate doctor
```

它会逐项检查，正常输出长这样（这是我在自己机器上跑的真实输出）：

```
flashgate doctor — apollo-h743 (STM32H743IIT6)
  firmware   : E:\...\examples\apollo-h743
  artifact   : E:\...\examples\apollo-h743\build\Debug\Apollo.bin
  programmer : C:\...\bundles\programmer\2.23.0\bin\STM32_Programmer_CLI.exe
  ST-Link    : ST-LINK SN  : 56FF6D067180545731431967
  console    : COM3 @ 115200  [VID/PID hint 1A86:7523 (USB-SERIAL CH340 (COM3))]
  cmake      : ...\cmake\4.2.3+st.1\bin\cmake.EXE
  ninja      : ...\ninja\1.13.2+st.1\bin\ninja.EXE
  arm-none-eabi-gcc : ...\gnu-tools-for-stm32\14.3.1+st.2\bin\arm-none-eabi-gcc.EXE
  HEAD sha   : 2b6488b
  all prerequisites OK
```

每一行的含义：firmware 和 artifact 是从板卡档案读的固件位置和编译产物；
programmer 是找到的烧录器程序；ST-Link 那行读的是探针序列号，能打出来
说明探针真的连上了；console 是解析出来的串口，方括号里说明它是怎么被
选中的（这里是因为 VID/PID 匹配到了 CH340）；后面三个是编译工具；HEAD
sha 是固件仓库当前版本，等会儿 verify 要拿它跟板子报告的版本对。

有一行红的就是环境没齐，照着提示修。都绿了就可以跑完整的：

```powershell
flashgate verify --all-probes
echo "exit=$LASTEXITCODE"
```

这条命令在做什么，以及每步花多久：

1. 编译。增量的话两秒左右，第一次从零配置大概三十秒。
2. 烧录。CubeProgrammer 写 flash 加校验，八秒上下。烧完自动复位运行。
3. 等板子说话。固件开机第一件事是往串口打一行 banner：

   ```
   FLASHGATE-BOOT board=apollo-h743 git=2b6488b build=2026-08-31T08:00:00Z rtos=FreeRTOS
   ```

   flashgate 在电脑这边听。十五秒没听到就算失败。
4. 核对身份。banner 里的 git 值要跟固件仓库当前状态一致，不一致说明
   你验证的不是手头这份代码。
5. 跑探针。往串口发命令，检查回答：

   ```
       step 1: led-demo> led0 breath
       board: OK led0 state=BREATH
       step 2: led-demo> led0?
       board: OK led0 state=BREATH ccr=691
   ```

   ccr=691 是 TIM3 比较寄存器当前的值，从硬件里读出来的，不是固件
   想说什么就说什么。

全部过了退出码是 0。想亲眼看板子说什么，用 `flashgate console` 开实时
监视，Ctrl+C 退出。

现在做个实验感受一下它拦什么。打开固件的 `Core/Src/main.c`，在
`bsp_console_boot_banner();` 之前插一行 `while (1) { }`，保存，再跑一遍
verify。编译照常通过，烧录照常成功，然后是十五秒的安静，最后：

```
[verify] TIMEOUT: board never printed the FLASHGATE-BOOT banner
exit=3
```

这就是这个工具存在的理由：那行死循环编译器发现不了，单测发不发现得了
看运气，但板子不会骗人。实验完把代码还原：

```powershell
git -C examples\apollo-h743 checkout -- Core/Src/main.c
```

## 4. 两套证据通道

"板子报告自己启动了"这件事，flashgate 支持两条通路，板卡档案里用
`evidence.mode` 选：`uart`、`swd`、`auto`（默认，串口能用就用串口，
否则落到 swd）。

uart 通道就是上面看到的 banner。固件开机往串口打一行固定格式的
字符串，里面带 git 版本和构建时间。优点是肉眼可见，拿串口助手就能
看；串口是双向的，所以功能探针也走这条路。缺点是多一根线。

swd 通道不需要串口线。固件启动后往一块固定地址的 RAM 写 64 字节
的签名结构体，flashgate 通过 ST-Link 直接读物理内存来验证。结构体
布局：

```
偏移   大小   内容
0x00   4      magic，固定 0xF1A5C0DE
0x04   2      布局版本，当前是 1
0x06   2      flags，bit0 表示串口初始化过
0x08   16     git 版本字符串（短 sha + 可选的 -dirty）
0x18   24     构建时间，ISO 8601
0x30   4      CRC32，覆盖 0x00 到 0x2F 这 48 字节（注意 magic 的最终值
              也参与 CRC）
```

用法：

```powershell
flashgate verify --evidence swd
```

这条通路有两个不明显的坑，都已经被处理了，但值得知道为什么：

一，RAM 不随复位清零。上一版固件留下的签名会原样躺在那儿，新固件
没启动的话它就冒充新版。flashgate 在烧录和启动之间会先用 ST-Link 把
magic 擦成零，只有真正跑起来的固件才能重新写上。

二，签名放哪块 RAM 有讲究。示例固件放在 DTCM 的最后 256 字节
（0x2001FF00，链接脚本里划了专门的 SIGRAM 段）。我们最初放在 AXI
SRAM 的尾部，结果遇到一件怪事：固件自己读得到签名，调试口永远读到
零，停核了读也是零。折腾一下午没找到根因（这颗芯片的 D-cache 在
没有任何代码开它的情况下是开着的），最后换到 DTCM 解决，DTCM 在
架构上就不经过缓存。如果你往别的芯片移植，选一块"调试口直读且
不被缓存"的内存，别重蹈这个。

两套通道携带的身份信息相同，包括 -dirty 语义：固件仓库有未提交
改动时，版本串后面会带 `-dirty` 后缀，构建时烙进固件。verify 拿它跟
仓库现状比对，对不上就报 5 号错误。这保证了一件事：验证通过时，板子
上跑的确实是你眼前这份工作区，不是哪个旧版本。

swd 通道的局限是没有功能探针，探针需要串口的双向能力。

## 5. 退出码

所有命令的结论都浓缩在退出码里，CI、脚本、Stop hook 都只需要消费
这一个数字：

| 码 | 含义 |
|---|---|
| 0 | 验证通过 |
| 1 | 编译失败 |
| 2 | 烧录失败 |
| 3 | 板子没出声（无 banner 或无签名，超时） |
| 4 | 串口输出里出现错误模式（HardFault、断言之类） |
| 5 | 板上固件的身份跟仓库对不上 |
| 6 | 环境问题（ST-Link 没连、串口找不到、工具链缺失） |
| 7 | 功能探针失败 |

## 6. 固件怎么对接 flashgate

flashgate 对固件的要求分三档，做完第一档就能用启动验证，后面的按需加：

| 档 | 固件要做的事 | 解锁的能力 |
|---|---|---|
| 一 | 开机往串口打一行 banner | uart 启动验证 |
| 二 | 往固定 RAM 地址写 64 字节签名 | swd 启动验证（免串口线） |
| 三 | 串口行协议（查询/设置命令） | 功能探针 |

三档都只是在固件里加代码，不改现有逻辑，从哪一档开始都行。

### 6.1 第一档：banner

在 main 里、外设初始化完成之后、进 RTOS 或主循环之前，printf 一行：

```c
printf("\r\nFLASHGATE-BOOT board=%s git=%s build=%s\r\n",
        "my-board", APP_GIT_SHA, APP_BUILD_ISO);
```

对这行字的要求只有三条：一行写完；开头和字段格式你自己定，但要用
同样的格式写 yaml 里的 banner_regex；git 字段来自构建时烙进的版本，
不要手写。串口怎么初始化的随便，printf 重定向、直接
HAL_UART_Transmit 一个字符串、写寄存器，都行。

版本宏的来源是整个对接里唯一有点讲究的部分：要在每次构建时重新
生成，而不是写死，也不能只在 configure 时生成一次。原因：agent 的
典型工作流是改代码、验证、再提交，版本如果在 configure 时烙一次，
commit 之后不重新 configure 就会过期。CMake 工程：

```cmake
# CMakeLists.txt：每次构建都重新生成版本头
add_custom_target(fw_version ALL
    COMMAND ${CMAKE_COMMAND}
            -DSOURCE_DIR=${CMAKE_SOURCE_DIR}
            -DOUT_FILE=${CMAKE_BINARY_DIR}/fw_version.h
            -P ${CMAKE_SOURCE_DIR}/cmake/gen_version.cmake
    BYPRODUCTS ${CMAKE_BINARY_DIR}/fw_version.h
)
add_dependencies(your_elf fw_version)
target_include_directories(your_elf PRIVATE ${CMAKE_BINARY_DIR})
```

```cmake
# cmake/gen_version.cmake
execute_process(COMMAND git rev-parse --short=7 HEAD
    WORKING_DIRECTORY ${SOURCE_DIR} OUTPUT_VARIABLE sha
    OUTPUT_STRIP_TRAILING_WHITESPACE RESULT_VARIABLE r)
if(NOT r EQUAL 0)
    set(sha unknown)
endif()
execute_process(COMMAND git status --porcelain
    WORKING_DIRECTORY ${SOURCE_DIR} OUTPUT_VARIABLE dirty
    OUTPUT_STRIP_TRAILING_WHITESPACE)
if(NOT dirty STREQUAL "")
    string(APPEND sha "-dirty")
endif()
string(TIMESTAMP iso "%Y-%m-%dT%H:%M:%SZ" UTC)
file(WRITE "${OUT_FILE}"
    "#define APP_GIT_SHA \"${sha}\"\n#define APP_BUILD_ISO \"${iso}\"\n")
```

Makefile 工程同理，两行 shell：

```make
fw_version.h:
	@echo "#define APP_GIT_SHA \"$$(git rev-parse --short=7 HEAD)\"" > $@
	@echo "#define APP_BUILD_ISO \"$$(date -u +%Y-%m-%dT%H:%M:%SZ)\"" >> $@
```

固件里 #include "fw_version.h"，配一个 __has_include 的兜底宏定义
会更稳。

这一档做完的验收：串口助手能看到那行字；写好 yaml 后
flashgate verify --evidence uart 退出码 0。

### 6.2 第二档：RAM 签名

给没有串口线的场景用。固件在启动早期往一个固定地址写这 64 字节：

```
偏移  大小  字段
0x00  4     magic，0xF1A5C0DE（注意：最终值参与 CRC）
0x04  2     布局版本 = 1
0x06  2     flags，bit0 = 串口已初始化，其余保留
0x08  16    git 版本字符串，NUL 结尾
0x18  24    构建时间，ISO 8601，NUL 结尾
0x30  4     CRC32，覆盖 0x00-0x2F 共 48 字节，算法同 zlib.crc32
0x34  12    保留，填零
```

参考实现，可移植 C：

```c
#include <stdint.h>
#include <string.h>

typedef struct {
    uint32_t magic;
    uint16_t version;
    uint16_t flags;
    char     git[16];
    char     build[24];
    uint32_t crc32;
    uint8_t  reserved[12];
} flashgate_sig_t;            /* 应为 64 字节，建议 _Static_assert 确认 */

static uint32_t sig_crc32(const uint8_t *p, uint32_t len)
{
    uint32_t crc = 0xFFFFFFFFu;
    for (uint32_t i = 0; i < len; i++) {
        crc ^= p[i];
        for (int b = 0; b < 8; b++)
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
    }
    return crc ^ 0xFFFFFFFFu;
}

/* 放进链接脚本划出的固定段，见下面"地址怎么挑" */
static volatile flashgate_sig_t g_flashgate_sig
    __attribute__((section(".flashgate_sig"), used));

void flashgate_publish(const char *git, const char *build, uint16_t flags)
{
    flashgate_sig_t s;
    memset(&s, 0, sizeof s);
    s.magic   = 0xF1A5C0DEu;
    s.version = 1;
    s.flags   = flags;
    strncpy(s.git,   git,   sizeof s.git   - 1);
    strncpy(s.build, build, sizeof s.build - 1);
    s.crc32 = sig_crc32((const uint8_t *)&s, 48);
    memcpy((void *)&g_flashgate_sig, &s, sizeof s);
    __asm volatile ("dsb" ::: "memory");
}
```

调用一行：flashgate_publish(APP_GIT_SHA, APP_BUILD_ISO, 1);
放在 banner 之后即可。

地址怎么挑，是这一档唯一需要动脑子的地方，四条规则：

一，必须是 RAM。掉电会丢没关系，复位后固件会重写；恰恰要的是
复位后内容还在，主机才能读到。

二，地址必须固定。普通全局变量会被链接器挪来挪去，要在链接脚本里
划一块专用段。做法是从某块 RAM 的尾部划，同时把该 RAM 的 LENGTH
缩小相应的字节：

```
MEMORY
{
    RAM (xrw)  : ORIGIN = 0x20000000, LENGTH = 128K - 0x100
    SIGRAM (rw): ORIGIN = 0x2001FF00, LENGTH = 0x100
}
SECTIONS
{
    .flashgate_sig (NOLOAD) :
    {
        KEEP(*(.flashgate_sig))
    } > SIGRAM
}
```

三，那块 RAM 必须不经缓存、调试口能直读。两个稳妥选择：核心本地
RAM（Cortex-M 的 DTCM 之类，架构上不经过缓存）；或者用 MPU 把这
256 字节配成强序非缓存。我们在一颗带 D-cache 的 M7 上踩过雷：放
AXI SRAM 时固件自己读得到、调试口读到的是零，停核读也一样，换
DTCM 解决。如果你的芯片没有这类内存，走 MPU 路线。

四，别和栈、堆、DMA 缓冲区重叠，规则二的做法天然避开。

验收：yaml 的 evidence.signature.address 填你挑的地址，
flashgate verify --evidence swd 退出码 0。

### 6.3 第三档：探针命令

想验证"改的那个功能真的能用"，固件在串口上实现一个行协议。
约定很少：

- 收一行命令，回一行响应，以 \r\n 结尾
- 成功的响应以 OK 开头，失败的以 ERR 开头
- 命令和字段名你自己定，yaml 里配对

协议骨架，轮询式，六十行上下：

```c
for (;;) {
    if (uart_line_ready(line, sizeof line)) {
        if (strcmp(line, "ping") == 0) {
            printf("OK pong git=%s\r\n", APP_GIT_SHA);
        } else if (strcmp(line, "led on") == 0) {
            led_set(1);
            printf("OK led state=%s\r\n", led_get() ? "on" : "off");
        } else if (strcmp(line, "led?") == 0) {
            printf("OK led state=%s duty=%lu\r\n",
                   led_get() ? "on" : "off",
                   (unsigned long)timer_ccr_read());
        } else {
            printf("ERR unknown-cmd\r\n");
        }
    }
}
```

RTOS 工程里丢一个低优先级任务跑这个循环；裸机就挂在主循环里。

两条设计经验，都来自实际踩坑：

响应要报读回来的实际状态，不要回声请求。上面 led on 的响应里
state 是再读一次硬件后的值。设置类命令静默失效是最难缠的一类
bug，回声式响应永远发现不了，回读式第一步就露馅。

查询类命令尽量报寄存器实际值。duty=%lu 那个字段是定时器 CCR
寄存器的当前读数。寄存器不会说谎，这是能给的最硬的证据；固件
自己"认为"的状态只能作参考。

### 6.4 主机侧：板卡档案

固件之外，写一份 yaml 描述这块板子，放 boards/。全部字段：

```yaml
board: my-board
mcu: STM32F407VGTx
description: 我的板子

firmware:
  dir: ../my-firmware        # 固件工程路径，相对这份 yaml
  configure: cmake -B build  # build 目录不存在时先跑这条
  build: ninja -C build      # 构建命令，看退出码
  artifact: build/fw.bin     # 烧录产物

flash:
  connect: port=SWD          # CubeProgrammer 的连接参数
  address: "0x08000000"      # flash 基址

serial:
  port: ""                   # 写死 COM 口；留空走自动解析
  vid: 0x1A86                # USB 转 TTL 芯片的 USB 提示
  pids: [0x7523, 0x5523]
  baudrate: 115200
  banner_regex: 'FLASHGATE-BOOT board=(?P<board>\S+) git=(?P<git>\S+) build=(?P<build>\S+)'
  banner_timeout_s: 15
  error_patterns: ["HardFault", "Assertion"]

evidence:
  mode: auto                 # uart / swd / auto
  signature:
    address: "0x2001FF00"    # 6.2 里挑的地址
    size: 64

probes:                      # 第 7 节
  ...

gate:
  watch: ["*.c", "*.h", "*.ld"]   # Stop hook 的触发范围
```

串口这块的思路是"板子只认 USART 引脚，USB 那头接什么是工作台的事"。
端口解析按三层来：显式的 port 或环境变量 FLASHGATE_SERIAL_PORT 优先；
没有就看 vid/pids 提示；再没有就数一下机器上有几个串口，只有一个就
用它。选错了口不会误判通过，banner 正则是最终判据，错口只会超时。

写好之后先 flashgate --board boards/my-board.yaml doctor，再 verify。

### 6.5 对接清单

最小对接（启动门）：

- [ ] printf 一行 banner
- [ ] 构建时生成版本头（git sha + 时间 + -dirty）
- [ ] 板卡档案：构建命令、产物、flash 地址、串口、banner 正则
- [ ] verify --evidence uart 退出码 0

进阶：

- [ ] 链接脚本划 SIGRAM，实现 flashgate_publish
- [ ] 档案加 evidence.signature 地址，verify --evidence swd 退出码 0
- [ ] 串口行协议（至少一个查询命令带寄存器读回）
- [ ] 档案加 probes，verify --all-probes 退出码 0

仓库里的 examples/apollo-h743 是三档全做完的实现，卡在哪一步可以翻
它对应的部分：bsp_console.c（banner）、bsp_signature.c（签名）、
app_console.c（行协议）。

## 7. 功能探针

启动验证只证明固件活着，探针进一步证明你改的那个功能真的能用。
原理：主机经串口下命令，断言板子的回答。

固件侧是一个很简单的行协议，一命令一响应。示例固件实现了这些：

```
命令            响应
ping            OK pong git=<版本> build=<时间>
led0? / led1?   OK led<N> state=<状态> ccr=<0..1000>
led0 <状态>     OK led<N> state=<状态>
selftest        OK selftest leds=2 states=BREATH
demo on|off     OK demo on|off
```

状态取值 OFF、ON、BLINK_SLOW、BLINK_FAST、BREATH，大小写不敏感。

有个设计细节值得强调：`led0 breath` 的响应里报的是读回来的实际
状态，不是把你请求的词原样弹回来。这样设计是因为最难缠的一类
bug 是设置函数静默失效：编译没警告，板子正常启动，ping 也通，但
设置根本没生效。回读式响应让这种 bug 在第一步就露馅，板子会回
`OK led0 state=OFF`，跟你请求的 BREATH 对不上。

探针定义在板卡档案的 `probes:` 节，由若干步组成，每步是发一条命令、
等一行匹配 expect 正则的响应、可选地对捕获组做 assert：

```yaml
probes:
  led-demo:
    description: LED 状态机与 PWM 寄存器
    step_timeout_s: 3
    steps:
      - send: "led0 breath"
        expect: '^OK led0 state=BREATH$'
      - send: "led0?"
        expect: '^OK led0 state=(?P<state>\S+) ccr=(?P<ccr>\d+)$'
        assert: "state == BREATH and ccr <= 1000"
```

assert 的语法是 `名字 操作符 值`，操作符支持 `== != > < >= <=`，多个
子句用 and 连接。值是数字就按数字比。这个求值器是手写的几十行
代码，不走 eval，探针文件是数据不是代码。

写断言有个原则：断言确定性的量，别断言瞬时值。呼吸灯进行中每次读
ccr 都是不同的数，断言 `ccr == 512` 必然闪断；断言状态、断言范围
（`ccr <= 1000`）才是稳的。

运行方式：

```powershell
flashgate verify --all-probes      # 完整闭环带全部探针
flashgate verify --probe led-demo  # 指定探针，可重复给多个
flashgate probe                    # 不编译不烧录，直接探正在跑的固件
```

第三种在调固件的时候很好用，改完烧完手动探一下，不用每次走全流程。

## 8. Stop hook

前面所有验证都得有人去跑。Stop hook 把它挂进 Claude Code 的生命周期：
agent 改动了 watched 文件、想结束回合的时候，hook 自动跑一次真机
验证，不过就拦下来（退出码 2），把失败原因喂回给 agent 让它继续修。

具体流程是这样。agent 说"我做完了"，Claude Code 触发 Stop 事件，
hook 脚本先给固件工作区算指纹（HEAD 加完整 diff 加未跟踪文件列表
的哈希），跟上次验证通过时存的指纹比。一致就直接放行，这个路径
耗时不到一秒，所以挂着它日常使用没有负担。指纹对不上才跑完整验证：
编译、烧录、听板子说话、跑探针，全过才放行。

拦不是无限拦。同一棵坏树最多拦两次，第三次放行，但会打一条明显的
警告，说这次停止的固件没有经过硬件验证。门不能把会话卡死，也
不能悄悄放过，所以拦是有次数上限的。

安装。项目级，写在固件工程的 `.claude/settings.json` 里，随仓库分发：

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "python /完整路径/flashgate/hooks/flashgate_stop.py --board /完整路径/boards/my-board.yaml",
        "timeout": 600
      }]
    }]
  }
}
```

仓库里的示例固件（`examples/apollo-h743/.claude/settings.json`）用的
是相对路径版本，因为它在 flashgate 仓库内部，clone 下来就能用。

自己一台机器上用，更省心的是用户级：同样的内容放进
`~/.claude/settings.json`，所有会话生效。

一个我们实际踩过的坑：项目级 hooks 在交互式会话里有审批机制，
第一次在某个目录启动 claude 时应该弹信任确认，没注意到、或者某些
版本没弹，hooks 就静默不加载，表现是"agent 说完成就完成了，门没
反应"。headless 模式（`claude -p`）不受这个影响，所以测试时钩子
正常、手动跑没反应，多半是这里。确认方法：会话里输 `/hooks`，
Stop 事件下面应该挂着 flashgate 那条命令。装在用户级可以完全绕开
这个问题。

其他实现了 Claude Code 兼容 hooks 契约的 harness 也能挂同一个脚本。

## 9. MCP server

让支持 MCP 的 agent 直接操作板子。装可选依赖后配置 `.mcp.json`：

```json
{ "mcpServers": { "flashgate": {
    "command": "flashgate-mcp",
    "args": ["--board", "/完整路径/boards/my-board.yaml"] } } }
```

提供八个工具：board_info 看档案，doctor 体检，build、flash、verify
对应同名命令，probe 跑探针，console_send 发一条命令给固件，
console_read 读一段时间串口输出。兼容 mcp 1.x 和 2.x。

## 10. 排错

以下每一条都是实际遇到过、修过的问题。

串口解析不出来（doctor 报 console UNRESOLVED）。先看线插没插。插了
还不行，多半是 USB 转 TTL 芯片跟档案里的 vid/pids 提示对不上，改
提示，或直接在 serial.port 写死 COM 口。

打不开串口，报"拒绝访问"。串口被别的程序占用了，串口助手、VSCode
的串口插件都算。关掉再跑。

烧录报 ST-LINK error (DEV_USB_COMM_ERR)。克隆版 ST-Link 连续烧多次
后偶发的 USB 通信故障。flashgate 内置了三次自动重试，重试之间还会
重启 stlink-server，一般自己就恢复了。真不行就拔插一下 ST-Link 的
USB，我们遇到过一次软件手段救不回来的，物理拔插立即好。

编译报 `'cube-cmake' 不是内部或外部命令`。在 VSCode 里装了 STM32
扩展并打开过固件工程的话，扩展会用自己的工具链重新 configure，
把它私有的工具名（cube-cmake）写进 build.ninja，离开那个环境就编
不了。flashgate 现在会识别这种失败并自动用标准 cmake 重新 configure
一次，正常情况下你只会看到一条黄色提示然后编译就过了。

verify 返回 3 但板子明显启动了。uart 通道的话检查 banner 格式是不是
跟 banner_regex 一致，固件侧改了 banner 格式就得同步改档案。swd
通道检查固件有没有真的调 bsp_signature_publish，以及地址跟档案
signature.address 是否一致。

verify 返回 5。板上固件的身份跟仓库现状不一致。常见于 commit 之后
没有重新构建，flashgate 的 verify 会先构建所以一般不会发生；发生在
手动 flash 之后，重新跑一次 verify 就好。

Stop hook 不触发。三个可能：启动目录不对，必须在挂着 settings.json
的那个目录启动 claude；项目级 hooks 没过审批，`/hooks` 里看不到就是
没加载；会话启动早于你写入配置，重启会话。

输出里夹着 `[36m` 之类的乱码。老版 PowerShell 5.1 的控制台不解释
ANSI 颜色码。v0.2.0 之后的 flashgate 会先探测终端能力，新开一个
窗口就正常了；也可以设环境变量 NO_COLOR 强制纯文本。

PowerShell 里 `echo $?` 显示 True/False。`$?` 在 PowerShell 里是布尔
不是退出码，用 `$LASTEXITCODE`。

探针偶尔闪断。八成是断言里写了非确定性的量，比如呼吸进行中的瞬时
ccr。改成断言状态和范围。

## 11. 已知限制

Windows 优先，工具链自动发现依赖 STM32Cube bundles 的目录结构，
Linux/macOS 没测。签名通道的固件参考实现针对 STM32H7 的 DTCM 方案，
往其他芯片移植时要自己挑一块调试口直读且不经过缓存的内存。swd 通道
没有功能探针。Stop hook 的审批行为跟 Claude Code 版本有关，以
`/hooks` 的实际显示为准。

---

有问题或想加自己板子的档案，开 issue：<https://github.com/Lion-1209/flashgate>
