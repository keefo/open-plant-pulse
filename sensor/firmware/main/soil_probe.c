#include "soil_probe.h"

#include <string.h>

#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#if CONFIG_OPP_SOIL_PROBE_ENABLED

#define SOIL_PROBE_UART UART_NUM_1
/* Must exceed the 128-byte hardware FIFO for the driver to accept it. */
#define SOIL_PROBE_RX_BUFFER_SIZE 256
/* The manual gives no reply latency; at 4800 baud the 19-byte reply alone
 * takes 40 ms, so half a second is generous without stalling the task. */
#define SOIL_PROBE_RESPONSE_TIMEOUT_MS 500
#define SOIL_PROBE_READ_SLICE_MS 20
#define SOIL_PROBE_REQUEST_SIZE 8
/* Replies on the prototype sometimes lose their last bits or bytes. A retry
 * costs 100 ms; a sample lost costs a whole interval. */
#define SOIL_PROBE_ATTEMPTS 3
#define SOIL_PROBE_RETRY_DELAY_MS 100
#define SOIL_PROBE_TASK_STACK_SIZE 3072
#define SOIL_PROBE_TASK_PRIORITY 5

static const char *TAG = "soil_probe";
static bool uart_installed;
static bool power_configured;
/* Without a power switch the probe is always on. */
static bool probe_powered = CONFIG_OPP_SOIL_PROBE_POWER_GPIO < 0;
static int64_t powered_at_us;
static bool answer_time_logged;

static portMUX_TYPE sample_lock = portMUX_INITIALIZER_UNLOCKED;
static TaskHandle_t monitor_task;
static opp_soil_probe_sample_record_t latest_sample;
static int64_t latest_sample_time_us;
static bool latest_sample_valid;
static bool monitor_enabled;
static esp_err_t last_error = ESP_ERR_INVALID_STATE;
static RTC_DATA_ATTR uint64_t sample_sequence;

/* One request/reply exchange at a time: the monitor task and a debug read from
 * the web console share the half-duplex bus, and interleaved requests would
 * garble both replies. */
static SemaphoreHandle_t bus_lock(void)
{
    static StaticSemaphore_t storage;
    static SemaphoreHandle_t lock;
    static portMUX_TYPE create_lock = portMUX_INITIALIZER_UNLOCKED;
    portENTER_CRITICAL(&create_lock);
    if (lock == NULL) {
        lock = xSemaphoreCreateMutexStatic(&storage);
    }
    portEXIT_CRITICAL(&create_lock);
    return lock;
}

/* Held for a whole power-on, read, power-off sequence.
 *
 * The bus lock covers one exchange; this covers the supply. Without it a debug
 * read from the console could power the probe up while the monitor task was
 * switching it off around its own read, and one of them would be talking to an
 * unpowered probe. */
static SemaphoreHandle_t power_lock(void)
{
    static StaticSemaphore_t storage;
    static SemaphoreHandle_t lock;
    static portMUX_TYPE create_lock = portMUX_INITIALIZER_UNLOCKED;
    portENTER_CRITICAL(&create_lock);
    if (lock == NULL) {
        lock = xSemaphoreCreateMutexStatic(&storage);
    }
    portEXIT_CRITICAL(&create_lock);
    return lock;
}

/* Put the probe back to sleep: the UART first, so no pin is left driving a
 * transceiver that is about to lose its supply. */
static void release_probe(void)
{
    const esp_err_t uart_error = opp_soil_probe_deinit();
    if (uart_error != ESP_OK) {
        ESP_LOGW(TAG, "Could not release the RS485 UART: %s", esp_err_to_name(uart_error));
    }
    const esp_err_t power_error = opp_soil_probe_set_power(false);
    if (power_error != ESP_OK) {
        ESP_LOGW(TAG, "Could not switch probe power off: %s", esp_err_to_name(power_error));
    }
}

static esp_err_t install_uart(void)
{
    if (uart_installed) {
        return ESP_OK;
    }

    const uart_config_t config = {
        .baud_rate = CONFIG_OPP_SOIL_PROBE_BAUD_RATE,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    esp_err_t error = uart_driver_install(SOIL_PROBE_UART, SOIL_PROBE_RX_BUFFER_SIZE,
                                          0, 0, NULL, 0);
    if (error != ESP_OK) {
        return error;
    }
    error = uart_param_config(SOIL_PROBE_UART, &config);
    /* The transceiver's driver-enable goes on the UART's RTS output, which the
     * hardware raises for exactly as long as a request is on the wire. Timing
     * it from software would risk clipping the last byte or the first byte of
     * the reply. */
    if (error == ESP_OK) {
        error = uart_set_pin(SOIL_PROBE_UART, CONFIG_OPP_SOIL_PROBE_TX_GPIO,
                             CONFIG_OPP_SOIL_PROBE_RX_GPIO, CONFIG_OPP_SOIL_PROBE_DE_GPIO,
                             UART_PIN_NO_CHANGE);
    }
    if (error == ESP_OK) {
        error = uart_set_mode(SOIL_PROBE_UART, UART_MODE_RS485_HALF_DUPLEX);
    }
    if (error != ESP_OK) {
        uart_driver_delete(SOIL_PROBE_UART);
        return error;
    }
    uart_installed = true;
    return ESP_OK;
}

esp_err_t opp_soil_probe_init(void)
{
    xSemaphoreTake(bus_lock(), portMAX_DELAY);
    const esp_err_t error = install_uart();
    xSemaphoreGive(bus_lock());
    return error;
}

esp_err_t opp_soil_probe_set_power(bool on)
{
#if CONFIG_OPP_SOIL_PROBE_POWER_GPIO >= 0
    if (!power_configured) {
        const gpio_config_t config = {
            .pin_bit_mask = 1ULL << CONFIG_OPP_SOIL_PROBE_POWER_GPIO,
            .mode = GPIO_MODE_OUTPUT,
            .pull_up_en = GPIO_PULLUP_DISABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE,
        };
        /* Low before it becomes an output, so configuring it cannot pulse
         * the probe on. */
        gpio_set_level(CONFIG_OPP_SOIL_PROBE_POWER_GPIO, 0);
        esp_err_t error = gpio_config(&config);
        if (error != ESP_OK) {
            return error;
        }
        power_configured = true;
    }
    if (on == probe_powered) {
        return ESP_OK;
    }
    esp_err_t error = gpio_set_level(CONFIG_OPP_SOIL_PROBE_POWER_GPIO, on ? 1 : 0);
    if (error != ESP_OK) {
        return error;
    }
    probe_powered = on;
    ESP_LOGI(TAG, "Probe power %s (GPIO%d)", on ? "on" : "off",
             CONFIG_OPP_SOIL_PROBE_POWER_GPIO);
    if (on) {
        powered_at_us = esp_timer_get_time();
        answer_time_logged = false;
        vTaskDelay(pdMS_TO_TICKS(CONFIG_OPP_SOIL_PROBE_WARMUP_MS));
    }
    return ESP_OK;
#else
    (void)on;
    return ESP_OK;
#endif
}

bool opp_soil_probe_is_powered(void)
{
    return probe_powered;
}

esp_err_t opp_soil_probe_deinit(void)
{
    if (!uart_installed) {
        return ESP_OK;
    }
    esp_err_t error = uart_driver_delete(SOIL_PROBE_UART);
    if (error == ESP_OK) {
        uart_installed = false;
    }
    return error;
}

/* Send one request and collect a reply of the expected size, skipping our own
 * request if the board echoes it. On success *reply points into received. */
static esp_err_t transact(const uint8_t request[SOIL_PROBE_REQUEST_SIZE],
                          size_t expected_size,
                          uint8_t *received, size_t capacity,
                          const uint8_t **reply, size_t *reply_length)
{
    if (!uart_installed) {
        return ESP_ERR_INVALID_STATE;
    }
    uart_flush_input(SOIL_PROBE_UART);
    if (uart_write_bytes(SOIL_PROBE_UART, request, SOIL_PROBE_REQUEST_SIZE) !=
        SOIL_PROBE_REQUEST_SIZE) {
        return ESP_FAIL;
    }
    esp_err_t error = uart_wait_tx_done(SOIL_PROBE_UART, pdMS_TO_TICKS(100));
    if (error != ESP_OK) {
        return error;
    }

    size_t length = 0;
    size_t start = 0;
    const int64_t deadline_us =
        esp_timer_get_time() + (int64_t)SOIL_PROBE_RESPONSE_TIMEOUT_MS * 1000;
    while (esp_timer_get_time() < deadline_us && length < capacity) {
        const int count = uart_read_bytes(SOIL_PROBE_UART, received + length,
                                          capacity - length,
                                          pdMS_TO_TICKS(SOIL_PROBE_READ_SLICE_MS));
        if (count < 0) {
            return ESP_FAIL;
        }
        length += (size_t)count;
        /* A board that leaves its receiver enabled while transmitting hears
         * the request first. */
        if (start == 0 && length >= SOIL_PROBE_REQUEST_SIZE &&
            memcmp(received, request, SOIL_PROBE_REQUEST_SIZE) == 0) {
            start = SOIL_PROBE_REQUEST_SIZE;
        }
        if (length - start >= expected_size) {
            break;
        }
    }
    if (length == 0) {
        return ESP_ERR_TIMEOUT;
    }
    *reply = received + start;
    *reply_length = length - start;
    return ESP_OK;
}

static void log_unusable_reply(const uint8_t *reply, size_t length)
{
    /* The raw bytes are what tells a swapped A/B, a wrong baud rate and a
     * wrong address apart, so show them rather than only saying "bad". */
    ESP_LOGW(TAG, "Unusable probe reply (%u byte(s)):", (unsigned)length);
    ESP_LOG_BUFFER_HEX_LEVEL(TAG, reply, length, ESP_LOG_WARN);
}

static esp_err_t read_once(opp_soil_probe_reading_t *reading)
{
    uint8_t request[SOIL_PROBE_REQUEST_SIZE];
    opp_modbus_build_read_request(CONFIG_OPP_SENSOR_MODBUS_ADDRESS,
                                  OPP_SOIL_PROBE_FIRST_REGISTER,
                                  OPP_SOIL_PROBE_REGISTER_COUNT, request);
    uint8_t received[SOIL_PROBE_REQUEST_SIZE + OPP_SOIL_PROBE_RESPONSE_SIZE];
    const uint8_t *reply;
    size_t reply_length;
    esp_err_t error = transact(request, OPP_SOIL_PROBE_RESPONSE_SIZE, received,
                               sizeof(received), &reply, &reply_length);
    if (error != ESP_OK) {
        return error;
    }
    if (!opp_modbus_parse_soil_probe_response(reply, reply_length,
                                              CONFIG_OPP_SENSOR_MODBUS_ADDRESS, reading)) {
        log_unusable_reply(reply, reply_length);
        return ESP_ERR_INVALID_RESPONSE;
    }
    return ESP_OK;
}

static esp_err_t read_registers_once(uint8_t function, uint16_t first_register,
                                     uint16_t count, uint16_t *values)
{
    uint8_t request[SOIL_PROBE_REQUEST_SIZE];
    if (!opp_modbus_build_read(CONFIG_OPP_SENSOR_MODBUS_ADDRESS, function,
                               first_register, count, request)) {
        return ESP_ERR_INVALID_ARG;
    }
    const size_t expected_size = 3U + 2U * count + 2U;
    uint8_t received[SOIL_PROBE_REQUEST_SIZE + 3U + 2U * OPP_MODBUS_MAX_READ_REGISTERS + 2U];
    const uint8_t *reply;
    size_t reply_length;
    esp_err_t error = transact(request, expected_size, received, sizeof(received),
                               &reply, &reply_length);
    if (error != ESP_OK) {
        return error;
    }
    if (!opp_modbus_parse_registers(reply, reply_length, CONFIG_OPP_SENSOR_MODBUS_ADDRESS,
                                    function, count, values)) {
        /* A five-byte reply with the function's high bit set is a Modbus
         * exception: the probe understood and refused, e.g. an unmapped
         * register. That is an answer, not line noise. */
        if (reply_length >= 3 && reply[1] == (uint8_t)(function | 0x80U)) {
            ESP_LOGW(TAG, "Probe refused function 0x%02x at 0x%04x: exception %u",
                     function, first_register, reply[2]);
            return ESP_ERR_NOT_SUPPORTED;
        }
        log_unusable_reply(reply, reply_length);
        return ESP_ERR_INVALID_RESPONSE;
    }
    return ESP_OK;
}

static bool is_final(esp_err_t error)
{
    return error == ESP_OK || error == ESP_ERR_INVALID_ARG ||
           error == ESP_ERR_INVALID_STATE || error == ESP_ERR_NOT_SUPPORTED;
}

esp_err_t opp_soil_probe_read(opp_soil_probe_reading_t *reading)
{
    if (reading == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!probe_powered) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(bus_lock(), portMAX_DELAY);
    esp_err_t error = ESP_FAIL;
    for (int attempt = 1; attempt <= SOIL_PROBE_ATTEMPTS; ++attempt) {
        error = read_once(reading);
        if (is_final(error)) {
            break;
        }
        if (attempt < SOIL_PROBE_ATTEMPTS) {
            vTaskDelay(pdMS_TO_TICKS(SOIL_PROBE_RETRY_DELAY_MS));
        }
    }
    xSemaphoreGive(bus_lock());
    if (error == ESP_OK && !answer_time_logged && powered_at_us != 0) {
        /* How long the probe really needs, for tuning the warm-up. */
        ESP_LOGI(TAG, "Probe answered %lld ms after power-on (warm-up %d ms)",
                 (long long)((esp_timer_get_time() - powered_at_us) / 1000),
                 CONFIG_OPP_SOIL_PROBE_WARMUP_MS);
        answer_time_logged = true;
    }
    return error;
}

esp_err_t opp_soil_probe_read_registers(uint8_t function, uint16_t first_register,
                                        uint16_t count, uint16_t *values)
{
    if (values == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    /* The probe is off between samples now, so this powers it up for the
     * question and puts it back afterwards. Somebody hunting for a register on
     * the console should not have to know that, or have to switch sampling on
     * to be allowed to ask. */
    xSemaphoreTake(power_lock(), portMAX_DELAY);
    const bool was_powered = probe_powered;
    esp_err_t error = ESP_OK;
    if (!was_powered) {
        error = opp_soil_probe_set_power(true);
    }
    if (error == ESP_OK) {
        error = opp_soil_probe_init();
    }
    if (error == ESP_OK) {
        xSemaphoreTake(bus_lock(), portMAX_DELAY);
        for (int attempt = 1; attempt <= SOIL_PROBE_ATTEMPTS; ++attempt) {
            error = read_registers_once(function, first_register, count, values);
            if (is_final(error)) {
                break;
            }
            if (attempt < SOIL_PROBE_ATTEMPTS) {
                vTaskDelay(pdMS_TO_TICKS(SOIL_PROBE_RETRY_DELAY_MS));
            }
        }
        xSemaphoreGive(bus_lock());
    }
    if (!was_powered) {
        release_probe();
    }
    xSemaphoreGive(power_lock());
    return error;
}

static void record_result(esp_err_t error, const opp_soil_probe_reading_t *reading)
{
    const int64_t now_us = esp_timer_get_time();
    portENTER_CRITICAL(&sample_lock);
    if (!monitor_enabled) {
        /* Switched off while this read was on the wire. */
        portEXIT_CRITICAL(&sample_lock);
        return;
    }
    last_error = error;
    if (error == ESP_OK) {
        latest_sample.values = *reading;
        latest_sample.sequence = ++sample_sequence;
        latest_sample_time_us = now_us;
        latest_sample_valid = true;
    } else {
        /* A stale soil reading reported as current is worse than none. */
        latest_sample_valid = false;
    }
    portEXIT_CRITICAL(&sample_lock);
}

static void soil_probe_monitor_task(void *context)
{
    (void)context;
    esp_err_t previous_error = ESP_ERR_INVALID_STATE;
    bool disabled_logged = false;

    while (true) {
        if (!opp_soil_probe_monitor_is_enabled()) {
            xSemaphoreTake(power_lock(), portMAX_DELAY);
            release_probe();
            xSemaphoreGive(power_lock());
            if (!disabled_logged) {
                ESP_LOGI(TAG, "Soil probe sampling disabled");
                disabled_logged = true;
            }
            previous_error = ESP_ERR_INVALID_STATE;
            ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
            continue;
        }
        disabled_logged = false;

        /* Powered for the reading and nothing else.
         *
         * The probe and its boost converter cost 28.5 mA, which is most of what
         * a battery node spends between samples, and it has no use between
         * them. The warm-up is paid once per sample instead of once per session:
         * measured against the interval that is a good trade, but it is why the
         * first pH after power-on has to be trustworthy before this number is
         * believed. */
        xSemaphoreTake(power_lock(), portMAX_DELAY);
        esp_err_t error = opp_soil_probe_set_power(true);
        if (error == ESP_OK) {
            error = opp_soil_probe_init();
        }
        opp_soil_probe_reading_t reading = {0};
        if (error == ESP_OK) {
            error = opp_soil_probe_read(&reading);
        }
        release_probe();
        xSemaphoreGive(power_lock());
        record_result(error, &reading);

        if (error == ESP_OK) {
            ESP_LOGI(TAG,
                     "moisture=%.1f%% temperature=%.1f C ec=%u uS/cm ph=%.1f "
                     "n=%u p=%u k=%u mg/kg",
                     reading.moisture_tenths_percent / 10.0,
                     reading.temperature_tenths_celsius / 10.0,
                     reading.conductivity_us_cm, reading.ph_tenths / 10.0,
                     reading.nitrogen_mg_kg, reading.phosphorus_mg_kg,
                     reading.potassium_mg_kg);
        } else if (error != previous_error) {
            /* Log a change of failure once, not every interval. */
            ESP_LOGW(TAG, "Probe read failed at address %d, %d baud "
                          "(TX GPIO%d, RX GPIO%d, DE GPIO%d): %s",
                     CONFIG_OPP_SENSOR_MODBUS_ADDRESS, CONFIG_OPP_SOIL_PROBE_BAUD_RATE,
                     CONFIG_OPP_SOIL_PROBE_TX_GPIO, CONFIG_OPP_SOIL_PROBE_RX_GPIO,
                     CONFIG_OPP_SOIL_PROBE_DE_GPIO, esp_err_to_name(error));
        }
        previous_error = error;
        ulTaskNotifyTake(pdTRUE,
                         pdMS_TO_TICKS(CONFIG_OPP_SOIL_PROBE_SAMPLE_INTERVAL_SECONDS * 1000));
    }
}

esp_err_t opp_soil_probe_monitor_start(bool enabled)
{
    if (monitor_task != NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    portENTER_CRITICAL(&sample_lock);
    monitor_enabled = enabled;
    latest_sample_valid = false;
    portEXIT_CRITICAL(&sample_lock);
    BaseType_t created = xTaskCreate(soil_probe_monitor_task, "soil_probe",
                                     SOIL_PROBE_TASK_STACK_SIZE, NULL,
                                     SOIL_PROBE_TASK_PRIORITY, &monitor_task);
    return created == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

bool opp_soil_probe_monitor_is_running(void)
{
    return monitor_task != NULL;
}

void opp_soil_probe_monitor_set_enabled(bool enabled)
{
    portENTER_CRITICAL(&sample_lock);
    monitor_enabled = enabled;
    if (!enabled) {
        latest_sample_valid = false;
        last_error = ESP_OK;
    }
    TaskHandle_t task = monitor_task;
    portEXIT_CRITICAL(&sample_lock);
    if (task != NULL) {
        xTaskNotifyGive(task);
    }
}

bool opp_soil_probe_monitor_is_enabled(void)
{
    portENTER_CRITICAL(&sample_lock);
    const bool enabled = monitor_enabled;
    portEXIT_CRITICAL(&sample_lock);
    return enabled;
}

bool opp_soil_probe_monitor_get_latest(opp_soil_probe_sample_record_t *sample,
                                       int64_t *sample_age_ms)
{
    if (sample == NULL || sample_age_ms == NULL) {
        return false;
    }
    int64_t sample_time_us = 0;
    portENTER_CRITICAL(&sample_lock);
    const bool valid = latest_sample_valid;
    if (valid) {
        *sample = latest_sample;
        sample_time_us = latest_sample_time_us;
    }
    portEXIT_CRITICAL(&sample_lock);
    if (!valid) {
        return false;
    }
    *sample_age_ms = (esp_timer_get_time() - sample_time_us) / 1000;
    return true;
}

esp_err_t opp_soil_probe_monitor_last_error(void)
{
    portENTER_CRITICAL(&sample_lock);
    const esp_err_t error = last_error;
    portEXIT_CRITICAL(&sample_lock);
    return error;
}

#else

/* Built without a probe: every call reports that plainly. */
esp_err_t opp_soil_probe_set_power(bool on)
{
    (void)on;
    return ESP_ERR_NOT_SUPPORTED;
}
bool opp_soil_probe_is_powered(void) { return false; }
esp_err_t opp_soil_probe_init(void) { return ESP_ERR_NOT_SUPPORTED; }
esp_err_t opp_soil_probe_deinit(void) { return ESP_OK; }
esp_err_t opp_soil_probe_read(opp_soil_probe_reading_t *reading)
{
    (void)reading;
    return ESP_ERR_NOT_SUPPORTED;
}
esp_err_t opp_soil_probe_read_registers(uint8_t function, uint16_t first_register,
                                        uint16_t count, uint16_t *values)
{
    (void)function;
    (void)first_register;
    (void)count;
    (void)values;
    return ESP_ERR_NOT_SUPPORTED;
}
esp_err_t opp_soil_probe_monitor_start(bool enabled)
{
    (void)enabled;
    return ESP_ERR_NOT_SUPPORTED;
}
bool opp_soil_probe_monitor_is_running(void) { return false; }
void opp_soil_probe_monitor_set_enabled(bool enabled) { (void)enabled; }
bool opp_soil_probe_monitor_is_enabled(void) { return false; }
bool opp_soil_probe_monitor_get_latest(opp_soil_probe_sample_record_t *sample,
                                       int64_t *sample_age_ms)
{
    (void)sample;
    (void)sample_age_ms;
    return false;
}
esp_err_t opp_soil_probe_monitor_last_error(void) { return ESP_ERR_NOT_SUPPORTED; }

#endif
