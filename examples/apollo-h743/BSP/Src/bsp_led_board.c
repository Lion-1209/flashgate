#include "bsp_led_board.h"
#include "bsp_led_pwm.h"

/* PB1 = TIM3_CH4 (LED0), PB0 = TIM3_CH3 (LED1). Both active-low. */
static led_pwm_hw_t s_led0_hw = { .htim = &htim3, .channel = TIM_CHANNEL_4, .active_low = true };
static led_pwm_hw_t s_led1_hw = { .htim = &htim3, .channel = TIM_CHANNEL_3, .active_low = true };

bsp_led_t g_leds[LED_COUNT] = {
    { .ops = &g_led_pwm_ops, .hw = &s_led0_hw, .has_pwm = true },   /* LED0 */
    { .ops = &g_led_pwm_ops, .hw = &s_led1_hw, .has_pwm = true },   /* LED1 */
};
