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
