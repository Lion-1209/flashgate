# flashgate 演示手册（真板，约 3 分钟全程）

前置：板子上电、ST-Link 和串口 USB 都插好、**关掉串口助手**（COM 口独占）。
每次破坏性演示后用 `git checkout -- <文件>` 还原。

录 GIF：Windows 推荐 ScreenToGif（免费），只录终端窗口，每场景 20–30 秒。

---

## 场景 0：体检（10 秒）

```bash
flashgate doctor
```

讲解词：*"先证明环境是好的——ST-Link 在线、串口在位、工具链齐全。"*
看点：全绿输出，`console : COM3 @ 115200 [VID/PID hint ...]`。

---

## 场景 1：绿灯——正常验证流（约 25 秒）

```bash
flashgate verify --all-probes
echo $?
```

讲解词：*"一条命令：编译、烧录、等板子开机自报身份、然后真刀真枪用一遍功能。"*
看点：

```
FLASHGATE-BOOT board=apollo-h743 git=<sha> build=... rtos=FreeRTOS   ← 板子自报身份
    step 1: led-demo> led0 breath
    board: OK led0 state=BREATH
    step 2: led-demo> led0?
    board: OK led0 state=BREATH ccr=691    ← TIM3 硬件寄存器实时值
exit code: 0                            ← 板子亲口证明能用
```

---

## 场景 2：红灯一——启动就挂（约 20 秒，M1 启动门）

破坏：在 `apollo/Core/Src/main.c` 的 `USER CODE BEGIN 2` 段里，
`bsp_console_boot_banner();` 之前插一行 `while (1) { }`。

```bash
flashgate verify --all-probes ; echo "exit=$?"
```

讲解词：*"这段代码编译通过、烧录成功——但板子 15 秒没说话。"*
看点：`TIMEOUT: board never printed the FLASHGATE-BOOT banner`，`exit=3`。
还原：`git -C ../apollo checkout -- Core/Src/main.c`

---

## 场景 3：红灯二——静默失效（约 25 秒，M2 探针门，最精彩）

破坏：`apollo/App/Src/app_led.c` 的 `led_set_state` 函数体第一行插 `return;`
（编译零警告、板子正常启动、ping 都通）。

```bash
flashgate verify --all-probes ; echo "exit=$?"
```

讲解词：*"最阴险的 bug：API 变成空操作，一切看起来正常。但固件回的是
读回来的实际状态——"*

```
    step 1: led-demo> led0 breath
    board: OK led0 state=OFF        ← 请求 BREATH，板子招了：根本没设上
exit=7
```

还原：`git -C ../apollo checkout -- App/Src/app_led.c`

---

## 场景 4：执法——Stop hook 拦截（约 60 秒，M3）

保持场景 3 的破坏不还原，直接模拟 agent 结束会话：

```bash
cd E:/999-Git/GithubTrency/flashgate
echo '{"hook_event_name":"Stop"}' | python hooks/flashgate_stop.py --board boards/apollo-h743.yaml
echo "exit=$?"     # 2 = 已拦截，agent 被强制继续干活
```

再跑两次同一条命令：第二次仍拦截（2/2），第三次放行但大声警告
（永不卡死会话、永不静默放水——coderio 四道门的升级语义）。

讲解词：*"在 Claude Code 里，这段逻辑挂在 Stop 事件上：agent 改了固件
想说'我做完了'，门先问板子。板子不点头，agent 就得继续修。"*

还原固件后再跑一次 hook：自动真机验证全绿 → `hardware verify PASS — stop allowed`。

---

## 场景 5（主菜）：真 agent 全程——Claude Code 被门拦下

前面四场是"门"在工作；这一场**主角是 agent 自己**。前置：本工作区的
`.claude/settings.json` 已挂好 Stop hook，直接可用。

```bash
cd E:/999-Git/GithubTrency
claude
```

**第一幕（守法）**：给它一个正常固件任务——

> 把 apollo 固件里 LED1 的开机默认状态从 BREATH 改成 BLINK_SLOW
> （文件：apollo/App/Src/app_led.c）

看点：agent 改完代码想结束回合 → Stop hook 检测到 watched 文件变化 →
自动上真机验证 → 全绿才放行。**不管 agent 诚实与否，门都会执行。**

**第二幕（执法）**：给它一个带跨仓库契约的任务——

> 把固件 boot banner 的前缀从 FLASHGATE-BOOT 改成 APOLLO-BOot
> （bsp_console.c 里）

agent 大概率只改固件侧 → 板子打出的 banner 与 boards/apollo-h743.yaml
的正则对不上 → verify 失败 → **Stop hook 拦截（exit 2）**，把板子证词
喂回给 agent → agent 被迫继续：发现契约横跨固件与板卡档案，补上 yaml
（或回退）→ 真机验证通过 → 才被允许说"完成"。

备选任务（同样必然触发拦截）：把固件 `led0?` 响应里的 `ccr=` 字段改名
为 `duty=`——探针正则立刻失配（exit 7）。

若 agent 一次就把两侧都改对（第一把就过），换备选任务再演一次；
或直接演示"agent 没跑 verify 就想结束"的第一幕变体——门替它跑了。

讲解词：*"看，agent 不是被提示词约束的——它被物理事实约束。
板子不点头，它走不掉。"*

（coderio 同样支持 Claude Code 兼容的 hooks 契约，把 flashgate_stop.py
挂进它的 `[[hooks]]` 即可用自己的 agent 演同一出戏。）

---

## 场景 6（可选）：MCP——任何 agent 直接和板子对话

```bash
python scripts/mcp_smoke.py
```

看点：`tools: [board_info, build, console_send, ...]` 8 个工具，
`console_send ping` 的回包来自真实固件。

---

## 一句话总结（收尾讲解词）

> "编译通过只说明语法对。flashgate 之后，'完成'的定义变成：
> 这份代码在真实芯片上启动、功能被真实使用、寄存器读回正确——
> 而且是板子自己证明的，不是 agent 嘴上说的。"
