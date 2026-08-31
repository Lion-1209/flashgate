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

/* Hardware ground truth: this LED's current CCR register value (0..1000).
 * flashgate probes read this to prove the PWM engine is really running. */
uint32_t bsp_led_pwm_get_ccr(const bsp_led_t *led);

#endif /* BSP_LED_PWM_H */
