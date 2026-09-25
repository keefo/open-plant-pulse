#include "esp_app_desc.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "driver/usb_serial_jtag.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

#include "bthome_broadcaster.h"
#include "bthome_payload.h"
#include "clock_sync.h"
#include "device_config_store.h"
#include "device_identity.h"
#include "force_report.h"
#include "sht45.h"
#include "sht45_monitor.h"
#include "web_ui.h"
#include "web_ui_config.h"

static const char *TAG = "plant_pulse";
static RTC_DATA_ATTR uint8_t bthome_packet_id;

#define DEVELOPMENT_REPORT_INTERVAL_MS 5000
/* Three seconds, which is also the advertising window, so an unclaimed sensor
 * effectively announces itself continuously. That is what lets the hub notice it
 * appearing or disappearing inside ten seconds. It costs power, and it stops the
 * moment the sensor belongs to a hub, which is before power matters. */
#define ONBOARDING_BEACON_INTERVAL_MS 3000
/* How often an owned sensor makes itself reachable between reports.
 *
 * Reachability is not reporting. A hub can only reach a sensor while it is
 * advertising, so a sensor that advertises every half hour takes half an hour to
 * accept a settings change. Re-advertising the last payload unchanged reopens
 * that door without inventing a measurement: the hub sees the same packet ID,
 * stores nothing, and can connect. */
/* A short gap, not a long one. Each window already costs the Bluetooth stack
 * starting and stopping, so spacing windows out adds that cost to the delay
 * rather than saving anything; back to back, a sensor is reachable almost
 * continuously and a change lands in a second or two. */
#define REACHABLE_INTERVAL_MS 250
/* Somebody who has just changed one setting usually changes another, so a
 * sensor stays reachable for a while after any hub connection even on battery.
 * Outside that window a battery sensor goes back to its reporting interval,
 * because being reachable costs the same whether or not anyone is listening. */
#define RESPONSIVE_WINDOW_MS 120000
#define DEVELOPMENT_BROADCAST_TASK_STACK_SIZE 4096
#define DEVELOPMENT_BROADCAST_TASK_PRIORITY 4

static uint64_t device_id(void)
{
    uint8_t mac[6];
    ESP_ERROR_CHECK(esp_efuse_mac_get_default(mac));
    uint64_t value = 0;
    for (size_t index = 0; index < sizeof(mac); index++) {
        value = (value << 8U) | mac[index];
    }
    return value;
}

static void set_air_sample(opp_bthome_sample_t *sample, const opp_sht45_sample_t *air)
{
    sample->air_available = true;
    sample->air_temperature_tenths_celsius =
        (int16_t)(air->air_temperature_c * 10.0f +
                  (air->air_temperature_c >= 0.0f ? 0.5f : -0.5f));
    sample->air_humidity_hundredths_percent =
        (uint16_t)(air->air_humidity_percent * 100.0f + 0.5f);
}

#if CONFIG_OPP_PRODUCTION_LIFECYCLE
static esp_err_t initialise_nvs(void)
{
    esp_err_t error = nvs_flash_init();
    if (error == ESP_ERR_NVS_NO_FREE_PAGES || error == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        error = nvs_flash_erase();
        if (error != ESP_OK) {
            return error;
        }
        error = nvs_flash_init();
    }
    return error;
}

static bool acquire_air_sample(opp_bthome_sample_t *sample)
{
    opp_sht45_sample_t air;
    esp_err_t error = opp_sht45_init();
    if (error == ESP_OK) {
        error = opp_sht45_probe();
    }
    if (error == ESP_OK) {
        error = opp_sht45_read(&air);
    }
    esp_err_t cleanup_error = opp_sht45_deinit();
    if (error == ESP_OK && cleanup_error != ESP_OK) {
        error = cleanup_error;
    }
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "SHT45 acquisition unavailable: %s", esp_err_to_name(error));
        return false;
    }

    set_air_sample(sample, &air);
    ESP_LOGI(TAG, "Fresh SHT45 sample temperature=%.2f C humidity=%.2f%%",
             air.air_temperature_c, air.air_humidity_percent);
    return true;
}

static void enter_deep_sleep(void)
{
    opp_device_config_t config;
    opp_device_config_store_get(&config);
    const uint64_t sleep_us =
        (uint64_t)config.reporting_interval_seconds * UINT64_C(1000000);
    esp_err_t error = esp_sleep_enable_timer_wakeup(sleep_us);
    if (error != ESP_OK) {
        ESP_LOGE(TAG, "Could not configure timer wake-up: %s", esp_err_to_name(error));
        esp_restart();
    }
    ESP_LOGI(TAG, "Entering deep sleep for %lu second(s)",
             (unsigned long)config.reporting_interval_seconds);
    vTaskDelay(pdMS_TO_TICKS(20));
    esp_deep_sleep_start();
}

static void run_production_cycle(void)
{
    ESP_LOGI(TAG, "Open Plant Pulse firmware %s production wake cycle",
             esp_app_get_description()->version);
    esp_err_t nvs_error = initialise_nvs();
    esp_err_t identity_error = opp_device_identity_init();
    if (identity_error != ESP_OK) {
        ESP_LOGE(TAG, "Failed to load device identity: %s", esp_err_to_name(identity_error));
    }
    esp_err_t config_error = opp_device_config_store_init();
    if (nvs_error != ESP_OK) {
        ESP_LOGE(TAG, "NVS initialization failed: %s", esp_err_to_name(nvs_error));
        enter_deep_sleep();
    }
    if (config_error != ESP_OK) {
        ESP_LOGE(TAG, "Device configuration initialization failed: %s",
                 esp_err_to_name(config_error));
        enter_deep_sleep();
    }

    opp_bthome_sample_t sample = {.packet_id = bthome_packet_id++};
    if (!acquire_air_sample(&sample)) {
        enter_deep_sleep();
    }

    uint8_t payload[OPP_BTHOME_V2_SERVICE_DATA_MAX_SIZE];
    const size_t payload_size = opp_bthome_encode_v2_service_data(&sample, payload);
    char local_name[OPP_BTHOME_LOCAL_NAME_SIZE];
    if (payload_size == 0 || !opp_bthome_format_local_name(device_id(), local_name)) {
        ESP_LOGE(TAG, "Could not encode BTHome payload or stable identity");
        enter_deep_sleep();
    }

    ESP_LOGI(TAG, "Advertising %s packet %u for %d ms", local_name, sample.packet_id,
             CONFIG_OPP_BTHOME_ADVERTISEMENT_WINDOW_MS);
    esp_err_t error = opp_bthome_broadcast(
        local_name,
        payload,
        payload_size,
        CONFIG_OPP_BTHOME_ADVERTISEMENT_WINDOW_MS,
        CONFIG_OPP_BTHOME_ADVERTISEMENT_INTERVAL_MS);
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "BTHome advertising cycle failed: %s", esp_err_to_name(error));
    } else {
        ESP_LOGI(TAG, "Bounded BTHome advertising cycle complete");
    }
    enter_deep_sleep();
}
#endif

#if !CONFIG_OPP_PRODUCTION_LIFECYCLE
static uint8_t last_payload[OPP_BTHOME_V2_SERVICE_DATA_MAX_SIZE];
static size_t last_payload_size;

/* Mains power removes the reason to be frugal, so a plugged-in sensor is always
 * reachable. On battery it is reachable only while somebody is working with it. */
static bool should_stay_reachable(void)
{
    if (usb_serial_jtag_is_connected()) {
        return true;
    }
    const int64_t last = opp_bthome_last_connection_ms();
    if (last == 0) {
        return false;
    }
    return (esp_timer_get_time() / 1000) - last < RESPONSIVE_WINDOW_MS;
}

/* Re-advertise what was last sent, unchanged.
 *
 * The same packet ID is the point: the hub recognises a duplicate and stores no
 * reading, so opening the door often does not fill the database with rows that
 * say nothing new. */
static void advertise_onboarding_beacon(const char *local_name);

static void advertise_reachable_window(const char *local_name)
{
    if (last_payload_size == 0) {
        /* Nothing has been sent yet, so there is nothing to repeat. Announce
         * presence instead: a beacon stores no reading, and it gives the next
         * window something to echo. */
        advertise_onboarding_beacon(local_name);
        return;
    }
    esp_err_t error = opp_bthome_broadcast(
        local_name,
        last_payload,
        last_payload_size,
        CONFIG_OPP_BTHOME_ADVERTISEMENT_WINDOW_MS,
        CONFIG_OPP_BTHOME_ADVERTISEMENT_INTERVAL_MS);
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "Reachability window failed: %s", esp_err_to_name(error));
    }
}

/* Identity only: the hub learns the sensor exists and can adopt it, and learns
 * nothing about a plant, because there is nothing to tell. */
static void advertise_onboarding_beacon(const char *local_name)
{
    uint8_t payload[OPP_BTHOME_V2_SERVICE_DATA_MAX_SIZE];
    const size_t payload_size = opp_bthome_encode_v2_beacon(bthome_packet_id++, payload);
    if (payload_size == 0) {
        ESP_LOGE(TAG, "Could not encode the onboarding beacon");
        return;
    }
    memcpy(last_payload, payload, payload_size);
    last_payload_size = payload_size;
    esp_err_t error = opp_bthome_broadcast(
        local_name,
        payload,
        payload_size,
        CONFIG_OPP_BTHOME_ADVERTISEMENT_WINDOW_MS,
        CONFIG_OPP_BTHOME_ADVERTISEMENT_INTERVAL_MS);
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "Onboarding beacon failed: %s", esp_err_to_name(error));
    }
}

static void broadcast_development_report(const char *local_name, uint32_t force_request_id)
{
    const bool forced = force_request_id != 0;
    opp_sht45_sample_record_t air;
    int64_t sample_age_ms;
    if (!opp_sht45_monitor_get_latest(&air, &sample_age_ms)) {
        /* Say "I am here with nothing to report" rather than saying nothing.
         *
         * Silence cannot be told apart from a flat battery or a sensor out of
         * range, so a hub waiting for a first reading waits for ever and never
         * learns why. A beacon carries no measurement, which is the truth when
         * the probe is absent or broken, and lets the hub say so. It is also
         * what makes an unowned sensor findable in the first place. */
        ESP_LOGW(TAG, "No SHT45 sample; sending a beacon with no measurement");
        advertise_onboarding_beacon(local_name);
        if (forced) {
            opp_force_report_failed(force_request_id, OPP_FORCE_REPORT_FAILURE_NO_SAMPLE);
        }
        return;
    }

    opp_bthome_sample_t sample = {
        .packet_id = bthome_packet_id++,
        .button_event = forced,
    };
    set_air_sample(&sample, &air.values);
    uint8_t payload[OPP_BTHOME_V2_SERVICE_DATA_MAX_SIZE];
    const size_t payload_size = opp_bthome_encode_v2_service_data(&sample, payload);
    if (payload_size == 0) {
        ESP_LOGW(TAG, "Skipping %s BLE report: payload encoding failed",
                 forced ? "forced" : "scheduled");
        if (forced) {
            opp_force_report_failed(force_request_id, OPP_FORCE_REPORT_FAILURE_ENCODING);
        }
        return;
    }
    memcpy(last_payload, payload, payload_size);
    last_payload_size = payload_size;
    if (forced) {
        opp_force_report_started(force_request_id, sample.packet_id);
    }

    ESP_LOGI(TAG, "%s BLE report %s packet %u (sample age %lld ms)",
             forced ? "Forced" : "Scheduled", local_name, sample.packet_id,
             (long long)sample_age_ms);
    esp_err_t error = opp_bthome_broadcast(
        local_name,
        payload,
        payload_size,
        CONFIG_OPP_BTHOME_ADVERTISEMENT_WINDOW_MS,
        CONFIG_OPP_BTHOME_ADVERTISEMENT_INTERVAL_MS);
    if (forced) {
        opp_force_report_finished(force_request_id, error);
    }
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "Development BTHome advertising failed: %s",
                 esp_err_to_name(error));
    }
}

static void development_broadcast_task(void *context)
{
    (void)context;
    char local_name[OPP_BTHOME_LOCAL_NAME_SIZE];
    if (!opp_bthome_format_local_name(device_id(), local_name)) {
        ESP_LOGE(TAG, "Could not format stable BTHome identity");
        vTaskDelete(NULL);
        return;
    }

    /* Announce immediately rather than after a first interval, claimed or not.
     *
     * An owned sensor used to wait a whole reporting interval before saying
     * anything, so a reboot made it disappear from its hub for up to half an
     * hour, and there was nothing for a reachability window to repeat. Being
     * heard from is the first thing anyone wants after power-on. */
    advertise_onboarding_beacon(local_name);

    TickType_t next_report = xTaskGetTickCount();
    while (true) {
        opp_device_config_t config;
        opp_device_config_store_get(&config);
        /* An unclaimed sensor announces itself often, not on its reporting
         * interval. Somebody is looking for it in a list that has to feel live:
         * on a thirty-minute cadence that list cannot tell a sensor that was
         * switched off from one that is simply between beacons. The sensor is
         * powered and attended during setup, so the cost is acceptable, and it
         * ends the moment the sensor belongs to a hub. */
        const TickType_t report_interval_ticks =
            !opp_device_identity_is_onboarded()
                ? pdMS_TO_TICKS(ONBOARDING_BEACON_INTERVAL_MS)
                : (config.revision == 0
                       ? pdMS_TO_TICKS(DEVELOPMENT_REPORT_INTERVAL_MS)
                       : pdMS_TO_TICKS(1000U) * config.reporting_interval_seconds);
        next_report += report_interval_ticks;
        while (true) {
            uint32_t force_request_id;
            if (opp_force_report_wait(0, &force_request_id)) {
                broadcast_development_report(local_name, force_request_id);
                continue;
            }
            const TickType_t now = xTaskGetTickCount();
            const int32_t ticks_remaining = (int32_t)(next_report - now);
            if (ticks_remaining <= 0) {
                broadcast_development_report(local_name, 0);
                break;
            }
            TickType_t wait_ticks = (TickType_t)ticks_remaining;
            bool reachability_tick = false;
            if (should_stay_reachable()) {
                const TickType_t reachable_ticks = pdMS_TO_TICKS(REACHABLE_INTERVAL_MS);
                if (reachable_ticks < wait_ticks) {
                    wait_ticks = reachable_ticks;
                    reachability_tick = true;
                }
            }
            if (opp_force_report_wait(wait_ticks, &force_request_id)) {
                broadcast_development_report(local_name, force_request_id);
            } else if (reachability_tick) {
                advertise_reachable_window(local_name);
            } else {
                broadcast_development_report(local_name, 0);
                break;
            }
        }
    }
}
#endif

void app_main(void)
{
#if CONFIG_OPP_PRODUCTION_LIFECYCLE
    run_production_cycle();
#else
    ESP_ERROR_CHECK(web_ui_config_init());
    ESP_ERROR_CHECK(opp_device_identity_init());
    ESP_ERROR_CHECK(opp_device_config_store_init());
    ESP_ERROR_CHECK(opp_force_report_init());
    ESP_ERROR_CHECK(opp_clock_sync_init());
    esp_err_t sht45_error = opp_sht45_monitor_start(web_ui_config_sht45_enabled());
    if (sht45_error != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start SHT45 monitor: %s", esp_err_to_name(sht45_error));
    }
    BaseType_t broadcast_task_created = xTaskCreate(
        development_broadcast_task,
        "bthome_report",
        DEVELOPMENT_BROADCAST_TASK_STACK_SIZE,
        NULL,
        DEVELOPMENT_BROADCAST_TASK_PRIORITY,
        NULL);
    if (broadcast_task_created != pdPASS) {
        ESP_LOGE(TAG, "Failed to start development BTHome reporting task");
    }
    ESP_ERROR_CHECK(web_ui_start());
    opp_device_config_t config;
    opp_device_config_store_get(&config);
    ESP_LOGI(TAG, "Open Plant Pulse firmware %s starting; awake BLE reporting every %lu second(s)",
             esp_app_get_description()->version,
             (unsigned long)(config.revision == 0
                                 ? DEVELOPMENT_REPORT_INTERVAL_MS / 1000U
                                 : config.reporting_interval_seconds));
#endif
}