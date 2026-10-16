#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* The bulk drain's tokens on the device characteristic. See "Durable delivery
 * and acknowledgement" in protocol/README.md. The queue page itself is built by
 * the report queue, which holds the reports. */
#define OPP_DRAIN_REQUEST 0x20U
#define OPP_DRAIN_ACKNOWLEDGE 0x21U
#define OPP_DRAIN_END 0x22U
#define OPP_DRAIN_ACKNOWLEDGE_SIZE 5

bool opp_drain_is_request(const uint8_t *payload, size_t payload_size);
bool opp_drain_is_end(const uint8_t *payload, size_t payload_size);

/* A cumulative acknowledgement: every queued report up to this ID is stored. */
size_t opp_drain_acknowledge_encode(uint32_t report_id,
                                    uint8_t output[OPP_DRAIN_ACKNOWLEDGE_SIZE]);
bool opp_drain_acknowledge_decode(const uint8_t *payload, size_t payload_size,
                                  uint32_t *report_id);
