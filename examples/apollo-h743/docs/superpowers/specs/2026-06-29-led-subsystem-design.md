# LED 子系统设计 (LED Indicator Subsystem)

- 日期：2026-06-29
- 状态：已批准（设计阶段）
- 目标平台：正点原子阿波罗 STM32H743IIT6 + FreeRTOS（CMSIS-RTOS V2）

## 1. 目标与非目标

### 目标
- 为板载 LED（PB0 / PB1）实现一个**分层、可移植**的指示子系统。
- 提供**语义化状态**：OFF / ON / BLINK_SLOW / BLINK_FAST / BREATH。
- 用**硬件 PWM** 实现呼吸效果（TIM3_CH3 / TIM3_CH4）。
- 把"亮度"作为驱动层与控制层之间的唯一抽象，使驱动后端**可插拔**（GPIO / PWM），换板只改板级表。

### 非目标（YAGNI）
- 不做命令队列（用直接调用 + 互斥量）。
- 不做 DMA 驱动的呼吸查表（两个 LED 不需要）。
- 不做 RGB / 多芯片 I2C 扩展 LED（接口已预留扩展性，但本轮不实现）。
- 不做运行时动态注册后端（编译期板级表足够）。

## 2. 分层架构

```
┌──────────────────────────────────────────────┐
│ 控制层 App/app_led  (状态机 + 管理任务 + 互斥量) │  知道"状态→亮度序列"，不懂硬件
│   输出: 亮度 level (0..1000)                  │
└───────────────────┬──────────────────────────┘
                    │ bsp_led_set_brightness(led, level)
┌───────────────────┴──────────────────────────┐
│ 驱动层 BSP/bsp_led  (亮度抽象 + ops 后端接口)    │  不含 RTOS
│   转发: led->ops->set_brightness(led, level)  │
└──────┬───────────────────────────────┬───────┘
       │ GPIO 后端                      │ PWM 后端
  level→亮/灭                       level→CCR 占空比
```

## 3. 目录结构

```
BSP/
├── Inc/
│   ├── bsp_led.h          驱动接口：bsp_led_t / bsp_led_ops_t / init / set_brightness
│   ├── bsp_tim3.h         TIM3 PWM 底层外设驱动
│   └── bsp_led_board.h    板级配置表声明（LED0/LED1 → 实例）
└── Src/
    ├── bsp_led.c          驱动核心（转发到 ops）
    ├── bsp_led_gpio.c     GPIO 后端 ops 实现
    ├── bsp_led_pwm.c      PWM  后端 ops 实现（用 bsp_tim3）
    ├── bsp_tim3.c         TIM3 + 引脚 AF 配置（含 MspInit）
    └── bsp_led_board.c    板级实例表 g_leds[]
App/
├── Inc/app_led.h          对外 API：状态枚举、led_id_t、led_set_state 等
└── Src/app_led.c          状态机 + 管理任务 + 互斥量
```

## 4. 驱动层接口

### 4.1 `bsp_led.h`

```c
#ifndef BSP_LED_H
#define BSP_LED_H

#include <stdint.h>
#include <stdbool.h>

typedef struct bsp_led bsp_led_t;

/* 后端操作集：每种后端（GPIO/PWM）实现一份并挂入实例 */
typedef struct {
    void (*init)(bsp_led_t *led);
    void (*set_brightness)(bsp_led_t *led, uint16_t level); /* level: 0..1000 */
} bsp_led_ops_t;

struct bsp_led {
    const bsp_led_ops_t *ops;     /* 指向后端 */
    void                *hw;      /* 后端私有上下文 */
    bool                 has_pwm; /* 能力位：能否连续调光（BREATH 依赖）*/
};

void bsp_led_init(bsp_led_t *led);
void bsp_led_set_brightness(bsp_led_t *led, uint16_t level); /* 转发到 ops->set_brightness */

#endif
```

### 4.2 GPIO 后端（`bsp_led_gpio.c`）

```c
typedef struct {
    GPIO_TypeDef *port;
    uint16_t      pin;
    bool          active_low;   /* true: 写 0 点亮 */
} led_gpio_hw_t;

/* ops: gpio_init / gpio_set_brightness */
/* set_brightness: level==0 → inactive 电平; else → active 电平 */
/*   active_low=true 时 active=0, inactive=1 */
```

### 4.3 PWM 后端（`bsp_led_pwm.c`）

```c
typedef struct {
    TIM_HandleTypeDef *htim;      /* 指向 htim3 */
    uint32_t           channel;   /* TIM_CHANNEL_3 / TIM_CHANNEL_4 */
    bool               active_low;
} led_pwm_hw_t;

/* ops: pwm_init / pwm_set_brightness */
/* set_brightness:
 *   ccr = active_low ? (1000 - level) : level;
 *   CCR 限幅到 [0, 1000]；CCR=1000（=ARR+1）对应 100% 占空。
 *   active_low=true 时：level=1000(full)→CCR=0→引脚常低→LED 全亮；
 *                       level=0(off)  →CCR=1000→引脚常高→LED 灭。
 *   __HAL_TIM_SET_COMPARE(htim, channel, ccr) */
```

`pwm_init` 调用 `bsp_tim3_init()`（幂等：仅首次真正初始化并 `HAL_TIM_PWM_Start` 对应通道）。

## 5. 板级配置（`bsp_led_board.h` / `.c`）

```c
/* bsp_led_board.h */
extern bsp_led_t g_leds[];   /* 以 led_id_t 为下标 */

/* bsp_led_board.c */
static led_pwm_hw_t s_led0_hw = { &htim3, TIM_CHANNEL_4, true }; /* PB1 */
static led_pwm_hw_t s_led1_hw = { &htim3, TIM_CHANNEL_3, true }; /* PB0 */

bsp_led_t g_leds[] = {
    { &s_pwm_ops, &s_led0_hw, true },  /* LED0 */
    { &s_pwm_ops, &s_led1_hw, true },  /* LED1 */
};
```

> 换板/换引脚时只改本文件：选后端 ops、填 hw 上下文、设 has_pwm。驱动核心与 App 代码不变。

## 6. 硬件映射

| 逻辑名 | 引脚 | 后端 | TIM 通道 | AF | 极性 |
|---|---|---|---|---|---|
| LED0 | PB1 | PWM | TIM3_CH4 | AF2 | active_low=true |
| LED1 | PB0 | PWM | TIM3_CH3 | AF2 | active_low=true |

### 已核实（硬件事实）
1. **LED0(DS0,红)=PB1、LED1(DS1,绿)=PB0** —— 来源：正点原子阿波罗 H743 资料 / RT-Thread sdk-bsp-stm32h743-atk-apollo。
2. **均为低电平点亮**（active_low=true）—— 同上来源。
3. **PB0=TIM3_CH3、PB1=TIM3_CH4，复用 AF2** —— 宏 `GPIO_AF2_TIM3`（`Drivers/STM32H7xx_HAL_Driver/Inc/stm32h7xx_hal_gpio_ex.h:93`）。

## 7. TIM3 PWM 配置

时钟链路：SYSCLK 480 → HCLK 240 → APB1 /2 = 120MHz → 定时器时钟 ×2 = **240MHz**。

| 参数 | 值 | 说明 |
|---|---|---|
| ARR | 999 | 1000 级占空比，与亮度量纲一致 |
| PSC | 239 | 240MHz / 240 / 1000 = **1kHz** |
| 通道 | CH3 (PB0)、CH4 (PB1) | PWM mode 1 |
| 极性 | 随 active_low | 用 `TIM_OCPOLARITY` 处理 |

- TIM3 是通用定时器，与 HAL 时基（TIM6）、FreeRTOS（SysTick）无冲突。
- TIM3 复用为 **AF2**（实现时以 H743 数据手册 alternate function mapping 表为准；注意 AF14 是 LTDC，不要混淆）。引脚 Mode=AF_PP，Speed=VERY_HIGH。
- 由 `bsp_tim3.c` 的 `HAL_TIM_MspInit`（或等价手写）完成 AF 配置。
- **占空比映射**：亮度 `level ∈ [0,1000]`；active_high 时 `CCR = level`，active_low 时 `CCR = 1000 - level`，均限幅 [0,1000]。CCR=1000 (=ARR+1) 对应 100% 占空。

### 现状改动
`gpio.c` 当前把 PB0/PB1 配成普通推挽输出。改走 PWM 后：
- 从 `MX_GPIO_Init()` 中移除 PB0/PB1 的配置（保留时钟使能无妨）。
- 由 `bsp_tim3` MspInit 把 PB0/PB1 配成 **AF2**（TIM3），Mode=AF_PP，Speed=VERY_HIGH。
- 同步在 `Apollo.ioc` 留注，避免日后重新生成时冲突（PB0/PB1 应改为 TIM3_CHx 而非 GPIO_Output）。

## 8. 控制层 API（`app_led.h`）

```c
#ifndef APP_LED_H
#define APP_LED_H

#include <stdbool.h>

typedef enum {
    LED_OFF = 0,
    LED_ON,
    LED_BLINK_SLOW,
    LED_BLINK_FAST,
    LED_BREATH,
} led_state_t;

typedef enum {
    LED0 = 0,
    LED1,
    LED_COUNT
} led_id_t;

void        app_led_init(void);                 /* 板级驱动初始化 + 建 mutex + 建任务 */
void        led_set_state(led_id_t id, led_state_t state);
led_state_t led_get_state(led_id_t id);

#endif
```

## 9. 状态机与时序

管理任务 `appLedTask` 以 **10ms 为周期**（`osDelayUntil`）运行，对每个 LED 读当前状态、计算亮度、写驱动层。

| 状态 | 亮度序列（tick = 10ms） | 频率/周期 |
|---|---|---|
| `LED_OFF` | 固定 0 | — |
| `LED_ON` | 固定 1000 | — |
| `LED_BLINK_SLOW` | 0/1000 交替，on 500ms / off 500ms（各 50 tick） | 1 Hz |
| `LED_BLINK_FAST` | 0/1000 交替，on 120ms / off 120ms（各 12 tick） | ≈4.2 Hz |
| `LED_BREATH` | 50 点亮度查表，每 4 tick 推进一点（50×4=200 tick） | 周期 2 s |

- 查表内容为 0→1000→0 的平滑（类正弦/伽马）曲线，避免线性渐变"突变感"。
- 每个 LED 维护一个相位计数器；`led_set_state` 改状态时复位相位。
- OFF/ON 这种稳态：仅在状态切换那一拍写一次亮度即可（不必每拍重写）。

### 呼吸回退保护
`led_set_state(id, LED_BREATH)`：
- 若 `g_leds[id].has_pwm == false` → Debug 构建走 `configASSERT`；Release 构建回退为 `LED_ON`。

## 10. 数据流与并发

```
任意任务 ──led_set_state()──► [Mutex] 写 state[id]、复位相位 ──► 解锁
                                                                    ▲ 读取
appLedTask(每10ms) ──► [Mutex] 读 state ──► 算 level ──► bsp_led_set_brightness() ──► ops->set_brightness() ──► 写寄存器
```

- 单个互斥量保护 `s_state[LED_COUNT]`。
- 临界区内：读枚举 + 算亮度 + 写 CCR/GPIO 寄存器，**均为非阻塞**（写 CCR 为数条指令），持锁时间极短。
- `led_set_state` 可被任何任务调用；写状态在锁内完成。

## 11. RTOS 对象与初始化时机

| 对象 | 配置 |
|---|---|
| `appLedTask` | osPriorityLow，栈 256 字，周期 10ms（`osDelayUntil`） |
| 互斥量 ×1 | 保护 `s_state[]` |

`app_led_init()` 一次性完成：
1. `bsp_led_init()` 逐个初始化板级实例（→ PWM 后端 init → `bsp_tim3_init` + 启动 PWM 通道）。
2. 创建互斥量。
3. 创建 `appLedTask`。

调用点：`MX_FREERTOS_Init()` 内 `/* USER CODE BEGIN RTOS_THREADS */`。此时 `osKernelInitialize()` 已执行（可建 RTOS 对象），调度器尚未启动（硬件初始化也安全）。

## 12. 集成改动

### CMake（`cmake/stm32cubemx/CMakeLists.txt`）
给 `stm32cubemx` INTERFACE 库追加（不污染 CubeMX 生成的 `MX_*` 列表）：
- include 路径：`BSP/Inc`、`App/Inc`
- 源文件：`BSP/Src/{bsp_led,bsp_led_gpio,bsp_led_pwm,bsp_tim3,bsp_led_board}.c`、`App/Src/app_led.c`

### `freertos.c`
在 `MX_FREERTOS_Init()` 的 `/* USER CODE BEGIN RTOS_THREADS */` 调用 `app_led_init();`。
`appLedTask` 由 `app_led_init` 内部创建（不占用现有 `defaultTask`/`appTaskLed`）。

### `gpio.c`
移除 PB0/PB1 的推挽输出配置（改由 `bsp_tim3` 配 AF）。

### `Apollo.ioc`
PB0/PB1 由 `GPIO_Output` 改为 TIM3_CH3/CH4（避免日后重新生成覆盖）。

## 13. 错误处理

风格对齐现有工程（嵌入式惯例，不做动态错误返回）：
- `bsp_tim3_init()` 失败 → `Error_Handler()`。
- 配置类错误（ops 为空、`id` 越界、互斥量获取失败）→ Debug 构建走 `configASSERT`；Release 构建 no-op / clamp。
- `led_set_state` 对越界 `id` → `configASSERT`（Debug）。

## 14. 验证计划

1. **编译**：CMake Debug 配置无错误无警告（`-Wall` 下）。
2. **静态链接检查**：确认 `g_leds[]`、ops、PWM 通道符号正确。
3. **上电默认演示**：`app_led_init` 后设 `LED0 = LED_BLINK_SLOW`、`LED1 = LED_BREATH`，肉眼确认：LED0 慢闪、LED1 平滑呼吸。
4. **运行时切换自检**：在 `defaultTask` 加一段自检序列（每 3s 轮换 `LED_OFF→ON→SLOW→FAST→BREATH`），证明 `led_set_state` 可被其他任务安全调用、状态正确切换。
5. **无 UART/printf**：靠**视觉 + SWD 在线观察 `s_state[]` 与 CCR 寄存器**验证。
6. **回退保护**：临时把某 LED 改成 GPIO 后端，调 `LED_BREATH`，确认 Debug 触发 assert。

## 15. 评审记录

- 设计讨论中已对齐的关键决策：能力层级=完整指示框架；呼吸=硬件 PWM；后端绑定=ops 结构体；调用模型=直接调用+锁+10ms 周期任务。
- 用户对方案的修正："始终走 PWM 会硬绑定 PWM 引脚、不普适" → 改为亮度抽象 + 可插拔后端 + `has_pwm` 能力位。本设计已落实该修正。
