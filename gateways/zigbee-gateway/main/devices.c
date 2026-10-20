#include "devices.h"

#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "devices";

typedef struct {
    bool used;
    ezb_extaddr_t eui;
    char eui_text[EUI_TEXT_LEN];
    uint16_t short_addr;
    int64_t last_seen_us;
} device_t;

static device_t s_devices[DEVICES_MAX];

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
    } else if (slot->short_addr != short_addr) {
        // Expected, not an anomaly: this is the reassignment that makes short
        // addresses useless as an identity.
        ESP_LOGI(TAG, "%s moved from 0x%04x to 0x%04x", text,
                 (unsigned)slot->short_addr, (unsigned)short_addr);
        slot->short_addr = short_addr;
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
