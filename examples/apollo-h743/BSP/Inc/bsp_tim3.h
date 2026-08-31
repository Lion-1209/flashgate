#ifndef BSP_TIM3_H
#define BSP_TIM3_H

#include "stm32h7xx_hal.h"

extern TIM_HandleTypeDef htim3;

/* Initialize TIM3 for LED PWM: PB0 = TIM3_CH3, PB1 = TIM3_CH4 (AF2).
 * ARR=999, PSC=239 -> 1 kHz, 1000 duty steps. Idempotent.
 * Also configures PB0/PB1 as AF2 (in HAL_TIM_PWM_MspInit). */
void bsp_tim3_init(void);

#endif /* BSP_TIM3_H */
