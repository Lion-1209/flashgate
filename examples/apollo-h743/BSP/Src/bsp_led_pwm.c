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

uint32_t bsp_led_pwm_get_ccr(const bsp_led_t *led)
{
    const led_pwm_hw_t *hw = (const led_pwm_hw_t *)led->hw;
    return __HAL_TIM_GET_COMPARE(hw->htim, hw->channel);
}

const bsp_led_ops_t g_led_pwm_ops = {
    .init           = pwm_init,
    .set_brightness = pwm_set_brightness,
};
