#include "firmware_update.h"

#include <string.h>

#include "esp_app_desc.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"

#include "web_ui.h"

static const char *TAG = "firmware_update";

#define UPDATE_TASK_STACK_SIZE 8192
#define UPDATE_TASK_PRIORITY 5
#define DOWNLOAD_CHUNK_SIZE 4096
#define DOWNLOAD_TIMEOUT_MS 20000
/* Long enough for the hub to read the outcome before the sensor disappears.
 * Restarting the instant the image is in place leaves whoever pressed the
 * button looking at "installing" with nothing ever following it. */
#define REBOOT_DELAY_MS 2000

static opp_firmware_update_command_t current_command;
static volatile opp_firmware_state_t current_state = OPP_FIRMWARE_STATE_IDLE;
static volatile uint8_t current_percent;
static volatile opp_firmware_failure_t current_failure = OPP_FIRMWARE_FAILURE_NONE;
static volatile uint32_t running_update_id;
static bool awaiting_confirmation;

static void set_state(opp_firmware_state_t state, uint8_t percent,
                      opp_firmware_failure_t failure)
{
    current_state = state;
    current_percent = percent;
    current_failure = failure;
}

static void fail(opp_firmware_failure_t failure, const char *reason)
{
    ESP_LOGE(TAG, "Update %lu failed: %s", (unsigned long)running_update_id, reason);
    set_state(OPP_FIRMWARE_STATE_FAILED, current_percent, failure);
    running_update_id = 0;
}

esp_err_t opp_firmware_update_init(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    if (esp_ota_get_state_partition(running, &state) == ESP_OK &&
        state == ESP_OTA_IMG_PENDING_VERIFY) {
        /* This image has never proved itself. Saying so in the log makes the
         * difference between a successful update and a rollback visible on the
         * bench, where the two otherwise look identical. */
        awaiting_confirmation = true;
        ESP_LOGW(TAG, "Running a newly installed image on trial from %s", running->label);
    } else {
        ESP_LOGI(TAG, "Running firmware %s from %s", esp_app_get_description()->version,
                 running->label);
    }
    return ESP_OK;
}

void opp_firmware_update_confirm(void)
{
    if (!awaiting_confirmation) {
        return;
    }
    awaiting_confirmation = false;
    const esp_err_t error = esp_ota_mark_app_valid_cancel_rollback();
    if (error == ESP_OK) {
        ESP_LOGI(TAG, "New firmware %s confirmed; rollback cancelled",
                 esp_app_get_description()->version);
    } else {
        ESP_LOGE(TAG, "Could not confirm this image: %s", esp_err_to_name(error));
    }
}

static bool digest_matches(const uint8_t *expected, const uint8_t *actual)
{
    return memcmp(expected, actual, OPP_FIRMWARE_UPDATE_DIGEST_SIZE) == 0;
}

static void update_task(void *context)
{
    (void)context;
    const opp_firmware_update_command_t command = current_command;

    char url[128];
    if (opp_firmware_update_format_url(&command, url, sizeof(url)) == 0) {
        fail(OPP_FIRMWARE_FAILURE_REJECTED, "the address would not fit");
        vTaskDelete(NULL);
        return;
    }

    const esp_partition_t *target = esp_ota_get_next_update_partition(NULL);
    if (target == NULL) {
        /* An image built before this feature has a single application slot and
         * nowhere to put a second one. Only a cable can fix that, and saying so
         * is more useful than a download that cannot be installed. */
        fail(OPP_FIRMWARE_FAILURE_NO_SLOT, "there is no second application slot");
        vTaskDelete(NULL);
        return;
    }
    if (command.size_bytes > target->size) {
        fail(OPP_FIRMWARE_FAILURE_TOO_LARGE, "the image is larger than the slot");
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "Installing %lu bytes from %s into %s", (unsigned long)command.size_bytes, url,
             target->label);
    esp_http_client_config_t http_config = {
        .url = url,
        .timeout_ms = DOWNLOAD_TIMEOUT_MS,
        .keep_alive_enable = true,
    };
    esp_http_client_handle_t client = esp_http_client_init(&http_config);
    if (client == NULL) {
        fail(OPP_FIRMWARE_FAILURE_DOWNLOAD, "no HTTP client");
        vTaskDelete(NULL);
        return;
    }

    uint8_t *buffer = malloc(DOWNLOAD_CHUNK_SIZE);
    if (buffer == NULL) {
        esp_http_client_cleanup(client);
        fail(OPP_FIRMWARE_FAILURE_DOWNLOAD, "no memory for the download");
        vTaskDelete(NULL);
        return;
    }

    esp_ota_handle_t ota = 0;
    bool ota_open = false;
    mbedtls_sha256_context digest;
    mbedtls_sha256_init(&digest);
    opp_firmware_failure_t failure = OPP_FIRMWARE_FAILURE_NONE;
    const char *reason = NULL;

    do {
        esp_err_t error = esp_http_client_open(client, 0);
        if (error != ESP_OK) {
            failure = OPP_FIRMWARE_FAILURE_DOWNLOAD;
            reason = "the hub did not answer";
            break;
        }
        const int64_t content_length = esp_http_client_fetch_headers(client);
        const int status = esp_http_client_get_status_code(client);
        if (status != 200) {
            failure = OPP_FIRMWARE_FAILURE_DOWNLOAD;
            reason = "the hub did not serve the image";
            break;
        }
        if (content_length > 0 && (uint32_t)content_length != command.size_bytes) {
            failure = OPP_FIRMWARE_FAILURE_DOWNLOAD;
            reason = "the hub served a different length";
            break;
        }

        error = esp_ota_begin(target, command.size_bytes, &ota);
        if (error != ESP_OK) {
            failure = OPP_FIRMWARE_FAILURE_FLASH;
            reason = "the slot could not be opened";
            break;
        }
        ota_open = true;
        mbedtls_sha256_starts(&digest, 0);
        set_state(OPP_FIRMWARE_STATE_DOWNLOADING, 0, OPP_FIRMWARE_FAILURE_NONE);

        uint32_t written = 0;
        while (written < command.size_bytes) {
            const int read = esp_http_client_read(client, (char *)buffer, DOWNLOAD_CHUNK_SIZE);
            if (read < 0) {
                failure = OPP_FIRMWARE_FAILURE_DOWNLOAD;
                reason = "the download was interrupted";
                break;
            }
            if (read == 0) {
                failure = OPP_FIRMWARE_FAILURE_DOWNLOAD;
                reason = "the image ended early";
                break;
            }
            error = esp_ota_write(ota, buffer, (size_t)read);
            if (error != ESP_OK) {
                failure = OPP_FIRMWARE_FAILURE_FLASH;
                reason = "the image could not be written";
                break;
            }
            mbedtls_sha256_update(&digest, buffer, (size_t)read);
            written += (uint32_t)read;
            set_state(OPP_FIRMWARE_STATE_DOWNLOADING,
                      (uint8_t)((uint64_t)written * 100U / command.size_bytes),
                      OPP_FIRMWARE_FAILURE_NONE);
        }
        if (reason != NULL) {
            break;
        }

        uint8_t actual[OPP_FIRMWARE_UPDATE_DIGEST_SIZE];
        mbedtls_sha256_finish(&digest, actual);
        if (!digest_matches(command.digest, actual)) {
            /* Everything written is discarded rather than finished: an image
             * that is not the one the hub sent is never made bootable, whatever
             * else it may be. */
            failure = OPP_FIRMWARE_FAILURE_DIGEST;
            reason = "the downloaded image did not match its digest";
            break;
        }

        set_state(OPP_FIRMWARE_STATE_INSTALLING, 100, OPP_FIRMWARE_FAILURE_NONE);
        error = esp_ota_end(ota);
        ota_open = false;
        if (error != ESP_OK) {
            failure = error == ESP_ERR_OTA_VALIDATE_FAILED ? OPP_FIRMWARE_FAILURE_REJECTED
                                                           : OPP_FIRMWARE_FAILURE_FLASH;
            reason = "the image was refused by the bootloader checks";
            break;
        }
        error = esp_ota_set_boot_partition(target);
        if (error != ESP_OK) {
            failure = OPP_FIRMWARE_FAILURE_FLASH;
            reason = "the sensor could not switch slots";
            break;
        }
    } while (false);

    if (ota_open) {
        esp_ota_abort(ota);
    }
    mbedtls_sha256_free(&digest);
    free(buffer);
    esp_http_client_close(client);
    esp_http_client_cleanup(client);

    if (reason != NULL) {
        fail(failure, reason);
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGW(TAG, "Installed update %lu; restarting", (unsigned long)command.update_id);
    set_state(OPP_FIRMWARE_STATE_REBOOTING, 100, OPP_FIRMWARE_FAILURE_NONE);
    vTaskDelay(pdMS_TO_TICKS(REBOOT_DELAY_MS));
    esp_restart();
}

esp_err_t opp_firmware_update_start(const opp_firmware_update_command_t *command)
{
    if (command == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (running_update_id == command->update_id) {
        /* The hub repeats a command until it is told something else, so a
         * repeat is the expected case rather than an error. */
        return ESP_OK;
    }
    if (opp_firmware_update_in_progress()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!web_ui_station_joined()) {
        /* Nothing can be fetched without the household network, and finding
         * that out here costs nothing, where finding it out after a download
         * has started costs the download. */
        set_state(OPP_FIRMWARE_STATE_FAILED, 0, OPP_FIRMWARE_FAILURE_NO_NETWORK);
        return ESP_ERR_INVALID_STATE;
    }

    current_command = *command;
    running_update_id = command->update_id;
    set_state(OPP_FIRMWARE_STATE_COMMANDED, 0, OPP_FIRMWARE_FAILURE_NONE);
    const BaseType_t created = xTaskCreate(update_task, "firmware_update", UPDATE_TASK_STACK_SIZE,
                                           NULL, UPDATE_TASK_PRIORITY, NULL);
    if (created != pdPASS) {
        fail(OPP_FIRMWARE_FAILURE_FLASH, "the update task could not start");
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void opp_firmware_update_status(opp_firmware_state_t *state, uint8_t *percent,
                                opp_firmware_failure_t *failure)
{
    if (state != NULL) {
        *state = current_state;
    }
    if (percent != NULL) {
        *percent = current_percent;
    }
    if (failure != NULL) {
        *failure = current_failure;
    }
}

bool opp_firmware_update_in_progress(void)
{
    return current_state == OPP_FIRMWARE_STATE_COMMANDED ||
           current_state == OPP_FIRMWARE_STATE_DOWNLOADING ||
           current_state == OPP_FIRMWARE_STATE_INSTALLING ||
           current_state == OPP_FIRMWARE_STATE_REBOOTING;
}
