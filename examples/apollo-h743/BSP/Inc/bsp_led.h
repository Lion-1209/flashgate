#ifndef BSP_LED_H
#define BSP_LED_H

#include <stdint.h>
#include <stdbool.h>

typedef struct bsp_led bsp_led_t;

/* Backend operations. Each backend (GPIO/PWM) provides one instance.
 * level semantics: 0 = fully dark, 1000 = fully bright. */
typedef struct {
    void (*init)(bsp_led_t *led);
    void (*set_brightness)(bsp_led_t *led, uint16_t level);  /* level: 0..1000 */
} bsp_led_ops_t;

struct bsp_led {
    const bsp_led_ops_t *ops;     /* points to the chosen backend's ops */
    void                *hw;      /* backend-private hardware context */
    bool                 has_pwm; /* capability: true if continuous dimming (BREATH) is supported */
};

void bsp_led_init(bsp_led_t *led);
void bsp_led_set_brightness(bsp_led_t *led, uint16_t level);

#endif /* BSP_LED_H */
