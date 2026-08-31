#include "app_console.h"
#include "app_led.h"
#include "bsp_console.h"
#include "bsp_signature.h"
#include "main.h"   /* SCB, CMSIS */
#include "bsp_led_board.h"

#include "FreeRTOS.h"
#include "task.h"          /* taskDISABLE_INTERRUPTS, used by configASSERT */
#include "cmsis_os2.h"

#include <stdint.h>
#include <stdio.h>
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

#define CONSOLE_LINE_MAX   64u
#define CONSOLE_TASK_STACK (512u * 4u)
#define CONSOLE_POLL_MS    10u

static const char *state_name(led_state_t s)
{
    switch (s) {
        case LED_OFF:        return "OFF";
        case LED_ON:         return "ON";
        case LED_BLINK_SLOW: return "BLINK_SLOW";
        case LED_BLINK_FAST: return "BLINK_FAST";
        case LED_BREATH:     return "BREATH";
        default:             return "?";
    }
}

static bool parse_state(const char *s, led_state_t *out)
{
    if      (strcasecmp(s, "OFF") == 0)        { *out = LED_OFF;        return true; }
    else if (strcasecmp(s, "ON") == 0)         { *out = LED_ON;         return true; }
    else if (strcasecmp(s, "BLINK_SLOW") == 0) { *out = LED_BLINK_SLOW; return true; }
    else if (strcasecmp(s, "BLINK_FAST") == 0) { *out = LED_BLINK_FAST; return true; }
    else if (strcasecmp(s, "BREATH") == 0)     { *out = LED_BREATH;     return true; }
    return false;
}

/* No libc tokenizer state: single-task use, but stays reentrancy-clean. */
static char *tok(char **cursor)
{
    char *s = *cursor;
    while (*s == ' ') s++;
    if (*s == '\0') return NULL;
    char *end = s;
    while (*end != '\0' && *end != ' ') end++;
    if (*end != '\0') { *end = '\0'; end++; }
    *cursor = end;
    return s;
}

static void cmd_led_query(int id)
{
    if (id < 0 || id >= LED_COUNT) { (void)printf("ERR bad-led\r\n"); return; }
    led_state_t st  = led_get_state((led_id_t)id);
    uint32_t    ccr = led_get_ccr((led_id_t)id);
    if (ccr == (uint32_t)-1) {
        (void)printf("OK led%d state=%s ccr=-\r\n", id, state_name(st));
    } else {
        (void)printf("OK led%d state=%s ccr=%lu\r\n", id, state_name(st), (unsigned long)ccr);
    }
}

static void cmd_led_set(int id, const char *name)
{
    if (id < 0 || id >= LED_COUNT) { (void)printf("ERR bad-led\r\n"); return; }
    led_state_t st;
    if (!parse_state(name, &st)) {
        (void)printf("ERR bad-state name=%s\r\n", name);
        return;
    }
    led_set_state((led_id_t)id, st);
    app_led_set_demo_enabled(false);              /* probe takes over the demo */
    /* Echo the READBACK, not the request: if the setter silently failed,
     * the probe sees it immediately. */
    led_state_t now = led_get_state((led_id_t)id);
    (void)printf("OK led%d state=%s\r\n", id, state_name(now));
}

static void cmd_selftest(void)
{
    int fails = 0;
    app_led_set_demo_enabled(false);
    for (int id = 0; id < LED_COUNT; id++) {
        led_set_state((led_id_t)id, LED_BREATH);
        if (led_get_state((led_id_t)id) != LED_BREATH) {
            fails++;
        }
    }
    if (fails != 0) {
        (void)printf("ERR selftest readback-fails=%d\r\n", fails);
    } else {
        (void)printf("OK selftest leds=%d states=BREATH\r\n", LED_COUNT);
    }
}

static void cmd_demo(const char *arg)
{
    bool on = (strcmp(arg, "on") == 0) || (strcmp(arg, "1") == 0);
    app_led_set_demo_enabled(on);
    (void)printf("OK demo %s\r\n", on ? "on" : "off");
}

static void handle_line(char *line)
{
    char *rest = line;
    char *cmd  = tok(&rest);
    if (cmd == NULL) { return; }

    if (strcmp(cmd, "ping") == 0) {
        (void)printf("OK pong git=%s build=%s\r\n", APP_GIT_SHA, APP_BUILD_ISO);
        return;
    }
    if (strcmp(cmd, "sig?") == 0) {
        (void)printf("OK sig magic=%08lX addr=%08lX ccr=%08lX\r\n",
                     (unsigned long)bsp_signature_read_magic(),
                     (unsigned long)FLASHGATE_SIG_ADDR,
                     (unsigned long)SCB->CCR);
        return;
    }
    if (strcmp(cmd, "selftest") == 0) { cmd_selftest(); return; }
    if (strcmp(cmd, "demo") == 0) {
        char *arg = tok(&rest);
        cmd_demo(arg != NULL ? arg : "off");
        return;
    }

    /* ledN? / ledN <STATE> */
    if (strncmp(cmd, "led", 3) == 0
        && (cmd[3] >= '0') && (cmd[3] < (char)('0' + LED_COUNT))) {
        int id = cmd[3] - '0';
        if (cmd[4] == '?' && cmd[5] == '\0') {
            cmd_led_query(id);
            return;
        }
        if (cmd[4] == '\0') {
            char *arg = tok(&rest);
            if (arg != NULL) { cmd_led_set(id, arg); return; }
        }
    }

    (void)printf("ERR unknown-cmd\r\n");
}

static void console_task(void *arg)
{
    (void)arg;
    static char    line[CONSOLE_LINE_MAX];
    static uint16_t len = 0u;
    uint8_t buf[32];

    for (;;) {
        size_t n = bsp_console_read(buf, sizeof buf);
        for (size_t i = 0u; i < n; i++) {
            char c = (char)buf[i];
            if ((c == '\n') || (c == '\r')) {
                if (len > 0u) {
                    line[len] = '\0';
                    handle_line(line);
                    len = 0u;
                }
            } else if (len < (CONSOLE_LINE_MAX - 1u)) {
                line[len++] = c;
            } else {
                len = 0u;                       /* drop overflowing line */
                (void)printf("ERR line-too-long\r\n");
            }
        }
        if (n == 0u) {
            osDelay(CONSOLE_POLL_MS);
        }
    }
}

void app_console_init(void)
{
    static const osThreadAttr_t attr = {
        .name       = "appConsoleTask",
        .stack_size = CONSOLE_TASK_STACK,
        .priority   = osPriorityLow,
    };
    osThreadId_t task = osThreadNew(console_task, NULL, &attr);
    configASSERT(task != NULL);
}
