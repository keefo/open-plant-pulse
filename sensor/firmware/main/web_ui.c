#include "web_ui.h"

#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "driver/temperature_sensor.h"
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
#include "lwip/ip4_addr.h"
#include "nvs_flash.h"
#include "web_ui_config.h"

#define LOG_RING_SIZE (16 * 1024)
#define LOG_LINE_SIZE 512
#define RESTART_REQUEST_SIZE 64
#define RESTART_DELAY_US (750 * 1000)

static const char *TAG = "web_ui";
static char log_ring[LOG_RING_SIZE];
static size_t log_head;
static size_t log_length;
static portMUX_TYPE log_lock = portMUX_INITIALIZER_UNLOCKED;
static vprintf_like_t console_vprintf;
static httpd_handle_t http_server;
static char station_ip[IP4ADDR_STRLEN_MAX] = "disconnected";
static char station_mac[18] = "unknown";
static bool station_connected;
static temperature_sensor_handle_t chip_temperature_sensor;
static esp_timer_handle_t restart_timer;

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

    const esp_app_desc_t *app = esp_app_get_description();
    char status[288];
    snprintf(status, sizeof(status),
             "{\"firmware_version\":\"%s\",\"connected\":%s,\"ip\":\"%s\",\"mac\":\"%s\","
             "\"uptime_s\":%lld,\"free_heap\":%lu,\"chip_temperature_c\":%s}",
             app->version, station_connected ? "true" : "false", station_ip, station_mac,
             esp_timer_get_time() / 1000000,
             (unsigned long)esp_get_free_heap_size(), temperature);
    httpd_resp_set_type(request, "application/json");
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    return httpd_resp_sendstr(request, status);
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
    ESP_LOGI(TAG, "UI configuration updated");
    return config_get_handler(request);
}

static esp_err_t config_reset_handler(httpd_req_t *request)
{
    if (web_ui_config_reset() != ESP_OK) {
        return httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR,
                                   "Could not reset configuration");
    }
    ESP_LOGI(TAG, "UI configuration reset to defaults");
    return config_get_handler(request);
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
        strlcpy(station_ip, "disconnected", sizeof(station_ip));
        ESP_LOGW(TAG, "Wi-Fi disconnected; reconnecting");
        esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = event_data;
        station_connected = true;
        snprintf(station_ip, sizeof(station_ip), IPSTR, IP2STR(&event->ip_info.ip));
        if (start_http_server() == ESP_OK) {
            ESP_LOGI(TAG, "Web diagnostics ready at http://%s", station_ip);
        }
    }
}

static esp_err_t initialise_nvs(void)
{
    esp_err_t error = nvs_flash_init();
    if (error == ESP_ERR_NVS_NO_FREE_PAGES || error == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_RETURN_ON_ERROR(nvs_flash_erase(), TAG, "Failed to erase NVS");
        error = nvs_flash_init();
    }
    return error;
}

esp_err_t web_ui_start(void)
{
#if !CONFIG_OPP_WEB_UI_ENABLED
    return ESP_OK;
#else
    console_vprintf = esp_log_set_vprintf(web_ui_vprintf);
    if (CONFIG_OPP_WIFI_SSID[0] == '\0') {
        ESP_LOGW(TAG, "Web diagnostics disabled: configure OPP_WIFI_SSID in menuconfig");
        return ESP_OK;
    }

    ESP_RETURN_ON_ERROR(initialise_nvs(), TAG, "Failed to initialise NVS");
    ESP_RETURN_ON_ERROR(web_ui_config_load(), TAG, "Failed to load UI configuration");
    const esp_timer_create_args_t restart_timer_args = {
        .callback = restart_timer_callback,
        .name = "web_restart",
    };
    ESP_RETURN_ON_ERROR(esp_timer_create(&restart_timer_args, &restart_timer), TAG,
                        "Failed to create restart timer");
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
    strlcpy((char *)wifi_config.sta.password, CONFIG_OPP_WIFI_PASSWORD,
            sizeof(wifi_config.sta.password));

    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "Failed to set station mode");
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_STA, &wifi_config), TAG,
                        "Failed to configure Wi-Fi");
    ESP_RETURN_ON_ERROR(esp_wifi_set_ps(WIFI_PS_NONE), TAG, "Failed to disable Wi-Fi power save");

    temperature_sensor_config_t temperature_config =
        TEMPERATURE_SENSOR_CONFIG_DEFAULT(-10, 80);
    ESP_RETURN_ON_ERROR(
        temperature_sensor_install(&temperature_config, &chip_temperature_sensor),
        TAG, "Failed to install chip temperature sensor");
    ESP_RETURN_ON_ERROR(temperature_sensor_enable(chip_temperature_sensor), TAG,
                        "Failed to enable chip temperature sensor");

    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "Failed to start Wi-Fi");

    uint8_t mac[6];
    ESP_RETURN_ON_ERROR(esp_wifi_get_mac(WIFI_IF_STA, mac), TAG, "Failed to read Wi-Fi MAC");
    snprintf(station_mac, sizeof(station_mac), "%02x:%02x:%02x:%02x:%02x:%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    ESP_LOGI(TAG, "Connecting to '%s' with MAC %s", CONFIG_OPP_WIFI_SSID, station_mac);
    return ESP_OK;
#endif
}