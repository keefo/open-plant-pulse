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

// A new firmware counts as good once a link to it is up, so it can be replaced remotely.
// Until then the bootloader goes back to the previous one. Whichever link comes up calls this.
void remote_mark_firmware_good(void);

#if CONFIG_SCALE_LINK_WIFI
// Turns the Wi-Fi radio and web server off or on, and remembers the choice across reboots.
void remote_set_wifi(bool on);
bool remote_wifi_enabled(void);
// The name this scale answers to, so a hub reached over another link can
// offer a way back to the scale's own page.
const char *remote_hostname(void);
// The address Wi-Fi gave it, or "" when it is not on a network. The
// name resolves only where mDNS does; the address resolves anywhere.
const char *remote_sta_ip(void);
#endif

// Seconds between readings, 0 meaning continuous (ten a second). The one
// sampling rate: every link reports at whatever this is set to.
uint32_t remote_interval_seconds(void);

#if CONFIG_SCALE_ZIGBEE
// Writes the Zigbee side's state as a JSON object. Counters rather than a
// bare up/down, because the faults worth catching here are slow ones: a link
// that rejoins every hour, or reports that start failing while the scale
// still looks fine from the web page.
int zigbee_format_status(char *buf, size_t len);
// Seconds between Zigbee reports as things stand, and the floor that may be
// raising it above the sampling rate. A floor of 0 follows sampling exactly.
uint32_t zigbee_report_every(void);
uint32_t zigbee_report_floor(void);
void zigbee_set_report_floor(uint32_t seconds);
#endif

// Rough charge left (0-100) of a single Li-ion cell at this voltage, or -1 if the divider isn't wired.
int remote_batt_pct(float volts);
#define BATT_LOW_PCT 20
// Index of the oldest line still kept.
uint32_t remote_log_oldest(void);
// Copies line i and its time (seconds since boot). Returns false if it's no longer kept.
bool remote_get_log(uint32_t i, char *line, size_t len, uint32_t *secs);
