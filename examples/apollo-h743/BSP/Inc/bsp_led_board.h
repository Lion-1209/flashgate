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
