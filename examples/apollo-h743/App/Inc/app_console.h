#ifndef APP_CONSOLE_H
#define APP_CONSOLE_H

/* Serial probe protocol over the flashgate console (USART1).
 * Line-based, host-driven: every command answers exactly one line,
 * `OK <payload>` or `ERR <payload>`. The flashgate probe runner anchors
 * its assertions on these responses — keep the grammar stable.
 *
 *   ping              -> OK pong git=<sha> build=<iso>
 *   led0? / led1?     -> OK led<N> state=<STATE> ccr=<0..1000 | ->
 *   led0 <STATE>      -> OK led<N> state=<STATE>   (pauses the demo cycler)
 *   selftest          -> OK selftest leds=<n> states=BREATH
 *   demo on|off       -> OK demo on|off
 *
 * STATE: OFF | ON | BLINK_SLOW | BLINK_FAST | BREATH */

void app_console_init(void);   /* call from MX_FREERTOS_Init (RTOS_THREADS) */

#endif /* APP_CONSOLE_H */
