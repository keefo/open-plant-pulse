// Who is on the network, and how to reach them.
//
// A Zigbee short address is handed out at join and reused later: one scale
// held 0xc458, then 0x63a1, then 0x40e5 over an evening of rejoining. Its
// EUI-64 never changed. So the hub addresses devices by EUI-64 and this table
// does the translation; nothing above it should ever remember a short address.
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

#include "esp_zigbee.h"

#define DEVICES_MAX 16
#define EUI_TEXT_LEN 17  // 16 hex digits and a terminator

// Records a device, or refreshes one already known. Called when a device
// announces itself and whenever a frame arrives from it.
void devices_seen(uint16_t short_addr, const ezb_extaddr_t *eui);

/* Reload the table saved by an earlier run. Called once at startup:
   without it a gateway restart silently discards every reading from
   a device that does not happen to announce itself again. */
void devices_load(void);

// Records a device known only by its short address, resolving the EUI-64 from
// the stack. Returns false if the stack cannot resolve it.
bool devices_seen_short(uint16_t short_addr);

// The short address to send to, or 0xFFFE when this EUI-64 is unknown.
// Callers must treat 0xFFFE as an error rather than guessing a destination.
uint16_t devices_short_for(const char *eui_text);

// The EUI-64 of a device, as 16 lowercase hex digits. False if not known.
bool devices_eui_for(uint16_t short_addr, char out[EUI_TEXT_LEN]);

// The raw EUI-64 of a known device, for calls that take the address itself
// rather than text. False when the device is not in the table.
bool devices_address_for(const char *eui_text, ezb_extaddr_t *out);

// Drops a device from the table. The network is a separate matter.
bool devices_forget(const char *eui_text);

// Walks the table. Returns false when the index is past the end.
bool devices_at(size_t index, char eui_out[EUI_TEXT_LEN], uint16_t *short_out,
                int64_t *last_seen_us);

size_t devices_count(void);
