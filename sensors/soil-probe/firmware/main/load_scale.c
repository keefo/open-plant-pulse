#include "load_scale.h"

#include <string.h>

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "sdkconfig.h"

#if CONFIG_OPP_LOAD_SCALE_ENABLED

#define SCALE_NAMESPACE "load_scale"
#define SCALE_KEY "calibration"

/* Channel A at gain 128, the only gain with enough resolution for a bridge
 * output of about 1 mV/V. One extra clock pulse after the 24 data bits
 * selects it for the next conversion. */
#define SCALE_GAIN_PULSES 1
#define SCALE_DATA_BITS 24

/* PD_SCK held high for more than 60 us powers the chip down, so a task switch
 * in the middle of the bit-bang would both corrupt the reading and drop the
 * excitation. The whole 25-pulse exchange runs in a critical section: at
 * 2 us per pulse that is about 50 us with interrupts off, which is short
 * enough not to disturb the Bluetooth controller. */
#define SCALE_CLOCK_HALF_PERIOD_US 1
#define SCALE_POWER_DOWN_HOLD_US 100

/* The datasheet gives 400 ms to the first conversion after power-up at 10 SPS.
 * Waiting for DOUT to fall covers it, but a cell that is unplugged never pulls
 * it low, so the wait is bounded. */
#define SCALE_READY_TIMEOUT_MS 600
#define SCALE_READY_POLL_MS 10

static const char *TAG = "load_scale";

typedef struct {
    int32_t tare_offset;
    /* Counts per gram, scaled by 65536 so a fractional slope survives NVS
     * without storing a float. */
    int32_t counts_per_gram_q16;
} scale_calibration_t;

static bool gpio_configured;
static bool powered;
static scale_calibration_t calibration;
static bool calibration_loaded;

static portMUX_TYPE clock_lock = portMUX_INITIALIZER_UNLOCKED;

static esp_err_t configure_gpio(void)
{
    if (gpio_configured) {
        return ESP_OK;
    }

    const gpio_config_t clock_config = {
        .pin_bit_mask = 1ULL << CONFIG_OPP_LOAD_SCALE_SCK_GPIO,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    /* High before it becomes an output, so configuring the pin leaves the
     * HX711 powered down rather than pulsing the bridge on. */
    gpio_set_level(CONFIG_OPP_LOAD_SCALE_SCK_GPIO, 1);
    esp_err_t error = gpio_config(&clock_config);
    if (error != ESP_OK) {
        return error;
    }

    const gpio_config_t data_config = {
        .pin_bit_mask = 1ULL << CONFIG_OPP_LOAD_SCALE_DT_GPIO,
        .mode = GPIO_MODE_INPUT,
        /* The HX711 drives DOUT both ways; the pull-up only keeps the pin
         * defined when no module is attached, so a missing board reads as
         * "never ready" instead of as a random conversion. */
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    error = gpio_config(&data_config);
    if (error != ESP_OK) {
        return error;
    }

    gpio_configured = true;
    powered = false;
    return ESP_OK;
}

static void power_up(void)
{
    gpio_set_level(CONFIG_OPP_LOAD_SCALE_SCK_GPIO, 0);
    powered = true;
}

void opp_load_scale_power_down(void)
{
    if (!gpio_configured) {
        return;
    }
    gpio_set_level(CONFIG_OPP_LOAD_SCALE_SCK_GPIO, 0);
    esp_rom_delay_us(SCALE_CLOCK_HALF_PERIOD_US);
    gpio_set_level(CONFIG_OPP_LOAD_SCALE_SCK_GPIO, 1);
    esp_rom_delay_us(SCALE_POWER_DOWN_HOLD_US);
    powered = false;
}

static bool data_ready(void)
{
    return gpio_get_level(CONFIG_OPP_LOAD_SCALE_DT_GPIO) == 0;
}

static esp_err_t wait_until_ready(void)
{
    for (int waited_ms = 0; waited_ms < SCALE_READY_TIMEOUT_MS;
         waited_ms += SCALE_READY_POLL_MS) {
        if (data_ready()) {
            return ESP_OK;
        }
        vTaskDelay(pdMS_TO_TICKS(SCALE_READY_POLL_MS));
    }
    return data_ready() ? ESP_OK : ESP_ERR_TIMEOUT;
}

/* One conversion, assuming DOUT is already low. */
static int32_t shift_in_conversion(void)
{
    uint32_t value = 0;

    portENTER_CRITICAL(&clock_lock);
    for (int bit = 0; bit < SCALE_DATA_BITS; bit++) {
        gpio_set_level(CONFIG_OPP_LOAD_SCALE_SCK_GPIO, 1);
        esp_rom_delay_us(SCALE_CLOCK_HALF_PERIOD_US);
        value = (value << 1) | (uint32_t)gpio_get_level(CONFIG_OPP_LOAD_SCALE_DT_GPIO);
        gpio_set_level(CONFIG_OPP_LOAD_SCALE_SCK_GPIO, 0);
        esp_rom_delay_us(SCALE_CLOCK_HALF_PERIOD_US);
    }
    for (int pulse = 0; pulse < SCALE_GAIN_PULSES; pulse++) {
        gpio_set_level(CONFIG_OPP_LOAD_SCALE_SCK_GPIO, 1);
        esp_rom_delay_us(SCALE_CLOCK_HALF_PERIOD_US);
        gpio_set_level(CONFIG_OPP_LOAD_SCALE_SCK_GPIO, 0);
        esp_rom_delay_us(SCALE_CLOCK_HALF_PERIOD_US);
    }
    portEXIT_CRITICAL(&clock_lock);

    /* 24-bit two's complement: sign-extend into the top byte. */
    if (value & 0x800000U) {
        value |= 0xFF000000U;
    }
    return (int32_t)value;
}

static esp_err_t read_average(int32_t *average)
{
    esp_err_t error = configure_gpio();
    if (error != ESP_OK) {
        return error;
    }

    power_up();

    for (int discarded = 0; discarded < CONFIG_OPP_LOAD_SCALE_DISCARDED_SAMPLES;
         discarded++) {
        error = wait_until_ready();
        if (error != ESP_OK) {
            opp_load_scale_power_down();
            return error;
        }
        (void)shift_in_conversion();
    }

    int64_t total = 0;
    for (int sample = 0; sample < CONFIG_OPP_LOAD_SCALE_SAMPLES; sample++) {
        error = wait_until_ready();
        if (error != ESP_OK) {
            opp_load_scale_power_down();
            return error;
        }
        total += shift_in_conversion();
    }

    opp_load_scale_power_down();
    *average = (int32_t)(total / CONFIG_OPP_LOAD_SCALE_SAMPLES);
    return ESP_OK;
}

static esp_err_t load_calibration(void)
{
    if (calibration_loaded) {
        return ESP_OK;
    }

    nvs_handle_t handle;
    esp_err_t error = nvs_open(SCALE_NAMESPACE, NVS_READONLY, &handle);
    if (error == ESP_ERR_NVS_NOT_FOUND) {
        calibration_loaded = true;
        return ESP_OK;
    }
    if (error != ESP_OK) {
        return error;
    }

    scale_calibration_t stored;
    size_t size = sizeof(stored);
    error = nvs_get_blob(handle, SCALE_KEY, &stored, &size);
    nvs_close(handle);

    if (error == ESP_OK && size == sizeof(stored)) {
        calibration = stored;
    } else if (error != ESP_ERR_NVS_NOT_FOUND) {
        return error;
    }

    calibration_loaded = true;
    return ESP_OK;
}

static esp_err_t store_calibration(void)
{
    nvs_handle_t handle;
    esp_err_t error = nvs_open(SCALE_NAMESPACE, NVS_READWRITE, &handle);
    if (error != ESP_OK) {
        return error;
    }
    error = nvs_set_blob(handle, SCALE_KEY, &calibration, sizeof(calibration));
    if (error == ESP_OK) {
        error = nvs_commit(handle);
    }
    nvs_close(handle);
    return error;
}

esp_err_t opp_load_scale_init(void)
{
    esp_err_t error = configure_gpio();
    if (error != ESP_OK) {
        return error;
    }
    opp_load_scale_power_down();
    return load_calibration();
}

bool opp_load_scale_is_calibrated(void)
{
    return load_calibration() == ESP_OK && calibration.counts_per_gram_q16 != 0;
}

esp_err_t opp_load_scale_read(opp_load_scale_reading_t *reading)
{
    if (reading == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t error = load_calibration();
    if (error != ESP_OK) {
        return error;
    }

    int32_t raw;
    error = read_average(&raw);
    if (error != ESP_OK) {
        return error;
    }

    reading->raw = raw;
    reading->calibrated = calibration.counts_per_gram_q16 != 0;
    reading->grams =
        reading->calibrated
            ? (int32_t)(((int64_t)(raw - calibration.tare_offset) * 65536) /
                        calibration.counts_per_gram_q16)
            : 0;

    ESP_LOGI(TAG, "Scale raw=%ld grams=%ld%s", (long)reading->raw,
             (long)reading->grams, reading->calibrated ? "" : " (uncalibrated)");
    return ESP_OK;
}

esp_err_t opp_load_scale_tare(void)
{
    esp_err_t error = load_calibration();
    if (error != ESP_OK) {
        return error;
    }

    int32_t raw;
    error = read_average(&raw);
    if (error != ESP_OK) {
        return error;
    }

    calibration.tare_offset = raw;
    error = store_calibration();
    if (error == ESP_OK) {
        ESP_LOGI(TAG, "Tare offset %ld", (long)calibration.tare_offset);
    }
    return error;
}

esp_err_t opp_load_scale_calibrate(int32_t known_grams)
{
    if (known_grams == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t error = load_calibration();
    if (error != ESP_OK) {
        return error;
    }

    int32_t raw;
    error = read_average(&raw);
    if (error != ESP_OK) {
        return error;
    }

    const int64_t counts = (int64_t)raw - calibration.tare_offset;
    const int64_t slope_q16 = (counts * 65536) / known_grams;
    if (slope_q16 == 0) {
        /* The known mass moved the reading by less than a count per gram, so
         * either nothing is on the platform or a cell is disconnected. */
        ESP_LOGE(TAG, "Calibration mass moved the reading by only %lld counts",
                 (long long)counts);
        return ESP_ERR_INVALID_RESPONSE;
    }

    calibration.counts_per_gram_q16 = (int32_t)slope_q16;
    error = store_calibration();
    if (error == ESP_OK) {
        ESP_LOGI(TAG, "Calibrated %lld counts over %ld g (%.3f counts/g)",
                 (long long)counts, (long)known_grams,
                 calibration.counts_per_gram_q16 / 65536.0);
    }
    return error;
}

#else /* !CONFIG_OPP_LOAD_SCALE_ENABLED */

esp_err_t opp_load_scale_init(void)
{
    return ESP_OK;
}

esp_err_t opp_load_scale_read(opp_load_scale_reading_t *reading)
{
    (void)reading;
    return ESP_ERR_NOT_SUPPORTED;
}

void opp_load_scale_power_down(void)
{
}

esp_err_t opp_load_scale_tare(void)
{
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t opp_load_scale_calibrate(int32_t known_grams)
{
    (void)known_grams;
    return ESP_ERR_NOT_SUPPORTED;
}

bool opp_load_scale_is_calibrated(void)
{
    return false;
}

#endif /* CONFIG_OPP_LOAD_SCALE_ENABLED */
