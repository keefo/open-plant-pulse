#include "devices.h"

#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "nvs.h"

static const char *TAG = "devices";

typedef struct {
    bool used;
    ezb_extaddr_t eui;
    char eui_text[EUI_TEXT_LEN];
    uint16_t short_addr;
    int64_t last_seen_us;
} device_t;

static device_t s_devices[DEVICES_MAX];

/* The table outlives a restart.
 *
 * It lived only in RAM, and losing it is worse than it sounds: a frame
 * from a short address this gateway cannot place is dropped, so after a
 * gateway restart a perfectly healthy scale reports every five minutes
 * into nothing. It still gets its acknowledgement from the stack, so it
 * believes all is well and its own self-heal never fires. Three evenings
 * of silence were this, and nothing in either device looked wrong.
 *
 * A saved short address can go stale -- they are reassigned on every
 * rejoin -- but a device announcing itself overwrites the entry, and a
 * stale guess that gets corrected beats no guess that gets discarded. */
#define DEVICES_NVS_NAMESPACE "zbgw"
#define DEVICES_NVS_KEY "devices"

typedef struct {
    ezb_extaddr_t eui;
    uint16_t short_addr;
} saved_device_t;

static void devices_save(void)
{
    saved_device_t saved[DEVICES_MAX];
    size_t n = 0;
    for (size_t i = 0; i < DEVICES_MAX; i++) {
        if (!s_devices[i].used) continue;
        saved[n].eui = s_devices[i].eui;
        saved[n].short_addr = s_devices[i].short_addr;
        n++;
    }
    nvs_handle_t h;
    if (nvs_open(DEVICES_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return;
    if (n) nvs_set_blob(h, DEVICES_NVS_KEY, saved, n * sizeof saved[0]);
    else nvs_erase_key(h, DEVICES_NVS_KEY);
    nvs_commit(h);
    nvs_close(h);
}

void devices_load(void)
{
    nvs_handle_t h;
    if (nvs_open(DEVICES_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return;
    saved_device_t saved[DEVICES_MAX];
    size_t size = sizeof saved;
    if (nvs_get_blob(h, DEVICES_NVS_KEY, saved, &size) == ESP_OK) {
        for (size_t i = 0; i < size / sizeof saved[0]; i++)
            devices_seen(saved[i].short_addr, &saved[i].eui);
        ESP_LOGI(TAG, "remembered %u device(s) from an earlier run",
                 (unsigned)(size / sizeof saved[0]));
    }
    nvs_close(h);
}

// EUI-64 bytes are stored little-endian; printed big-endian, the way the
// address appears on a label and in every other tool.
static void eui_to_text(const ezb_extaddr_t *eui, char out[EUI_TEXT_LEN])
{
    const uint8_t *b = (const uint8_t *)eui;
    for (int i = 0; i < 8; i++) {
        static const char hex[] = "0123456789abcdef";
        out[i * 2] = hex[b[7 - i] >> 4];
        out[i * 2 + 1] = hex[b[7 - i] & 0x0F];
    }
    out[16] = '\0';
}

static device_t *find_by_eui_text(const char *eui_text)
{
    for (size_t i = 0; i < DEVICES_MAX; i++)
        if (s_devices[i].used && !strcasecmp(s_devices[i].eui_text, eui_text))
            return &s_devices[i];
    return NULL;
}

void devices_seen(uint16_t short_addr, const ezb_extaddr_t *eui)
{
    char text[EUI_TEXT_LEN];
    eui_to_text(eui, text);

    device_t *slot = find_by_eui_text(text);
    if (!slot) {
        for (size_t i = 0; i < DEVICES_MAX; i++) {
            if (!s_devices[i].used) {
                slot = &s_devices[i];
                break;
            }
        }
        if (!slot) {
            ESP_LOGW(TAG, "no room for %s; table holds %d", text, DEVICES_MAX);
            return;
        }
        slot->used = true;
        memcpy(&slot->eui, eui, sizeof slot->eui);
        memcpy(slot->eui_text, text, sizeof text);
        slot->short_addr = short_addr;
        ESP_LOGI(TAG, "%s is 0x%04x", text, (unsigned)short_addr);
        devices_save();
    } else if (slot->short_addr != short_addr) {
        // Expected, not an anomaly: this is the reassignment that makes short
        // addresses useless as an identity.
        ESP_LOGI(TAG, "%s moved from 0x%04x to 0x%04x", text,
                 (unsigned)slot->short_addr, (unsigned)short_addr);
        slot->short_addr = short_addr;
        devices_save();
    }
    slot->last_seen_us = esp_timer_get_time();
}

bool devices_seen_short(uint16_t short_addr)
{
    ezb_extaddr_t eui;
    if (ezb_address_extended_by_short(short_addr, &eui) != 0) return false;
    devices_seen(short_addr, &eui);
    return true;
}

uint16_t devices_short_for(const char *eui_text)
{
    device_t *slot = find_by_eui_text(eui_text);
    return slot ? slot->short_addr : 0xFFFE;
}

bool devices_eui_for(uint16_t short_addr, char out[EUI_TEXT_LEN])
{
    for (size_t i = 0; i < DEVICES_MAX; i++) {
        if (s_devices[i].used && s_devices[i].short_addr == short_addr) {
            memcpy(out, s_devices[i].eui_text, EUI_TEXT_LEN);
            return true;
        }
    }
    return false;
}

bool devices_address_for(const char *eui_text, ezb_extaddr_t *out)
{
    device_t *slot = find_by_eui_text(eui_text);
    if (!slot) return false;
    memcpy(out, &slot->eui, sizeof *out);
    return true;
}

bool devices_forget(const char *eui_text)
{
    device_t *slot = find_by_eui_text(eui_text);
    if (!slot) return false;
    slot->used = false;
    return true;
}

bool devices_at(size_t index, char eui_out[EUI_TEXT_LEN], uint16_t *short_out,
                int64_t *last_seen_us)
{
    size_t seen = 0;
    for (size_t i = 0; i < DEVICES_MAX; i++) {
        if (!s_devices[i].used) continue;
        if (seen++ != index) continue;
        memcpy(eui_out, s_devices[i].eui_text, EUI_TEXT_LEN);
        *short_out = s_devices[i].short_addr;
        *last_seen_us = s_devices[i].last_seen_us;
        return true;
    }
    return false;
}

size_t devices_count(void)
{
    size_t n = 0;
    for (size_t i = 0; i < DEVICES_MAX; i++)
        if (s_devices[i].used) n++;
    return n;
}
