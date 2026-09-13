#include "web_ui_config.h"

#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "nvs.h"

#define CONFIG_NAMESPACE "web_ui"
#define CONFIG_KEY "ui_config"
#define CONFIG_SCHEMA_VERSION 1
#define CONFIG_CARD_COUNT 5

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
    dashboard_card_t cards[CONFIG_CARD_COUNT];
} ui_config_t;

static const ui_config_t default_config = {
    .theme_mode = "dark",
    .accent = "green",
    .density = "comfortable",
    .cards = {
        {.id = "device", .visible = true, .span = 1},
        {.id = "uptime", .visible = true, .span = 1},
        {.id = "heap", .visible = true, .span = 1},
        {.id = "mac", .visible = true, .span = 1},
        {.id = "chip_temperature", .visible = true, .span = 1},
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

static bool parse_config(const char *json, size_t length, ui_config_t *config,
                         char *error_message, size_t error_message_size)
{
    const char *const root_keys[] = {"schema_version", "theme", "dashboard"};
    const char *const theme_keys[] = {"mode", "accent"};
    const char *const dashboard_keys[] = {"density", "cards"};
    const char *const card_keys[] = {"id", "visible", "span"};
    const char *const theme_modes[] = {"dark", "light", "system"};
    const char *const accents[] = {"green", "blue", "amber"};
    const char *const densities[] = {"comfortable", "compact"};

    cJSON *root = cJSON_ParseWithLength(json, length);
    if (!cJSON_IsObject(root) ||
        !has_only_keys(root, root_keys, sizeof(root_keys) / sizeof(root_keys[0]))) {
        set_error(error_message, error_message_size, "Invalid configuration object");
        cJSON_Delete(root);
        return false;
    }

    const cJSON *schema_version = cJSON_GetObjectItemCaseSensitive(root, "schema_version");
    const cJSON *theme = cJSON_GetObjectItemCaseSensitive(root, "theme");
    const cJSON *dashboard = cJSON_GetObjectItemCaseSensitive(root, "dashboard");
    if (!cJSON_IsNumber(schema_version) || schema_version->valueint != CONFIG_SCHEMA_VERSION ||
        !cJSON_IsObject(theme) || !cJSON_IsObject(dashboard) ||
        !has_only_keys(theme, theme_keys, sizeof(theme_keys) / sizeof(theme_keys[0])) ||
        !has_only_keys(dashboard, dashboard_keys,
                       sizeof(dashboard_keys) / sizeof(dashboard_keys[0]))) {
        set_error(error_message, error_message_size, "Unsupported or incomplete configuration");
        cJSON_Delete(root);
        return false;
    }

    const cJSON *mode = cJSON_GetObjectItemCaseSensitive(theme, "mode");
    const cJSON *accent = cJSON_GetObjectItemCaseSensitive(theme, "accent");
    const cJSON *density = cJSON_GetObjectItemCaseSensitive(dashboard, "density");
    const cJSON *cards = cJSON_GetObjectItemCaseSensitive(dashboard, "cards");
    if (!cJSON_IsString(mode) ||
        !is_one_of(mode->valuestring, theme_modes, sizeof(theme_modes) / sizeof(theme_modes[0])) ||
        !cJSON_IsString(accent) ||
        !is_one_of(accent->valuestring, accents, sizeof(accents) / sizeof(accents[0])) ||
        !cJSON_IsString(density) ||
        !is_one_of(density->valuestring, densities, sizeof(densities) / sizeof(densities[0])) ||
        !cJSON_IsArray(cards) || cJSON_GetArraySize(cards) != CONFIG_CARD_COUNT) {
        set_error(error_message, error_message_size, "Invalid theme or dashboard settings");
        cJSON_Delete(root);
        return false;
    }

    ui_config_t candidate = {0};
    strlcpy(candidate.theme_mode, mode->valuestring, sizeof(candidate.theme_mode));
    strlcpy(candidate.accent, accent->valuestring, sizeof(candidate.accent));
    strlcpy(candidate.density, density->valuestring, sizeof(candidate.density));
    bool seen[CONFIG_CARD_COUNT] = {false};
    bool any_visible = false;
    for (size_t index = 0; index < CONFIG_CARD_COUNT; index++) {
        const cJSON *card = cJSON_GetArrayItem(cards, index);
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(card, "id");
        const cJSON *visible = cJSON_GetObjectItemCaseSensitive(card, "visible");
        const cJSON *span = cJSON_GetObjectItemCaseSensitive(card, "span");
        int known_index = cJSON_IsString(id) ? card_index(id->valuestring) : -1;
        if (!cJSON_IsObject(card) ||
            !has_only_keys(card, card_keys, sizeof(card_keys) / sizeof(card_keys[0])) ||
            known_index < 0 || seen[known_index] || !cJSON_IsBool(visible) ||
            !cJSON_IsNumber(span) || (span->valueint != 1 && span->valueint != 2) ||
            span->valuedouble != span->valueint) {
            set_error(error_message, error_message_size, "Invalid dashboard card configuration");
            cJSON_Delete(root);
            return false;
        }
        seen[known_index] = true;
        candidate.cards[index].id = default_config.cards[known_index].id;
        candidate.cards[index].visible = cJSON_IsTrue(visible);
        candidate.cards[index].span = (uint8_t)span->valueint;
        any_visible = any_visible || candidate.cards[index].visible;
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
    cJSON *cards = cJSON_CreateArray();
    if (root == NULL || theme == NULL || dashboard == NULL || cards == NULL) {
        cJSON_Delete(root);
        cJSON_Delete(theme);
        cJSON_Delete(dashboard);
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