#include "app_led.h"
#include "bsp_led.h"
#include "bsp_led_pwm.h"
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
static volatile bool s_demo_enabled = true;   /* defaultTask cycler gate */

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

uint32_t led_get_ccr(led_id_t id)
{
    configASSERT(id < LED_COUNT);
    if (!g_leds[id].has_pwm) {
        return (uint32_t)-1;
    }
    return bsp_led_pwm_get_ccr(&g_leds[id]);
}

void app_led_set_demo_enabled(bool enable)
{
    s_demo_enabled = enable;
}

bool app_led_demo_enabled(void)
{
    return s_demo_enabled;
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
