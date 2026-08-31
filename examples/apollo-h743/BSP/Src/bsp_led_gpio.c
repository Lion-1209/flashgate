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
