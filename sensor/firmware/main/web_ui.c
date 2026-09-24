#include "web_ui.h"

#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "cJSON.h"
#include "clock_sync.h"
#include "device_config_store.h"
#include "driver/temperature_sensor.h"
#include "driver/usb_serial_jtag.h"
#include "esp_app_desc.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_log_write.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "force_report.h"
#include "lwip/ip4_addr.h"
#include "power_source.h"
#include "sht45_monitor.h"
#include "web_ui_config.h"

#define LOG_RING_SIZE (16 * 1024)
#define LOG_LINE_SIZE 512
#define RESTART_REQUEST_SIZE 64
#define RESTART_DELAY_US (750 * 1000)
#define STATUS_JSON_MAX 3072

static const char *TAG = "web_ui";
static char log_ring[LOG_RING_SIZE];
static size_t log_head;
static size_t log_length;
static portMUX_TYPE log_lock = portMUX_INITIALIZER_UNLOCKED;
static vprintf_like_t console_vprintf;
static httpd_handle_t http_server;
static char station_ip[IP4ADDR_STRLEN_MAX] = "disconnected";
static char station_mac[18] = "unknown";
static char station_ssid[33] = "unknown";
static bool station_connected;
static temperature_sensor_handle_t chip_temperature_sensor;
static esp_timer_handle_t restart_timer;
#if CONFIG_OPP_CLOCK_SYNC_ENABLED && !CONFIG_OPP_WEB_UI_ENABLED
static esp_timer_handle_t clock_wifi_timer;
static bool clock_wifi_window_open;
#endif

extern const uint8_t index_html_start[] asm("_binary_index_html_start");
extern const uint8_t index_html_end[] asm("_binary_index_html_end");
extern const uint8_t app_css_start[] asm("_binary_app_css_start");
extern const uint8_t app_css_end[] asm("_binary_app_css_end");
extern const uint8_t app_js_start[] asm("_binary_app_js_start");
extern const uint8_t app_js_end[] asm("_binary_app_js_end");

typedef struct {
    const uint8_t *start;
    const uint8_t *end;
    const char *content_type;
} web_asset_t;

static web_asset_t index_asset = {
    .start = index_html_start,
    .end = index_html_end,
    .content_type = "text/html; charset=utf-8",
};
static web_asset_t css_asset = {
    .start = app_css_start,
    .end = app_css_end,
    .content_type = "text/css; charset=utf-8",
};
static web_asset_t js_asset = {
    .start = app_js_start,
    .end = app_js_end,
    .content_type = "application/javascript; charset=utf-8",
};

static void append_log(const char *data, size_t length)
{
    portENTER_CRITICAL(&log_lock);
    for (size_t index = 0; index < length; index++) {
        log_ring[log_head] = data[index];
        log_head = (log_head + 1) % LOG_RING_SIZE;
        if (log_length < LOG_RING_SIZE) {
            log_length++;
        }
    }
    portEXIT_CRITICAL(&log_lock);
}

static int web_ui_vprintf(const char *format, va_list arguments)
{
    va_list console_arguments;
    va_list buffer_arguments;
    va_copy(console_arguments, arguments);
    va_copy(buffer_arguments, arguments);

    int result = console_vprintf(format, console_arguments);
    char line[LOG_LINE_SIZE];
    int written = vsnprintf(line, sizeof(line), format, buffer_arguments);

    va_end(console_arguments);
    va_end(buffer_arguments);

    if (written > 0) {
        size_t length = written < sizeof(line) ? (size_t)written : sizeof(line) - 1;
        append_log(line, length);
    }
    return result;
}

static char *snapshot_logs(void)
{
    char *snapshot = malloc(LOG_RING_SIZE + 1);
    if (snapshot == NULL) {
        return NULL;
    }

    portENTER_CRITICAL(&log_lock);
    size_t start = (log_head + LOG_RING_SIZE - log_length) % LOG_RING_SIZE;
    size_t first_length = LOG_RING_SIZE - start;
    if (first_length > log_length) {
        first_length = log_length;
    }
    memcpy(snapshot, &log_ring[start], first_length);
    memcpy(snapshot + first_length, log_ring, log_length - first_length);
    snapshot[log_length] = '\0';
    portEXIT_CRITICAL(&log_lock);
    return snapshot;
}

static bool json_escape(const char *input, char *output, size_t output_size)
{
    static const char hex[] = "0123456789abcdef";
    size_t output_index = 0;

    for (size_t input_index = 0; input[input_index] != '\0'; input_index++) {
        const unsigned char character = (unsigned char)input[input_index];
        const char *escape = NULL;
        switch (character) {
        case '\"': escape = "\\\""; break;
        case '\\': escape = "\\\\"; break;
        case '\b': escape = "\\b"; break;
        case '\f': escape = "\\f"; break;
        case '\n': escape = "\\n"; break;
        case '\r': escape = "\\r"; break;
        case '\t': escape = "\\t"; break;
        default: break;
        }

        if (escape != NULL) {
            if (output_index + 2 >= output_size) {
                return false;
            }
            output[output_index++] = escape[0];
            output[output_index++] = escape[1];
        } else if (character < 0x20) {
            if (output_index + 6 >= output_size) {
                return false;
            }
            output[output_index++] = '\\';
            output[output_index++] = 'u';
            output[output_index++] = '0';
            output[output_index++] = '0';
            output[output_index++] = hex[character >> 4];
            output[output_index++] = hex[character & 0x0f];
        } else {
            if (output_index + 1 >= output_size) {
                return false;
            }
            output[output_index++] = (char)character;
        }
    }
    output[output_index] = '\0';
    return true;
}

static esp_err_t asset_handler(httpd_req_t *request)
{
    const web_asset_t *asset = request->user_ctx;
    httpd_resp_set_type(request, asset->content_type);
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    return httpd_resp_send(request, (const char *)asset->start,
                           asset->end - asset->start);
}

static esp_err_t favicon_handler(httpd_req_t *request)
{
    httpd_resp_set_status(request, "204 No Content");
    return httpd_resp_send(request, NULL, 0);
}

static esp_err_t send_json(httpd_req_t *request, const char *body)
{
    httpd_resp_set_type(request, "application/json");
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    return httpd_resp_sendstr(request, body);
}

static void restart_timer_callback(void *argument)
{
    (void)argument;
    ESP_LOGW(TAG, "Restarting device after web request");
    esp_restart();
}

#if CONFIG_OPP_CLOCK_SYNC_ENABLED && !CONFIG_OPP_WEB_UI_ENABLED
static void clock_wifi_timer_callback(void *argument)
{
    (void)argument;
    clock_wifi_window_open = false;
    opp_clock_sync_set_network_available(false);
    ESP_LOGI(TAG, "Clock-only Wi-Fi window ended");
    esp_wifi_stop();
}
#endif

static esp_err_t restart_handler(httpd_req_t *request)
{
    if (esp_timer_is_active(restart_timer)) {
        httpd_resp_set_status(request, "409 Conflict");
        return send_json(request, "{\"error\":\"Restart already scheduled\"}");
    }

    char content_type[32];
    const size_t json_type_length = strlen("application/json");
    if (httpd_req_get_hdr_value_str(request, "Content-Type", content_type,
                                    sizeof(content_type)) != ESP_OK ||
        strncmp(content_type, "application/json", json_type_length) != 0 ||
        (content_type[json_type_length] != '\0' && content_type[json_type_length] != ';')) {
        httpd_resp_set_status(request, "415 Unsupported Media Type");
        return send_json(request, "{\"error\":\"Content-Type must be application/json\"}");
    }
    if (request->content_len == 0) {
        httpd_resp_set_status(request, "400 Bad Request");
        return send_json(request, "{\"error\":\"Request body is required\"}");
    }
    if (request->content_len >= RESTART_REQUEST_SIZE) {
        httpd_resp_set_status(request, "413 Payload Too Large");
        return send_json(request, "{\"error\":\"Request body is too large\"}");
    }

    char body[RESTART_REQUEST_SIZE];
    size_t offset = 0;
    while (offset < request->content_len) {
        int received = httpd_req_recv(request, body + offset, request->content_len - offset);
        if (received == HTTPD_SOCK_ERR_TIMEOUT) {
            continue;
        }
        if (received <= 0) {
            httpd_resp_set_status(request, "400 Bad Request");
            return send_json(request, "{\"error\":\"Incomplete request body\"}");
        }
        offset += (size_t)received;
    }
    body[offset] = '\0';

    cJSON *root = cJSON_ParseWithLength(body, offset);
    const cJSON *confirmation = cJSON_GetObjectItemCaseSensitive(root, "confirm");
    bool valid = cJSON_IsObject(root) && root->child != NULL && root->child->next == NULL &&
                 cJSON_IsString(confirmation) &&
                 strcmp(confirmation->valuestring, "restart") == 0;
    cJSON_Delete(root);
    if (!valid) {
        httpd_resp_set_status(request, "400 Bad Request");
        return send_json(request, "{\"error\":\"Expected {\\\"confirm\\\":\\\"restart\\\"}\"}");
    }

    esp_err_t error = esp_timer_start_once(restart_timer, RESTART_DELAY_US);
    if (error != ESP_OK) {
        ESP_LOGE(TAG, "Failed to schedule restart: %s", esp_err_to_name(error));
        httpd_resp_set_status(request, "500 Internal Server Error");
        return send_json(request, "{\"error\":\"Failed to schedule restart\"}");
    }

    ESP_LOGW(TAG, "Device restart requested from web console");
    httpd_resp_set_status(request, "202 Accepted");
    return send_json(request, "{\"status\":\"restarting\"}");
}

static esp_err_t logs_handler(httpd_req_t *request)
{
    char *snapshot = snapshot_logs();
    if (snapshot == NULL) {
        return httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR, "Out of memory");
    }

    httpd_resp_set_type(request, "text/plain");
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    esp_err_t result = httpd_resp_send(request, snapshot, HTTPD_RESP_USE_STRLEN);
    free(snapshot);
    return result;
}

static esp_err_t status_handler(httpd_req_t *request)
{
    float chip_temperature_c;
    esp_err_t temperature_error = temperature_sensor_get_celsius(
        chip_temperature_sensor, &chip_temperature_c);
    char temperature[24];
    if (temperature_error == ESP_OK) {
        snprintf(temperature, sizeof(temperature), "%.1f", chip_temperature_c);
    } else {
        strlcpy(temperature, "null", sizeof(temperature));
    }

    opp_sht45_sample_record_t air_sample;
    int64_t air_sample_age_ms;
    const bool air_sample_valid = opp_sht45_monitor_get_latest(
        &air_sample, &air_sample_age_ms);
    char air_temperature[24];
    char air_humidity[24];
    char air_sample_age[24];
    if (air_sample_valid) {
        snprintf(air_temperature, sizeof(air_temperature), "%.2f",
                 air_sample.values.air_temperature_c);
        snprintf(air_humidity, sizeof(air_humidity), "%.2f",
                 air_sample.values.air_humidity_percent);
        snprintf(air_sample_age, sizeof(air_sample_age), "%lld", air_sample_age_ms);
    } else {
        strlcpy(air_temperature, "null", sizeof(air_temperature));
        strlcpy(air_humidity, "null", sizeof(air_humidity));
        strlcpy(air_sample_age, "null", sizeof(air_sample_age));
    }

    opp_clock_status_t clock_status;
    opp_clock_get_status(&clock_status);
    char date_time_utc[32] = "null";
    char last_sync_utc[32] = "null";
    char sample_time_utc[32] = "null";
    char unix_time[24] = "null";
    char last_sync_time[24] = "null";
    char clock_adjustment[24] = "null";
    char sample_sequence[24] = "null";
    char sample_monotonic[24] = "null";
    char sample_unix_time[24] = "null";
    if (clock_status.time_valid) {
        time_t now = (time_t)clock_status.unix_time_s;
        struct tm utc;
        gmtime_r(&now, &utc);
        strftime(date_time_utc, sizeof(date_time_utc), "\"%Y-%m-%dT%H:%M:%SZ\"", &utc);
        snprintf(unix_time, sizeof(unix_time), "%lld",
                 (long long)clock_status.unix_time_s);
    }
    if (clock_status.last_sync_unix_s > 0) {
        struct tm utc;
        time_t last_sync = (time_t)clock_status.last_sync_unix_s;
        gmtime_r(&last_sync, &utc);
        strftime(last_sync_utc, sizeof(last_sync_utc), "\"%Y-%m-%dT%H:%M:%SZ\"", &utc);
        snprintf(last_sync_time, sizeof(last_sync_time), "%lld",
                 (long long)clock_status.last_sync_unix_s);
    }
    if (clock_status.adjustment_known) {
        snprintf(clock_adjustment, sizeof(clock_adjustment), "%lld",
                 (long long)clock_status.last_adjustment_ms);
    }
    if (air_sample_valid) {
        snprintf(sample_sequence, sizeof(sample_sequence), "%llu",
                 (unsigned long long)air_sample.sequence);
        snprintf(sample_monotonic, sizeof(sample_monotonic), "%lld",
                 (long long)air_sample.monotonic_ms);
        if (air_sample.time_valid) {
            snprintf(sample_unix_time, sizeof(sample_unix_time), "%lld",
                     (long long)air_sample.unix_time_ms);
            time_t sample_seconds = (time_t)(air_sample.unix_time_ms / 1000);
            struct tm utc;
            gmtime_r(&sample_seconds, &utc);
            strftime(sample_time_utc, sizeof(sample_time_utc),
                     "\"%Y-%m-%dT%H:%M:%SZ\"", &utc);
        }
    }

    const char *sync_state = "unsynchronized";
    if (clock_status.sync_in_progress) {
        sync_state = "synchronizing";
    } else if (clock_status.time_valid && clock_status.sync_due) {
        sync_state = "correction due";
    } else if (clock_status.time_valid && clock_status.rtc_retained) {
        sync_state = "synchronized";
    } else if (clock_status.time_valid) {
        sync_state = "plausible";
    }

    const esp_app_desc_t *app = esp_app_get_description();
    const bool usb_connected = usb_serial_jtag_is_connected();
    char escaped_ssid[sizeof(station_ssid) * 6];
    if (!json_escape(station_ssid, escaped_ssid, sizeof(escaped_ssid))) {
        strlcpy(escaped_ssid, "unknown", sizeof(escaped_ssid));
    }
    opp_device_config_t device_config;
    opp_device_config_store_get(&device_config);
    opp_force_report_status_t force_report;
    opp_force_report_get_status(&force_report);
    char escaped_plant_name[sizeof(device_config.plant_name) * 6];
    char escaped_room[sizeof(device_config.room) * 6];
    if (!json_escape(device_config.plant_name, escaped_plant_name, sizeof(escaped_plant_name))) {
        strlcpy(escaped_plant_name, "", sizeof(escaped_plant_name));
    }
    if (!json_escape(device_config.room, escaped_room, sizeof(escaped_room))) {
        strlcpy(escaped_room, "", sizeof(escaped_room));
    }
    char force_report_packet_id[5];
    if (force_report.packet_id_valid) {
        snprintf(force_report_packet_id, sizeof(force_report_packet_id), "%u",
                 force_report.packet_id);
    } else {
        strlcpy(force_report_packet_id, "null", sizeof(force_report_packet_id));
    }
    char *status = malloc(STATUS_JSON_MAX);
    if (status == NULL) {
        return httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR,
                                   "Out of memory");
    }
    int status_length = snprintf(status, STATUS_JSON_MAX,
             "{\"firmware_version\":\"%s\",\"connected\":%s,\"ssid\":\"%s\","
             "\"ip\":\"%s\",\"mac\":\"%s\","
             "\"usb_connected\":%s,\"power_source\":\"%s\","
             "\"uptime_s\":%lld,\"free_heap\":%lu,\"chip_temperature_c\":%s,"
             "\"clock_valid\":%s,\"clock_sync_state\":\"%s\",\"date_time_utc\":%s,"
             "\"unix_time_s\":%s,\"rtc_clock_source\":\"internal RC slow clock\","
             "\"rtc_retained\":%s,\"last_clock_sync_utc\":%s,\"last_clock_sync_unix_s\":%s,"
             "\"last_clock_adjustment_ms\":%s,\"clock_sync_failures\":%lu,"
             "\"sht45_enabled\":%s,\"sht45_available\":%s,\"air_temperature_c\":%s,"
             "\"air_humidity_percent\":%s,\"air_sample_age_ms\":%s,"
             "\"air_sample_sequence\":%s,\"air_sample_monotonic_ms\":%s,"
             "\"air_sample_unix_ms\":%s,\"air_sample_time_utc\":%s,"
             "\"device_config_revision\":%lu,\"plant_name\":\"%s\",\"room\":\"%s\","
             "\"reporting_interval_seconds\":%lu,"
             "\"force_report_state\":\"%s\",\"force_report_error\":\"%s\","
             "\"force_report_request_id\":%lu,\"force_report_packet_id\":%s,"
             "\"force_report_requested_at_ms\":%lld,"
             "\"force_report_completed_at_ms\":%lld,"
             "\"force_report_acknowledged_at_ms\":%lld}",
             app->version, station_connected ? "true" : "false", escaped_ssid,
             station_ip, station_mac,
             usb_connected ? "true" : "false",
             opp_power_source_status_value(usb_connected),
             esp_timer_get_time() / 1000000,
             (unsigned long)esp_get_free_heap_size(), temperature,
             clock_status.time_valid ? "true" : "false", sync_state, date_time_utc,
             unix_time, clock_status.rtc_retained ? "true" : "false", last_sync_utc,
             last_sync_time, clock_adjustment, (unsigned long)clock_status.sync_failures,
             opp_sht45_monitor_is_enabled() ? "true" : "false",
             air_sample_valid ? "true" : "false", air_temperature,
             air_humidity, air_sample_age, sample_sequence, sample_monotonic,
             sample_unix_time, sample_time_utc, (unsigned long)device_config.revision,
             escaped_plant_name, escaped_room,
             (unsigned long)device_config.reporting_interval_seconds,
             opp_force_report_state_name(force_report.state),
             opp_force_report_failure_name(force_report.failure),
             (unsigned long)force_report.request_id,
             force_report_packet_id,
             (long long)force_report.requested_at_ms,
             (long long)force_report.report_completed_at_ms,
             (long long)force_report.acknowledged_at_ms);
    if (status_length < 0 || status_length >= STATUS_JSON_MAX) {
        free(status);
        return httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR,
                                   "Could not serialize status");
    }
    httpd_resp_set_type(request, "application/json");
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    esp_err_t result = httpd_resp_send(request, status, status_length);
    free(status);
    return result;
}

static esp_err_t config_get_handler(httpd_req_t *request)
{
    char *config = malloc(WEB_UI_CONFIG_JSON_MAX);
    if (config == NULL) {
        return httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR,
                                   "Out of memory");
    }
    if (web_ui_config_read(config, WEB_UI_CONFIG_JSON_MAX) != ESP_OK) {
        free(config);
        return httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR,
                                   "Could not serialize configuration");
    }
    httpd_resp_set_type(request, "application/json");
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    esp_err_t error = httpd_resp_sendstr(request, config);
    free(config);
    return error;
}

static esp_err_t config_put_handler(httpd_req_t *request)
{
    if (request->content_len <= 0 || request->content_len >= WEB_UI_CONFIG_JSON_MAX) {
        return httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST,
                                   "Configuration body is empty or too large");
    }

    char content_type[32];
    if (httpd_req_get_hdr_value_str(request, "Content-Type", content_type,
                                    sizeof(content_type)) != ESP_OK ||
        strncmp(content_type, "application/json", strlen("application/json")) != 0) {
        return httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST,
                                   "Content-Type must be application/json");
    }

    char *body = malloc(request->content_len + 1);
    if (body == NULL) {
        return httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR,
                                   "Out of memory");
    }
    size_t received = 0;
    while (received < request->content_len) {
        int result = httpd_req_recv(request, body + received,
                                    request->content_len - received);
        if (result == HTTPD_SOCK_ERR_TIMEOUT) {
            continue;
        }
        if (result <= 0) {
            free(body);
            return ESP_FAIL;
        }
        received += (size_t)result;
    }
    body[received] = '\0';

    char validation_error[96];
    esp_err_t error = web_ui_config_write(body, received, validation_error,
                                          sizeof(validation_error));
    free(body);
    if (error == ESP_ERR_INVALID_ARG) {
        return httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, validation_error);
    }
    if (error != ESP_OK) {
        return httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR,
                                   validation_error);
    }
    opp_sht45_monitor_set_enabled(web_ui_config_sht45_enabled());
    ESP_LOGI(TAG, "UI configuration updated");
    return config_get_handler(request);
}

static esp_err_t config_reset_handler(httpd_req_t *request)
{
    if (web_ui_config_reset() != ESP_OK) {
        return httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR,
                                   "Could not reset configuration");
    }
    opp_sht45_monitor_set_enabled(web_ui_config_sht45_enabled());
    ESP_LOGI(TAG, "UI configuration reset to defaults");
    return config_get_handler(request);
}

static esp_err_t force_report_handler(httpd_req_t *request)
{
    uint32_t request_id;
    esp_err_t error = opp_force_report_request(&request_id);
    if (error == ESP_ERR_INVALID_STATE) {
        httpd_resp_set_status(request, "409 Conflict");
        return httpd_resp_sendstr(request, "A forced report is already in progress");
    }
    if (error != ESP_OK) {
        return httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR,
                                   "Could not queue forced report");
    }

    char response[48];
    int length = snprintf(response, sizeof(response),
                          "{\"request_id\":%lu,\"state\":\"queued\"}",
                          (unsigned long)request_id);
    ESP_LOGI(TAG, "Forced BLE report request %lu queued", (unsigned long)request_id);
    httpd_resp_set_type(request, "application/json");
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    return httpd_resp_send(request, response, length);
}

static esp_err_t start_http_server(void)
{
    if (http_server != NULL) {
        return ESP_OK;
    }

    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.max_uri_handlers = 12;
    config.stack_size = 6144;
    ESP_RETURN_ON_ERROR(httpd_start(&http_server, &config), TAG,
                        "Failed to start HTTP server");

    const httpd_uri_t routes[] = {
        {.uri = "/", .method = HTTP_GET, .handler = asset_handler, .user_ctx = &index_asset},
        {.uri = "/app.css", .method = HTTP_GET, .handler = asset_handler, .user_ctx = &css_asset},
        {.uri = "/app.js", .method = HTTP_GET, .handler = asset_handler, .user_ctx = &js_asset},
        {.uri = "/favicon.ico", .method = HTTP_GET, .handler = favicon_handler},
        {.uri = "/logs", .method = HTTP_GET, .handler = logs_handler},
        {.uri = "/status", .method = HTTP_GET, .handler = status_handler},
        {.uri = "/api/v1/config/ui", .method = HTTP_GET, .handler = config_get_handler},
        {.uri = "/api/v1/config/ui", .method = HTTP_PUT, .handler = config_put_handler},
        {.uri = "/api/v1/config/ui/reset", .method = HTTP_POST, .handler = config_reset_handler},
        {.uri = "/api/v1/reports/force", .method = HTTP_POST, .handler = force_report_handler},
        {.uri = "/api/v1/restart", .method = HTTP_POST, .handler = restart_handler},
    };
    for (size_t index = 0; index < sizeof(routes) / sizeof(routes[0]); index++) {
        esp_err_t error = httpd_register_uri_handler(http_server, &routes[index]);
        if (error != ESP_OK) {
            httpd_stop(http_server);
            http_server = NULL;
            return error;
        }
    }
    return ESP_OK;
}

static void wifi_event_handler(void *argument, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        station_connected = false;
        opp_clock_sync_set_network_available(false);
        strlcpy(station_ip, "disconnected", sizeof(station_ip));
#if CONFIG_OPP_WEB_UI_ENABLED
        ESP_LOGW(TAG, "Wi-Fi disconnected; reconnecting");
        esp_wifi_connect();
#else
        if (clock_wifi_window_open) {
            ESP_LOGW(TAG, "Wi-Fi disconnected during clock synchronization; reconnecting");
            esp_wifi_connect();
        }
#endif
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = event_data;
        station_connected = true;
        opp_clock_sync_set_network_available(true);
        snprintf(station_ip, sizeof(station_ip), IPSTR, IP2STR(&event->ip_info.ip));
#if CONFIG_OPP_WEB_UI_ENABLED
        if (start_http_server() == ESP_OK) {
            ESP_LOGI(TAG, "Web diagnostics ready at http://%s", station_ip);
        }
#endif
    }
}

esp_err_t web_ui_start(void)
{
#if !CONFIG_OPP_WEB_UI_ENABLED && !CONFIG_OPP_CLOCK_SYNC_ENABLED
    return ESP_OK;
#else
#if CONFIG_OPP_WEB_UI_ENABLED
    console_vprintf = esp_log_set_vprintf(web_ui_vprintf);
#endif
    if (CONFIG_OPP_WIFI_SSID[0] == '\0') {
        ESP_LOGW(TAG, "Wi-Fi disabled: configure OPP_WIFI_SSID in menuconfig");
        return ESP_OK;
    }
#if CONFIG_OPP_CLOCK_SYNC_ENABLED && !CONFIG_OPP_WEB_UI_ENABLED
    if (!opp_clock_sync_needed()) {
        ESP_LOGI(TAG, "Clock is current; clock-only Wi-Fi remains off");
        return ESP_OK;
    }
    clock_wifi_window_open = true;
#endif

#if CONFIG_OPP_WEB_UI_ENABLED
    const esp_timer_create_args_t restart_timer_args = {
        .callback = restart_timer_callback,
        .name = "web_restart",
    };
    ESP_RETURN_ON_ERROR(esp_timer_create(&restart_timer_args, &restart_timer), TAG,
                        "Failed to create restart timer");
#endif
#if CONFIG_OPP_CLOCK_SYNC_ENABLED && !CONFIG_OPP_WEB_UI_ENABLED
    const esp_timer_create_args_t clock_wifi_timer_args = {
        .callback = clock_wifi_timer_callback,
        .name = "clock_wifi",
    };
    ESP_RETURN_ON_ERROR(esp_timer_create(&clock_wifi_timer_args, &clock_wifi_timer), TAG,
                        "Failed to create clock Wi-Fi timer");
#endif
    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "Failed to initialise network interface");
    ESP_RETURN_ON_ERROR(esp_event_loop_create_default(), TAG, "Failed to create event loop");
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t wifi_init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_wifi_init(&wifi_init), TAG, "Failed to initialise Wi-Fi");
    ESP_RETURN_ON_ERROR(
        esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL),
        TAG, "Failed to register Wi-Fi event handler");
    ESP_RETURN_ON_ERROR(
        esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL),
        TAG, "Failed to register IP event handler");

    wifi_config_t wifi_config = {
        .sta = {
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
            .pmf_cfg = {.capable = true, .required = false},
        },
    };
    strlcpy((char *)wifi_config.sta.ssid, CONFIG_OPP_WIFI_SSID,
            sizeof(wifi_config.sta.ssid));
    strlcpy(station_ssid, CONFIG_OPP_WIFI_SSID, sizeof(station_ssid));
    strlcpy((char *)wifi_config.sta.password, CONFIG_OPP_WIFI_PASSWORD,
            sizeof(wifi_config.sta.password));

    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "Failed to set station mode");
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_STA, &wifi_config), TAG,
                        "Failed to configure Wi-Fi");
    ESP_RETURN_ON_ERROR(esp_wifi_set_ps(WIFI_PS_NONE), TAG, "Failed to disable Wi-Fi power save");

#if CONFIG_OPP_WEB_UI_ENABLED
    temperature_sensor_config_t temperature_config =
        TEMPERATURE_SENSOR_CONFIG_DEFAULT(-10, 80);
    ESP_RETURN_ON_ERROR(
        temperature_sensor_install(&temperature_config, &chip_temperature_sensor),
        TAG, "Failed to install chip temperature sensor");
    ESP_RETURN_ON_ERROR(temperature_sensor_enable(chip_temperature_sensor), TAG,
                        "Failed to enable chip temperature sensor");
#endif

    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "Failed to start Wi-Fi");
#if CONFIG_OPP_CLOCK_SYNC_ENABLED && !CONFIG_OPP_WEB_UI_ENABLED
    ESP_RETURN_ON_ERROR(
        esp_timer_start_once(clock_wifi_timer,
                             (uint64_t)CONFIG_OPP_CLOCK_WIFI_WINDOW_SECONDS * 1000000),
        TAG, "Failed to start clock Wi-Fi timer");
#endif

    uint8_t mac[6];
    ESP_RETURN_ON_ERROR(esp_wifi_get_mac(WIFI_IF_STA, mac), TAG, "Failed to read Wi-Fi MAC");
    snprintf(station_mac, sizeof(station_mac), "%02x:%02x:%02x:%02x:%02x:%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    ESP_LOGI(TAG, "Connecting to '%s' with MAC %s", CONFIG_OPP_WIFI_SSID, station_mac);
    return ESP_OK;
#endif
}