#include "device_identity.h"

#include <inttypes.h>

#include "esp_log.h"
#include "esp_random.h"
#include "nvs.h"

static const char *TAG = "identity";

/* Separate from the configuration namespace on purpose: a factory reset clears
 * "device_cfg" and the bond, and must leave this alone. */
#define IDENTITY_NAMESPACE "factory"
#define PASSKEY_KEY "passkey"
#define ONBOARDED_KEY "onboarded"

static uint32_t cached_passkey;
static bool cached_onboarded;
static bool loaded;

static esp_err_t store_passkey(uint32_t passkey)
{
    nvs_handle_t handle;
    esp_err_t error = nvs_open(IDENTITY_NAMESPACE, NVS_READWRITE, &handle);
    if (error != ESP_OK) {
        return error;
    }
    error = nvs_set_u32(handle, PASSKEY_KEY, passkey);
    if (error == ESP_OK) {
        error = nvs_commit(handle);
    }
    nvs_close(handle);
    return error;
}

esp_err_t opp_device_identity_init(void)
{
    if (loaded) {
        return ESP_OK;
    }

    nvs_handle_t handle;
    esp_err_t error = nvs_open(IDENTITY_NAMESPACE, NVS_READONLY, &handle);
    if (error == ESP_OK) {
        uint32_t stored = 0;
        if (nvs_get_u32(handle, PASSKEY_KEY, &stored) == ESP_OK && stored <= OPP_PASSKEY_MAX) {
            cached_passkey = stored;
        }
        uint8_t onboarded = 0;
        if (nvs_get_u8(handle, ONBOARDED_KEY, &onboarded) == ESP_OK) {
            cached_onboarded = onboarded != 0;
        }
        nvs_close(handle);
    } else if (error != ESP_ERR_NVS_NOT_FOUND) {
        return error;
    }

    if (cached_passkey == 0) {
        /* esp_random is seeded from the hardware RNG once the radio or ADC has
         * been enabled; this runs after nvs_flash_init during start-up, which is
         * late enough for a device that will bring up Bluetooth in this boot. */
        cached_passkey = esp_random() % (OPP_PASSKEY_MAX + 1U);
        error = store_passkey(cached_passkey);
        if (error != ESP_OK) {
            return error;
        }
        ESP_LOGW(TAG, "Generated this device's pairing code: %06" PRIu32, cached_passkey);
        ESP_LOGW(TAG, "Write it on the sensor's label before closing the enclosure");
    }

    loaded = true;
    /* Printed on every boot so the code can be recovered for the label, not only
     * on the boot that generated it.
     *
     * This is deliberately safe only because of where it runs: web_ui_start()
     * installs the log hook that fills the console's 16 KiB ring, and it runs
     * after this. Anything logged here therefore reaches USB serial and nothing
     * else. Moving identity initialisation after web_ui_start() would publish the
     * pairing code on an unauthenticated web page. */
    ESP_LOGW(TAG, "Pairing code for this device's label: %06" PRIu32, cached_passkey);
    ESP_LOGI(TAG, "Device is %s", cached_onboarded ? "onboarded" : "waiting to be added");
    return ESP_OK;
}

uint32_t opp_device_identity_passkey(void)
{
    return cached_passkey;
}

bool opp_device_identity_is_onboarded(void)
{
    return cached_onboarded;
}

esp_err_t opp_device_identity_set_onboarded(bool onboarded)
{
    if (cached_onboarded == onboarded) {
        return ESP_OK;
    }
    nvs_handle_t handle;
    esp_err_t error = nvs_open(IDENTITY_NAMESPACE, NVS_READWRITE, &handle);
    if (error != ESP_OK) {
        return error;
    }
    error = nvs_set_u8(handle, ONBOARDED_KEY, onboarded ? 1 : 0);
    if (error == ESP_OK) {
        error = nvs_commit(handle);
    }
    nvs_close(handle);
    if (error == ESP_OK) {
        cached_onboarded = onboarded;
        ESP_LOGI(TAG, "Device is now %s", onboarded ? "onboarded" : "waiting to be added");
    }
    return error;
}
