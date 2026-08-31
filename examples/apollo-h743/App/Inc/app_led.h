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

/* Hardware ground truth: the LED's timer CCR register (PWM backends only).
 * Returns (uint32_t)-1 when this LED has no PWM backend. */
uint32_t    led_get_ccr(led_id_t id);

/* Pause/resume the default demo cycler (defaultTask). Console LED commands
 * pause it so probes stay deterministic; `demo on` resumes. */
void        app_led_set_demo_enabled(bool enable);
bool        app_led_demo_enabled(void);

#endif /* APP_LED_H */
