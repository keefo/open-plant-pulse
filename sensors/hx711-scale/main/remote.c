#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "esp_timer.h"
#include "remote.h"

#define LOG_LINES 40

static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static scale_reading_t s_reading;
static uint32_t s_reading_seq;   // 0 = no reading yet

static char s_log[LOG_LINES][REMOTE_LOG_LEN];
static uint32_t s_log_time[LOG_LINES];  // seconds since boot
static uint32_t s_log_seq;              // lines logged since boot

static QueueHandle_t s_cmds;

void remote_log(const char *fmt, ...)
{
    char line[REMOTE_LOG_LEN];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    printf("%s\n", line);

    portENTER_CRITICAL(&s_lock);
    int i = s_log_seq % LOG_LINES;
    strcpy(s_log[i], line);
    s_log_time[i] = esp_timer_get_time() / 1000000;
    s_log_seq++;
    portEXIT_CRITICAL(&s_lock);
}

void remote_publish(const scale_reading_t *r)
{
    portENTER_CRITICAL(&s_lock);
    s_reading = *r;
    s_reading.at_us = esp_timer_get_time();
    s_reading_seq++;
    portEXIT_CRITICAL(&s_lock);
}

void remote_init(void)
{
    s_cmds = xQueueCreate(4, REMOTE_CMD_LEN);
}

bool remote_send_command(const char *cmd)
{
    char buf[REMOTE_CMD_LEN];
    strlcpy(buf, cmd, sizeof buf);
    return xQueueSend(s_cmds, buf, 0) == pdTRUE;
}

bool remote_take_command(char *buf, size_t len)
{
    return remote_wait_command(buf, len, 0);
}

bool remote_wait_command(char *buf, size_t len, uint32_t timeout_ms)
{
    char cmd[REMOTE_CMD_LEN];
    if (xQueueReceive(s_cmds, cmd, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) return false;
    strlcpy(buf, cmd, len);
    return true;
}

uint32_t remote_get_reading(scale_reading_t *out)
{
    portENTER_CRITICAL(&s_lock);
    *out = s_reading;
    uint32_t seq = s_reading_seq;
    portEXIT_CRITICAL(&s_lock);
    return seq;
}

uint32_t remote_log_count(void)
{
    portENTER_CRITICAL(&s_lock);
    uint32_t n = s_log_seq;
    portEXIT_CRITICAL(&s_lock);
    return n;
}

uint32_t remote_log_oldest(void)
{
    uint32_t n = remote_log_count();
    return n > LOG_LINES ? n - LOG_LINES : 0;
}

bool remote_get_log(uint32_t i, char *line, size_t len, uint32_t *secs)
{
    bool ok;
    portENTER_CRITICAL(&s_lock);
    ok = i < s_log_seq && s_log_seq - i <= LOG_LINES;
    if (ok) {
        strlcpy(line, s_log[i % LOG_LINES], len);
        *secs = s_log_time[i % LOG_LINES];
    }
    portEXIT_CRITICAL(&s_lock);
    return ok;
}

int remote_batt_pct(float v)
{
    if (v < 1.0f) return -1;
    // Resting voltage of an 18650 against charge left. Reads high while charging.
    static const float volts[] = { 3.30f, 3.45f, 3.60f, 3.68f, 3.74f, 3.79f, 3.84f, 3.90f, 3.97f, 4.05f, 4.15f };
    if (v <= volts[0]) return 0;
    for (int i = 1; i < 11; i++)
        if (v < volts[i]) return (int)((i - 1 + (v - volts[i - 1]) / (volts[i] - volts[i - 1])) * 10 + 0.5f);
    return 100;
}

int remote_format_status(char *buf, size_t len)
{
    scale_reading_t r;
    uint32_t seq = remote_get_reading(&r);
    int n = snprintf(buf, len,
                     "{\"seq\":%lu,\"ok\":%s,\"sat\":%s,\"stable\":%s,\"step\":%g,\"raw\":%ld,\"delta\":%ld,\"factor\":%.3f,\"batt\":%.2f,\"batt_pct\":%d,\"up\":%lu,\"interval\":%lu,\"age\":%lu,",
                     (unsigned long)seq, r.ok ? "true" : "false", r.saturated ? "true" : "false",
                     r.stable ? "true" : "false", r.step,
                     (long)r.raw, (long)r.delta, r.factor, r.batt, remote_batt_pct(r.batt),
                     (unsigned long)(esp_timer_get_time() / 1000000), (unsigned long)r.interval_s,
                     (unsigned long)(seq ? (esp_timer_get_time() - r.at_us) / 1000000 : 0));
    if (n < (int)len) {
        if (r.factor != 0) n += snprintf(buf + n, len - n, "\"grams\":%.1f}", r.grams);
        else n += snprintf(buf + n, len - n, "\"grams\":null}");
    }
    return n < (int)len ? n : (int)len - 1;
}
