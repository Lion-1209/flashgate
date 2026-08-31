#include "bsp_signature.h"
#include "main.h"   /* SCB cache maintenance, CMSIS */

#include <string.h>

#if defined(__has_include)
#  if __has_include("fw_identity.h")
#    include "fw_identity.h"
#  endif
#endif
#ifndef APP_GIT_SHA
#define APP_GIT_SHA "unknown"
#endif
#ifndef APP_BUILD_ISO
#define APP_BUILD_ISO "unknown"
#endif

typedef struct {
    uint32_t magic;
    uint16_t version;
    uint16_t flags;
    char     git[16];
    char     build[24];
    uint32_t crc32;       /* over the first 48 bytes of this struct */
    uint8_t  reserved[12];
} flashgate_sig_t;

/* Placed by the linker into SIGRAM (NOLOAD, fixed address FLASHGATE_SIG_ADDR). */
static volatile flashgate_sig_t g_flashgate_sig
    __attribute__((section(".flashgate_sig"), used));

static uint32_t sig_crc32(const volatile uint8_t *data, uint32_t len)
{
    /* Table-less CRC-32 (poly 0x04C11DB7, init 0xFFFFFFFF, final xor) —
     * same standard the host tool implements. */
    uint32_t crc = 0xFFFFFFFFu;
    for (uint32_t i = 0; i < len; i++) {
        crc ^= (uint32_t)data[i];
        for (uint32_t b = 0; b < 8u; b++) {
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }
    return crc ^ 0xFFFFFFFFu;
}

void bsp_signature_publish(bool console_ok)
{
    flashgate_sig_t sig;
    memset(&sig, 0, sizeof sig);

    sig.magic   = FLASHGATE_SIG_MAGIC;   /* the final magic value is part of the CRC */
    sig.version = 1u;
    sig.flags   = console_ok ? 1u : 0u;
    strncpy(sig.git, APP_GIT_SHA, sizeof sig.git - 1u);
    strncpy(sig.build, APP_BUILD_ISO, sizeof sig.build - 1u);
    sig.crc32 = sig_crc32((const uint8_t *)&sig, 48u);

    /* Publish: whole payload with magic=0 first, then the magic word last.
     * A reader that sees the magic AND a valid CRC saw a complete signature. */
    memcpy((void *)&g_flashgate_sig, &sig, sizeof sig);

    __asm volatile ("dsb" ::: "memory");
    g_flashgate_sig.magic = FLASHGATE_SIG_MAGIC;
}

uint32_t bsp_signature_read_magic(void)
{
    return g_flashgate_sig.magic;
}
