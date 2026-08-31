# LED Indicator Subsystem Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a layered, portable LED indicator subsystem (driver layer + control layer) on the Apollo STM32H743 board, supporting OFF/ON/BLINK_SLOW/BLINK_FAST/BREATH states with hardware-PWM breathing.

**Architecture:** Brightness (0..1000) is the abstraction between a FreeRTOS state-machine task (App layer) and a pluggable backend driver (BSP layer). Backends implement an `ops` struct (function pointers); GPIO quantizes to on/off, PWM maps to TIM3 CCR. The board config table binds LED0/LED1 to PWM backends (TIM3_CH3/CH4). Change of board = change the table only.

**Tech Stack:** STM32H743 HAL, FreeRTOS V10.3.1 via CMSIS-RTOS v2, CMake + Ninja + arm-none-eabi-gcc (STM32CubeIDE bundle).

---

## Verification Model (read first)

This is bare-metal embedded with **no host C compiler** and the toolchain reachable only through the STM32CubeIDE bundle. Therefore:

- **Compilation is the automated gate.** After every task, build via the bundled Ninja (I/agent run this). It catches type errors, signature mismatches, missing symbols — the bulk of integration risk.
- **Behavior is verified on hardware by the user.** Flashing needs the board + ST-Link and is done by the user (via the IDE), who reports observed LED behavior back. The final task is an explicit on-target checkpoint.
- There is no unit-test runner. Pure logic (brightness computation) is written as a single pure function for clarity and future host-testing, but is not executed in isolation here.

### Build setup (Task 1 creates this once)

A helper script `tools/build.sh` resolves the bundled Ninja via `$CUBE_BUNDLE_PATH` and runs the build from the project root:

```bash
bash tools/build.sh        # equivalent to: ninja -C build/Debug
```

All "Run build" steps below mean: from project root, run `bash tools/build.sh`. Expected success output ends with `Linking C executable Apollo.elf` and a memory-region table, with exit code 0.

### Build health baseline (already verified)
- Baseline compiles clean: `Apollo.elf`, FLASH ≈ 29 KB, DTCMRAM ≈ 43 KB.
- TIM3 is free (TIM6 = HAL timebase, SysTick = FreeRTOS). PB0=TIM3_CH3, PB1=TIM3_CH4, both AF2 (`GPIO_AF2_TIM3`).
- Verified hardware: LED0(DS0,red)=PB1, LED1(DS1,green)=PB0, both **active-low**.

---

## File Structure

| File | Responsibility |
|---|---|
| `BSP/Inc/bsp_led.h` | Driver interface: `bsp_led_t`, `bsp_led_ops_t`, `bsp_led_init`, `bsp_led_set_brightness` |
| `BSP/Inc/bsp_tim3.h` | TIM3 PWM peripheral: `htim3`, `bsp_tim3_init` |
| `BSP/Inc/bsp_led_pwm.h` | PWM backend: `led_pwm_hw_t` context type + `g_led_pwm_ops` extern |
| `BSP/Inc/bsp_led_board.h` | `led_id_t` enum + `g_leds[]` extern |
| `BSP/Src/bsp_led.c` | Driver core (forwards to `ops`) |
| `BSP/Src/bsp_led_gpio.c` | GPIO backend ops (dormant — proves portability; not used by this board) |
| `BSP/Src/bsp_tim3.c` | TIM3 time-base + OC config + pin AF (MspInit) |
| `BSP/Src/bsp_led_pwm.c` | PWM backend ops (uses `bsp_tim3`) |
| `BSP/Src/bsp_led_board.c` | Instance table binding LED0/LED1 → PWM backend |
| `App/Inc/app_led.h` | Public API: `led_state_t`, `led_set_state`, `led_get_state`, `app_led_init` |
| `App/Src/app_led.c` | State machine + manager task + mutex + breathing LUT |
| `tools/build.sh` | Terminal build helper (resolves bundled Ninja) |
| Modify `cmake/stm32cubemx/CMakeLists.txt` | Add `LED_Subsystem` OBJECT lib + includes |
| Modify `Core/Src/freertos.c` | Call `app_led_init()` + demo cycling in defaultTask |
| Modify `Core/Src/gpio.c` | Remove PB0/PB1 output config (now TIM3 AF) |

Dependency direction: **App → BSP** (App includes BSP headers; BSP never includes App). `led_id_t` lives in BSP (`bsp_led_board.h`) because it names a physical LED.

---

## Task 1: Build helper + driver core + CMake infrastructure

**Files:**
- Create: `tools/build.sh`
- Create: `BSP/Inc/bsp_led.h`, `BSP/Src/bsp_led.c`
- Modify: `cmake/stm32cubemx/CMakeLists.txt`

- [ ] **Step 1: Create the build helper**

Create `tools/build.sh`:

```bash
#!/usr/bin/env bash
# Build the Apollo firmware from the terminal using the STM32CubeIDE-bundled Ninja.
# Relies on CUBE_BUNDLE_PATH being set (the STM32Cube VSCode extension exports it).
set -e
NINJA=$(ls "$CUBE_BUNDLE_PATH"/ninja/*/bin/ninja.exe 2>/dev/null | head -1)
if [ -z "$NINJA" ]; then
    echo "ERROR: bundled ninja not found under CUBE_BUNDLE_PATH=$CUBE_BUNDLE_PATH" >&2
    exit 1
fi
exec "$NINJA" -C build/Debug "$@"
```

Make it executable (git tracks the bit on commit; local exec bit is set now):

```bash
chmod +x tools/build.sh
```

- [ ] **Step 2: Create the driver interface header**

Create `BSP/Inc/bsp_led.h`:

```c
#ifndef BSP_LED_H
#define BSP_LED_H

#include <stdint.h>
#include <stdbool.h>

typedef struct bsp_led bsp_led_t;

/* Backend operations. Each backend (GPIO/PWM) provides one instance.
 * level semantics: 0 = fully dark, 1000 = fully bright. */
typedef struct {
    void (*init)(bsp_led_t *led);
    void (*set_brightness)(bsp_led_t *led, uint16_t level);  /* level: 0..1000 */
} bsp_led_ops_t;

struct bsp_led {
    const bsp_led_ops_t *ops;     /* points to the chosen backend's ops */
    void                *hw;      /* backend-private hardware context */
    bool                 has_pwm; /* capability: true if continuous dimming (BREATH) is supported */
};

void bsp_led_init(bsp_led_t *led);
void bsp_led_set_brightness(bsp_led_t *led, uint16_t level);

#endif /* BSP_LED_H */
```

- [ ] **Step 3: Create the driver core**

Create `BSP/Src/bsp_led.c`:

```c
#include "bsp_led.h"

void bsp_led_init(bsp_led_t *led)
{
    /* ops is guaranteed non-NULL by the static board table. */
    led->ops->init(led);
}

void bsp_led_set_brightness(bsp_led_t *led, uint16_t level)
{
    if (level > 1000u) {
        level = 1000u;            /* public-API contract: clamp to valid range */
    }
    led->ops->set_brightness(led, level);
}
```

- [ ] **Step 4: Register the LED subsystem in CMake**

In `cmake/stm32cubemx/CMakeLists.txt`, find the line:

```cmake
# Add STM32CubeMX generated application sources to the project
target_sources(${CMAKE_PROJECT_NAME} PRIVATE ${MX_Application_Src})
```

Insert this block **immediately before** that line:

```cmake
# === LED subsystem (BSP driver layer + App control layer) ===
set(LED_Subsystem_Src
    ${CMAKE_SOURCE_DIR}/BSP/Src/bsp_led.c
)
add_library(LED_Subsystem OBJECT)
target_sources(LED_Subsystem PRIVATE ${LED_Subsystem_Src})
target_include_directories(LED_Subsystem PUBLIC
    ${CMAKE_SOURCE_DIR}/BSP/Inc
    ${CMAKE_SOURCE_DIR}/App/Inc
)
target_link_libraries(LED_Subsystem PUBLIC stm32cubemx)

```

Then find the final link line:

```cmake
# Add libraries to the project
target_link_libraries(${CMAKE_PROJECT_NAME} ${MX_LINK_LIBS})
```

Change it to add `LED_Subsystem`:

```cmake
# Add libraries to the project
target_link_libraries(${CMAKE_PROJECT_NAME} ${MX_LINK_LIBS} LED_Subsystem)
```

- [ ] **Step 5: Run build to verify it configures and compiles**

Run: `bash tools/build.sh`
Expected: ninja reconfigures (CMakeLists changed), compiles `bsp_led.c`, links `Apollo.elf`. Exit 0.

- [ ] **Step 6: Commit**

```bash
git add tools/build.sh BSP/Inc/bsp_led.h BSP/Src/bsp_led.c cmake/stm32cubemx/CMakeLists.txt
git commit -m "feat(led): add driver core (brightness abstraction) and build infra"
```

---

## Task 2: GPIO backend

**Files:**
- Create: `BSP/Src/bsp_led_gpio.c`
- Modify: `cmake/stm32cubemx/CMakeLists.txt` (append source)

This backend is **not used by the current board** (LED0/LED1 use PWM). It exists to make the `ops` abstraction non-vacuous and to prove a plain-GPIO LED would work without touching the control layer. It is compiled but unreferenced (the linker drops it).

- [ ] **Step 1: Create the GPIO backend**

Create `BSP/Src/bsp_led_gpio.c`:

```c
#include "bsp_led.h"
#include "stm32h7xx_hal.h"

typedef struct {
    GPIO_TypeDef *port;
    uint16_t      pin;
    bool          active_low;   /* true: pin LOW = LED on */
} led_gpio_hw_t;

static void gpio_init(bsp_led_t *led)
{
    led_gpio_hw_t *hw = (led_gpio_hw_t *)led->hw;
    /* Pin mode/clock are expected to be configured externally (e.g. MX_GPIO_Init). */
    /* Initial state: dark. */
    HAL_GPIO_WritePin(hw->port, hw->pin,
                      hw->active_low ? GPIO_PIN_SET : GPIO_PIN_RESET);
}

static void gpio_set_brightness(bsp_led_t *led, uint16_t level)
{
    led_gpio_hw_t *hw = (led_gpio_hw_t *)led->hw;
    /* Quantize: any non-zero brightness = ON. */
    GPIO_PinState active   = hw->active_low ? GPIO_PIN_RESET : GPIO_PIN_SET;
    GPIO_PinState inactive = hw->active_low ? GPIO_PIN_SET   : GPIO_PIN_RESET;
    HAL_GPIO_WritePin(hw->port, hw->pin, (level == 0u) ? inactive : active);
}

const bsp_led_ops_t g_led_gpio_ops = {
    .init           = gpio_init,
    .set_brightness = gpio_set_brightness,
};
```

- [ ] **Step 2: Register it in CMake**

In `cmake/stm32cubemx/CMakeLists.txt`, after the existing `target_link_libraries(LED_Subsystem PUBLIC stm32cubemx)` line, add:

```cmake
target_sources(LED_Subsystem PRIVATE ${CMAKE_SOURCE_DIR}/BSP/Src/bsp_led_gpio.c)
```

- [ ] **Step 3: Run build**

Run: `bash tools/build.sh`
Expected: compiles `bsp_led_gpio.c`, links `Apollo.elf`. Exit 0.

- [ ] **Step 4: Commit**

```bash
git add BSP/Src/bsp_led_gpio.c cmake/stm32cubemx/CMakeLists.txt
git commit -m "feat(led): add GPIO backend ops"
```

---

## Task 3: TIM3 PWM peripheral driver

**Files:**
- Create: `BSP/Inc/bsp_tim3.h`, `BSP/Src/bsp_tim3.c`
- Modify: `cmake/stm32cubemx/CMakeLists.txt` (append source)

- [ ] **Step 1: Create the TIM3 header**

Create `BSP/Inc/bsp_tim3.h`:

```c
#ifndef BSP_TIM3_H
#define BSP_TIM3_H

#include "stm32h7xx_hal.h"

extern TIM_HandleTypeDef htim3;

/* Initialize TIM3 for LED PWM: PB0 = TIM3_CH3, PB1 = TIM3_CH4 (AF2).
 * ARR=999, PSC=239 -> 1 kHz, 1000 duty steps. Idempotent.
 * Also configures PB0/PB1 as AF2 (in HAL_TIM_PWM_MspInit). */
void bsp_tim3_init(void);

#endif /* BSP_TIM3_H */
```

- [ ] **Step 2: Create the TIM3 driver**

Create `BSP/Src/bsp_tim3.c`:

```c
#include "bsp_tim3.h"
#include <stdbool.h>

TIM_HandleTypeDef htim3;

static bool s_initialized = false;

/* Low-level: enable clocks + configure PB0/PB1 as TIM3 AF2.
 * Called by HAL during HAL_TIM_PWM_Init. */
void HAL_TIM_PWM_MspInit(TIM_HandleTypeDef *htim)
{
    if (htim->Instance != TIM3) {
        return;
    }

    __HAL_RCC_TIM3_CLK_ENABLE();
    __HAL_RCC_GPIOB_CLK_ENABLE();

    GPIO_InitTypeDef g = {0};
    g.Pin       = GPIO_PIN_0 | GPIO_PIN_1;   /* PB0 = TIM3_CH3, PB1 = TIM3_CH4 */
    g.Mode      = GPIO_MODE_AF_PP;
    g.Pull      = GPIO_NOPULL;
    g.Speed     = GPIO_SPEED_FREQ_VERY_HIGH;
    g.Alternate = GPIO_AF2_TIM3;
    HAL_GPIO_Init(GPIOB, &g);
}

void bsp_tim3_init(void)
{
    if (s_initialized) {
        return;
    }

    htim3.Instance               = TIM3;
    htim3.Init.Prescaler         = 239;                       /* 240 MHz / 240 = 1 MHz timer tick */
    htim3.Init.CounterMode       = TIM_COUNTERMODE_UP;
    htim3.Init.Period            = 999;                       /* 1 MHz / 1000 = 1 kHz; 1000 levels */
    htim3.Init.ClockDivision     = TIM_CLOCKDIVISION_DIV1;
    htim3.Init.RepetitionCounter = 0;
    htim3.Init.AutoReloadPreload = TIM_AUTORELOAD_PRELOAD_ENABLE;
    if (HAL_TIM_PWM_Init(&htim3) != HAL_OK) {                 /* triggers MspInit above */
        for (;;) {}                                           /* peripheral misconfig: trap */
    }

    TIM_OC_InitTypeDef oc = {0};
    oc.OCMode     = TIM_OCMODE_PWM1;                          /* active (HIGH) while CNT < CCR */
    oc.Pulse      = 0;                                        /* start dark; backend owns CCR */
    oc.OCPolarity = TIM_OCPOLARITY_HIGH;                      /* active-low handled via CCR in backend */
    oc.OCFastMode = TIM_OCFAST_DISABLE;
    oc.OCNPolarity = TIM_OCNPOLARITY_HIGH;
    oc.OCIdleState = TIM_OCIDLESTATE_RESET;
    oc.OCNIdleState = TIM_OCNIDLESTATE_RESET;
    if (HAL_TIM_PWM_ConfigChannel(&htim3, &oc, TIM_CHANNEL_3) != HAL_OK) { for (;;) {} }
    if (HAL_TIM_PWM_ConfigChannel(&htim3, &oc, TIM_CHANNEL_4) != HAL_OK) { for (;;) {} }

    s_initialized = true;
}
```

- [ ] **Step 3: Register it in CMake**

Append after the GPIO-backend `target_sources` line:

```cmake
target_sources(LED_Subsystem PRIVATE ${CMAKE_SOURCE_DIR}/BSP/Src/bsp_tim3.c)
```

- [ ] **Step 4: Run build**

Run: `bash tools/build.sh`
Expected: compiles `bsp_tim3.c`, links. Exit 0.

- [ ] **Step 5: Commit**

```bash
git add BSP/Inc/bsp_tim3.h BSP/Src/bsp_tim3.c cmake/stm32cubemx/CMakeLists.txt
git commit -m "feat(led): add TIM3 PWM peripheral driver (PB0/PB1, AF2, 1kHz)"
```

---

## Task 4: PWM backend

**Files:**
- Create: `BSP/Inc/bsp_led_pwm.h`, `BSP/Src/bsp_led_pwm.c`
- Modify: `cmake/stm32cubemx/CMakeLists.txt` (append sources)

- [ ] **Step 1: Create the PWM backend header**

Create `BSP/Inc/bsp_led_pwm.h`:

```c
#ifndef BSP_LED_PWM_H
#define BSP_LED_PWM_H

#include <stdbool.h>
#include "bsp_led.h"
#include "bsp_tim3.h"

/* PWM backend hardware context. */
typedef struct {
    TIM_HandleTypeDef *htim;
    uint32_t           channel;     /* TIM_CHANNEL_3 / TIM_CHANNEL_4 */
    bool               active_low;  /* true: LED on when pin LOW */
} led_pwm_hw_t;

extern const bsp_led_ops_t g_led_pwm_ops;

#endif /* BSP_LED_PWM_H */
```

- [ ] **Step 2: Create the PWM backend**

Create `BSP/Src/bsp_led_pwm.c`:

```c
#include "bsp_led_pwm.h"

/* CCR value giving 100% duty in PWM mode 1 = ARR (999) + 1. */
#define LED_PWM_CCR_MAX  1000u

static void pwm_set_brightness(bsp_led_t *led, uint16_t level);

static void pwm_init(bsp_led_t *led)
{
    led_pwm_hw_t *hw = (led_pwm_hw_t *)led->hw;
    bsp_tim3_init();                              /* idempotent: configures TIM3 + pins */
    HAL_TIM_PWM_Start(hw->htim, hw->channel);     /* enable this channel's output */
    pwm_set_brightness(led, 0u);                  /* start dark */
}

static void pwm_set_brightness(bsp_led_t *led, uint16_t level)
{
    led_pwm_hw_t *hw = (led_pwm_hw_t *)led->hw;
    /* With PWM1 + OCPOLARITY_HIGH, pin is HIGH while CNT < CCR.
     * active_low LED: full brightness (level=1000) needs pin always LOW -> CCR=0. */
    uint32_t ccr = hw->active_low ? (LED_PWM_CCR_MAX - level) : (uint32_t)level;
    if (ccr > LED_PWM_CCR_MAX) {
        ccr = LED_PWM_CCR_MAX;
    }
    __HAL_TIM_SET_COMPARE(hw->htim, hw->channel, ccr);
}

const bsp_led_ops_t g_led_pwm_ops = {
    .init           = pwm_init,
    .set_brightness = pwm_set_brightness,
};
```

- [ ] **Step 3: Register it in CMake**

Append:

```cmake
target_sources(LED_Subsystem PRIVATE ${CMAKE_SOURCE_DIR}/BSP/Src/bsp_led_pwm.c)
```

- [ ] **Step 4: Run build**

Run: `bash tools/build.sh`
Expected: compiles, links. Exit 0.

- [ ] **Step 5: Commit**

```bash
git add BSP/Inc/bsp_led_pwm.h BSP/Src/bsp_led_pwm.c cmake/stm32cubemx/CMakeLists.txt
git commit -m "feat(led): add PWM backend ops (brightness -> CCR)"
```

---

## Task 5: Board configuration table

**Files:**
- Create: `BSP/Inc/bsp_led_board.h`, `BSP/Src/bsp_led_board.c`
- Modify: `cmake/stm32cubemx/CMakeLists.txt` (append sources)

- [ ] **Step 1: Create the board header**

Create `BSP/Inc/bsp_led_board.h`:

```c
#ifndef BSP_LED_BOARD_H
#define BSP_LED_BOARD_H

#include "bsp_led.h"

/* Logical LED identifiers (board-specific). */
typedef enum {
    LED0 = 0,   /* DS0, red,   PB1 */
    LED1,       /* DS1, green, PB0 */
    LED_COUNT
} led_id_t;

/* LED instances, indexed by led_id_t. */
extern bsp_led_t g_leds[];

#endif /* BSP_LED_BOARD_H */
```

- [ ] **Step 2: Create the board table**

Create `BSP/Src/bsp_led_board.c`:

```c
#include "bsp_led_board.h"
#include "bsp_led_pwm.h"

/* PB1 = TIM3_CH4 (LED0), PB0 = TIM3_CH3 (LED1). Both active-low. */
static led_pwm_hw_t s_led0_hw = { .htim = &htim3, .channel = TIM_CHANNEL_4, .active_low = true };
static led_pwm_hw_t s_led1_hw = { .htim = &htim3, .channel = TIM_CHANNEL_3, .active_low = true };

bsp_led_t g_leds[LED_COUNT] = {
    { .ops = &g_led_pwm_ops, .hw = &s_led0_hw, .has_pwm = true },   /* LED0 */
    { .ops = &g_led_pwm_ops, .hw = &s_led1_hw, .has_pwm = true },   /* LED1 */
};
```

- [ ] **Step 3: Register it in CMake**

Append:

```cmake
target_sources(LED_Subsystem PRIVATE ${CMAKE_SOURCE_DIR}/BSP/Src/bsp_led_board.c)
```

- [ ] **Step 4: Run build**

Run: `bash tools/build.sh`
Expected: compiles, links. Exit 0.

- [ ] **Step 5: Commit**

```bash
git add BSP/Inc/bsp_led_board.h BSP/Src/bsp_led_board.c cmake/stm32cubemx/CMakeLists.txt
git commit -m "feat(led): add board config table (LED0/LED1 -> PWM backend)"
```

---

## Task 6: Control layer (state machine + manager task)

**Files:**
- Create: `App/Inc/app_led.h`, `App/Src/app_led.c`
- Modify: `cmake/stm32cubemx/CMakeLists.txt` (append source)

- [ ] **Step 1: Create the control-layer header**

Create `App/Inc/app_led.h`:

```c
#ifndef APP_LED_H
#define APP_LED_H

#include "bsp_led_board.h"   /* led_id_t */

/* Semantic indicator states. */
typedef enum {
    LED_OFF = 0,
    LED_ON,
    LED_BLINK_SLOW,   /* ~1 Hz, 50% duty */
    LED_BLINK_FAST,   /* ~4.2 Hz, 50% duty */
    LED_BREATH        /* 2 s raised-sine breathe (needs PWM backend) */
} led_state_t;

/* Initialize hardware, create mutex + manager task, set default demo states.
 * Call once from MX_FREERTOS_Init() (after osKernelInitialize, before scheduler start). */
void        app_led_init(void);

/* Thread-safe: set a LED's state. Safe to call from any task. */
void        led_set_state(led_id_t id, led_state_t state);

/* Thread-safe: read a LED's current state. */
led_state_t led_get_state(led_id_t id);

#endif /* APP_LED_H */
```

- [ ] **Step 2: Create the control layer**

Create `App/Src/app_led.c`:

```c
#include "app_led.h"
#include "bsp_led.h"
#include "FreeRTOS.h"      /* configASSERT */
#include "task.h"          /* taskDISABLE_INTERRUPTS, used by configASSERT */
#include "cmsis_os2.h"

/* ---- Breathing LUT: 50-point raised-sine over 0..1000 (0 -> 1000 -> ~0). ---- */
static const uint16_t s_breath_lut[50] = {
       0,      63,     125,     187,     249,     309,     368,     426,     482,     536,
     588,     637,     685,     729,     771,     809,     844,     876,     905,     930,
     951,     969,     982,     992,     998,    1000,     998,     992,     982,     969,
     951,     930,     905,     876,     844,     809,     771,     729,     685,     637,
     588,     536,     482,     426,     368,     309,     249,     187,     125,      63
};

#define TASK_PERIOD_MS    10u
#define BREATH_STEP_TICKS 4u       /* 50 * 4 * 10 ms = 2000 ms per breath */
#define SLOW_PERIOD       100u     /* ticks: 1 s full cycle -> 500 ms on/off (1 Hz) */
#define SLOW_ON           50u
#define FAST_PERIOD       24u      /* ticks: 240 ms full cycle -> 120 ms on/off (~4.2 Hz) */
#define FAST_ON           12u

typedef struct {
    led_state_t state;
    uint32_t    phase;             /* ticks elapsed since state set */
} led_rt_t;

static led_rt_t     s_rt[LED_COUNT];
static osMutexId_t  s_mutex;
static osThreadId_t s_task;

/* Pure function: map (state, phase) -> brightness level. No I/O. */
static uint16_t compute_level(const led_rt_t *rt)
{
    uint32_t p = rt->phase;
    switch (rt->state) {
        case LED_OFF:        return 0u;
        case LED_ON:         return 1000u;
        case LED_BLINK_SLOW: return (p % SLOW_PERIOD) < SLOW_ON ? 1000u : 0u;
        case LED_BLINK_FAST: return (p % FAST_PERIOD) < FAST_ON ? 1000u : 0u;
        case LED_BREATH:     return s_breath_lut[(p / BREATH_STEP_TICKS) % 50u];
        default:             return 0u;
    }
}

static void appLedTask(void *arg)
{
    (void)arg;

    uint32_t next = osKernelGetTickCount() + TASK_PERIOD_MS;
    for (;;) {
        osDelayUntil(next);          /* anti-drift periodic wake */
        next += TASK_PERIOD_MS;

        osMutexAcquire(s_mutex, osWaitForever);
        for (int i = 0; i < LED_COUNT; i++) {
            bsp_led_set_brightness(&g_leds[i], compute_level(&s_rt[i]));
            s_rt[i].phase++;
        }
        osMutexRelease(s_mutex);
    }
}

void led_set_state(led_id_t id, led_state_t state)
{
    configASSERT(id < LED_COUNT);

    if ((state == LED_BREATH) && !g_leds[id].has_pwm) {
        state = LED_ON;             /* fallback: GPIO backend cannot breathe */
    }

    osMutexAcquire(s_mutex, osWaitForever);
    s_rt[id].state = state;
    s_rt[id].phase = 0u;
    osMutexRelease(s_mutex);
}

led_state_t led_get_state(led_id_t id)
{
    configASSERT(id < LED_COUNT);
    led_state_t s;
    osMutexAcquire(s_mutex, osWaitForever);
    s = s_rt[id].state;
    osMutexRelease(s_mutex);
    return s;
}

void app_led_init(void)
{
    static const osMutexAttr_t mutex_attr = { .name = "led_mtx" };
    s_mutex = osMutexNew(&mutex_attr);
    configASSERT(s_mutex != NULL);

    for (int i = 0; i < LED_COUNT; i++) {
        s_rt[i].state = LED_OFF;
        s_rt[i].phase = 0u;
        bsp_led_init(&g_leds[i]);   /* -> PWM backend -> bsp_tim3_init + PWM start */
    }

    static const osThreadAttr_t task_attr = {
        .name       = "appLedTask",
        .stack_size = 256u * 4u,
        .priority   = osPriorityLow,
    };
    s_task = osThreadNew(appLedTask, NULL, &task_attr);
    configASSERT(s_task != NULL);

    /* Default demo: LED1 breathes steadily; LED0 starts slow-blink (defaultTask cycles it). */
    led_set_state(LED1, LED_BREATH);
    led_set_state(LED0, LED_BLINK_SLOW);
}
```

- [ ] **Step 3: Register it in CMake**

Append:

```cmake
target_sources(LED_Subsystem PRIVATE ${CMAKE_SOURCE_DIR}/App/Src/app_led.c)
```

- [ ] **Step 4: Run build**

Run: `bash tools/build.sh`
Expected: compiles `app_led.c`, links. Exit 0. (Symbols are still unreferenced until Task 7 wires `app_led_init`.)

- [ ] **Step 5: Commit**

```bash
git add App/Inc/app_led.h App/Src/app_led.c cmake/stm32cubemx/CMakeLists.txt
git commit -m "feat(led): add control layer (state machine + 10ms manager task)"
```

---

## Task 7: Integration + on-target verification

**Files:**
- Modify: `Core/Src/freertos.c`
- Modify: `Core/Src/gpio.c`
- Manual: note `Apollo.ioc` for CubeMX regeneration

- [ ] **Step 1: Wire app_led_init into FreeRTOS init**

In `Core/Src/freertos.c`:
1. Add include near the top (after `#include "cmsis_os.h"`):

```c
#include "app_led.h"
```

2. In `MX_FREERTOS_Init()`, inside `/* USER CODE BEGIN RTOS_THREADS */` ... `/* USER CODE END RTOS_THREADS */`, add:

```c
  app_led_init();
```

The section becomes:

```c
  /* USER CODE BEGIN RTOS_THREADS */
  app_led_init();
  /* add threads, ... */
  /* USER CODE END RTOS_THREADS */
```

- [ ] **Step 2: Add the runtime demo to defaultTask**

In `Core/Src/freertos.c`, replace the body of `StartDefaultTask` (inside `/* USER CODE BEGIN StartDefaultTask */` ... `/* USER CODE END StartDefaultTask */`) so it cycles LED0 through all states every 3 s, demonstrating cross-task `led_set_state`:

```c
  static const led_state_t seq[] = { LED_OFF, LED_ON, LED_BLINK_SLOW, LED_BLINK_FAST, LED_BREATH };
  static uint32_t idx = 0;
  for(;;)
  {
    led_set_state(LED0, seq[idx]);
    idx = (idx + 1u) % (sizeof(seq) / sizeof(seq[0]));
    osDelay(3000);
  }
```

(LED1 keeps breathing from `app_led_init`.)

- [ ] **Step 3: Remove PB0/PB1 GPIO output config (now owned by TIM3 AF)**

In `Core/Src/gpio.c` (`MX_GPIO_Init`), the only GPIO configuration is PB0/PB1 (the other pin comments are RCC/SYS pins configured elsewhere). Remove all of the PB0/PB1 config **and** the now-unused `GPIO_InitStruct` declaration. After the edit, `MX_GPIO_Init` should read exactly:

```c
void MX_GPIO_Init(void)
{

  /* GPIO Ports Clock Enable */
  __HAL_RCC_GPIOE_CLK_ENABLE();
  __HAL_RCC_GPIOC_CLK_ENABLE();
  __HAL_RCC_GPIOI_CLK_ENABLE();
  __HAL_RCC_GPIOF_CLK_ENABLE();
  __HAL_RCC_GPIOH_CLK_ENABLE();
  __HAL_RCC_GPIOB_CLK_ENABLE();
  __HAL_RCC_GPIOG_CLK_ENABLE();
  __HAL_RCC_GPIOA_CLK_ENABLE();

}
```

(Keep the GPIOB clock enable — `HAL_TIM_PWM_MspInit` relies on `__HAL_RCC_GPIOB_CLK_ENABLE()` but a duplicate enable is harmless and idempotent.) PB0/PB1 are now configured as AF2 by `HAL_TIM_PWM_MspInit`.

- [ ] **Step 4: Note the .ioc for future CubeMX regeneration**

Open `Apollo.ioc` and, for PB0 and PB1, change the signal from `GPIO_Output` to `TIM3_CH3` / `TIM3_CH4` respectively (so a future CubeMX regeneration won't reintroduce the GPIO config). This is a manual edit in the CubeMX pinout view; if you regenerate, re-apply Task 3's TIM3 settings or enable TIM3 in CubeMX to match.

(If you do not regenerate, this step is informational and can be skipped — the hand-written driver is the source of truth.)

- [ ] **Step 5: Run build (final firmware)**

Run: `bash tools/build.sh`
Expected: full clean link of `Apollo.elf`. Check the memory map: FLASH grows modestly (LED code + TIM3), still well under 2 MB. Exit 0.

- [ ] **Step 6: Commit**

```bash
git add Core/Src/freertos.c Core/Src/gpio.c
git commit -m "feat(led): wire app_led into FreeRTOS, demo cycle, free PB0/PB1 for TIM3"
```

- [ ] **Step 7: ON-TARGET VERIFICATION (user)**

This step requires the physical board and is performed by the user:

1. Build produces `Apollo.elf` (done in Step 5).
2. Flash `Apollo.elf` to the board via STM32CubeIDE (Run/Debug, ST-Link).
3. Observe after power-on:
   - **LED1 (green, PB0)**: smooth breathing (2 s rise/fall cycle).
   - **LED0 (red, PB1)**: cycles every 3 s through OFF → steady ON → slow blink (~1 Hz) → fast blink (~4.2 Hz) → breathing, then repeats.
4. Report results:
   - If a LED is dark when it should be lit (or always-on), the polarity assumption is inverted → set `.active_low = false` in `BSP/Src/bsp_led_board.c` for that LED, rebuild, reflash.
   - If breathing is choppy, confirm the LUT and `BREATH_STEP_TICKS` (Task 6).
5. Optional SWD check: in the debugger, watch `s_rt[0].state` / `s_rt[1].state` and `TIM3->CCR3` / `TIM3->CCR4` advance as expected.

**On-target gate passes when:** both LEDs behave as described in (3). This completes the feature.

---

## Verification Summary

- **Compile gate (automated, every task):** `bash tools/build.sh` exits 0.
- **Behavior gate (user, Task 7 Step 7):** LED1 breathes, LED0 cycles states.
- **No host unit tests** — by environment constraint (no host C compiler). The pure logic is isolated in `compute_level()` for future host-testing if a compiler is added.
