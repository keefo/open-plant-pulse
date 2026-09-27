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

#include "battery_monitor.h"
#include "bthome_broadcaster.h"
#include "bthome_payload.h"
#include "clock_sync.h"
#include "device_config_store.h"
#include "device_identity.h"
#include "firmware_update.h"
#include "force_report.h"
#include "report_delivery.h"
#include "report_id.h"
#include "sht45.h"
#include "sht45_monitor.h"
#include "soil_probe.h"
#include "web_ui.h"
#include "web_ui_config.h"

static const char *TAG = "plant_pulse";

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
 * that door without inventing a measurement: the hub sees the same report ID,
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
/* Below this a window is not worth starting the Bluetooth stack for. */
#define MINIMUM_WINDOW_MS 300
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

static void set_air(opp_bthome_report_t *report, const opp_sht45_sample_t *air)
{
    report->air_available = true;
    report->air_temperature_tenths_celsius =
        (int16_t)(air->air_temperature_c * 10.0f +
                  (air->air_temperature_c >= 0.0f ? 0.5f : -0.5f));
    report->air_humidity_hundredths_percent =
        (uint16_t)(air->air_humidity_percent * 100.0f + 0.5f);
}

static void set_soil(opp_bthome_report_t *report, const opp_soil_probe_reading_t *soil)
{
    report->soil_available = true;
    report->soil_moisture_tenths_percent = soil->moisture_tenths_percent;
    report->soil_temperature_tenths_celsius = soil->temperature_tenths_celsius;
    report->conductivity_us_cm = soil->conductivity_us_cm;
    report->soil_extras_available = true;
    report->ph_tenths = soil->ph_tenths;
    report->nitrogen_mg_kg = soil->nitrogen_mg_kg;
    report->phosphorus_mg_kg = soil->phosphorus_mg_kg;
    report->potassium_mg_kg = soil->potassium_mg_kg;
}

#if !CONFIG_OPP_PRODUCTION_LIFECYCLE
/* The production cycle does not run the battery monitor yet. */
static void set_battery(opp_bthome_report_t *report)
{
    opp_battery_status_t battery;
    int64_t age_ms;
    if (!opp_battery_monitor_get(&battery, &age_ms) || battery.millivolts <= 0) {
        return;
    }
    float percent = battery.percent + 0.5f;
    if (percent < 0.0f) {
        percent = 0.0f;
    } else if (percent > 100.0f) {
        percent = 100.0f;
    }
    report->battery_available = true;
    report->battery_percent = (uint8_t)percent;
    report->battery_millivolts =
        battery.millivolts > UINT16_MAX ? UINT16_MAX : (uint16_t)battery.millivolts;
    report->battery_charging = battery.flow == OPP_BATTERY_CHARGING;
}
#endif

/* Only a trusted clock is stamped; without one the report carries no time
 * rather than a guess (see "Acquisition timestamp" in protocol/README.md). */
static void set_timestamp(opp_bthome_report_t *report)
{
    int64_t unix_time_ms;
    if (opp_clock_capture_timestamp(&unix_time_ms) && unix_time_ms > 0) {
        report->timestamp_valid = true;
        report->timestamp_unix_s = (uint32_t)(unix_time_ms / 1000);
    }
}

/* Take a report ID and encode both packets. False when there is no ID to give
 * or nothing valid to send; the ID is spent either way, which only leaves a
 * gap. */
static bool encode_report(opp_bthome_report_t *report, opp_queued_report_t *encoded)
{
    *encoded = (opp_queued_report_t){0};
    report->report_id = opp_report_id_next();
    if (report->report_id == 0) {
        return false;
    }
    encoded->report_id = report->report_id;
    encoded->packet1_size = (uint8_t)opp_bthome_encode_packet1(report, encoded->packet1);
    encoded->packet2_size =
        (uint8_t)opp_bthome_encode_packet2(report, encoded->packet2);
    return encoded->packet1_size > 0 && encoded->packet2_size > 0;
}

static esp_err_t broadcast_report_for(const char *local_name, const opp_queued_report_t *encoded,
                                      uint32_t window_ms)
{
    return opp_bthome_broadcast(
        local_name,
        encoded->packet1,
        encoded->packet1_size,
        encoded->packet2_size > 0 ? encoded->packet2 : NULL,
        encoded->packet2_size,
        window_ms,
#if CONFIG_OPP_PRODUCTION_LIFECYCLE
        CONFIG_OPP_BTHOME_ADVERTISEMENT_INTERVAL_MS);
#else
        CONFIG_OPP_DEVELOPMENT_ADVERTISEMENT_INTERVAL_MS);
#endif
}

/* Keep a new report until the hub acknowledges it. Only a sensor that belongs to
 * a hub queues: nobody could acknowledge anything for an unclaimed one. */
static bool queue_report(const opp_queued_report_t *encoded)
{
    if (opp_delivery_push(encoded)) {
        return true;
    }
    ESP_LOGW(TAG, "Report %lu not kept: %lu of %lu reports already await the hub",
             (unsigned long)encoded->report_id, (unsigned long)opp_delivery_depth(),
             (unsigned long)opp_delivery_capacity());
    return false;
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

static bool acquire_air_sample(opp_bthome_report_t *report)
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

    set_air(report, &air);
    ESP_LOGI(TAG, "Fresh SHT45 sample temperature=%.2f C humidity=%.2f%%",
             air.air_temperature_c, air.air_humidity_percent);
    return true;
}

static bool acquire_soil_sample(opp_bthome_report_t *report)
{
    opp_soil_probe_reading_t soil;
    esp_err_t error = opp_soil_probe_set_power(true);
    if (error == ESP_OK) {
        error = opp_soil_probe_init();
    }
    if (error == ESP_OK) {
        error = opp_soil_probe_read(&soil);
    }
    esp_err_t cleanup_error = opp_soil_probe_deinit();
    /* Every path, success or not, leaves the probe unpowered for the sleep. */
    esp_err_t power_error = opp_soil_probe_set_power(false);
    if (cleanup_error == ESP_OK) {
        cleanup_error = power_error;
    }
    if (error == ESP_OK && cleanup_error != ESP_OK) {
        error = cleanup_error;
    }
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "Soil probe acquisition unavailable: %s", esp_err_to_name(error));
        return false;
    }

    set_soil(report, &soil);
    ESP_LOGI(TAG, "Fresh soil sample moisture=%.1f%% temperature=%.1f C ec=%u uS/cm",
             soil.moisture_tenths_percent / 10.0, soil.temperature_tenths_celsius / 10.0,
             soil.conductivity_us_cm);
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
    ESP_ERROR_CHECK(opp_firmware_update_init());
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

    esp_err_t id_error = opp_report_id_init();
    if (id_error == ESP_OK) {
        id_error = opp_delivery_init();
    }
    if (id_error != ESP_OK) {
        ESP_LOGE(TAG, "Report IDs or queue unavailable: %s", esp_err_to_name(id_error));
        enter_deep_sleep();
    }

    opp_bthome_report_t report = {0};
    const bool air_available = acquire_air_sample(&report);
    const bool soil_available = acquire_soil_sample(&report);
    if (!air_available && !soil_available) {
        enter_deep_sleep();
    }
    set_timestamp(&report);

    opp_queued_report_t encoded;
    char local_name[OPP_BTHOME_LOCAL_NAME_SIZE];
    if (!encode_report(&report, &encoded) ||
        !opp_bthome_format_local_name(device_id(), local_name)) {
        ESP_LOGE(TAG, "Could not encode BTHome report or stable identity");
        enter_deep_sleep();
    }
    /* An owned sensor advertises its oldest unacknowledged report, which may be
     * an earlier one: each wake is the retry. */
    if (opp_device_identity_is_onboarded()) {
        queue_report(&encoded);
        opp_delivery_newest(&encoded);
    }

    ESP_LOGI(TAG, "Advertising %s report %lu for %d ms", local_name,
             (unsigned long)encoded.report_id, CONFIG_OPP_BTHOME_ADVERTISEMENT_WINDOW_MS);
    esp_err_t error = broadcast_report_for(local_name, &encoded,
                                         CONFIG_OPP_BTHOME_ADVERTISEMENT_WINDOW_MS);
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "BTHome advertising cycle failed: %s", esp_err_to_name(error));
    } else {
        ESP_LOGI(TAG, "Bounded BTHome advertising cycle complete");
        opp_firmware_update_confirm();
    }
    enter_deep_sleep();
}
#endif

#if !CONFIG_OPP_PRODUCTION_LIFECYCLE
/* What was last advertised, for reachability windows to repeat unchanged. */
/* The latest report of a sensor no hub owns, for reachability windows to
 * repeat. An owned sensor repeats its queue instead. */
static opp_queued_report_t last_unqueued;

/* Mains power removes the reason to be frugal, so a plugged-in sensor is always
 * reachable. On battery it is reachable only while somebody is working with it. */
static bool should_stay_reachable(void)
{
#if CONFIG_OPP_ALWAYS_REACHABLE
    /* On the bench, a sensor that goes quiet for half an hour between reports
     * stalls every update and settings change sent to it. */
    return true;
#endif
    if (usb_serial_jtag_is_connected()) {
        return true;
    }
    /* An update in progress outranks any reporting interval: the hub is
     * watching it, and a sensor that went quiet halfway through would leave
     * whoever pressed the button with nothing to look at. */
    if (opp_firmware_update_in_progress()) {
        return true;
    }
    const int64_t last = opp_bthome_last_connection_ms();
    if (last == 0) {
        return false;
    }
    return (esp_timer_get_time() / 1000) - last < RESPONSIVE_WINDOW_MS;
}

static void advertise_onboarding_beacon(const char *local_name, uint32_t window_ms);
static TickType_t report_interval_ticks(void);

/* A report's own window ends half a second before the next one is due. */
static uint32_t report_window_ms(void)
{
    const uint32_t interval_ms = (uint32_t)report_interval_ticks() * portTICK_PERIOD_MS;
    const uint32_t limit_ms = interval_ms > 2 * MINIMUM_WINDOW_MS ? interval_ms - 500U
                                                                  : MINIMUM_WINDOW_MS;
    return limit_ms < CONFIG_OPP_BTHOME_ADVERTISEMENT_WINDOW_MS
               ? limit_ms
               : CONFIG_OPP_BTHOME_ADVERTISEMENT_WINDOW_MS;
}

/* Keep the door open between reports without inventing a measurement.
 *
 * An owned sensor advertises its oldest unacknowledged report, which is also
 * how it retries, or a beacon once the hub has everything: repeating a report
 * the hub already acknowledged would only make it connect to acknowledge it
 * again. An unclaimed sensor repeats its latest report, whose report ID tells
 * the hub it is nothing new. */
static void advertise_reachable_window(const char *local_name, uint32_t window_ms)
{
    opp_queued_report_t report;
    const bool have_report = opp_device_identity_is_onboarded()
                                 ? opp_delivery_newest(&report)
                                 : (report = last_unqueued, report.packet1_size > 0);
    if (!have_report) {
        advertise_onboarding_beacon(local_name, window_ms);
        return;
    }
    esp_err_t error = broadcast_report_for(local_name, &report, window_ms);
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "Reachability window failed: %s", esp_err_to_name(error));
    }
}

/* Identity only: the hub learns the sensor exists and can adopt it, and learns
 * nothing about a plant, because there is nothing to tell. */
static void advertise_onboarding_beacon(const char *local_name, uint32_t window_ms)
{
    opp_queued_report_t beacon = {0};
    beacon.packet1_size = (uint8_t)opp_bthome_encode_beacon(beacon.packet1);
    if (beacon.packet1_size == 0) {
        ESP_LOGE(TAG, "Could not encode the onboarding beacon");
        return;
    }
    esp_err_t error = broadcast_report_for(local_name, &beacon, window_ms);
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "Onboarding beacon failed: %s", esp_err_to_name(error));
    }
}

static void broadcast_development_report(const char *local_name, uint32_t force_request_id)
{
    const bool forced = force_request_id != 0;
    opp_sht45_sample_record_t air;
    int64_t air_age_ms;
    const bool air_available = opp_sht45_monitor_get_latest(&air, &air_age_ms);
    opp_soil_probe_sample_record_t soil;
    int64_t soil_age_ms;
    const bool soil_available = opp_soil_probe_monitor_get_latest(&soil, &soil_age_ms);
    if (!air_available && !soil_available) {
        /* Say "I am here with nothing to report" rather than saying nothing.
         *
         * Silence cannot be told apart from a flat battery or a sensor out of
         * range, so a hub waiting for a first reading waits for ever and never
         * learns why. A beacon carries no measurement, which is the truth when
         * the probe is absent or broken, and lets the hub say so. It is also
         * what makes an unowned sensor findable in the first place. */
        ESP_LOGW(TAG, "No SHT45 or soil sample; sending a beacon with no measurement");
        advertise_onboarding_beacon(local_name, CONFIG_OPP_BTHOME_ADVERTISEMENT_WINDOW_MS);
        if (forced) {
            opp_force_report_failed(force_request_id, OPP_FORCE_REPORT_FAILURE_NO_SAMPLE);
        }
        return;
    }

    opp_bthome_report_t report = {0};
    if (air_available) {
        set_air(&report, &air.values);
    }
    if (soil_available) {
        set_soil(&report, &soil.values);
    }
    set_battery(&report);
    set_timestamp(&report);
    opp_queued_report_t encoded;
    if (!encode_report(&report, &encoded)) {
        ESP_LOGW(TAG, "Skipping %s BLE report: no report ID or encoding failed",
                 forced ? "forced" : "scheduled");
        if (forced) {
            opp_force_report_failed(force_request_id, OPP_FORCE_REPORT_FAILURE_ENCODING);
        }
        return;
    }
    const bool queued = opp_device_identity_is_onboarded();
    if (queued) {
        if (!queue_report(&encoded)) {
            if (forced) {
                opp_force_report_failed(force_request_id, OPP_FORCE_REPORT_FAILURE_QUEUE_FULL);
            }
            advertise_reachable_window(local_name, CONFIG_OPP_BTHOME_ADVERTISEMENT_WINDOW_MS);
            return;
        }
    } else {
        last_unqueued = encoded;
    }
    if (forced) {
        opp_force_report_started(force_request_id, report.report_id);
    }

    ESP_LOGI(TAG, "%s BLE report %s report %lu (air %s, soil %s, battery %s, %s)",
             forced ? "Forced" : "Scheduled", local_name, (unsigned long)report.report_id,
             air_available ? "yes" : "no", soil_available ? "yes" : "no",
             report.battery_available ? "yes" : "no",
             report.timestamp_valid ? "timestamped" : "no clock");
    /* An owned sensor puts its oldest unacknowledged report on air, which is
     * this one unless earlier ones still wait. */
    if (queued) {
        opp_delivery_newest(&encoded);
    }
    esp_err_t error = broadcast_report_for(local_name, &encoded, report_window_ms());
    if (forced) {
        opp_force_report_finished(force_request_id, error, queued);
    }
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "Development BTHome advertising failed: %s",
                 esp_err_to_name(error));
    }
}

/* An unclaimed sensor announces itself often, not on its reporting interval.
 * Somebody is looking for it in a list that has to feel live: on a thirty-minute
 * cadence that list cannot tell a sensor that was switched off from one that is
 * simply between beacons. The sensor is powered and attended during setup, so
 * the cost is acceptable, and it ends the moment the sensor belongs to a hub. */
static TickType_t report_interval_ticks(void)
{
    if (!opp_device_identity_is_onboarded()) {
        return pdMS_TO_TICKS(ONBOARDING_BEACON_INTERVAL_MS);
    }
    opp_device_config_t config;
    opp_device_config_store_get(&config);
    return config.revision == 0 ? pdMS_TO_TICKS(DEVELOPMENT_REPORT_INTERVAL_MS)
                                : pdMS_TO_TICKS(1000U) * config.reporting_interval_seconds;
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
    advertise_onboarding_beacon(local_name, CONFIG_OPP_BTHOME_ADVERTISEMENT_WINDOW_MS);
    /* Being heard is what this sensor is for, so an image that has got this far
     * works. A newly installed one is kept from here on; one that crashed before
     * reaching this line is replaced by its predecessor at the next reset. */
    opp_firmware_update_confirm();

    /* The schedule counts from the last scheduled report, and the interval is
     * read afresh on every pass. Fixing a deadline once per report meant an
     * interval the hub changed from 30 minutes to 5 seconds waited out the
     * rest of the 30 minutes before it took effect. */
    /* Reports keep to the schedule the hub set: report n is due at the start
     * plus n intervals, so one that runs late does not push every later one
     * back. The interval is read afresh on every pass, so a change from the
     * hub applies at once rather than after the old interval ran out. */
    TickType_t next_report = xTaskGetTickCount() + report_interval_ticks();
    while (true) {
        uint32_t force_request_id;
        if (opp_force_report_wait(0, &force_request_id)) {
            /* A forced report leaves the schedule where it was. */
            broadcast_development_report(local_name, force_request_id);
            continue;
        }
        const TickType_t interval = report_interval_ticks();
        TickType_t now = xTaskGetTickCount();
        if ((int32_t)(next_report - now) > (int32_t)interval) {
            /* The interval was shortened: count it from now. */
            next_report = now + interval;
        }
        int32_t ticks_remaining = (int32_t)(next_report - now);
        if (ticks_remaining <= 0) {
            broadcast_development_report(local_name, 0);
            next_report += interval;
            now = xTaskGetTickCount();
            if ((int32_t)(now - next_report) >= 0) {
                /* More than an interval behind (a long connection, say):
                 * start afresh rather than burst to catch up. */
                next_report = now + interval;
            }
            continue;
        }
        TickType_t wait_ticks = (TickType_t)ticks_remaining;
        uint32_t window_ms = 0;
        if (should_stay_reachable()) {
            /* A window ends before the next report is due rather than
             * delay it: the hub page is meant to move at the interval. */
            const uint32_t until_due_ms = (uint32_t)ticks_remaining * portTICK_PERIOD_MS;
            window_ms = until_due_ms < CONFIG_OPP_BTHOME_ADVERTISEMENT_WINDOW_MS
                            ? until_due_ms
                            : CONFIG_OPP_BTHOME_ADVERTISEMENT_WINDOW_MS;
            if (window_ms >= MINIMUM_WINDOW_MS) {
                const TickType_t reachable_ticks = pdMS_TO_TICKS(REACHABLE_INTERVAL_MS);
                if (reachable_ticks < wait_ticks) {
                    wait_ticks = reachable_ticks;
                }
            } else {
                window_ms = 0;
            }
        }
        if (opp_force_report_wait(wait_ticks, &force_request_id)) {
            broadcast_development_report(local_name, force_request_id);
        } else if (window_ms > 0) {
            advertise_reachable_window(local_name, window_ms);
        }
        /* Otherwise the report is due; the next pass makes it. */
    }
}
#endif

void app_main(void)
{
#if CONFIG_OPP_PRODUCTION_LIFECYCLE
    run_production_cycle();
#else
    ESP_ERROR_CHECK(opp_firmware_update_init());
    ESP_ERROR_CHECK(web_ui_config_init());
    esp_err_t report_id_error = opp_report_id_init();
    if (report_id_error == ESP_OK) {
        report_id_error = opp_delivery_init();
    }
    if (report_id_error != ESP_OK) {
        ESP_LOGE(TAG, "Report IDs unavailable; only beacons can be sent: %s",
                 esp_err_to_name(report_id_error));
    }
    ESP_ERROR_CHECK(opp_device_identity_init());
    ESP_ERROR_CHECK(opp_device_config_store_init());
    ESP_ERROR_CHECK(opp_force_report_init());
    ESP_ERROR_CHECK(opp_clock_sync_init());
    esp_err_t sht45_error = opp_sht45_monitor_start(web_ui_config_sht45_enabled());
    if (sht45_error != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start SHT45 monitor: %s", esp_err_to_name(sht45_error));
    }
#if CONFIG_OPP_BATTERY_MONITOR_ENABLED
    esp_err_t battery_error = opp_battery_monitor_start();
    if (battery_error != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start battery monitor: %s", esp_err_to_name(battery_error));
    }
#endif
#if CONFIG_OPP_SOIL_PROBE_ENABLED
    esp_err_t soil_error = opp_soil_probe_monitor_start(web_ui_config_soil_probe_enabled());
    if (soil_error != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start soil probe monitor: %s", esp_err_to_name(soil_error));
    }
#endif
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