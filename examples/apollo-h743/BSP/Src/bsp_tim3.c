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
