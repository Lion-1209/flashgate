# flashgate console (USART1)

This firmware doubles as the test fixture for
[flashgate](https://github.com/Lion-1209/flashgate) — the hardware-in-the-loop
verification gate. It boots the normal LED demo and additionally prints one
load-bearing line over UART1.

## Hardware facts (verified against the ATK A-disk sources, experiment 3)

- Console is **USART1 on PA9 (TX) / PA10 (RX)**, 115200 8N1, RX interrupt
  driven into a 256-byte ring in `BSP/Src/bsp_console.c`.
- The board side is bare TTL on the core-board header; the USB side is
  whatever USB-TTL adapter is on the bench (currently a CH340 module).
  Port resolution lives in flashgate, not here.
- **PA10 is hardware-mutually-exclusive with the RGB LCD**: on STM32H743,
  LTDC_B1 lives only on PA10. Console and RGB LCD cannot coexist on this
  board — that is a board/silicon constraint, not a project choice. LTDC was
  therefore removed (it was configured but never used by any app code).

## The banner contract

`bsp_console_boot_banner()` prints, before the scheduler starts:

    FLASHGATE-BOOT board=apollo-h743 git=<sha> build=<ISO8601> rtos=FreeRTOS

flashgate's `boards/apollo-h743.yaml` regex-anchors on this line. **Changing
the format means changing both sides in lockstep.**

`git` and `build` come from `build/Debug/fw_identity.h`, regenerated at
**build time** (not configure time) by `cmake/firmware_identity.cmake`, so the
banner always matches `git rev-parse HEAD` even after committing without a
full reconfigure. Incremental cost: one file recompiles per build.

## CubeMX regeneration note

`Apollo.ioc` was hand-edited (LTDC removed, USART1 Asynchronous added;
no CubeMX on the dev machine). If the project is ever regenerated:

1. CubeMX will emit `Core/Src/usart.c` with a global `huart1` and
   `MX_USART1_UART_Init()` — colliding with `bsp_console.c`'s own instance.
   Migration: drop the BSP-local init and keep the generated one, moving the
   `__io_putchar` retarget, IRQ ring, and banner call into user sections
   (or `App/`).
2. `cmake/stm32cubemx/CMakeLists.txt` may be regenerated: re-add the
   `Console_Subsystem` block, the `fw_identity` dependency, and the
   `MX_Application_Src` adjustments (no `ltdc.c`, UART HAL sources).
3. Root `CMakeLists.txt` is converter-generated "only once" — the identity
   target and objcopy post-build survive regen, but verify after.

## SWD boot signature (evidence channel without a serial cable)

`BSP/bsp_signature.c` publishes a 64-byte identity at the **DTCM tail**
(`0x2001FF00`, reserved by the linker as SIGRAM — DTCMRAM was shrunk by
256 B): magic `0xF1A5C0DE`, layout version, flags, git sha, build time,
CRC32 over the first 48 bytes (the final magic value is PART of the CRC —
a spec bug where it wasn't cost an afternoon once). The host reads it
through the ST-Link alone (`flashgate verify --evidence swd`), wiping the
magic word between flash and start, because **RAM survives reset and a
stale previous-boot signature would otherwise lie**.

Why DTCM and not AXI SRAM: an earlier revision placed the signature at the
end of AXI SRAM (0x2407FF00) and the boot-time stores were visible to the
CPU but never to the debug port (the SoC runs with D-cache enabled even
though nothing in this tree enables it). DTCM is core-local, never cached,
and always debug-visible — one moving part fewer. Do not move the signature
back to AXI without solving that.
