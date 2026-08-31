#ifndef BSP_CONSOLE_H
#define BSP_CONSOLE_H

#include <stdint.h>
#include <stddef.h>

/* USART1 console: PA9 = TX, PA10 = RX, 115200 8N1.
 * Wired to the Apollo board's onboard CH340 USB-serial bridge.
 * On this board the RGB LCD is hardware-mutually-exclusive with the
 * console (LTDC_B1 shares PA10 with USART1_RX), so console wins. */

void bsp_console_init(void);

/* Prints the FLASHGATE-BOOT banner. flashgate's serial monitor anchors
 * its verification regex on this line — do not change the format without
 * updating boards/apollo-h743.yaml. */
void bsp_console_boot_banner(void);

/* Non-blocking: pop up to maxlen received bytes into buf.
 * Returns bytes actually read (0 if ring empty). Probe channel for M2. */
size_t bsp_console_read(uint8_t *buf, size_t maxlen);

#endif /* BSP_CONSOLE_H */
