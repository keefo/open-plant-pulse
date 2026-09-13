#include "web_ui_config.h"

#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_check.h"
#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"

#define CONFIG_NAMESPACE "web_ui"
#define CONFIG_KEY "ui_config"
#define CONFIG_SCHEMA_VERSION 6
#define SCHEMA_VERSION_WITH_SENSORS 5
#define SCHEMA_VERSION_WITH_CLOCK 4
#define SCHEMA_VERSION_WITH_CURRENT_CARDS 3
#define SCHEMA_VERSION_WITH_CLOCK_CARDS 2
#define INITIAL_CONFIG_SCHEMA_VERSION 1
#define CONFIG_CARD_COUNT 7
#define SCHEMA_V3_TO_V5_CARD_COUNT 6
#define SCHEMA_V2_CARD_COUNT 7
#define SCHEMA_V1_CARD_COUNT 5

static const char *TAG = "web_ui_config";

typedef struct {
    const char *id;
    bool visible;
    uint8_t span;
} dashboard_card_t;

typedef struct {
    char theme_mode[12];
    char accent[8];
    char density[12];
    char timezone[64];
    bool sht45_enabled;
    bool soil_probe_enabled;
    dashboard_card_t cards[CONFIG_CARD_COUNT];
} ui_config_t;

static const ui_config_t default_config = {
    .theme_mode = "dark",
    .accent = "green",
    .density = "comfortable",
    .timezone = "UTC",
    .sht45_enabled = true,
    .soil_probe_enabled = true,
    .cards = {
        {.id = "device", .visible = true, .span = 1},
        {.id = "power", .visible = true, .span = 1},
        {.id = "uptime", .visible = true, .span = 1},
        {.id = "heap", .visible = true, .span = 1},
        {.id = "chip_temperature", .visible = true, .span = 1},
        {.id = "date_time", .visible = true, .span = 1},
        {.id = "rtc", .visible = true, .span = 1},
    },
};
static ui_config_t current_config;

static void set_error(char *output, size_t output_size, const char *message)
{
    if (output != NULL && output_size > 0) {
        strlcpy(output, message, output_size);
    }
}

static bool is_one_of(const char *value, const char *const *choices, size_t choice_count)
{
    if (value == NULL) {
        return false;
    }
    for (size_t index = 0; index < choice_count; index++) {
        if (strcmp(value, choices[index]) == 0) {
            return true;
        }
    }
    return false;
}

static int card_index(const char *id)
{
    for (size_t index = 0; index < CONFIG_CARD_COUNT; index++) {
        if (id != NULL && strcmp(id, default_config.cards[index].id) == 0) {
            return (int)index;
        }
    }
    return -1;
}

static bool has_only_keys(const cJSON *object, const char *const *keys, size_t key_count)
{
    const cJSON *item;
    cJSON_ArrayForEach(item, object) {
        if (!is_one_of(item->string, keys, key_count)) {
            return false;
        }
    }
    return true;
}

static bool is_valid_timezone(const char *timezone)
{
    if (timezone == NULL) {
        return false;
    }

    size_t length = strlen(timezone);
    if (length == 0 || length >= sizeof(current_config.timezone) ||
        timezone[0] == '/' || timezone[length - 1] == '/' ||
        strstr(timezone, "//") != NULL || strstr(timezone, "..") != NULL) {
        return false;
    }

    for (size_t index = 0; index < length; index++) {
        const char character = timezone[index];
        if (!((character >= 'A' && character <= 'Z') ||
              (character >= 'a' && character <= 'z') ||
              (character >= '0' && character <= '9') ||
              character == '/' || character == '_' || character == '-' || character == '+')) {
            return false;
        }
    }
    return true;
}

static bool parse_config(const char *json, size_t length, ui_config_t *config,
                         char *error_message, size_t error_message_size)
{
    const char *const root_keys[] = {"schema_version", "theme", "dashboard", "clock", "sensors"};
    const char *const clock_root_keys[] = {"schema_version", "theme", "dashboard", "clock"};
    const char *const legacy_root_keys[] = {"schema_version", "theme", "dashboard"};
    const char *const theme_keys[] = {"mode", "accent"};
    const char *const dashboard_keys[] = {"density", "cards"};
    const char *const clock_keys[] = {"timezone"};
    const char *const sensor_keys[] = {"sht45", "soil_probe"};
    const char *const sensor_config_keys[] = {"enabled"};
    const char *const card_keys[] = {"id", "visible", "span"};
    const char *const theme_modes[] = {"dark", "light", "system"};
    const char *const accents[] = {"green", "blue", "amber"};
    const char *const densities[] = {"comfortable", "compact"};

    cJSON *root = cJSON_ParseWithLength(json, length);
    if (!cJSON_IsObject(root)) {
        set_error(error_message, error_message_size, "Invalid configuration object");
        cJSON_Delete(root);
        return false;
    }

    const cJSON *schema_version = cJSON_GetObjectItemCaseSensitive(root, "schema_version");
    const cJSON *theme = cJSON_GetObjectItemCaseSensitive(root, "theme");
    const cJSON *dashboard = cJSON_GetObjectItemCaseSensitive(root, "dashboard");
    const cJSON *clock = cJSON_GetObjectItemCaseSensitive(root, "clock");
    const cJSON *sensors = cJSON_GetObjectItemCaseSensitive(root, "sensors");
    const int schema = cJSON_IsNumber(schema_version) ? schema_version->valueint : 0;
    const char *const *accepted_root_keys = schema >= SCHEMA_VERSION_WITH_SENSORS
        ? root_keys
        : (schema == SCHEMA_VERSION_WITH_CLOCK ? clock_root_keys : legacy_root_keys);
    const size_t accepted_root_key_count = schema >= SCHEMA_VERSION_WITH_SENSORS
        ? sizeof(root_keys) / sizeof(root_keys[0])
        : (schema == SCHEMA_VERSION_WITH_CLOCK
               ? sizeof(clock_root_keys) / sizeof(clock_root_keys[0])
               : sizeof(legacy_root_keys) / sizeof(legacy_root_keys[0]));
    if (!cJSON_IsNumber(schema_version) ||
        schema_version->valuedouble != schema_version->valueint ||
        (schema_version->valueint != CONFIG_SCHEMA_VERSION &&
         schema_version->valueint != SCHEMA_VERSION_WITH_SENSORS &&
         schema_version->valueint != SCHEMA_VERSION_WITH_CLOCK &&
         schema_version->valueint != SCHEMA_VERSION_WITH_CURRENT_CARDS &&
         schema_version->valueint != SCHEMA_VERSION_WITH_CLOCK_CARDS &&
         schema_version->valueint != INITIAL_CONFIG_SCHEMA_VERSION) ||
        !has_only_keys(root, accepted_root_keys, accepted_root_key_count) ||
        !cJSON_IsObject(theme) || !cJSON_IsObject(dashboard) ||
        !has_only_keys(theme, theme_keys, sizeof(theme_keys) / sizeof(theme_keys[0])) ||
        !has_only_keys(dashboard, dashboard_keys,
                       sizeof(dashboard_keys) / sizeof(dashboard_keys[0])) ||
        (schema >= SCHEMA_VERSION_WITH_CLOCK &&
         (!cJSON_IsObject(clock) ||
          !has_only_keys(clock, clock_keys, sizeof(clock_keys) / sizeof(clock_keys[0])))) ||
        (schema >= SCHEMA_VERSION_WITH_SENSORS &&
         (!cJSON_IsObject(sensors) ||
          !has_only_keys(sensors, sensor_keys, sizeof(sensor_keys) / sizeof(sensor_keys[0]))))) {
        set_error(error_message, error_message_size, "Unsupported or incomplete configuration");
        cJSON_Delete(root);
        return false;
    }

    const cJSON *mode = cJSON_GetObjectItemCaseSensitive(theme, "mode");
    const cJSON *accent = cJSON_GetObjectItemCaseSensitive(theme, "accent");
    const cJSON *density = cJSON_GetObjectItemCaseSensitive(dashboard, "density");
    const cJSON *cards = cJSON_GetObjectItemCaseSensitive(dashboard, "cards");
    const cJSON *timezone = cJSON_GetObjectItemCaseSensitive(clock, "timezone");
    const cJSON *sht45 = cJSON_GetObjectItemCaseSensitive(sensors, "sht45");
    const cJSON *soil_probe = cJSON_GetObjectItemCaseSensitive(sensors, "soil_probe");
    const cJSON *sht45_enabled = cJSON_GetObjectItemCaseSensitive(sht45, "enabled");
    const cJSON *soil_probe_enabled = cJSON_GetObjectItemCaseSensitive(soil_probe, "enabled");
    if (!cJSON_IsString(mode) ||
        !is_one_of(mode->valuestring, theme_modes, sizeof(theme_modes) / sizeof(theme_modes[0])) ||
        !cJSON_IsString(accent) ||
        !is_one_of(accent->valuestring, accents, sizeof(accents) / sizeof(accents[0])) ||
        !cJSON_IsString(density) ||
        !is_one_of(density->valuestring, densities, sizeof(densities) / sizeof(densities[0])) ||
        !cJSON_IsArray(cards)) {
        set_error(error_message, error_message_size, "Invalid theme or dashboard settings");
        cJSON_Delete(root);
        return false;
    }
    if (schema >= SCHEMA_VERSION_WITH_CLOCK &&
        (!cJSON_IsString(timezone) || !is_valid_timezone(timezone->valuestring))) {
        set_error(error_message, error_message_size, "Invalid clock settings");
        cJSON_Delete(root);
        return false;
    }
    if (schema >= SCHEMA_VERSION_WITH_SENSORS &&
        (!cJSON_IsObject(sht45) || !cJSON_IsObject(soil_probe) ||
         !has_only_keys(sht45, sensor_config_keys,
                        sizeof(sensor_config_keys) / sizeof(sensor_config_keys[0])) ||
         !has_only_keys(soil_probe, sensor_config_keys,
                        sizeof(sensor_config_keys) / sizeof(sensor_config_keys[0])) ||
         !cJSON_IsBool(sht45_enabled) || !cJSON_IsBool(soil_probe_enabled))) {
        set_error(error_message, error_message_size, "Invalid sensor settings");
        cJSON_Delete(root);
        return false;
    }

    const size_t input_card_count = (size_t)cJSON_GetArraySize(cards);
    size_t expected_card_count = CONFIG_CARD_COUNT;
    if (schema >= SCHEMA_VERSION_WITH_CURRENT_CARDS && schema < CONFIG_SCHEMA_VERSION) {
        expected_card_count = SCHEMA_V3_TO_V5_CARD_COUNT;
    } else if (schema_version->valueint == SCHEMA_VERSION_WITH_CLOCK_CARDS) {
        expected_card_count = SCHEMA_V2_CARD_COUNT;
    } else if (schema_version->valueint == INITIAL_CONFIG_SCHEMA_VERSION) {
        expected_card_count = SCHEMA_V1_CARD_COUNT;
    }
    if (input_card_count != expected_card_count) {
        set_error(error_message, error_message_size, "Invalid dashboard card count");
        cJSON_Delete(root);
        return false;
    }

    ui_config_t candidate = {0};
    strlcpy(candidate.theme_mode, mode->valuestring, sizeof(candidate.theme_mode));
    strlcpy(candidate.accent, accent->valuestring, sizeof(candidate.accent));
    strlcpy(candidate.density, density->valuestring, sizeof(candidate.density));
    strlcpy(candidate.timezone,
            schema >= SCHEMA_VERSION_WITH_CLOCK ? timezone->valuestring
                                                : default_config.timezone,
            sizeof(candidate.timezone));
    candidate.sht45_enabled = schema >= SCHEMA_VERSION_WITH_SENSORS
        ? cJSON_IsTrue(sht45_enabled)
        : default_config.sht45_enabled;
    candidate.soil_probe_enabled = schema >= SCHEMA_VERSION_WITH_SENSORS
        ? cJSON_IsTrue(soil_probe_enabled)
        : default_config.soil_probe_enabled;
    bool seen[CONFIG_CARD_COUNT] = {false};
    bool legacy_mac_seen = false;
    bool legacy_mac_visible = false;
    bool any_visible = false;
    size_t output_index = 0;
    for (size_t index = 0; index < input_card_count; index++) {
        const cJSON *card = cJSON_GetArrayItem(cards, index);
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(card, "id");
        const cJSON *visible = cJSON_GetObjectItemCaseSensitive(card, "visible");
        const cJSON *span = cJSON_GetObjectItemCaseSensitive(card, "span");
        const bool legacy_mac = schema_version->valueint < SCHEMA_VERSION_WITH_CURRENT_CARDS &&
            cJSON_IsString(id) && strcmp(id->valuestring, "mac") == 0;
        int known_index = cJSON_IsString(id) ? card_index(id->valuestring) : -1;
        if (!cJSON_IsObject(card) ||
            !has_only_keys(card, card_keys, sizeof(card_keys) / sizeof(card_keys[0])) ||
            (!legacy_mac && (known_index < 0 || seen[known_index])) ||
            (legacy_mac && legacy_mac_seen) || !cJSON_IsBool(visible) ||
            !cJSON_IsNumber(span) || (span->valueint != 1 && span->valueint != 2) ||
            span->valuedouble != span->valueint) {
            set_error(error_message, error_message_size, "Invalid dashboard card configuration");
            cJSON_Delete(root);
            return false;
        }
        if (legacy_mac) {
            legacy_mac_seen = true;
            legacy_mac_visible = cJSON_IsTrue(visible);
            continue;
        }
        seen[known_index] = true;
        candidate.cards[output_index].id = default_config.cards[known_index].id;
        candidate.cards[output_index].visible = cJSON_IsTrue(visible);
        candidate.cards[output_index].span = (uint8_t)span->valueint;
        any_visible = any_visible || candidate.cards[output_index].visible;
        output_index++;
    }
    for (size_t index = 0; index < CONFIG_CARD_COUNT; index++) {
        if (!seen[index]) {
            candidate.cards[output_index++] = default_config.cards[index];
            any_visible = any_visible || default_config.cards[index].visible;
        }
    }
    if (legacy_mac_visible) {
        for (size_t index = 0; index < CONFIG_CARD_COUNT; index++) {
            if (strcmp(candidate.cards[index].id, "device") == 0) {
                candidate.cards[index].visible = true;
                any_visible = true;
                break;
            }
        }
    }
    if (!any_visible) {
        set_error(error_message, error_message_size, "At least one dashboard card must be visible");
        cJSON_Delete(root);
        return false;
    }

    *config = candidate;
    cJSON_Delete(root);
    return true;
}

static esp_err_t serialize_config(const ui_config_t *config, char *output, size_t output_size)
{
    cJSON *root = cJSON_CreateObject();
    cJSON *theme = cJSON_CreateObject();
    cJSON *dashboard = cJSON_CreateObject();
    cJSON *clock = cJSON_CreateObject();
    cJSON *sensors = cJSON_CreateObject();
    cJSON *sht45 = cJSON_CreateObject();
    cJSON *soil_probe = cJSON_CreateObject();
    cJSON *cards = cJSON_CreateArray();
    if (root == NULL || theme == NULL || dashboard == NULL || clock == NULL ||
        sensors == NULL || sht45 == NULL || soil_probe == NULL || cards == NULL) {
        cJSON_Delete(root);
        cJSON_Delete(theme);
        cJSON_Delete(dashboard);
        cJSON_Delete(clock);
        cJSON_Delete(sensors);
        cJSON_Delete(sht45);
        cJSON_Delete(soil_probe);
        cJSON_Delete(cards);
        return ESP_ERR_NO_MEM;
    }

    cJSON_AddNumberToObject(root, "schema_version", CONFIG_SCHEMA_VERSION);
    cJSON_AddStringToObject(theme, "mode", config->theme_mode);
    cJSON_AddStringToObject(theme, "accent", config->accent);
    cJSON_AddItemToObject(root, "theme", theme);
    cJSON_AddStringToObject(dashboard, "density", config->density);
    cJSON_AddItemToObject(dashboard, "cards", cards);
    cJSON_AddItemToObject(root, "dashboard", dashboard);
    cJSON_AddStringToObject(clock, "timezone", config->timezone);
    cJSON_AddItemToObject(root, "clock", clock);
    cJSON_AddBoolToObject(sht45, "enabled", config->sht45_enabled);
    cJSON_AddItemToObject(sensors, "sht45", sht45);
    cJSON_AddBoolToObject(soil_probe, "enabled", config->soil_probe_enabled);
    cJSON_AddItemToObject(sensors, "soil_probe", soil_probe);
    cJSON_AddItemToObject(root, "sensors", sensors);

    for (size_t index = 0; index < CONFIG_CARD_COUNT; index++) {
        cJSON *card = cJSON_CreateObject();
        if (card == NULL) {
            cJSON_Delete(root);
            return ESP_ERR_NO_MEM;
        }
        cJSON_AddStringToObject(card, "id", config->cards[index].id);
        cJSON_AddBoolToObject(card, "visible", config->cards[index].visible);
        cJSON_AddNumberToObject(card, "span", config->cards[index].span);
        cJSON_AddItemToArray(cards, card);
    }

    bool printed = cJSON_PrintPreallocated(root, output, (int)output_size, false);
    cJSON_Delete(root);
    return printed ? ESP_OK : ESP_ERR_INVALID_SIZE;
}

esp_err_t web_ui_config_init(void)
{
    esp_err_t error = nvs_flash_init();
    if (error == ESP_ERR_NVS_NO_FREE_PAGES || error == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_RETURN_ON_ERROR(nvs_flash_erase(), TAG, "Failed to erase NVS");
        error = nvs_flash_init();
    }
    ESP_RETURN_ON_ERROR(error, TAG, "Failed to initialise NVS");
    return web_ui_config_load();
}

esp_err_t web_ui_config_load(void)
{
    current_config = default_config;

    nvs_handle_t handle = 0;
    esp_err_t error = nvs_open(CONFIG_NAMESPACE, NVS_READONLY, &handle);
    if (error == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    if (error != ESP_OK) {
        return error;
    }

    size_t length = 0;
    error = nvs_get_str(handle, CONFIG_KEY, NULL, &length);
    if (error == ESP_ERR_NVS_NOT_FOUND) {
        nvs_close(handle);
        return ESP_OK;
    }
    if (error != ESP_OK || length > WEB_UI_CONFIG_JSON_MAX) {
        nvs_close(handle);
        return error == ESP_OK ? ESP_ERR_INVALID_SIZE : error;
    }

    char *json = malloc(length);
    if (json == NULL) {
        nvs_close(handle);
        return ESP_ERR_NO_MEM;
    }
    error = nvs_get_str(handle, CONFIG_KEY, json, &length);
    nvs_close(handle);
    if (error != ESP_OK) {
        free(json);
        return error;
    }

    ui_config_t loaded;
    char validation_error[96];
    if (!parse_config(json, length - 1, &loaded, validation_error, sizeof(validation_error))) {
        free(json);
        ESP_LOGW(TAG, "Stored UI configuration ignored: %s", validation_error);
        return ESP_OK;
    }
    free(json);
    current_config = loaded;
    return ESP_OK;
}

esp_err_t web_ui_config_read(char *output, size_t output_size)
{
    return serialize_config(&current_config, output, output_size);
}

esp_err_t web_ui_config_write(const char *json, size_t length,
                              char *error_message, size_t error_message_size)
{
    ui_config_t candidate;
    if (!parse_config(json, length, &candidate, error_message, error_message_size)) {
        return ESP_ERR_INVALID_ARG;
    }

    char *canonical = malloc(WEB_UI_CONFIG_JSON_MAX);
    if (canonical == NULL) {
        set_error(error_message, error_message_size, "Out of memory");
        return ESP_ERR_NO_MEM;
    }
    esp_err_t error = serialize_config(&candidate, canonical, WEB_UI_CONFIG_JSON_MAX);
    if (error != ESP_OK) {
        free(canonical);
        set_error(error_message, error_message_size, "Configuration is too large");
        return error;
    }

    nvs_handle_t handle = 0;
    error = nvs_open(CONFIG_NAMESPACE, NVS_READWRITE, &handle);
    if (error == ESP_OK) {
        error = nvs_set_str(handle, CONFIG_KEY, canonical);
    }
    if (error == ESP_OK) {
        error = nvs_commit(handle);
    }
    if (handle != 0) {
        nvs_close(handle);
    }
    free(canonical);
    if (error != ESP_OK) {
        set_error(error_message, error_message_size, "Could not persist configuration");
        return error;
    }

    current_config = candidate;
    return ESP_OK;
}

esp_err_t web_ui_config_reset(void)
{
    nvs_handle_t handle;
    esp_err_t error = nvs_open(CONFIG_NAMESPACE, NVS_READWRITE, &handle);
    if (error != ESP_OK) {
        return error;
    }
    error = nvs_erase_key(handle, CONFIG_KEY);
    if (error == ESP_ERR_NVS_NOT_FOUND) {
        error = ESP_OK;
    }
    if (error == ESP_OK) {
        error = nvs_commit(handle);
    }
    nvs_close(handle);
    if (error == ESP_OK) {
        current_config = default_config;
    }
    return error;
}

bool web_ui_config_sht45_enabled(void)
{
    return current_config.sht45_enabled;
}

bool web_ui_config_soil_probe_enabled(void)
{
    return current_config.soil_probe_enabled;
}