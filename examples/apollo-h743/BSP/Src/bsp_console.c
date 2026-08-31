#include "bsp_console.h"
#include "main.h"          /* pulls in the HAL via stm32h7xx_hal.h */

#include <stdio.h>

/* Generated at build time by cmake/firmware_identity.cmake (see fw_identity
 * custom target). Falls back to "unknown" when built outside the tree. */
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

#define CONSOLE_BAUDRATE       115200u
#define CONSOLE_IRQ_PRIORITY   5u     /* must be >= configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY */
#define RX_RING_SIZE           256u   /* power of two */

static UART_HandleTypeDef huart1;

/* Single-producer (IRQ) / single-consumer (reader) byte ring. */
static volatile uint16_t rx_head;                 /* written by IRQ only */
static volatile uint16_t rx_tail;                 /* written by reader only */
static uint8_t          rx_ring[RX_RING_SIZE];

static void console_gpio_init(void)
{
    GPIO_InitTypeDef gpio = {0};

    __HAL_RCC_GPIOA_CLK_ENABLE();

    gpio.Pin       = GPIO_PIN_9 | GPIO_PIN_10;
    gpio.Mode      = GPIO_MODE_AF_PP;
    gpio.Pull      = GPIO_PULLUP;                 /* idle-high when jumper off */
    gpio.Speed     = GPIO_SPEED_FREQ_VERY_HIGH;
    gpio.Alternate = GPIO_AF7_USART1;
    HAL_GPIO_Init(GPIOA, &gpio);
}

void bsp_console_init(void)
{
    __HAL_RCC_USART1_CLK_ENABLE();

    console_gpio_init();

    huart1.Instance          = USART1;
    huart1.Init.BaudRate     = CONSOLE_BAUDRATE;
    huart1.Init.WordLength   = UART_WORDLENGTH_8B;
    huart1.Init.StopBits     = UART_STOPBITS_1;
    huart1.Init.Parity       = UART_PARITY_NONE;
    huart1.Init.Mode         = UART_MODE_TX_RX;
    huart1.Init.HwFlowCtl    = UART_HWCONTROL_NONE;
    huart1.Init.OverSampling = UART_OVERSAMPLING_16;
    huart1.Init.OneBitSampling = UART_ONE_BIT_SAMPLE_DISABLE;
    huart1.AdvancedInit.AdvFeatureInit = UART_ADVFEATURE_NO_INIT;
    (void)HAL_UART_Init(&huart1);

    HAL_NVIC_SetPriority(USART1_IRQn, CONSOLE_IRQ_PRIORITY, 0u);
    HAL_NVIC_EnableIRQ(USART1_IRQn);

    /* Reception runs entirely from the IRQ into the ring; no HAL RX state. */
    __HAL_UART_CLEAR_FLAG(&huart1, UART_CLEAR_OREF | UART_CLEAR_FEF | UART_CLEAR_NEF);
    __HAL_UART_ENABLE_IT(&huart1, UART_IT_RXNE);
}

void bsp_console_boot_banner(void)
{
    /* Format is load-bearing: boards/apollo-h743.yaml regex-matches this line. */
    (void)printf("\r\nFLASHGATE-BOOT board=apollo-h743 git=%s build=%s rtos=FreeRTOS\r\n",
                 APP_GIT_SHA, APP_BUILD_ISO);
}

size_t bsp_console_read(uint8_t *buf, size_t maxlen)
{
    size_t n = 0;
    while ((n < maxlen) && (rx_tail != rx_head)) {
        buf[n++] = rx_ring[rx_tail];
        rx_tail  = (uint16_t)((rx_tail + 1u) & (RX_RING_SIZE - 1u));
    }
    return n;
}

/* Retarget: syscalls.c's weak _write calls __io_putchar. Blocking TX is fine
 * for banner/probe traffic; keep lines short so the gate never times out. */
int __io_putchar(int ch)
{
    uint8_t byte = (uint8_t)ch;
    (void)HAL_UART_Transmit(&huart1, &byte, 1u, HAL_MAX_DELAY);
    return ch;
}

void USART1_IRQHandler(void)
{
    uint32_t isr = USART1->ISR;

    if ((isr & USART_ISR_RXNE_RXFNE) != 0u) {
        uint8_t byte = (uint8_t)(USART1->RDR & 0xFFu);
        uint16_t next = (uint16_t)((rx_head + 1u) & (RX_RING_SIZE - 1u));
        if (next != rx_tail) {                     /* drop on overflow, newest wins keep-old */
            rx_ring[rx_head] = byte;
            rx_head = next;
        }
    }

    /* Clear error flags (ORE/FE/NE): an uncleared ORE wedges the IRQ entry. */
    if ((isr & (USART_ISR_ORE | USART_ISR_FE | USART_ISR_NE)) != 0u) {
        USART1->ICR = UART_CLEAR_OREF | UART_CLEAR_FEF | UART_CLEAR_NEF;
    }
}
