// HX711 scale on ESP32-C6 (or the original ESP32-WROVER)
//   ESP32-C6: HX711 DT -> GPIO1, SCK -> GPIO2, battery divider -> GPIO0
//   ESP32:    HX711 DT -> GPIO32, SCK -> GPIO33, battery divider -> GPIO34
//   HX711 VCC -> 3V3. Battery divider (1M/1M) midpoint is optional.
//
// Remote control (picked in menuconfig, "HX711 scale"):
//   Wi-Fi:     join "HX711-Scale" (password scale1234), open http://192.168.4.1
//   Bluetooth: open mac/scale.html in Chrome and press Bluetooth
//   USB only:  open mac/scale.html in Chrome and press USB
//
// Commands (buttons on the page, or type in the serial monitor and press Enter):
//   t        tare (zero) with the scale empty
//   <grams>  calibrate: put a known weight on, type its weight, e.g. 1000
//   r        reset calibration
//   i <s>    take a reading every <s> seconds (1 to 43200 = 12 h) and sleep in between; i 0 = 10 per second
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#else
#include "driver/uart.h"
#endif
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_system.h"
#include "esp_app_desc.h"
#include "esp_timer.h"
#include "filter.h"
#include "hx711.h"
#include "remote.h"

#if CONFIG_IDF_TARGET_ESP32C6
#define PIN_DT   GPIO_NUM_1
#define PIN_SCK  GPIO_NUM_2
#define BATT_CH  ADC_CHANNEL_0   // GPIO0
#else
#define PIN_DT   GPIO_NUM_32
#define PIN_SCK  GPIO_NUM_33
#define BATT_CH  ADC_CHANNEL_6   // GPIO34
#endif
#define STEP_MIN_COUNTS 50        // a display step must be at least this many counts
#define ZERO_TRACK_RATE 0.1f      // share of the remaining zero drift corrected per stable sample
#define BURST_N 8                 // samples per reading when sampling on an interval
#define INTERVAL_MAX_S 43200  // 12 hours

static hx711_t s_hx = { .dout = PIN_DT, .sck = PIN_SCK };
static int32_t s_offset = 0;
static uint32_t s_logged_minute = 0;
static float   s_factor = 0;     // raw counts per gram, 0 = not calibrated
static float   s_shown = NAN;    // grams on display; NAN forces the next value through
static filter_t s_flt;           // smoothing for every sample
static bool    s_have_offset;    // a zero was saved by an earlier tare
static uint32_t s_interval_s;    // seconds between readings, 0 = continuous (10 per second)
static bool    s_sample_now;     // take a reading straight away (after a command)
static bool    s_hx_awake = true;

static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_cali;
static bool s_cali_ok;

// ---------- persistence ----------
static void save_cal(void)
{
    nvs_handle_t h;
    if (nvs_open("scale", NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_i32(h, "offset", s_offset);
    nvs_set_blob(h, "factor", &s_factor, sizeof s_factor);
    esp_err_t cerr = nvs_commit(h);
    if (cerr != ESP_OK)
        remote_log("!!! Calibration not saved: %s", esp_err_to_name(cerr));
    nvs_close(h);
}

static void load_cal(void)
{
    nvs_handle_t h;
    if (nvs_open("scale", NVS_READONLY, &h) != ESP_OK) return;
    size_t len = sizeof s_factor;
    nvs_get_blob(h, "factor", &s_factor, &len);
    s_have_offset = nvs_get_i32(h, "offset", &s_offset) == ESP_OK;
    esp_err_t got = nvs_get_u32(h, "interval", &s_interval_s);
    if (got != ESP_OK || s_interval_s > INTERVAL_MAX_S) {
        // Worth saying out loud: a rate that quietly falls back to continuous
        // sampling drains a battery, and the reason is otherwise invisible.
        remote_log("Sampling interval not restored (%s); falling back to continuous",
                   got != ESP_OK ? esp_err_to_name(got) : "stored value out of range");
        s_interval_s = 0;
    }
    nvs_close(h);
}

uint32_t remote_interval_seconds(void)
{
    return s_interval_s;
}

static void save_interval(void)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open("scale", NVS_READWRITE, &h);
    if (err != ESP_OK) {
        remote_log("!!! Could not save the sampling interval: %s", esp_err_to_name(err));
        return;
    }
    // Checked, not assumed. Zigbee keeps its network in this same partition,
    // and a write that quietly fails leaves the running value right and the
    // saved one stale, so the setting comes back wrong after a restart.
    err = nvs_set_u32(h, "interval", s_interval_s);
    if (err == ESP_OK) err = nvs_commit(h);
    if (err != ESP_OK)
        remote_log("!!! Sampling interval not saved: %s. It will revert when the scale restarts.",
                   esp_err_to_name(err));
    nvs_close(h);
}

// ---------- HX711 power ----------
// Between interval readings the HX711 is powered down (about 1 uA instead of 1.5 mA).
static bool hx_wake(void)
{
    if (s_hx_awake) return true;
    hx711_power_up(&s_hx);
    s_hx_awake = true;
    int32_t v;
    return hx711_read_raw(&s_hx, &v, 1000);  // the first conversion after waking is still settling
}

static void hx_sleep(void)
{
    if (!s_hx_awake) return;
    hx711_power_down(&s_hx);
    s_hx_awake = false;
}


/* A short history of how this board has started, kept in NVS.
 *
 * esp_reset_reason() only describes the most recent reset, and the first
 * thing anybody does to a mute board is reset it -- which destroys the
 * answer. Four entries survive that, so the question "what happened last
 * night" can still be asked tomorrow.
 *
 * Each entry is the reason plus how long the previous run lasted, because
 * "panic after 9 hours" and "panic after 20 seconds" are different faults.
 */
#define BOOT_LOG_ENTRIES 4

static void boot_log_record(void)
{
    nvs_handle_t h;
    if (nvs_open("scale", NVS_READWRITE, &h) != ESP_OK) return;

    uint8_t next = 0;
    nvs_get_u8(h, "bootnext", &next);
    uint32_t ran_for = 0;
    nvs_get_u32(h, "ranfor", &ran_for);        /* written as we go */

    char key[16];
    snprintf(key, sizeof key, "boot%u", (unsigned)(next % BOOT_LOG_ENTRIES));
    uint32_t packed = ((uint32_t)esp_reset_reason() << 24) | (ran_for & 0x00FFFFFF);
    nvs_set_u32(h, key, packed);
    nvs_set_u8(h, "bootnext", (uint8_t)((next + 1) % BOOT_LOG_ENTRIES));
    nvs_set_u32(h, "ranfor", 0);
    nvs_commit(h);
    nvs_close(h);
}

/* Called once a minute, so the next boot knows how long this run lasted. */
void boot_log_still_running(uint32_t seconds)
{
    nvs_handle_t h;
    if (nvs_open("scale", NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_u32(h, "ranfor", seconds);
    nvs_commit(h);
    nvs_close(h);
}

int boot_log_format(char *buf, size_t len)
{
    static const char *reasons[] = {
        "unknown", "power on", "external", "software", "panic",
        "interrupt watchdog", "task watchdog", "other watchdog",
        "deep sleep", "brownout", "sdio", "usb", "jtag",
    };
    nvs_handle_t h;
    if (nvs_open("scale", NVS_READONLY, &h) != ESP_OK) return 0;
    int n = 0;
    for (int i = 0; i < BOOT_LOG_ENTRIES; i++) {
        char key[16];
        snprintf(key, sizeof key, "boot%d", i);
        uint32_t packed = 0;
        if (nvs_get_u32(h, key, &packed) != ESP_OK) continue;
        unsigned reason = packed >> 24;
        unsigned ran = packed & 0x00FFFFFF;
        n += snprintf(buf + n, len > (size_t)n ? len - n : 0,
                      "%s{\"reason\":\"%s\",\"ran_for_s\":%u}",
                      n ? "," : "",
                      reason < (sizeof reasons / sizeof reasons[0])
                          ? reasons[reason] : "?", ran);
    }
    nvs_close(h);
    return n;
}

// ---------- battery ----------
static void batt_init(void)
{
    adc_oneshot_unit_init_cfg_t ucfg = { .unit_id = ADC_UNIT_1 };
    adc_oneshot_new_unit(&ucfg, &s_adc);
    adc_oneshot_chan_cfg_t ccfg = { .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_DEFAULT };
    adc_oneshot_config_channel(s_adc, BATT_CH, &ccfg);
#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    adc_cali_curve_fitting_config_t cal = {
        .unit_id = ADC_UNIT_1, .chan = BATT_CH, .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    s_cali_ok = adc_cali_create_scheme_curve_fitting(&cal, &s_cali) == ESP_OK;
#else
    adc_cali_line_fitting_config_t cal = {
        .unit_id = ADC_UNIT_1, .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    s_cali_ok = adc_cali_create_scheme_line_fitting(&cal, &s_cali) == ESP_OK;
#endif
}

static float batt_volts(void)
{
    int sum = 0;
    for (int i = 0; i < 8; i++) {
        int mv = 0, raw = 0;
        adc_oneshot_read(s_adc, BATT_CH, &raw);
        if (s_cali_ok) adc_cali_raw_to_voltage(s_cali, raw, &mv);
        else mv = raw * 3300 / 4095;
        sum += mv;
    }
    return (sum / 8) * 2 / 1000.0f;  // x2 for the 1M/1M divider
}

// ---------- display ----------
// Smallest round step (in grams) that is still well clear of this cell's noise.
//
// STEP_MIN_COUNTS is a floor, not an answer: it assumed a quieter cell than
// the one fitted here, which wanders about a gram and a half while the
// floor allows a one gram step. Where the wander can be measured it is used
// instead.
//
// The measurement only means something at the full sample rate. A scale set
// to read once a minute fills the drift window in half an hour, so until it
// does this falls back to the floor, which is the behaviour it always had.
static float display_step(void)
{
    static const float steps[] = { 0.1f, 0.2f, 0.5f, 1, 2, 5, 10 };
    if (s_factor == 0) return 0;
    float floor_counts = STEP_MIN_COUNTS;
    float measured = filter_drift(&s_flt);
    if (measured > floor_counts) floor_counts = measured * 1.2f;
    for (size_t i = 0; i < sizeof steps / sizeof steps[0]; i++)
        if (steps[i] * fabsf(s_factor) >= floor_counts) return steps[i];
    return 10;
}

// Rounds to the step, and only moves when the reading changes by more than 0.6 of a step,
// so the last digit doesn't flicker between two values.
static float shown_grams(float grams, float step)
{
    if (isnan(s_shown) || fabsf(grams - s_shown) > 0.6f * step)
        s_shown = roundf(grams / step) * step;

    // An empty scale reads zero, not minus one.
    //
    // This band was half a step, which is the width that stops -0.0 being
    // printed and nothing more. The reading wanders by about a step and a
    // half on this cell, so an empty pan sat flipping between -1, 0 and 1
    // forever -- numbers that say nothing except that the scale is on.
    //
    // A step and a half, deliberately in units of the step rather than of
    // measured noise: the noise estimate is sampled per reading, so on a
    // scale that sleeps between readings it needs minutes to mean anything,
    // and a zero band that depends on how often somebody is looking is
    // worse than one that is simply wide enough.
    if (fabsf(grams) < 1.5f * step) s_shown = 0;
    return s_shown;
}

// ---------- BOOT button ----------
// No button tares this scale.
//
// BOOT used to, and it is the wrong button for a destructive, saved
// action: it sits next to RESET, it is what anybody reaches for when a
// board stops responding, and a tare under a fifteen kilo plant writes
// a new zero to NVS that survives the reboot they were actually trying
// to perform. That happened: a hung scale, a press of BOOT hoping for a
// reset, and a plant that then weighed 287 g.
//
// Taring is still available over serial ("t"), over Zigbee and from the
// hub, all of which can say what they are about to do first.

// ---------- serial commands ----------
static void handle_line(char *line)
{
    while (*line && isspace((unsigned char)*line)) line++;
    if (!*line) return;
    s_shown = NAN;  // tare and calibration change the reading: show the new value at once
    s_sample_now = true;
    if ((line[0] == 't' || isdigit((unsigned char)line[0])) && !hx_wake()) {
        remote_log("!!! HX711 read timed out (DT stuck high?)");
        return;
    }

    if (line[0] == 't') {
        int32_t v;
        if (s_interval_s == 0 && s_flt.stable) {
            // Already settled: the smoothed value is a good zero, so tare at once.
            s_offset = lroundf(s_flt.value);
            save_cal();
            remote_log(">>> Tared. Offset = %ld", (long)s_offset);
        } else if (hx711_read_avg(&s_hx, 5, &v)) {
            s_offset = v;
            save_cal();
            remote_log(">>> Tared. Offset = %ld", (long)s_offset);
        } else {
            remote_log("!!! Tare failed: HX711 read timed out");
        }
    } else if (line[0] == '=') {
        // "= <grams>": what is on the scale right now weighs this much.
        //
        // Moves the zero, not the calibration. Taring needs an empty pan,
        // and emptying it means lifting a plant that can weigh fifteen
        // kilos -- which is how a zero gets lost in the first place and is
        // a poor way to recover one. Calibration ("<grams>") is the other
        // operation and changes the counts-per-gram instead; using it here
        // would fix today's number by corrupting every future one.
        float grams = strtof(line + 1, NULL);
        int32_t v;
        if (s_factor == 0) {
            remote_log("!!! Not calibrated, so there is no weight to set");
        } else if (hx711_read_avg(&s_hx, 5, &v)) {
            s_offset = v - lroundf(grams * s_factor);
            save_cal();
            s_shown = NAN;
            remote_log(">>> Set to %.1f g. Offset = %ld (factor unchanged)",
                       grams, (long)s_offset);
        } else {
            remote_log("!!! Could not set the weight: HX711 read timed out");
        }
    } else if (line[0] == 'j') {
#if CONFIG_SCALE_ZIGBEE
        void zigbee_rejoin(void);
        zigbee_rejoin();
#else
        remote_log("!!! This build has no Zigbee");
#endif
    } else if (line[0] == 'r') {
        s_factor = 0;
        save_cal();
        remote_log(">>> Calibration cleared");
    } else if (line[0] == 'i') {
        char *end;
        long secs = strtol(line + 1, &end, 10);
        if (end == line + 1 || secs < 0 || secs > INTERVAL_MAX_S) {
            remote_log("!!! Sampling interval must be 0 (10 per second) or 1 to %d seconds", INTERVAL_MAX_S);
            return;
        }
        s_interval_s = secs;
        save_interval();
        filter_reset(&s_flt);
        if (secs == 0) remote_log(">>> Sampling: 10 readings per second");
        else remote_log(">>> Sampling: one reading every %ld s, sleeping in between", secs);
#if CONFIG_SCALE_ZIGBEE
    } else if (line[0] == 'z') {
        char *end;
        long secs = strtol(line + 1, &end, 10);
        if (end == line + 1 || secs < 0 || secs > INTERVAL_MAX_S) {
            remote_log("!!! Zigbee report floor must be 0 (follow the sampling rate) to %d seconds",
                       INTERVAL_MAX_S);
            return;
        }
        zigbee_set_report_floor((uint32_t)secs);
#endif
#if CONFIG_SCALE_LINK_WIFI
    } else if (line[0] == 'w') {
        char *end;
        long on = strtol(line + 1, &end, 10);
        if (end == line + 1 || (on != 0 && on != 1)) {
            remote_log("!!! Wi-Fi command is \"w 0\" to turn it off or \"w 1\" to turn it on");
            return;
        }
        remote_set_wifi(on != 0);
#endif
    } else if (isdigit((unsigned char)line[0])) {
        float grams = strtof(line, NULL);
        int32_t v;
        if (grams > 0 && hx711_read_avg(&s_hx, 20, &v)) {
            float delta = (float)(v - s_offset);
            if (delta > -50 && delta < 50) {
                remote_log("!!! Not calibrated: the reading barely changed (%.0f counts). Is the weight on? Is the load cell wired/mounted right?", delta);
                return;
            }
            s_factor = delta / grams;
            save_cal();
            remote_log(">>> Calibrated: %.3f counts per gram (%.0f g -> %.0f counts)", s_factor, grams, delta);
        }
    } else {
        remote_log("Unknown command \"%s\". Commands: t = tare, = <grams> = set what is on it now, <grams> = calibrate, r = reset calibration, i <seconds> = sampling interval, z <seconds> = Zigbee report floor, j = rejoin Zigbee, w 0/1 = Wi-Fi off/on", line);
    }
}

static void poll_serial(void)
{
    static char buf[32];
    static int n = 0;
    uint8_t c;
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    while (usb_serial_jtag_read_bytes(&c, 1, 0) == 1) {
#else
    while (uart_read_bytes(UART_NUM_0, &c, 1, 0) == 1) {
#endif
        if (c == '\r' || c == '\n') {
            buf[n] = 0;
            handle_line(buf);
            n = 0;
        } else if (n < (int)sizeof buf - 1) {
            buf[n++] = (char)c;
        }
    }
}

// ---------- sampling ----------
// Auto-zero, convert to grams and hand the reading over.
static void finish_reading(scale_reading_t *r, float value, float step_counts)
{
    // Auto-zero: while settled within half a step of zero, follow slow drift (temperature, creep).
    // Never acts on a real load, which sits further away than that.
    float drift = value - s_offset;
    if (r->stable && fabsf(drift) < 0.5f * step_counts)
        s_offset += lroundf(drift * ZERO_TRACK_RATE);

    r->delta = lroundf(value) - s_offset;
    if (s_factor != 0) r->grams = shown_grams(r->delta / s_factor, r->step);
    remote_publish(r);
}

static void print_reading(const scale_reading_t *r)
{
    if (s_factor != 0)
        printf("raw %9ld   delta %8ld   weight %8.1f g %s  batt %.2f V\n",
               (long)r->raw, (long)r->delta, r->grams, r->stable ? "(stable)  " : "(settling)", r->batt);
    else
        printf("raw %9ld   delta %8ld   (uncalibrated)   batt %.2f V\n",
               (long)r->raw, (long)r->delta, r->batt);
}

static bool check_saturated(scale_reading_t *r, int samples_to_warn)
{
    static int count;
    r->saturated = r->raw >= 0x7FFFFF - 10 || r->raw <= -0x800000 + 10;
    if (!r->saturated) count = 0;
    else if (++count == samples_to_warn)
        remote_log("!!! Reading is maxed out. Usually a load cell wire is loose or E+/E-/A+/A- are mixed up.");
    return r->saturated;
}

// Continuous: every HX711 sample (10 per second) goes through the filter and out to the page.
static void sample_continuous(void)
{
    static uint32_t n;
    static float batt = -1;
    if (batt < 0 || ++n % 10 == 0) batt = batt_volts();  // once a second

    scale_reading_t r = { .factor = s_factor, .batt = batt };
    if (!hx711_read_raw(&s_hx, &r.raw, 500)) {
        remote_log("!!! HX711 read timed out (DT stuck high?)");
        remote_publish(&r);
        filter_reset(&s_flt);
        vTaskDelay(pdMS_TO_TICKS(1000));
        return;
    }
    r.ok = true;
    check_saturated(&r, 30);  // 3 s in a row

    r.step = display_step();
    float step_counts = r.step ? r.step * fabsf(s_factor) : STEP_MIN_COUNTS;
    float value = filter_add(&s_flt, r.raw, step_counts);
    r.stable = s_flt.stable;
    finish_reading(&r, value, step_counts);
    if (n % 10 == 0) print_reading(&r);
}

// Interval: wake the HX711, take a short burst, power it down again.
static void sample_burst(void)
{
    scale_reading_t r = { .factor = s_factor, .interval_s = s_interval_s };
    int32_t v[BURST_N];
    bool ok = hx_wake();
    for (int i = 0; ok && i < BURST_N; i++)
        ok = hx711_read_raw(&s_hx, &v[i], 500);
    hx_sleep();
    r.batt = batt_volts();
    if (!ok) {
        remote_log("!!! HX711 read timed out (DT stuck high?)");
        remote_publish(&r);
        return;
    }
    for (int i = 1; i < BURST_N; i++)  // insertion sort, 8 items
        for (int j = i; j > 0 && v[j - 1] > v[j]; j--) {
            int32_t t = v[j]; v[j] = v[j - 1]; v[j - 1] = t;
        }
    // Average the middle half, which drops spikes at either end.
    int lo = BURST_N / 4, hi = BURST_N - BURST_N / 4;
    int64_t sum = 0;
    for (int i = lo; i < hi; i++) sum += v[i];
    float value = (float)sum / (hi - lo);

    r.ok = true;
    r.raw = v[BURST_N / 2];
    check_saturated(&r, 3);
    r.step = display_step();
    float step_counts = r.step ? r.step * fabsf(s_factor) : STEP_MIN_COUNTS;
    r.stable = v[hi - 1] - v[lo] < step_counts;  // the middle samples agree within a step
    s_shown = NAN;  // readings are far apart: show each one as measured
    finish_reading(&r, value, step_counts);
    print_reading(&r);
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    // Boards with only the chip's own USB port: read commands from it too.
    usb_serial_jtag_driver_config_t ucfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    usb_serial_jtag_driver_install(&ucfg);
    usb_serial_jtag_vfs_use_driver();
#else
    uart_driver_install(UART_NUM_0, 256, 0, 0, NULL, 0);
#endif
    remote_init();

    const esp_app_desc_t *app = esp_app_get_description();
    char build[9];
    esp_app_get_elf_sha256(build, sizeof build);
    remote_log("=== HX711 scale, firmware %s (build %s) ===", app->version, build);
    esp_reset_reason_t why = esp_reset_reason();
    boot_log_record();
    remote_log("%sLast reset: %s", why == ESP_RST_BROWNOUT || why == ESP_RST_PANIC ? "!!! " : "",
            why == ESP_RST_POWERON ? "power on" :
            why == ESP_RST_SW ? "restart (e.g. after an update)" :
            why == ESP_RST_PANIC ? "crash" :
            why == ESP_RST_BROWNOUT ? "brownout: the supply voltage dipped" :
            why == ESP_RST_INT_WDT || why == ESP_RST_TASK_WDT || why == ESP_RST_WDT ? "watchdog" :
            why == ESP_RST_EXT ? "reset button" : "other");
#if CONFIG_IDF_TARGET_ESP32C6
    // XIAO ESP32-C6: GPIO3 low powers the antenna switch, GPIO14 low picks the built-in antenna.
    // Other C6 boards leave these pins unused, so driving them low does no harm.
    gpio_config_t rf = { .pin_bit_mask = (1ULL << 3) | (1ULL << 14), .mode = GPIO_MODE_OUTPUT };
    gpio_config(&rf);
    gpio_set_level(3, 0);
    gpio_set_level(14, 0);
#endif
    remote_start();
    batt_init();
    hx711_init(&s_hx);
    load_cal();

    remote_log("Battery pin: %.2f V (ignore if divider not wired)", batt_volts());

    // Check the HX711 answers at all
    int32_t first;
    while (!hx711_read_raw(&s_hx, &first, 1000)) {
        remote_log("!!! HX711 not responding. Check VCC->3V3, GND, DT->GPIO32, SCK->GPIO33. Retrying...");
    }

    vTaskDelay(pdMS_TO_TICKS(500));  // let it settle
    if (s_have_offset) {
        // Keep the saved zero, so a restart with something on the scale (a plant, say) doesn't zero it out.
        remote_log("Using saved zero (offset %ld). Tare with the scale empty to set a new one.", (long)s_offset);
    } else if (hx711_read_avg(&s_hx, 20, &s_offset)) {
        save_cal();
        remote_log("Tared at boot. Offset = %ld", (long)s_offset);
    } else {
        remote_log("!!! Tare at boot failed: HX711 read timed out");
    }
    if (s_factor == 0)
        remote_log("Not calibrated yet: put a known weight on, then enter its grams (e.g. 1000).");
    else
        remote_log("Using saved calibration: %.3f counts/g", s_factor);
    if (s_interval_s)
        remote_log("Sampling: one reading every %lu s", (unsigned long)s_interval_s);

    filter_reset(&s_flt);
    int64_t next_us = 0;
    for (;;) {
        poll_serial();
        char cmd[REMOTE_CMD_LEN];
        while (remote_take_command(cmd, sizeof cmd))
            handle_line(cmd);

        if (s_interval_s == 0) {
            if (!s_hx_awake) {
                hx_wake();
                filter_reset(&s_flt);
            }
            sample_continuous();
            continue;
        }

        int64_t now = esp_timer_get_time();
        // How long this run has lasted, so the next boot can say
        // whether the last one died after a minute or after a week.
        // Once a minute: often enough to be useful, rare enough that
        // it is not what fills the NVS partition.
        uint32_t seconds = (uint32_t)(now / 1000000);
        if (seconds / 60 != s_logged_minute) {
            s_logged_minute = seconds / 60;
            boot_log_still_running(seconds);
        }
        if (s_sample_now || now >= next_us) {
            s_sample_now = false;
            next_us = now + s_interval_s * 1000000LL;
            sample_burst();
        } else {
            // Sleep until the next reading. Wake at least once a second for typed commands.
            int64_t wait_ms = (next_us - now) / 1000 + 1;
            if (remote_wait_command(cmd, sizeof cmd, wait_ms > 1000 ? 1000 : wait_ms))
                handle_line(cmd);
        }
    }
}
