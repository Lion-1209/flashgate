# CMSIS-RTOS v2 ↔ FreeRTOS API 速查与详解

> 配套本项目的 LED 子系统代码（[App/Src/app_led.c](../App/Src/app_led.c)、[Core/Src/freertos.c](../Core/Src/freertos.c)）。
> 读完后你应该能：看懂项目里所有 `osXxx()` 调用，并知道它们底层对应 FreeRTOS 的什么。

---

## 1. 先理清关系：CMSIS-RTOS v2 是什么

本项目用了**两套 API**，但只有一个内核在跑：

```
你的代码 (app_led.c)
      │  调用 osThreadNew / osDelay / osMutexAcquire ...   ← CMSIS-RTOS v2 标准 API
      ▼
cmsis_os2.c        一层薄薄的"翻译"（适配层）
      │  内部调用 xTaskCreate / vTaskDelay / xSemaphoreTake ...
      ▼
FreeRTOS 内核（tasks.c / queue.c / timers.c ...）   ← 真正的调度器
```

- **FreeRTOS** 是真正的 RTOS 内核（[Middlewares/Third_Party/FreeRTOS/](../Middlewares/Third_Party/FreeRTOS/)）。
- **CMSIS-RTOS v2** 是 ARM 定义的**标准 RTOS API 规范**（一组 `osXxx` 函数）。它本身不是内核，只是一个"接口约定"。
- ST 在 [cmsis_os2.c](../Middlewares/Third_Party/FreeRTOS/Source/CMSIS_RTOS_V2/cmsis_os2.c) 里把这组标准 API **翻译成 FreeRTOS 调用**。

**为什么要多这一层？** 可移植性 + 易读性。理论上换一个内核（RT-Thread、Keil RTX 等）只要换适配层，你的 `osXxx` 代码不用改。实际在本项目里，`osXxx` 就是 FreeRTOS 的" nicer 名字"。

> 你会在代码里看到两种 include：
> - `#include "cmsis_os2.h"` —— 新代码推荐用这个（本项目的 [app_led.c](../App/Src/app_led.c)）。
> - `#include "cmsis_os.h"` —— V1 兼容头（CubeMX 生成的 [freertos.c](../Core/Src/freertos.c)/[main.c](../Core/Src/main.c) 用它，内容一样）。

---

## 2. 核心概念对应总表

| 概念 | CMSIS-RTOS v2 | FreeRTOS 原生 | 我们项目里的位置 |
|---|---|---|---|
| 内核初始化 | `osKernelInitialize()` | （启动前手动）| [main.c](../Core/Src/main.c) `main()` |
| 内核启动 | `osKernelStart()` | `vTaskStartScheduler()` | [main.c](../Core/Src/main.c) `main()` |
| 创建任务 | `osThreadNew(fn, arg, attr)` | `xTaskCreate(...)` | [app_led.c](../App/Src/app_led.c) `app_led_init` |
| 当前 tick | `osKernelGetTickCount()` | `xTaskGetTickCount()` | `appLedTask` |
| 相对延时 | `osDelay(ticks)` | `vTaskDelay(ticks)` | [freertos.c](../Core/Src/freertos.c) demo |
| 绝对延时（防漂移）| `osDelayUntil(tick)` | `vTaskDelayUntil(&last, inc)` | `appLedTask` |
| 创建互斥量 | `osMutexNew(attr)` | `xSemaphoreCreateMutex()` | `app_led_init` |
| 加锁 | `osMutexAcquire(m, t)` | `xSemaphoreTake(m, t)` | `appLedTask` / `led_set_state` |
| 解锁 | `osMutexRelease(m)` | `xSemaphoreGive(m)` | 同上 |
| 任务句柄类型 | `osThreadId_t` | `TaskHandle_t` | `s_task` |
| 互斥量句柄类型 | `osMutexId_t` | `SemaphoreHandle_t` | `s_mutex` |
| 永久等待 | `osWaitForever` | `portMAX_DELAY` | `osMutexAcquire` |
| 断言 | （借用）`configASSERT(x)` | `configASSERT(x)` | `led_set_state` |

> **一个 tick 多长？** 由 [FreeRTOSConfig.h](../Core/Inc/FreeRTOSConfig.h) 的 `configTICK_RATE_HZ = 1000` 决定 → **1 tick = 1 ms**。所以 `osDelay(1)` 就是延时 1ms，`osDelay(3000)` 是 3 秒。

---

## 3. 本项目用到的 API 逐个讲

### 3.1 内核：初始化 → 启动（在 [main.c](../Core/Src/main.c)）

```c
osKernelInitialize();   // 创建内核对象前必须先调
MX_FREERTOS_Init();     // 这里我们建互斥量、建 LED 任务（见 3.3/3.5）
osKernelStart();        // 启动调度器，从此任务才开始跑。这行之后不会再返回
```

⚠️ 顺序很重要：**先 `osKernelInitialize`，再创建任务/互斥量，最后 `osKernelStart`**。在 `osKernelStart` 之前，任务对象已经创建但还没运行。

### 3.2 时间：`osDelay` vs `osDelayUntil`（本项目最容易混的两个）

**`osDelay(n)` —— 相对延时："从我调用起，睡 n 个 tick"。**
```c
// freertos.c 里 demo：每 3 秒切一次 LED 状态
osDelay(3000);   // → 底层 vTaskDelay(3000)
```
**缺点**：每次调用都会把"任务本次的工作耗时"也算进去累计，长期跑会**漂移**。

**`osDelayUntil(absolute_tick)` —— 绝对延时："睡到 tick 计数器等于 absolute_tick"。**
```c
// app_led.c 里 10ms 周期任务
uint32_t next = osKernelGetTickCount() + TASK_PERIOD_MS;
for (;;) {
    osDelayUntil(next);       // → 底层 vTaskDelayUntil(&tcnt, next - tcnt)
    next += TASK_PERIOD_MS;   // 关键：next 按固定步长累加，不依赖实际耗时
    ... // 干活
}
```
**为什么这样不漂移？** `next` 永远是"理想的下一个唤醒时刻"。哪怕某一轮干活多花了 2ms，下一轮还是唤醒在 `next`，周期严格 10ms。**周期性任务一律用 `osDelayUntil`。**

> CMSIS 把 FreeRTOS 那个"自己维护 `lastWakeTime` 指针 + 算增量"的麻烦事藏起来了——你只给它绝对时刻。

**`osKernelGetTickCount()`** → 返回当前 tick 计数（开机以来的 ms 数），对应 `xTaskGetTickCount()`。

### 3.3 任务：`osThreadNew` + `osThreadAttr_t`

```c
// app_led.c
static const osThreadAttr_t task_attr = {
    .name       = "appLedTask",
    .stack_size = 256u * 4u,        // 栈大小，单位是【字节】
    .priority   = osPriorityLow,    // 优先级
};
s_task = osThreadNew(appLedTask, NULL, &task_attr);   // → xTaskCreate(...)
configASSERT(s_task != NULL);
```

对应原生 FreeRTOS：
```c
// xTaskCreate(appLedTask, "appLedTask", 256/*字*/, NULL, osPriorityLow, &handle);
```

**三个坑：**
1. **`stack_size` 单位是字节**，而 FreeRTOS `xTaskCreate` 的栈深度单位是**字(word, 4字节)**。CMSIS 内部会 `/4`。所以你写 `256*4=1024` 字节 = FreeRTOS 的 256 字。
2. **优先级数值**：见 3.4。
3. 任务函数签名固定 `void fn(void *arg)`；参数 `arg` 不用就传 `NULL`，函数里 `(void)arg;` 消警告（看 `appLedTask`）。

### 3.4 优先级：数值越大越高

[FreeRTOSConfig.h](../Core/Inc/FreeRTOSConfig.h) 里 `configMAX_PRIORITIES = 56`。CMSIS 的优先级枚举值**直接当 FreeRTOS 优先级用**，两边都是**数字越大优先级越高**：

| CMSIS 枚举 | 值 | |
|---|---|---|
| `osPriorityIdle` | 1 | 空闲任务专用，别用 |
| `osPriorityLow` | 8 | ← 本项目 `appLedTask` |
| `osPriorityBelowNormal` | 16 | |
| `osPriorityNormal` | 24 | ← CubeMX 的 `defaultTask` |
| `osPriorityAboveNormal` | 32 | |
| `osPriorityHigh` | 40 | |
| `osPriorityRealtime` | 48 | 时间关键任务 |

> 本项目优先级从低到高：**Timer 服务任务(2) < appLedTask(8) < defaultTask(24)**。LED 刷新慢点无所谓，所以给最低档。
> 每个级别还有 `+1..+7` 的细分（如 `osPriorityLow3`），用不上就忽略。

### 3.5 互斥量：保护共享数据

多个任务可能同时读写 `s_state[]`，必须加锁。互斥量 = "谁拿到钥匙谁进临界区，别人阻塞等"。

```c
// 创建（app_led_init）
static const osMutexAttr_t mutex_attr = { .name = "led_mtx" };
s_mutex = osMutexNew(&mutex_attr);          // → xSemaphoreCreateMutex()
configASSERT(s_mutex != NULL);

// 写端：led_set_state —— 任意任务可调
osMutexAcquire(s_mutex, osWaitForever);     // → xSemaphoreTake(...,portMAX_DELAY) 拿锁，拿不到死等
s_rt[id].state = state;
s_rt[id].phase = 0u;
osMutexRelease(s_mutex);                    // → xSemaphoreGive(...)  放锁

// 读+写端：appLedTask（10ms 周期）
osMutexAcquire(s_mutex, osWaitForever);
for (int i = 0; i < LED_COUNT; i++) {
    bsp_led_set_brightness(&g_leds[i], compute_level(&s_rt[i]));
    s_rt[i].phase++;
}
osMutexRelease(s_mutex);
```

**关键点：**
- 第二个参数是**超时**：`osWaitForever`（永久等）、`0`（不等待，拿不到立刻返回）、或具体 tick 数。
- 返回值是 `osStatus_t`（`osOK`/`osErrorTimeout`/...）。本例用永久等待，不会超时，所以没检查；正式代码里建议检查。
- **互斥量有"优先级继承"**：低优先级任务持锁时，如果高优先级任务在等这把锁，低优先级会被临时提到和高的一样高，防止"优先级反转"。这是它比二值信号量更适合做"数据保护"的原因。
- **绝不能在 ISR（中断）里调用** `osMutexXxx`。中断里要用 `xSemaphoreTakeFromISR` 这类带 `FromISR` 后缀的原生 API（CMSIS 没封装互斥量的 ISR 版本）。

### 3.6 `configASSERT` —— 调试断言

来自 [FreeRTOSConfig.h:154](../Core/Inc/FreeRTOSConfig.h#L154)：
```c
#define configASSERT( x ) if ((x) == 0) {taskDISABLE_INTERRUPTS(); for( ;; );}
```
`configASSERT(id < LED_COUNT)` —— 如果传了越界的 id，关中断 + 死循环。接调试器一眼就能看到卡在这。注意它用了 `task.h` 里的 `taskDISABLE_INTERRUPTS()`，所以 `app_led.c` 必须 `#include "task.h"`（光 include `FreeRTOS.h` 不够，这是本项目编译时踩过的坑）。

---

## 4. 接下来你会用到的 API（GUI / 进阶必遇）

### 4.1 信号量（Semaphore）—— 同步 / 资源计数

```c
osSemaphoreId_t sem = osSemaphoreNew(max_count, initial_count, &attr); // 创建
osSemaphoreAcquire(sem, osWaitForever);   // 计数-1，为0则等
osSemaphoreRelease(sem);                  // 计数+1，唤醒等待者
```
- **计数信号量**：`max_count>1`，常用于"生产者/消费者"或限定 N 个资源。
- **二值信号量**：`max=1`，常用于"中断通知任务"（ISR 里 `Release`，任务里 `Acquire`）。⚠️ 二值信号量**没有优先级继承**——做数据保护用互斥量，做同步用信号量。

GUI 场景举例：触摸中断产生事件 → `osSemaphoreRelease` → UI 任务 `osSemaphoreAcquire` 醒来刷新。

### 4.2 消息队列（Message Queue）—— 传数据

```c
osMessageQueueId_t q = osMessageQueueNew(msg_count, msg_size, &attr); // 创建
osMessageQueuePut(q, &item, 0, osWaitForever);   // 发一条（0 是优先级，一般忽略）
osMessageQueueGet(q, &item, NULL, osWaitForever); // 收一条（阻塞等待）
```
任务间**搬数据**用这个，比全局变量+信号量安全。LVGL 移植里常用队列把按键/触摸事件喂给 UI 任务。

### 4.3 软件定时器（Software Timer）—— 周期/一次性回调

```c
osTimerId_t t = osTimerNew(callback, osTimerPeriodic, arg, &attr); // 周期型
osTimerStart(t, ticks);    // 每 ticks 触发一次 callback
```
callback 在**定时器服务任务**（优先级=2，见 [FreeRTOSConfig.h](../Core/Inc/FreeRTOSConfig.h) `configTIMER_TASK_PRIORITY`）里跑，**不是你的任务上下文**，所以 callback 里**不能阻塞**、不能调任何会等的 API。

> 我们 LED 任务没用软件定时器，而是 `osDelayUntil` 自己轮询——因为每 10ms 都要做"按相位算亮度"这种连续计算，定时器不合适。定时器更适合"每隔 1 秒采一次温湿度"这种离散事件。

### 4.4 线程标志（Thread Flags）—— 轻量事件通知

```c
osThreadFlagsSet(thread_id, 0x01);              // 给某任务置标志位
uint32_t r = osThreadFlagsWait(0x01, osFlagsWaitAny, osWaitForever); // 等到位
```
比队列轻——只传"事件发生了哪几位"，不传数据。一个任务最多 24 个标志位。

---

## 5. 常见坑汇总

| 坑 | 说明 |
|---|---|
| **tick 单位** | 1 tick = 1ms（本项目）。`osDelay`/`osDelayUntil`/超时参数都是 tick，不是"毫秒"——只是恰好相等。改了 `configTICK_RATE_HZ` 就得重算。 |
| **stack_size 是字节** | CMSIS `osThreadAttr_t.stack_size` 单位字节；FreeRTOS `xTaskCreate` 是字。写 `N*4` 最直观。栈给小了会栈溢出（可开 `configCHECK_FOR_STACK_OVERFLOW`）。 |
| **`osDelayUntil` 是绝对时刻** | 别传成"延时时长"，要传"目标 tick"。配合 `next += period` 做防漂移。 |
| **ISR 里只能用 `FromISR` 版** | 中断里调 `osXxx` 普通版会崩。CMSIS 对部分 API 有 `osXxxFromISR`（如信号量），但互斥量/任务创建等**根本不能在 ISR 里用**。 |
| **互斥量 ≠ 信号量** | 保护共享数据 → 互斥量（有优先级继承）。任务间同步/中断通知 → 二值/计数信号量。 |
| **优先级数值方向** | CMSIS 和 FreeRTOS 都是**大=高**。但别忘了 Timer 服务任务固定在 2、Idle 在 1。 |
| **调度器启动前不能延时/阻塞** | `osKernelStart()` 之前调 `osDelay` 会出错。任务函数里才安全。 |
| **`configASSERT` 需要 `task.h`** | 因为宏里用了 `taskDISABLE_INTERRUPTS()`。include `FreeRTOS.h` 后再 include `task.h`。 |
| **`USER CODE` 区** | CubeMX 重新生成时只保留 `/* USER CODE BEGIN X */...END X */` 之间的内容。我们的改动全在这些区里，重新生成不会丢（前提是 .ioc 也对应改了）。 |

---

## 6. 实战走读：[app_led.c](../App/Src/app_led.c) 全流程

把上面串起来，按运行时序：

1. **`main()`**（[main.c](../Core/Src/main.c)）：`osKernelInitialize()` → `MX_FREERTOS_Init()` → `osKernelStart()`。
2. **`MX_FREERTOS_Init()`**（[freertos.c](../Core/Src/freertos.c)）建了 `defaultTask`、`appTaskLed` 两个空任务；并在 `USER CODE BEGIN RTOS_THREADS` 里调了 **`app_led_init()`**。
3. **`app_led_init()`**（[app_led.c](../App/Src/app_led.c)）：
   - `osMutexNew` → 建互斥量 `s_mutex`。
   - 对每个 LED：初始化运行时状态 → `bsp_led_init()`（一路调到 `bsp_tim3_init` + `HAL_TIM_PWM_Start`，硬件 PWM 开始跑）。
   - `osThreadNew(appLedTask, ...)` → 建 `appLedTask`（优先级 Low，栈 1KB）。
   - 设默认演示：`LED1=BREATH`、`LED0=BLINK_SLOW`。
4. **调度器启动**，三个任务开始跑：
   - `appLedTask`：每 10ms（`osDelayUntil` 防漂移）→ 加锁 → 按 `s_rt[i].state` 用 `compute_level` 算亮度 → 写 PWM 寄存器 → `phase++` → 解锁。
   - `defaultTask`：每 3 秒 `led_set_state(LED0, 下一个状态)` —— 它要拿同一把锁，演示**跨任务线程安全调用**。
5. **`led_set_state(id, state)`**：`configASSERT` 防越界 → GPIO 后端不能呼吸则回退 `LED_ON` → 加锁改 `s_rt[id].state` 并复位 `phase` → 解锁。下一轮 10ms tick `appLedTask` 就会按新状态刷新。

**为什么线程安全？** `s_rt[]` 只在持锁时读写；`appLedTask` 持锁算+写硬件，`led_set_state` 持锁改状态，两者不会交错。`phase` 复位保证状态切换时亮度序列从头开始。

---

## 7. 延伸阅读（都在本仓库里）

- CMSIS API 声明：[cmsis_os2.h](../Middlewares/Third_Party/FreeRTOS/Source/CMSIS_RTOS_V2/cmsis_os2.h)（每个函数都有文档注释，最好的参考）
- 适配层实现（看 `osXxx` 怎么变 `xXxx`）：[cmsis_os2.c](../Middlewares/Third_Party/FreeRTOS/Source/CMSIS_RTOS_V2/cmsis_os2.c)
- FreeRTOS 配置（tick/堆/优先级位数）：[FreeRTOSConfig.h](../Core/Inc/FreeRTOSConfig.h)
- LED 子系统设计：[docs/superpowers/specs/2026-06-29-led-subsystem-design.md](superpowers/specs/2026-06-29-led-subsystem-design.md)
