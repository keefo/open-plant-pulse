#pragma once
// Talking to the scale from another device. remote.c holds the shared state (latest reading,
// message log, command queue); web.c (Wi-Fi), ble.c (Bluetooth) or usb.c (serial) carries it,
// picked in menuconfig.
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define REMOTE_LOG_LEN  160
#define REMOTE_CMD_LEN  32

typedef struct {
    int32_t raw, delta;
    float grams, factor, batt;
    float step;      // display step in grams, 0 = not calibrated
    bool ok;         // last HX711 read succeeded
    bool saturated;  // reading pinned at the ADC limit
    bool stable;     // reading has settled
    uint32_t interval_s;  // seconds between readings, 0 = continuous (10 per second)
    int64_t at_us;        // when it was taken (esp_timer time), set by remote_publish
} scale_reading_t;

// ---- for the scale loop ----
// Call first, before anything sends commands.
void remote_init(void);
// Starts the Wi-Fi or Bluetooth side.
void remote_start(void);
// Hands over the latest reading.
void remote_publish(const scale_reading_t *r);
// printf to serial, and keep a copy for the remote message log.
void remote_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
// Next command from a remote device or button ("t", "r" or grams). Returns false if none.
bool remote_take_command(char *buf, size_t len);
// Like remote_take_command, but waits up to timeout_ms for one.
bool remote_wait_command(char *buf, size_t len, uint32_t timeout_ms);

// ---- for web.c / ble.c ----
// Queues a command for the scale loop. Returns false if the queue is full.
bool remote_send_command(const char *cmd);
// Copies the latest reading. Returns its sequence number (0 = no reading yet).
uint32_t remote_get_reading(scale_reading_t *out);
// Number of lines logged since boot.
uint32_t remote_log_count(void);
// Writes the latest reading as one line of JSON. Returns its length.
int remote_format_status(char *buf, size_t len);

// Rough charge left (0-100) of a single Li-ion cell at this voltage, or -1 if the divider isn't wired.
int remote_batt_pct(float volts);
#define BATT_LOW_PCT 20
// Index of the oldest line still kept.
uint32_t remote_log_oldest(void);
// Copies line i and its time (seconds since boot). Returns false if it's no longer kept.
bool remote_get_log(uint32_t i, char *line, size_t len, uint32_t *secs);
