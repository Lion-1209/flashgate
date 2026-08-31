# LED 子系统代码详解

> 本文逐文件、逐函数拆解 [BSP/](../BSP) 和 [App/](../App) 下的 LED 代码。读完你应该能回答：
> 一次"设置 LED 为呼吸"的调用，是怎么最终变成 PB0/PB1 上 PWM 占空比变化的？
>
> 相关文档：[CMSIS-RTOS v2 ↔ FreeRTOS API 指南](cmsis-freertos-api.md)、[设计 spec](superpowers/specs/2026-06-29-led-subsystem-design.md)。

---

## 1. 全景：分层与文件职责

```
        应用 / 其他任务
              │  led_set_state(LED0, LED_BREATH)        ← 语义化 API（人话）
              ▼
┌─────────────────────────────────────────┐
│  控制层  App/app_led.{c,h}               │  状态机：把"状态"翻译成"亮度随时间的变化"
│    appLedTask 每 10ms 算出每个 LED 的亮度 │  并发：互斥量保护共享状态
└─────────────────────────────────────────┘
              │  bsp_led_set_brightness(led, level 0..1000)   ← 统一抽象：亮度
              ▼
┌─────────────────────────────────────────┐
│  驱动核心 BSP/bsp_led.{c,h}              │  只做转发：led->ops->set_brightness(led, level)
│    定义 bsp_led_t / bsp_led_ops_t 接口   │  不知道硬件长啥样
└─────────────────────────────────────────┘
              │  通过 ops 函数指针分派到具体后端
       ┌──────┴───────┐
       ▼              ▼
┌──────────────┐  ┌──────────────────────┐
│ GPIO 后端    │  │ PWM 后端              │
│ bsp_led_gpio │  │ bsp_led_pwm           │
│ level→亮/灭  │  │ level→CCR 占空比       │
│ (本项目未用) │  │  └─ bsp_tim3 (TIM3 配置)│
└──────────────┘  └──────────────────────┘
              │
              ▼  最终：写 TIM3->CCRx / GPIO->BSRR 寄存器
┌─────────────────────────────────────────┐
│  板级配置 BSP/bsp_led_board.{c,h}        │  把"LED0/LED1"绑定到具体后端 + 引脚 + 极性
└─────────────────────────────────────────┘
```

**文件清单：**

| 文件 | 行数 | 职责 | 依赖方向 |
|---|---|---|---|
| [BSP/Inc/bsp_led.h](../BSP/Inc/bsp_led.h) | ~30 | 驱动接口：`bsp_led_t`、`bsp_led_ops_t`、两个原型 | 无（纯 C 标准类型）|
| [BSP/Src/bsp_led.c](../BSP/Src/bsp_led.c) | ~15 | 驱动核心：转发到 ops | bsp_led.h |
| [BSP/Src/bsp_led_gpio.c](../BSP/Src/bsp_led_gpio.c) | ~30 | GPIO 后端（量化亮度为亮/灭）| bsp_led.h, HAL |
| [BSP/Inc/bsp_tim3.h](../BSP/Inc/bsp_tim3.h) | ~15 | TIM3 PWM 外设原型 | HAL |
| [BSP/Src/bsp_tim3.c](../BSP/Src/bsp_tim3.c) | ~57 | TIM3 时基 + 通道 + 引脚 AF | bsp_tim3.h, HAL |
| [BSP/Inc/bsp_led_pwm.h](../BSP/Inc/bsp_led_pwm.h) | ~20 | PWM 后端接口 + `led_pwm_hw_t` | bsp_led.h, bsp_tim3.h |
| [BSP/Src/bsp_led_pwm.c](../BSP/Src/bsp_led_pwm.c) | ~31 | PWM 后端（亮度→CCR，含极性翻转）| bsp_led_pwm.h |
| [BSP/Inc/bsp_led_board.h](../BSP/Inc/bsp_led_board.h) | ~18 | `led_id_t` 枚举 + `g_leds[]` 声明 | bsp_led.h |
| [BSP/Src/bsp_led_board.c](../BSP/Src/bsp_led_board.c) | ~11 | 实例表（LED0/LED1 → PWM）| bsp_led_board.h, bsp_led_pwm.h |
| [App/Inc/app_led.h](../App/Inc/app_led.h) | ~30 | 公共 API：`led_state_t` + 原型 | bsp_led_board.h |
| [App/Src/app_led.c](../App/Src/app_led.c) | ~111 | 状态机 + 任务 + 互斥量 + 呼吸 LUT | app_led.h, bsp_led.h, FreeRTOS |

依赖永远**从上往下**（App → BSP → HAL）。BSP 永远不 include App。

---

## 2. 数据流：一次亮度更新的完整旅程

`appLedTask` 每 10ms 做这一串调用，从抽象一路落到寄存器：

```
appLedTask (app_led.c)
  bsp_led_set_brightness(&g_leds[i], level)        ← bsp_led.c
    └─ led->ops->set_brightness(led, level)        ← 通过函数指针
        └─ pwm_set_brightness(led, level)          ← bsp_led_pwm.c（ops 指向它）
              计算出 ccr = 1000 - level（active_low）
            └─ __HAL_TIM_SET_COMPARE(&htim3, TIM_CHANNEL_x, ccr)   ← HAL 宏
                  └─ TIM3->CCR3 = ccr              ← 最终：写一个 32 位寄存器
```

整个链条的核心是 **`ops` 函数指针**——它让上层不需要知道下层是 GPIO 还是 PWM。下面逐层拆。

---

## 3. 驱动核心：[bsp_led.h](../BSP/Inc/bsp_led.h) + [bsp_led.c](../BSP/Src/bsp_led.c)

### 3.1 接口设计（"亮度"是唯一抽象）

```c
typedef struct bsp_led bsp_led_t;          // 前向声明（self-reference）

typedef struct {
    void (*init)(bsp_led_t *led);
    void (*set_brightness)(bsp_led_t *led, uint16_t level);   // level: 0..1000
} bsp_led_ops_t;                           // ← 后端操作集（函数指针表）

struct bsp_led {
    const bsp_led_ops_t *ops;     // 指向某个后端的 ops
    void                *hw;      // 后端私有硬件上下文（GPIO/PWM 各自定义）
    bool                 has_pwm; // 能力位：能否连续调光（呼吸依赖）
};
```

**两个关键决定：**

1. **抽象边界是"亮度 level (0..1000)"**，不是"开/关"也不是"占空比"。这是整个设计的点睛之笔——控制层只懂亮度，后端把亮度翻译成自己的硬件语言。换后端不用动控制层。
2. **`ops` 是函数指针表**——这是 C 语言实现"多态/可插拔"的经典手法（和 Linux 内核的 `file_operations`、HAL 的回调一个套路）。GPIO 后端和 PWM 后端各自实现一份 `bsp_led_ops_t`，板级表决定每个 LED 用哪份。

### 3.2 核心实现：只转发

```c
// bsp_led.c
void bsp_led_init(bsp_led_t *led) {
    led->ops->init(led);                    // 转发到后端的 init
}
void bsp_led_set_brightness(bsp_led_t *led, uint16_t level) {
    if (level > 1000u) level = 1000u;       // 公共契约：钳位（防御性）
    led->ops->set_brightness(led, level);   // 转发到后端的 set_brightness
}
```

`bsp_led.c` 自己**完全不知道硬件**——它只把调用顺着 `ops` 指针扔下去。所有硬件知识都在后端和板级表里。这就是它能在任何"亮度型"设备上复用的原因。

---

## 4. 后端 A：[bsp_led_gpio.c](../BSP/Src/bsp_led_gpio.c)（GPIO，本项目未用）

```c
typedef struct {
    GPIO_TypeDef *port;
    uint16_t      pin;
    bool          active_low;
} led_gpio_hw_t;                            // ← GPIO 后端的私有上下文

static void gpio_set_brightness(bsp_led_t *led, uint16_t level) {
    led_gpio_hw_t *hw = (led_gpio_hw_t *)led->hw;   // 从 void* 取回自己的类型
    GPIO_PinState active   = hw->active_low ? GPIO_PIN_RESET : GPIO_PIN_SET;
    GPIO_PinState inactive = hw->active_low ? GPIO_PIN_SET   : GPIO_PIN_RESET;
    HAL_GPIO_WritePin(hw->port, hw->pin, (level == 0u) ? inactive : active);
}
```

**它存在的意义：** 证明这套抽象不是"只有 PWM"的假货。一个普通 GPIO 引脚的 LED 配上 GPIO 后端，亮/灭/闪烁照样能用（闪烁 = 控制层在 level 0 和 1000 之间切，GPIO 后端天然量化成亮/灭）。**呼吸会失败**（GPIO 没法调亮度），所以它的实例 `has_pwm` 会是 `false`，控制层会自动回退为常亮（见 §7.3）。

> 本项目两个 LED 都用 PWM 后端，所以 `g_led_gpio_ops` 编译了但没人引用，链接器把它丢弃（FLASH 不变大）。这是"为可移植性预留、不为 YAGNI 买单"的平衡。

---

## 5. 后端 B：TIM3 + PWM（本项目实际用的）

这部分分两个文件：[bsp_tim3.c](../BSP/Src/bsp_tim3.c) 管外设配置，[bsp_led_pwm.c](../BSP/Src/bsp_led_pwm.c) 管亮度→占空比。

### 5.1 [bsp_tim3.c](../BSP/Src/bsp_tim3.c)：把 TIM3 配成 1kHz、1000 级 PWM

**时钟链路（为什么 PSC=239, ARR=999）：**

```
HSE 25MHz → PLL → SYSCLK 480MHz → AHB/2 → HCLK 240MHz
                                       → APB1 /2 = 120MHz
                                          → 定时器时钟 ×2 = 240MHz（APB 预分频≠1 时自动倍频）
PSC=239 → 计数 tick = 240MHz / 240 = 1MHz        （即每 1µs 计一个数）
ARR=999 → PWM 周期 = 1000 个计数 = 1kHz           （占空比分辨率正好 1000 级）
```

**为什么这样选？** 1000 级占空比和控制层的 `level (0..1000)` 量纲一致——level 直接就是 CCR 值（active_high 时），零换算。1kHz 频率人眼看不到闪烁。

```c
void bsp_tim3_init(void) {
    if (s_initialized) return;              // ← 幂等：多次调用只配一次（两个 LED 都会触发）

    htim3.Instance = TIM3;
    htim3.Init.Prescaler = 239;             // → 1MHz tick
    htim3.Init.Period    = 999;             // → 1kHz, 1000 级
    ...
    HAL_TIM_PWM_Init(&htim3);               // ← 内部会回调 HAL_TIM_PWM_MspInit（见下）

    TIM_OC_InitTypeDef oc = {0};
    oc.OCMode     = TIM_OCMODE_PWM1;        // PWM1: 通道在 CNT<CCR 期间"有效"(HIGH)
    oc.OCPolarity = TIM_OCPOLARITY_HIGH;    // 有效电平=高（极性翻转交给后端用 CCR 做）
    oc.Pulse      = 0;                      // 初始 CCR=0（灭）；运行时由后端改
    HAL_TIM_PWM_ConfigChannel(&htim3, &oc, TIM_CHANNEL_3);   // PB0
    HAL_TIM_PWM_ConfigChannel(&htim3, &oc, TIM_CHANNEL_4);   // PB1
    s_initialized = true;
}
```

**引脚配置在 MspInit 里（HAL 的约定）：**

```c
void HAL_TIM_PWM_MspInit(TIM_HandleTypeDef *htim) {
    if (htim->Instance != TIM3) return;     // 只处理 TIM3（这个回调可能被别的定时器共享）
    __HAL_RCC_TIM3_CLK_ENABLE();            // 开 TIM3 时钟
    __HAL_RCC_GPIOB_CLK_ENABLE();           // 开 GPIOB 时钟
    GPIO_InitTypeDef g = {0};
    g.Pin       = GPIO_PIN_0 | GPIO_PIN_1;  // PB0=TIM3_CH3, PB1=TIM3_CH4
    g.Mode      = GPIO_MODE_AF_PP;          // 复用推挽
    g.Alternate = GPIO_AF2_TIM3;            // ← AF2 = TIM3 的复用功能号
    g.Speed     = GPIO_SPEED_FREQ_VERY_HIGH;
    HAL_GPIO_Init(GPIOB, &g);
}
```

> **MspInit 是什么？** HAL 的分层约定：`HAL_TIM_PWM_Init` 管"定时器寄存器"，`HAL_TIM_PWM_MspInit`（**M**CU **S**upport **P**ackage Init）管"MCU 相关的底层"（时钟、引脚）。前者由 HAL 在初始化时**自动回调**后者。所以你不需要自己调 MspInit，只要定义它。
>
> **幂等性**：`bsp_led_init` 会为 LED0、LED1 各调一次 `pwm_init`→`bsp_tim3_init`。没有 `s_initialized` 守卫的话，第二次会重新配一遍 TIM3（浪费且可能出错）。守卫让它只配一次，`HAL_TIM_PWM_Start` 则是每通道各调一次（正常）。

### 5.2 [bsp_led_pwm.c](../BSP/Src/bsp_led_pwm.c)：亮度 → CCR（含极性翻转）

这是**全项目数学最绕的一个函数**，慢慢看：

```c
static void pwm_set_brightness(bsp_led_t *led, uint16_t level) {
    led_pwm_hw_t *hw = (led_pwm_hw_t *)led->hw;
    uint32_t ccr = hw->active_low ? (LED_PWM_CCR_MAX - level) : (uint32_t)level;
    if (ccr > LED_PWM_CCR_MAX) ccr = LED_PWM_CCR_MAX;
    __HAL_TIM_SET_COMPARE(hw->htim, hw->channel, ccr);   // 写 TIM3->CCRx = ccr
}
```

**前提（TIM3 配置决定）：** PWM mode 1 + OC 极性 HIGH → **引脚在 `CNT < CCR` 期间为高**，其余为低。计数器 0..999 循环。
- 引脚为高的时间占比 ≈ `CCR / 1000`。

**active_high LED（高电平点亮）：**
引脚高 = 亮。所以"亮度 level"直接等于"高的占比" → `CCR = level`。
- `level=1000` → `CCR=1000` → 引脚常高 → 全亮 ✓
- `level=0` → `CCR=0` → 引脚常低 → 灭 ✓

**active_low LED（低电平点亮，本板 LED 就是这种）：**
引脚低 = 亮。"亮度 level"现在代表的是**引脚为低的时间占比**。引脚低发生在 `CNT ≥ CCR` 期间，占比 = `(1000 - CCR) / 1000`。令它等于 `level/1000` → **`CCR = 1000 - level`**。
- `level=1000`（全亮）→ `CCR=0` → 引脚常低 → 全亮 ✓
- `level=0`（灭）→ `CCR=1000` → 引脚常高 → 灭 ✓

**一句话：极性翻转不改硬件配置（OCPOLARITY 保持 HIGH），而是在软件里把 CCR 取反。** 这样多个 LED 共用同一套 OC 配置，只靠各自 `active_low` 标志区分。

> `LED_PWM_CCR_MAX = 1000 = ARR(999) + 1`。CCR=1000 时 `CNT<CCR` 永远成立 → 100% 占空，这是 PWM mode 1 达到满占空的写法（CCR=ARR 只到 99.9%）。

**初始化（`pwm_init`）：**

```c
static void pwm_init(bsp_led_t *led) {
    led_pwm_hw_t *hw = (led_pwm_hw_t *)led->hw;
    bsp_tim3_init();                        // 配 TIM3（幂等）
    HAL_TIM_PWM_Start(hw->htim, hw->channel); // 使能该通道输出（写 CCxE 位）
    pwm_set_brightness(led, 0u);            // 初始灭
}
```

---

## 6. 板级配置：[bsp_led_board.c](../BSP/Src/bsp_led_board.c)

把抽象的 "LED0/LED1" 绑到具体硬件。**换板子只改这一个文件。**

```c
static led_pwm_hw_t s_led0_hw = { .htim = &htim3, .channel = TIM_CHANNEL_4, .active_low = true }; // LED0=PB1=CH4
static led_pwm_hw_t s_led1_hw = { .htim = &htim3, .channel = TIM_CHANNEL_3, .active_low = true }; // LED1=PB0=CH3

bsp_led_t g_leds[LED_COUNT] = {
    { .ops = &g_led_pwm_ops, .hw = &s_led0_hw, .has_pwm = true },   // LED0 用 PWM 后端
    { .ops = &g_led_pwm_ops, .hw = &s_led1_hw, .has_pwm = true },   // LED1 用 PWM 后端
};
```

每行三件事：选**后端 ops**（GPIO/PWM）、填**硬件上下文 hw**（哪个定时器/通道/极性）、声明**能力** has_pwm。

> 想象一下换板：新板 LED2 是个普通 GPIO 排针灯 → 加一行 `{ &g_led_gpio_ops, &s_led2_gpio_hw, false }`，控制层一行不改，它照样能开/关/闪，只是不能呼吸。这就是 §3.1 抽象的回报。

---

## 7. 控制层：[app_led.c](../App/Src/app_led.c)（最核心）

### 7.1 状态机：把"状态"翻译成"亮度随时间变化"

核心是 `compute_level`——一个**纯函数**（无 I/O、无副作用），输入 `(状态, 相位)`，输出亮度：

```c
static uint16_t compute_level(const led_rt_t *rt) {
    uint32_t p = rt->phase;                 // 自状态设置以来经过的 tick 数（每 tick=10ms）
    switch (rt->state) {
        case LED_OFF:        return 0u;
        case LED_ON:         return 1000u;
        case LED_BLINK_SLOW: return (p % SLOW_PERIOD) < SLOW_ON ? 1000u : 0u;  // 见下
        case LED_BLINK_FAST: return (p % FAST_PERIOD) < FAST_ON ? 1000u : 0u;
        case LED_BREATH:     return s_breath_lut[(p / BREATH_STEP_TICKS) % 50u];
        default:             return 0u;
    }
}
```

**闪烁的数学**（以慢闪为例）：
```
SLOW_PERIOD = 100 ticks = 1000ms（整个亮灭周期 1 秒）
SLOW_ON     = 50  ticks = 500ms（前半周期亮）
phase % 100 < 50  → 亮(1000)；否则 → 灭(0)
效果：500ms 亮 / 500ms 灭，1Hz，50% 占空。
```
快闪同理：周期 24 ticks=240ms，亮 12 ticks=120ms → ≈4.2Hz。

**呼吸的数学：**
```
BREATH_STEP_TICKS = 4：phase 每过 4 个 tick（40ms），LUT 推进一格
LUT 50 格 × 4 tick/格 × 10ms = 2000ms = 一次完整呼吸（0→1000→0）
index = (phase / 4) % 50，亮度 = s_breath_lut[index]
```

**呼吸 LUT 怎么来的**（见文件顶部那张表）：50 个点，值是 `round(1000·sin(π·i/50))`，i=0..49。它构成一个 0→1000(峰值在 i=25)→~0 的正弦半周期，所以呼吸看起来**两头慢、中间快**（线性渐变会显得"突兀"，正弦更自然）。这是**预计算表**——避免在 10ms 任务里跑浮点 `sinf`（省 CPU、不依赖 libm）。

> `compute_level` 故意写成纯函数，是为了将来能用 host 单测验证各种 phase/状态组合（虽然本环境没有 host 编译器，暂时只能靠上板观察）。

### 7.2 运行时状态 + 任务

```c
typedef struct { led_state_t state; uint32_t phase; } led_rt_t;
static led_rt_t s_rt[LED_COUNT];            // 每个 LED 的运行时状态（共享数据！）
static osMutexId_t s_mutex;                 // 保护 s_rt[]
static osThreadId_t s_task;

static void appLedTask(void *arg) {
    (void)arg;
    uint32_t next = osKernelGetTickCount() + TASK_PERIOD_MS;
    for (;;) {
        osDelayUntil(next);                 // 防漂移：睡到绝对时刻 next（10ms 周期）
        next += TASK_PERIOD_MS;             // next 按固定步长累加，干活耗时被吸收

        osMutexAcquire(s_mutex, osWaitForever);     // 加锁
        for (int i = 0; i < LED_COUNT; i++) {
            bsp_led_set_brightness(&g_leds[i], compute_level(&s_rt[i]));  // 算+写硬件
            s_rt[i].phase++;                                              // 推进相位
        }
        osMutexRelease(s_mutex);                   // 解锁
    }
}
```

**为什么周期任务用 `osDelayUntil` 不是 `osDelay`**：`osDelay(10)` 的实际周期 = 干活耗时 + 10ms，会漂移；`osDelayUntil` 锁定绝对时刻，10ms 严格稳定——呼吸/闪烁才均匀。详见 [API 指南 §3.2](cmsis-freertos-api.md)。

### 7.3 线程安全的 API（任意任务可调）

```c
void led_set_state(led_id_t id, led_state_t state) {
    configASSERT(id < LED_COUNT);           // 越界 → 断言卡死（调试期发现 bug）

    if ((state == LED_BREATH) && !g_leds[id].has_pwm) {
        state = LED_ON;                     // GPIO 后端不能呼吸 → 回退常亮（优雅降级）
    }

    osMutexAcquire(s_mutex, osWaitForever);
    s_rt[id].state = state;                 // 改状态
    s_rt[id].phase = 0u;                    // ← 复位相位：新状态从序列起点开始
    osMutexRelease(s_mutex);
}
```

**两个细节：**
1. **`phase = 0`** 很重要——不复位的话，切到闪烁时可能从"灭"那半周期开始，看着像延迟反应。
2. **互斥量保护的是 `s_rt[]`**。`appLedTask` 读它+推进 phase，`led_set_state` 写它。两者都持锁，不会交错。硬件寄存器写入（`__HAL_TIM_SET_COMPARE`）也在锁内，但它只是一条内存写、纳秒级、不阻塞，持锁时间极短。

### 7.4 初始化：把一切串起来

```c
void app_led_init(void) {
    s_mutex = osMutexNew(&(osMutexAttr_t){.name="led_mtx"});  // 1. 建互斥量
    configASSERT(s_mutex != NULL);

    for (int i = 0; i < LED_COUNT; i++) {
        s_rt[i].state = LED_OFF;            // 2. 运行时初值
        bsp_led_init(&g_leds[i]);           // 3. 硬件初始化（→ PWM 后端 → TIM3 配置 + PWM 启动）
    }

    s_task = osThreadNew(appLedTask, NULL, &(osThreadAttr_t){  // 4. 建任务
        .name="appLedTask", .stack_size=256*4, .priority=osPriorityLow });
    configASSERT(s_task != NULL);

    led_set_state(LED1, LED_BREATH);        // 5. 默认演示状态
    led_set_state(LED0, LED_BLINK_SLOW);
}
```

调用顺序有讲究：**先建互斥量**（后面 `led_set_state` 要用）→ **初始化硬件**（确保任务一跑就能写 CCR）→ **建任务** → **设默认状态**。这个函数在 [freertos.c](../Core/Src/freertos.c) 的 `MX_FREERTOS_Init` 里、`osKernelStart` 之前被调用，所以任务创建后要到调度器启动才真正运行。

---

## 8. 上电完整时序

```
main() [main.c]
 ├─ MPU_Config / HAL_Init / SystemClock_Config / MX_GPIO_Init / MX_LTDC_Init
 ├─ osKernelInitialize()                          ← 内核就绪
 ├─ MX_FREERTOS_Init() [freertos.c]
 │     ├─ osThreadNew(StartDefaultTask, ...)      ← CubeMX 建的空任务（我们用来跑 demo）
 │     ├─ osThreadNew(StartTask02, ...)           ← CubeMX 建的空任务（没用，可删）
 │     └─ app_led_init()  ← 我们的代码
 │           ├─ osMutexNew                        建 led_mtx
 │           ├─ bsp_led_init(LED0) → pwm_init → bsp_tim3_init(配TIM3+PB0/PB1) + PWM_Start(CH4)
 │           ├─ bsp_led_init(LED1) → pwm_init → bsp_tim3_init(幂等跳过) + PWM_Start(CH3)
 │           ├─ osThreadNew(appLedTask, ...)       建 LED 任务（就绪但未跑）
 │           └─ led_set_state(LED1, BREATH / LED0, SLOW)  写 s_rt[]
 └─ osKernelStart()                               ← 调度器启动，任务开始跑

调度器启动后（并发）：
 appLedTask (Low,8):    每 10ms → 加锁 → 算亮度 → 写 CCR → phase++ → 解锁
 defaultTask (Normal,24): 每 3s → led_set_state(LED0, 下一个状态)  [跨任务、加锁]
```

---

## 9. 扩展指南

| 想做什么 | 改哪里 | 控制层/App 要改吗 |
|---|---|---|
| 换 LED 的引脚/定时器通道 | [bsp_led_board.c](../BSP/Src/bsp_led_board.c) 的 `s_ledX_hw` | 不改 |
| 改"低电平点亮"为"高电平点亮" | 同上，`.active_low = false` | 不改 |
| 加第 3 个 LED（PWM 型） | [bsp_led_board.h](../BSP/Inc/bsp_led_board.h) 加枚举值 + board.c 加实例（可能要扩 TIM3 通道或用另一个定时器）| 不改 |
| 加第 3 个 LED（普通 GPIO） | 同上，ops 改成 `&g_led_gpio_ops`、has_pwm=false、定义 `led_gpio_hw_t` | 不改（呼吸会自动回退常亮）|
| 加新状态（如"双闪"） | [app_led.h](../App/Inc/app_led.h) 加枚举 + app_led.c 的 `compute_level` switch 加一分支 + 可选宏 | 这是控制层内部改动 |
| 调呼吸速度/闪烁频率 | [app_led.c](../App/Src/app_led.c) 顶部那几个 `#define`（`SLOW_PERIOD`/`BREATH_STEP_TICKS` 等）| 不改 |
| 换一块完全不同的板子 | 改 [bsp_led_board.c](../BSP/Src/bsp_led_board.c)（+ 可能加后端文件）| **不改** |

**加一个全新后端**（比如 I²C 扩展芯片驱动的 LED）的步骤，体现 ops 架构的价值：
1. 新建 `bsp_led_i2c.c`：实现 `i2c_init`/`i2c_set_brightness` + 导出 `g_led_i2c_ops`。
2. board.c 里某个 LED 的 `.ops = &g_led_i2c_ops`、`.hw = &s_i2c_hw`。
3. **`bsp_led.c` 和 `app_led.c` 一行都不用改**——这就是抽象的意义。

---

## 10. 关键数字速查

| 项 | 值 | 出处 |
|---|---|---|
| RTOS tick | 1 ms（1kHz） | [FreeRTOSConfig.h](../Core/Inc/FreeRTOSConfig.h) `configTICK_RATE_HZ` |
| 管理任务周期 | 10 ms | app_led.c `TASK_PERIOD_MS` |
| TIM3 PWM 频率 | 1 kHz | bsp_tim3.c PSC=239, ARR=999 |
| 占空比分辨率 | 1000 级（0..1000） | = 亮度 level 量纲 |
| 亮度 level 范围 | 0..1000（0=灭, 1000=满亮）| bsp_led.h / app_led.c |
| 慢闪 | 1 Hz（500ms 亮/灭）| `SLOW_PERIOD=100` ticks |
| 快闪 | ≈4.2 Hz（120ms 亮/灭）| `FAST_PERIOD=24` ticks |
| 呼吸周期 | 2 s（0→满→0）| 50 点 LUT × 4 tick × 10ms |
| 呼吸 LUT | 50 点，正弦 `round(1000·sin(π·i/50))` | app_led.c `s_breath_lut` |
| LED 任务栈 | 1024 字节（256×4）| app_led.c `stack_size` |
| LED 任务优先级 | osPriorityLow = 8 | app_led.c |
| LED0 | DS0 红，PB1 = TIM3_CH4，active_low | bsp_led_board.c |
| LED1 | DS1 绿，PB0 = TIM3_CH3，active_low | bsp_led_board.c |
| FreeRTOS 堆 | 15360 字节（heap_4）| FreeRTOSConfig.h |
