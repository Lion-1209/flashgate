#ifndef BSP_SIGNATURE_H
#define BSP_SIGNATURE_H

#include <stdint.h>
#include <stdbool.h>

/* flashgate SWD boot signature.
 *
 * The firmware publishes its identity at a FIXED RAM address (SIGRAM, end of
 * AXI SRAM) so the host can read it through the ST-Link alone — no serial
 * cable needed. Layout (little-endian, total 64 bytes):
 *
 *   0x00 u32 magic = 0xF1A5C0DE   (written LAST: its presence means valid)
 *   0x04 u16 layout version = 1
 *   0x06 u16 flags (bit0: console initialized)
 *   0x08 char git[16]              short sha + optional "-dirty"
 *   0x18 char build[24]            ISO-8601 build time
 *   0x30 u32 crc32 over bytes 0x00..0x2F
 *   0x34..0x3F reserved (zero)
 *
 * The host re-computes the CRC and re-reads on mismatch, so a debugger read
 * racing the boot-time writes is retried, not misjudged. */

#define FLASHGATE_SIG_MAGIC   0xF1A5C0DEu
#define FLASHGATE_SIG_ADDR    0x2001FF00u
#define FLASHGATE_SIG_SIZE    64u

/* Fill the signature at the fixed address. Call once at boot, after the
 * console banner (so a hanging banner also withholds the signature — both
 * gates stay consistent). Cheap: one struct copy + CRC32 over 48 bytes. */
void bsp_signature_publish(bool console_ok);

/* CPU's own view of the published signature (debug aid for `sig?`). */
#include <stdint.h>
uint32_t bsp_signature_read_magic(void);

#endif /* BSP_SIGNATURE_H */
